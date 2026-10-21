"""Export an explainable corpus: why UPA believes each rule.

Turn a verified surface into knowledge an AI or person can use without rereading the conversation: concepts,
statements with provenance, and for each formal rule the chain from the original statement through normalization
and derivation to the rule, its proof obligations, and their status. No knowledge is added; everything comes from
the surface, its closure, and the verification result.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import surface as S


def _load(out: Path, name: str):
    f = out / name
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def build_corpus(path: Path, out: Path, timeout_ms: int = 120000, rebuild: bool = True) -> dict:
    if rebuild or not (out / "verification-result.json").exists():
        S.build_surface(path, out, timeout_ms)
    surf, an = S.analyze(path.read_text(encoding="utf-8"), path)
    prov = _load(out, "provenance.json") or {}
    vr = _load(out, "verification-result.json") or {}
    model = _load(out, "inferred-model.json") or {}

    stmts = {}
    for s in surf.sentences:
        stmts[s.id] = {"id": s.id, "text": s.text, "section": s.section, "provenance": s.prov, "cite": ("» + «".join(S.cite_parts(s.cite)) if s.cite else s.cite),
                       "basis": list(s.basis or []), "inherited_from": surf.inherited.get(s.id)}
    for n in surf.closure.get("normalized", []):
        stmts.setdefault(n["from"], {"id": n["from"], "text": n["text"], "section": "business", "provenance": "AUTHOR_INPUT_FORM",
                                     "cite": n.get("cite"), "basis": [], "normalized_by": n["form"], "inherited_from": None})
    from .closure import AXIOMS, INPUT_FORMS
    forms = {f[0]: f[2] for f in INPUT_FORMS}

    def chain(sid, seen=None):
        seen = seen or set()
        if sid in seen:
            return []
        seen.add(sid)
        if sid in AXIOMS:
            return [{"step": "language axiom", "id": sid, "text": AXIOMS[sid]}]
        if sid in forms:
            return [{"step": "input form", "id": sid, "text": forms[sid]}]
        st = stmts.get(sid)
        if not st:
            return [{"step": "unknown", "id": sid}]
        node = {"step": st["provenance"], "id": sid, "text": st["text"]}
        if st.get("cite"):
            node["quoted_as"] = st["cite"]
        if st.get("inherited_from"):
            node["inherited_from"] = st["inherited_from"]
        return [node] + [x for b in st["basis"] for x in chain(b, seen)]

    obls = {}
    for o in vr.get("obligations", []):
        obls.setdefault(o.get("rule"), []).append({"id": o["id"], "kind": o["kind"], "operation": o.get("operation"), "status": o["status"]})
    rules = []
    for rid, p in sorted(prov.items()):
        if not isinstance(p, dict) or p.get("classification") != "EXPLICIT_RULE":
            continue
        src = p.get("source") or {}
        sid = src.get("sentence_id")
        os_ = obls.get(rid, [])
        st = sorted({o["status"] for o in os_})
        rules.append({"rule": rid, "pattern": p.get("pattern"), "statement": sid, "phrase": src.get("phrase"),
                      "chain": chain(sid) if sid else [], "formalization": p.get("inference_steps", []),
                      "obligations": os_, "status": st[0] if len(st) == 1 else ("MIXED" if st else "NO_OBLIGATION")})
    ops = _load(out, "generated-outcomes.json") or []
    meaning = {}

    def _walk(x):
        if isinstance(x, dict):
            o = x.get("origin")
            if isinstance(o, dict) and o.get("ref") and x.get("text") and x.get("group") == "technical":
                meaning[o["ref"]] = x["text"]
            for v in x.values():
                _walk(v)
        elif isinstance(x, list):
            for v in x:
                _walk(v)
    _walk(_load(out / "core", "canonical-ir.json") or {})
    limits = [a for a in (_load(out / "core", "assumptions.json") or []) if a.get("source") == "stdlib"]
    sys_id = {S.ascii_(k["display"]) for k in an.concepts.values() if not k.get("identity") and not k.get("combo_identity")
              and not (k.get("provided_id") and k["provided_id"][0] != "system")}
    for o in ops:
        o["system_id"] = any(S.ascii_(o["capability"]).endswith(x) for x in sys_id)
    def said(sid):
        return list(dict.fromkeys(n["quoted_as"] for n in chain(sid) if n.get("quoted_as")))
    def origin(sid):
        st = stmts.get(sid)
        if not sid or not st:
            return ""
        lab = LABEL.get(st["provenance"], st["provenance"])
        if st.get("inherited_from"):
            lab += f", inherited from {st['inherited_from']}"
        s = said(sid)
        return f" [{lab}{': “' + s[0] + '”' if s else ''}]"
    req = {r['rule']: r['statement'] for r in rules if r['rule'].startswith('BR_REQ_')}
    facts = _concept_facts(an, model, ops, said, origin, req)
    counts = {}
    for s in stmts.values():
        if not s["inherited_from"]:
            counts[s["provenance"]] = counts.get(s["provenance"], 0) + 1
    corpus = {"spec": surf.name, "version": surf.version, "level": surf.level, "uses": surf.uses,
              "said": {"input": surf.input, "answers": surf.answers},
              "not_formalized": surf.closure.get("uncovered", []),
              "verdict": vr.get("verdict"), "summary": vr.get("summary"),
              "combo_identity": {k["core"]: [x for x in (k["combo_identity"]["p1"], k["combo_identity"].get("p2")) if x]
                                 for k in an.concepts.values() if k.get("combo_identity")},
              "concepts": model.get("entities", {}), "relations": model.get("relations", []),
              "operations": ops, "concept_facts": facts,
              "technical": [{"id": s.id, "text": s.text, "means": meaning.get(s.text)} for s in surf.sentences if s.section == "technical"],
              "guarantee_limits": [{"id": a["id"], "text": LIMIT_WORDS.get(a["id"], a["text"])} for a in limits],
              "decisions": surf.decisions, "assumptions": surf.assumptions, "open_questions": surf.questions, "concept_display": {k: v["display"] for k, v in an.concepts.items()},
              "statements": list(stmts.values()), "rules": rules, "provenance_counts": counts,
              "technical_decisions": model.get("realization", [])}
    (out / "corpus.json").write_text(json.dumps(corpus, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "corpus.md").write_text(to_markdown(corpus), encoding="utf-8")
    (out / "corpus-compact.md").write_text(to_markdown(corpus, compact=True), encoding="utf-8")
    return corpus


TYPE_WORDS = {"Natural": "a whole number of 0 or greater", "Integer": "a whole number", "Text": "text (format and length unspecified)",
              "Identifier": "an identifier", "Decimal": "a decimal number", "Money": "an amount of money", "Date": "a date", "Boolean": "yes or no"}


LIMIT_WORDS = {
    "AX_FILE_WRITE": "File writes are treated as atomic: the entire file is written or nothing is written. Behavior under real file-system failures (full disks, permissions, or concurrent access) has not been verified or specified.",
    "AX_TYPED_INITIAL": "The initial state conforms to each property's type (guaranteed by the environment, not verified).",
}

def _concept_facts(an, model, ops, said=lambda sid: [], origin=lambda sid: "", req=None) -> dict:
    """Summarize each concept's observable behavior using only analyzed facts."""
    req = req or {}
    entities = model.get("entities", {})
    operation_names = [operation["capability"] for operation in ops]
    facts_by_concept = {}

    def article(word: str) -> str:
        return "an" if word[:1].lower() in "aeiou" else "a"

    for key, concept in an.concepts.items():
        entity = entities.get(concept["core"], {})
        name, plural = concept["display"], S.plural(concept["display"])
        facts = []
        registrations = [op for op in operation_names
                         if op.lower().startswith("register ") and S.ascii_(op).endswith(S.ascii_(name))]
        facts.append(f"Registration: {'supported by “' + registrations[0] + '”' if registrations else f'no operation registers {plural}' }.")

        modifications = [item[0] for item in concept.get("modify") or []]
        if not modifications and isinstance(concept.get("only_mod"), tuple):
            modifications = [concept["only_mod"][0]]
        modify_ops = [op for op in operation_names
                      if op.lower().startswith("modify ") and S.ascii_(op).endswith(S.ascii_(name))]
        if modify_ops:
            details = f"only {', '.join(item.replace('_', ' ') for item in modifications)} " if modifications else ""
            quotations = [said(item[1])[0] for item in concept.get("modify") or [] if said(item[1])]
            if quotations:
                details += "(as stated: " + "; ".join(f'“{quote}”' for quote in quotations) + ") "
            facts.append(f"Modification: {details}using {', '.join(f'“{op}”' for op in modify_ops)}; no other property of {article(name)} {name} can be changed.")
        elif concept.get("immutable"):
            facts.append(f"Modification: prohibited by a rule{origin(concept['immutable'])}.")
        else:
            facts.append(f"Modification: no operation is available; no property of {article(name)} {name} can be changed.")

        states = concept.get("states") or {}
        if len(states.get("values", [])) > 1:
            facts[-1] = facts[-1].rstrip(".") + " (except for state, which changes only through the transitions below)."
            facts.append(f"States: {', '.join(states['values'])}; every {name} is always in one of these states.{origin(states.get('sn'))}")
            initial = states.get("initial")
            if initial:
                if initial["kind"] == "fixed":
                    detail = f"is set to {initial['a']}"
                else:
                    detail = (f"is set to {initial['a']} when its {initial['p'].replace('_', ' ')} is at most {initial['n']}, "
                              f"and to {initial['b']} otherwise")
                facts.append(f"On registration, the state {detail}.{origin(initial.get('sn'))}")
            transitions = states.get("trans") or {}
            if transitions:
                allowed = "; ".join(
                    f"from {source} to {target} using “Move {name} to {target}”"
                    for target, sources in transitions.items() for source, _ in sources)
                facts.append(f"Allowed state transitions (all others are prohibited): {allowed}.")
                sources = {source for group in transitions.values() for source, _ in group}
                terminal = [value for value in states["values"] if value not in sources]
                if terminal:
                    facts.append(f"Terminal states (no transitions out): {', '.join(terminal)}.")

        facts.append(f"Deletion: {'prohibited by a rule' + origin(concept['no_delete']) if concept.get('no_delete') else f'no operation deletes {plural}' }.")
        if concept.get("identity"):
            identity = concept["identity"][0].replace("_", " ")
            facts.append(f"Identity: {identity} is required; no two {plural} can share the same value.{origin(concept['identity'][1])}")
        elif concept.get("combo_identity"):
            identity_parts = [part for part in (concept["combo_identity"]["p1"], concept["combo_identity"].get("p2")) if part]
            facts.append(f"Identity: no two {plural} can share the same combination of {' and '.join(identity_parts)}.{origin(concept['combo_identity'].get('sn'))}")
        elif concept.get("provided_id") and concept["provided_id"][0] != "system":
            provider = concept["provided_id"][0]
            facts.append(f"Identity: {provider} supplies the identifier; no two {plural} can have the same one.{origin(concept['provided_id'][1])}")
        elif [item for item in concept.get("combos") or [] if item.get("scope")]:
            scoped = next(item for item in concept["combos"] if item.get("scope"))
            facts.append(f"Identity: assigned by the system; values may repeat except where the uniqueness rule applies among {plural} outside state {scoped['scope']}.{origin(scoped.get('sn'))}")
        else:
            facts.append(f"Identity: assigned by the system; no rule prevents {plural} with identical data (duplicates are allowed).")

        for prop in entity.get("properties", []):
            prop_type = prop.get("type", "")
            if prop_type.startswith("reference to "):
                continue
            wording = TYPE_WORDS.get(prop_type, prop_type)
            if prop_type == "Text" and re.match(r"^(?:date|day|hour|schedule|moment|due)", prop["name"], re.I):
                wording = (f"text named “{prop['name'].replace('_', ' ')}”; it is unspecified whether it includes a day, time, or both, "
                           "what format it uses, or whether it is a valid date")
            minimum = (concept.get("min") or {}).get(prop["name"])
            maximum = (concept.get("max") or {}).get(prop["name"])
            if minimum or maximum:
                wording += f"; rule: {'at least ' + str(minimum[0]) if minimum else ''}{' and ' if minimum and maximum else ''}{'at most ' + str(maximum[0]) if maximum else ''}{origin((minimum or maximum)[1])}"
            requirement = f"BR_REQ_{concept.get('core', '')}_{prop['name']}"
            if requirement in req and not (concept.get("identity") and prop["name"] == concept["identity"][0]):
                wording += f"; required{origin(req[requirement])}"
            if concept.get("numbering") and prop["name"] == "number":
                wording = f"assigned by the system's numbering sequence ({wording})"
            facts.append(f"Property {prop['name'].replace('_', ' ')}: {wording} (the system inferred the type).")

        if concept.get("numbering"):
            numbering = concept["numbering"]
            facts.append(f"Numbering: each {name} belonging to a {numbering['owner']} is numbered consecutively from {numbering['start']}, with no gaps.{origin(numbering.get('sn'))}")
        if concept.get("artifact"):
            artifact = concept["artifact"]
            facts.append(f"Artifact: each {name} produces exactly one file named {artifact['name']}.{origin(artifact.get('sn'))}")
        for reference in concept.get("refs") or []:
            target = an.concepts.get(reference["target"], {})
            target_name = target.get("display", reference["target"])
            facts.append(f"Each {name} belongs to exactly one {target_name}.{origin(reference.get('sn'))}")
        facts_by_concept[key] = facts

    # Describe the inverse cardinality: how many instances of one concept may belong to another.
    for key, concept in an.concepts.items():
        for reference in concept.get("refs") or []:
            target_key = reference["target"]
            name, plural = concept["display"], S.plural(concept["display"])
            target_name = an.concepts.get(target_key, {}).get("display", target_key)
            capacity = concept.get("capacity")
            combinations = [item for item in concept.get("combos") or [] if target_key in (item["p1"], item["p2"])]
            single = [item for item in combinations if not item["p2"]]
            count_limits = [item for item in concept.get("count_max") or [] if item["owner"] == target_key]
            if count_limits:
                for limit in count_limits:
                    sentence = f"{article(target_name).capitalize()} {target_name} can have at most {limit['n']} {plural}{S.count_scope_txt(limit)} (verified rule)"
                    if limit["scope_list"]:
                        sentence += f"; {plural} in other states are not counted"
                    facts_by_concept.setdefault(target_key, []).append(sentence + "." + origin(limit.get("sn")))
            if capacity and capacity["owner"] == target_key:
                sentence = f"{article(target_name).capitalize()} {target_name} can have at most the number of {plural} specified by “{capacity['prop'].replace('_', ' ')}” (verified rule)"
                if concept.get("no_delete"):
                    sentence += f"; because {plural} cannot be deleted, this counts all {plural} ever registered"
                facts_by_concept.setdefault(target_key, []).append(sentence + "." + origin(capacity.get("sn")))
            elif single:
                scope = (f" outside state {single[0]['scope']} (there may be more in that state)"
                         if single[0].get("scope") else "")
                facts_by_concept.setdefault(target_key, []).append(
                    f"{article(target_name).capitalize()} {target_name} can have at most one {name}{scope}.{origin(single[0].get('sn'))}")
            elif combinations:
                other = next(part for part in (combinations[0]["p1"], combinations[0]["p2"]) if part != target_key)
                scope = (f", counting only {plural} outside state {combinations[0]['scope']}"
                         if combinations[0].get("scope") else "")
                facts_by_concept.setdefault(target_key, []).append(
                    f"{article(target_name).capitalize()} {target_name} can have multiple {plural}, but at most one for each {other}{scope}.{origin(combinations[0].get('sn'))}")
            elif not count_limits:
                facts_by_concept.setdefault(target_key, []).append(
                    f"{article(target_name).capitalize()} {target_name} can have any number of {plural}; no rule limits the count.")
    return facts_by_concept

LABEL = {"ASSERTED": "stated by the author", "NORMALIZED": "restated by the author", "RESOLVED": "confirmed or selected by the author in response to a system question",
         "DERIVED": "derived", "TECHNICAL": "technical decision not visible to the business", "AUTHOR_INPUT_FORM": "stated by the author",
         "language axiom": "language rule", "input form": "input form"}

def to_markdown(c: dict, compact: bool = False) -> str:
    lines = [f"# Corpus — {c['spec']} {c['version']}", "", f"Verdict: **{c['verdict']}** · {c['summary']}", ""]
    if c["said"]["input"]:
        lines += ["## What the author said", *[f"> {item}" for item in c["said"]["input"]], ""]
    if c["said"]["answers"]:
        lines += ["## Answers", *[f"> {item}" for item in c["said"]["answers"]], ""]
    if c.get("not_formalized"):
        lines += ["## Stated but not formalized (not guaranteed)",
                  "UPA guarantees none of the following: the author stated it, but no verified rule captures it.",
                  *[(f"- answered: “{item['input']}”" if item.get("kind") == "answer" else
                     f"- words without a rule: {', '.join(dict.fromkeys(item['words']))}")
                    for item in c["not_formalized"]], ""]
    lines += ["## Concepts", ""]
    for key, entity in c["concepts"].items():
        properties = ", ".join(prop["name"] for prop in entity["properties"])
        combo = (c.get("combo_identity") or {}).get(key)
        identity = entity["identity"] or (f"combination ({', '.join(combo)}); implementation key" if combo else "implementation key")
        lines.append(f"- **{entity['concept']}** ({key}) · identity: {identity} · {properties}")
    lines += ["", "## How to read this corpus", "",
              "- **Operations (closed world):** only the operations listed below are available. Anything not listed cannot be done.",
              "- **Rules:** anything not stated as a rule is undefined: neither prohibited nor guaranteed. Treat it as unspecified.",
              "- **Closed vocabulary:** concepts, properties, and operations are limited to those in this document. If a question uses a term not listed"
              " (for example, a state, condition, or deadline not shown here), that question is undefined; do not equate it with the closest term.",
              "- **Provenance:** each rule shows whether it was stated by the author, confirmed in an answer, or derived by the system.", ""]
    if c.get("concept_facts"):
        lines += ["## Facts about each concept", ""]
        for key, facts in c["concept_facts"].items():
            display = c.get("concept_display", {}).get(key, key)
            lines.append(f"### {display}")
            lines += [f"- {fact}" for fact in facts]
            lines.append("")
    if c.get("operations"):
        lines += ["## Operations and outcomes", ""]
        for operation in c["operations"]:
            lines.append(f"### {operation['capability']}")
            for outcome in operation["outcomes"]:
                effect = "no changes" if "no changes" in outcome["effect"] else outcome["effect"]
                technical = (" — technical case: the system assigns the identifier"
                             if outcome["when"] == "the identifier already exists" and operation.get("system_id") else "")
                condition = outcome["when"] if outcome["when"] != "otherwise" else "otherwise"
                lines.append(f"- if {condition} → **{outcome['result']}** ({effect}){technical}")
            lines.append("")
    if c.get("technical") or c.get("decisions"):
        lines += ["## Technical rules and decisions (not business-visible behavior)", ""]
        lines += [f"- {item['text']} (technical rule, {item['id']}){' — verified as: ' + item['means'] if item.get('means') else ''}"
                  for item in c.get("technical", [])]
        lines += [f"- {decision['text']} (decision {decision['id']})" for decision in c.get("decisions", [])]
        lines.append("")
    if c.get("assumptions"):
        lines += ["## Assumptions (not verified by UPA; guaranteed by their owner)", ""]
        lines += [f"- {item['text']} — owner: {item.get('owner') or 'unspecified'}" for item in c["assumptions"]]
        lines.append("")
    if c.get("guarantee_limits"):
        lines += ["## Guarantee limits (assumptions made by verification; no guarantee beyond them)", ""]
        lines += [f"- {item['text']} ({item['id']})" for item in c["guarantee_limits"]]
        lines.append("")
    if c.get("open_questions"):
        lines += ["## Open questions (undecided; the answer is not yet defined)", ""]
        lines += [f"- {question['text']} ({question['id']})" for question in c["open_questions"]]
        lines.append("")
    if compact:
        lines += ["The full chain for each rule (statement → derivation → proof obligation) is in corpus.md.", ""]
        return "\n".join(lines) + "\n"
    lines += ["## Why UPA believes each rule", ""]
    used_axioms = {}
    for rule in c["rules"]:
        lines.append(f"### {rule['rule']} · {rule['status']}")
        for depth, node in enumerate(rule["chain"]):
            label = LABEL.get(node["step"], node["step"])
            quote = f" — “{node['quoted_as']}”" if node.get("quoted_as") else ""
            inherited = f" (inherited from {node['inherited_from']})" if node.get("inherited_from") else ""
            if node["step"] == "language axiom":
                used_axioms[node["id"]] = node["text"]
                lines.append(f"{'  ' * min(depth, 1)}- {label}: {node['id']}")
                continue
            lines.append(f"{'  ' * min(depth, 1)}- {label}: {node.get('text', node['id'])}{quote}{inherited}")
        if rule["formalization"]:
            lines.append(f"  - formalization: {'; '.join(rule['formalization'])}")
        statuses = ', '.join(sorted({item['status'] for item in rule['obligations']})) or "—"
        lines.append(f"  - proof obligations: {len(rule['obligations'])} ({statuses})")
        lines.append("")
    if used_axioms:
        lines += ["## Language rules used", "", *[f"- **{key}**: {value}" for key, value in sorted(used_axioms.items())], ""]
    return "\n".join(lines) + "\n"
