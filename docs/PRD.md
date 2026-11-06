# PRD — THEMIS

**Evidence, not opinions, for every contribution.**

| Field | Value |
|---|---|
| Product | THEMIS — evidence-first contribution triage for open-source maintainers |
| Event | Mozilla MozFest "Developers Wilding: Online" (DoraHacks), Oct 28 – Nov 11, 2026 |
| Deadline | Nov 11, 2026, 22:59 UTC |
| Platform requirement | Hosted Otari gateway (Mozilla AI), open-weight models |
| License | Apache-2.0 |
| Status | Draft v1.0 |

---

## 1. Problem

AI tools have made pull requests, issues and security reports nearly free to produce, while reviewing them is still slow, manual and expensive. The open-source social contract is breaking under that asymmetry:

- A 2026 Linux Foundation research report (funded by Red Hat) estimates that roughly **25% of incoming PRs are low-quality AI slop, rising to ~31% on large projects**.
- **curl shut down its bug bounty program** (Feb 2026) after useful vulnerability reports fell from about 15% to 5% of submissions.
- **tldraw auto-closes all external PRs**, **Ghostty** restricts AI-generated PRs to accepted issues, and on **Oct 1, 2026 Sindre Sorhus disabled external PRs** on all his repositories.
- GitHub has added options to restrict or disable pull requests entirely.

The current responses — closing the door, banning AI, or "AI detectors" — all **destroy the commons**: they punish genuine newcomers and honest AI-assisted contributors along with the spam.

The real bottleneck is not *who* wrote a submission. It is the cost of **verifying whether its claims are true**: does the function it mentions exist? Does the bug reproduce? Do the tests pass? Does the "vulnerability" cite a real CVE? Does the added package really exist, or is it a hallucinated name (slopsquatting)? Is the text trying to manipulate an AI reviewer?

A second, related threat makes this urgent: **slopsquatting**. Open-source models hallucinate non-existent package names at an average rate of ~21.7% (commercial ~5.2%), and attackers pre-register those names. AI-generated PRs that add dependencies are a direct supply-chain vector into maintained projects.

## 2. Vision

Restore the cheap, trustworthy filter that open source used to have — without closing the door. Every submission arrives with an **evidence ledger**: each claim, how it was checked, what happened, and a receipt proving the check really ran. Maintainers review evidence, not walls of plausible text. Honest contributors (human or AI-assisted) are fast-tracked because their claims check out.

This is "Wilding" in the MozFest sense: instead of rigid closure, build care-based infrastructure that lets an open community keep evolving.

## 3. Principles

1. **Claims, not authorship.** No AI-text detection. Ever.
2. **Execution beats opinion.** A reproduced bug outranks any model's judgment.
3. **Deterministic verdicts.** Models extract and explain; code decides.
4. **The maintainer stays in charge.** Advisory by default; no auto-close.
5. **Fair to newcomers.** Identity never lowers a verdict.
6. **Untrusted by default.** Submission text is data, never instructions.
7. **Open and accountable.** Open-weight models, open source, every result has a gateway receipt.
8. **Frugal.** Cheap model first, expensive work only for submissions that earn it; hard budgets per contributor and per project.

## 4. Users and personas

| Persona | Context | Job to be done |
|---|---|---|
| **Maya — solo maintainer** | Popular library, 2–5 h/week, 40 new items/week | "Tell me which of these 40 items are real, and show me why, in under a minute each." |
| **Security triage team** | Mid-size project with a disclosure inbox | "Discard hallucinated vulnerability reports without missing the real one." |
| **First-time contributor (Leo)** | Uses an AI assistant, submits a genuine fix | "Get my PR seen instead of auto-closed; know what evidence to add." |
| **Foundation / OSPO** | Runs many repos | "One budget, consistent policy, visibility into cost and slop rates." |

## 5. Goals and non-goals

### Goals (hackathon MVP)
- G1. Triage PRs, issues and security-style reports on GitHub and produce an evidence ledger comment + labels within 3 minutes median.
- G2. Detect the most common slop patterns deterministically: non-existent symbols/files/versions, fabricated CVEs, claim/diff mismatch, failing or missing tests, hallucinated dependencies, duplicates.
- G3. Reproduce bug/vulnerability claims in Otari's hosted code-execution sandbox when feasible.
- G4. Block prompt injection aimed at AI reviewers.
- G5. Enforce per-contributor and per-project spending limits through Otari.
- G6. Score the maximum 100 points on the hackathon rubric with real, receipted Otari evidence.

### Non-goals
- Detecting AI-written text.
- Automatically fixing PRs or writing code for contributors.
- Replacing code review or CI.
- Supporting GitLab/Codeberg in the MVP (designed for, not built).
- Running arbitrary PR code on THEMIS servers.

## 6. User stories

| ID | As a… | I want… | So that… |
|---|---|---|---|
| US-1 | maintainer | every new PR/issue to get an evidence ledger automatically | I can triage without reading everything |
| US-2 | maintainer | references to files/functions/versions verified against the repo | invented code paths are flagged instantly |
| US-3 | security triager | claimed vulnerabilities reproduced in a sandbox when possible | I only escalate real issues |
| US-4 | security triager | cited CVEs and advisories verified on the web | fabricated citations are exposed |
| US-5 | maintainer | new dependencies in a PR checked for existence, age and suspicious signals | slopsquatted packages never get merged |
| US-6 | maintainer | hidden instructions targeting AI reviewers detected and quarantined | my tooling can't be hijacked |
| US-7 | maintainer | a hard spending cap per contributor and per project | spammers can't drain my budget |
| US-8 | maintainer | a queue in a dashboard sorted by evidence strength | I review the most credible items first |
| US-9 | contributor | a clear, polite list of the evidence my submission is missing | I can fix it instead of being ignored |
| US-10 | maintainer | to contest/override any THEMIS verdict with one click (`/themis override`) | the human always has the last word |
| US-11 | OSPO | cost and slop-rate stats per repo | I can justify and tune the policy |
| US-12 | judge | a reproducible demo and real Otari receipts | I can verify every claim the project makes |

## 7. Functional requirements

### 7.1 Intake
- FR-1 Ingest GitHub `pull_request`, `issues` and `issue_comment` (`/themis recheck`, `/themis override`) events via a GitHub Action (MVP) and a CLI (`themis check <url>`).
- FR-2 Normalize into a `Submission` (kind, title, body, diff summary, changed files, referenced commit, contributor key `gh:<id>`, first-time flag).
- FR-3 Classify submission kind: `bug_report`, `security_report`, `feature_request`, `bugfix_pr`, `feature_pr`, `docs_pr`, `dependency_pr`, `question`, `other`.

### 7.2 Shield (prompt-injection defense)
- FR-4 Run an Otari inference guardrail (prompt-injection profile, `block` mode) on all untrusted text before any model sees it.
- FR-5 Run a deterministic sanitizer in parallel: hidden Unicode (tags block, zero-width, bidi overrides), HTML comments, collapsed `<details>`, instruction-like patterns aimed at AI/bots, invisible markdown. Findings are evidence, not just filters.
- FR-6 On a guardrail block → verdict `TAINTED`, no further model processing, maintainer notified with the exact offending span (rendered safely).

### 7.3 Claim extraction (Clerk role)
- FR-7 Extract a list of atomic, falsifiable claims with types: `symbol_exists`, `file_exists`, `version_exists`, `behavior` (bug reproduces), `fixes_issue`, `test_added`, `cve_reference`, `dependency_added`, `performance`, `other`.
- FR-8 Each claim records source span offsets so the ledger can quote it.

### 7.4 Evidence engine
- FR-9 **Symbol check (deterministic):** verify referenced files, functions, classes, CLI flags and line ranges exist at the referenced commit or default branch (GitHub API tree + lightweight parsers).
- FR-10 **Version check (deterministic):** verify claimed versions/tags exist.
- FR-11 **Repo context (MCP):** fetch repository structure/docs through an MCP server (DeepWiki MCP; GitHub MCP when available) to ground claims.
- FR-12 **Web verification:** verify CVE IDs, advisories, upstream docs and API references through Otari built-in web search/fetch.
- FR-13 **Reproduction (Prover role + Otari code execution):** for `behavior`/`security_report` claims on self-contained code, generate a minimal reproduction and execute it in the Otari sandbox; record stdout, exit code, and whether the observed behavior matches the claim.
- FR-14 **Test results:** consume the secret-less test workflow's results artifact (pass/fail, tests added, coverage delta if available).
- FR-15 **Claim/diff consistency:** compare PR description claims to the actual diff (e.g., "adds tests" but no test files changed).
- FR-16 **Dependency verifier:** for every dependency added in a diff (npm, PyPI, crates, Go modules): existence, first-publish age, downloads, maintainer count, install-script presence, name-similarity to popular packages, and presence in the THEMIS hallucination ledger.
- FR-17 **Duplicate detection:** search existing issues/PRs for near-duplicates.

### 7.5 Verdict
- FR-18 Per-claim status: `REPRODUCED`, `CONFIRMED`, `CONTRADICTED`, `UNVERIFIED`, `NOT_APPLICABLE`.
- FR-19 Submission verdict (deterministic): `EVIDENCED`, `PLAUSIBLE`, `NEEDS_EVIDENCE`, `CONTRADICTED`, `TAINTED`, `DEFERRED` (budget), plus an evidence score 0–100.
- FR-20 Arbiter role (different model family from Clerk/Prover) writes a short, neutral explanation from *structured evidence only* — it never sees raw untrusted text.

### 7.6 Reporting and control
- FR-21 Post/update a single ledger comment and a GitHub check run; apply `themis:*` labels.
- FR-22 Generate a contributor-facing "what evidence is missing" checklist for `NEEDS_EVIDENCE`.
- FR-23 Commands: `/themis recheck`, `/themis override <verdict> <reason>` (maintainers only), `/themis explain`.
- FR-24 Every ledger links to its receipt (JSON with Otari request IDs, models, providers, costs).

### 7.7 Budgets and routing
- FR-25 Per-contributor budget (Otari per-user / end-user budget) with daily cap; exhausting it yields `DEFERRED` and a maintainer-approval path, never a negative verdict.
- FR-26 Project budget via an Otari key spending ceiling (`max_budget` and/or `request_limit`).
- FR-27 Cascade: cheap Clerk first; Prover/web/code-exec only for submissions that pass Shield and have checkable claims.
- FR-28 Otari routing policy with fallback for each role; record when fallback happened.

### 7.8 Dashboard
- FR-29 Queue view sorted by evidence score, filters by verdict/kind, per-item ledger, receipts, cost.
- FR-30 Stats: slop rate, cost per submission, budget usage, fallback count, guardrail blocks.

## 8. Non-functional requirements

| Area | Requirement |
|---|---|
| Latency | Median end-to-end ≤ 3 min per submission; Shield + Clerk ≤ 20 s |
| Cost | Target median ≤ $0.02 per submission (to be measured and reported) |
| Precision | **Zero** `CONTRADICTED` verdicts on genuine submissions in the benchmark (protect honest contributors); false negatives preferred over false positives |
| Security | Least-privilege GitHub token; no PR code on secret-holding runners; all secrets redacted |
| Privacy | Only numeric GitHub IDs sent to Otari as `user`; no emails/usernames |
| Transparency | Every model-backed result has a gateway receipt; prompts are versioned |
| Portability | Runs as a GitHub Action, CLI or small server; SQLite by default |
| Accessibility | Ledger comments readable as plain markdown; dashboard WCAG AA |

## 9. Hackathon rubric mapping (target 100/100)

| Rubric item | Pts | THEMIS implementation |
|---|---|---|
| Meaningful open-weight model | 20 | Clerk (claim extraction/classification) on an open-weight model |
| Second open-weight model, distinct role | 10 | Prover (repro code) and Arbiter (explanation) on different open-weight families |
| Model routing & fallbacks | 10 | Otari routing policy per role with `on_failure`; fallback recorded in receipts |
| MCP integrations | 10 | Repo context via DeepWiki MCP (streamed Messages) |
| Web search / fetch | 10 | CVE/advisory/doc verification via Otari built-in tools |
| Code execution | 10 | Reproductions run in Otari's hosted sandbox |
| Budgets / spending limits | 10 | Project key ceiling; verified `budget_exceeded` refusal handled as `DEFERRED` |
| Multiple distinct model roles | 10 | Clerk → Prover → Arbiter pipeline |
| Inference guardrails | 10 | Prompt-injection profile in block mode on all untrusted text |
| Per-user budgets | 10 | Per-contributor budgets via `user` / end-user budget header |
| Working real use case | 5 | Live demo repository with real submissions |
| Recorded demo | 5 | 3–5 min personally recorded walkthrough |

Eight advanced features are implemented; the best six count (cap 60). Implementing all eight is deliberate insurance against any hosted feature being unavailable on demo day.

## 10. Success metrics

| Metric | Target |
|---|---|
| Rubric evidence | 100/100 with a real receipt per item |
| Benchmark precision on `CONTRADICTED` | 100% (no genuine item contradicted) |
| Benchmark recall on slop | ≥ 70% flagged `CONTRADICTED` or `NEEDS_EVIDENCE` |
| Injection catch rate on benchmark | ≥ 90% |
| Hallucinated dependencies caught | 100% of seeded cases |
| Median cost / submission | ≤ $0.02, reported honestly |
| Median latency | ≤ 3 min |

## 11. Scope

**MVP (must ship):** Action + CLI, Shield, Clerk, symbol/version/test/consistency checks, web verification, code-exec reproduction, dependency verifier (npm + PyPI), Arbiter, deterministic verdict, ledger comment + labels, budgets, routing/fallback, MCP context, receipts, dashboard (read-only), demo repo, benchmark, video.

**Stretch:** GitHub App mode, shared community hallucination ledger service, crates/Go ecosystems, contributor appeal flow UI, Codeberg/Forgejo adapter, weekly maintainer digest.

## 12. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Hosted guardrails need a third-party vendor key | Lose 10 pts | Obtain a vendor key on day 1; deterministic sanitizer still protects users; per-user budgets + other features cover the cap |
| Hosted routing policies only route across own provider keys | Lose fallback points | Bring one provider key (BYOK) into the workspace on day 1; client-side cascade as product behavior |
| Code execution pricing errors on hosted (previously reported 402) | Lose 10 pts | Verify on day 1; MCP/web/budget features cover the cap |
| Sandbox can't reproduce repo-dependent bugs | Weaker evidence | Limit repro to self-contained claims; rely on test workflow for the rest; mark `UNVERIFIED` honestly |
| False positive harms a genuine contributor | Reputation | Precision-first scoring, advisory mode, override command, newcomer protection |
| Prompt injection through diff/code comments | Hijacked pipeline | Data envelope, Arbiter never sees raw text, deterministic labels, guardrail in block mode |
| Budget/credit runs out during judging | Broken demo | Separate demo key with ceiling; recorded video; dry-run mode |

## 13. Open questions

1. Can the hosted per-user budget be created with the workspace's own role, or does it require operator setup? Upstream docs: an organization owner or admin can, through a service key. Live confirmation pending in `docs/READINESS.md`.
2. Which open-weight models on hosted Otari are most reliable for structured JSON at low latency? (Measure in Phase 0.)
3. Is GitHub MCP reachable from hosted Otari (Streamable HTTP + auth), or is DeepWiki MCP the only option for the MVP?
