import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from unified_chat.config import Settings
from unified_chat.connectors.youtube import YouTubeApiError, YouTubeConnector
from unified_chat.service import ChatService
from unified_chat.store import MessageStore


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


class YouTubeConnectorSelectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.settings = make_settings(self.root)
        self.settings.youtube_client_secrets_file.write_text(
            '{"web":{"client_id":"test","client_secret":"test","auth_uri":"https://accounts.google.com/o/oauth2/auth","token_uri":"https://oauth2.googleapis.com/token"}}',
            encoding="utf-8",
        )
        self.store = MessageStore(self.settings.database_path)
        self.service = ChatService(self.store)
        self.connector = YouTubeConnector(self.settings, self.service)

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def test_select_broadcast_prefers_live_with_chat(self):
        items = [
            {
                "snippet": {"liveChatId": "chat-starting"},
                "status": {"lifeCycleStatus": "liveStarting"},
            },
            {
                "snippet": {"liveChatId": "chat-live"},
                "status": {"lifeCycleStatus": "live"},
            },
        ]

        selected, detail = self.connector._select_broadcast(items)

        self.assertEqual(detail, "")
        self.assertIsNotNone(selected)
        self.assertEqual(selected["snippet"]["liveChatId"], "chat-live")

    def test_select_broadcast_uses_live_starting_as_fallback(self):
        items = [
            {
                "snippet": {"liveChatId": "chat-starting"},
                "status": {"lifeCycleStatus": "liveStarting"},
            }
        ]

        selected, detail = self.connector._select_broadcast(items)

        self.assertEqual(detail, "")
        self.assertIsNotNone(selected)
        self.assertEqual(selected["snippet"]["liveChatId"], "chat-starting")

    def test_select_broadcast_returns_idle_when_no_live_chat_available(self):
        items = [
            {
                "snippet": {},
                "status": {"lifeCycleStatus": "live"},
            },
            {
                "snippet": {},
                "status": {"lifeCycleStatus": "liveStarting"},
            },
        ]

        selected, detail = self.connector._select_broadcast(items)

        self.assertIsNone(selected)
        self.assertEqual(detail, "No active YouTube live chat available yet")

    def test_select_broadcast_returns_idle_when_no_active_broadcasts(self):
        items = [
            {
                "snippet": {"liveChatId": "chat-complete"},
                "status": {"lifeCycleStatus": "complete"},
            },
            {
                "snippet": {"liveChatId": "chat-created"},
                "status": {"lifeCycleStatus": "created"},
            },
        ]

        selected, detail = self.connector._select_broadcast(items)

        self.assertIsNone(selected)
        self.assertEqual(detail, "Waiting for stream to go live")

    def test_broadcast_lookup_params_use_mine_without_broadcast_status(self):
        params = {
            "part": "snippet,status",
            "broadcastType": "all",
            "mine": "true",
            "maxResults": 10,
        }

        self.assertEqual(params["mine"], "true")
        self.assertNotIn("broadcastStatus", params)
        self.assertEqual(params["part"], "snippet,status")
        self.assertEqual(params["maxResults"], 10)

    def test_chat_messages_url_matches_youtube_live_chat_list_endpoint(self):
        self.assertEqual(
            self.connector.CHAT_MESSAGES_URL,
            "https://www.googleapis.com/youtube/v3/liveChat/messages",
        )


class YouTubeConnectorRunLoopTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.settings = make_settings(self.root)
        self.settings.youtube_client_secrets_file.write_text(
            '{"web":{"client_id":"test","client_secret":"test","auth_uri":"https://accounts.google.com/o/oauth2/auth","token_uri":"https://oauth2.googleapis.com/token"}}',
            encoding="utf-8",
        )
        self.store = MessageStore(self.settings.database_path)
        self.service = ChatService(self.store)
        self.connector = YouTubeConnector(self.settings, self.service)

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    async def test_run_polls_broadcasts_every_30_seconds_until_live(self):
        fake_credentials = SimpleNamespace(token="youtube-token")
        authorized_get = mock.AsyncMock(return_value={"items": []})

        with (
            mock.patch.object(self.connector, "_load_credentials", return_value=fake_credentials),
            mock.patch.object(self.connector, "_authorized_get", authorized_get),
            mock.patch.object(self.connector, "sleep_or_stop", side_effect=[True]) as sleep_or_stop,
        ):
            await self.connector.run()

        self.assertEqual(authorized_get.await_count, 1)
        self.assertEqual(authorized_get.await_args_list[0].args[2], self.connector.BROADCASTS_URL)
        self.assertEqual(sleep_or_stop.await_args_list[0].args[0], self.connector.DISCOVERY_POLL_SEC)

    async def test_run_discovers_live_chat_once_then_polls_messages_without_rediscovery(self):
        fake_credentials = SimpleNamespace(token="youtube-token")
        call_urls: list[str] = []

        async def fake_authorized_get(session, access_token, url, params):
            call_urls.append(url)
            if url == self.connector.BROADCASTS_URL:
                return {
                    "items": [
                        {
                            "snippet": {"liveChatId": "chat-live"},
                            "status": {"lifeCycleStatus": "live"},
                        }
                    ]
                }
            return {
                "items": [],
                "nextPageToken": "next-page",
                "pollingIntervalMillis": 1000,
            }

        with (
            mock.patch.object(self.connector, "_load_credentials", return_value=fake_credentials),
            mock.patch.object(self.connector, "_authorized_get", side_effect=fake_authorized_get),
            mock.patch.object(self.connector, "sleep_or_stop", side_effect=[False, True]) as sleep_or_stop,
        ):
            await self.connector.run()

        self.assertEqual(
            call_urls,
            [
                self.connector.BROADCASTS_URL,
                self.connector.CHAT_MESSAGES_URL,
                self.connector.CHAT_MESSAGES_URL,
            ],
        )
        self.assertEqual(sleep_or_stop.await_args_list[0].args[0], self.connector.CHAT_POLL_SEC)
        self.assertEqual(sleep_or_stop.await_args_list[1].args[0], self.connector.CHAT_POLL_SEC)

    async def test_run_returns_to_discovery_after_chat_404(self):
        fake_credentials = SimpleNamespace(token="youtube-token")
        call_urls: list[str] = []
        responses = iter(
            [
                {
                    "items": [
                        {
                            "snippet": {"liveChatId": "chat-live"},
                            "status": {"lifeCycleStatus": "live"},
                        }
                    ]
                },
                YouTubeApiError(404, None),
                {"items": []},
            ]
        )

        async def fake_authorized_get(session, access_token, url, params):
            call_urls.append(url)
            response = next(responses)
            if isinstance(response, Exception):
                raise response
            return response

        with (
            mock.patch.object(self.connector, "_load_credentials", return_value=fake_credentials),
            mock.patch.object(self.connector, "_authorized_get", side_effect=fake_authorized_get),
            mock.patch.object(self.connector, "sleep_or_stop", side_effect=[False, True]) as sleep_or_stop,
        ):
            await self.connector.run()

        self.assertEqual(
            call_urls,
            [
                self.connector.BROADCASTS_URL,
                self.connector.CHAT_MESSAGES_URL,
                self.connector.BROADCASTS_URL,
            ],
        )
        self.assertEqual(sleep_or_stop.await_args_list[0].args[0], self.connector.DISCOVERY_POLL_SEC)
        self.assertEqual(sleep_or_stop.await_args_list[1].args[0], self.connector.DISCOVERY_POLL_SEC)
        status = next(status for status in self.service.get_statuses() if status.platform == "youtube")
        self.assertEqual(status.state, "idle")

    async def test_run_handles_quota_exceeded_without_traceback_spam(self):
        fake_credentials = SimpleNamespace(token="youtube-token")
        quota_error = YouTubeApiError(
            403,
            {
                "error": {
                    "code": 403,
                    "errors": [
                        {
                            "reason": "quotaExceeded",
                        }
                    ],
                }
            },
        )

        with (
            mock.patch.object(self.connector, "_load_credentials", return_value=fake_credentials),
            mock.patch.object(self.connector, "_discover_live_chat_id", side_effect=quota_error),
            mock.patch.object(self.connector, "_seconds_until_quota_reset", return_value=120.0),
            mock.patch.object(self.connector, "sleep_or_stop", side_effect=[True]) as sleep_or_stop,
            mock.patch.object(self.connector.log, "warning") as log_warning,
            mock.patch.object(self.connector.log, "exception") as log_exception,
        ):
            await self.connector.run()

        log_warning.assert_called_once()
        log_exception.assert_not_called()
        self.assertEqual(sleep_or_stop.await_args_list[0].args[0], 120.0)
        status = next(status for status in self.service.get_statuses() if status.platform == "youtube")
        self.assertEqual(status.state, "rate_limited")
        self.assertEqual(status.detail, "YouTube quota exceeded; waiting for quota reset")
        self.assertEqual(status.last_error, "quotaExceeded")


if __name__ == "__main__":
    unittest.main()
