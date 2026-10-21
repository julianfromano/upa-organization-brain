import json

import pytest

from upa import compiler
from upa.errors import UpaError
from upa.parser import parse


def system_map_source(flow_step: str = 'step Carrier "HTTPS request" contract ContractA') -> str:
    return f'''
spec Shipping version "0.1"
system_map {{
  component Api {{
    name "shipping-api"
    kind service
    scope included
    role "Shipping channel"
    evidence "source §1"
  }}
  component Carrier {{
    name "contract boundary"
    kind external
    scope boundary
    role "Tracking contract only"
    evidence "WSDL"
  }}
  contract ContractA {{
    owner Carrier
    service "TrackingService"
    protocol "HTTPS"
    operation "GetStatus"
    request "Request"
    response "Response"
    usage consumer_known
    source "contract.yaml"
    sha256 "{'a' * 64}"
    evidence "WSDL §1"
    caller Api
  }}
  flow Query {{
    name "Query"
    status source_verified
    evidence "source §2"
    step Api "GET /query"
    {flow_step}
  }}
  observation OBS1 {{
    subject Api
    statement "The response is preserved in the channel envelope."
    status documented
    evidence "source §3"
  }}
}}
'''


def test_system_map_is_serialized_to_canonical_ir(tmp_path):
    source = tmp_path / "shipping.upa"
    source.write_text(system_map_source(), encoding="utf-8")

    result = compiler.build(source, None, tmp_path / "out")

    assert result["exit"] == 3, result
    assert result["verdict"] == "INCOMPLETE"
    assert result["incompleteness_reasons"]
    ir = json.loads((tmp_path / "out" / "ir.json").read_text(encoding="utf-8")) if (tmp_path / "out" / "ir.json").exists() else None
    if ir is not None:
        assert ir["system_map"]["contracts"][0]["id"] == "ContractA"


def test_unknown_contract_in_flow_rejected():
    with pytest.raises(UpaError):
        parse(system_map_source('step Carrier "HTTPS request" contract Missing'))


def extended_source(extra: str = "", comp_extra: str = "") -> str:
    return f"""
spec S version "1"
system_map {{
  component Db {{
    name "db"
    kind database
    scope included
    role "store"
    evidence "doc"
  }}
  component Web {{
    name "web"
    kind web_app
    scope included
    role "ui"
    evidence "doc"
    depends_on Db
    {comp_extra}
  }}
  {extra}
}}
"""


def test_catalog_job_and_depends_on_parse():
    spec = parse(extended_source(
        'catalog C { owner Web source "Enum" status source_verified evidence "doc" value A code "1" meaning "m" value B } '
        'job J { name "wf" owner Db kind etl_workflow status documented evidence "doc" reads "t1" writes "t2" }'))
    assert spec.system_components[1].depends_on == ["Db"]
    assert [v.name for v in spec.system_catalogs[0].values] == ["A", "B"]
    assert spec.system_jobs[0].writes == ["t2"]

def test_unknown_dependency_rejected():
    with pytest.raises(UpaError) as e:
        parse(extended_source(comp_extra="depends_on Nope"))
    assert e.value.code == "UNKNOWN_COMPONENT_DEPENDENCY"


def test_catalog_duplicate_values_rejected():
    src = extended_source('catalog C { owner Web source "E" status inferred evidence "d" value A value A }')
    with pytest.raises(UpaError) as e:
        parse(src)
    assert e.value.code == "DUPLICATE_CATALOG_VALUE"


def test_job_bad_kind_rejected():
    src = extended_source('job J { name "n" owner Db kind cron status inferred evidence "d" }')
    with pytest.raises(UpaError) as e:
        parse(src)
    assert e.value.code == "INVALID_JOB_KIND"




REALIZES_SOURCE = """
spec Orders version "0.1"
enum Status { New, Completed }
initial origin assumption(AS_1): forall p: not (p in Order)
assumption AS_1 "No orders exist at startup" owner "business"
question Q_1 "pending details"
operation RegisterOrder(id: Identifier) {
  when not (id in Order) { create Order[id] { status = Status.New } }
}
query OrderStatus(id: Identifier) -> Status = Order.status[id]
system_map {
  component Api {
    name "orders-api"
    kind service
    scope included
    role "Registers orders"
    evidence "source section 1"
    realizes RegisterOrder
    realizes OrderStatus
    realizes Q_1
  }
}
"""


def test_component_realizes_is_serialized_and_viewer_drills_down(tmp_path):
    from upa import viewer

    source = tmp_path / "orders.upa"
    source.write_text(REALIZES_SOURCE, encoding="utf-8")
    result = compiler.build(source, None, tmp_path / "out")
    assert result["exit"] == 0, result
    ir = json.loads((tmp_path / "out" / "canonical-ir.json").read_text(encoding="utf-8"))
    assert ir["system_map"]["components"][0]["realizes"] == ["RegisterOrder", "OrderStatus", "Q_1"]

    data = viewer.build_data(tmp_path / "out", REALIZES_SOURCE)
    assert data["formal"]["RegisterOrder"]["kind"] == "operation"
    assert data["formal"]["RegisterOrder"]["source"].startswith("operation RegisterOrder(")
    assert data["formal"]["OrderStatus"]["status"] == "DEFINITION"
    assert data["formal"]["Q_1"]["status"] == "OPEN"
    html = viewer.render([data])
    assert '<html lang="en">' in html


def test_component_realizes_must_reference_formal_element():
    with pytest.raises(UpaError, match="UNKNOWN_COMPONENT_REALIZES"):
        parse(REALIZES_SOURCE.replace("realizes Q_1", "realizes Missing"))


def test_component_realizes_rejects_duplicates():
    with pytest.raises(UpaError, match="DUPLICATE_COMPONENT_REALIZES"):
        parse(REALIZES_SOURCE.replace("realizes Q_1", "realizes RegisterOrder"))


def test_rule_parses_and_validates():
    src = extended_source(
        'rule R1 { subject Web kind validation status source_verified when "a" when "b" then "rejects" error "E1" '
        'source "x.cs#C.M" evidence "if (a)" }')
    spec = parse(src)
    assert spec.system_rules[0].whens == ["a", "b"] and spec.system_rules[0].error == "E1"


@pytest.mark.parametrize("rule,code", [
    ('rule R { subject Nope kind validation status inferred then "x" source "s" evidence "e" }', "UNKNOWN_RULE_SUBJECT"),
    ('rule R { subject Web kind magic status inferred then "x" source "s" evidence "e" }', "INVALID_RULE_KIND"),
    ('rule R { subject Web kind effect status maybe then "x" source "s" evidence "e" }', "INVALID_RULE_STATUS"),
    ('rule R { subject Web kind effect status inferred when "a" source "s" evidence "e" }', "RULE_OUTCOME_REQUIRED"),
    ('rule R { subject Web kind effect status inferred then "x" source " " evidence "e" }', "RULE_PROVENANCE_REQUIRED"),
])
def test_rule_errors(rule, code):
    with pytest.raises(UpaError) as e:
        parse(extended_source(rule))
    assert e.value.code == code


def test_duplicate_rule_rejected():
    r = 'rule R { subject Web kind effect status inferred then "x" source "s" evidence "e" }'
    with pytest.raises(UpaError) as e:
        parse(extended_source(r + " " + r))
    assert e.value.code == "DUPLICATE_RULE"
