# unifiprotect-sprinkler

A lightweight webhook receiver that bridges **Unifi Protect** smart detection events to an **Orbit bhyve** smart water timer. When a configured camera trigger fires (e.g. animal detection), a specified sprinkler zone activates automatically.

Runs as a single Docker container with no external Python dependencies (stdlib only).

---

## How It Works

```
Unifi Protect Camera
       │
       │  POST /webhook  {"alarm":{"triggers":[{"key":"animal"}]}}
       ▼
 sprinkler container (port 8383)
       │
       │  PATCH /v1/devices/{id}   {"device":{"zones":[{"station":1,"run_time":5}]}}
       ▼
 Orbit bhyve Cloud API
       │
       ▼
 Sprinkler zone activates
```

1. Unifi Protect fires a webhook on a smart detection event.
2. The app checks the payload for a matching trigger key (`animal`, `person`, `vehicle`, etc.).
3. On match it authenticates with the bhyve cloud API and sends a manual-run command for the configured zone.
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
# Step 1 — get a session token
TOKEN=$(curl -s -X POST https://api.orbitbhyve.com/v1/session \
  -H "Content-Type: application/json" \
  -d '{"session":{"email":"you@example.com","password":"yourpass"}}' \
  | jq -r '.orbit_session_token')

# Step 2 — list devices and their IDs
curl -s https://api.orbitbhyve.com/v1/devices \
  -H "orbit-api-key: $TOKEN" \
  -H "orbit-app-id: dad3e38c-9af4-4960-aa76-9e51e8ba5c2c" \
  | jq '.[] | {id, name}'
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

Set `TRIGGER_KEY` in `.env` to match the detection type you want to act on.

---

## Configuration

All configuration is via environment variables, set in `.env`:

| Variable         | Required | Default  | Description                                                  |
|------------------|----------|----------|--------------------------------------------------------------|
| `BHYVE_EMAIL`    | ✅       | —        | Email address for your Orbit bhyve account                   |
| `BHYVE_PASSWORD` | ✅       | —        | Password for your Orbit bhyve account                        |
| `BHYVE_DEVICE_ID`| ✅       | —        | bhyve device/timer ID (see [Quick Start](#quick-start))      |
| `ZONE_NUMBER`    |          | `1`      | Zone/station to activate (1-based, matches bhyve app)        |
| `RUN_TIME`       |          | `5`      | How long to run the zone, in minutes                         |
| `TRIGGER_KEY`    |          | `animal` | Unifi Protect trigger key to match (`animal`, `person`, etc.)|
| `WEBHOOK_PORT`   |          | `8383`   | Port the HTTP server listens on                              |
| `LOG_LEVEL`      |          | `INFO`   | Log verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR`           |

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

Response:
```json
{ "triggered": true }
```

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
2026-02-23 18:00:01 INFO     sprinkler: Starting Unifi Protect → bhyve sprinkler controller
2026-02-23 18:00:01 INFO     sprinkler: Device: abc123 | Zone: 1 | Run time: 5 min | Trigger key: animal
2026-02-23 18:00:02 INFO     sprinkler: bhyve login successful (user_id=456789)
2026-02-23 18:00:02 INFO     sprinkler: Listening on port 8383
2026-02-23 18:04:11 INFO     sprinkler: Webhook trigger matched: key=animal
2026-02-23 18:04:11 INFO     sprinkler: Activating zone 1 for 5 minute(s)
2026-02-23 18:04:12 INFO     sprinkler: Zone 1 is running for 5 minute(s)
2026-02-23 18:09:12 INFO     sprinkler: Zone 1 run complete
```

Set `LOG_LEVEL=DEBUG` to see full API request/response payloads.

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

- **Zero dependencies** — pure Python stdlib (`http.server`, `urllib`, `json`, `threading`, `signal`)
- **Thread-safe** — zone activation runs in a background thread; state protected by `threading.Lock`
- **Token refresh** — if the bhyve session token expires, the client re-authenticates transparently on the next request
- **Non-blocking webhook** — the HTTP response is returned immediately; zone activation happens asynchronously
- **Status auto-reset** — after the configured run time elapses, status returns to `idle` automatically

---

## License

MIT
