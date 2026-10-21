"""Component interface: the minimum another UPA (or AI agent) needs to use it without reading its source.

This is a projection of verified IR: operations and observable outcomes, compositions and atomicity policy,
exportable pure queries, and rules with their formal status. The contract does not include bodies."""
from __future__ import annotations
from pathlib import Path

from .components import PRIMITIVE, effective_tier, exportable


def _find_root(path: Path):
    from .compiler import find_root
    return find_root(path)


def interface(path: Path, timeout_ms: int = 60000, verify_rules: bool = True) -> dict:
    from .compiler import compile_source
    from .verifier import verify
    src = path.read_text(encoding="utf-8")
    spec, ir, _, errors = compile_source(src, None, _find_root(path))
    if errors:
        return {"spec": spec.name, "version": spec.version, "error": errors.to_json()[:3]}
    status, verdict = {}, "NOT_VERIFIED"
    if verify_rules:
        vr = verify(ir, timeout_ms, None)
        verdict = vr["verdict"]
        status = {r["rule"]: r["status"] for r in vr["rules"]}
    aliases = {a.name: a.base for a in spec.aliases}

    def outcomes(cases):
        out = []
        for c in cases:
            o = c["outcome"] or ("ok" if c["stmts"] or c["cond"] is None else None)
            if o and o not in out:
                out.append(o)
        return out

    def sig(params):
        return ", ".join(f"{p['name']}: {p['type']}" for p in params)

    ops = [{"name": o["name"], "params": sig(o["params"]), "outcomes": outcomes(o["cases"]),
            "imported_from": o["block"]} for o in ir["operations"]]
    composes = [{"name": c["name"], "params": sig(c["params"]), "atomic": bool(c.get("atomic")),
                 "steps": [x["op"] for x in c["calls"]],
                 "loops": [x["op"] for x in c["calls"] if x.get("loop")],
                 "outcomes": [f"{x['step']}.{x['outcome']}" for x in c.get("outcomes", [])]} for c in ir["composes"]]
    queries = [{"name": q.name, "params": ", ".join(f"{p.name}: {p.type}" for p in q.params), "ret": q.ret}
               for q in spec.queries if exportable(q, aliases) is None]
    rules = [{"id": p["id"], "kind": p["kind"], "text": p["text"],
              "origin": p["origin"]["kind"] if p["origin"] else None,
              "status": status.get(p["id"], "NOT_VERIFIED"), "imported_from": p["block"]} for p in ir["properties"]]
    gaps = [dict(g) for g in ir.get("notes", {}).get("language_gaps", [])]
    return {
        "spec": spec.name, "version": spec.version, "tier": effective_tier(spec),
        "tier_declared": bool(spec.tier), "depth": getattr(spec, "component_depth", 0), "verdict": verdict,
        "uses_components": [{"alias": c["alias"], "ref": c["ref"], "version": c["version"]} for c in spec.component_info],
        "entities": sorted(ir["entities"]), "operations": ops, "composes": composes,
        "queries": queries, "rules": rules, "language_gaps": gaps,
        "regeneration_coverage": dict(ir.get("regeneration_coverage", {})),
    }


def to_markdown(i: dict) -> str:
    if "error" in i:
        return f"# {i['spec']}@{i['version']}: does not compile\n" + "\n".join(f"- {e['error']}: {e['message']}" for e in i["error"])
    L = [f"# {i['spec']}@{i['version']} · tier {i['tier']} · depth {i['depth']} · {i['verdict']}"]
    if i["uses_components"]:
        L.append("Uses: " + ", ".join(f"{c['alias']}={c['ref']}@{c['version']}" for c in i["uses_components"]))
    if i["operations"]:
        L.append("## Operations")
        for o in i["operations"]:
            L.append(f"- {o['name']}({o['params']}) → {' | '.join(o['outcomes']) or 'ok'}")
    if i["composes"]:
        L.append("## Compositions")
        for c in i["composes"]:
            L.append(f"- {'atomic ' if c['atomic'] else ''}{c['name']}({c['params']}) = {'; '.join(c['steps'])}"
                     + (f" · repeats: {', '.join(c['loops'])}" if c["loops"] else "")
                     + (f" → {' | '.join(c['outcomes'])}" if c["outcomes"] else ""))
    if i["queries"]:
        L.append("## Reusable Pure Queries")
        for q in i["queries"]:
            L.append(f"- {q['name']}({q['params']}) -> {q['ret']}")
    if i["rules"]:
        L.append("## Rules")
        for r in i["rules"]:
            L.append(f"- [{r['status']}] {r['id']} ({r['origin']}): {r['text']}")
    if i.get("language_gaps"):
        L.append("## Language Gaps")
        for g in i["language_gaps"]:
            where = g.get("target") or g.get("scope") or "no declared scope (legacy)"
            L.append(f"- {g['id']} [{g['kind']}/{g['impact']}] {where} · "
                     f"{'required' if g['required'] else 'not required'}: {g['text']}")
        c = i.get("regeneration_coverage") or {}
        L.append(f"- regeneration coverage: {c.get('state', 'not_evaluated')} "
                 "(based on declared surfaces; VERIFIED is independent)")
    return "\n".join(L) + "\n"
