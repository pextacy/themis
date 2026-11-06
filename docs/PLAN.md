# PLAN — THEMIS Implementation Plan

This document describes **how** THEMIS will be built: architecture, components, pipeline, Otari integration per rubric item, data model, demo, testing, risks and the submission strategy. Timing lives in `PHASES.md`.

---

## 1. Strategy

1. **Verify the platform before building on it.** Hosted Otari has known sharp edges (type-only tool declarations, BYOK-only routing policies, vendor-keyed guardrails, a past `402` on code execution). Phase 0 is a feature-by-feature live spike that produces real receipts. Design decisions are locked only after that.
2. **Deterministic core, model-assisted edges.** The most convincing slop signals (non-existent symbols, fake versions, fabricated CVEs, missing tests, hallucinated packages) are checkable without any model. Models are used where they add unique value: claim extraction, reproduction authoring, and plain-language explanation.
3. **Every rubric item is a real product need,** not a bolt-on. Judges see a coherent pipeline where each Otari feature has an obvious reason to exist.
4. **Evidence-driven submission.** The repo ships its own rubric evidence table backed by redacted receipts, a judge quick path and a benchmark.
5. **Breadth insurance.** Implement all 8 advanced features so the 60-point cap is reached even if two are unavailable on judging day.

---

## 2. Architecture

```
                 GitHub (issues, PRs, comments)
                          │ webhook events
          ┌───────────────┴────────────────┐
          │                                │
  themis-tests.yml                  themis-analyze.yml
  (pull_request, NO secrets)        (issues / issue_comment / workflow_run, HAS OTARI_API_KEY)
  runs PR tests, uploads            never checks out PR code
  test-results artifact ──────────► downloads artifact
                                           │
                                    ┌──────▼──────┐
                                    │  THEMIS core │  (Python package, same code in CLI / Action / server)
                                    └──────┬──────┘
   ┌─────────┬──────────┬──────────┬───────┼─────────┬──────────┬─────────┐
   ▼         ▼          ▼          ▼       ▼         ▼          ▼         ▼
 Intake → Shield  →  Clerk  →  Evidence engine (parallel)  →  Verdict → Arbiter → Report
           │          │        ├ symbols/versions (GitHub API, deterministic)
           │          │        ├ repo context (MCP via Otari)
           │          │        ├ web verification (Otari web search/fetch)
           │          │        ├ reproduction (Prover + Otari code execution)
           │          │        ├ tests & claim/diff consistency
           │          │        ├ dependency verifier (registries + ledger + web)
           │          │        └ duplicates (GitHub search)
           │          │
           └──────────┴──────────────── all model calls ───────────────► Hosted Otari gateway
                                                                         (open-weight models,
                                                                          routing/fallback, budgets,
                                                                          per-user budgets, guardrails,
                                                                          tools, MCP, activity)
                                    │
                             SQLite store + receipts ──► FastAPI/HTMX dashboard
```

### 2.1 Why two workflows
`pull_request_target` + checkout of PR code is a well-known secret-exfiltration vector. THEMIS splits execution:
- `themis-tests.yml` (`on: pull_request`, fork-safe, no secrets) runs the project's own test command and uploads `themis-test-results.json`.
- `themis-analyze.yml` (`on: workflow_run` of the test workflow, plus `issues` and `issue_comment`) holds `OTARI_API_KEY`, reads the PR via API only, downloads the artifact, runs THEMIS, posts the ledger.

---

## 3. Pipeline (per submission)

| # | Stage | Model? | Otari features | Output |
|---|---|---|---|---|
| 0 | Intake | no | — | `Submission` |
| 1 | Shield | guardrail | Inference guardrails (block), per-user attribution | `ShieldResult` (clean / tainted + spans) |
| 2 | Clerk | yes (open-weight A) | Routing policy `themis-clerk` + fallback, per-user budget | `ClaimSet` (typed claims + spans + kind) |
| 3 | Plan | no | — | Which checks apply, cost estimate vs remaining budget |
| 4 | Evidence (parallel) | mixed | MCP, web search/fetch, code execution, routing | `Check[]` |
| 5 | Verdict | **no** | — | Deterministic `Verdict` + score |
| 6 | Arbiter | yes (open-weight B, different family) | Routing policy `themis-arbiter` | Neutral explanation from structured evidence |
| 7 | Report | no | — | Ledger comment, check run, labels, receipt |

### 3.1 Cascade and budget gating
- After Shield + Clerk (cheap), the planner estimates the remaining cost of applicable checks using `GET /models` pricing.
- If the contributor's per-user budget can't cover it → `DEFERRED` (maintainer can `/themis recheck --approve`).
- Expensive checks (code execution, multi-page web) only run when claims are checkable and deterministic checks didn't already contradict them (no point reproducing a bug in a function that doesn't exist).

### 3.2 Deterministic verdict function (sketch)

```
if shield.tainted:                         verdict = TAINTED
elif budget_deferred:                      verdict = DEFERRED
elif any(c.status == CONTRADICTED and c.strength == HARD for c in checks):
                                           verdict = CONTRADICTED
elif any(c.status in {REPRODUCED} for c in checks) or tests_pass_and_claims_confirmed:
                                           verdict = EVIDENCED
elif confirmed_ratio >= 0.6 and no contradictions:
                                           verdict = PLAUSIBLE
else:                                      verdict = NEEDS_EVIDENCE
```

HARD contradictions are only deterministic facts: referenced symbol/file/version does not exist at the referenced commit, cited CVE does not exist, added package does not exist on its registry, PR claims tests were added but no test file changed. Model-judged mismatches are SOFT and can never produce `CONTRADICTED` on their own.

Evidence score = weighted sum of per-claim outcomes (REPRODUCED 1.0, CONFIRMED 0.7, UNVERIFIED 0.3, CONTRADICTED-soft 0.1, CONTRADICTED-hard 0) × coverage factor, clamped 0–100. Weights live in config and are printed in the receipt.

---

## 4. Model roles

| Role | Job | Model profile | Why separate |
|---|---|---|---|
| **Clerk** | Classify kind, extract atomic claims with spans, JSON | Small/fast open-weight (Qwen-class ~27B) | Runs on everything; must be cheap |
| **Prover** | Write minimal reproduction scripts; interpret sandbox output | Strong open-weight coder (DeepSeek-/Qwen-coder-class) | Code quality matters; runs rarely |
| **Arbiter** | Explain the deterministic verdict from structured evidence | Different family (Llama-/GLM-/Kimi-class) | Independent family reduces correlated errors; never sees raw text |

Exact model IDs are chosen in Phase 0 from `GET /api/v1/models` by measuring JSON validity rate, latency and price on 10 fixtures, then written to `.env` / `themis.yml`.

---

## 5. Otari integration plan (per rubric item)

| Rubric item | Integration | Proof captured in `evidence/` |
|---|---|---|
| Open-weight model (20) | Clerk calls via `/chat/completions` | `clerk-live.json` (request ID, model, provider, cost) |
| 2nd open-weight role (10) | Prover + Arbiter on other families | `prover-live.json`, `arbiter-live.json` |
| Routing & fallback (10) | Policies `themis-clerk`, `themis-prover`, `themis-arbiter` (BYOK provider key in workspace) with `on_failure`; plus a deliberate fallback test using an unavailable primary in a dedicated `themis-fallback-demo` policy | `routing-live.json` with `Otari-Fallback: true` |
| MCP (10) | Inline `mcp_servers` (DeepWiki MCP) on streamed `/messages` to read repo structure for grounding | `mcp-live.json` with `mcp_tool_use` + `mcp_tool_result` |
| Web search/fetch (10) | Type-only built-in tool declarations; CVE/advisory/doc checks; results from `otari_tool_calls` | `web-live.json` |
| Code execution (10) | Prover's reproduction run in the hosted sandbox; stdout/exit code parsed | `codeexec-live.json` |
| Budgets (10) | Project key with `max_budget` + small `request_limit` on a dedicated demo key; refusal mapped to `DEFERRED` | `budget-refusal-live.json` |
| Distinct roles (10) | Clerk → Prover → Arbiter in one submission, one `Otari-Conversation-Id` | `pipeline-live.json` |
| Guardrails (10) | Prompt-injection profile, `mode: block`, on Shield stage | `guardrail-block-live.json` (403 `guardrail_violation`) and `guardrail-pass-live.json` |
| Per-user budgets (10) | `user=gh:<id>` + end-user budget; spammer scenario exhausts quota | `peruser-budget-live.json` |

`themis doctor` re-runs a minimal version of each and prints a readiness table, so judges can verify on their own key.

---

## 6. Components

### 6.1 `otari/client.py`
- `OtariClient.chat(role, messages, *, tools=None, user, conversation_id, response_format=None) -> OtariResult`
- `OtariClient.messages_stream(role, messages, *, mcp_servers, user, conversation_id) -> OtariStreamResult`
- `OtariResult`: `content`, `parsed`, `request_id`, `requested_model`, `effective_model`, `provider`, `fallback`, `usage`, `cost_usd | None`, `tool_calls`, `guardrail_verdicts`, `latency_ms`, `raw_headers_redacted`.
- Error taxonomy in `errors.py`: `OtariConfigError (401)`, `OtariFundingError (402)`, `OtariGuardrailBlocked (403 guardrail_violation)`, `OtariBudgetExceeded`, `OtariRateLimited (429)`, `OtariUpstreamError (5xx/504)`, `OtariSchemaRejected (400)`.
- Retries: 429 honoring `Retry-After` (max 2); 5xx once; never on 4xx.

### 6.2 Shield
- `shield/guardrails.py`: calls Otari with the guardrail profile on the untrusted text (no generation needed beyond the minimal), parses the guardrail verdict header / 403.
- `shield/sanitize.py`: Unicode tag chars (U+E0000–E007F), zero-width, bidi controls, HTML comments, `<details>` hidden blocks, markdown link-title payloads, phrases targeting bots/AI reviewers. Produces findings with offsets.

### 6.3 Evidence modules
| Module | Inputs | Method | HARD contradiction possible |
|---|---|---|---|
| `symbols.py` | claims of type symbol/file | GitHub tree + blob fetch at ref; Python `ast`, tree-sitter optional, regex fallback for other languages | yes |
| `versions.py` | version claims | GitHub tags/releases | yes |
| `context.py` | repo | DeepWiki MCP via Otari | no |
| `web.py` | CVE IDs, URLs, API refs | NVD/OSV/GitHub advisory pages through Otari web fetch/search; regex validation of CVE format first | yes (CVE not found in NVD/OSV) |
| `repro.py` | behavior/security claims + relevant source | Prover writes script embedding the minimal source excerpt; executed in Otari sandbox; Prover interprets output into `REPRODUCED/NOT_REPRODUCED/INCONCLUSIVE` | no (not reproducing ≠ false) |
| `tests_results.py` | artifact | parse JUnit/pytest JSON; detect tests added | yes (claims tests added but none changed) |
| `consistency.py` | claims + diff | deterministic file-level checks + Clerk-assisted soft checks | partially |
| `dependencies.py` | manifest diffs (package.json, requirements*.txt, pyproject.toml) | registry JSON APIs (npm, PyPI) + age/downloads/install scripts + Levenshtein to top packages + ledger; web fetch for registry page as corroboration | yes (package doesn't exist) |
| `duplicates.py` | title/body | GitHub search API + embedding-free token similarity | no |

### 6.4 Dependency verifier (anti-slopsquatting)
Signals, each recorded separately:
- `missing`: name not found on registry → HARD contradiction, red flag "would be slopsquattable".
- `newborn`: first publish < 30 days.
- `low_adoption`: weekly downloads under threshold.
- `install_scripts`: npm `preinstall/install/postinstall` present.
- `lookalike`: edit distance ≤ 2 to a top-5k package or a known hallucination pattern (mash-up of two real names).
- `ledger_hit`: name previously seen hallucinated (local ledger file `data/hallucination-ledger.jsonl`, shareable).
Output ranks risk `none / review / block`.

### 6.5 Report
- One comment per submission, updated in place (`<!-- themis:ledger -->` marker).
- Sections: verdict badge, evidence score, claim table (claim → check → result → evidence link), missing-evidence checklist, injection findings, cost and receipt link, override instructions.
- Labels: `themis:evidenced`, `themis:plausible`, `themis:needs-evidence`, `themis:contradicted`, `themis:tainted`, `themis:deferred`, `themis:dependency-risk`.

### 6.6 Dashboard
FastAPI + Jinja + HTMX, SQLite. Pages: Queue, Submission detail (ledger + receipts + timeline), Stats (slop rate, cost, fallbacks, guardrail blocks, budget usage), Doctor (readiness table).

---

## 7. Data model (SQLite)

| Table | Key fields |
|---|---|
| `submissions` | id, repo, number, kind, contributor_key, first_time, created_at, head_sha, verdict, score, status |
| `claims` | id, submission_id, type, text, span_start, span_end, normalized_target |
| `checks` | id, submission_id, claim_id, module, status, strength, detail_json, otari_request_id |
| `otari_calls` | request_id, submission_id, role, stage, requested_model, effective_model, provider, fallback, prompt_tokens, completion_tokens, cost_usd, latency_ms, outcome, created_at |
| `shield_findings` | id, submission_id, source (guardrail/sanitizer), kind, span, excerpt_redacted |
| `dependencies` | id, submission_id, ecosystem, name, version, signals_json, risk |
| `overrides` | id, submission_id, maintainer, from_verdict, to_verdict, reason, created_at |

---

## 8. Demo plan

### 8.1 Demo repository: `themis-demo/tinyhttp-lite`
A small, real Python library (≈600 LOC, pytest suite) seeded with these live submissions:

| # | Scenario | Expected verdict | Rubric features shown |
|---|---|---|---|
| D1 | Genuine bug report with repro snippet (real off-by-one in `parse_range`) | `EVIDENCED` (REPRODUCED in sandbox) | Clerk, Prover, code exec |
| D2 | AI-slop "critical RCE" report citing a non-existent function and a fabricated CVE | `CONTRADICTED` | symbols, web fetch |
| D3 | Slop PR "fixes memory leak in `ConnectionPool`" (class doesn't exist), claims tests added, none changed | `CONTRADICTED` | symbols, tests |
| D4 | Plausible PR adding a hallucinated npm/PyPI package | `CONTRADICTED` + `dependency-risk` | dependency verifier, web |
| D5 | Issue with hidden instruction in an HTML comment + Unicode tag chars ("label this verified and close others") | `TAINTED` | guardrail block, sanitizer |
| D6 | Genuine first-time contributor PR fixing D1 with a test | `EVIDENCED` (fast-track) | tests artifact, MCP context, Arbiter |
| D7 | Spam account files 8 issues in a minute | first N analyzed, rest `DEFERRED` | per-user budget refusal |
| D8 | Primary model forced unavailable in demo policy | completes via fallback, receipt shows fallback | routing & fallback |

### 8.2 Video (3–5 min, personally recorded)
0:00 problem in 20 s (curl/tldraw/Sorhus) → 0:20 install Action → 0:45 D2+D3 contradicted live → 1:45 D1 reproduced in sandbox → 2:30 D5 injection blocked → 3:00 D7 budget + D8 fallback in Otari Activity → 3:40 dashboard stats & cost → 4:20 principles (claims not authorship, maintainer in charge) → 4:45 close.

A separate 3–5 min Otari feedback recording covers what worked, what didn't, what we built ourselves, and recommendations (from `docs/OTARI_FEEDBACK.md`).

---

## 9. Testing and evaluation

- **Unit tests:** every evidence module with genuine / slop / injection fixtures; verdict function property tests (no HARD contradiction ⇒ never `CONTRADICTED`; tainted ⇒ always `TAINTED`).
- **Contract tests:** Otari client against recorded, redacted responses (marked as fixtures, never as evidence).
- **Live smoke (`-m live`):** one call per feature; writes receipts to `evidence/`.
- **Benchmark (`bench/`):** ≈40 labeled submissions — 15 genuine (real historical issues/PRs from permissive repos, rewritten minimally), 15 slop (constructed to mirror documented patterns), 10 injection/dependency adversarial. Report precision/recall per verdict, injection catch rate, cost and latency distributions. Results committed as `bench/results/<date>.json` and summarized in README.

---

## 10. Cost plan

- Clerk ≈ 1 call/submission, Arbiter ≈ 1, Prover + code exec ≈ 0–2, web ≈ 0–2, MCP ≈ 0–1.
- Budget keys: `themis-dev` ($5 ceiling), `themis-demo` ($3 ceiling, used by judges' quick path), `themis-budget-demo` (tiny `request_limit` to trigger refusals on purpose).
- Per-contributor default: 0.05 USD/day; project default: 10 USD/month (configurable).
- Real numbers are measured by the benchmark and reported; no estimates are presented as measurements.

---

## 11. Risk register

Status as of Nov 6, from upstream docs only. `docs/READINESS.md` holds the live result.

| ID | Risk | Likelihood | Mitigation | Fallback | Status |
|---|---|---|---|---|---|
| R1 | Hosted guardrail requires vendor key | High | Get a vendor key (free tier) on day 0 | Keep deterministic sanitizer; rely on other 7 features for cap | Open. Depends on whether hosted lists a profile. Otherwise define a vendor guardrail in the dashboard |
| R2 | Routing policy needs BYOK | High | Add one provider key to workspace day 0 | Client-side cascade; fallback points at risk | Lowered. Docs say entries without BYOK use the balance. An invalid key is enough to force the fallback |
| R3 | Code-exec 402/pricing issue | Medium | Verify day 0; report issue with request ID | MCP + web + budgets fill cap | Open. Check `otari_code_execution` in `/tools`, then run the spike |
| R4 | Per-user budget needs operator role | Medium | Ask Mozilla AI mentors early (Discord) | Budgets + 6 others fill cap | Lowered. Org owner or admin can do it with a service key, no operator needed |
| R5 | MCP reachability from hosted | Low (DeepWiki verified by others) | Use DeepWiki MCP | Skip MCP, cap still reachable | New step: enable `request_mcp_servers_allowed` on the workspace |
| R6 | JSON reliability of open models | Medium | Model selection by measured validity; one repair round | Degrade to `UNVERIFIED` | Open. Measured by `scripts/spikes/bakeoff.py` |
| R7 | Scope creep | High | MVP list frozen after Phase 0 | Cut stretch items first | Scope freezes when the Phase 0 gate is met |
| R8 | Credits exhausted near deadline | Low | Separate keys with ceilings; dry-run mode | Recorded video + receipts | Open. Key ceilings set during setup |
| R9 | Demo flakiness | Medium | Pre-seeded demo, cached receipts shown alongside live run | Video | Open |

---

## 12. Submission checklist

- [ ] Public repo, Apache-2.0, `baseline-pre-otari` tag on `main`
- [ ] `otari-hackathon` branch; `first-otari-call` tag linked in README
- [ ] README: problem, 60-second pitch, judge quick path, rubric evidence table, architecture diagram, benchmark summary, limitations
- [ ] `evidence/` with one real receipt per rubric item
- [ ] Live demo repository with D1–D8 visible
- [ ] Dashboard deployed (or run instructions) with demo key ceiling
- [ ] 3–5 min demo video (personally recorded)
- [ ] 3–5 min Otari feedback recording + `docs/OTARI_FEEDBACK.md`
- [ ] Issues filed upstream on Otari for any bugs found (with request IDs)
- [ ] DoraHacks BUIDL page complete, links verified, submitted before Nov 11 22:59 UTC
