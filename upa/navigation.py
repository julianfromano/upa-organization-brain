"""Component-corpus navigation: from macroprocess to atomic rule, and from each rule to its consumers."""
from __future__ import annotations
import re
from pathlib import Path

from .components import component_path
from .interface import interface


def tree(path: Path, root: Path | None = None, verify_rules: bool = True, timeout_ms: int = 60000,
         _seen: dict | None = None) -> dict:
    from .compiler import compile_source, find_root
    root = root or find_root(path)
    seen = _seen if _seen is not None else {}
    key = str(path.resolve())
    if key not in seen:
        node = interface(path, timeout_ms, verify_rules)
        spec, _, _, _ = compile_source(path.read_text(encoding="utf-8"), None, root)
        node["children"] = [{"alias": c["alias"], "ref": c["ref"],
                             "node": tree(component_path(root, c["ref"]), root, verify_rules, timeout_ms, seen)}
                            for c in spec.component_info]
        seen[key] = node
    return seen[key]


def render_tree(node: dict, with_rules: bool = False, prefix: str = "", label: str | None = None) -> list[str]:
    head = f"{label + ' = ' if label else ''}{node['spec']}@{node['version']} [{node['tier']}] {node['verdict']}"
    lines = [head]
    body = []
    for o in node.get("operations", []):
        if o["imported_from"]:
            continue
        body.append(f"op {o['name']}({o['params']}) → {' | '.join(o['outcomes']) or 'ok'}")
    for c in node.get("composes", []):
        body.append(f"{'atomic ' if c['atomic'] else ''}compose {c['name']}({c['params']})"
                    + (f" ↻ {', '.join(c['loops'])}" if c.get("loops") else ""))
    if with_rules:
        for r in node.get("rules", []):
            body.append(f"rule [{r['status']}] {r['id']}: {r['text']}"
                        + (f"  (imported from {r['imported_from']})" if r["imported_from"] else ""))
        for g in node.get("language_gaps", []):
            where = g.get("target") or g.get("scope") or "legacy"
            body.append(f"gap [{g['kind']}/{g['impact']}] {g['id']} → {where}: {g['text']}")
    else:
        own = [r for r in node.get("rules", []) if not r["imported_from"]]
        if own:
            body.append(f"{len(own)} own rules ({sum(r['status'] == 'PROVEN' for r in own)} PROVEN)")
    kids = node.get("children", [])
    items = [("leaf", b) for b in body] + [("node", k) for k in kids]
    for idx, (kind, item) in enumerate(items):
        last = idx == len(items) - 1
        branch, cont = ("└─ ", "   ") if last else ("├─ ", "│  ")
        if kind == "leaf":
            lines.append(prefix + branch + item)
        else:
            sub = render_tree(item["node"], with_rules, prefix + cont, item["alias"])
            lines.append(prefix + branch + sub[0])
            lines.extend(sub[1:])
    return lines


def _ref_of(root: Path, f: Path) -> str:
    return ".".join(f.relative_to(root).parts[:-1])


def units(root: Path) -> list[dict]:
    """Index of all corpus UPAs: tier, size, and usage."""
    from .compiler import compile_source
    found = {}
    for f in sorted(root.rglob("*.upa")):
        if f.parent == root:
            continue
        text = f.read_text(encoding="utf-8")
        try:
            spec, ir, _, errors = compile_source(text, None, root)
        except Exception:
            continue
        if errors:
            continue
        from .components import effective_tier
        found[_ref_of(root, f)] = {
            "ref": _ref_of(root, f), "version": spec.version, "tier": effective_tier(spec), "text": text,
            "depth": getattr(spec, "component_depth", 0),
            "own_operations": sum(1 for o in spec.operations if not o.origin_block),
            "composes": len(spec.composes), "queries": [q.name for q in spec.queries],
            "own_rules": sum(1 for p in spec.props if not p.origin_block),
        }
    for ref, u in found.items():
        u["used_by"] = sorted(r for r, o in found.items() if r != ref and _depends(o["text"], ref))
    for u in found.values():
        del u["text"]
    return list(found.values())


def _depends(text: str, ref: str) -> bool:
    return bool(re.search(r"\b(component\s+\w+\s*=|uses)\s+" + re.escape(ref) + r"@", text))


def used_by(root: Path, ref: str) -> dict:
    """Who uses `ref`: components that compose it, UPAs that inherit it, and queries and operations used (Alias.Name)."""
    out = {"ref": ref, "composed_by": [], "inherited_by": []}
    for f in sorted(root.rglob("*.upa")):
        if f.parent == root:
            continue
        text = f.read_text(encoding="utf-8")
        who = _ref_of(root, f)
        for m in re.finditer(r"\bcomponent\s+(\w+)\s*=\s*" + re.escape(ref) + r"@", text):
            alias = m.group(1)
            members = sorted(set(re.findall(r"\b" + alias + r"\.(\w+)", text)))
            out["composed_by"].append({"ref": who, "alias": alias, "uses": members})
        if re.search(r"\buses\s+" + re.escape(ref) + r"@", text):
            out["inherited_by"].append({"ref": who})
    return out
