# UPA

UPA compiles a specification (rules with provenance) into an inferred minimal model, a Logical IR, a provider-neutral Interaction IR, Z3 verification results, and C#.

## Usage

```powershell
pip install z3-solver pytest
python -m upa build examples\eshoponweb\rules\eshop\orders\checkout\checkout.upa --scope examples\eshoponweb\informal-scope.md
python -m upa project-interactions examples\eshoponweb\rules\eshop\orders\checkout\canonical-ir.json
python -m upa generate examples\eshoponweb\rules\eshop\orders\checkout\canonical-ir.json --out examples\eshoponweb\generated
python -m pytest tests
```

The only shipped example is [examples/eshoponweb](examples/eshoponweb/README.md), a verified model of the MIT-licensed eShopOnWeb storefront. Other folders: `skills/` and `agents/` (Copilot skill and agent for UPA), `targets/` (C# generator and the neutral React DOM provider), `docs/`.

`build` exit codes: 0 VERIFIED, 1 compilation error, 2 REFUTED, 3 INCOMPLETE, and 4 unresolved decisions (`--strict-model`).

`build` also emits `interaction-ir.json` without requiring a provider or modifying the Logical IR. See [docs/interaction-ir.md](docs/interaction-ir.md) for its format and inference rules.

Specifications may include a `system_map` block for components, boundary contracts, and evidence-backed workflows. The map is preserved in `canonical-ir.json`; it is not a proof of behavior. See [docs/system-map.md](docs/system-map.md).

## Typed gaps and regeneration

The legacy form `language_gap ID "text"` remains supported. To make limitations actionable, declare a typed gap:

```upa
language_gap GAP_API kind regeneration "The external contract must be regenerated"
  target Publish impact high required true
language_gap GAP_TRACE "Execution evidence is unavailable"
  kind evidence scope "operation" impact medium needed true
```

Kinds are `language`, `evidence`, and `regeneration`; `impact` accepts `low`, `medium`, `high`, or `critical`. `target` must resolve to a formal element, and `scope` must identify a non-empty declared surface. `required` and `needed` are Boolean aliases. Metadata is optional and validated when provided; defaults are medium impact and required. Without `scope` or `target`, a regeneration gap is listed but does not declare an evaluated surface.

The IR and viewer report regeneration coverage for declared surfaces, never an invented percentage: `complete` (all declared gaps are optional), `partial` (at least one required gap remains), or `not_evaluated` (no regeneration surfaces were declared). The `VERIFIED` verdict is independent: it proves rules over the IR, not that the system can be regenerated.

## What UPA guarantees

1. **Formal verification over the IR** (Z3, under declared assumptions): PROVEN / REFUTED / UNKNOWN; UNKNOWN is never success.
2. **C# validation**: conformance against a Python reference interpreter (`targets\dotnet\refmodel.py`) over 200 traces, plus property monitoring; this is **not a proof**.
3. **Integration and operations**: outside the MVP (durability, concurrency, liveness, and performance).

## Limitations

- The eShopOnWeb example is VERIFIED under its declared assumptions; see its README for gaps.
- The oracle is only partially independent; VERIFIED does not prove business correctness.
- Inference from usage does not support `Option` or `zero_or_more` collections.
- The catalog (`upa\reuse.py`) is structural and has an empty corpus; the experiment (`experiment\`) is defined but has not been run.
- Non-ambiguous rule discovery is in `discovery\`.

