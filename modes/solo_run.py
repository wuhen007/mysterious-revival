from __future__ import annotations
import hashlib
import sqlite3
import time
import uuid
from collections.abc import Mapping
from typing import Any
from ..engine.battle import BattleEngine
from ..engine.daily import consume as consume_action
from ..engine.randomness import draw_float
from ..engine.recovery import (apply_deadlock, conflict_strength, deadlock_blockers, deadlock_chance, essence_of, mark_deadlock_failure)
from ..engine.campaign import (difficulty_is_unlocked, encounter_refs_for_difficulty, extra_encounters_for_difficulty, settle_profile)
from ..engine.ghost_control import (build_solo_run_state, capture_probability, recruit_once, resolve_owned_ghost_entry)
from ..engine.models import (ActionResult, ButtonSpec, GameResponse, Identity)
from ..engine.story import (apply_dialogue_choice, dialogue_view, missing_objectives, trigger_location_events)
from ..storage.repository import GameRepository

class SoloRunService:
    def __init__(self, repository: GameRepository, registry: Any, max_ghost_slots: int=2):
        self.repository = repository
        self.registry = registry
        self.engine = BattleEngine(registry)
        self.max_ghost_slots = max(1, int(max_ghost_slots))

    def recruit(self, identity: Identity) -> ActionResult:
        domain = self.registry.find_domain('七中学校')
        if not domain:
            return ActionResult(False, '七中学校内容尚未加载，暂时无法完成总部招募。')
        manifest = self.registry.get_manifest(domain.pack_id, domain.version)
        if not manifest:
            return ActionResult(False, '总部起始驭鬼配置缺失，请管理员检查内容包。')
        with self.repository.transaction() as connection:
            profile, created = self.repository.ensure_profile_in(connection, identity)
            payload = dict(profile['payload'])
            try:
                ghost_id = recruit_once(payload, manifest)
            except (KeyError, TypeError, ValueError) as error:
                return ActionResult(False, f'总部起始驭鬼配置无效：{error}')
            if not ghost_id or not self.registry.get('ghosts', ghost_id, domain.pack_id, domain.version):
                return ActionResult(False, '起始驭鬼未注册到当前内容版本。')
            refs = payload.get('owned_ghost_refs')
            if not isinstance(refs, dict):
                refs = {}
            refs.setdefault(ghost_id, {'pack_id': domain.pack_id, 'pack_version': domain.version})
            payload['owned_ghost_refs'] = refs
            payload.setdefault('active_ghost_ids', [ghost_id])
            payload.setdefault('max_ghost_slots', self.max_ghost_slots)
            payload.setdefault('lifespan_days', 90)
            payload.setdefault('lifespan_started_at', time.time())
            self.repository.save_profile_payload_in(connection, profile['profile_id'], payload)
        if created:
            return ActionResult(True, f'总部档案已建立，随机配发初始驭鬼【{self._ghost_name(domain.pack_id, domain.version, ghost_id)}】。\n七中学校委托补给将在开局时发放。')
        return ActionResult(True, f'总部档案已保存；你的初始驭鬼是【{self._ghost_name(domain.pack_id, domain.version, ghost_id)}】，不会因重复招募而重抽。')

    def mode_button(self, identity: Identity, domain: dict[str, Any], pack_id: str, version: str, difficulty: str='normal') -> ButtonSpec:
        action_id = f'prompt:solo:{'hard' if difficulty == 'hard' else 'normal'}'
        label = ('困难' if difficulty == 'hard' else '普通') + '·单人'
        command = f'/鬼域 开局 {domain.get('name', '七中学校')} 单人 {'困难' if difficulty == 'hard' else '普通'}'
        return ButtonSpec(label, action_id, input_command=command)

    def create(self, identity: Identity, action_id: str) -> ActionResult:
        def create_in(connection: sqlite3.Connection, payload: dict[str, Any]) -> ActionResult:
            active = self.repository.active_run_in(connection, identity)
            if active:
                return ActionResult(False, '你已有进行中的副本；请回原群继续或先结束当前副本。', {'run': active})
            profile, _created = self.repository.ensure_profile_in(connection, identity)
            profile_payload = dict(profile['payload'])
            if not profile_payload.get('owned_ghost_ids'):
                return ActionResult(False, '请先发送 `/鬼域 开始` 接受总部招募并获得初始驭鬼。')
            pack_id, version = str(payload['pack_id']), str(payload['pack_version'])
            domain_entry = self.registry.get('domains', str(payload['domain_id']), pack_id, version)
            if not domain_entry:
                return ActionResult(False, '副本内容版本不存在。')
            starter_id = str(profile_payload.get('starter_ghost_id', profile_payload['owned_ghost_ids'][0]))
            if starter_id not in profile_payload['owned_ghost_ids']:
                return ActionResult(False, '档案中的初始驭鬼不在永久驭鬼列表中，请检查存档。')
            difficulty = str(payload.get('difficulty', 'normal'))
            if not difficulty_is_unlocked(profile_payload, domain_entry.data, difficulty):
                return ActionResult(False, f'委托难度 `{difficulty}` 尚未解锁。')
            refs = profile_payload.get('owned_ghost_refs')
            if not isinstance(refs, dict):
                refs = {}
            for owned_id in profile_payload['owned_ghost_ids']:
                ghost_id = str(owned_id)
                entry = resolve_owned_ghost_entry(self.registry, profile_payload, ghost_id, pack_id, version)
                if entry is None:
                    return ActionResult(False, f'档案中的驭鬼【{ghost_id}】缺少可用内容版本，无法开启副本。')
                refs.setdefault(ghost_id, {'pack_id': entry.pack_id, 'pack_version': entry.version})
            profile_payload['owned_ghost_refs'] = refs
            starter_ref = refs[starter_id]
            profile_payload.setdefault('starter_pack_id', starter_ref['pack_id'])
            profile_payload.setdefault('starter_pack_version', starter_ref['pack_version'])
            profile_payload.setdefault('active_ghost_ids', [starter_id])
            profile_payload.setdefault('max_ghost_slots', self.max_ghost_slots)
            run_profile = {**profile, 'payload': profile_payload, 'pack_id': pack_id, 'pack_version': version}
            try:
                state = build_solo_run_state(run_profile, domain_entry.data, self.registry, difficulty)
            except (KeyError, TypeError, ValueError) as error:
                return ActionResult(False, f'无法建立副本：{error}')
            self.repository.save_profile_payload_in(connection, profile['profile_id'], profile_payload)
            run = self.repository.create_run_in(connection, identity, profile['profile_id'], 'solo', payload['domain_id'], pack_id, version, 'active', state)
            domain_name = str(domain_entry.data.get('name', payload['domain_id']))
            return ActionResult(True, f'{domain_name}单人副本已建立（编号 {run['invite_code']}）。', {'run_id': run['run_id']})

        return self.repository.perform_action(identity, action_id, 'select_solo', create_in)

    def perform_text_action(self, identity: Identity, action: dict[str, Any]) -> GameResponse:
        run = self.repository.get_active_run(identity)
        if not run:
            return GameResponse('你当前没有进行中的单人副本。')
        if run['adapter_id'] != identity.adapter_id or run['group_id'] != identity.group_id:
            return GameResponse(f'请回副本发起群继续：群号 {run['group_id']}。')
        if run['kind'] != 'solo':
            return GameResponse('团队战斗指令将在团队战斗阶段接入。')
        key = identity.message_id
        action_id = hashlib.sha256(f'{identity.adapter_id}:{identity.user_id}:{identity.group_id}:{key}'.encode()).hexdigest() if key else uuid.uuid4().hex
        pending_id = self.repository.issue_action(identity, 'solo_action', {'run_id': run['run_id'], 'expected_version': run['version'], 'action': action}, action_id=action_id)
        return self.perform_action(identity, pending_id)

    def button_action(self, identity: Identity, action_id: str) -> GameResponse:
        return self.perform_action(identity, action_id)

    def perform_action(self, identity: Identity, action_id: str) -> GameResponse:
        result = self.repository.perform_action(identity, action_id, 'solo_action', (lambda conn, payload: self._apply_action(conn, identity, payload)))
        suffix = '（此操作已处理，未重复结算。）' if result.replayed else ''
        return self.response_after_action(identity, result.message + suffix)

    def _apply_action(self, connection: sqlite3.Connection, identity: Identity, payload: dict[str, Any]) -> ActionResult:
        run = self.repository.run_snapshot_in(connection, str(payload.get('run_id', '')))
        profile = self.repository.profile_in(connection, identity)
        if not run or not profile:
            return ActionResult(False, '副本或个人档案不存在。')
        if run['adapter_id'] != identity.adapter_id or run['group_id'] != identity.group_id:
            return ActionResult(False, '只能在副本发起群操作。')
        if run['version'] != payload.get('expected_version'):
            return ActionResult(False, '副本状态已变化，请重新查看后再操作。')
        if run['kind'] != 'solo' or run['leader_profile_id'] != profile['profile_id']:
            return ActionResult(False, '只有本局单人副本所有者可以操作。')
        if run['status'] != 'active':
            return ActionResult(False, '副本已经结束。')
        state = run['state']
        campaign_payload = dict(profile['payload'])
        owned_units = [unit for unit in state.get('ghost_units', []) if unit.get('side') == 'friendly' if unit.get('owner_profile_id') == profile['profile_id'] if not unit.get('rebelled')]
        all_deadlocked = bool(owned_units) and all((unit.get('deadlocked') for unit in owned_units))
        allowed, daily_message = consume_action(campaign_payload, len(owned_units), all_deadlocked)
        if not allowed:
            return ActionResult(False, daily_message)
        action = self._resolve_action(state, profile['profile_id'], dict(payload.get('action', {})))
        kind = action.get('type')
        if kind == 'deadlock':
            ok, message = self._deadlock(state, run, profile['profile_id'], str(action.get('target', '')))
            terminal = None
        elif kind == 'forward':
            ok, message = self._forward(state, run)
            terminal = None
        elif kind == 'extract':
            ok, message, terminal = self._extract(state, profile['profile_id'])
        elif kind == 'investigate' and state.get('phase') in {'explore', 'extraction'}:
            ok, message = self._investigate(state)
            terminal = None
            if ok:
                self._count_solo_action(state, profile['profile_id'])
        elif kind == 'story_choice' and state.get('phase') == 'explore':
            character = self.registry.get('characters', str(action.get('character_id', '')), run['pack_id'], run['pack_version'])
            if not character:
                return ActionResult(False, '剧情人物不存在于本局固定内容版本。')
            location = self._current_location(state)
            if character.data.get('location_ref') != location:
                return ActionResult(False, '该人物当前不在此地点。')
            ok, message = apply_dialogue_choice(character.data, state, profile['profile_id'], str(action.get('choice_id', '')))
            terminal = None
            if ok:
                self._count_solo_action(state, profile['profile_id'])
        elif kind == 'retreat' and state.get('phase') != 'battle':
            state['phase'] = 'retreated'
            ok, message, terminal = True, '你选择从当前地点撤离；成功撤离会保留本局捕获。', 'ended'
        else:
            ok, message, terminal = self.engine.act(state, profile['profile_id'], action)
        if not ok:
            return ActionResult(False, message)
        profile_payload = campaign_payload
        outcome = self._outcome(state, terminal)
        if outcome:
            domain = self.registry.get('domains', run['domain_id'], run['pack_id'], run['pack_version'])
            if domain:
                settlement = settle_profile(profile_payload, state, profile['profile_id'], domain.data, outcome, run['run_id'], True)
                message += self._progress_text(settlement)
        else:
            player_state = state.get('players', {}).get(profile['profile_id'], {})
            journal = profile_payload.setdefault('discovered_rules', [])
            for rule_id in player_state.get('discovered_rules', []):
                if rule_id not in journal:
                    journal.append(rule_id)
        self.repository.save_profile_payload_in(connection, profile['profile_id'], profile_payload)
        ended = outcome is not None
        self.repository.save_run_state_in(connection, run['run_id'], run['version'], state, 'ended' if ended else None)
        if ended:
            connection.execute('UPDATE run_members SET is_active = 0 WHERE run_id = ?', (run['run_id'],))
        return ActionResult(True, message, {'terminal': outcome or ''})

    @staticmethod
    def _count_solo_action(state: dict[str, Any], profile_id: str) -> None:
        player = state.get('players', {}).get(profile_id)
        if player is not None:
            player['actions_taken'] = int(player.get('actions_taken', 0)) + 1

    @staticmethod
    def _outcome(state: dict[str, Any], terminal: str | None) -> str | None:
        if state.get('phase') == 'extracted':
            return 'completed'
        if state.get('phase') == 'retreated':
            return 'retreated'
        if state.get('phase') == 'dead':
            return 'dead'
        return 'abandoned' if terminal == 'abandoned' else None

    def _resolve_action(self, state: dict[str, Any], profile_id: str, action: dict[str, Any]) -> dict[str, Any]:
        player = state.get('players', {}).get(profile_id, {})
        slot = action.pop('slot', None)
        if slot is not None:
            try:
                index = int(slot) - 1
                action['unit_id'] = player['ghost_unit_ids'][index]
            except (ValueError, IndexError, KeyError, TypeError):
                action['unit_id'] = 'invalid-slot'
        actor = self._unit(state, str(action.get('unit_id', ''))) if action.get('unit_id') else None
        action_type = action.get('type')
        if action_type in {'skill', 'attack', 'capture', 'observe'}:
            if not action.get('target_id'):
                target_label = str(action.pop('target', ''))
                enemies = [u for u in state.get('ghost_units', []) if u.get('side') == 'enemy' if int(u.get('hp', 0)) > 0]
                if target_label:
                    entry = self.registry.find_entry('ghosts', target_label, state['pack_id'], state['pack_version'])
                    ghost_id = entry.data['id'] if entry else target_label.casefold()
                    found = next((u for u in enemies if u['ghost_id'].casefold() == str(ghost_id).casefold() or u['name'].casefold() == target_label.casefold()), None)
                else:
                    found = enemies[0] if enemies else None
                action['target_id'] = found['unit_id'] if found else ''
        if action_type in {'item', 'capture'}:
            item_label = str(action.get('item_id', ''))
            entry = self.registry.find_entry('items', item_label, state['pack_id'], state['pack_version'])
            if entry:
                action['item_id'] = entry.data['id']
            target_label = str(action.get('target', '')) if action_type == 'item' else ''
            if target_label.casefold() in {'player', '玩家'}:
                action['target_id'] = 'player'
            elif target_label:
                ghost = self.registry.find_entry('ghosts', target_label, state['pack_id'], state['pack_version'])
                wanted_id = ghost.data['id'] if ghost else target_label.casefold()
                found = next((u for u in state.get('ghost_units', []) if int(u.get('hp', 0)) > 0 and (u.get('ghost_id', '').casefold() == str(wanted_id).casefold() or u.get('name', '').casefold() == target_label.casefold())), None)
                action['target_id'] = found['unit_id'] if found else 'invalid-target'
            elif not action.get('target_id'):
                action['target_id'] = 'player'
        if action_type == 'skill' and actor:
            skill_label = str(action.get('skill_id', ''))
            data = self._ghost_entry_for_unit(state, actor)
            if data:
                skill = next((s for s in data.data.get('skills', []) if s.get('id', '').casefold() == skill_label.casefold() or s.get('name', '').casefold() == skill_label.casefold()), None)
                if not skill and not skill_label:
                    if data.data.get('skills'):
                        skill = data.data['skills'][0]
                if skill:
                    action['skill_id'] = skill['id']
        return action

    def _ghost_behavior(self, state: Mapping[str, Any], unit: Mapping[str, Any]) -> str:
        entry = self._ghost_entry_for_unit(state, unit)
        behavior = entry.data.get('behavior', {}) if entry else {}
        return str(behavior.get('kind', 'brute'))

    def _deadlock(self, state: dict[str, Any], run: dict[str, Any], profile_id: str, target: str) -> tuple[bool, str]:
        player = state['players'][profile_id]
        owned = [unit for unit in state.get('ghost_units', []) if unit.get('side') == 'friendly' if unit.get('owner_profile_id') == profile_id if not unit.get('rebelled') if int(unit.get('hp', 0)) > 0]
        needle = target.strip().casefold()
        unit = next((item for item in owned if needle and (needle in str(item.get('name', '')).casefold() or needle == str(item.get('ghost_id', '')).casefold())), None)
        if unit is None:
            return (False, '格式：`/鬼域 死机 <已驾驭的鬼名>`；必须先拥有这只鬼。')
        candidates = [item for item in owned if item is not unit]
        enemies = [item for item in state.get('ghost_units', []) if item.get('side') == 'enemy' if int(item.get('hp', 0)) > 0]
        candidates.extend(enemies)
        behavior = self._ghost_behavior(state, unit)
        scored = [(conflict_strength(essence_of(str(unit.get('ghost_id', '')), behavior), essence_of(str(item.get('ghost_id', '')), self._ghost_behavior(state, item))), item) for item in candidates]
        conflict, partner = max(scored, key=(lambda pair: pair[0]), default=(0.0, None))
        reason = deadlock_blockers(unit, partner, conflict)
        if reason:
            return False, reason
        if int(player.get('san', 0)) < 20:
            return (False, 'SAN 不足 20，无法支撑一次死机尝试。')
        player['san'] = int(player['san']) - 20
        chance = deadlock_chance(int(unit.get('recovery', 0)), conflict, int(player.get('san', 0)))
        if draw_float(state['rng']) < chance:
            apply_deadlock(unit)
            if partner.get('side') == 'friendly':
                apply_deadlock(partner)
                partner_text = f'，{partner['name']} 同时死机'
            else:
                partner['hp'] = 0
                partner['deadlocked'] = True
                partner_text = f'，{partner['name']} 被死锁拖入沉寂'
            state['deadlock_pairs'] = int(state.get('deadlock_pairs', 0)) + 1
            return True, f'【死机成功】{unit['name']} 与 {partner['name']} 的规则互相咬死，双双停在原地{partner_text}。（成功率 {chance:.0%}）从此刻起，它不再产生复苏。'
        mark_deadlock_failure(unit)
        if int(unit.get('recovery', 0)) >= 100:
            player['hp'] = 0
            player['alive'] = False
            state['phase'] = 'dead'
            return True, f'死机失败，{unit['name']} 彻底复苏并接管了你的身体。'
        return True, f'死机失败（成功率 {chance:.0%}）。{unit['name']} 的复苏升至 {int(unit.get('recovery', 0))}/100，忠诚度下降。'

    def _forward(self, state: dict[str, Any], run: dict[str, Any]) -> tuple[bool, str]:
        if state.get('phase') != 'explore':
            return (False, '只有探索阶段可以继续前进。')
        domain = self.registry.get('domains', run['domain_id'], run['pack_id'], run['pack_version'])
        if not domain:
            return (False, '当前副本内容版本无法读取。')
        nodes = domain.data.get('nodes', [])
        route = state['route']
        next_index = int(route['node_index']) + 1
        if next_index >= len(nodes):
            return (False, '前方已无可前进地点，请选择撤离或结束副本。')
        route['node_index'] = next_index
        node = nodes[next_index]
        state['shared']['environment'] = dict(node.get('environment', {}))
        profile_id = run['leader_profile_id']
        story_text = '\n'.join(trigger_location_events(self._stories(run), state, profile_id, node['id']))
        name = node.get('name', node['id'])
        encounters = [*domain.data.get('encounters', []), *extra_encounters_for_difficulty(domain.data, state.get('difficulty', 'normal'))]
        for encounter in encounters:
            if encounter.get('location_ref') != node.get('id'):
                continue
            refs = encounter_refs_for_difficulty(domain.data, encounter, state.get('difficulty', 'normal'), 'solo')
            battle_text = self.engine.start_encounter(state, encounter['id'], refs)
            prefix = f'你来到【{name}】，灵异事件突然爆发！'
            return True, '\n'.join((part for part in (story_text, prefix, battle_text) if part))
        if next_index == len(nodes) - 1:
            state['phase'] = 'extraction'
            prefix = f'你抵达【{name}】，可以发送 `/鬼域 撤离`。'
            return True, '\n'.join((part for part in (story_text, prefix) if part))
        prefix = f'你前进到【{name}】。环境：{', '.join(node.get('tags', [])) or '普通'}。'
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
        return True, f'调查【{node.get('name', node['id'])}】：{clue}'

    def _extract(self, state: dict[str, Any], profile_id: str) -> tuple[bool, str, str | None]:
        if state.get('phase') != 'extraction':
            return (False, '只有到达操场出口后才能安全撤离。', None)
        domain = self.registry.get('domains', state['domain_id'], state['pack_id'], state['pack_version'])
        if not domain:
            return (False, '当前副本内容版本无法读取。', None)
        missing = missing_objectives(domain.data, state, profile_id)
        if missing:
            names = {goal['id']: goal.get('description', goal['id']) for goal in domain.data.get('goals', [])}
            return False, '撤离条件尚未完成：' + '；'.join((names.get(goal_id, goal_id) for goal_id in missing)), None
        state['phase'] = 'extracted'
        domain_name = str(domain.data.get('name', state['domain_id']))
        return True, f'你成功从{domain_name}撤离。本局捕获 {len(state.get('captured_ghost_ids', []))} 只鬼将按结算规则入档。', 'extracted'

    def _stories(self, run: Mapping[str, Any]) -> list[dict[str, Any]]:
        return [entry.data for entry in self.registry.list_entries('stories', run['pack_id'], run['pack_version'])]

    def _current_location(self, state: Mapping[str, Any]) -> str:
        route = state.get('route', {})
        nodes = route.get('node_ids', [])
        index = int(route.get('node_index', -1))
        if 0 <= index < len(nodes):
            return str(nodes[index])
        return ''

    def dialogue_response(self, identity: Identity, character_name: str) -> GameResponse:
        run = self.repository.get_active_run(identity)
        profile = self.repository.get_profile(identity)
        if not run or not profile or run['kind'] != 'solo':
            return GameResponse('当前没有可交谈的单人副本。')
        if run['adapter_id'] != identity.adapter_id or run['group_id'] != identity.group_id:
            return GameResponse(f'请回副本发起群继续：群号 {run['group_id']}。')
        if run['state'].get('phase') != 'explore':
            return GameResponse('只有探索阶段可以与剧情人物交谈。')
        character = self.registry.find_entry('characters', character_name, run['pack_id'], run['pack_version'])
        if not character:
            return GameResponse('当前地点没有这个剧情人物。')
        text, choices = dialogue_view(character.data, run['state'], profile['profile_id'], self._current_location(run['state']))
        buttons = tuple((self._action_button(identity, run, {'type': 'story_choice', 'character_id': character.data['id'], 'choice_id': choice['id']}, str(choice['label'])) for choice in choices[:5]))
        return GameResponse(text, buttons)

    def response_after_action(self, identity: Identity, message: str) -> GameResponse:
        run = self.repository.get_active_run(identity)
        if not run or run['adapter_id'] != identity.adapter_id or run['group_id'] != identity.group_id or run['kind'] != 'solo':
            return GameResponse(message)
        state = run['state']
        if state.get('phase') == 'battle':
            return self._battle_response(identity, run, message)
        if state.get('phase') == 'extraction':
            action_id = self.repository.issue_action(identity, 'solo_action', {'run_id': run['run_id'], 'expected_version': run['version'], 'action': {'type': 'extract'}})
            return GameResponse(message + '\n撤离会将本局成功捕获的鬼写入个人队伍。', (ButtonSpec('安全撤离', action_id, input_command='/鬼域 撤离'),))
        return GameResponse(
            message,
            (
                ButtonSpec('继续调查', 'prompt:solo:forward', input_command='/鬼域 前进'),
                ButtonSpec('查看补给', 'prompt:solo:inventory', input_command='/鬼域 背包'),
            ),
        )

    def _battle_response(self, identity: Identity, run: dict[str, Any], message: str) -> GameResponse:
        state = run['state']
        battle = state.get('battle', {})
        actor_id = battle.get('current_actor_id')
        buttons = []
        if actor_id == f'player:{run['leader_profile_id']}':
            player = state['players'][run['leader_profile_id']]
            inventory = player.get('inventory', {})
            for item_id, count in inventory.items():
                if not count > 0:
                    continue
                item = self.registry.get('items', item_id, run['pack_id'], run['pack_version'])
                if not item:
                    continue
                if not any((e.get('type') == 'heal' for e in item.data.get('effects', []))):
                    continue
                buttons.append(self._action_button(identity, run, {'type': 'item', 'item_id': item_id, 'target_id': 'player'}, f'使用{item.data['name']}'))
                break
            buttons.append(self._action_button(identity, run, {'type': 'retreat'}, '尝试撤退'))
            return GameResponse(message + '\n请选择一次绝境行动。', tuple(buttons[:5]))
        actor = self._unit(state, str(actor_id))
        enemies = [u for u in state.get('ghost_units', []) if u.get('side') == 'enemy' if int(u.get('hp', 0)) > 0]
        if not actor or not enemies:
            return GameResponse(message)
        target = enemies[0]
        buttons.append(self._action_button(identity, run, {'type': 'attack', 'unit_id': actor['unit_id'], 'target_id': target['unit_id']}, f'攻击{target['name']}'))
        data = self._ghost_entry_for_unit(state, actor)
        skills = data.data.get('skills', []) if data else []
        if skills:
            buttons.append(self._action_button(identity, run, {'type': 'skill', 'unit_id': actor['unit_id'], 'skill_id': skills[0]['id'], 'target_id': target['unit_id']}, f'技能：{skills[0]['name']}'))
        buttons.append(self._action_button(identity, run, {'type': 'observe', 'unit_id': actor['unit_id'], 'target_id': target['unit_id']}, '观察规律'))
        player = state['players'][run['leader_profile_id']]
        ghost_data = self._ghost_entry_for_unit(state, target)
        controlled_count = len(player.get('ghost_unit_ids', []))
        max_slots = int(player.get('max_ghost_slots', state.get('max_ghost_slots', 2)))
        if ghost_data and capture_probability(ghost_data.data, int(target['hp']), int(target['max_hp']), 0, controlled_count, max_slots) is not None:
            buttons.append(self._action_button(identity, run, {'type': 'capture', 'unit_id': actor['unit_id'], 'target_id': target['unit_id']}, '尝试捕获'))
        buttons.append(self._action_button(identity, run, {'type': 'skip', 'unit_id': actor['unit_id']}, '防守并跳过'))
        return GameResponse(message, tuple(buttons[:5]))

    def _action_button(self, identity: Identity, run: dict[str, Any], action: dict[str, Any], label: str) -> ButtonSpec:
        action_id = self.repository.issue_action(identity, 'solo_action', {'run_id': run['run_id'], 'expected_version': run['version'], 'action': action})
        command = self._command_for_action(run, action)
        return ButtonSpec(label, action_id, input_command=command)

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

    def _progress_text(self, settlement: Mapping[str, Any] | None) -> str:
        if not settlement or settlement.get('duplicate'):
            return ''
        lines = []
        for domain_id in settlement.get('unlocked_domains', []):
            name = str(domain_id)
            for entry in self.registry.entries.values():
                if entry.content_type != 'domains':
                    continue
                if not entry.data.get('id') == domain_id:
                    continue
                name = str(entry.data.get('name', domain_id))
                break
            lines.append(f'🔓 解锁新章节：【{name}】')
        ending = settlement.get('ending')
        if isinstance(ending, Mapping):
            lines.append(f'\n【{ending.get('title', '结局')}】\n{ending.get('text', '')}')
        if lines:
            return '\n' + '\n'.join(lines)
        return ''

    def _ghost_entry_for_unit(self, state: Mapping[str, Any], unit: Mapping[str, Any]):
        return self.registry.get(
            'ghosts',
            str(unit.get('ghost_id', '')),
            str(unit.get('ghost_pack_id') or state['pack_id']),
            str(unit.get('ghost_pack_version') or state['pack_version']),
        )

    def _ghost_name(self, pack_id: str, version: str, ghost_id: str) -> str:
        entry = self.registry.get('ghosts', ghost_id, pack_id, version)
        if entry:
            return entry.data.get('name', ghost_id)
        return ghost_id

    @staticmethod
    def _unit(state: Mapping[str, Any], unit_id: str) -> dict[str, Any] | None:
        return next((unit for unit in state.get('ghost_units', []) if unit.get('unit_id') == unit_id), None)

    def abandon_button(self, identity: Identity, run: dict[str, Any]) -> ButtonSpec:
        action_id = self.repository.issue_action(identity, 'confirm_abandon_solo', {'run_id': run['run_id'], 'expected_version': run['version']})
        return ButtonSpec('确认放弃本局', action_id)

    def confirm_abandon(self, identity: Identity, action_id: str) -> ActionResult:
        def abandon_in(connection: sqlite3.Connection, payload: dict[str, Any]) -> ActionResult:
            run = self.repository.run_snapshot_in(connection, str(payload.get('run_id', '')))
            profile = self.repository.profile_in(connection, identity)
            if not run or not profile:
                return ActionResult(False, '副本或个人档案不存在。')
            if run['adapter_id'] != identity.adapter_id or run['group_id'] != identity.group_id:
                return ActionResult(False, '只能在发起副本的群聊中操作。')
            if run['version'] != payload.get('expected_version'):
                return ActionResult(False, '副本状态已变化，请重新查看后再操作。')
            if run['kind'] != 'solo' or run['leader_profile_id'] != profile['profile_id']:
                return ActionResult(False, '只有本局单人副本的所有者可以放弃。')
            if run['status'] not in {'lobby', 'active'}:
                return ActionResult(False, '副本已经结束。')
            state = run['state']
            domain = self.registry.get('domains', run['domain_id'], run['pack_id'], run['pack_version'])
            if domain:
                campaign_payload = dict(profile['payload'])
                settle_profile(campaign_payload, state, profile['profile_id'], domain.data, 'abandoned', run['run_id'], True)
                self.repository.save_profile_payload_in(connection, profile['profile_id'], campaign_payload)
            connection.execute("UPDATE runs SET status = 'abandoned', state_json = ?, version = version + 1, updated_at = strftime('%s','now') WHERE run_id = ?", (self.repository._dump(state), run['run_id']))
            connection.execute('UPDATE run_members SET is_active = 0 WHERE run_id = ?', (run['run_id'],))
            return ActionResult(True, '已放弃本局；已发现规律写入图鉴，临时捕获与通关奖励不转入档案。')

        return self.repository.perform_action(identity, action_id, 'confirm_abandon_solo', abandon_in)

    def _battle_status_text(self, run: dict[str, Any]) -> str:
        state = run['state']
        battle = state.get('battle', {})
        actor_id = battle.get('current_actor_id')
        actor = self._unit(state, str(actor_id)) if actor_id and not str(actor_id).startswith('player:') else None
        player = state.get('players', {}).get(run['leader_profile_id'], {})
        enemies = [unit for unit in state.get('ghost_units', []) if unit.get('side') == 'enemy' if int(unit.get('hp', 0)) > 0]
        enemy_text = '、'.join((f'{unit['name']} {unit['hp']}/{unit['max_hp']} HP' for unit in enemies)) or '无'
        turn = '绝境选择' if str(actor_id).startswith('player:') else f'轮到【{actor['name']}】（忠诚度 {actor.get('loyalty', 0):.0f}）' if actor else '战斗正在结算'
        owned = [unit for unit in state.get('ghost_units', []) if unit.get('side') == 'friendly' if unit.get('owner_profile_id') == run['leader_profile_id']]
        top = max(owned, key=(lambda unit: int(unit.get('recovery', 0))), default={})
        recovery = '已死机' if top.get('deadlocked') else f'{int(top.get('recovery', 0))}/100'
        return f'\n{turn}。你 HP {player.get('hp', 0)}/{player.get('max_hp', 0)}、SAN {player.get('san', 0)}/{player.get('max_san', 0)}、最高复苏 {recovery}；敌方：{enemy_text}。'

    @staticmethod
    def _unit(state: dict[str, Any], unit_id: str) -> dict[str, Any] | None:
        return next((unit for unit in state.get('ghost_units', []) if unit.get('unit_id') == unit_id), None)


