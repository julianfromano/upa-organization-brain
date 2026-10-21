"""Homogeneous collections represented by multiplicity.

A rule describing multiple occurrences of one concept is modeled as one relation with multiplicity,
not as numbered attributes. The verifier reasons about scalar state, so the compiler lowers each
collection into two scalar attributes and leaves persistence, interaction, and UI decisions to
independent projections that read `collections` from the IR:

    Customer.phones: Text [m..n]
        -> Customer.phones        : Text     serialized elements, separated and ordered
        -> Customer.phones_count  : Natural  cardinality
        -> invariant <fact>_BOUNDS         m <= phones_count <= n

`List<T>` parameters become (`p: Text`, `p_count: Natural`), and `count(x)` becomes the cardinality
of `x`. A parameter inherits multiplicity from the attribute it is assigned to."""
from __future__ import annotations
import copy

from .ast import Fact, Param, Prop, Spec, Step, Stmt
from .parser import Parser

SEPARATOR = ";"
COUNT_SUFFIX = "_count"


def range_text(lo: int, hi: int | None) -> str:
    return f"[{lo}]" if lo == hi else f"[{lo}..{'*' if hi is None else hi}]"


def _is_list(type_name: str) -> bool:
    return isinstance(type_name, str) and type_name.startswith("List<") and type_name.endswith(">")


def _element(type_name: str) -> str:
    return type_name[5:-1]


def _walk_exprs(x):
    if isinstance(x, dict):
        yield x
        for v in x.values():
            yield from _walk_exprs(v)
    elif isinstance(x, (list, tuple)):
        for v in x:
            yield from _walk_exprs(v)


class _Expander:
    def __init__(self, spec: Spec, errors):
        self.spec = spec
        self.errors = errors
        self.colls: dict[tuple[str, str], dict] = {}

    # ---------------------------------------------------------------- facts
    def facts(self) -> None:
        s = self.spec
        derived: list = []
        for f in list(s.facts):
            if f.kind != "has" or not f.multi:
                continue
            key = (f.entity, f.attr)
            if key in self.colls:
                self.errors.add("COLLECTION_DUPLICATE", f"{f.entity}.{f.attr} is declared as a collection more than once", f.pos)
                continue
            if f.element.startswith(("Option<", "List<")):
                self.errors.add("COLLECTION_ELEMENT_UNSUPPORTED",
                                f"{f.entity}.{f.attr}: a collection element cannot be {f.element}", f.pos)
                continue
            count_attr = f.attr + COUNT_SUFFIX
            self.colls[key] = {
                "entity": f.entity, "attribute": f.attr, "element": f.element, "min": f.min, "max": f.max,
                "count_attribute": count_attr, "fact": f.id,
                "origin": {"kind": f.origin.kind, "ref": f.origin.ref} if f.origin else None,
                "storage": {"serialized_attribute": f.attr, "count_attribute": count_attr, "separator": SEPARATOR},
            }
            f.target = "Text"          # serialized form in the verifiable core
            count = Fact(f"{f.id}_COUNT", "has", f.entity, count_attr, f.origin, "exactly_one", "Natural", f.pos, 1, 1)
            derived.append(count)
            bound = self.bounds(f)
            if bound is not None:
                s.props.append(bound)
            if f.id in s.inherited_ids:
                s.inherited_ids |= {count.id, f"{f.id}_BOUNDS"}
        s.facts.extend(derived)
        s.collections = sorted(self.colls.values(), key=lambda c: (c["entity"], c["attribute"]))

    def bounds(self, f: Fact):
        c = f"{f.entity}.{f.attr}{COUNT_SUFFIX}[_k]"
        parts = []
        if f.min == f.max:
            parts.append(f"{c} == {f.min}")
        else:
            if f.min > 0:
                parts.append(f"{c} >= {f.min}")
            if f.max is not None:
                parts.append(f"{c} <= {f.max}")
        if not parts:
            return None
        expr = Parser(f"forall _k: (_k in {f.entity}) => ({' and '.join(parts)})").expr()
        return Prop("invariant", f"{f.id}_BOUNDS", "business",
                    f"{f.entity} has {range_text(f.min, f.max)} {f.attr}", f.origin, expr, [], f.pos)

    # ---------------------------------------------------------------- reescritura
    def count_of(self, v, lists: dict, pos):
        """Cardinality of a collection: a List<T> parameter or a read from a collection attribute."""
        if isinstance(v, dict) and v.get("k") == "var" and v["n"] in lists:
            return {"k": "var", "n": v["n"] + COUNT_SUFFIX, "pos": v.get("pos", pos)}
        if isinstance(v, dict) and v.get("k") == "attr" and (v["e"], v["a"]) in self.colls:
            return {**copy.deepcopy(v), "a": v["a"] + COUNT_SUFFIX}
        return None

    def rewrite(self, x, lists: dict):
        if isinstance(x, dict):
            if x.get("k") == "count":
                inner = self.rewrite(x["x"], lists)
                out = self.count_of(inner, lists, x.get("pos"))
                if out is None:
                    self.errors.add("COUNT_NOT_COLLECTION",
                                    "count() applies only to a List<T> parameter or an attribute declared with multiplicity",
                                    x.get("pos"))
                    return {"k": "int", "v": 0, "pos": x.get("pos")}
                return out
            return {k: self.rewrite(v, lists) for k, v in x.items()}
        if isinstance(x, list):
            return [self.rewrite(v, lists) for v in x]
        if isinstance(x, tuple):
            return tuple(self.rewrite(v, lists) for v in x)
        return x

    def stmts(self, stmts: list, lists: dict, bound: dict) -> list:
        out = []
        for st in stmts:
            st.key = self.rewrite(st.key, lists) if st.key is not None else None
            st.value = self.rewrite(st.value, lists) if st.value is not None else None
            st.cond = self.rewrite(st.cond, lists) if st.cond is not None else None
            st.inits = [(a, self.rewrite(e, lists)) for a, e in st.inits]
            st.args = [self.rewrite(e, lists) for e in st.args]
            out.append(st)
            if st.kind == "set" and (st.entity, st.attr) in self.colls:
                extra = self.assign(st, st.entity, st.attr, st.value, lists, bound)
                if extra is not None:
                    out.append(Stmt("set", st.entity, copy.deepcopy(st.key), st.attr + COUNT_SUFFIX, extra, [], st.pos))
            elif st.kind == "create" and not st.file:
                add = []
                for a, e in st.inits:
                    if (st.entity, a) in self.colls:
                        extra = self.assign(st, st.entity, a, e, lists, bound)
                        if extra is not None:
                            add.append((a + COUNT_SUFFIX, extra))
                st.inits = st.inits + add
        return out

    def assign(self, st, entity, attr, value, lists, bound):
        c = self.colls[(entity, attr)]
        count = self.count_of(value, lists, st.pos)
        if count is None:
            self.errors.add("COLLECTION_ASSIGN",
                            f"{entity}.{attr} is a collection: assign it from a List<{c['element']}> parameter "
                            f"or another collection, not a scalar value", st.pos)
            return None
        if value.get("k") == "var":
            p = lists[value["n"]]
            if p["element"] != c["element"]:
                self.errors.add("COLLECTION_ELEMENT_MISMATCH",
                                f"{value['n']}: List<{p['element']}> does not match {entity}.{attr} ({c['element']})", st.pos)
            bound.setdefault(value["n"], []).append(c)
        return count

    # ---------------------------------------------------------------- operations
    def params(self, owner: str, params: list[Param], pos) -> dict:
        lists = {}
        for p in params:
            if _is_list(p.type):
                lists[p.name] = {"element": _element(p.type)}
        return lists

    def lower_params(self, owner: str, params: list[Param], lists: dict, bound: dict, pos) -> list[Param]:
        out: list[Param] = []
        names = {p.name for p in params}
        for p in params:
            if p.name not in lists:
                out.append(p)
                continue
            targets = bound.get(p.name, [])
            if not targets:
                self.errors.add("COLLECTION_PARAM_UNBOUND",
                                f"{owner}.{p.name}: List<{lists[p.name]['element']}> must be assigned to an attribute "
                                f"declared with multiplicity to determine its bounds", pos)
                p.type = "Text"
                out.append(p)
                continue
            t = targets[0]
            if any((x["entity"], x["attribute"]) != (t["entity"], t["attribute"]) for x in targets):
                self.errors.add("COLLECTION_PARAM_AMBIGUOUS",
                                f"{owner}.{p.name} is assigned to different collections: "
                                f"{sorted({x['entity'] + '.' + x['attribute'] for x in targets})}", pos)
            if p.name + COUNT_SUFFIX in names:
                self.errors.add("DUPLICATE_NAME", f"{owner}: parameter {p.name + COUNT_SUFFIX} conflicts with the cardinality of {p.name}", pos)
            p.type = "Text"
            p.collection = {"entity": t["entity"], "attribute": t["attribute"], "element": t["element"],
                            "min": t["min"], "max": t["max"], "separator": SEPARATOR}
            out.append(p)
            out.append(Param(p.name + COUNT_SUFFIX, "Natural", None, p.name))
        return out

    def operations(self) -> None:
        s = self.spec
        lowered: dict[str, list[Param]] = {}
        for o in s.operations:
            lists = self.params(o.name, o.params, o.pos)
            bound: dict = {}
            for c in o.cases:
                c.cond = self.rewrite(c.cond, lists) if c.cond is not None else None
                c.stmts = self.stmts(c.stmts, lists, bound)
            o.params = self.lower_params(o.name, o.params, lists, bound, o.pos)
            lowered[o.name] = o.params
        for q in s.queries:
            if any(_is_list(p.type) for p in q.params):
                self.errors.add("COLLECTION_PARAM_UNSUPPORTED", f"query {q.name}: List<T> is only supported in operations and compositions", q.pos)
            q.body = self.rewrite(q.body, {})
        for e in s.externals:
            for op in e.ops:
                if any(_is_list(p.type) for p in op.params):
                    self.errors.add("COLLECTION_PARAM_UNSUPPORTED",
                                    f"external {e.name}.{op.name}: List<T> is not supported in external contracts", op.pos)
                op.effects = {k: self.stmts(v, {}, {}) for k, v in op.effects.items()}
        for c in s.composes:
            lists = self.params(c.name, c.params, c.pos)
            calls = []
            bound: dict = {}
            for step in c.calls:
                name, args = step
                target = lowered.get(name)
                new_args = []
                guard = getattr(step, "guard", None)
                guard = self.rewrite(guard, lists) if guard is not None else None
                loop = getattr(step, "loop", None)
                loop = self.rewrite(loop, lists) if loop is not None else None
                if target is None:
                    calls.append(Step(name, [self.rewrite(a, lists) for a in args], guard,
                                      getattr(step, "trail", ()), loop))
                    continue
                positional = [p for p in target if p.count_of is None]
                for a, p in zip(args, positional):
                    a = self.rewrite(a, lists)
                    new_args.append(a)
                    if p.collection is None:
                        continue
                    extra = self.count_of(a, lists, c.pos)
                    if extra is None:
                        self.errors.add("COLLECTION_ASSIGN",
                                        f"compose {c.name}: argument for {name}.{p.name} must be a List<T> parameter "
                                        f"or a collection", c.pos)
                        continue
                    new_args.append(extra)
                    if a.get("k") == "var":
                        bound.setdefault(a["n"], []).append(
                            {"entity": p.collection["entity"], "attribute": p.collection["attribute"],
                             "element": p.collection["element"], "min": p.collection["min"], "max": p.collection["max"]})
                new_args.extend(self.rewrite(a, lists) for a in args[len(positional):])
                calls.append(Step(name, new_args, guard, getattr(step, "trail", ()), loop))
            c.calls = calls
            c.params = self.lower_params(f"compose {c.name}", c.params, lists, bound, c.pos)
        s.props = [self._prop(p) for p in s.props]
        if s.initial is not None:
            s.initial = self.rewrite(s.initial, {})

    def _prop(self, p: Prop) -> Prop:
        if p.expr is not None:
            p.expr = self.rewrite(p.expr, {})
        return p


def expand_collections(spec: Spec, errors) -> None:
    uses_collections = any(f.kind == "has" and f.multi for f in spec.facts) or any(
        _is_list(p.type) for o in [*spec.operations, *spec.composes, *spec.queries] for p in o.params) or any(
        n.get("k") == "count" for n in _walk_exprs(
            [[c.cond, [(s.key, s.value, s.inits) for s in c.stmts]] for o in spec.operations for c in o.cases]
            + [q.body for q in spec.queries] + [p.expr for p in spec.props] + [spec.initial]))
    if not uses_collections:
        return
    ex = _Expander(spec, errors)
    ex.facts()
    ex.operations()
