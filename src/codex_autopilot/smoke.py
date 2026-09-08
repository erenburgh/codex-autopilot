from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile

from .bootstrap import initialize_project
from .config import load_config
from .orchestrator import DesktopOrchestrator
from .run_state import StateStore


def run_desktop_smoke(skill_path: Path, profile: str, keep: bool = False) -> tuple[int, Path]:
    base = Path(tempfile.mkdtemp(prefix="codex-autopilot-smoke-"))
    subprocess.run(["git", "init", "-q", str(base)], check=True)
    plan_file = base / "smoke-plan.json"
    milestones = [
        {"title": "Create artifact A", "objective": "Create artifact-a.txt containing exactly `alpha` followed by a newline.", "definition_of_done": ["artifact-a.txt exists with exact content", "PROJECT_STATE.md and HANDOFF.md are updated"], "execution_mode": "code", "execution_mode_reason": "The outcome is created and verified through repository files."},
        {"title": "Verify artifact A", "objective": "Verify artifact-a.txt, then create verification.txt containing exactly `verified` followed by a newline.", "definition_of_done": ["artifact-a.txt is verified", "verification.txt exists with exact content", "PROJECT_STATE.md and HANDOFF.md are updated"], "execution_mode": "code", "execution_mode_reason": "The outcome is verified through repository files and shell tools."},
    ]
    if profile == "adaptive":
        for item in milestones:
            item["reasoning"] = "medium"
    strategy = "auto" if profile == "adaptive" else "host-settings"
    plan_file.write_text(json.dumps({"goal": "Generic Codex Autopilot Desktop smoke test", "model_strategy": strategy, "milestones": milestones}, indent=2) + "\n", encoding="utf-8")
    initialize_project(base, plan_file, profile=profile, skill_path=skill_path)
    code = DesktopOrchestrator(load_config(base)).run()
    state = StateStore(base / ".codex-autopilot").load()
    ok = code == 0 and state.status == "DONE" and len(state.previous_thread_ids) == 2 and (base / "artifact-a.txt").read_text() == "alpha\n" and (base / "verification.txt").read_text() == "verified\n"
    if not keep and ok:
        # Durable Codex threads remain in Recents; only the disposable working tree is removed by the caller-facing CLI.
        import shutil
        shutil.rmtree(base)
    return (0 if ok else 1), base
