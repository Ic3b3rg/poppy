# Poppy

Poppy is a personal GitHub App that automatically reviews every pull request in the
repositories where you install it. It adds Change Size and gate labels, publishes one formal
review, and completes a `Poppy / PR Review` Check Run.

Poppy can approve or request changes. It cannot merge, push, close, or edit a pull request.
It runs on your server and uses your ChatGPT subscription through the local Codex App Server;
there is no OpenAI API-key fallback.

## What v0 does

- reacts to PR open, reopen, new commits, ready-for-review, and relevant edits;
- reviews up to three independent PRs in parallel by default;
- uses `gpt-5.6-luna` by default;
- submits `APPROVE`, `REQUEST_CHANGES`, or a safe `COMMENT` for an incomplete review;
- changes only labels prefixed with `poppy:`;
- never checks out or executes PR code.

Issue automation and autonomous code changes are deliberately outside v0. The approved scope
and its acceptance criteria are in [SPEC.md](SPEC.md).

## Requirements

- a Linux host with current Docker Engine and Docker Compose;
- a stable HTTPS URL routed to Poppy's internal port `8000`.

No workflow file is needed in the repositories being reviewed.

Poppy keeps no repository database. It sends bounded PR metadata and textual patches to OpenAI
through Codex so the model can perform the review. Poppy itself makes application calls only to
GitHub and OpenAI.

## Start a protected Setup Session

Poppy can boot before GitHub or ChatGPT is connected. On an empty private data volume it starts in
setup mode, generates one secret bootstrap link, and writes that complete link to the deployment
log. Opening the link exchanges its one-time query token for a Secure, HttpOnly, SameSite Setup
Session cookie and redirects to `/setup`, removing the token from the visible address. Until that
cookie first reaches the protected page, an interrupted exchange can be recovered after restart
from a newly logged one-time link; previously logged links remain invalid.

For a portable Docker Compose deployment, configure the public HTTPS origin:

```bash
git clone https://github.com/Ic3b3rg/poppy.git
cd poppy
cp .env.example .env
chmod 600 .env
# Edit POPPY_PUBLIC_BASE_URL in .env, then:
docker compose up -d --build
docker compose logs poppy
```

On Coolify, deploy this repository as a Docker Compose resource and assign the `poppy` service an
HTTPS domain routed to port `8000`. The Compose definition declares Coolify's
`SERVICE_URL_POPPY_8000` value and uses it as Poppy's public origin; `POPPY_PUBLIC_BASE_URL`
remains the explicit override for other hosts.

The existing `poppy_codex` volume is the sole durable store. Setup state is atomically persisted
there with restrictive permissions and survives container replacement alongside any existing
Codex login. The container root filesystem stays read-only; only that volume and the bounded
`/tmp` tmpfs are writable. The optional Compose secret uses an empty source on fresh deployments,
so a PEM is not required to enter setup mode.

A healthy fresh instance answers `GET /healthz` with `{"status":"ok","mode":"setup"}`. Review
webhooks return `503 Service Unavailable` until onboarding is complete. The GitHub and ChatGPT
connection actions are delivered by the following onboarding slices.

## Start, stop, and update

Turn Poppy off and on whenever you want:

```bash
docker compose stop
docker compose start
```

The protected Setup Session and any partial setup state remain in `poppy_codex` across these
operations. A link is logged for a fresh volume, and a distinct recovery link is logged after a
restart only when the cookie handoff was not yet confirmed.

An existing pre-onboarding reviewer keeps its `.env`, `secrets/github-app.pem`, and
`poppy_codex` volume. Before its first `docker compose up` on this version, add this Compose-only
source path to the existing `.env` so the old PEM is mounted at the same in-container secret path:

```dotenv
POPPY_GITHUB_PRIVATE_KEY_SOURCE=./secrets/github-app.pem
```

To update Poppy:

```bash
git pull --ff-only
docker compose build --pull
docker compose up -d
```

## Review policy

`high` and `critical` Findings block by default. Change Size uses additions plus deletions:

| Size | Changed lines |
| --- | ---: |
| XXS | 0–10 |
| XS | 11–50 |
| S | 51–200 |
| M | 201–500 |
| L | 501–750 |
| XL | 751–999 |
| XXL | 1000+ |

The environment variables in [.env.example](.env.example) control language, tone, blocking
severity, model, input limit, timeout, concurrency, host port, and log level.

## Local verification

With Python 3.13 or 3.14 and `uv` installed:

```bash
uv sync --frozen
uv run pytest
uv run mypy src
uv run ruff check .
uv run ruff format --check .
```

With Docker available, validate the resolved Compose model and opt into the networked production
image build and real setup-mode health check with:

```bash
uv run pytest -m container tests/integration/test_container_runtime.py
```

Normal tests are offline and never call GitHub or consume ChatGPT usage.
