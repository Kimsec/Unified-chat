from __future__ import annotations

import asyncio
import codecs
import contextlib
import json
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from unified_chat.connectors.base import BaseConnector
from unified_chat.models import Badge, UnifiedMessage
from unified_chat.oauth_pending import PendingOAuthStore
from unified_chat.utils import make_message_key, parse_datetime, utcnow

_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=30)
# The /stream connection is long-lived: no total cap, but treat a socket that
# goes silent longer than sock_read as dead and reconnect.
_STREAM_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_read=80)


class YouTubeApiError(RuntimeError):
    def __init__(self, status: int, data: Any) -> None:
        self.status = status
        self.data = data
        super().__init__(f"YouTube API failed {status}: {data}")


class _StreamArrayDecoder:
    """Incrementally pull complete objects from a streamed JSON array.

    The /stream endpoint sends `[{...},{...},...` over an open connection and the
    closing `]` never arrives while the chat is live. Feed decoded text chunks;
    each call returns the array elements (dicts) that completed in that chunk.
    Braces inside strings and escaped quotes are handled so message text with
    `{`/`}`/`"` can't confuse the framing.
    """

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, text: str) -> list[Any]:
        if text:
            self._buf += text
        buf = self._buf
        results: list[Any] = []
        depth = 0
        in_str = False
        escape = False
        obj_start = -1
        cut = 0  # buffer is fully consumed up to here
        for i, ch in enumerate(buf):
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                if depth == 0:
                    obj_start = i
                depth += 1
            elif ch == "}":
                if depth > 0:
                    depth -= 1
                    if depth == 0 and obj_start >= 0:
                        try:
                            results.append(json.loads(buf[obj_start:i + 1]))
                        except json.JSONDecodeError:
                            pass
                        cut = i + 1
                        obj_start = -1
        # Retain only the unconsumed tail: an in-progress object, or the trailing
        # separators/whitespace between objects. Re-scanned fresh next feed, so no
        # brace/string state is carried across calls.
        self._buf = buf[obj_start:] if depth > 0 and obj_start >= 0 else buf[cut:]
        return results


class YouTubeConnector(BaseConnector):
    platform = "youtube"
    BROADCASTS_URL = "https://www.googleapis.com/youtube/v3/liveBroadcasts"
    CHAT_MESSAGES_URL = "https://www.googleapis.com/youtube/v3/liveChat/messages"
    # Streaming transport of the recommended streamList method (not in the REST
    # discovery doc, but a working HTTPS endpoint). One open connection replaces
    # polling — no quota drain, no stale pageToken.
    CHAT_STREAM_URL = "https://www.googleapis.com/youtube/v3/liveChat/messages/stream"
    DISCOVERY_POLL_SEC = 30.0
    ERROR_RETRY_SEC = 30.0

    SYSTEM_EVENT_NOTICES = {
        "superChatEvent": "super_chat",
        "superStickerEvent": "super_sticker",
        "newSponsorEvent": "member",
        "memberMilestoneChatEvent": "member_milestone",
        "membershipGiftingEvent": "member_gift",
    }

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._pending_oauth = PendingOAuthStore(
            self.settings.youtube_token_path.with_name("youtube_oauth_pending.json")
        )

    def _save_credentials_json(self, content: str) -> None:
        tmp_path = self.settings.youtube_token_path.with_suffix(".tmp")
        tmp_path.write_text(content, encoding="utf-8")
        tmp_path.replace(self.settings.youtube_token_path)

    def _configured(self) -> bool:
        return bool(self.settings.youtube_client_secrets_file and self.settings.youtube_client_secrets_file.exists())

    def _build_flow(self, state: str | None = None, code_verifier: str | None = None) -> Flow:
        flow = Flow.from_client_secrets_file(
            str(self.settings.youtube_client_secrets_file),
            scopes=self.settings.youtube_scopes,
            state=state,
            code_verifier=code_verifier,
        )
        flow.redirect_uri = self.settings.youtube_redirect_uri
        return flow

    def get_authorization_url(self) -> str:
        if not self._configured():
            raise RuntimeError("Missing YOUTUBE_CLIENT_SECRETS_FILE")
        if not self.settings.youtube_scopes:
            raise RuntimeError("YOUTUBE_SCOPES is empty — set it in .env (see .env.example)")
        flow = self._build_flow()
        authorization_url, state = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )
        code_verifier = getattr(flow, "code_verifier", None)
        if not code_verifier:
            raise RuntimeError("YouTube OAuth flow did not generate a code verifier")
        self._pending_oauth.save(
            {
                "state": state,
                "code_verifier": code_verifier,
            }
        )
        return authorization_url

    async def complete_authorization(self, callback_url: str, state: str | None) -> None:
        if not self._configured():
            raise RuntimeError("Missing YOUTUBE_CLIENT_SECRETS_FILE")
        pending = self._pending_oauth.load()
        if not pending:
            raise RuntimeError("No pending YouTube authorization found; start again")
        if not state or state != pending.get("state"):
            raise RuntimeError("Invalid YouTube OAuth state")

        code_verifier = pending.get("code_verifier")
        if not code_verifier:
            raise RuntimeError("Missing saved YouTube code verifier; start authorization again")

        flow = self._build_flow(state=state, code_verifier=str(code_verifier))
        try:
            await asyncio.to_thread(flow.fetch_token, authorization_response=callback_url)
        except Exception as exc:
            raise RuntimeError(f"YouTube token exchange failed: {exc}") from exc
        await asyncio.to_thread(self._save_credentials_json, flow.credentials.to_json())
        self._pending_oauth.clear()

    def _load_credentials(self) -> Credentials | None:
        if not self.settings.youtube_token_path.exists():
            return None
        credentials = Credentials.from_authorized_user_file(
            str(self.settings.youtube_token_path),
            scopes=self.settings.youtube_scopes,
        )
        if credentials.expired and credentials.refresh_token:
            self.log.info("Refreshing expired YouTube credentials")
            credentials.refresh(GoogleRequest())
            self._save_credentials_json(credentials.to_json())
        if not credentials.valid:
            self.log.warning("YouTube credentials are not valid")
            return None
        return credentials

    async def _authorized_get(
        self, session: aiohttp.ClientSession, access_token: str, url: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {access_token}"}
        async with session.get(url, params=params, headers=headers, timeout=_HTTP_TIMEOUT) as response:
            try:
                data = await response.json(content_type=None)
            except (json.JSONDecodeError, aiohttp.ContentTypeError):
                text = await response.text()
                raise RuntimeError(f"YouTube API returned non-JSON {response.status}: {text[:300]}")
            if response.status >= 400:
                raise YouTubeApiError(response.status, data)
            return data

    def _system_event_text(self, snippet_type: str, snippet: dict[str, Any], name: str) -> str | None:
        if snippet_type == "superChatEvent":
            details = snippet.get("superChatDetails") or {}
            amount = str(details.get("amountDisplayString") or "").strip()
            comment = str(details.get("userComment") or "").strip()
            text = f"{name} sent a {amount} Super Chat" if amount else f"{name} sent a Super Chat"
            return f"{text}: {comment}" if comment else f"{text}!"

        if snippet_type == "superStickerEvent":
            details = snippet.get("superStickerDetails") or {}
            amount = str(details.get("amountDisplayString") or "").strip()
            if amount:
                return f"{name} sent a {amount} Super Sticker!"
            return f"{name} sent a Super Sticker!"

        if snippet_type == "newSponsorEvent":
            details = snippet.get("newSponsorDetails") or {}
            level = str(details.get("memberLevelName") or "").strip()
            if details.get("isUpgrade"):
                return f"{name} upgraded their membership to {level}!" if level else f"{name} upgraded their membership!"
            return f"{name} became a member ({level})!" if level else f"{name} became a member!"

        if snippet_type == "memberMilestoneChatEvent":
            details = snippet.get("memberMilestoneChatDetails") or {}
            months = details.get("memberMonth")
            comment = str(details.get("userComment") or "").strip()
            if isinstance(months, int) and months > 0:
                text = f"{name} has been a member for {months} month{'s' if months != 1 else ''}!"
            else:
                text = f"{name} celebrated a membership milestone!"
            return f"{text} {comment}".rstrip() if comment else text

        if snippet_type == "membershipGiftingEvent":
            details = snippet.get("membershipGiftingDetails") or {}
            count = details.get("giftMembershipsCount")
            count = count if isinstance(count, int) and count > 0 else 1
            plural = "membership" if count == 1 else "memberships"
            return f"{name} gifted {count} {plural}!"

        return None

    def _map_system_event(
        self,
        item: dict[str, Any],
        live_chat_id: str,
        snippet_type: str,
    ) -> UnifiedMessage | None:
        message_id = str(item.get("id") or "")
        snippet = item.get("snippet") or {}
        author = item.get("authorDetails") or {}
        name = str(author.get("displayName") or "Someone")

        text = self._system_event_text(snippet_type, snippet, name)
        if not message_id or not text:
            return None

        return UnifiedMessage(
            id=make_message_key("youtube", message_id),
            platform="youtube",
            platform_message_id=message_id,
            message_kind="system",
            notice_type=self.SYSTEM_EVENT_NOTICES[snippet_type],
            channel_id=str(author.get("channelId") or live_chat_id),
            author_display_name=name,
            author_login=author.get("channelId"),
            avatar_url=author.get("profileImageUrl"),
            badges=[],
            text=text,
            sent_at=parse_datetime(snippet.get("publishedAt")) or utcnow(),
            raw_payload=item,
        )

    def _map_message(self, item: dict[str, Any], live_chat_id: str) -> UnifiedMessage | None:
        message_id = str(item.get("id") or "")
        snippet = item.get("snippet") or {}
        author = item.get("authorDetails") or {}

        snippet_type = str(snippet.get("type") or "")
        if snippet_type in self.SYSTEM_EVENT_NOTICES:
            return self._map_system_event(item, live_chat_id, snippet_type)

        text = str(snippet.get("displayMessage") or "").strip()
        if not message_id or not text:
            return None

        badges = []
        if author.get("isChatOwner"):
            badges.append(Badge(text="Owner", type="owner"))
        if author.get("isChatModerator"):
            badges.append(Badge(text="Moderator", type="moderator"))
        if author.get("isChatSponsor"):
            badges.append(Badge(text="Member", type="member"))
        if author.get("isVerified"):
            badges.append(Badge(text="Verified", type="verified"))

        return UnifiedMessage(
            id=make_message_key("youtube", message_id),
            platform="youtube",
            platform_message_id=message_id,
            channel_id=str(author.get("channelId") or live_chat_id),
            author_display_name=str(author.get("displayName") or "Unknown"),
            author_login=author.get("channelId"),
            avatar_url=author.get("profileImageUrl"),
            badges=badges,
            text=text,
            sent_at=parse_datetime(snippet.get("publishedAt")) or utcnow(),
            raw_payload=item,
        )

    def _select_broadcast(self, items: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str]:
        live_candidate: dict[str, Any] | None = None
        starting_candidate: dict[str, Any] | None = None
        saw_chatless_candidate = False

        for item in items:
            snippet = (item or {}).get("snippet") or {}
            status = (item or {}).get("status") or {}
            life_cycle_status = str(status.get("lifeCycleStatus") or "").strip()
            live_chat_id = str(snippet.get("liveChatId") or "").strip()

            if life_cycle_status not in {"live", "liveStarting"}:
                continue
            if not live_chat_id:
                saw_chatless_candidate = True
                continue
            if life_cycle_status == "live":
                live_candidate = item
                break
            if life_cycle_status == "liveStarting" and starting_candidate is None:
                starting_candidate = item

        if live_candidate is not None:
            return live_candidate, ""
        if starting_candidate is not None:
            return starting_candidate, ""
        if saw_chatless_candidate:
            return None, "No active YouTube live chat available yet"
        return None, "Waiting for stream to go live"

    async def _discover_live_chat_id(
        self, session: aiohttp.ClientSession, access_token: str
    ) -> tuple[str | None, str]:
        broadcasts = await self._authorized_get(
            session,
            access_token,
            self.BROADCASTS_URL,
            {
                "part": "snippet,status",
                "broadcastType": "all",
                "mine": "true",
                "maxResults": 10,
            },
        )
        items = broadcasts.get("items") or []
        selected_broadcast, idle_detail = self._select_broadcast(items)
        if selected_broadcast is None:
            return None, idle_detail

        snippet = (selected_broadcast or {}).get("snippet") or {}
        detected_chat_id = str(snippet.get("liveChatId") or "").strip()
        if not detected_chat_id:
            return None, "Active YouTube broadcast found, but no live chat ID was returned"
        return detected_chat_id, ""

    async def _stream_chat_messages(
        self,
        session: aiohttp.ClientSession,
        access_token: str,
        live_chat_id: str,
        page_token: str | None,
    ):
        """Open the streaming chat connection and yield each response object.

        Yields `liveChatMessageListResponse` dicts as YouTube pushes them over
        the open connection. Returns when the connection closes; the caller
        reconnects with the last nextPageToken. Raises YouTubeApiError on a
        non-200 status so the caller can handle quota/401/404 distinctly.
        """
        params: dict[str, Any] = {
            "part": "snippet,authorDetails",
            "liveChatId": live_chat_id,
            "maxResults": 200,
        }
        if page_token:
            params["pageToken"] = page_token
        headers = {"Authorization": f"Bearer {access_token}"}
        decoder = _StreamArrayDecoder()
        utf8 = codecs.getincrementaldecoder("utf-8")()
        async with session.get(
            self.CHAT_STREAM_URL, params=params, headers=headers, timeout=_STREAM_TIMEOUT
        ) as response:
            if response.status != 200:
                try:
                    data = await response.json(content_type=None)
                except Exception:
                    data = (await response.text())[:300]
                raise YouTubeApiError(response.status, data)
            async for chunk in response.content.iter_any():
                for obj in decoder.feed(utf8.decode(chunk)):
                    yield obj

    def _api_error_reason(self, exc: YouTubeApiError) -> str | None:
        error = exc.data.get("error") if isinstance(exc.data, dict) else None
        errors = error.get("errors") if isinstance(error, dict) else None
        if isinstance(errors, list):
            for item in errors:
                if isinstance(item, dict) and item.get("reason"):
                    return str(item["reason"])
        return None

    def _is_quota_exceeded(self, exc: YouTubeApiError) -> bool:
        return exc.status == 403 and self._api_error_reason(exc) == "quotaExceeded"

    def _seconds_until_quota_reset(self) -> float:
        pacific = ZoneInfo("America/Los_Angeles")
        now_pacific = datetime.now(pacific)
        next_reset = (now_pacific + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return max(60.0, (next_reset - now_pacific).total_seconds())

    def _format_wait_time(self, wait_seconds: float) -> str:
        total_seconds = max(0, int(wait_seconds))
        hours, remainder = divmod(total_seconds, 3600)
        minutes, _ = divmod(remainder, 60)
        if hours and minutes:
            return f"{hours}h {minutes}m"
        if hours:
            return f"{hours}h"
        return f"{max(minutes, 1)}m"

    async def run(self) -> None:
        if not self._configured():
            await self.set_status(
                state="disabled",
                detail="Missing YOUTUBE_CLIENT_SECRETS_FILE",
                connected=False,
                auth_ready=False,
            )
            return

        live_chat_id: str | None = None
        next_page_token: str | None = None

        while not self._stop_event.is_set():
            try:
                credentials = await asyncio.to_thread(self._load_credentials)
            except Exception as exc:
                self.log_transient(f"YouTube credentials unavailable: {exc}")
                await self.set_status(
                    state="auth_required",
                    detail="YouTube credentials invalid; visit /auth/youtube/start to re-authorize",
                    connected=False,
                    auth_ready=False,
                    last_error=str(exc),
                    last_error_at=utcnow(),
                )
                if await self.sleep_or_stop(30):
                    break
                continue
            if credentials is None:
                await self.set_status(
                    state="auth_required",
                    detail="Visit /auth/youtube/start to authorize YouTube",
                    connected=False,
                    auth_ready=False,
                )
                if await self.sleep_or_stop(5):
                    break
                continue

            try:
                async with aiohttp.ClientSession() as session:
                    if live_chat_id is None:
                        live_chat_id, idle_detail = await self._discover_live_chat_id(session, credentials.token)
                        next_page_token = None
                        if live_chat_id is None:
                            await self.set_status(
                                state="idle",
                                detail=idle_detail,
                                connected=False,
                                auth_ready=True,
                                last_error=None,
                            )
                            if await self.sleep_or_stop(self.DISCOVERY_POLL_SEC):
                                break
                            continue

                    await self.set_status(
                        state="connecting",
                        detail="Opening YouTube chat stream",
                        connected=False,
                        auth_ready=True,
                        last_error=None,
                    )
                    ended = False
                    stream = self._stream_chat_messages(
                        session, credentials.token, live_chat_id, next_page_token
                    )
                    async with contextlib.aclosing(stream) as responses:
                        async for response_obj in responses:
                            self.clear_transient()
                            for item in response_obj.get("items") or []:
                                unified = self._map_message(item, live_chat_id)
                                if unified is not None:
                                    await self.service.publish_message(unified)
                            token = response_obj.get("nextPageToken")
                            if token:
                                next_page_token = token
                            if response_obj.get("offlineAt"):
                                self.log.info("YouTube broadcast went offline")
                                live_chat_id = None
                                next_page_token = None
                                ended = True
                                await self.set_status(
                                    state="idle",
                                    detail="YouTube broadcast ended",
                                    connected=False,
                                    auth_ready=True,
                                    last_error=None,
                                )
                                break
                            await self.set_status(
                                state="connected",
                                detail="Streaming YouTube chat messages",
                                connected=True,
                                auth_ready=True,
                                last_error=None,
                            )
                # Stream closed. Rediscover after a broadcast ends; otherwise
                # reconnect promptly, resuming from the last nextPageToken.
                if await self.sleep_or_stop(self.DISCOVERY_POLL_SEC if ended else 2.0):
                    break
            except YouTubeApiError as exc:
                if self._is_quota_exceeded(exc):
                    wait_seconds = self._seconds_until_quota_reset()
                    self.log.warning(
                        "YouTube quota exceeded; pausing until quota reset in %s",
                        self._format_wait_time(wait_seconds),
                    )
                    await self.set_status(
                        state="rate_limited",
                        detail="YouTube quota exceeded; waiting for quota reset",
                        connected=False,
                        auth_ready=True,
                        last_error="quotaExceeded",
                        last_error_at=utcnow(),
                    )
                    if await self.sleep_or_stop(wait_seconds):
                        break
                    continue
                if exc.status == 401:
                    # Token rejected mid-stream; next loop reloads/refreshes creds.
                    self.log_transient("YouTube stream unauthorized; refreshing credentials")
                    if await self.sleep_or_stop(2.0):
                        break
                    continue
                if exc.status == 404:
                    # The undocumented /stream endpoint is gone or changed. Fail
                    # loudly — no silent fallback to the quota-hungry list polling.
                    self.log.error(
                        "YouTube /stream returned 404 — the streaming endpoint may have "
                        "changed; YouTube chat is unavailable until the connector is updated."
                    )
                    await self.set_status(
                        state="error",
                        detail="YouTube streaming endpoint unavailable (HTTP 404)",
                        connected=False,
                        auth_ready=True,
                        last_error="stream endpoint returned 404",
                        last_error_at=utcnow(),
                    )
                    live_chat_id = None
                    next_page_token = None
                    if await self.sleep_or_stop(self.ERROR_RETRY_SEC):
                        break
                    continue
                # Other API errors (e.g. 5xx / 403 non-quota): drop chat, rediscover.
                self.log_transient(f"YouTube stream error {exc.status}: {self._api_error_reason(exc) or ''}")
                live_chat_id = None
                next_page_token = None
                if await self.sleep_or_stop(self.ERROR_RETRY_SEC):
                    break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if isinstance(exc, (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError, OSError)):
                    self.log_transient(f"YouTube stream connection error: {type(exc).__name__}: {exc}")
                    delay = 2.0
                else:
                    self.log.exception("YouTube connector error: %s", exc)
                    delay = self.ERROR_RETRY_SEC
                await self.set_status(
                    state="reconnecting",
                    detail="Reconnecting YouTube chat stream",
                    connected=False,
                    auth_ready=True,
                    last_error=str(exc),
                    last_error_at=utcnow(),
                )
                if await self.sleep_or_stop(delay):
                    break
