# unifiprotect-sprinkler

A lightweight webhook receiver that bridges **Unifi Protect** smart detection events to an **Orbit bhyve** smart water timer. When a configured camera trigger fires (e.g. animal detection), a specified sprinkler zone activates automatically.

Runs as a single Docker container with no external Python dependencies (stdlib only).

---

## How It Works

```
Unifi Protect Camera
       │
       │  POST /webhook  (JSON payload)
       ▼
 sprinkler container (port 8383)
       │
       │  WebSocket  change_mode/manual
       ▼
 Orbit bhyve Cloud API
       │
       ▼
 Sprinkler zone activates
```

1. Unifi Protect fires a webhook on a smart detection event.
2. The app checks the payload for a matching trigger key (`animal`, `person`, `vehicle`, etc.).
3. On match it connects to the bhyve WebSocket API and sends a `change_mode/manual` command for the configured zone.
4. The zone runs for the configured duration and status resets automatically.

---

## Quick Start

### 1. Clone and configure

```bash
git clone https://github.com/WJBarrow/unifiprotect-sprinkler.git
cd unifiprotect-sprinkler
cp .env.example .env
```

Edit `.env` with your credentials (see [Configuration](#configuration)).

### 2. Find your bhyve Device ID

You need your bhyve device ID before starting. Use your account credentials to query the API:

```bash
# Step 1 — get a session token  (field is "orbit_api_key" in the response)
TOKEN=$(curl -s -X POST https://api.orbitbhyve.com/v1/session \
  -H "Content-Type: application/json" \
  -H "orbit-app-id: dad3e38c-9af4-4960-aa76-9e51e8ba5c2c" \
  -d '{"session":{"email":"you@example.com","password":"yourpass"}}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['orbit_api_key'])")

# Step 2 — list devices and their IDs
curl -s https://api.orbitbhyve.com/v1/devices \
  -H "orbit-api-key: $TOKEN" \
  -H "orbit-app-id: dad3e38c-9af4-4960-aa76-9e51e8ba5c2c" \
  | python3 -c "import sys,json; [print(d['id'], d.get('name')) for d in json.load(sys.stdin)]"
```

Copy the ID into `BHYVE_DEVICE_ID` in your `.env`.

### 3. Start the container

```bash
docker compose up -d
```

Open the status page: **http://your-host:8383/**

### 4. Configure Unifi Protect

In the Unifi Protect UI:

1. Go to **Settings → Notifications → Webhooks** (or **Alarm Manager**).
2. Add a new webhook with method `POST` and URL:
   ```
   http://your-host:8383/webhook
   ```
3. Configure which cameras and detection types should fire the webhook.

Unifi Protect sends a payload like:
```json
{
  "alarm": {
    "triggers": [
      { "key": "animal" }
    ]
  }
}
```

Set `TRIGGER_KEY` in `.env` to match the detection type you want to act on (`animal`, `person`, `vehicle`, etc.).

---

## Configuration

All configuration is via environment variables, set in `.env`:

| Variable          | Required | Default                | Description                                                  |
|-------------------|----------|------------------------|--------------------------------------------------------------|
| `BHYVE_EMAIL`     | ✅       | —                      | Email address for your Orbit bhyve account                   |
| `BHYVE_PASSWORD`  | ✅       | —                      | Password for your Orbit bhyve account                        |
| `BHYVE_DEVICE_ID` | ✅       | —                      | bhyve device/timer ID (see [Quick Start](#quick-start))      |
| `ZONE_NUMBER`     |          | `1`                    | Zone/station to activate (1-based, matches bhyve app)        |
| `RUN_TIME`        |          | `5`                    | How long to run the zone, in minutes                         |
| `TRIGGER_KEY`     |          | `animal`               | Unifi Protect trigger key to match (`animal`, `person`, etc.)|
| `WEBHOOK_PORT`    |          | `8383`                 | Port the HTTP server listens on                              |
| `LOG_LEVEL`       |          | `INFO`                 | Log verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR`           |
| `LOG_FILE`        |          | `/data/activity.log`   | File to write activity log to (rotated at 10 MB, 3 backups). Set to empty string to disable. |

---

## Status Page & Testing

The web UI at **http://your-host:8383/** provides:

- **System Status** — current controller state (idle / activating / running / error), device ID, default zone and run time, configured trigger key
- **Last Activation** — timestamp, zone, and duration of the most recent activation
- **Test Form** — simulate a webhook trigger with custom zone and run time inputs (no camera required for testing)
- **Activity Log** — last 20 events with timestamps, auto-refreshes every 15 seconds

![Status page showing idle state with test form](docs/status-page.png)

### API Endpoints

| Method | Path       | Description                                              |
|--------|------------|----------------------------------------------------------|
| `GET`  | `/`        | Status page (HTML)                                       |
| `GET`  | `/health`  | JSON health check `{"status":"ok", ...}`                 |
| `POST` | `/webhook` | Unifi Protect webhook receiver                           |
| `POST` | `/test`    | Manually trigger a zone (JSON body required — see below) |

#### `POST /webhook`

Expected body (Unifi Protect format):
```json
{
  "alarm": {
    "triggers": [
      { "key": "animal" }
    ]
  }
}
```

To activate a specific zone from the webhook (overriding `ZONE_NUMBER`), include a `zone` field at the top level:
```json
{
  "zone": 2,
  "alarm": {
    "triggers": [
      { "key": "animal" }
    ]
  }
}
```

If `zone` is absent or invalid, the default `ZONE_NUMBER` from `.env` is used.

Response:
```json
{ "triggered": true }
```

> **Note:** Unifi Protect wraps its webhook body in an outer JSON string. The app automatically unwraps this double-encoding before parsing.

#### `POST /test`

```bash
curl -X POST http://your-host:8383/test \
  -H "Content-Type: application/json" \
  -d '{"zone": 2, "run_time": 3}'
```

Body fields:

| Field      | Type    | Range  | Description                                  |
|------------|---------|--------|----------------------------------------------|
| `zone`     | integer | 1–12   | Zone/station to run (overrides `ZONE_NUMBER`) |
| `run_time` | integer | 1–60   | Minutes to run (overrides `RUN_TIME`)         |

Response:
```json
{ "activated": true, "zone": 2, "run_time": 3 }
```

---

## Logs

```bash
# Follow live logs
docker compose logs -f

# Example output
2026-02-26 18:00:01 INFO     sprinkler: Starting Unifi Protect → bhyve sprinkler controller
2026-02-26 18:00:01 INFO     sprinkler: Device: abc123 | Zone: 3 | Run time: 1 min | Trigger key: animal
2026-02-26 18:00:02 INFO     sprinkler: bhyve login successful (user_id=456789)
2026-02-26 18:00:02 INFO     sprinkler: bhyve WebSocket connected and ready
2026-02-26 18:00:02 INFO     sprinkler: Listening on port 8383
2026-02-26 18:04:11 INFO     sprinkler: Webhook trigger matched: key=animal, zone=3
2026-02-26 18:04:11 INFO     sprinkler: Activating zone 3 for 1 minute(s)
2026-02-26 18:04:12 INFO     sprinkler: Zone 3 is running for 1 minute(s)
2026-02-26 18:05:12 INFO     sprinkler: Zone 3 run complete
```

Set `LOG_LEVEL=DEBUG` to see full WebSocket message payloads.

### Activity Log File

All activity is also written to a persistent log file (default: `./data/activity.log` on the host, mounted into the container at `/data/activity.log`). The file is rotated automatically when it reaches 10 MB, keeping 3 backups.

```bash
# Tail the persistent log file
tail -f data/activity.log
```

To disable file logging, set `LOG_FILE=` (empty) in `.env`.

The activity log in the status page UI displays timestamps in your browser's local time zone.

---

## Running Without Docker

Requires Python 3.8+ with no additional packages:

```bash
export BHYVE_EMAIL=you@example.com
export BHYVE_PASSWORD=yourpass
export BHYVE_DEVICE_ID=your_device_id
python sprinkler.py
```

---

## Architecture Notes

- **Zero dependencies** — pure Python stdlib (`http.server`, `urllib`, `json`, `threading`, `signal`, `websocket` via `websocket-client` bundled in image)
- **Thread-safe** — zone activation runs in a background thread; state protected by `threading.Lock`
- **WebSocket activation** — connects to the bhyve WebSocket API per activation using `orbit_session_token` for authentication; sends a `change_mode/manual` command with the target station and run time
- **Connect-on-demand** — a fresh WebSocket connection is opened for each activation (bhyve closes idle connections after ~35 seconds)
- **Non-blocking webhook** — the HTTP response is returned immediately; zone activation happens asynchronously
- **Status auto-reset** — after the configured run time elapses, status returns to `idle` automatically

---

## License

MIT
