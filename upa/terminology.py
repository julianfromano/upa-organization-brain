"""Inherited glossaries (organization > domain > module > spec) and term disambiguation.

A term used by an analyst (such as 'user' or 'customer') may refer to one concept, two distinct concepts, or depend on context.
UPA does not decide silently: it resolves the term from inherited knowledge or asks a question
(AMBIGUOUS_TERM / TERM_CONFLICT), including the exact wording each answer would add.

Glossary file: `glossary.upa` at any level under `rules/` (or beside the .upa file):
    spec SharedTerms version "1"
    glossary { "User can refer to customer or agent." "Customer means a person with an active relationship." }
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from pathlib import Path

from .surface import PH, Sentence, norm_s, match_sentence, parse_surface, slug

GL_PATTERNS = [
    ("GL_RESOLVE", re.compile(rf"^in this specification,? (?P<a>{PH}) means (?:the )?(?P<b>{PH})$", re.I | re.U)),
    ("GL_SYNONYM", re.compile(rf"^(?:the )?(?P<a>{PH}) is another name for (?:the )?(?P<b>{PH})$", re.I | re.U)),
    ("GL_DISTINCT", re.compile(rf"^(?:the )?(?P<a>{PH}) is distinct from (?:the )?(?P<b>{PH})$", re.I | re.U)),
    ("GL_LOCAL_ID", re.compile(rf"^(?:the )(?P<a>{PH}) is specific to each concept$", re.I | re.U)),
    ("GL_CONTEXT", re.compile(rf"^(?:the )?(?P<a>{PH}) can refer to (?:(?:a|an|the) )?(?P<b>{PH}) or (?:(?:a|an|the) )?(?P<c>{PH})$", re.I | re.U)),
    ("GL_MEANS", re.compile(r"^(?:the )?(?P<a>[^\W\d_]+(?:\s[^\W\d_]+){0,3}?) means (?P<d>.+)$", re.I | re.U)),
]
GL_DOC = {
    "GL_SYNONYM": "<term> is another name for <concept>.",
    "GL_DISTINCT": "<term> is distinct from <concept>.",
    "GL_LOCAL_ID": "The <identifier> is specific to each concept.",
    "GL_CONTEXT": "<term> can refer to <concept A> or <concept B>.",
    "GL_RESOLVE": "In this specification, <term> means <concept>.",
    "GL_MEANS": "<term> means <definition>.",
}


def _clean(s: str) -> str:
    return norm_s(s)


@dataclass
class Glossary:
    synonyms: dict = field(default_factory=dict)     # alias -> {canonical, level, text}
    distinct: dict = field(default_factory=dict)     # frozenset({a,b}) -> {level,text}
    context: dict = field(default_factory=dict)      # term -> {candidates, level, text}
    resolved: dict = field(default_factory=dict)     # term -> {canonical, level, text} (local to this specification)
    means: dict = field(default_factory=dict)        # term -> {definition, level}
    profiles: dict = field(default_factory=dict)     # concept -> {identity, props, level, text}
    local_ids: dict = field(default_factory=dict)    # identifier -> {level, text}: sharing it does not imply a synonym
    sources: list = field(default_factory=list)      # [{file, level, sentences}]
    errors: list = field(default_factory=list)

    def add(self, sentences, level: str, where: str, local: bool):
        n = 0
        for sn in sentences:
            n += 1
            t = re.sub(r"\s+", " ", sn.text.strip()).rstrip(".")
            for pid, rx in GL_PATTERNS:
                m = rx.match(t)
                if not m:
                    continue
                g = m.groupdict()
                a = _clean(g["a"])
                rec = {"level": level, "text": sn.text, "file": where}
                if pid == "GL_SYNONYM":
                    self.synonyms[a] = {"canonical": _clean(g["b"]), **rec}
                elif pid == "GL_DISTINCT":
                    self.distinct[frozenset((a, _clean(g["b"])))] = rec
                elif pid == "GL_LOCAL_ID":
                    self.local_ids[slug(g["a"])] = rec
                elif pid == "GL_CONTEXT":
                    self.context[a] = {"candidates": [_clean(g["b"]), _clean(g["c"])], **rec}
                elif pid == "GL_RESOLVE":
                    self.resolved[a] = {"canonical": _clean(g["b"]), **rec}
                else:
                    self.means[a] = {"definition": g["d"].strip(), **rec}
                break
            else:
                # Profile statements reuse the surface catalog (identity and properties).
                probe = Sentence(sn.id, sn.text, "business", None, sn.line)
                p, g = match_sentence(probe)
                if p is not None and p["id"] == "IDENTITY":
                    c = _clean(g["c"])
                    self.profiles.setdefault(c, {"identity": None, "props": [], "level": level, "file": where})
                    self.profiles[c]["identity"] = slug(g["p"])
                elif p is not None and p["id"] == "HAS_PROPS":
                    from .surface import split_list
                    c = _clean(g["c"])
                    self.profiles.setdefault(c, {"identity": None, "props": [], "level": level, "file": where})
                    self.profiles[c]["props"] = [slug(x) for x in split_list(g["props"])]
                else:
                    self.errors.append({"error": "GLOSSARY_UNSUPPORTED", "level": level, "file": where, "line": sn.line,
                                        "source": sn.text, "closest_patterns": list(GL_DOC.values())})
        return n


def glossary_files(path: Path | None) -> list[tuple[str, Path]]:
    """Return glossaries that apply to a .upa file, from most general to most specific."""
    if path is None:
        return []
    path = path.resolve()
    chain, root = [], None
    for p in path.parents:
        if p.name == "rules":
            root = p
            break
        chain.append(p)
        if len(chain) >= 8:
            break
    if root is not None:
        dirs = [d for d in reversed(chain)]
        top = [root] + dirs
        out = []
        for d in top:
            f = d / "glossary.upa"
            if f.exists():
                rel = d.relative_to(root).as_posix() if d != root else "(rules root)"
                out.append((rel or "(rules root)", f))
        return out
    f = path.parent / "glossary.upa"
    return [(path.parent.name, f)] if f.exists() else []


def load_inherited(path: Path | None) -> Glossary:
    g = Glossary()
    for level, f in glossary_files(path):
        s = parse_surface(f.read_text(encoding="utf-8"))
        n = g.add(s.glossary, level, str(f), local=False)
        g.sources.append({"file": str(f), "level": level, "sentences": n})
    return g


class Terminology:
    def __init__(self, surf, path: Path | None):
        self.g = load_inherited(path)
        self.inherited = {"synonyms": set(self.g.synonyms), "context": set(self.g.context)}
        self.local_n = self.g.add(surf.glossary, "this specification", str(path) if path else "", local=True)
        self.normalizations: list[dict] = []
        self.findings: list[dict] = []
        self.surf = surf

    # ---- before matching statements
    def aliases(self) -> dict:
        out = {}
        for a, v in self.g.synonyms.items():
            out[a] = {"canonical": v["canonical"], "source": v}
        for a, v in self.g.resolved.items():
            out[a] = {"canonical": v["canonical"], "source": v}
        return out

    def _mentions(self, term: str):
        rx = re.compile(rf"\b{re.escape(term)}(?:s|es)?\b", re.I | re.U)
        return [sn for sn in self.surf.sentences if rx.search(norm_s(sn.text)) or (sn.capability and rx.search(norm_s(sn.capability)))]

    def apply(self, errors) -> None:
        for e in self.g.errors:
            errors.add("GLOSSARY_UNSUPPORTED", f"unrecognized glossary statement at {e['level']}: {e['source']!r}",
                       (e["line"], 1), "terminology", source=e["source"], closest_patterns=e["closest_patterns"])
        aliases = self.aliases()
        for term, v in self.g.context.items():
            used = self._mentions(term)
            if term in aliases:
                if aliases[term]["canonical"] not in v["candidates"]:
                    errors.add("INVALID_TERM_RESOLUTION",
                               f"'{term}' can refer to {v['candidates']}, not '{aliases[term]['canonical']}'", None, "terminology")
                continue
            if used:
                self.findings.append(self._ctx_question(term, v, used))
                errors.add("AMBIGUOUS_TERM", f"the term {term!r} can refer to {v['candidates']} depending on context; "
                           "the specification does not say which", (used[0].line, 1), "terminology", term=term,
                           candidates=v["candidates"], source=v["text"], sentences=[s.id for s in used])
        for alias, v in aliases.items():
            rx = re.compile(rf"\b{re.escape(alias)}(s|es)?\b", re.I | re.U)
            canon = v["canonical"]

            def sub(m, canon=canon):
                suf = m.group(1)
                if not suf:
                    return canon
                return canon + ("es" if re.search(r"(?:s|x|z|ch|sh)$", canon, re.I) else "s")

            for sn in self.surf.sentences:
                new = rx.sub(sub, sn.match_text or sn.text)
                if new != (sn.match_text or sn.text):
                    self.normalizations.append({"sentence": sn.id, "alias": alias, "canonical": canon,
                                                "source_level": v["source"]["level"], "source_text": v["source"]["text"]})
                    sn.match_text = new
                if sn.capability and rx.search(sn.capability):
                    sn.capability = rx.sub(sub, sn.capability)

    def _ctx_question(self, term, v, used):
        a, b = v["candidates"]
        return {"id": f"TERM_{slug(term)}", "type": "AMBIGUOUS_TERM", "priority": 1, "term": term,
                "question": f"In this specification, is '{term}' {a} or {b}?",
                "why": f"The glossary ({v['level']}) says: “{v['text']}.” Choosing incorrectly merges two concepts with different rules.",
                "evidence": [s.text for s in used[:3]],
                "options": [{"answer": a, "writes": f"In this specification, {term} means {a}."},
                            {"answer": b, "writes": f"In this specification, {term} means {b}."},
                            {"answer": "neither; they are separate concepts",
                             "writes": f"{term.capitalize()} is distinct from {a}. Define its rules separately."}]}

    # ---- after building the model
    def check_model(self, an, errors) -> None:
        concepts = an.concepts
        keys = sorted(concepts)
        for i, a in enumerate(keys):
            for b in keys[i + 1:]:
                if frozenset((a, b)) in self.g.distinct:
                    continue
                shared = sorted(set(concepts[a]["props"]) & set(concepts[b]["props"]))
                same_id = concepts[a]["identity"] and concepts[b]["identity"] and concepts[a]["identity"][0] == concepts[b]["identity"][0]
                if same_id and concepts[a]["identity"][0] in self.g.local_ids:
                    # A glossary-local identifier is not evidence by itself; only other properties count.
                    same_id = len([x for x in shared if x != concepts[a]["identity"][0]]) >= 2
                if len(shared) >= 2 and same_id:
                    idd = concepts[a]["props"].get(concepts[a]["identity"][0], {}).get("display", concepts[a]["identity"][0])
                    self.findings.append({
                        "id": f"TERM_{slug(a)}_{slug(b)}", "type": "SYNONYM_CANDIDATE", "priority": 1, "term": f"{a} / {b}",
                        "question": f"Are '{a}' and '{b}' the same concept under two names, or are they distinct concepts?",
                        "why": f"They share the same identity ({concepts[a]['identity'][0]}) and properties {shared}. If they are the same, "
                               "data and rules would be duplicated; if they are distinct, explain how they differ.",
                        "evidence": [concepts[a]["display"], concepts[b]["display"]],
                        "options": [{"answer": "the same concept", "writes": f"{b.capitalize()} is another name for {a}."},
                                    {"answer": "distinct concepts", "writes": f"{b.capitalize()} is distinct from {a}."},
                                    {"answer": f"distinct concepts, and {idd} is specific to each concept "
                                               "(do not ask about it again for other concepts)",
                                     "writes": f"The {idd} is specific to each concept."}]})
                    errors.add("AMBIGUOUS_TERM", f"'{a}' and '{b}' share an identity and properties {shared}: are they the same concept?",
                               None, "terminology", term=f"{a}/{b}", candidates=[a, b])
        # inherited profiles
        for k, c in concepts.items():
            prof = self.g.profiles.get(k)
            if not prof:
                continue
            mine = c["identity"][0] if c["identity"] else None
            resolved = next((d for d in self.surf.decisions if d.get("resolves") == f"TERM_{slug(k)}"), None)
            if prof["identity"] and mine and prof["identity"] != mine and not resolved:
                self.findings.append({
                    "id": f"TERM_{slug(k)}", "type": "TERM_CONFLICT", "priority": 1, "term": k,
                    "question": f"'{k}' already exists at {prof['level']} with identity {prof['identity']}; here it is identified by {mine}. "
                                "Is this the same concept or a new concept with the same name?",
                    "why": "Reusing the name for a different concept breaks consumers of the inherited term.",
                    "evidence": [prof["file"]],
                    "options": [{"answer": "the same concept", "writes": f"(change the identity of {k} to {prof['identity']})"},
                                {"answer": "a different concept",
                                 "writes": f'decision DD_{slug(k)} "{k} is a separate concept in this specification." resolves TERM_{slug(k)} = NEW_CONCEPT'}]})
                errors.add("TERM_CONFLICT", f"'{k}' is identified by {mine}, but at {prof['level']} it is identified by {prof['identity']}",
                           None, "terminology", term=k)
        # informational facts
        self.known, self.new_terms, self.reuse = [], [], []
        for k, c in concepts.items():
            m = self.g.means.get(k)
            prof = self.g.profiles.get(k)
            if m or prof:
                self.known.append({"term": k, "level": (m or prof)["level"], "definition": (m or {}).get("definition"),
                                   "inherited_properties": (prof or {}).get("props", []),
                                   "inherited_identity": (prof or {}).get("identity")})
                missing = [p for p in (prof or {}).get("props", []) if p not in c["props"]]
                if missing:
                    self.reuse.append({"term": k, "inherited_properties_not_restated": missing,
                                       "note": "the module already knows these properties; confirm whether they apply here"})
            else:
                self.new_terms.append({"term": k, "question": f"'{k}' is not in any inherited glossary; how does the business define it?",
                                       "writes": f"{k.capitalize()} means <definition>."})

    def report(self) -> dict:
        return {"glossary_sources": self.g.sources, "local_glossary_sentences": self.local_n,
                "normalizations": self.normalizations, "findings": self.findings,
                "known_terms": getattr(self, "known", []), "new_terms": getattr(self, "new_terms", []),
                "reuse_candidates": getattr(self, "reuse", []),
                "distinct_declared": [sorted(k) for k in self.g.distinct],
                "limitations": ["inherited concepts are reported and compared, but their rules are not yet imported into the surface"]}
