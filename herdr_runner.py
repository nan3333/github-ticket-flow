#!/usr/bin/env python3
"""Herdr-backed execution for GitHub ticket-flow Kanban workers."""

from __future__ import annotations

import json
import os
import pathlib
import re
import shlex
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager, suppress
from typing import Any, Callable, Iterator, Mapping, Sequence

KANBAN_ENV_KEYS = (
    "HERMES_KANBAN_TASK",
    "HERMES_KANBAN_RUN_ID",
    "HERMES_KANBAN_WORKSPACE",
    "HERMES_KANBAN_WORKSPACES_ROOT",
    "HERMES_KANBAN_CLAIM_LOCK",
    "HERMES_KANBAN_BOARD",
    "HERMES_KANBAN_DB",
    "HERMES_KANBAN_BRANCH",
    "HERMES_KANBAN_GOAL_MODE",
    "HERMES_KANBAN_GOAL_MAX_TURNS",
    "HERMES_TENANT",
)
_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_SAFE_CHILD_ENV_KEYS = {
    "COLORTERM",
    "HERDR_SOCKET_PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "PATH",
    "SHELL",
    "SSH_AUTH_SOCK",
    "TERM",
    "TMPDIR",
}


def require_herdr_environment(environ: Mapping[str, str] | None = None) -> None:
    values = os.environ if environ is None else environ
    if values.get("HERDR_ENV") != "1" or not values.get("HERDR_WORKSPACE_ID"):
        raise RuntimeError(
            "Herdr execution must be started inside a Herdr-managed pane "
            "(HERDR_ENV=1 and HERDR_WORKSPACE_ID are required)"
        )


def _response_result(stdout: str) -> dict[str, Any]:
    payload = json.loads(stdout)
    result = payload.get("result", payload)
    if not isinstance(result, dict):
        raise RuntimeError("Herdr returned an unexpected response")
    return result


def _label(value: str) -> str:
    return " ".join(value.split())[:80]


def build_scrubbed_process_environment(overrides: Mapping[str, str]) -> dict[str, str]:
    """Build a secret-scrubbed, identity-fenced environment for a Hermes child."""
    try:
        from tools.environments.local import hermes_subprocess_env

        scrubbed = hermes_subprocess_env(inherit_credentials=False)
        env = {key: value for key, value in scrubbed.items() if key in _SAFE_CHILD_ENV_KEYS}
    except ImportError:
        env = {
            key: value
            for key, value in os.environ.items()
            if key in _SAFE_CHILD_ENV_KEYS
        }
    for key in KANBAN_ENV_KEYS:
        env.pop(key, None)
    env.update({key: str(value) for key, value in overrides.items()})
    return env


def build_scrubbed_command(argv: Sequence[str], env: Mapping[str, str]) -> str:
    assignments = [f"{key}={value}" for key, value in sorted(env.items())]
    return shlex.join(["env", "-i", *assignments, *argv])


def write_launch_script(
    argv: Sequence[str],
    env: Mapping[str, str],
    *,
    directory: str | pathlib.Path | None = None,
) -> pathlib.Path:
    """Write a self-removing local launcher so pane input stays bounded."""
    fd, raw_path = tempfile.mkstemp(prefix=".ticket-flow-herdr-", suffix=".sh", dir=directory, text=True)
    path = pathlib.Path(raw_path)
    try:
        os.fchmod(fd, 0o700)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\nrm -f \"$0\"\nexec ")
            handle.write(build_scrubbed_command(argv, env))
            handle.write("\n")
    except BaseException:
        with suppress(OSError):
            os.close(fd)
        with suppress(OSError):
            path.unlink()
        raise
    return path


def _select_worker_pid(process_info: Mapping[str, Any]) -> int:
    processes = process_info.get("foreground_processes")
    if not isinstance(processes, list):
        raise RuntimeError("Herdr did not report a Hermes worker process")
    for process in processes:
        if not isinstance(process, dict):
            continue
        pid = process.get("pid")
        argv = process.get("argv")
        if (
            isinstance(pid, int)
            and pid > 0
            and isinstance(argv, list)
            and "--cli" in argv
            and "chat" in argv
            and any(pathlib.Path(str(arg)).name == "hermes" for arg in argv)
        ):
            return pid
    raise RuntimeError("Herdr did not report a Hermes worker process")


class HerdrClient:
    """Small argv-only client for Herdr's local socket CLI."""

    def __init__(
        self,
        *,
        executable: str = "herdr",
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.executable = executable
        self.runner = runner

    def _run(self, *args: str, json_output: bool = True) -> str:
        completed = self.runner(
            [self.executable, *args],
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout

    @staticmethod
    def _env_args(env: Mapping[str, str]) -> list[str]:
        args: list[str] = []
        for key, value in sorted(env.items()):
            args.extend(["--env", f"{key}={value}"])
        return args

    def create_workspace(self, *, cwd: str, label: str, env: Mapping[str, str]) -> dict[str, str]:
        result = _response_result(
            self._run(
                "workspace",
                "create",
                "--cwd",
                cwd,
                "--label",
                _label(label),
                "--no-focus",
                *self._env_args(env),
            )
        )
        return {
            "workspace_id": result["workspace"]["workspace_id"],
            "pane_id": result["root_pane"]["pane_id"],
        }

    def create_tab(
        self,
        *,
        workspace_id: str,
        cwd: str,
        label: str,
        env: Mapping[str, str],
    ) -> dict[str, str]:
        result = _response_result(
            self._run(
                "tab",
                "create",
                "--workspace",
                workspace_id,
                "--cwd",
                cwd,
                "--label",
                _label(label),
                "--no-focus",
                *self._env_args(env),
            )
        )
        return {
            "tab_id": result["tab"]["tab_id"],
            "pane_id": result["root_pane"]["pane_id"],
        }

    def rename_pane(self, pane_id: str, label: str) -> None:
        self._run("pane", "rename", pane_id, _label(label))

    def run_in_pane(self, pane_id: str, command: str) -> None:
        self._run("pane", "run", pane_id, command)

    def process_id(self, pane_id: str, *, timeout_seconds: float = 5) -> int:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            result = _response_result(self._run("pane", "process-info", "--pane", pane_id))
            info = result.get("process_info", result)
            try:
                return _select_worker_pid(info)
            except RuntimeError:
                pass
            time.sleep(0.1)
        raise RuntimeError(f"Herdr pane {pane_id} did not start a foreground worker")

    def wait_for_output(self, pane_id: str, marker: str, *, timeout_ms: int) -> str:
        result = _response_result(
            self._run(
                "pane",
                "wait-output",
                pane_id,
                "--match",
                marker,
                "--timeout",
                str(timeout_ms),
            )
        )
        return str(result.get("matched_line", ""))

    def read_pane(self, pane_id: str, *, lines: int = 400) -> str:
        return self._run(
            "pane",
            "read",
            pane_id,
            "--source",
            "recent-unwrapped",
            "--lines",
            str(lines),
            json_output=False,
        )

    def close_pane(self, pane_id: str) -> None:
        self._run("pane", "close", pane_id)

    def close_workspace(self, workspace_id: str) -> None:
        self._run("workspace", "close", workspace_id)


def build_worker_environment(
    task: Any,
    workspace: str,
    board: str,
    *,
    kb: Any,
    resolve_profile_env: Callable[[str], str],
) -> dict[str, str]:
    if not task.assignee:
        raise ValueError(f"task {task.id} has no assignee")
    if not isinstance(task.current_run_id, int) or task.current_run_id <= 0 or not task.claim_lock:
        raise ValueError(f"task {task.id} has no valid run and claim fence")
    env = {
        "HERMES_HOME": str(resolve_profile_env(task.assignee)),
        "HERMES_PROFILE": task.assignee,
        "HERMES_SESSION_SOURCE": "kanban",
        "HERMES_KANBAN_TASK": task.id,
        "HERMES_KANBAN_DB": str(kb.kanban_db_path(board=board)),
        "HERMES_KANBAN_BOARD": board,
        "HERMES_KANBAN_WORKSPACES_ROOT": str(kb.workspaces_root(board=board)),
        "HERMES_KANBAN_WORKSPACE": workspace,
        "TERMINAL_CWD": workspace,
        "HERMES_TUI": "",
    }
    env["HERMES_KANBAN_RUN_ID"] = str(task.current_run_id)
    env["HERMES_KANBAN_CLAIM_LOCK"] = task.claim_lock
    if task.tenant:
        env["HERMES_TENANT"] = task.tenant
    if task.branch_name:
        env["HERMES_KANBAN_BRANCH"] = task.branch_name
    return env


def build_worker_argv(task: Any, hermes_argv: Sequence[str]) -> list[str]:
    if not task.assignee:
        raise ValueError(f"task {task.id} has no assignee")
    argv = [*hermes_argv, "-p", task.assignee, "--cli", "--accept-hooks"]
    for skill in task.skills or []:
        if skill:
            argv.extend(["--skills", skill])
    if task.model_override:
        argv.extend(["-m", task.model_override])
        if task.provider_override:
            argv.extend(["--provider", task.provider_override])
    if task.reasoning_effort:
        argv.extend(["--reasoning", task.reasoning_effort])
    argv.extend(["chat", "-q", f"work kanban task {task.id}"])
    return argv


class HerdrSpawner:
    """Dispatcher spawn function that puts one Hermes worker in one Herdr workspace."""

    def __init__(
        self,
        *,
        client: HerdrClient,
        kb: Any,
        resolve_profile_env: Callable[[str], str],
        hermes_argv: Sequence[str],
    ) -> None:
        self.client = client
        self.kb = kb
        self.resolve_profile_env = resolve_profile_env
        self.hermes_argv = list(hermes_argv)
        self.launches: list[dict[str, str]] = []

    def __call__(self, task: Any, workspace: str, *, board: str | None = None) -> int:
        if not board:
            raise ValueError("Herdr worker spawn requires an explicit board")
        env = build_worker_environment(
            task,
            workspace,
            board,
            kb=self.kb,
            resolve_profile_env=self.resolve_profile_env,
        )
        label = _label(f"tf-{task.id} {task.title}")
        created = self.client.create_workspace(cwd=workspace, label=label, env={})
        pane_id = created["pane_id"]
        herdr_env = {
            "HERMES_HERDR_WORKSPACE_ID": created["workspace_id"],
            "HERMES_HERDR_PANE_ID": pane_id,
        }
        launcher_path: pathlib.Path | None = None
        try:
            self.client.rename_pane(pane_id, f"{task.assignee}: {task.title}")
            process_env = build_scrubbed_process_environment(
                {
                    **env,
                    **herdr_env,
                    "HERDR_ENV": "1",
                    "HERDR_WORKSPACE_ID": created["workspace_id"],
                    "HERDR_PANE_ID": pane_id,
                }
            )
            launcher_path = write_launch_script(
                build_worker_argv(task, self.hermes_argv),
                process_env,
                directory=pathlib.Path(self.kb.kanban_db_path(board=board)).parent,
            )
            self.client.run_in_pane(pane_id, shlex.join(["/bin/sh", str(launcher_path)]))
            worker_pid = self.client.process_id(pane_id)
            with suppress(OSError):
                launcher_path.unlink()
        except BaseException:
            if launcher_path is not None:
                with suppress(OSError):
                    launcher_path.unlink()
            with suppress(Exception):
                self.client.close_workspace(created["workspace_id"])
            raise
        self.launches.append(
            {
                "task_id": task.id,
                "workspace_id": created["workspace_id"],
                "pane_id": pane_id,
            }
        )
        return worker_pid

    def drain_launches(self) -> list[dict[str, str]]:
        launches = self.launches
        self.launches = []
        return launches


def _delegated_environment(profile: str, profile_home: str) -> dict[str, str]:
    env = {key: "" for key in KANBAN_ENV_KEYS}
    env.update(
        {
            "HERMES_DELEGATED_CHILD_CONTEXT": "1",
            "HERMES_HOME": profile_home,
            "HERMES_PROFILE": profile,
            "HERMES_SESSION_SOURCE": "subagent",
            "HERMES_TUI": "",
        }
    )
    return env


def _load_delegate_spec(path: str | pathlib.Path) -> dict[str, Any]:
    payload = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Herdr delegation spec must be an object")
    profile = payload.get("profile")
    lenses = payload.get("lenses")
    if not isinstance(profile, str) or not profile.strip():
        raise ValueError("Herdr delegation spec profile must be a non-empty string")
    if not isinstance(lenses, list) or not 1 <= len(lenses) <= 4:
        raise ValueError("Herdr delegation spec lenses must contain 1 to 4 entries")
    seen: set[str] = set()
    for lens in lenses:
        if not isinstance(lens, dict):
            raise ValueError("each Herdr delegation lens must be an object")
        name = lens.get("name")
        prompt = lens.get("prompt")
        if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
            raise ValueError("Herdr delegation lens names must match [a-z][a-z0-9_-]{0,31}")
        if name in seen:
            raise ValueError(f"duplicate Herdr delegation lens name: {name}")
        seen.add(name)
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError(f"Herdr delegation lens {name} needs a non-empty prompt")
    timeout = payload.get("timeout_seconds", 1800)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 30 <= timeout <= 7200:
        raise ValueError("Herdr delegation timeout_seconds must be an integer from 30 to 7200")
    return payload


def run_delegate_spec(
    path: str | pathlib.Path,
    *,
    client: HerdrClient | Any | None = None,
    environ: Mapping[str, str] | None = None,
    resolve_profile_env: Callable[[str], str] | None = None,
    hermes_argv: Sequence[str] = ("hermes",),
) -> dict[str, Any]:
    values = os.environ if environ is None else environ
    require_herdr_environment(values)
    payload = _load_delegate_spec(path)
    profile = payload["profile"].strip()
    timeout_ms = int(payload.get("timeout_seconds", 1800)) * 1000
    workspace_id = values["HERDR_WORKSPACE_ID"]
    cwd = values.get("HERMES_KANBAN_WORKSPACE") or values.get("TERMINAL_CWD") or os.getcwd()
    task_id = values.get("HERMES_KANBAN_TASK", "standalone")
    if resolve_profile_env is None:
        from hermes_cli.profiles import resolve_profile_env as _resolve_profile_env

        resolve_profile_env = _resolve_profile_env
    herdr = client or HerdrClient()
    child_env = _delegated_environment(profile, str(resolve_profile_env(profile)))
    launched: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="ticket-flow-herdr-") as result_dir:
        try:
            for lens in payload["lenses"]:
                name = lens["name"]
                result_path = pathlib.Path(result_dir) / f"{name}.md"
                marker = f"__TICKET_FLOW_LENS_DONE_{uuid.uuid4().hex}__"
                prompt = (
                    f"{lens['prompt'].strip()}\n\n"
                    "You are a read-only evidence lens. Do not edit the repository, call Kanban lifecycle tools, "
                    "or synthesize the parent worker's final verdict. Cite exact files, lines, commands, and observed outputs. "
                    f"Before finishing, write your complete standalone report to {result_path} using write_file."
                )
                created = herdr.create_tab(
                    workspace_id=workspace_id,
                    cwd=cwd,
                    label=f"{task_id}-{name}",
                    env=child_env,
                )
                pane_id = created["pane_id"]
                launched.append(
                    {
                        "name": name,
                        "pane_id": pane_id,
                        "marker": marker,
                        "result_path": result_path,
                        "launcher_path": None,
                    }
                )
                herdr.rename_pane(pane_id, f"lens: {name}")
                argv = [*hermes_argv, "-p", profile, "--cli", "--accept-hooks", "chat", "-q", prompt]
                process_env = build_scrubbed_process_environment(
                    {
                        **child_env,
                        "HERDR_ENV": "1",
                        "HERDR_WORKSPACE_ID": workspace_id,
                        "HERDR_PANE_ID": pane_id,
                    }
                )
                launcher_path = write_launch_script(argv, process_env, directory=result_dir)
                launched[-1]["launcher_path"] = launcher_path
                command = (
                    f"{shlex.join(['/bin/sh', str(launcher_path)])} ; rc=$?; "
                    f"printf '\\n{marker}:%s\\n' \"$rc\""
                )
                herdr.run_in_pane(pane_id, command)

            results: list[dict[str, Any]] = []
            for lens in launched:
                matched = herdr.wait_for_output(
                    lens["pane_id"],
                    lens["marker"],
                    timeout_ms=timeout_ms,
                )
                match = re.search(re.escape(lens["marker"]) + r":(\d+)", matched)
                exit_code = int(match.group(1)) if match else None
                result_path = lens["result_path"]
                report_written = result_path.exists()
                report = (
                    result_path.read_text(encoding="utf-8")
                    if report_written
                    else herdr.read_pane(lens["pane_id"], lines=400)
                )
                results.append(
                    {
                        "name": lens["name"],
                        "pane_id": lens["pane_id"],
                        "exit_code": exit_code,
                        "report": report,
                        "report_written": report_written,
                    }
                )
        except BaseException:
            for lens in launched:
                launcher_path = lens.get("launcher_path")
                if isinstance(launcher_path, pathlib.Path):
                    with suppress(OSError):
                        launcher_path.unlink()
                with suppress(Exception):
                    herdr.close_pane(lens["pane_id"])
            raise

    ok = all(lens["exit_code"] == 0 and lens["report_written"] for lens in results)
    if not ok:
        for lens in launched:
            launcher_path = lens.get("launcher_path")
            if isinstance(launcher_path, pathlib.Path):
                with suppress(OSError):
                    launcher_path.unlink()
            with suppress(Exception):
                herdr.close_pane(lens["pane_id"])
    return {
        "ok": ok,
        "profile": profile,
        "lenses": results,
    }


def gateway_dispatch_is_disabled(
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> bool:
    completed = runner(
        ["hermes", "config", "get", "kanban.dispatch_in_gateway"],
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.returncode == 0 and completed.stdout.strip().lower() == "false"


@contextmanager
def _runner_lock(kb: Any, board: str) -> Iterator[None]:
    try:
        import fcntl
    except ImportError as exc:  # pragma: no cover - Herdr supports macOS/Linux
        raise RuntimeError("Herdr ticket-flow runner requires macOS or Linux") from exc
    lock_path = pathlib.Path(kb.kanban_db_path(board=board)).parent / ".herdr-runner.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"a Herdr runner already owns board {board}") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _gateway_dispatcher_lock(kb: Any) -> Iterator[None]:
    """Exclude the boot-captured gateway dispatcher for this runner's lifetime."""
    try:
        import fcntl
    except ImportError as exc:  # pragma: no cover - Herdr supports macOS/Linux
        raise RuntimeError("Herdr ticket-flow runner requires macOS or Linux") from exc
    lock_path = pathlib.Path(kb.kanban_home()) / "kanban" / ".dispatcher.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                "gateway dispatcher lock is still held; disable gateway dispatch and restart the gateway"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def run_dispatcher(
    board: str,
    *,
    once: bool = False,
    interval_seconds: float = 2.0,
    max_spawn: int = 4,
    client: HerdrClient | None = None,
) -> None:
    require_herdr_environment()
    if not gateway_dispatch_is_disabled():
        raise RuntimeError(
            "refusing to race the gateway dispatcher; set "
            "kanban.dispatch_in_gateway=false and restart the gateway first"
        )
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing
    from hermes_cli.kanban_db_dispatch import dispatch_once
    from hermes_cli.profiles import resolve_profile_env

    try:
        hermes_argv = kb._resolve_hermes_argv()
    except AttributeError:
        hermes_argv = ["hermes"]
    spawner = HerdrSpawner(
        client=client or HerdrClient(),
        kb=kb,
        resolve_profile_env=resolve_profile_env,
        hermes_argv=hermes_argv,
    )
    with _gateway_dispatcher_lock(kb), _runner_lock(kb, board):
        while True:
            with connect_closing(board=board) as conn:
                result = dispatch_once(
                    conn,
                    spawn_fn=spawner,
                    board=board,
                    max_spawn=max_spawn,
                )
            launches = spawner.drain_launches()
            if launches:
                with connect_closing(board=board) as conn:
                    for launch in launches:
                        kb.add_comment(
                            conn,
                            launch["task_id"],
                            "ticket-flow:herdr-runner",
                            "Herdr worker pointer: "
                            + json.dumps(
                                {
                                    "workspace_id": launch["workspace_id"],
                                    "pane_id": launch["pane_id"],
                                    "focus_command": f"herdr workspace focus {launch['workspace_id']}",
                                },
                                sort_keys=True,
                            ),
                        )
            for task_id, assignee, workspace in result.spawned:
                print(
                    json.dumps(
                        {
                            "event": "spawned",
                            "board": board,
                            "task_id": task_id,
                            "assignee": assignee,
                            "workspace": workspace,
                        }
                    ),
                    flush=True,
                )
            if once:
                return
            time.sleep(max(interval_seconds, 1.0))


def launch_dispatcher_tab(
    board: str,
    *,
    project_path: str,
    interval_seconds: float = 2.0,
    max_spawn: int = 4,
    client: HerdrClient | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    values = os.environ if environ is None else environ
    require_herdr_environment(values)
    herdr = client or HerdrClient()
    runner_env = {key: "" for key in KANBAN_ENV_KEYS}
    runner_env["HERMES_DELEGATED_CHILD_CONTEXT"] = ""
    created = herdr.create_tab(
        workspace_id=values["HERDR_WORKSPACE_ID"],
        cwd=project_path,
        label=f"ticket-flow runner: {board}",
        env=runner_env,
    )
    pane_id = created["pane_id"]
    herdr.rename_pane(pane_id, f"runner: {board}")
    herdr.run_in_pane(
        pane_id,
        shlex.join(
            [
                "hermes",
                "ticket-flow",
                "herdr-run",
                "--board",
                board,
                "--interval",
                str(interval_seconds),
                "--max-spawn",
                str(max_spawn),
            ]
        ),
    )
    return created
