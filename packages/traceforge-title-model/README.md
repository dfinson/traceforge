# traceforge-title-model

Pretrained **int8 ONNX titler weights** for [traceforge](https://github.com/dfinson/traceforge).

This is a *pure data* package. It carries the activity/step (span) titler head and
nothing else:

| head | dir | model | size |
|------|-----|-------|------|
| span (activity/step titles) | `data/span/` | seq-KD flan-t5-small | ~96 MB |

Session naming (naming a session from its first user message) is **not** a model
head here — the distilled request head was proven weak at it and was dropped;
traceforge names sessions with a zero-cost heuristic (plus an opt-in LLM API tier).

## Why a separate package?

The core `traceforge` wheel is code-only and small. The model weights live here and
are pulled in automatically as a hard dependency of `traceforge`:

```bash
pip install traceforge-toolkit  # pulls traceforge-title-model from PyPI
uv add traceforge-toolkit
```

You almost never depend on this package directly — `traceforge.title` resolves the
weights for you. If you must:

```python
import traceforge_title_model as m
m.span_dir()      # -> .../data/span
```

## Distribution

- **Primary:** PyPI (`traceforge-title-model`), resolved automatically by `traceforge`.
- **Mirror / fallback:** the same wheel is published as a GitHub release asset under the
  `title-model-v*` tags. `traceforge download-model --source gh` installs it from there when
  PyPI is unavailable.

## License

MIT, same as traceforge.
