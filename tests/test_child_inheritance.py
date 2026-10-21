"""Inherit PRES/TYPE proofs from an already-verified component (verifier.child_proof)."""
import sys
from pathlib import Path

from upa import compiler

ATOM = '''spec CounterComponent version "1.0"
entity Counter { n: Int }
initial origin literal("empty"): forall c: not (c in Counter)
operation Add(id: Identifier) {
  when id in Counter { set Counter.n[id] = Counter.n[id] + {DELTA} }
  otherwise { create Counter[id] { n = 1 } }
}
business_rules {
  invariant BR_POS origin literal("positive") "the counter is positive": forall c: (c in Counter) => Counter.n[c] >= 1
}
'''

PARENT = '''spec UseCase version "1.0"
component C = org.counter@1.0
compose Step(id: Identifier) = C.Add(id)
'''


def _build(tmp_path: Path, delta: str):
    root = tmp_path / "rules"
    (root / "org" / "counter").mkdir(parents=True)
    (root / "org" / "usecase").mkdir(parents=True)
    (root / "org" / "counter" / "counter.upa").write_text(ATOM.replace("{DELTA}", delta), encoding="utf-8")
    p = root / "org" / "usecase" / "usecase.upa"
    p.write_text(PARENT, encoding="utf-8")
    res = compiler.build(p, None, p.parent)
    import json
    obs = {o["id"]: o for o in json.loads((p.parent / "proof-obligations.json").read_text(encoding="utf-8"))}
    return res, obs


def test_parent_inherits_child_proof(tmp_path):
    res, obs = _build(tmp_path, "1")
    o = obs["PRES:C_BR_POS@C_Add"]
    assert res["verdict"] == "VERIFIED"
    assert o["status"] == "PROVEN" and o["inherited_from"] == "C/PRES:BR_POS@Add"


def test_parent_does_not_inherit_unproven_child(tmp_path):
    res, obs = _build(tmp_path, "0 - 5")
    o = obs["PRES:C_BR_POS@C_Add"]
    assert o["inherited_from"] is None and o["status"] == "REFUTED"
    assert res["verdict"] == "REFUTED"

PARENT2 = """spec UseCase version "1.0"
component C = org.counter@1.0
component D = org.counter@1.0
compose Step(id: Identifier) = C.Add(id)
compose Another(id: Identifier) = D.Add(id)
"""


def test_duplicate_rule_reuses_proof(tmp_path, monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "PARENT", PARENT2)
    res, obs = _build(tmp_path, "1")
    dup = [o for o in obs.values() if o.get("duplicate_of")]
    assert dup, "the imported rule should be proved only once even when imported twice"
    for o in dup:
        assert o["status"] == obs[o["duplicate_of"]]["status"]
    assert res["verdict"] == "VERIFIED"


