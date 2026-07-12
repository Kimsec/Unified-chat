from __future__ import annotations

import ipaddress
import logging
import secrets
import time
from contextlib import asynccontextmanager
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from werkzeug.security import check_password_hash

from unified_chat.config import Settings, load_settings
from unified_chat.connectors import KickConnector, TwitchConnector, YouTubeConnector
from unified_chat.models import DeleteMessageRequest, ModerationRequest, ReplyRequest
from unified_chat.service import ChatService
from unified_chat.store import MessageStore
from unified_chat.utils import utcnow

log = logging.getLogger("unified_chat.main")


class Runtime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = MessageStore(settings.database_path)
        self.service = ChatService(self.store)
        self.twitch = TwitchConnector(settings, self.service)
        self.youtube = YouTubeConnector(settings, self.service)
        self.kick = KickConnector(settings, self.service)
        self.connectors = [self.twitch, self.youtube, self.kick]
        self._next_hype_train_backfill_at = 0.0
        self._hype_train_backfill_ttl_sec = 15.0

    async def start(self) -> None:
        for connector in self.connectors:
            await connector.start()

    async def stop(self) -> None:
        for connector in reversed(self.connectors):
            await connector.stop()
        self.store.close()

    async def maybe_backfill_hype_train(self) -> None:
        if self.service.get_hype_train() is not None:
            return

        now = time.monotonic()
        if now < self._next_hype_train_backfill_at:
            return
        self._next_hype_train_backfill_at = now + self._hype_train_backfill_ttl_sec

        try:
            hype_train = await self.twitch.get_hype_train_status()
        except Exception as exc:
            log.warning("Hype train backfill failed: %s", exc)
            return

        if hype_train is not None:
            self.service.set_hype_train(hype_train)

    async def build_bootstrap(self, limit: int = 200) -> dict:
        await self.maybe_backfill_hype_train()
        return await self.service.bootstrap_event(limit)


settings = load_settings()

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

templates = Jinja2Templates(directory=str(settings.template_dir))


def _static_asset_version() -> str:
    latest = 0.0
    for name in ("app.js", "styles.css"):
        try:
            latest = max(latest, (settings.static_dir / name).stat().st_mtime)
        except OSError:
            pass
    return str(int(latest))


ASSET_VERSION = _static_asset_version()
templates.env.globals["asset_version"] = ASSET_VERSION


@asynccontextmanager
async def lifespan(app: FastAPI):
    runtime = Runtime(settings)
    app.state.runtime = runtime
    await runtime.start()
    yield
    await runtime.stop()


app = FastAPI(title="Unified Chat", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret_key,
    session_cookie="unified_chat_session",
    same_site="lax",
    https_only=settings.session_cookie_secure,
)
app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")


def get_runtime(request_or_app) -> Runtime:
    app_obj = getattr(request_or_app, "app", request_or_app)
    return app_obj.state.runtime


def auth_enabled() -> bool:
    return bool(settings.login_password_hash)


def connection_host(connection: Request | WebSocket) -> str:
    url = getattr(connection, "url", None)
    host = getattr(url, "hostname", None)
    if host:
        return str(host).lower()

    scope = getattr(connection, "scope", None) or {}
    server = scope.get("server")
    if server and server[0]:
        return str(server[0]).lower()
    return ""


def is_local_host(host: str) -> bool:
    normalized = host.strip().strip("[]").lower()
    if not normalized:
        return False
    if normalized == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private


def auth_required_for_connection(connection: Request | WebSocket) -> bool:
    if not auth_enabled():
        return False
    return not is_local_host(connection_host(connection))


def is_authenticated(connection: Request | WebSocket) -> bool:
    if not auth_required_for_connection(connection):
        return True
    session = getattr(connection, "session", None) or {}
    return bool(session.get("authenticated"))


def login_redirect() -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


def require_browser_auth(request: Request) -> RedirectResponse | None:
    if is_authenticated(request):
        return None
    return login_redirect()


def require_json_auth(request: Request) -> None:
    if is_authenticated(request):
        return
    raise HTTPException(status_code=401, detail="Authentication required")


async def parse_form_field(request: Request, key: str) -> str:
    raw_body = await request.body()
    values = parse_qs(raw_body.decode("utf-8", errors="ignore"))
    return str(values.get(key, [""])[0])


def render_login(request: Request, *, error: str | None = None, status_code: int = 200):
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "app_base_url": settings.app_base_url,
            "error": error,
        },
        status_code=status_code,
    )


def apply_popout_embed_headers(response) -> None:
    if "x-frame-options" in response.headers:
        del response.headers["x-frame-options"]
    frame_ancestors = ["'self'", *settings.popup_allowed_frame_ancestors]
    response.headers["Content-Security-Policy"] = f"frame-ancestors {' '.join(dict.fromkeys(frame_ancestors))}"


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if not auth_enabled():
        return RedirectResponse("/", status_code=303)
    if is_authenticated(request):
        return RedirectResponse("/", status_code=303)
    return render_login(request)


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request):
    if not auth_enabled():
        return RedirectResponse("/", status_code=303)

    entered_password = (await parse_form_field(request, "password")).strip()
    if entered_password and check_password_hash(settings.login_password_hash, entered_password):
        request.session["authenticated"] = True
        return RedirectResponse("/", status_code=303)
    return render_login(request, error="Incorrect password.", status_code=401)


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    destination = "/login" if auth_enabled() else "/"
    return RedirectResponse(destination, status_code=303)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    auth_response = require_browser_auth(request)
    if auth_response is not None:
        return auth_response
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "app_base_url": settings.app_base_url,
            "auth_enabled": auth_enabled(),
            "twitch_connect_enabled": settings.twitch_manages_token,
        },
    )


@app.get("/popout", response_class=HTMLResponse)
async def popout(request: Request):
    token = request.query_params.get("token")
    if token and settings.popout_token and secrets.compare_digest(token, settings.popout_token):
        request.session["authenticated"] = True
    auth_response = require_browser_auth(request)
    if auth_response is not None:
        return auth_response
    platform_names_param = request.query_params.get("platform_names")
    response = templates.TemplateResponse(
        request=request,
        name="popout.html",
        context={
            "app_base_url": settings.app_base_url,
            "platform_names_override": platform_names_param,
        },
    )
    apply_popout_embed_headers(response)
    return response


@app.get("/health")
async def health(request: Request):
    runtime = get_runtime(request)
    return {
        "status": runtime.service.overall_state(),
        "connectors": [status.model_dump(mode="json") for status in runtime.service.get_statuses()],
    }


@app.get("/api/messages")
async def get_messages(request: Request, limit: int = Query(200, ge=1, le=500)):
    require_json_auth(request)
    runtime = get_runtime(request)
    return await runtime.build_bootstrap(limit)


@app.post("/api/clear-messages")
async def clear_messages(request: Request):
    require_json_auth(request)
    runtime = get_runtime(request)
    await runtime.service.clear_messages()
    return {"ok": True}


@app.get("/api/emotes")
async def get_emotes(request: Request):
    require_json_auth(request)
    runtime = get_runtime(request)
    emotes = await runtime.twitch.get_emotes()
    return {"emotes": emotes}


@app.post("/api/reply/twitch")
async def reply_twitch(payload: ReplyRequest, request: Request):
    require_json_auth(request)
    runtime = get_runtime(request)
    try:
        result = await runtime.twitch.send_reply(payload.message)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"ok": True, "platform": "twitch", "result": result}


@app.post("/api/mod/twitch/ban")
async def mod_twitch_ban(payload: ModerationRequest, request: Request):
    require_json_auth(request)
    runtime = get_runtime(request)
    try:
        result = await runtime.twitch.ban_user(payload.user_id, reason=payload.reason)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"ok": True, "platform": "twitch", "result": result}


@app.post("/api/mod/twitch/timeout")
async def mod_twitch_timeout(payload: ModerationRequest, request: Request):
    require_json_auth(request)
    runtime = get_runtime(request)
    if payload.duration is None:
        raise HTTPException(status_code=400, detail="duration is required for timeouts")
    try:
        result = await runtime.twitch.ban_user(
            payload.user_id,
            duration=payload.duration,
            reason=payload.reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"ok": True, "platform": "twitch", "result": result}


@app.post("/api/mod/kick/ban")
async def mod_kick_ban(payload: ModerationRequest, request: Request):
    require_json_auth(request)
    runtime = get_runtime(request)
    try:
        result = await runtime.kick.ban_user(payload.user_id, reason=payload.reason)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"ok": True, "platform": "kick", "result": result}


@app.post("/api/mod/kick/timeout")
async def mod_kick_timeout(payload: ModerationRequest, request: Request):
    require_json_auth(request)
    runtime = get_runtime(request)
    if payload.duration is None:
        raise HTTPException(status_code=400, detail="duration is required for timeouts")
    try:
        result = await runtime.kick.ban_user(
            payload.user_id,
            duration=payload.duration,
            reason=payload.reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"ok": True, "platform": "kick", "result": result}


async def _delete_platform_message(platform: str, connector, service, message_id: str) -> dict:
    try:
        result = await connector.delete_message(message_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    # Strike the message through for all clients right away instead of waiting
    # for the platform's delete event (Kick has none; Twitch's dedupes on top).
    await service.mark_message_deleted(platform, message_id, utcnow())
    return {"ok": True, "platform": platform, "result": result}


@app.post("/api/mod/twitch/delete-message")
async def mod_twitch_delete_message(payload: DeleteMessageRequest, request: Request):
    require_json_auth(request)
    runtime = get_runtime(request)
    return await _delete_platform_message("twitch", runtime.twitch, runtime.service, payload.message_id)


@app.post("/api/mod/kick/delete-message")
async def mod_kick_delete_message(payload: DeleteMessageRequest, request: Request):
    require_json_auth(request)
    runtime = get_runtime(request)
    return await _delete_platform_message("kick", runtime.kick, runtime.service, payload.message_id)


@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    if not is_authenticated(websocket):
        await websocket.close(code=1008)
        return
    runtime = get_runtime(websocket)
    await runtime.service.hub.connect(websocket)
    try:
        await websocket.send_json(await runtime.build_bootstrap())
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        runtime.service.hub.disconnect(websocket)


@app.post("/webhooks/kick")
async def kick_webhook(request: Request):
    runtime = get_runtime(request)
    raw_body = await request.body()
    headers = dict(request.headers.items())
    try:
        await runtime.kick.handle_webhook(headers, raw_body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    return JSONResponse({"ok": True})


@app.get("/auth/youtube/start")
async def auth_youtube_start(request: Request):
    auth_response = require_browser_auth(request)
    if auth_response is not None:
        return auth_response
    runtime = get_runtime(request)
    try:
        url = runtime.youtube.get_authorization_url()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url)


@app.get("/auth/youtube/callback")
async def auth_youtube_callback(request: Request):
    runtime = get_runtime(request)
    try:
        await runtime.youtube.complete_authorization(str(request.url), request.query_params.get("state"))
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse("/")


@app.get("/auth/kick/start")
async def auth_kick_start(request: Request):
    auth_response = require_browser_auth(request)
    if auth_response is not None:
        return auth_response
    runtime = get_runtime(request)
    try:
        url = await runtime.kick.get_authorization_url()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url)


@app.get("/auth/kick/callback")
async def auth_kick_callback(request: Request):
    runtime = get_runtime(request)
    try:
        await runtime.kick.complete_authorization(
            request.query_params.get("code"),
            request.query_params.get("state"),
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse("/")


@app.get("/auth/twitch/start")
async def auth_twitch_start(request: Request):
    auth_response = require_browser_auth(request)
    if auth_response is not None:
        return auth_response
    runtime = get_runtime(request)
    try:
        url = runtime.twitch.get_authorization_url()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url)


@app.get("/auth/twitch/callback")
async def auth_twitch_callback(request: Request):
    runtime = get_runtime(request)
    try:
        await runtime.twitch.complete_authorization(
            request.query_params.get("code"),
            request.query_params.get("state"),
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse("/")
