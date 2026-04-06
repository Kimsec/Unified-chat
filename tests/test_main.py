import unittest
from types import SimpleNamespace
from unittest import mock
from urllib.parse import quote

from starlette.requests import Request

from unified_chat.main import (
    app,
    auth_youtube_start,
    get_messages,
    index,
    login_page,
    login_submit,
    popout,
    settings,
    websocket_chat,
)


def make_request(
    *,
    method: str = "GET",
    path: str = "/",
    body: bytes = b"",
    session: dict | None = None,
    app_obj=app,
    host: str = "127.0.0.1",
    query_string: bytes = b"",
):
    async def receive():
        nonlocal body
        payload = body
        body = b""
        return {"type": "http.request", "body": payload, "more_body": False}

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "root_path": "",
        "query_string": query_string,
        "headers": [
            (b"content-type", b"application/x-www-form-urlencoded"),
            (b"host", quote(host).encode("utf-8")),
        ],
        "client": ("127.0.0.1", 12345),
        "server": (host, 8090),
        "app": app_obj,
        "session": session if session is not None else {},
    }
    return Request(scope, receive=receive)


class FakeWebSocket:
    def __init__(self, *, session: dict | None = None, host: str = "127.0.0.1"):
        self.session = session if session is not None else {}
        self.closed_code = None
        self.url = SimpleNamespace(hostname=host)

    async def close(self, code: int):
        self.closed_code = code


class MainRouteTest(unittest.IsolatedAsyncioTestCase):
    async def test_index_renders_template_response_when_auth_disabled(self):
        request = make_request(path="/")

        with mock.patch.object(settings, "login_password_hash", ""):
            response = await index(request)

        self.assertEqual(response.status_code, 200)
        body = response.body.decode("utf-8")
        self.assertIn("Unified Chat", body)
        self.assertIn(settings.app_base_url, body)
        self.assertIn("/static/styles.css", body)
        self.assertIsNone(response.headers.get("content-security-policy"))

    async def test_index_redirects_to_login_when_auth_enabled_and_unauthenticated(self):
        request = make_request(path="/", session={}, host="unified-chat.kimsec.net")

        with mock.patch.object(settings, "login_password_hash", "hashed"):
            response = await index(request)

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login")

    async def test_index_allows_local_host_without_login_even_when_auth_enabled(self):
        request = make_request(path="/", session={}, host="192.168.25.5")

        with mock.patch.object(settings, "login_password_hash", "hashed"):
            response = await index(request)

        self.assertEqual(response.status_code, 200)

    async def test_login_submit_sets_session_on_success(self):
        session = {}
        request = make_request(
            method="POST",
            path="/login",
            body=b"password=secret",
            session=session,
            host="unified-chat.kimsec.net",
        )

        with (
            mock.patch.object(settings, "login_password_hash", "hashed"),
            mock.patch("unified_chat.main.check_password_hash", return_value=True),
        ):
            response = await login_submit(request)

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/")
        self.assertTrue(session.get("authenticated"))

    async def test_login_submit_rejects_invalid_password(self):
        session = {}
        request = make_request(
            method="POST",
            path="/login",
            body=b"password=wrong",
            session=session,
            host="unified-chat.kimsec.net",
        )

        with (
            mock.patch.object(settings, "login_password_hash", "hashed"),
            mock.patch("unified_chat.main.check_password_hash", return_value=False),
        ):
            response = await login_submit(request)

        self.assertEqual(response.status_code, 401)
        self.assertNotIn("authenticated", session)
        self.assertIn("Incorrect password.", response.body.decode("utf-8"))

    async def test_get_messages_requires_auth_when_enabled(self):
        request = make_request(path="/api/messages", session={}, host="unified-chat.kimsec.net")

        with mock.patch.object(settings, "login_password_hash", "hashed"):
            with self.assertRaisesRegex(Exception, "Authentication required"):
                await get_messages(request)

    async def test_auth_youtube_start_redirects_to_login_when_unauthenticated(self):
        fake_app = SimpleNamespace(state=SimpleNamespace(runtime=None))
        request = make_request(
            path="/auth/youtube/start",
            session={},
            app_obj=fake_app,
            host="unified-chat.kimsec.net",
        )

        with mock.patch.object(settings, "login_password_hash", "hashed"):
            response = await auth_youtube_start(request)

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login")

    async def test_login_page_does_not_get_popout_embed_headers(self):
        request = make_request(path="/login", session={}, host="unified-chat.kimsec.net")

        with mock.patch.object(settings, "login_password_hash", "hashed"):
            response = await login_page(request)

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.headers.get("content-security-policy"))
        self.assertIsNone(response.headers.get("x-frame-options"))

    async def test_popout_sets_embed_headers_for_stream_control(self):
        request = make_request(path="/popout")

        with mock.patch.object(settings, "popup_allowed_frame_ancestors", ["https://stream.kimsec.net"]):
            response = await popout(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers.get("content-security-policy"),
            "frame-ancestors 'self' https://stream.kimsec.net",
        )
        self.assertIsNone(response.headers.get("x-frame-options"))

    async def test_popout_passes_platform_names_override_to_template(self):
        request = make_request(path="/popout", query_string=b"platform_names=0")

        response = await popout(request)

        body = response.body.decode("utf-8")
        self.assertIn('platformNamesOverride: "0"', body)

    async def test_popout_redirects_to_login_when_auth_enabled_and_unauthenticated(self):
        request = make_request(path="/popout", session={}, host="unified-chat.kimsec.net")

        with mock.patch.object(settings, "login_password_hash", "hashed"):
            response = await popout(request)

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login")
        self.assertIsNone(response.headers.get("content-security-policy"))

    async def test_websocket_chat_closes_when_unauthenticated(self):
        websocket = FakeWebSocket(session={}, host="unified-chat.kimsec.net")

        with mock.patch.object(settings, "login_password_hash", "hashed"):
            await websocket_chat(websocket)

        self.assertEqual(websocket.closed_code, 1008)

    async def test_websocket_chat_allows_local_host_without_login(self):
        websocket = FakeWebSocket(session={}, host="127.0.0.1")

        fake_runtime = SimpleNamespace(
            service=SimpleNamespace(
                hub=SimpleNamespace(connect=mock.AsyncMock(), disconnect=mock.Mock()),
                bootstrap_event=mock.AsyncMock(return_value={"type": "bootstrap", "messages": [], "statuses": []}),
            )
        )

        websocket.app = SimpleNamespace(state=SimpleNamespace(runtime=fake_runtime))
        websocket.send_json = mock.AsyncMock()
        websocket.receive_text = mock.AsyncMock(side_effect=Exception("stop"))

        with mock.patch.object(settings, "login_password_hash", "hashed"):
            await websocket_chat(websocket)

        self.assertIsNone(websocket.closed_code)


if __name__ == "__main__":
    unittest.main()
