# tracemill-title-model

Pretrained **int8 ONNX titler weights** for [tracemill](https://github.com/dfinson/tracemill).

This is a *pure data* package. It carries the activity/step (span) titler head and
nothing else:

| head | dir | model | size |
|------|-----|-------|------|
| span (activity/step titles) | `data/span/` | seq-KD flan-t5-small | ~96 MB |

Session naming (naming a session from its first user message) is **not** a model
head here — the distilled request head was proven weak at it and was dropped;
tracemill names sessions with a zero-cost heuristic (plus an opt-in LLM API tier).

## Why a separate package?

The core `tracemill` wheel is code-only and small. The model weights live here and
are pulled in automatically as a hard dependency of `tracemill`:

```bash
pip install tracemill      # pulls tracemill-title-model from PyPI
uv add tracemill
```

You almost never depend on this package directly — `tracemill.title` resolves the
weights for you. If you must:

```python
import tracemill_title_model as m
m.span_dir()      # -> .../data/span
```

## Distribution

- **Primary:** PyPI (`tracemill-title-model`), resolved automatically by `tracemill`.
- **Mirror / fallback:** the same wheel is published as a GitHub release asset under the
  `title-model-v*` tags. `tracemill download-model --source gh` installs it from there when
  PyPI is unavailable.

## License

MIT, same as tracemill.
