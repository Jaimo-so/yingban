#!/usr/bin/python3
"""Root-owned SSH forced command; receives `deploy <commit>` and a tar.gz on stdin.

Install as /usr/local/sbin/yingban-deploy, owned by root, mode 0755.
The deployed application still runs as the existing yingban systemd user.
"""
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
import fcntl
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
from urllib.request import urlopen

BASE = Path("/opt/yingban")
REQUIRED = {"app.py", "server.py", "settings.py", "storage.py", "agent.py",
            "fastapi_app.py", "requirements.txt", "data/movies.json",
            "web/index.html", "web/admin.html", "web/share.html",
            "frontend/out/index.html", "frontend/out/admin.html", "frontend/out/share.html"}
MAX_BYTES = 128 * 1024 * 1024


def allowed(name: str) -> bool:
    path = PurePosixPath(name)
    if path.is_absolute() or path.as_posix() != name or any(p.startswith(".") for p in path.parts):
        return False
    return (bool(re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*\.py", name))
            or name in {"requirements.txt", "data/movies.json"}
            or name.startswith("web/") or name.startswith("frontend/out/"))


def unpack(payload: bytes, destination: Path) -> None:
    """Validate the entire archive before creating any output files."""
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        members = archive.getmembers()
        names = [m.name for m in members]
        if len(names) != len(set(names)) or not REQUIRED.issubset(names):
            raise ValueError("Bundle contains duplicate paths or lacks required runtime files")
        if sum(m.size for m in members) > MAX_BYTES:
            raise ValueError("Expanded bundle exceeds 128 MiB")
        for member in members:
            if not member.isfile() or not allowed(member.name):
                raise ValueError(f"Disallowed bundle entry: {member.name}")
        for member in members:
            target = destination / member.name
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
            target.chmod(0o644)


def health() -> dict:
    with urlopen("http://127.0.0.1:8765/api/health", timeout=5) as response:
        result = json.load(response)
    if result.get("ok") is not True or result.get("local_open_access") is not False:
        raise RuntimeError("Production health or authentication boundary failed")
    return result


def switch(release: Path) -> None:
    temporary = BASE / f".current-{os.getpid()}"
    temporary.symlink_to(release)
    os.replace(temporary, BASE / "current")


def wait_healthy(mode: str) -> None:
    for _ in range(30):
        try:
            if health().get("mode") == mode:
                return
        except Exception:
            pass
        time.sleep(2)
    raise RuntimeError("Service did not recover with the previous model mode within 60 seconds")


def deploy(commit: str, payload: bytes) -> None:
    previous = (BASE / "current").resolve(strict=True)
    before = health()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    release = BASE / "releases" / f"{stamp}-{commit[:12]}"
    release.mkdir(mode=0o755)
    try:
        unpack(payload, release)
        if (release / "requirements.txt").read_bytes() != (previous / "requirements.txt").read_bytes():
            raise RuntimeError("requirements.txt changed: prepare and verify server dependencies before deploying")
        for source in release.glob("*.py"):
            compile(source.read_bytes(), str(source), "exec")
        (release / "DEPLOYED_COMMIT").write_text(commit + "\n")
        subprocess.run(["/usr/local/sbin/yingban-backup"], check=True, timeout=120)
    except Exception:
        shutil.rmtree(release)
        raise
    try:
        switch(release)
        subprocess.run(["systemctl", "restart", "yingban.service"], check=True, timeout=60)
        wait_healthy(before["mode"])
    except Exception:
        switch(previous)
        subprocess.run(["systemctl", "restart", "yingban.service"], check=True, timeout=60)
        wait_healthy(before["mode"])
        print(f"Deployment failed; restored previous code: {previous}", file=sys.stderr)
        raise
    print(json.dumps({"deployed_commit": commit, "release": str(release),
                      "previous": str(previous), "health": health()}, ensure_ascii=False))


def main() -> None:
    match = re.fullmatch(r"deploy ([0-9a-f]{40})", os.environ.get("SSH_ORIGINAL_COMMAND", ""))
    if match is None:
        raise ValueError("Only deploy <40-character commit SHA> is accepted")
    os.umask(0o022)
    with open("/run/lock/yingban-deploy.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        payload = sys.stdin.buffer.read(MAX_BYTES + 1)
        if len(payload) > MAX_BYTES:
            raise ValueError("Compressed bundle exceeds 128 MiB")
        deploy(match.group(1), payload)


if __name__ == "__main__":
    main()
