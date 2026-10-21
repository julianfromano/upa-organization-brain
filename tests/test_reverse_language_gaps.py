import json
from pathlib import Path
from upa import surface

SPEC = """spec N version "0.1"

business {
    courses {
        "Every course has code and name."
        "The code uniquely identifies the course."
        "Courses can be registered."
        "The name of a course is required."
    }
    enrollments {
        "Every enrollment has student."
        "Enrollments can be registered."
        "Each enrollment belongs to exactly one registered course."
        "There cannot be two enrollments for the same course with the same student."
        "Enrollments are numbered consecutively for each course, starting at 0."
    }
}

outcomes for "Register course" {
    "If the name is missing, the result is NameRequired and no information changes."
    "If the code already belongs to a course, the result is CourseExists and no information changes."
    "Otherwise, the course is registered."
}

outcomes for "Register enrollment" {
    "If the course is not registered, the result is CourseMissing and the enrollment is not registered."
    "If an enrollment for the same course already exists with the same student, the result is EnrollmentExists and no information changes."
    "Otherwise, the enrollment is registered."
}

gap GAP_X "An enrollment can be canceled." proposal "Conditional deletion" evidence "EV-1"
"""


def test_numbering_from_zero_owner_form_and_language_gap(tmp_path):
    p = tmp_path / "n.upa"
    p.write_text(SPEC, encoding="utf-8")
    r = surface.build_surface(p, tmp_path / "o", 120000)
    assert r["verdict"] == "VERIFIED"
    gaps = json.loads((tmp_path / "o" / "language-gaps.json").read_text(encoding="utf-8"))
    assert [(g["id"], g["status"], g["evidence"]) for g in gaps] == [("GAP_X", "LANGUAGE_GAP", "EV-1")]
    core = (tmp_path / "o" / "core" / "core.upa").read_text(encoding="utf-8")
    assert ">= 0" in core


def test_owner_form_matches_combo_patterns():
    from upa.surface import PATTERNS, Sentence, match_sentence
    pid = lambda t, sec: match_sentence(Sentence("S", t, sec, None, 0))[0]["id"]
    assert pid("There cannot be two members for the same request with the same tax_number.", "business") == "UNIQUE_COMBO"
    assert pid("If a member for the same request already exists with the same tax_number, the result is X and no information changes.", "outcomes") == "OUT_COMBO"

def test_core_language_gap_note_reaches_the_ir(tmp_path):
    from upa import parser, ir
    src = 'spec G version "0.1"\n\nlanguage_gap LG_1 "missing count"\n'
    spec = parser.parse(src) if hasattr(parser, "parse") else None
    assert spec is not None
    assert [(n.kind, n.id) for n in spec.notes] == [("language_gap", "LG_1")]