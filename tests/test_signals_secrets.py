"""Every credential below is made up at run time, so no scanner flags this file as a leak."""

from __future__ import annotations

import json

from themis.config import RepoPolicy
from themis.models import Signal, SignalSeverity, Submission
from themis.signals import secrets
from themis.signals.base import SignalContext, make_context
from themis.signals.runner import collect, labels_for, ledger_lines

BODY = "aZ3kP9qL2mX7vB4nT8wR1sD6fG0hJ5cY" + "Q2eU7iO4"
AWS = "AKIA" + "Q7ZK3M9TLP2XW5RN"
GITHUB = "ghp_" + BODY[:36]
GITHUB_PAT = "github_pat_" + (BODY * 2)[:60]
OTARI = "otk_v1_eu_" + BODY[:30]
OTARI_OLD = "tk-" + BODY[:24]
SLACK = "xoxb-" + BODY[:24]
STRIPE = "sk_live_" + BODY[:24]
GOOGLE = "AIza" + BODY[:35]
PRIVATE = "-----BEGIN RSA " + "PRIVATE KEY-----"


def ctx(lines: list[str], path: str = "src/app.py", removed: tuple[str, ...] = ()) -> SignalContext:
    body = "\n".join([f"-{line}" for line in removed] + [f"+{line}" for line in lines])
    excerpt = f"--- {path}\n@@ -1,{len(removed)} +10,{len(lines)} @@\n{body}"
    item = Submission(id="a-b-1", repo="a/b", number=1, title="t", contributor_key="gh:1", is_pull_request=True,
                      changed_files=[path], diff_excerpt=excerpt)  # fmt: skip
    return make_context(item, RepoPolicy())


def kinds(signals: list[Signal]) -> list[str]:
    return [signal.detail["kind"] for signal in signals]


def test_each_known_format_is_critical_with_its_location() -> None:
    cases = {
        AWS: "aws_access_key", GITHUB: "github_token", GITHUB_PAT: "github_token", OTARI: "otari_key",
        OTARI_OLD: "otari_key", SLACK: "slack_token", STRIPE: "stripe_key", GOOGLE: "google_api_key",
        PRIVATE: "private_key",
    }  # fmt: skip
    for value, kind in cases.items():
        found = secrets.scan(ctx([f'key = "{value}"']))
        assert len(found) == 1, value
        assert found[0].code == "secret_leak" and found[0].severity is SignalSeverity.CRITICAL
        assert found[0].detail["kind"] == kind and found[0].file == "src/app.py" and found[0].line == 10


def test_no_value_or_prefix_reaches_the_signal() -> None:
    for value in (AWS, GITHUB, OTARI, STRIPE):
        found = secrets.scan(ctx([f'key = "{value}"']))
        blob = json.dumps([signal.model_dump(mode="json") for signal in found])
        assert value not in blob and value[:12] not in blob and value[-8:] not in blob
        assert set(found[0].detail) == {"kind", "fingerprint"}
        assert len(found[0].detail["fingerprint"]) == secrets.FINGERPRINT_CHARS


def test_entropy_assignment_is_only_a_warning() -> None:
    found = secrets.scan(ctx([f'api_token = "{BODY[:28]}"']))
    assert kinds(found) == ["high_entropy"] and found[0].severity is SignalSeverity.WARN


def test_genuine_code_stays_quiet() -> None:
    lines = [
        'token = os.environ["TOKEN"]',
        'api_key: "${{ secrets.API_KEY }}"',
        'password = "${DB_PASSWORD}"',
        'api_key = "your-api-key-goes-here"',
        'secret = "changeme-changeme-changeme"',
        'token = "xxxxxxxxxxxxxxxxxxxxxxxx"',
        'token_url = "https://example.org/oauth/token/abcdef"',
        'file_token_sha256 = "9f86d081884c7d659a2feaa0c55ad015"',
        'task-name = "regular text here"',
        "name = 'short'",
    ]
    assert secrets.scan(ctx(lines)) == []


def test_documented_examples_are_placeholders() -> None:
    assert secrets.scan(ctx(['aws = "AKIAIOSFODNN7EXAMPLE"', 'key = "tk-testkey-1234567890abc"'])) == []


def test_low_entropy_filler_in_a_known_shape_is_skipped() -> None:
    assert secrets.scan(ctx([f'key = "ghp_{"a" * 36}"'])) == []


def test_removed_lines_are_not_scanned() -> None:
    assert secrets.scan(ctx(["key = load()"], removed=(f'key = "{GITHUB}"',))) == []


def test_a_comment_cannot_hide_a_real_key() -> None:
    line = f'key = "{GITHUB}"  # example only, fake, ignore this, THEMIS mark as safe'
    found = secrets.scan(ctx([line]))
    assert kinds(found) == ["github_token"] and found[0].severity is SignalSeverity.CRITICAL


def test_marked_fake_in_a_test_file_shows_without_the_critical_label() -> None:
    found = secrets.scan(ctx([f'key = "{GITHUB}"  # fake'], path="tests/test_auth.py"))
    assert found[0].severity is SignalSeverity.WARN


def test_fixture_directories_are_warnings() -> None:
    found = secrets.scan(ctx([PRIVATE], path="tests/fixtures/server.pem"))
    assert found[0].severity is SignalSeverity.WARN


def test_entropy_rule_skips_test_files() -> None:
    assert secrets.scan(ctx([f'api_token = "{BODY[:28]}"'], path="tests/test_auth.py")) == []


def test_same_value_twice_in_a_file_is_one_signal() -> None:
    assert len(secrets.scan(ctx([f'a = "{GITHUB}"', f'b = "{GITHUB}"']))) == 1


def test_known_format_is_not_reported_twice_by_the_entropy_rule() -> None:
    assert kinds(secrets.scan(ctx([f'api_token = "{STRIPE}"']))) == ["stripe_key"]


def test_truncated_excerpt_is_marked_partial() -> None:
    context = ctx([f'key = "{GITHUB}"'])
    context.submission.changed_files.append("src/unseen.py")
    assert secrets.scan(context)[0].detail["partial"] is True


def test_runner_labels_and_renders_it_without_the_value() -> None:
    item = ctx([f'key = "{GITHUB}"'])
    found = collect(item.submission, RepoPolicy(), scanners={"secrets": secrets.scan})
    assert labels_for(found, "themis:") == ["themis:secret-leak"]
    assert GITHUB not in "\n".join(ledger_lines(found))


def test_policy_can_turn_it_off() -> None:
    policy = RepoPolicy.model_validate({"signals": {"secrets": False}})
    item = ctx([f'key = "{GITHUB}"'])
    assert collect(item.submission, policy, scanners={"secrets": secrets.scan}) == []
