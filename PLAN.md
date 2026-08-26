# Poppy v0 Implementation Plan

Status: approved by the Operator; Slices 1–3 complete, Slice 4 implemented with the
authenticated smoke test pending.

## Constraint

Build only the automatic GitHub PR reviewer described in `SPEC.md`. One Python service, in-memory concurrency, no database, no dashboard, no issue automation, and no merge capability.

Every behavior change is implemented test-first. Each numbered slice should leave the repository green and can become one focused commit.

## Slice 1 — Executable skeleton and review domain

Create the smallest installable service and its pure rules.

- Add `pyproject.toml`, exact lockfile, package layout, pytest, mypy, and Ruff configuration using current stable compatible releases.
- Define immutable types for PR input, Findings, review result, gate decision, and formal review verdict.
- Implement Change Size and severity/gate mapping.
- Implement the single formal-review Markdown renderer.
- Test every size boundary, including 10 -> `XXS` and 1000 -> `XXL`, plus pass/block/draft/error mappings.

Verification: unit tests, types, lint, and format pass.

## Slice 2 — Webhook-to-review tracer with fakes

Prove the whole application flow before integrating external systems.

- Add the webhook endpoint with exact raw-body HMAC verification and payload validation.
- Normalize only the approved PR actions.
- Add a bounded in-memory scheduler with concurrency three and duplicate suppression.
- Implement the Review Cycle against narrow `GitHubPort` and `CodexPort` interfaces.
- Use fakes to prove: webhook -> in-progress check -> review -> labels -> formal verdict -> completed check.
- Test stale SHA cancellation, invalid signatures, unsupported events, duplicate delivery, and independent parallel PRs.

Verification: HTTP integration tests run without network access.

## Slice 3 — Real GitHub App adapter

Replace the fake GitHub boundary without changing domain or orchestration code.

- Create installation tokens from the App ID and private key.
- Fetch PR state, statistics, and bounded patches.
- Create/update Check Runs, replace only `poppy:` labels, and submit the formal review for an explicit commit SHA.
- Expose no merge, close, branch-update, or contents-write operation.
- Test requests and responses with mocked HTTP fixtures, including pagination and API failures.

Verification: GitHub adapter contract tests pass and a security test proves the public port contains no merge operation.

## Slice 4 — Codex App Server adapter

Connect the review flow to the Operator's ChatGPT subscription.

- Integrate the latest stable `openai-codex` SDK and its bundled local App Server.
- Add the one-time device-code login command and persistent Codex data path.
- Start one isolated thread per Review Cycle with default `gpt-5.6-luna`.
- Send bounded PR data, require JSON, validate it strictly, and map timeout/rate-limit/invalid output to `action_required`.
- Configure no API-key fallback, GitHub credentials, network tools, MCP servers, plugins, or writable repository.
- Test the adapter with sanitized App Server event fixtures; keep normal tests offline.

Verification: adapter contract tests and one manual authenticated smoke review pass.

## Slice 5 — Container and real staging proof

Make the bot easy to run and stop.

- Add a minimal multi-stage `Dockerfile`, `compose.yaml`, health endpoint, ignored local secrets, and persistent Codex auth volume.
- Add concise setup instructions for GitHub App permissions/events, ChatGPT device login, HTTPS webhook URL, start, stop, logs, and update.
- Run the complete local verification suite in the production image.
- Install a staging GitHub App on a disposable repository and demonstrate open/update -> labels + formal review + Check Run.
- Record only sanitized evidence and never credentials or repository content.

Verification: container health check passes and the staging acceptance flow satisfies `SPEC.md`.

## Completion Gate

Poppy v0 is complete only when all automated checks pass, the container starts from documented commands, and the real staging PR receives the expected automatic review. Anything beyond this plan becomes a later issue rather than expanding v0.
