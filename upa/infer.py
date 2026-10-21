"""Semantic frontend: derive a minimal structural model from rules.

UPA does not require an upfront structure. Entities, attributes, attribute types, references (roles), and
write-only (ghost) state are derived from how rules, operations, and facts use them. Each derived element
retains provenance (`required_by`). Anything rules do not determine is reported as an ambiguity or unresolved
type; design conventions are not used to fill gaps.

Pipeline: rules -> semantic constraints -> concept classification -> minimal model -> ambiguities.
"""
from __future__ import annotations
from dataclasses import dataclass

from .ast import Attr, Entity, Origin, Prop, Spec
from .errors import ErrorBag

CATS = ("EXPLICIT_RULE", "INFERRED_STRUCTURE", "DERIVED_RULE", "DESIGN_DECISION", "EXTERNAL_ASSUMPTION")
BUILTIN = {"Bool": "bool", "Int": "int", "Natural": "nat", "Decimal": "dec", "Identifier": "id", "Text": "text"}


@dataclass
class T:
    kind: str                 # int nat bool id enum:X opt:? ? err
    decl: str | None = None   # Declared type name (Money, Status, ...).
    slot: tuple | None = None  # (entity, attribute) when the expression is an attribute.
    param: tuple | None = None  # (operation, parameter).


def classify_origin(o) -> str:
    if o is None:
        return "DESIGN_DECISION"
    return {"literal": "EXPLICIT_RULE", "derived": "DERIVED_RULE", "assumption": "EXTERNAL_ASSUMPTION"}.get(
        o.kind, "DESIGN_DECISION")


class Inference:
    def __init__(self, spec: Spec, errors: ErrorBag):
        self.s = spec
        self.errors = errors
        self.aliases = {a.name: a for a in spec.aliases}
        self.enums = {e.name: e for e in spec.enums}
        self.explicit = {e.name: e for e in spec.entities}
        self.constraints: list[dict] = []
        self.cands: dict[tuple, list] = {}      # slot -> [(T, source)]
        self.links: list[tuple] = []
        self.refs: dict[tuple, dict] = {}       # slot -> {class: {sources}}
        self.param_roles: dict[tuple, dict] = {}
        self.entity_use: dict[str, set] = {}     # entity -> sources
        self.slot_use: dict[tuple, set] = {}
        self.created: dict[str, set] = {}        # entity -> operations that create it
        self.inited: dict[tuple, set] = {}       # slot -> sources (create)
        self.set_targets: dict[tuple, set] = {}  # slot -> sources (set)
        self.nonghost_seed: dict[tuple, str] = {}
        self.set_edges: list[tuple] = []         # (destination slot, read slots, source)
        self.enum_guard: set[str] = set()        # enums referenced by guards and invariants
        self.enum_assigned: set[str] = set()
        self.fact_entities: dict[str, set] = {}
        self.in_value = False
        self._propagated: set = set()

    # ---------------------------------------------------------------- types
    def norm_decl(self, t: str) -> T:
        if t in BUILTIN:
            return T(BUILTIN[t], t)
        if t.startswith("Option<"):
            return T("opt:?", t)
        if t in self.aliases:
            return T(self.norm_decl(self.aliases[t].base).kind, t)
        if t in self.enums:
            return T("enum:" + t, t)
        return T("err", t)

    def add_constraint(self, kind, subject, detail, source):
        cid = f"SC-{len(self.constraints) + 1:03d}"
        self.constraints.append({"id": cid, "kind": kind, "subject": subject, "detail": detail, "source": source})
        return cid

    def use_entity(self, e, src):
        if e not in self.entity_use:
            self.entity_use[e] = set()
            self.add_constraint("ENTITY_REQUIRED", e, "a rule indexes or queries this concept by identity", src)
        self.entity_use[e].add(src)

    def use_slot(self, slot, src):
        self.use_entity(slot[0], src)
        if slot not in self.slot_use:
            self.slot_use[slot] = set()
            self.add_constraint("ATTRIBUTE_REQUIRED", f"{slot[0]}.{slot[1]}", "a rule reads or writes this attribute", src)
        self.slot_use[slot].add(src)

    def add_cand(self, slot, t: T, src):
        if t.kind in ("err", "?", "opt:?"):
            return
        self.cands.setdefault(slot, []).append((t, src))
        self.add_constraint("TYPE_EVIDENCE", f"{slot[0]}.{slot[1]}",
                            f"compatible with {t.decl or t.kind}", src)

    def unify(self, a: T, b: T, src):
        if a.slot and b.slot:
            self.links.append((a.slot, b.slot, src))
        elif a.slot:
            self.add_cand(a.slot, b, src)
        elif b.slot:
            self.add_cand(b.slot, a, src)

    def key_role(self, key_t: T, cls: str, src):
        """An expression used as an index for class `cls` plays a role of that class."""
        if key_t.slot:
            self.refs.setdefault(key_t.slot, {}).setdefault(cls, set()).add(src)
            self.add_cand(key_t.slot, T("id", "Identifier"), src)
            self.add_constraint("REFERENCE", f"{key_t.slot[0]}.{key_t.slot[1]}", f"reference to {cls}", src)
        elif key_t.param:
            self.param_roles.setdefault(key_t.param, {}).setdefault(cls, set()).add(src)

    # ---------------------------------------------------------------- expresiones
    def ex(self, e, env, src, reads=None) -> T:
        k = e["k"]
        if k == "int":
            return T("int")
        if k == "bool":
            return T("bool")
        if k == "str":
            return T("text", "Text")
        if k in ("none", "some"):
            if k == "some":
                self.ex(e["x"], env, src, reads)
            return T("opt:?")
        if k == "var":
            return env.get(e["n"], T("err"))
        if k == "enum":
            if e["e"] in self.enums:
                if not self.in_value:
                    self.enum_guard.add(e["e"])
                return T("enum:" + e["e"], e["e"])
            return T("err")
        if k == "attr":
            kt = self.ex(e["key"], env, src, reads)
            slot = (e["e"], e["a"])
            self.use_slot(slot, src)
            self.key_role(kt, e["e"], src)
            if kt.slot is None and kt.kind not in ("id", "err") and kt.param is None:
                pass
            if reads is not None:
                reads.append(slot)
            return T("?", slot=slot)
        if k == "in":
            kt = self.ex(e["key"], env, src, reads)
            self.use_entity(e["e"], src)
            self.key_role(kt, e["e"], src)
            return T("bool")
        if k == "sum":
            slot = (e["e"], e["a"])
            self.use_slot(slot, src)
            self.add_cand(slot, T("int"), src)
            return T("int")
        if k == "concat":
            for arg in (e["l"], e["r"]):
                t = self.ex(arg, env, src, reads)
                if t.slot:
                    self.add_cand(t.slot, T("text", "Text"), src)
            return T("text", "Text")
        if k == "norm":
            value = self.ex(e["x"], env, src, reads)
            if value.slot:
                self.add_cand(value.slot, T("text", "Text"), src)
            return T("text", "Text")
        if k == "to_text":
            self.ex(e["x"], env, src, reads)
            return T("text", "Text")
        if k == "text_length":
            value = self.ex(e["x"], env, src, reads)
            if value.slot:
                self.add_cand(value.slot, T("text", "Text"), src)
            return T("nat", "Natural")
        if k in ("contains", "starts_with", "ends_with"):
            for arg in (e["l"], e["r"]):
                value = self.ex(arg, env, src, reads)
                if value.slot:
                    self.add_cand(value.slot, T("text", "Text"), src)
            return T("bool")
        if k == "mod":
            value = self.ex(e["x"], env, src, reads)
            if value.slot:
                self.add_cand(value.slot, T("int", "Int"), src)
            return T("nat", "Natural")
        if k == "substring":
            value = self.ex(e["text"], env, src, reads)
            if value.slot:
                self.add_cand(value.slot, T("text", "Text"), src)
            for name in ("start", "length"):
                value = self.ex(e[name], env, src, reads)
                if value.slot:
                    self.add_cand(value.slot, T("int", "Int"), src)
            return T("text", "Text")
        if k == "replace_first":
            for name in ("text", "search", "replacement"):
                value = self.ex(e[name], env, src, reads)
                if value.slot:
                    self.add_cand(value.slot, T("text", "Text"), src)
            return T("text", "Text")
        if k == "old":
            return self.ex(e["x"], env, src, reads)
        if k == "result_is":
            return T("bool")
        if k == "unchanged":
            for f in e["except"]:
                if f["key"] is not None:
                    self.ex(f["key"], env, src, reads)
            return T("bool")
        if k == "un":
            self.want_bool(self.ex(e["x"], env, src, reads), src)
            return T("bool")
        if k == "if":
            self.want_bool(self.ex(e["c"], env, src, reads), src)
            a = self.ex(e["t"], env, src, reads)
            b = self.ex(e["f"], env, src, reads)
            self.unify(a, b, src)
            for c in (a, b):
                if c.decl and not c.slot:
                    return T(c.kind, c.decl)
            return a if not a.slot else (b if not b.slot else a)
        if k in ("forall", "exists"):
            env2 = dict(env)
            env2[e["v"]] = T("id", "Identifier")
            if e["e"]:
                self.use_entity(e["e"], src)
            self.want_bool(self.ex(e["body"], env2, src, reads), src)
            return T("bool")
        if k == "bin":
            op = e["op"]
            a = self.ex(e["l"], env, src, reads)
            b = self.ex(e["r"], env, src, reads)
            if op in ("and", "or", "=>"):
                self.want_bool(a, src)
                self.want_bool(b, src)
                return T("bool")
            if op in ("+", "-", "*"):
                for x in (a, b):
                    if x.slot:
                        self.add_cand(x.slot, T("int"), src)
                decl = next((x.decl for x in (a, b) if x.decl and not x.slot and x.kind in ("nat", "int")), None)
                return T("int", decl)
            self.unify(a, b, src)
            return T("bool")
        return T("err")

    def want_bool(self, t: T, src):
        if t.slot:
            self.add_cand(t.slot, T("bool", "Bool"), src)

    # ---------------------------------------------------------------- recorrido
    def walk(self):
        s = self.s
        for op in s.operations:
            src = f"OP:{op.name}"
            env = {p.name: self._param(op.name, p) for p in op.params}
            for c in op.cases:
                if c.cond is not None:
                    reads: list = []
                    self.ex(c.cond, env, src, reads)
                    for r in reads:
                        self.nonghost_seed.setdefault(r, src)
                base_env = env
                for st in c.stmts:
                    env = base_env
                    kr: list = []
                    env_s = env
                    if st.var is not None:
                        env_s = {**env, st.var: T("id", "Identifier")}
                        self.want_bool(self.ex(st.cond, env_s, src, kr), src)
                    kt = self.ex(st.key, env_s, src, kr)
                    slot_ent = st.entity
                    self.use_entity(slot_ent, src)
                    env = env_s
                    if st.kind == "delete":
                        self.key_role(kt, st.entity, src)
                    elif st.kind == "set":
                        slot = (st.entity, st.attr)
                        self.use_slot(slot, src)
                        self.set_targets.setdefault(slot, set()).add(src)
                        rr: list = []
                        self.in_value = True
                        vt = self.ex(st.value, env, src, rr)
                        self.in_value = False
                        self.unify(T("?", slot=slot), vt, src)
                        self.set_edges.append((slot, kr + rr, src))
                        self.key_role(kt, st.entity, src)
                        if vt.kind.startswith("enum:"):
                            self.enum_assigned.add(vt.kind[5:])
                    else:
                        self.created.setdefault(st.entity, set()).add(op.name)
                        self.key_role(kt, st.entity, src)
                        for a, v in st.inits:
                            slot = (st.entity, a)
                            self.use_slot(slot, src)
                            self.inited.setdefault(slot, set()).add(src)
                            self.nonghost_seed.setdefault(slot, src)
                            rr = []
                            self.in_value = True
                            vt = self.ex(v, env, src, rr)
                            self.in_value = False
                            for r in rr:
                                self.nonghost_seed.setdefault(r, src)
                            self.unify(T("?", slot=slot), vt, src)
                            self.add_constraint("CREATE_INITIALIZES", f"{st.entity}.{a}",
                                                f"{op.name} initializes the attribute when creating {st.entity}", src)
        for c in s.composes:
            env = {p.name: self._param(c.name, p) for p in c.params}
            for step in c.calls:
                for a in step[1] + ([step.guard] if getattr(step, "guard", None) is not None else []):
                    self.ex(a, env, f"OP:{c.name}")
        for q in s.queries:
            env = {p.name: self._param(q.name, p) for p in q.params}
            reads = []
            self.in_value = True
            self.ex(q.body, env, f"QUERY:{q.name}", reads)
            self.in_value = False
            for r in reads:
                self.nonghost_seed.setdefault(r, f"QUERY:{q.name}")
        if s.initial is not None:
            self.ex(s.initial, {}, "INITIAL")
        for p in s.props:
            env = {}
            if p.kind == "ensures":
                op = next((o for o in [*s.operations, *s.composes] if o.name == p.targets[0]), None)
                if op:
                    env = {pa.name: self._param(op.name, pa) for pa in op.params}
            if p.expr is not None:
                self.ex(p.expr, env, p.id)
        for f in s.facts:
            self.fact(f)

    def _param(self, owner, p):
        t = self.norm_decl(p.type)
        return T(t.kind, t.decl, None, (owner, p.name))

    def fact(self, f):
        src = f.id
        slot = (f.entity, f.attr)
        self.use_slot(slot, src)
        if f.kind == "has":
            self.add_constraint("STRUCTURAL_FACT", f"{f.entity}.{f.attr}",
                                f"{f.entity} has {f.card} {f.attr}: {f.target}", src)
            tt = self.norm_decl(f.target)
            if tt.kind == "err" and not f.target.startswith("Option<"):
                self.fact_entities.setdefault(f.target, set()).add(src)  # referenced concept only
                self.refs.setdefault(slot, {}).setdefault(f.target, set()).add(src)
                self.add_cand(slot, T("id", "Identifier"), src)
            else:
                self.add_cand(slot, tt, src)
            if f.card == "exactly_one" or f.multi:
                self.nonghost_seed.setdefault(slot, src)
        else:
            self.add_constraint("STRUCTURAL_FACT", f"{f.entity}.{f.attr}", f"{f.entity}.{f.attr} is {f.kind}", src)

    # ---------------------------------------------------------------- resolution
    def resolve(self):
        resolved: dict[tuple, T] = {}
        slots = set(self.slot_use)
        for final in (False, True):
            for _ in range(len(slots) + 2):
                changed = False
                for a, b, src in self.links:
                    for x, y in ((a, b), (b, a)):
                        if x in resolved and y not in resolved and (x, y, id(src)) not in self._propagated:
                            self._propagated.add((x, y, id(src)))
                            self.add_cand(y, resolved[x], src)
                for slot in sorted(slots):
                    if slot in resolved:
                        continue
                    t = self._pick(slot, final)
                    if t is not None:
                        resolved[slot] = t
                        changed = True
                if not changed:
                    break
        for slot in sorted(slots):
            if slot not in resolved and not self._explicit_attr(slot):
                self.errors.add("UNRESOLVED_TYPE",
                                f"the rules do not determine the type of {slot[0]}.{slot[1]}; add a fact "
                                f"('every {slot[0]} has exactly_one {slot[1]}: <Type>') or an assignment/comparison "
                                "that determines it", phase="infer", attribute=f"{slot[0]}.{slot[1]}")
        return resolved

    def _explicit_attr(self, slot):
        e = self.explicit.get(slot[0])
        return bool(e and any(a.name == slot[1] for a in e.attrs))

    def _pick(self, slot, final=True):
        cs = self.cands.get(slot, [])
        if not cs:
            return None
        enums = {c.kind for c, _ in cs if c.kind.startswith("enum:")}
        if len(enums) > 1:
            self.errors.add("INFERENCE_CONFLICT", f"{slot[0]}.{slot[1]} is used with different enums {sorted(enums)}",
                            phase="infer")
            return T("err")
        if enums:
            k = next(iter(enums))
            return T(k, k[5:])
        kinds = {c.kind for c, _ in cs}
        if "bool" in kinds:
            return T("bool", "Bool")
        if "id" in kinds:
            return T("id", "Identifier")
        if "text" in kinds:
            return T("text", "Text")
        decl = next((c.decl for c, _ in cs if c.decl in self.aliases), None)
        if decl:
            return T(self.norm_decl(decl).kind, decl)
        if not final:
            return None
        if "nat" in kinds:
            return T("nat", "Natural")
        if "int" in kinds:
            return T("int", "Int")
        return None

    # ---------------------------------------------------------------- ghost
    def ghost_slots(self, resolved):
        nonghost = set(self.nonghost_seed)
        changed = True
        while changed:
            changed = False
            for tgt, reads, _ in self.set_edges:
                if tgt in nonghost:
                    for r in reads:
                        if r not in nonghost:
                            nonghost.add(r)
                            changed = True
        return {s for s in self.slot_use if s not in nonghost}

    # ---------------------------------------------------------------- construction
    def run(self) -> dict:
        self.walk()
        resolved = self.resolve()
        ghost = self.ghost_slots(resolved)
        s = self.s

        entity_names = set(self.entity_use) | set(self.explicit)
        # model-only: concepts that appear only as the target of a fact
        ref_only = sorted(c for c in self.fact_entities if c not in entity_names)

        for e in sorted(entity_names):
            attrs_slots = sorted(a for (en, a) in self.slot_use if en == e)
            ent = self.explicit.get(e)
            if ent is None:
                ent = Entity(e, [], None, None, inferred=True)
                s.entities.append(ent)
                self.explicit[e] = ent
            have = {a.name for a in ent.attrs}
            for a in attrs_slots:
                if a in have:
                    continue
                t = resolved.get((e, a))
                if t is None or t.kind == "err":
                    continue
                ent.attrs.append(Attr(a, t.decl or t.kind, (e, a) in ghost))
            ent.attrs.sort(key=lambda x: x.name)

        # Derived facts on properties.
        for f in s.facts:
            if f.kind == "immutable":
                v = {"k": "var", "n": "k_"}
                expr = {"k": "forall", "v": "k_", "e": None, "body": {
                    "k": "bin", "op": "=>",
                    "l": {"k": "old", "x": {"k": "in", "e": f.entity, "key": v}},
                    "r": {"k": "bin", "op": "==",
                          "l": {"k": "attr", "e": f.entity, "a": f.attr, "key": v},
                          "r": {"k": "old", "x": {"k": "attr", "e": f.entity, "a": f.attr, "key": v}}}}}
                s.props.append(Prop("law", f"F_{f.id}", "business", f"{f.entity}.{f.attr} does not change once created",
                                    f.origin, expr, [], f.pos, origin_block=getattr(f, "origin_block", None)))
            elif f.kind == "unique":
                x, y = {"k": "var", "n": "x_"}, {"k": "var", "n": "y_"}
                guard = {"k": "bin", "op": "and",
                         "l": {"k": "bin", "op": "and", "l": {"k": "in", "e": f.entity, "key": x},
                               "r": {"k": "in", "e": f.entity, "key": y}},
                         "r": {"k": "bin", "op": "!=", "l": x, "r": y}}
                expr = {"k": "forall", "v": "x_", "e": None, "body": {"k": "forall", "v": "y_", "e": None, "body": {
                    "k": "bin", "op": "=>", "l": guard,
                    "r": {"k": "bin", "op": "!=", "l": {"k": "attr", "e": f.entity, "a": f.attr, "key": x},
                          "r": {"k": "attr", "e": f.entity, "a": f.attr, "key": y}}}}}
                s.props.append(Prop("invariant", f"F_{f.id}", "business", f"{f.entity}.{f.attr} is unique",
                                    f.origin, expr, [], f.pos, origin_block=getattr(f, "origin_block", None)))

        return self.report(resolved, ghost, ref_only)

    # ---------------------------------------------------------------- reports
    def report(self, resolved, ghost, ref_only) -> dict:
        s = self.s
        notes = {n.id: n for n in s.notes}

        # ---- ambiguities
        ambiguities, design = [], []
        for n in s.notes:
            if n.kind == "decision":
                design.append({"id": n.id, "classification": "DESIGN_DECISION", "text": n.text,
                               "alternatives": n.extra, "resolves": n.resolves, "chooses": n.chooses,
                               "status": "decided"})
        for e in sorted(self.created):
            ent = self.explicit.get(e)
            if ent is None or not ent.inferred:
                continue
            req = sorted(a.name for a in ent.attrs
                         if (e, a.name) in self.inited and (e, a.name) not in self.set_targets)
            res = sorted(a.name for a in ent.attrs if (e, a.name) in self.set_targets)
            if not req or not res:
                continue
            aid = f"AMB_{e}"
            dec = next((n for n in s.notes if n.kind == "decision" and n.resolves == aid), None)
            alts = [
                {"id": "MODEL_A", "description": f"{e} as a single persistent entity (attributes: {req + res})"},
                {"id": "MODEL_B", "description": f"immutable request record ({req}) and result record "
                                                  f"({res}) with the same key"},
            ]
            amb = {"id": aid, "status": "AMBIGUOUS_MODEL", "subject": e, "alternatives": alts,
                   "reason": "the current rules do not distinguish these implementations: both satisfy the same "
                             "invariants and observable operations", "resolved_by": None, "chosen": None}
            if dec is not None:
                if dec.chooses not in ("MODEL_A", "MODEL_B"):
                    self.errors.add("INVALID_DECISION", f"{dec.id}: '{dec.chooses}' is not an alternative for {aid}",
                                    phase="infer")
                elif dec.chooses == "MODEL_B":
                    self.errors.add("UNSUPPORTED_MODEL_CHOICE",
                                    f"{dec.id} selects MODEL_B; backend v0 only implements MODEL_A", phase="infer")
                amb["status"] = "RESOLVED_BY_DESIGN_DECISION"
                amb["resolved_by"] = dec.id
                amb["chosen"] = dec.chooses
            else:
                design.append({"id": f"PENDING_{aid}", "classification": "DESIGN_DECISION",
                               "text": f"choice between MODEL_A and MODEL_B for {e}; MODEL_A is used provisionally",
                               "status": "pending_human", "provisional": "MODEL_A", "resolves": aid})
            ambiguities.append(amb)

        # ---- inferred model with provenance
        model: dict = {}
        unjustified = []
        counts = dict(explicit_entities=0, inferred_entities=0, inferred_values=0, inferred_roles=0,
                      inferred_attributes=0, inferred_relations=0)
        for e in sorted(self.explicit):
            ent = self.explicit[e]
            imported = ent.origin_block is not None
            cls = "EXPLICIT_RULE" if not ent.inferred else "INFERRED_STRUCTURE"
            persisted = {n.text for n in s.notes if n.kind == "persists"}
            required = sorted(self.entity_use.get(e, set()))
            if not required and not imported and e not in persisted and not any(p.startswith(e + ".") for p in persisted):
                unjustified.append(e)
            counts["explicit_entities" if not ent.inferred else "inferred_entities"] += 1
            attrs = {}
            for a in ent.attrs:
                slot = (e, a.name)
                rq = sorted(self.slot_use.get(slot, set()))
                if not rq and f"{e}.{a.name}" in persisted:
                    rq = ["persists"]
                if not rq and not imported:
                    unjustified.append(f"{e}.{a.name}")
                refs = {c: sorted(v) for c, v in self.refs.get(slot, {}).items()}
                attrs[a.name] = {"type": a.type, "ghost": a.ghost,
                                 "specification_only": a.ghost,
                                 "classification": "INFERRED_STRUCTURE" if ent.inferred else "EXPLICIT_RULE",
                                 "role_of": sorted(refs), "required_by": rq,
                                 "type_evidence": sorted({src for _, src in self.cands.get(slot, [])})}
                if ent.inferred:
                    counts["inferred_attributes"] += 1
                if refs:
                    counts["inferred_roles"] += 1
            model[e] = {"kind": "Entity", "classification": cls, "required_by": required,
                        "created_by": sorted(self.created.get(e, [])), "attributes": attrs}
        relations = []
        for slot, cl in sorted(self.refs.items()):
            for cls, srcs in sorted(cl.items()):
                relations.append({"from": f"{slot[0]}.{slot[1]}", "role": slot[1], "plays": cls,
                                  "cardinality": "exactly_one" if any(
                                      f.entity == slot[0] and f.attr == slot[1] and f.card == "exactly_one"
                                      for f in s.facts) else "unspecified",
                                  "required_by": sorted(srcs), "classification": "INFERRED_STRUCTURE"})
                counts["inferred_relations"] += 1
        for c in ref_only:
            model[c] = {"kind": "EntityCandidate", "classification": "INFERRED_STRUCTURE",
                        "required_by": sorted(self.fact_entities[c]),
                        "note": "referenced only by facts; no behavioral rule uses its state, "
                                "is not materialized by the verifier", "attributes": {}}

        for h in unjustified:
            self.errors.add("UNJUSTIFIED_STRUCTURE", f"'{h}' is not required by any rule, operation, or fact",
                            phase="infer", member=h)

        # ---- parameter roles
        param_roles = [{"operation": o, "parameter": p, "plays": sorted(cl), "required_by": sorted(
            {x for v in cl.values() for x in v})} for (o, p), cl in sorted(self.param_roles.items())]
        role_groups: dict[str, list] = {}
        for r in relations:
            role_groups.setdefault(r["plays"], []).append(r["from"])
        role_notes = []
        for cls, owners in sorted(role_groups.items()):
            if len(owners) > 1:
                role_notes.append({
                    "class": cls, "roles": owners, "resolution": "REUSE",
                    "reason": f"roles {owners} share class {cls}; no rule introduces attributes that "
                              "distinguish a new type, so no role-specific classes are created"})

        # ---- concept classification
        concepts = []
        for e in sorted(self.explicit):
            ent = self.explicit[e]
            concepts.append({"name": e, "class": "Entity", "evidence": sorted(self.entity_use.get(e, set())),
                             "reason": "used with identity (indexed attributes / membership / creation)"})
        for c in ref_only:
            concepts.append({"name": c, "class": "Entity candidate (reference only)",
                             "evidence": sorted(self.fact_entities[c]),
                             "reason": "appears only as the target of a structural fact"})
        for a in s.aliases:
            concepts.append({"name": a.name, "class": "Value", "evidence": ["type"],
                             "reason": f"alias of {a.base} with scale {a.scale}; no identity"})
        for en in s.enums:
            guarded = en.name in self.enum_guard
            concepts.append({"name": en.name, "class": "State" if guarded else "Result",
                             "evidence": ["enum"],
                             "reason": ("its members are referenced in guards or properties (governs behavior)"
                                        if guarded else "its members are only recorded/observed; no rule branches on them")})
        for o in s.operations:
            concepts.append({"name": o.name, "class": "Operation", "evidence": [f"OP:{o.name}"],
                             "reason": "declared atomic transition"})
        for c in s.composes:
            concepts.append({"name": c.name, "class": "Operation", "evidence": [f"OP:{c.name}"],
                             "reason": "operation composition"})
        for r in relations:
            concepts.append({"name": r["from"], "class": "Role", "evidence": r["required_by"],
                             "reason": f"attribute used as the identifier for {r['plays']}: contextual role, not a class"})
        for r in param_roles:
            concepts.append({"name": f"{r['operation']}.{r['parameter']}", "class": "Role",
                             "evidence": r["required_by"], "reason": f"parameter used as the identifier for {r['plays']}"})

        # ---- extracted rules
        def rule_entry(p):
            return {"id": p.id, "kind": p.kind, "text": p.text,
                    "origin": {"kind": p.origin.kind, "ref": p.origin.ref} if p.origin else None,
                    "classification": classify_origin(p.origin)}
        extracted = {
            "facts": [{"id": f.id, "kind": f.kind, "subject": f"{f.entity}.{f.attr}", "card": f.card, "target": f.target,
                       "origin": {"kind": f.origin.kind, "ref": f.origin.ref} if f.origin else None,
                       "classification": classify_origin(f.origin)} for f in s.facts],
            "business_rules": [rule_entry(p) for p in s.props if p.group == "business"],
            "technical_rules": [rule_entry(p) for p in s.props if p.group == "technical"],
            "operations": [{"name": o.name, "params": [p.name for p in o.params], "classification": "DESIGN_DECISION",
                            "note": "operational decomposition chosen by the spec author"} for o in s.operations]
                          + [{"name": c.name, "params": [p.name for p in c.params], "classification": "DESIGN_DECISION",
                              "note": "composition"} for c in s.composes],
            "assumptions": [{"id": n.id, "text": n.text, "owner": n.extra, "classification": "EXTERNAL_ASSUMPTION"}
                            for n in s.notes if n.kind == "assumption"],
            "decisions": [{"id": n.id, "text": n.text, "classification": "DESIGN_DECISION"}
                          for n in s.notes if n.kind == "decision"],
            "open_questions": [{"id": n.id, "text": n.text, "classification": "UNSPECIFIED"}
                               for n in s.notes if n.kind == "question"],
        }

        # ---- provenance (reverse index)
        by_rule: dict[str, list] = {}
        for e, m in model.items():
            for r in m["required_by"]:
                by_rule.setdefault(r, []).append(e)
            for a, am in m["attributes"].items():
                for r in am["required_by"]:
                    by_rule.setdefault(r, []).append(f"{e}.{a}")
        provenance = {
            "why": {**{e: {"classification": m["classification"], "required_by": m["required_by"]}
                       for e, m in model.items()},
                    **{f"{e}.{a}": {"classification": am["classification"], "required_by": am["required_by"]}
                       for e, m in model.items() for a, am in m["attributes"].items()}},
            "by_rule": {k: sorted(set(v)) for k, v in sorted(by_rule.items())},
        }

        pending = [d for d in design if d.get("status") == "pending_human"]
        metrics = {
            **counts,
            "inferred_values": 0,
            "ambiguous_model_choices": len(ambiguities),
            "human_design_decisions_required": len(pending),
            "unjustified_structure_count": len(unjustified),
        }
        return {
            "extracted_rules": extracted,
            "semantic_constraints": self.constraints,
            "concept_classification": {"concepts": concepts, "role_notes": role_notes},
            "inferred_model": {"entities": model, "relations": relations, "parameter_roles": param_roles,
                               "categories": list(CATS)},
            "model_ambiguities": ambiguities,
            "design_decisions": design,
            "provenance": provenance,
            "metrics": metrics,
        }


def infer(spec: Spec, errors: ErrorBag) -> dict:
    return Inference(spec, errors).run()


def why(report: dict, member: str) -> dict:
    return report["provenance"]["why"].get(member, {"error": "UNKNOWN_MEMBER", "member": member})
