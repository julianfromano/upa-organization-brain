"""Z3 formal verification backend. Consumes the IR and does not know UPA syntax.

Each entity E is encoded as total relations: E.exists : Int -> Bool and E.<attr> : Int -> Int|Bool.
An operation is a deterministic, atomic transition relation over that state. The proof obligations are:

  INIT   the declared initial state establishes every invariant and type invariant
  PRES   every invariant is preserved at each step (induction), assuming all invariants held previously
  TYPE   type invariants (Natural >= 0, enum range) are preserved at each step
  LAW    a two-state law holds for every operation
  ENS    an operation postcondition holds
  REPEAT applying an operation twice is equivalent to applying it once

Results per obligation: PROVEN | REFUTED (with counterexample) | UNKNOWN. UNKNOWN is never treated as success.
"""
from __future__ import annotations
import os
import hashlib
import json
import re
import time
from itertools import count

import z3

from .ir import ir_hash

STDLIB_AXIOMS = {
    "AX_SUM_STORE": {
        "text": "sum(store(m,k,v)) = sum(m) - m[k] + v  (sum of a finite-support integer map)",
        "source": "stdlib",
        "evidence": "tests/test_axioms.py (sampled check: tested only; not proven within UPA)",
    },
    "AX_FILE_WRITE": {
        "text": "write_file is atomic and total: after the step, File.content[key] is exactly the written text (full disks, permissions, and real file-system concurrency are out of scope)",
        "source": "stdlib",
        "evidence": "declared; the real effect is checked only at layer 2/3 (IFileSink)",
    },
    "AX_TYPED_INITIAL": {
        "text": "the initial state conforms to attribute types (Natural >= 0, enums in range)",
        "source": "stdlib",
        "evidence": "declared; guaranteed by the environment providing the initial state; not verified",
    },
    "AX_FINITE_SUPPORT": {
        "text": "every state map has finite support (the initial state is finite and each step modifies only finitely many keys)",
        "source": "stdlib",
        "evidence": "declared; not verified",
    },
}


def has_node(e, kind) -> bool:
    if isinstance(e, dict):
        if e.get("k") == kind:
            return True
        return any(has_node(v, kind) for v in e.values())
    if isinstance(e, list):
        return any(has_node(v, kind) for v in e)
    return False


FRAME = os.environ.get("UPA_NO_FRAME") != "1"
MODULAR = os.environ.get("UPA_NO_MODULAR") != "1"
RELEVANCE = os.environ.get("UPA_NO_RELEVANCE") != "1"
MODULAR_REASON = ("modular: every step of the compose preserves all invariants and types (step obligations), so "
                  "the intermediate states, final state, and rollback of an atomic compose preserve them too")


def state_reads(e):
    """State symbols read by a Core expression: the attr/sum attribute and the exists set used by in and bounded quantifiers
    The expression depends only on these symbols (plus variables and constants), which underlies the frame proof."""
    out = set()
    def walk(x):
        if isinstance(x, dict):
            k = x.get("k")
            if k in ("attr", "sum"):
                out.add(f"{x['e']}.{x['a']}")
            elif k == "in" or (k in ("forall", "exists") and x.get("e") is not None):
                out.add(f"{x['e']}.exists")
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(e)
    return out


class Verifier:
    def __init__(self, ir: dict, timeout_ms: int = 30000, inherited: dict | None = None):
        self.ir = ir
        self.enums = ir["enums"]
        self.enum_attrs = {f"{e}.{a['name']}" for e, info in ir["entities"].items() for a in info["attrs"] if a["type"].startswith("enum:")}
        self.entities = ir["entities"]
        self.timeout = timeout_ms
        self._uid = count()
        self.SUM = z3.Function("SUM", z3.ArraySort(z3.IntSort(), z3.IntSort()), z3.IntSort())
        self.NORM = {op: z3.Function(f"NORM_{op}", z3.StringSort(), z3.StringSort()) for op in ("trim", "upper", "lower", "digits_only")}
        self.ops = {o["name"]: o for o in ir["operations"]}
        self.comps = {c["name"]: c for c in ir["composes"]}
        self.props = ir["properties"]
        self.invs = [p for p in self.props if p["kind"] == "invariant"]
        self.laws = [p for p in self.props if p["kind"] == "law"]
        self.ens = [p for p in self.props if p["kind"] == "ensures"]
        self.reps = [p for p in self.props if p["kind"] == "repeat"]
        self.inherited = inherited or {}
        self.cache = None          # {key: {"hyp": [...]}} — PROVEN entries only; see csolve
        self.cache_stats = {"reused": 0, "fresh": 0, "frame": 0, "solver": 0}
        self.children = {}

    # ------------------------------------------------------------ state
    def var_keys(self):
        keys = []
        for e, info in self.entities.items():
            keys.append((f"{e}.exists", z3.ArraySort(z3.IntSort(), z3.BoolSort())))
            for a in info["attrs"]:
                val = {"bool": z3.BoolSort(), "text": z3.StringSort(), "dec": z3.RealSort()}.get(a["type"], z3.IntSort())
                keys.append((f"{e}.{a['name']}", z3.ArraySort(z3.IntSort(), val)))
        return keys

    def fresh_state(self, tag):
        return {k: z3.Const(f"{tag}|{k}", s) for k, s in self.var_keys()}

    # ------------------------------------------------------------ Expressions
    def tr(self, e, st, env, old=None):
        k = e["k"]
        if k == "int":
            return z3.IntVal(e["v"])
        if k == "bool":
            return z3.BoolVal(e["v"])
        if k == "str":
            return z3.StringVal(e["v"])
        if k == "var":
            return env[e["n"]]
        if k == "enum":
            return z3.IntVal(self.enums[e["e"]].index(e["v"]))
        if k == "attr":
            return z3.Select(st[f"{e['e']}.{e['a']}"], self.tr(e["key"], st, env, old))
        if k == "in":
            return z3.Select(st[f"{e['e']}.exists"], self.tr(e["key"], st, env, old))
        if k == "sum":
            return self.SUM(st[f"{e['e']}.{e['a']}"])
        if k == "concat":
            return z3.Concat(self.tr(e["l"], st, env, old), self.tr(e["r"], st, env, old))
        if k == "norm":
            return self.NORM[e["op"]](self.tr(e["x"], st, env, old))
        if k == "to_text":
            value = self.tr(e["x"], st, env, old)
            magnitude = z3.IntToStr(z3.If(value < 0, -value, value))
            return z3.If(value < 0, z3.Concat(z3.StringVal("-"), magnitude), magnitude)
        if k == "text_length":
            return z3.Length(self.tr(e["x"], st, env, old))
        if k == "contains":
            return z3.Contains(self.tr(e["l"], st, env, old), self.tr(e["r"], st, env, old))
        if k == "starts_with":
            return z3.PrefixOf(self.tr(e["r"], st, env, old), self.tr(e["l"], st, env, old))
        if k == "ends_with":
            return z3.SuffixOf(self.tr(e["r"], st, env, old), self.tr(e["l"], st, env, old))
        if k == "mod":
            return self.tr(e["x"], st, env, old) % e["d"]["v"]
        if k == "substring":
            text = self.tr(e["text"], st, env, old)
            start = self.tr(e["start"], st, env, old)
            length = self.tr(e["length"], st, env, old)
            return z3.SubString(text, start, length)
        if k == "replace_first":
            text = self.tr(e["text"], st, env, old)
            search = self.tr(e["search"], st, env, old)
            replacement = self.tr(e["replacement"], st, env, old)
            return z3.If(search == z3.StringVal(""), text, z3.Replace(text, search, replacement))
        if k == "old":
            if old is None:
                raise ValueError("old() requires a previous state")
            return self.tr(e["x"], old, env, None)
        if k == "un":
            return z3.Not(self.tr(e["x"], st, env, old))
        if k == "if":
            return z3.If(self.tr(e["c"], st, env, old), self.tr(e["t"], st, env, old), self.tr(e["f"], st, env, old))
        if k in ("forall", "exists"):
            v = z3.Int(f"{e['v']}!{next(self._uid)}")
            env2 = {**env, e["v"]: v}
            body = self.tr(e["body"], st, env2, old)
            if e["e"] is not None:
                g = z3.Select(st[f"{e['e']}.exists"], v)
                body = z3.Implies(g, body) if k == "forall" else z3.And(g, body)
            return z3.ForAll([v], body) if k == "forall" else z3.Exists([v], body)
        if k == "bin":
            l = self.tr(e["l"], st, env, old)
            r = self.tr(e["r"], st, env, old)
            op = e["op"]
            return {
                "and": lambda: z3.And(l, r), "or": lambda: z3.Or(l, r), "=>": lambda: z3.Implies(l, r),
                "+": lambda: l + r, "-": lambda: l - r, "*": lambda: l * r, "==": lambda: l == r, "!=": lambda: l != r,
                "<": lambda: l < r, "<=": lambda: l <= r, ">": lambda: l > r, ">=": lambda: l >= r,
            }[op]()
        raise ValueError(f"unsupported expression node in verifier: {k}")

    # ------------------------------------------------------------ type invariants
    def type_invs(self, st):
        out = {}
        for e, info in self.entities.items():
            for a in info["attrs"]:
                arr = st[f"{e}.{a['name']}"]
                k = z3.Int(f"k!{next(self._uid)}")
                if a["type"] == "nat":
                    out[f"{e}.{a['name']}"] = z3.ForAll([k], z3.Select(arr, k) >= 0)
                elif a["type"].startswith("enum:"):
                    n = len(self.enums[a["type"][5:]])
                    out[f"{e}.{a['name']}"] = z3.ForAll([k], z3.And(z3.Select(arr, k) >= 0, z3.Select(arr, k) < n))
        return out

    def param_constraints(self, params, args):
        cs = []
        for p, a in zip(params, args):
            if p["type"] == "nat":
                cs.append(a >= 0)
            elif p["type"].startswith("enum:"):
                cs.append(z3.And(a >= 0, a < len(self.enums[p["type"][5:]])))
        return cs

    # ------------------------------------------------------------ transition
    def transition(self, name, st, args):
        if name in self.comps:
            c = self.comps[name]
            env = {p["name"]: a for p, a in zip(c["params"], args)}
            cur = st
            rej = z3.BoolVal(False)
            for call in c["calls"]:
                cargs = [self.tr(a, cur, env) for a in call["args"]]
                g = self.tr(call["guard"], cur, env) if call.get("guard") is not None else None
                if c.get("atomic"):
                    r = self.rejected(call["op"], cur, cargs)
                    rej = z3.Or(rej, r if g is None else z3.And(g, r))
                nxt = self.transition(call["op"], cur, cargs)
                cur = nxt if g is None else {k: (v if v.eq(cur[k]) else z3.If(g, v, cur[k])) for k, v in nxt.items()}
            if c.get("atomic"):
                return {k: z3.If(rej, st[k], v) for k, v in cur.items()}
            return cur
        op = self.ops[name]
        env = {p["name"]: a for p, a in zip(op["params"], args)}
        conds, effects = [], []
        for c in op["cases"]:
            cond = z3.BoolVal(True) if c["cond"] is None else self.tr(c["cond"], st, env)
            cur = dict(st)
            for s in c["stmts"]:
                if s.get("each"):
                    # for each x in E where c: evaluate the filter against the previous state and encode it as an array lambda
                    x = z3.Int(f"each!{next(self._uid)}")
                    env_x = {**env, s["each"]["var"]: x}
                    hit = z3.And(z3.Select(st[f"{s['entity']}.exists"], x), self.tr(s["each"]["cond"], st, env_x))
                    if s["kind"] == "delete":
                        ex_ = f"{s['entity']}.exists"
                        cur[ex_] = z3.Lambda([x], z3.And(z3.Select(cur[ex_], x), z3.Not(hit)))
                    else:
                        a = f"{s['entity']}.{s['attr']}"
                        cur[a] = z3.Lambda([x], z3.If(hit, self.tr(s["value"], st, env_x), z3.Select(cur[a], x)))
                    continue
                key = self.tr(s["key"], st, env)
                if s["kind"] == "delete":
                    cur[f"{s['entity']}.exists"] = z3.Store(cur[f"{s['entity']}.exists"], key, z3.BoolVal(False))
                elif s["kind"] == "set":
                    a = f"{s['entity']}.{s['attr']}"
                    cur[a] = z3.Store(cur[a], key, self.tr(s["value"], st, env))
                else:
                    cur[f"{s['entity']}.exists"] = z3.Store(cur[f"{s['entity']}.exists"], key, z3.BoolVal(True))
                    for i in s["inits"]:
                        a = f"{s['entity']}.{i['attr']}"
                        cur[a] = z3.Store(cur[a], key, self.tr(i["value"], st, env))
            conds.append(cond)
            effects.append(cur)
        out = {}
        for key, base in st.items():
            if all(eff[key].eq(base) for eff in effects):
                out[key] = base
                continue
            expr = base
            for cond, eff in reversed(list(zip(conds, effects))):
                expr = z3.If(cond, eff[key], expr)
            out[key] = expr
        return out

    def rejected(self, name, st, args):
        """The first matching case is a rejection: it has an observable outcome and no effect."""
        op = self.ops[name]
        env = {p["name"]: a for p, a in zip(op["params"], args)}
        out, earlier = z3.BoolVal(False), z3.BoolVal(False)
        for c in op["cases"]:
            cond = z3.BoolVal(True) if c["cond"] is None else self.tr(c["cond"], st, env)
            if c["outcome"] is not None and not c["stmts"] and not c.get("success"):
                out = z3.Or(out, z3.And(z3.Not(earlier), cond))
            earlier = z3.Or(earlier, cond)
        return out

    def signature(self, name):
        return (self.comps[name] if name in self.comps else self.ops[name])["params"]

    def fresh_params(self, name):
        sorts = {"bool": z3.BoolSort(), "text": z3.StringSort(), "dec": z3.RealSort()}
        return [z3.Const(f"p_{name}_{p['name']}", sorts.get(p["type"], z3.IntSort()))
                for p in self.signature(name)]

    # ------------------------------------------------------------ solving
    def _h(self, obj) -> str:
        return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:24]

    def _schema(self, *objs):
        """Schema dependencies are tracked per attribute, not per entity. Adding an attribute that the goal and
        operation do not mention does not invalidate the proof (additional symbols and hypotheses preserve validity); changing the type or
        ghost status of a referenced attribute does (v11)."""
        attrs, ents = set(), set()
        def walk(x):
            if isinstance(x, dict):
                if "e" in x and isinstance(x["e"], str):
                    ents.add(x["e"])
                    if isinstance(x.get("a"), str):
                        attrs.add((x["e"], x["a"]))
                if isinstance(x.get("entity"), str):
                    ents.add(x["entity"])
                    for i in x.get("inits") or []:
                        if isinstance(i, dict) and isinstance(i.get("attr"), str):
                            attrs.add((x["entity"], i["attr"]))
                for v in x.values():
                    walk(v)
            elif isinstance(x, (list, tuple)):
                for v in x:
                    walk(v)
            elif isinstance(x, str):
                for m in re.finditer(r"\b([A-Z]\w*)\.(\w+)", x):
                    ents.add(m.group(1)); attrs.add((m.group(1), m.group(2)))
        walk(list(objs))
        out = {}
        for e in sorted(ents & set(self.entities)):
            ent = self.entities[e]
            al = ent.get("attrs", []) if isinstance(ent, dict) else []
            out[e] = {"block": ent.get("block") if isinstance(ent, dict) else ent,
                      "attrs": [x for x in al if (e, x.get("name")) in attrs]}
        return out

    def init_conjuncts(self, init):
        """Represent the initial state as a set of conjuncts (forall k: A and B == (forall k: A) and (forall k: B)); An initial state
        with additional conjuncts implies the previous one, so a previous INIT proof remains valid."""
        if init is None:
            return ["<none>"]
        parts, stack = [], [init["body"] if init.get("k") == "forall" else init]
        while stack:
            x = stack.pop()
            if x.get("k") == "bin" and x.get("op") == "and":
                stack += [x["l"], x["r"]]
            else:
                parts.append(x)
        q = {k: v for k, v in init.items() if k != "body"} if init.get("k") == "forall" else {}
        return sorted({self._h([q, x]) for x in parts})

    def hyp_ids(self):
        return sorted({self._h(p["expr"]) for p in self.invs})

    def modular_deps(self, name, obligations):
        """Modular discharge of PRES/TYPE for a compose: if each step calls an operation (or verified compose) with
        PRES and TYPE obligations, the compose inherits them conditionally (conditional_on). This is sound because each step is proven
        for arbitrary arguments under all invariants; a guard selects between the step state and
        the previous state, and an atomic rejection returns the initial state. This does not apply when a step requires a new key (generated_for)."""
        deps = []
        for call in self.comps[name]["calls"]:
            op = call["op"]
            if op not in self.ops and op not in self.comps:
                return None
            if any(p.get("generated_for") for p in self.signature(op)):
                return None
            mine = [o["id"] for o in obligations if o["operation"] == op and o["kind"] in ("PRES", "TYPE")]
            have_pres = {o["rule"] for o in obligations if o["operation"] == op and o["kind"] == "PRES"}
            if any(q["id"] not in have_pres for q in self.invs):
                return None
            deps += mine
        return sorted(set(deps)) or None

    def child_proof(self, op_name, kind, rule, reads):
        """Reuse a verified component proof: PRES/TYPE for an operation imported from `alias` can use the child obligation
        when it is PROVEN and, in the parent, only operations from the same component write the state read by the rule and operation
        (so parent-reachable states project to child-reachable states)."""
        op = self.ops.get(op_name)
        alias = (op.get("block") or "").split("=")[0] if op and "=" in (op.get("block") or "") else None
        if not alias or alias not in self.children or not op_name.startswith(alias + "_"):
            return None
        if not hasattr(self, "_writers"):
            self._writers = {}
            for n, o in self.ops.items():
                a = (o.get("block") or "").split("=")[0] if "=" in (o.get("block") or "") else None
                for c in o["cases"]:
                    for s_ in c["stmts"]:
                        self._writers.setdefault(s_["entity"], set()).add(a)
        ents = {r.split(".")[0] for r in reads | state_reads(op["cases"])}
        ents |= {s_["entity"] for c in op["cases"] for s_ in c["stmts"]}
        if any(self._writers.get(e, set()) - {alias} for e in ents):
            return None
        oid = f"{kind}:{rule}@{op_name[len(alias) + 1:]}"
        return f"{alias}/{oid}" if self.children[alias].get(oid) == "PROVEN" else None

    def frame_proof(self):
        """Frame proof: the operation leaves every state symbol read by the invariant unchanged, so its translation after the
        operation is the same term as before, and that term is an assumption. The goal is syntactically an assumption, so no
        solver is needed. Count this separately (proof_cache.frame) from solver proofs (v13)."""
        self.cache_stats["frame"] = self.cache_stats.get("frame", 0) + 1
        return "PROVEN", None, "frame: the operation leaves the rule inputs unchanged (goal identical to an assumption)", 0

    def csolve(self, payload, hyp, assumptions, goal, uses_sum):
        """Proof cache: reuse a PROVEN obligation when the goal, operation, and referenced schema are identical
        and the earlier hypotheses are a subset of the current hypotheses (adding hypotheses does not invalidate a proof)."""
        if self.cache is None or payload is None:
            return self.solve(assumptions, goal, uses_sum)
        key = self._h([payload, self._schema(payload), uses_sum])
        hit = self.cache.get(key)
        if hit and set(hit["hyp"]) <= set(hyp):
            self.cache_stats["reused"] += 1
            return "PROVEN", None, "reused-from-cache", 0
        st, model, reason, ms = self.solve(assumptions, goal, uses_sum)
        self.cache_stats["fresh"] += 1
        if st == "PROVEN":
            self.cache[key] = {"hyp": list(hyp)}
        return st, model, reason, ms

    @staticmethod
    def _syms(e) -> set:
        out, seen, stack = set(), set(), [e]
        while stack:
            x = stack.pop()
            i = x.get_id()
            if i in seen:
                continue
            seen.add(i)
            if z3.is_const(x) and x.decl().kind() == z3.Z3_OP_UNINTERPRETED:
                out.add(x.decl().name())
            elif z3.is_quantifier(x):
                stack.append(x.body())
            elif z3.is_app(x):
                stack.extend(x.children())
        return out

    def relevant(self, assumptions, goal):
        g = self._syms(goal)
        return [a for a in assumptions if self._syms(a) & g]

    def norm_axioms(self, terms):
        """Instantiate normalization axioms at each use (without quantifiers): promised properties
        (length, boundaries, digits, idempotence) are assumed; the generator checks against the reference model that the function matches the
        catalog."""
        names = {f.name(): op for op, f in self.NORM.items()}
        apps, seen, stack = {}, set(), list(terms)
        while stack:
            x = stack.pop()
            i = x.get_id()
            if i in seen:
                continue
            seen.add(i)
            if z3.is_quantifier(x):
                stack.append(x.body())
            elif z3.is_app(x):
                if x.decl().name() in names and x.num_args() == 1:
                    apps[x.get_id()] = (names[x.decl().name()], x.arg(0))
                stack.extend(x.children())
        out, done = [], set()
        pending = [(op, arg, 0) for op, arg in apps.values()]
        while pending:
            op, arg, depth = pending.pop()
            if (op, arg.get_id()) in done:
                continue
            done.add((op, arg.get_id()))
            f = self.NORM[op]
            y = f(arg)
            ws = [z3.StringVal(c) for c in (" ", "\t", "\n", "\r")]
            edge = lambda s: z3.Or(*[z3.PrefixOf(c, s) for c in ws], *[z3.SuffixOf(c, s) for c in ws])
            digits = z3.Star(z3.Range("0", "9"))
            if op == "trim":
                out += [z3.Length(y) <= z3.Length(arg), z3.Contains(arg, y), z3.Not(edge(y)), z3.Implies(z3.Not(edge(arg)), y == arg)]
            elif op == "digits_only":
                out += [z3.Length(y) <= z3.Length(arg), z3.InRe(y, digits), z3.Implies(z3.InRe(arg, digits), y == arg)]
            else:
                out += [z3.Length(y) == z3.Length(arg)]
            out.append(f(y) == y)
            if depth == 0:
                pending.append((op, y, 1))
        return out

    def solve(self, assumptions, goal, uses_sum):
        self.cache_stats["solver"] += 1
        assumptions = list(assumptions) + self.norm_axioms(list(assumptions) + [goal])
        s = z3.Solver()
        s.set("timeout", self.timeout)
        if uses_sum:
            m, k, v = z3.Const("m!ax", z3.ArraySort(z3.IntSort(), z3.IntSort())), z3.Int("k!ax"), z3.Int("v!ax")
            s.add(z3.ForAll([m, k, v], self.SUM(z3.Store(m, k, v)) == self.SUM(m) - z3.Select(m, k) + v))
        for a in assumptions:
            s.add(a)
        s.add(z3.Not(goal))
        t0 = time.perf_counter()
        r = s.check()
        ms = round((time.perf_counter() - t0) * 1000)
        if r == z3.unknown and RELEVANCE and len(assumptions) > 1:
            # Retry with hypotheses that share symbols with the goal; removing hypotheses does not invalidate a proof.
            keep = self.relevant(assumptions, goal)
            if len(keep) < len(assumptions):
                s = z3.Solver()
                s.set("timeout", self.timeout)
                if uses_sum:
                    s.add(z3.ForAll([m, k, v], self.SUM(z3.Store(m, k, v)) == self.SUM(m) - z3.Select(m, k) + v))
                for a in keep:
                    s.add(a)
                s.add(z3.Not(goal))
                t1 = time.perf_counter()
                r2 = s.check()
                ms += round((time.perf_counter() - t1) * 1000)
                if r2 == z3.unsat:
                    return "PROVEN", None, "relevance: proven using hypotheses that share state with the goal", ms
        if r == z3.unsat:
            return "PROVEN", None, None, ms
        if r == z3.sat:
            return "REFUTED", s.model(), None, ms
        return "UNKNOWN", None, s.reason_unknown(), ms

    def render_model(self, model, st0, st1, params, names):
        def val(x):
            return str(model.eval(x, model_completion=True))
        ce = {"params": {n: val(p) for n, p in zip(names, params)}}
        pre = {k: val(v) for k, v in st0.items()}
        ce["pre_state"] = {k: v for k, v in pre.items() if len(v) < 400}
        if st1 is not None:
            post = {k: val(v) for k, v in st1.items()}
            ce["post_state_changed"] = {k: v for k, v in post.items() if v != pre.get(k) and len(v) < 400}
        return ce

    # ------------------------------------------------------------ Obligations
    def run(self) -> dict:
        obligations: list[dict] = []
        any_sum = any(has_node(p["expr"], "sum") for p in self.props if p.get("expr"))
        inv_sum = any(has_node(p["expr"], "sum") for p in self.invs)

        def record(oid, rule, kind, op, desc, status, model_info, reason, ms, sums, depends=None, inherited=False):
            ob = {"id": oid, "rule": rule, "kind": kind, "operation": op, "description": desc,
                  "status": status, "time_ms": ms, "stdlib_axioms": ["AX_SUM_STORE", "AX_FINITE_SUPPORT"] if sums else [],
                  "inherited_from": None}
            if depends:
                ob["conditional_on"] = depends
            if reason:
                ob["reason"] = reason
            if model_info:
                ob["counterexample"] = model_info
            obligations.append(ob)
            return ob

        s0 = self.fresh_state("s0")
        ty0 = self.type_invs(s0)
        inv0 = {p["id"]: self.tr(p["expr"], s0, {}) for p in self.invs}

        # --- INIT
        init = self.ir.get("initial")
        init_assume = list(ty0.values()) + ([self.tr(init, s0, {})] if init is not None else [])
        for p in self.invs:
            st, model, reason, ms = self.csolve(["INIT", p["expr"]], self.init_conjuncts(init), init_assume, inv0[p["id"]],
                                                inv_sum and has_node(p["expr"], "sum"))
            ce = self.render_model(model, s0, None, [], []) if model else None
            record(f"INIT:{p['id']}", p["id"], "INIT", None, f"the initial state establishes {p['id']}", st, ce, reason, ms,
                   has_node(p["expr"], "sum"))

        # --- per operation
        names = list(self.ops) + list(self.comps)
        for name in names:
            sig = self.signature(name)
            params = self.fresh_params(name)
            pnames = [p["name"] for p in sig]
            pc = self.param_constraints(sig, params) + [
                z3.Not(z3.Select(s0[f"{p['generated_for']}.exists"], a))
                for p, a in zip(sig, params) if p.get("generated_for")]
            s1 = self.transition(name, s0, params)
            # Prove enum ranges (TYPE) rather than assuming them; an extra quantifier makes cardinality bounds UNKNOWN.
            base = pc + [f for k_, f in ty0.items() if k_ not in self.enum_attrs] + list(
                {self._h(p["expr"]): inv0[p["id"]] for p in self.invs}.values())
            is_comp = name in self.comps
            opj = None if is_comp else self.ops[name]
            hyp = self.hyp_ids()

            ty1 = self.type_invs(s1)
            base_t = base + [f for k_, f in ty0.items() if k_ in self.enum_attrs]
            mod = self.modular_deps(name, obligations) if is_comp and MODULAR else None
            for label, f in ty1.items():
                if mod is not None and not (FRAME and s1[label].eq(s0[label])):
                    record(f"TYPE:{label}@{name}", f"TYPE:{label}", "TYPE", name, f"{label} preserves its type after {name}",
                           "PROVEN", None, MODULAR_REASON, 0, inv_sum, mod)
                    continue
                ch = None if is_comp or (FRAME and s1[label].eq(s0[label])) else self.child_proof(name, "TYPE", label, {label})
                if ch:
                    record(f"TYPE:{label}@{name}", f"TYPE:{label}", "TYPE", name, f"{label} preserves its type after {name}",
                           "PROVEN", None, "inherited: proven in the component", 0, inv_sum)["inherited_from"] = ch
                    continue
                st, model, reason, ms = (self.frame_proof() if FRAME and s1[label].eq(s0[label]) else
                                         self.csolve(opj and ["TYPE", label, opj], hyp, base_t, f, inv_sum))
                record(f"TYPE:{label}@{name}", f"TYPE:{label}", "TYPE", name,
                       f"{label} preserves its type after {name}", st,
                       self.render_model(model, s0, s1, params, pnames) if model else None, reason, ms, inv_sum)
            groups: dict = {}
            for p in self.invs:
                groups.setdefault(self._h(p["expr"]), []).append(p)
            for p in self.invs:
                if mod is not None and not (FRAME and all(s1[k].eq(s0[k]) for k in state_reads(p["expr"]))):
                    record(f"PRES:{p['id']}@{name}", p["id"], "PRES", name, f"{name} preserves {p['id']}",
                           "PROVEN", None, MODULAR_REASON, 0, inv_sum, mod)
                    continue
                framed = FRAME and all(s1[k].eq(s0[k]) for k in state_reads(p["expr"]))
                same = groups[self._h(p["expr"])]
                first = same[0]
                if first is not p and not framed:
                    # The same rule was imported through another path: same formula, operation, and obligation.
                    prev = next(o for o in reversed(obligations) if o["id"] == f"PRES:{first['id']}@{name}")
                    ob = record(f"PRES:{p['id']}@{name}", p["id"], "PRES", name, f"{name} preserves {p['id']}",
                                prev["status"], prev.get("counterexample"),
                                f"duplicate: same formula as {first['id']}" + (f" ({prev['reason']})" if prev.get("reason") else ""),
                                0, inv_sum)
                    ob["duplicate_of"] = prev["id"]
                    ob["inherited_from"] = prev.get("inherited_from")
                    continue
                ch = None
                if not (is_comp or framed):
                    for q in same:
                        alias = (q.get("block") or "").split("=")[0]
                        if q["id"].startswith(alias + "_"):
                            ch = self.child_proof(name, "PRES", q["id"][len(alias) + 1:], state_reads(q["expr"]))
                            if ch and ch.startswith(alias + "/"):
                                break
                            ch = None
                if ch:
                    record(f"PRES:{p['id']}@{name}", p["id"], "PRES", name, f"{name} preserves {p['id']}",
                           "PROVEN", None, "inherited: proven in the component", 0, inv_sum)["inherited_from"] = ch
                    continue
                f = self.tr(p["expr"], s1, {})
                sm = inv_sum
                st, model, reason, ms = (self.frame_proof() if framed else
                                         self.csolve(opj and ["PRES", p["expr"], opj], hyp, base, f, sm))
                record(f"PRES:{p['id']}@{name}", p["id"], "PRES", name, f"{name} preserves {p['id']}", st,
                       self.render_model(model, s0, s1, params, pnames) if model else None, reason, ms, sm)
            for p in self.laws:
                env = {}
                f = self.tr(p["expr"], s1, env, s0)
                sm = inv_sum or has_node(p["expr"], "sum")
                st, model, reason, ms = self.csolve(opj and ["LAW", p["expr"], opj], hyp, base, f, sm)
                record(f"LAW:{p['id']}@{name}", p["id"], "LAW", name, f"{name} satisfies {p['id']}", st,
                       self.render_model(model, s0, s1, params, pnames) if model else None, reason, ms, sm)
            for p in self.ens:
                if p["targets"][0] != name:
                    continue
                env = {pp["name"]: a for pp, a in zip(sig, params)}
                f = self.tr(p["expr"], s1, env, s0)
                sm = inv_sum or has_node(p["expr"], "sum")
                st, model, reason, ms = self.csolve(opj and ["ENS", p["expr"], opj], hyp, base, f, sm)
                record(f"ENS:{p['id']}@{name}", p["id"], "ENS", name, f"{name} satisfies postcondition {p['id']}", st,
                       self.render_model(model, s0, s1, params, pnames) if model else None, reason, ms, sm)
            if is_comp:
                continue
            for p in self.reps:
                if name not in p["targets"]:
                    continue
                s2 = self.transition(name, s1, params)
                inv1 = [self.tr(q["expr"], s1, {}) for q in self.invs]
                goal = z3.And(*[s2[k].eq(s1[k]) if s2[k].eq(s1[k]) else s2[k] == s1[k] for k in s0])
                st, model, reason, ms = self.solve(base + inv1 + list(self.type_invs(s1).values()), goal, inv_sum)
                depends = [f"PRES:{q['id']}@{name}" for q in self.invs] + [f"TYPE:{k}@{name}" for k in ty1]
                record(f"REPEAT:{p['id']}@{name}", p["id"], "REPEAT", name,
                       f"applying {name} twice is equivalent to applying it once", st,
                       self.render_model(model, s0, s2, params, pnames) if model else None, reason, ms, inv_sum,
                       depends)

        self._propagate_conditionals(obligations)
        return self._report(obligations)

    @staticmethod
    def _propagate_conditionals(obligations):
        by_id = {o["id"]: o for o in obligations}
        for o in obligations:
            deps = o.get("conditional_on")
            if not deps or o["status"] != "PROVEN":
                continue
            bad = [d for d in deps if by_id.get(d, {}).get("status") != "PROVEN"]
            if bad:
                o["status"] = "UNKNOWN"
                o["reason"] = f"depends on unproven obligations: {bad}"

    # ------------------------------------------------------------ informe
    def _report(self, obligations):
        rank = {"PROVEN": 0, "UNKNOWN": 1, "REFUTED": 2}
        rules = []
        notes = {n["id"]: n for k in ("assumptions",) for n in self.ir["notes"][k]}
        init_origin = self.ir.get("initial_origin")
        for p in self.props:
            obs = [o for o in obligations if o["rule"] == p["id"]]
            if p["kind"] == "invariant":
                obs += []
            status = "PROVEN"
            for o in obs:
                if rank[o["status"]] > rank[status]:
                    status = o["status"]
            if not obs:
                status = "UNKNOWN"
            axioms = sorted({a for o in obs for a in o["stdlib_axioms"]})
            user = []
            if p["origin"] and p["origin"]["kind"] == "assumption":
                user.append(p["origin"]["ref"])
            if p["kind"] == "invariant" and init_origin and init_origin["kind"] == "assumption":
                user.append(init_origin["ref"])
            rules.append({"rule": p["id"], "kind": p["kind"], "group": p["group"], "text": p["text"],
                          "origin": p["origin"], "status": status, "obligations": [o["id"] for o in obs],
                          "stdlib_axioms": axioms, "user_assumptions": sorted(set(user)),
                          "inherited": False})
        type_obs = [o for o in obligations if o["rule"].startswith("TYPE:")]
        if type_obs:
            st = max((o["status"] for o in type_obs), key=lambda s: rank[s])
            rules.append({"rule": "TYPES", "kind": "type_invariants", "group": "language",
                          "text": "type invariants: Natural >= 0 and enum range", "origin": None,
                          "status": st, "obligations": [o["id"] for o in type_obs],
                          "stdlib_axioms": ["AX_TYPED_INITIAL"],
                          "user_assumptions": [], "inherited": False})
        used_axioms = sorted({a for o in obligations for a in o["stdlib_axioms"]}
                             | ({"AX_TYPED_INITIAL"} if type_obs or self.type_invs(self.fresh_state("x")) else set())
                         | ({"AX_FILE_WRITE"} if any(s.get("file") for o in self.ir["operations"] for cs in o["cases"] for s in cs["stmts"]) else set()))
        assumptions = [{"id": a, **STDLIB_AXIOMS[a]} for a in used_axioms]
        for e in self.ir.get("externals", []):
            for o in e["operations"]:
                assumptions.append({
                    "id": f"AX_EXT_{e['name']}_{o['name']}", "source": "external-contract",
                    "text": f"the real service {e['name']}.{o['name']} satisfies the declared contract: it returns one of "
                            f"{[x['name'] for x in o['outcomes']]} and produces the effects declared for each outcome "
                            f"(uncertain outcomes have no modeled effect)",
                    "evidence": f"contract {e['name']}@{e['version']} source {e['source']} {e['hash']}; "
                                f"not provable: checked at layer 2/3 (contract monitor)"})
        referenced = sorted({u for r in rules for u in r["user_assumptions"]})
        for aid in referenced:
            n = notes.get(aid)
            assumptions.append({"id": aid, "text": n["text"] if n else "", "source": "user",
                                "owner": n["owner"] if n else "", "evidence": "declared in the UPA program"})
        summary = {s: sum(1 for o in obligations if o["status"] == s) for s in ("PROVEN", "REFUTED", "UNKNOWN")}
        verdict = "VERIFIED" if summary["REFUTED"] == 0 and summary["UNKNOWN"] == 0 else (
            "REFUTED" if summary["REFUTED"] else "INCOMPLETE")
        errors = [self._error(o, rules) for o in obligations if o["status"] != "PROVEN"]
        return {
            "tool": {"backend": "z3", "version": z3.get_version_string()},
            "ir_hash": ir_hash(self.ir),
            "timeout_ms": self.timeout,
            "verdict": verdict,
            "summary": summary,
            "summary_by_category": self.by_category(obligations),
            "obligations": obligations,
            "rules": rules,
            "assumptions": assumptions,
            "errors": errors,
        }

    @staticmethod
    def by_category(obligations):
        """Totals mix business guarantees with technical and type obligations. Report them separately so a change in implementation
        is not mistaken for an increase or decrease in business guarantees."""
        out = {}
        for o in obligations:
            r = o.get("rule") or ""
            cat = ("type" if o["id"].startswith("TYPE:") else "business" if r.split(":")[-1].startswith("BR_")
                   else "technical" if r.split(":")[-1].startswith("TR_") else "other")
            d = out.setdefault(cat, {"PROVEN": 0, "REFUTED": 0, "UNKNOWN": 0})
            d[o["status"]] += 1
        return out

    def _error(self, ob, rules):
        rule = next((r for r in rules if r["rule"] == ob["rule"]), None)
        literal = [p["id"] for p in self.props if p["origin"] and p["origin"]["kind"] == "literal"]
        op = ob.get("operation")
        scope = []
        if op:
            node = self.ops.get(op)
            scope.append({"operation": op, "cases": len(node["cases"]) if node else None,
                          "hint": ("fix this operation's guards or assignments; if the counterexample's prior state is "
                                   "unreachable, strengthen an invariant rather than weakening the rule")})
        return {
            "error": "REFUTED_RULE" if ob["status"] == "REFUTED" else "UNPROVEN_RULE",
            "rule": ob["rule"],
            "property": ob["description"],
            "obligation": ob["id"],
            "reason": ob.get("reason"),
            "counterexample": ob.get("counterexample", {}),
            "modifiable": [{"operation": op}] if op else [],
            "immutable": sorted(set(literal + ([ob["rule"]] if rule and rule.get("origin") else []))),
            "suggested_repair_scope": scope,
        }


def verify(ir: dict, timeout_ms: int = 30000, cache: dict | None = None, children: dict | None = None) -> dict:
    """`children`: {alias: {obligation id: status}} for already verified components, used to inherit PRES/TYPE proofs."""
    v = Verifier(ir, timeout_ms)
    v.cache = cache
    v.children = children or {}
    r = v.run()
    r["proof_cache"] = dict(v.cache_stats)
    return r
