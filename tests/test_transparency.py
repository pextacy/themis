import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from themis.cli_commands import receipts as receipts_cli
from themis.config import RepoPolicy, Scoring
from themis.models import (
    Check,
    CheckStatus,
    Claim,
    ClaimType,
    OtariCall,
    Receipt,
    Role,
    ShieldStatus,
    Strength,
    SubmissionKind,
    SubmissionRef,
    VerdictValue,
)
from themis.verdict import replay, scoring, transparency

CLAIM = Claim(id="c1", type=ClaimType.SYMBOL_EXISTS, text="uses parse_range", normalized_target="parse_range")
FIXED = datetime(2026, 11, 6, tzinfo=UTC)


def receipt(
    checks: list[Check],
    *,
    shield: dict | None = None,
    calls: list[OtariCall] | None = None,
    deferred: bool = False,
    stored: VerdictValue | None = None,
) -> Receipt:
    status = ShieldStatus(shield["status"]) if shield else ShieldStatus.CLEAN
    verdict = scoring.decide(status, [CLAIM], checks, Scoring(), budget_deferred=deferred)
    if stored:
        verdict = verdict.model_copy(update={"value": stored})
    return Receipt(
        submission=SubmissionRef(repo="a/b", number=1, kind=SubmissionKind.BUG_REPORT),
        verdict=verdict, claims=[CLAIM], checks=checks, otari_calls=calls or [],
        shield=shield if shield is not None else {"status": "CLEAN"}, themis_version="0.1.0",
    )  # fmt: skip


def write(item: Receipt, path: Path) -> Path:
    path.write_text(item.model_dump_json(by_alias=True))
    return path


def chain_of(tmp_path: Path, count: int = 3) -> Path:
    chain = tmp_path / "chain.jsonl"
    for number in range(count):
        file = tmp_path / f"themis-a-b-{number}.json"
        file.write_text(f'{{"n": {number}}}')
        transparency.append(file, f"a-b-{number}", "EVIDENCED", chain=chain, now=FIXED)
    return chain


def test_a_chain_links_each_entry_to_the_one_before(tmp_path: Path) -> None:
    chain = chain_of(tmp_path)
    rows = transparency.entries(chain)
    assert [row.index for row in rows] == [0, 1, 2]
    assert rows[0].prev_hash == transparency.GENESIS and rows[2].prev_hash == rows[1].entry_sha256
    assert transparency.verify(chain) is None
    assert transparency.head(chain) == rows[2].entry_sha256


def test_changing_an_old_verdict_is_found(tmp_path: Path) -> None:
    chain = chain_of(tmp_path)
    lines = chain.read_text().splitlines()
    forged = json.loads(lines[1]) | {"verdict": "CONTRADICTED"}
    chain.write_text("\n".join([lines[0], json.dumps(forged), lines[2]]) + "\n")
    assert transparency.verify(chain) == 1


def test_a_consistent_rewrite_of_one_entry_breaks_the_next_link(tmp_path: Path) -> None:
    chain = chain_of(tmp_path)
    rows = transparency.entries(chain)
    rows[1].verdict = "TAINTED"
    rows[1].entry_sha256 = transparency._digest(rows[1])
    chain.write_text("".join(row.model_dump_json() + "\n" for row in rows))
    assert transparency.verify(chain) == 2


def test_dropping_an_entry_and_garbage_are_found(tmp_path: Path) -> None:
    chain = chain_of(tmp_path)
    lines = chain.read_text().splitlines()
    chain.write_text("\n".join([lines[0], lines[2]]) + "\n")
    assert transparency.verify(chain) == 1
    chain.write_text("not json\n")
    assert transparency.verify(chain) == 0


def test_a_broken_tail_refuses_to_extend(tmp_path: Path) -> None:
    chain = tmp_path / "chain.jsonl"
    chain.write_text("not json\n")
    file = tmp_path / "r.json"
    file.write_text("{}")
    with pytest.raises(ValueError, match="cannot be read"):
        transparency.append(file, "a", "EVIDENCED", chain=chain)


def test_an_edited_or_missing_receipt_file_is_reported(tmp_path: Path) -> None:
    chain = chain_of(tmp_path, 2)
    (tmp_path / "themis-a-b-0.json").write_text('{"n": 99}')
    (tmp_path / "themis-a-b-1.json").unlink()
    problems = transparency.verify_receipts(chain, tmp_path)
    assert {item["problem"] for item in problems} == {"receipt file changed", "receipt file missing"}


def test_a_rerun_only_checks_the_newest_entry(tmp_path: Path) -> None:
    chain = tmp_path / "chain.jsonl"
    file = tmp_path / "themis-a-b-1.json"
    file.write_text("one")
    transparency.append(file, "a-b-1", "PLAUSIBLE", chain=chain)
    file.write_text("two")
    transparency.append(file, "a-b-1", "EVIDENCED", chain=chain)
    assert transparency.verify_receipts(chain, tmp_path) == []


def test_secrets_never_enter_the_chain(tmp_path: Path) -> None:
    file = tmp_path / "r.json"
    file.write_text("{}")
    entry = transparency.append(file, "ghp_abcdefghijklmnop1234", "EVIDENCED", chain=tmp_path / "c.jsonl")
    assert "ghp_" not in entry.submission_id


def test_replay_matches_a_genuine_receipt() -> None:
    confirmed = [Check(claim_id="c1", module="symbols", status=CheckStatus.CONFIRMED)]
    result = replay.replay(receipt(confirmed))
    assert result.matches and result.value is VerdictValue.PLAUSIBLE


def test_replay_names_what_the_receipt_does_not_hold() -> None:
    result = replay.replay(receipt([]))
    assert any("plausible_threshold" in note and "0.6" in note and "default" in note for note in result.assumptions)


def test_replay_finds_a_verdict_upgraded_by_hand() -> None:
    hard = [Check(claim_id="c1", module="symbols", status=CheckStatus.CONTRADICTED, strength=Strength.HARD)]
    result = replay.replay(receipt(hard, stored=VerdictValue.EVIDENCED))
    assert not result.matches and result.value is VerdictValue.CONTRADICTED


def test_replay_finds_a_check_forged_after_the_verdict() -> None:
    confirmed = [Check(claim_id="c1", module="symbols", status=CheckStatus.CONFIRMED)]
    item = receipt(confirmed)
    item.checks[0].status = CheckStatus.CONTRADICTED
    item.checks[0].strength = Strength.HARD
    assert not replay.replay(item).matches


def test_replay_keeps_a_tainted_verdict() -> None:
    result = replay.replay(receipt([], shield={"status": "TAINTED"}))
    assert result.matches and result.value is VerdictValue.TAINTED


def test_replay_uses_the_given_threshold() -> None:
    confirmed = [Check(claim_id="c1", module="symbols", status=CheckStatus.CONFIRMED)]
    policy = RepoPolicy.model_validate({"scoring": {"plausible_threshold": 0.9}})
    note = replay.replay(receipt(confirmed), policy).assumptions[0]
    assert "0.9" in note and "given policy" in note


def test_replay_reads_a_refusal_from_the_calls() -> None:
    call = OtariCall(
        request_id="r", role=Role.PROVER, endpoint="/chat", status=403, outcome="budget_exceeded", latency_ms=1
    )
    result = replay.replay(receipt([], calls=[call], deferred=True))
    assert result.matches and not any("not independently checked" in note for note in result.assumptions)


def test_an_arbiter_refusal_does_not_explain_a_deferral() -> None:
    call = OtariCall(
        request_id="r", role=Role.ARBITER, endpoint="/chat", status=403, outcome="budget_exceeded", latency_ms=1
    )
    confirmed = [Check(claim_id="c1", module="symbols", status=CheckStatus.CONFIRMED)]
    assert replay.replay(receipt(confirmed, calls=[call])).value is VerdictValue.PLAUSIBLE


def test_replay_says_when_a_deferral_cannot_be_checked() -> None:
    result = replay.replay(receipt([], deferred=True))
    assert result.matches and any("not independently checked" in note for note in result.assumptions)


def test_replay_notes_a_missing_shield_status() -> None:
    item = receipt([])
    item.shield = {}
    assert any("no shield status" in note for note in replay.replay(item).assumptions)


def cli() -> typer.Typer:
    app = typer.Typer()
    receipts_cli.register(app)
    return app


def test_cli_replay_exit_codes(tmp_path: Path) -> None:
    runner = CliRunner()
    good = write(receipt([Check(claim_id="c1", module="symbols", status=CheckStatus.CONFIRMED)]), tmp_path / "g.json")
    assert runner.invoke(cli(), ["replay", str(good)]).exit_code == 0
    hard = [Check(claim_id="c1", module="symbols", status=CheckStatus.CONTRADICTED, strength=Strength.HARD)]
    bad = write(receipt(hard, stored=VerdictValue.EVIDENCED), tmp_path / "b.json")
    assert runner.invoke(cli(), ["replay", str(bad)]).exit_code == 1
    (tmp_path / "x.json").write_text("{}")
    assert runner.invoke(cli(), ["replay", str(tmp_path / "x.json")]).exit_code == 2


def test_cli_verify(tmp_path: Path) -> None:
    runner = CliRunner()
    chain = chain_of(tmp_path)
    args = ["receipts", "verify", "--chain", str(chain), "--directory", str(tmp_path)]
    done = runner.invoke(cli(), args)
    assert done.exit_code == 0 and "3 entries" in done.output
    assert runner.invoke(cli(), [*args, "--expect-head", "0" * 64]).exit_code == 1
    chain.write_text("junk\n")
    assert runner.invoke(cli(), args).exit_code == 1
