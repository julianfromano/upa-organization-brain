"""Semantic analysis: resolve names and types, and validate evidence origins for each property."""
from __future__ import annotations
import re
from copy import deepcopy

from .ast import Spec
from .errors import ErrorBag

INTLIKE = ("int", "nat")
NUMERIC = INTLIKE + ("dec",)
RESERVED_NAMES = {
    "spec", "block", "level", "uses", "type", "enum", "entity", "facts", "fact", "initial", "operation", "compose",
    "query", "business_rules", "technical_rules", "invariant", "law", "ensures", "repeat", "decision", "assumption",
    "question", "exclusion", "language_gap", "kind", "scope", "target", "impact", "required", "needed", "human_decision", "parameter", "persists", "risk", "concurrency", "delete", "origin", "layer", "when", "otherwise", "set", "create", "write_file", "external", "invoke", "yields", "outcome", "uncertain", "resolve_by", "http", "source", "hash", "status", "on", "for", "and", "or", "not", "in",
    "true", "false", "if", "then", "else", "old", "sum", "trim", "upper", "lower", "digits_only", "concat", "to_text", "text_length", "contains", "starts_with",
    "ends_with", "replace_first", "substring", "mod", "forall", "exists", "None", "Some",
    "Bool", "Int", "Natural", "Decimal", "Identifier", "Text", "Option"}


def norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _from_component(x) -> bool:
    """Rule or fact imported from a component (`Alias=ref`); its origin was validated against the scope and
    decisions of that component during compilation."""
    return "=" in (getattr(x, "origin_block", None) or "")


def _state_free(e) -> bool:
    if isinstance(e, dict):
        if e.get("k") in ("attr", "in", "sum", "old", "forall", "exists"):
            return False
        return all(_state_free(v) for k, v in e.items() if k != "via")
    if isinstance(e, list):
        return all(_state_free(v) for v in e)
    return True


class Analyzer:
    def __init__(self, spec: Spec, scope_text: str | None, errors: ErrorBag):
        self.spec = spec
        self.scope = norm_ws(scope_text) if scope_text else None
        self.errors = errors
        self.aliases = {a.name: a for a in spec.aliases}
        self.enums = {e.name: e for e in spec.enums}
        self.entities = {e.name: e for e in spec.entities}
        self.ops = {o.name: o for o in spec.operations}
        self.composes = {c.name: c for c in spec.composes}
        self.notes = {n.id: n for n in spec.notes}

    # ---- Types
    def norm(self, t: str, pos=None) -> str:
        if t == "Bool":
            return "bool"
        if t == "Int":
            return "int"
        if t == "Natural":
            return "nat"
        if t == "Decimal":
            return "dec"
        if t == "Identifier":
            return "id"
        if t == "Text":
            return "text"
        if t.startswith("Option<"):
            return "opt:" + self.norm(t[7:-1], pos)
        if t in self.aliases:
            return self.norm(self.aliases[t].base, pos)
        if t in self.enums:
            return "enum:" + t
        self.errors.add("UNKNOWN_TYPE", f"unknown type '{t}'", pos)
        return "err"

    def compatible(self, expected: str, got: str) -> bool:
        if "err" in (expected, got):
            return True
        if expected in INTLIKE and got in INTLIKE:
            return True
        if expected == "dec" and got in NUMERIC:
            return True
        return expected == got

    def attr_type(self, ent: str, attr: str, pos):
        e = self.entities.get(ent)
        if not e:
            self.errors.add("UNKNOWN_ENTITY", f"unknown entity '{ent}'", pos)
            return None, False
        for a in e.attrs:
            if a.name == attr:
                return self.norm(a.type, pos), a.ghost
        self.errors.add("UNKNOWN_ATTRIBUTE", f"entity '{ent}' has no attribute '{attr}'", pos)
        return None, False

    # ---- expresiones
    def check(self, e: dict, env: dict, allow_old=False, allow_ghost=True, in_old=False) -> str:
        k = e["k"]
        pos = e.get("pos")
        t = "err"
        if k == "int":
            t = "int"
        elif k == "bool":
            t = "bool"
        elif k == "str":
            t = "text"
        elif k == "none":
            t = "opt:?"
        elif k == "some":
            t = "opt:" + self.check(e["x"], env, allow_old, allow_ghost, in_old)
        elif k == "var":
            if e["n"] in env:
                t = env[e["n"]]
            else:
                self.errors.add("UNKNOWN_NAME", f"undeclared name '{e['n']}'", pos)
        elif k == "enum":
            en = self.enums.get(e["e"])
            if not en:
                self.errors.add("UNKNOWN_NAME", f"'{e['e']}' is neither an enum nor an indexed entity", pos)
            elif e["v"] not in en.members:
                self.errors.add("UNKNOWN_ENUM_MEMBER", f"'{e['e']}' has no member '{e['v']}'", pos)
            else:
                t = "enum:" + e["e"]
        elif k == "attr":
            kt = self.check(e["key"], env, allow_old, allow_ghost, in_old)
            if not self.compatible("id", kt):
                self.errors.add("TYPE_MISMATCH", "an attribute index must be an Identifier", pos)
            at, ghost = self.attr_type(e["e"], e["a"], pos)
            if at is not None:
                t = at
                if ghost and not allow_ghost:
                    self.errors.add("GHOST_IN_BEHAVIOR",
                                    f"ghost attribute '{e['e']}.{e['a']}' may only be used in properties and ghost assignments", pos)
        elif k == "in":
            if e["e"] not in self.entities:
                self.errors.add("UNKNOWN_ENTITY", f"unknown entity '{e['e']}'", pos)
            kt = self.check(e["key"], env, allow_old, allow_ghost, in_old)
            if not self.compatible("id", kt):
                self.errors.add("TYPE_MISMATCH", "entity membership requires an Identifier", pos)
            t = "bool"
        elif k == "sum":
            at, _ = self.attr_type(e["e"], e["a"], pos)
            if at is not None and at not in INTLIKE:
                self.errors.add("TYPE_MISMATCH", "sum() requires a numeric attribute", pos)
            t = "int"
        elif k == "concat":
            left = self.check(e["l"], env, allow_old, allow_ghost, in_old)
            right = self.check(e["r"], env, allow_old, allow_ghost, in_old)
            if left not in ("text", "err") or right not in ("text", "err"):
                self.errors.add("TYPE_MISMATCH", f"concat() requires Text operands (received {left}, {right})", pos)
            t = "text"
        elif k == "norm":
            value = self.check(e["x"], env, allow_old, allow_ghost, in_old)
            if value not in ("text", "err"):
                self.errors.add("TYPE_MISMATCH", f"{e['op']}() requires Text (received {value})", pos)
            t = "text"
        elif k == "to_text":
            value = self.check(e["x"], env, allow_old, allow_ghost, in_old)
            if value not in INTLIKE + ("id", "err"):
                self.errors.add("TYPE_MISMATCH", f"to_text() requires Int, Natural, or Identifier (received {value})", pos)
            t = "text"
        elif k == "text_length":
            value = self.check(e["x"], env, allow_old, allow_ghost, in_old)
            if value not in ("text", "err"):
                self.errors.add("TYPE_MISMATCH", f"text_length() requires Text (received {value})", pos)
            t = "nat"
        elif k in ("contains", "starts_with", "ends_with"):
            left = self.check(e["l"], env, allow_old, allow_ghost, in_old)
            right = self.check(e["r"], env, allow_old, allow_ghost, in_old)
            if left not in ("text", "err") or right not in ("text", "err"):
                self.errors.add("TYPE_MISMATCH", f"{k}() requires two Text operands (received {left}, {right})", pos)
            t = "bool"
        elif k == "mod":
            tx = self.check(e["x"], env, allow_old, allow_ghost, in_old)
            if tx not in INTLIKE + ("err",):
                self.errors.add("TYPE_MISMATCH", f"mod() requires Int (received {tx})", pos)
            t = "nat"
        elif k == "substring":
            tt = self.check(e["text"], env, allow_old, allow_ghost, in_old)
            ts = self.check(e["start"], env, allow_old, allow_ghost, in_old)
            tl = self.check(e["length"], env, allow_old, allow_ghost, in_old)
            if tt not in ("text", "err") or ts not in INTLIKE + ("err",) or tl not in INTLIKE + ("err",):
                self.errors.add("TYPE_MISMATCH", "substring() requires (Text, Int, Int)", pos)
            t = "text"
        elif k == "replace_first":
            values = [self.check(e[key], env, allow_old, allow_ghost, in_old)
                      for key in ("text", "search", "replacement")]
            if any(value not in ("text", "err") for value in values):
                self.errors.add("TYPE_MISMATCH", "replace_first() requires three Text operands", pos)
            t = "text"
        elif k == "old":
            if not allow_old:
                self.errors.add("OLD_NOT_ALLOWED", "old() is only allowed in 'law' and 'ensures'", pos)
            t = self.check(e["x"], env, allow_old, allow_ghost, True)
        elif k in ("forall", "exists"):
            if e["e"] is not None and e["e"] not in self.entities:
                self.errors.add("UNKNOWN_ENTITY", f"unknown entity '{e['e']}'", pos)
            env2 = dict(env)
            env2[e["v"]] = "id"
            if self.check(e["body"], env2, allow_old, allow_ghost, in_old) not in ("bool", "err"):
                self.errors.add("TYPE_MISMATCH", "a quantifier body must be Bool", pos)
            t = "bool"
        elif k == "if":
            if self.check(e["c"], env, allow_old, allow_ghost, in_old) not in ("bool", "err"):
                self.errors.add("TYPE_MISMATCH", "the 'if' condition must be Bool", pos)
            a = self.check(e["t"], env, allow_old, allow_ghost, in_old)
            b = self.check(e["f"], env, allow_old, allow_ghost, in_old)
            if not self.compatible(a, b):
                self.errors.add("TYPE_MISMATCH", f"the 'if' branches have incompatible types: {a} / {b}", pos)
            t = "dec" if "dec" in (a, b) else "int" if a in INTLIKE and b in INTLIKE and a != b else (a if a != "err" else b)
        elif k == "un":
            if self.check(e["x"], env, allow_old, allow_ghost, in_old) not in ("bool", "err"):
                self.errors.add("TYPE_MISMATCH", "'not' requires Bool", pos)
            t = "bool"
        elif k == "bin":
            op = e["op"]
            a = self.check(e["l"], env, allow_old, allow_ghost, in_old)
            b = self.check(e["r"], env, allow_old, allow_ghost, in_old)
            if op in ("and", "or", "=>"):
                if not (a in ("bool", "err") and b in ("bool", "err")):
                    self.errors.add("TYPE_MISMATCH", f"'{op}' requires Bool operands", pos)
                t = "bool"
            elif op in ("+", "-", "*"):
                if not (a in NUMERIC + ("err",) and b in NUMERIC + ("err",)):
                    self.errors.add("TYPE_MISMATCH", f"'{op}' requires numeric operands (received {a}, {b})", pos)
                t = "dec" if "dec" in (a, b) else "int"
            else:
                ordered = op in ("<", "<=", ">", ">=")
                if not (self.compatible(a, b) or (a in NUMERIC and b in NUMERIC)):
                    self.errors.add("TYPE_MISMATCH", f"cannot compare {a} with {b}", pos)
                elif ordered and not (a in NUMERIC or a.startswith("enum:")):
                    self.errors.add("TYPE_MISMATCH", f"'{op}' requires numbers or enums", pos)
                t = "bool"
        else:  # pragma: no cover
            self.errors.add("INTERNAL", f"unknown node {k}", pos)
        e["ty"] = t
        return t

    # ---- Declarations
    def run(self):
        s = self.spec
        seen: dict[str, str] = {}

        def reg(name, kind, pos):
            if name in RESERVED_NAMES:
                self.errors.add("RESERVED_NAME", f"'{name}' is a reserved UPA word and cannot name a {kind}", pos)
            if name in seen:
                self.errors.add("DUPLICATE_NAME",
                                f"naming rule: '{name}' is already declared as {seen[name]}; types, enums, "
                                "operations, queries, compositions, and notes share one namespace. "
                                "Rename it (e.g. query StateOf, enum PageState)", pos)
            seen[name] = kind

        for a in s.aliases:
            reg(a.name, "type", a.pos)
            if a.base not in ("Natural", "Int"):
                self.errors.add("UNKNOWN_TYPE", f"an alias can only be based on Natural or Int, not '{a.base}'", a.pos)
        for en in s.enums:
            reg(en.name, "enum", en.pos)
            if len(set(en.members)) != len(en.members):
                self.errors.add("DUPLICATE_NAME", f"duplicate enum members in '{en.name}'", en.pos)
        for ent in s.entities:
            reg(ent.name, "entity", ent.pos)
            names = [a.name for a in ent.attrs]
            if len(set(names)) != len(names):
                self.errors.add("DUPLICATE_NAME", f"duplicate attributes in '{ent.name}'", ent.pos)
            for a in ent.attrs:
                if self.norm(a.type, ent.pos).startswith("opt:"):
                    self.errors.add("UNSUPPORTED", "Option is not supported as an attribute type in v0", ent.pos)
        for o in s.operations:
            reg(o.name, "operation", o.pos)
        for c in s.composes:
            reg(c.name, "compose", c.pos)
        for q in s.queries:
            reg(q.name, "query", q.pos)
        for n in s.notes:
            reg(n.id, n.kind, n.pos)
        for p in s.props:
            reg(p.id, p.kind, p.pos)
        for f in s.facts:
            reg(f.id, "fact", f.pos)
            if not _from_component(f):
                self.check_origin(f.origin, f.id, f.pos)

        if s.initial is not None:
            if not (s.initial_origin is None and getattr(s, "initial_from_components", False)):
                self.check_origin(s.initial_origin, "initial", s.initial.get("pos"))
            self.check(s.initial, {}, False, True)
            if s.initial["ty"] not in ("bool", "err"):
                self.errors.add("TYPE_MISMATCH", "'initial' must be Bool", s.initial.get("pos"))

        for n in s.notes:
            if n.kind == "concurrency":
                self.concurrency(n)
            elif n.kind == "language_gap":
                self.language_gap(n)
        for o in s.operations:
            self.operation(o)
        for c in s.composes:
            self.compose(c)
        for q in s.queries:
            env = {p.name: self.norm(p.type, q.pos) for p in q.params}
            got = self.check(q.body, env, False, False)
            want = self.norm(q.ret, q.pos)
            if got == "opt:?" or (want.startswith("opt:") and got.startswith("opt:") and "?" in got):
                pass
            elif not self.compatible(want, got):
                self.errors.add("TYPE_MISMATCH", f"query '{q.name}' returns {got}, but declares {want}", q.pos)
        for p in s.props:
            self.prop(p)

    def concurrency(self, n):
        m = n.meta
        op = next((o for o in self.spec.operations if o.name == m.get("on")), None) if m.get("on") else None
        if m.get("on") and op is None:
            self.errors.add("UNKNOWN_OPERATION", f"{n.id}: operation '{m['on']}' does not exist at this level", n.pos)
        mode = m.get("mode")
        if mode is not None and mode not in ("serialized", "optimistic", "unprotected"):
            self.errors.add("INVALID_CONCURRENCY", f"{n.id}: mode must be serialized, optimistic, or unprotected", n.pos)
        if m.get("on_conflict") not in (None, "retry", "reject", "wait"):
            self.errors.add("INVALID_CONCURRENCY", f"{n.id}: on_conflict must be retry, reject, or wait", n.pos)
        if mode in ("serialized", "optimistic") and not m.get("by"):
            self.errors.add("INVALID_CONCURRENCY", f"{n.id}: mode {mode} requires 'by' (a parameter or entity that defines the serialization unit)", n.pos)
        by = m.get("by")
        if by and not (op and any(p.name == by for p in op.params)) and by not in self.entities:
            self.errors.add("INVALID_CONCURRENCY", f"{n.id}: 'by {by}' is not an operation parameter or entity", n.pos)
        rule_ids = {p.id for p in self.spec.props}
        for r in m.get("protects", []):
            if r not in rule_ids:
                self.errors.add("UNKNOWN_RULE", f"{n.id}: 'protects {r}': rule is not defined at this level", n.pos)

    def language_gap(self, n):
        m = n.meta
        if m.get("kind") not in ("language", "evidence", "regeneration"):
            self.errors.add("INVALID_GAP_KIND",
                            f"{n.id}: kind must be language, evidence, or regeneration", n.pos)
        if m.get("impact") not in ("low", "medium", "high", "critical"):
            self.errors.add("INVALID_GAP_IMPACT",
                            f"{n.id}: impact must be low, medium, high, or critical", n.pos)
        if not isinstance(m.get("required"), bool):
            self.errors.add("INVALID_GAP_REQUIRED", f"{n.id}: required/needed must be true or false", n.pos)
        if not n.text.strip():
            self.errors.add("EMPTY_GAP_TEXT", f"{n.id}: text must not be empty", n.pos)
        if m.get("scope") is not None and not str(m["scope"]).strip():
            self.errors.add("INVALID_GAP_SCOPE", f"{n.id}: scope must not be empty", n.pos)
        target = m.get("target")
        if target and not self._gap_target_resolves(target):
            self.errors.add("UNKNOWN_GAP_TARGET",
                            f"{n.id}: target {target!r} does not reference an operation, composition, query, rule, fact, note, or component",
                            n.pos)

    def _gap_target_resolves(self, target: str) -> bool:
        def resolves(spec, path):
            head, dot, rest = path.partition(".")
            ids = ({o.name for o in spec.operations} | {c.name for c in spec.composes}
                   | {q.name for q in spec.queries} | {p.id for p in spec.props}
                   | {f.id for f in spec.facts} | {n.id for n in spec.notes}
                   | {c.alias for c in spec.components})
            if not dot:
                return head in ids
            child = getattr(spec, "_kids", {}).get(head)
            return child is not None and resolves(child, rest)

        return resolves(self.spec, target)

    def operation(self, o):
        env = {p.name: self.norm(p.type, o.pos) for p in o.params}
        if len(env) != len(o.params):
            self.errors.add("DUPLICATE_NAME", f"duplicate parameters in '{o.name}'", o.pos)
        otherwise_seen = False
        for i, c in enumerate(o.cases):
            if otherwise_seen:
                self.errors.add("UNREACHABLE_CASE", "a case appears after 'otherwise'", c.pos)
            if c.cond is None:
                otherwise_seen = True
            else:
                if self.check(c.cond, env, False, False) not in ("bool", "err"):
                    self.errors.add("TYPE_MISMATCH", "the 'when' condition must be Bool", c.pos)
            created: set[str] = set()
            for st in c.stmts:
                self.stmt(st, env, o, created)

    def stmt(self, st, env, op, created):
        if st.entity not in self.entities:
            self.errors.add("UNKNOWN_ENTITY", f"unknown entity '{st.entity}'", st.pos)
            return
        ent = self.entities[st.entity]
        if st.var is not None:
            if st.bulk_entity != st.entity:
                self.errors.add("BULK_ENTITY", f"'for each {st.var} in {st.bulk_entity}' must iterate over {st.bulk_entity}, not {st.entity}", st.pos)
            if st.var in env:
                self.errors.add("DUPLICATE_NAME", f"'for each' variable '{st.var}' is already a parameter", st.pos)
            if not (isinstance(st.key, dict) and st.key.get("k") == "var" and st.key.get("n") == st.var):
                self.errors.add("BULK_KEY", f"inside 'for each {st.var}', the key must be '{st.var}'", st.pos)
            env = {**env, st.var: "id"}
            if self.check(st.cond, env, False, False) not in ("bool", "err"):
                self.errors.add("TYPE_MISMATCH", "the 'where' filter in 'for each' must be Bool", st.pos)
        if self.check(st.key, env, False, False) not in ("id", "err"):
            self.errors.add("TYPE_MISMATCH", "the key must be an Identifier", st.pos)
        if st.kind == "delete":
            return
        if st.kind == "set":
            at, ghost = self.attr_type(st.entity, st.attr, st.pos)
            if at is None:
                return
            vt = self.check(st.value, env, False, allow_ghost=ghost)
            if not self.compatible(at, vt):
                self.errors.add("TYPE_MISMATCH",
                                f"cannot assign {vt} a {st.entity}.{st.attr} ({at})", st.pos)
        else:
            inited = set()
            for a, v in st.inits:
                at, ghost = self.attr_type(st.entity, a, st.pos)
                if at is None:
                    continue
                if ghost:
                    self.errors.add("GHOST_IN_BEHAVIOR", f"'create' cannot initialize ghost attribute '{a}'", st.pos)
                if a in inited:
                    self.errors.add("DUPLICATE_NAME", f"attribute '{a}' is initialized more than once", st.pos)
                inited.add(a)
                vt = self.check(v, env, False, False)
                if not self.compatible(at, vt):
                    self.errors.add("TYPE_MISMATCH", f"cannot assign {vt} a {st.entity}.{a} ({at})", st.pos)
            missing = [a.name for a in ent.attrs if not a.ghost and a.name not in inited]
            if missing:
                self.errors.add("CREATE_INCOMPLETE",
                                f"'create {st.entity}' does not initialize attributes {missing}", st.pos,
                                missing=missing)

    def compose(self, c):
        env = {p.name: self.norm(p.type, c.pos) for p in c.params}
        for step in c.calls:
            name, args = step
            guard = getattr(step, "guard", None)
            if guard is not None:
                if self.check(guard, env, False, False) != "bool":
                    self.errors.add("TYPE_MISMATCH", f"compose {c.name}: the step {name} 'when' condition must be Bool", c.pos)
                if _state_free(guard) is False:
                    self.errors.add("COMPOSE_GUARD_STATE",
                                    f"compose {c.name}: the step {name} 'when' condition may only use parameters "
                                    f"and pure queries (which do not read state)", c.pos)
            loop = getattr(step, "loop", None)
            if loop is not None:
                if self.check(loop, env, False, False) != "bool":
                    self.errors.add("TYPE_MISMATCH", f"compose {c.name}: the loop {name} 'while' condition must be Bool", c.pos)
                if _state_free(loop) is False:
                    self.errors.add("COMPOSE_LOOP_STATE",
                                    f"compose {c.name}: the loop {name} 'while' condition may only use parameters "
                                    f"and pure queries (which do not read state)", c.pos)
            target = self.ops.get(name)
            if not target:
                self.errors.add("UNKNOWN_OPERATION", f"'{name}' is not an operation", c.pos)
                continue
            if len(args) != len(target.params):
                self.errors.add("ARITY", f"'{name}' espera {len(target.params)} argumentos", c.pos)
                continue
            for a, p in zip(args, target.params):
                got = self.check(a, env, False, False)
                if not self.compatible(self.norm(p.type, c.pos), got):
                    self.errors.add("TYPE_MISMATCH", f"argument to '{name}.{p.name}': {got}", c.pos)

    def check_origin(self, o, pid, pos):
        if o is None:
            self.errors.add("ORIGIN_MISSING",
                            f"'{pid}' does not declare an origin (literal | derived | assumption)", pos, property=pid)
        elif o.kind == "unspecified":
            self.errors.add("UNSPECIFIED_AS_GUARANTEE",
                            f"{pid}: an 'unspecified' property cannot be a guarantee; declare a "
                            "derived decision or assumption, or leave it as 'question'", pos, property=pid)
        elif o.kind == "literal":
            if self.scope is not None and pid not in self.spec.inherited_ids and norm_ws(o.ref) not in self.scope:
                self.errors.add("LITERAL_NOT_IN_SCOPE",
                                f"{pid}: literal citation does not appear in the informal scope", pos, property=pid,
                                quote=o.ref)
        elif o.kind in ("derived", "assumption"):
            want = "decision" if o.kind == "derived" else "assumption"
            n = self.notes.get(o.ref)
            if n is None or n.kind != want:
                self.errors.add("ORIGIN_REF_UNKNOWN", f"{pid}: '{o.ref}' is not a declared {want}", pos,
                                property=pid)
        else:
            self.errors.add("ORIGIN_KIND", f"{pid}: unknown origin '{o.kind}'", pos)

    def prop(self, p):
        if not _from_component(p):
            self.check_origin(p.origin, p.id, p.pos)

        if p.kind == "invariant":
            t = self.check(p.expr, {}, False, True)
        elif p.kind == "law":
            t = self.check(p.expr, {}, True, True)
        elif p.kind == "ensures":
            op = self.ops.get(p.targets[0]) or self.composes.get(p.targets[0])
            if not op:
                self.errors.add("UNKNOWN_OPERATION", f"{p.id}: '{p.targets[0]}' is not an operation", p.pos)
                return
            env = {pa.name: self.norm(pa.type, p.pos) for pa in op.params}
            t = self.check(p.expr, env, True, True)
        else:
            for tgt in p.targets:
                if tgt not in self.ops:
                    self.errors.add("UNKNOWN_OPERATION", f"{p.id}: '{tgt}' is not an operation", p.pos)
            return
        if t not in ("bool", "err"):
            self.errors.add("TYPE_MISMATCH", f"{p.id}: property must be Bool", p.pos)


def analyze(spec: Spec, scope_text: str | None, errors: ErrorBag) -> Spec:
    Analyzer(spec, scope_text, errors).run()
    return spec


def strip_pos(x):
    """Create a canonical copy without source positions for the IR."""
    if isinstance(x, dict):
        return {k: strip_pos(v) for k, v in sorted(x.items()) if k != "pos"}
    if isinstance(x, list):
        return [strip_pos(v) for v in x]
    if isinstance(x, tuple):
        return [strip_pos(v) for v in x]
    return x


def clone_expr(e):
    return deepcopy(e)
