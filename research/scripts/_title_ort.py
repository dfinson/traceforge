"""Torch-free ONNX titler runtime: onnxruntime + tokenizers + numpy only.
No torch / no optimum imported -> ~150-250MB RSS instead of ~1GB.

Stateless batched beam search (re-feeds growing sequence; titles are short and
generated only at span boundaries, so kv-cache isn't needed). Mirrors the HF
generate config used in training/judge: num_beams, no_repeat_ngram_size=2,
repetition_penalty=1.3, length_penalty=0.8."""

import os
import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAD, EOS = 0, 1  # T5 pad=decoder_start, eos=</s>


def _sess(path, threads):
    so = ort.SessionOptions()
    so.intra_op_num_threads = threads
    so.inter_op_num_threads = 1
    return ort.InferenceSession(path, so, providers=["CPUExecutionProvider"])


def _logsoftmax(x):
    x = x - x.max(-1, keepdims=True)
    return x - np.log(np.exp(x).sum(-1, keepdims=True))


class OrtTitler:
    def __init__(self, model_dir, tok_dir=None, q=True, threads=2, prefix="summarize agent step: "):
        sfx = "_q" if q else ""
        self.enc = _sess(os.path.join(model_dir, f"encoder_model{sfx}.onnx"), threads)
        self.dec = _sess(os.path.join(model_dir, f"decoder_model{sfx}.onnx"), threads)
        self.tok = Tokenizer.from_file(os.path.join(tok_dir or model_dir, "tokenizer.json"))
        self.tok.enable_truncation(max_length=512)  # saved json bakes in max_length=20
        self.prefix = prefix

    def _encode(self, text):
        e = self.tok.encode(self.prefix + text)
        ids = np.array([e.ids], dtype=np.int64)
        mask = np.array([e.attention_mask], dtype=np.int64)
        h = self.enc.run(None, {"input_ids": ids, "attention_mask": mask})[0]
        return h, mask

    def generate(
        self, text, num_beams=5, num_return=1, max_new=32, no_repeat=2, rep_pen=1.3, len_pen=0.8
    ):
        h, mask = self._encode(text)  # h:[1,S,H]
        B = num_beams
        hB = np.repeat(h, B, axis=0)
        mB = np.repeat(mask, B, axis=0)
        beams = [[PAD]]
        scores = np.zeros(1, dtype=np.float64)
        done = []  # (length_normalized_score, tokens)
        for step in range(max_new):
            n = len(beams)
            ids = np.array(beams, dtype=np.int64)
            raw = self.dec.run(
                None,
                {
                    "input_ids": ids,
                    "encoder_hidden_states": hB[:n],
                    "encoder_attention_mask": mB[:n],
                },
            )[0][:, -1, :].astype(np.float64)
            for i, toks in enumerate(beams):
                for t in set(toks):  # HF repetition penalty on raw logits
                    raw[i, t] = raw[i, t] / rep_pen if raw[i, t] > 0 else raw[i, t] * rep_pen
                if no_repeat and len(toks) >= no_repeat:  # ban repeated n-grams
                    pref = tuple(toks[-(no_repeat - 1) :]) if no_repeat > 1 else ()
                    for k in range(len(toks) - no_repeat + 1):
                        if tuple(toks[k : k + no_repeat - 1]) == pref:
                            raw[i, toks[k + no_repeat - 1]] = -1e9
            lp = _logsoftmax(raw)
            cand = (scores[:n, None] + lp).reshape(-1)
            V = lp.shape[1]
            order = np.argpartition(cand, -2 * B)[-2 * B :]
            order = order[np.argsort(cand[order])[::-1]]
            new_beams, new_scores = [], []
            for idx in order:
                bi, tok = idx // V, int(idx % V)
                toks = beams[bi] + [tok]
                if tok == EOS:
                    done.append((cand[idx] / (len(toks) ** len_pen), toks))
                else:
                    new_beams.append(toks)
                    new_scores.append(cand[idx])
                if len(new_beams) >= B:
                    break
            if not new_beams:
                break
            beams, scores = new_beams, np.array(new_scores)
            if len(done) >= B:  # early_stopping=True: B finished hypotheses
                break
        # Prefer FINISHED (EOS-terminated) hypotheses; only fall back to
        # unfinished active beams if too few finished (avoids truncated titles).
        done.sort(key=lambda x: x[0], reverse=True)
        if len(done) < num_return:
            tail = sorted(
                ((s / (len(t) ** len_pen), t) for s, t in zip(scores, beams)),
                key=lambda x: x[0],
                reverse=True,
            )
            done = done + tail
        outs, seen = [], set()
        for _, toks in done:
            t = self.tok.decode([x for x in toks if x not in (PAD, EOS)]).strip()
            if t not in seen:
                seen.add(t)
                outs.append(t)
            if len(outs) >= num_return:
                break
        return outs
