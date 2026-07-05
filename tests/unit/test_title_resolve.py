"""Resolver guards for the packaged span head.

The weights are Git LFS-tracked, so a package built from a checkout that never
smudged LFS would contain ~133-byte pointer stubs instead of real binaries.
Existence alone must NOT count as installed: these tests pin that the resolver
rejects LFS pointers and implausibly small ONNX so the caller surfaces the
install hint instead of failing later inside onnxruntime.
"""

from __future__ import annotations

from tracemill.title import _resolve


def _write_triad(d, *, encoder=b"\x00" * 2_000_000, decoder=b"\x00" * 2_000_000):
    d.mkdir(parents=True, exist_ok=True)
    (d / "encoder.onnx").write_bytes(encoder)
    (d / "decoder.onnx").write_bytes(decoder)
    (d / "tokenizer.json").write_text('{"ok": true}')
    return d


def test_complete_true_for_real_binaries(tmp_path):
    d = _write_triad(tmp_path / "span")
    assert _resolve._complete(d) is True


def test_complete_false_for_lfs_pointer(tmp_path):
    pointer = b"version https://git-lfs.github.com/spec/v1\noid sha256:x\nsize 58627575\n"
    d = _write_triad(tmp_path / "span", encoder=pointer)
    assert _resolve._complete(d) is False


def test_complete_false_for_implausibly_small_onnx(tmp_path):
    d = _write_triad(tmp_path / "span", decoder=b"\x00" * 500)
    assert _resolve._complete(d) is False


def test_complete_false_when_file_missing(tmp_path):
    d = _write_triad(tmp_path / "span")
    (d / "tokenizer.json").unlink()
    assert _resolve._complete(d) is False


def test_complete_false_for_none():
    assert _resolve._complete(None) is False
