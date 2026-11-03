"""Write a small durable run status using the workflow's short-lived token."""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


CHINA_TIME = timezone(timedelta(hours=8), "Asia/Shanghai")
API_ROOT = "https://api.github.com"
STATUS_PATH = "status/latest.json"


def build_record(stage, summary_path, env, previous=None, instant=None):
    if stage not in {"started", "finished"}:
        raise ValueError("stage must be started or finished")
    instant = instant or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise ValueError("Run timestamp must include a timezone")
    repository = env.get("GITHUB_REPOSITORY", "")
    run_id = env.get("GITHUB_RUN_ID", "")
    attempt = env.get("GITHUB_RUN_ATTEMPT", "1")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("GITHUB_REPOSITORY must identify owner/repository")
    if not run_id.isdigit() or not attempt.isdigit():
        raise ValueError("GitHub run ID and attempt must be numeric")
    utc_time = instant.astimezone(timezone.utc).isoformat(timespec="seconds")
    record = {
        "format": 1,
        "stage": stage,
        "status": "running" if stage == "started" else "failure",
        "repository": repository,
        "workflow": env.get("GITHUB_WORKFLOW", ""),
        "run_id": run_id,
        "run_attempt": attempt,
        "sha": env.get("GITHUB_SHA", ""),
        "run_url": f"https://github.com/{repository}/actions/runs/{run_id}",
        "timezone": "Asia/Shanghai",
        "schedule": "Every day at 17:00 Asia/Shanghai (09:00 UTC)",
        "updated_at_utc": utc_time,
        "updated_at_beijing": instant.astimezone(CHINA_TIME).isoformat(timespec="seconds"),
    }
    previous = previous or {}
    if stage == "started":
        record["started_at_utc"] = utc_time
        return record
    if previous.get("run_id") == run_id and previous.get("run_attempt") == attempt:
        record["started_at_utc"] = previous.get("started_at_utc")
    record["finished_at_utc"] = utc_time
    record["collection_outcome"] = env.get("COLLECTION_OUTCOME", "")
    record["publish_outcome"] = env.get("PUBLISH_OUTCOME", "")
    record.update(succeeded=None, failed=None, rows_fetched=None)
    try:
        summary = json.loads(Path(summary_path).read_text(encoding="utf-8"))
        if not isinstance(summary, dict):
            raise ValueError("Collection summary must be an object")
        for key in ("succeeded", "failed", "rows_fetched"):
            value = summary.get(key)
            if type(value) is not int or value < 0:
                raise ValueError("Invalid collection summary count: " + key)
            record[key] = value
        record["collection_status"] = summary.get("status")
        record["database_integrity"] = summary.get("database_integrity")
        record["collection_finished_at"] = summary.get("finished_at")
        if (record["collection_outcome"] == "success"
                and record["publish_outcome"] == "success"
                and summary.get("status") == "ok"
                and record["succeeded"] == 20 and record["failed"] == 0
                and summary.get("database_integrity") == "ok"):
            record["status"] = "success"
    except (OSError, ValueError, TypeError) as exc:
        record["summary_error"] = type(exc).__name__ + ": " + str(exc)
    return record


def request_json(request):
    with urlopen(request, timeout=30) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise ValueError("Unexpected GitHub API response")
    return value


def write_record(stage, summary_path="data/last_run.json", env=None, instant=None):
    env = os.environ if env is None else env
    # Validate the run identity before a request. The token is never printed or persisted.
    build_record(stage, summary_path, env, instant=instant)
    token = env.get("GITHUB_TOKEN", "")
    if not token:
        raise ValueError("GITHUB_TOKEN is required")
    repository = env["GITHUB_REPOSITORY"]
    branch = env.get("GITHUB_REF_NAME") or "main"
    url = f"{API_ROOT}/repos/{repository}/contents/{quote(STATUS_PATH, safe='/')}"
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2026-03-10",
        "User-Agent": "NEV-CNINFO-Run-Status/1.0",
    }
    previous, sha = None, None
    try:
        existing = request_json(Request(url + "?" + urlencode({"ref": branch}), headers=headers))
    except HTTPError as exc:
        if exc.code != 404:
            raise
    else:
        sha = existing.get("sha")
        if not isinstance(sha, str) or not sha:
            raise ValueError("Existing run status did not provide its file SHA")
        if existing.get("encoding") != "base64":
            raise ValueError("Existing run status has an unexpected encoding")
        encoded = "".join(existing.get("content", "").split())
        previous = json.loads(base64.b64decode(encoded, validate=True))
        if not isinstance(previous, dict):
            raise ValueError("Existing run status must be an object")
    record = build_record(stage, summary_path, env, previous=previous, instant=instant)
    payload = {
        "message": f"Record CNINFO run {record['run_id']} attempt {record['run_attempt']}: {stage}",
        "branch": branch,
        "content": base64.b64encode(
            (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        ).decode("ascii"),
    }
    if sha is not None:
        payload["sha"] = sha
    result = request_json(Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={**headers, "Content-Type": "application/json"}, method="PUT",
    ))
    if not isinstance(result.get("content"), dict) or not result["content"].get("sha"):
        raise ValueError("GitHub did not confirm the run status file update")
    print(f"Recorded cloud run {record['run_id']}: {record['status']}")
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("started", "finished"), required=True)
    parser.add_argument("--summary", default="data/last_run.json")
    args = parser.parse_args(argv)
    write_record(args.stage, args.summary)


if __name__ == "__main__":
    main()
