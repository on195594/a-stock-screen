"""Single-process worker for user-submitted peer updates; no web session or OAuth secret."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import signal
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from screen import RULE, ScreenError, build_live_snapshot
from workspace import Mode, WorkspaceError, connect_workspace, get_run, import_snapshot, utc_now

STOP = False


def _stop(signum: int, frame: Any) -> None:
    global STOP
    STOP = True


def _payload(job: dict[str, Any]) -> dict[str, Any]:
    payload = json.loads(job["payload_json"])
    watchlist = payload.get("watchlist")
    if (
        not isinstance(watchlist, list)
        or not watchlist
        or not all(
            isinstance(item, dict)
            and isinstance(item.get("code"), str)
            and re.fullmatch(r"\d{6}\.(?:SH|SZ|BJ)", item["code"])
            and isinstance(item.get("name"), str)
            for item in watchlist
        )
        or len({item["code"] for item in watchlist}) != len(watchlist)
        or payload.get("anchor") not in {item["code"] for item in watchlist}
    ):
        raise WorkspaceError("invalid frozen watchlist")
    proof = payload.get("intent_hash")
    canonical = {key: value for key, value in payload.items() if key != "intent_hash"}
    digest = hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()
    if (
        job["kind"] != "peer"
        or payload.get("intent") != {"kind": "peer", "anchor": payload.get("anchor")}
        or payload.get("rule_id") != RULE
        or proof != digest
        or job["dedupe_key"] != digest
    ):
        raise WorkspaceError("invalid job identity")
    return payload


def _validate_snapshot(snap: dict[str, Any], job: dict[str, Any], mode: Mode) -> None:
    payload = _payload(job)
    meta = snap.get("workspace_meta")
    source = "fixture" if mode == "demo" else "tracker"
    scope = snap.get("scope")
    rows = snap.get("rows")
    frozen_codes = {item["code"] for item in payload["watchlist"]}
    observed_codes = snap.get("watchlist_codes")
    if (
        not isinstance(meta, dict)
        or meta.get("job_id") != job["job_id"]
        or meta.get("intent_hash") != payload["intent_hash"]
        or meta.get("target_date") != payload["target_date"]
        or meta.get("anchor") != payload["anchor"]
        or snap.get("anchor") != payload["anchor"]
        or snap.get("data_date") != payload["target_date"]
        or snap.get("rule") != RULE
        or snap.get("source") != source
        or not isinstance(scope, dict)
        or scope.get("source") != source
        or not isinstance(observed_codes, list)
        or not all(
            isinstance(code, str)
            and re.fullmatch(r"\d{6}(?:\.(?:SH|SZ|BJ))?", code)
            and ("." not in code or code in frozen_codes)
            for code in observed_codes
        )
        or len(observed_codes) != len(frozen_codes)
        or {code.split(".")[0] for code in observed_codes}
        != {code.split(".")[0] for code in frozen_codes}
        or not isinstance(rows, list)
        or not 0 < len(rows) <= 50
        or not all(isinstance(r, dict) for r in rows)
        or {r.get("code") for r in rows} != set(scope.get("selected_codes") or [])
    ):
        raise WorkspaceError("job snapshot does not match frozen intent or scope")


def _finish(
    state_dir: Path,
    mode: Mode,
    job: dict[str, Any],
    run_id: str | None,
    phase: str,
    summary: str | None = None,
    snapshot_sha256: str | None = None,
) -> None:
    conn = connect_workspace(state_dir, mode)
    try:
        conn.execute("BEGIN IMMEDIATE")
        if run_id:
            run = get_run(conn, run_id)
            payload = _payload(job)
            if (
                not run
                or run["kind"] != "peer"
                or run["anchor_code"] != payload["anchor"]
                or run["valuation_date"] != payload["target_date"]
                or run["rule_id"] != RULE
                or run["snapshot_sha256"] != snapshot_sha256
                or run["health"] == "unverified"
            ):
                raise WorkspaceError("registered run is not a verified result of this job")
            phase = "partial" if run["health"] == "partial" else "complete"
        status = "succeeded" if run_id else ("interrupted" if phase == "interrupted" else "failed")
        changed = conn.execute(
            """UPDATE update_jobs SET status=?,phase=?,processed=?,total=1,
            updated_at=?,finished_at=?,error_code=?,error_summary=?,result_run_id=?
            WHERE job_id=? AND status='running'""",
            (
                status,
                phase,
                1 if run_id else 0,
                utc_now(),
                utc_now(),
                None if run_id else phase,
                summary,
                run_id,
                job["job_id"],
            ),
        )
        if changed.rowcount != 1:
            raise WorkspaceError("job no longer running")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _final_path(state_dir: Path, job_id: str) -> Path:
    return state_dir / "snapshots" / "jobs" / f"{job_id}.json"


def _publish(state_dir: Path, mode: Mode, job: dict[str, Any], snap: dict[str, Any]) -> None:
    _validate_snapshot(snap, job, mode)
    final = _final_path(state_dir, job["job_id"])
    final.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    raw = (json.dumps(snap, ensure_ascii=False, allow_nan=False, sort_keys=True) + "\n").encode()
    tmp = final.with_name(f".{job['job_id']}.{os.getpid()}.tmp")
    try:
        with tmp.open("xb") as file:
            os.chmod(tmp, 0o600)
            file.write(raw)
            file.flush()
            os.fsync(file.fileno())
        os.link(tmp, final)  # Never overwrite a final snapshot, including on restart.
    finally:
        tmp.unlink(missing_ok=True)
    fd = os.open(final.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    _register(state_dir, mode, job, final)


def _register(state_dir: Path, mode: Mode, job: dict[str, Any], final: Path) -> None:
    raw = final.read_bytes()
    snap = json.loads(raw)
    _validate_snapshot(snap, job, mode)
    run_id = import_snapshot(state_dir, final, mode)
    _finish(
        state_dir,
        mode,
        job,
        run_id,
        "complete",
        snapshot_sha256=hashlib.sha256(raw).hexdigest(),
    )


def _recover(state_dir: Path, mode: Mode) -> None:
    conn = connect_workspace(state_dir, mode)
    try:
        jobs = [
            dict(row) for row in conn.execute("SELECT * FROM update_jobs WHERE status='running'")
        ]
    finally:
        conn.close()
    for job in jobs:
        final = _final_path(state_dir, job["job_id"])
        if not final.is_file():
            _finish(state_dir, mode, job, None, "interrupted", "上次运行中断，请手动重试")
            continue
        try:
            _register(state_dir, mode, job, final)
        except (WorkspaceError, ScreenError, ValueError, OSError, TypeError):
            _finish(state_dir, mode, job, None, "failed", "已保存的结果无法验证，请手动重试")


def _claim(state_dir: Path, mode: Mode) -> dict[str, Any] | None:
    conn = connect_workspace(state_dir, mode)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM update_jobs WHERE status='queued' ORDER BY requested_at,job_id LIMIT 1"
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        now = utc_now()
        conn.execute(
            """UPDATE update_jobs SET status='running',phase='fetching',total=1,
            started_at=?,updated_at=? WHERE job_id=? AND status='queued'""",
            (now, now, row["job_id"]),
        )
        conn.commit()
        return dict(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _process(state_dir: Path, mode: Mode, tracker_root: Path | None, job: dict[str, Any]) -> None:
    try:
        payload = _payload(job)
        if mode == "demo":
            fixture = Path(__file__).parent / "tests" / "fixtures" / "peer_complete_v1.json"
            snap = json.loads(fixture.read_text(encoding="utf-8"))
            snap["screened_at"] = datetime.now(UTC).isoformat()
            snap["generated_at"] = snap["screened_at"]
        else:
            if tracker_root is None:
                raise WorkspaceError("tracker directory unavailable")
            frozen = [
                {"code": item["code"].split(".")[0], "name": item["name"]}
                for item in payload["watchlist"]
            ]
            snap = build_live_snapshot(
                tracker_root,
                payload["anchor"],
                payload["target_date"],
                frozen,
                refresh_financials=True,
            )
        if STOP:
            _finish(state_dir, mode, job, None, "interrupted", "更新中断，请手动重试")
            return
        snap["workspace_meta"] = {
            "job_id": job["job_id"],
            "intent_hash": payload["intent_hash"],
            "anchor": payload["anchor"],
            "target_date": payload["target_date"],
        }
        _publish(state_dir, mode, job, snap)
    except (WorkspaceError, ScreenError, OSError, ValueError, TypeError) as exc:
        # Provider exceptions may contain credentials; never log or persist their text.
        print(f"Peer job {job['job_id']} failed ({type(exc).__name__})", flush=True)
        if _final_path(state_dir, job["job_id"]).is_file():
            # Preserve running for recovery after a crash between file and DB publication.
            raise WorkspaceError("final snapshot needs recovery") from None
        summary = "更新未完成，请核查数据来源并重试"
        if isinstance(exc, ScreenError):
            if str(exc).startswith("金融行业不适用 peer-screen-v1:"):
                summary = "金融行业不适用于同业筛选，请改选非金融参照公司"
            elif str(exc) == "参照公司行业不明":
                summary = "参照公司行业不明，无法建立同业范围，请改选参照"
            elif str(exc) == "参照公司不是当前沪深主板上市公司":
                summary = "参照公司不在当前沪深主板范围，请改选参照"
        _finish(state_dir, mode, job, None, "failed", summary)


def run_worker(state_dir: Path, mode: Mode, tracker_root: Path | None, once: bool = False) -> None:
    if mode not in ("demo", "production"):
        raise WorkspaceError("worker mode must be demo or production")
    if mode == "production":
        if not os.getenv("TUSHARE_TOKEN", "").strip() or tracker_root is None:
            raise WorkspaceError("worker requires its own TuShare Token and read-only tracker")
        for rel in ("a_stock_tracker/config.py", "data/trading_calendar.json"):
            if not (tracker_root / rel).is_file():
                raise WorkspaceError("worker tracker inputs unavailable")
    conn = connect_workspace(state_dir, mode)
    conn.close()
    lock_fd = os.open(state_dir / "worker.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WorkspaceError("worker already active") from exc
        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
        _recover(state_dir, mode)
        while not STOP:
            job = _claim(state_dir, mode)
            if job:
                _process(state_dir, mode, tracker_root, job)
            elif once:
                break
            else:
                time.sleep(1)
    finally:
        os.close(lock_fd)


def main() -> int:
    parser = argparse.ArgumentParser(description="User-initiated peer update worker")
    parser.add_argument("--state-dir", type=Path, default=Path(".local/demo"))
    parser.add_argument("--mode", choices=("demo", "production"), default="demo")
    parser.add_argument("--tracker-root", type=Path)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    try:
        run_worker(args.state_dir, args.mode, args.tracker_root, args.once)
    except WorkspaceError as exc:
        print(f"Worker unavailable: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
