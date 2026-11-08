import inspect

import pytest
from hypothesis import given
from hypothesis import strategies as st

from themis.config import Scoring
from themis.models import Check, CheckStatus, Claim, ClaimType, ShieldStatus, Strength, VerdictValue
from themis.verdict import scoring

SCORING = Scoring()
CLAIMS = [Claim(id=f"c{n}", type=ClaimType.OTHER, text="x") for n in range(3)]


def check(
    status: CheckStatus, claim: str | None = "c0", strength: Strength | None = None, module: str = "symbols", **detail
) -> Check:  # type: ignore[no-untyped-def]
    return Check(claim_id=claim, module=module, status=status, strength=strength, detail=detail)


def hard(claim: str = "c0") -> Check:
    return check(CheckStatus.CONTRADICTED, claim, Strength.HARD)


def soft(claim: str = "c0") -> Check:
    return check(CheckStatus.CONTRADICTED, claim, Strength.SOFT)


any_check = st.builds(
    Check,
    claim_id=st.sampled_from(["c0", "c1", "c2", None]),
    module=st.sampled_from(["symbols", "tests", "web", "repro", "dependencies"]),
    status=st.sampled_from(list(CheckStatus)),
    strength=st.sampled_from([Strength.SOFT, Strength.HARD, None]),
)


def is_hard(item: Check) -> bool:
    return item.status is CheckStatus.CONTRADICTED and item.strength is Strength.HARD


@given(st.lists(any_check, max_size=12))
def test_no_hard_contradiction_is_never_contradicted(checks: list[Check]) -> None:
    safe = [item for item in checks if not is_hard(item)]
    assert scoring.decide(ShieldStatus.CLEAN, CLAIMS, safe, SCORING).value is not VerdictValue.CONTRADICTED


@given(st.lists(any_check, max_size=12), st.booleans())
def test_tainted_is_always_tainted(checks: list[Check], deferred: bool) -> None:
    verdict = scoring.decide(ShieldStatus.TAINTED, CLAIMS, checks, SCORING, budget_deferred=deferred)
    assert verdict.value is VerdictValue.TAINTED and verdict.score == 0


@given(st.lists(any_check, max_size=12))
def test_a_hard_contradiction_is_always_contradicted_on_a_clean_submission(checks: list[Check]) -> None:
    verdict = scoring.decide(ShieldStatus.CLEAN, CLAIMS, [*checks, hard()], SCORING)
    assert verdict.value is VerdictValue.CONTRADICTED


@given(st.lists(any_check, max_size=12))
def test_score_stays_in_range_and_is_repeatable(checks: list[Check]) -> None:
    first = scoring.decide(ShieldStatus.CLEAN, CLAIMS, checks, SCORING)
    assert 0 <= first.score <= 100 and first == scoring.decide(ShieldStatus.CLEAN, CLAIMS, checks, SCORING)


def test_identity_is_not_an_input() -> None:
    names = set(inspect.signature(scoring.decide).parameters)
    assert names == {"shield", "claims", "checks", "scoring", "budget_deferred"}


def test_reproduced_is_evidenced() -> None:
    verdict = scoring.decide(ShieldStatus.CLEAN, CLAIMS, [check(CheckStatus.REPRODUCED)], SCORING)
    assert verdict.value is VerdictValue.EVIDENCED


def test_passing_tests_and_confirmed_claims_are_evidenced() -> None:
    checks = [check(CheckStatus.CONFIRMED, c.id) for c in CLAIMS] + [check(CheckStatus.CONFIRMED, None, module="tests")]
    assert scoring.decide(ShieldStatus.CLEAN, CLAIMS, checks, SCORING).value is VerdictValue.EVIDENCED


def test_mostly_confirmed_is_plausible_and_a_soft_contradiction_stops_it() -> None:
    checks = [check(CheckStatus.CONFIRMED, "c0"), check(CheckStatus.CONFIRMED, "c1")]
    assert scoring.decide(ShieldStatus.CLEAN, CLAIMS, checks, SCORING).value is VerdictValue.PLAUSIBLE
    verdict = scoring.decide(ShieldStatus.CLEAN, CLAIMS, [*checks, soft("c2")], SCORING)
    assert verdict.value is VerdictValue.NEEDS_EVIDENCE


def test_a_tests_check_on_a_claim_is_not_a_passing_suite() -> None:
    checks = [check(CheckStatus.CONFIRMED, c.id, module="tests") for c in CLAIMS]
    assert scoring.decide(ShieldStatus.CLEAN, CLAIMS, checks, SCORING).value is VerdictValue.PLAUSIBLE


def test_no_claims_needs_evidence() -> None:
    verdict = scoring.decide(ShieldStatus.CLEAN, [], [], SCORING)
    assert verdict.value is VerdictValue.NEEDS_EVIDENCE and verdict.score == 0


@pytest.mark.parametrize(
    "shield,expected",
    [(ShieldStatus.TAINTED_UNCHECKED, VerdictValue.TAINTED_UNCHECKED), (ShieldStatus.DEFERRED, VerdictValue.DEFERRED)],
)
def test_shield_statuses_map_straight_through(shield: ShieldStatus, expected: VerdictValue) -> None:
    assert scoring.decide(shield, [], [], SCORING).value is expected


def test_budget_never_gives_a_negative_verdict() -> None:
    verdict = scoring.decide(ShieldStatus.CLEAN, CLAIMS, [], SCORING, budget_deferred=True)
    assert verdict.value is VerdictValue.DEFERRED and verdict.score == 0


def test_weights_are_recorded_and_all_unverified_scores_zero() -> None:
    verdict = scoring.decide(ShieldStatus.CLEAN, CLAIMS, [check(CheckStatus.UNVERIFIED)], SCORING)
    assert verdict.score == 0 and verdict.weights["reproduced"] == 1.0


def test_labels_are_a_fixed_table() -> None:
    assert scoring.labels_for(VerdictValue.TAINTED_UNCHECKED, [], "themis:") == ["themis:tainted"]
    risky = check(CheckStatus.CONFIRMED, None, module="dependencies", risk="review")
    assert scoring.labels_for(VerdictValue.PLAUSIBLE, [risky], "x-") == ["x-plausible", "x-dependency-risk"]
