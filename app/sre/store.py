"""File-backed persistence for applications, incidents and the audit log.

shortcut: one JSON file per record with a process lock; fine for a single
AegisOps replica. Move to PostgreSQL (tables mirroring these models) before
running more than one replica.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from app.sre.models import IncidentState, RegisteredApplication, utcnow

_SAFE_ID = set("abcdefghijklmnopqrstuvwxyz0123456789-")


def _check_id(value: str) -> str:
    if not value or not set(value) <= _SAFE_ID or len(value) > 64:
        raise KeyError(value)
    return value


class StateStore:
    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root or os.environ.get("AEGIS_STATE_DIR", "data/sre_state"))
        for sub in ("applications", "incidents"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # ── helpers ──
    def _write(self, path: Path, payload: str) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(payload, encoding="utf-8")
        for attempt in range(20):
            try:
                os.replace(tmp, path)  # atomic: a crash never leaves a half-written record
                return
            except PermissionError:  # Windows: target briefly open by a concurrent reader
                if attempt == 19:
                    raise
                time.sleep(0.05)

    # ── applications ──
    @staticmethod
    def hash_key(key: str) -> str:
        return hashlib.sha256(key.encode()).hexdigest()

    def register_application(self, app: RegisteredApplication) -> tuple[RegisteredApplication, str]:
        with self._lock:
            existing = self.find_application(app.name)
            if existing:
                app.app_id = existing.app_id
            key = "aeg_" + secrets.token_urlsafe(32)
            app.ingest_key_hash = self.hash_key(key)
            self._write(self.root / "applications" / f"{app.app_id}.json", app.model_dump_json(indent=2))
        return app, key

    def save_application(self, app: RegisteredApplication) -> None:
        with self._lock:
            self._write(self.root / "applications" / f"{app.app_id}.json", app.model_dump_json(indent=2))

    def list_applications(self) -> list[RegisteredApplication]:
        return [
            RegisteredApplication.model_validate_json(p.read_text(encoding="utf-8"))
            for p in sorted((self.root / "applications").glob("*.json"))
        ]

    def find_application(self, name_or_id: str) -> RegisteredApplication | None:
        return next((a for a in self.list_applications() if name_or_id in (a.name, a.app_id)), None)

    def verify_ingest_key(self, app: RegisteredApplication, key: str | None) -> bool:
        return bool(key) and secrets.compare_digest(app.ingest_key_hash, self.hash_key(key or ""))

    # ── incidents ──
    def save_incident(self, state: IncidentState) -> None:
        state.updated_at = utcnow()
        with self._lock:
            self._write(self.root / "incidents" / f"{_check_id(state.incident_id)}.json", state.model_dump_json(indent=2))

    def get_incident(self, incident_id: str) -> IncidentState | None:
        try:
            path = self.root / "incidents" / f"{_check_id(incident_id)}.json"
        except KeyError:
            return None
        if not path.exists():
            return None
        return IncidentState.model_validate_json(path.read_text(encoding="utf-8"))

    def list_incidents(self, application: str | None = None) -> list[IncidentState]:
        items = [
            IncidentState.model_validate_json(p.read_text(encoding="utf-8"))
            for p in (self.root / "incidents").glob("*.json")
        ]
        if application:
            items = [i for i in items if i.application == application]
        return sorted(items, key=lambda i: i.created_at, reverse=True)

    # ── ingested application logs (from webhooks / SDK exception reports) ──
    def append_logs(self, application: str, entries: list[dict[str, Any]]) -> None:
        (self.root / "logs").mkdir(exist_ok=True)
        with self._lock, (self.root / "logs" / f"{_check_id(application)}.jsonl").open("a", encoding="utf-8") as fh:
            for e in entries:
                fh.write(json.dumps(e, default=str) + "\n")

    def read_logs(self, application: str, limit: int = 1000) -> list[dict[str, Any]]:
        path = self.root / "logs" / f"{_check_id(application)}.jsonl"
        if not path.exists():
            return []
        return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()[-limit:]]

    # ── audit ──
    def audit(self, actor: str, action: str, **details: Any) -> None:
        record = {"at": utcnow().isoformat(), "actor": actor, "action": action, **details}
        with self._lock, (self.root / "audit.log.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")

    def read_audit(self, limit: int = 200) -> list[dict[str, Any]]:
        path = self.root / "audit.log.jsonl"
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
        return [json.loads(line) for line in lines]
