from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class Badge(BaseModel):
    text: str
    type: str
    count: int | None = None


class Emote(BaseModel):
    id: str
    text: str
    begin: int
    end: int


class UnifiedMessage(BaseModel):
    id: str
    platform: Literal["twitch", "youtube", "kick"]
    platform_message_id: str
    message_kind: Literal["chat", "system"] = "chat"
    notice_type: str | None = None
    channel_id: str | None = None
    author_display_name: str
    author_login: str | None = None
    author_color: str | None = None
    avatar_url: str | None = None
    badges: list[Badge] = Field(default_factory=list)
    emotes: list[Emote] = Field(default_factory=list)
    text: str
    sent_at: datetime
    deleted_at: datetime | None = None
    raw_payload: dict[str, Any] = Field(default_factory=dict)


class ConnectorStatus(BaseModel):
    platform: Literal["twitch", "youtube", "kick"]
    state: str = "starting"
    connected: bool = False
    auth_ready: bool = False
    detail: str = ""
    last_event_at: datetime | None = None
    last_error_at: datetime | None = None
    last_error: str | None = None


class ReplyRequest(BaseModel):
    message: str
