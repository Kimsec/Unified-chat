import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import unified_chat.connectors.kick as kick_module
from unified_chat.config import Settings
from unified_chat.connectors.kick import KickConnector
from unified_chat.connectors.youtube import YouTubeConnector
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


class FakeYouTubeCredentials:
    def __init__(self, payload: dict[str, str]) -> None:
        self._payload = payload

    def to_json(self) -> str:
        return json.dumps(self._payload)


class FakeYouTubeFlow:
    def __init__(self, *, authorization_url: str, state: str, code_verifier: str) -> None:
        self._authorization_url = authorization_url
        self._state = state
        self.code_verifier = code_verifier
        self.credentials = FakeYouTubeCredentials({"token": "youtube-token"})
        self.fetch_calls: list[dict] = []

    def authorization_url(self, **kwargs):
        return self._authorization_url, self._state

    def fetch_token(self, **kwargs):
        self.fetch_calls.append(kwargs)


class FakeKickResponse:
    def __init__(self, status: int, payload: dict) -> None:
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self, content_type=None):
        return self._payload


class FakeKickClientSession:
    def __init__(self, response: FakeKickResponse) -> None:
        self._response = response
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def post(self, url, data=None, headers=None):
        self.calls.append({"url": url, "data": data, "headers": headers})
        return self._response


class OAuthConnectorTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.settings = make_settings(self.root)
        self.settings.youtube_client_secrets_file.write_text(
            json.dumps(
                {
                    "web": {
                        "client_id": "test-client-id",
                        "client_secret": "test-client-secret",
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                        "token_uri": "https://oauth2.googleapis.com/token",
                    }
                }
            ),
            encoding="utf-8",
        )
        self.store = MessageStore(self.settings.database_path)
        self.service = ChatService(self.store)
        self.youtube = YouTubeConnector(self.settings, self.service)
        self.kick = KickConnector(self.settings, self.service)

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    async def test_youtube_authorization_persists_code_verifier(self):
        flow = FakeYouTubeFlow(
            authorization_url="https://accounts.google.com/o/oauth2/auth?state=yt-state",
            state="yt-state",
            code_verifier="yt-verifier",
        )

        with mock.patch.object(self.youtube, "_build_flow", return_value=flow):
            url = self.youtube.get_authorization_url()

        self.assertEqual(url, "https://accounts.google.com/o/oauth2/auth?state=yt-state")
        pending = self.youtube._pending_oauth.load()
        self.assertEqual(
            pending,
            {
                "state": "yt-state",
                "code_verifier": "yt-verifier",
            },
        )

    async def test_youtube_callback_uses_saved_code_verifier(self):
        self.youtube._pending_oauth.save({"state": "yt-state", "code_verifier": "yt-verifier"})
        flow = FakeYouTubeFlow(
            authorization_url="https://accounts.google.com/o/oauth2/auth?state=yt-state",
            state="yt-state",
            code_verifier="unused-in-callback",
        )
        build_calls: list[dict] = []

        def build_flow(*, state=None, code_verifier=None):
            build_calls.append({"state": state, "code_verifier": code_verifier})
            return flow

        with mock.patch.object(self.youtube, "_build_flow", side_effect=build_flow):
            await self.youtube.complete_authorization(
                "https://unified-chat.kimsec.net/auth/youtube/callback?state=yt-state&code=abc",
                "yt-state",
            )

        self.assertEqual(
            build_calls,
            [{"state": "yt-state", "code_verifier": "yt-verifier"}],
        )
        self.assertEqual(
            flow.fetch_calls,
            [
                {
                    "authorization_response": "https://unified-chat.kimsec.net/auth/youtube/callback?state=yt-state&code=abc"
                }
            ],
        )
        self.assertTrue(self.settings.youtube_token_path.exists())
        self.assertIsNone(self.youtube._pending_oauth.load())

    async def test_kick_authorization_persists_code_verifier(self):
        url = await self.kick.get_authorization_url()

        self.assertIn("state=", url)
        self.assertIn("code_challenge=", url)
        pending = self.kick._pending_oauth.load()
        self.assertIsNotNone(pending)
        self.assertTrue(pending["state"])
        self.assertTrue(pending["code_verifier"])

    async def test_kick_callback_uses_saved_code_verifier(self):
        self.kick._pending_oauth.save({"state": "kick-state", "code_verifier": "kick-verifier"})
        session = FakeKickClientSession(
            FakeKickResponse(
                200,
                {
                    "access_token": "kick-access-token",
                    "refresh_token": "kick-refresh-token",
                    "expires_in": 3600,
                },
            )
        )

        with mock.patch.object(
            kick_module.aiohttp,
            "ClientSession",
            return_value=session,
        ):
            await self.kick.complete_authorization("kick-code", "kick-state")

        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.calls[0]["data"]["code_verifier"], "kick-verifier")
        self.assertTrue(self.settings.kick_token_path.exists())
        self.assertIsNone(self.kick._pending_oauth.load())


if __name__ == "__main__":
    unittest.main()
