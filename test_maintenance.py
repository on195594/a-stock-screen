"""Offline restore drills never start a worker or access production data."""

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from auth import create_demo_actor
from maintenance import MANIFEST, copy_workspace
from services import mark_seen, request_watch_update, save_watch
from workspace import (
    MODE_FILE,
    WORKSPACE_FILE,
    WorkspaceError,
    connect_workspace,
    import_snapshot,
    initialize,
)


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    initialize(root, "demo", journal_mode="DELETE")
    run = import_snapshot(root, Path("tests/fixtures/peer_complete_v1.json"), "demo")
    actor = create_demo_actor()
    saved = save_watch(
        actor,
        "600001.SH",
        run,
        {"reason": "synthetic private note", "next_check": "check cashflow"},
        0,
        root,
        "demo",
    )
    mark_seen(actor, "600001.SH", run, saved["revision"], root, "demo")
    request_watch_update(actor, "queued", root, "demo")
    return root


@pytest.mark.parametrize("status", ["queued", "running"])
@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
def test_backup_restore_preserves_notes_ack_and_interrupts_only_restored_jobs(
    source, tmp_path, status, journal
):
    with sqlite3.connect(source / WORKSPACE_FILE) as conn:
        conn.execute(f"PRAGMA journal_mode={journal}")
        if status == "running":
            conn.execute("UPDATE update_jobs SET status='running', started_at=requested_at")
    backup, restored = tmp_path / "backup", tmp_path / "restored"
    result = copy_workspace(source, backup, "demo")
    assert len(result["snapshots"]) == 1
    original_hash = hashlib.sha256((source / WORKSPACE_FILE).read_bytes()).hexdigest()
    result = copy_workspace(backup, restored, "demo", restore=True)
    assert result["interrupted_jobs"] == 1
    assert hashlib.sha256((source / WORKSPACE_FILE).read_bytes()).hexdigest() == original_hash
    with connect_workspace(source, "demo") as src, connect_workspace(restored, "demo") as dst:
        assert tuple(src.execute("SELECT * FROM watch_items").fetchone()) == tuple(
            dst.execute("SELECT * FROM watch_items").fetchone()
        )
        assert src.execute("SELECT status FROM update_jobs").fetchone()[0] == status
        assert dst.execute("SELECT status, error_code FROM update_jobs").fetchone()[:] == (
            "interrupted",
            "RESTORED_OFFLINE",
        )
        assert dst.execute("PRAGMA foreign_key_check").fetchall() == []
    with sqlite3.connect(backup / WORKSPACE_FILE) as conn:
        assert conn.execute("SELECT status FROM update_jobs").fetchone()[0] == status
    with pytest.raises(FileExistsError):
        copy_workspace(source, backup, "demo")


@pytest.mark.parametrize("problem", ["mode", "schema", "hash", "missing", "symlink", "traversal"])
def test_backup_rejects_invalid_source(source, tmp_path, problem):
    dest = tmp_path / "candidate"
    with connect_workspace(source, "demo") as conn:
        relative = conn.execute("SELECT snapshot_path FROM screen_runs").fetchone()[0]
    file = source / relative
    if problem == "mode":
        (source / MODE_FILE).write_text("production")
    elif problem == "schema":
        with sqlite3.connect(source / WORKSPACE_FILE) as conn:
            conn.execute("PRAGMA user_version=99")
    elif problem == "hash":
        file.write_bytes(b"corrupt")
    elif problem == "missing":
        file.unlink()
    elif problem == "symlink":
        saved = tmp_path / "outside.json"
        file.rename(saved)
        file.symlink_to(saved)
    elif problem == "traversal":
        with sqlite3.connect(source / WORKSPACE_FILE) as conn:
            conn.execute("UPDATE screen_runs SET snapshot_path='../outside.json'")
    with pytest.raises(WorkspaceError):
        copy_workspace(source, dest, "demo")
    assert not (dest / MODE_FILE).exists()


def test_restore_rejects_changed_database_and_nested_destination(source, tmp_path):
    backup = tmp_path / "backup"
    copy_workspace(source, backup, "demo")
    with sqlite3.connect(backup / WORKSPACE_FILE) as conn:
        conn.execute("UPDATE watch_items SET reason='changed'")
    with pytest.raises(WorkspaceError, match="hash mismatch"):
        copy_workspace(backup, tmp_path / "restore", "demo", restore=True)
    with pytest.raises(WorkspaceError, match="separate directories"):
        copy_workspace(source, source / "backup", "demo")


def test_backup_rejects_public_assets_destination(source, tmp_path, monkeypatch):
    import maintenance

    monkeypatch.setattr(maintenance, "__file__", str(tmp_path / "maintenance.py"))
    with pytest.raises(WorkspaceError, match="static assets"):
        copy_workspace(source, tmp_path / "assets" / "private", "demo")
    assert not (tmp_path / "assets").exists()


def test_backup_copies_references_from_backup_database(source, tmp_path, monkeypatch):
    import maintenance

    original = maintenance._safe_file
    added = False

    def concurrent_run(root, relative):
        nonlocal added
        if relative.startswith("snapshots/") and not added:
            added = True
            import_snapshot(source, Path("tests/fixtures/peer_second_change.json"), "demo")
        return original(root, relative)

    monkeypatch.setattr(maintenance, "_safe_file", concurrent_run)
    result = copy_workspace(source, tmp_path / "backup", "demo")
    assert added and len(result["snapshots"]) == 1
    assert len(json.loads((tmp_path / "backup" / MANIFEST).read_text())["snapshots"]) == 1
    with connect_workspace(source, "demo") as conn:
        assert conn.execute("SELECT count(*) FROM screen_runs").fetchone()[0] == 2


def test_restore_rejects_corrupted_or_mismatched_manifest(source, tmp_path):
    backup = tmp_path / "backup"
    copy_workspace(source, backup, "demo")

    # Missing manifest
    manifest_file = backup / MANIFEST
    manifest_raw = manifest_file.read_text(encoding="utf-8")
    manifest_file.unlink()
    with pytest.raises(WorkspaceError, match="missing or outside"):
        copy_workspace(backup, tmp_path / "restore_missing", "demo", restore=True)

    # Corrupt JSON manifest
    manifest_file.write_text("invalid json", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="corrupt backup manifest"):
        copy_workspace(backup, tmp_path / "restore_corrupt", "demo", restore=True)

    # Mismatched snapshots dict
    bad_manifest = json.loads(manifest_raw)
    bad_manifest["snapshots"] = {"snapshots/nonexistent.json": "a" * 64}
    manifest_file.write_text(json.dumps(bad_manifest), encoding="utf-8")
    with pytest.raises(WorkspaceError, match="manifest mismatch"):
        copy_workspace(backup, tmp_path / "restore_mismatch", "demo", restore=True)


def test_manage_backup_and_restore_cli(source, tmp_path, monkeypatch, capsys):
    import sys

    import manage

    backup_dir = tmp_path / "cli_backup"
    restore_dir = tmp_path / "cli_restore"

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "manage.py",
            "backup",
            "--source",
            str(source),
            "--destination",
            str(backup_dir),
            "--mode",
            "demo",
        ],
    )
    assert manage.main() == 0
    captured = capsys.readouterr()
    assert "backup verified" in captured.out

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "manage.py",
            "restore",
            "--source",
            str(backup_dir),
            "--destination",
            str(restore_dir),
            "--mode",
            "demo",
        ],
    )
    assert manage.main() == 0
    captured = capsys.readouterr()
    assert "restore verified" in captured.out
