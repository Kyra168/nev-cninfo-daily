"""Offline checks for persistence, failure reporting and malformed CNINFO pages."""
from contextlib import closing, redirect_stdout, redirect_stderr
import datetime as dt
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock

SPEC = importlib.util.spec_from_file_location("collector", Path(__file__).resolve().parents[1] / "scripts" / "collect_cninfo.py")
collector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collector)
DAY = dt.date(2026, 11, 3)


def announcement(code="000001", ident="123"):
    return {
        "secCode": code, "orgId": "org1", "announcementId": ident,
        "announcementTitle": "<em>公告</em> &amp; 报告",
        "announcementTime": int(dt.datetime(2026, 11, 3, tzinfo=collector.CHINA_TIME).timestamp() * 1000),
    }


def response(data):
    result = Mock()
    result.json.return_value = data
    return result


class ParsingTests(unittest.TestCase):
    def test_zero_announcements_is_success(self):
        session = Mock()
        session.request.return_value = response({"totalAnnouncement": 0, "announcements": None})
        client = collector.CninfoClient(session=session)
        self.assertEqual(client.fetch_company("000001", "org1", DAY, DAY), [])

    def test_incomplete_page_is_failure_not_empty_success(self):
        session = Mock()
        session.request.return_value = response({"totalAnnouncement": 2, "announcements": [announcement()]})
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            collector.CninfoClient(session=session).fetch_company("000001", "org1", DAY, DAY)

    def test_invalid_company_and_timestamp_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "mismatch"):
            collector.CninfoClient._record(announcement("000002"), "000001", DAY, DAY)
        invalid = announcement()
        invalid["announcementTime"] = float("nan")
        with self.assertRaisesRegex(ValueError, "finite"):
            collector.CninfoClient._record(invalid, "000001", DAY, DAY)

    def test_link_matches_original_akshare_format(self):
        row = collector.CninfoClient._record(announcement(), "000001", DAY, DAY)
        self.assertEqual(row[1], "2026-11-03")
        self.assertEqual(row[2], "公告 & 报告")
        self.assertEqual(row[3], "http://www.cninfo.com.cn/new/disclosure/detail?stockCode=000001&announcementId=123&orgId=org1&announcementTime=2026-11-03 00:00:00")

    def test_network_retries_are_bounded(self):
        session = Mock()
        session.request.side_effect = collector.requests.Timeout("offline")
        sleeper = Mock()
        client = collector.CninfoClient(session=session, attempts=3, sleeper=sleeper)
        with self.assertRaisesRegex(RuntimeError, "bounded retries"):
            client.stock_map()
        self.assertEqual(session.request.call_count, 3)
        self.assertEqual(sleeper.call_count, 2)

    def test_all_pages_are_fetched(self):
        session = Mock()
        session.request.side_effect = [
            response({"totalAnnouncement": 31, "announcements": [announcement(ident=str(i)) for i in range(30)]}),
            response({"totalAnnouncement": 31, "announcements": [announcement(ident="30")]}),
        ]
        rows = collector.CninfoClient(session=session).fetch_company("000001", "org1", DAY, DAY)
        self.assertEqual(len(rows), 31)
        self.assertEqual(session.request.call_count, 2)


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "nev_stock.db"
        self.summary = self.root / "last_run.json"
        self.pool = self.root / "companies.json"
        self.codes = [f"{index:06d}" for index in range(1, 21)]
        self.pool.write_text(json.dumps({"companies": [{"code": code, "name": "Company " + code} for code in self.codes]}), encoding="utf-8")

    def run_collection(self, client):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return collector.run_collection(self.db, self.pool, self.summary, client=client, company_delay=0, end_date=DAY)

    def fake_client(self):
        client = Mock()
        client.stock_map.return_value = {code: "org1" for code in self.codes}
        client.fetch_company.side_effect = lambda code, org, start, end: [collector.CninfoClient._record(announcement(code), code, start, end)]
        return client

    def test_rerun_keeps_one_copy_and_preserves_other_tables(self):
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE daily_quotes (code TEXT, close REAL)")
        conn.execute("INSERT INTO daily_quotes VALUES ('000001', 5)")
        conn.commit()
        conn.close()
        self.assertEqual(self.run_collection(self.fake_client()), 0)
        self.assertEqual(self.run_collection(self.fake_client()), 0)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM announcements").fetchone()[0], 20)
            self.assertEqual(conn.execute("SELECT close FROM daily_quotes").fetchone()[0], 5)
        report = json.loads(self.summary.read_text(encoding="utf-8"))
        self.assertEqual(report["succeeded"], 20)
        self.assertEqual(report["failed"], 0)
        self.assertTrue(report["finished_at"].endswith("+08:00"))

    def test_partial_failure_persists_success_and_nonzero_status(self):
        client = self.fake_client()
        def fetch(code, org, start, end):
            if code == "000002":
                raise TimeoutError("upstream unavailable")
            return [collector.CninfoClient._record(announcement(code), code, start, end)]
        client.fetch_company.side_effect = fetch
        self.assertEqual(self.run_collection(client), 1)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM announcements").fetchone()[0], 19)
            self.assertEqual(conn.execute("SELECT status FROM crawl_state WHERE code='000002'").fetchone()[0], "fail")
        report = json.loads(self.summary.read_text(encoding="utf-8"))
        self.assertEqual((report["succeeded"], report["failed"]), (19, 1))

    def test_map_failure_does_not_clear_saved_data(self):
        self.assertEqual(self.run_collection(self.fake_client()), 0)
        broken = Mock()
        broken.stock_map.side_effect = ValueError("bad response")
        self.assertEqual(self.run_collection(broken), 1)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM announcements").fetchone()[0], 20)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM crawl_state WHERE status='fail'").fetchone()[0], 20)
        self.assertFalse(broken.fetch_company.called)

    def test_pool_must_contain_twenty_distinct_companies(self):
        self.pool.write_text('{"companies": []}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "exactly 20"):
            self.run_collection(self.fake_client())
        self.assertFalse(self.db.exists())


if __name__ == "__main__":
    unittest.main()
