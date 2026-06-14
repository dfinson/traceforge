"""Generate typed Literal unions from classify/schema.yaml.

Usage:
    python scripts/generate_types.py

Reads src/tracemill/classify/schema.yaml and emits src/tracemill/_generated.py
with Literal type aliases for every dimension enum. CI runs this and checks
for drift via `git diff --exit-code`.
"""

from __future__ import annotations

import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "src" / "tracemill" / "classify" / "schema.yaml"
OUTPUT_PATH = ROOT / "src" / "tracemill" / "_generated.py"


def main() -> int:
    if not SCHEMA_PATH.exists():
        print(f"ERROR: schema not found at {SCHEMA_PATH}", file=sys.stderr)
        return 1

    with SCHEMA_PATH.open() as f:
        spec = yaml.safe_load(f)

    schemas = spec["components"]["schemas"]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    lines: list[str] = [
        '"""Auto-generated from classify/schema.yaml — DO NOT EDIT.',
        "",
        f"Generated at: {now}",
        "Re-generate: python scripts/generate_types.py",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "from typing import Literal",
        "",
    ]

    for name, schema in schemas.items():
        enum_values = schema.get("enum", [])
        if not enum_values:
            continue

        # Build Literal union
        quoted = ", ".join(f'"{v}"' for v in sorted(enum_values))
        type_line = f"{name} = Literal[{quoted}]"

        # Wrap long lines
        if len(type_line) > 100:
            items = ",\n    ".join(f'"{v}"' for v in sorted(enum_values))
            type_line = f"{name} = Literal[\n    {items},\n]"

        desc = schema.get("description", "")
        if desc:
            lines.append(f"# {desc}")
        lines.append(type_line)

        # Emit frozenset of valid values for runtime validation
        values_name = f"{name}_VALUES"
        values_items = ", ".join(f'"{v}"' for v in sorted(enum_values))
        values_line = f"{values_name}: frozenset[str] = frozenset({{{values_items}}})"
        if len(values_line) > 100:
            vitems = ",\n    ".join(f'"{v}"' for v in sorted(enum_values))
            values_line = f"{values_name}: frozenset[str] = frozenset({{\n    {vitems},\n}})"
        lines.append(values_line)
        lines.append("")

    # Add __all__
    all_names = [name for name in schemas if schemas[name].get("enum")]
    all_str = ", ".join(f'"{n}"' for n in all_names)
    values_str = ", ".join(f'"{n}_VALUES"' for n in all_names)
    lines.append(f"__all__ = [{all_str}, {values_str}]")
    lines.append("")

    OUTPUT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"Generated {OUTPUT_PATH} ({len(all_names)} types)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
