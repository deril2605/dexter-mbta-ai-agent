# Deploying the Dexter beta to Azure Container Apps

This directory is **fully decoupled** from the app. Delete `web/` and `deploy/` and
set `DEXTER_SERVE_WEB=false` to return Dexter to its Phase 1 (local-only) shape.

The web UI is the CLI's twin: a dumb static client that only calls `POST /chat`.
The brain serves it from the same origin, so there is **no CORS** to configure.

## What gets deployed
- One container (`deploy/Dockerfile`) running `uvicorn dexter.service.app:app`.
- Serves the terminal UI at `/`, the API at `/chat`, health at `/health`.
- Gated by a shared passcode; traces shipped to **Phoenix Cloud**.

## Prerequisites
- `az` CLI logged in (`az login`), and `az extension add --name containerapp`.
- A resource group and an Azure Container Registry (ACR), or create them below.
- Azure OpenAI creds, optional MBTA key, a chosen passcode.
- A Phoenix Cloud account → an OTLP endpoint + API key (Settings → API keys).

## 1. Variables (fill these in)
```bash
RG=dexter-rg
LOC=eastus
ACR=dexteracr$RANDOM          # must be globally unique, lowercase
APP=dexter
ENVNAME=dexter-env
IMAGE=$ACR.azurecr.io/dexter:latest
```

## 2. One-time infra
```bash
az group create -n $RG -l $LOC
az acr create -n $ACR -g $RG --sku Basic --admin-enabled true
az containerapp env create -n $ENVNAME -g $RG -l $LOC
```

## 3. Build & push the image
Build context is the **repo root** so the Dockerfile can copy `dexter/` and `web/`.
```bash
# from the repo root:
az acr build -r $ACR -t dexter:latest -f deploy/Dockerfile .
```
(Or local Docker: `docker build -f deploy/Dockerfile -t $IMAGE . && az acr login -n $ACR && docker push $IMAGE`.)

## 4. Create the Container App
`--min-replicas 1` keeps one warm instance: **no cold start** and the `/routes`
cache (warmed on startup) stays hot. Secrets are passed as ACA secrets, never baked
into the image.
```bash
az containerapp create \
  -n $APP -g $RG --environment $ENVNAME \
  --image $IMAGE \
  --registry-server $ACR.azurecr.io \
  --target-port 8000 --ingress external \
  --min-replicas 1 --max-replicas 3 \
  --secrets \
     azure-key="<AZURE_OPENAI_API_KEY>" \
     mbta-key="<MBTA_API_KEY>" \
     passcode="<CHOOSE_A_PASSCODE>" \
     phoenix-key="<PHOENIX_API_KEY>" \
  --env-vars \
     AZURE_OPENAI_ENDPOINT="<...>" \
     AZURE_OPENAI_API_VERSION="2024-06-01" \
     AZURE_OPENAI_DEPLOYMENT_ROUTER="<deployment-name>" \
     AZURE_OPENAI_API_KEY=secretref:azure-key \
     MBTA_API_KEY=secretref:mbta-key \
     DEXTER_SERVE_WEB=true \
     DEXTER_PASSCODE=secretref:passcode \
     DEXTER_TRACING=true \
     DEXTER_TRACING_ENDPOINT="<phoenix-cloud-otlp-endpoint>" \
     DEXTER_TRACING_API_KEY=secretref:phoenix-key
```
Grab the public URL:
```bash
az containerapp show -n $APP -g $RG --query properties.configuration.ingress.fqdn -o tsv
```
Share `https://<fqdn>/` with your tester. They'll be asked for the passcode on their
first message.

## 5. Push a change during the beta
```bash
az acr build -r $ACR -t dexter:latest -f deploy/Dockerfile .   # from repo root
az containerapp update -n $APP -g $RG --image $IMAGE           # new revision
```

## Notes
- Single uvicorn worker on purpose: the route cache and the LangGraph checkpointer
  are per-process in-memory state. Scale out via replicas, not workers, and treat
  multi-turn affinity as best-effort for the beta.
- To pull the whole thing later: `az containerapp delete -n $APP -g $RG`, then remove
  `web/` and `deploy/` and flip `DEXTER_SERVE_WEB=false`.
