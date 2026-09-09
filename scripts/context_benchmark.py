#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import tempfile

from codex_autopilot.bootstrap import initialize_project, select_milestone
from codex_autopilot.config import load_config
from codex_autopilot.memory import ProjectMemory
from codex_autopilot.orchestrator import build_worker_prompt
from codex_autopilot.plan import load_plan
from codex_autopilot.run_state import StateStore


SAMPLES = {1, 5, 10, 20}


def run_benchmark() -> list[dict[str, int]]:
    with tempfile.TemporaryDirectory(prefix="codex-autopilot-context-") as raw:
        root = Path(raw) / "project"
        root.mkdir()
        (root / ".git").mkdir()
        skill = root / "worker-skill.md"
        skill.write_text("worker\n", encoding="utf-8")
        plan_file = root / "plan.json"
        plan_file.write_text(
            json.dumps(
                {
                    "goal": "Complete twenty independently verifiable features.",
                    "model_strategy": "auto",
                    "milestones": [
                        {
                            "title": f"Feature {index}",
                            "objective": f"Implement and verify feature {index}.",
                            "definition_of_done": [f"Feature {index} has a passing focused test."],
                            "execution_mode": "code",
                            "execution_mode_reason": "Repository files and tests are sufficient.",
                            "reasoning": "medium",
                        }
                        for index in range(1, 21)
                    ],
                }
            ),
            encoding="utf-8",
        )
        initialize_project(root, plan_file, profile="adaptive", skill_path=skill, replace=True)
        cfg = load_config(root)
        plan = load_plan(cfg.state_dir, cfg.profile)
        store = StateStore(cfg.state_dir)
        memory = ProjectMemory(root)
        accumulated_history: list[str] = []
        rows: list[dict[str, int]] = []

        for milestone in range(1, 21):
            for offset in range(5):
                serial = (milestone - 1) * 5 + offset
                statement = (
                    f"Historic observation {serial} from milestone {milestone}: "
                    + "implementation context that an older worker might have copied into its summary " * 6
                )
                accumulated_history.append(statement)
                memory.add_observation(statement=statement, created_by=f"worker-{milestone}")
                memory.add_constraint(
                    statement=f"Constraint {serial}: retain deterministic feature behavior.",
                    origin="agent",
                    created_by=f"worker-{milestone}",
                )
            if milestone not in SAMPLES:
                continue
            state = store.load()
            state.milestone_index = milestone - 1
            state.milestone_id = f"M{milestone}"
            state.worker_sequence = milestone
            store.save(state)
            select_milestone(cfg.state_dir, plan, milestone - 1)
            cfg.state_dir.joinpath("HANDOFF.md").write_text(
                "# Handoff (advisory)\n\nNext: inspect the current milestone and canonical memory.\n",
                encoding="utf-8",
            )
            prompt = build_worker_prompt(cfg, state, plan)
            page = memory.search(query=f"milestone {milestone}", categories=["observation"], limit=8)
            payload = json.dumps({"records": page.records, "next_cursor": page.next_cursor}, ensure_ascii=False)
            summary = memory.export_summary()
            rows.append(
                {
                    "milestone": milestone,
                    "v08_prompt_chars": len(prompt),
                    "v08_prompt_approx_tokens": math.ceil(len(prompt) / 4),
                    "memory_records": sum(summary["records"].values()),
                    "sample_mcp_calls": 1,
                    "sample_mcp_payload_chars": len(payload),
                    "sample_mcp_records": len(page.records),
                    "synthetic_v07_full_history_chars": 2_500 + len("\n".join(accumulated_history)),
                }
            )
        return rows


def markdown(rows: list[dict[str, int]]) -> str:
    lines = [
        "# Context benchmark",
        "",
        "This deterministic synthetic benchmark adds five Observations and five Constraints per milestone. "
        "The v0.8 column measures the real worker prompt builder. The v0.7 column is a labeled synthetic baseline "
        "that prepends all accumulated prose; it is not a measurement from a live v0.7 model run.",
        "",
        "| Milestone | v0.8 prompt chars | Approx. tokens | Memory records | MCP calls sampled | MCP payload chars | Records returned | Synthetic v0.7 full-history chars |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        lines.append(
            f"| M{row['milestone']} | {row['v08_prompt_chars']} | {row['v08_prompt_approx_tokens']} | "
            f"{row['memory_records']} | {row['sample_mcp_calls']} | {row['sample_mcp_payload_chars']} | "
            f"{row['sample_mcp_records']} | {row['synthetic_v07_full_history_chars']} |"
        )
    delta = rows[-1]["v08_prompt_chars"] - rows[0]["v08_prompt_chars"]
    baseline_delta = rows[-1]["synthetic_v07_full_history_chars"] - rows[0]["synthetic_v07_full_history_chars"]
    lines.extend(
        [
            "",
            f"Measured v0.8 initial-prompt growth from M1 to M20: **{delta} characters**.",
            f"Synthetic full-history growth over the same fixture: **{baseline_delta} characters**.",
            "",
            "The MCP sample is one bounded FTS query with limit 8 at each checkpoint. Real workers may make more calls depending on the milestone; the server caps each page at 20 records.",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    rows = run_benchmark()
    content = json.dumps(rows, indent=2) + "\n" if args.json else markdown(rows)
    if args.output:
        args.output.write_text(content, encoding="utf-8")
    else:
        print(content, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
