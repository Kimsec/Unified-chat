from __future__ import annotations

import asyncio
import time
from typing import Iterable

from unified_chat.hub import WebSocketHub
from unified_chat.models import ConnectorStatus, UnifiedMessage
from unified_chat.store import MessageStore
from unified_chat.utils import parse_datetime, utcnow


class ChatService:
    HYPE_TRAIN_END_GRACE_SEC = 5.0

    def __init__(self, store: MessageStore) -> None:
        self.store = store
        self.hub = WebSocketHub()
        self._lock = asyncio.Lock()
        self._statuses = {
            platform: ConnectorStatus(platform=platform)
            for platform in ("twitch", "youtube", "kick")
        }
        self._hype_train: dict | None = None
        self._hype_train_hide_at: float | None = None

    def clear_messages(self) -> None:
        self.store.clear_messages()

    def get_messages(self, limit: int = 200) -> list[UnifiedMessage]:
        return self.store.list_messages(limit)

    def get_statuses(self) -> list[ConnectorStatus]:
        return [self._statuses[key] for key in ("twitch", "youtube", "kick")]

    async def publish_message(self, message: UnifiedMessage) -> bool:
        inserted = self.store.add_message(message)
        if inserted:
            await self.hub.broadcast({"type": "message", "message": message.model_dump(mode="json")})
        return inserted

    def _clear_hype_train(self) -> None:
        self._hype_train = None
        self._hype_train_hide_at = None

    def _prune_hype_train(self) -> dict | None:
        if self._hype_train is None:
            return None

        if self._hype_train.get("phase") == "end":
            hide_at = self._hype_train_hide_at or 0.0
            if time.monotonic() >= hide_at:
                self._clear_hype_train()
                return None
            return self._hype_train

        expires_at = parse_datetime(self._hype_train.get("expires_at"))
        if expires_at is not None and expires_at <= utcnow():
            self._clear_hype_train()
            return None
        return self._hype_train

    def _serialize_hype_train(self, data: dict) -> dict:
        payload = dict(data)
        if payload.get("phase") == "end":
            hide_at = self._hype_train_hide_at or time.monotonic()
            remaining_ms = max(int((hide_at - time.monotonic()) * 1000), 0)
            payload["hide_after_ms"] = remaining_ms
        return payload

    def set_hype_train(self, data: dict | None) -> dict | None:
        if not data:
            self._clear_hype_train()
            return None

        self._hype_train = dict(data)
        if self._hype_train.get("phase") == "end":
            self._hype_train_hide_at = time.monotonic() + self.HYPE_TRAIN_END_GRACE_SEC
        else:
            self._hype_train_hide_at = None
        return self._prune_hype_train()

    def get_hype_train(self) -> dict | None:
        current = self._prune_hype_train()
        if current is None:
            return None
        return self._serialize_hype_train(current)

    def has_active_hype_train(self) -> bool:
        current = self._prune_hype_train()
        return bool(current and current.get("phase") != "end")

    async def broadcast_hype_train(self, data: dict) -> None:
        self.set_hype_train(data)
        payload = self.get_hype_train() or dict(data)
        await self.hub.broadcast(payload)

    async def set_status(self, platform: str, **updates) -> ConnectorStatus:
        async with self._lock:
            current = self._statuses[platform]
            merged = current.model_copy(update=updates)
            self._statuses[platform] = merged
        await self.hub.broadcast({"type": "status", "status": merged.model_dump(mode="json")})
        return merged

    async def bootstrap_event(self, limit: int = 200) -> dict:
        return {
            "type": "bootstrap",
            "messages": [message.model_dump(mode="json") for message in self.get_messages(limit)],
            "statuses": [status.model_dump(mode="json") for status in self.get_statuses()],
            "hype_train": self.get_hype_train(),
        }

    def overall_state(self) -> str:
        statuses: Iterable[ConnectorStatus] = self.get_statuses()
        if any(status.connected for status in statuses):
            return "ok"
        if any(status.state not in {"starting", "disabled"} for status in self.get_statuses()):
            return "degraded"
        return "starting"
