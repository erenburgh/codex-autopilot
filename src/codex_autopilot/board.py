"""The branch board: every task of the run on one line, in the run's language.

What she wanted from the Desktop sidebar - a branch per task, visible from
the start - is shown as data instead of as threads made in advance (those
are measured not to survive, see ``staffing``). One line per task: its id
and title, its department, lead and rubric version from the roster, its
state in plain words, the first line of its last report and its threads,
current and past. One summary line on top: how many are accepted, working,
waiting, stopped and waiting for her, and what staffing found (the roster,
isolation, the project's roots).

The states, in the project's language (``config.language``):

- working, with the current thread's name and id;
- waiting for its unmet dependencies, by id;
- waiting for acceptance / under acceptance by its Lead Role;
- revision N of M, hire K, effort X;
- stopped: the reason, and the ticket the on-call holds;
- waiting for her: the decision the on-call handed up, its recommendation
  and the ready ``codex-autopilot unblock`` command that is her answer;
- accepted.

``codex-autopilot status`` and the status card print it; the runtime also
writes it to ``.codex-autopilot/BOARD.md`` at every save of run state. The
file is a view, not a journal: only the runtime writes it, nothing reads it
back, and a failure to write it never stops a run - housekeeping may never
stop a run.
"""

from __future__ import annotations

from pathlib import Path
import os
import tempfile
from typing import Any, Mapping, Sequence

BOARD_FILE = "BOARD.md"
MAX_REPORT_CHARS = 160
MAX_THREADS_SHOWN = 4
_OPEN_PHASES_EXCLUDED = frozenset({"RECOVERED", "RESOLVED"})
_LIVE_SESSION_STATUSES = frozenset(
    {"RESERVED", "CREATE_REQUESTED", "RELAYING", "CREATED", "PREPARING", "PREPARED",
     "SEND_RELAYING", "ACTIVE", "AMBIGUOUS"}
)

CATEGORIES = ("accepted", "working", "waiting", "stopped", "yours")

WORDS: dict[str, dict[str, str]] = {
    "en": {
        "title": "Branch board",
        "total": "Total",
        "accepted": "accepted",
        "working": "working",
        "waiting": "waiting",
        "stopped": "stopped",
        "yours": "waits for you",
        "cancelled_count": "cancelled",
        "s_accepted": "accepted",
        "s_cancelled": "cancelled",
        "s_working": "working: {thread}",
        "s_thread_pending": "its thread is being started",
        "s_waits_for": "waiting for {ids}",
        "s_ready": "ready to start",
        "s_retry": "waiting to retry",
        "s_acceptance": "waiting for acceptance",
        "s_on_acceptance": "under acceptance by {lead}",
        "s_revision": "revision {n} of {m}, hire {k}, effort {x}",
        "s_stopped_ticket": "stopped: {reason} — ticket {ticket} with the on-call",
        "s_stopped": "stopped: {reason}",
        "s_yours": "waits for you: {decision} — recommendation: {recommendation} — answer: {answer}",
        "no_reason": "no reason recorded",
        "roster_stop": "the run's roster did not assemble ({count} violations)",
        "report": "report",
        "threads": "threads",
        "rubric": "rubric",
        "no_lead": "no lead",
        "staffing_ok": "staffing: roster complete",
        "staffing_bad": "staffing: roster incomplete, {count} violations",
        "staffing_none": "staffing: no roster yet",
        "staffing_stale": "staffing: roster of an older plan",
        "isolation": "isolation: {outcome}",
        "isolation_PASS": "proven",
        "isolation_ROOT_WRITABLE": "the root is writable",
        "isolation_NOT_PROVEN": "not proven",
        "isolation_NOT_MEASURED": "not measured",
        "roots": "roots: {count} findings",
        "task": "Task",
        "department": "Department · lead · rubric",
        "state": "State",
        "last_report": "Last report",
        "footer": "A view, not a journal: written by the runtime at every save of run state.",
    },
    "ru": {
        "title": "Доска веток",
        "total": "Итого",
        "accepted": "принято",
        "working": "в работе",
        "waiting": "ждёт",
        "stopped": "стоит",
        "yours": "ждёт тебя",
        "cancelled_count": "отменено",
        "s_accepted": "принята",
        "s_cancelled": "отменена",
        "s_working": "работает: {thread}",
        "s_thread_pending": "тред создаётся",
        "s_waits_for": "ждёт {ids}",
        "s_ready": "готова к запуску",
        "s_retry": "ждёт повтора",
        "s_acceptance": "ждёт приёмки",
        "s_on_acceptance": "на приёмке у {lead}",
        "s_revision": "ревизия {n} из {m}, наём {k}, effort {x}",
        "s_stopped_ticket": "стоит: {reason} — билет {ticket} у дежурного",
        "s_stopped": "стоит: {reason}",
        "s_yours": "ждёт тебя: {decision} — рекомендация: {recommendation} — ответ: {answer}",
        "no_reason": "причина не записана",
        "roster_stop": "штат прогона не собран ({count} нарушений)",
        "report": "отчёт",
        "threads": "треды",
        "rubric": "рубрика",
        "no_lead": "лида нет",
        "staffing_ok": "штат собран",
        "staffing_bad": "штат не собран: {count} нарушений",
        "staffing_none": "штат ещё не собран",
        "staffing_stale": "штат собран для старого плана",
        "isolation": "изоляция: {outcome}",
        "isolation_PASS": "доказана",
        "isolation_ROOT_WRITABLE": "корень доступен на запись",
        "isolation_NOT_PROVEN": "не доказана",
        "isolation_NOT_MEASURED": "не измерена",
        "roots": "корни: {count} находок",
        "task": "Задача",
        "department": "Отдел · лид · рубрика",
        "state": "Состояние",
        "last_report": "Последний отчёт",
        "footer": "Отображение, не журнал: пишет только рантайм при каждом сохранении состояния.",
    },
}


def words_for(language: str) -> dict[str, str]:
    from .language import is_russian

    try:
        return WORDS["ru" if is_russian(language) else "en"]
    except ValueError:
        return WORDS["en"]


def _clip(text: Any, limit: int) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _open_incidents(cfg: Any) -> list[dict[str, Any]]:
    from .pipeline_engineer import PipelineIncidentStore

    return [
        item for item in PipelineIncidentStore(cfg.state_dir).load().get("incidents") or ()
        if not item.get("resolved_at") and str(item.get("phase")) not in _OPEN_PHASES_EXCLUDED
    ]


def _ticket_of(task_id: str, incidents: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """The open ticket holding a task; one handed to her comes first."""

    holding = [item for item in incidents if task_id in [str(x) for x in item.get("affected_task_ids") or ()]]
    for item in reversed(holding):
        if str(item.get("phase")) == "ESCALATE_TO_USER":
            return item
    return holding[-1] if holding else None


def _stop_reason(incident: Mapping[str, Any], words: Mapping[str, str]) -> str:
    system = incident.get("system_state") or {}
    if system.get("stop_kind") == "staffing":
        return words["roster_stop"].format(count=len(system.get("roster_issues") or ()) or "?")
    summary = str(incident.get("summary") or "")
    # The door appends its own sentence about the on-call to every summary;
    # the board says that itself.
    head = summary.split(" The on-call looks first", 1)[0]
    return _clip(head or incident.get("code") or words["no_reason"], 140)


def _sessions(state: Any, task_id: str) -> list[Mapping[str, Any]]:
    return [
        item for item in getattr(state, "worker_sessions", None) or ()
        if str(item.get("task_id") or "") == task_id and item.get("kind") != "pipeline_engineer"
    ]


def _current(sessions: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    live = [item for item in sessions if item.get("status") in _LIVE_SESSION_STATUSES]
    return live[-1] if live else None


def _thread_label(session: Mapping[str, Any] | None, words: Mapping[str, str]) -> str:
    if not session:
        return words["s_thread_pending"]
    descriptor = session.get("descriptor") if isinstance(session.get("descriptor"), Mapping) else {}
    title = str((descriptor or {}).get("title") or "")
    thread = str(session.get("thread_id") or "")
    if title and thread:
        return f"{title} ({thread})"
    return title or thread or words["s_thread_pending"]


def _threads(sessions: Sequence[Mapping[str, Any]]) -> str:
    shown = [
        f"{item.get('kind') or '?'} {item.get('status') or '?'} {item.get('thread_id') or '—'}"
        for item in sessions[-MAX_THREADS_SHOWN:]
    ]
    earlier = len(sessions) - len(shown)
    return "; ".join(shown) + (f"; +{earlier}" if earlier > 0 else "") if shown else "—"


def _report(cfg: Any, task_id: str, sessions: Sequence[Mapping[str, Any]]) -> str:
    """The first line of substance of the task's handoff (its per-task checkpoint)."""

    from .config import STATE_DIR_NAME
    from .lifecycle_base import task_checkpoint_path

    state_dir = Path(cfg.state_dir)
    for item in reversed(sessions):
        descriptor = item.get("descriptor") if isinstance(item.get("descriptor"), Mapping) else None
        cwd = str((descriptor or {}).get("cwd") or "")
        if cwd and Path(cwd).resolve(strict=False) != Path(cfg.root).resolve(strict=False):
            state_dir = Path(cwd) / STATE_DIR_NAME
        break
    candidates = [task_checkpoint_path(state_dir, task_id), task_checkpoint_path(Path(cfg.state_dir), task_id)]
    for path in dict.fromkeys(candidates):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            text = line.strip()
            if not text or text.startswith(("#", "---", "```", "<!--")):
                continue
            return _clip(text.lstrip("-*> ").strip(), MAX_REPORT_CHARS)
    return "—"


def _roster_view(plan: Any, roster: Mapping[str, Any] | None) -> tuple[dict[str, Mapping[str, Any]], str]:
    """The roster's tasks by id, when it is the current plan's, and its standing."""

    from .plan_verification import plan_sha256

    if not roster:
        return {}, "none"
    if roster.get("plan_sha256") != plan_sha256(plan):
        return {}, "stale"
    tasks = {str(item.get("id")): item for item in roster.get("tasks") or () if isinstance(item, Mapping)}
    return tasks, "ok" if roster.get("complete") else "bad"


def _department(plan: Any, state: Any, task: Any, entry: Mapping[str, Any] | None) -> tuple[str, str, str]:
    """(department, lead name, rubric version) - the roster's, else derived now."""

    if entry is not None:
        department = (entry.get("department") or {}).get("name") or ""
        lead = (entry.get("lead") or {}).get("name") or ""
        rubric = entry.get("rubric") or {}
        return department, lead, f"v{rubric['version']}" if rubric.get("version") else ""
    from .department_runtime import derive_task_department, settled_task_ids

    try:
        derived = derive_task_department(plan, task, settled=settled_task_ids(state.task_states))
    except Exception:  # noqa: BLE001 - shown as "no lead"; the gate stops it
        return "", "", ""
    return derived.name, plan.role_map[derived.lead_role_id].name, ""


def board_rows(
    cfg: Any, plan: Any, state: Any, *, incidents: Sequence[Mapping[str, Any]] | None = None,
    roster: Mapping[str, Any] | None = None,
) -> list[dict[str, str]]:
    from .lifecycle_base import task_effort
    from .stop_diagnosis import owner_answer
    from .task_state import unmet_dependencies

    words = words_for(getattr(cfg, "language", "en"))
    incidents = list(_open_incidents(cfg) if incidents is None else incidents)
    entries, _standing = _roster_view(plan, roster)
    rows: list[dict[str, str]] = []
    for task in plan.tasks:
        value = str(state.task_states.get(task.id) or "")
        sessions = _sessions(state, task.id)
        current = _current(sessions)
        department, lead, rubric = _department(plan, state, task, entries.get(task.id))
        ticket = _ticket_of(task.id, incidents)
        revisions = int((state.task_revisions or {}).get(task.id, 0))
        revision = dict(
            m=task.verification.max_revision_attempts,
            k=int((getattr(state, "task_rehires", None) or {}).get(task.id, 0)) + 1,
            x=task_effort(plan, state, task.id),
        )
        if ticket is not None and str(ticket.get("phase")) == "ESCALATE_TO_USER":
            escalation = ticket.get("escalation") or {}
            category = "yours"
            phrase = words["s_yours"].format(
                decision=_clip(escalation.get("decision_needed") or ticket.get("escalation_detail")
                               or _stop_reason(ticket, words), 200),
                recommendation=_clip(escalation.get("recommendation") or "—", 200),
                answer=owner_answer(cfg, ticket),
            )
        elif ticket is not None:
            category = "stopped"
            phrase = words["s_stopped_ticket"].format(reason=_stop_reason(ticket, words), ticket=ticket.get("incident_id"))
        elif value in {"BLOCKED", "FAILED"}:
            category = "stopped"
            phrase = words["s_stopped"].format(reason=_clip(state.last_error or words["no_reason"], 140))
        elif value == "VERIFIED":
            category, phrase = "accepted", words["s_accepted"]
        elif value == "CANCELLED":
            category, phrase = "cancelled", words["s_cancelled"]
        elif value == "RUNNING":
            category, phrase = "working", words["s_working"].format(thread=_thread_label(current, words))
        elif value == "REVISING":
            category = "working"
            phrase = (words["s_revision"].format(n=max(1, revisions), **revision) + " — "
                      + words["s_working"].format(thread=_thread_label(current, words)))
        elif value == "VERIFYING":
            category = "working"
            phrase = words["s_on_acceptance"].format(lead=lead or words["no_lead"])
            if current is not None:
                phrase += f" — {_thread_label(current, words)}"
        elif value == "IMPLEMENTED":
            category, phrase = "waiting", words["s_acceptance"]
        elif value == "REVISION_REQUIRED":
            category, phrase = "waiting", words["s_revision"].format(n=revisions + 1, **revision)
        elif value == "RETRY_WAIT":
            category, phrase = "waiting", words["s_retry"]
        else:
            unmet = unmet_dependencies(plan, task.id, state.task_states)
            category = "waiting"
            phrase = words["s_waits_for"].format(ids=", ".join(unmet)) if unmet else words["s_ready"]
        rows.append({
            "id": task.id,
            "title": task.title,
            "department": department,
            "lead": lead,
            "rubric": rubric,
            "category": category,
            "state": phrase,
            "report": _report(cfg, task.id, sessions),
            "threads": _threads(sessions),
        })
    return rows


def board_summary(rows: Sequence[Mapping[str, str]], roster: Mapping[str, Any] | None, plan: Any, language: str) -> str:
    words = words_for(language)
    counts = {key: sum(1 for row in rows if row["category"] == key) for key in (*CATEGORIES, "cancelled")}
    parts = [f"{words[key]} {counts[key]}" for key in CATEGORIES]
    if counts["cancelled"]:
        parts.append(f"{words['cancelled_count']} {counts['cancelled']}")
    _entries, standing = _roster_view(plan, roster)
    findings = [
        words["staffing_ok"] if standing == "ok"
        else words["staffing_bad"].format(count=len((roster or {}).get("issues") or ())) if standing == "bad"
        else words["staffing_stale"] if standing == "stale" else words["staffing_none"]
    ]
    run = (roster or {}).get("run") or {}
    isolation = run.get("isolation") or {}
    if isolation:
        outcome = str(isolation.get("outcome") or "NOT_MEASURED")
        findings.append(words["isolation"].format(outcome=words.get(f"isolation_{outcome}", outcome)))
    roots = (run.get("roots_audit") or {}).get("findings") or ()
    if roots:
        findings.append(words["roots"].format(count=len(roots)))
    return f"{words['total']}: " + " · ".join(parts) + " — " + "; ".join(findings)


def _row_head(row: Mapping[str, str], words: Mapping[str, str]) -> str:
    lead = row["lead"] or words["no_lead"]
    who = f"{row['department']} · {lead}" if row["department"] and row["department"] != row["lead"] else lead
    rubric = f", {words['rubric']} {row['rubric']}" if row["rubric"] else ""
    return f"{row['id']} {row['title']} — {who}{rubric}"


def render_board(cfg: Any, plan: Any, state: Any, *, detailed: bool = True) -> list[str]:
    """The board as text lines: the summary, then one line per task."""

    from .staffing import load_roster

    language = getattr(cfg, "language", "en")
    words = words_for(language)
    roster = load_roster(cfg.state_dir)
    rows = board_rows(cfg, plan, state, roster=roster)
    lines = [f"{words['title']} — {board_summary(rows, roster, plan, language)}"]
    for row in rows:
        line = f"- {_row_head(row, words)} — {row['state']}"
        if detailed:
            line += f" — {words['report']}: {row['report']} — {words['threads']}: {row['threads']}"
        lines.append(line)
    return lines


def _cell(text: Any) -> str:
    return str(text or "—").replace("|", "\\|").replace("\n", " ")


def render_board_markdown(cfg: Any, plan: Any, state: Any) -> str:
    from .staffing import load_roster

    language = getattr(cfg, "language", "en")
    words = words_for(language)
    roster = load_roster(cfg.state_dir)
    rows = board_rows(cfg, plan, state, roster=roster)
    out = [
        f"# {words['title']}",
        "",
        board_summary(rows, roster, plan, language),
        "",
        f"| {words['task']} | {words['department']} | {words['state']} | {words['last_report']} | {words['threads']} |",
        "|---|---|---|---|---|",
    ]
    for row in rows:
        lead = row["lead"] or words["no_lead"]
        who = f"{row['department']} · {lead}" if row["department"] and row["department"] != row["lead"] else lead
        if row["rubric"]:
            who += f" · {row['rubric']}"
        out.append(
            f"| {_cell(row['id'] + ' ' + row['title'])} | {_cell(who)} | {_cell(row['state'])} "
            f"| {_cell(row['report'])} | {_cell(row['threads'])} |"
        )
    out += ["", f"_{words['footer']}_", ""]
    return "\n".join(out)


# The config and the plan change rarely and the state is saved often: both
# are read again only when their file changed (mtime, size).
_CACHE: dict[tuple[str, str], tuple[tuple[int, int], Any]] = {}


def _cached(kind: str, path: Path, load: Any) -> Any:
    stat = path.stat()
    key = (kind, str(path))
    stamp = (stat.st_mtime_ns, stat.st_size)
    hit = _CACHE.get(key)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    value = load()
    _CACHE[key] = (stamp, value)
    return value


def refresh_board_file(state_dir: Path, state: Any) -> bool:
    """Write BOARD.md from the state being saved. Never raises: a view may not stop a run."""

    try:
        from .config import CONFIG_NAME, load_config
        from .plan import PLAN_FILE, load_plan

        state_dir = Path(state_dir)
        config = state_dir / CONFIG_NAME
        plan_file = state_dir / PLAN_FILE
        if not config.is_file() or not plan_file.is_file():
            return False
        cfg = _cached("config", config, lambda: load_config(state_dir.parent))
        plan = _cached(f"plan:{cfg.profile}", plan_file, lambda: load_plan(state_dir, cfg.profile))
        text = render_board_markdown(cfg, plan, state)
        path = state_dir / BOARD_FILE
        try:
            if path.read_text(encoding="utf-8") == text:
                return True
        except OSError:
            pass
        _write(path, text)
        return True
    except Exception:  # noqa: BLE001 - housekeeping may never stop a run
        return False


def _write(path: Path, text: str) -> None:
    fd, raw = tempfile.mkstemp(prefix=".board-", dir=path.parent)
    temp = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
