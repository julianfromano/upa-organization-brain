import json
from pathlib import Path

from upa import guide, surface

ROOT = Path(__file__).resolve().parents[1]

MEMBER_SPEC = '''spec MemberRecords version "0.1"
business {
    members {
        "Every member has code and name."
        "The code uniquely identifies the member."
        "Members can be registered."
    }
    profiles {
        "Every profile has code and name."
        "The code uniquely identifies the profile."
        "Profiles can be registered."
    }
}
outcomes for "Register member" {
    "If the code already belongs to a member, the result is MemberExists and no information changes."
    "Otherwise, the member is registered."
}
outcomes for "Register profile" {
    "If the code already belongs to a profile, the result is ProfileExists and no information changes."
    "Otherwise, the profile is registered."
}
initial {
    "Initially, there are no members or profiles."
}
'''

MEMBER_ONLY_SPEC = '''spec Members version "0.1"
business {
    members {
        "Every member has code and name."
        "The code uniquely identifies the member."
        "Members can be registered."
    }
}
initial {
    "Initially, there are no members."
}
'''

COMPLETE_MEMBER_SPEC = MEMBER_ONLY_SPEC.replace(
    'initial {',
    'outcomes for "Register member" {\n'
    '    "If the code already belongs to a member, the result is MemberExists and no information changes."\n'
    '    "Otherwise, the member is registered."\n'
    '}\ninitial {'
)


def build(path, output):
    return surface.build_surface(path, output, 60000)


def codes(result):
    return {error["error"] for error in result.get("errors", [])}


def write_spec(path, source):
    path.write_text(source, encoding="utf-8")
    return path


def tree(tmp_path):
    rules = tmp_path / "rules"
    (rules / "catalog" / "domain" / "module" / "specific" / "records").mkdir(parents=True)
    (rules / "glossary.upa").write_text(
        'spec CommonTerms version "0.1"\nglossary { "The code is specific to each concept." }\n',
        encoding="utf-8",
    )
    (rules / "catalog" / "domain" / "glossary.upa").write_text(
        'spec DomainTerms version "0.1"\nglossary { "Member can refer to a profile or a record." }\n',
        encoding="utf-8",
    )
    (rules / "catalog" / "glossary.upa").write_text(
        'spec CatalogTerms version "0.1"\nglossary { '
        '"Every profile has code and name." '
        '"The code uniquely identifies the profile." }\n',
        encoding="utf-8",
    )
    return rules / "catalog" / "domain" / "module" / "specific" / "records" / "records.upa"


def test_baseline_without_glossary_flags_structural_synonym(tmp_path):
    path = write_spec(tmp_path / "members.upa", MEMBER_SPEC)
    assert "AMBIGUOUS_TERM" in codes(build(path, tmp_path / "out"))



def test_declared_synonym_merges_concepts(tmp_path):
    source = MEMBER_SPEC.replace("ProfileExists", "MemberExists") + '\nglossary { "Member is another name for profile." }\n'
    path = write_spec(tmp_path / "members.upa", source)
    result = build(path, tmp_path / "out")
    assert result["exit"] == 0, result
    metrics = json.loads((tmp_path / "out" / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["inference"]["inferred_entities"] == 1


def test_merged_synonyms_with_conflicting_outcomes_are_flagged(tmp_path):
    source = '''spec MemberRecords version "0.1"
business {
    members {
        "Every member has code and name."
        "The code uniquely identifies the member."
        "Members can be registered."
    }
    profiles {
        "Every profile has code and name."
        "The code uniquely identifies the profile."
        "Profiles can be registered."
    }
}
outcomes for "Register member" {
    "If the code already belongs to a member, the result is MemberExists and no information changes."
    "Otherwise, the member is registered."
}
outcomes for "Register profile" {
    "If the code already belongs to a profile, the result is ProfileExists and no information changes."
    "Otherwise, the profile is registered."
}
initial {
    "Initially, there are no members or profiles."
}
glossary {
    "Member is another name for profile."
}
'''
    path = write_spec(tmp_path / "members.upa", source)
    result = build(path, tmp_path / "out")
    errors = [error for error in result.get("errors", []) if error["error"] == "UNMATCHED_OUTCOME"]
    assert result["exit"] != 0 and errors and errors[0].get("twin")


def test_declared_distinct_terms_keep_two_concepts(tmp_path):
    source = MEMBER_SPEC + '\nglossary { "Member is distinct from profile." }\n'
    path = write_spec(tmp_path / "members.upa", source)
    result = build(path, tmp_path / "out")
    assert result["exit"] == 0
    metrics = json.loads((tmp_path / "out" / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["inference"]["inferred_entities"] == 2


def test_inherited_context_term_must_be_resolved(tmp_path):
    path = tree(tmp_path)
    write_spec(path, MEMBER_ONLY_SPEC)
    result = build(path, tmp_path / "out")
    assert "AMBIGUOUS_TERM" in codes(result)
    guidance = guide.guide(path.read_text(encoding="utf-8"), path)
    assert guidance["stage"] == "TERMINOLOGY"
    assert guidance["next_question"]["type"] == "AMBIGUOUS_TERM"
    assert any("means profile" in option["writes"]
               for option in guidance["next_question"]["options"])
    assert guidance["already_known"]["glossary_sources"]


def test_inherited_context_resolution_builds_and_traces(tmp_path):
    path = tree(tmp_path)
    source = MEMBER_ONLY_SPEC + '\nglossary { "In this specification, member means profile." }\n'
    write_spec(path, source)
    result = build(path, tmp_path / "out")
    assert result["exit"] == 0
    report = json.loads((tmp_path / "out" / "terminology-report.json").read_text(encoding="utf-8"))
    assert report["normalizations"]
    assert any(term["term"] == "profile" for term in report["known_terms"])


def test_invalid_resolution_is_rejected(tmp_path):
    path = tree(tmp_path)
    source = MEMBER_ONLY_SPEC + '\nglossary { "In this specification, member means widget." }\n'
    write_spec(path, source)
    assert "INVALID_TERM_RESOLUTION" in codes(build(path, tmp_path / "out"))


def test_identity_conflict_with_inherited_profile_can_be_resolved(tmp_path):
    path = tree(tmp_path)
    source = MEMBER_ONLY_SPEC.replace(
        "The code uniquely identifies the member.",
        "The phone uniquely identifies the member.",
    ) + '\nglossary { "In this specification, member means profile." }\n'
    write_spec(path, source)
    assert "TERM_CONFLICT" in codes(build(path, tmp_path / "out"))
    decision = source + 'decision DD_C "Member is a separate concept in this specification." resolves TERM_profile = NEW_CONCEPT\n'
    write_spec(path, decision)
    assert "TERM_CONFLICT" not in codes(build(path, tmp_path / "out-resolved"))


def test_guide_ready_for_a_complete_neutral_spec():
    result = guide.guide(COMPLETE_MEMBER_SPEC, ROOT / "member-records.upa")
    assert result["ready_to_build"] and result["stage"] == "READY"
    assert result["next_question"]["type"] in ("NEW_TERM", "OPEN_QUESTION")


def test_guide_prioritizes_terminology_before_outcomes():
    source = MEMBER_SPEC + '\nglossary { "Members and profiles have similar features." }\n'
    result = guide.guide(source, ROOT / "members.upa")
    priorities = [question["priority"] for question in result["questions"]]
    assert priorities == sorted(priorities) and priorities[0] == 1
    assert result["questions"][0]["type"] == "GLOSSARY_UNSUPPORTED"


def test_unsupported_english_glossary_sentence_is_reported(tmp_path):
    path = write_spec(
        tmp_path / "members.upa",
        MEMBER_SPEC + '\nglossary { "Members and profiles have similar features." }\n',
    )
    assert "GLOSSARY_UNSUPPORTED" in codes(build(path, tmp_path / "out"))
