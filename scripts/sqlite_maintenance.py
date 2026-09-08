#!/usr/bin/env python3
"""Verified SQLite snapshots. Quiesce writers separately before a migration cutover."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from urllib.parse import urlencode


def connect_readonly(path: Path, vfs: str = "") -> sqlite3.Connection:
    if vfs not in {"", "unix-dotfile"}:
        raise ValueError("unsupported SQLite VFS")
    options = {"mode": "ro"}
    if vfs:
        options["vfs"] = vfs
    connection = sqlite3.connect(path.resolve().as_uri() + "?" + urlencode(options), uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    names = [row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )]
    return {name: connection.execute(
        'SELECT COUNT(*) FROM "' + name.replace('"', '""') + '"'
    ).fetchone()[0] for name in names}


def inspect_database(path: Path, vfs: str = "") -> dict[str, object]:
    if not path.is_file():
        raise FileNotFoundError(path)
    with closing(connect_readonly(path, vfs)) as connection:
        connection.execute("BEGIN")
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_key_errors = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        counts = table_counts(connection)
    return {
        "path": str(path.resolve()), "bytes": path.stat().st_size,
        "integrity_check": integrity, "foreign_key_errors": foreign_key_errors,
        "journal_mode": str(journal_mode).lower(), "table_counts": counts,
    }


def backup_database(
    source: Path, destination: Path, journal_mode: str | None = "DELETE", *, source_vfs: str = ""
) -> dict[str, object]:
    # Every published snapshot is standalone DELETE mode. WAL is only a runtime choice.
    if journal_mode not in {None, "DELETE", "delete"}:
        raise ValueError("backup/migration snapshots require journal_mode=DELETE")
    if not source.is_file():
        raise FileNotFoundError(source)
    if os.path.lexists(destination):
        raise FileExistsError(f"refusing to overwrite {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage: Path | None = None
    try:
        # SQLite backup may copy WAL mode from its source: do it on a local disk,
        # then close and convert before any database bytes are placed on NAS.
        with tempfile.TemporaryDirectory(prefix="yingban-snapshot-") as directory:
            local = Path(directory) / "snapshot.db"
            with closing(connect_readonly(source, source_vfs)) as src:
                src.execute("BEGIN")
                source_counts = table_counts(src)  # establishes the same read snapshot used by backup
                with closing(sqlite3.connect(local)) as dst:
                    src.backup(dst)
                    mode = dst.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
                    if mode.lower() != "delete":
                        raise RuntimeError("snapshot could not enter DELETE mode")
            verified = inspect_database(local)
            if verified["integrity_check"] != "ok" or verified["foreign_key_errors"] != 0:
                raise RuntimeError("snapshot failed SQLite integrity or foreign key checks")
            if verified["table_counts"] != source_counts:
                raise RuntimeError("snapshot table counts differ from source read snapshot")
            descriptor, name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
            stage = Path(name)
            with os.fdopen(descriptor, "wb") as target, local.open("rb") as origin:
                shutil.copyfileobj(origin, target)
                target.flush()
                os.fsync(target.fileno())
            staged = inspect_database(stage, "unix-dotfile")
            if any(staged[key] != verified[key] for key in ("integrity_check", "foreign_key_errors", "table_counts", "journal_mode")):
                raise RuntimeError("destination snapshot failed verification")
            # Unlike exists()+replace(), link publishes atomically WITHOUT overwriting
            # a target created concurrently. Both handles are closed before publication.
            os.link(stage, destination)
            stage.unlink()
            stage = None
            directory_fd = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            verified.update(path=str(destination.resolve()), source_counts_match=True)
            return verified
    finally:
        if stage is not None:
            stage.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    default_vfs = os.getenv("YINGBAN_SQLITE_VFS", "") or "default"
    inspect_parser = subparsers.add_parser("inspect", help="read integrity checks and row counts")
    inspect_parser.add_argument("database", type=Path)
    inspect_parser.add_argument("--vfs", choices=("default", "unix-dotfile"), default=default_vfs)
    for command in ("backup", "migrate"):
        action = subparsers.add_parser(command, help="publish a verified DELETE-mode snapshot without overwriting")
        action.add_argument("source", type=Path)
        action.add_argument("destination", type=Path)
        action.add_argument("--source-vfs", choices=("default", "unix-dotfile"), default=default_vfs)
        action.add_argument("--journal-mode", choices=("DELETE",), default="DELETE")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "inspect":
        result = inspect_database(args.database, "" if args.vfs == "default" else args.vfs)
    else:
        result = backup_database(args.source, args.destination, args.journal_mode,
                                 source_vfs="" if args.source_vfs == "default" else args.source_vfs)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
