import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Literal
from unittest.mock import patch

import pytest

import screen
from auth import AuthError, check_actor, create_demo_actor, create_owner_actor
from services import (
    ServiceError,
    _annual_facts,
    delete_watch,
    dismiss_update_job,
    get_company_context,
    get_home,
    get_peer_discover,
    get_update_job,
    list_update_jobs,
    mark_seen,
    read_verified_snapshot,
    request_peer_update,
    save_watch,
)
from workspace import (
    add_watch_item,
    connect_workspace,
    get_run,
    get_watch_item,
    import_snapshot,
    initialize,
    register_run,
)

FIXTURES_DIR = Path(__file__).parent / "tests" / "fixtures"


def present(value: dict | None) -> dict:
    assert value is not None
    return value


def test_auth_boundaries() -> None:
    demo = create_demo_actor()
    assert demo.is_valid
    assert check_actor(demo).user_id == "demo_user"

    owner = create_owner_actor("123456", "123456")
    assert owner.is_valid
    assert owner.user_id == "123456"

    # Mismatched GitHub ID
    with pytest.raises(AuthError, match="unauthorized GitHub user ID"):
        create_owner_actor("999999", "123456")

    # Non-numeric ID
    with pytest.raises(AuthError, match="numeric"):
        create_owner_actor("admin_user", "123456")

    # Revoked actor
    owner.revoke()
    assert not owner.is_valid
    with pytest.raises(AuthError, match="expired or revoked"):
        check_actor(owner)


def test_home_verifies_each_snapshot_once_per_request(tmp_path: Path) -> None:
    initialize(tmp_path, mode="demo", journal_mode="DELETE")
    first = import_snapshot(tmp_path, FIXTURES_DIR / "peer_complete_v1.json", "demo")
    latest = import_snapshot(tmp_path, FIXTURES_DIR / "peer_second_change.json", "demo")
    with connect_workspace(tmp_path, "demo") as conn:
        for code in ("600001.SH", "600002.SH"):
            add_watch_item(conn, code=code, name=code, added_run_id=first)
        latest_path = tmp_path / get_run(conn, latest)["snapshot_path"]

    with patch("services.read_verified_snapshot", wraps=read_verified_snapshot) as read:
        home = get_home(create_demo_actor(), tmp_path, "demo")
        assert home["total_watch_count"] == 2
        assert read.call_count == 2

        # An invalid snapshot must be detected on the next request, not hidden by a cache.
        latest_path.write_bytes(b"damaged")
        read.reset_mock()
        home = get_home(create_demo_actor(), tmp_path, "demo")
        assert read.call_count == 2
        assert all(
            item["change_summary"] == "最新快照文件损坏或无法读取，仍展示上次可用资料"
            for item in home["watch_items"]
        )


def test_home_uses_first_usable_ack_row_when_snapshot_has_duplicate_codes(
    tmp_path: Path,
) -> None:
    initialize(tmp_path, mode="demo", journal_mode="DELETE")
    original = json.loads((FIXTURES_DIR / "peer_complete_v1.json").read_text(encoding="utf-8"))
    baseline_pb = original["rows"][0]["pb"]
    original["rows"].insert(0, {"code": "600001.SH", "pb": None})
    latest = json.loads((FIXTURES_DIR / "peer_complete_v1.json").read_text(encoding="utf-8"))
    latest["rows"][0]["pb"] = baseline_pb + 1
    (tmp_path / "snapshots").mkdir()
    with connect_workspace(tmp_path, "demo") as conn:
        for run_id, snap, day in (("ack", original, "20"), ("new", latest, "21")):
            raw = json.dumps(snap, ensure_ascii=False).encode("utf-8")
            rel = f"snapshots/{run_id}.json"
            (tmp_path / rel).write_bytes(raw)
            register_run(
                conn,
                run_id=run_id,
                kind="peer",
                anchor_code="600001.SH",
                rule_id="peer-screen-v1",
                captured_at=f"2026-09-{day}T16:00:00+08:00",
                valuation_date=f"2026-09-{day}",
                health="complete",
                snapshot_path=rel,
                snapshot_bytes=raw,
            )
        add_watch_item(conn, code="600001.SH", name="示例公司甲", added_run_id="ack")
        conn.execute(
            "UPDATE watch_items SET ack_run_id='ack', ack_at='2026-09-20T16:00:00+08:00' "
            "WHERE code='600001.SH'"
        )

    item = get_home(create_demo_actor(), tmp_path, "demo")["watch_items"][0]
    assert item["change_summary"] == f"PB变动: {baseline_pb} → {baseline_pb + 1}"


def test_service_workflows(tmp_path: Path) -> None:
    ws_dir = tmp_path / "svc_ws"
    initialize(ws_dir, mode="demo", journal_mode="DELETE")

    run_v1 = import_snapshot(ws_dir, FIXTURES_DIR / "peer_complete_v1.json", mode="demo")
    run_v2 = import_snapshot(ws_dir, FIXTURES_DIR / "peer_second_change.json", mode="demo")

    actor = create_demo_actor()

    # 1. Home is initially empty
    home = get_home(actor, ws_dir, mode="demo")
    assert home["total_watch_count"] == 0
    assert home["needs_review_count"] == 0

    # 2. Add watch item for 600001.SH
    saved = save_watch(
        actor,
        code="600001.SH",
        source_run_id=run_v1,
        fields={"status": "research", "reason": "初次加入，观察高ROE"},
        expected_revision=0,
        state_dir=ws_dir,
        mode="demo",
    )
    assert saved["code"] == "600001.SH"
    assert saved["revision"] == 2

    # 3. Duplicate add with expected_revision=0 MUST NOT overwrite existing notes
    dup = save_watch(
        actor,
        code="600001.SH",
        source_run_id=run_v1,
        fields={"status": "paused", "reason": "试图恶意覆盖"},
        expected_revision=0,
        state_dir=ws_dir,
        mode="demo",
    )
    assert dup["status"] == "research"
    assert dup["reason"] == "初次加入，观察高ROE"
    assert dup["revision"] == 2

    # 4. Source run must actually contain the stock code
    with pytest.raises(ServiceError, match="does not contain stock"):
        save_watch(
            actor,
            code="600999.SH",
            source_run_id=run_v1,
            fields={"status": "observe"},
            expected_revision=0,
            state_dir=ws_dir,
            mode="demo",
        )

    # 5. Home now shows 1 item needing review (首次待阅)
    home2 = get_home(actor, ws_dir, mode="demo")
    assert home2["total_watch_count"] == 1
    assert home2["needs_review_count"] == 1
    assert home2["watch_items"][0]["change_summary"] == "首次待阅"

    # 6. Company context with and without personal notes
    ctx_no_notes = get_company_context(
        actor, "600001.SH", ws_dir, mode="demo", include_personal_notes=False
    )
    assert ctx_no_notes["is_watched"] is True
    assert ctx_no_notes["reason"] == ""
    assert ctx_no_notes["has_usable_facts"] is True

    ctx_with_notes = get_company_context(
        actor, "600001.SH", ws_dir, mode="demo", include_personal_notes=True
    )
    assert ctx_with_notes["reason"] == "初次加入，观察高ROE"
    assert ctx_with_notes["watch_status"] == "research"

    # 7. Mark seen up to displayed run (run_v2)
    seen = mark_seen(
        actor,
        code="600001.SH",
        displayed_run_id=run_v2,
        expected_revision=2,
        state_dir=ws_dir,
        mode="demo",
    )
    assert seen["revision"] == 3
    assert seen["ack_run_id"] == run_v2

    # 8. Peer discovery
    peer_res = get_peer_discover(actor, "600001.SH", ws_dir, mode="demo")
    assert peer_res["has_run"] is True
    assert len(peer_res["rows"]) == 4


def test_excluded_company_retains_usable_facts_and_can_be_acknowledged(tmp_path: Path) -> None:
    ws_dir = tmp_path / "excluded_ws"
    initialize(ws_dir, mode="demo", journal_mode="DELETE")
    actor = create_demo_actor()
    snap = json.loads((FIXTURES_DIR / "peer_complete_v1.json").read_text(encoding="utf-8"))
    row = snap["rows"][2]
    row["name"] = "*ST示例公司丙"
    row["exclusions"] = ["KNOWN_ST_WARNING"]
    snap["results"] = screen.rank_peers(
        snap["rows"], snap["anchor"], [screen.base_code(c) for c in snap["watchlist_codes"]]
    )
    assert row["code"] not in snap["results"]["qualified_codes"]
    path = tmp_path / "excluded.json"
    path.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
    run_id = import_snapshot(ws_dir, path, "demo")
    conn = connect_workspace(ws_dir, "demo")
    try:
        assert present(get_run(conn, run_id))["health"] == "complete"
    finally:
        conn.close()

    saved = save_watch(actor, row["code"], run_id, {}, 0, ws_dir, "demo")
    home = get_home(actor, ws_dir, "demo")
    assert home["watch_items"][0]["change_summary"] == "首次待阅"
    ctx = get_company_context(actor, row["code"], ws_dir, "demo")
    assert ctx["has_usable_facts"] is True
    assert ctx["usable_fact"]["pb"] == row["pb"]
    assert ctx["displayed_run_id"] == run_id
    assert ctx["peer_rank"] is None
    seen = mark_seen(actor, row["code"], run_id, saved["revision"], ws_dir, "demo")
    assert seen["ack_run_id"] == run_id
    snap["screened_at"] = "2026-09-21T16:00:00+08:00"
    snap["generated_at"] = snap["screened_at"]
    row["pb"] = 1.6
    path.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
    newer_run = import_snapshot(ws_dir, path, "demo")
    conn = connect_workspace(ws_dir, "demo")
    try:
        assert present(get_run(conn, newer_run))["health"] == "complete"
    finally:
        conn.close()
    home = get_home(actor, ws_dir, "demo")
    assert home["watch_items"][0]["change_summary"] == "PB变动: 1.5 → 1.6"
    ctx = get_company_context(actor, row["code"], ws_dir, "demo")
    assert ctx["displayed_run_id"] == newer_run
    assert ctx["usable_fact"]["pb"] == 1.6


@pytest.mark.parametrize("pb", [-1.2, 0.0])
@pytest.mark.parametrize("financial_source", ["local", "online"])
def test_generated_nonpositive_pb_facts_can_be_acknowledged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pb: float, financial_source: str
) -> None:
    tracker = tmp_path / "tracker"
    stocks = [
        {
            "ts_code": code,
            "name": name,
            "industry": "专用设备",
            "market": "主板",
            "exchange": "SSE",
            "list_status": "L",
        }
        for code, name in [("600001.SH", "参照"), ("600002.SH", "同业"), ("600003.SH", "非正PB")]
    ]
    valuations = [
        {"ts_code": stock["ts_code"], "pb": value, "total_mv": 100 - index * 10}
        for index, (stock, value) in enumerate(zip(stocks, [2.0, 1.5, pb]))
    ]
    reports = [
        {
            "end_date": f"{year}1231",
            "ann_date": f"{year + 1}0415",
            "roe_waa": 10.0,
            "update_flag": "0",
            "source": "tushare.fina_indicator",
            "acquired_at": "2026-09-20T16:00:00+08:00",
        }
        for year in (2023, 2024, 2025)
    ]
    online_calls: list[str] = []

    def online(_client, _token, code, _data_date, _screened_at):
        online_calls.append(code)
        return reports

    monkeypatch.setattr(screen, "fetch_financials", online)
    monkeypatch.setattr(screen.time, "sleep", lambda _seconds: None)
    if financial_source == "local":
        db = tracker / "data" / "tushare-primary.db"
        db.parent.mkdir(parents=True)
        with sqlite3.connect(db) as conn:
            conn.execute(
                "CREATE TABLE financial_observations (record_key TEXT, payload_json TEXT, source TEXT, endpoint TEXT, code TEXT)"
            )
            conn.execute("CREATE TABLE observation_events (record_key TEXT, observed_at TEXT)")
            for stock in stocks:
                for report in reports:
                    key = f"{stock['ts_code']}-{report['end_date']}"
                    conn.execute(
                        "INSERT INTO financial_observations VALUES (?,?,?,?,?)",
                        (
                            key,
                            json.dumps(report),
                            report["source"],
                            "fina_indicator",
                            screen.base_code(stock["ts_code"]),
                        ),
                    )
                    conn.execute(
                        "INSERT INTO observation_events VALUES (?,?)", (key, report["acquired_at"])
                    )

    monkeypatch.setitem(
        sys.modules, "tushare", SimpleNamespace(pro_api=lambda *_a, **_kw: object())
    )
    monkeypatch.setattr(
        screen,
        "load_reference",
        lambda *_a: {
            "anchor": "600001.SH",
            "watchlist_codes": ["600001"],
            "legacy_reference": {"industry": "专用设备", "name": "参照"},
        },
    )
    monkeypatch.setattr(screen, "read_token", lambda *_a: "synthetic")
    monkeypatch.setattr(
        screen,
        "fetch_universe",
        lambda *_a: (
            stocks,
            valuations,
            {
                "stock_basic_acquired_at": "2026-09-20T16:00:00+08:00",
                "daily_basic_acquired_at": "2026-09-20T16:00:00+08:00",
            },
        ),
    )
    snap = screen.build_live_snapshot(tracker, "600001.SH", "2026-09-20")
    row = next(r for r in snap["rows"] if r["code"] == "600003.SH")
    expected_source = (
        "tracker:data/tushare-primary.db"
        if financial_source == "local"
        else "tushare.fina_indicator"
    )
    assert row["pb"] == pb
    assert row["exclusions"] == ["INVALID_PB"]
    assert row["financial_source"] == expected_source
    assert [r["roe_waa"] for r in row["annual_roes"]] == [10.0] * 3
    assert row["roe_mean"] == 10.0
    assert ("600003.SH" in online_calls) is (financial_source == "online")
    baseline = screen.rank_peers(snap["rows"][:-1], snap["anchor"], ["600001"])
    assert snap["results"] == baseline  # exclusion must not change ranking or top

    ws_dir = tmp_path / "negative_pb_ws"
    initialize(ws_dir, mode="demo", journal_mode="DELETE")
    path = tmp_path / "generated.json"
    path.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
    run_id = import_snapshot(ws_dir, path, "demo")
    conn = connect_workspace(ws_dir, "demo")
    try:
        run = get_run(conn, run_id)
        assert run is not None and run["health"] == "complete"
    finally:
        conn.close()

    actor = create_demo_actor()
    saved = save_watch(actor, row["code"], run_id, {}, 0, ws_dir, "demo")
    ctx = get_company_context(actor, row["code"], ws_dir, "demo")
    assert ctx["has_usable_facts"] is True
    assert ctx["usable_fact"]["pb"] == pb
    assert ctx["usable_fact"]["financial_source"] == expected_source
    assert [r["roe"] for r in ctx["usable_fact"]["annual_roes"]] == [10.0] * 3
    assert ctx["displayed_run_id"] == run_id
    assert ctx["peer_rank"] is None
    seen = mark_seen(actor, row["code"], ctx["displayed_run_id"], saved["revision"], ws_dir, "demo")
    assert seen["ack_run_id"] == run_id


def test_snapshot_verification_and_tampering(tmp_path: Path) -> None:
    ws_dir = tmp_path / "tamper_ws"
    initialize(ws_dir, mode="demo", journal_mode="DELETE")

    run_id = import_snapshot(ws_dir, FIXTURES_DIR / "peer_complete_v1.json", mode="demo")
    conn = ws_dir / "workspace.sqlite3"

    import sqlite3

    c = sqlite3.connect(conn)
    row = c.execute(
        "SELECT snapshot_path, snapshot_sha256 FROM screen_runs WHERE run_id=?", (run_id,)
    ).fetchone()
    c.close()

    snap_file = ws_dir / row[0]
    assert snap_file.is_file()

    # Valid read succeeds
    data = read_verified_snapshot(ws_dir, row[0], row[1])
    assert data["anchor"] == "600001.SH"

    # Tampered file raises hash mismatch error
    with pytest.raises(ServiceError, match="snapshot hash mismatch"):
        read_verified_snapshot(ws_dir, row[0], "0" * 64)

    # Path traversal rejected
    with pytest.raises(ServiceError, match="traversal"):
        read_verified_snapshot(ws_dir, "../../../etc/passwd")


def test_unverified_run_excluded_from_facts(tmp_path: Path) -> None:
    ws_dir = tmp_path / "unverified_ws"
    initialize(ws_dir, mode="demo", journal_mode="DELETE")

    run_unverified = import_snapshot(
        ws_dir, FIXTURES_DIR / "peer_unverified_old_rule.json", mode="demo"
    )
    assert run_unverified.startswith("peer_600001_SH_")
    actor = create_demo_actor()

    # Company context must not treat unverified run as usable facts
    ctx = get_company_context(actor, "600001.SH", ws_dir, mode="demo")
    assert ctx["has_usable_facts"] is False
    assert ctx["usable_fact"] is None
    assert ctx["displayed_run_id"] is None


def test_save_watch_rejects_unverified_source(tmp_path: Path) -> None:
    ws_dir = tmp_path / "unverified_source_ws"
    initialize(ws_dir, mode="demo", journal_mode="DELETE")
    run_id = import_snapshot(ws_dir, FIXTURES_DIR / "peer_unverified_old_rule.json", "demo")
    conn = connect_workspace(ws_dir, "demo")
    try:
        assert present(get_run(conn, run_id))["health"] == "unverified"
    finally:
        conn.close()

    with pytest.raises(ServiceError, match=f"cannot add watch item from unverified run: {run_id}"):
        save_watch(create_demo_actor(), "600001.SH", run_id, {}, 0, ws_dir, "demo")
    conn = connect_workspace(ws_dir, "demo")
    try:
        assert get_watch_item(conn, "600001.SH") is None
    finally:
        conn.close()


def test_missing_unverified_history_does_not_block_company_facts(tmp_path: Path) -> None:
    ws_dir = tmp_path / "missing_unverified_ws"
    initialize(ws_dir, mode="demo", journal_mode="DELETE")
    old_id = import_snapshot(ws_dir, FIXTURES_DIR / "peer_unverified_old_rule.json", "demo")
    current_id = import_snapshot(ws_dir, FIXTURES_DIR / "peer_complete_v1.json", "demo")
    conn = connect_workspace(ws_dir, "demo")
    try:
        old_run = get_run(conn, old_id)
        assert old_run is not None and old_run["health"] == "unverified"
        assert present(get_run(conn, current_id))["health"] == "complete"
    finally:
        conn.close()
    (ws_dir / old_run["snapshot_path"]).unlink()

    ctx = get_company_context(create_demo_actor(), "600001.SH", ws_dir, "demo")
    assert ctx["displayed_run_id"] == current_id
    assert ctx["latest_attempt_run_id"] == current_id
    assert ctx["has_usable_facts"] is True
    assert ctx["usable_fact"]["pb"] == 1.85


@pytest.mark.parametrize("old_health", ["complete", "partial"])
@pytest.mark.parametrize("damage", ["missing", "hash_mismatch"])
def test_broken_verified_history_does_not_hide_current_facts(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    old_health: Literal["complete", "partial"],
    damage: str,
) -> None:
    ws_dir = tmp_path / "broken_history_ws"
    initialize(ws_dir, mode="demo", journal_mode="DELETE")
    actor = create_demo_actor()
    old_id = f"old_{old_health}"
    raw = json.dumps(
        json.loads((FIXTURES_DIR / "peer_complete_v1.json").read_text(encoding="utf-8")),
        ensure_ascii=False,
    ).encode("utf-8")
    path = f"snapshots/{old_id}.json"
    (ws_dir / path).parent.mkdir(parents=True, exist_ok=True)
    (ws_dir / path).write_bytes(raw)
    conn = connect_workspace(ws_dir, "demo")
    try:
        register_run(
            conn,
            run_id=old_id,
            kind="peer",
            anchor_code="600001.SH",
            rule_id="peer-screen-v1",
            captured_at="2026-09-19T16:00:00+08:00",
            valuation_date="2026-09-19",
            health=old_health,
            snapshot_path=path,
            snapshot_bytes=raw,
        )
        conn.commit()
    finally:
        conn.close()

    saved = save_watch(actor, "600001.SH", old_id, {}, 0, ws_dir, "demo")
    mark_seen(actor, "600001.SH", old_id, saved["revision"], ws_dir, "demo")
    current_id = import_snapshot(ws_dir, FIXTURES_DIR / "peer_complete_v1.json", "demo")
    conn = connect_workspace(ws_dir, "demo")
    try:
        old_run = get_run(conn, old_id)
        assert old_run is not None and old_run["health"] == old_health
        assert present(get_run(conn, current_id))["health"] == "complete"
    finally:
        conn.close()
    old_file = ws_dir / old_run["snapshot_path"]
    if damage == "missing":
        old_file.unlink()
    else:
        old_file.write_bytes(b"tampered")

    ctx = get_company_context(actor, "600001.SH", ws_dir, "demo")
    assert ctx["displayed_run_id"] == current_id
    assert ctx["latest_attempt_run_id"] == current_id
    assert ctx["has_usable_facts"] is True
    home = get_home(actor, ws_dir, "demo")
    assert home["total_watch_count"] == 1
    assert home["watch_items"][0]["code"] == "600001.SH"
    assert old_id in caplog.text
    assert "Skipping invalid snapshot" in caplog.text


def test_newer_unverified_run_does_not_create_company_gap(tmp_path: Path) -> None:
    ws_dir = tmp_path / "unverified_newer_ws"
    initialize(ws_dir, mode="demo", journal_mode="DELETE")
    actor = create_demo_actor()
    verified_id = import_snapshot(ws_dir, FIXTURES_DIR / "peer_complete_v1.json", "demo")
    saved = save_watch(actor, "600001.SH", verified_id, {}, 0, ws_dir, "demo")

    snap = json.loads((FIXTURES_DIR / "peer_complete_v1.json").read_text(encoding="utf-8"))
    snap["rule"] = "peer-screen-v0-legacy"
    snap["screened_at"] = "2026-09-21T16:00:00+08:00"
    snap["generated_at"] = snap["screened_at"]
    snap["rows"][0]["pb"] = None
    newer_path = tmp_path / "newer_unverified.json"
    newer_path.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
    unverified_id = import_snapshot(ws_dir, newer_path, "demo")
    conn = connect_workspace(ws_dir, "demo")
    try:
        assert present(get_run(conn, unverified_id))["health"] == "unverified"
    finally:
        conn.close()

    ctx = get_company_context(actor, "600001.SH", ws_dir, "demo")
    assert ctx["latest_attempt_run_id"] == verified_id
    assert ctx["displayed_run_id"] == verified_id
    assert ctx["usable_fact"]["pb"] == 1.85
    assert ctx["has_latest_attempt_gap"] is False
    seen = mark_seen(actor, "600001.SH", ctx["displayed_run_id"], saved["revision"], ws_dir, "demo")
    assert seen["ack_run_id"] == verified_id


def test_save_watch_idempotent_retry(tmp_path: Path) -> None:
    ws_dir = tmp_path / "idempotent_ws"
    initialize(ws_dir, mode="demo", journal_mode="DELETE")
    run_id = import_snapshot(ws_dir, FIXTURES_DIR / "peer_complete_v1.json", mode="demo")
    actor = create_demo_actor()

    # Initial save at revision 0 -> revision becomes 2 (added with revision 1 then updated to revision 2)
    saved1 = save_watch(
        actor=actor,
        code="600001.SH",
        source_run_id=run_id,
        fields={"status": "research", "reason": "测试理由A"},
        expected_revision=0,
        state_dir=ws_dir,
        mode="demo",
    )
    assert saved1["revision"] == 2
    assert saved1["reason"] == "测试理由A"

    # Retry exact same request with expected_revision=0 (e.g. client network retry):
    # Should idempotently succeed and return the saved record!
    retry = save_watch(
        actor=actor,
        code="600001.SH",
        source_run_id=run_id,
        fields={"status": "research", "reason": "测试理由A"},
        expected_revision=0,
        state_dir=ws_dir,
        mode="demo",
    )
    assert retry["revision"] == 2
    assert retry["reason"] == "测试理由A"

    # Update to revision 3
    saved2 = save_watch(
        actor=actor,
        code="600001.SH",
        source_run_id=run_id,
        fields={"status": "observe", "reason": "更新理由B"},
        expected_revision=2,
        state_dir=ws_dir,
        mode="demo",
    )
    assert saved2["revision"] == 3

    # Retry update with old expected_revision=2 and same content:
    # Must succeed idempotently!
    retry2 = save_watch(
        actor=actor,
        code="600001.SH",
        source_run_id=run_id,
        fields={"status": "observe", "reason": "更新理由B"},
        expected_revision=2,
        state_dir=ws_dir,
        mode="demo",
    )
    assert retry2["revision"] == 3
    assert retry2["reason"] == "更新理由B"


def test_transactional_auth_revocation(tmp_path: Path) -> None:
    ws_dir = tmp_path / "revoke_ws"
    initialize(ws_dir, mode="demo", journal_mode="DELETE")
    run_id = import_snapshot(ws_dir, FIXTURES_DIR / "peer_complete_v1.json", mode="demo")
    actor = create_demo_actor()
    actor.revoke()

    with pytest.raises(AuthError, match="authorization"):
        save_watch(
            actor=actor,
            code="600001.SH",
            source_run_id=run_id,
            fields={"status": "research", "reason": "未授权测试"},
            expected_revision=0,
            state_dir=ws_dir,
            mode="demo",
        )

    with pytest.raises(AuthError, match="authorization"):
        mark_seen(
            actor=actor,
            code="600001.SH",
            displayed_run_id=run_id,
            expected_revision=1,
            state_dir=ws_dir,
            mode="demo",
        )


def test_services_defensive_against_corrupted_snapshot_rows(tmp_path: Path) -> None:
    ws_dir = tmp_path / "corrupted_snap_ws"
    initialize(ws_dir, mode="demo", journal_mode="DELETE")
    actor = create_demo_actor()

    bad_snap = {
        "rows": None,
        "results": {"ranking": None},
    }
    raw = json.dumps(bad_snap).encode("utf-8")
    rel_path = "snapshots/bad.json"
    (ws_dir / "snapshots").mkdir(parents=True, exist_ok=True)
    (ws_dir / rel_path).write_bytes(raw)

    conn = connect_workspace(ws_dir, "demo")
    register_run(
        conn,
        run_id="bad_run",
        kind="peer",
        anchor_code="600001.SH",
        rule_id="unknown",
        captured_at="2026-09-20T16:00:00+08:00",
        valuation_date="2026-09-20",
        health="unverified",
        snapshot_path=rel_path,
        snapshot_bytes=raw,
    )
    conn.commit()
    conn.close()

    ctx = get_company_context(actor, "600001.SH", ws_dir, "demo")
    assert ctx["code"] == "600001.SH"
    assert ctx["usable_fact"] is None

    home = get_home(actor, ws_dir, "demo")
    assert home["total_watch_count"] == 0

    disc = get_peer_discover(actor, "600001.SH", ws_dir, "demo")
    assert disc["has_run"] is False
    assert "暂无" in disc["message"]


def test_get_peer_discover_defensive_against_malformed_ranking(tmp_path: Path) -> None:
    ws_dir = tmp_path / "malformed_ranking_ws"
    initialize(ws_dir, mode="demo", journal_mode="DELETE")
    actor = create_demo_actor()

    # Test 1: results is None
    snap1: dict = {"results": None, "rows": []}
    raw1 = json.dumps(snap1).encode("utf-8")
    rel_path1 = "snapshots/bad1.json"
    (ws_dir / "snapshots").mkdir(parents=True, exist_ok=True)
    (ws_dir / rel_path1).write_bytes(raw1)

    conn = connect_workspace(ws_dir, "demo")
    register_run(
        conn,
        run_id="bad_run1",
        kind="peer",
        anchor_code="600001.SH",
        rule_id="unknown",
        captured_at="2026-09-20T16:00:00+08:00",
        valuation_date="2026-09-20",
        health="complete",
        snapshot_path=rel_path1,
        snapshot_bytes=raw1,
    )
    conn.commit()
    conn.close()

    disc1 = get_peer_discover(actor, "600001.SH", ws_dir, "demo")
    assert disc1["has_run"] is True
    assert disc1["results"]["ranking"] == []

    # Test 2: ranking contains malformed/non-dict elements and null
    snap2 = {
        "results": {
            "ranking": [
                None,
                "not_a_dict",
                123,
                {"code": "600002.SH", "position": 2},
            ]
        },
        "rows": [],
    }
    raw2 = json.dumps(snap2).encode("utf-8")
    rel_path2 = "snapshots/bad2.json"
    (ws_dir / rel_path2).write_bytes(raw2)

    conn = connect_workspace(ws_dir, "demo")
    register_run(
        conn,
        run_id="bad_run2",
        kind="peer",
        anchor_code="600002.SH",
        rule_id="unknown",
        captured_at="2026-09-20T17:00:00+08:00",
        valuation_date="2026-09-20",
        health="complete",
        snapshot_path=rel_path2,
        snapshot_bytes=raw2,
    )
    conn.commit()
    conn.close()

    disc2 = get_peer_discover(actor, "600002.SH", ws_dir, "demo")
    assert disc2["has_run"] is True
    assert disc2["results"]["ranking"] == [{"code": "600002.SH", "position": 2}]


def test_home_unusable_partial_row_does_not_report_fact_changes(tmp_path: Path) -> None:
    ws_dir = tmp_path / "home_gap_ws"
    initialize(ws_dir, mode="demo", journal_mode="DELETE")
    actor = create_demo_actor()
    base = json.loads((FIXTURES_DIR / "peer_complete_v1.json").read_text(encoding="utf-8"))
    run_id = import_snapshot(ws_dir, FIXTURES_DIR / "peer_complete_v1.json", "demo")
    saved = save_watch(actor, "600001.SH", run_id, {}, 0, ws_dir, "demo")

    def add_partial(run: str, hour: int, row: dict) -> None:
        snap = {"run_id": run, "rows": [row], "results": {"ranking": []}}
        raw = json.dumps(snap).encode("utf-8")
        path = f"snapshots/{run}.json"
        (ws_dir / path).write_bytes(raw)
        conn = connect_workspace(ws_dir, "demo")
        try:
            register_run(
                conn,
                run_id=run,
                kind="peer",
                anchor_code="600001.SH",
                rule_id="peer-screen-v1",
                captured_at=f"2026-09-21T{hour:02}:00:00+08:00",
                valuation_date="2026-09-21",
                health="partial",
                snapshot_path=path,
                snapshot_bytes=raw,
            )
            conn.commit()
        finally:
            conn.close()

    original = base["rows"][0]
    for hour, row, summary in (
        (8, {**original, "pb": None, "facts_usable": False}, "首次待阅（本次数据缺口）"),
        (
            9,
            {**original, "annual_roes": [], "facts_usable": False, "financial_status": "failed"},
            "首次待阅（本次财务更新失败）",
        ),
    ):
        add_partial(f"first_gap_{hour}", hour, row)
        home = get_home(actor, ws_dir, "demo")
        assert home["watch_items"][0]["has_change"] is True
        assert home["watch_items"][0]["change_summary"] == summary
        assert home["needs_review_count"] == 1
        ctx = get_company_context(actor, "600001.SH", ws_dir, "demo")
        assert ctx["has_latest_attempt_gap"] is True
        assert ctx["latest_attempt_run_id"] == f"first_gap_{hour}"
        assert ctx["latest_attempt_date"] == "2026-09-21"
        assert ctx["displayed_run_id"] == run_id
        assert ctx["usable_valuation_date"] == "2026-09-20"

    with pytest.raises(
        ServiceError, match="cannot acknowledge stale run when newer run has data gap"
    ):
        mark_seen(actor, "600001.SH", run_id, saved["revision"], ws_dir, "demo")
    conn = connect_workspace(ws_dir, "demo")
    try:
        assert present(get_watch_item(conn, "600001.SH"))["ack_run_id"] is None
    finally:
        conn.close()
    missing_pb = {**original, "pb": None, "facts_usable": False}
    add_partial("gap_pb", 10, missing_pb)
    home = get_home(actor, ws_dir, "demo")
    assert home["watch_items"][0]["change_summary"] == "首次待阅（本次数据缺口）"
    assert home["needs_review_count"] == 1

    missing_roes = {**original, "pb": 1.95, "annual_roes": [], "facts_usable": False}
    add_partial("gap_roes", 11, missing_roes)
    home = get_home(actor, ws_dir, "demo")
    assert home["watch_items"][0]["change_summary"] == "首次待阅（本次数据缺口）"

    changed = {**original, "pb": 1.95}
    add_partial("usable_after_gap", 12, changed)
    home = get_home(actor, ws_dir, "demo")
    assert home["watch_items"][0]["change_summary"] == "首次待阅"

    missing_financial = {
        **changed,
        "annual_roes": [],
        "facts_usable": False,
        "financial_status": "failed",
    }
    add_partial("gap_financial", 13, missing_financial)
    home = get_home(actor, ws_dir, "demo")
    assert home["watch_items"][0]["change_summary"] == "首次待阅（本次财务更新失败）"


@pytest.mark.parametrize("gap_row", ["unusable", "missing"])
def test_mark_seen_rejects_new_gap_after_page_open(tmp_path: Path, gap_row: str) -> None:
    ws_dir = tmp_path / "stale_ws"
    initialize(ws_dir, mode="demo", journal_mode="DELETE")
    actor = create_demo_actor()
    run_id = import_snapshot(ws_dir, FIXTURES_DIR / "peer_complete_v1.json", "demo")
    saved = save_watch(actor, "600001.SH", run_id, {}, 0, ws_dir, "demo")
    displayed = get_company_context(actor, "600001.SH", ws_dir, "demo")
    assert displayed["displayed_run_id"] == run_id
    base = json.loads((FIXTURES_DIR / "peer_complete_v1.json").read_text(encoding="utf-8"))
    rows = [{**base["rows"][0], "annual_roes": []}] if gap_row == "unusable" else []
    raw = json.dumps({"anchor": "600001.SH", "rows": rows}).encode("utf-8")
    path = "snapshots/new_gap.json"
    (ws_dir / path).write_bytes(raw)
    conn = connect_workspace(ws_dir, "demo")
    try:
        register_run(
            conn,
            run_id="new_gap",
            kind="peer",
            anchor_code="600001.SH",
            rule_id="peer-screen-v1",
            captured_at="2026-09-21T16:00:00+00:00",
            valuation_date="2026-09-21",
            health="partial",
            snapshot_path=path,
            snapshot_bytes=raw,
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(
        ServiceError, match="cannot acknowledge stale run when newer run has data gap"
    ):
        mark_seen(
            actor, "600001.SH", displayed["displayed_run_id"], saved["revision"], ws_dir, "demo"
        )
    conn = connect_workspace(ws_dir, "demo")
    try:
        item = get_watch_item(conn, "600001.SH")
        assert item is not None
        assert item["ack_run_id"] is None and item["revision"] == saved["revision"]
    finally:
        conn.close()


@pytest.mark.parametrize("health", ["complete", "partial"])
@pytest.mark.parametrize("kind", ["peer", "watch"])
def test_home_reports_newer_relevant_damaged_snapshot(
    tmp_path: Path, health: Literal["complete", "partial"], kind: Literal["peer", "watch"]
) -> None:
    initialize(tmp_path, mode="demo", journal_mode="DELETE")
    actor = create_demo_actor()
    first = import_snapshot(tmp_path, FIXTURES_DIR / "peer_complete_v1.json", "demo")
    saved = save_watch(actor, "600001.SH", first, {}, 0, tmp_path, "demo")
    mark_seen(actor, "600001.SH", first, saved["revision"], tmp_path, "demo")

    raw = b'{"rows": [{"code": "600001.SH"}]}'
    path = tmp_path / "snapshots" / "new.json"
    path.write_bytes(raw)
    with connect_workspace(tmp_path, "demo") as conn:
        register_run(
            conn,
            run_id="new_damaged",
            kind=kind,
            anchor_code="600001.SH" if kind == "peer" else None,
            rule_id="peer-screen-v1" if kind == "peer" else None,
            captured_at="2026-09-21T16:00:00+08:00",
            valuation_date="2026-09-21",
            health=health,
            snapshot_path="snapshots/new.json",
            snapshot_bytes=raw,
        )
        if kind == "watch":
            conn.execute(
                """INSERT INTO update_jobs
                (job_id,request_id,kind,payload_json,dedupe_key,status,requested_at,
                 updated_at,finished_at,result_run_id) VALUES (?,?,?,?,?,'succeeded',?,?,?,?)""",
                (
                    "new_damaged",
                    "request_new_damaged",
                    "watch",
                    json.dumps({"expected_codes": ["600001.SH"]}),
                    "watch_dedupe",
                    "2026-09-21",
                    "2026-09-21",
                    "2026-09-21",
                    "new_damaged",
                ),
            )
    path.write_bytes(b"corrupt")

    home = get_home(actor, tmp_path, "demo")
    assert home["needs_review_count"] == 1
    assert home["watch_items"][0]["has_change"] is True
    assert home["watch_items"][0]["change_summary"] == (
        "最新快照文件损坏或无法读取，仍展示上次可用资料"
    )
    ctx = get_company_context(actor, "600001.SH", tmp_path, "demo")
    assert ctx["latest_attempt_error"] == "最新快照文件损坏或无法读取"
    assert ctx["displayed_run_id"] == first


def test_home_damaged_snapshot_after_unusable_row_keeps_older_usable_fact(tmp_path: Path) -> None:
    initialize(tmp_path, mode="demo", journal_mode="DELETE")
    actor = create_demo_actor()
    first = import_snapshot(tmp_path, FIXTURES_DIR / "peer_complete_v1.json", "demo")
    saved = save_watch(actor, "600001.SH", first, {}, 0, tmp_path, "demo")
    mark_seen(actor, "600001.SH", first, saved["revision"], tmp_path, "demo")
    gap_raw = b'{"rows": [{"code": "600001.SH", "pb": null}]}'
    gap_path = tmp_path / "snapshots" / "gap.json"
    gap_path.write_bytes(gap_raw)
    damaged_path = tmp_path / "snapshots" / "damaged.json"
    damaged_path.write_bytes(b"{}")
    with connect_workspace(tmp_path, "demo") as conn:
        for run_id, raw, day in (
            ("gap", gap_raw, "21"),
            ("damaged", b"{}", "22"),
        ):
            register_run(
                conn,
                run_id=run_id,
                kind="peer",
                anchor_code="600001.SH",
                rule_id="peer-screen-v1",
                captured_at=f"2026-09-{day}T16:00:00+08:00",
                valuation_date=f"2026-09-{day}",
                health="partial",
                snapshot_path=f"snapshots/{run_id}.json",
                snapshot_bytes=raw,
            )
    damaged_path.unlink()
    home = get_home(actor, tmp_path, "demo")
    assert home["needs_review_count"] == 1
    assert home["watch_items"][0]["change_summary"] == (
        "最新快照文件损坏或无法读取，仍展示上次可用资料"
    )


def test_home_damaged_snapshot_without_usable_row(tmp_path: Path) -> None:
    initialize(tmp_path, mode="demo", journal_mode="DELETE")
    actor = create_demo_actor()
    first = import_snapshot(tmp_path, FIXTURES_DIR / "peer_complete_v1.json", "demo")
    save_watch(actor, "600001.SH", first, {}, 0, tmp_path, "demo")
    with connect_workspace(tmp_path, "demo") as conn:
        path = tmp_path / present(get_run(conn, first))["snapshot_path"]
    path.unlink()

    home = get_home(actor, tmp_path, "demo")
    assert home["needs_review_count"] == 1
    assert home["watch_items"][0]["has_change"] is True
    assert home["watch_items"][0]["change_summary"] == "最新快照文件损坏或无法读取"


def test_home_ignores_unrelated_damaged_snapshot(tmp_path: Path) -> None:
    initialize(tmp_path, mode="demo", journal_mode="DELETE")
    actor = create_demo_actor()
    first = import_snapshot(tmp_path, FIXTURES_DIR / "peer_complete_v1.json", "demo")
    saved = save_watch(actor, "600001.SH", first, {}, 0, tmp_path, "demo")
    mark_seen(actor, "600001.SH", first, saved["revision"], tmp_path, "demo")
    raw = b"{}"
    path = tmp_path / "snapshots" / "other.json"
    path.write_bytes(raw)
    with connect_workspace(tmp_path, "demo") as conn:
        register_run(
            conn,
            run_id="other_damaged",
            kind="peer",
            anchor_code="600002.SH",
            rule_id="peer-screen-v1",
            captured_at="2026-09-21T16:00:00+08:00",
            valuation_date="2026-09-21",
            health="complete",
            snapshot_path="snapshots/other.json",
            snapshot_bytes=raw,
        )
    path.unlink()
    home = get_home(actor, tmp_path, "demo")
    assert home["needs_review_count"] == 0
    assert home["watch_items"][0]["has_change"] is False


def test_regressed_valuation_date_keeps_older_usable_facts(tmp_path: Path) -> None:
    initialize(tmp_path, mode="demo", journal_mode="DELETE")
    actor = create_demo_actor()
    first = import_snapshot(tmp_path, FIXTURES_DIR / "peer_complete_v1.json", "demo")
    saved = save_watch(actor, "600001.SH", first, {}, 0, tmp_path, "demo")
    mark_seen(actor, "600001.SH", first, saved["revision"], tmp_path, "demo")

    newer = json.loads((FIXTURES_DIR / "peer_complete_v1.json").read_text(encoding="utf-8"))
    newer["screened_at"] = newer["generated_at"] = "2026-09-21T16:00:00+08:00"
    newer["data_date"] = "2026-09-19"
    for row in newer["rows"]:
        row["valuation_date"] = "2026-09-19"
    path = tmp_path / "regressed.json"
    path.write_text(json.dumps(newer, ensure_ascii=False), encoding="utf-8")
    second = import_snapshot(tmp_path, path, "demo")
    with connect_workspace(tmp_path, "demo") as conn:
        assert present(get_run(conn, second))["health"] == "complete"

    ctx = get_company_context(actor, "600001.SH", tmp_path, "demo")
    assert ctx["latest_attempt_run_id"] == second
    assert ctx["latest_attempt_error"] == "估值日期倒退异常"
    assert ctx["has_latest_attempt_gap"] is True
    assert ctx["displayed_run_id"] == first
    assert ctx["usable_valuation_date"] == "2026-09-20"
    home = get_home(actor, tmp_path, "demo")
    assert home["needs_review_count"] == 1
    assert home["watch_items"][0]["has_change"] is True
    assert home["watch_items"][0]["change_summary"] == "最新运行估值日倒退异常，仍展示上次可用资料"
    assert home["watch_items"][0]["valuation_date"] == "2026-09-20"


def test_home_dates_only_from_displayed_watched_facts(tmp_path: Path) -> None:
    initialize(tmp_path, mode="demo", journal_mode="DELETE")
    actor = create_demo_actor()
    assert get_home(actor, tmp_path, "demo")["valuation_date"] == "暂无"
    first = import_snapshot(tmp_path, FIXTURES_DIR / "peer_complete_v1.json", "demo")
    for code in ("600001.SH", "600002.SH"):
        save_watch(actor, code, first, {}, 0, tmp_path, "demo")

    data = json.loads((FIXTURES_DIR / "peer_complete_v1.json").read_text(encoding="utf-8"))

    def register_single(code: str, day: str, run_id: str) -> None:
        row = next(r for r in data["rows"] if r["code"] == code)
        row = {**row, "valuation_date": day}
        raw = json.dumps({"rows": [row]}).encode()
        rel_path = f"snapshots/{run_id}.json"
        (tmp_path / rel_path).write_bytes(raw)
        with connect_workspace(tmp_path, "demo") as conn:
            register_run(
                conn,
                run_id=run_id,
                kind="watch",
                anchor_code=None,
                rule_id=None,
                captured_at=f"{day}T16:00:00+08:00",
                valuation_date=day,
                health="complete",
                snapshot_path=rel_path,
                snapshot_bytes=raw,
            )

    register_single("600003.SH", "2026-09-23", "unrelated")
    assert get_home(actor, tmp_path, "demo")["valuation_date"] == "2026-09-20"
    register_single("600002.SH", "2026-09-21", "company_two")
    home = get_home(actor, tmp_path, "demo")
    assert home["valuation_date"] == "各公司数据日不同"
    assert {it["code"]: it["valuation_date"] for it in home["watch_items"]} == {
        "600001.SH": "2026-09-20",
        "600002.SH": "2026-09-21",
    }
    register_single("600001.SH", "2026-09-21", "company_one")
    assert get_home(actor, tmp_path, "demo")["valuation_date"] == "2026-09-21"


def test_home_reports_valuation_date_change_with_same_pb(tmp_path: Path) -> None:
    initialize(tmp_path, mode="demo", journal_mode="DELETE")
    actor = create_demo_actor()
    first = import_snapshot(tmp_path, FIXTURES_DIR / "peer_complete_v1.json", "demo")
    saved = save_watch(actor, "600001.SH", first, {}, 0, tmp_path, "demo")
    mark_seen(actor, "600001.SH", first, saved["revision"], tmp_path, "demo")

    newer = json.loads((FIXTURES_DIR / "peer_complete_v1.json").read_text(encoding="utf-8"))
    newer["screened_at"] = newer["generated_at"] = "2026-09-21T16:00:00+08:00"
    newer["data_date"] = "2026-09-21"
    for row in newer["rows"]:
        row["valuation_date"] = "2026-09-21"
    new_file = tmp_path / "newer.json"
    new_file.write_text(json.dumps(newer, ensure_ascii=False), encoding="utf-8")
    second = import_snapshot(tmp_path, new_file, "demo")
    with connect_workspace(tmp_path, "demo") as conn:
        assert present(get_run(conn, second))["health"] == "complete"
    home = get_home(actor, tmp_path, "demo")
    assert home["needs_review_count"] == 1
    assert home["watch_items"][0]["has_change"] is True
    assert home["watch_items"][0]["change_summary"] == "估值日期变动: 2026-09-20 → 2026-09-21"


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_home_reports_missing_ack_baseline(tmp_path: Path, damage: str) -> None:
    initialize(tmp_path, mode="demo", journal_mode="DELETE")
    actor = create_demo_actor()
    first = import_snapshot(tmp_path, FIXTURES_DIR / "peer_complete_v1.json", "demo")
    saved = save_watch(actor, "600001.SH", first, {}, 0, tmp_path, "demo")
    mark_seen(actor, "600001.SH", first, saved["revision"], tmp_path, "demo")
    import_snapshot(tmp_path, FIXTURES_DIR / "peer_second_change.json", "demo")
    conn = connect_workspace(tmp_path, "demo")
    try:
        path = tmp_path / present(get_run(conn, first))["snapshot_path"]
    finally:
        conn.close()
    if damage == "missing":
        path.unlink()
    else:
        path.write_text("corrupt", encoding="utf-8")

    home = get_home(actor, tmp_path, "demo")
    assert home["needs_review_count"] == 1
    assert home["watch_items"][0]["has_change"] is True
    assert home["watch_items"][0]["change_summary"] == "已阅基准文件异常，无法比对变化"


def test_home_does_not_fall_back_past_damaged_ack_run(tmp_path: Path) -> None:
    initialize(tmp_path, "demo", journal_mode="DELETE")
    actor = create_demo_actor()
    data = json.loads((FIXTURES_DIR / "peer_complete_v1.json").read_text(encoding="utf-8"))
    run_ids = []
    for day in (20, 21, 22):
        data["screened_at"] = data["generated_at"] = f"2026-09-{day}T16:00:00+08:00"
        file = tmp_path / f"run_{day}.json"
        file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        run_ids.append(import_snapshot(tmp_path, file, "demo"))
    saved = save_watch(actor, "600001.SH", run_ids[0], {}, 0, tmp_path, "demo")
    mark_seen(actor, "600001.SH", run_ids[1], saved["revision"], tmp_path, "demo")
    with connect_workspace(tmp_path, "demo") as conn:
        ack_path = tmp_path / present(get_run(conn, run_ids[1]))["snapshot_path"]
    ack_path.write_text("corrupt", encoding="utf-8")

    with connect_workspace(tmp_path, "demo") as conn:
        assert present(get_watch_item(conn, "600001.SH"))["ack_run_id"] == run_ids[1]
    item = get_home(actor, tmp_path, "demo")["watch_items"][0]
    assert item["has_change"] is True
    assert item["change_summary"] == "已阅基准文件异常，无法比对变化"


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
@pytest.mark.parametrize("health", ["complete", "partial"])
def test_company_reports_latest_snapshot_error(
    tmp_path: Path, damage: str, health: Literal["complete", "partial"]
) -> None:
    initialize(tmp_path, mode="demo", journal_mode="DELETE")
    actor = create_demo_actor()
    first = import_snapshot(tmp_path, FIXTURES_DIR / "peer_complete_v1.json", "demo")
    saved = save_watch(actor, "600001.SH", first, {}, 0, tmp_path, "demo")
    raw = json.dumps({"anchor": "600001.SH", "rows": [{"code": "600001.SH"}]}).encode()
    path = tmp_path / "snapshots" / "latest.json"
    path.write_bytes(raw)
    conn = connect_workspace(tmp_path, "demo")
    try:
        register_run(
            conn,
            run_id="latest",
            kind="peer",
            anchor_code="600001.SH",
            rule_id="peer-screen-v1",
            captured_at="2026-09-21T16:00:00+08:00",
            valuation_date="2026-09-21",
            health=health,
            snapshot_path="snapshots/latest.json",
            snapshot_bytes=raw,
        )
        conn.commit()
    finally:
        conn.close()
    if damage == "missing":
        path.unlink()
    else:
        path.write_text("corrupt", encoding="utf-8")

    # A newer, healthy run for another company must not hide this company's damaged run.
    other_raw = json.dumps({"rows": [{"code": "600002.SH"}]}).encode()
    other_path = tmp_path / "snapshots" / "other.json"
    other_path.write_bytes(other_raw)
    conn = connect_workspace(tmp_path, "demo")
    try:
        register_run(
            conn,
            run_id="other",
            kind="peer",
            anchor_code="600002.SH",
            rule_id="peer-screen-v1",
            captured_at="2026-09-22T16:00:00+08:00",
            valuation_date="2026-09-22",
            health="partial",
            snapshot_path="snapshots/other.json",
            snapshot_bytes=other_raw,
        )
        conn.commit()
    finally:
        conn.close()

    ctx = get_company_context(actor, "600001.SH", tmp_path, "demo")
    assert ctx["latest_attempt_error"] == "最新快照文件损坏或无法读取"
    assert ctx["has_latest_attempt_gap"] is True  # app.py disables the acknowledgement button
    assert ctx["latest_attempt_run_id"] == "latest"
    assert ctx["displayed_run_id"] == first
    with pytest.raises(ServiceError, match="newer run has data gap"):
        mark_seen(actor, "600001.SH", first, saved["revision"], tmp_path, "demo")


def test_peer_watch_anchor_persists_and_legacy_anchor_is_inferred(tmp_path: Path) -> None:
    initialize(tmp_path, mode="demo", journal_mode="DELETE")
    actor = create_demo_actor()
    first = import_snapshot(tmp_path, FIXTURES_DIR / "peer_complete_v1.json", "demo")
    saved = save_watch(actor, "600002.SH", first, {}, 0, tmp_path, "demo")
    conn = connect_workspace(tmp_path, "demo")
    try:
        assert present(get_watch_item(conn, "600002.SH"))["anchor_code"] == "600001.SH"
        # Simulate an older workspace where the peer's anchor was not stored.
        conn.execute("UPDATE watch_items SET anchor_code=NULL WHERE code='600002.SH'")
        raw = b"{}"
        path = tmp_path / "snapshots" / "damaged_peer.json"
        path.write_bytes(raw)
        register_run(
            conn,
            run_id="damaged_peer",
            kind="peer",
            anchor_code="600001.SH",
            rule_id="peer-screen-v1",
            captured_at="2026-09-21T16:00:00+08:00",
            valuation_date="2026-09-21",
            health="complete",
            snapshot_path="snapshots/damaged_peer.json",
            snapshot_bytes=raw,
        )
        conn.commit()
    finally:
        conn.close()
    path.unlink()

    ctx = get_company_context(actor, "600002.SH", tmp_path, "demo")
    assert ctx["latest_attempt_error"] == "最新快照文件损坏或无法读取"
    assert ctx["has_latest_attempt_gap"] is True
    assert ctx["latest_attempt_run_id"] == "damaged_peer"
    save_watch(actor, "600002.SH", first, {}, 0, tmp_path, "demo")
    with connect_workspace(tmp_path, "demo") as conn:
        assert present(get_watch_item(conn, "600002.SH"))["anchor_code"] == "600001.SH"
        assert present(get_watch_item(conn, "600002.SH"))["revision"] == saved["revision"]


@pytest.mark.parametrize("targets", [None, ["600003.SH"], ["600001.SH"]])
def test_damaged_watch_run_requires_explicit_target(
    tmp_path: Path, targets: list[str] | None
) -> None:
    initialize(tmp_path, mode="demo", journal_mode="DELETE")
    actor = create_demo_actor()
    first = import_snapshot(tmp_path, FIXTURES_DIR / "peer_complete_v1.json", "demo")
    raw = b"{}"
    path = tmp_path / "snapshots" / "damaged_watch.json"
    path.write_bytes(raw)
    with connect_workspace(tmp_path, "demo") as conn:
        register_run(
            conn,
            run_id="damaged_watch",
            kind="watch",
            anchor_code=None,
            rule_id=None,
            captured_at="2026-09-22T16:00:00+08:00",
            valuation_date="2026-09-22",
            health="partial",
            snapshot_path="snapshots/damaged_watch.json",
            snapshot_bytes=raw,
        )
        if targets is not None:
            conn.execute(
                """INSERT INTO update_jobs
                (job_id,request_id,kind,payload_json,dedupe_key,status,requested_at,
                 updated_at,finished_at,result_run_id) VALUES (?,?,?,?,?,'succeeded',?,?,?,?)""",
                (
                    "job_watch",
                    "request_watch",
                    "watch",
                    json.dumps({"expected_codes": targets}),
                    "watch_dedupe",
                    "2026-09-22",
                    "2026-09-22",
                    "2026-09-22",
                    "damaged_watch",
                ),
            )
    path.unlink()
    ctx = get_company_context(actor, "600001.SH", tmp_path, "demo")
    assert ctx["latest_attempt_error"] == (
        "最新快照文件损坏或无法读取" if targets == ["600001.SH"] else None
    )
    assert ctx["latest_attempt_run_id"] == ("damaged_watch" if targets == ["600001.SH"] else first)


def test_company_ignores_other_company_damaged_run(tmp_path: Path) -> None:
    initialize(tmp_path, mode="demo", journal_mode="DELETE")
    actor = create_demo_actor()
    first = import_snapshot(tmp_path, FIXTURES_DIR / "peer_complete_v1.json", "demo")
    raw = b'{"rows": [{"code": "600002.SH"}]}'
    path = tmp_path / "snapshots" / "other.json"
    path.write_bytes(raw)
    conn = connect_workspace(tmp_path, "demo")
    try:
        register_run(
            conn,
            run_id="other",
            kind="peer",
            anchor_code="600002.SH",
            rule_id="peer-screen-v1",
            captured_at="2026-09-22T16:00:00+08:00",
            valuation_date="2026-09-22",
            health="partial",
            snapshot_path="snapshots/other.json",
            snapshot_bytes=raw,
        )
        conn.commit()
    finally:
        conn.close()
    path.write_bytes(b"corrupt")

    ctx = get_company_context(actor, "600001.SH", tmp_path, "demo")
    assert ctx["latest_attempt_run_id"] == first
    assert ctx["displayed_run_id"] == first
    assert ctx["latest_attempt_error"] is None
    assert ctx["has_latest_attempt_gap"] is False


def test_snapshot_read_oserror_is_service_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initialize(tmp_path, mode="demo", journal_mode="DELETE")
    actor = create_demo_actor()
    first = import_snapshot(tmp_path, FIXTURES_DIR / "peer_complete_v1.json", "demo")
    save_watch(actor, "600001.SH", first, {}, 0, tmp_path, "demo")
    conn = connect_workspace(tmp_path, "demo")
    try:
        rel_path = present(get_run(conn, first))["snapshot_path"]
    finally:
        conn.close()
    original_read = Path.read_bytes

    def unreadable(self: Path) -> bytes:
        if self == tmp_path / rel_path:
            raise PermissionError("read denied")
        return original_read(self)

    monkeypatch.setattr(Path, "read_bytes", unreadable)
    with pytest.raises(ServiceError, match="failed to read snapshot file: read denied"):
        read_verified_snapshot(tmp_path, rel_path)
    ctx = get_company_context(actor, "600001.SH", tmp_path, "demo")
    assert ctx["latest_attempt_error"] == "最新快照文件损坏或无法读取"
    assert ctx["has_latest_attempt_gap"] is True
    assert get_home(actor, tmp_path, "demo")["total_watch_count"] == 1


def test_home_ignores_annual_acquisition_time_only(tmp_path: Path) -> None:
    ws_dir = tmp_path / "home_annual_ws"
    initialize(ws_dir, mode="demo", journal_mode="DELETE")
    actor = create_demo_actor()
    base = json.loads((FIXTURES_DIR / "peer_complete_v1.json").read_text(encoding="utf-8"))
    first = tmp_path / "first.json"
    for item in base["rows"][0]["annual_roes"]:
        item["acquired_at"] = "2026-09-20T10:00:00+08:00"
    first.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
    run_id = import_snapshot(ws_dir, first, "demo")
    saved = save_watch(actor, "600001.SH", run_id, {}, 0, ws_dir, "demo")
    mark_seen(actor, "600001.SH", run_id, saved["revision"], ws_dir, "demo")

    base["screened_at"] = "2026-09-21T16:00:00+08:00"
    base["generated_at"] = base["screened_at"]
    for item in base["rows"][0]["annual_roes"]:
        item["acquired_at"] = "2026-09-21T10:00:00+08:00"
    second = tmp_path / "second.json"
    second.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
    second_id = import_snapshot(ws_dir, second, "demo")
    conn = connect_workspace(ws_dir, "demo")
    try:
        assert present(get_run(conn, second_id))["health"] == "complete"
    finally:
        conn.close()
    home = get_home(actor, ws_dir, "demo")
    assert home["watch_items"][0]["has_change"] is False
    assert home["watch_items"][0]["change_summary"] == "本工具覆盖的字段暂无未阅变化"

    base["screened_at"] = "2026-09-22T16:00:00+08:00"
    base["generated_at"] = base["screened_at"]
    base["rows"][0]["annual_roes"][0]["ann_date"] = "2024-04-16"
    third = tmp_path / "third.json"
    third.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
    import_snapshot(ws_dir, third, "demo")
    home = get_home(actor, ws_dir, "demo")
    assert home["watch_items"][0]["change_summary"] == "采用的年报数据有变化"


def test_home_ignores_reversed_annual_report_order(tmp_path: Path) -> None:
    ws_dir = tmp_path / "home_reversed_annual_ws"
    initialize(ws_dir, mode="demo", journal_mode="DELETE")
    actor = create_demo_actor()
    first = FIXTURES_DIR / "peer_complete_v1.json"
    first_id = import_snapshot(ws_dir, first, "demo")
    saved = save_watch(actor, "600001.SH", first_id, {}, 0, ws_dir, "demo")
    mark_seen(actor, "600001.SH", first_id, saved["revision"], ws_dir, "demo")

    second = json.loads(first.read_text(encoding="utf-8"))
    second["screened_at"] = second["generated_at"] = "2026-09-21T16:00:00+08:00"
    second["rows"][0]["annual_roes"].reverse()
    assert [item["year"] for item in second["rows"][0]["annual_roes"]] == [2025, 2024, 2023]
    second_file = tmp_path / "reversed.json"
    second_file.write_text(json.dumps(second, ensure_ascii=False), encoding="utf-8")
    second_id = import_snapshot(ws_dir, second_file, "demo")
    conn = connect_workspace(ws_dir, "demo")
    try:
        assert present(get_run(conn, second_id))["health"] == "complete"
    finally:
        conn.close()

    item = get_home(actor, ws_dir, "demo")["watch_items"][0]
    assert item["has_change"] is False
    assert item["change_summary"] == "本工具覆盖的字段暂无未阅变化"


def test_home_ignores_equivalent_annual_report_formats(tmp_path: Path) -> None:
    ws_dir = tmp_path / "annual_formats_ws"
    initialize(ws_dir, mode="demo", journal_mode="DELETE")
    actor = create_demo_actor()
    first = json.loads((FIXTURES_DIR / "peer_complete_v1.json").read_text(encoding="utf-8"))
    first_row = first["rows"][0]
    first_row["annual_roes"][0].update(roe=12.0, report_type="annual", update_flag="1")
    first_row["roe_mean"] = 14.0
    first_file = tmp_path / "annual_first.json"
    first_file.write_text(json.dumps(first, ensure_ascii=False), encoding="utf-8")
    first_id = import_snapshot(ws_dir, first_file, "demo")
    saved = save_watch(actor, "600001.SH", first_id, {}, 0, ws_dir, "demo")
    mark_seen(actor, "600001.SH", first_id, saved["revision"], ws_dir, "demo")

    second = json.loads(first_file.read_text(encoding="utf-8"))
    second["screened_at"] = second["generated_at"] = "2026-09-21T16:00:00+08:00"
    second_row = second["rows"][0]
    for report in second_row["annual_roes"]:
        year = report.pop("year")
        report["period" if year != 2024 else "end_date"] = f"{year}1231"
        report["roe_waa"] = report.pop("roe")
        report["ann_date"] = report["ann_date"].replace("-", "")
    second_row["annual_roes"][0]["update_flag"] = 1
    second_row["annual_roes"].reverse()
    assert _annual_facts(first_row) == _annual_facts(second_row)
    assert _annual_facts(second_row)[0] == {
        "period_end": "2023-12-31",
        "roe": 12.0,
        "ann_date": "2024-04-15",
        "report_type": "annual",
        "update_flag": "1",
    }
    second_file = tmp_path / "annual_second.json"
    second_file.write_text(json.dumps(second, ensure_ascii=False), encoding="utf-8")
    second_id = import_snapshot(ws_dir, second_file, "demo")
    conn = connect_workspace(ws_dir, "demo")
    try:
        assert present(get_run(conn, second_id))["health"] == "complete"
    finally:
        conn.close()

    home = get_home(actor, ws_dir, "demo")
    assert home["needs_review_count"] == 0
    assert home["watch_items"][0]["has_change"] is False
    assert home["watch_items"][0]["change_summary"] == "本工具覆盖的字段暂无未阅变化"


def test_company_peer_rank_prefers_latest_valuation_date(tmp_path: Path) -> None:
    initialize(tmp_path, "demo", journal_mode="DELETE")
    for run_id, day, captured_at, position in (
        ("run_a", "2026-09-20", "2026-09-20T10:00:00Z", 1),
        ("run_b", "2026-09-18", "2026-09-21T10:00:00Z", 3),
    ):
        raw = json.dumps(
            {
                "anchor": "600001.SH",
                "rows": [{"code": "600001.SH"}],
                "results": {"ranking": [{"code": "600001.SH", "position": position}]},
            }
        ).encode()
        rel_path = f"snapshots/{run_id}.json"
        (tmp_path / "snapshots").mkdir(exist_ok=True)
        (tmp_path / rel_path).write_bytes(raw)
        with connect_workspace(tmp_path, "demo") as conn:
            register_run(
                conn,
                run_id=run_id,
                kind="peer",
                anchor_code="600001.SH",
                rule_id="peer-screen-v1",
                captured_at=captured_at,
                valuation_date=day,
                health="complete",
                snapshot_path=rel_path,
                snapshot_bytes=raw,
            )

    rank = get_company_context(create_demo_actor(), "600001.SH", tmp_path, "demo")["peer_rank"]
    assert rank["valuation_date"] == "2026-09-20"
    assert rank["position"] == 1


def test_company_context_picks_usable_fact_from_partial_run(tmp_path: Path) -> None:
    ws_dir = tmp_path / "partial_fact_ws"
    initialize(ws_dir, mode="demo", journal_mode="DELETE")
    actor = create_demo_actor()

    snap = {
        "rows": [
            {
                "code": "600001.SH",
                "name": "标的甲",
                "pb": 1.5,
                "valuation_date": "2026-09-20",
                "valuation_source": "tracker",
                "financial_source": "tracker",
                "risk_source": "tracker",
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
                "pb": 2.0,
                "financial_source": "tushare.fina_indicator:error",
                "exclusions": ["SCREEN_ERROR: timeout"],
                "facts_usable": False,
            },
        ],
        "results": {"ranking": []},
    }
    raw = json.dumps(snap).encode("utf-8")
    rel_path = "snapshots/partial_run.json"
    (ws_dir / "snapshots").mkdir(parents=True, exist_ok=True)
    (ws_dir / rel_path).write_bytes(raw)

    conn = connect_workspace(ws_dir, "demo")
    register_run(
        conn,
        run_id="run_partial_1",
        kind="peer",
        anchor_code="600001.SH",
        rule_id="peer-screen-v1",
        captured_at="2026-09-20T16:00:00+08:00",
        valuation_date="2026-09-20",
        health="partial",
        snapshot_path=rel_path,
        snapshot_bytes=raw,
    )
    conn.commit()
    conn.close()

    # 1. Official peer discover does NOT treat partial run as complete peer ranking
    disc = get_peer_discover(actor, "600001.SH", ws_dir, "demo")
    assert disc["has_run"] is False

    # 2. Company context DOES pick up usable facts for 600001.SH from verified partial run
    ctx = get_company_context(actor, "600001.SH", ws_dir, "demo")
    assert ctx["code"] == "600001.SH"
    assert ctx["name"] == "标的甲"
    assert ctx["usable_fact"] is not None
    assert ctx["usable_fact"]["pb"] == 1.5
    assert ctx["usable_fact"]["roe_mean"] == 14.0
    assert ctx["displayed_run_id"] == "run_partial_1"


@pytest.mark.parametrize("coverage", ["anchor", "selected_codes", "watch"])
def test_covered_missing_row_after_ack_is_reported_and_cannot_be_acknowledged(
    tmp_path: Path, coverage: str
) -> None:
    initialize(tmp_path, "demo", journal_mode="DELETE")
    actor = create_demo_actor()
    first = import_snapshot(tmp_path, FIXTURES_DIR / "peer_complete_v1.json", "demo")
    saved = save_watch(actor, "600001.SH", first, {}, 0, tmp_path, "demo")
    seen = mark_seen(actor, "600001.SH", first, saved["revision"], tmp_path, "demo")
    snap = {
        "rows": [{"code": "600002.SH"}],
        "anchor": "600001.SH" if coverage == "anchor" else "600002.SH",
        "scope": {"selected_codes": ["600001.SH"] if coverage == "selected_codes" else []},
        "workspace_meta": {"expected_codes": ["600001.SH"]},
    }
    raw = json.dumps(snap).encode()
    path = "snapshots/covered_missing.json"
    (tmp_path / path).write_bytes(raw)
    with connect_workspace(tmp_path, "demo") as conn:
        register_run(
            conn,
            run_id="covered_missing",
            kind="watch" if coverage == "watch" else "peer",
            anchor_code=None if coverage == "watch" else str(snap["anchor"]),
            rule_id=None if coverage == "watch" else "peer-screen-v1",
            captured_at="2026-09-21T16:00:00+08:00",
            valuation_date="2026-09-21",
            health="partial",
            snapshot_path=path,
            snapshot_bytes=raw,
        )
        if coverage == "watch":
            conn.execute(
                """INSERT INTO update_jobs
                (job_id,request_id,kind,payload_json,dedupe_key,status,requested_at,
                 updated_at,finished_at,result_run_id) VALUES (?,?,?,?,?,'succeeded',?,?,?,?)""",
                (
                    "covered_missing",
                    "req_missing",
                    "watch",
                    json.dumps({"expected_codes": ["600001.SH"]}),
                    "missing",
                    "2026-09-21",
                    "2026-09-21",
                    "2026-09-21",
                    "covered_missing",
                ),
            )

    home = get_home(actor, tmp_path, "demo")
    assert home["needs_review_count"] == 1
    assert home["watch_items"][0]["has_change"] is True
    assert "数据缺口" in home["watch_items"][0]["change_summary"]
    assert "上次可用资料" in home["watch_items"][0]["change_summary"]
    ctx = get_company_context(actor, "600001.SH", tmp_path, "demo")
    assert ctx["latest_attempt_run_id"] == "covered_missing"
    assert ctx["latest_attempt_error"] == "最新运行覆盖该标的但缺少事实数据（数据缺口）"
    assert ctx["has_latest_attempt_gap"] is True
    assert ctx["displayed_run_id"] == first
    with pytest.raises(
        ServiceError, match="cannot acknowledge stale run when newer run has data gap"
    ):
        mark_seen(actor, "600001.SH", first, seen["revision"], tmp_path, "demo")
    with connect_workspace(tmp_path, "demo") as conn:
        assert present(get_watch_item(conn, "600001.SH"))["revision"] == seen["revision"]


def test_discover_falls_back_from_damaged_complete_snapshot(tmp_path: Path) -> None:
    initialize(tmp_path, "demo", journal_mode="DELETE")
    actor = create_demo_actor()
    first = import_snapshot(tmp_path, FIXTURES_DIR / "peer_complete_v1.json", "demo")
    raw = b'{"rows": [], "results": {"ranking": []}}'
    path = tmp_path / "snapshots" / "latest_complete.json"
    path.write_bytes(raw)
    with connect_workspace(tmp_path, "demo") as conn:
        register_run(
            conn,
            run_id="latest_complete",
            kind="peer",
            anchor_code="600001.SH",
            rule_id="peer-screen-v1",
            captured_at="2026-09-21T16:00:00+08:00",
            valuation_date="2026-09-21",
            health="complete",
            snapshot_path="snapshots/latest_complete.json",
            snapshot_bytes=raw,
        )
    path.write_bytes(b"damaged")
    disc = get_peer_discover(actor, "600001.SH", tmp_path, "demo")
    assert disc["run_id"] == first
    assert len(disc["rows"]) == 4
    assert "latest_complete" in disc["warning"]
    assert first in disc["warning"]
    assert "2026-09-20" in disc["warning"]

    with connect_workspace(tmp_path, "demo") as conn:
        old_path = tmp_path / present(get_run(conn, first))["snapshot_path"]
    old_path.unlink()
    disc = get_peer_discover(actor, "600001.SH", tmp_path, "demo")
    assert disc["has_run"] is False
    assert disc["error"] == "最新同业快照损坏或无法读取"


def peer_view_run(state, run_id, snap, health="complete", day=22):
    snap = {**snap, "data_date": f"2026-09-{day}", "screened_at": f"2026-09-{day}T16:00:00+08:00"}
    raw = json.dumps(snap).encode()
    rel = f"snapshots/{run_id}.json"
    (state / "snapshots").mkdir(exist_ok=True)
    (state / rel).write_bytes(raw)
    with connect_workspace(state, "demo") as conn:
        register_run(
            conn,
            run_id=run_id,
            kind="peer",
            anchor_code=snap.get("anchor", "600001.SH"),
            rule_id="peer-screen-v1",
            captured_at=f"2026-09-{day}T16:00:00+08:00",
            valuation_date=f"2026-09-{day}",
            health=health,
            snapshot_path=rel,
            snapshot_bytes=raw,
        )
    return run_id


@pytest.mark.parametrize("has_old", [False, True])
def test_discover_partial_is_diagnostic_not_new_top(tmp_path, has_old):
    initialize(tmp_path, "demo", journal_mode="DELETE")
    actor = create_demo_actor()
    old = (
        import_snapshot(tmp_path, FIXTURES_DIR / "peer_complete_v1.json", "demo")
        if has_old
        else None
    )
    snap = json.loads((FIXTURES_DIR / "peer_complete_v1.json").read_text())
    snap["rows"] = [snap["rows"][0]]  # Three selected rows are absent.
    snap["results"].update(
        ranking=[], top=[], discovery_complete=False, outside_watchlist_qualified_count=0
    )
    partial = peer_view_run(tmp_path, "partial", snap, "partial")
    data = get_peer_discover(actor, "600001.SH", tmp_path, "demo")
    assert data["has_run"] is has_old
    assert data["attempt"]["label"] == "部分完成"
    assert data["attempt"]["result"]["run_id"] == partial
    assert data["attempt"]["result"]["results"]["top"] == []
    assert len(data["attempt"]["result"]["scope"]["selected_codes"]) == 4
    if has_old:
        assert data["run_id"] == old and data["showing_previous"]
        assert data["results"]["top"] == ["600002.SH", "600001.SH", "600003.SH"]
    else:
        assert "暂无" in data["message"] and "results" not in data


def test_discover_historical_job_pin_and_wrong_context(tmp_path):
    from services import request_peer_update

    initialize(tmp_path, "demo", journal_mode="DELETE")
    actor = create_demo_actor()
    snap = json.loads((FIXTURES_DIR / "peer_complete_v1.json").read_text())
    first = peer_view_run(tmp_path, "first", snap, day=20)
    later = peer_view_run(tmp_path, "later", snap, day=23)
    job = request_peer_update(actor, "600001.SH", "old-job", tmp_path, "demo")
    with connect_workspace(tmp_path, "demo") as conn:
        conn.execute(
            "UPDATE update_jobs SET status='succeeded',phase='complete',finished_at=requested_at,result_run_id=? WHERE job_id=?",
            (first, job["job_id"]),
        )
    historical = get_peer_discover(actor, "600001.SH", tmp_path, "demo", job_id=job["job_id"])
    assert historical["run_id"] == first
    assert historical["attempt"]["result"]["run_id"] == first
    assert get_peer_discover(actor, "600001.SH", tmp_path, "demo")["run_id"] == later
    assert get_peer_discover(actor, "600001.SH", tmp_path, "demo", run_id=first)["run_id"] == first
    for options in ({"job_id": job["job_id"]}, {"run_id": first}):
        with pytest.raises(ServiceError, match="不匹配"):
            get_peer_discover(actor, "600002.SH", tmp_path, "demo", **options)
    with connect_workspace(tmp_path, "demo") as conn:
        conn.execute(
            "UPDATE update_jobs SET status='failed',phase='failed',result_run_id=NULL,requested_at='2026-09-21T12:00:00+08:00' WHERE job_id=?",
            (job["job_id"],),
        )
    failed = get_peer_discover(actor, "600001.SH", tmp_path, "demo", job_id=job["job_id"])
    assert failed["run_id"] == first  # Not the run captured after this historical failure.
    assert failed["attempt"]["label"] == "失败" and failed["showing_previous"]


@pytest.mark.parametrize(
    "status,label",
    [("queued", "排队中"), ("running", "更新中"), ("failed", "失败"), ("interrupted", "已中断")],
)
def test_discover_job_without_run_is_not_no_candidates(tmp_path, status, label):
    from services import request_peer_update

    initialize(tmp_path, "demo", journal_mode="DELETE")
    actor = create_demo_actor()
    job = request_peer_update(actor, "600001.SH", "empty-job", tmp_path, "demo")
    with connect_workspace(tmp_path, "demo") as conn:
        conn.execute(
            "UPDATE update_jobs SET status=?,started_at=requested_at,finished_at=? WHERE job_id=?",
            (
                status,
                None if status in ("queued", "running") else job["requested_at"],
                job["job_id"],
            ),
        )
    data = get_peer_discover(actor, "600001.SH", tmp_path, "demo")
    assert not data["has_run"]
    assert data["attempt"]["label"] == label and data["attempt"]["result"] is None
    assert data["attempt"]["target_date"] == job["target_date"]


def test_discover_preserves_anchor_facts_and_rechecks_authorization(tmp_path):
    initialize(tmp_path, "demo", journal_mode="DELETE")
    actor = create_demo_actor()
    snap = json.loads((FIXTURES_DIR / "peer_complete_v1.json").read_text())
    snap["rows"][0]["exclusions"] = ["KNOWN_ST_WARNING"]
    snap["results"]["ranking"] = [
        r for r in snap["results"]["ranking"] if r["code"] != snap["anchor"]
    ]
    snap["results"]["top"] = [r["code"] for r in snap["results"]["ranking"]]
    run_id = peer_view_run(tmp_path, "excluded-anchor", snap)
    data = get_peer_discover(actor, snap["anchor"], tmp_path, "demo")
    assert data["results"] == snap["results"]  # No reranking or promotion by personal state.
    assert data["rows"][0]["pb"] == 1.85
    assert data["rows"][0]["annual_roes"][0]["roe_waa"] == 12.5
    saved = save_watch(
        actor, "600002.SH", run_id, {"status": "paused", "reason": "私人判断"}, 0, tmp_path, "demo"
    )
    repeated = save_watch(actor, "600002.SH", run_id, {"status": "observe"}, 0, tmp_path, "demo")
    assert repeated == saved and repeated["ack_run_id"] is None
    data = get_peer_discover(actor, snap["anchor"], tmp_path, "demo")
    assert data["watch_statuses"]["600002.SH"] == "paused"
    assert "私人判断" not in str(data)
    assert data["results"]["top"] == snap["results"]["top"]

    def revoke_after_read(*args, **kwargs):
        result = read_verified_snapshot(*args, **kwargs)
        actor.revoke()
        return result

    with patch("services.read_verified_snapshot", side_effect=revoke_after_read):
        with pytest.raises(AuthError):
            get_peer_discover(actor, snap["anchor"], tmp_path, "demo")


def test_delete_watch_preserves_facts_and_rejects_stale_readded_record(tmp_path):
    initialize(tmp_path, "demo", journal_mode="DELETE")
    actor = create_demo_actor()
    run = import_snapshot(tmp_path, FIXTURES_DIR / "peer_complete_v1.json", "demo")
    old = save_watch(actor, "600001.SH", run, {}, 0, tmp_path, "demo")
    other = save_watch(actor, "600002.SH", run, {"reason": "保留"}, 0, tmp_path, "demo")
    before = {p.name: p.read_bytes() for p in (tmp_path / "snapshots").rglob("*.json")}
    with pytest.raises(ServiceError):
        delete_watch(actor, old["code"], old["revision"] + 1, old["updated_at"], tmp_path, "demo")
    delete_watch(actor, old["code"], old["revision"], old["updated_at"], tmp_path, "demo")
    with connect_workspace(tmp_path, "demo") as conn:
        assert get_watch_item(conn, old["code"]) is None
        assert get_watch_item(conn, other["code"]) == other
        assert get_run(conn, run) is not None
    new = save_watch(actor, old["code"], run, {}, 0, tmp_path, "demo")
    assert new["revision"] == old["revision"] and new["updated_at"] != old["updated_at"]
    assert new["reason"] == "" and new["ack_run_id"] is None
    with pytest.raises(ServiceError):
        delete_watch(actor, old["code"], old["revision"], old["updated_at"], tmp_path, "demo")
    with pytest.raises(ServiceError):
        save_watch(
            actor,
            old["code"],
            run,
            {"reason": "旧页覆盖"},
            old["revision"],
            tmp_path,
            "demo",
            expected_updated_at=old["updated_at"],
        )
    with pytest.raises(ServiceError):
        mark_seen(
            actor,
            old["code"],
            run,
            old["revision"],
            tmp_path,
            "demo",
            expected_updated_at=old["updated_at"],
        )
    acknowledged = mark_seen(
        actor,
        new["code"],
        run,
        new["revision"],
        tmp_path,
        "demo",
        expected_updated_at=new["updated_at"],
    )
    assert (
        mark_seen(
            actor,
            new["code"],
            run,
            new["revision"],
            tmp_path,
            "demo",
            expected_updated_at=new["updated_at"],
        )
        == acknowledged
    )
    assert before and before == {
        p.name: p.read_bytes() for p in (tmp_path / "snapshots").rglob("*.json")
    }


@pytest.mark.parametrize("status", ["queued", "running", "succeeded", "failed", "interrupted"])
def test_dismiss_job_preserves_request_id_and_alias_deduplication(tmp_path, status):
    initialize(tmp_path, "demo", journal_mode="DELETE")
    actor = create_demo_actor()
    run = import_snapshot(tmp_path, FIXTURES_DIR / "peer_complete_v1.json", "demo")
    job = request_peer_update(actor, "600001.SH", "original", tmp_path, "demo")
    assert (
        request_peer_update(actor, "600001.SH", "alias", tmp_path, "demo")["job_id"]
        == job["job_id"]
    )
    with connect_workspace(tmp_path, "demo") as conn:
        conn.execute(
            "UPDATE update_jobs SET status=?, started_at=requested_at, finished_at=?, result_run_id=? WHERE job_id=?",
            (
                status,
                None if status in ("queued", "running") else job["requested_at"],
                run if status == "succeeded" else None,
                job["job_id"],
            ),
        )
    if status in ("failed", "interrupted"):
        dismiss_update_job(actor, job["job_id"], tmp_path, "demo")
        dismiss_update_job(actor, job["job_id"], tmp_path, "demo")
        assert list_update_jobs(actor, tmp_path, "demo") == []
        with pytest.raises(ServiceError):
            get_update_job(actor, job["job_id"], tmp_path, "demo")
        with pytest.raises(ServiceError):
            get_peer_discover(actor, "600001.SH", tmp_path, "demo", job_id=job["job_id"])
        assert (
            get_peer_discover(actor, "600001.SH", tmp_path, "demo")["attempt"].get("job_id") is None
        )
    else:
        with pytest.raises(ServiceError):
            dismiss_update_job(actor, job["job_id"], tmp_path, "demo")
        assert len(list_update_jobs(actor, tmp_path, "demo")) == 1
    for request_id in ("original", "alias"):
        assert (
            request_peer_update(actor, "600001.SH", request_id, tmp_path, "demo")["job_id"]
            == job["job_id"]
        )
    with connect_workspace(tmp_path, "demo") as conn:
        assert conn.execute("SELECT count(*) FROM update_jobs").fetchone()[0] == 1
        assert get_run(conn, run) is not None


@pytest.mark.parametrize("operation", ["watch", "job"])
@pytest.mark.parametrize("revoke_at", [1, 2, 3])
def test_removal_authorization_and_rollback(tmp_path, monkeypatch, operation, revoke_at):
    initialize(tmp_path, "demo", journal_mode="DELETE")
    actor = create_demo_actor()
    run = import_snapshot(tmp_path, FIXTURES_DIR / "peer_complete_v1.json", "demo")
    item = save_watch(actor, "600001.SH", run, {"reason": "不可丢失"}, 0, tmp_path, "demo")
    job = request_peer_update(actor, "600001.SH", "remove-auth", tmp_path, "demo")
    with connect_workspace(tmp_path, "demo") as conn:
        conn.execute(
            "UPDATE update_jobs SET status='failed', finished_at=requested_at WHERE job_id=?",
            (job["job_id"],),
        )
    calls = 0

    def recheck(current):
        nonlocal calls
        calls += 1
        if calls == revoke_at:
            current.revoke()
        return check_actor(current)

    monkeypatch.setattr("services.check_actor", recheck)
    with pytest.raises(AuthError):
        if operation == "watch":
            delete_watch(
                actor, item["code"], item["revision"], item["updated_at"], tmp_path, "demo"
            )
        else:
            dismiss_update_job(actor, job["job_id"], tmp_path, "demo")
    with connect_workspace(tmp_path, "demo") as conn:
        assert get_watch_item(conn, item["code"]) == item
        assert (
            conn.execute(
                "SELECT phase FROM update_jobs WHERE job_id=?", (job["job_id"],)
            ).fetchone()[0]
            != "dismissed"
        )
