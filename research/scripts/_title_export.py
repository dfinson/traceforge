"""Export a fine-tuned T5 titler to the torch-free, int8 ONNX prod artifact.

Self-contained promote path: torch -> no-past ONNX (encoder + decoder) -> int8
dynamic quantize, written as ``encoder.onnx`` / ``decoder.onnx`` + ``tokenizer.json``
into the served data dir (``src/traceforge/title/data`` by default). Uses only
``torch.onnx`` + ``onnxruntime.quantization`` so it is robust to optimum/transformers
version drift, and reproduces the exact IO contract the runtime expects:

    encoder.onnx : input_ids[b,s], attention_mask[b,s]            -> last_hidden_state[b,s,H]
    decoder.onnx : input_ids[b,ds], encoder_hidden_states[b,s,H],
                   encoder_attention_mask[b,s]                    -> logits[b,ds,V]

No-past (no kv-cache) by design: titles are short and generated only at span
boundaries, so the stateless decoder re-feeds the growing sequence (see
``src/traceforge/title/inference.py``). int8 weight-only dynamic quant (QInt8,
per-tensor) matches the AVX2 dynamic scheme of the prior shipped artifact.

Usage:
    python -m scripts._title_export <model_dir> [out_dir]
    # or via env:
    TITLE_MODEL_DIR=... TITLE_OUT_DIR=... python -m scripts._title_export
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
_OPSET = 18  # match the shipped artifact's opset (onnx graph contract)


def _wrappers(model):
    import torch
    from transformers.modeling_outputs import BaseModelOutput

    class Enc(torch.nn.Module):
        def __init__(self, m):
            super().__init__()
            # get_encoder() resolves the encoder submodule for any seq2seq arch
            # (T5: model.encoder, BART: model.model.encoder).
            self.enc = m.get_encoder()

        def forward(self, input_ids, attention_mask):
            return self.enc(input_ids=input_ids, attention_mask=attention_mask).last_hidden_state

    class Dec(torch.nn.Module):
        def __init__(self, m):
            super().__init__()
            self.m = m

        def forward(self, input_ids, encoder_hidden_states, encoder_attention_mask):
            return self.m(
                attention_mask=encoder_attention_mask,
                decoder_input_ids=input_ids,
                encoder_outputs=BaseModelOutput(last_hidden_state=encoder_hidden_states),
            ).logits

    return Enc(model).eval(), Dec(model).eval()


def export(model_dir: str | os.PathLike[str], out_dir: str | os.PathLike[str]) -> None:
    import torch
    from onnxruntime.quantization import QuantType, quantize_dynamic
    from transformers import AutoModelForSeq2SeqLM

    model_dir = Path(model_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    model = AutoModelForSeq2SeqLM.from_pretrained(str(model_dir)).eval()
    hidden = model.config.d_model
    enc, dec = _wrappers(model)

    # Tiny dummy inputs; dynamic axes generalize over batch / sequence length.
    ids = torch.ones(1, 4, dtype=torch.long)
    mask = torch.ones(1, 4, dtype=torch.long)
    dec_ids = torch.zeros(1, 1, dtype=torch.long)
    enc_h = torch.zeros(1, 4, hidden, dtype=torch.float32)

    seq = {0: "batch_size", 1: "encoder_sequence_length"}
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        enc_fp32 = tmp / "encoder_model.onnx"
        dec_fp32 = tmp / "decoder_model.onnx"
        with torch.no_grad():
            torch.onnx.export(
                enc,
                (ids, mask),
                str(enc_fp32),
                input_names=["input_ids", "attention_mask"],
                output_names=["last_hidden_state"],
                dynamic_axes={
                    "input_ids": seq,
                    "attention_mask": seq,
                    "last_hidden_state": seq,
                },
                opset_version=_OPSET,
                dynamo=False,
            )
            torch.onnx.export(
                dec,
                (dec_ids, enc_h, mask),
                str(dec_fp32),
                input_names=[
                    "input_ids",
                    "encoder_hidden_states",
                    "encoder_attention_mask",
                ],
                output_names=["logits"],
                dynamic_axes={
                    "input_ids": {0: "batch_size", 1: "decoder_sequence_length"},
                    "encoder_hidden_states": seq,
                    "encoder_attention_mask": seq,
                    "logits": {0: "batch_size", 1: "decoder_sequence_length"},
                },
                opset_version=_OPSET,
                dynamo=False,
            )
        for fp32, name in ((enc_fp32, "encoder.onnx"), (dec_fp32, "decoder.onnx")):
            quantize_dynamic(
                str(fp32),
                str(out_dir / name),
                weight_type=QuantType.QInt8,
                per_channel=False,
            )
            print("quantized ->", out_dir / name)

    shutil.copyfile(model_dir / "tokenizer.json", out_dir / "tokenizer.json")
    print("copied tokenizer.json ->", out_dir / "tokenizer.json")


def main() -> None:
    args = sys.argv[1:]
    model_dir = args[0] if args else os.environ.get("TITLE_MODEL_DIR")
    if not model_dir:
        raise SystemExit("usage: python -m scripts._title_export <model_dir> [out_dir]")
    out_dir = (
        args[1]
        if len(args) > 1
        else os.environ.get("TITLE_OUT_DIR", str(REPO / "src" / "traceforge" / "title" / "data"))
    )
    export(model_dir, out_dir)


if __name__ == "__main__":
    main()
