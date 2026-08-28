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

### Connect the private GitHub App

From the protected `/setup` page, **Connect GitHub** starts GitHub's App Manifest flow. Poppy
submits a private App with an instance-and-registration-attempt-specific proposed Poppy name. Its
homepage, webhook, manifest callback, and post-install setup URLs all derive from the configured
public HTTPS origin. The manifest requests only `Checks: write`, `Pull requests: write`,
`Issues: write`, and `Metadata: read`, and subscribes only to the `pull_request` event.

GitHub allows a private App to be installed only on the account that owns it. Leave the optional
organization field empty to register Poppy for the Operator's personal repositories, or enter the
organization login to register it for that organization's repositories. After registration, Poppy
exchanges the callback code for the new App's ID, slug, private key, and webhook secret, then sends
the Operator through GitHub's native installation screen. Choose either **All repositories** or
**Only select repositories** there. Poppy does not copy that selection into a repository database
or local allowlist: the GitHub installation remains the source of truth as access is later added or
removed.

GitHub may already have created the App when a manifest-code exchange is interrupted or returns an
invalid response. Poppy preserves the proposed App name and owner account, shows a cleanup warning,
and gives each retry a fresh proposed name. Check the named account's GitHub Developer settings and
delete any unused App before or after retrying. Only the App whose credentials are accepted and
whose installation is verified becomes the one GitHub App connected to this Poppy Instance.

The post-install callback is correlated to the pending Setup Session and is not trusted by itself.
Poppy authenticates as the newly created App and verifies that the reported installation belongs
to that App before marking GitHub connected. A rejected or unavailable verification leaves the
installation step retryable. Accepted credentials and installation identity are written atomically
to `github-setup.json` with restrictive permissions, so the connected state survives restart.
Connecting GitHub alone does not finish onboarding: ChatGPT connection and final readiness are
separate setup conditions.

The protocol behavior follows GitHub's current documentation:

- [Registering a GitHub App from a manifest](https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest)
- [GitHub App naming requirements](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app#registering-a-github-app)
- [Private GitHub App installation ownership](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/making-a-github-app-public-or-private#private-installation-flow)
- [Create a GitHub App from a manifest REST endpoint](https://docs.github.com/en/rest/apps/apps?apiVersion=2026-03-10#create-a-github-app-from-a-manifest)
- [Installing a GitHub App from a third party](https://docs.github.com/en/apps/using-github-apps/installing-a-github-app-from-a-third-party)
- [Preserving correlation state in an App installation link](https://docs.github.com/en/apps/sharing-github-apps/sharing-your-github-app#sharing-your-github-app-via-an-install-link)
- [Reviewing and modifying an installed GitHub App](https://docs.github.com/en/apps/using-github-apps/reviewing-and-modifying-installed-github-apps)
- [Security guidance for the post-install setup URL](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/about-the-setup-url)
- [Get an installation for the authenticated App REST endpoint](https://docs.github.com/en/rest/apps/apps?apiVersion=2026-03-10#get-an-installation-for-the-authenticated-app)
- [Generating a JSON Web Token for a GitHub App](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-json-web-token-jwt-for-a-github-app)
- [Deleting an unused GitHub App](https://docs.github.com/en/apps/maintaining-github-apps/deleting-a-github-app)

### Connect the ChatGPT subscription

From the same protected `/setup` page, **Connect ChatGPT** starts Codex App Server's managed
ChatGPT device-code flow. Open the displayed OpenAI verification URL, sign in with the Operator's
ChatGPT account, and enter the temporary user code. The page refreshes while authorization is
pending and then reports success or a sanitized failure. A pending attempt can be cancelled;
failed and cancelled attempts can be retried without starting more than one active attempt.

Poppy accepts only a managed ChatGPT account. It forces Codex's ChatGPT login mode, clears the
`OPENAI_API_KEY` environment variable for the App Server, and rejects and logs out any non-ChatGPT
account returned at the boundary. For the ChatGPT flow, setup responses expose only status plus
the temporary verification URL and user code, while normal logs expose only sanitized status—never
the login ID, account details, access token, or refresh token.

Codex owns token persistence and refresh. Poppy forces file-backed credential storage so Codex
writes `auth.json` beneath `POPPY_CODEX_HOME`, which is the existing private `poppy_codex` volume.
Poppy writes only non-credential setup status to `chatgpt-setup.json`. A completed connection
survives container replacement; an interrupted pending attempt resumes as failed and retryable,
and the mere presence of a partial `auth.json` is never treated as connected.

GitHub and ChatGPT remain independent setup steps: either can be connected first. Completing this
step activates reviewer mode only when the GitHub installation is also connected and valid.

The authentication behavior follows OpenAI's current documentation:

- [Codex App Server authentication endpoints](https://learn.chatgpt.com/docs/app-server#auth-endpoints)
- [Login on headless devices](https://learn.chatgpt.com/docs/auth#login-on-headless-devices)
- [Codex credential storage](https://learn.chatgpt.com/docs/auth#credential-storage)

### Enter reviewer mode

When both connections are valid, Poppy constructs the existing GitHub adapter, Codex adapter,
Review Cycle, and in-memory scheduler inside the running process. It then atomically persists a
non-secret readiness marker, removes the server-side Setup Session state, and begins accepting
signed pull-request webhooks. No restart or additional confirmation is required.

Readiness permanently removes the setup surface. Every `/setup` page, mutation, and provider
callback returns `404 Not Found`, including requests carrying an old bootstrap link or Setup
Session cookie. `GET /healthz` reports `mode: reviewer` only while an active reviewer is available;
an instance sealed as ready but unable to construct that runtime reports `mode: unavailable` and
continues rejecting webhooks.

On restart, the readiness marker is consulted before any Setup Session is created. A ready
instance emits no setup link, reloads the connected GitHub credentials and Codex home, revalidates
the GitHub installation and ChatGPT account, and starts directly in reviewer mode. Missing,
corrupt, revoked, or invalid ready-state credentials never reopen setup. GitHub webhook bodies are
authenticated with the persisted secret and `X-Hub-Signature-256` before the unchanged Review
Cycle is scheduled. Before readiness is sealed, a transient runtime-validation failure is retried
by the live setup page without requiring a restart.

The lifecycle and signature handling follow the current official guidance:

- [FastAPI lifespan events](https://fastapi.tiangolo.com/advanced/events/)
- [Starlette lifespan](https://www.starlette.io/lifespan/)
- [Starlette pure ASGI middleware](https://www.starlette.io/middleware/#pure-asgi-middleware)
- [Validating GitHub webhook deliveries](https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries)

## Start, stop, and update

Turn Poppy off and on whenever you want:

```bash
docker compose stop
docker compose start
```

Before readiness, the protected Setup Session and any partial setup state remain in `poppy_codex`
across these operations. A link is logged for a fresh volume, and a distinct recovery link is
logged after a restart only when the cookie handoff was not yet confirmed. After readiness, Poppy
starts directly in reviewer mode and never creates or logs another Setup Session.

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
