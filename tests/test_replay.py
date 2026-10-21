import json
from pathlib import Path
from upa.compiler import compile_source
from upa.replay import replay

SRC = """spec R version "1.0"
entity Order { route: Int }
operation AssignRoute(id: Identifier, zone: Text, priority: Bool) {
  when id in Order { }
  otherwise { create Order[id] { route = if substring(zone, 0, 3) == "NTH" then 3 else if priority then 1 else 2 } }
}
"""


def _ir():
    _, ir, _, errs = compile_source(SRC, None)
    assert not errs.items
    return ir


def test_replay_reproduces_infers_and_contradicts():
    rep = replay(_ir(), [
        {"op": "AssignRoute", "args": {"zone": "NTH0000"}, "expect": {"Order.route": 3}, "free": ["priority"], "n": 5},
        {"op": "AssignRoute", "args": {"zone": "SOU0000"}, "expect": {"Order.route": 1}, "free": ["priority"], "n": 4},
        {"op": "AssignRoute", "args": {"zone": "NTH0000"}, "expect": {"Order.route": 1}, "free": ["priority"], "n": 1},
    ])
    assert rep["case_count"] == 3
    assert rep["observation_count"] == 10
    assert rep["reproduction_pct"] == 90.0
    st = [r["status"] for r in rep["rows"]]
    assert st == ["REPRODUCES", "PARTIALLY_REPRODUCES", "CONTRADICTS"]
    assert rep["rows"][1]["explained_by"] == [{"priority": True}]
    assert len(rep["rows"][2]["observations"]) == 2
    assert rep["by_status"] == {"REPRODUCES": 5, "PARTIALLY_REPRODUCES": 4, "CONTRADICTS": 1}
