import json, random, re
from pathlib import Path
import pytest
from upa import compiler
from upa.verifier import verify

FX = Path(__file__).parent / "fixtures"
SCOPE_TEXT = "All values start at zero.\nThe value is never negative."
RECORD_SPEC = '''spec RecordCounter version "0.1"
type Count = Natural scale 2
initial origin literal("all values start at zero"): forall id: Record.count[id] == 0
operation Decrease(id: Identifier, amount: Count) {
  when id in Record and Record.count[id] >= amount {
    set Record.count[id] = Record.count[id] - amount
  }
}
business_rules {
  invariant BR_NONNEG origin literal("the value is never negative") "values remain nonnegative":
    forall id: Record.count[id] >= 0
}
'''


def run(name, tmp_path):
    source = {
        "record_ok.upa": RECORD_SPEC,
        "record_bad_guard.upa": RECORD_SPEC.replace(" and Record.count[id] >= amount", ""),
        "record_bad_quote.upa": RECORD_SPEC.replace(
            'origin literal("the value is never negative")',
            'origin literal("text outside the approved scope")',
        ),
        "record_unspecified.upa": RECORD_SPEC.replace(
            'origin literal("the value is never negative")',
            'origin unspecified("no source")',
        ),
    }[name]
    spec_path = tmp_path / name
    scope_path = tmp_path / "scope.md"
    spec_path.write_text(source, encoding="utf-8")
    scope_path.write_text(SCOPE_TEXT, encoding="utf-8")
    return compiler.build(spec_path, scope_path, tmp_path)


def _transfer_source_and_scope():
    source = (FX / "transfer_resolved.upa").read_text(encoding="utf-8")
    citations = re.findall(r'origin literal\("([^\"]+)"\)', source)
    return source, "\n".join(citations)


def test_ok_verified(tmp_path):
    r = run("record_ok.upa", tmp_path)
    assert r["verdict"] == "VERIFIED" and r["summary"]["REFUTED"] == 0 and r["summary"]["UNKNOWN"] == 0


def test_successful_build_clears_stale_errors(tmp_path):
    (tmp_path / "errors.json").write_text('[{"error":"STALE"}]', encoding="utf-8")
    r = run("record_ok.upa", tmp_path)
    assert r["verdict"] == "VERIFIED"
    assert not (tmp_path / "errors.json").exists()


def test_missing_guard_refuted_with_counterexample(tmp_path):
    r = run("record_bad_guard.upa", tmp_path)
    assert r["verdict"] == "REFUTED" and r["exit"] == 2
    err = r["errors"][0]
    assert err["error"] == "REFUTED_RULE" and err.get("counterexample")


def test_quote_not_in_scope(tmp_path):
    r = run("record_bad_quote.upa", tmp_path)
    assert r["exit"] == 1 and r["errors"][0]["error"] == "LITERAL_NOT_IN_SCOPE"


def test_unspecified_cannot_be_guarantee(tmp_path):
    r = run("record_unspecified.upa", tmp_path)
    assert r["exit"] == 1 and r["errors"][0]["error"] == "UNSPECIFIED_AS_GUARANTEE"


def test_unknown_is_not_success():
    spec, ir, report, errors = compiler.compile_source(RECORD_SPEC, SCOPE_TEXT)
    vr = verify(ir, timeout_ms=1)  # A timeout must never count as success without a proof.
    assert vr["verdict"] in ("VERIFIED", "INCOMPLETE")
    if vr["summary"]["UNKNOWN"]:
        assert vr["verdict"] == "INCOMPLETE"


def test_inference_no_unjustified_structure():
    spec, ir, report, errors = compiler.compile_source(RECORD_SPEC, SCOPE_TEXT)
    assert not errors
    assert report["metrics"]["unjustified_structure_count"] == 0
    assert "Record" in ir["entities"] and "Record" in json.dumps(report["inferred_model"])


def test_ambiguity_pending_then_resolved():
    source, scope = _transfer_source_and_scope()
    unresolved = source.replace('decision DD_6 "Use a single-entity model" resolves AMB_Order = MODEL_A\n', "")
    _, _, report, errors = compiler.compile_source(unresolved, scope)
    assert not errors.items and any(d["status"] == "pending_human" for d in report["design_decisions"])
    _, _, resolved_report, resolved_errors = compiler.compile_source(source, scope)
    assert not resolved_errors.items
    assert not any(d["status"] == "pending_human" for d in resolved_report["design_decisions"])


def test_why_traces_provenance():
    from upa.infer import why
    source, scope = _transfer_source_and_scope()
    _, _, report, errors = compiler.compile_source(source, scope)
    assert not errors.items
    explanation = why(report, "Warehouse.stock")
    assert "BR_CONSERVATION" in json.dumps(explanation)


def test_sum_axiom_soundness_sampling():
    """AX_SUM_STORE: sum(store(a,k,v)) = sum(a) - a[k] + v over finite maps."""
    rnd = random.Random(1)
    for _ in range(500):
        m = {rnd.randint(0, 9): rnd.randint(0, 99) for _ in range(rnd.randint(0, 8))}
        k, v = rnd.randint(0, 9), rnd.randint(0, 99)
        m2 = dict(m); m2[k] = v
        assert sum(m2.values()) == sum(m.values()) - m.get(k, 0) + v


def test_dotnet_conformance_trace_is_deterministic():
    from targets.dotnet import refmodel
    source, scope = _transfer_source_and_scope()
    _, ir, _, errors = compiler.compile_source(source, scope)
    assert not errors.items
    assert json.dumps(refmodel.traces(ir, 3, 5, 5)) == json.dumps(refmodel.traces(ir, 3, 5, 5))


def test_dotnet_conformance_trace_uses_valid_text_and_enum_arguments():
    from targets.dotnet import refmodel
    ir = {
        "entities": {},
        "enums": {"Kind": ["Text", "Button"]},
        "operations": [{
            "name": "Set",
            "params": [{"name": "value", "type": "text"}, {"name": "kind", "type": "enum:Kind"}],
            "cases": [],
        }],
        "composes": [],
        "texts": ["", "literal"],
    }
    generated = refmodel.traces(ir, seed=1, n_traces=5, steps=20)
    for trace in generated["traces"]:
        for step in trace["steps"]:
            text_id, kind = step["args"]
            assert 0 <= text_id < 2
            assert kind in (0, 1)


def test_reuse_resolve_and_catalog(tmp_path):
    from upa import reuse
    cand = {"name": "Record", "attrs": {"count": "nat"}, "rules": ["BR_NONNEG"]}
    assert reuse.resolve(cand, [])["decision"] == "NEW"
    blk = {"name": "Record", "version": 1, **cand}
    reuse.publish(tmp_path, blk)
    corpus = reuse.load_corpus(tmp_path)
    assert reuse.resolve(cand, corpus)["decision"] == "REUSE"
    ext = {**cand, "attrs": {"count": "nat", "limit": "nat"}}
    assert reuse.resolve(ext, corpus)["decision"] == "EXTEND"
    with pytest.raises(FileExistsError):
        reuse.publish(tmp_path, blk)

def _tree(tmp_path, child_ops):
    root = tmp_path / "rules"
    layer_dir = root / "layers" / "backend"
    module_dir = root / "catalog" / "module"
    layer_dir.mkdir(parents=True)
    module_dir.mkdir(parents=True)
    (layer_dir / "backend.upa").write_text(
        'spec Backend version "0.1" level layer\n', encoding="utf-8"
    )
    module = module_dir / "module.upa"
    module.write_text('''spec ComponentLibrary version "0.1" level module
uses layers.backend@0.1
enum Kind { Button, Label }
enum PageState { Draft, Published }
assumption AS_1 "Click counters only change through declared operations" owner "test"
operation Click(cid: Identifier) layer backend {
  when cid in Component and Component.kind[cid] == Kind.Button and Component.page[cid] in Page and Page.state[Component.page[cid]] == PageState.Published {
    set Component.clicks[cid] = Component.clicks[cid] + 1
  }
}
business_rules {
  law BR_CLICKS_MONOTONIC origin assumption(AS_1) "click counts never decrease":
    forall cid: old(cid in Component) => Component.clicks[cid] >= old(Component.clicks[cid])
}
''', encoding="utf-8")
    child = module_dir / "specific" / "child" / "child.upa"
    child.parent.mkdir(parents=True)
    child.write_text(
        'spec Child version "0.1" level specific\n'
        'uses catalog.module@0.1\n'
        'assumption AS_9 "This specific behavior is requested by the user" owner "user"\n'
        + child_ops,
        encoding="utf-8",
    )
    return child


def test_neutral_module_inherits_backend_layer(tmp_path):
    child = _tree(tmp_path, "")
    module = child.parents[2] / "module.upa"
    rules_root = child.parents[4]
    source = module.read_text(encoding="utf-8")
    _, ir, _, errors = compiler.compile_source(source, None, rules_root)
    assert not errors.items and ir["level"] == "module"
    assert {operation["name"]: operation["layer"] for operation in ir["operations"]}["Click"] == "backend"


def _mini(body, extra=""):
    return ('spec M version "0.1"\n' + extra + body)


def test_text_type_equality_ok_and_arithmetic_rejected():
    ok = _mini('''
operation Set(k: Identifier, v: Text) { when true { set E.name[k] = v } }
query IsEmpty(k: Identifier) -> Bool = E.name[k] == ""
''')
    _, ir, rep, err = compiler.compile_source(ok, None)
    assert not err and ir["texts"] == [""]
    assert "Text" in json.dumps(rep["inferred_model"])
    bad = _mini('query Q(k: Identifier) -> Bool = E.name[k] < "a"\noperation S(k: Identifier, v: Text) { when true { set E.name[k] = v } }')
    _, _, _, err2 = compiler.compile_source(bad, None)
    assert err2


def test_text_concat_is_typed_verified_and_generated():
    from targets.dotnet.generator import Gen
    from targets.dotnet.refmodel import Ref

    src = _mini('''
operation JoinName(k: Identifier, first: Text, last: Text) {
  when true { set Person.fullName[k] = concat(first, concat(" ", last)) }
}
business_rules {
  ensures BR_FULL_NAME for JoinName origin literal("full name") "the full name combines first and last names":
    Person.fullName[k] == concat(first, concat(" ", last))
}
''')
    _, ir, _, errors = compiler.compile_source(src, None)
    assert not errors.items
    assert ir["entities"]["Person"]["attrs"][0]["type"] == "text"
    assert verify(ir)["verdict"] == "VERIFIED"

    ref = Ref(ir)
    state = ref.apply(ref.empty(), "JoinName", [1, ref.intern_text("Ada"), ref.intern_text("Lovelace")])
    result_id = state["Person.fullName"][1]
    assert ref.text_value(result_id) == "Ada Lovelace"

    generated = Gen(ir).domain("test")
    generated += Gen(ir).service("test")
    assert "TextStore.Intern(TextStore.Get(p_first) + TextStore.Get(" in generated

    invalid = _mini('query Invalid() -> Text = concat("count=", 3)')
    _, _, _, invalid_errors = compiler.compile_source(invalid, None)
    assert any(e.code == "TYPE_MISMATCH" and "concat()" in e.message for e in invalid_errors.items)


def test_text_conversion_and_search_primitives():
    from targets.dotnet.generator import Gen
    from targets.dotnet.refmodel import Ref

    src = _mini('''
operation BuildRow(id: Identifier, qty: Natural, description: Text) {
  when true {
    set Row.value[id] = concat(to_text(id), concat(",", concat(to_text(qty), concat(",", description))))
  }
}
business_rules {
  ensures BR_ROW for BuildRow origin literal("row data") "a row stores its identifier, quantity, and description":
    Row.value[id] == concat(to_text(id), concat(",", concat(to_text(qty), concat(",", description))))
  invariant BR_TEXT_SEARCH origin literal("text search") "text predicates recognize prefixes, substrings, and suffixes":
    starts_with("Ada Lovelace", "Ada") and contains("Ada Lovelace", "Lovelace") and ends_with("Ada Lovelace", "Lovelace")
  invariant BR_TEXT_LENGTH origin literal("text length") "text length counts Unicode code points":
    text_length("A🙂") == 2
  invariant BR_REPLACE_FIRST origin literal("replace first") "replacement changes only the first occurrence":
    replace_first("a,b,c", ",", ";") == "a;b,c" and replace_first("abc", "", "x") == "abc" and replace_first("abc", "z", "x") == "abc"
}
''')
    _, ir, _, errors = compiler.compile_source(src, None)
    assert not errors.items
    assert verify(ir)["verdict"] == "VERIFIED"

    ref = Ref(ir)
    description = ref.intern_text("book")
    state = ref.apply(ref.empty(), "BuildRow", [-42, 3, description])
    row = ref.text_value(state["Row.value"][-42])
    assert row == "-42,3,book"
    assert ref.ev({"k": "text_length", "x": {"k": "str", "v": "A🙂"}}, state, {}) == 2
    assert ref.text_value(ref.ev({"k": "replace_first", "text": {"k": "str", "v": "a,b,c"},
                                  "search": {"k": "str", "v": ","}, "replacement": {"k": "str", "v": ";"}},
                                 state, {})) == "a;b,c"

    generated = Gen(ir).domain("test")
    generated += Gen(ir).service("test")
    assert "ToString(System.Globalization.CultureInfo.InvariantCulture)" in generated
    assert "StringComparison.Ordinal" in generated
    assert "EnumerateRunes().Count()" in generated

    invalid = _mini('query Invalid() -> Text = to_text(true)')
    _, _, _, invalid_errors = compiler.compile_source(invalid, None)
    assert any(e.code == "TYPE_MISMATCH" and "to_text()" in e.message for e in invalid_errors.items)


def test_naming_rule_and_reserved_words():
    dup = _mini('enum PageState { A, B }\nquery PageState(k: Identifier) -> Bool = true')
    _, _, _, err = compiler.compile_source(dup, None)
    assert any(e.code == "DUPLICATE_NAME" for e in err.items)
    res = _mini('enum Text { A, B }')
    _, _, _, err = compiler.compile_source(res, None)
    assert any(e.code == "RESERVED_NAME" for e in err.items)


def test_layer_level_required_for_layer_annotation(tmp_path):
    src = tmp_path / "rules/catalog/module/module.upa"
    src.parent.mkdir(parents=True)
    src.write_text('spec Catalog version "0.1" level module\n'
                   'operation Op(k: Identifier) layer backend { when true { create E[k] { x = 1 } } }\n', encoding="utf-8")
    r = compiler.build(src, None, tmp_path / "o")
    assert any(e["error"] == "LAYER_NOT_INHERITED" for e in r["errors"])



def test_write_file_primitive_verified_and_refuted(tmp_path):
    ok = FX / "export_ok.upa"
    r = compiler.build(ok, FX / "export_scope.md", tmp_path / "ok")
    assert r["verdict"] == "VERIFIED"
    ir = json.loads((tmp_path / "ok/canonical-ir.json").read_text(encoding="utf-8"))
    assert any(s.get("file") for o in ir["operations"] for c in o["cases"] for s in c["stmts"])
    assert "AX_FILE_WRITE" in (tmp_path / "ok/assumptions.json").read_text(encoding="utf-8")
    bad = tmp_path / "bad.upa"
    bad.write_text(ok.read_text(encoding="utf-8").replace("write_file id = body", 'write_file id = ""'), encoding="utf-8")
    r2 = compiler.build(bad, FX / "export_scope.md", tmp_path / "bad")
    assert r2["verdict"] == "REFUTED"


def test_write_file_generates_sink(tmp_path):
    from targets.dotnet import generator
    ir = json.loads(_build_ir(tmp_path))
    generator.generate(ir, tmp_path / "cs")
    assert "Files.Write(" in (tmp_path / "cs/Service.g.cs").read_text(encoding="utf-8")


def _build_ir(tmp_path):
    compiler.build(FX / "export_ok.upa", FX / "export_scope.md", tmp_path / "ir")
    return (tmp_path / "ir/canonical-ir.json").read_text(encoding="utf-8")


def test_external_contract_proves_for_all_outcomes(tmp_path):
    r = compiler.build(FX / "ext_inventory.upa", FX / "ext_inventory_scope.md", tmp_path / "e")
    assert r["verdict"] == "VERIFIED"
    asm = (tmp_path / "e/assumptions.json").read_text(encoding="utf-8")
    assert "AX_EXT_FulfillmentService_Reserve" in asm


def test_external_unknown_cannot_become_success(tmp_path):
    src = (FX / "ext_inventory.upa").read_text(encoding="utf-8")
    bad = tmp_path / "bad.upa"
    bad.write_text(src.replace("state = ReservationState.Uncertain", "state = ReservationState.Accepted"), encoding="utf-8")
    r = compiler.build(bad, FX / "ext_inventory_scope.md", tmp_path / "b", 10000)
    assert r["verdict"] in ("REFUTED", "INCOMPLETE")   # never VERIFIED (Z3 may return UNKNOWN for quantified formulas)


def test_external_uncertain_requires_resolution(tmp_path):
    src = (FX / "ext_inventory.upa").read_text(encoding="utf-8")
    spec, ir, report, errors = compiler.compile_source(src.replace("    resolve_by GetReservation\n", ""), None)
    assert any(e.code == "UNRESOLVABLE_EFFECT" for e in errors.items)


def test_openapi_import_is_deterministic():
    from upa.openapi_import import import_openapi, to_upa
    a = import_openapi(FX / "openapi/fulfillment.yaml", "InventoryReservations")
    assert a == import_openapi(FX / "openapi/fulfillment.yaml", "InventoryReservations")
    assert [o["name"] for o in a["operations"]] == ["Reserve", "GetReservation"]
    assert a["operations"][0]["outcomes"][-1] == {"name": "Unknown", "status": None, "uncertain": True,
                                                  "description": a["operations"][0]["outcomes"][-1]["description"]}
    assert "external InventoryReservations" in to_upa(a)
