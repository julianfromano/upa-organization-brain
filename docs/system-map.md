# System map

`system_map` records source-backed architecture alongside a UPA specification. It keeps deployed components and external contracts traceable without treating topology as a business guarantee.

```upa
system_map {
  component OrderApi {
    name "order-api"
    kind service
    scope included
    role "Primary order-processing service"
    runtime "Java"
    repository "example-org/order-api"
    evidence "architecture-review.md section 2"
  }

  component CarrierContract {
    name "Carrier tracking interface"
    kind external
    scope boundary
    role "Implementation is outside this specification"
    evidence "carrier-contract.md section 1"
  }

  contract TrackingLookup {
    owner CarrierContract
    service "TrackingService"
    protocol "HTTPS"
    operation "GetStatus"
    request "GetStatusRequest"
    response "GetStatusResponse"
    usage consumer_known
    source "contracts/tracking.yaml"
    sha256 "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    evidence "carrier-contract.md section 2"
    caller OrderApi
  }

  flow RefreshShipmentStatus {
    name "Refresh shipment status"
    status source_verified
    evidence "order-api source, section 3"
    step OrderApi "GET /v1/shipments/status"
    step CarrierContract "GetStatus" contract TrackingLookup
  }

  observation OBS_STATUS_RESPONSE {
    subject OrderApi
    statement "The integration returns HTTP 200 with the tracking result in a JSON response."
    status documented
    evidence "architecture-review.md section 4"
  }
}
```

The hash above is a placeholder; replace it with the real SHA-256 of the contract source.

## Rules

- `component` requires a unique identifier, name, kind, scope, role, and evidence. `runtime` and `repository` are optional unless established by the source.
- `scope` is `included` for a modeled component or `boundary` for an external interface. Boundary components may appear in flows and own contracts, but cannot be contract callers.
- `contract` records request/response names, source path, SHA-256, usage, and code-confirmed callers. The compiler checks hash format and references; it does not read the source or inline nested message schemas.
- `usage` is `consumer_known`, `no_known_consumer`, or `not_exercised`. The first requires an included caller; the others forbid callers.
- `flow` requires at least two steps and evidence. `source_verified` means a static call path was confirmed in source, `production_observed` means runtime evidence confirmed it, and `inferred` is not source-verified.
- A step's optional `contract` must be owned by that step's component.
- `observation` attaches a claim to a component, contract, or flow. Status is `source_verified`, `runtime_observed`, `documented`, or `inferred`; runtime observations must cite their environment. Observations describe evidence and are not solver-checked invariants.
- `system_map` is included in `canonical-ir.json` but does not change Logical IR rules, assumptions, solver results, or code generation. `VERIFIED` applies only to behavioral rules formalized in the specification. A map without formal behavioral rules builds as `INCOMPLETE`, not vacuously `VERIFIED`.

## Full inventories

Component kinds also include `web_app`, `database`, `scheduler`, `etl`, and `report`. Components may declare repeated `depends_on <ComponentId>` references; each dependency must exist and cannot point to itself.

Contract usage `traffic_observed` means runtime traffic was seen and requires at least one caller. A boundary component may be a caller; `consumer_known` still requires an included caller.

`catalog <Id> { owner <C> source "..." status <s> evidence "..." value NAME [code "..."] [meaning "..."]... }` records source-backed enumerations. At least one value is required; names must be unique. Do not invent meanings absent from the source.

`job <Id> { name "..." owner <C> kind etl_workflow|batch_process|scheduled_job|report status <s> evidence "..." { reads "..." } { writes "..." } }` records batch and ETL artifacts. Status is `source_verified`, `runtime_observed`, `documented`, or `inferred`.

## Links to the formal model

A component may repeat `realizes <Id>` to link it to an operation, query, composition, business or technical rule, fact, decision, assumption, question, or exclusion in the same specification. Unknown or repeated ids are rejected as `UNKNOWN_COMPONENT_REALIZES` and `DUPLICATE_COMPONENT_REALIZES`. Linking an open question honestly records that the component exists but its rules are not yet formalized. `realizes` is navigation metadata: it adds no proof obligations and appears in `canonical-ir.json` only when present.

The viewer uses these links to open the formal model from a component card, including the literal UPA text, source line, and solver result for each linked proof obligation.

A `rule <Id>` declaration records one atomic source-backed behavior, including subject, kind, status, condition, outcome, optional error, source location, and literal evidence. Copy values, messages, and codes exactly from the source; do not interpret codes the source does not explain.

## Standard viewer

Every `upa build` (Business Surface or Core) also writes `viewer.html` beside the artifacts, using the same viewer for any specification level. `--no-viewer` disables it. A viewer failure does not fail the build; the output JSON includes `viewer_error`. Use `upa view <directory...> --out <file>` to combine multiple specifications.
