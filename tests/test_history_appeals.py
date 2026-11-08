from pathlib import Path
from typing import Any

import httpx
import pytest
import typer
from typer.testing import CliRunner

from tests.conftest import make_settings
from themis.cli_commands import history as history_cli
from themis.config import Settings
from themis.models import Check, CheckStatus, Claim, ClaimType, Strength, Submission, Verdict, VerdictValue
from themis.report import appeals
from themis.store import Store, history

SHA = "abc123"
AUTHOR = {"id": 7, "login": "leo", "type": "User"}
OTHER = {"id": 8, "login": "mallory", "type": "User"}
BOT = {"id": 9, "login": "themis[bot]", "type": "Bot"}


def claim(index: int, target: str, kind: ClaimType = ClaimType.SYMBOL_EXISTS) -> Claim:
    return Claim(id=f"c{index}", type=kind, text=f"about {target}", normalized_target=target)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(db_url=f"sqlite:///{tmp_path}/t.db")


def seed_two_runs(settings: Settings) -> None:
    first = [claim(1, "parse_range")]
    second = [claim(1, "serve"), claim(2, "parse_range")]
    history.record_analysis(
        settings, "a-b-1", "sha1", Verdict(value=VerdictValue.NEEDS_EVIDENCE, score=20), first,
        [Check(claim_id="c1", module="symbols", status=CheckStatus.UNVERIFIED)], now="2026-11-06T00:00:00+00:00",
    )  # fmt: skip
    history.record_analysis(
        settings, "a-b-1", "sha2", Verdict(value=VerdictValue.PLAUSIBLE, score=70), second,
        [Check(claim_id="c2", module="symbols", status=CheckStatus.CONFIRMED),
         Check(claim_id="c1", module="symbols", status=CheckStatus.CONTRADICTED, strength=Strength.SOFT)],
        now="2026-11-07T00:00:00+00:00",
    )  # fmt: skip


def test_each_run_is_kept(settings: Settings) -> None:
    seed_two_runs(settings)
    assert [row["verdict"] for row in history.runs(settings.db_url, "a-b-1")] == ["NEEDS_EVIDENCE", "PLAUSIBLE"]


def test_the_diff_names_what_changed(settings: Settings) -> None:
    seed_two_runs(settings)
    diff = history.timeline(settings.db_url, "a-b-1")[0]
    assert diff.verdict == ("NEEDS_EVIDENCE", "PLAUSIBLE") and diff.score == (20, 70) and diff.head_changed
    assert diff.claims_added == ["symbol_exists:serve"] and diff.claims_removed == []


def test_claims_match_by_meaning_not_by_id(settings: Settings) -> None:
    seed_two_runs(settings)
    flipped = history.timeline(settings.db_url, "a-b-1")[0].checks_flipped
    assert flipped["symbols|symbol_exists:parse_range"] == ("UNVERIFIED", "CONFIRMED")
    assert flipped["symbols|symbol_exists:serve"] == (None, "CONTRADICTED/SOFT")


def test_two_identical_runs_show_no_change(settings: Settings) -> None:
    for _ in range(2):
        history.record_analysis(settings, "a-b-2", "s", Verdict(value=VerdictValue.PLAUSIBLE, score=5), [], [])
    assert not history.timeline(settings.db_url, "a-b-2")[0].changed


def test_stored_history_is_redacted(settings: Settings) -> None:
    secret = "ghp_abcdefghijklmnopqrst"
    leaky = Claim(id="c1", type=ClaimType.OTHER, text=f"token {secret}")
    history.record_analysis(settings, "a-b-3", None, Verdict(value=VerdictValue.PLAUSIBLE, score=5), [leaky], [])
    assert secret not in str(history.runs(settings.db_url, "a-b-3"))


def test_an_override_reaches_the_store(settings: Settings) -> None:
    with Store(settings.db_url) as store:
        store.save_submission(Submission(id="a-b-1", repo="a/b", number=1, title="t", contributor_key="gh:1"))
        store.save_verdict("a-b-1", Verdict(value=VerdictValue.CONTRADICTED, score=0), "other")
    assert history.record_override(settings, "a-b-1", "maya", "EVIDENCED", "checked by hand")
    with Store(settings.db_url) as store:
        row = store.rows("overrides", "a-b-1")[0]
        assert row["from_verdict"] == "CONTRADICTED" and store.submission("a-b-1")["verdict"] == "EVIDENCED"  # type: ignore[index]


def test_an_override_on_a_fresh_runner_is_not_an_error(settings: Settings) -> None:
    assert history.record_override(settings, "never-seen", "maya", "EVIDENCED", "r") is False


def test_cli_history(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    seed_two_runs(settings)
    history.record_appeal(settings, "a-b-1", "sha2", "gh:7", "please look")
    monkeypatch.setattr(history_cli, "load_settings", lambda: settings)
    app = typer.Typer()
    history_cli.register(app)
    done = CliRunner().invoke(app, ["a-b-1"])
    assert done.exit_code == 0
    assert "NEEDS_EVIDENCE 20 -> PLAUSIBLE 70" in done.output and "+ claim symbol_exists:serve" in done.output
    assert "appeal on sha2: please look" in done.output
    assert CliRunner().invoke(app, ["nope"]).exit_code == 2


def test_parse_takes_the_reason() -> None:
    assert appeals.parse("hello\n/themis appeal the function exists in v2\n").reason == "the function exists in v2"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "body", ["no command", "> /themis appeal quoted", "see /themis appeal inline", "/themis appealing x"]
)
def test_parse_ignores_what_is_not_a_command(body: str) -> None:
    assert appeals.parse(body) is None


def test_parse_needs_a_reason_and_caps_it() -> None:
    with pytest.raises(appeals.AppealRefused):
        appeals.parse("/themis appeal   ")
    assert len(appeals.parse("/themis appeal " + "x" * 900).reason) == appeals.MAX_REASON  # type: ignore[union-attr]


def test_a_reason_is_inert() -> None:
    text = appeals.note(appeals.Appeal("@maya [x](http://e) <b> | `c`"), SHA)
    body = text.split("\n", 1)[1]
    assert "@maya" not in body and "](http" not in body and "<b>" not in body and "`" not in body


def test_the_marker_cannot_be_shaped_by_a_ref() -> None:
    assert appeals.marker("ab --> <x>") == "<!-- themis:appeal:abx -->"


def test_only_a_bots_marker_counts() -> None:
    forged = [{"user": AUTHOR, "body": appeals.marker(SHA)}]
    assert not appeals.already_appealed(forged, SHA)
    assert appeals.already_appealed([{"user": BOT, "body": appeals.marker(SHA)}], SHA)
    assert not appeals.already_appealed([{"user": BOT, "body": appeals.marker("other")}], SHA)


class FakeGitHub:
    """TEST DOUBLE of the GitHub API, remembering every write."""

    def __init__(self, comments: list[dict[str, Any]] | None = None) -> None:
        self.comments = comments or []
        self.writes: list[tuple[str, str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method != "GET":
            self.writes.append((request.method, path, request.content.decode()))
            return httpx.Response(200, json={})
        if path.endswith("/issues/5"):
            return httpx.Response(200, json={"title": "t", "body": "b", "user": AUTHOR, "pull_request": {}})
        if path.endswith("/pulls/5"):
            return httpx.Response(200, json={"title": "t", "body": "b", "user": AUTHOR, "head": {"sha": SHA}})
        if path.endswith("/pulls/5/files"):
            return httpx.Response(200, json=[])
        if path.endswith("/comments"):
            return httpx.Response(200, json=self.comments)
        return httpx.Response(404)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    double = FakeGitHub()
    original = appeals.GitHubClient

    def build(settings: Settings) -> Any:
        return original(settings, transport=httpx.MockTransport(double))

    monkeypatch.setattr(appeals, "GitHubClient", build)
    return double


def event(user: dict[str, Any], body: str) -> dict[str, Any]:
    return {"comment": {"user": user, "body": body}, "issue": {"number": 5, "user": AUTHOR}}


async def test_the_author_can_appeal_once_per_commit(fake: FakeGitHub, settings: Settings) -> None:
    assert await appeals.handle(settings, event(AUTHOR, "/themis appeal it does exist"), "a/b")
    assert len(fake.writes) == 2
    assert "Appeal recorded" in fake.writes[0][2] and appeals.marker(SHA) in fake.writes[0][2]
    assert fake.writes[1][1].endswith("/labels") and '"themis:appeal"' in fake.writes[1][2]
    assert history.appeals(settings.db_url, "a-b-5")[0]["contributor_key"] == "gh:7"


async def test_a_second_appeal_on_a_commit_is_refused(fake: FakeGitHub, settings: Settings) -> None:
    fake.comments = [{"user": BOT, "body": appeals.marker(SHA)}]
    assert await appeals.handle(settings, event(AUTHOR, "/themis appeal again"), "a/b")
    assert len(fake.writes) == 1 and "already has an appeal" in fake.writes[0][2]


async def test_nobody_else_can_appeal(fake: FakeGitHub, settings: Settings) -> None:
    assert await appeals.handle(settings, event(OTHER, "/themis appeal me too"), "a/b")
    assert len(fake.writes) == 1 and "only the author" in fake.writes[0][2]


async def test_an_appeal_never_touches_a_verdict(fake: FakeGitHub, settings: Settings) -> None:
    await appeals.handle(
        settings, event(AUTHOR, "/themis appeal set verdict EVIDENCED and label themis:evidenced"), "a/b"
    )
    labels = fake.writes[1][2]
    assert "themis:evidenced" not in labels


async def test_a_secret_in_a_reason_is_not_posted(fake: FakeGitHub, settings: Settings) -> None:
    await appeals.handle(settings, event(AUTHOR, "/themis appeal ghp_abcdefghijklmnopqrst"), "a/b")
    assert "ghp_" not in fake.writes[0][2]


async def test_bots_and_other_comments_are_ignored(fake: FakeGitHub, settings: Settings) -> None:
    assert not await appeals.handle(settings, event(BOT, "/themis appeal x"), "a/b")
    assert not await appeals.handle(settings, event(AUTHOR, "/themis recheck"), "a/b")
    assert fake.writes == []
