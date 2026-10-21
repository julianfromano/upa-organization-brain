import json

import pytest

from upa import parser, viewer
from upa.compiler import build, compile_source
from upa.errors import UpaError
from upa.interaction import project


def typed_source(required="true"):
    return f'''spec OrderDemo version "0.1"
operation ProcessOrder(id: Identifier) {{
  when true outcome OK success {{ }}
}}
language_gap GAP_LANG kind language "A required language feature is missing"
  target ProcessOrder impact medium required true
language_gap GAP_EV "Execution evidence is missing"
  kind evidence scope "operation ProcessOrder" impact high needed true
language_gap GAP_REGEN kind regeneration "The adapter has not been generated"
  target ProcessOrder impact critical required {required}
'''


def test_legacy_language_gap_remains_compatible():
    spec = parser.parse('spec Legacy version "0.1"\nlanguage_gap LG_1 "counting is not supported"\n')
    gap = spec.notes[0]
    assert gap.meta == {
        "kind": "language", "impact": "medium", "required": True, "legacy": True,
    }


def test_typed_gap_only_requires_id_kind_and_text():
    _, ir, _, errors = compile_source(
        'spec Minimal version "0.1"\n'
        'language_gap GAP_MIN kind regeneration "No surface has been evaluated yet"\n',
        None,
    )
    assert not errors
    assert ir["notes"]["language_gaps"][0]["kind"] == "regeneration"
    assert ir["regeneration_coverage"]["state"] == "not_evaluated"


def test_typed_gaps_reach_canonical_and_interaction_ir():
    spec, ir, _, errors = compile_source(typed_source(), None)
    assert not errors
    gaps = ir["notes"]["language_gaps"]
    assert [g["kind"] for g in gaps] == ["language", "evidence", "regeneration"]
    assert gaps[0]["target"] == "ProcessOrder"
    assert gaps[1]["scope"] == "operation ProcessOrder"
    assert gaps[2]["impact"] == "critical" and gaps[2]["required"] is True
    assert ir["regeneration_coverage"] == {
        "state": "partial",
        "declared_surfaces": ["ProcessOrder"],
        "open_required": ["GAP_REGEN"],
        "basis": "declared_surfaces_only",
        "percentage": None,
        "verification_result": "SEPARATE_ARTIFACT",
    }
    interaction = project(ir)
    assert interaction["language_gaps"] == gaps
    assert interaction["regeneration_coverage"]["state"] == "partial"


def test_regeneration_coverage_can_be_complete_without_percentages():
    _, ir, _, errors = compile_source(typed_source("false"), None)
    assert not errors
    assert ir["regeneration_coverage"]["state"] == "complete"
    assert ir["regeneration_coverage"]["percentage"] is None


@pytest.mark.parametrize(
    ("line", "code"),
    [
        ('language_gap G kind other "x" scope "s" impact high required true', "INVALID_GAP_KIND"),
        ('language_gap G kind language "x" scope "s" impact enormous required true', "INVALID_GAP_IMPACT"),
        ('language_gap G kind language "x" scope "s" impact high required maybe', "INVALID_GAP_REQUIRED"),
        ('language_gap G kind language "x" target Ausente impact high required true', "UNKNOWN_GAP_TARGET"),
    ],
)
def test_invalid_gap_metadata_is_rejected(line, code):
    source = f'spec Invalid version "0.1"\n{line}\n'
    if code == "INVALID_GAP_REQUIRED":
        with pytest.raises(UpaError) as error:
            parser.parse(source)
        assert error.value.code == code
    else:
        _, _, _, errors = compile_source(source, None)
        assert code in {item["error"] for item in errors.to_json()}


def test_viewer_lists_typed_gaps_and_separates_verified_from_regenerable(tmp_path):
    source = tmp_path / "demo.upa"
    out = tmp_path / "out"
    source.write_text(typed_source(), encoding="utf-8")
    result = build(source, None, out, timeout_ms=10000)
    assert result["verdict"] == "VERIFIED"
    data = viewer.build_data(out)
    assert data["regeneration_coverage"]["state"] == "partial"
    assert data["regeneration_coverage"]["verification_result"] == "VERIFIED"
    html = viewer.build_viewer([out], tmp_path / "viewer.html").read_text(encoding="utf-8")
    assert "Regeneration coverage" in html
    assert "Gaps by class, severity, and scope" in html
    assert "regeneration" in html.lower()
    assert '"percentage": null' in html


def test_inline_order_example_builds_and_exposes_gap_coverage(tmp_path):
    demo = tmp_path / "order-demo.upa"
    demo.write_text(typed_source(), encoding="utf-8")
    result = build(demo, None, tmp_path / "build", timeout_ms=30000)
    assert result["verdict"] == "VERIFIED"
    ir = json.loads((tmp_path / "build" / "canonical-ir.json").read_text(encoding="utf-8"))
    assert ir["regeneration_coverage"]["state"] == "partial"
