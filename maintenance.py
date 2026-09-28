"""Explicit offline workspace backup/restore; no network or service operations."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from workspace import MODE_FILE, WORKSPACE_FILE, Mode, WorkspaceError, _verify_schema, utc_now

MANIFEST = "backup-manifest.json"


def _safe_file(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise WorkspaceError("unsafe backup path")
    target = root / path
    if any(part.is_symlink() for part in [target, *target.parents] if part.is_relative_to(root)):
        raise WorkspaceError("backup refuses symlinks")
    if not target.resolve().is_relative_to(root) or not target.is_file():
        raise WorkspaceError("backup file missing or outside workspace")
    return target


def _readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _check(conn: sqlite3.Connection, mode: Mode) -> None:
    _verify_schema(conn, mode=mode, deep=True)
    if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise WorkspaceError("workspace integrity check failed")


def copy_workspace(source: Path, destination: Path, mode: Mode, *, restore: bool = False) -> dict:
    """Copy DB consistently, then copy only references from that DB. Never overwrite.

    A missing mode marker makes a failed candidate unusable. Failed directories are
    retained for explicit inspection, never treated as successful backups.
    """
    if mode not in ("demo", "production"):
        raise WorkspaceError("invalid workspace mode")
    if source.is_symlink() or destination.is_symlink():
        raise WorkspaceError("workspace root cannot be a symlink")
    source = source.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if destination.is_relative_to((Path(__file__).resolve().parent / "assets").resolve()):
        raise WorkspaceError("backup cannot be exposed through static assets")
    if destination.is_relative_to(source) or source.is_relative_to(destination):
        raise WorkspaceError("source and destination must be separate directories")
    marker = _safe_file(source, MODE_FILE)
    if marker.read_text(encoding="ascii").strip() != mode:
        raise WorkspaceError("workspace mode mismatch")
    database = _safe_file(source, WORKSPACE_FILE)
    manifest = None
    if restore:
        try:
            manifest = json.loads(_safe_file(source, MANIFEST).read_text(encoding="utf-8"))
        except Exception as exc:
            raise WorkspaceError(f"corrupt backup manifest: {exc}") from exc
        if (
            not isinstance(manifest, dict)
            or manifest.get("format") != 1
            or manifest.get("mode") != mode
            or manifest.get("database_sha256") != hashlib.sha256(database.read_bytes()).hexdigest()
        ):
            raise WorkspaceError("backup manifest or database hash mismatch")
    # Exclusive private directory, never reuse a live state directory or a failed copy.
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    target_db = destination / WORKSPACE_FILE
    target_db.touch(mode=0o600, exist_ok=False)
    with closing(_readonly(database)) as original, closing(sqlite3.connect(target_db)) as copied:
        original.execute("BEGIN")
        _check(original, mode)
        original.backup(copied)
        original.rollback()
        # Portable offline copies contain no WAL sidecars; source mode is untouched.
        copied.execute("PRAGMA journal_mode=DELETE")
        _check(copied, mode)
        references = copied.execute(
            "SELECT snapshot_path,snapshot_sha256 FROM screen_runs ORDER BY snapshot_path"
        ).fetchall()
        expected = dict(references)
        if restore and manifest is not None and manifest.get("snapshots") != expected:
            raise WorkspaceError("backup snapshot manifest mismatch")
        for relative, digest in references:
            if not relative.startswith("snapshots/"):
                raise WorkspaceError("snapshot must be inside snapshots directory")
            raw = _safe_file(source, relative).read_bytes()
            if hashlib.sha256(raw).hexdigest() != digest:
                raise WorkspaceError("snapshot hash mismatch")
            target = destination / relative
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with target.open("xb") as output:
                output.write(raw)
            os.chmod(target, 0o600)
        interrupted = 0
        if restore:
            now = utc_now()
            interrupted = copied.execute(
                "UPDATE update_jobs SET status='interrupted', phase='interrupted', "
                "updated_at=?, finished_at=?, result_run_id=NULL, "
                "error_code='RESTORED_OFFLINE', error_summary='从备份恢复，未完成任务不自动执行，请重新提交' "
                "WHERE status IN ('queued','running')",
                (now, now),
            ).rowcount
            copied.commit()
        _check(copied, mode)
    summary = {
        "format": 1,
        "mode": mode,
        "created_at": utc_now(),
        "database_sha256": hashlib.sha256(target_db.read_bytes()).hexdigest(),
        "snapshots": expected,
        "interrupted_jobs": interrupted,
    }
    (destination / MANIFEST).write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
    os.chmod(destination / MANIFEST, 0o600)
    # Flush all output before publishing the marker that makes this a workspace.
    for path in [target_db, destination / MANIFEST, *(destination / p for p in expected)]:
        with path.open("rb") as file:
            os.fsync(file.fileno())
    with (destination / MODE_FILE).open("x", encoding="ascii") as file:
        file.write(mode + "\n")
        file.flush()
        os.fsync(file.fileno())
    os.chmod(destination / MODE_FILE, 0o600)
    for directory in sorted(
        {destination, *((destination / p).parent for p in expected)},
        key=lambda p: len(p.parts),
        reverse=True,
    ):
        try:
            fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass
    return summary
