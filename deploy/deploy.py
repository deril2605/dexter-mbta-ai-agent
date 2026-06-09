#!/usr/bin/env python3
from __future__ import annotations

import argparse
import getpass
import os
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

try:
    from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
    from azure.identity import DefaultAzureCredential
    from azure.mgmt.appcontainers import ContainerAppsAPIClient
    from azure.mgmt.authorization import AuthorizationManagementClient
    from azure.mgmt.containerregistry import ContainerRegistryManagementClient
    from azure.mgmt.containerregistry.models import (
        RegistryPropertiesUpdateParameters,
        RegistryUpdateParameters,
    )
    from azure.mgmt.resource import ResourceManagementClient
    from azure.mgmt.storage import StorageManagementClient
except ImportError as exc:  # pragma: no cover - import path depends on local install
    print(
        "Missing Azure SDK dependency. Install them with:\n"
        "  python -m pip install -r deploy/requirements.txt",
        file=sys.stderr,
    )
    raise SystemExit(1) from exc


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BOOTSTRAP_IMAGE = "mcr.microsoft.com/azuredocs/containerapps-helloworld:latest"

# Saved commutes live in a SQLite file on a durable Azure Files share mounted into
# the container at this path. SQLite is embedded (no DB server, scales to zero with
# the app), while the data persists on the share across restarts/scale-to-zero. Safe
# with a single writer — keep maxReplicas=1 while using this.
DATA_VOLUME_NAME = "dexter-data"
DATA_MOUNT_PATH = "/data"
DB_FILE_PATH = f"{DATA_MOUNT_PATH}/dexter.db"


def info(message: str) -> None:
    print(f"[dexter-deploy] {message}")


def fail(message: str) -> NoReturn:
    raise SystemExit(f"Error: {message}")


def require_executable(name: str) -> str:
    path = shutil.which(name)
    if not path:
        fail(f"Missing required executable '{name}'. Install it and retry.")
    return path


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in raw_line:
            continue
        key, value = raw_line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if value.startswith("#"):
            value = ""
        elif len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def seed_process_env(merged_env: dict[str, str], *keys: str) -> None:
    for key in keys:
        value = merged_env.get(key, "").strip()
        if value and not os.environ.get(key):
            os.environ[key] = value


def resolve_config_value(
    merged_env: dict[str, str],
    key: str,
    prompt: str,
    *,
    secret: bool = False,
    optional: bool = False,
    default: str | None = None,
) -> str:
    existing = merged_env.get(key, "").strip()
    if existing:
        return existing

    while True:
        if secret:
            value = getpass.getpass(f"{prompt}: ")
        else:
            label = f"{prompt}"
            if default:
                label += f" [{default}]"
            value = input(f"{label}: ").strip()
            if not value and default is not None:
                value = default
        if value or optional:
            return value
        print("A value is required.", file=sys.stderr)


def deterministic_registry_name(subscription_id: str, app_name: str) -> str:
    base = "".join(ch for ch in app_name.lower() if ch.isalnum())
    if len(base) < 6:
        base = "dexterbeta"
    suffix = subscription_id.replace("-", "").lower()[:18]
    return f"{base}{suffix}"[:50]


def deterministic_storage_account_name(subscription_id: str, app_name: str) -> str:
    """A globally-unique-ish storage account name (3-24 lowercase alphanumerics)."""
    base = "".join(ch for ch in app_name.lower() if ch.isalnum()) or "dexter"
    suffix = subscription_id.replace("-", "").lower()
    return f"{base}{suffix}"[:24]


def build_revision_suffix(image_tag: str) -> str:
    seed = f"deploy-{image_tag}-{datetime.now(UTC).strftime('%H%M%S')}".lower()
    cleaned = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in seed).strip("-")
    return (cleaned or "deploy")[:40].strip("-") or "deploy"


def build_deployed_at_label() -> str:
    # Emit an ISO-8601 UTC instant; the app formats it to Eastern for display so
    # there's a single source of truth for time formatting (DEXTER_DEPLOYED_AT).
    return datetime.now(UTC).isoformat()


def run_subprocess(command: list[str], *, input_text: str | None = None) -> None:
    try:
        subprocess.run(command, check=True, text=True, input=input_text)
    except subprocess.CalledProcessError as exc:
        rendered = " ".join(command)
        fail(f"Command failed ({exc.returncode}): {rendered}")


def ensure_provider(resource_client: ResourceManagementClient, namespace: str) -> None:
    provider = resource_client.providers.get(namespace)
    state = (get_attr(provider, "registration_state") or "").lower()
    if state == "registered":
        info(f"Azure resource provider {namespace} is already registered")
        return

    info(f"Registering Azure resource provider {namespace}")
    resource_client.providers.register(namespace)


@dataclass
class DeployConfig:
    subscription_id: str
    resource_group: str
    location: str
    environment_resource_group: str
    environment_name: str
    registry_resource_group: str
    app_name: str
    registry_name: str
    image_repository: str
    image_tag: str
    min_replicas: int
    max_replicas: int
    storage_account_name: str
    file_share_name: str
    env_storage_name: str
    docker_exe: str
    azure_openai_endpoint: str
    azure_openai_api_key: str
    azure_openai_api_version: str
    azure_openai_deployment_router: str
    mbta_api_key: str
    dexter_passcode: str
    dexter_deployed_at: str
    dexter_tracing_endpoint: str
    dexter_tracing_api_key: str
    mbta_base_url: str
    log_level: str

    @property
    def registry_server(self) -> str:
        return f"{self.registry_name}.azurecr.io"

    @property
    def image_reference(self) -> str:
        return f"{self.registry_server}/{self.image_repository}:{self.image_tag}"

    @property
    def tracing_enabled(self) -> bool:
        return bool(self.dexter_tracing_endpoint or self.dexter_tracing_api_key)


def collect_config(args: argparse.Namespace, merged_env: dict[str, str]) -> DeployConfig:
    subscription_id = args.subscription_id or resolve_config_value(
        merged_env,
        "AZURE_SUBSCRIPTION_ID",
        "Azure subscription ID",
    )
    app_name = (
        args.app_name
        or merged_env.get("AZURE_CONTAINERAPP_NAME", "").strip()
        or "dexter-beta"
    )
    resource_group = (
        args.resource_group
        or merged_env.get("AZURE_RESOURCE_GROUP", "").strip()
        or f"{app_name}-rg"
    )
    location = args.location or merged_env.get("AZURE_LOCATION", "").strip() or "eastus"
    environment_name = (
        args.environment_name
        or merged_env.get("AZURE_CONTAINERAPPS_ENV_NAME", "").strip()
        or f"{app_name}-env"
    )
    environment_resource_group = (
        args.environment_resource_group
        or merged_env.get("AZURE_CONTAINERAPPS_ENV_RESOURCE_GROUP", "").strip()
        or resource_group
    )
    registry_name = (
        args.registry_name
        or merged_env.get("AZURE_CONTAINER_REGISTRY_NAME", "").strip()
        or deterministic_registry_name(subscription_id, app_name)
    )
    registry_resource_group = (
        args.registry_resource_group
        or merged_env.get("AZURE_CONTAINER_REGISTRY_RESOURCE_GROUP", "").strip()
        or resource_group
    )
    image_repository = (
        args.image_repository
        or merged_env.get("AZURE_CONTAINER_REPOSITORY", "").strip()
        or "dexter"
    )
    image_tag = (
        args.image_tag
        or merged_env.get("AZURE_CONTAINER_IMAGE_TAG", "").strip()
        or datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    )
    docker_exe = (
        args.docker_exe or merged_env.get("AZURE_DOCKER_EXE", "").strip() or "docker"
    )
    max_replicas_raw = (
        str(args.max_replicas)
        if args.max_replicas is not None
        else merged_env.get("AZURE_CONTAINER_MAX_REPLICAS", "").strip() or "1"
    )
    try:
        max_replicas = int(max_replicas_raw)
    except ValueError:
        fail("AZURE_CONTAINER_MAX_REPLICAS must be an integer.")
    if max_replicas < 1:
        fail("AZURE_CONTAINER_MAX_REPLICAS must be at least 1.")
    # Default minReplicas=0 (scale to zero — pay nothing while idle). Saved commutes
    # survive because the SQLite file lives on the mounted Azure Files share, not the
    # ephemeral container. Keep max_replicas=1 so there's a single SQLite writer.
    min_replicas_raw = (
        str(args.min_replicas)
        if args.min_replicas is not None
        else merged_env.get("AZURE_CONTAINER_MIN_REPLICAS", "").strip() or "0"
    )
    try:
        min_replicas = int(min_replicas_raw)
    except ValueError:
        fail("AZURE_CONTAINER_MIN_REPLICAS must be an integer.")
    if min_replicas < 0:
        fail("AZURE_CONTAINER_MIN_REPLICAS must be 0 or greater.")
    if min_replicas > max_replicas:
        fail("AZURE_CONTAINER_MIN_REPLICAS cannot exceed AZURE_CONTAINER_MAX_REPLICAS.")

    storage_account_name = (
        merged_env.get("AZURE_STORAGE_ACCOUNT_NAME", "").strip()
        or deterministic_storage_account_name(subscription_id, app_name)
    )
    file_share_name = merged_env.get("AZURE_FILE_SHARE_NAME", "").strip() or "dexter-data"
    env_storage_name = DATA_VOLUME_NAME

    azure_openai_endpoint = resolve_config_value(
        merged_env, "AZURE_OPENAI_ENDPOINT", "Azure OpenAI endpoint"
    )
    azure_openai_api_key = resolve_config_value(
        merged_env, "AZURE_OPENAI_API_KEY", "Azure OpenAI API key", secret=True
    )
    azure_openai_api_version = resolve_config_value(
        merged_env,
        "AZURE_OPENAI_API_VERSION",
        "Azure OpenAI API version",
        default="2024-06-01",
    )
    azure_openai_deployment_router = resolve_config_value(
        merged_env,
        "AZURE_OPENAI_DEPLOYMENT_ROUTER",
        "Azure OpenAI router deployment name",
    )
    mbta_api_key = resolve_config_value(
        merged_env,
        "MBTA_API_KEY",
        "MBTA API key (optional; press Enter to skip)",
        secret=True,
        optional=True,
    )
    dexter_passcode = resolve_config_value(
        merged_env,
        "DEXTER_PASSCODE",
        "Dexter beta passcode",
        secret=True,
    )
    dexter_deployed_at = build_deployed_at_label()

    dexter_tracing_endpoint = merged_env.get("DEXTER_TRACING_ENDPOINT", "").strip()
    dexter_tracing_api_key = merged_env.get("DEXTER_TRACING_API_KEY", "").strip()
    if dexter_tracing_api_key and not dexter_tracing_endpoint:
        dexter_tracing_endpoint = resolve_config_value(
            merged_env,
            "DEXTER_TRACING_ENDPOINT",
            "Phoenix Cloud OTLP endpoint",
        )

    mbta_base_url = merged_env.get("MBTA_BASE_URL", "").strip() or "https://api-v3.mbta.com"
    log_level = merged_env.get("LOG_LEVEL", "").strip() or "INFO"

    return DeployConfig(
        subscription_id=subscription_id,
        resource_group=resource_group,
        location=location,
        environment_resource_group=environment_resource_group,
        environment_name=environment_name,
        registry_resource_group=registry_resource_group,
        app_name=app_name,
        registry_name=registry_name,
        image_repository=image_repository,
        image_tag=image_tag,
        min_replicas=min_replicas,
        max_replicas=max_replicas,
        storage_account_name=storage_account_name,
        file_share_name=file_share_name,
        env_storage_name=env_storage_name,
        docker_exe=docker_exe,
        azure_openai_endpoint=azure_openai_endpoint,
        azure_openai_api_key=azure_openai_api_key,
        azure_openai_api_version=azure_openai_api_version,
        azure_openai_deployment_router=azure_openai_deployment_router,
        mbta_api_key=mbta_api_key,
        dexter_passcode=dexter_passcode,
        dexter_deployed_at=dexter_deployed_at,
        dexter_tracing_endpoint=dexter_tracing_endpoint,
        dexter_tracing_api_key=dexter_tracing_api_key,
        mbta_base_url=mbta_base_url,
        log_level=log_level,
    )


def get_attr(obj: Any, *names: str) -> Any:
    current = obj
    for name in names:
        if current is None:
            return None
        if isinstance(current, dict):
            current = current.get(name)
        else:
            current = getattr(current, name, None)
    return current


def ensure_resource_group(
    resource_client: ResourceManagementClient, resource_group_name: str, location: str
) -> None:
    try:
        resource_client.resource_groups.get(resource_group_name)
        info(f"Resource group {resource_group_name} exists; reusing it")
    except ResourceNotFoundError:
        info(f"Creating resource group {resource_group_name}")
        resource_client.resource_groups.create_or_update(
            resource_group_name, {"location": location}
        )


def ensure_resource_groups(resource_client: ResourceManagementClient, config: DeployConfig) -> None:
    seen: set[str] = set()
    for resource_group_name in (
        config.resource_group,
        config.environment_resource_group,
        config.registry_resource_group,
    ):
        if resource_group_name in seen:
            continue
        seen.add(resource_group_name)
        ensure_resource_group(resource_client, resource_group_name, config.location)


def ensure_registry(
    acr_client: ContainerRegistryManagementClient, config: DeployConfig
) -> Any:
    try:
        registry = acr_client.registries.get(
            config.registry_resource_group, config.registry_name
        )
        info(f"Container registry {config.registry_name} exists; reusing it")
        if not getattr(registry, "admin_user_enabled", False):
            info("Enabling admin user on the existing container registry for Docker push auth")
            poller = acr_client.registries.begin_update(
                config.registry_resource_group,
                config.registry_name,
                RegistryUpdateParameters(
                    properties=RegistryPropertiesUpdateParameters(admin_user_enabled=True)
                ),
            )
            registry = poller.result()
        return registry
    except ResourceNotFoundError:
        info(f"Creating container registry {config.registry_name}")
        poller = acr_client.registries.begin_create(
            config.registry_resource_group,
            config.registry_name,
            {
                "location": config.location,
                "sku": {"name": "Basic"},
                "admin_user_enabled": True,
            },
        )
        return poller.result()


def get_registry_credentials(
    acr_client: ContainerRegistryManagementClient, config: DeployConfig
) -> tuple[str, str]:
    credentials = acr_client.registries.list_credentials(
        config.registry_resource_group, config.registry_name
    )
    username = get_attr(credentials, "username")
    passwords = get_attr(credentials, "passwords") or []
    password = None
    for entry in passwords:
        password = get_attr(entry, "value")
        if password:
            break
    if not username or not password:
        fail("Could not retrieve ACR admin credentials for Docker push.")
    return username, password


def docker_build_and_push(
    config: DeployConfig, registry_username: str, registry_password: str
) -> None:
    docker_path = require_executable(config.docker_exe)
    info(f"Logging Docker into {config.registry_server}")
    run_subprocess(
        [
            docker_path,
            "login",
            config.registry_server,
            "--username",
            registry_username,
            "--password-stdin",
        ],
        input_text=registry_password,
    )

    info(f"Building image {config.image_reference}")
    run_subprocess(
        [
            docker_path,
            "build",
            "-f",
            str(REPO_ROOT / "deploy" / "Dockerfile"),
            "-t",
            config.image_reference,
            str(REPO_ROOT),
        ]
    )

    info(f"Pushing image {config.image_reference}")
    run_subprocess([docker_path, "push", config.image_reference])

    subprocess.run([docker_path, "logout", config.registry_server], check=False, text=True)


def ensure_environment(app_client: ContainerAppsAPIClient, config: DeployConfig) -> Any:
    try:
        environment = app_client.managed_environments.get(
            config.environment_resource_group, config.environment_name
        )
        info(f"Container app environment {config.environment_name} exists; reusing it")
        return environment
    except ResourceNotFoundError:
        info(f"Creating container app environment {config.environment_name}")
        poller = app_client.managed_environments.begin_create_or_update(
            resource_group_name=config.environment_resource_group,
            environment_name=config.environment_name,
            environment_envelope={
                "location": config.location,
                "properties": {},
            },
        )
        return poller.result()


def ensure_storage(storage_client: StorageManagementClient, config: DeployConfig) -> str:
    """Ensure the storage account + Azure Files share exist; return an account key.

    The share holds the SQLite file (saved commutes). Idempotent: reuses an existing
    account/share on redeploys, so rider data is never wiped by a deploy.
    """
    rg = config.resource_group
    name = config.storage_account_name
    try:
        storage_client.storage_accounts.get_properties(rg, name)
        info(f"Storage account {name} exists; reusing it")
    except ResourceNotFoundError:
        info(f"Creating storage account {name}")
        storage_client.storage_accounts.begin_create(
            rg,
            name,
            {
                "sku": {"name": "Standard_LRS"},
                "kind": "StorageV2",
                "location": config.location,
            },
        ).result()

    info(f"Ensuring Azure Files share {config.file_share_name}")
    try:
        storage_client.file_shares.create(rg, name, config.file_share_name, {})
    except HttpResponseError as exc:
        if exc.status_code != 409:  # 409 = share already exists, which is fine
            raise

    keys = storage_client.storage_accounts.list_keys(rg, name)
    for entry in get_attr(keys, "keys") or []:
        if value := get_attr(entry, "value"):
            return value
    fail("Could not retrieve a storage account key for the Azure Files mount.")


def ensure_env_storage(
    app_client: ContainerAppsAPIClient, config: DeployConfig, account_key: str
) -> None:
    """Register the Azure Files share as managed-environment storage (so it's mountable)."""
    info(f"Registering Azure Files storage '{config.env_storage_name}' on the environment")
    app_client.managed_environments_storages.create_or_update(
        resource_group_name=config.environment_resource_group,
        environment_name=config.environment_name,
        storage_name=config.env_storage_name,
        storage_envelope={
            "properties": {
                "azureFile": {
                    "accountName": config.storage_account_name,
                    "accountKey": account_key,
                    "shareName": config.file_share_name,
                    "accessMode": "ReadWrite",
                }
            }
        },
    )


def get_container_app(app_client: ContainerAppsAPIClient, config: DeployConfig) -> Any | None:
    try:
        return app_client.container_apps.get(config.resource_group, config.app_name)
    except ResourceNotFoundError:
        return None


def ensure_bootstrap_app(
    app_client: ContainerAppsAPIClient, config: DeployConfig, environment_id: str
) -> Any:
    app = get_container_app(app_client, config)
    principal_id = get_attr(app, "identity", "principal_id")
    if app is not None and principal_id:
        info(f"Container app {config.app_name} exists; reusing it")
        return app

    info(f"Creating bootstrap container app {config.app_name}")
    poller = app_client.container_apps.begin_create_or_update(
        resource_group_name=config.resource_group,
        container_app_name=config.app_name,
        container_app_envelope={
            "location": config.location,
            "identity": {"type": "SystemAssigned"},
            "properties": {
                "environmentId": environment_id,
                "configuration": {
                    "activeRevisionsMode": "Single",
                    "ingress": {
                        "external": True,
                        "targetPort": 8000,
                        "transport": "auto",
                        "allowInsecure": False,
                        "traffic": [{"latestRevision": True, "weight": 100}],
                    },
                    "secrets": [],
                },
                "template": {
                    "containers": [
                        {
                            "name": config.app_name,
                            "image": DEFAULT_BOOTSTRAP_IMAGE,
                        }
                    ],
                    "scale": {
                        "minReplicas": config.min_replicas,
                        "maxReplicas": config.max_replicas,
                    },
                },
            },
        },
    )
    app = poller.result()
    principal_id = get_attr(app, "identity", "principal_id")
    if not principal_id:
        fail("Could not resolve the container app managed identity principal ID.")
    return app


def ensure_acrpull_role(
    auth_client: AuthorizationManagementClient, principal_id: str, scope: str
) -> None:
    role_defs = list(auth_client.role_definitions.list(scope, filter="roleName eq 'AcrPull'"))
    if not role_defs:
        fail("Could not find the built-in AcrPull role definition.")
    role_definition_id = role_defs[0].id
    assignment_name = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"{scope}|{principal_id}|{role_definition_id}")
    )

    for attempt in range(1, 7):
        try:
            auth_client.role_assignments.create(
                scope,
                assignment_name,
                {
                    "role_definition_id": role_definition_id,
                    "principal_id": principal_id,
                    "principal_type": "ServicePrincipal",
                },
            )
            return
        except HttpResponseError as exc:
            code = getattr(exc, "error", None)
            code_value = getattr(code, "code", "")
            if code_value == "RoleAssignmentExists" or exc.status_code == 409:
                return
            if attempt < 6:
                info("Waiting for managed identity propagation before retrying AcrPull assignment")
                time.sleep(10)
                continue
            raise


def desired_secrets(config: DeployConfig) -> list[dict[str, str]]:
    secrets = [
        {"name": "azure-openai-endpoint", "value": config.azure_openai_endpoint},
        {"name": "azure-openai-api-key", "value": config.azure_openai_api_key},
        {"name": "azure-openai-api-version", "value": config.azure_openai_api_version},
        {
            "name": "azure-openai-deployment-router",
            "value": config.azure_openai_deployment_router,
        },
        {"name": "dexter-passcode", "value": config.dexter_passcode},
        {"name": "dexter-deployed-at", "value": config.dexter_deployed_at},
    ]
    if config.mbta_api_key:
        secrets.append({"name": "mbta-api-key", "value": config.mbta_api_key})
    if config.dexter_tracing_endpoint:
        secrets.append(
            {"name": "dexter-tracing-endpoint", "value": config.dexter_tracing_endpoint}
        )
    if config.dexter_tracing_api_key:
        secrets.append({"name": "dexter-tracing-api-key", "value": config.dexter_tracing_api_key})
    return secrets


def desired_env(config: DeployConfig) -> list[dict[str, str]]:
    env = [
        {"name": "AZURE_OPENAI_ENDPOINT", "secretRef": "azure-openai-endpoint"},
        {"name": "AZURE_OPENAI_API_KEY", "secretRef": "azure-openai-api-key"},
        {"name": "AZURE_OPENAI_API_VERSION", "secretRef": "azure-openai-api-version"},
        {
            "name": "AZURE_OPENAI_DEPLOYMENT_ROUTER",
            "secretRef": "azure-openai-deployment-router",
        },
        {"name": "DEXTER_PASSCODE", "secretRef": "dexter-passcode"},
        {"name": "DEXTER_DEPLOYED_AT", "secretRef": "dexter-deployed-at"},
        {"name": "DEXTER_SERVE_WEB", "value": "true"},
        {"name": "DEXTER_TRACING", "value": "true" if config.tracing_enabled else "false"},
        {"name": "MBTA_BASE_URL", "value": config.mbta_base_url},
        {"name": "LOG_LEVEL", "value": config.log_level},
        # Saved commutes persist to the mounted Azure Files share (survives scale-to-zero).
        {"name": "DEXTER_DB_PATH", "value": DB_FILE_PATH},
    ]
    if config.mbta_api_key:
        env.append({"name": "MBTA_API_KEY", "secretRef": "mbta-api-key"})
    if config.dexter_tracing_endpoint:
        env.append({"name": "DEXTER_TRACING_ENDPOINT", "secretRef": "dexter-tracing-endpoint"})
    if config.dexter_tracing_api_key:
        env.append({"name": "DEXTER_TRACING_API_KEY", "secretRef": "dexter-tracing-api-key"})
    return env


def deploy_container_app(
    app_client: ContainerAppsAPIClient,
    config: DeployConfig,
    environment_id: str,
) -> Any:
    info("Applying Dexter container app configuration")
    poller = app_client.container_apps.begin_create_or_update(
        resource_group_name=config.resource_group,
        container_app_name=config.app_name,
        container_app_envelope={
            "location": config.location,
            "identity": {"type": "SystemAssigned"},
            "properties": {
                "environmentId": environment_id,
                "configuration": {
                    "activeRevisionsMode": "Single",
                    "ingress": {
                        "external": True,
                        "targetPort": 8000,
                        "transport": "auto",
                        "allowInsecure": False,
                        "traffic": [{"latestRevision": True, "weight": 100}],
                    },
                    "registries": [{"server": config.registry_server, "identity": "system"}],
                    "secrets": desired_secrets(config),
                },
                "template": {
                    "revisionSuffix": build_revision_suffix(config.image_tag),
                    "containers": [
                        {
                            "name": config.app_name,
                            "image": config.image_reference,
                            "env": desired_env(config),
                            "volumeMounts": [
                                {
                                    "volumeName": DATA_VOLUME_NAME,
                                    "mountPath": DATA_MOUNT_PATH,
                                }
                            ],
                        }
                    ],
                    "volumes": [
                        {
                            "name": DATA_VOLUME_NAME,
                            "storageType": "AzureFile",
                            "storageName": config.env_storage_name,
                        }
                    ],
                    "scale": {
                        "minReplicas": config.min_replicas,
                        "maxReplicas": config.max_replicas,
                    },
                },
            },
        },
    )
    return poller.result()


def get_fqdn(app: Any) -> str:
    fqdn = get_attr(app, "configuration", "ingress", "fqdn")
    if not fqdn:
        fqdn = get_attr(app, "properties", "configuration", "ingress", "fqdn")
    if not fqdn:
        fqdn = get_attr(app, "latest_revision_fqdn")
    if not fqdn:
        fail("Deployment finished, but no ingress FQDN was returned.")
    return fqdn


def teardown(
    args: argparse.Namespace,
    resource_client: ResourceManagementClient,
    app_client: ContainerAppsAPIClient,
    acr_client: ContainerRegistryManagementClient,
    config: DeployConfig,
) -> None:
    if not args.yes:
        confirm = input(
            f"Delete ACA resources for '{config.app_name}' in "
            f"'{config.resource_group}'? Type 'yes' to continue: "
        ).strip()
        if confirm != "yes":
            fail("Teardown cancelled.")

    if args.delete_resource_group:
        info(f"Deleting resource group {config.resource_group}")
        resource_client.resource_groups.begin_delete(config.resource_group)
        print(f"Started resource group deletion for '{config.resource_group}'.")
        return

    app = get_container_app(app_client, config)
    if app is not None:
        info(f"Deleting container app {config.app_name}")
        app_client.container_apps.begin_delete(config.resource_group, config.app_name).result()
    else:
        info(f"Container app {config.app_name} does not exist; skipping")

    if args.delete_environment:
        try:
            info(f"Deleting container app environment {config.environment_name}")
            app_client.managed_environments.begin_delete(
                config.environment_resource_group, config.environment_name
            ).result()
        except ResourceNotFoundError:
            info(f"Container app environment {config.environment_name} does not exist; skipping")

    if args.delete_registry:
        try:
            info(f"Deleting Azure Container Registry {config.registry_name}")
            acr_client.registries.begin_delete(
                config.registry_resource_group, config.registry_name
            ).result()
        except ResourceNotFoundError:
            info(f"Azure Container Registry {config.registry_name} does not exist; skipping")

    print(f"Teardown complete for '{config.app_name}'.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Deploy or tear down the Dexter beta on Azure Container Apps using "
            "the Azure Python SDK."
        )
    )
    parser.add_argument("--subscription-id")
    parser.add_argument("--resource-group")
    parser.add_argument("--location", default=None)
    parser.add_argument("--environment-resource-group")
    parser.add_argument("--registry-name")
    parser.add_argument("--registry-resource-group")
    parser.add_argument("--app-name")
    parser.add_argument("--environment-name")
    parser.add_argument("--image-repository")
    parser.add_argument("--image-tag")
    parser.add_argument("--env-file")
    parser.add_argument("--min-replicas", type=int)
    parser.add_argument("--max-replicas", type=int)
    parser.add_argument("--docker-exe")
    parser.add_argument("--teardown", action="store_true")
    parser.add_argument("--delete-environment", action="store_true")
    parser.add_argument("--delete-registry", action="store_true")
    parser.add_argument("--delete-resource-group", action="store_true")
    parser.add_argument("--yes", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    env_path = Path(args.env_file) if args.env_file else REPO_ROOT / ".env"
    file_env = load_env_file(env_path)
    merged_env = {**file_env, **os.environ}
    seed_process_env(
        merged_env,
        "AZURE_TENANT_ID",
        "AZURE_CLIENT_ID",
        "AZURE_CLIENT_SECRET",
    )

    config = collect_config(args, merged_env)
    credential = DefaultAzureCredential()
    resource_client = ResourceManagementClient(credential, config.subscription_id)
    acr_client = ContainerRegistryManagementClient(credential, config.subscription_id)
    app_client = ContainerAppsAPIClient(credential, config.subscription_id)
    auth_client = AuthorizationManagementClient(credential, config.subscription_id)
    storage_client = StorageManagementClient(credential, config.subscription_id)

    if args.delete_resource_group and not args.teardown:
        fail("--delete-resource-group only makes sense together with --teardown.")

    if args.teardown:
        teardown(args, resource_client, app_client, acr_client, config)
        return

    ensure_provider(resource_client, "Microsoft.App")
    ensure_provider(resource_client, "Microsoft.ContainerRegistry")
    ensure_provider(resource_client, "Microsoft.Storage")
    ensure_resource_groups(resource_client, config)
    registry = ensure_registry(acr_client, config)
    username, password = get_registry_credentials(acr_client, config)
    docker_build_and_push(config, username, password)

    environment = ensure_environment(app_client, config)
    environment_location = get_attr(environment, "location")
    if environment_location and environment_location != config.location:
        info(
            f"Using existing container app environment location '{environment_location}' "
            f"instead of configured location '{config.location}'"
        )
        config.location = environment_location
    environment_id = get_attr(environment, "id")
    if not environment_id:
        fail("Could not resolve the Container Apps environment resource ID.")

    bootstrap_app = ensure_bootstrap_app(app_client, config, environment_id)
    principal_id = get_attr(bootstrap_app, "identity", "principal_id")
    if not principal_id:
        fail("Could not resolve the container app managed identity principal ID.")

    registry_id = get_attr(registry, "id")
    if not registry_id:
        fail("Could not resolve the ACR resource ID.")
    ensure_acrpull_role(auth_client, principal_id, registry_id)
    time.sleep(5)

    # Durable storage for saved commutes: an Azure Files share mounted into the
    # container, so the SQLite file survives restarts and scale-to-zero.
    account_key = ensure_storage(storage_client, config)
    ensure_env_storage(app_client, config, account_key)

    app = deploy_container_app(app_client, config, environment_id)
    fqdn = get_fqdn(app)

    print()
    print(f"Dexter beta is live at: https://{fqdn}/")
    print(f"Resource group: {config.resource_group}")
    print(
        "Container Apps environment: "
        f"{config.environment_name} ({config.environment_resource_group})"
    )
    print(f"Registry: {config.registry_name}")
    print(f"Image: {config.image_reference}")
    print(
        f"Saved commutes: {config.storage_account_name}/{config.file_share_name} "
        f"mounted at {DB_FILE_PATH}"
    )
    print(f"Scale: minReplicas={config.min_replicas}, maxReplicas={config.max_replicas}")


if __name__ == "__main__":
    main()
