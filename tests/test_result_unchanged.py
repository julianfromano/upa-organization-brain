from upa.compiler import compile_source
from upa.verifier import verify

BASE = """spec T version "1"

assumption AS_I "empty" owner "test"

initial origin assumption(AS_I): forall k: not (k in A)

operation Reg(id: Identifier, name: Text) {
  when id in A outcome Dup { }
  when name == "" outcome NameRequired { }
  otherwise {
    create A[id] { name = name }
  }
}

operation Ren(id: Identifier, name: Text) {
  when not (id in A) outcome Missing { }
  otherwise {
    set A.name[id] = name
  }
}

business_rules {
%s
}
"""


def run(rules):
    spec, ir, _, err = compile_source(BASE % rules, None)
    assert not err, err.to_json()
    v = verify(ir)
    return {o["rule"]: o["status"] for o in v["obligations"]}


def ens(i, op, text, expr):
    return f'  ensures {i} for {op} origin assumption(AS_I) "{text}":\n    {expr}\n'


GOOD = (
    ens("E_REJ", "Reg", "rechazo", "old(id in A) => (result is Dup and unchanged)")
    + ens("E_OK", "Reg", "created", "result is success => (A.name[id] == name and unchanged except A[id])")
    + ens("E_REN", "Ren", "mod", "result is success => (A.name[id] == name and unchanged except A.name[id])")
    + '  law L_FIELDS origin assumption(AS_I) "name only":\n    unchanged except A, A.name\n'
)


def test_result_and_unchanged_are_proved_for_a_correct_contract():
    st = run(GOOD)
    assert st and set(st.values()) == {"PROVEN"}


def test_result_and_unchanged_refute_wrong_contracts():
    st = run(
        ens("E_BAD1", "Reg", "mal1", "result is success => unchanged")
        + ens("E_BAD2", "Ren", "mal2", "result is Missing => (id in A)")
        + ens("E_BAD3", "Reg", "mal3", "result is success => (A.name[id] == name and unchanged except A.name[id])")
        + ens("E_BAD4", "Ren", "mal4", "result is success => unchanged")
    )
    assert st["E_BAD1"] == "REFUTED"
    assert st["E_BAD2"] == "REFUTED"
    assert st["E_BAD3"] == "REFUTED"
    assert st["E_BAD4"] == "REFUTED"


def test_result_rejects_unknown_outcome_and_misuse():
    spec, ir, _, err = compile_source(BASE % ens("E_X", "Reg", "x", "result is Nope"), None)
    assert err and any(e["error"] == "RESULT_UNKNOWN" for e in err.to_json())
    spec, ir, _, err = compile_source(BASE % '  law L origin assumption(AS_I) "x":\n    result is success\n', None)
    assert err and any(e["error"] == "RESULT_OUTSIDE_ENSURES" for e in err.to_json())


def test_generated_identifier_is_fresh_and_removes_duplicate_branch():
    src = BASE.replace("Reg(id: Identifier, name", "Reg(id: Identifier generated for A, name") % ens(
        "E_FRESH", "Reg", "fresco", "result is success => not old(id in A)"
    )
    spec, ir, _, err = compile_source(src, None)
    assert not err, err.to_json()
    assert next(p for o in ir["operations"] if o["name"] == "Reg" for p in o["params"] if p["name"] == "id")["generated_for"] == "A"
    st = {o["rule"]: o["status"] for o in verify(ir)["obligations"]}
    assert st["E_FRESH"] == "PROVEN"
    plain = run(ens("E_FRESH", "Reg", "fresco", "result is success => not old(id in A)"))
    assert plain["E_FRESH"] == "PROVEN"  # The Dup case already excludes success.
    src2 = src.replace("when id in A outcome Dup { }\n  ", "")
    spec, ir, _, err = compile_source(src2, None)
    assert not err, err.to_json()
    st = {o["rule"]: o["status"] for o in verify(ir)["obligations"]}
    assert st["E_FRESH"] == "PROVEN"
    src3 = src2.replace(" generated for A", "")
    spec, ir, _, err = compile_source(src3, None)
    st = {o["rule"]: o["status"] for o in verify(ir)["obligations"]}
    assert st["E_FRESH"] == "REFUTED"
