"""Isolated user-clicked peer update and worker recovery checks (synthetic data only)."""

import fcntl
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import screen
import services
import worker
from auth import create_demo_actor, create_owner_actor
from screen import ScreenError
from services import ServiceError, get_update_job, request_peer_update
from workspace import WorkspaceError, connect_workspace, get_run, initialize


def test_peer_request_idempotency_and_worker_result(tmp_path: Path, monkeypatch) -> None:
    initialize(tmp_path, "demo", journal_mode="DELETE")
    actor = create_demo_actor()
    first = request_peer_update(actor, "600001.SH", "clicked-1", tmp_path, "demo")
    assert first["status"] == "queued"
    assert (
        request_peer_update(actor, "600001.SH", "clicked-2", tmp_path, "demo")["job_id"]
        == first["job_id"]
    )
    with pytest.raises(ServiceError, match="不同更新意图"):
        request_peer_update(actor, "600002.SH", "clicked-1", tmp_path, "demo")
    monkeypatch.setattr(services, "_peer_target_date", lambda *_: "2026-09-21")
    assert (
        request_peer_update(actor, "600001.SH", "clicked-2", tmp_path, "demo")["job_id"]
        == first["job_id"]
    )  # Retry alias must not recompute the target date.
    monkeypatch.setattr(services, "_peer_target_date", lambda *_: "2026-09-20")
    worker.run_worker(tmp_path, "demo", None, once=True)
    done = get_update_job(actor, first["job_id"], tmp_path, "demo")
    assert done["status"] == "succeeded"
    assert done["phase"] == "complete"
    with connect_workspace(tmp_path, "demo") as conn:
        run = get_run(conn, done["result_run_id"])
    assert run and run["health"] == "complete" and run["anchor_code"] == "600001.SH"
    again = request_peer_update(actor, "600001.SH", "clicked-3", tmp_path, "demo")
    assert again["job_id"] != first["job_id"]


def test_worker_recovers_only_matching_final_file(tmp_path: Path) -> None:
    initialize(tmp_path, "demo", journal_mode="DELETE")
    actor = create_demo_actor()
    first = request_peer_update(actor, "600001.SH", "crash-1", tmp_path, "demo")
    claimed = worker._claim(tmp_path, "demo")
    assert claimed and claimed["job_id"] == first["job_id"]
    payload = json.loads(claimed["payload_json"])
    fixture = Path(__file__).parent / "tests/fixtures/peer_complete_v1.json"
    snap = json.loads(fixture.read_text(encoding="utf-8"))
    snap["workspace_meta"] = {
        "job_id": first["job_id"],
        "intent_hash": payload["intent_hash"],
        "target_date": payload["target_date"],
        "anchor": payload["anchor"],
    }
    final = worker._final_path(tmp_path, first["job_id"])
    final.parent.mkdir(parents=True)
    final.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
    worker.run_worker(tmp_path, "demo", None, once=True)
    assert get_update_job(actor, first["job_id"], tmp_path, "demo")["status"] == "succeeded"

    second = request_peer_update(actor, "600001.SH", "crash-2", tmp_path, "demo")
    assert worker._claim(tmp_path, "demo") is not None
    wrong = worker._final_path(tmp_path, second["job_id"])
    snap["workspace_meta"]["job_id"] = first["job_id"]  # Forged file for a different job.
    wrong.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
    worker.run_worker(tmp_path, "demo", None, once=True)
    assert get_update_job(actor, second["job_id"], tmp_path, "demo")["status"] == "failed"


def test_worker_lock_and_production_token_preflight(tmp_path: Path, monkeypatch) -> None:
    initialize(tmp_path, "demo", journal_mode="DELETE")
    lock = os.open(tmp_path / "worker.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(WorkspaceError, match="already active"):
            worker.run_worker(tmp_path, "demo", None, once=True)
    finally:
        os.close(lock)

    other = tmp_path / "production"
    initialize(other, "production", journal_mode="DELETE")
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    with pytest.raises(WorkspaceError, match="own TuShare Token"):
        worker.run_worker(other, "production", tmp_path, once=True)
    assert not (other / "worker.lock").exists()


def test_production_request_freezes_watchlist_and_proved_date(tmp_path: Path, monkeypatch) -> None:
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 24, 0, 30, tzinfo=ZoneInfo("Asia/Shanghai"))

    monkeypatch.setattr(services, "datetime", FixedDatetime)
    state = tmp_path / "state"
    initialize(state, "production", journal_mode="DELETE")
    tracker = tmp_path / "synthetic_tracker"
    (tracker / "a_stock_tracker").mkdir(parents=True)
    config = tracker / "a_stock_tracker/config.py"
    config.write_text(
        "WATCHLIST = [{'code': '600900', 'name': '合成甲'}, {'code': '920001.BJ', 'name': '合成北交'}]\n"
    )
    (tracker / "data").mkdir()
    calendar = tracker / "data/trading_calendar.json"
    proof = {
        "source": "tushare.trade_cal:SSE;SYNTHETIC_TEST",
        "covered_from": "2026-09-01",
        "covered_to": "2026-09-23",
        "as_of": "2026-09-23",
        "dates": ["2026-09-22", "2026-09-23"],
    }
    calendar.write_text(json.dumps(proof))
    actor = create_owner_actor("123", "123")
    first = request_peer_update(actor, "600900.SH", "frozen-1", state, "production", tracker)
    assert first["target_date"] == "2026-09-23"
    proof["dates"].append("2026-09-24")
    calendar.write_text(json.dumps(proof))
    with pytest.raises(ServiceError, match="交易日历"):
        services._peer_target_date(tracker, "production")
    proof["dates"].pop()
    config.write_text("WATCHLIST = [{'code': '600001', 'name': '合成乙'}]\n")
    proof["covered_to"] = "2026-09-22"
    calendar.write_text(json.dumps(proof))
    assert (
        request_peer_update(actor, "600900.SH", "frozen-1", state, "production", tracker)["job_id"]
        == first["job_id"]
    )
    with connect_workspace(state, "production") as conn:
        frozen = dict(
            conn.execute("SELECT * FROM update_jobs WHERE job_id=?", (first["job_id"],)).fetchone()
        )
    assert worker._payload(frozen)["watchlist"] == [
        {"code": "600900.SH", "name": "合成甲"},
        {"code": "920001.BJ", "name": "合成北交"},
    ]
    captured = []

    def no_network(root, anchor, target, watchlist, refresh_financials=False):
        assert refresh_financials is True
        captured.extend(watchlist)
        raise ScreenError("synthetic stop before network")

    monkeypatch.setattr(worker, "build_live_snapshot", no_network)
    claimed = worker._claim(state, "production")
    assert claimed is not None
    worker._process(state, "production", tracker, claimed)
    assert captured == [
        {"code": "600900", "name": "合成甲"},
        {"code": "920001", "name": "合成北交"},
    ]
    with pytest.raises(ServiceError, match="交易日历"):
        services._peer_target_date(tracker, "production")


def test_worker_refresh_does_not_promote_old_financial_cache(tmp_path: Path, monkeypatch) -> None:
    def reports(roe: float) -> list[dict]:
        return [
            {
                "end_date": f"{year}1231",
                "ann_date": f"{year + 1}0415",
                "roe_waa": roe,
                "update_flag": "0",
                "source": "tushare.fina_indicator",
                "acquired_at": "2026-04-16T08:00:00+08:00",
            }
            for year in (2023, 2024, 2025)
        ]

    monkeypatch.setattr(screen, "read_local_financials", lambda *_: reports(10))
    calls: list[str] = []

    def fetch(_client, _token, code, _data_date, _screened_at):
        calls.append(code)
        return reports(20)

    monkeypatch.setattr(screen, "fetch_financials", fetch)
    inputs = (
        [{"ts_code": "600001.SH", "name": "合成参照", "industry": "合成行业"}],
        {"600001.SH": {"pb": 2.0, "total_mv": 100.0}},
        {"watchlist_codes": ["600001"]},
        tmp_path,
        "2026-09-20",
        object(),
        "synthetic",
        {
            "stock_basic_acquired_at": "2026-09-20T12:00:00+08:00",
            "daily_basic_acquired_at": "2026-09-20T12:00:00+08:00",
        },
    )
    old_rows, _ = screen.load_inputs(*inputs)
    assert old_rows[0]["roe_mean"] == 10 and not calls
    assert "financial_checked_at" not in old_rows[0]  # CLI semantics stay unchanged.
    fresh_rows, completed_at = screen.load_inputs(*inputs, refresh_financials=True)
    assert calls == ["600001.SH"]
    assert fresh_rows[0]["roe_mean"] == 20
    assert fresh_rows[0]["financial_checked_at"] >= completed_at

    def fail(_client, _token, _code, _data_date, _screened_at):
        raise ScreenError("synthetic provider failure")

    monkeypatch.setattr(screen, "fetch_financials", fail)
    failed_rows, _ = screen.load_inputs(*inputs, refresh_financials=True)
    assert failed_rows[0]["roe_mean"] is None
    assert failed_rows[0]["financial_checked_at"] is None
    assert "FINANCIAL_REQUEST_FAILED" in failed_rows[0]["exclusions"]
    assert "synthetic provider failure" not in str(failed_rows)
