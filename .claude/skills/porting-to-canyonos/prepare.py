#!/usr/bin/env python3
"""Create a CanyonOS Core artifact tree from a chosen Python import root.

The script owns the mechanical part of step 1: it creates ``.car/config`` and
copies the selected import root to ``.car/app`` with development artifacts and
credential files excluded. Choosing the correct import root still requires
reading the application's imports.
"""

import argparse
import hashlib
import json
import os
import shutil
import sys
import uuid
from pathlib import Path, PurePosixPath

EXCLUDED_DIRECTORIES = frozenset(
    {
        ".car",
        ".git",
        ".hg",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        ".svn",
        "__pycache__",
        "build",
        "dist",
        "env",
        "htmlcov",
        "node_modules",
        "venv",
    }
)
EXCLUDED_FILE_SUFFIXES = (".pyc", ".pyo")
ENV_TEMPLATES = frozenset({".env.example", ".env.sample", ".env.template"})
STATE_FILENAME = ".porting-state.json"


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _reject_symlinks(import_root: Path) -> None:
    """Keep the copied artifact independent of paths outside the copy."""
    for path in import_root.rglob("*"):
        if path.is_symlink():
            relative = path.relative_to(import_root)
            raise ValueError(
                f"import root contains a symbolic link: {relative}; replace it "
                "with the intended file or directory before preparing the port"
            )


def _ignore_factory(artifact_root: Path):
    def ignore(directory: str, names: list[str]) -> set[str]:
        directory_path = Path(directory)
        ignored = set()
        for name in names:
            path = directory_path / name
            if path.resolve() == artifact_root:
                ignored.add(name)
            elif path.is_dir() and name in EXCLUDED_DIRECTORIES:
                ignored.add(name)
            elif name.startswith(".env") and name not in ENV_TEMPLATES:
                ignored.add(name)
            elif name.endswith(EXCLUDED_FILE_SUFFIXES):
                ignored.add(name)
        return ignored

    return ignore


def _copy_source(import_root: Path, destination: Path, artifact_root: Path) -> None:
    shutil.copytree(
        import_root,
        destination,
        ignore=_ignore_factory(artifact_root),
        copy_function=shutil.copy2,
        symlinks=True,
    )


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): _hash_file(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def _load_state(config_dir: Path) -> dict[str, str]:
    state_path = config_dir / STATE_FILENAME
    if not state_path.is_file():
        raise ValueError(
            f"{state_path} is missing; this artifact predates refresh tracking. "
            "Use --force only if discarding all edits in .car/app is intentional"
        )
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read refresh state {state_path}: {error}") from error
    files = state.get("source_files") if isinstance(state, dict) else None
    if (
        not isinstance(state, dict)
        or state.get("version") != 1
        or not isinstance(files, dict)
        or not all(_valid_state_entry(path, digest) for path, digest in files.items())
    ):
        raise ValueError(f"invalid refresh state: {state_path}")
    return files


def _valid_state_entry(path: object, digest: object) -> bool:
    if not isinstance(path, str) or not isinstance(digest, str):
        return False
    relative = PurePosixPath(path)
    return (
        path == relative.as_posix()
        and not relative.is_absolute()
        and path not in ("", ".")
        and ".." not in relative.parts
        and len(digest) == 64
        and all(character in "0123456789abcdef" for character in digest)
    )


def _write_state(path: Path, source_hashes: dict[str, str]) -> None:
    path.write_text(
        json.dumps(
            {"version": 1, "source_files": source_hashes},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _refresh_app(
    app_dir: Path,
    source_copy: Path,
    staged_app: Path,
    previous_source: dict[str, str],
) -> None:
    current = _file_hashes(app_dir)
    incoming = _file_hashes(source_copy)
    conflicts = []

    for relative in sorted(previous_source.keys() | incoming.keys() | current.keys()):
        old = previous_source.get(relative)
        new = incoming.get(relative)
        edited = current.get(relative)
        source_changed = new != old
        app_changed = edited != old
        if old is None and new is not None and edited not in (None, new):
            conflicts.append(relative)
        elif old is not None and source_changed and app_changed and edited != new:
            conflicts.append(relative)

    # File/directory replacements need the same three-way protection. A new
    # source file at `pkg` must not erase a port-only `pkg/adapter.py`, and a
    # new source directory must not silently replace a port-authored file at
    # `pkg`.
    for incoming_path in incoming:
        prefix = incoming_path + "/"
        for current_path, edited in current.items():
            if not current_path.startswith(prefix):
                continue
            if edited != previous_source.get(current_path):
                conflicts.append(current_path)
    for current_path, edited in current.items():
        prefix = current_path + "/"
        if any(incoming_path.startswith(prefix) for incoming_path in incoming):
            if edited != previous_source.get(current_path):
                conflicts.append(current_path)

    if conflicts:
        conflicts = sorted(set(conflicts))
        shown = "\n  ".join(conflicts[:20])
        suffix = "" if len(conflicts) <= 20 else f"\n  ... and {len(conflicts) - 20} more"
        raise ValueError(
            "refresh found files changed in both the source and .car/app:\n  "
            f"{shown}{suffix}\nResolve them in .car/app, then update the source "
            "or use --force only to discard all port edits"
        )

    shutil.copytree(app_dir, staged_app, copy_function=shutil.copy2, symlinks=True)

    # Apply safe source deletions first so file-to-directory changes have room.
    for relative, old in previous_source.items():
        if relative in incoming or current.get(relative) != old:
            continue
        target = staged_app / relative
        if target.is_file():
            target.unlink()

    for directory in sorted(
        (path for path in staged_app.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        try:
            directory.rmdir()
        except OSError:
            pass

    for relative, new in incoming.items():
        old = previous_source.get(relative)
        edited = current.get(relative)
        if new == old or edited == new:
            continue
        if old is not None and edited != old:
            continue
        target = staged_app / relative
        if target.is_dir():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_copy / relative, target)


def prepare(
    import_root: Path,
    artifact_root: Path,
    force: bool = False,
    refresh: bool = False,
) -> None:
    import_root = import_root.expanduser().resolve()
    artifact_root = artifact_root.expanduser().resolve()
    app_dir = artifact_root / "app"
    config_dir = artifact_root / "config"

    if not import_root.is_dir():
        raise ValueError(f"import root is not a directory: {import_root}")
    if import_root == artifact_root:
        raise ValueError("artifact root cannot also be the import root")
    if _is_relative_to(import_root, artifact_root):
        raise ValueError("import root cannot be inside the artifact root")
    if app_dir.exists() and not app_dir.is_dir():
        raise ValueError(f"app path exists but is not a directory: {app_dir}")
    if force and refresh:
        raise ValueError("--force and --refresh are mutually exclusive")
    if refresh and not app_dir.is_dir():
        raise ValueError(f"cannot refresh because {app_dir} does not exist")
    if app_dir.exists() and not force and not refresh:
        raise FileExistsError(
            f"{app_dir} already exists; use --refresh to preserve port edits, or "
            "--force to discard and replace the entire source copy"
        )
    if config_dir.exists() and not config_dir.is_dir():
        raise ValueError(f"config path exists but is not a directory: {config_dir}")

    _reject_symlinks(import_root)
    if refresh:
        _reject_symlinks(app_dir)

    artifact_root.mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(exist_ok=True)
    transaction_id = uuid.uuid4().hex
    source_copy = artifact_root / f".source-{transaction_id}.tmp"
    temporary_app = artifact_root / f".app-{transaction_id}.tmp"
    previous_app = artifact_root / f".app-{uuid.uuid4().hex}.previous"
    temporary_state = config_dir / f".{STATE_FILENAME}-{transaction_id}.tmp"
    state_path = config_dir / STATE_FILENAME

    installed_new_app = False
    try:
        _copy_source(import_root, source_copy, artifact_root)
        source_hashes = _file_hashes(source_copy)
        if refresh:
            previous_source = _load_state(config_dir)
            _refresh_app(app_dir, source_copy, temporary_app, previous_source)
            shutil.rmtree(source_copy)
        else:
            source_copy.rename(temporary_app)
        _write_state(temporary_state, source_hashes)
        if app_dir.exists():
            app_dir.rename(previous_app)
        temporary_app.rename(app_dir)
        installed_new_app = True
        os.replace(temporary_state, state_path)
    except Exception:
        if source_copy.exists():
            shutil.rmtree(source_copy)
        if temporary_app.exists():
            shutil.rmtree(temporary_app)
        if temporary_state.exists():
            temporary_state.unlink()
        if installed_new_app and app_dir.exists():
            shutil.rmtree(app_dir)
        if previous_app.exists():
            previous_app.rename(app_dir)
        raise

    if previous_app.exists():
        shutil.rmtree(previous_app)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Create .car/config and copy an import root into .car/app."
    )
    parser.add_argument(
        "import_root",
        help="directory whose contents should become the contents of .car/app",
    )
    parser.add_argument(
        "artifact_root",
        nargs="?",
        default=".car",
        help="artifact directory to create (default: .car)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="discard edits and replace an existing app/ copy; leave config/ unchanged",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="update unchanged source files while preserving port-authored edits",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        prepare(
            Path(args.import_root),
            Path(args.artifact_root),
            force=args.force,
            refresh=args.refresh,
        )
    except (OSError, ValueError) as error:
        sys.stderr.write(f"prepare.py: {error}\n")
        return 1

    artifact_root = Path(args.artifact_root)
    print(f"Created {artifact_root / 'config'}")
    action = "Refreshed" if args.refresh else "Copied"
    print(f"{action} {Path(args.import_root)} to {artifact_root / 'app'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
