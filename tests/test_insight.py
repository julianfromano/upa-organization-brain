import json
from pathlib import Path

from upa import compiler, insight
from upa.viewer import build_data

SPEC = """spec D version "1"
entity Request { status: Identifier }
entity Member { request_id: Identifier, number: Natural }
persists P1 "Request.status"
persists P2 "Member.number"
initial origin literal("empty"): forall c: not (c in Request) and not (c in Member)
operation Delete(s: Identifier) {
  when s in Request outcome Delete success {
    delete Request[s]
    for each i in Member where Member.request_id[i] == s delete Member[i]
  }
  when true outcome NotFound { }
}
business_rules {
  invariant BR_REF origin literal("Each member belongs to a registered request (EV-INT-1).") "every member has a request":
    forall i: (i in Member) => Member.request_id[i] in Request
}
"""


def _build(tmp: Path, text: str, name: str):
    d = tmp / name
    d.mkdir()
    p = d / "c.upa"
    p.write_text(text, encoding="utf-8")
    res = compiler.build(p, None, d, 60000)
    return d, res


def test_origin_classification():
    assert insight.classify_origin({"kind": "literal", "ref": "Foo.bar: does something"}) == "location + interpretation"
    assert insight.classify_origin({"kind": "literal", "ref": "It is stated (EV-INT-1)."}) == "location + interpretation"
    assert insight.classify_origin({"kind": "literal", "ref": "A phrase without a location."}) == "modeler interpretation"
    assert insight.classify_origin({"kind": "derived", "ref": "DD_X"}) == "derived decision"
    assert insight.classify_origin({"kind": "assumption", "ref": "AS_X"}) == "modeler assumption"


def test_insight_impact_and_quality(tmp_path):
    d, res = _build(tmp_path, SPEC, "a")
    assert res["verdict"] == "VERIFIED"
    data = build_data(d)
    ins = data["insight"]
    assert ins["quality"]["verified_extracts"] == 0 and ins["quality"]["rules"] >= 1
    imp = ins["impact"]["BR_REF"]
    assert "Member.request_id" in imp["reads"]
    assert [w["op"] for w in imp["writers"]] == ["Delete"] and imp["writers"][0]["status"] == "PROVEN"
    assert imp["evidence"]["class"] == "location + interpretation"


def test_refutation_in_business_language(tmp_path):
    bad = SPEC.replace("    for each i in Member where Member.request_id[i] == s delete Member[i]\n", "")
    d, res = _build(tmp_path, bad, "b")
    assert res["verdict"] == "REFUTED"
    ref = insight.refutations(*(json.loads((d / f).read_text(encoding="utf-8")) for f in ("canonical-ir.json", "verification-result.json")), {})
    assert ref and ref[0]["operation"] == "Delete" and ref[0]["text"] == "every member has a request"
    assert ref[0]["params"] and ref[0]["cause"]


def test_semantic_diff(tmp_path):
    a, _ = _build(tmp_path, SPEC, "old")
    new = SPEC.replace("when true outcome NotFound { }", "when s in Member outcome EsMember { }\n  when true outcome NotFound { }")
    new = new.replace("  when s in Request outcome Delete success {", "  when s in Request and not (s in Member) outcome Delete success {")
    b, _ = _build(tmp_path, new, "new")
    d = insight.diff_trees(a, b)
    assert any(x["outcome"] == "EsMember" for x in d["outcomes"])
    assert any(g["outcome"] == "Delete" and g["change"].startswith("more restrictive") for g in d["guards"])
    assert "Summary" in insight.diff_markdown(d)
    assert insight.diff_trees(a, a)["summary"]["guards"] == 0


def test_parse_z3_array():
    arr = insight._parse_array("Store(Store(K(Int, False), 1, True), 2, True)")
    assert arr == {"*": "False", "1": "True", "2": "True"}