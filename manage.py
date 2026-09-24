"""Workspace management CLI for initialization, fixture importing, and demo setup."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from workspace import (
    WorkspaceError,
    connect_workspace,
    import_snapshot,
    initialize,
    list_runs,
    list_watch_items,
)

FIXTURES_DIR = Path(__file__).parent / "tests" / "fixtures"


def cmd_init(args: argparse.Namespace) -> int:
    try:
        path = initialize(args.state_dir, args.mode, args.journal_mode)
        print(f"Initialized {args.mode} workspace at {path}")
        return 0
    except WorkspaceError as exc:
        print(f"Initialization error: {exc}", file=sys.stderr)
        return 1


def cmd_import(args: argparse.Namespace) -> int:
    try:
        run_id = import_snapshot(args.state_dir, args.snapshot, args.mode)
        print(f"Imported snapshot as run_id: {run_id}")
        return 0
    except WorkspaceError as exc:
        print(f"Import error: {exc}", file=sys.stderr)
        return 1


def cmd_setup_demo(args: argparse.Namespace) -> int:
    """Idempotently initialize demo workspace and import test fixtures."""
    state_dir: Path = args.state_dir
    print(f"Setting up demo workspace in {state_dir}...")
    try:
        initialize(state_dir, mode="demo", journal_mode=args.journal_mode)
        fixtures = [
            FIXTURES_DIR / "peer_complete_v1.json",
            FIXTURES_DIR / "peer_second_change.json",
            FIXTURES_DIR / "peer_unverified_old_rule.json",
        ]
        for f in fixtures:
            if f.is_file():
                run_id = import_snapshot(state_dir, f, mode="demo")
                print(f"  Loaded fixture {f.name} -> {run_id}")
        print("Demo workspace ready.")
        return 0
    except WorkspaceError as exc:
        print(f"Demo setup failed: {exc}", file=sys.stderr)
        return 1


def cmd_list(args: argparse.Namespace) -> int:
    try:
        conn = connect_workspace(args.state_dir, args.mode)
        try:
            print("--- Screen Runs ---")
            for r in list_runs(conn):
                print(
                    f"[{r['kind']}] run_id={r['run_id']} anchor={r['anchor_code']} "
                    f"health={r['health']} date={r['valuation_date']}"
                )
            print("--- Watch Items ---")
            for w in list_watch_items(conn):
                print(
                    f"{w['code']} {w['name']} status={w['status']} rev={w['revision']} "
                    f"ack={w['ack_run_id']}"
                )
        finally:
            conn.close()
        return 0
    except WorkspaceError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="a-stock-screen workspace manager")
    sub = parser.add_subparsers(dest="command", required=True)

    # init
    p_init = sub.add_parser("init", help="Initialize workspace")
    p_init.add_argument("--state-dir", type=Path, default=Path(".local/demo"))
    p_init.add_argument("--mode", choices=("demo", "production"), default="demo")
    p_init.add_argument("--journal-mode", choices=("WAL", "DELETE"), default="WAL")
    p_init.set_defaults(func=cmd_init)

    # import
    p_import = sub.add_parser("import", help="Import a peer snapshot")
    p_import.add_argument("--state-dir", type=Path, default=Path(".local/demo"))
    p_import.add_argument("--mode", choices=("demo", "production"), default="demo")
    p_import.add_argument("--snapshot", type=Path, required=True)
    p_import.set_defaults(func=cmd_import)

    # demo-setup
    p_demo = sub.add_parser("setup-demo", help="Setup demo workspace with fixtures")
    p_demo.add_argument("--state-dir", type=Path, default=Path(".local/demo"))
    p_demo.add_argument("--journal-mode", choices=("WAL", "DELETE"), default="WAL")
    p_demo.set_defaults(func=cmd_setup_demo)

    # list
    p_list = sub.add_parser("list", help="List runs and watch items")
    p_list.add_argument("--state-dir", type=Path, default=Path(".local/demo"))
    p_list.add_argument("--mode", choices=("demo", "production"), default="demo")
    p_list.set_defaults(func=cmd_list)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
