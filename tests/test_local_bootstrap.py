from __future__ import annotations

import stat
import tempfile
import unittest
from pathlib import Path

from local_bootstrap import ensure_local_environment, has_usable_invite, parse_env


PRODUCT_DIR = Path(__file__).resolve().parents[1]


EXAMPLE_ENV = """# Local test configuration
YINGBAN_HOST=127.0.0.1
YINGBAN_INVITE_PEPPER=replace-with-a-long-random-secret
YINGBAN_SESSION_SECRET=replace-with-another-long-random-secret
YINGBAN_ADMIN_TOKEN=replace-with-a-private-admin-token
MODEL_API_KEY=
"""


class LocalBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temporary.name)
        self.env_path = self.base_dir / ".env"
        self.example_path = self.base_dir / ".env.example"
        self.example_path.write_text(EXAMPLE_ENV, encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def bootstrap(self):
        return ensure_local_environment(
            env_path=self.env_path,
            env_example_path=self.example_path,
            base_dir=self.base_dir,
        )

    def test_first_run_creates_private_non_placeholder_credentials(self) -> None:
        result = self.bootstrap()
        values = parse_env(self.env_path.read_text(encoding="utf-8"))

        self.assertTrue(result.created)
        self.assertEqual(
            set(result.changed_keys),
            {
                "YINGBAN_INVITE_PEPPER",
                "YINGBAN_SESSION_SECRET",
                "YINGBAN_ADMIN_TOKEN",
            },
        )
        self.assertTrue(values["YINGBAN_ADMIN_TOKEN"].startswith("YB-ADMIN-"))
        self.assertNotIn("replace-with", "\n".join(values.values()))
        self.assertEqual(stat.S_IMODE(self.env_path.stat().st_mode), 0o600)
        self.assertEqual(values["YINGBAN_HOST"], "127.0.0.1")

    def test_second_run_is_idempotent_and_preserves_credentials(self) -> None:
        first = self.bootstrap()
        first_text = self.env_path.read_text(encoding="utf-8")
        second = self.bootstrap()

        self.assertFalse(second.created)
        self.assertEqual(second.changed_keys, ())
        self.assertEqual(first_text, self.env_path.read_text(encoding="utf-8"))
        self.assertEqual(first.values, second.values)

    def test_existing_database_blocks_automatic_secret_replacement(self) -> None:
        database_path = self.base_dir / "data" / "yingban.db"
        database_path.parent.mkdir(parents=True)
        database_path.write_bytes(b"existing database")

        with self.assertRaisesRegex(RuntimeError, "已有影伴数据库"):
            self.bootstrap()
        self.assertFalse(self.env_path.exists())

    def test_issued_or_active_invite_prevents_duplicate_bootstrap_invite(self) -> None:
        self.assertTrue(has_usable_invite([{"status": "issued"}]))
        self.assertTrue(has_usable_invite([{"status": "active"}]))
        self.assertFalse(has_usable_invite([]))
        self.assertFalse(has_usable_invite([{"status": "revoked"}]))

    def test_launcher_and_pages_explain_first_run_credentials(self) -> None:
        user_html = (PRODUCT_DIR / "web" / "index.html").read_text(encoding="utf-8")
        admin_html = (PRODUCT_DIR / "web" / "admin.html").read_text(encoding="utf-8")
        launcher = (PRODUCT_DIR / "启动影伴.command").read_text(encoding="utf-8")

        self.assertIn("邀请码会显示在启动窗口并复制到剪贴板", user_html)
        self.assertIn("首次本机启动会在启动窗口显示口令", admin_html)
        self.assertIn("python local_bootstrap.py", launcher)


if __name__ == "__main__":
    unittest.main()
