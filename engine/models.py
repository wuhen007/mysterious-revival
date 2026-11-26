from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any

@dataclass(frozen=True)
class Identity:
    adapter_id: 'str'
    adapter_name: 'str'
    user_id: 'str'
    group_id: 'str'
    message_id: 'str' = ''

@dataclass(frozen=True)
class ButtonSpec:
    label: 'str'
    action_id: 'str'
    callback_id: 'str' = 'astrbot_plugin_ghost_domain:action'
    input_command: 'str' = ''
    @property
    def data(self) -> dict[str, str]:
        command = self.input_command or f'/鬼域 确认 {self.action_id}'
        return {'action_id': self.action_id, 'cmd': command}


@dataclass(frozen=True)
class GameResponse:
    text: 'str'
    buttons: 'tuple[ButtonSpec, ...]' = ()

@dataclass(frozen=True)
class ActionResult:
    ok: 'bool'
    message: 'str'
    payload: 'dict[str, Any]' = field(default_factory=dict)
    replayed: 'bool' = False

@dataclass(frozen=True)
class ContentEntry:
    content_type: 'str'
    pack_id: 'str'
    version: 'str'
    data: 'dict[str, Any]'

@dataclass(frozen=True)
class ValidationIssue:
    file: 'str'
    path: 'str'
    message: 'str'
    def __str__(self) -> str:
        return f'{self.file}: {self.path}: {self.message}'


