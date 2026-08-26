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

- a GitHub account that can create and install a GitHub App;
- a ChatGPT account with Codex access;
- a Linux host with current Docker Engine and Docker Compose;
- an HTTPS URL routed to `127.0.0.1:8000` by your reverse proxy or Cloudflare Tunnel.

No workflow file is needed in the repositories being reviewed.

Poppy keeps no repository database. It sends bounded PR metadata and textual patches to OpenAI
through Codex so the model can perform the review. Poppy itself makes application calls only to
GitHub and OpenAI.

## 1. Create the GitHub App

In GitHub, open **Settings → Developer settings → GitHub Apps → New GitHub App** and set:

- Webhook URL: `https://your-domain.example/webhooks/github`;
- Webhook secret: a new random value that you keep for the `.env` file;
- Repository permissions:
  - Checks: **Read and write**;
  - Pull requests: **Read and write**;
  - Issues: **Read and write** (GitHub labels use the Issues API);
  - Metadata: **Read-only**;
- Subscribe to events: **Pull request**.

Do not grant `Contents: write`. Generate a private key, install the App only on your selected
repositories, choose **Only on this account** when GitHub asks where it can be installed, and note
the numeric App ID.

## 2. Configure Poppy

```bash
git clone https://github.com/Ic3b3rg/poppy.git
cd poppy
cp .env.example .env
mkdir -p secrets
mv ~/Downloads/your-github-app-key.pem secrets/github-app.pem
chmod 600 .env secrets/github-app.pem
```

Edit `.env` with the App ID and the same webhook secret configured in GitHub. The key, `.env`,
Codex login, and all PEM files are ignored by Git and excluded from the image build.

## 3. Authenticate the ChatGPT subscription once

```bash
docker compose build
docker compose run --rm poppy login
```

Open the displayed URL, enter the device code, and finish the ChatGPT login. Authentication is
stored in the private `poppy_codex` Docker volume and survives container replacement. Poppy
refuses to start if that volume contains no ChatGPT login or contains only API-key auth.

## 4. Start and stop

```bash
docker compose up -d
docker compose ps
docker compose logs -f poppy
```

Point the HTTPS proxy or Cloudflare Tunnel at `http://127.0.0.1:8000`. A healthy instance answers
`GET /healthz` with `{"status":"ok"}`.

Turn Poppy off and on whenever you want:

```bash
docker compose stop
docker compose start
```

GitHub does not deliver work while the bot is stopped. You can redeliver a missed webhook from
the GitHub App's **Advanced** page after restarting it.

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

Normal tests are offline and never call GitHub or consume ChatGPT usage.
