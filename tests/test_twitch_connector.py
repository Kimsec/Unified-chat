import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import aiohttp

import unified_chat.connectors.twitch as twitch_module
from unified_chat.config import Settings
from unified_chat.connectors.twitch import SubscribeResult, TwitchConnector
from unified_chat.service import ChatService
from unified_chat.store import MessageStore


def make_settings(root: Path) -> Settings:
    return Settings(
        project_dir=root,
        host="127.0.0.1",
        port=8090,
        app_base_url="http://127.0.0.1:8090",
        log_level="info",
        login_password_hash="",
        session_secret_key="dev-session-key",
        session_cookie_secure=False,
        popup_allowed_frame_ancestors=["https://stream.kimsec.net"],
        database_path=root / "messages.db",
        twitch_client_id="client-id",
        twitch_broadcaster_id="broadcaster-id",
        twitch_tokens_path=root / "twitch_tokens.json",
        twitch_eventsub_ws_url="wss://eventsub.wss.twitch.tv/ws",
        youtube_client_secrets_file=None,
        youtube_token_path=root / "youtube_tokens.json",
        youtube_redirect_uri="http://127.0.0.1:8090/auth/youtube/callback",
        youtube_scopes=["https://www.googleapis.com/auth/youtube.readonly"],
        youtube_poll_fallback_sec=5,
        kick_client_id="",
        kick_client_secret="",
        kick_broadcaster_user_id="",
        kick_token_path=root / "kick_tokens.json",
        kick_redirect_uri="http://127.0.0.1:8090/auth/kick/callback",
        kick_scope="events:subscribe",
        template_dir=root,
        static_dir=root,
    )


def write_token(path: Path, token: str) -> None:
    path.write_text(json.dumps({"access_token": token}), encoding="utf-8")


def welcome_packet(session_id: str, keepalive_timeout: int = 10) -> SimpleNamespace:
    return SimpleNamespace(
        type=aiohttp.WSMsgType.TEXT,
        data=json.dumps(
            {
                "metadata": {"message_type": "session_welcome"},
                "payload": {
                    "session": {
                        "id": session_id,
                        "keepalive_timeout_seconds": keepalive_timeout,
                    }
                },
            }
        ),
    )


def reconnect_packet(reconnect_url: str) -> SimpleNamespace:
    return SimpleNamespace(
        type=aiohttp.WSMsgType.TEXT,
        data=json.dumps(
            {
                "metadata": {"message_type": "session_reconnect"},
                "payload": {"session": {"reconnect_url": reconnect_url}},
            }
        ),
    )


def keepalive_packet() -> SimpleNamespace:
    return SimpleNamespace(
        type=aiohttp.WSMsgType.TEXT,
        data=json.dumps(
            {
                "metadata": {"message_type": "session_keepalive"},
                "payload": {},
            }
        ),
    )


def notification_packet(subscription_type: str, event: dict, timestamp: str = "2026-04-06T10:00:00Z") -> SimpleNamespace:
    return SimpleNamespace(
        type=aiohttp.WSMsgType.TEXT,
        data=json.dumps(
            {
                "metadata": {
                    "message_type": "notification",
                    "message_timestamp": timestamp,
                    "subscription_type": subscription_type,
                },
                "payload": {
                    "subscription": {"type": subscription_type},
                    "event": event,
                },
            }
        ),
    )


def revocation_packet(subscription_type: str) -> SimpleNamespace:
    return SimpleNamespace(
        type=aiohttp.WSMsgType.TEXT,
        data=json.dumps(
            {
                "metadata": {"message_type": "revocation"},
                "payload": {"subscription": {"type": subscription_type}},
            }
        ),
    )


class FakeResponse:
    def __init__(
        self,
        status: int,
        text: str = "",
        headers: dict[str, str] | None = None,
        json_data: dict | None = None,
    ) -> None:
        self.status = status
        self._text = text
        self.headers = headers or {}
        self._json_data = json_data

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def text(self) -> str:
        return self._text

    async def json(self, content_type=None) -> dict:
        if self._json_data is not None:
            return self._json_data
        return {}


class FakePostSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append(
            {
                "url": url,
                "headers": headers or {},
                "json": json,
                "timeout": timeout,
            }
        )
        return self._responses.pop(0)

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append(
            {
                "url": url,
                "headers": headers or {},
                "params": params or {},
                "timeout": timeout,
                "method": "GET",
            }
        )
        return self._responses.pop(0)


class FakeWebSocket:
    def __init__(self, packets: list[object], connector: TwitchConnector | None = None) -> None:
        self._packets = list(packets)
        self._connector = connector

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def receive(self):
        if self._connector is not None and self._connector._stop_event.is_set():
            return keepalive_packet()
        if not self._packets:
            raise AssertionError("No more fake WebSocket packets available")
        packet = self._packets.pop(0)
        if isinstance(packet, Exception):
            raise packet
        return packet


class FakeClientSession:
    def __init__(self, websocket: FakeWebSocket) -> None:
        self.websocket = websocket
        self.ws_connect_calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def ws_connect(self, url, autoping=True):
        self.ws_connect_calls.append({"url": url, "autoping": autoping})
        return self.websocket

    def post(self, url, **kwargs):
        return FakeResponse(202, '{"data":[]}')


class TwitchConnectorTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.settings = make_settings(self.root)
        write_token(self.settings.twitch_tokens_path, "initial-token")
        self.store = MessageStore(self.settings.database_path)
        self.service = ChatService(self.store)
        self.connector = TwitchConnector(self.settings, self.service)

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def twitch_status(self):
        return next(status for status in self.service.get_statuses() if status.platform == "twitch")


    async def test_subscribe_chat_retries_once_on_401(self):
        session = FakePostSession(
            [
                FakeResponse(401, '{"error":"Unauthorized"}'),
                FakeResponse(202, '{"ok":true}'),
            ]
        )

        with mock.patch.object(
            self.connector,
            "_load_access_token",
            side_effect=["stale-token", "fresh-token"],
        ):
            result = await self.connector._subscribe_chat(session, "session-1")

        self.assertEqual(result.outcome, "ok")
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(
            session.calls[0]["headers"]["Authorization"],
            "Bearer stale-token",
        )
        self.assertEqual(
            session.calls[1]["headers"]["Authorization"],
            "Bearer fresh-token",
        )

    async def test_subscribe_chat_returns_rate_limit_result(self):
        session = FakePostSession(
            [
                FakeResponse(
                    429,
                    '{"error":"Too Many Requests"}',
                    headers={"Ratelimit-Reset": "145"},
                )
            ]
        )

        with mock.patch.object(self.connector, "_load_access_token", return_value="token"), mock.patch.object(
            twitch_module.time,
            "time",
            return_value=100.0,
        ):
            result = await self.connector._subscribe_chat(session, "session-1")

        self.assertEqual(result.outcome, "rate_limited")
        self.assertEqual(result.retry_at, 145.0)

    async def test_subscribe_chat_notification_uses_notification_type(self):
        session = FakePostSession([FakeResponse(202, '{"ok":true}')])

        with mock.patch.object(self.connector, "_load_access_token", return_value="token"):
            result = await self.connector._subscribe_chat_notification(session, "session-1")

        self.assertEqual(result.outcome, "ok")
        self.assertEqual(session.calls[0]["json"]["type"], self.connector.CHAT_NOTIFICATION_SUBSCRIPTION)

    async def test_subscribe_chat_message_delete_uses_delete_type(self):
        session = FakePostSession([FakeResponse(202, '{"ok":true}')])

        with mock.patch.object(self.connector, "_load_access_token", return_value="token"):
            result = await self.connector._subscribe_chat_message_delete(session, "session-1")

        self.assertEqual(result.outcome, "ok")
        self.assertEqual(session.calls[0]["json"]["type"], self.connector.CHAT_MESSAGE_DELETE_SUBSCRIPTION)

    async def test_hype_train_subscriptions_use_v2_and_broadcaster_only_condition(self):
        session = FakePostSession(
            [
                FakeResponse(202, '{"ok":true}'),
                FakeResponse(202, '{"ok":true}'),
                FakeResponse(202, '{"ok":true}'),
            ]
        )

        with mock.patch.object(self.connector, "_load_access_token", return_value="token"):
            await self.connector._subscribe_hype_train_begin(session, "session-1")
            await self.connector._subscribe_hype_train_progress(session, "session-1")
            await self.connector._subscribe_hype_train_end(session, "session-1")

        expected_types = [
            self.connector.HYPE_TRAIN_BEGIN,
            self.connector.HYPE_TRAIN_PROGRESS,
            self.connector.HYPE_TRAIN_END,
        ]
        for call, expected_type in zip(session.calls, expected_types):
            self.assertEqual(call["json"]["type"], expected_type)
            self.assertEqual(call["json"]["version"], "2")
            self.assertEqual(
                call["json"]["condition"],
                {"broadcaster_user_id": self.settings.twitch_broadcaster_id},
            )
            self.assertNotIn("user_id", call["json"]["condition"])

    async def test_run_moves_to_connected_after_welcome_and_subscribe(self):
        websocket = FakeWebSocket([welcome_packet("session-1")], connector=self.connector)
        session = FakeClientSession(websocket)

        async def fake_subscribe(_session, _session_id):
            return SubscribeResult("ok")

        async def fake_subscribe_notification(_session, _session_id):
            self.connector._stop_event.set()
            return SubscribeResult("ok")

        with mock.patch.object(self.connector, "_subscribe_chat", side_effect=fake_subscribe), mock.patch.object(
            self.connector,
            "_subscribe_chat_notification",
            side_effect=fake_subscribe_notification,
        ), mock.patch.object(
            twitch_module.aiohttp,
            "ClientSession",
            return_value=session,
        ):
            await self.connector.run()

        status = self.twitch_status()
        self.assertEqual(status.state, "connected")
        self.assertTrue(status.connected)
        self.assertEqual(
            session.ws_connect_calls,
            [{"url": self.settings.twitch_eventsub_ws_url, "autoping": True}],
        )

    async def test_run_uses_reconnect_url_from_twitch(self):
        reconnect_url = "wss://eventsub.wss.twitch.tv/ws?token=reconnect"
        session_one = FakeClientSession(
            FakeWebSocket(
                [
                    welcome_packet("session-1"),
                    reconnect_packet(reconnect_url),
                ],
                connector=self.connector,
            )
        )
        session_two = FakeClientSession(
            FakeWebSocket([welcome_packet("session-2")], connector=self.connector)
        )
        sessions = [session_one, session_two]
        subscribe_calls: list[str] = []
        notification_calls: list[str] = []

        async def fake_subscribe(_session, session_id):
            subscribe_calls.append(session_id)
            return SubscribeResult("ok")

        async def fake_subscribe_notification(_session, session_id):
            notification_calls.append(session_id)
            if len(notification_calls) == 2:
                self.connector._stop_event.set()
            return SubscribeResult("ok")

        with mock.patch.object(self.connector, "_subscribe_chat", side_effect=fake_subscribe), mock.patch.object(
            self.connector,
            "_subscribe_chat_notification",
            side_effect=fake_subscribe_notification,
        ), mock.patch.object(
            twitch_module.aiohttp,
            "ClientSession",
            side_effect=lambda: sessions.pop(0),
        ):
            await self.connector.run()

        self.assertEqual(subscribe_calls, ["session-1", "session-2"])
        self.assertEqual(notification_calls, ["session-1", "session-2"])
        self.assertEqual(
            session_one.ws_connect_calls,
            [{"url": self.settings.twitch_eventsub_ws_url, "autoping": True}],
        )
        self.assertEqual(
            session_two.ws_connect_calls,
            [{"url": reconnect_url, "autoping": True}],
        )

    async def test_run_sets_reconnecting_after_keepalive_timeout(self):
        session = FakeClientSession(
            FakeWebSocket([welcome_packet("session-1"), asyncio.TimeoutError()], connector=self.connector)
        )

        async def fake_sleep_or_stop(_seconds):
            self.connector._stop_event.set()
            return True

        with mock.patch.object(self.connector, "_subscribe_chat", return_value=SubscribeResult("ok")), mock.patch.object(
            self.connector,
            "_subscribe_chat_notification",
            return_value=SubscribeResult("ok"),
        ), mock.patch.object(
            self.connector,
            "sleep_or_stop",
            side_effect=fake_sleep_or_stop,
        ), mock.patch.object(
            twitch_module.aiohttp,
            "ClientSession",
            return_value=session,
        ):
            await self.connector.run()

        status = self.twitch_status()
        self.assertEqual(status.state, "reconnecting")
        self.assertFalse(status.connected)
        self.assertEqual(status.last_error, "Twitch keepalive timed out")

    async def test_run_waits_for_token_without_opening_socket(self):
        self.settings.twitch_tokens_path.unlink()

        async def fake_sleep_or_stop(_seconds):
            self.connector._stop_event.set()
            return True

        with mock.patch.object(
            self.connector,
            "sleep_or_stop",
            side_effect=fake_sleep_or_stop,
        ), mock.patch.object(
            twitch_module.aiohttp,
            "ClientSession",
            side_effect=AssertionError("ClientSession should not be opened when token is missing"),
        ):
            await self.connector.run()

        status = self.twitch_status()
        self.assertEqual(status.state, "waiting_for_token")
        self.assertFalse(status.connected)

    async def test_run_sets_auth_required_without_reconnecting_socket(self):
        session = FakeClientSession(FakeWebSocket([welcome_packet("session-1")], connector=self.connector))

        async def fake_subscribe(_session, _session_id):
            return SubscribeResult("auth_failed", detail="Twitch subscribe failed 401: unauthorized")

        async def fake_subscribe_notification(_session, _session_id):
            self.connector._stop_event.set()
            return SubscribeResult("ok")

        with mock.patch.object(self.connector, "_subscribe_chat", side_effect=fake_subscribe), mock.patch.object(
            self.connector,
            "_subscribe_chat_notification",
            side_effect=fake_subscribe_notification,
        ), mock.patch.object(
            self.connector,
            "_load_access_token",
            return_value="still-present-token",
        ), mock.patch.object(
            twitch_module.aiohttp,
            "ClientSession",
            return_value=session,
        ):
            await self.connector.run()

        status = self.twitch_status()
        self.assertEqual(status.state, "auth_required")
        self.assertFalse(status.connected)
        self.assertFalse(status.auth_ready)

    async def test_map_message_keeps_normal_twitch_messages_without_source_avatar(self):
        metadata = {"message_timestamp": "2026-04-06T10:00:00Z"}
        payload = {
            "event": {
                "message_id": "msg-1",
                "broadcaster_user_id": "111",
                "chatter_user_name": "Kim",
                "chatter_user_login": "kim",
                "message": {
                    "text": "hello",
                    "fragments": [{"type": "text", "text": "hello"}],
                },
                "badges": [],
            }
        }
        session = FakePostSession([])

        message = await self.connector._map_message(session, metadata, payload)

        self.assertIsNotNone(message)
        self.assertIsNone(message.avatar_url)
        self.assertEqual(session.calls, [])

    async def test_map_message_shared_chat_fetches_and_caches_source_avatar(self):
        metadata = {"message_timestamp": "2026-04-06T10:00:00Z"}
        payload = {
            "event": {
                "message_id": "msg-2",
                "broadcaster_user_id": "111",
                "source_broadcaster_user_id": "222",
                "source_broadcaster_user_name": "FriendStreamer",
                "source_broadcaster_user_login": "friendstreamer",
                "chatter_user_name": "Viewer",
                "chatter_user_login": "viewer",
                "message": {
                    "text": "shared hello",
                    "fragments": [{"type": "text", "text": "shared hello"}],
                },
                "badges": [],
            }
        }
        session = FakePostSession(
            [
                FakeResponse(
                    200,
                    json_data={
                        "data": [
                            {
                                "id": "222",
                                "login": "friendstreamer",
                                "display_name": "FriendStreamer",
                                "profile_image_url": "https://example.com/friend.jpg",
                            }
                        ]
                    },
                )
            ]
        )

        first = await self.connector._map_message(session, metadata, payload)
        second = await self.connector._map_message(session, metadata, payload)

        self.assertIsNotNone(first)
        self.assertEqual(first.avatar_url, "https://example.com/friend.jpg")
        self.assertEqual(second.avatar_url, "https://example.com/friend.jpg")
        get_calls = [call for call in session.calls if call.get("method") == "GET"]
        self.assertEqual(len(get_calls), 1)
        self.assertEqual(
            first.raw_payload["payload"]["event"]["source_broadcaster"]["name"],
            "FriendStreamer",
        )
        self.assertEqual(
            first.raw_payload["payload"]["event"]["source_broadcaster"]["login"],
            "friendstreamer",
        )

    async def test_map_message_shared_chat_still_publishes_without_avatar_on_lookup_failure(self):
        metadata = {"message_timestamp": "2026-04-06T10:00:00Z"}
        payload = {
            "event": {
                "message_id": "msg-3",
                "broadcaster_user_id": "111",
                "source_broadcaster_user_id": "333",
                "source_broadcaster_user_name": "BackupName",
                "source_broadcaster_user_login": "backuplogin",
                "chatter_user_name": "Viewer",
                "chatter_user_login": "viewer",
                "message": {
                    "text": "shared hello",
                    "fragments": [{"type": "text", "text": "shared hello"}],
                },
                "badges": [],
            }
        }
        session = FakePostSession([FakeResponse(500, "server error")])

        message = await self.connector._map_message(session, metadata, payload)

        self.assertIsNotNone(message)
        self.assertIsNone(message.avatar_url)
        self.assertEqual(
            message.raw_payload["payload"]["event"]["source_broadcaster"]["name"],
            "BackupName",
        )
        self.assertEqual(
            message.raw_payload["payload"]["event"]["source_broadcaster"]["login"],
            "backuplogin",
        )

    async def test_map_notification_message_creates_system_notice(self):
        metadata = {"message_timestamp": "2026-04-06T10:00:00Z"}
        payload = {
            "event": {
                "message_id": "notice-1",
                "broadcaster_user_id": "111",
                "chatter_user_name": "viewer23",
                "chatter_user_login": "viewer23",
                "system_message": "viewer23 subscribed at Tier 1.",
                "notice_type": "sub",
            }
        }
        session = FakePostSession([])

        message = await self.connector._map_notification_message(session, metadata, payload)

        self.assertIsNotNone(message)
        self.assertEqual(message.message_kind, "system")
        self.assertEqual(message.notice_type, "sub")
        self.assertEqual(message.text, "viewer23 subscribed at Tier 1.")

    def test_map_hype_train_event_normalizes_begin_progress_and_end(self):
        base_event = {
            "id": "train-1",
            "level": 2,
            "progress": 30,
            "goal": 100,
            "total": 130,
            "type": "golden_kappa",
            "started_at": "2026-04-06T10:00:00Z",
            "expires_at": "2026-04-06T10:05:00Z",
        }

        for subscription_type, expected_phase in (
            (self.connector.HYPE_TRAIN_BEGIN, "begin"),
            (self.connector.HYPE_TRAIN_PROGRESS, "progress"),
            (self.connector.HYPE_TRAIN_END, "end"),
        ):
            payload = {"event": dict(base_event, ended_at="2026-04-06T10:06:00Z")}
            result = self.connector._map_hype_train_event(subscription_type, {}, payload)
            self.assertIsNotNone(result)
            self.assertEqual(result["phase"], expected_phase)
            self.assertEqual(result["train_type"], "golden_kappa")
            self.assertEqual(result["progress"], 30)
            self.assertEqual(result["goal"], 100)

    async def test_get_hype_train_status_returns_normalized_active_train(self):
        session = FakePostSession(
            [
                FakeResponse(
                    200,
                    json_data={
                        "data": [
                            {
                                "id": "train-2",
                                "level": 4,
                                "progress": 75,
                                "goal": 100,
                                "total": 275,
                                "type": "golden_kappa",
                                "started_at": "2026-04-06T10:00:00Z",
                                "expires_at": "2026-04-06T10:05:00Z",
                            }
                        ]
                    },
                )
            ]
        )

        with mock.patch.object(self.connector, "_load_access_token", return_value="token"), mock.patch.object(
            twitch_module.aiohttp,
            "ClientSession",
            return_value=session,
        ):
            result = await self.connector.get_hype_train_status()

        self.assertIsNotNone(result)
        self.assertEqual(result["phase"], "progress")
        self.assertEqual(result["progress"], 75)
        self.assertEqual(result["goal"], 100)
        self.assertEqual(result["train_type"], "golden_kappa")
        self.assertEqual(session.calls[0]["params"]["broadcaster_id"], self.settings.twitch_broadcaster_id)

    async def test_get_hype_train_status_returns_none_for_empty_or_null_current(self):
        empty_session = FakePostSession([FakeResponse(200, json_data={"data": []})])
        null_session = FakePostSession([FakeResponse(200, json_data={"data": [{"current": None}]})])

        with mock.patch.object(self.connector, "_load_access_token", return_value="token"), mock.patch.object(
            twitch_module.aiohttp,
            "ClientSession",
            return_value=empty_session,
        ):
            empty_result = await self.connector.get_hype_train_status()

        with mock.patch.object(self.connector, "_load_access_token", return_value="token"), mock.patch.object(
            twitch_module.aiohttp,
            "ClientSession",
            return_value=null_session,
        ):
            null_result = await self.connector.get_hype_train_status()

        self.assertIsNone(empty_result)
        self.assertIsNone(null_result)

    async def test_map_notification_message_shared_chat_fetches_source_avatar(self):
        metadata = {"message_timestamp": "2026-04-06T10:00:00Z"}
        payload = {
            "event": {
                "message_id": "notice-2",
                "broadcaster_user_id": "111",
                "chatter_user_name": "viewer23",
                "chatter_user_login": "viewer23",
                "system_message": "viewer23 subscribed at Tier 1.",
                "notice_type": "shared_chat_sub",
                "source_broadcaster_user_id": "222",
                "source_broadcaster_user_name": "FriendStreamer",
                "source_broadcaster_user_login": "friendstreamer",
            }
        }
        session = FakePostSession(
            [
                FakeResponse(
                    200,
                    json_data={
                        "data": [
                            {
                                "id": "222",
                                "login": "friendstreamer",
                                "display_name": "FriendStreamer",
                                "profile_image_url": "https://example.com/friend.jpg",
                            }
                        ]
                    },
                )
            ]
        )

        message = await self.connector._map_notification_message(session, metadata, payload)

        self.assertIsNotNone(message)
        self.assertEqual(message.avatar_url, "https://example.com/friend.jpg")
        self.assertEqual(
            message.raw_payload["payload"]["event"]["source_broadcaster"]["name"],
            "FriendStreamer",
        )

    async def test_notification_revocation_keeps_primary_chat_connected(self):
        chat_event = {
            "message_id": "msg-1",
            "broadcaster_user_id": "111",
            "chatter_user_name": "Kim",
            "chatter_user_login": "kim",
            "message": {
                "text": "hello",
                "fragments": [{"type": "text", "text": "hello"}],
            },
            "badges": [],
        }
        websocket = FakeWebSocket(
            [
                welcome_packet("session-1"),
                notification_packet(self.connector.CHAT_MESSAGE_SUBSCRIPTION, chat_event),
                revocation_packet(self.connector.CHAT_NOTIFICATION_SUBSCRIPTION),
            ],
            connector=self.connector,
        )
        session = FakeClientSession(websocket)
        notification_subscribe_calls = 0

        async def fake_subscribe(_session, _session_id):
            return SubscribeResult("ok")

        async def fake_subscribe_notification(_session, _session_id):
            nonlocal notification_subscribe_calls
            notification_subscribe_calls += 1
            if notification_subscribe_calls >= 2:
                self.connector._stop_event.set()
            return SubscribeResult("ok")

        with mock.patch.object(self.connector, "_subscribe_chat", side_effect=fake_subscribe), mock.patch.object(
            self.connector,
            "_subscribe_chat_notification",
            side_effect=fake_subscribe_notification,
        ), mock.patch.object(
            twitch_module.aiohttp,
            "ClientSession",
            return_value=session,
        ):
            await self.connector.run()

        status = self.twitch_status()
        self.assertEqual(status.state, "connected")
        self.assertTrue(status.connected)
        self.assertGreaterEqual(notification_subscribe_calls, 2)

    async def test_message_delete_notification_marks_existing_message_deleted(self):
        delete_event = {
            "broadcaster_user_id": "111",
            "target_user_id": "222",
            "target_user_name": "Kim",
            "target_user_login": "kim",
            "message_id": "msg-1",
        }
        websocket = FakeWebSocket(
            [
                welcome_packet("session-1"),
                notification_packet(
                    self.connector.CHAT_MESSAGE_DELETE_SUBSCRIPTION,
                    delete_event,
                    timestamp="2026-04-06T10:01:00Z",
                ),
            ],
            connector=self.connector,
        )
        session = FakeClientSession(websocket)

        async def fake_mark_deleted(*_args):
            self.connector._stop_event.set()
            return True

        self.service.mark_message_deleted = mock.AsyncMock(side_effect=fake_mark_deleted)

        with mock.patch.object(self.connector, "_subscribe_chat", return_value=SubscribeResult("ok")), mock.patch.object(
            self.connector,
            "_subscribe_chat_notification",
            return_value=SubscribeResult("ok"),
        ), mock.patch.object(
            self.connector,
            "_subscribe_chat_message_delete",
            return_value=SubscribeResult("ok"),
        ), mock.patch.object(
            twitch_module.aiohttp,
            "ClientSession",
            return_value=session,
        ):
            await self.connector.run()

        self.service.mark_message_deleted.assert_awaited_once()
        args = self.service.mark_message_deleted.await_args.args
        self.assertEqual(args[0], "twitch")
        self.assertEqual(args[1], "msg-1")
        self.assertEqual(args[2].isoformat(), "2026-04-06T10:01:00+00:00")

    async def test_message_delete_revocation_keeps_primary_chat_connected(self):
        chat_event = {
            "message_id": "msg-1",
            "broadcaster_user_id": "111",
            "chatter_user_name": "Kim",
            "chatter_user_login": "kim",
            "message": {
                "text": "hello",
                "fragments": [{"type": "text", "text": "hello"}],
            },
            "badges": [],
        }
        websocket = FakeWebSocket(
            [
                welcome_packet("session-1"),
                notification_packet(self.connector.CHAT_MESSAGE_SUBSCRIPTION, chat_event),
                revocation_packet(self.connector.CHAT_MESSAGE_DELETE_SUBSCRIPTION),
            ],
            connector=self.connector,
        )
        session = FakeClientSession(websocket)
        delete_subscribe_calls = 0

        async def fake_subscribe(_session, _session_id):
            return SubscribeResult("ok")

        async def fake_subscribe_delete(_session, _session_id):
            nonlocal delete_subscribe_calls
            delete_subscribe_calls += 1
            if delete_subscribe_calls >= 2:
                self.connector._stop_event.set()
            return SubscribeResult("ok")

        with mock.patch.object(self.connector, "_subscribe_chat", side_effect=fake_subscribe), mock.patch.object(
            self.connector,
            "_subscribe_chat_notification",
            side_effect=fake_subscribe,
        ), mock.patch.object(
            self.connector,
            "_subscribe_chat_message_delete",
            side_effect=fake_subscribe_delete,
        ), mock.patch.object(
            twitch_module.aiohttp,
            "ClientSession",
            return_value=session,
        ):
            await self.connector.run()

        status = self.twitch_status()
        self.assertEqual(status.state, "connected")
        self.assertTrue(status.connected)
        self.assertGreaterEqual(delete_subscribe_calls, 2)


if __name__ == "__main__":
    unittest.main()
