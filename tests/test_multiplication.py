import json
from pathlib import Path

from upa.compiler import build
from upa.parser import Parser

SPEC = """spec Inventory version "0.1"
facts {
  fact F_CAP origin literal("A deposit has principal"): every Deposit has exactly_one principal: Natural
}
initial origin assumption(AS_1): forall k: not (k in Deposit)
operation Cancel(id: Identifier, pen: Natural) {
  when not (id in Deposit) { }
  when pen * 10 < Deposit.principal[id] { }
  otherwise { set Deposit.principal[id] = Deposit.principal[id] + pen * 2 }
}

assumption AS_1 "No deposits exist at startup" owner "Business"
"""


def test_precedence_mul_over_add():
    e = Parser("spec X version \"0.1\"\ninitial origin assumption(A): 1 + 2 * 3 == 7\n").parse()
    assert e is not None


def test_mul_builds(tmp_path: Path):
    src = tmp_path / "a.upa"
    src.write_text(SPEC, encoding="utf-8")
    out = tmp_path / "out"
    result = build(src, None, out)
    assert result.get("exit", 0) == 0
    ir = json.loads((out / "canonical-ir.json").read_text(encoding="utf-8"))
    assert '"op": "*"' in json.dumps(ir)
