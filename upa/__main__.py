import argparse
import json
import sys
from pathlib import Path

from . import compiler


def _emit_viewer(res: dict, out: Path, enabled: bool) -> None:
    # The viewer is included in every build; viewer failures never invalidate compilation.
    if not enabled or not isinstance(res, dict):
        return
    try:
        from . import viewer as _v
        res["viewer"] = str(_v.build_viewer([out], out / "viewer.html"))
    except Exception as exc:
        res["viewer"] = None
        res["viewer_error"] = f"{type(exc).__name__}: {exc}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="upa")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="builds the model, verifies it, and emits artifacts")
    b.add_argument("file")
    b.add_argument("--scope")
    b.add_argument("--out")
    b.add_argument("--timeout-ms", type=int, default=60000)
    b.add_argument("--strict-model", action="store_true", help="fail if design decisions remain unresolved")
    b.add_argument("--no-viewer", action="store_true", help="do not generate an HTML viewer alongside the artifacts")
    w = sub.add_parser("why", help="why does this model member exist?")
    w.add_argument("file")
    w.add_argument("member")
    w.add_argument("--scope")
    g = sub.add_parser("generate", help="generate .NET code from the verified IR")
    g.add_argument("ir")
    g.add_argument("--out", required=True)
    f = sub.add_parser("frontend", help="infer and generate the UI from the Logical IR and a technical profile")
    f.add_argument("ir")
    f.add_argument("--profile", required=True)
    f.add_argument("--out", required=True)
    ip = sub.add_parser("project-interactions", help="project the Logical IR to an Interaction IR without a provider")
    ip.add_argument("ir")
    ip.add_argument("--out")
    i = sub.add_parser("import-openapi", help="import an OpenAPI/Swagger document as a structural external contract")
    i.add_argument("file")
    i.add_argument("--name")
    i.add_argument("--out", required=True)
    gd = sub.add_parser("guide", help="analyst conversation status and the next question to ask")
    gd.add_argument("file")
    gd.add_argument("--out")
    sub.add_parser("catalog", help="explicit catalog of Business Surface phrases (Markdown)")
    ex = sub.add_parser("explain", help="explain why UPA believes each rule (corpus.json and corpus.md)")
    ex.add_argument("file")
    ex.add_argument("--out")
    ex.add_argument("--rule", help="show only this rule’s explanation chain")
    ex.add_argument("--no-build", action="store_true", help="use the verification result already written to --out")
    vw = sub.add_parser("view", help="HTML viewer for non-technical users (flows, states, rules, and open questions)")
    vw.add_argument("paths", nargs="+", help="folder(s) containing `upa build` artifacts or compiled .upa file(s)")
    vw.add_argument("--out", default="viewer.html")
    vw.add_argument("--baseline", help="folder with an earlier build to show the semantic diff")
    df = sub.add_parser("diff", help="semantic diff between two versions (rules, guards, effects, boundaries, and obligations)")
    df.add_argument("old")
    df.add_argument("new")
    df.add_argument("--out")
    itf = sub.add_parser("interface", help="compact component contract (operations, outcomes, and rules)")
    itf.add_argument("file")
    itf.add_argument("--json", action="store_true")
    itf.add_argument("--no-verify", action="store_true")
    tr = sub.add_parser("tree", help="macroprocess -> process -> atom -> rule tree")
    tr.add_argument("file")
    tr.add_argument("--rules", action="store_true", help="include every rule")
    tr.add_argument("--no-verify", action="store_true")
    un = sub.add_parser("units", help="index reusable UPAs in a rules/ folder (tier, size, and consumers)")
    un.add_argument("root")
    un.add_argument("--used-by", dest="used_by", help="which UPAs use this one (dot-separated reference)")
    ow = sub.add_parser("owners", help="which UPAs read or write each real table (`store` declarations) in a rules/ tree")
    ow.add_argument("root")
    ow.add_argument("--out", help="write owners.json and owners.md to this folder")
    rp = sub.add_parser("replay", help="replay observed cases (JSON Lines) against the verified IR: reproduce or contradict")
    rp.add_argument("ir", help="canonical-ir.json or a compiled UPA folder")
    rp.add_argument("--cases", required=True)
    rp.add_argument("--out", help="write replay.json and replay.md to this folder")
    a = ap.parse_args(argv)

    if a.cmd == "owners":
        from .boundary import owners, owners_markdown
        sys.stdout.reconfigure(encoding="utf-8")
        rep = owners(Path(a.root))
        if a.out:
            Path(a.out).mkdir(parents=True, exist_ok=True)
            (Path(a.out) / "owners.json").write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
            (Path(a.out) / "owners.md").write_text(owners_markdown(rep), encoding="utf-8")
        print(owners_markdown(rep))
        return 0
    if a.cmd == "replay":
        from .replay import load_cases, replay, to_markdown
        sys.stdout.reconfigure(encoding="utf-8")
        irp = Path(a.ir)
        irp = irp / "canonical-ir.json" if irp.is_dir() else irp
        rep = replay(json.loads(irp.read_text(encoding="utf-8")), load_cases(Path(a.cases)))
        if a.out:
            Path(a.out).mkdir(parents=True, exist_ok=True)
            (Path(a.out) / "replay.json").write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
            (Path(a.out) / "replay.md").write_text(to_markdown(rep), encoding="utf-8")
        print(json.dumps({k: v for k, v in rep.items() if k != "rows"}, ensure_ascii=False))
        return 0 if "CONTRADICTS" not in rep["by_status"] else 3

    if a.cmd in ("interface", "tree", "units"):
        sys.stdout.reconfigure(encoding="utf-8")
        if a.cmd == "interface":
            from . import interface as _i
            res = _i.interface(Path(a.file), verify_rules=not a.no_verify)
            print(json.dumps(res, ensure_ascii=False, indent=2) if a.json else _i.to_markdown(res))
        elif a.cmd == "tree":
            from . import navigation as _n
            print("\n".join(_n.render_tree(_n.tree(Path(a.file), verify_rules=not a.no_verify), a.rules)))
        else:
            from . import navigation as _n
            if a.used_by:
                print(json.dumps(_n.used_by(Path(a.root), a.used_by), ensure_ascii=False, indent=2))
            else:
                for u in _n.units(Path(a.root)):
                    print(f"{u['ref']}@{u['version']}  [{u['tier']}]  ops={u['own_operations']} composes={u['composes']} "
                          f"queries={len(u['queries'])} rules={u['own_rules']}  used_by={len(u['used_by'])}")
        return 0

    if a.cmd == "view":
        from . import viewer as _v
        out = _v.build_viewer([Path(x) for x in a.paths], Path(a.out), a.baseline)
        print(str(out))
        return 0

    if a.cmd == "diff":
        from . import insight as _i
        d = _i.diff_trees(Path(a.old), Path(a.new))
        md = _i.diff_markdown(d)
        if a.out:
            Path(a.out).write_text(md, encoding="utf-8")
            Path(a.out).with_suffix(".json").write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        print(md)
        return 0

    if a.cmd == "catalog":
        from . import surface as _s
        sys.stdout.reconfigure(encoding="utf-8")
        print(_s.catalog_markdown())
        return 0

    if a.cmd == "explain":
        from . import corpus as _c
        p = Path(a.file)
        out = Path(a.out) if a.out else p.parent / "out"
        cp = _c.build_corpus(p, out, rebuild=not a.no_build)
        sys.stdout.reconfigure(encoding="utf-8")
        if a.rule:
            print(json.dumps([r for r in cp["rules"] if r["rule"] == a.rule], ensure_ascii=False, indent=2))
        else:
            print(json.dumps({"verdict": cp["verdict"], "rules": len(cp["rules"]), "provenance_counts": cp["provenance_counts"],
                              "corpus": str(out / "corpus.md")}, ensure_ascii=False, indent=2))
        return 0 if cp["verdict"] == "VERIFIED" else 1

    if a.cmd == "guide":
        from . import guide as _g
        p = Path(a.file)
        res = _g.guide(p.read_text(encoding="utf-8"), p)
        if a.out:
            Path(a.out).mkdir(parents=True, exist_ok=True)
            (Path(a.out) / "guide-report.json").write_text(json.dumps(res, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ready_to_build") else 3

    if a.cmd == "build":
        p = Path(a.file)
        from . import surface
        if surface.is_surface(p.read_text(encoding="utf-8")):
            out_dir = Path(a.out) if a.out else p.parent
            res = surface.build_surface(p, out_dir, a.timeout_ms)
            _emit_viewer(res, out_dir, not a.no_viewer)
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return res["exit"]
        out_dir = Path(a.out) if a.out else p.parent
        res = compiler.build(p, Path(a.scope) if a.scope else None, out_dir, a.timeout_ms)
        _emit_viewer(res, out_dir, not a.no_viewer)
        print(json.dumps({k: v for k, v in res.items() if not k.startswith("_")}, ensure_ascii=False, indent=2))
        if res["exit"] == 0 and a.strict_model and res["pending_design_decisions"]:
            return 4
        return res["exit"]
    if a.cmd == "why":
        p = Path(a.file)
        scope = Path(a.scope).read_text(encoding="utf-8") if a.scope else None
        _, _, report, errors = compiler.compile_source(p.read_text(encoding="utf-8"), scope)
        from .infer import why
        print(json.dumps(why(report, a.member), ensure_ascii=False, indent=2))
        return 0
    if a.cmd == "generate":
        from targets.dotnet import generator
        ir = json.loads(Path(a.ir).read_text(encoding="utf-8"))
        from targets.dotnet import refmodel
        sc = Path(a.ir).parent / "tests" / "Scenarios.cs"
        generator.generate(ir, Path(a.out), sc if sc.exists() else None)
        (Path(a.out) / "conformance-traces.json").write_text(json.dumps(refmodel.traces(ir)), encoding="utf-8")
        return 0
    if a.cmd == "frontend":
        from .frontend import FrontendError, generate
        try:
            result = generate(Path(a.ir), Path(a.profile), Path(a.out))
        except (FrontendError, OSError, json.JSONDecodeError) as exc:
            print(json.dumps({"error": "FRONTEND_GENERATION_FAILED", "message": str(exc)},
                             ensure_ascii=False), file=sys.stderr)
            return 1
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if a.cmd == "project-interactions":
        from .interaction import project
        ir_path = Path(a.ir)
        interaction_ir = project(json.loads(ir_path.read_text(encoding="utf-8")))
        output = Path(a.out) if a.out else ir_path.parent / "interaction-ir.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(interaction_ir, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                          encoding="utf-8")
        print(json.dumps({
            "format": interaction_ir["format"],
            "operations": len(interaction_ir["operations"]),
            "capabilities": [item["name"] for item in interaction_ir["capabilities"]],
            "output": str(output),
        }, ensure_ascii=False, indent=2))
        return 0
    if a.cmd == "import-openapi":
        from .openapi_import import import_openapi, to_upa
        con = import_openapi(Path(a.file), a.name)
        out = Path(a.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{con['name']}.openapi.json").write_text(json.dumps(con, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (out / f"{con['name']}.external.upa").write_text(to_upa(con), encoding="utf-8")
        print(json.dumps({"contract": con["name"], "hash": con["hash"], "operations": [o["name"] for o in con["operations"]]}))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
