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

                CREATE TABLE IF NOT EXISTS account_profiles (
                    account_id TEXT PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
                    onboarding_status TEXT NOT NULL DEFAULT 'not_started'
                        CHECK(onboarding_status IN ('not_started', 'in_progress', 'completed')),
                    onboarding_completed_at TEXT,
                    taste_summary TEXT NOT NULL DEFAULT '',
                    taste_dimensions_json TEXT NOT NULL DEFAULT '[]',
                    taste_version INTEGER NOT NULL DEFAULT 0,
                    taste_generated_at TEXT,
                    auto_generate_reflection_drafts INTEGER NOT NULL DEFAULT 1
                        CHECK(auto_generate_reflection_drafts IN (0, 1)),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS user_movie_feedback (
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                    movie_id TEXT NOT NULL REFERENCES movies(id) ON DELETE CASCADE,
                    context TEXT NOT NULL
                        CHECK(context IN ('onboarding', 'recommendation', 'post_watch', 'manual')),
                    sentiment TEXT NOT NULL
                        CHECK(sentiment IN ('positive', 'neutral', 'negative')),
                    reason_codes_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(account_id, movie_id, context)
                );

                CREATE TABLE IF NOT EXISTS recommendation_feedback (
                    id TEXT PRIMARY KEY,
                    impression_id TEXT NOT NULL REFERENCES recommendation_impressions(id) ON DELETE CASCADE,
                    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                    movie_id TEXT NOT NULL REFERENCES movies(id) ON DELETE CASCADE,
                    action TEXT NOT NULL CHECK(action IN (
                        'watchlist', 'watched', 'discuss', 'not_now', 'wrong_tone',
                        'wrong_genre', 'too_heavy', 'not_interested', 'other'
                    )),
                    reason_code TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS movie_reflections (
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                    movie_id TEXT NOT NULL REFERENCES movies(id) ON DELETE CASCADE,
                    version INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    source TEXT NOT NULL CHECK(source IN ('ai', 'user', 'ai_then_user')),
                    status TEXT NOT NULL CHECK(status IN ('draft', 'confirmed', 'locked', 'deleted')),
                    based_on_version INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    confirmed_at TEXT,
                    deleted_at TEXT,
                    UNIQUE(account_id, movie_id, version)
                );

                CREATE TABLE IF NOT EXISTS product_events (
                    id TEXT PRIMARY KEY,
                    account_id TEXT REFERENCES accounts(id) ON DELETE SET NULL,
                    session_hint TEXT,
                    event_name TEXT NOT NULL,
                    properties_json TEXT NOT NULL DEFAULT '{}',
                    schema_version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS model_usage_events (
                    id TEXT PRIMARY KEY,
                    account_id TEXT REFERENCES accounts(id) ON DELETE SET NULL,
                    operation TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    service_id TEXT NOT NULL,
                    success INTEGER NOT NULL CHECK(success IN (0, 1)),
                    latency_ms INTEGER NOT NULL,
                    input_units INTEGER,
                    output_units INTEGER,
                    estimated_cost REAL NOT NULL DEFAULT 0,
                    pricing_version TEXT NOT NULL DEFAULT 'unpriced-v1',
                    error_category TEXT,
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
                CREATE INDEX IF NOT EXISTS idx_onboarding_feedback_account
                    ON user_movie_feedback(account_id, context, updated_at);
                CREATE INDEX IF NOT EXISTS idx_recommendation_feedback_account
                    ON recommendation_feedback(account_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_reflections_current
                    ON movie_reflections(account_id, movie_id, version DESC);
                CREATE INDEX IF NOT EXISTS idx_product_events_name_time
                    ON product_events(event_name, created_at);
                CREATE INDEX IF NOT EXISTS idx_model_usage_operation_time
                    ON model_usage_events(operation, created_at);
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

            # Stage nine migration: preserve every non-empty stage-eight note as a
            # confirmed first version. The NOT EXISTS guard makes startup idempotent.
            connection.execute(
                """
                INSERT INTO movie_reflections (
                    id, account_id, movie_id, version, content, source, status,
                    based_on_version, created_at, updated_at, confirmed_at, deleted_at
                )
                SELECT 'refl_migrated_' || lower(hex(randomblob(8))),
                       ums.account_id, ums.movie_id, 1, trim(ums.note), 'ai', 'confirmed',
                       NULL, ums.updated_at, ums.updated_at, ums.updated_at, NULL
                FROM user_movie_states ums
                WHERE trim(COALESCE(ums.note, '')) != ''
                  AND NOT EXISTS (
                      SELECT 1 FROM movie_reflections mr
                      WHERE mr.account_id = ums.account_id AND mr.movie_id = ums.movie_id
                  )
                """
            )

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

    def account_profile(self, account_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM account_profiles WHERE account_id = ?", (account_id,)
            ).fetchone()
        if row is None:
            return {
                "account_id": account_id,
                "onboarding_status": "not_started",
                "onboarding_completed_at": None,
                "taste_summary": "",
                "taste_dimensions": [],
                "taste_version": 0,
                "taste_generated_at": None,
                "auto_generate_reflection_drafts": True,
            }
        result = dict(row)
        result["taste_dimensions"] = json.loads(result.pop("taste_dimensions_json") or "[]")
        result["auto_generate_reflection_drafts"] = bool(
            result["auto_generate_reflection_drafts"]
        )
        return result

    def _ensure_profile(self, connection: sqlite3.Connection, account_id: str) -> None:
        now = utc_now()
        connection.execute(
            """
            INSERT INTO account_profiles (
                account_id, onboarding_status, taste_summary,
                taste_dimensions_json, taste_version,
                auto_generate_reflection_drafts, created_at, updated_at
            ) VALUES (?, 'not_started', '', '[]', 0, 1, ?, ?)
            ON CONFLICT(account_id) DO NOTHING
            """,
            (account_id, now, now),
        )

    def onboarding_movies(self, account_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT umf.sentiment, umf.reason_codes_json, umf.created_at AS selected_at,
                       m.*
                FROM user_movie_feedback umf
                JOIN movies m ON m.id = umf.movie_id
                WHERE umf.account_id = ? AND umf.context = 'onboarding'
                ORDER BY umf.created_at, umf.movie_id
                """,
                (account_id,),
            ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = self._decode_movie_row(dict(row))
            item["reason_codes"] = json.loads(item.pop("reason_codes_json") or "[]")
            items.append(item)
        return items

    def add_onboarding_movie(
        self, account_id: str, movie_id: str, sentiment: str
    ) -> dict[str, Any]:
        if sentiment not in {"positive", "neutral", "negative"}:
            raise ValueError("口味反馈必须是喜欢、一般或不喜欢")
        now = utc_now()
        with self.transaction(immediate=True) as connection:
            movie = connection.execute("SELECT 1 FROM movies WHERE id = ?", (movie_id,)).fetchone()
            if movie is None:
                raise ValueError("电影不存在")
            existing = connection.execute(
                """SELECT 1 FROM user_movie_feedback
                   WHERE account_id = ? AND movie_id = ? AND context = 'onboarding'""",
                (account_id, movie_id),
            ).fetchone()
            count = int(
                connection.execute(
                    """SELECT COUNT(*) FROM user_movie_feedback
                       WHERE account_id = ? AND context = 'onboarding'""",
                    (account_id,),
                ).fetchone()[0]
            )
            if existing is None and count >= 5:
                raise ValueError("冷启动最多选择 5 部电影")
            self._ensure_profile(connection, account_id)
            connection.execute(
                """
                INSERT INTO user_movie_feedback (
                    id, account_id, movie_id, context, sentiment,
                    reason_codes_json, created_at, updated_at
                ) VALUES (?, ?, ?, 'onboarding', ?, '[]', ?, ?)
                ON CONFLICT(account_id, movie_id, context) DO UPDATE SET
                    sentiment = excluded.sentiment,
                    updated_at = excluded.updated_at
                """,
                ("umf_" + secrets.token_hex(8), account_id, movie_id, sentiment, now, now),
            )
            connection.execute(
                """
                INSERT INTO user_movie_states (
                    account_id, movie_id, state, source, rating, note, created_at, updated_at
                ) VALUES (?, ?, 'watched', 'onboarding', NULL, NULL, ?, ?)
                ON CONFLICT(account_id, movie_id) DO UPDATE SET
                    state = 'watched',
                    source = CASE
                        WHEN user_movie_states.state = 'watched' THEN user_movie_states.source
                        ELSE 'onboarding'
                    END,
                    updated_at = excluded.updated_at
                """,
                (account_id, movie_id, now, now),
            )
            connection.execute(
                """UPDATE account_profiles
                   SET onboarding_status = 'in_progress', updated_at = ?
                   WHERE account_id = ?""",
                (now, account_id),
            )
        return next(item for item in self.onboarding_movies(account_id) if item["id"] == movie_id)

    def remove_onboarding_movie(self, account_id: str, movie_id: str) -> bool:
        now = utc_now()
        with self.transaction(immediate=True) as connection:
            removed = connection.execute(
                """DELETE FROM user_movie_feedback
                   WHERE account_id = ? AND movie_id = ? AND context = 'onboarding'""",
                (account_id, movie_id),
            )
            if removed.rowcount == 0:
                return False
            connection.execute(
                """DELETE FROM user_movie_states
                   WHERE account_id = ? AND movie_id = ? AND source = 'onboarding'""",
                (account_id, movie_id),
            )
            self._ensure_profile(connection, account_id)
            connection.execute(
                """UPDATE account_profiles
                   SET onboarding_status = 'in_progress', updated_at = ?
                   WHERE account_id = ?""",
                (now, account_id),
            )
        return True

    def complete_onboarding(self, account_id: str) -> dict[str, Any]:
        items = self.onboarding_movies(account_id)
        if len(items) != 5 or any(item.get("sentiment") not in {"positive", "neutral", "negative"} for item in items):
            raise ValueError("需要恰好选择 5 部电影并完成每部反馈")

        scores: dict[tuple[str, str], int] = {}
        evidence: dict[tuple[str, str], list[dict[str, Any]]] = {}
        weights = {"positive": 2, "neutral": 0, "negative": -2}
        for item in items:
            weight = weights[item["sentiment"]]
            for group, label in (("genre", "类型"), ("theme", "主题"), ("mood", "观感")):
                values = item.get({"genre": "genres", "theme": "themes", "mood": "moods"}[group], [])[:4]
                for value in values:
                    key = (group, str(value))
                    scores[key] = scores.get(key, 0) + weight
                    evidence.setdefault(key, []).append(
                        {
                            "movie_id": item["id"],
                            "title": item["title_zh"],
                            "sentiment": item["sentiment"],
                        }
                    )

        ranked = sorted(
            ((abs(score), score, key) for key, score in scores.items() if score != 0),
            key=lambda value: (-value[0], value[2][0], value[2][1]),
        )[:8]
        dimensions = []
        label_map = {"genre": "类型", "theme": "主题", "mood": "观感"}
        for strength, score, key in ranked:
            group, value = key
            dimensions.append(
                {
                    "id": f"{group}:{value}",
                    "group": group,
                    "label": f"{label_map[group]} · {value}",
                    "direction": "prefer" if score > 0 else "avoid",
                    "confidence": "high" if strength >= 4 else "medium",
                    "evidence": evidence[key][:5],
                    "hidden": False,
                }
            )
        preferred = [item["label"].split(" · ", 1)[-1] for item in dimensions if item["direction"] == "prefer"][:3]
        avoided = [item["label"].split(" · ", 1)[-1] for item in dimensions if item["direction"] == "avoid"][:2]
        if preferred:
            summary = "目前更偏向" + "、".join(preferred)
            if avoided:
                summary += "；对" + "、".join(avoided) + "暂时更谨慎"
            summary += "。这些判断只来自你选的 5 部电影，可以随时修正。"
        else:
            summary = "这 5 部电影呈现出比较开放的口味，还没有形成强偏好；之后的反馈会继续修正。"

        now = utc_now()
        with self.transaction(immediate=True) as connection:
            self._ensure_profile(connection, account_id)
            current = connection.execute(
                "SELECT taste_version FROM account_profiles WHERE account_id = ?", (account_id,)
            ).fetchone()
            version = int(current["taste_version"] or 0) + 1
            connection.execute(
                """
                UPDATE account_profiles
                SET onboarding_status = 'completed', onboarding_completed_at = ?,
                    taste_summary = ?, taste_dimensions_json = ?, taste_version = ?,
                    taste_generated_at = ?, updated_at = ?
                WHERE account_id = ?
                """,
                (now, summary, json.dumps(dimensions, ensure_ascii=False), version, now, now, account_id),
            )
        return self.account_profile(account_id)

    def correct_taste_dimension(self, account_id: str, dimension_id: str) -> dict[str, Any]:
        profile = self.account_profile(account_id)
        dimensions = profile.get("taste_dimensions", [])
        found = False
        for dimension in dimensions:
            if dimension.get("id") == dimension_id:
                dimension["hidden"] = True
                dimension["corrected_by_user"] = True
                found = True
        if not found:
            raise ValueError("口味结论不存在")
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """UPDATE account_profiles
                   SET taste_dimensions_json = ?, taste_version = taste_version + 1,
                       updated_at = ? WHERE account_id = ?""",
                (json.dumps(dimensions, ensure_ascii=False), now, account_id),
            )
        return self.account_profile(account_id)

    def set_reflection_preference(self, account_id: str, enabled: bool) -> dict[str, Any]:
        with self.transaction(immediate=True) as connection:
            self._ensure_profile(connection, account_id)
            connection.execute(
                """UPDATE account_profiles
                   SET auto_generate_reflection_drafts = ?, updated_at = ?
                   WHERE account_id = ?""",
                (int(bool(enabled)), utc_now(), account_id),
            )
        return self.account_profile(account_id)

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
            existing = connection.execute(
                """SELECT 1 FROM movie_reflections
                   WHERE account_id = ? AND movie_id = ? AND status != 'deleted'""",
                (account_id, movie_id),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO movie_reflections (
                        id, account_id, movie_id, version, content, source, status,
                        based_on_version, created_at, updated_at, confirmed_at, deleted_at
                    ) VALUES (?, ?, ?, 1, ?, 'ai', 'confirmed', NULL, ?, ?, ?, NULL)
                    """,
                    ("refl_" + secrets.token_hex(8), account_id, movie_id, clean_note, now, now, now),
                )
        return self.get_movie_state(account_id, movie_id) or {}

    def reflection_bundle(self, account_id: str, movie_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, movie_id, version, content, source, status, based_on_version,
                       created_at, updated_at, confirmed_at
                FROM movie_reflections
                WHERE account_id = ? AND movie_id = ? AND status != 'deleted'
                ORDER BY version DESC
                """,
                (account_id, movie_id),
            ).fetchall()
        history = [dict(row) for row in rows]
        current = history[0] if history else None
        confirmed = next(
            (item for item in history if item["status"] in {"confirmed", "locked"}), None
        )
        return {"current": current, "confirmed": confirmed, "history": history}

    def create_reflection_version(
        self,
        account_id: str,
        movie_id: str,
        content: str,
        source: str,
        status: str,
        based_on_version: int | None = None,
    ) -> dict[str, Any]:
        clean = str(content).strip()
        if not 1 <= len(clean) <= 2000:
            raise ValueError("观后感必须在 1 到 2000 个字符之间")
        if source not in {"ai", "user", "ai_then_user"}:
            raise ValueError("观后感来源无效")
        if status not in {"draft", "confirmed"}:
            raise ValueError("新观后感状态无效")
        now = utc_now()
        with self.transaction(immediate=True) as connection:
            movie_state = connection.execute(
                """SELECT state FROM user_movie_states
                   WHERE account_id = ? AND movie_id = ?""",
                (account_id, movie_id),
            ).fetchone()
            if movie_state is None or movie_state["state"] != "watched":
                raise ValueError("只有看过的电影可以保存观后感")
            locked = connection.execute(
                """SELECT version FROM movie_reflections
                   WHERE account_id = ? AND movie_id = ? AND status = 'locked'
                   ORDER BY version DESC LIMIT 1""",
                (account_id, movie_id),
            ).fetchone()
            if locked and source == "ai" and based_on_version is None:
                based_on_version = int(locked["version"])
            version = int(
                connection.execute(
                    """SELECT COALESCE(MAX(version), 0) + 1 FROM movie_reflections
                       WHERE account_id = ? AND movie_id = ?""",
                    (account_id, movie_id),
                ).fetchone()[0]
            )
            confirmed_at = now if status == "confirmed" else None
            connection.execute(
                """
                INSERT INTO movie_reflections (
                    id, account_id, movie_id, version, content, source, status,
                    based_on_version, created_at, updated_at, confirmed_at, deleted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    "refl_" + secrets.token_hex(8), account_id, movie_id, version,
                    clean, source, status, based_on_version, now, now, confirmed_at,
                ),
            )
            if status == "confirmed":
                connection.execute(
                    """UPDATE user_movie_states SET note = ?, updated_at = ?
                       WHERE account_id = ? AND movie_id = ?""",
                    (clean, now, account_id, movie_id),
                )
        bundle = self.reflection_bundle(account_id, movie_id)
        return bundle["current"] or {}

    def set_reflection_status(
        self, account_id: str, movie_id: str, version: int, action: str
    ) -> dict[str, Any]:
        if action not in {"confirm", "lock", "unlock"}:
            raise ValueError("观后感操作无效")
        now = utc_now()
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                """SELECT * FROM movie_reflections
                   WHERE account_id = ? AND movie_id = ? AND version = ? AND status != 'deleted'""",
                (account_id, movie_id, version),
            ).fetchone()
            if row is None:
                raise ValueError("观后感版本不存在")
            if action == "confirm":
                target = "confirmed"
            elif action == "lock":
                target = "locked"
                connection.execute(
                    """UPDATE movie_reflections SET status = 'confirmed', updated_at = ?
                       WHERE account_id = ? AND movie_id = ? AND status = 'locked'""",
                    (now, account_id, movie_id),
                )
            else:
                if row["status"] != "locked":
                    raise ValueError("只有锁定版本可以解锁")
                target = "confirmed"
            connection.execute(
                """UPDATE movie_reflections
                   SET status = ?, updated_at = ?, confirmed_at = COALESCE(confirmed_at, ?)
                   WHERE account_id = ? AND movie_id = ? AND version = ?""",
                (target, now, now, account_id, movie_id, version),
            )
            connection.execute(
                """UPDATE user_movie_states SET note = ?, updated_at = ?
                   WHERE account_id = ? AND movie_id = ?""",
                (str(row["content"]), now, account_id, movie_id),
            )
        return self.reflection_bundle(account_id, movie_id)

    def delete_reflections(self, account_id: str, movie_id: str) -> bool:
        now = utc_now()
        with self.transaction(immediate=True) as connection:
            result = connection.execute(
                """UPDATE movie_reflections
                   SET content = '', status = 'deleted', deleted_at = ?, updated_at = ?
                   WHERE account_id = ? AND movie_id = ? AND status != 'deleted'""",
                (now, now, account_id, movie_id),
            )
            connection.execute(
                """UPDATE user_movie_states SET note = NULL, updated_at = ?
                   WHERE account_id = ? AND movie_id = ?""",
                (now, account_id, movie_id),
            )
        return result.rowcount > 0

    def remove_movie_state(self, account_id: str, movie_id: str) -> bool:
        with self.transaction(immediate=True) as connection:
            connection.execute(
                "DELETE FROM user_movie_feedback WHERE account_id = ? AND movie_id = ?",
                (account_id, movie_id),
            )
            now = utc_now()
            connection.execute(
                """UPDATE movie_reflections
                   SET content = '', status = 'deleted', deleted_at = ?, updated_at = ?
                   WHERE account_id = ? AND movie_id = ? AND status != 'deleted'""",
                (now, now, account_id, movie_id),
            )
            result = connection.execute(
                """
                DELETE FROM user_movie_states
                WHERE account_id = ? AND movie_id = ?
                """,
                (account_id, movie_id),
            )
            profile = connection.execute(
                "SELECT 1 FROM account_profiles WHERE account_id = ?", (account_id,)
            ).fetchone()
            if profile:
                count = int(connection.execute(
                    """SELECT COUNT(*) FROM user_movie_feedback
                       WHERE account_id = ? AND context = 'onboarding'""",
                    (account_id,),
                ).fetchone()[0])
                if count < 5:
                    connection.execute(
                        """UPDATE account_profiles SET onboarding_status = 'in_progress', updated_at = ?
                           WHERE account_id = ?""",
                        (now, account_id),
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
        items = [self._decode_movie_row(dict(row)) for row in rows]
        for item in items:
            bundle = self.reflection_bundle(account_id, str(item["movie_id"]))
            current = bundle["current"]
            if current:
                item["note"] = current["content"]
                item["reflection_status"] = current["status"]
                item["reflection_version"] = current["version"]
                item["reflection_source"] = current["source"]
                item["reflection_updated_at"] = current["updated_at"]
        return items

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

    def persistently_excluded_ids(self, account_id: str) -> set[str]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT movie_id FROM user_movie_states
                   WHERE account_id = ? AND state = 'disliked'""",
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
    ) -> dict[str, str]:
        impression_ids: dict[str, str] = {}
        with self.connect() as connection:
            for movie in movies:
                impression_id = "rec_" + secrets.token_hex(8)
                connection.execute(
                    """
                    INSERT INTO recommendation_impressions
                        (id, account_id, movie_id, request_summary, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        impression_id,
                        account_id,
                        movie["id"],
                        request_summary[:300],
                        utc_now(),
                    ),
                )
                impression_ids[str(movie["id"])] = impression_id
        return impression_ids

    def record_recommendation_feedback(
        self,
        account_id: str,
        impression_id: str,
        action: str,
        reason_code: str | None = None,
    ) -> dict[str, Any]:
        allowed = {
            "watchlist", "watched", "discuss", "not_now", "wrong_tone",
            "wrong_genre", "too_heavy", "not_interested", "other",
        }
        if action not in allowed:
            raise ValueError("推荐反馈动作无效")
        clean_reason = str(reason_code or "").strip()[:200] or None
        now = utc_now()
        with self.transaction(immediate=True) as connection:
            impression = connection.execute(
                """SELECT * FROM recommendation_impressions
                   WHERE id = ? AND account_id = ?""",
                (impression_id, account_id),
            ).fetchone()
            if impression is None:
                raise ValueError("推荐曝光不存在或不属于当前账户")
            movie_id = str(impression["movie_id"])
            feedback_id = "rfb_" + secrets.token_hex(8)
            connection.execute(
                """INSERT INTO recommendation_feedback (
                       id, impression_id, account_id, movie_id, action, reason_code, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (feedback_id, impression_id, account_id, movie_id, action, clean_reason, now),
            )
            target_state = {
                "watchlist": "watchlist",
                "watched": "watched",
                "discuss": "watched",
                "not_interested": "disliked",
            }.get(action)
            if target_state:
                connection.execute(
                    """
                    INSERT INTO user_movie_states (
                        account_id, movie_id, state, source, rating, note, created_at, updated_at
                    ) VALUES (?, ?, ?, 'recommendation_feedback', NULL, NULL, ?, ?)
                    ON CONFLICT(account_id, movie_id) DO UPDATE SET
                        state = excluded.state,
                        source = excluded.source,
                        updated_at = excluded.updated_at
                    """,
                    (account_id, movie_id, target_state, now, now),
                )
        return {
            "id": feedback_id,
            "impression_id": impression_id,
            "movie_id": movie_id,
            "action": action,
            "reason_code": clean_reason,
            "created_at": now,
        }

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

    def record_product_event(
        self,
        event_name: str,
        account_id: str | None = None,
        properties: dict[str, Any] | None = None,
        session_hint: str | None = None,
    ) -> None:
        forbidden = {
            "message", "content", "prompt", "reflection", "note", "audio",
            "api_key", "invite_code", "token", "transcript", "query",
        }
        safe: dict[str, Any] = {}
        for key, value in (properties or {}).items():
            normalized = str(key).lower()
            if any(part in normalized for part in forbidden):
                continue
            if isinstance(value, (bool, int, float)) or value is None:
                safe[str(key)] = value
            elif isinstance(value, str):
                safe[str(key)] = value[:120]
            elif isinstance(value, list):
                safe[str(key)] = [item for item in value[:20] if isinstance(item, (bool, int, float, str))]
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO product_events (
                       id, account_id, session_hint, event_name, properties_json,
                       schema_version, created_at
                   ) VALUES (?, ?, ?, ?, ?, 1, ?)""",
                (
                    "evt_" + secrets.token_hex(8), account_id,
                    str(session_hint or "")[:32] or None, str(event_name)[:100],
                    json.dumps(safe, ensure_ascii=False), utc_now(),
                ),
            )

    def record_model_usage(
        self,
        operation: str,
        provider: str,
        service_id: str,
        success: bool,
        latency_ms: int,
        account_id: str | None = None,
        input_units: int | None = None,
        output_units: int | None = None,
        error_category: str | None = None,
    ) -> None:
        pricing = self.usage_pricing()
        rate = pricing["rates"].get(str(operation), {})
        estimated_cost = round(
            (max(0, int(input_units or 0)) / 1_000_000) * float(rate.get("input_per_million", 0))
            + (max(0, int(output_units or 0)) / 1_000_000) * float(rate.get("output_per_million", 0))
            + float(rate.get("call_cost", 0)),
            9,
        )
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO model_usage_events (
                       id, account_id, operation, provider, service_id, success,
                       latency_ms, input_units, output_units, estimated_cost,
                       pricing_version, error_category, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "use_" + secrets.token_hex(8), account_id, str(operation)[:80],
                    str(provider)[:80], str(service_id)[:200], int(bool(success)),
                    max(0, int(latency_ms)), input_units, output_units,
                    estimated_cost, pricing["version"],
                    str(error_category or "")[:120] or None, utc_now(),
                ),
            )

    def usage_pricing(self) -> dict[str, Any]:
        settings = self.get_app_settings(("usage_pricing_version", "usage_pricing_json"))
        version = settings.get("usage_pricing_version", "unpriced-v1").strip() or "unpriced-v1"
        try:
            raw_rates = json.loads(settings.get("usage_pricing_json", "{}"))
        except (TypeError, ValueError):
            raw_rates = {}
        rates: dict[str, dict[str, float]] = {}
        if isinstance(raw_rates, dict):
            for operation, raw_rate in raw_rates.items():
                if not isinstance(raw_rate, dict):
                    continue
                rates[str(operation)[:80]] = {
                    key: max(0.0, float(raw_rate.get(key, 0) or 0))
                    for key in ("input_per_million", "output_per_million", "call_cost")
                }
        return {"version": version[:80], "currency": "CNY", "rates": rates}

    def save_usage_pricing(self, payload: dict[str, Any]) -> dict[str, Any]:
        version = str(payload.get("version", "")).strip()
        raw_rates = payload.get("rates")
        if not version or len(version) > 80:
            raise ValueError("价格版本必须为 1–80 个字符")
        if not isinstance(raw_rates, dict):
            raise ValueError("价格表格式不正确")
        rates: dict[str, dict[str, float]] = {}
        for operation, raw_rate in raw_rates.items():
            if not isinstance(raw_rate, dict):
                raise ValueError("每项价格必须包含输入、输出或单次调用价格")
            clean: dict[str, float] = {}
            for key in ("input_per_million", "output_per_million", "call_cost"):
                try:
                    value = float(raw_rate.get(key, 0) or 0)
                except (TypeError, ValueError) as error:
                    raise ValueError("价格必须是非负数字") from error
                if value < 0 or value > 1_000_000:
                    raise ValueError("价格必须在 0–1000000 之间")
                clean[key] = value
            rates[str(operation)[:80]] = clean
        self.set_app_settings({
            "usage_pricing_version": version,
            "usage_pricing_json": json.dumps(rates, ensure_ascii=False, sort_keys=True),
        })
        return self.usage_pricing()

    def metrics_summary(self) -> dict[str, Any]:
        with self.connect() as connection:
            accounts = int(connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0])
            onboarding_rows = connection.execute(
                """SELECT onboarding_status, COUNT(*) AS count FROM account_profiles
                   GROUP BY onboarding_status"""
            ).fetchall()
            event_rows = connection.execute(
                """SELECT event_name, COUNT(*) AS count FROM product_events
                   GROUP BY event_name ORDER BY event_name"""
            ).fetchall()
            usage_rows = connection.execute(
                """SELECT operation, COUNT(*) AS calls,
                          SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS failures,
                          ROUND(AVG(latency_ms), 1) AS average_latency_ms,
                          ROUND(SUM(estimated_cost), 6) AS estimated_cost
                   FROM model_usage_events GROUP BY operation ORDER BY operation"""
            ).fetchall()
            exposure_count = int(connection.execute(
                "SELECT COUNT(*) FROM recommendation_impressions"
            ).fetchone()[0])
            feedback_rows = connection.execute(
                """SELECT action, COUNT(*) AS count FROM recommendation_feedback
                   GROUP BY action ORDER BY action"""
            ).fetchall()
            reflection_rows = connection.execute(
                """SELECT status, COUNT(*) AS count FROM movie_reflections
                   GROUP BY status ORDER BY status"""
            ).fetchall()
        return {
            "accounts": accounts,
            "onboarding": {str(row["onboarding_status"]): int(row["count"]) for row in onboarding_rows},
            "events": {str(row["event_name"]): int(row["count"]) for row in event_rows},
            "recommendations": {
                "impressions": exposure_count,
                "feedback": {str(row["action"]): int(row["count"]) for row in feedback_rows},
            },
            "reflections": {str(row["status"]): int(row["count"]) for row in reflection_rows},
            "usage": [dict(row) for row in usage_rows],
            "pricing": self.usage_pricing(),
        }

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
