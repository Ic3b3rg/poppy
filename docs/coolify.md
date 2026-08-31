# Deploy Poppy on Coolify

This is the shortest supported path from Coolify's **+ New** screen to the first automatic pull
request review. It needs a current Coolify installation with HTTPS working on its wildcard domain,
a GitHub account, a ChatGPT subscription, and one disposable repository for the proof run.

Do not create credentials, environment values, files, or storage by hand. Poppy creates its own
private GitHub App and starts ChatGPT's device authorization from the protected setup page.

## 1. Create the resource

1. Open the target Coolify project and environment, then select **+ New**.
2. Select **Public Repository** and, if Coolify asks, select the destination server.
3. Paste `https://github.com/Ic3b3rg/poppy` and select **Check Repository**.
4. Set these values:

   | Field | Value |
   | --- | --- |
   | Branch | `main` |
   | Build Pack | **Docker Compose** |
   | Base Directory | `/` |
   | Docker Compose Location | `/compose.yaml` |

5. Select **Continue** and save the generated application configuration.

Coolify treats the repository's Compose file as the source of truth. The `poppy` service already
declares the image build, health check, internal port, private storage, and runtime hardening.

## 2. Confirm the domain and storage

1. Open **Configuration → General → poppy → Domains**.
2. Keep the generated hostname, require `https://`, and keep `:8000` at the end of the value. For
   example, the Coolify field is `https://poppy-abc.example.com:8000`, while the public browser URL
   is `https://poppy-abc.example.com`. The suffix tells Coolify which internal container port to
   proxy; it does not expose public port 8000.
3. Open **Configuration → Environment Variables**. Do not add a value. Coolify generates
   `SERVICE_URL_POPPY_8000`, and Poppy uses that generated HTTPS origin directly.
4. Open **Configuration → Persistent Storages** and confirm exactly one parsed mount:
   `poppy_data` at `/var/lib/poppy`. Do not create another mount.
5. Select **Deploy**, open **Deployments**, and wait for a successful, healthy deployment.
6. Open `https://<generated-hostname>/healthz`. The first response must be
   `{"status":"ok","mode":"setup"}`.

## 3. Open the protected setup page

1. Open the `poppy` component's **Logs**.
2. Find the newest line beginning `Poppy Setup Session:` and open its complete HTTPS link in your
   browser. Treat that one-time link like a password; do not paste it into an issue or screenshot.
3. After the redirect, confirm the address bar contains only `/setup`, without the `token` query.

## 4. Connect GitHub

1. On the setup page select **Connect GitHub**.
2. Leave the organization field empty for repositories owned by your personal GitHub account, then
   select **Register Poppy on GitHub**.
3. On GitHub, approve creation of the private App.
4. On the installation screen select **Only select repositories**, choose the disposable repository
   for this proof, and complete the installation.
5. Let GitHub return to Poppy and confirm that the setup page shows GitHub as **Connected**.

The repository selection stays under GitHub's control. You can add your other repositories later
from the App's GitHub installation settings without reconfiguring Poppy.

## 5. Connect ChatGPT

1. On the same setup page select **Connect ChatGPT**.
2. Open the displayed verification URL, sign in to the ChatGPT account whose subscription Poppy
   should use, and enter the temporary device code.
3. Return to Poppy and wait for **Success. Connected**.
4. Once both integrations are connected, setup closes automatically. Reloading `/setup` must now
   return `404`, and `/healthz` must return `{"status":"ok","mode":"reviewer"}`.

## 6. Prove the first review

1. In the selected disposable repository, create a branch that changes 11–50 lines and open a
   non-draft pull request. Do not add a workflow file to that repository.
2. Wait for the review to complete, then verify all three outputs on the same head commit:

   - labels include `poppy:size/xs` and exactly one result label, either `poppy:passed` or
     `poppy:blocked`;
   - the pull request conversation contains one Poppy **Formal Review** for that event;
   - **Checks** shows `Poppy / PR Review` completed, with success or failure matching the result
     label.

Poppy may approve, request changes, or leave a safe comment. It never merges the pull request.
The other Change Size ranges are documented in the project [README](../README.md#review-policy).

Record only the Poppy commit SHA, disposable pull-request URL, observed labels, review event, check
conclusion, and test timestamps. Never record the setup link, device code, App key, webhook secret,
Codex authentication files, or private repository content.

## 7. Stop, start, redeploy, and update

1. In the Coolify resource header select **Stop** and confirm it.
2. Select **Start**. When the component is healthy, verify `/healthz` is still in `reviewer` mode,
   `/setup` still returns `404`, and **Logs** contain no new `Poppy Setup Session` line.
3. Select **Redeploy** and wait for success. Repeat the same three checks. The `poppy_data` volume
   preserves setup state and ChatGPT authentication while the container is replaced.
4. To update Poppy after a new commit reaches `main`, select **Redeploy**, verify the source commit
   in **Deployments**, then repeat the health and first-review checks.

To turn Poppy off for a while, leave the resource stopped. Starting it later resumes reviewer mode;
onboarding is not repeated.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Build cannot find Compose | **Base Directory** is `/` and **Docker Compose Location** is `/compose.yaml`. |
| Domain is unreachable | **Domains** uses HTTPS and ends in `:8000`; the component is healthy. |
| No setup link on a fresh volume | Open the component **Logs**, not only the deployment build log. |
| A setup link returns `404` | It was consumed or expired. Continue with the existing browser session; before setup is confirmed, a restart logs one new recovery link. |
| `/setup` returns `404` after both connections | This is the expected permanent ready state. |
| Health says `unavailable` after readiness | Inspect sanitized logs and verify the GitHub App installation and ChatGPT account still exist. Do not delete `poppy_data`. |
| A pull request receives nothing | Confirm the App is installed on that repository, the PR is not a draft, and it was opened or updated after installation. |

Deleting `poppy_data` intentionally erases the App credentials and ChatGPT authorization and is not
a troubleshooting step.

## Official references

- [Coolify: deploy a public repository](https://coolify.io/docs/applications/ci-cd/github/public-repository)
- [Coolify: Docker Compose build pack](https://coolify.io/docs/applications/build-packs/docker-compose)
- [Coolify: Compose domains and generated service URLs](https://coolify.io/docs/knowledge-base/docker/compose)
- [Coolify: persistent storage](https://coolify.io/docs/knowledge-base/persistent-storage)
- [Coolify: deployment lifecycle](https://next.coolify.io/docs/applications/deployments/overview)
- [Docker: named volumes in Compose](https://docs.docker.com/reference/compose-file/services/#volumes)
