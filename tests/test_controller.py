import json
import unittest
from types import SimpleNamespace as Obj
from unittest.mock import Mock

from azure.mgmt.network.models import (
    BackendAddressPool,
    LoadBalancerBackendAddress,
    SubResource,
)
from azure.mgmt.network import NetworkManagementClient

from external_lb.controller import (
    Settings,
    desired_addresses,
    managed_name,
    list_endpoint_slices,
    plan_addresses,
    reconcile,
    sync_pool,
)


VNET = "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Network/virtualNetworks/vnet"
SETTINGS = Settings("sub", "rg", "lb", "pool", VNET, 60)


def service(name="web", namespace="default", lb_class="azure-external-lb"):
    return Obj(
        metadata=Obj(name=name, namespace=namespace),
        spec=Obj(type="LoadBalancer", load_balancer_class=lb_class),
    )


def endpoint(ip, ready=True, terminating=False, serving=True, kind="Pod"):
    return Obj(
        addresses=[ip],
        target_ref=Obj(kind=kind, namespace="default"),
        conditions=Obj(ready=ready, terminating=terminating, serving=serving),
    )


def endpoint_slice(name="web", endpoints=None, address_type="IPv4"):
    return Obj(
        metadata=Obj(namespace="default", labels={"kubernetes.io/service-name": name}),
        address_type=address_type,
        endpoints=endpoints or [],
    )


def address(name, ip):
    return LoadBalancerBackendAddress(
        name=name,
        ip_address=ip, virtual_network=SubResource(id=VNET),
    )


def pool(existing=None, nic=None):
    result = BackendAddressPool(
        virtual_network=SubResource(id=VNET),
        load_balancer_backend_addresses=existing or [],
    )
    result.backend_ip_configurations = nic
    return result


class DesiredAddressesTest(unittest.TestCase):
    def test_selects_ready_pod_ips_only(self):
        slices = [
            endpoint_slice(endpoints=[
                endpoint("10.0.0.1"),
                endpoint("10.0.0.2", ready=False),
                endpoint("10.0.0.3", terminating=True),
                endpoint("10.0.0.4", serving=False),
                endpoint("10.0.0.5", kind="Node"),
            ]),
            endpoint_slice(name="other", endpoints=[endpoint("10.0.0.6")]),
            endpoint_slice(endpoints=[endpoint("10.0.0.7")], address_type="IPv6"),
        ]
        self.assertEqual(desired_addresses([service()], slices), {"10.0.0.1"})
        self.assertEqual(desired_addresses([service(lb_class=None)], slices), set())

    def test_multiple_services_union_and_absent_conditions(self):
        slice_ = endpoint_slice(endpoints=[endpoint("10.0.0.1")])
        slice_.endpoints[0].conditions = None
        self.assertEqual(
            desired_addresses([service(), service("other")], [
                slice_, endpoint_slice(name="other", endpoints=[endpoint("10.0.0.2")])
            ]),
            {"10.0.0.1", "10.0.0.2"},
        )

    def test_invalid_ip_rejected(self):
        with self.assertRaises(ValueError):
            desired_addresses([service()], [endpoint_slice(endpoints=[endpoint("invalid")])])


class ReconcileTest(unittest.TestCase):
    def test_preserves_unmanaged_adds_and_removes_owned(self):
        existing = [address("manual", "10.0.0.8"), address(managed_name("10.0.0.1"), "10.0.0.1")]
        planned = plan_addresses(existing, {"10.0.0.2"}, VNET)
        self.assertEqual(
            [(item.name, item.ip_address) for item in planned],
            [("manual", "10.0.0.8"), (managed_name("10.0.0.2"), "10.0.0.2")],
        )

    def test_unmanaged_matching_ip_not_duplicated(self):
        self.assertEqual(
            plan_addresses([address("manual", "10.0.0.8")], {"10.0.0.8"}, VNET)[0].name,
            "manual",
        )

    def test_reserved_name_and_duplicate_ip_fail_closed(self):
        with self.assertRaises(ValueError):
            plan_addresses([address(managed_name("10.0.0.1"), "10.0.0.2")], set(), VNET)
        with self.assertRaises(ValueError):
            plan_addresses(
                [address("manual", "10.0.0.1"), address("other", "10.0.0.1")],
                set(), VNET,
            )

    def test_noop_and_update(self):
        ops = Mock()
        ops.get.return_value = pool([address("manual", "10.0.0.8")])
        self.assertFalse(sync_pool(ops, SETTINGS, set()))
        ops.begin_create_or_update.assert_not_called()
        self.assertTrue(sync_pool(ops, SETTINGS, {"10.0.0.1"}))
        ops.begin_create_or_update.assert_called_once()
        self.assertEqual(
            ops.begin_create_or_update.call_args.args[3].load_balancer_backend_addresses[1].name,
            managed_name("10.0.0.1"),
        )

    def test_azure_sdk_serializes_ip_backend_address(self):
        sdk = NetworkManagementClient(Mock(), "00000000-0000-0000-0000-000000000000")
        planned = pool(plan_addresses([], {"10.0.0.1"}, VNET))
        body = sdk._serialize.body(planned, "BackendAddressPool")
        entry = body["properties"]["loadBalancerBackendAddresses"][0]
        self.assertEqual(entry["properties"]["ipAddress"], "10.0.0.1")
        self.assertEqual(entry["properties"]["virtualNetwork"]["id"], VNET)

    def test_reject_nic_and_vnet_mismatch(self):
        ops = Mock()
        ops.get.return_value = pool(nic=[Obj(id="nic")])
        with self.assertRaises(ValueError):
            sync_pool(ops, SETTINGS, {"10.0.0.1"})
        ops.get.return_value = pool()
        ops.get.return_value.virtual_network.id = "wrong"
        with self.assertRaises(ValueError):
            sync_pool(ops, SETTINGS, {"10.0.0.1"})
        ops.begin_create_or_update.assert_not_called()

    def test_list_failure_never_mutates_pool(self):
        core = Mock()
        core.list_service_for_all_namespaces.side_effect = RuntimeError("API unavailable")
        ops = Mock()
        with self.assertRaises(RuntimeError):
            reconcile(core, Mock(), ops, SETTINGS)
        ops.get.assert_not_called()

    def test_null_endpoints_from_existing_slice_do_not_break_full_list(self):
        core, discovery, ops = Mock(), Mock(), Mock()
        core.list_service_for_all_namespaces.return_value.items = [service()]
        discovery.list_endpoint_slice_for_all_namespaces.return_value.data = json.dumps({
            "items": [
                {"addressType": "IPv4", "metadata": {
                    "namespace": "other", "labels": {"kubernetes.io/service-name": "arc"}
                }, "endpoints": None},
                {"addressType": "IPv4", "metadata": {
                    "namespace": "default", "labels": {"kubernetes.io/service-name": "web"}
                }, "endpoints": [{"addresses": ["10.0.0.2"], "targetRef": {"kind": "Pod"},
                                 "conditions": {"ready": True}}]},
            ]
        }).encode()
        self.assertEqual(
            desired_addresses(core.list_service_for_all_namespaces.return_value.items,
                              list_endpoint_slices(discovery)),
            {"10.0.0.2"},
        )
        ops.get.return_value = pool()
        self.assertTrue(reconcile(core, discovery, ops, SETTINGS))
        discovery.list_endpoint_slice_for_all_namespaces.assert_called_with(
            _preload_content=False
        )

    def test_class_removal_clears_managed_address(self):
        core, discovery, ops = Mock(), Mock(), Mock()
        core.list_service_for_all_namespaces.return_value.items = [service(lb_class=None)]
        discovery.list_endpoint_slice_for_all_namespaces.return_value.data = json.dumps({
            "items": [{"addressType": "IPv4", "metadata": {
                "namespace": "default", "labels": {"kubernetes.io/service-name": "web"}
            }, "endpoints": [{"addresses": ["10.0.0.1"], "targetRef": {"kind": "Pod"},
                             "conditions": {"ready": True}}]}]
        }).encode()
        ops.get.return_value = pool([address(managed_name("10.0.0.1"), "10.0.0.1")])
        self.assertTrue(reconcile(core, discovery, ops, SETTINGS))
        self.assertEqual(
            ops.begin_create_or_update.call_args.args[3].load_balancer_backend_addresses,
            [],
        )


if __name__ == "__main__":
    unittest.main()
