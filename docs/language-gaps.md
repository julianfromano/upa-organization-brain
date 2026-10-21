# Typed language gaps

`language_gap` records a language limitation without turning it into a guarantee. The legacy syntax remains supported:

```upa
language_gap GAP_1 "The language cannot express this behavior"
```

This is equivalent to a required `language` gap with `medium` impact and no declared surface. It does not contribute to regeneration coverage.

The typed form requires an id, kind, and description. `scope`/`target`, `impact`, and `required`/`needed` are optional validated metadata:

```upa
language_gap GAP_2 kind regeneration "The adapter is not generated yet"
  target SaveOrder impact high required true
```

- `kind`: `language`, `evidence`, or `regeneration`.
- `scope`: non-empty name of an evaluated surface.
- `target`: operation, composition, query, rule, fact, note, or component reference.
- `impact`: `low`, `medium`, `high`, or `critical`.
- `required` and `needed`: Boolean aliases. The defaults are `medium` impact and required.

Coverage is categorical, not percentage-based. It is `not_evaluated` when no regeneration surfaces are declared, `partial` when any required surface remains, and `complete` when every declared surface is marked not required. The Z3 result is separate: `VERIFIED` does not mean the specification is regenerable.
