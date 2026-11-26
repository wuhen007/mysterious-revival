from __future__ import annotations
import hashlib
import time
from typing import Any
from ..engine.campaign import difficulty_is_unlocked
from ..engine.ghost_control import resolve_owned_ghost_entry
from ..engine.models import (ActionResult, ButtonSpec, GameResponse, Identity)
from ..engine.daily import summary as daily_summary
from ..engine.progression import (LEGACY_NAMES, domain_available, rank_for, unlock_requirements)
from ..engine.recovery import lifespan_label
from ..modes.party_run import PartyRunService
from ..modes.solo_run import SoloRunService
from ..storage.repository import GameRepository
_GROUP_ONLY_COMMANDS = {'准备', '出战', '加入', '离队', '开始任务'}

class CommandHandler:
    def __init__(self, repository: GameRepository, registry: Any, solo_runs: SoloRunService, party_runs: PartyRunService):
        self.repository = repository
        self.registry = registry
        self.solo_runs = solo_runs
        self.party_runs = party_runs

    @staticmethod
    def _supports_callback_buttons(identity: Identity) -> bool:
        platform = identity.adapter_name.casefold()
        return 'onebot' not in platform and 'aiocqhttp' not in platform

    def dispatch(self, identity: Identity, command: str, argument: str='') -> GameResponse:
        handlers = {'帮助': self.help, '开始': self.recruit, '开局': self.open_run, '委托': self.contracts, '商店': self.shop, '购买': self.purchase, '状态': self.status, '加入': self.join, '准备': self.ready, '开始任务': self.start_party, '离队': self.leave, '放弃': self.abandon, '前进': self.forward, '调查': self.investigate, '交谈': self.talk, '选择': self.choose, '图鉴': self.journal, '编队': self.formation, '出战': self.select_ghost, '观察': self.observe, '指挥': self.command_ghost, '使用': self.use_item, '捕获': self.capture, '护主': self.guard, '跳过': self.skip, '死机': self.deadlock, '撤退': self.retreat, '撤离': self.extract, '背包': self.inventory, '章节': self.chapters, '存档': self.save}
        handler = handlers.get(command)
        if handler is None:
            return GameResponse('未识别的指令。发送 `/鬼域 帮助` 查看可用指令。')
        if command in _GROUP_ONLY_COMMANDS and not identity.group_id:
            return GameResponse('组队指令仅支持群聊；私聊可以使用总部、委托和单人副本指令。')
        return handler(identity, argument.strip())

    @staticmethod
    def help(_identity: Identity, _argument: str='') -> GameResponse:
        return GameResponse('鬼域求生\n/鬼域 开始 — 接受总部招募并永久获得一只随机初始驭鬼\n/鬼域 章节 — 查看主线章节、解锁状态与可选势力副本\n/鬼域 存档 — 查看档案、代号评定、通关进度与结局\n/鬼域 委托 — 查看普通/困难委托解锁\n/鬼域 商店、购买 槽位 — 查看或扩展驭鬼槽位（6 魂晶/次，最多 6 槽）\n/鬼域 开局 七中学校 [单人|团队] [普通|困难] — 私聊默认单人；群聊可选处理方式\n/鬼域 前进 — 沿学校路线探索并触发配置遭遇\n/鬼域 调查 — 发现当前地点线索（团队共享）\n/鬼域 交谈 <人物名>、选择 <人物ID> <选项ID> — 推进分支剧情\n/鬼域 图鉴 [驭鬼名|规律ID] — 查看已发现规律及其线索/反制详情\n/鬼域 编队 [加入|移除 <驭鬼ID>] — 查看或调整下一局出战驭鬼\n/鬼域 商店、购买 槽位 — 查看或扩展驭鬼槽位（6 魂晶/次，最多 6 槽）\n/鬼域 状态、背包 — 查看档案和本局状态\n/鬼域 观察 [鬼名] — 调查敌鬼规律\n/鬼域 指挥 1 攻击 [鬼名] — 命令当前行动鬼攻击\n/鬼域 指挥 1 技能 [技能名] [鬼名] — 命令使用技能\n/鬼域 指挥 1 护送后撤 — 拉开与病鬼的距离；护主、跳过也可直接下令\n/鬼域 使用 <道具> [目标]、捕获 <鬼名> [坟土|棺材钉]\n/鬼域 撤退 — 战斗中尝试撤离；到达操场出口后用 `/鬼域 撤离`\n/鬼域 加入 [邀请码]、出战 <已拥有鬼名>（可多选；`出战 移除 <鬼名>`移除）、准备、开始任务 — 团队大厅\n/鬼域 离队、放弃 — 需要二次确认\n团队探索按队员轮换；每名队员只指挥自己带入的鬼，个人状态与补给独立，敌情与线索共享。普通难度通关解锁困难；剧情选择与规律图鉴会写入本局或个人档案。')

    def recruit(self, identity: Identity, _argument: str='') -> GameResponse:
        result = self.solo_runs.recruit(identity)
        buttons = ()
        if result.ok and self._supports_callback_buttons(identity) and not self.repository.get_active_run(identity):
            domain = self.registry.find_domain('七中学校')
            if domain and 'solo' in domain.data.get('supported_modes', []):
                buttons = (self.solo_runs.mode_button(identity, domain.data, domain.pack_id, domain.version, 'normal'),)
        if buttons:
            return GameResponse(f'✅ {result.message}', buttons)
        lines = [f'✅ {result.message}']
        if identity.group_id:
            lines.append('➡️ 下一步建议：发送 `/鬼域 委托` 查看难度，再选择单人或团队处理。')
            if not self._supports_callback_buttons(identity):
                lines.append('纯文字开局：`/鬼域 开局 七中学校 单人 普通`。')
        else:
            lines.append('➡️ 下一步建议：私聊发送 `/鬼域 委托`，查看并开始单人委托。')
            if not self._supports_callback_buttons(identity):
                lines.append('也可直接发送 `/鬼域 开局 七中学校` 开始普通单人副本。')
        return GameResponse('\n'.join(lines))

    def open_run(self, identity: Identity, request: str='七中学校') -> GameResponse:
        active = self.repository.get_active_run(identity)
        if active:
            if active['adapter_id'] != identity.adapter_id or active['group_id'] != identity.group_id:
                return GameResponse(f'你已有进行中的副本，请回发起群继续：群号 {active['group_id']}。')
            return self._run_status(identity, active)
        profile = self.repository.get_profile(identity)
        if not profile or not profile['payload'].get('owned_ghost_ids'):
            return GameResponse('请先发送 `/鬼域 开始` 接受总部招募并获得初始驭鬼。')
        parts = request.split()
        difficulty_names = {'普通': 'normal', 'normal': 'normal', '困难': 'hard', 'hard': 'hard'}
        mode_names = {'单人': 'solo', 'solo': 'solo', '团队': 'party', 'party': 'party'}
        requested_difficulty = None
        requested_mode = None
        for _ in range(2):
            if not parts:
                break
            final = parts[-1].casefold()
            if requested_difficulty is None and final in difficulty_names:
                requested_difficulty = difficulty_names[final]
            elif requested_mode is None and final in mode_names:
                requested_mode = mode_names[final]
            else:
                break
            parts.pop()
        domain_name = ' '.join(parts) or '七中学校'
        domain = self.registry.find_domain(domain_name)
        if not domain:
            return GameResponse(f'暂未找到副本 `{domain_name}`。发送 `/鬼域 委托` 查看可用委托。')
        if not domain_available(profile['payload'], domain.data):
            required = [self._domain_name(item) for item in unlock_requirements(domain.data)]
            return GameResponse(f'`{domain.data.get('name', domain_name)}` 尚未解锁。请先通关：{'、'.join(required) or '前置章节'}。发送 `/鬼域 章节` 查看进度。')
        levels = domain.data.get('difficulty_levels', {'normal': {'unlocked_by_default': True}})
        selected_levels = [requested_difficulty] if requested_difficulty else list(levels)
        unlocked = [level for level in selected_levels if level in levels if difficulty_is_unlocked(profile['payload'], domain.data, level)]
        if requested_difficulty and requested_difficulty not in unlocked:
            return GameResponse(f'`{requested_difficulty}` 难度尚未解锁。发送 `/鬼域 委托` 查看解锁进度。')
        if not identity.group_id:
            if requested_mode == 'party':
                return GameResponse('团队副本需要在 QQ 群中开局；私聊仅支持单人副本。')
        if not identity.group_id and requested_mode is None and not self._supports_callback_buttons(identity):
            requested_mode = 'solo'
        if requested_mode:
            if requested_mode == 'solo' and 'solo' in domain.data.get('supported_modes', []):
                run_service, action = self.solo_runs, 'select_solo'
            elif requested_mode == 'party' and 'party' in domain.data.get('supported_modes', []):
                run_service, action = self.party_runs, 'create_party'
            else:
                return GameResponse('当前副本不支持所选处理方式或难度。')
            action_id = self.repository.issue_action(identity, action, {'domain_id': domain.data['id'], 'pack_id': domain.pack_id, 'pack_version': domain.version, 'difficulty': unlocked[0]})
            result = run_service.create(identity, action_id)
            if not result.ok:
                return GameResponse(result.message)
            if requested_mode == 'solo':
                return self.solo_runs.response_after_action(identity, result.message)
            return GameResponse(result.message)
        can_use_buttons = self._supports_callback_buttons(identity)
        buttons = []
        if can_use_buttons:
            for level in unlocked:
                if 'solo' in domain.data.get('supported_modes', []):
                    buttons.append(self.solo_runs.mode_button(identity, domain.data, domain.pack_id, domain.version, level))
                if not identity.group_id:
                    continue
                if 'party' in domain.data.get('supported_modes', []):
                    buttons.append(self.party_runs.mode_button(identity, domain.data, domain.pack_id, domain.version, level))
        if not buttons:
            if can_use_buttons:
                return GameResponse('当前没有可用处理模式或已解锁的委托难度。')
        prompt = '选择七中学校委托的处理方式与难度。'
        if buttons:
            return GameResponse(prompt, tuple(buttons[:5]))
        fallback = '文字开局：`/鬼域 开局 七中学校 单人 普通`；群聊团队：`/鬼域 开局 七中学校 团队 普通`。'
        return GameResponse(prompt + '\n' + fallback)

    def contracts(self, identity: Identity, _argument: str='') -> GameResponse:
        profile = self.repository.get_profile(identity)
        if not profile:
            return GameResponse('尚未建立总部档案。先发送 `/鬼域 开始` 招募初始驭鬼。')
        domain = self.registry.find_domain('七中学校')
        if not domain:
            return GameResponse('总部当前没有可用委托。')
        resources = profile['payload'].get('campaign_resources', {})
        lines = ['📋 总部委托｜七中学校', '━━━━━━━━━━━━', f'💎 魂晶：{int(resources.get('soul_shards', 0))}', '']
        buttons = []
        for level in domain.data.get('difficulty_levels', {}):
            unlocked = difficulty_is_unlocked(profile['payload'], domain.data, level)
            label = '普通' if level == 'normal' else '困难' if level == 'hard' else level
            marker = '✅' if unlocked else '🔒'
            status = '已解锁' if unlocked else '未解锁'
            description = '普通调查' if level == 'normal' else '新增走廊鬼婴、敲门鬼两次敲门强攻，并减少一份补给'
            lines.extend((f'【{marker} {label}委托｜{status}】', description, ''))
            if not unlocked:
                continue
            if not self._supports_callback_buttons(identity):
                continue
            if 'solo' in domain.data.get('supported_modes', []):
                buttons.append(self.solo_runs.mode_button(identity, domain.data, domain.pack_id, domain.version, level))
            if not identity.group_id:
                continue
            if 'party' not in domain.data.get('supported_modes', []):
                continue
            buttons.append(self.party_runs.mode_button(identity, domain.data, domain.pack_id, domain.version, level))
            continue
        if buttons:
            return GameResponse('\n'.join(lines), tuple(buttons[:5]))
        lines.append('➡️ 建议下一步')
        if not identity.group_id:
            lines.append('开始单人委托。')
            if self._supports_callback_buttons(identity):
                lines.append('点击下方难度按钮，指令会填入输入框；请检查后手动发送。')
            else:
                lines.append('发送 `/鬼域 开局 七中学校` 开始普通单人副本；解锁困难后可发送 `/鬼域 开局 七中学校 困难`。')
        elif self._supports_callback_buttons(identity):
            lines.append('点击下方单人或团队按钮，指令会填入输入框；请检查后手动发送。')
            lines.append('备用指令：`/鬼域 开局 七中学校 单人 普通` 或 `/鬼域 开局 七中学校 团队 普通`。')
        else:
            lines.append('发送 `/鬼域 开局 七中学校 单人 普通`；组队可发送 `/鬼域 开局 七中学校 团队 普通`。')
        return GameResponse('\n'.join(lines), tuple(buttons[:5]))

    def shop(self, identity: Identity, _argument: str='') -> GameResponse:
        profile = self.repository.get_profile(identity)
        if not profile:
            return GameResponse('尚未建立总部档案。先发送 `/鬼域 开始`。')
        payload = profile['payload']
        resources = payload.get('campaign_resources', {})
        shards = int(resources.get('soul_shards', 0))
        slots = max(1, int(payload.get('max_ghost_slots', 2)))
        lines = ['🏪 总部商店', '━━━━━━━━━━━━', f'💎 魂晶：{shards}', f'驭鬼槽位扩展：6 魂晶/次（当前 {slots}/6）']
        lines.append('魂晶获取：符合结算资格时，每章节普通/困难首通各一次；部分剧情选择另有奖励。重复通关不重复领取。')
        if slots >= 6:
            lines.append('槽位已达上限。')
            return GameResponse('\n'.join(lines))
        if shards < 6:
            lines.append('魂晶不足；积累到 6 枚即可购买。')
            return GameResponse('\n'.join(lines))
        button = ButtonSpec('扩展驭鬼槽位', 'prompt:shop:ghost-slot', input_command='/鬼域 购买 槽位')
        if self._supports_callback_buttons(identity):
            return GameResponse('\n'.join(lines), (button,))
        lines.append('发送 `/鬼域 购买 槽位` 扩展一个出战槽位。')
        return GameResponse('\n'.join(lines))

    def purchase(self, identity: Identity, item: str='') -> GameResponse:
        if not identity.message_id:
            return GameResponse('当前适配器未提供稳定消息编号，暂不能安全处理购买；请稍后重试。')
        if item.strip().casefold() not in {'槽位', 'ghost_slot'}:
            return GameResponse('当前可购买：`/鬼域 购买 槽位`（6 魂晶，驭鬼槽位上限为 6）。')
        message_key = identity.message_id
        action_id = hashlib.sha256(
            f'shop:{identity.adapter_id}:{identity.user_id}:{identity.group_id}:{message_key}:ghost-slot'.encode()
        ).hexdigest()
        self.repository.issue_action(identity, 'shop_purchase', {'item_id': 'ghost_slot'}, action_id=action_id)

        def purchase_in(connection, _payload):
            if self.repository.active_run_in(connection, identity):
                return ActionResult(False, '副本进行中不能购买或调整驭鬼槽位。')
            profile = self.repository.profile_in(connection, identity)
            if not profile:
                return ActionResult(False, '尚未建立总部档案。先发送 `/鬼域 开始`。')
            payload = dict(profile['payload'])
            slots = max(1, int(payload.get('max_ghost_slots', 2)))
            if slots >= 6:
                return ActionResult(False, '驭鬼槽位已达上限（6）。')
            resources = dict(payload.get('campaign_resources', {}))
            shards = int(resources.get('soul_shards', 0))
            if shards < 6:
                return ActionResult(False, f'魂晶不足：需要 6，目前只有 {shards}。')
            resources['soul_shards'] = shards - 6
            payload['campaign_resources'] = resources
            payload['max_ghost_slots'] = slots + 1
            self.repository.save_profile_payload_in(connection, profile['profile_id'], payload)
            return ActionResult(True, f'购买成功：驭鬼槽位提升至 {slots + 1}/6，剩余魂晶 {shards - 6}。')

        result = self.repository.perform_action(identity, action_id, 'shop_purchase', purchase_in)
        suffix = '（本次请求已处理，未重复扣费。）' if result.replayed else ''
        if result.ok and self._supports_callback_buttons(identity):
            button = ButtonSpec('调整出战编队', 'prompt:formation:view', input_command='/鬼域 编队')
            return GameResponse(result.message + suffix, (button,))
        if result.ok:
            return GameResponse(result.message + suffix + '\n下一步：发送 `/鬼域 编队` 调整出战驭鬼。')
        return GameResponse(result.message + suffix)

    def chapters(self, identity: Identity, _argument: str='') -> GameResponse:
        profile = self.repository.get_profile(identity)
        if not profile:
            return GameResponse('尚未建立总部档案。先发送 `/鬼域 开始` 接受招募。')
        payload = profile['payload']
        completed = payload.get('completed_domains', [])
        domains = self._latest_domains()
        main = [item for item in domains if not item.get('unlock', {}).get('optional')]
        side = [item for item in domains if item.get('unlock', {}).get('optional')]
        lines = ['📖 主线章节', '━━━━━━━━━━━━']
        for index, domain in enumerate(sorted(main, key=(lambda item: int(item.get('chapter', 999)))), start=1):
            domain_id = str(domain.get('id', ''))
            if domain_id in completed:
                marker = '✅'
            elif domain_available(payload, domain):
                marker = '🔓'
            else:
                marker = '🔒'
            lines.append(f'{marker} {index}. {domain.get('name', domain_id)}')
            if marker == '🔒':
                required = [self._domain_name(item) for item in unlock_requirements(domain)]
                lines.append(f'　需通关：{'、'.join(required) or '前置章节'}')
        if side:
            lines.extend(['', '🗂 可选副本（势力与组织，随时可进）', '━━━━━━━━━━━━'])
            lines.extend((f'· {domain.get('name', domain.get('id'))}' for domain in side))
        lines.extend(['', f'当前代号：{rank_for(payload)}｜已通关 {len(completed)}/{len(main)}', '➡️ 下一步：`/鬼域 开局 <章节名> 单人 普通`，或 `/鬼域 存档` 查看档案。'])
        return GameResponse('\n'.join(lines))

    def save(self, identity: Identity, _argument: str='') -> GameResponse:
        profile = self.repository.get_profile(identity)
        if not profile:
            return GameResponse('尚未建立总部档案。先发送 `/鬼域 开始` 接受招募。')
        payload = profile['payload']
        resources = payload.get('campaign_resources', {})
        owned = payload.get('owned_ghost_ids', [])
        ghost_names = [self._ghost_name(ghost_id) for ghost_id in owned]
        active = payload.get('active_ghost_ids')
        if not isinstance(active, list) or not active:
            active = [str(payload.get('starter_ghost_id', owned[0]))] if owned else []
        active_names = [self._ghost_name(ghost_id) for ghost_id in active]
        max_slots = max(1, min(6, int(payload.get('max_ghost_slots', 2))))
        domains = [item for item in self._latest_domains() if not item.get('unlock', {}).get('optional')]
        completed = payload.get('completed_domains', [])
        lines = ['🗂 个人存档', '━━━━━━━━━━━━', f'档案号：{profile['profile_id'][:8]}（{profile['adapter_name']}）', f'代号评定：{rank_for(payload)}', f'魂晶：{int(resources.get('soul_shards', 0))}', f'永久驭鬼：{'、'.join(ghost_names) or '无'}', f'当前编队（{len(active)}/{max_slots}）：{'、'.join(active_names) or '无'}', f'规律图鉴：{len(payload.get('discovered_rules', []))} 条', f'已通关：{len(completed)}/{len(domains)}']
        started = float(payload.get('lifespan_started_at', profile.get('created_at', time.time())))
        total = int(payload.get('lifespan_days', 90))
        remaining = max(0, int(total - max(0.0, (time.time() - started) / 86400)))
        lines.append(f'剩余寿命：{lifespan_label(remaining)}（上限 {lifespan_label(total)}）')
        lines.append(f'今日行动：{daily_summary(payload, len(owned), False)}')
        abilities = payload.get('unlocked_abilities', [])
        if abilities:
            names = [LEGACY_NAMES.get(str(item), str(item)) for item in abilities]
            lines.append(f'七老传承：{'、'.join(names)}')
        ending = payload.get('ending')
        if isinstance(ending, dict):
            lines.append(f'结局：{ending.get('title', '结局')}（已达成）')
        lines.extend(['', f'数据库：{self.repository.database_path}', '存档为本地 SQLite，随时可用 `/鬼域 状态` 恢复查看。'])
        return GameResponse('\n'.join(lines))

    def _latest_domains(self) -> list[dict[str, Any]]:
        latest = {}
        for entry in self.registry.entries.values():
            if entry.content_type != 'domains':
                continue
            domain_id = str(entry.data.get('id', ''))
            key = tuple((int(part) for part in entry.version.lstrip('v').split('.') if part.isdigit())) or (0,)
            current = latest.get(domain_id)
            if current is None or key > current[0]:
                latest[domain_id] = key, entry.data
        return [data for _key, data in latest.values()]

    def _domain_name(self, domain_id: str) -> str:
        for domain in self._latest_domains():
            if str(domain.get('id', '')) == str(domain_id):
                return str(domain.get('name', domain_id))
        return str(domain_id)

    def _ghost_name(self, ghost_id: str) -> str:
        for entry in self.registry.entries.values():
            if entry.content_type != 'ghosts':
                continue
            if entry.data.get('id') == ghost_id:
                return str(entry.data.get('name', ghost_id))
        return str(ghost_id)

    def status(self, identity: Identity, _argument: str='') -> GameResponse:
        profile = self.repository.get_profile(identity)
        if not profile:
            return GameResponse('尚未建立总部档案。发送 `/鬼域 开始` 开始招募。')
        run = self.repository.get_active_run(identity)
        if run:
            if run['adapter_id'] != identity.adapter_id or run['group_id'] != identity.group_id:
                return GameResponse(f'你在其他群有进行中的副本：群号 {run['group_id']}。请回原群继续。')
            return self._run_status(identity, run)
        payload = profile['payload']
        return GameResponse(f'总部个人档案\n永久驭鬼：{len(payload.get('owned_ghost_ids', []))} 只\n已发现规律：{len(payload.get('discovered_rules', []))} 条\n当前没有进行中的副本。')

    def forward(self, identity: Identity, _argument: str='') -> GameResponse:
        return self._adventure_action(identity, {'type': 'forward'})

    def investigate(self, identity: Identity, _argument: str='') -> GameResponse:
        return self._adventure_action(identity, {'type': 'investigate'})

    def talk(self, identity: Identity, character_name: str='') -> GameResponse:
        if not character_name:
            return GameResponse('格式：`/鬼域 交谈 <人物名>`。')
        run = self.repository.get_active_run(identity)
        if not run or run['adapter_id'] != identity.adapter_id or run['group_id'] != identity.group_id:
            return GameResponse('当前群没有你的进行中副本。')
        service = self.party_runs if run['kind'] == 'party' else self.solo_runs
        return service.dialogue_response(identity, character_name)

    def journal(self, identity: Identity, query: str='') -> GameResponse:
        profile = self.repository.get_profile(identity)
        if not profile:
            return GameResponse('尚未建立个人档案。')
        discovered = set(str(rule_id) for rule_id in profile['payload'].get('discovered_rules', []))
        if not discovered:
            return GameResponse('规律图鉴尚为空。')

        needle = query.strip().casefold()
        if needle:
            latest: dict[tuple[str, str], tuple[tuple[int, ...], Any, dict[str, Any]]] = {}
            for entry in self.registry.entries.values():
                if entry.content_type != 'ghosts':
                    continue
                ghost_id = str(entry.data.get('id', ''))
                ghost_name = str(entry.data.get('name', ghost_id))
                ghost_matches = needle in {ghost_id.casefold(), ghost_name.casefold()}
                for rule in entry.data.get('rules', []):
                    rule_id = str(rule.get('id', ''))
                    if rule_id not in discovered or not (ghost_matches or needle == rule_id.casefold()):
                        continue
                    key = (ghost_id, rule_id)
                    version_key = tuple(int(part) for part in entry.version.lstrip('v').split('.') if part.isdigit()) or (0,)
                    current = latest.get(key)
                    if current is None or version_key > current[0]:
                        latest[key] = (version_key, entry, rule)
            if not latest:
                return GameResponse('没有找到与该名称匹配的已发现规律。请发送 `/鬼域 图鉴` 查看已掌握条目。')
            lines = [f'📖 图鉴详情｜{query.strip()}']
            for _version, entry, rule in latest.values():
                lines.append(f'【{entry.data.get("name", entry.data.get("id", "驭鬼"))}】{rule.get("description", rule.get("id", "规律"))}')
                lines.extend(f'线索：{clue}' for clue in rule.get('clues', []))
                counter_method = str(rule.get('counter_method', '')).strip()
                if counter_method:
                    lines.append(f'反制：{counter_method}')
            return GameResponse('\n'.join(lines))

        names = []
        for rule_id in sorted(discovered):
            name = rule_id
            for entry in self.registry.entries.values():
                if entry.content_type != 'ghosts':
                    continue
                rule = next((item for item in entry.data.get('rules', []) if item.get('id') == rule_id), None)
                if not rule:
                    continue
                name = f'{entry.data["name"]}：{rule.get("description", rule_id)}'
                break
            names.append(f'- {name}')
        return GameResponse('永久规律图鉴：\n' + '\n'.join(names) + '\n发送 `/鬼域 图鉴 <驭鬼名或规律ID>` 查看线索与反制详情。')

    def choose(self, identity: Identity, argument: str='') -> GameResponse:
        parts = argument.split(maxsplit=1)
        if len(parts) != 2:
            return GameResponse('格式：`/鬼域 选择 <人物ID> <选项ID>`。也可以使用交谈按钮。')
        return self._adventure_action(identity, {'type': 'story_choice', 'character_id': parts[0], 'choice_id': parts[1]})

    def observe(self, identity: Identity, target: str='') -> GameResponse:
        return self._battle_action(identity, {'type': 'observe', 'target': target})

    def command_ghost(self, identity: Identity, argument: str='') -> GameResponse:
        parts = argument.split(maxsplit=3)
        if len(parts) < 2:
            return GameResponse('格式：`/鬼域 指挥 <槽位> <攻击|技能|护主|护送后撤|跳过> [目标或技能名]`。')
        try:
            slot = int(parts[0])
        except ValueError:
            return GameResponse('驭鬼槽位请填写数字，例如 `/鬼域 指挥 1 攻击 敲门鬼`。')
        verb = parts[1]
        rest = parts[2:] if len(parts) > 2 else []
        action = {'slot': slot}
        if verb in {'attack', '攻击'}:
            action.update(type='attack', target=' '.join(rest))
        elif verb in {'skill', '技能'}:
            action.update(type='skill', skill_id=rest[0] if rest else '')
            if len(rest) > 1:
                action['target'] = ' '.join(rest[1:])
        elif verb in {'guard', '护主'}:
            action['type'] = 'guard'
        elif verb in {'escort', '后撤', '护送后撤'}:
            action['type'] = 'escort_retreat'
        elif verb in {'skip', '跳过', '防守'}:
            action['type'] = 'skip'
        else:
            return GameResponse('未知驭鬼指令；可用：攻击、技能、护主、护送后撤、跳过。')
        return self._battle_action(identity, action)

    def use_item(self, identity: Identity, argument: str='') -> GameResponse:
        parts = argument.split(maxsplit=1)
        if not parts:
            return GameResponse('格式：`/鬼域 使用 <道具名> [玩家|鬼名]`。')
        action = {'type': 'item', 'item_id': parts[0]}
        if len(parts) > 1:
            action['target'] = parts[1]
        return self._battle_action(identity, action)

    def capture(self, identity: Identity, argument: str='') -> GameResponse:
        parts = argument.split(maxsplit=1)
        if not parts:
            return GameResponse('格式：`/鬼域 捕获 <鬼名> [坟土|棺材钉]`。')
        action = {'type': 'capture', 'target': parts[0]}
        if len(parts) > 1:
            action['item_id'] = parts[1]
        return self._battle_action(identity, action)

    def deadlock(self, identity: Identity, target: str='') -> GameResponse:
        if not target:
            return GameResponse('格式：`/鬼域 死机 <已驾驭的鬼名>`；需要该鬼复苏 ≥70% 且存在冲突对象。')
        return self._adventure_action(identity, {'type': 'deadlock', 'target': target})

    def guard(self, identity: Identity, _argument: str='') -> GameResponse:
        return self._battle_action(identity, {'type': 'guard'})

    def skip(self, identity: Identity, _argument: str='') -> GameResponse:
        return self._battle_action(identity, {'type': 'skip'})

    def retreat(self, identity: Identity, _argument: str='') -> GameResponse:
        return self._adventure_action(identity, {'type': 'retreat'})

    def extract(self, identity: Identity, _argument: str='') -> GameResponse:
        return self._adventure_action(identity, {'type': 'extract'})

    def inventory(self, identity: Identity, _argument: str='') -> GameResponse:
        run = self.repository.get_active_run(identity)
        if not run or run['adapter_id'] != identity.adapter_id or run['group_id'] != identity.group_id:
            return GameResponse('当前群没有你的进行中副本。')
        profile = self.repository.get_profile(identity)
        player = run['state'].get('players', {}).get(profile['profile_id'], {}) if profile else {}
        inventory = player.get('inventory', {})
        if not inventory:
            return GameResponse('本局背包为空。')
        lines = []
        for item_id, count in inventory.items():
            entry = self.registry.get('items', item_id, run['pack_id'], run['pack_version'])
            lines.append(f'{entry.data['name'] if entry else item_id} ×{count}')
        return GameResponse('本局背包：\n' + '\n'.join(lines))

    def formation(self, identity: Identity, argument: str='') -> GameResponse:
        profile = self.repository.get_profile(identity)
        if not profile:
            return GameResponse('尚未建立总部档案。先发送 `/鬼域 开始` 招募初始驭鬼。')
        run = self.repository.get_active_run(identity)
        if run and run['adapter_id'] == identity.adapter_id and run['group_id'] == identity.group_id:
            return GameResponse('副本进行中不能修改编队；结束本局后可调整下一局出战驭鬼。')

        profile_payload = profile['payload']
        owned_ids = [str(ghost_id) for ghost_id in profile_payload.get('owned_ghost_ids', [])]
        if not owned_ids:
            return GameResponse('档案中还没有可编入队伍的驭鬼。')
        fallback_pack = str(profile_payload.get('starter_pack_id', ''))
        fallback_version = str(profile_payload.get('starter_pack_version', ''))
        starter_id = str(profile_payload.get('starter_ghost_id', owned_ids[0]))
        active = profile_payload.get('active_ghost_ids')
        if not isinstance(active, list) or not active:
            active = [starter_id if starter_id in owned_ids else owned_ids[0]]
        active = [str(ghost_id) for ghost_id in active]
        max_slots = max(1, min(6, int(profile_payload.get('max_ghost_slots', 2))))

        parts = argument.split(maxsplit=1)
        if parts:
            operation = parts[0]
            needle = parts[1].strip().casefold() if len(parts) > 1 else ''
            if operation not in {'加入', '移除'} or not needle:
                return GameResponse('格式：`/鬼域 编队 加入 <驭鬼ID>` 或 `/鬼域 编队 移除 <驭鬼ID>`。')
            selected_entry = None
            for ghost_id in owned_ids:
                entry = resolve_owned_ghost_entry(self.registry, profile_payload, ghost_id, fallback_pack, fallback_version)
                if entry and (ghost_id.casefold() == needle or str(entry.data.get('name', '')).casefold() == needle):
                    selected_entry = entry
                    break
            if selected_entry is None:
                return GameResponse('只能编入档案中已有的驭鬼；发送 `/鬼域 编队` 查看可用名单。')
            ghost_id = str(selected_entry.data['id'])
            with self.repository.transaction() as connection:
                if self.repository.active_run_in(connection, identity):
                    return GameResponse('副本进行中不能修改编队；结束本局后再调整。')
                current = self.repository.profile_in(connection, identity)
                if not current:
                    return GameResponse('个人档案不存在。')
                payload = dict(current['payload'])
                current_owned = [str(item) for item in payload.get('owned_ghost_ids', [])]
                if ghost_id not in current_owned:
                    return GameResponse('该驭鬼已不在你的档案中；请重新查看编队。')
                roster = payload.get('active_ghost_ids')
                if not isinstance(roster, list) or not roster:
                    current_starter = str(payload.get('starter_ghost_id', current_owned[0]))
                    roster = [current_starter if current_starter in current_owned else current_owned[0]]
                roster = [str(item) for item in roster]
                slots = max(1, min(6, int(payload.get('max_ghost_slots', 2))))
                if operation == '加入':
                    if ghost_id in roster:
                        return GameResponse(f'【{selected_entry.data["name"]}】已在当前编队中。')
                    if len(roster) >= slots:
                        return GameResponse(f'编队槽位已满（{len(roster)}/{slots}）；先提升槽位或移除一只驭鬼。')
                    roster.append(ghost_id)
                else:
                    if ghost_id not in roster:
                        return GameResponse(f'【{selected_entry.data["name"]}】不在当前编队中。')
                    if len(roster) <= 1:
                        return GameResponse('至少保留一只出战驭鬼。')
                    roster.remove(ghost_id)
                payload['active_ghost_ids'] = roster
                refs = payload.get('owned_ghost_refs')
                if not isinstance(refs, dict):
                    refs = {}
                refs[ghost_id] = {'pack_id': selected_entry.pack_id, 'pack_version': selected_entry.version}
                payload['owned_ghost_refs'] = refs
                self.repository.save_profile_payload_in(connection, current['profile_id'], payload)
            update = f'编队已更新：{operation}【{selected_entry.data["name"]}】。当前 {len(roster)}/{slots} 个槽位。'
            view = self.formation(identity)
            return GameResponse(update + '\n' + view.text, view.buttons)

        names = []
        buttons = []
        for ghost_id in owned_ids:
            entry = resolve_owned_ghost_entry(self.registry, profile_payload, ghost_id, fallback_pack, fallback_version)
            name = str(entry.data.get('name', ghost_id)) if entry else ghost_id
            names.append(f'{"✅" if ghost_id in active else "▫️"} {name}')
            if entry and self._supports_callback_buttons(identity):
                operation = '移除' if ghost_id in active else '加入'
                label = f'{"撤下" if operation == "移除" else "上阵"}·{name}'[:16]
                buttons.append(ButtonSpec(label, f'prompt:formation:{operation}:{ghost_id}', input_command=f'/鬼域 编队 {operation} {ghost_id}'))
        text = f'🧩 出战编队（槽位 {len(active)}/{max_slots}）\n' + '\n'.join(names)
        if not self._supports_callback_buttons(identity):
            text += '\n可发送 `/鬼域 编队 加入 <鬼ID>` 或 `/鬼域 编队 移除 <鬼ID>` 调整。'
        return GameResponse(text, tuple(buttons[:5]))

    def _adventure_action(self, identity: Identity, action: dict[str, Any]) -> GameResponse:
        run = self.repository.get_active_run(identity)
        if run and run['kind'] == 'party':
            return self.party_runs.perform_text_action(identity, action)
        return self.solo_runs.perform_text_action(identity, action)

    def _battle_action(self, identity: Identity, action: dict[str, Any]) -> GameResponse:
        return self._adventure_action(identity, action)

    def join(self, identity: Identity, invite_code: str='') -> GameResponse:
        return GameResponse(self.party_runs.join(identity, invite_code).message)

    def select_ghost(self, identity: Identity, ghost_name: str='') -> GameResponse:
        if not ghost_name:
            return GameResponse('格式：`/鬼域 出战 <个人档案中已拥有的鬼名>`。')
        result = self.party_runs.select_ghost(identity, ghost_name)
        return GameResponse(result.message)

    def ready(self, identity: Identity, _argument: str='') -> GameResponse:
        return GameResponse(self.party_runs.ready(identity).message)

    def start_party(self, identity: Identity, _argument: str='') -> GameResponse:
        result = self.party_runs.start(identity)
        if result.ok:
            return self.party_runs.response_after_action(identity, result.message)
        return GameResponse(result.message)

    def leave(self, identity: Identity, _argument: str='') -> GameResponse:
        run = self.repository.get_active_run(identity)
        if not run:
            return GameResponse('你当前没有进行中的副本。')
        if run['adapter_id'] != identity.adapter_id or run['group_id'] != identity.group_id:
            return GameResponse(f'请回副本发起群操作：群号 {run['group_id']}。')
        if run['kind'] == 'solo':
            return GameResponse('单人副本请使用 `/鬼域 撤退` 或 `/鬼域 放弃`。')
        if run['status'] != 'lobby':
            return GameResponse('团队副本开始后不能离队。')
        profile = self.repository.get_profile(identity)
        if profile and profile['profile_id'] == run['leader_profile_id']:
            return GameResponse('队长不能单独离队；如需结束大厅，请使用 `/鬼域 放弃`。')
        return GameResponse('确认离开待组队大厅？', (self.party_runs.confirmation_button(identity, run),))

    def abandon(self, identity: Identity, _argument: str='') -> GameResponse:
        run = self.repository.get_active_run(identity)
        if not run:
            return GameResponse('你当前没有进行中的副本。')
        if run['adapter_id'] != identity.adapter_id or run['group_id'] != identity.group_id:
            return GameResponse(f'请回副本发起群操作：群号 {run['group_id']}。')
        if run['kind'] == 'solo':
            button = self.solo_runs.abandon_button(identity, run)
        else:
            if run['status'] != 'lobby':
                return GameResponse('团队副本开始后不能解散放弃。')
            profile = self.repository.get_profile(identity)
            if not profile or profile['profile_id'] != run['leader_profile_id']:
                return GameResponse('只有队长可以解散团队大厅；如需离开，请使用 `/鬼域 离队`。')
            button = self.party_runs.confirmation_button(identity, run)
        return GameResponse('放弃副本会结束当前 Run；个人档案不会删除。确认继续？', (button,))

    def _run_status(self, identity: Identity, run: dict[str, Any]) -> GameResponse:
        if run['kind'] == 'solo':
            state = run['state']
            if state.get('phase') == 'battle':
                text = '已恢复战斗。' + self.solo_runs._battle_status_text(run)
                return self.solo_runs.response_after_action(identity, text)
            route = state.get('route', {})
            domain = self.registry.get('domains', run['domain_id'], run['pack_id'], run['pack_version'])
            nodes = domain.data.get('nodes', []) if domain else []
            index = int(route.get('node_index', 0))
            location = nodes[index].get('name', '未知地点') if 0 <= index < len(nodes) else '未知地点'
            if state.get('phase') == 'extraction':
                return self.solo_runs.response_after_action(identity, f'你已到达【{location}】，可以安全撤离。')
            return GameResponse(f'七中学校单人副本（{run['invite_code']}）\n地点：{location}\n发送 `/鬼域 前进` 继续调查，或发送 `/鬼域 背包` 查看补给。')
        if run['status'] == 'active':
            return self.party_runs.status_response(identity)
        state_label = '待组队'
        names = '、'.join((member['user_id'] for member in run['members']))
        return GameResponse(f'七中学校团队副本（{state_label}）\n邀请码：{run['invite_code']}\n队员（{run['member_count']}/4）：{names}\n准备：{run['ready_count']}/{run['member_count']}')


