"""Composition behavior across component tiers, interfaces, and navigation."""
from upa import compiler, interface, navigation
from upa.verifier import verify

LEDGER = """spec Ledger version "1.0" tier atom
entity Account { balance: Int }
decision DD_ACCOUNT "Accounts keep nonnegative balances"
assumption AS_EMPTY "Accounts start empty" owner "test"
initial origin assumption(AS_EMPTY): forall account: not (account in Account)
operation Increase(account: Identifier, amount: Int) {
  when amount >= 0 and account in Account { set Account.balance[account] = Account.balance[account] + amount }
  when amount >= 0 { create Account[account] { balance = amount } }
  otherwise { }
}
operation Decrease(account: Identifier, amount: Int) {
  when amount >= 0 and account in Account and Account.balance[account] >= amount {
    set Account.balance[account] = Account.balance[account] - amount
  }
  otherwise { }
}
business_rules {
  invariant BR_NONNEGATIVE origin derived(DD_ACCOUNT) "account balances never go below zero":
    forall account: (account in Account) => Account.balance[account] >= 0
}
"""

TRANSFER = """spec Transfer version "1.0" tier process
component Ledger = org.accounts.ledger@1.0
compose atomic Move(source: Identifier, target: Identifier, amount: Int) =
  Ledger.Decrease(source, amount); Ledger.Increase(target, amount)
"""

UNSAFE_TRANSFER = """spec UnsafeTransfer version "1.0" tier process
component Transfer = org.orders.transfer@1.0
compose Move(source: Identifier, target: Identifier, amount: Int) =
  Transfer.Move(source, target, amount)
"""

WORKFLOW = """spec Workflow version "1.0" tier macroprocess
component Transfers = org.orders.transfer@1.0
compose atomic Complete(source: Identifier, target: Identifier, amount: Int) =
  Transfers.Move(source, target, amount)
"""


def _rules_root(tmp_path):
    root = tmp_path / "rules"
    specs = {
        "org/accounts/ledger/ledger.upa": LEDGER,
        "org/orders/transfer/transfer.upa": TRANSFER,
        "org/orders/unsafe/unsafe.upa": UNSAFE_TRANSFER,
        "org/orders/workflow/workflow.upa": WORKFLOW,
    }
    for relative, source in specs.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    return root


def _verdict(path, root):
    _, ir, _, errors = compiler.compile_source(path.read_text(encoding="utf-8"), None, root)
    assert not errors, errors.to_json()
    return verify(ir, 20000, None)


def test_atomic_compose_over_component_operations_is_verified(tmp_path):
    root = _rules_root(tmp_path)
    verdict = _verdict(root / "org/orders/transfer/transfer.upa", root)
    assert verdict["verdict"] == "VERIFIED"
    assert {rule["rule"] for rule in verdict["rules"]} >= {"Ledger_BR_NONNEGATIVE"}


def test_non_atomic_compose_cannot_wrap_an_atomic_component(tmp_path):
    root = _rules_root(tmp_path)
    source = UNSAFE_TRANSFER
    _, _, _, errors = compiler.compile_source(source, None, root)
    assert "COMPOSE_ATOMICITY_LOST" in {error["error"] for error in errors.to_json()}


def test_macroprocess_flattens_atomic_process_and_reproves(tmp_path):
    root = _rules_root(tmp_path)
    verdict = _verdict(root / "org/orders/workflow/workflow.upa", root)
    assert verdict["verdict"] == "VERIFIED"


def test_tier_order_violation(tmp_path):
    root = _rules_root(tmp_path)
    path = root / "org/accounts/ledger/ledger.upa"
    path.write_text(LEDGER.replace("tier atom", "tier process"), encoding="utf-8")
    parent = root / "org/orders/transfer/transfer.upa"
    parent.write_text(TRANSFER.replace("tier process", "tier atom"), encoding="utf-8")
    _, _, _, errors = compiler.compile_source(parent.read_text(encoding="utf-8"), None, root)
    assert "TIER_ORDER" in {error["error"] for error in errors.to_json()}


def test_child_tier_cannot_be_higher(tmp_path):
    root = _rules_root(tmp_path)
    child = root / "org/accounts/ledger/ledger.upa"
    child.write_text(LEDGER.replace("tier atom", "tier macroprocess"), encoding="utf-8")
    parent = root / "org/orders/transfer/transfer.upa"
    _, _, _, errors = compiler.compile_source(parent.read_text(encoding="utf-8"), None, root)
    assert "TIER_ORDER" in {error["error"] for error in errors.to_json()}


def test_same_tier_nesting_has_no_depth_cap(tmp_path):
    root = _rules_root(tmp_path)
    base = root / "org/orders/transfer/transfer.upa"
    base.write_text(TRANSFER.replace("tier process", "tier macroprocess"), encoding="utf-8")
    previous_ref = "org.orders.transfer"
    previous_operation = "Move"
    for index in range(1, 5):
        directory = root / "org" / "nested" / f"level{index}"
        directory.mkdir(parents=True)
        source = (
            f'spec Level{index} version "1.0" tier macroprocess\n\n'
            f'component Previous = {previous_ref}@1.0\n\n'
            f'compose atomic Run{index}(source: Identifier, target: Identifier, amount: Int) = '
            f'Previous.{previous_operation}(source, target, amount)\n'
        )
        (directory / f"level{index}.upa").write_text(source, encoding="utf-8")
        previous_ref = f"org.nested.level{index}"
        previous_operation = f"Run{index}"
    top = root / "org/nested/level4/level4.upa"
    spec, ir, _, errors = compiler.compile_source(top.read_text(encoding="utf-8"), None, root)
    assert not errors, errors.to_json()
    assert spec.component_depth >= 5
    assert verify(ir, 20000, None)["verdict"] == "VERIFIED"


def test_atomicity_cannot_be_lost(tmp_path):
    root = _rules_root(tmp_path)
    path = root / "org/orders/workflow/workflow.upa"
    source = WORKFLOW.replace("compose atomic", "compose")
    path.write_text(source, encoding="utf-8")
    _, _, _, errors = compiler.compile_source(source, None, root)
    assert "COMPOSE_ATOMICITY_LOST" in {error["error"] for error in errors.to_json()}


def test_unknown_component_operation(tmp_path):
    root = _rules_root(tmp_path)
    path = root / "org/orders/transfer/transfer.upa"
    source = TRANSFER.replace("Ledger.Increase", "Ledger.Unknown")
    path.write_text(source, encoding="utf-8")
    _, _, _, errors = compiler.compile_source(source, None, root)
    assert "COMPONENT_OPERATION_UNKNOWN" in {error["error"] for error in errors.to_json()}


def test_interface_and_navigation(tmp_path):
    root = _rules_root(tmp_path)
    transfer_path = root / "org/orders/transfer/transfer.upa"
    result = interface.interface(transfer_path)
    assert result["tier"] == "process" and result["depth"] == 1 and result["verdict"] == "VERIFIED"
    assert result["composes"][0]["atomic"] is True
    assert len(interface.to_markdown(result)) < len(TRANSFER) * 6
    tree = navigation.tree(root / "org/orders/workflow/workflow.upa")
    assert tree["children"][0]["node"]["children"][0]["alias"] == "Ledger"
    users = navigation.used_by(root, "org.accounts.ledger")
    assert {user["ref"] for user in users["composed_by"]} == {"org.orders.transfer"}
    assert any(unit["ref"] == "org.orders.transfer" and len(unit["used_by"]) == 1
               for unit in navigation.units(root))
