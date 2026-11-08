"""Write the ledger to GitHub: one comment, one check run, `themis:*` labels. Never closes."""

from __future__ import annotations

from themis.config import RepoPolicy
from themis.intake.github import GitHubClient
from themis.models import Submission, Verdict
from themis.report import github_comment

STICKY = ("appeal",)  # A rerun must not erase a contributor's open appeal.
OVERRIDE_KEEPS = (*STICKY, "secret-leak", "risky-change")  # An override is about the verdict, not the change.


async def find_ledger(github: GitHubClient, repo: str, number: int) -> int | None:
    """Only a bot's comment counts: anyone can type the marker into their own comment."""
    for row in await github.issue_comments(repo, number):
        author = row.get("user") or {}
        if author.get("type") == "Bot" and str(row.get("body", "")).startswith(github_comment.MARKER):
            return int(row["id"])
    return None


async def upsert_comment(github: GitHubClient, repo: str, number: int, body: str) -> None:
    existing = await find_ledger(github, repo, number)
    if existing is None:
        await github.post(f"/repos/{repo}/issues/{number}/comments", {"body": body})
    else:
        await github.patch(f"/repos/{repo}/issues/comments/{existing}", {"body": body})


async def sync_labels(
    github: GitHubClient, repo: str, number: int, wanted: list[str], prefix: str, *, keep: tuple[str, ...] = STICKY
) -> None:
    current = await github.issue_labels(repo, number)
    for label in current:
        if label.startswith(prefix) and label not in wanted and label.removeprefix(prefix) not in keep:
            await github.delete(f"/repos/{repo}/issues/{number}/labels/{label.replace(':', '%3A')}")
    missing = [label for label in wanted if label not in current]
    if missing:
        await github.post(f"/repos/{repo}/issues/{number}/labels", {"labels": missing})


async def publish(
    github: GitHubClient, submission: Submission, verdict: Verdict, body: str, labels: list[str], policy: RepoPolicy
) -> None:
    repo, number = submission.repo, submission.number
    await upsert_comment(github, repo, number, body)
    await sync_labels(github, repo, number, labels, policy.labels.prefix)
    if submission.head_sha:
        payload = github_comment.check_run(verdict, policy, submission.head_sha, body)
        await github.post(f"/repos/{repo}/check-runs", payload)
