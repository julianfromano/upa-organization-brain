"""Language sugar lowered to expressions already available in the IR.

  result is <Outcome>          the operation result (in `ensures`) has this value; `success` is the normal case.
  unchanged [except <items>]    state is unchanged; with `except`, only listed items may change:
                                  E          membership in E may change
                                  E[k]       ... only for key k
                                  E.a        attribute a of E may change for any key
                                  E.a[k]     ... only for key k

Macros expand after entity inference and before semantic analysis, so the verifier, C# generator,
and reference model do not need to know these nodes."""
from __future__ import annotations
import copy

from .errors import ErrorBag

SUCCESS = "success"


def _var(n):
    return {"k": "var", "n": n}


def _bin(op, l, r):
    return {"k": "bin", "op": op, "l": l, "r": r}


def _and(parts):
    out = None
    for p in parts:
        out = p if out is None else _bin("and", out, p)
    return out if out is not None else {"k": "bool", "v": True}


def _or(parts):
    out = None
    for p in parts:
        out = p if out is None else _bin("or", out, p)
    return out if out is not None else {"k": "bool", "v": False}


def _old(x):
    return {"k": "old", "x": x}


def _not(x):
    return {"k": "un", "op": "not", "x": x}


def _forall(body, v="_k"):
    return {"k": "forall", "e": None, "v": v, "body": body}


def result_expr(op, name: str, errors: ErrorBag, pos) -> dict:
    names = {c.outcome for c in op.cases if c.outcome}
    if name != SUCCESS and name not in names:
        errors.add("RESULT_UNKNOWN", f"{op.name} has no outcome '{name}' (available: {sorted(names) or 'none'}; "
                   f"the normal case is '{SUCCESS}')", pos)
        return {"k": "bool", "v": False}
    earlier: list[dict] = []
    hits: list[dict] = []
    for c in op.cases:
        cond = c.cond if c.cond is not None else {"k": "bool", "v": True}
        first = _and([_not(copy.deepcopy(e)) for e in earlier] + [copy.deepcopy(cond)])
        if (c.outcome or SUCCESS) == name:
            hits.append(first)
        earlier.append(cond)
    if name == SUCCESS:
        hits.append(_and([_not(copy.deepcopy(e)) for e in earlier]))
    return _old(_or(hits))


def frame_expr(spec, free: list[dict], errors: ErrorBag, pos) -> dict:
    ents = {e.name: e for e in spec.entities}
    items = {}
    for f in free:
        if f["e"] not in ents:
            errors.add("UNKNOWN_ENTITY", f"unchanged except: unknown entity '{f['e']}'", pos)
            continue
        if f["a"] is not None and f["a"] not in {a.name for a in ents[f["e"]].attrs}:
            errors.add("UNKNOWN_ATTR", f"unchanged except: {f['e']} has no attribute '{f['a']}'", pos)
            continue
        items.setdefault((f["e"], f["a"]), []).append(f["key"])
    parts = []
    k = _var("_k")
    for name, ent in ents.items():
        def guard(keys, base=None):
            conds = [] if base is None else [base]
            for key in keys:
                if key is not None:
                    conds.append(_bin("!=", k, _old(copy.deepcopy(key))))
            return conds
        mem = items.get((name, None))
        if mem is None or all(x is not None for x in mem):
            keys = mem or []
            inn = {"k": "in", "e": name, "key": k}
            body = _and([_bin("=>", inn, _old(copy.deepcopy(inn))), _bin("=>", _old(copy.deepcopy(inn)), inn)])
            parts.append(_forall(_bin("=>", _and(guard(keys)), body) if keys else body))
        for a in ent.attrs:
            if a.ghost:
                continue
            slot = items.get((name, a.name))
            if slot is not None and any(x is None for x in slot):
                continue
            pre = _old({"k": "in", "e": name, "key": k})
            eq = _bin("==", {"k": "attr", "e": name, "a": a.name, "key": k}, _old({"k": "attr", "e": name, "a": a.name, "key": k}))
            parts.append(_forall(_bin("=>", _and(guard(slot or [], pre)), eq)))
    return _and(parts)


def expand_macros(spec, errors: ErrorBag) -> None:
    ops = {o.name: o for o in spec.operations}

    def walk(x, prop):
        if isinstance(x, list):
            return [walk(v, prop) for v in x]
        if not isinstance(x, dict):
            return x
        k = x.get("k")
        if k == "result_is":
            op = ops.get(prop.targets[0]) if prop.kind == "ensures" and prop.targets else None
            if op is None:
                errors.add("RESULT_OUTSIDE_ENSURES", "'result is' is only allowed in an operation 'ensures'", x.get("pos"))
                return {"k": "bool", "v": False}
            return result_expr(op, x["v"], errors, x.get("pos"))
        if k == "unchanged":
            if prop.kind not in ("ensures", "law"):
                errors.add("UNCHANGED_NOT_ALLOWED", "'unchanged' is only allowed in 'law' and 'ensures'", x.get("pos"))
                return {"k": "bool", "v": False}
            free = [{**f, "key": walk(f["key"], prop)} for f in x["except"]]
            return frame_expr(spec, free, errors, x.get("pos"))
        return {key: walk(v, prop) if key != "pos" else v for key, v in x.items()}

    for p in spec.props:
        if p.expr is not None:
            p.expr = walk(p.expr, p)
