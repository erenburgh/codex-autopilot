from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import time
import uuid
from typing import Any

from .plan import atomic_json


REGISTRY_SCHEMA = 1
MAX_AGE_SECONDS = 3_600


def _parse_time(value: object) -> float:
    try:
        return datetime.fromisoformat(str(value)).astimezone(timezone.utc).timestamp()
    except (TypeError, ValueError):
        return 0


def registry_directory() -> Path:
    configured = os.environ.get("CODEX_AUTOPILOT_LAUNCH_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    uid = os.getuid() if hasattr(os, "getuid") else os.getpid()
    return Path(tempfile.gettempdir()).resolve() / f"codex-autopilot-{uid}-v0.8" / "launch-requests"


class LaunchRegistry:
    """Short-lived per-user bridge from an initiating Stop hook to a target root."""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = (directory or registry_directory()).expanduser().resolve()

    def add(self, payload: dict[str, Any]) -> str:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.directory.chmod(0o700)
        except OSError:
            pass
        self.prune()
        project_root = str(payload.get("project_root") or "")
        for path in self.directory.glob("*.json"):
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                if str(existing.get("project_root") or "") == project_root:
                    path.unlink(missing_ok=True)
            except (OSError, json.JSONDecodeError):
                path.unlink(missing_ok=True)
        request_id = str(payload.get("request_id") or uuid.uuid4())
        record = dict(payload)
        record.update({"schema_version": REGISTRY_SCHEMA, "request_id": request_id})
        atomic_json(self.directory / f"{request_id}.json", record)
        return request_id

    def remove(self, request_id: str | None) -> None:
        if request_id:
            (self.directory / f"{request_id}.json").unlink(missing_ok=True)

    def prune(self) -> None:
        if not self.directory.is_dir():
            return
        cutoff = time.time() - MAX_AGE_SECONDS
        for path in self.directory.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                armed = _parse_time(data.get("armed_at"))
                if data.get("schema_version") != REGISTRY_SCHEMA or armed < cutoff:
                    path.unlink(missing_ok=True)
            except (OSError, json.JSONDecodeError):
                path.unlink(missing_ok=True)

    def pending(self) -> list[dict[str, Any]]:
        self.prune()
        records: list[dict[str, Any]] = []
        if not self.directory.is_dir():
            return records
        for path in self.directory.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                data["_registry_path"] = str(path)
                records.append(data)
            except (OSError, json.JSONDecodeError):
                continue
        return sorted(records, key=lambda item: (_parse_time(item.get("armed_at")), str(item.get("request_id"))))

    def claim_unique(self, *, project_hint: Path | None = None) -> dict[str, Any] | None:
        records = self.pending()
        if project_hint is not None:
            hint = project_hint.expanduser().resolve()
            matching = [item for item in records if Path(str(item.get("project_root") or "")).expanduser().resolve() == hint]
            records = matching
        if not records:
            return None
        if len(records) != 1:
            targets = ", ".join(str(item.get("project_root")) for item in records)
            raise RuntimeError(f"multiple Autopilot starts are armed; finish one initiating turn at a time: {targets}")
        record = records[0]
        path = Path(record.pop("_registry_path"))
        claimed = path.with_name(f".{path.stem}.claimed-{os.getpid()}.json")
        try:
            os.replace(path, claimed)
        except FileNotFoundError:
            return None
        try:
            return json.loads(claimed.read_text(encoding="utf-8"))
        finally:
            claimed.unlink(missing_ok=True)
