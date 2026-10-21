"""Inheritance across levels: organization > domain > module > specific.

`uses <dotted.path>@<version>` imports parent declarations. Inherited rules are not copied as text;
they enter the child specification and are re-proved against its operations. A child cannot weaken a
parent rule: the verifier refutes any child operation that violates an inherited rule.
The path resolves to <root>/<a>/<b>/<c>/<c>.upa."""
from __future__ import annotations
from pathlib import Path

from .errors import ErrorBag, UpaError
from .parser import parse

LEVELS = ["layer", "organization", "domain", "module", "specific"]


def find_root(path: Path) -> Path | None:
    for p in [path.resolve(), *path.resolve().parents]:
        if p.name == "rules":
            return p
    return None


def _load(dotted: str, version: str, root: Path, errors: ErrorBag, seen: tuple):
    parts = dotted.split(".")
    f = root.joinpath(*parts, parts[-1] + ".upa")
    if dotted in seen:
        errors.add("USES_CYCLE", f"inheritance cycle: {' -> '.join(seen + (dotted,))}")
        return None
    if not f.exists():
        errors.add("USES_NOT_FOUND", f"'{dotted}@{version}': does not exist: {f}")
        return None
    parent = parse(f.read_text(encoding="utf-8"))
    if parent.version != version:
        errors.add("USES_VERSION", f"'{dotted}': requested @{version}, but the file declares @{parent.version}")
        return None
    apply_uses(parent, root, errors, seen + (dotted,))
    return parent


def apply_uses(spec, root: Path | None, errors: ErrorBag, seen: tuple = ()) -> None:
    try:
        _apply(spec, root, errors, seen)
    finally:
        _check_layers(spec, errors)


def _check_layers(spec, errors: ErrorBag) -> None:
    if spec.level == "layer":
        return
    for item in [*spec.operations, *spec.composes, *spec.queries]:
        l = item.layer
        if l and l != "shared" and l not in spec.layers:
            errors.add("LAYER_NOT_INHERITED",
                       f"'{item.name}' declares layer {l}, but the spec does not inherit the layer-level spec '{l}' (with 'uses')")


def _apply(spec, root, errors, seen) -> None:
    if not spec.uses:
        return
    if root is None:
        errors.add("USES_NO_ROOT", "the spec uses 'uses' but no 'rules/' directory was found for resolution")
        return
    for dotted, version in list(spec.uses):
        parent = _load(dotted, version, root, errors, seen)
        if parent is None:
            continue
        if spec.level and parent.level and LEVELS.index(parent.level) >= LEVELS.index(spec.level):
            errors.add("LEVEL_VIOLATION",
                       f"'{spec.name}' (level {spec.level}) may only use more general levels; "
                       f"'{parent.name}' is {parent.level}")
            continue
        _merge(spec, parent, dotted, errors)


def _merge(child, parent, dotted: str, errors: ErrorBag) -> None:
    def names(items, key):
        return {key(i) for i in items}

    def take(attr, key, label, tag=True):
        have = names(getattr(child, attr), key)
        for it in getattr(parent, attr):
            k = key(it)
            if k in have:
                if k in child.inherited_ids or any(k == key(x) for x in getattr(child, attr) if x is not it):
                    errors.add("INHERITANCE_COLLISION", f"{label} '{k}' already exists (inherited from {dotted}); rename it")
                continue
            if tag and hasattr(it, "origin_block") and it.origin_block is None:
                it.origin_block = dotted
            getattr(child, attr).append(it)
            have.add(k)

    take("aliases", lambda x: x.name, "type", False)
    take("enums", lambda x: x.name, "enum")
    take("externals", lambda x: x.name, "external", False)
    take("operations", lambda x: x.name, "operation")
    take("composes", lambda x: x.name, "compose", False)
    take("queries", lambda x: x.name, "query", False)
    take("props", lambda x: x.id, "property")
    take("facts", lambda x: x.id, "fact", False)
    take("notes", lambda x: x.id, "note", False)
    child.layers |= parent.layers | ({parent.name.lower()} if parent.level == "layer" else set())
    child.inherited_ids |= {p.id for p in parent.props} | {f.id for f in parent.facts} | parent.inherited_ids
    if parent.initial is not None:
        if child.initial is None:
            child.initial, child.initial_origin = parent.initial, parent.initial_origin
            child.inherited_ids.add("initial")
        else:
            child.initial = {"k": "bin", "op": "and", "l": parent.initial, "r": child.initial, "pos": child.initial.get("pos")}
