"""Cloud-neutral representation (CNR): a normalized, provider-agnostic model of the
resources discovered in the source template, independent of Bicep/ARM's specific
JSON shape (apiVersion quirks, nested `resources[]`, expression syntax, etc).

This is the artifact that gets reasoned over by the LLM and by every downstream
generator (CloudFormation today, Terraform/GCP Deployment Manager tomorrow), so
adding a new source (e.g. Terraform HCL) or a new target only requires plugging
into this shape instead of re-deriving everything from raw ARM JSON again.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field


@dataclass
class CNRParameter:
    name: str
    type: str
    default: object | None = None
    secure: bool = False


@dataclass
class CNRResource:
    logical_id: str
    azure_type: str
    api_version: str | None
    name_expression: object  # raw ARM "name" value, may be a literal or expression
    properties: dict
    depends_on: list[str] = field(default_factory=list)
    parent_logical_id: str | None = None


@dataclass
class CloudNeutralRepresentation:
    parameters: list[CNRParameter]
    resources: list[CNRResource]
    outputs: dict


def build_cnr(arm_template: dict) -> CloudNeutralRepresentation:
    """Convert a compiled ARM template into the cloud-neutral representation."""
    parameters = [
        CNRParameter(
            name=name,
            type=definition.get("type", "string"),
            default=definition.get("defaultValue"),
            secure="secure" in str(definition.get("type", "")).lower(),
        )
        for name, definition in (arm_template.get("parameters") or {}).items()
    ]

    resources: list[CNRResource] = []
    # (CNRResource, raw ARM dict) pairs with a still-unknown parent, used by
    # _infer_parent_from_depends_on below -- only populated for resources that
    # came through as flat top-level entries (parent_logical_id is None),
    # never for genuinely nested `"resources": [...]` ARM, which is already
    # correctly linked by the recursive walk itself.
    unlinked: list[tuple[CNRResource, dict]] = []

    def _walk(arm_resources: list[dict], parent_logical_id: str | None) -> None:
        for resource in arm_resources:
            logical_id = _derive_logical_id(resource, parent_logical_id)
            cnr_resource = CNRResource(
                logical_id=logical_id,
                azure_type=resource.get("type", ""),
                api_version=resource.get("apiVersion"),
                name_expression=resource.get("name"),
                properties=resource.get("properties", {}) or {},
                depends_on=list(resource.get("dependsOn", []) or []),
                parent_logical_id=parent_logical_id,
            )
            resources.append(cnr_resource)
            if parent_logical_id is None and "/" in cnr_resource.azure_type:
                unlinked.append((cnr_resource, resource))
            _walk(resource.get("resources", []) or [], logical_id)

    _walk(arm_template.get("resources", []) or [], None)
    _infer_parents_from_depends_on(resources, unlinked)

    return CloudNeutralRepresentation(
        parameters=parameters,
        resources=resources,
        outputs=arm_template.get("outputs", {}) or {},
    )


def write_cnr(cnr: CloudNeutralRepresentation, output_dir: str, filename: str = "cnr.json") -> str:
    """Serialize the cloud-neutral representation to a JSON file in the output folder."""
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, filename)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(asdict(cnr), f, indent=2, default=str)
    return output_path


def _derive_logical_id(resource: dict, parent_logical_id: str | None) -> str:
    """Best-effort stable logical id derived from the resource's declared name."""
    raw_name = str(resource.get("name", "")) or resource.get("type", "Resource")
    slug = "".join(ch for ch in raw_name if ch.isalnum()) or "Resource"
    slug = slug[0].upper() + slug[1:]
    return f"{parent_logical_id}{slug}" if parent_logical_id else slug


_RESOURCE_ID_RE = re.compile(r"^resourceId\('([^']+)',\s*(.+)\)$")
_FORMAT_NAME_RE = re.compile(r"^format\('([^']+)',\s*(.+)\)$")


def _bracket_strip(expression: object) -> str:
    """ARM wraps expressions in `[...]`; literal strings have no brackets.
    Strips them so a wrapped and unwrapped copy of the same expression compare equal."""
    text = str(expression)
    if text.startswith("[") and text.endswith("]"):
        return text[1:-1]
    return text


def _split_top_level_csv(text: str) -> list[str]:
    """Split a comma-separated ARM argument list while preserving commas nested
    inside quotes or balanced parentheses (e.g. function calls)."""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    in_single_quote = False
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "'":
            in_single_quote = not in_single_quote
            buf.append(ch)
            i += 1
            continue
        if not in_single_quote:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth = max(0, depth - 1)
            elif ch == "," and depth == 0:
                part = "".join(buf).strip()
                if part:
                    parts.append(part)
                buf = []
                i += 1
                continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def _name_segments(expression: object) -> tuple[str, ...]:
    """Best-effort normalized resource-name segments.

    Examples:
    - "[parameters('vnetName')]" -> ("parameters('vnetName')",)
    - "[format('{0}/{1}', parameters('sa'), 'default')]" ->
      ("parameters('sa')", "'default'")
    """
    expr = _bracket_strip(expression).strip()

    format_match = _FORMAT_NAME_RE.match(expr)
    if format_match:
        fmt, args_blob = format_match.groups()
        args = _split_top_level_csv(args_blob)
        format_parts = fmt.split("/")
        segments: list[str] = []
        for part in format_parts:
            placeholder = re.fullmatch(r"\{(\d+)\}", part)
            if not placeholder:
                segments = []
                break
            idx = int(placeholder.group(1))
            if idx >= len(args):
                segments = []
                break
            segments.append(args[idx].strip())
        if segments:
            return tuple(segments)

    if expr.startswith("'") and expr.endswith("'"):
        literal = expr[1:-1]
        if "/" in literal:
            return tuple(segment for segment in literal.split("/") if segment)
        return (expr,)

    if "/" in expr:
        return tuple(segment.strip() for segment in expr.split("/") if segment.strip())
    return (expr,)


def _infer_parents_from_depends_on(
    resources: list[CNRResource], unlinked: list[tuple[CNRResource, dict]]
) -> None:
    """Fills in `parent_logical_id` for child-type resources that compiled to a flat
    top-level entry instead of a genuinely nested ARM `"resources": [...]` block --
    this is exactly what `az bicep build` produces for Bicep's `parent: foo` syntax
    (e.g. `resources/storage/main.bicep`'s blobServices/containers), as opposed to
    the nested shape `az group export` emits for the same resources on a live
    export. Without this, FOLDABLE_CHILD_TYPES/IMPLICIT_NOOP_TYPES child-shape
    checks would never find their parent for any hand-authored `parent:` Bicep.

    Matches each child's own `dependsOn: ["[resourceId('<ParentType>', <args>)]"]`
    entry (emitted by the compiler for every `parent:` relationship) against the
    candidate parent's own `(type, bracket-stripped name expression)` -- both sides
    come from the same compiler, so for a direct one-level parent/child pair the
    `resourceId(...)` args and the parent's own name expression are textually
    identical. Deeper (2+ level) parent chains aren't resolved by this best-effort
    pass; they fall back to the pre-existing behavior (parent_logical_id stays None).
    """
    if not unlinked:
        return
    by_type_and_name = {
        (r.azure_type, _bracket_strip(r.name_expression).strip()): r.logical_id for r in resources
    }
    by_type_and_segments = {
        (r.azure_type, _name_segments(r.name_expression)): r.logical_id for r in resources
    }
    for cnr_resource, arm_resource in unlinked:
        expected_parent_type = cnr_resource.azure_type.rsplit("/", 1)[0]
        for dep in arm_resource.get("dependsOn", []) or []:
            match = _RESOURCE_ID_RE.match(_bracket_strip(dep))
            if not match or match.group(1) != expected_parent_type:
                continue
            args_text = match.group(2).strip()
            parent_logical_id = by_type_and_name.get((expected_parent_type, args_text))
            if not parent_logical_id:
                parent_logical_id = by_type_and_segments.get(
                    (expected_parent_type, tuple(_split_top_level_csv(args_text)))
                )
            if parent_logical_id:
                cnr_resource.parent_logical_id = parent_logical_id
                break
