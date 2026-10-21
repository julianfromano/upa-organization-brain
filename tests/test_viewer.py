import json
import re
import subprocess
from html.parser import HTMLParser
from pathlib import Path

from upa import viewer

DEMO_SPEC = '''spec OrderManagement version "1.0" tier process
operation PlaceOrder(orderId: Identifier, quantity: Natural) {
  when quantity == 0 outcome INVALID_QUANTITY { }
  otherwise { create Order[orderId] { quantity = quantity } }
}
compose PlaceOrderFlow(orderId: Identifier, quantity: Natural) = PlaceOrder(orderId, quantity)
'''


def _build_demo(tmp_path, name="demo"):
    from upa.__main__ import main

    src = tmp_path / f"{name}.upa"
    src.write_text(DEMO_SPEC, encoding="utf-8")
    artifacts = tmp_path / f"{name}-build"
    assert main(["build", str(src), "--out", str(artifacts)]) == 0
    return artifacts


def test_viewer_builds_plain_language_html(tmp_path):
    artifacts = _build_demo(tmp_path)
    d = viewer.build_data(viewer.find_artifacts(artifacts))
    assert d["verdict"] == "VERIFIED"
    assert d["sim"] and d["sim"]["operations"]
    assert d["sim"]["composes"]
    out = viewer.build_viewer([artifacts], tmp_path / "viewer.html")
    html = out.read_text(encoding="utf-8")
    assert 'lang="en"' in html and "UPA Viewer" in html
    assert 'const SYSTEMS=' in html
    assert "</script><script" not in html
    assert "Otherwise, if" in html
    assert "Otherwise" in html
    assert "function simGroups" in html
    assert "Other process inputs" in html
    assert '["narrative","Narrative (in plain words)"]' in html
    assert 'const MAP_TAB=["map","System map"]' in html
    assert 'const SIM_TAB=["simulate","Simulate"]' in html
    assert 'const LEVEL_TAB=["levels","Levels and components"]' in html
    assert '"component-flow"' in html and '"simulate"' in html
    rendered = _render_initial_dom(out, tmp_path, "#narrative")
    assert "create an Order" in rendered


def test_viewer_reads_english_reverse_evidence_fields(tmp_path):
    artifacts = _build_demo(tmp_path)
    (artifacts / "reverse-evidence.json").write_text(json.dumps({
        "evidence": [{"id": "EV-TEST-1", "type": "source", "finding": "A source finding", "ref": "src/order.py:2"}],
        "rules": [{"rule": "Orders have a unique key", "counterexample": "Duplicate key",
                   "evidence": ["EV-TEST-1"], "status": "confirmed"}],
    }), encoding="utf-8")

    data = viewer.build_data(viewer.find_artifacts(artifacts))

    assert data["evidence"][0]["finding"] == "A source finding"
    assert data["evidence_rules"][0]["counterexample"] == "Duplicate key"
    assert data["evidence_rules"][0]["status"] == "confirmed"


def test_humanize_splits_camel_case():
    assert viewer.humanize("SalesChannelOriginRequired") == "Sales channel origin required"


def test_english_noun_helpers_handle_plural_compounds_and_articles(tmp_path):
    html = viewer.build_viewer([_build_demo(tmp_path)], tmp_path / "helpers.html").read_text(encoding="utf-8")
    helpers = "\n".join(
        line for line in html.splitlines()
        if line.startswith("function pluralize(") or line.startswith("function article(")
    )
    result = subprocess.run(
        ["node", "-e", helpers + '\nconsole.log(JSON.stringify([pluralize("Policy category"), pluralize("Address"), pluralize("analysis"), article("Order"), article("User account"), article("Hour")]))'],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout) == ["Policy categories", "Addresses", "analyses", "an", "a", "an"]


def test_flow_shows_what_is_stored_from_the_formal_model(tmp_path):
    artifacts = _build_demo(tmp_path)
    ir = viewer._load(viewer.find_artifacts(artifacts), "canonical-ir.json", {})
    ver = viewer._load(viewer.find_artifacts(artifacts), "verification-result.json", {})
    effect = viewer._effects(ir, ver)["placeorder"]
    assert effect["writes"][0]["entity"] == "Order"
    assert effect["writes"][0]["kind"] == "creates"
    assert {field["name"] for field in effect["writes"][0]["fields"]} == {"Quantity"}

def test_every_build_emits_the_standard_viewer(tmp_path, capsys):
    from upa.__main__ import main
    src = tmp_path / "orders.upa"
    src.write_text(DEMO_SPEC, encoding="utf-8")
    assert main(["build", str(src), "--out", str(tmp_path)]) == 0
    assert (tmp_path / "viewer.html").exists()
    (tmp_path / "viewer.html").unlink()
    assert main(["build", str(src), "--out", str(tmp_path), "--no-viewer"]) == 0
    assert not (tmp_path / "viewer.html").exists()


def test_repeat_story_wraps_the_complete_leaf_and_group_in_a_yellow_box(tmp_path):
    out = viewer.build_viewer([_build_demo(tmp_path)], tmp_path / "v.html")
    html = out.read_text(encoding="utf-8")

    # Both rendering branches put the class on the containing <li>, keeping
    # the operation or group and all nested steps inside the same visual box.
    assert '<li class="relstep${lk&&lk!=outerLoop?` repeat-block`:""}">' in html
    assert '<li class="relgrp${lp&&lp!=outerLoop?` repeat-block`:""}">' in html
    assert (
        ".repeat-block{border:2px solid #f59e0b;border-radius:8px;"
        "padding:8px 10px;background:#fff}"
    ) in html


def test_viewer_renders_independent_async_endpoints(tmp_path):
    import json
    d = tmp_path / "artifacts"
    d.mkdir()
    (d / "canonical-ir.json").write_text(json.dumps({
        "ir_version": "upa-ir-0.1", "spec": "Demo", "version": "0.1",
        "operations": [], "composes": [], "queries": [], "properties": [],
        "entities": {}, "types": {}, "enums": {}, "facts": [], "initial": None,
        "notes": {},
        "async_producers": [{
            "id": "Publication", "operation": "Publish", "transport": "topic",
            "channel": "orders", "message": "OrderCreated",
            "confirmation": "confirmed", "evidence": "producer:1",
        }],
        "async_consumers": [{
            "id": "Consumption", "operation": "Consume", "transport": "topic",
            "channel": "orders", "subscription": "worker", "message": "OrderCreated",
            "durability": "durable", "processing": "deferred",
            "delivery": "at_least_once", "completion": "ack_on_return",
            "failure": "reject_on_exception", "evidence": "consumer:1",
        }],
        "async_correlations": [{
            "id": "Publication__Consumption", "producer": "Publication", "consumer": "Consumption",
            "transport": "topic", "channel": "orders", "message": "OrderCreated",
        }],
        "system_map": {"components": [], "contracts": [], "flows": [],
                       "observations": [], "catalogs": [], "jobs": [], "rules": []},
    }), encoding="utf-8")
    (d / "verification-result.json").write_text(json.dumps({
        "verdict": "INCOMPLETE", "summary": {}, "obligations": [], "rules": [],
    }), encoding="utf-8")
    out = viewer.build_viewer([d], tmp_path / "async.html")
    html = out.read_text(encoding="utf-8")
    assert '"async_producers": [{"id": "Publication"' in html
    assert "Asynchronous publications and subscriptions" in html
    assert "ack_on_return" in html and "reject_on_exception" in html
    assert "OrderCreated" in html
    assert 'class="async-band" value="${num}"' in html
    assert 'class="async-band consumer-only" value="${num}"' in html
    assert "asyncLabel(s,a,role)" in html


def test_integral_viewer_embeds_external_process_hierarchy(tmp_path):
    import json

    roots = []
    for key, name, tier in (
        ("macro", "CompleteSystem", "macroprocess"),
        ("P0", "DirectoryUpdate", "process"),
    ):
        d = tmp_path / key
        d.mkdir()
        (d / "canonical-ir.json").write_text(json.dumps({
            "ir_version": "upa-ir-0.1", "spec": name, "version": "1.0",
            "tier": tier, "operations": [], "composes": [], "queries": [],
            "properties": [], "entities": {}, "types": {}, "enums": {},
            "facts": [], "initial": None, "notes": {},
        }), encoding="utf-8")
        (d / "verification-result.json").write_text(json.dumps({
            "verdict": "VERIFIED", "summary": {}, "obligations": [], "rules": [],
        }), encoding="utf-8")
        roots.append(d)

    out = viewer.build_viewer(roots, tmp_path / "integral.html")
    html = out.read_text(encoding="utf-8")

    assert '"viewer_key": "P0"' in html
    assert '"external_systems": [{"key": "P0", "index": 1, "name": "DirectoryUpdate"}]' in html
    assert 'location.hash.slice(1)||((SYSTEMS[0].external_systems||[]).length?"narrative"' in html
    assert "(s.external_systems||[]).length)?[LEVEL_TAB]" in html
    assert "Open full process" in html
    assert "System · process map" in html
    assert "Lines are part of the map" in html
    assert "drillExplorer" in html
    assert "compositionLevel" in html
    assert "Narrative for this level" in html
    assert "drill-shell" in html
    assert "drill-stage" in html
    assert "cytoscape" in html
    assert "cy-graph" in html
    assert "data-graph-edge" in html
    assert "mountGraphs" in html
    assert "previousPreview" in html
    assert "Preview; click to return" in html
    assert "height:500px" in html
    assert 'wheelSensitivity:1.2' in html
    assert r'.join("\n")' in html
    assert r'.join("\\n")' not in html
    assert "process-graph-links" not in html
    assert "process-relations" not in html
    assert "Return to the process map" in html
    assert 'class="process-node ${' in html
    assert "REL_SEL" in html
    assert "function openSystem(i)" in html
    assert 'tab=SYSTEMS[i].sim?"narrative":"summary"' in html
    assert 'data-drill-system="${x.index}"' in html
    assert 'data-drill-path="${nextPath(c.alias)}"' in html
    assert 'onclick="openSystem(' not in html
    assert "openDrillDetail('levels')" in html
    assert "openDrillDetail('formal')" in html
    assert "DETAIL_PATH=DRILL.path.slice()" in html
    assert "Return to this map location" in html
    assert "Verifiable AI what-if" in html
    assert "activeRuleContext" in html
    assert "whatIfCandidates" in html
    assert "e.obligations" in html
    assert "Do not guess or fill in missing rules." in html
    assert "Run the UPA compiler and verifier" in html
    assert "vscode://GitHub.Copilot-Chat/chat?prompt=" in html


def test_viewer_explains_formal_meaning_of_boolean_conditions(tmp_path):
    from upa.__main__ import main

    src = tmp_path / "validation.upa"
    src.write_text(
        '''spec Validation version "1.0" tier atom
operation Validate(dateIsValid: Bool) {
  when not dateIsValid outcome INVALID_DATE { }
  when true outcome ACCEPTED success { }
}
terms {
  dateIsValid "valid date"
    "True exactly when a date has been provided."
}
''',
        encoding="utf-8",
    )
    artifacts = tmp_path / "build"
    assert main(["build", str(src), "--out", str(artifacts)]) == 0
    out = viewer.build_viewer([artifacts], tmp_path / "formal-meaning.html")
    rendered = _render_initial_dom(out, tmp_path, "#narrative")

    assert 'class="formal-help"' in rendered
    assert 'role="tooltip"' in rendered
    assert "What this means formally: valid date" in rendered
    assert "True exactly when a date has been provided." in rendered
    assert "Definitions:" not in rendered
    assert "If the date is not valid:" in rendered


class _StartTagTree(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.starts = []
        self.serial = 0

    def handle_starttag(self, tag, attrs):
        self.serial += 1
        node = (self.serial, tag, dict(attrs), tuple(x[0] for x in self.stack))
        self.starts.append(node)
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input",
                       "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][1] == tag:
                del self.stack[i:]
                return


def _render_initial_dom(html_path: Path, tmp_path: Path, hash_value: str = "") -> str:
    html = html_path.read_text(encoding="utf-8")
    script = re.findall(r"<script>(.*?)</script>", html, re.S)[-1]
    js = tmp_path / "render-viewer.js"
    js.write_text(
        """
const elements={sys:{style:{},add(){},innerHTML:""},hdr:{style:{}},nav:{style:{}},main:{style:{}}};
global.location={hash:HASH_VALUE}; global.window={scrollTo(){}};
global.Option=function(label,value){this.label=label;this.value=value};
global.document={
  querySelector(sel){return elements[sel.slice(1)]},
  querySelectorAll(){return []}
};
""".replace("HASH_VALUE", json.dumps(hash_value)) + script + "\nprocess.stdout.write(JSON.stringify(elements.main.innerHTML));\n",
        encoding="utf-8",
    )
    result = subprocess.run(["node", "--check", str(js)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    result = subprocess.run(["node", str(js)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_async_fanout_band_is_generic_for_a_compiled_demo(tmp_path):
    from upa.__main__ import main

    src = tmp_path / "demo.upa"
    src.write_text(
        '''spec DemoCommerce version "0.1" tier process
operation Billing(id: Identifier) {
  when true outcome INVOICE_ISSUED success { }
}
operation Inventory(id: Identifier) {
  when true outcome STOCK_RESERVED success { }
}
operation Audit(id: Identifier) {
  when true outcome AUDITED success { }
}
compose CommerceFlow(id: Identifier) = Billing(id); Inventory(id); Audit(id)
publishes Invoices from Billing via stream "commerce.invoices"
  message "InvoiceIssued" confirmation confirmed evidence "demo-producer:1"
subscribes Reservations at Inventory via stream "commerce.invoices"
  message "InvoiceIssued" subscription "reservations"
  durability durable processing event_driven delivery at_least_once
  completion explicit_ack failure dead_letter evidence "demo:1"
subscribes Auditor at Audit via stream "commerce.invoices"
  message "InvoiceIssued" subscription "audit"
  durability durable processing deferred delivery at_least_once
  completion ack_on_return failure reject_on_exception evidence "demo:2"
''',
        encoding="utf-8",
    )
    artifacts = tmp_path / "build"
    assert main(["build", str(src), "--out", str(artifacts)]) == 0
    out = viewer.build_viewer([artifacts], tmp_path / "demo.html")
    rendered = _render_initial_dom(out, tmp_path)
    tree = _StartTagTree()
    tree.feed(rendered)

    wanted = [(serial, attrs, parent) for serial, tag, attrs, parent in tree.starts
              if tag == "li" and (attrs.get("data-flow-endpoint") in {
                  "Billing", "Inventory", "Audit"} or attrs.get("data-async-id") == "Invoices")]
    assert len(wanted) == 4
    producer, band, consumer, auditor = sorted(wanted)
    assert producer[1]["data-flow-endpoint"] == "Billing"
    assert band[1]["data-async-from"] == producer[1]["data-flow-endpoint"]
    assert consumer[1]["data-flow-endpoint"] == "Inventory"
    assert auditor[1]["data-flow-endpoint"] == "Audit"
    assert producer[2] == band[2] == consumer[2] == auditor[2]

    anchors = [attrs for _, tag, attrs, _ in tree.starts
               if tag == "span" and attrs.get("data-async-anchor") in {"Invoices", "Reservations", "Auditor"}]
    assert [a["data-async-role"] for a in anchors].count("producer") == 1
    assert [a["data-async-role"] for a in anchors].count("consumer") == 2
    assert "publishes “InvoiceIssued”" in rendered
    assert "event_driven · durable · at-least-once" in rendered
    assert "subscription reservations" in rendered
    assert "success explicit_ack · failure dead_letter" in rendered
    assert "subscription audit" in rendered
    assert "commerce.invoices" in rendered



def test_viewer_renders_producer_only_and_consumer_only_without_invention(tmp_path):
    from upa.__main__ import main

    src = tmp_path / "ends.upa"
    src.write_text(
        '''spec Endpoints version "0.1" tier process
operation Publish(id: Identifier) { when true outcome OK success { } }
operation Receive(id: Identifier) { when true outcome OK success { } }
compose Demo(id: Identifier) = Publish(id); Receive(id)
publishes ProducerOnly from Publish via topic "outputs" message "Output"
  confirmation confirmed evidence "producer:1"
subscribes ConsumerOnly at Receive via queue "inputs" message "Input"
  subscription "worker" durability durable processing deferred delivery at_least_once
  completion ack_on_return failure reject_on_exception evidence "consumer:1"
''', encoding="utf-8")
    artifacts = tmp_path / "ends-build"
    assert main(["build", str(src), "--out", str(artifacts)]) == 0
    out = viewer.build_viewer([artifacts], tmp_path / "ends.html")
    rendered = _render_initial_dom(out, tmp_path, "#map")
    assert "No matching consumer is declared." in rendered
    assert "No producer is inferred." in rendered
    assert 'data-async-id="ProducerOnly"' in rendered
    assert 'data-async-consumer-only="ConsumerOnly"' in rendered
