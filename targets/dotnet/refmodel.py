"""Reference interpreter for the IR, independent of the C# generator. Produces deterministic conformance traces."""
from __future__ import annotations
import json
import random
from pathlib import Path


class Ref:
    def __init__(self, ir):
        self.ir = ir
        self.enums = ir["enums"]
        self.ops = {o["name"]: o for o in ir["operations"]}
        self.comps = {c["name"]: c for c in ir["composes"]}
        self.texts = list(ir.get("texts", [""]))
        self.text_ids = {text: i for i, text in enumerate(self.texts)}
        self.attr_types = {
            (e, a["name"]): a["type"]
            for e, info in ir["entities"].items()
            for a in info["attrs"]
        }

    def text_value(self, text_id):
        return self.texts[text_id] if 0 <= text_id < len(self.texts) else ""

    def intern_text(self, text):
        if text not in self.text_ids:
            self.text_ids[text] = len(self.texts)
            self.texts.append(text)
        return self.text_ids[text]

    def empty(self):
        st = {}
        for e, info in self.ir["entities"].items():
            st[f"{e}.exists"] = {}
            for a in info["attrs"]:
                st[f"{e}.{a['name']}"] = {}
        return st

    def get(self, st, key, k):
        return st[key].get(k, 0)

    def ev(self, e, st, env):
        k = e["k"]
        if k == "int":
            return e["v"]
        if k == "bool":
            return 1 if e["v"] else 0
        if k == "str":
            return self.texts.index(e["v"])
        if k == "concat":
            left = self.text_value(self.ev(e["l"], st, env))
            right = self.text_value(self.ev(e["r"], st, env))
            return self.intern_text(left + right)
        if k == "norm":
            text = self.text_value(self.ev(e["x"], st, env))
            one = lambda f: "".join(c2 if len(c2 := f(c)) == 1 else c for c in text)
            result = {
                "trim": lambda: text.strip(" \t\n\r"),
                "upper": lambda: one(str.upper),
                "lower": lambda: one(str.lower),
                "digits_only": lambda: "".join(c for c in text if "0" <= c <= "9"),
            }[e["op"]]()
            return self.intern_text(result)
        if k == "to_text":
            return self.intern_text(str(self.ev(e["x"], st, env)))
        if k == "text_length":
            return len(self.text_value(self.ev(e["x"], st, env)))
        if k in ("contains", "starts_with", "ends_with"):
            left = self.text_value(self.ev(e["l"], st, env))
            right = self.text_value(self.ev(e["r"], st, env))
            result = {
                "contains": lambda: right in left,
                "starts_with": lambda: left.startswith(right),
                "ends_with": lambda: left.endswith(right),
            }[k]()
            return 1 if result else 0
        if k == "mod":
            return self.ev(e["x"], st, env) % e["d"]["v"]
        if k == "substring":
            text = self.text_value(self.ev(e["text"], st, env))
            start = self.ev(e["start"], st, env)
            length = self.ev(e["length"], st, env)
            if start < 0 or length <= 0 or start >= len(text):
                return self.intern_text("")
            return self.intern_text(text[start:start + length])
        if k == "replace_first":
            text = self.text_value(self.ev(e["text"], st, env))
            search = self.text_value(self.ev(e["search"], st, env))
            replacement = self.text_value(self.ev(e["replacement"], st, env))
            index = text.find(search) if search else -1
            result = text if index < 0 else text[:index] + replacement + text[index + len(search):]
            return self.intern_text(result)
        if k == "var":
            return env[e["n"]]
        if k == "enum":
            return self.enums[e["e"]].index(e["v"])
        if k == "attr":
            return self.get(st, f"{e['e']}.{e['a']}", self.ev(e["key"], st, env))
        if k == "in":
            return 1 if self.ev(e["key"], st, env) in st[f"{e['e']}.exists"] else 0
        if k in ("forall", "exists"):
            dom = {key for m in st.values() for key in m} | {-(2 ** 63) + 1}
            def holds(v):
                inner = {**env, e["v"]: v}
                if k == "forall":
                    return (e["e"] and v not in st[f"{e['e']}.exists"]) or bool(self.ev(e["body"], st, inner))
                return (not e["e"] or v in st[f"{e['e']}.exists"]) and bool(self.ev(e["body"], st, inner))
            hit = all(holds(v) for v in dom) if k == "forall" else any(holds(v) for v in dom)
            return 1 if hit else 0
        if k == "un":
            return 0 if self.ev(e["x"], st, env) else 1
        if k == "if":
            return self.ev(e["t"], st, env) if self.ev(e["c"], st, env) else self.ev(e["f"], st, env)
        if k == "bin":
            op = e["op"]
            if op == "and":
                return 1 if self.ev(e["l"], st, env) and self.ev(e["r"], st, env) else 0
            if op == "or":
                return 1 if self.ev(e["l"], st, env) or self.ev(e["r"], st, env) else 0
            if op == "=>":
                return 1 if (not self.ev(e["l"], st, env)) or self.ev(e["r"], st, env) else 0
            l, r = self.ev(e["l"], st, env), self.ev(e["r"], st, env)
            return {"+": l + r, "-": l - r, "*": l * r, "==": int(l == r), "!=": int(l != r), "<": int(l < r),
                    "<=": int(l <= r), ">": int(l > r), ">=": int(l >= r)}[op]
        raise ValueError(f"unsupported node in operations: {k}")

    def rejected(self, st, name, args):
        op = self.ops[name]
        env = {p["name"]: a for p, a in zip(op["params"], args)}
        for c in op["cases"]:
            if c["cond"] is None or self.ev(c["cond"], st, env):
                return c["outcome"] is not None and not c["stmts"] and not c.get("success")
        return False

    def apply(self, st, name, args):
        if name in self.comps:
            c = self.comps[name]
            env = {p["name"]: a for p, a in zip(c["params"], args)}
            start, rej = st, False
            for call in c["calls"]:
                if call.get("guard") is not None and not self.ev(call["guard"], st, env):
                    continue
                cargs = [self.ev(a, st, env) for a in call["args"]]
                rej = rej or (bool(c.get("atomic")) and self.rejected(st, call["op"], cargs))
                st = self.apply(st, call["op"], cargs)
            return start if (c.get("atomic") and rej) else st
        op = self.ops[name]
        env = {p["name"]: a for p, a in zip(op["params"], args)}
        for c in op["cases"]:
            if c["cond"] is None or self.ev(c["cond"], st, env):
                new = {k: dict(v) for k, v in st.items()}
                for s in c["stmts"]:
                    if s.get("each"):
                        ex_ = st[f"{s['entity']}.exists"]
                        for k_ in [k for k in ex_ if ex_[k]]:
                            inner = {**env, s["each"]["var"]: k_}
                            if not self.ev(s["each"]["cond"], st, inner):
                                continue
                            if s["kind"] == "delete":
                                new[f"{s['entity']}.exists"].pop(k_, None)
                            else:
                                value = self.ev(s["value"], st, inner)
                                if self.attr_types.get((s["entity"], s["attr"])) == "bool":
                                    value = 1 if value else 0
                                new[f"{s['entity']}.{s['attr']}"][k_] = value
                        continue
                    key = self.ev(s["key"], st, env)
                    if s["kind"] == "delete":
                        new[f"{s['entity']}.exists"].pop(key, None)
                    elif s["kind"] == "set":
                        value = self.ev(s["value"], st, env)
                        if self.attr_types.get((s["entity"], s["attr"])) == "bool":
                            value = 1 if value else 0
                        new[f"{s['entity']}.{s['attr']}"][key] = value
                    else:
                        new[f"{s['entity']}.exists"][key] = 1
                        for i in s["inits"]:
                            value = self.ev(i["value"], st, env)
                            if self.attr_types.get((s["entity"], i["attr"])) == "bool":
                                value = 1 if value else 0
                            new[f"{s['entity']}.{i['attr']}"][key] = value
                return new
        return st

    @staticmethod
    def dump(st):
        return {k: {str(i): v for i, v in sorted(m.items()) if v != 0} for k, m in sorted(st.items())}


def traces(ir: dict, seed: int = 7, n_traces: int = 200, steps: int = 12) -> dict:
    """Seeded random traces. Each operation argument is generated by type and role (ids from a small pool to cause repetitions; amounts around account balances), without domain-specific knowledge."""
    rnd = random.Random(seed)
    ref = Ref(ir)
    ids = [1, 2, 3, 4]
    accounts = [101, 102, 103]
    unknown = 999
    out = []
    names = list(ref.ops) + list(ref.comps)
    for _ in range(n_traces):
        st = ref.empty()
        # initial state: entities not created by any operation are provided by the environment
        created = {s["entity"] for o in ir["operations"] for c in o["cases"] for s in c["stmts"] if s["kind"] == "create"}
        for e, info in ir["entities"].items():
            if e in created:
                continue
            for k in accounts:
                st[f"{e}.exists"][k] = 1
                for a in info["attrs"]:
                    if a["type"] == "nat" and not a["ghost"]:
                        st[f"{e}.{a['name']}"][k] = rnd.choice([0, 50, 100, 250, 1000])
        initial = ref.dump(st)
        seq = []
        for _ in range(steps):
            name = rnd.choice(names)
            sig = (ref.ops.get(name) or ref.comps[name])["params"]
            args = []
            for j, p in enumerate(sig):
                if p.get("generated_for"):
                    used = st[f"{p['generated_for']}.exists"]
                    args.append(rnd.choice([i for i in ids + list(range(5, 40)) if not used.get(i)]))
                elif p["type"] == "bool":
                    args.append(rnd.choice([0, 1]))
                elif p["type"] == "nat":
                    args.append(rnd.choice([0, 1, 40, 50, 100, 120, 250, 1000]))
                elif p["type"] == "text":
                    args.append(rnd.randrange(len(ref.texts)))
                elif p["type"].startswith("enum:"):
                    members = ref.enums[p["type"][5:]]
                    args.append(rnd.randrange(len(members)))
                elif p["name"] in ("id", "tid"):
                    args.append(rnd.choice(ids))
                else:
                    args.append(rnd.choice(accounts + [unknown]))
            st = ref.apply(st, name, args)
            seq.append({"op": name, "args": args, "state": ref.dump(st)})
        out.append({"initial": initial, "steps": seq})
    return {"seed": seed, "traces": out}


if __name__ == "__main__":
    import sys
    ir = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    Path(sys.argv[2]).write_text(json.dumps(traces(ir)), encoding="utf-8")
