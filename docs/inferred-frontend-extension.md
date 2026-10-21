# Inferred frontend extension for UPA

## Purpose

This document describes the optional frontend projection layered on top of UPA's logical model. It explains the existing contracts and the neutral React DOM provider in [`targets/react-web/`](../targets/react-web/). UPA remains focused on system semantics; a profile supplies the presentation bindings required by a frontend.

The pipeline is:

```text
UPA source -> Logical IR -> frontend inference -> Interaction Model + UI IR
           -> React DOM provider -> renderable UI + browser bundle
```

The extension does not change the source specification or Logical IR. A later provider can use the same abstract roles without changing UPA.

## CLI

Build a specification and emit its Logical IR:

```powershell
python -m upa build path/to/spec.upa --out path/to/build --timeout-ms 90000
```

Generate frontend artifacts with the profile selected for that application:

```powershell
python -m upa frontend path/to/build/canonical-ir.json --profile path/to/profile.json --out path/to/frontend-artifacts
```

The command writes `interaction-model.json` and `ui-ir.json` only when a frontend is required. The independent interaction projection is available through `python -m upa project-interactions <canonical-ir.json> --out <interaction-ir.json>`.

Build the browser bundle using the provider's own component catalog and source:

```powershell
npm --prefix targets/react-web install
npm --prefix targets/react-web run build -- --ui-ir path/to/frontend-artifacts/ui-ir.json --out path/to/wwwroot
```

The provider uses React and React DOM 18.3.1 with esbuild 0.25.12. It has no dependency on a proprietary component toolkit. `npm install` is needed only for provider build verification or development; the Python frontend command does not access the network.

## Separation of concerns

- **UPA and Logical IR:** describe domain rules, state, operations, inputs, and outcomes. They do not name pages, controls, layout, navigation, or browser technologies.
- **Interaction projection:** reports abstract interaction capabilities and their evidence. A parameter list alone is not evidence that a person initiates an operation.
- **Profile:** binds operations and logical parameters to page IDs, labels, input roles, collections, and explicit resolutions for parameters that are not entered by a person.
- **UI IR:** captures inferred page and interaction structure, capabilities, and provenance without depending on React.
- **Provider:** resolves abstract roles to generic React DOM components and creates a constrained browser projection. It does not define business rules.

## Profile and inference contracts

Profiles use the `upa-ui-ir-1` format. A page has a unique positive integer `id` and a non-empty title. An operation binding names an operation present in the Logical IR, its page, an action component ID, field bindings, and feedback provenance. Every logical parameter must be bound to a field or explicitly resolved. Collection fields must refer to attributes of their declared entity, except the special `$key` attribute.

Input roles include `textual-input`, `numeric-input`, `boolean-input`, `selection`, and `collection-input`. The profile's explicit role takes precedence where supported; type-based inference supplies a default. The generated interaction model retains operation, parameter, and inference traces. Profile hashes make inference inputs auditable and output ordering is deterministic.

A parameter is not silently omitted: use `resolved_parameters` with a supported resolution kind and a reason, such as `server_generated`. Bindings must not be used to hide a mismatch between the profile and the logical contract. Collection and outcome APIs belong to the application; the provider does not fabricate business behavior.

## React DOM component catalog

[`targets/react-web/catalog.json`](../targets/react-web/catalog.json) uses the `component-catalog-1` format and identifies provider `react-web` on `React DOM`. Its roles map to locally implemented, generic components (`Button`, `Card`, `TextField`, `Checkbox`, `Select`, `Alert`, `Header`, and `Toast`) in `targets/react-web/src/components.jsx`. The build checks that each role has a catalog contract and a matching local export before bundling.

Styles are ordinary provider-owned CSS in `targets/react-web/src/app.css`; the provider has no organization-specific tokens, package, or component dependency. The generated projection contains only the profile summary, roles, pages, interaction field metadata, collection labels, and a digest of the UI IR.

The example runtime submits interaction values to the application's generic `/api/operations/{operationId}` endpoint. Applications must implement that contract and provide any option data for selection controls. Provider build success checks artifact generation and component bindings; it does not prove that an application API exists or that domain behavior is correct.

## Diagnostics and limitations

Invalid profiles, missing logical parameters, unresolved component roles, and missing exports fail explicitly. A logical model with no configured interaction is valid: the frontend generator emits no UI artifacts. The provider does not invent validation rules, outcomes, options, or data sources.

The current provider is intentionally small. It offers a basic responsive layout, page navigation, form controls, and feedback. It is not a general design system and does not include advanced accessibility review, routing, data loading, or domain-specific API integration. Extend the catalog and local component implementation together when adding a role; keep the profile and logical model independent of React.
