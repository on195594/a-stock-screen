"""Tests for workspace storage, snapshot imports, and personal research state."""

import json
from pathlib import Path

import pytest

import screen
from workspace import (
    WorkspaceError,
    add_watch_item,
    connect_workspace,
    get_latest_peer_run,
    get_run,
    get_watch_item,
    import_snapshot,
    initialize,
    is_row_usable,
    list_runs,
    mark_watch_ack,
    register_run,
    save_watch_item,
)

FIXTURES_DIR = Path(__file__).parent / "tests" / "fixtures"


def present(value: dict | None) -> dict:
    assert value is not None
    return value


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("year", 2023),
        ("ann_date", ""),
        ("ann_date", "2024-01-01"),
        ("ann_date", "2024-02-30"),
        ("end_date", "20241130"),
        ("period", "20231130"),
    ],
)
def test_row_rejects_invalid_annual_periods_and_announcements(field: str, value: object) -> None:
    data = json.loads((FIXTURES_DIR / "peer_complete_v1.json").read_text(encoding="utf-8"))
    row = data["rows"][0]
    assert is_row_usable(row)
    row["annual_roes"][1][field] = value
    assert not is_row_usable(row)


@pytest.mark.parametrize("field", ["roe", "roe_waa", "roe_mean"])
@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf"), True])
def test_nonfinite_roe_is_not_usable_or_verified(
    tmp_path: Path, field: str, bad_value: float | bool
) -> None:
    ws_dir = tmp_path / "ws"
    initialize(ws_dir, mode="demo", journal_mode="DELETE")
    data = json.loads((FIXTURES_DIR / "peer_complete_v1.json").read_text(encoding="utf-8"))
    # Exclude this row from ranking, so a forged ROE cannot be caught by ranking alone.
    row = data["rows"][3]
    row["name"] = "ST示例公司丁"
    row["exclusions"] = ["KNOWN_ST_WARNING"]
    data["results"] = screen.rank_peers(
        data["rows"], data["anchor"], [screen.base_code(c) for c in data["watchlist_codes"]]
    )
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with connect_workspace(ws_dir, "demo") as conn:
        assert (
            present(get_run(conn, import_snapshot(ws_dir, baseline, "demo")))["health"]
            == "complete"
        )

    if field == "roe_mean":
        row[field] = bad_value
    else:
        if field == "roe":
            row["annual_roes"][0]["roe_waa"] = row["annual_roes"][0]["roe"]
        row["annual_roes"][0][field] = bad_value
    assert not is_row_usable(row)
    path = tmp_path / "invalid_roe.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with connect_workspace(ws_dir, "demo") as conn:
        assert (
            present(get_run(conn, import_snapshot(ws_dir, path, "demo")))["health"] == "unverified"
        )


@pytest.mark.parametrize("pb,tagged", [(2.0, True), (None, False), (0.0, False), (-1.0, False)])
def test_import_rejects_inconsistent_invalid_pb(
    tmp_path: Path, pb: float | None, tagged: bool
) -> None:
    ws_dir = tmp_path / "ws"
    initialize(ws_dir, mode="demo", journal_mode="DELETE")
    data = json.loads((FIXTURES_DIR / "peer_complete_v1.json").read_text(encoding="utf-8"))
    row = data["rows"][3]
    row["pb"] = pb
    row["exclusions"] = ["INVALID_PB"] if tagged else []
    row["facts_usable"] = pb is not None
    data["results"] = screen.rank_peers(
        data["rows"], data["anchor"], [screen.base_code(c) for c in data["watchlist_codes"]]
    )
    path = tmp_path / "bad_pb.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with connect_workspace(ws_dir, "demo") as conn:
        assert (
            present(get_run(conn, import_snapshot(ws_dir, path, "demo")))["health"] == "unverified"
        )


@pytest.mark.parametrize("pb", [float("nan"), float("inf"), float("-inf"), True, "bad"])
def test_import_rejects_invalid_pb_without_facts_usable(
    tmp_path: Path, pb: float | bool | str
) -> None:
    ws_dir = tmp_path / "ws"
    initialize(ws_dir, mode="demo", journal_mode="DELETE")
    data = json.loads((FIXTURES_DIR / "peer_complete_v1.json").read_text(encoding="utf-8"))
    row = data["rows"][3]
    row["name"] = "ST示例公司丁"
    row["exclusions"] = ["KNOWN_ST_WARNING"]
    row.pop("facts_usable")
    data["results"] = screen.rank_peers(
        data["rows"], data["anchor"], [screen.base_code(c) for c in data["watchlist_codes"]]
    )
    row["pb"] = pb
    path = tmp_path / "invalid_pb.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with connect_workspace(ws_dir, "demo") as conn:
        assert (
            present(get_run(conn, import_snapshot(ws_dir, path, "demo")))["health"] == "unverified"
        )


def test_initialize_and_idempotence(tmp_path: Path) -> None:
    demo_dir = tmp_path / "demo_ws"
    db_file = initialize(demo_dir, mode="demo", journal_mode="DELETE")
    assert db_file.exists()
    assert (demo_dir / ".workspace-mode").read_text(encoding="ascii").strip() == "demo"

    # Idempotent: repeated initialization succeeds without re-creating tables
    db_file_2 = initialize(demo_dir, mode="demo", journal_mode="DELETE")
    assert db_file == db_file_2


def test_mode_mismatch_rejected(tmp_path: Path) -> None:
    demo_dir = tmp_path / "mode_ws"
    initialize(demo_dir, mode="demo", journal_mode="DELETE")

    with pytest.raises(WorkspaceError, match="workspace mode mismatch"):
        connect_workspace(demo_dir, mode="production")


def test_missing_mode_marker_rejected(tmp_path: Path) -> None:
    ws_dir = tmp_path / "no_marker"
    initialize(ws_dir, mode="demo", journal_mode="DELETE")
    (ws_dir / ".workspace-mode").unlink()

    with pytest.raises(WorkspaceError, match="existing workspace is missing its mode marker"):
        initialize(ws_dir, mode="demo", journal_mode="DELETE")


def test_watch_item_crud_and_revision_lock(tmp_path: Path) -> None:
    ws_dir = tmp_path / "watch_ws"
    initialize(ws_dir, mode="demo", journal_mode="DELETE")

    fixture_file = FIXTURES_DIR / "peer_complete_v1.json"
    run_id = import_snapshot(ws_dir, fixture_file, mode="demo")

    conn = connect_workspace(ws_dir, mode="demo")
    try:
        # Add item
        added = add_watch_item(conn, code="600001.SH", name="示例公司甲", added_run_id=run_id)
        assert added is True

        # Duplicate add returns False without changing data
        added_again = add_watch_item(
            conn, code="600001.SH", name="示例公司甲-新名称", added_run_id=run_id
        )
        assert added_again is False

        item = get_watch_item(conn, "600001.SH")
        assert item is not None
        assert item["name"] == "示例公司甲"
        assert item["status"] == "observe"
        assert item["revision"] == 1

        # Save with valid revision
        rev2 = save_watch_item(
            conn,
            code="600001.SH",
            expected_revision=1,
            status="research",
            reason="核查ROE持续性",
            next_check="2026年三季报披露后",
            note_url="https://example.com/notes/600001",
        )
        assert rev2 == 2

        # Revision conflict
        with pytest.raises(WorkspaceError, match="record missing or revision conflict"):
            save_watch_item(
                conn,
                code="600001.SH",
                expected_revision=1,
                status="paused",
                reason="conflict",
            )

        # Non-https URL rejected
        with pytest.raises(WorkspaceError, match="note URL must use https"):
            save_watch_item(
                conn,
                code="600001.SH",
                expected_revision=2,
                status="research",
                reason="test",
                note_url="http://insecure.com",
            )

        # URL with credentials rejected
        with pytest.raises(WorkspaceError, match="cannot contain credentials"):
            save_watch_item(
                conn,
                code="600001.SH",
                expected_revision=2,
                status="research",
                reason="test",
                note_url="https://user:password@example.com/notes",
            )

        # Text length validation
        with pytest.raises(WorkspaceError, match="personal text is too long"):
            save_watch_item(
                conn,
                code="600001.SH",
                expected_revision=2,
                status="research",
                reason="x" * 1001,
            )
    finally:
        conn.close()


def test_snapshot_import_and_deduplication(tmp_path: Path) -> None:
    ws_dir = tmp_path / "import_ws"
    initialize(ws_dir, mode="demo", journal_mode="DELETE")

    fixture_file = FIXTURES_DIR / "peer_complete_v1.json"
    run_id1 = import_snapshot(ws_dir, fixture_file, mode="demo")
    assert run_id1.startswith("peer_600001_SH_")

    # Import same snapshot again -> deduplicated, returns same run_id
    run_id2 = import_snapshot(ws_dir, fixture_file, mode="demo")
    assert run_id1 == run_id2

    conn = connect_workspace(ws_dir, mode="demo")
    try:
        run = get_run(conn, run_id1)
        assert run is not None
        assert run["health"] == "complete"
        assert run["anchor_code"] == "600001.SH"
        assert run["valuation_date"] == "2026-09-20"
        assert Path(ws_dir / run["snapshot_path"]).is_file()

        # Query latest peer run
        latest = get_latest_peer_run(conn, "600001.SH")
        assert latest is not None
        assert latest["run_id"] == run_id1

        runs = list_runs(conn, kind="peer")
        assert len(runs) == 1
    finally:
        conn.close()


def test_production_rejects_fixture_snapshot(tmp_path: Path) -> None:
    ws_dir = tmp_path / "prod_ws"
    initialize(ws_dir, mode="production", journal_mode="DELETE")

    fixture_file = FIXTURES_DIR / "peer_complete_v1.json"
    with pytest.raises(WorkspaceError, match="production workspace rejects fixture data"):
        import_snapshot(ws_dir, fixture_file, mode="production")


@pytest.mark.parametrize(
    "source_key",
    ("valuation_source", "financial_source", "risk_source", "annual_roes"),
)
def test_production_rejects_row_fixture_source(tmp_path: Path, source_key: str) -> None:
    ws_dir = tmp_path / "prod_row_ws"
    initialize(ws_dir, mode="production", journal_mode="DELETE")
    snapshot = _make_valid_test_snapshot()
    if source_key == "annual_roes":
        snapshot["rows"][0]["annual_roes"][0]["source"] = "fixture"
    else:
        snapshot["rows"][0][source_key] = "fixture"
    path = tmp_path / "row_fixture.json"
    path.write_text(json.dumps(snapshot), encoding="utf-8")

    with pytest.raises(WorkspaceError, match="production workspace rejects fixture data"):
        import_snapshot(ws_dir, path, mode="production")


def test_unverified_rule_snapshot(tmp_path: Path) -> None:
    ws_dir = tmp_path / "unverified_ws"
    initialize(ws_dir, mode="demo", journal_mode="DELETE")

    fixture_file = FIXTURES_DIR / "peer_unverified_old_rule.json"
    run_id = import_snapshot(ws_dir, fixture_file, mode="demo")

    conn = connect_workspace(ws_dir, mode="demo")
    try:
        run = get_run(conn, run_id)
        assert run is not None
        assert run["health"] == "unverified"
        assert run["rule_id"] == "peer-screen-v0-legacy"

        assert get_latest_peer_run(conn, "600001.SH") is None
    finally:
        conn.close()


def test_mark_watch_ack_contract(tmp_path: Path) -> None:
    ws_dir = tmp_path / "ack_ws"
    initialize(ws_dir, mode="demo", journal_mode="DELETE")

    file_v1 = FIXTURES_DIR / "peer_complete_v1.json"
    file_v2 = FIXTURES_DIR / "peer_second_change.json"
    unverified_file = FIXTURES_DIR / "peer_unverified_old_rule.json"

    run_v1 = import_snapshot(ws_dir, file_v1, mode="demo")
    run_v2 = import_snapshot(ws_dir, file_v2, mode="demo")
    run_unverified = import_snapshot(ws_dir, unverified_file, mode="demo")

    conn = connect_workspace(ws_dir, mode="demo")
    try:
        add_watch_item(conn, code="600001.SH", name="示例公司甲", added_run_id=run_v1)

        # Unverified run cannot be acknowledged
        with pytest.raises(WorkspaceError, match="cannot acknowledge unverified run"):
            mark_watch_ack(
                conn,
                code="600001.SH",
                displayed_run_id=run_unverified,
                expected_revision=1,
                state_dir=ws_dir,
            )

        # Acknowledging complete run v1 succeeds
        rev2 = mark_watch_ack(
            conn, code="600001.SH", displayed_run_id=run_v1, expected_revision=1, state_dir=ws_dir
        )
        assert rev2 == 2
        # Lost response: retry with the original revision must return the current one.
        assert (
            mark_watch_ack(
                conn,
                code="600001.SH",
                displayed_run_id=run_v1,
                expected_revision=1,
                state_dir=ws_dir,
            )
            == rev2
        )
        assert present(get_watch_item(conn, "600001.SH"))["revision"] == rev2

        # Advance to newer run v2 succeeds (600001.SH has usable facts in v2)
        rev3 = mark_watch_ack(
            conn, code="600001.SH", displayed_run_id=run_v2, expected_revision=2, state_dir=ws_dir
        )
        assert rev3 == 3

        # Monotonic regression check: cannot acknowledge older run v1 after v2!
        with pytest.raises(
            WorkspaceError, match="cannot acknowledge an older run than current ack"
        ):
            mark_watch_ack(
                conn,
                code="600001.SH",
                displayed_run_id=run_v1,
                expected_revision=3,
                state_dir=ws_dir,
            )

        # In run_v2, stock 600003.SH has financial_status == 'failed' (facts_usable == False)
        add_watch_item(conn, code="600003.SH", name="示例公司丙", added_run_id=run_v1)
        with pytest.raises(WorkspaceError, match="does not have usable facts"):
            mark_watch_ack(
                conn,
                code="600003.SH",
                displayed_run_id=run_v2,
                expected_revision=1,
                state_dir=ws_dir,
            )
    finally:
        conn.close()


@pytest.mark.parametrize("newer_state", ["gap", "corrupt", "regressed"])
def test_ack_blocks_same_capture_newer_run_with_gap(tmp_path: Path, newer_state: str) -> None:
    initialize(tmp_path, "demo", journal_mode="DELETE")
    data = json.loads((FIXTURES_DIR / "peer_complete_v1.json").read_text(encoding="utf-8"))
    row = next(r for r in data["rows"] if r["code"] == "600001.SH")
    assert is_row_usable(row)
    with connect_workspace(tmp_path, "demo") as conn:
        for run_id in ("run_a", "run_b"):
            candidate = dict(row)
            if run_id == "run_b" and newer_state == "gap":
                candidate["pb"] = None
            raw = json.dumps({"rows": [candidate], "run_id": run_id}).encode()
            rel_path = f"snapshots/{run_id}.json"
            (tmp_path / "snapshots").mkdir(exist_ok=True)
            (tmp_path / rel_path).write_bytes(raw)
            register_run(
                conn,
                run_id=run_id,
                kind="peer",
                anchor_code="600001.SH",
                rule_id="peer-screen-v1",
                captured_at="2026-09-21T16:00:00+08:00",
                valuation_date="2026-09-19"
                if run_id == "run_b" and newer_state == "regressed"
                else "2026-09-20",
                health="partial" if newer_state == "gap" and run_id == "run_b" else "complete",
                snapshot_path=rel_path,
                snapshot_bytes=raw,
            )
        assert [r["run_id"] for r in list_runs(conn)] == ["run_b", "run_a"]
        assert [r["run_id"] for r in list_runs(conn, kind="peer")] == ["run_b", "run_a"]
        add_watch_item(conn, code="600001.SH", name="测试", added_run_id="run_a")
        if newer_state == "corrupt":
            (tmp_path / "snapshots/run_b.json").write_bytes(b"corrupt")
        with pytest.raises(
            WorkspaceError, match="cannot acknowledge stale run when newer run has data gap"
        ):
            mark_watch_ack(
                conn,
                code="600001.SH",
                displayed_run_id="run_a",
                expected_revision=1,
                state_dir=tmp_path,
            )
        assert present(get_watch_item(conn, "600001.SH"))["ack_run_id"] is None


@pytest.mark.parametrize("gap", ["unusable", "missing", "regressed"])
def test_ack_blocks_newer_other_anchor_covering_stock(tmp_path: Path, gap: str) -> None:
    initialize(tmp_path, "demo", journal_mode="DELETE")
    data = json.loads((FIXTURES_DIR / "peer_complete_v1.json").read_text(encoding="utf-8"))
    row = next(r for r in data["rows"] if r["code"] == "600001.SH")
    with connect_workspace(tmp_path, "demo") as conn:
        for run_id in ("run_1", "run_2"):
            newer = run_id == "run_2"
            candidate = dict(row)
            if newer and gap == "unusable":
                candidate["financial_status"] = "failed"
                candidate["pb"] = None
            raw = json.dumps(
                {
                    "anchor": "600002.SH" if newer else "600001.SH",
                    "scope": {"selected_codes": ["600001.SH"]},
                    "rows": [] if newer and gap == "missing" else [candidate],
                }
            ).encode()
            rel_path = f"snapshots/{run_id}.json"
            (tmp_path / "snapshots").mkdir(exist_ok=True)
            (tmp_path / rel_path).write_bytes(raw)
            register_run(
                conn,
                run_id=run_id,
                kind="peer",
                anchor_code="600002.SH" if newer else "600001.SH",
                rule_id="peer-screen-v1",
                captured_at=f"2026-09-{21 if newer else 20}T16:00:00+08:00",
                valuation_date="2026-09-19" if newer and gap == "regressed" else "2026-09-20",
                health="partial" if newer and gap != "regressed" else "complete",
                snapshot_path=rel_path,
                snapshot_bytes=raw,
            )
        add_watch_item(conn, code="600001.SH", name="标的甲", added_run_id="run_1")
        with pytest.raises(
            WorkspaceError, match="cannot acknowledge stale run when newer run has data gap"
        ):
            mark_watch_ack(
                conn,
                code="600001.SH",
                displayed_run_id="run_1",
                expected_revision=1,
                state_dir=tmp_path,
            )
        assert present(get_watch_item(conn, "600001.SH"))["ack_run_id"] is None


@pytest.mark.parametrize("ack_baseline", [False, True])
def test_ack_rejects_regressed_date_against_current_ack(tmp_path: Path, ack_baseline: bool) -> None:
    initialize(tmp_path, "demo", journal_mode="DELETE")
    data = json.loads((FIXTURES_DIR / "peer_complete_v1.json").read_text(encoding="utf-8"))
    row = next(r for r in data["rows"] if r["code"] == "600001.SH")
    with connect_workspace(tmp_path, "demo") as conn:
        raw = json.dumps({"rows": [row]}).encode()
        (tmp_path / "snapshots").mkdir(exist_ok=True)
        (tmp_path / "snapshots/run_a.json").write_bytes(raw)
        register_run(
            conn,
            run_id="run_a",
            kind="peer",
            anchor_code="600001.SH",
            rule_id="peer-screen-v1",
            captured_at="2026-09-20T16:00:00+08:00",
            valuation_date="2026-09-20",
            health="complete",
            snapshot_path="snapshots/run_a.json",
            snapshot_bytes=raw,
        )
        add_watch_item(conn, code="600001.SH", name="测试", added_run_id="run_a")
        rev = (
            mark_watch_ack(
                conn,
                code="600001.SH",
                displayed_run_id="run_a",
                expected_revision=1,
                state_dir=tmp_path,
            )
            if ack_baseline
            else 1
        )
        regressed = json.dumps({"rows": [{**row, "valuation_date": "2026-09-19"}]}).encode()
        (tmp_path / "snapshots/run_b.json").write_bytes(regressed)
        register_run(
            conn,
            run_id="run_b",
            kind="peer",
            anchor_code="600001.SH",
            rule_id="peer-screen-v1",
            captured_at="2026-09-21T16:00:00+08:00",
            valuation_date="2026-09-19",
            health="complete",
            snapshot_path="snapshots/run_b.json",
            snapshot_bytes=regressed,
        )
        with pytest.raises(WorkspaceError, match="regressed valuation date"):
            mark_watch_ack(
                conn,
                code="600001.SH",
                displayed_run_id="run_b",
                expected_revision=rev,
                state_dir=tmp_path,
            )
        assert present(get_watch_item(conn, "600001.SH"))["ack_run_id"] == (
            "run_a" if ack_baseline else None
        )


def test_ack_ignores_newer_unrelated_peer_run(tmp_path: Path) -> None:
    ws_dir = tmp_path / "unrelated_ack_ws"
    initialize(ws_dir, mode="demo", journal_mode="DELETE")
    first = _make_valid_test_snapshot()
    first_file = tmp_path / "first.json"
    first_file.write_text(json.dumps(first, ensure_ascii=False), encoding="utf-8")
    first_id = import_snapshot(ws_dir, first_file, mode="demo")

    # Same valid discovery shape, but a different industry's codes and later capture time.
    second = json.loads(
        json.dumps(first).replace("600001.SH", "601001.SH").replace("600002.SH", "601002.SH")
    )
    second["scope"]["industry"] = "医药制造"
    second["watchlist_codes"] = ["601001"]
    second["screened_at"] = second["generated_at"] = "2026-09-21T16:00:00+08:00"
    second_file = tmp_path / "second.json"
    second_file.write_text(json.dumps(second, ensure_ascii=False), encoding="utf-8")
    second_id = import_snapshot(ws_dir, second_file, mode="demo")

    conn = connect_workspace(ws_dir, mode="demo")
    try:
        assert present(get_run(conn, first_id))["health"] == "complete"
        assert present(get_run(conn, second_id))["health"] == "complete"
        add_watch_item(conn, code="600001.SH", name="标的甲", added_run_id=first_id)
        assert (
            mark_watch_ack(
                conn,
                code="600001.SH",
                displayed_run_id=first_id,
                expected_revision=1,
                state_dir=ws_dir,
            )
            == 2
        )
        assert present(get_watch_item(conn, "600001.SH"))["ack_run_id"] == first_id
    finally:
        conn.close()


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_ack_ignores_damaged_unrelated_peer_and_rejects_related_one(
    tmp_path: Path, damage: str
) -> None:
    initialize(tmp_path, mode="demo", journal_mode="DELETE")
    first = import_snapshot(tmp_path, FIXTURES_DIR / "peer_complete_v1.json", "demo")
    other = tmp_path / "snapshots" / "other_ack.json"
    raw = b"{}"
    other.write_bytes(raw)
    with connect_workspace(tmp_path, "demo") as conn:
        add_watch_item(conn, code="600002.SH", name="同业", added_run_id=first)
        register_run(
            conn,
            run_id="other_ack",
            kind="peer",
            anchor_code="601001.SH",
            rule_id="peer-screen-v1",
            captured_at="2026-09-21T16:00:00+08:00",
            valuation_date="2026-09-21",
            health="partial",
            snapshot_path="snapshots/other_ack.json",
            snapshot_bytes=raw,
        )
        conn.commit()
        if damage == "missing":
            other.unlink()
        else:
            other.write_bytes(b"corrupt")
        assert (
            mark_watch_ack(
                conn,
                code="600002.SH",
                displayed_run_id=first,
                expected_revision=1,
                state_dir=tmp_path,
            )
            == 2
        )
        register_run(
            conn,
            run_id="related_ack",
            kind="peer",
            anchor_code="600001.SH",
            rule_id="peer-screen-v1",
            captured_at="2026-09-22T16:00:00+08:00",
            valuation_date="2026-09-22",
            health="partial",
            snapshot_path="snapshots/related_ack.json",
            snapshot_bytes=b"[]",
        )
        add_watch_item(conn, code="600001.SH", name="参照", added_run_id=first)
        with pytest.raises(WorkspaceError, match="newer run has data gap"):
            mark_watch_ack(
                conn,
                code="600001.SH",
                displayed_run_id=first,
                expected_revision=1,
                state_dir=tmp_path,
            )


def test_ack_read_permission_error_is_workspace_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initialize(tmp_path, mode="demo", journal_mode="DELETE")
    first = import_snapshot(tmp_path, FIXTURES_DIR / "peer_complete_v1.json", "demo")
    with connect_workspace(tmp_path, "demo") as conn:
        add_watch_item(conn, code="600001.SH", name="参照", added_run_id=first)
        path = tmp_path / present(get_run(conn, first))["snapshot_path"]
        original_read = Path.read_bytes

        def unreadable(self: Path) -> bytes:
            if self == path:
                raise PermissionError("read denied")
            return original_read(self)

        monkeypatch.setattr(Path, "read_bytes", unreadable)
        with pytest.raises(WorkspaceError, match="failed to read snapshot file.*read denied"):
            mark_watch_ack(
                conn,
                code="600001.SH",
                displayed_run_id=first,
                expected_revision=1,
                state_dir=tmp_path,
            )
        assert present(get_watch_item(conn, "600001.SH"))["ack_run_id"] is None


def test_ack_ignores_newer_peer_excluded_by_cap(tmp_path: Path) -> None:
    ws_dir = tmp_path / "cap_ack_ws"
    initialize(ws_dir, mode="demo", journal_mode="DELETE")
    first = _make_valid_test_snapshot()
    first_file = tmp_path / "first.json"
    first_file.write_text(json.dumps(first, ensure_ascii=False), encoding="utf-8")
    first_id = import_snapshot(ws_dir, first_file, "demo")

    # A later complete peer scan selects 50 peers but caps out the watched stock.
    second = json.loads(json.dumps(first))
    second["screened_at"] = second["generated_at"] = "2026-09-21T16:00:00+08:00"
    second["rows"] = [second["rows"][0]]
    for i in range(1, 50):
        row = json.loads(json.dumps(second["rows"][0]))
        row["code"] = f"610{i:03d}.SH"
        second["rows"].append(row)
    selected = [row["code"] for row in second["rows"]]
    second["scope"].update(
        enumerated_count=51,
        total_candidates=51,
        enumerated_codes=[*selected, "600002.SH"],
        selected_codes=selected,
        excluded_by_cap=["600002.SH"],
        watchlist_codes=["600002.SH"],
    )
    second["watchlist_codes"] = ["600002"]
    second["results"] = screen.rank_peers(second["rows"], second["anchor"], ["600002"])
    second_file = tmp_path / "second.json"
    second_file.write_text(json.dumps(second, ensure_ascii=False), encoding="utf-8")
    second_id = import_snapshot(ws_dir, second_file, "demo")

    conn = connect_workspace(ws_dir, "demo")
    try:
        assert present(get_run(conn, second_id))["health"] == "complete"
        add_watch_item(conn, code="600002.SH", name="标的乙", added_run_id=first_id)
        assert (
            mark_watch_ack(
                conn,
                code="600002.SH",
                displayed_run_id=first_id,
                expected_revision=1,
                state_dir=ws_dir,
            )
            == 2
        )
        assert present(get_watch_item(conn, "600002.SH"))["ack_run_id"] == first_id
    finally:
        conn.close()


def test_native_screen_snapshot_import_complete(tmp_path: Path) -> None:
    ws_dir = tmp_path / "native_screen_ws"
    initialize(ws_dir, mode="demo", journal_mode="DELETE")

    anchor = "600001.SH"
    watchlist = [screen.base_code(anchor)]
    rows = [
        {
            "code": "600001.SH",
            "name": "真实标的甲",
            "industry": "专用设备",
            "pb": 2.10,
            "total_mv": 1500000.0,
            "valuation_date": "2026-09-20",
            "valuation_source": "tushare.daily_basic",
            "financial_source": "tracker",
            "risk_source": "tracker",
            "annual_roes": [
                {
                    "period": "20231231",
                    "roe_waa": 12.0,
                    "source": "tracker",
                    "ann_date": "2024-04-15",
                    "update_flag": "0",
                },
                {
                    "period": "20241231",
                    "roe_waa": 14.0,
                    "source": "tracker",
                    "ann_date": "2025-04-20",
                    "update_flag": "0",
                },
                {
                    "period": "20251231",
                    "roe_waa": 16.0,
                    "source": "tracker",
                    "ann_date": "2026-04-15",
                    "update_flag": "0",
                },
            ],
            "roe_mean": 14.0,
            "exclusions": [],
        },
        {
            "code": "600002.SH",
            "name": "真实标的乙",
            "industry": "专用设备",
            "pb": 1.50,
            "total_mv": 2000000.0,
            "valuation_date": "2026-09-20",
            "valuation_source": "tushare.daily_basic",
            "financial_source": "tracker",
            "risk_source": "tracker",
            "annual_roes": [
                {
                    "period": "20231231",
                    "roe_waa": 15.0,
                    "source": "tracker",
                    "ann_date": "2024-04-10",
                    "update_flag": "0",
                },
                {
                    "period": "20241231",
                    "roe_waa": 15.0,
                    "source": "tracker",
                    "ann_date": "2025-04-12",
                    "update_flag": "0",
                },
                {
                    "period": "20251231",
                    "roe_waa": 15.0,
                    "source": "tracker",
                    "ann_date": "2026-04-15",
                    "update_flag": "0",
                },
            ],
            "roe_mean": 15.0,
            "exclusions": [],
        },
    ]

    results = screen.rank_peers(rows, anchor, watchlist)
    snapshot_data = {
        "schema_version": 1,
        "rule": "peer-screen-v1",
        "limits": {"cap": 50, "top_n": 3, "max_report_age_days": 550},
        "formula": "research_order=(roe_rank_desc+pb_rank_asc)/2; average ties",
        "screened_at": "2026-09-20T16:00:00+08:00",
        "generated_at": "2026-09-20T16:00:00+08:00",
        "data_date": "2026-09-20",
        "anchor": anchor,
        "watchlist_codes": watchlist,
        "source": "tracker",
        "scope": {
            "source": "tracker",
            "industry": "专用设备",
            "cap": 50,
            "enumerated_count": 2,
            "total_candidates": 2,
            "enumerated_codes": ["600001.SH", "600002.SH"],
            "selected_codes": ["600001.SH", "600002.SH"],
            "excluded_missing_valuation": [],
            "excluded_by_cap": [],
        },
        "rows": rows,
        "results": results,
    }

    snap_file = tmp_path / "native_screen_snapshot.json"
    snap_file.write_text(json.dumps(snapshot_data, ensure_ascii=False), encoding="utf-8")

    run_id = import_snapshot(ws_dir, snap_file, mode="demo")

    conn = connect_workspace(ws_dir, mode="demo")
    try:
        run = get_run(conn, run_id)
        assert run is not None
        assert run["health"] == "complete"
        assert run["anchor_code"] == anchor
        assert run["valuation_date"] == "2026-09-20"

        latest = get_latest_peer_run(conn, anchor)
        assert latest is not None
        assert latest["run_id"] == run_id

        # Verify native screen.py snapshot can be marked as acknowledged
        add_watch_item(conn, code="600001.SH", name="真实标的甲", added_run_id=run_id)
        rev2 = mark_watch_ack(
            conn,
            code="600001.SH",
            displayed_run_id=run_id,
            expected_revision=1,
            state_dir=ws_dir,
        )
        assert rev2 == 2

        # Verify get_company_context correctly extracts 4-digit years from period
        from auth import create_demo_actor
        from services import get_company_context

        ctx = get_company_context(
            create_demo_actor(), "600001.SH", ws_dir, mode="demo", include_personal_notes=True
        )
        assert ctx["has_usable_facts"] is True
        roes = ctx["usable_fact"]["annual_roes"]
        assert len(roes) == 3
        assert [r["year"] for r in roes] == ["2023", "2024", "2025"]
    finally:
        conn.close()


def test_tampered_ranking_metadata_degrades_to_unverified(tmp_path: Path) -> None:
    ws_dir = tmp_path / "tamper_meta_ws"
    initialize(ws_dir, mode="demo", journal_mode="DELETE")

    anchor = "600001.SH"
    watchlist = [screen.base_code(anchor)]
    rows = [
        {
            "code": "600001.SH",
            "name": "真实标的甲",
            "pb": 2.10,
            "annual_roes": [
                {
                    "period": "20231231",
                    "roe_waa": 12.0,
                    "source": "tracker",
                    "ann_date": "2024-04-15",
                    "update_flag": "0",
                },
                {
                    "period": "20241231",
                    "roe_waa": 14.0,
                    "source": "tracker",
                    "ann_date": "2025-04-20",
                    "update_flag": "0",
                },
                {
                    "period": "20251231",
                    "roe_waa": 16.0,
                    "source": "tracker",
                    "ann_date": "2026-04-15",
                    "update_flag": "0",
                },
            ],
            "roe_mean": 14.0,
            "exclusions": [],
        },
        {
            "code": "600002.SH",
            "name": "真实标的乙",
            "pb": 1.50,
            "annual_roes": [
                {
                    "period": "20231231",
                    "roe_waa": 15.0,
                    "source": "tracker",
                    "ann_date": "2024-04-10",
                    "update_flag": "0",
                },
                {
                    "period": "20241231",
                    "roe_waa": 15.0,
                    "source": "tracker",
                    "ann_date": "2025-04-12",
                    "update_flag": "0",
                },
                {
                    "period": "20251231",
                    "roe_waa": 15.0,
                    "source": "tracker",
                    "ann_date": "2026-04-15",
                    "update_flag": "0",
                },
            ],
            "roe_mean": 15.0,
            "exclusions": [],
        },
    ]

    results = screen.rank_peers(rows, anchor, watchlist)
    # Tamper with anchor_position: claim position 99 instead of true position
    tampered_results = dict(results)
    tampered_results["anchor_position"] = 99

    snapshot_data = {
        "schema_version": 1,
        "rule": "peer-screen-v1",
        "limits": {"cap": 50, "top_n": 3, "max_report_age_days": 550},
        "formula": "research_order=(roe_rank_desc+pb_rank_asc)/2; average ties",
        "screened_at": "2026-09-20T16:00:00+08:00",
        "generated_at": "2026-09-20T16:00:00+08:00",
        "data_date": "2026-09-20",
        "anchor": anchor,
        "watchlist_codes": watchlist,
        "source": "screen_test",
        "scope": {"source": "screen_test", "total_candidates": 2},
        "rows": rows,
        "results": tampered_results,
    }

    snap_file = tmp_path / "tampered_snapshot.json"
    snap_file.write_text(json.dumps(snapshot_data, ensure_ascii=False), encoding="utf-8")

    run_id = import_snapshot(ws_dir, snap_file, mode="demo")

    conn = connect_workspace(ws_dir, mode="demo")
    try:
        run = get_run(conn, run_id)
        assert run is not None
        assert run["health"] == "unverified"

        # Omitting required metadata key (e.g. 'top') degrades to unverified
        missing_key_results = dict(results)
        del missing_key_results["top"]
        snapshot_data["results"] = missing_key_results
        snap_file2 = tmp_path / "missing_top_snapshot.json"
        snap_file2.write_text(json.dumps(snapshot_data, ensure_ascii=False), encoding="utf-8")
        run_id2 = import_snapshot(ws_dir, snap_file2, mode="demo")
        run2 = get_run(conn, run_id2)
        assert run2 is not None
        assert run2["health"] == "unverified"
    finally:
        conn.close()


def test_results_rows_mismatch_degrades_to_unverified(tmp_path: Path) -> None:
    ws_dir = tmp_path / "mismatch_ws"
    initialize(ws_dir, mode="demo", journal_mode="DELETE")

    anchor = "600001.SH"
    watchlist = [screen.base_code(anchor)]
    rows = [
        {
            "code": "600001.SH",
            "name": "真实标的甲",
            "pb": 2.10,
            "annual_roes": [
                {
                    "period": "20231231",
                    "roe_waa": 12.0,
                    "source": "tracker",
                    "ann_date": "2024-04-15",
                    "update_flag": "0",
                },
                {
                    "period": "20241231",
                    "roe_waa": 14.0,
                    "source": "tracker",
                    "ann_date": "2025-04-20",
                    "update_flag": "0",
                },
                {
                    "period": "20251231",
                    "roe_waa": 16.0,
                    "source": "tracker",
                    "ann_date": "2026-04-15",
                    "update_flag": "0",
                },
            ],
            "roe_mean": 14.0,
            "exclusions": [],
        },
        {
            "code": "600002.SH",
            "name": "真实标的乙",
            "pb": 1.50,
            "annual_roes": [
                {
                    "period": "20231231",
                    "roe_waa": 15.0,
                    "source": "tracker",
                    "ann_date": "2024-04-10",
                    "update_flag": "0",
                },
                {
                    "period": "20241231",
                    "roe_waa": 15.0,
                    "source": "tracker",
                    "ann_date": "2025-04-12",
                    "update_flag": "0",
                },
                {
                    "period": "20251231",
                    "roe_waa": 15.0,
                    "source": "tracker",
                    "ann_date": "2026-04-15",
                    "update_flag": "0",
                },
            ],
            "roe_mean": 15.0,
            "exclusions": [],
        },
    ]

    results = dict(screen.rank_peers(rows, anchor, watchlist))
    # Inject an inconsistent results.rows that diverges from top-level rows
    results["rows"] = [
        {
            "code": "600001.SH",
            "name": "虚假标的甲",
            "pb": 99.99,
            "annual_roes": [],
            "roe_mean": 99.99,
            "exclusions": [],
        }
    ]

    snapshot_data = {
        "schema_version": 1,
        "rule": "peer-screen-v1",
        "limits": {"cap": 50, "top_n": 3, "max_report_age_days": 550},
        "formula": "research_order=(roe_rank_desc+pb_rank_asc)/2; average ties",
        "screened_at": "2026-09-20T16:00:00+08:00",
        "generated_at": "2026-09-20T16:00:00+08:00",
        "data_date": "2026-09-20",
        "anchor": anchor,
        "watchlist_codes": watchlist,
        "source": "screen_test",
        "scope": {"source": "screen_test", "total_candidates": 2},
        "rows": rows,
        "results": results,
    }

    snap_file = tmp_path / "mismatch_rows_snapshot.json"
    snap_file.write_text(json.dumps(snapshot_data, ensure_ascii=False), encoding="utf-8")

    run_id = import_snapshot(ws_dir, snap_file, mode="demo")

    conn = connect_workspace(ws_dir, mode="demo")
    try:
        run = get_run(conn, run_id)
        assert run is not None
        assert run["health"] == "unverified"
    finally:
        conn.close()


def test_unknown_or_missing_source_degrades_to_unverified(tmp_path: Path) -> None:
    ws_dir = tmp_path / "unknown_src_ws"
    initialize(ws_dir, mode="demo", journal_mode="DELETE")

    anchor = "600001.SH"
    watchlist = [screen.base_code(anchor)]
    rows = [
        {
            "code": "600001.SH",
            "name": "真实标的甲",
            "pb": 2.10,
            "annual_roes": [
                {
                    "period": "20231231",
                    "roe_waa": 12.0,
                    "source": "tracker",
                    "ann_date": "2024-04-15",
                    "update_flag": "0",
                },
                {
                    "period": "20241231",
                    "roe_waa": 14.0,
                    "source": "tracker",
                    "ann_date": "2025-04-20",
                    "update_flag": "0",
                },
                {
                    "period": "20251231",
                    "roe_waa": 16.0,
                    "source": "tracker",
                    "ann_date": "2026-04-15",
                    "update_flag": "0",
                },
            ],
            "roe_mean": 14.0,
            "exclusions": [],
        },
        {
            "code": "600002.SH",
            "name": "真实标的乙",
            "pb": 1.50,
            "annual_roes": [
                {
                    "period": "20231231",
                    "roe_waa": 15.0,
                    "source": "tracker",
                    "ann_date": "2024-04-10",
                    "update_flag": "0",
                },
                {
                    "period": "20241231",
                    "roe_waa": 15.0,
                    "source": "tracker",
                    "ann_date": "2025-04-12",
                    "update_flag": "0",
                },
                {
                    "period": "20251231",
                    "roe_waa": 15.0,
                    "source": "tracker",
                    "ann_date": "2026-04-15",
                    "update_flag": "0",
                },
            ],
            "roe_mean": 15.0,
            "exclusions": [],
        },
    ]

    results = screen.rank_peers(rows, anchor, watchlist)

    # 1. Missing source and missing scope source
    snapshot_unknown = {
        "schema_version": 1,
        "rule": "peer-screen-v1",
        "limits": {"cap": 50, "top_n": 3, "max_report_age_days": 550},
        "formula": "research_order=(roe_rank_desc+pb_rank_asc)/2; average ties",
        "screened_at": "2026-09-20T16:00:00+08:00",
        "generated_at": "2026-09-20T16:00:00+08:00",
        "data_date": "2026-09-20",
        "anchor": anchor,
        "watchlist_codes": watchlist,
        "rows": rows,
        "results": results,
    }

    snap_file = tmp_path / "unknown_src_snapshot.json"
    snap_file.write_text(json.dumps(snapshot_unknown, ensure_ascii=False), encoding="utf-8")

    run_id = import_snapshot(ws_dir, snap_file, mode="demo")

    conn = connect_workspace(ws_dir, mode="demo")
    try:
        run = get_run(conn, run_id)
        assert run is not None
        assert run["health"] == "unverified"

        # 2. Explicit 'unknown' source also degrades to unverified
        snapshot_unknown["source"] = "unknown"
        snapshot_unknown["scope"] = {"source": "unknown"}
        snap_file2 = tmp_path / "explicit_unknown_src_snapshot.json"
        snap_file2.write_text(json.dumps(snapshot_unknown, ensure_ascii=False), encoding="utf-8")
        run_id2 = import_snapshot(ws_dir, snap_file2, mode="demo")
        run2 = get_run(conn, run_id2)
        assert run2 is not None
        assert run2["health"] == "unverified"
    finally:
        conn.close()


def test_invalid_limits_degrades_to_unverified(tmp_path: Path) -> None:
    ws_dir = tmp_path / "invalid_limits_ws"
    initialize(ws_dir, mode="demo", journal_mode="DELETE")

    anchor = "600001.SH"
    watchlist = [screen.base_code(anchor)]
    rows = [
        {
            "code": "600001.SH",
            "name": "真实标的甲",
            "pb": 2.10,
            "annual_roes": [
                {
                    "period": "20231231",
                    "roe_waa": 12.0,
                    "source": "tracker",
                    "ann_date": "2024-04-15",
                    "update_flag": "0",
                },
                {
                    "period": "20241231",
                    "roe_waa": 14.0,
                    "source": "tracker",
                    "ann_date": "2025-04-20",
                    "update_flag": "0",
                },
                {
                    "period": "20251231",
                    "roe_waa": 16.0,
                    "source": "tracker",
                    "ann_date": "2026-04-15",
                    "update_flag": "0",
                },
            ],
            "roe_mean": 14.0,
            "exclusions": [],
        },
        {
            "code": "600002.SH",
            "name": "真实标的乙",
            "pb": 1.50,
            "annual_roes": [
                {
                    "period": "20231231",
                    "roe_waa": 15.0,
                    "source": "tracker",
                    "ann_date": "2024-04-10",
                    "update_flag": "0",
                },
                {
                    "period": "20241231",
                    "roe_waa": 15.0,
                    "source": "tracker",
                    "ann_date": "2025-04-12",
                    "update_flag": "0",
                },
                {
                    "period": "20251231",
                    "roe_waa": 15.0,
                    "source": "tracker",
                    "ann_date": "2026-04-15",
                    "update_flag": "0",
                },
            ],
            "roe_mean": 15.0,
            "exclusions": [],
        },
    ]

    results = screen.rank_peers(rows, anchor, watchlist)
    # Incorrect limits deviating from peer-screen-v1 standard (e.g. cap=100 instead of 50)
    snapshot_data = {
        "schema_version": 1,
        "rule": "peer-screen-v1",
        "limits": {"cap": 100, "top_n": 3, "max_report_age_days": 550},
        "formula": "research_order=(roe_rank_desc+pb_rank_asc)/2; average ties",
        "screened_at": "2026-09-20T16:00:00+08:00",
        "generated_at": "2026-09-20T16:00:00+08:00",
        "data_date": "2026-09-20",
        "anchor": anchor,
        "watchlist_codes": watchlist,
        "source": "screen_test",
        "scope": {"source": "screen_test", "total_candidates": 2},
        "rows": rows,
        "results": results,
    }

    snap_file = tmp_path / "invalid_limits.json"
    snap_file.write_text(json.dumps(snapshot_data, ensure_ascii=False), encoding="utf-8")

    run_id = import_snapshot(ws_dir, snap_file, mode="demo")

    conn = connect_workspace(ws_dir, mode="demo")
    try:
        run = get_run(conn, run_id)
        assert run is not None
        assert run["health"] == "unverified"
    finally:
        conn.close()


def test_missing_scope_evidence_degrades_to_unverified(tmp_path: Path) -> None:
    ws_dir = tmp_path / "missing_scope_ws"
    initialize(ws_dir, mode="demo", journal_mode="DELETE")

    anchor = "600001.SH"
    watchlist = [screen.base_code(anchor)]
    rows = [
        {
            "code": "600001.SH",
            "name": "真实标的甲",
            "pb": 2.10,
            "annual_roes": [
                {
                    "period": "20231231",
                    "roe_waa": 12.0,
                    "source": "tracker",
                    "ann_date": "2024-04-15",
                    "update_flag": "0",
                },
                {
                    "period": "20241231",
                    "roe_waa": 14.0,
                    "source": "tracker",
                    "ann_date": "2025-04-20",
                    "update_flag": "0",
                },
                {
                    "period": "20251231",
                    "roe_waa": 16.0,
                    "source": "tracker",
                    "ann_date": "2026-04-15",
                    "update_flag": "0",
                },
            ],
            "roe_mean": 14.0,
            "exclusions": [],
        },
        {
            "code": "600002.SH",
            "name": "真实标的乙",
            "pb": 1.50,
            "annual_roes": [
                {
                    "period": "20231231",
                    "roe_waa": 15.0,
                    "source": "tracker",
                    "ann_date": "2024-04-10",
                    "update_flag": "0",
                },
                {
                    "period": "20241231",
                    "roe_waa": 15.0,
                    "source": "tracker",
                    "ann_date": "2025-04-12",
                    "update_flag": "0",
                },
                {
                    "period": "20251231",
                    "roe_waa": 15.0,
                    "source": "tracker",
                    "ann_date": "2026-04-15",
                    "update_flag": "0",
                },
            ],
            "roe_mean": 15.0,
            "exclusions": [],
        },
    ]

    results = screen.rank_peers(rows, anchor, watchlist)
    # Scope without candidate counts cannot prove complete enumeration
    snapshot_data = {
        "schema_version": 1,
        "rule": "peer-screen-v1",
        "limits": {"cap": 50, "top_n": 3, "max_report_age_days": 550},
        "formula": "research_order=(roe_rank_desc+pb_rank_asc)/2; average ties",
        "screened_at": "2026-09-20T16:00:00+08:00",
        "generated_at": "2026-09-20T16:00:00+08:00",
        "data_date": "2026-09-20",
        "anchor": anchor,
        "watchlist_codes": watchlist,
        "source": "screen_test",
        "scope": {"source": "screen_test"},
        "rows": rows,
        "results": results,
    }

    snap_file = tmp_path / "missing_scope.json"
    snap_file.write_text(json.dumps(snapshot_data, ensure_ascii=False), encoding="utf-8")

    run_id = import_snapshot(ws_dir, snap_file, mode="demo")

    conn = connect_workspace(ws_dir, mode="demo")
    try:
        run = get_run(conn, run_id)
        assert run is not None
        assert run["health"] == "unverified"
    finally:
        conn.close()


def test_ranking_name_conflict_degrades_to_unverified(tmp_path: Path) -> None:
    ws_dir = tmp_path / "name_conflict_ws"
    initialize(ws_dir, mode="demo", journal_mode="DELETE")

    anchor = "600001.SH"
    watchlist = [screen.base_code(anchor)]
    rows = [
        {
            "code": "600001.SH",
            "name": "真实标的甲",
            "pb": 2.10,
            "annual_roes": [
                {
                    "period": "20231231",
                    "roe_waa": 12.0,
                    "source": "tracker",
                    "ann_date": "2024-04-15",
                    "update_flag": "0",
                },
                {
                    "period": "20241231",
                    "roe_waa": 14.0,
                    "source": "tracker",
                    "ann_date": "2025-04-20",
                    "update_flag": "0",
                },
                {
                    "period": "20251231",
                    "roe_waa": 16.0,
                    "source": "tracker",
                    "ann_date": "2026-04-15",
                    "update_flag": "0",
                },
            ],
            "roe_mean": 14.0,
            "exclusions": [],
        },
        {
            "code": "600002.SH",
            "name": "真实标的乙",
            "pb": 1.50,
            "annual_roes": [
                {
                    "period": "20231231",
                    "roe_waa": 15.0,
                    "source": "tracker",
                    "ann_date": "2024-04-10",
                    "update_flag": "0",
                },
                {
                    "period": "20241231",
                    "roe_waa": 15.0,
                    "source": "tracker",
                    "ann_date": "2025-04-12",
                    "update_flag": "0",
                },
                {
                    "period": "20251231",
                    "roe_waa": 15.0,
                    "source": "tracker",
                    "ann_date": "2026-04-15",
                    "update_flag": "0",
                },
            ],
            "roe_mean": 15.0,
            "exclusions": [],
        },
    ]

    results = screen.rank_peers(rows, anchor, watchlist)
    # Tamper with ranking company name
    tampered_results = json.loads(json.dumps(results))
    tampered_results["ranking"][0]["name"] = "被篡改的公司名"

    snapshot_data = {
        "schema_version": 1,
        "rule": "peer-screen-v1",
        "limits": {"cap": 50, "top_n": 3, "max_report_age_days": 550},
        "formula": "research_order=(roe_rank_desc+pb_rank_asc)/2; average ties",
        "screened_at": "2026-09-20T16:00:00+08:00",
        "generated_at": "2026-09-20T16:00:00+08:00",
        "data_date": "2026-09-20",
        "anchor": anchor,
        "watchlist_codes": watchlist,
        "source": "screen_test",
        "scope": {"source": "screen_test", "total_candidates": 2},
        "rows": rows,
        "results": tampered_results,
    }

    snap_file = tmp_path / "name_conflict.json"
    snap_file.write_text(json.dumps(snapshot_data, ensure_ascii=False), encoding="utf-8")

    run_id = import_snapshot(ws_dir, snap_file, mode="demo")

    conn = connect_workspace(ws_dir, mode="demo")
    try:
        run = get_run(conn, run_id)
        assert run is not None
        assert run["health"] == "unverified"
    finally:
        conn.close()


def test_live_snapshot_output_shape_import_complete(tmp_path: Path) -> None:
    ws_dir = tmp_path / "live_shape_ws"
    initialize(ws_dir, mode="demo", journal_mode="DELETE")

    anchor = "600001.SH"
    watchlist = [screen.base_code(anchor)]
    rows = [
        {
            "code": "600001.SH",
            "name": "真实标的甲",
            "pb": 2.10,
            "valuation_source": "tushare.daily_basic",
            "valuation_date": "2026-09-20",
            "financial_source": "tracker:data/tushare-primary.db",
            "risk_source": "tushare.stock_basic.name",
            "annual_roes": [
                {
                    "period": "20231231",
                    "roe_waa": 12.0,
                    "source": "tracker",
                    "ann_date": "2024-04-15",
                    "update_flag": "0",
                },
                {
                    "period": "20241231",
                    "roe_waa": 14.0,
                    "source": "tracker",
                    "ann_date": "2025-04-20",
                    "update_flag": "0",
                },
                {
                    "period": "20251231",
                    "roe_waa": 16.0,
                    "source": "tracker",
                    "ann_date": "2026-04-15",
                    "update_flag": "0",
                },
            ],
            "roe_mean": 14.0,
            "exclusions": [],
        },
        {
            "code": "600002.SH",
            "name": "真实标的乙",
            "pb": 1.50,
            "valuation_source": "tushare.daily_basic",
            "valuation_date": "2026-09-20",
            "financial_source": "tracker:data/tushare-primary.db",
            "risk_source": "tushare.stock_basic.name",
            "annual_roes": [
                {
                    "period": "20231231",
                    "roe_waa": 15.0,
                    "source": "tracker",
                    "ann_date": "2024-04-10",
                    "update_flag": "0",
                },
                {
                    "period": "20241231",
                    "roe_waa": 15.0,
                    "source": "tracker",
                    "ann_date": "2025-04-12",
                    "update_flag": "0",
                },
                {
                    "period": "20251231",
                    "roe_waa": 15.0,
                    "source": "tracker",
                    "ann_date": "2026-04-15",
                    "update_flag": "0",
                },
            ],
            "roe_mean": 15.0,
            "exclusions": [],
        },
    ]

    results = screen.rank_peers(rows, anchor, watchlist)
    scope = {
        "industry": "专用设备",
        "industry_source": "tushare.stock_basic",
        "enumerated_count": 2,
        "enumerated_codes": ["600001.SH", "600002.SH"],
        "selected_codes": ["600001.SH", "600002.SH"],
        "excluded_missing_valuation": [],
        "excluded_by_cap": [],
        "cap": 50,
        "valuation_date": "2026-09-20",
        "source": "tracker",
    }

    live_snapshot = {
        "schema_version": screen.SCHEMA_VERSION,
        "rule": screen.RULE,
        "source": "tracker",
        "limits": {
            "cap": screen.CAP,
            "top_n": screen.TOP_N,
            "max_report_age_days": screen.MAX_REPORT_AGE_DAYS,
        },
        "formula": "research_order=(roe_rank_desc+pb_rank_asc)/2; average ties",
        "screened_at": "2026-09-20T16:00:00+08:00",
        "generated_at": "2026-09-20T16:00:00+08:00",
        "data_date": "2026-09-20",
        "anchor": anchor,
        "watchlist_codes": watchlist,
        "legacy_reference": {
            "industry": "专用设备",
            "name": "真实标的甲",
            "updated_at": "2026-09-20 09:15:00",
        },
        "scope": scope,
        "rows": rows,
        "results": results,
    }

    snap_file = tmp_path / "live_shape_snapshot.json"
    snap_file.write_text(json.dumps(live_snapshot, ensure_ascii=False), encoding="utf-8")

    run_id = import_snapshot(ws_dir, snap_file, mode="demo")

    conn = connect_workspace(ws_dir, mode="demo")
    try:
        run = get_run(conn, run_id)
        assert run is not None
        assert run["health"] == "complete"
    finally:
        conn.close()


def test_scope_selected_codes_mismatch_rows_degrades_to_unverified(tmp_path: Path) -> None:
    ws_dir = tmp_path / "scope_mismatch_ws"
    initialize(ws_dir, mode="demo", journal_mode="DELETE")

    anchor = "600001.SH"
    watchlist = [screen.base_code(anchor)]
    rows = [
        {
            "code": "600001.SH",
            "name": "真实标的甲",
            "pb": 2.10,
            "annual_roes": [
                {
                    "period": "20231231",
                    "roe_waa": 12.0,
                    "source": "tracker",
                    "ann_date": "2024-04-15",
                    "update_flag": "0",
                },
                {
                    "period": "20241231",
                    "roe_waa": 14.0,
                    "source": "tracker",
                    "ann_date": "2025-04-20",
                    "update_flag": "0",
                },
                {
                    "period": "20251231",
                    "roe_waa": 16.0,
                    "source": "tracker",
                    "ann_date": "2026-04-15",
                    "update_flag": "0",
                },
            ],
            "roe_mean": 14.0,
            "exclusions": [],
        },
        {
            "code": "600002.SH",
            "name": "真实标的乙",
            "pb": 1.50,
            "annual_roes": [
                {
                    "period": "20231231",
                    "roe_waa": 15.0,
                    "source": "tracker",
                    "ann_date": "2024-04-10",
                    "update_flag": "0",
                },
                {
                    "period": "20241231",
                    "roe_waa": 15.0,
                    "source": "tracker",
                    "ann_date": "2025-04-12",
                    "update_flag": "0",
                },
                {
                    "period": "20251231",
                    "roe_waa": 15.0,
                    "source": "tracker",
                    "ann_date": "2026-04-15",
                    "update_flag": "0",
                },
            ],
            "roe_mean": 15.0,
            "exclusions": [],
        },
    ]
    results = screen.rank_peers(rows, anchor, watchlist)

    # selected_codes only lists 600001.SH, but rows has 2 companies
    snapshot_data = {
        "schema_version": 1,
        "rule": "peer-screen-v1",
        "limits": {"cap": 50, "top_n": 3, "max_report_age_days": 550},
        "formula": "research_order=(roe_rank_desc+pb_rank_asc)/2; average ties",
        "screened_at": "2026-09-20T16:00:00+08:00",
        "generated_at": "2026-09-20T16:00:00+08:00",
        "data_date": "2026-09-20",
        "anchor": anchor,
        "watchlist_codes": watchlist,
        "source": "tracker",
        "scope": {
            "source": "tracker",
            "industry": "专用设备",
            "enumerated_count": 2,
            "selected_codes": ["600001.SH"],
        },
        "rows": rows,
        "results": results,
    }
    snap_file = tmp_path / "scope_mismatch.json"
    snap_file.write_text(json.dumps(snapshot_data, ensure_ascii=False), encoding="utf-8")
    run_id = import_snapshot(ws_dir, snap_file, mode="demo")

    conn = connect_workspace(ws_dir, mode="demo")
    try:
        run = get_run(conn, run_id)
        assert run is not None
        assert run["health"] == "unverified"
    finally:
        conn.close()


def test_scope_enumerated_count_less_than_selected_degrades_to_unverified(tmp_path: Path) -> None:
    ws_dir = tmp_path / "scope_less_enum_ws"
    initialize(ws_dir, mode="demo", journal_mode="DELETE")

    anchor = "600001.SH"
    watchlist = [screen.base_code(anchor)]
    rows = [
        {
            "code": "600001.SH",
            "name": "真实标的甲",
            "pb": 2.10,
            "annual_roes": [
                {
                    "period": "20231231",
                    "roe_waa": 12.0,
                    "source": "tracker",
                    "ann_date": "2024-04-15",
                    "update_flag": "0",
                },
                {
                    "period": "20241231",
                    "roe_waa": 14.0,
                    "source": "tracker",
                    "ann_date": "2025-04-20",
                    "update_flag": "0",
                },
                {
                    "period": "20251231",
                    "roe_waa": 16.0,
                    "source": "tracker",
                    "ann_date": "2026-04-15",
                    "update_flag": "0",
                },
            ],
            "roe_mean": 14.0,
            "exclusions": [],
        },
        {
            "code": "600002.SH",
            "name": "真实标的乙",
            "pb": 1.50,
            "annual_roes": [
                {
                    "period": "20231231",
                    "roe_waa": 15.0,
                    "source": "tracker",
                    "ann_date": "2024-04-10",
                    "update_flag": "0",
                },
                {
                    "period": "20241231",
                    "roe_waa": 15.0,
                    "source": "tracker",
                    "ann_date": "2025-04-12",
                    "update_flag": "0",
                },
                {
                    "period": "20251231",
                    "roe_waa": 15.0,
                    "source": "tracker",
                    "ann_date": "2026-04-15",
                    "update_flag": "0",
                },
            ],
            "roe_mean": 15.0,
            "exclusions": [],
        },
    ]
    results = screen.rank_peers(rows, anchor, watchlist)

    # enumerated_count=1 is less than selected_codes count (2)
    snapshot_data = {
        "schema_version": 1,
        "rule": "peer-screen-v1",
        "limits": {"cap": 50, "top_n": 3, "max_report_age_days": 550},
        "formula": "research_order=(roe_rank_desc+pb_rank_asc)/2; average ties",
        "screened_at": "2026-09-20T16:00:00+08:00",
        "generated_at": "2026-09-20T16:00:00+08:00",
        "data_date": "2026-09-20",
        "anchor": anchor,
        "watchlist_codes": watchlist,
        "source": "tracker",
        "scope": {
            "source": "tracker",
            "industry": "专用设备",
            "enumerated_count": 1,
            "selected_codes": ["600001.SH", "600002.SH"],
        },
        "rows": rows,
        "results": results,
    }
    snap_file = tmp_path / "scope_less_enum.json"
    snap_file.write_text(json.dumps(snapshot_data, ensure_ascii=False), encoding="utf-8")
    run_id = import_snapshot(ws_dir, snap_file, mode="demo")

    conn = connect_workspace(ws_dir, mode="demo")
    try:
        run = get_run(conn, run_id)
        assert run is not None
        assert run["health"] == "unverified"
    finally:
        conn.close()


def test_arbitrary_source_string_degrades_to_unverified(tmp_path: Path) -> None:
    ws_dir = tmp_path / "arbitrary_src_ws"
    initialize(ws_dir, mode="demo", journal_mode="DELETE")

    anchor = "600001.SH"
    watchlist = [screen.base_code(anchor)]
    rows = [
        {
            "code": "600001.SH",
            "name": "真实标的甲",
            "pb": 2.10,
            "annual_roes": [
                {
                    "period": "20231231",
                    "roe_waa": 12.0,
                    "source": "tracker",
                    "ann_date": "2024-04-15",
                    "update_flag": "0",
                },
                {
                    "period": "20241231",
                    "roe_waa": 14.0,
                    "source": "tracker",
                    "ann_date": "2025-04-20",
                    "update_flag": "0",
                },
                {
                    "period": "20251231",
                    "roe_waa": 16.0,
                    "source": "tracker",
                    "ann_date": "2026-04-15",
                    "update_flag": "0",
                },
            ],
            "roe_mean": 14.0,
            "exclusions": [],
        },
        {
            "code": "600002.SH",
            "name": "真实标的乙",
            "pb": 1.50,
            "annual_roes": [
                {
                    "period": "20231231",
                    "roe_waa": 15.0,
                    "source": "tracker",
                    "ann_date": "2024-04-10",
                    "update_flag": "0",
                },
                {
                    "period": "20241231",
                    "roe_waa": 15.0,
                    "source": "tracker",
                    "ann_date": "2025-04-12",
                    "update_flag": "0",
                },
                {
                    "period": "20251231",
                    "roe_waa": 15.0,
                    "source": "tracker",
                    "ann_date": "2026-04-15",
                    "update_flag": "0",
                },
            ],
            "roe_mean": 15.0,
            "exclusions": [],
        },
    ]
    results = screen.rank_peers(rows, anchor, watchlist)

    # Source is an arbitrary custom string
    snapshot_data = {
        "schema_version": 1,
        "rule": "peer-screen-v1",
        "limits": {"cap": 50, "top_n": 3, "max_report_age_days": 550},
        "formula": "research_order=(roe_rank_desc+pb_rank_asc)/2; average ties",
        "screened_at": "2026-09-20T16:00:00+08:00",
        "generated_at": "2026-09-20T16:00:00+08:00",
        "data_date": "2026-09-20",
        "anchor": anchor,
        "watchlist_codes": watchlist,
        "source": "custom_scraper",
        "scope": {
            "source": "custom_scraper",
            "industry": "专用设备",
            "enumerated_count": 2,
            "selected_codes": ["600001.SH", "600002.SH"],
        },
        "rows": rows,
        "results": results,
    }
    snap_file = tmp_path / "arbitrary_src.json"
    snap_file.write_text(json.dumps(snapshot_data, ensure_ascii=False), encoding="utf-8")
    run_id = import_snapshot(ws_dir, snap_file, mode="demo")

    conn = connect_workspace(ws_dir, mode="demo")
    try:
        run = get_run(conn, run_id)
        assert run is not None
        assert run["health"] == "unverified"
    finally:
        conn.close()


def test_mismatched_top_and_scope_source_degrades_to_unverified(tmp_path: Path) -> None:
    ws_dir = tmp_path / "mismatch_src_ws"
    initialize(ws_dir, mode="demo", journal_mode="DELETE")

    anchor = "600001.SH"
    watchlist = [screen.base_code(anchor)]
    rows = [
        {
            "code": "600001.SH",
            "name": "真实标的甲",
            "pb": 2.10,
            "annual_roes": [
                {
                    "period": "20231231",
                    "roe_waa": 12.0,
                    "source": "tracker",
                    "ann_date": "2024-04-15",
                    "update_flag": "0",
                },
                {
                    "period": "20241231",
                    "roe_waa": 14.0,
                    "source": "tracker",
                    "ann_date": "2025-04-20",
                    "update_flag": "0",
                },
                {
                    "period": "20251231",
                    "roe_waa": 16.0,
                    "source": "tracker",
                    "ann_date": "2026-04-15",
                    "update_flag": "0",
                },
            ],
            "roe_mean": 14.0,
            "exclusions": [],
        },
        {
            "code": "600002.SH",
            "name": "真实标的乙",
            "pb": 1.50,
            "annual_roes": [
                {
                    "period": "20231231",
                    "roe_waa": 15.0,
                    "source": "tracker",
                    "ann_date": "2024-04-10",
                    "update_flag": "0",
                },
                {
                    "period": "20241231",
                    "roe_waa": 15.0,
                    "source": "tracker",
                    "ann_date": "2025-04-12",
                    "update_flag": "0",
                },
                {
                    "period": "20251231",
                    "roe_waa": 15.0,
                    "source": "tracker",
                    "ann_date": "2026-04-15",
                    "update_flag": "0",
                },
            ],
            "roe_mean": 15.0,
            "exclusions": [],
        },
    ]
    results = screen.rank_peers(rows, anchor, watchlist)

    # Top-level is tracker, but scope says fixture
    snapshot_data = {
        "schema_version": 1,
        "rule": "peer-screen-v1",
        "limits": {"cap": 50, "top_n": 3, "max_report_age_days": 550},
        "formula": "research_order=(roe_rank_desc+pb_rank_asc)/2; average ties",
        "screened_at": "2026-09-20T16:00:00+08:00",
        "generated_at": "2026-09-20T16:00:00+08:00",
        "data_date": "2026-09-20",
        "anchor": anchor,
        "watchlist_codes": watchlist,
        "source": "tracker",
        "scope": {
            "source": "fixture",
            "industry": "专用设备",
            "enumerated_count": 2,
            "selected_codes": ["600001.SH", "600002.SH"],
        },
        "rows": rows,
        "results": results,
    }
    snap_file = tmp_path / "mismatch_src.json"
    snap_file.write_text(json.dumps(snapshot_data, ensure_ascii=False), encoding="utf-8")
    run_id = import_snapshot(ws_dir, snap_file, mode="demo")

    conn = connect_workspace(ws_dir, mode="demo")
    try:
        run = get_run(conn, run_id)
        assert run is not None
        assert run["health"] == "unverified"
    finally:
        conn.close()


def test_scope_selected_codes_malformed_unhashable_element_degrades_to_unverified(
    tmp_path: Path,
) -> None:
    ws_dir = tmp_path / "malformed_scope_ws"
    initialize(ws_dir, mode="demo", journal_mode="DELETE")

    anchor = "600001.SH"
    watchlist = [screen.base_code(anchor)]
    rows = [
        {
            "code": "600001.SH",
            "name": "真实标的甲",
            "pb": 2.10,
            "annual_roes": [
                {
                    "period": "20231231",
                    "roe_waa": 12.0,
                    "source": "tracker",
                    "ann_date": "2024-04-15",
                    "update_flag": "0",
                },
                {
                    "period": "20241231",
                    "roe_waa": 14.0,
                    "source": "tracker",
                    "ann_date": "2025-04-20",
                    "update_flag": "0",
                },
                {
                    "period": "20251231",
                    "roe_waa": 16.0,
                    "source": "tracker",
                    "ann_date": "2026-04-15",
                    "update_flag": "0",
                },
            ],
            "roe_mean": 14.0,
            "exclusions": [],
        },
    ]
    results = screen.rank_peers(rows, anchor, watchlist)

    # selected_codes contains an unhashable dict object
    snapshot_data = {
        "schema_version": 1,
        "rule": "peer-screen-v1",
        "limits": {"cap": 50, "top_n": 3, "max_report_age_days": 550},
        "formula": "research_order=(roe_rank_desc+pb_rank_asc)/2; average ties",
        "screened_at": "2026-09-20T16:00:00+08:00",
        "generated_at": "2026-09-20T16:00:00+08:00",
        "data_date": "2026-09-20",
        "anchor": anchor,
        "watchlist_codes": watchlist,
        "source": "tracker",
        "scope": {
            "source": "tracker",
            "industry": "专用设备",
            "enumerated_count": 1,
            "selected_codes": [{"bad": 1}],
        },
        "rows": rows,
        "results": results,
    }
    snap_file = tmp_path / "unhashable_scope.json"
    snap_file.write_text(json.dumps(snapshot_data, ensure_ascii=False), encoding="utf-8")
    run_id = import_snapshot(ws_dir, snap_file, mode="demo")

    conn = connect_workspace(ws_dir, mode="demo")
    try:
        run = get_run(conn, run_id)
        assert run is not None
        assert run["health"] == "unverified"
    finally:
        conn.close()


def test_scope_selected_codes_missing_anchor_degrades_to_unverified(tmp_path: Path) -> None:
    ws_dir = tmp_path / "missing_anchor_scope_ws"
    initialize(ws_dir, mode="demo", journal_mode="DELETE")

    anchor = "600001.SH"
    watchlist = [screen.base_code(anchor)]
    rows = [
        {
            "code": "600002.SH",
            "name": "真实标的乙",
            "pb": 1.50,
            "annual_roes": [
                {
                    "period": "20231231",
                    "roe_waa": 15.0,
                    "source": "tracker",
                    "ann_date": "2024-04-10",
                    "update_flag": "0",
                },
                {
                    "period": "20241231",
                    "roe_waa": 15.0,
                    "source": "tracker",
                    "ann_date": "2025-04-12",
                    "update_flag": "0",
                },
                {
                    "period": "20251231",
                    "roe_waa": 15.0,
                    "source": "tracker",
                    "ann_date": "2026-04-15",
                    "update_flag": "0",
                },
            ],
            "roe_mean": 15.0,
            "exclusions": [],
        },
    ]
    results = screen.rank_peers(rows, anchor, watchlist)

    snapshot_data = {
        "schema_version": 1,
        "rule": "peer-screen-v1",
        "limits": {"cap": 50, "top_n": 3, "max_report_age_days": 550},
        "formula": "research_order=(roe_rank_desc+pb_rank_asc)/2; average ties",
        "screened_at": "2026-09-20T16:00:00+08:00",
        "generated_at": "2026-09-20T16:00:00+08:00",
        "data_date": "2026-09-20",
        "anchor": anchor,
        "watchlist_codes": watchlist,
        "source": "tracker",
        "scope": {
            "source": "tracker",
            "industry": "专用设备",
            "enumerated_count": 1,
            "selected_codes": ["600002.SH"],
        },
        "rows": rows,
        "results": results,
    }
    snap_file = tmp_path / "missing_anchor_scope.json"
    snap_file.write_text(json.dumps(snapshot_data, ensure_ascii=False), encoding="utf-8")
    run_id = import_snapshot(ws_dir, snap_file, mode="demo")

    conn = connect_workspace(ws_dir, mode="demo")
    try:
        run = get_run(conn, run_id)
        assert run is not None
        assert run["health"] == "unverified"
    finally:
        conn.close()


def test_scope_selected_codes_exceeding_cap_degrades_to_unverified(tmp_path: Path) -> None:
    ws_dir = tmp_path / "exceeding_cap_scope_ws"
    initialize(ws_dir, mode="demo", journal_mode="DELETE")

    anchor = "600001.SH"
    watchlist = [screen.base_code(anchor)]
    # Create 51 codes
    codes = [f"60{i:04d}.SH" for i in range(1, 52)]
    rows = [
        {
            "code": c,
            "name": f"公司{c}",
            "pb": 1.50,
            "annual_roes": [
                {
                    "period": "20231231",
                    "roe_waa": 15.0,
                    "source": "tracker",
                    "ann_date": "2024-04-10",
                    "update_flag": "0",
                },
                {
                    "period": "20241231",
                    "roe_waa": 15.0,
                    "source": "tracker",
                    "ann_date": "2025-04-12",
                    "update_flag": "0",
                },
                {
                    "period": "20251231",
                    "roe_waa": 15.0,
                    "source": "tracker",
                    "ann_date": "2026-04-15",
                    "update_flag": "0",
                },
            ],
            "roe_mean": 15.0,
            "exclusions": [],
        }
        for c in codes
    ]
    results = screen.rank_peers(rows, anchor, watchlist)

    snapshot_data = {
        "schema_version": 1,
        "rule": "peer-screen-v1",
        "limits": {"cap": 50, "top_n": 3, "max_report_age_days": 550},
        "formula": "research_order=(roe_rank_desc+pb_rank_asc)/2; average ties",
        "screened_at": "2026-09-20T16:00:00+08:00",
        "generated_at": "2026-09-20T16:00:00+08:00",
        "data_date": "2026-09-20",
        "anchor": anchor,
        "watchlist_codes": watchlist,
        "source": "tracker",
        "scope": {
            "source": "tracker",
            "industry": "专用设备",
            "enumerated_count": 51,
            "selected_codes": codes,
        },
        "rows": rows,
        "results": results,
    }
    snap_file = tmp_path / "exceeding_cap_scope.json"
    snap_file.write_text(json.dumps(snapshot_data, ensure_ascii=False), encoding="utf-8")
    run_id = import_snapshot(ws_dir, snap_file, mode="demo")

    conn = connect_workspace(ws_dir, mode="demo")
    try:
        run = get_run(conn, run_id)
        assert run is not None
        assert run["health"] == "unverified"
    finally:
        conn.close()


def test_scope_selected_codes_mismatched_exchange_suffix_degrades_to_unverified(
    tmp_path: Path,
) -> None:
    ws_dir = tmp_path / "mismatch_suffix_ws"
    initialize(ws_dir, mode="demo", journal_mode="DELETE")

    anchor = "600001.SH"
    watchlist = [screen.base_code(anchor)]
    # Rows contains 600001.SZ instead of 600001.SH (same 6-digit number, different exchange)
    rows = [
        {
            "code": "600001.SZ",
            "name": "真实标的SZ",
            "pb": 2.10,
            "annual_roes": [
                {
                    "period": "20231231",
                    "roe_waa": 12.0,
                    "source": "tracker",
                    "ann_date": "2024-04-15",
                    "update_flag": "0",
                },
                {
                    "period": "20241231",
                    "roe_waa": 14.0,
                    "source": "tracker",
                    "ann_date": "2025-04-20",
                    "update_flag": "0",
                },
                {
                    "period": "20251231",
                    "roe_waa": 16.0,
                    "source": "tracker",
                    "ann_date": "2026-04-15",
                    "update_flag": "0",
                },
            ],
            "roe_mean": 14.0,
            "exclusions": [],
        },
    ]
    results = screen.rank_peers(rows, anchor, watchlist)

    snapshot_data = {
        "schema_version": 1,
        "rule": "peer-screen-v1",
        "limits": {"cap": 50, "top_n": 3, "max_report_age_days": 550},
        "formula": "research_order=(roe_rank_desc+pb_rank_asc)/2; average ties",
        "screened_at": "2026-09-20T16:00:00+08:00",
        "generated_at": "2026-09-20T16:00:00+08:00",
        "data_date": "2026-09-20",
        "anchor": anchor,
        "watchlist_codes": watchlist,
        "source": "tracker",
        "scope": {
            "source": "tracker",
            "industry": "专用设备",
            "enumerated_count": 1,
            "selected_codes": ["600001.SZ"],
        },
        "rows": rows,
        "results": results,
    }
    snap_file = tmp_path / "mismatch_suffix.json"
    snap_file.write_text(json.dumps(snapshot_data, ensure_ascii=False), encoding="utf-8")
    run_id = import_snapshot(ws_dir, snap_file, mode="demo")

    conn = connect_workspace(ws_dir, mode="demo")
    try:
        run = get_run(conn, run_id)
        assert run is not None
        assert run["health"] == "unverified"
    finally:
        conn.close()


def _make_valid_test_snapshot():
    anchor = "600001.SH"
    watchlist = [screen.base_code(anchor)]
    rows = [
        {
            "code": "600001.SH",
            "name": "标的甲",
            "pb": 1.80,
            "valuation_date": "2026-09-20",
            "valuation_source": "tracker",
            "financial_source": "tracker",
            "risk_source": "tracker",
            "valuation_status": "ok",
            "financial_status": "ok",
            "annual_roes": [
                {
                    "period": "20231231",
                    "roe_waa": 12.0,
                    "ann_date": "2024-04-15",
                    "source": "tracker",
                },
                {
                    "period": "20241231",
                    "roe_waa": 14.0,
                    "ann_date": "2025-04-20",
                    "source": "tracker",
                },
                {
                    "period": "20251231",
                    "roe_waa": 16.0,
                    "ann_date": "2026-04-15",
                    "source": "tracker",
                },
            ],
            "roe_mean": 14.0,
            "exclusions": [],
            "facts_usable": True,
        },
        {
            "code": "600002.SH",
            "name": "标的乙",
            "pb": 1.20,
            "valuation_date": "2026-09-20",
            "valuation_source": "tracker",
            "financial_source": "tracker",
            "risk_source": "tracker",
            "valuation_status": "ok",
            "financial_status": "ok",
            "annual_roes": [
                {
                    "period": "20231231",
                    "roe_waa": 15.0,
                    "ann_date": "2024-04-10",
                    "source": "tracker",
                },
                {
                    "period": "20241231",
                    "roe_waa": 15.0,
                    "ann_date": "2025-04-12",
                    "source": "tracker",
                },
                {
                    "period": "20251231",
                    "roe_waa": 15.0,
                    "ann_date": "2026-04-15",
                    "source": "tracker",
                },
            ],
            "roe_mean": 15.0,
            "exclusions": [],
            "facts_usable": True,
        },
    ]
    results = screen.rank_peers(rows, anchor, watchlist)
    return {
        "schema_version": 1,
        "rule": "peer-screen-v1",
        "limits": {"cap": 50, "top_n": 3, "max_report_age_days": 550},
        "formula": "research_order=(roe_rank_desc+pb_rank_asc)/2; average ties",
        "screened_at": "2026-09-20T16:00:00+08:00",
        "generated_at": "2026-09-20T16:00:00+08:00",
        "data_date": "2026-09-20",
        "anchor": anchor,
        "watchlist_codes": watchlist,
        "source": "tracker",
        "scope": {
            "source": "tracker",
            "industry": "专用设备",
            "cap": 50,
            "enumerated_count": 2,
            "total_candidates": 2,
            "enumerated_codes": ["600001.SH", "600002.SH"],
            "selected_codes": ["600001.SH", "600002.SH"],
            "excluded_missing_valuation": [],
            "excluded_by_cap": [],
        },
        "rows": rows,
        "results": results,
    }


def test_reverse_annual_reports_are_usable() -> None:
    row = _make_valid_test_snapshot()["rows"][0]
    row["annual_roes"].reverse()
    assert [report["period"][:4] for report in row["annual_roes"]] == ["2025", "2024", "2023"]
    assert is_row_usable(row)


@pytest.mark.parametrize("bad_source", [None, "unknown.vendor", ""])
def test_annual_report_source_required_for_verified_facts(
    tmp_path: Path, bad_source: str | None
) -> None:
    ws_dir = tmp_path / "source_ws"
    initialize(ws_dir, mode="production", journal_mode="DELETE")
    snapshot = _make_valid_test_snapshot()
    row = snapshot["rows"][0]
    assert is_row_usable(row)
    if bad_source is None:
        row["annual_roes"][1].pop("source")
    else:
        row["annual_roes"][1]["source"] = bad_source
    assert not is_row_usable(row)
    path = tmp_path / "bad_annual_source.json"
    path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    run_id = import_snapshot(ws_dir, path, "production")
    conn = connect_workspace(ws_dir, "production")
    try:
        assert present(get_run(conn, run_id))["health"] == "unverified"
    finally:
        conn.close()


def test_malformed_json_root_and_rows_rejected(tmp_path: Path) -> None:
    ws_dir = tmp_path / "malformed_ws"
    initialize(ws_dir, mode="demo", journal_mode="DELETE")

    # 1. Non-object root
    file_list_root = tmp_path / "list_root.json"
    file_list_root.write_bytes(b"[1, 2, 3]")
    with pytest.raises(WorkspaceError, match="snapshot root must be a JSON object"):
        import_snapshot(ws_dir, file_list_root, mode="demo")

    # 2. Rows is null
    file_null_rows = tmp_path / "null_rows.json"
    file_null_rows.write_text(json.dumps({"anchor": "600001.SH", "rows": None}), encoding="utf-8")
    with pytest.raises(WorkspaceError, match="snapshot rows must be a list of objects"):
        import_snapshot(ws_dir, file_null_rows, mode="demo")

    # 3. Rows is a string
    file_str_rows = tmp_path / "str_rows.json"
    file_str_rows.write_text(
        json.dumps({"anchor": "600001.SH", "rows": "not_a_list"}), encoding="utf-8"
    )
    with pytest.raises(WorkspaceError, match="snapshot rows must be a list of objects"):
        import_snapshot(ws_dir, file_str_rows, mode="demo")

    # 4. Rows contains non-dict elements
    file_bad_elem = tmp_path / "bad_elem.json"
    file_bad_elem.write_text(
        json.dumps({"anchor": "600001.SH", "rows": [123, "not_dict"]}), encoding="utf-8"
    )
    with pytest.raises(WorkspaceError, match="snapshot rows must be a list of objects"):
        import_snapshot(ws_dir, file_bad_elem, mode="demo")


def test_scope_partition_contradictions_degrade_to_unverified(tmp_path: Path) -> None:
    ws_dir = tmp_path / "scope_contra_ws"
    initialize(ws_dir, mode="demo", journal_mode="DELETE")

    # Base valid snapshot
    base = _make_valid_test_snapshot()

    # Case 1: Missing enumerated_codes in scope
    c1 = json.loads(json.dumps(base))
    del c1["scope"]["enumerated_codes"]
    f1 = tmp_path / "c1.json"
    f1.write_text(json.dumps(c1), encoding="utf-8")
    run1 = get_run(connect_workspace(ws_dir, "demo"), import_snapshot(ws_dir, f1, mode="demo"))
    assert run1 is not None and run1["health"] == "unverified"

    # Case 2: enumerated_count contradicts length of enumerated_codes
    c2 = json.loads(json.dumps(base))
    c2["scope"]["enumerated_count"] = 5
    f2 = tmp_path / "c2.json"
    f2.write_text(json.dumps(c2), encoding="utf-8")
    run2 = get_run(connect_workspace(ws_dir, "demo"), import_snapshot(ws_dir, f2, mode="demo"))
    assert run2 is not None and run2["health"] == "unverified"

    # Case 3: selected_codes overlaps with excluded_by_cap
    c3 = json.loads(json.dumps(base))
    c3["scope"]["excluded_by_cap"] = ["600002.SH"]
    f3 = tmp_path / "c3.json"
    f3.write_text(json.dumps(c3), encoding="utf-8")
    run3 = get_run(connect_workspace(ws_dir, "demo"), import_snapshot(ws_dir, f3, mode="demo"))
    assert run3 is not None and run3["health"] == "unverified"

    # Case 4: scope.cap != 50
    c4 = json.loads(json.dumps(base))
    c4["scope"]["cap"] = 30
    f4 = tmp_path / "c4.json"
    f4.write_text(json.dumps(c4), encoding="utf-8")
    run4 = get_run(connect_workspace(ws_dir, "demo"), import_snapshot(ws_dir, f4, mode="demo"))
    assert run4 is not None and run4["health"] == "unverified"


def test_row_level_dates_and_sources_validation(tmp_path: Path) -> None:
    ws_dir = tmp_path / "row_dates_sources_ws"
    initialize(ws_dir, mode="demo", journal_mode="DELETE")

    base = _make_valid_test_snapshot()

    # Case 1: Row missing valuation_date
    c1 = json.loads(json.dumps(base))
    del c1["rows"][0]["valuation_date"]
    f1 = tmp_path / "r1.json"
    f1.write_text(json.dumps(c1), encoding="utf-8")
    run1 = get_run(connect_workspace(ws_dir, "demo"), import_snapshot(ws_dir, f1, mode="demo"))
    assert run1 is not None and run1["health"] == "unverified"

    # Case 2: Row valuation_source is untrusted
    c2 = json.loads(json.dumps(base))
    c2["rows"][0]["valuation_source"] = "untrusted_web_crawler"
    f2 = tmp_path / "r2.json"
    f2.write_text(json.dumps(c2), encoding="utf-8")
    run2 = get_run(connect_workspace(ws_dir, "demo"), import_snapshot(ws_dir, f2, mode="demo"))
    assert run2 is not None and run2["health"] == "unverified"

    # Case 3: Row financial_source is unknown or invalid
    c3 = json.loads(json.dumps(base))
    c3["rows"][0]["financial_source"] = "random_source"
    f3 = tmp_path / "r3.json"
    f3.write_text(json.dumps(c3), encoding="utf-8")
    run3 = get_run(connect_workspace(ws_dir, "demo"), import_snapshot(ws_dir, f3, mode="demo"))
    assert run3 is not None and run3["health"] == "unverified"

    # Case 4: Non-consecutive annual roe periods (e.g. 2021, 2023, 2025)
    c4 = json.loads(json.dumps(base))
    c4["rows"][0]["annual_roes"] = [
        {"period": "20211231", "roe_waa": 12.0, "ann_date": "2022-04-15", "source": "tracker"},
        {"period": "20231231", "roe_waa": 14.0, "ann_date": "2024-04-20", "source": "tracker"},
        {"period": "20251231", "roe_waa": 16.0, "ann_date": "2026-04-15", "source": "tracker"},
    ]
    f4 = tmp_path / "r4.json"
    f4.write_text(json.dumps(c4), encoding="utf-8")
    run4 = get_run(connect_workspace(ws_dir, "demo"), import_snapshot(ws_dir, f4, mode="demo"))
    assert run4 is not None and run4["health"] == "unverified"

    # Case 5: Annual report older than 550 days from data_date
    c5 = json.loads(json.dumps(base))
    c5["rows"][0]["annual_roes"] = [
        {"period": "20211231", "roe_waa": 10.0, "ann_date": "2022-04-15", "source": "tracker"},
        {"period": "20221231", "roe_waa": 12.0, "ann_date": "2023-04-20", "source": "tracker"},
        {"period": "20231231", "roe_waa": 14.0, "ann_date": "2024-04-15", "source": "tracker"},
    ]
    f5 = tmp_path / "r5.json"
    f5.write_text(json.dumps(c5), encoding="utf-8")
    run5 = get_run(connect_workspace(ws_dir, "demo"), import_snapshot(ws_dir, f5, mode="demo"))
    assert run5 is not None and run5["health"] == "unverified"

    # Case 6: Valuation date 2026-12-31 vs latest annual period 2024-12-31 (730 days > 550 days)
    c6 = json.loads(json.dumps(base))
    c6["data_date"] = "2026-12-31"
    for r in c6["rows"]:
        r["valuation_date"] = "2026-12-31"
    c6["rows"][0]["annual_roes"] = [
        {"period": "20221231", "roe_waa": 10.0, "ann_date": "2023-04-15", "source": "tracker"},
        {"period": "20231231", "roe_waa": 12.0, "ann_date": "2024-04-20", "source": "tracker"},
        {"period": "20241231", "roe_waa": 14.0, "ann_date": "2025-04-15", "source": "tracker"},
    ]
    f6 = tmp_path / "r6.json"
    f6.write_text(json.dumps(c6), encoding="utf-8")
    run6 = get_run(connect_workspace(ws_dir, "demo"), import_snapshot(ws_dir, f6, mode="demo"))
    assert run6 is not None and run6["health"] == "unverified"

    # Case 7: Quarterly report (non 12-31) in annual_roes
    c7 = json.loads(json.dumps(base))
    c7["rows"][0]["annual_roes"] = [
        {"period": "20231231", "roe_waa": 10.0, "ann_date": "2024-04-15", "source": "tracker"},
        {"period": "20241231", "roe_waa": 12.0, "ann_date": "2025-04-20", "source": "tracker"},
        {"period": "20250930", "roe_waa": 14.0, "ann_date": "2025-10-25", "source": "tracker"},
    ]
    f7 = tmp_path / "r7.json"
    f7.write_text(json.dumps(c7), encoding="utf-8")
    run7 = get_run(connect_workspace(ws_dir, "demo"), import_snapshot(ws_dir, f7, mode="demo"))
    assert run7 is not None and run7["health"] == "unverified"

    # Case 8: Missing risk_source on facts_usable row
    c8 = json.loads(json.dumps(base))
    del c8["rows"][0]["risk_source"]
    f8 = tmp_path / "r8.json"
    f8.write_text(json.dumps(c8), encoding="utf-8")
    run8 = get_run(connect_workspace(ws_dir, "demo"), import_snapshot(ws_dir, f8, mode="demo"))
    assert run8 is not None and run8["health"] == "unverified"

    # Case 9: Fake prefix in financial_source (e.g. tracker:fake)
    c9 = json.loads(json.dumps(base))
    c9["rows"][0]["financial_source"] = "tracker:fake"
    f9 = tmp_path / "r9.json"
    f9.write_text(json.dumps(c9), encoding="utf-8")
    run9 = get_run(connect_workspace(ws_dir, "demo"), import_snapshot(ws_dir, f9, mode="demo"))
    assert run9 is not None and run9["health"] == "unverified"

    # Case 10: Fake prefix in valuation_source / risk_source (e.g. tushare.fake)
    c10 = json.loads(json.dumps(base))
    c10["rows"][0]["valuation_source"] = "tushare.fake"
    f10 = tmp_path / "r10.json"
    f10.write_text(json.dumps(c10), encoding="utf-8")
    run10 = get_run(connect_workspace(ws_dir, "demo"), import_snapshot(ws_dir, f10, mode="demo"))
    assert run10 is not None and run10["health"] == "unverified"

    # Case 11: Out-of-range year 0 in annual_roes (caught safely, degrades to unverified)
    c11 = json.loads(json.dumps(base))
    c11["rows"][0]["annual_roes"] = [
        {"year": 0, "roe_waa": 10.0, "ann_date": "2024-04-15", "source": "tracker"},
        {"year": 1, "roe_waa": 12.0, "ann_date": "2025-04-20", "source": "tracker"},
        {"year": 2, "roe_waa": 14.0, "ann_date": "2026-04-15", "source": "tracker"},
    ]
    f11 = tmp_path / "r11.json"
    f11.write_text(json.dumps(c11), encoding="utf-8")
    run11 = get_run(connect_workspace(ws_dir, "demo"), import_snapshot(ws_dir, f11, mode="demo"))
    assert run11 is not None and run11["health"] == "unverified"

    # Case 12: Future ann_date (later than screened_at) degrades to unverified
    c12 = json.loads(json.dumps(base))
    c12["rows"][0]["annual_roes"][2]["ann_date"] = "2027-04-15"
    f12 = tmp_path / "r12.json"
    f12.write_text(json.dumps(c12), encoding="utf-8")
    run12 = get_run(connect_workspace(ws_dir, "demo"), import_snapshot(ws_dir, f12, mode="demo"))
    assert run12 is not None and run12["health"] == "unverified"

    # Case 13: ann_date before annual period end date degrades to unverified
    c13 = json.loads(json.dumps(base))
    c13["rows"][0]["annual_roes"][2]["ann_date"] = "2025-06-01"
    f13 = tmp_path / "r13.json"
    f13.write_text(json.dumps(c13), encoding="utf-8")
    run13 = get_run(connect_workspace(ws_dir, "demo"), import_snapshot(ws_dir, f13, mode="demo"))
    assert run13 is not None and run13["health"] == "unverified"


def test_screen_generated_snapshot_with_exclusions_and_errors_health_classification(
    tmp_path: Path,
) -> None:
    ws_dir = tmp_path / "screen_exclusions_ws"
    initialize(ws_dir, mode="demo", journal_mode="DELETE")

    anchor = "600001.SH"
    watchlist = [screen.base_code(anchor)]
    rows_clean = [
        # Anchor stock (qualified, in watchlist)
        {
            "code": "600001.SH",
            "name": "参照标的甲",
            "pb": 2.10,
            "valuation_source": "tushare.daily_basic",
            "valuation_date": "2026-09-20",
            "financial_source": "tracker:data/tushare-primary.db",
            "risk_source": "tushare.stock_basic.name",
            "annual_roes": [
                {
                    "period": "20231231",
                    "roe_waa": 12.0,
                    "source": "tracker",
                    "ann_date": "2024-04-15",
                    "update_flag": "0",
                },
                {
                    "period": "20241231",
                    "roe_waa": 14.0,
                    "source": "tracker",
                    "ann_date": "2025-04-20",
                    "update_flag": "0",
                },
                {
                    "period": "20251231",
                    "roe_waa": 16.0,
                    "source": "tracker",
                    "ann_date": "2026-04-15",
                    "update_flag": "0",
                },
            ],
            "roe_mean": 14.0,
            "exclusions": [],
            "facts_usable": True,
        },
        # Outside peer stock (qualified, outside watchlist)
        {
            "code": "600002.SH",
            "name": "同业标的乙",
            "pb": 1.50,
            "valuation_source": "tushare.daily_basic",
            "valuation_date": "2026-09-20",
            "financial_source": "tushare.fina_indicator",
            "risk_source": "tushare.stock_basic.name",
            "annual_roes": [
                {
                    "period": "20231231",
                    "roe_waa": 15.0,
                    "source": "tracker",
                    "ann_date": "2024-04-10",
                    "update_flag": "0",
                },
                {
                    "period": "20241231",
                    "roe_waa": 15.0,
                    "source": "tracker",
                    "ann_date": "2025-04-12",
                    "update_flag": "0",
                },
                {
                    "period": "20251231",
                    "roe_waa": 15.0,
                    "source": "tracker",
                    "ann_date": "2026-04-15",
                    "update_flag": "0",
                },
            ],
            "roe_mean": 15.0,
            "exclusions": [],
            "facts_usable": True,
        },
        # ST stock: financial fetch skipped by business rule, source is 'none'
        {
            "code": "600003.SH",
            "name": "*ST标的丙",
            "pb": 0.80,
            "valuation_source": "tushare.daily_basic",
            "valuation_date": "2026-09-20",
            "financial_source": "none",
            "risk_source": "tushare.stock_basic.name",
            "annual_roes": [],
            "roe_mean": None,
            "exclusions": ["KNOWN_ST_WARNING"],
            "facts_usable": False,
        },
        # Invalid PB stock: business excluded cleanly
        {
            "code": "600004.SH",
            "name": "标的丁",
            "pb": -1.0,
            "valuation_source": "tushare.daily_basic",
            "valuation_date": "2026-09-20",
            "financial_source": "none",
            "risk_source": "tushare.stock_basic.name",
            "annual_roes": [],
            "roe_mean": None,
            "exclusions": ["INVALID_PB"],
            "facts_usable": False,
        },
    ]

    # Fully sourced excluded rows make a valid complete baseline; omitted reports
    # on ST/invalid-PB rows are tested separately below.
    for row in rows_clean[2:]:
        row["financial_source"] = "tushare.fina_indicator"
        row["annual_roes"] = json.loads(json.dumps(rows_clean[0]["annual_roes"]))
        row["roe_mean"] = 14.0
    rows_clean[2]["facts_usable"] = True
    rows_clean[3]["facts_usable"] = True

    results_clean = screen.rank_peers(rows_clean, anchor, watchlist)
    scope = {
        "industry": "专用设备",
        "industry_source": "tushare.stock_basic",
        "enumerated_count": 4,
        "enumerated_codes": ["600001.SH", "600002.SH", "600003.SH", "600004.SH"],
        "selected_codes": ["600001.SH", "600002.SH", "600003.SH", "600004.SH"],
        "excluded_missing_valuation": [],
        "excluded_by_cap": [],
        "cap": 50,
        "valuation_date": "2026-09-20",
        "source": "tracker",
    }
    snapshot_clean_complete = {
        "schema_version": screen.SCHEMA_VERSION,
        "rule": screen.RULE,
        "source": "tracker",
        "limits": {
            "cap": screen.CAP,
            "top_n": screen.TOP_N,
            "max_report_age_days": screen.MAX_REPORT_AGE_DAYS,
        },
        "formula": "research_order=(roe_rank_desc+pb_rank_asc)/2; average ties",
        "screened_at": "2026-09-20T16:00:00+08:00",
        "generated_at": "2026-09-20T16:00:00+08:00",
        "data_date": "2026-09-20",
        "anchor": anchor,
        "watchlist_codes": watchlist,
        "scope": scope,
        "rows": rows_clean,
        "results": results_clean,
    }

    # 1. Clean complete discovery: all business exclusions known, no network errors, outside qualified company exists
    f_comp = tmp_path / "clean_comp.json"
    f_comp.write_text(json.dumps(snapshot_clean_complete, ensure_ascii=False), encoding="utf-8")
    run_comp_id = import_snapshot(ws_dir, f_comp, mode="demo")
    run_comp = get_run(connect_workspace(ws_dir, "demo"), run_comp_id)
    assert run_comp is not None
    assert run_comp["health"] == "complete"

    # Excluded but fully sourced ST reports are usable, never ranking-eligible.
    assert "600003.SH" not in results_clean["qualified_codes"]
    assert is_row_usable(rows_clean[2])
    # A stale flag cannot turn valid facts into a gap just because ranking excludes ST.
    assert is_row_usable({**rows_clean[2], "facts_usable": False})
    assert "600004.SH" not in results_clean["qualified_codes"]
    assert is_row_usable(rows_clean[3])  # Negative PB is a sourced fact, not a peer candidate.
    assert is_row_usable({**rows_clean[3], "pb": 0})
    for pb in (True, float("nan"), float("inf")):
        assert not is_row_usable({**rows_clean[3], "pb": pb})
    missing_source = json.loads(json.dumps(snapshot_clean_complete))
    missing_source["rows"][2]["financial_source"] = "none"
    missing_source["rows"][2]["facts_usable"] = False
    path = tmp_path / "st_roes_without_source.json"
    path.write_text(json.dumps(missing_source, ensure_ascii=False), encoding="utf-8")
    run = get_run(connect_workspace(ws_dir, "demo"), import_snapshot(ws_dir, path, "demo"))
    assert run is not None and run["health"] == "unverified"

    # Name alone is an ST risk fact, even when risk_status is absent.
    for risk_source in ("none", "tushare.stock_basic:error"):
        missing_risk = json.loads(json.dumps(snapshot_clean_complete))
        assert "risk_status" not in missing_risk["rows"][2]
        missing_risk["rows"][2]["risk_source"] = risk_source
        path = tmp_path / f"st_name_{risk_source}.json"
        path.write_text(json.dumps(missing_risk, ensure_ascii=False), encoding="utf-8")
        run = get_run(connect_workspace(ws_dir, "demo"), import_snapshot(ws_dir, path, "demo"))
        assert run is not None and run["health"] == "unverified"

    # PB on an excluded row also needs a real valuation source.
    missing_pb_source = json.loads(json.dumps(snapshot_clean_complete))
    missing_pb_source["rows"][2]["valuation_source"] = "none"
    path = tmp_path / "st_pb_without_source.json"
    path.write_text(json.dumps(missing_pb_source, ensure_ascii=False), encoding="utf-8")
    run = get_run(connect_workspace(ws_dir, "demo"), import_snapshot(ws_dir, path, "demo"))
    assert run is not None and run["health"] == "unverified"

    # Missing facts without a verified business exclusion cannot replace the full ranking.
    for field, value in (("pb", None), ("annual_roes", [])):
        missing = json.loads(json.dumps(snapshot_clean_complete))
        missing["rows"][0][field] = value
        missing["rows"][0]["facts_usable"] = False
        missing["results"] = screen.rank_peers(missing["rows"], anchor, watchlist)
        path = tmp_path / f"missing_{field}.json"
        path.write_text(json.dumps(missing, ensure_ascii=False), encoding="utf-8")
        run = get_run(connect_workspace(ws_dir, "demo"), import_snapshot(ws_dir, path, "demo"))
        assert run is not None and run["health"] == ("unverified" if field == "pb" else "partial")
        assert (
            present(get_latest_peer_run(connect_workspace(ws_dir, "demo"), anchor))["run_id"]
            == run_comp_id
        )

    # ST cannot mask a missing PB or missing financial data; neither can invalid PB
    # justify skipped annual reports. Source evidence is mandatory even for exclusions.
    for index, field, value in (
        (2, "pb", None),
        (2, "annual_roes", []),
        (3, "pb", None),
        (3, "annual_roes", []),
    ):
        missing = json.loads(json.dumps(snapshot_clean_complete))
        missing["rows"][index][field] = value
        missing["rows"][index]["facts_usable"] = False
        missing["results"] = screen.rank_peers(missing["rows"], anchor, watchlist)
        path = tmp_path / f"excluded_missing_{index}_{field}.json"
        path.write_text(json.dumps(missing, ensure_ascii=False), encoding="utf-8")
        run = get_run(connect_workspace(ws_dir, "demo"), import_snapshot(ws_dir, path, "demo"))
        assert run is not None and run["health"] == (
            "unverified" if field == "pb" and index == 2 else "partial"
        )
        assert (
            present(get_latest_peer_run(connect_workspace(ws_dir, "demo"), anchor))["run_id"]
            == run_comp_id
        )

    for source in ("valuation_source", "financial_source", "risk_source"):
        for source_value in (None, "unknown_source"):
            missing = json.loads(json.dumps(snapshot_clean_complete))
            if source_value is None:
                del missing["rows"][2][source]
            else:
                missing["rows"][2][source] = source_value
            path = tmp_path / f"excluded_{source}_{source_value}.json"
            path.write_text(json.dumps(missing, ensure_ascii=False), encoding="utf-8")
            run = get_run(connect_workspace(ws_dir, "demo"), import_snapshot(ws_dir, path, "demo"))
            assert run is not None and run["health"] == "unverified"

    # An unproven exclusion label cannot explain the missing PB either.
    unproven = json.loads(json.dumps(snapshot_clean_complete))
    unproven["rows"][0].update(pb=None, facts_usable=False, exclusions=["KNOWN_ST_WARNING"])
    unproven["results"] = screen.rank_peers(unproven["rows"], anchor, watchlist)
    f_unproven = tmp_path / "unproven_exclusion.json"
    f_unproven.write_text(json.dumps(unproven, ensure_ascii=False), encoding="utf-8")
    run = get_run(connect_workspace(ws_dir, "demo"), import_snapshot(ws_dir, f_unproven, "demo"))
    assert run is not None and run["health"] == "unverified"

    # 2. Network error on 600004.SH: degrades from complete to partial
    snapshot_with_error = json.loads(json.dumps(snapshot_clean_complete))
    snapshot_with_error["rows"][3]["financial_source"] = "tushare.fina_indicator:error"
    snapshot_with_error["rows"][3].update(annual_roes=[], roe_mean=None, facts_usable=False)
    snapshot_with_error["rows"][3]["exclusions"] = ["INVALID_PB", "SCREEN_ERROR: timeout"]
    snapshot_with_error["results"] = screen.rank_peers(
        snapshot_with_error["rows"], anchor, watchlist
    )
    f_err = tmp_path / "with_err.json"
    f_err.write_text(json.dumps(snapshot_with_error, ensure_ascii=False), encoding="utf-8")
    run_err_id = import_snapshot(ws_dir, f_err, mode="demo")
    run_err = get_run(connect_workspace(ws_dir, "demo"), run_err_id)
    assert run_err is not None
    assert run_err["health"] == "partial"

    for source_key, error_source in (
        ("valuation_source", "tushare.daily_basic:error"),
        ("risk_source", "tushare.stock_basic:error"),
    ):
        error = json.loads(json.dumps(snapshot_with_error))
        error["rows"][3]["financial_source"] = "none"
        if source_key == "valuation_source":
            error["rows"][3]["pb"] = None
        else:
            error["rows"][3]["name"] = ""  # No name or risk fact to substantiate.
        error["rows"][3][source_key] = error_source
        path = tmp_path / f"error_{source_key}.json"
        path.write_text(json.dumps(error, ensure_ascii=False), encoding="utf-8")
        run = get_run(connect_workspace(ws_dir, "demo"), import_snapshot(ws_dir, path, "demo"))
        assert run is not None and run["health"] == "partial"

        error["rows"][3][source_key] = "unknown:error"
        path.write_text(json.dumps(error, ensure_ascii=False), encoding="utf-8")
        run = get_run(connect_workspace(ws_dir, "demo"), import_snapshot(ws_dir, path, "demo"))
        assert run is not None and run["health"] == "unverified"

    # 3. Clean partial discovery: 600002.SH also in watchlist (0 outside qualified stocks)
    snapshot_partial = json.loads(json.dumps(snapshot_clean_complete))
    snapshot_partial["watchlist_codes"] = ["600001", "600002"]
    snapshot_partial["results"] = screen.rank_peers(
        snapshot_partial["rows"], anchor, ["600001", "600002"]
    )
    f_part = tmp_path / "clean_part.json"
    f_part.write_text(json.dumps(snapshot_partial, ensure_ascii=False), encoding="utf-8")
    run_part_id = import_snapshot(ws_dir, f_part, mode="demo")
    run_part = get_run(connect_workspace(ws_dir, "demo"), run_part_id)
    assert run_part is not None
    assert run_part["health"] == "partial"

    # 4. Zero candidate clean result: all candidates excluded by business conditions
    snapshot_zero = json.loads(json.dumps(snapshot_clean_complete))
    for r in snapshot_zero["rows"]:
        r["exclusions"] = ["KNOWN_ST_WARNING"]
        if r["pb"] <= 0:
            r["exclusions"].append("INVALID_PB")
        r["facts_usable"] = True
    snapshot_zero["results"] = screen.rank_peers(snapshot_zero["rows"], anchor, watchlist)
    assert len(snapshot_zero["results"]["ranking"]) == 0
    f_zero = tmp_path / "clean_zero.json"
    f_zero.write_text(json.dumps(snapshot_zero, ensure_ascii=False), encoding="utf-8")
    run_zero_id = import_snapshot(ws_dir, f_zero, mode="demo")
    run_zero = get_run(connect_workspace(ws_dir, "demo"), run_zero_id)
    assert run_zero is not None
    assert run_zero["health"] == "partial"
