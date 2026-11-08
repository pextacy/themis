# Features added after the MVP

Twenty-five features, grouped by what they do. Every one runs without a model call unless noted, and all of them were tested offline with mocks. "Not live" marks what has never met the real service.

Three rules hold across all of them:

- Signals are facts beside the verdict. `scoring.decide` never reads them.
- New evidence checks can reach `HARD` only for a dependency that does not exist. Everything else is `SOFT` at most.
- Nothing here reads who wrote a submission, except the first-time boost, which can only raise priority.

## Verifying claims

| # | Feature | Module | What it does | Not live |
|---|---|---|---|---|
| 1 | Stack traces | `evidence/tracebacks.py` | Checks Python, Node, Go, Java and Rust frames against the repo: file, line, function | |
| 2 | References | `evidence/references.py` | Checks permalinks, `path:line` mentions, commit hashes and quoted flags | GitHub commit lookups |
| 3 | crates.io and Go | `evidence/ecosystems.py`, `registries.py` | Verifies new Cargo and go.mod dependencies. `themis deps crates serde` works too | Both registries |
| 4 | Advisories | `evidence/advisories.py` | For exact pins: does the version exist, does OSV list it, does the license clash | OSV |
| 5 | Test impact | `evidence/test_impact.py` | Maps changed source to changed tests and checks a "tests added" claim | |

## Signals about the change

| # | Feature | Module | What it does |
|---|---|---|---|
| 6 | Leaked secrets | `signals/secrets.py` | Finds credentials in added lines. Reports a fingerprint, never the value. Adds `themis:secret-leak` |
| 7 | Supply chain | `signals/supply_chain.py` | Flags workflow escalation, install hooks, pipe-to-shell, decoded `eval`, lockfile URLs leaving the registry. Adds `themis:risky-change` |
| 8 | Change quality | `signals/change_quality.py` | Neutral facts: whitespace-only, no-op, mass rename, file sprawl, removed tests |
| 9 | Report completeness | `signals/completeness.py` | Lists what a bug or security report lacks. Feeds "What would help". Works beyond English |

## Shield and intake

| # | Feature | Module | What it does | Not live |
|---|---|---|---|---|
| 10 | Homoglyphs | `shield/confusables.py` | Folds look-alike letters, then reruns the injection patterns. Honest non-Latin text gives no finding | |
| 11 | Encoded payloads | `shield/encoded.py` | Decodes base64, hex, URL, entity and ROT13 text two levels deep. Catches concealing terminal escapes and data-carrying images | |
| 12 | Clerk fallback | `agents/fallback_clerk.py` | When the Clerk fails, extracts plainly worded claims by regex, marked degraded | |
| 13 | Self-check | `cli_commands/precheck.py` | `themis precheck`: a free check for contributors. No key, no network, never runs project code | |
| 14 | Forgejo and Codeberg | `intake/forgejo.py`, `forge.py` | `themis check <codeberg url>`. Read-only: results print and nothing is posted | Authenticated reads |
| 15 | Private inbox | `intake/inbox.py` | `themis triage-file` for disclosure mail. Drops addresses and headers, posts nothing | Full run |

## Operations

| # | Feature | Module | What it does | Not live |
|---|---|---|---|---|
| 16 | Backfill | `intake/backfill.py` | `themis scan`: resumable, USD cap from real costs, no posting unless asked. Items running at the cap still finish, so spend can pass it slightly | GitHub |
| 17 | Analysis cache | `store/cache.py` | Unchanged input is not billed twice. `/themis recheck` always reruns. Never caches `DEFERRED`, `TAINTED_UNCHECKED` or degraded results | |
| 18 | Local budgets | `store/usage.py` | `themis budget`, plus optional local deferral and a burst limit. Only ever `DEFERRED` | |
| 19 | Webhook server | `web/webhook.py` | `POST /webhook/github` for self-hosting. A pull request here has no test artifact, so its test checks stay `UNVERIFIED` | GitHub delivery |
| 20 | Weekly digest | `report/digest.py` | `themis digest`. Does not claim time saved, because that is not measured | Issue posting |

## Trust and insight

| # | Feature | Module | What it does | Not live |
|---|---|---|---|---|
| 21 | Receipt chain and replay | `verdict/transparency.py`, `replay.py` | Hash chain over receipts. `themis replay` recomputes a verdict and names any input it had to assume | |
| 22 | Exports | `report/exports.py` | SARIF, CSV with formula neutralizing, JSONL, and evidence bundles. Results with no file stay out of SARIF unless asked | Code-scanning upload |
| 23 | Priority and insights | `verdict/priority.py`, `web/insights.py` | Makes `first_time_contributor_fast_track` real. JSON API, `/metrics`, badge, `/analytics`, optional token | |
| 24 | History and appeals | `store/history.py`, `report/appeals.py` | Timeline across runs. `/themis appeal <reason>` adds a label and a note and never changes the verdict. Overrides now reach the store | GitHub |
| 25 | Benchmark tuning | `bench_tune.py` | Tunes on a train split with zero false `CONTRADICTED`, reports on the holdout. Never changes defaults | |

## Known gaps

- The `dependencies` table is never written. Dependency results live in `checks`.
- crates and Go have no popular-package list, so the lookalike signal stays off for them.
- The web queue uses default policy for fast-track, not each repository's.
- The benchmark does not run the new checks by default. A one-off offline run with `tracebacks`, `references` and `test_impact` changed no verdict, but the dataset holds few cases that use them.
