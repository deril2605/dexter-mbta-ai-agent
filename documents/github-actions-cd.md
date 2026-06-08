# GitHub Actions CD for Azure Container Apps

Last updated: 2026-06-08

This note captures the GitHub Actions deployment path for Dexter and the Azure
configuration required to make it work.

## Purpose

Deploy Dexter automatically after changes land on `main`, without relying on a
laptop-local Docker daemon or the broad admin behavior in `deploy/deploy.py`.

This is a narrower production path than the local deploy script:
- PRs run validation only
- merges to `main` build and push a new image
- the existing Container App is updated in place

## Repository workflows

### CI

File: `.github/workflows/ci.yml`

Triggers:
- `pull_request`
- `workflow_dispatch`

Behavior:
- checks out the repo
- installs dependencies with `uv`
- runs `ruff check .`
- runs `pytest`
- never authenticates to Azure

### Deploy ACA

File: `.github/workflows/deploy.yml`

Triggers:
- `push` to `main`
- `workflow_dispatch`

Behavior:
- authenticates to Azure using GitHub OIDC via `azure/login`
- builds the image from `deploy/Dockerfile`
- pushes `dealsignalacr12345.azurecr.io/dexter:<commit-sha>`
- updates Container App `dexter-beta`
- stamps `DEXTER_DEPLOYED_AT`
- smoke-tests `/health`

The deploy workflow is path-filtered. A merge to `main` triggers deployment only
when runtime-relevant files change:
- `dexter/**`
- `web/**`
- `deploy/**`
- `pyproject.toml`
- `uv.lock`
- `.github/workflows/deploy.yml`

This avoids production deploys for documentation-only changes such as
`documents/**`.

## Shared Azure target

- Resource group: `rg-dealsignal-prod`
- Container Apps environment: `dealsignal-env`
- Container registry: `dealsignalacr12345`
- Container app: `dexter-beta`
- Image repository: `dexter`

The app remains CPU-only. The shared CAE may support GPU workloads, but Dexter
does not request GPU resources by default.

## Why GitHub Actions does not call `deploy.py`

The local deploy script is intentionally broad. It can:
- check and register providers
- create or reuse shared resource groups
- create or reuse ACR and CAE
- enable the ACR admin user
- assign `AcrPull`

That is useful for manual/bootstrap administration, but too broad for a public
repo's routine production workflow.

The GitHub Actions deploy path is intentionally narrower:
- assume shared infra already exists
- push a new image
- update the existing Container App
- avoid long-lived Azure secrets

## GitHub setup

### Environment

Create a GitHub environment named `production`.

Recommended:
- require approval before deployment
- restrict who can approve production runs

### Environment variables

Under the `production` environment, add these variables:

- `AZURE_CLIENT_ID`
- `AZURE_TENANT_ID`
- `AZURE_SUBSCRIPTION_ID`

Use variables, not secrets, for these three values. They are identifiers, not
credentials.

## Azure setup

### App registration

Create or reuse a Microsoft Entra app registration for GitHub Actions, for
example `dexter-github-actions`.

Useful values from the Overview page:
- Application (client) ID -> `AZURE_CLIENT_ID`
- Directory (tenant) ID -> `AZURE_TENANT_ID`
- Subscription ID -> `AZURE_SUBSCRIPTION_ID`

### Federated credential

In the app registration:
1. Open `Certificates & secrets`
2. Open `Federated credentials`
3. Add a GitHub Actions federated credential

Recommended trust shape:
- organization: repo owner
- repository: this repo
- entity type: `Environment`
- environment: `production`

This allows Azure login without storing a client secret in GitHub.

### Azure role assignments

Grant the GitHub Actions identity:

- `AcrPush` on ACR `dealsignalacr12345`
- `Contributor` on RG `rg-dealsignal-prod`

Why:
- `AcrPush` allows image pushes to ACR
- `Contributor` allows updating ACA resources in the shared RG

Unlike the local deploy script, the workflow should not need
`User Access Administrator` if the Container App's managed identity already has
the required `AcrPull` relationship in place.

## Public repo security posture

This repository is public, so the deployment workflow should stay conservative:
- do not deploy from `pull_request`
- do not expose deployment credentials to PR workflows
- authenticate only on post-merge or manual deployment runs
- prefer OIDC over long-lived client secrets

PRs are validated by CI only. Production deployment happens on `push` to `main`
or via manual dispatch.

## Deploy metadata

The workflow stamps `DEXTER_DEPLOYED_AT` during deployment.

The app surfaces that value through `/health`, and the web UI header shows
`last redeployed` using that timestamp.

This gives a visible signal that the deployment workflow actually updated the
live app.

## Validation performed

On branch `dr-github-actions-cd`:
- `uv run ruff check .` passed
- `uv run pytest` passed

The deployment workflow was also validated indirectly by a live run: the app's
`last redeployed` timestamp changed after deployment, confirming the ACA update
path and deploy-time stamping worked end to end.

## Operational guidance

- Prefer GitHub Actions for routine production deploys from `main`.
- Keep `uv run python deploy/deploy.py` as the manual fallback/admin path.
- Use `workflow_dispatch` when a manual redeploy is needed without another merge.
- Consider an image retention or cleanup policy for SHA-tagged ACR images over time.
