# Shared ACA Runbook

## Current Shared Target

- Resource group: `rg-dealsignal-prod`
- Container Apps environment: `dealsignal-env`
- Container registry: `dealsignalacr12345`
- Container app: `dexter-beta`
- Image repository: `dexter`

## Primary Deploy Command

Run from the repo root:

```powershell
uv run python deploy/deploy.py
```

The script:
- reads `.env`
- builds the Docker image locally
- pushes to ACR
- updates the ACA app

## Known Local Requirements

- Docker Desktop must be running.
- `docker info` must succeed before deploy.
- The Python interpreter used for deploy must have:
  - `azure-core`
  - `azure-identity`
  - `azure-mgmt-appcontainers`
  - `azure-mgmt-authorization`
  - `azure-mgmt-containerregistry`
  - `azure-mgmt-resource`

## Expected Shared-Infra Behavior

- Resource providers should already be registered.
- The script should reuse an existing RG, CAE, and ACR when present.
- The script may enable the ACR admin user if needed for Docker push auth.
- Dexter should remain CPU-only even if the shared CAE can host GPU workloads.

## Common Failure Modes

### Docker daemon unavailable

Symptoms:
- `failed to connect to the docker API`
- missing `dockerDesktopLinuxEngine` pipe

Action:
- start Docker Desktop
- wait for engine startup
- rerun after `docker info` succeeds

### Azure RBAC failure

Symptoms:
- `AuthorizationFailed`
- missing permissions for provider registration, RG writes, ACR reads, or role assignments

Action:
- confirm the principal has sufficient access to `rg-dealsignal-prod`
- confirm `User Access Administrator` if role assignments are required

### ACR admin-user update shape errors

Symptoms:
- `InvalidRequestContent`
- top-level `admin_user_enabled` payload rejected

Action:
- keep the SDK-native `RegistryUpdateParameters(properties=...)` payload in `deploy.py`

### Environment location mismatch

Symptoms:
- deploy reports the existing CAE location differs from `.env`

Action:
- allow the script to reuse the actual environment location
- update docs or `.env.example` later if needed for clarity
