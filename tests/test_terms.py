from upa.compiler import compile_source

SRC = '''spec T version "1.0"
entity Order { status: Int }
operation Reserve(id: Identifier, priority: Bool) {
  when id in Order { }
  otherwise { create Order[id] { status = 1 } }
}
terms {
  Reserve "Reserve the order" "The workflow reserves it in this batch"
  priority "indicates a priority payment"
  Order.status "order status"
}
'''


def test_terms_are_emitted_in_ir():
    _, ir, _, errs = compile_source(SRC, None)
    assert not errs.items
    assert ir["terms"]["Reserve"] == {"label": "Reserve the order", "text": "The workflow reserves it in this batch"}
    assert ir["terms"]["Order.status"]["label"] == "order status"


def test_unknown_term_is_an_error():
    _, ir, _, errs = compile_source(SRC.replace('priority \"indicates', 'priorityx \"indicates'), None)
    assert ir is None and "TERM_UNKNOWN" in {e.code for e in errs.items}


def test_terms_do_not_change_semantics():
    _, with_terms, _, _ = compile_source(SRC, None)
    _, without, _, _ = compile_source(SRC.split("terms {")[0], None)
    assert with_terms["operations"] == without["operations"]
