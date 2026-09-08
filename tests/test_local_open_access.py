from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app import build_context
from fastapi_app import create_fastapi_app
from settings import Settings, is_loopback_host
from storage import Store


PRODUCT_DIR = Path(__file__).resolve().parents[1]


def local_settings(root: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "base_dir": PRODUCT_DIR,
        "host": "127.0.0.1",
        "port": 8765,
        "database_path": root / "local-open.db",
        "movie_seed_path": PRODUCT_DIR / "data" / "movies.json",
        "invite_pepper": "test-local-invite-pepper",
        "session_secret": "test-local-session-secret",
        "admin_token": "",
        "cookie_secure": False,
        "local_open_access": True,
        "model_api_key": "",
    }
    values.update(overrides)
    return Settings(**values)


class LocalOpenAccessTests(unittest.TestCase):
    def test_loopback_host_detection_rejects_public_and_ambiguous_hosts(self) -> None:
        self.assertTrue(is_loopback_host("127.0.0.1"))
        self.assertTrue(is_loopback_host("::1"))
        self.assertTrue(is_loopback_host("localhost."))
        self.assertFalse(is_loopback_host("0.0.0.0"))
        self.assertFalse(is_loopback_host("yingban.example"))

    def test_build_rejects_open_access_on_non_loopback_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = local_settings(Path(directory), host="0.0.0.0")
            with self.assertRaisesRegex(ValueError, "只能与 localhost 或回环 IP"):
                build_context(settings)

    def test_local_account_is_stable_and_reuses_one_existing_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "new.db", "pepper", "secret")
            self.assertEqual(store.ensure_local_account(), "usr_local_owner")
            self.assertEqual(store.ensure_local_account(), "usr_local_owner")

            existing = Store(root / "existing.db", "pepper", "secret")
            invite = existing.generate_invites(1)[0]
            account_id, _token = existing.login_with_invite(invite, 30)
            self.assertEqual(existing.ensure_local_account(), account_id)

    def test_loopback_browser_enters_user_and_admin_without_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = build_context(local_settings(Path(directory)))
            with TestClient(
                create_fastapi_app(context),
                base_url="http://127.0.0.1:8765",
                client=("127.0.0.1", 50000),
            ) as client:
                health = client.get("/api/health")
                self.assertEqual(health.status_code, 200)
                self.assertTrue(health.json()["local_open_access"])

                me = client.get("/api/me")
                self.assertEqual(me.status_code, 200)
                self.assertTrue(me.json()["authenticated"])
                self.assertTrue(me.json()["local_open_access"])

                admin = client.get("/api/admin/agent-config")
                self.assertEqual(admin.status_code, 200)
                self.assertTrue(admin.json()["access"]["local_open_access"])

    def test_remote_client_host_and_origin_cannot_use_open_access(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = build_context(local_settings(Path(directory)))
            application = create_fastapi_app(context)

            with TestClient(
                application,
                base_url="http://127.0.0.1:8765",
                client=("203.0.113.9", 50000),
            ) as remote_client:
                self.assertFalse(remote_client.get("/api/me").json()["authenticated"])
                self.assertEqual(
                    remote_client.get("/api/admin/agent-config").status_code,
                    401,
                )

            with TestClient(
                application,
                base_url="http://127.0.0.1:8765",
                client=("127.0.0.1", 50000),
            ) as local_client:
                bad_origin = local_client.get(
                    "/api/me", headers={"Origin": "https://attacker.example"}
                )
                self.assertFalse(bad_origin.json()["authenticated"])

                bad_host = local_client.get(
                    "/api/admin/agent-config", headers={"Host": "attacker.example"}
                )
                self.assertEqual(bad_host.status_code, 401)


if __name__ == "__main__":
    unittest.main()
