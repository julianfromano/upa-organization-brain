"""`outcome X success`: a successful no-op is not a rejection (it does not make an atomic composition empty)."""
from upa.compiler import compile_source
from upa.verifier import verify

SRC = """spec S version "1.0"
entity Request { status: Int }
operation Validate(ok: Bool) {
  when not ok outcome INVALID { }
  when true outcome VALID{SUCCESS} { }
}
operation Alta(id: Identifier) {
  when id in Request { }
  otherwise { create Request[id] { status = 1 } }
}
compose atomic CreateRequest(ok: Bool, id: Identifier) =
  Validate(ok);
  Alta(id)
business_rules {
  ensures BR_CREATE for CreateRequest origin literal("if valid and not already present, it is recorded") "a new valid request is recorded":
    (old(not (id in Request)) and ok) => id in Request
}
"""


def _vr(src):
    _, ir, _, errs = compile_source(src, None)
    assert not errs.items, errs.to_json()
    return ir, verify(ir, 20000, None)


def test_success_case_is_not_rejection():
    ir, vr = _vr(SRC.replace("{SUCCESS}", " success"))
    case = [c for o in ir["operations"] if o["name"] == "Validate" for c in o["cases"]][-1]
    assert case.get("success") is True
    assert {r["rule"]: r["status"] for r in vr["rules"]}["BR_CREATE"] == "PROVEN"


def test_empty_outcome_without_success_rejects_and_empties_atomic_compose():
    _, vr = _vr(SRC.replace("{SUCCESS}", ""))
    assert {r["rule"]: r["status"] for r in vr["rules"]}["BR_CREATE"] == "REFUTED"
