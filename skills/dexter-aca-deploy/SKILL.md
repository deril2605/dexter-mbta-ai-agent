---
name: dexter-aca-deploy
description: Redeploy the Dexter MBTA app to its shared Azure Container Apps environment and troubleshoot the deploy flow. Use when asked to deploy or redeploy this repo's code to Azure Container Apps, verify the shared ACA/ACR settings, update deploy-related config or docs, or diagnose failures in deploy/deploy.py, Docker build/push, ACR access, or Container Apps rollout.
---

# Dexter ACA Deploy

## Overview

Use this skill to deploy the current repo to the shared Azure Container Apps setup for Dexter.
Prefer the existing Python deploy flow in `deploy/deploy.py`; do not invent a separate Azure workflow unless the user explicitly asks to change deployment architecture.

## Workflow

1. Inspect `deploy/`, `.env`, and `deploy/deploy.md` before changing anything.
2. Confirm the shared-infra target still matches:
   - resource group `rg-dealsignal-prod`
   - Container Apps environment `dealsignal-env`
   - registry `dealsignalacr12345`
   - app `dexter-beta`
3. Redeploy from the repo root with:

```powershell
uv run python deploy/deploy.py
```

4. If the deploy succeeds, report the live ACA URL and mention the new image tag.
5. If the deploy fails, diagnose the failing layer before suggesting fixes:
   - Azure auth or RBAC
   - resource provider / shared resource lookup
   - Docker daemon or local build
   - ACR login or push
   - ACA create or update
   - runtime startup after deployment

## Operating Rules

- Treat `deploy/deploy.py` as the source of truth for the deploy flow.
- Reuse shared infrastructure; do not casually switch to new RG, CAE, or ACR names.
- Keep Dexter CPU-only unless the user explicitly asks for GPU. A shared CAE may support GPU, but Dexter should not request GPU resources by default.
- Do not use teardown flags against shared infrastructure unless the user explicitly asks and understands the blast radius.
- If deployment behavior changes, update both `.env.example` and `deploy/deploy.md`.

## Prerequisites Checklist

Before running a deploy, verify:

- Docker is installed and the daemon is running.
- The repo `.env` contains Azure auth, Azure OpenAI, passcode, and deploy settings.
- The chosen Python interpreter has the Azure SDK packages required by `deploy/deploy.py`.
- The service principal or user has access to the shared RG, CAE, ACR, and role assignment operations needed by the deploy script.

Use [references/shared-aca-runbook.md](references/shared-aca-runbook.md) for the exact target values, common failure modes, and recovery guidance.

## Common Responses

- If Docker cannot connect to the daemon, tell the user to start Docker Desktop and verify `docker info`.
- If Azure says a provider or RG already exists, prefer reuse over create-or-update.
- If the deploy succeeds but the app fails afterward, move to ACA logs and app health checks rather than changing the deploy flow first.
- If asked how to redeploy after code changes, keep the answer simple: rerun `uv run python deploy/deploy.py`.

## Output Style

- Report the concrete command you ran.
- Quote the ACA URL when deployment succeeds.
- Call out whether the issue is local, Azure RBAC, ACR, or ACA.
- Keep follow-up instructions short and executable.
