from __future__ import annotations

import asyncio
import json
import secrets
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal
from urllib.parse import urlencode

import aiohttp

from unified_chat.connectors.base import BaseConnector
from unified_chat.models import Badge, Emote, UnifiedMessage
from unified_chat.oauth_pending import PendingOAuthStore
from unified_chat.utils import make_message_key, parse_datetime, utcnow

_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=30)


class TwitchDisconnect(RuntimeError):
    """Expected EventSub drop (keepalive timeout / socket closed) — reconnect without a traceback."""


@dataclass(slots=True)
class SubscribeResult:
    outcome: Literal["ok", "auth_failed", "rate_limited", "retryable_error", "fatal_error"]
    detail: str = ""
    retry_at: float | None = None


class TwitchConnector(BaseConnector):
    platform = "twitch"
    CHAT_MESSAGE_SUBSCRIPTION = "channel.chat.message"
    CHAT_MESSAGE_DELETE_SUBSCRIPTION = "channel.chat.message_delete"
    CHAT_NOTIFICATION_SUBSCRIPTION = "channel.chat.notification"
    HYPE_TRAIN_BEGIN = "channel.hype_train.begin"
    HYPE_TRAIN_PROGRESS = "channel.hype_train.progress"
    HYPE_TRAIN_END = "channel.hype_train.end"
    HYPE_TRAIN_TYPES = frozenset({HYPE_TRAIN_BEGIN, HYPE_TRAIN_PROGRESS, HYPE_TRAIN_END})
    CHANNEL_POINTS_REDEMPTION_SUBSCRIPTION = "channel.channel_points_custom_reward_redemption.add"
    SUBSCRIBE_URL = "https://api.twitch.tv/helix/eventsub/subscriptions"
    CHAT_URL = "https://api.twitch.tv/helix/chat/messages"
    BAN_URL = "https://api.twitch.tv/helix/moderation/bans"
    USERS_URL = "https://api.twitch.tv/helix/users"
    HYPE_TRAIN_STATUS_URL = "https://api.twitch.tv/helix/hypetrain/status"
    AUTHORIZE_URL = "https://id.twitch.tv/oauth2/authorize"
    TOKEN_URL = "https://id.twitch.tv/oauth2/token"

    def __init__(self, settings, service) -> None:
        super().__init__(settings, service)
        self._source_broadcaster_cache: dict[str, dict[str, str | None]] = {}
        # Only used in managed mode; harmless to create either way.
        self._pending_oauth = PendingOAuthStore(
            self.settings.twitch_tokens_path.with_name("twitch_oauth_pending.json")
        )

    def _configured(self) -> bool:
        return bool(
            self.settings.twitch_client_id
            and self.settings.twitch_broadcaster_id
            and self.settings.twitch_tokens_path
        )

    def _load_access_token(self) -> str:
        try:
            with open(self.settings.twitch_tokens_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except FileNotFoundError:
            self.log.warning("Twitch token file not found: %s", self.settings.twitch_tokens_path)
            return ""
        except json.JSONDecodeError:
            self.log.warning("Twitch token file is not valid JSON: %s", self.settings.twitch_tokens_path)
            return ""
        return (
            str(payload.get("access") or "")
            or str(payload.get("access_token") or "")
            or str(payload.get("token") or "")
        )


    def _manages_token(self) -> bool:
        return self.settings.twitch_manages_token

    def _save_token_file(self, payload: dict[str, Any]) -> None:
        tmp_path = self.settings.twitch_tokens_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp_path.replace(self.settings.twitch_tokens_path)

    def get_authorization_url(self) -> str:
        if not self._manages_token():
            raise RuntimeError(
                "Twitch is read-only here (token managed by stream-control). "
                "Set TWITCH_CLIENT_SECRET and point TWITCH_TOKENS_PATH at data/ to enable."
            )
        if not self.settings.twitch_scopes:
            raise RuntimeError("TWITCH_SCOPES is empty — set it in .env (see .env.example)")
        state = secrets.token_urlsafe(24)
        params = {
            "client_id": self.settings.twitch_client_id,
            "redirect_uri": self.settings.twitch_redirect_uri,
            "response_type": "code",
            "scope": self.settings.twitch_scopes,
            "state": state,
            "force_verify": "true",
        }
        self._pending_oauth.save({"state": state})
        return f"{self.AUTHORIZE_URL}?{urlencode(params)}"

    async def complete_authorization(self, code: str | None, state: str | None) -> None:
        if not self._manages_token():
            raise RuntimeError("Twitch token is read-only here")
        pending = self._pending_oauth.load()
        if not pending:
            raise RuntimeError("No pending Twitch authorization found; start again")
        if not code or not state or state != pending.get("state"):
            raise RuntimeError("Invalid Twitch OAuth callback")

        data = {
            "client_id": self.settings.twitch_client_id,
            "client_secret": self.settings.twitch_client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": self.settings.twitch_redirect_uri,
        }
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            async with session.post(self.TOKEN_URL, data=data) as response:
                payload = await response.json(content_type=None)
                if response.status >= 400:
                    raise RuntimeError(f"Twitch token exchange failed {response.status}: {payload}")
        self._save_token_file(self._token_payload(payload))
        self._pending_oauth.clear()

    def _token_payload(self, data: dict[str, Any]) -> dict[str, Any]:
        return {
            "access_token": str(data.get("access_token") or ""),
            "refresh_token": str(data.get("refresh_token") or ""),
            "expires_at": (utcnow() + timedelta(seconds=int(data.get("expires_in") or 0))).isoformat(),
        }

    async def _refresh_token(self, refresh_token: str) -> dict[str, Any]:
        data = {
            "client_id": self.settings.twitch_client_id,
            "client_secret": self.settings.twitch_client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            async with session.post(self.TOKEN_URL, data=data) as response:
                payload = await response.json(content_type=None)
                if response.status >= 400:
                    raise RuntimeError(f"Twitch refresh failed {response.status}: {payload}")
        return self._token_payload(payload)

    async def _maybe_refresh_token(self) -> None:
        """In managed mode, refresh the stored access token when it's near expiry.

        No-op (and no network) when unmanaged, so the read-only path is untouched.
        """
        if not self._manages_token():
            return
        try:
            raw = self.settings.twitch_tokens_path.read_text(encoding="utf-8")
            payload = json.loads(raw)
        except (FileNotFoundError, json.JSONDecodeError):
            return
        expires_at = parse_datetime(payload.get("expires_at"))
        refresh_token = payload.get("refresh_token")
        if not refresh_token or not expires_at:
            return
        if expires_at > utcnow() + timedelta(minutes=30):
            return
        try:
            refreshed = await self._refresh_token(refresh_token)
        except Exception as exc:
            self.log_transient(f"Twitch token refresh failed: {exc}")
            return
        self._save_token_file(refreshed)

    @staticmethod
    def _rate_limit_retry_at(response: aiohttp.ClientResponse) -> float:
        now = time.time()
        reset_value = response.headers.get("Ratelimit-Reset")
        if reset_value:
            try:
                retry_at = float(reset_value)
                if retry_at > now:
                    return retry_at
            except (TypeError, ValueError):
                pass

        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return now + max(float(retry_after), 1.0)
            except (TypeError, ValueError):
                pass

        return now + 30.0

    @staticmethod
    def _coerce_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    async def _subscribe_eventsub(
        self,
        session: aiohttp.ClientSession,
        session_id: str,
        subscription_type: str,
        *,
        version: str = "1",
        condition: dict[str, str] | None = None,
    ) -> SubscribeResult:
        token = self._load_access_token()
        if not token:
            return SubscribeResult(
                "auth_failed",
                detail=f"No Twitch token available in {self.settings.twitch_tokens_path}",
            )

        if condition is None:
            condition = {
                "broadcaster_user_id": self.settings.twitch_broadcaster_id,
                "user_id": self.settings.twitch_broadcaster_id,
            }

        body = {
            "type": subscription_type,
            "version": version,
            "condition": condition,
            "transport": {"method": "websocket", "session_id": session_id},
        }

        async def _send(token_value: str) -> SubscribeResult:
            headers = {
                "Client-Id": self.settings.twitch_client_id,
                "Authorization": f"Bearer {token_value}",
                "Content-Type": "application/json",
            }
            async with session.post(
                self.SUBSCRIBE_URL,
                headers=headers,
                json=body,
                timeout=_HTTP_TIMEOUT,
            ) as response:
                if response.status in (200, 202, 409):
                    return SubscribeResult("ok")
                if response.status == 401:
                    detail = await response.text()
                    return SubscribeResult(
                        "auth_failed",
                        detail=f"Twitch subscribe failed 401: {detail[:300]}",
                    )
                if response.status == 429:
                    detail = await response.text()
                    return SubscribeResult(
                        "rate_limited",
                        detail=f"Twitch subscribe failed 429: {detail[:300]}",
                        retry_at=self._rate_limit_retry_at(response),
                    )
                detail = await response.text()
                outcome = "retryable_error" if response.status >= 500 else "fatal_error"
                return SubscribeResult(
                    outcome,
                    detail=f"Twitch subscribe failed {response.status}: {detail[:300]}",
                )

        ok = await _send(token)
        if ok.outcome == "ok":
            self.log.info("Subscribed to Twitch %s", subscription_type)
            return ok
        if ok.outcome != "auth_failed":
            if ok.outcome == "rate_limited":
                wait = max(int((ok.retry_at or time.time()) - time.time()), 1)
                self.log.warning("Twitch rate-limited for %s, waiting %ds before retry", subscription_type, wait)
            return ok

        self.log.warning("Twitch subscribe returned 401 for %s, reloading token", subscription_type)
        fresh_token = self._load_access_token()
        if not fresh_token:
            return SubscribeResult(
                "auth_failed",
                detail=f"No Twitch token available in {self.settings.twitch_tokens_path}",
            )

        retried = await _send(fresh_token)
        if retried.outcome == "ok":
            self.log.info("Subscribed to Twitch %s", subscription_type)
        return retried

    async def _subscribe_chat(self, session: aiohttp.ClientSession, session_id: str) -> SubscribeResult:
        return await self._subscribe_eventsub(session, session_id, self.CHAT_MESSAGE_SUBSCRIPTION)

    async def _subscribe_chat_notification(
        self,
        session: aiohttp.ClientSession,
        session_id: str,
    ) -> SubscribeResult:
        return await self._subscribe_eventsub(session, session_id, self.CHAT_NOTIFICATION_SUBSCRIPTION)

    async def _subscribe_chat_message_delete(
        self,
        session: aiohttp.ClientSession,
        session_id: str,
    ) -> SubscribeResult:
        return await self._subscribe_eventsub(session, session_id, self.CHAT_MESSAGE_DELETE_SUBSCRIPTION)

    def _hype_train_condition(self) -> dict[str, str]:
        return {"broadcaster_user_id": self.settings.twitch_broadcaster_id}

    async def _subscribe_hype_train_begin(self, session: aiohttp.ClientSession, session_id: str) -> SubscribeResult:
        return await self._subscribe_eventsub(session, session_id, self.HYPE_TRAIN_BEGIN, version="2", condition=self._hype_train_condition())

    async def _subscribe_hype_train_progress(self, session: aiohttp.ClientSession, session_id: str) -> SubscribeResult:
        return await self._subscribe_eventsub(session, session_id, self.HYPE_TRAIN_PROGRESS, version="2", condition=self._hype_train_condition())

    async def _subscribe_hype_train_end(self, session: aiohttp.ClientSession, session_id: str) -> SubscribeResult:
        return await self._subscribe_eventsub(session, session_id, self.HYPE_TRAIN_END, version="2", condition=self._hype_train_condition())

    async def _subscribe_channel_points_redemption(
        self,
        session: aiohttp.ClientSession,
        session_id: str,
    ) -> SubscribeResult:
        return await self._subscribe_eventsub(
            session,
            session_id,
            self.CHANNEL_POINTS_REDEMPTION_SUBSCRIPTION,
            condition={"broadcaster_user_id": self.settings.twitch_broadcaster_id},
        )

    def _normalize_hype_train_payload(
        self,
        event: dict[str, Any],
        *,
        phase: str,
    ) -> dict[str, Any] | None:
        if not isinstance(event, dict):
            return None

        hype_train_id = str(event.get("id") or "")
        if not hype_train_id:
            return None

        return {
            "type": "hype_train",
            "id": hype_train_id,
            "phase": phase,
            "level": self._coerce_int(event.get("level"), 1),
            "progress": self._coerce_int(event.get("progress"), 0),
            "goal": self._coerce_int(event.get("goal"), 0),
            "total": self._coerce_int(event.get("total"), 0),
            "train_type": str(event.get("type") or "") or None,
            "started_at": event.get("started_at"),
            "expires_at": event.get("expires_at"),
            "ended_at": event.get("ended_at"),
            "cooldown_ends_at": event.get("cooldown_ends_at"),
        }

    async def get_hype_train_status(self) -> dict[str, Any] | None:
        token = self._load_access_token()
        if not token:
            return None

        headers = {
            "Client-Id": self.settings.twitch_client_id,
            "Authorization": f"Bearer {token}",
        }

        try:
            async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
                async with session.get(
                    self.HYPE_TRAIN_STATUS_URL,
                    headers=headers,
                    params={"broadcaster_id": self.settings.twitch_broadcaster_id},
                    timeout=_HTTP_TIMEOUT,
                ) as response:
                    if response.status == 200:
                        payload = await response.json(content_type=None)
                    else:
                        detail = await response.text()
                        if response.status == 403:
                            self.log.warning(
                                "Twitch hype train status unavailable (missing channel:read:hype_train?): %s",
                                detail[:200],
                            )
                        elif response.status == 401:
                            self.log.warning("Twitch hype train status auth failed: %s", detail[:200])
                        else:
                            self.log.warning(
                                "Twitch hype train status failed %d: %s",
                                response.status,
                                detail[:200],
                            )
                        return None
        except Exception as exc:
            self.log.warning("Twitch hype train status request error: %s", exc)
            return None

        items = (payload or {}).get("data") or []
        if not items:
            return None

        item = items[0] or {}
        current = item.get("current", item)
        if current is None:
            return None

        normalized = self._normalize_hype_train_payload(current, phase="progress")
        if normalized is not None:
            self.log.info("Loaded active Twitch hype train via Helix backfill")
        return normalized

    async def _resolve_source_broadcaster(
        self,
        session: aiohttp.ClientSession,
        source_broadcaster_user_id: str | None,
    ) -> str | None:
        broadcaster_id = str(source_broadcaster_user_id or "").strip()
        if not broadcaster_id:
            return None

        cached = self._source_broadcaster_cache.get(broadcaster_id)
        if cached is not None:
            return cached.get("avatar_url")

        token = self._load_access_token()
        if not token:
            return None

        headers = {
            "Client-Id": self.settings.twitch_client_id,
            "Authorization": f"Bearer {token}",
        }
        try:
            async with session.get(
                self.USERS_URL,
                headers=headers,
                params={"id": broadcaster_id},
                timeout=_HTTP_TIMEOUT,
            ) as response:
                if response.status != 200:
                    detail = await response.text()
                    self.log.warning(
                        "Twitch Get Users failed %d for shared-chat source %s: %s",
                        response.status,
                        broadcaster_id,
                        detail[:200],
                    )
                    return None
                data = await response.json(content_type=None)
        except Exception as exc:
            self.log.warning("Twitch source broadcaster lookup error for %s: %s", broadcaster_id, exc)
            return None

        user = ((data or {}).get("data") or [None])[0] or {}
        avatar_url = str(user.get("profile_image_url") or "") or None
        self._source_broadcaster_cache[broadcaster_id] = {
            "id": broadcaster_id,
            "avatar_url": avatar_url,
        }
        return avatar_url

    async def _build_source_broadcaster(
        self,
        session: aiohttp.ClientSession,
        event: dict[str, Any],
    ) -> tuple[dict[str, Any], str | None]:
        source_broadcaster_user_id = str(event.get("source_broadcaster_user_id") or "").strip() or None
        source_broadcaster = None
        source_avatar_url = None
        if source_broadcaster_user_id:
            source_broadcaster = {
                "id": source_broadcaster_user_id,
                "login": str(event.get("source_broadcaster_user_login") or "") or None,
                "name": str(event.get("source_broadcaster_user_name") or "") or None,
            }
            source_avatar_url = await self._resolve_source_broadcaster(session, source_broadcaster_user_id)
            if source_avatar_url:
                source_broadcaster["avatar_url"] = source_avatar_url
            event = {
                **event,
                "source_broadcaster": source_broadcaster,
            }
        return event, source_avatar_url

    async def _map_message(
        self,
        session: aiohttp.ClientSession,
        metadata: dict[str, Any],
        payload: dict[str, Any],
    ) -> UnifiedMessage | None:
        event = payload.get("event") or {}
        message = event.get("message") or {}
        fragments = message.get("fragments") or []
        text = message.get("text") or "".join(fragment.get("text", "") for fragment in fragments)
        message_id = str(event.get("message_id") or "")
        if not message_id or not text.strip():
            return None

        emotes = []
        pos = 0
        for frag in fragments:
            frag_text = frag.get("text") or ""
            frag_type = frag.get("type")
            if frag_type == "emote":
                emote_data = frag.get("emote") or {}
                emote_id = emote_data.get("id")
                if emote_id:
                    emotes.append(Emote(
                        id=str(emote_id),
                        text=frag_text,
                        begin=pos,
                        end=pos + len(frag_text),
                    ))
            pos += len(frag_text)

        badges = []
        for badge in event.get("badges") or []:
            info = badge.get("info")
            badges.append(
                Badge(
                    text=str(badge.get("set_id") or badge.get("id") or "badge"),
                    type=str(badge.get("set_id") or badge.get("id") or "badge"),
                    count=int(info) if isinstance(info, str) and info.isdigit() else None,
                )
            )

        event, source_avatar_url = await self._build_source_broadcaster(session, event)

        return UnifiedMessage(
            id=make_message_key("twitch", message_id),
            platform="twitch",
            platform_message_id=message_id,
            channel_id=str(event.get("broadcaster_user_id") or self.settings.twitch_broadcaster_id),
            author_display_name=str(event.get("chatter_user_name") or event.get("chatter_user_login") or "Unknown"),
            author_login=event.get("chatter_user_login"),
            author_id=str(event.get("chatter_user_id") or "") or None,
            author_color=event.get("color"),
            avatar_url=source_avatar_url,
            badges=badges,
            emotes=emotes,
            text=text,
            sent_at=parse_datetime(metadata.get("message_timestamp")) or utcnow(),
            raw_payload={"metadata": metadata, "payload": {**payload, "event": event}},
        )

    async def _map_notification_message(
        self,
        session: aiohttp.ClientSession,
        metadata: dict[str, Any],
        payload: dict[str, Any],
    ) -> UnifiedMessage | None:
        event = payload.get("event") or {}
        message_id = str(event.get("message_id") or "")
        system_message = str(event.get("system_message") or "").strip()
        if not message_id or not system_message:
            return None

        event, source_avatar_url = await self._build_source_broadcaster(session, event)

        return UnifiedMessage(
            id=make_message_key("twitch", message_id),
            platform="twitch",
            platform_message_id=message_id,
            message_kind="system",
            notice_type=str(event.get("notice_type") or "") or None,
            channel_id=str(event.get("broadcaster_user_id") or self.settings.twitch_broadcaster_id),
            author_display_name=str(event.get("chatter_user_name") or event.get("chatter_user_login") or "Twitch"),
            author_login=event.get("chatter_user_login"),
            author_id=str(event.get("chatter_user_id") or "") or None,
            author_color=event.get("color"),
            avatar_url=source_avatar_url,
            badges=[],
            emotes=[],
            text=system_message,
            sent_at=parse_datetime(metadata.get("message_timestamp")) or utcnow(),
            raw_payload={"metadata": metadata, "payload": {**payload, "event": event}},
        )

    def _map_redemption_message(
        self,
        metadata: dict[str, Any],
        payload: dict[str, Any],
    ) -> UnifiedMessage | None:
        event = payload.get("event") or {}
        redemption_id = str(event.get("id") or "")
        if not redemption_id:
            return None

        reward = event.get("reward") or {}
        reward_title = str(reward.get("title") or "a reward").strip() or "a reward"
        reward_cost = reward.get("cost")
        user_input = (event.get("user_input") or "").strip()
        user_name = str(
            event.get("user_name") or event.get("user_login") or "Someone"
        ).strip() or "Someone"

        text = f"{user_name} redeemed {reward_title}"
        if isinstance(reward_cost, int) and reward_cost > 0:
            text += f" ({reward_cost} points)"
        if user_input:
            text += f": {user_input}"

        return UnifiedMessage(
            id=make_message_key("twitch", redemption_id),
            platform="twitch",
            platform_message_id=redemption_id,
            message_kind="system",
            notice_type="channel_points_reward",
            channel_id=str(event.get("broadcaster_user_id") or self.settings.twitch_broadcaster_id),
            author_display_name=user_name,
            author_login=event.get("user_login"),
            author_id=str(event.get("user_id") or "") or None,
            author_color=None,
            avatar_url=None,
            badges=[],
            emotes=[],
            text=text,
            sent_at=parse_datetime(event.get("redeemed_at"))
                or parse_datetime(metadata.get("message_timestamp"))
                or utcnow(),
            raw_payload={"metadata": metadata, "payload": payload},
        )

    def _map_hype_train_event(
        self,
        subscription_type: str,
        metadata: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        event = payload.get("event") or {}
        phase_map = {
            self.HYPE_TRAIN_BEGIN: "begin",
            self.HYPE_TRAIN_PROGRESS: "progress",
            self.HYPE_TRAIN_END: "end",
        }
        phase = phase_map.get(subscription_type)
        if not phase:
            return None
        return self._normalize_hype_train_payload(event, phase=phase)

    async def _handle_message_delete(
        self,
        metadata: dict[str, Any],
        payload: dict[str, Any],
    ) -> None:
        event = payload.get("event") or {}
        message_id = str(event.get("message_id") or "").strip()
        if not message_id:
            return

        deleted_at = parse_datetime(metadata.get("message_timestamp")) or utcnow()
        marked = await self.service.mark_message_deleted("twitch", message_id, deleted_at)
        if not marked:
            self.log.info("Twitch delete event for unknown message %s", message_id)

    async def get_emotes(self) -> list[dict[str, Any]]:
        token = self._load_access_token()
        if not token:
            return []
        headers = {
            "Client-Id": self.settings.twitch_client_id,
            "Authorization": f"Bearer {token}",
        }
        emotes: list[dict[str, Any]] = []
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            for label, url, params in [
                ("channel", "https://api.twitch.tv/helix/chat/emotes", {"broadcaster_id": self.settings.twitch_broadcaster_id}),
                ("global", "https://api.twitch.tv/helix/chat/emotes/global", {}),
            ]:
                try:
                    async with session.get(url, headers=headers, params=params) as resp:
                        if resp.status != 200:
                            detail = await resp.text()
                            self.log.warning("Twitch %s emotes failed %d: %s", label, resp.status, detail[:200])
                            continue
                        data = await resp.json(content_type=None)
                        for e in data.get("data") or []:
                            emotes.append({
                                "id": e.get("id"),
                                "name": e.get("name"),
                                "url": f"https://static-cdn.jtvnw.net/emoticons/v2/{e.get('id')}/default/dark/1.0",
                            })
                        self.log.info("Loaded %d %s emotes", len(data.get("data") or []), label)
                except Exception as exc:
                    self.log.warning("Twitch %s emotes error: %s", label, exc)
        return emotes

    async def send_reply(self, message_text: str) -> dict[str, Any]:
        content = (message_text or "").strip()
        if not content:
            raise ValueError("Reply message is empty")

        payload = {
            "broadcaster_id": self.settings.twitch_broadcaster_id,
            "sender_id": self.settings.twitch_broadcaster_id,
            "message": content[:500],
        }
        token = self._load_access_token()
        if not token:
            raise RuntimeError("No Twitch access token found")

        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            for attempt in range(2):
                headers = {
                    "Client-Id": self.settings.twitch_client_id,
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                }
                async with session.post(self.CHAT_URL, headers=headers, json=payload) as response:
                    try:
                        data = await response.json(content_type=None)
                    except Exception:
                        data = {"raw": await response.text()}
                    if response.status in (200, 202):
                        return data
                    if response.status == 401 and attempt == 0:
                        token = self._load_access_token()
                        continue
                    raise RuntimeError(f"Twitch send failed {response.status}: {data}")
        raise RuntimeError("Unable to send Twitch message")

    async def ban_user(
        self,
        user_id: str,
        *,
        duration: int | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        target = str(user_id or "").strip()
        if not target:
            raise ValueError("user_id is required")
        if target == self.settings.twitch_broadcaster_id:
            raise ValueError("Cannot ban the broadcaster")
        if duration is not None and not 1 <= duration <= 1209600:
            raise ValueError("duration must be between 1 and 1209600 seconds")

        data: dict[str, Any] = {"user_id": target}
        if duration is not None:
            data["duration"] = duration
        if reason:
            data["reason"] = str(reason)[:500]
        params = {
            "broadcaster_id": self.settings.twitch_broadcaster_id,
            "moderator_id": self.settings.twitch_broadcaster_id,
        }
        token = self._load_access_token()
        if not token:
            raise RuntimeError("No Twitch access token found")

        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            for attempt in range(2):
                headers = {
                    "Client-Id": self.settings.twitch_client_id,
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                }
                async with session.post(
                    self.BAN_URL,
                    headers=headers,
                    params=params,
                    json={"data": data},
                ) as response:
                    try:
                        payload = await response.json(content_type=None)
                    except Exception:
                        payload = {"raw": await response.text()}
                    if response.status in (200, 202):
                        return payload
                    if response.status == 409:
                        return {"already_banned": True}
                    if response.status == 401 and attempt == 0:
                        token = self._load_access_token()
                        continue
                    raise RuntimeError(f"Twitch ban failed {response.status}: {payload}")
        raise RuntimeError("Unable to ban Twitch user")

    async def run(self) -> None:
        if not self._configured():
            await self.set_status(
                state="disabled",
                detail="Missing TWITCH_CLIENT_ID, TWITCH_BROADCASTER_ID or TWITCH_TOKENS_PATH",
                connected=False,
                auth_ready=False,
            )
            return

        default_websocket_url = self.settings.twitch_eventsub_ws_url
        websocket_url = default_websocket_url
        backoff = 5
        while not self._stop_event.is_set():
            await self._maybe_refresh_token()
            token = self._load_access_token()
            if not token:
                detail = (
                    "No Twitch token yet; visit /auth/twitch/start to connect"
                    if self._manages_token()
                    else f"No Twitch token available in {self.settings.twitch_tokens_path}"
                )
                await self.set_status(
                    state="auth_required" if self._manages_token() else "waiting_for_token",
                    detail=detail,
                    connected=False,
                    auth_ready=False,
                )
                if await self.sleep_or_stop(5):
                    break
                websocket_url = default_websocket_url
                continue

            try:
                async with aiohttp.ClientSession() as session:
                    await self.set_status(
                        state="connecting",
                        detail="Opening Twitch EventSub connection",
                        connected=False,
                        auth_ready=True,
                        last_error=None,
                    )
                    async with session.ws_connect(websocket_url, autoping=True) as ws:
                        self.clear_transient()
                        await self.set_status(
                            state="connecting",
                            detail="Connected to Twitch EventSub, waiting for welcome",
                            connected=False,
                            auth_ready=True,
                            last_error=None,
                        )
                        requested_reconnect_url: str | None = None
                        disconnect_detail: str | None = None
                        session_id: str | None = None
                        _all_subs = (
                            (self.CHAT_MESSAGE_SUBSCRIPTION, self._subscribe_chat),
                            (self.CHAT_NOTIFICATION_SUBSCRIPTION, self._subscribe_chat_notification),
                            (self.CHAT_MESSAGE_DELETE_SUBSCRIPTION, self._subscribe_chat_message_delete),
                            (self.HYPE_TRAIN_BEGIN, self._subscribe_hype_train_begin),
                            (self.HYPE_TRAIN_PROGRESS, self._subscribe_hype_train_progress),
                            (self.HYPE_TRAIN_END, self._subscribe_hype_train_end),
                            (self.CHANNEL_POINTS_REDEMPTION_SUBSCRIPTION, self._subscribe_channel_points_redemption),
                        )
                        subscribed = {st: False for st, _ in _all_subs}
                        next_subscribe_attempt_at = {st: 0.0 for st, _ in _all_subs}
                        keepalive_timeout = 35.0

                        while not self._stop_event.is_set():
                            now = time.time()
                            await self._maybe_refresh_token()
                            if session_id:
                                for subscription_type, subscribe_func in _all_subs:
                                    if subscribed[subscription_type] or now < next_subscribe_attempt_at[subscription_type]:
                                        continue
                                    result = await subscribe_func(session, session_id)
                                    if result.outcome == "ok":
                                        subscribed[subscription_type] = True
                                        backoff = 5
                                        if subscription_type == self.CHAT_MESSAGE_SUBSCRIPTION:
                                            await self.set_status(
                                                state="connected",
                                                detail="Listening for chat messages",
                                                connected=True,
                                                auth_ready=True,
                                                last_error=None,
                                            )
                                        continue

                                    if result.outcome == "rate_limited":
                                        retry_at = result.retry_at or (time.time() + 30.0)
                                        next_subscribe_attempt_at[subscription_type] = retry_at
                                        if subscription_type == self.CHAT_MESSAGE_SUBSCRIPTION:
                                            wait = max(int(retry_at - time.time()), 1)
                                            await self.set_status(
                                                state="rate_limited",
                                                detail=f"Twitch transport limit reached; retrying in {wait}s",
                                                connected=False,
                                                auth_ready=True,
                                                last_error=result.detail or None,
                                                last_error_at=utcnow(),
                                            )
                                        else:
                                            self.log.warning(
                                                "Twitch %s rate-limited; retrying later",
                                                subscription_type,
                                            )
                                        continue

                                    if result.outcome == "auth_failed":
                                        next_subscribe_attempt_at[subscription_type] = time.time() + 5.0
                                        if subscription_type == self.CHAT_MESSAGE_SUBSCRIPTION:
                                            latest_token = self._load_access_token()
                                            has_token = bool(latest_token)
                                            if has_token:
                                                await self.set_status(
                                                    state="auth_required",
                                                    detail="Twitch token rejected; waiting for refresh",
                                                    connected=False,
                                                    auth_ready=False,
                                                    last_error=result.detail or "Twitch access token rejected",
                                                    last_error_at=utcnow(),
                                                )
                                            else:
                                                await self.set_status(
                                                    state="waiting_for_token",
                                                    detail=f"No Twitch token available in {self.settings.twitch_tokens_path}",
                                                    connected=False,
                                                    auth_ready=False,
                                                    last_error=result.detail or None,
                                                    last_error_at=utcnow(),
                                                )
                                        else:
                                            self.log.warning(
                                                "Twitch %s auth failed; waiting for token refresh",
                                                subscription_type,
                                            )
                                        continue

                                    next_subscribe_attempt_at[subscription_type] = time.time() + (
                                        15.0 if result.outcome == "retryable_error" else 30.0
                                    )
                                    if subscription_type == self.CHAT_MESSAGE_SUBSCRIPTION:
                                        await self.set_status(
                                            state="subscribing",
                                            detail="Twitch subscription not ready; staying connected and retrying",
                                            connected=False,
                                            auth_ready=True,
                                            last_error=result.detail or None,
                                            last_error_at=utcnow(),
                                        )
                                    else:
                                        self.log.warning(
                                            "Twitch %s not ready yet; staying connected and retrying",
                                            subscription_type,
                                        )

                            try:
                                packet = await asyncio.wait_for(
                                    ws.receive(),
                                    timeout=keepalive_timeout + 5.0,
                                )
                            except asyncio.TimeoutError:
                                disconnect_detail = "Twitch keepalive timed out"
                                break

                            if packet.type == aiohttp.WSMsgType.TEXT:
                                data = json.loads(packet.data)
                                metadata = data.get("metadata") or {}
                                payload = data.get("payload") or {}
                                message_type = metadata.get("message_type")

                                if message_type == "session_welcome":
                                    session_info = payload.get("session") or {}
                                    session_id = session_info.get("id")
                                    keepalive_timeout = float(
                                        session_info.get("keepalive_timeout_seconds") or 35.0
                                    )
                                    subscribed = {st: False for st, _ in _all_subs}
                                    next_subscribe_attempt_at = {st: 0.0 for st, _ in _all_subs}
                                    await self.set_status(
                                        state="subscribing",
                                        detail="Connected to Twitch EventSub, subscribing to chat messages",
                                        connected=False,
                                        auth_ready=True,
                                        last_error=None,
                                    )
                                    continue

                                if message_type == "session_keepalive":
                                    continue

                                if message_type == "session_reconnect":
                                    reconnect_url = (payload.get("session") or {}).get("reconnect_url")
                                    if reconnect_url:
                                        requested_reconnect_url = reconnect_url
                                        self.log.info("Twitch requested reconnect")
                                        break
                                    continue

                                if message_type == "notification":
                                    subscription_type = (
                                        (payload.get("subscription") or {}).get("type")
                                        or metadata.get("subscription_type")
                                    )
                                    if subscription_type == self.CHAT_MESSAGE_SUBSCRIPTION:
                                        subscribed[self.CHAT_MESSAGE_SUBSCRIPTION] = True
                                        unified = await self._map_message(session, metadata, payload)
                                        if unified is not None:
                                            await self.service.publish_message(unified)
                                            await self.set_status(
                                                state="connected",
                                                detail="Listening for chat messages",
                                                connected=True,
                                                auth_ready=True,
                                                last_event_at=unified.sent_at,
                                            )
                                    elif subscription_type == self.CHAT_NOTIFICATION_SUBSCRIPTION:
                                        subscribed[self.CHAT_NOTIFICATION_SUBSCRIPTION] = True
                                        unified = await self._map_notification_message(session, metadata, payload)
                                        if unified is not None:
                                            await self.service.publish_message(unified)
                                            if subscribed[self.CHAT_MESSAGE_SUBSCRIPTION]:
                                                await self.set_status(
                                                    state="connected",
                                                    detail="Listening for chat messages",
                                                    connected=True,
                                                    auth_ready=True,
                                                    last_event_at=unified.sent_at,
                                                )
                                    elif subscription_type == self.CHAT_MESSAGE_DELETE_SUBSCRIPTION:
                                        subscribed[self.CHAT_MESSAGE_DELETE_SUBSCRIPTION] = True
                                        await self._handle_message_delete(metadata, payload)
                                    elif subscription_type in self.HYPE_TRAIN_TYPES:
                                        subscribed[subscription_type] = True
                                        ht_data = self._map_hype_train_event(subscription_type, metadata, payload)
                                        if ht_data:
                                            await self.service.broadcast_hype_train(ht_data)
                                    elif subscription_type == self.CHANNEL_POINTS_REDEMPTION_SUBSCRIPTION:
                                        subscribed[self.CHANNEL_POINTS_REDEMPTION_SUBSCRIPTION] = True
                                        unified = self._map_redemption_message(metadata, payload)
                                        if unified is not None:
                                            await self.service.publish_message(unified)
                                    continue

                                if message_type == "revocation":
                                    subscription = payload.get("subscription") or {}
                                    subscription_type = subscription.get("type")
                                    if subscription_type in subscribed:
                                        subscribed[subscription_type] = False
                                        next_subscribe_attempt_at[subscription_type] = 0.0
                                        if subscription_type == self.CHAT_MESSAGE_SUBSCRIPTION:
                                            await self.set_status(
                                                state="subscribing",
                                                detail="Twitch subscription revoked; resubscribing",
                                                connected=False,
                                                auth_ready=True,
                                                last_error="Twitch EventSub subscription revoked",
                                                last_error_at=utcnow(),
                                            )
                                        else:
                                            self.log.warning(
                                                "Twitch %s subscription revoked; resubscribing",
                                                subscription_type,
                                            )
                                    continue

                            if packet.type in (
                                aiohttp.WSMsgType.CLOSED,
                                aiohttp.WSMsgType.CLOSE,
                                aiohttp.WSMsgType.ERROR,
                            ):
                                disconnect_detail = "Twitch EventSub socket closed"
                                break

                        if requested_reconnect_url:
                            websocket_url = requested_reconnect_url
                            await self.set_status(
                                state="reconnecting",
                                detail="Twitch requested reconnect; reopening session",
                                connected=False,
                                auth_ready=True,
                                last_error=None,
                            )
                            continue

                        if self._stop_event.is_set():
                            break

                        if disconnect_detail:
                            raise TwitchDisconnect(disconnect_detail)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if isinstance(exc, TwitchDisconnect):
                    self.log.warning("%s, reconnecting", exc)
                elif isinstance(exc, (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError, OSError)):
                    self.log_transient(f"Twitch connection error: {type(exc).__name__}: {exc}")
                else:
                    self.log.exception("Twitch connector error: %s", exc)
                await self.set_status(
                    state="reconnecting",
                    detail=f"Retrying Twitch connection in {backoff}s",
                    connected=False,
                    auth_ready=bool(self._load_access_token()),
                    last_error=str(exc),
                    last_error_at=utcnow(),
                )
                websocket_url = default_websocket_url
                if await self.sleep_or_stop(backoff):
                    break
                backoff = min(backoff * 2, 120)
