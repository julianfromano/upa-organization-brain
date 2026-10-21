"""Database boundary declarations: `store Entity in "catalog..table" key "column" { attr = "column" }`.

UPA does not emulate the database. It models decisions and declares which real tables represent its entities.
`upa owners` indexes reads and writes across a rules/ tree and flags tables written by multiple UPAs for
coordination or explicit boundary modeling.
"""
from __future__ import annotations

import json
from pathlib import Path


def check_stores(spec, errors) -> None:
    ents = {e.name: {a.name for a in e.attrs} for e in spec.entities}
    seen = set()
    for st in spec.stores:
        if st["entity"] not in ents:
            errors.add("STORE_UNKNOWN", f"store: {st['entity']!r} is not an entity in this model", st["pos"])
            continue
        if st["entity"] in seen:
            errors.add("STORE_DUPLICATE", f"store: {st['entity']!r} already has a table", st["pos"])
        seen.add(st["entity"])
        for a in st["columns"]:
            if a not in ents[st["entity"]]:
                errors.add("STORE_UNKNOWN", f"store: {st['entity']}.{a} is not an attribute", st["pos"])


def _reads(e, acc: set) -> None:
    if isinstance(e, dict):
        if e.get("k") in ("attr", "in", "sum") and e.get("e"):
            acc.add(e["e"])
        for v in e.values():
            _reads(v, acc)
    elif isinstance(e, list):
        for v in e:
            _reads(v, acc)


def owners(root: Path) -> dict:
    """Index tables to writers and readers using compiled canonical-ir.json files under root."""
    tables: dict[str, dict] = {}
    for f in sorted(Path(root).rglob("canonical-ir.json")):
        ir = json.loads(f.read_text(encoding="utf-8"))
        stores = {s["entity"]: s for s in ir.get("stores", [])}
        if not stores:
            continue
        spec = f"{ir.get('spec')}@{ir.get('version')}"
        rel = str(f.parent.relative_to(root)) if f.parent != Path(root) else "."
        for ent, st in stores.items():
            t = tables.setdefault(st["table"], {"table": st["table"], "writers": [], "readers": [], "entities": []})
            t["entities"].append({"spec": spec, "dir": rel, "entity": ent, "key": st.get("key"),
                                  "columns": st.get("columns", {}), "evidence": st.get("evidence")})
        for o in ir["operations"]:
            if "_" in o["name"]:
                continue  # Flattened component operation; its source UPA owns the operation.
            writes: dict[str, set] = {}
            reads: set = set()
            for c in o["cases"]:
                _reads(c.get("cond"), reads)
                for s in c["stmts"]:
                    cols = writes.setdefault(s["entity"], set())
                    if s["kind"] == "set":
                        cols.add(s["attr"])
                    elif s["kind"] == "delete":
                        cols.add("(delete)")
                    else:
                        cols.update(i["attr"] for i in s.get("inits", []))
                        cols.add("(create)")
                    _reads(s, reads)
            for ent, cols in writes.items():
                if ent in stores:
                    m = stores[ent].get("columns", {})
                    tables[stores[ent]["table"]]["writers"].append(
                        {"spec": spec, "op": o["name"], "columns": sorted(m.get(c, c) for c in cols)})
            for ent in reads - set(writes):
                if ent in stores:
                    tables[stores[ent]["table"]]["readers"].append({"spec": spec, "op": o["name"]})
    for t in tables.values():
        t["owners"] = sorted({w["spec"] for w in t["writers"]})
        t["shared"] = len(t["owners"]) > 1
    return {"root": str(root), "tables": sorted(tables.values(), key=lambda t: t["table"])}


def owners_markdown(rep: dict) -> str:
    out = ["# Table ownership (writes and reads declared by `store`)", ""]
    for t in rep["tables"]:
        flag = " ⚠ multiple owners" if t["shared"] else ""
        out.append(f"## `{t['table']}`{flag}")
        for e in t["entities"]:
            out.append(f"- Represented by `{e['entity']}` in {e['spec']} ({e['dir']}), key `{e['key'] or '?'}`"
                       + (f" · evidence: {e['evidence']}" if e.get("evidence") else ""))
        for w in t["writers"]:
            out.append(f"- Writes: {w['spec']} `{w['op']}` → {', '.join(w['columns'])}")
        for r in t["readers"]:
            out.append(f"- Reads: {r['spec']} `{r['op']}`")
        out.append("")
    return "\n".join(out)
