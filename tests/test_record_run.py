"""Offline checks for run status publishing and GitHub SHA protection."""
from contextlib import redirect_stdout
import base64
from datetime import datetime, timezone
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError


SPEC = importlib.util.spec_from_file_location(
    "record_run", Path(__file__).resolve().parents[1] / "scripts" / "record_run.py"
)
record = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(record)
ENV = {
    "GITHUB_REPOSITORY": "example/nev-cninfo-daily",
    "GITHUB_TOKEN": "test-only-not-a-real-token",
    "GITHUB_RUN_ID": "123",
    "GITHUB_RUN_ATTEMPT": "2",
    "GITHUB_REF_NAME": "main",
    "GITHUB_SHA": "a" * 40,
    "COLLECTION_OUTCOME": "success",
    "PUBLISH_OUTCOME": "success",
}
INSTANT = datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc)


class RunStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.summary = Path(self.temp.name) / "summary.json"
        self.summary.write_text(json.dumps({
            "status": "ok", "succeeded": 20, "failed": 0, "rows_fetched": 1234,
            "database_integrity": "ok", "finished_at": "2026-11-03T17:03:00+08:00",
        }), encoding="utf-8")

    def test_started_record_identifies_run_and_beijing_time(self):
        result = record.build_record("started", self.summary, ENV, instant=INSTANT)
        self.assertEqual(result["status"], "running")
        self.assertEqual(result["updated_at_beijing"], "2026-11-03T17:00:00+08:00")
        self.assertEqual(result["run_url"], "https://github.com/example/nev-cninfo-daily/actions/runs/123")
        self.assertEqual(result["sha"], "a" * 40)
        self.assertNotIn(ENV["GITHUB_TOKEN"], json.dumps(result))

    def test_finished_only_succeeds_when_collection_and_persistence_succeed(self):
        success = record.build_record("finished", self.summary, ENV, instant=INSTANT)
        self.assertEqual(success["status"], "success")
        self.assertEqual((success["succeeded"], success["failed"], success["rows_fetched"]), (20, 0, 1234))
        for key in ("COLLECTION_OUTCOME", "PUBLISH_OUTCOME"):
            with self.subTest(key=key):
                result = record.build_record("finished", self.summary, {**ENV, key: "failure"})
                self.assertEqual(result["status"], "failure")

    def test_missing_summary_cannot_claim_success(self):
        result = record.build_record("finished", self.summary.with_name("missing.json"), ENV)
        self.assertEqual(result["status"], "failure")
        self.assertIsNone(result["succeeded"])
        self.assertIn("summary_error", result)

    def test_first_404_creates_without_sha_or_token_in_content(self):
        missing = HTTPError("https://api.github.com/status", 404, "Not found", None, None)
        with patch.object(record, "request_json", side_effect=[missing, {"content": {"sha": "new-sha"}}]) as api:
            with redirect_stdout(io.StringIO()):
                record.write_record("started", self.summary, ENV, instant=INSTANT)
        self.assertEqual(api.call_count, 2)
        request = api.call_args_list[1].args[0]
        self.assertEqual(request.method, "PUT")
        body = json.loads(request.data)
        self.assertNotIn("sha", body)
        content = base64.b64decode(body["content"]).decode()
        self.assertNotIn(ENV["GITHUB_TOKEN"], content)
        self.assertEqual(json.loads(content)["status"], "running")

    def test_existing_file_uses_confirmed_sha_and_keeps_start_time(self):
        earlier = record.build_record("started", self.summary, ENV, instant=INSTANT)
        existing = {
            "sha": "known-file-sha", "encoding": "base64",
            "content": base64.b64encode(json.dumps(earlier).encode()).decode(),
        }
        with patch.object(record, "request_json", side_effect=[existing, {"content": {"sha": "new-sha"}}]) as api:
            with redirect_stdout(io.StringIO()):
                result = record.write_record("finished", self.summary, ENV, instant=INSTANT)
        body = json.loads(api.call_args_list[1].args[0].data)
        self.assertEqual(body["sha"], "known-file-sha")
        self.assertEqual(result["started_at_utc"], earlier["started_at_utc"])
        self.assertEqual(result["status"], "success")

    def test_non_404_network_error_does_not_create_or_overwrite(self):
        denied = HTTPError("https://api.github.com/status", 503, "Unavailable", None, None)
        with patch.object(record, "request_json", side_effect=denied) as api:
            with self.assertRaises(HTTPError):
                record.write_record("started", self.summary, ENV)
        self.assertEqual(api.call_count, 1)

    def test_unconfirmed_file_sha_stops_before_put(self):
        with patch.object(record, "request_json", return_value={"encoding": "base64", "content": "e30="}) as api:
            with self.assertRaisesRegex(ValueError, "file SHA"):
                record.write_record("started", self.summary, ENV)
        self.assertEqual(api.call_count, 1)


if __name__ == "__main__":
    unittest.main()
