# THEMIS

**Evidence, not opinions, for every contribution.**

AI tools made pull requests, issues and security reports almost free to write. Reviewing them still costs a maintainer's time, and projects are starting to close the door. THEMIS keeps it open. It does not try to guess who or what wrote a submission. It checks whether the claims are true, then posts an evidence ledger a maintainer can trust in seconds.

```text
### THEMIS · CONTRADICTED · evidence score 8/100
| # | Claim                                  | Check   | Result                       |
| 1 | Fixes a leak in ConnectionPool._release | symbols | ❌ ConnectionPool (HARD)      |
| 2 | Tests were added                       | tests   | ❌ no test file changed (HARD)|
```

## The problem

- Maintainers estimate that about 25% of incoming pull requests are low-quality AI output, and 31% on large projects ([Linux Foundation Research survey, Oct 2026](https://www.phoronix.com/news/Linux-Foundation-25p-AI-Slop)). That is a survey estimate, not a count.
- curl [ended its bug bounty](https://www.theregister.com/2026/01/21/curl_ends_bug_bounty/) on Jan 31, 2026, to remove the incentive for made-up reports.
- tldraw [auto-closes external pull requests](https://tldraw.dev/blog/stay-away-from-my-trash), and Ghostty allows AI-assisted ones [only for accepted issues](https://www.mintlify.com/ghostty-org/ghostty/guides/contributing).
- GitHub [added settings](https://github.blog/changelog/2026-02-13-new-repository-settings-for-configuring-pull-request-access/) to turn pull requests off or limit them to collaborators.

Each of these closes the door on honest newcomers along with the spam. The bottleneck is the cost of checking whether a claim is true, and that is what THEMIS lowers.

Built for the Mozilla MozFest "Developers Wilding: Online" hackathon. Every model call goes through Mozilla AI's hosted [Otari](https://otari.ai) gateway using open-weight models.

## How it works

```text
GitHub event → Intake → Shield → Clerk → Plan → Evidence → Verdict → Arbiter → Report
                         │        │              │           │         │
                 injection check  claims    repository,   deterministic  neutral
                                            web, sandbox,   code only    explanation
                                            tests, registries
```

- **Claims, not authorship.** Honest AI-assisted contributions pass. There is no AI-text detector.
- **Models extract and explain. Code decides.** The verdict is a pure function with no identity input.
- **A wrong accusation is the worst failure.** `CONTRADICTED` needs a deterministic fact. Anything uncertain stays `UNVERIFIED`.
- **The maintainer stays in charge.** Advisory by default, with no code that can close or merge.
- **Submission text is data.** It stays in a data envelope, and the Arbiter never reads it.

## Quickstart

```bash
uv sync --dev
cp .env.example .env               # OTARI_API_KEY and model IDs
uv run pytest -q                   # no network needed
uv run themis bench                # benchmark, no model and no key needed
uv run themis deps pypi reqeusts   # dependency check, no key needed
uv run themis check https://github.com/OWNER/REPO/issues/1 --dry-run
uv run themis serve                # dashboard on 127.0.0.1:8080
```

To install it on a repository, follow [`docs/DOCS.md`](docs/DOCS.md) section 2.3.

## Judge quick path

1. Read [`docs/READINESS.md`](docs/READINESS.md) for what has been verified on the live gateway.
2. Open [`evidence/README.md`](evidence/README.md) for one receipt per rubric item.
3. Read [`docs/OTARI_FEEDBACK.md`](docs/OTARI_FEEDBACK.md) for what we learned, with request IDs.
4. Run `uv run pytest -q` and `uv run themis bench`.
5. Compare the pre-Otari baseline [`baseline-pre-otari`](https://github.com/pextacy/themis/tree/baseline-pre-otari) with the first commit that calls Otari, [`first-otari-call`](https://github.com/pextacy/themis/tree/first-otari-call).

## Rubric evidence

| Rubric item | Where | Live proof |
|---|---|---|
| Open-weight model | Clerk, `src/themis/agents/clerk.py` | `evidence/chat-a-live.json` |
| Second open-weight role | Arbiter on Liquid `lfm2.5-2.6b-free`, another family than the Clerk | `evidence/chat-b-live.json` |
| Routing and fallback | `Otari-Fallback` read in the client | `evidence/routing-live.json` |
| MCP | `src/themis/evidence/context.py` | `evidence/mcp-live.json` |
| Web search and fetch | `src/themis/evidence/web.py` | `evidence/web-live.json` |
| Code execution | `src/themis/evidence/repro.py` | Not verified. Built and tested offline. The hosted sandbox answers 402 on an unfunded account: `evidence/codeexec-live.json` |
| Budgets | Refusal maps to `DEFERRED` | `evidence/budget-refusal-live.json` |
| Multiple model roles | Clerk and Prover on Nemotron, Arbiter on LFM2.5 | `evidence/chat-a-live.json`, `evidence/chat-b-live.json` |
| Inference guardrails | `src/themis/shield/guardrails.py` | Not verified. No hosted guardrail profile is set up, so the demo runs in `monitor` mode and the deterministic sanitizer alone screens text |
| Per-user budgets | `src/themis/otari/budgets.py` | `evidence/peruser-budget-live.json` |

## Measured results

| Measure | Result | What it means |
|---|---|---|
| Offline benchmark, 36 items | 0 false contradictions, 15 of 15 slop flagged, 6 of 6 injections caught | Regression check. The dataset was written alongside the rules, so this is not field accuracy |
| Real bug reports, 110 from 10 unseen repositories | 5 of 1,328 checks wrongly `HARD` (0.38%) | Worst case: every backticked token treated as a claim. Target is zero, not yet met |
| Real issue and PR text, 5,000 bodies from 25 repositories | 0 tainted by the sanitizer | False positives only, says nothing about catch rate |
| Live Clerk on `nemotron-3.5-super-vl:free` | 7 of 13 answered fixtures valid (gate: 95%) | Measured Nov 8, cut short by the provider's free daily quota. All six failures stopped at `finish_reason=length`: the reasoning model spent its token allowance thinking. The allowance was raised from 1,200 to 4,096 and is not yet re-measured. `themis bench --live` has not produced a usable run yet |

## Limitations

- The real Clerk has not yet passed its validity gate or been measured against the benchmark. When it fails, plainly worded claims still get checked by a regex fallback, marked degraded.
- Reproduction works on self-contained source, and a model writes the probe.
- Code execution needs a funded Otari account, and hosted guardrails need a vendor set up in the dashboard.
- GitHub only. npm and PyPI only for dependencies.

## Roadmap

- Measure the real pipeline on maintainers' repositories, not only on a benchmark written alongside the rules.
- Bring the 0.38% wrong-`HARD` rate on real bug reports down to zero.
- Pin actions to commit hashes and publish the action under a release tag.
- Post ledgers on Forgejo and Codeberg, which are read-only today.

## Project documents

[`docs/DOCS.md`](docs/DOCS.md) · [`docs/FEATURES.md`](docs/FEATURES.md) · [`docs/OTARI_FEEDBACK.md`](docs/OTARI_FEEDBACK.md) · [`docs/READINESS.md`](docs/READINESS.md)

## License

Apache-2.0. See [`LICENSE`](LICENSE).
