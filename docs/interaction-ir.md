# Interaction IR

UPA emits a provider-neutral interaction projection from a successfully compiled Logical IR. The projection is a separate artifact: it does not mutate the Logical IR, its obligations, or its verification result, and it does not require a renderer, profile, or UI library.

```text
UPA source → Logical IR → Interaction Projection → Interaction IR
                                           └──────→ provider resolution (downstream)
```

`python -m upa build <spec.upa> --out <directory>` writes `interaction-ir.json` beside `canonical-ir.json`. To re-project an existing Logical IR without selecting a provider:

```powershell
python -m upa project-interactions <directory>\canonical-ir.json
```

The command accepts `--out <file>` to select another output path. Identical Logical IR produces byte-stable, sorted JSON. The artifact records the Logical IR format and SHA-256 hash it came from. Projection is available after semantic compilation; it does not imply that formal verification returned `VERIFIED`.

## Contract

The top-level `format` is `upa-interaction-ir-1`. Each operation carries:

- `intent`: `CREATE`, `UPDATE`, `EXECUTE`, or `UNRESOLVED`, with a subject when one can be identified without guessing.
- `actor`: `UNRESOLVED` unless the Logical IR explicitly identifies the initiator.
- `parameters`: every operation parameter and its classification.
- `inputs`: required `USER_INPUT` values and `ENTITY_REFERENCE` values.
- `system_values`: `DERIVED`, `GENERATED`, and `TECHNICAL` values, with expressions and source paths.
- `preconditions` and `outcomes`: original expressions and outcome names from the Logical IR. Outcomes are classified but business names are never invented.
- `effects`, `capabilities`, `ambiguities`, and stable paths back to the Logical IR.

`UNRESOLVED` is an intentional result, not a default input classification. A parameter is not considered person-entered merely because it appears in an operation signature. Values are classified only when their use in a state change or structural reference provides evidence.

## Deterministic inference rules

| Classification | Evidence |
|---|---|
| `USER_INPUT` | A required operation parameter is used by a state-changing statement and is not an entity reference. |
| `ENTITY_REFERENCE` | A parameter is the key in an explicit membership condition, or the target key of an update. The target entity identity comes from a `create` key in the same Logical IR. Missing identity evidence remains an ambiguity. |
| `DERIVED` | A created attribute reads a state attribute directly, or an explicit `to_text(parameter)` expression creates a text representation for a file value. |
| `GENERATED` | A created attribute has a constant initializer or a state-based computed value, such as a customer's next order number. |
| `TECHNICAL` | A value materializes a logical file payload; its expression remains traceable but is not treated as a separate user input. |
| `UNRESOLVED` | No rule above has sufficient structural evidence. |

Create-key membership checks indicate duplicate/conflict conditions rather than references to the entity being created. Other membership checks can establish references, including checks for a missing entity.

The projection directly supports `command-execution`, `data-input`, `validation-feedback`, and `human-interaction`. The first three are tied to effects, parameters, and named outcomes. If the IR does not say who initiates an operation, `human-interaction` is emitted as `UNRESOLVED`; parameters alone are not evidence of a human actor. A provider must not silently turn that unresolved capability into a person-facing experience.

## Order-processing example

A deterministic regression fixture for `RegisterOrder` identifies `Order` as the created subject, `customerId` as a reference to `Customer.id`, and `productCode` as a reference to `Product.code`. The order's product description is derived from `Product.description[productCode]`; its number is generated from the customer's order counter. A text representation of `quantity` is derived only when the Logical IR contains an explicit `to_text(quantity)` expression. Technical file content remains separate from business inputs.

The source note about choosing a product from a list is preserved as a source comment only. The logical invariant is grounded in the product-reference decision and membership rule, not in a presentation widget.

A provider-specific web profile remains downstream and must be checked against this contract before use. Mismatches are not resolved by changing the Logical IR or treating profile assumptions as business evidence; record unresolved reconciliation explicitly.
