"""
Hudson Sailing Club event monitor.

Checks the club calendar for events matching a keyword (default: "The Morning
Race") on every matching weekday within a rolling window, and sends a
notification when an event that was previously full opens up.

The site requires authentication. The script logs in fresh on each run using
Django's CSRF-protected login form.

Configuration is via environment variables (set in GitHub Actions secrets/vars
or a local .env for testing):

    HUDSON_USERNAME  (required) Email used to log in.
    HUDSON_PASSWORD  (required) Password.
    NTFY_TOPIC       (required) ntfy.sh topic, e.g. "hudson-sail-alerts-xyz"
    EVENT_KEYWORD    (optional) Substring to match in event titles.
                                Default: "The Morning Race"
    WEEKDAYS         (optional) Comma-separated weekday names or numbers
                                (Mon=0..Sun=6). Default: "Wed"
                                Examples: "Wed", "Sat,Sun", "0,2,4"
    LOOKAHEAD_DAYS   (optional) How many days ahead to scan. Default: 30
    STATE_FILE       (optional) Path to JSON state file.
                                Default: "state/seen.json"
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://hudsonsailing.club/"
LOGIN_URL = "https://hudsonsailing.club/accounts/login/"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 20

# Matches strings like "4/5 attending", "0/8 attending", etc.
# Group 1 = current attendees, group 2 = capacity.
ATTENDANCE_RE = re.compile(r"(\d+)\s*/\s*(\d+)\s*attending", re.IGNORECASE)


class LoginError(RuntimeError):
    pass


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


def login(session: requests.Session, username: str, password: str) -> None:
    """
    Log in to Hudson Sailing Club. Standard Django CSRF flow:

      1. GET the login page → server sets a `csrftoken` cookie and embeds a
         `csrfmiddlewaretoken` hidden input in the form.
      2. POST the form back with that token + credentials. Django checks the
         cookie and the form token match.
      3. On success, server sets a `sessionid` cookie. We use the session for
         subsequent requests.
    """
    # Step 1: GET the login page.
    r = session.get(LOGIN_URL, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")
    token_input = soup.find("input", attrs={"name": "csrfmiddlewaretoken"})
    if not token_input or not token_input.get("value"):
        raise LoginError(
            "Could not find csrfmiddlewaretoken on the login page. "
            "The login form may have changed."
        )
    token = token_input["value"]

    # Step 2: POST credentials. Django requires Referer to match Origin for
    # CSRF protection on HTTPS, so we set both.
    headers = {
        "Referer": LOGIN_URL,
        "Origin": BASE_URL.rstrip("/"),
    }
    data = {
        "csrfmiddlewaretoken": token,
        "username": username,
        "password": password,
        "next": "/",
    }
    r = session.post(
        LOGIN_URL,
        data=data,
        headers=headers,
        timeout=REQUEST_TIMEOUT,
        allow_redirects=True,
    )
    r.raise_for_status()

    # Step 3: verify login worked.
    # If we land back on the login page, the credentials were rejected (Django
    # re-renders the form with an error message rather than redirecting).
    if "/accounts/login" in r.url or 'name="password"' in r.text:
        # Try to surface the server's error message if there is one.
        err_soup = BeautifulSoup(r.text, "html.parser")
        err_node = err_soup.find(class_=re.compile(r"error|alert|invalid", re.I))
        hint = err_node.get_text(" ", strip=True) if err_node else "(no detail)"
        raise LoginError(f"Login failed — server rejected credentials. {hint}")

    if "sessionid" not in session.cookies:
        raise LoginError(
            "Login appeared to succeed but no sessionid cookie was set. "
            "The site's auth flow may have changed."
        )


def fetch_day(session: requests.Session, d: date) -> str:
    # The site uses non-zero-padded months in the URL (e.g. ?date=2026-5-20).
    url = f"{BASE_URL}?date={d.year}-{d.month}-{d.day}"
    resp = session.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    # If the session somehow expired mid-run, Django will redirect to the
    # login page. Detect that so we don't silently parse an empty calendar.
    if "/accounts/login" in resp.url or 'name="csrfmiddlewaretoken"' in resp.text:
        raise LoginError(f"Session expired while fetching {d.isoformat()}.")
    return resp.text


@dataclass(frozen=True)
class Event:
    date_iso: str       # "2026-05-20"
    title: str
    current: int
    capacity: int

    @property
    def is_full(self) -> bool:
        return self.current >= self.capacity

    @property
    def spots_left(self) -> int:
        return max(self.capacity - self.current, 0)

    @property
    def key(self) -> str:
        # Stable identifier for state tracking.
        return f"{self.date_iso}::{self.title}"


WEEKDAY_NAMES = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "weds": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}


def parse_weekdays(spec: str) -> set[int]:
    """Parse 'Wed,Sat' or '2,5' into a set of weekday ints (Mon=0..Sun=6)."""
    result: set[int] = set()
    for raw in spec.split(","):
        token = raw.strip().lower()
        if not token:
            continue
        if token.isdigit():
            n = int(token)
            if 0 <= n <= 6:
                result.add(n)
            else:
                raise ValueError(f"Weekday number out of range (0-6): {token}")
        elif token in WEEKDAY_NAMES:
            result.add(WEEKDAY_NAMES[token])
        else:
            raise ValueError(f"Unrecognized weekday: {raw!r}")
    if not result:
        raise ValueError("WEEKDAYS produced an empty set")
    return result


def upcoming_dates(start: date, lookahead_days: int, weekdays: set[int]) -> Iterable[date]:
    """Yield each date in [start, start+lookahead_days] matching `weekdays`."""
    for i in range(lookahead_days + 1):
        d = start + timedelta(days=i)
        if d.weekday() in weekdays:
            yield d


def parse_events(html: str, day_iso: str, keyword: str) -> list[Event]:
    """
    Find events whose title contains `keyword`, with their attendance count.

    Strategy: find the innermost elements containing the keyword (one per
    event), then for each, search outward for the closest "N/M attending"
    text. This is more reliable than starting from attendance text because
    one keyword match = one event, whereas an attendance text's ancestor
    may contain many events.

    The site disallows automated previewing, so this uses heuristics that
    should work across common layouts. Tune here if the live page surprises us.
    """
    soup = BeautifulSoup(html, "html.parser")
    events: list[Event] = []
    seen_keys: set[str] = set()
    kw_lower = keyword.lower()

    # Innermost elements containing the keyword: no child also contains it.
    keyword_nodes = []
    for el in soup.find_all(True):
        text = el.get_text(" ", strip=True)
        if kw_lower not in text.lower():
            continue
        if any(
            kw_lower in (c.get_text(" ", strip=True) or "").lower()
            for c in el.find_all(True, recursive=False)
        ):
            continue
        keyword_nodes.append(el)

    for el in keyword_nodes:
        # Look for attendance: in el itself, then expanding through ancestors.
        # Stop at the first ancestor whose text contains an attendance string
        # near the keyword.
        current = capacity = None
        title_source = el.get_text(" ", strip=True)

        cursor = el
        for _ in range(6):
            if cursor is None:
                break
            text = cursor.get_text(" ", strip=True)
            m = ATTENDANCE_RE.search(text)
            if m:
                kw_idx = text.lower().find(kw_lower)
                att_idx = m.start()
                # Require keyword and attendance to be close; otherwise this
                # ancestor is too broad and the attendance belongs to another event.
                if kw_idx != -1 and abs(kw_idx - att_idx) <= 250:
                    current, capacity = int(m.group(1)), int(m.group(2))
                    break
            cursor = cursor.parent

        if current is None or capacity is None:
            continue

        title = ATTENDANCE_RE.sub("", title_source).strip()
        title = " ".join(title.split())
        # Trim trailing separators left behind after stripping attendance text.
        title = title.rstrip(" -–—·•|,:;")
        if len(title) > 140:
            title = title[:140].rstrip() + "…"

        ev = Event(date_iso=day_iso, title=title, current=current, capacity=capacity)
        if ev.key in seen_keys:
            continue
        seen_keys.add(ev.key)
        events.append(ev)

    return events


def load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True))


def notify(topic: str, title: str, message: str, click_url: str | None = None) -> None:
    # Use ntfy's JSON API rather than the header-based one, because HTTP
    # headers are restricted to latin-1 and our title may contain emoji or
    # other Unicode characters.
    payload: dict = {
        "topic": topic,
        "title": title,
        "message": message,
        "priority": 4,  # high
        "tags": ["sailboat", "bell"],
    }
    if click_url:
        payload["click"] = click_url
    resp = requests.post(
        "https://ntfy.sh/",
        json=payload,
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()


def main() -> int:
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        print("ERROR: NTFY_TOPIC env var is required.", file=sys.stderr)
        return 2

    username = os.environ.get("HUDSON_USERNAME")
    password = os.environ.get("HUDSON_PASSWORD")
    if not username or not password:
        print(
            "ERROR: HUDSON_USERNAME and HUDSON_PASSWORD env vars are required.",
            file=sys.stderr,
        )
        return 2

    keyword = os.environ.get("EVENT_KEYWORD", "The Morning Race")
    weekdays_spec = os.environ.get("WEEKDAYS", "Wed")
    try:
        weekdays = parse_weekdays(weekdays_spec)
    except ValueError as e:
        print(f"ERROR: invalid WEEKDAYS={weekdays_spec!r}: {e}", file=sys.stderr)
        return 2

    try:
        lookahead = int(os.environ.get("LOOKAHEAD_DAYS", "30"))
    except ValueError:
        print("ERROR: LOOKAHEAD_DAYS must be an integer.", file=sys.stderr)
        return 2
    if lookahead < 0:
        print("ERROR: LOOKAHEAD_DAYS must be >= 0.", file=sys.stderr)
        return 2

    today = date.today()
    state_file = Path(os.environ.get("STATE_FILE", "state/seen.json"))

    state = load_state(state_file)
    # state schema:
    #   { "2026-05-20::The Morning Race": {"was_full": true, "last_seen": "..."} }

    new_state: dict = {}
    notifications_sent = 0
    dates_to_check = list(upcoming_dates(today, lookahead, weekdays))

    print(
        f"Checking {len(dates_to_check)} date(s) "
        f"(weekdays={sorted(weekdays)}, lookahead={lookahead}d) "
        f"for keyword {keyword!r}."
    )

    # Authenticate once and reuse the session for all calendar fetches.
    session = make_session()
    try:
        login(session, username, password)
    except (LoginError, requests.RequestException) as e:
        print(f"ERROR: login failed: {e}", file=sys.stderr)
        return 1
    print("Login OK.")

    for d in dates_to_check:
        day_iso = d.isoformat()
        try:
            html = fetch_day(session, d)
        except LoginError as e:
            # Session expired mid-run; try to log in again once.
            print(f"WARN: {e} Re-authenticating.", file=sys.stderr)
            try:
                login(session, username, password)
                html = fetch_day(session, d)
            except (LoginError, requests.RequestException) as e2:
                print(f"ERROR: re-auth/fetch failed for {day_iso}: {e2}", file=sys.stderr)
                for k, v in state.items():
                    if k.startswith(f"{day_iso}::"):
                        new_state[k] = v
                continue
        except requests.RequestException as e:
            print(f"WARN: failed to fetch {day_iso}: {e}", file=sys.stderr)
            # Preserve prior state for this date so we don't lose tracking.
            for k, v in state.items():
                if k.startswith(f"{day_iso}::"):
                    new_state[k] = v
            continue

        events = parse_events(html, day_iso, keyword)
        print(f"{day_iso} ({d.strftime('%a')}): found {len(events)} matching event(s).")

        for ev in events:
            prev = state.get(ev.key, {})
            was_full = bool(prev.get("was_full", False))

            # Notify on the transition full -> open. Also notify the first time
            # we see an event that's already open (so you don't miss the very
            # first observation).
            should_notify = (not ev.is_full) and (was_full or ev.key not in state)

            if should_notify:
                pretty_date = datetime.fromisoformat(ev.date_iso).strftime("%a %b %-d, %Y")
                title = f"⛵ Spot open: {ev.title}"
                body = (
                    f"{pretty_date}\n"
                    f"{ev.current}/{ev.capacity} attending "
                    f"({ev.spots_left} spot{'s' if ev.spots_left != 1 else ''} left)"
                )
                url = f"{BASE_URL}?date={d.year}-{d.month}-{d.day}"
                try:
                    notify(topic, title, body, click_url=url)
                    notifications_sent += 1
                    print(f"  -> NOTIFIED: {ev.title} ({ev.current}/{ev.capacity})")
                except requests.RequestException as e:
                    print(f"  -> notify failed: {e}", file=sys.stderr)

            new_state[ev.key] = {
                "was_full": ev.is_full,
                "current": ev.current,
                "capacity": ev.capacity,
                "last_seen": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            }

    save_state(state_file, new_state)
    print(f"Done. {notifications_sent} notification(s) sent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
