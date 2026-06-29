"""CPU-only footprint probe for the titler: per-segment decode latency + RAM.

Live serving processes ONE segment at a time as events stream, on CPU, with
threads capped. This measures whether a given checkpoint meets the near-zero
footprint constraint. Run:
  $env:TITLE_MODEL_DIR="data\\interim\\t5-title-model"        # tiny
  .venv\\Scripts\\python.exe -u -m scripts._title_footprint
"""
from __future__ import annotations

import os
import time

os.environ["CUDA_VISIBLE_DEVICES"] = "-1"  # force CPU
os.environ.setdefault("OMP_NUM_THREADS", "2")

import pandas as pd  # noqa: E402
import torch  # noqa: E402
from transformers import AutoTokenizer, T5ForConditionalGeneration  # noqa: E402

from scripts._title_t5_train import DATASET, MAX_SRC, MAX_TGT, MODEL_DIR, PREFIX  # noqa: E402

NB = 5
N = 40


def main() -> None:
    torch.set_num_threads(int(os.environ.get("OMP_NUM_THREADS", "2")))
    assert not torch.cuda.is_available(), "CPU-only probe; CUDA must be hidden"

    df = pd.read_parquet(DATASET)
    ho = df[df.split == "heldout"].sample(N, random_state=0).reset_index(drop=True)

    import psutil
    proc = psutil.Process()
    rss0 = proc.memory_info().rss / 1e6

    tok = AutoTokenizer.from_pretrained(MODEL_DIR)
    mdl = T5ForConditionalGeneration.from_pretrained(MODEL_DIR).eval()
    nparams = sum(p.numel() for p in mdl.parameters())
    rss_load = proc.memory_info().rss / 1e6

    lat = []
    peak = rss_load
    with torch.no_grad():
        for c in ho.ctx:  # one segment at a time (live serving)
            enc = tok(PREFIX + c, padding=True, truncation=True,
                      max_length=MAX_SRC, return_tensors="pt")
            t0 = time.perf_counter()
            mdl.generate(**enc, max_new_tokens=MAX_TGT, num_beams=NB,
                         num_return_sequences=NB, no_repeat_ngram_size=2,
                         repetition_penalty=1.3, length_penalty=0.8,
                         early_stopping=True)
            lat.append(time.perf_counter() - t0)
            peak = max(peak, proc.memory_info().rss / 1e6)

    lat.sort()
    p50 = lat[len(lat) // 2] * 1000
    p90 = lat[int(len(lat) * 0.9)] * 1000
    mx = lat[-1] * 1000
    print(f"model_dir   : {MODEL_DIR}")
    print(f"params      : {nparams/1e6:.1f}M")
    print(f"threads     : {torch.get_num_threads()}")
    print(f"RSS baseline: {rss0:.0f} MB")
    print(f"RSS loaded  : {rss_load:.0f} MB  (model add {rss_load-rss0:.0f} MB)")
    print(f"RSS peak    : {peak:.0f} MB  (decode add {peak-rss_load:.0f} MB)")
    print(f"latency/seg : p50 {p50:.0f} ms  p90 {p90:.0f} ms  max {mx:.0f} ms  (n={N}, beams={NB})")

    # Log AFTER measurement so the mlflow import never contaminates the RSS probe.
    import mlflow
    from tracemill_research.mlflow_utils import log_yaml_params, start_run
    from tracemill_research.paths import EXPERIMENTS_DIR

    yaml_path = EXPERIMENTS_DIR / "titler-architecture-sweep.yaml"
    with start_run("titler-architecture-sweep-v1",
                   run_name=os.path.basename(MODEL_DIR),
                   tags={"probe": "footprint", "model_dir": MODEL_DIR}):
        log_yaml_params(yaml_path)
        mlflow.log_param("model_dir", MODEL_DIR)
        mlflow.log_param("params_m", round(nparams / 1e6, 2))
        mlflow.log_param("threads", torch.get_num_threads())
        mlflow.log_param("beams", NB)
        mlflow.log_param("n", N)
        mlflow.log_metric("rss_baseline_mb", rss0)
        mlflow.log_metric("rss_loaded_mb", rss_load)
        mlflow.log_metric("rss_peak_mb", peak)
        mlflow.log_metric("model_add_mb", rss_load - rss0)
        mlflow.log_metric("decode_add_mb", peak - rss_load)
        mlflow.log_metric("latency_p50_ms", p50)
        mlflow.log_metric("latency_p90_ms", p90)
        mlflow.log_metric("latency_max_ms", mx)


if __name__ == "__main__":
    main()
