#!/usr/bin/env python3
"""bounty-check: is this GitHub "bounty" issue actually still claimable?

Checks a GitHub issue against the things that quietly kill a bounty without
ever showing up in the listing text: the issue got closed, the repo got
archived, or someone already has an open PR against it. Bounty aggregator
sites (Algora, IssueHunt, Opire, and the raw "bounty"-labeled issues you find
via GitHub search) don't reliably reflect any of this - see README for why
this tool exists.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

API_ROOT = "https://api.github.com"

ISSUE_URL_RE = re.compile(
    r"github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues/(?P<number>\d+)"
)
ISSUEHUNT_URL_RE = re.compile(
    r"issuehunt\.io/r/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues/(?P<number>\d+)"
)
SHORTHAND_RE = re.compile(r"^(?P<owner>[A-Za-z0-9._-]+)/(?P<repo>[A-Za-z0-9._-]+)#(?P<number>\d+)$")
# Lookbehind excludes matches glued onto a larger domain/path (e.g. the
# "opire.dev" in "evil.com/opire.dev/issues/x") so the match always starts at
# a real URL boundary, not wherever the literal text happens to appear.
OPIRE_URL_RE = re.compile(
    r"(?<![\w/.-])(?:https?://)?(?:[\w-]+\.)*opire\.dev/issues/(?P<opire_id>[A-Za-z0-9]+)"
)
OPIRE_ISSUE_LABEL_RE = re.compile(r"Issue URL:")

STALE_DAYS = 730  # 2 years with no repo activity is worth flagging

# _fetch_url_text targets a host derived from user-supplied input (unlike
# _get, which only ever targets the fixed GitHub API host) - bound both size
# and total wall-clock time so a slow or oversized response can't hang or
# blow up memory on one bad ref.
FETCH_MAX_BYTES = 2_000_000
FETCH_TIMEOUT = 20


@dataclass
class Verdict:
    ref: str
    title: str = ""
    verdict: str = "UNKNOWN"
    notes: list[str] = field(default_factory=list)
    url: str = ""


def parse_ref(ref: str) -> tuple[str, str, int]:
    """Accept a full GitHub URL, an IssueHunt URL, or `owner/repo#123`."""
    for pattern in (ISSUE_URL_RE, ISSUEHUNT_URL_RE):
        m = pattern.search(ref)
        if m:
            return m.group("owner"), m.group("repo"), int(m.group("number"))
    m = SHORTHAND_RE.match(ref.strip())
    if m:
        return m.group("owner"), m.group("repo"), int(m.group("number"))
    raise ValueError(
        f"Couldn't parse {ref!r} as a GitHub issue reference. "
        "Expected a github.com/.../issues/N URL, an oss.issuehunt.io URL, "
        "or owner/repo#N."
    )


def _fetch_url_text(url: str) -> str:
    """Plain GET of an arbitrary (non-GitHub-API) URL, decoded as text.

    Bounded on size (FETCH_MAX_BYTES) and wall-clock time (FETCH_TIMEOUT).
    urllib's per-request `timeout` only bounds a single socket operation, not
    the whole response - a server that drips a few bytes at a time with
    delays just under that timeout can otherwise hold the connection open far
    longer than intended, so the actual fetch runs in a worker thread with a
    hard `future.result(timeout=...)` deadline instead. (Python can't force-kill
    a thread, so a truly adversarial drip can still leave that one worker
    running in the background - but the caller gets its TimeoutError back
    promptly either way and isn't blocked waiting on it.)
    """

    def _do_fetch() -> str:
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "bounty-check")
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read(FETCH_MAX_BYTES).decode("utf-8", errors="replace")

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(_do_fetch).result(timeout=FETCH_TIMEOUT)
    finally:
        pool.shutdown(wait=False)


def resolve_ref(ref: str) -> str:
    """Resolve an Opire bounty-listing URL to the real GitHub issue it's for.

    Opire's own issue-page URLs (app.opire.dev/issues/<opaque-id>) don't
    contain the owner/repo/number anywhere in the URL itself - unlike
    IssueHunt's URLs, which do. But the underlying GitHub issue URL is
    embedded as plain text in Opire's server-rendered page ("Issue URL:
    https://github.com/...") even before any JS runs, so one extra plain GET
    is enough to resolve it without needing Opire's private API.

    The page also embeds several *other* github.com/.../issues/N links
    unrelated to the one being viewed (a "browse other rewards" feed) - a
    plain whole-page search for the first GitHub issue link can pick up one
    of those instead of the right one, so this looks specifically for the
    link near the "Issue URL:" label rather than the first match anywhere
    on the page.

    Returns `ref` unchanged if it isn't an Opire URL.
    """
    m = OPIRE_URL_RE.search(ref)
    if not m:
        return ref
    url = m.group(0)
    if not url.startswith("http"):
        url = "https://" + url
    html = _fetch_url_text(url)

    label = OPIRE_ISSUE_LABEL_RE.search(html)
    if not label:
        raise ValueError(
            f"Couldn't find an \"Issue URL:\" label on the Opire page for {ref!r}."
        )
    # A small window right after the label, not the whole page - the label
    # and the link are typically separated only by an HTML comment/tag or two.
    nearby = html[label.end() : label.end() + 300]
    found = ISSUE_URL_RE.search(nearby)
    if not found:
        raise ValueError(
            f"Found an \"Issue URL:\" label but no GitHub issue link near it, for {ref!r}."
        )
    return f"https://github.com/{found.group('owner')}/{found.group('repo')}/issues/{found.group('number')}"


def _get(url: str, token: str | None) -> dict | list | None:
    """GET a GitHub API URL. Returns None on 404 (deleted/inaccessible), raises otherwise."""
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "bounty-check")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        if e.code == 403:
            raise RuntimeError(
                "GitHub API rate limit likely hit. Pass --token or set "
                "GITHUB_TOKEN to raise the limit from 60/hr to 5000/hr."
            ) from e
        raise


def _days_since(iso_timestamp: str) -> int:
    then = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - then).days


def find_linked_open_prs(owner: str, repo: str, number: int, token: str | None) -> list[str]:
    """Best-effort: find open PRs that reference this issue.

    Uses the issue timeline API (the same data GitHub's own UI uses for
    "linked pull requests"). Falls back to a text-search heuristic if the
    timeline call fails for any reason, since that's less reliable but
    better than nothing.
    """
    timeline = _get(
        f"{API_ROOT}/repos/{owner}/{repo}/issues/{number}/timeline?per_page=100", token
    )
    if isinstance(timeline, list):
        prs = []
        for event in timeline:
            if event.get("event") != "cross-referenced":
                continue
            source = event.get("source", {}) or {}
            source_issue = source.get("issue", {}) or {}
            if source_issue.get("pull_request") and source_issue.get("state") == "open":
                prs.append(source_issue.get("html_url", ""))
        return prs

    # Fallback heuristic: search PR bodies for a reference to this issue number.
    query = f"repo:{owner}/{repo} type:pr is:open {number} in:body"
    result = _get(f"{API_ROOT}/search/issues?q={urllib.parse.quote(query)}", token)
    if isinstance(result, dict):
        return [item["html_url"] for item in result.get("items", [])]
    return []


def fetch_labeled_issues(owner: str, repo: str, label: str, token: str | None) -> list[str]:
    """Return `owner/repo#N` refs for open issues in a repo carrying `label`."""
    refs = []
    page = 1
    while True:
        url = (
            f"{API_ROOT}/repos/{owner}/{repo}/issues?state=open"
            f"&labels={urllib.parse.quote(label)}&per_page=100&page={page}"
        )
        result = _get(url, token)
        if not result:
            break
        for item in result:
            if "pull_request" in item:
                continue  # the issues endpoint also returns PRs; skip them
            refs.append(f"{owner}/{repo}#{item['number']}")
        if len(result) < 100:
            break
        page += 1
    return refs


def check_one(ref: str, token: str | None) -> Verdict:
    v = Verdict(ref=ref)
    try:
        github_ref = resolve_ref(ref)
    except (ValueError, urllib.error.URLError, concurrent.futures.TimeoutError) as e:
        v.verdict = "BAD_REF"
        v.notes.append(f"Couldn't resolve Opire URL: {e}")
        return v

    try:
        owner, repo, number = parse_ref(github_ref)
    except ValueError as e:
        v.verdict = "BAD_REF"
        v.notes.append(str(e))
        return v

    if github_ref != ref:
        v.notes.append(f"Resolved from Opire listing to {github_ref}")

    try:
        return _check_one_inner(v, owner, repo, number, token)
    except (RuntimeError, urllib.error.URLError) as e:
        # A single bad ref (rate limit, network blip, unexpected API error)
        # shouldn't take down the rest of the batch.
        v.verdict = "ERROR"
        v.notes.append(str(e))
        return v


def _check_one_inner(v: Verdict, owner: str, repo: str, number: int, token: str | None) -> Verdict:
    v.url = f"https://github.com/{owner}/{repo}/issues/{number}"

    issue = _get(f"{API_ROOT}/repos/{owner}/{repo}/issues/{number}", token)
    if issue is None:
        v.verdict = "NOT_FOUND"
        v.notes.append("Issue not found (deleted, or repo/issue number wrong).")
        return v

    v.title = issue.get("title", "")

    repo_info = _get(f"{API_ROOT}/repos/{owner}/{repo}", token)
    if repo_info is None:
        v.verdict = "NOT_FOUND"
        v.notes.append("Repo not found (deleted or renamed).")
        return v

    if repo_info.get("archived"):
        v.verdict = "ARCHIVED_REPO"
        v.notes.append(
            "Repo is archived - a PR can't be merged here even if the issue looks open."
        )
        return v

    if issue.get("state") == "closed":
        v.verdict = "CLOSED"
        v.notes.append("Issue is already closed - the bounty is very likely already claimed.")
        return v

    open_prs = find_linked_open_prs(owner, repo, number, token)
    if open_prs:
        v.verdict = "HAS_OPEN_PR"
        v.notes.append(
            f"{len(open_prs)} open PR(s) already reference this issue - someone's ahead of you: "
            + ", ".join(open_prs[:3])
        )
        return v

    v.verdict = "OPEN_CLAIMABLE"
    pushed_at = repo_info.get("pushed_at")
    if pushed_at and _days_since(pushed_at) > STALE_DAYS:
        v.notes.append(
            f"Repo hasn't been pushed to in over {STALE_DAYS // 365} years - "
            "maintainers may be slow or gone even though the repo isn't archived."
        )
    return v


def main(argv: list[str] | None = None) -> int:
    # Issue/PR titles are arbitrary Unicode (CJK, emoji, etc.) but Windows
    # consoles default stdout to the system codepage (e.g. cp1252), which
    # raises UnicodeEncodeError on anything outside it. Force UTF-8 with a
    # safe fallback so real-world titles never crash the whole run.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "refs", nargs="*", help="GitHub issue URL(s), IssueHunt URL(s), or owner/repo#N"
    )
    parser.add_argument(
        "--repo",
        metavar="OWNER/REPO",
        help="Scan every open issue in a repo carrying --label, instead of listing refs one by one",
    )
    parser.add_argument(
        "--label",
        default="bounty",
        help="Label to filter on when using --repo (default: bounty)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("GITHUB_TOKEN"),
        help="GitHub token (or set GITHUB_TOKEN) to raise the API rate limit",
    )
    parser.add_argument("--json", action="store_true", help="Output JSON instead of a table")
    args = parser.parse_args(argv)

    refs = list(args.refs)
    if args.repo:
        try:
            owner, repo = args.repo.split("/", 1)
        except ValueError:
            parser.error("--repo must be in the form owner/repo")
        refs += fetch_labeled_issues(owner, repo, args.label, args.token)

    if not refs:
        parser.error("Provide at least one ref, or --repo owner/repo to scan a whole repo")

    results = [check_one(ref, args.token) for ref in refs]

    if args.json:
        print(json.dumps([vars(r) for r in results], indent=2))
    else:
        for r in results:
            print(f"{r.ref}")
            print(f"  verdict: {r.verdict}")
            if r.title:
                print(f"  title:   {r.title}")
            for note in r.notes:
                print(f"  note:    {note}")
            print()

    claimable = sum(1 for r in results if r.verdict == "OPEN_CLAIMABLE")
    print(f"{claimable}/{len(results)} actually open and claimable.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
