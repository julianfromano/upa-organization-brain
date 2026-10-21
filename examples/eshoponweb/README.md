# UPA example: eShopOnWeb

This example models the main eShopOnWeb storefront behaviors as UPA components: access control, catalog administration and filtering, baskets, login-time basket merge, orders, and three end-to-end store journeys. It is an English-language formal model, not a copy of the application source.

The source project is [.NET Architecture eShopOnWeb](https://github.com/dotnet-architecture/eShopOnWeb), licensed under the MIT License. The source revision analyzed for this example is `4da8212117e87d808d4bbc7da6286fd2147ce606`. Source origins are cited as repository-relative `file:line` references in the `.upa` specifications and `system_map`. Literal C# origin quotes are retained verbatim. The source checkout is not included here.

## Build and view

Run these commands from the `upa-open` repository root in PowerShell. Each build writes verification artifacts (`canonical-ir.json`, `verification-result.json`, proof and evidence reports, and `viewer.html`) beside the corresponding specification. The checked-in viewer snapshots are generated with English labels and store artifact paths relative to the repository root. Build components bottom-up, then build the store macro last:

```powershell
$specs = @(
  'examples\eshoponweb\rules\eshop\access\authorization\authorization.upa',
  'examples\eshoponweb\rules\eshop\catalog\item\item.upa',
  'examples\eshoponweb\rules\eshop\catalog\create\create.upa',
  'examples\eshoponweb\rules\eshop\catalog\edit\edit.upa',
  'examples\eshoponweb\rules\eshop\catalog\delete\delete.upa',
  'examples\eshoponweb\rules\eshop\catalog\filter\filter.upa',
  'examples\eshoponweb\rules\eshop\catalog\administration\administration.upa',
  'examples\eshoponweb\rules\eshop\basket\buyer\buyer.upa',
  'examples\eshoponweb\rules\eshop\basket\lines\lines.upa',
  'examples\eshoponweb\rules\eshop\basket\add\add.upa',
  'examples\eshoponweb\rules\eshop\basket\update\update.upa',
  'examples\eshoponweb\rules\eshop\basket\merge\merge.upa',
  'examples\eshoponweb\rules\eshop\orders\order\order.upa',
  'examples\eshoponweb\rules\eshop\orders\checkout\checkout.upa',
  'examples\eshoponweb\rules\eshop\store\store.upa'
)
foreach ($spec in $specs) {
  python -m upa build $spec --scope examples\eshoponweb\informal-scope.md
  if ($LASTEXITCODE -ne 0) { throw "UPA build failed: $spec (exit $LASTEXITCODE)" }
}
```

Open any generated `viewer.html` in a browser, or build a combined view explicitly:

```powershell
python -m upa view examples\eshoponweb\rules\eshop\store --out examples\eshoponweb\store-view.html
```

Generate and validate the checkout component's C# target (requires the .NET SDK):

```powershell
python -m upa generate examples\eshoponweb\rules\eshop\orders\checkout\canonical-ir.json --out examples\eshoponweb\generated
dotnet build examples\eshoponweb\generated
```

The generator emits conformance traces with the C# project. Run conformance and attach the report to the checkout build artifacts like this:

```powershell
Push-Location examples\eshoponweb\generated
dotnet run --no-build --project Upa.Generated.csproj -- conformance-traces.json csharp-validation.json
Pop-Location
Copy-Item examples\eshoponweb\generated\csharp-validation.json examples\eshoponweb\rules\eshop\orders\checkout\csharp-validation.json -Force
python -m upa build examples\eshoponweb\rules\eshop\orders\checkout\checkout.upa --scope examples\eshoponweb\informal-scope.md
```

The C# result is conformance validation against an independent reference interpreter, not a proof. `upa build` incorporates the report into `guarantee-scope.json` when the file is present.

## Findings

- Adding an existing item increases its quantity but preserves the unit price captured when it first entered the basket.
- On sign-in, a valid anonymous basket cookie triggers a merge. Existing signed-in basket prices take precedence; otherwise the anonymous line's price is copied.
- Checkout records a snapshot of catalog identity, name, and image, and uses the basket line's price and quantity. Later catalog edits do not change that order line.
- Catalog creation does not apply the update path's domain guards. API editing updates details, brand, and type; the Admin-page path updates name and price while retaining other fields.
- The store macro contains `AddAndCheckout`, `MergeOnLogin`, and `ManageCatalog`, with business-facing `terms` and a descriptive system map.

## Limits

`VERIFIED` means the Z3 obligations hold for the formal model under its listed assumptions; it does not establish that the model is a complete or correct description of the running application. In particular, checkout's quantity update, order insert, and basket deletion are separate commits but the compose is atomic; the checkout shipping address is hard-coded in the source; concurrent checkout and partial-failure recovery are not modeled. The model abstracts multi-line basket and order loops to representative lines, does not prove total-unit conservation during basket merge, and does not exercise a live database or web application. Default image timestamp behavior, request validation, database constraints, and the UI's result ordering/pagination details also have declared gaps.
