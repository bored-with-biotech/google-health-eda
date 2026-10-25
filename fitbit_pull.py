#!/usr/bin/env python3
"""
Pull Fitbit data through the Google Health API into this folder (raw/ and csv/).

Fitbit's old Web API (api.fitbit.com) is being retired; Fitbit devices now
surface through Google Health API v4 with Google OAuth. Verified references:
  base URL   https://health.googleapis.com/v4
  list       GET  /users/me/dataTypes/{type}/dataPoints?filter=...&pageSize=...
  rollup     POST /users/me/dataTypes/{type}/dataPoints:dailyRollUp
  identity   GET  /users/me/identity
  devices    GET  /users/me/pairedDevices
  settings   GET  /users/me/settings
  data types https://developers.google.com/health/data-types
  scopes     https://developers.google.com/health/scopes

Usage
  python3 fitbit_pull.py auth                       # one-time browser login
  python3 fitbit_pull.py whoami                     # identity + devices + settings
  python3 fitbit_pull.py pull --from 2026-08-01 --to 2026-09-12
  python3 fitbit_pull.py pull --days 30
  python3 fitbit_pull.py pull --days 7 --types steps,sleep,heart-rate

Output
  raw/{type}/{from}_{to}.json     untouched API response pages (ground truth)
  csv/{type}.csv                  flattened, one row per data point, deduped
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
SECRETS = HERE / ".secrets"
CLIENT_SECRET = SECRETS / "client_secret.json"
TOKEN_FILE = SECRETS / "token.json"
RAW_DIR = HERE / "raw"
CSV_DIR = HERE / "csv"

BASE = "https://health.googleapis.com/v4"
SCOPE_PREFIX = "https://www.googleapis.com/auth/googlehealth."
SCOPES = [
    SCOPE_PREFIX + "activity_and_fitness.readonly",
    SCOPE_PREFIX + "health_metrics_and_measurements.readonly",
    SCOPE_PREFIX + "sleep.readonly",
    SCOPE_PREFIX + "profile.readonly",
    SCOPE_PREFIX + "settings.readonly",
]

# kind: how the API filters this type (from the official google-health-cli registry)
#   interval -> "{snake}.interval.civil_start_time"  civil ISO-8601, no Z
#   sleep    -> "sleep.interval.civil_end_time"      civil, end-time only
#   sample   -> "{snake}.sample_time.physical_time"  RFC-3339 with Z
#   daily    -> "{snake}.date"                       YYYY-MM-DD
# rollup: True if the type supports dataPoints:dailyRollUp (one row per day)
TYPES: dict[str, dict] = {
    "steps":                        {"kind": "interval", "rollup": True},
    "distance":                     {"kind": "interval", "rollup": True},
    "total-calories":               {"kind": "interval", "rollup": True, "max_days": 14},
    "active-minutes":               {"kind": "interval", "rollup": True, "max_days": 14},
    "floors":                       {"kind": "interval", "rollup": True},
    "active-zone-minutes":          {"kind": "interval", "rollup": False},
    "exercise":                     {"kind": "interval", "rollup": False, "page": 25},
    "sleep":                        {"kind": "sleep",    "rollup": False, "page": 25},
    "heart-rate":                   {"kind": "sample",   "rollup": False, "max_days": 1},
    "heart-rate-variability":       {"kind": "sample",   "rollup": False},
    "oxygen-saturation":            {"kind": "sample",   "rollup": False},
    "weight":                       {"kind": "sample",   "rollup": False},
    "body-fat":                     {"kind": "sample",   "rollup": False},
    "vo2-max":                      {"kind": "sample",   "rollup": False},
    "daily-resting-heart-rate":     {"kind": "daily",    "rollup": False},
    "daily-heart-rate-variability": {"kind": "daily",    "rollup": False},
    "daily-oxygen-saturation":      {"kind": "daily",    "rollup": False},
    "daily-respiratory-rate":       {"kind": "daily",    "rollup": False},
    "daily-heart-rate-zones":       {"kind": "daily",    "rollup": False},
    "daily-vo2-max":                {"kind": "daily",    "rollup": False},
}

# Default pull set. heart-rate is opt-in (one sample every few seconds, huge).
DEFAULT_TYPES = [
    "steps", "distance", "total-calories", "active-minutes", "floors",
    "active-zone-minutes", "exercise", "sleep",
    "daily-resting-heart-rate", "daily-heart-rate-variability",
    "daily-oxygen-saturation", "daily-respiratory-rate", "weight",
]


# --------------------------------------------------------------------------- auth
def _creds(interactive: bool):
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            TOKEN_FILE.write_text(creds.to_json())
            return creds
        except RefreshError as e:
            # Testing-status OAuth apps revoke refresh tokens after 7 days. `auth` must
            # fall through to the browser; `pull` must say so instead of tracebacking.
            if not interactive:
                sys.exit(f"Token refresh failed ({e.args[0] if e.args else e}). Run:  python3 fitbit_pull.py auth")
            TOKEN_FILE.rename(TOKEN_FILE.with_name(f"token.revoked.{dt.date.today():%Y%m%d}.json"))
    if not interactive:
        sys.exit("No valid token. Run:  python3 fitbit_pull.py auth")
    if not CLIENT_SECRET.exists():
        sys.exit(
            f"Missing {CLIENT_SECRET}\n"
            "Download the OAuth client JSON from Google Cloud Console "
            "(Desktop app type) and save it there. See README.md."
        )
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
    # access_type=offline + prompt=consent guarantees a refresh_token is issued.
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    SECRETS.mkdir(exist_ok=True)
    TOKEN_FILE.write_text(creds.to_json())
    TOKEN_FILE.chmod(0o600)
    print(f"Token saved to {TOKEN_FILE}")
    return creds


class Api:
    def __init__(self, creds):
        self.creds = creds
        self.s = requests.Session()

    def _headers(self):
        from google.auth.transport.requests import Request
        if self.creds.expired and self.creds.refresh_token:
            self.creds.refresh(Request())
            TOKEN_FILE.write_text(self.creds.to_json())
        return {"Authorization": f"Bearer {self.creds.token}", "Accept": "application/json"}

    def _req(self, method: str, path: str, **kw) -> dict:
        url = f"{BASE}/{path}"
        for attempt in range(5):
            r = self.s.request(method, url, headers=self._headers(), timeout=60, **kw)
            if r.status_code in (429, 500, 502, 503, 504):
                wait = 2 ** attempt
                print(f"  {r.status_code} on {path}; retry in {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            if not r.ok:
                sys.exit(f"HTTP {r.status_code} {method} {url}\n{r.text}")
            return r.json() if r.text else {}
        sys.exit(f"Gave up on {url}")

    def get(self, path: str, params: dict | None = None) -> dict:
        return self._req("GET", path, params=params)

    def post(self, path: str, body: dict) -> dict:
        return self._req("POST", path, json=body)


# --------------------------------------------------------------------------- filters
def snake(slug: str) -> str:
    return slug.replace("-", "_")


def build_filter(slug: str, start: dt.date, end: dt.date) -> str:
    """Closed-open [start, end). Only >= and < are supported by the API."""
    kind = TYPES[slug]["kind"]
    s = snake(slug)
    if kind == "interval":
        f = f"{s}.interval.civil_start_time"
        lo, hi = f'"{start.isoformat()}T00:00:00"', f'"{end.isoformat()}T00:00:00"'
    elif kind == "sleep":
        f = "sleep.interval.civil_end_time"
        lo, hi = f'"{start.isoformat()}T00:00:00"', f'"{end.isoformat()}T00:00:00"'
    elif kind == "sample":
        f = f"{s}.sample_time.physical_time"
        lo, hi = f'"{start.isoformat()}T00:00:00Z"', f'"{end.isoformat()}T00:00:00Z"'
    elif kind == "daily":
        f = f"{s}.date"
        lo, hi = f'"{start.isoformat()}"', f'"{end.isoformat()}"'
    else:
        raise ValueError(kind)
    return f"{f} >= {lo} AND {f} < {hi}"


def civil(d: dt.date) -> dict:
    return {"date": {"year": d.year, "month": d.month, "day": d.day}}


def chunks(start: dt.date, end: dt.date, max_days: int):
    cur = start
    while cur < end:
        nxt = min(cur + dt.timedelta(days=max_days), end)
        yield cur, nxt
        cur = nxt


# --------------------------------------------------------------------------- pulls
def list_points(api: Api, slug: str, start: dt.date, end: dt.date) -> list[dict]:
    cfg = TYPES[slug]
    out: list[dict] = []
    for a, b in chunks(start, end, cfg.get("max_days", 90)):
        params = {"filter": build_filter(slug, a, b), "pageSize": cfg.get("page", 10000)}
        token = None
        while True:
            if token:
                params["pageToken"] = token
            page = api.get(f"users/me/dataTypes/{slug}/dataPoints", params)
            out.extend(page.get("dataPoints", []))
            token = page.get("nextPageToken")
            if not token:
                break
    return out


def daily_rollup(api: Api, slug: str, start: dt.date, end: dt.date) -> list[dict]:
    cfg = TYPES[slug]
    out: list[dict] = []
    for a, b in chunks(start, end, cfg.get("max_days", 90)):
        # API rule: windowSizeDays * pageSize must not exceed the type's max range (90 days).
        body = {"range": {"start": civil(a), "end": civil(b)}, "windowSizeDays": 1, "pageSize": (b - a).days}
        token = None
        while True:
            if token:
                body["pageToken"] = token
            page = api.post(f"users/me/dataTypes/{slug}/dataPoints:dailyRollUp", body)
            out.extend(page.get("rollupDataPoints", []))
            token = page.get("nextPageToken")
            if not token:
                break
    return out


# --------------------------------------------------------------------------- output
def flatten(obj, prefix: str = "", out: dict | None = None) -> dict:
    """Nested dict -> dotted keys. Lists (sleep stages, HR zones, exercise
    events) are kept as compact JSON text; raw/ always has the full shape."""
    out = {} if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            flatten(v, f"{prefix}{k}.", out)
    elif isinstance(obj, list):
        out[prefix.rstrip(".")] = json.dumps(obj, separators=(",", ":"))
    else:
        out[prefix.rstrip(".")] = obj
    return out


def civil_to_iso(c: dict | None) -> str:
    if not c or "date" not in c:
        return ""
    d = c["date"]
    t = c.get("time", {})
    return (f"{d.get('year', 0):04d}-{d.get('month', 0):02d}-{d.get('day', 0):02d}"
            f"T{t.get('hours', 0):02d}:{t.get('minutes', 0):02d}:{t.get('seconds', 0):02d}")


def row_key(row: dict) -> str:
    """Stable identity for dedupe across overlapping pulls."""
    for k in ("name", "civilStartTime", "startTime"):
        if row.get(k):
            return f"{k}={row[k]}|{row.get('dataSource.name', '')}"
    return json.dumps(row, sort_keys=True)


def write_csv(slug: str, mode: str, points: list[dict]) -> Path:
    path = CSV_DIR / f"{slug}{'_daily' if mode == 'rollup' else ''}.csv"
    rows = []
    for p in points:
        r = flatten(p)
        for k in ("civilStartTime", "civilEndTime"):
            if isinstance(p.get(k), dict):
                r[k] = civil_to_iso(p[k])
        rows.append(r)
    existing: dict[str, dict] = {}
    if path.exists():
        with path.open(newline="") as fh:
            for r in csv.DictReader(fh):
                existing[row_key(r)] = r
    for r in rows:
        existing[row_key(r)] = r  # newer pull wins
    cols: list[str] = []
    for r in existing.values():
        for k in r:
            if k not in cols:
                cols.append(k)
    CSV_DIR.mkdir(exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in existing.values():
            w.writerow(r)
    return path


def write_raw(slug: str, mode: str, start: dt.date, end: dt.date, points: list[dict]) -> Path:
    d = RAW_DIR / slug
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{start.isoformat()}_{end.isoformat()}{'_daily' if mode == 'rollup' else ''}.json"
    path.write_text(json.dumps({
        "dataType": slug, "mode": mode, "from": start.isoformat(), "to": end.isoformat(),
        "pulledAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "count": len(points), "points": points,
    }, indent=1))
    return path


# --------------------------------------------------------------------------- commands
def cmd_auth(_):
    _creds(interactive=True)
    print("Authorized.")


def cmd_whoami(_):
    api = Api(_creds(interactive=False))
    for name in ("identity", "settings", "pairedDevices"):
        print(f"== {name}")
        print(json.dumps(api.get(f"users/me/{name}"), indent=2))


def cmd_pull(a):
    today = dt.date.today()
    end = dt.date.fromisoformat(a.to) if a.to else today + dt.timedelta(days=1)
    start = dt.date.fromisoformat(getattr(a, "from")) if getattr(a, "from") else end - dt.timedelta(days=a.days)
    if start >= end:
        sys.exit("--from must be before --to")
    types = [t.strip() for t in a.types.split(",")] if a.types else DEFAULT_TYPES
    bad = [t for t in types if t not in TYPES]
    if bad:
        sys.exit(f"Unknown type(s): {bad}. Known: {', '.join(TYPES)}")

    api = Api(_creds(interactive=False))
    print(f"Pulling {start} -> {end} (exclusive) for {len(types)} types")
    for slug in types:
        cfg = TYPES[slug]
        mode = "rollup" if cfg["rollup"] and not a.no_rollup else "list"
        pts = daily_rollup(api, slug, start, end) if mode == "rollup" else list_points(api, slug, start, end)
        raw = write_raw(slug, mode, start, end, pts)
        out = write_csv(slug, mode, pts)
        print(f"  {slug:30s} {mode:6s} {len(pts):6d} pts -> {out.relative_to(HERE)}  (raw: {raw.name})")
        if cfg["rollup"] and mode == "rollup" and a.also_list:
            pts = list_points(api, slug, start, end)
            write_raw(slug, "list", start, end, pts)
            out = write_csv(slug, "list", pts)
            print(f"  {slug:30s} list   {len(pts):6d} pts -> {out.relative_to(HERE)}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("auth", help="one-time browser OAuth; saves .secrets/token.json").set_defaults(fn=cmd_auth)
    sub.add_parser("whoami", help="print identity, settings (timezone), paired devices").set_defaults(fn=cmd_whoami)
    pl = sub.add_parser("pull", help="pull data into raw/ and csv/")
    pl.add_argument("--from", help="start date YYYY-MM-DD (inclusive)")
    pl.add_argument("--to", help="end date YYYY-MM-DD (exclusive); default tomorrow")
    pl.add_argument("--days", type=int, default=30, help="if --from omitted: days back from --to (default 30)")
    pl.add_argument("--types", help=f"comma list; default: {','.join(DEFAULT_TYPES)}")
    pl.add_argument("--no-rollup", action="store_true", help="list raw intervals instead of daily rollups")
    pl.add_argument("--also-list", action="store_true", help="for rollup types, also pull the raw interval list")
    pl.set_defaults(fn=cmd_pull)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
