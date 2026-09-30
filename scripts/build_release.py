"""Build the production bundle from code and the verified Next.js export only."""
from pathlib import Path
import argparse
import tarfile


def build(root: Path, output: Path) -> None:
    files = list(root.glob("*.py")) + [root / "requirements.txt", root / "data/movies.json"]
    for folder in ("web", "frontend/out"):
        files.extend(p for p in (root / folder).rglob("*") if p.is_file() and p.name != ".DS_Store")
    for page in ("index.html", "admin.html", "share.html", "landing.html"):
        if not (root / "frontend/out" / page).is_file():
            raise ValueError("Run frontend build and verify:parity before packaging")
    with tarfile.open(output, "w:gz") as archive:
        for path in sorted(files):
            relative = path.relative_to(root)
            if path.is_symlink() or any(part.startswith(".") for part in relative.parts):
                raise ValueError(f"Refusing hidden file or symlink: {relative}")
            archive.add(path, arcname=relative.as_posix(), recursive=False)
    print(f"Release bundle: {output} ({len(files)} files)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    build(Path(__file__).resolve().parents[1], args.output.resolve())
