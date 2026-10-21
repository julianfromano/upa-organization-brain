"""Canonical IR: a typed representation independent of source syntax and any backend."""
from __future__ import annotations
import hashlib
import json

from .ast import Spec
from .semantic import Analyzer, strip_pos

IR_VERSION = "upa-ir-0.1"


def build_ir(spec: Spec, an: Analyzer, report: dict | None = None) -> dict:
    def gap(n):
        return {
            "id": n.id, "kind": n.meta.get("kind", "language"), "text": n.text,
            "status": "LANGUAGE_GAP",
            **({"scope": n.meta["scope"]} if n.meta.get("scope") else {}),
            **({"target": n.meta["target"]} if n.meta.get("target") else {}),
            "impact": n.meta.get("impact", "medium"),
            "required": bool(n.meta.get("required", True)),
            **({"legacy": True} if n.meta.get("legacy") else {}),
        }

    def regeneration_coverage(gaps):
        declared = [g for g in gaps if g["kind"] == "regeneration"
                    and (g.get("target") or g.get("scope"))]
        surfaces = sorted({g.get("target") or g.get("scope") for g in declared if g.get("target") or g.get("scope")})
        if not declared:
            state = "not_evaluated"
        elif any(g["required"] for g in declared):
            state = "partial"
        else:
            state = "complete"
        return {
            "state": state,
            "declared_surfaces": surfaces,
            "open_required": [g["id"] for g in declared if g["required"]],
            "basis": "declared_surfaces_only",
            "percentage": None,
            "verification_result": "SEPARATE_ARTIFACT",
        }

    language_gaps = [gap(n) for n in spec.notes if n.kind == "language_gap"]
    def stmt(s):
        d = {"kind": s.kind, "entity": s.entity, "key": strip_pos(s.key)}
        if s.file:
            d["file"] = True
        if s.var is not None:
            d["each"] = {"var": s.var, "cond": strip_pos(s.cond)}
        if s.kind == "delete":
            pass
        elif s.kind == "set":
            d["attr"] = s.attr
            d["value"] = strip_pos(s.value)
        else:
            d["inits"] = [{"attr": a, "value": strip_pos(v)} for a, v in s.inits]
        return d

    def params(ps):
        out = []
        for p in ps:
            d = {"name": p.name, "type": an.norm(p.type), "decl": p.type}
            if p.collection:
                d["collection"] = dict(p.collection)
            if p.count_of:
                d["count_of"] = p.count_of
            if p.generated_for:
                d["generated_for"] = p.generated_for
            out.append(d)
        return out

    def fact(f):
        d = {"id": f.id, "kind": f.kind, "entity": f.entity, "attr": f.attr, "card": f.card, "target": f.target,
             "origin": {"kind": f.origin.kind, "ref": f.origin.ref} if f.origin else None}
        if f.multi:
            d.update({"min": f.min, "max": f.max, "element": f.element})
        return d

    def compose_outcomes(sp, c):
        ops = {o.name: o for o in sp.operations}
        seen, out = set(), []
        for n, _ in c.calls:
            o = ops.get(n)
            for case in (o.cases if o else []):
                if case.outcome and (n, case.outcome) not in seen:
                    seen.add((n, case.outcome))
                    out.append({"step": n, "outcome": case.outcome})
        return {"outcomes": out} if out else {}

    ir = {
        "ir_version": IR_VERSION,
        "spec": spec.name,
        "kind": spec.kind,
        "version": spec.version,
        "uses": [{"block": b, "version": v} for b, v in spec.uses],
        "level": spec.level,
        **({"tier": spec.tier} if spec.tier else {}),
        "inherited": sorted(spec.inherited_ids),
        "types": {a.name: {"base": an.norm(a.base), "scale": a.scale} for a in spec.aliases},
        "enums": {e.name: list(e.members) for e in spec.enums},
        "entities": {
            e.name: {"attrs": [{"name": a.name, "type": an.norm(a.type), "decl": a.type, "ghost": a.ghost}
                               for a in e.attrs],
                     "block": e.origin_block}
            for e in sorted(spec.entities, key=lambda x: x.name)},
        "facts": [fact(f) for f in spec.facts],
        "initial": strip_pos(spec.initial) if spec.initial is not None else None,
        "initial_origin": ({"kind": spec.initial_origin.kind, "ref": spec.initial_origin.ref}
                           if spec.initial_origin else None),
        "operations": [
            {"name": o.name, "params": params(o.params), "block": o.origin_block, "layer": o.layer,
             "cases": [{"cond": strip_pos(c.cond) if c.cond is not None else None,
                        "stmts": [stmt(s) for s in c.stmts],
                        "outcome": c.outcome, **({"success": True} if c.success else {})} for c in o.cases]}
            for o in spec.operations],
        "composes": [
            {"name": c.name, "params": params(c.params), "layer": c.layer,
             "calls": [{"op": st[0], "args": [strip_pos(a) for a in st[1]],
                        **({"guard": strip_pos(st.guard)} if getattr(st, "guard", None) is not None else {}),
                        **({"loop": strip_pos(st.loop)} if getattr(st, "loop", None) is not None else {}),
                        **({"trail": list(st.trail)} if getattr(st, "trail", ()) else {})} for st in c.calls],
             **({"atomic": True} if c.atomic else {}),
             **({"execution": [dict(x) for x in c.execution]}
                if any(x["stage"] != i or x["async"] or x["parallel"] or x["loop"]
                       for i, x in enumerate(c.execution)) else {}),
             **compose_outcomes(spec, c)}
            for c in spec.composes],
        "queries": [{"name": q.name, "params": params(q.params), "layer": q.layer, "ret": an.norm(q.ret), "decl": q.ret,
                     "body": strip_pos(q.body)} for q in spec.queries],
        "properties": [
            {"id": p.id, "kind": p.kind, "group": p.group, "text": p.text,
             "origin": {"kind": p.origin.kind, "ref": p.origin.ref} if p.origin else None,
             "expr": strip_pos(p.expr) if p.expr is not None else None,
             "targets": list(p.targets), "block": p.origin_block}
            for p in spec.props],
        "externals": [{"name": e.name, "version": e.version, "source": e.source, "hash": e.hash,
                       "operations": [{"name": o.name, "http": o.http, "params": params(o.params),
                                       "outcomes": [{"name": n, "status": s, "uncertain": u} for n, s, u in o.outcomes],
                                       "resolve_by": o.resolve_by,
                                       "outcome_enum": f"{e.name}_{o.name}_Outcome"} for o in e.ops]}
                      for e in spec.externals],
        **({"terms": {k: {"label": v[0], **({"text": v[1]} if v[1] else {})} for k, v in sorted(spec.terms.items())}}
           if spec.terms else {}),
        **({"stores": [{k: v for k, v in x.items() if k != "pos"} for x in spec.stores]} if spec.stores else {}),
        **({"invocations": [{"target": v.target, "kind": v.kind, "name": v.name, "mode": v.mode, "via": list(v.via),
                             "evidence": v.evidence} for v in spec.invocations]} if spec.invocations else {}),
        **({"async_producers": [{
            "id": v.id, "operation": v.operation, "transport": v.transport,
            "channel": v.channel, **({"message": v.message} if v.message else {}),
            "confirmation": v.confirmation, "evidence": v.evidence,
        } for v in spec.async_producers]} if spec.async_producers else {}),
        **({"async_consumers": [{
            "id": v.id, "operation": v.operation, "transport": v.transport,
            "channel": v.channel, "message": v.message, "subscription": v.subscription,
            "durability": v.durability, "processing": v.processing,
            "delivery": v.delivery, "completion": v.completion,
            "failure": v.failure, "evidence": v.evidence,
        } for v in spec.async_consumers]} if spec.async_consumers else {}),
        **({"async_correlations": [{
            "id": f"{p.id}__{c.id}", "producer": p.id, "consumer": c.id,
            "transport": p.transport, "channel": p.channel, "message": p.message,
        } for p in spec.async_producers for c in spec.async_consumers
            if p.transport == c.transport and p.channel == c.channel
            and bool(p.message) and p.message == c.message]}
           if any(p.transport == c.transport and p.channel == c.channel
                  and bool(p.message) and p.message == c.message
                  for p in spec.async_producers for c in spec.async_consumers) else {}),
        "notes": {
            "decisions": [{"id": n.id, "text": n.text, "alternatives": n.extra} for n in spec.notes if n.kind == "decision"],
            "assumptions": [{"id": n.id, "text": n.text, "owner": n.extra} for n in spec.notes if n.kind == "assumption"],
            "questions": [{"id": n.id, "text": n.text} for n in spec.notes if n.kind == "question"],
            "human_decisions": [{"id": n.id, "text": n.text, "owner": n.extra, "options": [{"id": i, "text": t} for i, t in n.options]}
                                for n in spec.notes if n.kind == "human_decision"],
            "persists": [{"id": n.id, "target": n.text, **n.meta} for n in spec.notes if n.kind == "persists"],
            "risks": [{"id": n.id, "text": n.text, **n.meta} for n in spec.notes if n.kind == "risk"],
            "concurrency": [{"id": n.id, "text": n.text, **n.meta} for n in spec.notes if n.kind == "concurrency"],
            "parameters": [{"id": n.id, "text": n.text, "owner": n.extra} for n in spec.notes if n.kind == "parameter"],
            "exclusions": [{"id": n.id, "text": n.text} for n in spec.notes if n.kind == "exclusion"],
            "language_gaps": language_gaps,
        },
        "regeneration_coverage": regeneration_coverage(language_gaps),
    }
    lits = set()
    if spec.collections:
        ir["collections"] = [dict(c) for c in spec.collections]
    if spec.component_info:
        ir["composition"] = {
            "depth": getattr(spec, "component_depth", 0),
            "components": [dict(c) for c in spec.component_info],
            "calls": sorted({f"{c['alias']}.{c['query']}" if c["alias"] else c["query"]
                             for c in spec.component_calls if c["alias"]}),
        }
    if spec.system_map_declared:
        ir["system_map"] = {
            "components": [{
                "id": c.id, "name": c.name, "kind": c.kind, "scope": c.scope,
                "role": c.role, "evidence": c.evidence, "runtime": c.runtime, "repository": c.repository,
                "depends_on": sorted(c.depends_on), **({"realizes": list(c.realizes)} if c.realizes else {}),
            } for c in sorted(spec.system_components, key=lambda x: x.id)],
            "contracts": [{
                "id": c.id, "owner": c.owner, "service": c.service, "protocol": c.protocol,
                "operation": c.operation, "request": c.request, "response": c.response,
                "usage": c.usage, "source": c.source, "sha256": c.sha256, "evidence": c.evidence,
                "callers": sorted(c.callers),
            } for c in sorted(spec.system_contracts, key=lambda x: x.id)],
            "flows": [{
                "id": f.id, "name": f.name, "status": f.status, "evidence": f.evidence,
                "steps": [{"component": step.component, "operation": step.operation,
                           "contract": step.contract} for step in f.steps],
            } for f in sorted(spec.system_flows, key=lambda x: x.id)],
            "observations": [{
                "id": o.id, "subject": o.subject, "statement": o.statement,
                "status": o.status, "evidence": o.evidence,
            } for o in sorted(spec.system_observations, key=lambda x: x.id)],
            "catalogs": [{
                "id": c.id, "owner": c.owner, "source": c.source, "status": c.status, "evidence": c.evidence,
                "values": [{"name": v.name, "code": v.code, "meaning": v.meaning} for v in c.values],
            } for c in sorted(spec.system_catalogs, key=lambda x: x.id)],
            "rules": [{
                "id": r.id, "subject": r.subject, "kind": r.kind, "status": r.status, "source": r.source,
                "evidence": r.evidence, "when": list(r.whens), "then": list(r.thens), "error": r.error,
            } for r in sorted(spec.system_rules, key=lambda x: x.id)],
            "jobs": [{
                "id": j.id, "name": j.name, "owner": j.owner, "kind": j.kind, "status": j.status,
                "evidence": j.evidence, "reads": list(j.reads), "writes": list(j.writes),
            } for j in sorted(spec.system_jobs, key=lambda x: x.id)],
            "scope": "source-backed architecture and observations; not a behavioral proof",
        }

    def walk(x):
        if isinstance(x, dict):
            if x.get("k") == "str":
                lits.add(x["v"])
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(ir)
    # Text intern table: "" is the default value (0); each distinct literal receives a distinct ID.
    ir["texts"] = [""] + sorted(lits - {""})
    if report is not None:
        ir["model"] = {"entities": report["inferred_model"]["entities"],
                       "relations": report["inferred_model"]["relations"],
                       "parameter_roles": report["inferred_model"]["parameter_roles"],
                       "ambiguities": report["model_ambiguities"]}
        ir["notes"]["pending_design_decisions"] = [d["id"] for d in report["design_decisions"]
                                                   if d.get("status") == "pending_human"]
    ir["notes"]["concurrency_gaps"] = _concurrency_gaps(ir)
    return ir


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def ir_hash(ir: dict) -> str:
    return hashlib.sha256(canonical_json(ir).encode("utf-8")).hexdigest()


def _concurrency_gaps(ir: dict) -> list:
    """Operations that write state used by a relational rule (uniqueness, counters, sequences) without declaring
    `concurrency`. This is a heuristic warning: the verifier assumes atomic, sequential operations."""
    def walk(x):
        if isinstance(x, dict):
            yield x
            for v in x.values():
                yield from walk(v)
        elif isinstance(x, list):
            for v in x:
                yield from walk(v)

    sensitive = {}
    for p in ir.get("properties", []):
        if p.get("kind") != "invariant":
            continue
        nodes = list(walk(p.get("expr")))
        quant = sum(1 for n in nodes if n.get("k") in ("forall", "exists"))
        nested_key = any(n.get("k") == "attr" and isinstance(n.get("key"), dict) and n["key"].get("k") == "attr" for n in nodes)
        if quant >= 2 or nested_key or any(n.get("k") == "sum" for n in nodes):
            sensitive[p["id"]] = {n["e"] for n in nodes if isinstance(n.get("e"), str)}
    declared = {c.get("on") for c in ir["notes"].get("concurrency", [])}
    gaps = []
    for o in ir.get("operations", []):
        if o["name"] in declared:
            continue
        written = {s["entity"] for c in o["cases"] for s in c["stmts"] if s.get("entity")}
        rules = sorted(r for r, ents in sensitive.items() if ents & written)
        if rules:
            gaps.append({"operation": o["name"], "rules": rules})
    return gaps