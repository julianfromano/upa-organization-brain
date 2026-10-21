# Components: composing UPA specifications

A UPA specification can reuse another verified specification as a component. Define a rule once in an atomic UPA, verify it independently, and compose it into higher-level workflows. Navigation tools expose both the composition tree and every place a component is reused.

```upa
spec OrderFulfillment version "1.0" tier process
component Inventory = domains.inventory.stock@1.0
component Shipping = domains.shipping.dispatch@2.0
```

## Component contract

| Concern | Contract |
|---|---|
| Resolution | `domains.inventory.stock` resolves to `rules/domains/inventory/stock/stock.upa`. If present, the component's scope document is also loaded. |
| Version | The requested version must match the component's declared version (`COMPONENT_VERSION`). |
| Independence | Components compile and verify separately. A failing component causes `COMPONENT_INVALID` in its parent. |
| Exported queries | Only pure queries are exportable: primitive (or aliased primitive) parameters and results, with no state reads, enums, or stateful operators such as `attr`, `in`, `old`, `sum`, `count`, `forall`, or `exists`. |
| Query calls | Use `Alias.Query(args)` in a parent expression. The query body is inlined so the parent proves its own rules against the component's exact definition. Local calls may use `Query(args)`. |
| Verdict | A parent's verdict is the least favorable of its own verdict and its components' verdicts (`VERIFIED < INCOMPLETE < REFUTED`). `summary_total` aggregates proven obligations across the tree. |
| Depth | An atomic specification has depth zero; otherwise depth is one plus the maximum component depth. The value is recorded in `ir.composition.depth`. |
| Source mapping | `system_map` may use `realizes Alias`, `realizes Alias.Element`, or a longer component path to connect a deployed component to a formal element. Invalid paths produce `UNKNOWN_COMPONENT_REALIZES`. |
| Imported origins | Imported rules, facts, and initial conditions retain their component origin. The parent does not ask for evidence already validated against the child. If the parent has no `initial` declaration, compatible child initial conditions are imported. |
| Design decisions | A decision resolved in a child must be resolved again in the parent with its own `decision ... resolves ...` declaration. `--strict-model` exits with code 4 when a decision remains open. |
| Build | `upa build` builds each component in its own directory and records its verdict in the parent report. |

Call errors include `UNKNOWN_QUERY`, `UNKNOWN_COMPONENT`, `COMPONENT_QUERY_UNKNOWN`, `COMPONENT_QUERY_NOT_EXPORTABLE`, `QUERY_CALL_ARITY`, and `QUERY_CALL_CYCLE`. Resolution errors include `COMPONENT_NOT_FOUND`, `COMPONENT_NO_ROOT`, `COMPONENT_CYCLE`, and `DUPLICATE_COMPONENT_ALIAS`.

## Inheritance and composition

- `uses X@v` declares **inheritance**. It merges declarations and re-proves the parent's rules in the child.
- `component A = x.y@v` declares **composition**. The child remains encapsulated, is verified once, and can be reused by multiple specifications.

A `compose` can call a component operation or composition as `Alias.Operation(args)`. The compiler selectively imports the called operation under an alias-qualified name along with the entities, facts, types, and invariants it needs. The parent re-proves the imported obligations; component compositions are flattened into their steps.

A regular composition continues after a rejected step. If a later rule assumes that rejection stopped the workflow, that rule can be refuted. A successful no-effect outcome is explicit:

```upa
when condition outcome Accepted success { }
```

Without `success`, a no-effect outcome means rejection. An unconditional rejection in an atomic composition can make the workflow a no-op; proof obligations alone may not expose that modeling mistake, so inspect the execution narrative as well.

`compose atomic X(...) = a; b` is all-or-nothing: if a guarded step rejects, the composition has no effect. The verifier, reference model, and generated C# share this behavior. A parent calling an atomic child composition must also be atomic; otherwise compilation reports `COMPOSE_ATOMICITY_LOST`. `ensures R for X` also accepts compositions. Each composition's `outcomes` is the ordered union of its steps' outcomes.

## Tiers and execution metadata

A specification may declare `tier atom`, `subprocess`, `process`, or `macroprocess`. Tiers describe analysis scope and are independent of inheritance `level`. If no tier is declared, UPA derives one from composition depth. A component may not have a higher tier than its parent (`TIER_ORDER`), and an `atom` cannot compose another component. Equal-tier nesting is allowed; composition depth is not limited to four.

Composition steps are verified in source order. Execution metadata does not establish guarantees about parallel ordering; encode required ordering in guards or sequential steps.

- `;` starts the next sequential stage; `&` places a step in the same stage as the previous one.
- `async` marks work completed later by another process.
- `parallel` marks concurrent fan-out.
- `loop while condition do Step(...)` repeats while a pure Boolean condition holds. Verification checks a representative iteration and its invariants without inventing a repetition limit. Atomic compositions cannot contain asynchronous steps or loops (`COMPOSE_ASYNC_IN_ATOMIC`, `COMPOSE_LOOP_IN_ATOMIC`).
- `invokes Op kind "name" sync|async [via p1, p2] [evidence "file#method"]` records an operation's interaction with an external entity. Kinds include `database`, `api`, `service`, `queue`, `file`, `directory`, and `mail`; `via` names parameters carrying its result.

Asynchronous boundaries have independent declarations. Producers use `publishes`; consumers use `subscribes`. A producer does not need to know its consumer's subscription, delivery, or acknowledgment behavior, and a consumer does not require a producer declaration. Each side may have zero or more declarations. Keep transport, channel, message, confirmation, subscription, durability, delivery, completion, and failure metadata explicit; a producer/consumer match is inferred only when the declared transport, channel, and message agree exactly.

## Inspection, tracing, and replay

The compiler flattens parent guards into component steps. Each flattened step retains its guard and component trail; in an atomic composition, a rejection matters only when its guard is true. Inlined query calls retain their original name and arguments in the IR, enabling regeneration and value tracing.

The viewer can simulate a case against the IR, showing the component path, evaluated guards and queries, chosen operation case, state changes, and applicable proof rules. State persists between runs until reset. The simulation is an inspection aid; Z3 verification remains the formal proof.

The `terms` block maps technical names to business labels and optional explanations. It is metadata and does not alter verified `operations` or `properties`. Keys must refer to names declared in the same specification; unknown keys, duplicate keys, and empty labels produce `TERM_UNKNOWN`, `TERM_DUPLICATE`, and `TERM_EMPTY`. The viewer searches for a term in the component that owns a step, the rest of the component tree, and finally the root specification.

`store` maps an entity to a real table without emulating a database:

```upa
store Order in "orders..order_record" key "order_id" { status = "status" } evidence "OrderStore.Insert"
```

`upa owners <rules>` summarizes declared table readers and writers across compiled specifications. Shared writers are flagged for coordination or explicit boundary modeling. `upa replay <ir|directory> --cases cases.jsonl [--out directory]` compares aggregated, non-personal examples with the reference interpreter. It explores all combinations of free Boolean inputs and reports `REPRODUCES`, `PARTIALLY_REPRODUCES`, or `CONTRADICTS` according to whether all, some, or none explain the expected state.

## Verification and navigation

Component builds verify children before parents. Imported obligations retain their provenance; a parent reuses a child's proven obligation only when the relevant state ownership conditions hold. Modular discharge, relevance retry, and duplicate-rule handling are recorded in the verification report. `UPA_NO_MODULAR` and `UPA_NO_RELEVANCE` disable the corresponding optimizations for comparison.

The `mod(expr, N)` primitive uses Euclidean remainder for a positive integer literal `N`, yielding a value from zero through `N - 1`.

- `upa interface <file>` shows a compact contract of operations, outcomes, compositions, pure queries, and stateful rules.
- `upa tree <file> [--rules]` displays the component hierarchy and optionally its rules.
- `upa units <rules>` and `upa units <rules> --used-by <ref>` index specifications and reuse relationships.
- `upa view <file> --out <file.html>` displays component flows, tiers, guards, and proven rules; each reused component appears once in the component-flow index.
