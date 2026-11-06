# PHASES — THEMIS Delivery Schedule

Hard deadline: **Nov 11, 2026, 22:59 UTC** (Nov 12, 01:59 Europe/Istanbul).
Internal submission target: **Nov 11, 18:00 UTC** (5 h safety buffer).
All times below are UTC.

| Phase | Window | Theme | Exit gate |
|---|---|---|---|
| 0 | Nov 6, 16:30 → Nov 7, 06:00 | Platform verification & baseline | Readiness table: every Otari feature live-verified or explicitly blocked |
| 1 | Nov 7, 06:00 → 14:00 | Skeleton & Otari client | `first-otari-call` tag; `themis doctor` green |
| 2 | Nov 7, 14:00 → Nov 8, 06:00 | Intake, Shield, Clerk | Claims extracted from 10 fixtures; injection fixtures blocked |
| 3 | Nov 8, 06:00 → Nov 9, 06:00 | Evidence engine | All evidence modules pass genuine/slop/injection fixtures |
| 4 | Nov 9, 06:00 → 18:00 | Verdict, Arbiter, Report | End-to-end ledger posted on a real GitHub issue |
| 5 | Nov 9, 18:00 → Nov 10, 10:00 | Budgets, routing, dashboard | Budget refusal, per-user budget and fallback receipts captured |
| 6 | Nov 10, 10:00 → Nov 11, 02:00 | Demo repo, benchmark, hardening | D1–D8 live; benchmark results committed |
| 7 | Nov 11, 02:00 → 18:00 | Docs, recordings, submission | DoraHacks submission complete |

---

## Phase 0 — Platform verification & baseline
**Goal:** know exactly what hosted Otari can do for us before writing product code.

Tasks
- [ ] Create public repo `themis`, Apache-2.0, minimal README; commit and tag **`baseline-pre-otari`** on `main` (no Otari code).
- [ ] Create branch `otari-hackathon`.
- [ ] Otari account, fund workspace, create keys: `themis-dev`, `themis-demo`, `themis-budget-demo`.
- [ ] Join Otari Discord; introduce the project; ask about per-user budget setup and guardrail vendor requirements.
- [ ] `GET /models`, `GET /tools`; save redacted catalogs.
- [ ] Spike scripts (`scripts/spikes/*.py`), one live call each, receipts saved:
  - [ ] chat completion with open-weight model A (JSON output)
  - [ ] chat completion with open-weight model B (different family)
  - [ ] built-in web search + web fetch (type-only declarations)
  - [ ] built-in code execution (stdout + exit code)
  - [ ] streamed `/messages` with DeepWiki MCP
  - [ ] guardrail prompt-injection profile: one pass, one block
  - [ ] key `request_limit` budget refusal on `themis-budget-demo`
  - [ ] per-user budget with `user=gh:<id>`
  - [ ] routing policy with `on_failure` (add one BYOK provider key) and a forced fallback
- [ ] Model bake-off: 10 fixtures × 3–4 candidate models → JSON validity, latency, cost. Pick Clerk / Prover / Arbiter.
- [ ] Write `docs/OTARI_FEEDBACK.md` v0 with every surprise + request ID.
- [ ] Freeze MVP scope; update `PLAN.md §11` risk statuses.

Exit gate: readiness table with ✅ / ⛔ per rubric feature; at least 6 advanced features ✅.

Fallback if ⛔: R1–R5 mitigations in `PLAN.md §11`.

---

## Phase 1 — Skeleton & Otari client
Tasks
- [ ] `pyproject.toml` (uv), ruff, mypy strict, pytest, structlog, pydantic v2, httpx, typer, fastapi.
- [ ] `config.py` (env + `themis.yml`), `models.py` (domain models).
- [ ] `otari/client.py` with `chat` and `messages_stream`, header capture, error taxonomy, retries, timeouts.
- [ ] `otari/tools.py`, `otari/mcp.py`, `otari/budgets.py`.
- [ ] `store/` SQLite schema + redaction.
- [ ] `themis doctor` command (runs Phase 0 spikes as a readiness table).
- [ ] `themis evidence capture <feature>` writing redacted receipts to `evidence/`.
- [ ] First real Otari call committed → tag **`first-otari-call`**, link in README.
- [ ] `.otari/guardrails.yml` for agents working on this repo (no `.env` edits, no force-push, no direct provider SDKs).

Exit gate: `uv run themis doctor` green on `themis-dev`; unit tests for client error mapping.

---

## Phase 2 — Intake, Shield, Clerk
Tasks
- [ ] `intake/github.py`: issues, PRs (metadata, changed files, diff summary), comments; contributor key `gh:<id>`; first-time flag.
- [ ] `shield/sanitize.py`: Unicode tags, zero-width, bidi, HTML comments, hidden `<details>`, bot-targeted instructions; findings with offsets.
- [ ] `shield/guardrails.py`: Otari guardrail call, block/monitor handling, `TAINTED_UNCHECKED` when unavailable in block mode.
- [ ] `agents/prompts/clerk.md` (versioned) + `agents/clerk.py`: kind classification + atomic claims + spans, pydantic validation, one repair.
- [ ] Fixtures: 10 genuine, 10 slop, 6 injection.

Exit gate: Clerk JSON validity ≥ 95% on fixtures; all injection fixtures flagged by sanitizer and/or guardrail.

---

## Phase 3 — Evidence engine
Tasks
- [ ] `evidence/symbols.py` + `versions.py` (GitHub tree/tags, Python `ast`, regex fallback).
- [ ] `evidence/context.py` (DeepWiki MCP via streamed `/messages`).
- [ ] `evidence/web.py` (CVE format check → NVD/OSV/GHSA via web fetch/search; doc/API reference checks).
- [ ] `agents/prover.py` + `evidence/repro.py` (minimal repro scripts embedding source excerpts; Otari code execution; interpretation).
- [ ] `evidence/tests_results.py` + `action/tests` sub-action producing `themis-test-results.json`.
- [ ] `evidence/consistency.py` (claims vs diff; "tests added" vs changed files).
- [ ] `evidence/dependencies.py` (npm + PyPI registries, signals, ledger) + `themis deps` CLI.
- [ ] `evidence/duplicates.py`.
- [ ] Planner (`verdict/plan.py`): applicable checks + cost estimate vs remaining budget; cheap-first ordering.

Exit gate: each module has genuine/slop/injection tests passing; one live receipt each for MCP, web and code execution.

---

## Phase 4 — Verdict, Arbiter, Report
Tasks
- [ ] `verdict/scoring.py` deterministic function + property tests (no HARD ⇒ never `CONTRADICTED`; tainted ⇒ `TAINTED`).
- [ ] `verdict/receipts.py` (`themis.receipt.v1`).
- [ ] `agents/arbiter.py` (structured evidence only, neutral tone, contributor checklist).
- [ ] `report/github_comment.py` (single updatable ledger comment), check run, labels.
- [ ] Comment commands: `/themis recheck`, `/themis override`, `/themis explain` with maintainer authorization.
- [ ] `themis check <url>` end-to-end; `--dry-run`, `--no-post`, `--json`.
- [ ] `.github/workflows/themis-tests.yml` + `themis-analyze.yml` (two-workflow security split).

Exit gate: a ledger is posted automatically on a real issue and a real fork PR in a test repo.

---

## Phase 5 — Budgets, routing, dashboard
Tasks
- [ ] Per-contributor budgets wired (`user`, end-user budget header); `DEFERRED` path + `/themis recheck --approve`.
- [ ] Project ceiling on `themis-demo` key; refusal → `DEFERRED` with clear message.
- [ ] Routing policies `themis-clerk`, `themis-prover`, `themis-arbiter`, `themis-fallback-demo`; fallback captured in receipts.
- [ ] Dashboard: Queue, Detail, Stats, Doctor (FastAPI + Jinja + HTMX, SQLite).
- [ ] Capture receipts: `budget-refusal-live.json`, `peruser-budget-live.json`, `routing-live.json`.

Exit gate: all 8 advanced features have a real receipt in `evidence/` (or a documented ⛔ with request ID and upstream issue link).

---

## Phase 6 — Demo repository, benchmark, hardening
Tasks
- [ ] Create demo repo `tinyhttp-lite` (≈600 LOC + pytest), install THEMIS Action.
- [ ] Seed scenarios D1–D8 (see `PLAN.md §8.1`) from separate test accounts where needed; verify each verdict.
- [ ] Build `bench/dataset` (~40 labeled items) and run `themis bench --live`; commit `bench/results/<date>.json`.
- [ ] Tune scoring weights only on a held-out split; confirm zero false `CONTRADICTED` on genuine items.
- [ ] Failure drills: gateway 5xx, guardrail unavailable, budget exhausted, invalid JSON, GitHub API rate limit.
- [ ] Security review: workflow permissions, no PR checkout with secrets, redaction of receipts, no secrets in git history.

Exit gate: D1–D8 show expected verdicts live; benchmark targets in `PRD.md §10` met or honestly reported.

---

## Phase 7 — Documentation, recordings, submission
Tasks
- [ ] README: 60-second pitch, problem evidence with links, architecture diagram, judge quick path, rubric evidence table, benchmark summary, limitations, roadmap.
- [ ] `evidence/README.md` index (rubric item → receipt → request ID).
- [ ] Finalize `docs/OTARI_FEEDBACK.md`; file upstream Otari issues for bugs found (with request IDs).
- [ ] Record **demo video (3–5 min)** following `PLAN.md §8.2` — personally recorded, real screen, real calls.
- [ ] Record **Otari feedback video (3–5 min)**.
- [ ] Final pass: links, license, `.env.example`, tags `baseline-pre-otari` and `first-otari-call`, branch `otari-hackathon`.
- [ ] Fill DoraHacks BUIDL page (description, repo, demo repo, videos, team), submit by **18:00 UTC**.
- [ ] Post-submit smoke: open one new issue on the demo repo and confirm a ledger appears.

Exit gate: submission confirmed on DoraHacks before 22:59 UTC.

---

## Cut list (in order, if behind schedule)
1. Dashboard Stats page (keep Queue + Detail).
2. Duplicate detection.
3. `/themis explain` command.
4. Benchmark size reduced to ~20 items.
5. crates/Go ecosystems (already stretch).
6. Never cut: Shield, deterministic verdict, receipts, two-workflow security split, demo video.

## Daily ritual
- Start: `themis doctor` on `themis-dev`; check balance and budget keys.
- End: commit, push, update `docs/OTARI_FEEDBACK.md`, tick checklist items, re-check the time left to the deadline.
