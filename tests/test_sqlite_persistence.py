from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

PRODUCT_DIR = Path(__file__).resolve().parents[1]
if str(PRODUCT_DIR) not in sys.path:
    sys.path.insert(0, str(PRODUCT_DIR))
from scripts.sqlite_maintenance import backup_database, inspect_database
from storage import Store


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()

    def tearDown(self):
        self.directory.cleanup()

    def test_live_wal_backup_restores_same_invite_account_and_session(self):
        source = self.root / 'source.db'
        store = Store(source, 'test-pepper', 'test-secret')
        code = store.generate_invites(1, 'keep note')[0]
        account, token = store.login_with_invite(code, 30)
        # Keep a reader open so committed state genuinely resides in the WAL.
        with closing(store.connect()) as reader:
            reader.execute('BEGIN')
            reader.execute('SELECT COUNT(*) FROM invite_codes').fetchone()
            extra_code = store.generate_invites(1, 'wal-only')[0]
            self.assertTrue(Path(str(source) + '-wal').is_file())
            target = self.root / 'nas.db'
            result = backup_database(source, target)
        self.assertTrue(result['source_counts_match'])
        self.assertEqual(result['journal_mode'], 'delete')
        self.assertEqual(result['table_counts']['invite_codes'], 2)
        restored = Store(target, 'test-pepper', 'test-secret', journal_mode='DELETE', vfs='unix-dotfile')
        self.assertEqual(restored.login_with_invite(code, 30)[0], account)
        self.assertEqual(restored.account_for_session(token), account)
        self.assertTrue(restored.login_with_invite(extra_code, 30)[0])
        self.assertEqual(os.stat(target).st_mode & 0o777, 0o600)
        self.assertFalse(Path(str(target) + '-wal').exists())

    def test_dotfile_backup_and_restore_remains_usable(self):
        source = self.root / 'source.db'
        store = Store(source, 'p', 's', journal_mode='DELETE', vfs='unix-dotfile')
        code = store.generate_invites(1)[0]
        backup = self.root / 'backup.db'
        backup_database(source, backup, source_vfs='unix-dotfile')
        restored_path = self.root / 'restored.db'
        backup_database(backup, restored_path, source_vfs='unix-dotfile')
        restored = Store(restored_path, 'p', 's', journal_mode='DELETE', vfs='unix-dotfile')
        self.assertTrue(restored.login_with_invite(code, 30)[0])
        self.assertFalse(Path(str(source) + '.lock').exists())

    def test_backup_refuses_existing_destination(self):
        source = self.root / 'source.db'
        Store(source, 'p', 's')
        target = self.root / 'target.db'
        target.write_bytes(b'existing-data')
        with self.assertRaises(FileExistsError):
            backup_database(source, target)
        self.assertEqual(target.read_bytes(), b'existing-data')

    def test_backup_does_not_overwrite_destination_created_during_publication(self):
        source = self.root / 'source.db'
        Store(source, 'p', 's')
        target = self.root / 'target.db'
        real_link = os.link
        def publish(stage, destination):
            target.write_bytes(b'concurrent-data')
            return real_link(stage, destination)
        with patch('scripts.sqlite_maintenance.os.link', side_effect=publish):
            with self.assertRaises(FileExistsError):
                backup_database(source, target)
        self.assertEqual(target.read_bytes(), b'concurrent-data')
        self.assertFalse(list(self.root.glob('.target.db.*')))

    def test_backup_rejects_foreign_key_corruption_before_publication(self):
        source = self.root / 'source.db'
        with closing(sqlite3.connect(source)) as c:
            c.executescript('CREATE TABLE parent(id PRIMARY KEY); CREATE TABLE child(parent_id REFERENCES parent(id)); INSERT INTO child VALUES (99);')
        target = self.root / 'target.db'
        with self.assertRaisesRegex(RuntimeError, 'foreign key'):
            backup_database(source, target)
        self.assertFalse(target.exists())

    def test_missing_mount_fails_without_creating_database(self):
        mount = self.root / 'unmounted'
        mount.mkdir()
        path = mount / 'app.db'
        with self.assertRaisesRegex(RuntimeError, 'mount is missing'):
            Store(path, 'p', 's', required_mount=mount)
        self.assertFalse(path.exists())

    def test_mounted_but_missing_database_fails_without_creating_database(self):
        path = self.root / 'missing.db'
        with patch.object(Path, 'is_mount', return_value=True):
            with self.assertRaisesRegex(RuntimeError, 'database is missing'):
                Store(path, 'p', 's', required_mount=self.root)
        self.assertFalse(path.exists())

    def test_removed_database_is_not_silently_recreated(self):
        path = self.root / 'persistent.db'
        Store(path, 'p', 's', journal_mode='DELETE')
        with patch.object(Path, 'is_mount', return_value=True):
            store = Store(path, 'p', 's', journal_mode='DELETE', vfs='unix-dotfile', required_mount=self.root)
            path.unlink()
            with self.assertRaises(sqlite3.OperationalError):
                store.connect()
        self.assertFalse(path.exists())

    def test_dotfile_vfs_rejects_wal(self):
        with self.assertRaisesRegex(ValueError, 'requires journal_mode=DELETE'):
            Store(self.root / 'bad.db', 'p', 's', vfs='unix-dotfile')

    def test_dotfile_lock_blocks_other_process_and_releases_after_rollback(self):
        path = self.root / 'lock.db'
        store = Store(path, 'p', 's', journal_mode='DELETE', vfs='unix-dotfile')
        program = """
import sqlite3, sys
c=sqlite3.connect(sys.argv[1],uri=True,timeout=0.15)
try:
 c.execute('BEGIN IMMEDIATE')
except sqlite3.OperationalError as error:
 assert 'locked' in str(error)
 print('blocked')
else:
 c.rollback()
 print('acquired')
finally:
 c.close()
"""
        uri = path.as_uri() + '?mode=rw&vfs=unix-dotfile'
        with closing(store.connect()) as first:
            first.execute('BEGIN IMMEDIATE')
            blocked = subprocess.run([sys.executable, '-c', program, uri], capture_output=True, text=True, check=True, timeout=5)
            self.assertEqual(blocked.stdout.strip(), 'blocked')
            first.rollback()
        released = subprocess.run([sys.executable, '-c', program, uri], capture_output=True, text=True, check=True, timeout=5)
        self.assertEqual(released.stdout.strip(), 'acquired')
        self.assertFalse(Path(str(path) + '.lock').exists())
