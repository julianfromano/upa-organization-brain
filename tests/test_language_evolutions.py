import json
import re

from upa import surface

BASE = """spec Mini version "1"

business {
    requests {
        "The identifier for each request is assigned by the system."
        "Every request has notes and office."
        "Requests can be registered."
        "The notes of a request can be modified."
        "The state of every request can be open or closed."
        "A new request starts in state open."
        "A request in state open can transition to state closed."
        "%s"
    }
    members {
        "Every member has document."
        "Members can be registered."
        "Each member belongs to exactly one registered request."
        "Members are numbered consecutively for each request, starting at 0."
    }
}

initial {
    "Initially, there are no requests or members."
}
"""
ONLY = "Only notes and the state of a registered request can be modified."


def build(tmp_path, only=ONLY):
    p = tmp_path / "m.upa"
    p.write_text(BASE % only, encoding="utf-8")
    r = surface.build_surface(p, tmp_path / "o", 120000)
    cp = tmp_path / "o" / "core" / "core.upa"
    if not cp.exists():
        return r, None, {}
    vr = json.loads((tmp_path / "o" / "verification-result.json").read_text(encoding="utf-8"))
    return r, cp.read_text(encoding="utf-8"), {o["rule"]: o["status"] for o in vr["obligations"]}


def rule_text(core, rid):
    m = re.search(rf"(?:ensures|law|invariant) {rid}\b.*?:\n    (.+)", core)
    assert m, rid
    return m.group(1)


def test_outcomes_and_success_are_guarantees_about_result_and_frame(tmp_path):
    r, core, st = build(tmp_path)
    assert r["verdict"] == "VERIFIED" and set(st.values()) == {"PROVEN"}
    assert "result is " in core and " and unchanged)" in core
    ok = rule_text(core, "BR_OK_RegisterMember")
    assert ok.startswith("result is success => (")
    assert "unchanged except Member[key]" in ok and "CounterRequest.seq_member[request] == old(" in ok
    assert "unchanged except Request.notes[identifier]" in rule_text(core, "BR_OK_ModifyNotesOfRequest")
    assert "StateRequest.Closed" in rule_text(core, "BR_OK_TransitionRequestToClosed")


def test_states_are_an_enum_not_free_text(tmp_path):
    _, core, st = build(tmp_path)
    assert "enum StateRequest { Open, Closed }" in core
    assert 'state[' in core and '== "' not in re.sub(r'"[^"\n]*"', "", core) and "StateRequest.Open" in core
    assert not re.search(r'state\[[^\]]+\] (?:==|!=) "', core)
    assert st["TYPE:Request.state"] == "PROVEN"


def test_technical_counter_lives_in_its_own_entity(tmp_path):
    _, core, st = build(tmp_path)
    assert "create CounterRequest[identifier] { seq_member = 0 }" in core
    assert not re.search(r"\bRequest\.seq_member", core)
    assert "TR_COUNTER_Request" in st and st["TR_COUNTER_Request"] == "PROVEN"
    assert "CounterRequest" in re.search(r"^initial origin .*", core, re.MULTILINE).group(0)


def test_generated_identifier_has_no_duplicate_outcome(tmp_path):
    _, core, st = build(tmp_path)
    assert "RegisterRequest(identifier: Identifier generated for Request" in core
    assert "RequestExists" not in core


def test_only_modifiable_list_preserves_the_other_attributes(tmp_path):
    _, core, st = build(tmp_path)
    law = rule_text(core, "BR_ONLYMOD_Request__preserves")
    assert "Request.office[v] == old(Request.office[v])" in law
    assert "notes" not in law and "state" not in law
    assert st["BR_ONLYMOD_Request__preserves"] == "PROVEN"


def test_only_modifiable_list_is_validated_against_capabilities(tmp_path):
    # does not include notes, which can be modified
    r, _, _ = build(tmp_path, "Only office and the state of a registered request can be modified.")
    assert r.get("verdict") != "VERIFIED"
    assert any(e["error"] == "CAPABILITY_RULE_CONFLICT" for e in r["errors"])
    # no state declaration, but transitions exist
    sub = tmp_path / "b"
    sub.mkdir()
    r, _, _ = build(sub, "Only notes of a registered request can be modified.")
    assert any(e["error"] == "CAPABILITY_RULE_CONFLICT" for e in r["errors"])
    # includes a property that no capability modifies
    sub = tmp_path / "c"
    sub.mkdir()
    r, _, _ = build(sub, "Only notes, office, and the state of a registered request can be modified.")
    assert any(e["error"] == "MISSING_CAPABILITY" for e in r["errors"])
