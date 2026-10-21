"""Governance views over verified IR: fidelity and coverage, rule impact,
refutations in business language, and semantic differences between versions.

Everything is derived from canonical-ir.json and verification-result.json; nothing replaces the verifier."""
from __future__ import annotations

import json
import re
from pathlib import Path

LOC = re.compile(r"^[\w./\\-]+(\.[\w]+)*(:\d+(-\d+)?)?(#\w+)?\s*[:(]")


def _walk(x):
    if isinstance(x, dict):
        yield x
        for v in x.values():
            yield from _walk(v)
    elif isinstance(x, list):
        for v in x:
            yield from _walk(v)


def expr_reads(expr) -> set[str]:
    out: set[str] = set()
    for n in _walk(expr):
        k = n.get("k")
        if k == "attr" and n.get("e"):
            out.add(f"{n['e']}.{n['a']}")
        elif k in ("in", "exists") and n.get("e"):
            out.add(n["e"])
    return out


def op_writes(op) -> set[str]:
    out: set[str] = set()
    for c in op.get("cases", []):
        for n in _walk(c.get("stmts", [])):
            kind, ent = n.get("kind"), n.get("entity")
            if not ent:
                continue
            if kind == "create":
                out.add(ent)
                out.update(f"{ent}.{i['attr']}" for i in n.get("inits", []))
            elif kind == "set":
                out.add(f"{ent}.{n['attr']}")
            elif kind == "delete":
                out.add(ent)
    return out


def touches(reads: set[str], writes: set[str]) -> bool:
    for w in writes:
        for r in reads:
            if w == r or (("." not in w) and r.startswith(w + ".")) or (("." not in r) and w.startswith(r + ".")):
                return True
    return False


def classify_origin(origin: dict) -> str:
    """Classify the evidence behind a rule without inventing verification that does not exist."""
    k = (origin or {}).get("kind")
    if k == "derived":
        return "derived decision"
    if k == "assumption":
        return "modeler assumption"
    ref = ((origin or {}).get("ref") or "").strip()
    if not ref:
        return "unsupported"
    if LOC.match(ref) or re.search(r"\bEV-[A-Z]+-\d+", ref):
        return "location + interpretation"
    return "modeler interpretation"


# ------------------------------------------------------------------ Quality
def _sum_notes(tree_notes: list[dict]) -> dict:
    keys = ("assumptions", "human_decisions", "questions", "language_gaps", "persists", "risks", "concurrency",
            "parameters", "concurrency_gaps")
    out = {}
    for k in keys:
        seen, lst = set(), []
        for n in tree_notes:
            for x in (n.get(k) or []):
                key = x.get("id") if isinstance(x, dict) else None
                if key is not None and key in seen:
                    continue
                seen.add(key)
                lst.append(x)
        out[k] = lst
    return out


def quality(ir: dict, ver: dict, notes: dict, replays: list[dict]) -> dict:
    props = ir.get("properties", [])
    origins: dict[str, int] = {}
    for p in props:
        c = classify_origin(p.get("origin"))
        origins[c] = origins.get(c, 0) + 1
    n = max(len(props), 1)
    summ = ver.get("summary") or {}
    ops = ir.get("operations", [])
    conc_on = {c.get("on") for c in notes.get("concurrency", [])}
    gaps = notes.get("concurrency_gaps") or []
    reads = {(r.get("reproduction_pct"), r.get("case_count")) for r in replays}
    pcts = [r["reproduction_pct"] for r in replays if isinstance(r.get("reproduction_pct"), (int, float))]
    return {
        "rules": len(props),
        "origins": origins,
        "traceable_pct": round(100 * (origins.get("location + interpretation", 0) + origins.get("derived decision", 0)) / n),
        "verified_extracts": 0,
        "accepted_by_owner": 0,
        "proven": summ.get("PROVEN", 0), "refuted": summ.get("REFUTED", 0), "unknown": summ.get("UNKNOWN", 0),
        "assumptions": len(notes.get("assumptions", [])), "human_open": len(notes.get("human_decisions", [])),
        "questions_open": len(notes.get("questions", [])), "language_gaps": len(notes.get("language_gaps", [])),
        "persists": len(notes.get("persists", [])), "risks": len(notes.get("risks", [])),
        "concurrency_declared": len(notes.get("concurrency", [])), "concurrency_gaps": len(gaps),
        "operations": len(ops), "ops_with_concurrency": len([o for o in ops if o["name"] in conc_on or o["name"].split("_")[-1] in conc_on]),
        "replay_pct": round(sum(pcts) / len(pcts)) if pcts else None, "replay_cases": sum(r.get("case_count") or 0 for r in replays),
        "regeneration": ir.get("regeneration_coverage"),
    }


# -------------------------------------------------------- readable refutations
def _parse_array(s: str):
    """Z3 prints arrays as Store(K(Int, False), 1, True); convert them to {key: value, '*': default}."""
    s = s.strip()
    m = re.fullmatch(r"K\([^,]+,\s*(.+)\)", s)
    if m:
        return {"*": m.group(1).strip()}
    if s.startswith("Store(") and s.endswith(")"):
        inner = s[6:-1]
        depth = 0
        parts, cur = [], ""
        for ch in inner:
            if ch in "([":
                depth += 1
            elif ch in ")]":
                depth -= 1
            if ch == "," and depth == 0:
                parts.append(cur)
                cur = ""
            else:
                cur += ch
        parts.append(cur)
        if len(parts) == 3:
            base = _parse_array(parts[0])
            if base is not None:
                base[parts[1].strip()] = parts[2].strip()
                return base
    return None


def _pretty_state(state: dict) -> list[str]:
    out = []
    for k, v in (state or {}).items():
        arr = _parse_array(v)
        if arr is None:
            out.append(f"{k} = {v}")
            continue
        items = {a: b for a, b in arr.items() if a != "*"}
        if not items:
            continue
        out.append(f"{k}: " + ", ".join(f"#{a}→{b}" for a, b in items.items()))
    return out


def refutations(ir: dict, ver: dict, notes: dict) -> list[dict]:
    props = {p["id"]: p for p in ir.get("properties", [])}
    conc = {c.get("on"): c for c in notes.get("concurrency", [])}
    gap_ops = {g.get("operation") for g in (notes.get("concurrency_gaps") or []) if isinstance(g, dict)}
    out = []
    for o in ver.get("obligations", []):
        if o.get("status") != "REFUTED":
            continue
        p = props.get(o.get("rule"), {})
        op = o.get("operation")
        ce = o.get("counterexample") or {}
        short = (op or "").split("_")[-1]
        if short in conc or op in conc:
            cause = f"The operation declares concurrency mode «{(conc.get(short) or conc.get(op)).get('mode')}», but the formal model still analyzes it as atomic; check that the declared mode covers this rule."
        elif op in gap_ops or short in gap_ops:
            cause = "The operation writes state read by this rule and has no declared concurrency protection (see undeclared concurrency)."
        else:
            cause = "The operation does not guarantee the rule for some model-permitted initial state; a guard or effect may be missing."
        out.append({
            "obligation": o["id"], "rule": o.get("rule"), "kind": o.get("kind"),
            "text": p.get("text") or o.get("description"), "origin": classify_origin(p.get("origin")),
            "origin_ref": (p.get("origin") or {}).get("ref"),
            "operation": op, "path": [op] if op else [],
            "params": ce.get("params") or {}, "pre": _pretty_state(ce.get("pre_state")),
            "post": _pretty_state(ce.get("post_state_changed")), "cause": cause,
            "raw": ce,
        })
    return out


# ------------------------------------------------------------------ Impact
def impact(ir: dict, ver: dict, composition_ops: dict[str, list[str]], components: list[dict], stores: list[dict],
           replays: list[dict], notes: dict) -> dict:
    ops = {o["name"]: o for o in ir.get("operations", [])}
    writes = {n: op_writes(o) for n, o in ops.items()}
    pres: dict[str, dict[str, dict]] = {}
    for o in ver.get("obligations", []):
        if o.get("kind") == "PRES":
            pres.setdefault(o["rule"], {})[o["operation"]] = o
    ens: dict[str, list[str]] = {}
    for p in ir.get("properties", []):
        if p.get("kind") == "ensures":
            ens[p["id"]] = list(p.get("targets") or [])
    replay_by_op: dict[str, list[dict]] = {}
    for r in replays:
        for f in r.get("rows", []):
            replay_by_op.setdefault(f.get("op"), []).append({"label": f.get("label"), "status": f.get("status")})
    out = {}
    for p in ir.get("properties", []):
        rd = expr_reads(p.get("expr"))
        hit = [n for n, w in writes.items() if touches(rd, w)]
        if p.get("kind") == "ensures":
            hit = sorted(set(hit) | set(ens.get(p["id"], [])))
        touch = []
        for n in hit[:40]:
            ob = pres.get(p["id"], {}).get(n)
            status = (ob or {}).get("status") or ("PROVEN" if p.get("kind") == "ensures" else "N/A")
            touch.append({"op": n, "status": status, "writes": sorted(writes.get(n, set()) & _rel(rd, writes.get(n, set()))),
                          "how": "proven" if status == "PROVEN" else "refuted" if status == "REFUTED" else "unresolved"})
        by = sorted({c for n in hit for c, cops in composition_ops.items() if n in cops})
        token = (p["id"].split("_")[0] if "_" in p["id"] else "").lower()
        comps = [{"name": c.get("name"), "repo": c.get("repository"), "id": c.get("id")} for c in components
                 if token and any(token in (r or "").lower() for r in c.get("realizes", []))]
        ents = sorted({r.split(".")[0] for r in rd})
        tabs = [{"entity": s.get("entity"), "table": s.get("table")} for s in stores if s.get("entity") in ents]
        owners = sorted({a.get("owner") for a in notes.get("assumptions", []) if a.get("owner")})
        origin = p.get("origin") or {}
        out[p["id"]] = {
            "text": p.get("text"), "kind": p.get("kind"), "block": p.get("block"),
            "reads": sorted(rd), "writers": touch, "untouched_ops": max(len(ops) - len(hit), 0),
            "processes": by, "components": comps, "tables": tabs,
            "replay": [x for n in hit for x in replay_by_op.get(n, [])][:20],
            "owners": owners,
            "generated": [f"Ops.{n}" for n in hit[:40]],
            "evidence": {"class": classify_origin(origin), "ref": origin.get("ref"), "kind": origin.get("kind")},
        }
    return out


def _rel(reads: set[str], writes: set[str]) -> set[str]:
    return {w for w in writes if touches(reads, {w})}


# --------------------------------------------------------------------- diff
def _load_tree(root: Path, only: set | None = None) -> dict:
    root = Path(root)
    irs, vers = {}, {}
    for f in sorted(root.rglob("canonical-ir.json")):
        try:
            ir = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        key = ir.get("spec") or f.parent.name
        if only is not None and key not in only:
            continue
        irs[key] = ir
        vf = f.parent / "verification-result.json"
        if vf.exists():
            vers[key] = json.loads(vf.read_text(encoding="utf-8"))
    return {"irs": irs, "vers": vers}


def _conj(e) -> list[str]:
    if isinstance(e, dict) and e.get("k") == "bin" and e.get("op") == "and":
        return _conj(e["l"]) + _conj(e["r"])
    return [] if e is None else [json.dumps(e, sort_keys=True)]


def _canon(x) -> str:
    return json.dumps(x, sort_keys=True, ensure_ascii=False)


def _effects(op) -> list[str]:
    out = []
    for c in op.get("cases", []):
        for n in _walk(c.get("stmts", [])):
            if n.get("kind") in ("create", "set", "delete"):
                out.append(f"{n['kind']} {n.get('entity')}" + (f".{n['attr']}" if n.get("attr") else ""))
    return sorted(set(out))


def diff_trees(old_dir: Path, new_dir: Path, only: set | None = None) -> dict:
    a, b = _load_tree(old_dir, only), _load_tree(new_dir, only)

    def flat(t, key):
        out = {}
        for ir in t["irs"].values():
            for x in ir.get(key, []):
                out[x.get("id") or x.get("name")] = x
        return out

    rules_a, rules_b = flat(a, "properties"), flat(b, "properties")
    ops_a, ops_b = flat(a, "operations"), flat(b, "operations")
    out: dict = {"rules": {"added": [], "removed": [], "modified": []}, "guards": [], "outcomes": [], "effects": [],
                 "states_removed": [], "states_added": [], "entities": [], "boundaries": {"added": [], "removed": []},
                 "assumptions": [], "obligations": [], "evidence_invalidated": [], "components": []}
    for i in sorted(rules_b.keys() - rules_a.keys()):
        out["rules"]["added"].append({"id": i, "text": rules_b[i].get("text")})
    for i in sorted(rules_a.keys() - rules_b.keys()):
        out["rules"]["removed"].append({"id": i, "text": rules_a[i].get("text")})
        if (rules_a[i].get("origin") or {}).get("ref"):
            out["evidence_invalidated"].append({"rule": i, "why": "the rule no longer exists", "ref": rules_a[i]["origin"]["ref"]})
    for i in sorted(rules_a.keys() & rules_b.keys()):
        ra, rb = rules_a[i], rules_b[i]
        if _canon(ra.get("expr")) != _canon(rb.get("expr")) or ra.get("kind") != rb.get("kind"):
            out["rules"]["modified"].append({"id": i, "text": rb.get("text"), "before": ra.get("text")})
        if _canon(ra.get("origin")) != _canon(rb.get("origin")):
            out["evidence_invalidated"].append({"rule": i, "why": "declared evidence changed",
                                                "ref": (ra.get("origin") or {}).get("ref")})
    for n in sorted(ops_a.keys() & ops_b.keys()):
        ca, cb = ops_a[n].get("cases", []), ops_b[n].get("cases", [])
        oa = {c.get("outcome"): c for c in ca}
        ob = {c.get("outcome"): c for c in cb}
        for o in sorted(set(ob) - set(oa), key=str):
            out["outcomes"].append({"op": n, "change": "new outcome", "outcome": o})
        for o in sorted(set(oa) - set(ob), key=str):
            out["outcomes"].append({"op": n, "change": "removed outcome", "outcome": o})
        for o in set(oa) & set(ob):
            ga, gb = set(_conj(oa[o].get("cond"))), set(_conj(ob[o].get("cond")))
            if ga != gb:
                kind = "more restrictive (adds conditions)" if ga < gb else "broader (removes conditions)" if gb < ga else "different"
                out["guards"].append({"op": n, "outcome": o, "change": kind, "added": len(gb - ga), "removed": len(ga - gb)})
        ea, eb = set(_effects(ops_a[n])), set(_effects(ops_b[n]))
        for e in sorted(eb - ea):
            out["effects"].append({"op": n, "change": "new effect", "effect": e})
        for e in sorted(ea - eb):
            out["effects"].append({"op": n, "change": "removed effect", "effect": e})
    for n in sorted(ops_b.keys() - ops_a.keys()):
        out["outcomes"].append({"op": n, "change": "new operation", "outcome": None})
    for n in sorted(ops_a.keys() - ops_b.keys()):
        out["outcomes"].append({"op": n, "change": "removed operation", "outcome": None})

    def enums(t):
        return {f"{n}.{v}" for ir in t["irs"].values() for n, vs in (ir.get("enums") or {}).items() for v in vs}
    ea, eb = enums(a), enums(b)
    out["states_removed"], out["states_added"] = sorted(ea - eb), sorted(eb - ea)

    def attrs(t):
        s = set()
        for ir in t["irs"].values():
            for n, e in (ir.get("entities") or {}).items():
                s.add(n)
                s.update(f"{n}.{x['name']}" for x in e.get("attrs", []))
        return s
    sa, sb = attrs(a), attrs(b)
    out["entities"] = [{"change": "new", "item": x} for x in sorted(sb - sa)] + [{"change": "removed", "item": x} for x in sorted(sa - sb)]

    def comps(t):
        return {c["id"]: c for ir in t["irs"].values() for c in (ir.get("system_map") or {}).get("components", [])}
    ca, cb = comps(a), comps(b)
    out["boundaries"]["added"] = [cb[i].get("name") for i in sorted(cb.keys() - ca.keys())]
    out["boundaries"]["removed"] = [ca[i].get("name") for i in sorted(ca.keys() - cb.keys())]

    def invs(t):
        return {" → ".join(str(i[k]) for k in ("operation", "op", "name", "target", "service", "external", "id") if i.get(k))
                for ir in t["irs"].values() for i in (ir.get("invocations") or [])}
    ia, ib = invs(a), invs(b)
    out["boundaries"]["added"] += sorted(ib - ia)
    out["boundaries"]["removed"] += sorted(ia - ib)

    def notes(t):
        return {x["id"]: x for ir in t["irs"].values() for x in (ir.get("notes") or {}).get("assumptions", [])}
    na, nb = notes(a), notes(b)
    for i in sorted(nb.keys() - na.keys()):
        out["assumptions"].append({"id": i, "change": "new assumption", "text": nb[i].get("text")})
    for i in sorted(na.keys() - nb.keys()):
        out["assumptions"].append({"id": i, "change": "removed assumption", "text": na[i].get("text")})
    for i in sorted(na.keys() & nb.keys()):
        if na[i].get("text") != nb[i].get("text"):
            out["assumptions"].append({"id": i, "change": "modified assumption", "text": nb[i].get("text")})

    def obls(t):
        return {o["id"]: o["status"] for v in t["vers"].values() for o in v.get("obligations", [])}
    xa, xb = obls(a), obls(b)
    for i in sorted(xa.keys() & xb.keys()):
        if xa[i] != xb[i]:
            out["obligations"].append({"id": i, "before": xa[i], "after": xb[i]})
    out["obligations"].sort(key=lambda x: (x["after"] != "REFUTED", x["id"]))

    changed = {x["id"] for x in out["rules"]["added"] + out["rules"]["removed"] + out["rules"]["modified"]}
    changed |= {x["op"] for x in out["guards"] + out["effects"] + out["outcomes"]}
    toks = {c.split("_")[0].lower() for c in changed if c}
    seen = {}
    for cid, c in {**ca, **cb}.items():
        if any(t in (r or "").lower() for r in c.get("realizes", []) for t in toks):
            seen[cid] = {"name": c.get("name"), "repo": c.get("repository")}
    out["components"] = list(seen.values())
    out["summary"] = {k: (sum(len(v) for v in out[k].values()) if isinstance(out[k], dict) else len(out[k]))
                      for k in ("rules", "guards", "outcomes", "effects", "states_removed", "states_added", "entities",
                                "boundaries", "assumptions", "obligations", "evidence_invalidated", "components")}
    return out


def diff_markdown(d: dict) -> str:
    L = ["# Semantic diff", ""]
    s = d["summary"]
    L.append("Summary: " + ", ".join(f"{k}={v}" for k, v in s.items() if v) or "no changes")
    for t, key in (("Added rules", "added"), ("Removed rules", "removed"), ("Modified rules", "modified")):
        if d["rules"][key]:
            L += ["", f"## {t}"] + [f"- `{x['id']}` {x.get('text') or ''}" for x in d["rules"][key]]
    for t, key, fmt in (("Guards", "guards", lambda x: f"`{x['op']}` → {x['outcome']}: {x['change']}"),
                        ("Outcomes and operations", "outcomes", lambda x: f"`{x['op']}` {x['change']} {x.get('outcome') or ''}"),
                        ("Effects", "effects", lambda x: f"`{x['op']}` {x['change']}: {x['effect']}"),
                        ("Assumptions", "assumptions", lambda x: f"`{x['id']}` {x['change']}"),
                        ("Changed obligations", "obligations", lambda x: f"`{x['id']}`: {x['before']} → {x['after']}"),
                        ("Invalidated evidence", "evidence_invalidated", lambda x: f"`{x['rule']}`: {x['why']}")):
        if d[key]:
            L += ["", f"## {t}"] + ["- " + fmt(x) for x in d[key]]
    if d["states_removed"] or d["states_added"]:
        L += ["", "## States", *[f"- removed: {x}" for x in d["states_removed"]], *[f"- new: {x}" for x in d["states_added"]]]
    if d["boundaries"]["added"] or d["boundaries"]["removed"]:
        L += ["", "## Boundaries", *[f"- new: {x}" for x in d["boundaries"]["added"]], *[f"- removed: {x}" for x in d["boundaries"]["removed"]]]
    if d["components"]:
        L += ["", "## Potentially Impacted Components", *[f"- {c['name']} ({c['repo']})" for c in d["components"]]]
    return "\n".join(L) + "\n"
