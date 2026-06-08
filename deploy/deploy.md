# Deploying the Dexter beta to Azure Container Apps

This directory is fully decoupled from the app. Delete `web/` and `deploy/` and
set `DEXTER_SERVE_WEB=false` to return Dexter to its local-only shape.

The web UI is still just a static client calling `POST /chat`. The FastAPI brain
serves it from the same origin, so there is no CORS setup to manage.

## What gets deployed
- One container built from `deploy/Dockerfile` and run by Azure Container Apps.
- Serves the terminal UI at `/`, the API at `/chat`, and health at `/health`.
- Public HTTPS ingress on port `8000`.
- `minReplicas: 1` so the beta stays warm.
- Shared passcode gate via `DEXTER_PASSCODE`.
- Phoenix tracing only when tracing credentials are present.

## Deployment model

The deploy flow is Python-based and does not require the Azure CLI.

- `deploy/deploy.py` uses the Azure management SDK plus `DefaultAzureCredential`.
- `deploy/deploy.ps1` and `deploy/deploy.sh` are thin wrappers around that script.
- The image is built locally with Docker from the repo root, then pushed to ACR.
- For this repo, prefer `uv run python deploy/deploy.py` so the deploy uses the
  repo virtualenv and its installed Azure SDK packages.

The script is create-or-update:
- If the resource group, ACR, Container Apps environment, or app already exist, it reuses them.
- If they do not exist, it creates them.
- Re-running the same command builds a new image, updates the app, and rolls a new revision.

## One-time setup

### 1. Install prerequisites

- Python 3
- Docker
- Azure SDK dependencies:

```bash
python -m pip install -r deploy/requirements.txt
```

### 2. Make sure Azure authentication works

`deploy/deploy.py` uses `DefaultAzureCredential`, so you need one supported auth path
available locally. The most predictable non-`az` option is a service principal in `.env`
or your shell environment:

- `AZURE_TENANT_ID`
- `AZURE_CLIENT_ID`
- `AZURE_CLIENT_SECRET`
- `AZURE_SUBSCRIPTION_ID`

Other `DefaultAzureCredential` sources can also work, but the service principal env vars
are the easiest path to keep this deploy flow repeatable.

### 3. Fill in `.env`

The deploy script reads names and secrets from the repo-local `.env`. Once those values
are in place, the deploy command can usually stay argument-free.

Important app settings:
- `AZURE_OPENAI_ENDPOINT`
- `AZURE_OPENAI_API_KEY`
- `AZURE_OPENAI_API_VERSION`
- `AZURE_OPENAI_DEPLOYMENT_ROUTER`
- `DEXTER_PASSCODE`
- `MBTA_API_KEY` optional
- `DEXTER_TRACING_ENDPOINT` optional
- `DEXTER_TRACING_API_KEY` optional

Important deploy settings:
- `AZURE_SUBSCRIPTION_ID`
- `AZURE_RESOURCE_GROUP`
- `AZURE_LOCATION`
- `AZURE_CONTAINERAPP_NAME`
- `AZURE_CONTAINERAPPS_ENV_NAME`
- `AZURE_CONTAINERAPPS_ENV_RESOURCE_GROUP` optional when the CAE lives outside the app RG
- `AZURE_CONTAINER_REGISTRY_NAME` optional
- `AZURE_CONTAINER_REGISTRY_RESOURCE_GROUP` optional when ACR lives outside the app RG
- `AZURE_CONTAINER_REPOSITORY`
- `AZURE_CONTAINER_IMAGE_TAG` optional
- `AZURE_CONTAINER_MAX_REPLICAS`

For your current shared-infra setup, the local `.env` is already seeded with:
- `AZURE_RESOURCE_GROUP=rg-dealsignal-prod`
- `AZURE_LOCATION=eastus2`
- `AZURE_CONTAINERAPPS_ENV_RESOURCE_GROUP=rg-dealsignal-prod`
- `AZURE_CONTAINERAPPS_ENV_NAME=dealsignal-env`
- `AZURE_CONTAINER_REGISTRY_RESOURCE_GROUP=rg-dealsignal-prod`
- `AZURE_CONTAINER_REGISTRY_NAME=dealsignalacr12345`

That means the script will try to deploy the Dexter app into the existing
`dealsignal-env` environment first, and only create it if it does not exist.
Dexter itself is still deployed as a normal CPU-only container app; reusing a
shared CAE does not add GPU requests unless the app spec is explicitly changed
to do so.

## Current shared deployment

As of the latest successful deploy, the shared target is:
- Resource group: `rg-dealsignal-prod`
- Container Apps environment: `dealsignal-env`
- Container registry: `dealsignalacr12345`
- Container app: `dexter-beta`
- Image repository: `dexter`

Notes from the live environment:
- The existing CAE reports location `East US`, even though the local config is
  seeded as `eastus2`.
- `deploy.py` now reuses the environment's actual location when they differ.
- The latest successful live URL was:

```text
https://dexter-beta.ambitiousdesert-fb83f82c.eastus.azurecontainerapps.io/
```

Treat the URL above as a recent known-good endpoint, not a permanent contract.

## Quick deploy

After the one-time setup, deploy with one command.

### Preferred

```powershell
uv run python deploy/deploy.py
```

### PowerShell

```powershell
.\deploy\deploy.ps1
```

### Bash

```bash
./deploy/deploy.sh
```

On success the script prints the live shareable URL:

```text
https://<app-name>.<hash>.<region>.azurecontainerapps.io/
```

## Push-a-change loop

After you change the app, rerun the same command.

The deploy script will:
1. Reuse the configured resource names from `.env`.
2. Build a fresh image from the repo root.
3. Push it to ACR.
4. Update the Container App.
5. Roll a new revision.
6. Print the live URL again.

If you want a custom image tag for a given push:

### PowerShell

```powershell
.\deploy\deploy.ps1 --image-tag beta-20260607
```

### Bash

```bash
./deploy/deploy.sh --image-tag beta-20260607
```

If you leave `AZURE_CONTAINER_IMAGE_TAG` blank, the script uses a UTC timestamp tag.

## Shared-infra access

For the shared `rg-dealsignal-prod` setup, the deploy principal needs Azure RBAC
that goes beyond plain read access.

Recommended role assignments on `rg-dealsignal-prod`:
- `Contributor`
- `User Access Administrator`

Why both are needed:
- `Contributor` covers reading and updating the shared ACR, CAE, and Container App.
- `User Access Administrator` lets the deploy script create the `AcrPull` role
  assignment for the Container App's managed identity on the registry scope.

The subscription-level resource providers `Microsoft.App` and
`Microsoft.ContainerRegistry` should already be registered. The deploy script now
checks provider state first and only tries to register when they are missing.

## Shared existing resources

This flow is designed to work with shared Azure resources instead of forcing new ones
every time.

- If `AZURE_CONTAINERAPPS_ENV_NAME` already exists in
  `AZURE_CONTAINERAPPS_ENV_RESOURCE_GROUP`, the script reuses that CAE.
- If `AZURE_CONTAINER_REGISTRY_NAME` already exists in
  `AZURE_CONTAINER_REGISTRY_RESOURCE_GROUP`, the script reuses that ACR.
- If either one does not exist, the script creates it.

Defaults:
- `AZURE_CONTAINERAPPS_ENV_RESOURCE_GROUP` defaults to `AZURE_RESOURCE_GROUP`
- `AZURE_CONTAINER_REGISTRY_RESOURCE_GROUP` defaults to `AZURE_RESOURCE_GROUP`

Notes:
- The script enables the ACR admin user if needed so Docker can push without `az`.
- The Container App gets a system-assigned managed identity and `AcrPull` on the registry.
- `DEXTER_SERVE_WEB` is always forced to `true` in the deployed app.
- `DEXTER_DEPLOYED_AT` is stamped during each deploy in Eastern time and shown in the web UI header.
- `DEXTER_TRACING` is turned on only when tracing credentials are present.
- The script now checks whether resource groups already exist before attempting
  to create them, which keeps shared-RG deploys from failing on unnecessary writes.
- The script now uses the Azure SDK's `RegistryUpdateParameters(properties=...)`
  payload shape when enabling the ACR admin user.

## Redeploy after code changes

After changing app code, web assets, or deploy-time secrets, redeploy with:

```powershell
uv run python deploy/deploy.py
```

That command will:
1. Build a fresh Docker image from the repo root.
2. Push the image to `dealsignalacr12345`.
3. Update `dexter-beta`.
4. Roll a new ACA revision.
5. Print the live URL.

If you want a manual image tag for a release:

```powershell
uv run python deploy/deploy.py --image-tag beta-20260608-1
```

## Troubleshooting

### Docker is installed but build fails immediately

If you see errors about `dockerDesktopLinuxEngine` or the Docker API pipe:
- start Docker Desktop
- wait for the Linux engine to come up
- confirm `docker info` succeeds
- rerun the deploy

### Deploy works in the repo venv but not through `deploy.ps1`

If `deploy.ps1` picks `py -3` or another interpreter without the Azure SDK
packages, the wrapper can fail before the deploy even starts. In that case,
prefer:

```powershell
uv run python deploy/deploy.py
```

### Azure says the provider or shared RG already exists

The deploy should now reuse existing providers, resource groups, ACR, and CAE.
If you hit another auth error, check whether the failing step is trying to do an
unnecessary write versus a real deploy update.

### ACA is live but the app still looks broken

Check the deployed app before editing the deploy flow again:
- open `/`
- open `/health`
- inspect Container App logs in Azure
- verify the `DEXTER_PASSCODE` gate and Azure OpenAI settings in the deployed secrets
- confirm the title bar shows a recent `last redeployed` timestamp in Eastern time

## Teardown

Delete only the container app:

### PowerShell

```powershell
.\deploy\deploy.ps1 --teardown
```

### Bash

```bash
./deploy/deploy.sh --teardown
```

Delete the app plus the configured environment and registry:

### PowerShell

```powershell
.\deploy\deploy.ps1 --teardown --delete-environment --delete-registry
```

### Bash

```bash
./deploy/deploy.sh --teardown --delete-environment --delete-registry
```

Delete the app resource group:

### PowerShell

```powershell
.\deploy\deploy.ps1 --teardown --delete-resource-group --yes
```

### Bash

```bash
./deploy/deploy.sh --teardown --delete-resource-group --yes
```

If your environment or registry live in shared resource groups, do not use the delete
flags casually. Those resources may be used by more than Dexter.

## Files in this folder
- `deploy.py`: Azure SDK deploy + teardown logic
- `deploy.ps1`: PowerShell wrapper
- `deploy.sh`: bash wrapper
- `requirements.txt`: Python dependencies for deploy tooling
- `Dockerfile`: Dexter brain container for ACA
- `Dockerfile.dockerignore`: keeps secrets and local cruft out of the image
