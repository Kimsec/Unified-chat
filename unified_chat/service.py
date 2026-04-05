from __future__ import annotations

import asyncio
from typing import Iterable

from unified_chat.hub import WebSocketHub
from unified_chat.models import ConnectorStatus, UnifiedMessage
from unified_chat.store import MessageStore


class ChatService:
    def __init__(self, store: MessageStore) -> None:
        self.store = store
        self.hub = WebSocketHub()
        self._lock = asyncio.Lock()
        self._statuses = {
            platform: ConnectorStatus(platform=platform)
            for platform in ("twitch", "youtube", "kick")
        }

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
        }

    def overall_state(self) -> str:
        statuses: Iterable[ConnectorStatus] = self.get_statuses()
        if any(status.connected for status in statuses):
            return "ok"
        if any(status.state not in {"starting", "disabled"} for status in self.get_statuses()):
            return "degraded"
        return "starting"
