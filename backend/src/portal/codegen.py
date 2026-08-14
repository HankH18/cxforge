"""Generate ``portal/src/api-types.ts`` from the FastAPI OpenAPI schema.

T-19: ``portal/src/api.ts`` and ``backend/src/portal/schemas.py`` used to
agree only by hand, with nothing to fail if they drifted. This script IS
the fix — it introspects the live ``app.openapi()`` output (FastAPI/pydantic
already does 100% of the schema work; no new dependency, on either side of
the repo, is needed) and emits TypeScript ``interface``/``type``
declarations mechanically. If ``schemas.py`` ever grows a shape this mapper
doesn't understand, :meth:`_TypeMapper.map` raises ``NotImplementedError``
rather than silently emitting a loose ``any``/``unknown`` — a crash here is
visible in CI; a silently-wrong generated type would defeat the point of
the ticket.

Schema discovery walks every ``/api/``-prefixed path's request body and
*successful* (``2xx``) response bodies, following ``$ref`` transitively —
no hand-maintained whitelist, so a future endpoint's new response schema is
picked up automatically. ``4xx``/``422`` responses are deliberately
excluded: FastAPI attaches ``HTTPValidationError``/``ValidationError`` to
every endpoint in the whole app (including ``/health`` and
``/webhooks/zendesk``, which aren't portal paths at all) as generic
request-validation infrastructure, not part of the portal's own data
contract — ``portal/src/api.ts`` has never had TS types for them, and
pulling them in would make "byte-identical to what is committed"
impossible without also committing types nothing in the portal ever uses.

``DraftStatus``/``RunOutcome`` are ``typing.Literal`` aliases in
``schemas.py`` and get no OpenAPI *component* schema of their own — they
appear only as anonymous inline ``enum`` arrays wherever a field uses them.
The wire format has no way to recover the Python-level name, so
``_NAMED_ENUMS`` below is a small, explicit, hand-maintained value-tuple ->
name map that supplies it back. This is not the "re-deriving the
duplication by hand" the ticket forbids: that's about field *shapes*
silently drifting, and this map only supplies a cosmetic label for a value
set. If it ever goes stale, the failure mode is a still-type-correct
anonymous union appearing where a named one used to — caught immediately by
the byte-identical parity check as a diff, never a silent hazard.

Determinism (load-bearing for the byte-identical acceptance criterion):
every ordering decision below is either ``sorted(...)`` or an existing
``dict``'s natural insertion order (which mirrors pydantic's own
field-declaration order for a schema's ``properties``) — never a bare
``set()``, whose iteration order is not stable run-to-run because Python
randomizes string-hash seeds per process by default.

CLI:
    uv run python backend/src/portal/codegen.py --out portal/src/api-types.ts
    uv run python backend/src/portal/codegen.py --out portal/src/api-types.ts --check

``--out`` is always required (no implicit default, to avoid fragile
``__file__``-relative path math when this script runs from a copied tree —
see ``backend/tests/portal/test_api_contract.py``). ``--check`` never
writes; it computes the generated text in memory, diffs it against the
existing file, prints a unified diff to stderr, and exits 1 on any
difference.
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path
from typing import Any

# Self-contained path bootstrapping: makes `from main import app` work
# whether this script is invoked from the repo root, from CI, or from a
# temp-copied tree (backend/tests/portal/test_api_contract.py's rename
# test) — Path(__file__).resolve() naturally resolves to wherever the
# invoked copy of *this* script physically sits, so the copy imports the
# copied backend/src tree, never the real one.
_BACKEND_SRC = Path(__file__).resolve().parents[1]  # .../backend/src
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

from main import app  # noqa: E402 -- after sys.path bootstrap above

# Value-tuple -> Python-level name, for OpenAPI's anonymous inline enums.
# See the module docstring for why this one map is hand-maintained and why
# that's safe.
_NAMED_ENUMS: dict[tuple[str, ...], str] = {
    ("pending", "approved", "rejected", "auto_sent"): "DraftStatus",
    ("auto_sent", "gated_sent", "rejected", "escalated", "off_topic"): "RunOutcome",
}

_HEADER = """// GENERATED FILE — DO NOT EDIT BY HAND.
// Source of truth: backend/src/portal/schemas.py, via FastAPI's OpenAPI
// schema (`app.openapi()`).
// Regenerate:
//   uv run python backend/src/portal/codegen.py --out portal/src/api-types.ts
// Verify (no write): same command with --check
"""


def _refs_in(node: object) -> list[str]:
    """Every ``#/components/schemas/X`` ref reachable under ``node``."""
    found: list[str] = []
    if isinstance(node, dict):
        ref = node.get("$ref", "")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            found.append(ref.rsplit("/", 1)[-1])
        for value in node.values():
            found.extend(_refs_in(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_refs_in(value))
    return found


def _collect_portal_schema_names(openapi: dict[str, Any]) -> list[str]:
    """Every component schema reachable from a successful (2xx) response or
    a request body of an ``/api/``-prefixed path, transitively. See the
    module docstring for why 422/validation-error schemas are excluded.
    """
    paths = openapi["paths"]
    schemas = openapi["components"]["schemas"]
    seen: dict[str, None] = {}
    queue: list[str] = []

    for path_name in sorted(paths):
        if not path_name.startswith("/api/"):
            continue
        operations = paths[path_name]
        for method in sorted(operations):
            op = operations[method]
            queue += _refs_in(op.get("requestBody", {}))
            for status, response in op.get("responses", {}).items():
                if not status.startswith("2"):
                    continue
                queue += _refs_in(response)

    while queue:
        name = queue.pop(0)
        if name not in seen:
            seen[name] = None
            queue += _refs_in(schemas[name])

    return sorted(seen)


class _TypeMapper:
    """Maps one OpenAPI/JSON-Schema node to a TS type string, recording
    which named enum aliases (``_NAMED_ENUMS``) were actually used along
    the way so the caller knows which ``export type`` aliases to emit.
    """

    def __init__(self) -> None:
        self.used_enums: dict[str, None] = {}

    def map(self, node: dict[str, Any]) -> str:
        if "$ref" in node:
            return str(node["$ref"]).rsplit("/", 1)[-1]

        if "anyOf" in node:
            return " | ".join(self.map(sub) for sub in node["anyOf"])

        node_type = node.get("type")

        if node_type == "null":
            return "null"
        if node_type == "boolean":
            return "boolean"
        if node_type in ("integer", "number"):
            return "number"
        if node_type == "string":
            if "enum" in node:
                return self._enum(node["enum"])
            return "string"
        if node_type == "array":
            item_t = self.map(node["items"])
            return f"({item_t})[]" if " | " in item_t else f"{item_t}[]"
        if node_type == "object":
            additional = node.get("additionalProperties")
            if isinstance(additional, dict):
                return f"Record<string, {self.map(additional)}>"

        raise NotImplementedError(
            f"portal codegen: no TS mapping for OpenAPI schema node {node!r} -- "
            "extend backend/src/portal/codegen.py's _TypeMapper rather than "
            "silently emitting `any`/`unknown`."
        )

    def _enum(self, values: list[str]) -> str:
        key = tuple(values)
        name = _NAMED_ENUMS.get(key)
        if name is not None:
            self.used_enums[name] = None
            return name
        return " | ".join(f"'{v}'" for v in values)


def _emit_interface(name: str, schema: dict[str, Any], mapper: _TypeMapper) -> str:
    required = set(schema.get("required", []))
    properties: dict[str, Any] = schema.get("properties", {})
    lines = [f"export interface {name} {{"]
    for field_name, field_schema in properties.items():
        ts_type = mapper.map(field_schema)
        optional = "" if field_name in required else "?"
        lines.append(f"  {field_name}{optional}: {ts_type}")
    lines.append("}")
    return "\n".join(lines)


def _values_for(enum_name: str) -> tuple[str, ...]:
    for values, name in _NAMED_ENUMS.items():
        if name == enum_name:
            return values
    raise KeyError(enum_name)  # pragma: no cover -- used_enums only ever holds known names


def generate(openapi: dict[str, Any]) -> str:
    """The full generated TS file, as a single string, from a live
    ``app.openapi()`` dict.
    """
    schema_names = _collect_portal_schema_names(openapi)
    schemas = openapi["components"]["schemas"]

    mapper = _TypeMapper()
    # Interfaces first so every field's type has been mapped (and every
    # used named enum recorded) before we decide which enum aliases to
    # emit.
    interfaces = [_emit_interface(name, schemas[name], mapper) for name in schema_names]

    enum_lines = [
        f"export type {enum_name} = " + " | ".join(f"'{v}'" for v in _values_for(enum_name))
        for enum_name in sorted(mapper.used_enums)
    ]

    blocks = enum_lines + interfaces
    return _HEADER + "\n" + "\n\n".join(blocks) + "\n"


def _run(out: str, check: bool) -> int:
    generated = generate(app.openapi())
    out_path = Path(out)

    if not check:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(generated, encoding="utf-8", newline="\n")
        print(f"portal codegen: wrote {out_path}")
        return 0

    try:
        existing = out_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        existing = ""

    if existing == generated:
        return 0

    diff = difflib.unified_diff(
        existing.splitlines(keepends=True),
        generated.splitlines(keepends=True),
        fromfile=str(out_path),
        tofile="<freshly generated>",
    )
    sys.stderr.writelines(diff)
    print(
        f"\nportal codegen: {out_path} is stale -- regenerate with "
        f"`uv run python backend/src/portal/codegen.py --out {out_path}` (no --check).",
        file=sys.stderr,
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", required=True, help="path to write (or, with --check, compare against)"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit 1 if --out's content differs from a fresh generation",
    )
    args = parser.parse_args(argv)
    return _run(args.out, args.check)


if __name__ == "__main__":
    raise SystemExit(main())
