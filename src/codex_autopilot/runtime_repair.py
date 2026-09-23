"""Bounded repair of the runtime by the runtime itself.

A task stops moving not only because of the project. It stops because of
the runtime itself: a thread went into the wrong phase, a worker's reply
did not parse, the dispatcher crashed on its own error. Until now a human
repaired that - so for a user without such a human the run simply stood.

Here the on-call engineer gets the right to edit the runtime's code, but
not the right to declare a repair. An edit passes a gateway: the
reproduction test must FAIL on the current code and PASS on the fixed
one, the whole test suite must stay green, and the guarded functions must
stay byte-identical. If any of that is not so, the live installation is
not touched at all.

A repair is a SET of edits, not one. That is how real repairs are shaped:
separating a model error from a machine fault on the v1.0 run touched
three modules at once, and they cannot be applied one at a time - after
the first the suite is red, and the gateway would rightly refuse. For the
same reason a new module may be added: removing the v0.8 format required
moving code into a separate file because the old one hit the size limit.

How a proven set reaches the runtime changed. It used to be written from
the engineer's own thread straight into the installed tree - outside the
project, beyond the thread's ``:workspace`` sandbox (a permission request
nobody answers), and file by file under every live dispatcher, which imports
modules lazily. Now ``prove_runtime_patch`` proves the set in a copy inside
the project and returns it; ``runtime_install`` stages it there, drains the
run, and installs it with ``install_proven_patch`` into a fresh copy of the
current version before switching ``current`` with one rename - only when no
dispatcher is alive.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence


class RuntimeRepairError(Exception):
    """The edit did not pass the gateway and never reached the live installation."""


# Modules the engineer never edits. Authority and trust are not changed by
# the one who uses them, and the gateway does not rewrite itself -
# otherwise the very first edit removes every other check.
UNPATCHABLE_MODULES = frozenset(
    {
        "engineer_authority.py",
        "runtime_repair.py",
        "hook_trust.py",
    }
)

# Functions whose text must survive any edit unchanged. These are the very
# guards that refused for good reason: thread ownership, reservation
# ownership, surface membership. A patch touching any of them is refused
# whole - even when the tests are green.
GUARDED_DEFINITIONS: tuple[tuple[str, str], ...] = (
    ("lifecycle_base.py", "_require_desktop_owned"),
    ("lifecycle_base.py", "_require_relay_executor"),
    ("lifecycle_base.py", "_dispatcher_owns_reservation"),
    ("cli.py", "_relay_executor_thread_id"),
    # The engineer may repair the incident bookkeeping - that is where real
    # defects happen; we fixed one by hand on the v1.0 run. But not what its
    # own work is measured by: the fault class, the ticket identity, the
    # action vocabulary and the mandatory healthcheck stay as they are.
    ("pipeline_engineer.py", "classify_incident"),
    ("pipeline_engineer.py", "incident_signature"),
    ("pipeline_engineer.py", "escalate_to_user"),
    ("pipeline_engineer.py", "_require_named_actions"),
    ("pipeline_engineer.py", "_require_passing_healthcheck"),
    # The on-call's own limits over a stopped task. They live in patchable
    # modules (the table itself is in engineer_authority), so a repair
    # could otherwise rewrite the very checks that bound it - and a green
    # suite would not stop it if the patch changed the test too: who may
    # return a task and when, when a stop ticket may close, which patch
    # buys a fresh hire, what an escalation carries, where advisory tickets
    # go, and her own answer.
    ("engineer_stop_actions.py", "require_engineer_thread"),
    ("engineer_stop_actions.py", "return_stopped_task"),
    ("engineer_stop_actions.py", "require_stop_ticket_closable"),
    ("engineer_stop_actions.py", "acceptance_patches"),
    # Which patch changed the acceptance path, whether it still stands, and
    # taking back what it bought: the fresh hire past her ladder rests on
    # these, and so does the binding of the patch commands themselves.
    ("engineer_stop_actions.py", "require_patch_holder"),
    ("engineer_stop_actions.py", "take_back_patch"),
    ("engineer_stop_actions.py", "_consumed_patch_ids"),
    ("ladder_grants.py", "acceptance_path_changes"),
    ("ladder_grants.py", "_entry"),
    ("ladder_grants.py", "_shape"),
    ("ladder_grants.py", "_tests_phase"),
    ("ladder_grants.py", "patch_is_live"),
    ("ladder_grants.py", "revoke_grants"),
    ("runtime_install.py", "patch_status"),
    ("revision_budget.py", "at_top_of_ladder"),
    ("stop_diagnosis.py", "means_for"),
    ("engineer_escalation.py", "parse_engineer_escalation"),
    ("revision_budget.py", "premises_changed"),
    ("revision_budget.py", "grant_fresh_hire"),
    ("pipeline_engineer.py", "route_incident"),
    ("pipeline_engineer.py", "complete_pipeline_engineer"),
    ("owner_answers.py", "answer_task"),
    ("run_arming.py", "arm_run"),
    # R4: which request the run's durable authorization covers, how it is
    # recorded, and the refusal to send a covered one to her. The list
    # itself is in engineer_authority; these read it, so a repair could
    # otherwise narrow "covered" to nothing and escalate at will.
    ("run_authorization.py", "authorization_record"),
    ("run_authorization.py", "ensure_recorded"),
    ("run_authorization.py", "covering_operation"),
    ("approval_stops.py", "record_approval_required"),
    ("engineer_escalation.py", "read_engineer_outcome"),
)

TEST_TIMEOUT_SECONDS = 900


@dataclass(frozen=True, slots=True)
class RuntimeTree:
    """Where the repairable runtime and its proofs live."""

    src: Path
    tests: Path

    @property
    def package(self) -> Path:
        return self.src / "codex_autopilot"

    @property
    def root(self) -> Path:
        return self.src.parent


@dataclass(frozen=True, slots=True)
class Edit:
    """One change within a set.

    ``old`` is the exact fragment being replaced, and it must occur in the
    module exactly once. ``old`` equal to None means a new module: then
    ``new`` is its whole content, and the module must not exist. An
    existing file cannot be overwritten whole: an edit names a place, it
    does not swap a file.
    """

    module: str
    new: str
    old: str | None = None


@dataclass(frozen=True, slots=True)
class ModuleChange:
    module: str
    sha256_before: str | None
    sha256_after: str

    def to_dict(self) -> dict[str, str | None]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PatchRecord:
    patch_id: str
    changes: tuple[ModuleChange, ...]
    test_name: str
    at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "patch_id": self.patch_id,
            "test_name": self.test_name,
            "at": self.at,
            "changes": [item.to_dict() for item in self.changes],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> "PatchRecord":
        changes = tuple(
            ModuleChange(
                module=str(item["module"]),
                sha256_before=item["sha256_before"],  # type: ignore[arg-type]
                sha256_after=str(item["sha256_after"]),
            )
            for item in raw["changes"]  # type: ignore[union-attr]
        )
        return cls(
            patch_id=str(raw["patch_id"]),
            changes=changes,
            test_name=str(raw["test_name"]),
            at=str(raw["at"]),
        )


def resolve_runtime_tree(*, module_file: str | None = None) -> RuntimeTree:
    """The runtime tree, from this module's own location.

    Works the same in an installation (<...>/runtime/src, <...>/runtime/tests)
    and in a working tree (repo/src, repo/tests): in both, the tests live
    next to the sources. Without tests no repair is possible - there would
    be nothing to prove the edit broke nothing else.
    """

    here = Path(module_file or __file__).absolute()
    src = here.parents[1]
    tests = src.parent / "tests"
    if not (src / "codex_autopilot").is_dir():
        raise RuntimeRepairError(f"runtime sources are not where expected: {src}")
    if not tests.is_dir():
        raise RuntimeRepairError(
            "this installation ships no test suite, so a repair cannot be proven; "
            f"expected {tests}"
        )
    return RuntimeTree(src=src, tests=tests)


def guard_hashes(src: Path) -> dict[str, str]:
    """Hashes of the guarded functions' text - an identity that cannot be touched.

    Three things here are not accidental; the reviewer named each:

    - the name must occur in the module exactly once. Python executes the
      LAST definition, and the first draft hashed the FIRST - a duplicate
      appended after the original passed the check;
    - the hash covers the decorators, not just the ``def`` line:
      ``get_source_segment`` excludes decorators, and a wrapper around a
      guard would have stayed invisible;
    - every definition with this name counts, nested ones included: there
      must be none at all.
    """

    hashes: dict[str, str] = {}
    for module, name in GUARDED_DEFINITIONS:
        text = (src / "codex_autopilot" / module).read_text(encoding="utf-8")
        found = _definitions(text, name)
        if not found:
            raise RuntimeRepairError(f"guarded definition disappeared: {module}:{name}")
        if len(found) > 1:
            raise RuntimeRepairError(
                f"guarded definition {module}:{name} occurs {len(found)} times; "
                "a duplicate would be the one Python actually runs"
            )
        hashes[f"{module}:{name}"] = hashlib.sha256(
            _decorated_segment(text, found[0]).encode("utf-8")
        ).hexdigest()
    return hashes


def _definitions(text: str, name: str) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        node
        for node in ast.walk(ast.parse(text))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    ]


def _decorated_segment(text: str, node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """The definition's text together with its decorators."""

    lines = text.splitlines(keepends=True)
    start = min([node.lineno, *(item.lineno for item in node.decorator_list)])
    end = node.end_lineno or node.lineno
    return "".join(lines[start - 1 : end])


# What does not go into the copy. Our own past patches need no copying,
# and the live run's state must not end up in the copy at all: the
# engineer's tests execute right there.
STAGING_EXCLUDES = frozenset(
    {".git", ".codex-autopilot", "patches", "__pycache__", ".venv", "venv", "build", "dist"}
)


def _copy_runtime(tree: RuntimeTree, staging: Path) -> None:
    """Copy the whole tree, not only the sources and tests.

    This was already got wrong here: the copy held src and tests, while a
    third of the suite also needs plugins - 94 tests failed in the copy, and
    the gateway would have refused ANY edit, a correct one included. The
    check must run on the same tree the ordinary test run does.
    """

    shutil.copytree(
        tree.root,
        staging,
        ignore=shutil.ignore_patterns(*STAGING_EXCLUDES),
        symlinks=True,
    )
    if not (staging / "src" / "codex_autopilot").is_dir():
        raise RuntimeRepairError(f"the staged copy has no runtime sources: {staging}")
    if not (staging / "tests").is_dir():
        raise RuntimeRepairError(f"the staged copy has no test suite: {staging}")
    # The copy must be a repository: part of the suite asks git - Project
    # Memory requires a repository, and the release check reads
    # .gitignore. Without this the gateway would refuse a correct edit for a
    # reason unrelated to it. No history is needed, only the repository.
    subprocess.run(
        [_git(), "init", "-q"],
        cwd=staging,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )



def _git() -> str:
    """Git is looked up in PATH: the runtime has no copy of its own."""

    found = shutil.which("git")
    if not found:
        raise RuntimeRepairError(
            "git is required to prove a repair: part of the suite asks the repository"
        )
    return found


@dataclass(frozen=True, slots=True)
class ProvenPatch:
    """A set of edits the gateway proved, not yet installed anywhere.

    ``sources`` is the whole text of every changed module after the set;
    ``originals`` the text before (None for a new module) - the installer
    checks the live tree still matches it before writing anything.
    """

    record: PatchRecord
    sources: dict[str, str]
    originals: dict[str, str | None]
    test_name: str
    test_source: str


def install_proven_patch(tree: RuntimeTree, proven: ProvenPatch) -> PatchRecord:
    """Write a proven set into a tree whole, with its backup and its test.

    The on-call's command used to do this from inside its thread, into the
    live installation. That tree is outside the project, beyond the
    ``:workspace`` sandbox the thread runs in, and it is shared with every
    dispatcher of every run - written file by file, non-atomically. Now the
    command only proves and stages (``runtime_install.stage_proven_patch``),
    and this is called by the installer on a fresh copy of the current
    version, which then replaces ``current`` with one rename.

    Every module must still read as it did when the set was proven: a tree
    that moved underneath is refused, never overwritten.
    """

    for change in proven.record.changes:
        target = tree.package / change.module
        now = _sha256(target.read_text(encoding="utf-8")) if target.is_file() else None
        if now != change.sha256_before:
            raise RuntimeRepairError(
                f"{change.module} changed since the patch was proven; prove it again"
            )
    _store_backup(tree, proven.record)
    for module, source in proven.sources.items():
        (tree.package / module).write_text(source, encoding="utf-8")
    (tree.tests / f"{proven.test_name}.py").write_text(proven.test_source, encoding="utf-8")
    return proven.record


def prove_runtime_patch(
    *,
    edits: Sequence[Edit],
    test_name: str,
    test_source: str,
    at: str,
    tree: RuntimeTree | None = None,
    staging_parent: Path | None = None,
) -> ProvenPatch:
    """Take a set of edits through the gateway; return it proven, write nothing live.

    ``staging_parent`` puts the gateway's copy inside a directory the
    caller can write - the project, for a thread in the ``:workspace``
    sandbox - instead of the system temp directory.
    """

    tree = tree or resolve_runtime_tree()
    if not edits:
        raise RuntimeRepairError("a repair changes at least one module")
    _validate_test_name(test_name)
    if (tree.tests / f"{test_name}.py").exists():
        raise RuntimeRepairError(
            f"{test_name}.py already exists: a repair proves itself with its own test, "
            "it does not overwrite someone else's"
        )
    for edit in edits:
        _check_module_name(edit.module)

    if staging_parent is not None:
        Path(staging_parent).mkdir(parents=True, exist_ok=True)
    staging = (
        Path(tempfile.mkdtemp(prefix="codex-autopilot-repair-", dir=staging_parent)) / "tree"
    )
    try:
        _copy_runtime(tree, staging)
        (staging / "tests" / f"{test_name}.py").write_text(test_source, encoding="utf-8")

        if _run_one_test(staging, test_name).returncode == 0:
            raise RuntimeRepairError(
                "the reproduction test passes on the current code, so it proves nothing: "
                "a repair is accepted only for a failure that can be shown first"
            )

        # The set is applied whole and only in the copy: until it is proven,
        # the live installation knows nothing of it.
        changes = _apply_edits(staging / "src" / "codex_autopilot", edits)

        after = _run_one_test(staging, test_name)
        if after.returncode != 0:
            raise RuntimeRepairError(
                "the reproduction test still fails with the patch applied:\n" + _tail(after)
            )

        suite = _run_suite(staging)
        if suite.returncode != 0:
            raise RuntimeRepairError(
                "the patch breaks the rest of the runtime:\n" + _tail(suite)
            )

        expected = guard_hashes(tree.src)
        observed = guard_hashes(staging / "src")
        touched = sorted(key for key, value in expected.items() if observed.get(key) != value)
        if touched:
            raise RuntimeRepairError(
                "the patch changes guarded definitions and is refused: " + ", ".join(touched)
            )

        record = PatchRecord(
            patch_id=_patch_id(edits, at),
            changes=changes,
            test_name=test_name,
            at=at,
        )
        return ProvenPatch(
            record=record,
            sources={
                change.module: (staging / "src" / "codex_autopilot" / change.module).read_text(
                    encoding="utf-8"
                )
                for change in changes
            },
            originals={
                change.module: (
                    None
                    if change.sha256_before is None
                    else (tree.package / change.module).read_text(encoding="utf-8")
                )
                for change in changes
            },
            test_name=test_name,
            test_source=test_source,
        )
    finally:
        shutil.rmtree(staging.parent, ignore_errors=True)


def revert_runtime_patch(patch_id: str, *, tree: RuntimeTree | None = None) -> PatchRecord:
    """Revert a set of edits whole, together with its test."""

    tree = tree or resolve_runtime_tree()
    folder = tree.root / "patches" / patch_id
    manifest = folder / "patch.json"
    if not manifest.is_file():
        raise RuntimeRepairError(f"unknown patch {patch_id}")
    record = PatchRecord.from_dict(json.loads(manifest.read_text(encoding="utf-8")))
    for change in record.changes:
        target = tree.package / change.module
        if not target.is_file():
            raise RuntimeRepairError(f"{change.module} is gone; refusing a partial revert")
        if _sha256(target.read_text(encoding="utf-8")) != change.sha256_after:
            raise RuntimeRepairError(
                f"{change.module} changed after this patch was applied; reverting would "
                "silently discard that later change"
            )
    for change in record.changes:
        target = tree.package / change.module
        if change.sha256_before is None:
            target.unlink()
            continue
        target.write_text(
            (folder / f"{change.module}.orig").read_text(encoding="utf-8"), encoding="utf-8"
        )
    (tree.tests / f"{record.test_name}.py").unlink(missing_ok=True)
    return record


def _apply_edits(package: Path, edits: Sequence[Edit]) -> tuple[ModuleChange, ...]:
    """Apply the set in the copy and return what became of what."""

    originals: dict[str, str | None] = {}
    for edit in edits:
        target = package / edit.module
        if edit.module not in originals:
            originals[edit.module] = (
                target.read_text(encoding="utf-8") if target.is_file() else None
            )
        if edit.old is None:
            if originals[edit.module] is not None or target.is_file():
                raise RuntimeRepairError(
                    f"{edit.module} already exists: name the fragment to replace instead of "
                    "handing over a whole file"
                )
            text = edit.new
        else:
            if not target.is_file():
                raise RuntimeRepairError(f"no such runtime module: {edit.module}")
            text = _replace_once(
                target.read_text(encoding="utf-8"), edit.old, edit.new, module=edit.module
            )
        try:
            ast.parse(text)
        except SyntaxError as exc:
            raise RuntimeRepairError(
                f"the patched module {edit.module} does not parse: {exc}"
            ) from exc
        target.write_text(text, encoding="utf-8")

    return tuple(
        ModuleChange(
            module=module,
            sha256_before=None if original is None else _sha256(original),
            sha256_after=_sha256((package / module).read_text(encoding="utf-8")),
        )
        for module, original in originals.items()
    )


def _check_module_name(module: str) -> None:
    if module != Path(module).name or not module.endswith(".py"):
        raise RuntimeRepairError(f"a repair names modules of the runtime, not {module!r}")
    # Runtime module names are lower case, and the comparison uses the
    # lower-case form: the installation's file system is case-insensitive,
    # and ``Hook_Trust.py`` would land on hook_trust.py, past the ban.
    if module != module.lower():
        raise RuntimeRepairError(
            f"runtime modules are named in lower case; {module!r} would land on "
            f"{module.lower()!r} on a case-insensitive file system"
        )
    if module in UNPATCHABLE_MODULES:
        raise RuntimeRepairError(
            f"{module} is out of reach for a repair: authority, trust and this gateway "
            "are not rewritten by the one who uses them"
        )


def _replace_once(text: str, old: str, new: str, *, module: str) -> str:
    if not old.strip():
        raise RuntimeRepairError("the replaced fragment must not be empty")
    if old == new:
        raise RuntimeRepairError(f"the patch changes nothing in {module}")
    found = text.count(old)
    if found == 0:
        raise RuntimeRepairError(f"the fragment does not occur in {module}")
    if found > 1:
        raise RuntimeRepairError(
            f"the fragment occurs {found} times in {module}; a repair must name one place"
        )
    return text.replace(old, new)


def _validate_test_name(test_name: str) -> None:
    if not test_name.startswith("test_") or not test_name.replace("_", "").isalnum():
        raise RuntimeRepairError(
            f"the reproduction test must be named test_<something>, not {test_name!r}"
        )


def _run_one_test(staging: Path, test_name: str) -> subprocess.CompletedProcess[str]:
    return _run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", f"{test_name}.py"],
        staging,
    )


def _run_suite(staging: Path) -> subprocess.CompletedProcess[str]:
    return _run([sys.executable, "-m", "unittest", "discover", "-s", "tests"], staging)


def _run(command: list[str], staging: Path) -> subprocess.CompletedProcess[str]:
    """A run in the copy, with no rights over the live run.

    The engineer writes the test, so it is the engineer's code. It executes
    in the copy, and the environment is scrubbed of CODEX_*: without the
    owning thread any attempt to touch the live run hits the same guard as
    always.
    """

    home = staging / "home"
    home.mkdir(exist_ok=True)
    env = {key: value for key, value in os.environ.items() if not key.startswith("CODEX_")}
    env.update(
        {
            "PYTHONPATH": str(staging / "src"),
            "HOME": str(home),
            "TMPDIR": str(home),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return subprocess.run(
        command,
        cwd=staging,
        env=env,
        capture_output=True,
        text=True,
        timeout=TEST_TIMEOUT_SECONDS,
    )


def _store_backup(tree: RuntimeTree, record: PatchRecord) -> None:
    folder = tree.root / "patches" / record.patch_id
    folder.mkdir(parents=True, exist_ok=True)
    for change in record.changes:
        if change.sha256_before is None:
            continue
        (folder / f"{change.module}.orig").write_text(
            (tree.package / change.module).read_text(encoding="utf-8"), encoding="utf-8"
        )
    (folder / "patch.json").write_text(
        json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )


def _patch_id(edits: Sequence[Edit], at: str) -> str:
    material = "\n".join(
        part
        for edit in edits
        for part in (edit.module, edit.old or "", edit.new)
    )
    digest = hashlib.sha256((material + "\n" + at).encode("utf-8")).hexdigest()
    return f"patch-{digest[:16]}"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _tail(result: subprocess.CompletedProcess[str], *, limit: int = 1_500) -> str:
    """What exactly failed - by name, not by the last bytes of output.

    The tail of unittest output is warnings and dots; the names of failed
    tests stand higher and never made it in. A refusal that does not name
    the cause forces guessing (R31), so the names come first.
    """

    output = (result.stdout or "") + (result.stderr or "")
    named = [
        line.strip()
        for line in output.splitlines()
        if line.startswith(("FAIL:", "ERROR:", "Ran ", "FAILED", "OK"))
    ]
    head = "\n".join(named[:24])
    return (head + "\n...\n" if head else "") + output[-limit:]
