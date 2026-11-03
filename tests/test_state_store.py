"""Offline safeguards for durable cloud snapshots and history restoration."""
from contextlib import closing, redirect_stdout
import gzip
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zipfile

import requests


SPEC = importlib.util.spec_from_file_location(
    "state_store", Path(__file__).resolve().parents[1] / "scripts" / "state_store.py"
)
state = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(state)


def make_database(path, title="Existing history", *, codes=None):
    with closing(sqlite3.connect(path)) as con:
        con.executescript(
            "CREATE TABLE companies(code TEXT PRIMARY KEY);"
            "CREATE TABLE announcements(code TEXT, notice_date TEXT, title TEXT, url TEXT,"
            " PRIMARY KEY(code,url));"
            "CREATE TABLE financials(code TEXT, revenue REAL);"
        )
        pool = sorted(state.expected_codes() if codes is None else codes)
        con.executemany("INSERT INTO companies VALUES (?)", ((code,) for code in pool))
        if pool:
            con.execute(
                "INSERT INTO announcements VALUES (?,?,?,?)",
                (pool[0], "2026-11-03", title, "https://www.cninfo.com.cn/report/123"),
            )
            con.execute("INSERT INTO financials VALUES (?,?)", (pool[0], 123456.78))
        con.commit()


def archive_bytes(database, *, checksum=None, extras=()):
    payload = Path(database).read_bytes()
    manifest = {
        "format": 1,
        "database_sha256": checksum or hashlib.sha256(payload).hexdigest(),
    }
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("nev_stock.db", payload)
        archive.writestr("manifest.json", json.dumps(manifest))
        for name, content in extras:
            archive.writestr(name, content)
    return result.getvalue()


def history(path):
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as con:
        return {
            "announcements": con.execute("SELECT * FROM announcements ORDER BY code,url").fetchall(),
            "financials": con.execute("SELECT * FROM financials ORDER BY code").fetchall(),
            "companies": con.execute("SELECT code FROM companies ORDER BY code").fetchall(),
        }


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source.db"
        make_database(self.source)
        self.destination = self.root / "destination.db"
        make_database(self.destination, "Old destination must survive invalid input")
        self.old_destination = self.destination.read_bytes()
        self.seed = self.root / "seed.db.gz"
        with gzip.open(self.seed, "wb") as handle:
            handle.write(self.source.read_bytes())
        self.stdout = io.StringIO()

    def test_bundle_and_restore_preserve_all_tables_and_summary(self):
        summary = self.root / "last_run.json"
        summary.write_text('{"status":"ok","succeeded":20}', encoding="utf-8")
        output = self.root / "snapshot.zip"
        with redirect_stdout(self.stdout):
            manifest = state.bundle(self.source, summary, output)
            restored = state.restore_archive(output.read_bytes(), self.destination)
        self.assertEqual(restored, manifest)
        self.assertEqual(history(self.destination), history(self.source))
        self.assertEqual(len(history(self.destination)["companies"]), 20)
        self.assertEqual(manifest["database_sha256"], hashlib.sha256(self.destination.read_bytes()).hexdigest())
        with zipfile.ZipFile(output) as archive:
            self.assertEqual(archive.read("last_run.json"), summary.read_bytes())
            self.assertEqual(set(archive.namelist()), {"nev_stock.db", "manifest.json", "last_run.json"})

    def test_checksum_damage_never_replaces_existing_database(self):
        with self.assertRaisesRegex(ValueError, "checksum"):
            state.restore_archive(archive_bytes(self.source, checksum="0" * 64), self.destination)
        self.assertEqual(self.destination.read_bytes(), self.old_destination)

    def test_unexpected_and_traversing_zip_members_are_rejected(self):
        for member in ("../escape.txt", "/absolute.db", "nested/nev_stock.db", "unexpected.txt"):
            with self.subTest(member=member):
                payload = archive_bytes(self.source, extras=[(member, b"untrusted")])
                with self.assertRaisesRegex(ValueError, "unexpected"):
                    state.restore_archive(payload, self.destination)
                self.assertEqual(self.destination.read_bytes(), self.old_destination)
                self.assertFalse((self.root / "escape.txt").exists())

    def test_valid_checksum_with_wrong_company_pool_is_rejected(self):
        wrong = self.root / "wrong-pool.db"
        make_database(wrong, codes={"000001"})
        with self.assertRaisesRegex(ValueError, "company pool"):
            state.restore_archive(archive_bytes(wrong), self.destination)
        self.assertEqual(self.destination.read_bytes(), self.old_destination)

    def test_api_failure_never_falls_back_to_seed(self):
        github = Mock(spec=state.GitHub)
        github.pages.side_effect = requests.HTTPError("GitHub API is unavailable")
        with patch.object(state.gzip, "open") as seed_open:
            with self.assertRaises(requests.HTTPError):
                state.restore(self.destination, self.seed, github)
            seed_open.assert_not_called()
        self.assertEqual(self.destination.read_bytes(), self.old_destination)

    def test_first_run_without_releases_restores_verified_seed(self):
        github = Mock(spec=state.GitHub)
        github.pages.return_value = []
        with redirect_stdout(self.stdout):
            state.restore(self.destination, self.seed, github)
        self.assertEqual(history(self.destination), history(self.source))
        github.call.assert_not_called()

    def test_newest_bad_snapshot_falls_back_to_older_valid_snapshot(self):
        github = Mock(spec=state.GitHub)
        newer = {"id": 2, "state": "uploaded", "name": "nev-state-20261104T090000Z-2-1.zip"}
        older = {"id": 1, "state": "uploaded", "name": "nev-state-20261103T090000Z-1-1.zip"}

        def pages(path):
            if path == "/releases":
                return [{"id": 10, "tag_name": "database-2026-11"}]
            self.assertEqual(path, "/releases/10/assets")
            return [older, newer]

        def call(method, path, **kwargs):
            self.assertEqual(method, "GET")
            self.assertEqual(kwargs["headers"]["Accept"], "application/octet-stream")
            if path == "/releases/assets/2":
                return SimpleNamespace(content=archive_bytes(self.source, checksum="0" * 64))
            self.assertEqual(path, "/releases/assets/1")
            return SimpleNamespace(content=archive_bytes(self.source))

        github.pages.side_effect = pages
        github.call.side_effect = call
        with redirect_stdout(self.stdout), patch.object(state.gzip, "open") as seed_open:
            state.restore(self.destination, self.seed, github)
            seed_open.assert_not_called()
        self.assertEqual(history(self.destination), history(self.source))
        self.assertEqual([args.args[1] for args in github.call.call_args_list],
                         ["/releases/assets/2", "/releases/assets/1"])

    def test_all_bad_snapshots_fail_without_resetting_history(self):
        github = Mock(spec=state.GitHub)
        github.pages.side_effect = [
            [{"id": 10, "tag_name": "database-2026-11"}],
            [{"id": 2, "state": "uploaded", "name": "nev-state-20261104T090000Z-2-1.zip"}],
        ]
        github.call.return_value = SimpleNamespace(content=b"not a snapshot")
        with redirect_stdout(self.stdout), patch.object(state.gzip, "open") as seed_open:
            with self.assertRaisesRegex(RuntimeError, "refusing to reset history"):
                state.restore(self.destination, self.seed, github)
            seed_open.assert_not_called()
        self.assertEqual(self.destination.read_bytes(), self.old_destination)

    def test_existing_empty_database_release_does_not_reset_to_seed(self):
        github = Mock(spec=state.GitHub)
        github.pages.side_effect = [[{"id": 10, "tag_name": "database-2026-11"}], []]
        with patch.object(state.gzip, "open") as seed_open:
            with self.assertRaisesRegex(RuntimeError, "refusing to reset history"):
                state.restore(self.destination, self.seed, github)
            seed_open.assert_not_called()
        self.assertEqual(self.destination.read_bytes(), self.old_destination)


if __name__ == "__main__":
    unittest.main()
