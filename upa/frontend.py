"""Deterministic frontend capability and UI IR generation from the logical IR."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .ir import canonical_json

FORMAT = "upa-ui-ir-1"
INPUT_ROLES = {"textual-input", "numeric-input", "boolean-input", "selection", "collection-input"}


class FrontendError(ValueError):
    """Invalid profile or an interaction that cannot be resolved."""


def _identifier(value: object, context: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise FrontendError(f"{context} must be a valid UPA identifier")
    return value


def _label(name: str) -> str:
    words = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name).replace("_", " ").split()
    return " ".join(word.capitalize() for word in words)


def _type_role(type_name: str) -> str:
    if type_name == "bool":
        return "boolean-input"
    if type_name in ("nat", "int"):
        return "numeric-input"
    if type_name.startswith("enum:"):
        return "selection"
    return "textual-input"


def infer_frontend(ir: dict, profile: dict) -> dict:
    if not isinstance(ir, dict) or not isinstance(ir.get("entities"), dict):
        raise FrontendError("Logical IR must be an object with entities")
    if not isinstance(profile, dict):
        raise FrontendError("profile must be a JSON object")
    if profile.get("format") != FORMAT:
        raise FrontendError(f"profile.format must be {FORMAT}")
    profile_name = profile.get("name")
    if not isinstance(profile_name, str) or not profile_name.strip():
        raise FrontendError("profile.name is required")
    profile_title = profile.get("title") or "UPA"
    if not isinstance(profile_title, str) or not profile_title.strip():
        raise FrontendError("profile.title must be a non-empty string")
    version = profile.get("version")
    if not isinstance(version, str) or not version.strip():
        raise FrontendError("profile.version is required")

    ir_operations = ir.get("operations", [])
    if not isinstance(ir_operations, list):
        raise FrontendError("Logical IR operations must be a list")
    operations = {}
    for operation in ir_operations:
        if not isinstance(operation, dict):
            raise FrontendError("each Logical IR operation must be an object")
        operation_name = _identifier(operation.get("name"), "operation.name")
        if operation_name in operations:
            raise FrontendError(f"duplicate operation in Logical IR: {operation_name}")
        if not isinstance(operation.get("params"), list) or not isinstance(operation.get("cases"), list):
            raise FrontendError(f"{operation_name}: Logical IR requires params and cases to be lists")
        parameter_names = []
        for parameter in operation["params"]:
            if not isinstance(parameter, dict) or not isinstance(parameter.get("type"), str):
                raise FrontendError(f"{operation_name}: invalid parameter in Logical IR")
            parameter_names.append(_identifier(parameter.get("name"), f"{operation_name}.params[].name"))
        if len(parameter_names) != len(set(parameter_names)):
            raise FrontendError(f"{operation_name}: duplicate parameters in Logical IR")
        for case in operation["cases"]:
            if not isinstance(case, dict) or not isinstance(case.get("stmts"), list):
                raise FrontendError(f"{operation_name}: invalid case in Logical IR")
        operations[operation_name] = operation
    configured = profile.get("operations", [])
    if not isinstance(configured, list):
        raise FrontendError("profile.operations must be a list")

    interactions = []
    known_interactions: set[str] = set()
    page_by_id = {}
    pages = profile.get("pages", [])
    if not isinstance(pages, list):
        raise FrontendError("profile.pages must be a list")
    for page in pages:
        if not isinstance(page, dict):
            raise FrontendError("each profile page must be an object")
        page_id = page.get("id")
        if type(page_id) is not int or page_id <= 0 or page_id in page_by_id:
            raise FrontendError("each page needs a unique positive integer id")
        page_by_id[page_id] = page
        if not isinstance(page.get("title"), str) or not page["title"].strip():
            raise FrontendError(f"page {page_id} requires a title")

    for binding in configured:
        if not isinstance(binding, dict):
            raise FrontendError("each operation binding must be an object")
        name = _identifier(binding.get("operation"), "operations[].operation")
        if name in known_interactions:
            raise FrontendError(f"duplicate exposed operation: {name}")
        operation = operations.get(name)
        if operation is None:
            raise FrontendError(f"exposed operation '{name}' does not exist in Logical IR")
        known_interactions.add(name)
        page_id = binding.get("page_id")
        if page_id not in page_by_id:
            raise FrontendError(f"{name}: page_id {page_id!r} is not declared")
        action_id = binding.get("action_component_id")
        if type(action_id) is not int or action_id <= 0:
            raise FrontendError(f"{name}: action_component_id must be a positive integer")
        if binding.get("action_kind") != "Button":
            raise FrontendError(f"{name}: action_kind must be Button for the React DOM provider")

        params = {p["name"]: p for p in operation["params"]}
        fields = binding.get("fields", [])
        if not isinstance(fields, list):
            raise FrontendError(f"{name}: fields must be a list")
        seen_field_ids: set[int] = set()
        field_bindings = []
        for field in fields:
            if not isinstance(field, dict):
                raise FrontendError(f"{name}: each field must be an object")
            field_id = field.get("component_id")
            if type(field_id) is not int or field_id <= 0 or field_id in seen_field_ids:
                raise FrontendError(f"{name}: field component_id must be unique and positive")
            if field.get("component_kind") not in ("Input", "Combo"):
                raise FrontendError(f"{name}: component_kind must be Input or Combo")
            seen_field_ids.add(field_id)
            field_params = field.get("parameters")
            if not isinstance(field_params, list) or not field_params:
                raise FrontendError(f"{name}: each field must bind at least one parameter")
            for parameter in field_params:
                _identifier(parameter, f"{name}.fields.parameters")
                if parameter not in params:
                    raise FrontendError(f"{name}: bound parameter '{parameter}' does not exist")
            type_names = sorted({params[p]["type"] for p in field_params})
            collections_in_field = [params[p]["collection"] for p in field_params if params[p].get("collection")]
            if collections_in_field and len(field_params) != 1:
                raise FrontendError(f"{name}: a collection must be bound to its own field")
            role = field.get("role") or ("collection-input" if collections_in_field else _type_role(type_names[0]))
            if role not in INPUT_ROLES:
                raise FrontendError(f"{name}: unsupported input role: {role}")
            if (role == "collection-input") != bool(collections_in_field):
                raise FrontendError(f"{name}: role {role} does not match a "
                                    f"{'collection' if collections_in_field else 'scalar'}")
            input_type = field.get("input_type", "text")
            if input_type not in ("text", "number"):
                raise FrontendError(f"{name}: input_type must be text or number")
            if input_type == "number" and role != "numeric-input":
                raise FrontendError(f"{name}: input_type=number requires the numeric-input role")
            required = field.get("required", False)
            if not isinstance(required, bool):
                raise FrontendError(f"{name}: required must be a boolean")
            minimum = field.get("min")
            if minimum is not None and (
                isinstance(minimum, bool) or not isinstance(minimum, (int, float)) or minimum < 0
            ):
                raise FrontendError(f"{name}: min must be a non-negative number")
            transforms = field.get("parameter_transforms", {})
            if not isinstance(transforms, dict) or set(transforms) - set(field_params):
                raise FrontendError(f"{name}: parameter_transforms must only reference parameters in this field")
            if len(type_names) > 1 and set(transforms) != set(field_params):
                raise FrontendError(
                    f"{name}: different types require an explicit transform for each parameter"
                )
            field_bindings.append({
                "id": f"{name}.{field_id}",
                "component_id": field_id,
                "component_kind": field["component_kind"],
                "label": field.get("label") or _label(field_params[0]),
                "role": role,
                "parameters": field_params,
                "types": type_names,
                "parameter_transforms": transforms,
                "input_type": input_type,
                "required": required,
                "min": minimum,
                **({"collection": collections_in_field[0]} if collections_in_field else {}),
                "trace": {
                    "operation": name,
                    "parameters": field_params,
                    "inference_rule": "UPA_PARAMETER_TO_INPUT",
                    "profile_binding": True,
                },
            })
        mapped = {p for field in field_bindings for p in field["parameters"]}
        resolved = binding.get("resolved_parameters", {})
        if not isinstance(resolved, dict):
            raise FrontendError(f"{name}: resolved_parameters must be an object")
        resolved_bindings = {}
        for parameter, binding_reason in resolved.items():
            if parameter not in params:
                raise FrontendError(f"{name}: resolved parameter '{parameter}' does not exist")
            if isinstance(binding_reason, str) and binding_reason.strip():
                resolved_bindings[parameter] = {"kind": "non_interactive", "reason": binding_reason}
            elif isinstance(binding_reason, dict):
                reason = binding_reason.get("reason")
                if not isinstance(reason, str) or not reason.strip():
                    raise FrontendError(f"{name}: each non-visible parameter requires a reason")
                kind = binding_reason.get("kind")
                if kind not in ("server_generated", "server_lookup", "field_transform"):
                    raise FrontendError(f"{name}: invalid resolution kind for {parameter}")
                if kind == "field_transform":
                    component_id = binding_reason.get("component_id")
                    transform = binding_reason.get("transform")
                    if component_id not in seen_field_ids or not isinstance(transform, str) or not transform:
                        raise FrontendError(
                            f"{name}: field_transform for {parameter} must point to an existing field and specify a transform"
                        )
                resolved_bindings[parameter] = {
                    "kind": kind,
                    "reason": reason,
                    **({"component_id": binding_reason["component_id"],
                        "transform": binding_reason["transform"]}
                       if kind == "field_transform" else {}),
                }
            else:
                raise FrontendError(f"{name}: each non-visible parameter requires an explicit resolution")
            mapped.add(parameter)
        for pname, p in params.items():
            if p.get("count_of") and pname not in mapped:
                resolved_bindings[pname] = {
                    "kind": "collection_count",
                    "reason": f"count of collection {p['count_of']}, calculated by the UI",
                    "of": p["count_of"],
                }
                mapped.add(pname)
        missing = sorted(set(params) - mapped)
        if missing:
            raise FrontendError(f"{name}: parameters without an input or explicit resolution: {missing}")

        if not any(binding_case["stmts"] for binding_case in operation["cases"]):
            raise FrontendError(f"{name}: has no case that changes state")
        feedback = binding.get("feedback")
        if not isinstance(feedback, dict) or feedback.get("source") != "existing_application_api":
            raise FrontendError(
                f"{name}: feedback must declare source=existing_application_api for the React DOM provider"
            )
        if not isinstance(feedback.get("trace"), str) or not feedback["trace"]:
            raise FrontendError(f"{name}: feedback.trace is required")
        interaction = {
            "id": name,
            "title": binding.get("title") or _label(name),
            "page_id": page_id,
            "action_component_id": action_id,
            "fields": field_bindings,
            "resolved_parameters": resolved_bindings,
            "outcomes": [
                {"name": case["outcome"], "case_index": index, "trace": f"{name}.cases[{index}]"}
                for index, case in enumerate(operation["cases"]) if case.get("outcome")
            ],
            "preconditions": [
                {"case_index": index, "expression": case["cond"], "trace": f"{name}.cases[{index}].cond"}
                for index, case in enumerate(operation["cases"]) if case.get("cond")
            ],
            "feedback": {"source": feedback["source"], "trace": feedback["trace"]},
            "trace": {"operation": name, "inference_rule": "EXPOSED_STATE_TRANSITION"},
        }
        interactions.append(interaction)

    interactions.sort(key=lambda item: (item["page_id"], item["id"]))

    capabilities = []
    if interactions:
        capabilities = [
            {"name": "human-interaction", "evidence": [f"PROFILE:{i['id']}" for i in interactions]},
            {"name": "data-input", "evidence": sorted({f"OP:{i['id']}" for i in interactions if i["fields"]})},
            {"name": "command-execution", "evidence": [f"OP:{i['id']}" for i in interactions]},
            {"name": "validation-feedback", "evidence": sorted({
                f"API:{i['id']}.feedback" for i in interactions
            })},
        ]

    collections = profile.get("collections", [])
    if not isinstance(collections, list):
        raise FrontendError("profile.collections must be a list")
    collection_names_seen = set()
    for collection in collections:
        if not isinstance(collection, dict):
            raise FrontendError("each profile collection must be an object")
        collection_name = collection.get("name")
        if not isinstance(collection_name, str) or not collection_name:
            raise FrontendError("each collection requires a name")
        if collection_name in collection_names_seen:
            raise FrontendError(f"duplicate collection: {collection_name}")
        collection_names_seen.add(collection_name)
        collection_page_id = collection.get("page_id")
        if type(collection_page_id) is not int or collection_page_id not in page_by_id:
            raise FrontendError(f"collection {collection.get('name')!r}: page_id is not declared")
        if not isinstance(collection.get("source"), str) or not collection["source"]:
            raise FrontendError(f"collection {collection['name']!r}: API source is required")
        entity = collection.get("entity")
        if not isinstance(entity, str) or entity not in ir["entities"]:
            raise FrontendError(f"collection {collection.get('name')!r}: entity '{entity}' is not in Logical IR")
        fields = collection.get("fields", [])
        if not isinstance(fields, list) or not fields:
            raise FrontendError(f"collection {collection.get('name')!r}: fields must be a non-empty list")
        for field in fields:
            if not isinstance(field, dict):
                raise FrontendError(f"collection {collection['name']!r}: each field must be an object")
            attribute = field.get("attribute")
            if not isinstance(attribute, str):
                raise FrontendError(f"collection {collection['name']!r}: attribute must be a string")
            if attribute != "$key" and attribute not in {
                a["name"] for a in ir["entities"][entity]["attrs"]
            }:
                raise FrontendError(f"collection {collection.get('name')!r}: attribute {field.get('attribute')!r} does not exist in {entity}")
            if not isinstance(field.get("label"), str) or not field["label"].strip():
                raise FrontendError(f"collection {collection.get('name')!r}: each field requires a label")
            if not isinstance(field.get("api_field"), str) or not field["api_field"]:
                raise FrontendError(f"collection {collection.get('name')!r}: each field requires an api_field")

    collection_names = {collection["name"] for collection in collections}
    for page in pages:
        collection = page.get("collection")
        if collection is not None and (
            not isinstance(collection, str) or collection not in collection_names
        ):
            raise FrontendError(
                f"page {page['id']}: collection {collection!r} is not declared"
            )

    if collections:
        capabilities.append({
            "name": "result-presentation",
            "evidence": sorted(f"API:{collection['name']}" for collection in collections),
        })

    return {
        "format": FORMAT,
        "profile": {"name": profile_name, "title": profile_title, "version": version},
        "source": {"spec": ir.get("spec"), "version": ir.get("version"), "ir_version": ir.get("ir_version")},
        "frontend_required": bool(interactions),
        "capabilities": capabilities,
        "interactions": interactions,
        "pages": sorted(
            [{"id": p["id"], "title": p["title"], "collection": p.get("collection"),
              "interactions": [item["id"] for item in interactions if item["page_id"] == p["id"]]}
             for p in pages],
            key=lambda p: p["id"],
        ),
        "collections": sorted(collections, key=lambda c: (c["page_id"], c["name"])),
        "provenance": {
            "logical_ir_sha256": hashlib.sha256(canonical_json(ir).encode("utf-8")).hexdigest(),
            "profile_sha256": hashlib.sha256(canonical_json(profile).encode("utf-8")).hexdigest(),
        },
    }


def generate(ir_path: Path, profile_path: Path, output: Path) -> dict:
    ir = json.loads(ir_path.read_text(encoding="utf-8"))
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    ui_ir = infer_frontend(ir, profile)
    output.mkdir(parents=True, exist_ok=True)
    if not ui_ir["frontend_required"]:
        return {"frontend_required": False, "output": str(output)}

    (output / "interaction-model.json").write_text(
        json.dumps({"format": FORMAT, "interactions": ui_ir["interactions"]},
                   ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (output / "ui-ir.json").write_text(
        json.dumps(ui_ir, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"frontend_required": True, "pages": len(ui_ir["pages"]),
            "interactions": len(ui_ir["interactions"]), "output": str(output),
            "artifacts": ["interaction-model.json", "ui-ir.json"]}
