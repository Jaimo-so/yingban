from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import string
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlencode


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
    def __init__(
        self,
        path: Path,
        invite_pepper: str,
        session_secret: str,
        *,
        journal_mode: str = "WAL",
        vfs: str = "",
        required_mount: str | Path | None = None,
    ):
        self.path = Path(path).expanduser().resolve()
        self.invite_pepper = invite_pepper.encode("utf-8")
        self.session_secret = session_secret.encode("utf-8")
        self.journal_mode = journal_mode.strip().upper()
        if self.journal_mode not in {"WAL", "DELETE"}:
            raise ValueError(
                "journal_mode must be WAL for local storage or DELETE for network storage"
            )
        self.vfs = vfs.strip()
        if self.vfs not in {"", "unix-dotfile"}:
            raise ValueError("vfs must be empty (platform default) or unix-dotfile")
        if self.vfs == "unix-dotfile" and self.journal_mode != "DELETE":
            raise ValueError("unix-dotfile requires journal_mode=DELETE")
        self.required_mount = Path(required_mount).resolve() if required_mount else None
        if self.required_mount:
            if not self.required_mount.is_mount() or not self.path.is_relative_to(self.required_mount):
                raise RuntimeError("required database mount is missing or does not contain database")
            if not self.path.is_file():
                raise RuntimeError("persistent database is missing; restore it before starting")
        self._schema_lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        if self.required_mount and not self.required_mount.is_mount():
            raise RuntimeError("required database mount is missing")
        options = {"mode": "rw" if self.required_mount else "rwc"}
        if self.vfs:
            options["vfs"] = self.vfs
        connection = sqlite3.connect(
            self.path.as_uri() + "?" + urlencode(options),
            uri=True, timeout=10, factory=ClosingConnection,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            actual_mode = connection.execute(
                f"PRAGMA journal_mode = {self.journal_mode}"
            ).fetchone()[0]
            if str(actual_mode).upper() != self.journal_mode:
                raise RuntimeError(
                    f"SQLite refused journal_mode={self.journal_mode}; got {actual_mode}"
                )
            if self.journal_mode == "DELETE":
                connection.execute("PRAGMA synchronous = FULL")
            return connection
        except BaseException:
            connection.close()
            raise

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
                    note TEXT NOT NULL DEFAULT '',
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
                    release_date TEXT NOT NULL DEFAULT '',
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
                    weekly_recommendations_enabled INTEGER NOT NULL DEFAULT 1
                        CHECK(weekly_recommendations_enabled IN (0, 1)),
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

                CREATE TABLE IF NOT EXISTS conversation_summaries (
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                    movie_id TEXT NOT NULL REFERENCES movies(id) ON DELETE CASCADE,
                    summary TEXT NOT NULL,
                    topics_json TEXT NOT NULL DEFAULT '[]',
                    open_questions_json TEXT NOT NULL DEFAULT '[]',
                    spoilers_allowed INTEGER NOT NULL DEFAULT 0
                        CHECK(spoilers_allowed IN (0, 1)),
                    consent_status TEXT NOT NULL DEFAULT 'active'
                        CHECK(consent_status IN ('active', 'revoked')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deleted_at TEXT,
                    UNIQUE(account_id, movie_id)
                );

                CREATE TABLE IF NOT EXISTS conversation_records (
                    id TEXT NOT NULL,
                    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                    movie_id TEXT NOT NULL REFERENCES movies(id) ON DELETE CASCADE,
                    messages_json TEXT NOT NULL DEFAULT '[]',
                    skill_key TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(account_id, id)
                );

                CREATE TABLE IF NOT EXISTS weekly_recommendations (
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                    week_key TEXT NOT NULL,
                    movie_ids_json TEXT NOT NULL DEFAULT '[]',
                    reason_summary TEXT NOT NULL DEFAULT '',
                    source_inputs_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'ready'
                        CHECK(status IN ('ready', 'viewed', 'dismissed', 'expired')),
                    created_at TEXT NOT NULL,
                    viewed_at TEXT,
                    expires_at TEXT NOT NULL,
                    UNIQUE(account_id, week_key)
                );

                CREATE TABLE IF NOT EXISTS monthly_recaps (
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                    month_key TEXT NOT NULL,
                    content_json TEXT NOT NULL DEFAULT '{}',
                    source_movie_ids_json TEXT NOT NULL DEFAULT '[]',
                    source_reflection_ids_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'draft'
                        CHECK(status IN ('draft', 'confirmed', 'deleted')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(account_id, month_key)
                );

                CREATE TABLE IF NOT EXISTS background_jobs (
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                    job_type TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued'
                        CHECK(status IN ('queued', 'running', 'succeeded', 'failed', 'expired')),
                    input_reference_json TEXT NOT NULL DEFAULT '{}',
                    result_json TEXT NOT NULL DEFAULT '{}',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 2,
                    available_at TEXT NOT NULL,
                    error_category TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    expires_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS share_cards (
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                    public_token TEXT NOT NULL UNIQUE,
                    source_type TEXT NOT NULL
                        CHECK(source_type IN ('reflection', 'monthly_recap', 'taste_dimension')),
                    source_id TEXT NOT NULL,
                    content_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ('active', 'revoked', 'expired')),
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    revoked_at TEXT
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

                CREATE TABLE IF NOT EXISTS skill_definitions (
                    skill_key TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL,
                    module TEXT NOT NULL CHECK(module IN ('discussion', 'recommendation')),
                    activation_mode TEXT NOT NULL CHECK(activation_mode IN (
                        'explicit_or_intent', 'module_default'
                    )),
                    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
                    active_version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS skill_versions (
                    skill_key TEXT NOT NULL REFERENCES skill_definitions(skill_key) ON DELETE CASCADE,
                    version INTEGER NOT NULL,
                    instructions TEXT NOT NULL,
                    input_contract_json TEXT NOT NULL DEFAULT '{}',
                    output_contract_json TEXT NOT NULL DEFAULT '{}',
                    change_note TEXT NOT NULL DEFAULT '',
                    published_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(skill_key, version)
                );

                CREATE TABLE IF NOT EXISTS viewing_cognition_entries (
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                    movie_id TEXT NOT NULL REFERENCES movies(id) ON DELETE CASCADE,
                    viewing_round INTEGER NOT NULL CHECK(viewing_round >= 1),
                    stage TEXT NOT NULL CHECK(stage IN (
                        'first_impression', 'post_discussion', 'revisit',
                        'rewatch', 'retrospective'
                    )),
                    watched_at TEXT,
                    edition TEXT NOT NULL DEFAULT '',
                    raw_impression TEXT NOT NULL,
                    synthesis TEXT NOT NULL,
                    dimensions_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'confirmed'
                        CHECK(status IN ('confirmed', 'deleted')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deleted_at TEXT
                );

                CREATE TABLE IF NOT EXISTS content_drafts (
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                    movie_id TEXT NOT NULL REFERENCES movies(id) ON DELETE CASCADE,
                    content_scene TEXT NOT NULL CHECK(content_scene IN (
                        'xiaohongshu', 'formal_review', 'promotion'
                    )),
                    version INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    source_material TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'draft'
                        CHECK(status IN ('draft', 'confirmed', 'deleted')),
                    based_on_version INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    confirmed_at TEXT,
                    deleted_at TEXT,
                    UNIQUE(account_id, movie_id, content_scene, version)
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
                CREATE INDEX IF NOT EXISTS idx_conversation_summaries_account
                    ON conversation_summaries(account_id, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_conversation_records_account_movie
                    ON conversation_records(account_id, movie_id, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_weekly_recommendations_account
                    ON weekly_recommendations(account_id, week_key DESC);
                CREATE INDEX IF NOT EXISTS idx_monthly_recaps_account
                    ON monthly_recaps(account_id, month_key DESC);
                CREATE INDEX IF NOT EXISTS idx_background_jobs_account
                    ON background_jobs(account_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_share_cards_account
                    ON share_cards(account_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_product_events_name_time
                    ON product_events(event_name, created_at);
                CREATE INDEX IF NOT EXISTS idx_model_usage_operation_time
                    ON model_usage_events(operation, created_at);
                CREATE INDEX IF NOT EXISTS idx_skill_versions_key_version
                    ON skill_versions(skill_key, version DESC);
                CREATE INDEX IF NOT EXISTS idx_cognition_account_movie
                    ON viewing_cognition_entries(account_id, movie_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_content_drafts_account_movie
                    ON content_drafts(account_id, movie_id, updated_at DESC);
                """
            )
            movie_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(movies)").fetchall()
            }
            invite_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(invite_codes)").fetchall()
            }
            if "note" not in invite_columns:
                connection.execute(
                    "ALTER TABLE invite_codes ADD COLUMN note TEXT NOT NULL DEFAULT ''"
                )
            for column, definition in (
                ("poster_url", "TEXT NOT NULL DEFAULT ''"),
                ("source_url", "TEXT NOT NULL DEFAULT ''"),
                ("external_ids_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("release_date", "TEXT NOT NULL DEFAULT ''"),
            ):
                if column not in movie_columns:
                    connection.execute(f"ALTER TABLE movies ADD COLUMN {column} {definition}")

            profile_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(account_profiles)").fetchall()
            }
            if "weekly_recommendations_enabled" not in profile_columns:
                connection.execute(
                    "ALTER TABLE account_profiles ADD COLUMN "
                    "weekly_recommendations_enabled INTEGER NOT NULL DEFAULT 1 "
                    "CHECK(weekly_recommendations_enabled IN (0, 1))"
                )

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

    def ensure_builtin_skills(self, definitions: tuple[dict[str, Any], ...]) -> None:
        """Seed immutable built-in identities without overwriting administrator versions."""
        now = utc_now()
        with self.transaction(immediate=True) as connection:
            for definition in definitions:
                skill_key = str(definition["key"])
                exists = connection.execute(
                    "SELECT 1 FROM skill_definitions WHERE skill_key = ?", (skill_key,)
                ).fetchone()
                if exists:
                    continue
                connection.execute(
                    """
                    INSERT INTO skill_definitions (
                        skill_key, name, description, module, activation_mode,
                        enabled, active_version, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 1, 1, ?, ?)
                    """,
                    (
                        skill_key,
                        str(definition["name"]),
                        str(definition["description"]),
                        str(definition["module"]),
                        str(definition["activation_mode"]),
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO skill_versions (
                        skill_key, version, instructions, input_contract_json,
                        output_contract_json, change_note, published_at,
                        created_at, updated_at
                    ) VALUES (?, 1, ?, ?, ?, '内置初始版本', ?, ?, ?)
                    """,
                    (
                        skill_key,
                        str(definition["instructions"]),
                        json.dumps(definition.get("input_contract", {}), ensure_ascii=False),
                        json.dumps(definition.get("output_contract", {}), ensure_ascii=False),
                        now,
                        now,
                        now,
                    ),
                )

    @staticmethod
    def _decode_skill_row(row: dict[str, Any]) -> dict[str, Any]:
        row["enabled"] = bool(row.get("enabled"))
        for key in ("input_contract_json", "output_contract_json"):
            if key in row:
                row[key.removesuffix("_json")] = json.loads(row.pop(key) or "{}")
        return row

    def skill_bundle(self, skill_key: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            definition = connection.execute(
                "SELECT * FROM skill_definitions WHERE skill_key = ?", (skill_key,)
            ).fetchone()
            if definition is None:
                return None
            versions = connection.execute(
                """
                SELECT version, instructions, input_contract_json,
                       output_contract_json, change_note, published_at,
                       created_at, updated_at
                FROM skill_versions
                WHERE skill_key = ?
                ORDER BY version DESC
                """,
                (skill_key,),
            ).fetchall()
        decoded_versions = [self._decode_skill_row(dict(row)) for row in versions]
        result = self._decode_skill_row(dict(definition))
        active_version = int(result["active_version"])
        result["active"] = next(
            (item for item in decoded_versions if int(item["version"]) == active_version), None
        )
        result["draft"] = next(
            (item for item in decoded_versions if item.get("published_at") is None), None
        )
        result["versions"] = [
            {
                "version": item["version"],
                "change_note": item["change_note"],
                "published_at": item["published_at"],
                "is_active": int(item["version"]) == active_version,
            }
            for item in decoded_versions
            if item.get("published_at") is not None
        ]
        return result

    def list_skills(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            keys = [
                str(row["skill_key"])
                for row in connection.execute(
                    """
                    SELECT skill_key FROM skill_definitions
                    ORDER BY CASE module WHEN 'discussion' THEN 0 ELSE 1 END, created_at
                    """
                ).fetchall()
            ]
        return [bundle for key in keys if (bundle := self.skill_bundle(key)) is not None]

    def active_skill(self, skill_key: str, module: str | None = None) -> dict[str, Any] | None:
        bundle = self.skill_bundle(skill_key)
        if not bundle or not bundle["enabled"] or bundle.get("active") is None:
            return None
        if module and bundle["module"] != module:
            return None
        return bundle

    def set_skill_enabled(self, skill_key: str, enabled: bool) -> dict[str, Any]:
        with self.connect() as connection:
            result = connection.execute(
                "UPDATE skill_definitions SET enabled = ?, updated_at = ? WHERE skill_key = ?",
                (int(bool(enabled)), utc_now(), skill_key),
            )
        if result.rowcount != 1:
            raise ValueError("Skill 不存在")
        return self.skill_bundle(skill_key) or {}

    def save_skill_draft(
        self,
        skill_key: str,
        instructions: str,
        input_contract: dict[str, Any],
        output_contract: dict[str, Any],
        change_note: str,
    ) -> dict[str, Any]:
        clean = str(instructions).strip()
        if not 100 <= len(clean) <= 30_000:
            raise ValueError("Skill 指令长度必须在 100 到 30000 个字符之间")
        if not isinstance(input_contract, dict) or not isinstance(output_contract, dict):
            raise ValueError("Skill 输入和输出契约必须是 JSON 对象")
        note = str(change_note).strip()[:500]
        now = utc_now()
        with self.transaction(immediate=True) as connection:
            definition = connection.execute(
                "SELECT 1 FROM skill_definitions WHERE skill_key = ?", (skill_key,)
            ).fetchone()
            if definition is None:
                raise ValueError("Skill 不存在")
            draft = connection.execute(
                """
                SELECT version FROM skill_versions
                WHERE skill_key = ? AND published_at IS NULL
                ORDER BY version DESC LIMIT 1
                """,
                (skill_key,),
            ).fetchone()
            version = int(draft["version"]) if draft else int(
                connection.execute(
                    "SELECT COALESCE(MAX(version), 0) + 1 FROM skill_versions WHERE skill_key = ?",
                    (skill_key,),
                ).fetchone()[0]
            )
            connection.execute(
                """
                INSERT INTO skill_versions (
                    skill_key, version, instructions, input_contract_json,
                    output_contract_json, change_note, published_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)
                ON CONFLICT(skill_key, version) DO UPDATE SET
                    instructions = excluded.instructions,
                    input_contract_json = excluded.input_contract_json,
                    output_contract_json = excluded.output_contract_json,
                    change_note = excluded.change_note,
                    updated_at = excluded.updated_at
                """,
                (
                    skill_key,
                    version,
                    clean,
                    json.dumps(input_contract, ensure_ascii=False),
                    json.dumps(output_contract, ensure_ascii=False),
                    note,
                    now,
                    now,
                ),
            )
            connection.execute(
                "UPDATE skill_definitions SET updated_at = ? WHERE skill_key = ?",
                (now, skill_key),
            )
        return self.skill_bundle(skill_key) or {}

    def publish_skill_draft(self, skill_key: str) -> dict[str, Any]:
        now = utc_now()
        with self.transaction(immediate=True) as connection:
            draft = connection.execute(
                """
                SELECT version FROM skill_versions
                WHERE skill_key = ? AND published_at IS NULL
                ORDER BY version DESC LIMIT 1
                """,
                (skill_key,),
            ).fetchone()
            if draft is None:
                raise ValueError("没有可发布的 Skill 草稿")
            version = int(draft["version"])
            connection.execute(
                """UPDATE skill_versions SET published_at = ?, updated_at = ?
                   WHERE skill_key = ? AND version = ?""",
                (now, now, skill_key, version),
            )
            connection.execute(
                """UPDATE skill_definitions SET active_version = ?, updated_at = ?
                   WHERE skill_key = ?""",
                (version, now, skill_key),
            )
        return self.skill_bundle(skill_key) or {}

    def rollback_skill(self, skill_key: str) -> dict[str, Any]:
        with self.transaction(immediate=True) as connection:
            definition = connection.execute(
                "SELECT active_version FROM skill_definitions WHERE skill_key = ?", (skill_key,)
            ).fetchone()
            if definition is None:
                raise ValueError("Skill 不存在")
            previous = connection.execute(
                """
                SELECT version FROM skill_versions
                WHERE skill_key = ? AND published_at IS NOT NULL AND version < ?
                ORDER BY version DESC LIMIT 1
                """,
                (skill_key, int(definition["active_version"])),
            ).fetchone()
            if previous is None:
                raise ValueError("当前 Skill 没有可回滚的历史版本")
            connection.execute(
                "UPDATE skill_definitions SET active_version = ?, updated_at = ? WHERE skill_key = ?",
                (int(previous["version"]), utc_now(), skill_key),
            )
        return self.skill_bundle(skill_key) or {}

    def save_cognition_entry(
        self,
        account_id: str,
        movie_id: str,
        viewing_round: int,
        stage: str,
        raw_impression: str,
        synthesis: str,
        dimensions: list[Any],
        watched_at: str | None = None,
        edition: str = "",
    ) -> dict[str, Any]:
        stages = {
            "first_impression", "post_discussion", "revisit", "rewatch", "retrospective"
        }
        try:
            viewing_round = int(viewing_round)
        except (TypeError, ValueError) as error:
            raise ValueError("观看轮次必须是正整数") from error
        if viewing_round < 1 or viewing_round > 999:
            raise ValueError("观看轮次必须在 1 到 999 之间")
        if stage not in stages:
            raise ValueError("观影阶段无效")
        raw = str(raw_impression).strip()
        summary = str(synthesis).strip()
        if not 1 <= len(raw) <= 6000:
            raise ValueError("本次原始感受必须在 1 到 6000 个字符之间")
        if not 1 <= len(summary) <= 6000:
            raise ValueError("本次认知整理必须在 1 到 6000 个字符之间")
        clean_date = str(watched_at or "").strip()
        if clean_date:
            try:
                datetime.fromisoformat(clean_date)
            except ValueError as error:
                raise ValueError("观看日期格式无效") from error
        clean_dimensions: list[str] = []
        for item in dimensions[:20] if isinstance(dimensions, list) else []:
            value = str(item).strip()
            if value and value not in clean_dimensions:
                clean_dimensions.append(value[:80])
        now = utc_now()
        with self.transaction(immediate=True) as connection:
            state = connection.execute(
                """SELECT state FROM user_movie_states
                   WHERE account_id = ? AND movie_id = ?""",
                (account_id, movie_id),
            ).fetchone()
            if state is None or state["state"] != "watched":
                raise ValueError("只有已经确认看过的电影可以保存阶段认知")
            entry_id = "cog_" + secrets.token_hex(8)
            connection.execute(
                """
                INSERT INTO viewing_cognition_entries (
                    id, account_id, movie_id, viewing_round, stage, watched_at,
                    edition, raw_impression, synthesis, dimensions_json,
                    status, created_at, updated_at, deleted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', ?, ?, NULL)
                """,
                (
                    entry_id,
                    account_id,
                    movie_id,
                    viewing_round,
                    stage,
                    clean_date or None,
                    str(edition).strip()[:200],
                    raw,
                    summary,
                    json.dumps(clean_dimensions, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        return self.cognition_bundle(account_id, movie_id)

    def cognition_bundle(self, account_id: str, movie_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, movie_id, viewing_round, stage, watched_at, edition,
                       raw_impression, synthesis, dimensions_json, created_at, updated_at
                FROM viewing_cognition_entries
                WHERE account_id = ? AND movie_id = ? AND status = 'confirmed'
                ORDER BY COALESCE(watched_at, created_at), created_at
                """,
                (account_id, movie_id),
            ).fetchall()
        entries: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["dimensions"] = json.loads(item.pop("dimensions_json") or "[]")
            entries.append(item)
        comparison = None
        if len(entries) >= 2:
            previous, current = entries[-2], entries[-1]
            previous_dimensions = set(previous["dimensions"])
            current_dimensions = set(current["dimensions"])
            comparison = {
                "previous_entry_id": previous["id"],
                "current_entry_id": current["id"],
                "previous_round": previous["viewing_round"],
                "current_round": current["viewing_round"],
                "previous_synthesis": previous["synthesis"],
                "current_synthesis": current["synthesis"],
                "unchanged_dimensions": sorted(previous_dimensions & current_dimensions),
                "new_dimensions": sorted(current_dimensions - previous_dimensions),
                "not_repeated_dimensions": sorted(previous_dimensions - current_dimensions),
            }
        return {"entries": entries, "comparison": comparison}

    def delete_cognition_entry(self, account_id: str, entry_id: str) -> bool:
        now = utc_now()
        with self.connect() as connection:
            result = connection.execute(
                """
                UPDATE viewing_cognition_entries
                SET raw_impression = '', synthesis = '', dimensions_json = '[]',
                    status = 'deleted', updated_at = ?, deleted_at = ?
                WHERE account_id = ? AND id = ? AND status != 'deleted'
                """,
                (now, now, account_id, entry_id),
            )
        return result.rowcount > 0

    def create_content_draft(
        self,
        account_id: str,
        movie_id: str,
        content_scene: str,
        content: str,
        source_material: str = "",
    ) -> dict[str, Any]:
        if content_scene not in {"xiaohongshu", "formal_review", "promotion"}:
            raise ValueError("内容场景无效")
        clean = str(content).strip()
        if not 20 <= len(clean) <= 30_000:
            raise ValueError("内容草稿必须在 20 到 30000 个字符之间")
        source = str(source_material).strip()[:6000]
        now = utc_now()
        with self.transaction(immediate=True) as connection:
            state = connection.execute(
                """SELECT state FROM user_movie_states
                   WHERE account_id = ? AND movie_id = ?""",
                (account_id, movie_id),
            ).fetchone()
            if state is None or state["state"] != "watched":
                raise ValueError("只有已经确认看过的电影可以保存内容草稿")
            version = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(version), 0) + 1 FROM content_drafts
                    WHERE account_id = ? AND movie_id = ? AND content_scene = ?
                    """,
                    (account_id, movie_id, content_scene),
                ).fetchone()[0]
            )
            based_on = version - 1 if version > 1 else None
            draft_id = "draft_" + secrets.token_hex(8)
            connection.execute(
                """
                INSERT INTO content_drafts (
                    id, account_id, movie_id, content_scene, version, content,
                    source_material, status, based_on_version, created_at,
                    updated_at, confirmed_at, deleted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?, NULL, NULL)
                """,
                (
                    draft_id,
                    account_id,
                    movie_id,
                    content_scene,
                    version,
                    clean,
                    source,
                    based_on,
                    now,
                    now,
                ),
            )
        return self.content_draft_bundle(account_id, movie_id)

    def content_draft_bundle(self, account_id: str, movie_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, movie_id, content_scene, version, content, source_material,
                       status, based_on_version, created_at, updated_at, confirmed_at
                FROM content_drafts
                WHERE account_id = ? AND movie_id = ? AND status != 'deleted'
                ORDER BY updated_at DESC
                """,
                (account_id, movie_id),
            ).fetchall()
        return {"items": [dict(row) for row in rows]}

    def delete_content_draft(self, account_id: str, draft_id: str) -> bool:
        now = utc_now()
        with self.connect() as connection:
            result = connection.execute(
                """
                UPDATE content_drafts
                SET content = '', source_material = '', status = 'deleted',
                    updated_at = ?, deleted_at = ?
                WHERE account_id = ? AND id = ? AND status != 'deleted'
                """,
                (now, now, account_id, draft_id),
            )
        return result.rowcount > 0

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
                "weekly_recommendations_enabled": True,
            }
        result = dict(row)
        result["taste_dimensions"] = json.loads(result.pop("taste_dimensions_json") or "[]")
        result["auto_generate_reflection_drafts"] = bool(
            result["auto_generate_reflection_drafts"]
        )
        result["weekly_recommendations_enabled"] = bool(
            result["weekly_recommendations_enabled"]
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
        if not 1 <= len(items) <= 5 or any(item.get("sentiment") not in {"positive", "neutral", "negative"} for item in items):
            raise ValueError("请选择 1～5 部电影并完成每部反馈")

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
            summary += f"。这些判断只来自你选的 {len(items)} 部电影，可以随时修正。"
        else:
            summary = f"这 {len(items)} 部电影呈现出比较开放的口味，还没有形成强偏好；之后的反馈会继续修正。"

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

    def set_weekly_recommendations_preference(
        self, account_id: str, enabled: bool
    ) -> dict[str, Any]:
        with self.transaction(immediate=True) as connection:
            self._ensure_profile(connection, account_id)
            connection.execute(
                """UPDATE account_profiles
                   SET weekly_recommendations_enabled = ?, updated_at = ?
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

    def _recoverable_invite_code(self, invite_id: str) -> str:
        """Derive a copyable invite without storing its plaintext in SQLite."""
        digest = hmac.new(
            self.invite_pepper,
            f"yingban-invite-code-v1:{invite_id}".encode("utf-8"),
            hashlib.sha256,
        ).digest()
        # INVITE_ALPHABET has exactly 32 symbols, so every character carries
        # five bits. Twenty characters preserve the existing 100-bit code space.
        value = int.from_bytes(digest[:13], "big") >> 4
        characters = [
            INVITE_ALPHABET[(value >> shift) & 31]
            for shift in range(95, -1, -5)
        ]
        groups = ["".join(characters[index:index + 5]) for index in range(0, 20, 5)]
        return "YB-" + "-".join(groups)

    def _recover_invite_code(self, invite_id: str, code_digest: str) -> str | None:
        code = self._recoverable_invite_code(invite_id)
        if hmac.compare_digest(self._invite_digest(code), str(code_digest)):
            return code
        return None

    @staticmethod
    def _clean_invite_note(note: str) -> str:
        clean = " ".join(str(note).split())
        if len(clean) > 200:
            raise ValueError("邀请码备注不能超过 200 个字符")
        return clean

    def generate_invites(self, count: int, note: str = "") -> list[str]:
        if count < 1 or count > 100:
            raise ValueError("count must be between 1 and 100")
        clean_note = self._clean_invite_note(note)
        codes: list[str] = []
        with self.transaction(immediate=True) as connection:
            for _ in range(count):
                for _attempt in range(10):
                    invite_id = "inv_" + secrets.token_hex(8)
                    code = self._recoverable_invite_code(invite_id)
                    try:
                        connection.execute(
                            """
                            INSERT INTO invite_codes
                                (id, code_digest, code_hint, note, status, created_at)
                            VALUES (?, ?, ?, ?, 'issued', ?)
                            """,
                            (
                                invite_id,
                                self._invite_digest(code),
                                code[-5:],
                                clean_note,
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
                SELECT i.id, i.code_digest, i.code_hint, i.note, i.status, i.account_id,
                       i.created_at, i.activated_at, i.revoked_at, i.replaced_by,
                       CASE WHEN i.account_id IS NULL THEN 0 ELSE (
                           SELECT COUNT(*) FROM model_usage_events u
                           WHERE u.account_id = i.account_id
                             AND u.provider != 'deterministic'
                       ) END AS model_calls,
                       CASE WHEN i.account_id IS NULL THEN 0 ELSE (
                           SELECT COUNT(*) FROM conversation_records c
                           WHERE c.account_id = i.account_id
                       ) END AS conversation_count,
                       CASE WHEN i.account_id IS NULL THEN 0 ELSE (
                           SELECT COUNT(DISTINCT c.movie_id) FROM conversation_records c
                           WHERE c.account_id = i.account_id
                       ) END AS movie_count
                FROM invite_codes i ORDER BY i.created_at DESC
                """
            ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["copy_available"] = bool(
                item["status"] != "revoked"
                and self._recover_invite_code(item["id"], item["code_digest"])
            )
            item.pop("code_digest", None)
            items.append(item)
        return items

    def invite_code_for_admin(self, invite_id: str) -> str:
        with self.connect() as connection:
            invite = connection.execute(
                "SELECT id, code_digest, status FROM invite_codes WHERE id = ?",
                (invite_id,),
            ).fetchone()
        if invite is None:
            raise InviteError("邀请码不存在")
        if invite["status"] == "revoked":
            raise InviteError("已停用的邀请码不可复制")
        code = self._recover_invite_code(str(invite["id"]), str(invite["code_digest"]))
        if code is None:
            raise InviteError("该邀请码生成于保存功能上线前，或邀请码密钥已经更换，无法恢复完整内容")
        return code

    def update_invite_note(self, invite_id: str, note: str) -> dict[str, Any]:
        clean_note = self._clean_invite_note(note)
        with self.connect() as connection:
            result = connection.execute(
                "UPDATE invite_codes SET note = ? WHERE id = ?",
                (clean_note, invite_id),
            )
            if result.rowcount != 1:
                raise InviteError("邀请码不存在")
        return next(item for item in self.list_invites() if item["id"] == invite_id)

    def save_conversation_record(
        self,
        account_id: str,
        conversation_id: str,
        movie_id: str,
        messages: list[dict[str, Any]],
        skill_key: str | None = None,
    ) -> dict[str, Any]:
        clean_id = str(conversation_id).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", clean_id):
            raise ValueError("对话标识无效")
        if self.movie(movie_id) is None:
            raise ValueError("电影不存在")
        if not isinstance(messages, list):
            raise ValueError("对话消息格式无效")
        clean_messages: list[dict[str, str]] = []
        total_characters = 0
        for item in messages[-40:]:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role", ""))
            content = str(item.get("content", "")).strip()
            if role not in {"user", "assistant"} or not content:
                continue
            content = content[:6000]
            total_characters += len(content)
            if total_characters > 120_000:
                raise ValueError("对话内容过长")
            clean_messages.append({"role": role, "content": content})
        if not clean_messages:
            raise ValueError("对话消息不能为空")
        clean_skill_key = str(skill_key or "").strip()[:100] or None
        now = utc_now()
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT created_at FROM conversation_records WHERE account_id = ? AND id = ?",
                (account_id, clean_id),
            ).fetchone()
            created_at = str(existing["created_at"]) if existing else now
            connection.execute(
                """INSERT INTO conversation_records (
                       id, account_id, movie_id, messages_json, skill_key,
                       created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(account_id, id) DO UPDATE SET
                       movie_id = excluded.movie_id,
                       messages_json = excluded.messages_json,
                       skill_key = excluded.skill_key,
                       updated_at = excluded.updated_at""",
                (
                    clean_id,
                    account_id,
                    str(movie_id),
                    json.dumps(clean_messages, ensure_ascii=False),
                    clean_skill_key,
                    created_at,
                    now,
                ),
            )
        return {
            "id": clean_id,
            "movie_id": str(movie_id),
            "messages": clean_messages,
            "skill_key": clean_skill_key,
            "created_at": created_at,
            "updated_at": now,
        }

    def invite_conversations(self, invite_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            invite = connection.execute(
                """SELECT id, code_hint, note, status, account_id
                   FROM invite_codes WHERE id = ?""",
                (invite_id,),
            ).fetchone()
            if invite is None:
                raise InviteError("邀请码不存在")
            rows = []
            if invite["account_id"]:
                rows = connection.execute(
                    """SELECT c.id, c.movie_id, c.messages_json, c.skill_key,
                              c.created_at, c.updated_at,
                              m.title_zh, m.title_original, m.year
                       FROM conversation_records c
                       JOIN movies m ON m.id = c.movie_id
                       WHERE c.account_id = ?
                       ORDER BY c.updated_at DESC""",
                    (invite["account_id"],),
                ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["messages"] = json.loads(item.pop("messages_json") or "[]")
            items.append(item)
        return {"invite": dict(invite), "items": items}

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
        now = utc_now()
        with self.transaction(immediate=True) as connection:
            current = connection.execute(
                "SELECT * FROM invite_codes WHERE id = ?", (invite_id,)
            ).fetchone()
            if current is None or current["status"] != "active":
                raise InviteError("只有已激活的邀请码可以换发")
            new_id = "inv_" + secrets.token_hex(8)
            new_code = self._recoverable_invite_code(new_id)
            connection.execute(
                """
                INSERT INTO invite_codes
                    (id, code_digest, code_hint, note, status, account_id,
                     created_at, activated_at)
                VALUES (?, ?, ?, ?, 'active', ?, ?, ?)
                """,
                (
                    new_id,
                    self._invite_digest(new_code),
                    new_code[-5:],
                    str(current["note"] or ""),
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
                        source, poster_url, source_url, external_ids_json, release_date,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        release_date = CASE
                            WHEN excluded.release_date != '' THEN excluded.release_date
                            ELSE movies.release_date
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
                        str(record.get("release_date", ""))[:10],
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

    def transition_movie_state(
        self,
        account_id: str,
        movie_id: str,
        state: str,
        source: str,
    ) -> tuple[dict[str, Any], bool]:
        """Change state once and report whether the durable state really changed."""
        if state not in {"watched", "watchlist", "disliked"}:
            raise ValueError("invalid movie state")
        now = utc_now()
        with self.transaction(immediate=True) as connection:
            previous = connection.execute(
                """
                SELECT * FROM user_movie_states
                WHERE account_id = ? AND movie_id = ?
                """,
                (account_id, movie_id),
            ).fetchone()
            if previous is not None and previous["state"] == state:
                return dict(previous), False
            connection.execute(
                """
                INSERT INTO user_movie_states
                    (account_id, movie_id, state, source, rating, note,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, NULL, NULL, ?, ?)
                ON CONFLICT(account_id, movie_id) DO UPDATE SET
                    state = excluded.state,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                (account_id, movie_id, state, source, now, now),
            )
            current = connection.execute(
                """
                SELECT * FROM user_movie_states
                WHERE account_id = ? AND movie_id = ?
                """,
                (account_id, movie_id),
            ).fetchone()
        return (dict(current) if current is not None else {}), True

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
            connection.execute(
                """UPDATE viewing_cognition_entries
                   SET raw_impression = '', synthesis = '', dimensions_json = '[]',
                       status = 'deleted', deleted_at = ?, updated_at = ?
                   WHERE account_id = ? AND movie_id = ? AND status != 'deleted'""",
                (now, now, account_id, movie_id),
            )
            connection.execute(
                """UPDATE content_drafts
                   SET content = '', source_material = '', status = 'deleted',
                       deleted_at = ?, updated_at = ?
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

    def movie_states_page(
        self, account_id: str, state: str | None, cursor: int = 0, limit: int = 12
    ) -> dict[str, Any]:
        safe_cursor = max(0, int(cursor))
        safe_limit = max(1, min(int(limit), 50))
        items = self.movie_states(account_id, state)
        page = items[safe_cursor : safe_cursor + safe_limit]
        next_cursor = safe_cursor + len(page)
        return {
            "items": page,
            "total": len(items),
            "cursor": safe_cursor,
            "next_cursor": next_cursor if next_cursor < len(items) else None,
        }

    def follow_up_watchlist(
        self, account_id: str, movie_id: str, action: str, reason_code: str | None = None
    ) -> dict[str, Any]:
        if action not in {"keep", "watched", "not_now", "not_interested"}:
            raise ValueError("想看跟进操作无效")
        current = self.get_movie_state(account_id, movie_id)
        if current is None or current["state"] != "watchlist":
            raise ValueError("这部电影不在当前想看列表")
        if action == "keep":
            item = current
        elif action == "watched":
            item = self.set_movie_state(account_id, movie_id, "watched", "watchlist_follow_up")
        elif action == "not_interested":
            item = self.set_movie_state(account_id, movie_id, "disliked", "watchlist_follow_up")
        else:
            with self.connect() as connection:
                connection.execute(
                    "DELETE FROM user_movie_states WHERE account_id = ? AND movie_id = ? AND state = 'watchlist'",
                    (account_id, movie_id),
                )
            item = {"movie_id": movie_id, "state": None}
        return {"action": action, "reason_code": reason_code, "item": item}

    def conversation_summary(self, account_id: str, movie_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT * FROM conversation_summaries
                   WHERE account_id = ? AND movie_id = ?
                     AND consent_status = 'active' AND deleted_at IS NULL""",
                (account_id, movie_id),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["topics"] = json.loads(result.pop("topics_json") or "[]")
        result["open_questions"] = json.loads(result.pop("open_questions_json") or "[]")
        result["spoilers_allowed"] = bool(result["spoilers_allowed"])
        return result

    def save_conversation_summary(
        self,
        account_id: str,
        movie_id: str,
        summary: str,
        topics: list[str],
        open_questions: list[str],
        spoilers_allowed: bool,
    ) -> dict[str, Any]:
        clean_summary = str(summary).strip()
        clean_topics = [str(item).strip()[:120] for item in topics if str(item).strip()][:12]
        clean_questions = [str(item).strip()[:240] for item in open_questions if str(item).strip()][:8]
        if not 1 <= len(clean_summary) <= 2000:
            raise ValueError("电影会话摘要必须在 1 到 2000 个字符之间")
        combined = "\n".join([clean_summary, *clean_topics, *clean_questions])
        prohibited_patterns = (
            r"\b1[3-9]\d{9}\b",
            r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}",
            r"(?:我的|我在|我被|我已)(?:学校|公司|单位|住址|地址|电话|手机号|微信|邮箱|病历|诊断|确诊|治疗)",
            r"(?:我想自杀|我不想活|我要伤害自己)",
        )
        if any(re.search(pattern, combined, flags=re.IGNORECASE) for pattern in prohibited_patterns):
            raise ValueError("电影会话摘要只能保留电影讨论内容，请移除联系方式或现实敏感经历")
        if self.movie(movie_id) is None:
            raise ValueError("电影不存在")
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO conversation_summaries (
                       id, account_id, movie_id, summary, topics_json,
                       open_questions_json, spoilers_allowed, consent_status,
                       created_at, updated_at, deleted_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, NULL)
                   ON CONFLICT(account_id, movie_id) DO UPDATE SET
                       summary = excluded.summary,
                       topics_json = excluded.topics_json,
                       open_questions_json = excluded.open_questions_json,
                       spoilers_allowed = excluded.spoilers_allowed,
                       consent_status = 'active', updated_at = excluded.updated_at,
                       deleted_at = NULL""",
                (
                    "conv_" + secrets.token_hex(8), account_id, movie_id, clean_summary,
                    json.dumps(clean_topics, ensure_ascii=False),
                    json.dumps(clean_questions, ensure_ascii=False),
                    int(bool(spoilers_allowed)), now, now,
                ),
            )
        return self.conversation_summary(account_id, movie_id) or {}

    def delete_conversation_summary(self, account_id: str, movie_id: str) -> bool:
        now = utc_now()
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE conversation_summaries
                   SET summary = '', topics_json = '[]', open_questions_json = '[]',
                       consent_status = 'revoked', deleted_at = ?, updated_at = ?
                   WHERE account_id = ? AND movie_id = ? AND deleted_at IS NULL""",
                (now, now, account_id, movie_id),
            )
        return result.rowcount > 0

    @staticmethod
    def current_week_key() -> str:
        iso = datetime.now(UTC).date().isocalendar()
        return f"{iso.year}-W{iso.week:02d}"

    def weekly_recommendation(
        self, account_id: str, week_key: str | None = None, mark_viewed: bool = False
    ) -> dict[str, Any] | None:
        key = week_key or self.current_week_key()
        now = utc_now()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM weekly_recommendations WHERE account_id = ? AND week_key = ?",
                (account_id, key),
            ).fetchone()
            if row and mark_viewed and row["status"] == "ready":
                connection.execute(
                    "UPDATE weekly_recommendations SET status = 'viewed', viewed_at = ? WHERE id = ?",
                    (now, row["id"]),
                )
                row = connection.execute(
                    "SELECT * FROM weekly_recommendations WHERE id = ?", (row["id"],)
                ).fetchone()
        if row is None:
            return None
        result = dict(row)
        movie_ids = json.loads(result.pop("movie_ids_json") or "[]")
        result["movies"] = [movie for movie_id in movie_ids if (movie := self.movie(str(movie_id)))]
        result["source_inputs"] = json.loads(result.pop("source_inputs_json") or "{}")
        return result

    def save_weekly_recommendation(
        self,
        account_id: str,
        movies: list[dict[str, Any]],
        reason_summary: str,
        source_inputs: dict[str, Any],
        *,
        replace: bool = False,
    ) -> dict[str, Any]:
        key = self.current_week_key()
        now_dt = datetime.now(UTC)
        expires = (now_dt + timedelta(days=8)).isoformat()
        movie_ids = [str(movie["id"]) for movie in movies[:3]]
        if any(movie_id in self.watched_ids(account_id) for movie_id in movie_ids):
            raise ValueError("本周建议不能包含已看电影")
        existing = self.weekly_recommendation(account_id, key)
        if existing and not replace:
            return existing
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO weekly_recommendations (
                       id, account_id, week_key, movie_ids_json, reason_summary,
                       source_inputs_json, status, created_at, viewed_at, expires_at
                   ) VALUES (?, ?, ?, ?, ?, ?, 'ready', ?, NULL, ?)
                   ON CONFLICT(account_id, week_key) DO UPDATE SET
                       movie_ids_json = excluded.movie_ids_json,
                       reason_summary = excluded.reason_summary,
                       source_inputs_json = excluded.source_inputs_json,
                       status = 'ready', viewed_at = NULL, expires_at = excluded.expires_at""",
                (
                    "week_" + secrets.token_hex(8), account_id, key,
                    json.dumps(movie_ids, ensure_ascii=False), str(reason_summary)[:500],
                    json.dumps(source_inputs, ensure_ascii=False), now_dt.isoformat(), expires,
                ),
            )
        return self.weekly_recommendation(account_id, key) or {}

    def dismiss_weekly_recommendation(self, account_id: str, *, permanently: bool = False) -> None:
        key = self.current_week_key()
        with self.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE weekly_recommendations SET status = 'dismissed' WHERE account_id = ? AND week_key = ?",
                (account_id, key),
            )
            if permanently:
                self._ensure_profile(connection, account_id)
                connection.execute(
                    "UPDATE account_profiles SET weekly_recommendations_enabled = 0, updated_at = ? WHERE account_id = ?",
                    (utc_now(), account_id),
                )

    def generate_monthly_recap(self, account_id: str, month_key: str) -> dict[str, Any]:
        try:
            datetime.strptime(month_key, "%Y-%m")
        except ValueError as error:
            raise ValueError("月份必须使用 YYYY-MM") from error
        prefix = month_key + "%"
        with self.connect() as connection:
            state_rows = connection.execute(
                """SELECT ums.movie_id, ums.state, m.title_zh, m.themes_json
                   FROM user_movie_states ums JOIN movies m ON m.id = ums.movie_id
                   WHERE ums.account_id = ? AND ums.updated_at LIKE ?
                   ORDER BY ums.updated_at, ums.movie_id""",
                (account_id, prefix),
            ).fetchall()
            reflection_rows = connection.execute(
                """SELECT mr.id, mr.movie_id, mr.version, mr.content, m.title_zh, m.themes_json
                   FROM movie_reflections mr JOIN movies m ON m.id = mr.movie_id
                   WHERE mr.account_id = ? AND mr.status IN ('confirmed', 'locked')
                     AND COALESCE(mr.confirmed_at, mr.updated_at) LIKE ?
                   ORDER BY mr.updated_at, mr.id""",
                (account_id, prefix),
            ).fetchall()
        movies = [dict(row) for row in state_rows]
        reflections = [dict(row) for row in reflection_rows]
        theme_sources: dict[str, list[str]] = {}
        for row in reflections:
            for theme in json.loads(row.pop("themes_json") or "[]"):
                theme_sources.setdefault(str(theme), []).append(str(row["id"]))
        themes = [
            {"label": theme, "source_reflection_ids": ids}
            for theme, ids in sorted(theme_sources.items(), key=lambda item: (-len(item[1]), item[0]))
            if len(ids) >= 1
        ][:5]
        content = {
            "month": month_key,
            "watched": [
                {"movie_id": row["movie_id"], "title": row["title_zh"]}
                for row in movies if row["state"] == "watched"
            ],
            "watchlist": [
                {"movie_id": row["movie_id"], "title": row["title_zh"]}
                for row in movies if row["state"] == "watchlist"
            ],
            "confirmed_reflections": [
                {"reflection_id": row["id"], "movie_id": row["movie_id"], "title": row["title_zh"]}
                for row in reflections
            ],
            "themes": themes,
            "next_direction": "",
        }
        movie_ids = list(dict.fromkeys(str(row["movie_id"]) for row in movies))
        reflection_ids = [str(row["id"]) for row in reflections]
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO monthly_recaps (
                       id, account_id, month_key, content_json, source_movie_ids_json,
                       source_reflection_ids_json, status, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, 'draft', ?, ?)
                   ON CONFLICT(account_id, month_key) DO UPDATE SET
                       content_json = excluded.content_json,
                       source_movie_ids_json = excluded.source_movie_ids_json,
                       source_reflection_ids_json = excluded.source_reflection_ids_json,
                       status = 'draft', updated_at = excluded.updated_at""",
                (
                    "recap_" + secrets.token_hex(8), account_id, month_key,
                    json.dumps(content, ensure_ascii=False), json.dumps(movie_ids),
                    json.dumps(reflection_ids), now, now,
                ),
            )
        return self.monthly_recap(account_id, month_key) or {}

    def monthly_recap(self, account_id: str, month_key: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM monthly_recaps WHERE account_id = ? AND month_key = ? AND status != 'deleted'",
                (account_id, month_key),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["content"] = json.loads(result.pop("content_json") or "{}")
        result["source_movie_ids"] = json.loads(result.pop("source_movie_ids_json") or "[]")
        result["source_reflection_ids"] = json.loads(result.pop("source_reflection_ids_json") or "[]")
        return result

    def confirm_monthly_recap(
        self, account_id: str, month_key: str, next_direction: str = ""
    ) -> dict[str, Any]:
        recap = self.monthly_recap(account_id, month_key)
        if recap is None:
            raise ValueError("请先生成月度回顾草稿")
        content = recap["content"]
        content["next_direction"] = str(next_direction).strip()[:300]
        with self.connect() as connection:
            connection.execute(
                """UPDATE monthly_recaps SET content_json = ?, status = 'confirmed', updated_at = ?
                   WHERE id = ? AND account_id = ?""",
                (json.dumps(content, ensure_ascii=False), utc_now(), recap["id"], account_id),
            )
        return self.monthly_recap(account_id, month_key) or {}

    def create_share_card(
        self,
        account_id: str,
        source_type: str,
        source_id: str,
        content: dict[str, Any],
        expires_days: int = 7,
    ) -> dict[str, Any]:
        if source_type not in {"reflection", "monthly_recap", "taste_dimension"}:
            raise ValueError("分享内容类型无效")
        days = max(1, min(int(expires_days), 30))
        now = datetime.now(UTC)
        card_id = "share_" + secrets.token_hex(8)
        token = secrets.token_urlsafe(24)
        safe_content = json.loads(json.dumps(content, ensure_ascii=False))
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO share_cards (
                       id, account_id, public_token, source_type, source_id,
                       content_json, status, created_at, expires_at, revoked_at
                   ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, NULL)""",
                (
                    card_id, account_id, token, source_type, source_id,
                    json.dumps(safe_content, ensure_ascii=False), now.isoformat(),
                    (now + timedelta(days=days)).isoformat(),
                ),
            )
        return self.share_card_for_account(account_id, card_id) or {}

    def share_card_for_account(self, account_id: str, card_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM share_cards WHERE id = ? AND account_id = ?",
                (card_id, account_id),
            ).fetchone()
        return self._decode_share_card(row)

    def public_share_card(self, token: str) -> dict[str, Any] | None:
        now = utc_now()
        with self.connect() as connection:
            row = connection.execute(
                """SELECT * FROM share_cards
                   WHERE public_token = ? AND status = 'active'
                     AND revoked_at IS NULL AND expires_at > ?""",
                (token, now),
            ).fetchone()
        result = self._decode_share_card(row)
        if result:
            result.pop("account_id", None)
            result.pop("public_token", None)
        return result

    @staticmethod
    def _decode_share_card(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["content"] = json.loads(result.pop("content_json") or "{}")
        return result

    def revoke_share_card(self, account_id: str, card_id: str) -> bool:
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE share_cards SET status = 'revoked', revoked_at = ?
                   WHERE id = ? AND account_id = ? AND status = 'active'""",
                (utc_now(), card_id, account_id),
            )
        return result.rowcount > 0

    def create_background_job(
        self,
        account_id: str,
        job_type: str,
        input_reference: dict[str, Any],
        *,
        max_attempts: int = 2,
        expires_minutes: int = 30,
    ) -> dict[str, Any]:
        now = datetime.now(UTC)
        job_id = "job_" + secrets.token_hex(8)
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO background_jobs (
                       id, account_id, job_type, status, input_reference_json,
                       result_json, attempt_count, max_attempts, available_at,
                       error_category, created_at, started_at, finished_at, expires_at
                   ) VALUES (?, ?, ?, 'queued', ?, '{}', 0, ?, ?, NULL, ?, NULL, NULL, ?)""",
                (
                    job_id, account_id, str(job_type)[:80],
                    json.dumps(input_reference, ensure_ascii=False), max(1, min(max_attempts, 5)),
                    now.isoformat(), now.isoformat(),
                    (now + timedelta(minutes=max(1, expires_minutes))).isoformat(),
                ),
            )
        return self.background_job(account_id, job_id) or {}

    def background_job(self, account_id: str, job_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM background_jobs WHERE id = ? AND account_id = ?",
                (job_id, account_id),
            ).fetchone()
            if (
                row is not None
                and row["status"] in {"queued", "running"}
                and str(row["expires_at"]) <= utc_now()
            ):
                connection.execute(
                    """UPDATE background_jobs
                       SET status = 'expired', finished_at = ?, error_category = 'Expired'
                       WHERE id = ? AND account_id = ? AND status IN ('queued', 'running')""",
                    (utc_now(), job_id, account_id),
                )
                row = connection.execute(
                    "SELECT * FROM background_jobs WHERE id = ? AND account_id = ?",
                    (job_id, account_id),
                ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["input_reference"] = json.loads(result.pop("input_reference_json") or "{}")
        result["result"] = json.loads(result.pop("result_json") or "{}")
        return result

    def update_background_job(
        self,
        account_id: str,
        job_id: str,
        status: str,
        *,
        result: dict[str, Any] | None = None,
        error_category: str | None = None,
    ) -> dict[str, Any]:
        if status not in {"running", "succeeded", "failed", "expired"}:
            raise ValueError("后台任务状态无效")
        now = utc_now()
        started_at = now if status == "running" else None
        finished_at = now if status in {"succeeded", "failed", "expired"} else None
        with self.connect() as connection:
            update = connection.execute(
                """UPDATE background_jobs
                   SET status = ?, result_json = ?, error_category = ?,
                       attempt_count = attempt_count + CASE WHEN ? = 'running' THEN 1 ELSE 0 END,
                       started_at = COALESCE(started_at, ?),
                       finished_at = CASE WHEN ? IS NULL THEN finished_at ELSE ? END
                   WHERE id = ? AND account_id = ?""",
                (
                    status, json.dumps(result or {}, ensure_ascii=False), error_category,
                    status, started_at, finished_at, finished_at, job_id, account_id,
                ),
            )
        if update.rowcount == 0:
            raise ValueError("后台任务不存在")
        return self.background_job(account_id, job_id) or {}

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

    def has_poster_url(self, poster_url: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM movies WHERE poster_url = ? LIMIT 1", (poster_url,)
            ).fetchone()
        return row is not None

    def public_share_uses_poster_url(self, token: str, poster_url: str) -> bool:
        """Return whether this active public share explicitly contains the poster."""
        with self.connect() as connection:
            row = connection.execute(
                """SELECT content_json FROM share_cards
                   WHERE public_token = ? AND status = 'active'
                     AND revoked_at IS NULL AND expires_at > ?""",
                (token, utc_now()),
            ).fetchone()
        if row is None:
            return False
        try:
            content = json.loads(str(row["content_json"] or "{}"))
        except (TypeError, json.JSONDecodeError):
            return False
        visual = content.get("visual") if isinstance(content, dict) else None
        movies = visual.get("movies") if isinstance(visual, dict) else None
        return isinstance(movies, list) and any(
            isinstance(movie, dict) and movie.get("poster_url") == poster_url
            for movie in movies
        )

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

    def recent_product_event_count(
        self,
        account_id: str,
        event_name: str,
        *,
        since: datetime,
    ) -> int:
        """Count one account's named events after a UTC boundary.

        The query deliberately uses the privacy-safe product event stream rather
        than adding a second, feature-specific rate-limit store.
        """
        boundary = since.astimezone(UTC).isoformat()
        with self.connect() as connection:
            return int(
                connection.execute(
                    """SELECT COUNT(*) FROM product_events
                       WHERE account_id = ? AND event_name = ? AND created_at >= ?""",
                    (account_id, str(event_name)[:100], boundary),
                ).fetchone()[0]
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
            conversation_rows = connection.execute(
                """SELECT CASE WHEN deleted_at IS NULL THEN 'active' ELSE 'deleted' END AS status,
                          COUNT(*) AS count
                   FROM conversation_summaries GROUP BY status"""
            ).fetchall()
            weekly_rows = connection.execute(
                """SELECT status, COUNT(*) AS count FROM weekly_recommendations
                   GROUP BY status ORDER BY status"""
            ).fetchall()
            recap_rows = connection.execute(
                """SELECT status, COUNT(*) AS count FROM monthly_recaps
                   GROUP BY status ORDER BY status"""
            ).fetchall()
            job_rows = connection.execute(
                """SELECT status, COUNT(*) AS count FROM background_jobs
                   GROUP BY status ORDER BY status"""
            ).fetchall()
            share_rows = connection.execute(
                """SELECT status, COUNT(*) AS count FROM share_cards
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
            "continuity": {
                "conversation_summaries": {
                    str(row["status"]): int(row["count"]) for row in conversation_rows
                },
                "weekly_recommendations": {
                    str(row["status"]): int(row["count"]) for row in weekly_rows
                },
                "monthly_recaps": {
                    str(row["status"]): int(row["count"]) for row in recap_rows
                },
                "background_jobs": {
                    str(row["status"]): int(row["count"]) for row in job_rows
                },
                "share_cards": {
                    str(row["status"]): int(row["count"]) for row in share_rows
                },
            },
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
