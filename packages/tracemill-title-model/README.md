# tracemill-title-model

Pretrained **int8 ONNX titler weights** for [tracemill](https://github.com/dfinson/tracemill).

This is a *pure data* package. It carries the two titler heads and nothing else:

| head | dir | model | size |
|------|-----|-------|------|
| span (activity/step titles) | `data/span/` | seq-KD flan-t5-small | ~96 MB |
| request (session names) | `data/request/` | rationale-distilled t5-tiny | ~35 MB |

## Why a separate package?

The core `tracemill` wheel is code-only and small. The ~130 MB of model weights are
pulled in **only** when you install the titler extra:

```bash
pip install "tracemill[title]"      # pulls tracemill-title-model from PyPI
uv add "tracemill[title]"
```

You almost never depend on this package directly — `tracemill.title` resolves the
weights for you. If you must:

```python
import tracemill_title_model as m
m.span_dir()      # -> .../data/span
m.request_dir()   # -> .../data/request
```

## Distribution

- **Primary:** PyPI (`tracemill-title-model`), resolved automatically by `tracemill[title]`.
- **Mirror / fallback:** the same wheel is published as a GitHub release asset under the
  `title-model-v*` tags. `tracemill download-model --source gh` installs it from there when
  PyPI is unavailable.

## License

MIT, same as tracemill.
