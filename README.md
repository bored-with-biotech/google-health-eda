# Fitbit data via the Google Health API

Fitbit's own Web API (`api.fitbit.com`) is being retired. Fitbit devices now
expose data through **Google Health API v4** with Google OAuth. Your Fitbit
account must already be a Google account (Fitbit forced that migration), and
the Fitbit app must be syncing to it.

`fitbit_pull.py` logs in once, then pulls any date range into this folder.

## Layout

```
google-health-eda/
  fitbit_pull.py          the puller
  requirements.txt        python deps (requests, google-auth, google-auth-oauthlib)
  .secrets/               client_secret.json + token.json  (gitignored)
  raw/{type}/{from}_{to}.json   untouched API responses     (gitignored)
  csv/{type}.csv          flattened, one row per data point (gitignored)
```

## One-time Google Cloud setup (~10 min)

1. **Create a project** at https://console.cloud.google.com (any name, e.g. `zenithar-health`).
2. **Enable the API:** https://console.developers.google.com/apis/library/health.googleapis.com
   → Enable "Google Health API".
3. **Consent screen / Audience:** https://console.developers.google.com/auth/audience
   → External. The app stays *unverified / Testing*; that is fine for personal use
   (cap of 100 users). **Add your own Gmail as a test user.**
4. **Scopes:** https://console.developers.google.com/auth/scopes → add these five:
   ```
   https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly
   https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly
   https://www.googleapis.com/auth/googlehealth.sleep.readonly
   https://www.googleapis.com/auth/googlehealth.profile.readonly
   https://www.googleapis.com/auth/googlehealth.settings.readonly
   ```
5. **OAuth client:** https://console.developers.google.com/apis/credentials
   → Create credentials → OAuth client ID → **Desktop app**.
   Download the JSON and save it as `.secrets/client_secret.json` in this folder.

   (Google's quickstart shows a "Web application" client with redirect
   `https://www.google.com` and a copy-paste code. The Desktop type is what
   Google's own `google-health-cli` uses and avoids the paste step.)

## Install

```
python3 -m pip install -r requirements.txt
```
Python 3.10+ (the script uses `X | Y` type syntax). No other local code is
required; everything lives in this folder.

## Use

```
cd google-health-eda
python3 fitbit_pull.py auth                       # browser opens once; token cached
python3 fitbit_pull.py whoami                     # identity, timezone, paired devices
python3 fitbit_pull.py pull --days 30             # default types, last 30 days
python3 fitbit_pull.py pull --from 2026-06-01 --to 2026-09-13
python3 fitbit_pull.py pull --days 7 --types heart-rate      # opt-in, large
```

`--to` is exclusive. Re-pulling an overlapping range is safe: CSVs are deduped
by data-point name/start time and the newer pull wins.

## What gets pulled by default

| Type | Mode | Notes |
|---|---|---|
| steps, distance, total-calories, active-minutes, floors | dailyRollUp | one row per day |
| active-zone-minutes, exercise, sleep | list | sleep/exercise pages are capped at 25 by the API; script paginates |
| daily-resting-heart-rate, daily-heart-rate-variability, daily-oxygen-saturation, daily-respiratory-rate | list | one row per day |
| weight | list | one row per weigh-in |

Opt-in with `--types`: `heart-rate` (one sample every few seconds; pulled a day
at a time), `heart-rate-variability`, `oxygen-saturation`, `body-fat`, `vo2-max`,
`daily-heart-rate-zones`, `daily-vo2-max`. Use `--no-rollup` to get raw minute
intervals for the rollup types, or `--also-list` to get both.

**A missing day is not a zero.** If a date is absent, the watch wasn't worn or
didn't sync.

## Filter syntax the script uses (verified 2026-09-12)

| Kind | Filter field | Time format |
|---|---|---|
| interval (steps, exercise, …) | `{type}.interval.civil_start_time` | `YYYY-MM-DDT00:00:00` (local, no Z) |
| sleep | `sleep.interval.civil_end_time` | civil; sleep is filterable on end-time only |
| sample (heart-rate, weight, …) | `{type}.sample_time.physical_time` | RFC-3339 with `Z` |
| daily summary | `{type}.date` | `YYYY-MM-DD` |

Only `>=` and `<` comparators exist. Sources: Google Health REST reference
(`users.dataTypes.dataPoints.list`, `dailyRollUp`), the data-types page, and
the type registry in Google's `google-health-cli` repo.

## Troubleshooting

- `403 access_not_configured` → step 2 not done.
- `Error 403: access_denied` at login → your Gmail is not on the test-user list (step 3).
- `invalid_scope` → step 4 missing a scope.
- Empty results with no error → check `whoami` shows a paired device, and that the
  Fitbit app has synced. Civil-time filters use the timezone in `settings`.
- Delete `.secrets/token.json` and re-run `auth` to force a fresh consent.
