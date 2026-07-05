from __future__ import annotations

import asyncio
import logging

from unified_chat.config import Settings
from unified_chat.service import ChatService


class BaseConnector:
    platform = "base"

    def __init__(self, settings: Settings, service: ChatService) -> None:
        self.settings = settings
        self.service = service
        self.log = logging.getLogger(f"unified_chat.{self.platform}")
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._last_transient_error: str | None = None

    def log_transient(self, message: str) -> None:
        if message != self._last_transient_error:
            self._last_transient_error = message
            self.log.warning("%s", message)
        else:
            self.log.debug("%s", message)

    def clear_transient(self) -> None:
        if self._last_transient_error is not None:
            self.log.info("Recovered after: %s", self._last_transient_error)
        self._last_transient_error = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run(), name=f"{self.platform}-connector")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def set_status(self, **updates):
        return await self.service.set_status(self.platform, **updates)

    async def sleep_or_stop(self, seconds: float) -> bool:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return False

    async def run(self) -> None:
        raise NotImplementedError
