#!/usr/bin/env bash
# Regenerate _generated.py from the OpenAPI schema.
# CI checks: uv run scripts/generate_types.sh && git diff --exit-code src/traceforge/_generated.py
set -euo pipefail

uv run datamodel-codegen \
    --input src/traceforge/classify/schema.yaml \
    --input-file-type openapi \
    --output-model-type dataclasses.dataclass \
    --target-python-version 3.12 \
    --output src/traceforge/_generated.py

echo "Generated src/traceforge/_generated.py"
