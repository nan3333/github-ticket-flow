import argparse
import pathlib
import sys
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

    def test_start_parses_herdr_runner(self):
        args = self.parser().parse_args(["start", "191", "--runner", "herdr", "--dry-run"])
        self.assertEqual(args.runner, "herdr")

    def test_herdr_commands_exist(self):
        run = self.parser().parse_args(["herdr-run", "--board", "demo", "--once"])
        delegate = self.parser().parse_args(["herdr-delegate", "--spec", "/tmp/lenses.json"])
        self.assertEqual(run.ticket_flow_action, "herdr-run")
        self.assertTrue(run.once)
        self.assertEqual(delegate.spec, "/tmp/lenses.json")

    def test_status_and_detach_subcommands_exist(self):
        status = self.parser().parse_args(["status", "--board", "demo"])
        detach = self.parser().parse_args(["detach", "--board", "demo", "--yes"])
        self.assertEqual(status.ticket_flow_action, "status")
        self.assertEqual(detach.ticket_flow_action, "detach")
        self.assertTrue(detach.yes)

    def test_failed_herdr_delegate_returns_nonzero_after_printing_result(self):
        args = self.parser().parse_args(["herdr-delegate", "--spec", "/tmp/lenses.json", "--json"])
        with mock.patch.object(cli.herdr_runner, "run_delegate_spec", return_value={"ok": False, "lenses": []}), mock.patch.object(
            cli, "_print"
        ) as print_result:
            code = cli.ticket_flow_command(args)

        self.assertEqual(code, 1)
        print_result.assert_called_once_with({"ok": False, "lenses": []}, True)

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
