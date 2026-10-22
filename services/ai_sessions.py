import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


class AIChatSessionStore:
    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize)

    def _connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS ai_chat_sessions (
                    session_id TEXT PRIMARY KEY,
                    guild_id INTEGER NOT NULL,
                    owner_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    channel_name TEXT NOT NULL,
                    session_type TEXT NOT NULL DEFAULT 'private_channel',
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_activity TEXT NOT NULL,
                    archived_at TEXT,
                    model TEXT,
                    context_length INTEGER,
                    prompt_tokens INTEGER,
                    messages_json TEXT NOT NULL,
                    search_text TEXT NOT NULL,
                    members_json TEXT NOT NULL DEFAULT '[]'
                )
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(ai_chat_sessions)")
            }
            if "members_json" not in columns:
                connection.execute(
                    "ALTER TABLE ai_chat_sessions ADD COLUMN members_json TEXT NOT NULL DEFAULT '[]'"
                )
            if "session_type" not in columns:
                connection.execute(
                    "ALTER TABLE ai_chat_sessions ADD COLUMN session_type TEXT NOT NULL DEFAULT 'private_channel'"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS ai_chat_sessions_search "
                "ON ai_chat_sessions(guild_id, status, owner_id, last_activity)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS ai_chat_sessions_type "
                "ON ai_chat_sessions(guild_id, session_type, owner_id, last_activity)"
            )

    async def save_active_session(
        self,
        *,
        session_id: str,
        guild_id: int,
        owner_id: int,
        channel_id: int,
        channel_name: str,
        created_at: str,
        last_activity: str,
        model: Optional[str],
        context_length: Optional[int],
        prompt_tokens: Optional[int],
        messages: list[dict[str, Any]],
        members: Optional[list[int]] = None,
        session_type: str = "private_channel",
    ) -> None:
        await asyncio.to_thread(
            self._save_active_session,
            session_id,
            guild_id,
            owner_id,
            channel_id,
            channel_name,
            created_at,
            last_activity,
            model,
            context_length,
            prompt_tokens,
            messages,
            members or [],
            session_type,
        )

    def _save_active_session(
        self,
        session_id: str,
        guild_id: int,
        owner_id: int,
        channel_id: int,
        channel_name: str,
        created_at: str,
        last_activity: str,
        model: Optional[str],
        context_length: Optional[int],
        prompt_tokens: Optional[int],
        messages: list[dict[str, Any]],
        members: list[int],
        session_type: str,
    ) -> None:
        serialized = json.dumps(messages, ensure_ascii=False)
        serialized_members = json.dumps(sorted(set(members)))
        search_text = "\n".join(str(item.get("content", "")) for item in messages).casefold()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO ai_chat_sessions (
                    session_id, guild_id, owner_id, channel_id, channel_name,
                    session_type, status, created_at, last_activity, archived_at, model,
                    context_length, prompt_tokens, messages_json, search_text, members_json
                ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, NULL, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    channel_id = excluded.channel_id,
                    channel_name = excluded.channel_name,
                    session_type = excluded.session_type,
                    status = 'active',
                    last_activity = excluded.last_activity,
                    archived_at = NULL,
                    model = excluded.model,
                    context_length = excluded.context_length,
                    prompt_tokens = excluded.prompt_tokens,
                    messages_json = excluded.messages_json,
                    search_text = excluded.search_text,
                    members_json = excluded.members_json
                """,
                (
                    session_id,
                    guild_id,
                    owner_id,
                    channel_id,
                    channel_name,
                    session_type,
                    created_at,
                    last_activity,
                    model,
                    context_length,
                    prompt_tokens,
                    serialized,
                    search_text,
                    serialized_members,
                ),
            )

    async def update_members(self, session_id: str, members: list[int]) -> None:
        await asyncio.to_thread(self._update_members, session_id, members)

    def _update_members(self, session_id: str, members: list[int]) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE ai_chat_sessions SET members_json = ? WHERE session_id = ?",
                (json.dumps(sorted(set(members))), session_id),
            )

    async def get_by_channel(self, guild_id: int, channel_id: int) -> Optional[dict[str, Any]]:
        row = await asyncio.to_thread(self._get_by_channel, guild_id, channel_id)
        return self._row_to_session(row) if row else None

    def _get_by_channel(self, guild_id: int, channel_id: int) -> Optional[sqlite3.Row]:
        with self._connect() as connection:
            return connection.execute(
                "SELECT * FROM ai_chat_sessions WHERE guild_id = ? AND channel_id = ? AND status = 'active'",
                (guild_id, channel_id),
            ).fetchone()

    async def get_by_user_channel(
        self, guild_id: int, owner_id: int, channel_id: int, session_type: str = "regular"
    ) -> Optional[dict[str, Any]]:
        """Get a regular AI command session by user and channel."""
        row = await asyncio.to_thread(
            self._get_by_user_channel, guild_id, owner_id, channel_id, session_type
        )
        return self._row_to_session(row) if row else None

    def _get_by_user_channel(
        self, guild_id: int, owner_id: int, channel_id: int, session_type: str
    ) -> Optional[sqlite3.Row]:
        with self._connect() as connection:
            return connection.execute(
                "SELECT * FROM ai_chat_sessions WHERE guild_id = ? AND owner_id = ? AND channel_id = ? AND session_type = ? AND status = 'active'",
                (guild_id, owner_id, channel_id, session_type),
            ).fetchone()

    async def get_user_sessions(
        self, guild_id: int, owner_id: int, *, session_type: Optional[str] = None, status: Optional[str] = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Get all sessions for a user, optionally filtered by type and status."""
        rows = await asyncio.to_thread(
            self._get_user_sessions, guild_id, owner_id, session_type, status, limit
        )
        return [self._row_to_session(row, include_messages=False) for row in rows]

    def _get_user_sessions(
        self, guild_id: int, owner_id: int, session_type: Optional[str], status: Optional[str], limit: int
    ) -> list[sqlite3.Row]:
        clauses = ["guild_id = ?", "owner_id = ?"]
        parameters: list[Any] = [guild_id, owner_id]
        if session_type is not None:
            clauses.append("session_type = ?")
            parameters.append(session_type)
        if status is not None:
            clauses.append("status = ?")
            parameters.append(status)
        parameters.append(max(1, min(limit, 50)))
        with self._connect() as connection:
            return connection.execute(
                f"SELECT * FROM ai_chat_sessions WHERE {' AND '.join(clauses)} ORDER BY last_activity DESC LIMIT ?",
                parameters,
            ).fetchall()

    async def get_archived(self, guild_id: int, session_id: str) -> Optional[dict[str, Any]]:
        row = await asyncio.to_thread(self._get_archived, guild_id, session_id)
        return self._row_to_session(row) if row else None

    def _get_archived(self, guild_id: int, session_id: str) -> Optional[sqlite3.Row]:
        with self._connect() as connection:
            return connection.execute(
                "SELECT * FROM ai_chat_sessions WHERE guild_id = ? AND session_id = ? AND status = 'archived'",
                (guild_id, session_id),
            ).fetchone()

    async def search_archived(
        self,
        guild_id: int,
        query: str,
        *,
        owner_id: Optional[int] = None,
        session_type: Optional[str] = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        rows = await asyncio.to_thread(self._search_archived, guild_id, query, owner_id, session_type, limit)
        return [self._row_to_session(row, include_messages=False) for row in rows]

    def _search_archived(
        self,
        guild_id: int,
        query: str,
        owner_id: Optional[int],
        session_type: Optional[str],
        limit: int,
    ) -> list[sqlite3.Row]:
        clauses = ["guild_id = ?", "status = 'archived'", "search_text LIKE ?"]
        parameters: list[Any] = [guild_id, f"%{query.casefold()}%"]
        if owner_id is not None:
            clauses.append("owner_id = ?")
            parameters.append(owner_id)
        if session_type is not None:
            clauses.append("session_type = ?")
            parameters.append(session_type)
        parameters.append(max(1, min(limit, 20)))
        with self._connect() as connection:
            return connection.execute(
                f"SELECT * FROM ai_chat_sessions WHERE {' AND '.join(clauses)} "
                "ORDER BY archived_at DESC LIMIT ?",
                parameters,
            ).fetchall()

    async def get_expired_active(self, cutoff: str) -> list[dict[str, Any]]:
        rows = await asyncio.to_thread(self._get_expired_active, cutoff)
        return [self._row_to_session(row) for row in rows]

    async def get_all_active(self) -> list[dict[str, Any]]:
        rows = await asyncio.to_thread(self._get_all_active)
        return [self._row_to_session(row) for row in rows]

    def _get_all_active(self) -> list[sqlite3.Row]:
        with self._connect() as connection:
            return connection.execute(
                "SELECT * FROM ai_chat_sessions WHERE status = 'active' ORDER BY last_activity"
            ).fetchall()

    def _get_expired_active(self, cutoff: str) -> list[sqlite3.Row]:
        with self._connect() as connection:
            return connection.execute(
                "SELECT * FROM ai_chat_sessions WHERE status = 'active' AND last_activity < ? ORDER BY last_activity",
                (cutoff,),
            ).fetchall()

    async def archive(self, session_id: str, archived_at: Optional[str] = None) -> None:
        archived_at = archived_at or datetime.now(timezone.utc).isoformat()
        await asyncio.to_thread(self._archive, session_id, archived_at)

    def _archive(self, session_id: str, archived_at: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE ai_chat_sessions SET status = 'archived', archived_at = ? WHERE session_id = ?",
                (archived_at, session_id),
            )

    @staticmethod
    def _row_to_session(row: sqlite3.Row, *, include_messages: bool = True) -> dict[str, Any]:
        session = dict(row)
        session["messages"] = json.loads(session.pop("messages_json")) if include_messages else []
        session["members"] = json.loads(session.pop("members_json", "[]"))
        session["preview"] = session.get("search_text", "")[:240]
        session.pop("search_text", None)
        return session
