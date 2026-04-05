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


class FakeResponse:
    def __init__(self, status: int, text: str = "", headers: dict[str, str] | None = None) -> None:
        self.status = status
        self._text = text
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def text(self) -> str:
        return self._text


class FakePostSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

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

    async def test_run_moves_to_connected_after_welcome_and_subscribe(self):
        websocket = FakeWebSocket([welcome_packet("session-1")], connector=self.connector)
        session = FakeClientSession(websocket)

        async def fake_subscribe(_session, _session_id):
            self.connector._stop_event.set()
            return SubscribeResult("ok")

        with mock.patch.object(self.connector, "_subscribe_chat", side_effect=fake_subscribe), mock.patch.object(
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

        async def fake_subscribe(_session, session_id):
            subscribe_calls.append(session_id)
            if len(subscribe_calls) == 2:
                self.connector._stop_event.set()
            return SubscribeResult("ok")

        with mock.patch.object(self.connector, "_subscribe_chat", side_effect=fake_subscribe), mock.patch.object(
            twitch_module.aiohttp,
            "ClientSession",
            side_effect=lambda: sessions.pop(0),
        ):
            await self.connector.run()

        self.assertEqual(subscribe_calls, ["session-1", "session-2"])
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
            self.connector._stop_event.set()
            return SubscribeResult("auth_failed", detail="Twitch subscribe failed 401: unauthorized")

        with mock.patch.object(self.connector, "_subscribe_chat", side_effect=fake_subscribe), mock.patch.object(
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


if __name__ == "__main__":
    unittest.main()
