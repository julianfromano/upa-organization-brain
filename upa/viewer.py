"""Build a self-contained, plain-language HTML viewer for UPA artifacts.

The viewer explains system behavior, workflows, states, rules, open questions, and
verification status for each rule.
"""
from __future__ import annotations

import csv
import html
import json
import re
import unicodedata
from pathlib import Path

OUTCOME_RE = re.compile(r"^If (.+?), the result is (\w+)(?: and (.+?))?\.?$", re.S)
OTHERWISE_RE = re.compile(r"^Otherwise,? (.+?)\.?$", re.S)
TRANS_RE = re.compile(r"^(?:A|An|The) (.+?) in state (.+?) can transition to state (.+?)\.?$")
STATES_RE = re.compile(r"^The state of (?:every|each) (.+?) can be (.+?)\.?$")
NEW_RE = re.compile(r"^(?:Every|Each) new (.+?) starts in state (.+?)\.?$")
EV_RE = re.compile(r"EV-[A-Z]+-\d+(?:\.\.\d+)?")


def _load(d: Path, name: str, default):
    p = d / name
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


def humanize(name: str) -> str:
    """Turn a technical identifier into a readable English label."""
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)
    s = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", s).replace("_", " ")
    words = [word for word in s.split() if word]
    if not words:
        return ""
    readable = [word if len(word) > 1 and word.isupper() else word.lower() for word in words]
    readable[0] = readable[0][:1].upper() + readable[0][1:]
    return " ".join(readable)


def _split_list(text: str) -> list[str]:
    parts = re.split(r",\s*| or | and ", text)
    return [p.strip() for p in parts if p.strip()]


def _has_system_map(d: Path) -> bool:
    ir = _load(d, "canonical-ir.json", {})
    return bool(ir.get("system_map", {}).get("components"))


def _key(s: str) -> str:
    s = unicodedata.normalize("NFD", s)
    return re.sub(r"[^a-z0-9]", "", "".join(c for c in s if not unicodedata.combining(c)).lower())


def _display_dir(d: Path) -> str:
    """Prefer a working-directory-relative path so viewers do not embed local user paths."""
    resolved = d.resolve()
    try:
        return str(resolved.relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(resolved)


def _expr(e: dict, params: dict) -> str:
    """Describe an IR expression in plain English."""
    k = e.get("k")
    if k == "var":
        p = params.get(e["n"])
        if p and p.get("generated_for"):
            return "assigned by the system"
        return f"the value entered for “{humanize(e['n'])}”"
    if k == "enum":
        return f"“{humanize(e['v'])}”"
    if k in ("int", "str", "bool"):
        return f"“{e['v']}”" if k == "str" else str(e["v"]).lower()
    if k == "attr":
        if e["e"].startswith("Counter"):
            return "the internal counter"
        return f"the current {humanize(e['a']).lower()} value for {humanize(e['e']).lower()}"
    if k == "bin":
        op = {"+": "plus", "-": "minus"}.get(e["op"], e["op"])
        return f"{_expr(e['l'], params)} {op} {_expr(e['r'], params)}"
    if k == "if":
        return f"if {_expr(e['c'], params)}, then {_expr(e['t'], params)}; otherwise {_expr(e['f'], params)}"
    return "a calculated value"


def _effects(ir: dict, ver: dict) -> dict:
    """Return each operation’s successful writes from the formal model, not from prose."""
    status = {}
    for o in ver.get("obligations", []):
        rule = o.get("rule") or ""
        if rule.startswith("BR_OK_"):
            status.setdefault(rule[6:], set()).add(o.get("status"))
    out = {}
    for op in ir.get("operations", []):
        params = {p["name"]: p for p in op["params"]}
        ok = next((c for c in op["cases"] if c.get("outcome") is None), None)
        if ok is None:
            continue
        items, tech = [], []
        for st in ok["stmts"]:
            ent = st.get("entity")
            technical = ent.startswith("Counter")
            kind = {"create": "creates", "delete": "deletes"}.get(st["kind"], "sets")
            if st.get("each"):
                kind += "_each"
            if st["kind"] == "delete":
                fields = []
            elif st["kind"] == "create":
                fields = [{"name": humanize(i["attr"]), "value": _expr(i["value"], params)} for i in st.get("inits", [])]
            else:
                fields = [{"name": humanize(st["attr"]), "value": _expr(st["value"], params)}]
            row = {"kind": kind, "entity": humanize(ent), "key": _expr(st["key"], params) if st.get("key") and not st.get("each") else None, "fields": fields}
            (tech if technical else items).append(row)
        sts = status.get(op["name"], set())
        out[_key(op["name"])] = {
            "op": op["name"],
            "inputs": [{"name": humanize(p["name"]), "generated": bool(p.get("generated_for")), "type": p.get("decl")} for p in op["params"]],
            "writes": items, "technical": tech,
            "status": "VERIFIED" if sts == {"PROVEN"} else ("REFUTED" if "REFUTED" in sts else ("DECLARED" if not sts else "INCONCLUSIVE")),
        }
    return out


FORMAL_KINDS = r"operation|query|compose|invariant|law|ensures|repeat|fact|question|decision|assumption|exclusion"


def _source_snippets(src: str) -> dict:
    """Return the literal UPA source for each formal declaration, keyed by its ID."""
    lines = src.splitlines()
    out = {}
    head = re.compile(rf"^(\s*)({FORMAL_KINDS})\s+(?:atomic\s+)?([A-Za-z_][A-Za-z0-9_]*)\b")

    def code(s: str) -> str:
        return re.sub(r'"(?:\\.|[^"\\])*"', '""', s).split("//", 1)[0]

    for i, line in enumerate(lines):
        m = head.match(line)
        if not m or m.group(3) in out:
            continue
        indent, block = len(m.group(1)), [line]
        c = code(line)
        parens, depth, braced = c.count("(") - c.count(")"), c.count("{") - c.count("}"), "{" in c
        j = i + 1
        while j < len(lines):
            nxt = lines[j]
            if parens <= 0 and depth <= 0:
                if braced or not nxt.strip() or len(nxt) - len(nxt.lstrip()) <= indent:
                    break
            block.append(nxt)
            c = code(nxt)
            parens += c.count("(") - c.count(")")
            depth += c.count("{") - c.count("}")
            braced = braced or "{" in c
            j += 1
        out[m.group(3)] = {"kind": m.group(2), "line": i + 1, "source": "\n".join(block)}
    return out


def _formal_elements(ir: dict, ver: dict, src: str) -> dict:
    snippets = _source_snippets(src) if src else {}
    obligations = ver.get("obligations", [])
    rules = {r["rule"]: r for r in ver.get("rules", [])}
    notes = ir.get("notes") or {}
    note_kind = {"questions": "question", "decisions": "decision", "assumptions": "assumption", "exclusions": "exclusion"}
    note_status = {"question": "OPEN", "decision": "DECIDED", "assumption": "ASSUMPTION", "exclusion": "EXCLUDED"}
    out = {}

    def summarize(obs):
        sts = {o.get("status") for o in obs}
        if not obs:
            return "NO_OBLIGATIONS"
        return "REFUTED" if "REFUTED" in sts else ("INCONCLUSIVE" if "UNKNOWN" in sts else "VERIFIED")

    def obl(obs):
        return [{"id": o["id"], "description": o.get("description"), "status": o.get("status")} for o in obs]

    for op in ir.get("operations", []):
        obs = [o for o in obligations if o.get("operation") == op["name"]]
        out[op["name"]] = {"kind": "operation", "text": None, "status": summarize(obs), "obligations": obl(obs)}
    for q in ir.get("queries", []):
        out[q["name"]] = {"kind": "query", "text": None, "status": "DEFINITION", "obligations": []}
    for c in ir.get("composes", []):
        obs = [o for o in obligations if o.get("operation") == c["name"]]
        out[c["name"]] = {"kind": "compose", "text": None, "status": summarize(obs), "obligations": obl(obs)}
    for rid, r in rules.items():
        obs = [o for o in obligations if o.get("rule") == rid]
        out[rid] = {"kind": r.get("kind"), "text": r.get("text"), "origin": r.get("origin"),
                    "assumptions": r.get("user_assumptions", []), "status": summarize(obs), "obligations": obl(obs)}
    for key, kind in note_kind.items():
        for n in notes.get(key, []):
            out.setdefault(n["id"], {"kind": kind, "text": n.get("text"), "status": note_status[kind], "obligations": []})
    for fid, s in snippets.items():
        if fid in out:
            out[fid]["source"] = s["source"]
            out[fid]["line"] = s["line"]
        elif s["kind"] == "fact":
            out[fid] = {"kind": "fact", "text": None, "status": "HECHO", "obligations": [], "source": s["source"], "line": s["line"]}
    for qname, e in out.items():
        if e["kind"] != "query":
            continue
        pat = re.compile(rf"\b{re.escape(qname)}\s*\(")
        e["used_in"] = sorted(fid for fid, x in out.items()
                              if fid != qname and x.get("source") and pat.search(x["source"]))
    return out


def _composition(d: Path, ir: dict, ver: dict, depth_guard: int) -> dict | None:
    comp = ir.get("composition")
    if not comp:
        return None
    from .inherit import find_root
    root = find_root(d)
    component_paths = [Path(c["path"]) for c in comp.get("components", []) if c.get("path")]
    if root is None or not all((root / path).exists() for path in component_paths):
        root = None
        search_bases = dict.fromkeys([d.resolve(), *d.resolve().parents, Path.cwd(), *Path.cwd().parents])
        for base in search_bases:
            candidates = [base / "rules"]
            if (base / "examples").exists():
                candidates.extend((base / "examples").glob("**/rules"))
            for candidate in candidates:
                if component_paths and all((candidate / path).exists() for path in component_paths):
                    root = candidate
                    break
            if root is not None:
                break
    verdicts = {c["alias"]: c for c in ver.get("components", [])}
    children = []
    for c in comp["components"]:
        child_dir = (root / c["path"]).parent if root else None
        data = None
        if child_dir and (child_dir / "canonical-ir.json").exists() and depth_guard < 64:
            data = build_data(child_dir, None, depth_guard + 1)
        children.append({"alias": c["alias"], "ref": c["ref"], "version": c["version"], "depth": c["depth"],
                         "exports": c.get("exports", []), "verdict": (verdicts.get(c["alias"]) or {}).get("verdict"),
                         "data": data})
    return {"depth": comp["depth"], "calls": comp.get("calls", []), "children": children}


def _sim_tree(composition: dict | None) -> list[dict]:
    out = []
    for c in (composition or {}).get("children", []):
        d = c.get("data") or {}
        out.append({"alias": c["alias"], "ref": c["ref"], "version": c["version"], "name": d.get("name"),
                    "tier": d.get("tier"), "verdict": c.get("verdict") or d.get("verdict"),
                    "terms": d.get("terms") or {}, "invocations": d.get("invocations") or [],
                    "stores": d.get("stores") or [], "gaps": (d.get("notes") or {}).get("language_gaps") or [],
                    "questions": (d.get("notes") or {}).get("questions") or [], "hdec": (d.get("notes") or {}).get("human_decisions") or [],
                    "children": _sim_tree(d.get("composition"))})
    return out


def _simulation(ir: dict, composition: dict | None) -> dict | None:
    """Data needed to run the exact IR semantics in the viewer, matching the verifier."""
    if not ir.get("operations"):
        return None
    targets = {o["name"] for o in ir["operations"]} | {c["name"] for c in ir.get("composes", [])}
    return {"operations": ir["operations"], "composes": ir.get("composes", []), "entities": ir["entities"],
            "enums": ir["enums"], "tree": _sim_tree(composition),
            "rules": [{"id": p["id"], "kind": p["kind"], "text": p.get("text"), "origin": p.get("origin"),
                       "targets": p.get("targets") or [], "expr": p["expr"]}
                      for p in ir.get("properties", [])
                      if p["kind"] in ("ensures", "law") and p.get("expr") is not None
                      and set(p.get("targets") or []) & targets and not re.search(r"(^|_)(F|TR)_", p["id"])]}


def _replays(d: Path) -> list[dict]:
    """Load `upa replay` results from <dir>/replay/ (real cases compared with the model)."""
    out = []
    for f in sorted(d.rglob("replay.json")):
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({"dir": str(f.parent.relative_to(d)), "spec": r.get("spec"), "case_count": r.get("case_count"),
                    "observation_count": r.get("observation_count"), "by_status": r.get("by_status"), "reproduction_pct": r.get("reproduction_pct"),
                    "rows": [{k: x.get(k) for k in ("label", "op", "n", "status", "explained_by", "expect")} for x in r.get("rows", [])][:80]})
    return out


def _ref_of(d: Path) -> str | None:
    from .inherit import find_root
    root = find_root(d)
    if not root:
        return None
    try:
        rel = d.resolve().relative_to(root)
    except ValueError:
        return None
    return ".".join(rel.parts) if rel.parts and (d / f"{rel.parts[-1]}.upa").exists() else None


def _used_by(d: Path) -> list[dict]:
    """Index other UPA specs that declare `component X = <this>@v`."""
    from .inherit import find_root
    ref, root = _ref_of(d), find_root(d)
    if not ref or not root:
        return []
    pat = re.compile(rf"^\s*component\s+(\w+)\s*=\s*{re.escape(ref)}@([\w.]+)", re.M)
    out = []
    for f in sorted(root.rglob("*.upa")):
        if "out" in f.relative_to(root).parts[:-1]:
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        for m in pat.finditer(text):
            spec = re.search(r"^\s*(?:spec|block)\s+(\w+)", text, re.M)
            out.append({"spec": spec.group(1) if spec else f.stem, "alias": m.group(1), "version": m.group(2),
                        "path": f.relative_to(root).as_posix()})
    return out


def _find_source(d: Path, spec_name: str | None) -> str:
    for cand in sorted(d.glob("*.upa")):
        text = cand.read_text(encoding="utf-8")
        if not spec_name or re.search(rf"^\s*(spec|block)\s+{re.escape(spec_name)}\b", text, re.M):
            return text
    return ""


def find_artifacts(path: Path) -> Path:
    path = Path(path)
    base = path if path.is_dir() else path.parent
    for cand in (base, base / "out"):
        if (cand / "normalized-surface.json").exists() or _has_system_map(cand) or (cand / "canonical-ir.json").exists():
            return cand
    raise FileNotFoundError(f"normalized-surface.json was not found in {base}; run `upa build` first")


def _usage(d: Path) -> dict:
    p = d / "endpoint-usage-30d.csv"
    if not p.exists():
        return {}
    rows = list(csv.DictReader(p.open(encoding="utf-8-sig")))
    return {"endpoints": len(rows), "with_traffic": sum(1 for r in rows if str(r.get("requests") or r.get("count") or r.get("calls") or "0").strip() not in ("", "0"))}


def _tree_data(composition):
    for c in (composition or {}).get("children", []):
        d = c.get("data") or {}
        yield d
        yield from _tree_data(d.get("composition"))


def _insight(ir, ver, composition, notes, replays, baseline):
    from . import insight as I
    kids = list(_tree_data(composition))
    allnotes = I._sum_notes([notes] + [k.get("notes") or {} for k in kids])
    stores = list({(s.get("entity"), s.get("table")): s for s in list(ir.get("stores", [])) + [s for k in kids for s in (k.get("stores") or [])]}.values())
    comps = (ir.get("system_map") or {}).get("components", [])
    cops = {c["name"]: [x["op"] for x in c.get("calls", [])] for c in ir.get("composes", [])}
    out = {"quality": I.quality(ir, ver, allnotes, replays), "refuted": I.refutations(ir, ver, allnotes),
           "impact": I.impact(ir, ver, cops, comps, stores, replays, allnotes), "diff": None}
    if baseline:
        names = {ir.get("spec")} | {k.get("name") for k in kids}
        out["diff"] = I.diff_trees(Path(baseline), Path(_ROOT_FOR_DIFF[0]), names)
    return out


_ROOT_FOR_DIFF = [None]


def build_data(artifacts: Path, source: str | None = None, _depth: int = 0, baseline: str | None = None) -> dict:
    d = Path(artifacts)
    surf = _load(d, "normalized-surface.json", {})
    ver = _load(d, "verification-result.json", {})
    caps = _load(d, "inferred-capabilities.json", [])
    model = _load(d, "inferred-model.json", {})
    impact = _load(d, "question-impact.json", [])
    rev = _load(d, "reverse-evidence.json", {})
    sents = surf.get("sentences", [])
    ir = _load(d, "canonical-ir.json", {})
    eff = _effects(ir, ver) if ir else {}
    ir = _load(d, "canonical-ir.json", {})
    system_map = ir.get("system_map") or None
    src_text = source if source is not None else _find_source(d, ir.get("spec"))
    formal = _formal_elements(ir, ver, src_text) if ir else {}
    composition = _composition(d, ir, ver, _depth) if ir else None
    notes = ir.get("notes") or {}
    if not surf and ir:
        surf = {"spec": ir.get("spec"), "version": ir.get("version"), "questions": notes.get("questions", [])}

    rules = ver.get("rules", [])
    by_ref: dict[str, list[dict]] = {}
    for r in rules:
        ref = (r.get("origin") or {}).get("ref") if isinstance(r.get("origin"), dict) else None
        if ref:
            by_ref.setdefault(ref.strip(), []).append(r)

    def proofs_for(text: str) -> list[dict]:
        t = text.strip()
        out = list(by_ref.get(t, []))
        if not out:
            for ref, rs in by_ref.items():
                if t.startswith(ref) or ref.startswith(t):
                    out += rs
        return out

    def status_of(rs: list[dict]) -> str:
        if not rs:
            return "DECLARED"
        st = {r.get("status") for r in rs}
        return "REFUTED" if "REFUTED" in st else ("INCONCLUSIVE" if "UNKNOWN" in st else "VERIFIED")

    # capabilities and workflows
    steps_by_cap: dict[str, list[dict]] = {}
    for s in sents:
        if s.get("section") != "outcomes":
            continue
        cap = s.get("capability") or ""
        text = s["text"]
        m = OUTCOME_RE.match(text)
        if m:
            effect = (m.group(3) or "").strip()
            steps_by_cap.setdefault(cap, []).append({"kind": "rejects", "when": m.group(1), "result": humanize(m.group(2)), "effect": effect,
                                                     "line": s.get("line"), "status": status_of(proofs_for(text))})
            continue
        m = OTHERWISE_RE.match(text)
        if m:
            steps_by_cap.setdefault(cap, []).append({"kind": "otherwise", "text": m.group(1), "line": s.get("line"), "status": status_of(proofs_for(text))})
        else:
            steps_by_cap.setdefault(cap, []).append({"kind": "note", "text": text, "line": s.get("line"), "status": status_of(proofs_for(text))})

    caps_out = []
    seen = set()
    for c in caps:
        n = c["capability"]
        seen.add(n)
        caps_out.append({"name": n, "kind": c.get("kind"), "concept": c.get("concept"), "steps": steps_by_cap.get(n, []), "effect": eff.get(_key(n))})
    for n, st in steps_by_cap.items():
        if n not in seen:
            caps_out.append({"name": n, "kind": None, "concept": None, "steps": st, "effect": eff.get(_key(n))})

    # States
    states: list[str] = []
    edges: list[dict] = []
    initial = None
    concept_of_states = None
    for s in sents:
        t = s["text"]
        m = STATES_RE.match(t)
        if m:
            concept_of_states = m.group(1)
            states = _split_list(m.group(2))
        m = NEW_RE.match(t)
        if m:
            initial = m.group(2).strip()
        m = TRANS_RE.match(t)
        if m:
            edges.append({"from": m.group(2).strip(), "to": m.group(3).strip(), "text": t, "status": status_of(proofs_for(t))})
    for e in edges:
        for x in (e["from"], e["to"]):
            if x not in states:
                states.append(x)
    produced = set()
    for c in caps_out:
        if c["kind"] == "transition":
            m = re.search(r" a (.+)$", c["name"])
            if m:
                produced.add(m.group(1).strip().lower())
    state_info = {"concept": concept_of_states, "states": states, "initial": initial, "edges": edges,
                  "unproduced": [s for s in states if s != initial and s.lower() not in produced] if edges else []}

    # Rules by group
    groups: dict[str, list[dict]] = {}
    for s in sents:
        if s.get("section") != "business":
            continue
        rs = proofs_for(s["text"])
        groups.setdefault(s.get("group") or "general", []).append({
            "id": s["id"], "text": s["text"], "status": status_of(rs), "checks": sum(len(r.get("obligations", [])) for r in rs),
            "evidence": sorted(set(EV_RE.findall(s["text"]))), "line": s.get("line")})
    initial_rules = [{"id": s["id"], "text": s["text"]} for s in sents if s.get("section") == "initial"]
    technical = [{"text": r["text"], "status": "VERIFIED" if r.get("status") == "PROVEN" else r.get("status"), "why": (r.get("origin") or {}).get("ref")}
                 for r in rules if r.get("group") != "business" and r.get("group") != "language"]

    # Concepts
    concepts = []
    for name, e in (model.get("entities") or {}).items():
        concepts.append({"name": name, "identity": e.get("identity"),
                         "props": [{"name": humanize(p["name"]), "type": p.get("type"), "required": bool(p.get("required"))} for p in e.get("properties", [])]})
    relations = [{"text": f"Each {humanize(r['from']).lower()} belongs to {'exactly one' if r.get('cardinality') == 'exactly_one' else 'one'} {humanize(r.get('to') or r.get('plays') or '').lower()}"} for r in model.get("relations", [])]

    imp = {i["id"]: i for i in impact}
    questions = [{"id": q["id"], "text": q["text"], "affects": [humanize(a) if " " not in a else a for a in (imp.get(q["id"]) or {}).get("affected_capabilities", [])],
                  "blocking": (imp.get(q["id"]) or {}).get("blocking")} for q in surf.get("questions", [])]

    ev_ledger = {e["id"]: e for e in rev.get("evidence", [])}
    summary = ver.get("summary") or {}
    insight = _insight(ir, ver, composition, notes, _replays(d) if _depth == 0 else [], baseline) if ir else None
    return {
        "insight": insight,
        "name": surf.get("spec") or d.name, "version": surf.get("version"), "verdict": ver.get("verdict", "UNVERIFIED"),
        "artifact_dir": _display_dir(d),
        "summary": summary, "by_category": ver.get("summary_by_category", {}),
        "capabilities": caps_out, "states": state_info, "groups": groups, "initial_rules": initial_rules, "technical": technical,
        "concepts": concepts, "relations": relations,
        "system_map": system_map, "formal": formal, "exclusions": notes.get("exclusions", []),
        "composition": composition, "level": (composition or {}).get("depth", 0), "ref": _ref_of(d), "tier": ir.get("tier"),
        "invocations": ir.get("invocations", []), "terms": ir.get("terms", {}), "stores": ir.get("stores", []),
        "async_producers": ir.get("async_producers", []),
        "async_consumers": ir.get("async_consumers", []),
        "async_correlations": ir.get("async_correlations", []),
        "regeneration_coverage": {
            **(ir.get("regeneration_coverage") or {
                "state": "not_evaluated", "declared_surfaces": [], "open_required": [],
                "basis": "declared_surfaces_only", "percentage": None,
            }),
            "verification_result": ver.get("verdict", "UNVERIFIED"),
        },
        "notes": {"assumptions": notes.get("assumptions", []), "language_gaps": notes.get("language_gaps", []), "questions": notes.get("questions", []), "human_decisions": notes.get("human_decisions", []), "parameters": notes.get("parameters", []), "risks": notes.get("risks", []), "concurrency": notes.get("concurrency", []), "concurrency_gaps": notes.get("concurrency_gaps", []), "persists": notes.get("persists", [])},
        "replays": _replays(d) if _depth == 0 else [],
        "execution": {c["name"]: c["execution"] for c in ir.get("composes", []) if c.get("execution")},
        "used_by": _used_by(d), "file": next((f.name for f in sorted(d.glob("*.upa"))
                                             if src_text and f.read_text(encoding="utf-8") == src_text), None),
        "summary_total": ver.get("summary_total"),
        "sim": _simulation(ir, composition) if ir else None,
        "questions": questions,
        "decisions": [{"id": x["id"], "text": x["text"], "alternatives": x.get("alternatives")} for x in surf.get("decisions", [])],
        "assumptions": [{"id": x["id"], "text": x["text"], "owner": x.get("owner")} for x in surf.get("assumptions", [])],
        "gaps": [{"id": x["id"], "text": x["text"], "proposal": x.get("proposal"), "evidence": x.get("evidence")} for x in surf.get("gaps", [])],
        "evidence": list(ev_ledger.values()), "evidence_rules": rev.get("rules", []),
        "origin": {"repo": rev.get("repo"), "dynatrace": rev.get("dynatrace")}, "usage": _usage(d),
    }


def render(systems: list[dict]) -> str:
    payload = json.dumps(systems, ensure_ascii=False).replace("</", "<\\/")
    cytoscape = (Path(__file__).with_name("vendor") / "cytoscape-3.34.4.min.js").read_text(encoding="utf-8")
    cytoscape = cytoscape.replace("</script", "<\\/script")
    return (HTML.replace("__DATA__", payload)
            .replace("__TITLE__", html.escape(" · ".join(s["name"] for s in systems)))
            .replace("__CYTOSCAPE__", cytoscape))


def build_viewer(paths: list[Path], out: Path, baseline: str | None = None) -> Path:
    systems = []
    for p in map(Path, paths):
        source = p.read_text(encoding="utf-8") if p.is_file() and p.suffix == ".upa" else None
        from .inherit import find_root
        _ROOT_FOR_DIFF[0] = str(find_root(find_artifacts(p)) or find_artifacts(p))
        data = build_data(find_artifacts(p), source, 0, baseline)
        data["viewer_key"] = p.stem if p.is_file() else p.name
        systems.append(data)
    if systems and systems[0].get("tier") == "macroprocess" and len(systems) > 1:
        systems[0]["external_systems"] = [
            {"key": system["viewer_key"], "index": index, "name": system["name"]}
            for index, system in enumerate(systems[1:], start=1)
        ]
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(systems), encoding="utf-8")
    return out


HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>UPA Viewer · __TITLE__</title>
<style>
:root{--ink:#1f2937;--mut:#6b7280;--bg:#f6f7f9;--card:#fff;--line:#e5e7eb;--ok:#166534;--okbg:#dcfce7;--bad:#991b1b;--badbg:#fee2e2;--warn:#92400e;--warnbg:#fef3c7;--acc:#1d4ed8;--accbg:#dbeafe}
*{box-sizing:border-box}body{margin:0;font:16px/1.5 system-ui,Segoe UI,sans-serif;color:var(--ink);background:var(--bg)}
header{background:#fff;border-bottom:1px solid var(--line);padding:14px 24px;display:flex;gap:16px;align-items:center;flex-wrap:wrap}
header h1{font-size:20px;margin:0}header select{font-size:15px;padding:4px}
nav{display:flex;gap:4px;padding:0 24px;background:#fff;border-bottom:1px solid var(--line);overflow:auto}
nav button{border:0;background:none;padding:12px 14px;font-size:15px;cursor:pointer;border-bottom:3px solid transparent;color:var(--mut);white-space:nowrap}
nav button.on{color:var(--acc);border-color:var(--acc);font-weight:600}
main{max-width:1100px;margin:0 auto;padding:24px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 20px;margin:0 0 16px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}
.big{font-size:32px;font-weight:700}.mut{color:var(--mut)}.small{font-size:13px}
.badge{display:inline-block;border-radius:999px;padding:2px 10px;font-size:12px;font-weight:600;white-space:nowrap}
.ok{background:var(--okbg);color:var(--ok)}.bad{background:var(--badbg);color:var(--bad)}.warn{background:var(--warnbg);color:var(--warn)}.info{background:var(--accbg);color:var(--acc)}
h2{margin:0 0 4px;font-size:22px}h3{margin:20px 0 8px;font-size:17px}
input.q{width:100%;padding:10px 12px;font-size:15px;border:1px solid var(--line);border-radius:8px;margin-bottom:14px}
.rule{display:flex;gap:10px;align-items:flex-start;padding:10px 0;border-bottom:1px solid var(--line)}.rule:last-child{border:0}.rule .t{flex:1}
details>summary{cursor:pointer;font-weight:600}
.flow{display:flex;flex-direction:column;align-items:center;gap:0;margin:12px 0}
.box{border:2px solid var(--line);border-radius:10px;padding:8px 14px;max-width:640px;width:100%;text-align:center;background:#fff}
.start{border-color:var(--acc);background:var(--accbg);font-weight:600}
.arrow{color:var(--mut);font-size:20px;line-height:20px}
.hmap{display:flex;align-items:center;gap:0;overflow-x:auto;padding:10px 4px 14px;margin:8px 0;border:1px dashed var(--line);border-radius:10px;background:#fafbfc}
.hstage{display:flex;flex-direction:column;gap:8px;align-items:stretch;min-width:190px;max-width:240px;flex:0 0 auto;position:relative}
.hstage.par{border:2px solid #f59e0b;border-radius:10px;padding:18px 6px 6px;background:#fffbeb}
.hstage.par:before{content:"in parallel";position:absolute;top:2px;left:8px;font-size:11px;font-weight:600;color:#b45309}
.hstage.alt{border:2px solid #0ea5e9;border-radius:10px;padding:18px 6px 6px;background:#f0f9ff}
.hstage.alt:before{content:"◇ conditional branch";position:absolute;top:2px;left:8px;font-size:11px;font-weight:600;color:#0369a1}
.hnode.skip{border-style:dotted;opacity:.75;background:#f8fafc}
.hlink{flex:0 0 auto;width:54px;text-align:center;font-size:11px;color:var(--mut);position:relative}
.hlink .ln{border-top:2px solid #64748b;margin:0 2px;position:relative;height:0}
.hlink .ln:after{content:"";position:absolute;right:-2px;top:-6px;border:5px solid transparent;border-left:8px solid #64748b}
.hlink.async .ln{border-top-style:dashed;border-top-color:#a855f7}.hlink.async .ln:after{border-left-color:#a855f7}.hlink.async{color:#7e22ce}
.hnode{border:2px solid #94a3b8;border-left-width:6px;border-radius:9px;background:#fff;padding:6px 8px;font-size:13px;text-align:left;cursor:pointer}
.hnode:hover{box-shadow:0 0 0 3px #bfdbfe}
.hnode.async{border-style:dashed}
.hnode.fan{box-shadow:4px 4px 0 -1px #fff,4px 4px 0 1px #94a3b8,8px 8px 0 -1px #fff,8px 8px 0 1px #94a3b8;margin:0 8px 8px 0}
.hnode .op{font-weight:700;word-break:break-word}.hnode .rf{font-size:11px;color:var(--mut);word-break:break-all}
.hterm{flex:0 0 auto;border-radius:20px;padding:6px 12px;font-size:12px;font-weight:600;background:#e2e8f0}
.ext{display:inline-flex;align-items:center;gap:3px;font-size:11px;border:1px solid #cbd5e1;border-radius:12px;padding:1px 7px;margin:3px 3px 0 0;background:#f8fafc;white-space:nowrap;max-width:100%;overflow:hidden;text-overflow:ellipsis}
.ext.database{background:#ecfeff;border-color:#67e8f9}.ext.api{background:#fef2f2;border-color:#fca5a5}.ext.service{background:#eff6ff;border-color:#93c5fd}
.ext.queue{background:#faf5ff;border-color:#d8b4fe}.ext.file{background:#fefce8;border-color:#fde047}.ext.directory{background:#f0fdf4;border-color:#86efac}.ext.mail{background:#fff7ed;border-color:#fdba74}
.ext.am{border-style:dashed}
.legend2{display:flex;flex-wrap:wrap;gap:6px;align-items:center;font-size:12px;margin:6px 0}
.dec{display:flex;gap:12px;align-items:stretch;width:100%;max-width:900px;justify-content:center}
.dec .q{flex:1;max-width:420px;border:2px solid #f59e0b;background:var(--warnbg);border-radius:10px;padding:8px 12px;text-align:center}
.dec .no{flex:1;max-width:420px;border:2px solid #ef4444;background:var(--badbg);border-radius:10px;padding:8px 12px;display:flex;flex-direction:column;justify-content:center;text-align:center}
.lbl{font-size:12px;color:var(--mut)}
.end{border-color:#22c55e;background:var(--okbg);font-weight:600}
svg text{font:13px system-ui}.node{cursor:pointer}.node rect{fill:#fff;stroke:#94a3b8;stroke-width:2}.node.init rect{stroke:var(--acc);stroke-width:3}.node.dim{opacity:.25}.edge{stroke:#94a3b8;fill:none;stroke-width:1.6}.edge.hl{stroke:var(--acc);stroke-width:2.6}.edge.dim{opacity:.12}
table{border-collapse:collapse;width:100%}td,th{border:1px solid var(--line);padding:6px 10px;text-align:left;font-size:14px}th{background:#f3f4f6}
.chip{display:inline-block;background:#eef2ff;color:#3730a3;border-radius:6px;padding:0 6px;font-size:12px;margin-right:4px;cursor:pointer}
.async-anchor{display:inline-block;margin-left:6px;padding:2px 7px;border:1px solid #a855f7;border-radius:999px;background:#faf5ff;color:#6b21a8;font-size:11px;font-weight:700;vertical-align:middle}
.async-band{list-style:none;margin:12px 0!important;padding:10px;border:2px dashed #a855f7;border-radius:10px;background:#faf5ff;color:#581c87}
.async-band-grid{display:grid;grid-template-columns:minmax(150px,1fr) 28px minmax(190px,1.25fr) 28px minmax(150px,1fr);align-items:center;gap:6px}
.async-edge{font-size:12px;line-height:1.35}.async-edge strong{display:block}.async-arrow{text-align:center;font-size:20px;font-weight:700;color:#7e22ce}
.async-message{padding:8px;border:1px solid #c084fc;border-radius:8px;background:#fff;text-align:center}.async-message code{display:block;margin:2px 0 5px;font-weight:700;word-break:break-word}
.async-fanout{display:flex;flex-direction:column;gap:8px}
.async-badge{display:inline-block;padding:2px 7px;border-radius:999px;background:#ede9fe;color:#5b21b6;font-size:11px;font-weight:700}
.formal-definitions{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin-top:7px!important}
.formal-definition{display:inline-flex;align-items:center;gap:3px;color:var(--mut)}
.formal-help{position:relative;width:19px;height:19px;padding:0;border:1px solid #7c3aed;border-radius:50%;background:#faf5ff;color:#6b21a8;font:700 12px/17px system-ui;cursor:help;text-align:center}
.formal-help:hover,.formal-help:focus-visible{outline:2px solid #c4b5fd;outline-offset:2px}
.formal-tooltip{display:none;position:absolute;z-index:30;left:50%;bottom:calc(100% + 8px);transform:translateX(-50%);width:max-content;max-width:min(380px,80vw);padding:9px 11px;border:1px solid #a855f7;border-radius:8px;background:#fff;color:#3b0764;box-shadow:0 8px 24px #581c8730;font:400 12px/1.4 system-ui;text-align:left;white-space:normal}
.formal-help:hover .formal-tooltip,.formal-help:focus .formal-tooltip,.formal-help:focus-visible .formal-tooltip{display:block}
.process-map{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px;margin:12px 0}
.process-node{display:flex;flex-direction:column;align-items:flex-start;min-height:84px;padding:10px 12px;border:2px solid #7c3aed;border-radius:10px;background:#fff;color:#312e81;text-align:left;cursor:pointer}
.process-node:hover,.process-node:focus-visible{background:#f5f3ff;box-shadow:0 0 0 3px #ddd6fe;outline:none}
.process-node.selected{background:#ede9fe;box-shadow:0 0 0 3px #c4b5fd;border-color:#5b21b6}
.process-node.behavior-node{border-color:#0f766e;background:#f0fdfa;color:#134e4a}
.process-node.behavior-node:hover,.process-node.behavior-node:focus-visible{background:#ccfbf1;box-shadow:0 0 0 3px #99f6e4}
.process-node.behavior-node.selected{background:#99f6e4;box-shadow:0 0 0 3px #5eead4;border-color:#115e59}
.process-node b{font-size:16px}.process-node span{font-weight:650}.process-node small{margin-top:auto;color:var(--mut)}
.process-graph{position:relative;height:500px;margin:8px 0;border:1px solid #ddd6fe;border-radius:12px;background:linear-gradient(#fff,#fafaff);overflow:hidden}.cy-graph{position:absolute;inset:0}.graph-fallback{display:none}
.graph-legend{position:absolute;z-index:4;left:10px;bottom:9px;display:flex;gap:10px;padding:5px 8px;border:1px solid var(--line);border-radius:8px;background:#fffffff0;font-size:11px;color:var(--mut);pointer-events:none}.graph-legend i{display:inline-block;width:25px;border-top:2px solid #2563eb;vertical-align:middle}.graph-legend i.async{border-top-color:#7c3aed;border-top-style:dashed}
.drill-prev{position:absolute;z-index:5;right:12px;top:12px;width:min(260px,42%);padding:8px 10px;border:1px solid #c4b5fd;border-radius:9px;background:#fffffff2;color:var(--ink);text-align:left;box-shadow:0 4px 16px #312e8120;cursor:pointer}.drill-prev:hover,.drill-prev:focus-visible{outline:3px solid #ddd6fe}.drill-prev b,.drill-prev small{display:block}.drill-prev .preview-nodes{display:flex;gap:4px;overflow:hidden;margin-top:5px}.drill-prev .preview-nodes span{flex:0 0 auto;max-width:74px;padding:2px 5px;border-radius:5px;background:#ede9fe;color:#5b21b6;font-size:10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.process-breadcrumb{display:flex;align-items:center;gap:8px;margin-bottom:10px}
.drill-shell{border-left:5px solid #7c3aed;overflow:hidden}.drill-stage{animation:drill-in .18s ease-out}.drill-path{display:flex;flex-wrap:wrap;gap:5px;align-items:center;margin-bottom:12px;padding-bottom:10px;border-bottom:1px solid var(--line)}
.drill-path button{border:0;background:transparent;color:#5b21b6;text-decoration:underline;cursor:pointer;padding:2px 4px}.drill-path .current{font-weight:700;color:var(--ink);padding:2px 4px}
.drill-story{margin-top:16px;border-top:3px solid #7c3aed;padding-top:12px}.drill-empty{padding:10px;border:1px dashed #94a3b8;border-radius:8px;color:var(--mut)}
.whatif-ai{margin-top:16px;padding:14px;border:1px solid #c4b5fd;border-radius:10px;background:#faf5ff}.whatif-ai textarea{display:block;width:100%;min-height:76px;margin:8px 0;padding:9px;border:1px solid #a78bfa;border-radius:7px;font:inherit;resize:vertical}.whatif-actions{display:flex;gap:8px;flex-wrap:wrap}.whatif-status{margin-top:7px;color:#5b21b6}
@keyframes drill-in{from{opacity:.35;transform:translateX(12px)}to{opacity:1;transform:none}}
@media(max-width:700px){.async-band-grid{grid-template-columns:1fr}.async-arrow{transform:rotate(90deg)}}
.help{background:var(--accbg);border-radius:8px;padding:10px 14px;margin-bottom:16px;font-size:14px}
@media print{nav,header select,input.q{display:none}.card{break-inside:avoid}}
pre.code{background:#0f172a;color:#e2e8f0;padding:12px 14px;border-radius:8px;overflow:auto;font:13px/1.45 Consolas,monospace;white-space:pre}
ul.qt{margin:2px 0 2px 4px;padding-left:16px;font-size:13px;border-left:2px solid var(--line)}ul.qt li{margin:1px 0}b.val{color:var(--acc)}
button.link{border:1px solid var(--acc);background:var(--accbg);color:var(--acc);border-radius:8px;padding:6px 12px;font-size:14px;cursor:pointer;margin-top:8px}
</style></head><body>
<header><h1>Visor UPA</h1><select id="sys" aria-label="System"></select><span id="hdr" class="mut"></span></header>
<nav id="nav" role="tablist"></nav><main id="main"></main>
<style>ul.relcond{margin:2px 0 4px 20px;padding:0;font-weight:400}ul.relcond li{margin:1px 0}ul.rel{margin:4px 0 4px 18px;padding:0}ol.rel>li,ol .relgrp,ol .relstep{margin:6px 0}.repeat-block{border:2px solid #f59e0b;border-radius:8px;padding:8px 10px;background:#fff}.relg{font-size:.78em;color:#92400e;background:#fef3c7;display:block;width:fit-content;margin-bottom:3px;padding:1px 6px;border-radius:4px;margin-bottom:2px}button.link.on{font-weight:bold;text-decoration:underline}
.fz{display:grid;grid-template-columns:minmax(120px,1fr) 34px minmax(170px,1.3fr) minmax(160px,1.2fr);align-items:stretch;margin:8px 0 2px;font-size:.92em}
.fz-in{border:1px solid var(--acc);background:var(--accbg);border-radius:8px 0 0 8px;padding:6px 10px;display:flex;flex-direction:column;justify-content:center}
.fz-end{background:repeating-linear-gradient(45deg,#dc2626 0 6px,#b91c1c 6px 12px);border-radius:2px;display:flex;align-items:center;justify-content:center;box-shadow:0 0 0 2px #fecaca}
.fz-end span{writing-mode:vertical-rl;transform:rotate(180deg);color:#fff;font-size:9px;font-weight:700;letter-spacing:.08em}
.fz-arrows{display:flex;flex-direction:column;justify-content:center;gap:6px;padding:4px 8px;color:#b91c1c}
.fz-go,.fz-back{display:flex;align-items:center;gap:4px;white-space:nowrap;font-size:.85em}.fz-back{color:var(--ink);white-space:normal}
.fz-line{flex:1;min-width:20px;border-top:2px dashed currentColor}.fz-none{color:var(--mut);font-style:italic}
.fz-out{border:2px dashed #9ca3af;background:repeating-linear-gradient(135deg,#f9fafb 0 8px,#f3f4f6 8px 16px);border-radius:0 12px 12px 0;padding:6px 10px;display:flex;flex-direction:column;gap:2px;color:#374151}
.fz-ev{margin:0 0 8px 4px}
.fz-chips{margin:3px 0 0}.fz-chip{display:inline-flex;align-items:center;gap:4px;font-size:.78em;margin:2px 6px 0 0;padding:1px 8px 1px 0;border:1px dashed #9ca3af;border-left:none;border-radius:0 10px 10px 0;background:#f9fafb;color:#374151}
.fz-chip i{display:inline-block;width:4px;align-self:stretch;background:#dc2626;margin-right:4px}.fz-chip.fz-closed{border-color:#86efac;background:var(--okbg);color:var(--ok)}.fz-chip.fz-closed i{background:#16a34a}</style>
<script>__CYTOSCAPE__</script>
<script>
const SYSTEMS=__DATA__;
const FULL_TABS=[["summary","Summary"],["flows","Behavior and failure paths"],["states","Lifecycle states"],["rules","Business rules"],["concepts","Concepts"],["questions","Open questions and decisions"],["evidence","Evidence"]];
const MAP_TAB=["map","System map"];
const FORMAL_TAB=["formal","Formal model"];
const LEVEL_TAB=["levels","Levels and components"];
const FLOW_TAB=["component-flow","Component workflows"];
const SIM_TAB=["simulate","Simulate"];
const tabsOf=s=>{const ex=[...(s.sim?[RELATO_TAB,SIM_TAB]:[]),...(s.insight?[CAL_TAB,IMP_TAB,...(s.insight.diff?[DIF_TAB]:[])]:[]),...(Object.values(s.formal||{}).some(e=>e.kind=="compose"&&e.source)?[FLOW_TAB]:[]),...((s.composition||(s.used_by||[]).length||(s.external_systems||[]).length)?[LEVEL_TAB]:[])];if(s.system_map)return s.capabilities.length?[MAP_TAB,...ex,FORMAL_TAB,...FULL_TABS]:[MAP_TAB,...ex,FORMAL_TAB];return [...ex,...(Object.keys(s.formal||{}).length?[FORMAL_TAB]:[]),...FULL_TABS]};
let fcomp=null;
const DRILL={system:0,path:[]};
let DETAIL_PATH=[];
let cur=0,tab=location.hash.slice(1)||((SYSTEMS[0].external_systems||[]).length?"narrative":(SYSTEMS[0].sim?"narrative":"summary"));const $=s=>document.querySelector(s);
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const cap=s=>s?s[0].toUpperCase()+s.slice(1):s;
const badge=s=>({VERIFIED:'<span class="badge ok">✔ Verified</span>',REFUTED:'<span class="badge bad">✖ Refuted</span>',INCONCLUSIVE:'<span class="badge warn">? Inconclusive</span>',DECLARED:'<span class="badge info" title="Describes what exists or is allowed; the rules that protect it are verified separately">Declared</span>'}[s]||"");
const chips=t=>(String(t).match(/EV-[A-Z]+-\d+(?:\.\.\d+)?/g)||[]).map(e=>`<span class="chip" onclick="goEv('${e.split('..')[0]}')">${e}</span>`).join("");
function goEv(id){tab="evidence";draw();setTimeout(()=>{const el=document.getElementById(id);if(el){el.scrollIntoView({block:"center"});el.style.outline="3px solid #f59e0b"}},50)}
function nav(){const T=tabsOf(S());if(!T.some(x=>x[0]==tab))tab=T[0][0];$("#nav").innerHTML=T.map(([k,l])=>`<button role="tab" class="${k==tab?'on':''}" onclick="tab='${k}';draw()">${l}</button>`).join("")}
function S(){return SYSTEMS[cur]}
function openSystem(i){if(i<0||i>=SYSTEMS.length)return;cur=i;fcomp=null;DETAIL_PATH=[];tab=SYSTEMS[i].sim?"narrative":"summary";$("#sys").value=i;draw()}
function detailData(s){let d=s;for(const alias of DETAIL_PATH){const c=drillChild(d,alias);if(!c||!c.data)break;d=c.data}return d}
function openDrillDetail(view){if(DRILL.system){cur=DRILL.system;DETAIL_PATH=DRILL.path.slice();$("#sys").value=cur}tab=view;draw()}
function returnToDrill(){cur=0;DETAIL_PATH=[];tab="narrative";$("#sys").value=0;draw()}
function detailBanner(s){if(!cur&&!DETAIL_PATH.length)return "";const d=detailData(s),path=[SYSTEMS[cur].viewer_key||SYSTEMS[cur].name,...DETAIL_PATH];
 return `<div class="process-breadcrumb"><button type="button" class="link" onclick="returnToDrill()">← Return to this map location</button><b>${path.map(esc).join(" › ")}</b><span class="mut">${esc(d.name)}</span></div>`}
function activeRuleContext(s){return !cur&&DRILL.system?drillData(DRILL.system,DRILL.path):detailData(s)}
function activeRuleBanner(s){if(!DRILL.system&& !cur)return "";const d=activeRuleContext(s),path=!cur?[SYSTEMS[DRILL.system].viewer_key||SYSTEMS[DRILL.system].name,...DRILL.path]:[SYSTEMS[cur].viewer_key||SYSTEMS[cur].name,...DETAIL_PATH];
 return `<div class="process-breadcrumb"><button type="button" class="link" onclick="returnToDrill()">← Return to this map location</button><span class="badge info">What-if for</span><b>${path.map(esc).join(" › ")}</b><span class="mut">${esc(d.name)}</span></div>`}
function draw(){nav();const s=S(),main=$("#main");$("#hdr").textContent=`version ${s.version||"?"}`;main.innerHTML=VIEWS[tab](s);
 main.onclick=e=>{const target=e.target.closest? e.target.closest("[data-behavior-name],[data-open-system],[data-drill-system],[data-drill-path]"):null;if(!target)return;e.preventDefault();
   if(target.dataset.behaviorName!==undefined){REL_SEL[target.dataset.behaviorKey]=target.dataset.behaviorName;draw();return}  if(target.dataset.drillSystem!==undefined){DRILL.system=Number(target.dataset.drillSystem);DRILL.path=[];impSel="";draw();return}
  if(target.dataset.drillPath!==undefined){DRILL.path=JSON.parse(decodeURIComponent(target.dataset.drillPath));impSel="";draw();return}
  openSystem(Number(target.dataset.openSystem))};
  if(tab=="states")stateEvents();if(typeof requestAnimationFrame=="function")requestAnimationFrame(mountGraphs);window.scrollTo(0,0)}
const RK={validation:"Validation",decision:"Decision",calculation:"Calculation",effect:"Effect",error_mapping:"Error",state_transition:"State transition"};
const ruleHtml=r=>`<div class="rule" data-t="${esc((r.id+" "+r.when.join(" ")+" "+r.then.join(" ")+" "+(r.error||"")).toLowerCase())}"><div class="t"><span class="badge info">${esc(RK[r.kind]||r.kind)}</span> ${r.when.length?`<b>If</b> ${r.when.map(esc).join(" <b>and</b> ")} `:""}<b>then</b> ${r.then.map(esc).join("; ")}${r.error?`<div class="small">Error/message: <code>${esc(r.error)}</code></div>`:""}<div class="small mut">${esc(r.id)} · ${esc(r.source)} · <code>${esc(r.evidence)}</code></div></div>${stb(r.status)}</div>`;
const KIND={web_app:"Web application",service:"Service",gateway:"Gateway",batch:"Batch process",library:"Library",channel:"Channel",external:"External",database:"Database",scheduler:"Scheduler",etl:"ETL",report:"Reports"};
const ST={source_verified:["ok","Verified in source code"],runtime_observed:["ok","Observed at runtime"],production_observed:["ok","Observed in production"],documented:["info","Documented"],inferred:["warn","Inferred"]};
const stb=x=>{const v=ST[x]||["info",x];return `<span class="badge ${v[0]}">${esc(v[1])}</span>`};
const USE={traffic_observed:["ok","Traffic observed"],consumer_known:["ok","Known consumer"],no_known_consumer:["warn","No known consumer"],not_exercised:["warn","Not exercised"]};
function systemMap(s){const m=Object.assign({components:[],contracts:[],flows:[],catalogs:[],jobs:[],observations:[],rules:[]},s.system_map),cn={};m.components.forEach(c=>cn[c.id]=c);
const RS={};m.rules.forEach(r=>(RS[r.subject]=RS[r.subject]||[]).push(r));const rcount=id=>(RS[id]||[]).length;
const inc=m.components.filter(c=>c.scope=="included"),bnd=m.components.filter(c=>c.scope!="included");
const obsOf=id=>m.observations.filter(o=>o.subject==id),flowsOf=id=>m.flows.filter(f=>f.steps.some(x=>x.component==id)),ctrOf=id=>m.contracts.filter(k=>k.owner==id);
const targetSystem=c=>SYSTEMS.findIndex(x=>x.viewer_key==c.id);
const card=c=>{const os=obsOf(c.id),fs=flowsOf(c.id),ks=ctrOf(c.id),target=targetSystem(c);return `<details class="card"><summary><b>${esc(c.name)}</b> <span class="badge info">${esc(KIND[c.kind]||c.kind)}</span> <span class="small mut">${esc(c.role)}</span></summary>${c.runtime?`<p class="small"><b>Observed usage:</b> ${esc(c.runtime)}</p>`:""}${(c.depends_on||[]).length?`<div class="small mut">Depends on: ${c.depends_on.map(d=>esc((cn[d]||{}).name||d)).join(", ")}</div>`:""}${os.length?`<h4>What it means and how it works</h4>${os.map(o=>`<div class="rule"><div class="t">${esc(o.statement)}<div class="small mut">${esc(o.evidence)}</div></div>${stb(o.status)}</div>`).join("")}`:""}${fs.length?`<h4>Participates in workflows</h4>${fs.map(f=>`<div class="small">• ${esc(f.name)} ${stb(f.status)}: ${f.steps.filter(x=>x.component==c.id).map(x=>esc(x.operation)).join("; ")}</div>`).join("")}`:""}${ks.length?`<h4>Published contracts</h4>${ks.map(k=>`<div class="small">• ${esc(k.service)} · ${esc(k.operation)}</div>`).join("")}`:""}${repo(c)}<div class="small mut" style="margin-top:6px">Evidence: ${esc(c.evidence)}</div>${target>=0?`<button type="button" class="link" data-open-system="${target}">Open full process →</button>`:""}${(c.realizes||[]).length?`<button class="link" onclick="fcomp='${c.id}';tab='formal';draw()">View in the formal model (${c.realizes.length}) →</button>`:`<div class="small mut" style="margin-top:6px">No linked formal elements.</div>`}</details>`};
const repo=c=>c.repository?`<div class="small mut">Repository: ${esc(c.repository)}</div>`:"";
const bySvc={};m.contracts.forEach(k=>(bySvc[k.service]=bySvc[k.service]||[]).push(k));
const contracts=Object.entries(bySvc).map(([svc,ks])=>`<details class="card"><summary>${esc(svc)} · ${ks.length} operation(s) · ${esc((cn[ks[0].owner]||{}).name||ks[0].owner)}</summary><table><tr><th>Operation</th><th>Input</th><th>Usage</th><th>Rules</th></tr>${ks.map(k=>{const u=USE[k.usage]||["info",k.usage];return `<tr><td>${esc(k.operation)}</td><td class="small">${esc(k.request)}</td><td><span class="badge ${u[0]}">${esc(u[1])}</span></td><td>${rcount(k.id)}</td></tr>`}).join("")}</table><div class="small mut">Protocol: ${esc(ks[0].protocol)} · sha256 ${esc((ks[0].sha256||"").slice(0,12))}… · ${esc(ks[0].source)}</div></details>`).join("");
const flowCard=f=>`<details class="card"><summary>${esc(f.name.replace(/^\[[^\]]+\]\s*/,""))} ${stb(f.status)} <span class="small mut">${f.steps.length} steps${rcount(f.id)?`, ${rcount(f.id)} rules`:""}</span></summary><div class="flow">${f.steps.map((x,i)=>`${i?'<div class="arrow">↓</div>':""}<div class="box ${i?"":"start"}"><b>${esc((cn[x.component]||{}).name||x.component)}</b><br><span class="small">${esc(x.operation)}</span>${x.contract?` <span class="chip">${esc(x.contract)}</span>`:""}</div>`).join("")}</div>${(RS[f.id]||[]).length?`<h4>Atomic rules</h4>${RS[f.id].map(ruleHtml).join("")}`:""}<div class="small mut">Evidence: ${esc(f.evidence)}</div></details>`;
const grp={};m.flows.forEach(f=>{const g=(f.name.match(/^\[([^\]]+)\]/)||[0,"Other"])[1];(grp[g]=grp[g]||[]).push(f)});
const flows=Object.entries(grp).map(([g,fs])=>`<h3>${esc(g)} (${fs.length})</h3>`+fs.map(flowCard).join("")).join("");
const cats=m.catalogs.map(c=>`<details class="card"><summary>${esc(c.source)} · ${c.values.length} values ${stb(c.status)}</summary><table><tr><th>Name</th><th>Code</th><th>Meaning</th></tr>${c.values.map(v=>`<tr><td>${esc(v.name)}</td><td>${esc(v.code||"")}</td><td>${esc(v.meaning||"—")}</td></tr>`).join("")}</table></details>`).join("");
const jobs=`<table><tr><th>Job</th><th>Type</th><th>Reads</th><th>Writes</th><th>Status</th></tr>${m.jobs.map(j=>`<tr><td>${esc(j.name)}</td><td>${esc(j.kind)}</td><td class="small">${j.reads.map(esc).join(", ")}</td><td class="small">${j.writes.map(esc).join(", ")}</td><td>${stb(j.status)}</td></tr>`).join("")}</table>`;
const obs=m.observations.map(o=>`<div class="rule"><div class="t">${esc(o.statement)}<div class="small mut">${esc((cn[o.subject]||{}).name||o.subject)} · ${esc(o.evidence)}</div></div>${stb(o.status)}</div>`).join("");
const allRules=m.rules||[];
const ruleSec=allRules.length?`<h2>Atomic rules (${allRules.length})</h2><div class="help">Each rule is a condition and its result, copied from decompiled source with its file and method of origin.</div><input class="q" placeholder="Search rules… (e.g. document, date, error)" oninput="filt(this.value)">`+Object.entries(RS).map(([sid,rs])=>`<details class="card"><summary>${esc((cn[sid]||{}).name||sid)} · ${rs.length} rules</summary>${rs.map(ruleHtml).join("")}</details>`).join(""):"";
const qs=s.questions.map(q=>`<div class="rule"><div class="t"><b>${esc(q.id)}</b> ${esc(q.text)}</div><span class="badge warn">Open</span></div>`).join("");
const ex=(s.exclusions||[]).map(q=>`<div class="rule"><div class="t"><b>${esc(q.id)}</b> ${esc(q.text)}</div><span class="badge bad">Excluded</span></div>`).join("");
const ATD={at_least_once:"at least once",at_most_once:"at most once",exactly_once:"exactly once",unspecified:"unspecified"};
const AP=s.async_producers||[],AC=s.async_consumers||[],AR=s.async_correlations||[];
const consumerBy=id=>AC.find(x=>x.id==id),producerBy=id=>AP.find(x=>x.id==id);
const consumerBox=(s,c)=>`<div class="box" data-async-consumer="${esc(c.id)}"><b>${esc(cap(L(s,...opScope(s,c.operation))))}</b><div class="async-anchor">↯ ${esc(asyncLabel(s,c,"consumer"))}</div><div class="small">subscription ${esc(c.subscription)} · ${esc(c.processing)} · ${esc(c.durability)} · ${esc(ATD[c.delivery]||c.delivery)}</div><div class="small">success ${esc(c.completion)} · failure ${esc(c.failure)}</div></div>`;
const producerAsync=p=>{const cs=AR.filter(r=>r.producer==p.id).map(r=>consumerBy(r.consumer)).filter(Boolean);return `<div class="card async-topology" data-async-producer="${esc(p.id)}"><h3 style="margin-top:0">⇢ ${esc(asyncLabel(s,p,"producer"))}</h3><div class="flow"><div class="box start"><b>${esc(cap(L(s,...opScope(s,p.operation))))}</b><div class="async-anchor">↯ publishes ${esc(p.message||"undeclared contract")}</div><div class="small">confirmation ${esc(p.confirmation)}</div></div><div class="arrow">→</div><div class="box" style="border-style:dashed;border-color:#a855f7" data-async-channel="${esc(p.channel)}"><b>${esc(p.transport)} ${esc(p.channel)}</b>${p.message?`<div><code>${esc(p.message)}</code></div>`:""}</div>${cs.length?`<div class="arrow">→</div><div class="async-fanout">${cs.map(c=>consumerBox(s,c)).join("")}</div>`:`<div class="small mut">No matching consumer is declared.</div>`}</div><div class="small mut">Publication evidence: ${esc(p.evidence)}</div></div>`};
const matchedConsumers=new Set(AR.map(r=>r.consumer));
const consumerOnly=AC.filter(c=>!matchedConsumers.has(c.id)).map(c=>`<div class="card async-topology consumer-only" data-async-consumer-only="${esc(c.id)}"><h3 style="margin-top:0">⇠ ${esc(asyncLabel(s,c,"consumer"))}</h3><div class="flow"><div class="box" style="border-style:dashed;border-color:#a855f7"><b>Input ${esc(c.transport)} ${esc(c.channel)}</b><div><code>${esc(c.message)}</code></div></div><div class="arrow">→</div>${consumerBox(s,c)}</div><div class="small mut">A producer is not inferred. Consumer evidence: ${esc(c.evidence)}</div></div>`).join("");
const asyncs=AP.map(producerAsync).join("")+consumerOnly;
const asyncTechnical=(AP.length||AC.length)?`<details class="card"><summary><b>Technical details: endpoints and correlations</b></summary><table><tr><th>Type</th><th>ID / operation</th><th>Exact key</th></tr>${AP.map(p=>`<tr><td>producer</td><td>${esc(p.id)} / ${esc(p.operation)}</td><td>${esc(p.transport)} · ${esc(p.channel)} · ${esc(p.message||"∅")}</td></tr>`).join("")}${AC.map(c=>`<tr><td>consumer</td><td>${esc(c.id)} / ${esc(c.operation)}</td><td>${esc(c.transport)} · ${esc(c.channel)} · ${esc(c.message)}</td></tr>`).join("")}</table><p class="small">${AR.length} correlation(s), inferred only from exact, case-sensitive matches on transport + channel + message/contract.</p>${AR.map(r=>`<div class="small" data-async-correlation="${esc(r.id)}">${esc(r.producer)} → ${esc(r.consumer)} · ${esc(r.transport)} / ${esc(r.channel)} / ${esc(r.message)}</div>`).join("")}</details>`:"";
return `<div class="help"><b>How to read this:</b> this evidence-backed map shows how the system is assembled. It does not prove behavior; each item indicates whether it was verified in source code, observed at runtime, documented, or inferred.</div>
<div class="card"><h2>${esc(s.name)}</h2>${vbadge(s.verdict)}${s.composition?` <span class="badge info">level ${s.level}</span> <span class="small mut">${(s.summary_total||{}).PROVEN||0} proven obligations across ${s.composition.children.length} component(s) · <a href="#" onclick="tab='levels';draw();return false">View levels</a></span><br>`:" "}<span class="small mut">${inc.length} included components, ${bnd.length} boundaries, ${m.contracts.length} contracts, ${m.flows.length} workflows, ${m.jobs.length} jobs</span></div>
${asyncs?`<h2>Asynchronous publications and subscriptions (${AP.length} / ${AC.length})</h2><div class="help">Producers and consumers are independent. Arrows are inferred only from exact matches on transport, channel, and contract; fan-out is shown without coupling declarations.</div>${asyncs}${asyncTechnical}`:""}<h2>End-to-end workflows (${m.flows.length})</h2><div class="help">Each workflow indicates whether it was verified in source code or inferred.</div>${flows}<h2>Components</h2>${inc.map(card).join("")}<h3>Boundaries (out of scope; contract only)</h3>${bnd.map(card).join("")}
${ruleSec}<h2>Contracts</h2>${contracts}<h2>Value catalogs</h2>${cats}<h2>Jobs and ETL</h2><div class="card" style="overflow:auto">${jobs}</div>
<h2>Observations</h2><div class="card">${obs}</div><h2>Exclusions</h2><div class="card">${ex}</div><h2>Open questions</h2><div class="card">${qs}</div>`}
const FK={operation:"Operation",query:"Query",compose:"Composition",invariant:"Invariant",law:"Law",ensures:"Postcondition",repeat:"Repeatability",fact:"Fact",question:"Open question",decision:"Decision",assumption:"Assumption",exclusion:"Exclusion"};
const FS={VERIFIED:["ok","Verified"],REFUTED:["bad","Refuted"],INCONCLUSIVE:["warn","Inconclusive"],NO_OBLIGATIONS:["info","No obligations"],DEFINITION:["info","Pure function"],FACT:["info","Structural fact"],OPEN:["warn","Open"],DECIDED:["info","Decided"],ASSUMPTION:["warn","Assumption"],EXCLUDED:["bad","Excluded"]};
const OS={PROVEN:"ok",REFUTED:"bad",UNKNOWN:"warn"};
function resolveEl(s,id){let d=s,trail=[];while(id.includes(".")){const i=id.indexOf("."),a=id.slice(0,i),ch=((d.composition||{}).children||[]).find(c=>c.alias==a);if(!ch||!ch.data)return [null,id,trail];trail.push(ch);d=ch.data;id=id.slice(i+1)}return [d,id,trail]}
function compCard(s,ch){const d=ch.data;if(!d)return `<div class="card"><b>Component ${esc(ch.alias)}</b> = ${esc(ch.ref)}@${esc(ch.version)} <span class="badge warn">not built</span></div>`;const t=d.summary_total||d.summary||{};
return `<div class="card"><b>Component ${esc(ch.alias)}</b> = <code>${esc(ch.ref)}@${esc(ch.version)}</code> <span class="badge info">level ${d.level}${d.level==0?" · atomic":""}</span> ${vbadge(ch.verdict||d.verdict)} <span class="small mut">${t.PROVEN||0} proven obligations</span><p class="small">A standalone UPA component, independently verified and reusable. ${(ch.exports||[]).length?`Functions exposed to other UPA specs: ${ch.exports.map(x=>`<code>${esc(ch.alias)}.${esc(x.name)}(${x.params.map(p=>esc(p[0])).join(", ")})</code>`).join(" ")}`:""}</p><button class="link" onclick="tab='levels';draw()">Open in Levels and components →</button></div>`}
const VERDICT_LABEL={VERIFIED:"Verified",REFUTED:"Refuted",INCOMPLETE:"Incomplete"};const vbadge=v=>`<span class="badge ${v=="VERIFIED"?"ok":v=="REFUTED"?"bad":"warn"}">${esc(VERDICT_LABEL[v]||v||"Unverified")}</span>`;
function felem(s0,id0,nest){const [s,id,trail]=resolveEl(s0,id0);if(!s)return `<div class="card"><b>${esc(id0)}</b> <span class="badge warn">component not built</span></div>`;
const ch=((s.composition||{}).children||[]).find(c=>c.alias==id);if(ch&&!(s.formal||{})[id])return compCard(s,ch);
const e=(s.formal||{})[id];if(!e)return `<div class="card"><b>${esc(id0)}</b> <span class="badge warn">not found in the model</span></div>`;const st=FS[e.status]||["info",e.status];
const where=trail.length?`<div class="small"><span class="chip">${trail.map(c=>`${esc(c.alias)} · ${esc(c.ref)}@${esc(c.version)}`).join(" › ")}</span> element of a lower-level component</div>`:"";
const ob=e.obligations.length?`<details><summary class="small">Proof obligations (${e.obligations.filter(o=>o.status=="PROVEN").length}/${e.obligations.length} verified)</summary><table><tr><th>Obligation</th><th>Result</th></tr>${e.obligations.map(o=>`<tr><td class="small">${esc(o.description||o.id)}<div class="mut">${esc(o.id)}</div></td><td><span class="badge ${OS[o.status]||"info"}">${esc(o.status)}</span></td></tr>`).join("")}</table></details>`:"";
const org=e.origin?`<div class="small mut">Origin: ${esc(e.origin.kind)}${e.origin.ref?` “${esc(e.origin.ref)}”`:""}${(e.assumptions||[]).length?` · assumes ${e.assumptions.map(esc).join(", ")}`:""}</div>`:"";
const pre=trail.length?id0.slice(0,id0.length-id.length):"";
const used=(e.used_in||[]).length&&(nest||0)<3?`<details><summary class="small">Used in ${e.used_in.length} element(s): ${e.used_in.map(esc).join(", ")}</summary>${e.used_in.map(u=>felem(s0,pre+u,(nest||0)+1)).join("")}</details>`:"";
return `<div class="card" id="f-${esc(id0)}"><b>${esc(FK[e.kind]||e.kind)} ${esc(id0)}</b> <span class="badge ${st[0]}">${esc(st[1])}</span>${e.line?` <span class="small mut">${esc(s.file||"upa")} line ${e.line}</span>`:""}${where}${e.text?`<p style="margin:6px 0">${esc(e.text)}</p>`:""}${org}${e.source?`<pre class="code">${esc(e.source)}</pre>`:""}${ob}${used}</div>`}
const GK=[["Queries (pure, reusable functions)",["query"]],["Operations",["operation","compose"]],["Verified rules",["invariant","law","ensures","repeat"]],["Structural facts",["fact"]],["Questions, decisions, and assumptions",["question","decision","assumption","exclusion"]]];
function lnode(d,label,ch,open){const t=d.summary_total||d.summary||{},f=d.formal||{},ids=Object.keys(f);
const groups=GK.map(([g,ks])=>{const xs=ids.filter(i=>ks.includes(f[i].kind));return xs.length?`<details ${g.startsWith("Verified")||g.startsWith("Queries")?"open":""}><summary>${esc(g)} (${xs.length})</summary>${xs.map(i=>felem(d,i)).join("")}</details>`:""}).join("");
const localKids=((d.composition||{}).children||[]).map(c=>c.data?lnode(c.data,c.alias,c,false):`<div class="card">${esc(c.alias)} = ${esc(c.ref)} <span class="badge warn">not built</span></div>`).join("");
const externalKids=(d.external_systems||[]).map(x=>lnode(SYSTEMS[x.index],x.key,null,false)).join("");
const kids=localKids+externalKids;
const ub=(d.used_by||[]).length?`<div class="small">Used by: ${d.used_by.map(u=>`<code>${esc(u.spec)}</code> as <code>${esc(u.alias)}</code> (${esc(u.path)})`).join(", ")}</div>`:`<div class="small mut">No other UPA uses this component yet.</div>`;
const ex=ch&&(ch.exports||[]).length?`<div class="small">Exposes: ${ch.exports.map(x=>`<code>${esc(ch.alias)}.${esc(x.name)}(${x.params.map(p=>esc(p[0])+": "+esc(p[1])).join(", ")}) → ${esc(x.ret)}</code>`).join(" ")}</div>`:"";
return `<details class="card" ${open?"open":""}><summary>${label?`<b>${esc(label)}</b> = `:""}<b>${esc(d.name)}</b> <span class="small mut">${esc(d.ref||"")}@${esc(d.version||"?")}</span> <span class="badge info">level ${d.level||0}${(d.level||0)==0?" · atomic":""}</span> ${vbadge(ch&&ch.verdict||d.verdict)} <span class="small mut">${t.PROVEN||0} proofs passed${t.REFUTED?`, ${t.REFUTED} refuted`:""}</span></summary>${ub}${ex}${kids?`<h3>Components (lower level)</h3>${kids}`:""}${groups?`<h3>Elements defined in ${esc(d.name)}</h3>${groups}`:""}</details>`}
function levels(s){const banner=detailBanner(s);s=detailData(s);return `${banner}<div class="help"><b>How to read this:</b> a UPA is built from smaller UPA components. An <b>atomic</b> UPA (level 0) formalizes an individual rule using its primitives; a level-N UPA composes lower-level components, up to the macroprocess. Each component is verified independently, and its verdict contributes to the parent. Open each level to explore the formal text and proofs down to the smallest rule.</div>${lnode(s,null,null,true)}`}
function formal(s){const banner=detailBanner(s);s=detailData(s);const m=s.system_map||{components:[]},cs=m.components.filter(c=>(c.realizes||[]).length);
const opts=`<select onchange="fcomp=this.value||null;draw()"><option value="">All components</option>${m.components.map(c=>`<option value="${esc(c.id)}" ${c.id==fcomp?"selected":""}>${esc(c.name)} (${(c.realizes||[]).length})</option>`).join("")}</select>`;
const help=`<div class="help"><b>How to read this:</b> this is the low-level UPA model behind the map: the exact formal text for each operation, query, and rule, plus the verifier result for each obligation. Open questions mark what has not yet been formalized. <a href="#" onclick="returnToDrill();return false">← Return to this map location</a></div>`;
const all=Object.keys(s.formal||{}),linked=new Set(cs.flatMap(c=>c.realizes));
if(fcomp){const c=m.components.find(x=>x.id==fcomp);if(c)return `${banner}${help}<div class="card">${opts}<h2 style="margin-top:12px">${esc(c.name)}</h2><p class="mut">${esc(c.role)}</p></div>${(c.realizes||[]).length?c.realizes.map(id=>felem(s,id)).join(""):`<div class="card">This component has no linked formal elements yet.</div>`}`}
const rest=all.filter(id=>!linked.has(id));
return `${banner}${help}<div class="card">${opts}</div>${cs.map(c=>`<h2>${esc(c.name)}</h2>${c.realizes.map(id=>felem(s,id)).join("")}`).join("")}${rest.length?`<h2>${cs.length?"Unassigned model elements":"Model elements"}</h2>${rest.map(id=>felem(s,id)).join("")}`:""}`}
const TIERC={macroprocess:"#7c3aed",process:"#1d4ed8",subprocess:"#0891b2",atom:"#16a34a"},TIERN={macroprocess:"macroprocess",process:"process",subprocess:"subprocess",atom:"atomic"};
function splitTop(x,sep){const out=[];let d=0,q=false,b="";for(const c of x){if(c=='"')q=!q;if(!q){if(c=="(")d++;if(c==")")d--;if(c==sep&&d==0){out.push(b.trim());b="";continue}}b+=c}if(b.trim())out.push(b.trim());return out}
const nocom=s=>(s||"").replace(/\/\/[^\n]*/g,"");
function header(src){const m=/^\s*(?:compose(?:\s+atomic)?|operation)\s+(\w+)\s*\(([^)]*)\)/.exec(nocom(src));return m?{name:m[1],params:splitTop(m[2],",").map(p=>p.split(":")[0].trim())}:{name:"",params:[]}}
function composeSteps(src){src=nocom(src);const i=src.indexOf("=",src.indexOf(")"));const body=src.slice(i+1).trim(),parts=[];let d=0,q=false,b="",sep=null;
 for(let k=0;k<body.length;k++){const c=body[k];if(c=='"')q=!q;if(!q){if(c=="(")d++;if(c==")")d--;if(d==0&&(c==";"||(c=="&"&&body[k-1]!="&"&&body[k+1]!="&"))){if(b.trim())parts.push({t:b.trim(),sep});sep=c;b="";continue}}b+=c}
 if(b.trim())parts.push({t:b.trim(),sep});let stage=0,prevG=false;
 return parts.map((p,n)=>{const g0=/^(?:(?:async|parallel)\s+)*when\s/.test(p.t);if(n&&p.sep==";"&&!(g0&&prevG))stage++;prevG=g0;let st=p.t,as=false,fan=false,guard=null,loop=null,m;
  while((m=/^(async|parallel)\s+/.exec(st))){if(m[1]=="async")as=true;else fan=true;st=st.slice(m[0].length)}
  if((m=/^loop\s+while\s+([\s\S]*?)\s+do\s+/.exec(st))){loop=m[1];st=st.slice(m[0].length)}
  else if((m=/^when\s+([\s\S]*?)\s+then\s+/.exec(st))){guard=m[1];st=st.slice(m[0].length)}
  const r=/^(?:(\w+)\.)?(\w+)\s*\(([\s\S]*)\)$/.exec(st);
  return Object.assign(r?{alias:r[1]||null,op:r[2],args:splitTop(r[3],",")}:{alias:null,op:st,args:[]},{stage,async:as,parallel:fan,guard,loop,alt:!!guard})})}
const EXI={database:"🛢",api:"🌐",service:"⚙",queue:"📨",file:"📁",directory:"👥",mail:"✉"},EXN={database:"database",api:"external API",service:"internal service",queue:"queue / asynchronous message",file:"file / folder",directory:"directory service",mail:"email"};
const EXC=new Map();
function extOf(d,op,depth){const key=(d.ref||d.name)+"#"+op;if(EXC.has(key))return EXC.get(key);EXC.set(key,[]);
 let out=(d.invocations||[]).filter(i=>i.target==op).map(i=>Object.assign({at:(d.ref||d.name)+" · "+op},i));const e=(d.formal||{})[op];
 if(e&&e.kind=="compose"&&depth<64){const kids=(d.composition||{}).children||[];for(const st of composeSteps(e.source||"")){const ch=st.alias?kids.find(c=>c.alias==st.alias):null,cd=ch?ch.data:(st.alias?null:d);if(cd)out=out.concat(extOf(cd,st.op,depth+1))}}
 const seen=new Set();out=out.filter(i=>{const k=i.kind+"|"+i.name+"|"+i.mode;if(seen.has(k))return false;seen.add(k);return true});EXC.set(key,out);return out}
function exChip(i){return `<span class="ext ${esc(i.kind)} ${i.mode=="async"?"am":""}" title="${esc(EXN[i.kind]||i.kind)} · ${i.mode=="async"?"asynchronous (does not wait)":"synchronous (waits for a response)"}${(i.via||[]).length?" · result in: "+esc(i.via.join(", ")):""}${i.evidence?" · "+esc(i.evidence):""}${i.at?" · from "+esc(i.at):""}">${EXI[i.kind]||"•"} ${esc(i.name)}${i.mode=="async"?" ⇢":""}</span>`}
function exList(xs){return xs.map(i=>`<div class="small" style="margin:2px 0">${exChip(i)} <b>${esc(EXN[i.kind]||i.kind)}</b> · ${i.mode=="async"?"<span class='badge warn'>asynchronous</span>":"synchronous"}${(i.via||[]).length?` · result in <code>${esc(i.via.join(", "))}</code>`:""}${i.evidence?` · <span class="mut">${esc(i.evidence)}</span>`:""}${i.at?` · <span class="mut">${esc(i.at)}</span>`:""}</div>`).join("")}
function exLegend(){return `<div class="legend2"><b>External dependencies:</b> ${Object.keys(EXI).map(k=>`<span class="ext ${k}">${EXI[k]} ${EXN[k]}</span>`).join("")} <span class="ext am">⇢ asynchronous call</span></div>`}
let FID=0;
function openStep(id){const el=document.getElementById(id);if(!el)return;let p=el;while(p){if(p.tagName=="DETAILS")p.open=true;p=p.parentElement}el.scrollIntoView({behavior:"smooth",block:"center"});el.style.boxShadow="0 0 0 4px #fde68a";setTimeout(()=>el.style.boxShadow="",1500)}
function conjs(g){const out=[];let d=0,q=false,b="";const s=String(g);for(let k=0;k<s.length;k++){const c=s[k];if(c=='"')q=!q;if(!q){if(c=="(")d++;if(c==")")d--;if(d==0&&s.slice(k,k+5)==" and "){out.push(b.trim());b="";k+=4;continue}}b+=c}if(b.trim())out.push(b.trim());return out}
function gshort(x){x=String(x);return x.length>38?x.slice(0,36)+"…":x}
function guardBadge(g){const cs=conjs(g);const full=cs.join(" and\n");if(cs.length<2&&String(g).length<=48)return `<span class="badge info" title="${esc(g)}">If ${esc(g)}</span>`;return `<span class="badge info" style="white-space:normal" title="${esc(full)}">If ${esc(gshort(cs[0]))}${cs.length>1?` <b>+${cs.length-1} more conditions</b>`:""}</span>`}
function guardList(g){const cs=conjs(g);return cs.length<2&&String(g).length<=48?"":`<div class="small" style="margin:4px 0"><b>Runs only if all conditions are met:</b><ul style="margin:2px 0 2px 18px">${cs.map(c=>`<li><code>${esc(c)}</code></li>`).join("")}</ul></div>`}
function hmap(d,name,pid,depth){const e=d.formal[name],kids=(d.composition||{}).children||[];const steps=composeSteps(e.source||"");const stages=[];
 steps.forEach((st,i)=>{(stages[st.stage]=stages[st.stage]||[]).push([st,i])});
 const node=([st,i])=>{const ch=st.alias?kids.find(c=>c.alias==st.alias):null,cd=ch?ch.data:(st.alias?null:d),ok=cd&&(cd.formal||{})[st.op];
  const color=ok?(TIERC[cd.tier]||"#94a3b8"):"#f59e0b",xs=ok?extOf(cd,st.op,depth):[];const shown=xs.slice(0,6);
  return `<div class="hnode ${st.async?"async":""} ${st.parallel?"fan":""}" style="border-color:${color}" onclick="openStep('${pid}-${i}')" title="Open this step’s details"><div class="op">${esc(st.op)}</div><div class="rf">${esc(ok?(cd.ref||cd.name):(st.alias||"")+" (not found)")}${ok&&cd.tier?" · "+esc(TIERN[cd.tier]||cd.tier):""}</div>${st.guard?`<div class="small">${guardBadge(st.guard)}</div>`:""}${st.loop?`<div class="small"><span class="badge warn">↻ while ${esc(st.loop)}</span></div>`:""}${st.async?`<div class="small"><span class="badge warn">asynchronous · not awaited</span></div>`:""}${st.parallel?`<div class="small"><span class="badge warn">×N instances in parallel</span></div>`:""}<div>${shown.map(exChip).join("")}${xs.length>shown.length?`<span class="ext">+${xs.length-shown.length}</span>`:""}</div></div>`};
 let h=`<div class="hmap"><div class="hterm">Start</div>`;
 stages.forEach(g=>{const as=g.every(([st])=>st.async),some=g.some(([st])=>st.async);const alt=g.every(([st])=>st.alt);h+=`<div class="hlink ${some?"async":""}">${as?"asynchronous":some?"partially asynchronous":alt?"branch":"then"}<div class="ln"></div></div><div class="hstage ${alt?"alt":g.length>1?"par":""}">${g.map(node).join("")}${alt?`<div class="hnode skip" title="If no condition is met, no step in this stage runs and the workflow continues"><div class="op">No matching condition</div><div class="rf">No matching condition; this stage is skipped</div></div>`:""}</div>`});
 return h+`<div class="hlink"><div class="ln"></div></div><div class="hterm">End</div></div>`}
function rulesFor(d,op){const f=d.formal||{};return Object.keys(f).filter(k=>["ensures","law"].includes(f[k].kind)&&f[k].source&&new RegExp("\\bfor\\s+"+op+"\\b").test(f[k].source))}
function tierTag(d){const tr=d.tier||(d.level?"":"atom");return tr?`<span class="badge" style="background:${TIERC[tr]}1a;color:${TIERC[tr]}">${TIERN[tr]||tr}</span>`:`<span class="badge info">level ${d.level||0}</span>`}
function ruleLine(d,r,pre){const e=d.formal[r],st=FS[e.status]||["info",e.status];return `<div class="rule"><div class="t">${esc(e.text||r)}<div class="small mut"><code>${esc(pre+r)}</code>${e.origin&&e.origin.ref?` · “${esc(e.origin.ref)}”`:""}</div></div><span class="badge ${st[0]}">${esc(st[1])}</span></div>`}
let flowAll=false;
function stepBox(d,ch,st,depth,path,id){
 const cd=ch?ch.data:d,e=cd&&(cd.formal||{})[st.op];
 if(!cd||!e)return `<div class="box" style="text-align:left"><b>${esc((st.alias?st.alias+".":"")+st.op)}</b> <span class="badge warn">not found</span></div>`;
 const color=TIERC[cd.tier]||"#94a3b8",hp=header(e.source).params;
 const binds=st.args.map((a,i)=>{const p=hp[i]||"?";const call=/^\w+\.\w+\s*\(/.test(a);return p==a?"":`<div class="small"><code>${esc(p)}</code> ← ${call?`<span class="badge warn">query</span> `:""}<code>${esc(a)}</code></div>`}).filter(Boolean).join("");
 const rs=rulesFor(cd,st.op);const np=path+(st.alias?st.alias+".":"");const xs=extOf(cd,st.op,depth),own=(cd.invocations||[]).filter(i=>i.target==st.op);
 let inner="";
 if(e.kind=="compose")inner=flowOf(cd,st.op,depth+1,np);
 else inner=`<pre class="code">${esc(e.source||"")}</pre>`+usedComps(cd,e.source||"",depth,np);
 const where=ch?`<b>${esc(st.alias)}</b> = <code>${esc(cd.ref||ch.ref)}</code> ${tierTag(cd)} ${vbadge(ch.verdict||cd.verdict)}`:`<span class="small mut">local step</span>`;
 return `<details id="${id||""}" class="box" style="text-align:left;border-color:${color};border-left-width:6px;${st.async?"border-style:dashed;":""}" ${flowAll?"open":""}><summary><b>${esc(st.op)}</b>${st.async?` <span class="badge warn">asynchronous</span>`:""}${st.parallel?` <span class="badge warn">×N in parallel</span>`:""}${st.guard?` ${guardBadge(st.guard)}`:""}${st.loop?` <span class="badge warn">↻ while ${esc(st.loop)}</span>`:""} <span class="small mut">${e.kind=="compose"?"composition · open to view its steps":"operation · open to view its checks, dependencies, and rules"}</span><div style="font-weight:400">${where}</div>${xs.length?`<div>${xs.map(exChip).join("")}</div>`:""}</summary>${st.guard?guardList(st.guard):""}${own.length?`<h4 style="margin:10px 0 4px">Calls external dependencies (${own.length})</h4>${exList(own)}`:(xs.length?`<h4 style="margin:10px 0 4px">External dependencies used downstream (${xs.length})</h4>${exList(xs)}`:"")}${binds?`<div style="margin:6px 0">${binds}</div>`:""}${inner}${rs.length?`<h4 style="margin:10px 0 4px">Proven rules for this step (${rs.length})</h4>${rs.map(r=>ruleLine(cd,r,np)).join("")}`:""}</details>`}
function usedComps(d,src,depth,path){if(depth>64)return "";const kids=(d.composition||{}).children||[];
 const used=kids.filter(c=>c.data&&new RegExp("\\b"+c.alias+"\\.\\w+\\s*\\(").test(src));if(!used.length)return "";
 return `<h4 style="margin:10px 0 4px">Components used by this step (${used.length})</h4>`+used.map(c=>{const cd=c.data,f=cd.formal||{},np=path+c.alias+".";
  const qs=[...new Set([...src.matchAll(new RegExp("\\b"+c.alias+"\\.(\\w+)\\s*\\(","g"))].map(m=>m[1]))].filter(q=>f[q]);
  const rs=Object.keys(f).filter(k=>["ensures","law","invariant"].includes(f[k].kind));
  const qsrc=qs.map(q=>`<pre class="code">${esc(f[q].source||q)}</pre>`+(f[q].kind=="query"?usedComps(cd,f[q].source||"",depth+1,np):"")).join("");
  return `<details class="box" style="text-align:left;border-color:${TIERC[cd.tier]||"#94a3b8"};border-left-width:6px" ${flowAll?"open":""}><summary><b>${esc(c.alias)}</b> = <code>${esc(cd.ref||c.ref)}</code> ${tierTag(cd)} ${vbadge(c.verdict||cd.verdict)} <span class="small mut">${qs.length?"uses "+qs.map(esc).join(", ")+" · ":""}${rs.length} rules</span></summary>${qsrc}${rs.length?`<h4 style="margin:10px 0 4px">Component rules (${rs.length})</h4>${rs.map(r=>ruleLine(cd,r,np)).join("")}`:""}</details>`}).join("")}
function flowOf(d,name,depth,path){const e=d.formal[name];if(depth>64)return "";const kids=(d.composition||{}).children||[];
 const steps=composeSteps(e.source||""),pid="fs"+(FID++);
 return hmap(d,name,pid,depth)+`<div class="small mut" style="margin:4px 0">Step details (select a step on the map to open it):</div><div class="flow" style="align-items:stretch">${steps.map((st,i)=>(i?`<div class="arrow" style="text-align:center">${st.stage==steps[i-1].stage&&st.alt?"◇ <span class='small'>or (conditional alternative)</span>":st.stage==steps[i-1].stage?"‖ <span class='small'>in parallel with the previous step</span>":st.async?"⇣ <span class='small'>asynchronous</span>":"↓"}</div>`:"")+stepBox(d,st.alias?kids.find(c=>c.alias==st.alias):null,st,depth,path,pid+"-"+i)).join("")}</div>`}
function componentFlow(s){const banner=detailBanner(s);s=detailData(s);FID=0;EXC.clear();const f=s.formal||{},cs=Object.keys(f).filter(k=>f[k].kind=="compose"&&f[k].source);
 const legend=Object.entries(TIERN).map(([k,v])=>`<span class="badge" style="background:${TIERC[k]}1a;color:${TIERC[k]}">${v}</span>`).join(" ");
 return banner+`<div class="help"><b>How to read this:</b> each workflow is a composition of ${esc(s.name)}. Read from left to right: each column is a sequential stage; steps inside an orange box run <b>in parallel</b>; a dashed purple arrow and outline indicate an <b>asynchronous</b> step (the workflow does not wait for it; another process completes it); a stacked shadow indicates N parallel instances. Each step lists the <b>external dependencies</b> it touches (database, external API, service, queue, file, or directory), gathered from its atomic operations; a dashed outline marks an asynchronous call. Select a step to open its details. Border color indicates level: ${legend}. Open a step to drill down through subprocesses to the atomic operation and its formal text. <code>parameter ← query</code> means a step receives a value calculated by another UPA (for example, a routing service that assigns a destination or an approval path that determines whether processing can continue). <button class="link" onclick="flowAll=!flowAll;draw()">${flowAll?"Collapse all":"Expand all"}</button></div>`+
 exLegend()+cs.map(c=>{const rs=rulesFor(s,c),h=header(f[c].source),xs=extOf(s,c,0);const byk={};xs.forEach(i=>(byk[i.kind]=byk[i.kind]||[]).push(i));return `<div class="card"><h2>${esc(c)}</h2><details class="small mut"><summary>inputs (${h.params.length} parameters)</summary>${h.params.map(esc).join(", ")}</details>${xs.length?`<details style="margin:6px 0"><summary class="small"><b>External dependencies for this workflow (${xs.length})</b> ${Object.keys(byk).map(k=>`${EXI[k]||""} ${byk[k].length} ${esc(EXN[k]||k)}`).join(" · ")}</summary>${exList(xs)}</details>`:""}${flowOf(s,c,0,"")}${rs.length?`<h3>Rules spanning workflow steps (${rs.length})</h3>${rs.map(r=>ruleLine(s,r,"")).join("")}`:""}</div>`}).join("")+childFlows(s)}
const CF={};let CFN=0;
function usesOf(d,alias,op){const f=d.formal||{};return Object.keys(f).filter(k=>f[k].kind=="compose"&&f[k].source&&new RegExp("\\b"+alias+"\\."+op+"\\s*\\(").test(nocom(f[k].source)))}
function cfOpen(el,id){const b=el.querySelector(":scope>.cfb");if(!el.open||b.dataset.done)return;const x=CF[id];const rs=rulesFor(x.d,x.c);
 b.innerHTML=flowOf(x.d,x.c,1,x.path)+(rs.length?`<h3>Verified rules for this workflow (${rs.length})</h3>${rs.map(r=>ruleLine(x.d,r,x.path)).join("")}`:"");b.dataset.done=1}
function childFlows(s){const seen=new Map(),rows=[];
 const walk=(d,path,depth)=>{for(const ch of ((d.composition||{}).children||[])){const cd=ch.data;if(!cd)continue;const ref=cd.ref||ch.ref,p=path+ch.alias+".";
  if(seen.has(ref)){seen.get(ref).also.push(p.slice(0,-1));continue}
  const f=cd.formal||{},cs=Object.keys(f).filter(k=>f[k].kind=="compose"&&f[k].source);const row={ch,cd,ref,p,depth,cs,parent:d,also:[]};seen.set(ref,row);if(cs.length)rows.push(row);walk(cd,p,depth+1)}};
 walk(s,"",0);if(!rows.length)return "";
 return `<div class="card"><h2>Component workflows (${rows.reduce((a,r)=>a+r.cs.length,0)} across ${rows.length} components)</h2><div class="help">In addition to the workflows in ${esc(s.name)}, each component it uses has its own workflows (for example, an external service response or an end-of-day process). They are listed by level; open one to view its step map, alternatives, and rules. <i>Used by</i> shows which workflow at the parent level invokes it; if nothing invokes it, another process triggers the workflow (batch, network, or operator).</div>`+
 rows.map(r=>`<div style="margin:10px 0 4px 0;padding-left:${r.depth*18}px"><b>${esc(r.p.slice(0,-1))}</b> = <code>${esc(r.ref)}</code> ${tierTag(r.cd)} ${vbadge(r.ch.verdict||r.cd.verdict)}${r.also.length?` <span class="small mut">also in ${esc(r.also.join(", "))}</span>`:""}</div>`+
  r.cs.map(c=>{const id="cf"+(CFN++);CF[id]={d:r.cd,c,path:r.p};const by=usesOf(r.parent,r.ch.alias,c),h=header(r.cd.formal[c].source),st=composeSteps(r.cd.formal[c].source),alts=st.filter(x=>x.alt).length;
   return `<details class="box" style="text-align:left;margin:4px 0 4px ${r.depth*18+10}px;border-left:6px solid ${TIERC[r.cd.tier]||"#94a3b8"}" ontoggle="cfOpen(this,'${id}')"><summary><b>${esc(c)}</b> <span class="small mut">${st.length} steps${alts?` · ${alts} alternatives`:""} · ${h.params.length} parameters</span> ${by.length?`<span class="badge info">used by ${esc(by.join(", "))}</span>`:`<span class="badge warn">triggered by another process</span>`}</summary><div class="cfb"></div></details>`}).join("")).join("")+`</div>`}
// ---------- Execute the exact IR semantics, as checked by the verifier, on a concrete input
const SIM_STATE={};let simSel=null,simOut=null;
function simSt(s){const k=s.name;if(!SIM_STATE[k])SIM_STATE[k]={};return SIM_STATE[k]}
const clone=o=>JSON.parse(JSON.stringify(o));
function defVal(sim,t){if(t=="bool")return false;if(t=="text")return "";if(t&&t.startsWith("enum:"))return (sim.enums[t.slice(5)]||[""])[0];return 0}
function attrType(sim,e,a){const x=((sim.entities[e]||{}).attrs||[]).find(z=>z.name==a);return x?x.type:"int"}
function substr(s,st,ln){s=String(s);if(st<0||ln<=0||st>=s.length)return "";return s.substr(st,Math.min(ln,s.length-st))}
const OPN={"and":"and","or":"or","=>":"⇒","==":"=","!=":"≠","<":"<","<=":"≤",">":">",">=":"≥","+":"+","-":"−","*":"×"};
function rx(e,d){d=d||0;if(!e||typeof e!="object")return String(e);
 if(e.via&&e.via.length){const v=e.via[0];return `${v.q}(${v.args.map(a=>rx(a,d+1)).join(", ")})`}
 const k=e.k;
 switch(k){case"int":return String(e.v);case"bool":return e.v?"true":"false";case"str":return JSON.stringify(e.v);case"var":return e.n;
 case"enum":return `${e.e}.${e.v}`;case"attr":return `${e.e}.${e.a}[${rx(e.key,d+1)}]`;case"in":return `${rx(e.key,d+1)} in ${e.e}`;
 case"sum":return `sum(${e.e}.${e.a})`;case"concat":return `${rx(e.l,d+1)} ++ ${rx(e.r,d+1)}`;case"to_text":return `text(${rx(e.x,d+1)})`;case"norm":return `${e.op}(${rx(e.x,d+1)})`;
 case"text_length":return `length(${rx(e.x,d+1)})`;case"contains":return `contains(${rx(e.l,d+1)}, ${rx(e.r,d+1)})`;
 case"starts_with":return `starts_with(${rx(e.l,d+1)}, ${rx(e.r,d+1)})`;case"ends_with":return `ends_with(${rx(e.l,d+1)}, ${rx(e.r,d+1)})`;
 case"mod":return `mod(${rx(e.x,d+1)}, ${e.d.v})`;case"substring":return `substring(${rx(e.text,d+1)}, ${rx(e.start,d+1)}, ${rx(e.length,d+1)})`;
 case"replace_first":return `replace_first(${rx(e.text,d+1)}, ${rx(e.search,d+1)}, ${rx(e.replacement,d+1)})`;
 case"old":return `before(${rx(e.x,d+1)})`;case"un":return `not ${rx(e.x,d+1)}`;
 case"if":return `(if ${rx(e.c,d+1)} then ${rx(e.t,d+1)} else ${rx(e.f,d+1)})`;
 case"forall":case"exists":return `${k=="forall"?"for all":"exists"} ${e.v}${e.e?" in "+e.e:""}: ${rx(e.body,d+1)}`;
 case"bin":{const s=`${rx(e.l,d+1)} ${OPN[e.op]||e.op} ${rx(e.r,d+1)}`;return d?`(${s})`:s}}
 return k}
const MAXT=400;
function ev(sim,e,env,st,old,T){
 if(e.via&&e.via.length){const v=e.via[0],rest=Object.assign({},e,{via:e.via.slice(1)});
  const node={t:"q",q:v.q,args:v.params.map((p,i)=>[p,ev(sim,v.args[i],env,st,old,null)]),ch:[]};
  if(T&&T.length<MAXT)T.push(node);const val=ev(sim,rest,env,st,old,T?node.ch:null);node.val=val;return val}
 const R=x=>ev(sim,x,env,st,old,T);
 switch(e.k){
 case"int":case"bool":case"str":return e.v;
 case"var":return env[e.n];
 case"enum":return e.v;
 case"attr":{const key=R(e.key),r=(st[e.e]||{})[key];return r&&e.a in r?r[e.a]:defVal(sim,attrType(sim,e.e,e.a))}
 case"in":return !!(st[e.e]||{})[R(e.key)];
 case"sum":return Object.values(st[e.e]||{}).reduce((a,r)=>a+(+r[e.a]||0),0);
 case"concat":return String(R(e.l))+String(R(e.r));
 case"norm":{const s=String(R(e.x));return e.op==="trim"?s.replace(/^[ \t\n\r]+|[ \t\n\r]+$/g,""):e.op==="upper"?s.toUpperCase():e.op==="lower"?s.toLowerCase():s.replace(/[^0-9]/g,"")}case"to_text":return String(R(e.x));
 case"text_length":return String(R(e.x)).length;
 case"contains":return String(R(e.l)).includes(String(R(e.r)));
 case"starts_with":return String(R(e.l)).startsWith(String(R(e.r)));
 case"ends_with":return String(R(e.l)).endsWith(String(R(e.r)));
 case"mod":{const d=e.d.v,x=R(e.x);return ((x%d)+d)%d}
 case"substring":return substr(R(e.text),R(e.start),R(e.length));
 case"replace_first":{const t=String(R(e.text)),s=String(R(e.search));return s===""?t:t.replace(s,String(R(e.replacement)))}
 case"old":return ev(sim,e.x,env,old||st,null,T);
 case"un":return !R(e.x);
 case"if":{const c=R(e.c);if(T&&T.length<MAXT)T.push({t:"if",c:rx(e.c),val:c});return c?R(e.t):R(e.f)}
 case"forall":case"exists":{if(!e.e)return true;const ks=Object.keys(st[e.e]||{});const f=k2=>ev(sim,e.body,Object.assign({},env,{[e.v]:k2}),st,old,null);return e.k=="forall"?ks.every(f):ks.some(f)}
 case"bin":{const op=e.op;
  if(op=="and"){const l=R(e.l);return l?!!R(e.r):false}
  if(op=="or"){const l=R(e.l);return l?true:!!R(e.r)}
  if(op=="=>"){const l=R(e.l);return l?!!R(e.r):true}
  const l=R(e.l),r=R(e.r);
  switch(op){case"+":return l+r;case"-":return l-r;case"*":return l*r;case"==":return l===r;case"!=":return l!==r;case"<":return l<r;case"<=":return l<=r;case">":return l>r;case">=":return l>=r}}}
 throw new Error("unsupported expression node: "+e.k)}
function runOp(sim,name,args,st){
 const op=sim.operations.find(o=>o.name==name),env={};op.params.forEach((p,i)=>env[p.name]=args[i]);
 const tr={op:name,args:op.params.map((p,i)=>[p.name,args[i]]),cases:[],effects:[],q:[]};
 let chosen=null;
 for(const [i,c] of op.cases.entries()){const T=[];const v=c.cond==null?true:!!ev(sim,c.cond,env,st,null,T);
  tr.cases.push({i,cond:c.cond==null?"otherwise":rx(c.cond),val:v,outcome:c.outcome,T,empty:!c.stmts.length});if(v){chosen=c;break}}
 const nst=clone(st);
 if(chosen){for(const s of chosen.stmts){const T=[];
   if(s.each){const ks=Object.keys(st[s.entity]||{}).filter(k=>{const e2={...env,[s.each.var]:isNaN(+k)?k:+k};return !!ev(sim,s.each.cond,e2,st,null,[])});
    for(const k of ks){const kk=isNaN(+k)?k:+k;const e2={...env,[s.each.var]:kk};
     if(s.kind=="delete"){delete nst[s.entity][k];tr.effects.push({kind:"delete",entity:s.entity,key:kk,vals:[],T})}
     else{const v=ev(sim,s.value,e2,st,null,T);nst[s.entity][k]=nst[s.entity][k]||{};nst[s.entity][k][s.attr]=v;tr.effects.push({kind:"set",entity:s.entity,key:kk,vals:[[s.attr,v,rx(s.value),T]]})}}
    continue}
   const key=ev(sim,s.key,env,st,null,T);
   if(s.kind=="delete"){if(nst[s.entity])delete nst[s.entity][key];tr.effects.push({kind:"delete",entity:s.entity,key,vals:[],T})}
   else if(s.kind=="set"){const v=ev(sim,s.value,env,st,null,T);((nst[s.entity]=nst[s.entity]||{})[key]=nst[s.entity][key]||{})[s.attr]=v;
    tr.effects.push({kind:"set",entity:s.entity,key,vals:[[s.attr,v,rx(s.value),T]]})}
   else{const vals=s.inits.map(a=>{const TT=[];const v=ev(sim,a.value,env,st,null,TT);return [a.attr,v,rx(a.value),TT]});
    const rec={};vals.forEach(([a,v])=>rec[a]=v);(nst[s.entity]=nst[s.entity]||{})[key]=rec;tr.effects.push({kind:"create",entity:s.entity,key,vals,T})}}}
 tr.rejected=!!(chosen&&chosen.outcome!=null&&!chosen.stmts.length&&!chosen.success);tr.outcome=chosen?chosen.outcome:null;tr.noCase=!chosen;
 return [nst,tr]}
function runCompose(sim,name,args,st0){
 const c=sim.composes.find(x=>x.name==name);
 if(!c){const [st,tr]=runOp(sim,name,args,st0);return [st,[{call:{op:name},ran:true,tr}]]}
 const env={};c.params.forEach((p,i)=>env[p.name]=args[i]);let st=st0,rej=false;const steps=[];
 for(const call of c.calls){const G=[];let g=true;if(call.guard!=null)g=!!ev(sim,call.guard,env,st,null,G);
  const LG=[];let lp=true;if(call.loop!=null)lp=!!ev(sim,call.loop,env,st,null,LG);
  const step={call,guard:call.guard!=null?rx(call.guard):null,gval:g,G,loop:call.loop!=null?rx(call.loop):null,loopval:lp,LG,ran:g&&lp};
  if(step.ran){const A=[];const cargs=call.args.map(a=>ev(sim,a,env,st,null,A));step.A=A;step.argx=call.args.map(rx);
   const [nst,tr]=runOp(sim,call.op,cargs,st);step.tr=tr;if(tr.rejected)rej=true;st=nst}
  steps.push(step)}
 if(c.atomic&&rej)return [st0,steps,true];
 return [st,steps,false]}
// ---------- Rendering
const fmt=v=>typeof v=="string"?(v===""?'""':JSON.stringify(v)):typeof v=="boolean"?(v?"yes":"no"):String(v);
function aliasPath(sim,op){let kids=sim.tree,parts=op.split("_"),path=[],i=0;
 while(i<parts.length-1){const n=kids.find(k=>k.alias==parts[i]);if(!n)break;path.push(n);kids=n.children;i++}
 return [path,parts.slice(i).join("_")]}
function tierChip(n){const tr=n.tier||"atom";return `<span class="badge" style="background:${TIERC[tr]}1a;color:${TIERC[tr]}" title="${esc(n.ref)}@${esc(n.version)}">${esc(n.alias)} · ${TIERN[tr]||tr}</span>`}
function qtree(T,lvl){lvl=lvl||0;if(!T||!T.length)return "";return `<ul class="qt">${T.map(n=>n.t=="q"?`<li><b>${esc(n.q)}</b>(${n.args.map(([p,v])=>`<span class="mut">${esc(p)}=</span>${esc(fmt(v))}`).join(", ")}) → <b class="val">${esc(fmt(n.val))}</b>${lvl<6?qtree(n.ch,lvl+1):""}</li>`:`<li class="small"><span class="badge ${n.val?"ok":"info"}">${n.val?"yes":"no"}</span> ${esc(n.c)}?</li>`).join("")}</ul>`}
function stepHtml(s,sim,st,n){const [path,leaf]=aliasPath(sim,st.call.op);const color=TIERC[(path[path.length-1]||{}).tier||"atom"]||"#94a3b8";
 const crumbs=`${(st.call.trail||[]).filter(t=>!t.endsWith("."+leaf)).map(t=>`<span class="small mut">${esc(t)} ›</span>`).join(" ")} ${path.map(tierChip).join(" › ")} <b>${esc(leaf)}</b>`;
 const guard=st.guard?`<div class="small" style="margin:4px 0"><span class="badge ${st.gval?"ok":"warn"}">${st.gval?"condition met":"condition not met: step skipped"}</span> <code>when ${esc(st.guard)}</code>${qtree(st.G)}</div>`:"";
 const loop=st.loop?`<div class="small" style="margin:4px 0"><span class="badge ${st.loopval?"ok":"warn"}">${st.loopval?"↻ one representative iteration ran":"loop ended: no further iteration"}</span> <code>while ${esc(st.loop)}</code>${qtree(st.LG)}</div>`:"";
 if(!st.ran)return `<div class="box" style="text-align:left;opacity:.55;border-style:dashed;border-color:${color}"><div><b>${n}.</b> ${crumbs}</div>${guard}${loop}</div>`;
 const tr=st.tr;
 const args=`<details><summary class="small">Inputs received (${tr.args.length})</summary><table>${tr.args.map(([p,v],i)=>`<tr><td><code>${esc(p)}</code></td><td><b>${esc(fmt(v))}</b></td><td class="small mut">${st.argx&&st.argx[i]!==p?esc(st.argx[i]):""}</td></tr>`).join("")}</table>${qtree(st.A)}</details>`;
 const cases=tr.cases.map(c=>`<div class="small"><span class="badge ${c.val?"ok":"info"}">${c.val?"✔ applies":"does not apply"}</span> case ${c.i+1}: <code>${esc(c.cond)}</code>${c.val&&c.empty?` <span class="badge warn">no effect${c.outcome?" · "+esc(c.outcome):""}</span>`:""}${qtree(c.T)}</div>`).join("");
 const eff=tr.effects.map(f=>`<div style="margin-top:6px"><b>${f.kind=="create"?"Creates":f.kind=="delete"?"Deletes":"Updates"} ${esc(f.entity)}[${esc(fmt(f.key))}]</b><table>${f.vals.map(([a,v,x,T])=>`<tr><td><code>${esc(a)}</code></td><td><b class="val">${esc(fmt(v))}</b></td><td class="small">${x!==String(v)&&x!==JSON.stringify(v)?`<span class="mut">= ${esc(x)}</span>${qtree(T)}`:""}</td></tr>`).join("")}</table></div>`).join("")||`<div class="small mut">State unchanged.</div>`;
 const rs=sim.rules.filter(r=>r.targets.includes(st.call.op));
 return `<div class="box" style="text-align:left;border-color:${color};border-left-width:6px"><div><b>${n}.</b> ${crumbs}</div>${guard}${loop}${args}<div style="margin-top:6px"><b class="small">Selected case</b>${cases}</div>${eff}${rs.length?`<details><summary class="small">Verified rules for this step (${rs.length})</summary>${rs.map(r=>`<div class="small">• ${esc(r.text||r.id)} <span class="mut">${esc(r.id)}</span></div>`).join("")}</details>`:""}</div>`}
function stateHtml(sim,st){const es=Object.keys(st).filter(e=>Object.keys(st[e]).length);if(!es.length)return `<p class="small mut">Sin registros.</p>`;
 return es.map(e=>{const attrs=((sim.entities[e]||{}).attrs||[]).map(a=>a.name);return `<h4 style="margin:8px 0 2px">${esc(e)}</h4><table><tr><th>id</th>${attrs.map(a=>`<th>${esc(a)}</th>`).join("")}</tr>${Object.entries(st[e]).map(([k,r])=>`<tr><td>${esc(k)}</td>${attrs.map(a=>`<td>${a in r?esc(fmt(r[a])):'<span class="mut">—</span>'}</td>`).join("")}</tr>`).join("")}</table>`}).join("")}
function ruleEval(sim,r,env,pre,post){try{const e=r.expr;let ap=null;if(e.k=="bin"&&e.op=="=>")ap=!!ev(sim,e.l,env,post,pre,null);const ok=!!ev(sim,e,env,post,pre,null);return {ap,ok}}catch(x){return {ap:null,ok:null,err:x.message}}}
function simInputs(sim,name){const c=sim.composes.find(x=>x.name==name)||sim.operations.find(x=>x.name==name);return c.params}
function inputHtml(sim,p,v){const id=`in-${p.name}`;
 if(p.type=="bool")return `<select id="${id}"><option value="1" ${v?"selected":""}>yes</option><option value="0" ${v?"":"selected"}>no</option></select>`;
 if(p.type.startsWith("enum:"))return `<select id="${id}">${(sim.enums[p.type.slice(5)]||[]).map(o=>`<option ${o==v?"selected":""}>${esc(o)}</option>`).join("")}</select>`;
 if(p.type=="text"||p.type=="id")return `<input id="${id}" value="${esc(v)}" style="width:100%">`;
 return `<input id="${id}" type="number" value="${esc(v)}" style="width:120px">`}
const SIM_IN={};
function simRead(sim,name){return simInputs(sim,name).map(p=>{const el=document.getElementById(`in-${p.name}`);const raw=el?el.value:"";
 const v=p.type=="bool"?raw=="1":(p.type=="text"||p.type=="id"||p.type.startsWith("enum:"))?raw:(+raw||0);(SIM_IN[name]=SIM_IN[name]||{})[p.name]=v;return v})}
function simRun(){const s=S(),sim=s.sim,name=simSel,args=simRead(sim,name),st0=simSt(s);
 let res;try{res=runCompose(sim,name,args,st0)}catch(x){simOut={err:x.message};draw();return}
 const [st,steps,reverted]=res,env={};simInputs(sim,name).forEach((p,i)=>env[p.name]=args[i]);
 const rules=sim.rules.filter(r=>r.targets.includes(name)).map(r=>Object.assign({r},ruleEval(sim,r,env,st0,st)));
 simWhy=null;SIM_STATE[s.name]=st;simOut={name,args,steps,reverted,rules,pre:st0,post:st};draw()}
function simReset(){SIM_STATE[S().name]={};simOut=null;draw()}
function exprVars(e,out){out=out||new Set();if(!e||typeof e!="object")return out;if(e.k=="var")out.add(e.n);for(const [k,v] of Object.entries(e)){if(k=="pos"||k=="via")continue;if(Array.isArray(v))v.forEach(x=>exprVars(x,out));else if(v&&typeof v=="object")exprVars(v,out)}return out}
function simGroups(s,sim,name,ps){const c=sim.composes.find(x=>x.name==name);if(!c)return [{title:"Operation inputs",params:ps}];
 const left=new Set(ps.map(p=>p.name)),groups=[];
 for(const call of c.calls){const used=new Set();(call.args||[]).forEach(a=>exprVars(a,used));exprVars(call.guard,used);exprVars(call.loop,used);const gp=ps.filter(p=>left.has(p.name)&&used.has(p.name));if(!gp.length)continue;
  gp.forEach(p=>left.delete(p.name));const [scope,leaf]=opScope(s,call.op);groups.push({title:cap(L(s,scope,leaf)),params:gp})}
 const rest=ps.filter(p=>left.has(p.name));if(rest.length)groups.push({title:"Other process inputs",params:rest});return groups}
// ---------- Suggested test cases and explanations
const SIM_CASES={};let simWhy=null;
function litsOf(sim){const L={int:new Set(),str:new Set()};const w=e=>{if(!e||typeof e!="object")return;if(e.k=="int"&&Number.isFinite(e.v))L.int.add(e.v);if(e.k=="str"&&e.v!=="")L.str.add(e.v);for(const [k,v] of Object.entries(e)){if(k=="pos")continue;if(Array.isArray(v))v.forEach(w);else if(v&&typeof v=="object")w(v)}};w(sim.operations);w(sim.composes);return {int:[...L.int].filter(n=>Math.abs(n)<1e6),str:[...L.str]}}
function candVals(sim,p,lits){const t=p.type;if(t=="bool")return [false,true];if(t.startsWith("enum:"))return sim.enums[t.slice(5)]||[""];
 if(t=="id")return ["T1","T2"];if(t=="text")return ["","x",...lits.str.slice(0,4)];
 const b=new Set([0,1,-1,2]);lits.int.slice(0,6).forEach(n=>{b.add(n);b.add(n-1);b.add(n+1)});return [...b].slice(0,10)}
function combos(lists,cap){let total=1;lists.forEach(l=>total*=l.length);if(total<=cap){let acc=[[]];for(const l of lists)acc=acc.flatMap(a=>l.map(v=>[...a,v]));return acc}
 const out=[];for(const b of [lists.map(l=>l[l.length-1]),lists.map(l=>l[0])]){out.push(b);lists.forEach((l,i)=>l.forEach(v=>{if(v!==b[i]){const c=b.slice();c[i]=v;out.push(c)}}))}
 let x=12345;for(let i=out.length;i<cap;i++)out.push(lists.map(l=>{x=(x*1103515245+12345)%2147483648;return l[x%l.length]}));return out}
function genCases(s,name){const sim=s.sim,lits=litsOf(sim),ps=simInputs(sim,name);
 const names=[...sim.composes.map(c=>c.name),...sim.operations.map(o=>o.name)].filter(n=>n!=name);
 const run=(nm,args,st)=>{try{return runCompose(sim,nm,args,clone(st))}catch(e){return null}};
 const okRun=r=>r&&!r[2]&&!r[1].some(x=>x.ran&&x.tr&&x.tr.rejected)&&r[1].some(x=>x.ran);
 const starts=[{prep:[],state:{},label:""}];
 for(let d=0;d<3;d++){const cur=starts[starts.length-1];let found=null;
  for(const nm of names){if(cur.prep.some(p=>p.name==nm))continue;const pp=simInputs(sim,nm);
   for(const a of combos(pp.map(p=>candVals(sim,p,lits)),60)){const r=run(nm,a,cur.state);if(okRun(r)&&JSON.stringify(r[0])!=JSON.stringify(cur.state)){found={name:nm,args:a,post:r[0]};break}}
   if(found)break}
  if(!found)break;const prep=[...cur.prep,{name:found.name,args:found.args}];
  starts.push({prep,state:found.post,label:"after "+prep.map(p=>aliasPath(sim,p.name)[1]).join(" + ")})}
 const all=combos(ps.map(p=>candVals(sim,p,lits)),900),seen=new Set(),cases=[];
 const keyOf=steps=>{const k=[];steps.forEach((x,i)=>{k.push(`${i}:${x.ran?"run":"skip"}`);if(x.ran&&x.tr)k.push(`${x.call.op}#${x.tr.cases.findIndex(c=>c.val)}:${x.tr.outcome}`)});return k};
 const bnd=new Set(lits.int.flatMap(n=>[n-1,n,n+1]));
 const lab=nw=>{const o=nw.find(k=>k.includes("#"));if(o){const [a,b]=o.split("#"),oc=b.split(":")[1];return `Outcome “${oc=="null"?"no matching case":oc}” en ${aliasPath(sim,a)[1]}`}const k=nw[0].split(":");return `Paso ${+k[0]+1} ${k[1]=="skip"?"skipped because its condition was false":"executed"}`};
 let happy=null;
 for(const st of starts)for(const a of all){const r=run(name,a,st.state);if(!okRun(r))continue;const ran=r[1].filter(x=>x.ran).length;
  if(!happy||ran>happy.ran||(ran==happy.ran&&st.prep.length<happy.st.prep.length))happy={args:a,ran,post:r[0],steps:r[1],st}}
 if(happy){keyOf(happy.steps).forEach(k=>seen.add(k));cases.push({kind:"success",label:"Smallest successful case: the process completes without rejection",args:happy.args,start:happy.st})}
 for(const st of starts)for(const a of all){const r=run(name,a,st.state);if(!r)continue;const nw=keyOf(r[1]).filter(k=>!seen.has(k));if(!nw.length)continue;
  nw.forEach(k=>seen.add(k));const lim=lits.int.length&&a.some(v=>typeof v=="number"&&bnd.has(v));
  cases.push({kind:st.prep.length?"sequence":lim?"boundary":"branch",label:lab(nw),args:a,start:st})}
 let conc=null;if(happy){const r1=run(name,happy.args,happy.st.state),r2=run(name,happy.args,happy.st.state);if(r1&&r2){const cr=x=>x[1].flatMap(z=>z.tr?z.tr.effects.filter(e=>e.kind=="create").map(e=>e.entity+"["+e.key+"]"):[]);const b=new Set(cr(r2));conc={col:cr(r1).filter(k=>b.has(k))}}}
 return {cases:cases.slice(0,40),happy,conc,tried:all.length*starts.length,covered:seen.size}}
function simGen(){const s=S();SIM_CASES[s.name+"|"+simSel]=genCases(s,simSel);draw()}
function simLoad(i){const s=S(),g=SIM_CASES[s.name+"|"+simSel],c=g.cases[i];SIM_IN[simSel]={};simInputs(s.sim,simSel).forEach((p,j)=>SIM_IN[simSel][p.name]=c.args[j]);SIM_STATE[s.name]=clone(c.start.state);simOut=null;draw();simRun()}
function casesHtml(s,sim){const g=SIM_CASES[s.name+"|"+simSel],ps=simInputs(sim,simSel);
 const head=`<div class="card"><h3 style="margin-top:0">Generated test cases</h3><p class="small mut">In addition to manual input, the viewer explores value combinations (type values, model constants, and nearby values) using the simulator semantics, then keeps cases that cover a new branch, outcome, or boundary. This is a search of the model, not proof that no other cases exist.</p><button class="link" onclick="simGen()">${g?"Regenerate":"Generate cases"}</button>`;
 if(!g)return head+`</div>`;
 const colors={success:"ok",branch:"info",boundary:"warn",sequence:"info"};
 return head+` <span class="small mut">${g.tried} combinations tested · ${g.covered} distinct branches/results covered · ${g.cases.length} cases</span>
 <table style="margin-top:8px"><tr><th>Type</th><th>Coverage</th><th>Input</th><th>Starting state</th><th></th></tr>${g.cases.map((c,i)=>`<tr><td><span class="badge ${colors[c.kind]||"info"}">${c.kind}</span></td><td>${esc(c.label)}</td><td class="small"><code>${esc(ps.map((p,j)=>p.name+"="+fmt(c.args[j])).join(", "))}</code></td><td class="small">${esc(c.start.label||"empty state")}</td><td><button class="link" onclick="simLoad(${i})">Load and run</button></td></tr>`).join("")}</table>
 ${g.conc?`<div style="margin-top:10px"><b>Concurrent scenario:</b> two simultaneous runs of the success case against the same initial state. ${g.conc.col.length?`<span class="badge warn">both create ${esc(g.conc.col.join(", "))}</span> <span class="small">without a declared protection (<code>concurrency</code>), one may overwrite the other or create a duplicate.</span>`:`<span class="badge ok">the two runs do not collide on keys</span> <span class="small mut">(not all interleavings are modeled).</span>`}</div>`:""}
 <p class="small mut">Not yet covered: pairs of potentially conflicting rules (currently, only create-key collisions are detected).</p></div>`}
function whyHtml(s,sim,o){const items=o.steps.map((x,i)=>({x,i})).filter(z=>z.x.ran&&z.x.tr);if(!items.length)return "";
 if(simWhy==null||!items.some(z=>z.i==simWhy)){const rj=items.filter(z=>z.x.tr.rejected);simWhy=(rj.length?rj[rj.length-1]:items[items.length-1]).i}
 const z=items.find(q=>q.i==simWhy),x=z.x,tr=x.tr,[path,leaf]=aliasPath(sim,tr.op),[scope]=opScope(s,tr.op);
 const oc=tr.outcome==null?"no matching case":tr.outcome,chosen=tr.cases.findIndex(c=>c.val);
 const names=new Set(),walk=T=>(T||[]).forEach(n=>{if(n.t=="q"){names.add(n.q);walk(n.ch)}});
 tr.cases.forEach(c=>walk(c.T));tr.effects.forEach(e=>walk(e.T));
 const sys=invOf(s,tr.op,x.call);
 const rules=sim.rules.filter(r=>r.targets.includes(tr.op)),ev0=o.rules;
 return `<div class="card"><h3 style="margin-top:0">Why did this happen?</h3>
 <label>Outcome to explain <select onchange="simWhy=+this.value;draw()">${items.map(q=>`<option value="${q.i}" ${q.i==simWhy?"selected":""}>${q.i+1}. ${esc(aliasPath(sim,q.x.tr.op)[1])} → ${esc(q.x.tr.outcome==null?"no matching case":q.x.tr.outcome)}</option>`).join("")}</select></label>
 <p><b>Why did “${esc(leaf)}” end with “${esc(oc)}”${tr.rejected?" (rejected)":""}?</b></p>
 <ol class="rel">
 <li><b>Operation that produced this state:</b> ${path.map(tierChip).join(" › ")} <b>${esc(leaf)}</b>${x.guard?`<div class="small"><span class="badge ok">ran because</span> <code>${esc(x.guard)}</code>${qtree(x.G)}</div>`:""}</li>
 <li><b>Condition evaluated:</b> ${chosen<0?"none: no case matched.":""}${tr.cases.slice(0,chosen<0?tr.cases.length:chosen+1).map(c=>`<div class="small" style="margin:3px 0"><span class="badge ${c.val?"ok":"info"}">${c.val?"met":"not met"}</span> <code>${esc(c.cond)}</code>${c.val?` → <b>${esc(c.outcome==null?"—":c.outcome)}</b>`:""}${qtree(c.T)}</div>`).join("")}</li>
 <li><b>Inputs used:</b> ${tr.args.map(([n,v])=>`<span class="badge info" title="${esc(n)}">${esc(L(s,scope,n))} = ${esc(fmt(v))}</span>`).join(" ")||"—"}</li>
 <li><b>Applicable rules:</b> ${rules.map(r=>{const e=ev0.find(q=>q.r.id==r.id);return `<div class="small">${e?(e.ok===false?'<span class="badge bad">✖</span>':e.ap===false?'<span class="badge info">does not apply</span>':'<span class="badge ok">✔</span>'):""} ${esc(r.text||r.id)}</div>`}).join("")||'<span class="mut">none declared for this operation</span>'}</li>
 <li><b>Source of each input:</b> ${sys.length?sys.map(v=>`<span class="badge warn">external system: ${esc(v.name)}</span>`).join(" "):'<span class="mut">no external system declared for this step</span>'} ${[...names].map(n=>`<span class="badge info">query ${esc(L(s,scope,n))}</span>`).join(" ")}</li>
 <li><b>Model evidence:</b> ${rules.filter(r=>r.origin&&r.origin.ref).map(r=>`<div class="small">${esc(r.id)}: “${esc(r.origin.ref)}” <span class="mut">(${esc(r.origin.kind||"")})</span></div>`).join("")||'<span class="mut">no source reference</span>'}</li>
 <li><b>Difference from production behavior:</b> <span class="mut">no production case is loaded for this input; to compare it with the real system, run <code>python -m upa replay</code>.</span></li>
 </ol>
 ${tr.effects.length?`<div class="small"><b>What was recorded and where each value came from</b>${tr.effects.map(e=>`<div style="margin:4px 0">${esc(e.kind)} ${esc(e.entity)}[${esc(fmt(e.key))}] ${e.vals.map(([a,v,t,T])=>`<div class="small">${esc(a)} = <b>${esc(fmt(v))}</b> <span class="mut">because ${esc(t)}</span>${qtree(T)}</div>`).join("")}</div>`).join("")}</div>`:""}</div>`}
function simulate(s){const sim=s.sim;if(!sim)return `<div class="card">This UPA has no executable operations.</div>`;
 const own=sim.operations.filter(o=>!o.block).map(o=>o.name),names=[...sim.composes.map(c=>c.name),...own];
 if(!simSel||!names.includes(simSel))simSel=names[0];
 const ps=simInputs(sim,simSel),cur=SIM_IN[simSel]||{},groups=simGroups(s,sim,simSel,ps);
 const form=`<div class="card"><label><b>Process to run</b> <select onchange="simSel=this.value;simOut=null;draw()">${names.map(n=>`<option ${n==simSel?"selected":""}>${esc(n)}</option>`).join("")}</select></label>
 ${groups.map(g=>`<fieldset style="margin-top:12px"><legend><b>${esc(g.title)}</b></legend><table>${g.params.map(p=>{const ex=X(s,"",p.name);return `<tr><td><b>${esc(L(s,"",p.name))}</b>${ex?`<div class="small mut">${esc(ex)}</div>`:""}<div><code>${esc(p.name)}</code> <span class="small mut">${esc(p.decl||p.type)}</span></div></td><td>${inputHtml(sim,p,p.name in cur?cur[p.name]:(p.type=="id"?"T1":defVal(sim,p.type)))}</td></tr>`}).join("")}</table></fieldset>`).join("")}
 <button class="link" onclick="simRun()">▶ Run</button> <button class="link" onclick="simReset()">Reset state</button> <span class="small mut">State is preserved between runs, so you can chain processes using the same identifier.</span></div>`;
 const help=`<div class="help"><b>How to read this:</b> enter inputs and run the simulation. The viewer executes the UPA rules exactly (using the same semantics checked by the verifier) and traces the data path: each step shows its component and level (${Object.entries(TIERN).map(([k,v])=>`<span class="badge" style="background:${TIERC[k]}1a;color:${TIERC[k]}">${v}</span>`).join(" ")}), evaluated conditions, queries from other UPAs that calculated each value, and recorded changes. Gray steps were skipped because their conditions were not met.</div>`;
 let out="";
 if(simOut&&simOut.err)out=`<div class="card"><span class="badge bad">Error</span> ${esc(simOut.err)}</div>`;
 else if(simOut){const o=simOut,ran=o.steps.filter(x=>x.ran);
  const route=o.steps.map(x=>{const [p,leaf]=aliasPath(sim,x.call.op);return `<span class="badge ${x.ran?"ok":"info"}" style="${x.ran?"":"opacity:.6;text-decoration:line-through"}">${esc(p.map(n=>n.alias).concat(leaf).join("."))}</span>`}).join(" → ");
  const viol=o.rules.filter(r=>r.ok===false);
  out=`<div class="card"><h2>Run trace for ${esc(o.name)}</h2><div>${route}</div><p class="small">${ran.length} of ${o.steps.length} steps executed${o.reverted?' · <span class="badge warn">atomic composition rolled back after a rejection</span>':""}.</p></div>
  <div class="flow" style="align-items:stretch"><div class="box start">Input: ${o.args.map((v,i)=>`${esc(ps[i].name)}=${esc(fmt(v))}`).join(", ")}</div>${o.steps.map((x,i)=>`<div class="arrow" style="text-align:center">↓</div>${stepHtml(s,sim,x,i+1)}`).join("")}<div class="arrow" style="text-align:center">↓</div><div class="box end">End</div></div>
  <div class="card"><h3 style="margin-top:0">Rules evaluated for this case (${o.rules.length})</h3>${o.rules.map(x=>`<div class="rule"><div class="t">${esc(x.r.text||x.r.id)}<div class="small mut">${esc(x.r.id)}${x.r.origin&&x.r.origin.ref?` · “${esc(x.r.origin.ref)}”`:""}</div></div>${x.ok===false?'<span class="badge bad">✖ not met</span>':x.ap===false?'<span class="badge info">not applicable to this case</span>':'<span class="badge ok">✔ met</span>'}</div>`).join("")||'<p class="small mut">No rules defined here.</p>'}${viol.length?'<p class="small"><span class="badge bad">Warning</span> a verified rule was not met in this simulation. Check the inputs (for example, an identifier that is already in use).</p>':""}</div>
  <div class="card"><h3 style="margin-top:0">State after the run</h3>${stateHtml(sim,o.post)}</div>${whyHtml(s,sim,o)}`}
 return help+form+casesHtml(s,sim)+out}

// ---------- Plain-English narrative, using terms before humanized identifiers.
const RELATO_TAB=["narrative","Narrative (in plain words)"];
const ACRONYMS=new Set(["API","HTTP","ID","URL","UUID","XML","JSON","SQL","UPA"]);
function hum(n){return String(n).replace(/_/g," ").replace(/([a-z0-9])([A-Z])/g,"$1 $2").replace(/([A-Z]+)([A-Z][a-z])/g,"$1 $2").split(/\s+/).filter(Boolean).map((w,i)=>ACRONYMS.has(w.toUpperCase())?w.toUpperCase():i?w.toLowerCase():w[0].toUpperCase()+w.slice(1).toLowerCase()).join(" ")}
function pluralize(label){const parts=String(label).split(/(\s+)/),i=parts.map((x,j)=>/\S/.test(x)?j:-1).filter(j=>j>=0).pop();if(i==null)return label;const word=parts[i],lower=word.toLowerCase(),irregular={person:"people",child:"children",mouse:"mice",goose:"geese",man:"men",woman:"women",foot:"feet",tooth:"teeth",analysis:"analyses",crisis:"crises",basis:"bases",datum:"data",status:"statuses"};let plural=irregular[lower];if(!plural){if(/[^aeiou]y$/i.test(word))plural=word.slice(0,-1)+"ies";else if(/(?:s|x|z|ch|sh)$/i.test(word))plural=word+"es";else if(/(?:fe|f)$/i.test(word))plural=word.replace(/(?:fe|f)$/i,"ves");else plural=word+"s"}if(word===word.toUpperCase())plural=plural.toUpperCase();else if(word[0]===word[0].toUpperCase())plural=plural[0].toUpperCase()+plural.slice(1);parts[i]=plural;return parts.join("") }
function noun(s,scope,key,count=1){const term=T0(s,scope,key),label=term?.label||hum(key);return count!==1?(term?.plural||pluralize(label)):label}
function article(label){const first=String(label).trim().split(/\s+/)[0]||"",lower=first.toLowerCase();if(/^(hour|honest|honor|heir|herb)/.test(lower))return "an";if(/^(uni([^nmd]|$)|use|user|usual|utility|euro|one|once|ewe|you)/.test(lower))return "a";return /^[aeiou]/i.test(first)?"an":"a"}
function TT(s){if(s._tt)return s._tt;const by={},g={};
 const walk=(ns,pre)=>(ns||[]).forEach(n=>{const p=pre.concat(n.alias);walk(n.children,p);by[p.join(".")]={terms:n.terms||{},inv:n.invocations||[],stores:n.stores||[],node:n};Object.assign(g,n.terms||{})});
 walk((s.sim||{}).tree,[]);Object.assign(g,s.terms||{});by[""]={terms:s.terms||{},inv:s.invocations||[],stores:s.stores||[]};return s._tt={by,g}}
function T0(s,scope,key){const t=TT(s),sc=(t.by[scope]||{}).terms||{};return sc[key]||t.g[key]||null}
function TR(s,scope,key){const t=T0(s,scope,key);if(t||!String(key).includes("."))return [t,key];const i=key.lastIndexOf("."),sc=(scope?scope+".":"")+key.slice(0,i);return TT(s).by[sc]?TR(s,sc,key.slice(i+1)):[null,key.slice(i+1)]}
const L=(s,scope,key)=>{const [t,k]=TR(s,scope,key);return t?t.label:hum(k)};
const X=(s,scope,key)=>{const [t]=TR(s,scope,key);return t&&t.text?t.text:""};
function opScope(s,op){const [path,leaf]=aliasPath(s.sim,op);return [path.map(n=>n.alias).join("."),leaf]}
function asyncLabel(s,a,role){const m=String(a.id||"").match(/^(P\d+)(.*)$/);if(!m)return `${a.id} · ${role}`;const process=cap(L(s,...opScope(s,a.operation))),detail=cap(hum(m[2]||""));return `${process}${detail?` · ${detail}`:""} · ${role}`}
function trailScope(t){const i=t.lastIndexOf(".");return i<0?["",t]:[t.slice(0,i),t.slice(i+1)]}
const CMP={"==":"is","!=":"is not","<":"is less than","<=":"is at most",">":"is greater than",">=":"is at least","+":"plus","-":"minus","*":"times"};
const isTrue=e=>e&&e.k=="bool"&&e.v===true,isFalse=e=>e&&e.k=="bool"&&e.v===false;
function nl(e,c,d){d=d||0;if(!e||typeof e!="object")return String(e);const s=c.s,R=x=>nl(x,c,d+1);
 if(e.via&&e.via.length){const v=e.via[0],lab=L(s,c.scope,v.q);const body=Object.assign({},e,{via:[]});
  if(c.defs&&!c.defs.some(x=>x[0]==lab)){c.defs.push([lab,null]);const i=c.defs.length-1;
   const al=[...new Set(e.via.slice(1).map(x=>L(s,c.scope,x.q)).filter(x=>x!=lab))];
   const tx=e.via.map(x=>X(s,c.scope,x.q)).find(Boolean)||"";
   c.defs[i]=[lab,nlCases(body,c,1)+(al.length?`<div class="mut">Based on: ${al.map(a=>"“"+esc(a)+"”").join(", ")}.</div>`:""),tx]}
  return `“${lab}”`}
 switch(e.k){case"int":return String(e.v);case"bool":return e.v?"yes":"no";case"str":return e.v===""?"empty":`“${e.v}”`;
 case"var":{const lab=L(s,c.scope,e.n),tx=X(s,c.scope,e.n);if(c.defs&&tx&&!c.defs.some(x=>x[0]==lab))c.defs.push([lab,null,tx]);return lab}
 case"enum":return T0(s,c.scope,`${e.e}.${e.v}`)?L(s,c.scope,`${e.e}.${e.v}`):hum(e.v);
 case"attr":{const a=T0(s,c.scope,`${e.e}.${e.a}`)?L(s,c.scope,`${e.e}.${e.a}`):L(s,c.scope,e.a);const ent=L(s,c.scope,e.e);return `${article(ent)} ${ent}’s ${a}`}
 case"in":return e.key&&e.key.k=="var"&&e.key.n=="id"?`${article(L(s,c.scope,e.e))} ${L(s,c.scope,e.e)} already exists for this operation`:`${article(L(s,c.scope,e.e))} ${L(s,c.scope,e.e)} already exists for ${R(e.key)}`;
 case"sum":return `the total ${L(s,c.scope,e.a)} across all ${noun(s,c.scope,e.e,2)}`;
 case"concat":return `${R(e.l)} followed by ${R(e.r)}`;case"to_text":return R(e.x);case"norm":return R(e.x)+" "+({trim:"with surrounding whitespace removed",upper:"in uppercase",lower:"in lowercase",digits_only:"with non-digits removed"})[e.op];
 case"text_length":return `the number of characters in ${R(e.x)}`;
 case"contains":return `${R(e.l)} contains ${R(e.r)}`;case"starts_with":return `${R(e.l)} starts with ${R(e.r)}`;case"ends_with":return `${R(e.l)} ends with ${R(e.r)}`;
 case"mod":return `the remainder when ${R(e.x)} is divided by ${e.d.v}`;
 case"substring":{const st=e.start,ln=e.length;return st.k=="int"&&st.v===0?`the first ${R(ln)} characters of ${R(e.text)}`:`${R(ln)} characters of ${R(e.text)} starting at position ${st.k=="int"?st.v+1:R(st)+" + 1"}`}
 case"replace_first":return `${R(e.text)} with the first ${R(e.search)} replaced by ${R(e.replacement)}`;
 case"old":return `${R(e.x)} (before the change)`;case"un":{if(e.x&&e.x.k=="in"&&!(e.x.via&&e.x.via.length))return e.x.key&&e.x.key.k=="var"&&e.x.key.n=="id"?`${article(L(s,c.scope,e.x.e))} ${L(s,c.scope,e.x.e)} has not yet been recorded for this operation`:`${article(L(s,c.scope,e.x.e))} ${L(s,c.scope,e.x.e)} does not exist for ${R(e.x.key)}`;if(e.x&&e.x.k=="var"){const label=L(s,c.scope,e.x.n),valid=/^valid\s+(.+)$/i.exec(label);if(valid)return `the ${valid[1]} is not valid`}return `not ${R(e.x)}`};
 case"if":return `if ${R(e.c)}, then ${R(e.t)}; otherwise ${R(e.f)}`;
 case"forall":return `for every ${e.e?L(s,c.scope,e.e):e.v}: ${R(e.body)}`;case"exists":return `there is at least one ${e.e?L(s,c.scope,e.e):e.v} such that ${R(e.body)}`;
 case"bin":{const o=e.op;
  if(o=="and"||o=="or"){const sep=o=="and"?" and ":" or ";const parts=[],raw=[];const fl=x=>x.k=="bin"&&x.op==o&&!(x.via&&x.via.length)?(fl(x.l),fl(x.r)):(raw.push(x),parts.push(R(x)));fl(e);
   const lit=x=>x.k=="bin"&&x.op=="=="&&!(x.via&&x.via.length)&&["str","int","enum"].includes(x.r.k);
   if(o=="or"&&raw.length>2){const grp=[];raw.forEach((x,i)=>{const g=lit(x)?grp.find(z=>z.l==J(x.l)):null;if(g)g.v.push(x.r);else grp.push(lit(x)?{l:J(x.l),le:x.l,v:[x.r]}:{p:parts[i]})});
    if(grp.length<raw.length){const ps=grp.map(g=>g.p||(g.v.length>1?`${R(g.le)} is ${g.v.slice(0,-1).map(R).join(", ")} or ${R(g.v[g.v.length-1])}`:`${R(g.le)} is ${R(g.v[0])}`));const t2=ps.join(sep);return d&&ps.length>1?`(${t2})`:t2}}const t3=parts.join(sep);return d&&parts.length>1?`(${t3})`:t3}
  if(o=="=>")return `if ${R(e.l)}, then ${R(e.r)}`;
  if((o=="=="||o=="!=")&&(isTrue(e.r)||isFalse(e.r))){const neg=(o=="==")==isFalse(e.r);return neg?`not ${R(e.l)}`:R(e.l)}
  return `${R(e.l)} ${CMP[o]||o} ${R(e.r)}`}}
 return e.k}
function nlCases(e,c,top){if(e&&e.k=="if"&&!(e.via&&e.via.length)){const L2=[];let x=e;
  while(x&&x.k=="if"&&!(x.via&&x.via.length)){L2.push([nl(x.c,c,1),nl(x.t,c,1)]);x=x.f}
  return `<ul class="rel">${L2.map(([a,b],i)=>`<li>${i?"Otherwise, if the previous condition is false and ":"If "}${esc(a)} → <b>${esc(b)}</b></li>`).join("")}<li>Otherwise → <b>${esc(nl(x,c,1))}</b></li></ul>`}
 return `<b>${esc(nl(e,c,0))}</b>`}
function definitionButton(l,h,t){return `<button type="button" class="formal-help" aria-label="${esc(h?"How this is calculated: "+l:"What this means formally: "+l)}">?<span class="formal-tooltip" role="tooltip">${t?`<span>${esc(t)}</span>`:""}${h||""}</span></button>`}
function explained(text,c){let out=esc(text);const defs=c.defs.filter(x=>!x[1]&&x[2]).sort((a,b)=>b[0].length-a[0].length);defs.forEach(([l,h,t],i)=>{const token=`@@UPA_DEF_${i}@@`;out=out.split(esc(l)).join(token);out=out.split(token).join(`${esc(l)} ${definitionButton(l,h,t)}`)});return out}
const DIRK={database:"Reads from/writes to the database",api:"Calls the API",service:"Calls the service",queue:"Sends/receives through the queue",file:"Uses the file",directory:"Uses the directory",mail:"Sends email to"};
function defsHtml(c){const xs=c.defs.filter(x=>x[1]);return xs.length?`<div class="small formal-definitions"><span class="mut">Calculations:</span>${xs.map(([l,h,t])=>`<span class="formal-definition">${esc(l)} ${definitionButton(l,h,t)}</span>`).join("")}</div>`:""}
const DBVERB={database:"queries the database",api:"calls the API",service:"calls the service",queue:"uses the queue",file:"reads the file",directory:"uses the directory",mail:"sends email to"};
function storeOf(s,scope,ent){const by=TT(s).by;const own=((by[scope]||{}).stores||[]).find(x=>x.entity==ent);if(own)return own;for(const k in by){const f=(by[k].stores||[]).find(x=>x.entity==ent);if(f)return f}return null}
function invOf(s,op,call){const [scope,leaf]=opScope(s,op);const all=((TT(s).by[scope]||{}).inv||[]).filter(v=>v.target==leaf);
 if(!call)return all;const o=s.sim.operations.find(x=>x.name==op);if(!o)return all;
 // If all values from this boundary are computed by another model step rather than supplied as input, it is not a boundary here
 return all.filter(v=>!(v.via||[]).length||(v.via||[]).some(pn=>{const a=call.args[o.params.findIndex(p=>p.name==pn)];return !a||a.k=="var"}))}
function closedOf(s,op,call){const all=invOf(s,op),open=new Set(invOf(s,op,call));return call?all.filter(v=>!open.has(v)):[]}
const KICON={database:"🛢",api:"🌐",service:"⚙",queue:"✉",file:"📄",directory:"📁",mail:"✉"};
const KNAME={database:"database",api:"API",service:"service",queue:"queue",file:"file",directory:"directory",mail:"email"};
function frontierViz(v,ans,from){const db=v.kind=="database",as=v.mode=="async";
 return `<div class="fz" title="Boundary: the following behavior is not modeled">
  <div class="fz-in"><span class="small mut">inside the model</span><b>${esc(from||"this step")}</b></div>
  <div class="fz-end" aria-label="end of model"><span>MODEL BOUNDARY</span></div>
  <div class="fz-arrows"><div class="fz-go">${esc(DBVERB[v.kind]||v.kind)} <span class="fz-line"></span>▶</div>
   ${as?`<div class="fz-back fz-none">continues without waiting for a response</div>`:`<div class="fz-back">◀<span class="fz-line"></span> ${ans&&ans.length?`returns: ${ans.map(a=>"“"+esc(a)+"”").join(", ")}`:"the response returns (its contents are not modeled)"}</div>`}</div>
  <div class="fz-out"><span class="small">${KICON[v.kind]||"◇"} ${esc(KNAME[v.kind]||v.kind)} · continues outside the model</span><b>${esc(v.name)}</b>
   <span class="small mut">${db?"tables, filters, and ordering are not modeled":"internal logic is not modeled"}</span></div>
 </div>${v.evidence?`<div class="small mut fz-ev">Evidence: ${esc(v.evidence)}</div>`:""}`}
function frontierChips(s,op,call){const inv=invOf(s,op,call),cl=closedOf(s,op,call);return inv.length||cl.length?`<div class="fz-chips">${inv.map(v=>`<span class="fz-chip" title="${esc(v.evidence||"")}"><i></i>${KICON[v.kind]||"◇"} calls <b>${esc(v.name)}</b> ${v.mode=="async"?"→":"⇄"}</span>`).join("")}${cl.map(v=>`<span class="fz-chip fz-closed" title="This boundary is resolved by another model step"><i></i>✓ ${esc(v.name)}: modeled internally</span>`).join("")}</div>`:""}
function frontierHtml(s,scope,inv,from){if(!inv.length)return "";
 return `<div><b>The model boundary:</b>${inv.map(v=>frontierViz(v,(v.via||[]).map(p=>L(s,scope,p)),from)).join("")}
  <p class="small mut">Anything to the right of the red line is not modeled; it is a <b>boundary</b>. The model only uses the returned value${inv.some(v=>v.kind=="database")?" (whether the data was found and the query succeeded)":""}, and treats it as unverified input.</p></div>`}
const varsIn=(e,acc=new Set())=>{if(e&&typeof e=="object"){if(e.k=="var"&&e.n)acc.add(e.n);Object.values(e).forEach(x=>varsIn(x,acc))}return acc};
// The model resolves this step boundary: show which steps produce the value and where the actual boundary lies
function closedHtml(s,op,call,co,closed){const o=s.sim.operations.find(x=>x.name==op);
 const used=new Set();closed.forEach(v=>(v.via||[]).forEach(pn=>{const j=o.params.findIndex(p=>p.name==pn);if(j>=0)varsIn(call.args[j],used)}));
 const pre=t=>(t&&t.length?t:[]).slice(0,-2).join("/"),idx=(co?co.calls:[]).indexOf(call),mine=pre(call.trail);
 const srcs=[],seen=new Set();(co?co.calls:[]).forEach((c2,i2)=>{if(c2===call||(idx>=0&&i2>idx))return;if(pre(c2.trail)!=mine)return;const o2=s.sim.operations.find(x=>x.name==c2.op);if(!o2)return;const [sc2,lf2]=opScope(s,c2.op);
  invOf(s,c2.op,c2).forEach(v=>{const hit=(v.via||[]).filter(pn=>{const a=c2.args[o2.params.findIndex(p=>p.name==pn)];return a&&a.k=="var"&&used.has(a.n)});const k=v.kind+"|"+v.name;
   if(hit.length&&!seen.has(k)){seen.add(k);srcs.push({step:cap(L(s,sc2,lf2)),v,ans:hit.map(pn=>L(s,sc2,pn))})}})});
 return `<div class="fz-closed-box"><p class="small"><span class="badge ok">✓ boundary resolved</span> On its own, this step would call ${closed.map(v=>`<b>${esc(v.name)}</b>`).join(", ")}. It does not need to call it here: the model already knows the result from lower-level steps, which are the steps that touch external systems. No undeclared boundary remains at this point:</p>
  ${srcs.length?srcs.map(x=>frontierViz(x.v,x.ans,x.step)).join(""):`<p class="small mut">(the producing steps are not in this composition)</p>`}</div>`}
function opStory(s,op,argMap,call,co){const o=s.sim.operations.find(x=>x.name==op);if(!o)return "";
 const [scope,leaf]=opScope(s,op),c={s,scope,defs:[]};
 const ps=o.params.filter(p=>p.type!="id");const inv=invOf(s,op,call),closed=closedOf(s,op,call);
 const from=p=>inv.filter(v=>(v.via||[]).includes(p));
 const asks=ps.length?`<div><b>Needs to know:</b><ul class="rel">${ps.map(p=>{const a=argMap&&argMap[p.name],f=from(p.name),ex=X(s,scope,p.name),lab=L(s,scope,p.name);return `<li><b>${esc(lab)}</b>${ex?` ${definitionButton(lab,null,ex)}`:""}${f.length?` <span class="badge info">provided by ${esc(f.map(v=>v.name).join(" / "))}</span>`:""}${a?` <span class="mut">(${esc(a)})</span>`:""}</li>`}).join("")}</ul></div>`:"";
 const talks=frontierHtml(s,scope,inv,cap(L(s,scope,leaf)))+(closed.length?closedHtml(s,op,call,co,closed):"");
 const visible=o.cases.filter(k=>k.stmts.length||k.outcome);
 const n=visible.length;
 const isT=k=>k.cond&&k.cond.k=="bool"&&k.cond.v===true;
 const cs=visible.map((k,i)=>{const cond=k.cond&&!isT(k)?nl(k.cond,c,0):null;
  const head=cond?`${i?"Otherwise, if the previous condition is false and":"If"} ${explained(cond,c)}`:(n>1?"Otherwise":"Always");
  let body;if(!k.stmts.length)body=k.success?`<b>continues</b> without recording anything (returns ${esc(hum(k.outcome))})`:`<b>rejects</b> (${esc(hum(k.outcome))})`;
  else body=k.stmts.map(t=>{const ent=L(s,scope,t.entity),sto=storeOf(s,scope,t.entity),col=a=>sto&&sto.columns&&sto.columns[a]?` <span class="small mut">[column ${esc(sto.columns[a])}]</span>`:"";
   const where=sto?` <span class="small mut">(stored in table <code>${esc(sto.table)}</code>${sto.key?`, key ${esc(sto.key)}`:""})</span>`:"";
   const each=t.each?`for each ${esc(noun(s,scope,t.entity,2))} that match ${esc(rx(t.each.cond))}: `:"";
   if(t.each&&t.kind=="delete")return `${each}delete${where}`;
   if(t.each&&t.kind=="set")return `${each}set ${esc(T0(s,scope,t.entity+"."+t.attr)?L(s,scope,t.entity+"."+t.attr):L(s,scope,t.attr))}${col(t.attr)}${where} to ${nlCases(t.value,c)}`;
   if(t.kind=="create")return `create ${article(ent)} ${esc(ent)}${where} with:<ul class="rel">${t.inits.map(a=>`<li>${esc(T0(s,scope,t.entity+"."+a.attr)?L(s,scope,t.entity+"."+a.attr):L(s,scope,a.attr))}${col(a.attr)}: ${nlCases(a.value,c)}</li>`).join("")}</ul>`;
   if(t.kind=="set")return `set ${esc(ent)}’s ${esc(T0(s,scope,t.entity+"."+t.attr)?L(s,scope,t.entity+"."+t.attr):L(s,scope,t.attr))}${col(t.attr)}${where} to ${nlCases(t.value,c)}`;
   if(t.kind=="delete")return `delete ${article(ent)} ${esc(ent)}${where}`;return esc(t.kind)}).join("; ");
  return `<li>${head}: ${body}</li>`}).join("");
 return `${asks}${talks}${cs?`<div><b>What happens:</b><ul class="rel">${cs}</ul></div>`:""}${defsHtml(c)}`}
const conj=e=>e&&e.k=="bin"&&e.op=="and"&&!(e.via&&e.via.length)?[...conj(e.l),...conj(e.r)]:e?[e]:[];
const J=x=>JSON.stringify(x);
function composeStory(s,name){const co=s.sim.composes.find(x=>x.name==name);const c0={s,scope:"",defs:[]};
 const root={ch:[],calls:[]};
 co.calls.forEach((call,i)=>{const tr=call.trail&&call.trail.length?call.trail:[call.op];let node=root;
  tr.slice(0,-1).forEach(t=>{let k=node.ch[node.ch.length-1];if(!k||k.t!=t){k={t,ch:[]};node.ch.push(k)}node=k});node.ch.push({call,i})});
 const guardsOf=n=>n.call?[n.call.guard?conj(n.call.guard).map(J):[]]:n.ch.flatMap(guardsOf);
 const common=n=>{const G=guardsOf(n);return G.length?G[0].filter(g=>G.every(x=>x.includes(g))):[]};
 const loopsOf=n=>n.call?[n.call.loop?J(n.call.loop):null]:n.ch.flatMap(loopsOf);
 const commonLoop=n=>{const xs=loopsOf(n);return xs.length&&xs[0]&&xs.every(x=>x==xs[0])?xs[0]:null};
 let num=0;
 let prevTop=[];const cond=(cs,plus)=>plus?(cs.length==1?`<div class="relg">In addition to the previous conditions, only if ${esc(cs[0])}:</div>`:`<div class="relg">In addition to the previous conditions, only if all of these conditions are met:<ul class="relcond">${cs.map(c=>`<li>${esc(c)}</li>`).join("")}</ul></div>`):cs.length==1?`<div class="relg">Only if ${esc(cs[0])}:</div>`:`<div class="relg">Only if all of these conditions are met:<ul class="relcond">${cs.map(c=>`<li>${esc(c)}</li>`).join("")}</ul></div>`;
  const render=(n,outer,outerLoop)=>{if(n.call){const call=n.call,[scope,leaf]=opScope(s,call.op),g=(call.guard?conj(call.guard):[]).filter(x=>!outer.includes(J(x))),lk=call.loop?J(call.loop):null;
   const o=s.sim.operations.find(x=>x.name==call.op);const argMap={};
   if(o)o.params.forEach((p,j)=>{const a=call.args[j];if(a&&!(a.k=="var"&&a.n==p.name))argMap[p.name]=nl(a,Object.assign({},c0,{defs:null}),1)});
   const ex=X(s,scope,leaf);num++;
   return `<li class="relstep${lk&&lk!=outerLoop?` repeat-block`:""}">${lk&&lk!=outerLoop?`<div class="relg"><span class="badge warn">↻ Repeats</span> while ${esc(nl(call.loop,c0,1))}. The viewer shows one representative iteration:</div>`:""}${g.length?cond(g.map(x=>nl(x,c0,1))):""}<b>${esc(cap(L(s,scope,leaf)))}</b>${ex?` — <span>${esc(ex)}</span>`:""} <span class="small mut">(${esc(scope.replace(/\./g," › ")||"this process")})</span>${frontierChips(s,call.op,call)}
    <details><summary class="small">How it works</summary><div class="small">${opStory(s,call.op,argMap,call,co)}</div><button class="link small" onclick="simSel='${esc(name)}';tab='simulate';draw()">Try it in the simulator</button></details></li>`}
  if(n.ch.length==1&&!n.ch[0].call){const k=n.ch[0],[a1,b1]=trailScope(n.t),[a2,b2]=trailScope(k.t);if(L(s,a1,b1)==L(s,a2,b2))return render(k,outer,outerLoop)}
  if(n.ch.length==1&&n.ch[0].call){const [a1,b1]=trailScope(n.t),[a2,b2]=opScope(s,n.ch[0].call.op);if(L(s,a1,b1)==L(s,a2,b2))return render(n.ch[0],outer,outerLoop)}
  const [sc,nm]=trailScope(n.t),cm=common(n).filter(x=>!outer.includes(x)),inner=outer.concat(cm),ex=X(s,sc,nm),lp=commonLoop(n);
  return `<li class="relgrp${lp&&lp!=outerLoop?` repeat-block`:""}">${lp&&lp!=outerLoop?`<div class="relg"><span class="badge warn">↻ Repeats</span> while ${esc(nl(JSON.parse(lp),c0,1))}. The viewer shows one representative iteration:</div>`:""}${cm.length?(()=>{const top=!outer.length,pv=prevTop,inc=top&&pv.length&&pv.every(x=>cm.includes(x)),nw=inc?cm.filter(x=>!pv.includes(x)):cm;if(top)prevTop=cm;return nw.length?cond(nw.map(x=>nl(JSON.parse(x),c0,1)),inc):cond(["the previous conditions are met"].map(x=>x),false).replace("Only if the previous conditions are met","Same conditions as the previous step")})():""}<b>${esc(cap(L(s,sc,nm)))}</b>${ex?` — ${esc(ex)}`:""} <span class="small mut">(${esc(sc.replace(/\./g," › ")||"this process")})</span><ol>${n.ch.map(k=>render(k,inner,lp||outerLoop)).join("")}</ol></li>`};
 const endpointOf=n=>n.call?((n.call.trail&&n.call.trail[0])||n.call.op):n.t;
 const anchorsFor=n=>{const ep=endpointOf(n);return [...(s.async_producers||[]).filter(a=>a.operation==ep).map(a=>Object.assign({},a,{role:"producer"})),...(s.async_consumers||[]).filter(a=>a.operation==ep).map(a=>Object.assign({},a,{role:"consumer"}))]};
 const anchorHtml=(n,h)=>{const ep=endpointOf(n),aa=anchorsFor(n);if(!aa.length)return h;
  const chips=aa.map(a=>`<span class="async-anchor" data-async-anchor="${esc(a.id)}" data-async-role="${a.role}">↯ ${esc(asyncLabel(s,a,a.role=="producer"?"producer":"consumer"))}</span>`).join("");
  return h.replace("<li ",`<li data-flow-endpoint="${esc(ep)}" `).replace(/(<li[^>]*>)/,`$1${chips}`)};
 const deliveryLabel=a=>String(a.delivery||"unspecified").replace(/_/g,"-");
 const bandHtml=p=>{const rs=(s.async_correlations||[]).filter(r=>r.producer==p.id),cs=rs.map(r=>(s.async_consumers||[]).find(c=>c.id==r.consumer)).filter(Boolean);return `<li class="async-band" value="${num}" role="group" aria-label="${esc(asyncLabel(s,p,"producer"))}" data-async-id="${esc(p.id)}" data-async-from="${esc(p.operation)}"><div class="async-band-grid"><div class="async-edge producer"><strong>${esc(asyncLabel(s,p,"producer"))}</strong>publishes “${esc(p.message||"undeclared contract")}”</div><div class="async-arrow" aria-hidden="true">→</div><div class="async-message"><span class="small">${esc(p.transport)}</span><code>${esc(p.channel)}</code><span class="async-badge">publication ${esc(p.confirmation)}</span></div>${cs.length?`<div class="async-arrow" aria-hidden="true">→</div><div class="async-fanout">${cs.map(c=>`<div class="async-edge consumer" data-async-to="${esc(c.operation)}"><strong>${esc(asyncLabel(s,c,"consumer"))}</strong>subscription ${esc(c.subscription)}<span class="small">${esc(c.processing)} · ${esc(c.durability)} · ${esc(deliveryLabel(c))}</span><span class="small">success ${esc(c.completion)} · failure ${esc(c.failure)}</span></div>`).join("")}</div>`:`<div class="small mut">No matching consumer is declared.</div>`}</div><div class="small mut">Evidence: ${esc(p.evidence)}</div></li>`};
 const consumerOnlyHtml=c=>`<li class="async-band consumer-only" value="${num}" data-async-consumer-only="${esc(c.id)}"><div class="async-band-grid"><div class="async-message"><span class="small">${esc(c.transport)}</span><code>${esc(c.channel)}</code><span class="small">${esc(c.message)}</span></div><div class="async-arrow">→</div><div class="async-edge consumer"><strong>${esc(asyncLabel(s,c,"consumer"))}</strong>subscription ${esc(c.subscription)}<span class="small">${esc(c.processing)} · ${esc(c.durability)} · ${esc(deliveryLabel(c))}</span></div></div><div class="small mut">No producer is inferred. Evidence: ${esc(c.evidence)}</div></li>`;
 const rootHtml=()=>{let out="",matched=new Set((s.async_correlations||[]).map(r=>r.consumer));root.ch.forEach(n=>{(s.async_consumers||[]).filter(c=>c.operation==endpointOf(n)&&!matched.has(c.id)).forEach(c=>out+=consumerOnlyHtml(c));out+=anchorHtml(n,render(n,[],null));(s.async_producers||[]).filter(p=>p.operation==endpointOf(n)).forEach(p=>out+=bandHtml(p))});return out};
 const ext={},sys=new Map();
 co.calls.forEach(call=>{const o=s.sim.operations.find(x=>x.name==call.op);if(!o)return;const [sc]=opScope(s,call.op);
  invOf(s,call.op,call).forEach(v=>{const k=v.kind+"|"+v.name;if(!sys.has(k))sys.set(k,{v,sc,steps:new Set(),ans:new Set()});const e=sys.get(k);e.steps.add(cap(L(s,...opScope(s,call.op))));
   (v.via||[]).forEach(pn=>{const j=o.params.findIndex(p=>p.name==pn),a=call.args[j];e.ans.add(L(s,sc,pn));if(a&&a.k=="var")(ext[a.n]=ext[a.n]||new Set()).add(v.name)})})});
 const pp=co.params.filter(p=>p.type!="id"),req=pp.filter(p=>!ext[p.name]),rsp=pp.filter(p=>ext[p.name]);
 const ins=req.map(p=>esc(L(s,"",p.name)));
 const rspHtml=rsp.length?`<p class="small"><b>Responses used from other systems</b> (the model receives the result; it does not calculate it): ${rsp.map(p=>`${esc(L(s,"",p.name))} <span class="mut">← ${esc([...ext[p.name]].join(" / "))}</span>`).join("; ")}.</p>`:"";
 const tg=[];const wk=(ns,pre)=>(ns||[]).forEach(n=>{const pth=pre.concat(n.alias);(n.gaps||[]).forEach(g=>tg.push([pth.join(" › "),"not expressible",g]));(n.questions||[]).forEach(g=>tg.push([pth.join(" › "),"open question",g]));(n.hdec||[]).forEach(h=>tg.push([pth.join(" › "),"human decision",h.text+" — decided by: "+h.owner+". Options: "+(h.options||[]).map(o=>o.id+" ("+o.text+")").join("; ")]));wk(n.children,pth)});wk(s.sim.tree,[]);
 ((s.notes||{}).language_gaps||[]).forEach(g=>tg.push(["this process","not expressible",g]));((s.notes||{}).questions||[]).forEach(g=>tg.push(["this process","open question",g]));((s.notes||{}).risks||[]).forEach(h=>tg.push(["this process","identified risk",h.text+(h.severity?" (severity "+h.severity+")":"")]));((s.notes||{}).concurrency||[]).forEach(h=>tg.push(["this process","concurrency ("+(h.mode||"undeclared")+")",h.text+(h.by?" — by "+h.by:"")+((h.protects||[]).length?" — protects "+h.protects.join(", "):"")+(h.on_conflict?" — on conflict: "+h.on_conflict:"")]));((s.notes||{}).concurrency_gaps||[]).forEach(h=>tg.push(["this process","concurrency undeclared","“"+h.operation+"” writes state on which the following depend "+h.rules.join(", ")+"; the model assumes atomic operations"]));((s.notes||{}).persists||[]).forEach(h=>tg.push(["this process",h.cache?"cached data":"persisted data",h.target+(h.hint?" — currently in "+h.hint:"")+(h.normalized?" — normalized: "+h.normalized:"")]));((s.notes||{}).parameters||[]).forEach(h=>tg.push(["this process","configurable parameter",h.text+(h.owner?" — defined by: "+h.owner:"")]));((s.notes||{}).human_decisions||[]).forEach(h=>tg.push(["this process","human decision",h.text+" — decided by: "+h.owner+". Options: "+(h.options||[]).map(o=>o.id+" ("+o.text+")").join("; ")]));
 {const sn=new Map();tg.forEach(([w,k,g])=>{const key=k+"|"+(g.text||g.id);if(!sn.has(key))sn.set(key,[w,k,g])});tg.length=0;tg.push(...sn.values())}
 const lim=(sys.size||tg.length)?`<div class="card"><h3 style="margin:0 0 6px">How far this model goes</h3>
  <p class="small">This narrative is exact within the model. At these points it relies on behavior that is not modeled internally; the returned values have not been verified and are treated as inputs.</p>
  ${sys.size?`<h4 style="margin:6px 0">Boundaries: systems and databases queried (${sys.size})</h4>${[...sys.values()].map(e=>frontierViz(e.v,[...e.ans],[...e.steps].join(", "))).join("")}`:""}
  ${tg.length?`<details><summary class="small"><b>What remains unmodeled or undecided (${tg.length})</b></summary><ul class="rel small">${tg.map(([w,k,g])=>`<li><span class="badge warn">${esc(k)}</span> ${esc(g.text||g.id)} <span class="mut">(${esc(w)})</span></li>`).join("")}</ul></details>`:""}
  <p class="small mut">If an input to this process is not listed as a system response, the model receives it already resolved and does not identify its source. If it is not a user-provided value, this is an undeclared boundary (an <code>invokes</code> declaration is missing).</p></div>`:"";
 const ex=X(s,"",name);
 return `<div class="card"><h3 style="margin:0">${esc(cap(L(s,"",name)))}</h3>${ex?`<p>${esc(ex)}</p>`:""}
  <p class="small"><b>Starting inputs:</b> ${ins.join("; ")||(rsp.length?"no user-provided inputs; all values come from other systems":"only the operation identifier")}.</p>
  ${co.atomic?`<p class="small"><span class="badge warn">all or nothing</span> If any step rejects the request, nothing is recorded.</p>`:""}
  ${rspHtml}<ol class="rel">${rootHtml()}</ol>${defsHtml(c0)}
  <div class="small mut">Steps marked “Only if” are conditional alternatives; they run only when the condition is met. Technical name: <code>${esc(name)}</code> · <a href="#" onclick="openDrillDetail('levels');return false" >View components</a> · <a href="#" onclick="openDrillDetail('formal');return false" >View formal model</a></div></div>${lim}`}
const REL_SEL={};
function narrativeOne(s,embedded){const sim=s.sim;if(!sim)return `<div class="card">This UPA has no executable behavior to describe.</div>`;
 const names=sim.composes.map(c=>c.name);const called=new Set(sim.composes.flatMap(c=>c.calls.map(x=>x.op)));
 const loose=sim.operations.filter(o=>!o.block&&!called.has(o.name));
 const key=String(s.viewer_key||s.ref||s.name),selected=names.includes(REL_SEL[key])?REL_SEL[key]:names[0];
 const pick=names.length>1?` <div class="card"><b>Behavior:</b> ${names.map(n=>`<button type="button" class="link ${n==selected?"on":""}" data-behavior-key="${esc(key)}" data-behavior-name="${esc(n)}" title="${esc(n)}">${esc(cap(L(s,"",n)))}</button>`).join(" · ")}</div>`:"";
  const rest=loose.length?`<div class="card"><h3 style="margin:0 0 6px">Other operations triggered independently (${loose.length})</h3>${loose.map(o=>{const [sc,lf]=opScope(s,o.name);const ex=X(s,sc,lf);return `<details><summary><b>${esc(cap(L(s,sc,lf)))}</b>${ex?` — ${esc(ex)}`:""} <span class="small mut">(${esc(sc.replace(/\./g," › ")||"this process")})</span></summary><div class="small">${opStory(s,o.name)}</div></details>`}).join("")}</div>`:"";
 return pick+(names.length?composeStory(s,selected):"")+rest+(embedded?"":regenCard(s))}
function graphFrame(nodes,rels,overlay){const specs=rels.map(r=>`<i hidden data-graph-edge data-from="${esc(r.from)}" data-to="${esc(r.to)}" data-kind="${esc(r.kind)}" data-label="${esc(r.label)}"></i>`).join("");
 return `<div class="process-graph" data-process-graph><div class="cy-graph"></div><div class="graph-fallback">${nodes}</div>${specs}${overlay||""}<div class="graph-legend"><span><i></i> synchronous</span><span><i class="async"></i> asynchronous</span><span>Drag to pan · scroll to zoom</span></div></div>`}
let CY_GRAPHS=[];
function mountGraphs(){CY_GRAPHS.forEach(c=>c.destroy());CY_GRAPHS=[];if(typeof cytoscape!="function"||!document.querySelectorAll)return;
 document.querySelectorAll("[data-process-graph]").forEach(g=>{const rawNodes=[...g.querySelectorAll("[data-graph-node]")],rawEdges=[...g.querySelectorAll("[data-graph-edge]")];
  const elements=rawNodes.map(n=>({data:{id:n.dataset.graphNode,label:[n.querySelector("b")?.textContent,n.querySelector("span")?.textContent].filter(Boolean).join("\n"),subtitle:n.querySelector("small")?.textContent||"",external:n.classList.contains("external")?1:0,behavior:n.classList.contains("behavior-node")?1:0,selected:n.classList.contains("selected")?1:0,behaviorKey:n.dataset.behaviorKey,behaviorName:n.dataset.behaviorName,drillSystem:n.dataset.drillSystem,drillPath:n.dataset.drillPath}}));
  rawEdges.forEach((e,i)=>elements.push({data:{id:`e${i}`,source:e.dataset.from,target:e.dataset.to,label:e.dataset.label,kind:e.dataset.kind}}));
  const cy=cytoscape({container:g.querySelector(".cy-graph"),elements,minZoom:.35,maxZoom:2.2,wheelSensitivity:1.2,
   style:[
    {selector:"node",style:{"shape":"round-rectangle","width":154,"height":76,"background-color":"#fff","border-width":2,"border-color":"#7c3aed","label":"data(label)","font-family":"system-ui","font-size":12,"font-weight":600,"color":"#312e81","text-wrap":"wrap","text-max-width":136,"text-valign":"center","text-halign":"center","text-outline-color":"#fff","text-outline-width":2}},
    {selector:"node[external = 1]",style:{"border-style":"dashed","border-color":"#64748b","background-color":"#f8fafc","color":"#334155"}},
    {selector:"node[behavior = 1]",style:{"shape":"diamond","width":190,"height":92,"background-color":"#ccfbf1","border-color":"#0f766e","color":"#134e4a"}},
    {selector:"node[selected = 1]",style:{"background-color":"#ede9fe","border-color":"#5b21b6","border-width":4}},
    {selector:"node[behavior = 1][selected = 1]",style:{"background-color":"#99f6e4","border-color":"#115e59","border-width":5}},
    {selector:"node:active",style:{"overlay-color":"#7c3aed","overlay-opacity":.12}},
    {selector:"edge",style:{"width":2.4,"curve-style":"bezier","target-arrow-shape":"triangle","line-color":"#2563eb","target-arrow-color":"#2563eb","label":"data(label)","font-family":"system-ui","font-size":10,"font-weight":600,"color":"#334155","text-rotation":"autorotate","text-background-color":"#fff","text-background-opacity":.92,"text-background-padding":4,"text-margin-y":-8}},
    {selector:'edge[kind = "async"]',style:{"line-style":"dashed","line-color":"#7c3aed","target-arrow-color":"#7c3aed"}},
    {selector:"edge:loop",style:{"loop-direction":"-45deg","loop-sweep":"80deg","text-rotation":"none"}}
   ],
   layout:rawNodes.length>4?{name:"circle",padding:70,startAngle:-Math.PI/2,clockwise:true,avoidOverlap:true,spacingFactor:1.12}:{name:"breadthfirst",directed:true,padding:90,spacingFactor:1.35,avoidOverlap:true}
  });
  cy.on("tap","node",ev=>{const d=ev.target.data();if(d.behaviorName){REL_SEL[d.behaviorKey]=d.behaviorName;draw()}else if(d.drillSystem!==undefined&&d.drillSystem!==null){DRILL.system=Number(d.drillSystem);DRILL.path=[];draw()}else if(d.drillPath){DRILL.path=JSON.parse(decodeURIComponent(d.drillPath));draw()}});
  cy.ready(()=>cy.fit(undefined,55));CY_GRAPHS.push(cy)})}
function processMap(s,embedded){const m=s.system_map||{components:[]},component=k=>m.components.find(c=>c.id==k),epProcess=a=>{const z=/Referenciar(P\d+)/.exec(String(a&&a.operation||""));return z?z[1]:null},rels=[],seen=new Set(),add=(from,to,kind,label)=>{const k=[from,to,kind,label].join("|");if(!seen.has(k)){seen.add(k);rels.push({from,to,kind,label})}};
 const producers=new Map((s.async_producers||[]).map(x=>[x.id,x])),consumers=new Map((s.async_consumers||[]).map(x=>[x.id,x]));
 (s.async_correlations||[]).forEach(r=>{const p=producers.get(r.producer),c=consumers.get(r.consumer);add(epProcess(p)||"externo",epProcess(c)||"externo","async",`${r.channel} · ${r.message}`)});
 const matchedP=new Set((s.async_correlations||[]).map(r=>r.producer)),matchedC=new Set((s.async_correlations||[]).map(r=>r.consumer));
 (s.async_producers||[]).filter(x=>!matchedP.has(x.id)).forEach(x=>add(epProcess(x)||"externo","externo","async",`${x.channel} · ${x.message}`));
 (s.async_consumers||[]).filter(x=>!matchedC.has(x.id)).forEach(x=>add("externo",epProcess(x)||"externo","async",`${x.channel} · ${x.message}`));
 m.components.filter(c=>/^P\d+$/.test(c.id)).forEach(c=>(c.depends_on||[]).filter(x=>/^P\d+$/.test(x)).forEach(x=>add(c.id,x,"sync","synchronous call")));
 const nodeIds=new Set((s.external_systems||[]).map(x=>x.key)),needsExternal=rels.some(r=>!nodeIds.has(r.from)||!nodeIds.has(r.to));
 const nodes=(s.external_systems||[]).map(x=>{const child=SYSTEMS[x.index],c=component(x.key),title=String((c&&c.name)||child.name).replace(new RegExp("^"+x.key+"\\s*[—:-]?\\s*","i"),"");return `<button type="button" class="process-node ${DRILL.system==x.index?"selected":""}" data-graph-node="${esc(x.key)}" data-drill-system="${x.index}"><b>${esc(x.key)}</b><span>${esc(title)}</span><small>${esc((c&&c.role)||TIERN[child.tier]||child.tier||"process")} · Show lower level ↓</small></button>`}).join("")+(needsExternal?`<div class="process-node external" data-graph-node="externo"><b>External</b><span>Outside the modeled system</span><small>declared boundary</small></div>`:"");
 const body=`<h2 style="margin-top:0">System · process map</h2><p>Select a process to explore it. Lines are part of the map: dashed purple indicates asynchronous communication; solid blue indicates a synchronous call.</p>${graphFrame(nodes,rels)}`;
 return embedded?body:`<div class="card drill-shell">${body}</div>`}
function drillChild(d,alias){return ((d.composition||{}).children||[]).find(x=>x.alias==alias)}
function drillData(system,path){let d=SYSTEMS[system];for(const alias of path){const c=drillChild(d,alias);if(!c||!c.data)break;d=c.data}return d}
function composeSequence(co,children){const aliases=new Set(children.map(x=>x.alias)),seq=[];
 ((co||{}).calls||[]).forEach(call=>{const alias=((call.trail||[])[0]||"").split(".")[0];if(aliases.has(alias)&&seq[seq.length-1]!=alias)seq.push(alias)});
 return seq}
function compositionLevel(d,basePath,preview){const children=((d.composition||{}).children||[]),title=cap(L(d,"",d.name||d.ref||"level"));
 const composes=((d.sim||{}).composes||[]),key=String(d.viewer_key||d.ref||d.name),selected=composes.some(c=>c.name==REL_SEL[key])?REL_SEL[key]:((composes[0]||{}).name);
 const behaviorNodes=composes.map((co,i)=>`<button type="button" class="process-node behavior-node ${co.name==selected?"selected":""}" data-graph-node="__behavior_${i}" data-behavior-key="${esc(key)}" data-behavior-name="${esc(co.name)}"><b>Behavior</b><span>${esc(cap(L(d,"",co.name)))}</span><small>Select to show its narrative</small></button>`).join("");
 if(!children.length){const atom=`<div class="process-node" data-graph-node="atom"><b>Atomic</b><span>${esc(title)}</span><small>final level · no lower-level components</small></div>`,rels=composes.map((_,i)=>({from:`__behavior_${i}`,to:"atom",kind:"sync",label:"runs in"}));return `<h2 style="margin-top:0">${esc(title)}</h2>${graphFrame(behaviorNodes+atom,rels,preview)}`}
 const nextPath=alias=>encodeURIComponent(JSON.stringify(basePath.concat(alias)));
 const nodes=children.map(c=>{const cd=c.data||{},label=cap(L(cd,"",cd.name||c.alias)),sub=((cd.composition||{}).children||[]).length;return `<button type="button" class="process-node" data-graph-node="${esc(c.alias)}" data-drill-path="${nextPath(c.alias)}"><b>${esc(c.alias)}</b><span>${esc(label)}</span><small>${esc(TIERN[cd.tier]||cd.tier||"component")} · ${sub?`Show ${sub} lower-level node${sub==1?"":"s"} ↓`:"Show atomic model and narrative ↓"}</small></button>`}).join("");
 const rels=[];composes.forEach((co,i)=>{const seq=composeSequence(co,children);if(seq.length)rels.push({from:`__behavior_${i}`,to:seq[0],kind:"sync",label:"runs"});seq.slice(1).forEach((to,j)=>rels.push({from:seq[j],to,kind:"sync",label:"then"}))});
 return `<h2 style="margin-top:0">${esc(title)} · lower level</h2>${graphFrame(behaviorNodes+nodes,rels,preview)}${rels.length?"":`<div class="drill-empty">${children.length==1?"This level contains a single node.":"The model does not declare a direct order between these nodes, so no relationship is inferred."}</div>`}`}
function previousPreview(s){let title,labels,attrs;if(!DRILL.path.length){title="System";labels=(s.external_systems||[]).map(x=>x.key);attrs=`data-drill-system="0"`}
 else{const parentPath=DRILL.path.slice(0,-1),parent=drillData(DRILL.system,parentPath);title=parentPath.length?parentPath[parentPath.length-1]:(SYSTEMS[DRILL.system].viewer_key||SYSTEMS[DRILL.system].name);labels=((parent.composition||{}).children||[]).map(x=>x.alias);attrs=`data-drill-path="${encodeURIComponent(JSON.stringify(parentPath))}"`}
 const shown=labels.slice(0,6),extra=labels.length-shown.length;return `<button type="button" class="drill-prev" ${attrs}><b>← Previous level: ${esc(title)}</b><small>Preview; click to return</small><span class="preview-nodes">${shown.map(x=>`<span>${esc(x)}</span>`).join("")}${extra?`<span>+${extra}</span>`:""}</span></button>`}
let WHATIF_CONTEXT=null;
function whatIfPanel(s){WHATIF_CONTEXT=s;return `<div class="whatif-ai"><b>Verifiable AI what-if</b><p class="small">Describe a change in plain language. The prompt requires the AI to translate it into a temporary UPA model, run the verifier, and compare results; it must not answer by intuition.</p><textarea id="whatif-question" placeholder="Example: What if a future date were also considered invalid?"></textarea><div class="whatif-actions"><button type="button" class="link" onclick="prepareWhatIf(false)">Copy formal prompt</button><button type="button" class="link" onclick="prepareWhatIf(true)">Copy and open Copilot Chat</button></div><div id="whatif-status" class="small whatif-status"></div></div>`}
function whatIfPrompt(question){const s=WHATIF_CONTEXT||activeRuleContext(S()),r=whatIfCandidates(s)[impSel]||null,path=[SYSTEMS[DRILL.system]&&(SYSTEMS[DRILL.system].viewer_key||SYSTEMS[DRILL.system].name),...DRILL.path].filter(Boolean);
 return `VERIFIABLE UPA WHAT-IF\nUser question: ${question}\nSelected node: ${path.join(" > ")||s.name}\nSpec: ${s.name} ${s.version||""}\nCompiled artifacts: ${s.artifact_dir||"not provided"}\nKnown source file: ${s.file||"locate the corresponding .upa file"}\nSelected rule: ${r?`${impSel} — ${r.text||""}`:"none; find the rule mentioned in the model"}\n\nRequired procedure:\n1. Do not guess or fill in missing rules.\n2. Read the .upa and canonical-ir.json for this node and its referenced components.\n3. Translate my change into a candidate formal modification, showing the exact interpretation and any ambiguity.\n4. Work on a temporary copy; do not modify the canonical model without my confirmation.\n5. Run the UPA compiler and verifier on both the baseline and candidate.\n6. Compare PROVEN/REFUTED/UNKNOWN obligations, results, guards, effects, states, and affected components.\n7. Report any verifier counterexample. If the change cannot be expressed in UPA, identify it as a gap; do not simulate an answer.\n8. Report the formal interpretation, candidate UPA diff, commands run, evidence for the result, and a conclusion limited to what was demonstrated.`}
function prepareWhatIf(openChat){const q=$("#whatif-question"),status=$("#whatif-status"),text=(q&&q.value||"").trim();if(!text){status.textContent="Enter the change you want to evaluate first.";return}const prompt=whatIfPrompt(text);
 const copied=navigator.clipboard&&navigator.clipboard.writeText?navigator.clipboard.writeText(prompt):Promise.reject();copied.then(()=>status.textContent="Formal prompt copied.").catch(()=>{q.value=prompt;q.select();document.execCommand&&document.execCommand("copy");status.textContent="Prompt prepared and selected for copying."});
 if(openChat){const a=document.createElement("a");a.href="vscode://GitHub.Copilot-Chat/chat?prompt="+encodeURIComponent(prompt);a.click();status.textContent="Prompt copied and sent to Copilot Chat. The response must include UPA verification."}}
function drillExplorer(s){if(!DRILL.system)return `<div class="card drill-shell"><div class="drill-stage">${processMap(s,true)}</div></div>`;
 let d=SYSTEMS[DRILL.system],path=[],crumb=[`<button type="button" data-drill-system="0">System</button>`,`<button type="button" data-drill-system="${DRILL.system}">${esc(d.viewer_key||d.name)}</button>`];
 for(let i=0;i<DRILL.path.length;i++){const alias=DRILL.path[i],child=drillChild(d,alias);if(!child||!child.data)break;d=child.data;path=path.concat(alias);
  const encoded=encodeURIComponent(JSON.stringify(path));crumb.push(i==DRILL.path.length-1?`<span class="current">${esc(alias)}</span>`:`<button type="button" data-drill-path="${encoded}">${esc(alias)}</button>`)}
 if(!DRILL.path.length)crumb[crumb.length-1]=`<span class="current">${esc(SYSTEMS[DRILL.system].viewer_key||SYSTEMS[DRILL.system].name)}</span>`;
  return `<div class="card drill-shell"><div class="drill-path">${crumb.join("<span>›</span>")}</div><div class="drill-stage">${compositionLevel(d,path,previousPreview(s))}</div><div class="drill-story"><span class="badge info">Narrative for this level</span> <button type="button" class="link" onclick="openDrillDetail('levels')">View components</button> <button type="button" class="link" onclick="openDrillDetail('formal')">View formal model</button>${narrativeOne(d,true)}${whatIfPanel(d)}</div></div>`}
function narrative(s){const help=`<div class="help"><b>About this view:</b> a plain-language account of system behavior, generated from the same model checked by the verifier. It is not separate documentation: if the model changes, this narrative changes too. Each step identifies its component; open “How it works” to see its inputs, dependencies, and branches.</div>`;
 if((s.external_systems||[]).length)return help+drillExplorer(s)+regenCard(s);
 const back=cur>0&&SYSTEMS[0]&&(SYSTEMS[0].external_systems||[]).length?`<div class="process-breadcrumb"><button type="button" class="link" data-open-system="0">← Return to the process map</button><span class="mut">System › ${esc(s.viewer_key||s.name)}</span></div>`:"";
 return back+help+narrativeOne(s,false)}
function regenCard(s){const dir="rules\\"+String(s.ref||"").replace(/\./g,"\\"),out="generated\\"+String(s.ref||s.name).split(".").pop();
 const st=s.stores||[],rp=s.replays||[];
 return `<div class="card"><h3 style="margin:0 0 6px">For engineers: replay, regenerate, and compare with reality</h3>
 <p class="small">This narrative, simulator, and generated code use the same verified IR (${esc(s.verdict)}). To regenerate the system in .NET and verify that the code reproduces the model step by step:</p>
 <pre class="small">python -m upa build ${esc(dir)}\\${esc(s.file||"model.upa")} --scope ${esc(dir)}\\informal-scope.md --out ${esc(dir)} --strict-model
python -m upa generate ${esc(dir)}\\canonical-ir.json --out ${esc(out)}
dotnet run --project ${esc(out)}\\Upa.Generated.csproj -- ${esc(out)}\\conformance-traces.json ${esc(out)}\\csharp-validation.json
python -m upa replay ${esc(dir)} --cases cases.jsonl --out ${esc(dir)}\\replay
python -m upa owners rules</pre>
 ${st.length?`<details><summary class="small"><b>Real tables represented (${st.length})</b></summary><table class="small">${st.map(x=>`<tr><td><code>${esc(x.entity)}</code></td><td>${esc(x.table)}</td><td class="mut">key ${esc(x.key||"?")} ${x.evidence?"· "+esc(x.evidence):""}</td></tr>`).join("")}</table></details>`:`<p class="small mut">This UPA defines no real tables (<code>store</code>) of its own; see each component.</p>`}
 ${rp.map(r=>`<details open><summary class="small"><b>Real cases compared with the model</b> (${esc(r.dir)}): ${r.case_count} cases, ${r.observation_count} observations, reproduces ${r.reproduction_pct}% ${Object.entries(r.by_status||{}).map(([k,v])=>`<span class="badge ${k=="CONTRADICTS"?"bad":k=="REPRODUCES"?"ok":"info"}">${esc(STATUS_LABEL[k]||k)} ${v}</span>`).join(" ")}</summary><table class="small"><tr><th>Case</th><th>Count</th><th>Expected</th><th>Result</th><th>Explained by</th></tr>${r.rows.map(f=>`<tr><td>${esc(f.label)}</td><td>${f.n}</td><td>${esc(Object.entries(f.expect||{}).map(([k,v])=>k+"="+v).join(", "))}</td><td>${esc(STATUS_LABEL[f.status]||f.status)}</td><td>${f.status=="REPRODUCES"?"any value":esc((f.explained_by||[]).map(e=>Object.entries(e).map(([k,v])=>hum(k)+": "+(v?"yes":"no")).join(", ")).join(" | ")||"—")}</td></tr>`).join("")}</table></details>`).join("")}
 </div>`}
const CAL_TAB=["quality","Model fidelity and coverage"],IMP_TAB=["impact","Change impact"],DIF_TAB=["changes","Version comparison"];
let impSel="",impQ="";
const ORIGIN_HELP={"location + interpretation":"Identifies where to look in the code and what the modeler understood. The location can be checked; the wording cannot.","modeler interpretation":"Text written by the modeler without a verifiable location.","derived decision":"A design choice recorded as a decision (DD_…), not a fact about the original system.","modeler assumption":"Assumed for modeling; no evidence is available.","unsupported":"No supporting evidence was declared."};
const ORIGIN_LABEL={"location + interpretation":"Location and interpretation","modeler interpretation":"Modeler interpretation","derived decision":"Derived decision","modeler assumption":"Modeler assumption","unsupported":"No supporting evidence"};
const STATUS_LABEL={CONTRADICTS:"Contradicts",REPRODUCES:"Reproduces",PARTIALLY_REPRODUCES:"Partially reproduces"};const statusBadge=x=>x=="PROVEN"?'<span class="badge ok">Proven</span>':x=="REFUTED"?'<span class="badge bad">Refuted</span>':x=="N/A"?'<span class="badge info">Not applicable</span>':'<span class="badge warn">Unresolved</span>';
function whatIfCandidates(s){const out=Object.assign({},((s.insight||{}).impact)||{}),add=(id,text,kind,ref,status)=>{if(!out[id])out[id]={text,kind,block:"formal model",evidence:{class:"formal model",ref},reads:[],writers:[],untouched_ops:0,processes:[],components:[],tables:[],replay:[],owners:[],generated:[],synthetic:true,status}};
 const file=s.file||"UPA file";
 Object.values(s.groups||{}).flat().forEach(r=>add(r.id,r.text,"Business rule",file+(r.line?` line ${r.line}`:""),r.status));
 Object.entries(s.formal||{}).forEach(([id,e])=>{if(["operation","compose","query","invariant","law","ensures","repeat"].includes(e.kind))add(id,e.text||`${FK[e.kind]||e.kind} ${id}`,e.kind,file+(e.line?` line ${e.line}`:""),e.status);
  (e.obligations||[]).forEach(o=>add(`${id}::${o.id}`,o.description||o.id,`Obligation for ${id}`,file+(e.line?` line ${e.line}`:""),o.status))});
 return out}
function refutedHtml(s){const R=(s.insight||{}).refuted||[];if(!R.length)return `<div class="card"><h3 style="margin-top:0">Refuted rules</h3><p class="small">None: the verifier found no counterexample to any rule.</p></div>`;
 return `<div class="card"><h3 style="margin-top:0">Refuted rules (${R.length})</h3>${R.map(r=>`<div class="card" style="border-left:4px solid #991b1b"><p><b>Rule:</b> “${esc(r.text)}”</p>
 <p class="small mut">Evidence: ${esc(ORIGIN_LABEL[r.origin]||r.origin)}${r.origin_ref?` — ${esc(r.origin_ref)}`:""}</p>
 <p><b>Counterexample</b> (operation <code>${esc(r.operation||"?")}</code>):</p><ul class="rel">${Object.entries(r.params||{}).map(([k,v])=>`<li>${esc(hum(k))}: <b>${esc(v)}</b></li>`).join("")}${(r.pre||[]).map(x=>`<li>Before: ${esc(x)}</li>`).join("")}${(r.post||[]).map(x=>`<li>After: ${esc(x)}</li>`).join("")}</ul>
 <p><b>Path that allows it:</b> ${r.path.map(x=>`<code>${esc(x)}</code>`).join(" → ")||"—"}</p><p><b>Possible cause:</b> ${esc(r.cause)}</p>
 <details><summary class="small">Raw verifier model</summary><pre class="small">${esc(JSON.stringify(r.raw,null,1))}</pre></details></div>`).join("")}</div>`}
function quality(s){const q=s.insight.quality,o=q.origins,tot=Math.max(q.rules,1);
 const bar=Object.entries(o).map(([k,v])=>`<tr><td>${esc(ORIGIN_LABEL[k]||k)}</td><td>${v}</td><td>${Math.round(100*v/tot)}%</td><td class="small mut">${esc(ORIGIN_HELP[k]||"")}</td></tr>`).join("");
 const rg=q.regeneration||{};
 return `<div class="help"><b>Model fidelity and coverage</b> answer: “How closely does the model match the real system, and how much does it cover?” The number of proofs (PROVEN) measures something else: whether the model is internally consistent.</div>
<div class="grid">
<div class="card"><div class="big">${q.traceable_pct}%</div>of rules have traceable evidence or a recorded decision</div>
<div class="card"><div class="big">${q.verified_extracts}</div>source quotations verified against the source<br><span class="small mut">The model does not yet store these; all text is currently interpretation.</span></div>
<div class="card"><div class="big">${q.accepted_by_owner}</div>rules accepted by an owner<br><span class="small mut">No acceptance record</span></div>
<div class="card"><div class="big">${q.replay_pct==null?"—":q.replay_pct+"%"}</div>of ${q.replay_cases} real cases reproduced by the model</div>
<div class="card"><div class="big">${q.ops_with_concurrency}/${q.operations}</div>operations with declared concurrency protection<br><span class="small mut">${q.concurrency_gaps} undeclared group(s)</span></div>
<div class="card"><div class="big">${q.persists}</div>persisted data not used by any rule<br><span class="small mut">${q.risks} detected risk(s)</span></div>
<div class="card"><div class="big">${q.assumptions}</div>assumption(s) · ${q.human_open} open human decision(s) · ${q.questions_open} question(s) · ${q.language_gaps} language gap(s)</div>
<div class="card"><div class="big">${rg.percentage==null?"—":rg.percentage+"%"}</div>regeneration coverage<br><span class="small mut">${esc(rg.state||"")}</span></div></div>
<div class="card"><h3 style="margin-top:0">What supports each rule?</h3><table class="small"><tr><th>Evidence type</th><th>Rules</th><th>%</th><th>Meaning</th></tr>${bar}</table>
<p class="small mut">Evidence levels: verified extract (exact quote checked) · location reference · modeler interpretation · decision accepted by an owner. Classification is a heuristic based on the text of <code>origin literal(…)</code>.</p></div>
<div class="card"><h3 style="margin-top:0">Internal consistency</h3><p class="small">${q.proven} proofs passed · ${q.refuted} refuted · ${q.unknown} unresolved. This is a secondary measure: it shows that the rules do not contradict each other, not that they accurately describe the system.</p></div>
${refutedHtml(s)}`}
const IMPACT_KIND={"business rule":"Business rule","formal model":"Formal model","obligation":"Obligation"};
function impact(s){const banner=activeRuleBanner(s);s=activeRuleContext(s);const I=whatIfCandidates(s),ids=Object.keys(I).filter(k=>!impQ||(k+" "+(I[k].text||"")).toLowerCase().includes(impQ.toLowerCase()));if(!impSel||!I[impSel])impSel=ids[0]||"";const r=I[impSel];
 const L=(title,items)=>items.length?`<p><b>${title}:</b> ${items.join(" · ")}</p>`:`<p class="mut"><b>${title}:</b> none declared</p>`;
 const body=!r?"<p>No rules found.</p>":r.synthetic?`<div class="card"><h3 style="margin-top:0">${esc(r.text||impSel)}</h3><p class="small mut"><code>${esc(impSel)}</code> · ${esc(IMPACT_KIND[r.kind]||r.kind)} · ${esc(r.evidence.ref||"formal model")}</p><p><span class="badge info">${esc(r.status||"formal")}</span> This item belongs to the selected node but has no precomputed impact. Run the formal what-if and compare verifier results to determine its impact.</p></div>`:`<div class="card"><h3 style="margin-top:0">${esc(r.text||impSel)}</h3><p class="small mut"><code>${esc(impSel)}</code> · ${esc(IMPACT_KIND[r.kind]||r.kind)} · block ${esc(r.block||"—")}</p>
 <p><b>Evidence:</b> ${esc(ORIGIN_LABEL[r.evidence.class]||r.evidence.class)}${r.evidence.ref?` — ${esc(r.evidence.ref)}`:""}</p>
 ${L("Reads",r.reads.map(x=>`<code>${esc(x)}</code>`))}
 <p><b>Operations that affect this rule (${r.writers.length}):</b> the other ${r.untouched_ops} operation(s) do not change anything the rule reads.</p>
 <table class="small"><tr><th>Operation</th><th>Writes</th><th>Rule preserved</th></tr>${r.writers.map(w=>`<tr><td><code>${esc(w.op)}</code></td><td>${w.writes.map(x=>`<code>${esc(x)}</code>`).join(" ")}</td><td>${statusBadge(w.status)}</td></tr>`).join("")}</table>
 ${L("Parent processes",r.processes.map(x=>`<code>${esc(x)}</code>`))}
 ${L("Components (by block name)",r.components.map(c=>`${esc(c.name)} <span class="mut">${esc(c.repo||"")}</span>`))}
 ${L("Real tables",r.tables.map(t=>`<code>${esc(t.entity)}</code> → ${esc(t.table)}`))}
 ${L("Replay cases",r.replay.map(x=>`${esc(x.label)} (${esc(x.status)})`))}
 ${L("Declared owners",r.owners.map(esc))}
 ${L("Generated code",r.generated.slice(0,12).map(x=>`<code>${esc(x)}</code>`))}</div>`;
 return `${banner}<div class="help"><b>If I change this rule in ${esc(s.name)}…</b> select a rule to see which operations, processes, components, and real cases depend on it. The analysis is based on what each rule reads and each operation writes.</div>
 <div class="card"><input placeholder="Search rules…" value="${esc(impQ)}" onchange="impQ=this.value;impSel='';draw()" style="width:60%;padding:6px"> <select onchange="impSel=this.value;draw()" style="max-width:100%">${ids.map(k=>`<option value="${esc(k)}"${k==impSel?" selected":""}>${esc((I[k].text||k).slice(0,110))}</option>`).join("")}</select> <span class="small mut">${ids.length} rule(s)</span></div>${whatIfPanel(s)}${body}`}
function changes(s){const d=s.insight.diff;if(!d)return `<div class="help">To view changes, generate the viewer with <code>python -m upa view … --baseline &lt;folder containing the previous version&gt;</code>.</div>`;
 const sec=(t,a,f)=>a.length?`<div class="card"><h3 style="margin-top:0">${t} (${a.length})</h3><ul class="rel">${a.map(x=>`<li>${f(x)}</li>`).join("")}</ul></div>`:"";
 const n=Object.values(d.summary).reduce((a,b)=>a+b,0);
 return `<div class="help"><b>Semantic diff</b>: compares meaning, not the text of the <code>.upa</code> files.</div>${n?"":'<div class="card">No semantic differences.</div>'}
 ${sec("Obligations with changed status",d.obligations,x=>`<code>${esc(x.id)}</code>: ${statusBadge(x.before)} → ${statusBadge(x.after)}`)}
 ${sec("Added rules",d.rules.added,x=>`${esc(x.text||x.id)}`)}${sec("Removed rules",d.rules.removed,x=>`${esc(x.text||x.id)}`)}${sec("Modified rules",d.rules.modified,x=>`${esc(x.text||x.id)}<div class="small mut">Before: ${esc(x.before||"")}</div>`)}
 ${sec("Guards",d.guards,x=>`<code>${esc(x.op)}</code> → ${esc(x.outcome)}: ${esc(x.change)}`)}${sec("Outcomes and operations",d.outcomes,x=>`<code>${esc(x.op)}</code> ${esc(x.change)} ${esc(x.outcome||"")}`)}${sec("Effects",d.effects,x=>`<code>${esc(x.op)}</code> ${esc(x.change)}: ${esc(x.effect)}`)}
 ${sec("Removed states",d.states_removed,esc)}${sec("Added states",d.states_added,esc)}${sec("Data and attributes",d.entities,x=>`${esc(x.change)}: <code>${esc(x.item)}</code>`)}
 ${sec("Added boundaries",d.boundaries.added,esc)}${sec("Removed boundaries",d.boundaries.removed,esc)}${sec("Assumptions",d.assumptions,x=>`<code>${esc(x.id)}</code> ${esc(x.change)}`)}
 ${sec("Invalidated evidence",d.evidence_invalidated,x=>`<code>${esc(x.rule)}</code>: ${esc(x.why)}`)}${sec("Potentially affected components",d.components,x=>`${esc(x.name)} <span class="mut">${esc(x.repo||"")}</span>`)}`}
const VIEWS={map:systemMap,formal,levels,"component-flow":componentFlow,simulate,narrative,quality,impact,changes,
summary(s){
const nr=Object.values(s.groups).flat().length,ok=Object.values(s.groups).flat().filter(r=>r.status=="VERIFIED").length;
const v=s.verdict=="VERIFIED"?'<span class="badge ok">All rules verified</span>':vbadge(s.verdict);
return `<div class="help"><b>How to read this:</b> each tab presents the system in business terms. “Verified” means a tool proved that the rules always hold, within the scope of the model. <b>Open questions</b> are matters only the business can answer.</div>
<div class="card"><h2>${esc(s.name)}</h2>${v} <span class="mut small">${s.summary.PROVEN||0} automated proofs passed, ${s.summary.REFUTED||0} refuted</span></div>
<div class="grid">
<div class="card"><div class="big">${s.capabilities.length}</div>available actions<br><a href="#" onclick="tab='flows';draw();return false">View workflows</a></div>
<div class="card"><div class="big">${nr}</div>business rules (${ok} verified; the rest describe what exists or is allowed)<br><a href="#" onclick="tab='rules';draw();return false">View rules</a></div>
<div class="card"><div class="big">${s.states.states.length}</div>possible states, ${s.states.edges.length} allowed transitions<br><a href="#" onclick="tab='states';draw();return false">View states</a></div>
<div class="card"><div class="big">${s.questions.length}</div>open business question(s)<br><a href="#" onclick="tab='questions';draw();return false">View questions</a></div>
<div class="card"><div class="big">${s.gaps.length}</div>real behaviors not yet expressible<br><a href="#" onclick="tab='questions';draw();return false">View limitations</a></div></div>
${s.origin.repo?`<div class="card small mut">Reconstructed from: ${esc(s.origin.repo)}. Observed traffic source: ${esc(s.origin.dynatrace||"—")}.</div>`:""}
${s.questions.length?`<div class="card"><h3 style="margin-top:0">Business decisions needed</h3>${s.questions.map(q=>`<p>❓ ${esc(q.text)}</p>`).join("")}</div>`:""}`},
flows(s){return `<div class="help">Each card shows an action a user can request. Follow the path downward: when a red check fails, the action is rejected <b>without changing state</b>; when all checks pass, it proceeds.</div>`+s.capabilities.map(c=>{
const rej=c.steps.filter(x=>x.kind=="rejects"),oth=c.steps.filter(x=>x.kind!="rejects");
const ef=c.effect;
let f=`<div class="flow"><div class="box start">Requested action: ${esc(c.name)}${ef?`<div class="small" style="font-weight:400">Inputs: ${ef.inputs.map(i=>i.generated?`<i>${esc(i.name)} (assigned by the system)</i>`:esc(i.name)).join(", ")}</div>`:""}</div>`;
rej.forEach(r=>{f+=`<div class="arrow">↓</div><div class="dec"><div class="q"><span class="lbl">Check</span><br>Does ${esc(cap(r.when))} hold?</div><div class="no"><span class="lbl">If so →</span>Rejected: <b>${esc(r.result)}</b><br><span class="small">${esc(r.effect||"")}</span> ${badge(r.status)}</div></div>`});
f+=`<div class="arrow">↓</div>`;
f+=oth.length?oth.map(o=>`<div class="box end">${o.kind=="otherwise"?"If all checks pass: ":""}${esc(o.text)} ${badge(o.status)}</div>`).join(""):`<div class="box end">Completed</div>`;
if(ef){const rows=a=>a.map(w=>`<tr><td>${w.kind=="creates"?"<b>Created</b>":w.kind=="deletes"?"<b>Deleted</b>":w.kind=="deletes_each"?"<b>All matching records deleted</b>":w.kind=="sets_each"?"All matching records updated":"Updated"}</td><td>${esc(w.entity)}${w.key?` <span class="mut small">(identified by ${esc(w.key)})</span>`:""}</td><td>${w.fields.map(x=>`<b>${esc(x.name)}</b> = ${esc(x.value)}`).join("<br>")}</td></tr>`).join("");
f+=`<div class="arrow">↓</div><div class="box" style="text-align:left"><b>What gets stored</b> ${badge(ef.status)}<table style="margin-top:6px"><tr><th></th><th>Data</th><th>Values</th></tr>${rows(ef.writes)}</table><div class="small mut" style="margin-top:6px">Nothing else changes: this is a verified guarantee (<code>unchanged except</code>), not just a description.</div>${ef.technical.length?`<details class="small"><summary>Technical detail (not business behavior)</summary><table>${rows(ef.technical)}</table><span class="mut">An internal counter used for numbering or counting; this is an implementation choice, not a business rule.</span></details>`:""}</div>`}
return `<div class="card"><h3 style="margin-top:0">${esc(c.name)}</h3>${rej.length?`<span class="small mut">${rej.length} check(s) before completion</span>`:""}${f}</div></div>`}).join("")},
states(s){const st=s.states;if(!st.states.length)return'<div class="card">This system does not define any states.</div>';
const W=900,H=560,cx=W/2,cy=H/2,R=Math.min(W,H)/2-70,n=st.states.length,pos={};
st.states.forEach((x,i)=>{const a=2*Math.PI*i/n-Math.PI/2;pos[x]={x:cx+R*1.35*Math.cos(a),y:cy+R*Math.sin(a)}});
const boxWidth=150,boxHeight=34;let svg=`<svg viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="State diagram"><defs><marker id="ar" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0L10 5L0 10z" fill="#64748b"/></marker></defs>`;
const pairs=new Set(st.edges.map(e=>e.from+"|"+e.to));
st.edges.forEach((e,i)=>{const a=pos[e.from],b=pos[e.to],dx=b.x-a.x,dy=b.y-a.y,d=Math.hypot(dx,dy)||1,ux=dx/d,uy=dy/d;
const off=pairs.has(e.to+"|"+e.from)?14:0,nx=-uy*off,ny=ux*off;
const x1=a.x+ux*(boxWidth/2+2)+nx,y1=a.y+uy*(boxHeight/2+2)+ny,x2=b.x-ux*(boxWidth/2+8)+nx,y2=b.y-uy*(boxHeight/2+8)+ny;
svg+=`<line class="edge" data-f="${esc(e.from)}" data-t="${esc(e.to)}" x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" marker-end="url(#ar)"/>`});
st.states.forEach(x=>{const p=pos[x];svg+=`<g class="node ${x==st.initial?'init':''}" data-s="${esc(x)}"><rect x="${p.x-boxWidth/2}" y="${p.y-boxHeight/2}" width="${boxWidth}" height="${boxHeight}" rx="8"/><text x="${p.x}" y="${p.y+5}" text-anchor="middle">${esc(cap(x))}</text></g>`});svg+="</svg>";
const mat=`<table><tr><th>From ↓ / To →</th>${st.states.map(x=>`<th>${esc(cap(x))}</th>`).join("")}</tr>${st.states.map(f=>`<tr><th>${esc(cap(f))}</th>${st.states.map(t=>`<td style="text-align:center">${pairs.has(f+"|"+t)?"✔":""}</td>`).join("")}</tr>`).join("")}</table>`;
return `<div class="help">Select a state to highlight its incoming and outgoing transitions. The thick blue border marks the initial state${st.initial?` (“${esc(st.initial)}”)`:""}.</div>
<div class="card"><h2>States for ${esc(st.concept||"the entity")}</h2>${svg}</div>
<div class="card"><h3 style="margin-top:0">Allowed transitions</h3><div style="overflow:auto">${mat}</div></div>
${st.unproduced.length?`<div class="card"><span class="badge warn">Warning</span> No described operation reaches: <b>${st.unproduced.map(x=>esc(x)).join(", ")}</b>. These states are produced by functions that have not yet been analyzed.</div>`:""}`},
rules(s){const sec=Object.entries(s.groups).map(([g,rs])=>`<div class="card"><h2>${esc(cap(g))}</h2>${rs.map(r=>`<div class="rule" data-t="${esc(r.text.toLowerCase())}"><div class="t">${esc(r.text)} ${chips(r.text)}</div>${badge(r.status)}</div>`).join("")}</div>`).join("");
const ini=s.initial_rules.length?`<div class="card"><h2>Starting assumptions</h2><p class="small mut">This assumption is needed to verify the model; it is not a discovered business rule.</p>${s.initial_rules.map(r=>`<div class="rule"><div class="t">${esc(r.text)}</div><span class="badge info">Assumption</span></div>`).join("")}</div>`:"";
const tec=s.technical.length?`<details class="card"><summary>Technical guarantees (${s.technical.length}) — for implementers</summary>${s.technical.map(r=>`<div class="rule"><div class="t">${esc(r.text)}<div class="small mut">${esc(r.why||"")}</div></div>${badge(r.status=="VERIFIED"?"VERIFIED":"INCONCLUSIVE")}</div>`).join("")}</details>`:"";
return `<input class="q" placeholder="Search rules… (e.g. status, channel, member)" oninput="filt(this.value)">${sec}${ini}${tec}`},
concepts(s){return s.concepts.map(c=>`<div class="card"><h2>${esc(c.name)}</h2><p class="small mut">Identifier: ${esc(c.identity||"—")}</p><table><tr><th>Data</th><th>Required</th></tr>${c.props.map(p=>`<tr><td>${esc(p.name)}</td><td>${p.required?"Yes":"No"}</td></tr>`).join("")}</table></div>`).join("")+(s.relations.length?`<div class="card"><h3 style="margin-top:0">Relationships</h3>${s.relations.map(r=>`<p>🔗 ${esc(r.text)}</p>`).join("")}</div>`:"")},
questions(s){const gs=(s.notes||{}).language_gaps||[],cv=s.regeneration_coverage||{state:"not_evaluated",declared_surfaces:[],open_required:[]};
const cn={complete:"Complete",partial:"Partial",not_evaluated:"Not evaluated"}[cv.state]||cv.state;
const cls={language:"Language",evidence:"Evidence",regeneration:"Regeneration"},sev={low:"Low",medium:"Medium",high:"High",critical:"Critical"};
const gapRows=gs.slice().sort((a,b)=>(a.kind+"|"+a.impact+"|"+(a.target||a.scope||"")+"|"+a.id).localeCompare(b.kind+"|"+b.impact+"|"+(b.target||b.scope||"")+"|"+b.id));
return `<div class="card"><h2>Regeneration coverage</h2><div class="rule"><div class="t"><b>${esc(cn)}</b><div class="small mut">Declared surfaces: ${cv.declared_surfaces.length?cv.declared_surfaces.map(esc).join(", "):"none"}. Required outstanding items: ${(cv.open_required||[]).length?(cv.open_required||[]).map(esc).join(", "):"none"}. No percentage is calculated.</div><div class="small"><b>${esc(cv.verification_result||"Not evaluated")}</b> is the formal verification result; it does not mean the system can be regenerated.</div></div><span class="badge ${cv.state=="complete"?"ok":cv.state=="partial"?"warn":"info"}">${esc(cn)}</span></div></div>
<div class="card"><h2>Gaps by class, severity, and scope</h2>${gapRows.length?`<table><tr><th>ID</th><th>Class</th><th>Severity</th><th>Scope</th><th>Required</th><th>Description</th></tr>${gapRows.map(g=>`<tr><td><code>${esc(g.id)}</code></td><td>${esc(cls[g.kind]||g.kind)}</td><td>${esc(sev[g.impact]||g.impact)}</td><td>${esc(g.target||g.scope||"undeclared (legacy)")}</td><td>${g.required?"Yes":"No"}</td><td>${esc(g.text)}</td></tr>`).join("")}</table>`:"<p>None.</p>"}</div>
<div class="card"><h2>Open questions</h2><p class="small mut">Only questions the system and its sources cannot answer are listed here; they require a business decision.</p>${s.questions.length?s.questions.map(q=>`<div class="rule"><div class="t"><b>${esc(q.id)}</b> ${esc(q.text)} ${chips(q.text)}${q.affects.length?`<div class="small mut">Affects: ${q.affects.map(esc).join(", ")}</div>`:""}</div><span class="badge warn">Open</span></div>`).join(""):"<p>None.</p>"}</div>
<div class="card"><h2>Decisions made</h2>${s.decisions.map(d=>`<div class="rule"><div class="t"><b>${esc(d.id)}</b> ${esc(d.text)} ${chips(d.text)}</div><span class="badge info">Decided</span></div>`).join("")||"<p>None.</p>"}</div>
<div class="card"><h2>Assumptions</h2>${s.assumptions.map(d=>`<div class="rule"><div class="t"><b>${esc(d.id)}</b> ${esc(d.text)}<div class="small mut">Owner: ${esc(d.owner||"—")}</div></div><span class="badge warn">Assumption</span></div>`).join("")||"<p>None.</p>"}</div>
<div class="card"><h2>Real behavior not yet expressible</h2><p class="small mut">Compatibility notes for legacy surface gaps.</p>${s.gaps.map(g=>`<div class="rule"><div class="t"><b>${esc(g.id)}</b> ${esc(g.text)} ${chips(g.text)}${g.proposal?`<div class="small mut">Suggested expression: ${esc(g.proposal)}</div>`:""}</div><span class="badge info">Language limitation</span></div>`).join("")||"<p>None.</p>"}</div>`},
evidence(s){if(!s.evidence.length&&!s.evidence_rules.length)return'<div class="card">No reverse-engineering evidence is recorded for this system.</div>';
const evidenceStatus=x=>({confirmed:"Confirmed",refuted:"Refuted",inconclusive:"Inconclusive"}[x]||x);
return `<div class="help">A counterexample was actively sought for each important rule. This tab shows the source of each claim.</div>
<div class="card"><h2>Rules and supporting evidence</h2>${s.evidence_rules.map(r=>`<div class="rule"><div class="t">${esc(r.rule)}<div class="small mut">Counterexample sought: ${esc(r.counterexample||"—")}</div><div>${(r.evidence||[]).map(e=>`<span class="chip" onclick="goEv('${e}')">${e}</span>`).join("")}</div></div><span class="badge ${r.status=="confirmed"?"ok":"warn"}">${esc(evidenceStatus(r.status))}</span></div>`).join("")}</div>
<div class="card"><h2>Evidence</h2><table><tr><th>ID</th><th>Type</th><th>Finding</th><th>Location</th></tr>${s.evidence.map(e=>`<tr id="${esc(e.id)}"><td>${esc(e.id)}</td><td>${esc(e.type)}</td><td>${esc(e.finding)}</td><td class="small mut">${esc(e.ref)}</td></tr>`).join("")}</table></div>`}};
function filt(v){v=v.toLowerCase();document.querySelectorAll(".rule[data-t]").forEach(r=>r.style.display=r.dataset.t.includes(v)?"":"none")}
function stateEvents(){const nodes=[...document.querySelectorAll(".node")],edges=[...document.querySelectorAll(".edge")];
nodes.forEach(n=>n.onclick=()=>{const s=n.dataset.s,on=n.classList.contains("sel");nodes.forEach(x=>x.classList.remove("sel","dim"));edges.forEach(e=>e.classList.remove("hl","dim"));if(on)return;
const rel=new Set([s]);edges.forEach(e=>{const m=e.dataset.f==s||e.dataset.t==s;e.classList.add(m?"hl":"dim");if(m){rel.add(e.dataset.f);rel.add(e.dataset.t)}});nodes.forEach(x=>{x.classList.add(rel.has(x.dataset.s)?"sel":"dim")})})}
const sel=$("#sys");SYSTEMS.forEach((x,i)=>sel.add(new Option(x.name,i)));sel.onchange=()=>{cur=+sel.value;DETAIL_PATH=[];draw()};if(SYSTEMS.length<2)sel.style.display="none";
if(window.addEventListener)window.addEventListener("resize",()=>CY_GRAPHS.forEach(c=>{c.resize();c.fit(undefined,55)}));
draw();
</script></body></html>
"""
