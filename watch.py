"""Fixed-code fact collection and validation; no peer selection or ranking."""

from __future__ import annotations

import json
import math
import os
import time
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from screen import (
    ScreenError,
    call_api,
    fetch_financials,
    finite_number,
    frame_records,
    now_iso,
    risk_status,
    select_annual_roes,
)
from workspace import Mode, WorkspaceError, is_row_usable, parse_iso_utc, utc_now


def _fetch_row(client: Any, token: str, code: str, target: str) -> dict[str, Any]:
    row: dict[str, Any] = {
        "code": code,
        "name": code,
        "pb": None,
        "roe_mean": None,
        "annual_roes": [],
        "valuation_date": target,
        "valuation_source": "tushare.daily_basic",
        "financial_source": "tushare.fina_indicator",
        "risk_source": "unavailable",
        "risk_status": "unknown",
        "financial_status": "not_requested",
        "valuation_status": "not_requested",
        "exclusions": [],
    }
    try:
        shanghai_now = now_iso()
        for status in ("L", "D", "P"):
            records = frame_records(
                call_api(
                    client,
                    "stock_basic",
                    token,
                    ts_code=code,
                    list_status=status,
                    fields="ts_code,name,industry,market,exchange,list_status",
                ),
                {"ts_code", "name", "industry", "market", "exchange", "list_status"},
                "stock_basic",
            )
            if len(records) > 1 or any(r["ts_code"] != code for r in records):
                raise ScreenError("stock_basic identity mismatch")
            if records:
                row.update(
                    {
                        key: records[0][key] if isinstance(records[0][key], str) else ""
                        for key in ("name", "industry", "market", "exchange", "list_status")
                    }
                )
                row.update(
                    risk_source="tushare.stock_basic.name",
                    risk_status=risk_status(row["name"]),
                    basic_acquired_at=utc_now(),
                )
                break
        if row["risk_source"] == "unavailable":
            row["exclusions"].append("BASIC_UNAVAILABLE")
    except ScreenError:
        row["exclusions"].append("BASIC_REQUEST_FAILED")
    try:
        records = frame_records(
            call_api(
                client,
                "daily_basic",
                token,
                ts_code=code,
                trade_date=target.replace("-", ""),
                fields="ts_code,trade_date,pb,total_mv",
            ),
            {"ts_code", "trade_date", "pb", "total_mv"},
            "daily_basic",
        )
        if len(records) > 1 or any(
            r["ts_code"] != code or str(r["trade_date"]).replace("-", "") != target.replace("-", "")
            for r in records
        ):
            raise ScreenError("daily_basic identity/date mismatch")
        if records:
            if isinstance(records[0]["pb"], bool):
                raise ScreenError("invalid boolean PB")
            row.update(
                pb=finite_number(records[0]["pb"]), total_mv=finite_number(records[0]["total_mv"])
            )
        row["valuation_acquired_at"] = utc_now()
        row["valuation_status"] = "ok" if row["pb"] is not None else "missing"
    except ScreenError:
        row["valuation_status"] = "failed"
        row["exclusions"].append("VALUATION_REQUEST_FAILED")
    # No PB, ST, market, industry or old-pool eligibility gate on financial retrieval.
    try:
        records = fetch_financials(client, token, code, target, shanghai_now)
        if any(isinstance(report.get("roe_waa"), bool) for report in records):
            raise ScreenError("invalid boolean ROE")
        financial = select_annual_roes(records, target, shanghai_now)
        row.update(annual_roes=financial["annual_roes"], roe_mean=financial["roe_mean"])
        if financial["error"]:
            row["financial_status"] = (
                "conflict" if financial["error"].startswith("AMBIGUOUS_VERSION:") else "missing"
            )
            row["exclusions"].append(financial["error"])
        else:
            row["financial_status"] = "ok"
            row["financial_checked_at"] = utc_now()
    except ScreenError:
        row["financial_status"] = "failed"
        row["exclusions"].append("FINANCIAL_REQUEST_FAILED")
    if row["pb"] is None:
        row["exclusions"].append("MISSING_PB")
    row["facts_usable"] = is_row_usable(row)
    return row


def build_watch_snapshot(
    codes: list[str],
    target: str,
    mode: Mode,
    stop: Callable[[], bool],
) -> dict[str, Any]:
    rows = []
    if mode == "demo":
        fixture = json.loads(
            (Path(__file__).parent / "tests/fixtures/peer_complete_v1.json").read_text()
        )
        available = {row["code"]: row for row in fixture["rows"]}
        for code in codes:
            row = dict(
                available.get(
                    code,
                    {
                        "code": code,
                        "name": code,
                        "annual_roes": [],
                        "valuation_source": "fixture",
                        "financial_source": "fixture",
                        "risk_source": "fixture",
                        "valuation_status": "missing",
                        "financial_status": "missing",
                        "exclusions": ["SYNTHETIC_FACTS_UNAVAILABLE"],
                    },
                )
            )
            row["valuation_date"] = target
            row["facts_usable"] = is_row_usable(row)
            if row["facts_usable"]:
                row["financial_checked_at"] = utc_now()
            rows.append(row)
    else:
        import tushare as ts  # type: ignore[import-untyped]

        token = os.getenv("TUSHARE_TOKEN", "").strip()
        if not token:
            raise ScreenError("worker token unavailable")
        client = ts.pro_api(token, timeout=30)
        for index, code in enumerate(codes):
            if stop():
                raise InterruptedError("watch update stopped")
            if index:
                time.sleep(0.35)
            rows.append(_fetch_row(client, token, code, target))
    return {
        "kind": "watch",
        "anchor": None,
        "rule": None,
        "results": None,
        "source": "fixture" if mode == "demo" else "tracker",
        "is_fixture": mode == "demo",
        "screened_at": utc_now(),
        "data_date": target,
        "rows": rows,
    }


def validate_watch_snapshot(
    snap: dict[str, Any],
    payload: dict[str, Any],
    job: dict[str, Any],
    mode: Mode,
) -> Literal["complete", "partial"]:
    """Validate the final file again during recovery, before it can become a run."""
    codes = payload["codes"]
    expected_meta = {
        "job_id": job["job_id"],
        "intent_hash": payload["intent_hash"],
        "target_date": payload["target_date"],
        "anchor": None,
        "expected_codes": codes,
    }
    rows = snap.get("rows")
    if (
        snap.get("workspace_meta") != expected_meta
        or snap.get("kind") != "watch"
        or snap.get("anchor") is not None
        or snap.get("rule") is not None
        or snap.get("results") is not None
        or snap.get("data_date") != payload["target_date"]
        or snap.get("source") != ("fixture" if mode == "demo" else "tracker")
        or snap.get("is_fixture") is not (mode == "demo")
        or not isinstance(rows, list)
        or len(rows) != len(codes)
        or not all(isinstance(r, dict) and isinstance(r.get("code"), str) for r in rows)
        or sorted(r["code"] for r in rows) != codes
    ):
        raise WorkspaceError("watch snapshot does not match frozen intent or scope")
    captured = parse_iso_utc(str(snap.get("screened_at", "")))
    captured_shanghai_date = (
        datetime.fromisoformat(captured).astimezone(ZoneInfo("Asia/Shanghai")).date()
    )
    requested = parse_iso_utc(job["requested_at"])
    if not requested <= captured <= utc_now():
        raise WorkspaceError("watch snapshot observation time outside job lifetime")
    usable_count = 0
    for row in rows:
        if mode == "production" and (
            any(
                row.get(k) == "fixture"
                for k in ("valuation_source", "financial_source", "risk_source")
            )
            or any(
                isinstance(a, dict) and a.get("source") == "fixture"
                for a in (row.get("annual_roes") or [])
            )
        ):
            raise WorkspaceError("production workspace rejects fixture data")
        usable = is_row_usable(row)
        if row.get("facts_usable") is not usable or (
            usable and row.get("financial_status") == "failed"
        ):
            raise WorkspaceError("watch fact usability mismatch")
        if usable:
            checked = parse_iso_utc(str(row.get("financial_checked_at", "")))
            if (
                row.get("valuation_date") != payload["target_date"]
                or not requested <= checked <= captured
            ):
                raise WorkspaceError("watch facts not checked for this request")
            raw = [
                dict(
                    a,
                    end_date=a.get("period") or a.get("end_date") or f"{a['year']}-12-31",
                    roe_waa=a.get("roe_waa", a.get("roe")),
                )
                for a in row["annual_roes"]
            ]
            selection = select_annual_roes(
                raw,
                payload["target_date"],
                datetime.fromisoformat(captured).astimezone(ZoneInfo("Asia/Shanghai")).isoformat(),
            )
            if mode == "demo":
                # Existing synthetic annual rows intentionally omit provider version flags.
                selection = {
                    "error": None,
                    "annual_roes": raw,
                    "roe_mean": sum(a["roe_waa"] for a in raw) / 3,
                }
            if selection["error"] or not math.isclose(
                row["roe_mean"], selection["roe_mean"], abs_tol=0.01 if mode == "demo" else 1e-9
            ):
                raise WorkspaceError("watch annual facts invalid")
            if mode == "production":
                if (
                    row.get("valuation_source") != "tushare.daily_basic"
                    or row.get("financial_source") != "tushare.fina_indicator"
                    or row.get("risk_source") != "tushare.stock_basic.name"
                ):
                    raise WorkspaceError("watch facts must be freshly fetched")
                for acquired in [
                    row.get("basic_acquired_at"),
                    row.get("valuation_acquired_at"),
                    *(a.get("acquired_at") for a in raw),
                ]:
                    if not acquired:
                        raise WorkspaceError("watch acquisition time missing")
                    # Provider acquisition timestamps have second precision.
                    if not requested[:19] <= parse_iso_utc(str(acquired))[:19] <= captured[:19]:
                        raise WorkspaceError("watch acquisition time outside job lifetime")
            if any(
                date.fromisoformat(a["ann_date"]) > captured_shanghai_date
                for a in selection["annual_roes"]
            ):
                raise WorkspaceError("watch annual announcement from future")
            usable_count += 1
    return "complete" if usable_count == len(codes) else "partial"
