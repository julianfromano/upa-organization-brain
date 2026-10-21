"""Replay anonymized observations against a verified IR and report whether the model reproduces them.

Case format (JSON Lines, one case per line):
  {"op": "AssignRoute", "args": {"zone": "NTH"}, "expect": {"Order.route": 1},
   "free": ["priority"], "n": 66, "label": "orders by northern route"}
- args: known parameters (missing values use defaults: false, an empty string, or 0).
- free: Boolean parameters missing from the log; all combinations are tried to explain the observation.
- expect: expected values for "Entity.attribute" (or "Entity.exists") for the case ID.
- n: number of times observed (aggregated cases, with no personal data).
"""
from __future__ import annotations

import itertools
import json
from pathlib import Path

from targets.dotnet.refmodel import Ref

CASE_ID = 1


def _enc(ref: Ref, ty: str, v):
    if ty == "bool":
        return 1 if v else 0
    if ty == "text":
        return ref.intern_text(str(v))
    if ty.startswith("enum:"):
        return ref.enums[ty[5:]].index(v)
    if ty == "id":
        return CASE_ID
    return int(v)


def _dec(ref: Ref, ty: str | None, v):
    if ty == "bool":
        return bool(v)
    if ty == "text":
        return ref.text_value(v)
    if ty and ty.startswith("enum:"):
        return ref.enums[ty[5:]][v]
    return v


def _params(ir: dict, name: str) -> list[dict]:
    for x in ir["operations"] + ir.get("composes", []):
        if x["name"] == name:
            return x["params"]
    raise KeyError(f"no operation or composition named {name!r} exists in {ir.get('spec')}")


def run_case(ir: dict, case: dict) -> dict:
    params = _params(ir, case["op"])
    free = [f for f in case.get("free", []) if any(p["name"] == f and p["type"] == "bool" for p in params)]
    explained, observed = [], []
    for combo in itertools.product([False, True], repeat=len(free)):
        ref = Ref(ir)
        known = dict(case.get("args", {}), **dict(zip(free, combo)))
        args = [_enc(ref, p["type"], known.get(p["name"], "" if p["type"] == "text" else 0)) for p in params]
        st = ref.apply(ref.empty(), case["op"], args)
        got = {}
        for key in case["expect"]:
            ent, attr = key.split(".", 1)
            ty = "bool" if attr == "exists" else ref.attr_types.get((ent, attr))
            got[key] = _dec(ref, ty, st.get(key, {}).get(CASE_ID, 0))
        observed.append({"free": dict(zip(free, combo)), "got": got})
        if all(got[k] == v for k, v in case["expect"].items()):
            explained.append(dict(zip(free, combo)))
    status = "REPRODUCES" if explained and (not free or len(explained) == 2 ** len(free)) else (
        "PARTIALLY_REPRODUCES" if explained else "CONTRADICTS")
    return {"label": case.get("label"), "op": case["op"], "n": case.get("n", 1), "expect": case["expect"],
            "status": status, "explained_by": explained, "observations": observed if status == "CONTRADICTS" else None}


def replay(ir: dict, cases: list[dict]) -> dict:
    rows = [run_case(ir, c) for c in cases]
    tot = sum(r["n"] for r in rows)
    by = {}
    for r in rows:
        by[r["status"]] = by.get(r["status"], 0) + r["n"]
    return {"spec": ir.get("spec"), "version": ir.get("version"), "case_count": len(rows), "observation_count": tot,
            "by_status": by, "reproduction_pct": round(100 * (by.get("REPRODUCES", 0) + by.get("PARTIALLY_REPRODUCES", 0)) / tot, 2) if tot else None,
            "rows": rows}


def load_cases(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("//")]


def to_markdown(rep: dict) -> str:
    out = [f"# Replay of observed cases — {rep['spec']} {rep['version']}", "",
           f"- Cases: {rep['case_count']} (observations: {rep['observation_count']})",
           f"- Reproduction rate: {rep['reproduction_pct']}% · by status: {rep['by_status']}", "",
           "| Case | Operation | n | Expected | Status | Explained by |", "|---|---|---|---|---|---|"]
    for r in rep["rows"]:
        exp = ", ".join(f"{k}={v}" for k, v in r["expect"].items())
        why = "any value of the unconstrained parameters" if r["status"] == "REPRODUCES" and r["explained_by"] != [{}] else "; ".join(", ".join(f"{k}={'yes' if v else 'no'}" for k, v in e.items()) or "—" for e in r["explained_by"]) or (
            "observed " + json.dumps([o["got"] for o in r["observations"] or []], ensure_ascii=False))
        out.append(f"| {r['label'] or ''} | {r['op']} | {r['n']} | {exp} | {r['status']} | {why} |")
    out += ["", "REPRODUCES means the model matches the observation for every value of unconstrained parameters; "
            "PARTIALLY_REPRODUCES means it matches only for the indicated values (an inference about data absent from the log); "
            "CONTRADICTS means no combination explains it: either the model or the log is wrong."]
    return "\n".join(out) + "\n"
