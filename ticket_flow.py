#!/usr/bin/env python3
"""Deterministic GitHub issue to Hermes Kanban compiler."""

from __future__ import annotations

import copy
import datetime as dt
import json
import pathlib
import re
import subprocess
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - surfaced when YAML is requested
    yaml = None

PLUGIN_SKILL = "github-ticket-flow:workflow"
STAGES = ("research", "delivery", "merge")
DEFAULTS: dict[str, Any] = {
    "roles": {
        "analyst": "analyst",
        "researcher": "researcher",
        "implementer": "implementer",
        "reviewer": "reviewer",
        "merge_verifier": "default",
    },
    "pipeline": {
        "research_ahead": 1,
        "require_previous_merge": True,
        "require_user_merge_confirmation": True,
    },
    "worktrees": {"branch_template": "agent/issue-{issue}"},
    "stage_skills": {"research": [], "delivery": ["ponytail"], "merge": []},
    "gates": {"final": ["hermes verify --json"]},
}
ALLOWED_OVERRIDE_KEYS = {"roles", "pipeline", "worktrees", "stage_skills", "gates"}


def run(argv: list[str], *, cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=cwd, check=True, text=True, capture_output=True)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _repo_slug(repository: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", repository.split("/", 1)[-1].lower()).strip("-")


def effective_config(
    project_path: str,
    repository: str,
    issues: list[int],
    overrides: dict[str, Any],
    *,
    board: str | None = None,
    workflow_id: str | None = None,
) -> dict[str, Any]:
    unknown = set(overrides) - ALLOWED_OVERRIDE_KEYS
    if unknown:
        raise ValueError(f"unsupported repository override(s): {', '.join(sorted(unknown))}")
    issue_suffix = "-".join(str(number) for number in issues)
    slug = _repo_slug(repository)
    config = _deep_merge(DEFAULTS, overrides)
    config.update(
        {
            "api_version": 1,
            "kind": "github-ticket-flow",
            "enabled": True,
            "workflow_id": workflow_id or f"{slug}-{issue_suffix}",
            "board": board or f"{slug}-ticket-flow-{issue_suffix}",
            "project_path": str(pathlib.Path(project_path).resolve()),
            "github": {"repository": repository, "source_of_truth": "github"},
            "issues": list(issues),
        }
    )
    validate_config(config)
    return config


def validate_config(config: dict[str, Any]) -> None:
    issues = config.get("issues")
    if not isinstance(issues, list) or not issues or not all(isinstance(n, int) and n > 0 for n in issues):
        raise ValueError("issues must be a non-empty list of positive integers")
    if len(set(issues)) != len(issues):
        raise ValueError("issues must be unique")
    project = pathlib.Path(str(config.get("project_path", "")))
    if not project.is_absolute():
        raise ValueError("project_path must be absolute")
    repo = config.get("github", {}).get("repository")
    if not isinstance(repo, str) or repo.count("/") != 1:
        raise ValueError("repository must be owner/repo")
    roles = config.get("roles", {})
    for role in ("analyst", "researcher", "implementer", "reviewer", "merge_verifier"):
        if not isinstance(roles.get(role), str) or not roles[role].strip():
            raise ValueError(f"roles.{role} must be a profile name")
    research_ahead = config.get("pipeline", {}).get("research_ahead", 1)
    if isinstance(research_ahead, bool) or not isinstance(research_ahead, int) or research_ahead < 0:
        raise ValueError("pipeline.research_ahead must be a non-negative integer")
    require_user_merge_confirmation = config.get("pipeline", {}).get("require_user_merge_confirmation", True)
    if not isinstance(require_user_merge_confirmation, bool):
        raise ValueError("pipeline.require_user_merge_confirmation must be a boolean")
    branch = config.get("worktrees", {}).get("branch_template", "")
    if "{issue}" not in branch:
        raise ValueError("worktrees.branch_template must contain {issue}")
    for stage, skills in config.get("stage_skills", {}).items():
        if stage not in STAGES or not isinstance(skills, list) or not all(isinstance(s, str) and s for s in skills):
            raise ValueError(f"invalid stage_skills.{stage}")
    gates = config.get("gates", {}).get("final", [])
    if not isinstance(gates, list) or not all(isinstance(gate, str) and gate.strip() for gate in gates):
        raise ValueError("gates.final must be a list of commands")


def load_optional_config(path: str | pathlib.Path) -> dict[str, Any]:
    config_path = pathlib.Path(path)
    if not config_path.exists():
        return {}
    text = config_path.read_text(encoding="utf-8")
    if config_path.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        if yaml is None:
            raise RuntimeError("YAML overrides require PyYAML")
        data = yaml.safe_load(text)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("repository ticket-flow config must be an object")
    return data


def _parse_github_remote(remote: str) -> str:
    value = remote.strip()
    match = re.search(r"github\.com[/:]([^/]+)/([^/]+?)(?:\.git)?$", value)
    if not match:
        raise ValueError("origin is not a GitHub owner/repo remote")
    return f"{match.group(1)}/{match.group(2)}"


def discover_project(cwd: str | None = None) -> tuple[str, str]:
    root = run(["git", "rev-parse", "--show-toplevel"], cwd=cwd).stdout.strip()
    remote = run(["git", "remote", "get-url", "origin"], cwd=root).stdout.strip()
    return root, _parse_github_remote(remote)


def fetch_issues(config: dict[str, Any]) -> dict[int, dict[str, Any]]:
    repository = config["github"]["repository"]
    records: dict[int, dict[str, Any]] = {}
    for number in config["issues"]:
        payload = json.loads(
            run(
                ["gh", "issue", "view", str(number), "--repo", repository, "--json", "number,title,url,state"],
                cwd=config["project_path"],
            ).stdout
        )
        if payload.get("state") != "OPEN":
            raise ValueError(f"GitHub issue #{number} is not open")
        records[number] = payload
    return records


def _stage_skills(config: dict[str, Any], stage: str) -> list[str]:
    extras = config.get("stage_skills", {}).get(stage, [])
    return list(dict.fromkeys([PLUGIN_SKILL, *extras]))


def _body(config: dict[str, Any], issue: dict[str, Any], stage: str) -> str:
    common = (
        f"Workflow: {config['workflow_id']}\n"
        f"GitHub issue: {issue['url']}\n"
        f"Repository: {config['github']['repository']}\n"
        f"Issue #{issue['number']}: {issue['title']}\n\n"
        f"Load and follow `{PLUGIN_SKILL}`. Call `kanban_show` first. Use parent handoffs and terminate through Kanban lifecycle tools.\n\n"
    )
    if stage == "research":
        return common + (
            "Stage: RESEARCH. Read-only: do not edit files, create branches, commit, push, or open a PR. "
            "Inspect the current issue and comments, linked PRs, origin/main, owner symbols, callers, tests, and project rules. "
            "Produce an evidence-backed implementation context packet with path-and-line citations. Separate verified facts, hypotheses, "
            "open questions, risks, and suggested tests. Complete with metadata matching `references/research-handoff.schema.json`, "
            "including the exact researched base SHA."
        )
    if stage == "delivery":
        analyst = config["roles"]["analyst"]
        reviewer = config["roles"]["reviewer"]
        return common + (
            "Stage: DELIVERY_WITH_ANALYSIS_AND_REVIEW. You are the sole writer. Validate the research parent against current origin/main, "
            "follow TDD, and apply Ponytail full for the smallest correct diff without weakening requirements or safety. Keep the candidate "
            f"uncommitted and compute the deterministic diff digest. Before review, launch a read-only `{analyst} chat` pass in the exact "
            "worktree, giving it the issue, expected digest, changed files, and tests. Require it to recompute the digest before and after "
            "inspection and return advisory metadata matching `references/diff-analysis-handoff.schema.json`: changed behavior, owner/caller "
            "impact, tests, scope surprises, potential omissions, and review hotspots with path-and-line evidence. The analyst must not approve "
            f"or reject. If the analyst fails or the digest moves, block with the concrete error; do not skip the pass. Then request same-card review from `{reviewer}` "
            "with the worktree, digest, test evidence, and complete analyst advisory. The reviewer is read-only and independently verifies the "
            "issue, research packet, diff, tests, and digest; the analyst advisory is non-authoritative. Any edit invalidates both analysis and "
            "approval and requires a fresh analyst pass before re-review. Approval must preserve worktree and digest metadata."
        )
    gates = "\n".join(f"- `{gate}`" for gate in config.get("gates", {}).get("final", []))
    if config["pipeline"]["require_user_merge_confirmation"]:
        merge_policy = (
            "then block for user merge confirmation. Put the PR URL in block metadata/summary, never an ordinary comment. "
            "After unblocking, independently verify the expected head merged"
        )
    else:
        merge_policy = (
            "then merge automatically only after required GitHub checks pass, the expected head and approved digest remain unchanged, "
            "and reviewer approval is still valid. Use a repository-permitted merge method and independently verify the expected head merged"
        )
    return common + (
        "Stage: FINALIZE_AND_MERGE. Verify the approved digest, run every final gate below, commit atomically, push, open and read back "
        f"a PR containing `Closes #N`, {merge_policy}, synchronize clean main, reap the worktree and branches, "
        "and complete with `references/merge-handoff.schema.json`. Do not release the next delivery before this.\n\nFinal gates:\n"
        + gates
    )


def build_task_specs(config: dict[str, Any], issues: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    path = config["project_path"]
    repository = config["github"]["repository"]
    workflow = config["workflow_id"]
    numbers = config["issues"]
    research_ahead = config["pipeline"]["research_ahead"]
    branch_template = config["worktrees"]["branch_template"]
    tasks: list[dict[str, Any]] = []
    previous_merge: str | None = None
    for index, number in enumerate(numbers):
        issue = issues[number]
        research_key = f"research-{number}"
        delivery_key = f"delivery-{number}"
        merge_key = f"merge-{number}"
        research_parent_index = index - research_ahead - 1
        research_parents = [f"merge-{numbers[research_parent_index]}"] if research_parent_index >= 0 else []
        prefix = f"ticket-flow:{workflow}:{repository}:{number}"
        tasks.extend(
            [
                {
                    "key": research_key,
                    "stage": "research",
                    "title": f"Research GitHub #{number}: {issue['title']}",
                    "body": _body(config, issue, "research"),
                    "assignee": config["roles"]["analyst"],
                    "parents": research_parents,
                    "workspace": f"dir:{path}",
                    "branch": None,
                    "skills": _stage_skills(config, "research"),
                    "idempotency_key": f"{prefix}:research",
                },
                {
                    "key": delivery_key,
                    "stage": "delivery",
                    "title": f"Deliver GitHub #{number}: {issue['title']}",
                    "body": _body(config, issue, "delivery"),
                    "assignee": config["roles"]["implementer"],
                    "parents": [research_key] + ([previous_merge] if previous_merge else []),
                    "workspace": "worktree",
                    "branch": branch_template.format(issue=number),
                    "skills": _stage_skills(config, "delivery"),
                    "idempotency_key": f"{prefix}:delivery",
                },
                {
                    "key": merge_key,
                    "stage": "merge",
                    "title": f"Finalize and verify merge for GitHub #{number}",
                    "body": _body(config, issue, "merge"),
                    "assignee": config["roles"]["merge_verifier"],
                    "parents": [delivery_key],
                    "workspace": f"dir:{path}",
                    "branch": None,
                    "skills": _stage_skills(config, "merge"),
                    "idempotency_key": f"{prefix}:merge",
                },
            ]
        )
        previous_merge = merge_key
    return tasks


def visible_plan(config: dict[str, Any], issues: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: value for key, value in task.items() if key != "body"} for task in build_task_specs(config, issues)]


def ensure_board(config: dict[str, Any]) -> None:
    boards = {board["slug"] for board in json.loads(run(["hermes", "kanban", "boards", "list", "--json"]).stdout)}
    if config["board"] not in boards:
        run(
            [
                "hermes", "kanban", "boards", "create", config["board"],
                "--name", config["workflow_id"].replace("-", " ").title(),
                "--description", f"GitHub ticket-flow execution for {config['github']['repository']}",
                "--default-workdir", config["project_path"],
            ]
        )


def apply_plan(config: dict[str, Any], issues: dict[int, dict[str, Any]]) -> list[dict[str, str]]:
    ensure_board(config)
    ids: dict[str, str] = {}
    created: list[dict[str, str]] = []
    for task in build_task_specs(config, issues):
        argv = [
            "hermes", "kanban", "--board", config["board"], "create", task["title"],
            "--body", task["body"], "--assignee", task["assignee"],
            "--workspace", task["workspace"], "--idempotency-key", task["idempotency_key"],
            "--created-by", f"ticket-flow:{config['workflow_id']}",
        ]
        for skill in task["skills"]:
            argv.extend(["--skill", skill])
        for parent in task["parents"]:
            argv.extend(["--parent", ids[parent]])
        if task["branch"]:
            argv.extend(["--branch", task["branch"]])
        argv.append("--json")
        payload = json.loads(run(argv).stdout)
        task_id = payload.get("id") or payload.get("task_id")
        if not task_id:
            raise RuntimeError(f"Kanban did not return an id for {task['key']}")
        ids[task["key"]] = task_id
        created.append({"key": task["key"], "id": task_id})
    return created


def build_manifest(config: dict[str, Any], cards: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "manifest_version": 1,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "effective_config": copy.deepcopy(config),
        "cards": copy.deepcopy(cards),
    }


def persist_manifest(config: dict[str, Any], manifest: dict[str, Any]) -> pathlib.Path:
    boards = json.loads(run(["hermes", "kanban", "boards", "list", "--json"]).stdout)
    board = next((item for item in boards if item["slug"] == config["board"]), None)
    if not board:
        raise RuntimeError(f"board not found after creation: {config['board']}")
    path = pathlib.Path(board["db_path"]).parent / "ticket-flow-manifest.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return path


def detach(board: str) -> None:
    run(["hermes", "kanban", "boards", "rm", board])
