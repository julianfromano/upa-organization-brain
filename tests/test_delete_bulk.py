"""Bulk deletes, cascades, human decisions, and concurrency validation."""
import json

from upa import compiler

SPEC = """spec Cascade version "1.0"
entity Request { status: Text }
entity Member { request: Identifier, number: Natural }
initial origin literal("empty"): forall c: not (c in Request) and not (c in Member)

operation DeleteRequest(request_id: Identifier) {
  when request_id in Request outcome Deleted success {
    delete Request[request_id]
    for each member in Member where Member.request[member] == request_id delete Member[member]
  }
  when true outcome NotFound { }
}

operation Renumber(request_id: Identifier, offset: Natural) {
  when request_id in Request outcome Renumbered success {
    for each member in Member where Member.request[member] == request_id and Member.number[member] > offset set Member.number[member] = Member.number[member] - 1
  }
  when true outcome NotFound { }
}

human_decision HD_PUT "A PUT for a missing request returns a technical error: is that intentional or a defect?" owner "product owner" options (INTENT "intentional; document it", DEFECT "a defect; return a business outcome")

business_rules {
  invariant BR_REQUEST_STATUS origin literal("every request has a status") "requests have a status":
    forall request_id: (request_id in Request) => Request.status[request_id] != ""
  ensures BR_CASCADE for DeleteRequest origin literal("deleting a request also deletes its members") "deleting a request removes its members":
    old(request_id in Request) => (not (request_id in Request) and forall member: (member in Member) => not (Member.request[member] == request_id))
}
"""


def _build(tmp_path, text):
    path = tmp_path / "cascade.upa"
    path.write_text(text, encoding="utf-8")
    result = compiler.build(path, None, tmp_path, 60000)
    obligations = {item["id"]: item for item in result.get("_obligations", [])}
    return result, obligations


def test_cascade_is_proven(tmp_path):
    result, obligations = _build(tmp_path, SPEC)
    assert result["verdict"] == "VERIFIED", result
    assert obligations["ENS:BR_CASCADE@DeleteRequest"]["status"] == "PROVEN"


def test_wrong_cascade_is_refuted(tmp_path):
    bad = SPEC.replace("    for each member in Member where Member.request[member] == request_id delete Member[member]\n", "")
    result, obligations = _build(tmp_path, bad)
    assert result["verdict"] == "REFUTED"
    assert obligations["ENS:BR_CASCADE@DeleteRequest"]["status"] == "REFUTED"


def test_human_decision_in_ir(tmp_path):
    _build(tmp_path, SPEC)
    path = next(tmp_path.rglob("canonical-ir.json"))
    ir = json.loads(path.read_text(encoding="utf-8"))
    decision = ir["notes"]["human_decisions"][0]
    assert decision["owner"] == "product owner"
    assert [option["id"] for option in decision["options"]] == ["INTENT", "DEFECT"]


def test_persists_risk_concurrency_in_ir(tmp_path):
    source = """spec Metadata version "1"
entity Record { count: Natural, note: Text }
persists PERSIST_NOTE "Record.note" hint "inventory.Record.Note" normalized "trim+upper"
risk RISK_1 "data loss" on "Create" severity "medium"
concurrency CONCURRENCY_1 "simultaneous creation" on "Create" mode "unprotected"
parameter PARAMETER_1 "threshold" owner "operations"
operation Create(key: Identifier) { when key in Record outcome Exists { } otherwise { create Record[key] { count = 1, note = "" } } }
business_rules { invariant INV_COUNT origin literal("count is nonnegative") "count": forall record: (record in Record) => Record.count[record] >= 0 }
"""
    result, _ = _build(tmp_path, source)
    assert result["verdict"] == "VERIFIED", result
    ir = json.loads(next(tmp_path.glob("canonical-ir.json")).read_text(encoding="utf-8"))
    notes = ir["notes"]
    assert notes["persists"][0]["hint"] == "inventory.Record.Note"
    assert notes["persists"][0]["normalized"] == "trim+upper"
    assert notes["risks"][0]["severity"] == "medium"
    assert notes["concurrency"][0]["mode"] == "unprotected"
    assert notes["parameters"][0]["owner"] == "operations"


def test_normalizations_are_proven_and_idempotent(tmp_path):
    source = """spec Normalize version "1"
entity Record { external_ref: Text, name: Text }
operation Create(key: Identifier, external_ref: Text) {
  when key in Record outcome Exists { }
  otherwise { create Record[key] { external_ref = digits_only(external_ref), name = trim(external_ref) } }
}
business_rules {
  ensures BR_LENGTH for Create origin literal("the stored reference is no longer than the input") "length":
    old(not (key in Record)) => text_length(Record.external_ref[key]) <= text_length(external_ref)
  ensures BR_IDEMPOTENT for Create origin literal("normalizing again does not change the stored value") "idempotent":
    old(not (key in Record)) => digits_only(Record.external_ref[key]) == Record.external_ref[key]
  ensures BR_TRIM for Create origin literal("trim removes surrounding whitespace") "no surrounding whitespace":
    old(not (key in Record)) => trim(Record.name[key]) == Record.name[key]
}
"""
    result, obligations = _build(tmp_path, source)
    assert obligations["ENS:BR_LENGTH@Create"]["status"] == "PROVEN", result
    assert obligations["ENS:BR_IDEMPOTENT@Create"]["status"] == "PROVEN", result
    assert obligations["ENS:BR_TRIM@Create"]["status"] == "PROVEN", result


def test_concurrency_validation_and_gaps(tmp_path):
    source = """spec Concurrency version "1"
entity Assignment { owner_id: Identifier, reference: Text }
operation Create(key: Identifier, owner_id: Identifier, reference: Text) {
  when key in Assignment outcome Exists { }
  otherwise { create Assignment[key] { owner_id = owner_id, reference = reference } }
}
business_rules {
  invariant BR_UNIQUE origin literal("no two assignments for an owner share a reference") "unique reference":
    forall a: forall b: ((a in Assignment) and (b in Assignment) and Assignment.owner_id[a] == Assignment.owner_id[b] and Assignment.reference[a] == Assignment.reference[b]) => a == b
}
"""
    result, _ = _build(tmp_path, source)
    ir = json.loads((tmp_path / "canonical-ir.json").read_text(encoding="utf-8"))
    assert ir["notes"]["concurrency_gaps"] == [{"operation": "Create", "rules": ["BR_UNIQUE"]}]
    resolved = source.replace("business_rules", 'concurrency CONCURRENCY_A "owner-level serialization" on "Create" mode "serialized" by owner_id protects BR_UNIQUE on_conflict retry\nbusiness_rules', 1)
    second = tmp_path / "resolved"
    second.mkdir()
    result, _ = _build(second, resolved)
    ir = json.loads((second / "canonical-ir.json").read_text(encoding="utf-8"))
    assert ir["notes"]["concurrency_gaps"] == []
    assert ir["notes"]["concurrency"][0]["protects"] == ["BR_UNIQUE"]
    assert ir["notes"]["concurrency"][0]["by"] == "owner_id"
    invalid = resolved.replace("protects BR_UNIQUE", "protects BR_UNKNOWN")
    third = tmp_path / "invalid"
    third.mkdir()
    result, _ = _build(third, invalid)
    assert any(error["error"] == "UNKNOWN_RULE" for error in result["errors"])
