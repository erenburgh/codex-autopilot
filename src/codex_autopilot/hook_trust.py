from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import tempfile
from typing import Any, Callable

from .appserver import AppServerClient


AUTOPILOT_MARKETPLACE = "codex-autopilot-local"
TRUSTED_HOOK_STATUSES = frozenset({"trusted", "managed"})
REVIEW_HOOK_STATUSES = frozenset({"modified", "untrusted"})


class HookPreflightError(RuntimeError):
    """The supported App Server hook inventory is missing or unsafe."""


class HookTrustApprovalRequired(HookPreflightError):
    exit_code = 77

    def __init__(self, *, plugin_id: str, trust_status: str, current_hash: str) -> None:
        self.plugin_id = plugin_id
        self.trust_status = trust_status
        self.current_hash = current_hash
        super().__init__(
            "Codex Autopilot Stop hook: APPROVAL REQUIRED\n\n"
            "No production worker was created. Open `/hooks` and trust the current "
            f"Stop hook for `{plugin_id}` exactly once, then retry. "
            f"trustStatus={trust_status}; currentHash={current_hash}. "
            "Autopilot never changes or bypasses hook trust."
        )


@dataclass(frozen=True, slots=True)
class HookTrustSnapshot:
    cwd: str
    plugin_id: str
    command: str
    trust_status: str
    current_hash: str


def installed_runtime_path() -> Path:
    configured = os.environ.get("CODEX_AUTOPILOT_RUNTIME")
    if configured:
        return Path(configured).expanduser().absolute()
    install_root = Path(
        os.environ.get(
            "CODEX_AUTOPILOT_INSTALL_ROOT",
            str(Path.home() / "Library/Application Support/CodexAutopilot"),
        )
    ).expanduser()
    return (install_root / "current/bin/codex-autopilot").absolute()


def runtime_hook_command(runtime: Path | None = None) -> str:
    # Do not resolve the `current` symlink: its stable spelling is part of the
    # hook definition and therefore part of Codex's trust identity.
    path = (runtime or installed_runtime_path()).expanduser().absolute()
    return f'"{path}" hook'


def autopilot_plugin_id(skill_name: str) -> str:
    if skill_name not in {
        "codex-autopilot-adaptive",
        "codex-autopilot-host-settings",
    }:
        raise HookPreflightError(f"unsupported Autopilot plugin identity: {skill_name}")
    return f"{skill_name}@{AUTOPILOT_MARKETPLACE}"


def require_trusted_stop_hook(
    client: Any,
    cwd: Path,
    *,
    plugin_id: str,
    expected_command: str | None = None,
) -> HookTrustSnapshot:
    """Fail closed unless hooks/list proves the exact Stop hook is executable."""

    project = cwd.expanduser().resolve()
    inventories = client.list_hooks(project)
    matching_cwds = [
        item
        for item in inventories
        if isinstance(item, dict)
        and _same_path(item.get("cwd"), project)
    ]
    if len(matching_cwds) != 1:
        raise HookPreflightError(
            "Autopilot Stop hook preflight failed: hooks/list did not return exactly "
            f"one inventory for {project}"
        )
    inventory = matching_cwds[0]
    errors = inventory.get("errors") or []
    if errors:
        raise HookPreflightError(
            "Autopilot Stop hook preflight failed: hooks/list reported errors: "
            + "; ".join(str(item) for item in errors)
        )
    hooks = inventory.get("hooks")
    if not isinstance(hooks, list):
        raise HookPreflightError(
            "Autopilot Stop hook preflight failed: hooks/list returned no hook array"
        )
    matches = [
        item
        for item in hooks
        if isinstance(item, dict)
        and item.get("pluginId") == plugin_id
        and str(item.get("eventName") or "").lower() == "stop"
    ]
    if len(matches) != 1:
        detail = "missing" if not matches else f"duplicated ({len(matches)})"
        raise HookPreflightError(
            f"Autopilot Stop hook preflight failed: exact {plugin_id} Stop hook is {detail}"
        )
    hook = matches[0]
    if hook.get("enabled") is not True:
        raise HookPreflightError(
            f"Autopilot Stop hook preflight failed: {plugin_id} Stop hook is disabled"
        )
    if hook.get("handlerType") != "command":
        raise HookPreflightError(
            f"Autopilot Stop hook preflight failed: {plugin_id} Stop hook is not a command hook"
        )
    trust_status = str(hook.get("trustStatus") or "unknown").lower()
    current_hash = str(hook.get("currentHash") or "unknown")
    if trust_status in REVIEW_HOOK_STATUSES:
        raise HookTrustApprovalRequired(
            plugin_id=plugin_id,
            trust_status=trust_status,
            current_hash=current_hash,
        )
    if trust_status not in TRUSTED_HOOK_STATUSES:
        raise HookPreflightError(
            "Autopilot Stop hook preflight failed: unsupported trust status "
            f"{trust_status!r} for {plugin_id}"
        )
    command = str(hook.get("command") or "")
    wanted = expected_command or runtime_hook_command()
    if command != wanted:
        raise HookPreflightError(
            "Autopilot Stop hook preflight failed: trusted hook command does not use "
            f"the stable installed runtime entrypoint; expected {wanted!r}, got {command!r}"
        )
    return HookTrustSnapshot(
        cwd=str(project),
        plugin_id=plugin_id,
        command=command,
        trust_status=trust_status,
        current_hash=current_hash,
    )


def require_trusted_stop_hook_for_config(
    cfg: Any,
    *,
    client_factory: Callable[..., Any] = AppServerClient,
) -> HookTrustSnapshot:
    """Run the production gate in a bounded read-only App Server process."""

    log_path = Path(tempfile.gettempdir()) / f"codex-autopilot-hook-preflight-{os.getpid()}.jsonl"
    client = client_factory(cfg.desktop.binary, log_path)
    try:
        client.connect()
        return require_trusted_stop_hook(
            client,
            cfg.root,
            plugin_id=autopilot_plugin_id(cfg.skill_name),
        )
    except HookPreflightError:
        raise
    except Exception as exc:
        raise HookPreflightError(
            f"Autopilot Stop hook preflight failed through hooks/list: {exc}"
        ) from exc
    finally:
        client.close()
        log_path.unlink(missing_ok=True)


def _same_path(raw: Any, expected: Path) -> bool:
    if not isinstance(raw, str) or not raw:
        return False
    try:
        return Path(raw).expanduser().resolve() == expected
    except (OSError, ValueError):
        return False
