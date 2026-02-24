#!/usr/bin/env python3
"""
Unifi Protect → Orbit bhyve Sprinkler Controller

Listens for a Unifi Protect webhook notification and activates a configured
zone on an Orbit bhyve smart water timer.

bhyve API reference (community-documented):
  Base URL : https://api.orbitbhyve.com/v1
  App ID   : dad3e38c-9af4-4960-aa76-9e51e8ba5c2c
  Login    : POST /session  {"session":{"email":"...","password":"..."}}
             → {"orbit_session_token":"...", "user_id":"..."}
  Run zone : PATCH /devices/{device_id}
             Headers: orbit-api-key: <token>, orbit-app-id: <app_id>
             Body: {"device":{"manual_preset_runtime":<min>,
                              "zones":[{"station":<n>,"run_time":<min>}]}}
"""

import json
import logging
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

log = logging.getLogger("sprinkler")

# ─── Configuration ────────────────────────────────────────────────────────────

class Config:
    def __init__(self):
        self.bhyve_email      = os.environ.get("BHYVE_EMAIL", "")
        self.bhyve_password   = os.environ.get("BHYVE_PASSWORD", "")
        self.bhyve_device_id  = os.environ.get("BHYVE_DEVICE_ID", "")
        self.zone_number      = int(os.environ.get("ZONE_NUMBER", "1"))
        self.run_time         = int(os.environ.get("RUN_TIME", "5"))
        self.trigger_key      = os.environ.get("TRIGGER_KEY", "animal")
        self.webhook_port     = int(os.environ.get("WEBHOOK_PORT", "8383"))
        self.log_level        = os.environ.get("LOG_LEVEL", "INFO").upper()

        errors = []
        if not self.bhyve_email:
            errors.append("BHYVE_EMAIL is required")
        if not self.bhyve_password:
            errors.append("BHYVE_PASSWORD is required")
        if not self.bhyve_device_id:
            errors.append("BHYVE_DEVICE_ID is required")
        if errors:
            for e in errors:
                print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)


# ─── bhyve API Client ─────────────────────────────────────────────────────────

class APIError(Exception):
    pass


class BhyveClient:
    BASE_URL = "https://api.orbitbhyve.com/v1"
    APP_ID   = "dad3e38c-9af4-4960-aa76-9e51e8ba5c2c"
    TIMEOUT  = 15

    def __init__(self, config: Config):
        self.config  = config
        self._token  = None
        self._lock   = threading.Lock()

    # ── internal helpers ──────────────────────────────────────────────────────

    def _request(self, method: str, path: str, body=None, *, auth: bool = False):
        url  = f"{self.BASE_URL}{path}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {
            "Content-Type": "application/json",
            "orbit-app-id": self.APP_ID,
        }
        if auth and self._token:
            headers["orbit-api-key"] = self._token

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.TIMEOUT) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as exc:
            body_txt = exc.read().decode()
            raise APIError(f"HTTP {exc.code} {method} {path}: {body_txt}") from exc
        except urllib.error.URLError as exc:
            raise APIError(f"Network error {method} {path}: {exc.reason}") from exc

    # ── public methods ────────────────────────────────────────────────────────

    def login(self):
        """Authenticate and cache the session token."""
        log.debug("Logging in to bhyve as %s", self.config.bhyve_email)
        resp = self._request("POST", "/session", {
            "session": {
                "email":    self.config.bhyve_email,
                "password": self.config.bhyve_password,
            }
        })
        token = resp.get("orbit_session_token")
        if not token:
            raise APIError(f"No session token in login response: {resp}")
        with self._lock:
            self._token = token
        log.info("bhyve login successful (user_id=%s)", resp.get("user_id", "?"))

    def _ensure_logged_in(self):
        with self._lock:
            needs_login = not self._token
        if needs_login:
            self.login()

    def start_zone(self, zone: int, run_time: int):
        """
        Manually run a single zone.

        :param zone:     Station/zone number (1-based)
        :param run_time: Duration in minutes
        """
        self._ensure_logged_in()
        device_id = self.config.bhyve_device_id
        log.info("Starting zone %d on device %s for %d min", zone, device_id, run_time)

        payload = {
            "device": {
                "manual_preset_runtime": run_time,
                "zones": [{"station": zone, "run_time": run_time}],
            }
        }

        try:
            resp = self._request("PATCH", f"/devices/{device_id}", payload, auth=True)
            log.debug("start_zone response: %s", resp)
            return resp
        except APIError as exc:
            # Token may have expired — re-login once and retry
            log.warning("Zone start failed (%s); re-logging in and retrying", exc)
            with self._lock:
                self._token = None
            self.login()
            resp = self._request("PATCH", f"/devices/{device_id}", payload, auth=True)
            log.debug("start_zone retry response: %s", resp)
            return resp


# ─── Sprinkler Controller ─────────────────────────────────────────────────────

class SprinklerController:
    MAX_LOG = 20

    def __init__(self, config: Config, client: BhyveClient):
        self.config       = config
        self.client       = client
        self._lock        = threading.Lock()
        self.status       = "idle"   # idle | activating | running | error
        self.last_zone    = None
        self.last_run_time = None
        self.last_triggered = None
        self.activity_log = []       # [(timestamp_str, message), ...]

    def _add_activity(self, message: str):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            self.activity_log.insert(0, (ts, message))
            if len(self.activity_log) > self.MAX_LOG:
                self.activity_log = self.activity_log[:self.MAX_LOG]
        log.info(message)

    def activate_zone(self, zone: int = None, run_time: int = None):
        """Activate a zone (uses config defaults if not specified)."""
        zone     = zone     if zone     is not None else self.config.zone_number
        run_time = run_time if run_time is not None else self.config.run_time

        with self._lock:
            self.status        = "activating"
            self.last_zone     = zone
            self.last_run_time = run_time
            self.last_triggered = datetime.now().isoformat()

        self._add_activity(f"Activating zone {zone} for {run_time} minute(s)")

        try:
            self.client.start_zone(zone, run_time)
            with self._lock:
                self.status = "running"
            self._add_activity(f"Zone {zone} is running for {run_time} minute(s)")

            # Reset status after runtime elapses
            def _reset():
                time.sleep(run_time * 60)
                with self._lock:
                    if self.status == "running":
                        self.status = "idle"
                self._add_activity(f"Zone {zone} run complete")

            threading.Thread(target=_reset, daemon=True).start()
            return True

        except APIError as exc:
            with self._lock:
                self.status = "error"
            self._add_activity(f"Error activating zone {zone}: {exc}")
            return False

    def get_state(self) -> dict:
        with self._lock:
            return {
                "status":        self.status,
                "last_triggered": self.last_triggered,
                "last_zone":     self.last_zone,
                "last_run_time": self.last_run_time,
            }


# ─── Status Page HTML ─────────────────────────────────────────────────────────

_STATUS_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Sprinkler Controller</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      background: #0f172a; color: #e2e8f0; min-height: 100vh; padding: 2rem;
    }}
    h1 {{ font-size: 1.75rem; color: #38bdf8; margin-bottom: 0.2rem; }}
    .subtitle {{ color: #64748b; font-size: 0.875rem; margin-bottom: 2rem; }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 1.25rem; margin-bottom: 1.25rem;
    }}
    .card {{
      background: #1e293b; border: 1px solid #334155;
      border-radius: 12px; padding: 1.5rem;
    }}
    .card h2 {{
      font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.08em;
      color: #64748b; margin-bottom: 1rem;
    }}
    .stat-row {{
      display: flex; justify-content: space-between; align-items: center;
      padding: 0.45rem 0; border-bottom: 1px solid #1e293b;
      font-size: 0.875rem;
    }}
    .stat-row:last-child {{ border-bottom: none; }}
    .stat-label {{ color: #94a3b8; }}
    .stat-value {{ color: #f1f5f9; font-weight: 500; }}
    .badge {{
      display: inline-block; padding: 0.25rem 0.75rem;
      border-radius: 9999px; font-size: 0.8rem; font-weight: 600;
    }}
    .badge-idle       {{ background: #1e3a5f; color: #38bdf8; }}
    .badge-activating {{ background: #431407; color: #fb923c; }}
    .badge-running    {{ background: #052e16; color: #4ade80; }}
    .badge-error      {{ background: #450a0a; color: #f87171; }}
    form {{ display: flex; flex-direction: column; gap: 1rem; }}
    .field label {{
      display: block; font-size: 0.8rem; color: #94a3b8; margin-bottom: 0.35rem;
    }}
    input[type=number] {{
      width: 100%; padding: 0.6rem 0.85rem;
      background: #0f172a; border: 1px solid #334155;
      border-radius: 8px; color: #f1f5f9; font-size: 0.95rem;
    }}
    input[type=number]:focus {{ outline: none; border-color: #38bdf8; }}
    button {{
      padding: 0.7rem 1rem; background: #0284c7; color: #fff;
      border: none; border-radius: 8px; font-size: 0.95rem;
      font-weight: 600; cursor: pointer; transition: background 0.15s;
    }}
    button:hover {{ background: #0369a1; }}
    button:disabled {{ background: #334155; cursor: not-allowed; }}
    #result {{
      margin-top: 0.75rem; padding: 0.65rem 0.9rem;
      border-radius: 8px; font-size: 0.875rem; display: none;
    }}
    #result.ok  {{ background: #052e16; color: #4ade80; display: block; }}
    #result.err {{ background: #450a0a; color: #f87171; display: block; }}
    .log-list {{ list-style: none; }}
    .log-list li {{
      display: flex; gap: 1rem; padding: 0.45rem 0;
      border-bottom: 1px solid #1e293b; font-size: 0.82rem;
    }}
    .log-list li:last-child {{ border-bottom: none; }}
    .log-ts  {{ color: #475569; white-space: nowrap; flex-shrink: 0; }}
    .log-msg {{ color: #cbd5e1; }}
    .empty   {{ color: #475569; font-size: 0.85rem; font-style: italic; }}
  </style>
</head>
<body>
  <h1>Sprinkler Controller</h1>
  <p class="subtitle">Unifi Protect &rarr; Orbit bhyve integration &mdash; port {port}</p>

  <div class="grid">
    <!-- Status card -->
    <div class="card">
      <h2>System Status</h2>
      <div class="stat-row">
        <span class="stat-label">Controller</span>
        <span class="badge badge-{status_class}">{status}</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Device ID</span>
        <span class="stat-value" style="font-size:0.78rem;font-family:monospace">{device_id}</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Default Zone</span>
        <span class="stat-value">{default_zone}</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Default Run Time</span>
        <span class="stat-value">{default_run_time} min</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Trigger Key</span>
        <span class="stat-value">{trigger_key}</span>
      </div>
    </div>

    <!-- Last activation card -->
    <div class="card">
      <h2>Last Activation</h2>
      <div class="stat-row">
        <span class="stat-label">Triggered At</span>
        <span class="stat-value">{last_triggered}</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Zone</span>
        <span class="stat-value">{last_zone}</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Run Time</span>
        <span class="stat-value">{last_run_time}</span>
      </div>
    </div>

    <!-- Test card -->
    <div class="card">
      <h2>Test &mdash; Simulate Webhook</h2>
      <form id="testForm">
        <div class="field">
          <label for="zone">Zone Number (1&ndash;12)</label>
          <input type="number" id="zone" name="zone"
                 min="1" max="12" value="{default_zone}" required>
        </div>
        <div class="field">
          <label for="run_time">Run Time (minutes, 1&ndash;60)</label>
          <input type="number" id="run_time" name="run_time"
                 min="1" max="60" value="{default_run_time}" required>
        </div>
        <button type="submit" id="submitBtn">&#9654; Activate Zone</button>
      </form>
      <div id="result"></div>
    </div>
  </div>

  <!-- Activity log -->
  <div class="card">
    <h2>Activity Log</h2>
    <ul class="log-list">
      {activity_items}
    </ul>
  </div>

  <script>
    const form = document.getElementById('testForm');
    const result = document.getElementById('result');
    const btn = document.getElementById('submitBtn');

    form.addEventListener('submit', async (e) => {{
      e.preventDefault();
      const zone = parseInt(document.getElementById('zone').value, 10);
      const run_time = parseInt(document.getElementById('run_time').value, 10);

      btn.disabled = true;
      btn.textContent = 'Activating\u2026';
      result.className = '';
      result.style.display = 'none';

      try {{
        const resp = await fetch('/test', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ zone, run_time }})
        }});
        const data = await resp.json();
        if (resp.ok && data.activated) {{
          result.className = 'ok';
          result.textContent = `\u2713 Zone ${{zone}} activated for ${{run_time}} minute(s)`;
          setTimeout(() => location.reload(), 2500);
        }} else {{
          result.className = 'err';
          result.textContent = '\u2717 ' + (data.error || 'Activation failed');
        }}
      }} catch (err) {{
        result.className = 'err';
        result.textContent = '\u2717 Request error: ' + err.message;
      }} finally {{
        btn.disabled = false;
        btn.textContent = '\u25b6 Activate Zone';
      }}
    }});

    // Auto-refresh every 15 s to reflect running status
    setTimeout(() => location.reload(), 15000);
  </script>
</body>
</html>
"""


# ─── HTTP Request Handler ─────────────────────────────────────────────────────

class WebhookHandler(BaseHTTPRequestHandler):
    controller: SprinklerController = None
    config: Config = None

    def log_message(self, fmt, *args):
        log.debug("HTTP %s — " + fmt, self.address_string(), *args)

    # ── response helpers ──────────────────────────────────────────────────────

    def _json(self, code: int, body: dict):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _html(self, html: str):
        data = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode())

    # ── routes ────────────────────────────────────────────────────────────────

    def do_GET(self):
        if self.path in ("/", "/status"):
            self._serve_status()
        elif self.path == "/health":
            self._json(200, {"status": "ok", **self.controller.get_state()})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/webhook":
            self._handle_webhook()
        elif self.path == "/test":
            self._handle_test()
        else:
            self._json(404, {"error": "not found"})

    # ── status page ───────────────────────────────────────────────────────────

    def _serve_status(self):
        state = self.controller.get_state()
        status = state["status"]
        badge = {"idle": "idle", "activating": "activating",
                 "running": "running", "error": "error"}.get(status, "idle")

        logs = self.controller.activity_log
        if logs:
            items = "\n      ".join(
                f'<li><span class="log-ts">{ts}</span>'
                f'<span class="log-msg">{msg}</span></li>'
                for ts, msg in logs
            )
        else:
            items = '<li><span class="empty">No activity yet</span></li>'

        html = _STATUS_HTML.format(
            port=self.config.webhook_port,
            status=status.upper(),
            status_class=badge,
            device_id=self.config.bhyve_device_id,
            default_zone=self.config.zone_number,
            default_run_time=self.config.run_time,
            trigger_key=self.config.trigger_key,
            last_triggered=state["last_triggered"] or "Never",
            last_zone=str(state["last_zone"]) if state["last_zone"] is not None else "—",
            last_run_time=f"{state['last_run_time']} min" if state["last_run_time"] else "—",
            activity_items=items,
        )
        self._html(html)

    # ── webhook handler ───────────────────────────────────────────────────────

    def _handle_webhook(self):
        try:
            data = self._read_body()
        except (json.JSONDecodeError, ValueError) as exc:
            log.warning("Bad webhook payload: %s", exc)
            self._json(400, {"error": "invalid JSON"})
            return

        log.debug("Webhook received: %s", json.dumps(data))

        # Match trigger key in alarm.triggers[].key  (same pattern as trimlight project)
        alarm = data.get("alarm") or data.get("Alarm") or {}
        triggers = []
        if isinstance(alarm, dict):
            triggers = alarm.get("triggers") or alarm.get("Triggers") or []

        matched = any(
            t.get("key") == self.config.trigger_key or
            t.get("Key") == self.config.trigger_key
            for t in triggers
            if isinstance(t, dict)
        )

        if matched:
            log.info("Webhook trigger matched: key=%s", self.config.trigger_key)
            threading.Thread(
                target=self.controller.activate_zone, daemon=True
            ).start()
            self._json(200, {"triggered": True})
        else:
            log.debug("Webhook received but trigger key '%s' not matched",
                      self.config.trigger_key)
            self._json(200, {"triggered": False})

    # ── test handler ──────────────────────────────────────────────────────────

    def _handle_test(self):
        try:
            data = self._read_body()
        except (json.JSONDecodeError, ValueError):
            self._json(400, {"error": "invalid JSON"})
            return

        try:
            zone     = int(data.get("zone", self.config.zone_number))
            run_time = int(data.get("run_time", self.config.run_time))
            if not (1 <= zone <= 12):
                raise ValueError("zone must be 1–12")
            if not (1 <= run_time <= 60):
                raise ValueError("run_time must be 1–60")
        except (TypeError, ValueError) as exc:
            self._json(400, {"error": str(exc)})
            return

        log.info("Test activation requested: zone=%d, run_time=%d min", zone, run_time)
        threading.Thread(
            target=self.controller.activate_zone,
            kwargs={"zone": zone, "run_time": run_time},
            daemon=True,
        ).start()
        self._json(200, {"activated": True, "zone": zone, "run_time": run_time})


# ─── Entry Point ──────────────────────────────────────────────────────────────

def main():
    config = Config()

    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    log.info("Starting Unifi Protect → bhyve sprinkler controller")
    log.info("Device: %s | Zone: %d | Run time: %d min | Trigger key: %s",
             config.bhyve_device_id, config.zone_number,
             config.run_time, config.trigger_key)

    client     = BhyveClient(config)
    controller = SprinklerController(config, client)

    # Validate credentials at startup
    try:
        client.login()
    except APIError as exc:
        log.error("bhyve login failed: %s", exc)
        sys.exit(1)

    WebhookHandler.controller = controller
    WebhookHandler.config     = config

    server = HTTPServer(("0.0.0.0", config.webhook_port), WebhookHandler)
    log.info("Listening on port %d", config.webhook_port)
    log.info("Status page → http://0.0.0.0:%d/", config.webhook_port)

    def _shutdown(sig, _frame):
        log.info("Shutting down (signal %d)…", sig)
        threading.Thread(target=server.shutdown, daemon=True).start()
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        server.serve_forever()
    except SystemExit:
        pass


if __name__ == "__main__":
    main()
