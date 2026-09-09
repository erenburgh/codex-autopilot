from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import tempfile
import uuid


TERMINAL_STATUSES = {"DONE", "BLOCKED"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class RunState:
    schema_version: int = 4
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: str = "IDLE"
    phase: str = "IDLE"
    milestone_index: int = 0
    worker_sequence: int = 0
    attempt: int = 0
    current_thread_id: str | None = None
    current_turn_id: str | None = None
    previous_thread_ids: list[str] = field(default_factory=list)
    milestone_id: str | None = None
    planned_execution_mode: str | None = None
    execution_mode: str | None = None
    selected_model_key: str | None = None
    selected_model_id: str | None = None
    selected_model_display: str | None = None
    selected_reasoning: str | None = None
    model_selection_reason: str | None = None
    reasoning_adjustment: str | None = None
    capability_escalated: bool = False
    capability_escalation_reason: str | None = None
    last_worker_status: str | None = None
    worker_history: list[dict[str, object]] = field(default_factory=list)
    client_user_message_id: str | None = None
    prompt_sha256: str | None = None
    expected_thread_name: str | None = None
    creation_not_before: int | None = None
    checkpoint_before: dict[str, str] | None = None
    memory_audit_before: int | None = None
    prompt_chars: int | None = None
    prompt_approx_tokens: int | None = None
    memory_records_at_start: int | None = None
    relevant_memory_count: int | None = None
    preflight_completed_at: str | None = None
    retry_count: int = 0
    retry_at: int | None = None
    reset_at: int | None = None
    last_error: str | None = None
    last_final_message: str | None = None
    permission_profile: str | None = None
    project_id: str | None = None
    dispatcher_pid: int | None = None
    initiator_thread_id: str | None = None
    initiator_turn_id: str | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    completed_at: str | None = None

    def touch(self) -> None:
        self.updated_at = utc_now()


class StateStore:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.path = state_dir / "run-state.json"
        self.lock_path = state_dir / "dispatcher.lock"
        self.pause_path = state_dir / "pause-requested"
        self.launch_path = state_dir / "launch-request.json"
        self._lock_handle = None

    def acquire(self) -> None:
        handle = self.lock_path.open("a", encoding="utf-8")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise RuntimeError("Another Codex Autopilot dispatcher already holds this project lock")
        self._lock_handle = handle

    def release(self) -> None:
        if self._lock_handle:
            fcntl.flock(self._lock_handle, fcntl.LOCK_UN)
            self._lock_handle.close()
            self._lock_handle = None

    def load(self) -> RunState:
        if not self.path.exists():
            return RunState()
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if data.get("schema_version") != 4:
            raise ValueError("Unsupported run-state schema. Run the v0.8 start workflow to migrate v0.7 state safely.")
        known = RunState.__dataclass_fields__
        return RunState(**{key: value for key, value in data.items() if key in known})

    def save(self, state: RunState) -> None:
        state.touch()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        fd, raw = tempfile.mkstemp(prefix=".run-state-", dir=self.state_dir)
        temp = Path(raw)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(asdict(state), handle, ensure_ascii=False, indent=2, allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
        finally:
            temp.unlink(missing_ok=True)

    def request_pause(self) -> None:
        self.pause_path.write_text(utc_now() + "\n", encoding="utf-8")

    def pause_requested(self) -> bool:
        return self.pause_path.exists()

    def clear_pause(self) -> None:
        self.pause_path.unlink(missing_ok=True)

    def arm(self, payload: dict[str, object]) -> None:
        from .plan import atomic_json
        atomic_json(self.launch_path, payload)

    def claim_launch(self) -> dict[str, object] | None:
        if not self.launch_path.is_file():
            return None
        claimed = self.state_dir / f".launch-claimed-{os.getpid()}.json"
        try:
            os.replace(self.launch_path, claimed)
        except FileNotFoundError:
            return None
        try:
            return json.loads(claimed.read_text(encoding="utf-8"))
        finally:
            claimed.unlink(missing_ok=True)
