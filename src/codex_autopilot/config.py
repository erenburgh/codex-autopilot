from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib

from .language import DEFAULT_LANGUAGE, normalize_language
from .plan import (
    DEFAULT_COMPUTER_USE_SLOTS,
    COMPAT_EXECUTION_STRATEGY,
    COMPAT_MAX_PARALLEL_WORKERS,
    EXECUTION_STRATEGIES,
)


CONFIG_NAME = "config.toml"
STATE_DIR_NAME = ".codex-autopilot"
PROFILES = {"adaptive", "host-settings"}
# Единственная поверхность воркера. Вторая, headless_app_server, вела в
# orchestrator.py, который не мог выполниться в продукте: run, resume и
# _dispatch отказывали при desktop_owned, а умолчание везде было
# desktop_owned. В 0.8.1 и путь, и поверхность сняты.
DESKTOP_OWNED_SURFACE = "desktop_owned"
WORKER_SURFACES = {DESKTOP_OWNED_SURFACE}


@dataclass(frozen=True, slots=True)
class DesktopConfig:
    binary: str = "codex"
    permission_profile: str = ":workspace"
    # App Server project ids and Desktop sidebar project ids are different
    # namespaces. project_id is App Server-owned metadata only.
    project_id: str | None = None
    desktop_project_id: str | None = None
    title_prefix: str = "Codex Autopilot"
    turn_timeout_seconds: int = 14_400
    reconcile_timeout_seconds: int = 300


@dataclass(frozen=True, slots=True)
class RetryConfig:
    initial_seconds: int = 30
    maximum_seconds: int = 900
    maximum_attempts: int = 96


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    """Operational scheduler limits; absent v0.8 sections load fail-closed."""

    execution_strategy: str = COMPAT_EXECUTION_STRATEGY
    max_parallel_workers: int = COMPAT_MAX_PARALLEL_WORKERS
    # Названо ли число человеком в этом файле. Умолчание здесь - единица
    # ради совместимости с v0.8, и принять её за выбор пользователя
    # значило бы загнать любой прогон со старым конфигом в один поток.
    max_parallel_workers_declared: bool = False
    computer_use_slots: int = DEFAULT_COMPUTER_USE_SLOTS
    worker_surface: str = DESKTOP_OWNED_SURFACE
    # До какого размещения ветки в Desktop задача не вправе начинать работу.
    # "in_project" - только внутри проекта; "visible" - достаточно того, что
    # Desktop о ней знает; "any" - не проверять. Невидимая задача обесценивает
    # автопилот: её нельзя открыть и прочитать, поэтому по умолчанию строго.
    required_thread_placement: str = "in_project"
    # Системный банер, когда задача проверена, встала или прогон завершён.
    # Выключено по умолчанию: это побочный эффект на машине человека.
    # Замерено, что другого пути нет - App Server не объявляет ни одного
    # API про непрочитанное, а thread/metadata/update принимает только
    # projectId. См. notify.py.
    desktop_notifications: bool = False


@dataclass(frozen=True, slots=True)
class Config:
    root: Path
    state_dir: Path
    roadmap: Path
    profile: str
    language: str
    skill_name: str
    skill_path: Path
    desktop: DesktopConfig
    retry: RetryConfig
    runtime: RuntimeConfig = RuntimeConfig()
    auto_commit: bool = False

    @property
    def adaptive(self) -> bool:
        return self.profile == "adaptive"


def config_path(root: Path) -> Path:
    return root.resolve() / STATE_DIR_NAME / CONFIG_NAME


def _install_root() -> Path:
    """Корень установки по собственному расположению рантайма.

    Рантайм лежит в <install_root>/current/runtime/src/codex_autopilot.
    absolute(), а не resolve(): разрешение развернуло бы симлинк
    `current` в каталог с номером версии - тот самый, который следующая
    установка удалит.
    """

    return Path(__file__).absolute().parents[3]


def stable_skill_path(recorded: Path) -> Path | None:
    """Тот же скилл по пути, который переживает установку.

    Установщик кладёт плагин в каталог с версией и меткой времени и
    удаляет прежний, а прогон хранил путь целиком - вместе с версией.
    Первая же установка оставляла ссылку в пустоте: живой прогон умирал
    на `could not resolve installed plugin root from skill`, и отказ
    попадал в класс AMBIGUOUS_SIDE_EFFECT, из которого нет выхода.

    Раскладки двух хранилищ различаются - в кэше это
    <плагин>/<версия>/skills/..., в установке plugins/<плагин>/skills/...
    - поэтому имя плагина не вычисляется по позиции, а ищется.
    """

    parts = Path(recorded).parts
    if "skills" not in parts:
        return None
    index = len(parts) - 1 - parts[::-1].index("skills")
    tail = parts[index:]
    pattern = str(Path("plugins", "*", *tail))
    for candidate in sorted(_install_root().glob(pattern)):
        if candidate.is_file():
            return candidate
    return None


def durable_skill_path(skill_path: Path) -> Path:
    """Путь, который записывается в конфиг прогона."""

    stable = stable_skill_path(Path(skill_path))
    return stable if stable is not None else Path(skill_path).resolve()


def resolve_skill_path(raw: str) -> Path:
    """Записанный путь, а если он исчез - стабильный эквивалент."""

    recorded = Path(str(raw)).expanduser()
    if recorded.is_file():
        return recorded.resolve()
    stable = stable_skill_path(recorded)
    if stable is not None:
        return stable
    # Ни того, ни другого: пусть отказ случится там же и с тем же текстом,
    # что и прежде, а не превратится в загадку на уровне конфигурации.
    return recorded.resolve()


def load_config(root_or_path: Path) -> Config:
    candidate = root_or_path.expanduser().resolve()
    path = candidate if candidate.name == CONFIG_NAME and candidate.is_file() else config_path(candidate)
    if not path.is_file():
        raise FileNotFoundError(f"Codex Autopilot is not initialized: {path}")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    project = data.get("project") or {}
    desktop = data.get("desktop") or {}
    retry = data.get("retry") or {}
    runtime = data.get("runtime") or {}
    root = Path(str(project.get("root", path.parent.parent))).expanduser().resolve()
    if root != path.parent.parent.resolve():
        raise ValueError("config project.root does not match the initialized project directory")
    state_dir = root / STATE_DIR_NAME
    profile = str(data.get("profile", "adaptive"))
    if profile not in PROFILES:
        raise ValueError(f"profile must be one of {sorted(PROFILES)}")
    permission = str(desktop.get("permission_profile", ":workspace"))
    if permission != ":workspace":
        raise ValueError("v0.8 public beta only supports the :workspace permission profile")
    skill_name = "codex-autopilot-adaptive" if profile == "adaptive" else "codex-autopilot-host-settings"
    skill_path_raw = desktop.get("skill_path")
    if not skill_path_raw:
        raise ValueError("desktop.skill_path is required")
    auto_commit = bool((data.get("git") or {}).get("auto_commit", False))
    return Config(
        root=root,
        state_dir=state_dir,
        roadmap=root / "ROADMAP.md",
        profile=profile,
        language=normalize_language(data.get("language", DEFAULT_LANGUAGE)),
        skill_name=skill_name,
        skill_path=resolve_skill_path(str(skill_path_raw)),
        desktop=DesktopConfig(
            binary=str(desktop.get("binary", "codex")),
            permission_profile=permission,
            project_id=_optional_string(desktop.get("project_id")),
            desktop_project_id=_optional_string(desktop.get("desktop_project_id")),
            title_prefix=str(desktop.get("title_prefix", "Codex Autopilot")),
            turn_timeout_seconds=_positive_int(desktop.get("turn_timeout_seconds", 14_400), "turn_timeout_seconds"),
            reconcile_timeout_seconds=_positive_int(desktop.get("reconcile_timeout_seconds", 300), "reconcile_timeout_seconds"),
        ),
        retry=RetryConfig(
            initial_seconds=_positive_int(retry.get("initial_seconds", 30), "retry.initial_seconds"),
            maximum_seconds=_positive_int(retry.get("maximum_seconds", 900), "retry.maximum_seconds"),
            maximum_attempts=_positive_int(retry.get("maximum_attempts", 96), "retry.maximum_attempts"),
        ),
        runtime=RuntimeConfig(
            execution_strategy=_execution_strategy(
                runtime.get("execution_strategy", COMPAT_EXECUTION_STRATEGY)
            ),
            max_parallel_workers=_positive_int(
                runtime.get("max_parallel_workers", COMPAT_MAX_PARALLEL_WORKERS),
                "runtime.max_parallel_workers",
            ),
            max_parallel_workers_declared="max_parallel_workers" in runtime,
            computer_use_slots=_positive_int(
                runtime.get("computer_use_slots", DEFAULT_COMPUTER_USE_SLOTS),
                "runtime.computer_use_slots",
            ),
            worker_surface=_worker_surface(
                runtime.get("worker_surface", DESKTOP_OWNED_SURFACE)
            ),
            required_thread_placement=_thread_placement(
                runtime.get("required_thread_placement", "in_project")
            ),
            desktop_notifications=_bool(
                runtime.get("desktop_notifications", False),
                "runtime.desktop_notifications",
            ),
        ),
        auto_commit=auto_commit,
    )


def _optional_string(value: object) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _positive_int(value: object, name: str) -> int:
    result = int(value)
    if result <= 0:
        raise ValueError(f"{name} must be positive")
    return result


def _execution_strategy(value: object) -> str:
    result = str(value).strip()
    if result not in EXECUTION_STRATEGIES:
        raise ValueError(
            f"runtime.execution_strategy must be one of {sorted(EXECUTION_STRATEGIES)}"
        )
    return result


THREAD_PLACEMENT_LEVELS = ("in_project", "visible", "any")


def _bool(value: object, name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError(f"{name} must be true or false")


def _thread_placement(value: object) -> str:
    text = str(value or "").strip().lower()
    if text not in THREAD_PLACEMENT_LEVELS:
        raise ValueError(
            "runtime.required_thread_placement must be one of "
            + ", ".join(THREAD_PLACEMENT_LEVELS)
        )
    return text


def _worker_surface(value: object) -> str:
    result = str(value).strip()
    if result not in WORKER_SURFACES:
        raise ValueError(
            f"runtime.worker_surface must be one of {sorted(WORKER_SURFACES)}"
        )
    return result
