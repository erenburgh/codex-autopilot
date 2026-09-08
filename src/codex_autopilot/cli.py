from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from . import __version__
from .appserver import AppServerClient
from .bootstrap import initialize_project, purge_project_state
from .config import STATE_DIR_NAME, load_config
from .control import arm, find_project_root, handle_prompt_hook, handle_stop_hook, pid_alive, spawn_dispatcher, status_text, wait_for_dispatcher
from .models import MODEL_IDS, PUBLIC_REASONING
from .orchestrator import DesktopOrchestrator
from .run_state import StateStore
from .smoke import run_desktop_smoke


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="codex-autopilot")
    top.add_argument("--version", action="version", version=__version__)
    sub = top.add_subparsers(dest="command", required=True)
    bootstrap = sub.add_parser("bootstrap", help="initialize a project from a structured plan")
    bootstrap.add_argument("--project", type=Path, default=Path.cwd())
    bootstrap.add_argument("--plan-file", type=Path, required=True)
    bootstrap.add_argument("--profile", choices=["adaptive", "host-settings"])
    bootstrap.add_argument("--skill-path", type=Path)
    bootstrap.add_argument("--replace", action="store_true")
    start_skill = sub.add_parser("start-skill", help=argparse.SUPPRESS)
    start_skill.add_argument("--project", type=Path, default=Path.cwd())
    start_skill.add_argument("--plan-file", type=Path, required=True)
    start_skill.add_argument("--replace", action="store_true")
    armed = sub.add_parser("arm", help=argparse.SUPPRESS)
    armed.add_argument("--project", type=Path, default=Path.cwd())
    run = sub.add_parser("run", help="advanced foreground dispatcher")
    run.add_argument("--project", type=Path, default=Path.cwd())
    run.add_argument("--detach", action="store_true")
    dispatch = sub.add_parser("_dispatch", help=argparse.SUPPRESS)
    dispatch.add_argument("--project", type=Path, required=True)
    dispatch.add_argument("--initiator-thread")
    dispatch.add_argument("--initiator-turn")
    for name in ("status", "stop", "resume", "logs"):
        item = sub.add_parser(name)
        item.add_argument("--project", type=Path, default=Path.cwd())
    doctor = sub.add_parser("doctor")
    doctor.add_argument("--project", type=Path, default=Path.cwd())
    hook = sub.add_parser("hook", help=argparse.SUPPRESS)
    uninstall = sub.add_parser("uninstall")
    uninstall.add_argument("--yes", action="store_true")
    uninstall.add_argument("--project", type=Path)
    uninstall.add_argument("--purge-project-state", action="store_true")
    test = sub.add_parser("test")
    test_sub = test.add_subparsers(dest="test_command", required=True)
    desktop = test_sub.add_parser("desktop")
    desktop.add_argument("--profile", choices=["adaptive", "host-settings"], default="adaptive")
    desktop.add_argument("--skill-path", type=Path)
    desktop.add_argument("--keep", action="store_true")
    return top


def _profile_and_skill(args) -> tuple[str, Path]:
    profile = getattr(args, "profile", None) or os.environ.get("CODEX_AUTOPILOT_PROFILE") or "adaptive"
    skill_arg = getattr(args, "skill_path", None)
    skill = skill_arg or (Path(os.environ["CODEX_AUTOPILOT_SKILL_PATH"]) if os.environ.get("CODEX_AUTOPILOT_SKILL_PATH") else None)
    if skill is None and os.environ.get("CODEX_AUTOPILOT_INSTALL_ROOT"):
        plugin = f"codex-autopilot-{profile}"
        skill = Path(os.environ["CODEX_AUTOPILOT_INSTALL_ROOT"]) / "current" / "plugins" / plugin / "skills" / plugin / "SKILL.md"
    if profile not in {"adaptive", "host-settings"} or skill is None:
        raise ValueError("profile and installed SKILL.md path are required")
    return profile, skill


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command in {"bootstrap", "start-skill"}:
            profile, skill = _profile_and_skill(args)
            plan = initialize_project(args.project, args.plan_file, profile=profile, skill_path=skill, replace=args.replace)
            if args.command == "start-skill":
                arm(args.project)
            print(f"Initialized {len(plan.milestones)} milestones ({profile})." + (" Dispatcher launch armed for this turn's Stop hook." if args.command == "start-skill" else ""))
            return 0
        if args.command == "arm":
            arm(args.project)
            print("Dispatcher launch armed for this turn's Stop hook.")
            return 0
        if args.command in {"run", "resume"}:
            root = args.project.resolve()
            store = StateStore(root / STATE_DIR_NAME)
            if args.command == "resume":
                state = store.load()
                if state.status == "DONE":
                    print("Already DONE.")
                    return 0
                if state.status == "BLOCKED":
                    print(f"BLOCKED: {state.last_error}", file=sys.stderr)
                    return 78
                if pid_alive(state.dispatcher_pid):
                    print(f"Already running: pid {state.dispatcher_pid}")
                    return 0
                store.clear_pause()
                pid = spawn_dispatcher(root)
                phase = wait_for_dispatcher(root, pid)
                print(f"Resumed: pid {pid}, phase {phase}")
                return 0
            if args.detach:
                pid = spawn_dispatcher(root)
                phase = wait_for_dispatcher(root, pid)
                print(f"Started: pid {pid}, phase {phase}")
                return 0
            return DesktopOrchestrator(load_config(root)).run()
        if args.command == "_dispatch":
            return DesktopOrchestrator(load_config(args.project)).run(args.initiator_thread, args.initiator_turn)
        if args.command == "status":
            print(status_text(args.project))
            return 0
        if args.command == "stop":
            StateStore(load_config(args.project).state_dir).request_pause()
            print("Pause requested.")
            return 0
        if args.command == "logs":
            cfg = load_config(args.project)
            path = cfg.state_dir / "logs" / "dispatcher.log"
            print(path.read_text(encoding="utf-8", errors="replace") if path.exists() else "No dispatcher log yet.", end="")
            return 0
        if args.command == "doctor":
            return doctor(args.project)
        if args.command == "hook":
            payload = json.load(sys.stdin)
            event = payload.get("hook_event_name")
            result = handle_stop_hook(payload) if event == "Stop" else handle_prompt_hook(payload) if event == "UserPromptSubmit" else {}
            print(json.dumps(result, ensure_ascii=False))
            return 0
        if args.command == "test":
            profile, skill = _profile_and_skill(args)
            code, directory = run_desktop_smoke(skill, profile, args.keep)
            print(f"Desktop smoke {'PASS' if code == 0 else 'FAIL'}; workspace={directory}")
            return code
        if args.command == "uninstall":
            return uninstall(args)
    except (ValueError, RuntimeError, FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"codex-autopilot: {exc}", file=sys.stderr)
        return 2
    return 2


def doctor(project: Path) -> int:
    checks: list[tuple[str, bool, str]] = []
    checks.append(("macOS", sys.platform == "darwin", sys.platform))
    checks.append(("Python >=3.11", sys.version_info >= (3, 11), sys.version.split()[0]))
    codex = shutil.which("codex")
    checks.append(("Codex CLI", bool(codex), codex or "not found"))
    if codex:
        auth = subprocess.run([codex, "login", "status"], capture_output=True, text=True)
        checks.append(("Codex authentication", auth.returncode == 0, (auth.stdout or auth.stderr).strip()))
        try:
            with AppServerClient(codex, Path("/tmp/codex-autopilot-doctor.jsonl")) as client:
                profiles = client.list_permission_profiles(project.resolve())
                models = client.list_models()
            allowed = {item.get("id") for item in profiles if item.get("allowed") is not False}
            checks.append(("App Server :workspace", ":workspace" in allowed, str(sorted(allowed))))
            catalog = {str(item.get("model") or item.get("id")): item for item in models}
            for key, model_id in MODEL_IDS.items():
                model = catalog.get(model_id)
                efforts = {str(item.get("reasoningEffort")) for item in (model or {}).get("supportedReasoningEfforts") or []}
                checks.append((f"Model {key}", model is not None, model_id if model else "unavailable"))
                checks.append((f"Model {key} Adaptive efforts", set(PUBLIC_REASONING).issubset(efforts), str(sorted(efforts))))
        except Exception as exc:
            checks.append(("App Server", False, str(exc)))
    for name, ok, details in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name}: {details}")
    return 0 if all(ok for _, ok, _ in checks) else 1


def uninstall(args) -> int:
    if not args.yes:
        print("Re-run with --yes. Project source and state remain unless --purge-project-state is also provided.", file=sys.stderr)
        return 2
    project = args.project.resolve() if args.project else find_project_root(Path.cwd())
    if project and (project / STATE_DIR_NAME / "config.toml").is_file():
        store = StateStore(project / STATE_DIR_NAME)
        state = store.load()
        if pid_alive(state.dispatcher_pid):
            store.request_pause()
            deadline = time.monotonic() + 35
            while pid_alive(state.dispatcher_pid) and time.monotonic() < deadline:
                time.sleep(0.1)
            if pid_alive(state.dispatcher_pid):
                print("Codex Autopilot dispatcher did not stop; uninstall was cancelled to keep the active run intact.", file=sys.stderr)
                return 1
    codex = shutil.which("codex")
    if codex:
        for profile in ("codex-autopilot-adaptive", "codex-autopilot-host-settings"):
            subprocess.run([codex, "plugin", "remove", f"{profile}@codex-autopilot-local"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run([codex, "plugin", "marketplace", "remove", "codex-autopilot-local"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if args.purge_project_state:
        if args.project is None:
            raise ValueError("--purge-project-state requires --project")
        purge_project_state(args.project)
    install_root = os.environ.get("CODEX_AUTOPILOT_INSTALL_ROOT")
    if install_root:
        root = Path(install_root).expanduser().resolve()
        target = root / __version__
        current = root / "current"
        if current.is_symlink() and current.resolve() == target.resolve():
            current.unlink()
        shutil.rmtree(target, ignore_errors=True)
        try:
            root.rmdir()
        except OSError:
            pass
    print("Codex Autopilot uninstalled. Project source and Git repository were preserved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
