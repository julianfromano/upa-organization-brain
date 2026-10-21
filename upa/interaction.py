"""Deterministic, provider-neutral projection from Logical IR to Interaction IR."""
from __future__ import annotations

import hashlib
from .ir import IR_VERSION, canonical_json

FORMAT = "upa-interaction-ir-1"


class InteractionError(ValueError):
    """Logical IR cannot be projected into the supported Interaction IR format."""


def _walk(value, path: str):
    if isinstance(value, dict):
        yield value, path
        for key in sorted(value):
            yield from _walk(value[key], f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk(item, f"{path}[{index}]")


def _variables(value) -> set[str]:
    return {
        node["n"]
        for node, _ in _walk(value, "$")
        if node.get("k") == "var" and isinstance(node.get("n"), str)
    }


def _attribute_refs(value) -> list[dict]:
    refs = []
    for node, path in _walk(value, "$"):
        if node.get("k") == "attr":
            refs.append({
                "entity": node.get("e"),
                "attribute": node.get("a"),
                "key_expression": node.get("key"),
                "expression_path": path,
            })
    return refs


def _memberships(value, path: str):
    def visit(node, node_path: str, negated: bool):
        if isinstance(node, dict):
            if node.get("k") == "un" and node.get("op") == "not":
                yield from visit(node.get("x"), f"{node_path}.x", not negated)
                return
            if node.get("k") == "in" and isinstance(node.get("e"), str):
                key = node.get("key")
                variable = key.get("n") if isinstance(key, dict) and key.get("k") == "var" else None
                yield {
                    "entity": node["e"],
                    "parameter": variable,
                    "polarity": "negative" if negated else "positive",
                    "trace": node_path,
                    "expression": node,
                }
            for key in sorted(node):
                if key != "pos":
                    yield from visit(node[key], f"{node_path}.{key}", negated)
        elif isinstance(node, list):
            for index, item in enumerate(node):
                yield from visit(item, f"{node_path}[{index}]", negated)

    yield from visit(value, path, False)


def _is_direct_parameter(value, name: str) -> bool:
    return isinstance(value, dict) and value.get("k") == "var" and value.get("n") == name


def _intent(operation: dict) -> tuple[dict, list[dict]]:
    creates = []
    updates = []
    effects = []
    for case_index, case in enumerate(operation["cases"]):
        for statement_index, statement in enumerate(case["stmts"]):
            entity = statement.get("entity")
            is_file = bool(statement.get("file")) or entity == "File"
            path = f"operations.{operation['name']}.cases[{case_index}].stmts[{statement_index}]"
            effects.append({"kind": statement.get("kind"), "entity": entity, "trace": path})
            if is_file:
                continue
            if statement.get("kind") == "create":
                creates.append((entity, path))
            elif statement.get("kind") == "set":
                updates.append((entity, path))
    subjects = sorted({entity for entity, _ in creates if entity})
    update_subjects = sorted({entity for entity, _ in updates if entity})
    if len(subjects) == 1:
        return {
            "kind": "CREATE",
            "subject": subjects[0],
            "trace": [path for entity, path in creates if entity == subjects[0]],
        }, effects
    if len(subjects) > 1:
        return {
            "kind": "UNRESOLVED",
            "subject": None,
            "reason": "The operation creates more than one domain entity.",
            "candidates": subjects,
            "trace": [path for _, path in creates],
        }, effects
    if len(update_subjects) == 1:
        return {
            "kind": "UPDATE",
            "subject": update_subjects[0],
            "trace": [path for entity, path in updates if entity == update_subjects[0]],
        }, effects
    if len(update_subjects) > 1:
        return {
            "kind": "UNRESOLVED",
            "subject": None,
            "reason": "The operation modifies more than one domain entity.",
            "candidates": update_subjects,
            "trace": [path for _, path in updates],
        }, effects
    return {"kind": "EXECUTE", "subject": None, "trace": []}, effects


def _outcome_kind(condition, intent: dict) -> str:
    memberships = list(_memberships(condition, "$"))
    if intent["kind"] == "CREATE" and any(
        item["entity"] == intent["subject"] and item["polarity"] == "positive"
        for item in memberships
    ):
        return "CONFLICT"
    return "VALIDATION"


def _operation_interaction(operation: dict, identities: dict[str, dict]) -> dict:
    name = operation["name"]
    intent, effects = _intent(operation)
    params = operation["params"]
    param_by_name = {parameter["name"]: parameter for parameter in params}
    param_names = set(param_by_name)
    param_memberships: dict[str, list[dict]] = {parameter: [] for parameter in param_names}
    conditions = []
    for case_index, case in enumerate(operation["cases"]):
        condition = case.get("cond")
        if condition is None:
            continue
        trace = f"operations.{name}.cases[{case_index}].cond"
        matches = list(_memberships(condition, trace))
        for membership in matches:
            parameter = membership.get("parameter")
            if parameter in param_memberships:
                param_memberships[parameter].append(membership)
        outcome = case.get("outcome")
        conditions.append({
            "expression": condition,
            "outcome": outcome,
            "kind": _outcome_kind(condition, intent),
            "trace": trace,
        })

    used_in_create: set[str] = set()
    used_in_update: set[str] = set()
    generated_values = []
    derived_values = []
    technical_values = []
    for case_index, case in enumerate(operation["cases"]):
        for statement_index, statement in enumerate(case["stmts"]):
            statement_path = f"operations.{name}.cases[{case_index}].stmts[{statement_index}]"
            entity = statement.get("entity")
            if statement.get("kind") == "create":
                key = statement.get("key")
                used_in_create.update(_variables(key))
                for init_index, init in enumerate(statement.get("inits", [])):
                    value = init.get("value")
                    used_in_create.update(_variables(value))
                    init_path = f"{statement_path}.inits[{init_index}]"
                    is_file_value = bool(statement.get("file")) or entity == "File"
                    if is_file_value:
                        technical_values.append({
                            "name": init["attr"],
                            "entity": entity,
                            "kind": "TECHNICAL",
                            "expression": value,
                            "trace": init_path,
                            "inference_rule": "FILE_CONTENT_MATERIALIZATION",
                        })
                    if not is_file_value and not _is_direct_parameter(
                        value, value.get("n") if isinstance(value, dict) else ""
                    ):
                        attribute_refs = _attribute_refs(value)
                        if attribute_refs and value.get("k") == "attr":
                            derived_values.append({
                                "name": init["attr"],
                                "entity": entity,
                                "kind": "DERIVED",
                                "derives_from": attribute_refs,
                                "expression": value,
                                "trace": init_path,
                                "inference_rule": "STATE_ATTRIBUTE_VALUE_IN_CREATE",
                            })
                        elif attribute_refs:
                            generated_values.append({
                                "name": init["attr"],
                                "entity": entity,
                                "kind": "GENERATED",
                                "derives_from": attribute_refs,
                                "expression": value,
                                "trace": init_path,
                                "inference_rule": "STATE_BASED_VALUE_GENERATION",
                            })
                        elif _variables(value) - param_names:
                            derived_values.append({
                                "name": init["attr"],
                                "entity": entity,
                                "kind": "DERIVED",
                                "derives_from": sorted(_variables(value)),
                                "expression": value,
                                "trace": init_path,
                                "inference_rule": "NON_PARAMETER_VALUE_EXPRESSION",
                            })
                        else:
                            generated_values.append({
                                "name": init["attr"],
                                "entity": entity,
                                "kind": "GENERATED",
                                "expression": value,
                                "trace": init_path,
                                "inference_rule": "CREATE_INITIALIZER_NOT_BOUND_TO_INPUT",
                            })
                    for node, node_path in _walk(value, init_path):
                        if node.get("k") == "to_text":
                            source = node.get("x")
                            if isinstance(source, dict) and source.get("k") == "var":
                                parameter = source.get("n")
                                if parameter in param_by_name:
                                    derived_values.append({
                                        "name": f"{parameter}Text",
                                        "kind": "DERIVED",
                                        "derives_from": [{"parameter": parameter}],
                                        "representation": "text",
                                        "expression": node,
                                        "trace": node_path,
                                        "inference_rule": "EXPLICIT_TO_TEXT_OF_OPERATION_PARAMETER",
                                    })
            elif statement.get("kind") == "set":
                used_in_update.update(_variables(statement.get("key")))
                used_in_update.update(_variables(statement.get("value")))

    parameters = []
    inputs = []
    unresolved = []
    for parameter in params:
        parameter_name = parameter["name"]
        if parameter.get("count_of"):
            parameters.append({
                "name": parameter_name,
                "type": parameter["type"],
                "kind": "DERIVED",
                "required": True,
                "derives_from": [{"parameter": parameter["count_of"]}],
                "representation": "collection_count",
                "trace": [f"operations.{name}.params.{parameter_name}"],
                "inference_rule": "COLLECTION_CARDINALITY_OF_INPUT",
            })
            continue
        matches = param_memberships[parameter_name]
        reference = next((
            item for item in matches
            if not (
                intent["kind"] == "CREATE"
                and item["entity"] == intent["subject"]
                and item["polarity"] == "positive"
            )
        ), None)
        if reference is None:
            for case_index, case in enumerate(operation["cases"]):
                for statement_index, statement in enumerate(case["stmts"]):
                    if statement.get("kind") == "set" and _is_direct_parameter(
                        statement.get("key"), parameter_name
                    ):
                        reference = {
                            "entity": statement.get("entity"),
                            "parameter": parameter_name,
                            "polarity": "target",
                            "trace": f"operations.{name}.cases[{case_index}].stmts[{statement_index}].key",
                        }
                        break
                if reference:
                    break
        if reference:
            identity = identities.get(reference["entity"])
            item = {
                "name": parameter_name,
                "type": parameter["type"],
                "kind": "ENTITY_REFERENCE",
                "required": True,
                "reference": {
                    "entity": reference["entity"],
                    "identity_attribute": identity["attribute"] if identity else None,
                },
                "trace": [
                    f"operations.{name}.params.{parameter_name}",
                    reference["trace"],
                    *([identity["trace"]] if identity else []),
                ],
                "inference_rule": "ENTITY_MEMBERSHIP_OR_UPDATE_TARGET",
            }
            if identity is None:
                item["ambiguity"] = "No explicit identity was found for the referenced entity."
                unresolved.append({
                    "parameter": parameter_name,
                    "reason": item["ambiguity"],
                    "trace": item["trace"],
                })
            else:
                inputs.append(item)
        elif parameter_name in used_in_create or parameter_name in used_in_update:
            item = {
                "name": parameter_name,
                "type": parameter["type"],
                "kind": "USER_INPUT",
                "required": True,
                "trace": [f"operations.{name}.params.{parameter_name}"],
                "inference_rule": "OPERATION_PARAMETER_USED_BY_STATE_CHANGE",
            }
            if parameter.get("collection"):
                collection = parameter["collection"]
                item["shape"] = "COLLECTION_INPUT"
                item["collection"] = {
                    "element_type": collection["element"], "min": collection["min"], "max": collection["max"],
                    "entity": collection["entity"], "attribute": collection["attribute"],
                    "separator": collection["separator"],
                }
                item["inference_rule"] = "OPERATION_COLLECTION_PARAMETER_USED_BY_STATE_CHANGE"
            inputs.append(item)
        else:
            item = {
                "name": parameter_name,
                "type": parameter["type"],
                "kind": "UNRESOLVED",
                "required": True,
                "trace": [f"operations.{name}.params.{parameter_name}"],
            }
            unresolved.append({
                "parameter": parameter_name,
                "reason": "The parameter has no semantic use that supports classification.",
                "trace": item["trace"],
            })
        parameters.append(item)

    outcomes = []
    for case_index, case in enumerate(operation["cases"]):
        if case.get("outcome"):
            outcomes.append({
                "name": case["outcome"],
                "kind": _outcome_kind(case.get("cond"), intent),
                "case_index": case_index,
                "condition": case.get("cond"),
                "trace": f"operations.{name}.cases[{case_index}]",
            })
        elif case.get("cond") is None and case.get("stmts"):
            outcomes.append({
                "name": None,
                "kind": "SUCCESS",
                "case_index": case_index,
                "trace": f"operations.{name}.cases[{case_index}]",
                "inference_rule": "UNCONDITIONAL_STATE_CHANGE_CASE",
            })

    capabilities = []
    if effects:
        capabilities.append({
            "name": "human-interaction",
            "status": "UNRESOLVED",
            "evidence": [effect["trace"] for effect in effects],
            "reason": "The IR does not explicitly identify who initiates the operation.",
        })
    if inputs and effects:
        capabilities.append({
            "name": "data-input",
            "status": "OBSERVED",
            "evidence": [item["trace"][0] for item in inputs],
            "inference_rule": "OPERATION_PARAMETERS_USED_BY_STATE_CHANGE",
        })
    if effects:
        capabilities.append({
            "name": "command-execution",
            "status": "OBSERVED",
            "evidence": [effect["trace"] for effect in effects],
            "inference_rule": "STATE_CHANGE_STATEMENTS",
        })
    if any(outcome["kind"] in {"CONFLICT", "VALIDATION"} for outcome in outcomes):
        capabilities.append({
            "name": "validation-feedback",
            "status": "OBSERVED",
            "evidence": [outcome["trace"] for outcome in outcomes
                         if outcome["kind"] in {"CONFLICT", "VALIDATION"}],
            "inference_rule": "NAMED_OPERATION_OUTCOMES",
        })

    return {
        "id": name,
        "intent": intent,
        "actor": {
            "kind": "UNRESOLVED",
            "reason": "Logical IR does not declare a human or technical initiator.",
        },
        "parameters": parameters,
        "inputs": inputs,
        "system_values": sorted(
            [*derived_values, *generated_values, *technical_values],
            key=lambda value: (value.get("entity", ""), value["name"], value["kind"], value["trace"]),
        ),
        "preconditions": conditions,
        "outcomes": outcomes,
        "effects": effects,
        "capabilities": sorted(capabilities, key=lambda item: item["name"]),
        "ambiguities": sorted(unresolved, key=lambda item: item["parameter"]),
        "trace": {
            "operation": f"operations.{name}",
            "inference_rule": "LOGICAL_OPERATION_PROJECTION",
        },
    }


def project(ir: dict) -> dict:
    if not isinstance(ir, dict) or ir.get("ir_version") != IR_VERSION:
        raise InteractionError(f"Logical IR is required {IR_VERSION}")
    operations = ir.get("operations")
    if not isinstance(operations, list):
        raise InteractionError("Logical IR operations must be a list")

    identities: dict[str, dict] = {}
    operation_names = set()
    for operation in operations:
        if not isinstance(operation, dict) or not isinstance(operation.get("name"), str):
            raise InteractionError("each operation must have a name")
        if operation["name"] in operation_names:
            raise InteractionError(f"duplicate operation in Logical IR: {operation['name']}")
        operation_names.add(operation["name"])
        if not isinstance(operation.get("params"), list) or not isinstance(operation.get("cases"), list):
            raise InteractionError(f"{operation['name']}: params and cases must be lists")
        parameter_names = set()
        for parameter in operation["params"]:
            if (
                not isinstance(parameter, dict)
                or not isinstance(parameter.get("name"), str)
                or not isinstance(parameter.get("type"), str)
            ):
                raise InteractionError(f"{operation['name']}: invalid parameter")
            if parameter["name"] in parameter_names:
                raise InteractionError(f"{operation['name']}: duplicate parameter: {parameter['name']}")
            parameter_names.add(parameter["name"])
        for case in operation["cases"]:
            if not isinstance(case, dict) or not isinstance(case.get("stmts"), list):
                raise InteractionError(f"{operation['name']}: invalid case")
            if any(not isinstance(statement, dict) for statement in case["stmts"]):
                raise InteractionError(f"{operation['name']}: invalid statement")
        for case_index, case in enumerate(operation.get("cases", [])):
            for statement_index, statement in enumerate(case.get("stmts", [])):
                entity = statement.get("entity")
                if statement.get("kind") != "create" or statement.get("file") or entity == "File":
                    continue
                key = statement.get("key")
                if isinstance(key, dict) and key.get("k") == "var":
                    identity = {"attribute": key["n"], "trace": (
                        f"operations.{operation['name']}.cases[{case_index}].stmts[{statement_index}].key"
                    )}
                    if entity in identities and identities[entity]["attribute"] != identity["attribute"]:
                        identities[entity] = {"attribute": None, "trace": identity["trace"]}
                    elif entity not in identities:
                        identities[entity] = identity

    projected = [_operation_interaction(operation, identities) for operation in operations]
    projected.sort(key=lambda item: item["id"])
    capabilities_by_name: dict[str, dict] = {}
    for operation in projected:
        for capability in operation["capabilities"]:
            aggregate = capabilities_by_name.setdefault(capability["name"], {
                "status": "OBSERVED",
                "evidence": [],
                "reasons": [],
            })
            aggregate["evidence"].extend(capability["evidence"])
            if capability["status"] == "UNRESOLVED":
                aggregate["status"] = "UNRESOLVED"
                aggregate["reasons"].append(capability["reason"])
    capabilities = [
        {
            "name": name,
            "status": aggregate["status"],
            "evidence": sorted(set(aggregate["evidence"])),
            **({"reasons": sorted(set(aggregate["reasons"]))} if aggregate["reasons"] else {}),
        }
        for name, aggregate in sorted(capabilities_by_name.items())
    ]
    return {
        "format": FORMAT,
        "source": {
            "spec": ir.get("spec"),
            "logical_ir_version": ir["ir_version"],
            "sha256": hashlib.sha256(canonical_json(ir).encode("utf-8")).hexdigest(),
        },
        "operations": projected,
        **({"language_gaps": [dict(item) for item in ir["notes"]["language_gaps"]]}
           if (ir.get("notes") or {}).get("language_gaps") else {}),
        **({"regeneration_coverage": dict(ir["regeneration_coverage"])}
           if ir.get("regeneration_coverage") else {}),
        **({"async_producers": [dict(item) for item in ir["async_producers"]]}
           if ir.get("async_producers") else {}),
        **({"async_consumers": [dict(item) for item in ir["async_consumers"]]}
           if ir.get("async_consumers") else {}),
        **({"async_correlations": [dict(item) for item in ir["async_correlations"]]}
           if ir.get("async_correlations") else {}),
        "capabilities": capabilities,
        "inference_rules": [
            "ENTITY_MEMBERSHIP_OR_UPDATE_TARGET",
            "EXPLICIT_TO_TEXT_OF_OPERATION_PARAMETER",
            "LOGICAL_OPERATION_PROJECTION",
            "NAMED_OPERATION_OUTCOMES",
            "OPERATION_PARAMETER_USED_BY_STATE_CHANGE",
            "OPERATION_PARAMETERS_USED_BY_STATE_CHANGE",
            "STATE_ATTRIBUTE_VALUE_IN_CREATE",
            "UNCONDITIONAL_STATE_CHANGE_CASE",
        ],
    }
