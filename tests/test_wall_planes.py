import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import wall_planes as report


TODAY = datetime(2026, 10, 23, 13, tzinfo=timezone.utc)


def issue(identifier, state="unstarted", **overrides):
    return {
        "id": identifier, "identifier": identifier, "title": "A <wall> & detail",
        "url": f"https://linear.app/higharc/issue/{identifier}",
        "createdAt": (TODAY - timedelta(days=2)).isoformat(),
        "completedAt": None, "canceledAt": None, "state": {"type": state},
        **overrides,
    }


def project(identifier, issues):
    return {"id": identifier, "name": f"Wall Planes {identifier}",
            "url": f"https://linear.app/higharc/project/{identifier}", "issues": issues}


class WallPlanesReportTests(unittest.TestCase):
    def test_counts_use_current_states_and_include_unestimated_triage(self):
        issues = [issue("triage", "triage"), issue("backlog", "backlog"),
                  issue("done", "completed"), issue("cancel", "canceled"),
                  issue("reopened", "started", completedAt=TODAY.isoformat())]
        self.assertEqual(report.counts(issues), {"open": 3, "completed": 1, "total": 4})
        self.assertEqual(report.recent(issues, "completedAt", TODAY), [])

    def test_weekly_window_excludes_canceled_future_and_exact_lower_boundary(self):
        issues = [issue("recent"), issue("old", createdAt=(TODAY - timedelta(days=7)).isoformat()),
                  issue("future", createdAt=(TODAY + timedelta(seconds=1)).isoformat()),
                  issue("cancel", "canceled"), issue("now", createdAt=TODAY.isoformat())]
        self.assertEqual([i["id"] for i in report.recent(issues, "createdAt", TODAY)], ["now", "recent"])

    def test_projects_remain_separate_and_empty_project_is_visible(self):
        projects = [project("Distributor", [issue("one")]), project("Enterprise", [])]
        result = report.build_report(projects, TODAY)
        self.assertEqual(result["totals"]["open"], 1)
        self.assertEqual(result["projects"][1]["total"], 0)
        payload = report.slack_payload(projects, result, "https://example.com/charts")
        text = str(payload)
        self.assertIn("Wall Planes Enterprise", text)
        self.assertIn("No tickets in scope yet", text)
        self.assertIn("A &lt;wall&gt; &amp; detail", text)
        self.assertNotIn("Townhomes", text)

    @patch("wall_planes.linear_query")
    def test_fetch_paginates_each_project_including_empty_and_deduplicates(self, query):
        def page(identifier, nodes, more=False, cursor=None):
            return {"project": {**project(identifier, []), "issues": {
                "nodes": nodes, "pageInfo": {"hasNextPage": more, "endCursor": cursor}}}}
        first, second = report.PROJECT_IDS
        query.side_effect = [page(first, [issue("one")], True, "next"),
                             page(first, [issue("one"), issue("two")]), page(second, [])]
        projects = report.fetch_projects("fake-test-key")
        self.assertEqual([len(p["issues"]) for p in projects], [2, 0])
        self.assertEqual(query.call_args_list[1].args[2], {"id": first, "after": "next"})
        self.assertEqual(query.call_args_list[2].args[2], {"id": second, "after": None})

    @patch("wall_planes.linear_query", return_value={"project": None})
    def test_inaccessible_project_fails_instead_of_silently_omitting_it(self, query):
        with self.assertRaisesRegex(RuntimeError, "Cannot access"):
            report.fetch_projects("fake-test-key")

    def test_history_excludes_unborn_completed_and_canceled(self):
        issues = [issue("open"), issue("done", "completed", completedAt=TODAY.isoformat()),
                  issue("cancel", "canceled", canceledAt=TODAY.isoformat()),
                  issue("future", createdAt=(TODAY + timedelta(days=1)).isoformat())]
        self.assertEqual(report.open_at(issues, TODAY), 1)
        self.assertEqual(report.open_at(issues, TODAY - timedelta(days=1)), 3)

    def test_large_weekly_lists_fit_slack_blocks(self):
        issues = [issue(str(i), title="<" * 1000) for i in range(50)]
        projects = [project("Distributor", issues), project("Enterprise", [])]
        payload = report.slack_payload(projects, report.build_report(projects, TODAY), "https://example.com")
        for block in payload["blocks"]:
            if block["type"] == "section":
                self.assertLessEqual(len(block["text"]["text"]), 3000)


if __name__ == "__main__":
    unittest.main()
