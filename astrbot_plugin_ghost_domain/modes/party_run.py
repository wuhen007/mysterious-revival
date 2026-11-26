from __future__ import annotations
import hashlib
import json
import sqlite3
import uuid
from collections.abc import Mapping
from typing import Any
from ..engine.battle import BattleEngine
from ..engine.daily import consume as consume_action
from ..engine.campaign import (difficulty_is_unlocked, encounter_refs_for_difficulty, extra_encounters_for_difficulty, settle_profile)
from ..engine.ghost_control import capture_probability, resolve_owned_ghost_entry
from ..engine.models import (ActionResult, ButtonSpec, GameResponse, Identity)
from ..engine.party import (build_party_run_state, complete_exploration_action, current_explorer, eligible_settlement_profiles)
from ..engine.story import (apply_dialogue_choice, dialogue_view, missing_objectives, trigger_location_events)
from ..storage.repository import GameRepository

class PartyRunService:
    def __init__(self, repository: GameRepository, registry: Any):
        self.repository = repository
        self.registry = registry
        self.engine = BattleEngine(registry)

    def mode_button(self, identity: Identity, domain: dict[str, Any], pack_id: str, version: str, difficulty: str='normal') -> ButtonSpec:
        action_id = f'prompt:party:{'hard' if difficulty == 'hard' else 'normal'}'
        label = ('困难' if difficulty == 'hard' else '普通') + '·团队'
        command = f'/鬼域 开局 {domain.get('name', '七中学校')} 团队 {'困难' if difficulty == 'hard' else '普通'}'
        return ButtonSpec(label, action_id, input_command=command)

    def create(self, identity: Identity, action_id: str) -> ActionResult:
        def create_in(connection: sqlite3.Connection, payload: dict[str, Any]) -> ActionResult:
            active = self.repository.active_run_in(connection, identity)
            if active:
                return ActionResult(False, '你已有进行中的副本；请回原群继续或先结束当前副本。', {'run': active})
            profile, _created = self.repository.ensure_profile_in(connection, identity)
            domain = self.registry.get('domains', str(payload['domain_id']), str(payload['pack_id']), str(payload['pack_version']))
            difficulty = str(payload.get('difficulty', 'normal'))
            if not domain or not difficulty_is_unlocked(profile['payload'], domain.data, difficulty):
                return ActionResult(False, f'团队委托难度 `{difficulty}` 尚未解锁或内容不存在。')
            run = self.repository.create_run_in(connection, identity, profile['profile_id'], 'party', payload['domain_id'], payload['pack_id'], payload['pack_version'], 'lobby', {'difficulty': difficulty, 'selected_ghosts': {}})
            return ActionResult(True, f'团队大厅已建立，邀请码 {run['invite_code']}。队员在本群发送 `/鬼域 加入 {run['invite_code']}`，每人准备后由队长开始。', {'run': run})

        return self.repository.perform_action(identity, action_id, 'create_party', create_in)

    def join(self, identity: Identity, invite_code: str='') -> ActionResult:
        if not self.repository.get_profile(identity):
            return ActionResult(False, '请先发送 `/鬼域 开始` 完成总部建档，再加入团队副本。')
        lobbies = self.repository.find_group_lobbies(identity.group_id, identity.adapter_id, invite_code)
        if not lobbies:
            return ActionResult(False, '本群没有匹配的邀请码的待组队大厅。')
        if not invite_code:
            if len(lobbies) > 1:
                codes = ', '.join((run['invite_code'] for run in lobbies))
                return ActionResult(False, f'本群有多个大厅，请指定邀请码：`/鬼域 加入 <邀请码>`（{codes}）。')
        run = lobbies[0]
        action_id = self.repository.issue_action(identity, 'join_party', {'run_id': run['run_id'], 'expected_version': run['version']})
        return self.join_action(identity, action_id)

    def join_action(self, identity: Identity, action_id: str) -> ActionResult:
        def join_in(connection: sqlite3.Connection, payload: dict[str, Any]) -> ActionResult:
            run = self.repository.run_snapshot_in(connection, str(payload.get('run_id', '')))
            profile = self.repository.profile_in(connection, identity)
            if not run or not profile:
                return ActionResult(False, '团队大厅或个人档案不存在。')
            if run['adapter_id'] != identity.adapter_id or run['group_id'] != identity.group_id:
                return ActionResult(False, '只能加入同一机器人适配器、同一群聊中的团队大厅。')
            if run['version'] != payload.get('expected_version') or run['status'] != 'lobby':
                return ActionResult(False, '团队大厅已变化或已开始，请重新查看。')
            if self.repository.active_run_in(connection, identity):
                return ActionResult(False, '你已有进行中的副本，不能同时加入另一局。')
            if run['member_count'] >= 4:
                return ActionResult(False, '团队已满（最多 4 人）。')
            connection.execute("INSERT INTO run_members(run_id, profile_id, adapter_id, user_id, role, is_ready, is_active, joined_at) VALUES (?, ?, ?, ?, 'member', 0, 1, strftime('%s','now'))", (run['run_id'], profile['profile_id'], identity.adapter_id, identity.user_id))
            connection.execute("UPDATE runs SET version = version + 1, updated_at = strftime('%s','now') WHERE run_id = ?", (run['run_id'],))
            snapshot = self.repository.run_snapshot_in(connection, run['run_id'])
            return ActionResult(True, f'已加入团队（{snapshot['member_count']}/4）。准备后发送 `/鬼域 准备`。', {'run': snapshot})

        return self.repository.perform_action(identity, action_id, 'join_party', join_in)

    def ready(self, identity: Identity) -> ActionResult:
        run = self.repository.get_active_run(identity)
        if not run or run['kind'] != 'party':
            return ActionResult(False, '你当前没有待准备的团队大厅。')
        action_id = self.repository.issue_action(identity, 'ready_party', {'run_id': run['run_id'], 'expected_version': run['version']})
        return self.mark_ready(identity, action_id)

    def mark_ready(self, identity: Identity, action_id: str) -> ActionResult:
        def ready_in(connection: sqlite3.Connection, payload: dict[str, Any]) -> ActionResult:
            run = self._member_run(connection, identity, payload)
            if isinstance(run, ActionResult):
                return run
            if run['kind'] != 'party' or run['status'] != 'lobby':
                return ActionResult(False, '只有待组队大厅可以准备。')
            connection.execute('UPDATE run_members SET is_ready = 1 WHERE run_id = ? AND adapter_id = ? AND user_id = ? AND is_active = 1', (run['run_id'], identity.adapter_id, identity.user_id))
            connection.execute("UPDATE runs SET version = version + 1, updated_at = strftime('%s','now') WHERE run_id = ?", (run['run_id'],))
            snapshot = self.repository.run_snapshot_in(connection, run['run_id'])
            return ActionResult(True, f'已准备（{snapshot['ready_count']}/{snapshot['member_count']}）。队长在全员准备后发送 `/鬼域 开始任务`。', {'run': snapshot})

        return self.repository.perform_action(identity, action_id, 'ready_party', ready_in)

    def start(self, identity: Identity) -> ActionResult:
        run = self.repository.get_active_run(identity)
        if not run or run['kind'] != 'party':
            return ActionResult(False, '你当前没有可开始的团队大厅。')
        action_id = self.repository.issue_action(identity, 'start_party', {'run_id': run['run_id'], 'expected_version': run['version']})
        return self.start_action(identity, action_id)

    def start_action(self, identity: Identity, action_id: str) -> ActionResult:
        def start_in(connection: sqlite3.Connection, payload: dict[str, Any]) -> ActionResult:
            run = self._member_run(connection, identity, payload)
            if isinstance(run, ActionResult):
                return run
            profile = self.repository.profile_in(connection, identity)
            if run['kind'] != 'party' or run['status'] != 'lobby':
                return ActionResult(False, '团队大厅已开始或已结束。')
            if not profile or run['leader_profile_id'] != profile['profile_id']:
                return ActionResult(False, '只有队长可以开始团队副本。')
            if not 2 <= run['member_count'] <= 4:
                return ActionResult(False, '团队需要 2–4 名队员才能开始。')
            if run['ready_count'] != run['member_count']:
                return ActionResult(False, '请等待所有队员准备后再开始。')
            rows = connection.execute('SELECT p.* FROM run_members m JOIN profiles p ON p.profile_id = m.profile_id WHERE m.run_id = ? AND m.is_active = 1 ORDER BY m.joined_at, m.user_id', (run['run_id'],)).fetchall()
            profiles = []
            for row in rows:
                member = self.repository._profile_dict(row)
                member['pack_id'] = run['pack_id']
                member['pack_version'] = run['pack_version']
                profiles.append(member)
            domain = self.registry.get('domains', run['domain_id'], run['pack_id'], run['pack_version'])
            if not domain:
                return ActionResult(False, '团队副本内容版本无法读取。')
            difficulty = str(run.get('state', {}).get('difficulty', 'normal'))
            if any((not difficulty_is_unlocked(member['payload'], domain.data, difficulty) for member in profiles)):
                return ActionResult(False, f'所有队员都必须先解锁 `{difficulty}` 难度才能开始。')
            try:
                selections = run.get('state', {}).get('selected_ghosts', {})
                state = build_party_run_state(profiles, domain.data, self.registry, run['leader_profile_id'], selections, difficulty)
            except (KeyError, TypeError, ValueError) as error:
                return ActionResult(False, f'团队副本无法启动：{error}')
            self.repository.save_run_state_in(connection, run['run_id'], run['version'], state, 'active')
            return ActionResult(True, '队员名单已锁定，团队副本启动。共享地点与敌鬼状态，个人 HP、SAN、补给和驭鬼归各自队员。', {'run_id': run['run_id']})

        return self.repository.perform_action(identity, action_id, 'start_party', start_in)

    def select_ghost(self, identity: Identity, ghost_name: str) -> ActionResult:
        run = self.repository.get_active_run(identity)
        profile = self.repository.get_profile(identity)
        if not run or run['kind'] != 'party' or run['status'] != 'lobby' or not profile:
            return ActionResult(False, '只有待组队大厅中的队员可以选择出战驭鬼。')
        if run['adapter_id'] != identity.adapter_id or run['group_id'] != identity.group_id:
            return ActionResult(False, '只能在副本发起群选择出战驭鬼。')
        payload = profile['payload']
        pack_id = run['pack_id']
        version = run['pack_version']
        raw = ghost_name.strip()
        remove = raw.startswith('移除 ')
        needle = raw[3:].strip().casefold() if remove else raw.casefold()
        entry = None
        for owned_id in payload.get('owned_ghost_ids', []):
            candidate = resolve_owned_ghost_entry(self.registry, payload, str(owned_id), pack_id, version)
            if candidate and (str(owned_id).casefold() == needle or str(candidate.data.get('name', '')).casefold() == needle):
                entry = candidate
                break
        if not entry:
            return ActionResult(False, '只能选择个人档案中已拥有且内容版本可用的驭鬼。')
        action_id = self.repository.issue_action(identity, 'select_party_ghost', {'run_id': run['run_id'], 'expected_version': run['version'], 'ghost_id': entry.data['id'], 'remove': remove, 'source_pack_id': entry.pack_id, 'source_pack_version': entry.version})

        def select_in(connection: sqlite3.Connection, action_payload: dict[str, Any]) -> ActionResult:
            current = self.repository.run_snapshot_in(connection, str(action_payload.get('run_id', '')))
            member = self.repository.profile_in(connection, identity)
            if not current or not member or current['kind'] != 'party' or current['status'] != 'lobby':
                return ActionResult(False, '团队大厅已开始或已结束。')
            if current['adapter_id'] != identity.adapter_id or current['group_id'] != identity.group_id:
                return ActionResult(False, '只能在发起群选择出战驭鬼。')
            if current['version'] != action_payload.get('expected_version'):
                return ActionResult(False, '团队大厅状态已变化，请重新选择。')
            active_member = connection.execute('SELECT 1 FROM run_members WHERE run_id = ? AND profile_id = ? AND is_active = 1', (current['run_id'], member['profile_id'])).fetchone()
            if not active_member:
                return ActionResult(False, '你不是当前大厅的有效队员。')
            ghost_id = str(action_payload['ghost_id'])
            if ghost_id not in member['payload'].get('owned_ghost_ids', []):
                return ActionResult(False, '这只鬼不属于你的个人档案。')
            state = current.get('state', {})
            selections = state.setdefault('selected_ghosts', {})
            current_selection = selections.get(member['profile_id'])
            selected = [str(current_selection)] if isinstance(current_selection, str) else [str(item) for item in (current_selection or [])]
            selected = list(dict.fromkeys(selected))
            slots = max(1, min(6, int(member['payload'].get('max_ghost_slots', 2))))
            if action_payload.get('remove'):
                if ghost_id not in selected:
                    return ActionResult(False, '这只鬼不在你的团队出战名单中。')
                if len(selected) <= 1:
                    return ActionResult(False, '团队出战至少保留一只驭鬼。')
                selected.remove(ghost_id)
                message = f'已从团队出战名单移除【{entry.data["name"]}】。'
            else:
                if ghost_id in selected:
                    return ActionResult(False, f'【{entry.data["name"]}】已经在团队出战名单中。')
                if len(selected) >= slots:
                    return ActionResult(False, f'团队编队槽位已满（{len(selected)}/{slots}）。')
                selected.append(ghost_id)
                message = f'已加入团队出战名单【{entry.data["name"]}】（{len(selected)}/{slots}）。'
            selections[member['profile_id']] = selected
            refs = member['payload'].get('owned_ghost_refs')
            if not isinstance(refs, dict):
                refs = {}
            refs.setdefault(ghost_id, {'pack_id': action_payload['source_pack_id'], 'pack_version': action_payload['source_pack_version']})
            member['payload']['owned_ghost_refs'] = refs
            self.repository.save_profile_payload_in(connection, member['profile_id'], member['payload'])
            connection.execute('UPDATE run_members SET is_ready = 0 WHERE run_id = ? AND profile_id = ?', (current['run_id'], member['profile_id']))
            self.repository.save_run_state_in(connection, current['run_id'], current['version'], state)
            return ActionResult(True, message + ' 请重新准备。', {'selected_ghost_ids': selected})

        return self.repository.perform_action(identity, action_id, 'select_party_ghost', select_in)

    def confirmation_button(self, identity: Identity, run: dict[str, Any]) -> ButtonSpec:
        profile = self.repository.get_profile(identity)
        is_leader = bool(profile and run['leader_profile_id'] == profile['profile_id'])
        if is_leader:
            action = 'confirm_abandon_party'
            label = '确认解散并放弃团队副本'
        else:
            action = 'confirm_leave_party'
            label = '确认离开团队'
        action_id = self.repository.issue_action(identity, action, {'run_id': run['run_id'], 'expected_version': run['version']})
        command = '/鬼域 放弃' if is_leader else '/鬼域 离队'
        return ButtonSpec(label, action_id, input_command=command)

    def confirm_leave(self, identity: Identity, action_id: str) -> ActionResult:
        def leave_in(connection: sqlite3.Connection, payload: dict[str, Any]) -> ActionResult:
            run = self._member_run(connection, identity, payload)
            if isinstance(run, ActionResult):
                return run
            profile = self.repository.profile_in(connection, identity)
            if run['kind'] != 'party':
                return ActionResult(False, '这不是团队副本。')
            if run['status'] != 'lobby':
                return ActionResult(False, '团队副本开始后不能离队。')
            if profile and run['leader_profile_id'] == profile['profile_id']:
                return ActionResult(False, '队长不能单独离队；请确认解散团队副本。')
            connection.execute('UPDATE run_members SET is_active = 0 WHERE run_id = ? AND adapter_id = ? AND user_id = ?', (run['run_id'], identity.adapter_id, identity.user_id))
            connection.execute("UPDATE runs SET version = version + 1, updated_at = strftime('%s','now') WHERE run_id = ?", (run['run_id'],))
            remaining = connection.execute('SELECT COUNT(*) FROM run_members WHERE run_id = ? AND is_active = 1', (run['run_id'],)).fetchone()[0]
            if remaining == 0:
                connection.execute("UPDATE runs SET status = 'ended', version = version + 1 WHERE run_id = ?", (run['run_id'],))
            return ActionResult(True, '你已离开团队副本；其他队员的 Run 不受影响。')

        return self.repository.perform_action(identity, action_id, 'confirm_leave_party', leave_in)

    def confirm_abandon(self, identity: Identity, action_id: str) -> ActionResult:
        def abandon_in(connection: sqlite3.Connection, payload: dict[str, Any]) -> ActionResult:
            run = self._member_run(connection, identity, payload)
            if isinstance(run, ActionResult):
                return run
            profile = self.repository.profile_in(connection, identity)
            if run['kind'] != 'party' or not profile or run['leader_profile_id'] != profile['profile_id']:
                return ActionResult(False, '只有团队队长可以解散并放弃该副本。')
            if run['status'] != 'lobby':
                return ActionResult(False, '团队副本开始后不能解散放弃。')
            connection.execute("UPDATE runs SET status = 'abandoned', version = version + 1, updated_at = strftime('%s','now') WHERE run_id = ?", (run['run_id'],))
            connection.execute('UPDATE run_members SET is_active = 0 WHERE run_id = ?', (run['run_id'],))
            return ActionResult(True, '团队副本已解散；所有成员的永久档案保留。')

        return self.repository.perform_action(identity, action_id, 'confirm_abandon_party', abandon_in)

    def issue_confirmation(self, identity: Identity) -> tuple[dict[str, Any], ButtonSpec] | None:
        run = self.repository.get_active_run(identity)
        if not run:
            return
        button = self.confirmation_button(identity, run)
        return run, button

    def _member_run(self, connection: sqlite3.Connection, identity: Identity, payload: dict[str, Any]) -> dict[str, Any] | ActionResult:
        run = self.repository.run_snapshot_in(connection, str(payload.get('run_id', '')))
        if not run:
            return ActionResult(False, '副本不存在。')
        if run['adapter_id'] != identity.adapter_id or run['group_id'] != identity.group_id:
            return ActionResult(False, '只能在发起副本的群聊中操作。')
        if run['version'] != payload.get('expected_version'):
            return ActionResult(False, '副本状态已变化，请重新查看后再操作。')
        profile = self.repository.profile_in(connection, identity)
        if profile:
            if not any((member['profile_id'] == profile['profile_id'] and member['is_active'] for member in run['members'])):
                return ActionResult(False, '你不是当前副本的有效成员。')
        if run['status'] not in {'lobby', 'active'}:
            return ActionResult(False, '副本已经结束。')
        return run

    def perform_text_action(self, identity: Identity, action: dict[str, Any]) -> GameResponse:
        run = self.repository.get_active_run(identity)
        if not run:
            return GameResponse('你当前没有进行中的团队副本。')
        if run['kind'] != 'party':
            return GameResponse('当前不是团队副本。')
        if run['adapter_id'] != identity.adapter_id or run['group_id'] != identity.group_id:
            return GameResponse(f'请回副本发起群继续：群号 {run['group_id']}。')
        action_id = hashlib.sha256(f'{identity.adapter_id}:{identity.user_id}:{identity.group_id}:{identity.message_id or uuid.uuid4().hex}'.encode()).hexdigest()
        self.repository.issue_action(identity, 'party_action', {'run_id': run['run_id'], 'expected_version': run['version'], 'action': action}, action_id=action_id)
        return self.perform_action(identity, action_id)

    def button_action(self, identity: Identity, action_id: str) -> GameResponse:
        return self.perform_action(identity, action_id)

    def perform_action(self, identity: Identity, action_id: str) -> GameResponse:
        result = self.repository.perform_action(identity, action_id, 'party_action', (lambda connection, payload: self._apply_party_action(connection, identity, payload)))
        suffix = '（此操作已处理，未重复结算。）' if result.replayed else ''
        return self.response_after_action(identity, result.message + suffix)

    @staticmethod
    def _captain_id(state: Mapping[str, Any], run: Mapping[str, Any]) -> str:
        '''队长阵亡或失能时，指挥权自动移交给第一位存活队员。'''

        leader_id = str(run.get('leader_profile_id', ''))
        players = state.get('players', {})
        leader = players.get(leader_id, {})
        if bool(leader.get('alive', int(leader.get('hp', 0)) > 0)) and int(leader.get('hp', 0)) > 0:
            return leader_id
        for profile_id, player in players.items():
            if not bool(player.get('alive', int(player.get('hp', 0)) > 0)):
                continue
            if int(player.get('hp', 0)) > 0:
                return str(profile_id)
        return leader_id

    def _apply_party_action(self, connection: sqlite3.Connection, identity: Identity, payload: dict[str, Any]) -> ActionResult:
        run = self.repository.run_snapshot_in(connection, str(payload.get('run_id', '')))
        profile = self.repository.profile_in(connection, identity)
        if not run or not profile:
            return ActionResult(False, '团队副本或个人档案不存在。')
        if run['kind'] != 'party' or run['adapter_id'] != identity.adapter_id or run['group_id'] != identity.group_id:
            return ActionResult(False, '只能在发起副本的群操作。')
        if run['status'] != 'active' or run['version'] != payload.get('expected_version'):
            return ActionResult(False, '团队副本状态已变化或已结束，请重新查看。')
        if not any((member['profile_id'] == profile['profile_id'] and member['is_active'] for member in run['members'])):
            return ActionResult(False, '你不是当前团队副本的有效成员。')
        state = run['state']
        campaign_payload = dict(profile['payload'])
        owned_units = [unit for unit in state.get('ghost_units', []) if unit.get('side') == 'friendly' if unit.get('owner_profile_id') == profile['profile_id'] if not unit.get('rebelled')]
        all_deadlocked = bool(owned_units) and all((unit.get('deadlocked') for unit in owned_units))
        allowed, daily_message = consume_action(campaign_payload, len(owned_units), all_deadlocked)
        if not allowed:
            return ActionResult(False, daily_message)
        self.repository.save_profile_payload_in(connection, profile['profile_id'], campaign_payload)
        player = state.get('players', {}).get(profile['profile_id'])
        if not player:
            return ActionResult(False, '你的个人战斗状态不在当前团队 Run 中。')
        if not player.get('alive', int(player.get('hp', 0)) > 0) or int(player.get('hp', 0)) <= 0:
            return ActionResult(False, '你已死亡，不能继续操作；队伍中的其他成员仍可继续。')
        action = dict(payload.get('action', {}))
        kind = action.get('type')
        terminal = None
        if kind == 'retreat':
            if state.get('phase') == 'battle' and str(state.get('battle', {}).get('current_actor_id', '')).startswith('player:'):
                ok, message, engine_terminal = self.engine.act(state, profile['profile_id'], action)
                if not ok:
                    return ActionResult(False, message)
                terminal = engine_terminal
                if state.get('phase') == 'dead':
                    terminal = 'defeat'
                elif state.get('phase') == 'retreated':
                    terminal = 'retreated'
            else:
                if profile['profile_id'] != self._captain_id(state, run):
                    return ActionResult(False, '只有队长（或队长失能后的接任者）可以发起团队撤退。')
                state['phase'] = 'retreated'
                player['actions_taken'] = int(player.get('actions_taken', 0)) + 1
                if state.get('battle'):
                    state['battle']['status'] = 'retreated'
                message, terminal = '队长已带领队伍撤退；存活且符合条件的个人成果将分别结算。', 'retreated'
        elif kind == 'extract':
            if profile['profile_id'] != self._captain_id(state, run):
                return ActionResult(False, '只有队长（或队长失能后的接任者）可以发起团队撤离。')
            if state.get('phase') != 'extraction':
                return ActionResult(False, '只有到达操场出口后才能安全撤离。')
            domain = self.registry.get('domains', run['domain_id'], run['pack_id'], run['pack_version'])
            if not domain:
                return ActionResult(False, '当前副本内容版本无法读取。')
            missing = missing_objectives(domain.data, state, profile['profile_id'])
            if missing:
                names = {goal['id']: goal.get('description', goal['id']) for goal in domain.data.get('goals', [])}
                return ActionResult(False, '撤离条件尚未完成：' + '；'.join((names.get(goal_id, goal_id) for goal_id in missing)))
            state['phase'] = 'extracted'
            player['actions_taken'] = int(player.get('actions_taken', 0)) + 1
            message, terminal = '团队成功从七中学校撤离，正在按队员资格结算个人成果。', 'completed'
        elif state.get('phase') == 'battle':
            ok, message, engine_terminal = self.engine.act(state, profile['profile_id'], action)
            if not ok:
                return ActionResult(False, message)
            if state.get('phase') == 'dead':
                terminal = 'defeat'
            elif state.get('phase') == 'retreated':
                terminal = 'retreated'
            else:
                terminal = engine_terminal
        else:
            if current_explorer(state) != profile['profile_id']:
                return ActionResult(False, f'当前由 {self._member_name(state, current_explorer(state))} 探索；请等待你的探索行动。')
            if kind == 'forward':
                ok, message = self._forward(state, run, profile['profile_id'])
            elif kind == 'investigate':
                ok, message = self._investigate(state)
            elif kind == 'story_choice':
                character = self.registry.get('characters', str(action.get('character_id', '')), run['pack_id'], run['pack_version'])
                if not character or character.data.get('location_ref') != self._current_location(state):
                    return ActionResult(False, '剧情人物当前不在此地点。')
                ok, message = apply_dialogue_choice(character.data, state, profile['profile_id'], str(action.get('choice_id', '')))
            else:
                return ActionResult(False, '探索阶段可以前进、调查当前地点或选择对话。')
            if not ok:
                return ActionResult(False, message)
            complete_exploration_action(state, profile['profile_id'])
        if terminal:
            self._settle_party(connection, run, state, terminal)
        else:
            self.repository.save_run_state_in(connection, run['run_id'], run['version'], state)
        return ActionResult(True, message, {'run_id': run['run_id'], 'terminal': terminal or ''})

    def _forward(self, state: dict[str, Any], run: dict[str, Any], explorer_id: str) -> tuple[bool, str]:
        if state.get('phase') != 'explore':
            return (False, '只有探索阶段可以继续前进。')
        domain = self.registry.get('domains', run['domain_id'], run['pack_id'], run['pack_version'])
        if not domain:
            return (False, '当前副本内容版本无法读取。')
        nodes = domain.data.get('nodes', [])
        next_index = int(state['route'].get('node_index', 0)) + 1
        if next_index >= len(nodes):
            return (False, '前方已无可前进地点，请由队长发起撤离。')
        state['route']['node_index'] = next_index
        node = nodes[next_index]
        state['shared']['environment'] = dict(node.get('environment', {}))
        story_text = '\n'.join(trigger_location_events(self._stories(run), state, explorer_id, node['id']))
        difficulty = str(state.get('difficulty', 'normal'))
        encounters = [*domain.data.get('encounters', []), *extra_encounters_for_difficulty(domain.data, difficulty)]
        for encounter in encounters:
            if encounter.get('location_ref') != node.get('id'):
                continue
            refs = encounter_refs_for_difficulty(domain.data, encounter, difficulty, 'party')
            battle_text = self.engine.start_encounter(state, encounter['id'], refs)
            prefix = f'队伍抵达【{node.get('name', node['id'])}】，共享遭遇开始！'
            return True, '\n'.join((part for part in (story_text, prefix, battle_text) if part))
        if next_index == len(nodes) - 1:
            state['phase'] = 'extraction'
            prefix = f'队伍抵达【{node.get('name', node['id'])}】，队长可发起 `/鬼域 撤离`。'
            return True, '\n'.join((part for part in (story_text, prefix) if part))
        prefix = f'队伍前进到【{node.get('name', node['id'])}】。地点线索由全队共享。'
        return True, '\n'.join((part for part in (story_text, prefix) if part))

    def _investigate(self, state: dict[str, Any]) -> tuple[bool, str]:
        domain = self.registry.get('domains', state['domain_id'], state['pack_id'], state['pack_version'])
        if not domain:
            return (False, '当前副本内容版本无法读取。')
        index = int(state['route'].get('node_index', 0))
        nodes = domain.data.get('nodes', [])
        if index < 0 or index >= len(nodes):
            return (False, '当前地点不存在。')
        node = nodes[index]
        clues = node.get('clues', [])
        evidence = state['shared'].setdefault('exploration_evidence', {}).setdefault(node['id'], {'clue_index': 0})
        clue_index = int(evidence.get('clue_index', 0))
        if clue_index >= len(clues):
            return (False, '这个地点暂时没有新的线索。')
        clue = clues[clue_index]
        evidence['clue_index'] = clue_index + 1
        state['shared'].setdefault('clues_found', []).append({'node_id': node['id'], 'text': clue})
        counters = state['shared'].setdefault('counters', {})
        counters['clues_found'] = int(counters.get('clues_found', 0)) + 1
        return True, f'调查【{node.get('name', node['id'])}】：{clue}（线索与队伍共享）。'

    def _stories(self, run: Mapping[str, Any]) -> list[dict[str, Any]]:
        return [entry.data for entry in self.registry.list_entries('stories', run['pack_id'], run['pack_version'])]

    @staticmethod
    def _current_location(state: Mapping[str, Any]) -> str:
        route = state.get('route', {})
        nodes = route.get('node_ids', [])
        index = int(route.get('node_index', -1))
        if 0 <= index < len(nodes):
            return str(nodes[index])
        return ''

    def dialogue_response(self, identity: Identity, character_name: str) -> GameResponse:
        run = self.repository.get_active_run(identity)
        profile = self.repository.get_profile(identity)
        if not run or not profile or run['kind'] != 'party':
            return GameResponse('当前没有可交谈的团队副本。')
        if run['adapter_id'] != identity.adapter_id or run['group_id'] != identity.group_id:
            return GameResponse(f'请回副本发起群继续：群号 {run['group_id']}。')
        if run['state'].get('phase') != 'explore':
            return GameResponse('只有探索阶段可以与剧情人物交谈。')
        current_id = current_explorer(run['state'])
        if current_id != profile['profile_id']:
            return GameResponse(f'当前轮到 {self._member_name(run['state'], current_id)} 探索。')
        character = self.registry.find_entry('characters', character_name, run['pack_id'], run['pack_version'])
        if not character:
            return GameResponse('当前地点没有这个剧情人物。')
        text, choices = dialogue_view(character.data, run['state'], profile['profile_id'], self._current_location(run['state']))
        buttons = tuple((self._action_button(identity, run, {'type': 'story_choice', 'character_id': character.data['id'], 'choice_id': choice['id']}, str(choice['label'])) for choice in choices[:5]))
        return GameResponse(text, buttons)

    def _settle_party(self, connection: sqlite3.Connection, run: dict[str, Any], state: dict[str, Any], outcome: str) -> None:
        settlement_id = run['run_id']
        state['settlement_id'] = settlement_id
        domain = self.registry.get('domains', run['domain_id'], run['pack_id'], run['pack_version'])
        if not domain:
            raise ValueError('pinned domain content is missing during settlement')
        difficulty = str(state.get('difficulty', 'normal'))
        minimum_actions = int(domain.data.get('difficulty_levels', {}).get(difficulty, {}).get('minimum_party_actions', 2))
        qualified = set(eligible_settlement_profiles(state, minimum_actions=minimum_actions))
        rows = connection.execute('SELECT p.* FROM run_members m JOIN profiles p ON p.profile_id = m.profile_id WHERE m.run_id = ?', (run['run_id'],)).fetchall()
        for row in rows:
            profile_payload = json.loads(row['payload_json'])
            settle_profile(profile_payload, state, row['profile_id'], domain.data, outcome, settlement_id, row['profile_id'] in qualified)
            self.repository.save_profile_payload_in(connection, row['profile_id'], profile_payload)
        self.repository.save_run_state_in(connection, run['run_id'], run['version'], state, 'ended')
        connection.execute('UPDATE run_members SET is_active = 0 WHERE run_id = ?', (run['run_id'],))

    @staticmethod
    def _member_name(state: Mapping[str, Any], profile_id: str | None) -> str:
        if not profile_id:
            return '无人'
        player = state.get('players', {}).get(profile_id, {})
        return str(player.get('user_id', profile_id))

    def response_after_action(self, identity: Identity, message: str) -> GameResponse:
        run = self.repository.get_active_run(identity)
        if not run or run['kind'] != 'party' or run['adapter_id'] != identity.adapter_id or run['group_id'] != identity.group_id:
            return GameResponse(message)
        state = run['state']
        phase = state.get('phase')
        if phase == 'extraction':
            captain = self._captain_id(state, run)
            target_identity = self._identity_for_profile(run, state, captain)
            button = self._action_button(target_identity, run, {'type': 'extract'}, '团队安全撤离')
            return GameResponse(message + '\n已到达操场出口，请队长确认团队撤离。', (button,))
        if phase == 'battle':
            return self._party_battle_response(identity, run, state, message)
        if phase != 'explore':
            return GameResponse(message)
        profile_id = current_explorer(state)
        if not profile_id:
            return GameResponse(message + '\n没有存活队员可以继续探索。')
        target_identity = self._identity_for_profile(run, state, profile_id)
        domain = self.registry.get('domains', run['domain_id'], run['pack_id'], run['pack_version'])
        nodes = domain.data.get('nodes', []) if domain else []
        index = int(state.get('route', {}).get('node_index', 0))
        location = nodes[index].get('name', '未知地点') if 0 <= index < len(nodes) else '未知地点'
        player = state['players'][profile_id]
        buttons = [self._action_button(target_identity, run, {'type': 'forward'}, '继续前进'), self._action_button(target_identity, run, {'type': 'investigate'}, '调查地点')]
        party_order = state.get('party', {}).get('exploration_order', [])
        member_names = '、'.join((self._member_name(state, member_id) for member_id in party_order))
        return GameResponse(f'{message}\n团队探索：{member_names}\n当前地点：{location}\n轮到 {self._member_name(state, profile_id)} 探索。（HP {player['hp']}/{player['max_hp']}，SAN {player['san']}/{player['max_san']}；线索全队共享。）', tuple(buttons))

    def status_response(self, identity: Identity) -> GameResponse:
        run = self.repository.get_active_run(identity)
        if not run or run['kind'] != 'party' or run['adapter_id'] != identity.adapter_id or run['group_id'] != identity.group_id:
            return GameResponse('当前群没有你的进行中团队副本。')
        state = run['state']
        players = state.get('players', {})
        names = '、'.join((f'{self._member_name(state, profile_id)} HP {player['hp']}/{player['max_hp']} SAN {player['san']}/{player['max_san']}' for profile_id, player in players.items()))
        return self.response_after_action(identity, f'团队状态\n{names}')

    def _party_battle_response(self, identity: Identity, run: dict[str, Any], state: dict[str, Any], message: str) -> GameResponse:
        battle = state.get('battle', {})
        actor_id = str(battle.get('current_actor_id', ''))
        if not actor_id:
            return GameResponse(message)
        enemies = [unit for unit in state.get('ghost_units', []) if unit.get('side') == 'enemy' if int(unit.get('hp', 0)) > 0]
        enemy_text = '、'.join((f'{unit['name']} {unit['hp']}/{unit['max_hp']} HP' for unit in enemies)) or '无'
        if actor_id.startswith('player:'):
            profile_id = actor_id.split(':', 1)[1]
            player = state['players'].get(profile_id, {})
            buttons = []
            for item_id, quantity in player.get('inventory', {}).items():
                if quantity <= 0:
                    continue
                entry = self.registry.get('items', item_id, run['pack_id'], run['pack_version'])
                if not entry:
                    continue
                if not any((effect.get('type') == 'heal' for effect in entry.data.get('effects', []))):
                    continue
                buttons.append(self._action_button(self._identity_for_profile(run, state, profile_id), run, {'type': 'item', 'item_id': item_id, 'target_id': 'player'}, f'使用{entry.data['name']}'))
                break
            buttons.append(self._action_button(self._identity_for_profile(run, state, profile_id), run, {'type': 'retreat'}, '尝试绝境撤离'))
            return GameResponse(f'{message}\n绝境选择队员 {self._member_name(state, profile_id)}；敌方：{enemy_text}。只能使用道具或尝试撤离。', tuple(buttons))
        actor = next((unit for unit in state.get('ghost_units', []) if unit.get('unit_id') == actor_id), None)
        if not actor:
            return GameResponse(message)
        owner_id = str(actor.get('owner_profile_id', ''))
        owner_identity = self._identity_for_profile(run, state, owner_id)
        owner = state['players'].get(owner_id, {})
        buttons = []
        if enemies:
            target = enemies[0]
            buttons.append(self._action_button(owner_identity, run, {'type': 'attack', 'unit_id': actor_id, 'target_id': target['unit_id']}, f'攻擊{target['name']}'))
            ghost_data = self._ghost_entry_for_unit(run, actor)
            skills = ghost_data.data.get('skills', []) if ghost_data else []
            if skills:
                buttons.append(self._action_button(owner_identity, run, {'type': 'skill', 'unit_id': actor_id, 'skill_id': skills[0]['id'], 'target_id': target['unit_id']}, f'技能：{skills[0]['name']}'))
            buttons.append(self._action_button(owner_identity, run, {'type': 'observe', 'unit_id': actor_id, 'target_id': target['unit_id']}, '观察规律'))
            target_data = self.registry.get('ghosts', target['ghost_id'], run['pack_id'], run['pack_version'])
            controlled = len(owner.get('ghost_unit_ids', []))
            max_slots = int(owner.get('max_ghost_slots', state.get('max_ghost_slots', 2)))
            if target_data and capture_probability(target_data.data, int(target['hp']), int(target['max_hp']), 0, controlled, max_slots) is not None:
                buttons.append(self._action_button(owner_identity, run, {'type': 'capture', 'unit_id': actor_id, 'target_id': target['unit_id']}, '尝试捕获'))
        buttons.append(self._action_button(owner_identity, run, {'type': 'skip', 'unit_id': actor_id}, '防守并跳过'))
        return GameResponse(f'{message}\n轮到 {self._member_name(state, owner_id)} 指挥【{actor['name']}】（忠诚度 {actor.get('loyalty', 0):.0f}）。本人 HP {owner.get('hp', 0)}/{owner.get('max_hp', 0)}、SAN {owner.get('san', 0)}/{owner.get('max_san', 0)}；共享敌方：{enemy_text}。', tuple(buttons[:5]))

    def _action_button(self, identity: Identity, run: dict[str, Any], action: dict[str, Any], label: str) -> ButtonSpec:
        action_id = self.repository.issue_action(identity, 'party_action', {'run_id': run['run_id'], 'expected_version': run['version'], 'action': action})
        command = self._command_for_action(run, action)
        return ButtonSpec(label, action_id, input_command=command)

    def _ghost_entry_for_unit(self, run: Mapping[str, Any], unit: Mapping[str, Any]):
        return self.registry.get(
            'ghosts',
            str(unit.get('ghost_id', '')),
            str(unit.get('ghost_pack_id') or run['pack_id']),
            str(unit.get('ghost_pack_version') or run['pack_version']),
        )

    def _command_for_action(self, run: dict[str, Any], action: dict[str, Any]) -> str:
        state = run.get('state', {})
        kind = action.get('type')
        simple = {'forward': '前进', 'investigate': '调查', 'extract': '撤离', 'retreat': '撤退', 'guard': '护主', 'skip': '跳过'}
        if kind in simple:
            return f'/鬼域 {simple[kind]}'
        if kind == 'story_choice':
            return f'/鬼域 选择 {action.get('character_id', '')} {action.get('choice_id', '')}'.strip()
        if kind == 'item':
            return f'/鬼域 使用 {action.get('item_id', '')}'.strip()
        if not isinstance(state, dict) or not state:
            return ''
        unit_id = str(action.get('unit_id', ''))
        players = state.get('players', {})
        owner_id = next((pid for pid, player in players.items() if unit_id in player.get('ghost_unit_ids', [])), str(run.get('leader_profile_id', '')))
        ids = list(players.get(owner_id, {}).get('ghost_unit_ids', []))
        slot = ids.index(unit_id) + 1 if unit_id in ids else 1
        def unit_name(target_id: Any) -> str:
            unit = next((item for item in state.get('ghost_units', []) if item.get('unit_id') == target_id), None)
            if unit:
                return str(unit.get('name', ''))
            return ''

        if kind == 'attack':
            return f'/鬼域 指挥 {slot} 攻击 {unit_name(action.get('target_id'))}'.strip()
        if kind == 'skill':
            return f'/鬼域 指挥 {slot} 技能 {action.get('skill_id', '')} {unit_name(action.get('target_id'))}'.strip()
        if kind == 'observe':
            return f'/鬼域 观察 {unit_name(action.get('target_id'))}'.strip()
        if kind == 'capture':
            return f'/鬼域 捕获 {unit_name(action.get('target_id'))}'.strip()
        return ''

    @staticmethod
    def _identity_for_profile(run: Mapping[str, Any], state: Mapping[str, Any], profile_id: str) -> Identity:
        player = state.get('players', {}).get(profile_id, {})
        return Identity(str(run['adapter_id']), str(run['adapter_id']), str(player.get('user_id', profile_id)), str(run['group_id']))


