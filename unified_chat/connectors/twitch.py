from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Literal

import aiohttp

from unified_chat.connectors.base import BaseConnector
from unified_chat.models import Badge, Emote, UnifiedMessage
from unified_chat.utils import make_message_key, parse_datetime, utcnow

_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=30)


@dataclass(slots=True)
class SubscribeResult:
    outcome: Literal["ok", "auth_failed", "rate_limited", "retryable_error", "fatal_error"]
    detail: str = ""
    retry_at: float | None = None


class TwitchConnector(BaseConnector):
    platform = "twitch"
    SUBSCRIBE_URL = "https://api.twitch.tv/helix/eventsub/subscriptions"
    CHAT_URL = "https://api.twitch.tv/helix/chat/messages"
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

    async def _subscribe_chat(self, session: aiohttp.ClientSession, session_id: str) -> SubscribeResult:
        token = self._load_access_token()
        if not token:
            return SubscribeResult(
                "auth_failed",
                detail=f"No Twitch token available in {self.settings.twitch_tokens_path}",
            )

        body = {
            "type": "channel.chat.message",
            "version": "1",
            "condition": {
                "broadcaster_user_id": self.settings.twitch_broadcaster_id,
                "user_id": self.settings.twitch_broadcaster_id,
            },
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
            self.log.info("Subscribed to Twitch chat messages")
            return ok
        if ok.outcome != "auth_failed":
            if ok.outcome == "rate_limited":
                wait = max(int((ok.retry_at or time.time()) - time.time()), 1)
                self.log.warning("Twitch rate-limited, waiting %ds before retry", wait)
            return ok

        self.log.warning("Twitch subscribe returned 401, reloading token")
        fresh_token = self._load_access_token()
        if not fresh_token:
            return SubscribeResult(
                "auth_failed",
                detail=f"No Twitch token available in {self.settings.twitch_tokens_path}",
            )

        retried = await _send(fresh_token)
        if retried.outcome == "ok":
            self.log.info("Subscribed to Twitch chat messages")
        return retried

    def _map_message(self, metadata: dict[str, Any], payload: dict[str, Any]) -> UnifiedMessage | None:
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

        return UnifiedMessage(
            id=make_message_key("twitch", message_id),
            platform="twitch",
            platform_message_id=message_id,
            channel_id=str(event.get("broadcaster_user_id") or self.settings.twitch_broadcaster_id),
            author_display_name=str(event.get("chatter_user_name") or event.get("chatter_user_login") or "Unknown"),
            author_login=event.get("chatter_user_login"),
            author_color=event.get("color"),
            badges=badges,
            emotes=emotes,
            text=text,
            sent_at=parse_datetime(metadata.get("message_timestamp")) or utcnow(),
            raw_payload={"metadata": metadata, "payload": payload},
        )

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
            token = self._load_access_token()
            if not token:
                await self.set_status(
                    state="waiting_for_token",
                    detail=f"No Twitch token available in {self.settings.twitch_tokens_path}",
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
                        subscribed = False
                        next_subscribe_attempt_at = 0.0
                        keepalive_timeout = 35.0

                        while not self._stop_event.is_set():
                            now = time.time()
                            if session_id and not subscribed and now >= next_subscribe_attempt_at:
                                result = await self._subscribe_chat(session, session_id)
                                if result.outcome == "ok":
                                    subscribed = True
                                    backoff = 5
                                    await self.set_status(
                                        state="connected",
                                        detail="Listening for chat messages",
                                        connected=True,
                                        auth_ready=True,
                                        last_error=None,
                                    )
                                elif result.outcome == "rate_limited":
                                    retry_at = result.retry_at or (time.time() + 30.0)
                                    next_subscribe_attempt_at = retry_at
                                    wait = max(int(retry_at - time.time()), 1)
                                    await self.set_status(
                                        state="rate_limited",
                                        detail=f"Twitch transport limit reached; retrying in {wait}s",
                                        connected=False,
                                        auth_ready=True,
                                        last_error=result.detail or None,
                                        last_error_at=utcnow(),
                                    )
                                elif result.outcome == "auth_failed":
                                    next_subscribe_attempt_at = time.time() + 5.0
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
                                    next_subscribe_attempt_at = time.time() + (
                                        15.0 if result.outcome == "retryable_error" else 30.0
                                    )
                                    await self.set_status(
                                        state="subscribing",
                                        detail="Twitch subscription not ready; staying connected and retrying",
                                        connected=False,
                                        auth_ready=True,
                                        last_error=result.detail or None,
                                        last_error_at=utcnow(),
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
                                    subscribed = False
                                    next_subscribe_attempt_at = 0.0
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
                                    if subscription_type == "channel.chat.message":
                                        subscribed = True
                                        unified = self._map_message(metadata, payload)
                                        if unified is not None:
                                            await self.service.publish_message(unified)
                                            await self.set_status(
                                                state="connected",
                                                detail="Listening for chat messages",
                                                connected=True,
                                                auth_ready=True,
                                                last_event_at=unified.sent_at,
                                            )
                                    continue

                                if message_type == "revocation":
                                    subscription = payload.get("subscription") or {}
                                    if subscription.get("type") == "channel.chat.message":
                                        subscribed = False
                                        next_subscribe_attempt_at = 0.0
                                        await self.set_status(
                                            state="subscribing",
                                            detail="Twitch subscription revoked; resubscribing",
                                            connected=False,
                                            auth_ready=True,
                                            last_error="Twitch EventSub subscription revoked",
                                            last_error_at=utcnow(),
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
                            raise RuntimeError(disconnect_detail)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if isinstance(exc, RuntimeError) and str(exc) == "Twitch keepalive timed out":
                    self.log.warning("Twitch keepalive timed out, reconnecting")
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
