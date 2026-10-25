# Fitbit data via the Google Health API

Fitbit's Web API is being retired; devices now expose data through **Google Health API v4** with Google OAuth. Your Fitbit account must be a Google account and the Fitbit app must be syncing.

`fitbit_pull.py` logs in once, then pulls any date range into `raw/` (untouched JSON) and `csv/` (one row per data point). Both are gitignored.

## One-time Google Cloud setup

1. Create a project at https://console.cloud.google.com.
2. Enable "Google Health API" in the API library.
3. Set the consent screen to External, leave it in Testing, and **add your Gmail as a test user**.
4. Add the five `googlehealth.*.readonly` scopes (activity_and_fitness, health_metrics_and_measurements, sleep, profile, settings).
5. Create an OAuth client of type **Desktop app** and save the JSON as `.secrets/client_secret.json`.

## Install and use

Requires Python 3.10+.

```
python3 -m pip install -r requirements.txt
python3 fitbit_pull.py auth                  # browser opens once; token cached
python3 fitbit_pull.py whoami                # identity, timezone, devices
python3 fitbit_pull.py pull --days 30
python3 fitbit_pull.py pull --from 2026-06-01 --to 2026-09-13
python3 fitbit_pull.py pull --days 7 --types heart-rate   # opt-in, large
```

`--to` is exclusive. Re-pulling overlapping ranges is safe; CSVs are deduped. Defaults cover daily activity, sleep, exercise, resting heart rate, HRV, SpO2, respiratory rate and weight. **A missing day is not a zero**: the watch wasn't worn or didn't sync.

## Troubleshooting

- `403 access_not_configured`: step 2 not done.
- `403 access_denied` at login: your Gmail isn't a test user.
- Empty results: check `whoami` shows a device and the app has synced.
- Delete `.secrets/token.json` and re-run `auth` to force fresh consent.
