# Spatial contracts

`spatial/contracts.py` retains the domain primitives used for frames and related
spatial operations. The shared observation envelope moved to
[`relay.observations`](../relay/observations.py).

Read [the shared observation contract](../docs/observation-contract.md) for the
v1 envelope and payloads, [relay ingress](../docs/observation-ingress.md) for
host-owned source bindings, and
[`schemas/observation-v1.schema.json`](../schemas/observation-v1.schema.json)
for the portable schema artifact.

Canonical ingress records authenticated diagnostic evidence. A world-map consumer
has an additional host gate: it needs a registration for a canonically bound
source and a measured association with the current approved map. Admission alone
never establishes that association or authorizes motion.

`SWEEP_WORLD_OBSERVATION_SOURCES` remains a compatibility setting for map-consumer
registrations. Its optional legacy `sources` object is accepted only for existing
configuration files; the canonical bindings, frames, and clock mappings come from
`SWEEP_OBSERVATIONS_FILE`.
