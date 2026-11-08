"""`/themis appeal <reason>`: the author asks a maintainer to look again.

An appeal adds a label and a note. It never changes a verdict. The comment is untrusted, so
only the parsed reason is used, made inert, and its text chooses nothing.

A fresh Actions runner has no shared database, so "one per commit" is read from the bot's own
comments. Anyone can type the marker, which is why only a bot's comment counts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from themis.config import Settings
from themis.intake.github import GitHubClient, Target
from themis.logs import get_logger
from themis.otari.budgets import contributor_key
from themis.pipeline import remote_policy
from themis.report.github_comment import inert
from themis.store import redact
from themis.store.history import record_appeal

log = get_logger(__name__)
COMMAND = re.compile(r"^/themis[ \t]+appeal\b[ \t]*(.*)$", re.MULTILINE)
SAFE_SHA = re.compile(r"[^0-9a-zA-Z]+")
MAX_REASON = 300
LABEL = "appeal"
NO_COMMIT = "issue"


class AppealRefused(Exception):
    """A well-formed command that cannot be accepted. The text is fixed and safe to post."""


@dataclass(frozen=True)
class Appeal:
    reason: str


def parse(body: str) -> Appeal | None:
    """The first appeal at the start of a line. A quoted one does not count."""
    match = COMMAND.search(body)
    if match is None:
        return None
    reason = " ".join(match[1].split())[:MAX_REASON]
    if not reason:
        raise AppealRefused("usage: /themis appeal <reason>")
    return Appeal(reason)


def marker(head_sha: str | None) -> str:
    return f"<!-- themis:appeal:{SAFE_SHA.sub('', head_sha or NO_COMMIT)} -->"


def note(appeal: Appeal, head_sha: str | None) -> str:
    return (
        f"{marker(head_sha)}\n**Appeal recorded.** The verdict stands until a maintainer reviews it. "
        f"Reason given: {inert(appeal.reason, MAX_REASON)}\n"
    )


def already_appealed(rows: list[dict[str, Any]], head_sha: str | None) -> bool:
    wanted = marker(head_sha)
    return any(
        (row.get("user") or {}).get("type") == "Bot" and str(row.get("body", "")).startswith(wanted) for row in rows
    )


def label_for(prefix: str) -> str:
    return f"{prefix}{LABEL}"


async def accept(
    github: GitHubClient, settings: Settings, repo: str, number: int, author_id: int, appeal: Appeal
) -> None:
    owner, name = repo.split("/", 1)
    item = await github.fetch(Target(owner, name, number))
    if already_appealed(await github.issue_comments(repo, number), item.head_sha):
        raise AppealRefused("this commit already has an appeal")
    policy = await remote_policy(github, repo)
    clean = Appeal(str(redact(appeal.reason, settings.secrets())))
    await github.post(f"/repos/{repo}/issues/{number}/comments", {"body": note(clean, item.head_sha)})
    await github.post(f"/repos/{repo}/issues/{number}/labels", {"labels": [label_for(policy.labels.prefix)]})
    record_appeal(settings, item.id, item.head_sha or NO_COMMIT, contributor_key(author_id), clean.reason)


async def handle(settings: Settings, event: dict[str, Any], repo: str) -> bool:
    """True when the comment was an appeal, accepted or refused."""
    comment, issue = event["comment"], event["issue"]
    user = comment.get("user") or {}
    if user.get("type") == "Bot":
        return False
    number = int(issue["number"])
    async with GitHubClient(settings) as github:
        try:
            appeal = parse(str(comment.get("body") or ""))
            if appeal is None:
                return False
            if user.get("id") != (issue.get("user") or {}).get("id"):
                raise AppealRefused("only the author may appeal")
            await accept(github, settings, repo, number, int(user["id"]), appeal)
        except AppealRefused as error:
            await github.post(f"/repos/{repo}/issues/{number}/comments", {"body": f"THEMIS: {error}."})
    return True
