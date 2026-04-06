import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from unified_chat.config import Settings
from unified_chat.connectors.kick import KickConnector
from unified_chat.service import ChatService
from unified_chat.store import MessageStore
from unified_chat.utils import utcnow


def make_settings(root: Path) -> Settings:
    return Settings(
        project_dir=root,
        host="127.0.0.1",
        port=8090,
        app_base_url="https://unified-chat.kimsec.net",
        log_level="info",
        login_password_hash="",
        session_secret_key="dev-session-key",
        session_cookie_secure=True,
        popup_allowed_frame_ancestors=["https://stream.kimsec.net"],
        database_path=root / "messages.db",
        twitch_client_id="client-id",
        twitch_broadcaster_id="broadcaster-id",
        twitch_tokens_path=root / "twitch_tokens.json",
        twitch_eventsub_ws_url="wss://eventsub.wss.twitch.tv/ws",
        youtube_client_secrets_file=root / "google-client-secret.json",
        youtube_token_path=root / "youtube_tokens.json",
        youtube_redirect_uri="https://unified-chat.kimsec.net/auth/youtube/callback",
        youtube_scopes=["https://www.googleapis.com/auth/youtube.readonly"],
        youtube_poll_fallback_sec=5,
        kick_client_id="kick-client-id",
        kick_client_secret="kick-client-secret",
        kick_broadcaster_user_id="13397451",
        kick_token_path=root / "kick_tokens.json",
        kick_redirect_uri="https://unified-chat.kimsec.net/auth/kick/callback",
        kick_scope="events:subscribe",
        template_dir=root,
        static_dir=root,
    )


class KickConnectorStatusTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.settings = make_settings(self.root)
        self.store = MessageStore(self.settings.database_path)
        self.service = ChatService(self.store)
        self.connector = KickConnector(self.settings, self.service)

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def kick_status(self):
        return next(status for status in self.service.get_statuses() if status.platform == "kick")

    async def test_run_waits_for_webhook_until_one_arrives(self):
        with (
            mock.patch.object(self.connector, "_get_subscription_token", return_value=("kick-token", "app")),
            mock.patch.object(self.connector, "_ensure_chat_subscription", return_value=None),
            mock.patch.object(self.connector, "sleep_or_stop", side_effect=[True]),
        ):
            await self.connector.run()

        status = self.kick_status()
        self.assertEqual(status.state, "idle")
        self.assertFalse(status.connected)
        self.assertEqual(status.detail, "Waiting for someone to chat")
        self.assertIsNone(status.last_event_at)

    async def test_handle_webhook_marks_kick_connected(self):
        payload = {
            "message_id": "kick-msg-1",
            "content": "Hello from Kick",
            "created_at": "2026-04-05T12:50:00Z",
            "sender": {
                "username": "Kim",
                "channel_slug": "kim",
                "identity": {"badges": [], "username_color": "#53fc18"},
            },
            "broadcaster": {"user_id": 13397451},
        }
        headers = {"Kick-Event-Type": "chat.message.sent"}

        with mock.patch("unified_chat.connectors.kick.verify_kick_signature", return_value=None):
            unified = await self.connector.handle_webhook(headers, json.dumps(payload).encode("utf-8"))

        status = self.kick_status()
        self.assertIsNotNone(unified)
        self.assertEqual(status.state, "connected")
        self.assertTrue(status.connected)
        self.assertEqual(status.detail, "Listening for chat messages")
        self.assertIsNotNone(status.last_event_at)

    async def test_delivery_status_returns_idle_when_webhooks_are_stale(self):
        self.connector._webhook_seen = True
        with mock.patch("unified_chat.connectors.kick.utcnow") as mocked_now:
            now = utcnow()
            stale_at = now - self.connector.WEBHOOK_ACTIVITY_WINDOW - timedelta(seconds=1)
            self.connector._last_webhook_at = stale_at

            mocked_now.return_value = now
            state, detail, connected = self.connector._delivery_status()

        self.assertEqual(state, "idle")
        self.assertFalse(connected)
        self.assertEqual(detail, "Waiting for someone to chat")

    async def test_ensure_chat_subscription_refreshes_existing_subscription_once(self):
        class FakeResponse:
            def __init__(self, status, payload=None, text=""):
                self.status = status
                self._payload = payload
                self._text = text

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def json(self, content_type=None):
                return self._payload

            async def text(self):
                return self._text

        class FakeSession:
            def __init__(self):
                self.get_calls = []
                self.delete_calls = []
                self.post_calls = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            def get(self, url, params=None):
                self.get_calls.append((url, params))
                return FakeResponse(
                    200,
                    {
                        "data": [
                            {
                                "id": "sub-1",
                                "event": "chat.message.sent",
                                "version": 1,
                                "broadcaster_user_id": 13397451,
                            }
                        ]
                    },
                )

            def delete(self, url, params=None):
                self.delete_calls.append((url, params))
                return FakeResponse(204, None)

            def post(self, url, json=None):
                self.post_calls.append((url, json))
                return FakeResponse(200, {"ok": True})

        fake_session = FakeSession()

        with mock.patch("unified_chat.connectors.kick.aiohttp.ClientSession", return_value=fake_session):
            await self.connector._ensure_chat_subscription("kick-token", "app")

        self.assertEqual(len(fake_session.get_calls), 1)
        self.assertEqual(len(fake_session.delete_calls), 1)
        self.assertEqual(len(fake_session.post_calls), 1)
        self.assertEqual(fake_session.delete_calls[0][1], [("id", "sub-1")])
        self.assertEqual(fake_session.post_calls[0][1]["broadcaster_user_id"], 13397451)
        self.assertTrue(self.connector._subscription_refreshed)

    async def test_ensure_chat_subscription_skips_recreate_after_refresh(self):
        class FakeResponse:
            def __init__(self, status, payload=None):
                self.status = status
                self._payload = payload

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def json(self, content_type=None):
                return self._payload

            async def text(self):
                return ""

        class FakeSession:
            def __init__(self):
                self.get_calls = []
                self.delete_calls = []
                self.post_calls = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            def get(self, url, params=None):
                self.get_calls.append((url, params))
                return FakeResponse(
                    200,
                    {
                        "data": [
                            {
                                "id": "sub-1",
                                "event": "chat.message.sent",
                                "version": 1,
                                "broadcaster_user_id": 13397451,
                            }
                        ]
                    },
                )

            def delete(self, url, params=None):
                self.delete_calls.append((url, params))
                return FakeResponse(204, None)

            def post(self, url, json=None):
                self.post_calls.append((url, json))
                return FakeResponse(200, {"ok": True})

        self.connector._subscription_refreshed = True
        fake_session = FakeSession()

        with mock.patch("unified_chat.connectors.kick.aiohttp.ClientSession", return_value=fake_session):
            await self.connector._ensure_chat_subscription("kick-token", "app")

        self.assertEqual(len(fake_session.get_calls), 1)
        self.assertEqual(len(fake_session.delete_calls), 0)
        self.assertEqual(len(fake_session.post_calls), 0)


if __name__ == "__main__":
    unittest.main()
