# Spec: Poppy v0

Status: approved by the Operator on 2026-08-26; review UX extension approved on 2026-09-01.

## Goal

Poppy is a personal GitHub reviewer. Whenever a pull request is opened or updated in one of the Operator's selected repositories, Poppy automatically:

1. reviews the change;
2. publishes one GitHub review with a concise summary and concrete Findings, anchored to changed
   lines when GitHub accepts the location;
3. adds one Change Size label and one result label;
4. completes the `Poppy / PR Review` Check Run;
5. approves a passing, ready pull request or requests changes when blocking Findings exist.

Poppy is always a reviewer. It never creates, edits, closes, or merges pull requests and never pushes code.

## Simplicity Rule

The v0 is one small service with one webhook endpoint and one review flow. Do not add a database,
Redis, dashboard, issue workflow, autonomous coding, per-repository config, or extra
infrastructure. Inline annotations belong to the same formal review and do not add another flow.

Use the simplest implementation that satisfies this spec. New capabilities require a later vertical slice.

## Trigger

The private GitHub App is installed only on repositories selected by the Operator. It listens to these `pull_request` actions:

- `opened`;
- `reopened`;
- `synchronize`;
- `ready_for_review`;
- `edited` when title, body, or base changes.
- `labeled` only when the applied label is exactly `poppy:rerun`.

Each event starts a Review Cycle for the current head SHA and normalized PR input. Duplicate deliveries are ignored in memory. Different pull requests may be reviewed concurrently; default concurrency is three.

Automatic events with the same normalized PR state remain deduplicated. Each distinct
`poppy:rerun` delivery is an explicit request to review that same latest state again; a duplicate
delivery remains deduplicated.

If Poppy is switched off, it does nothing. Missed deliveries can be redelivered from GitHub after it is switched on again. Automatic recovery is deliberately deferred.

## Review Flow

1. Verify the webhook signature and validate the payload.
2. Return `202 Accepted` after scheduling bounded in-memory work.
3. Create a `Poppy / PR Review` Check Run as `in_progress`.
4. Fetch PR metadata and bounded textual patches from GitHub.
5. Ask Codex for a structured review.
6. Validate the result and deterministically decide the gate.
7. Re-read the PR and discard the result if its head SHA or open/draft state changed.
8. Add exactly one `poppy:size/*` label and one result label.
9. Submit one formal GitHub review for the exact commit, including right-side inline comments for
   Findings whose exact file and line are present in the bounded diff:
   - `APPROVE` when a ready PR has no blocking Findings;
   - `REQUEST_CHANGES` when a ready PR has blocking Findings;
   - `COMMENT` for a draft or an incomplete review.
   If GitHub rejects an inline location, retry the same complete review without inline comments.
10. Complete the Check Run as `success`, `failure`, or `action_required`.

The formal review body keeps every Finding, including those also rendered inline, so the fallback
is lossless. Poppy does not create a second sticky issue comment.

## AI Runtime

- Use the official `openai-codex` SDK and its local Codex App Server runtime.
- Authenticate once with the Operator's ChatGPT subscription using the device-code flow. Persist the Codex login in a protected Docker volume.
- Do not require or silently fall back to an OpenAI API key.
- Default model: `gpt-5.6-luna`. `gpt-5.5-luna` is not a valid public model ID.
- Use one isolated Codex thread per Review Cycle and validate the final JSON with strict application types.
- Give Codex no GitHub credentials, plugins, MCP servers, writable repository, or network tools.
- Pass only bounded PR metadata and textual patches. Never check out or execute PR code.

## Findings and Gate

The validated review contains:

- a concise summary;
- risk: `low`, `medium`, `high`, or `critical`;
- at most twenty Findings;
- for each Finding: severity, title, explanation, evidence, suggested response, and optional file/line.

`high` and `critical` Findings block by default. Invalid output, missing required patches, model failure, timeout, or stale state never produces an approval and completes as `action_required` where applicable.

The review renders Findings in `critical`, `high`, `medium`, `low` order with red, orange, yellow,
and blue severity markers. A compact table is followed by expandable evidence and suggested fixes.

## Labels

Poppy changes only labels prefixed with `poppy:`.

Result labels:

- `poppy:passed`;
- `poppy:blocked`;
- `poppy:action-required`.

Poppy also ensures the repository has a `poppy:rerun` label. Applying it triggers a Review Cycle
for the latest PR state; completion removes it from the PR so it can be applied again.

Change Size uses additions plus deletions:

| Label | Changed lines |
| --- | ---: |
| `poppy:size/xxs` | 0–10 |
| `poppy:size/xs` | 11–50 |
| `poppy:size/s` | 51–200 |
| `poppy:size/m` | 201–500 |
| `poppy:size/l` | 501–750 |
| `poppy:size/xl` | 751–999 |
| `poppy:size/xxl` | 1000+ |

## Permissions and Merge Boundary

GitHub App permissions:

- `Checks: read/write`;
- `Pull requests: read/write`;
- `Issues: read/write` for labels;
- `Metadata: read`.

Do not grant `Contents: write`. GitHub requires that permission to merge, providing a hard boundary in addition to Poppy having no merge operation in its GitHub adapter.

## Runtime and Configuration

- Run one containerized service on the Operator's VPS.
- Expose its HTTP port through the Operator's existing HTTPS proxy or Cloudflare Tunnel.
- Start and stop it with `docker compose up -d` and `docker compose stop`.
- Configure only secrets, language/tone, blocking severity, model, context limit, timeout, and concurrency through environment variables.
- The source repository may be public. GitHub keys, webhook secret, ChatGPT credentials, and local environment files must never enter source control or container images.

## Version and Code Quality Policy

- At implementation time use the latest stable compatible runtime, SDK, and libraries.
- Commit exact resolved dependencies and pin the production image so builds remain reproducible.
- Keep code direct and typed: functional core, narrow GitHub/Codex adapters, explicit results, validation at boundaries, descriptive names, KISS, and YAGNI.
- Test behavior rather than implementation details.
- Future updates are proposed through tested dependency PRs; production never downloads floating updates at startup.

## Acceptance Criteria

- Installing the App on two selected repositories requires no workflow file in either repository.
- Opening a PR or pushing a new commit automatically starts a review.
- Applying `poppy:rerun` starts another review of the latest commit and duplicate webhook
  deliveries do not start duplicate work.
- Three independent PRs can be reviewed concurrently.
- A current ready PR receives exactly one formal Poppy review for that Review Cycle, the correct labels, and a completed Check Run.
- Findings are ordered by severity and use inline review comments only for valid right-side diff
  locations; the full review still succeeds without inline comments when GitHub rejects them.
- Ten changed lines map to `XXS`; one thousand map to `XXL`.
- Passing reviews approve; blocking reviews request changes; drafts and technical failures never approve.
- A stale Review Cycle cannot publish a result for an older SHA.
- Poppy cannot merge because it has neither a merge operation nor `Contents: write`.
- Automated tests cover size boundaries, gate mapping, webhook verification, duplicate scheduling, stale results, GitHub writes, and invalid Codex output.
- A staging repository demonstrates the real webhook-to-review flow using the Operator's ChatGPT subscription.

## Out of Scope

- GitLab Merge Requests in v0.
- Issues, specs, code generation, fixes, branches, or PR creation.
- Merge, close, update-branch, or repository-content writes.
- Durable jobs, automatic recovery after downtime, UI, analytics, and multi-user operation.

## References

- [GitHub pull-request review API](https://docs.github.com/en/rest/pulls/reviews)
- [GitHub Check Runs](https://docs.github.com/en/rest/guides/using-the-rest-api-to-interact-with-checks)
- [GitHub merge permission](https://docs.github.com/en/rest/pulls/pulls#merge-a-pull-request)
- [Codex App Server](https://developers.openai.com/codex/app-server)
- [Codex SDK](https://learn.chatgpt.com/docs/codex-sdk)
- [Codex subscription authentication](https://developers.openai.com/codex/auth)
- [OpenAI model catalog](https://developers.openai.com/api/docs/models)
