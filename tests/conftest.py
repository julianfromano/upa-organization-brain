
import pytest


@pytest.fixture
def no_closure(monkeypatch):
    """v15: deterministic closure is enabled by default; these tests pin v14 behavior (UPA_NO_CLOSURE=1)."""
    from upa import closure
    monkeypatch.setattr(closure, "ENABLED", False)
