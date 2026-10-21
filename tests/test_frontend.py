import hashlib
import json
from pathlib import Path

import pytest

from upa.frontend import FrontendError, generate, infer_frontend
from upa.ir import canonical_json

FIXTURES = Path(__file__).parent / "fixtures" / "order-agenda"
IR_PATH = FIXTURES / "logical-ir.json"
PROFILE_PATH = FIXTURES / "profile.json"


def inputs():
    ir = json.loads(IR_PATH.read_text(encoding="utf-8"))
    profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    return ir, profile


def test_order_agenda_frontend_inference_is_deterministic_and_traceable():
    ir, profile = inputs()
    first = infer_frontend(ir, profile)
    second = infer_frontend(ir, profile)

    assert first == second
    assert "interactions" not in profile["pages"][0]
    assert first["provenance"]["profile_sha256"] == hashlib.sha256(
        canonical_json(profile).encode("utf-8")
    ).hexdigest()
    assert first["frontend_required"] is True
    assert [page["title"] for page in first["pages"]] == ["Orders"]
    assert {item["id"] for item in first["interactions"]} == {"RegisterOrder"}
    order = first["interactions"][0]
    assert order["resolved_parameters"]["id"]["kind"] == "server_generated"
    assert next(field for field in order["fields"] if field["role"] == "selection")["component_id"] == 2
    assert order["trace"]["operation"] == "RegisterOrder"
    assert {capability["name"] for capability in first["capabilities"]} >= {
        "human-interaction", "data-input", "command-execution", "validation-feedback",
        "result-presentation",
    }
    assert "components" not in first


def test_profile_must_bind_every_logical_parameter():
    ir, profile = inputs()
    register_order = profile["operations"][0]
    del register_order["resolved_parameters"]["id"]

    with pytest.raises(FrontendError, match="parameters without an input"):
        infer_frontend(ir, profile)


def test_profile_rejects_unverified_collection_fields():
    ir, profile = inputs()
    profile["collections"][0]["fields"][0]["attribute"] = "unknown"

    with pytest.raises(FrontendError, match="attribute"):
        infer_frontend(ir, profile)


def test_frontend_artifacts_are_byte_for_byte_deterministic(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    result_a = generate(IR_PATH, PROFILE_PATH, first)
    result_b = generate(IR_PATH, PROFILE_PATH, second)

    assert result_a["artifacts"] == ["interaction-model.json", "ui-ir.json"]
    assert (first / "interaction-model.json").read_bytes() == (second / "interaction-model.json").read_bytes()
    assert (first / "ui-ir.json").read_bytes() == (second / "ui-ir.json").read_bytes()


def test_no_human_interaction_does_not_emit_frontend_artifacts(tmp_path):
    ir = {"entities": {}, "operations": []}
    profile = {
        "format": "upa-ui-ir-1",
        "name": "non-interactive",
        "version": "1",
        "pages": [],
        "operations": [],
        "collections": [],
    }
    output = tmp_path / "no-ui"
    ir_path, profile_path = tmp_path / "logical-ir.json", tmp_path / "profile.json"
    ir_path.write_text(json.dumps(ir), encoding="utf-8")
    profile_path.write_text(json.dumps(profile), encoding="utf-8")

    assert infer_frontend(ir, profile)["frontend_required"] is False
    assert generate(ir_path, profile_path, output)["frontend_required"] is False
    assert list(output.iterdir()) == []
