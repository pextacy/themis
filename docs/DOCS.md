# THEMIS Documentation

Reference documentation for users (maintainers), contributors, operators and judges.

- [1. Overview](#1-overview)
- [2. Quickstart](#2-quickstart)
- [3. Configuration](#3-configuration)
- [4. GitHub Action](#4-github-action)
- [5. CLI](#5-cli)
- [6. Pipeline reference](#6-pipeline-reference)
- [7. Otari integration reference](#7-otari-integration-reference)
- [8. Verdicts, claims and checks](#8-verdicts-claims-and-checks)
- [9. Evidence ledger and receipts](#9-evidence-ledger-and-receipts)
- [10. Dependency verifier](#10-dependency-verifier)
- [11. Security model](#11-security-model)
- [12. Dashboard](#12-dashboard)
- [13. Benchmark](#13-benchmark)
- [14. Troubleshooting](#14-troubleshooting)
- [15. FAQ](#15-faq)
- [16. Judge quick path](#16-judge-quick-path)

---

## 1. Overview

THEMIS triages GitHub pull requests, issues and security-style reports by **verifying their claims**. For each submission it:

1. screens untrusted text for prompt injection (Shield),
2. extracts atomic, falsifiable claims (Clerk),
3. checks each claim against the repository, the web, test results, package registries and sandboxed execution (Evidence engine),
4. computes a deterministic verdict and evidence score (Verdict),
5. explains the result in neutral language (Arbiter),
6. posts an evidence ledger, labels and a receipt (Report).

All model calls go through **hosted Otari** using **open-weight models**.

---

## 2. Quickstart

### 2.1 Requirements
- Python 3.12+, [`uv`](https://docs.astral.sh/uv/)
- An Otari account with a funded workspace and an API key
- A GitHub token with `contents:read`, `issues:write`, `pull_requests:write`, `checks:write`

### 2.2 Local
```bash
git clone https://github.com/<org>/themis && cd themis
uv sync --dev
cp .env.example .env          # fill OTARI_API_KEY, GITHUB_TOKEN, model IDs
uv run themis doctor          # readiness table for every Otari feature
uv run themis check https://github.com/<owner>/<repo>/issues/<n>
```

### 2.3 As a GitHub Action (recommended)
1. Add secret `OTARI_API_KEY` to the repository.
2. Copy `.github/workflows/themis-tests.yml` and `.github/workflows/themis-analyze.yml` from `examples/`.
3. Add `.github/themis.yml` (see §3.2).
4. Open an issue — THEMIS replies with a ledger.

---

## 3. Configuration

### 3.1 Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `OTARI_API_KEY` | yes | — | Hosted Otari API key (never commit) |
| `OTARI_BASE_URL` | no | `https://api.otari.ai/api/v1` | Gateway base URL |
| `THEMIS_MODEL_CLERK` | yes | — | Model ID or Otari routing-policy name for the Clerk role |
| `THEMIS_MODEL_PROVER` | yes | — | Model ID or policy name for the Prover role |
| `THEMIS_MODEL_ARBITER` | yes | — | Model ID or policy name for the Arbiter role (different family) |
| `THEMIS_GUARDRAIL_PROFILE` | no | `prompt-injection` | Otari guardrail profile used by Shield |
| `THEMIS_GUARDRAIL_MODE` | no | `block` | `block` or `monitor` |
| `THEMIS_MCP_SERVERS` | no | DeepWiki | JSON list of inline MCP server declarations |
| `THEMIS_OTARI_TIMEOUT_S` | no | `60` | Per-call timeout |
| `THEMIS_MAX_CONCURRENCY` | no | `4` | Parallel evidence checks |
| `GITHUB_TOKEN` | yes | — | GitHub API token |
| `THEMIS_DB_URL` | no | `sqlite:///./themis.db` | Store |
| `THEMIS_LOG_LEVEL` | no | `INFO` | Log level |

Model IDs must come from `GET /api/v1/models` on your workspace. Do not copy IDs from examples blindly — catalogs change.

### 3.2 Repository policy: `.github/themis.yml`

```yaml
version: 1
mode: advisory            # advisory | gate (gate = failing check run for CONTRADICTED/TAINTED)

kinds:                    # which submission kinds to analyze
  issues: true
  pull_requests: true
  security_reports: true  # issues labeled `security` or matching the template

checks:
  symbols: true
  versions: true
  context_mcp: true
  web: true
  reproduction: true
  tests: true
  consistency: true
  dependencies: true
  duplicates: true

tests:
  command: "pytest -q --junitxml=themis-junit.xml"   # used by themis-tests.yml

budgets:
  per_contributor_daily_usd: 0.05
  project_monthly_usd: 10.00

scoring:
  weights: { reproduced: 1.0, confirmed: 0.7, unverified: 0.3, soft_contradicted: 0.1, hard_contradicted: 0.0 }
  plausible_threshold: 0.6

policy:
  auto_close: false                 # THEMIS never closes unless set true
  first_time_contributor_fast_track: true
  maintainers: ["@owner", "@team/maintainers"]   # who may /themis override

labels:
  prefix: "themis:"
```

---

## 4. GitHub Action

### 4.1 Workflows

**`themis-tests.yml`** — fork-safe, **no secrets**
```yaml
on: pull_request
permissions: { contents: read }
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: <org>/themis/action/tests@v1     # runs tests.command, uploads themis-test-results.json
```

**`themis-analyze.yml`** — holds `OTARI_API_KEY`, **never checks out PR code**
```yaml
on:
  issues: { types: [opened, edited] }
  issue_comment: { types: [created] }
  workflow_run: { workflows: ["themis-tests"], types: [completed] }
permissions: { contents: read, issues: write, pull-requests: write, checks: write, actions: read }
jobs:
  analyze:
    runs-on: ubuntu-latest
    steps:
      - uses: <org>/themis/action@v1
        with:
          otari-api-key: ${{ secrets.OTARI_API_KEY }}
          config: .github/themis.yml
```

### 4.2 Comment commands
| Command | Who | Effect |
|---|---|---|
| `/themis recheck` | anyone on own item, maintainers on any | Re-run the pipeline |
| `/themis recheck --approve` | maintainers | Run even if the contributor budget is exhausted (charges project budget) |
| `/themis override <verdict> <reason>` | maintainers | Replace the verdict; recorded in `overrides` |
| `/themis explain` | anyone | Post the detailed per-check explanation |

---

## 5. CLI

```text
themis doctor                         Check key, balance, models, tools, MCP, guardrails, budgets
themis check <url> [--dry-run] [--json] [--no-post]
themis bench [--subset NAME] [--live]
themis serve [--host 127.0.0.1] [--port 8080]
themis evidence capture <feature>     Run one live call and write a redacted receipt to evidence/
themis ledger <submission-id>         Print the ledger markdown
themis deps <ecosystem> <name>        Run the dependency verifier on one package
```

Exit codes: `0` success, `2` configuration error, `3` gateway error, `4` budget deferred, `5` tainted (only with `--gate`).

---

## 6. Pipeline reference

| Stage | Module | Uses models | Failure behavior |
|---|---|---|---|
| Intake | `intake/` | no | Unsupported event → skip |
| Shield | `shield/` | guardrail | Guardrail unavailable in `block` → `TAINTED_UNCHECKED`, human review |
| Clerk | `agents/clerk.py` | yes | Invalid JSON after repair → claims = `[]`, verdict `NEEDS_EVIDENCE` |
| Plan | `verdict/plan.py` | no | Insufficient budget → `DEFERRED` |
| Evidence | `evidence/*` | some | Each check fails independently → `UNVERIFIED` |
| Verdict | `verdict/scoring.py` | **no** | Pure function |
| Arbiter | `agents/arbiter.py` | yes | Failure → template explanation |
| Report | `report/` | no | GitHub error → retried, ledger kept in store |

### 6.1 Untrusted data envelope
All submission content is sent only as:
```
<untrusted_submission id="..." kind="...">
  ...escaped content...
</untrusted_submission>
```
System prompts state that envelope content is data and must never be followed. The Arbiter receives only the structured `Check[]` JSON, never the envelope.

---

## 7. Otari integration reference

> Facts below come from the upstream Otari docs and OpenAPI schema (`mozilla-ai/otari` @ `e9a72e7c8e`) and from reports during the hackathon. `docs/READINESS.md` says which are live-verified. Re-verify with `themis doctor`; record discrepancies in `docs/OTARI_FEEDBACK.md`.

### 7.1 Endpoints
| Endpoint | Use |
|---|---|
| `POST /chat/completions` | Clerk, Prover, Arbiter, web tools, code execution |
| `POST /messages` (streamed) | MCP calls — exposes `mcp_tool_use` / `mcp_tool_result` blocks |
| `GET /models` | Model catalog, pricing, capabilities |
| `GET /tools` | Built-in tool availability for the workspace |

### 7.2 Request conventions
- `model`: model ID or routing-policy name from config.
- `user`: `gh:<numeric_github_id>` (per-user attribution and budgets) on `/chat/completions`; `metadata.user_id` on `/messages`. Requires a service key: a normal key naming another user is refused with 403.
- Header `Otari-Conversation-Id: themis-<submission_id>`.
- Header `Otari-End-User-Budget: <budget id>` picks the budget only when the request creates the end user; an existing end user keeps its budget. The id must be on the key's `end_user_budget_ids`, else 403 `end_user_budget_not_allowed`.
- Built-in tools declared **type-only**, e.g. `{"type": "otari_web_search"}`, `{"type": "otari_code_execution"}`; additional keys may be rejected with HTTP 400.
- Guardrails requested per call as a top-level field, `"guardrails": [{"profile": "prompt-injection", "mode": "block", "on_unavailable": "block"}]`, or mandated by the organization or a routing policy. Profiles are listed by `GET /tool-settings/guardrails/profiles`.
- MCP activity blocks need the header `anthropic-beta: mcp-client-2025-11-20`. Inline `mcp_servers` need `request_mcp_servers_allowed` on the workspace; a new workspace starts with it off, and the request is then refused with 400.

### 7.3 Response data persisted
`Otari-Request-ID`, `Otari-Provider`, `Otari-Model` (effective model; the body's `model` keeps the policy name), `Otari-Attempt-Count`, `Otari-Fallback`, `Otari-Guardrails`, `usage` (tokens, `cost_usd` as a string when priced), `otari_tool_calls`.

### 7.4 Error mapping
| Gateway outcome | THEMIS exception | THEMIS behavior |
|---|---|---|
| 401 | `OtariConfigError` | Abort run, `doctor` hint |
| 402 `insufficient_funds` / `pricing_required` | `OtariFundingError` | Affected check `UNVERIFIED`; alert maintainer |
| 403 `guardrail_violation` | `OtariGuardrailBlocked` | Verdict `TAINTED` |
| 403 `budget_exceeded` (read `Otari-Budget-Scope`, `Retry-After`) | `OtariBudgetExceeded` | Verdict `DEFERRED` |
| 502 on a `block` guardrail that cannot be evaluated | `OtariGuardrailUnavailable` | Stop as `TAINTED_UNCHECKED` |
| 400 schema | `OtariSchemaRejected` | Bug: logged with request ID, check `UNVERIFIED` |
| 429 | `OtariRateLimited` | Retry with `Retry-After` (max 2) |
| 5xx / 504 | `OtariUpstreamError` | One retry; routing policy fallback; else `UNVERIFIED` |

### 7.5 Routing policies (workspace)
Example policy for the Clerk role, configured in the Otari dashboard. Upstream docs say an entry with no key of yours behind it is served by otari.ai and billed to the balance, so BYOK should only be needed for the forced-fallback test:
```yaml
themis-clerk:
  select:
    - default: <provider>:<open-weight-model-A>
  on_failure:
    - <provider>:<open-weight-model-A2>
```
`themis-fallback-demo` deliberately has a failing primary: put a provider key with an invalid value behind the primary, then the primary fails and `on_failure` serves the request with `Otari-Fallback: true`.

### 7.6 Budgets
- Project key: a budget (`max_budget`, optional `request_limit`) set on the key by an organization owner or admin.
- Per contributor: an organization budget used as an end-user budget on a service key. Each end user gets a full copy of the limit. The key's own ceiling pools all of them. Deleting an end user does not reset its spend.
- A request's hold is its worst case, including its whole `max_tokens`, so send a realistic `max_tokens` or small budgets refuse early.
- THEMIS never treats `insufficient_funds` as a budget refusal; they are reported differently.

---

## 8. Verdicts, claims and checks

### 8.1 Claim types
`symbol_exists`, `file_exists`, `version_exists`, `behavior`, `fixes_issue`, `test_added`, `cve_reference`, `dependency_added`, `performance`, `other`.

### 8.2 Check statuses
| Status | Meaning |
|---|---|
| `REPRODUCED` | Behavior observed in sandbox execution |
| `CONFIRMED` | Fact verified (exists, passes, cited source real) |
| `CONTRADICTED` | Evidence shows the claim is false (`HARD` deterministic / `SOFT` model-judged) |
| `UNVERIFIED` | Could not be checked (unsupported, error, budget) |
| `NOT_APPLICABLE` | Check irrelevant to this claim |

### 8.3 Submission verdicts
| Verdict | Label | Meaning | Default action |
|---|---|---|---|
| `EVIDENCED` | `themis:evidenced` | Core claims reproduced or confirmed, tests pass | Fast-track to review |
| `PLAUSIBLE` | `themis:plausible` | Mostly confirmed, nothing contradicted | Normal review |
| `NEEDS_EVIDENCE` | `themis:needs-evidence` | Not enough checkable evidence | Contributor checklist |
| `CONTRADICTED` | `themis:contradicted` | At least one HARD contradiction | Low priority, maintainer decides |
| `TAINTED` | `themis:tainted` | Prompt injection detected | Quarantine, human review |
| `DEFERRED` | `themis:deferred` | Budget exhausted | Waits for approval or next day |

`CONTRADICTED` can only result from deterministic facts. A model's opinion alone can never produce it.

---

## 9. Evidence ledger and receipts

### 9.1 Ledger comment (excerpt)
```markdown
<!-- themis:ledger -->
### THEMIS · CONTRADICTED · evidence score 8/100
| # | Claim | Check | Result |
|---|---|---|---|
| 1 | "`ConnectionPool._release` leaks sockets" | symbols @ a1b2c3d | ❌ `ConnectionPool` not found in repository (HARD) |
| 2 | "Adds regression tests" | tests artifact | ❌ no test files changed (HARD) |
| 3 | "Fixes CVE-2026-99999" | web (NVD/OSV) | ❌ CVE ID not found (HARD) |

**What would help:** link the exact file and line, add a failing test, cite the advisory.
Receipt: `receipts/themis-1842.json` · cost $0.004 · Otari requests: 3 · override: `/themis override <verdict> <reason>`
```

### 9.2 Receipt schema (`receipt.v1`)
```json
{
  "schema": "themis.receipt.v1",
  "submission": {"repo": "owner/name", "number": 42, "kind": "bugfix_pr", "head_sha": "…"},
  "verdict": {"value": "CONTRADICTED", "score": 8, "weights": {"…": 0}},
  "claims": [{"id": "c1", "type": "symbol_exists", "text": "…", "span": [10, 42]}],
  "checks": [{"claim_id": "c1", "module": "symbols", "status": "CONTRADICTED", "strength": "HARD",
              "detail": {"ref": "a1b2c3d", "searched": ["src/**"]}, "otari_request_id": null}],
  "otari_calls": [{"request_id": "…", "role": "clerk", "requested_model": "…", "effective_model": "…",
                   "provider": "…", "fallback": false, "usage": {"prompt": 812, "completion": 190},
                   "cost_usd": 0.0011, "latency_ms": 2140, "prompt_version": "clerk@3"}],
  "shield": {"guardrail": {"profile": "prompt-injection", "mode": "block", "valid": true}, "sanitizer": []},
  "created_at": "2026-11-09T12:00:00Z",
  "themis_version": "0.1.0"
}
```
`cost_usd` is `null` when the gateway does not report it (e.g. some tool endpoints) — never estimated silently.

---

## 10. Dependency verifier

Ecosystems (MVP): npm (`package.json`), PyPI (`requirements*.txt`, `pyproject.toml`).

| Signal | Rule | Strength |
|---|---|---|
| `missing` | Name not on registry | HARD contradiction, risk `block` |
| `ledger_hit` | Name recorded as hallucinated before | risk `block` |
| `newborn` | First release < 30 days | risk `review` |
| `install_scripts` | npm lifecycle scripts present | risk `review` |
| `low_adoption` | Downloads below threshold | risk `review` |
| `lookalike` | Edit distance ≤ 2 to a popular package, or mash-up of two real names | risk `review` |

The hallucination ledger is a JSONL file (`data/hallucination-ledger.jsonl`) of `{ecosystem, name, first_seen, source_model?, count}` and is designed to be shared between projects (community immune system).

CLI: `themis deps pypi requests-toolbelt-pro`.

---

## 11. Security model

### 11.1 Threats and controls
| Threat | Control |
|---|---|
| Prompt injection in issue/PR/diff/comments | Otari guardrail (block), sanitizer, data envelope, Arbiter isolation, deterministic labels |
| Secret exfiltration via PR code | Two-workflow split; analysis workflow never checks out PR code; PR code runs only in Otari sandbox or secret-less runner |
| Budget drain by spam | Per-contributor budgets, project ceiling, cheap-first cascade, `DEFERRED` |
| Malicious dependency (slopsquatting) | Dependency verifier + ledger |
| Model manipulation of outcomes | Models cannot set labels or close/merge; HARD contradictions are deterministic |
| Unfair treatment of newcomers | Identity not an input to scoring; fast-track only raises priority |
| Data leakage to providers | Only numeric IDs as `user`; no emails; redaction before persistence |

### 11.2 Permissions
Minimum GitHub permissions per workflow are listed in §4.1. The dashboard binds to `127.0.0.1` by default.

---

## 12. Dashboard

`themis serve` → `http://127.0.0.1:8080`

- **Queue:** submissions sorted by evidence score, verdict filters, kind filters.
- **Detail:** ledger, every check, shield findings, Otari calls with request IDs, cost, fallback flags, override history.
- **Stats:** slop rate, verdict distribution, median cost/latency, guardrail blocks, budget usage, fallback count.
- **Doctor:** live readiness table (key, balance, models, tools, MCP, guardrails, budgets).

---

## 13. Benchmark

```bash
uv run themis bench --live
```
Dataset in `bench/dataset/*.json` (genuine, slop, adversarial). Metrics: precision/recall per verdict, false-`CONTRADICTED` count on genuine items (must be 0), injection catch rate, dependency catch rate, cost and latency distributions. Results are written to `bench/results/` and summarized in the README.

---

## 14. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `doctor`: models list empty | Key lacks access / wrong region | Check workspace, base URL `/api/v1` |
| 400 on tool call | Extra keys in built-in tool declaration | Use type-only declarations |
| 402 on code execution | Funding or tool pricing not configured | Top up; check tool pricing; report with request ID |
| MCP activity missing | Non-streaming endpoint used | Use streamed `/messages` with MCP |
| Every item `DEFERRED` | Per-contributor budget too low | Raise `budgets.per_contributor_daily_usd` |
| Guardrail always unavailable | No guardrail service configured in workspace | Configure vendor key or switch to `monitor` for testing |
| Ledger not posted on fork PRs | Analysis triggered on `pull_request` | Use `workflow_run` setup from §4.1 |

---

## 15. FAQ

**Does THEMIS detect AI-generated text?** No. AI-assisted contributions with true claims pass.
**Can THEMIS close PRs?** Only if a maintainer sets `policy.auto_close: true`. Default is advisory.
**Does THEMIS run my contributors' code?** Only inside Otari's hosted sandbox (minimal reproductions) or in a secret-less CI job.
**Which models?** Open-weight models via hosted Otari, chosen per role by measured reliability.
**How much does it cost?** Measured per submission and shown in every ledger; see benchmark results.

---

## 16. Judge quick path

1. Open the demo repository and look at issues/PRs D1–D8 — each has a THEMIS ledger.
2. Open `evidence/README.md` — one real redacted receipt per rubric item, with Otari request IDs.
3. (Optional) Run locally with your key:
   ```bash
   uv sync && cp .env.example .env && uv run themis doctor
   uv run themis check <demo-issue-url> --no-post
   ```
4. Watch the 3–5 min demo video and the Otari feedback recording (links in README).
