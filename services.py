"""Application services: Home, Peer Discovery, Company Details, and Research State."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import urllib.parse
from datetime import date
from pathlib import Path
from typing import Any

from auth import Actor, check_actor
from screen import normalize_flag
from workspace import (
    Mode,
    WorkspaceError,
    _parse_report_date,
    add_watch_item,
    connect_workspace,
    get_run,
    get_watch_item,
    is_row_usable,
    list_runs,
    list_watch_items,
    mark_watch_ack,
    save_watch_item,
    watch_run_targets,
)

logger = logging.getLogger(__name__)


class ServiceError(RuntimeError):
    pass


def read_verified_snapshot(
    state_dir: Path, snapshot_rel_path: str, expected_sha256: str | None = None
) -> dict[str, Any]:
    """Read a snapshot JSON from state_dir, validating path boundaries and optional SHA-256."""
    state_dir = state_dir.expanduser().resolve()
    full_path = (state_dir / snapshot_rel_path).resolve()

    try:
        full_path.relative_to(state_dir)
    except ValueError as exc:
        raise ServiceError(f"snapshot path traversal detected: {snapshot_rel_path}") from exc

    if not full_path.is_file():
        raise ServiceError(f"snapshot file missing: {snapshot_rel_path}")

    try:
        raw_bytes = full_path.read_bytes()
    except OSError as exc:
        raise ServiceError(f"failed to read snapshot file: {exc}") from exc
    if expected_sha256:
        computed_sha = hashlib.sha256(raw_bytes).hexdigest()
        if computed_sha != expected_sha256:
            raise ServiceError(f"snapshot hash mismatch for {snapshot_rel_path}")

    try:
        return json.loads(raw_bytes.decode("utf-8"))
    except Exception as exc:
        raise ServiceError("failed to load snapshot JSON") from exc


def safe_rows(snap: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(snap, dict):
        return []
    rows = snap.get("rows")
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict)]


def safe_ranking(snap: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(snap, dict):
        return []
    res = snap.get("results")
    if not isinstance(res, dict):
        return []
    rk = res.get("ranking")
    if not isinstance(rk, list):
        return []
    return [r for r in rk if isinstance(r, dict)]


def _run_covers_stock(snap: dict[str, Any], kind: str, code: str) -> bool:
    if kind == "watch":
        meta = snap.get("workspace_meta")
        return isinstance(meta, dict) and code in (meta.get("expected_codes") or [])
    scope = snap.get("scope")
    return snap.get("anchor") == code or (
        isinstance(scope, dict) and code in (scope.get("selected_codes") or [])
    )


def _annual_facts(row: dict[str, Any]) -> list[dict[str, Any]]:
    facts = []
    for item in row.get("annual_roes") or []:
        if not isinstance(item, dict):
            continue
        period_end = _parse_report_date(item.get("period") or item.get("end_date"))
        if period_end is None and item.get("year") is not None:
            period_end = date(item["year"], 12, 31)
        ann_date = _parse_report_date(item.get("ann_date"))
        # Home compares only usable rows, whose annual periods, dates and ROEs are validated.
        assert period_end is not None and ann_date is not None
        roe = item.get("roe_waa") if item.get("roe_waa") is not None else item.get("roe")
        assert roe is not None
        facts.append(
            {
                "period_end": period_end.isoformat(),
                "roe": float(roe),
                "ann_date": ann_date.isoformat(),
                "report_type": item.get("report_type"),
                "update_flag": normalize_flag(item.get("update_flag")),
            }
        )
    return sorted(facts, key=lambda item: item["period_end"])


def _watch_anchor(conn: sqlite3.Connection, item: dict[str, Any] | None) -> str | None:
    if not item:
        return None
    if item.get("anchor_code"):
        return str(item["anchor_code"])
    added = get_run(conn, item["added_run_id"]) if item.get("added_run_id") else None
    return str(added["anchor_code"]) if added and added.get("anchor_code") else None


def _is_run_relevant_to_stock(
    conn: sqlite3.Connection, run: dict[str, Any], code: str, item_anchor: str | None
) -> bool:
    if run.get("health") not in ("complete", "partial"):
        return False
    if run.get("kind") == "peer":
        return run.get("anchor_code") == code or bool(
            item_anchor and run.get("anchor_code") == item_anchor
        )
    return run.get("kind") == "watch" and code in (watch_run_targets(conn, run["run_id"]) or ())


def get_home(actor: Actor, state_dir: Path, mode: Mode) -> dict[str, Any]:
    """Retrieve overview of personal research watchlist and unreviewed changes."""
    check_actor(actor)
    conn = connect_workspace(state_dir, mode)
    try:
        items = list_watch_items(conn)
        runs = list_runs(conn)
        # Cache only watched rows and coverage for this request; recheck hashes next time.
        watched_codes = {it["code"] for it in items}
        snapshots: dict[
            str,
            tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], set[str]] | ServiceError,
        ] = {}

        def snapshot_for(
            run: dict[str, Any],
        ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], set[str]] | ServiceError:
            run_id = run["run_id"]
            if run_id not in snapshots:
                try:
                    snap = read_verified_snapshot(
                        state_dir, run["snapshot_path"], run["snapshot_sha256"]
                    )
                    rows: dict[str, dict[str, Any]] = {}
                    usable_rows: dict[str, dict[str, Any]] = {}
                    for row in safe_rows(snap):
                        code = row.get("code")
                        if isinstance(code, str) and code in watched_codes:
                            if code not in rows:
                                rows[code] = row  # Latest scan uses the first matching row.
                            if code not in usable_rows and is_row_usable(row):
                                usable_rows[code] = row  # Ack baseline uses the first usable row.
                    covered = (
                        {
                            code
                            for code in watched_codes
                            if _run_covers_stock(snap, run["kind"], code)
                        }
                        if isinstance(snap, dict)
                        else set()
                    )
                    snapshots[run_id] = rows, usable_rows, covered
                except ServiceError as exc:
                    logger.warning("Skipping invalid snapshot for run %s: %s", run_id, exc)
                    snapshots[run_id] = exc
            return snapshots[run_id]

        overview_items = []
        needs_review_count = 0

        for it in items:
            code = it["code"]
            ack_run_id = it.get("ack_run_id")
            item_anchor = _watch_anchor(conn, it)

            latest_run = None
            latest_row = None
            latest_corrupt_run = None
            latest_corrupt_index = None
            latest_index = None
            usable_run = None
            for index, r in enumerate(runs):
                if r.get("health") == "unverified":
                    continue
                cached = snapshot_for(r)
                if isinstance(cached, ServiceError):
                    if latest_corrupt_run is None and _is_run_relevant_to_stock(
                        conn, r, code, item_anchor
                    ):
                        latest_corrupt_run = r
                        latest_corrupt_index = index
                    continue
                rows, _, covered_codes = cached
                row = rows.get(code)
                if (row is not None or code in covered_codes) and latest_run is None:
                    latest_run = r
                    latest_row = row
                    latest_index = index
                if (
                    row is not None
                    and is_row_usable(row)
                    and r["health"] in ("complete", "partial")
                ):
                    if usable_run is None or (r["valuation_date"] or "") > (
                        usable_run["valuation_date"] or ""
                    ):
                        usable_run = r

            ack_run = get_run(conn, ack_run_id) if ack_run_id else None
            latest_date = latest_run["valuation_date"] if latest_run else None
            regression = bool(
                latest_date
                and (
                    (usable_run and latest_date < (usable_run["valuation_date"] or ""))
                    or (
                        ack_run
                        and ack_run["valuation_date"]
                        and latest_date < ack_run["valuation_date"]
                    )
                )
            )
            item_date = (usable_run["valuation_date"] if usable_run else latest_date) or "暂无"
            change_summary = ""
            has_change = False

            if latest_corrupt_index is not None and (
                latest_index is None or latest_corrupt_index < latest_index
            ):
                has_change = True
                change_summary = (
                    "最新快照文件损坏或无法读取，仍展示上次可用资料"
                    if usable_run
                    else "最新快照文件损坏或无法读取"
                )
            elif regression:
                has_change = True
                change_summary = "最新运行估值日倒退异常，仍展示上次可用资料"
            elif not ack_run_id:
                has_change = True
                if latest_run and latest_row is None:
                    change_summary = "首次待阅（最新运行覆盖该标的但缺少事实数据：数据缺口）"
                elif latest_row and not is_row_usable(latest_row):
                    gap = (
                        "本次财务更新失败"
                        if latest_row.get("financial_status") == "failed"
                        else "本次数据缺口"
                    )
                    change_summary = f"首次待阅（{gap}）"
                else:
                    change_summary = "首次待阅"
            elif latest_run and latest_run["run_id"] != ack_run_id:
                ack_row = None
                if ack_run and ack_run["health"] != "unverified":
                    cached = snapshot_for(ack_run)
                    if not isinstance(cached, ServiceError):
                        ack_row = cached[1].get(code)

                if latest_row is None:
                    has_change = True
                    change_summary = (
                        "最新运行覆盖该标的但缺少事实数据（数据缺口），仍展示上次可用资料"
                    )
                elif ack_row is None:
                    has_change = True
                    change_summary = "已阅基准文件异常，无法比对变化"
                elif not is_row_usable(latest_row):
                    has_change = True
                    change_summary = (
                        "本次财务更新失败，仍展示上次资料"
                        if latest_row.get("financial_status") == "failed"
                        else "本次数据缺口，仍展示上次可用资料"
                    )
                elif latest_row and ack_row and ack_run:
                    old_val_date = ack_row.get("valuation_date") or ack_run.get("valuation_date")
                    new_val_date = latest_row.get("valuation_date") or latest_run.get(
                        "valuation_date"
                    )
                    if _annual_facts(latest_row) != _annual_facts(ack_row):
                        has_change = True
                        change_summary = "采用的年报数据有变化"
                    elif latest_row.get("pb") != ack_row.get("pb"):
                        has_change = True
                        change_summary = f"PB变动: {ack_row.get('pb')} → {latest_row.get('pb')}"
                    elif new_val_date != old_val_date:
                        has_change = True
                        change_summary = f"估值日期变动: {old_val_date} → {new_val_date}"
                    else:
                        change_summary = "本工具覆盖的字段暂无未阅变化"
                else:
                    change_summary = "本工具覆盖的字段暂无未阅变化"

            if has_change:
                needs_review_count += 1

            overview_items.append(
                {
                    "code": it["code"],
                    "name": it["name"],
                    "status": it["status"],
                    "reason": it["reason"],
                    "has_change": has_change,
                    "change_summary": change_summary,
                    "revision": it["revision"],
                    "valuation_date": item_date,
                }
            )

        dates = {it["valuation_date"] for it in overview_items if it["valuation_date"] != "暂无"}
        return {
            "valuation_date": next(iter(dates))
            if len(dates) == 1
            else "各公司数据日不同"
            if dates
            else "暂无",
            "needs_review_count": needs_review_count,
            "total_watch_count": len(items),
            "watch_items": overview_items,
        }
    finally:
        conn.close()


def get_company_context(
    actor: Actor,
    code: str,
    state_dir: Path,
    mode: Mode,
    include_personal_notes: bool = False,
) -> dict[str, Any]:
    """Retrieve full context for a company: usable facts, peer ranks, and personal state."""
    check_actor(actor)
    conn = connect_workspace(state_dir, mode)
    try:
        watch_item = get_watch_item(conn, code)
        item_anchor = _watch_anchor(conn, watch_item)
        runs = list_runs(conn)

        usable_fact_run = None
        usable_fact_row = None
        latest_attempt_run = None
        latest_attempt_row = None
        latest_attempt_error = None
        last_peer_rank = None
        last_peer_rank_key: tuple[str, str, str] | None = None

        for r in runs:
            if r.get("health") == "unverified":
                continue
            try:
                snap = read_verified_snapshot(state_dir, r["snapshot_path"], r["snapshot_sha256"])
            except ServiceError as exc:
                logger.warning("Skipping invalid snapshot for run %s: %s", r["run_id"], exc)
                if not latest_attempt_run and _is_run_relevant_to_stock(conn, r, code, item_anchor):
                    latest_attempt_run = r
                    latest_attempt_error = "最新快照文件损坏或无法读取"
                continue
            if r["kind"] == "peer" and r.get("health") == "complete":
                # Match the peer board's valuation_date/captured_at/run_id ordering.
                key = (r["valuation_date"] or "", r["captured_at"], r["run_id"])
                if last_peer_rank_key is None or key > last_peer_rank_key:
                    for rk in safe_ranking(snap):
                        if rk.get("code") == code:
                            last_peer_rank = {
                                "position": rk.get("position"),
                                "research_order": rk.get("research_order"),
                                "anchor": snap.get("anchor") if isinstance(snap, dict) else None,
                                "valuation_date": r.get("valuation_date"),
                            }
                            last_peer_rank_key = key
                            break

            row = next(
                (row for row in safe_rows(snap) if (row.get("code") or row.get("ts_code")) == code),
                None,
            )
            covered = isinstance(snap, dict) and _run_covers_stock(snap, r["kind"], code)
            if not latest_attempt_run and (row is not None or covered):
                latest_attempt_run = r
                latest_attempt_row = row
                if row is None:
                    latest_attempt_error = "最新运行覆盖该标的但缺少事实数据（数据缺口）"
            if (
                row is not None
                and is_row_usable(row)
                and r.get("health") in ("complete", "partial")
                and (
                    not usable_fact_run
                    or (r.get("valuation_date") or "")
                    > (usable_fact_run.get("valuation_date") or "")
                )
            ):
                usable_fact_run = r
                usable_fact_row = row

        ack_run = (
            get_run(conn, watch_item["ack_run_id"])
            if watch_item and watch_item["ack_run_id"]
            else None
        )
        latest_date = latest_attempt_run.get("valuation_date") if latest_attempt_run else None
        if (
            not latest_attempt_error
            and latest_date
            and (
                (usable_fact_run and latest_date < (usable_fact_run.get("valuation_date") or ""))
                or (
                    ack_run
                    and ack_run.get("valuation_date")
                    and latest_date < ack_run["valuation_date"]
                )
            )
        ):
            latest_attempt_error = "估值日期倒退异常"

        name = ""
        if usable_fact_row:
            name = usable_fact_row.get("name", "")
        elif latest_attempt_row:
            name = latest_attempt_row.get("name", "")
        elif watch_item:
            name = watch_item.get("name", "")

        usable_fact_data = None
        if usable_fact_row:
            norm_roes = []
            for item in usable_fact_row.get("annual_roes", []):
                if isinstance(item, dict):
                    r_val = (
                        item.get("roe_waa") if item.get("roe_waa") is not None else item.get("roe")
                    )
                    year_str = (
                        str(item.get("year") or "")
                        or (str(item.get("end_date") or "")[:4])
                        or (str(item.get("period") or "")[:4])
                    )
                    norm_roes.append(
                        {
                            "year": year_str,
                            "roe": r_val,
                            "ann_date": item.get("ann_date"),
                        }
                    )
            usable_fact_data = {
                **usable_fact_row,
                "financial_status": usable_fact_row.get("financial_status")
                or (
                    ", ".join(str(x) for x in (usable_fact_row.get("exclusions") or []))
                    if usable_fact_row.get("exclusions")
                    else "ok"
                ),
                "annual_roes": norm_roes,
            }

        context: dict[str, Any] = {
            "code": code,
            "name": name,
            "is_watched": watch_item is not None,
            "watch_status": watch_item.get("status") if watch_item else None,
            "revision": watch_item.get("revision") if watch_item else 0,
            "ack_run_id": watch_item.get("ack_run_id") if watch_item else None,
            "displayed_run_id": usable_fact_run.get("run_id") if usable_fact_run else None,
            "usable_fact": usable_fact_data,
            "usable_valuation_date": usable_fact_run.get("valuation_date")
            if usable_fact_run
            else None,
            "latest_attempt_run_id": latest_attempt_run.get("run_id")
            if latest_attempt_run
            else None,
            "latest_attempt_date": latest_attempt_run.get("valuation_date")
            if latest_attempt_run
            else None,
            "latest_attempt_status": latest_attempt_row.get("financial_status")
            if latest_attempt_row
            else None,
            "latest_attempt_error": latest_attempt_error,
            "has_latest_attempt_gap": bool(
                latest_attempt_error
                or (
                    latest_attempt_run
                    and latest_attempt_run != usable_fact_run
                    and latest_attempt_row
                    and not is_row_usable(latest_attempt_row)
                )
            ),
            "has_usable_facts": usable_fact_data is not None,
            "peer_rank": last_peer_rank,
        }

        if include_personal_notes and watch_item:
            context["reason"] = watch_item.get("reason", "")
            context["next_check"] = watch_item.get("next_check", "")
            context["note_url"] = watch_item.get("note_url")
        else:
            context["reason"] = ""
            context["next_check"] = ""
            context["note_url"] = None

        return context
    finally:
        conn.close()


def save_watch(
    actor: Actor,
    code: str,
    source_run_id: str,
    fields: dict[str, Any],
    expected_revision: int,
    state_dir: Path,
    mode: Mode,
) -> dict[str, Any]:
    """Add a new company or update personal research notes with revision checking."""
    check_actor(actor)
    conn = connect_workspace(state_dir, mode)
    try:
        run = get_run(conn, source_run_id)
        if not run:
            raise ServiceError(f"source run not found: {source_run_id}")
        if run.get("health") == "unverified":
            raise ServiceError(f"cannot add watch item from unverified run: {source_run_id}")

        snap = read_verified_snapshot(state_dir, run["snapshot_path"], run["snapshot_sha256"])
        name = ""
        matched_row = None
        for r in safe_rows(snap):
            if r.get("code") == code:
                matched_row = r
                name = r.get("name", "")
                break

        if not matched_row:
            raise ServiceError(f"source run {source_run_id} does not contain stock {code}")

        status = fields.get("status", "observe")
        reason = fields.get("reason", "")
        next_check = fields.get("next_check", "")
        note_url = fields.get("note_url")

        if status not in ("research", "observe", "paused"):
            raise ServiceError("invalid personal status")
        if len(reason) > 1000 or len(next_check) > 1000:
            raise ServiceError("personal text is too long")
        if note_url is not None:
            if len(note_url) > 2048:
                raise ServiceError("note URL is too long")
            parsed = urllib.parse.urlparse(note_url)
            if parsed.scheme != "https" or not parsed.netloc:
                raise ServiceError("note URL must use https")
            if parsed.username is not None or parsed.password is not None:
                raise ServiceError("note URL cannot contain credentials")

        check_actor(actor)
        conn.execute("BEGIN IMMEDIATE;")
        try:
            check_actor(actor)
            if run.get("anchor_code"):
                conn.execute(
                    "UPDATE watch_items SET anchor_code=? WHERE code=? AND anchor_code IS NULL",
                    (run["anchor_code"], code),
                )
            if expected_revision == 0:
                added = add_watch_item(
                    conn,
                    code=code,
                    name=name,
                    added_run_id=source_run_id,
                    anchor_code=run.get("anchor_code"),
                )
                if not added:
                    # Duplicate addition must NEVER overwrite existing user notes! Return existing record as-is.
                    existing = get_watch_item(conn, code)
                    assert existing is not None
                    conn.commit()
                    return dict(existing)

                # Newly added: default revision is 1. Apply initial user fields if provided.
                if reason or (status != "observe") or next_check or note_url:
                    save_watch_item(
                        conn,
                        code=code,
                        expected_revision=1,
                        status=status,
                        reason=reason,
                        next_check=next_check,
                        note_url=note_url,
                    )
            else:
                existing = get_watch_item(conn, code)
                if not existing:
                    raise ServiceError("watch item not found")
                # Idempotent retry check: if identical fields were already saved, return existing
                is_identical = (
                    existing["status"] == status
                    and (existing["reason"] or "") == reason
                    and (existing["next_check"] or "") == next_check
                    and (existing["note_url"] or None) == note_url
                )
                if is_identical and existing["revision"] in (
                    expected_revision,
                    expected_revision + 1,
                ):
                    conn.commit()
                    return dict(existing)

                save_watch_item(
                    conn,
                    code=code,
                    expected_revision=expected_revision,
                    status=status,
                    reason=reason,
                    next_check=next_check,
                    note_url=note_url,
                )

            updated = get_watch_item(conn, code)
            assert updated is not None
            conn.commit()
            return dict(updated)
        except Exception:
            conn.rollback()
            raise
    except WorkspaceError as exc:
        raise ServiceError(str(exc)) from exc
    finally:
        conn.close()


def mark_seen(
    actor: Actor,
    code: str,
    displayed_run_id: str,
    expected_revision: int,
    state_dir: Path,
    mode: Mode,
) -> dict[str, Any]:
    """Mark changes seen up to the currently displayed run."""
    check_actor(actor)
    conn = connect_workspace(state_dir, mode)
    try:
        check_actor(actor)
        conn.execute("BEGIN IMMEDIATE;")
        try:
            check_actor(actor)
            mark_watch_ack(
                conn,
                code=code,
                displayed_run_id=displayed_run_id,
                expected_revision=expected_revision,
                state_dir=state_dir,
            )
            updated = get_watch_item(conn, code)
            assert updated is not None
            conn.commit()
            return dict(updated)
        except Exception:
            conn.rollback()
            raise
    except WorkspaceError as exc:
        raise ServiceError(str(exc)) from exc
    finally:
        conn.close()


def get_peer_discover(
    actor: Actor,
    anchor_code: str,
    state_dir: Path,
    mode: Mode,
) -> dict[str, Any]:
    """Get peer screening results for an anchor company."""
    check_actor(actor)
    conn = connect_workspace(state_dir, mode)
    try:
        candidates = conn.execute(
            """SELECT * FROM screen_runs
            WHERE kind='peer' AND anchor_code=? AND health='complete'
            ORDER BY valuation_date DESC, captured_at DESC, run_id DESC""",
            (anchor_code,),
        )
        newest_damaged = None
        for run in candidates:
            try:
                snap = read_verified_snapshot(
                    state_dir, run["snapshot_path"], run["snapshot_sha256"]
                )
            except ServiceError as exc:
                logger.warning("Skipping invalid peer snapshot for run %s: %s", run["run_id"], exc)
                if newest_damaged is None:
                    newest_damaged = run["run_id"]
                continue
            break
        else:
            if newest_damaged:
                return {
                    "has_run": False,
                    "anchor_code": anchor_code,
                    "error": "最新同业快照损坏或无法读取",
                }
            return {
                "has_run": False,
                "anchor_code": anchor_code,
                "message": f"暂无参照公司 {anchor_code} 的完整同业筛选结果",
            }

        results_data: dict[str, Any] = (
            dict(snap["results"])
            if isinstance(snap, dict) and isinstance(snap.get("results"), dict)
            else {}
        )
        return {
            "has_run": True,
            "run_id": run["run_id"],
            "anchor_code": anchor_code,
            "valuation_date": run["valuation_date"],
            "health": run["health"],
            "warning": (
                f"最新同业运行 ({newest_damaged}) 快照损坏，展示历史可用同业榜 "
                f"(运行: {run['run_id']}, 估值日: {run['valuation_date']})"
                if newest_damaged
                else None
            ),
            "results": {
                **results_data,
                "ranking": safe_ranking(snap),
            },
            "rows": safe_rows(snap),
        }
    finally:
        conn.close()
