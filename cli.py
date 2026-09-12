"""Operator CLI for the GitHub ticket-flow plugin."""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys

try:
    from . import ticket_flow as core
    from . import herdr_runner
except ImportError:  # Direct execution in the plugin test suite.
    import ticket_flow as core
    import herdr_runner


def register_cli(parser: argparse.ArgumentParser) -> None:
    subs = parser.add_subparsers(dest="ticket_flow_action")

    start = subs.add_parser("start", help="Create and dispatch a ticket workflow")
    start.add_argument("issues", nargs="+", type=int, help="Ordered GitHub issue numbers")
    start.add_argument("--config", help="Optional repository override YAML/JSON")
    start.add_argument("--board", help="Override generated Kanban board slug")
    start.add_argument("--workflow-id", help="Override generated workflow identity")
    start.add_argument("--research-ahead", type=int, help="Number of future tickets research may lead")
    start.add_argument("--runner", choices=("hermes", "herdr"), help="Override the configured Kanban worker runner")
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

    run_herdr = subs.add_parser("herdr-run", help="Dispatch this board's Kanban workers into visible Herdr workspaces")
    run_herdr.add_argument("--board", required=True, help="Kanban board slug")
    run_herdr.add_argument("--once", action="store_true", help="Run one dispatch pass and exit")
    run_herdr.add_argument("--interval", type=float, default=2.0, help="Seconds between dispatch passes")
    run_herdr.add_argument("--max-spawn", type=int, default=4, help="Maximum workers to start per pass")

    delegate = subs.add_parser("herdr-delegate", help="Run read-only evidence lenses in visible Herdr tabs")
    delegate.add_argument("--spec", required=True, help="JSON delegation spec path")
    delegate.add_argument("--json", action="store_true", help="Print machine-readable JSON")

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
    if args.runner:
        overrides = core._deep_merge(overrides, {"execution": {"runner": args.runner}})
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

    use_herdr = config["execution"]["runner"] == "herdr" and not args.no_dispatch
    if use_herdr:
        herdr_runner.require_herdr_environment()
        if not herdr_runner.gateway_dispatch_is_disabled():
            raise RuntimeError(
                "Herdr execution requires kanban.dispatch_in_gateway=false and a restarted gateway"
            )

    cards = core.apply_plan(config, issues)
    manifest = core.build_manifest(config, cards)
    manifest_path = core.persist_manifest(config, manifest)
    dispatch = None
    if not args.no_dispatch:
        if use_herdr:
            dispatch = {
                "runner": "herdr",
                **herdr_runner.launch_dispatcher_tab(
                    config["board"],
                    project_path=config["project_path"],
                    interval_seconds=config["execution"]["poll_interval_seconds"],
                    max_spawn=config["execution"]["max_spawn"],
                ),
            }
        else:
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


def _herdr_run(args: argparse.Namespace) -> int:
    herdr_runner.run_dispatcher(
        args.board,
        once=args.once,
        interval_seconds=args.interval,
        max_spawn=args.max_spawn,
    )
    return 0


def _herdr_delegate(args: argparse.Namespace) -> int:
    result = herdr_runner.run_delegate_spec(args.spec)
    _print(result, args.json)
    return 0 if result.get("ok") else 1


def ticket_flow_command(args: argparse.Namespace) -> int:
    action = getattr(args, "ticket_flow_action", None)
    try:
        if action == "start":
            return _start(args)
        if action == "status":
            return _status(args)
        if action == "detach":
            return _detach(args)
        if action == "herdr-run":
            return _herdr_run(args)
        if action == "herdr-delegate":
            return _herdr_delegate(args)
        print("Usage: hermes ticket-flow {start|status|detach|herdr-run|herdr-delegate}", file=sys.stderr)
        return 2
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) and exc.stderr else str(exc)
        print(f"ticket-flow error: {detail}", file=sys.stderr)
        return 1
