"""Lexer and recursive-descent parser for UPA v0."""
from __future__ import annotations
import re
from dataclasses import dataclass

from .ast import (Attr, Case, ComponentRef, Compose, Entity, EnumDecl, External, ExternalOp, Fact, Note, Operation, Origin, Param,
                  Prop, Query, Spec, Step, Stmt, SystemComponent, SystemContract, SystemFlow, SystemFlowStep,
                  SystemObservation, SystemCatalog, SystemJob, SystemRule, CatalogValue, TypeAlias,
                  Invocation, INVOCATION_KINDS, AsyncProducer, AsyncConsumer, ASYNC_TRANSPORTS,
                  ASYNC_PUBLICATION_CONFIRMATIONS,
                  ASYNC_DURABILITIES, ASYNC_PROCESSING, ASYNC_DELIVERIES,
                  ASYNC_COMPLETIONS, ASYNC_FAILURES)
from .errors import UpaError


@dataclass
class Tok:
    kind: str   # IDENT INT STRING SYM EOF
    text: str
    line: int
    col: int

    @property
    def pos(self):
        return (self.line, self.col)


_TOKEN = re.compile(r"""
    (?P<ws>[ \t\r\n]+)
  | (?P<comment>//[^\n]*)
  | (?P<string>"(?:[^"\\\n]|\\.)*")
  | (?P<ident>[A-Za-z_][A-Za-z_0-9]*)
  | (?P<int>[0-9]+)
  | (?P<sym>==|!=|<=|>=|=>|->|[{}()\[\],:=<>+\-.;@*&])
""", re.VERBOSE)


def tokenize(src: str) -> list[Tok]:
    toks: list[Tok] = []
    i, line, col = 0, 1, 1
    while i < len(src):
        m = _TOKEN.match(src, i)
        if not m:
            raise UpaError("LEX_ERROR", f"unexpected character {src[i]!r}", (line, col), "parse")
        text = m.group(0)
        kind = m.lastgroup
        if kind == "string":
            toks.append(Tok("STRING", text[1:-1].replace('\\"', '"').replace("\\\\", "\\"), line, col))
        elif kind in ("ident", "int", "sym"):
            toks.append(Tok({"ident": "IDENT", "int": "INT", "sym": "SYM"}[kind], text, line, col))
        nl = text.count("\n")
        if nl:
            line += nl
            col = len(text) - text.rfind("\n")
        else:
            col += len(text)
        i = m.end()
    toks.append(Tok("EOF", "", line, col))
    return toks


class Parser:
    def __init__(self, src: str):
        self.toks = tokenize(src)
        self.i = 0

    # ---- utilidades
    @property
    def cur(self) -> Tok:
        return self.toks[self.i]

    def peek(self, n=1) -> Tok:
        return self.toks[min(self.i + n, len(self.toks) - 1)]

    def err(self, msg: str, tok: Tok | None = None):
        t = tok or self.cur
        raise UpaError("PARSE_ERROR", msg, t.pos, "parse", {"found": t.text or "EOF"})

    def at(self, text: str) -> bool:
        return self.cur.kind in ("IDENT", "SYM") and self.cur.text == text

    def accept(self, text: str) -> bool:
        if self.at(text):
            self.i += 1
            return True
        return False

    def expect(self, text: str) -> Tok:
        if not self.at(text):
            self.err(f"expected '{text}'")
        t = self.cur
        self.i += 1
        return t

    def ident(self) -> str:
        if self.cur.kind != "IDENT":
            self.err("expected an identifier")
        t = self.cur.text
        self.i += 1
        return t

    def string(self) -> str:
        if self.cur.kind != "STRING":
            self.err("expected a quoted string")
        t = self.cur.text
        self.i += 1
        return t

    # ---- file
    def parse(self) -> Spec:
        if not (self.at("spec") or self.at("block")):
            self.err("the file must start with 'spec' or 'block'")
        kind = self.cur.text
        self.i += 1
        name = self.ident()
        self.expect("version")
        spec = Spec(kind, name, self.string())
        if self.accept("level"):
            spec.level = self.ident()
            if spec.level not in ("layer", "organization", "domain", "module", "specific"):
                self.err(f"unknown level {spec.level!r} (layer | organization | domain | module | specific)")
        if self.accept("tier"):
            spec.tier = self.ident()
            if spec.tier not in ("atom", "subprocess", "process", "macroprocess"):
                self.err(f"unknown tier {spec.tier!r} (atom | subprocess | process | macroprocess)")
        while self.cur.kind != "EOF":
            self.decl(spec)
        self.validate_system_map(spec)
        self.validate_invocations(spec)
        self.validate_async_endpoints(spec)
        return spec

    def invocation(self) -> Invocation:
        t = self.cur
        self.expect("invokes")
        target = self.ident()
        kind_tok = self.cur
        kind = self.ident()
        if kind not in INVOCATION_KINDS:
            raise UpaError("INVALID_INVOKES_KIND", f"unknown external entity kind {kind!r} ({' | '.join(INVOCATION_KINDS)})",
                           kind_tok.pos, "parse")
        name = self.string()
        mode_tok = self.cur
        mode = self.ident()
        if mode not in ("sync", "async"):
            raise UpaError("INVALID_INVOKES_MODE", f"invocation mode {mode!r}: must be sync or async", mode_tok.pos, "parse")
        inv = Invocation(target, kind, name, mode, pos=t.pos)
        if self.accept("via"):
            inv.via.append(self.ident())
            while self.accept(","):
                inv.via.append(self.ident())
        if self.accept("evidence"):
            inv.evidence = self.string()
        return inv

    def validate_invocations(self, spec: Spec) -> None:
        targets = {o.name: o.params for o in spec.operations}
        targets.update({c.name: c.params for c in spec.composes})
        for inv in spec.invocations:
            if inv.target not in targets:
                raise UpaError("UNKNOWN_INVOKES_TARGET", f"invokes {inv.target}: no operation or composition has that name",
                               inv.pos, "parse")
            names = {p.name for p in targets[inv.target]}
            for v in inv.via:
                if v not in names:
                    raise UpaError("INVOKES_PARAM_UNKNOWN", f"invokes {inv.target} via {v}: is not a parameter of {inv.target}",
                                   inv.pos, "parse")
        for c in spec.composes:
            if c.atomic and any(x["async"] for x in c.execution):
                raise UpaError("COMPOSE_ASYNC_IN_ATOMIC",
                               f"{c.name}: an atomic composition cannot have async steps (it does not wait for their results)", c.pos, "parse")
            if c.atomic and any(x["loop"] for x in c.execution):
                raise UpaError("COMPOSE_LOOP_IN_ATOMIC",
                               f"{c.name}: an atomic composition cannot contain loops", c.pos, "parse")

    def dotted_name(self) -> str:
        name = self.ident()
        while self.accept("."):
            name += "." + self.ident()
        return name

    def _async_transport_channel(self):
        self.expect("via")
        transport_tok = self.cur
        transport = self.ident()
        if transport not in ASYNC_TRANSPORTS:
            raise UpaError("INVALID_ASYNC_TRANSPORT",
                           f"async transport {transport!r}: must be {' | '.join(ASYNC_TRANSPORTS)}",
                           transport_tok.pos, "parse")
        return transport, self.string()

    def publishes(self) -> AsyncProducer:
        t = self.cur
        self.expect("publishes")
        ident = self.ident()
        self.expect("from")
        operation = self.dotted_name()
        transport, channel = self._async_transport_channel()
        producer = AsyncProducer(ident, operation, transport, channel, pos=t.pos)
        if self.accept("message"):
            producer._message_declared = True
            producer.message = self.string()
        self.expect("confirmation")
        producer.confirmation = self.ident()
        self.expect("evidence")
        producer.evidence = self.string()
        if producer.confirmation not in ASYNC_PUBLICATION_CONFIRMATIONS:
            raise UpaError("INVALID_ASYNC_PUBLICATION_CONFIRMATION",
                           f"publication confirmation {producer.confirmation!r}: must be "
                           f"{' | '.join(ASYNC_PUBLICATION_CONFIRMATIONS)}", t.pos, "parse")
        return producer

    def subscribes(self) -> AsyncConsumer:
        t = self.cur
        self.expect("subscribes")
        ident = self.ident()
        self.expect("at")
        operation = self.dotted_name()
        transport, channel = self._async_transport_channel()
        consumer = AsyncConsumer(ident, operation, transport, channel, pos=t.pos)
        self.expect("message")
        consumer._message_declared = True
        consumer.message = self.string()
        self.expect("subscription")
        consumer._subscription_declared = True
        consumer.subscription = self.string()
        self.expect("durability")
        consumer.durability = self.ident()
        self.expect("processing")
        consumer.processing = self.ident()
        self.expect("delivery")
        consumer.delivery = self.ident()
        self.expect("completion")
        consumer.completion = self.ident()
        self.expect("failure")
        consumer.failure = self.ident()
        self.expect("evidence")
        consumer.evidence = self.string()
        enums = (
            ("INVALID_ASYNC_DURABILITY", "durabilidad", consumer.durability, ASYNC_DURABILITIES),
            ("INVALID_ASYNC_PROCESSING", "procesamiento", consumer.processing, ASYNC_PROCESSING),
            ("INVALID_ASYNC_DELIVERY", "entrega", consumer.delivery, ASYNC_DELIVERIES),
            ("INVALID_ASYNC_COMPLETION", "confirmation", consumer.completion, ASYNC_COMPLETIONS),
            ("INVALID_ASYNC_FAILURE", "falla", consumer.failure, ASYNC_FAILURES),
        )
        for code, label, value, allowed in enums:
            if value not in allowed:
                raise UpaError(code, f"{label} async {value!r}: must be {' | '.join(allowed)}",
                               t.pos, "parse")
        return consumer

    def validate_async_endpoints(self, spec: Spec) -> None:
        seen: set[str] = set()
        local = {x.name for x in spec.operations} | {x.name for x in spec.composes}
        aliases = {x.alias for x in spec.components}
        for kind, endpoints in (("producer", spec.async_producers), ("consumer", spec.async_consumers)):
            for endpoint in endpoints:
                if endpoint.id in seen:
                    raise UpaError("DUPLICATE_ASYNC_ENDPOINT",
                                   f"duplicate async endpoint: {endpoint.id}", endpoint.pos, "parse")
                seen.add(endpoint.id)
                root, dot, member = endpoint.operation.partition(".")
                valid_shape = (not dot and root in local) or (dot and root in aliases and bool(member))
                if not valid_shape:
                    raise UpaError("UNKNOWN_ASYNC_ENDPOINT",
                                   f"{endpoint.id}: {endpoint.operation} must be a local operation/composition "
                                   "or an Alias.operation/composition path", endpoint.pos, "parse")
                if not endpoint.channel.strip() or not endpoint.evidence.strip():
                    raise UpaError("ASYNC_ENDPOINT_EMPTY",
                                   f"{endpoint.id}: channel and evidence are required", endpoint.pos, "parse")
                if self._invalid_async_label(endpoint.message) or (
                        endpoint.message == "" and bool(getattr(endpoint, "_message_declared", False))):
                    raise UpaError("ASYNC_MESSAGE_EMPTY",
                                   f"{endpoint.id}: message must not be empty, have leading or trailing whitespace "
                                   "or contain control characters", endpoint.pos, "parse")
                if kind == "consumer" and (self._invalid_async_label(endpoint.subscription)
                        or not endpoint.subscription):
                    raise UpaError("ASYNC_SUBSCRIPTION_EMPTY",
                                   f"{endpoint.id}: subscription must not be empty, have leading or trailing whitespace "
                                   "or contain control characters", endpoint.pos, "parse")

    @staticmethod
    def _invalid_async_label(value: str) -> bool:
        return value != value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value)

    def decl(self, s: Spec):
        t = self.cur
        w = t.text if t.kind == "IDENT" else ""
        if w == "uses":
            self.i += 1
            n = self.ident()
            while self.accept("."):
                n += "." + self.ident()
            self.expect("@")
            v = self.cur.text
            self.i += 1
            while self.at("."):
                self.i += 1
                v += "." + self.cur.text
                self.i += 1
            s.uses.append((n, v))
        elif w == "component":
            self.i += 1
            alias = self.ident()
            self.expect("=")
            n = self.ident()
            while self.accept("."):
                n += "." + self.ident()
            self.expect("@")
            v = self.cur.text
            self.i += 1
            while self.at("."):
                self.i += 1
                v += "." + self.cur.text
                self.i += 1
            if any(c.alias == alias for c in s.components):
                raise UpaError("DUPLICATE_COMPONENT_ALIAS", f"duplicate component alias: {alias}", t.pos, "parse")
            s.components.append(ComponentRef(alias, n, v, t.pos))
        elif w == "type":
            self.i += 1
            n = self.ident()
            self.expect("=")
            base = self.ident()
            scale = 0
            if self.accept("scale"):
                scale = int(self.cur.text)
                self.i += 1
            s.aliases.append(TypeAlias(n, base, scale, t.pos))
        elif w == "enum":
            self.i += 1
            n = self.ident()
            self.expect("{")
            members = [self.ident()]
            while self.accept(","):
                members.append(self.ident())
            self.expect("}")
            s.enums.append(EnumDecl(n, members, t.pos))
        elif w == "entity":
            self.i += 1
            n = self.ident()
            self.expect("{")
            attrs = [self.attr()]
            while self.accept(","):
                attrs.append(self.attr())
            self.expect("}")
            s.entities.append(Entity(n, attrs, t.pos))
        elif w == "initial":
            self.i += 1
            s.initial_origin = self.origin()
            self.expect(":")
            s.initial = self.expr()
        elif w == "external":
            s.externals.append(self.external())
        elif w == "system_map":
            self.system_map(s)
        elif w == "operation":
            s.operations.append(self.operation())
        elif w == "compose":
            s.composes.append(self.compose())
        elif w == "query":
            s.queries.append(self.query())
        elif w == "invokes":
            s.invocations.append(self.invocation())
        elif w == "publishes":
            s.async_producers.append(self.publishes())
        elif w == "subscribes":
            s.async_consumers.append(self.subscribes())
        elif w == "async_transition":
            raise UpaError("ASYNC_TRANSITION_REMOVED",
                           "async_transition was removed; migrate to independent declarations "
                           "'publishes ...' and 'subscribes ...' without coupled from/to fields", t.pos, "parse")
        elif w == "store":
            self.i += 1
            spos = t.pos
            ent = self.ident()
            self.expect("in")
            st = {"entity": ent, "table": self.string(), "key": None, "columns": {}, "evidence": None, "pos": spos}
            if self.accept("key"):
                st["key"] = self.string()
            if self.accept("{"):
                while not self.at("}"):
                    a = self.ident()
                    self.expect("=")
                    st["columns"][a] = self.string()
                    self.accept(",")
                self.expect("}")
            if self.accept("evidence"):
                st["evidence"] = self.string()
            s.stores.append(st)
        elif w == "terms":
            self.i += 1
            self.expect("{")
            while not self.at("}"):
                tpos = self.cur.pos
                key = self.ident()
                while self.accept("."):
                    key += "." + self.ident()
                label = self.string()
                text = self.string() if self.cur.kind == "STRING" else ""
                if key in s.terms:
                    raise UpaError("TERM_DUPLICATE", f"terms: {key!r} already has a business label", tpos, "parse")
                s.terms[key] = (label, text, tpos)
            self.expect("}")
        elif w in ("decision", "assumption", "question", "exclusion", "language_gap", "human_decision", "parameter", "persists", "risk", "concurrency"):
            self.i += 1
            nid = self.ident()
            gap_kind = None
            # New form: language_gap ID kind regeneration "text" ...
            # The legacy language_gap ID "text" form remains valid.
            if w == "language_gap" and self.accept("kind"):
                gap_kind = self.ident()
            text = self.string()
            extra = ""
            options = []
            if w == "human_decision":
                self.expect("owner")
                extra = self.string()
                self.expect("options")
                self.expect("(")
                while True:
                    oid = self.ident()
                    options.append((oid, self.string()))
                    if not self.accept(","):
                        break
                self.expect(")")
            if w == "decision" and self.accept("alternatives"):
                extra = self.string()
            elif w in ("assumption", "parameter") and self.accept("owner"):
                extra = self.string()
            meta = {}
            if w == "language_gap":
                legacy = gap_kind is None and not any(self.at(k) for k in ("kind", "scope", "target", "impact", "required", "needed"))
                meta = {"kind": gap_kind or "language", "impact": "medium", "required": True, "legacy": legacy}
                seen_gap_fields = set()
                while True:
                    if self.accept("kind"):
                        if gap_kind is not None or "kind" in seen_gap_fields:
                            raise UpaError("DUPLICATE_GAP_FIELD", f"{nid}: kind is duplicated", self.cur.pos, "parse")
                        meta["kind"] = self.ident()
                        seen_gap_fields.add("kind")
                    elif self.accept("scope"):
                        if "scope" in seen_gap_fields:
                            raise UpaError("DUPLICATE_GAP_FIELD", f"{nid}: scope is duplicated", self.cur.pos, "parse")
                        meta["scope"] = self.string() if self.cur.kind == "STRING" else self.ident()
                        seen_gap_fields.add("scope")
                    elif self.accept("target"):
                        if "target" in seen_gap_fields:
                            raise UpaError("DUPLICATE_GAP_FIELD", f"{nid}: target is duplicated", self.cur.pos, "parse")
                        meta["target"] = self.dotted_name()
                        seen_gap_fields.add("target")
                    elif self.accept("impact"):
                        if "impact" in seen_gap_fields:
                            raise UpaError("DUPLICATE_GAP_FIELD", f"{nid}: impact is duplicated", self.cur.pos, "parse")
                        meta["impact"] = self.ident()
                        seen_gap_fields.add("impact")
                    elif self.at("required") or self.at("needed"):
                        field = self.ident()
                        if "required" in seen_gap_fields:
                            raise UpaError("DUPLICATE_GAP_FIELD", f"{nid}: required/needed is duplicated", self.cur.pos, "parse")
                        value = self.ident()
                        if value not in ("true", "false"):
                            raise UpaError("INVALID_GAP_REQUIRED", f"{nid}: {field} must be true or false", self.cur.pos, "parse")
                        meta["required"] = value == "true"
                        meta["required_keyword"] = field
                        seen_gap_fields.add("required")
                    else:
                        break
                meta["legacy"] = legacy
            if w in ("persists", "risk", "concurrency"):
                while True:
                    kw = next((k for k in ("hint", "normalized", "on", "mode", "severity", "owner", "by", "on_conflict", "protects") if self.at(k)), None)
                    if kw == "protects":
                        self.i += 1
                        meta[kw] = [self.ident()]
                        while self.accept(","):
                            meta[kw].append(self.ident())
                    elif kw in ("by", "on_conflict"):
                        self.i += 1
                        meta[kw] = self.ident()
                    elif kw:
                        self.i += 1
                        meta[kw] = self.string()
                    elif w == "persists" and self.accept("cache"):
                        meta["cache"] = True
                    else:
                        break
            note = Note(w, nid, text, extra, t.pos)
            note.meta = meta
            note.options = options
            if w == "decision" and self.accept("resolves"):
                note.resolves = self.ident()
                self.expect("=")
                note.chooses = self.ident()
            s.notes.append(note)
        elif w == "facts":
            self.i += 1
            self.expect("{")
            while not self.at("}"):
                s.facts.append(self.fact())
            self.expect("}")
        elif w in ("business_rules", "technical_rules"):
            self.i += 1
            self.expect("{")
            group = "business" if w == "business_rules" else "technical"
            while not self.at("}"):
                s.props.append(self.prop(group))
            self.expect("}")
        else:
            self.err(f"unknown declaration '{t.text}'")

    def system_map(self, spec: Spec) -> None:
        if spec.system_map_declared:
            self.err("only one system_map block is allowed per specification")
        spec.system_map_declared = True
        self.expect("system_map")
        self.expect("{")
        while not self.at("}"):
            if self.at("component"):
                spec.system_components.append(self.system_component())
            elif self.at("contract"):
                spec.system_contracts.append(self.system_contract())
            elif self.at("flow"):
                spec.system_flows.append(self.system_flow())
            elif self.at("observation"):
                spec.system_observations.append(self.system_observation())
            elif self.at("catalog"):
                spec.system_catalogs.append(self.system_catalog())
            elif self.at("job"):
                spec.system_jobs.append(self.system_job())
            elif self.at("rule"):
                spec.system_rules.append(self.system_rule())
            else:
                self.err("expected component, contract, flow, observation, catalog, or job inside system_map")
        self.expect("}")

    def system_component(self) -> SystemComponent:
        start = self.expect("component")
        component_id = self.ident()
        self.expect("{")
        values = {}
        depends_on = []
        realizes = []
        while not self.at("}"):
            key = self.ident()
            if key == "depends_on":
                depends_on.append(self.ident())
                continue
            if key == "realizes":
                target = self.ident()
                while self.at(".") and self.peek().kind == "IDENT":
                    self.i += 1
                    target += "." + self.ident()
                realizes.append(target)
                continue
            if key not in ("name", "kind", "scope", "role", "evidence", "runtime", "repository"):
                self.err(f"unknown field in component: {key}")
            if key in values:
                self.err(f"duplicate field in component: {key}")
            if key in ("kind", "scope"):
                values[key] = self.ident()
            else:
                values[key] = self.string()
        self.expect("}")
        required = ("name", "kind", "scope", "role", "evidence")
        missing = [key for key in required if key not in values]
        if missing:
            self.err(f"component {component_id} requires: {', '.join(missing)}", start)
        return SystemComponent(
            component_id, values["name"], values["kind"], values["scope"], values["role"],
            values["evidence"], values.get("runtime"), values.get("repository"), depends_on, start.pos, realizes)

    def system_contract(self) -> SystemContract:
        start = self.expect("contract")
        contract_id = self.ident()
        self.expect("{")
        values = {}
        callers = []
        while not self.at("}"):
            key = self.ident()
            if key == "caller":
                callers.append(self.ident())
                continue
            if key not in ("owner", "service", "protocol", "operation", "request", "response",
                           "usage", "source", "sha256", "evidence"):
                self.err(f"unknown field in contract: {key}")
            if key in values:
                self.err(f"duplicate field in contract: {key}")
            if key in ("owner", "usage"):
                values[key] = self.ident()
            else:
                values[key] = self.string()
        self.expect("}")
        required = ("owner", "service", "protocol", "operation", "request", "response",
                    "usage", "source", "sha256", "evidence")
        missing = [key for key in required if key not in values]
        if missing:
            self.err(f"contract {contract_id} requires: {', '.join(missing)}", start)
        return SystemContract(
            contract_id, values["owner"], values["service"], values["protocol"], values["operation"],
            values["request"], values["response"], values["usage"], values["source"], values["sha256"],
            values["evidence"], callers, start.pos)

    def system_flow(self) -> SystemFlow:
        start = self.expect("flow")
        flow_id = self.ident()
        self.expect("{")
        values = {}
        steps = []
        while not self.at("}"):
            key = self.ident()
            if key == "step":
                step_pos = self.cur.pos
                component = self.ident()
                operation = self.string()
                contract = self.ident() if self.accept("contract") else None
                steps.append(SystemFlowStep(component, operation, contract, step_pos))
                continue
            if key not in ("name", "status", "evidence"):
                self.err(f"unknown field in flow: {key}")
            if key in values:
                self.err(f"duplicate field in flow: {key}")
            values[key] = self.ident() if key == "status" else self.string()
        self.expect("}")
        required = ("name", "status", "evidence")
        missing = [key for key in required if key not in values]
        if missing:
            self.err(f"flow {flow_id} requires: {', '.join(missing)}", start)
        return SystemFlow(flow_id, values["name"], values["status"], values["evidence"], steps, start.pos)

    def system_observation(self) -> SystemObservation:
        start = self.expect("observation")
        observation_id = self.ident()
        self.expect("{")
        values = {}
        while not self.at("}"):
            key = self.ident()
            if key not in ("subject", "statement", "status", "evidence"):
                self.err(f"unknown field in observation: {key}")
            if key in values:
                self.err(f"duplicate field in observation: {key}")
            values[key] = self.ident() if key in ("subject", "status") else self.string()
        self.expect("}")
        required = ("subject", "statement", "status", "evidence")
        missing = [key for key in required if key not in values]
        if missing:
            self.err(f"observation {observation_id} requires: {', '.join(missing)}", start)
        return SystemObservation(
            observation_id, values["subject"], values["statement"], values["status"], values["evidence"], start.pos)

    def system_catalog(self) -> SystemCatalog:
        start = self.expect("catalog")
        catalog_id = self.ident()
        self.expect("{")
        values = {}
        entries = []
        while not self.at("}"):
            key = self.ident()
            if key == "value":
                entry = CatalogValue(self.ident())
                while self.at("code") or self.at("meaning"):
                    field_name = self.ident()
                    if getattr(entry, field_name) is not None:
                        self.err(f"duplicate field in value: {field_name}")
                    setattr(entry, field_name, self.string())
                entries.append(entry)
                continue
            if key not in ("owner", "source", "status", "evidence"):
                self.err(f"unknown field in catalog: {key}")
            if key in values:
                self.err(f"duplicate field in catalog: {key}")
            values[key] = self.ident() if key in ("owner", "status") else self.string()
        self.expect("}")
        missing = [key for key in ("owner", "source", "status", "evidence") if key not in values]
        if missing:
            self.err(f"catalog {catalog_id} requires: {', '.join(missing)}", start)
        return SystemCatalog(catalog_id, values["owner"], values["source"], values["status"],
                             values["evidence"], entries, start.pos)

    def system_rule(self) -> SystemRule:
        start = self.expect("rule")
        rule_id = self.ident()
        self.expect("{")
        values = {}
        whens = []
        thens = []
        while not self.at("}"):
            key = self.ident()
            if key == "when":
                whens.append(self.string())
                continue
            if key == "then":
                thens.append(self.string())
                continue
            if key not in ("subject", "kind", "status", "source", "evidence", "error"):
                self.err(f"unknown field in rule: {key}")
            if key in values:
                self.err(f"duplicate field in rule: {key}")
            values[key] = self.ident() if key in ("subject", "kind", "status") else self.string()
        self.expect("}")
        missing = [key for key in ("subject", "kind", "status", "source", "evidence") if key not in values]
        if missing:
            self.err(f"rule {rule_id} requires: {', '.join(missing)}", start)
        return SystemRule(rule_id, values["subject"], values["kind"], values["status"], values["source"],
                          values["evidence"], whens, thens, values.get("error"), start.pos)

    def system_job(self) -> SystemJob:
        start = self.expect("job")
        job_id = self.ident()
        self.expect("{")
        values = {}
        reads = []
        writes = []
        while not self.at("}"):
            key = self.ident()
            if key == "reads":
                reads.append(self.string())
                continue
            if key == "writes":
                writes.append(self.string())
                continue
            if key not in ("name", "owner", "kind", "status", "evidence"):
                self.err(f"unknown field in job: {key}")
            if key in values:
                self.err(f"duplicate field in job: {key}")
            values[key] = self.ident() if key in ("owner", "kind", "status") else self.string()
        self.expect("}")
        missing = [key for key in ("name", "owner", "kind", "status", "evidence") if key not in values]
        if missing:
            self.err(f"job {job_id} requires: {', '.join(missing)}", start)
        return SystemJob(job_id, values["name"], values["owner"], values["kind"], values["status"],
                         values["evidence"], reads, writes, start.pos)

    def validate_system_map(self, spec: Spec) -> None:
        if not spec.system_map_declared:
            return

        def fail(code: str, message: str, pos: tuple | None = None) -> None:
            raise UpaError(code, message, pos, "parse")

        components = {}
        for component in spec.system_components:
            if component.id in components:
                fail("DUPLICATE_COMPONENT", f"component duplicate: {component.id}", component.pos)
            if component.scope not in ("included", "boundary"):
                fail("INVALID_COMPONENT_SCOPE",
                     f"{component.id}: scope must be included or boundary", component.pos)
            if component.kind not in ("service", "gateway", "batch", "library", "channel", "external",
                                      "web_app", "database", "scheduler", "etl", "report"):
                fail("INVALID_COMPONENT_KIND", f"{component.id}: unknown kind {component.kind!r}", component.pos)
            components[component.id] = component

        for component in spec.system_components:
            for dependency in component.depends_on:
                if dependency not in components:
                    fail("UNKNOWN_COMPONENT_DEPENDENCY",
                         f"{component.id}: depends_on {dependency!r} is not declared", component.pos)
                if dependency == component.id:
                    fail("SELF_COMPONENT_DEPENDENCY", f"{component.id}: cannot depend on itself", component.pos)

        formal_ids = ({o.name for o in spec.operations} | {q.name for q in spec.queries}
                      | {c.name for c in spec.composes} | {p.id for p in spec.props}
                      | {f.id for f in spec.facts} | {n.id for n in spec.notes}
                      | {c.alias for c in spec.components})
        for component in spec.system_components:
            if len(set(component.realizes)) != len(component.realizes):
                fail("DUPLICATE_COMPONENT_REALIZES", f"{component.id}: duplicate realizes reference", component.pos)
            for target in component.realizes:
                if "." in target:
                    if target.split(".", 1)[0] not in {c.alias for c in spec.components}:
                        fail("UNKNOWN_COMPONENT_REALIZES",
                             f"{component.id}: realizes {target!r} does not start with a declared component alias",
                             component.pos)
                    continue
                if target not in formal_ids:
                    fail("UNKNOWN_COMPONENT_REALIZES",
                         f"{component.id}: realizes {target!r} is not a declared operation, query, rule, fact, or note",
                         component.pos)

        contracts = {}
        for contract in spec.system_contracts:
            if contract.id in contracts:
                fail("DUPLICATE_CONTRACT", f"contract duplicate: {contract.id}", contract.pos)
            if contract.owner not in components:
                fail("UNKNOWN_CONTRACT_OWNER",
                     f"{contract.id}: owner {contract.owner!r} is not declared", contract.pos)
            if contract.usage not in ("consumer_known", "traffic_observed", "no_known_consumer", "not_exercised"):
                fail("INVALID_CONTRACT_USAGE",
                     f"{contract.id}: usage must be consumer_known, traffic_observed, no_known_consumer "
                     "or not_exercised", contract.pos)
            if contract.usage in ("consumer_known", "traffic_observed") and not contract.callers:
                fail("CONTRACT_CALLER_REQUIRED",
                     f"{contract.id}: {contract.usage} requires at least one caller", contract.pos)
            if contract.usage not in ("consumer_known", "traffic_observed") and contract.callers:
                fail("CONTRACT_CALLER_CONFLICT",
                     f"{contract.id}: {contract.usage} does not allow callers", contract.pos)
            if len(contract.sha256) != 64 or any(c not in "0123456789abcdefABCDEF" for c in contract.sha256):
                fail("INVALID_CONTRACT_HASH", f"{contract.id}: sha256 must contain 64 hexadecimal digits",
                     contract.pos)
            if not all((contract.source, contract.evidence)):
                fail("CONTRACT_PROVENANCE_REQUIRED",
                     f"{contract.id}: source and evidence must not be empty", contract.pos)
            for caller_id in contract.callers:
                caller = components.get(caller_id)
                if caller is None:
                    fail("UNKNOWN_CONTRACT_CALLER",
                         f"{contract.id}: caller {caller_id!r} is not declared", contract.pos)
                if contract.usage == "consumer_known" and caller.scope != "included":
                    fail("INVALID_CONTRACT_CALLER_SCOPE",
                         f"{contract.id}: caller {caller_id!r} must be included", contract.pos)
            contracts[contract.id] = contract

        flows = set()
        for flow in spec.system_flows:
            if flow.id in flows:
                fail("DUPLICATE_FLOW", f"flow duplicate: {flow.id}", flow.pos)
            flows.add(flow.id)
            if flow.status not in ("source_verified", "production_observed", "inferred"):
                fail("INVALID_FLOW_STATUS",
                     f"{flow.id}: status must be source_verified, production_observed or inferred", flow.pos)
            if len(flow.steps) < 2:
                fail("FLOW_TOO_SHORT", f"{flow.id}: a flow requires at least two steps", flow.pos)
            if not flow.evidence:
                fail("FLOW_EVIDENCE_REQUIRED", f"{flow.id}: evidence must not be empty", flow.pos)
            for step in flow.steps:
                if step.component not in components:
                    fail("UNKNOWN_FLOW_COMPONENT",
                         f"{flow.id}: component {step.component!r} is not declared", step.pos)
                if step.contract:
                    contract = contracts.get(step.contract)
                    if contract is None:
                        fail("UNKNOWN_FLOW_CONTRACT",
                             f"{flow.id}: contract {step.contract!r} is not declared", step.pos)
                    if step.component != contract.owner:
                        fail("FLOW_CONTRACT_OWNER_MISMATCH",
                             f"{flow.id}: {step.contract} belongs to {contract.owner}, not to {step.component}",
                             step.pos)

        observations = set()
        valid_subjects = set(components) | set(contracts) | flows
        for observation in spec.system_observations:
            if observation.id in observations:
                fail("DUPLICATE_OBSERVATION", f"duplicate observation: {observation.id}", observation.pos)
            observations.add(observation.id)
            if observation.subject not in valid_subjects:
                fail("UNKNOWN_OBSERVATION_SUBJECT",
                     f"{observation.id}: subject {observation.subject!r} is not declared", observation.pos)
            if observation.status not in ("source_verified", "runtime_observed", "documented", "inferred"):
                fail("INVALID_OBSERVATION_STATUS",
                     f"{observation.id}: status must be source_verified, runtime_observed, documented or inferred",
                     observation.pos)
            if not observation.statement.strip() or not observation.evidence.strip():
                fail("OBSERVATION_PROVENANCE_REQUIRED",
                     f"{observation.id}: statement and evidence must not be empty", observation.pos)

        statuses = ("source_verified", "runtime_observed", "documented", "inferred")
        catalogs = set()
        for catalog in spec.system_catalogs:
            if catalog.id in catalogs:
                fail("DUPLICATE_CATALOG", f"catalog duplicate: {catalog.id}", catalog.pos)
            catalogs.add(catalog.id)
            if catalog.owner not in components:
                fail("UNKNOWN_CATALOG_OWNER", f"{catalog.id}: owner {catalog.owner!r} is not declared", catalog.pos)
            if catalog.status not in statuses:
                fail("INVALID_CATALOG_STATUS", f"{catalog.id}: status must be {', '.join(statuses)}", catalog.pos)
            if not catalog.values:
                fail("CATALOG_EMPTY", f"{catalog.id}: requires at least one value", catalog.pos)
            names = [value.name for value in catalog.values]
            if len(names) != len(set(names)):
                fail("DUPLICATE_CATALOG_VALUE", f"{catalog.id}: contains duplicate values", catalog.pos)
            if not catalog.source.strip() or not catalog.evidence.strip():
                fail("CATALOG_PROVENANCE_REQUIRED",
                     f"{catalog.id}: source and evidence must not be empty", catalog.pos)

        rules = set()
        for rule in spec.system_rules:
            if rule.id in rules:
                fail("DUPLICATE_RULE", f"duplicate rule: {rule.id}", rule.pos)
            rules.add(rule.id)
            if rule.subject not in valid_subjects:
                fail("UNKNOWN_RULE_SUBJECT", f"{rule.id}: subject {rule.subject!r} is not declared", rule.pos)
            if rule.kind not in ("validation", "decision", "calculation", "effect", "error_mapping", "state_transition"):
                fail("INVALID_RULE_KIND",
                     f"{rule.id}: kind must be validation, decision, calculation, effect, error_mapping or state_transition",
                     rule.pos)
            if rule.status not in statuses:
                fail("INVALID_RULE_STATUS", f"{rule.id}: status must be {', '.join(statuses)}", rule.pos)
            if not rule.thens:
                fail("RULE_OUTCOME_REQUIRED", f"{rule.id}: requires at least one then clause", rule.pos)
            if not rule.source.strip() or not rule.evidence.strip():
                fail("RULE_PROVENANCE_REQUIRED", f"{rule.id}: source and evidence must not be empty", rule.pos)

        jobs = set()
        for job in spec.system_jobs:
            if job.id in jobs:
                fail("DUPLICATE_JOB", f"job duplicate: {job.id}", job.pos)
            jobs.add(job.id)
            if job.owner not in components:
                fail("UNKNOWN_JOB_OWNER", f"{job.id}: owner {job.owner!r} is not declared", job.pos)
            if job.kind not in ("etl_workflow", "batch_process", "scheduled_job", "report"):
                fail("INVALID_JOB_KIND",
                     f"{job.id}: kind must be etl_workflow, batch_process, scheduled_job or report", job.pos)
            if job.status not in statuses:
                fail("INVALID_JOB_STATUS", f"{job.id}: status must be {', '.join(statuses)}", job.pos)
            if not job.evidence.strip():
                fail("JOB_PROVENANCE_REQUIRED", f"{job.id}: evidence must not be empty", job.pos)

    def external(self) -> External:
        pos = self.cur.pos
        self.i += 1
        name = self.ident()
        self.expect("version")
        ver = self.string()
        self.expect("source")
        src = self.string()
        self.expect("hash")
        h = self.string()
        self.expect("{")
        ops = []
        while not self.at("}"):
            op_pos = self.cur.pos
            self.expect("operation")
            on = self.ident()
            ps = self.params()
            op = ExternalOp(on, ps, pos=op_pos)
            if self.accept("http"):
                op.http = self.string()
            self.expect("{")
            while not self.at("}"):
                if self.accept("outcome"):
                    o = self.ident()
                    status, unc = None, False
                    while True:
                        if self.accept("status"):
                            status = self.string()
                        elif self.accept("uncertain"):
                            unc = True
                        else:
                            break
                    op.outcomes.append((o, status, unc))
                elif self.accept("resolve_by"):
                    op.resolve_by = self.ident()
                elif self.accept("on"):
                    o = self.ident()
                    self.expect("{")
                    sts = []
                    while not self.at("}"):
                        sts.append(self.stmt())
                    self.expect("}")
                    op.effects[o] = sts
                else:
                    self.err("expected 'outcome', 'resolve_by' u 'on'")
            self.expect("}")
            ops.append(op)
        self.expect("}")
        return External(name, ver, src, h, ops, pos)

    def fact(self) -> Fact:
        t = self.cur
        self.expect("fact")
        fid = self.ident()
        origin = self.origin()
        self.expect(":")
        if self.accept("every"):
            ent = self.ident()
            self.expect("has")
            card, lo, hi = self.multiplicity(t)
            attr = self.ident()
            self.expect(":")
            target = self.type_ref()
            f = Fact(fid, "has", ent, attr, origin, card, target, t.pos, lo, hi)
            f.multi = hi is None or hi > 1
            if f.multi:
                f.element = target
            return f
        ent = self.ident()
        self.expect(".")
        attr = self.ident()
        self.expect("is")
        kind = self.ident()
        if kind not in ("immutable", "unique"):
            self.err(f"expected 'immutable' or 'unique', not '{kind}'", t)
        return Fact(fid, kind, ent, attr, origin, None, None, t.pos)

    def multiplicity(self, t: Tok) -> tuple[str, int, int | None]:
        """Fact multiplicity: a keyword or range `[n]`, `[m..n]`, `[m..*]` (an omitted upper bound means unbounded)."""
        named = {"exactly_one": (1, 1), "zero_or_one": (0, 1), "zero_or_more": (0, None), "one_or_more": (1, None)}
        if self.accept("["):
            lo = self.integer()
            hi: int | None = lo
            if self.accept("."):
                self.expect(".")
                hi = None if self.accept("*") else self.integer()
            self.expect("]")
            if hi is not None and (hi < lo or hi == 0):
                self.err(f"invalid multiplicity [{lo}..{hi}]", t)
            for k, v in named.items():
                if v == (lo, hi):
                    return k, lo, hi
            return (f"[{lo}]" if lo == hi else f"[{lo}..{'*' if hi is None else hi}]"), lo, hi
        card = self.ident()
        if card not in named:
            self.err(f"unknown cardinality '{card}'", t)
        return card, *named[card]

    def integer(self) -> int:
        if self.cur.kind != "INT":
            self.err("expected an integer")
        v = int(self.cur.text)
        self.i += 1
        return v

    def attr(self) -> Attr:
        ghost = self.accept("ghost")
        n = self.ident()
        self.expect(":")
        return Attr(n, self.type_ref(), ghost)

    def type_ref(self) -> str:
        t = self.ident()
        if t == "List":
            self.expect("<")
            inner = self.type_ref()
            self.expect(">")
            return f"List<{inner}>"
        if t == "Option":
            self.expect("<")
            inner = self.type_ref()
            self.expect(">")
            return f"Option<{inner}>"
        return t

    def params(self) -> list[Param]:
        self.expect("(")
        ps: list[Param] = []
        if not self.at(")"):
            while True:
                n = self.ident()
                self.expect(":")
                pt = self.type_ref()
                gen = None
                if self.accept("generated"):
                    self.expect("for")
                    gen = self.ident()
                ps.append(Param(n, pt, generated_for=gen))
                if not self.accept(","):
                    break
        self.expect(")")
        return ps

    def operation(self) -> Operation:
        pos = self.cur.pos
        self.i += 1
        n = self.ident()
        ps = self.params()
        layer = self.layer()
        self.expect("{")
        cases: list[Case] = []
        while not self.at("}"):
            cp = self.cur.pos
            outcome = None
            success = False
            if self.accept("when"):
                cond = self.expr()
                if self.accept("outcome"):
                    outcome = self.ident()
                    success = self.accept("success")
            elif self.accept("otherwise"):
                cond = None
            else:
                self.err("expected 'when' u 'otherwise'")
            self.expect("{")
            stmts = []
            while not self.at("}"):
                stmts.append(self.stmt())
            self.expect("}")
            cases.append(Case(cond, stmts, cp, outcome, success))
        self.expect("}")
        op = Operation(n, ps, cases, pos)
        op.layer = layer
        return op

    def layer(self):
        if self.accept("layer"):
            l = self.ident()
            if l not in ("frontend", "backend", "shared"):
                self.err(f"unknown layer {l!r} (frontend | backend | shared)")
            return l
        return None

    def stmt(self) -> Stmt:
        pos = self.cur.pos
        if self.accept("set"):
            e = self.ident()
            self.expect(".")
            a = self.ident()
            self.expect("[")
            key = self.expr()
            self.expect("]")
            self.expect("=")
            return Stmt("set", e, key, a, self.expr(), [], pos)
        if self.accept("delete"):
            e = self.ident()
            self.expect("[")
            key = self.expr()
            self.expect("]")
            return Stmt("delete", e, key, None, None, [], pos)
        if self.at("for") and self.toks[self.i + 1].text == "each":
            self.i += 2
            var = self.ident()
            self.expect("in")
            ent = self.ident()
            cond = self.expr() if self.accept("where") else {"k": "bool", "v": True}
            inner = self.stmt()
            if inner.kind not in ("set", "delete") or inner.file or inner.var is not None:
                self.err("after 'for each x in E [where c]', expected 'set' or 'delete'")
            inner.var, inner.cond = var, cond
            inner.bulk_entity = ent
            return inner
        if self.accept("invoke"):
            ext = self.ident()
            self.expect(".")
            op = self.ident()
            self.expect("(")
            args = []
            if not self.at(")"):
                args.append(self.expr())
                while self.accept(","):
                    args.append(self.expr())
            self.expect(")")
            self.expect("yields")
            st = Stmt("invoke", ext, None, op, None, [], pos)
            st.args = args
            st.yields = self.ident()
            return st
        if self.accept("write_file"):
            # write_file <key-Identifier> = <content-Text>  ==  overwrites File[key].content
            key = self.expr()
            self.expect("=")
            st = Stmt("create", "File", key, None, None, [("content", self.expr())], pos)
            st.file = True
            return st
        if self.accept("create"):
            e = self.ident()
            self.expect("[")
            key = self.expr()
            self.expect("]")
            inits: list[tuple[str, dict]] = []
            if self.accept("{"):
                while True:
                    a = self.ident()
                    self.expect("=")
                    inits.append((a, self.expr()))
                    if not self.accept(","):
                        break
                self.expect("}")
            return Stmt("create", e, key, None, None, inits, pos)
        self.err("expected 'set', 'create', 'delete', 'for each', 'write_file', or 'invoke'")

    def compose(self) -> Compose:
        pos = self.cur.pos
        self.i += 1
        atomic = False
        if self.at("atomic") and self.toks[self.i + 1].text != "(":
            atomic = True
            self.i += 1
        n = self.ident()
        ps = self.params()
        layer = self.layer()
        self.expect("=")
        calls = []
        execution = []
        stage = 0
        while True:
            is_async = is_fanout = False
            while self.cur.kind == "IDENT" and self.cur.text in ("async", "parallel") and self.peek().kind == "IDENT":
                if self.cur.text == "async":
                    is_async = True
                else:
                    is_fanout = True
                self.i += 1
            guard = None
            loop = None
            if self.at("loop"):
                self.i += 1
                self.expect("while")
                loop = self.expr()
                self.expect("do")
            elif self.at("when"):
                self.i += 1
                guard = self.expr()
                self.expect("then")
            c = self.ident()
            while self.accept("."):
                c += "." + self.ident()
            self.expect("(")
            args = []
            if not self.at(")"):
                args.append(self.expr())
                while self.accept(","):
                    args.append(self.expr())
            self.expect(")")
            calls.append(Step(c, args, guard, loop=loop))
            execution.append({"call": c, "stage": stage, "async": is_async, "parallel": is_fanout,
                              "loop": loop is not None})
            if self.accept(";"):
                stage += 1
            elif not self.accept("&"):
                break
        comp = Compose(n, ps, calls, pos, layer)
        comp.atomic = atomic
        comp.execution = execution
        return comp

    def query(self) -> Query:
        pos = self.cur.pos
        self.i += 1
        n = self.ident()
        ps = self.params()
        layer = self.layer()
        self.expect("->")
        ret = self.type_ref()
        self.expect("=")
        return Query(n, ps, ret, self.expr(), pos, layer)

    # ---- Properties
    def origin(self) -> Origin | None:
        if not self.at("origin"):
            return None
        pos = self.cur.pos
        self.i += 1
        kind = self.ident()
        self.expect("(")
        ref = self.string() if self.cur.kind == "STRING" else self.ident()
        self.expect(")")
        return Origin(kind, ref, pos)

    def prop(self, group: str) -> Prop:
        t = self.cur
        kind = self.ident()
        if kind not in ("invariant", "law", "ensures", "repeat"):
            self.err(f"unknown property '{kind}'", t)
        pid = self.ident()
        targets: list[str] = []
        if kind in ("ensures", "repeat"):
            self.expect("for")
            targets.append(self.ident())
            while kind == "repeat" and self.accept(","):
                targets.append(self.ident())
        origin = self.origin()
        text = self.string()
        expr = None
        if kind != "repeat":
            self.expect(":")
            expr = self.expr()
        return Prop(kind, pid, group, text, origin, expr, targets, t.pos)

    # ---- expresiones
    def expr(self):
        return self.implies()

    def implies(self):
        l = self.or_()
        if self.at("=>"):
            pos = self.cur.pos
            self.i += 1
            return {"k": "bin", "op": "=>", "l": l, "r": self.implies(), "pos": pos}
        return l

    def or_(self):
        l = self.and_()
        while self.at("or"):
            pos = self.cur.pos
            self.i += 1
            l = {"k": "bin", "op": "or", "l": l, "r": self.and_(), "pos": pos}
        return l

    def and_(self):
        l = self.not_()
        while self.at("and"):
            pos = self.cur.pos
            self.i += 1
            l = {"k": "bin", "op": "and", "l": l, "r": self.not_(), "pos": pos}
        return l

    def not_(self):
        if self.at("not") and not (self.peek().text == "in"):
            pos = self.cur.pos
            self.i += 1
            return {"k": "un", "op": "not", "x": self.not_(), "pos": pos}
        return self.cmp()

    def cmp(self):
        l = self.add()
        t = self.cur
        if t.kind == "SYM" and t.text in ("==", "!=", "<", "<=", ">", ">="):
            self.i += 1
            return {"k": "bin", "op": t.text, "l": l, "r": self.add(), "pos": t.pos}
        if self.at("in"):
            self.i += 1
            return {"k": "in", "e": self.ident(), "key": l, "pos": t.pos}
        if self.at("not") and self.peek().text == "in":
            self.i += 2
            return {"k": "un", "op": "not", "x": {"k": "in", "e": self.ident(), "key": l, "pos": t.pos}, "pos": t.pos}
        return l

    def add(self):
        l = self.mul()
        while self.cur.kind == "SYM" and self.cur.text in ("+", "-"):
            t = self.cur
            self.i += 1
            l = {"k": "bin", "op": t.text, "l": l, "r": self.mul(), "pos": t.pos}
        return l

    def mul(self):
        l = self.atom()
        while self.cur.kind == "SYM" and self.cur.text == "*":
            t = self.cur
            self.i += 1
            l = {"k": "bin", "op": "*", "l": l, "r": self.atom(), "pos": t.pos}
        return l

    def atom(self):
        t = self.cur
        if t.kind == "INT":
            self.i += 1
            return {"k": "int", "v": int(t.text), "pos": t.pos}
        if t.kind == "STRING":
            self.i += 1
            return {"k": "str", "v": t.text, "pos": t.pos}
        if t.kind == "SYM" and t.text == "(":
            self.i += 1
            e = self.expr()
            self.expect(")")
            return e
        if t.kind != "IDENT":
            self.err("unexpected expression")
        w = t.text
        if w == "result" and self.peek().kind == "IDENT" and self.peek().text == "is":
            self.i += 2
            return {"k": "result_is", "v": self.ident(), "pos": t.pos}
        if w == "unchanged":
            self.i += 1
            free = []
            if self.at("except"):
                self.i += 1
                while True:
                    ent = self.ident()
                    attr = self.ident() if self.accept(".") else None
                    key = None
                    if self.accept("["):
                        key = self.expr()
                        self.expect("]")
                    free.append({"e": ent, "a": attr, "key": key})
                    if not self.accept(","):
                        break
            return {"k": "unchanged", "except": free, "pos": t.pos}
        if w in ("true", "false"):
            self.i += 1
            return {"k": "bool", "v": w == "true", "pos": t.pos}
        if w == "None":
            self.i += 1
            return {"k": "none", "pos": t.pos}
        if w == "Some":
            self.i += 1
            self.expect("(")
            x = self.expr()
            self.expect(")")
            return {"k": "some", "x": x, "pos": t.pos}
        if w == "if":
            self.i += 1
            c = self.expr()
            self.expect("then")
            a = self.expr()
            self.expect("else")
            return {"k": "if", "c": c, "t": a, "f": self.expr(), "pos": t.pos}
        if w == "old":
            self.i += 1
            self.expect("(")
            x = self.expr()
            self.expect(")")
            return {"k": "old", "x": x, "pos": t.pos}
        if w == "count" and self.peek().kind == "SYM" and self.peek().text == "(":
            self.i += 2
            x = self.expr()
            self.expect(")")
            return {"k": "count", "x": x, "pos": t.pos}
        if w == "sum":
            self.i += 1
            self.expect("(")
            e = self.ident()
            self.expect(".")
            a = self.ident()
            self.expect(")")
            return {"k": "sum", "e": e, "a": a, "pos": t.pos}
        if w == "mod" and self.peek().kind == "SYM" and self.peek().text == "(":
            self.i += 1
            self.expect("(")
            x = self.expr()
            self.expect(",")
            d = self.expr()
            self.expect(")")
            if d.get("k") != "int" or d["v"] <= 0:
                self.err("mod() requires a positive integer literal divisor: mod(expr, 10)")
            return {"k": "mod", "x": x, "d": d, "pos": t.pos}
        if w in ("trim", "upper", "lower", "digits_only") and self.peek().text == "(":
            self.i += 1
            self.expect("(")
            x = self.expr()
            self.expect(")")
            return {"k": "norm", "op": w, "x": x, "pos": t.pos}
        if w in ("concat", "to_text", "text_length", "contains",         "starts_with", "ends_with", "replace_first", "substring"):
            self.i += 1
            self.expect("(")
            args = [self.expr()]
            while self.accept(","):
                args.append(self.expr())
            self.expect(")")
            if w in ("to_text", "text_length"):
                if len(args) != 1:
                    self.err(f"{w}() requires one argument")
                return {"k": w, "x": args[0], "pos": t.pos}
            if w == "substring":
                if len(args) != 3:
                    self.err("substring() requires three arguments: text, start, length")
                return {"k": w, "text": args[0], "start": args[1], "length": args[2], "pos": t.pos}
            if w == "replace_first":
                if len(args) != 3:
                    self.err("replace_first() requires three arguments")
                return {"k": w, "text": args[0], "search": args[1], "replacement": args[2], "pos": t.pos}
            if len(args) != 2:
                self.err(f"{w}() requires two arguments")
            return {"k": w, "l": args[0], "r": args[1], "pos": t.pos}
        if w in ("forall", "exists"):
            self.i += 1
            v = self.ident()
            ent = self.ident() if self.accept("in") else None
            self.expect(":")
            return {"k": w, "v": v, "e": ent, "body": self.expr(), "pos": t.pos}
        self.i += 1
        if self.at("("):
            return {"k": "call", "alias": None, "q": w, "args": self.call_args(), "pos": t.pos}
        if self.at(".") and self.peek().kind == "IDENT":
            self.i += 1
            m = self.ident()
            if self.at("("):
                return {"k": "call", "alias": w, "q": m, "args": self.call_args(), "pos": t.pos}
            if self.at("["):
                self.i += 1
                key = self.expr()
                self.expect("]")
                return {"k": "attr", "e": w, "a": m, "key": key, "pos": t.pos}
            return {"k": "enum", "e": w, "v": m, "pos": t.pos}
        return {"k": "var", "n": w, "pos": t.pos}


def _call_args(self):
    self.expect("(")
    args = []
    if not self.at(")"):
        args.append(self.expr())
        while self.accept(","):
            args.append(self.expr())
    self.expect(")")
    return args


Parser.call_args = _call_args


def _call_args(self):
    self.expect("(")
    args = []
    if not self.at(")"):
        args.append(self.expr())
        while self.accept(","):
            args.append(self.expr())
    self.expect(")")
    return args


Parser.call_args = _call_args


def parse(src: str) -> Spec:
    return Parser(src).parse()
