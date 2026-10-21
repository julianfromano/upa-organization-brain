"""Importador estructural determinista de OpenAPI/Swagger -> contrato externo UPA.

Extracts only what the document states (operations, parameters, types, and response codes). It does not infer
semantics: idempotency, the meaning of each state, and handling of uncertain outcomes are completed through
semantic enrichment (agent plus review), which becomes a verifiable contract.
"""
from __future__ import annotations
import hashlib
import json
import re
from pathlib import Path

METHODS = ("get", "post", "put", "patch", "delete")


def _load(raw: bytes, name: str):
    if name.lower().endswith((".yaml", ".yml")):
        import yaml
        return yaml.safe_load(raw)
    return json.loads(raw)


def _ref(doc, node):
    seen = 0
    while isinstance(node, dict) and "$ref" in node and seen < 10:
        cur = doc
        for part in node["$ref"].lstrip("#/").split("/"):
            cur = cur[part]
        node, seen = cur, seen + 1
    return node


def _utype(name: str, schema: dict, in_path: bool = False) -> str:
    t = (schema or {}).get("type")
    if t == "boolean":
        return "Bool"
    if t in ("integer", "number"):
        return "Natural" if (schema.get("minimum") is not None and schema["minimum"] >= 0) else "Int"
    if in_path or re.search(r"(^|_)id$|Id$", name):
        return "Identifier"
    return "Text"


def _ident(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_]", "_", s).strip("_")
    return s if s and not s[0].isdigit() else "_" + s


def import_openapi(path: Path, name: str | None = None) -> dict:
    raw = Path(path).read_bytes()
    doc = _load(raw, Path(path).name)
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    ops = []
    for url, item in sorted((doc.get("paths") or {}).items()):
        for m in METHODS:
            if m not in item:
                continue
            op = item[m]
            oid = _ident(op.get("operationId") or f"{m}_{url}")
            params = []
            for p in (item.get("parameters") or []) + (op.get("parameters") or []):
                p = _ref(doc, p)
                params.append({"name": _ident(p["name"]), "in": p["in"], "required": bool(p.get("required")),
                               "type": _utype(p["name"], _ref(doc, p.get("schema") or {}), p["in"] == "path")})
            body = _ref(doc, (((op.get("requestBody") or {}).get("content") or {}).get("application/json") or {}).get("schema") or {})
            req = set(body.get("required") or [])
            for pn, ps in sorted((body.get("properties") or {}).items()):
                ps = _ref(doc, ps)
                params.append({"name": _ident(pn), "in": "body", "required": pn in req, "type": _utype(pn, ps)})
            seen, uniq = set(), []
            for p in params:
                if p["name"] in seen:
                    raise ValueError(f"{oid}: duplicate parameter {p['name']!r} across path/query/body")
                seen.add(p["name"])
                uniq.append(p)
            outcomes = [{"name": "Http" + str(code), "status": str(code), "uncertain": False,
                         "description": _ref(doc, r).get("description", "")}
                        for code, r in sorted((op.get("responses") or {}).items(), key=lambda kv: str(kv[0]))]
            outcomes.append({"name": "Unknown", "status": None, "uncertain": True,
                             "description": "transport failure or timeout: the effect may or may not have occurred (always possible, not specified by OpenAPI)"})
            ops.append({"name": oid, "http": f"{m.upper()} {url}", "params": uniq, "outcomes": outcomes})
    title = _ident(name or (doc.get("info") or {}).get("title") or "External")
    return {"name": title, "version": str((doc.get("info") or {}).get("version", "0")), "source": Path(path).name,
            "hash": digest, "operations": ops}


def to_upa(c: dict) -> str:
    out = [f'// Structural contract imported from {c["source"]} ({c["hash"]}). Origin: literal (OpenAPI).',
           "// Semantic enrichment is missing: per-outcome effects (on ...) and resolve_by for uncertain outcomes.",
           f'external {c["name"]} version "{c["version"]}" source "{c["source"]}" hash "{c["hash"]}" {{']
    for o in c["operations"]:
        ps = ", ".join(f'{p["name"]}: {p["type"]}' for p in o["params"])
        out.append(f'  operation {o["name"]}({ps}) http "{o["http"]}" {{')
        for oc in o["outcomes"]:
            st = f' status "{oc["status"]}"' if oc["status"] else ""
            out.append(f'    outcome {oc["name"]}{st}{" uncertain" if oc["uncertain"] else ""}')
        out.append("  }")
    out.append("}")
    return "\n".join(out) + "\n"
