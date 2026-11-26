from __future__ import annotations
from pathlib import Path
import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from .content_registry import ContentRegistry
from .engine.models import GameResponse, Identity
from .handlers.buttons import ButtonHandler
from .handlers.commands import CommandHandler
from .handlers.rate_limiter import InMemoryRateLimiter
from .handlers.pagination import MessagePager
from .modes.party_run import PartyRunService
from .modes.solo_run import SoloRunService
from .storage.repository import GameRepository
_PLUGIN_ID = 'astrbot_plugin_ghost_domain'
_BUTTON_ACTION_ID = f'{_PLUGIN_ID}:action'
_BUTTON_ACTION_TIMEOUT = 86400
_BUTTON_COMMAND_DATA_KEY = 'cmd'
_BUTTON_PATCH_MARKER = '_ghost_domain_command_button_patch'

def _build_qq_command_keyboard(rows) -> dict:
    style_cls = getattr(Comp.Button, 'Style', None)
    style_map = {}
    if style_cls is not None:
        style_map = {getattr(style_cls, name, None): value for name, value in (('DEFAULT', 0), ('PRIMARY', 3), ('SUCCESS', 1), ('DANGER', 0))}
        style_map.pop(None, None)
    keyboard_rows = []
    for row in rows:
        buttons = []
        for button in row.buttons:
            data = getattr(button.action, 'data', None) or {}
            command = str(data.get(_BUTTON_COMMAND_DATA_KEY) or '')
            label = str(button.label)[:16]
            buttons.append({'id': str(button.id)[:64], 'render_data': {'label': label, 'visited_label': label, 'style': style_map.get(button.style, 0)}, 'action': {'type': 2, 'permission': {'type': 2}, 'data': command, 'enter': False, 'unsupport_tips': f'请手动发送已填入输入框的指令：{command}'}})
        keyboard_rows.append({'buttons': buttons})
    return {'content': {'rows': keyboard_rows}}

def _install_qq_command_button_patch() -> bool:
    try:
        from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import QQOfficialMessageEvent as target
    except Exception as error:
        logger.warning(f"鬼域 QQ 指令按钮补丁未安装：{error}")
        return False
    if getattr(target, _BUTTON_PATCH_MARKER, False):
        return True
    original = target._parse_keyboard

    def parse_keyboard(message_chain):
        rows = [component for component in message_chain.chain if isinstance(component, Comp.ActionRow) and component.buttons]
        all_buttons = [button for row in rows for button in row.buttons]
        is_ghost_domain = bool(all_buttons) and all(
            isinstance(getattr(button.action, "data", None), dict)
            and button.action.data.get(_BUTTON_COMMAND_DATA_KEY)
            and button.action.data.get("_ghost_domain_plugin") == _PLUGIN_ID
            for button in all_buttons
        )
        if is_ghost_domain:
            try:
                return _build_qq_command_keyboard(rows)
            except Exception as error:
                logger.warning(f"鬼域 QQ 指令键盘构造失败，回退回调按钮：{error}")
        return original(message_chain)

    try:
        target._parse_keyboard = staticmethod(parse_keyboard)
        setattr(target, _BUTTON_PATCH_MARKER, True)
        return True
    except Exception as error:
        logger.warning(f"鬼域 QQ 指令按钮补丁安装失败：{error}")
        return False
class Main(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None=None):
        super().__init__(context)
        self.config = config or {}
        self.max_buttons = self._config_int('render_max_buttons', 5, 1, 5)
        self.rate_limiter = InMemoryRateLimiter()
        self.message_pager = MessagePager()
        max_slots = self._config_int('game_max_ghost_slots', 2, 1, 6)
        _install_qq_command_button_patch()
        plugin_root = Path(__file__).resolve().parent
        data_root = Path(get_astrbot_data_path()) / 'plugin_data' / _PLUGIN_ID
        self.repository = GameRepository(data_root / 'campaign.sqlite3')
        self.registry = ContentRegistry(plugin_root / 'content' / 'packs')
        self.content_issues = self.registry.load()
        for issue in self.content_issues:
            logger.error(f'鬼域内容包校验失败：{issue}')
        self.solo_runs = SoloRunService(self.repository, self.registry, max_slots)
        self.party_runs = PartyRunService(self.repository, self.registry)
        self.commands = CommandHandler(self.repository, self.registry, self.solo_runs, self.party_runs)
        self.buttons = ButtonHandler(self.repository, self.solo_runs, self.party_runs)

    def _config_int(self, key: str, default: int, low: int, high: int) -> int:
        value = self.config.get(key) if hasattr(self.config, 'get') else None
        if value is None:
            sections = {'render_max_buttons': ('render', 'max_buttons'), 'game_max_ghost_slots': ('game', 'max_ghost_slots')}
            section, nested = sections.get(key, ('', ''))
            block = self.config.get(section) if hasattr(self.config, 'get') else None
            if isinstance(block, dict):
                value = block.get(nested)
        try:
            parsed = int(value) if value is not None else default
        except (TypeError, ValueError):
            parsed = default
        return max(low, min(high, parsed))

    def _identity(self, event: AstrMessageEvent) -> Identity:
        adapter_name = str(event.get_platform_name() or 'unknown')
        return Identity(adapter_id=str(event.get_platform_id() or adapter_name), adapter_name=adapter_name, user_id=str(event.get_sender_id() or ''), group_id=str(event.get_group_id() or ''), message_id=str(getattr(getattr(event, 'message_obj', None), 'message_id', '') or ''))

    @staticmethod
    def _markdown_text(text: str) -> str:
        lines = str(text or '').splitlines()
        return '\n'.join((f'{line.rstrip()}  ' if line.strip() else '' for line in lines)).rstrip()

    def _render(self, event: AstrMessageEvent, response: GameResponse):
        platform = str(event.get_platform_name() or "").casefold()
        is_text_command = "onebot" in platform or "aiocqhttp" in platform or platform == "webchat"
        is_qq_official = "qqofficial" in platform or "qq_official" in platform or "qq-official" in platform
        text = Main._markdown_text(response.text) if is_qq_official else response.text
        set_extra = getattr(event, "set_extra", None)
        if set_extra:
            set_extra("reply_keep_format", True)
        chain = [Comp.Plain(text)]
        buttons = response.buttons[:max(1, min(15, int(getattr(self, "max_buttons", 5))))]
        if buttons:
            if is_text_command:
                input_commands = [f"{button.label}：{button.data['cmd']}" for button in buttons]
                instructions = (
                    "\n可选操作（复制并发送）：\n" + "\n".join(input_commands)
                    if input_commands
                    else "\n当前适配器使用文字指令；发送 `/鬼域 帮助` 查看用法。"
                )
                if platform == "webchat":
                    chain[0] = Comp.Plain(text + instructions)
                else:
                    chain.append(Comp.Plain(instructions))
            else:
                for offset in range(0, len(buttons), 3):
                    row_buttons = buttons[offset:offset + 3]
                    fallback_lines = [f"{button.label}：{button.data['cmd']}" for button in row_buttons]
                    fallback_text = "指令已填入输入框，请手动发送：\n" + "\n".join(fallback_lines) if fallback_lines else "当前平台不支持回调按钮，请根据提示发送对应的 /鬼域 文字指令。"
                    chain.append(Comp.ActionRow(
                        buttons=[
                            Comp.Button(
                                id=f"ghost_domain:{button.action_id}:{offset + index}",
                                label=button.label,
                                action=Comp.Button.CallbackAction(data={**button.data, "_ghost_domain_plugin": _PLUGIN_ID}),
                                style=Comp.Button.Style.PRIMARY,
                            )
                            for index, button in enumerate(row_buttons)
                        ],
                        action_timeout=_BUTTON_ACTION_TIMEOUT,
                        fallback_text=fallback_text,
                    ))
        result = event.chain_result(chain)
        if is_qq_official:
            use_markdown = getattr(result, "use_markdown", None)
            if use_markdown:
                use_markdown(True)
        return result
    def _get_rate_limited_result(self, event: AstrMessageEvent, identity: Identity):
        limiter = getattr(self, 'rate_limiter', None)
        if limiter is None:
            limiter = InMemoryRateLimiter()
            self.rate_limiter = limiter
        allowed, notify = limiter.check(identity)
        if allowed:
            return None
        if notify:
            return event.plain_result('操作过于频繁，请稍后再试。')
        return event.chain_result([])

    def _get_message_pager(self) -> MessagePager:
        pager = getattr(self, 'message_pager', None)
        if pager is None:
            pager = MessagePager()
            self.message_pager = pager
        return pager

    def _render_response(self, event: AstrMessageEvent, response: GameResponse, identity: Identity | None = None):
        identity = identity or self._identity(event)
        return self._render(event, self._get_message_pager().start(identity, response))

    def _respond(self, event: AstrMessageEvent, command: str, argument: str=''):
        event.stop_event()
        identity = self._identity(event)
        limited = self._get_rate_limited_result(event, identity)
        if limited is not None:
            return limited
        response = self.commands.dispatch(identity, command, argument)
        return self._render_response(event, response, identity)

    @filter.command_group('鬼域', alias={'/鬼域'})
    def ghost_domain(self):
        pass

    @ghost_domain.command('帮助')
    async def help(self, event: AstrMessageEvent):
        yield self._respond(event, '帮助')

    @ghost_domain.command('更多')
    async def more(self, event: AstrMessageEvent):
        event.stop_event()
        identity = self._identity(event)
        limited = self._get_rate_limited_result(event, identity)
        if limited is not None:
            yield limited
            return
        response = self._get_message_pager().next(identity)
        yield self._render(event, response)

    @ghost_domain.command('开始')
    async def recruit(self, event: AstrMessageEvent):
        yield self._respond(event, '开始')

    @ghost_domain.command('开局')
    async def open_run(self, event: AstrMessageEvent, domain: str='七中学校', mode: str='', difficulty: str=''):
        request = ' '.join((part for part in (domain, mode, difficulty) if part)).strip()
        yield self._respond(event, '开局', request)

    @ghost_domain.command('委托')
    async def contracts(self, event: AstrMessageEvent):
        yield self._respond(event, '委托')

    @ghost_domain.command('商店')
    async def shop(self, event: AstrMessageEvent):
        yield self._respond(event, '商店')

    @ghost_domain.command('购买')
    async def purchase(self, event: AstrMessageEvent, item: str=''):
        yield self._respond(event, '购买', item)

    @ghost_domain.command('章节')
    async def chapters(self, event: AstrMessageEvent):
        yield self._respond(event, '章节')

    @ghost_domain.command('存档')
    async def save(self, event: AstrMessageEvent):
        yield self._respond(event, '存档')

    @ghost_domain.command('状态')
    async def status(self, event: AstrMessageEvent):
        yield self._respond(event, '状态')

    @ghost_domain.command('编队')
    async def formation(self, event: AstrMessageEvent, operation: str='', ghost_id: str=''):
        argument = ' '.join(part for part in (operation, ghost_id) if part)
        yield self._respond(event, '编队', argument)

    @ghost_domain.command('加入')
    async def join(self, event: AstrMessageEvent, invite_code: str=''):
        yield self._respond(event, '加入', invite_code)

    @ghost_domain.command('出战')
    async def select_ghost(self, event: AstrMessageEvent, ghost_name: str=''):
        yield self._respond(event, '出战', ghost_name)

    @ghost_domain.command('准备')
    async def ready(self, event: AstrMessageEvent):
        yield self._respond(event, '准备')

    @ghost_domain.command('开始任务')
    async def start_party(self, event: AstrMessageEvent):
        yield self._respond(event, '开始任务')

    @ghost_domain.command('离队')
    async def leave(self, event: AstrMessageEvent):
        yield self._respond(event, '离队')

    @ghost_domain.command('放弃')
    async def abandon(self, event: AstrMessageEvent):
        yield self._respond(event, '放弃')

    @ghost_domain.command('前进')
    async def advance(self, event: AstrMessageEvent):
        yield self._respond(event, '前进')

    @ghost_domain.command('调查')
    async def investigate(self, event: AstrMessageEvent):
        yield self._respond(event, '调查')

    @ghost_domain.command('交谈')
    async def talk(self, event: AstrMessageEvent, character_name: str=''):
        yield self._respond(event, '交谈', character_name)

    @ghost_domain.command('选择')
    async def choose(self, event: AstrMessageEvent, character_id: str='', choice_id: str=''):
        yield self._respond(event, '选择', ' '.join((part for part in (character_id, choice_id) if part)))

    @ghost_domain.command('观察')
    async def observe(self, event: AstrMessageEvent, target: str=''):
        yield self._respond(event, '观察', target)

    @ghost_domain.command('指挥')
    async def command_ghost(self, event: AstrMessageEvent, argument: GreedyStr):
        yield self._respond(event, '指挥', argument)

    @ghost_domain.command('使用')
    async def use_item(self, event: AstrMessageEvent, item: str='', target: str=''):
        yield self._respond(event, '使用', ' '.join((part for part in (item, target) if part)))

    @ghost_domain.command('捕获')
    async def capture(self, event: AstrMessageEvent, target: str='', item: str=''):
        yield self._respond(event, '捕获', ' '.join((part for part in (target, item) if part)))

    @ghost_domain.command('护主')
    async def guard(self, event: AstrMessageEvent):
        yield self._respond(event, '护主')

    @ghost_domain.command('跳过')
    async def skip(self, event: AstrMessageEvent):
        yield self._respond(event, '跳过')

    @ghost_domain.command('撤退')
    async def retreat(self, event: AstrMessageEvent):
        yield self._respond(event, '撤退')

    @ghost_domain.command('撤离')
    async def extract(self, event: AstrMessageEvent):
        yield self._respond(event, '撤离')

    @ghost_domain.command('死机')
    async def deadlock(self, event: AstrMessageEvent, target: str=''):
        yield self._respond(event, '死机', target)

    @ghost_domain.command('确认')
    async def confirm_button(self, event: AstrMessageEvent, action_id: str=''):
        event.stop_event()
        identity = self._identity(event)
        limited = self._get_rate_limited_result(event, identity)
        if limited is not None:
            yield limited
            return
        if not action_id:
            yield event.plain_result('确认指令缺少按钮编号，请从最新的确认按钮重新操作。')
            return
        response = self.buttons.dispatch(identity, action_id)
        yield self._render_response(event, response, identity)

    @ghost_domain.command('图鉴')
    async def journal(self, event: AstrMessageEvent, query: str='', detail: str=''):
        argument = ' '.join(part for part in (query, detail) if part)
        yield self._respond(event, '图鉴', argument)

    @ghost_domain.command('背包')
    async def inventory(self, event: AstrMessageEvent):
        yield self._respond(event, '背包')

    @filter.button_action()
    async def on_button(self, event: AstrMessageEvent):
        interaction = event.get_button_interaction()
        if interaction is None or not isinstance(interaction.data, dict):
            return
        if interaction.data.get("_ghost_domain_plugin") != _PLUGIN_ID:
            return
        event.stop_event()
        identity = self._identity(event)
        limited = self._get_rate_limited_result(event, identity)
        if limited is not None:
            yield limited
            return
        command = interaction.data.get("cmd")
        if command is not None:
            if not isinstance(command, str) or not command.startswith("/鬼域 "):
                yield event.plain_result("按钮指令无效；请发送 `/鬼域 帮助` 查看用法。")
                return
            yield event.plain_result(f"按钮未自动执行，请复制并手动发送：\n{command}")
            return
        action_id = interaction.data.get("action_id")
        if not isinstance(action_id, str) or not action_id:
            yield event.plain_result("按钮数据无效；请使用文字指令重新操作。")
            return
        response = self.buttons.dispatch(identity, action_id)
        yield self._render_response(event, response, identity)



