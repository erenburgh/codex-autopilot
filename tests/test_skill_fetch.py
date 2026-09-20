"""Fetching a skill the screener named, without a turn touching the network.

A fetch inside a screening turn would be a Codex turn reaching the network,
and the runtime's own rule is that a turn must never raise a permission
dialog: the dispatcher answers no approval, the dialog waits in a task
nobody is watching, and the run dies. So the screener names a provider and a
locator from what it already knows, and the RUNTIME fetches - an ordinary
local process outside any Codex turn.

No test here opens a socket. The transport is a seam; the offline case is
driven through it, and the real one is driven by hand.
"""

from __future__ import annotations

from pathlib import Path
import io
import sys
import unittest.mock
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from codex_autopilot.skill_fetch import (
    DEFAULT_SKILL_FETCH_HOSTS,
    MAX_FETCH_BYTES,
    SkillFetchError,
    fetch_skill_bundle,
    skill_source_url,
)

SKILL_MD = b"---\nname: taste\ndescription: A design discipline.\n---\n\n# Taste\n"


class Response(io.BytesIO):
    """The smallest thing that behaves like what urlopen returns."""

    def __init__(self, body: bytes, *, url: str = "", content_type: str = "text/plain"):
        super().__init__(body)
        self._url = url
        self._content_type = content_type

    def geturl(self) -> str:
        return self._url

    @property
    def headers(self):
        return {"Content-Type": self._content_type}

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
        return False


class FetchCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state_dir = Path(self.temp.name) / ".codex-autopilot"
        self.state_dir.mkdir(parents=True)
        self.requested: list[tuple[str, int]] = []

    def transport(self, body=SKILL_MD, *, url=None, content_type="text/plain", raises=None):
        def open_url(target: str, *, timeout: int):
            self.requested.append((target, timeout))
            if raises is not None:
                raise raises
            return Response(body, url=url or target, content_type=content_type)

        return open_url

    def fetch(self, **overrides):
        payload = {
            "provider": "github.com/example/skills",
            "locator": "skills/taste",
            "name": "taste",
            "allowed_hosts": DEFAULT_SKILL_FETCH_HOSTS,
            "timeout_seconds": 20,
            "open_url": self.transport(),
        }
        payload.update(overrides)
        return fetch_skill_bundle(self.state_dir, **payload)


class TheUrlIsBuiltFromWhatTheScreenerNamedTests(FetchCase):
    def test_a_github_provider_and_path_become_a_raw_url(self) -> None:
        self.assertEqual(
            skill_source_url("github.com/example/skills", "skills/taste", "main"),
            "https://raw.githubusercontent.com/example/skills/main/skills/taste/SKILL.md",
        )

    def test_a_raw_provider_is_used_as_given(self) -> None:
        self.assertEqual(
            skill_source_url(
                "raw.githubusercontent.com", "example/skills/main/skills/taste", "main"
            ),
            "https://raw.githubusercontent.com/example/skills/main/skills/taste/SKILL.md",
        )

    def test_a_provider_carrying_a_scheme_is_refused(self) -> None:
        for provider in ("https://github.com/e/s", "http://github.com/e/s", "git@github.com:e/s"):
            with self.subTest(provider=provider):
                with self.assertRaisesRegex(SkillFetchError, "host and path"):
                    skill_source_url(provider, "skills/taste", "main")

    def test_a_locator_that_climbs_is_refused(self) -> None:
        for locator in ("../../etc", "skills/../../..", "/etc/passwd"):
            with self.subTest(locator=locator):
                with self.assertRaises(SkillFetchError):
                    skill_source_url("github.com/example/skills", locator, "main")


class OnlyAllowedHostsOverHttpsTests(FetchCase):
    def test_a_host_outside_the_allowlist_is_refused_naming_the_allowed(self) -> None:
        with self.assertRaises(SkillFetchError) as caught:
            self.fetch(provider="evil.example/example/skills")

        self.assertIn("evil.example", str(caught.exception))
        for host in DEFAULT_SKILL_FETCH_HOSTS:
            self.assertIn(host, str(caught.exception))
        self.assertEqual(self.requested, [], "nothing was opened")

    def test_an_empty_allowlist_fetches_nothing_and_says_how(self) -> None:
        with self.assertRaises(SkillFetchError) as caught:
            self.fetch(allowed_hosts=())

        self.assertIn("skill_fetch_hosts", str(caught.exception))
        self.assertEqual(self.requested, [])

    def test_a_redirect_away_from_the_requested_url_is_refused(self) -> None:
        """A redirect can land anywhere, including outside the allowlist,
        so the landing place is compared rather than trusted."""

        with self.assertRaisesRegex(SkillFetchError, "redirect"):
            self.fetch(
                open_url=self.transport(url="https://codeload.github.com/elsewhere")
            )

    def test_the_request_carries_the_timeout_it_was_given(self) -> None:
        self.fetch(timeout_seconds=7)

        self.assertEqual(self.requested[0][1], 7)


class WhatComesBackIsCheckedTests(FetchCase):
    def test_a_skill_is_staged_inside_the_project(self) -> None:
        staged = self.fetch()

        # Resolved on both sides: /var is a symlink to /private/var on
        # macOS, and comparing one resolved path to one unresolved one
        # fails for a reason that has nothing to do with the fetch.
        self.assertTrue(staged.is_relative_to(self.state_dir.resolve()))
        self.assertEqual((staged / "SKILL.md").read_bytes(), SKILL_MD)

    def test_a_page_of_html_is_not_a_skill(self) -> None:
        with self.assertRaisesRegex(SkillFetchError, "not a skill"):
            self.fetch(
                open_url=self.transport(
                    b"<!DOCTYPE html>\n<html><body>404</body></html>",
                    content_type="text/html; charset=utf-8",
                )
            )

    def test_a_response_that_only_looks_like_a_page_is_also_refused(self) -> None:
        with self.assertRaisesRegex(SkillFetchError, "not a skill"):
            self.fetch(open_url=self.transport(b"<html>no content type</html>"))

    def test_an_empty_response_is_refused(self) -> None:
        with self.assertRaisesRegex(SkillFetchError, "empty"):
            self.fetch(open_url=self.transport(b"   \n"))

    def test_a_response_past_the_size_bound_is_refused_while_streaming(self) -> None:
        """Checked while reading, not after: a bound applied afterwards has
        already let the bytes into memory."""

        with self.assertRaises(SkillFetchError) as caught:
            self.fetch(open_url=self.transport(b"#" * (MAX_FETCH_BYTES + 1)))

        self.assertIn(str(MAX_FETCH_BYTES), str(caught.exception))
        self.assertFalse((self.state_dir / "staged-skills" / "taste").exists())

    def test_a_response_at_the_size_bound_is_accepted(self) -> None:
        body = b"---\nname: taste\n---\n" + b"#" * (MAX_FETCH_BYTES - 20)

        staged = self.fetch(open_url=self.transport(body))

        self.assertEqual(len((staged / "SKILL.md").read_bytes()), MAX_FETCH_BYTES)


class WhenTheNetworkIsNotThereTests(FetchCase):
    def test_being_offline_is_an_unmet_need_not_a_crash(self) -> None:
        import urllib.error

        with self.assertRaises(SkillFetchError) as caught:
            self.fetch(
                open_url=self.transport(
                    raises=urllib.error.URLError("nodename nor servname provided")
                )
            )

        self.assertIn("nodename", str(caught.exception))

    def test_a_404_names_the_url_that_was_not_there(self) -> None:
        import urllib.error

        with self.assertRaises(SkillFetchError) as caught:
            self.fetch(
                open_url=self.transport(
                    raises=urllib.error.HTTPError(
                        "https://raw.githubusercontent.com/example/skills/main/skills/taste/SKILL.md",
                        404, "Not Found", {}, None,
                    )
                )
            )

        self.assertIn("404", str(caught.exception))
        self.assertIn("skills/taste", str(caught.exception))

    def test_a_host_that_hangs_is_bounded_by_the_timeout(self) -> None:
        with self.assertRaises(SkillFetchError) as caught:
            self.fetch(open_url=self.transport(raises=TimeoutError("timed out")))

        self.assertIn("timed out", str(caught.exception))

    def test_nothing_is_left_staged_after_any_failure(self) -> None:
        import urllib.error

        for failure in (
            urllib.error.URLError("offline"),
            TimeoutError("timed out"),
            urllib.error.HTTPError("https://x/y", 500, "Server Error", {}, None),
        ):
            with self.subTest(failure=type(failure).__name__):
                with self.assertRaises(SkillFetchError):
                    self.fetch(open_url=self.transport(raises=failure))
                self.assertFalse(
                    (self.state_dir / "staged-skills" / "taste").exists()
                )


if __name__ == "__main__":
    unittest.main()


class TheHostileCasesTests(FetchCase):
    """The ones a user meets eventually, driven rather than assumed."""

    def test_a_redirect_to_a_host_outside_the_allowlist_is_refused(self) -> None:
        with self.assertRaises(SkillFetchError) as caught:
            self.fetch(open_url=self.transport(url="https://evil.example/taste/SKILL.md"))

        self.assertIn("evil.example", str(caught.exception))
        self.assertIn("redirect", str(caught.exception))

    def test_a_repository_that_exists_with_no_skill_file_is_refused(self) -> None:
        """GitHub answers a missing raw path with 404, not with a page."""

        import urllib.error

        with self.assertRaises(SkillFetchError) as caught:
            self.fetch(
                open_url=self.transport(
                    raises=urllib.error.HTTPError(
                        "https://raw.githubusercontent.com/example/skills/main/nope/SKILL.md",
                        404, "Not Found", {}, None,
                    )
                )
            )

        self.assertIn("404", str(caught.exception))
        self.assertIn("SKILL.md", str(caught.exception))

    def test_what_would_be_an_archive_is_just_refused_as_not_a_skill(self) -> None:
        """No archive is ever opened, so a zip bomb has nothing to expand
        into: the response is judged as text and gzip is not text."""

        with self.assertRaises(SkillFetchError):
            self.fetch(
                open_url=self.transport(
                    b"\x1f\x8b\x08\x00" + b"\x00" * 200,
                    content_type="application/gzip",
                )
            )

    def test_the_environment_cannot_put_a_proxy_or_a_secret_in_the_way(self) -> None:
        """Driven against the real opener, with a hostile environment set.

        Measured: a default opener built with HTTPS_PROXY in the environment
        carries a ProxyHandler whose proxies are {'https': <that value>}, so
        every fetch would go through whatever the environment named. Passing
        ProxyHandler({}) registers NO handler at all - an empty proxy map
        defines no *_open method, so add_handler drops it - and that is what
        suppresses it: build_opener then skips the default one too.
        """

        import os
        import urllib.request
        from codex_autopilot.skill_fetch import _open_without_credentials

        captured = {}

        def fake_open(self, url, timeout=None):
            captured["handlers"] = [type(h).__name__ for h in self.handlers]
            captured["headers"] = list(self.addheaders)
            return Response(SKILL_MD, url=url)

        hostile = {"HTTPS_PROXY": "http://attacker.example:8080",
                   "HTTP_PROXY": "http://attacker.example:8080"}
        with unittest.mock.patch.dict(os.environ, hostile):
            # The control: the default opener really would use it.
            self.assertEqual(
                [
                    handler.proxies
                    for handler in urllib.request.build_opener().handlers
                    if isinstance(handler, urllib.request.ProxyHandler)
                ],
                [{"http": "http://attacker.example:8080",
                  "https": "http://attacker.example:8080"}],
            )
            with unittest.mock.patch.object(
                urllib.request.OpenerDirector, "open", fake_open
            ):
                _open_without_credentials(
                    "https://raw.githubusercontent.com/a/b/main/c/SKILL.md", timeout=5
                )

        self.assertNotIn("ProxyHandler", captured["handlers"])
        for handler in captured["handlers"]:
            self.assertNotIn("Auth", handler, "no credential handler")
        self.assertEqual(
            [name for name, _value in captured["headers"]],
            ["User-Agent"],
            "no header this process did not write",
        )

    def test_only_https_is_ever_requested(self) -> None:
        self.fetch()

        self.assertTrue(self.requested[0][0].startswith("https://"))
