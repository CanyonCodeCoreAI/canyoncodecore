"""
Logic for `canyonos sync`: mirror the current project directory into the Global
Controller container's /workspace volume via `docker cp`.

Files live inside the container's named volume (see `init.py`), not on a live
bind mount, so host-side edits don't reach a running build. A manifest records
which paths the previous sync managed. Before the next additive `docker cp`,
only managed paths that disappeared from the host are removed; container-side
build artifacts and other untracked workspace files are left alone.
"""

import json
import os
import tempfile

from canyonos import ui
from canyonos.docker_cmd import DOCKER_RUN_TIMEOUT, run_docker
from canyonos.gc import require_state
from canyonos.init import GC_WORKSPACE_PATH, STATE_DIR


SYNC_STATE_PATH = os.path.join(STATE_DIR, "sync-manifest.json")
_SYNC_STATE_VERSION = 1

# This runs inside the Global Controller container. Paths are deleted deepest
# first, and directories are removed only when empty. That preserves files a
# container-side build may have generated beneath a formerly synced directory.
_DELETE_STALE_SCRIPT = """
import errno
import json
import os
import sys

root = os.path.realpath(sys.argv[1])
manifest_path = sys.argv[2]
try:
    with open(manifest_path, encoding="utf-8") as manifest_file:
        stale_entries = json.load(manifest_file)
    for relative, kind in stale_entries:
        parts = relative.split("/")
        if not relative or any(part in ("", ".", "..") for part in parts):
            raise RuntimeError("unsafe sync manifest path: " + repr(relative))
        unresolved_parent = os.path.join(root, *parts[:-1])
        parent = os.path.realpath(unresolved_parent)
        if parent != os.path.abspath(unresolved_parent):
            raise RuntimeError("sync manifest path crosses a symlink: " + repr(relative))
        if os.path.commonpath((root, parent)) != root:
            raise RuntimeError("sync manifest path escapes workspace: " + repr(relative))
        target = os.path.join(parent, parts[-1])
        if kind == "directory":
            try:
                os.rmdir(target)
            except FileNotFoundError:
                pass
            except OSError as error:
                if error.errno not in (errno.ENOTEMPTY, errno.EEXIST, errno.ENOTDIR):
                    raise
        else:
            try:
                if not os.path.isdir(target) or os.path.islink(target):
                    os.unlink(target)
            except FileNotFoundError:
                pass
finally:
    try:
        os.unlink(manifest_path)
    except FileNotFoundError:
        pass
"""


def _project_entries(project_root):
    """Return the relative paths and types managed by a project sync."""
    entries = {}
    for directory, dirnames, filenames in os.walk(project_root, followlinks=False):
        for name in dirnames + filenames:
            path = os.path.join(directory, name)
            relative = os.path.relpath(path, project_root).replace(os.sep, "/")
            entries[relative] = (
                "directory"
                if os.path.isdir(path) and not os.path.islink(path)
                else "entry"
            )
    return entries


# The manifest only records what the last sync copied, so discarding it costs
# nothing beyond skipping stale-file removal on the next run.
_MANIFEST_RECOVERY_HINT = (
    "Move or remove that file, then run `canyonos deploy` again."
)


def _previous_entries(container_id):
    try:
        with open(SYNC_STATE_PATH, encoding="utf-8") as manifest_file:
            manifest = json.load(manifest_file)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(
            f"Could not read the sync manifest at {SYNC_STATE_PATH}: {error}. "
            f"{_MANIFEST_RECOVERY_HINT}"
        ) from None

    if not isinstance(manifest, dict):
        raise RuntimeError(
            f"The sync manifest at {SYNC_STATE_PATH} is invalid. {_MANIFEST_RECOVERY_HINT}"
        )

    if manifest.get("container_id") != container_id:
        return {}

    entries = manifest.get("entries")
    if (
        manifest.get("version") != _SYNC_STATE_VERSION
        or not isinstance(entries, dict)
        or any(
            not isinstance(path, str)
            or not path
            or path.startswith("/")
            or any(part in ("", ".", "..") for part in path.split("/"))
            or kind not in {"directory", "entry"}
            for path, kind in entries.items()
        )
    ):
        raise RuntimeError(
            f"The sync manifest at {SYNC_STATE_PATH} is invalid. {_MANIFEST_RECOVERY_HINT}"
        )
    return entries


def _stale_entries(previous, current):
    stale = [
        (path, kind)
        for path, kind in previous.items()
        if current.get(path) != kind
    ]
    return sorted(stale, key=lambda item: item[0].count("/"), reverse=True)


def _remove_stale_entries(container_id, stale_entries):
    if not stale_entries:
        return

    local_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", prefix="canyonos-sync-stale-", suffix=".json", delete=False
        ) as manifest_file:
            local_path = manifest_file.name
            json.dump(stale_entries, manifest_file)

        remote_path = f"/tmp/{os.path.basename(local_path)}"
        copied = run_docker(
            ["docker", "cp", local_path, f"{container_id}:{remote_path}"],
            timeout=DOCKER_RUN_TIMEOUT,
            action="Preparing stale project file cleanup",
        )
        if copied.returncode != 0:
            detail = copied.stderr.strip() or copied.stdout.strip()
            raise RuntimeError(f"Could not prepare stale project file cleanup: {detail}")

        removed = run_docker(
            [
                "docker",
                "exec",
                container_id,
                "python",
                "-c",
                _DELETE_STALE_SCRIPT,
                GC_WORKSPACE_PATH,
                remote_path,
            ],
            timeout=DOCKER_RUN_TIMEOUT,
            action="Removing stale project files",
        )
        if removed.returncode != 0:
            detail = removed.stderr.strip() or removed.stdout.strip()
            raise RuntimeError(f"Could not remove stale project files: {detail}")
    finally:
        if local_path is not None:
            try:
                os.remove(local_path)
            except FileNotFoundError:
                pass


def _save_sync_state(container_id, entries):
    os.makedirs(os.path.dirname(SYNC_STATE_PATH), exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=os.path.dirname(SYNC_STATE_PATH),
            prefix="sync-manifest.",
            suffix=".tmp",
            delete=False,
        ) as manifest_file:
            temporary_path = manifest_file.name
            json.dump(
                {
                    "version": _SYNC_STATE_VERSION,
                    "container_id": container_id,
                    "entries": entries,
                },
                manifest_file,
                sort_keys=True,
            )
            manifest_file.flush()
            os.fsync(manifest_file.fileno())
        os.replace(temporary_path, SYNC_STATE_PATH)
    except Exception:
        if temporary_path is not None:
            try:
                os.remove(temporary_path)
            except FileNotFoundError:
                pass
        raise


def run_sync():
    """Mirror the current directory into the container. Returns True on success."""
    state = require_state()
    if state is None:
        return False

    container_id = state["container_id"]
    # Trailing "/." copies the *contents* of the current directory into
    # /workspace, rather than nesting it under /workspace/<dirname>.
    src = os.path.join(os.getcwd(), ".")
    label = f"Syncing {os.getcwd()} -> {container_id[:12]}:{GC_WORKSPACE_PATH}"

    with ui.status(f"{label}..."):
        # Captured so docker's own progress output doesn't clobber the spinner.
        try:
            entries = _project_entries(os.getcwd())
            stale_entries = _stale_entries(
                _previous_entries(container_id), entries
            )
            _remove_stale_entries(container_id, stale_entries)
            result = run_docker(
                ["docker", "cp", src, f"{container_id}:{GC_WORKSPACE_PATH}"],
                timeout=DOCKER_RUN_TIMEOUT,
                action="Syncing project files",
            )
        except (OSError, RuntimeError) as e:
            ui.fail(str(e))
            return False
    if result.returncode != 0:
        ui.fail(f"Sync failed: {result.stderr.strip() or result.stdout.strip()}")
        return False

    try:
        _save_sync_state(container_id, entries)
    except OSError as e:
        ui.fail(f"Could not save the sync manifest at {SYNC_STATE_PATH}: {e}")
        return False

    ui.ok("Sync complete.")
    return True
