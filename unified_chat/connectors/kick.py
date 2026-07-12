from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import secrets
from datetime import timedelta
from typing import Any
from urllib.parse import quote, urlencode

import aiohttp
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from unified_chat.connectors.base import BaseConnector
from unified_chat.models import Badge, Emote, UnifiedMessage
from unified_chat.oauth_pending import PendingOAuthStore
from unified_chat.utils import make_message_key, parse_datetime, utcnow

_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=30)


class KickAuthError(RuntimeError):
    """Raised when Kick OAuth token is invalid or rejected."""


DEFAULT_KICK_PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAq/+l1WnlRrGSolDMA+A8
6rAhMbQGmQ2SapVcGM3zq8ANXjnhDWocMqfWcTd95btDydITa10kDvHzw9WQOqp2
MZI7ZyrfzJuz5nhTPCiJwTwnEtWft7nV14BYRDHvlfqPUaZ+1KR4OCaO/wWIk/rQ
L/TjY0M70gse8rlBkbo2a8rKhu69RQTRsoaf4DVhDPEeSeI5jVrRDGAMGL3cGuyY
6CLKGdjVEM78g3JfYOvDU/RvfqD7L89TZ3iN94jrmWdGz34JNlEI5hqK8dd7C5EF
BEbZ5jgB8s8ReQV8H+MkuffjdAj3ajDDX3DOJMIut1lBrUVD1AaSrGCKHooWoL2e
twIDAQAB
-----END PUBLIC KEY-----
"""

_EMOTE_MARKER_RE = re.compile(r"\[emote:(\d+):([^\]]*)\]")


def parse_kick_emotes(content: str) -> tuple[str, list[Emote]]:
    """Replace emote markers with their name and return (clean_text, emotes).

    Emote begin/end positions refer to the cleaned text, matching how Twitch
    emote fragments are recorded, so the frontend renders both identically.
    """
    emotes: list[Emote] = []
    parts: list[str] = []
    cursor = 0
    length = 0
    for match in _EMOTE_MARKER_RE.finditer(content):
        before = content[cursor:match.start()]
        parts.append(before)
        length += len(before)
        emote_id = match.group(1)
        name = match.group(2) or emote_id
        parts.append(name)
        emotes.append(Emote(id=emote_id, text=name, begin=length, end=length + len(name)))
        length += len(name)
        cursor = match.end()
    parts.append(content[cursor:])
    return "".join(parts), emotes


def build_kick_signature_payload(message_id: str, timestamp: str, raw_body: bytes) -> bytes:
    return message_id.encode("utf-8") + b"." + timestamp.encode("utf-8") + b"." + raw_body


def verify_kick_signature(
    headers: dict[str, str], raw_body: bytes, public_key_pem: str = DEFAULT_KICK_PUBLIC_KEY_PEM
) -> None:
    message_id = headers.get("Kick-Event-Message-Id") or headers.get("kick-event-message-id")
    timestamp = headers.get("Kick-Event-Message-Timestamp") or headers.get("kick-event-message-timestamp")
    signature = headers.get("Kick-Event-Signature") or headers.get("kick-event-signature")
    if not message_id or not timestamp or not signature:
        raise ValueError("Missing Kick signature headers")

    public_key = serialization.load_pem_public_key(public_key_pem.encode("utf-8"))
    signed_payload = build_kick_signature_payload(message_id, timestamp, raw_body)
    signature_bytes = base64.b64decode(signature)
    public_key.verify(signature_bytes, signed_payload, padding.PKCS1v15(), hashes.SHA256())


class KickConnector(BaseConnector):
    platform = "kick"
    OAUTH_BASE_URL = "https://id.kick.com"
    API_BASE_URL = "https://api.kick.com/public/v1"
    WEBHOOK_ACTIVITY_WINDOW = timedelta(minutes=10)

    CHAT_EVENT = "chat.message.sent"
    SUB_NEW_EVENT = "channel.subscription.new"
    SUB_RENEWAL_EVENT = "channel.subscription.renewal"
    SUB_GIFTS_EVENT = "channel.subscription.gifts"
    KICKS_GIFTED_EVENT = "kicks.gifted"
    SUBSCRIPTION_EVENTS = frozenset({SUB_NEW_EVENT, SUB_RENEWAL_EVENT, SUB_GIFTS_EVENT})
    WEBHOOK_EVENTS = (
        (CHAT_EVENT, 1),
        (SUB_NEW_EVENT, 1),
        (SUB_RENEWAL_EVENT, 1),
        (SUB_GIFTS_EVENT, 1),
        (KICKS_GIFTED_EVENT, 1),
    )

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._pending_oauth = PendingOAuthStore(
            self.settings.kick_token_path.with_name("kick_oauth_pending.json")
        )
        self._app_token_cache: dict[str, Any] | None = None
        self._subscription_refreshed = False
        self._webhook_seen = False
        self._last_webhook_at = None

    def _delivery_status(self) -> tuple[str, str, bool]:
        if not self._webhook_seen or self._last_webhook_at is None:
            return "idle", "Waiting for someone to chat", False

        if self._last_webhook_at >= utcnow() - self.WEBHOOK_ACTIVITY_WINDOW:
            return "connected", "Listening for chat messages", True

        return "idle", "Waiting for someone to chat", False

    def _configured(self) -> bool:
        return bool(self.settings.kick_client_id and self.settings.kick_client_secret)

    def _load_token_file(self) -> dict[str, Any] | None:
        if not self.settings.kick_token_path.exists():
            return None
        try:
            return json.loads(self.settings.kick_token_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            self.log.warning("Kick token file is not valid JSON: %s", self.settings.kick_token_path)
            return None

    def _save_token_file(self, payload: dict[str, Any]) -> None:
        content = json.dumps(payload, indent=2)
        tmp_path = self.settings.kick_token_path.with_suffix(".tmp")
        tmp_path.write_text(content, encoding="utf-8")
        tmp_path.replace(self.settings.kick_token_path)

    async def get_authorization_url(self) -> str:
        if not self._configured():
            raise RuntimeError("Missing KICK_CLIENT_ID or KICK_CLIENT_SECRET")
        if not self.settings.kick_scope:
            raise RuntimeError("KICK_SCOPE is empty — set it in .env (see .env.example)")

        oauth_state = secrets.token_urlsafe(24)
        code_verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode("utf-8")).digest()
        ).decode("utf-8").rstrip("=")

        params = {
            "response_type": "code",
            "client_id": self.settings.kick_client_id,
            "redirect_uri": self.settings.kick_redirect_uri,
            "scope": self.settings.kick_scope,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": oauth_state,
        }
        self._pending_oauth.save(
            {
                "state": oauth_state,
                "code_verifier": code_verifier,
            }
        )
        # Kick's OAuth server expects %20 between scopes, not the + that
        # urlencode's default quote_plus produces.
        return f"{self.OAUTH_BASE_URL}/oauth/authorize?{urlencode(params, quote_via=quote)}"

    async def complete_authorization(self, code: str | None, state: str | None) -> None:
        if not self._configured():
            raise RuntimeError("Missing KICK_CLIENT_ID or KICK_CLIENT_SECRET")
        pending = self._pending_oauth.load()
        if not pending:
            raise RuntimeError("No pending Kick authorization found; start again")
        code_verifier = pending.get("code_verifier")
        if not code or not state or state != pending.get("state") or not code_verifier:
            raise RuntimeError("Invalid Kick OAuth callback")

        payload = {
            "grant_type": "authorization_code",
            "client_id": self.settings.kick_client_id,
            "client_secret": self.settings.kick_client_secret,
            "redirect_uri": self.settings.kick_redirect_uri,
            "code_verifier": str(code_verifier),
            "code": code,
        }
        try:
            async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
                async with session.post(
                    f"{self.OAUTH_BASE_URL}/oauth/token",
                    data=payload,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                ) as response:
                    data = await response.json(content_type=None)
                    if response.status >= 400:
                        raise RuntimeError(f"Kick token exchange failed {response.status}: {data}")
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"Kick token exchange failed: {exc}") from exc

        token_payload = {
            **data,
            "expires_at": (utcnow() + timedelta(seconds=int(data.get("expires_in") or 0))).isoformat(),
        }
        self._save_token_file(token_payload)
        self._pending_oauth.clear()

    async def _refresh_user_token(self, refresh_token: str) -> dict[str, Any]:
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self.settings.kick_client_id,
            "client_secret": self.settings.kick_client_secret,
        }
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            async with session.post(
                f"{self.OAUTH_BASE_URL}/oauth/token",
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ) as response:
                data = await response.json(content_type=None)
                if response.status >= 400:
                    if response.status != 429 and response.status < 500:
                        raise KickAuthError(f"Kick refresh rejected {response.status}: {data}")
                    raise RuntimeError(f"Kick refresh failed {response.status}: {data}")
        return {
            **data,
            "expires_at": (utcnow() + timedelta(seconds=int(data.get("expires_in") or 0))).isoformat(),
        }

    async def _get_user_token(self) -> str | None:
        payload = self._load_token_file()
        if not payload:
            return None

        expires_at = parse_datetime(payload.get("expires_at"))
        refresh_token = payload.get("refresh_token")
        if expires_at and expires_at <= utcnow() + timedelta(minutes=2):
            if not refresh_token:
                return None
            payload = await self._refresh_user_token(refresh_token)
            self._save_token_file(payload)
        return payload.get("access_token")

    async def _get_app_token(self) -> str | None:
        cached = self._app_token_cache or {}
        expires_at = parse_datetime(cached.get("expires_at"))
        if cached.get("access_token") and expires_at and expires_at > utcnow() + timedelta(minutes=2):
            return cached["access_token"]

        if not self._configured():
            return None
        payload = {
            "grant_type": "client_credentials",
            "client_id": self.settings.kick_client_id,
            "client_secret": self.settings.kick_client_secret,
        }
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            async with session.post(
                f"{self.OAUTH_BASE_URL}/oauth/token",
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ) as response:
                data = await response.json(content_type=None)
                if response.status >= 400:
                    if response.status != 429 and response.status < 500:
                        raise KickAuthError(f"Kick app token rejected {response.status}: {data}")
                    raise RuntimeError(f"Kick app token failed {response.status}: {data}")
        self._app_token_cache = {
            **data,
            "expires_at": (utcnow() + timedelta(seconds=int(data.get("expires_in") or 0))).isoformat(),
        }
        return self._app_token_cache.get("access_token")

    async def _get_subscription_token(self) -> tuple[str | None, str]:
        user_token = await self._get_user_token()
        if user_token:
            return user_token, "user"
        if self.settings.kick_broadcaster_user_id:
            app_token = await self._get_app_token()
            if app_token:
                return app_token, "app"
        return None, "none"

    def _subscription_matches(self, subscription: dict[str, Any]) -> bool:
        name = str(subscription.get("name") or subscription.get("event") or "")
        version = str(subscription.get("version") or "")
        expected_broadcaster = self.settings.kick_broadcaster_user_id
        broadcaster_user_id = subscription.get("broadcaster_user_id")

        if (name, version) not in {(n, str(v)) for n, v in self.WEBHOOK_EVENTS}:
            return False
        if expected_broadcaster and broadcaster_user_id not in (None, int(expected_broadcaster), str(expected_broadcaster)):
            return False
        return True

    async def _delete_subscriptions(self, session: aiohttp.ClientSession, subscription_ids: list[str]) -> None:
        if not subscription_ids:
            return
        params = [("id", subscription_id) for subscription_id in subscription_ids]
        async with session.delete(f"{self.API_BASE_URL}/events/subscriptions", params=params) as response:
            if response.status >= 400:
                detail = await response.text()
                raise RuntimeError(f"Kick unsubscribe failed {response.status}: {detail[:300]}")

    async def _ensure_chat_subscription(self, access_token: str, mode: str) -> None:
        headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
        async with aiohttp.ClientSession(headers=headers, timeout=_HTTP_TIMEOUT) as session:
            query_params: dict[str, Any] = {}
            if self.settings.kick_broadcaster_user_id:
                query_params["broadcaster_user_id"] = int(self.settings.kick_broadcaster_user_id)
            async with session.get(f"{self.API_BASE_URL}/events/subscriptions", params=query_params) as response:
                existing = await response.json(content_type=None)
                if response.status >= 400:
                    raise RuntimeError(f"Kick subscriptions list failed {response.status}: {existing}")

            subscriptions = existing.get("data") or []
            matching_subscriptions = [
                subscription
                for subscription in subscriptions
                if isinstance(subscription, dict) and self._subscription_matches(subscription)
            ]
            covered_events = {
                str(subscription.get("name") or subscription.get("event") or "")
                for subscription in matching_subscriptions
            }
            all_events_covered = covered_events >= {name for name, _ in self.WEBHOOK_EVENTS}
            if all_events_covered and self._subscription_refreshed:
                return

            if matching_subscriptions:
                subscription_ids = [str(subscription.get("id") or "") for subscription in matching_subscriptions if subscription.get("id")]
                if subscription_ids:
                    self.log.info(
                        "Refreshing existing Kick chat subscriptions to apply current webhook configuration"
                    )
                    await self._delete_subscriptions(session, subscription_ids)

            body: dict[str, Any] = {
                "events": [{"name": name, "version": version} for name, version in self.WEBHOOK_EVENTS],
                "method": "webhook",
            }
            if mode == "app":
                if not self.settings.kick_broadcaster_user_id:
                    raise RuntimeError("KICK_BROADCASTER_USER_ID is required when using an app token")
                body["broadcaster_user_id"] = int(self.settings.kick_broadcaster_user_id)

            async with session.post(f"{self.API_BASE_URL}/events/subscriptions", json=body) as response:
                data = await response.json(content_type=None)
                if response.status >= 400:
                    raise RuntimeError(f"Kick subscribe failed {response.status}: {data}")
            self._subscription_refreshed = True
            self.log.info("Kick chat subscription created")

    def _map_message(self, payload: dict[str, Any]) -> UnifiedMessage | None:
        message_id = str(payload.get("message_id") or "")
        sender = payload.get("sender") or {}
        identity = sender.get("identity") or {}
        content = str(payload.get("content") or "")
        text, emotes = parse_kick_emotes(content)
        if not emotes:
            text = text.strip()

        # Kick replies carry the tagged user in replies_to, not in content,
        # so restore the "@username " prefix the Kick UI shows.
        replies_to = payload.get("replies_to")
        reply_sender = replies_to.get("sender") or {} if isinstance(replies_to, dict) else {}
        mention = str(reply_sender.get("username") or "").strip()
        if mention and not text.lower().startswith(f"@{mention.lower()}"):
            prefix = f"@{mention} "
            text = prefix + text
            emotes = [
                Emote(id=emote.id, text=emote.text, begin=emote.begin + len(prefix), end=emote.end + len(prefix))
                for emote in emotes
            ]

        if not message_id or not text.strip():
            return None

        badges = [
            Badge(
                text=str(badge.get("text") or badge.get("type") or "badge"),
                type=str(badge.get("type") or "badge"),
                count=badge.get("count"),
            )
            for badge in identity.get("badges") or []
        ]

        broadcaster = payload.get("broadcaster") or {}
        return UnifiedMessage(
            id=make_message_key("kick", message_id),
            platform="kick",
            platform_message_id=message_id,
            channel_id=str(broadcaster.get("user_id") or self.settings.kick_broadcaster_user_id or ""),
            author_id=str(sender.get("user_id") or "") or None,
            author_display_name=str(sender.get("username") or sender.get("channel_slug") or "Unknown"),
            author_login=sender.get("channel_slug") or sender.get("username"),
            author_color=identity.get("username_color"),
            avatar_url=sender.get("profile_picture"),
            badges=badges,
            emotes=emotes,
            text=text,
            sent_at=parse_datetime(payload.get("created_at")) or utcnow(),
            raw_payload=payload,
        )

    def _map_subscription_event(
        self,
        event_type: str,
        payload: dict[str, Any],
        delivery_id: str,
    ) -> UnifiedMessage | None:
        """Map a Kick subscription webhook to a system-notice message.

        Subscription payloads carry no message id, so the unique
        Kick-Event-Message-Id delivery header doubles as the dedup key.
        """
        if not delivery_id:
            return None

        broadcaster = payload.get("broadcaster") or {}
        duration = payload.get("duration")
        months = duration if isinstance(duration, int) else 0

        if event_type == self.SUB_GIFTS_EVENT:
            gifter = payload.get("gifter") or {}
            anonymous = bool(gifter.get("is_anonymous")) or not gifter.get("username")
            author = {} if anonymous else gifter
            name = "Anonymous" if anonymous else str(gifter.get("username"))
            count = max(len(payload.get("giftees") or []), 1)
            plural = "subscription" if count == 1 else "subscriptions"
            text = f"{name} gifted {count} {plural}!"
            notice_type = "sub_gift"
        else:
            author = payload.get("subscriber") or {}
            name = str(author.get("username") or "Someone")
            if event_type == self.SUB_RENEWAL_EVENT:
                notice_type = "resub"
                text = f"{name} resubscribed!"
                if months > 1:
                    text += f" They've been subscribed for {months} months!"
            else:
                notice_type = "sub"
                if months > 1:
                    text = f"{name} subscribed for {months} months!"
                else:
                    text = f"{name} subscribed!"

        return UnifiedMessage(
            id=make_message_key("kick", delivery_id),
            platform="kick",
            platform_message_id=delivery_id,
            message_kind="system",
            notice_type=notice_type,
            channel_id=str(broadcaster.get("user_id") or self.settings.kick_broadcaster_user_id or ""),
            author_display_name=name,
            author_login=author.get("channel_slug") or author.get("username"),
            author_id=str(author.get("user_id") or "") or None,
            avatar_url=None,
            badges=[],
            text=text,
            sent_at=parse_datetime(payload.get("created_at")) or utcnow(),
            raw_payload=payload,
        )

    def _map_kicks_event(self, payload: dict[str, Any], delivery_id: str) -> UnifiedMessage | None:
        """Map a Kick 'kicks.gifted' webhook to a system-notice message.

        Like subscription events, the payload carries no message id, so the
        unique Kick-Event-Message-Id delivery header is the dedup key.
        """
        if not delivery_id:
            return None

        sender = payload.get("sender") or {}
        gift = payload.get("gift") or {}
        broadcaster = payload.get("broadcaster") or {}

        anonymous = bool(sender.get("is_anonymous")) or not sender.get("username")
        name = "Anonymous" if anonymous else str(sender.get("username"))
        author = {} if anonymous else sender

        amount = gift.get("amount")
        amount = amount if isinstance(amount, int) and amount > 0 else 0
        unit = "Kick" if amount == 1 else "Kicks"
        text = f"{name} sent {amount} {unit}!" if amount else f"{name} sent Kicks!"
        message = str(gift.get("message") or "").strip()
        if message:
            text += f" {message}"

        return UnifiedMessage(
            id=make_message_key("kick", delivery_id),
            platform="kick",
            platform_message_id=delivery_id,
            message_kind="system",
            notice_type="kicks",
            channel_id=str(broadcaster.get("user_id") or self.settings.kick_broadcaster_user_id or ""),
            author_display_name=name,
            author_login=author.get("channel_slug") or author.get("username"),
            author_id=str(author.get("user_id") or "") or None,
            avatar_url=None,
            badges=[],
            text=text,
            sent_at=parse_datetime(payload.get("created_at")) or utcnow(),
            raw_payload=payload,
        )

    async def handle_webhook(self, headers: dict[str, str], raw_body: bytes) -> UnifiedMessage | None:
        try:
            verify_kick_signature(headers, raw_body)
        except (InvalidSignature, ValueError) as exc:
            self.log.warning("Kick webhook signature verification failed: %s", exc)
            raise RuntimeError(str(exc)) from exc

        event_type = headers.get("Kick-Event-Type") or headers.get("kick-event-type") or ""
        if (
            event_type != self.CHAT_EVENT
            and event_type != self.KICKS_GIFTED_EVENT
            and event_type not in self.SUBSCRIPTION_EVENTS
        ):
            self.log.debug("Ignoring Kick webhook event type: %s", event_type)
            return None

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self.log.warning("Malformed Kick webhook body: %s", exc)
            raise ValueError("Invalid webhook payload") from exc

        if event_type == self.CHAT_EVENT:
            unified = self._map_message(payload)
        elif event_type == self.KICKS_GIFTED_EVENT:
            delivery_id = headers.get("Kick-Event-Message-Id") or headers.get("kick-event-message-id") or ""
            unified = self._map_kicks_event(payload, delivery_id)
        else:
            delivery_id = headers.get("Kick-Event-Message-Id") or headers.get("kick-event-message-id") or ""
            unified = self._map_subscription_event(event_type, payload, delivery_id)
        if unified is not None:
            self._webhook_seen = True
            self._last_webhook_at = utcnow()
            await self.service.publish_message(unified)
            await self.set_status(
                state="connected",
                detail="Listening for chat messages",
                connected=True,
                auth_ready=True,
                last_event_at=unified.sent_at,
                last_error=None,
            )
        return unified

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
        if not target.isdigit():
            raise ValueError("user_id must be a numeric Kick user id")
        broadcaster_id = str(self.settings.kick_broadcaster_user_id or "").strip()
        if not broadcaster_id:
            raise ValueError("KICK_BROADCASTER_USER_ID is required for Kick moderation")
        if target == broadcaster_id:
            raise ValueError("Cannot ban the broadcaster")
        # The API takes minutes (1..10080); the endpoint contract is seconds like Twitch.
        if duration is not None and not 60 <= duration <= 604800:
            raise ValueError("duration must be between 60 and 604800 seconds")

        token = await self._get_user_token()
        if not token:
            raise RuntimeError("Kick authorization required; visit /auth/kick/start")

        body: dict[str, Any] = {
            "broadcaster_user_id": int(broadcaster_id),
            "user_id": int(target),
        }
        if duration is not None:
            body["duration"] = duration // 60
        if reason:
            body["reason"] = str(reason)[:100]

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            async with session.post(
                f"{self.API_BASE_URL}/moderation/bans", headers=headers, json=body
            ) as response:
                try:
                    payload = await response.json(content_type=None)
                except Exception:
                    payload = {"raw": await response.text()}
                if response.status < 400:
                    return payload
                if response.status in (401, 403):
                    raise RuntimeError(
                        "Kick rejected the moderation request; re-authorize via /auth/kick/start "
                        "so the token gets the moderation:ban scope"
                    )
                raise RuntimeError(f"Kick ban failed {response.status}: {payload}")

    async def delete_message(self, message_id: str) -> dict[str, Any]:
        target = str(message_id or "").strip()
        if not target:
            raise ValueError("message_id is required")

        token = await self._get_user_token()
        if not token:
            raise RuntimeError("Kick authorization required; visit /auth/kick/start")

        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            async with session.delete(
                f"{self.API_BASE_URL}/chat/{quote(target, safe='')}", headers=headers
            ) as response:
                try:
                    payload = await response.json(content_type=None)
                except Exception:
                    payload = {"raw": await response.text()}
                if response.status < 400:
                    return payload
                if response.status in (401, 403):
                    raise RuntimeError(
                        "Kick rejected the delete; re-authorize via /auth/kick/start "
                        "so the token gets the moderation:chat_message:manage scope"
                    )
                raise RuntimeError(f"Kick delete failed {response.status}: {payload}")

    async def run(self) -> None:
        if not self._configured():
            await self.set_status(
                state="disabled",
                detail="Missing KICK_CLIENT_ID or KICK_CLIENT_SECRET",
                connected=False,
                auth_ready=False,
            )
            return

        access_token = None
        while not self._stop_event.is_set():
            try:
                access_token, mode = await self._get_subscription_token()
                if not access_token:
                    detail = "Visit /auth/kick/start or set KICK_BROADCASTER_USER_ID for app-token mode"
                    await self.set_status(
                        state="auth_required",
                        detail=detail,
                        connected=False,
                        auth_ready=False,
                    )
                    if await self.sleep_or_stop(10):
                        break
                    continue

                await self._ensure_chat_subscription(access_token, mode)
                self.clear_transient()
                state, detail, connected = self._delivery_status()
                await self.set_status(
                    state=state,
                    detail=detail,
                    connected=connected,
                    auth_ready=True,
                    last_error=None,
                )
                if await self.sleep_or_stop(300):
                    break
            except asyncio.CancelledError:
                raise
            except KickAuthError as exc:
                self.log_transient(f"Kick authorization invalid: {exc}")
                await self.set_status(
                    state="auth_required",
                    detail="Kick authorization expired; visit /auth/kick/start to re-authorize",
                    connected=False,
                    auth_ready=False,
                    last_error=str(exc),
                    last_error_at=utcnow(),
                )
                if await self.sleep_or_stop(60):
                    break
            except Exception as exc:
                if isinstance(exc, (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError, OSError)):
                    self.log_transient(f"Kick connection error: {type(exc).__name__}: {exc}")
                else:
                    self.log.exception("Kick connector error: %s", exc)
                await self.set_status(
                    state="reconnecting",
                    detail="Retrying Kick subscription setup",
                    connected=False,
                    auth_ready=bool(access_token),
                    last_error=str(exc),
                    last_error_at=utcnow(),
                )
                if await self.sleep_or_stop(30):
                    break
