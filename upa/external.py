"""External capabilities: assume-guarantee contracts and the `invoke` effect primitive.

An `external` declaration lists possible operation outcomes and their effects on the model.
`invoke Ext.Op(args) yields r` expands into one case per outcome. The environment selects the result
(parameter `r`, universally quantified by the verifier), so every property is checked for every outcome
allowed by the contract. The real service remains under assumption AX_EXT_<Ext>_<Op>.
"""
from __future__ import annotations
import copy

from .ast import Case, EnumDecl, Param, Spec


def _subst(x, env):
    if isinstance(x, dict):
        if x.get("k") == "var" and x["n"] in env:
            return copy.deepcopy(env[x["n"]])
        return {k: _subst(v, env) for k, v in x.items()}
    if isinstance(x, list):
        return [_subst(v, env) for v in x]
    if isinstance(x, tuple):
        return tuple(_subst(v, env) for v in x)
    return x


def _stmt_subst(st, env):
    n = copy.copy(st)
    n.key = _subst(st.key, env) if st.key is not None else None
    n.value = _subst(st.value, env) if st.value is not None else None
    n.inits = [(a, _subst(e, env)) for a, e in st.inits]
    n.cond = _subst(st.cond, env) if st.cond is not None else None
    return n


def expand_externals(spec: Spec, errors) -> None:
    ext = {e.name: e for e in spec.externals}
    for e in spec.externals:
        names = {o.name for o in e.ops}
        for o in e.ops:
            if not o.outcomes:
                errors.add("EXT_NO_OUTCOMES", f"{e.name}.{o.name}: declare at least one 'outcome'", o.pos)
            onames = {n for n, _, _ in o.outcomes}
            for k in o.effects:
                if k not in onames:
                    errors.add("EXT_UNKNOWN_OUTCOME", f"{e.name}.{o.name}: 'on {k}' is not a declared outcome", o.pos)
            if o.resolve_by and o.resolve_by not in names:
                errors.add("EXT_BAD_RESOLVER", f"{e.name}.{o.name}: resolve_by {o.resolve_by!r} is not an operation of {e.name}", o.pos)
            if any(u for _, _, u in o.outcomes) and not o.resolve_by:
                errors.add("UNRESOLVABLE_EFFECT",
                           f"{e.name}.{o.name} has uncertain outcomes ({[n for n, _, u in o.outcomes if u]}) "
                           f"and the contract does not specify how to resolve them (resolve_by): an uncertain external effect "
                           f"must be reconcilable", o.pos)
            spec.enums.append(EnumDecl(f"{e.name}_{o.name}_Outcome", [n for n, _, _ in o.outcomes], e.pos))

    for op in spec.operations:
        new_cases: list[Case] = []
        for case in op.cases:
            inv = [s for s in case.stmts if s.kind == "invoke"]
            if not inv:
                new_cases.append(case)
                continue
            if len(inv) > 1:
                errors.add("EXT_ONE_INVOKE", f"{op.name}: a case allows only one 'invoke'", inv[1].pos)
                new_cases.append(case)
                continue
            st = inv[0]
            e = ext.get(st.entity)
            eo = next((o for o in e.ops if o.name == st.attr), None) if e else None
            if eo is None:
                errors.add("EXT_UNKNOWN", f"invoke {st.entity}.{st.attr}: that external operation does not exist", st.pos)
                new_cases.append(case)
                continue
            if len(st.args) != len(eo.params):
                errors.add("EXT_ARITY", f"invoke {e.name}.{eo.name}: expected {len(eo.params)} arguments", st.pos)
                new_cases.append(case)
                continue
            enum = f"{e.name}_{eo.name}_Outcome"
            have = next((p for p in op.params if p.name == st.yields), None)
            if have is None:
                op.params.append(Param(st.yields, enum))
            elif have.type != enum:
                errors.add("EXT_YIELD_TYPE", f"{op.name}: parameter {st.yields!r} must have type {enum}", st.pos)
            env = {p.name: a for p, a in zip(eo.params, st.args)}
            rest = [s for s in case.stmts if s.kind != "invoke"]
            for o, _, _ in eo.outcomes:
                is_o = {"k": "bin", "op": "==", "l": {"k": "var", "n": st.yields, "pos": st.pos},
                        "r": {"k": "enum", "e": enum, "v": o, "pos": st.pos}, "pos": st.pos}
                cond = is_o if case.cond is None else {"k": "bin", "op": "and", "l": case.cond, "r": is_o, "pos": st.pos}
                eff = [_stmt_subst(s, env) for s in eo.effects.get(o, [])]
                new_cases.append(Case(cond, rest + eff, case.pos, case.outcome, case.success))
        op.cases = new_cases
