import json
from pathlib import Path

import pytest

from upa.compiler import build

SPEC = """spec Agenda version "0.1"
facts {
  fact F_PHONES origin literal("Every customer has between 1 and 3 phone numbers"): every Customer has %s phones: Text
  fact F_NAME origin literal("Every customer has a name"): every Customer has exactly_one name: Text
}
initial origin assumption(AS_1): forall k: not (k in Customer)
operation RegisterCustomer(customer_id: Identifier, name: Text, phones: List<Text>) {
  when customer_id in Customer { }
%s  otherwise { create Customer[customer_id] { name = name, phones = phones } }
}

assumption AS_1 "Each customer has a unique customer ID" owner "Business"
"""
GUARDS = "  when count(phones) < 1 { }\n  when count(phones) > 3 { }\n"


def run(tmp_path: Path, card="[1..3]", guards=GUARDS):
    src = tmp_path / "a.upa"
    src.write_text(SPEC % (card, guards), encoding="utf-8")
    out = tmp_path / "out"
    return build(src, None, out), out


def test_collection_verified_and_projected(tmp_path):
    result, out = run(tmp_path)
    assert result.get("exit", 0) == 0
    ir = json.loads((out / "canonical-ir.json").read_text(encoding="utf-8"))
    assert ir["collections"][0]["min"] == 1 and ir["collections"][0]["max"] == 3
    persistence = json.loads((out / "persistence-ir.json").read_text(encoding="utf-8"))
    table = persistence["tables"][0]
    assert table["table"] == "PHONE" and table["multiplicity"] == "[1..3]"
    interaction = json.loads((out / "interaction-ir.json").read_text(encoding="utf-8"))
    params = interaction["operations"][0]["parameters"] if "operations" in interaction else []
    blob = json.dumps(interaction)
    assert "COLLECTION_INPUT" in blob and "collection_count" in blob
    assert params == [] or any(p["name"] == "phones_count" and p["kind"] == "DERIVED" for p in params)


def test_missing_upper_guard_is_refuted(tmp_path):
    result, out = run(tmp_path, guards="  when count(phones) < 1 { }\n")
    assert "REFUTED" in json.dumps(result) or "REFUTED" in "".join(
        p.read_text(encoding="utf-8") for p in out.glob("*.json"))


@pytest.mark.parametrize("card", ["[0]", "[3..1]"])
def test_invalid_multiplicity_rejected(tmp_path, card):
    with pytest.raises(Exception):
        result, _ = run(tmp_path, card=card)
        assert result.get("exit", 0) != 0
        raise Exception("rechazado")


def test_scalar_spec_has_no_persistence_projection(tmp_path):
    result, out = run(tmp_path, card="exactly_one", guards="")
    assert not (out / "persistence-ir.json").exists()
