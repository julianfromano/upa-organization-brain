"""Conversation guide: turns analysis state into the next question the agent should ask the analyst.

The guide agent does not guess: it asks only what the compiler and inherited knowledge (level-specific glossaries) cannot
resolve, one question at a time; each option provides the exact sentence to write in the surface."""
from __future__ import annotations
import os
import json
import re
from pathlib import Path

from .errors import UpaError
from .surface import agree, analyze, article, classify_question, norm_s, plural

PRIORITY = {"AGGREGATE_AS_PROPERTY": 2, "AMBIGUOUS_TERM": 1, "TERM_CONFLICT": 1, "SYNONYM_CANDIDATE": 1, "GLOSSARY_UNSUPPORTED": 1,
            "AMBIGUOUS_MODEL": 2, "AMBIGUOUS_RULE": 3, "UNSUPPORTED_PATTERN": 3, "UNRESOLVED_BUSINESS_TERM": 3,
            "CAPABILITY_RULE_CONFLICT": 3, "MISSING_CAPABILITY": 3, "UNSUPPORTED_STATEMENT": 2, "UNCOVERED_INPUT": 5, "AMBIGUOUS_MODIFY": 3, "UNOWNED_PROPERTY": 2, "UNMATCHED_OUTCOME": 3, "SURFACE_ERROR": 3, "CORE_ERROR": 3, "STATE_SCOPE": 3, "UNCOVERED_RULE": 3, "LIFECYCLE_VERB": 3, "INCOMPLETE_CAPABILITY": 4, "NEW_TERM": 5, "OPEN_QUESTION": 6, "REDUNDANT_SENTENCE": 7}


def _consequence(an, sid) -> str:
    """What the person needs to know to confirm: a formal consequence not shown by the sentence alone."""
    m = next((x for x in an.matched if x["sentence"] == sid), None)
    if not m or m["pattern"] != "CAPACITY":
        return ""
    for c in an.concepts.values():
        cp = c.get("capacity")
        if cp and c.get("no_delete"):
            from .surface import plural
            pl = plural(c["display"])
            return (f" Consequence: because registered {pl} are never deleted or terminated, the limit counts all"
                    f" {pl} ever registered, not only those currently active.")
    return ""


def _q(id_, type_, question, why, options=None, evidence=None, blocking=True):
    return {"id": id_, "type": type_, "priority": PRIORITY[type_], "blocking": blocking, "question": agree(question),
            "why": why, "options": options or [], "evidence": evidence or []}


def base_summary(surf, an) -> dict | None:
    """What the agent needs to know about the base specifications (use spec) without reading their text: concepts, identity,
    properties, relationships, capabilities, and outcome names. Inherited rules have already been verified."""
    if not surf.bases:
        return None
    inh = set(surf.inherited)
    own = {s.id for s in surf.sentences if s.id not in inh}
    concepts = {}
    for k, c in an.concepts.items():
        if c["sources"] and all(x in own for x in c["sources"]):
            continue
        concepts[c["display"]] = {"identity": (c["identity"] or [None])[0] or ("combination" if c.get("combo_identity") else None),
                                  "props": sorted(c["props"]), "refs": sorted(an.concepts[r["target"]]["display"] for r in c["refs"])}
    caps = {cap["id"]: sorted({x["outcome"]["name"] for x in cap["cases"] if "outcome" in x})
            for cap in an.capabilities if any(s in inh for s in cap.get("declared", []))}
    # focus: inherited items touched by this increment (mentioned in its sentences or referenced by its concepts) are detailed;
    # for the rest, include only the name so the agent knows it exists and does not duplicate it (v12)
    own_txt = " ".join(norm_s(s.text) for s in surf.sentences if s.id in own)
    touched = set()
    for k, c in an.concepts.items():
        mine = any(x in own for x in c["sources"])
        if mine or re.search(r"\b" + re.escape(norm_s(c["display"])) + r"s?\b", own_txt):
            touched.add(c["display"])
            if mine:
                touched |= {an.concepts[r["target"]]["display"] for r in c["refs"]}
    focus = {k: v for k, v in concepts.items() if k in touched}
    others = sorted(k for k in concepts if k not in touched)
    caps_focus = {k: v for k, v in caps.items() if any(re.search(r"\b" + re.escape(norm_s(t)) + r"\b", norm_s(k)) for t in touched)}
    return {"specs": surf.bases, "concepts": concepts, "capabilities": caps,
            "focus": {"concepts": focus, "capabilities": caps_focus, "other_concepts": others},
            "rules_inherited": sum(1 for s in surf.sentences if s.id in inh and s.section in ("business", "technical"))}


def guide(src: str, path: Path | None = None) -> dict:
    try:
        surf, an = analyze(src, path)
    except UpaError as e:
        return {"stage": "SYNTAX", "ready_to_build": False, "questions": [], "next_question": None,
                "syntax_error": e.to_json(), "instruction": "fix the surface syntax before asking any questions"}
    tm = an.terminology
    qs, seen = [], set()
    for f in tm.findings:
        qs.append(_q(f["id"], f["type"], f["question"], f["why"], f["options"], f.get("evidence")))
        seen.add(f["id"])
    for e in an.errors.items:
        if e.code == "GLOSSARY_UNSUPPORTED":
            qs.append(_q(f"GLOS_{len(qs)}", "GLOSSARY_UNSUPPORTED", f"A glossary sentence has no recognized form: {e.extra.get('source')!r}",
                         "The glossary supports only catalog forms.",
                         [{"answer": "rephrase", "writes": p} for p in e.extra.get("closest_patterns", [])]))
    handled = {"UNCOVERED_INPUT", "UNSUPPORTED_STATEMENT", "AMBIGUOUS_MODIFY", "MISSING_CAPABILITY", "GLOSSARY_UNSUPPORTED", "AMBIGUOUS_RULE", "UNSUPPORTED_PATTERN", "UNRESOLVED_BUSINESS_TERM", "INCOMPLETE_CAPABILITY",
               "AMBIGUOUS_MODEL", "AMBIGUOUS_TERM", "TERM_CONFLICT", "SYNONYM_CANDIDATE"}
    for n, e in enumerate(an.errors.items):
        if e.code == "CAPABILITY_RULE_CONFLICT":
            ss = e.extra.get("sentences", [])
            cites = "; ".join(f"«{s['text']}» ({s['spec']})" for s in ss)
            rule = ss[0] if ss else None
            opts = [{"answer": "capability does not apply: remove it", "writes": f"(remove «{s['text']}»)"} for s in ss[1:]
                    if s["spec"] == "this specification"]
            if rule and rule["spec"] != "this specification":
                opts.append({"answer": f"the rule in {rule['spec']} changed", "writes":
                             f"(new version of {rule['spec']}: an increment cannot change an inherited rule)"})
            elif rule:
                opts.append({"answer": "the rule changed", "writes": f"(replace «{rule['text']}»)"})
            qs.append(_q(f"CONF_{n}", "CAPABILITY_RULE_CONFLICT",
                         f"These sentences conflict: {cites}. Which one applies?",
                         "A capability cannot violate a rule; the compiler will not choose which one takes precedence.", opts, [s["text"] for s in ss]))
        elif e.code == "UNSUPPORTED_STATEMENT":
            # v15: content the person did not state is never silently added; ask using the exact sentence
            why = {"missing citation": "The sentence does not cite what the person said.", "citation not found": "The citation does not appear in what the person said.",
                   "unsupported inference": f"The sentence adds {e.extra.get('missing')}, which the citation does not mention; this is an interpretation."}[e.extra["reason"]]
            why += _consequence(an, e.extra.get("sid"))
            qs.append(_q(f"SUP_{e.extra.get('sid')}", "UNSUPPORTED_STATEMENT",
                         f"Is this correct?: «{e.extra.get('sentence')}»", why + " If the person confirms it, their exact response goes in `answers` and "
                         "the sentence cites it (RESOLVED provenance).",
                         [{"answer": "yes (the person confirms it)", "writes": f'answers {{ "<exact response>" for "{e.extra.get("sentence")}" }} and cite it with from "<exact response>"'},
                          {"answer": "no", "writes": f"(remove «{e.extra.get('sentence')}»)"}], [e.extra.get("sentence")]))
        elif e.code == "UNCOVERED_INPUT":
            pass
        elif e.code == "AGGREGATE_AS_PROPERTY":
            qs.append(_q(f"AGG_{e.extra['concept']}_{e.extra['prop']}", "AGGREGATE_AS_PROPERTY", e.message,
                         "A count is not an input field: if it were a property, the system could report «2» when there are 5. "
                         "The bound applies to the records being counted (and, if only some count, to their state).",
                         [{"answer": "replace", "writes": f"A {e.extra['concept']} cannot have more than <n> {e.extra['counted']} [in state <X>]."}]))
        elif e.code == "AMBIGUOUS_MODIFY":
            cands = e.extra.get("candidates") or []
            indefinite = article(e.extra["concept"].split()[0])
            qs.append(_q(f"AMOD_{len(qs)}", "AMBIGUOUS_MODIFY", f"«{e.extra.get('sentence')}»: what can be modified on {e.extra['concept']}?",
                         "More than one property is possible (or no property is known); the compiler will not choose.",
                         [{"answer": p_, "writes": f"The {p_} of {e.extra['concept']} can be modified."} for p_ in cands]))
        elif e.code == "UNMATCHED_OUTCOME":
            qs.append(_q(f"UNM_{n}", "UNMATCHED_OUTCOME",
                         (f"{e.message}. Which one applies?" if e.extra.get("twin") else
                          f"{e.message}: no other rule produces this situation. Should this be removed, or is the rule that produces it missing?"),
                         "A sentence that does not become a guarantee cannot remain in the specification as if it were valid. "
                         f"Existing cases: {', '.join(e.extra.get('cases', [])) or 'none'}.",
                         ([{"answer": "the first applies", "writes": f"(remove «{e.extra.get('sentence')}»)"},
                           {"answer": "the second applies", "writes": f"(remove «{e.extra.get('twin')}»)"}] if e.extra.get("twin") else
                          [{"answer": "remove it", "writes": f"(remove «{e.extra.get('sentence')}»)"},
                           {"answer": "a rule is missing", "writes": "(add the business rule that makes this situation possible)"}]),
                         [e.extra.get("sentence")]))
        elif (e.code not in handled or (e.code == "UNRESOLVED_BUSINESS_TERM" and not any(u["error"] == e.code for u in an.unresolved))) \
                and e.code not in {f["type"] for f in tm.findings}:
            qs.append(_q(f"ERR_{n}", "SURFACE_ERROR", f"The compiler cannot continue: {e.message}",
                         "Surface error without a dedicated question yet; report it as-is rather than falsely declaring READY.",
                         None, None))
    for a in an.ambiguities:
        if a["status"] == "pending_human":
            alts = a.get("candidates") or a.get("alternatives") or []
            names = [x if isinstance(x, str) else x.get("id", str(x)) for x in alts]
            qs.append(_q(a["id"], "AMBIGUOUS_MODEL", a["reason"][0].upper() + a["reason"][1:] + ". Which one is correct?",
                         "Different answers produce different models and guarantees; the compiler will not choose silently.",
                         [{"answer": n, "writes": f'decision DD_{a["id"]} "<rationale>" resolves {a["id"]} = {n}'} for n in names]))
    for u in an.unresolved:
        if u["error"] == "AMBIGUOUS_RULE":
            qs.append(_q(f"AMBR_{u['sentence']}", "AMBIGUOUS_RULE",
                         f"«{u['source']}» does not say what is observed. Which concrete outcome is valid and which is invalid?",
                         u["reason"], [{"answer": "provide the valid and invalid cases", "writes": "(one sentence per outcome, using the outcome patterns)"}],
                         [u["source"]]))
        elif u["error"] == "UNSUPPORTED_PATTERN":
            qs.append(_q(f"UNSP_{u['sentence']}", "UNSUPPORTED_PATTERN",
                         f"I cannot express «{u['source']}» using the catalog forms. Can it be rephrased, or is it a new rule?",
                         u["reason"], [{"answer": "rephrase", "writes": p} for p in u.get("closest_patterns", [])], [u["source"]]))
        elif u["error"] == "UNRESOLVED_BUSINESS_TERM" and u.get("unowned_property"):
            t = u.get("term")
            if f"UNOW_{norm_s(t)}" in {x["id"] for x in qs}:
                continue
            qs.append(_q(f"UNOW_{norm_s(t)}", "UNOWNED_PROPERTY", f"What does {t} belong to?",
                         "A property without a concept cannot be placed; its owner will not be guessed.",
                         [{"answer": c_, "writes": f"Every {c_} has {t}."} for c_ in u.get("candidates", [])]))
        elif u["error"] == "UNRESOLVED_BUSINESS_TERM":
            qs.append(_q(f"UNRT_{len(qs)}", "UNRESOLVED_BUSINESS_TERM",
                         f"What does {u.get('term')!r} mean to the business? It is not a concept or an inherited glossary term.",
                         "Without knowing what it is, I cannot place it in the model.", [{"answer": "define it", "writes": f"Every <concept> has {u.get('term')}."}]))
    for e in an.errors.items:
        if e.code != "MISSING_CAPABILITY":
            continue
        key, cpt = e.extra["concept"], an.concepts[e.extra["concept"]]
        w = cpt["display"].split()
        pl = " ".join([plural(w[0])] + w[1:])
        indefinite = article(w[0])
        if e.extra["kind"] == "register":
            qs.append(_q(f"MCAP_{key}", "MISSING_CAPABILITY",
                         f"No sentence says how {pl} are registered. Are they registered in this specification?",
                         "Without a capability to create them, their rules hold vacuously: nothing exercises them.",
                         [{"answer": "yes, they are registered here", "writes": f"{pl.capitalize()} can be registered."},
                          {"answer": "they are registered in another specification", "writes":
                           f'use spec "<specification that registers {pl}>"  (if no such specification exists: decision DD_SOURCE_{key} "{pl} are registered in <system>")'}],
                         None))
        else:
            prop = cpt["props"].get(cpt["only_mod"][0], {}).get("display", cpt["only_mod"][0])
            prop = "the " + prop
            qs.append(_q(f"MMOD_{key}", "MISSING_CAPABILITY",
                         f"It was stated that only {prop} of {cpt['display']} can be modified, but no sentence says that it can. Is modification allowed?",
                         "'Only' restricts what can be modified; it does not enable modification.",
                         [{"answer": "yes", "writes": f"{prop.capitalize()} of a {cpt['display']} can be modified."},
                          {"answer": "no, modification is not allowed", "writes": agree(f"(replace with «A registered {cpt['display']} cannot be modified.»)")}],
                         None))
    for c in an.incomplete:
        for i, m in enumerate(c["missing"]):
            sug = (c.get("suggest") or [])[i] if i < len(c.get("suggest") or []) else ""
            qtext = (f"«{c['capability']}»: what is the {m}?" if m.startswith("outcome")
                     else f"«{c['capability']}»: {m} is missing. What does the business require?")
            qs.append(_q(f"CAP_{len(qs)}", "INCOMPLETE_CAPABILITY", qtext,
                         "Every capability must declare its outcomes; success or rejection is never assumed.",
                         [{"answer": "declare the initial state", "writes": f'initial {{ "{sug}" }}'}]
                         if sug and c["capability"] == "initial state" else
                         [{"answer": "reject without changes (suggested name; editable)", "writes": f'outcomes for "{c["capability"]}" {{ "{sug}" }}'}]
                         if sug else
                         [{"answer": "declare an outcome", "writes": 'outcomes for "' + c["capability"] + '" { "If <condition>, the result is <Name> and no information changes." }'}]))
    bsum = base_summary(surf, an)
    inherited_terms = {norm_s(k) for k in (bsum or {}).get("concepts", {})}
    for t in tm.new_terms:
        if norm_s(t["term"]) in inherited_terms:
            continue  # already in the base specification; do not ask again in an increment
        qs.append(_q(f"NEW_{norm_s(t['term']).replace(' ', '_')}", "NEW_TERM", t["question"],
                     "An undefined new term may be confused with another term (such as user/customer).",
                     [{"answer": "define it", "writes": t["writes"]}], blocking=False))
    for n_, u in enumerate(surf.closure.get("uncovered", [])):
        qs.append(_q(f"UNC_{n_}", "UNCOVERED_INPUT", f"The person mentioned {u['words']}, but no sentence captures it. Does it add a rule?",
                     "Do not silently discard anything the person said or invent a rule or definition to cover it.",
                     [{"answer": "it is a rule", "writes": '(use a catalog sentence with from "<excerpt>")'},
                      {"answer": "it is context and adds no rule", "writes": 'nothing (it remains listed in the corpus as not formalized)'}],
                     [u["input"]], blocking=False))
    # v23: an unformalized sentence marked as a rule (obligation, lifecycle, or number) is not context until the person
    # says so. A response about the sentence (or an excerpt from it) closes the question, even if it does not formalize it.
    _ctx = [norm_s(f).rstrip(". ") for f in surf.answer_ctx.values()]
    for n_, u in enumerate(surf.closure.get("uncovered", [])):
        for k_, rc in enumerate(u.get("rule_clauses", [])):
            ncl = norm_s(rc["clause"]).rstrip(". ")
            if any(f and (f in ncl or ncl in f) for f in _ctx):
                continue
            qs.append(_q(f"RULE_{n_}_{k_}", "UNCOVERED_RULE",
                         f"The person said «{rc['clause']}», but no rule captures it ({', '.join(rc['marks'])}). What must the system do?",
                         "This sounds like a rule (obligation, state transition, or numeric bound), not context. Do not decide on the person’s behalf: if it is a rule, formalize it "
                         "(states, bounds, or catalog outcomes); if it is context, ask the person to confirm.",
                         [{"answer": "it is a rule", "writes": f'(catalog sentences citing "{rc["clause"]}" or the response)'},
                          {"answer": "it does not change what the system does", "writes": f'answers {{ "<exact response>" for "{rc["clause"]}" }}'}],
                         [rc["clause"]]))
    for r_ in getattr(surf, "redundant", []):
        qs.append(_q(f"RED_{r_['id']}", "REDUNDANT_SENTENCE", (f"«{r_['text']}» is already stated ({r_['spec']}); this repetition adds another supporting citation, not a new rule."
                   if r_.get("adds_cite") else f"«{r_['text']}» is already stated ({r_['spec']}). Can it be removed?"),
                     "Repeating a rule does not add guarantees; it is used once.", [{"answer": "remove it", "writes": f"(remove «{r_['text']}»)"}],
                     [r_["text"]], blocking=False))
    for q_ in surf.questions:
        cl = classify_question(q_, an, {})
        qs.append(_q(q_["id"], "OPEN_QUESTION", q_["text"], f"classified as {cl['classification']}", None, None, blocking=False))
    # v22: with states, “there cannot be two X with ...” has materially different formalizations (in any state,
    # or only among records not in a final state). Ask unless a response already clarifies the scope.
    sents = {s.id: s for s in surf.sentences}
    clarified_input = {norm_s(fragment).rstrip(". ") for fragment in surf.answer_ctx.values()}
    for c in an.concepts.values():
        st = c.get("states") or {}
        if len(st.get("values", [])) < 2 or (os.environ.get("UPA_NO_V23") != "1" and not st.get("trans")):
            continue  # v23: without transitions, final states cannot be identified; complete the lifecycle first
        source_states = {state for transitions in (st.get("trans") or {}).values() for state, _ in transitions}
        final_states = [state for state in st["values"] if state not in source_states] or [state for state in st["values"] if state != (st.get("initial") or {}).get("a")]
        for cb in c["combos"]:
            s_ = sents.get(cb["sn"])
            if not s_ or s_.id in surf.inherited:
                continue
            if cb.get("scope"):
                # v24: a scope that excludes a final state creates the same ambiguity unless the person mentioned that state
                from .surface import cite_parts, scope_list
                if os.environ.get("UPA_NO_V24") == "1":
                    continue
                cited_text = norm_s(" ".join(cite_parts(s_.cite))) if s_.cite else ""
                remaining_states = [state for state in final_states if state not in scope_list(cb) and norm_s(state)[:5] not in cited_text]
                if not remaining_states:
                    continue
                from .surface import combo_phrase, plural as _pl
                all_states = scope_list(cb) + remaining_states
                qs.append(_q(f"SCOPE_{cb['sn']}", "STATE_SCOPE",
                             agree(f"«{s_.text}»: does {article(c['display'])} {c['display']} in state {' or '.join(remaining_states)} count for this rule?"),
                             f"{' and '.join(remaining_states)} is also a final state, and the rule does not say whether it counts. If it does, it occupies a slot permanently.",
                             [{"answer": "yes, it counts", "writes": f'answers {{ "<exact response>" for "{s_.text}" }}'},
                              {"answer": "no, it does not count either",
                               "writes": f"Among {_pl(c['display'])} not in state {' or '.join(all_states)}, there cannot be two with {combo_phrase(cb)}."}],
                             [s_.text]))
                continue
            responses = [answer for answer, fragment in surf.answer_ctx.items()
                         if norm_s(fragment).rstrip(". ") == norm_s(s_.text).rstrip(". ")]
            if os.environ.get("UPA_NO_V23") == "1":
                if norm_s(s_.text).rstrip(". ") in clarified_input:
                    continue
            else:
                # A clarification closes the question only when a sentence cites the response.
                # A response saying the records do not count conflicts with the unscoped rule.
                cited_answers = [answer for answer in responses if _cited_answer(surf, answer)]
                if cited_answers and not any(_NEG_SCOPE.search(norm_s(answer)) for answer in cited_answers):
                    continue
            from .surface import combo_phrase, plural as _pl
            final_state_text = " or ".join(final_states)
            prior_answer_notice = "" if os.environ.get("UPA_NO_V23") == "1" or not responses else (
                f" The person already answered «{responses[0]}»: do not ask again; formalize the response using the option that"
                " applies, citing it with from (if the answer says they do not count, it conflicts with the rule without a scope).")
            qs.append(_q(f"SCOPE_{cb['sn']}", "STATE_SCOPE",
                         agree(f"«{s_.text}»: does {article(c['display'])} {c['display']} in state {final_state_text} count for this rule?") + prior_answer_notice,
                         f"{c['display']} has states; the rule may apply in any state or only to records not in state {final_state_text}. "
                         "These are different rules (under the first, a record in state " + final_state_text + " prevents another matching record from being registered).",
                         [{"answer": "yes, it counts in every state", "writes": f'answers {{ "<exact response>" for "{s_.text}" }}'}] +
                         [{"answer": f"no, records in state {f_} do not count",
                           "writes": f"Among {_pl(c['display'])} not in state {f_}, there cannot be two with {combo_phrase(cb)}."}
                          for f_ in final_states] +
                         ([{"answer": f"no, records in state {' or '.join(final_states)} do not count",
                            "writes": f"Among {_pl(c['display'])} not in state {' or '.join(final_states)}, there cannot be two with {combo_phrase(cb)}."}]
                          if len(final_states) > 1 else []), [s_.text]))
    if os.environ.get("UPA_NO_V23") != "1":
        qs += _lifecycle_questions(surf, an)
    if not any(q["blocking"] for q in qs) and not an.errors and not an.incomplete:
        # v21: do not promise VERIFIED based on READY without checking Core: failure to compile generated Core is a compiler defect,
        # not a question for the author; block the build rather than falsely declaring READY
        try:
            from .compiler import compile_source
            from .surface import CoreGen
            _, _, _, cerr = compile_source(CoreGen(an).generate(), None)
            for n_, e in enumerate(cerr.items):
                qs.append(_q(f"CORE_{n_}", "CORE_ERROR", f"The generated Core does not compile: {e.message}",
                             "Compiler defect (not the person’s fault): do not declare READY until it is fixed.", None, None))
        except UpaError as e:
            qs.append(_q("CORE_X", "CORE_ERROR", f"The generated Core does not compile: {e.message}",
                         "Compiler defect (not the person’s fault): do not declare READY until it is fixed.", None, None))
    if os.environ.get("UPA_NO_V25") != "1":
        qs = _group_transitions(qs, an)
    qs.sort(key=lambda x: (x["priority"], x["id"]))
    blocking = [q for q in qs if q["blocking"]]
    stage = ("TERMINOLOGY" if any(q["priority"] == 1 for q in blocking) else
             "MODEL" if any(q["priority"] == 2 for q in blocking) else
             "RULES" if any(q["priority"] == 3 for q in blocking) else
             "OUTCOMES" if any(q["priority"] == 4 for q in blocking) else "READY")
    return {
        "spec": surf.name, "stage": stage, "ready_to_build": not blocking,
        "already_known": {"base_specs": bsum, "glossary_sources": tm.g.sources, "known_terms": tm.report()["known_terms"],
                          "normalizations_applied": tm.normalizations, "reuse_candidates": tm.report()["reuse_candidates"]},
        "questions": qs, "next_question": blocking[0] if blocking else (qs[0] if qs else None),
        "do_not_ask": ["data types", "technical identifiers", "operation names", "guards and state",
                       "implementation technology", "anything already listed in already_known"],
        "recognized_sentences": len(an.matched), "total_sentences": len(surf.sentences),
    }


# v23: response to STATE_SCOPE saying that records in the state do not count
_NEG_SCOPE = re.compile(r"\b(?:does not|do not|cannot) count\b|\bthere can be another\b|\bcan be registered again\b|\bdoes not prevent\b")
_LIFE_VERBS = {"approv": "approve", "cancel": "cancel", "reject": "reject", "void": "void", "expir": "expire",
               "suspend": "suspend", "block": "block", "reactivat": "reactivate", "return": "return",
               "clos": "close", "enabl": "enable"}


def _cited_answer(surf, a_) -> bool:
    from .surface import cite_parts
    fa = norm_s(a_).rstrip(". ")
    return any(norm_s(x).rstrip(". ") in fa for s in surf.sentences if s.cite for x in cite_parts(s.cite) if x.strip())


def _lifecycle_questions(surf, an) -> list:
    """A lifecycle verb stated by the person (approve, reject, etc.) about a concept with states must appear in its lifecycle:
    it must be cited by a transition or appear as a state name (past participle). Otherwise, ask what state it leads to."""
    from .surface import cite_parts
    with_states = [c for c in an.concepts.values() if len((c.get("states") or {}).get("values", [])) > 1]
    if not with_states:
        return []
    said = " ".join(list(surf.input) + list(surf.answers))
    toks = re.findall(r"[^\W\d_]+", norm_s(said))
    # A negated verb (for example, “not rejected”) does not name an operation.
    words = {w for i, w in enumerate(toks) if not (i and toks[i - 1] in ("not", "never")) }
    pats = {k: v for k, v in _LIFE_VERBS.items() if any(w.startswith(k) for w in words)}
    sents = {s.id: s for s in surf.sentences}
    trans_cites = " ".join(norm_s(x) for c in with_states for fr in (c["states"].get("trans") or {}).values() for _, sid in fr
                           if sid in sents and sents[sid].cite for x in cite_parts(sents[sid].cite))
    states = [norm_s(w) for c in with_states for v in c["states"]["values"] for w in v.split()]
    out, seen = [], set()
    for stem, verb in pats.items():
        if verb in seen:
            continue
        stems = [k for k, v in _LIFE_VERBS.items() if v == verb]
        if any(re.search(r"\b" + k, trans_cites) for k in stems):
            continue
        if any(s_.startswith(k) and re.search(r"(?:ed|ing)$", s_) for s_ in states for k in stems):
            continue
        seen.add(verb)
        c = with_states[0]
        d = c["display"]
        indefinite = article(d)
        st = c["states"]
        opts = [{"answer": f"from {a_} to {b_}", "writes": f"A {d} in state {a_} can transition to state {b_}."}
                for a_ in st["values"] for b_ in st["values"] if a_ != b_][:6]
        frag = next((t for t in list(surf.input) + list(surf.answers) if any(k in norm_s(t) for k in stems)), "")
        out.append(_q(f"LIFE_{verb}", "LIFECYCLE_VERB",
                      f"The person said «{verb}», but no state transition captures it. When {verb}ing {indefinite} {d}, what state is it in and what state does it move to?",
                      "A lifecycle verb used by the author describes a business operation. Without a transition, the system cannot perform it "
                      "(every undeclared transition is prohibited). If that is not intended, ask the author to clarify.",
                      opts + [{"answer": "it is not a system operation", "writes": f'answers {{ "<exact response>" for "{frag[:80]}" }}'}],
                      [frag]))
    return out


def _group_transitions(qs, an):
    """v25: multiple unsupported transitions to the same state form one question ("from which states can it move to B?").
    The person answers once, and each transition cites that response. The question is unchanged; only its repetition is removed."""
    tr = {}
    for m in an.matched:
        if m["pattern"] == "TRANSITION":
            f_ = m["semantic_form"]
            tr[m["sentence"]] = (norm_s(f_.get("c", "")), norm_s(f_.get("sb", "")), f_.get("sa", ""), f_.get("c", ""), f_.get("sb", ""))
    groups = {}
    for q in qs:
        sid = q["id"][4:] if q["type"] == "UNSUPPORTED_STATEMENT" else None
        if sid in tr:
            groups.setdefault(tr[sid][:2], []).append((q, tr[sid]))
    out, done = [], set()
    for q in qs:
        sid = q["id"][4:] if q["type"] == "UNSUPPORTED_STATEMENT" else None
        g = groups.get(tr[sid][:2]) if sid in tr else None
        if not g or len(g) < 2:
            out.append(q)
            continue
        if tr[sid][:2] in done:
            continue
        done.add(tr[sid][:2])
        _, _, _, cpt, sb = g[0][1]
        origins = [x[1][2] for x in g]
        phrases = [x[0]["evidence"][0] for x in g]
        indefinite = article(cpt.split()[0])
        out.append(_q(f"SUPG_{norm_s(cpt)}_{norm_s(sb)}", "UNSUPPORTED_STATEMENT",
                      f"From which states can {cpt} move to {sb}? (proposed: from {' and from '.join(origins)})",
                      "These transitions share one question and one response. Each confirmed transition cites that response "
                      "(RESOLVED provenance); remove any transition the person does not confirm.",
                      [{"answer": "from all proposed states", "writes": f'answers {{ "<exact response>" for "{phrases[0]}" }} and cite every transition with from "<exact response>"'},
                       {"answer": "from some", "writes": "the confirmed transitions cite the response; remove the others"}],
                      phrases))
    return out
