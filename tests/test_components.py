"""Composition across UPAs (`component Alias = a.b.c@v`) and the substring primitive."""
import json
from pathlib import Path

from upa import compiler
from upa.verifier import verify
from upa.viewer import build_data

ATOM = '''spec TextUtilities version "1.0"
enum M { A, B }
query Pad4(s: Text) -> Text = if text_length(s) < 4 then concat(substring("0000", 0, 4 - text_length(s)), s) else s
query LastFour(s: Text) -> Text = if text_length(s) > 4 then substring(s, text_length(s) - 4, 4) else Pad4(s)
query IsA(m: M) -> Bool = m == M.A
'''

PARENT = '''spec UseCase version "1.0"
component T = org.textutils@1.0
entity R { v: Text }
decision DD_1 "Normalize using the component rule"
assumption AS_1 "No records exist at startup" owner "x"
initial origin assumption(AS_1): forall r: not (r in R)
operation Save(id: Identifier, s: Text) {
  when id in R { }
  otherwise { create R[id] { v = T.LastFour(s) } }
}
business_rules {
  invariant BR_FOUR origin derived(DD_1) "every stored value has four characters":
    forall r: (r in R) => text_length(R.v[r]) == 4
}
'''


def _rules(tmp_path: Path, atom: str = ATOM, parent: str = PARENT) -> Path:
    root = tmp_path / "rules"
    (root / "org" / "textutils").mkdir(parents=True)
    (root / "org" / "usecase").mkdir(parents=True)
    (root / "org" / "textutils" / "textutils.upa").write_text(atom, encoding="utf-8")
    p = root / "org" / "usecase" / "usecase.upa"
    p.write_text(parent, encoding="utf-8")
    return p


def _compile(p: Path):
    return compiler.compile_source(p.read_text(encoding="utf-8"), None, p.parents[2])


def codes(errors):
    return {e["error"] for e in errors.to_json()}


def test_substring_is_verified_with_smt_semantics():
    src = '''spec S version "1.0"
entity E { v: Text }
decision DD_1 "d"
assumption AS_1 "a" owner "o"
initial origin assumption(AS_1): forall e: not (e in E)
operation Put(id: Identifier, s: Text) {
  when id in E { }
  otherwise { create E[id] { v = substring(concat(s, "abcd"), text_length(s), 2) } }
}
business_rules {
  invariant BR_AB origin derived(DD_1) "always ab": forall e: (e in E) => E.v[e] == "ab"
}
'''
    _, ir, _, errors = compiler.compile_source(src, None)
    assert not errors.items
    assert verify(ir)["verdict"] == "VERIFIED"


def test_component_call_is_inlined_and_proven(tmp_path):
    spec, ir, _, errors = _compile(_rules(tmp_path))
    assert not errors.items, errors.to_json()
    comp = ir["composition"]
    assert comp["depth"] == 1 and comp["calls"] == ["T.LastFour"]
    child = comp["components"][0]
    assert child["ref"] == "org.textutils" and child["depth"] == 0
    assert {e["name"] for e in child["exports"]} == {"Pad4", "LastFour"}   # IsA uses an enum and is not exported.
    assert verify(ir)["verdict"] == "VERIFIED"


def test_async_endpoint_component_member_must_be_operation_or_compose(tmp_path):
    parent = '''spec UseCase version "1.0"
component T = org.textutils@1.0
operation Consume(id: Identifier) { when true outcome OK success { } }
publishes NonProducerQuery from T.Pad4 via topic "demo"
  message "Data" confirmation confirmed evidence "demo:1"
'''
    _, _, _, errors = _compile(_rules(tmp_path, parent=parent))
    assert "UNKNOWN_ASYNC_ENDPOINT" in codes(errors)


def test_broken_component_rule_refutes_parent(tmp_path):
    p = _rules(tmp_path, atom=ATOM.replace("substring(s, text_length(s) - 4, 4)", "substring(s, 0, 5)"))
    _, ir, _, errors = _compile(p)
    assert not errors.items
    vr = verify(ir)
    assert vr["verdict"] == "REFUTED"
    assert any(r["rule"] == "BR_FOUR" and r["status"] == "REFUTED" for r in vr["rules"])


def test_component_errors(tmp_path):
    p = _rules(tmp_path, parent=PARENT.replace("T.LastFour(s)", "T.LastFour(s, s)"))
    assert "QUERY_CALL_ARITY" in codes(_compile(p)[3])
    p.write_text(PARENT.replace("T.LastFour(s)", "T.IsA(s)").replace("v: Text", "v: Bool")
                 .replace("text_length(R.v[r]) == 4", "R.v[r]"), encoding="utf-8")
    assert "COMPONENT_QUERY_NOT_EXPORTABLE" in codes(_compile(p)[3])
    p.write_text(PARENT.replace("T.LastFour(s)", "X.LastFour(s)"), encoding="utf-8")
    assert "UNKNOWN_COMPONENT" in codes(_compile(p)[3])
    p.write_text(PARENT.replace("org.textutils@1.0", "org.textutils@2.0"), encoding="utf-8")
    assert "COMPONENT_VERSION" in codes(_compile(p)[3])
    p.write_text(PARENT.replace("org.textutils@1.0", "org.missing@1.0"), encoding="utf-8")
    assert "COMPONENT_NOT_FOUND" in codes(_compile(p)[3])


def test_component_cycle(tmp_path):
    p = _rules(tmp_path, atom='spec TextUtilities version "1.0"\ncomponent U = org.usecase@1.0\n' + ATOM.split("\n", 1)[1])
    assert "COMPONENT_INVALID" in codes(_compile(p)[3]) or "COMPONENT_CYCLE" in codes(_compile(p)[3])


def test_build_aggregates_component_verdicts_and_viewer_tree(tmp_path):
    p = _rules(tmp_path)
    res = compiler.build(p, None, p.parent)
    assert res["verdict"] == "VERIFIED"
    assert [c["verdict"] for c in res["components"]] == ["VERIFIED"]
    assert res["summary_total"]["PROVEN"] >= res["summary"]["PROVEN"]
    assert (p.parents[1] / "textutils" / "canonical-ir.json").exists()
    data = build_data(p.parent, p.read_text(encoding="utf-8"))
    assert data["level"] == 1 and data["composition"]["children"][0]["data"]["name"] == "TextUtilities"
    child = build_data(p.parents[1] / "textutils")
    assert child["used_by"] == [{"spec": "UseCase", "alias": "T", "version": "1.0", "path": "org/usecase/usecase.upa"}]
    assert "LastFour" in child["formal"]["Pad4"]["used_in"]


def test_realizes_qualified_component_element(tmp_path):
    smap = '''system_map {
  component C1 {
    name "n"
    kind library
    scope included
    role "r"
    evidence "e"
    realizes T.%s
  }
}
'''
    p = _rules(tmp_path, parent=PARENT + smap % "Pad4")
    assert not _compile(p)[3].items
    p.write_text(PARENT + smap % "NotFound", encoding="utf-8")
    assert "UNKNOWN_COMPONENT_REALIZES" in codes(_compile(p)[3])


def test_mod_primitive_is_verified_and_mutation_refutes():
    src = '''spec S version "1.0"
entity E { v: Int }
decision DD_1 "d"
assumption AS_1 "a" owner "o"
initial origin assumption(AS_1): forall e: not (e in E)
operation Put(id: Identifier, n: Int) {
  when id in E { }
  otherwise { create E[id] { v = if mod(n, 10) == 0 then 0 else 10 - mod(n, 10) } }
}
business_rules {
  invariant BR_DIGIT origin derived(DD_1) "always a digit": forall e: (e in E) => (E.v[e] >= 0 and E.v[e] <= 9)
}
'''
    _, ir, _, errors = compiler.compile_source(src, None)
    assert not errors.items, errors.to_json()
    assert verify(ir)["verdict"] == "VERIFIED"
    _, ir, _, errors = compiler.compile_source(src.replace("mod(n, 10) == 0 then 0", "mod(n, 10) == 0 then 10"), None)
    assert verify(ir)["verdict"] == "REFUTED"


GRAND = '''spec Macro version "1.0"
component U = org.usecase@1.0
compose SaveTwice(a: Identifier, b: Identifier, s: Text) = U.Save(a, s); U.Save(b, s)
'''


def _grand(tmp_path: Path, body: str = GRAND) -> Path:
    _rules(tmp_path)
    g = tmp_path / "rules" / "org" / "macro"
    g.mkdir(parents=True)
    (g / "macro.upa").write_text(body, encoding="utf-8")
    return g / "macro.upa"


def test_imported_rules_keep_child_origins_and_initial(tmp_path):
    # The grandparent declares neither DD_1 nor initial: BR_FOUR was validated in the child, whose initial clause is also imported.
    p = _grand(tmp_path)
    _, ir, _, errors = _compile(p)
    assert not errors.items, errors.to_json()
    vr = verify(ir)
    assert vr["verdict"] == "VERIFIED"
    assert any(r["rule"].endswith("BR_FOUR") and r["status"] == "PROVEN" for r in vr["rules"])


def test_realizes_multilevel_component_path(tmp_path):
    smap = '''system_map {
  component C1 {
    name "n"
    kind library
    scope included
    role "r"
    evidence "e"
    realizes U.T.%s
  }
}
'''
    p = _grand(tmp_path, GRAND + smap % "Pad4")
    assert not _compile(p)[3].items, _compile(p)[3].to_json()
    p.write_text(GRAND + smap % "NotFound", encoding="utf-8")
    assert "UNKNOWN_COMPONENT_REALIZES" in codes(_compile(p)[3])


GUARDED = '''spec UseCase version "1.0"
component T = org.textutils@1.0
entity R { v: Text }
decision DD_1 "Save only when requested"
assumption AS_1 "No records exist at startup" owner "x"
initial origin assumption(AS_1): forall r: not (r in R)
operation Save(id: Identifier, s: Text) {
  when id in R { }
  otherwise { create R[id] { v = T.LastFour(s) } }
}
compose MaybeSave(id: Identifier, s: Text, requested: Bool) = when requested and T.Pad4(s) != "" then Save(id, s)
business_rules {
  ensures BR_ONLY_IF_REQUESTED for MaybeSave origin derived(DD_1) "do not save without a request":
    (not old(id in R) and not requested) => not (id in R)
  ensures BR_SAVED for MaybeSave origin derived(DD_1) "save when requested":
    (not old(id in R) and requested) => id in R
}
'''


def test_guarded_compose_step(tmp_path):
    p = _rules(tmp_path, parent=GUARDED)
    _, ir, _, errors = _compile(p)
    assert not errors.items, errors.to_json()
    call = ir["composes"][0]["calls"][0]
    assert call["guard"]["r"]["l"]["via"][0]["q"] == "T.Pad4"
    assert verify(ir)["verdict"] == "VERIFIED"
    p.write_text(GUARDED.replace('when requested and T.Pad4(s) != "" then ', ""), encoding="utf-8")
    vr = verify(_compile(p)[1])
    assert [r["rule"] for r in vr["rules"] if r["status"] == "REFUTED"] == ["BR_ONLY_IF_REQUESTED"]
    p.write_text(GUARDED.replace("when requested and", "when (id in R) and"), encoding="utf-8")
    assert "COMPOSE_GUARD_STATE" in codes(_compile(p)[3])


def test_flattened_steps_keep_guard_and_trail(tmp_path):
    _rules(tmp_path, parent=GUARDED)
    g = tmp_path / "rules" / "org" / "macro"
    g.mkdir(parents=True)
    (g / "macro.upa").write_text('spec Macro version "1.0"\ncomponent U = org.usecase@1.0\n'
                                 'compose Todo(id: Identifier, s: Text, a: Bool, b: Bool) = when a then U.MaybeSave(id, s, b)\n',
                                 encoding="utf-8")
    _, ir, _, errors = _compile(g / "macro.upa")
    assert not errors.items, errors.to_json()
    call = ir["composes"][0]["calls"][0]
    assert call["op"] == "U_Save" and call["trail"] == ["U.MaybeSave"]
    assert call["guard"]["op"] == "and"
    assert verify(ir)["verdict"] == "VERIFIED"
