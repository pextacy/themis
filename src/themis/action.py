"""GitHub Action entry point. This process holds secrets, so it reads PRs through the API only."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from themis.config import Settings
from themis.intake.github import GitHubClient, GitHubError, Target
from themis.logs import get_logger
from themis.models import VerdictValue
from themis.pipeline import KindDisabled, remote_policy, run_url
from themis.report import appeals, commands, github_comment, publish
from themis.store import history
from themis.verdict.scoring import labels_for

log = get_logger(__name__)
PROJECT_BUDGET_KEY = "gh:0"  # Approved rechecks bill the project, not the contributor.
ANALYZED_ACTIONS = ("opened", "edited")


def _target(repo: str, number: int) -> Target:
    owner, name = repo.split("/", 1)
    return Target(owner, name, number)


async def apply_override(
    github: GitHubClient, repo: str, number: int, command: commands.Command, login: str, prefix: str
) -> None:
    """Adds to the ledger, so what THEMIS found stays readable under the human decision."""
    value = command.verdict or VerdictValue.NEEDS_EVIDENCE
    existing = await publish.find_ledger(github, repo, number)
    note = (
        f"\n**Maintainer override:** {github_comment.inert(login, 40)} set "
        f"{value.value}. Reason: {github_comment.inert(command.reason, 300)}\n"
    )
    if existing is None:
        await github.post(f"/repos/{repo}/issues/{number}/comments", {"body": f"{github_comment.MARKER}{note}"})
    else:
        rows = await github.issue_comments(repo, number)
        body = next(str(row["body"]) for row in rows if row["id"] == existing)
        await github.patch(f"/repos/{repo}/issues/comments/{existing}", {"body": body + note})
    await publish.sync_labels(github, repo, number, labels_for(value, [], prefix), prefix, keep=publish.OVERRIDE_KEEPS)


async def handle_comment(settings: Settings, event: dict[str, Any], repo: str) -> None:
    comment, issue = event["comment"], event["issue"]
    if (comment.get("user") or {}).get("type") == "Bot":
        return
    number = int(issue["number"])
    if await appeals.handle(settings, event, repo):
        return
    async with GitHubClient(settings) as github:
        try:
            command = commands.parse(str(comment.get("body") or ""))
            if command is None:
                return
            policy = await remote_policy(github, repo)
            login = str((comment.get("user") or {}).get("login") or "")
            maintainer = commands.is_maintainer(comment.get("author_association"), login, policy)
            is_author = (comment.get("user") or {}).get("id") == (issue.get("user") or {}).get("id")
            commands.authorize(command, maintainer=maintainer, is_author=is_author)
        except commands.CommandRefused as error:
            await github.post(f"/repos/{repo}/issues/{number}/comments", {"body": f"THEMIS: {error}."})
            return
        if command.name == "override":
            await apply_override(github, repo, number, command, login, policy.labels.prefix)
            value = (command.verdict or VerdictValue.NEEDS_EVIDENCE).value
            history.record_override(settings, f"{repo.replace('/', '-')}-{number}", login, value, command.reason)
            return
        item = await github.fetch(_target(repo, number))
    if command.approve:
        item = item.model_copy(update={"contributor_key": PROJECT_BUDGET_KEY})
    outcome = await run_url(
        settings, _target(repo, number), post=command.name == "recheck", submission=item, force=True
    )
    if command.name == "explain":
        async with GitHubClient(settings) as github:
            body = github_comment.render_explain(outcome.checks)
            await github.post(f"/repos/{repo}/issues/{number}/comments", {"body": body})


async def handle_workflow_run(
    settings: Settings, event: dict[str, Any], repo: str, test_results: dict[str, Any] | None
) -> None:
    run = event["workflow_run"]
    pulls = run.get("pull_requests") or []
    number = pulls[0]["number"] if pulls else None
    async with GitHubClient(settings) as github:
        number = number or await github.pull_for_commit(repo, str(run.get("head_sha")))
    if number is None:
        log.warning("action.no_pull_request", stage="intake", head_sha=run.get("head_sha"))
        return
    await run_url(settings, _target(repo, int(number)), test_results=test_results, automatic=True)


def load_results(path: Path | None) -> dict[str, Any] | None:
    """A file a PR's code wrote. It is parsed as JSON and never trusted beyond the schema."""
    if path is None or not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


async def dispatch(settings: Settings, name: str, event: dict[str, Any], repo: str, results: Path | None) -> None:
    try:
        if name == "issues" and event.get("action") in ANALYZED_ACTIONS and "pull_request" not in event["issue"]:
            await run_url(settings, _target(repo, int(event["issue"]["number"])), automatic=True)
        elif name == "issue_comment" and event.get("action") == "created":
            await handle_comment(settings, event, repo)
        elif name == "workflow_run" and event.get("action") == "completed":
            await handle_workflow_run(settings, event, repo, load_results(results))
    except KindDisabled as error:
        log.info("action.kind_disabled", submission_id=str(error), stage="action")
    except GitHubError as error:
        log.warning("action.github_failed", stage="action", error=str(error))
        raise
