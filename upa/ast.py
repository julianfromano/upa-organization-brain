"""UPA v0 AST. Expressions are dictionaries {"k": node_kind, ..., "pos": (line, column)}."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any

Expr = dict[str, Any]


class Step(tuple):
    """A compose step unpacked as (name, args). `guard` (a pure expression or None) conditions the step;
    `loop` represents a repetition whose condition is reevaluated before each iteration; `trail` records
    the component compositions from which the flattened step originated (outermost to innermost)."""

    def __new__(cls, name, args, guard=None, trail=(), loop=None):
        obj = super().__new__(cls, (name, args))
        obj.guard = guard
        obj.trail = tuple(trail)
        obj.loop = loop
        return obj

    def __getnewargs__(self):
        return (self[0], self[1], self.guard, self.trail, self.loop)


@dataclass
class Origin:
    kind: str  # literal | derived | assumption
    ref: str   # literal text citation or decision/assumption id
    pos: tuple | None = None


@dataclass
class Param:
    name: str
    type: str
    collection: dict | None = None   # List<T> parameter: multiplicity inherited from the attribute it is assigned to
    count_of: str | None = None      # technical parameter: cardinality of the referenced collection
    generated_for: str | None = None  # system-assigned identifier, always fresh relative to the entity


@dataclass
class Attr:
    name: str
    type: str
    ghost: bool = False


@dataclass
class Entity:
    name: str
    attrs: list[Attr]
    pos: tuple | None = None
    origin_block: str | None = None
    inferred: bool = False


@dataclass
class EnumDecl:
    name: str
    members: list[str]
    pos: tuple | None = None
    origin_block: str | None = None


@dataclass
class TypeAlias:
    name: str
    base: str
    scale: int = 0
    pos: tuple | None = None


@dataclass
class Stmt:
    kind: str  # set | create | delete
    entity: str
    key: Expr
    attr: str | None = None
    value: Expr | None = None
    inits: list[tuple[str, Expr]] = field(default_factory=list)
    pos: tuple | None = None
    file: bool = False   # write_file: file write (effect primitive)
    args: list = field(default_factory=list)   # invoke: arguments
    yields: str | None = None                  # invoke: parameter receiving the result
    var: str | None = None                     # for each: variable iterating over entity instances
    cond: Expr | None = None                   # for each: filter (over the variable and prior state)
    bulk_entity: str | None = None             # for each: iterated entity (must match the set/delete target)


@dataclass
class Case:
    cond: Expr | None  # None = otherwise
    stmts: list[Stmt]
    pos: tuple | None = None
    outcome: str | None = None   # observable case outcome (annotation; does not affect state)
    success: bool = False        # `outcome X success`: successful outcome with no effect (does not count as rejection)


@dataclass
class Operation:
    name: str
    params: list[Param]
    cases: list[Case]
    pos: tuple | None = None
    origin_block: str | None = None
    layer: str | None = None


@dataclass
class Compose:
    name: str
    params: list[Param]
    calls: list[tuple[str, list[Expr]]]
    pos: tuple | None = None
    layer: str | None = None
    atomic: bool = False   # all or nothing: if a step rejects, the entire composition has no effect
    # execution plan as written (before flattening): stages (';' advances, '&' is parallel), async,
    # parallel (fan-out), and loop (reevaluated condition).
    execution: list[dict] = field(default_factory=list)


@dataclass
class Invocation:
    """`invokes Op kind "target" sync|async [via p1, p2] [evidence "..."]`: an operation calls an external entity
    (database, API, service, queue, file, or directory). This is architecture metadata: the entity result arrives
    through the `via` parameters and is not interpreted by the verifier."""
    target: str
    kind: str
    name: str
    mode: str
    via: list[str] = field(default_factory=list)
    evidence: str = ""
    pos: tuple | None = None


INVOCATION_KINDS = ("database", "api", "service", "queue", "file", "directory", "mail")


@dataclass
class AsyncProducer:
    """Local asynchronous publication, independent of any consumer."""
    id: str
    operation: str
    transport: str
    channel: str
    message: str = ""
    confirmation: str = "unspecified"
    evidence: str = ""
    pos: tuple | None = None


@dataclass
class AsyncConsumer:
    """Local asynchronous subscription, independent of any producer."""
    id: str
    operation: str
    transport: str
    channel: str
    message: str = ""
    subscription: str = ""
    durability: str = "transient"
    processing: str = "deferred"
    delivery: str = "unspecified"
    completion: str = "unspecified"
    failure: str = "unspecified"
    evidence: str = ""
    pos: tuple | None = None


ASYNC_TRANSPORTS = ("queue", "topic", "stream", "event_bus")
ASYNC_PUBLICATION_CONFIRMATIONS = ("confirmed", "fire_and_forget", "unspecified")
ASYNC_DURABILITIES = ("durable", "transient")
ASYNC_PROCESSING = ("deferred", "scheduled", "event_driven")
ASYNC_DELIVERIES = ("at_least_once", "at_most_once", "exactly_once", "unspecified")
ASYNC_COMPLETIONS = ("ack_on_return", "explicit_ack", "unspecified")
ASYNC_FAILURES = ("reject_on_exception", "nack_on_exception", "dead_letter", "unspecified")


@dataclass
class Query:
    name: str
    params: list[Param]
    ret: str
    body: Expr
    pos: tuple | None = None
    layer: str | None = None


@dataclass
class Prop:
    kind: str   # invariant | law | ensures | repeat
    id: str
    group: str  # business | technical
    text: str
    origin: Origin | None
    expr: Expr | None
    targets: list[str] = field(default_factory=list)
    pos: tuple | None = None
    origin_block: str | None = None


@dataclass
class Note:
    kind: str  # decision | assumption | question | exclusion | language_gap | human_decision | parameter
    id: str
    text: str
    extra: str = ""
    pos: tuple | None = None
    resolves: str | None = None   # model ambiguity id (AMB_*)
    chooses: str | None = None    # selected alternative (MODEL_*)
    meta: dict = field(default_factory=dict)      # typed note metadata (gaps, persistence, risk, concurrency)
    options: list = field(default_factory=list)   # human_decision: [(id, text)]


@dataclass
class Fact:
    id: str
    kind: str  # has | immutable | unique
    entity: str
    attr: str
    origin: "Origin | None" = None
    card: str | None = None     # exactly_one | zero_or_one | zero_or_more | one_or_more | m..n | m..*
    target: str | None = None
    pos: tuple | None = None
    min: int | None = None      # minimum multiplicity (only for kind == has)
    max: int | None = None      # maximum multiplicity; None = unbounded
    multi: bool = False         # homogeneous collection (maximum is absent or greater than one)
    element: str | None = None  # collection element type


@dataclass
class ExternalOp:
    name: str
    params: list[Param]
    http: str | None = None
    outcomes: list[tuple] = field(default_factory=list)   # (name, status|None, uncertain)
    resolve_by: str | None = None
    effects: dict = field(default_factory=dict)           # outcome -> [Stmt]
    pos: tuple | None = None


@dataclass
class External:
    name: str
    version: str
    source: str
    hash: str
    ops: list[ExternalOp]
    pos: tuple | None = None


@dataclass
class SystemComponent:
    id: str
    name: str
    kind: str
    scope: str
    role: str
    evidence: str
    runtime: str | None = None
    repository: str | None = None
    depends_on: list[str] = field(default_factory=list)
    pos: tuple | None = None
    realizes: list[str] = field(default_factory=list)


@dataclass
class SystemContract:
    id: str
    owner: str
    service: str
    protocol: str
    operation: str
    request: str
    response: str
    usage: str
    source: str
    sha256: str
    evidence: str
    callers: list[str] = field(default_factory=list)
    pos: tuple | None = None


@dataclass
class SystemFlowStep:
    component: str
    operation: str
    contract: str | None = None
    pos: tuple | None = None


@dataclass
class SystemFlow:
    id: str
    name: str
    status: str
    evidence: str
    steps: list[SystemFlowStep] = field(default_factory=list)
    pos: tuple | None = None


@dataclass
class SystemObservation:
    id: str
    subject: str
    statement: str
    status: str
    evidence: str
    pos: tuple | None = None


@dataclass
class CatalogValue:
    name: str
    code: str | None = None
    meaning: str | None = None


@dataclass
class SystemCatalog:
    id: str
    owner: str
    source: str
    status: str
    evidence: str
    values: list[CatalogValue] = field(default_factory=list)
    pos: tuple | None = None


@dataclass
class SystemJob:
    id: str
    name: str
    owner: str
    kind: str
    status: str
    evidence: str
    reads: list[str] = field(default_factory=list)
    writes: list[str] = field(default_factory=list)
    pos: tuple | None = None


@dataclass
class SystemRule:
    id: str
    subject: str
    kind: str
    status: str
    source: str
    evidence: str
    whens: list[str] = field(default_factory=list)
    thens: list[str] = field(default_factory=list)
    error: str | None = None
    pos: tuple | None = None


@dataclass
class ComponentRef:
    alias: str
    ref: str       # dotted path, e.g. org.accounts.account_id
    version: str
    pos: tuple | None = None


@dataclass
class Spec:
    kind: str  # spec | block
    name: str
    version: str
    uses: list[tuple[str, str]] = field(default_factory=list)
    level: str | None = None
    tier: str | None = None   # analysis tier: atom | subprocess | process | macroprocess
    inherited_ids: set = field(default_factory=set)
    layers: set = field(default_factory=set)   # inherited layers (specifications at the 'layer' level)
    aliases: list[TypeAlias] = field(default_factory=list)
    enums: list[EnumDecl] = field(default_factory=list)
    entities: list[Entity] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    initial: Expr | None = None
    initial_origin: Origin | None = None
    operations: list[Operation] = field(default_factory=list)
    composes: list[Compose] = field(default_factory=list)
    queries: list[Query] = field(default_factory=list)
    props: list[Prop] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)
    externals: list[External] = field(default_factory=list)
    invocations: list[Invocation] = field(default_factory=list)
    async_producers: list[AsyncProducer] = field(default_factory=list)
    async_consumers: list[AsyncConsumer] = field(default_factory=list)
    terms: dict = field(default_factory=dict)   # technical name -> (business label, explanation, position)
    stores: list = field(default_factory=list)   # entity -> real table: {entity, table, key, columns, evidence, position}
    collections: list[dict] = field(default_factory=list)   # homogeneous collections declared with multiplicity
    system_map_declared: bool = False
    system_components: list[SystemComponent] = field(default_factory=list)
    system_contracts: list[SystemContract] = field(default_factory=list)
    system_flows: list[SystemFlow] = field(default_factory=list)
    system_observations: list[SystemObservation] = field(default_factory=list)
    system_catalogs: list[SystemCatalog] = field(default_factory=list)
    system_jobs: list[SystemJob] = field(default_factory=list)
    system_rules: list[SystemRule] = field(default_factory=list)
    # composition: component Alias = a.b.c@version (child UPA is verified separately)
    components: list[ComponentRef] = field(default_factory=list)
    component_info: list[dict] = field(default_factory=list)   # resolved by components.expand_components
    component_calls: list[dict] = field(default_factory=list)  # inlined calls Alias.Q(...) / Q(...)
