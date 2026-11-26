from __future__ import annotations
import json
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from ..engine.models import ActionResult, Identity
from .profile_migrations import (
    CURRENT_PROFILE_SCHEMA_VERSION,
    ProfileMigrationError,
    migrate_profile_payload,
)
_SCHEMA_VERSION = 1
_SCHEMA = "\nCREATE TABLE IF NOT EXISTS profiles (\n    profile_id TEXT PRIMARY KEY,\n    adapter_id TEXT NOT NULL,\n    adapter_name TEXT NOT NULL,\n    user_id TEXT NOT NULL,\n    payload_json TEXT NOT NULL,\n    created_at REAL NOT NULL,\n    updated_at REAL NOT NULL,\n    UNIQUE (adapter_id, user_id)\n);\nCREATE TABLE IF NOT EXISTS runs (\n    run_id TEXT PRIMARY KEY,\n    invite_code TEXT NOT NULL UNIQUE,\n    kind TEXT NOT NULL CHECK (kind IN ('solo', 'party')),\n    domain_id TEXT NOT NULL,\n    pack_id TEXT NOT NULL,\n    pack_version TEXT NOT NULL,\n    adapter_id TEXT NOT NULL,\n    group_id TEXT NOT NULL,\n    leader_profile_id TEXT NOT NULL REFERENCES profiles(profile_id),\n    status TEXT NOT NULL CHECK (status IN ('lobby', 'active', 'abandoned', 'ended')),\n    version INTEGER NOT NULL DEFAULT 1,\n    state_json TEXT NOT NULL,\n    created_at REAL NOT NULL,\n    updated_at REAL NOT NULL\n);\nCREATE TABLE IF NOT EXISTS run_members (\n    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,\n    profile_id TEXT NOT NULL REFERENCES profiles(profile_id),\n    adapter_id TEXT NOT NULL,\n    user_id TEXT NOT NULL,\n    role TEXT NOT NULL CHECK (role IN ('leader', 'member')),\n    is_ready INTEGER NOT NULL DEFAULT 0 CHECK (is_ready IN (0, 1)),\n    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),\n    joined_at REAL NOT NULL,\n    PRIMARY KEY (run_id, profile_id)\n);\nCREATE UNIQUE INDEX IF NOT EXISTS one_active_run_per_user\n    ON run_members(adapter_id, user_id) WHERE is_active = 1;\nCREATE INDEX IF NOT EXISTS run_members_by_user\n    ON run_members(adapter_id, user_id, is_active);\nCREATE INDEX IF NOT EXISTS runs_by_group_status\n    ON runs(group_id, status, created_at);\nCREATE TABLE IF NOT EXISTS pending_actions (\n    action_id TEXT PRIMARY KEY,\n    adapter_id TEXT NOT NULL,\n    user_id TEXT NOT NULL,\n    group_id TEXT NOT NULL,\n    action TEXT NOT NULL,\n    payload_json TEXT NOT NULL,\n    expires_at REAL NOT NULL,\n    created_at REAL NOT NULL\n);\nCREATE TABLE IF NOT EXISTS action_receipts (\n    action_id TEXT PRIMARY KEY,\n    adapter_id TEXT NOT NULL,\n    user_id TEXT NOT NULL,\n    group_id TEXT NOT NULL,\n    result_json TEXT NOT NULL,\n    created_at REAL NOT NULL\n);\n"
ActionHandler = Callable[[sqlite3.Connection, dict[str, Any]], ActionResult]

class RepositoryError(RuntimeError):
    pass

class GameRepository:
    '''Small transactional SQLite repository for profile and run routing.'''

    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys = ON')
        connection.execute('PRAGMA busy_timeout = 5000')
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute('BEGIN IMMEDIATE')
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            version = int(connection.execute('PRAGMA user_version').fetchone()[0])
            if version > _SCHEMA_VERSION:
                raise RepositoryError(f'database schema {version} is newer than supported {_SCHEMA_VERSION}')
            connection.executescript(_SCHEMA)
            connection.execute(f'PRAGMA user_version = {_SCHEMA_VERSION}')
        finally:
            connection.close()

    def ensure_profile(self, identity: Identity) -> tuple[dict[str, Any], bool]:
        with self.transaction() as connection:
            return self.ensure_profile_in(connection, identity)

    def ensure_profile_in(self, connection: sqlite3.Connection, identity: Identity) -> tuple[dict[str, Any], bool]:
        row = connection.execute('SELECT * FROM profiles WHERE adapter_id = ? AND user_id = ?', (identity.adapter_id, identity.user_id)).fetchone()
        if row:
            return self._profile_dict(row), False
        now = time.time()
        profile_id = uuid.uuid4().hex
        payload = {'recruitment_status': 'pending', 'owned_ghost_ids': [], 'discovered_rules': []}
        connection.execute('INSERT INTO profiles(profile_id, adapter_id, adapter_name, user_id, payload_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)', (profile_id, identity.adapter_id, identity.adapter_name, identity.user_id, self._dump(payload), now, now))
        row = connection.execute('SELECT * FROM profiles WHERE profile_id = ?', (profile_id,)).fetchone()
        return self._profile_dict(row), True

    def migrate_profiles(self) -> dict[str, Any]:
        """Explicitly back up and migrate all profiles; never called at startup."""
        connection = self._connect()
        try:
            connection.execute('BEGIN IMMEDIATE')
            rows = connection.execute(
                'SELECT profile_id, payload_json FROM profiles ORDER BY profile_id'
            ).fetchall()
            migrations: list[tuple[str, str]] = []
            for row in rows:
                profile_id = str(row['profile_id'])
                try:
                    payload = json.loads(row['payload_json'])
                    migrated = migrate_profile_payload(payload)
                except (json.JSONDecodeError, ProfileMigrationError) as exc:
                    raise RepositoryError(
                        f'档案 {profile_id} 无法安全迁移；未修改任何档案。'
                    ) from exc
                if migrated != payload:
                    migrations.append((profile_id, self._dump(migrated)))

            if not migrations:
                connection.commit()
                return {'migrated_profiles': 0, 'backup_path': None}

            backup_path = self._backup_database_in()
            now = time.time()
            for profile_id, payload_json in migrations:
                cursor = connection.execute(
                    'UPDATE profiles SET payload_json = ?, updated_at = ? WHERE profile_id = ?',
                    (payload_json, now, profile_id),
                )
                if cursor.rowcount != 1:
                    raise RepositoryError(f'档案 {profile_id} 在迁移期间消失；事务已回滚。')
            connection.commit()
            return {
                'migrated_profiles': len(migrations),
                'backup_path': str(backup_path),
            }
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _backup_database_in(self) -> Path:
        backup_path = self.database_path.with_name(
            f'{self.database_path.stem}.pre-profile-v{CURRENT_PROFILE_SCHEMA_VERSION}-{uuid.uuid4().hex}.sqlite3'
        )
        if backup_path.exists():
            raise RepositoryError('无法创建唯一的档案迁移备份路径。')
        source = sqlite3.connect(self.database_path, timeout=5, isolation_level=None)
        backup = sqlite3.connect(backup_path, timeout=5, isolation_level=None)
        try:
            source.backup(backup)
            integrity = backup.execute('PRAGMA integrity_check').fetchone()
            source_count = source.execute('SELECT COUNT(*) FROM profiles').fetchone()[0]
            backup_count = backup.execute('SELECT COUNT(*) FROM profiles').fetchone()[0]
            if not integrity or integrity[0] != 'ok' or source_count != backup_count:
                raise RepositoryError(f'档案迁移备份校验失败：{backup_path}')
            backup.commit()
        except Exception as exc:
            raise RepositoryError(
                f'无法创建有效的档案迁移备份：{backup_path}；尚未修改档案。'
            ) from exc
        finally:
            backup.close()
            source.close()
        return backup_path

    def get_profile(self, identity: Identity) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute('SELECT * FROM profiles WHERE adapter_id = ? AND user_id = ?', (identity.adapter_id, identity.user_id)).fetchone()
        finally:
            connection.close()
        return self._profile_dict(row) if row else None

    def get_active_run(self, identity: Identity) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            return self.active_run_in(connection, identity)
        finally:
            connection.close()

    def active_run_in(self, connection: sqlite3.Connection, identity: Identity) -> dict[str, Any] | None:
        row = connection.execute("SELECT r.run_id FROM runs r JOIN run_members m ON m.run_id = r.run_id WHERE m.adapter_id = ? AND m.user_id = ? AND m.is_active = 1 AND r.status IN ('lobby', 'active') LIMIT 1", (identity.adapter_id, identity.user_id)).fetchone()
        if row:
            return self.run_snapshot_in(connection, row['run_id'])

    def run_snapshot(self, run_id: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            return self.run_snapshot_in(connection, run_id)
        finally:
            connection.close()

    def run_snapshot_in(self, connection: sqlite3.Connection, run_id: str) -> dict[str, Any] | None:
        run = connection.execute('SELECT * FROM runs WHERE run_id = ?', (run_id,)).fetchone()
        if not run:
            return
        members = connection.execute('SELECT profile_id, user_id, role, is_ready, is_active FROM run_members WHERE run_id = ? ORDER BY joined_at, user_id', (run_id,)).fetchall()
        active_members = [dict(member) for member in members if member['is_active']]
        return {'run_id': run['run_id'], 'invite_code': run['invite_code'], 'kind': run['kind'], 'domain_id': run['domain_id'], 'pack_id': run['pack_id'], 'pack_version': run['pack_version'], 'adapter_id': run['adapter_id'], 'group_id': run['group_id'], 'leader_profile_id': run['leader_profile_id'], 'status': run['status'], 'version': run['version'], 'member_count': len(active_members), 'ready_count': sum((member['is_ready'] for member in active_members)), 'members': active_members, 'state': json.loads(run['state_json'])}

    def find_group_lobbies(self, group_id: str, adapter_id: str, invite_code: str='') -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            query = "SELECT run_id FROM runs WHERE group_id = ? AND adapter_id = ? AND kind = 'party' AND status = 'lobby'"
            parameters = group_id, adapter_id
            if invite_code:
                query += ' AND invite_code = ?'
                parameters += (invite_code.strip().upper(),)
            query += ' ORDER BY created_at'
            rows = connection.execute(query, parameters).fetchall()
            return [snapshot for row in rows if (snapshot := self.run_snapshot_in(connection, row['run_id'])) is not None]
        finally:
            connection.close()

    def issue_action(self, identity: Identity, action: str, payload: dict[str, Any], ttl_seconds: int=86400, action_id: str | None=None) -> str:
        private_solo_actions = {'select_solo', 'solo_action', 'confirm_abandon_solo', 'shop_purchase'}
        if not identity.group_id:
            if action not in private_solo_actions:
                raise RepositoryError('this game action requires a group context')
        action_id = action_id or uuid.uuid4().hex
        now = time.time()
        stored_payload = dict(payload)
        stored_payload['_expected_identity'] = {'adapter_id': identity.adapter_id, 'user_id': identity.user_id, 'group_id': identity.group_id}
        with self.transaction() as connection:
            connection.execute('INSERT INTO pending_actions(action_id, adapter_id, user_id, group_id, action, payload_json, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(action_id) DO NOTHING', (action_id, identity.adapter_id, identity.user_id, identity.group_id, action, self._dump(stored_payload), now + max(1, ttl_seconds), now))
        return action_id

    def lookup_action(self, identity: Identity, action_id: str) -> dict[str, Any] | ActionResult:
        connection = self._connect()
        try:
            receipt = connection.execute('SELECT adapter_id, user_id, group_id, result_json FROM action_receipts WHERE action_id = ?', (action_id,)).fetchone()
            if receipt:
                if (receipt['adapter_id'], receipt['user_id'], receipt['group_id']) != (identity.adapter_id, identity.user_id, identity.group_id):
                    return ActionResult(False, '这个按钮不属于你或当前群聊。')
                result = json.loads(receipt['result_json'])
                return ActionResult(bool(result['ok']), result['message'], result.get('payload', {}), True)

            pending = connection.execute('SELECT * FROM pending_actions WHERE action_id = ?', (action_id,)).fetchone()
            if not pending:
                return ActionResult(False, '按钮已失效，请使用文字指令重新操作。')
            if (pending['adapter_id'], pending['user_id'], pending['group_id']) != (identity.adapter_id, identity.user_id, identity.group_id):
                return ActionResult(False, '这个按钮不属于你或当前群聊。')
            if pending['expires_at'] < time.time():
                return ActionResult(False, '按钮已过期，请使用文字指令重新操作。')
            payload = json.loads(pending['payload_json'])
            if payload.pop('_expected_identity', None) != {'adapter_id': identity.adapter_id, 'user_id': identity.user_id, 'group_id': identity.group_id}:
                return ActionResult(False, '按钮上下文不匹配，请使用文字指令重新操作。')
            return {'action': pending['action'], 'payload': payload}
        finally:
            connection.close()

    def perform_action(self, identity: Identity, action_id: str, expected_action: str, handler: ActionHandler) -> ActionResult:
        with self.transaction() as connection:
            receipt = connection.execute('SELECT adapter_id, user_id, group_id, result_json FROM action_receipts WHERE action_id = ?', (action_id,)).fetchone()
            if receipt:
                if (receipt['adapter_id'], receipt['user_id'], receipt['group_id']) != (identity.adapter_id, identity.user_id, identity.group_id):
                    return ActionResult(False, '这个按钮不属于你或当前群聊。')
                result = json.loads(receipt['result_json'])
                return ActionResult(bool(result['ok']), result['message'], result.get('payload', {}), True)
            pending = connection.execute('SELECT * FROM pending_actions WHERE action_id = ?', (action_id,)).fetchone()
            if not pending:
                return ActionResult(False, '按钮已失效，请使用文字指令重新操作。')
            if (pending['adapter_id'], pending['user_id'], pending['group_id']) != (identity.adapter_id, identity.user_id, identity.group_id):
                return ActionResult(False, '这个按钮不属于你或当前群聊。')
            if pending['expires_at'] < time.time():
                connection.execute('DELETE FROM pending_actions WHERE action_id = ?', (action_id,))
                return ActionResult(False, '按钮已过期，请使用文字指令重新操作。')
            if pending['action'] != expected_action:
                return ActionResult(False, '按钮动作不匹配。')
            payload = json.loads(pending['payload_json'])
            if payload.pop('_expected_identity', None) != {'adapter_id': identity.adapter_id, 'user_id': identity.user_id, 'group_id': identity.group_id}:
                return ActionResult(False, '按钮上下文不匹配，请使用文字指令重新操作。')
            connection.execute('SAVEPOINT action_mutation')
            try:
                result = handler(connection, payload)
            except RepositoryError as error:
                connection.execute('ROLLBACK TO action_mutation')
                connection.execute('RELEASE action_mutation')
                return ActionResult(False, str(error))
            except sqlite3.IntegrityError:
                connection.execute('ROLLBACK TO action_mutation')
                connection.execute('RELEASE action_mutation')
                return ActionResult(False, '状态已变化，请重新查看副本状态后操作。')
            if not result.ok:
                connection.execute('ROLLBACK TO action_mutation')
                connection.execute('RELEASE action_mutation')
                return result
            connection.execute('RELEASE action_mutation')
            connection.execute('DELETE FROM pending_actions WHERE action_id = ?', (action_id,))
            connection.execute('INSERT INTO action_receipts(action_id, adapter_id, user_id, group_id, result_json, created_at) VALUES (?, ?, ?, ?, ?, ?)', (action_id, identity.adapter_id, identity.user_id, identity.group_id, self._dump({'ok': result.ok, 'message': result.message, 'payload': result.payload}), time.time()))
            return result

    def create_run_in(self, connection: sqlite3.Connection, identity: Identity, profile_id: str, kind: str, domain_id: str, pack_id: str, pack_version: str, status: str, initial_state: dict[str, Any] | None=None) -> dict[str, Any]:
        now = time.time()
        run_id = uuid.uuid4().hex
        invite_code = uuid.uuid4().hex[:8].upper()
        connection.execute('INSERT INTO runs(run_id, invite_code, kind, domain_id, pack_id, pack_version, adapter_id, group_id, leader_profile_id, status, version, state_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)', (run_id, invite_code, kind, domain_id, pack_id, pack_version, identity.adapter_id, identity.group_id, profile_id, status, self._dump(initial_state or {}), now, now))
        connection.execute("INSERT INTO run_members(run_id, profile_id, adapter_id, user_id, role, is_ready, is_active, joined_at) VALUES (?, ?, ?, ?, 'leader', 0, 1, ?)", (run_id, profile_id, identity.adapter_id, identity.user_id, now))
        return self.run_snapshot_in(connection, run_id) or {}

    def save_run_state_in(self, connection: sqlite3.Connection, run_id: str, expected_version: int, state: dict[str, Any], status: str | None=None) -> int:
        if status is not None and status not in {'ended', 'lobby', 'active', 'abandoned'}:
            raise RepositoryError(f'invalid run status {status!r}')
        assignments = 'state_json = ?, version = version + 1, updated_at = ?'
        parameters = [self._dump(state), time.time()]
        if status is not None:
            assignments += ', status = ?'
            parameters.append(status)
        parameters.extend((run_id, expected_version))
        cursor = connection.execute(f'UPDATE runs SET {assignments} WHERE run_id = ? AND version = ?', parameters)
        if cursor.rowcount != 1:
            raise RepositoryError('副本状态已变化，请重新查看后再操作。')
        return expected_version + 1

    @staticmethod
    def save_profile_payload_in(connection: sqlite3.Connection, profile_id: str, payload: dict[str, Any]) -> None:
        cursor = connection.execute('UPDATE profiles SET payload_json = ?, updated_at = ? WHERE profile_id = ?', (GameRepository._dump(payload), time.time(), profile_id))
        if cursor.rowcount != 1:
            raise RepositoryError('个人档案不存在，无法保存进度。')

    @staticmethod
    def profile_in(connection: sqlite3.Connection, identity: Identity) -> dict[str, Any] | None:
        row = connection.execute('SELECT * FROM profiles WHERE adapter_id = ? AND user_id = ?', (identity.adapter_id, identity.user_id)).fetchone()
        if not row:
            return
        return {'profile_id': row['profile_id'], 'adapter_id': row['adapter_id'], 'adapter_name': row['adapter_name'], 'user_id': row['user_id'], 'payload': json.loads(row['payload_json']), 'created_at': row['created_at']}

    @staticmethod
    def _profile_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {'profile_id': row['profile_id'], 'adapter_id': row['adapter_id'], 'adapter_name': row['adapter_name'], 'user_id': row['user_id'], 'payload': json.loads(row['payload_json']), 'created_at': row['created_at']}

    @staticmethod
    def _dump(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(',', ':'))



