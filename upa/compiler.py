"""Compilation pipeline: scope -> rules -> constraints -> minimal model -> canonical IR -> obligations -> verification."""
from __future__ import annotations
import hashlib
import json
import re
import time
from pathlib import Path

from .components import component_path, expand_components
from .errors import ErrorBag, UpaError
from .external import expand_externals
from .terms import check_terms
from .boundary import check_stores
from .infer import infer
from .inherit import apply_uses, find_root
from .ir import build_ir, canonical_json, ir_hash
from .macros import expand_macros
from .multiplicity import expand_collections
from .parser import parse
from .semantic import Analyzer, norm_ws
from .verifier import verify


def sentences(scope: str) -> list[str]:
    return [x.strip() for x in re.split(r"(?<=[.!?])\s+", scope.strip()) if x.strip()]


def coverage(ir: dict, scope: str) -> dict:
    quotes = [(p["id"], norm_ws(p["origin"]["ref"])) for p in ir["properties"]
              if p["origin"] and p["origin"]["kind"] == "literal"]
    quotes += [(f["id"], norm_ws(f["origin"]["ref"])) for f in ir.get("facts", [])
               if f["origin"] and f["origin"]["kind"] == "literal"]
    out = []
    for s in sentences(scope):
        ns = norm_ws(s)
        by = sorted({i for i, q in quotes if q in ns})
        out.append({"sentence": s, "covered_by": by})
    unc = [o["sentence"] for o in out if not o["covered_by"]]
    return {"sentences": out, "uncovered": unc, "ratio": round(1 - len(unc) / max(len(out), 1), 3)}


def compile_source(src: str, scope: str | None, rules_root: Path | None = None, _stack: tuple = ()):
    """Return (spec, IR, inference report, errors)."""
    errors = ErrorBag()
    spec = parse(src)
    apply_uses(spec, rules_root, errors)
    expand_components(spec, rules_root, errors, _stack)
    expand_collections(spec, errors)
    expand_externals(spec, errors)
    check_terms(spec, errors)
    report = infer(spec, errors)
    check_stores(spec, errors)  # Entities may be introduced by facts, so check stores after inference.
    expand_macros(spec, errors)
    an = Analyzer(spec, scope, errors)
    an.run()
    ir = None if errors else build_ir(spec, an, report)
    return spec, ir, report, errors


def write(out: Path, name: str, obj) -> None:
    (out / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def evidence_ledger(ir: dict, vr: dict | None) -> dict:
    status = {r["rule"]: r["status"] for r in vr["rules"]} if vr else {}
    items = []
    for p in ir["properties"]:
        o = p["origin"]
        items.append({"id": p["id"], "kind": p["kind"], "group": p["group"], "text": p["text"],
                      "origin": o["kind"] if o else None, "evidence": o["ref"] if o else None,
                      "formal_status": status.get(p["id"], "NOT_VERIFIED"),
                      "may_be_guarantee": bool(o) and o["kind"] != "unspecified"})
    for f in ir.get("facts", []):
        o = f["origin"]
        items.append({"id": f["id"], "kind": "fact:" + f["kind"], "group": "structural",
                      "text": f"{f['entity']}.{f['attr']} {f['kind']}" if f["kind"] != "has" else
                      f"every {f['entity']} has {f['card']} {f['attr']}: {f['target']}",
                      "origin": o["kind"] if o else None, "evidence": o["ref"] if o else None,
                      "formal_status": "STRUCTURAL", "may_be_guarantee": True})
    return {"items": items, "open_questions": ir["notes"]["questions"],
            "rule": "unspecified properties cannot become guarantees"}


def guarantee_scope(ir: dict, vr: dict | None, csharp: dict | None, model_pending: list) -> dict:
    proven = [r["rule"] for r in vr["rules"] if r["status"] == "PROVEN"] if vr else []
    other = [{"rule": r["rule"], "status": r["status"]} for r in vr["rules"] if r["status"] != "PROVEN"] if vr else []
    return {
        "plane_1_formal_on_IR": {
            "what": "the verifier (Z3) proves each rule over canonical IR under the listed assumptions; "
                    "two-state laws and invariants hold across every sequence of atomic operations",
            "proven_rules": proven, "not_proven": other,
            "assumptions": [a["id"] for a in vr["assumptions"]] if vr else [],
            "does_not_prove": ["business correctness beyond the formalized rules",
                               "progress/liveness (every order completes)",
                               "fidelity of the IR-to-C# translation"]},
        "plane_2_validated_on_csharp": csharp or {
            "status": "NOT_RUN", "what": "C# conformance tests against the IR reference model; this is not a proof"},
        "plane_3_integration_operation": {
            "status": "OUT_OF_SCOPE_FOR_MVP",
            "items": ["step durability and atomicity (AS_1)", "concurrency (Q_4)", "real persistence", "performance",
                      "real process interruptions"]},
        "pending_design_decisions": model_pending,
    }


def build(path: Path, scope_path: Path | None, out: Path, timeout_ms: int = 60000, cache_path: Path | None = None) -> dict:
    t0 = time.perf_counter()
    out.mkdir(parents=True, exist_ok=True)
    src = path.read_text(encoding="utf-8")
    scope = scope_path.read_text(encoding="utf-8") if scope_path else None
    result = {"phase": "compile", "exit": 0}
    try:
        spec, ir, report, errors = compile_source(src, scope, find_root(path))
    except UpaError as e:
        write(out, "errors.json", [e.to_json()])
        return {"phase": "parse", "exit": 1, "errors": [e.to_json()]}

    for key, fname in (("extracted_rules", "extracted-rules.json"), ("semantic_constraints", "semantic-constraints.json"),
                       ("concept_classification", "concept-classification.json"),
                       ("inferred_model", "inferred-model.json"), ("model_ambiguities", "model-ambiguities.json"),
                       ("design_decisions", "design-decisions.json"), ("provenance", "provenance.json")):
        write(out, fname, report[key])
    if errors:
        write(out, "errors.json", errors.to_json())
        return {"phase": "semantic", "exit": 1, "errors": errors.to_json()}

    (out / "errors.json").unlink(missing_ok=True)
    write(out, "canonical-ir.json", ir)
    from .interaction import project
    write(out, "interaction-ir.json", project(ir))
    if ir.get("collections"):
        from .persistence import project as project_persistence
        write(out, "persistence-ir.json", project_persistence(ir))
    else:
        (out / "persistence-ir.json").unlink(missing_ok=True)
    cov = coverage(ir, scope) if scope else None
    if cov:
        write(out, "scope-coverage.json", cov)
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path and cache_path.exists() else ({} if cache_path else None)
    # children are verified first: the parent inherits their already-proven PRES/TYPE obligations (verifier.child_proof)
    child_results = []
    if spec.component_info:
        root = find_root(path)
        for c in spec.component_info:
            child = component_path(root, c["ref"])
            child_scope = child.parent / "informal-scope.md"
            child_results.append((c, build(child, child_scope if child_scope.exists() else None, child.parent, timeout_ms, cache_path)))
    children = {c["alias"]: {o["id"]: o["status"] for o in r.get("_obligations", [])} for c, r in child_results}
    vr = verify(ir, timeout_ms, cache, children)
    if spec.component_info:
        order = {"VERIFIED": 0, "INCOMPLETE": 1, "REFUTED": 2}
        vr["components"] = []
        for c, child_result in child_results:
            verdict = child_result.get("verdict", "INCOMPLETE")
            vr["components"].append({"alias": c["alias"], "ref": c["ref"], "version": c["version"],
                                     "verdict": verdict, "summary": child_result.get("summary", {}),
                                     "summary_total": child_result.get("summary_total", child_result.get("summary", {}))})
            if order.get(verdict, 1) > order[vr["verdict"]]:
                vr["verdict"] = verdict
                vr.setdefault("incompleteness_reasons", []).append(
                    f"component {c['alias']} ({c['ref']}@{c['version']}) is {verdict}")
    if spec.system_map_declared and not vr["rules"] and not spec.component_info:
        vr["verdict"] = "INCOMPLETE"
        vr["incompleteness_reasons"] = [
            "the system map contains descriptive architecture and observations but no formal behavioral rules"
        ]
    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache, sort_keys=True), encoding="utf-8")
    write(out, "verification-result.json", vr)
    write(out, "proof-obligations.json", vr["obligations"])
    write(out, "assumptions.json", vr["assumptions"])
    pending = [d["id"] for d in report["design_decisions"] if d.get("status") == "pending_human"]
    cs_path = out / "csharp-validation.json"
    csharp = json.loads(cs_path.read_text(encoding="utf-8")) if cs_path.exists() else None
    write(out, "evidence-ledger.json", evidence_ledger(ir, vr))
    write(out, "guarantee-scope.json", guarantee_scope(ir, vr, csharp, pending))
    metrics = {
        "semantic_model": report["metrics"],
        "verification": {"verdict": vr["verdict"], **vr["summary"], "z3": vr["tool"]["version"]},
        "sizes": {"upa_lines": len(src.splitlines()), "ir_bytes": len(canonical_json(ir)),
                  "operations": len(ir["operations"]), "properties": len(ir["properties"]),
                  "facts": len(ir["facts"]), "obligations": len(vr["obligations"])},
        "scope_coverage": cov["ratio"] if cov else None,
        "tokens": None,
        "build_seconds": round(time.perf_counter() - t0, 2),
        "scope_sha256": hashlib.sha256(scope.encode("utf-8")).hexdigest() if scope else None,
        "ir_hash": ir_hash(ir),
    }
    if spec.component_info:
        total = dict(vr["summary"])
        for c in vr["components"]:
            for k, v in c.get("summary_total", c["summary"]).items():
                total[k] = total.get(k, 0) + v
        vr["summary_total"] = total
        metrics["composition"] = {"depth": spec.component_depth, "summary_total": total,
                                  "components": [{k: c[k] for k in ("alias", "ref", "version", "verdict")}
                                                 for c in vr["components"]]}
        write(out, "verification-result.json", vr)
    write(out, "metrics.json", metrics)
    exit_code = {"VERIFIED": 0, "REFUTED": 2, "INCOMPLETE": 3}[vr["verdict"]]
    result = {"phase": "verify", "exit": exit_code, "verdict": vr["verdict"], "summary": vr["summary"],
              "pending_design_decisions": pending, "errors": vr["errors"],
              "_obligations": [{"id": o["id"], "status": o["status"]} for o in vr["obligations"]]}
    if "summary_total" in vr:
        result["summary_total"] = vr["summary_total"]
        result["components"] = vr["components"]
    if "incompleteness_reasons" in vr:
        result["incompleteness_reasons"] = vr["incompleteness_reasons"]
    return result
