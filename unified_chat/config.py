from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


PROJECT_DIR = Path(__file__).resolve().parent.parent


def _optional_path(value: str | None) -> Path | None:
    if not value:
        return None
    return Path(value).expanduser()


def _split_scopes(value: str) -> list[str]:
    return [part.strip() for part in value.split() if part.strip()]


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


@dataclass(slots=True)
class Settings:
    project_dir: Path
    host: str
    port: int
    app_base_url: str
    log_level: str
    login_password_hash: str
    session_secret_key: str
    session_cookie_secure: bool
    database_path: Path
    twitch_client_id: str
    twitch_broadcaster_id: str
    twitch_tokens_path: Path
    twitch_eventsub_ws_url: str
    youtube_client_secrets_file: Path | None
    youtube_token_path: Path
    youtube_redirect_uri: str
    youtube_scopes: list[str]
    youtube_poll_fallback_sec: int
    kick_client_id: str
    kick_client_secret: str
    kick_broadcaster_user_id: str
    kick_token_path: Path
    kick_redirect_uri: str
    kick_scope: str
    template_dir: Path
    static_dir: Path

    def ensure_dirs(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.youtube_token_path.parent.mkdir(parents=True, exist_ok=True)
        self.kick_token_path.parent.mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    load_dotenv(PROJECT_DIR / ".env")

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8090"))
    app_base_url = os.getenv("APP_BASE_URL", f"http://127.0.0.1:{port}").rstrip("/")
    login_password_hash = os.getenv("LOGIN_PASSWORD_HASH", "").strip()
    session_secret_key = os.getenv("SESSION_SECRET_KEY", "").strip()
    if login_password_hash and not session_secret_key:
        raise RuntimeError("SESSION_SECRET_KEY is required when LOGIN_PASSWORD_HASH is set")
    session_cookie_secure = _parse_bool(
        os.getenv("SESSION_COOKIE_SECURE"),
        default=app_base_url.startswith("https://"),
    )

    settings = Settings(
        project_dir=PROJECT_DIR,
        host=host,
        port=port,
        app_base_url=app_base_url,
        log_level=os.getenv("LOG_LEVEL", "info"),
        login_password_hash=login_password_hash,
        session_secret_key=session_secret_key or "dev-unified-chat-session-key",
        session_cookie_secure=session_cookie_secure,
        database_path=Path(os.getenv("DATABASE_PATH", str(PROJECT_DIR / "data" / "unified_chat.db"))).expanduser(),
        twitch_client_id=os.getenv("TWITCH_CLIENT_ID", "").strip(),
        twitch_broadcaster_id=os.getenv("TWITCH_BROADCASTER_ID", "").strip(),
        twitch_tokens_path=Path(
            os.getenv("TWITCH_TOKENS_PATH", "/home/kim3k/stream-control/twitch_tokens.json")
        ).expanduser(),
        twitch_eventsub_ws_url=os.getenv("TWITCH_EVENTSUB_WS_URL", "wss://eventsub.wss.twitch.tv/ws").strip(),
        youtube_client_secrets_file=_optional_path(os.getenv("YOUTUBE_CLIENT_SECRETS_FILE")),
        youtube_token_path=Path(
            os.getenv("YOUTUBE_TOKEN_PATH", str(PROJECT_DIR / "data" / "youtube_tokens.json"))
        ).expanduser(),
        youtube_redirect_uri=os.getenv("YOUTUBE_REDIRECT_URI", f"{app_base_url}/auth/youtube/callback").strip(),
        youtube_scopes=_split_scopes(
            os.getenv("YOUTUBE_SCOPES", "https://www.googleapis.com/auth/youtube.readonly")
        ),
        youtube_poll_fallback_sec=max(1, int(os.getenv("YOUTUBE_POLL_FALLBACK_SEC", "8"))),
        kick_client_id=os.getenv("KICK_CLIENT_ID", "").strip(),
        kick_client_secret=os.getenv("KICK_CLIENT_SECRET", "").strip(),
        kick_broadcaster_user_id=os.getenv("KICK_BROADCASTER_USER_ID", "").strip(),
        kick_token_path=Path(
            os.getenv("KICK_TOKEN_PATH", str(PROJECT_DIR / "data" / "kick_tokens.json"))
        ).expanduser(),
        kick_redirect_uri=os.getenv("KICK_REDIRECT_URI", f"{app_base_url}/auth/kick/callback").strip(),
        kick_scope=os.getenv("KICK_SCOPE", "events:subscribe").strip(),
        template_dir=PROJECT_DIR / "unified_chat" / "templates",
        static_dir=PROJECT_DIR / "unified_chat" / "static",
    )
    settings.ensure_dirs()
    return settings
