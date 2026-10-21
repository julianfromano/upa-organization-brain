"""Composition across UPAs: `component Alias = a.b.c@version`.

Unlike `uses` (inheritance, where a child absorbs its parent's declarations and re-proves its rules), a component
is an independent UPA that compiles and verifies on its own and exposes pure queries for reuse. The parent calls
them as `Alias.Query(args)`; the call is replaced with the query body and substituted arguments, so parent rules
are proved against the component's exact definition rather than a hand-written copy.

Composition depth is zero for an atomic UPA and otherwise one plus the maximum depth of its components. Calls
without an alias (`Query(args)`) resolve to local queries."""
from __future__ import annotations
import copy
import dataclasses
from pathlib import Path

from .ast import Step
from .errors import ErrorBag

PRIMITIVE = {"Bool", "Int", "Natural", "Text", "Identifier"}
IMPURE = {"attr", "in", "sum", "old", "count", "forall", "exists", "unchanged", "result_is", "enum"}
MAX_INLINE_DEPTH = 32


def component_path(root: Path, dotted: str) -> Path:
    parts = dotted.split(".")
    return root.joinpath(*parts, parts[-1] + ".upa")


def _nodes(e):
    if isinstance(e, dict):
        if "k" in e:
            yield e
        for v in e.values():
            yield from _nodes(v)
    elif isinstance(e, list):
        for v in e:
            yield from _nodes(v)


def _impurity(body) -> str | None:
    for n in _nodes(body):
        if n.get("k") in IMPURE:
            return n["k"]
        if n.get("k") == "call" and n.get("alias") is None:
            return "call"
    return None


def _base_type(t: str, aliases: dict) -> str:
    seen = set()
    while t in aliases and t not in seen:
        seen.add(t)
        t = aliases[t]
    return t


def exportable(query, aliases: dict) -> str | None:
    """Return None if another UPA can call the query; otherwise return the reason."""
    for p in query.params:
        if _base_type(p.type, aliases) not in PRIMITIVE:
            return f"parameter {p.name}: {p.type} is not a primitive type"
    if _base_type(query.ret, aliases) not in PRIMITIVE:
        return f"return type {query.ret} is not primitive"
    why = _impurity(query.body)
    if why:
        return f"body is not pure (uses {why})"
    return None


def _retarget(e, pos):
    for n in _nodes(e):
        n["pos"] = pos
    return e


def _subst(body, params, args):
    names = {p.name: a for p, a in zip(params, args)}

    def go(e):
        if isinstance(e, dict):
            if e.get("k") == "var" and e["n"] in names:
                return copy.deepcopy(names[e["n"]])
            return {k: go(v) for k, v in e.items()}
        if isinstance(e, list):
            return [go(v) for v in e]
        return e
    return go(copy.deepcopy(body))


class _Inliner:
    def __init__(self, spec, exports, errors: ErrorBag):
        self.spec = spec
        self.exports = exports
        self.errors = errors
        self.local = {q.name: q for q in spec.queries}

    def expr(self, e, depth=0):
        if isinstance(e, list):
            return [self.expr(v, depth) for v in e]
        if not isinstance(e, dict):
            return e
        if e.get("k") != "call":
            return {k: self.expr(v, depth) for k, v in e.items()}
        pos = e.get("pos")
        args = [self.expr(a, depth) for a in e["args"]]
        alias, name = e["alias"], e["q"]
        label = f"{alias}.{name}" if alias else name
        if depth > MAX_INLINE_DEPTH:
            self.errors.add("QUERY_CALL_CYCLE", f"{label}: recursive or excessively deep query calls", pos)
            return {"k": "bool", "v": False, "pos": pos}
        if alias is None:
            q = self.local.get(name)
            if q is None:
                self.errors.add("UNKNOWN_QUERY", f"'{name}(...)' is not a declared query", pos)
                return {"k": "bool", "v": False, "pos": pos}
        else:
            if alias not in self.exports:
                self.errors.add("UNKNOWN_COMPONENT", f"'{alias}' is not a declared component (component {alias} = ...)", pos)
                return {"k": "bool", "v": False, "pos": pos}
            entry = self.exports[alias].get(name)
            if entry is None:
                self.errors.add("COMPONENT_QUERY_UNKNOWN", f"component {alias} does not declare query {name}", pos)
                return {"k": "bool", "v": False, "pos": pos}
            q, why = entry
            if why:
                self.errors.add("COMPONENT_QUERY_NOT_EXPORTABLE", f"{label} cannot be called from another UPA: {why}", pos)
                return {"k": "bool", "v": False, "pos": pos}
        if len(args) != len(q.params):
            self.errors.add("QUERY_CALL_ARITY",
                            f"{label} expects {len(q.params)} arguments but received {len(args)}", pos)
            return {"k": "bool", "v": False, "pos": pos}
        self.spec.component_calls.append({"alias": alias, "query": name, "pos": list(pos) if pos else None})
        out = self.expr(_retarget(_subst(q.body, q.params, args), pos), depth + 1)
        if isinstance(out, dict):
            # provenance used to explain a case trace in the viewer: which query produced this value and with which arguments
            out = dict(out)
            out["via"] = [{"q": label, "params": [p.name for p in q.params], "args": args}] + list(out.get("via", []))
        return out

    def obj(self, o):
        if isinstance(o, dict):
            return self.expr(o)
        if isinstance(o, Step):
            return Step(o[0], self.obj(o[1]), self.expr(o.guard) if o.guard is not None else None, o.trail,
                        self.expr(o.loop) if o.loop is not None else None)
        if isinstance(o, list):
            return [self.obj(v) for v in o]
        if dataclasses.is_dataclass(o) and not isinstance(o, type):
            for f in dataclasses.fields(o):
                setattr(o, f.name, self.obj(getattr(o, f.name)))
            return o
        if isinstance(o, tuple):
            return tuple(self.obj(v) for v in o)
        return o


TIERS = ["atom", "subprocess", "process", "macroprocess"]


def effective_tier(spec) -> str:
    """Declared analysis tier, or one derived from composition depth when omitted."""
    if spec.tier:
        return spec.tier
    return TIERS[min(getattr(spec, "component_depth", 0), len(TIERS) - 1)]


def _check_tier(spec, child, ref, errors: ErrorBag) -> None:
    if not spec.tier:
        return
    # Equal tiers are allowed (a process made of processes), so depth is not bounded by the number of tiers.
    if TIERS.index(effective_tier(child)) > TIERS.index(spec.tier):
        errors.add("TIER_ORDER",
                   f"{ref.alias} = {ref.ref}: a component cannot have a higher tier than its parent "
                   f"({effective_tier(child)} within {spec.tier})", ref.pos)


def _entities_in(x) -> set:
    out = set()
    for n in _nodes(x):
        for key in ("e", "entity"):
            if isinstance(n.get(key), str) and n.get(key):
                out.add(n[key])
    return out


def _op_entities(op) -> set:
    out = set()
    for c in op.cases:
        out |= _entities_in(c.cond) if c.cond is not None else set()
        for s in c.stmts:
            out.add(s.entity)
            for e in (s.key, s.value, s.cond):
                if e is not None:
                    out |= _entities_in(e)
            for _, v in s.inits:
                out |= _entities_in(v)
    return out


def _import_operations(spec, children: dict, errors: ErrorBag) -> None:
    """`compose X = Alias.Op(...); ...`: selectively imports a component operation and the entities, facts, types,
    and invariants it uses under the name Alias_Op. Imported rules are proven again in the parent, so composition is
    checked against the exact component definition."""
    imported: dict[str, str] = {}
    for comp in spec.composes:
        new_calls = []
        for step in comp.calls:
            name, args = step
            guard = getattr(step, "guard", None)
            loop = getattr(step, "loop", None)
            if "." not in name:
                new_calls.append(step)
                continue
            alias, op_name = name.split(".", 1)
            child = children.get(alias)
            if child is None:
                errors.add("UNKNOWN_COMPONENT", f"'{alias}' is not a declared component (component {alias} = ...)", comp.pos)
                new_calls.append(step)
                continue
            op = next((o for o in child.operations if o.name == op_name), None)
            sub_comp = next((x for x in child.composes if x.name == op_name), None)
            if op is None and sub_comp is None:
                errors.add("COMPONENT_OPERATION_UNKNOWN",
                           f"component {alias} does not declare operation or composition {op_name}", comp.pos)
                new_calls.append(step)
                continue
            local = f"{alias}_{op_name}"
            if sub_comp is not None:
                if sub_comp.atomic and not comp.atomic:
                    errors.add("COMPOSE_ATOMICITY_LOST",
                               f"{alias}.{op_name} is atomic: {comp.name} must be declared with 'compose atomic' to preserve the guarantee",
                               comp.pos)
                new_calls.extend(_import_steps(spec, child, alias, sub_comp, args, imported, errors, comp.pos, guard, loop))
            else:
                if local not in imported:
                    imported[local] = alias
                    _import_one(spec, child, alias, op, local, errors, comp.pos)
                new_calls.append(Step(local, args, guard, (name,), loop))
        comp.calls = new_calls


def _import_steps(spec, child, alias, sub_comp, args, imported: dict, errors: ErrorBag, pos, guard=None, loop=None) -> list:
    """A component composition is flattened into its steps with call arguments substituted.
    The parent step condition is conjoined with each child condition; both are pure and read no state."""
    out = []
    for st in sub_comp.calls:
        step, step_args = st
        step_local = f"{alias}_{step}"
        op = next((o for o in child.operations if o.name == step), None)
        if op is None:
            errors.add("COMPONENT_OPERATION_UNKNOWN", f"{alias}.{sub_comp.name}: step {step} is not an operation", pos)
            continue
        if step_local not in imported:
            imported[step_local] = alias
            _import_one(spec, child, alias, op, step_local, errors, pos)
        g = getattr(st, "guard", None)
        g = _subst(g, sub_comp.params, args) if g is not None else None
        if guard is not None:
            g = guard if g is None else {"k": "bin", "op": "and", "l": guard, "r": g, "pos": pos}
        lp = getattr(st, "loop", None)
        lp = _subst(lp, sub_comp.params, args) if lp is not None else None
        if loop is not None:
            lp = loop if lp is None else {"k": "bin", "op": "and", "l": loop, "r": lp, "pos": pos}
        trail = (f"{alias}.{sub_comp.name}",) + tuple(f"{alias}.{x}" for x in getattr(st, "trail", ()))
        out.append(Step(step_local, [_subst(a, sub_comp.params, args) for a in step_args], g, trail, lp))
    return out


def _import_one(spec, child, alias, op, local, errors: ErrorBag, pos) -> None:
    ref = next(c.ref for c in spec.components if c.alias == alias)
    if any(o.name == local for o in spec.operations) or any(q.name == local for q in spec.queries):
        errors.add("COMPONENT_IMPORT_COLLISION", f"{local} already exists in the parent", pos)
        return
    touched = _op_entities(op)
    # The operation ensure clause already expands `unchanged` over the child entities.
    for p in child.props:
        if p.kind in ("ensures", "law") and p.targets == [op.name] and p.expr is not None:
            touched |= _entities_in(p.expr)
    own_e = {e.name: e for e in spec.entities}
    for e in child.entities:
        if e.name not in touched:
            continue
        have = own_e.get(e.name)
        if have is None:
            ne = copy.deepcopy(e)
            ne.origin_block = f"{alias}={ref}"
            spec.entities.append(ne)
            own_e[e.name] = ne
        elif {(a.name, a.type) for a in have.attrs} != {(a.name, a.type) for a in e.attrs}:
            errors.add("COMPONENT_ENTITY_CONFLICT",
                       f"entity {e.name} differs between the parent and component {alias}", pos)
    for t in child.aliases:
        if not any(x.name == t.name for x in spec.aliases):
            spec.aliases.append(copy.deepcopy(t))
    for en in child.enums:
        if not any(x.name == en.name for x in spec.enums):
            ne = copy.deepcopy(en)
            ne.origin_block = f"{alias}={ref}"
            spec.enums.append(ne)
    have_facts = {f.id for f in spec.facts}
    for f in child.facts:
        if getattr(f, "entity", None) in touched and f.id not in have_facts:
            nf = copy.deepcopy(f)
            nf.origin_block = f"{alias}={ref}"
            spec.facts.append(nf)
    new_op = copy.deepcopy(op)
    new_op.name = local
    new_op.origin_block = f"{alias}={ref}"
    spec.operations.append(new_op)
    have_props = {p.id for p in spec.props}
    for p in child.props:
        ents = _entities_in(p.expr) if p.expr is not None else set()
        if p.kind == "invariant":
            keep = bool(ents) and ents <= touched
        elif p.kind == "law" and not p.targets:
            keep = bool(ents) and ents <= touched
        elif p.kind in ("ensures", "law"):
            keep = bool(p.targets) and p.targets == [op.name]
        else:
            keep = False
        if not keep:
            continue
        np_ = copy.deepcopy(p)
        np_.id = f"{alias}_{p.id}"
        np_.targets = [local] if p.targets else []
        np_.origin_block = f"{alias}={ref}"
        if np_.id not in have_props:
            spec.props.append(np_)
            have_props.add(np_.id)


def _conjuncts(e) -> list:
    if isinstance(e, dict) and e.get("k") == "bin" and e.get("op") == "and":
        return _conjuncts(e["l"]) + _conjuncts(e["r"])
    return [e]


def _import_initial(spec, children: dict) -> None:
    """Imported invariants hold from the component initial state: the parent combines the parts of each child
    `initial` clause that mention only entities imported from that child."""
    for alias, child in children.items():
        if child.initial is None:
            continue
        mine = {e.name for e in spec.entities if (e.origin_block or "").startswith(alias + "=")}
        for part in _conjuncts(child.initial):
            ents = _entities_in(part)
            if not ents or not ents <= mine:
                continue
            part = copy.deepcopy(part)
            if spec.initial is None:
                spec.initial = part
                spec.initial_from_components = True
            else:
                spec.initial = {"k": "bin", "op": "and", "l": spec.initial, "r": part}


def _ids_of(s) -> set:
    return ({o.name for o in s.operations} | {q.name for q in s.queries} | {x.name for x in s.composes}
            | {p.id for p in s.props} | {x.id for x in s.facts} | {n.id for n in s.notes}
            | {x["alias"] for x in s.component_info})


def resolves(s, path: str) -> bool:
    """`realizes A.B.Element`: follow component aliases to a declared element."""
    head, _, rest = path.partition(".")
    if not rest:
        return head in _ids_of(s)
    kid = getattr(s, "_kids", {}).get(head)
    return kid is not None and resolves(kid, rest)


def resolves_async_endpoint(s, path: str) -> bool:
    """Resolve operations and compositions only, including those in nested components."""
    head, _, rest = path.partition(".")
    if not rest:
        return head in ({x.name for x in s.operations} | {x.name for x in s.composes})
    kid = getattr(s, "_kids", {}).get(head)
    return kid is not None and resolves_async_endpoint(kid, rest)


def expand_components(spec, root: Path | None, errors: ErrorBag, stack: tuple = ()) -> None:
    from .compiler import compile_source
    from .ir import ir_hash

    exports: dict[str, dict] = {}
    children: dict = {}
    depth = 0
    for c in spec.components:
        if root is None:
            errors.add("COMPONENT_NO_ROOT", "the spec declares components but the 'rules/' directory was not found", c.pos)
            continue
        if c.ref in stack:
            errors.add("COMPONENT_CYCLE", f"composition cycle: {' -> '.join(stack + (c.ref,))}", c.pos)
            continue
        f = component_path(root, c.ref)
        if not f.exists():
            errors.add("COMPONENT_NOT_FOUND", f"'{c.ref}@{c.version}': {f} does not exist", c.pos)
            continue
        scope_file = f.parent / "informal-scope.md"
        scope = scope_file.read_text(encoding="utf-8") if scope_file.exists() else None
        try:
            child, child_ir, _, child_errors = compile_source(f.read_text(encoding="utf-8"), scope, root,
                                                              stack + (c.ref,))
        except Exception as ex:  # Error while parsing the child component.
            errors.add("COMPONENT_INVALID", f"{c.alias} = {c.ref}: {ex}", c.pos)
            continue
        if child.version != c.version:
            errors.add("COMPONENT_VERSION", f"'{c.ref}': requested @{c.version}, but the file declares @{child.version}", c.pos)
            continue
        if child_errors:
            first = "; ".join(f"{x['error']}: {x['message']}" for x in child_errors.to_json()[:3])
            errors.add("COMPONENT_INVALID", f"{c.alias} = {c.ref}@{c.version} does not compile: {first}", c.pos)
            continue
        _check_tier(spec, child, c, errors)
        children[c.alias] = child
        aliases = {a.name: a.base for a in child.aliases}
        table = {q.name: (q, exportable(q, aliases)) for q in child.queries}
        exports[c.alias] = table
        child_ids = ({o.name for o in child.operations} | {q.name for q in child.queries}
                     | {x.name for x in child.composes} | {p.id for p in child.props}
                     | {x.id for x in child.facts} | {n.id for n in child.notes}
                     | {x["alias"] for x in child.component_info})
        for sc in spec.system_components:
            for target in sc.realizes:
                if target.startswith(c.alias + ".") and not resolves(child, target.split(".", 1)[1]):
                    errors.add("UNKNOWN_COMPONENT_REALIZES",
                               f"{sc.id}: realizes {target!r}: {c.ref} does not declare {target.split('.', 1)[1]}", sc.pos)
        child_depth = getattr(child, "component_depth", 0)
        depth = max(depth, child_depth + 1)
        spec.component_info.append({
            "alias": c.alias, "ref": c.ref, "version": c.version, "spec": child.name,
            "path": "/".join(c.ref.split(".")) + "/" + c.ref.split(".")[-1] + ".upa",
            "ir_hash": ir_hash(child_ir), "depth": child_depth,
            "exports": [{"name": q.name, "params": [[p.name, p.type] for p in q.params], "ret": q.ret}
                        for q, why in table.values() if why is None],
            "components": [{"alias": x["alias"], "ref": x["ref"], "version": x["version"]}
                           for x in child.component_info],
        })
    spec.component_depth = depth
    spec._kids = children
    for kind, endpoints in (("publishes", spec.async_producers), ("subscribes", spec.async_consumers)):
        for endpoint in endpoints:
            alias, dot, rest = endpoint.operation.partition(".")
            if dot and alias in children and not resolves_async_endpoint(children[alias], rest):
                errors.add("UNKNOWN_ASYNC_ENDPOINT",
                           f"{endpoint.id} {kind} {endpoint.operation}: the component does not declare that "
                           "operation or composition",
                           endpoint.pos)
    if spec.tier == "atom" and spec.components:
        errors.add("TIER_ORDER", "a UPA with tier 'atom' cannot compose other UPAs", spec.components[0].pos)
    _import_operations(spec, children, errors)
    _import_initial(spec, children)
    inl = _Inliner(spec, exports, errors)
    for attr in ("operations", "composes", "queries", "props", "facts", "externals"):
        setattr(spec, attr, inl.obj(getattr(spec, attr)))
    spec.initial = inl.expr(spec.initial)
