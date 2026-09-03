#!/usr/bin/env python3
"""Create a CanyonOS Core artifact tree from a chosen Python import root.

The script owns the mechanical part of step 1: it creates ``.car/config`` and
copies the selected import root to ``.car/app`` with development artifacts and
credential files excluded. Choosing the correct import root still requires
reading the application's imports.
"""

import argparse
import shutil
import sys
import uuid
from pathlib import Path

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


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


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


def prepare(import_root: Path, artifact_root: Path, force: bool = False) -> None:
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
    if app_dir.exists() and not force:
        raise FileExistsError(
            f"{app_dir} already exists; use --force to replace only the source copy"
        )
    if config_dir.exists() and not config_dir.is_dir():
        raise ValueError(f"config path exists but is not a directory: {config_dir}")

    artifact_root.mkdir(parents=True, exist_ok=True)
    temporary_app = artifact_root / f".app-{uuid.uuid4().hex}.tmp"
    previous_app = artifact_root / f".app-{uuid.uuid4().hex}.previous"

    installed_new_app = False
    try:
        shutil.copytree(
            import_root,
            temporary_app,
            ignore=_ignore_factory(artifact_root),
            copy_function=shutil.copy2,
            symlinks=True,
        )
        if app_dir.exists():
            app_dir.rename(previous_app)
        temporary_app.rename(app_dir)
        installed_new_app = True
        config_dir.mkdir(exist_ok=True)
    except Exception:
        if temporary_app.exists():
            shutil.rmtree(temporary_app)
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
        help="replace an existing app/ copy; leave config/ unchanged",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        prepare(Path(args.import_root), Path(args.artifact_root), args.force)
    except (OSError, ValueError) as error:
        sys.stderr.write(f"prepare.py: {error}\n")
        return 1

    artifact_root = Path(args.artifact_root)
    print(f"Created {artifact_root / 'config'}")
    print(f"Copied {Path(args.import_root)} to {artifact_root / 'app'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
