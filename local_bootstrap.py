from __future__ import annotations

import os
import secrets
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
ENV_EXAMPLE_PATH = BASE_DIR / ".env.example"
DEFAULT_DATABASE_PATH = BASE_DIR / "data" / "yingban.db"

PLACEHOLDERS = {
    "YINGBAN_INVITE_PEPPER": {
        "",
        "replace-with-a-long-random-secret",
        "development-invite-pepper-change-me",
    },
    "YINGBAN_SESSION_SECRET": {
        "",
        "replace-with-another-long-random-secret",
        "development-session-secret-change-me",
    },
    "YINGBAN_ADMIN_TOKEN": {
        "",
        "replace-with-a-private-admin-token",
    },
}


@dataclass(frozen=True)
class LocalEnvironment:
    created: bool
    changed_keys: tuple[str, ...]
    values: dict[str, str]


def parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            values[key] = value.strip().strip('"').strip("'")
    return values


def replace_env_value(text: str, key: str, value: str) -> str:
    lines = text.splitlines()
    replacement = f"{key}={value}"
    for index, raw_line in enumerate(lines):
        candidate = raw_line.strip()
        if candidate.startswith("#") or "=" not in candidate:
            continue
        existing_key, _existing_value = candidate.split("=", 1)
        if existing_key.strip() == key:
            lines[index] = replacement
            break
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(replacement)
    return "\n".join(lines).rstrip() + "\n"


def configured_database_path(values: dict[str, str], base_dir: Path) -> Path:
    configured = values.get("YINGBAN_DATABASE", "").strip()
    if not configured:
        return base_dir / "data" / "yingban.db"
    path = Path(configured).expanduser()
    return path if path.is_absolute() else base_dir / path


def ensure_local_environment(
    env_path: Path = ENV_PATH,
    env_example_path: Path = ENV_EXAMPLE_PATH,
    base_dir: Path = BASE_DIR,
) -> LocalEnvironment:
    existed = env_path.is_file()
    if existed:
        text = env_path.read_text(encoding="utf-8")
    elif env_example_path.is_file():
        text = env_example_path.read_text(encoding="utf-8")
    else:
        text = ""

    values = parse_env(text)
    database_path = configured_database_path(values, base_dir)
    database_exists = database_path.is_file() and database_path.stat().st_size > 0
    changed: list[str] = []

    generated = {
        "YINGBAN_INVITE_PEPPER": secrets.token_urlsafe(48),
        "YINGBAN_SESSION_SECRET": secrets.token_urlsafe(48),
        "YINGBAN_ADMIN_TOKEN": f"YB-ADMIN-{secrets.token_urlsafe(24)}",
    }
    for key, placeholders in PLACEHOLDERS.items():
        current = values.get(key, "")
        if current not in placeholders and not current.startswith("development-"):
            continue
        if database_exists:
            raise RuntimeError(
                "检测到已有影伴数据库，但本机安全凭据缺失或仍是示例值。"
                "为避免使已有邀请码和会话失效，请先备份数据库，再手动配置 .env。"
            )
        text = replace_env_value(text, key, generated[key])
        values[key] = generated[key]
        changed.append(key)

    if changed or not existed:
        env_path.parent.mkdir(parents=True, exist_ok=True)
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{env_path.name}.", dir=env_path.parent
        )
        try:
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_name, 0o600)
            os.replace(temporary_name, env_path)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)
    else:
        os.chmod(env_path, 0o600)

    return LocalEnvironment(
        created=not existed,
        changed_keys=tuple(changed),
        values=values,
    )


def copy_to_macos_clipboard(value: str) -> bool:
    if os.getenv("YINGBAN_SKIP_CLIPBOARD") == "1" or sys.platform != "darwin":
        return False
    try:
        completed = subprocess.run(
            ["pbcopy"],
            input=value,
            text=True,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    return completed.returncode == 0


def has_usable_invite(items: list[dict[str, object]]) -> bool:
    return any(item.get("status") in {"issued", "active"} for item in items)


def main() -> int:
    try:
        environment = ensure_local_environment()
    except (OSError, RuntimeError) as error:
        print(f"首次启动配置失败：{error}", file=sys.stderr)
        return 1

    for key in PLACEHOLDERS:
        os.environ[key] = environment.values[key]

    from app import build_context

    app = build_context()
    invite_code = ""
    if not has_usable_invite(app.store.list_invites()):
        invite_code = app.store.generate_invites(1, "本机首次启动")[0]

    if environment.changed_keys or invite_code:
        print("")
        print("================ 影伴首次启动 ================")
        if invite_code:
            print(f"用户邀请码：{invite_code}")
            if copy_to_macos_clipboard(invite_code):
                print("邀请码已复制到剪贴板；页面打开后按 Command-V 粘贴即可。")
        if "YINGBAN_ADMIN_TOKEN" in environment.changed_keys:
            print(f"管理员口令：{environment.values['YINGBAN_ADMIN_TOKEN']}")
            print("管理员口令同时保存在本机 .env 的 YINGBAN_ADMIN_TOKEN 中。")
        print("请妥善保管以上凭据；影伴不会把它们提交到 Git。")
        print("================================================")
        print("")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
