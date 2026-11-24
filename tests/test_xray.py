import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import xray


class CLI(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.env = self.enterContext(patch.dict(os.environ, {
            "KIAO_WORKER_ROOT_PATH": str(self.tmp), "KIAO_WORKER_STATE_PATH": str(self.state),
        }))
        self.token = "fake.token-for-tests"
        self.configure({"region": "eu", "token": self.token})
        self.opener = self.enterContext(patch("xray_client.build_opener")).return_value

    def configure(self, credentials):
        (self.state / "atlassian.json").write_text(json.dumps({
            "jira": {"token": "unrelated-token"}, "xray": credentials,
        }), encoding="utf-8")

    def invoke(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = xray.main(list(map(str, args)))
        message = stdout.getvalue() or stderr.getvalue()
        self.assertNotIn(self.token, message)
        return code, json.loads(message)

    def file(self, name, data):
        path = self.tmp / name
        path.write_text(data, encoding="utf-8")
        return path

    def reply(self, *bodies):
        self.opener.open.side_effect = [
            io.BytesIO(body if isinstance(body, bytes) else json.dumps(body).encode()) for body in bodies
        ]

    def request(self, index=0):
        return self.opener.open.call_args_list[index].args[0]

    def test_search_uses_bearer_and_returns_one_page(self):
        self.reply({"data": {"getTests": {"total": 12, "start": 2, "results": [
            {"issueId": "1001", "jira": {"key": "QA-1", "summary": self.token}},
            {"issueId": "1002", "jira": {"key": "QA-2"}},
        ]}}})
        self.assertEqual(self.invoke("search", "test", "--jql", "project = QA", "--limit", 2, "--start", 2), (0, {
            "items": [
                {"issueId": "1001", "jira": {"key": "QA-1", "summary": "[redacted]"}},
                {"issueId": "1002", "jira": {"key": "QA-2"}},
            ], "total": 12, "nextStart": 4,
        }))
        self.assertEqual(self.opener.open.call_count, 1)
        request = self.request()
        self.assertEqual(request.full_url, "https://eu.xray.cloud.getxray.app/api/v2/graphql")
        self.assertEqual(request.get_header("Authorization"), f"Bearer {self.token}")
        body = json.loads(request.data)
        self.assertNotIn("steps", body["query"])
        self.assertEqual(body["variables"], {"getTests_jql": "project = QA", "getTests_limit": 2, "getTests_start": 2})

    def test_api_key_exchange_reads_the_default_state_directory(self):
        self.env["KIAO_WORKER_STATE_PATH"] = ""
        self.state = self.tmp / ".kiao-config"
        self.state.mkdir()
        self.configure({"client_id": "fake-id", "client_secret": "fake-secret"})
        self.reply(self.token, {"data": {"getStatuses": [{"name": "PASSED"}]}})
        self.assertEqual(self.invoke("statuses"), (0, {"items": [{"name": "PASSED"}]}))
        auth, query = self.request(), self.request(1)
        self.assertEqual(auth.full_url, "https://xray.cloud.getxray.app/api/v2/authenticate")
        self.assertIsNone(auth.get_header("Authorization"))
        self.assertEqual(json.loads(auth.data), {"client_id": "fake-id", "client_secret": "fake-secret"})
        self.assertEqual(query.get_header("Authorization"), f"Bearer {self.token}")

    def test_invalid_configuration_is_rejected_before_http(self):
        self.configure({"region": "elsewhere", "token": self.token})
        code, result = self.invoke("statuses")
        self.assertEqual(code, 2)
        self.assertIn("xray.region", result["error"])
        self.opener.open.assert_not_called()

    def test_related_resolves_key_and_paginates_in_one_request(self):
        self.reply({"data": {"getCoverableIssues": {"total": 1, "start": 0, "results": [
            {"jira": {"key": "QA-11"}, "tests": {"total": 3, "start": 1, "results": [{"issueId": "3"}]}},
        ]}}})
        self.assertEqual(self.invoke("related", "requirement", "qa-11", "tests", "--limit", 1, "--start", 1),
                         (0, {"items": [{"issueId": "3"}], "total": 3, "nextStart": 2}))
        body = json.loads(self.request().data)
        self.assertIn("tests(limit:1,start:1)", body["query"])
        self.assertEqual(body["variables"]["getCoverableIssues_jql"], "key in (QA-11)")
        self.assertEqual(self.opener.open.call_count, 1)

    def test_link_batches_keys_and_removes_duplicate_ids(self):
        self.reply({"data": {
            "refs0": {"results": [{"issueId": "101", "jira": {"key": "QA-10"}}]},
            "refs1": {"results": [{"issueId": "201", "jira": {"key": "QA-20"}}]},
        }}, {"data": {"addTestsToTestExecution": {"addedTests": ["201", "202"], "warning": None}}})
        code, result = self.invoke("link", "add", "execution", "QA-10", "tests", "QA-20", "201", "202")
        self.assertEqual(code, 0)
        self.assertEqual(result["addedTests"], ["201", "202"])
        lookup = json.loads(self.request().data)
        self.assertIn("refs0:getTestExecutions", lookup["query"])
        self.assertIn("refs1:getTests", lookup["query"])
        body = json.loads(self.request(1).data)
        self.assertEqual(body["variables"], {
            "addTestsToTestExecution_issueId": "101", "addTestsToTestExecution_testIssueIds": ["201", "202"],
        })
        self.assertEqual(self.opener.open.call_count, 2)

    def test_missing_link_key_prevents_mutation(self):
        self.reply({"data": {"refs0": {"results": []}}})
        code, result = self.invoke("link", "add", "plan", "1001", "tests", "QA-100")
        self.assertEqual(code, 1)
        self.assertIn("QA-100", result["error"])
        self.assertEqual(self.opener.open.call_count, 1)
        self.assertTrue(json.loads(self.request().data)["query"].startswith("query"))

    def test_create_manual_test_uses_optional_fields_and_known_ids(self):
        steps = self.file("steps.json", '[{"action":"Open checkout","result":"Cart visible"}]')
        fields = self.file("fields.json", '{"labels":["smoke"]}')
        self.reply({"data": {"createTest": {"test": {"issueId": "101", "jira": {"key": "QA-10"}}, "warnings": []}}})
        code, result = self.invoke("create", "test", "qa", "Checkout", "--steps", steps, "--type", "Manual",
                                   "--preconditions", "200", "--fields", fields)
        self.assertEqual((code, result["test"]["issueId"]), (0, "101"))
        variables = json.loads(self.request().data)["variables"]
        self.assertEqual(variables["createTest_jira"]["fields"], {
            "project": {"key": "QA"}, "summary": "Checkout", "labels": ["smoke"],
        })
        self.assertEqual(variables["createTest_preconditionIssueIds"], ["200"])
        self.assertEqual(variables["createTest_steps"], [{"action": "Open checkout", "result": "Cart visible"}])

    def test_graphql_partial_error_never_reports_success_or_retries(self):
        self.reply({"data": {"updateTestRunStatus": "PASSED"}, "errors": [{"message": f"bad {self.token}"}]})
        code, result = self.invoke("record", "run-42", "--status", "PASSED")
        self.assertEqual(code, 1)
        self.assertIn("bad [redacted]", result["error"])
        self.assertEqual(self.opener.open.call_count, 1)

    def test_rate_limit_reports_retry_after_without_retrying(self):
        self.opener.open.side_effect = HTTPError(
            "https://eu.xray.cloud.getxray.app/api/v2/graphql", 429, "rate limit", {"Retry-After": "18"},
            io.BytesIO(json.dumps({"message": self.token}).encode()),
        )
        code, result = self.invoke("statuses")
        self.assertEqual((code, result["status"], result["retryAfter"]), (1, 429, "18"))
        self.assertEqual(self.opener.open.call_count, 1)

    def test_result_validation_runs_without_credentials_or_http(self):
        (self.state / "atlassian.json").unlink()
        report = self.file("report.json", '{"tests":[{"testKey":"QA-17","status":"CUSTOM"}]}')
        self.assertEqual(self.invoke("import-results", "xray", report, "--validate-only"),
                         (0, {"format": "xray", "valid": True, "tests": 1}))
        invalid = self.file("invalid.xml", "<robot/>")
        self.assertEqual(self.invoke("import-results", "junit", invalid)[0], 2)
        self.opener.open.assert_not_called()

    def test_junit_upload_sends_original_report_and_destination(self):
        xml = "<testsuite><testcase name='cart'/></testsuite>"
        report = self.file("report.xml", xml)
        self.reply({"id": "800", "key": "QA-31", "self": "https://example.com/rest/api/2/issue/800"})
        self.assertEqual(self.invoke("import-results", "junit", report, "--execution", "QA-31", "--environment", "chrome"),
                         (0, {"id": "800", "key": "QA-31"}))
        request = self.request()
        url = urlsplit(request.full_url)
        self.assertEqual(url.path, "/api/v2/import/execution/junit")
        self.assertEqual(parse_qs(url.query), {"testExecKey": ["QA-31"], "testEnvironments": ["chrome"]})
        self.assertEqual(request.get_header("Content-type"), "text/xml")
        self.assertEqual(request.data, xml.encode())

    def test_bulk_import_and_status_keep_output_compact(self):
        payload = [{"testtype": "Manual", "fields": {"summary": "Cart", "project": {"key": "QA"}}}]
        file = self.file("tests.json", json.dumps(payload))
        self.reply({"jobId": "job-1"}, {"status": "successful", "progress": ["ignored"], "result": {
            "issues": [{"key": "QA-20"}], "errors": [],
        }})
        self.assertEqual(self.invoke("import-tests", file), (0, {"jobId": "job-1"}))
        self.assertEqual(json.loads(self.request().data), payload)
        self.assertEqual(self.invoke("import-status", "job-1"), (0, {
            "status": "successful", "issueCount": 1, "sampleKeys": ["QA-20"], "errorCount": 0,
        }))
        self.assertTrue(self.request(1).full_url.endswith("/api/v2/import/test/bulk/job-1/status"))

    def test_export_saves_zip_and_checks_existing_path_before_request(self):
        archive = io.BytesIO()
        with ZipFile(archive, mode="w") as output:
            output.writestr("cart.feature", "Feature: Cart")
        self.reply(archive.getvalue())
        path = self.tmp / "features.zip"
        self.assertEqual(self.invoke("export-cucumber", path, "--keys", "QA-17", "QA-18")[0], 0)
        self.assertEqual(path.read_bytes(), archive.getvalue())
        self.assertEqual(parse_qs(urlsplit(self.request().full_url).query), {"keys": ["QA-17;QA-18"]})
        self.assertEqual(self.invoke("export-cucumber", path, "--keys", "QA-17")[0], 2)
        self.assertEqual(self.opener.open.call_count, 1)


if __name__ == "__main__":
    unittest.main()
