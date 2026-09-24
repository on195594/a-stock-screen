"""SQLite workspace bootstrap and personal research state."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import urllib.parse
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal, TypeGuard

Mode = Literal["demo", "production"]
SCHEMA_VERSION = 1
WORKSPACE_FILE = "workspace.sqlite3"
MODE_FILE = ".workspace-mode"
VALID_FINANCIAL_SOURCES = {"tushare.fina_indicator", "tracker", "tracker:data/tushare-primary.db"}

DDL = """
CREATE TABLE screen_runs (
    run_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('peer', 'watch')),
    anchor_code TEXT,
    rule_id TEXT,
    captured_at TEXT NOT NULL,
    valuation_date TEXT,
    health TEXT NOT NULL CHECK (health IN ('complete', 'partial', 'unverified')),
    snapshot_path TEXT NOT NULL UNIQUE,
    snapshot_sha256 TEXT NOT NULL UNIQUE CHECK (length(snapshot_sha256) = 64),
    indexed_at TEXT NOT NULL,
    CHECK ((kind = 'peer' AND anchor_code IS NOT NULL AND rule_id IS NOT NULL)
        OR (kind = 'watch' AND anchor_code IS NULL AND rule_id IS NULL))
);
CREATE INDEX idx_runs_context ON screen_runs(kind, anchor_code, captured_at);
CREATE INDEX idx_runs_rule_date ON screen_runs(rule_id, valuation_date);
CREATE TABLE watch_items (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    anchor_code TEXT,
    status TEXT NOT NULL CHECK (status IN ('research', 'observe', 'paused')),
    reason TEXT NOT NULL DEFAULT '' CHECK (length(reason) <= 1000),
    next_check TEXT NOT NULL DEFAULT '' CHECK (length(next_check) <= 1000),
    note_url TEXT CHECK (note_url IS NULL OR length(note_url) <= 2048),
    added_run_id TEXT NOT NULL REFERENCES screen_runs(run_id),
    ack_run_id TEXT REFERENCES screen_runs(run_id),
    ack_at TEXT,
    updated_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    CHECK ((ack_run_id IS NULL AND ack_at IS NULL)
        OR (ack_run_id IS NOT NULL AND ack_at IS NOT NULL))
);
CREATE INDEX idx_watch_status ON watch_items(status, updated_at);
CREATE TABLE update_jobs (
    job_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    request_aliases_json TEXT NOT NULL DEFAULT '[]'
        CHECK (json_valid(request_aliases_json) AND json_type(request_aliases_json) = 'array'),
    kind TEXT NOT NULL CHECK (kind IN ('peer', 'watch')),
    payload_json TEXT NOT NULL
        CHECK (json_valid(payload_json) AND json_type(payload_json) = 'object'),
    dedupe_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('queued', 'running', 'succeeded', 'failed', 'interrupted')
    ),
    phase TEXT NOT NULL DEFAULT 'queued',
    processed INTEGER NOT NULL DEFAULT 0 CHECK (processed >= 0),
    total INTEGER CHECK (total IS NULL OR total >= processed),
    requested_at TEXT NOT NULL,
    started_at TEXT,
    updated_at TEXT NOT NULL,
    finished_at TEXT,
    error_code TEXT,
    error_summary TEXT,
    result_run_id TEXT REFERENCES screen_runs(run_id),
    CHECK ((status = 'succeeded' AND result_run_id IS NOT NULL)
        OR (status != 'succeeded' AND result_run_id IS NULL)),
    CHECK ((status IN ('queued', 'running') AND finished_at IS NULL)
        OR (status IN ('succeeded', 'failed', 'interrupted') AND finished_at IS NOT NULL)),
    CHECK (status != 'running' OR started_at IS NOT NULL)
);
CREATE UNIQUE INDEX idx_active_job_dedupe ON update_jobs(dedupe_key)
    WHERE status IN ('queued', 'running');
CREATE INDEX idx_jobs_queue ON update_jobs(status, requested_at);
"""

EXPECTED_COLUMNS = {
    "screen_runs": {"run_id", "kind", "snapshot_path", "snapshot_sha256"},
    "watch_items": {"code", "name", "status", "reason", "revision", "added_run_id"},
    "update_jobs": {"job_id", "request_id", "payload_json", "status"},
}


class WorkspaceError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def parse_iso_utc(ts: str) -> str:
    """Normalize ISO timestamp to UTC ISO string with microseconds."""
    try:
        dt = datetime.fromisoformat(ts)
    except Exception as exc:
        raise WorkspaceError(f"invalid timestamp format: {ts}") from exc
    if dt.tzinfo is None:
        raise WorkspaceError(f"timestamp missing timezone: {ts}")
    dt_utc = dt.astimezone(UTC)
    now_utc = datetime.now(UTC)
    if dt_utc > now_utc:
        raise WorkspaceError(f"timestamp in the future: {ts}")
    return dt_utc.isoformat(timespec="microseconds")


def _sqlite_version_tuple() -> tuple[int, ...]:
    return tuple(int(p) for p in sqlite3.sqlite_version.split("."))


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=5, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


EXPECTED_INDEXES = {
    "idx_active_job_dedupe",
    "idx_jobs_queue",
    "idx_runs_rule_date",
    "idx_watch_status",
}


def check_wal_safety(journal_mode: str, mode: Mode | None = None) -> None:
    """Validate SQLite version safety for WAL mode across new and existing workspaces."""
    if journal_mode.upper() == "WAL":
        ver = _sqlite_version_tuple()
        if ver < (3, 37, 0):
            raise WorkspaceError(
                f"SQLite WAL mode requires sqlite >= 3.37.0 (contains WAL corruption fixes), current: {sqlite3.sqlite_version}"
            )
        wal_safe = ver >= (3, 51, 3) or ver in ((3, 44, 6), (3, 50, 7))
        if not wal_safe and mode == "production":
            raise WorkspaceError(
                f"SQLite version {sqlite3.sqlite_version} is not certified for WAL-reset safety in production"
            )


def _verify_schema(conn: sqlite3.Connection, mode: Mode | None = None, deep: bool = False) -> None:
    if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        raise WorkspaceError("workspace schema version mismatch")
    names = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    if names != set(EXPECTED_COLUMNS):
        raise WorkspaceError("workspace tables mismatch")
    for table, expected in EXPECTED_COLUMNS.items():
        actual = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not expected <= actual:
            raise WorkspaceError(f"workspace schema mismatch: {table}")

    # Verify critical indexes
    indexes = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
        )
    }
    if not EXPECTED_INDEXES <= indexes:
        raise WorkspaceError(f"missing required workspace indexes: {EXPECTED_INDEXES - indexes}")

    # Verify foreign key integrity during deep verification (e.g. at startup/init)
    if deep:
        fk_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
        if fk_errors:
            raise WorkspaceError("foreign key constraint violation in workspace")

    # Check journal mode safety: WAL requires verified safe version
    jm = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).upper()
    check_wal_safety(jm, mode=mode)

    try:
        conn.execute("SELECT json_valid('{}')").fetchone()
    except sqlite3.OperationalError as exc:
        raise WorkspaceError("SQLite JSON support is required") from exc


def initialize(
    state_dir: Path,
    mode: Mode,
    journal_mode: Literal["WAL", "DELETE"] = "WAL",
) -> Path:
    """Create or validate a private workspace; never migrate an existing schema."""
    if mode not in ("demo", "production"):
        raise ValueError("mode must be demo or production")
    if journal_mode not in ("WAL", "DELETE"):
        raise ValueError("journal_mode must be WAL or DELETE")

    check_wal_safety(journal_mode, mode=mode)

    state_dir = state_dir.expanduser().resolve()
    db_path = state_dir / WORKSPACE_FILE
    marker = state_dir / MODE_FILE
    db_existed = db_path.exists()
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(state_dir, 0o700)

    if marker.exists():
        if marker.read_text(encoding="ascii").strip() != mode:
            raise WorkspaceError("workspace mode mismatch")
    elif db_existed:
        raise WorkspaceError("existing workspace is missing its mode marker")
    else:
        temporary = marker.with_suffix(".tmp")
        temporary.write_text(mode + "\n", encoding="ascii")
        os.chmod(temporary, 0o600)
        os.replace(temporary, marker)

    if db_path.exists():
        with _connect(db_path) as conn:
            _verify_schema(conn, mode=mode, deep=True)
        return db_path

    with _connect(db_path) as conn:
        journal = conn.execute(f"PRAGMA journal_mode={journal_mode}").fetchone()[0]
        if journal.lower() != journal_mode.lower():
            raise WorkspaceError(f"SQLite {journal_mode} mode unavailable")
        try:
            conn.executescript(f"BEGIN IMMEDIATE;\n{DDL}\nPRAGMA user_version={SCHEMA_VERSION};")
            _verify_schema(conn, mode=mode, deep=True)
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    os.chmod(db_path, 0o600)
    return db_path


def connect_workspace(state_dir: Path, mode: Mode) -> sqlite3.Connection:
    state_dir = state_dir.expanduser().resolve()
    db_path = state_dir / WORKSPACE_FILE
    marker = state_dir / MODE_FILE
    if not db_path.is_file() or not marker.is_file():
        raise WorkspaceError("workspace is not initialized")
    if marker.read_text(encoding="ascii").strip() != mode:
        raise WorkspaceError("workspace mode mismatch")
    conn = _connect(db_path)
    try:
        _verify_schema(conn, mode=mode, deep=False)
    except Exception:
        conn.close()
        raise
    return conn


def register_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    kind: Literal["peer", "watch"],
    anchor_code: str | None,
    rule_id: str | None,
    captured_at: str,
    valuation_date: str | None,
    health: Literal["complete", "partial", "unverified"],
    snapshot_path: str,
    snapshot_bytes: bytes,
) -> None:
    path = Path(snapshot_path)
    if path.is_absolute() or ".." in path.parts:
        raise WorkspaceError("snapshot path must be workspace-relative")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", run_id):
        raise WorkspaceError("invalid run id")
    conn.execute(
        """INSERT INTO screen_runs
        (run_id,kind,anchor_code,rule_id,captured_at,valuation_date,health,
         snapshot_path,snapshot_sha256,indexed_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            run_id,
            kind,
            anchor_code,
            rule_id,
            captured_at,
            valuation_date,
            health,
            path.as_posix(),
            hashlib.sha256(snapshot_bytes).hexdigest(),
            utc_now(),
        ),
    )


def _is_finite_number(value: Any) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _parse_report_date(val: Any) -> date | None:
    text = str(val or "").strip().replace("-", "")
    if len(text) == 8 and text.isdigit():
        try:
            return datetime.strptime(text, "%Y%m%d").date()
        except (ValueError, OverflowError):
            pass
    return None


def is_row_usable(row: dict[str, Any]) -> bool:
    """Determine whether a snapshot row contains usable facts for personal review."""
    pb_val = row.get("pb")
    pb_ok = _is_finite_number(pb_val)
    roes = row.get("annual_roes")
    roes_ok = (
        isinstance(roes, list)
        and len(roes) == 3
        and all(
            isinstance(item, dict)
            and _is_finite_number(
                item.get("roe_waa") if item.get("roe_waa") is not None else item.get("roe")
            )
            and isinstance(item.get("source"), str)
            and item["source"].strip().lower() in VALID_FINANCIAL_SOURCES | {"fixture"}
            and all(
                _is_finite_number(item[key])
                for key in ("roe", "roe_waa")
                if item.get(key) is not None
            )
            for item in roes
        )
    )
    years: list[int] = []
    if roes_ok and isinstance(roes, list):
        for item in roes:
            year = item.get("year")
            if year is not None and (type(year) is not int or not 1 <= year <= 9999):
                return False
            period_end = date(year, 12, 31) if year is not None else None
            for key in ("end_date", "period"):
                if item.get(key) is not None:
                    parsed = _parse_report_date(item[key])
                    if (
                        parsed is None
                        or (parsed.month, parsed.day) != (12, 31)
                        or (period_end is not None and parsed != period_end)
                    ):
                        return False
                    period_end = parsed
            announced = _parse_report_date(item.get("ann_date"))
            if period_end is None or announced is None or announced < period_end:
                return False
            years.append(period_end.year)
        sorted_years = sorted(years)
        if sorted_years != list(range(sorted_years[0], sorted_years[0] + 3)):
            return False
    return bool(
        pb_ok
        and roes_ok
        and _is_finite_number(row.get("roe_mean"))
        and isinstance(row.get("valuation_date"), str)
        and re.fullmatch(r"\d{4}-\d{2}-\d{2}", row["valuation_date"])
        and row.get("valuation_source") in ("tushare.daily_basic", "tracker", "fixture")
        and row.get("financial_source")
        in ("tushare.fina_indicator", "tracker", "tracker:data/tushare-primary.db", "fixture")
        and row.get("risk_source")
        in ("tushare.stock_basic.name", "tushare.stock_basic", "tracker", "fixture")
    )


def import_snapshot(
    state_dir: Path,
    snapshot_file: Path,
    mode: Mode,
) -> str:
    """Import a peer snapshot into the workspace, deduplicating by SHA-256."""
    state_dir = state_dir.expanduser().resolve()
    snapshot_file = snapshot_file.expanduser().resolve()
    if not snapshot_file.is_file():
        raise WorkspaceError(f"snapshot file not found: {snapshot_file}")

    raw_bytes = snapshot_file.read_bytes()
    sha256 = hashlib.sha256(raw_bytes).hexdigest()

    conn = connect_workspace(state_dir, mode)
    try:
        # Deduplication: return existing run_id if identical content was already imported
        existing = conn.execute(
            "SELECT run_id FROM screen_runs WHERE snapshot_sha256 = ?", (sha256,)
        ).fetchone()
        if existing:
            return existing[0]

        try:
            data = json.loads(raw_bytes.decode("utf-8"))
        except Exception as exc:
            raise WorkspaceError("failed to parse snapshot JSON") from exc

        if not isinstance(data, dict):
            raise WorkspaceError("snapshot root must be a JSON object")

        top_rows = data.get("rows")
        if (
            top_rows is None
            or not isinstance(top_rows, list)
            or not all(isinstance(r, dict) for r in top_rows)
        ):
            raise WorkspaceError("snapshot rows must be a list of objects")

        rows: list[dict[str, Any]] = top_rows

        # Production rejects fixture data
        is_fixture = bool(
            data.get("is_fixture")
            or data.get("source") == "fixture"
            or (isinstance(data.get("scope"), dict) and data["scope"].get("source") == "fixture")
            or any(
                any(
                    row.get(key) == "fixture"
                    for key in ("valuation_source", "financial_source", "risk_source")
                )
                or (
                    isinstance(row.get("annual_roes"), list)
                    and any(
                        isinstance(report, dict) and report.get("source") == "fixture"
                        for report in row["annual_roes"]
                    )
                )
                for row in rows
            )
        )
        if mode == "production" and is_fixture:
            raise WorkspaceError("production workspace rejects fixture data")

        anchor = data.get("anchor")
        if not anchor or not re.fullmatch(r"\d{6}\.(?:SH|SZ|BJ)", str(anchor)):
            raise WorkspaceError("snapshot missing or invalid anchor code")

        raw_ts = data.get("screened_at") or data.get("generated_at")
        if not raw_ts:
            raise WorkspaceError("snapshot missing screened_at / generated_at")
        captured_at = parse_iso_utc(str(raw_ts))

        valuation_date = data.get("data_date")
        if valuation_date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(valuation_date)):
            raise WorkspaceError("snapshot has invalid data_date format")

        raw_rule = data.get("rule") or data.get("rule_id")
        if not raw_rule:
            rule_id = "unknown"
        else:
            rule_id = str(raw_rule)
        has_replay = bool(data.get("replayed_from") or data.get("is_replay"))

        # Health determination
        results = data.get("results")
        ranking = results.get("ranking") if isinstance(results, dict) else None

        # Verify results.rows consistency with top-level rows
        results_rows_consistent = True
        if isinstance(results, dict) and "rows" in results:
            if results["rows"] != rows:
                results_rows_consistent = False

        # Strict provenance check: top-level source must equal scope.source,
        # and must belong to explicitly allowed sources for the workspace mode.
        allowed_sources = {"tracker", "fixture"} if mode == "demo" else {"tracker"}
        top_source = data.get("source")
        scope_source = (
            data.get("scope", {}).get("source") if isinstance(data.get("scope"), dict) else None
        )
        source_ok = (
            isinstance(top_source, str)
            and isinstance(scope_source, str)
            and top_source == scope_source
            and top_source in allowed_sources
        )

        limits = data.get("limits")
        limits_ok = (
            isinstance(limits, dict)
            and limits.get("cap") == 50
            and limits.get("top_n") == 3
            and limits.get("max_report_age_days") == 550
        )

        # Candidate scope verification for peer discovery completeness:
        # Check scope.cap matches limits.cap, enumerated_codes, selected_codes,
        # excluded_missing_valuation, and excluded_by_cap form a consistent partition.
        scope = data.get("scope")
        scope_ok = False
        if isinstance(scope, dict) and len(rows) > 0 and limits_ok:
            enumerated = scope.get("enumerated_count")
            if enumerated is None:
                enumerated = scope.get("total_candidates")
            scope_cap = scope.get("cap")
            selected_codes = scope.get("selected_codes")
            enumerated_codes = scope.get("enumerated_codes")
            excluded_missing_valuation = scope.get("excluded_missing_valuation")
            excluded_by_cap = scope.get("excluded_by_cap")

            row_codes_list = [
                str(r.get("code") or r.get("ts_code"))
                for r in rows
                if (r.get("code") or r.get("ts_code"))
            ]
            row_codes_set = set(row_codes_list)
            anchor_in_rows = anchor in row_codes_set

            def is_valid_codes_list(lst: Any) -> bool:
                return isinstance(lst, list) and all(
                    isinstance(c, str) and bool(re.fullmatch(r"\d{6}\.(?:SH|SZ|BJ)", c))
                    for c in lst
                )

            if (
                scope_cap == 50
                and isinstance(enumerated, int)
                and enumerated > 0
                and isinstance(selected_codes, list)
                and isinstance(enumerated_codes, list)
                and isinstance(excluded_missing_valuation, list)
                and isinstance(excluded_by_cap, list)
                and is_valid_codes_list(selected_codes)
                and is_valid_codes_list(enumerated_codes)
                and is_valid_codes_list(excluded_missing_valuation)
                and is_valid_codes_list(excluded_by_cap)
            ):
                sel_set = set(selected_codes)
                enum_set = set(enumerated_codes)
                miss_set = set(excluded_missing_valuation)
                cap_set = set(excluded_by_cap)

                # Uniqueness within lists
                no_dups = (
                    len(sel_set) == len(selected_codes)
                    and len(enum_set) == len(enumerated_codes)
                    and len(miss_set) == len(excluded_missing_valuation)
                    and len(cap_set) == len(excluded_by_cap)
                )

                # Enumerated count matches length
                count_ok = enumerated == len(enumerated_codes)

                # Partition equation: enumerated = selected | missing_val | excluded_by_cap
                partition_ok = enum_set == (sel_set | miss_set | cap_set)

                # Disjoint sets
                disjoint_ok = (
                    sel_set.isdisjoint(miss_set)
                    and sel_set.isdisjoint(cap_set)
                    and miss_set.isdisjoint(cap_set)
                )

                # Cap truncation rule
                eligible_count = len(enum_set) - len(miss_set)
                if eligible_count > 50:
                    cap_logic_ok = len(selected_codes) == 50 and len(excluded_by_cap) == (
                        eligible_count - 50
                    )
                else:
                    cap_logic_ok = (
                        len(selected_codes) == eligible_count and len(excluded_by_cap) == 0
                    )

                # Rows and anchor match
                rows_match = (
                    sel_set == row_codes_set
                    and len(selected_codes) == len(row_codes_list)
                    and anchor_in_rows
                    and anchor in sel_set
                )

                if (
                    no_dups
                    and count_ok
                    and partition_ok
                    and disjoint_ok
                    and cap_logic_ok
                    and rows_match
                ):
                    scope_ok = True

        is_verified_complete = False
        is_verified_partial = False
        if (
            not has_replay
            and rule_id == "peer-screen-v1"
            and isinstance(results, dict)
            and valuation_date
            and len(rows) > 0
            and isinstance(ranking, list)
            and results_rows_consistent
            and source_ok
            and scope_ok
            and limits_ok
        ):
            # 1. Verify code uniqueness
            row_codes = [
                str(r.get("code") or r.get("ts_code"))
                for r in rows
                if (r.get("code") or r.get("ts_code"))
            ]
            ranking_codes = [
                str(r.get("code") or r.get("ts_code"))
                for r in ranking
                if isinstance(r, dict) and (r.get("code") or r.get("ts_code"))
            ]
            unique_codes_ok = (
                len(row_codes) == len(rows)
                and len(row_codes) == len(set(row_codes))
                and len(ranking_codes) == len(ranking)
                and len(ranking_codes) == len(set(ranking_codes))
            )

            # 2. Check each row and derive usable facts consistent with screen.py
            facts_consistent = True
            normalized_rows = []
            has_row_failure = False
            all_rows_explained = True

            VALID_VALUATION_SOURCES = {"tushare.daily_basic", "tracker"}
            financial_sources = VALID_FINANCIAL_SOURCES | ({"fixture"} if mode == "demo" else set())
            VALID_RISK_SOURCES = {
                "tushare.stock_basic.name",
                "tushare.stock_basic",
                "tracker",
            }
            ALLOWED_EXCLUDED_VALUATION_SOURCES = {"none", "tushare.daily_basic:error"}
            ALLOWED_EXCLUDED_FINANCIAL_SOURCES = {"none", "tushare.fina_indicator:error"}
            ALLOWED_EXCLUDED_RISK_SOURCES = {"none", "tushare.stock_basic:error"}
            if mode == "demo":
                VALID_VALUATION_SOURCES.add("fixture")
                VALID_RISK_SOURCES.add("fixture")

            def is_valid_row_source(src_key: str, src: Any, is_excluded: bool = False) -> bool:
                if not isinstance(src, str) or not src.strip():
                    return False
                s = src.strip().lower()
                if src_key == "valuation_source":
                    return s in VALID_VALUATION_SOURCES or (
                        is_excluded and s in ALLOWED_EXCLUDED_VALUATION_SOURCES
                    )
                if src_key == "financial_source":
                    if s in financial_sources:
                        return True
                    if is_excluded and s in ALLOWED_EXCLUDED_FINANCIAL_SOURCES:
                        return True
                    return False
                if src_key == "risk_source":
                    return s in VALID_RISK_SOURCES or (
                        is_excluded and s in ALLOWED_EXCLUDED_RISK_SOURCES
                    )
                return False

            screened_date = _parse_report_date(
                str(data.get("screened_at") or data.get("generated_at") or captured_at)[:10]
            )

            for r in rows:
                code_str = str(r.get("code") or r.get("ts_code"))

                exclusions = list(r.get("exclusions") or [])
                fin_status = r.get("financial_status")
                if fin_status and fin_status != "ok":
                    exclusions.append(str(fin_status))
                is_excluded = len(exclusions) > 0 or r.get("facts_usable") is False

                row_has_failure = False
                for src_key in ("financial_source", "valuation_source", "risk_source"):
                    val_src = str(r.get(src_key) or "").lower()
                    if ":error" in val_src:
                        row_has_failure = True
                        break
                for exc_item in exclusions:
                    e = str(exc_item).upper()
                    if "ERROR" in e or "TIMEOUT" in e:
                        row_has_failure = True
                        break
                if fin_status and fin_status != "ok":
                    row_has_failure = True
                if r.get("valuation_status") and r.get("valuation_status") != "ok":
                    row_has_failure = True

                if row_has_failure:
                    has_row_failure = True

                # Every row, including excluded rows, must identify all three sources.
                for src_key in ("financial_source", "valuation_source", "risk_source"):
                    if not is_valid_row_source(src_key, r.get(src_key), is_excluded=is_excluded):
                        facts_consistent = False
                        break
                if not facts_consistent:
                    break
                # Missing/error sources cannot substantiate facts already present in the row.
                if (
                    (
                        str(r.get("valuation_source") or "").strip().lower()
                        in ALLOWED_EXCLUDED_VALUATION_SOURCES
                        and r.get("pb") not in (None, "")
                    )
                    or (
                        str(r.get("financial_source") or "").strip().lower()
                        in ALLOWED_EXCLUDED_FINANCIAL_SOURCES
                        and (
                            r.get("annual_roes") not in (None, []) or r.get("roe_mean") is not None
                        )
                    )
                    or (
                        str(r.get("risk_source") or "").strip().lower()
                        in ALLOWED_EXCLUDED_RISK_SOURCES
                        and (
                            r.get("risk_status") not in (None, "")
                            or bool(str(r.get("name") or "").strip())
                        )
                    )
                ):
                    facts_consistent = False
                    break

                # Valuation date on row: must match snapshot data_date
                val_date = r.get("valuation_date")
                val_date_ok = (
                    isinstance(val_date, str)
                    and val_date == valuation_date
                    and bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", val_date))
                )

                pb_val = r.get("pb")
                if pb_val is not None and not _is_finite_number(pb_val):
                    facts_consistent = False
                    break
                if (pb_val is None or pb_val <= 0) != ("INVALID_PB" in exclusions):
                    facts_consistent = False
                    break
                pb_ok = _is_finite_number(pb_val)
                val_source_ok = is_valid_row_source(
                    "valuation_source", r.get("valuation_source"), is_excluded=False
                )
                fin_source_ok = is_valid_row_source(
                    "financial_source", r.get("financial_source"), is_excluded=False
                )
                risk_source_ok = is_valid_row_source(
                    "risk_source", r.get("risk_source"), is_excluded=False
                )

                roes = r.get("annual_roes")
                if (r.get("roe_mean") is not None and not _is_finite_number(r["roe_mean"])) or (
                    isinstance(roes, list)
                    and any(
                        isinstance(item, dict)
                        and any(
                            item.get(key) is not None and not _is_finite_number(item[key])
                            for key in ("roe", "roe_waa")
                        )
                        for item in roes
                    )
                ):
                    facts_consistent = False
                    break
                roes_list_ok = isinstance(roes, list) and len(roes) == 3
                if (
                    isinstance(roes, list)
                    and not roes_list_ok
                    and any(
                        not isinstance(item, dict)
                        or not is_valid_row_source(
                            "financial_source", item.get("source"), is_excluded=False
                        )
                        for item in roes
                    )
                ):
                    facts_consistent = False
                    break

                # Parse and verify the 3 annual periods
                roes_period_ok = False
                parsed_dates: list[date] = []
                if roes_list_ok and isinstance(roes, list):
                    all_items_valid = True
                    for item in roes:
                        if not isinstance(item, dict) or not is_valid_row_source(
                            "financial_source", item.get("source"), is_excluded=False
                        ):
                            all_items_valid = False
                            facts_consistent = False
                            break
                        p_date: date | None = None
                        if isinstance(item.get("year"), int) and not isinstance(
                            item.get("year"), bool
                        ):
                            y = item["year"]
                            try:
                                p_date = date(y, 12, 31)
                            except (ValueError, OverflowError):
                                all_items_valid = False
                                break
                        p_raw = item.get("period") or item.get("end_date")
                        if p_raw is not None:
                            parsed_p = _parse_report_date(p_raw)
                            # Must strictly be December 31st (annual report period)
                            if parsed_p is None or parsed_p.month != 12 or parsed_p.day != 31:
                                all_items_valid = False
                                break
                            if p_date is not None and p_date != parsed_p:
                                all_items_valid = False
                                break
                            p_date = parsed_p

                        val = (
                            item.get("roe_waa")
                            if item.get("roe_waa") is not None
                            else item.get("roe")
                        )
                        ann = item.get("ann_date")
                        ann_date_obj = _parse_report_date(ann)
                        ann_ok = (
                            ann_date_obj is not None
                            and p_date is not None
                            and ann_date_obj >= p_date
                            and (screened_date is None or ann_date_obj <= screened_date)
                        )
                        if p_date is None or not _is_finite_number(val) or not ann_ok:
                            all_items_valid = False
                            break
                        parsed_dates.append(p_date)

                    if all_items_valid and len(parsed_dates) == 3:
                        s_dates = sorted(parsed_dates)
                        s_years = [d.year for d in s_dates]
                        # Consecutive 3 annual periods
                        if s_years == [s_years[0], s_years[0] + 1, s_years[0] + 2]:
                            # Age within 550 days of valuation_date (using real calendar dates)
                            try:
                                v_date = _parse_report_date(valuation_date)
                                if v_date is not None:
                                    diff_days = (v_date - s_dates[-1]).days
                                    if 0 <= diff_days <= 550:
                                        roes_period_ok = True
                            except Exception:
                                pass

                roes_ok = roes_list_ok and roes_period_ok
                no_exclusions = len(exclusions) == 0

                # A row can only be facts_usable if:
                # 1. pb_ok, val_date_ok, val_source_ok
                # 2. roes_ok, fin_source_ok
                # 3. risk_source_ok (business exclusions affect ranking, not fact usability)
                expected_usable = (
                    pb_ok
                    and val_date_ok
                    and val_source_ok
                    and roes_ok
                    and fin_source_ok
                    and risk_source_ok
                )

                if "facts_usable" in r and r.get("facts_usable") is not expected_usable:
                    facts_consistent = False
                    break
                if no_exclusions and not (val_source_ok and fin_source_ok and risk_source_ok):
                    facts_consistent = False
                    break

                # Only verified business exclusions can explain missing required facts.
                business_excluded = (
                    (
                        "KNOWN_ST_WARNING" in exclusions
                        and str(r.get("name") or "").strip().upper().startswith(("ST", "*ST"))
                    )
                    or (
                        "INVALID_PB" in exclusions
                        and isinstance(pb_val, (int, float))
                        and pb_val <= 0
                    )
                    or (
                        "NON_POSITIVE_ROE_MEAN" in exclusions
                        and roes_ok
                        and isinstance(r.get("roe_mean"), (int, float))
                        and r["roe_mean"] <= 0
                        and isinstance(roes, list)
                        and abs(
                            r["roe_mean"]
                            - sum(
                                float(
                                    item.get("roe_waa")
                                    if item.get("roe_waa") is not None
                                    else item["roe"]
                                )
                                for item in roes
                            )
                            / 3.0
                        )
                        <= 0.05
                    )
                )
                invalid_pb_explained = (
                    "INVALID_PB" in exclusions and isinstance(pb_val, (int, float)) and pb_val <= 0
                )
                # A non-positive mean is only proven by valid annual ROEs above;
                # an ST label or skipped financial fetch cannot explain missing reports.
                if (
                    (not expected_usable and not business_excluded)
                    or (
                        not pb_ok
                        and not invalid_pb_explained
                        and r.get("valuation_source") != "tushare.daily_basic:error"
                    )
                    or (not roes_ok and r.get("financial_source") != "tushare.fina_indicator:error")
                ):
                    all_rows_explained = False

                norm_roes = []
                if roes_ok and isinstance(roes, list):
                    for item in roes:
                        val = (
                            item.get("roe_waa")
                            if item.get("roe_waa") is not None
                            else item.get("roe")
                        )
                        norm_roes.append({**item, "roe_waa": val})

                if expected_usable:
                    expected_mean = sum(float(item["roe_waa"]) for item in norm_roes) / 3.0
                    actual_mean = r.get("roe_mean")
                    if (
                        not _is_finite_number(actual_mean)
                        or abs(float(actual_mean) - expected_mean) > 0.05
                    ):
                        facts_consistent = False
                        break

                normalized_rows.append(
                    {
                        **r,
                        "code": code_str,
                        "pb": pb_val,
                        "annual_roes": norm_roes,
                        "roe_mean": r.get("roe_mean"),
                        "exclusions": exclusions,
                    }
                )

            # 3. Use screen.rank_peers pure algorithm to recompute true ranking
            ranking_matched = False
            if unique_codes_ok and facts_consistent and len(normalized_rows) == len(rows):
                try:
                    import screen

                    watchlist = [
                        screen.base_code(str(c)) for c in (data.get("watchlist_codes") or [anchor])
                    ]
                    recomputed = screen.rank_peers(normalized_rows, anchor, watchlist)
                    exp_ranking = recomputed.get("ranking", [])

                    if len(exp_ranking) == len(ranking):
                        all_match = True
                        row_name_map = {
                            str(r.get("code") or r.get("ts_code")): r.get("name")
                            for r in rows
                            if isinstance(r, dict)
                        }
                        for exp, act in zip(exp_ranking, ranking):
                            act_code = str(act.get("code"))
                            if str(exp.get("code")) != act_code:
                                all_match = False
                                break
                            if "name" in act and act["name"] != row_name_map.get(act_code):
                                all_match = False
                                break
                            if exp.get("position") != act.get("position"):
                                all_match = False
                                break
                            exp_order = exp.get("research_order")
                            act_order = act.get("research_order")
                            if (
                                not isinstance(exp_order, (int, float))
                                or not isinstance(act_order, (int, float))
                                or abs(exp_order - act_order) > 0.01
                            ):
                                all_match = False
                                break
                            exp_roe_rk = exp.get("roe_rank")
                            act_roe_rk = act.get("roe_rank")
                            if (
                                not isinstance(exp_roe_rk, (int, float))
                                or not isinstance(act_roe_rk, (int, float))
                                or abs(exp_roe_rk - act_roe_rk) > 0.01
                            ):
                                all_match = False
                                break
                            exp_pb_rk = exp.get("pb_rank")
                            act_pb_rk = act.get("pb_rank")
                            if (
                                not isinstance(exp_pb_rk, (int, float))
                                or not isinstance(act_pb_rk, (int, float))
                                or abs(exp_pb_rk - act_pb_rk) > 0.01
                            ):
                                all_match = False
                                break
                        if all_match:
                            meta_ok = (
                                "qualified_codes" in results
                                and "top" in results
                                and "anchor_position" in results
                                and "outside_watchlist_qualified_count" in results
                                and results.get("qualified_codes")
                                == recomputed.get("qualified_codes")
                                and results.get("top") == recomputed.get("top")
                                and results.get("anchor_position")
                                == recomputed.get("anchor_position")
                                and results.get("outside_watchlist_qualified_count")
                                == recomputed.get("outside_watchlist_qualified_count")
                            )
                            if meta_ok:
                                ranking_matched = True
                except Exception:
                    ranking_matched = False

            is_verified_partial = False
            if unique_codes_ok and facts_consistent and ranking_matched:
                if (
                    not has_row_failure
                    and all_rows_explained
                    and results.get("discovery_complete") is True
                    and recomputed.get("discovery_complete") is True
                ):
                    is_verified_complete = True
                else:
                    is_verified_partial = True

        if has_replay or rule_id != "peer-screen-v1":
            health: Literal["complete", "partial", "unverified"] = "unverified"
        elif is_verified_complete:
            health = "complete"
        elif is_verified_partial:
            health = "partial"
        else:
            health = "unverified"

        run_id = f"peer_{anchor.replace('.', '_')}_{sha256[:12]}"

        # Copy raw bytes into snapshots/imports/<sha256>/snapshot.json atomically
        rel_dir = Path("snapshots") / "imports" / sha256
        target_dir = state_dir / rel_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        target_file = target_dir / "snapshot.json"

        if target_file.exists():
            existing_bytes = target_file.read_bytes()
            if hashlib.sha256(existing_bytes).hexdigest() != sha256:
                raise WorkspaceError("corrupted snapshot file already exists in repository")
        else:
            tmp_file = target_dir / f"snapshot.tmp.{os.getpid()}"
            tmp_file.write_bytes(raw_bytes)
            os.chmod(tmp_file, 0o600)
            os.replace(tmp_file, target_file)

        register_run(
            conn,
            run_id=run_id,
            kind="peer",
            anchor_code=anchor,
            rule_id=rule_id,
            captured_at=captured_at,
            valuation_date=valuation_date,
            health=health,
            snapshot_path=(rel_dir / "snapshot.json").as_posix(),
            snapshot_bytes=raw_bytes,
        )
        return run_id
    finally:
        conn.close()


def add_watch_item(
    conn: sqlite3.Connection,
    *,
    code: str,
    name: str,
    added_run_id: str,
    anchor_code: str | None = None,
) -> bool:
    if not re.fullmatch(r"\d{6}\.(?:SH|SZ|BJ)", code):
        raise WorkspaceError("invalid stock code")
    cursor = conn.execute(
        """INSERT INTO watch_items(code,name,anchor_code,status,added_run_id,updated_at)
        VALUES (?, ?, ?, 'observe', ?, ?) ON CONFLICT(code) DO NOTHING""",
        (code, name.strip(), anchor_code, added_run_id, utc_now()),
    )
    return cursor.rowcount == 1


def save_watch_item(
    conn: sqlite3.Connection,
    *,
    code: str,
    expected_revision: int,
    status: Literal["research", "observe", "paused"],
    reason: str,
    next_check: str = "",
    note_url: str | None = None,
) -> int:
    if status not in ("research", "observe", "paused"):
        raise WorkspaceError("invalid personal status")
    if len(reason) > 1000 or len(next_check) > 1000:
        raise WorkspaceError("personal text is too long")
    if note_url is not None:
        if len(note_url) > 2048:
            raise WorkspaceError("note URL is too long")
        try:
            parsed = urllib.parse.urlparse(note_url)
            if parsed.scheme != "https" or not parsed.netloc:
                raise WorkspaceError("note URL must use https")
            if parsed.username is not None or parsed.password is not None:
                raise WorkspaceError("note URL cannot contain credentials")
        except WorkspaceError:
            raise
        except Exception as exc:
            raise WorkspaceError("invalid note URL") from exc

    cursor = conn.execute(
        """UPDATE watch_items SET status=?,reason=?,next_check=?,note_url=?,
        updated_at=?,revision=revision+1 WHERE code=? AND revision=?""",
        (status, reason, next_check, note_url, utc_now(), code, expected_revision),
    )
    if cursor.rowcount != 1:
        raise WorkspaceError("record missing or revision conflict")
    return expected_revision + 1


def watch_run_targets(conn: sqlite3.Connection, run_id: str) -> set[str] | None:
    """Return explicit watch job targets, or None when they are unknown."""
    job = conn.execute(
        "SELECT payload_json FROM update_jobs WHERE result_run_id=? OR job_id=?",
        (run_id, run_id),
    ).fetchone()
    if not job:
        return None
    try:
        payload = json.loads(job["payload_json"])
        targets = [
            code
            for key in ("codes", "expected_codes")
            for code in (payload.get(key) if isinstance(payload.get(key), list) else [])
            if isinstance(code, str)
        ]
        return set(targets) if targets else None
    except (ValueError, TypeError, AttributeError):
        return None


def mark_watch_ack(
    conn: sqlite3.Connection,
    *,
    code: str,
    displayed_run_id: str,
    expected_revision: int,
    state_dir: Path,
) -> int:
    """Advance ack_run_id to displayed_run_id if valid; idempotent if already acknowledged."""
    item = conn.execute("SELECT * FROM watch_items WHERE code=?", (code,)).fetchone()
    if not item:
        raise WorkspaceError("watch item not found")
    already_acknowledged = item["ack_run_id"] == displayed_run_id
    if not already_acknowledged and item["revision"] != expected_revision:
        raise WorkspaceError("revision conflict")

    run = conn.execute("SELECT * FROM screen_runs WHERE run_id=?", (displayed_run_id,)).fetchone()
    if not run:
        raise WorkspaceError("displayed run not found")
    if run["health"] == "unverified":
        raise WorkspaceError("cannot acknowledge unverified run")

    # Monotonic check: displayed run cannot be older than current ack run
    if item["ack_run_id"]:
        current_ack = conn.execute(
            "SELECT * FROM screen_runs WHERE run_id=?", (item["ack_run_id"],)
        ).fetchone()
        if current_ack and (run["captured_at"], run["run_id"]) < (
            current_ack["captured_at"],
            current_ack["run_id"],
        ):
            raise WorkspaceError("cannot acknowledge an older run than current ack")
        if (
            current_ack
            and run["valuation_date"]
            and current_ack["valuation_date"]
            and run["valuation_date"] < current_ack["valuation_date"]
        ):
            raise WorkspaceError("cannot acknowledge a regressed valuation date")

    state_dir = state_dir.expanduser().resolve()

    def row_in_run(candidate: sqlite3.Row) -> tuple[dict[str, Any] | None, bool]:
        snap_path = (state_dir / candidate["snapshot_path"]).resolve()
        try:
            snap_path.relative_to(state_dir)
        except ValueError as exc:
            raise WorkspaceError(
                f"snapshot path traversal detected: {candidate['snapshot_path']}"
            ) from exc
        if not snap_path.is_file():
            raise WorkspaceError(f"snapshot file missing: {candidate['snapshot_path']}")
        try:
            snap_bytes = snap_path.read_bytes()
        except OSError as exc:
            raise WorkspaceError(
                f"failed to read snapshot file {candidate['snapshot_path']}: {exc}"
            ) from exc
        if hashlib.sha256(snap_bytes).hexdigest() != candidate["snapshot_sha256"]:
            raise WorkspaceError(f"snapshot hash mismatch: {candidate['snapshot_path']}")
        try:
            data = json.loads(snap_bytes.decode("utf-8"))
        except Exception as exc:
            raise WorkspaceError("failed to parse snapshot JSON") from exc
        if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
            raise WorkspaceError("invalid snapshot rows")
        matched = next(
            (r for r in data["rows"] if isinstance(r, dict) and r.get("code") == code), None
        )
        scope = data.get("scope")
        if not isinstance(scope, dict):
            scope = {}
        if candidate["kind"] == "watch":
            meta = data.get("workspace_meta")
            covered = isinstance(meta, dict) and code in (meta.get("expected_codes") or [])
        else:
            covered = data.get("anchor") == code or code in (scope.get("selected_codes") or [])
        return matched, covered or matched is not None

    matched_row, _ = row_in_run(run)
    if not matched_row:
        raise WorkspaceError(f"displayed run does not contain stock {code}")
    if not is_row_usable(matched_row):
        raise WorkspaceError(f"stock {code} does not have usable facts in displayed run")

    item_anchor = item["anchor_code"]
    if not item_anchor and item["added_run_id"]:
        added = conn.execute(
            "SELECT anchor_code FROM screen_runs WHERE run_id=?", (item["added_run_id"],)
        ).fetchone()
        if added:
            item_anchor = added["anchor_code"]

    for older in conn.execute(
        """SELECT * FROM screen_runs
        WHERE (captured_at < ? OR (captured_at = ? AND run_id < ?))
        AND health IN ('complete', 'partial')
        ORDER BY captured_at DESC, run_id DESC""",
        (run["captured_at"], run["captured_at"], run["run_id"]),
    ):
        if (
            older["valuation_date"]
            and run["valuation_date"]
            and (older["valuation_date"] > run["valuation_date"])
        ):
            try:
                older_row, _ = row_in_run(older)
            except WorkspaceError:
                continue
            if older_row and is_row_usable(older_row):
                raise WorkspaceError("cannot acknowledge a regressed valuation date")

    for newer in conn.execute(
        """SELECT * FROM screen_runs
        WHERE (captured_at > ? OR (captured_at = ? AND run_id > ?))
        AND health IN ('complete', 'partial')
        ORDER BY captured_at DESC, run_id DESC""",
        (run["captured_at"], run["captured_at"], run["run_id"]),
    ):
        peer_relevant = newer["kind"] == "peer" and (
            newer["anchor_code"] == code or (item_anchor and newer["anchor_code"] == item_anchor)
        )
        targets = watch_run_targets(conn, newer["run_id"]) if newer["kind"] == "watch" else None
        watch_relevant = targets is not None and code in targets
        # Even an unrelated anchor/target may contain this stock as a peer.
        # Metadata only decides what to do if the snapshot cannot be read.
        try:
            newer_row, covered = row_in_run(newer)
        except WorkspaceError as exc:
            if peer_relevant or watch_relevant:
                raise WorkspaceError(
                    "cannot acknowledge stale run when newer run has data gap"
                ) from exc
            continue
        if covered and (
            not newer_row
            or not is_row_usable(newer_row)
            or (
                newer["valuation_date"]
                and run["valuation_date"]
                and newer["valuation_date"] < run["valuation_date"]
            )
        ):
            raise WorkspaceError("cannot acknowledge stale run when newer run has data gap")

    # A retry of the old acknowledgement must still check newly arrived data gaps.
    if already_acknowledged:
        return int(item["revision"])

    cursor = conn.execute(
        """UPDATE watch_items SET ack_run_id=?,ack_at=?,updated_at=?,revision=revision+1
        WHERE code=? AND revision=?""",
        (displayed_run_id, utc_now(), utc_now(), code, expected_revision),
    )
    if cursor.rowcount != 1:
        raise WorkspaceError("revision conflict during ack")
    return expected_revision + 1


def get_watch_item(conn: sqlite3.Connection, code: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM watch_items WHERE code=?", (code,)).fetchone()
    return dict(row) if row else None


def list_watch_items(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute("SELECT * FROM watch_items ORDER BY name,code")]


def get_run(conn: sqlite3.Connection, run_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM screen_runs WHERE run_id=?", (run_id,)).fetchone()
    return dict(row) if row else None


def get_latest_peer_run(conn: sqlite3.Connection, anchor_code: str) -> dict[str, Any] | None:
    row = conn.execute(
        """SELECT * FROM screen_runs
        WHERE kind='peer' AND anchor_code=? AND health='complete'
        ORDER BY valuation_date DESC, captured_at DESC, run_id DESC LIMIT 1""",
        (anchor_code,),
    ).fetchone()
    return dict(row) if row else None


def list_runs(conn: sqlite3.Connection, kind: str | None = None) -> list[dict[str, Any]]:
    if kind:
        rows = conn.execute(
            "SELECT * FROM screen_runs WHERE kind=? ORDER BY captured_at DESC, run_id DESC", (kind,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM screen_runs ORDER BY captured_at DESC, run_id DESC"
        ).fetchall()
    return [dict(row) for row in rows]
