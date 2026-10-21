import json

import pytest

from upa import compiler
from upa.errors import UpaError
from upa.parser import parse

BASE = '''spec Demo version "0.1"
entity Order { ok: Bool }
operation Alta(p: Identifier, present: Bool) {
  when present outcome Duplicate { }
  otherwise { create Order[p] { ok = true } }
}
operation Notify(p: Identifier) { otherwise { } }
operation Audit(p: Identifier) { otherwise { } }
'''

INVOKES = ('invokes Alta database "Orders.Insert" sync via present evidence "Order.cs#Create"\n'
           'invokes Notify queue "Notification queue" async\n')


def _ir(src: str, tmp_path=None) -> dict:
    _, ir, _, err = compiler.compile_source(src, None)
    assert not err.items
    return ir


def test_invokes_parse():
    spec = parse(BASE + INVOKES)
    assert [(i.target, i.kind, i.name, i.mode, i.via, i.evidence) for i in spec.invocations] == [
        ("Alta", "database", "Orders.Insert", "sync", ["present"], "Order.cs#Create"),
        ("Notify", "queue", "Notification queue", "async", [], ""),
    ]


def test_invokes_in_ir(tmp_path):
    inv = _ir(BASE + INVOKES, tmp_path)["invocations"]
    assert inv[0]["target"] == "Alta" and inv[0]["kind"] == "database" and inv[0]["via"] == ["present"]
    assert inv[1]["mode"] == "async"


def test_no_invokes_no_ir_key(tmp_path):
    assert "invocations" not in _ir(BASE, tmp_path)


@pytest.mark.parametrize("line,code", [
    ('invokes Alta cloud "x" sync', "INVALID_INVOKES_KIND"),
    ('invokes Alta api "x" later', "INVALID_INVOKES_MODE"),
    ('invokes Nada api "x" sync', "UNKNOWN_INVOKES_TARGET"),
    ('invokes Alta api "x" sync via result', "INVOKES_PARAM_UNKNOWN"),
])
def test_invokes_errors(line, code):
    with pytest.raises(UpaError) as e:
        parse(BASE + line + "\n")
    assert e.value.code == code


def test_compose_execution_stages_and_modes(tmp_path):
    ir = _ir(BASE + 'compose Flow(p: Identifier, present: Bool) = '
                    'Alta(p, present); Notify(p) & Audit(p); async parallel Notify(p)\n', tmp_path)
    ex = ir["composes"][0]["execution"]
    assert [(e["call"], e["stage"], e["async"], e["parallel"]) for e in ex] == [
        ("Alta", 0, False, False), ("Notify", 1, False, False), ("Audit", 1, False, False), ("Notify", 2, True, True)]


def test_sequential_compose_has_no_execution_key(tmp_path):
    ir = _ir(BASE + 'compose Flow(p: Identifier, present: Bool) = Alta(p, present); Notify(p)\n', tmp_path)
    assert "execution" not in ir["composes"][0]


def test_async_in_atomic_rejected():
    with pytest.raises(UpaError) as e:
        parse(BASE + 'compose atomic Flow(p: Identifier, present: Bool) = Alta(p, present); async Notify(p)\n')
    assert e.value.code == "COMPOSE_ASYNC_IN_ATOMIC"


def test_execution_modifiers_do_not_change_steps():
    a = parse(BASE + 'compose F(p: Identifier, present: Bool) = Alta(p, present); Notify(p); Audit(p)\n')
    b = parse(BASE + 'compose F(p: Identifier, present: Bool) = Alta(p, present); async Notify(p) & parallel Audit(p)\n')
    assert [(s[0], len(s[1])) for s in a.composes[0].calls] == [(s[0], len(s[1])) for s in b.composes[0].calls]


def test_loop_is_preserved_in_ir():
    ir = _ir(BASE + 'compose Flow(p: Identifier, present: Bool) = loop while present do Alta(p, present)\n')
    call = ir["composes"][0]["calls"][0]
    assert call["loop"]["k"] == "var" and call["loop"]["n"] == "present" and call["loop"]["ty"] == "bool"
    assert ir["composes"][0]["execution"][0]["loop"] is True


def test_loop_in_atomic_rejected():
    with pytest.raises(UpaError) as e:
        parse(BASE + 'compose atomic Flow(p: Identifier, present: Bool) = loop while present do Alta(p, present)\n')
    assert e.value.code == "COMPOSE_LOOP_IN_ATOMIC"


def test_loop_condition_must_be_bool():
    _, _, _, err = compiler.compile_source(
        BASE + 'compose Flow(p: Identifier, n: Natural, present: Bool) = loop while n do Alta(p, present)\n', None)
    assert any(x.code == "TYPE_MISMATCH" for x in err.items)


ASYNC_BOUNDARY = (
    'compose Publish(p: Identifier) = Notify(p)\n'
    'compose Consume(p: Identifier) = Audit(p)\n'
    'publishes OrderPublication from Publish via topic "orders" '
    'message "OrderCreated" confirmation confirmed evidence "Producer.java:10"\n'
    'subscribes OrderConsumer at Consume via topic "orders" '
    'message "OrderCreated" subscription "worker" durability durable '
    'processing deferred delivery at_least_once completion ack_on_return '
    'failure reject_on_exception evidence "Consumer.java:20"\n'
)


def test_async_endpoints_are_typed_correlated_and_projected(tmp_path):
    spec = parse(BASE + ASYNC_BOUNDARY)
    producer = spec.async_producers[0]
    consumer = spec.async_consumers[0]
    assert (producer.id, producer.operation, producer.transport, producer.confirmation) == (
        "OrderPublication", "Publish", "topic", "confirmed")
    assert (consumer.id, consumer.operation, consumer.subscription) == (
        "OrderConsumer", "Consume", "worker")
    ir = _ir(BASE + ASYNC_BOUNDARY, tmp_path)
    assert ir["async_producers"][0]["confirmation"] == "confirmed"
    assert ir["async_consumers"][0]["delivery"] == "at_least_once"
    assert ir["async_consumers"][0]["completion"] == "ack_on_return"
    assert "from" not in ir["async_producers"][0] and "to" not in ir["async_consumers"][0]
    assert ir["async_correlations"] == [{
        "id": "OrderPublication__OrderConsumer", "producer": "OrderPublication",
        "consumer": "OrderConsumer", "transport": "topic", "channel": "orders",
        "message": "OrderCreated",
    }]
    from upa.interaction import project
    interaction = project(ir)
    assert interaction["async_producers"] == ir["async_producers"]
    assert interaction["async_consumers"] == ir["async_consumers"]
    assert interaction["async_correlations"] == ir["async_correlations"]


def test_specs_without_async_endpoints_remain_compatible(tmp_path):
    ir = _ir(BASE, tmp_path)
    assert "async_producers" not in ir and "async_consumers" not in ir
    from upa.interaction import project
    assert "async_producers" not in project(ir)


def test_producer_without_consumer_and_optional_message(tmp_path):
    source = BASE + (
        'compose Publish(p: Identifier) = Notify(p)\n'
        'publishes Notifications from Publish via queue "notifications" '
        'confirmation fire_and_forget evidence "prod:1"\n')
    spec = parse(source)
    assert spec.async_producers[0].message == ""
    ir = _ir(source, tmp_path)
    assert "message" not in ir["async_producers"][0]
    assert "async_consumers" not in ir and "async_correlations" not in ir


def test_consumer_without_producer(tmp_path):
    source = BASE + (
        'compose Consume(p: Identifier) = Audit(p)\n'
        'subscribes Audit at Consume via stream "audit-stream" message "Event" '
        'subscription "auditor" durability durable processing deferred delivery at_least_once '
        'completion explicit_ack failure dead_letter evidence "consumer:1"\n')
    ir = _ir(source, tmp_path)
    assert ir["async_consumers"][0]["id"] == "Audit"
    assert "async_producers" not in ir and "async_correlations" not in ir


def test_fanout_and_contract_mismatch_are_derived_exactly(tmp_path):
    source = BASE + ASYNC_BOUNDARY + (
        'subscribes OrderAudit at Audit via topic "orders" message "OrderCreated" '
        'subscription "audit" durability durable processing event_driven delivery at_least_once '
        'completion explicit_ack failure dead_letter evidence "audit:1"\n'
        'subscribes DifferentContract at Audit via topic "orders" message "OrderUpdated" '
        'subscription "other" durability durable processing deferred delivery at_least_once '
        'completion explicit_ack failure dead_letter evidence "other:1"\n'
        'subscribes DifferentChannel at Audit via topic "Orders" message "OrderCreated" '
        'subscription "case-channel" durability durable processing deferred delivery at_least_once '
        'completion explicit_ack failure dead_letter evidence "other:2"\n'
        'subscribes DifferentTransport at Audit via queue "orders" message "OrderCreated" '
        'subscription "queue" durability durable processing deferred delivery at_least_once '
        'completion explicit_ack failure dead_letter evidence "other:3"\n')
    ir = _ir(source, tmp_path)
    assert {x["consumer"] for x in ir["async_correlations"]} == {
        "OrderConsumer", "OrderAudit"}
    assert "DifferentContract" not in {x["consumer"] for x in ir["async_correlations"]}
    assert "DifferentChannel" not in {x["consumer"] for x in ir["async_correlations"]}
    assert "DifferentTransport" not in {x["consumer"] for x in ir["async_correlations"]}


def test_async_endpoint_id_cannot_be_duplicated():
    with pytest.raises(UpaError) as e:
        parse(BASE + ASYNC_BOUNDARY + (
            'publishes OrderConsumer from Notify via topic "x" confirmation confirmed evidence "x"\n'))
    assert e.value.code == "DUPLICATE_ASYNC_ENDPOINT"


@pytest.mark.parametrize("line,code", [
    (ASYNC_BOUNDARY.replace("topic", "socket", 1), "INVALID_ASYNC_TRANSPORT"),
    (ASYNC_BOUNDARY.replace("at_least_once", "maybe_once", 1), "INVALID_ASYNC_DELIVERY"),
    (ASYNC_BOUNDARY.replace("from Publish", "from DoesNotExist", 1), "UNKNOWN_ASYNC_ENDPOINT"),
    (ASYNC_BOUNDARY.replace("from Publish", "from Publish.Nonexistent", 1), "UNKNOWN_ASYNC_ENDPOINT"),
    (ASYNC_BOUNDARY.replace('message "OrderCreated" subscription', 'message "" subscription', 1),
     "ASYNC_MESSAGE_EMPTY"),
    (ASYNC_BOUNDARY.replace('subscription "worker"', 'subscription ""', 1),
     "ASYNC_SUBSCRIPTION_EMPTY"),
])
def test_async_endpoint_validation(line, code):
    with pytest.raises(UpaError) as e:
        parse(BASE + line)
    assert e.value.code == code


def test_removed_async_transition_has_clear_migration_error():
    with pytest.raises(UpaError) as e:
        parse(BASE + 'async_transition Old from Notify to Audit via topic "x"\n')
    assert e.value.code == "ASYNC_TRANSITION_REMOVED"
    assert "publishes" in e.value.message and "subscribes" in e.value.message
