"""Deterministic semantic closure for UPA specifications.

Authors declare what they know; UPA builds the most complete canonical representation justified by that input.
Every canonical sentence retains its provenance:

- ASSERTED: written by the author (with `from "citation"` when the specification has an `input` block).
- NORMALIZED: equivalent meaning in canonical form; an input sentence is decomposed into canonical sentences.
- DERIVED: follows from accepted sentences and an explicit UPA semantic axiom (AXIOMS).
- RESOLVED: a human decision resolving an ambiguity (decision ... resolves ...).
- TECHNICAL: an implementation detail invisible to the business (adds no business rules).

Plausible but unprovable claims are not added; they remain errors or questions. The order is normalize, close to a fixed
point, and then ask about anything still missing (guide).
"""
from __future__ import annotations
import os
import re
import unicodedata

ENABLED = os.environ.get("UPA_NO_CLOSURE") != "1"

AXIOMS = {
    "AX_REJECT": "An operation that would violate a rule returns a rejection outcome and leaves information unchanged; "
                 "the proposed outcome name is a technical suggestion that can be edited.",
    "AX_SUCCESS": "Every declared capability has a normal case: if no rejection condition applies, the operation succeeds.",
    "AX_EMPTY_START": "If no initial state is declared, no records exist initially.",
    "AX_ONLYMOD": "‘Only property P can be modified’ also asserts that P can be modified.",
    "AX_SINGLE_PROP": "‘A record can be modified’ without naming a property refers to the record’s only modifiable property.",
    "AX_UNIQUE_OWNER": "A property named without a concept belongs to the only concept that has it.",
    "AX_TECH_KEY": "A concept without a business identity uses a system-assigned technical key; "
                   "this does not add a business uniqueness rule.",
}

# A business identifier makes identity a modeling question rather than a technical-key default.
ID_LEX = re.compile(r"^(?:code|number|id|isbn|email|employee\s+id|registration|document)\b", re.I)

PH = r"[^\W\d]+(?:\s[^\W\d]+){0,3}?"
ART = r"(?:a|an|the|each|every)"
MODV = r"(?:modify|correct|change|edit|update)"
DELV = r"(?:delete|remove)"

_NUMWORDS = "one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty|one hundred"
INPUT_FORMS = [
    # id, regex (final period omitted), documentation
    ("N_REG_WITH", rf"(?P<cs>{PH}) can be registered with (?P<props>.+)",
     "<Concepts> can be registered with <properties>."),
    ("N_RANGE", rf"(?:the )?(?P<p>{PH})(?: of {ART} (?P<c>{PH}))? ranges? from (?P<a>\d+) to (?P<b>\d+)",
     "The <property> [of a <concept>] ranges from <a> to <b>."),
    ("N_MAX", rf"(?:the )?(?P<p>{PH})(?: of {ART} (?P<c>{PH}))? cannot (?:be greater than|exceed) (?P<b>\d+)",
     "The <property> [of a <concept>] cannot exceed <n>."),
    ("N_MIN", rf"(?:the )?(?P<p>{PH})(?: of {ART} (?P<c>{PH}))? (?:is|must be|has to be)(?: at)? (?:least|a minimum of) (?P<a>\d+|{_NUMWORDS})(?: [^\W\d_]+)?",
     "The <property> [of a <concept>] is at least <n>."),
    ("N_MAX2", rf"(?:the )?(?P<p>{PH})(?: of {ART} (?P<c>{PH}))? (?:is|must be|has to be|can be)(?: at)? (?:most|a maximum of|up to|no more than) (?P<b>\d+|{_NUMWORDS})(?: [^\W\d_]+)?",
     "The <property> [of a <concept>] is at most <n>."),
    ("N_NO_TWO", rf"(?:a|an|each) (?P<a>{PH}) cannot have (?:two|more than one|1) (?P<cs>{PH})",
     "A <A> cannot have two <concepts>."),
    ("N_MOD_ANY", rf"(?:a|an|the|each|every)?\s*(?P<c>{PH}) can be (?P<verb>{MODV})(?P<nd>,? but cannot be (?:deleted|removed))?",
     "A <concept> can be modified [but not deleted]."),
    ("N_MOD_ANY2", rf"(?P<c>{PH}) can be (?P<verb>{MODV})", "<Concepts> can be modified."),
    ("N_NO_DEL", rf"(?:a|an|the|each|every)?\s*(?P<c>{PH}) cannot be (?:deleted|removed)", "<Concepts> cannot be deleted."),
    ("N_ACK_EMPTY", r"initially, there (?:is|are) nothing(?:\s+(?:stored|registered))?",
     "Initially, there is nothing. (acknowledges AX_EMPTY_START)"),
    ("N_ACK_WARN", r"(?:if|when) .+ (?:does not exist|do not exist|is not registered|are not registered|is unavailable|are unavailable)(?: .+)?, (?:notify|show an error|reject)",
     "When an unavailable item is requested, notify the caller. (acknowledges AX_REJECT)"),
    ("N_BELONGS", rf"each (?P<a>{PH}) (?:is part of|belongs to|corresponds to) (?:a|an) (?P<b>{PH})(?: registered)?",
     "Each <A> belongs to a <B>."),
]
INPUT_FORMS = [(i, re.compile("^" + rx + "$", re.I | re.U), d) for i, rx, d in INPUT_FORMS]


def fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    return re.sub(r"\s+", " ", "".join(ch for ch in s if not unicodedata.combining(ch))).strip()


def singular(word: str) -> str:
    """Return an English plural noun to its singular form for common regular endings."""
    words = word.strip().split(" ")
    noun = words[0]
    lower = noun.lower()
    irregular = {"people": "person", "children": "child", "mice": "mouse", "teeth": "tooth", "feet": "foot"}
    if lower in irregular:
        noun = irregular[lower]
    elif len(lower) > 4 and lower.endswith("ies"):
        noun = noun[:-3] + "y"
    elif lower.endswith(("ches", "shes", "xes", "zes")):
        noun = noun[:-2]
    elif len(lower) > 3 and lower.endswith("s") and not lower.endswith("ss"):
        noun = noun[:-1]
    words[0] = noun
    return " ".join(words)


def _surface():
    from . import surface
    return surface


def _art(w, indef=False):
    return ("an" if w[:1].lower() in "aeiou" else "a") if indef else "the"


def _strip_art(p: str) -> str:
    return re.sub(r"^(?:a|an|the|its|their)\s+", "", p.strip(), flags=re.I)


def normalize(surf) -> None:
    """Replace input sentences with canonical forms; model-dependent forms are deferred until closure."""
    S = _surface()
    out, pend = [], []
    for sn in surf.sentences:
        if sn.section != "business" or sn.id in surf.inherited:
            out.append(sn)
            continue
        txt = sn.text.strip().rstrip(".")
        # Leave canonical sentences unchanged.
        if S.match_sentence(sn)[0] is not None and not re.search(r"^.+ can be registered with ", txt, re.I):
            out.append(sn)
            continue
        hit = None
        for fid, rx, _ in INPUT_FORMS:
            m = rx.match(txt)
            if m:
                hit = (fid, m.groupdict())
                break
        if not hit:
            out.append(sn)
            continue
        fid, g = hit
        new = []
        if fid == "N_REG_WITH":
            cs = g["cs"].strip()
            c = singular(cs)
            props = [_strip_art(p) for p in S.split_list(g["props"])]
            new.append(f"{cs} can be registered.")
            new.append(f"Every {c} has {', '.join(props[:-1]) + ' and ' + props[-1] if len(props) > 1 else props[0]}.")
            for p in props:
                new.append(f"The {p} of a {c} is required.")
        elif fid in ("N_RANGE", "N_MAX", "N_MIN", "N_MAX2"):
            g = {k: (_n(fold(v)) if k in ("a", "b") and v else v) for k, v in g.items()}
            p = g["p"].strip()
            if p.endswith("s"):
                p = singular(p)
            c = singular(g["c"].strip()) if g.get("c") else None
            suffix = f" of a {c}" if c else ""
            if fid in ("N_RANGE", "N_MIN"):
                new.append(f"The {p}{suffix} is {g['a']} or greater.")
            if fid != "N_MIN":
                new.append(f"The {p}{suffix} is {g['b']} or less.")
        elif fid in ("N_MOD_ANY", "N_MOD_ANY2"):
            c = singular(g["c"].strip())
            pend.append({"kind": "MOD_ANY", "concept": c, "sn": sn})
            if g.get("nd"):
                new.append(f"Registered {S.plural(c.split()[0])}{' ' + ' '.join(c.split()[1:]) if len(c.split()) > 1 else ''} cannot be deleted.")
        elif fid == "N_NO_TWO":
            a_ = g["a"].strip()
            new.append(f"There cannot be two {g['cs'].strip()} with the same {a_}.")
        elif fid == "N_NO_DEL":
            c = singular(g["c"].strip())
            new.append(f"A registered {c} cannot be deleted.")
        elif fid in ("N_ACK_EMPTY", "N_ACK_WARN"):
            surf.closure.setdefault("acknowledged", []).append(
                {"from": sn.id, "text": sn.text, "cite": sn.cite, "axiom": "AX_EMPTY_START" if fid == "N_ACK_EMPTY" else "AX_REJECT"})
        elif fid == "N_BELONGS":
            a, b = g["a"].strip(), g["b"].strip()
            new.append(f"Each {a} belongs to exactly one registered {b}.")
        for k, t in enumerate(new):
            out.append(S.Sentence(f"{sn.id}n{k + 1}", t, sn.section, sn.group, sn.line, cite=sn.cite, prov="NORMALIZED",
                                  basis=[sn.id, fid]))
        surf.closure.setdefault("normalized", []).append({"from": sn.id, "text": sn.text, "form": fid, "to": new, "cite": sn.cite,
                                                           "deferred": [p_["kind"] for p_ in pend if p_["sn"] is sn]})
    surf.sentences[:] = out
    surf.closure.setdefault("pending", []).extend(pend)


def _sid_rule(an, pid, pred=lambda g: True):
    for r in an.rules:
        if r["p"]["id"] == pid and pred(r["g"]):
            return r["sn"].id
    return None


def _case_basis(an, c, case):
    k = case["kind"]
    S = _surface()
    if k == "DUPLICATE":
        return [x for x in [(c["identity"] or [None, None])[1], (c["provided_id"] or [None, None])[1]] if x]
    if k == "BELOW_MIN":
        return [c["min"][case["arg"]][1]]
    if k == "ABOVE_MAX":
        return [c["max"][case["arg"]][1]]
    if k == "NOT_INFORMED":
        return [x for x in [_sid_rule(an, "REQUIRED", lambda g: S.slug(g["p"]) == case["arg"])] if x]
    if k == "REF_MISSING":
        return [r["sn"] for r in c["refs"] if r["target"] == case["arg"]] or ([c["register"]] if c["register"] else [])
    if k == "COMBO_DUP":
        return [cb["sn"] for cb in c["combos"] if (cb.get("scope") or None) == (case.get("scope") or None)][:1]
    if k == "WRONG_STATE":
        return [sid for _, sid in ((c.get("states") or {}).get("trans") or {}).get(case.get("to"), [])][:1]
    if k in ("CAPACITY", "BELOW_COUNT"):
        e = an.concepts.get(case["arg"]) or c
        cp = e.get("capacity") or c.get("capacity")
        return [cp["sn"]] if cp else []
    return []


def derive(surf, an) -> list:
    """Derive canonical sentences that follow from accepted statements and AXIOMS."""
    S = _surface()
    have = {(s.section, S.norm_s(s.capability or ""), S.norm_s(s.text).rstrip(". ")) for s in surf.sentences}
    new = []

    def add(text, section, basis, prov="DERIVED", capability=None, group="derived"):
        k = (section, S.norm_s(capability or ""), S.norm_s(text).rstrip(". "))
        if k in have or not text:
            return
        have.add(k)
        n = len([s for s in surf.sentences if s.id.startswith("D")]) + len(new) + 1
        new.append(S.Sentence(f"D{n:03d}", text, section, group if section == "business" else None, 0,
                              capability=capability, prov=prov, basis=basis))

    # 1. Resolve deferred generic modification statements against the model.
    for p_ in list(surf.closure.get("pending", [])):
        if p_["kind"] != "MOD_ANY" or p_.get("done"):
            continue
        ck = S.norm_s(p_["concept"])
        ck = ck if ck in an.concepts else next((k for k in an.concepts if S.norm_s(singular(k)) == ck), None)
        if ck is None:
            continue
        c = an.concepts[ck]
        key = c["identity"][0] if c["identity"] else None
        cands = [pk for pk, e in c["props"].items() if pk != key and e["type"] != "Identifier"]
        p_["candidates"] = cands
        if len(cands) == 1:
            d = c["props"][cands[0]]["display"]
            add(f"The {d} of a {c['display']} can be modified.", "business",
                [p_["sn"].id, "AX_SINGLE_PROP"] + [x for x in c["props"][cands[0]]["sources"][:1]])
            p_["done"] = True
    for k, c in an.concepts.items():
        # 2. A restriction on modification also enables the named modification.
        if c["only_mod"] and not c["modify"]:
            pk, sid = c["only_mod"]
            d = c["props"].get(pk, {}).get("display", pk)
            add(f"The {d} of a {c['display']} can be modified.", "business", [sid, "AX_ONLYMOD"])
        # 3. Add a technical identifier when no business identity was stated.
        if c["register"] and not c["identity"] and not c["provided_id"] and not c["combo_identity"] \
                and not [x for x in c["combos"] if not x.get("scope")] \
                and not any(ID_LEX.match(e["display"]) for e in c["props"].values()):
            add(f"Each {c['display']} has a unique identifier supplied by the system.", "business",
                [c["register"], "AX_TECH_KEY"], prov="TECHNICAL")
    # 4. Default to an empty initial state when none is specified.
    for inc in an.incomplete:
        if inc["capability"] == "initial state":
            sug = (inc.get("suggest") or [None])[0]
            if not sug:
                regs = [c for c in an.concepts.values() if c["register"] and c["key"] not in an.initial_concepts]
                if regs:
                    sug = f"Initially, there are no {' or '.join(S.plural(c['display']) for c in regs)}."
            if sug:
                add(sug, "initial", ["AX_EMPTY_START"])
    # 5. Derive each rule-backed rejection outcome and the normal success outcome.
    if any(s.section == "business" for s in new):
        return new
    for cap in an.capabilities:
        c = an.concepts[cap["concept"]]
        for case in cap["cases"]:
            if "outcome" in case or case.get("technical"):
                continue
            add(an.suggest(cap, case), "outcomes", _case_basis(an, c, case) + ["AX_REJECT"], capability=cap["id"])
        if "success_sentence" not in cap:
            src = (c["register"] if cap["kind"] == "register" else cap.get("src_sn") if cap["kind"] == "transition"
                   else next((x for p, x in c["modify"] if p == cap.get("prop")), None))
            add(an.suggest(cap, None), "outcomes", [x for x in [src] if x] + ["AX_SUCCESS"], capability=cap["id"])
    return new


def close(surf, an, reanalyze, max_iter=8):
    """Reach a fixed point by deriving and reanalyzing until no further statements follow."""
    S = _surface()
    log = surf.closure.setdefault("derived", [])
    for _ in range(max_iter):
        new = derive(surf, an)
        if not new:
            break
        surf.sentences.extend(new)
        log += [{"id": s.id, "text": s.text, "section": s.section, "capability": s.capability, "prov": s.prov, "basis": s.basis}
                for s in new]
        for s in surf.sentences:
            s.match_text = None
        an = reanalyze()
    _audit(surf, an)
    surf.closure["counts"] = counts(surf)
    return an


def _words(t):
    return [w for w in re.findall(r"[a-z0-9]+", fold(t))]


def lemma(w: str) -> str:
    """Apply conservative English singularization for provenance matching."""
    return fold(singular(fold(w)))


_VERB_END = ("ed", "ing", "s", "es")


def _state_anchored(term: str, cite_words: list[str]) -> bool:
    """Match a named state to the same English verb in another inflection."""
    cited = {fold(word) for word in cite_words}
    for word in _words(term):
        if len(word) <= 2 or word == "the" or lemma(word) in {lemma(x) for x in cited}:
            continue
        match = re.match(r"^(.{3,}?)(?:ed|ing|s|es)$", word)
        if not (match and any(x.startswith(match.group(1)) and x[len(match.group(1)):] in _VERB_END for x in cited)):
            return False
    return True


def _anchored(term: str, cite_words: list[str]) -> bool:
    cited = {lemma(word) for word in cite_words}
    number_words = "|".join(word for word in NUMW if word not in ("one", "a"))
    counted = {lemma(match) for match in re.findall(r"\b(?:\d+|" + number_words + r")\s+([a-z]{4,})", " ".join(cite_words))}
    if counted & {lemma(word) for word in _words(term)}:
        cited.add("count")
    if cited & {"how", "many"}:
        cited.add("count")
    for word in _words(term):
        if len(word) <= 2 or word in ("each", "every", "registered", "the", "a", "an"):
            continue
        if lemma(word) not in cited:
            return False
    return True


NUMW = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
        "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30,
        "hundred": 100}
_NUM = r"(\d+|" + "|".join(NUMW) + r")"
_MIN_CMP = r"(?:at least|a minimum of|minimum(?: of)?|no less than|cannot be less than)"
_MAX_CMP = r"(?:at most|a maximum of|maximum(?: of)?|up to|no more than|cannot (?:be greater than|exceed))"


def _n(x: str) -> str:
    return str(NUMW.get(x, x))


def cite_numbers(cite: str) -> dict:
    """Extract numeric bounds and direction from a citation; an isolated count noun is not a bound."""
    t = fold(cite)
    mn, mx = set(), set()
    for m in re.finditer(r"\b(?:between|from)\s+" + _NUM + r"\s+(?:and|to)\s+" + _NUM + r"\b", t):
        mn.add(_n(m.group(1))); mx.add(_n(m.group(2)))
    for m in re.finditer(r"\b" + _MIN_CMP + r"\s+" + _NUM + r"\b", t):
        mn.add(_n(m.group(1)))
    for m in re.finditer(r"\b" + _MAX_CMP + r"\s+" + _NUM + r"\b", t):
        mx.add(_n(m.group(1)))
    # v26: “cannot have more than 2” and “nobody can have more than 2” both express a maximum of 2
    for m in re.finditer(r"\b(?:no|nobody|none)\b(?:\s+\w+){0,3}?\s+more than\s+" + _NUM + r"\b", t):
        mx.add(_n(m.group(1)))
    for m in re.finditer(r"\b" + _NUM + r"(?:\s+[a-z]+)?\s+or (greater|higher|less|lower)\b", t):
        (mn if m.group(2) in ("greater", "higher") else mx).add(_n(m.group(1)))
    return {"min": mn, "max": mx, "any": set(re.findall(r"\d+", t)) | mn | mx}


def _number_misses(text: str, cite: str) -> list:
    cn = cite_numbers(cite)
    t = fold(text)
    miss = []
    for n in re.findall(r"\d+", t):
        rel = re.search(rf"\b{n} or (greater|less)\b", t)
        if rel:
            k = "min" if rel.group(1) == "greater" else "max"
            if n not in cn[k]:
                miss.append(n if n not in cn["any"] else f"{n} as a {'minimum' if k == 'min' else 'maximum'}")
        elif n not in cn["any"]:
            miss.append(n)
    return miss


INCORP = re.compile(r"\b(?:register\w*|record\w*|add\w*|create\w*|enroll\w*|enter\w*)\b", re.I)


def _check_cite(surf, an, sn, cite, src, ans, glos):
    """Return None when the citation supports the sentence; otherwise return (message, details)."""
    if not cite:
        return f"«{sn.text}» does not cite what the person said", {"reason": "missing citation"}
    S = _surface()
    parts = S.cite_parts(cite)
    cw, oracion = _words(glos), ""
    for part in parts:
        fc = fold(part).rstrip(". ")
        if not (fc and fc in ans) and fc not in src:
            return f"citation «{part}» does not appear in what the person said", {"reason": "citation not found"}
        cw += _words(part)
        oracion += " " + fc
        if os.environ.get("UPA_NO_V20") != "1" and fc in src:
            # v20: a citation is read within the person’s sentence that contains it (“...and only the email can change”
            # uses “members” from that same sentence as its subject. Numbers still come only from the citation.
            for t_ in surf.input:
                for o_ in re.split(r"(?<=[.!?])\s+", t_):
                    if fc in fold(o_):
                        cw += _words(o_)
                        oracion += " " + fold(o_)
        # a response is understood in the context it clarifies: “From the assessment.” for “Scores range from 1 to 10.”
        ctx = next((f for a_, f in surf.answer_ctx.items() if fc and fc in fold(a_)), None)
        if ctx:
            cw += _words(ctx)
            oracion += " " + fold(ctx)
    cite = " ".join(parts)
    m = next((x for x in an.matched if x["sentence"] == sn.id), None)
    miss = (_number_misses(sn.text, cite) if os.environ.get("UPA_NO_V20") != "1"
            else [n for n in re.findall(r"\d+", sn.text) if n not in re.findall(r"\d+", cite)])
    if m:
        for k_, v in m["semantic_form"].items():
            if k_ in ("c", "cs", "a", "b", "o", "p", "p1", "p2", "props") and isinstance(v, str) and not _anchored(v, cw):
                miss.append(v)
            if k_ in ("sa", "sb", "sx", "vals") and isinstance(v, str) and not _state_anchored(v, cw):
                miss.append(v)
    if m and m["pattern"] == "REGISTER" and os.environ.get("UPA_NO_V20") != "1" and not INCORP.search(oracion):
        # v20: “We rent bicycles” names bicycles but does not say they are registered; the verb must also be anchored
        miss.append("register")
    if miss:
        return f"«{sn.text}» adds {miss}, which do not appear in «{cite}»", {"reason": "unsupported inference", "missing": miss}
    return None


def _audit(surf, an):
    """When input is present, author statements must be supported by a literal citation and anchored terms and numbers."""
    if not surf.input:
        _audit_pending_only(surf, an)
        return
    src = fold(" ".join(surf.input))
    ans = fold(" ".join(surf.answers))
    glos = " ".join(fold(g.text) for g in surf.glossary)
    for sn in surf.sentences:
        if sn.id in surf.inherited or sn.prov not in ("ASSERTED", "NORMALIZED", "RESOLVED") or sn.section not in ("business", "outcomes", "initial", "technical"):
            continue
        # v21: a repeated sentence with another citation adds support; one supporting citation is sufficient
        cites = [sn.cite] + [x for x in getattr(sn, "alt_cites", []) if x != sn.cite]
        fails = []
        for cite in cites:
            r_ = _check_cite(surf, an, sn, cite, src, ans, glos)
            if r_ is None:
                if cite != sn.cite:
                    sn.cite = cite
                if any(fold(x).rstrip(". ") in ans and fold(x).rstrip(". ") not in src for x in _surface().cite_parts(cite)):
                    sn.prov = "RESOLVED"
                fails = []
                break
            fails.append(r_)
        if fails:
            msg, kw = fails[0]
            an.errors.add("UNSUPPORTED_STATEMENT", msg, None, "closure", sentence=sn.text, sid=sn.id, **kw)
    # content stated by the person but cited by no sentence must not be silently lost
    S_ = _surface()
    cited = {lemma(w) for s in list(surf.sentences) + list(surf.glossary) if s.cite for x in S_.cite_parts(s.cite) for w in _words(x)}
    if os.environ.get("UPA_NO_V21") == "1":
        cited |= {lemma(w) for t in surf.answer_for for w in _words(t)}
    else:
        # v21: clarifying something does not formalize it. A clarification counts as covered only if a sentence cites the response;
        # a response cited by no sentence remains “stated but not formalized” (meal-delivery pilot: “if it exceeds 30,
        # pending approval” answered the question and then disappeared).
        cites = [fold(x).rstrip(". ") for s in list(surf.sentences) + list(surf.glossary) if s.cite for x in S_.cite_parts(s.cite)]
        cites += [fold(x).rstrip(". ") for s in surf.sentences for x in getattr(s, "alt_cites", [])]
        for a_ in surf.answers:
            fa = fold(a_)
            if any(x and x in fa for x in cites):
                if a_ in surf.answer_ctx:
                    cited |= {lemma(w) for w in _words(surf.answer_ctx[a_])}
            else:
                lost = [w for w in _words(a_) if len(w) > 3 and w not in STOP and lemma(w) not in cited]
                if lost:
                    surf.closure.setdefault("uncovered", []).append({"words": lost, "input": a_, "kind": "answer"})
    for ack in surf.closure.get("acknowledged", []):
        if not ack["cite"] or fold(ack["cite"]).rstrip(". ") not in src:
            an.errors.add("UNSUPPORTED_STATEMENT", f"«{ack['text']}» does not cite what the person said", None, "closure",
                          sentence=ack["text"], sid=ack["from"], reason="missing citation" if not ack["cite"] else "citation not found")
        else:
            cited |= {lemma(w) for w in _words(ack["cite"])}
    ruled = set()
    for s in surf.sentences:
        if s.section in ("business", "outcomes", "initial", "technical"):
            ruled |= set(re.findall(r"\d+", s.text))
    for i, t in enumerate(surf.input):
        lost = [w for w in _words(t) if len(w) > 3 and w not in STOP and lemma(w) not in cited]
        if os.environ.get("UPA_NO_V21") != "1":
            # v21: a number stated by the person must appear in a rule; citing the longer sentence that contains it
            # is not enough (“about thirty meals at most” was covered by the registration sentence)
            lost += [w for w in _words(t) if (w.isdigit() or (w in NUMW and w not in ("a", "an", "one", "two")))
                     and _n(w) not in ruled and w not in lost]
        if lost:
            # v15 library pilot: treating this as a blocking error prompted the agent to invent glossary definitions (“We have means...” ).
            # Keep it visible in the guide and corpus without blocking: nothing is silently lost or invented to cover it.
            u_ = {"words": lost, "input": t}
            if os.environ.get("UPA_NO_V23") != "1":
                cits = [fold(x).rstrip(". ") for s in list(surf.sentences) + list(surf.glossary) if s.cite for x in S_.cite_parts(s.cite)]
                u_["rule_clauses"] = _rule_clauses(t, lost, cits)
            surf.closure.setdefault("uncovered", []).append(u_)
    _audit_pending_only(surf, an)


# v23: deterministic markers that a sentence states a rule rather than context. Meal-delivery (v21) and room-booking (v23) pilots: the author
# could reach READY with “I have to approve it” and “even if they are cancelled” treated as context, without asking the person.
_OBLIG = re.compile(r"\b(have to|has to|must|cannot|can not|is required to|are required to|prohibited|only if|provided that|"
                    r"required\w*|prohibited\w*|only if|provided that)\b")
_LIFE = ("approv", "cancel", "reject", "void", "expir", "suspend", "block", "enabl", "reactivat", "confirm", "return", "clos")


def _rule_clauses(t: str, lost: list[str], cites: list[str] = ()) -> list[dict]:
    """Find uncovered input clauses marked as rules by an obligation, lifecycle verb, or number."""
    lost_f = {fold(w) for w in lost}
    out = []
    for cl in re.split(r"(?<=[.;!?])\s+", t.strip()):
        cw = [fold(w) for w in _words(cl)]
        lw = [w for w in cw if w in lost_f]
        if not lw:
            continue
        marks = [w for w in lw if w.isdigit() or (w in NUMW and w not in ("a", "an", "one", "two"))]
        marks += [w for w in lw if w.startswith(_LIFE)]
        m = _OBLIG.search(fold(cl))
        # an obligation alone is insufficient if a rule already cites the sentence: only loose words remain, not a missing rule
        if m and len(lw) >= 2 and not any(x and x in fold(cl) for x in cites):
            marks.append(m.group(1))
        if marks:
            out.append({"clause": cl.strip(), "marks": list(dict.fromkeys(marks))})
    return out


STOP = {"a", "an", "the", "and", "or", "but", "as", "when", "where", "this", "these", "those", "only", "can", "could", "has", "have",
        "all", "each", "about", "between", "from", "until", "also", "always", "never", "some", "any", "other", "others", "same",
        "must", "want", "need", "we", "our", "system", "do", "make", "use", "after", "please", "specification", "repeat", "there"}


def _audit_pending_only(surf, an):
    for p_ in surf.closure.get("pending", []):
        if p_["kind"] == "MOD_ANY" and not p_.get("done"):
            an.errors.add("AMBIGUOUS_MODIFY", f"«{p_['sn'].text}»: it is unclear what can be modified", None, "closure",
                          sentence=p_["sn"].text, concept=p_["concept"], candidates=p_.get("candidates", []))


def counts(surf) -> dict:
    own = [s for s in surf.sentences if s.id not in surf.inherited]
    by = {}
    for s in own:
        by[s.prov] = by.get(s.prov, 0) + 1
    by["AUTHOR_INPUT"] = len({s.basis[0] if s.basis else s.id for s in own if s.prov in ("ASSERTED", "NORMALIZED")})
    by["AUTHOR_ANSWERS"] = len({s.basis[0] if s.basis else s.id for s in own if s.prov == "RESOLVED"})
    by["RESOLVED_DECISIONS"] = sum(1 for d in surf.decisions if d.get("resolves"))
    return by
