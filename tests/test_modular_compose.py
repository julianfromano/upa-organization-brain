"""Modular proof reuse: a composition inherits PRES/TYPE from its steps, and only when those steps proved them."""
from upa.compiler import compile_source
from upa.verifier import verify

SRC = """spec S version "1.0"
entity Request { status: Int }
initial origin literal("empty"): forall p: not (p in Request)
operation Validate(ok: Bool) {
  when not ok outcome INVALID { }
  when true outcome VALID success { }
}
operation Alta(id: Identifier, n: Int) {
  when id in Request { set Request.status[id] = Request.status[id] + 1 }
  otherwise { create Request[id] { status = {VAL} } }
}
compose atomic CreateRequest(ok: Bool, id: Identifier, n: Int) =
  Validate(ok);
  when ok then Alta(id, n)
business_rules {
  invariant BR_POS origin literal("status is positive") "status is positive": forall p: (p in Request) => Request.status[p] >= 1
}
"""


def _pres(val):
    _, ir, _, errs = compile_source(SRC.replace("{VAL}", val), None)
    assert not errs.items, errs.to_json()
    vr = verify(ir, 20000, None)
    return {o["id"]: o for o in vr["obligations"]}


def test_compose_inherits_proven_steps():
    obs = _pres("1")
    o = obs["PRES:BR_POS@CreateRequest"]
    assert o["status"] == "PROVEN" and o["reason"].startswith("modular")
    assert "PRES:BR_POS@Alta" in o["conditional_on"]


def test_compose_not_proven_when_a_step_breaks_the_invariant():
    obs = _pres("n")
    assert obs["PRES:BR_POS@Alta"]["status"] == "REFUTED"
    assert obs["PRES:BR_POS@CreateRequest"]["status"] != "PROVEN"
