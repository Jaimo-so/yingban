from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import string
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator


INVITE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class InviteError(ValueError):
    pass


class SessionError(ValueError):
    pass


class ClosingConnection(sqlite3.Connection):
    """Make `with connection` commit/rollback and close deterministically."""

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc_value, traceback))
        finally:
            self.close()


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, path: Path, invite_pepper: str, session_secret: str):
        self.path = Path(path)
        self.invite_pepper = invite_pepper.encode("utf-8")
        self.session_secret = session_secret.encode("utf-8")
        self._schema_lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, factory=ClosingConnection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._schema_lock, self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ('active', 'suspended', 'deleted')),
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS invite_codes (
                    id TEXT PRIMARY KEY,
                    code_digest TEXT NOT NULL UNIQUE,
                    code_hint TEXT NOT NULL,
                    status TEXT NOT NULL
                        CHECK(status IN ('issued', 'active', 'revoked')),
                    account_id TEXT REFERENCES accounts(id),
                    created_at TEXT NOT NULL,
                    activated_at TEXT,
                    revoked_at TEXT,
                    replaced_by TEXT REFERENCES invite_codes(id)
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    token_digest TEXT NOT NULL UNIQUE,
                    account_id TEXT NOT NULL REFERENCES accounts(id),
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS admin_sessions (
                    id TEXT PRIMARY KEY,
                    token_digest TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS movies (
                    id TEXT PRIMARY KEY,
                    title_zh TEXT NOT NULL,
                    title_original TEXT NOT NULL,
                    aliases_json TEXT NOT NULL DEFAULT '[]',
                    year INTEGER NOT NULL,
                    directors_json TEXT NOT NULL DEFAULT '[]',
                    regions_json TEXT NOT NULL DEFAULT '[]',
                    genres_json TEXT NOT NULL DEFAULT '[]',
                    themes_json TEXT NOT NULL DEFAULT '[]',
                    moods_json TEXT NOT NULL DEFAULT '[]',
                    content_notes_json TEXT NOT NULL DEFAULT '[]',
                    summary TEXT NOT NULL,
                    popularity_rank INTEGER NOT NULL DEFAULT 9999,
                    source TEXT NOT NULL DEFAULT 'curated-seed',
                    poster_url TEXT NOT NULL DEFAULT '',
                    source_url TEXT NOT NULL DEFAULT '',
                    external_ids_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS user_movie_states (
                    account_id TEXT NOT NULL REFERENCES accounts(id),
                    movie_id TEXT NOT NULL REFERENCES movies(id),
                    state TEXT NOT NULL CHECK(state IN ('watched', 'watchlist', 'disliked')),
                    source TEXT NOT NULL,
                    rating INTEGER CHECK(rating IS NULL OR (rating >= 1 AND rating <= 5)),
                    note TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(account_id, movie_id)
                );

                CREATE TABLE IF NOT EXISTS recommendation_impressions (
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL REFERENCES accounts(id),
                    movie_id TEXT NOT NULL REFERENCES movies(id),
                    request_summary TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS safety_events (
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL REFERENCES accounts(id),
                    category TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_invites_status
                    ON invite_codes(status);
                CREATE INDEX IF NOT EXISTS idx_sessions_digest
                    ON sessions(token_digest);
                CREATE INDEX IF NOT EXISTS idx_movie_state_account
                    ON user_movie_states(account_id, state);
                """
            )
            movie_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(movies)").fetchall()
            }
            for column, definition in (
                ("poster_url", "TEXT NOT NULL DEFAULT ''"),
                ("source_url", "TEXT NOT NULL DEFAULT ''"),
                ("external_ids_json", "TEXT NOT NULL DEFAULT '{}'"),
            ):
                if column not in movie_columns:
                    connection.execute(f"ALTER TABLE movies ADD COLUMN {column} {definition}")

        # The database can contain the administrator-configured model key.
        # Keep the local file private even when the process umask is permissive.
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def get_app_settings(self, keys: list[str] | tuple[str, ...] | None = None) -> dict[str, str]:
        with self.connect() as connection:
            if keys:
                placeholders = ",".join("?" for _ in keys)
                rows = connection.execute(
                    f"SELECT key, value FROM app_settings WHERE key IN ({placeholders})",
                    tuple(keys),
                ).fetchall()
            else:
                rows = connection.execute("SELECT key, value FROM app_settings").fetchall()
        return {str(row["key"]): str(row["value"]) for row in rows}

    def set_app_settings(self, values: dict[str, str]) -> None:
        if not values:
            return
        now = utc_now()
        with self.transaction(immediate=True) as connection:
            connection.executemany(
                """
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                [(key, str(value), now) for key, value in values.items()],
            )

    def _invite_digest(self, code: str) -> str:
        normalized = code.strip().upper().encode("utf-8")
        return hmac.new(self.invite_pepper, normalized, hashlib.sha256).hexdigest()

    def _session_digest(self, token: str) -> str:
        return hmac.new(
            self.session_secret, token.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def _new_invite_code(self) -> str:
        groups = [
            "".join(secrets.choice(INVITE_ALPHABET) for _ in range(5))
            for _ in range(4)
        ]
        return "YB-" + "-".join(groups)

    def generate_invites(self, count: int) -> list[str]:
        if count < 1 or count > 100:
            raise ValueError("count must be between 1 and 100")
        codes: list[str] = []
        with self.transaction(immediate=True) as connection:
            for _ in range(count):
                for _attempt in range(10):
                    code = self._new_invite_code()
                    try:
                        connection.execute(
                            """
                            INSERT INTO invite_codes
                                (id, code_digest, code_hint, status, created_at)
                            VALUES (?, ?, ?, 'issued', ?)
                            """,
                            (
                                "inv_" + secrets.token_hex(8),
                                self._invite_digest(code),
                                code[-5:],
                                utc_now(),
                            ),
                        )
                        codes.append(code)
                        break
                    except sqlite3.IntegrityError:
                        continue
                else:
                    raise RuntimeError("could not create a unique invite code")
        return codes

    def login_with_invite(self, code: str, session_days: int) -> tuple[str, str]:
        digest = self._invite_digest(code)
        now = datetime.now(UTC)
        with self.transaction(immediate=True) as connection:
            invite = connection.execute(
                "SELECT * FROM invite_codes WHERE code_digest = ?", (digest,)
            ).fetchone()
            if invite is None:
                raise InviteError("邀请码无效")
            if invite["status"] == "revoked":
                raise InviteError("邀请码已停用或已换发")

            account_id = invite["account_id"]
            if invite["status"] == "issued":
                account_id = "usr_" + secrets.token_hex(12)
                connection.execute(
                    "INSERT INTO accounts (id, status, created_at) VALUES (?, 'active', ?)",
                    (account_id, now.isoformat()),
                )
                connection.execute(
                    """
                    UPDATE invite_codes
                    SET status = 'active', account_id = ?, activated_at = ?
                    WHERE id = ? AND status = 'issued'
                    """,
                    (account_id, now.isoformat(), invite["id"]),
                )

            account = connection.execute(
                "SELECT * FROM accounts WHERE id = ?", (account_id,)
            ).fetchone()
            if account is None or account["status"] != "active":
                raise InviteError("账户当前不可用")

            token = secrets.token_urlsafe(32)
            connection.execute(
                """
                INSERT INTO sessions
                    (id, token_digest, account_id, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    "ses_" + secrets.token_hex(10),
                    self._session_digest(token),
                    account_id,
                    now.isoformat(),
                    (now + timedelta(days=session_days)).isoformat(),
                ),
            )
        return account_id, token

    def account_for_session(self, token: str | None) -> str | None:
        if not token:
            return None
        digest = self._session_digest(token)
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT s.account_id
                FROM sessions s
                JOIN accounts a ON a.id = s.account_id
                WHERE s.token_digest = ?
                  AND s.expires_at > ?
                  AND a.status = 'active'
                """,
                (digest, utc_now()),
            ).fetchone()
            return str(row["account_id"]) if row else None

    def logout(self, token: str | None) -> None:
        if not token:
            return
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM sessions WHERE token_digest = ?",
                (self._session_digest(token),),
            )

    def create_admin_session(self, hours: int = 8) -> str:
        token = secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO admin_sessions
                    (id, token_digest, created_at, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    "adm_" + secrets.token_hex(8),
                    self._session_digest(token),
                    now.isoformat(),
                    (now + timedelta(hours=hours)).isoformat(),
                ),
            )
        return token

    def valid_admin_session(self, token: str | None) -> bool:
        if not token:
            return False
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM admin_sessions
                WHERE token_digest = ? AND expires_at > ?
                """,
                (self._session_digest(token), utc_now()),
            ).fetchone()
            return bool(row)

    def list_invites(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, code_hint, status, account_id, created_at,
                       activated_at, revoked_at, replaced_by
                FROM invite_codes ORDER BY created_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def revoke_invite(self, invite_id: str) -> None:
        with self.transaction(immediate=True) as connection:
            result = connection.execute(
                """
                UPDATE invite_codes
                SET status = 'revoked', revoked_at = ?
                WHERE id = ? AND status != 'revoked'
                """,
                (utc_now(), invite_id),
            )
            if result.rowcount != 1:
                raise InviteError("邀请码不存在或已经停用")
            row = connection.execute(
                "SELECT account_id FROM invite_codes WHERE id = ?", (invite_id,)
            ).fetchone()
            if row and row["account_id"]:
                connection.execute(
                    "DELETE FROM sessions WHERE account_id = ?", (row["account_id"],)
                )

    def rotate_invite(self, invite_id: str) -> str:
        new_code = self._new_invite_code()
        now = utc_now()
        with self.transaction(immediate=True) as connection:
            current = connection.execute(
                "SELECT * FROM invite_codes WHERE id = ?", (invite_id,)
            ).fetchone()
            if current is None or current["status"] != "active":
                raise InviteError("只有已激活的邀请码可以换发")
            new_id = "inv_" + secrets.token_hex(8)
            connection.execute(
                """
                INSERT INTO invite_codes
                    (id, code_digest, code_hint, status, account_id,
                     created_at, activated_at)
                VALUES (?, ?, ?, 'active', ?, ?, ?)
                """,
                (
                    new_id,
                    self._invite_digest(new_code),
                    new_code[-5:],
                    current["account_id"],
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE invite_codes
                SET status = 'revoked', revoked_at = ?, replaced_by = ?
                WHERE id = ?
                """,
                (now, new_id, invite_id),
            )
            connection.execute(
                "DELETE FROM sessions WHERE account_id = ?",
                (current["account_id"],),
            )
        return new_code

    def upsert_movies(self, records: list[dict[str, Any]]) -> None:
        with self.connect() as connection:
            for record in records:
                connection.execute(
                    """
                    INSERT INTO movies (
                        id, title_zh, title_original, aliases_json, year,
                        directors_json, regions_json, genres_json, themes_json,
                        moods_json, content_notes_json, summary, popularity_rank,
                        source, poster_url, source_url, external_ids_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        title_zh = excluded.title_zh,
                        title_original = excluded.title_original,
                        aliases_json = excluded.aliases_json,
                        year = excluded.year,
                        directors_json = excluded.directors_json,
                        regions_json = excluded.regions_json,
                        genres_json = excluded.genres_json,
                        themes_json = excluded.themes_json,
                        moods_json = excluded.moods_json,
                        content_notes_json = excluded.content_notes_json,
                        summary = excluded.summary,
                        popularity_rank = excluded.popularity_rank,
                        source = excluded.source,
                        poster_url = CASE
                            WHEN excluded.poster_url != '' THEN excluded.poster_url
                            ELSE movies.poster_url
                        END,
                        source_url = CASE
                            WHEN excluded.source_url != '' THEN excluded.source_url
                            ELSE movies.source_url
                        END,
                        external_ids_json = CASE
                            WHEN excluded.external_ids_json != '{}' THEN excluded.external_ids_json
                            ELSE movies.external_ids_json
                        END,
                        updated_at = excluded.updated_at
                    """,
                    (
                        record["id"],
                        record["title_zh"],
                        record["title_original"],
                        json.dumps(record.get("aliases", []), ensure_ascii=False),
                        int(record["year"]),
                        json.dumps(record.get("directors", []), ensure_ascii=False),
                        json.dumps(record.get("regions", []), ensure_ascii=False),
                        json.dumps(record.get("genres", []), ensure_ascii=False),
                        json.dumps(record.get("themes", []), ensure_ascii=False),
                        json.dumps(record.get("moods", []), ensure_ascii=False),
                        json.dumps(record.get("content_notes", []), ensure_ascii=False),
                        record["summary"],
                        int(record.get("popularity_rank", 9999)),
                        record.get("source", "curated-seed"),
                        str(record.get("poster_url", "")),
                        str(record.get("source_url", "")),
                        json.dumps(record.get("external_ids", {}), ensure_ascii=False),
                        utc_now(),
                    ),
                )

    def set_movie_state(
        self,
        account_id: str,
        movie_id: str,
        state: str,
        source: str,
        rating: int | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        if state not in {"watched", "watchlist", "disliked"}:
            raise ValueError("invalid movie state")
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO user_movie_states
                    (account_id, movie_id, state, source, rating, note,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, movie_id) DO UPDATE SET
                    state = excluded.state,
                    source = excluded.source,
                    rating = COALESCE(excluded.rating, user_movie_states.rating),
                    note = COALESCE(excluded.note, user_movie_states.note),
                    updated_at = excluded.updated_at
                """,
                (account_id, movie_id, state, source, rating, note, now, now),
            )
        return self.get_movie_state(account_id, movie_id) or {}

    def get_movie_state(self, account_id: str, movie_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM user_movie_states
                WHERE account_id = ? AND movie_id = ?
                """,
                (account_id, movie_id),
            ).fetchone()
        return dict(row) if row else None

    def save_movie_reflection(
        self, account_id: str, movie_id: str, note: str
    ) -> dict[str, Any]:
        clean_note = str(note).strip()
        if not clean_note or len(clean_note) > 2000:
            raise ValueError("观后感笔记必须在 1 到 2000 个字符之间")
        now = utc_now()
        with self.connect() as connection:
            result = connection.execute(
                """
                UPDATE user_movie_states
                SET note = ?, updated_at = ?
                WHERE account_id = ? AND movie_id = ? AND state = 'watched'
                """,
                (clean_note, now, account_id, movie_id),
            )
            if result.rowcount == 0:
                connection.execute(
                    """
                    INSERT INTO user_movie_states
                        (account_id, movie_id, state, source, rating, note,
                         created_at, updated_at)
                    VALUES (?, ?, 'watched', 'discussion_reflection', NULL, ?, ?, ?)
                    ON CONFLICT(account_id, movie_id) DO UPDATE SET
                        state = 'watched',
                        note = excluded.note,
                        updated_at = excluded.updated_at
                    """,
                    (account_id, movie_id, clean_note, now, now),
                )
        return self.get_movie_state(account_id, movie_id) or {}

    def remove_movie_state(self, account_id: str, movie_id: str) -> bool:
        with self.connect() as connection:
            result = connection.execute(
                """
                DELETE FROM user_movie_states
                WHERE account_id = ? AND movie_id = ?
                """,
                (account_id, movie_id),
            )
        return result.rowcount == 1

    def movie_states(self, account_id: str, state: str | None = None) -> list[dict[str, Any]]:
        params: list[Any] = [account_id]
        state_filter = ""
        if state:
            state_filter = " AND ums.state = ?"
            params.append(state)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT ums.*, m.id AS id, m.title_zh, m.title_original, m.aliases_json,
                       m.directors_json, m.regions_json, m.year,
                       m.genres_json, m.moods_json, m.poster_url, m.source_url,
                       m.themes_json, m.content_notes_json, m.summary,
                       m.popularity_rank, m.source, m.external_ids_json
                FROM user_movie_states ums
                JOIN movies m ON m.id = ums.movie_id
                WHERE ums.account_id = ? {state_filter}
                ORDER BY ums.updated_at DESC
                """,
                params,
            ).fetchall()
        return [self._decode_movie_row(dict(row)) for row in rows]

    def watched_ids(self, account_id: str) -> set[str]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT movie_id FROM user_movie_states
                WHERE account_id = ? AND state = 'watched'
                """,
                (account_id,),
            ).fetchall()
        return {str(row["movie_id"]) for row in rows}

    def all_movies(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM movies ORDER BY popularity_rank, year DESC"
            ).fetchall()
        return [self._decode_movie_row(dict(row)) for row in rows]

    def movie(self, movie_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM movies WHERE id = ?", (movie_id,)
            ).fetchone()
        return self._decode_movie_row(dict(row)) if row else None

    def record_recommendations(
        self, account_id: str, movies: list[dict[str, Any]], request_summary: str
    ) -> None:
        with self.connect() as connection:
            for movie in movies:
                connection.execute(
                    """
                    INSERT INTO recommendation_impressions
                        (id, account_id, movie_id, request_summary, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        "rec_" + secrets.token_hex(8),
                        account_id,
                        movie["id"],
                        request_summary[:300],
                        utc_now(),
                    ),
                )

    def recent_recommended_ids(self, account_id: str, limit: int = 20) -> set[str]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT movie_id FROM recommendation_impressions
                WHERE account_id = ?
                ORDER BY created_at DESC LIMIT ?
                """,
                (account_id, limit),
            ).fetchall()
        return {str(row["movie_id"]) for row in rows}

    def record_safety_event(self, account_id: str, category: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO safety_events (id, account_id, category, created_at)
                VALUES (?, ?, ?, ?)
                """,
                ("safe_" + secrets.token_hex(8), account_id, category, utc_now()),
            )

    @staticmethod
    def _decode_movie_row(row: dict[str, Any]) -> dict[str, Any]:
        for key in (
            "aliases_json",
            "directors_json",
            "regions_json",
            "genres_json",
            "themes_json",
            "moods_json",
            "content_notes_json",
            "external_ids_json",
        ):
            if key in row:
                fallback = "{}" if key == "external_ids_json" else "[]"
                row[key.removesuffix("_json")] = json.loads(row.pop(key) or fallback)
        return row
