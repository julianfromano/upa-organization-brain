"""Persistence projection: an independent decision derived from Logical IR multiplicities.

Each homogeneous collection (`Customer.phones: Text [1..3]`) maps to a child table with one row per element.
The projection does not mutate Logical IR or its verification; the verifiable core continues to use the
serialized representation and its cardinality.
"""
from __future__ import annotations
import hashlib
import re

from .ir import IR_VERSION, canonical_json
from .multiplicity import range_text

FORMAT = "upa-persistence-ir-1"


def _snake(name: str) -> str:
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name).lower()


def _singular(word: str) -> str:
    if word.endswith("ies") and len(word) > 3:
        return word[:-3] + "y"
    if word.endswith(("sses", "xes", "ches", "shes")):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss") and len(word) > 1:
        return word[:-1]
    return word


def project(ir: dict) -> dict:
    if ir.get("ir_version") != IR_VERSION:
        raise ValueError(f"Logical IR is required {IR_VERSION}")
    tables = []
    for c in ir.get("collections", []):
        parent = _snake(c["entity"])
        table = _singular(_snake(c["attribute"])).upper()
        constraints = [{"kind": "PARENT_REQUIRED", "column": f"{parent}_id", "references": c["entity"]},
                       {"kind": "UNIQUE_POSITION_PER_PARENT", "columns": [f"{parent}_id", "position"]}]
        if c["min"] > 0:
            constraints.append({"kind": "MIN_ROWS_PER_PARENT", "value": c["min"]})
        if c["max"] is not None:
            constraints.append({"kind": "MAX_ROWS_PER_PARENT", "value": c["max"]})
        tables.append({
            "table": table,
            "collection": f"{c['entity']}.{c['attribute']}",
            "element_type": c["element"],
            "multiplicity": range_text(c["min"], c["max"]),
            "columns": [
                {"name": f"{parent}_id", "role": "PARENT_KEY", "references": c["entity"]},
                {"name": "position", "role": "ORDER", "type": "Natural"},
                {"name": "value", "role": "ELEMENT", "type": c["element"]},
            ],
            "constraints": constraints,
            "trace": {"fact": c["fact"], "inference_rule": "COLLECTION_TO_CHILD_TABLE"},
        })
    tables.sort(key=lambda t: t["table"])
    return {"format": FORMAT,
            "logical_ir_sha256": hashlib.sha256(canonical_json(ir).encode("utf-8")).hexdigest(),
            "tables": tables}
