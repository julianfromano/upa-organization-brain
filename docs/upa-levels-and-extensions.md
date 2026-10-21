# Tiers and extensions

## Inheritance levels

`uses a.b.c@<version>` includes a more general specification; a level mismatch produces `LEVEL_VIOLATION`. The specification resolves to `rules/a/b/c/c.upa`.

Levels describe specialization and are distinct from component tiers. A layered design might define shared policy at a general level, a domain rule at a more specific level, and a user-facing workflow at the most specific level. A child inherits declarations and re-proves applicable rules. A declared `level` must be more specific than the parent's level.

One namespace is shared by types, enums, operations, queries, compositions, and notes; duplicate names produce `DUPLICATE_NAME`. Reserved words cannot be used as names (`RESERVED_NAME`).

## Explicit scope and exclusions

An informal scope document records the source statements that a specification is intended to cover. Each statement must be accounted for by a formal element, an explicit exclusion, or an open question. An exclusion such as `exclusion EX_1 "Manual reconciliation is outside this model"` remains in the IR as `notes.exclusions`; it does not add behavior or a proof obligation.

Coverage is not a claim that every sentence is formalized. The verifier proves only the rules represented in the formal model under its stated assumptions. Use explicit gaps and exclusions to make the remaining limits visible.

## OpenAPI and external operations

OpenAPI describes an interface shape; UPA adds behavioral semantics. Imported endpoint descriptions do not by themselves establish state transitions or business guarantees. Model external results as declared outcomes and make uncertainty explicit. REST is one realization of an effect, not a business abstraction.

## Business Surface

Business Surface recognizes only the phrase patterns in its `PATTERNS` registry, including registration, modification, identity, required fields, mutability, and deletion. Unsupported prose must remain an explicit open question or move to Core; the compiler must not silently infer behavior.

## Outcome declarations

Core supports `when <condition> outcome <Name> { ... }`. This names possible operation results without changing the meaning of existing state rules. A result that has no effect is a rejection unless marked `success`.

## Interaction projection

The Interaction IR is an independent projection from Logical IR. It classifies intents, inputs, references, and system values using structural evidence. It never proves that a person initiates an operation: without an explicit actor signal, the actor remains unresolved. Provider resolution is downstream and must not strengthen the logical contract.

## Natural-language terminology

`terms` maps declared technical names to business labels and optional explanations. It does not change semantics. Prefer ordinary, stable domain language, for example:

```upa
terms {
  ReserveStock "Reserve inventory" "Hold the requested quantity for an order"
  Order.status "order status"
  OrderState.Ready "ready for shipment"
}
```

Inherited profiles may be compared with a specification, but their rules are not imported unless the model explicitly says so.
