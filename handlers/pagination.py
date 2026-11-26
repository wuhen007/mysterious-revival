from __future__ import annotations

from dataclasses import dataclass
from time import monotonic
from typing import Callable

from ..engine.models import ButtonSpec, GameResponse, Identity

PAGE_CHARS = 1400
PENDING_TTL_SECONDS = 300
_NO_MORE = "没有待继续的长消息，或分页已过期；请重新发送原指令。"


def split_message(text: str, page_chars: int = PAGE_CHARS) -> tuple[str, ...]:
    """Split text losslessly, preferring newline boundaries near the page limit."""
    if page_chars < 1:
        raise ValueError("page_chars must be positive")
    content = str(text or "")
    if len(content) <= page_chars:
        return (content,)

    pages: list[str] = []
    start = 0
    while len(content) - start > page_chars:
        end = start + page_chars
        newline = content.rfind("\n", start, end)
        if newline >= start + page_chars // 2:
            end = newline + 1
        pages.append(content[start:end])
        start = end
    pages.append(content[start:])
    return tuple(pages)


@dataclass
class _PendingPages:
    pages: tuple[str, ...]
    buttons: tuple[ButtonSpec, ...]
    next_index: int
    expires_at: float


class MessagePager:
    """Short-lived response pages isolated by adapter, sender, and group."""

    def __init__(
        self,
        page_chars: int = PAGE_CHARS,
        ttl_seconds: int = PENDING_TTL_SECONDS,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if page_chars < 1:
            raise ValueError("page_chars must be positive")
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be positive")
        self.page_chars = page_chars
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._pending: dict[tuple[str, str, str], _PendingPages] = {}

    @staticmethod
    def _key(identity: Identity) -> tuple[str, str, str]:
        return (identity.adapter_id, identity.user_id, identity.group_id)

    def _prune(self, now: float) -> None:
        for key, pending in tuple(self._pending.items()):
            if pending.expires_at <= now:
                self._pending.pop(key, None)

    @staticmethod
    def _page_response(pending: _PendingPages, page_index: int) -> GameResponse:
        total = len(pending.pages)
        text = f"{pending.pages[page_index]}\n\n（第 {page_index + 1}/{total} 页）"
        if page_index + 1 < total:
            buttons = (
                ButtonSpec(
                    "下一页",
                    f"prompt:message-page:{page_index + 2}",
                    input_command="/鬼域 更多",
                ),
            )
        else:
            buttons = pending.buttons
        return GameResponse(text, buttons)

    def start(self, identity: Identity, response: GameResponse) -> GameResponse:
        now = self._clock()
        self._prune(now)
        key = self._key(identity)
        self._pending.pop(key, None)
        pages = split_message(response.text, self.page_chars)
        if len(pages) == 1:
            return response
        pending = _PendingPages(
            pages=pages,
            buttons=response.buttons,
            next_index=1,
            expires_at=now + self.ttl_seconds,
        )
        self._pending[key] = pending
        return self._page_response(pending, 0)

    def next(self, identity: Identity) -> GameResponse:
        now = self._clock()
        self._prune(now)
        key = self._key(identity)
        pending = self._pending.get(key)
        if pending is None:
            return GameResponse(_NO_MORE)

        page_index = pending.next_index
        pending.next_index += 1
        response = self._page_response(pending, page_index)
        if page_index + 1 >= len(pending.pages):
            self._pending.pop(key, None)
        return response
