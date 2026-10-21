"""Business labels (`terms { ... }`) map reverse-engineered technical names to familiar domain language.
They do not change semantics; the viewer uses them to describe flows in natural language."""
from __future__ import annotations


def known_names(spec) -> set[str]:
    names: set[str] = set()
    for group in (spec.operations, spec.composes, spec.queries):
        for x in group:
            names.add(x.name)
            names.update(p.name for p in x.params)
    for e in spec.entities:
        names.add(e.name)
        names.update(f"{e.name}.{a.name}" for a in e.attrs)
        names.update(a.name for a in e.attrs)
    for en in spec.enums:
        names.add(en.name)
        names.update(f"{en.name}.{m}" for m in en.members)
    for x in spec.externals:
        names.add(x.name)
        names.update(f"{x.name}.{o.name}" for o in x.ops)
    names.update(c.alias for c in spec.components)
    names.update(x.id for x in spec.async_producers)
    names.update(x.id for x in spec.async_consumers)
    names.update(n.id for n in spec.notes)
    return names


def check_terms(spec, errors) -> None:
    if not spec.terms:
        return
    names = known_names(spec)
    for key, (label, _text, pos) in spec.terms.items():
        if key not in names:
            errors.add("TERM_UNKNOWN", f"terms: {key!r} is not an operation, parameter, query, entity, attribute, enum, or component in this model", pos)
        elif not label.strip():
            errors.add("TERM_EMPTY", f"terms: {key!r} has an empty business label", pos)
