import json
import sqlite3
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

import screen


def test_fetch_financials_rejects_mismatched_code() -> None:
    class Frame:
        columns = ["ts_code", "ann_date", "end_date", "update_flag", "roe_waa"]

        def to_json(self, **_kwargs: object) -> str:
            return json.dumps(
                [
                    {"ts_code": "600001.SH"},
                    {"ts_code": "600002.SH"},
                ]
            )

    class Client:
        def fina_indicator(self, **_kwargs: object) -> Frame:
            return Frame()

    with pytest.raises(screen.ScreenError, match="fina_indicator 包含错配代码: 600002.SH"):
        screen.fetch_financials(
            Client(), "unused", "600001.SH", "2026-09-20", "2026-09-20T16:00:00+08:00"
        )


def test_reference_read_is_literal_and_read_only(tmp_path: Path) -> None:
    tracker = tmp_path / "tracker"
    package = tracker / "a_stock_tracker"
    package.mkdir(parents=True)
    config = package / "config.py"
    config.write_text(
        "import os\nWATCHLIST: list[dict] = [{'code': '600900', 'name': '长江电力'}]\n",
        encoding="utf-8",
    )
    db = tracker / "tracker.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE stock_fundamentals(code TEXT, name TEXT, industry TEXT, updated_at TEXT)"
        )
        conn.execute(
            "INSERT INTO stock_fundamentals VALUES ('600900','长江电力','水力发电','2026-09-22')"
        )
    before = db.read_bytes()

    reference = screen.load_reference(tracker, "600900.SH")

    assert reference["anchor"] == "600900.SH"
    assert reference["legacy_reference"]["industry"] == "水力发电"
    assert db.read_bytes() == before


def test_scope_cap_and_average_tie_ranking() -> None:
    stocks = [
        {
            "ts_code": "600900.SH",
            "name": "参照",
            "industry": "水力发电",
            "market": "主板",
            "exchange": "SSE",
            "list_status": "L",
        },
        {
            "ts_code": "600001.SH",
            "name": "同业甲",
            "industry": "水力发电",
            "market": "主板",
            "exchange": "SSE",
            "list_status": "L",
        },
        {
            "ts_code": "000001.SZ",
            "name": "同业乙",
            "industry": "水力发电",
            "market": "主板",
            "exchange": "SZSE",
            "list_status": "L",
        },
        {
            "ts_code": "600002.SH",
            "name": "同业丙",
            "industry": "水力发电",
            "market": "主板",
            "exchange": "SSE",
            "list_status": "L",
        },
        {
            "ts_code": "300001.SZ",
            "name": "创业板",
            "industry": "水力发电",
            "market": "创业板",
            "exchange": "SZSE",
            "list_status": "L",
        },
        {
            "ts_code": "920001.BJ",
            "name": "北交所",
            "industry": "水力发电",
            "market": "北交所",
            "exchange": "BSE",
            "list_status": "L",
        },
    ]
    valuations = [
        {"ts_code": "600900.SH", "pb": 2, "total_mv": 50},
        {"ts_code": "600001.SH", "pb": 1, "total_mv": 100},
        {"ts_code": "000001.SZ", "pb": 2, "total_mv": 90},
        {"ts_code": "600002.SH", "pb": 3, "total_mv": 80},
    ]
    selected, scope, _ = screen.load_peers(stocks, valuations, "600900.SH", ["600900"], cap=3)
    assert [row["ts_code"] for row in selected] == [
        "600900.SH",
        "600001.SH",
        "000001.SZ",
    ]
    assert scope["excluded_by_cap"] == ["600002.SH"]

    rows = [
        {
            "code": "600900.SH",
            "pb": 2.0,
            "roe_mean": 10.0,
            "annual_roes": [{}, {}, {}],
            "exclusions": [],
        },
        {
            "code": "600001.SH",
            "pb": 1.0,
            "roe_mean": 10.0,
            "annual_roes": [{}, {}, {}],
            "exclusions": [],
        },
        {
            "code": "000001.SZ",
            "pb": 3.0,
            "roe_mean": -1.0,
            "annual_roes": [{}, {}, {}],
            "exclusions": ["NON_POSITIVE_ROE_MEAN"],
        },
    ]
    result = screen.rank_peers(rows, "600900.SH", ["600900"])
    ranking = {item["code"]: item for item in result["ranking"]}
    assert ranking["600001.SH"]["roe_rank"] == 1.5
    assert ranking["600900.SH"]["roe_rank"] == 1.5
    assert ranking["600001.SH"]["position"] == 1
    assert ranking["600900.SH"]["position"] == 2


def test_three_year_selection_prefers_unique_revision_and_rejects_gap() -> None:
    records = [
        {
            "end_date": "20251231",
            "ann_date": "20260320",
            "roe_waa": 8,
            "update_flag": "0",
            "source": "test",
        },
        {
            "end_date": "20251231",
            "ann_date": "20260321",
            "roe_waa": 9,
            "update_flag": "1",
            "source": "test",
            "acquired_at": "2026-09-20T10:00:00+08:00",
        },
        {
            "end_date": "20251231",
            "ann_date": "20260321",
            "roe_waa": 9,
            "update_flag": "1",
            "source": "test",
            "acquired_at": "2026-09-22T10:00:00+08:00",
        },
        {
            "end_date": "20241231",
            "ann_date": "20250320",
            "roe_waa": 10,
            "update_flag": "0",
            "source": "test",
        },
        {
            "end_date": "20231231",
            "ann_date": "20240320",
            "roe_waa": 11,
            "update_flag": "0",
            "source": "test",
        },
    ]
    selected = screen.select_annual_roes(records, "2026-09-22", "2026-09-22T20:00:00+08:00")
    assert selected["error"] is None
    assert selected["roe_mean"] == 10
    assert selected["annual_roes"][-1]["roe_waa"] == 9
    assert selected["annual_roes"][-1]["selection_basis"] == "unique_update_flag_1"
    assert selected["annual_roes"][-1]["acquired_at"] == "2026-09-22T10:00:00+08:00"

    gap = [row for row in records if row["end_date"] != "20241231"]
    assert (
        screen.select_annual_roes(gap, "2026-09-22", "2026-09-22T20:00:00+08:00")["error"]
        == "MISSING_THREE_ANNUAL_REPORTS"
    )


def test_replay_is_offline_and_does_not_overwrite_input(tmp_path: Path) -> None:
    input_path = tmp_path / "snapshot.json"
    snapshot = {
        "schema_version": 1,
        "rule": screen.RULE,
        "screened_at": "2026-09-22T10:00:00+08:00",
        "generated_at": "2026-09-22T10:00:00+08:00",
        "data_date": "2026-09-21",
        "anchor": "600900.SH",
        "watchlist_codes": ["600900"],
        "scope": {"industry": "水力发电", "enumerated_count": 1, "cap": 50},
        "rows": [],
        "results": {},
    }
    original = json.dumps(snapshot, ensure_ascii=False)
    input_path.write_text(original, encoding="utf-8")

    previous_path = tmp_path / "previous.json"
    previous_path.write_text(original, encoding="utf-8")
    output = screen.replay_snapshot(input_path, tmp_path / "output", previous_path)

    assert input_path.read_text(encoding="utf-8") == original
    assert (output / "snapshot.json").is_file()
    assert (output / "report.md").is_file()
    replayed = json.loads((output / "snapshot.json").read_text(encoding="utf-8"))
    assert replayed["screened_at"] == snapshot["screened_at"]
    assert replayed["data_date"] == snapshot["data_date"]
    assert replayed["replayed_from"] == str(input_path.resolve())
    assert replayed["comparison"]["has_changes"] is False


def test_incomplete_snapshot_replays_without_fabricating_results(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "incomplete.json"
    input_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "rule": screen.RULE,
                "anchor": "600001.SH",
                "rows": [
                    {
                        "code": "600001.SH",
                        "name": "候选",
                        "pb": 1.0,
                        "roe_mean": 10.0,
                        "annual_roes": [],
                        "exclusions": [],
                    }
                ],
                "results": {},
            }
        ),
        encoding="utf-8",
    )

    output = screen.replay_snapshot(input_path, tmp_path / "output")
    replayed = json.loads((output / "snapshot.json").read_text(encoding="utf-8"))
    report = (output / "report.md").read_text(encoding="utf-8")

    assert replayed["results"] == {}
    assert replayed["replay_warnings"] == ["字段不足，未按当前规则重算排名"]
    assert "快照警告" in report
    assert "采用年报年份：—" in report

    review = screen.candidate_review_sections(
        {
            "anchor": "600001.SH",
            "rows": [
                {
                    "code": "600001.SH",
                    "name": "候选",
                    "roe_mean": None,
                    "pb": None,
                    "annual_roes": [],
                }
            ],
            "results": {
                "top": ["600001.SH"],
                "ranking": [{"code": "600001.SH", "position": 1}],
            },
        }
    )
    assert "逐年 ROE 数据不足" in "\n".join(review)
    assert "未触发负 ROE" not in "\n".join(review)


def test_unsupported_rule_replays_without_recalculation(tmp_path: Path) -> None:
    input_path = tmp_path / "legacy.json"
    legacy_results = {"top": [], "ranking": [], "legacy_marker": True}
    input_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "rule": "peer-screen-v0",
                "anchor": "600001.SH",
                "scope": {},
                "rows": [],
                "results": legacy_results,
            }
        ),
        encoding="utf-8",
    )

    output = screen.replay_snapshot(input_path, tmp_path / "output")
    replayed = json.loads((output / "snapshot.json").read_text(encoding="utf-8"))
    report = (output / "report.md").read_text(encoding="utf-8")

    assert replayed["results"] == legacy_results
    assert replayed["replay_warnings"] == [
        "规则 peer-screen-v0 不受当前版本支持，保留原结果且未重算排名"
    ]
    assert "快照警告" in report


def test_report_contains_candidate_review_without_investment_claims() -> None:
    annual_anchor = [
        {"period": "2023-12-31", "roe_waa": 13},
        {"period": "2024-12-31", "roe_waa": 14},
        {"period": "2025-12-31", "roe_waa": 15},
    ]
    annual_candidate = [
        {"period": "2023-12-31", "roe_waa": 12},
        {"period": "2024-12-31", "roe_waa": 11},
        {"period": "2025-12-31", "roe_waa": 10},
    ]
    rows = [
        {
            "code": "600900.SH",
            "name": "参照",
            "pb": 3.0,
            "roe_mean": 14.0,
            "annual_roes": annual_anchor,
            "in_watchlist": True,
            "risk_status": "name_check_clear_other_risks_unknown",
            "exclusions": [],
            "financial_source": "test",
        },
        {
            "code": "600001.SH",
            "name": "候选",
            "pb": 1.0,
            "roe_mean": 11.0,
            "annual_roes": annual_candidate,
            "in_watchlist": False,
            "risk_status": "name_check_clear_other_risks_unknown",
            "exclusions": [],
            "financial_source": "test",
        },
    ]
    snapshot = {
        "anchor": "600900.SH",
        "data_date": "2026-09-22",
        "screened_at": "2026-09-22T20:00:00+08:00",
        "scope": {"industry": "水力发电", "enumerated_count": 2, "cap": 50},
        "rows": rows,
        "results": screen.rank_peers(rows, "600900.SH", ["600900"]),
    }

    report = screen.render_report(snapshot)

    assert "采用年报年份：2023/2024/2025" in report
    assert "## 候选审查" in report
    assert "ROE 均值差 -3.00 个百分点" in report
    assert "PB 差 -2.00 倍（候选减参照）" in report
    assert "最新年度 ROE 从 11.00% 降至 10.00%" in report
    assert "不是买入建议" in report


def test_compare_classifies_member_change_without_calling_it_deterioration(
    tmp_path: Path,
) -> None:
    annual = [
        {
            "period": "2023-12-31",
            "roe_waa": 8,
            "ann_date": "2024-03-01",
            "update_flag": "0",
        },
        {
            "period": "2024-12-31",
            "roe_waa": 9,
            "ann_date": "2025-03-01",
            "update_flag": "0",
        },
        {
            "period": "2025-12-31",
            "roe_waa": 10,
            "ann_date": "2026-03-01",
            "update_flag": "0",
        },
    ]

    def row(code: str, name: str, pb: float) -> dict:
        return {
            "code": code,
            "name": name,
            "pb": pb,
            "annual_roes": annual,
            "exclusions": [],
        }

    common = {
        "schema_version": 1,
        "rule": screen.RULE,
        "formula": "same",
        "limits": {"cap": 50},
        "anchor": "600900.SH",
        "scope": {
            "industry": "水力发电",
            "industry_source": "tushare.stock_basic",
            "cap": 50,
            "market_bias": "same",
        },
        "screened_at": "2026-09-22T20:00:00+08:00",
        "data_date": "2026-09-22",
    }
    previous: dict[str, Any] = {
        **common,
        "rows": [row("600900.SH", "参照", 3), row("600001.SH", "旧成员", 1)],
        "results": {"top": ["600001.SH", "600900.SH"]},
    }
    current = {
        **common,
        "rows": [row("600900.SH", "参照", 3), row("600002.SH", "新成员", 1)],
        "results": {"top": ["600002.SH", "600900.SH"]},
    }

    comparison = screen.compare_snapshots(previous, current, tmp_path / "previous.json")
    current["comparison"] = comparison
    text = "\n".join(screen.comparison_sections(current))

    assert comparison["scope_exits"] == [{"code": "600001.SH", "name": "旧成员"}]
    assert comparison["scope_entries"] == [{"code": "600002.SH", "name": "新成员"}]
    assert comparison["newly_missing"] == []
    assert comparison["resolved_missing"] == []
    assert "范围变化可能影响排名，不代表公司经营恶化" in text

    unchanged = screen.compare_snapshots(previous, previous, tmp_path / "same.json")
    assert unchanged["has_changes"] is False

    missing = deepcopy(previous)
    missing["rows"][1]["pb"] = None
    missing["rows"][1]["annual_roes"] = []
    changed = screen.compare_snapshots(previous, missing, tmp_path / "before-missing.json")
    assert changed["newly_missing"] == [
        {
            "code": "600001.SH",
            "name": "旧成员",
            "reasons": ["ANNUAL_DATA_MISSING", "PB_MISSING"],
        }
    ]
    recovered = screen.compare_snapshots(missing, previous, tmp_path / "recovered.json")
    assert recovered["resolved_missing"] == changed["newly_missing"]
