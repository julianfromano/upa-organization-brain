import re
from pathlib import Path

import pytest
from upa import guide, surface

ROOT = Path(__file__).resolve().parents[1]

BASE_SPEC = '''spec Records version "0.1"
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


def write_spec(path, source):
    path.write_text(source, encoding="utf-8")
    return path


def test_v05_guide_is_ready_for_a_complete_neutral_spec():
    result = guide.guide(BASE_SPEC, ROOT / "records.upa")
    assert result["ready_to_build"] and result["stage"] == "READY"
    assert result["next_question"]["type"] in ("NEW_TERM", "OPEN_QUESTION")


def test_v05_duplicate_outcome_is_optional():
    source = BASE_SPEC.replace(
        '    "If the code already belongs to a record, the result is RecordExists and no information changes."\n', ""
    )
    result = guide.guide(source, ROOT / "records.upa")
    assert result["ready_to_build"] and result["stage"] == "READY"


def test_v05_initial_state_is_optional_for_a_stateless_spec():
    source = BASE_SPEC[:BASE_SPEC.index("initial {")]
    result = guide.guide(source, ROOT / "records.upa")
    assert result["ready_to_build"] and result["stage"] == "READY"


def test_v05_unrecognized_glossary_sentence_is_reported(tmp_path):
    source = BASE_SPEC + '\nglossary {\n    "Records and entries are similar."\n}\n'
    path = write_spec(tmp_path / "records.upa", source)
    result = surface.build_surface(path, tmp_path / "out", 60000)
    assert "GLOSSARY_UNSUPPORTED" in {error["error"] for error in result.get("errors", [])}


def test_v05_numbering_sentence_supplies_identity(tmp_path):
    source = '''spec NumberedFolios version "0.1"
business {
    folios {
        "Every folio has number and score."
        "The number uniquely identifies the folio."
        "Folios can be registered."
        "Each folio belongs to exactly one registered portfolio."
        "Folios are numbered consecutively for each portfolio, starting at 1."
    }
}
initial {
    "Initially, there are no folios."
}
'''
    path = write_spec(tmp_path / "evaluations.upa", source)
    result = guide.guide(source, path)
    assert not any("how it is identified" in question["question"] for question in result["questions"])


def test_v05_surface_catalog_examples_match_their_patterns():
    from upa.terminology import GL_DOC, GL_PATTERNS

    fill = {
        "<concept>": "record", "<concepts>": "records", "<properties>": "code and name",
        "<property>": "name", "<identifier>": "code", "<role>": "operator",
        "<A>": "record", "<B>": "item", "<n>": "1", "<owner>": "record",
        "<type>": "text", "<Name>": "Sample", "<other concept>": "item", "<C>": "closed",
        "<file>": "text", "<X>": "closed", "<Y>": "open", "<term>": "member",
        "<concept A>": "profile", "<concept B>": "record", "<definition>": "a named record",
    }

    def fillin(text):
        text = re.sub(r"\[([^\]]*)\]", r"\1", text)
        text = re.sub(r"(\w+)\|\w+", r"\1", text)
        for key, value in fill.items():
            text = text.replace(key, value)
        return text.rstrip(".")

    for pattern in surface.PATTERNS:
        main, _, alternate = pattern["doc"].partition("  (or: ")
        assert pattern["rx"].match(fillin(main)), pattern["id"]
        alternate = alternate.rstrip(")").rstrip(".")
        if alternate and not alternate.endswith("…"):
            assert any(regex.match(fillin(alternate)) for regex in pattern["alts"]), pattern["id"]

    glossary_patterns = dict(GL_PATTERNS)
    for key, sentence in GL_DOC.items():
        assert glossary_patterns[key].match(fillin(sentence)), key


@pytest.mark.usefixtures("no_closure")
def test_v14_missing_capability_and_specific_level_header(tmp_path):
    source = '''spec Records version "0.1" level specific
business {
    records {
        "Every profile has code and description."
        "The code uniquely identifies the profile."
        "Only the description of a registered profile can be modified."
    }
}
initial {
    "Initially, there are no profiles."
}
'''
    path = write_spec(tmp_path / "profiles.upa", source)
    result = guide.guide(source, path)
    missing = {question["id"] for question in result["questions"] if question["type"] == "MISSING_CAPABILITY"}
    assert missing == {"MCAP_profile", "MMOD_profile"}
    writes = [option["writes"] for question in result["questions"] if question["type"] == "MISSING_CAPABILITY" for option in question["options"]]
    assert "Profiles can be registered." in writes
    assert "The description of a profile can be modified." in writes
    build = surface.build_surface(path, tmp_path / "out", 60000)
    assert build.get("verdict") != "VERIFIED"


def test_v14_unmatched_outcome_is_not_dropped_silently(tmp_path):
    source = BASE_SPEC.replace(
        '    "Otherwise, the record is registered."',
        '    "If the name is missing, the result is LabelMissing and no information changes."\n'
        '    "Otherwise, the record is registered."',
    )
    path = write_spec(tmp_path / "records.upa", source)
    result = guide.guide(source, path)
    assert any(question["type"] == "UNMATCHED_OUTCOME" for question in result["questions"])
    assert not result["ready_to_build"]
