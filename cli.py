"""Operator CLI for the GitHub ticket-flow plugin."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys

try:
    from . import ticket_flow as core
except ImportError:  # Direct execution in the plugin test suite.
    import ticket_flow as core


def register_cli(parser: argparse.ArgumentParser) -> None:
    subs = parser.add_subparsers(dest="ticket_flow_action")

    start = subs.add_parser("start", help="Create and dispatch a ticket workflow")
    start.add_argument("issues", nargs="+", type=int, help="Ordered GitHub issue numbers")
    start.add_argument("--config", help="Optional repository override YAML/JSON")
    start.add_argument("--board", help="Override generated Kanban board slug")
    start.add_argument("--workflow-id", help="Override generated workflow identity")
    start.add_argument("--research-ahead", type=int, help="Number of future tickets research may lead")
    start.add_argument("--auto-merge", action="store_true", help="Skip the user checkpoint and merge after review and required checks")
    start.add_argument("--dry-run", action="store_true", help="Validate and print the plan without writing state")
    start.add_argument("--no-dispatch", action="store_true", help="Create the board without spawning ready workers")
    start.add_argument("--json", action="store_true", help="Print machine-readable JSON")

    status = subs.add_parser("status", help="Show ticket-flow boards for this repository")
    status.add_argument("--board", help="Show one board")
    status.add_argument("--json", action="store_true", help="Print machine-readable JSON")

    detach = subs.add_parser("detach", help="Archive a completed ticket-flow board")
    detach.add_argument("--board", required=True, help="Board slug to archive")
    detach.add_argument("--yes", action="store_true", help="Confirm archival")
    detach.add_argument("--json", action="store_true", help="Print machine-readable JSON")

    analyst_pass = subs.add_parser("analyst-pass", help="Run an analyst without the caller's Kanban identity")
    analyst_pass.add_argument("--profile", required=True, help="Read-only analyst profile")
    analyst_pass.add_argument("--worktree", required=True, help="Exact candidate worktree")
    analyst_pass.add_argument("--query-file", required=True, help="Analyst instructions file")
    analyst_pass.add_argument("--max-turns", type=int, default=40, help="Analyst turn budget")

    parser.set_defaults(func=ticket_flow_command)


def _print(payload, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2))
        return
    if isinstance(payload, str):
        print(payload)
        return
    print(json.dumps(payload, indent=2))


def _start(args: argparse.Namespace) -> int:
    project, repository = core.discover_project()
    config_path = pathlib.Path(args.config) if args.config else pathlib.Path(project) / ".hermes" / "ticket-flow.yaml"
    overrides = core.load_optional_config(config_path)
    if args.research_ahead is not None:
        overrides = core._deep_merge(overrides, {"pipeline": {"research_ahead": args.research_ahead}})
    if args.auto_merge:
        overrides = core._deep_merge(overrides, {"pipeline": {"require_user_merge_confirmation": False}})
    config = core.effective_config(
        project,
        repository,
        args.issues,
        overrides,
        board=args.board,
        workflow_id=args.workflow_id,
    )
    issues = core.fetch_issues(config)
    plan = core.visible_plan(config, issues)
    if args.dry_run:
        _print({"dry_run": True, "effective_config": config, "plan": plan}, args.json)
        return 0

    cards = core.apply_plan(config, issues)
    manifest = core.build_manifest(config, cards)
    manifest_path = core.persist_manifest(config, manifest)
    dispatch = None
    if not args.no_dispatch:
        dispatch = json.loads(
            core.run(["hermes", "kanban", "--board", config["board"], "dispatch", "--json"]).stdout
        )
    _print(
        {
            "created": True,
            "board": config["board"],
            "workflow_id": config["workflow_id"],
            "manifest": str(manifest_path),
            "cards": cards,
            "dispatch": dispatch,
        },
        args.json,
    )
    return 0


def _status(args: argparse.Namespace) -> int:
    _, repository = core.discover_project()
    slug = core._repo_slug(repository)
    boards = json.loads(core.run(["hermes", "kanban", "boards", "list", "--json"]).stdout)
    selected = [board for board in boards if board["slug"] == args.board] if args.board else [
        board for board in boards if board["slug"].startswith(f"{slug}-ticket-flow-")
    ]
    result = []
    for board in selected:
        tasks = json.loads(
            core.run(["hermes", "kanban", "--board", board["slug"], "list", "--json"]).stdout
        )
        manifest_path = pathlib.Path(board["db_path"]).parent / "ticket-flow-manifest.json"
        result.append(
            {
                "board": board,
                "manifest": json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else None,
                "tasks": tasks,
            }
        )
    _print(result, args.json)
    return 0


def _detach(args: argparse.Namespace) -> int:
    if not args.yes:
        _print({"dry_run": True, "would_archive": args.board, "github_untouched": True}, args.json)
        return 0
    core.detach(args.board)
    _print({"archived_board": args.board, "github_untouched": True}, args.json)
    return 0


def _analyst_pass(args: argparse.Namespace) -> int:
    worktree = pathlib.Path(args.worktree).resolve()
    query_file = pathlib.Path(args.query_file).resolve()
    if not worktree.is_dir():
        raise ValueError(f"analyst worktree is not a directory: {worktree}")
    if not query_file.is_file():
        raise ValueError(f"analyst query file does not exist: {query_file}")
    if args.max_turns < 1:
        raise ValueError("analyst max-turns must be positive")

    env = {key: value for key, value in os.environ.items() if not key.startswith("HERMES_KANBAN_")}
    env["HERMES_DELEGATED_CHILD_CONTEXT"] = "1"
    completed = subprocess.run(
        [
            "hermes",
            "-p",
            args.profile,
            "--in",
            str(worktree),
            "chat",
            "--oneshot",
            "--query-file",
            str(query_file),
            "--max-turns",
            str(args.max_turns),
        ],
        check=True,
        text=True,
        capture_output=True,
        cwd=str(worktree),
        env=env,
    )
    print(completed.stdout, end="")
    return 0


def ticket_flow_command(args: argparse.Namespace) -> int:
    action = getattr(args, "ticket_flow_action", None)
    try:
        if action == "start":
            return _start(args)
        if action == "status":
            return _status(args)
        if action == "detach":
            return _detach(args)
        if action == "analyst-pass":
            return _analyst_pass(args)
        print("Usage: hermes ticket-flow {start|status|detach|analyst-pass}", file=sys.stderr)
        return 2
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) and exc.stderr else str(exc)
        print(f"ticket-flow error: {detail}", file=sys.stderr)
        return 1
