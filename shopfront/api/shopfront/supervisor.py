"""Process supervisor for the ShopFront API (container entrypoint).

Runs uvicorn as a child process and restarts it if it crashes. A small control
server on port 8081 (private network only) provides:
  POST /ops/restart          allowlisted remediation (Bearer OPS_TOKEN)
  POST /faults/downtime      demo fault: stop the API for duration_s (X-Fault-Token)
  DELETE /faults/downtime    end the downtime early
  GET  /status
This gives real application downtime (port 8000 closed) without Docker socket access.
"""

from __future__ import annotations

import hmac
import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FAULT_TOKEN = os.environ.get("FAULT_TOKEN", "")
OPS_TOKEN = os.environ.get("OPS_TOKEN", "")
FAULTS_ENABLED = os.environ.get("FAULT_INJECTION_ENABLED", "false").lower() == "true"
CMD = [sys.executable, "-m", "uvicorn", "shopfront.main:app", "--host", "0.0.0.0", "--port", "8000",
       "--no-access-log"]


def log(level: str, message: str) -> None:
    print(json.dumps({"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "level": level,
                      "service": "shopfront-supervisor", "message": message}), flush=True)


class Supervisor:
    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.down_until = 0.0
        self.lock = threading.Lock()

    def start(self) -> None:
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                self.proc = subprocess.Popen(CMD)
                log("info", f"shopfront-api process started (pid {self.proc.pid})")

    def stop(self) -> None:
        with self.lock:
            if self.proc and self.proc.poll() is None:
                self.proc.send_signal(signal.SIGTERM)
                try:
                    self.proc.wait(10)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                log("warning", "shopfront-api process stopped")

    def restart(self) -> None:
        self.down_until = 0.0
        self.stop()
        self.start()

    def loop(self) -> None:
        while True:
            time.sleep(2)
            down = self.down_until and time.time() < self.down_until
            if self.down_until and not down:
                log("info", "Downtime window expired; restarting shopfront-api automatically")
                self.down_until = 0.0
            if not down and (self.proc is None or self.proc.poll() is not None):
                if self.proc is not None:
                    log("error", f"shopfront-api exited with code {self.proc.returncode}; restarting")
                self.start()


sup = Supervisor()


class Control(BaseHTTPRequestHandler):
    def _reply(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _token_ok(self, header: str, expected: str) -> bool:
        got = (self.headers.get(header) or "").removeprefix("Bearer ")
        return bool(expected) and hmac.compare_digest(got, expected)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/status":
            alive = sup.proc is not None and sup.proc.poll() is None
            return self._reply(200, {"api_running": alive, "downtime_remaining_s": max(0, round(sup.down_until - time.time()))})
        self._reply(404, {"detail": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/ops/restart":
            if not self._token_ok("Authorization", OPS_TOKEN):
                return self._reply(401, {"detail": "invalid token"})
            log("info", "Remediation 'restart_service' requested by AegisOps")
            sup.restart()
            return self._reply(200, {"action": "restart_service", "result": "shopfront-api restarted"})
        if self.path == "/faults/downtime":
            if not FAULTS_ENABLED:
                return self._reply(403, {"detail": "fault injection is disabled"})
            if not self._token_ok("X-Fault-Token", FAULT_TOKEN):
                return self._reply(401, {"detail": "invalid token"})
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            duration = min(max(float(body.get("duration_s", 300)), 10), 900)
            sup.down_until = time.time() + duration
            sup.stop()
            log("error", f"shopfront-api taken down for {duration:.0f}s (demo fault); service unavailable")
            return self._reply(200, {"injected": "downtime", "duration_s": duration})
        self._reply(404, {"detail": "not found"})

    def do_DELETE(self) -> None:  # noqa: N802
        if self.path == "/faults/downtime":
            if not self._token_ok("X-Fault-Token", FAULT_TOKEN):
                return self._reply(401, {"detail": "invalid token"})
            sup.restart()
            return self._reply(200, {"cleared": "downtime"})
        self._reply(404, {"detail": "not found"})

    def log_message(self, *_):  # quiet default access log
        pass


def main() -> None:
    signal.signal(signal.SIGTERM, lambda *_: (sup.stop(), sys.exit(0)))
    sup.start()
    threading.Thread(target=sup.loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8081), Control).serve_forever()


if __name__ == "__main__":
    main()
