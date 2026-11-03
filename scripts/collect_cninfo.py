#!/usr/bin/env python3
"""Fetch the existing 20-company CNINFO announcement pool into SQLite.

Only public announcement metadata is collected: date, title and detail link.
The database schema and links match the original NEV precrawler. No login,
personal token, browser session or password is used by this collector.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import math
from pathlib import Path
import re
import sqlite3
import sys
import time

import requests


ROOT = Path(__file__).resolve().parents[1]
CHINA_TIME = dt.timezone(dt.timedelta(hours=8), "Asia/Shanghai")
SOURCE = "https://www.cninfo.com.cn"
SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    code TEXT PRIMARY KEY, name TEXT, segment TEXT, sub TEXT
);
CREATE TABLE IF NOT EXISTS announcements (
    code TEXT NOT NULL, notice_date TEXT NOT NULL, title TEXT, url TEXT,
    PRIMARY KEY (code, url)
);
CREATE TABLE IF NOT EXISTS crawl_state (
    task TEXT NOT NULL, code TEXT NOT NULL, status TEXT NOT NULL,
    message TEXT, rows INTEGER DEFAULT 0, updated_at TEXT,
    PRIMARY KEY (task, code)
);
"""


def now() -> dt.datetime:
    return dt.datetime.now(CHINA_TIME)


def load_companies(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    companies = data.get("companies")
    if not isinstance(companies, list) or len(companies) != 20:
        raise ValueError("The NEV pool must contain exactly 20 companies.")
    codes = set()
    for company in companies:
        if not isinstance(company, dict):
            raise ValueError("Invalid company entry.")
        code = company.get("code")
        if not isinstance(code, str) or not re.fullmatch(r"\d{6}", code):
            raise ValueError("A company code must be a six-digit string.")
        if code in codes or not company.get("name"):
            raise ValueError("Company codes must be unique and names must exist.")
        codes.add(code)
    return companies


class CninfoClient:
    def __init__(self, *, session=None, attempts=3, request_timeout=20.0,
                 company_timeout=90.0, max_pages=100, sleeper=time.sleep):
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; NEVResearchCollector/1.0)",
            "Referer": SOURCE + "/new/commonUrl/pageOfSearch?url=disclosure/list/search",
            "Accept": "application/json, text/plain, */*",
        })
        self.attempts = attempts
        self.request_timeout = request_timeout
        self.company_timeout = company_timeout
        self.max_pages = max_pages
        self.sleep = sleeper

    def _json(self, method, url, *, deadline=None, **kwargs):
        last_error = None
        for attempt in range(self.attempts):
            remaining = deadline - time.monotonic() if deadline is not None else self.request_timeout
            if remaining <= 0:
                raise TimeoutError("CNINFO company time limit exceeded.")
            timeout = min(self.request_timeout, remaining)
            try:
                response = self.session.request(method, url, timeout=(min(5.0, timeout), timeout), **kwargs)
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("CNINFO did not return a JSON object.")
                return payload
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                if attempt + 1 < self.attempts:
                    delay = min(2 ** (attempt + 1), 8)
                    if deadline is not None and time.monotonic() + delay >= deadline:
                        break
                    self.sleep(delay)
        raise RuntimeError(f"CNINFO request failed after bounded retries: {last_error}") from last_error

    def stock_map(self) -> dict[str, str]:
        data = self._json("GET", SOURCE + "/new/data/szse_stock.json")
        stocks = data.get("stockList")
        if not isinstance(stocks, list) or not stocks:
            raise ValueError("CNINFO stock map is missing or empty.")
        result = {}
        for stock in stocks:
            if isinstance(stock, dict) and stock.get("code") and stock.get("orgId"):
                result[str(stock["code"])] = str(stock["orgId"])
        if not result:
            raise ValueError("CNINFO stock map has no valid entries.")
        return result

    @staticmethod
    def _page(data: dict) -> tuple[int, list]:
        total = data.get("totalAnnouncement")
        if isinstance(total, bool):
            raise ValueError("Invalid announcement count.")
        if not isinstance(total, (int, str)) or not str(total).isdigit():
            raise ValueError("CNINFO response lacks a valid announcement count.")
        total = int(total)
        records = data.get("announcements")
        if records is None and total == 0:
            return total, []
        if not isinstance(records, list):
            raise ValueError("CNINFO response lacks announcement records.")
        if total > 0 and not records:
            raise ValueError("CNINFO returned an empty page for a nonempty result.")
        return total, records

    @staticmethod
    def _record(record: dict, code: str, start: dt.date, end: dt.date) -> tuple:
        if not isinstance(record, dict) or str(record.get("secCode", "")) != code:
            raise ValueError("Announcement company code mismatch.")
        title = record.get("announcementTitle")
        announcement_id, org_id = record.get("announcementId"), record.get("orgId")
        stamp = record.get("announcementTime")
        if not isinstance(title, str) or not title.strip() or not announcement_id or not org_id:
            raise ValueError("Announcement has a missing title or identifier.")
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
            raise ValueError("Announcement timestamp must be milliseconds since epoch.")
        if not math.isfinite(stamp):
            raise ValueError("Announcement timestamp is not finite.")
        published = dt.datetime.fromtimestamp(stamp / 1000, CHINA_TIME)
        if not (start <= published.date() <= end):
            raise ValueError("Announcement date is outside the requested window.")
        title = html.unescape(re.sub(r"<[^>]*>", "", title)).strip()
        if not title:
            raise ValueError("Announcement title is empty after removing markup.")
        # Keep AKShare's existing detail URL format so old database rows deduplicate.
        url = (f"http://www.cninfo.com.cn/new/disclosure/detail?stockCode={code}"
               f"&announcementId={announcement_id}&orgId={org_id}"
               f"&announcementTime={published.strftime('%Y-%m-%d %H:%M:%S')}")
        return code, published.date().isoformat(), title, url

    def fetch_company(self, code: str, org_id: str, start: dt.date, end: dt.date) -> list[tuple]:
        deadline = time.monotonic() + self.company_timeout
        payload = {
            "pageNum": 1, "pageSize": 30, "column": "szse", "tabName": "fulltext",
            "plate": "", "stock": f"{code},{org_id}", "searchkey": "", "secid": "",
            "category": "", "trade": "", "seDate": f"{start.isoformat()}~{end.isoformat()}",
            "sortName": "", "sortType": "", "isHLtitle": "false",
        }
        data = self._json("POST", SOURCE + "/new/hisAnnouncement/query", data=payload, deadline=deadline)
        total, records = self._page(data)
        pages = math.ceil(total / 30)
        if pages > self.max_pages:
            raise ValueError(f"CNINFO result exceeds the {self.max_pages}-page safety limit.")
        all_records = list(records)
        for page in range(2, pages + 1):
            payload["pageNum"] = page
            data = self._json("POST", SOURCE + "/new/hisAnnouncement/query", data=payload, deadline=deadline)
            page_total, records = self._page(data)
            if page_total != total:
                raise ValueError("CNINFO result changed during pagination; retry on the next run.")
            all_records.extend(records)
        if len(all_records) != total:
            raise ValueError(f"Incomplete CNINFO pagination: expected {total}, got {len(all_records)}.")
        rows = [self._record(record, code, start, end) for record in all_records]
        return list(dict.fromkeys(rows))


def mark_state(conn, code, status, count, message=""):
    conn.execute(
        "INSERT OR REPLACE INTO crawl_state (task,code,status,message,rows,updated_at) VALUES (?,?,?,?,?,?)",
        ("announcements", code, status, message[:500], count, now().strftime("%Y-%m-%d %H:%M:%S")),
    )


def write_summary(path: Path, summary: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def run_collection(db_path: Path, companies_path: Path, summary_path: Path, *,
                   client=None, days=180, company_delay=1.0, end_date=None) -> int:
    companies = load_companies(companies_path)
    end = end_date or now().date()
    start = end - dt.timedelta(days=days)
    summary = {
        "source": SOURCE, "scope": "announcement_metadata", "timezone": "Asia/Shanghai",
        "started_at": now().isoformat(timespec="seconds"), "start_date": start.isoformat(),
        "end_date": end.isoformat(), "companies": len(companies), "results": [],
    }
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        conn.executescript(SCHEMA)
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO companies (code,name,segment,sub) VALUES (?,?,?,?)",
                [(company["code"], company["name"], company.get("segment"), company.get("sub"))
                 for company in companies],
            )
        client = client or CninfoClient()
        map_error = None
        try:
            stock_map = client.stock_map()
        except Exception as exc:
            map_error = f"Stock map: {type(exc).__name__}: {exc}"
            stock_map = {}
        for index, company in enumerate(companies):
            code = company["code"]
            result = {"code": code, "name": company["name"]}
            try:
                if map_error:
                    raise RuntimeError(map_error)
                if code not in stock_map:
                    raise ValueError("Company is absent from CNINFO's stock map.")
                rows = client.fetch_company(code, stock_map[code], start, end)
                with conn:
                    conn.executemany(
                        "INSERT OR REPLACE INTO announcements (code,notice_date,title,url) VALUES (?,?,?,?)", rows,
                    )
                    mark_state(conn, code, "ok", len(rows))
                result.update(status="ok", rows=len(rows), message="")
                print(f"[OK] {code} {company['name']}: {len(rows)} announcements", flush=True)
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"[:500]
                with conn:
                    mark_state(conn, code, "fail", 0, message)
                result.update(status="fail", rows=0, message=message)
                print(f"[FAIL] {code} {company['name']}: {message}", file=sys.stderr, flush=True)
            summary["results"].append(result)
            if company_delay and index + 1 < len(companies) and not map_error:
                time.sleep(company_delay)
        summary["succeeded"] = sum(result["status"] == "ok" for result in summary["results"])
        summary["failed"] = len(companies) - summary["succeeded"]
        summary["rows_fetched"] = sum(result["rows"] for result in summary["results"])
        summary["status"] = "ok" if summary["failed"] == 0 else "partial_failure"
        summary["database_integrity"] = conn.execute("PRAGMA quick_check").fetchone()[0]
        if summary["database_integrity"] != "ok":
            summary["status"] = "database_error"
        summary["finished_at"] = now().isoformat(timespec="seconds")
        write_summary(summary_path, summary)
        print(f"Finished: {summary['succeeded']}/20 succeeded; {summary['failed']} failed.", flush=True)
        return 0 if summary["status"] == "ok" else 1
    finally:
        conn.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "nev_stock.db")
    parser.add_argument("--companies", type=Path, default=ROOT / "precrawler" / "companies.json")
    parser.add_argument("--summary", type=Path, default=ROOT / "data" / "last_run.json")
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--request-timeout", type=float, default=20)
    parser.add_argument("--company-timeout", type=float, default=90)
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--company-delay", type=float, default=1)
    args = parser.parse_args(argv)
    if args.days < 1 or args.attempts < 1 or args.attempts > 5:
        parser.error("days must be positive; attempts must be between 1 and 5")
    if args.request_timeout <= 0 or args.company_timeout <= 0 or args.company_delay < 0:
        parser.error("timeouts must be positive and company-delay cannot be negative")
    client = CninfoClient(attempts=args.attempts, request_timeout=args.request_timeout,
                          company_timeout=args.company_timeout)
    return run_collection(args.db, args.companies, args.summary, client=client,
                          days=args.days, company_delay=args.company_delay)


if __name__ == "__main__":
    sys.exit(main())
