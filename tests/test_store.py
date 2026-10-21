from upa.compiler import compile_source

SRC = """spec RequestStore version "1.0"
entity Request { status: Int, reference: Text }
operation CreateRequest(id: Identifier, reference: Text) {
  when id in Request { }
  otherwise { create Request[id] { status = 1, reference = reference } }
}
store Request in "InventoryDB..Requests" key "RequestId" { status = "Status", reference = "ExternalReference" } evidence "REQUEST_CREATE#insert"
"""


def test_store_in_ir_and_owners(tmp_path):
    import json
    from upa.boundary import owners
    _, ir, _, errs = compile_source(SRC, None)
    assert not errs.items
    assert ir["stores"] == [{"entity": "Request", "table": "InventoryDB..Requests", "key": "RequestId",
                             "columns": {"status": "Status", "reference": "ExternalReference"}, "evidence": "REQUEST_CREATE#insert"}]
    for d in ("a", "b"):
        (tmp_path / d).mkdir()
        ir2 = dict(ir, spec=d.upper())
        (tmp_path / d / "canonical-ir.json").write_text(json.dumps(ir2), encoding="utf-8")
    t = owners(tmp_path)["tables"][0]
    assert t["shared"] and t["owners"] == ["A@1.0", "B@1.0"]
    assert t["writers"][0]["columns"] == ["(create)", "ExternalReference", "Status"]


def test_store_unknown_attr():
    _, ir, _, errs = compile_source(SRC.replace('reference = "ExternalReference"', 'referencex = "X"'), None)
    assert ir is None and "STORE_UNKNOWN" in {e.code for e in errs.items}
