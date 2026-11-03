"""Persist SQLite snapshots as immutable assets in a private GitHub repository.

Authentication uses only the runner's short-lived GITHUB_TOKEN. No passwords.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import uuid
import zipfile

import requests

ROOT = Path(__file__).resolve().parents[1]
ASSET_PATTERN = re.compile(r"^nev-state-\d{8}T\d{6}Z-[A-Za-z0-9_-]+\.zip$")
RELEASE_PATTERN = re.compile(r"^database-\d{4}-\d{2}$")
MAX_DATABASE_SIZE = 256 * 1024 * 1024


def expected_codes():
    data = json.loads((ROOT / "precrawler" / "companies.json").read_text(encoding="utf-8"))
    return {str(c["code"]) for c in data["companies"]}


def check_database(path):
    con = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        if con.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("SQLite integrity check failed")
        codes = {r[0] for r in con.execute("SELECT code FROM companies")}
        if codes != expected_codes():
            raise ValueError("Database company pool differs from the configured 20 companies")
        con.execute("SELECT code, notice_date, title, url FROM announcements LIMIT 1")
    finally:
        con.close()


class GitHub:
    def __init__(self, repository=None, token=None):
        self.repository = repository or os.environ.get("GITHUB_REPOSITORY", "")
        token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository):
            raise ValueError("Set GITHUB_REPOSITORY to owner/repository")
        if not token:
            raise ValueError("A GitHub token from the authorized connection or workflow is required")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2026-03-10",
            "User-Agent": "NEV-CNINFO-Daily/1.0",
        })

    def call(self, method, path, **kwargs):
        response = self.session.request(
            method, "https://api.github.com/repos/" + self.repository + path,
            timeout=(15, 90), **kwargs,
        )
        response.raise_for_status()
        return response

    def pages(self, path):
        page = 1
        separator = "&" if "?" in path else "?"
        while True:
            rows = self.call("GET", f"{path}{separator}per_page=100&page={page}").json()
            if not isinstance(rows, list):
                raise ValueError("Unexpected GitHub pagination response")
            yield from rows
            if len(rows) < 100:
                break
            page += 1


def restore_archive(payload, destination):
    """Validate every relevant byte before replacing the working database."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("Snapshot contains duplicate archive members")
        if set(names) - {"nev_stock.db", "manifest.json", "last_run.json"}:
            raise ValueError("Snapshot contains unexpected archive members")
        for required in ("nev_stock.db", "manifest.json"):
            if required not in names:
                raise ValueError("Snapshot is missing " + required)
        if archive.getinfo("nev_stock.db").file_size > MAX_DATABASE_SIZE:
            raise ValueError("Snapshot exceeds configured database size limit")
        if archive.getinfo("manifest.json").file_size > 1024 * 1024:
            raise ValueError("Snapshot manifest is too large")
        manifest = json.loads(archive.read("manifest.json"))
        database = archive.read("nev_stock.db")
        if manifest.get("database_sha256") != hashlib.sha256(database).hexdigest():
            raise ValueError("Database snapshot checksum mismatch")
    with tempfile.TemporaryDirectory(dir=destination.parent) as scratch:
        temporary = Path(scratch) / "checked.db"
        temporary.write_bytes(database)
        check_database(temporary)
        # Cloud restore only targets an inactive working file, never the user's live D: database.
        os.replace(temporary, destination)
    return manifest


def restore(db, seed, github):
    releases = [r for r in github.pages("/releases")
                if RELEASE_PATTERN.fullmatch(r.get("tag_name", ""))]
    releases.sort(key=lambda r: r["tag_name"], reverse=True)
    candidates = []
    for release in releases:
        assets = [a for a in github.pages(f"/releases/{release['id']}/assets")
                  if a.get("state") == "uploaded"
                  and ASSET_PATTERN.fullmatch(a.get("name", ""))]
        candidates.extend(sorted(assets, key=lambda a: a["name"], reverse=True))
        if len(candidates) >= 3:
            break
    if candidates:
        for asset in candidates[:3]:
            try:
                response = github.call("GET", f"/releases/assets/{asset['id']}",
                                       headers={"Accept": "application/octet-stream"})
                restore_archive(response.content, db)
                print("Restored persistent snapshot:", asset["name"])
                return
            except (ValueError, OSError, sqlite3.Error, requests.RequestException,
                    zipfile.BadZipFile, KeyError) as exc:
                print(f"Could not restore {asset['name']}: {type(exc).__name__}")
        raise RuntimeError("Persistent snapshots exist but none passed validation; refusing to reset history")
    if releases:
        raise RuntimeError("A database release exists without a usable snapshot; refusing to reset history")
    db = Path(db)
    db.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(seed, "rb") as source, tempfile.NamedTemporaryFile(
        dir=db.parent, suffix=".db", delete=False
    ) as target:
        temporary = Path(target.name)
        shutil.copyfileobj(source, target)
    try:
        check_database(temporary)
        os.replace(temporary, db)
    finally:
        temporary.unlink(missing_ok=True)
    print("First run: restored the verified original database seed")


def bundle(db, summary, output):
    db, summary, output = Path(db), Path(summary), Path(output)
    check_database(db)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as scratch:
        snapshot = Path(scratch) / "nev_stock.db"
        source = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
        target = sqlite3.connect(snapshot)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        check_database(snapshot)
        manifest = {
            "format": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "database_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
            "company_count": len(expected_codes()),
            "source": "CNINFO announcements",
        }
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(snapshot, "nev_stock.db")
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            if summary.is_file():
                archive.write(summary, "last_run.json")
    print("Created validated database snapshot:", output.name)
    return manifest


def publish(archive_path, github):
    archive_path = Path(archive_path)
    # Validate before making any external change.
    with tempfile.TemporaryDirectory() as scratch:
        restore_archive(archive_path.read_bytes(), Path(scratch) / "check.db")
    now = datetime.now(timezone.utc)
    tag = now.strftime("database-%Y-%m")
    try:
        release = github.call("GET", "/releases/tags/" + tag).json()
    except requests.HTTPError as exc:
        if exc.response.status_code != 404:
            raise
        release = github.call("POST", "/releases", json={
            "tag_name": tag,
            "target_commitish": os.environ.get("GITHUB_SHA", "main"),
            "name": "NEV 数据库备份 " + now.strftime("%Y-%m"),
            "body": "每次运行生成独立快照，保留原始数据与抓取状态。",
            "draft": False,
            "prerelease": True,
            "make_latest": "false",
        }).json()
    suffix = os.environ.get("GITHUB_RUN_ID", uuid.uuid4().hex[:12])
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", suffix + "-" + attempt):
        raise ValueError("Invalid workflow run identifier")
    name = now.strftime("nev-state-%Y%m%dT%H%M%SZ-") + suffix + "-" + attempt + ".zip"
    upload_url = f"https://uploads.github.com/repos/{github.repository}/releases/{release['id']}/assets"
    # A new unique name never deletes or overwrites an older database snapshot.
    with archive_path.open("rb") as payload:
        response = github.session.post(upload_url, params={"name": name}, data=payload,
                                       headers={"Content-Type": "application/zip"},
                                       timeout=(15, 180))
    response.raise_for_status()
    asset = response.json()
    if asset.get("state") != "uploaded" or asset.get("size") != archive_path.stat().st_size:
        raise RuntimeError("GitHub did not confirm a complete snapshot upload")
    local_digest = "sha256:" + hashlib.sha256(archive_path.read_bytes()).hexdigest()
    if asset.get("digest") and asset["digest"] != local_digest:
        raise RuntimeError("Uploaded snapshot checksum does not match the local archive")
    print("Persistent snapshot saved:", asset.get("browser_download_url", name))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["restore", "bundle", "publish"])
    parser.add_argument("--db", default="data/nev_stock.db")
    parser.add_argument("--summary", default="data/last_run.json")
    parser.add_argument("--seed", default="seed/nev_stock.db.gz")
    parser.add_argument("--archive", default="data/nev-data.zip")
    parser.add_argument("--repository")
    args = parser.parse_args()
    if args.action == "bundle":
        bundle(args.db, args.summary, args.archive)
    elif args.action == "restore":
        restore(args.db, args.seed, GitHub(args.repository))
    else:
        publish(args.archive, GitHub(args.repository))


if __name__ == "__main__":
    main()
