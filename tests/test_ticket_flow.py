import importlib.util
import json
import pathlib
import tempfile
import unittest
from unittest import mock

CORE_PATH = pathlib.Path(__file__).parents[1] / "ticket_flow.py"
spec = importlib.util.spec_from_file_location("github_ticket_flow_core", CORE_PATH)
ticket_flow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ticket_flow)


class TicketFlowPluginTests(unittest.TestCase):
    def test_default_run_infers_identity_and_uses_no_repo_config(self):
        cfg = ticket_flow.effective_config(
            project_path="/tmp/example",
            repository="owner/repo",
            issues=[191, 154, 156],
            overrides={},
        )
        self.assertEqual(cfg["workflow_id"], "repo-191-154-156")
        self.assertEqual(cfg["board"], "repo-ticket-flow-191-154-156")
        self.assertEqual(cfg["roles"]["implementer"], "implementer")
        self.assertEqual(cfg["pipeline"]["research_ahead"], 1)
        self.assertEqual(cfg["stage_skills"]["delivery"], ["ponytail"])

    def test_optional_config_deep_merges_repo_overrides(self):
        cfg = ticket_flow.effective_config(
            project_path="/tmp/example",
            repository="owner/repo",
            issues=[7],
            overrides={"roles": {"reviewer": "security-reviewer"}, "gates": {"final": ["make verify"]}},
        )
        self.assertEqual(cfg["roles"]["researcher"], "researcher")
        self.assertEqual(cfg["roles"]["reviewer"], "security-reviewer")
        self.assertEqual(cfg["gates"]["final"], ["make verify"])

    def test_delivery_cards_bundle_plugin_skill_and_ponytail(self):
        cfg = ticket_flow.effective_config("/tmp/example", "owner/repo", [10], {})
        tasks = ticket_flow.build_task_specs(
            cfg,
            {10: {"number": 10, "title": "First", "url": "https://github.com/owner/repo/issues/10", "state": "OPEN"}},
        )
        by_stage = {task["stage"]: task for task in tasks}
        self.assertEqual(by_stage["research"]["skills"], ["github-ticket-flow:workflow"])
        self.assertEqual(by_stage["delivery"]["skills"], ["github-ticket-flow:workflow", "ponytail"])
        self.assertEqual(by_stage["merge"]["skills"], ["github-ticket-flow:workflow"])
        self.assertIn("hermes verify --json", by_stage["merge"]["body"])

    def test_manual_merge_checkpoint_is_default(self):
        cfg = ticket_flow.effective_config("/tmp/example", "owner/repo", [10], {})
        issue = {"number": 10, "title": "First", "url": "https://github.com/owner/repo/issues/10", "state": "OPEN"}
        merge = ticket_flow.build_task_specs(cfg, {10: issue})[-1]
        self.assertIn("block for user merge confirmation", merge["body"])

    def test_auto_merge_run_has_no_user_blocker(self):
        cfg = ticket_flow.effective_config(
            "/tmp/example",
            "owner/repo",
            [10],
            {"pipeline": {"require_user_merge_confirmation": False}},
        )
        issue = {"number": 10, "title": "First", "url": "https://github.com/owner/repo/issues/10", "state": "OPEN"}
        merge = ticket_flow.build_task_specs(cfg, {10: issue})[-1]
        self.assertNotIn("block for user merge confirmation", merge["body"])
        self.assertIn("merge automatically only after required GitHub checks pass", merge["body"])

    def test_merge_checkpoint_setting_must_be_boolean(self):
        with self.assertRaisesRegex(ValueError, "must be a boolean"):
            ticket_flow.effective_config(
                "/tmp/example",
                "owner/repo",
                [10],
                {"pipeline": {"require_user_merge_confirmation": "no"}},
            )

    def test_research_ahead_one_gates_third_research(self):
        cfg = ticket_flow.effective_config("/tmp/example", "owner/repo", [10, 20, 30], {})
        issues = {
            number: {"number": number, "title": str(number), "url": f"https://github.com/owner/repo/issues/{number}", "state": "OPEN"}
            for number in cfg["issues"]
        }
        tasks = ticket_flow.build_task_specs(cfg, issues)
        by_key = {task["key"]: task for task in tasks}
        self.assertEqual(by_key["research-10"]["parents"], [])
        self.assertEqual(by_key["research-20"]["parents"], [])
        self.assertEqual(by_key["research-30"]["parents"], ["merge-10"])
        self.assertEqual(by_key["delivery-20"]["parents"], ["research-20", "merge-10"])

    def test_manifest_captures_effective_config_and_cards(self):
        cfg = ticket_flow.effective_config("/tmp/example", "owner/repo", [10], {})
        created = [{"key": "research-10", "id": "t_abc"}]
        manifest = ticket_flow.build_manifest(cfg, created)
        self.assertEqual(manifest["manifest_version"], 1)
        self.assertEqual(manifest["effective_config"], cfg)
        self.assertEqual(manifest["cards"], created)

    @mock.patch.object(ticket_flow.subprocess, "run")
    def test_project_discovery_uses_git_root_and_github_remote(self, run):
        run.side_effect = [
            mock.Mock(stdout="/tmp/example\n"),
            mock.Mock(stdout="https://github.com/owner/repo.git\n"),
        ]
        project, repo = ticket_flow.discover_project("/tmp/example/src")
        self.assertEqual(project, "/tmp/example")
        self.assertEqual(repo, "owner/repo")

    def test_optional_config_must_be_an_object(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "ticket-flow.yaml"
            path.write_text("- not\n- an\n- object\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "object"):
                ticket_flow.load_optional_config(path)

    def test_plan_is_json_serializable(self):
        cfg = ticket_flow.effective_config("/tmp/example", "owner/repo", [10], {})
        issues = {10: {"number": 10, "title": "First", "url": "https://github.com/owner/repo/issues/10", "state": "OPEN"}}
        json.dumps(ticket_flow.visible_plan(cfg, issues))


if __name__ == "__main__":
    unittest.main()
