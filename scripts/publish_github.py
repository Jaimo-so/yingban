"""Publish the product subdirectory from a larger local workspace to GitHub.

Default: show a dry-run diff. Pass --push -m MESSAGE to commit and push.
Only public source/configuration/assets are copied to a fresh repository clone.
The enclosing workspace's Git index and history are never staged or pushed.
"""
from pathlib import Path, PurePosixPath
import argparse
import shutil
import subprocess
import tempfile

ROOT_FILES = {".dockerignore", ".env.example", ".gitignore", ".railwayignore", ".vefaasignore",
              "Dockerfile", "LICENSE", "README.md", "requirements.txt", "railway.json",
              "启动影伴.command", "影伴产品介绍.md"}
SCRIPTS = {"sqlite_maintenance.py", "build_release.py", "deploy_receive.py", "publish_github.py"}
SKIP = {"node_modules", ".next", "out", "__pycache__", ".git", ".venv", ".DS_Store"}


def public_path(name: str) -> bool:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or any(p in SKIP for p in path.parts):
        return False
    if len(path.parts) == 1:
        return name in ROOT_FILES or path.suffix == ".py"
    if any(p.startswith(".") for p in path.parts[1:] if p != ".gitignore"):
        return False
    top = path.parts[0]
    if top == "data":
        return name == "data/movies.json"
    if top == "scripts":
        return len(path.parts) == 2 and path.name in SCRIPTS
    if top == "tests":
        return path.suffix == ".py"
    if top == ".github":
        return len(path.parts) == 3 and path.parts[1] == "workflows" and path.suffix in {".yml", ".yaml"}
    if top == "docs":
        return len(path.parts) >= 3 and path.parts[1] == "images" and path.suffix.lower() in {".png", ".jpg", ".webp", ".svg"}
    if top == "frontend":
        return path.name != "next-env.d.ts" and path.suffix != ".tsbuildinfo"
    return top in {"web", "product-intro-assets"}


def source_files(root: Path) -> dict[str, Path]:
    files = {}
    # Prune generated trees before walking: frontend/node_modules can be large.
    import os
    for directory, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP and not (Path(directory) / d).is_symlink()]
        for name in names:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if public_path(relative):
                if path.is_symlink():
                    raise ValueError(f"Refusing symlink: {relative}")
                files[relative] = path
    return files


def sync(root: Path, checkout: Path) -> None:
    files = source_files(root)
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=checkout).decode().split("\0")
    for name in filter(None, tracked):
        if not public_path(name):
            raise ValueError(f"Remote tracked file is outside the public manifest; review first: {name}")
        if name not in files:
            (checkout / name).unlink()
    for name, source in files.items():
        target = checkout / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--push", action="store_true")
    parser.add_argument("-m", "--message", default="Update Yingban")
    parser.add_argument("--repo", default="Jaimo-so/yingban")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="yingban-publish-") as temporary:
        checkout = Path(temporary) / "repo"
        subprocess.run(["gh", "repo", "clone", args.repo, str(checkout), "--", "--branch", "main", "--single-branch"], check=True)
        sync(root, checkout)
        subprocess.run(["git", "add", "--all"], cwd=checkout, check=True)
        subprocess.run(["git", "status", "--short"], cwd=checkout, check=True)
        tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=checkout).decode().split("\0")
        if not all(public_path(name) for name in filter(None, tracked)):
            raise RuntimeError("A private path is tracked in the public repository")
        staged = subprocess.check_output(["git", "diff", "--cached", "--name-only", "-z"], cwd=checkout).decode().split("\0")
        if not all(public_path(name) for name in filter(None, staged)):
            raise RuntimeError("A private path reached the Git index")
        subprocess.run(["git", "diff", "--cached", "--stat"], cwd=checkout, check=True)
        subprocess.run(["git", "diff", "--cached", "--check"], cwd=checkout, check=True)
        if not args.push:
            print("Dry run complete. Use --push -m MESSAGE to publish these changes.")
            return
        if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=checkout).returncode == 0:
            print("GitHub already matches the public local files.")
            return
        for key in ("user.name", "user.email"):
            value = subprocess.check_output(["git", "config", "--get", key], cwd=root).decode().strip()
            subprocess.run(["git", "config", key, value], cwd=checkout, check=True)
        subprocess.run(["git", "commit", "-m", args.message], cwd=checkout, check=True)
        subprocess.run(["git", "push", "origin", "HEAD:main"], cwd=checkout, check=True)
        print(f"Published. Deployment progress: https://github.com/{args.repo}/actions")


if __name__ == "__main__":
    main()
