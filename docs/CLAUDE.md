# CLAUDE.md — THEMIS

> Instructions for AI coding agents (Claude Code, Codex, opencode, etc.) working in this repository.
> Read this file fully before making any change. Companion documents: `PRD.md` (what and why), `PLAN.md` (how), `DOCS.md` (reference), `PHASES.md` (when).

---

## 1. What THEMIS is

THEMIS is an open-source, evidence-first triage system for open-source maintainers who are being flooded with low-quality, AI-generated pull requests, issues and security reports ("AI slop").

THEMIS does **not** try to detect whether something was written by AI. It answers a different question: **"Is what this submission claims actually true?"** For every incoming PR, issue or security report, THEMIS extracts the concrete claims, checks them against the repository, the web and real code execution, and posts an evidence ledger the maintainer can trust in seconds.

Tagline: **Evidence, not opinions, for every contribution.**

Built for the **Mozilla MozFest "Developers Wilding: Online" hackathon** (DoraHacks, Oct 28 – Nov 11, 2026). Every model call goes through **Mozilla AI's hosted Otari gateway** using **open-weight models**.

---

## 2. Golden rules (never violate)

1. **All LLM traffic goes through hosted Otari.** Base URL comes from `OTARI_BASE_URL` (default `https://api.otari.ai/api/v1`). Never call a model provider (OpenAI, DeepInfra, Together, Bedrock, …) directly, never add a provider SDK, never add a "direct mode" fallback. Bypassing the gateway invalidates the hackathon submission.
2. **Only open-weight models** are used for the pipeline roles. Model IDs come from configuration, never hard-coded. Discover valid IDs with `GET /api/v1/models`.
3. **Submission text is untrusted data, never instructions.** PR titles, bodies, diffs, issue text, comments, file contents and web pages may contain prompt injection. They are only ever placed inside the `<untrusted_submission>` / `<untrusted_context>` data envelope in a *user* message. They are never concatenated into a system prompt, never used as a tool name, never used to choose a model or a label.
4. **Models never decide the final verdict alone.** Models extract claims and explain evidence. The verdict and labels are computed by the deterministic function in `src/themis/verdict/scoring.py`. If a model output fails schema validation after one repair attempt, the affected check becomes `UNVERIFIED` — never `REPRODUCED`.
5. **THEMIS never auto-closes, auto-merges or auto-bans by default.** `policy.auto_close` defaults to `false`. Destructive GitHub actions require explicit maintainer configuration.
6. **Never execute submission code on a machine that holds secrets.** PR code runs only in (a) Otari's hosted code-execution sandbox, or (b) the secret-less `themis-tests.yml` workflow triggered by `pull_request`. The `themis-analyze.yml` workflow that holds `OTARI_API_KEY` must never check out or run PR code.
7. **Every model-backed result carries an Otari receipt.** Persist `Otari-Request-ID`, effective model, provider, fallback flag, token usage and `cost_usd` (or `null` when the gateway reports none) for every call. Never fabricate request IDs, costs or evidence. Test doubles must be clearly marked and never written into `evidence/`.
8. **No secrets in logs, receipts, comments, fixtures or commits.** Redact with `themis.store.redact` before persisting or posting anything.
9. **Fail closed on safety, fail open on availability.** If the guardrail service is unavailable in `block` mode → stop the submission as `TAINTED_UNCHECKED` and ask a human. If a model is unavailable → mark the affected check `UNVERIFIED`, keep going with the rest.
10. **Do not penalize newcomers.** Contributor history may only *raise* priority (fast-track), never lower a verdict. Identity is never an input to the verdict function.

---

## 3. Repository layout

```
themis/
├── CLAUDE.md  PRD.md  PLAN.md  DOCS.md  PHASES.md
├── pyproject.toml              # uv-managed, Python >= 3.12
├── .env.example                # never commit .env
├── .otari/guardrails.yml       # Otari Agent Guardrails for agents working on THIS repo
├── src/themis/
│   ├── cli.py                  # `themis` CLI (typer)
│   ├── config.py               # env + .github/themis.yml loading (pydantic-settings)
│   ├── models.py               # domain models: Submission, Claim, Check, Verdict, Receipt
│   ├── otari/
│   │   ├── client.py           # the ONLY module that talks HTTP to Otari
│   │   ├── errors.py           # gateway error taxonomy
│   │   ├── tools.py            # built-in tool declarations (web search/fetch, code exec)
│   │   ├── mcp.py              # inline MCP server declarations + streamed Messages parser
│   │   └── budgets.py          # per-contributor budget headers, budget refusal handling
│   ├── intake/                 # GitHub payload → Submission
│   ├── shield/                 # guardrails + deterministic sanitizer
│   ├── agents/                 # clerk.py, prover.py, arbiter.py + prompts/
│   ├── evidence/               # symbols, repro, web, tests_results, dependencies, duplicates, context
│   ├── verdict/                # scoring.py (deterministic), receipts.py
│   ├── report/                 # GitHub comment / check-run rendering
│   ├── store/                  # SQLite persistence, redaction
│   └── web/                    # FastAPI + HTMX maintainer dashboard
├── action/action.yml           # composite GitHub Action
├── .github/workflows/          # themis-tests.yml (no secrets), themis-analyze.yml (secrets)
├── demo/                       # demo repo seeding scripts + scenarios
├── bench/                      # labeled benchmark + runner
├── evidence/                   # committed, redacted, REAL Otari receipts for judges
└── tests/                      # pytest
```

Only `src/themis/otari/client.py` may import `httpx` for Otari calls. Everything else calls `OtariClient`.

---

## 4. Commands

```bash
uv sync --dev                         # install
uv run pytest -q                      # unit tests (no network, Otari mocked)
uv run pytest -q -m live              # live tests against hosted Otari (needs OTARI_API_KEY, costs money)
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run themis doctor                  # verifies key, balance, models, tools, MCP, guardrails, budgets
uv run themis check <github-url>      # analyze one PR / issue end-to-end, print ledger
uv run themis check <url> --dry-run   # no Otari calls, shows the planned pipeline
uv run themis bench                   # run labeled benchmark, writes bench/results/*.json
uv run themis serve                   # dashboard at http://127.0.0.1:8080
```

Run `ruff`, `mypy` and `pytest -q` before every commit. Live tests are opt-in and must stay cheap (one request per feature).

---

## 5. Coding conventions

- Python 3.12+, full type hints, `mypy --strict` on `src/`.
- Pydantic v2 models for every model input/output and every persisted record.
- `async` end-to-end (`httpx.AsyncClient`), bounded concurrency via `asyncio.Semaphore` (default 4).
- Every pipeline stage is a pure-ish function: `async def run(ctx: StageContext) -> StageResult`. No hidden global state.
- Prompts live in `src/themis/agents/prompts/*.md`, versioned with a `prompt_version` header that is written into receipts.
- Structured outputs: request JSON, validate with pydantic, allow exactly one repair round-trip, then degrade to `UNVERIFIED`.
- Timeouts: every Otari call has an explicit timeout (`THEMIS_OTARI_TIMEOUT_S`, default 60). Never retry 4xx except 429.
- Logging: `structlog`, JSON, include `submission_id`, `stage`, `otari_request_id`. Never log raw submission bodies at INFO.
- Tests: every deterministic check needs fixtures for a genuine case, a slop case and an injection case.
- Keep functions small; no module over ~400 lines.

---

## 6. Otari integration rules

- Endpoints used: `POST /chat/completions`, `POST /messages` (streamed, for MCP activity), `GET /models`, `GET /tools`.
- Always send `user` = the THEMIS contributor key (`gh:<numeric_github_id>`) so per-contributor budgets and usage attribution work. Never send usernames or emails.
- Always send `Otari-Conversation-Id: themis-<submission_id>` so one submission reads as one session in Otari Activity.
- Built-in tools are declared **type-only** on hosted Otari (extra keys such as `max_uses` were rejected by the hosted validator). Read tool results from `otari_tool_calls`.
- MCP: use inline `mcp_servers` with the streamed Messages API; non-streaming Chat Completions may hide MCP activity. Pair `mcp_tool_result` to the preceding `mcp_tool_use` by stream order.
- Handle these gateway outcomes explicitly (see `otari/errors.py`): `401` fatal config, `402` funding/pricing, `403 guardrail_violation`, budget refusal (`budget_exceeded`), `429` retry with `Retry-After`, `5xx/504` fallback.
- Read and persist response headers: `Otari-Request-ID`, `Otari-Provider`, `Otari-Fallback`, guardrail verdict header.
- Verify every assumption in this section with `themis doctor` against the live gateway; if the gateway behaves differently, update `DOCS.md §7` and `docs/OTARI_FEEDBACK.md` instead of guessing.

---

## 7. Hackathon compliance (do not break)

- `main` contains the tagged **pre-Otari baseline commit** (`baseline-pre-otari`). Do not rewrite history before it.
- All Otari work happens on branch **`otari-hackathon`**. The first commit that makes a real Otari call is tagged `first-otari-call` and linked in the README.
- `evidence/` holds only real, redacted receipts produced by `themis evidence capture`. Never hand-edit them.
- `docs/OTARI_FEEDBACK.md` is updated whenever something in Otari surprises us (good or bad), with the request ID.
- README must keep the "Judge quick path" and the rubric evidence table current.

---

## 8. Things you must not do

- Do not add an "AI-written text detector". THEMIS judges claims, not authorship.
- Do not let a model choose GitHub labels, close/merge actions, or budget numbers.
- Do not use `pull_request_target` together with `actions/checkout` of the PR head.
- Do not commit `.env`, real API keys, real contributor emails, or unredacted gateway responses.
- Do not silently swallow gateway errors; surface them in the ledger with the request ID.
- Do not hard-code model IDs, prices or the base URL.
- Do not mark a task done if a live verification it depends on has not actually been observed.

---

## 9. Definition of done (per change)

- Lint, types and unit tests pass.
- New behavior has tests (genuine + slop + injection fixtures where relevant).
- If it touches Otari: a live smoke was run once and its redacted receipt is in `evidence/`.
- `DOCS.md` updated if configuration, CLI, verdicts or receipts changed.
- Commit message follows Conventional Commits (`feat(evidence): …`, `fix(otari): …`).
