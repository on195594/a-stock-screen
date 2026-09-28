"""Application services: Home, Peer Discovery, Company Details, and Research State."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import urllib.parse
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

from auth import Actor, check_actor
from screen import (
    ScreenError,
    annual_entries,
    candidate_review_sections,
    fmt_number,
    normalize_flag,
    read_watchlist,
)
from workspace import (
    Mode,
    WorkspaceError,
    _parse_report_date,
    add_watch_item,
    connect_workspace,
    get_run,
    get_watch_item,
    is_row_usable,
    latest_watch_failure,
    list_runs,
    list_watch_items,
    mark_watch_ack,
    save_watch_item,
    utc_now,
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


def roe_trend(row: dict[str, Any]) -> str:
    """Describe validated annual facts, not investment quality or future returns."""
    if (
        not is_row_usable(row)
        or row.get("facts_usable") is False
        or row.get("financial_status") == "failed"
    ):
        return "ROE趋势：资料有缺口，暂不判断"
    annual = _annual_facts(row)
    values = [a["roe"] for a in annual]
    if values[0] < values[1] < values[2]:
        trend = "连续上升"
    elif values[0] > values[1] > values[2]:
        trend = "连续下降"
    elif values[0] == values[1] == values[2]:
        trend = "三年相同"
    else:
        trend = "非单调变化"
    warning = "；最新年度ROE为负，均值不能掩盖这一点" if values[-1] < 0 else ""
    return (
        f"ROE趋势（{annual[0]['period_end'][:4]}—{annual[-1]['period_end'][:4]}）：{trend}{warning}"
    )


def peer_fact_insights(run: dict[str, Any], snap: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Presentation only: one complete, same-date/year cohort; never alter ranks."""
    rows = [r for r in safe_rows(snap) if isinstance(r.get("code"), str)]
    ranks = safe_ranking(snap)
    codes = {r["code"] for r in ranks if isinstance(r.get("code"), str)}
    cohort = [r for r in rows if r.get("code") in codes]
    reason = "不是完整的当前规则同业榜"
    comparable = (
        run.get("kind") == "peer"
        and run.get("health") == "complete"
        and run.get("rule_id") == "peer-screen-v1"
        and isinstance(snap, dict)
        and isinstance(snap.get("results"), dict)
        and snap["results"].get("discovery_complete") is True
    )
    if comparable:
        if len(codes) < 2:
            reason = "合格样本不足2家"
            comparable = False
        elif (
            len(codes) != len(ranks)
            or len(cohort) != len(codes)
            or {r.get("code") for r in cohort} != codes
            or any(
                not is_row_usable(r)
                or r["pb"] <= 0
                or r["roe_mean"] <= 0
                or r.get("exclusions")
                or r.get("facts_usable") is False
                or r.get("financial_status") == "failed"
                for r in cohort
            )
        ):
            reason = "合格样本有缺失或异常"
            comparable = False
        elif (
            any(r["valuation_date"] != run.get("valuation_date") for r in cohort)
            or len({tuple(a["period_end"] for a in _annual_facts(r)) for r in cohort}) != 1
        ):
            reason = "估值日或财务年度不一致"
            comparable = False
    mid = median(float(r["pb"]) for r in cohort) if comparable else None
    insights = {}
    for row in rows:
        comparison = f"PB中位数暂不比较：{reason}"
        if mid is not None:
            if row.get("code") not in codes:
                comparison = "未进入本次合格样本，不参与PB中位数比较"
            else:
                pb = float(row["pb"])
                relation = "低于" if pb < mid else "高于" if pb > mid else "等于"
                comparison = (
                    f"PB{relation}本次同口径合格样本中位数 {fmt_number(mid)} 倍"
                    f"（{len(cohort)}家，含本公司）；不等于低估或高估"
                )
        insights[row.get("code", "")] = {
            "pb_comparison": comparison,
            "roe_trend": roe_trend(row),
        }
    return insights


def _company_comparison(
    conn: sqlite3.Connection,
    state_dir: Path,
    code: str,
    displayed_run_id: str | None,
    ack_run_id: str | None,
    *,
    snapshot_cache: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compare the exact acknowledged and displayed snapshots, never an intermediate run."""

    def facts(run_id: str) -> dict[str, Any]:
        run = get_run(conn, run_id)
        if not run or run["health"] not in ("complete", "partial"):
            raise ServiceError("运行不可用于事实对照")
        if run["kind"] == "peer" and run["rule_id"] != "peer-screen-v1":
            raise ServiceError("未知同业规则")
        snap = None
        if snapshot_cache is not None:
            snap = snapshot_cache.get(run_id)
        if snap is None:
            snap = read_verified_snapshot(state_dir, run["snapshot_path"], run["snapshot_sha256"])
            if snapshot_cache is not None:
                snapshot_cache[run_id] = snap
        rows = [r for r in safe_rows(snap) if r.get("code") == code]
        if (
            len(rows) != 1
            or not is_row_usable(rows[0])
            or rows[0].get("facts_usable") is False
            or rows[0].get("financial_status") == "failed"
        ):
            raise ServiceError("公司事实缺失、重复或不可用")
        row = rows[0]
        scope = snap.get("scope")
        scope = scope if isinstance(scope, dict) else {}
        members = (
            scope.get("selected_codes")
            if run["kind"] == "peer"
            else watch_run_targets(conn, run_id)
        )
        if members is not None and (
            not isinstance(members, (list, set)) or any(not isinstance(c, str) for c in members)
        ):
            raise ServiceError("范围成员异常")
        fields: dict[str, Any] = {
            "估值日": row["valuation_date"],
            "PB（倍）": row["pb"],
            "ROE三年均值（%）": row["roe_mean"],
            "名称风险标记": row.get("risk_status"),
            "上市状态": row.get("list_status"),
        }
        for annual in _annual_facts(row):
            year = annual["period_end"][:4]
            fields[f"{year}年ROE（%）"] = annual["roe"]
            fields[f"{year}年公告日"] = annual["ann_date"]
            fields[f"{year}年报告类型"] = annual["report_type"]
            fields[f"{year}年修订标记"] = annual["update_flag"]
        fields.update(
            {
                "估值来源": row.get("valuation_source"),
                "财务来源": row.get("financial_source"),
                "规则": run.get("rule_id") or "固定关注资料（无同业排名）",
                "运行类型": "同业扫描" if run["kind"] == "peer" else "固定关注资料",
                "参照公司": run.get("anchor_code"),
                "运行完整性": "完整" if run["health"] == "complete" else "部分完成",
                "行业": scope.get("industry"),
                "筛选排除项": tuple(sorted(row.get("exclusions") or [])),
                "本次范围成员": tuple(sorted(members)) if members is not None else None,
            }
        )
        return fields

    blocked = {"can_ack": False, "items": []}
    if not displayed_run_id:
        return {**blocked, "summary": "暂无可用事实，不能确认已阅"}
    try:
        current = facts(displayed_run_id)
    except ServiceError:
        return {**blocked, "summary": "当前资料异常或规则未知，不能确认已阅"}
    if ack_run_id:
        try:
            previous = facts(ack_run_id)
        except ServiceError:
            return {**blocked, "summary": "已阅基准异常或规则未知，无法对照；不自动改用其他基准"}
    else:
        previous = {}
    items = [
        {
            "label": key,
            "before": previous.get(key),
            "after": current.get(key),
            "changed": previous.get(key) != current.get(key),
        }
        for key in dict.fromkeys([*previous, *current])
    ]
    changed = [i["label"] for i in items if i["changed"]]
    if not ack_run_id:
        summary = "首次待阅：无已阅基准，核对当前完整事实后可确认"
    elif ack_run_id == displayed_run_id:
        summary = "本页所示资料已阅"
    elif not changed:
        summary = "本工具覆盖的字段暂无未阅变化（仅核查时间变化不算财务变化）"
    elif changed == ["估值日"]:
        summary = "仅估值日期更新，所列事实值未变"
    else:
        summary = "以下标有【变化】的字段与上次已阅资料不同"
    return {"can_ack": True, "summary": summary, "items": items}


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
        comp_cache: dict[str, dict[str, Any]] = {}

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
            usable_row = None
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
                        usable_row = row

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
            change_tier = "no_change"

            if latest_corrupt_index is not None and (
                latest_index is None or latest_corrupt_index < latest_index
            ):
                has_change = True
                change_tier = "anomaly"
                change_summary = (
                    "最新快照文件损坏或无法读取，仍展示上次可用资料"
                    if usable_run
                    else "最新快照文件损坏或无法读取"
                )
            elif latest_watch_failure(
                conn, code, latest_run["captured_at"] if latest_run else None
            ):
                has_change = True
                change_tier = "anomaly"
                change_summary = (
                    "最近固定关注更新未完成，仍展示上次可用资料；请查看最近更新"
                    if usable_run
                    else "最近固定关注更新未完成，暂无可用事实；请查看最近更新"
                )
            elif regression:
                has_change = True
                change_tier = "anomaly"
                change_summary = "最新运行估值日倒退异常，仍展示上次可用资料"
            elif not ack_run_id:
                has_change = True
                if latest_run and latest_row is None:
                    change_tier = "anomaly"
                    change_summary = "首次待阅（最新运行覆盖该标的但缺少事实数据：数据缺口）"
                elif latest_row and not is_row_usable(latest_row):
                    change_tier = "anomaly"
                    gap = (
                        "本次财务更新失败"
                        if latest_row.get("financial_status") == "failed"
                        else "本次数据缺口"
                    )
                    change_summary = f"首次待阅（{gap}）"
                elif latest_row and (
                    latest_row.get("risk_status") == "known_warning"
                    or latest_row.get("list_status") in ("D", "P")
                ):
                    change_tier = "risk_change"
                    change_summary = "首次待阅：存在名称风险警示或非正常上市状态，请核查"
                else:
                    change_tier = "fact_change"
                    change_summary = "首次待阅"
            elif latest_run and latest_run["run_id"] != ack_run_id:
                ack_row = None
                if ack_run and ack_run["health"] != "unverified":
                    cached = snapshot_for(ack_run)
                    if not isinstance(cached, ServiceError):
                        ack_row = cached[1].get(code)

                if latest_row is None:
                    has_change = True
                    change_tier = "anomaly"
                    change_summary = (
                        "最新运行覆盖该标的但缺少事实数据（数据缺口），仍展示上次可用资料"
                    )
                elif ack_row is None:
                    has_change = True
                    change_tier = "anomaly"
                    change_summary = "已阅基准文件异常，无法比对变化"
                elif not is_row_usable(latest_row):
                    has_change = True
                    change_tier = "anomaly"
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
                    comparison = _company_comparison(
                        conn,
                        state_dir,
                        code,
                        latest_run["run_id"],
                        ack_run_id,
                        snapshot_cache=comp_cache,
                    )
                    changed_labels = {
                        field["label"] for field in comparison["items"] if field["changed"]
                    }
                    real_risk_change = False
                    if "名称风险标记" in changed_labels:
                        old_risk = ack_row.get("risk_status")
                        new_risk = latest_row.get("risk_status")
                        if not (
                            old_risk is None and new_risk == "name_check_clear_other_risks_unknown"
                        ):
                            real_risk_change = True
                    if "上市状态" in changed_labels:
                        old_list = ack_row.get("list_status")
                        new_list = latest_row.get("list_status")
                        if not (old_list is None and new_list == "L"):
                            real_risk_change = True

                    if not comparison["can_ack"]:
                        has_change = True
                        change_tier = "anomaly"
                        change_summary = comparison["summary"]
                    elif real_risk_change:
                        has_change = True
                        change_tier = "risk_change"
                        change_summary = "公司风险标记或上市状态有变化，请核查；未知不等于风险解除"
                    elif _annual_facts(latest_row) != _annual_facts(ack_row):
                        has_change = True
                        change_tier = "fact_change"
                        change_summary = "采用的年报数据有变化"
                    elif latest_row.get("pb") != ack_row.get("pb"):
                        has_change = True
                        change_tier = "fact_change"
                        change_summary = f"PB变动: {ack_row.get('pb')} → {latest_row.get('pb')}"
                    elif changed_labels - {"估值日"}:
                        has_change = True
                        change_tier = "fact_change"
                        change_summary = "来源、范围或资料口径有变化，请查看对照"
                    elif new_val_date != old_val_date:
                        has_change = True
                        change_tier = "date_change"
                        change_summary = f"估值日期变动: {old_val_date} → {new_val_date}"
                    else:
                        change_tier = "no_change"
                        change_summary = "本工具覆盖的字段暂无未阅变化"
                else:
                    change_tier = "no_change"
                    change_summary = "本工具覆盖的字段暂无未阅变化"

            if has_change and it["status"] != "paused":
                needs_review_count += 1

            overview_items.append(
                {
                    "code": it["code"],
                    "name": it["name"],
                    "status": it["status"],
                    "reason": it["reason"],
                    "next_check": it["next_check"],
                    "has_change": has_change,
                    "change_tier": change_tier,
                    "change_summary": change_summary,
                    "revision": it["revision"],
                    "updated_at": it["updated_at"],
                    "valuation_date": item_date,
                    "fact_summary": (
                        f"PB {fmt_number(usable_row.get('pb'))} 倍 · "
                        f"ROE三年均值 {fmt_number(usable_row.get('roe_mean'))}%"
                        if usable_row
                        else "暂无可用事实"
                    ),
                    "roe_trend": roe_trend(usable_row or {}),
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
        fact_insights: dict[str, str] = {}
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
                fact_insights = peer_fact_insights(r, snap).get(code, {})

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
            "updated_at": watch_item.get("updated_at") if watch_item else None,
            "ack_run_id": watch_item.get("ack_run_id") if watch_item else None,
            "displayed_run_id": usable_fact_run.get("run_id") if usable_fact_run else None,
            "displayed_kind": usable_fact_run.get("kind") if usable_fact_run else None,
            "latest_listing_status": (latest_attempt_row or {}).get("list_status"),
            "latest_risk_status": (latest_attempt_row or {}).get("risk_status"),
            "usable_fact": usable_fact_data,
            "fact_insights": fact_insights,
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

        failed_job = latest_watch_failure(
            conn, code, latest_attempt_run["captured_at"] if latest_attempt_run else None
        )
        if failed_job:
            context.update(
                latest_attempt_job_id=failed_job["job_id"],
                latest_attempt_run_id=None,
                latest_attempt_date=json.loads(failed_job["payload_json"])["target_date"],
                latest_attempt_status="failed",
                latest_attempt_error=(
                    "最近固定关注更新未完成；以下为旧可用资料，不可确认为本次已阅"
                    if usable_fact_run
                    else "最近固定关注更新未完成；暂无可用资料，不可确认为本次已阅"
                ),
                has_latest_attempt_gap=True,
            )
        context["comparison"] = _company_comparison(
            conn, state_dir, code, context["displayed_run_id"], context["ack_run_id"]
        )
        if context["has_latest_attempt_gap"]:
            context["comparison"]["can_ack"] = False
            context["comparison"]["summary"] = (
                "本次资料有缺口或日期异常；以下仅为旧可用事实对照，不可将旧资料标记为本次已阅"
            )
        check_actor(actor)
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
    *,
    expected_updated_at: str | None = None,
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

                if (
                    expected_updated_at is not None
                    and existing["updated_at"] != expected_updated_at
                ):
                    raise ServiceError("记录已变化或已删除后重新加入，请刷新后重试")
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
    *,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    """Mark changes seen up to the currently displayed run."""
    check_actor(actor)
    conn = connect_workspace(state_dir, mode)
    try:
        check_actor(actor)
        conn.execute("BEGIN IMMEDIATE;")
        try:
            check_actor(actor)
            if expected_updated_at is not None:
                item = get_watch_item(conn, code)
                if not item or (
                    item["updated_at"] != expected_updated_at
                    and item["ack_run_id"] != displayed_run_id
                ):
                    raise ServiceError("记录已变化或已删除后重新加入，请刷新后重试")
            item = get_watch_item(conn, code)
            # Revalidate the same baseline at confirmation, including damage after page load.
            if item and item["ack_run_id"] and item["ack_run_id"] != displayed_run_id:
                comparison = _company_comparison(
                    conn, state_dir, code, displayed_run_id, item["ack_run_id"]
                )
                if not comparison["can_ack"]:
                    raise ServiceError(comparison["summary"])
            mark_watch_ack(
                conn,
                code=code,
                displayed_run_id=displayed_run_id,
                expected_revision=expected_revision,
                state_dir=state_dir,
            )
            updated = get_watch_item(conn, code)
            assert updated is not None
            check_actor(actor)
            conn.commit()
            return dict(updated)
        except Exception:
            conn.rollback()
            raise
    except WorkspaceError as exc:
        raise ServiceError(str(exc)) from exc
    finally:
        conn.close()


def set_watch_status(
    actor: Actor,
    code: str,
    status: str,
    expected_revision: int,
    expected_updated_at: str,
    state_dir: Path,
    mode: Mode,
) -> None:
    """Pause or resume personal attention without touching notes, facts or acknowledgement."""
    check_actor(actor)
    if status not in ("observe", "paused"):
        raise ServiceError("快捷操作仅支持暂停或恢复为观察")
    conn = connect_workspace(state_dir, mode)
    try:
        conn.execute("BEGIN IMMEDIATE")
        check_actor(actor)
        changed = conn.execute(
            """UPDATE watch_items SET status=?, revision=revision+1, updated_at=?
            WHERE code=? AND revision=? AND updated_at=?""",
            (status, utc_now(), code, expected_revision, expected_updated_at),
        )
        if changed.rowcount != 1:
            raise ServiceError("记录已变化或已删除后重新加入，请刷新后重试")
        check_actor(actor)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def delete_watch(
    actor: Actor,
    code: str,
    expected_revision: int,
    expected_updated_at: str,
    state_dir: Path,
    mode: Mode,
) -> None:
    """Delete only personal state; shared immutable observations remain untouched."""
    check_actor(actor)
    conn = connect_workspace(state_dir, mode)
    try:
        conn.execute("BEGIN IMMEDIATE")
        check_actor(actor)
        removed = conn.execute(
            "DELETE FROM watch_items WHERE code=? AND revision=? AND updated_at=?",
            (code, expected_revision, expected_updated_at),
        )
        if removed.rowcount != 1:
            raise ServiceError("记录已变化或已删除，请刷新后重试")
        check_actor(actor)
        conn.commit()
    finally:
        # Closing an uncommitted connection rolls back, including revoked authorization.
        conn.close()


def dismiss_update_job(actor: Actor, job_id: str, state_dir: Path, mode: Mode) -> None:
    """Hide terminal failures, retaining request IDs so retries cannot trigger new work."""
    check_actor(actor)
    conn = connect_workspace(state_dir, mode)
    try:
        conn.execute("BEGIN IMMEDIATE")
        check_actor(actor)
        changed = conn.execute(
            "UPDATE update_jobs SET phase='dismissed' WHERE job_id=? "
            "AND status IN ('failed','interrupted') AND result_run_id IS NULL",
            (job_id,),
        )
        if changed.rowcount != 1:
            raise ServiceError("只能清理失败或中断且无结果的任务")
        check_actor(actor)
        conn.commit()
    finally:
        conn.close()


def job_status_label(job: dict[str, Any]) -> str:
    if job.get("status") == "succeeded":
        return "部分完成" if job.get("phase") == "partial" else "完成"
    return {"queued": "排队中", "running": "更新中", "failed": "失败", "interrupted": "已中断"}.get(
        str(job.get("status")), "状态未知"
    )


def _peer_result(run: dict[str, Any], snap: dict[str, Any]) -> dict[str, Any]:
    """Presentation facts only; never recompute eligibility or ranking."""
    results = dict(snap["results"]) if isinstance(snap.get("results"), dict) else {}
    insights = peer_fact_insights(run, snap)
    # Report formatter uses period/roe_waa; historical synthetic fixtures used year/roe.
    rows = [
        {
            **row,
            "fact_insights": insights.get(row.get("code", ""), {}),
            "annual_roes": [
                {
                    **a,
                    "period": a.get("period") or a.get("end_date") or str(a.get("year", "")),
                    "roe_waa": a.get("roe_waa") if a.get("roe_waa") is not None else a.get("roe"),
                }
                for a in annual_entries(row)
            ],
        }
        for row in safe_rows(snap)
    ]
    return {
        "run_id": run["run_id"],
        "valuation_date": run["valuation_date"],
        "captured_at": run["captured_at"],
        "health": run["health"],
        "results": {**results, "ranking": safe_ranking(snap)},
        "rows": rows,
        "scope": snap.get("scope") or {},
        "review_lines": candidate_review_sections({**snap, "rows": rows}),
        "source": snap.get("source") or "未记录",
        "limits": snap.get("limits") or {},
    }


def get_peer_discover(
    actor: Actor,
    anchor_code: str,
    state_dir: Path,
    mode: Mode,
    *,
    job_id: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Keep a verified complete board separate from the attempt (or pinned job).

    run_id pins the displayed board on return; job_id pins historical diagnostics.
    Neither a partial result nor a later run may impersonate the requested result.
    """
    check_actor(actor)
    conn = connect_workspace(state_dir, mode)
    try:
        runs = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM screen_runs WHERE kind='peer' AND anchor_code=? "
                "AND health IN ('complete','partial') ORDER BY captured_at DESC, run_id DESC",
                (anchor_code,),
            )
        ]
        job_row = conn.execute(
            "SELECT * FROM update_jobs WHERE kind='peer' AND phase!='dismissed' AND "
            + ("job_id=?" if job_id else "json_extract(payload_json,'$.anchor')=?")
            + " ORDER BY requested_at DESC, job_id DESC LIMIT 1",
            (job_id or anchor_code,),
        ).fetchone()
        job = _job_summary(job_row) if job_row else None
        if job_id and (not job or job["anchor"] != anchor_code):
            raise ServiceError("任务与参照不匹配或任务不存在")
        if run_id and not any(r["run_id"] == run_id and r["health"] == "complete" for r in runs):
            raise ServiceError("指定完整榜不存在或与参照不匹配")

        result_run = next((r for r in runs if job and r["run_id"] == job["result_run_id"]), None)
        latest = runs[0] if runs else None
        # Imported observations newer than the last job are attempts too.
        if not job_id and latest and job and latest["run_id"] != job["result_run_id"]:
            if datetime.fromisoformat(latest["captured_at"]) > datetime.fromisoformat(
                job["finished_at"] or job["requested_at"]
            ):
                job = None
        attempt_run = result_run if job else latest
        attempt = (
            dict(job)
            if job
            else ({"status": "succeeded", "phase": attempt_run["health"]} if attempt_run else None)
        )
        damaged: list[str] = []
        cache: dict[str, dict[str, Any] | None] = {}

        def load(run: dict[str, Any]) -> dict[str, Any] | None:
            key = run["run_id"]
            if key not in cache:
                try:
                    snap = read_verified_snapshot(
                        state_dir, run["snapshot_path"], run["snapshot_sha256"]
                    )
                    if not isinstance(snap, dict):
                        raise ServiceError("快照格式异常")
                    cache[key] = _peer_result(run, snap)
                except ServiceError:
                    damaged.append(key)
                    cache[key] = None
            return cache[key]

        if attempt is not None:
            attempt["label"] = job_status_label(attempt)
            attempt["result"] = load(attempt_run) if attempt_run else None
            if attempt_run:
                attempt["target_date"] = attempt_run["valuation_date"]
            if attempt_run and not attempt["result"]:
                attempt["error_summary"] = "快照损坏或无法读取"
            if job and job["result_run_id"] and result_run is None:
                attempt["error_summary"] = "任务结果不存在或与参照不匹配"

        candidates = sorted(
            (r for r in runs if r["health"] == "complete"),
            key=lambda r: (r["valuation_date"] or "", r["captured_at"], r["run_id"]),
            reverse=True,
        )
        if job_id:
            assert job is not None  # Validated against anchor above.
            # Historical jobs cannot silently open today's latest board.
            if result_run and result_run["health"] == "complete":
                candidates = [result_run]
            else:
                cutoff = datetime.fromisoformat(job["requested_at"])
                candidates = [
                    r for r in candidates if datetime.fromisoformat(r["captured_at"]) <= cutoff
                ]
        if run_id:
            candidates = [r for r in candidates if r["run_id"] == run_id]
        board = next((data for r in candidates if (data := load(r)) is not None), None)
        out: dict[str, Any] = {
            "has_run": board is not None,
            "anchor_code": anchor_code,
            "attempt": attempt,
            "watch_statuses": {it["code"]: it["status"] for it in list_watch_items(conn)},
            "message": ""
            if board
            else f"暂无参照公司 {anchor_code} 的完整同业筛选结果；请选择允许的参照提交扫描，或查看本次诊断。",
        }
        if board:
            out.update(board)
        if damaged:
            out["warning" if board else "error"] = (
                f"最新同业运行 ({damaged[0]}) 快照损坏，展示历史可用同业榜 "
                f"(运行: {board['run_id']}, 估值日: {board['valuation_date']})"
                if board
                else "最新同业快照损坏或无法读取"
            )
        out["showing_previous"] = bool(
            board
            and attempt
            and (not attempt.get("result") or attempt["result"]["run_id"] != board["run_id"])
        )
        if (
            board
            and attempt_run
            and (attempt_run["valuation_date"] or "") < (board["valuation_date"] or "")
        ):
            out["warning"] = "本次估值日期倒退；当前完整榜仍为较新的可用资料。"
        check_actor(actor)
        return out
    finally:
        conn.close()


def peer_anchors(actor: Actor, tracker_root: Path | None, mode: Mode) -> list[dict[str, str]]:
    """Only offer original watched anchors; demo never reads the tracker."""
    check_actor(actor)
    if mode == "demo":
        return [{"code": "600001.SH", "name": "合成参照"}]
    if tracker_root is None:
        raise ServiceError("未配置只读参照目录")
    try:
        anchors = read_watchlist(tracker_root / "a_stock_tracker" / "config.py")
    except (ScreenError, OSError) as exc:
        raise ServiceError("原关注清单不可读取") from exc
    check_actor(actor)
    return [{"code": item["ts_code"], "name": item["name"]} for item in anchors]


def _peer_target_date(tracker_root: Path | None, mode: Mode) -> str:
    if mode == "demo":
        return "2026-09-20"  # Explicit synthetic fixture date, never a live observation.
    if tracker_root is None:
        raise ServiceError("未配置交易日历")
    try:
        calendar = json.loads(
            (tracker_root / "data" / "trading_calendar.json").read_text(encoding="utf-8")
        )
        yesterday = datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=1)
        start = date.fromisoformat(calendar["covered_from"])
        end = date.fromisoformat(calendar["covered_to"])
        as_of = date.fromisoformat(calendar["as_of"])
        if (
            not str(calendar.get("source", "")).startswith("tushare.trade_cal:SSE;")
            or start > yesterday
        ):
            raise ValueError("calendar coverage is stale")
        proved_end = min(end, as_of)
        uncovered = [
            proved_end + timedelta(days=i) for i in range(1, (yesterday - proved_end).days + 1)
        ]
        if any(day.weekday() < 5 for day in uncovered):
            raise ValueError("calendar coverage is stale")
        trading_days = [date.fromisoformat(day) for day in calendar["dates"]]
        if not trading_days or any(day < start or day > proved_end for day in trading_days):
            raise ValueError("calendar dates exceed proved coverage")
        completed = [day for day in trading_days if day <= yesterday]
        if not completed:
            raise ValueError("no completed trade day")
        return max(completed).isoformat()
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise ServiceError("交易日历缺失或未覆盖昨日；旧资料仍可查看，本次不提交更新") from exc


def _job_summary(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    payload = json.loads(row["payload_json"])
    return {
        "job_id": row["job_id"],
        "kind": row["kind"],
        "anchor": payload.get("anchor"),
        "target_date": payload.get("target_date"),
        "codes": payload.get("codes", []),
        "status": row["status"],
        "phase": row["phase"],
        "requested_at": row["requested_at"],
        "updated_at": row["updated_at"],
        "finished_at": row["finished_at"],
        "error_summary": row["error_summary"],
        "result_run_id": row["result_run_id"],
    }


def request_peer_update(
    actor: Actor,
    anchor: str,
    request_id: str,
    state_dir: Path,
    mode: Mode,
    tracker_root: Path | None = None,
) -> dict[str, Any]:
    """Freeze a user-clicked peer scan. Network access belongs only to worker.py."""
    if not isinstance(anchor, str):
        check_actor(actor)
        raise ServiceError("无效参照公司")
    return _request_update(actor, anchor, request_id, state_dir, mode, tracker_root)


def request_watch_update(
    actor: Actor,
    request_id: str,
    state_dir: Path,
    mode: Mode,
    tracker_root: Path | None = None,
) -> dict[str, Any]:
    """Freeze nonpaused personal codes, never the tracker pool or a peer ranking."""
    return _request_update(actor, None, request_id, state_dir, mode, tracker_root)


def _request_update(
    actor: Actor,
    anchor: str | None,
    request_id: str,
    state_dir: Path,
    mode: Mode,
    tracker_root: Path | None,
) -> dict[str, Any]:
    check_actor(actor)
    if (
        not request_id
        or len(request_id) > 100
        or not all(c.isascii() and (c.isalnum() or c in "_-") for c in request_id)
    ):
        raise ServiceError("无效请求编号")
    kind = "watch" if anchor is None else "peer"
    intent = {"kind": kind} if kind == "watch" else {"kind": kind, "anchor": anchor}
    try:
        conn = connect_workspace(state_dir, mode)
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """SELECT * FROM update_jobs WHERE request_id=? OR EXISTS
                (SELECT 1 FROM json_each(update_jobs.request_aliases_json) WHERE value=?)""",
                (request_id, request_id),
            ).fetchone()
            if existing:
                if json.loads(existing["payload_json"]).get("intent") != intent:
                    raise ServiceError("请求编号对应不同更新意图")
                check_actor(actor)
                conn.commit()
                return _job_summary(existing)

            if kind == "watch":
                codes = [
                    row[0]
                    for row in conn.execute(
                        "SELECT code FROM watch_items WHERE status != 'paused' ORDER BY code"
                    )
                ]
                if not codes:
                    raise ServiceError("没有未暂停的关注公司，请先关注或恢复公司")
                if len(codes) > 50:
                    raise ServiceError("未暂停关注超过50家，请先暂停部分公司；不会静默截断")
                scope = {"codes": codes}
            else:
                watchlist = peer_anchors(actor, tracker_root, mode)
                if anchor not in {item["code"] for item in watchlist}:
                    raise ServiceError("参照公司不在允许范围内")
                scope = {"watchlist": watchlist}
            target_date = _peer_target_date(tracker_root, mode)
            payload: dict[str, Any] = {
                "intent": intent,
                "anchor": anchor,
                "target_date": target_date,
                "rule_id": "peer-screen-v1" if kind == "peer" else None,
                **scope,
            }
            dedupe_key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
            payload["intent_hash"] = dedupe_key
            active = conn.execute(
                "SELECT * FROM update_jobs WHERE dedupe_key=? AND status IN ('queued','running')",
                (dedupe_key,),
            ).fetchone()
            if active:
                aliases = json.loads(active["request_aliases_json"])
                aliases.append(request_id)
                conn.execute(
                    "UPDATE update_jobs SET request_aliases_json=? WHERE job_id=?",
                    (json.dumps(aliases), active["job_id"]),
                )
                check_actor(actor)
                conn.commit()
                return _job_summary(active)
            if (
                conn.execute("SELECT count(*) FROM update_jobs WHERE status='queued'").fetchone()[0]
                >= 3
            ):
                raise ServiceError("待处理任务已满，请稍后再试")
            now = utc_now()
            job_id = uuid.uuid4().hex
            conn.execute(
                """INSERT INTO update_jobs
                (job_id,request_id,kind,payload_json,dedupe_key,status,requested_at,updated_at)
                VALUES (?,?,?,?,?,'queued',?,?)""",
                (job_id, request_id, kind, json.dumps(payload), dedupe_key, now, now),
            )
            check_actor(actor)
            conn.commit()
            row = conn.execute("SELECT * FROM update_jobs WHERE job_id=?", (job_id,)).fetchone()
            return _job_summary(row)
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()
    except WorkspaceError as exc:
        raise ServiceError(str(exc)) from exc


def list_update_jobs(actor: Actor, state_dir: Path, mode: Mode) -> list[dict[str, Any]]:
    check_actor(actor)
    conn = connect_workspace(state_dir, mode)
    try:
        rows = conn.execute(
            "SELECT * FROM update_jobs WHERE phase!='dismissed' ORDER BY requested_at DESC LIMIT 20"
        ).fetchall()
        check_actor(actor)
        return [_job_summary(row) for row in rows]
    finally:
        conn.close()


def get_update_job(actor: Actor, job_id: str, state_dir: Path, mode: Mode) -> dict[str, Any]:
    check_actor(actor)
    conn = connect_workspace(state_dir, mode)
    try:
        row = conn.execute("SELECT * FROM update_jobs WHERE job_id=?", (job_id,)).fetchone()
        check_actor(actor)
        if row is None or row["phase"] == "dismissed":
            raise ServiceError("更新任务不存在或已清理")
        return _job_summary(row)
    finally:
        conn.close()
