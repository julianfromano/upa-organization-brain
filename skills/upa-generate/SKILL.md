---
name: upa-generate
description: Generate, verify, and visualize a UPA specification (a component language verified with Z3) from a request, document, or existing code. Use only when the user explicitly asks for UPA (for example, “UPA”, “write a UPA spec”, or “model this in UPA”).
---

# UPA specification workflow

Use this skill only for UPA modeling. Keep the specification focused on system semantics; do not add screens, controls, layout, navigation, or framework details to UPA. Do not include secrets or personal data. Use neutral domain examples such as orders, accounts, inventory, or tickets.

## Repository and references

Work from the repository root. Before modeling, inspect the relevant grammar, architecture documentation, and nearby examples that exist in this checkout. Use repository-relative paths in notes and generated references. Do not assume a command, DSL feature, or file exists without checking it.

## Workflow

1. **Set scope.** Identify the domain and model level (atomic rule, subprocess, process, or macro-process). Use the output directory requested by the user; otherwise keep the spec and build output together in a neutral examples directory.
2. **Gather evidence.** Read supplied source code and documents before writing. Cite the relevant repository-relative file and line where available. For a free-text request, distinguish user-provided facts from assumptions.
3. **Model bottom-up.** Use reusable atomic components and compose them at higher levels. Preserve the real order of validation and outcomes. Mark literal, derived, and assumed values with the DSL's supported origin syntax. Put unsupported requirements in an explicit gap instead of inventing semantics.
4. **Build and verify.** Run `python -m upa build <file.upa> --out <directory> --timeout-ms 90000`. Treat `VERIFIED`, `REFUTED`, `INCOMPLETE`, and `UNKNOWN` distinctly; a timeout or `UNKNOWN` is not success. Use `python -m upa build -h` for current options.
5. **Challenge the model.** Compare each rule, validation order, result, and composition with the evidence. Correct mismatches and rebuild. For stateful systems, examine all source/destination pairs and distinguish transitions by mechanism; label unverified inferences.
6. **Generate companion artifacts when requested.** Use the actual CLI commands:
   - `python -m upa frontend <canonical-ir.json> --profile <profile.json> --out <directory>` for a React DOM profile.
   - `python -m upa project-interactions <canonical-ir.json> --out <interaction-ir.json>` for the provider-independent interaction projection.
   - `python -m upa view <directory> --out <viewer.html>` for the HTML viewer.
   - `python -m upa interface <file.upa>`, `tree <file.upa> [--rules]`, and `units <rules-directory>` for inspection.
7. **Deliver concisely.** Report the component tree, verification verdicts, viewer path if generated, evidence boundaries, assumptions, and unresolved gaps. A verified model proves consistency with the specification, not fidelity to a real system unless checked against that system's implementation.

## State-transition checks

For models with state, record each state-changing operation or external mechanism and its source and destination. Decide every matrix cell, including no-op transitions. Represent external actors as inputs rather than presenting them as operations offered by the modeled system. Trace each transition to its source; mark symmetry or union-derived rules as assumptions. Separately list contract details that the spec does not represent, such as error shape, identifier assignment, catalog validation, or behavior for unknown identifiers.

## Commands

The CLI is `python -m upa`; consult `python -m upa <command> -h` if flags are uncertain. Available commands include `build`, `frontend`, `project-interactions`, `generate`, `view`, `interface`, `tree`, `units`, `owners`, `replay`, `why`, `guide`, `catalog`, `explain`, `diff`, and `import-openapi`. Do not invent commands or rely on machine-specific absolute paths. Do not commit, push, publish, or deploy unless the user explicitly asks.
