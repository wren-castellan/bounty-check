import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bounty_check as bc


def _http_error(code):
    return urllib.error.HTTPError(url="", code=code, msg="", hdrs=None, fp=None)


class FetchLabeledIssuesTests(unittest.TestCase):
    def test_filters_out_pull_requests_and_paginates(self):
        page1 = [{"number": 1}, {"number": 2, "pull_request": {}}] + [
            {"number": n} for n in range(3, 102)
        ]
        with patch.object(bc, "_get", side_effect=[page1, []]) as mock_get:
            refs = bc.fetch_labeled_issues("foo", "bar", "bounty", token=None)
        self.assertEqual(refs, [f"foo/bar#{n}" for n in [1] + list(range(3, 102))])
        self.assertEqual(mock_get.call_count, 2)  # stopped after a short page

    def test_empty_repo(self):
        with patch.object(bc, "_get", return_value=[]):
            refs = bc.fetch_labeled_issues("foo", "bar", "bounty", token=None)
        self.assertEqual(refs, [])


class ParseRefTests(unittest.TestCase):
    def test_github_url(self):
        self.assertEqual(
            bc.parse_ref("https://github.com/foo/bar/issues/123"), ("foo", "bar", 123)
        )

    def test_issuehunt_url(self):
        self.assertEqual(
            bc.parse_ref("https://oss.issuehunt.io/r/foo/bar/issues/42"), ("foo", "bar", 42)
        )

    def test_shorthand(self):
        self.assertEqual(bc.parse_ref("foo/bar#7"), ("foo", "bar", 7))

    def test_garbage_raises(self):
        with self.assertRaises(ValueError):
            bc.parse_ref("not a valid ref")

    def test_shorthand_rejects_special_characters_instead_of_corrupting_the_url(self):
        with self.assertRaises(ValueError):
            bc.parse_ref("foo/bar?x=1#1")


class ResolveRefTests(unittest.TestCase):
    """resolve_ref() turns an Opire listing URL (whose own URL is an opaque
    ID with no owner/repo/number in it) into the real GitHub issue URL that's
    embedded as plain text on Opire's own page - no private API needed."""

    OPIRE_PAGE_HTML = (
        "<html>...<p>Issue URL: <!-- -->"
        "https://github.com/flowese/UdioWrapper/issues/7</p>...</html>"
    )

    def test_resolves_opire_url_to_the_embedded_github_issue(self):
        with patch.object(bc, "_fetch_url_text", return_value=self.OPIRE_PAGE_HTML) as mock_fetch:
            resolved = bc.resolve_ref("https://app.opire.dev/issues/01HW8CK374Y67WDDZG22BYVZQ4")
        self.assertEqual(resolved, "https://github.com/flowese/UdioWrapper/issues/7")
        mock_fetch.assert_called_once_with(
            "https://app.opire.dev/issues/01HW8CK374Y67WDDZG22BYVZQ4"
        )

    def test_non_opire_refs_pass_through_unchanged_without_any_network_call(self):
        with patch.object(bc, "_fetch_url_text") as mock_fetch:
            self.assertEqual(bc.resolve_ref("foo/bar#1"), "foo/bar#1")
            self.assertEqual(
                bc.resolve_ref("https://github.com/foo/bar/issues/1"),
                "https://github.com/foo/bar/issues/1",
            )
        mock_fetch.assert_not_called()

    def test_opire_page_with_no_recognizable_github_link_raises_cleanly(self):
        with patch.object(bc, "_fetch_url_text", return_value="<html>no issue link here</html>"):
            with self.assertRaises(ValueError):
                bc.resolve_ref("https://app.opire.dev/issues/deadbeef")


class CheckOneTests(unittest.TestCase):
    """Exercise check_one against mocked GitHub API responses only —
    no network access, so this suite runs offline and deterministically."""

    def _run(self, issue, repo, timeline_or_search=None):
        responses = [issue, repo]
        if timeline_or_search is not None:
            responses.append(timeline_or_search)

        def fake_get(url, token):
            if "/timeline" in url or "/search/issues" in url:
                return timeline_or_search
            if "/issues/" in url:
                return issue
            return repo

        with patch.object(bc, "_get", side_effect=fake_get):
            return bc.check_one("foo/bar#1", token=None)

    def test_closed_issue(self):
        v = self._run(
            issue={"title": "x", "state": "closed"},
            repo={"archived": False, "pushed_at": "2026-01-01T00:00:00Z"},
            timeline_or_search=[],
        )
        self.assertEqual(v.verdict, "CLOSED")

    def test_archived_repo_wins_even_if_issue_open(self):
        v = self._run(
            issue={"title": "x", "state": "open"},
            repo={"archived": True, "pushed_at": "2020-01-01T00:00:00Z"},
            timeline_or_search=[],
        )
        self.assertEqual(v.verdict, "ARCHIVED_REPO")

    def test_open_with_linked_pr(self):
        v = self._run(
            issue={"title": "x", "state": "open"},
            repo={"archived": False, "pushed_at": "2026-01-01T00:00:00Z"},
            timeline_or_search=[
                {
                    "event": "cross-referenced",
                    "source": {
                        "issue": {
                            "pull_request": {"url": "..."},
                            "state": "open",
                            "html_url": "https://github.com/foo/bar/pull/2",
                        }
                    },
                }
            ],
        )
        self.assertEqual(v.verdict, "HAS_OPEN_PR")
        self.assertIn("pull/2", v.notes[0])

    def test_genuinely_open_and_claimable(self):
        v = self._run(
            issue={"title": "x", "state": "open"},
            repo={"archived": False, "pushed_at": "2026-06-01T00:00:00Z"},
            timeline_or_search=[],
        )
        self.assertEqual(v.verdict, "OPEN_CLAIMABLE")
        self.assertEqual(v.notes, [])

    def test_stale_repo_flagged_but_still_claimable(self):
        v = self._run(
            issue={"title": "x", "state": "open"},
            repo={"archived": False, "pushed_at": "2018-01-01T00:00:00Z"},
            timeline_or_search=[],
        )
        self.assertEqual(v.verdict, "OPEN_CLAIMABLE")
        self.assertTrue(any("years" in n for n in v.notes))

    def test_issue_not_found(self):
        with patch.object(bc, "_get", return_value=None):
            v = bc.check_one("foo/bar#999", token=None)
        self.assertEqual(v.verdict, "NOT_FOUND")

    def test_bad_ref_does_not_hit_network(self):
        with patch.object(bc, "_get") as mock_get:
            v = bc.check_one("garbage", token=None)
        mock_get.assert_not_called()
        self.assertEqual(v.verdict, "BAD_REF")

    def test_rate_limit_on_one_ref_does_not_crash_the_batch(self):
        def fake_get(url, token):
            raise RuntimeError("GitHub API rate limit likely hit.")

        with patch.object(bc, "_get", side_effect=fake_get):
            v = bc.check_one("foo/bar#1", token=None)
        self.assertEqual(v.verdict, "ERROR")
        self.assertIn("rate limit", v.notes[0])

    def test_network_error_on_one_ref_does_not_crash_the_batch(self):
        with patch.object(
            bc, "_get", side_effect=urllib.error.URLError("timed out")
        ):
            v = bc.check_one("foo/bar#1", token=None)
        self.assertEqual(v.verdict, "ERROR")

    def test_opire_url_resolves_then_checks_the_real_github_issue(self):
        opire_html = (
            "<p>Issue URL: <!-- -->https://github.com/foo/bar/issues/1</p>"
        )

        def fake_get(url, token):
            if "/timeline" in url or "/search/issues" in url:
                return []
            if "/issues/" in url:
                return {"title": "x", "state": "open"}
            return {"archived": False, "pushed_at": "2026-06-01T00:00:00Z"}

        with patch.object(bc, "_fetch_url_text", return_value=opire_html), patch.object(
            bc, "_get", side_effect=fake_get
        ):
            v = bc.check_one("https://app.opire.dev/issues/01HW8CK374Y67WDDZG22BYVZQ4", token=None)

        self.assertEqual(v.verdict, "OPEN_CLAIMABLE")
        self.assertTrue(any("Resolved from Opire listing" in n for n in v.notes))

    def test_opire_url_with_unresolvable_page_is_a_clean_bad_ref_not_a_crash(self):
        with patch.object(bc, "_fetch_url_text", return_value="<html>nothing here</html>"):
            v = bc.check_one("https://app.opire.dev/issues/deadbeef", token=None)
        self.assertEqual(v.verdict, "BAD_REF")


class MainOutputEncodingTests(unittest.TestCase):
    """Real-world issue/PR titles are arbitrary Unicode (CJK, emoji, etc.),
    but Windows consoles default stdout to a narrow codepage like cp1252.
    Reproduces that exact condition with a strict-cp1252 stream instead of
    just asserting reconfigure() was called, so a regression here fails
    the same way the real bug did."""

    def test_non_ascii_title_does_not_crash_on_a_narrow_console_codepage(self):
        issue = {"title": "Audio no sound （Add asio support)", "state": "open"}
        repo = {"archived": False, "pushed_at": "2026-06-01T00:00:00Z"}

        def fake_get(url, token):
            if "/timeline" in url or "/search/issues" in url:
                return []
            if "/issues/" in url:
                return issue
            return repo

        narrow_stdout = io.TextIOWrapper(
            io.BytesIO(), encoding="cp1252", errors="strict"
        )
        with patch.object(bc, "_get", side_effect=fake_get), patch.object(
            sys, "stdout", narrow_stdout
        ), patch.object(sys, "stderr", narrow_stdout):
            bc.main(["foo/bar#1"])  # must not raise UnicodeEncodeError

        narrow_stdout.flush()


if __name__ == "__main__":
    unittest.main()
