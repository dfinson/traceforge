# Releasing traceforge

This repo publishes **two** independent distributions to PyPI. This document is
the operational runbook and go/no-go checklist for cutting a release.

> **Status:** packaging is **ready** and both former code blockers are resolved —
> the F2 governance durability fix (#58) and the docs refresh (#78, plus red-team
> passes #113/#114) are merged to `main`. Cutting the first release now depends only
> on the one-time PyPI Trusted Publisher setup ([§5](#5-pypi-trusted-publishing-oidc))
> and pushing the release tag ([§7](#7-gono-go-checklist)).

---

## 1. The two distributions

| Distribution | Source | Contents | Publish workflow | Tag |
|---|---|---|---|---|
| `traceforge-toolkit` | root `pyproject.toml` | code + small data (classify/mappings/gates YAML, phase/boundary/title heads, `potion-base-8M` `.safetensors`) | `.github/workflows/publish.yml` | `v*` |
| `traceforge-title-model` | `packages/traceforge-title-model` | pure-data ONNX span titler (`encoder.onnx`, `decoder.onnx`, `tokenizer.json`) | `.github/workflows/publish-title-model.yml` | `title-model-v*` |

> **Note:** the core distribution is published to PyPI as **`traceforge-toolkit`** because
> the bare name `traceforge` was already taken by an unrelated project. The import package
> (`import traceforge`), the `traceforge` CLI command, and the brand stay `traceforge` — the
> `scikit-learn` → `import sklearn` pattern.

`traceforge-toolkit` depends on `traceforge-title-model>=0.2`. The titler weights are a
separate distribution so the core wheel stays code-first and the ~95 MB model
only re-releases when it is retrained (rarely), on its own tag.

Both weight classes (`.safetensors` in `traceforge-toolkit`, `.onnx` in
`traceforge-title-model`) are tracked with **Git LFS** — see [§4](#4-git-lfs--weight-integrity).

## 2. Versioning strategy

- **Scheme:** [SemVer](https://semver.org/). Both distributions are pre-1.0
  (`0.MINOR.PATCH`); while `0.x`, a **MINOR** bump may carry breaking changes and
  a **PATCH** bump is reserved for backwards-compatible fixes. Cut `1.0.0` once the
  public API (SDK `Pipeline`/`GatePolicy`/`Verdict`, the `traceforge` CLI surface,
  and the YAML config schema) is committed to stability.
- **Initial versions:** `traceforge-toolkit` `0.1.0`; `traceforge-title-model` `0.2.0`
  (already ahead — it iterated during model development).
- **Independent lifecycles:** the two versions move independently. A code-only
  release bumps `traceforge-toolkit` and reuses the existing model. A retrain bumps
  `traceforge-title-model` and, if the new weights require it, widens the
  `traceforge-toolkit` dependency floor.
- **Single source of truth:** each version lives only in its own
  `pyproject.toml`. The CLI reports it via `importlib.metadata`
  (`click.version_option(package_name="traceforge-toolkit")`) — never hard-code a version
  string elsewhere.
- **Model dependency pin:** `traceforge-toolkit` requires `traceforge-title-model>=0.2`.
  The model is a pure-data package with a stable three-file head contract
  (`encoder.onnx`/`decoder.onnx`/`tokenizer.json`), so a compatible retrain is
  drop-in. If a future retrain changes that on-disk contract, raise the floor
  (e.g. `>=0.3`) in the same PR that ships the new model.

## 3. Release order (critical)

Because `traceforge-toolkit` depends on `traceforge-title-model`, a fresh model release
must land on PyPI **before** the code release that requires it:

1. (Only if the model changed) tag `title-model-vX.Y.Z` → wait for
   `Publish title model` to finish and the version to be visible on PyPI.
2. Tag `vA.B.C` → `Publish` builds and uploads `traceforge-toolkit`.

If the model is unchanged, step 1 is skipped and `traceforge-toolkit` resolves the
already-published model.

## 4. Git LFS & weight integrity

The model binaries are Git LFS objects. If a publish job checks out the repo
**without** LFS smudging, the working tree holds ~133-byte pointer files instead
of real weights, and the build would happily bake those pointers into a wheel
that imports but cannot load its model.

Two guards prevent that:

- **`lfs: true`** on every `actions/checkout` in both publish workflows (and in CI).
- **`scripts/verify_no_lfs_pointers.py`** runs after `python -m build` in both
  workflows. It opens every built artifact, asserts each `*.onnx` / `*.safetensors`
  member is a real binary (not an LFS pointer, not implausibly small), and
  `--require`s that the expected weight suffix is present at all (catching a
  "weights dropped out entirely" regression):
  - `publish.yml` → `--require .safetensors`
  - `publish-title-model.yml` → `--require .onnx`

A pointer stub or a missing-weights build therefore **fails loudly before publish**.

## 5. PyPI Trusted Publishing (OIDC)

Both workflows authenticate with **PyPI Trusted Publishing** — no API tokens or
secrets. Each declares `permissions: id-token: write` and `environment: pypi`,
and uses `pypa/gh-action-pypi-publish`.

One-time PyPI setup (per distribution, done in the PyPI project's
*Publishing* settings — or as a pending publisher before the first upload):

| Field | Value |
|---|---|
| Owner | `dfinson` |
| Repository | `traceforge` |
| Workflow (traceforge-toolkit) | `publish.yml` |
| Workflow (title-model) | `publish-title-model.yml` |
| Environment | `pypi` |

Also create a GitHub Environment named `pypi` on the repo (optionally with
required reviewers) so releases are gated.

## 6. Local pre-flight (dry run — never uploads)

Run from the repo root. This mirrors exactly what CI builds and verifies, minus
the upload step:

```bash
# Build both distributions
uv build --sdist --wheel --out-dir dist
uv build packages/traceforge-title-model --out-dir dist

# Integrity gate — must exit 0
uv run --no-project python scripts/verify_no_lfs_pointers.py dist \
  --require .onnx --require .safetensors

# Optional: confirm metadata renders
uv run --no-project --with twine twine check dist/*
```

Expected: the traceforge-toolkit wheel carries the classify/mappings/gates YAML, the
phase/boundary/title heads, `py.typed`, and the `potion-base-8M` `.safetensors`;
the title-model wheel carries the ONNX triad; the traceforge-toolkit **sdist** stays lean
(~29 MB — it deliberately excludes `packages/`, `tests/`, and `research/`, so it
never bundles the titler weights and never approaches PyPI's 100 MiB per-file cap).

## 7. Go/No-Go checklist

**Blockers (must be TRUE before any tag is pushed):**

- [x] **F2 — governance durability fix merged.** Resolved by **#58** (commit
      `3710a3e`): `SessionRegistry` now holds two separate residency maps —
      `_durable_states` (DB-backed observation writer) and `_gate_states`
      (ephemeral `_db=None` gate) — so the gate-state path can no longer no-op
      `persist_no_commit` and lose durability.
- [x] **Docs-refresh PR merged.** Resolved by **#78** (Docusaurus site + README +
      CONTRIBUTING) and the red-team doc passes **#113**/**#114**; README/SPEC now
      reflect delivery.

**Packaging readiness (verified by this PR):**

- [x] `traceforge-toolkit` metadata complete: name, version, description, readme, license
      (SPDX `MIT` → `License-Expression`), authors, requires-python
      (`>=3.11,<3.14`), classifiers (Dev Status 4-Beta, Python 3.11/3.12/3.13,
      Typing :: Typed), keywords, `project.urls`.
- [x] `traceforge-title-model` metadata complete (authors, classifiers, urls).
- [x] Dependencies verified **torch-free / CPU-only** — no `torch`, `nvidia-*`,
      `cuda`, or `onnxruntime-gpu` in the resolved graph.
- [x] `traceforge` CLI entry point (`traceforge = "traceforge.cli:main"`) resolves.
- [x] Package **data ships in the wheel** (all classify/mappings/gates YAML,
      schema, phase/boundary/title heads, `potion-base-8M` weights).
- [x] `py.typed` shipped (PEP 561).
- [x] LFS weights are real bytes; `verify_no_lfs_pointers` wired into both
      workflows and passing.
- [x] Core sdist trimmed (no redundant weights; well under the 100 MiB cap) and
      proven to build a complete wheel from itself.
- [x] Trusted-publishing workflows in place for both distributions with
      `lfs: true` checkouts.

**One-time publishing setup (still pending — do once before the first tag):**

- [x] Create the `pypi` **GitHub Environment** on the repo (optionally with required
      reviewers) — see [§5](#5-pypi-trusted-publishing-oidc). (done — environment exists on the repo)
- [ ] Register **PyPI Trusted Publishers** for **both** dists: `traceforge-toolkit`
      (`publish.yml`) and `traceforge-title-model` (`publish-title-model.yml`).

**Release steps (once blockers clear):**

1. [ ] Final `main` is green (Test on 3.11/3.12/3.13, Lint).
2. [ ] Bump versions if needed; update this checklist.
3. [ ] (If model changed) push `title-model-vX.Y.Z`; confirm on PyPI + GH release mirror.
4. [ ] Push `vA.B.C`; confirm `Publish` succeeds and `pip install traceforge-toolkit` pulls
       the model automatically.
5. [ ] Smoke test in a clean env: `pip install traceforge-toolkit && traceforge --version`.

## 8. Fallback: GitHub-release model mirror

`publish-title-model.yml` also uploads the model wheel as a GitHub Release asset
under the `title-model-v*` tag, as a **manual disaster-recovery copy**. There is no
customer-facing command for it: the weights are a hard dependency and normally
arrive straight from PyPI. If PyPI is ever unavailable, install the wheel directly
from the release asset URL:

```bash
pip install https://github.com/dfinson/traceforge/releases/download/title-model-vX.Y.Z/traceforge_title_model-X.Y.Z-py3-none-any.whl
```

Alternatively, point `$TRACEFORGE_TITLE_MODEL` at a directory containing an unpacked
`encoder.onnx` / `decoder.onnx` / `tokenizer.json` triad.

The release asset is the same wheel published to PyPI, so both channels stay in
lockstep.
