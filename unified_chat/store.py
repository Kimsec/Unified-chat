from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from unified_chat.models import Badge, Emote, UnifiedMessage
from unified_chat.utils import dumps_json, utcnow


class MessageStore:
    MAX_STORED_MESSAGES = 500

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self._database_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    platform TEXT NOT NULL,
                    platform_message_id TEXT NOT NULL,
                    message_kind TEXT NOT NULL DEFAULT 'chat',
                    notice_type TEXT,
                    channel_id TEXT,
                    author_display_name TEXT NOT NULL,
                    author_login TEXT,
                    author_color TEXT,
                    avatar_url TEXT,
                    badges_json TEXT NOT NULL,
                    text TEXT NOT NULL,
                    sent_at TEXT NOT NULL,
                    deleted_at TEXT,
                    raw_payload_json TEXT NOT NULL,
                    inserted_at TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_platform_message
                ON messages(platform, platform_message_id)
                """
            )
            # Migration: add emotes_json column if missing
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(messages)").fetchall()}
            if "message_kind" not in columns:
                self._conn.execute("ALTER TABLE messages ADD COLUMN message_kind TEXT NOT NULL DEFAULT 'chat'")
            if "notice_type" not in columns:
                self._conn.execute("ALTER TABLE messages ADD COLUMN notice_type TEXT")
            if "emotes_json" not in columns:
                self._conn.execute("ALTER TABLE messages ADD COLUMN emotes_json TEXT NOT NULL DEFAULT '[]'")
            if "deleted_at" not in columns:
                self._conn.execute("ALTER TABLE messages ADD COLUMN deleted_at TEXT")
            self._conn.commit()

    def add_message(self, message: UnifiedMessage) -> bool:
        payload = (
            message.id,
            message.platform,
            message.platform_message_id,
            message.message_kind,
            message.notice_type,
            message.channel_id,
            message.author_display_name,
            message.author_login,
            message.author_color,
            message.avatar_url,
            dumps_json([badge.model_dump() for badge in message.badges]),
            dumps_json([emote.model_dump() for emote in message.emotes]),
            message.text,
            message.sent_at.isoformat(),
            message.deleted_at.isoformat() if message.deleted_at else None,
            dumps_json(message.raw_payload),
            utcnow().isoformat(),
        )
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO messages (
                        id, platform, platform_message_id, message_kind, notice_type, channel_id,
                        author_display_name, author_login, author_color, avatar_url,
                        badges_json, emotes_json, text, sent_at, deleted_at, raw_payload_json, inserted_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    payload,
                )
                self._conn.execute(
                    """
                    DELETE FROM messages
                    WHERE id IN (
                        SELECT id
                        FROM messages
                        ORDER BY sent_at DESC, inserted_at DESC
                        LIMIT -1 OFFSET ?
                    )
                    """,
                    (self.MAX_STORED_MESSAGES,),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def mark_message_deleted(
        self,
        platform: str,
        platform_message_id: str,
        deleted_at: datetime,
    ) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE messages
                SET deleted_at = COALESCE(deleted_at, ?)
                WHERE platform = ? AND platform_message_id = ?
                """,
                (deleted_at.isoformat(), platform, platform_message_id),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def clear_messages(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM messages")
            self._conn.commit()
            self._conn.execute("VACUUM")


    def list_messages(self, limit: int = 200) -> list[UnifiedMessage]:
        safe_limit = max(1, min(int(limit), 500))
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT *
                FROM messages
                ORDER BY sent_at DESC, inserted_at DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        return [self._row_to_message(row) for row in reversed(rows)]

    def _row_to_message(self, row: sqlite3.Row) -> UnifiedMessage:
        badges = [Badge(**badge) for badge in json.loads(row["badges_json"] or "[]")]
        emotes = [Emote(**e) for e in json.loads(row["emotes_json"] or "[]")]
        return UnifiedMessage(
            id=row["id"],
            platform=row["platform"],
            platform_message_id=row["platform_message_id"],
            message_kind=row["message_kind"] or "chat",
            notice_type=row["notice_type"],
            channel_id=row["channel_id"],
            author_display_name=row["author_display_name"],
            author_login=row["author_login"],
            author_color=row["author_color"],
            avatar_url=row["avatar_url"],
            badges=badges,
            emotes=emotes,
            text=row["text"],
            sent_at=row["sent_at"],
            deleted_at=row["deleted_at"],
            raw_payload=json.loads(row["raw_payload_json"] or "{}"),
        )
