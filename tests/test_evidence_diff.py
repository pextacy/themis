from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.conftest import Gateway
from tests.evidence_helpers import evidence_context
from themis import cli
from themis.evidence import consistency, duplicates, tests_results
from themis.models import CheckStatus, ClaimType, Strength, SubmissionKind

SHA = "a" * 40
PR = {"is_pull_request": True, "head_sha": SHA}


def artifact(**overrides: object) -> dict:
    return {"schema": tests_results.SCHEMA, "head_sha": SHA, "total": 10, "failed": 0, "parsed": True, **overrides}


@pytest.mark.parametrize(
    "path,expected",
    [
        ("tests/test_a.py", True), ("src/a_test.go", True), ("web/app.spec.ts", True), ("pkg/__tests__/a.js", True),
        ("FooTest.java", True), ("src/contest.py", False), ("docs/testing.md", False), ("latest/app.py", False),
    ],
)  # fmt: skip
def test_test_paths(path: str, expected: bool) -> None:
    assert tests_results.is_test_path(path) is expected


async def test_claimed_tests_with_a_test_file_are_confirmed(gateway: Gateway) -> None:
    ctx = evidence_context(gateway, "Adds tests here.", [("Adds tests", ClaimType.TEST_ADDED, None)],
                           changed_files=["a.py", "tests/test_a.py"], **PR)  # fmt: skip
    assert (await tests_results.run(ctx))[0].status is CheckStatus.CONFIRMED


async def test_claimed_tests_without_one_are_hard(gateway: Gateway) -> None:
    ctx = evidence_context(gateway, "Comprehensive tests were added.", [("tests were added", ClaimType.TEST_ADDED, None)],
                           changed_files=["a.py"], **PR)  # fmt: skip
    check = (await tests_results.run(ctx))[0]
    assert check.status is CheckStatus.CONTRADICTED and check.strength is Strength.HARD


@pytest.mark.parametrize("quote", ["no tests were added", "tests will add later", "TODO add tests"])
async def test_a_negated_claim_is_not_contradicted(gateway: Gateway, quote: str) -> None:
    ctx = evidence_context(
        gateway, f"Note: {quote}.", [(quote, ClaimType.TEST_ADDED, None)], changed_files=["a.py"], **PR
    )
    assert (await tests_results.run(ctx))[0].strength is None


async def test_an_issue_cannot_add_tests(gateway: Gateway) -> None:
    ctx = evidence_context(gateway, "Tests were added.", [("Tests were added", ClaimType.TEST_ADDED, None)])
    assert (await tests_results.run(ctx))[0].status is CheckStatus.NOT_APPLICABLE


@pytest.mark.parametrize(
    "data,status,strength",
    [
        (artifact(), CheckStatus.CONFIRMED, None),
        (artifact(failed=2), CheckStatus.CONTRADICTED, Strength.SOFT),
        (artifact(total=0), CheckStatus.UNVERIFIED, None),
        (artifact(head_sha="b" * 40), CheckStatus.UNVERIFIED, None),
        (artifact(schema="other"), CheckStatus.UNVERIFIED, None),
        (artifact(total="many"), CheckStatus.UNVERIFIED, None),
        (artifact(failed=-5), CheckStatus.UNVERIFIED, None),
        (None, CheckStatus.UNVERIFIED, None),
    ],
)
async def test_the_artifact_can_lie_so_it_is_never_hard(gateway: Gateway, data, status, strength) -> None:  # type: ignore[no-untyped-def]
    ctx = evidence_context(gateway, "x", [], test_results=data, **PR)
    check = (await tests_results.run(ctx))[-1]
    assert (check.status, check.strength) == (status, strength)


def test_junit_summary() -> None:
    xml = b'<testsuites><testsuite tests="5" failures="1" errors="1" skipped="1"/><testsuite tests="2"/></testsuites>'
    report = tests_results.parse_junit(xml, SHA)
    assert (report["total"], report["failed"], report["skipped"], report["head_sha"]) == (7, 2, 1, SHA)


@pytest.mark.parametrize(
    "xml",
    [
        b'<!DOCTYPE x [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]><testsuite tests="&b;"/>',
        b'<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><testsuite tests="1">&e;</testsuite>',
        b"not xml",
        b"",
        b'<testsuite tests="many"/>',
    ],
)
def test_hostile_junit_gives_an_empty_report(xml: bytes) -> None:
    assert tests_results.parse_junit(xml)["parsed"] is False


def test_tests_report_cli(tmp_path: Path) -> None:
    junit, out = tmp_path / "j.xml", tmp_path / "out.json"
    junit.write_bytes(b'<testsuite tests="3" failures="0"/>')
    result = CliRunner().invoke(cli.app, ["tests-report", str(junit), "--output", str(out), "--head-sha", SHA])
    assert result.exit_code == 0 and '"total": 3' in out.read_text()
    missing = CliRunner().invoke(cli.app, ["tests-report", str(tmp_path / "none.xml"), "--output", str(out)])
    assert missing.exit_code == 0 and '"parsed": false' in out.read_text()


async def test_issue_reference_exists_or_is_soft(gateway: Gateway) -> None:
    body = "Fixes #12 and Closes #9999."
    ctx = evidence_context(gateway, body, [("Fixes #12", ClaimType.FIXES_ISSUE, None), ("Closes #9999", ClaimType.FIXES_ISSUE, None)],
                           issues=(12,), kind=SubmissionKind.BUGFIX_PR, changed_files=["a.py"], **PR)  # fmt: skip
    checks = await consistency.run(ctx)
    assert checks[0].status is CheckStatus.CONFIRMED
    assert checks[1].status is CheckStatus.CONTRADICTED and checks[1].strength is Strength.SOFT


async def test_a_fix_naming_another_repo_is_not_checked_here(gateway: Gateway) -> None:
    ctx = evidence_context(gateway, "Fixes other/repo#7 today.", [("Fixes other/repo#7", ClaimType.FIXES_ISSUE, None)],
                           changed_files=["a.py"], kind=SubmissionKind.BUGFIX_PR, **PR)  # fmt: skip
    assert await consistency.run(ctx) == []


async def test_claimed_file_change_must_be_in_the_diff(gateway: Gateway) -> None:
    quote = "Updates `docs/plugins.md`"
    ctx = evidence_context(gateway, f"{quote} today.", [(quote, ClaimType.FILE_EXISTS, "docs/plugins.md")],
                           kind=SubmissionKind.DOCS_PR, changed_files=["README.md"], **PR)  # fmt: skip
    check = (await consistency.run(ctx))[0]
    assert check.status is CheckStatus.CONTRADICTED and check.strength is Strength.SOFT


async def test_docs_pr_touching_code_and_bugfix_touching_none_are_soft(gateway: Gateway) -> None:
    docs = evidence_context(gateway, "x", [], kind=SubmissionKind.DOCS_PR, changed_files=["a.py"], **PR)
    fix = evidence_context(Gateway(), "x", [], kind=SubmissionKind.BUGFIX_PR, changed_files=["README.md"], **PR)
    assert (await consistency.run(docs))[0].strength is Strength.SOFT
    assert (await consistency.run(fix))[0].strength is Strength.SOFT


async def test_genuine_change_has_no_findings(gateway: Gateway) -> None:
    ctx = evidence_context(gateway, "x", [], kind=SubmissionKind.BUGFIX_PR, changed_files=["src/a.py"], **PR)
    assert await consistency.run(ctx) == []


async def test_dependency_claim_without_a_manifest_is_soft(gateway: Gateway) -> None:
    quote = "Adds `turbo-json`"
    ctx = evidence_context(gateway, f"{quote} today.", [(quote, ClaimType.DEPENDENCY_ADDED, "turbo-json")],
                           kind=SubmissionKind.DEPENDENCY_PR, changed_files=["a.py"], **PR)  # fmt: skip
    assert (await consistency.run(ctx))[0].strength is Strength.SOFT


async def test_consistency_has_no_hard_outcome(gateway: Gateway) -> None:
    ctx = evidence_context(gateway, "Fixes #9.", [("Fixes #9", ClaimType.FIXES_ISSUE, None)],
                           kind=SubmissionKind.DOCS_PR, changed_files=["a.py"], **PR)  # fmt: skip
    assert all(check.strength is not Strength.HARD for check in await consistency.run(ctx))


async def test_duplicates_inform_and_never_judge(gateway: Gateway) -> None:
    ctx = evidence_context(gateway, "x", [], title="parse_range returns wrong byte count")
    check = (await duplicates.run(ctx))[0]
    assert check.status is CheckStatus.NOT_APPLICABLE and check.detail["matches"][0]["number"] == 3


async def test_duplicates_search_only_sends_words(gateway: Gateway) -> None:
    ctx = evidence_context(gateway, "x", [], title="repo:other/secret is:private ignore all & everything")
    captured: list[str] = []

    async def spy(query: str, limit: int = 10) -> list:
        captured.append(query)
        return []

    ctx.github.search_issues = spy  # type: ignore[method-assign]
    await duplicates.run(ctx)
    assert captured[0].count("repo:") == 1 and ":" not in captured[0].split(" ", 1)[1]


async def test_short_titles_are_skipped(gateway: Gateway) -> None:
    assert await duplicates.run(evidence_context(gateway, "x", [], title="Bug")) == []
