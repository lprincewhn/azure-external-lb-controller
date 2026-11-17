"""Reconcile ready Pod endpoints with a dedicated Azure IP backend pool."""

import hashlib
import ipaddress
import json
import logging
import os
import signal
import threading
from dataclasses import dataclass
from time import monotonic
from types import SimpleNamespace

from azure.identity import DefaultAzureCredential
from azure.mgmt.network import NetworkManagementClient
from azure.mgmt.network.models import (
    LoadBalancerBackendAddress,
    SubResource,
)
from kubernetes import client, config, watch


LOG = logging.getLogger(__name__)
CLASS = "azure-external-lb"
PREFIX = "k8spod-"


@dataclass(frozen=True)
class Settings:
    subscription: str
    resource_group: str
    load_balancer: str
    pool: str
    vnet_id: str
    interval: int

    @classmethod
    def from_env(cls):
        names = (
            "AZURE_SUBSCRIPTION_ID",
            "AZURE_RESOURCE_GROUP",
            "AZURE_LOAD_BALANCER",
            "AZURE_BACKEND_POOL",
            "AZURE_VNET_RESOURCE_ID",
        )
        values = [os.environ.get(name, "").strip() for name in names]
        missing = [name for name, value in zip(names, values) if not value]
        if missing:
            raise ValueError(f"Missing required configuration: {', '.join(missing)}")
        try:
            interval = int(os.environ.get("RECONCILE_INTERVAL_SECONDS", "60"))
        except ValueError as exc:
            raise ValueError("RECONCILE_INTERVAL_SECONDS must be an integer") from exc
        if interval < 1:
            raise ValueError("RECONCILE_INTERVAL_SECONDS must be positive")
        return cls(*values, interval)


def managed_name(ip):
    return PREFIX + hashlib.sha256(ip.encode("ascii")).hexdigest()[:32]


def desired_addresses(services, slices):
    selected = {
        (svc.metadata.namespace, svc.metadata.name)
        for svc in services
        if svc.spec.load_balancer_class == CLASS
        and svc.spec.type == "LoadBalancer"
    }
    addresses = set()
    for endpoint_slice in slices:
        if endpoint_slice.address_type != "IPv4":
            continue
        key = (
            endpoint_slice.metadata.namespace,
            (endpoint_slice.metadata.labels or {}).get(
                "kubernetes.io/service-name"
            ),
        )
        if key not in selected:
            continue
        for endpoint in endpoint_slice.endpoints or []:
            conditions = endpoint.conditions
            ref = endpoint.target_ref
            if (
                ref is None
                or ref.kind != "Pod"
                or (ref.namespace is not None and ref.namespace != key[0])
                or (conditions is not None and conditions.ready is False)
                or (conditions is not None and conditions.serving is False)
                or (conditions is not None and conditions.terminating is True)
            ):
                continue
            for value in endpoint.addresses or []:
                try:
                    ip = ipaddress.ip_address(value)
                except ValueError as exc:
                    raise ValueError(f"Invalid EndpointSlice IP: {value}") from exc
                if ip.version != 4:
                    raise ValueError(f"Non-IPv4 address in IPv4 EndpointSlice: {value}")
                addresses.add(str(ip))
    return addresses


def plan_addresses(existing, desired, vnet_id):
    kept = []
    present = set()
    seen_names = set()
    for address in existing:
        name = address.name or ""
        ip = address.ip_address
        if name in seen_names:
            raise ValueError(f"Duplicate backend address name: {name}")
        seen_names.add(name)
        if ip in present:
            raise ValueError(f"Duplicate backend IP: {ip}")
        if ip:
            present.add(ip)
        if name.startswith(PREFIX):
            if not ip or name != managed_name(ip):
                raise ValueError(f"Reserved backend address name has unexpected IP: {name}")
            if (
                address.virtual_network
                and address.virtual_network.id
                and address.virtual_network.id.lower() != vnet_id.lower()
            ):
                raise ValueError(f"Managed backend address has unexpected VNet: {name}")
            if ip in desired:
                kept.append(address)
        else:
            kept.append(address)
    for ip in sorted(desired - present):
        kept.append(
            LoadBalancerBackendAddress(
                name=managed_name(ip),
                ip_address=ip,
                virtual_network=SubResource(id=vnet_id),
            )
        )
    return kept


def sync_pool(operations, settings, addresses):
    args = (settings.resource_group, settings.load_balancer, settings.pool)
    pool = operations.get(*args)
    if pool.backend_ip_configurations:
        raise ValueError("NIC-backed pool cannot be used for IP-based backends")
    if pool.sync_mode == "Automatic":
        raise ValueError("Automatically synchronized pool cannot be manually reconciled")
    if pool.virtual_network and pool.virtual_network.id:
        if pool.virtual_network.id.lower() != settings.vnet_id.lower():
            raise ValueError("Configured VNet does not match the backend pool VNet")
    existing = pool.load_balancer_backend_addresses or []
    planned = plan_addresses(existing, addresses, settings.vnet_id)
    if [(a.name, a.ip_address) for a in planned] == [
        (a.name, a.ip_address) for a in existing
    ]:
        return False
    pool.load_balancer_backend_addresses = planned
    operations.begin_create_or_update(*args, pool).result()
    LOG.info("Updated backend pool: %d ready IPs, %d total entries", len(addresses), len(planned))
    return True


def list_endpoint_slices(discovery):
    response = discovery.list_endpoint_slice_for_all_namespaces(_preload_content=False)
    payload = json.loads(response.data)
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise ValueError("Invalid EndpointSlice list response")
    slices = []
    for item in payload["items"]:
        metadata = item["metadata"]
        endpoints = []
        for endpoint in item.get("endpoints") or []:
            conditions = endpoint.get("conditions") or {}
            target = endpoint.get("targetRef") or {}
            endpoints.append(SimpleNamespace(
                addresses=endpoint["addresses"],
                conditions=SimpleNamespace(
                    ready=conditions.get("ready"),
                    serving=conditions.get("serving"),
                    terminating=conditions.get("terminating"),
                ),
                target_ref=SimpleNamespace(
                    kind=target.get("kind"), namespace=target.get("namespace")
                ) if target else None,
            ))
        slices.append(SimpleNamespace(
            address_type=item["addressType"],
            metadata=SimpleNamespace(
                namespace=metadata["namespace"], labels=metadata.get("labels")
            ),
            endpoints=endpoints,
        ))
    return slices


def reconcile(core, discovery, operations, settings):
    services = core.list_service_for_all_namespaces().items
    slices = list_endpoint_slices(discovery)
    addresses = desired_addresses(services, slices)
    return sync_pool(operations, settings, addresses)


def watch_resource(method, changed, stop, raw=False):
    while not stop.is_set():
        watcher = watch.Watch(return_type="object" if raw else None)
        try:
            for _ in watcher.stream(method, timeout_seconds=30):
                changed.set()
                if stop.is_set():
                    watcher.stop()
                    break
        except Exception:
            LOG.exception("Kubernetes watch failed; retrying")
            stop.wait(5)
        finally:
            watcher.stop()


def run(core, discovery, operations, settings, stop):
    changed = threading.Event()
    for method, raw in (
        (core.list_service_for_all_namespaces, False),
        (discovery.list_endpoint_slice_for_all_namespaces, True),
    ):
        threading.Thread(
            target=watch_resource, args=(method, changed, stop, raw), daemon=True
        ).start()
    deadline = 0.0
    while not stop.is_set():
        now = monotonic()
        if changed.is_set() or now >= deadline:
            changed.clear()
            try:
                reconcile(core, discovery, operations, settings)
            except Exception:
                LOG.exception("Reconciliation failed; will retry")
            deadline = monotonic() + settings.interval
        changed.wait(timeout=min(1, max(0, deadline - monotonic())))


def main():
    logging.basicConfig(level=logging.INFO)
    settings = Settings.from_env()
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    core = client.CoreV1Api()
    discovery = client.DiscoveryV1Api()
    network = NetworkManagementClient(DefaultAzureCredential(), settings.subscription)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    run(core, discovery, network.load_balancer_backend_address_pools, settings, stop)


if __name__ == "__main__":
    main()
