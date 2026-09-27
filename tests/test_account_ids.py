from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient

from app import build_context
from catalog import MovieCatalog
from fastapi_app import create_fastapi_app
from settings import Settings
from storage import CredentialError, Store


PRODUCT_DIR = Path(__file__).resolve().parents[1]


class AccountIdTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "accounts.db"
        self.store = self.reopen()

    def reopen(self) -> Store:
        return Store(self.path, "test-pepper", "test-secret")

    def seed_legacy(self) -> dict[str, list[dict]]:
        """Populate every account FK so lost/cross-linked data cannot pass unnoticed."""
        MovieCatalog(self.store, PRODUCT_DIR / "data/movies.json").load_seed()
        per_table = {
            "account_credentials": {
                "username": "legacy_user", "password_salt": b"test-salt",
                "password_hash": hashlib.pbkdf2_hmac("sha256", b"test-password", b"test-salt", 1),
                "password_iterations": 1,
            },
            "invite_codes": {"code_digest": self.store._invite_digest("test-legacy-code"),
                             "code_hint": "CODE", "status": "active"},
            "sessions": {"token_digest": self.store._session_digest("test-session")},
            "user_movie_states": {"state": "watched", "source": "manual"},
            "recommendation_impressions": {"request_summary": "轻松电影"},
            "account_profiles": {},
            "user_movie_feedback": {"context": "manual", "sentiment": "positive"},
            "recommendation_feedback": {"impression_id": "fixture", "action": "discuss"},
            "movie_reflections": {"version": 1, "content": "保留电影感受", "source": "user", "status": "confirmed"},
            "conversation_summaries": {"summary": "保留电影记忆"},
            "conversation_records": {"messages_json": '[{"role":"user","content":"usr_first 原文不能替换"}]'},
            "chat_records": {"mode": "discussion", "messages_json": '[{"role":"user","content":"保留聊天"}]'},
            "weekly_recommendations": {"week_key": "2026-W01"},
            "monthly_recaps": {"month_key": "2026-01"},
            "background_jobs": {"job_type": "reflection", "status": "queued", "available_at": "2026-01-01"},
            "share_cards": {"public_token": "test-public-share", "source_type": "reflection", "source_id": "fixture"},
            "product_events": {"event_name": "login_succeeded"},
            "model_usage_events": {"operation": "chat", "provider": "test", "service_id": "test", "success": 1, "latency_ms": 1},
            "safety_events": {"category": "test"},
            "viewing_cognition_entries": {"viewing_round": 1, "stage": "first_impression", "raw_impression": "原始感受", "synthesis": "整理"},
            "content_drafts": {"content_scene": "formal_review", "version": 1, "content": "保留草稿"},
            "user_feedback": {"kind": "problem", "content": "保留反馈", "source_page": "home"},
        }
        with self.store.transaction(immediate=True) as connection:
            # Insertion order deliberately differs from registration time.
            connection.executemany(
                "INSERT INTO accounts (id, status, created_at) VALUES (?, ?, ?)",
                [("usr_second", "suspended", "2026-02-01"),
                 ("usr_first", "active", "2026-01-01"),
                 ("usr_third", "deleted", "2026-02-01")],
            )
            movie_id = connection.execute("SELECT id FROM movies LIMIT 1").fetchone()[0]
            actual_tables = {
                row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
                if any(fk["table"] == "accounts" for fk in connection.execute(f'PRAGMA foreign_key_list("{row["name"]}")'))
            }
            self.assertEqual(actual_tables, set(per_table))
            for table, fields in per_table.items():
                columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
                values = {key: value for key, value in {
                    "id": "fixture", "account_id": "usr_first", "movie_id": movie_id,
                    "created_at": "2026-01-01", "updated_at": "2026-01-01",
                    "expires_at": "2099-01-01", **fields,
                }.items() if key in columns}
                connection.execute(
                    f"INSERT INTO {table} ({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
                    tuple(values.values()),
                )
            return {table: [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
                    for table in per_table}

    def test_legacy_migration_preserves_every_account_relation_and_sessions(self) -> None:
        before = self.seed_legacy()
        migrated = self.reopen()
        with migrated.connect() as connection:
            self.assertEqual(
                [tuple(row) for row in connection.execute("SELECT id, status FROM accounts ORDER BY CAST(id AS INTEGER)")],
                [("1", "active"), ("2", "suspended"), ("3", "deleted")],
            )
            self.assertEqual(
                [tuple(r) for r in connection.execute("SELECT id, legacy_id FROM account_id_allocations ORDER BY id")],
                [(1, "usr_first"), (2, "usr_second"), (3, "usr_third")],
            )
            for table, rows in before.items():
                self.assertEqual([dict(row) for row in connection.execute(f"SELECT * FROM {table}")],
                                 [{**row, "account_id": "1"} for row in rows], table)
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(migrated.account_for_session("test-session"), "1")
        self.assertEqual(migrated.account_storage_hint("1"), "_first")
        self.assertEqual(migrated.login_with_password("legacy_user", "test-password", 30)[0], "1")
        self.assertEqual(migrated.admin_feedback_page()["items"][0]["account_id"], "1")
        reopened = self.reopen()
        self.assertEqual(reopened.account_for_session("test-session"), "1")
        self.assertEqual(reopened.register_with_password("next_user", "test-password", 30)[0], "4")
        settings = Settings(database_path=self.path, invite_pepper="test-pepper", session_secret="test-secret",
                            model_api_key="", local_open_access=False, cookie_secure=False)
        with TestClient(create_fastapi_app(build_context(settings))) as client:
            self.assertEqual(client.post("/api/auth/login", json={
                "username": "legacy_user", "password": "test-password",
            }).status_code, 200)
            account = client.get("/api/me").json()["account"]
            self.assertEqual(account["id_hint"], "1")
            self.assertEqual(account["storage_hint"], "_first")

    def test_new_registration_invites_and_password_binding_share_one_sequence(self) -> None:
        self.assertEqual(self.store.register_with_password("first_user", "test-password", 30)[0], "1")
        first, second = self.store.generate_invites(2)
        self.assertEqual(self.store.login_with_invite(first, 30)[0], "2")
        self.assertEqual(self.store.login_with_invite(first, 30)[0], "2")
        self.assertEqual(self.store.register_with_password("bound_user", "test-password", 30, first)[0], "2")
        self.assertEqual(self.store.register_with_password("third_user", "test-password", 30, second)[0], "3")
        with self.assertRaises(CredentialError):
            self.store.register_with_password("first_user", "test-password", 30)
        self.assertEqual(self.reopen().register_with_password("fourth_user", "test-password", 30)[0], "4")

    def test_concurrent_account_creation_has_unique_sequential_ids(self) -> None:
        stores = [self.reopen() for _ in range(4)]
        codes = self.store.generate_invites(16)
        def create(index: int) -> str:
            return stores[index % 4].login_with_invite(codes[index], 30)[0]
        with ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(create, range(16)))
        self.assertEqual(sorted(map(int, ids)), list(range(1, 17)))

    def test_committed_ids_are_not_reused_after_deletion_or_restart(self) -> None:
        self.assertEqual(self.store.ensure_local_account(), "1")
        with self.store.connect() as connection:
            connection.execute("DELETE FROM accounts WHERE id = '1'")
        self.assertEqual(self.reopen().ensure_local_account(), "2")
        self.assertEqual(self.reopen().ensure_local_account(), "2")
        self.assertEqual(self.reopen().account_storage_hint("2"), "account-2")

    def test_legacy_local_owner_keeps_identity_in_multi_account_database(self) -> None:
        with self.store.connect() as connection:
            connection.executemany("INSERT INTO accounts (id, status, created_at) VALUES (?, 'active', ?)",
                                   [("usr_other", "2026-01-01"), ("usr_local_owner", "2026-01-02")])
        reopened = self.reopen()
        self.assertEqual(reopened.ensure_local_account(), "2")
        self.assertEqual(reopened.account_storage_hint("2"), "_owner")
        self.assertEqual(self.reopen().ensure_local_account(), "2")
        with reopened.connect() as connection:
            connection.execute("UPDATE accounts SET status = 'suspended' WHERE id = '2'")
        with self.assertRaisesRegex(RuntimeError, "不可用"):
            reopened.ensure_local_account()

    def test_existing_numeric_ids_survive_mixed_database_migration(self) -> None:
        with self.store.connect() as connection:
            connection.executemany("INSERT INTO accounts (id, status, created_at) VALUES (?, 'active', ?)",
                                   [("9", "2026-01-02"), ("usr_old", "2026-01-01")])
        reopened = self.reopen()
        self.assertEqual({a["id"] for a in reopened.admin_accounts_page()["items"]}, {"9", "10"})
        self.assertEqual(reopened.login_with_invite(reopened.generate_invites(1)[0], 30)[0], "11")

    def test_failed_migration_rolls_back_all_references_and_allocations(self) -> None:
        before = self.seed_legacy()
        with self.store.connect() as connection:
            connection.execute("CREATE TRIGGER fail_migration BEFORE UPDATE OF id ON accounts "
                               "WHEN NEW.id = '2' BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "test failure"):
            self.reopen()
        with self.store.connect() as connection:
            for table, rows in before.items():
                self.assertEqual([dict(row) for row in connection.execute(f"SELECT * FROM {table}")], rows)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM account_id_allocations").fetchone()[0], 0)
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            connection.execute("DROP TRIGGER fail_migration")
        self.assertEqual(self.reopen().account_for_session("test-session"), "1")

    def test_api_reports_full_numeric_ids_and_keeps_account_isolation(self) -> None:
        settings = Settings(database_path=self.path, invite_pepper="test-pepper", session_secret="test-secret",
                            model_api_key="", local_open_access=False, admin_token="test-admin", cookie_secure=False)
        context = build_context(settings)
        # The account hint must not truncate a numeric ID after six digits.
        with context.store.connect() as connection:
            connection.execute("INSERT INTO account_id_allocations (id) VALUES (999999)")
        with TestClient(create_fastapi_app(context)) as first, TestClient(create_fastapi_app(context)) as second:
            for client, username, expected in ((first, "first_user", "1000000"), (second, "second_user", "1000001")):
                response = client.post("/api/auth/register", json={"username": username, "password": "test-password"})
                self.assertEqual(response.status_code, 201, response.text)
                self.assertEqual(response.json()["account"]["id_hint"], expected)
                self.assertEqual(client.get("/api/me").json()["account"]["id_hint"], expected)
                self.assertEqual(client.get("/api/me").json()["account"]["storage_hint"], f"account-{expected}")
            conversation = context.store.append_chat_turn("1000000", "test_chat_123", "discussion", "私有讨论", "回复")
            self.assertIsNone(context.store.admin_chat_detail("1000001", conversation))
            self.assertEqual(first.get("/api/admin/accounts").status_code, 401)
            first.cookies.clear()
            login = first.post("/api/auth/login", json={"username": "first_user", "password": "test-password"})
            self.assertEqual(login.json()["account"]["id_hint"], "1000000")


if __name__ == "__main__":
    unittest.main()
