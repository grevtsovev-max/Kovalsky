"""Compare deployed source files with Git without reading runtime data."""
import argparse
import hashlib
import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOP_LEVEL = {"pyproject.toml", "KANBAN.json"}


def git(*args):
    return subprocess.check_output(["git", "-C", str(ROOT), *args])


def included(name):
    path = Path(name)
    return (name in TOP_LEVEL or name.startswith("newsroom/")) and not (
        "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}
    )


def compare(release, ref):
    # Resolve first, so an invalid ref cannot be interpreted as a Git option.
    commit = git("rev-parse", "--verify", "--end-of-options", ref + "^{commit}").decode().strip()
    names = {
        name for name in git("ls-tree", "-r", "--name-only", commit).decode().splitlines()
        if included(name)
    }
    if not release.is_dir() or not (release / "newsroom").is_dir():
        raise ValueError("Release directory is unavailable or has no newsroom package")
    same, changed, missing, unreadable = [], [], [], []
    for name in sorted(names):
        path = release / name
        try:
            actual = path.read_bytes()
        except FileNotFoundError:
            missing.append(name)
            continue
        except (PermissionError, IsADirectoryError):
            unreadable.append(name)
            continue
        expected = git("show", commit + ":" + name)
        if actual == expected:
            same.append(name)
        else:
            changed.append({"file": name, "git_sha256": hashlib.sha256(expected).hexdigest(),
                            "release_sha256": hashlib.sha256(actual).hexdigest()})
    actual_names = {
        str(path.relative_to(release)) for path in (release / "newsroom").rglob("*")
        if path.is_file() and included(str(path.relative_to(release)))
    }
    return {"commit": commit, "matching_files": len(same), "changed": changed,
            "missing": missing, "unreadable": unreadable,
            "extra": sorted(actual_names - names)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--ref", default="origin/main")
    args = parser.parse_args()
    try:
        report = compare(args.release.resolve(), args.ref)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(2, f"Comparison failed: {type(error).__name__}\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(any(report[key] for key in ("changed", "missing", "unreadable", "extra")))


if __name__ == "__main__":
    raise SystemExit(main())
