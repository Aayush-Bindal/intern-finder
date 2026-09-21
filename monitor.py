"""Internship monitor: render company career pages, diff keyword matches, email alerts.

Run with `python monitor.py`. All work happens inside functions; importing this module has no
side effects (no browser launch, no network, no file writes).
"""

import hashlib
import html
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

REPO_ROOT = Path(__file__).resolve().parent
COMPANIES_FILE = REPO_ROOT / "companies.json"
SNAPSHOTS_DIR = REPO_ROOT / "snapshots"
STATE_FILE = SNAPSHOTS_DIR / "_state.json"

NAV_TIMEOUT_MS = 45_000
NETWORKIDLE_TIMEOUT_MS = 10_000
SETTLE_MS = 1_500
REQUEST_DELAY_RANGE = (2.0, 3.0)  # seconds slept between companies
EMAIL_DELAY_SECONDS = 0.6  # gap between Resend calls
MIN_TEXT_LEN = 200  # shorter rendered text = failed render
SNIPPET_PAD = 100  # chars of context on each side of a match
RESEND_URL = "https://api.resend.com/emails"
DEFAULT_FROM = "Internship Monitor <onboarding@resend.dev>"


# --------------------------------------------------------------------------- text helpers


def slugify(name: str) -> str:
    """Snapshot filename stem for a company name. "Walmart Labs" -> "walmart-labs"."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_text(raw: str) -> str:
    return " ".join(raw.split())


def find_matches(text: str, keywords: list[str]) -> dict[str, str]:
    """Case-insensitive substring match; returns {keyword (original casing): snippet}."""
    matches: dict[str, str] = {}
    lowered = text.lower()
    for keyword in keywords:
        idx = lowered.find(keyword.lower())
        if idx < 0:
            continue
        start = max(0, idx - SNIPPET_PAD)
        end = min(len(text), idx + len(keyword) + SNIPPET_PAD)
        snippet = text[start:end].strip()
        if start > 0:
            snippet = "…" + snippet
        if end < len(text):
            snippet = snippet + "…"
        matches[keyword] = snippet
    return matches


# ------------------------------------------------------------------------ snapshot I/O


def load_snapshot(slug: str) -> dict | None:
    """Parsed snapshot dict, or None when missing/unreadable (treated as a first run)."""
    path = SNAPSHOTS_DIR / f"{slug}.json"
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        logging.warning("%s: snapshot unreadable, treating as first run (%s)", slug, exc)
        return None


def save_snapshot(
    slug: str,
    company: dict,
    text_length: int,
    text_sha1: str,
    matches: dict[str, dict],
) -> None:
    os.makedirs(SNAPSHOTS_DIR, exist_ok=True)
    payload = {
        "company": company["name"],
        "url": company["url"],
        "last_checked": utc_now_iso(),
        "text_length": text_length,
        "text_sha1": text_sha1,
        "matches": matches,
    }
    with open(SNAPSHOTS_DIR / f"{slug}.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")


def matches_from_previous(prev: dict | None) -> dict[str, dict]:
    if prev is None:
        return {}
    return prev.get("matches", {})


# ------------------------------------------------------------------------- playwright fetch

_PW = None  # sync_playwright() context manager
_BROWSER = None  # playwright browser
_USER_AGENT = None  # derived string


def _browser():
    """Lazily launch one shared Chromium and derive the user agent string once."""
    global _PW, _BROWSER, _USER_AGENT
    if _BROWSER is None:
        _PW = sync_playwright().start()
        _BROWSER = _PW.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
        ctx = _BROWSER.new_context()
        try:
            page = ctx.new_page()
            _USER_AGENT = page.evaluate("navigator.userAgent").replace("HeadlessChrome", "Chrome")
        finally:
            ctx.close()
    return _BROWSER, _USER_AGENT


def close_browser() -> None:
    global _PW, _BROWSER, _USER_AGENT
    try:
        if _BROWSER is not None:
            _BROWSER.close()
        if _PW is not None:
            _PW.stop()
    except Exception as exc:
        logging.warning("browser shutdown: %s", exc)
    finally:
        _PW = None
        _BROWSER = None
        _USER_AGENT = None


def fetch_page_text(url: str) -> str:
    """Render `url` in headless Chromium and return its whitespace-normalized visible text."""
    browser, user_agent = _browser()
    ctx = browser.new_context(
        user_agent=user_agent,
        locale="en-IN",
        timezone_id="Asia/Kolkata",
        viewport={"width": 1366, "height": 900},
        extra_http_headers={"Accept-Language": "en-IN,en;q=0.9"},
    )
    try:
        page = ctx.new_page()

        def _block_heavy(route):
            if route.request.resource_type in {"image", "media", "font"}:
                route.abort()
            else:
                route.continue_()

        ctx.route("**/*", _block_heavy)
        page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        try:
            page.wait_for_load_state("networkidle", timeout=NETWORKIDLE_TIMEOUT_MS)
        except Exception:
            pass
        page.wait_for_timeout(SETTLE_MS)
        try:
            text = page.inner_text("body", timeout=10_000)
        except Exception:
            text = BeautifulSoup(page.content(), "html.parser").get_text(" ")
    finally:
        try:
            ctx.close()
        except Exception:
            pass
    return normalize_text(text)


# --------------------------------------------------------------------------- email layer


def build_alert_html(company: dict, keyword: str, snippet: str, timestamp: str) -> str:
    name = html.escape(company["name"])
    url = html.escape(company["url"])
    return "\n".join(
        [
            '<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;font-size:14px;line-height:1.5">',
            f'<h2 style="margin:0 0 12px">🚨 New Intern Role Detected — {name}</h2>',
            f'<p style="margin:0 0 4px"><strong>Company:</strong> {name}</p>',
            f'<p style="margin:0 0 4px"><strong>URL:</strong> <a href="{url}">{url}</a></p>',
            f'<p style="margin:0 0 4px"><strong>Keyword:</strong> {html.escape(keyword)}</p>',
            f'<p style="margin:0 0 4px"><strong>Snippet:</strong> {html.escape(snippet)}</p>',
            f'<p style="margin:0"><strong>Detected:</strong> {html.escape(timestamp)}</p>',
            "</div>",
        ]
    )


def build_alert_text(company: dict, keyword: str, snippet: str, timestamp: str) -> str:
    return "\n".join(
        [
            f"Company: {company['name']}",
            f"URL: {company['url']}",
            f"Keyword: {keyword}",
            f"Snippet: {snippet}",
            f"Detected: {timestamp}",
        ]
    )


def build_digest_html(entries: list[dict], timestamp: str) -> str:
    total_roles = sum(len(entry["matches"]) for entry in entries)
    parts = [
        '<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;font-size:14px;line-height:1.5">',
        '<h2 style="margin:0 0 12px">'
        f"🚨 Internship Monitor — Baseline: {total_roles} matching roles across {len(entries)} companies"
        "</h2>",
    ]
    for entry in entries:
        name = html.escape(entry["name"])
        url = html.escape(entry["url"])
        parts.append(f'<h3 style="margin:16px 0 4px">{name}</h3>')
        parts.append(f'<p style="margin:0 0 4px"><a href="{url}">{url}</a></p>')
        parts.append('<ul style="margin:0 0 4px">')
        for keyword, snippet in entry["matches"].items():
            parts.append(f"<li><strong>{html.escape(keyword)}</strong> — {html.escape(snippet)}</li>")
        parts.append("</ul>")
    parts.append(f'<p style="margin:16px 0 0"><strong>Detected:</strong> {html.escape(timestamp)}</p>')
    parts.append("</div>")
    return "\n".join(parts)


def build_digest_text(entries: list[dict], timestamp: str) -> str:
    total_roles = sum(len(entry["matches"]) for entry in entries)
    lines = [
        f"🚨 Internship Monitor — Baseline: {total_roles} matching roles across {len(entries)} companies",
    ]
    for entry in entries:
        lines.append("")
        lines.append(f"Company: {entry['name']}")
        lines.append(f"URL: {entry['url']}")
        for keyword, snippet in entry["matches"].items():
            lines.append(f"- {keyword} — {snippet}")
    lines.append("")
    lines.append(f"Detected: {timestamp}")
    return "\n".join(lines)


def send_email(subject: str, html_body: str, text_body: str) -> bool:
    key = os.environ.get("RESEND_API_KEY")
    alert_email = os.environ.get("ALERT_EMAIL")
    if not key or not alert_email:
        logging.error("RESEND_API_KEY/ALERT_EMAIL not set — email skipped")
        return False

    recipients = [addr.strip() for addr in alert_email.split(",") if addr.strip()]
    from_addr = os.environ.get("ALERT_FROM") or DEFAULT_FROM
    response = None
    try:
        response = requests.post(
            RESEND_URL,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "from": from_addr,
                "to": recipients,
                "subject": subject,
                "html": html_body,
                "text": text_body,
            },
            timeout=30,
        )
        response.raise_for_status()
        return True
    except requests.RequestException as exc:
        detail = ""
        if response is not None:
            detail = f" [status={response.status_code} body={response.text[:300]}]"
        logging.error("Resend send failed: %s%s", exc, detail)
        return False


# ----------------------------------------------------------------------------- main flow


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    with open(COMPANIES_FILE, encoding="utf-8") as f:
        companies = json.load(f)
    logging.info("monitoring %d companies", len(companies))

    digest_entries: list[dict] = []
    failures: list[str] = []
    sent = 0

    try:
        for i, company in enumerate(companies):
            name = company["name"]
            try:
                text = fetch_page_text(company["url"])
                if len(text) < MIN_TEXT_LEN:
                    raise RuntimeError(f"rendered text too short ({len(text)} chars)")

                slug = slugify(name)
                prev = load_snapshot(slug)
                prev_matches = matches_from_previous(prev)
                found = find_matches(text, company["keywords"])
                new_keywords = [kw for kw in found if kw not in prev_matches]
                first_run = prev is None

                now = utc_now_iso()
                matches = {
                    keyword: {
                        "snippet": snippet,
                        "first_seen": prev_matches.get(keyword, {}).get("first_seen", now),
                    }
                    for keyword, snippet in found.items()
                }
                text_length = len(text)
                text_sha1 = hashlib.sha1(text.encode("utf-8")).hexdigest()

                if first_run:
                    if found:
                        digest_entries.append(
                            {"name": name, "url": company["url"], "matches": found}
                        )
                    save_snapshot(slug, company, text_length, text_sha1, matches)
                    logging.info("%s: first run, %d match(es) recorded, baseline", name, len(found))
                elif not new_keywords:
                    save_snapshot(slug, company, text_length, text_sha1, matches)
                    logging.info("%s: %d match(es), 0 new", name, len(found))
                else:
                    timestamp = utc_now_iso()
                    delivered = 0
                    for keyword in new_keywords:
                        ok = send_email(
                            f"🚨 New Intern Role Detected — {name}",
                            build_alert_html(company, keyword, found[keyword], timestamp),
                            build_alert_text(company, keyword, found[keyword], timestamp),
                        )
                        if ok:
                            delivered += 1
                        time.sleep(EMAIL_DELAY_SECONDS)
                    if delivered == len(new_keywords):
                        sent += delivered
                        save_snapshot(slug, company, text_length, text_sha1, matches)
                    else:
                        logging.error(
                            "%s: alert delivery failed, snapshot left unchanged (will retry next run)",
                            name,
                        )
                    logging.info(
                        "%s: %d match(es), %d new (%s)",
                        name,
                        len(found),
                        len(new_keywords),
                        ", ".join(new_keywords),
                    )
            except Exception as exc:
                logging.warning("%s: fetch skipped (%s: %s)", name, type(exc).__name__, exc)
                failures.append(name)
                continue

            if i < len(companies) - 1:
                time.sleep(random.uniform(*REQUEST_DELAY_RANGE))

        if digest_entries:
            total_roles = sum(len(entry["matches"]) for entry in digest_entries)
            timestamp = utc_now_iso()
            subject = (
                f"🚨 Internship Monitor — Baseline: {total_roles} matching roles "
                f"across {len(digest_entries)} companies"
            )
            if send_email(
                subject,
                build_digest_html(digest_entries, timestamp),
                build_digest_text(digest_entries, timestamp),
            ):
                sent += 1
                logging.info(
                    "baseline digest sent (%d roles, %d companies)", total_roles, len(digest_entries)
                )
            else:
                logging.warning("baseline digest not sent")
        else:
            logging.info("no baseline email (no first-run matches)")

        os.makedirs(SNAPSHOTS_DIR, exist_ok=True)
        heartbeat = {
            "last_run": utc_now_iso(),
            "companies_checked": len(companies),
            "failures": failures,
            "emails_sent": sent,
        }
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(heartbeat, f, indent=2, ensure_ascii=False)
            f.write("\n")
    finally:
        close_browser()

    logging.info(
        "done — %d companies, %d skipped, %d email(s) sent", len(companies), len(failures), sent
    )


if __name__ == "__main__":
    main()
