import argparse
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

PLUGIN_ROOT = pathlib.Path(__file__).parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))
import cli  # noqa: E402


class TicketFlowCliTests(unittest.TestCase):
    def parser(self):
        parser = argparse.ArgumentParser()
        cli.register_cli(parser)
        return parser

    def test_start_parses_issue_numbers_and_dry_run(self):
        args = self.parser().parse_args(["start", "191", "154", "156", "--dry-run"])
        self.assertEqual(args.ticket_flow_action, "start")
        self.assertEqual(args.issues, [191, 154, 156])
        self.assertTrue(args.dry_run)

    def test_start_parses_auto_merge(self):
        args = self.parser().parse_args(["start", "191", "--auto-merge", "--dry-run"])
        self.assertTrue(args.auto_merge)

    def test_status_and_detach_subcommands_exist(self):
        status = self.parser().parse_args(["status", "--board", "demo"])
        detach = self.parser().parse_args(["detach", "--board", "demo", "--yes"])
        self.assertEqual(status.ticket_flow_action, "status")
        self.assertEqual(detach.ticket_flow_action, "detach")
        self.assertTrue(detach.yes)

    def test_analyst_pass_scrubs_the_callers_kanban_identity(self):
        kanban_keys = (
            "HERMES_KANBAN_TASK",
            "HERMES_KANBAN_RUN_ID",
            "HERMES_KANBAN_WORKSPACE",
            "HERMES_KANBAN_WORKSPACES_ROOT",
            "HERMES_KANBAN_CLAIM_LOCK",
            "HERMES_KANBAN_BOARD",
            "HERMES_KANBAN_DB",
        )
        with tempfile.TemporaryDirectory() as worktree:
            query_file = pathlib.Path(worktree) / "analyst.md"
            query_file.write_text("Inspect read-only.", encoding="utf-8")
            args = self.parser().parse_args(
                [
                    "analyst-pass",
                    "--profile",
                    "analyst",
                    "--worktree",
                    worktree,
                    "--query-file",
                    str(query_file),
                ]
            )
            polluted = {key: f"parent-{key}" for key in kanban_keys}
            polluted["HERMES_KANBAN_FUTURE_IDENTITY"] = "parent-future"
            polluted["PATH"] = os.environ["PATH"]
            completed = mock.Mock(stdout='{"authority":"advisory-only"}\n')
            with (
                mock.patch.dict(os.environ, polluted, clear=True),
                mock.patch.object(cli.subprocess, "run", return_value=completed) as run,
                mock.patch("builtins.print"),
            ):
                code = cli.ticket_flow_command(args)

        self.assertEqual(code, 0)
        argv = run.call_args.args[0]
        env = run.call_args.kwargs["env"]
        resolved_worktree = str(pathlib.Path(worktree).resolve())
        self.assertEqual(
            argv,
            [
                "hermes",
                "-p",
                "analyst",
                "--in",
                resolved_worktree,
                "chat",
                "--oneshot",
                "--query-file",
                str(query_file.resolve()),
                "--max-turns",
                "40",
            ],
        )
        self.assertEqual(run.call_args.kwargs["cwd"], resolved_worktree)
        self.assertEqual(env["HERMES_DELEGATED_CHILD_CONTEXT"], "1")
        self.assertEqual(env["PATH"], polluted["PATH"])
        for key in kanban_keys:
            self.assertNotIn(key, env)
        self.assertNotIn("HERMES_KANBAN_FUTURE_IDENTITY", env)

    @mock.patch.object(cli.core, "fetch_issues")
    @mock.patch.object(cli.core, "discover_project", return_value=("/tmp/example", "owner/repo"))
    def test_dry_run_does_not_create_board(self, discover, fetch):
        fetch.return_value = {
            9: {"number": 9, "title": "Nine", "url": "https://github.com/owner/repo/issues/9", "state": "OPEN"}
        }
        args = self.parser().parse_args(["start", "9", "--dry-run", "--json"])
        with mock.patch.object(cli.core, "apply_plan") as apply:
            code = cli.ticket_flow_command(args)
        self.assertEqual(code, 0)
        apply.assert_not_called()
        discover.assert_called_once()


if __name__ == "__main__":
    unittest.main()
