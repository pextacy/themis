"""Create the demo repository and scenarios D1-D8 with the `gh` CLI.

    uv run python demo/seed.py            # prints the plan and changes nothing
    uv run python demo/seed.py --apply --repo OWNER/tinyhttp-lite

`--apply` creates a public repository and opens real issues and pull requests. It reads the key
from OTARI_API_KEY and the settings below from the environment, and stops before creating anything
if one is missing. They go in before the first issue, or the workflows would run with no key.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LIBRARY = ROOT / "tinyhttp-lite"
SCENARIOS = json.loads((ROOT / "scenarios.json").read_text())
VARIABLES = ("OTARI_BASE_URL", "THEMIS_MODEL_CLERK", "THEMIS_MODEL_PROVER", "THEMIS_MODEL_ARBITER")


def run(*command: str, cwd: Path | None = None) -> None:
    subprocess.run(command, cwd=cwd, check=True)  # noqa: S603 - fixed commands, no shell


def missing_settings(environment: dict[str, str]) -> list[str]:
    return [name for name in ("OTARI_API_KEY", *VARIABLES) if not environment.get(name)]


def configure(repo: str, environment: dict[str, str]) -> None:
    # stdin, because a command line can be read by other processes.
    subprocess.run(  # noqa: S603 - fixed command, no shell
        ["gh", "secret", "set", "OTARI_API_KEY", "--repo", repo],  # noqa: S607
        input=environment["OTARI_API_KEY"], text=True, check=True,
    )  # fmt: skip
    for name in VARIABLES:
        run("gh", "variable", "set", name, "--repo", repo, "--body", environment[name])
    for name in ("THEMIS_GUARDRAIL_MODE", "OTARI_CONTRIBUTOR_BUDGET_ID"):
        if environment.get(name):
            run("gh", "variable", "set", name, "--repo", repo, "--body", environment[name])


def apply_file(path: Path, spec: str) -> None:
    action, _, content = spec.partition(":")
    if action == "write":
        path.write_text(content)
    elif action == "append":
        path.write_text(path.read_text() + content)
    else:
        old, new = content.split("=>", 1)
        path.write_text(path.read_text().replace(old, new))


def open_pull(repo: str, scenario: dict, work: Path) -> None:
    run("git", "checkout", "-q", "-b", scenario["branch"], "main", cwd=work)
    for name, spec in scenario["files"].items():
        apply_file(work / name, spec)
    run("git", "add", "-A", cwd=work)
    run("git", "commit", "-q", "-m", scenario["title"], cwd=work)
    run("git", "push", "-q", "origin", scenario["branch"], cwd=work)
    run("gh", "pr", "create", "--repo", repo, "--head", scenario["branch"], "--title", scenario["title"],
        "--body", scenario["body"], cwd=work)  # fmt: skip


def create(repo: str, environment: dict[str, str]) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        work = Path(temporary) / "repo"
        shutil.copytree(LIBRARY, work, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".venv"))
        run("git", "init", "-q", "-b", "main", cwd=work)
        run("git", "add", "-A", cwd=work)
        run("git", "commit", "-q", "-m", "Initial import", cwd=work)
        run("git", "tag", "v0.3.1", cwd=work)
        run("gh", "repo", "create", repo, "--public", "--source", str(work), "--push", cwd=work)
        run("git", "push", "-q", "origin", "v0.3.1", cwd=work)
        configure(repo, environment)
        for scenario in SCENARIOS:
            if scenario["type"] == "pr":
                open_pull(repo, scenario, work)
            else:
                for _ in range(scenario.get("count", 1)):
                    run(
                        "gh",
                        "issue",
                        "create",
                        "--repo",
                        repo,
                        "--title",
                        scenario["title"],
                        "--body",
                        scenario["body"],
                    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Really create the repository and scenarios.")
    parser.add_argument("--repo", help="OWNER/NAME to create.")
    args = parser.parse_args()
    for scenario in SCENARIOS:
        print(f"{scenario['id']} {scenario['type']:5} expected {scenario['expected']}: {scenario['title']}")
    if args.apply:
        if not args.repo:
            parser.error("--apply needs --repo")
        environment = dict(os.environ)
        missing = missing_settings(environment)
        if missing:
            parser.error(f"not set: {', '.join(missing)}")
        create(args.repo, environment)


if __name__ == "__main__":
    main()
