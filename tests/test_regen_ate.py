from upa import compiler
from targets.dotnet import generator
from targets.dotnet.refmodel import Ref


def test_guarded_compose_is_reproducible_by_refmodel_and_dotnet_generator(tmp_path):
    src = '''
spec GuardedRegen version "0.1"

operation Mark(id: Identifier, v: Natural) {
  when true { create Seen[id] { value = v } }
}

compose MaybeMark(id: Identifier, enabled: Bool, v: Natural) =
  when enabled then Mark(id, v)
'''
    _, ir, _, errors = compiler.compile_source(src, None)
    assert not errors.items

    ref = Ref(ir)
    skipped = ref.apply(ref.empty(), "MaybeMark", [1, 0, 99])
    taken = ref.apply(ref.empty(), "MaybeMark", [1, 1, 99])
    assert skipped["Seen.exists"] == {}
    assert taken["Seen.exists"] == {1: 1}
    assert taken["Seen.value"] == {1: 99}

    out = tmp_path / "cs"
    generator.generate(ir, out)
    domain = (out / "Domain.g.cs").read_text(encoding="utf-8")
    assert "if (p_enabled)" in domain
    assert "s = Mark(s, p_id, p_v);" in domain
    assert '"MaybeMark" => MaybeMark(s, a[0], (a[1] != 0), a[2])' in domain
