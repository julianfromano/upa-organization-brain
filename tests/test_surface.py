import json
import pytest
from pathlib import Path

from upa import surface


ORDER_SPEC = '''spec OrderPlanning version "0.3"

business {
    customers {
        "Every customer has tax ID, first_name, last_name, and phone."
        "The tax ID uniquely identifies the customer."
        "The phone of a customer is required."
        "Customers can be registered."
        "The phone of a customer can be modified."
        "Only the phone of a registered customer can be modified."
        "Registered customers cannot be deleted."
    }
    products {
        "Every product has code and description."
        "The code uniquely identifies the product."
        "The description of a product is required."
        "Products can be registered."
        "The description of a product can be modified."
        "Only the description of a registered product can be modified."
        "Registered products cannot be deleted."
    }
    orders {
        "Orders can be registered."
        "Each order has a unique identifier supplied by the requester."
        "Each order belongs to exactly one registered customer."
        "Each order belongs to exactly one registered product."
        "The quantity of an order is 1 or greater."
        "Orders are numbered consecutively for each customer, starting at 1."
        "A registered order cannot be modified."
        "A registered order cannot be deleted."
        "Each order generates exactly one CSV file."
    }
}

outcomes for "Register customer" {
    "If the tax ID already belongs to a customer, the result is CustomerExists and no information changes."
    "If the phone is missing, the result is PhoneRequired and the customer is not registered."
    "Otherwise, the customer is registered."
}

outcomes for "Register product" {
    "If the code already belongs to a product, the result is ProductExists and no information changes."
    "If the description is missing, the result is DescriptionRequired and the product is not registered."
    "Otherwise, the product is registered."
}

outcomes for "Register order" {
    "If the identifier already belongs to an order, the result is OrderExists and no information changes."
    "If the quantity is less than 1, the result is QuantityTooSmall and the order is not registered."
    "If the customer is not registered, the result is CustomerMissing and the order is not registered."
    "If the product is not registered, the result is ProductMissing and the order is not registered."
    "Otherwise, the order is registered."
}

technical {
    "The content for the CSV file is provided when registering an order."
    "The order and its CSV file cannot end in inconsistent final states."
}

initial {
    "Initially, there are no customers, products, or orders."
}

use capability "Save file"

decision DD_CSV "The CSV file uses the same identifier as the order."
decision DD_ORDER "The order is modeled as a persistent entity." resolves AMBS_class_Order = ENTITY

glossary {
    "The code is specific to each concept."
}
'''


def _spec(tmp_path, text=ORDER_SPEC):
    path = tmp_path / "orders.upa"
    path.write_text(text, encoding="utf-8")
    return path


def run(tmp_path, text):
    path = tmp_path / "spec.upa"
    path.write_text(text, encoding="utf-8")
    out = tmp_path / "out"
    return surface.build_surface(path, out, 60000), out


def errors(result):
    return {item["error"] for item in result.get("errors", [])}


def test_is_surface():
    assert surface.is_surface(ORDER_SPEC)
    assert not surface.is_surface('spec X version "1"\nentity A {}\n')


def test_vague_sentence_is_ambiguous(tmp_path):
    source = ORDER_SPEC.replace(
        '"Orders are numbered consecutively for each customer, starting at 1."',
        '"Orders must be processed correctly."')
    result, _ = run(tmp_path, source)
    assert "AMBIGUOUS_RULE" in errors(result)


def test_unsupported_pattern(tmp_path):
    source = ORDER_SPEC.replace(
        '"A registered order cannot be deleted."', '"Orders are archived quarterly."')
    result, _ = run(tmp_path, source)
    assert "UNSUPPORTED_PATTERN" in errors(result)


@pytest.mark.usefixtures("no_closure")
def test_missing_outcome_blocks_build(tmp_path):
    source = '\n'.join(line for line in ORDER_SPEC.splitlines() if 'the quantity is less than 1' not in line)
    result, _ = run(tmp_path, source)
    assert "INCOMPLETE_CAPABILITY" in errors(result)
    assert result.get("exit") != 0


def test_order_spec_builds_end_to_end(tmp_path):
    path = _spec(tmp_path)
    out = tmp_path / "build"
    result = surface.build_surface(path, out, 120000)
    assert result["verdict"] == "VERIFIED" and result["exit"] == 0
    metrics = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["business_surface"]["procedural_construct_count"] == 0
    assert metrics["inference"]["unjustified_structure_count"] == 0
    assert metrics["semantic_quality"]["implicit_outcome_count"] == 0
    assert metrics["semantic_quality"]["semantic_weakening_count"] == 0
    for name in (
        "normalized-surface.json", "matched-patterns.json", "provenance.json", "canonical-ir.json",
        "interaction-ir.json", "verification-result.json", "semantic-weakening-report.json",
        "generated-outcomes.json",
    ):
        assert (out / name).exists(), name
    core = (out / "core" / "core.upa").read_text(encoding="utf-8")
    assert "Order.number[v] == old(Order.number[v])" in core


def test_evolution_extends_immutability(tmp_path):
    source = ORDER_SPEC.replace(
        '"The quantity of an order is 1 or greater."',
        '"The quantity of an order is 1 or greater."\n        "Every order has notes."')
    result, out = run(tmp_path, source)
    core_path = out / "core" / "core.upa"
    core = core_path.read_text(encoding="utf-8") if core_path.exists() else ""
    assert "Order.notes[v] == old(Order.notes[v])" in core
    assert (out / "interaction-ir.json").exists()
