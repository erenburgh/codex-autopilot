"""Hiring: the screener's requisition and the stack the runtime resolves.

No plan can know which skills a task needs.  The plan is authored before
anyone has looked at the repository the task will touch, so ``skill_packs``
and ``loaded_skills`` stay empty in real runs, and every worker prompt
carries ``"loaded_skills":[]`` - measured on 20 Sep 2026 against a live
plan.  This module is the layer that decides per task, at the moment the
worker is hired.

The screener names capabilities and says why this task needs them.  The
runtime, not the screener, turns that into a stack: it replays the exact
resolver every plan-declared skill already passes, one candidate at a time
against the stack being assembled.  A skill that cannot be resolved is
recorded with the resolver's own refusal instead of being quietly dropped.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .skill_packs import (
    SkillPack,
    SkillPackError,
    SkillReference,
    resolve_skill_stack,
    skill_identifier,
    skill_pack_from_raw,
    skill_reference_from_raw,
)


SCREENING_PREFIX = "AUTOPILOT_SCREENING: "
# One directory component of the user's skills directory, never a path.
_INSTALLED_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# Installed pack manifests, one JSON file per exact revision, inside the
# project's state directory beside logs/ and migrations/.
SKILL_LIBRARY_DIRNAME = "skills"
NECESSITIES = ("required", "helpful")
# "installed" is a bundle the runtime admitted into the project: the skill
# itself, which a worker reads, as distinct from a pack, which governs.
HIRING_STATUSES = ("hired", "installed", "withheld", "unmet")
# Set by the runtime from where IT read the bundle, never from the
# requisition. "local" is the user's own Codex home; "market" is content
# fetched from a repository and admitted into the project.
BUNDLE_ORIGIN_LOCAL = "local"
BUNDLE_ORIGIN_MARKET = "market"
BUNDLE_ORIGINS = (BUNDLE_ORIGIN_LOCAL, BUNDLE_ORIGIN_MARKET)
REQUISITION_ITEM_FIELDS = (
    "capability",
    "rationale",
    "necessity",
    "candidates",
    "search_intent",
    "bundle",
    "installed",
)
# Provenance is never one of these. A pack or bundle is local because the
# runtime itself read it out of the user's own Codex home, and there is no
# field a screener can set to say so - if there were, a fetched bundle would
# set it too and the R18 withholding would evaporate.
FORBIDDEN_PROVENANCE_FIELDS = ("origin", "provenance", "local", "source", "trusted")
BUNDLE_FIELDS = ("name", "staged_path", "provider", "locator", "ref")
MAX_RATIONALE_CHARS = 1_000
MAX_SEARCH_INTENT_CHARS = 1_000
MAX_REASON_CHARS = 2_000
# A hired pack enters the worker prompt whole: procedures, checklists,
# failure modes, quality criteria, provenance and its deterministic checks.
# The prompt budget is 193 800 characters (ai_studio.MAX_PROMPT_CHARS), and
# it is shared with the rules block, the task contract and the selected
# context, none of which may be truncated. At a few kilobytes per pack,
# eight is roughly a sixth of that budget - a ceiling, not a measurement.
# The resolver already admits at most one pack per capability, so eight
# capabilities is also eight distinct kinds of help.
MAX_REQUISITION_ITEMS = 8
# Alternatives for one capability, tried in the order the screener gave.
MAX_CANDIDATES_PER_ITEM = 4
# How much of each installed pack the screening brief carries. The brief
# describes every installed pack, so this multiplies by the size of the
# library; the hired packs arrive in full in the worker's own prompt.
INVENTORY_LINES_PER_PACK = 3
INVENTORY_LINE_CHARS = 240


class ScreeningProtocolError(ValueError):
    """The screener's answer cannot be read as a requisition."""


class SkillLibraryError(ValueError):
    """The installed skill library cannot be read into an exact catalog."""


def load_skill_library(directory: Path) -> tuple[SkillPack, ...]:
    """Read the pack manifests installed on this machine, in path order.

    A manifest that does not parse stops the read by name rather than being
    skipped.  Skipping would produce a catalog that is quietly smaller than
    the directory, and the hiring record would then blame a missing skill on
    the screener instead of on a broken file.
    """

    if not directory.is_dir():
        return ()
    packs: list[SkillPack] = []
    for path in sorted(directory.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SkillLibraryError(f"installed skill {path.name} is not readable JSON: {exc}") from exc
        try:
            packs.append(skill_pack_from_raw(raw, f"installed skill {path.name}"))
        except SkillPackError as exc:
            raise SkillLibraryError(str(exc)) from exc
    keys = [pack.reference.key for pack in packs]
    if len(set(keys)) != len(keys):
        raise SkillLibraryError(
            "the installed skill library declares the same id@version twice; "
            "resolution would be ambiguous"
        )
    return tuple(packs)


def skill_catalog(
    plan_packs: Sequence[SkillPack], library_packs: Sequence[SkillPack]
) -> tuple[SkillPack, ...]:
    """Union the plan's catalog with what is installed on this machine.

    Where both declare the same id@version, the library entry wins, but only
    when the revision digest is identical.  ``revision_sha256`` covers every
    executable instruction and deliberately excludes trust status and
    evidence ids, so an identical digest means the library holds the same
    behaviour in a later trust state - the promotion of a candidate the plan
    declared.  A different digest under the same id@version is a redefinition:
    the trust records on file describe a revision that no longer exists, so
    the catalog refuses rather than choosing one silently.

    Winning here grants no trust.  A library manifest that claims ``trusted``
    is still re-resolved against Project Memory by
    ``validate_trusted_skill_promotions`` and ``_validate_skill_qualification``
    before anything reaches a prompt.
    """

    by_key: dict[tuple[str, str], SkillPack] = {
        pack.reference.key: pack for pack in plan_packs
    }
    for pack in library_packs:
        declared = by_key.get(pack.reference.key)
        if declared is not None and declared.revision_sha256 != pack.revision_sha256:
            raise SkillLibraryError(
                f"installed skill {pack.id}@{pack.version} redefines the plan-declared "
                f"revision {declared.revision_sha256[:12]} as {pack.revision_sha256[:12]}; "
                "a new behaviour needs a new version"
            )
        by_key[pack.reference.key] = pack
    return tuple(by_key.values())


@dataclass(frozen=True, slots=True)
class SkillBundleRequest:
    """A skill bundle the screener fetched and left inside the project.

    ``staged_path`` is relative to the project's .codex-autopilot directory
    and never absolute: the admission step resolves it there and refuses
    anything outside, and refusing the shape here as well means a screener
    is told what is wrong instead of having a path quietly rejected later.
    """

    name: str
    provider: str
    # Either the screener staged it itself, or it named where the RUNTIME
    # should fetch it from. A screening turn never touches the network: a
    # turn that raises a permission dialog kills the run, because the
    # dispatcher answers no approval and the dialog waits unseen.
    staged_path: str = ""
    locator: str = ""
    ref: str = "main"

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "provider": self.provider,
            **({"staged_path": self.staged_path} if self.staged_path else {}),
            **({"locator": self.locator} if self.locator else {}),
            **({"ref": self.ref} if self.ref != "main" else {}),
        }


@dataclass(frozen=True, slots=True)
class RequisitionItem:
    """One capability the screener asks for, and why this task needs it."""

    capability: str
    rationale: str
    necessity: str
    candidates: tuple[SkillReference, ...] = ()
    search_intent: str = ""
    bundle: SkillBundleRequest | None = None
    # The name of a skill the user installed herself. Only a name: the
    # runtime resolves it against its own read of her Codex home.
    installed: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "rationale": self.rationale,
            "necessity": self.necessity,
            **(
                {"candidates": [item.to_dict() for item in self.candidates]}
                if self.candidates
                else {}
            ),
            **({"search_intent": self.search_intent} if self.search_intent else {}),
            **({"bundle": self.bundle.to_dict()} if self.bundle is not None else {}),
            **({"installed": self.installed} if self.installed else {}),
        }


@dataclass(frozen=True, slots=True)
class SkillRequisition:
    task_id: str
    items: tuple[RequisitionItem, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "items": [item.to_dict() for item in self.items],
        }


@dataclass(frozen=True, slots=True)
class HiringOutcome:
    """What the runtime did with one requisition item, and on what grounds."""

    capability: str
    rationale: str
    necessity: str
    status: str
    skill: SkillReference | None = None
    reason: str = ""
    search_intent: str = ""
    bundle: tuple[tuple[str, str], ...] = ()

    @property
    def bundle_record(self) -> dict[str, str]:
        return dict(self.bundle)

    @property
    def is_external_bundle(self) -> bool:
        """Whether this bundle came from outside the user's own machine.

        A bundle the runtime read from her Codex home is hers: she installed
        it and uses it, so it is not external content and is not withheld
        from the verifier. A bundle fetched from a repository is, and is.
        """

        return self.bundle_record.get("origin") == BUNDLE_ORIGIN_MARKET

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "rationale": self.rationale,
            "necessity": self.necessity,
            "status": self.status,
            **({"skill": self.skill.to_dict()} if self.skill is not None else {}),
            **({"bundle": dict(self.bundle)} if self.bundle else {}),
            **({"reason": self.reason} if self.reason else {}),
            **({"search_intent": self.search_intent} if self.search_intent else {}),
        }


@dataclass(frozen=True, slots=True)
class HiringDecision:
    """The recorded hire for one task: what was taken, and what was not."""

    task_id: str
    outcomes: tuple[HiringOutcome, ...] = ()

    @property
    def hired(self) -> tuple[SkillReference, ...]:
        return tuple(
            item.skill
            for item in self.outcomes
            if item.status == "hired" and item.skill is not None
        )

    @property
    def installed(self) -> tuple[HiringOutcome, ...]:
        """Bundles admitted into the project: the skills themselves."""

        return tuple(item for item in self.outcomes if item.status == "installed")

    @property
    def unfilled(self) -> tuple[HiringOutcome, ...]:
        """Every capability the worker asked for and did not get.

        An installed bundle is not one of them: the worker did get it, as a
        skill to read rather than as a governed pack.
        """

        return tuple(
            item for item in self.outcomes if item.status not in {"hired", "installed"}
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "outcomes": [item.to_dict() for item in self.outcomes],
        }


def parse_screening_result(
    message: str, *, task_id: str, installed_names: Sequence[str] | None = None
) -> SkillRequisition:
    """Parse one exact, final, structured requisition for this exact task.

    Free-form reasoning may precede the protocol line.  The line must occur
    exactly once and be the final non-empty line, so a quoted example or an
    abandoned draft cannot advance durable state - the same rule the
    verifier protocol enforces, for the same reason.
    """

    lines = [line.strip() for line in message.splitlines() if line.strip()]
    protocol = [line for line in lines if line.startswith(SCREENING_PREFIX)]
    if len(protocol) != 1:
        raise ScreeningProtocolError(
            "screening response must carry exactly one AUTOPILOT_SCREENING line"
        )
    if lines[-1] != protocol[0]:
        raise ScreeningProtocolError(
            "the AUTOPILOT_SCREENING line must be the final non-empty line"
        )
    try:
        raw = json.loads(protocol[0][len(SCREENING_PREFIX) :])
    except json.JSONDecodeError as exc:
        raise ScreeningProtocolError("screening result is not valid JSON") from exc
    return requisition_from_raw(raw, task_id=task_id, installed_names=installed_names)


def requisition_from_raw(
    raw: Any, *, task_id: str, installed_names: Sequence[str] | None = None
) -> SkillRequisition:
    if not isinstance(raw, Mapping):
        raise ScreeningProtocolError("screening result must be a JSON object")
    _reject_unknown(raw, {"task_id", "items"}, "screening result")
    declared = _required_text(raw.get("task_id"), "screening result.task_id", 128)
    if declared != task_id:
        raise ScreeningProtocolError(
            f"screening result names task {declared!r}; this screening was asked "
            f"about task {task_id!r}"
        )
    items_raw = raw.get("items")
    if not isinstance(items_raw, list):
        raise ScreeningProtocolError("screening result.items must be an array")
    if len(items_raw) > MAX_REQUISITION_ITEMS:
        raise ScreeningProtocolError(
            f"a requisition may ask for at most {MAX_REQUISITION_ITEMS} capabilities; "
            f"got {len(items_raw)}"
        )
    items = tuple(
        _item_from_raw(item, f"screening item {index}", installed_names)
        for index, item in enumerate(items_raw, 1)
    )
    capabilities = [item.capability for item in items]
    duplicates = sorted(
        {name for name in capabilities if capabilities.count(name) > 1}
    )
    if duplicates:
        # One capability yields at most one loaded pack: the resolver refuses
        # two packs claiming the same capability in one stack. Two items for
        # it would be a request the runtime could never fill as written.
        raise ScreeningProtocolError(
            "a requisition asks for each capability once; duplicated: "
            + ", ".join(duplicates)
        )
    return SkillRequisition(task_id=task_id, items=items)


def resolve_requisition(
    requisition: SkillRequisition,
    catalog: Sequence[SkillPack],
    *,
    qualification_evidence_store: Any | None = None,
    require_qualification: bool = True,
) -> HiringDecision:
    """Turn a requisition into a stack by replaying the production resolver.

    Every candidate is resolved against the stack already assembled, not on
    its own: capability duplication, declared conflicts, trusted status and
    the revision-bound qualification PASS are all decided by
    ``resolve_skill_stack`` itself.  Nothing here restates those rules, so a
    rule that stops running stops being enforced here too - visibly.
    """

    packs = tuple(catalog)
    present = {pack.reference.key for pack in packs}
    hired: list[SkillReference] = []
    outcomes: list[HiringOutcome] = []
    for item in requisition.items:
        chosen: SkillReference | None = None
        refusals: list[str] = []
        for candidate in item.candidates:
            if candidate.key not in present:
                refusals.append(
                    f"{candidate.id}@{candidate.version} is not in the catalog"
                )
                continue
            try:
                resolve_skill_stack(
                    packs,
                    (*hired, candidate),
                    qualification_evidence_store=qualification_evidence_store,
                    require_qualification=require_qualification,
                )
            except SkillPackError as exc:
                refusals.append(f"{candidate.id}@{candidate.version}: {exc}")
                continue
            chosen = candidate
            break
        if chosen is not None:
            hired.append(chosen)
            outcomes.append(
                HiringOutcome(
                    capability=item.capability,
                    rationale=item.rationale,
                    necessity=item.necessity,
                    status="hired",
                    skill=chosen,
                )
            )
            continue
        outcomes.append(
            HiringOutcome(
                capability=item.capability,
                rationale=item.rationale,
                necessity=item.necessity,
                # Present but unusable is a different fact from absent: the
                # first is qualification work waiting to be done, the second
                # is a skill this machine does not have at all.
                status=(
                    "withheld"
                    if any(item_ref.key in present for item_ref in item.candidates)
                    else "unmet"
                ),
                reason=_bounded(
                    "; ".join(refusals) or "the screener named no candidate",
                    MAX_REASON_CHARS,
                ),
                search_intent=item.search_intent,
            )
        )
    return HiringDecision(task_id=requisition.task_id, outcomes=tuple(outcomes))


def apply_admitted_bundles(
    decision: HiringDecision,
    admitted: Mapping[str, Mapping[str, Any]],
    refused: Mapping[str, str] = {},
) -> HiringDecision:
    """Upgrade the outcomes whose bundle the runtime actually admitted.

    Resolution runs first and knows only packs, so a bundle item comes out of
    it unmet. Admission touches the filesystem and belongs to the runtime, so
    it happens after, and an item whose bundle was refused keeps the unmet
    outcome with the refusal as its reason - never a claim that a skill is
    there when it is not.
    """

    upgraded: list[HiringOutcome] = []
    for outcome in decision.outcomes:
        record = admitted.get(outcome.capability)
        if record is None:
            reason = refused.get(outcome.capability)
            upgraded.append(
                outcome
                if reason is None
                else HiringOutcome(
                    capability=outcome.capability,
                    rationale=outcome.rationale,
                    necessity=outcome.necessity,
                    status="unmet",
                    reason=_bounded(reason, MAX_REASON_CHARS),
                    search_intent=outcome.search_intent,
                )
            )
            continue
        upgraded.append(
            HiringOutcome(
                capability=outcome.capability,
                rationale=outcome.rationale,
                necessity=outcome.necessity,
                status="installed",
                bundle=tuple(sorted((str(k), str(v)) for k, v in record.items())),
            )
        )
    return HiringDecision(task_id=decision.task_id, outcomes=tuple(upgraded))


def hiring_decision_from_raw(raw: Any) -> HiringDecision:
    """Read a recorded decision back fail-closed, the way state is read."""

    if not isinstance(raw, Mapping):
        raise ScreeningProtocolError("hiring decision must be an object")
    _reject_unknown(raw, {"task_id", "outcomes"}, "hiring decision")
    task_id = _required_text(raw.get("task_id"), "hiring decision.task_id", 128)
    outcomes_raw = raw.get("outcomes")
    if not isinstance(outcomes_raw, list):
        raise ScreeningProtocolError("hiring decision.outcomes must be an array")
    outcomes = tuple(
        _outcome_from_raw(item, f"hiring outcome {index}")
        for index, item in enumerate(outcomes_raw, 1)
    )
    capabilities = [item.capability for item in outcomes]
    if len(set(capabilities)) != len(capabilities):
        raise ScreeningProtocolError(
            "a hiring decision records each capability once"
        )
    return HiringDecision(task_id=task_id, outcomes=outcomes)


def _item_from_raw(
    raw: Any, label: str, installed_names: Sequence[str] | None = None
) -> RequisitionItem:
    if not isinstance(raw, Mapping):
        raise ScreeningProtocolError(f"{label} must be an object")
    _reject_unknown(raw, set(REQUISITION_ITEM_FIELDS), label)
    capability = _identifier(raw.get("capability"), f"{label}.capability")
    rationale = _required_text(raw.get("rationale"), f"{label}.rationale", MAX_RATIONALE_CHARS)
    necessity = _choice(raw.get("necessity"), NECESSITIES, f"{label}.necessity")
    candidates_raw = raw.get("candidates", [])
    if not isinstance(candidates_raw, list):
        raise ScreeningProtocolError(f"{label}.candidates must be an array")
    if len(candidates_raw) > MAX_CANDIDATES_PER_ITEM:
        raise ScreeningProtocolError(
            f"{label}.candidates may name at most {MAX_CANDIDATES_PER_ITEM} alternatives"
        )
    try:
        candidates = tuple(
            skill_reference_from_raw(entry, f"{label}.candidates[{index}]")
            for index, entry in enumerate(candidates_raw)
        )
    except SkillPackError as exc:
        raise ScreeningProtocolError(str(exc)) from exc
    keys = [candidate.key for candidate in candidates]
    if len(set(keys)) != len(keys):
        raise ScreeningProtocolError(f"{label}.candidates repeats an exact revision")
    search_intent = ""
    if raw.get("search_intent") is not None:
        search_intent = _required_text(
            raw.get("search_intent"), f"{label}.search_intent", MAX_SEARCH_INTENT_CHARS
        )
    bundle = (
        _bundle_from_raw(raw.get("bundle"), f"{label}.bundle")
        if raw.get("bundle") is not None
        else None
    )
    installed = ""
    if raw.get("installed") is not None:
        installed = _required_text(raw.get("installed"), f"{label}.installed", 128)
        if _INSTALLED_NAME.fullmatch(installed) is None:
            raise ScreeningProtocolError(
                f"{label}.installed must be the name of one installed skill, not a "
                f"path: got {installed!r}"
            )
        # A name that was never offered is refused HERE, while it is still a
        # protocol error the screener gets to read and answer - it has one
        # more attempt. Left to resolution it became a valid requisition
        # that merely failed, and the capability went down as an unmet need
        # with the market never considered. Measured twice on a live run.
        if installed_names is not None and installed not in set(installed_names):
            offer = ", ".join(sorted(installed_names))
            raise ScreeningProtocolError(
                f"{label}.installed names {installed!r}, which is not among the "
                f"skills installed on this machine. Installed: "
                f"{offer or 'nothing at all'}. Name one of those, pick a declared "
                f"pack, or hand over a bundle instead - do not invent a name."
            )
    offered = [
        name
        for name, given in (
            ("candidates", bool(candidates)),
            ("bundle", bundle is not None),
            ("installed", bool(installed)),
        )
        if given
    ]
    if len(offered) > 1:
        # One capability yields one outcome, so two ways to fill it cannot
        # both be honoured - and "an installed skill that is also a fetched
        # bundle" is the shape a provenance bypass would take.
        raise ScreeningProtocolError(
            f"{label} offers {', '.join(offered)} for one capability; give exactly "
            "one of candidates, bundle or installed"
        )
    if not offered and not search_intent:
        raise ScreeningProtocolError(
            f"{label} names no candidate, no bundle, no installed skill and no "
            "search_intent, so nothing can act on it: give exact {id, version} "
            "candidates, hand over a bundle, name an installed skill, or say in "
            "search_intent what to look for"
        )
    return RequisitionItem(
        capability=capability,
        rationale=rationale,
        necessity=necessity,
        candidates=candidates,
        search_intent=search_intent,
        bundle=bundle,
        installed=installed,
    )


def _bundle_from_raw(raw: Any, label: str) -> SkillBundleRequest:
    if not isinstance(raw, Mapping):
        raise ScreeningProtocolError(f"{label} must be an object")
    _reject_unknown(raw, set(BUNDLE_FIELDS), label)
    staged = ""
    if raw.get("staged_path") is not None:
        staged = _required_text(raw.get("staged_path"), f"{label}.staged_path", 512)
        if staged.startswith(("/", "~")) or ".." in Path(staged).parts:
            # It is joined to the project state directory. A path that could
            # climb out of it is refused where the screener can read why, not
            # silently at the admission step.
            raise ScreeningProtocolError(
                f"{label}.staged_path must be relative to the project's "
                f".codex-autopilot directory and must not climb out of it; got {staged!r}"
            )
    locator = (
        _required_text(raw.get("locator"), f"{label}.locator", 512)
        if raw.get("locator") is not None
        else ""
    )
    # staged_path is the discriminator: present means the bundle is already
    # in the project, absent means the runtime fetches provider+locator.
    # locator stays meaningful in both - for a staged bundle it records
    # where the text came from.
    if not staged and not locator:
        raise ScreeningProtocolError(
            f"{label} names neither staged_path (a bundle already in the "
            "project) nor locator (the path under provider that the runtime "
            "will fetch), so there is nothing to bring in"
        )
    return SkillBundleRequest(
        name=_required_text(raw.get("name"), f"{label}.name", 128),
        staged_path=staged,
        ref=(
            _required_text(raw.get("ref"), f"{label}.ref", 128)
            if raw.get("ref") is not None
            else "main"
        ),
        # External content: the trust ladder refuses an external record with
        # no provider, so a bundle with no origin could never be recorded.
        provider=_required_text(raw.get("provider"), f"{label}.provider", 512),
        locator=locator,
    )


def _outcome_from_raw(raw: Any, label: str) -> HiringOutcome:
    if not isinstance(raw, Mapping):
        raise ScreeningProtocolError(f"{label} must be an object")
    _reject_unknown(
        raw,
        {
            "capability", "rationale", "necessity", "status", "skill", "reason",
            "search_intent", "bundle",
        },
        label,
    )
    status = _choice(raw.get("status"), HIRING_STATUSES, f"{label}.status")
    skill = None
    if raw.get("skill") is not None:
        try:
            skill = skill_reference_from_raw(raw.get("skill"), f"{label}.skill")
        except SkillPackError as exc:
            raise ScreeningProtocolError(str(exc)) from exc
    bundle_raw = raw.get("bundle")
    if bundle_raw is not None and not isinstance(bundle_raw, Mapping):
        raise ScreeningProtocolError(f"{label}.bundle must be an object")
    bundle = tuple(sorted((str(k), str(v)) for k, v in (bundle_raw or {}).items()))
    if bundle:
        origin = dict(bundle).get("origin")
        if origin not in BUNDLE_ORIGINS:
            raise ScreeningProtocolError(
                f"{label}.bundle must record where the runtime read it: "
                f"origin is one of {list(BUNDLE_ORIGINS)}"
            )
        # A local bundle has no provider because nobody published it to the
        # runtime; a market one must name where it came from, because the
        # trust ladder refuses external evidence without a provider.
        if (origin == BUNDLE_ORIGIN_MARKET) != bool(dict(bundle).get("provider")):
            raise ScreeningProtocolError(
                f"{label}.bundle origin {origin!r} disagrees with its provider"
            )
    if (status == "installed") != bool(bundle):
        raise ScreeningProtocolError(
            f"{label} must carry the bundle it installed, and none when it installed nothing"
        )
    if (status == "hired") != (skill is not None):
        raise ScreeningProtocolError(
            f"{label} must name exactly the skill it hired, and none when it hired nobody"
        )
    return HiringOutcome(
        capability=_identifier(raw.get("capability"), f"{label}.capability"),
        rationale=_required_text(raw.get("rationale"), f"{label}.rationale", MAX_RATIONALE_CHARS),
        necessity=_choice(raw.get("necessity"), NECESSITIES, f"{label}.necessity"),
        status=status,
        skill=skill,
        reason=_optional_text(raw.get("reason"), f"{label}.reason", MAX_REASON_CHARS),
        search_intent=_optional_text(
            raw.get("search_intent"), f"{label}.search_intent", MAX_SEARCH_INTENT_CHARS
        ),
        bundle=bundle,
    )


def _identifier(value: Any, label: str) -> str:
    try:
        return skill_identifier(value, label)
    except SkillPackError as exc:
        raise ScreeningProtocolError(str(exc)) from exc


def _required_text(value: Any, label: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ScreeningProtocolError(f"{label} must be a non-empty string")
    text = value.strip()
    if len(text) > limit:
        raise ScreeningProtocolError(f"{label} must be at most {limit} characters")
    return text


def _optional_text(value: Any, label: str, limit: int) -> str:
    if value is None:
        return ""
    return _required_text(value, label, limit)


def _choice(value: Any, allowed: Iterable[str], label: str) -> str:
    choices = tuple(allowed)
    text = _required_text(value, label, 64)
    if text not in choices:
        raise ScreeningProtocolError(f"{label} must be one of {sorted(choices)}")
    return text


def _bounded(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _reject_unknown(raw: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        # R31: a refusal names what is accepted. A screener told only that a
        # field is wrong retries with the next plausible synonym.
        raise ScreeningProtocolError(
            f"{label} has unknown fields: {', '.join(unknown)}. "
            f"the accepted fields are: {', '.join(sorted(allowed))}"
        )


def inventory_entry(pack: SkillPack) -> dict[str, Any]:
    """A bounded description of one installed pack for the screening brief.

    The full prompt dictionary of a pack is what a worker receives once it is
    hired.  Putting all of them in the brief would spend the screener's whole
    context on skills it will not take, so the brief carries what a hiring
    judgment actually needs: what the pack is for, how far it is trusted, and
    the first few lines of what it makes the worker do.
    """

    return {
        "id": pack.id,
        "version": pack.version,
        "capability": pack.capability,
        "source": pack.source,
        "status": pack.status,
        "procedures": [
            _bounded(text, INVENTORY_LINE_CHARS)
            for text in pack.procedures[:INVENTORY_LINES_PER_PACK]
        ],
        "quality_criteria": [
            _bounded(text, INVENTORY_LINE_CHARS)
            for text in pack.quality_criteria[:INVENTORY_LINES_PER_PACK]
        ],
        **(
            {"conflicts_with": [item.to_dict() for item in pack.conflicts_with]}
            if pack.conflicts_with
            else {}
        ),
    }


def recorded_hiring(
    records: Any, *, task_id: str, graph_version: int
) -> HiringDecision | None:
    """Read back the hire made for this exact task contract, or nothing.

    A replan rewrites task contracts under the same ids, so a hire made for
    graph version N says nothing about the task that now carries that id.
    Reusing it would put skills chosen for vanished work in front of a
    worker doing different work.
    """

    if not isinstance(records, Mapping):
        return None
    record = records.get(task_id)
    if not isinstance(record, Mapping) or record.get("graph_version") != graph_version:
        return None
    return hiring_decision_from_raw(record.get("decision"))


def record_hiring(
    records: dict[str, Any],
    *,
    task_id: str,
    graph_version: int,
    decision: HiringDecision,
    requisition: SkillRequisition | None,
    screened_by: Mapping[str, Any],
    at: str,
    unscreened: str = "",
) -> dict[str, Any]:
    """Store the hire, its grounds and its author, keyed by task contract."""

    if decision.task_id != task_id:
        raise ScreeningProtocolError(
            f"hiring decision names task {decision.task_id!r}, not {task_id!r}"
        )
    record = {
        "task_id": task_id,
        "graph_version": graph_version,
        "decision": decision.to_dict(),
        **({"requisition": requisition.to_dict()} if requisition is not None else {}),
        "screened_by": dict(screened_by),
        "recorded_at": at,
        **({"unscreened": _bounded(unscreened, MAX_REASON_CHARS)} if unscreened else {}),
    }
    records[task_id] = record
    return record
