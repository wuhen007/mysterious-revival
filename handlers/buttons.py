from __future__ import annotations
from ..engine.models import (ActionResult, GameResponse, Identity)
from ..modes.party_run import PartyRunService
from ..modes.solo_run import SoloRunService
from ..storage.repository import GameRepository

class ButtonHandler:
    """Resolve callback IDs, then rely on the repository's atomic scope checks."""

    _PROMPT_COMMANDS = {'prompt:solo:normal': '/鬼域 开局 七中学校 单人 普通', 'prompt:solo:hard': '/鬼域 开局 七中学校 单人 困难', 'prompt:party:normal': '/鬼域 开局 七中学校 团队 普通', 'prompt:party:hard': '/鬼域 开局 七中学校 团队 困难', 'prompt:solo:forward': '/鬼域 前进', 'prompt:solo:inventory': '/鬼域 背包'}
    def __init__(self, repository: GameRepository, solo_runs: SoloRunService, party_runs: PartyRunService):
        self.repository = repository
        self.solo_runs = solo_runs
        self.party_runs = party_runs

    def dispatch(self, identity: Identity, action_id: str) -> GameResponse:
        command = self._PROMPT_COMMANDS.get(action_id)
        if command is not None:
            return GameResponse(f'下一步：请手动发送以下指令：\n{command}')
        pending = self.repository.lookup_action(identity, action_id)
        if isinstance(pending, ActionResult):
            suffix = '（此操作已处理，未重复结算。）' if pending.replayed else ''
            run = self.repository.get_active_run(identity)
            if run and run['kind'] == 'party':
                return self.party_runs.response_after_action(identity, pending.message + suffix)
            return self.solo_runs.response_after_action(identity, pending.message + suffix)
        action = pending['action']
        if action == 'solo_action':
            return self.solo_runs.button_action(identity, action_id)
        if action == 'party_action':
            return self.party_runs.button_action(identity, action_id)
        handlers = {'select_solo': self.solo_runs.create, 'create_party': self.party_runs.create, 'join_party': self.party_runs.join_action, 'ready_party': self.party_runs.mark_ready, 'start_party': self.party_runs.start_action, 'confirm_leave_party': self.party_runs.confirm_leave, 'confirm_abandon_party': self.party_runs.confirm_abandon, 'confirm_abandon_solo': self.solo_runs.confirm_abandon}
        handler = handlers.get(action)
        if handler is None:
            return GameResponse('按钮操作类型无效，请使用文字指令重新开始。')
        result = handler(identity, action_id)
        suffix = '（此操作已处理，未重复结算。）' if result.replayed else ''
        if action == 'start_party' and result.ok:
            return self.party_runs.response_after_action(identity, result.message + suffix)
        return GameResponse(result.message + suffix)


