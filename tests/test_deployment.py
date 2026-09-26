"""Production bundle and public publishing boundaries; no live deployment."""
from pathlib import Path
from unittest.mock import patch
import io
import tarfile
import tempfile
import unittest

from scripts import deploy_receive as receiver
from scripts.publish_github import public_path


def bundle(extra=None):
    entries = {name: b"pass\n" for name in receiver.REQUIRED}
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, data in entries.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))
        if extra is not None:
            archive.addfile(extra, io.BytesIO(b""))
    return output.getvalue()


class DeploymentTests(unittest.TestCase):
    def test_unpack_accepts_required_runtime_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary)
            receiver.unpack(bundle(), destination)
            self.assertEqual((destination / "app.py").read_text(), "pass\n")

    def test_unpack_rejects_traversal_secrets_and_links_before_writing(self):
        entries = [tarfile.TarInfo(name) for name in ("../escape", "/tmp/escape", ".env", "data/yingban.db")]
        link = tarfile.TarInfo("web/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/yingban/yingban.env"
        entries.append(link)
        for entry in entries:
            with self.subTest(name=entry.name), tempfile.TemporaryDirectory() as temporary:
                with self.assertRaises(ValueError):
                    receiver.unpack(bundle(entry), Path(temporary))
                self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_unpack_rejects_duplicate_entries(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ValueError):
                receiver.unpack(bundle(tarfile.TarInfo("app.py")), Path(temporary))

    def test_public_manifest_excludes_private_and_generated_files(self):
        for name in (".env", "AGENTS.md", "第八十阶段技术开发文档.md", "影伴产品设计PRD.md",
                     "data/yingban.db", "data/yingban.db-wal", "evals/report.json",
                     "frontend/.env.local", "frontend/node_modules/foo.js", "frontend/out/index.html",
                     "docs/内部设计.md", "../../secret", "scripts/private-helper.py"):
            with self.subTest(name=name):
                self.assertFalse(public_path(name))
        for name in ("app.py", ".env.example", ".github/workflows/deploy.yml",
                     "frontend/app/(product)/page.tsx", "docs/images/yingban-home.png"):
            self.assertTrue(public_path(name))

    def test_dependency_change_stops_before_backup_or_switch(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            old = base / "releases/old"
            old.mkdir(parents=True)
            (old / "requirements.txt").write_text("old dependencies")
            (base / "current").symlink_to(old)
            with patch.object(receiver, "BASE", base), patch.object(receiver, "health", return_value={"mode": "model"}), patch.object(receiver.subprocess, "run") as run:
                with self.assertRaisesRegex(RuntimeError, "requirements.txt changed"):
                    receiver.deploy("a" * 40, bundle())
                run.assert_not_called()
            self.assertEqual((base / "current").resolve(), old.resolve())

    def test_failed_health_restores_previous_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            old = base / "releases/old"
            old.mkdir(parents=True)
            (old / "requirements.txt").write_text("pass\n")
            (base / "current").symlink_to(old)
            with patch.object(receiver, "BASE", base), patch.object(receiver, "health", return_value={"mode": "model"}), patch.object(receiver.subprocess, "run"), patch.object(receiver, "wait_healthy", side_effect=[RuntimeError("unhealthy"), None]):
                with self.assertRaisesRegex(RuntimeError, "unhealthy"):
                    receiver.deploy("b" * 40, bundle())
            self.assertEqual((base / "current").resolve(), old.resolve())


if __name__ == "__main__":
    unittest.main()
