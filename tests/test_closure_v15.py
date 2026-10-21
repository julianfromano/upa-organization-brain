"""Self-contained regression tests for semantic closure and provenance."""
from pathlib import Path

from upa import guide, surface

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "closure_records.upa"


def _case(tmp_path, edit=None):
    src = FIXTURE.read_text(encoding="utf-8")
    if edit:
        src = edit(src)
    path = tmp_path / "records.upa"
    path.write_text(src, encoding="utf-8")
    return path


def _codes(analysis):
    return [error.code for error in analysis.errors.items]


def test_neutral_record_fixture_reaches_ready_and_verified(tmp_path):
    path = _case(tmp_path)
    src = path.read_text(encoding="utf-8")

    assert guide.guide(src, path)["stage"] == "READY"
    analyzed, _ = surface.analyze(src, path)
    assert analyzed.closure["counts"]["AUTHOR_INPUT"] > 0
    assert analyzed.closure["counts"]["DERIVED"] > 0
    assert surface.build_surface(path, tmp_path / "out", 60000)["verdict"] == "VERIFIED"


def test_statement_without_citation_is_unsupported(tmp_path):
    path = _case(tmp_path, lambda src: src.replace(
        '"The code uniquely identifies the record." from '
        '"Records can be registered with code and name."',
        '"The code uniquely identifies the record."'))
    _, analysis = surface.analyze(path.read_text(encoding="utf-8"), path)
    assert "UNSUPPORTED_STATEMENT" in _codes(analysis)


def test_uncovered_input_remains_visible_without_blocking_ready(tmp_path):
    path = _case(tmp_path, lambda src: src.replace(
        "input {" + chr(10),
        "input {" + chr(10) + '    "Records are green."' + chr(10)))
    src = path.read_text(encoding="utf-8")
    analyzed, _ = surface.analyze(src, path)
    assert analyzed.closure["uncovered"]
    result = guide.guide(src, path)
    assert result["stage"] == "READY"
    assert any(q["type"] == "UNCOVERED_INPUT" and not q["blocking"] for q in result["questions"])


def test_derived_statements_have_closed_provenance(tmp_path):
    path = _case(tmp_path)
    analyzed, _ = surface.analyze(path.read_text(encoding="utf-8"), path)
    derived = [sentence for sentence in analyzed.sentences
               if sentence.prov in ("DERIVED", "TECHNICAL", "NORMALIZED")]
    ids = {sentence.id for sentence in analyzed.sentences}
    originals = ({item["from"] for item in analyzed.closure.get("normalized", [])}
                 | {item.get("from", item.get("id"))
                    for item in analyzed.closure.get("pending", [])})
    from upa.closure import AXIOMS, INPUT_FORMS
    known = ids | originals | set(AXIOMS) | {form[0] for form in INPUT_FORMS}
    assert derived
    assert all(sentence.basis for sentence in derived)
    assert all(basis in known for sentence in derived for basis in sentence.basis)


def test_closure_is_noop_on_complete_neutral_spec(tmp_path, monkeypatch):
    from upa import closure

    source = '''spec Records version "0.1"
business {
    records {
        "Every record has code and name."
        "The code uniquely identifies the record."
        "Records can be registered."
    }
}
outcomes for "Register record" {
    "If the code already belongs to a record, the result is RecordExists and no information changes."
    "Otherwise, the record is registered."
}
initial {
    "Initially, there are no records."
}
'''
    path = tmp_path / "complete_records.upa"
    path.write_text(source, encoding="utf-8")
    enabled = surface.build_surface(path, tmp_path / "enabled", 60000)
    monkeypatch.setattr(closure, "ENABLED", False)
    disabled = surface.build_surface(path, tmp_path / "disabled", 60000)
    assert enabled["verdict"] == disabled["verdict"] == "VERIFIED"
    core = lambda folder: (tmp_path / folder / "core" / "core.upa").read_text(encoding="utf-8")
    assert core("enabled") == core("disabled")


def test_corpus_rules_retain_a_trace_to_author_input(tmp_path):
    from upa import corpus

    result = corpus.build_corpus(_case(tmp_path), tmp_path / "corpus")
    assert result["verdict"] == "VERIFIED" and result["rules"]
    accepted_origins = {"ASSERTED", "NORMALIZED", "RESOLVED", "AUTHOR_INPUT_FORM", "TECHNICAL"}
    for rule in result["rules"]:
        assert rule["status"] == "PROVEN", rule["rule"]
        assert any(step["step"] in accepted_origins or step.get("inherited_from")
                   for step in rule["chain"]), rule["rule"]
    assert (tmp_path / "corpus" / "corpus.md").exists()


def test_numeric_paraphrases_preserve_direction():
    from upa.closure import cite_numbers, _number_misses

    assert cite_numbers("at least one unit")["min"] == {"1"}
    assert cite_numbers("up to ten records")["max"] == {"10"}
    assert cite_numbers("one record belongs to a profile")["any"] == set()
    assert _number_misses("The limit is 1 or greater.", "at least one unit") == []
    assert _number_misses("The limit is 1 or less.", "at least one unit") == ["1 as a maximum"]
    assert _number_misses("The limit is 1 or less.", "The limit is 1 or greater.") == ["1 as a maximum"]
    assert _number_misses("The limit is 2 or greater.", "at least one unit") == ["2"]


def test_counted_noun_and_state_anchors_do_not_invent_terms():
    from upa import closure

    assert closure._anchored("count of records", closure._words("up to thirty records"))
    assert not closure._anchored("count of records", closure._words("up to thirty profiles"))
    assert closure._state_anchored("archived", closure._words("a record can be archived"))
    assert not closure._state_anchored("archived", closure._words("a record can be registered"))


def test_input_and_answer_sections_are_recognized_as_surface():
    assert surface.is_surface('spec Records version "0.1"\ninput {\n "a record"\n}\n')
    assert surface.is_surface('spec Records version "0.1"\nanswers {\n "a record"\n}\n')


def test_acknowledgement_without_its_input_citation_is_unsupported(tmp_path):
    path = _case(tmp_path, lambda src: src.replace(
        "input {" + chr(10),
        "input {" + chr(10) + '    "Initially, no record exists."' + chr(10)).replace(
        "    }" + chr(10) + "}",
        '        "Initially, nothing is registered."' + chr(10) + "    }" + chr(10) + "}"))
    _, analysis = surface.analyze(path.read_text(encoding="utf-8"), path)
    assert "UNSUPPORTED_STATEMENT" in _codes(analysis)
