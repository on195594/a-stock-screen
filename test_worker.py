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
from services import (
    ServiceError,
    get_company_context,
    get_update_job,
    mark_seen,
    request_peer_update,
    request_watch_update,
    save_watch,
)
from watch import _fetch_row, build_watch_snapshot
from workspace import (
    WorkspaceError,
    add_watch_item,
    connect_workspace,
    get_run,
    get_watch_item,
    import_snapshot,
    initialize,
    utc_now,
)


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


def _watch_setup(tmp_path):
    initialize(tmp_path, "demo", journal_mode="DELETE")
    actor = create_demo_actor()
    run = import_snapshot(
        tmp_path, Path(__file__).parent / "tests/fixtures/peer_complete_v1.json", "demo"
    )
    for code in ("600001.SH", "600002.SH"):
        save_watch(actor, code, run, {}, 0, tmp_path, "demo")
    return actor, run


def _watch_final(job):
    payload = worker._payload(job)
    snap = build_watch_snapshot(payload["codes"], payload["target_date"], "demo", lambda: False)
    snap["workspace_meta"] = {
        "job_id": job["job_id"],
        "intent_hash": payload["intent_hash"],
        "target_date": payload["target_date"],
        "anchor": None,
        "expected_codes": payload["codes"],
    }
    return snap


def test_watch_freezes_scope_aliases_and_date_before_retries(tmp_path, monkeypatch):
    actor, run = _watch_setup(tmp_path)
    with connect_workspace(tmp_path, "demo") as conn:
        conn.execute("UPDATE watch_items SET status='paused' WHERE code='600002.SH'")
    job = request_watch_update(actor, "watch-first", tmp_path, "demo")
    alias = request_watch_update(actor, "watch-alias", tmp_path, "demo")
    assert alias["job_id"] == job["job_id"] and job["codes"] == ["600001.SH"]
    with pytest.raises(ServiceError, match="不同更新意图"):
        request_peer_update(actor, "600001.SH", "watch-first", tmp_path, "demo")
    with connect_workspace(tmp_path, "demo") as conn:
        conn.execute("UPDATE watch_items SET status='paused'")
        conn.execute("UPDATE watch_items SET status='research' WHERE code='600002.SH'")
    monkeypatch.setattr(
        services,
        "_peer_target_date",
        lambda *_: (_ for _ in ()).throw(AssertionError("must not re-read calendar")),
    )
    for request in ("watch-first", "watch-alias"):
        assert request_watch_update(actor, request, tmp_path, "demo")["job_id"] == job["job_id"]
    worker.run_worker(tmp_path, "demo", None, once=True)
    result = get_update_job(actor, job["job_id"], tmp_path, "demo")
    assert result["phase"] == "complete"
    ctx = get_company_context(actor, "600001.SH", tmp_path, "demo")
    assert ctx["displayed_run_id"] == result["result_run_id"]
    assert ctx["peer_rank"]["valuation_date"] == "2026-09-20"
    assert ctx["ack_run_id"] is None
    assert get_company_context(actor, "600002.SH", tmp_path, "demo")["displayed_run_id"] == run
    with connect_workspace(tmp_path, "demo") as conn:
        assert get_run(conn, run)["kind"] == "peer"
        item = get_watch_item(conn, "600001.SH")
    seen = mark_seen(
        actor, "600001.SH", result["result_run_id"], item["revision"], tmp_path, "demo"
    )
    assert seen["ack_run_id"] == result["result_run_id"]


@pytest.mark.parametrize("count", [0, 51])
def test_watch_empty_overflow_and_paused_refuse_without_calendar(tmp_path, monkeypatch, count):
    initialize(tmp_path, "demo", journal_mode="DELETE")
    run = import_snapshot(
        tmp_path, Path(__file__).parent / "tests/fixtures/peer_complete_v1.json", "demo"
    )
    with connect_workspace(tmp_path, "demo") as conn:
        for index in range(count):
            add_watch_item(
                conn, code=f"{600000 + index}.SH", name="合成", anchor_code=None, added_run_id=run
            )
    monkeypatch.setattr(
        services,
        "_peer_target_date",
        lambda *_: (_ for _ in ()).throw(AssertionError("calendar not needed")),
    )
    with pytest.raises(ServiceError, match="没有未暂停|超过50"):
        request_watch_update(create_demo_actor(), "bad-scope", tmp_path, "demo")
    with connect_workspace(tmp_path, "demo") as conn:
        conn.execute("UPDATE watch_items SET status='paused'")
        assert conn.execute("SELECT count(*) FROM update_jobs").fetchone()[0] == 0
    with pytest.raises(ServiceError, match="没有未暂停"):
        request_watch_update(create_demo_actor(), "paused", tmp_path, "demo")


@pytest.mark.parametrize("window", ["file", "registered", "missing"])
def test_watch_recovery_publication_windows(tmp_path, monkeypatch, window):
    actor, _ = _watch_setup(tmp_path)
    requested = request_watch_update(actor, "recover-watch", tmp_path, "demo")
    job = worker._claim(tmp_path, "demo")
    if window != "missing":
        final = worker._final_path(tmp_path, job["job_id"])
        final.parent.mkdir(parents=True, exist_ok=True)
        final.write_text(json.dumps(_watch_final(job)))
        if window == "registered":
            with monkeypatch.context() as m:
                m.setattr(
                    worker,
                    "_finish",
                    lambda *a, **kw: (_ for _ in ()).throw(OSError("simulated crash")),
                )
                with pytest.raises(OSError):
                    worker._register(tmp_path, "demo", job, final)
    worker.run_worker(tmp_path, "demo", None, once=True)
    result = get_update_job(actor, requested["job_id"], tmp_path, "demo")
    assert result["status"] == ("interrupted" if window == "missing" else "succeeded")
    worker.run_worker(tmp_path, "demo", None, once=True)
    with connect_workspace(tmp_path, "demo") as conn:
        assert conn.execute("SELECT count(*) FROM screen_runs WHERE kind='watch'").fetchone()[
            0
        ] == (0 if window == "missing" else 1)


@pytest.mark.parametrize(
    "corruption", ["job", "intent", "date", "scope", "duplicate", "ranking", "timestamp", "hash"]
)
def test_watch_recovery_rejects_mismatched_final(tmp_path, corruption):
    actor, _ = _watch_setup(tmp_path)
    request_watch_update(actor, "bad-final", tmp_path, "demo")
    job = worker._claim(tmp_path, "demo")
    snap = _watch_final(job)
    if corruption == "job":
        snap["workspace_meta"]["job_id"] = "wrong"
    elif corruption == "intent":
        snap["workspace_meta"]["intent_hash"] = "wrong"
    elif corruption == "date":
        snap["data_date"] = "2026-09-19"
    elif corruption == "scope":
        snap["workspace_meta"]["expected_codes"] = ["600003.SH"]
    elif corruption == "duplicate":
        snap["rows"][1] = snap["rows"][0]
    elif corruption == "ranking":
        snap["results"] = {"ranking": []}
    elif corruption == "timestamp":
        snap["screened_at"] = "2020-01-01T00:00:00+00:00"
    elif corruption == "hash":
        with connect_workspace(tmp_path, "demo") as conn:
            conn.execute(
                "UPDATE update_jobs SET dedupe_key='wrong' WHERE job_id=?", (job["job_id"],)
            )
    final = worker._final_path(tmp_path, job["job_id"])
    final.parent.mkdir(parents=True, exist_ok=True)
    final.write_text(json.dumps(snap))
    worker.run_worker(tmp_path, "demo", None, once=True)
    assert get_update_job(actor, job["job_id"], tmp_path, "demo")["status"] == "failed"
    with connect_workspace(tmp_path, "demo") as conn:
        assert (
            conn.execute("SELECT count(*) FROM screen_runs WHERE kind='watch'").fetchone()[0] == 0
        )


def test_watch_partial_preserves_old_facts_and_blocks_ack(tmp_path, monkeypatch):
    actor, first = _watch_setup(tmp_path)
    job = request_watch_update(actor, "partial-watch", tmp_path, "demo")

    def partial(codes, target, mode, stop):
        snap = build_watch_snapshot(codes, target, mode, stop)
        snap["rows"][0].update(pb=None, facts_usable=False)
        return snap

    monkeypatch.setattr(worker, "build_watch_snapshot", partial)
    worker.run_worker(tmp_path, "demo", None, once=True)
    result = get_update_job(actor, job["job_id"], tmp_path, "demo")
    assert result["phase"] == "partial"
    gap = get_company_context(actor, "600001.SH", tmp_path, "demo")
    assert gap["has_latest_attempt_gap"] and gap["displayed_run_id"] == first
    assert not gap["comparison"]["can_ack"]
    with pytest.raises(ServiceError):
        mark_seen(actor, "600001.SH", first, gap["revision"], tmp_path, "demo")
    good = get_company_context(actor, "600002.SH", tmp_path, "demo")
    assert good["displayed_run_id"] == result["result_run_id"] and good["comparison"]["can_ack"]


def test_watch_failed_job_without_snapshot_is_a_gap_until_success(tmp_path, monkeypatch):
    actor, first = _watch_setup(tmp_path)
    baseline = mark_seen(actor, "600001.SH", first, 1, tmp_path, "demo")
    job = request_watch_update(actor, "failure-no-run", tmp_path, "demo")
    with monkeypatch.context() as m:
        m.setattr(
            worker,
            "build_watch_snapshot",
            lambda *a: (_ for _ in ()).throw(ScreenError("secret-provider-value")),
        )
        worker.run_worker(tmp_path, "demo", None, once=True)
    result = get_update_job(actor, job["job_id"], tmp_path, "demo")
    assert result["status"] == "failed" and result["result_run_id"] is None
    assert "secret-provider-value" not in json.dumps(result)
    ctx = get_company_context(actor, "600001.SH", tmp_path, "demo")
    assert ctx["latest_attempt_job_id"] == job["job_id"] and ctx["displayed_run_id"] == first
    assert ctx["has_latest_attempt_gap"] and not ctx["comparison"]["can_ack"]
    assert "旧可用资料" in ctx["latest_attempt_error"]
    assert services.get_home(actor, tmp_path, "demo")["needs_review_count"] == 2
    with pytest.raises(ServiceError):
        mark_seen(actor, "600001.SH", first, baseline["revision"], tmp_path, "demo")
    services.dismiss_update_job(actor, job["job_id"], tmp_path, "demo")
    assert get_company_context(actor, "600001.SH", tmp_path, "demo")["has_latest_attempt_gap"]
    request_watch_update(actor, "explicit-retry", tmp_path, "demo")
    worker.run_worker(tmp_path, "demo", None, once=True)
    assert not get_company_context(actor, "600001.SH", tmp_path, "demo")["has_latest_attempt_gap"]


def test_watch_failure_without_prior_run_shows_no_usable_facts(tmp_path, monkeypatch):
    from workspace import register_run

    initialize(tmp_path, "demo", journal_mode="DELETE")
    actor = create_demo_actor()
    with connect_workspace(tmp_path, "demo") as conn:
        register_run(
            conn,
            run_id="run_unverified",
            kind="peer",
            anchor_code="600001.SH",
            rule_id="peer-screen-v1",
            captured_at="2026-09-20T00:00:00+00:00",
            valuation_date="2026-09-20",
            health="unverified",
            snapshot_path="snapshots/dummy.json",
            snapshot_bytes=b"{}",
        )
        add_watch_item(
            conn, code="600001.SH", name="合成", added_run_id="run_unverified", anchor_code=None
        )
    request_watch_update(actor, "first-failure", tmp_path, "demo")
    with monkeypatch.context() as m:
        m.setattr(
            worker,
            "build_watch_snapshot",
            lambda *a: (_ for _ in ()).throw(ScreenError("synthetic-error")),
        )
        worker.run_worker(tmp_path, "demo", None, once=True)
    home = services.get_home(actor, tmp_path, "demo")
    assert "暂无可用事实" in home["watch_items"][0]["change_summary"]
    ctx = get_company_context(actor, "600001.SH", tmp_path, "demo")
    assert "暂无可用资料" in ctx["latest_attempt_error"]
    assert ctx["has_latest_attempt_gap"] and not ctx["comparison"]["can_ack"]


def test_watch_validation_respects_shanghai_announcement_date_across_utc_boundary(tmp_path):
    actor, _ = _watch_setup(tmp_path)
    request_watch_update(actor, "timezone-check", tmp_path, "demo")
    job = worker._claim(tmp_path, "demo")
    # UTC 2026-09-27 18:00 is Shanghai 2026-09-28 02:00.
    job["requested_at"] = "2026-09-27T18:00:00+00:00"
    snap = _watch_final(job)
    snap["screened_at"] = "2026-09-27T18:00:01+00:00"
    for r in snap["rows"]:
        r["financial_checked_at"] = "2026-09-27T18:00:00.500000+00:00"
    snap["rows"][0]["annual_roes"][-1]["ann_date"] = "2026-09-28"
    worker._validate_snapshot(snap, job, "demo")
    snap["rows"][0]["annual_roes"][-1]["ann_date"] = "2026-09-29"
    with pytest.raises(WorkspaceError, match="announcement from future"):
        worker._validate_snapshot(snap, job, "demo")


def test_watch_fifty_fixed_targets_and_concurrent_tabs(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    actor, first = _watch_setup(tmp_path)
    with connect_workspace(tmp_path, "demo") as conn:
        for index in range(3, 51):
            add_watch_item(conn, code=f"{600000 + index}.SH", name="合成", added_run_id=first)
    monkeypatch.setattr(
        services,
        "peer_anchors",
        lambda *a: (_ for _ in ()).throw(AssertionError("not the tracker pool")),
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = list(
            pool.map(
                lambda index: request_watch_update(actor, f"tab-{index}", tmp_path, "demo"),
                range(2),
            )
        )
    assert jobs[0]["job_id"] == jobs[1]["job_id"] and len(jobs[0]["codes"]) == 50
    worker.run_worker(tmp_path, "demo", None, once=True)
    result = get_update_job(actor, jobs[0]["job_id"], tmp_path, "demo")
    assert result["phase"] == "partial"
    snap = json.loads(worker._final_path(tmp_path, jobs[0]["job_id"]).read_text())
    assert [row["code"] for row in snap["rows"]] == jobs[0]["codes"]
    assert snap["rows"][-1]["code"] == "600050.SH" and not snap["rows"][-1]["facts_usable"]
    with connect_workspace(tmp_path, "demo") as conn:
        assert conn.execute("SELECT count(*) FROM update_jobs").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM screen_runs WHERE kind='peer'").fetchone()[0] == 1


def test_watch_request_revocation_before_commit_rolls_back(tmp_path, monkeypatch):
    from auth import AuthError, check_actor

    actor, _ = _watch_setup(tmp_path)
    calls = 0

    def revoke_before_commit(a):
        nonlocal calls
        calls += 1
        if calls == 2:
            a.revoke()
        check_actor(a)

    monkeypatch.setattr(services, "check_actor", revoke_before_commit)
    with pytest.raises(AuthError):
        request_watch_update(actor, "revoked-in-transaction", tmp_path, "demo")
    with connect_workspace(tmp_path, "demo") as conn:
        assert conn.execute("SELECT count(*) FROM update_jobs").fetchone()[0] == 0


def test_watch_stop_and_single_worker_lock(tmp_path, monkeypatch):
    actor, _ = _watch_setup(tmp_path)
    queued = request_watch_update(actor, "watch-stop", tmp_path, "demo")
    with (tmp_path / "worker.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(WorkspaceError, match="already active"):
            worker.run_worker(tmp_path, "demo", None, once=True)
    job = worker._claim(tmp_path, "demo")
    monkeypatch.setattr(worker, "STOP", True)
    worker._process(tmp_path, "demo", None, job)
    assert get_update_job(actor, queued["job_id"], tmp_path, "demo")["status"] == "interrupted"
    assert not worker._final_path(tmp_path, job["job_id"]).exists()


def test_watch_queue_limit_and_revoked_actor(tmp_path, monkeypatch):
    from auth import AuthError

    actor, _ = _watch_setup(tmp_path)
    days = iter(("2026-09-20", "2026-09-21", "2026-09-22", "2026-09-23"))
    monkeypatch.setattr(services, "_peer_target_date", lambda *_: next(days))
    for index in range(3):
        request_watch_update(actor, f"queued-{index}", tmp_path, "demo")
    with pytest.raises(ServiceError, match="已满"):
        request_watch_update(actor, "overflow", tmp_path, "demo")
    actor.revoke()
    with pytest.raises(AuthError):
        request_watch_update(actor, "queued-0", tmp_path, "demo")
    with connect_workspace(tmp_path, "demo") as conn:
        assert conn.execute("SELECT count(*) FROM update_jobs").fetchone()[0] == 3


def test_watch_provider_fetches_fixed_code_despite_nonpositive_and_st(tmp_path, monkeypatch):
    import pandas as pd
    import tushare as ts

    import watch

    calls = []

    class Client:
        def stock_basic(self, **kw):
            calls.append(("basic", kw["ts_code"]))
            return pd.DataFrame(
                [
                    dict(
                        ts_code=kw["ts_code"],
                        name="*ST合成",
                        industry="金融",
                        market="其他",
                        exchange="BSE",
                        list_status="L",
                    )
                ]
            )

        def daily_basic(self, **kw):
            calls.append(("valuation", kw["ts_code"]))
            return pd.DataFrame(
                [dict(ts_code=kw["ts_code"], trade_date="20260920", pb=-1.2, total_mv=1)]
            )

        def fina_indicator(self, **kw):
            calls.append(("financial", kw["ts_code"]))
            return pd.DataFrame(
                [
                    dict(
                        ts_code=kw["ts_code"],
                        ann_date=f"{year + 1}0415",
                        end_date=f"{year}1231",
                        update_flag="0",
                        roe_waa=-float(year - 2020),
                    )
                    for year in (2023, 2024, 2025)
                ]
            )

    actor, first = _watch_setup(tmp_path)
    monkeypatch.setattr(
        screen, "rank_peers", lambda *a: (_ for _ in ()).throw(AssertionError("no ranking"))
    )
    monkeypatch.setenv("TUSHARE_TOKEN", "synthetic-token")
    monkeypatch.setattr(ts, "pro_api", lambda token, timeout: Client())
    requested = utc_now()
    production = build_watch_snapshot(["800001.BJ"], "2026-09-20", "production", lambda: False)
    row = production["rows"][0]
    assert row["pb"] < 0 and row["roe_mean"] < 0 and row["facts_usable"]
    assert row["risk_status"] == "known_warning"
    assert calls == [("basic", "800001.BJ"), ("valuation", "800001.BJ"), ("financial", "800001.BJ")]
    with connect_workspace(tmp_path, "demo") as conn:
        conn.execute("DELETE FROM watch_items")
        add_watch_item(conn, code="800001.BJ", name="合成", anchor_code=None, added_run_id=first)
    request_watch_update(actor, "production-shaped", tmp_path, "demo")
    job = worker._claim(tmp_path, "demo")
    job["requested_at"] = requested
    snap = _watch_final(job)
    snap.update(rows=[row], source="tracker", is_fixture=False, screened_at=utc_now())
    worker._validate_snapshot(snap, job, "production")
    snap["rows"][0]["annual_roes"][0]["source"] = "fixture"
    with pytest.raises(WorkspaceError, match="fixture"):
        worker._validate_snapshot(snap, job, "production")
    # One provider failure keeps the other facts and never stores the exception/Token.
    monkeypatch.setattr(
        Client,
        "daily_basic",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("synthetic-token sensitive")),
    )
    row = watch._fetch_row(Client(), "synthetic-token", "800001.BJ", "2026-09-20")
    assert row["pb"] is None and row["annual_roes"] and not row["facts_usable"]
    assert "synthetic-token" not in json.dumps(row) and "sensitive" not in json.dumps(row)
    monkeypatch.setattr(
        Client,
        "fina_indicator",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("synthetic-token")),
    )
    row = _fetch_row(Client(), "synthetic-token", "800001.BJ", "2026-09-20")
    assert (
        row["financial_status"] == "failed"
        and not row.get("financial_checked_at")
        and not row["facts_usable"]
    )
    assert "synthetic-token" not in json.dumps(row)
    before = len(calls)
    with pytest.raises(InterruptedError):
        build_watch_snapshot(["800001.BJ"], "2026-09-20", "production", lambda: True)
    assert len(calls) == before


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


@pytest.mark.parametrize(
    "error,expected",
    [
        ("金融行业不适用 peer-screen-v1: secret-provider-value", "金融行业不适用"),
        ("参照公司行业不明", "行业不明"),
        ("参照公司不是当前沪深主板上市公司", "沪深主板"),
        ("provider failed: secret-provider-value", "更新未完成"),
    ],
)
def test_worker_business_failure_is_actionable_without_provider_text(
    tmp_path, monkeypatch, error, expected
):
    initialize(tmp_path, "demo", journal_mode="DELETE")
    actor = create_demo_actor()
    request_peer_update(actor, "600001.SH", "failure", tmp_path, "demo")
    job = worker._claim(tmp_path, "demo")

    def fail(*args, **kwargs):
        raise ScreenError(error)

    monkeypatch.setattr(worker, "build_live_snapshot", fail)
    # Exercise only the production provider branch; all DB access stays in this synthetic workspace.
    monkeypatch.setattr(
        worker, "_finish", lambda state, mode, job, run, phase, message: messages.append(message)
    )
    messages = []
    worker._process(tmp_path, "production", tmp_path, job)
    assert expected in messages[0]
    assert "secret-provider-value" not in messages[0]
