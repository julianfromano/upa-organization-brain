"""Minimal cumulative catalog: resolve(candidate, corpus) -> REUSE | EXTEND | SPECIALIZE | NEW.

A candidate has {name, attrs: {attribute: type}, rules: [ids/text]}; published blocks add
version and hash. An empty corpus always returns NEW. This is a structural, not semantic,
approximation: a person makes the final decision for nearly identical blocks, recorded in reuse-analysis.json."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path


def block_hash(block: dict) -> str:
    core = {k: block[k] for k in ("name", "attrs", "rules")}
    return hashlib.sha256(json.dumps(core, sort_keys=True).encode()).hexdigest()


def resolve(candidate: dict, corpus: list[dict]) -> dict:
    cattrs, crules = set(candidate["attrs"].items()), set(candidate["rules"])
    best = None
    for b in corpus:
        battrs, brules = set(b["attrs"].items()), set(b["rules"])
        if battrs == cattrs and brules == crules:
            return {"decision": "REUSE", "block": f"{b['name']}@{b['version']}", "why": "identical structure and rules"}
        if battrs <= cattrs and brules <= crules:
            best = best or {"decision": "EXTEND", "block": f"{b['name']}@{b['version']}",
                            "why": "the existing block is contained: adds " + ", ".join(sorted(map(str, (cattrs - battrs) | (crules - brules))))}
        elif cattrs <= battrs and crules <= brules and best is None:
            best = {"decision": "SPECIALIZE", "block": f"{b['name']}@{b['version']}",
                    "why": "the candidate is a subset of the existing block"}
    return best or {"decision": "NEW", "block": None, "why": "no comparable block in the corpus"}


def load_corpus(root: Path) -> list[dict]:
    out = []
    for p in sorted(root.glob("*@*/block.json")):
        b = json.loads(p.read_text(encoding="utf-8"))
        if b.get("hash") != block_hash(b):
            raise ValueError(f"modified block: {p}")
        out.append(b)
    return out


def publish(root: Path, block: dict) -> Path:
    """Publish an immutable block <Name>@<version>; a name@version collision is an error."""
    d = root / f"{block['name']}@{block['version']}"
    if d.exists():
        raise FileExistsError(f"{d.name} already exists: blocks are immutable; publish another version")
    d.mkdir(parents=True)
    block = {**block, "hash": block_hash(block)}
    (d / "block.json").write_text(json.dumps(block, indent=2, sort_keys=True), encoding="utf-8")
    return d


def candidates_from_ir(ir: dict) -> list[dict]:
    out = []
    for e, info in ir["entities"].items():
        rules = [p["id"] for p in ir["properties"] if e + "." in json.dumps(p.get("expr", ""))]
        out.append({"name": e, "attrs": {a["name"]: a["type"] for a in info["attrs"]}, "rules": sorted(rules)})
    return out
