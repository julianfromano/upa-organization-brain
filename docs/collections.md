# Collection multiplicity

UPA represents repeated occurrences of one homogeneous concept as a collection with multiplicity rather than as numbered attributes.

```upa
fact F_ITEMS origin literal("Each order contains one to three items"):
  every Order has [1..3] items: Text
```

Supported multiplicities are `[n]`, `[m..n]`, `[m..*]`, `exactly_one`, `zero_or_one`, `zero_or_more` (`[0..*]`), and `one_or_more` (`[1..*]`). Operations can use `List<T>` parameters and `count(x)`.

| Layer | Responsibility | Artifact |
|---|---|---|
| Logic (`upa/multiplicity.py`) | Encodes a collection as a serialized `Text` attribute plus a `Natural` count, creates the `<ID>_BOUNDS` invariant, and verifies it with Z3. | `canonical-ir.json` (`collections`) |
| Persistence (`upa/persistence.py`) | Describes a child table with entity key, position, value, and minimum/maximum bounds. | `persistence-ir.json` (when collections exist) |
| Interaction (`upa/interaction.py`) | Projects a `COLLECTION_INPUT` parameter with `min`/`max`; the count is marked `DERIVED`. | `interaction-ir.json` |
| UI (`upa/frontend.py`) | A provider may render a `collection-input` role as an editable list that enforces the bounds. | `renderable-ui.json` |

Occurrences with distinct semantic roles, such as billing and shipping addresses, are separate concepts rather than one collection. Business Surface does not yet recognize natural-language multiplicity phrases; use Core declarations for now.
