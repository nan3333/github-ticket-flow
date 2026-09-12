import importlib.util
import json
import os
import pathlib
import re
import shlex
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace

RUNNER_PATH = pathlib.Path(__file__).parents[1] / "herdr_runner.py"
spec = importlib.util.spec_from_file_location("github_ticket_flow_herdr_runner", RUNNER_PATH)
herdr_runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(herdr_runner)


class FakeHerdrClient:
    def __init__(self):
        self.calls = []
        self.next_id = 1
        self.launch_scripts = []

    def create_workspace(self, *, cwd, label, env):
        self.calls.append(("create_workspace", cwd, label, env))
        return {"workspace_id": "w2", "pane_id": "w2:p1"}

    def create_tab(self, *, workspace_id, cwd, label, env):
        pane_id = f"w1:p{self.next_id}"
        self.next_id += 1
        self.calls.append(("create_tab", workspace_id, cwd, label, env, pane_id))
        return {"tab_id": pane_id.replace(":p", ":t"), "pane_id": pane_id}

    def rename_pane(self, pane_id, label):
        self.calls.append(("rename_pane", pane_id, label))

    def run_in_pane(self, pane_id, command):
        self.calls.append(("run_in_pane", pane_id, command))
        parts = shlex.split(command)
        script_text = pathlib.Path(parts[1]).read_text(encoding="utf-8") if parts[:1] == ["/bin/sh"] else command
        self.launch_scripts.append(script_text)
        report_path = re.search(r"report to ([^ ]+) using write_file", script_text)
        if report_path:
            pathlib.Path(report_path.group(1)).write_text("report", encoding="utf-8")

    def process_id(self, pane_id, *, timeout_seconds=5):
        self.calls.append(("process_id", pane_id, timeout_seconds))
        return 4321

    def wait_for_output(self, pane_id, marker, *, timeout_ms):
        self.calls.append(("wait_for_output", pane_id, marker, timeout_ms))
        return marker + ":0"

    def read_pane(self, pane_id, *, lines=400):
        self.calls.append(("read_pane", pane_id, lines))
        return f"fallback from {pane_id}"

    def close_pane(self, pane_id):
        self.calls.append(("close_pane", pane_id))

    def close_workspace(self, workspace_id):
        self.calls.append(("close_workspace", workspace_id))


class HerdrRunnerTests(unittest.TestCase):
    def test_requires_herdr_managed_environment(self):
        with self.assertRaisesRegex(RuntimeError, "inside a Herdr-managed pane"):
            herdr_runner.require_herdr_environment({})

    def test_worker_environment_pins_kanban_identity(self):
        task = SimpleNamespace(
            id="t_abc",
            assignee="implementer",
            tenant="house-a",
            branch_name="agent/issue-1",
            current_run_id=17,
            claim_lock="host:pid:lock",
        )
        kb = SimpleNamespace(
            kanban_db_path=lambda board: pathlib.Path("/tmp/kanban.db"),
            workspaces_root=lambda board: pathlib.Path("/tmp/workspaces"),
        )

        env = herdr_runner.build_worker_environment(
            task,
            "/tmp/worktree",
            "demo",
            kb=kb,
            resolve_profile_env=lambda profile: f"/profiles/{profile}",
        )

        self.assertEqual(env["HERMES_HOME"], "/profiles/implementer")
        self.assertEqual(env["HERMES_KANBAN_TASK"], "t_abc")
        self.assertEqual(env["HERMES_KANBAN_RUN_ID"], "17")
        self.assertEqual(env["HERMES_KANBAN_CLAIM_LOCK"], "host:pid:lock")
        self.assertEqual(env["HERMES_KANBAN_BOARD"], "demo")
        self.assertEqual(env["HERMES_KANBAN_WORKSPACE"], "/tmp/worktree")
        self.assertEqual(env["HERMES_TENANT"], "house-a")
        self.assertEqual(env["HERMES_KANBAN_BRANCH"], "agent/issue-1")

    def test_delegated_environment_clears_every_task_identity(self):
        env = herdr_runner._delegated_environment("reviewer", "/profiles/reviewer")

        expected = {
            "HERMES_KANBAN_TASK",
            "HERMES_KANBAN_RUN_ID",
            "HERMES_KANBAN_CLAIM_LOCK",
            "HERMES_KANBAN_GOAL_MODE",
            "HERMES_KANBAN_GOAL_MAX_TURNS",
            "HERMES_KANBAN_WORKSPACE",
            "HERMES_KANBAN_WORKSPACES_ROOT",
            "HERMES_KANBAN_BOARD",
            "HERMES_KANBAN_DB",
            "HERMES_KANBAN_BRANCH",
            "HERMES_TENANT",
        }
        self.assertTrue(expected.issubset(env))
        self.assertTrue(all(env[key] == "" for key in expected))

    def test_scrubbed_command_does_not_inherit_secrets_or_stale_identity(self):
        with mock.patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "secret", "HERMES_KANBAN_TASK": "stale", "PATH": "/bin"},
            clear=True,
        ):
            env = herdr_runner.build_scrubbed_process_environment(
                {"HERMES_KANBAN_TASK": "current", "HERMES_PROFILE": "implementer"}
            )
        command = herdr_runner.build_scrubbed_command(["hermes", "chat"], env)

        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertEqual(env["HERMES_KANBAN_TASK"], "current")
        self.assertTrue(command.startswith("env -i "))
        self.assertNotIn("secret", command)

    def test_spawner_launches_one_shot_worker_in_visible_workspace(self):
        task = SimpleNamespace(
            id="t_abc",
            title="Deliver GitHub #1",
            assignee="implementer",
            skills=["github-ticket-flow:workflow"],
            model_override=None,
            provider_override=None,
            reasoning_effort=None,
            tenant=None,
            branch_name="agent/issue-1",
            current_run_id=17,
            claim_lock="host:pid:lock",
        )
        client = FakeHerdrClient()
        spawner = herdr_runner.HerdrSpawner(
            client=client,
            kb=SimpleNamespace(
                kanban_db_path=lambda board: pathlib.Path("/tmp/kanban.db"),
                workspaces_root=lambda board: pathlib.Path("/tmp/workspaces"),
            ),
            resolve_profile_env=lambda profile: f"/profiles/{profile}",
            hermes_argv=["hermes"],
        )

        pid = spawner(task, "/tmp/worktree", board="demo")

        self.assertEqual(pid, 4321)
        create = client.calls[0]
        self.assertEqual(create[:3], ("create_workspace", "/tmp/worktree", "tf-t_abc Deliver GitHub #1"))
        launch = next(call for call in client.calls if call[0] == "run_in_pane")
        self.assertLess(len(launch[2]), 512)
        self.assertTrue(launch[2].startswith("/bin/sh "))
        script = client.launch_scripts[0]
        self.assertIn("exec env -i", script)
        self.assertIn("hermes -p implementer --cli --accept-hooks", script)
        self.assertIn("--skills github-ticket-flow:workflow", script)
        self.assertIn("chat -q 'work kanban task t_abc'", script)
        self.assertIn("HERMES_HERDR_WORKSPACE_ID=w2", script)
        self.assertIn("HERMES_HERDR_PANE_ID=w2:p1", script)
        for expected in (
            "HERMES_KANBAN_BOARD=demo",
            "HERMES_KANBAN_TASK=t_abc",
            "HERMES_KANBAN_RUN_ID=17",
            "HERMES_KANBAN_CLAIM_LOCK=host:pid:lock",
            "HERMES_PROFILE=implementer",
            "HERMES_KANBAN_WORKSPACE=/tmp/worktree",
            "HERMES_KANBAN_BRANCH=agent/issue-1",
        ):
            self.assertIn(expected, script)
        self.assertNotIn("--resume", script)
        self.assertNotIn("compact", script)
        self.assertEqual(
            spawner.drain_launches(),
            [{"task_id": "t_abc", "workspace_id": "w2", "pane_id": "w2:p1"}],
        )
        self.assertEqual(spawner.drain_launches(), [])

    def test_process_info_selects_actual_hermes_pid_not_process_group(self):
        payload = {
            "foreground_process_group_id": 7000,
            "foreground_processes": [
                {"pid": 7001, "argv": ["sh", "-c", "wrapper"]},
                {"pid": 7002, "argv": ["python", "hermes", "--cli", "chat", "-q", "work"]},
            ],
        }

        self.assertEqual(herdr_runner._select_worker_pid(payload), 7002)

    def test_process_info_rejects_unverified_group_leader_when_shell_pid_is_missing(self):
        with self.assertRaisesRegex(RuntimeError, "Hermes worker process"):
            herdr_runner._select_worker_pid(
                {
                    "foreground_process_group_id": 7001,
                    "foreground_processes": [{"pid": 7001, "argv": ["python", "-m", "unrelated"]}],
                }
            )

    def test_process_info_fails_closed_without_hermes_process(self):
        with self.assertRaisesRegex(RuntimeError, "Hermes worker process"):
            herdr_runner._select_worker_pid(
                {"foreground_process_group_id": 7000, "foreground_processes": [{"pid": 7001, "argv": ["sleep", "10"]}]}
            )

    def test_process_info_does_not_treat_idle_shell_as_worker(self):
        with self.assertRaisesRegex(RuntimeError, "Hermes worker process"):
            herdr_runner._select_worker_pid(
                {
                    "foreground_process_group_id": 7000,
                    "shell_pid": 7000,
                    "foreground_processes": [{"pid": 7000, "argv": ["-bash"]}],
                }
            )

    def test_spawner_closes_workspace_when_worker_does_not_start(self):
        class FailingClient(FakeHerdrClient):
            def process_id(self, pane_id, *, timeout_seconds=5):
                raise RuntimeError("no worker")

        task = SimpleNamespace(
            id="t_abc",
            title="Deliver GitHub #1",
            assignee="implementer",
            skills=[],
            model_override=None,
            provider_override=None,
            reasoning_effort=None,
            tenant=None,
            branch_name=None,
            current_run_id=17,
            claim_lock="lock",
        )
        client = FailingClient()
        spawner = herdr_runner.HerdrSpawner(
            client=client,
            kb=SimpleNamespace(
                kanban_db_path=lambda board: pathlib.Path("/tmp/kanban.db"),
                workspaces_root=lambda board: pathlib.Path("/tmp/workspaces"),
            ),
            resolve_profile_env=lambda profile: f"/profiles/{profile}",
            hermes_argv=["hermes"],
        )

        with self.assertRaisesRegex(RuntimeError, "no worker"):
            spawner(task, "/tmp/worktree", board="demo")
        self.assertIn(("close_workspace", "w2"), client.calls)

    def test_worker_environment_requires_run_and_claim_fence(self):
        kb = SimpleNamespace(
            kanban_db_path=lambda board: pathlib.Path("/tmp/kanban.db"),
            workspaces_root=lambda board: pathlib.Path("/tmp/workspaces"),
        )
        base = {
            "id": "t_abc",
            "assignee": "implementer",
            "tenant": None,
            "branch_name": None,
            "current_run_id": 17,
            "claim_lock": "lock",
        }
        for missing in ("current_run_id", "claim_lock"):
            values = {**base, missing: None}
            with self.subTest(missing=missing), self.assertRaisesRegex(ValueError, "run and claim fence"):
                herdr_runner.build_worker_environment(
                    SimpleNamespace(**values),
                    "/tmp/worktree",
                    "demo",
                    kb=kb,
                    resolve_profile_env=lambda profile: f"/profiles/{profile}",
                )

    def test_delegate_batch_starts_every_lens_before_waiting(self):
        client = FakeHerdrClient()
        with tempfile.TemporaryDirectory() as td:
            spec_path = pathlib.Path(td) / "lenses.json"
            spec_path.write_text(
                json.dumps(
                    {
                        "profile": "reviewer",
                        "lenses": [
                            {"name": "requirements", "prompt": "Review requirements"},
                            {"name": "security", "prompt": "Review tenancy"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            result = herdr_runner.run_delegate_spec(
                spec_path,
                client=client,
                environ={
                    "HERDR_ENV": "1",
                    "HERDR_WORKSPACE_ID": "w1",
                    "HERMES_KANBAN_WORKSPACE": "/tmp/worktree",
                    "HERMES_KANBAN_TASK": "t_abc",
                },
                resolve_profile_env=lambda profile: f"/profiles/{profile}",
            )

        call_names = [call[0] for call in client.calls]
        self.assertLess(max(i for i, name in enumerate(call_names) if name == "run_in_pane"), call_names.index("wait_for_output"))
        create_calls = [call for call in client.calls if call[0] == "create_tab"]
        self.assertEqual(len(create_calls), 2)
        for call in create_calls:
            child_env = call[4]
            self.assertEqual(child_env["HERMES_DELEGATED_CHILD_CONTEXT"], "1")
            self.assertEqual(child_env["HERMES_KANBAN_TASK"], "")
        self.assertTrue(all("HERMES_DELEGATED_CHILD_CONTEXT=1" in script for script in client.launch_scripts))
        self.assertTrue(result["ok"])
        self.assertEqual([lens["name"] for lens in result["lenses"]], ["requirements", "security"])

    def test_delegate_failure_is_reported_and_launched_panes_are_closed_on_exception(self):
        class FailingClient(FakeHerdrClient):
            def wait_for_output(self, pane_id, marker, *, timeout_ms):
                raise RuntimeError("wait failed")

        client = FailingClient()
        with tempfile.TemporaryDirectory() as td:
            spec_path = pathlib.Path(td) / "lenses.json"
            spec_path.write_text(
                json.dumps(
                    {
                        "profile": "reviewer",
                        "lenses": [
                            {"name": "requirements", "prompt": "Review requirements"},
                            {"name": "security", "prompt": "Review tenancy"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "wait failed"):
                herdr_runner.run_delegate_spec(
                    spec_path,
                    client=client,
                    environ={"HERDR_ENV": "1", "HERDR_WORKSPACE_ID": "w1"},
                    resolve_profile_env=lambda profile: f"/profiles/{profile}",
                )

        self.assertEqual(
            [call[1] for call in client.calls if call[0] == "close_pane"],
            ["w1:p1", "w1:p2"],
        )

    def test_delegate_nonzero_or_missing_report_makes_batch_fail(self):
        class NonzeroClient(FakeHerdrClient):
            def run_in_pane(self, pane_id, command):
                self.calls.append(("run_in_pane", pane_id, command))

            def wait_for_output(self, pane_id, marker, *, timeout_ms):
                return marker + ":7"

        client = NonzeroClient()
        with tempfile.TemporaryDirectory() as td:
            spec_path = pathlib.Path(td) / "lenses.json"
            spec_path.write_text(
                json.dumps({"profile": "reviewer", "lenses": [{"name": "security", "prompt": "Review tenancy"}]}),
                encoding="utf-8",
            )
            result = herdr_runner.run_delegate_spec(
                spec_path,
                client=client,
                environ={"HERDR_ENV": "1", "HERDR_WORKSPACE_ID": "w1"},
                resolve_profile_env=lambda profile: f"/profiles/{profile}",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["lenses"][0]["exit_code"], 7)
        self.assertFalse(result["lenses"][0]["report_written"])
        self.assertIn(("close_pane", "w1:p1"), client.calls)

    def test_gateway_dispatcher_lock_contends_and_releases(self):
        import fcntl

        with tempfile.TemporaryDirectory() as td:
            kb = SimpleNamespace(kanban_home=lambda: pathlib.Path(td))
            lock_path = pathlib.Path(td) / "kanban" / ".dispatcher.lock"
            lock_path.parent.mkdir(parents=True)
            with lock_path.open("a+") as holder:
                fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(RuntimeError, "gateway dispatcher lock"):
                    with herdr_runner._gateway_dispatcher_lock(kb):
                        self.fail("contended lock must not be acquired")
                fcntl.flock(holder.fileno(), fcntl.LOCK_UN)

            with herdr_runner._gateway_dispatcher_lock(kb):
                pass
            with herdr_runner._gateway_dispatcher_lock(kb):
                pass

    def test_dispatcher_tab_forwards_configured_polling_and_spawn_limits(self):
        client = FakeHerdrClient()

        created = herdr_runner.launch_dispatcher_tab(
            "demo-board",
            project_path="/tmp/project",
            interval_seconds=7,
            max_spawn=2,
            client=client,
            environ={"HERDR_ENV": "1", "HERDR_WORKSPACE_ID": "w1"},
        )

        self.assertEqual(created["pane_id"], "w1:p1")
        launch = next(call for call in client.calls if call[0] == "run_in_pane")
        self.assertIn("--interval 7", launch[2])
        self.assertIn("--max-spawn 2", launch[2])


if __name__ == "__main__":
    unittest.main()