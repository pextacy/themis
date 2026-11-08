# Evidence

Redacted receipts written by `themis evidence capture`. Nothing here is edited by hand, and a test double cannot write here.

| Rubric item | Receipt | Status on Nov 8 |
|---|---|---|
| Open-weight model | `chat-a-live.json` | Live pass |
| Second open-weight role | `chat-b-live.json` | Live pass on Liquid `lfm2.5-2.6b-free` (free Gemma still answered 429) |
| Routing and fallback | `routing-live.json` | Live pass: main provider key rejected, backup answered on attempt 2 |
| MCP | `mcp-live.json` | Live pass |
| Web search and fetch | `web-live.json` | Live pass |
| Code execution | `codeexec-live.json` | Refused: needs an account balance |
| Budgets | `budget-refusal-live.json` | Live pass: 403 `budget_exceeded` on the demo key |
| Inference guardrails | none | Not passed: no hosted profile |
| Per-user budgets | `peruser-budget-live.json` | Live pass: refused with scope `user` |

A non-passing receipt is kept on purpose: it records what the gateway really answered. See `docs/READINESS.md`.
