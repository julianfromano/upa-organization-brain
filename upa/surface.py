"""UPA Business Surface: controlled declarative language -> verifiable UPA Core.

Person or agent -> Business Surface (controlled sentences) -> analysis -> UPA Core (+IR) -> obligations -> Z3.

The surface declares facts, capabilities, rules, outcomes, assumptions, decisions, and questions. Entities,
attributes, relationships, operations, preconditions, and effects are DERIVED. Every element retains its provenance.
An ambiguous or out-of-catalog sentence never reaches the Core (AMBIGUOUS_RULE / UNSUPPORTED_PATTERN).
"""
from __future__ import annotations
import difflib
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ErrorBag
from .parser import tokenize, parse as parse_core
from .errors import UpaError

SECTIONS = ("input", "answers", "glossary", "intent", "business", "technical", "initial", "assumptions", "questions", "examples")
FORMAL_SECTIONS = ("business", "technical", "initial")


# --------------------------------------------------------------------------- lexical utilities
def ascii_(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", ascii_(s).lower()).strip("_")


def camel(s: str) -> str:
    return "".join(w.capitalize() for w in slug(s).split("_") if w)


def plural(word: str) -> str:
    irregular = {"person": "people", "child": "children", "person": "people", "mouse": "mice", "foot": "feet"}
    if word.lower() in irregular:
        return irregular[word.lower()]
    if re.search(r"[^aeiou]y$", word, re.I):
        return word[:-1] + "ies"
    if word.endswith(("s", "x", "z", "ch", "sh")):
        return word + "es"
    return word + "s"


def norm_s(s: str) -> str:
    return re.sub(r"\s+", " ", ascii_(s).lower()).strip().rstrip(".")


def split_list(s: str) -> list[str]:
    parts = re.split(r",\s*|\s+(?:and|or)\s+", s.strip(), flags=re.I)
    return [re.sub(r"^(?:a|an|the)\s+", "", re.sub(r"^(?:and|or)\s+", "", part.strip(), flags=re.I), flags=re.I)
            for part in parts if part.strip()]


# --------------------------------------------------------------------------- 1. reading the surface
@dataclass
class Sentence:
    id: str
    text: str
    section: str
    group: str | None
    line: int
    match_text: str | None = None   # text with normalized terms (alias -> canonical)
    capability: str | None = None   # for outcomes
    owner: str | None = None        # for assumptions
    cite: str | None = None         # v15: literal input excerpt supporting this sentence (from "...")
    prov: str = "ASSERTED"          # v15: ASSERTED | NORMALIZED | DERIVED | RESOLVED | TECHNICAL
    basis: list = field(default_factory=list)  # v15: source sentences/axioms (NORMALIZED/DERIVED/TECHNICAL)


@dataclass
class Surface:
    name: str
    version: str
    sentences: list[Sentence] = field(default_factory=list)
    decisions: list[dict] = field(default_factory=list)
    assumptions: list[dict] = field(default_factory=list)
    questions: list[dict] = field(default_factory=list)
    gaps: list[dict] = field(default_factory=list)  # discovered semantics that UPA cannot yet express
    uses: list[dict] = field(default_factory=list)
    glossary: list[Sentence] = field(default_factory=list)
    inherited: dict = field(default_factory=dict)   # sentence_id -> source specification (use spec)
    bases: list[dict] = field(default_factory=list)
    redundant: list[dict] = field(default_factory=list)  # repeated sentences (the first is used)
    level: str | None = None
    input: list = field(default_factory=list)   # v15: the author’s exact words
    answers: list = field(default_factory=list) # v15: exact responses to guide questions
    answer_ctx: dict = field(default_factory=dict)  # v15: response -> clarified excerpt (anchors the response to its question)
    answer_for: list = field(default_factory=list)  # v15: input excerpts clarified by those responses
    closure: dict = field(default_factory=dict) # v15: closure report


def parse_surface(src: str) -> Surface:
    toks = tokenize(src)
    i = 0

    def cur():
        return toks[i]

    def err(msg):
        raise UpaError("PARSE_ERROR", msg, cur().pos, "parse", {"found": cur().text or "EOF"})

    def expect(t):
        nonlocal i
        if cur().text != t or cur().kind == "STRING":
            err(f"expected '{t}'")
        i += 1

    def word():
        nonlocal i
        if cur().kind != "IDENT":
            err("expected a word")
        i += 1
        return toks[i - 1].text

    def string():
        nonlocal i
        if cur().kind != "STRING":
            err("expected a quoted sentence")
        i += 1
        return toks[i - 1]

    def cite():
        nonlocal i
        if cur().kind == "IDENT" and cur().text == "from":
            i += 1
            first = Cite(string().text)
            # v22: from "a" and "b": the sentence is supported by a combination of literal citations (for example, two responses)
            while cur().kind == "IDENT" and cur().text == "and":
                i += 1
                first.more.append(string().text)
            return first
        return None

    expect("spec")
    name = word()
    expect("version")
    s = Surface(name, string().text)
    if cur().text == "level" and cur().kind != "STRING":
        i += 1
        s.level = word()
    n = 0
    qn = 0
    an = 0
    while cur().kind != "EOF":
        w = word()
        if w in SECTIONS:
            expect("{")
            while cur().text != "}" or cur().kind == "STRING":
                if cur().kind == "STRING":
                    t = string()
                    owner = None
                    if cur().kind == "IDENT" and cur().text == "owner":
                        i += 1
                        owner = string().text
                    if w == "input":
                        s.input.append(t.text)
                    elif w == "answers":
                        s.answers.append(t.text)
                        if cur().kind == "IDENT" and cur().text == "for":
                            i += 1
                            s.answer_for.append(string().text)
                            s.answer_ctx[t.text] = s.answer_for[-1]
                    elif w == "glossary":
                        s.glossary.append(Sentence(f"G{len(s.glossary) + 1:03d}", t.text, "glossary", None, t.line, cite=cite()))
                    elif w == "assumptions":
                        an += 1
                        s.assumptions.append({"id": f"AS_{an}", "text": t.text, "owner": owner or "", "line": t.line})
                    elif w == "questions":
                        qn += 1
                        s.questions.append({"id": f"Q_{qn}", "text": t.text, "line": t.line})
                    else:
                        n += 1
                        s.sentences.append(Sentence(f"S{n:03d}", t.text, w, None, t.line, cite=cite()))
                elif w == "business" and cur().kind == "IDENT":
                    g = word()
                    expect("{")
                    while cur().kind == "STRING":
                        t = string()
                        n += 1
                        s.sentences.append(Sentence(f"S{n:03d}", t.text, w, g, t.line, cite=cite()))
                    expect("}")
                else:
                    err("only quoted sentences are allowed inside a block")
            expect("}")
        elif w == "outcomes":
            expect("for")
            cap = string().text
            expect("{")
            while cur().kind == "STRING":
                t = string()
                n += 1
                s.sentences.append(Sentence(f"S{n:03d}", t.text, "outcomes", None, t.line, capability=cap, cite=cite()))
            expect("}")
        elif w == "decision":
            did = word()
            d = {"id": did, "text": string().text, "alternatives": None, "resolves": None, "chooses": None}
            if cur().text == "alternatives" and cur().kind == "IDENT":
                i += 1
                d["alternatives"] = string().text
            if cur().text == "resolves" and cur().kind == "IDENT":
                i += 1
                d["resolves"] = word()
                expect("=")
                d["chooses"] = word()
            s.decisions.append(d)
        elif w == "assumption":
            aid = word()
            t = string().text
            expect("owner")
            s.assumptions.append({"id": aid, "text": t, "owner": string().text, "line": toks[i - 1].line})
        elif w == "gap":
            gid = word()
            g = {"id": gid, "text": string().text, "proposal": None, "evidence": None, "line": toks[i - 1].line}
            while cur().kind == "IDENT" and cur().text in ("proposal", "evidence"):
                k = cur().text
                i += 1
                g[k] = string().text
            s.gaps.append(g)
        elif w == "question":
            qid = word()
            s.questions.append({"id": qid, "text": string().text, "line": toks[i - 1].line})
        elif w == "use":
            if cur().text == "spec" and cur().kind == "IDENT":
                i += 1
                s.uses.append({"spec": string().text})
            else:
                expect("capability")
                s.uses.append({"capability": string().text})
        else:
            i -= 1
            err(f"unknown block '{w}'")
    return s


class Cite(str):
    """Literal citation; `more` holds additional citations that are read together (from \"a\" and \"b\")."""
    def __new__(cls, v):
        o = super().__new__(cls, v)
        o.more = []
        return o

    def parts(self):
        return [str(self)] + list(self.more)


def cite_parts(cite) -> list:
    return cite.parts() if hasattr(cite, "parts") else ([cite] if cite else [])


# --------------------------------------------------------------------------- 2. pattern catalog
PH = r"[^\W\d]+(?:\s[^\W\d]+){0,3}?"
DET = r"(?:a|an|the|each|every)"

PATTERNS: list[dict] = []


def pat(pid, kind, logical, rx, sections=("business",), required=(), doc="", alts=()):
    PATTERNS.append({"id": pid, "kind": kind, "logical": logical, "rx": re.compile("^" + rx + "$", re.I | re.U),
                     "sections": sections, "doc": doc,
                     "alts": [re.compile("^" + a + "$", re.I | re.U) for a in alts]})


pat("PROVIDED_ID", "provided_identity", ["identity_rule"],
    rf"each (?P<c>{PH}) has a unique identifier supplied by (?:the )?(?P<role>{PH})",
    doc="Each <concept> has a unique identifier supplied by <role>.")
pat("ID_GENERATED", "generated_identity", ["identity_rule"],
    rf"the identifier for (?:a|an|each|every) (?P<c>{PH}) is assigned by the system",
    doc="The identifier for each <concept> is assigned by the system.")
pat("HAS_PROPS", "structure", ["structural_fact"], rf"(?:every|each|a|an) (?P<c>{PH}) has (?P<props>.+)",
    doc="Every <concept> has <properties>.")
pat("IDENTITY", "identity_rule", ["identity_rule", "cardinality_constraint"],
    rf"(?:the )?(?P<p>{PH}) uniquely identifies (?:the )?(?P<c>{PH})",
    doc="The <property> uniquely identifies the <concept>.")
pat("REQUIRED", "required", ["invariant", "precondition"],
    rf"(?:the )?(?P<p>{PH})(?: of (?:a|an|the|each|every) (?P<c>{PH}))? is required",
    doc="The <property> [of a <concept>] is required.")
pat("REGISTER", "capability_register", ["capability"], rf"(?P<cs>{PH}) can be registered",
    doc="<concepts> can be registered.")
pat("ONLY_MODIFIABLE", "only_modifiable", ["temporal_preservation"],
    rf"only (?:the )?(?P<p>{PH}) of a registered (?P<c>{PH}) can be modified",
    doc="Only the <property> of a registered <concept> can be modified.")
pat("MODIFY", "capability_modify", ["capability"],
    rf"(?:the )?(?P<p>{PH}) of (?:a|an|the) (?P<c>{PH}) can be modified",
    doc="The <property> of a <concept> can be modified.")
pat("ONLY_MODIFIABLE_LIST", "only_modifiable_list", ["temporal_preservation"],
    rf"only (?P<ps>.+?) and the state of a registered (?P<c>{PH}) can be modified",
    doc="Only <property>, <property>, and the state of a registered <concept> can be modified.")
pat("NO_DELETE", "no_delete", ["temporal_preservation"], rf"registered (?P<cs>{PH}) cannot be deleted",
    doc="Registered <concepts> cannot be deleted.")
pat("NO_DELETE_ONE", "no_delete", ["temporal_preservation"],
    rf"a registered (?P<c>{PH}) cannot be deleted", doc="A registered <concept> cannot be deleted.")
pat("BELONGS", "relation_exactly_one", ["cardinality_constraint", "precondition"],
    rf"each (?P<a>{PH}) belongs to exactly one registered (?P<b>{PH})",
    doc="Each <A> belongs to exactly one registered <B>.")
pat("MIN", "min_bound", ["invariant", "precondition"],
    rf"(?:the )?(?P<p>{PH})(?: of (?:a|an|the|each|every) (?P<c>{PH}))? is (?P<n>\d+) or greater",
    doc="The <property> [of a <concept>] is <n> or greater.")
pat("MAX", "max_bound", ["invariant", "precondition"],
    rf"(?:the )?(?P<p>{PH})(?: of (?:a|an|the|each|every) (?P<c>{PH}))? is (?P<n>\d+) or less",
    doc="The <property> [of a <concept>] is <n> or less.")
pat("NUMBERING", "numbering", ["invariant", "temporal_property", "precondition"],
    rf"(?P<cs>{PH}) are numbered consecutively for each (?P<o>{PH}), starting at (?P<n>\d+)",
    doc="<concepts> are numbered consecutively for each <owner>, starting at <n>.")
pat("COUNT_MAX", "count_bound", ["invariant", "precondition", "aggregate_bound"],
    rf"a (?P<o>{PH}) cannot have more than (?P<n>\d+) (?P<cs>{PH})(?: in state (?P<sx>{PH}(?: or {PH})*))?",
    doc="A <owner> cannot have more than <n> <concepts> [in state <X>].")
pat("CAPACITY", "capacity_bound", ["invariant", "precondition", "aggregate_bound"],
    rf"a (?P<o>{PH}) cannot have more (?P<cs>{PH}) than its (?P<p>{PH})",
    doc="A <owner> cannot have more <concepts> than its <property>.")
pat("UNIQUE_COMBO", "unique_combination", ["invariant", "identity_rule", "precondition"],
    rf"there cannot be two (?P<cs>{PH})(?: for the same (?P<o>{PH}))? with the same (?P<p1>{PH})(?: and the same (?P<p2>{PH}))?",
    doc="There cannot be two <concepts> [for the same <owner>] with the same <A> [and the same <B>].  (or: There cannot be two <concepts> for the same <A> with the same <B>.)",
    alts=(rf"there cannot be two (?P<cs>{PH}) for the same (?P<p1>{PH}) with the same (?P<p2>{PH})",))
pat("IMMUTABLE", "immutable", ["temporal_preservation"],
    rf"a registered (?P<c>{PH}) cannot be modified", doc="A registered <concept> cannot be modified.")
pat("GENERATES", "generates_artifact", ["relation", "invariant"],
    rf"each (?P<c>{PH}) generates exactly one (?P<art>{PH}) file",
    doc="Each <concept> generates exactly one <type> file.")
pat("STATES", "states", ["structural_fact"],
    rf"the state of (?:every|each|a|an) (?P<c>{PH}) can be (?P<vals>.+)",
    doc="The state of every <concept> can be <A>, <B>, or <C>.")
pat("INITIAL_STATE_IF", "initial_state_conditional", ["temporal_property"],
    rf"a new (?P<c>{PH}) starts in state (?P<sa>{PH}) if its (?P<p>{PH}) is (?P<n>\d+) or less; otherwise, it starts in state (?P<sb>{PH})",
    doc="A new <concept> starts in state <A> if its <property> is <n> or less; otherwise, it starts in state <B>.")
pat("INITIAL_STATE", "initial_state_value", ["temporal_property"],
    rf"a new (?P<c>{PH}) starts in state (?P<sa>{PH})", doc="A new <concept> starts in state <A>.")
pat("TRANSITION", "state_transition", ["capability", "temporal_property"],
    rf"a (?P<c>{PH}) in state (?P<sa>{PH}) can transition to state (?P<sb>{PH})",
    doc="A <concept> in state <A> can transition to state <B>.")
pat("UNIQUE_SCOPED", "unique_combination_scoped", ["invariant", "precondition"],
    rf"among (?P<cs>{PH}) not in state (?P<sx>{PH}(?: or {PH})*), there cannot be two with the same (?P<p1>{PH})(?: and the same (?P<p2>{PH}))?",
    doc="Among <concepts> not in state <X> [or <Y>], there cannot be two with the same <A> [and the same <B>].")
pat("CONSISTENT", "artifact_consistency", ["technical_invariant"],
    rf"(?:the )?(?P<a>{PH}) and its (?P<art>{PH}) file cannot end in inconsistent final states",
    sections=("technical",), doc="The <concept> and its <X> file cannot end in inconsistent final states.")
pat("CONTENT_INPUT", "artifact_content_input", ["input_assumption"],
    rf"the content for the (?P<art>{PH}) file is provided when registering (?:(?:a|an|the) )?(?P<c>{PH})",
    sections=("technical",), doc="The content for the <file> file is provided when registering the <concept>.")
pat("INITIAL", "initial_state", ["initial_state"], rf"initially, there are no (?P<list>.+)",
    sections=("initial",), doc="Initially, there are no <concepts>.")
_OUT = r"the result is (?P<n>[\w]+) and (?P<eff>.+)"
pat("OUT_DUPLICATE", "outcome_duplicate", ["outcome"],
    rf"if (?:the )?(?P<p>{PH}) already belongs to (?:a|an) (?P<c>{PH}), {_OUT}", sections=("outcomes",),
    doc="If the <identifier> already belongs to a <concept>, the result is <Name> and no information changes.")
pat("OUT_NOT_INFORMED", "outcome_not_informed", ["outcome"],
    rf"if (?:the )?(?P<p>{PH}) is missing, {_OUT}", sections=("outcomes",),
    doc="If the <property> is missing, the result is <Name> and no information changes.")
pat("OUT_REF_MISSING", "outcome_ref_missing", ["outcome"],
    rf"if (?:the )?(?P<c>{PH}) is not registered, {_OUT}", sections=("outcomes",),
    doc="If the <concept> is not registered, the result is <Name> and the <other concept> is not registered.")
pat("OUT_BELOW_MIN", "outcome_below_min", ["outcome"],
    rf"if (?:the )?(?P<p>{PH}) is less than (?P<k>\d+), {_OUT}", sections=("outcomes",),
    doc="If the <property> is less than <n>, the result is <Name> and no information changes.")
pat("OUT_ABOVE_MAX", "outcome_above_max", ["outcome"],
    rf"if (?:the )?(?P<p>{PH}) is greater than (?P<k>\d+), {_OUT}", sections=("outcomes",),
    doc="If the <property> is greater than <n>, the result is <Name> and no information changes.")
pat("OUT_CAPACITY", "outcome_capacity", ["outcome"],
    rf"if (?:the )?(?P<o>{PH}) has no (?P<p>{PH}) available, {_OUT}", sections=("outcomes",),
    doc="If the <owner> has no <property> available, the result is <Name> and no information changes.")
pat("OUT_COUNT", "outcome_count_bound", ["outcome"],
    rf"if (?:the )?(?P<o>{PH}) already has (?P<k>\d+) (?P<cs>{PH})(?: in state (?P<sx>{PH}(?: or {PH})*))?, {_OUT}",
    sections=("outcomes",), doc="If the <owner> already has <n> <concepts> [in state <X>], the result is <Name> and no information changes.")
pat("OUT_COMBO", "outcome_combo_duplicate", ["outcome"],
    rf"if (?:a|an) (?P<c>{PH})(?: not in state (?P<sx>{PH}(?: or {PH})*))?(?: for the same (?P<o>{PH}))? already exists with the same (?P<p1>{PH})(?: and the same (?P<p2>{PH}))?, {_OUT}",
    sections=("outcomes",), doc="If a <concept> [not in state <X>] [for the same <owner>] already exists with the same <A> [and the same <B>], the result is <Name> and no information changes.",
    alts=(rf"if (?:a|an) (?P<c>{PH})(?: not in state (?P<sx>{PH}(?: or {PH})*))? already exists for the same (?P<p1>{PH}) with the same (?P<p2>{PH}), {_OUT}",))
pat("OUT_BELOW_COUNT", "outcome_below_count", ["outcome"],
    rf"if the new (?P<p>{PH}) is less than the number of (?P<cs>{PH}) for (?:the )?(?P<o>{PH}), {_OUT}", sections=("outcomes",),
    doc="If the new <property> is less than the number of <concepts> for the <owner>, the result is <Name> and no information changes.")
pat("OUT_WRONG_STATE", "outcome_wrong_state", ["outcome"],
    rf"if (?:the )?(?P<c>{PH}) is not in state (?P<ss>.+?), {_OUT}", sections=("outcomes",),
    doc="If the <concept> is not in state <A> [or <B>], the result is <Name> and no information changes.")
pat("OUT_SUCCESS_STATE", "outcome_success_state", ["outcome"],
    rf"otherwise, (?:the )?(?P<c>{PH}) ends up in state (?P<s>{PH})", sections=("outcomes",),
    doc="Otherwise, the <concept> ends up in state <B>.")
pat("OUT_SUCCESS_REG", "outcome_success_register", ["outcome"],
    rf"otherwise, (?:the )?(?P<c>{PH}) is registered", sections=("outcomes",),
    doc="Otherwise, the <concept> is registered.")
pat("OUT_SUCCESS_MOD", "outcome_success_modify", ["outcome"],
    rf"otherwise, (?:the )?(?P<p>{PH}) of (?:a|an|the) (?P<c>{PH}) is modified", sections=("outcomes",),
    doc="Otherwise, the <property> of the <concept> is modified.")

SECTION_TITLE = {"business": "business { <group> { ... } }", "technical": "technical { ... }", "initial": "initial { ... }",
                 "outcomes": 'outcomes for "<Capability>" { ... }  (one sentence per outcome; the last is “Otherwise, …”)'}

def catalog() -> dict:
    """Explicit catalog of accepted sentences by block (v14: the agent receives the full catalog rather than learning by trial and error)."""
    from .terminology import GL_DOC
    out: dict[str, list] = {}
    for p in PATTERNS:
        out.setdefault(p["sections"][0], []).append({"id": p["id"], "form": p["doc"]})
    out["glossary"] = [{"id": k, "form": v} for k, v in GL_DOC.items()]
    return out


def catalog_markdown() -> str:
    cat = catalog()
    lines = [
        "Write one sentence per line, in quotation marks, ending with a period. Replace `<...>` with business terms (use a singular concept unless",
        "the placeholder says <concepts>). Capitalization does not matter; spell each term consistently. Nothing outside this list compiles.",
        "If the person says something else, rephrase it using one of these forms or record it as a `question`.",
        "",
    ]
    for section in ["business", "outcomes", "initial", "technical", "glossary"]:
        lines.append(f"**{SECTION_TITLE.get(section, 'glossary { ... }')}**")
        lines += [f"- `{item['form']}`  ({item['id']})" for item in cat.get(section, [])]
        lines.append("")
    lines += [
        "**Header and other blocks**",
        '- `spec <Name> version "<v>" [level <layer|organization|domain|module|specific>]`',
        '- `use spec "<base>.upa"` (an increment based on an existing specification)',
        '- `decision <ID> "<text>" [alternatives "<x>"] [resolves <ID> = <CHOICE>]` · `question <ID> "<text>"` · `assumptions { "<sentence>" owner "<x>" }`',
        '- `input { "<the person’s exact words>" }` · `answers { "<exact response>" [for "<input excerpt being clarified>"] }`',
        '- after any sentence: `from "<exact excerpt from input or answers>"` (required when `input` is present)',
        "",
    ]
    from .closure import AXIOMS, INPUT_FORMS
    lines += ["**Input forms (v15): the compiler decomposes these into canonical sentences (NORMALIZED)**"]
    lines += [f"- `{documentation}`  ({form_id})" for form_id, _, documentation in INPUT_FORMS]
    lines += ["", "**What the compiler derives automatically (DERIVED/TECHNICAL): do not write these manually**"]
    lines += [f"- {key}: {value}" for key, value in AXIOMS.items()]
    return "\n".join(lines)


VAGUE = re.compile(r"\b(correctly|properly|appropriately|well|quickly|efficiently|safely|easily|reasonably|optimally|normally|when appropriate|etcetera|etc)\b", re.I)



def match_sentence(sn: Sentence):
    t = re.sub(r"\s+", " ", (sn.match_text or sn.text).strip()).rstrip(".")
    ordered_patterns = sorted(PATTERNS, key=lambda item: item["id"] != "ONLY_MODIFIABLE_LIST")
    for p in ordered_patterns:
        if sn.section not in p["sections"] and not (sn.section == "technical" and "business" in p["sections"]):
            continue
        m = p["rx"].match(t)
        for a in p.get("alts", ()):
            m = m or a.match(t)
        if m:
            return p, {k: v for k, v in m.groupdict().items()}
    return None, None


 # --------------------------------------------------------------------------- 3. analysis
class Analysis:
    def __init__(self, surf: Surface):
        self.s = surf
        self.errors = ErrorBag()
        self.warnings: list[dict] = []
        self.matched: list[dict] = []
        self.unresolved: list[dict] = []
        self.rules: list[dict] = []        # one per recognized sentence
        self.concepts: dict[str, dict] = {}
        self.lexicon: dict[str, str] = {}  # singular normalizado -> display
        self.heuristics: list[dict] = []
        self.ambiguities: list[dict] = []
        self.incomplete: list[dict] = []
        self.capabilities: list[dict] = []
        self.outcomes_decl: list[dict] = []
        self.states_redundant: list[dict] = []
        self.initial_concepts: list[str] = []
        self.roles: dict[str, str] = {}
        self.artifacts: dict[str, dict] = {}

    # ---- step 1: recognize sentences
    def recognize(self):
        for sn in self.s.sentences:
            p, g = match_sentence(sn)
            if p is None:
                self._unresolved(sn)
                continue
            self.matched.append({"sentence": sn.id, "text": sn.text, "section": sn.section, "pattern": p["id"],
                                 "semantic_form": {"kind": p["kind"], **g}, "logical": p["logical"]})
            self.rules.append({"sn": sn, "p": p, "g": g})
        # Concept lexicon: singular slots
        for r in self.rules:
            if r["sn"].section == "outcomes":
                continue
            for k in ("c", "a", "b", "o"):
                v = r["g"].get(k)
                if v and not (r["p"]["id"] == "CAPACITY" and k == "o" and False):
                    self.lexicon.setdefault(norm_s(v), v.lower())
        from . import closure as _cl
        if _cl.ENABLED:
            # v15: “<Concepts> can be registered” introduces the concept; it need not also be named in singular form
            for r in self.rules:
                if r["p"]["id"] == "REGISTER":
                    cs = norm_s(r["g"]["cs"].strip())
                    if any(x in self.lexicon for x in (cs, cs[:-1], cs[:-2])):
                        continue
                    v = _cl.singular(r["g"]["cs"].strip())
                    self.lexicon.setdefault(norm_s(v), v.lower())

    def _unresolved(self, sn: Sentence):
        if VAGUE.search(sn.text):
            e = {"error": "AMBIGUOUS_RULE", "source": sn.text, "sentence": sn.id, "line": sn.line,
                 "reason": f"The term {VAGUE.search(sn.text).group(0)!r} does not define observable outcomes or verifiable properties.",
                 "required_information": ["valid outcome", "invalid outcome", "properties to preserve"]}
        else:
            near = difflib.get_close_matches(sn.text.lower(), [p["doc"].lower() for p in PATTERNS], n=2, cutoff=0.0)
            e = {"error": "UNSUPPORTED_PATTERN", "source": sn.text, "sentence": sn.id, "line": sn.line,
                 "reason": "The sentence does not match a catalog pattern; free-form language is not interpreted.",
                 "closest_patterns": near}
        self.unresolved.append(e)
        self.errors.add(e["error"], e["reason"], (sn.line, 1), "surface", source=sn.text)

    # ---- concept resolution
    def concept(self, raw: str, plural=False, sn: Sentence | None = None) -> str | None:
        n = norm_s(raw)
        cands = [n]
        if plural or n not in self.lexicon:
            words = n.split(" ")
            last = words[-1]
            for suf in ("es", "s"):
                if last.endswith(suf):
                    cands.append(" ".join(words[:-1] + [last[: -len(suf)]]))
        for c in cands:
            if c in self.lexicon:
                if c != n:
                    self.heuristics.append({"term": raw, "resolved_to": c, "heuristic": "plural-to-singular normalization",
                                            "confidence": "high"})
                key = c
                if key not in self.concepts:
                    disp = self.lexicon[key]
                    self.concepts[key] = {"key": key, "display": disp, "core": camel(key), "props": {}, "identity": None,
                                          "provided_id": None, "id_generated": None, "required": set(), "register": None, "modify": [],
                                          "only_mod": None, "only_mods": None, "no_delete": None, "immutable": None, "refs": [],
                                          "min": {}, "max": {}, "numbering": None, "capacity": None, "count_max": [], "combos": [], "cap_of": [], "combo_identity": None, "artifact": None, "states": None, "sources": [],
                                          "owner_of": []}
                return key
        self.errors.add("UNRESOLVED_BUSINESS_TERM", f"term {raw!r} is not a concept declared with 'Every <concept> has …' "
                        "or used in a sentence naming a singular concept", (sn.line, 1) if sn else None, "surface")
        self.unresolved.append({"error": "UNRESOLVED_BUSINESS_TERM", "source": sn.text if sn else "", "term": raw,
                                "sentence": sn.id if sn else None})
        return None

    def prop(self, ckey: str, raw: str, sn: Sentence, ptype=None, why="declared") -> str:
        c = self.concepts[ckey]
        pk = slug(raw)
        e = c["props"].setdefault(pk, {"name": pk, "display": raw.lower(), "type": ptype or "Text", "sources": [],
                                       "inferred_by": []})
        if sn.id not in e["sources"]:
            e["sources"].append(sn.id)
        if ptype and e["type"] == "Text":
            e["type"] = ptype
        return pk

    def owner_of_prop(self, raw: str, sn: Sentence) -> str | None:
        pk = slug(raw)
        owners = [k for k, c in self.concepts.items() if pk in c["props"]]
        if len(owners) == 1:
            return owners[0]
        if not owners:
            self.errors.add("UNRESOLVED_BUSINESS_TERM", f"property {raw!r} does not belong to any concept", (sn.line, 1), "surface")
            self.unresolved.append({"error": "UNRESOLVED_BUSINESS_TERM", "source": sn.text, "term": raw, "sentence": sn.id,
                                    "unowned_property": True, "candidates": sorted(set(self.lexicon.values()))})
        else:
            self.ambiguities.append({"id": f"AMBS_prop_{pk}", "kind": "AMBIGUOUS_MODEL", "sentence": sn.id,
                                     "reason": f"property {raw!r} exists on multiple concepts {owners}; no choice is made silently",
                                     "candidates": owners, "status": "pending_human"})
            self.errors.add("AMBIGUOUS_MODEL", f"property {raw!r} exists on multiple concepts {owners}", (sn.line, 1), "surface")
        return None

    # ---- step 2: build the model
    def build_model(self):
        R = self.rules
        by = lambda pid: [r for r in R if r["p"]["id"] == pid]
        # estructura
        for r in by("HAS_PROPS") + by("PROVIDED_ID") + by("GENERATES") + by("BELONGS") + by("MIN") + by("IMMUTABLE") + by("STATES"):
            pass
        for r in by("HAS_PROPS"):
            ck = self.concept(r["g"]["c"], sn=r["sn"])
            if ck:
                self.concepts[ck]["sources"].append(r["sn"].id)
                for p in split_list(r["g"]["props"]):
                    self.prop(ck, p, r["sn"])
        for r in by("IDENTITY"):
            ck = self.concept(r["g"]["c"], sn=r["sn"])
            if ck:
                pk = self.prop(ck, r["g"]["p"], r["sn"], "Identifier")
                c = self.concepts[ck]
                if c["identity"] and c["identity"][0] != pk:
                    self.errors.add("AMBIGUOUS_MODEL", f"{c['display']} has two identities", (r["sn"].line, 1), "surface")
                c["identity"] = (pk, r["sn"].id)
                c["props"][pk]["type"] = "Identifier"
        for r in by("PROVIDED_ID"):
            ck = self.concept(r["g"]["c"], sn=r["sn"])
            if ck:
                self.concepts[ck]["provided_id"] = (norm_s(r["g"]["role"]), r["sn"].id)
                self.roles[slug(r["g"]["role"])] = r["sn"].id
        for r in by("ID_GENERATED"):
            ck = self.concept(r["g"]["c"], sn=r["sn"])
            if ck:
                self.concepts[ck]["provided_id"] = ("system", r["sn"].id)
                self.concepts[ck]["id_generated"] = r["sn"].id
        for r in by("REQUIRED"):
            if r["g"].get("c"):
                ck = self.concept(r["g"]["c"], sn=r["sn"])
                if ck and slug(r["g"]["p"]) not in self.concepts[ck]["props"]:
                    self.errors.add("UNRESOLVED_BUSINESS_TERM", f"{r['g']['p']!r} is not a declared property of {self.concepts[ck]['display']}",
                                    (r["sn"].line, 1), "surface")
                    ck = None
            else:
                ck = self.owner_of_prop(r["g"]["p"], r["sn"])
            if ck:
                self.concepts[ck]["required"].add(slug(r["g"]["p"]))
                self.concepts[ck]["props"][slug(r["g"]["p"])]["sources"].append(r["sn"].id)
        for r in by("MIN") + by("MAX"):
            # v15: omit the concept when the property belongs to exactly one concept (uniqueness-based resolution)
            ck = self.concept(r["g"]["c"], sn=r["sn"]) if r["g"].get("c") else self.owner_of_prop(r["g"]["p"], r["sn"])
            if ck:
                pk = self.prop(ck, r["g"]["p"], r["sn"], "Natural")
                self.concepts[ck]["min" if r["p"]["id"] == "MIN" else "max"][pk] = (int(r["g"]["n"]), r["sn"].id)
                self.concepts[ck]["props"][pk]["inferred_by"].append(r["sn"].id)
                if not r["g"].get("c"):
                    self.heuristics.append({"term": r["g"]["p"], "heuristic": "property belongs to a unique concept",
                                            "resolved_to": self.concepts[ck]["display"], "confidence": "derived", "source": r["sn"].id})
        for r in by("BELONGS"):
            a = self.concept(r["g"]["a"], sn=r["sn"])
            b = self.concept(r["g"]["b"], sn=r["sn"])
            if a and b:
                self.concepts[a]["refs"].append({"target": b, "sn": r["sn"].id})
        for r in by("NO_DELETE") + by("NO_DELETE_ONE"):
            ck = self.concept(r["g"].get("cs") or r["g"]["c"], plural="cs" in r["g"], sn=r["sn"])
            if ck:
                self.concepts[ck]["no_delete"] = r["sn"].id
        for r in by("IMMUTABLE"):
            ck = self.concept(r["g"]["c"], sn=r["sn"])
            if ck:
                self.concepts[ck]["immutable"] = r["sn"].id
        for r in by("ONLY_MODIFIABLE"):
            ck = self.concept(r["g"]["c"], sn=r["sn"])
            if ck:
                pk = self.prop(ck, r["g"]["p"], r["sn"])
                self.concepts[ck]["only_mod"] = (pk, r["sn"].id)
        for r in by("ONLY_MODIFIABLE_LIST"):
            ck = self.concept(r["g"]["c"], sn=r["sn"])
            if ck:
                props, state_flag = [], True
                for x in split_list(r["g"]["ps"]):
                    x = re.sub(r"^(?:a|an|the)\s+", "", x.strip(), flags=re.I)
                    if norm_s(x) == "state":
                        state_flag = True
                    else:
                        props.append(slug(x))
                self.concepts[ck]["only_mods"] = {"props": props, "state": state_flag, "sn": r["sn"].id}
        for r in by("REGISTER"):
            ck = self.concept(r["g"]["cs"], plural=True, sn=r["sn"])
            if ck:
                self.concepts[ck]["register"] = r["sn"].id
        for r in by("MODIFY"):
            ck = self.concept(r["g"]["c"], sn=r["sn"])
            if ck:
                pk = self.prop(ck, r["g"]["p"], r["sn"])
                self.concepts[ck]["modify"].append((pk, r["sn"].id))
        for r in by("NUMBERING"):
            ck = self.concept(r["g"]["cs"], plural=True, sn=r["sn"])
            ok = self.concept(r["g"]["o"], sn=r["sn"])
            if ck and ok:
                self.concepts[ck]["numbering"] = {"owner": ok, "start": int(r["g"]["n"]), "sn": r["sn"].id}
                # v14: “has a number” and numbering refer to the same value; numbering defines its type (Natural, consecutive)
                if self.concepts[ck]["props"].pop("number", None) is not None:
                    self.heuristics.append({"id": f"H_NUMBER_{ck}", "rule": "the declared number is the sequence number", "sentence": r["sn"].id})
                self.concepts[ok]["owner_of"].append(ck)
        for r in by("CAPACITY"):
            ck = self.concept(r["g"]["cs"], plural=True, sn=r["sn"])
            ok = self.concept(r["g"]["o"], sn=r["sn"])
            if ck and ok:
                pk = self.prop(ok, r["g"]["p"], r["sn"], "Natural")
                self.concepts[ok]["props"][pk]["inferred_by"].append(r["sn"].id)
                self.concepts[ck]["capacity"] = {"owner": ok, "prop": pk, "sn": r["sn"].id}
                self.concepts[ok]["cap_of"].append(ck)
        for r in by("UNIQUE_COMBO"):
            ck = self.concept(r["g"]["cs"], plural=True, sn=r["sn"])
            if ck:
                self.concepts[ck]["combos"].append({"p1": slug(r["g"]["p1"]), "p2": slug(r["g"]["p2"]) if r["g"].get("p2") else None, "owner": slug(r["g"]["o"]) if r["g"].get("o") else None, "sn": r["sn"].id})
        for r in by("GENERATES"):
            ck = self.concept(r["g"]["c"], sn=r["sn"])
            if ck:
                art = r["g"]["art"].upper()
                self.concepts[ck]["artifact"] = {"name": art, "sn": r["sn"].id, "identity": None, "content_input": None}
                self.artifacts[art] = {"name": art, "of": ck, "sn": r["sn"].id}
        for r in by("STATES"):
            ck = self.concept(r["g"]["c"], sn=r["sn"])
            if ck:
                vals = split_list(re.sub(r"\s+or\s+", ", ", r["g"]["vals"]))
                self.concepts[ck]["states"] = {"values": vals, "sn": r["sn"].id}
                if len(vals) <= 1:
                    self.states_redundant.append({"warning": "REDUNDANT_STATE", "concept": f"{self.concepts[ck]['core']}.state",
                                                  "reason": "The attribute has only one possible value and does not distinguish states.",
                                                  "suggested_action": "Remove the attribute or declare additional states with observable semantics.",
                                                  "source": r["sn"].id})
        self._states(by)
        for r in by("CONSISTENT"):
            ck = self._concept_by_artifact(r["g"]["art"], r["g"]["a"], r["sn"])
            if ck:
                self.concepts[ck]["consistency"] = r["sn"].id
        for r in by("CONTENT_INPUT"):
            ck = self.concept(r["g"]["c"], sn=r["sn"])
            if ck and self.concepts[ck]["artifact"]:
                self.concepts[ck]["artifact"]["content_input"] = r["sn"].id
        for r in by("INITIAL"):
            for x in split_list(r["g"]["list"]):
                ck = self.concept(x, plural=True, sn=r["sn"])
                if ck:
                    self.initial_concepts.append(ck)
            self.initial_sn = r["sn"].id
            self.initial_sns = getattr(self, "initial_sns", []) + [r["sn"].id]
        # Artifact identity decision
        for d in self.s.decisions:
            m = re.match(r"^the (?P<art>.+?) file uses the same identifier as the (?P<c>.+?)\.?$", norm_s(d["text"]), re.I)
            if m:
                ck = self.concept(m.group("c"), sn=Sentence("D", d["text"], "decision", None, 0))
                if ck and self.concepts[ck]["artifact"]:
                    self.concepts[ck]["artifact"]["identity"] = d["id"]
        self._model_checks()

    def concept_key_plural(self, raw):
        """Resolve a known concept named in plural form without reporting errors or creating a concept."""
        v = norm_s(raw)
        for cand in (v, v[:-1], v[:-2], v.replace("es ", " ")):
            if cand in self.concepts:
                return cand
        return None

    def _concept_by_artifact(self, art, raw, sn):
        a = self.artifacts.get(art.upper())
        if a:
            return a["of"]
        return self.concept(raw, sn=sn)

    # ---- model checks
    def _origin(self, sid) -> dict:
        sid = sid.id if hasattr(sid, "id") else sid
        sn = next((s for s in self.s.sentences if s.id == sid), None)
        return {"id": sid, "text": sn.text if sn else None, "spec": self.s.inherited.get(sid, "this specification")}

    def _states(self, by):
        """v22: lifecycle states named in a sentence must be declared for the concept (states are never invented); the author declares the initial state and transitions."""
        def st(ck, raw, sn):
            s = self.concepts[ck]["states"]
            v = norm_s(raw)
            if not s:
                self.errors.add("UNRESOLVED_BUSINESS_TERM", f"«{sn.text}»: {self.concepts[ck]['display']} has undeclared states "
                                f"(The state of each {self.concepts[ck]['display']} can be …)", (sn.line, 1), "surface")
                return None
            hit = next((x for x in s["values"] if norm_s(x) == v), None)
            if hit is None:
                self.errors.add("UNRESOLVED_BUSINESS_TERM", f"«{raw}» is not a declared state of {self.concepts[ck]['display']} "
                                f"({', '.join(s['values'])})", (sn.line, 1), "surface")
            return hit
        for c in self.concepts.values():
            if c["states"]:
                c["states"].setdefault("initial", None)
                c["states"].setdefault("trans", {})
        for r in by("INITIAL_STATE") + by("INITIAL_STATE_IF"):
            ck = self.concept(r["g"]["c"], sn=r["sn"])
            if not ck:
                continue
            a_ = st(ck, r["g"]["sa"], r["sn"])
            if r["p"]["id"] == "INITIAL_STATE":
                if a_:
                    self.concepts[ck]["states"]["initial"] = {"kind": "fixed", "a": a_, "sn": r["sn"].id}
                continue
            b_ = st(ck, r["g"]["sb"], r["sn"])
            pk = self.prop(ck, r["g"]["p"], r["sn"], "Natural")
            self.concepts[ck]["props"][pk]["inferred_by"].append(r["sn"].id)
            if a_ and b_:
                self.concepts[ck]["states"]["initial"] = {"kind": "if", "a": a_, "b": b_, "p": pk, "n": int(r["g"]["n"]), "sn": r["sn"].id}
        for r in by("TRANSITION"):
            ck = self.concept(r["g"]["c"], sn=r["sn"])
            if not ck:
                continue
            a_, b_ = st(ck, r["g"]["sa"], r["sn"]), st(ck, r["g"]["sb"], r["sn"])
            if a_ and b_:
                self.concepts[ck]["states"]["trans"].setdefault(b_, []).append((a_, r["sn"].id))
        for r in by("UNIQUE_SCOPED"):
            ck = self.concept(r["g"]["cs"], plural=True, sn=r["sn"])
            if ck:
                # v24: “not in state A or B” (multiple final states)
                xs = [st(ck, x, r["sn"]) for x in re.split(r"\s+or\s+", r["g"]["sx"].strip())]
                if xs and all(xs):
                    xs = list(dict.fromkeys(xs))
                    self.concepts[ck]["combos"].append({"p1": slug(r["g"]["p1"]), "p2": slug(r["g"]["p2"]) if r["g"].get("p2") else None,
                                                        "owner": slug(r["g"]["o"]) if r["g"].get("o") else None,
                                                        "sn": r["sn"].id, "scope": " or ".join(xs), "scope_list": xs})
        for r in by("COUNT_MAX"):
            # v26: optionally bound the number per owner for records in particular states (“in progress”)
            ck = self.concept(r["g"]["cs"], plural=True, sn=r["sn"])
            ok = self.concept(r["g"]["o"], sn=r["sn"])
            if ck and ok:
                xs = [st(ck, x, r["sn"]) for x in re.split(r"\s+or\s+", r["g"]["sx"].strip())] if r["g"].get("sx") else []
                if all(xs):
                    self.concepts[ck]["count_max"].append({"owner": ok, "n": int(r["g"]["n"]), "scope_list": list(dict.fromkeys(xs)),
                                                           "sn": r["sn"].id})
        for c in self.concepts.values():
            s = c["states"]
            if s and len(s["values"]) > 1:
                miss = (["initial state"] if not s["initial"] else []) + (["allowed transitions between states"] if not s["trans"] else [])
                if miss:
                    self.incomplete.append({"status": "INCOMPLETE_CAPABILITY", "capability": f"states of {c['display']}", "missing": miss})

    def _model_checks(self):
        for k, c in self.concepts.items():
            attrs = set(c["props"]) | {slug(self.concepts[r["target"]]["display"]) for r in c["refs"]} | ({"number"} if c["numbering"] else set())
            for cb in c["combos"]:
                for p in combo_ps(cb):
                    if p not in attrs:
                        self.errors.add("UNRESOLVED_BUSINESS_TERM", f"'{p}' is not a property or relationship of {c['display']}", None, "surface")
            if c["identity"]:
                # v21: “There cannot be two X with the same K” adds no rule when K is the identity; identity already guarantees uniqueness.
                same = [cb for cb in c["combos"] if combo_ps(cb) == [c["identity"][0]] and not cb.get("scope")]
                if same:
                    c["combos"] = [cb for cb in c["combos"] if cb not in same]
                    c["id_unique"] = same[0]
            if c["numbering"] and not c["identity"] and not c["provided_id"]:
                # v14: numbering per owner already identifies a record by (owner, number); numbering guarantees uniqueness,
                # so identity does not need to be asked, and an explicit combination with the number is redundant (no extra rejection)
                own = slug(self.concepts[c["numbering"]["owner"]]["display"])
                c["combos"] = [cb for cb in c["combos"] if {cb["p1"], cb["p2"]} != {own, "number"}]
                c["combo_identity"] = {"p1": own, "p2": "number", "sn": c["numbering"]["sn"], "derived_from": "NUMBERING"}
                self.heuristics.append({"term": c["display"], "heuristic": "identity by numbering (owner, number)",
                                        "resolved_to": f"({own}, number)", "confidence": "high", "source": c["numbering"]["sn"]})
            if c["register"] and not c["identity"] and not c["provided_id"] and not c["combo_identity"] and [x for x in c["combos"] if not x.get("scope")]:
                cb = [x for x in c["combos"] if not x.get("scope")][0]
                c["combo_identity"] = cb
                self.heuristics.append({"term": c["display"], "heuristic": "identity by combination (minimal model, principle 13)",
                                        "resolved_to": f"({', '.join(combo_ps(cb))})", "confidence": "medium", "source": cb["sn"]})
            # v14: without a capability to create a concept, its rules hold vacuously; “only modifiable” does not enable modification
            if c["register"] is None and f"DD_SOURCE_{k}" not in {d.get("id") for d in self.s.decisions}:
                self.errors.add("MISSING_CAPABILITY", f"no sentence says how {c['display']} are registered", None, "surface",
                                concept=k, kind="register")
            if c["only_mod"] and not c["modify"]:
                self.errors.add("MISSING_CAPABILITY", f"only {c['only_mod'][0]} of {c['display']} can be modified, but no modification capability "
                                "is declared", None, "surface", concept=k, kind="modify")
            for pk, pr in c["props"].items():
                # v26: “the number of loans for a member” is a count of loans, not a member property
                m_ = re.match(r"^(?:quantity|number|total) of (.+)$", norm_s(pr["display"]))
                if m_ and m_.group(1) not in self.concepts and m_.group(1).endswith("s") and self.concept_key_plural(m_.group(1)):
                    self.errors.add("AGGREGATE_AS_PROPERTY", f"{pr['display']} of {c['display']} counts {m_.group(1)}; it is not a data field: "
                                    f"write ‘A {c['display']} cannot have more than <n> {m_['group(1)']} [in state <X>]’", None, "surface",
                                    concept=k, prop=pk, counted=m_.group(1))
            for cm in c["count_max"]:
                if not any(r["target"] == cm["owner"] for r in c["refs"]):
                    self.incomplete.append({"status": "INCOMPLETE_CAPABILITY", "capability": f"count bound for {c['display']}",
                                            "missing": [f"relationship between each {c['display']} and its {self.concepts[cm['owner']]['display']}"]})
            if c["capacity"]:
                cp = c["capacity"]
                if not any(r["target"] == cp["owner"] for r in c["refs"]):
                    self.incomplete.append({"status": "INCOMPLETE_CAPABILITY", "capability": f"capacity for {c['display']}",
                                            "missing": [f"relationship between each {c['display']} and its {self.concepts[cp['owner']]['display']}"]})
            if c["register"] and not c["identity"] and not c["provided_id"] and not c["combo_identity"]:
                self.incomplete.append({"status": "INCOMPLETE_CAPABILITY", "capability": f"Register {c['display']}",
                                        "missing": [f"how a {c['display']} is identified (a property that uniquely identifies it ",
                                                    "or an identifier supplied by a role)"]})
            if c["immutable"] and (c["states"] or {}).get("trans"):
                self.errors.add("CAPABILITY_RULE_CONFLICT", f"{c['display']}: state transitions conflict with the 'cannot be modified' rule",
                                None, "surface", sentences=[self._origin(c["immutable"])] +
                                [self._origin(x) for fr in c["states"]["trans"].values() for _, x in fr], concept=c["display"])
            if c["only_mod"] and (c["states"] or {}).get("trans"):
                self.errors.add("CAPABILITY_RULE_CONFLICT", f"{c['display']}: state transitions conflict with the modification restriction",
                                None, "surface", sentences=[self._origin(c["only_mod"][1])] +
                                [self._origin(x) for fr in c["states"]["trans"].values() for _, x in fr], concept=c["display"])
            om = c["only_mods"]
            if om:
                for pk in om["props"]:
                    if pk not in c["props"]:
                        self.errors.add("UNRESOLVED_BUSINESS_TERM", f"{pk!r} is not a declared property of {c['display']}", None, "surface",
                                        concept=k, prop=pk)
                if om["state"] and not c["states"]:
                    self.errors.add("CAPABILITY_RULE_CONFLICT", f"{c['display']}: the rule permits state changes, but no states are declared",
                                    None, "surface", sentences=[self._origin(om["sn"])], concept=c["display"])
                if not om["state"] and (c["states"] or {}).get("trans"):
                    self.errors.add("CAPABILITY_RULE_CONFLICT", f"{c['display']}: state transitions are declared, but the rule does not permit changing state",
                                    None, "surface", sentences=[self._origin(om["sn"])] +
                                    [self._origin(x) for fr in c["states"]["trans"].values() for _, x in fr], concept=c["display"])
                for pk, msid in c["modify"]:
                    if pk not in om["props"]:
                        self.errors.add("CAPABILITY_RULE_CONFLICT", f"{c['display']}: {pk} can be modified but is not included in the restriction",
                                        None, "surface", sentences=[self._origin(om["sn"]), self._origin(msid)], concept=c["display"], prop=pk)
                for pk in om["props"]:
                    if pk in c["props"] and pk not in {m[0] for m in c["modify"]}:
                        self.errors.add("MISSING_CAPABILITY", f"the rule permits modifying {pk} of {c['display']}, but no modification capability is declared",
                                        None, "surface", concept=k, kind="modify")
            if c["register"] and c["modify"] and c["immutable"]:
                self.errors.add("CAPABILITY_RULE_CONFLICT", f"{c['display']}: a modification capability conflicts with the rule "
                                "'cannot be modified'", None, "surface",
                                sentences=[self._origin(c["immutable"])] + [self._origin(x) for _, x in c["modify"]], concept=c["display"])
            for pk, msid in c["modify"]:
                om = c["only_mod"]
                if om and om[0] != pk:
                    self.errors.add("CAPABILITY_RULE_CONFLICT", f"{c['display']}: {pk} can be modified but the rule only "
                                    f"allows modifying {om[0]}", None, "surface",
                                    sentences=[self._origin(om[1]), self._origin(msid)], concept=c["display"], prop=pk, allowed=om[0])
            if c["numbering"]:
                n = c["numbering"]
                if not any(r["target"] == n["owner"] for r in c["refs"]):
                    self.incomplete.append({"status": "INCOMPLETE_CAPABILITY", "capability": f"numbering for {c['display']}",
                                            "missing": [f"relationship between each {c['display']} and its {self.concepts[n['owner']]['display']} "
                                                        "(required form: 'Each … belongs to exactly one registered …')"]})
            if c["artifact"]:
                a = c["artifact"]
                if not a["identity"]:
                    self.ambiguities.append({"id": f"AMBS_artifact_{slug(a['name'])}", "kind": "AMBIGUOUS_MODEL",
                                             "reason": f"the identity of the {a['name']} file for each {c['display']} is not specified",
                                             "candidates": ["same identifier as the " + c["display"], "its own identifier"],
                                             "status": "blocking"})
                    self.errors.add("AMBIGUOUS_MODEL", f"a decision is needed about the identity of the {a['name']} file "
                                    f"(for example: decision DD_x \"The {a['name']} file uses the same identifier as the "
                                    f"{c['display']}.\")", None, "surface")
                if not a["content_input"]:
                    self.incomplete.append({"status": "INCOMPLETE_CAPABILITY", "capability": f"Register {c['display']}",
                                            "missing": [f"source of the {a['name']} file content"]})
        inv = [c["display"] for c in self.concepts.values()
               if c["refs"] or c["min"] or c.get("max") or c["required"] or c["numbering"] or c["capacity"] or c["count_max"] or c["combos"] or c["artifact"]]
        if inv and not any(r["p"]["id"] == "INITIAL" for r in self.rules):
            self.incomplete.append({"status": "INCOMPLETE_CAPABILITY", "capability": "initial state",
                                    "missing": ["what exists initially (without this, rules cannot be shown to hold from the "
                                                f"start; this affects {sorted(set(inv))}). Example: initial {{ \"Initially, there are no …\" }}"]})
        elif inv:
            missing_concepts = sorted(c["display"] for k, c in self.concepts.items()
                           if k not in self.initial_concepts and (c["register"] or c["display"] in inv))
            if missing_concepts:
                self.incomplete.append({"status": "INCOMPLETE_CAPABILITY", "capability": "initial state",
                                        "missing": [f"what exists initially for {', '.join(missing_concepts)}"],
                                        "suggest": [f"Initially, there are no {' or '.join(plural(x) for x in missing_concepts)}."]})
        for c in self.concepts.values():
            if c["immutable"]:
                self.ambiguities.append({"id": f"AMBS_class_{c['core']}", "kind": "AMBIGUOUS_MODEL",
                                         "reason": f"{c['display']} could be modeled as an entity or as a durable result "
                                                   "(an immutable record)",
                                         "candidates": ["ENTITY", "DURABLE_RESULT"], "status": "pending_human"})
        # A decision can resolve these ambiguities: resolves AMBS_x = ENTITY
        res = {d["resolves"]: d for d in self.s.decisions if d["resolves"]}
        for a in self.ambiguities:
            if a["id"] in res:
                a["status"] = "resolved"
                a["resolved_by"] = res[a["id"]]["id"]
                a["chosen"] = res[a["id"]]["chooses"]

    # ---- step 3: capabilities and outcomes
    def capabilities_and_outcomes(self):
        decl = []
        for r in self.rules:
            if r["sn"].section == "outcomes":
                decl.append(r)
        self.outcomes_decl = decl
        caps = []
        for k, c in self.concepts.items():
            if c["register"]:
                cases = []
                key = (c["identity"] or (None,))[0] if c["identity"] else "identifier"
                if not c["id_generated"]:
                    cases.append({"kind": "DUPLICATE", "arg": key, "label": agree(f"the {self._pdisp(c, key)} already exists")})
                for pk, (n, _) in sorted(c["min"].items()):
                    cases.append({"kind": "BELOW_MIN", "arg": pk, "n": n, "label": f"the {self._pdisp(c, pk)} is less than {n}"})
                for pk, (n, _) in sorted(c["max"].items()):
                    cases.append({"kind": "ABOVE_MAX", "arg": pk, "n": n, "label": agree(f"the {self._pdisp(c, pk)} is greater than {n}")})
                for pk in sorted(c["required"]):
                    if (c["identity"] and pk == c["identity"][0]) or c["props"][pk]["type"] != "Text":
                        continue  # a number always has a value in the Core; the type guarantees it is required
                    cases.append({"kind": "NOT_INFORMED", "arg": pk, "label": agree(f"the {self._pdisp(c, pk)} is missing")})
                for rf in c["refs"]:
                    cases.append({"kind": "REF_MISSING", "arg": rf["target"], "label": agree(f"the {self.concepts[rf['target']]['display']} is not registered")})
                for cb in c["combos"]:
                    sc = f" not in state {cb['scope']}" if cb.get("scope") else ""
                    cases.append({"kind": "COMBO_DUP", "arg": tuple(combo_ps(cb)), "scope": cb.get("scope"), "scope_list": scope_list(cb),
                                  "label": agree(f"{article(c['display'])} {c['display']}{sc} already exists with {combo_phrase(cb)}")})
                for cm in c["count_max"]:
                    nw = count_new_scope(c, cm)
                    if nw is not False:
                        cases.append({"kind": "COUNT_FULL", "arg": cm["owner"], "n": cm["n"], "scope_list": cm["scope_list"], "new": nw,
                                      "label": agree(f"the {self.concepts[cm['owner']]['display']} already has {cm['n']} {plural(c['display'])}{count_scope_txt(cm)}")})
                if c["capacity"]:
                    cp = c["capacity"]
                    cases.append({"kind": "CAPACITY", "arg": cp["owner"], "prop": cp["prop"],
                                  "label": agree(f"the {self.concepts[cp['owner']]['display']} has no {cp['prop']} available")})
                if c["combo_identity"]:
                    cases[0] = {"kind": "DUPLICATE", "arg": "key", "label": "the technical key is already in use", "technical": True,
                                "outcome": {"name": "TechnicalKeyInUse", "display": "TechnicalKeyInUse", "effect": "no information changes",
                                            "sentence": None, "technical": True}}
                caps.append({"id": f"Register {c['display']}", "kind": "register", "concept": k, "cases": cases})
            for pk, sid in c["modify"]:
                cases = [{"kind": "REF_MISSING", "arg": k, "label": agree(f"the {c['display']} is not registered")}]
                if pk in c["required"] and c["props"][pk]["type"] == "Text":
                    cases.append({"kind": "NOT_INFORMED", "arg": pk, "label": agree(f"the {self._pdisp(c, pk)} is missing")})
                if pk in c["min"]:
                    cases.append({"kind": "BELOW_MIN", "arg": pk, "n": c["min"][pk][0], "label": agree(f"the {self._pdisp(c, pk)} is too small")})
                if pk in c["max"]:
                    cases.append({"kind": "ABOVE_MAX", "arg": pk, "n": c["max"][pk][0], "label": agree(f"the {self._pdisp(c, pk)} is too large")})
                for ek, e in self.concepts.items():
                    if e["capacity"] and e["capacity"]["owner"] == k and e["capacity"]["prop"] == pk:
                        cases.append({"kind": "BELOW_COUNT", "arg": ek, "prop": pk,
                                      "label": f"the new {self._pdisp(c, pk)} is less than the number of {plural(e['display'])} for the {c['display']}"})
                caps.append({"id": f"Modify {c['props'][pk]['display']} of {c['display']}", "kind": "modify", "concept": k,
                             "prop": pk, "cases": cases})
            for to, frm in ((c["states"] or {}).get("trans") or {}).items():
                srcs = list(dict.fromkeys(a_ for a_, _ in frm))
                cases = [{"kind": "REF_MISSING", "arg": k, "label": agree(f"the {c['display']} is not registered")},
                         {"kind": "WRONG_STATE", "arg": tuple(srcs), "to": to, "label": agree(f"the {c['display']} is not in state {' or '.join(srcs)}")}]
                for cb in c["combos"]:
                    # Leaving a state excluded from uniqueness and entering an included state may repeat the combination
                    if cb.get("scope") and any(s_ in srcs for s_ in scope_list(cb)) and to not in scope_list(cb):
                        cases.append({"kind": "COMBO_DUP", "arg": tuple(combo_ps(cb)), "scope": cb["scope"], "scope_list": scope_list(cb), "transition": True,
                                      "label": agree(f"a {c['display']} not in state {cb['scope']} already exists with {combo_phrase(cb)}")})
                for cm in c["count_max"]:
                    # Moving from an excluded state into an included state can exceed the bound.
                    if cm["scope_list"] and to in cm["scope_list"] and any(s_ not in cm["scope_list"] for s_ in srcs):
                        cases.append({"kind": "COUNT_FULL", "arg": cm["owner"], "n": cm["n"], "scope_list": cm["scope_list"], "transition": True,
                                      "label": agree(f"the {self.concepts[cm['owner']]['display']} already has {cm['n']} {plural(c['display'])}{count_scope_txt(cm)}")})
                caps.append({"id": f"Transition {c['display']} to {to}", "kind": "transition", "concept": k, "to": to, "from": srcs,
                             "src_sn": frm[0][1], "cases": cases})
        # assign declared outcomes
        for cap in caps:
            mine = [r for r in decl if norm_s(r["sn"].capability or "") == norm_s(cap["id"])]
            cap["declared"] = [r["sn"].id for r in mine]
            used = set()
            for case in cap["cases"]:
                if case.get("technical"):
                    continue
                hit = None
                for r in mine:
                    if self._outcome_matches(case, r, cap):
                        hit = r
                        break
                if hit:
                    case["outcome"] = {"name": ascii_(hit["g"]["n"]), "display": hit["g"]["n"], "effect": hit["g"]["eff"],
                                       "sentence": hit["sn"].id}
                    used.add(hit["sn"].id)
            ok = [r for r in mine if r["p"]["id"] in ("OUT_SUCCESS_REG", "OUT_SUCCESS_MOD")
                  or (r["p"]["id"] == "OUT_SUCCESS_STATE" and cap["kind"] == "transition" and norm_s(r["g"]["s"]) == norm_s(cap["to"]))]
            if ok:
                used.add(ok[0]["sn"].id)
                cap["success_sentence"] = ok[0]["sn"].id
            cap["success_name"] = camel(cap["id"]) + "Ok"
            missing = [f"outcome when {c['label']}" for c in cap["cases"] if "outcome" not in c]
            suggest = [self.suggest(cap, c) for c in cap["cases"] if "outcome" not in c]
            if "success_sentence" not in cap:
                missing.append("outcome for the normal case ('Otherwise, …')")
                suggest.append(self.suggest(cap, None))
            if missing:
                self.incomplete.append({"status": "INCOMPLETE_CAPABILITY", "capability": cap["id"], "missing": missing,
                                        "suggest": suggest})
            for r in mine:
                if r["sn"].id in used:
                    continue
                twin = next((cs for cs in cap["cases"] if "outcome" in cs and self._outcome_matches(cs, r, cap)), None)
                if twin is None and r["p"]["id"] in ("OUT_SUCCESS_REG", "OUT_SUCCESS_MOD", "OUT_SUCCESS_STATE") and ok:
                    twin = {"outcome": {"name": None, "sentence": ok[0]["sn"].id}}
                if twin is not None:
                    first = next((s.text for s in self.s.sentences if s.id == twin["outcome"]["sentence"]), "")
                    if twin["outcome"]["name"] in (None, ascii_(r["g"].get("n") or "")):
                        self.warnings.append({"warning": "REDUNDANT_OUTCOME", "capability": cap["id"], "sentence": r["sn"].text})
                    else:
                        self.errors.add("UNMATCHED_OUTCOME", f"«{r['sn'].text}» and «{first}» declare the same case for «{cap['id']}» with "
                                        "different results", None, "surface", capability=cap["id"], sentence=r["sn"].text,
                                        twin=first, inherited=r["sn"].id in self.s.inherited, cases=[agree(x["label"]) for x in cap["cases"]])
                    continue
                if True:
                    # v14: an analyst sentence that does not become a guarantee is not silently discarded
                    self.errors.add("UNMATCHED_OUTCOME", f"«{r['sn'].text}» does not match any case for «{cap['id']}»", None, "surface",
                                    capability=cap["id"], sentence=r["sn"].text, inherited=r["sn"].id in self.s.inherited,
                                    cases=[agree(x["label"]) for x in cap["cases"]])
        known = {norm_s(c["id"]) for c in caps}
        for r in decl:
            if norm_s(r["sn"].capability or "") not in known:
                self.errors.add("OUTCOMES_FOR_UNKNOWN_CAPABILITY", f"'outcomes for {r['sn'].capability!r}': capability not found "
                                f"(available: {[c['id'] for c in caps]})", (r["sn"].line, 1), "surface")
        for inc in self.incomplete:
            self.errors.add("INCOMPLETE_CAPABILITY", f"{inc['capability']}: missing " + "; ".join(inc["missing"]), None, "surface")
        self.capabilities = caps

    def suggest(self, cap, case) -> str:
        """Return a catalog-valid sentence that closes the case; the outcome name is only a suggestion."""
        concept = self.concepts[cap["concept"]]
        name = concept["display"]
        if case is None:
            if cap["kind"] == "transition":
                return f"Otherwise, the {name} ends up in state {cap['to']}."
            if cap["kind"] == "register":
                return f"Otherwise, the {name} is registered."
            prop = self._pdisp(concept, cap["prop"])
            return f"Otherwise, the {prop} of the {name} is modified."
        kind, unchanged = case["kind"], "and no information changes."
        if kind == "DUPLICATE":
            prop = self._pdisp(concept, case["arg"])
            return f"If the {prop} already belongs to a {name}, the result is {camel(concept['key'])}Exists {unchanged}"
        if kind == "REF_MISSING":
            target = self.concepts[case["arg"]]["display"]
            return f"If the {target} is not registered, the result is {camel(norm_s(target))}Missing and the {target} is not registered."
        if kind == "NOT_INFORMED":
            prop = self._pdisp(concept, case["arg"])
            return f"If the {prop} is missing, the result is {camel(case['arg'])}Required {unchanged}"
        if kind == "BELOW_MIN":
            prop = self._pdisp(concept, case["arg"])
            return f"If the {prop} is less than {case['n']}, the result is {camel(case['arg'])}TooSmall {unchanged}"
        if kind == "ABOVE_MAX":
            prop = self._pdisp(concept, case["arg"])
            return f"If the {prop} is greater than {case['n']}, the result is {camel(case['arg'])}TooLarge {unchanged}"
        if kind == "COMBO_DUP":
            scope = f" not in state {case['scope']}" if case.get("scope") else ""
            return f"If a {name}{scope} already exists with {combo_phrase(case['arg'])}, the result is {camel(concept['key'])}Exists {unchanged}"
        if kind == "WRONG_STATE":
            return f"If the {name} is not in state {' or '.join(case['arg'])}, the result is {camel(concept['key'])}WrongState {unchanged}"
        if kind == "COUNT_FULL":
            owner = self.concepts[case["arg"]]["display"]
            return f"If the {owner} already has {case['n']} {plural(name)}{count_scope_txt(case)}, the result is {camel(norm_s(owner))}AtCapacity {unchanged}"
        if kind == "CAPACITY":
            owner = self.concepts[case["arg"]]["display"]
            return f"If the {owner} has no {case['prop'].replace('_', ' ')} available, the result is {camel(norm_s(owner))}AtCapacity {unchanged}"
        if kind == "BELOW_COUNT":
            owner = self.concepts[case["arg"]]["display"]
            prop = case["prop"]
            return f"If the new {prop} is less than the number of {plural(owner)} for the {name}, the result is {camel(prop)}TooSmall {unchanged}"
        return ""

    @staticmethod
    def _pdisp(c, pk):
        return c["props"][pk]["display"] if pk in c["props"] else pk

    def _outcome_matches(self, case, r, cap) -> bool:
        pid, g = r["p"]["id"], r["g"]
        c = self.concepts[cap["concept"]]
        if case["kind"] == "DUPLICATE" and pid == "OUT_DUPLICATE":
            return slug(g["p"]) == case["arg"] and norm_s(g["c"]) == c["key"]
        if case["kind"] == "NOT_INFORMED" and pid == "OUT_NOT_INFORMED":
            return slug(g["p"]) == case["arg"]
        if case["kind"] == "REF_MISSING" and pid == "OUT_REF_MISSING":
            return norm_s(g["c"]) == case["arg"]
        if case["kind"] == "COMBO_DUP" and pid == "OUT_COMBO":
            return (sorted(slug(x) for x in (g.get("o"), g["p1"], g.get("p2")) if x) == sorted(case["arg"]) and norm_s(g["c"]) == c["key"]
                    and norm_s(g.get("sx") or "") == norm_s(case.get("scope") or ""))
        if case["kind"] == "WRONG_STATE" and pid == "OUT_WRONG_STATE":
            ss = {norm_s(x) for x in re.split(r"\s*(?:,|\bor\b|\band\b)\s*", g["ss"]) if x.strip()}
            return norm_s(g["c"]) == c["key"] and ss == {norm_s(x) for x in case["arg"]}
        if case["kind"] == "COUNT_FULL" and pid == "OUT_COUNT":
            xs = [norm_s(x) for x in re.split(r"\s+or\s+", g["sx"].strip())] if g.get("sx") else []
            return (norm_s(g["o"]) == case["arg"] and int(g["k"]) == case["n"] and self.concept(g["cs"], plural=True) == cap["concept"]
                    and sorted(xs) == sorted(norm_s(x) for x in case["scope_list"]))
        if case["kind"] == "CAPACITY" and pid == "OUT_CAPACITY":
            return norm_s(g["o"]) == case["arg"] and slug(g["p"]) == case["prop"]
        if case["kind"] == "BELOW_COUNT" and pid == "OUT_BELOW_COUNT":
            return slug(g["p"]) == case["prop"] and self.concept(g["cs"], plural=True) == case["arg"] and norm_s(g["o"]) == c["key"]
        if case["kind"] == "BELOW_MIN" and pid == "OUT_BELOW_MIN":
            return slug(g["p"]) == case["arg"] and int(g["k"]) == case["n"]
        if case["kind"] == "ABOVE_MAX" and pid == "OUT_ABOVE_MAX":
            return slug(g["p"]) == case["arg"] and int(g["k"]) == case["n"]
        return False


 # --------------------------------------------------------------------------- 4. Core generation
def article(word: str) -> str:
    """Return the indefinite article used before an English word."""
    return "an" if word[:1].lower() in "aeiou" else "a"


def combo_ps(cb):
    """v20: a unique combination has one or two properties (for example, “the same bicycle” or “the same member and class”)."""
    return [p for p in ((cb.get("owner"), cb["p1"], cb["p2"]) if isinstance(cb, dict) else cb) if p]


def count_scope_txt(cm) -> str:
    return f" in state {' or '.join(cm['scope_list'])}" if cm.get("scope_list") else ""


def count_new_scope(c, cm):
    """v26: Does a new registration count toward the bound? Without a scope, always. With a scope, a fixed initial state
    outside the scope means the new record cannot exceed the bound; a threshold-dependent initial state yields a condition."""
    if not cm["scope_list"]:
        return None
    ini = (c["states"] or {}).get("initial")
    if not ini:
        return None
    if ini["kind"] == "fixed":
        return None if ini["a"] in cm["scope_list"] else False
    a_in, b_in = ini["a"] in cm["scope_list"], ini["b"] in cm["scope_list"]
    if a_in and b_in:
        return None
    if not a_in and not b_in:
        return False
    return f"{ini['p']} <= {ini['n']}" if a_in else f"{ini['p']} > {ini['n']}"


def scope_list(x) -> list:
    """States excluded from a scoped uniqueness rule (v24: there may be more than one)."""
    return list(x.get("scope_list") or ([x["scope"]] if x.get("scope") else []))


def combo_phrase(cb):
    # v23: generated sentences must match the catalog, which does not permit underscores in names
    return " and ".join(f"the same {p.replace('_', ' ')}" for p in combo_ps(cb))


def same_phrase(w: str) -> str:
    return f"the same {w}"


def agree(text: str) -> str:
    """Retained as a compatibility helper; English nouns do not use grammatical gender agreement."""
    return text


def q(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _off(expr: str, n: int) -> str:
    return expr if n == 0 else f"{expr} {'+' if n > 0 else '-'} {abs(n)}"


class CoreGen:
    def __init__(self, an: Analysis):
        self.a = an
        self.props: list[dict] = []     # {section, kind, id, rule, guarantee, source, text, expr}
        self.prov: dict[str, dict] = {}
        self.required: dict[str, list[str]] = {}   # base_id -> required guarantees
        self.lines: list[str] = []
        self.realization: list[dict] = []
        self.sent = {s.id: s for s in an.s.sentences}
        self.writes: dict[str, list] = {}
        self.success: dict[str, list] = {}   # operation -> success postcondition equalities

    def bus(self, c) -> list[str]:
        """Business attributes of a concept, excluding its key and implementation details."""
        key = c["identity"][0] if c["identity"] else None
        out = [p for p in c["props"] if p != key]
        out += [slug(self.a.concepts[r["target"]]["display"]) for r in c["refs"]]
        if c["numbering"]:
            out.append("number")
        if c["states"]:
            out.append("state")
        return out

    @staticmethod
    def op_name(cap, c):
        if cap["kind"] == "register":
            return f"Register{c['core']}"
        if cap["kind"] == "transition":
            return f"Transition{c['core']}To{camel(norm_s(cap['to']))}"
        return f"Modify{camel(cap['prop'])}Of{c['core']}"

    def op_transition(self, c, cap):
        E, K = c["core"], self.key_of(c)
        op = self.op_name(cap, c)
        L = [f"operation {op}({K}: Identifier) {{"]
        self.writes[op] = [(E, "state", K)]
        self.success[op] = [f"{E}.state[{K}] == {self.sv(E, cap['to'])}"]
        for case in cap["cases"]:
            L.append(f'  when {self.cond(c, case, modify=True)} outcome {case["outcome"]["name"]} {{ }}')
        L.append(f"  otherwise {{\n    set {E}.state[{K}] = {self.sv(E, cap['to'])}\n  }}\n}}\n")
        return L

    @staticmethod
    def sv(E, s):
        return f"State{E}.{camel(norm_s(s))}"

    def cnt(self, a):
        return f"cnt_{slug(self.a.concepts[a]['display'])}"

    def seq(self, owner, a):
        return f"seq_{slug(self.a.concepts[a]['display'])}"

    def prop(self, group, kind, base, guar, sid, text, expr, origin="literal", extra=None, step=None):
        pid = base + (f"__{guar}" if guar else "")
        text = agree(text)
        self.props.append({"group": group, "kind": kind, "id": pid, "base": base, "guarantee": guar, "sn": sid, "text": text,
                           "expr": expr, "origin": origin, "targets": extra or []})
        self.prov[pid] = {"classification": "EXPLICIT_RULE" if origin == "literal" else "DERIVED_RULE",
                          "source": {"sentence_id": sid, "phrase": self.sent[sid].text if sid in self.sent else None},
                          "pattern": step[0] if step else None, "inference_steps": step[1] if step else []}

    def generate(self) -> str:
        a = self.a
        C = a.concepts
        L = [f'spec {a.s.name} version {q(a.s.version)}', ""]
        for d in a.s.decisions:
            line = f'decision {d["id"]} {q(d["text"])}'
            if d["alternatives"]:
                line += f' alternatives {q(d["alternatives"])}'
            if d["resolves"] and d["resolves"].startswith("AMB_"):
                line += f' resolves {d["resolves"]} = {d["chooses"]}'
            L.append(line)
        for k, c in C.items():
            if c["states"] and c["states"]["values"]:
                L.append(f'enum State{c["core"]} {{ {", ".join(camel(norm_s(v)) for v in c["states"]["values"])} }}')
        for s in a.s.assumptions:
            L.append(f'assumption {s["id"]} {q(s["text"])} owner {q(s["owner"] or "undeclared")}')
        for s in a.s.questions:
            L.append(f'question {s["id"]} {q(s["text"])}')
        for g in a.s.gaps:
            L.append(f'language_gap {g["id"]} {q(g["text"])}')
        if any(c["capacity"] for c in C.values()):
            L.append('decision DD_AUTO_COUNT "A per-owner counter and an assigned position implement the quantity bound; this is an implementation detail" '
                     'alternatives "count on demand; persisted counter with an assigned slot"')
        if any(c.get("combo_identity") for c in C.values()):
            L.append('decision DD_AUTO_KEY "The caller supplies the technical key for a concept identified by a combination; a key already in use is a technical outcome (TechnicalKeyInUse)" '
                     'alternatives "caller-supplied key; key generated by the implementation"')
        needs_seq = [(k, c) for k, c in C.items() if c["numbering"]]
        if needs_seq:
            L.append('decision DD_AUTO_SEQ "consecutive numbering uses a counter per owner; this is an implementation detail, not business data" '
                     'alternatives "maximum existing number + 1; transactional sequence; persisted counter"')
        init_sid = getattr(a, "initial_sn", None)
        if init_sid:
            L.append(f'assumption AS_INITIAL {q(" ".join(self.sent[x].text for x in a.initial_sns))} owner "business (initial block)"')
        L.append("")
        if init_sid:
            names = [C[k]["core"] for k in a.initial_concepts]
            names += [f"Counter{C[k]['core']}" for k in a.initial_concepts if C[k]["owner_of"] or C[k]["cap_of"]]
            files = any(C[k]["artifact"] for k in a.initial_concepts)
            cl = " and ".join(f"(forall k: not (k in {n}))" for n in names + (["File"] if files else []))
            L.append(f"initial origin assumption(AS_INITIAL): {cl}")
            L.append("")
        for k, c in C.items():
            if c["register"]:
                L += self.op_register(c)
            for pk, sid in c["modify"]:
                L += self.op_modify(c, pk)
            for cap in a.capabilities:
                if cap["kind"] == "transition" and cap["concept"] == k:
                    L += self.op_transition(c, cap)
        self.rules()
        self.outcome_obligations()
        self.success_obligations()
        tf = self.realization_facts()
        if tf:
            L += ["facts {"] + tf + ["}"]
        L.append("business_rules {")
        L += [self.emit(p) for p in self.props if p["group"] == "business"]
        L.append("}")
        L.append("technical_rules {")
        L += [self.emit(p) for p in self.props if p["group"] == "technical"]
        L.append("}")
        return "\n".join(L) + "\n"

    def realization_facts(self):
        """Implementation counters use Natural values by construction. Declare them as facts so their type does not depend
        on which operations use them: otherwise an increment comparing a counter with a Natural would change the inferred type
        of an inherited attribute and the test cache would stop recognizing base obligations (v09)."""
        C, out = self.a.concepts, []
        for k, c in C.items():
            if c["capacity"]:
                O, o = C[c["capacity"]["owner"]]["core"], slug(C[c["capacity"]["owner"]]["display"])
                out.append(f"  fact TF_CNT_{O}_{c['core']} origin derived(DD_AUTO_COUNT): every Counter{O} has exactly_one {self.cnt(c['key'])}: Natural")
                out.append(f"  fact TF_ORD_{c['core']}_{o} origin derived(DD_AUTO_COUNT): every {c['core']} has exactly_one position_{o}: Natural")
            if c["numbering"]:
                O = C[c["numbering"]["owner"]]["core"]
                out.append(f"  fact TF_SEQ_{O}_{c['core']} origin derived(DD_AUTO_SEQ): every Counter{O} has exactly_one {self.seq(C[c['numbering']['owner']], c['key'])}: Natural")
        return out

    def emit(self, p) -> str:
        o = f'literal({q(self.sent[p["sn"]].text)})' if p["origin"] == "literal" else f'derived({p["origin"]})'
        tg = f' for {", ".join(p["targets"])}' if p["targets"] else ""
        return f'  {p["kind"]} {p["id"]}{tg} origin {o} {q(p["text"])}:\n    {p["expr"]}'

    # ---- operations
    def conds(self, c, cap):
        out = []
        for case in cap["cases"]:
            out.append(case)
        return out

    def key_of(self, c):
        return c["identity"][0] if c["identity"] else ("key" if c.get("combo_identity") else "identifier")

    def params(self, c):
        ps = [(self.key_of(c), f"Identifier generated for {c['core']}" if c["id_generated"] else "Identifier")]
        for p, e in c["props"].items():
            if c["identity"] and p == c["identity"][0]:
                continue
            ps.append((p, e["type"]))
        for r in c["refs"]:
            ps.append((slug(self.a.concepts[r["target"]]["display"]), "Identifier"))
        if c["artifact"]:
            ps.append((f"content_{slug(c['artifact']['name'])}", "Text"))
        return ps

    def op_register(self, c):
        cap = next(x for x in self.a.capabilities if x["kind"] == "register" and x["concept"] == c["key"])
        ps = self.params(c)
        L = [f'operation Register{c["core"]}({", ".join(f"{n}: {t}" for n, t in ps)}) {{']
        E = c["core"]
        for case in cap["cases"]:
            L.append(f'  when {self.cond(c, case)} outcome {case["outcome"]["name"]} {{ }}')
        L.append("  otherwise {")
        inits = [f"{p} = {p}" for p in c["props"] if not (c["identity"] and p == c["identity"][0])]
        ini = (c["states"] or {}).get("initial")
        E0 = c["core"]
        if ini:
            inits.append(f"state = {self.sv(E0, ini['a'])}" if ini["kind"] == "fixed" else
                         f"state = if {ini['p']} <= {ini['n']} then {self.sv(E0, ini['a'])} else {self.sv(E0, ini['b'])}")
        inits += [f"{slug(self.a.concepts[r['target']]['display'])} = {slug(self.a.concepts[r['target']]['display'])}" for r in c["refs"]]
        post = []
        if c["numbering"]:
            n = c["numbering"]
            o = self.a.concepts[n["owner"]]
            ref = slug(o["display"])
            sq = f"Counter{o['core']}.{self.seq(o, c['key'])}[{ref}]"
            inits.append(f"number = {_off(sq, n['start'])}")
            post.append(f"set {sq.split('[')[0]}[{ref}] = {sq} + 1")
        tech = [f"{self.seq(c, oc)} = 0" for oc in c["owner_of"]]
        if c["capacity"]:
            cp = c["capacity"]
            o = self.a.concepts[cp["owner"]]
            ref = slug(o["display"])
            ca = f"Counter{o['core']}.{self.cnt(c['key'])}"
            inits.append(f"position_{ref} = {ca}[{ref}] + 1")
            post.append(f"set {ca}[{ref}] = {ca}[{ref}] + 1")
        tech += [f"{self.cnt(oc)} = 0" for oc in c["cap_of"]]
        K = self.key_of(c)
        w = [(E, None, K)] + [(E, i.split(" = ")[0], K) for i in inits]
        for x in post:
            loc = x[4:].split(" = ")[0]
            ent, rest = loc.split(".", 1)
            w.append((ent, rest.split("[")[0], rest.split("[", 1)[1][:-1]))
        if tech:
            w.append((f"Counter{E}", None, K))
            w += [(f"Counter{E}", t_.split(" = ")[0], K) for t_ in tech]
        if c["artifact"]:
            w.append(("File", None, K))
        self.writes[f"Register{E}"] = w
        eqs = [f"{E}.{n}[{K}] == (" + re.sub(r"(Counter\w+\.\w+\[\w+\])", r"old(\1)", v) + ")"
               for n, v in (i.split(" = ", 1) for i in inits)]
        eqs += [f"Counter{E}.{n}[{K}] == {v}" for n, v in (i.split(" = ", 1) for i in tech)]
        for x in post:
            loc = x[4:].split(" = ")[0]
            eqs.append(f"{loc} == old({loc}) + 1")
        eqs.append(f"{K} in {E}")
        if c["artifact"]:
            eqs.append(f"{K} in File")
        self.success[f"Register{E}"] = eqs
        L.append(f'    create {E}[{self.key_of(c)}] {{ {", ".join(inits)} }}' if inits else f"    create {E}[{self.key_of(c)}]")
        if tech:
            L.append(f'    create Counter{E}[{K}] {{ {", ".join(tech)} }}')
        L += [f"    {x}" for x in post]
        if c["artifact"]:
            L.append(f'    write_file {self.key_of(c)} = content_{slug(c["artifact"]["name"])}')
        L += ["  }", "}", ""]
        return L

    def op_modify(self, c, pk):
        cap = next(x for x in self.a.capabilities if x["kind"] == "modify" and x["concept"] == c["key"] and x["prop"] == pk)
        t = c["props"][pk]["type"]
        L = [f'operation Modify{camel(pk)}Of{c["core"]}({self.key_of(c)}: Identifier, {pk}: {t}) {{']
        self.writes[f'Modify{camel(pk)}Of{c["core"]}'] = [(c["core"], pk, self.key_of(c))]
        self.success[f'Modify{camel(pk)}Of{c["core"]}'] = [f"{c['core']}.{pk}[{self.key_of(c)}] == {pk}"]
        for case in cap["cases"]:
            L.append(f'  when {self.cond(c, case, modify=True, pk=pk)} outcome {case["outcome"]["name"]} {{ }}')
        L.append(f"  otherwise {{\n    set {c['core']}.{pk}[{self.key_of(c)}] = {pk}\n  }}\n}}\n")
        return L

    def cond(self, c, case, modify=False, pk=None):
        k = case["kind"]
        if k == "DUPLICATE":
            return f"{self.key_of(c)} in {c['core']}"
        if k == "REF_MISSING":
            tgt = self.a.concepts[case["arg"]]
            ref = self.key_of(c) if modify else slug(tgt["display"])
            return f"not ({ref} in {tgt['core']})"
        if k == "NOT_INFORMED":
            return f'{case["arg"]} == ""'
        if k == "BELOW_MIN":
            return f'{case["arg"]} < {case["n"]}'
        if k == "ABOVE_MAX":
            return f'{case["arg"]} > {case["n"]}'
        if k == "WRONG_STATE":
            return "not (" + " or ".join(f"{c['core']}.state[{self.key_of(c)}] == {self.sv(c['core'], s)}" for s in case["arg"]) + ")"
        if k == "COMBO_DUP":
            E = c["core"]
            sc = [f"{E}.state[w] != {self.sv(E, s_)}" for s_ in scope_list(case)]
            if case.get("transition"):
                K = self.key_of(c)
                return f"exists w in {E}: " + " and ".join([f"w != {K}"] + sc + [f"{E}.{p}[w] == {E}.{p}[{K}]" for p in case["arg"]])
            return f"exists w in {E}: " + " and ".join(sc + [f"{E}.{p}[w] == {p}" for p in case["arg"]])
        if k == "COUNT_FULL":
            E, n = c["core"], case["n"]
            o = slug(self.a.concepts[case["arg"]]["display"])
            ws = [f"w{i}" for i in range(1, n + 1)]
            K = self.key_of(c)
            own = f"{E}.{o}[{K}]" if case.get("transition") else o
            parts = [f"{a_} != {b_}" for i, a_ in enumerate(ws) for b_ in ws[i + 1:]]
            for w in ws:
                if case.get("transition"):
                    parts.append(f"{w} != {K}")
                parts.append(f"{E}.{o}[{w}] == {own}")
                if case["scope_list"]:
                    parts.append("(" + " or ".join(f"{E}.state[{w}] == {self.sv(E, s_)}" for s_ in case["scope_list"]) + ")")
            if case.get("new"):
                parts.append(case["new"])
            return "".join(f"exists {w} in {E}: " for w in ws) + " and ".join(parts)
        if k == "BELOW_COUNT":
            return f"{pk} < Counter{c['core']}.{self.cnt(case['arg'])}[{self.key_of(c)}]"
        if k == "CAPACITY":
            o = self.a.concepts[case["arg"]]
            ref = slug(o["display"])
            return f"Counter{o['core']}.{self.cnt(c['key'])}[{ref}] >= {o['core']}.{case['prop']}[{ref}]"
        raise AssertionError(k)

    # ---- rules
    def rules(self):
        a, C = self.a, self.a.concepts
        for k, c in C.items():
            E, v = c["core"], "v"
            for pk in sorted(c["required"]):
                if c["identity"] and pk == c["identity"][0]:
                    continue
                if c["props"][pk]["type"] != "Text":
                    continue  # v15: an empty-string check on a Natural is invalid; the total type makes numbers required
                sid = next(r["sn"].id for r in a.rules if r["p"]["id"] == "REQUIRED" and slug(r["g"]["p"]) == pk)
                self.prop("business", "invariant", f"BR_REQ_{E}_{pk}", None, sid, f"every {c['display']} has {c['props'][pk]['display']}",
                          f'forall v: (v in {E}) => {E}.{pk}[v] != ""',
                          step=("REQUIRED", [f"property {pk} belongs to {E} by HAS_PROPS", "required -> non-empty invariant"]))
            if c["no_delete"]:
                self.prop("business", "law", f"BR_KEPT_{E}", None, c["no_delete"], f"a registered {c['display']} remains registered",
                          f"forall v: old(v in {E}) => v in {E}", step=("NO_DELETE", ["temporal preservation of existence"]))
            if c["immutable"]:
                attrs = self.bus(c)
                eq = " and ".join(f"{E}.{x}[v] == old({E}.{x}[v])" for x in attrs) or f"v in {E}"
                self.prop("business", "law", f"BR_IMMUTABLE_{E}", "preserves", c["immutable"],
                          f"a registered {c['display']} cannot be modified", f"forall v: old(v in {E}) => ({eq})",
                          step=("IMMUTABLE", [f"equality of all current business attributes of {E}: {attrs}"]))
                self.required[f"BR_IMMUTABLE_{E}"] = [f"preserves:{x}" for x in attrs]
            if c["only_mods"]:
                om = c["only_mods"]
                free = set(om["props"]) | ({"state"} if om["state"] else set())
                attrs = [x for x in self.bus(c) if x not in free]
                eq = " and ".join(f"{E}.{x}[v] == old({E}.{x}[v])" for x in attrs) or f"v in {E}"
                shown = [c["props"][x]["display"] for x in om["props"]] + (["state"] if om["state"] else [])
                self.prop("business", "law", f"BR_ONLYMOD_{E}", "preserves", om["sn"],
                          f"only {', '.join(shown)} can change on a registered {c['display']}",
                          f"forall v: old(v in {E}) => ({eq})",
                          step=("ONLY_MODIFIABLE_LIST", [f"business attributes of {E} outside the allowed list do not change: {attrs}",
                                                         "the list must match the declared modification capabilities (validated)"]))
            if c["only_mod"]:
                pk, sid = c["only_mod"]
                attrs = [x for x in self.bus(c) if x != pk]
                eq = " and ".join(f"{E}.{x}[v] == old({E}.{x}[v])" for x in attrs) or f"v in {E}"
                self.prop("business", "law", f"BR_ONLYMOD_{E}", "preserves", sid,
                          f"only the {c['props'][pk]['display']} of a registered {c['display']} can change",
                          f"forall v: old(v in {E}) => ({eq})",
                          step=("ONLY_MODIFIABLE", [f"all business attributes of {E} except {pk}: {attrs}"]))
                self.required[f"BR_ONLYMOD_{E}"] = [f"preserves:{x}" for x in attrs]
            for r in c["refs"]:
                T = C[r["target"]]
                ref = slug(T["display"])
                self.prop("business", "invariant", f"BR_REF_{E}_{ref}", None, r["sn"], f"every {c['display']} belongs to a registered {T['display']}",
                          f"forall v: (v in {E}) => {E}.{ref}[v] in {T['core']}",
                          step=("BELONGS", [f"{E}.{ref} is an inferred relationship to {T['core']}"]))
            for pk, (n, sid) in c["min"].items():
                self.prop("business", "invariant", f"BR_MIN_{E}_{pk}", None, sid, f"the {c['props'][pk]['display']} of every {c['display']} is at least {n}",
                          f"forall v: (v in {E}) => {E}.{pk}[v] >= {n}", step=("MIN", [f"{E}.{pk} is a bounded Natural value"]))
            for pk, (n, sid) in c["max"].items():
                self.prop("business", "invariant", f"BR_MAX_{E}_{pk}", None, sid, f"the {c['props'][pk]['display']} of every {c['display']} is at most {n}",
                          f"forall v: (v in {E}) => {E}.{pk}[v] <= {n}", step=("MAX", [f"{E}.{pk} is a bounded Natural value"]))
            if c["numbering"]:
                self.numbering(c)
            if c["capacity"]:
                self.capacity(c)
            if c["owner_of"] or c["cap_of"]:
                self.prop("technical", "invariant", f"TR_COUNTER_{E}", None, c["register"],
                          f"technical counters for {c['display']} exist while the {c['display']} exists",
                          f"forall v: ((v in {E}) => (v in Counter{E})) and ((v in Counter{E}) => (v in {E}))",
                          origin="DD_AUTO_COUNT" if c["cap_of"] else "DD_AUTO_SEQ",
                          step=("TECH_STATE", [f"technical state separated from the business state of {E}: Counter{E}"]))
            if c.get("id_unique"):
                k_ = c["identity"][0]
                self.prop("business", "invariant", f"BR_UNIQUE_{E}_{k_}", None, c["id_unique"]["sn"],
                          f"no two {c['display']} share the same {c['props'].get(k_, {}).get('display', k_)}",
                          f"forall v1: forall v2: ((v1 in {E}) and (v2 in {E}) and v1 == v2) => v1 == v2",
                          step=("UNIQUE_COMBO", [f"{k_} is the identity of {c['display']}; identity already guarantees uniqueness"]))
            for cm in c["count_max"]:
                n, O = cm["n"], C[cm["owner"]]
                o = slug(O["display"])
                vs = [f"v{i}" for i in range(1, n + 2)]
                pre = [f"({x} in {E})" for x in vs] + [f"{E}.{o}[v1] == {E}.{o}[{x}]" for x in vs[1:]]
                if cm["scope_list"]:
                    pre += ["(" + " or ".join(f"{E}.state[{x}] == {self.sv(E, s_)}" for s_ in cm["scope_list"]) + ")" for x in vs]
                pre += [f"{a_} != {b_}" for i, a_ in enumerate(vs) for b_ in vs[i + 1:] if (a_, b_) != (vs[-2], vs[-1])]
                self.prop("business", "invariant", f"BR_COUNT_{E}_{camel(o)}" + (f"_EN_{camel(norm_s('_'.join(cm['scope_list'])))}" if cm["scope_list"] else ""),
                          None, cm["sn"], f"a {O['display']} has at most {n} {plural(c['display'])}{count_scope_txt(cm)}",
                          "".join(f"forall {x}: " for x in vs) + "(" + " and ".join(pre) + f") => {vs[-2]} == {vs[-1]}",
                          step=("COUNT_MAX", [f"there cannot be {n + 1} distinct {plural(c['display'])} for the same {O['display']}{count_scope_txt(cm)}",
                                              "constant bound: expressed without a counter (n+1 quantifiers)"]))
            for cb in c["combos"]:
                ps = combo_ps(cb)
                sc = cb.get("scope")
                self.prop("business", "invariant", f"BR_UNIQUE_{E}_" + "_".join(ps) + (f"_EXCEPT_{camel(norm_s(sc))}" if sc else ""), None, cb["sn"],
                          f"there cannot be two {plural(c['display'])}" + (f" outside state {sc}" if sc else "") + f" with {combo_phrase(cb)}",
                          f"forall v1: forall v2: ((v1 in {E}) and (v2 in {E}) and " +
                          "".join(f"{E}.state[v1] != {self.sv(E, s_)} and {E}.state[v2] != {self.sv(E, s_)} and " for s_ in scope_list(cb)) +
                          " and ".join(f"{E}.{p}[v1] == {E}.{p}[v2]" for p in ps) + ") => v1 == v2",
                          step=("UNIQUE_COMBO", ["uniqueness of the combination" + (" (also the business identity)" if c.get("combo_identity") is cb else "")]))
            s_ = c["states"]
            if s_ and len(s_["values"]) > 1 and s_.get("initial"):
                vals = " or ".join(f"{E}.state[v] == {self.sv(E, x)}" for x in s_["values"])
                self.prop("business", "invariant", f"BR_STATES_{E}", None, s_["sn"],
                          f"every {c['display']} is in one of its states ({', '.join(s_['values'])})",
                          f"forall v: (v in {E}) => ({vals})", step=("STATES", ["closed set of allowed states"]))
                ini = s_["initial"]
                val = self.sv(E, ini["a"]) if ini["kind"] == "fixed" else f"if {E}.{ini['p']}[v] <= {ini['n']} then {self.sv(E, ini['a'])} else {self.sv(E, ini['b'])}"
                self.prop("business", "law", f"BR_INITIAL_STATE_{E}", None, ini["sn"],
                          f"a new {c['display']} starts in state " + (ini["a"] if ini["kind"] == "fixed" else
                          f"{ini['a']} if its {c['props'][ini['p']]['display']} is {ini['n']} or less; otherwise, {ini['b']}"),
                          f"forall v: ((not old(v in {E})) and (v in {E})) => {E}.state[v] == {val}",
                          step=("INITIAL_STATE", ["state assigned during registration"]))
                allowed = " or ".join(f"(old({E}.state[v]) == {self.sv(E, a_)} and {E}.state[v] == {self.sv(E, b_)})"
                                      for b_, fr in s_["trans"].items() for a_, _ in fr) or "false"
                tsid = next(iter(s_["trans"].values()))[0][1] if s_["trans"] else s_["sn"]
                self.prop("business", "law", f"BR_TRANSITIONS_{E}", None, tsid,
                          f"a {c['display']} changes state only through an allowed transition",
                          f"forall v: (old(v in {E}) and (v in {E}) and {E}.state[v] != old({E}.state[v])) => ({allowed})",
                          step=("TRANSITION", ["every undeclared transition is prohibited (declared transitions: " +
                                ", ".join(f"{a_}→{b_}" for b_, fr in s_["trans"].items() for a_, _ in fr) + ")"]))
            if c["artifact"]:
                a_ = c["artifact"]
                self.prop("business", "invariant", f"BR_ARTIFACT_{E}", None, a_["sn"],
                          f"each registered {c['display']} has exactly one {a_['name']} file",
                          f"forall v: (v in {E}) => v in File",
                          step=("GENERATES", [f"one-to-one relationship between {E} and File{a_['name']}", f"file identity = {E} identity (decision {a_['identity']})"]))
            if c.get("consistency"):
                a_ = c["artifact"]
                self.prop("technical", "invariant", f"TR_CONSISTENT_{E}", None, c["consistency"],
                          f"{c['display']} and its {a_['name']} file either both exist or neither exists",
                          f"forall v: ((v in {E}) => (v in File)) and ((v in File) => (v in {E}))",
                          step=("CONSISTENT", ["record and file existence are equivalent"]))

    NO_CHANGE = re.compile(r"no information changes|is not registered|is not modified|is not created")

    def frame(self, op) -> str:
        parts = []
        for ent, attr, k in self.writes[op]:
            if attr is None:
                parts.append(f"(({k} in {ent}) => old({k} in {ent})) and (old({k} in {ent}) => ({k} in {ent}))")
            else:
                parts.append(f"{ent}.{attr}[{k}] == old({ent}.{attr}[{k}])")
        return " and ".join(parts)

    def outcome_obligations(self):
        """The declared effect of each outcome (“no information changes”) is a guarantee, not just generated code."""
        for cap in self.a.capabilities:
            c = self.a.concepts[cap["concept"]]
            op = self.op_name(cap, c)
            earlier = []
            for case in cap["cases"]:
                o = case.get("outcome")
                if not o:
                    continue
                cnd0 = self.cond(c, case, modify=cap["kind"] in ("modify", "transition"), pk=cap.get("prop"))
                cnd = " and ".join([f"(not ({e}))" for e in earlier] + [f"({cnd0})"])
                earlier.append(cnd0)
                if not self.NO_CHANGE.search(norm_s(o["effect"] or "")):
                    continue
                txt = f"if {case['label']}, the result is {o['display']} and no information changes"
                st = ("OUTCOME_EFFECT", [f"guard for {o['name']} evaluated in the previous state", f"outcome {o['name']} leaves information unchanged"])
                if o.get("technical"):
                    self.prop("technical", "ensures", f"TR_OUT_{op}_{o['name']}", None, cap["declared"][0] if cap["declared"] else None,
                              txt, f"old({cnd}) => (result is {o['name']} and unchanged)", origin="DD_AUTO_KEY", extra=[op], step=st)
                else:
                    self.prop("business", "ensures", f"BR_OUT_{op}_{o['name']}", None, o["sentence"], txt,
                              f"old({cnd}) => (result is {o['name']} and unchanged)", extra=[op], step=st)

    def success_obligations(self):
        """The success branch states its complete postcondition: the written values and nothing else (global frame)."""
        for cap in self.a.capabilities:
            c = self.a.concepts[cap["concept"]]
            op = self.op_name(cap, c)
            sid = cap.get("success_sentence")
            if not sid or op not in self.success:
                continue
            items, seen = [], set()
            for ent, attr, k in self.writes[op]:
                if attr is not None and (ent, None, k) in self.writes[op]:
                    continue
                it = f"{ent}[{k}]" if attr is None else f"{ent}.{attr}[{k}]"
                if it not in seen:
                    seen.add(it)
                    items.append(it)
            body = " and ".join(self.success[op] + [f"unchanged except {', '.join(items)}"])
            self.prop("business", "ensures", f"BR_OK_{op}", None, sid,
                      f"when {cap['id'][0].lower() + cap['id'][1:]} succeeds, the requested values remain and nothing else changes",
                      f"result is success => ({body})", extra=[op],
                      step=("SUCCESS_POST", ["success-branch postcondition: written values and global frame (unchanged except)"]))

    def capacity(self, c):
        C = self.a.concepts
        cp = c["capacity"]
        E, O = c["core"], C[cp["owner"]]["core"]
        o = slug(C[cp["owner"]]["display"])
        ca, pk, sid = self.cnt(c["key"]), cp["prop"], cp["sn"]
        base = f"BR_CAPACITY_{E}"
        txt = f"a {C[cp['owner']]['display']} has no more {c['display']} than its {pk}"
        st = ("CAPACITY", [f"{O}.{pk} is Natural (inferred)", f"{E}.position_{o} and Counter{O}.{ca} are implementation details (DD_AUTO_COUNT)",
                           "cardinality <= counter by injection (unique positions in [1, counter]); pigeonhole argument is outside SMT"])
        self.prop("business", "invariant", base, "bound", sid, f"{txt}: the counter does not exceed {pk}",
                  f"forall k: (k in {O}) => Counter{O}.{ca}[k] <= {O}.{pk}[k]", step=st)
        self.prop("business", "invariant", base, "injective", sid, f"{txt}: each {c['display']} occupies a distinct position",
                  f"forall v1: forall v2: ((v1 in {E}) and (v2 in {E}) and {E}.{o}[v1] == {E}.{o}[v2] and {E}.position_{o}[v1] == {E}.position_{o}[v2]) => v1 == v2", step=st)
        self.prop("business", "invariant", base, "range", sid, f"{txt}: occupied positions range from 1 to the counter",
                  f"forall v: (v in {E}) => ({E}.position_{o}[v] >= 1 and {E}.position_{o}[v] <= Counter{O}.{ca}[{E}.{o}[v]])", step=st)
        self.required[base] = ["bound", "injective", "range"]
        self.prop("technical", "invariant", f"TR_CNT_NONNEG_{E}", None, sid, f"the counter for {O} is never negative",
                  f"forall v: (v in {O}) => Counter{O}.{ca}[v] >= 0", origin="DD_AUTO_COUNT", step=st)
        self.realization.append({"element": f"Counter{O}.{ca}, {E}.position_{o}", "justified_by": [sid], "decision": "DD_AUTO_COUNT",
                                 "alternatives": ["count on demand (aggregate)", "persisted counter + assigned position"],
                                 "note": "implementation of the cardinality bound; proof by injection"})

    def numbering(self, c):
        a, C = self.a, self.a.concepts
        n = c["numbering"]
        E, O = c["core"], C[n["owner"]]["core"]
        o = slug(C[n["owner"]]["display"])
        s = n["start"]
        sq = self.seq(C[n["owner"]], c["key"])
        base = f"BR_NUMBERING_{E}"
        sid = n["sn"]
        txt = f"{plural(c['display'])} of each {C[n['owner']]['display']} are numbered consecutively starting at {s}"
        st = ("NUMBERING", [f"{E}.number is an inferred structure", f"owner {O} by BELONGS", "four guarantees: starting value, uniqueness, no gaps, and next number"])
        self.prop("business", "invariant", base, "from_start", sid, f"{txt}: numbering starts at {s}",
                  f"forall v: (v in {E}) => {E}.number[v] >= {s}", step=st)
        self.prop("business", "invariant", base, "unique_per_owner", sid, f"{txt}: no numbers are repeated for the same {C[n['owner']]['display']}",
                  f"forall v1: forall v2: ((v1 in {E}) and (v2 in {E}) and {E}.{o}[v1] == {E}.{o}[v2] and {E}.number[v1] == {E}.number[v2]) => v1 == v2", step=st)
        self.prop("business", "invariant", base, "no_gaps", sid, f"{txt}: there are no gaps",
                  f"forall v: (v in {E}) => ({E}.number[v] == {s} or (exists w in {E}: {E}.{o}[w] == {E}.{o}[v] and {E}.number[w] == {E}.number[v] - 1))", step=st)
        self.prop("business", "law", base, "next_number", sid, f"{txt}: each new {c['display']} receives the next number",
                  f"forall v: ((not old(v in {E})) and (v in {E})) => {E}.number[v] == Counter{O}.{sq}[{E}.{o}[v]] {"+" if s >= 1 else "-"} {abs(s - 1)}", step=st)
        self.required[base] = ["from_start", "unique_per_owner", "no_gaps", "next_number"]
        self.prop("technical", "invariant", f"TR_SEQ_NONNEG_{E}", None, sid, f"the counter for {O} is never negative",
                  f"forall v: (v in {O}) => Counter{O}.{sq}[v] >= 0", origin="DD_AUTO_SEQ",
                  step=("NUMBERING", ["implementation: counter per owner"]))
        self.prop("technical", "invariant", f"TR_SEQ_BOUND_{E}", None, sid, f"the counter for {O} bounds the numbers of {E}",
                  f"forall v: (v in {E}) => {E}.number[v] < {_off(f'Counter{O}.{sq}[{E}.{o}[v]]', s)}", origin="DD_AUTO_SEQ",
                  step=("NUMBERING", ["implementation: counter per owner"]))
        self.prop("technical", "invariant", f"TR_SEQ_COVER_{E}", None, sid, f"the final number in the counter for {O} is assigned",
                  f"forall k1: ((k1 in {O}) and Counter{O}.{sq}[k1] >= 1) => (exists w in {E}: {E}.{o}[w] == k1 and {E}.number[w] == Counter{O}.{sq}[k1] {"+" if s >= 1 else "-"} {abs(s - 1)})",
                  origin="DD_AUTO_SEQ", step=("NUMBERING", ["implementation: counter per owner"]))
        self.realization.append({"element": f"Counter{O}.{sq}", "justified_by": [sid], "decision": "DD_AUTO_SEQ",
                                 "alternatives": ["persisted counter", "maximum existing number + 1", "transactional sequence"],
                                 "note": "implementation of the numbering rule; not business data"})


# --------------------------------------------------------------------------- 5. weakening detection
def attrs_in(x, out=None):
    out = set() if out is None else out
    if isinstance(x, dict):
        if x.get("k") == "attr":
            out.add((x["e"], x["a"]))
        for v in x.values():
            attrs_in(v, out)
    elif isinstance(x, (list, tuple)):
        for v in x:
            attrs_in(v, out)
    return out


def check_weakening(rules_required: dict[str, list[str]], core_spec, concept_of: dict[str, str]) -> list[dict]:
    """Compare the guarantees required by the catalog for each business rule with those present in the generated Core."""
    props = {p.id: p for p in core_spec.props}
    out = []
    for base, req in rules_required.items():
        missing = []
        for g in req:
            if g.startswith("preserves:"):
                p = props.get(f"{base}__preserves")
                att = g.split(":", 1)[1]
                have = bool(p) and (concept_of[base], att) in attrs_in(p.expr)
                if not have:
                    missing.append(g)
            elif f"{base}__{g}" not in props:
                missing.append(g)
        out.append({"rule": base, "required_guarantees": req, "missing_guarantees": missing,
                    "status": "WEAKENED" if missing else "FULL"})
    return out


# --------------------------------------------------------------------------- 6. complete pipeline
PROCEDURAL = re.compile(r"\b(create|set|old|write_file|when|otherwise|operation|entity|enum|forall|exists|invariant|law|"
                        r"ensures|compose|query|origin)\b")


def is_surface(src: str) -> bool:
    return bool(re.match(r'\s*(?://[^\n]*\n\s*)*spec\s+\w+\s+version\s+"[^"]*"\s+(?:level\s+\w+\s+)?(?:(?://[^\n]*\n\s*)*)'
                         r'(?:glossary|business|intent|technical|initial|assumptions|questions|examples|outcomes|use|decision|assumption|question|gap|input|answers)\b', src))


def classify_question(q_: dict, an: Analysis, rule_ids: dict) -> dict:
    t = norm_s(q_["text"])
    concepts = [k for k, c in an.concepts.items() if re.search(rf"\b{re.escape(k)}s?\b", t)]
    affected_rules = []
    for k in concepts:
        c = an.concepts[k]
        if re.search(r"baja|elimin|borr|restring", t) and c["no_delete"]:
            affected_rules.append(f"BR_KEPT_{c['core']}")
        if re.search(r"modific|cambi", t) and (c["immutable"] or c["only_mod"] or c["only_mods"]):
            affected_rules.append(f"BR_{'IMMUTABLE' if c['immutable'] else 'ONLYMOD'}_{c['core']}")
    caps = [cap["id"] for cap in an.capabilities if cap["concept"] in concepts]
    if re.search(r"file|csv|counter|transacti|retry|storage", t):
        cls = "AFFECTS_REALIZATION_ONLY"
    elif affected_rules:
        cls = "AFFECTS_RULES"
    elif caps and re.search(r"can be registered|can be modified", t):
        cls = "AFFECTS_CAPABILITIES"
    elif concepts:
        cls = "NON_BLOCKING"
    else:
        cls = "NON_BLOCKING"
    return {"id": q_["id"], "question": q_["text"], "classification": cls, "concepts": concepts,
            "affected_rules": affected_rules, "affected_capabilities": caps,
            "blocking": cls == "BLOCKING"}


def load_bases(surf: Surface, path: Path | None, seen=None, closure: bool | None = None) -> None:
    """use spec "path.upa": sentences, outcomes, decisions, and assumptions are inherited from the base with provenance.
    The new specification contains only the additions; the agent does not need the base text (see `summary`)."""
    seen = seen if seen is not None else set()
    for u in [u for u in surf.uses if "spec" in u]:
        bp = ((path.parent if path else Path.cwd()) / u["spec"]).resolve()
        if bp in seen:
            continue
        seen.add(bp)
        if not bp.exists():
            raise UpaError("UNKNOWN_BASE_SPEC", f"use spec {u['spec']!r}: {bp} does not exist", None, "surface")
        b = parse_surface(bp.read_text(encoding="utf-8"))
        load_bases(b, bp, seen, closure)
        close_surface(b, bp, closure)  # v15: inherit the already-closed base and its provenance
        surf.bases += [x for x in b.bases if x["path"] not in {y["path"] for y in surf.bases}]
        pre = []
        for s_ in b.sentences:
            sid = s_.id if "." in s_.id else f"{b.name}.{s_.id}"
            pre.append(Sentence(sid, s_.text, s_.section, s_.group, s_.line, capability=s_.capability, cite=s_.cite,
                                prov=s_.prov, basis=[f"{b.name}.{x}" if re.match(r"^[SD]\d", str(x)) else x for x in s_.basis]))
            surf.inherited[sid] = b.inherited.get(s_.id, b.name)
        surf.sentences[:0] = pre
        known = {d["id"] for d in surf.decisions}
        surf.decisions[:0] = [d for d in b.decisions if d["id"] not in known]
        surf.assumptions[:0] = b.assumptions
        surf.glossary[:0] = b.glossary
        surf.bases.append({"spec": b.name, "version": b.version, "path": str(bp), "sentences": len(pre),
                           "hash": hashlib.sha256(bp.read_bytes()).hexdigest()[:16]})


def dedupe_sentences(surf: Surface) -> None:
    """Repeating a sentence (from a base or this specification) does not create a new rule: keep the first sentence and its
    provenance, and record the repetition for the guide. Otherwise, the Core would receive two rules with the same name (v11)."""
    seen, keep, red = {}, [], []
    for s_ in surf.sentences:
        k = (s_.section, s_.capability, norm_s(s_.text).rstrip(". "))
        if k in seen:
            if s_.cite and s_.id not in surf.inherited and seen[k].id not in surf.inherited and s_.cite != seen[k].cite:
                seen[k].alt_cites = getattr(seen[k], "alt_cites", []) + [s_.cite]
            red.append({"id": s_.id, "text": s_.text, "same_as": seen[k].id, "adds_cite": bool(s_.cite and s_.cite != seen[k].cite),
                        "spec": surf.inherited.get(seen[k].id, "this specification")})
            continue
        seen[k] = s_
        keep.append(s_)
    surf.sentences[:] = keep
    surf.redundant = red


def _analyze_once(surf: Surface, path: Path | None) -> Analysis:
    from .terminology import Terminology
    an = Analysis(surf)
    tm = an.terminology = Terminology(surf, path)
    tm.apply(an.errors)
    an.recognize()
    an.build_model()
    tm.check_model(an, an.errors)
    an.capabilities_and_outcomes()
    return an


def close_surface(surf: Surface, path: Path | None, closure: bool | None = None) -> Analysis:
    """v15: normalize -> analyze -> close to a fixed point. Without closure (UPA_NO_CLOSURE=1), behavior matches v14."""
    from . import closure as cl
    on = cl.ENABLED if closure is None else closure
    if on:
        cl.normalize(surf)
    dedupe_sentences(surf)
    an = _analyze_once(surf, path)
    if on:
        an = cl.close(surf, an, lambda: _analyze_once(surf, path))
    return an


def analyze(src: str, path: Path | None = None, closure: bool | None = None) -> tuple[Surface, Analysis]:
    surf = parse_surface(src)
    load_bases(surf, path, closure=closure)
    an = close_surface(surf, path, closure)
    return surf, an


def write_json(out: Path, name: str, obj):
    (out / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def build_surface(path: Path, out: Path, timeout_ms: int = 60000, cache_path: Path | None = None) -> dict:
    from . import compiler
    out.mkdir(parents=True, exist_ok=True)
    src = path.read_text(encoding="utf-8")
    try:
        surf, an = analyze(src, path)
    except UpaError as e:
        write_json(out, "errors.json", [e.to_json()])
        return {"phase": "parse", "exit": 1, "errors": [e.to_json()]}

    # superficie
    formal = [s for s in surf.sentences if s.section in FORMAL_SECTIONS + ("outcomes",)]
    write_json(out, "normalized-surface.json", {
        "spec": surf.name, "version": surf.version,
        "sentences": [{"id": s.id, "section": s.section, "group": s.group, "capability": s.capability, "line": s.line,
                       "normalized": norm_s(s.text), "text": s.text} for s in surf.sentences],
        "decisions": surf.decisions, "assumptions": surf.assumptions, "questions": surf.questions, "gaps": surf.gaps, "uses": surf.uses,
        "bases": surf.bases, "inherited_sentences": surf.inherited})
    write_json(out, "matched-patterns.json", an.matched)
    write_json(out, "unresolved-phrases.json", an.unresolved)
    write_json(out, "semantic-classification.json", [
        {"sentence": m["sentence"], "text": m["text"], "pattern": m["pattern"], "classification": m["logical"],
         "user_classified": False} for m in an.matched])
    amb = [u for u in an.unresolved if u["error"] in ("AMBIGUOUS_RULE",)]
    write_json(out, "ambiguity-report.json", {"ambiguous_rules": amb, "ambiguous_model": an.ambiguities,
                                              "heuristics": an.heuristics})
    write_json(out, "inferred-capabilities.json", [
        {"capability": c["id"], "kind": c["kind"], "concept": an.concepts[c["concept"]]["core"],
         "cases": [{k: v for k, v in x.items() if k != "outcome"} for x in c["cases"]]} for c in an.capabilities])
    write_json(out, "generated-outcomes.json", [
        {"capability": c["id"],
         "outcomes": [{"when": x["label"], "result": x.get("outcome", {}).get("display"), "effect": x.get("outcome", {}).get("effect"),
                       "source": x.get("outcome", {}).get("sentence")} for x in c["cases"]]
         + [{"when": "otherwise", "result": c.get("success_name"), "effect": "is registered or modified",
             "source": c.get("success_sentence")}]} for c in an.capabilities])
    write_json(out, "terminology-report.json", an.terminology.report())
    write_json(out, "language-gaps.json", [{**g, "status": "LANGUAGE_GAP"} for g in surf.gaps])
    write_json(out, "question-impact.json", [classify_question(x, an, {}) for x in surf.questions])

    metrics_surface = None
    if an.errors or an.incomplete:
        # does not reach Core: report the error and stop
        write_json(out, "errors.json", an.errors.to_json())
        write_json(out, "inferred-model.json", {"status": "NOT_BUILT", "reason": "surface errors"})
        write_json(out, "semantic-weakening-report.json", [])
        write_json(out, "provenance.json", {})
        write_json(out, "metrics.json", surface_metrics(src, an, None, None))
        return {"phase": "surface", "exit": 1, "errors": an.errors.to_json(),
                "incomplete_capabilities": an.incomplete}

    gen = CoreGen(an)
    core_src = gen.generate()
    core_dir = out / "core"
    core_dir.mkdir(exist_ok=True)
    (core_dir / "core.upa").write_text(core_src, encoding="utf-8")
    scope = "\n".join(s.text for s in surf.sentences) + "\n"
    (core_dir / "scope.md").write_text(scope, encoding="utf-8")
    try:
        core_spec = parse_core(core_src)
    except UpaError as e:
        write_json(out, "errors.json", [e.to_json() | {"note": "internal error while generating the Core"}])
        return {"phase": "core-generation", "exit": 1, "errors": [e.to_json()]}
    concept_of = {}
    for k, c in an.concepts.items():
        concept_of[f"BR_IMMUTABLE_{c['core']}"] = c["core"]
        concept_of[f"BR_ONLYMOD_{c['core']}"] = c["core"]
    weak = check_weakening(gen.required, core_spec, concept_of)
    write_json(out, "semantic-weakening-report.json", weak)
    bad = [w for w in weak if w["status"] == "WEAKENED"]
    if bad:
        errs = [{"error": "SEMANTIC_WEAKENING", "rule": w["rule"], "missing_guarantees": w["missing_guarantees"]} for w in bad]
        write_json(out, "errors.json", errs)
        return {"phase": "weakening", "exit": 1, "errors": errs}

    write_json(out, "inferred-model.json", inferred_model(an, gen))
    write_json(out, "provenance.json", provenance(an, gen))
    res = compiler.build(core_dir / "core.upa", core_dir / "scope.md", core_dir, timeout_ms, cache_path)
    for f in ("canonical-ir.json", "interaction-ir.json", "proof-obligations.json", "verification-result.json", "assumptions.json",
              "guarantee-scope.json", "evidence-ledger.json"):
        if (core_dir / f).exists():
            (out / f).write_text((core_dir / f).read_text(encoding="utf-8"), encoding="utf-8")
    vr = json.loads((core_dir / "verification-result.json").read_text(encoding="utf-8")) if (core_dir / "verification-result.json").exists() else None
    core_ir = json.loads((core_dir / "canonical-ir.json").read_text(encoding="utf-8")) if (core_dir / "canonical-ir.json").exists() else None
    write_json(out, "metrics.json", surface_metrics(src, an, vr, core_ir, gen))
    res["surface"] = {"sentences": len(surf.sentences), "recognized": len(an.matched), "core": str(core_dir / "core.upa")}
    res["pending_model_ambiguities"] = [a["id"] for a in an.ambiguities if a["status"] == "pending_human"]
    return res


def surface_metrics(src, an: Analysis, vr, ir, gen=None) -> dict:
    stripped = re.sub(r'"(?:[^"\\]|\\.)*"', '""', re.sub(r"//[^\n]*", "", src))
    proc = len(PROCEDURAL.findall(stripped))
    total = len(an.s.sentences)
    rec = len(an.matched)
    ents = [c for c in an.concepts.values()]
    attrs = sum(len(c["props"]) for c in ents)
    rels = sum(len(c["refs"]) for c in ents)
    outc = sum(1 for c in an.capabilities for x in c["cases"] if "outcome" in x) + len(an.capabilities)
    unjust = 0
    for c in ents:
        for p in c["props"].values():
            if not p["sources"]:
                unjust += 1
    implicit = sum(1 for c in an.capabilities for x in c["cases"] if "outcome" not in x)
    d = {
        "business_surface": {
            "source_sentences": total, "recognized_sentences": rec,
            "ambiguous_sentences": sum(1 for u in an.unresolved if u["error"] == "AMBIGUOUS_RULE"),
            "unsupported_patterns": sum(1 for u in an.unresolved if u["error"] == "UNSUPPORTED_PATTERN"),
            "business_vocabulary_ratio": round(rec / total, 3) if total else 0.0,
            "procedural_construct_count": proc},
        "inference": {
            "inferred_entities": len(ents), "inferred_values": attrs - sum(1 for c in ents if c["identity"]),
            "inferred_identifiers": sum(1 for c in ents if c["identity"] or c["provided_id"]),
            "inferred_attributes": attrs, "inferred_relations": rels,
            "inferred_capabilities": len(an.capabilities), "inferred_outcomes": outc,
            "ambiguous_model_choices": len(an.ambiguities), "unjustified_structure_count": unjust},
        "semantic_quality": {
            "semantic_weakening_count": 0, "redundant_state_count": len(an.states_redundant),
            "implicit_outcome_count": implicit,
            "unresolved_business_terms": sum(1 for u in an.unresolved if u["error"] == "UNRESOLVED_BUSINESS_TERM")}}
    if vr:
        d["verification"] = {"verdict": vr["verdict"], **vr["summary"]}
    if ir:
        d["core"] = {"operations": len(ir["operations"]), "properties": len(ir["properties"])}
    return d


def inferred_model(an: Analysis, gen: CoreGen) -> dict:
    ents = {}
    for k, c in an.concepts.items():
        ident = c["identity"][0] if c["identity"] else ("(supplied by " + c["provided_id"][0] + ")" if c["provided_id"] else None)
        props = []
        for pk, p in c["props"].items():
            props.append({"name": pk, "type": p["type"], "required": pk in c["required"],
                          "justified_by": p["sources"] + p["inferred_by"], "classification": "INFERRED_STRUCTURE"})
        for r in c["refs"]:
            props.append({"name": slug(an.concepts[r["target"]]["display"]), "type": f"reference to {an.concepts[r['target']]['core']}",
                          "required": True, "justified_by": [r["sn"]], "classification": "INFERRED_RELATION"})
        if c["numbering"]:
            props.append({"name": "number", "type": "Natural", "required": True, "justified_by": [c["numbering"]["sn"]],
                          "classification": "INFERRED_STRUCTURE"})
        kind = "Entity candidate"
        ents[c["core"]] = {"concept": c["display"], "class": kind, "identity": ident, "properties": props,
                           "artifact": c["artifact"]["name"] if c["artifact"] else None,
                           "classification_alternatives": ["Entity", "DurableResult"] if c["immutable"] else None}
    values = sorted({p["name"] for c in an.concepts.values() for p in c["props"].values() if p["type"] in ("Natural", "Text")})
    return {"entities": ents, "values": values,
            "identifiers": sorted(c["identity"][0] for c in an.concepts.values() if c["identity"]),
            "roles": sorted(an.roles), "artifacts": [{"name": a["name"], "of": an.concepts[a["of"]]["core"], "identity": "same identifier"} for a in an.artifacts.values()],
            "relations": [{"from": c["core"], "to": an.concepts[r["target"]]["core"], "cardinality": "exactly_one", "source": r["sn"]}
                          for c in an.concepts.values() for r in c["refs"]]
            + [{"from": c["core"], "to": f"File{c['artifact']['name']}", "cardinality": "exactly_one", "source": c["artifact"]["sn"]}
               for c in an.concepts.values() if c["artifact"]],
            "realization": gen.realization,
            "redundant_states": an.states_redundant,
            "note": "realization elements exist only in the Core to satisfy rules; they are not business data"}


def provenance(an: Analysis, gen: CoreGen) -> dict:
    out = {}
    for k, c in an.concepts.items():
        for pk, p in c["props"].items():
            out[f"{c['core']}.{pk}"] = {"classification": "INFERRED_STRUCTURE", "required_by": p["sources"] + p["inferred_by"],
                                        "sources": [{"sentence_id": s, "phrase": gen.sent[s].text} for s in p["sources"] + p["inferred_by"] if s in gen.sent]}
        for r in c["refs"]:
            out[f"{c['core']}.{slug(an.concepts[r['target']]['display'])}"] = {
                "classification": "INFERRED_RELATION", "source": {"sentence_id": r["sn"], "phrase": gen.sent[r["sn"]].text},
                "pattern": "EXACTLY_ONE_RELATION", "cardinality": {"order_to_target": 1}, "constraints": {"target_must_exist": True}}
        if c["numbering"]:
            out[f"{c['core']}.number"] = {"classification": "INFERRED_STRUCTURE", "source": {"sentence_id": c["numbering"]["sn"],
                                          "phrase": gen.sent[c["numbering"]["sn"]].text}, "pattern": "NUMBERING"}
    for rz in gen.realization:
        out[rz["element"]] = {"classification": "DESIGN_DECISION", "decision": rz["decision"], "justified_by": rz["justified_by"],
                              "alternatives": rz["alternatives"]}
    for pid, p in gen.prov.items():
        out[pid] = p
    for cap in an.capabilities:
        out[f"capability:{cap['id']}"] = {"classification": "INFERRED_CAPABILITY", "cases": len(cap["cases"]) + 1,
                                          "sources": [s for s in cap["declared"]]}
    for h in an.heuristics:
        out.setdefault("heuristics", []).append(h)
    return out
