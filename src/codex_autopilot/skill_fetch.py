"""Fetching a skill the screener named, from outside a Codex turn.

The obvious design - let the screener download what it finds - is the one
that can kill a run.  A fetch inside a screening turn is a Codex turn
reaching the network, and the runtime's own rule, the one already in every
worker prompt, is that a turn must never raise a permission dialog: the
dispatcher answers no approval, the dialog then waits in a task nobody is
watching, and the run dies there.

So the screener never touches the network.  It names a provider and a
locator in its requisition from what it already knows, and the runtime
fetches - the dispatcher is an ordinary local process under the user's own
shell, outside any Codex turn, so it raises no approval, and it is already
the place that digests and admits a bundle.  A wrong locator is a failed
fetch, recorded as an unmet need, and the task continues.

What this version fetches is one ``SKILL.md``.  A skill's instructions are
the thing a worker reads, and fetching a single text file removes the whole
archive surface: nothing is ever expanded, so there is no archive to be a
bomb, and no redirect chain to follow into a host nobody allowed.  Bundles
of several files are a later step and a separate decision.
"""

from __future__ import annotations

import re
import shutil
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Sequence

from .hired_skills import SKILL_FILE, STAGED_SKILLS_DIRNAME


# Where a skill may be fetched from. The user extends this in config; a
# locator outside it is refused by name. Defaults are the two hosts that
# serve public skill repositories without credentials.
DEFAULT_SKILL_FETCH_HOSTS: tuple[str, ...] = (
    "github.com",
    "raw.githubusercontent.com",
)
DEFAULT_SKILL_FETCH_TIMEOUT_SECONDS = 20
# One SKILL.md. The same ceiling the admission path puts on a skill file, so
# a fetch cannot land something admission would refuse anyway.
MAX_FETCH_BYTES = 512 * 1024
# Read in chunks so the bound is enforced while streaming. A size checked
# after the read has already let the bytes into memory.
_CHUNK = 64 * 1024
_RAW_HOST = "raw.githubusercontent.com"
_PROVIDER = re.compile(r"^[A-Za-z0-9.-]+(?:/[A-Za-z0-9._-]+)*$")
_LOCATOR = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class SkillFetchError(ValueError):
    """The skill could not be fetched, and the reason is in the message."""


def skill_source_url(provider: str, locator: str, ref: str) -> str:
    """Turn what the screener named into one exact HTTPS URL.

    The screener gives a host and a path, never a URL: a scheme it could
    choose is a scheme it could get wrong, and there is nothing to decide -
    every fetch here is HTTPS.
    """

    host = _required(provider, "provider")
    path = _required(locator, "locator")
    if "://" in host or "@" in host:
        raise SkillFetchError(
            f"provider {host!r} must be a host and path such as "
            "'github.com/<owner>/<repo>', never a URL or an SSH address"
        )
    if _PROVIDER.fullmatch(host) is None:
        raise SkillFetchError(f"provider {host!r} is not a host and path")
    if _LOCATOR.fullmatch(path) is None or ".." in path.split("/"):
        raise SkillFetchError(
            f"locator {path!r} must be a relative path inside the repository"
        )
    reference = _required(ref, "ref")
    if _LOCATOR.fullmatch(reference) is None:
        raise SkillFetchError(f"ref {reference!r} is not a branch or tag name")
    parts = host.split("/")
    if parts[0] == "github.com":
        if len(parts) != 3:
            raise SkillFetchError(
                f"provider {host!r} must name owner and repository, as "
                "'github.com/<owner>/<repo>'"
            )
        _owner, repository = parts[1], parts[2]
        return (
            f"https://{_RAW_HOST}/{_owner}/{repository}/{reference}/{path}/{SKILL_FILE}"
        )
    if parts[0] == _RAW_HOST and len(parts) == 1:
        # The locator already carries owner/repo/ref/path.
        return f"https://{_RAW_HOST}/{path}/{SKILL_FILE}"
    return f"https://{host}/{path}/{SKILL_FILE}"


def fetch_skill_bundle(
    state_dir: Path,
    *,
    provider: str,
    locator: str,
    name: str,
    allowed_hosts: Sequence[str],
    timeout_seconds: int = DEFAULT_SKILL_FETCH_TIMEOUT_SECONDS,
    ref: str = "main",
    open_url: Callable[..., Any] | None = None,
) -> Path:
    """Fetch one skill into the project's staging directory, or refuse.

    Nothing is written outside the project: the staged directory is built
    under the state directory and handed to the ordinary admission path,
    which already refuses plugin-shaped bundles, symbolic links, escaping
    paths and oversize content.
    """

    skill_name = _skill_name(name)
    url = skill_source_url(provider, locator, ref)
    _require_allowed_host(url, allowed_hosts)
    body = _read_bounded(url, timeout_seconds, open_url or _open_without_credentials)
    staged = Path(state_dir).expanduser().resolve(strict=False) / STAGED_SKILLS_DIRNAME / skill_name
    # A previous attempt may have left a partial directory; a fetch replaces
    # what it stages rather than merging into it.
    shutil.rmtree(staged, ignore_errors=True)
    staged.mkdir(parents=True)
    (staged / SKILL_FILE).write_bytes(body)
    return staged


def _open_without_credentials(url: str, *, timeout: int) -> Any:
    """Open the URL with nothing the environment might have put there.

    No proxy, no stored password, no authentication handler, and no header
    this process did not write. A fetch that would need a secret is a fetch
    that should be refused rather than quietly authorized by whatever
    happens to be in the environment.
    """

    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(),
    )
    opener.addheaders = [("User-Agent", "codex-autopilot-skill-fetch")]
    return opener.open(url, timeout=timeout)


def _require_allowed_host(url: str, allowed_hosts: Sequence[str]) -> None:
    allowed = tuple(str(item).strip().lower() for item in allowed_hosts if str(item).strip())
    host = url.split("://", 1)[1].split("/", 1)[0].lower()
    if not allowed:
        raise SkillFetchError(
            "no host is allowed for fetching skills, so nothing is fetched. "
            "set runtime.skill_fetch_hosts in config.toml to the hosts this "
            "project may fetch from, for example "
            f"{list(DEFAULT_SKILL_FETCH_HOSTS)}"
        )
    if host not in allowed:
        raise SkillFetchError(
            f"host {host!r} is not allowed for fetching skills; "
            f"runtime.skill_fetch_hosts allows {list(allowed)}"
        )


def _read_bounded(url: str, timeout: int, open_url: Callable[..., Any]) -> bytes:
    try:
        with open_url(url, timeout=timeout) as response:
            landed = str(getattr(response, "geturl", lambda: url)() or url)
            if landed != url:
                # A redirect can land anywhere, including a host nobody
                # allowed, so where it actually landed is compared rather
                # than trusted. Refusing is safe here: a raw skill file is
                # served directly.
                raise SkillFetchError(
                    f"fetching {url} was redirected to {landed}; a skill is "
                    "fetched from exactly the host that was allowed"
                )
            content_type = str(
                (getattr(response, "headers", None) or {}).get("Content-Type") or ""
            ).lower()
            chunks: list[bytes] = []
            read = 0
            while True:
                chunk = response.read(_CHUNK)
                if not chunk:
                    break
                read += len(chunk)
                if read > MAX_FETCH_BYTES:
                    raise SkillFetchError(
                        f"{url} is larger than {MAX_FETCH_BYTES} bytes; a skill "
                        "file that big is refused rather than truncated"
                    )
                chunks.append(chunk)
    except SkillFetchError:
        raise
    except urllib.error.HTTPError as exc:
        raise SkillFetchError(f"fetching {url} failed: HTTP {exc.code} {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise SkillFetchError(f"fetching {url} failed: {exc.reason}") from exc
    except (OSError, TimeoutError) as exc:
        raise SkillFetchError(f"fetching {url} failed: {exc}") from exc
    body = b"".join(chunks)
    if not body.strip():
        raise SkillFetchError(f"{url} returned an empty body")
    if "html" in content_type or body.lstrip()[:1] == b"<":
        # A missing path on a web host answers with a page, not an error.
        raise SkillFetchError(
            f"{url} returned a web page, not a skill: a skill is the text of "
            f"a {SKILL_FILE}"
        )
    try:
        # A SKILL.md is UTF-8 text. "Not HTML" is not the same as "is text":
        # an archive passed that check, and it is the shape a zip bomb would
        # arrive in. Nothing here expands an archive, but admitting one as a
        # skill file would put bytes in front of a worker as instructions.
        body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillFetchError(
            f"{url} returned bytes that are not UTF-8 text, so it is not a "
            f"skill: a skill is the text of a {SKILL_FILE}, never an archive "
            "or a binary"
        ) from exc
    return body


def _skill_name(value: Any) -> str:
    text = _required(value, "name")
    if _NAME.fullmatch(text) is None:
        raise SkillFetchError(
            f"skill name {text!r} is not a single safe directory component"
        )
    return text


def _required(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SkillFetchError(f"skill fetch {label} must be a non-empty string")
    return value.strip()
