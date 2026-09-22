#!/usr/bin/env python3
"""Small, read-only peer screen for personal A-share research."""

from __future__ import annotations

import argparse
import ast
import json
import math
import os
import re
import sqlite3
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

RULE = "peer-screen-v1"
SCHEMA_VERSION = 1
CAP = 50
TOP_N = 3
MAX_REPORT_AGE_DAYS = 550
SHANGHAI = ZoneInfo("Asia/Shanghai")
FINANCIAL_INDUSTRY_WORDS = (
    "银行",
    "证券",
    "保险",
    "多元金融",
    "金融服务",
    "信托",
    "期货",
)


class ScreenError(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


def parse_date(value: Any) -> date:
    text = str(value or "").strip().replace("-", "")
    try:
        return datetime.strptime(text, "%Y%m%d").date()
    except ValueError as exc:
        raise ScreenError(f"无效日期: {value!r}") from exc


def iso_date(value: Any) -> str:
    return parse_date(value).isoformat()


def finite_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def normalize_flag(value: Any) -> str:
    number = finite_number(value)
    if number is not None and number.is_integer():
        return str(int(number))
    return str(value or "").strip()


def normalize_code(value: str) -> str:
    text = value.strip().upper()
    match = re.fullmatch(r"(\d{6})(?:\.(SH|SZ|BJ))?", text)
    if not match:
        raise ScreenError(f"无效股票代码: {value!r}")
    base, suffix = match.groups()
    suffix = suffix or ("SH" if base.startswith(("5", "6", "9")) else "SZ")
    return f"{base}.{suffix}"


def base_code(value: str) -> str:
    return normalize_code(value).split(".", 1)[0]


def read_watchlist(config_path: Path) -> list[dict[str, str]]:
    try:
        tree = ast.parse(
            config_path.read_text(encoding="utf-8"), filename=str(config_path)
        )
    except (OSError, SyntaxError) as exc:
        raise ScreenError(f"无法读取 WATCHLIST: {exc}") from exc
    value_node: ast.expr | None = None
    for node in tree.body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "WATCHLIST"
        ):
            value_node = node.value
            break
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "WATCHLIST" for t in node.targets
        ):
            value_node = node.value
            break
    if value_node is None:
        raise ScreenError(f"{config_path} 中没有 WATCHLIST 字面量")
    try:
        raw = ast.literal_eval(value_node)
    except (ValueError, SyntaxError) as exc:
        raise ScreenError("WATCHLIST 不是可安全解析的字面量") from exc
    if not isinstance(raw, list):
        raise ScreenError("WATCHLIST 必须是列表")
    result: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("code"), str):
            raise ScreenError("WATCHLIST 条目缺少字符串 code")
        result.append(
            {"code": base_code(item["code"]), "name": str(item.get("name") or "")}
        )
    return result


def read_cached_reference(db_path: Path, code: str) -> dict[str, Any] | None:
    if not db_path.is_file():
        return None
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True) as conn:
            row = conn.execute(
                "SELECT name, industry, updated_at FROM stock_fundamentals WHERE code=?",
                (base_code(code),),
            ).fetchone()
    except sqlite3.Error as exc:
        raise ScreenError(f"旧缓存只读失败: {exc}") from exc
    return {"name": row[0], "industry": row[1], "updated_at": row[2]} if row else None


def load_reference(tracker_root: Path, anchor: str) -> dict[str, Any]:
    tracker_root = tracker_root.expanduser().resolve()
    watchlist = read_watchlist(tracker_root / "a_stock_tracker" / "config.py")
    anchor_code = normalize_code(anchor)
    watchlist_codes = [item["code"] for item in watchlist]
    if base_code(anchor_code) not in watchlist_codes:
        raise ScreenError(f"参照公司 {anchor_code} 不在原关注集合中")
    return {
        "anchor": anchor_code,
        "watchlist": watchlist,
        "watchlist_codes": watchlist_codes,
        "legacy_reference": read_cached_reference(
            tracker_root / "tracker.db", anchor_code
        ),
    }


def read_token(tracker_root: Path) -> str:
    token = os.environ.get("TUSHARE_TOKEN", "").strip()
    env_path = tracker_root / ".env"
    if not token and env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.split("#", 1)[0].strip()
            if "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            if key.strip() == "TUSHARE_TOKEN":
                token = value.strip().strip('"').strip("'")
                break
    if not token:
        raise ScreenError("TUSHARE_TOKEN 未配置")
    return token


def frame_records(
    frame: Any, required: set[str], endpoint: str
) -> list[dict[str, Any]]:
    columns = set(getattr(frame, "columns", []))
    missing = required - columns
    if missing:
        raise ScreenError(f"{endpoint} 缺少字段: {', '.join(sorted(missing))}")
    return json.loads(
        frame.to_json(orient="records", force_ascii=False, date_format="iso")
    )


def call_api(client: Any, endpoint: str, token: str, **params: Any) -> Any:
    try:
        return getattr(client, endpoint)(**params)
    except Exception as exc:
        message = str(exc).replace(token, "[REDACTED]")
        raise ScreenError(f"{endpoint} 请求失败: {message}") from exc


def fetch_universe(
    client: Any, token: str, data_date: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, str]]:
    basic_frame = call_api(
        client,
        "stock_basic",
        token,
        exchange="",
        list_status="L",
        fields="ts_code,symbol,name,industry,market,exchange,list_status",
    )
    basic = frame_records(
        basic_frame,
        {"ts_code", "name", "industry", "market", "exchange", "list_status"},
        "stock_basic",
    )
    stock_basic_acquired_at = now_iso()
    if not basic or len(basic) >= 6000:
        raise ScreenError("stock_basic 为空或疑似达到 6000 行截断上限")
    if len({str(row["ts_code"]) for row in basic}) != len(basic):
        raise ScreenError("stock_basic 含重复证券代码")

    valuation_frame = call_api(
        client,
        "daily_basic",
        token,
        trade_date=data_date.replace("-", ""),
        fields="ts_code,trade_date,pb,total_mv",
    )
    valuation = frame_records(
        valuation_frame, {"ts_code", "trade_date", "pb", "total_mv"}, "daily_basic"
    )
    daily_basic_acquired_at = now_iso()
    if not valuation or len(valuation) >= 6000:
        raise ScreenError("daily_basic 为空或疑似达到 6000 行截断上限")
    dates = {iso_date(row["trade_date"]) for row in valuation}
    if dates != {data_date}:
        raise ScreenError(f"daily_basic 返回了非目标日期: {sorted(dates)}")
    return (
        basic,
        valuation,
        {
            "stock_basic_acquired_at": stock_basic_acquired_at,
            "daily_basic_acquired_at": daily_basic_acquired_at,
        },
    )


def risk_status(name: Any) -> str:
    normalized = str(name or "").strip().upper()
    if not normalized:
        return "unknown"
    if normalized.startswith(("ST", "*ST")):
        return "known_warning"
    return "name_check_clear_other_risks_unknown"


def is_financial_industry(industry: str) -> bool:
    return any(word in industry for word in FINANCIAL_INDUSTRY_WORDS)


def load_peers(
    stock_rows: list[dict[str, Any]],
    valuation_rows: list[dict[str, Any]],
    anchor: str,
    watchlist_codes: list[str],
    cap: int = CAP,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, dict[str, Any]]]:
    stocks = {normalize_code(str(row["ts_code"])): dict(row) for row in stock_rows}
    anchor = normalize_code(anchor)
    reference = stocks.get(anchor)
    if reference is None:
        raise ScreenError(f"stock_basic 中没有参照公司 {anchor}")
    if (
        reference.get("list_status") != "L"
        or reference.get("exchange") not in {"SSE", "SZSE"}
        or reference.get("market") != "主板"
    ):
        raise ScreenError("参照公司不是当前沪深主板上市公司")
    industry = str(reference.get("industry") or "").strip()
    if not industry:
        raise ScreenError("参照公司行业不明")
    if is_financial_industry(industry):
        raise ScreenError(f"金融行业不适用 peer-screen-v1: {industry}")

    valuations = {
        normalize_code(str(row["ts_code"])): dict(row) for row in valuation_rows
    }
    peers = [
        row
        for code, row in stocks.items()
        if str(row.get("industry") or "").strip() == industry
        and row.get("list_status") == "L"
        and row.get("exchange") in {"SSE", "SZSE"}
        and row.get("market") == "主板"
    ]
    if reference not in peers:
        raise ScreenError("参照公司未进入同业枚举")

    eligible_others: list[tuple[float, str, dict[str, Any]]] = []
    missing_valuation: list[str] = []
    for row in peers:
        code = normalize_code(str(row["ts_code"]))
        if code == anchor:
            continue
        total_mv = finite_number(valuations.get(code, {}).get("total_mv"))
        if total_mv is None or total_mv <= 0:
            missing_valuation.append(code)
        else:
            eligible_others.append((total_mv, code, row))
    eligible_others.sort(key=lambda item: (-item[0], item[1]))
    selected = [reference, *(item[2] for item in eligible_others[: max(0, cap - 1)])]
    selected_codes = [normalize_code(str(row["ts_code"])) for row in selected]
    scope = {
        "industry": industry,
        "industry_source": "tushare.stock_basic",
        "enumerated_count": len(peers),
        "enumerated_codes": sorted(
            normalize_code(str(row["ts_code"])) for row in peers
        ),
        "selected_codes": selected_codes,
        "excluded_missing_valuation": sorted(missing_valuation),
        "excluded_by_cap": [item[1] for item in eligible_others[max(0, cap - 1) :]],
        "cap": cap,
        "market_bias": "同日总市值降序，偏向较大公司",
        "watchlist_codes": sorted(watchlist_codes),
    }
    return selected, scope, valuations


def read_local_financials(db_path: Path, code: str) -> list[dict[str, Any]]:
    if not db_path.is_file():
        return []
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    query = """
        SELECT f.payload_json, f.source, MAX(e.observed_at)
        FROM financial_observations AS f
        LEFT JOIN observation_events AS e ON e.record_key = f.record_key
        WHERE f.endpoint='fina_indicator' AND f.code=?
        GROUP BY f.record_key, f.payload_json, f.source
    """
    try:
        with sqlite3.connect(uri, uri=True) as conn:
            raw = conn.execute(query, (base_code(code),)).fetchall()
    except sqlite3.Error as exc:
        raise ScreenError(f"财务原始库只读失败: {exc}") from exc
    rows: list[dict[str, Any]] = []
    for payload, source, observed_at in raw:
        try:
            row = json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            continue
        row["source"] = source
        row["acquired_at"] = observed_at
        rows.append(row)
    return rows


def select_annual_roes(
    records: list[dict[str, Any]], data_date: str, screened_at: str
) -> dict[str, Any]:
    data_day = parse_date(data_date)
    screened_day = parse_date(screened_at[:10])
    deduped: dict[str, dict[str, Any]] = {}
    for raw in records:
        try:
            period = iso_date(raw.get("end_date"))
            announced = iso_date(raw.get("ann_date"))
        except ScreenError:
            continue
        if (
            not period.endswith("-12-31")
            or parse_date(period) > data_day
            or parse_date(announced) > screened_day
        ):
            continue
        row = {
            "period": period,
            "ann_date": announced,
            "roe_waa": finite_number(raw.get("roe_waa")),
            "update_flag": normalize_flag(raw.get("update_flag")),
            "source": str(raw.get("source") or ""),
            "acquired_at": raw.get("acquired_at"),
        }
        key = json.dumps(
            {name: value for name, value in row.items() if name != "acquired_at"},
            ensure_ascii=False,
            sort_keys=True,
        )
        previous = deduped.get(key)
        if previous is None or str(row.get("acquired_at") or "") > str(
            previous.get("acquired_at") or ""
        ):
            deduped[key] = row

    groups: dict[str, list[dict[str, Any]]] = {}
    for row in deduped.values():
        groups.setdefault(row["period"], []).append(row)
    periods = sorted(groups, reverse=True)[:3]
    if len(periods) < 3:
        return {
            "annual_roes": [],
            "roe_mean": None,
            "error": "MISSING_THREE_ANNUAL_REPORTS",
        }
    years = [int(period[:4]) for period in periods]
    if years != [years[0], years[0] - 1, years[0] - 2]:
        return {
            "annual_roes": [],
            "roe_mean": None,
            "error": "NON_CONSECUTIVE_ANNUAL_REPORTS",
        }
    if (data_day - parse_date(periods[0])).days > MAX_REPORT_AGE_DAYS:
        return {
            "annual_roes": [],
            "roe_mean": None,
            "error": "LATEST_ANNUAL_REPORT_TOO_OLD",
        }

    selected: list[dict[str, Any]] = []
    for period in periods:
        rows = groups[period]
        revisions = [row for row in rows if row["update_flag"] == "1"]
        if len(revisions) == 1:
            chosen = revisions[0]
            basis = "unique_update_flag_1"
        elif not revisions and len(rows) == 1 and rows[0]["update_flag"] == "0":
            chosen = rows[0]
            basis = "single_original_update_flag_0"
        else:
            return {
                "annual_roes": [],
                "roe_mean": None,
                "error": f"AMBIGUOUS_VERSION:{period}",
            }
        if chosen["roe_waa"] is None:
            return {
                "annual_roes": [],
                "roe_mean": None,
                "error": f"INVALID_ROE:{period}",
            }
        selected.append({**chosen, "selection_basis": basis})
    selected.sort(key=lambda row: row["period"])
    return {
        "annual_roes": selected,
        "roe_mean": sum(float(row["roe_waa"]) for row in selected) / 3,
        "error": None,
    }


def fetch_financials(
    client: Any, token: str, code: str, data_date: str, screened_at: str
) -> list[dict[str, Any]]:
    end = parse_date(screened_at[:10])
    start = date(max(1990, parse_date(data_date).year - 6), 1, 1)
    frame = call_api(
        client,
        "fina_indicator",
        token,
        ts_code=normalize_code(code),
        start_date=start.strftime("%Y%m%d"),
        end_date=end.strftime("%Y%m%d"),
        fields="ts_code,ann_date,end_date,update_flag,roe_waa",
    )
    records = frame_records(
        frame,
        {"ts_code", "ann_date", "end_date", "update_flag", "roe_waa"},
        "fina_indicator",
    )
    if len(records) >= 100:
        raise ScreenError(f"{code} fina_indicator 疑似达到 100 行截断上限")
    acquired_at = now_iso()
    for row in records:
        row["source"] = "tushare.fina_indicator"
        row["acquired_at"] = acquired_at
    return records


def load_inputs(
    selected: list[dict[str, Any]],
    valuations: dict[str, dict[str, Any]],
    reference: dict[str, Any],
    tracker_root: Path,
    data_date: str,
    client: Any,
    token: str,
    source_times: dict[str, str],
) -> tuple[list[dict[str, Any]], str]:
    rows: list[dict[str, Any]] = []
    screened_at = now_iso()
    watchlist_codes = set(reference["watchlist_codes"])
    raw_db = tracker_root / "data" / "tushare-primary.db"
    for index, basic in enumerate(selected):
        code = normalize_code(str(basic["ts_code"]))
        valuation = valuations.get(code, {})
        pb = finite_number(valuation.get("pb"))
        total_mv = finite_number(valuation.get("total_mv"))
        status = risk_status(basic.get("name"))
        exclusions: list[str] = []
        if status == "known_warning":
            exclusions.append("KNOWN_ST_WARNING")
        if pb is None or pb <= 0:
            exclusions.append("INVALID_PB")

        financial: dict[str, Any] = {
            "annual_roes": [],
            "roe_mean": None,
            "error": "NOT_FETCHED",
        }
        financial_source = "none"
        if not exclusions:
            local_records = read_local_financials(raw_db, code)
            local_selection = select_annual_roes(local_records, data_date, screened_at)
            if local_selection["error"] is None:
                financial = local_selection
                financial_source = "tracker:data/tushare-primary.db"
            else:
                if index:
                    time.sleep(0.35)
                try:
                    online_records = fetch_financials(
                        client, token, code, data_date, screened_at
                    )
                    financial = select_annual_roes(
                        online_records, data_date, screened_at
                    )
                    financial_source = "tushare.fina_indicator"
                except ScreenError as exc:
                    financial = {"annual_roes": [], "roe_mean": None, "error": str(exc)}
                    financial_source = "tushare.fina_indicator:error"
        if financial["error"]:
            exclusions.append(str(financial["error"]))
        roe_mean = finite_number(financial.get("roe_mean"))
        if roe_mean is not None and roe_mean <= 0:
            exclusions.append("NON_POSITIVE_ROE_MEAN")
        rows.append(
            {
                "code": code,
                "name": str(basic.get("name") or ""),
                "industry": str(basic.get("industry") or ""),
                "market": basic.get("market"),
                "exchange": basic.get("exchange"),
                "list_status": basic.get("list_status"),
                "in_watchlist": base_code(code) in watchlist_codes,
                "pb": pb,
                "total_mv": total_mv,
                "valuation_date": data_date,
                "basic_source": "tushare.stock_basic",
                "basic_acquired_at": source_times["stock_basic_acquired_at"],
                "valuation_source": "tushare.daily_basic",
                "valuation_acquired_at": source_times["daily_basic_acquired_at"],
                "annual_roes": financial["annual_roes"],
                "roe_mean": roe_mean,
                "financial_source": financial_source,
                "risk_status": status,
                "risk_source": "tushare.stock_basic.name",
                "exclusions": sorted(set(exclusions)),
            }
        )
    return rows, screened_at


def average_ranks(
    rows: list[dict[str, Any]], field: str, reverse: bool
) -> dict[str, float]:
    ordered = sorted(rows, key=lambda row: float(row[field]), reverse=reverse)
    ranks: dict[str, float] = {}
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][field] == ordered[index][field]:
            end += 1
        rank = ((index + 1) + end) / 2
        for row in ordered[index:end]:
            ranks[row["code"]] = rank
        index = end
    return ranks


def rank_peers(
    rows: list[dict[str, Any]], anchor: str, watchlist_codes: list[str]
) -> dict[str, Any]:
    qualified = [
        row
        for row in rows
        if not row.get("exclusions")
        and finite_number(row.get("pb")) is not None
        and finite_number(row.get("roe_mean")) is not None
        and float(row["pb"]) > 0
        and float(row["roe_mean"]) > 0
        and len(row.get("annual_roes", [])) == 3
    ]
    roe_ranks = average_ranks(qualified, "roe_mean", reverse=True)
    pb_ranks = average_ranks(qualified, "pb", reverse=False)
    ranking = [
        {
            "code": row["code"],
            "roe_rank": roe_ranks[row["code"]],
            "pb_rank": pb_ranks[row["code"]],
            "research_order": (roe_ranks[row["code"]] + pb_ranks[row["code"]]) / 2,
        }
        for row in qualified
    ]
    ranking.sort(key=lambda item: (item["research_order"], item["code"]))
    for position, item in enumerate(ranking, 1):
        item["position"] = position
    anchor = normalize_code(anchor)
    anchor_item = next((item for item in ranking if item["code"] == anchor), None)
    watchlist = set(watchlist_codes)
    outside_qualified = sum(
        base_code(item["code"]) not in watchlist for item in ranking
    )
    return {
        "qualified_codes": [item["code"] for item in ranking],
        "ranking": ranking,
        "top": [item["code"] for item in ranking[:TOP_N]],
        "anchor_position": anchor_item["position"] if anchor_item else None,
        "outside_watchlist_qualified_count": outside_qualified,
        "discovery_complete": outside_qualified > 0,
    }


def default_data_date(tracker_root: Path) -> str:
    path = tracker_root / "data" / "trading_calendar.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        dates = [iso_date(value) for value in payload["dates"]]
    except (OSError, KeyError, TypeError, json.JSONDecodeError, ScreenError) as exc:
        raise ScreenError("无法确认最近已完成交易日，请显式传 --date") from exc
    today = datetime.now(SHANGHAI).date()
    completed = [value for value in dates if parse_date(value) <= today]
    if not completed:
        raise ScreenError("本地交易日历没有已完成交易日，请显式传 --date")
    return max(completed)


def build_live_snapshot(
    tracker_root: Path, anchor: str, requested_date: str | None
) -> dict[str, Any]:
    tracker_root = tracker_root.expanduser().resolve()
    reference = load_reference(tracker_root, anchor)
    data_date = (
        iso_date(requested_date) if requested_date else default_data_date(tracker_root)
    )
    token = read_token(tracker_root)
    try:
        import tushare as ts
    except ImportError as exc:
        raise ScreenError("当前 Python 环境缺少 tushare") from exc
    client = ts.pro_api(token, timeout=30)
    stock_rows, valuation_rows, source_times = fetch_universe(client, token, data_date)
    selected, scope, valuations = load_peers(
        stock_rows, valuation_rows, reference["anchor"], reference["watchlist_codes"]
    )
    rows, screened_at = load_inputs(
        selected,
        valuations,
        reference,
        tracker_root,
        data_date,
        client,
        token,
        source_times,
    )
    results = rank_peers(rows, reference["anchor"], reference["watchlist_codes"])
    generated_at = now_iso()
    scope.update(source_times)
    scope["valuation_date"] = data_date
    return {
        "schema_version": SCHEMA_VERSION,
        "rule": RULE,
        "limits": {
            "cap": CAP,
            "top_n": TOP_N,
            "max_report_age_days": MAX_REPORT_AGE_DAYS,
        },
        "formula": "research_order=(roe_rank_desc+pb_rank_asc)/2; average ties",
        "screened_at": screened_at,
        "generated_at": generated_at,
        "data_date": data_date,
        "anchor": reference["anchor"],
        "watchlist_codes": reference["watchlist_codes"],
        "legacy_reference": reference["legacy_reference"],
        "scope": scope,
        "rows": rows,
        "results": results,
    }


def fmt_number(value: Any, digits: int = 2) -> str:
    number = finite_number(value)
    return "—" if number is None else f"{number:.{digits}f}"


def annual_entries(row: dict[str, Any]) -> list[dict[str, Any]]:
    value = row.get("annual_roes")
    return (
        [item for item in value if isinstance(item, dict)]
        if isinstance(value, list)
        else []
    )


def annual_signature(row: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(
        [
            {
                "period": item.get("period"),
                "roe_waa": finite_number(item.get("roe_waa")),
                "ann_date": item.get("ann_date"),
                "update_flag": normalize_flag(item.get("update_flag")),
            }
            for item in annual_entries(row)
        ],
        key=lambda item: str(item["period"]),
    )


def indexed_rows(
    snapshot: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    raw_rows = snapshot.get("rows")
    if not isinstance(raw_rows, list):
        return {}, ["rows 不是列表"]
    rows: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    for index, row in enumerate(raw_rows):
        if not isinstance(row, dict) or not isinstance(row.get("code"), str):
            warnings.append(f"rows[{index}] 缺少字符串 code")
            continue
        code = row["code"]
        if code in rows:
            warnings.append(f"rows 含重复代码 {code}")
            continue
        rows[code] = row
    return rows, warnings


def missing_signature(row: dict[str, Any]) -> set[str]:
    reasons = set(row.get("exclusions", []))
    if finite_number(row.get("pb")) is None:
        reasons.add("PB_MISSING")
    if not annual_signature(row):
        reasons.add("ANNUAL_DATA_MISSING")
    return reasons


def compare_snapshots(
    previous: dict[str, Any], current: dict[str, Any], previous_path: Path
) -> dict[str, Any]:
    incompatibilities: list[str] = []
    for key in ("anchor", "rule", "formula", "limits"):
        if previous.get(key) != current.get(key):
            incompatibilities.append(key)
    for key in ("industry", "industry_source", "cap", "market_bias"):
        if previous.get("scope", {}).get(key) != current.get("scope", {}).get(key):
            incompatibilities.append(f"scope.{key}")

    previous_rows, previous_row_warnings = indexed_rows(previous)
    current_rows, current_row_warnings = indexed_rows(current)
    if previous_row_warnings:
        incompatibilities.append("previous.rows")
    if current_row_warnings:
        incompatibilities.append("current.rows")
    previous_codes = set(previous_rows)
    current_codes = set(current_rows)

    def identities(
        codes: set[str], rows: dict[str, dict[str, Any]]
    ) -> list[dict[str, str]]:
        return [
            {"code": code, "name": str(rows.get(code, {}).get("name") or "")}
            for code in sorted(codes)
        ]

    scope_entries = identities(current_codes - previous_codes, current_rows)
    scope_exits = identities(previous_codes - current_codes, previous_rows)
    compatible = not incompatibilities
    previous_top = set(previous.get("results", {}).get("top", []))
    current_top = set(current.get("results", {}).get("top", []))
    top_entries = (
        identities(current_top - previous_top, current_rows) if compatible else []
    )
    top_exits = (
        identities(previous_top - current_top, previous_rows) if compatible else []
    )

    pb_changes: list[dict[str, Any]] = []
    annual_changes: list[dict[str, Any]] = []
    newly_missing: list[dict[str, Any]] = []
    resolved_missing: list[dict[str, Any]] = []
    for code in sorted(previous_codes & current_codes):
        before = previous_rows[code]
        after = current_rows[code]
        previous_pb = finite_number(before.get("pb"))
        current_pb = finite_number(after.get("pb"))
        if (
            previous_pb is not None
            and current_pb is not None
            and previous_pb != current_pb
        ):
            pb_changes.append(
                {
                    "code": code,
                    "name": str(after.get("name") or before.get("name") or ""),
                    "before": previous_pb,
                    "after": current_pb,
                    "delta": current_pb - previous_pb,
                }
            )
        previous_annual = annual_signature(before)
        current_annual = annual_signature(after)
        if previous_annual and current_annual and previous_annual != current_annual:
            annual_changes.append(
                {
                    "code": code,
                    "name": str(after.get("name") or before.get("name") or ""),
                    "before": previous_annual,
                    "after": current_annual,
                }
            )

        previous_missing = missing_signature(before)
        current_missing = missing_signature(after)
        for target, reasons in (
            (newly_missing, current_missing - previous_missing),
            (resolved_missing, previous_missing - current_missing),
        ):
            if reasons:
                target.append(
                    {
                        "code": code,
                        "name": str(after.get("name") or before.get("name") or ""),
                        "reasons": sorted(reasons),
                    }
                )

    has_changes = any(
        (
            incompatibilities,
            scope_entries,
            scope_exits,
            top_entries,
            top_exits,
            pb_changes,
            annual_changes,
            newly_missing,
            resolved_missing,
        )
    )
    return {
        "previous_path": str(previous_path.expanduser().resolve()),
        "previous_screened_at": previous.get("screened_at"),
        "previous_data_date": previous.get("data_date"),
        "compatible_rank": compatible,
        "incompatibilities": incompatibilities,
        "scope_entries": scope_entries,
        "scope_exits": scope_exits,
        "top_entries": top_entries,
        "top_exits": top_exits,
        "pb_changes": pb_changes,
        "annual_changes": annual_changes,
        "newly_missing": newly_missing,
        "resolved_missing": resolved_missing,
        "member_change_may_affect_rank": bool(scope_entries or scope_exits),
        "has_changes": has_changes,
    }


def comparison_sections(snapshot: dict[str, Any]) -> list[str]:
    comparison = snapshot.get("comparison")
    if not comparison:
        return []
    lines = ["## 与指定旧快照相比", ""]
    lines.append(
        f"- 对照数据日：{comparison.get('previous_data_date', '—')}；本次数据日：{snapshot.get('data_date', '—')}。"
    )
    if comparison.get("incompatibilities"):
        lines.append(
            "- 规则/范围不兼容，仅展示原始事实变化，不比较排名："
            + ", ".join(comparison["incompatibilities"])
            + "。"
        )
    if not comparison.get("has_changes"):
        lines.append("- 同一输入未发现新变化。")
        lines.append("")
        return lines

    for label, key in (("新进入前三", "top_entries"), ("退出前三", "top_exits")):
        values = comparison.get(key, [])
        if values:
            lines.append(
                f"- {label}："
                + ", ".join(
                    f"{item['name'] or '—'} `{item['code']}`" for item in values
                )
                + "。"
            )
    if comparison.get("member_change_may_affect_rank"):
        entered = (
            ", ".join(item["code"] for item in comparison.get("scope_entries", []))
            or "无"
        )
        exited = (
            ", ".join(item["code"] for item in comparison.get("scope_exits", []))
            or "无"
        )
        lines.append(
            f"- 同业成员范围变化：进入 {entered}；退出 {exited}。范围变化可能影响排名，不代表公司经营恶化。"
        )
    for item in comparison.get("pb_changes", []):
        lines.append(
            f"- PB 变化：{item['name'] or '—'} `{item['code']}` {item['before']:.4g} → {item['after']:.4g}"
            f"（{item['delta']:+.4g}）；这里只记录数值，不归因于股价或基本面。"
        )
    for item in comparison.get("annual_changes", []):
        before = ", ".join(
            f"{value['period']}:{fmt_number(value['roe_waa'])}%"
            for value in item["before"]
        )
        after = ", ".join(
            f"{value['period']}:{fmt_number(value['roe_waa'])}%"
            for value in item["after"]
        )
        lines.append(
            f"- 年报/ROE 变化：{item['name'] or '—'} `{item['code']}` [{before}] → [{after}]。"
        )
    for label, key in (
        ("新增数据缺失", "newly_missing"),
        ("数据缺失已恢复", "resolved_missing"),
    ):
        for item in comparison.get(key, []):
            lines.append(
                f"- {label}：{item['name'] or '—'} `{item['code']}`（{', '.join(item['reasons'])}）；不写成公司变差。"
            )
    lines.append("")
    return lines


def candidate_review_sections(snapshot: dict[str, Any]) -> list[str]:
    rows, _ = indexed_rows(snapshot)
    results = snapshot.get("results")
    results = results if isinstance(results, dict) else {}
    ranking = results.get("ranking")
    ranks = (
        {
            item["code"]: item
            for item in ranking
            if isinstance(item, dict) and isinstance(item.get("code"), str)
        }
        if isinstance(ranking, list)
        else {}
    )
    anchor_code = snapshot.get("anchor")
    anchor = rows.get(anchor_code) if isinstance(anchor_code, str) else None
    anchor_rank = ranks.get(anchor_code) if isinstance(anchor_code, str) else None
    top = results.get("top")
    lines = ["", "## 候选审查", ""]
    for code in top if isinstance(top, list) else []:
        row = rows.get(code)
        rank = ranks.get(code)
        if row is None or rank is None:
            lines.append(
                f"- 快照中的候选 `{code}` 缺少对应明细或排名，无法生成审查说明。"
            )
            continue
        lines.append(f"### {row.get('name') or '—'} `{code}`")
        lines.append("")
        lines.append(
            f"- 排序原因：三年 ROE 均值 {fmt_number(row.get('roe_mean'))}%（第 {fmt_number(rank.get('roe_rank'), 1)} 名），"
            f"PB {fmt_number(row.get('pb'))}（第 {fmt_number(rank.get('pb_rank'), 1)} 名），综合研究次序第 {rank.get('position')}。"
        )
        row_roe = finite_number(row.get("roe_mean"))
        row_pb = finite_number(row.get("pb"))
        anchor_roe = finite_number(anchor.get("roe_mean")) if anchor else None
        anchor_pb = finite_number(anchor.get("pb")) if anchor else None
        if (
            anchor is not None
            and anchor_rank is not None
            and row_roe is not None
            and row_pb is not None
            and anchor_roe is not None
            and anchor_pb is not None
        ):
            row_years = [
                str(item.get("period") or "")[:4] for item in annual_entries(row)
            ]
            anchor_years = [
                str(item.get("period") or "")[:4] for item in annual_entries(anchor)
            ]
            comparison = (
                f"相对参照公司，ROE 均值差 {row_roe - anchor_roe:+.2f} 个百分点，"
                f"PB 差 {row_pb - anchor_pb:+.2f} 倍（候选减参照）"
            )
            if row_years != anchor_years:
                comparison += f"；年报覆盖不同（候选 {row_years}，参照 {anchor_years}）"
            lines.append(f"- 与参照比较：{comparison}。")
        else:
            lines.append("- 与参照比较：参照或候选数据不完整，不计算差值。")

        annual = sorted(
            annual_entries(row), key=lambda item: str(item.get("period") or "")
        )
        values = [finite_number(item.get("roe_waa")) for item in annual]
        facts: list[str] = []
        if len(annual) != 3 or any(value is None for value in values):
            facts.append("逐年 ROE 数据不足，不能核验负值或最新年度下降")
        else:
            numeric = [float(value) for value in values if value is not None]
            if any(value < 0 for value in numeric):
                facts.append("三年中存在负 ROE")
            if numeric[-1] < numeric[-2]:
                facts.append(
                    f"最新年度 ROE 从 {fmt_number(numeric[-2])}% 降至 {fmt_number(numeric[-1])}%"
                )
        lines.append(
            "- 已知事实："
            + (
                "；".join(facts)
                if facts
                else "本次指定字段未触发负 ROE 或最新年度下降提示"
            )
            + "。"
        )
        lines.append(
            "- 待核查：主营业务可比吗？高 ROE 是否依赖杠杆或一次性收益？低 PB 是否反映资产质量问题？风险警示、停牌与可交易性如何？"
        )
        lines.append("")
    return lines


def render_report(snapshot: dict[str, Any]) -> str:
    rows, row_warnings = indexed_rows(snapshot)
    results = snapshot.get("results")
    results = results if isinstance(results, dict) else {}
    raw_ranking = results.get("ranking")
    ranking = (
        [item for item in raw_ranking if isinstance(item, dict)]
        if isinstance(raw_ranking, list)
        else []
    )
    rank_by_code = {
        item["code"]: item for item in ranking if isinstance(item.get("code"), str)
    }
    raw_top = results.get("top")
    top = (
        [code for code in raw_top if isinstance(code, str)]
        if isinstance(raw_top, list)
        else []
    )
    anchor = snapshot.get("anchor")
    shown = top + ([anchor] if isinstance(anchor, str) and anchor not in top else [])
    raw_scope = snapshot.get("scope")
    scope = raw_scope if isinstance(raw_scope, dict) else {}
    qualified_count = len(ranking)
    selected_count = len(rows)
    financial_sources = (
        ", ".join(
            sorted(
                {str(row.get("financial_source") or "none") for row in rows.values()}
            )
        )
        or "none"
    )
    annual_coverages = sorted(
        {
            "/".join(
                str(item.get("period") or "")[:4]
                for item in annual_entries(row)
                if item.get("period")
            )
            for row in rows.values()
            if annual_entries(row)
        }
    )
    warnings = [
        *(
            snapshot.get("replay_warnings", [])
            if isinstance(snapshot.get("replay_warnings"), list)
            else []
        ),
        *row_warnings,
    ]
    lines = [
        "# 个人同业选股报告",
        "",
        f"- 参照公司：`{anchor}`",
        f"- 行业：{scope.get('industry', '—')}（TuShare 粗粒度标签）",
        f"- 估值数据日：{snapshot.get('data_date', '—')}；实际筛选时间：{snapshot.get('screened_at', '—')}",
        f"- 采用年报年份：{'；'.join(annual_coverages) or '—'}",
        f"- 来源：基础信息 `tushare.stock_basic`；估值 `tushare.daily_basic`；财务 {financial_sources}",
        f"- 覆盖：枚举 {scope.get('enumerated_count', 0)} / 截取 {selected_count} / 合格 {qualified_count} / 缺失或排除 {selected_count - qualified_count}",
        f"- 范围限制：最多 {scope.get('cap', CAP)} 家，按同日总市值截取，偏向较大公司；不是完整行业或全市场扫描。",
        "",
    ]
    if warnings:
        lines.append("> **快照警告：**" + "；".join(str(item) for item in warnings))
        lines.append("")
    lines.extend(comparison_sections(snapshot))
    if results.get("discovery_complete"):
        lines.append(
            f"> 已有 {results.get('outside_watchlist_qualified_count', 0)} 家池外公司以完整数据参与比较。"
        )
    else:
        lines.append(
            "> **交付缺口：**没有合格池外公司参与比较，本次不能称为完成同业发现。"
        )
    lines.extend(
        [
            "",
            "## 同业前三与参照位置",
            "",
            "| 公司 | 旧池/池外 | 三年逐年 ROE | ROE 均值 | PB | ROE 名次 | PB 名次 | 研究次序 | 待核查 |",
            "|---|---|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for code in shown:
        row = rows.get(code)
        if row is None:
            continue
        rank = rank_by_code.get(code, {})
        annual = (
            " / ".join(
                f"{str(item.get('period') or '')[:4]}:{fmt_number(item.get('roe_waa'))}%"
                for item in annual_entries(row)
            )
            or "—"
        )
        checks = []
        if any(
            finite_number(item.get("roe_waa")) is not None
            and float(item["roe_waa"]) < 0
            for item in annual_entries(row)
        ):
            checks.append("某年 ROE 为负")
        if row.get("risk_status") != "known_warning":
            checks.append("风险警示/停牌/可交易性待完整核查")
        exclusions = row.get("exclusions")
        checks.extend(exclusions if isinstance(exclusions, list) else [])
        lines.append(
            f"| {row.get('name') or '—'} `{code}` | {'旧池' if row.get('in_watchlist') else '池外'} | {annual} | "
            f"{fmt_number(row.get('roe_mean'))}% | {fmt_number(row.get('pb'))} | {fmt_number(rank.get('roe_rank'), 1)} | "
            f"{fmt_number(rank.get('pb_rank'), 1)} | {fmt_number(rank.get('research_order'), 1)} | {'；'.join(checks) or '—'} |"
        )
    if not shown:
        lines.append("| — | — | — | — | — | — | — | — | 无合格对象 |")

    lines.extend(candidate_review_sections(snapshot))
    lines.extend(
        [
            "",
            "## 完整合格排名",
            "",
            "| 位置 | 公司 | ROE 均值 | PB | 研究次序 |",
            "|---:|---|---:|---:|---:|",
        ]
    )
    for item in ranking:
        ranking_code = item.get("code")
        row = rows.get(ranking_code) if isinstance(ranking_code, str) else None
        if row is None:
            lines.append(
                f"| {item.get('position', '—')} | `{ranking_code or '—'}`（快照缺少公司明细） | — | — | {fmt_number(item.get('research_order'), 1)} |"
            )
            continue
        lines.append(
            f"| {item.get('position', '—')} | {row.get('name') or '—'} `{ranking_code}` | {fmt_number(row.get('roe_mean'))}% | "
            f"{fmt_number(row.get('pb'))} | {fmt_number(item.get('research_order'), 1)} |"
        )
    if not ranking:
        lines.append("| — | 无合格对象 | — | — | — |")

    excluded = [row for row in rows.values() if row.get("exclusions")]
    lines.extend(["", "## 未入选与数据缺口", ""])
    if excluded:
        for row in sorted(excluded, key=lambda item: item["code"]):
            lines.append(
                f"- {row.get('name') or '—'} `{row['code']}`：{', '.join(row['exclusions'])}"
            )
    if scope.get("excluded_missing_valuation"):
        lines.append(
            "- 未进入截取范围（缺同日有效总市值）："
            + ", ".join(scope["excluded_missing_valuation"])
        )
    if scope.get("excluded_by_cap"):
        lines.append(
            f"- 未进入截取范围（超过 cap={scope.get('cap', CAP)}）："
            + ", ".join(scope["excluded_by_cap"])
        )
    if (
        not excluded
        and not scope.get("excluded_missing_valuation")
        and not scope.get("excluded_by_cap")
    ):
        lines.append("- 无。")
    lines.extend(
        [
            "",
            "## 使用边界",
            "",
            "本报告只安排研究顺序，不是买入建议、低估结论或收益模型。资料非实时；主营业务可比性、杠杆、一次性损益、资产质量、风险警示、停牌与可交易性均需人工核查。PB 较低不等于被低估，ROE 较高不等于未来回报更高。",
            "",
        ]
    )
    return "\n".join(lines)


def unique_output_dir(output_root: Path) -> Path:
    output_root.mkdir(parents=True, exist_ok=True)
    stem = datetime.now(SHANGHAI).strftime("%Y%m%dT%H%M%S%z")
    candidate = output_root / stem
    suffix = 1
    while candidate.exists():
        candidate = output_root / f"{stem}-{suffix}"
        suffix += 1
    candidate.mkdir()
    return candidate


def atomic_write(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def save_output(snapshot: dict[str, Any], output_root: Path) -> Path:
    directory = unique_output_dir(output_root)
    atomic_write(
        directory / "snapshot.json",
        json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    report = render_report(snapshot)
    atomic_write(directory / "report.md", report)
    return directory


def load_snapshot(input_path: Path) -> dict[str, Any]:
    try:
        snapshot = json.loads(input_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ScreenError(f"无法读取快照: {exc}") from exc
    if not isinstance(snapshot, dict):
        raise ScreenError("快照顶层必须是对象")
    if snapshot.get("schema_version") != SCHEMA_VERSION:
        raise ScreenError(
            f"不支持的 schema_version: {snapshot.get('schema_version')!r}"
        )
    return snapshot


def replay_snapshot(
    input_path: Path, output_root: Path, compare_path: Path | None = None
) -> Path:
    snapshot = load_snapshot(input_path)
    rows, replay_warnings = indexed_rows(snapshot)
    rank_fields = {"exclusions", "annual_roes", "pb", "roe_mean"}

    def supports_ranking(row: dict[str, Any]) -> bool:
        if not rank_fields <= row.keys() or not isinstance(row["exclusions"], list):
            return False
        if row["exclusions"]:
            return True
        annual = annual_entries(row)
        return (
            len(annual) == 3
            and all(finite_number(item.get("roe_waa")) is not None for item in annual)
            and finite_number(row["pb"]) is not None
            and finite_number(row["roe_mean"]) is not None
        )

    rank_fields_missing = any(not supports_ranking(row) for row in rows.values())
    if snapshot.get("rule") == RULE and not snapshot.get("results"):
        if replay_warnings or rank_fields_missing:
            replay_warnings.append("字段不足，未按当前规则重算排名")
        else:
            try:
                watchlist_codes = snapshot.get("watchlist_codes")
                snapshot["results"] = rank_peers(
                    list(rows.values()),
                    str(snapshot.get("anchor") or ""),
                    watchlist_codes if isinstance(watchlist_codes, list) else [],
                )
            except (KeyError, TypeError, ValueError, ScreenError):
                replay_warnings.append("字段不足，未按当前规则重算排名")
    if replay_warnings:
        snapshot["replay_warnings"] = replay_warnings
    if compare_path:
        snapshot["comparison"] = compare_snapshots(
            load_snapshot(compare_path), snapshot, compare_path
        )
    snapshot["generated_at"] = now_iso()
    snapshot["replayed_from"] = str(input_path.resolve())
    return save_output(snapshot, output_root)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="个人同业选股工具")
    result.add_argument("--tracker-root", type=Path)
    result.add_argument("--anchor")
    result.add_argument("--date")
    result.add_argument("--refresh", action="store_true")
    result.add_argument("--input", type=Path)
    result.add_argument("--compare", type=Path)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    output_root = Path(__file__).resolve().parent / "output"
    try:
        if args.input:
            if args.anchor or args.date or args.refresh or args.tracker_root:
                raise ScreenError(
                    "--input 与 --anchor/--date/--refresh/--tracker-root 互斥"
                )
            directory = replay_snapshot(args.input, output_root, args.compare)
        else:
            if not args.anchor:
                raise ScreenError("参照模式必须提供 --anchor")
            if not args.refresh:
                raise ScreenError(
                    "当前没有完整本地同业清单；请显式使用 --refresh 或用 --input 复看快照"
                )
            previous = load_snapshot(args.compare) if args.compare else None
            tracker_root = args.tracker_root or (
                Path(__file__).resolve().parent.parent / "a-stock-tracker"
            )
            snapshot = build_live_snapshot(tracker_root, args.anchor, args.date)
            if previous is not None:
                snapshot["comparison"] = compare_snapshots(
                    previous, snapshot, args.compare
                )
            directory = save_output(snapshot, output_root)
        print(directory)
        return 0
    except ScreenError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
