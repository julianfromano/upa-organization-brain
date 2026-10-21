import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from upa.compiler import build
from upa.__main__ import main
from upa.interaction import FORMAT, InteractionError, project
from upa.ir import IR_VERSION


def var(name, type_name="id"):
    return {"k": "var", "n": name, "ty": type_name}


def membership(entity, key, negative=False):
    expression = {"k": "in", "e": entity, "key": var(key), "ty": "bool"}
    return {"k": "un", "op": "not", "x": expression, "ty": "bool"} if negative else expression


def equal(left, right):
    return {"k": "bin", "l": left, "op": "==", "r": right, "ty": "bool"}


def text(value):
    return {"k": "str", "v": value, "ty": "text"}


def attribute(entity, name, key):
    return {"k": "attr", "e": entity, "a": name, "key": var(key), "ty": "text"}


def create(entity, key, inits, file=False):
    return {
        "kind": "create",
        "entity": entity,
        "key": var(key),
        "inits": [{"attr": name, "value": value} for name, value in inits],
        **({"file": True} if file else {}),
    }


def agenda_ir():
    create_customer = create("Customer", "customer_id", [("name", var("name", "text"))])
    create_product = create("Product", "code", [("description", var("description", "text"))])
    create_order = create("Order", "id", [
        ("customer", var("customer_id")),
        ("product", var("code")),
        ("productDescription", attribute("Product", "description", "code")),
        ("qty", var("qty", "nat")),
        ("number", {
            "k": "bin",
            "l": {"k": "attr", "e": "Customer", "a": "orders", "key": var("customer_id"), "ty": "int"},
            "op": "+",
            "r": {"k": "int", "v": 1, "ty": "int"},
            "ty": "int",
        }),
    ])
    content = {
        "k": "concat",
        "l": {"k": "to_text", "x": var("qty", "nat"), "ty": "text"},
        "r": text(""),
        "ty": "text",
    }
    return {
        "ir_version": IR_VERSION,
        "spec": "OrderAgenda",
        "version": "0.4",
        "entities": {},
        "operations": [
            {
                "name": "RegisterCustomer",
                "params": [
                    {"name": "customer_id", "type": "id"},
                    {"name": "name", "type": "text"},
                ],
                "cases": [
                    {"cond": membership("Customer", "customer_id"), "outcome": "CustomerExists", "stmts": []},
                    {"cond": equal(var("phone", "text"), text("")), "outcome": "PhoneRequired", "stmts": []},
                    {"cond": None, "stmts": [create_customer]},
                ],
            },
            {
                "name": "ChangePhone",
                "params": [
                    {"name": "customer_id", "type": "id"},
                    {"name": "phone", "type": "text"},
                ],
                "cases": [
                    {"cond": membership("Customer", "customer_id", True), "outcome": "CustomerNotFound", "stmts": []},
                    {"cond": None, "stmts": [{
                        "kind": "set", "entity": "Customer", "key": var("customer_id"),
                        "attr": "phone", "value": var("phone", "text"),
                    }]},
                ],
            },
            {
                "name": "RegisterProduct",
                "params": [
                    {"name": "code", "type": "id"},
                    {"name": "description", "type": "text"},
                ],
                "cases": [
                    {"cond": membership("Product", "code"), "outcome": "ProductExists", "stmts": []},
                    {"cond": None, "stmts": [create_product]},
                ],
            },
            {
                "name": "ChangeProduct",
                "params": [
                    {"name": "code", "type": "id"},
                    {"name": "description", "type": "text"},
                ],
                "cases": [
                    {"cond": membership("Product", "code", True), "outcome": "ProductNotFound", "stmts": []},
                    {"cond": None, "stmts": [{
                        "kind": "set", "entity": "Product", "key": var("code"),
                        "attr": "description", "value": var("description", "text"),
                    }]},
                ],
            },
            {
                "name": "RegisterOrder",
                "params": [
                    {"name": "id", "type": "id"},
                    {"name": "customer_id", "type": "id"},
                    {"name": "code", "type": "id"},
                    {"name": "qty", "type": "nat"},
                ],
                "cases": [
                    {"cond": membership("Order", "id"), "outcome": "OrderExists", "stmts": []},
                    {"cond": equal(var("qty", "nat"), {"k": "int", "v": 0, "ty": "int"}),
                     "outcome": "QuantityRequired", "stmts": []},
                    {"cond": membership("Customer", "customer_id", True), "outcome": "CustomerNotFound", "stmts": []},
                    {"cond": membership("Product", "code", True), "outcome": "ProductNotFound", "stmts": []},
                    {"cond": None, "stmts": [
                        create_order,
                        {
                            "kind": "set", "entity": "Customer", "key": var("customer_id"),
                            "attr": "orders", "value": var("customer_id"),
                        },
                        create("File", "id", [("content", content)], file=True),
                    ]},
                ],
            },
        ],
    }


def operation(interaction_ir, name):
    return next(item for item in interaction_ir["operations"] if item["id"] == name)


def test_agenda_projection_is_deterministic_and_provider_neutral():
    logical_ir = agenda_ir()
    first = project(logical_ir)
    second = project(logical_ir)

    assert first == second
    assert first["format"] == FORMAT
    assert len(first["operations"]) == 5
    serialized = json.dumps(first, ensure_ascii=False)
    for forbidden in ("React", "LegacySelect", "basic_page", "Page", "button", "component"):
        assert forbidden.casefold() not in serialized.casefold()


def test_register_order_classifies_references_derived_and_generated_values():
    result = project(agenda_ir())
    order = operation(result, "RegisterOrder")

    assert order["intent"]["kind"] == "CREATE"
    assert order["intent"]["subject"] == "Order"
    inputs = {item["name"]: item for item in order["inputs"]}
    assert inputs["customer_id"]["kind"] == "ENTITY_REFERENCE"
    assert inputs["customer_id"]["reference"] == {
        "entity": "Customer",
        "identity_attribute": "customer_id",
    }
    assert inputs["code"]["kind"] == "ENTITY_REFERENCE"
    assert inputs["code"]["reference"] == {
        "entity": "Product",
        "identity_attribute": "code",
    }
    assert inputs["qty"]["kind"] == "USER_INPUT"
    assert inputs["id"]["kind"] == "USER_INPUT"

    values = {item["name"]: item for item in order["system_values"]}
    assert values["productDescription"]["kind"] == "DERIVED"
    assert values["productDescription"]["derives_from"][0]["entity"] == "Product"
    assert values["productDescription"]["derives_from"][0]["attribute"] == "description"
    assert values["number"]["kind"] == "GENERATED"
    assert values["qtyText"]["kind"] == "DERIVED"
    assert values["qtyText"]["derives_from"] == [{"parameter": "qty"}]
    assert values["qtyText"]["inference_rule"] == "EXPLICIT_TO_TEXT_OF_OPERATION_PARAMETER"

    outcomes = {item["name"]: item["kind"] for item in order["outcomes"]}
    assert outcomes["OrderExists"] == "CONFLICT"
    assert outcomes["QuantityRequired"] == "VALIDATION"
    assert outcomes["CustomerNotFound"] == "VALIDATION"
    assert outcomes["ProductNotFound"] == "VALIDATION"
    assert sum(kind == "SUCCESS" for kind in outcomes.values()) == 1


def test_references_and_create_conflicts_are_distinguished_by_operation_effect():
    result = project(agenda_ir())
    customer_create = operation(result, "RegisterCustomer")
    phone_update = operation(result, "ChangePhone")

    assert next(item for item in customer_create["inputs"] if item["name"] == "customer_id")["kind"] == "USER_INPUT"
    assert next(item for item in phone_update["inputs"] if item["name"] == "customer_id")["kind"] == "ENTITY_REFERENCE"
    customer_exists = next(item for item in customer_create["outcomes"] if item["name"] == "CustomerExists")
    assert customer_exists["kind"] == "CONFLICT"


def test_missing_actor_is_explicit_and_provider_can_consume_projection_without_ui_runtime():
    interaction_ir = project(agenda_ir())
    order = operation(interaction_ir, "RegisterOrder")

    assert order["actor"]["kind"] == "UNRESOLVED"
    human = next(item for item in order["capabilities"] if item["name"] == "human-interaction")
    assert human["status"] == "UNRESOLVED"
    assert "does not explicitly identify" in human["reason"]

    def mock_provider(model):
        return [
            {
                "operation": item["id"],
                "intent": item["intent"]["kind"],
                "inputs": [parameter["name"] for parameter in item["inputs"]],
            }
            for item in model["operations"]
        ]

    rendered = mock_provider(interaction_ir)
    order_view = next(item for item in rendered if item["operation"] == "RegisterOrder")
    assert order_view == {
        "operation": "RegisterOrder",
        "intent": "CREATE",
        "inputs": ["id", "customer_id", "code", "qty"],
    }


def test_unreferenced_parameter_is_not_silently_promoted_to_user_input():
    logical_ir = agenda_ir()
    target = next(item for item in logical_ir["operations"] if item["name"] == "RegisterCustomer")
    target["params"].append({"name": "unused", "type": "text", "decl": "Text"})

    projected = operation(project(logical_ir), "RegisterCustomer")
    unresolved = next(item for item in projected["parameters"] if item["name"] == "unused")
    assert unresolved["kind"] == "UNRESOLVED"
    assert projected["ambiguities"][0]["parameter"] == "unused"


def test_file_payload_is_technical_and_does_not_hide_its_source_input():
    logical_ir = agenda_ir()
    target = next(item for item in logical_ir["operations"] if item["name"] == "RegisterOrder")
    target["params"].append({"name": "payload", "type": "text"})
    file_statement = target["cases"][-1]["stmts"][-1]
    file_statement["inits"][0]["value"] = var("payload", "text")

    projected = operation(project(logical_ir), "RegisterOrder")
    assert next(item for item in projected["inputs"] if item["name"] == "payload")["kind"] == "USER_INPUT"
    content = next(item for item in projected["system_values"] if item["name"] == "content")
    assert content["kind"] == "TECHNICAL"


def test_invalid_logical_ir_is_rejected_explicitly():
    logical_ir = agenda_ir()
    logical_ir["operations"].append(logical_ir["operations"][0])

    with pytest.raises(InteractionError, match="duplicate"):
        project(logical_ir)


def test_compiler_emits_interaction_ir_without_a_provider():
    source = """
spec ProjectionSmoke version "1.0"
entity Record { name: Text }
operation CreateRecord(id: Identifier, name: Text) {
  otherwise { create Record[id] { name = name } }
}
"""
    with TemporaryDirectory() as directory:
        root = Path(directory)
        source_path = root / "smoke.upa"
        output = root / "out"
        source_path.write_text(source, encoding="utf-8")

        result = build(source_path, None, output, timeout_ms=10000)

        assert result["verdict"] == "VERIFIED"
        artifact_path = output / "interaction-ir.json"
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        assert artifact["format"] == FORMAT
        assert artifact["operations"][0]["id"] == "CreateRecord"
        reprojection = root / "reprojected-interaction-ir.json"
        assert main([
            "project-interactions",
            str(output / "canonical-ir.json"),
            "--out",
            str(reprojection),
        ]) == 0
        assert json.loads(reprojection.read_text(encoding="utf-8")) == artifact
