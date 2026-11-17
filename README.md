# Azure external LB controller

Synchronizes ready Pod IPv4 addresses from EndpointSlices of all Kubernetes Services with `spec.loadBalancerClass: azure-external-lb` into one explicitly configured Azure Standard Load Balancer IP-based backend pool. The CNI **must** assign the Pod an address backed by a VM/VMSS secondary IP in the LB's VNet; network reachability, NSGs, health probe, load-balancing rules and backend ports are deployment prerequisites. Azure LB backend pools contain **IP addresses, not ports**: configure rules and probes on the LB separately. No Azure resource or Kubernetes Service status is created by this controller.

The controller owns only backend addresses named `k8spod-<32 hexadecimal characters>` in its dedicated pool. It preserves addresses with other names, but refuses to operate on a NIC-backed pool, duplicate IPs or names in the reserved namespace that do not match their expected IP. Give this controller an exclusive IP pool (no other writers); Azure pool updates are whole-resource writes. Removing the class, deleting a Service or losing readiness removes its managed IP on the next reconciliation. Multiple selected Services contribute a union of ready IPs.

## Configuration

Set `AZURE_SUBSCRIPTION_ID`, `AZURE_RESOURCE_GROUP`, `AZURE_LOAD_BALANCER`, `AZURE_BACKEND_POOL`, and `AZURE_VNET_RESOURCE_ID` (full Azure resource ID). The pool must already exist and reference the same VNet. Authenticate using `DefaultAzureCredential` (prefer workload identity with `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, and projected federated token). Grant the identity `Microsoft.Network/loadBalancers/backendAddressPools/read` and `write` on the pool; network operator is a broader alternative. Never store credentials in this repository. `RECONCILE_INTERVAL_SECONDS` defaults to 60. Run exactly one replica; deployment uses `Recreate` to avoid concurrent writers.

Install with `pip install .` then run `azure-external-lb-controller`; for local development `pip install -e '.[test]'` and `python -m unittest discover -s tests -v`. Deploy the example in `deploy/` after substituting its **non-secret** Azure identifiers and Kubernetes workload-identity annotations/labels. This repo deliberately carries no environment-specific values.

Kubernetes Service example (existing Service's spec):

```yaml
type: LoadBalancer
loadBalancerClass: azure-external-lb
```

`loadBalancerClass` is immutable once set; the Service must not also be managed by another controller. The controller reads Service and EndpointSlice resources cluster-wide; RBAC is read-only. It consumes only IPv4 EndpointSlices referencing Pod targets and endpoints with `ready: true` (or absent), `serving` not false, and `terminating` not true. The controller periodically re-lists both resources and immediately reconciles after watch notifications. Failed Kubernetes lists and Azure writes are logged and retried; an incomplete list never clears the pool.

Azure Standard LB's IP-based backend pools require VM/VMSS-backed IPs in the same VNet. The Pod-secondary-IP integration is an explicit infrastructure assumption, not validated or provisioned here. Validate in a non-production LB before rollout. Backend removal is immediate on readiness loss; configure the pool's drain period and probes externally if connection draining is needed.
