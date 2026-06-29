"""Quick inspection of nebius/SWE-agent-trajectories shard 0."""

import statistics

import pyarrow.parquet as pq

p = "data/raw/swe-agent-nebius/data/train-00000-of-00012.parquet"
t = pq.read_table(p)
df = t.to_pandas()
resolved = df[df["target"]].iloc[0]
traj = resolved["trajectory"]
print("trajectory entries:", len(traj))
print("roles in this trajectory:", sorted({e["role"] for e in traj}))
print()
print("=== first 6 entries ===")
for i, e in enumerate(traj[:6]):
    role = e["role"]
    txt = e["text"] or ""
    if len(txt) > 500:
        txt = txt[:500] + "...[truncated]..."
    print(f"--- {i} role={role} (len={len(e['text'] or '')}) ---")
    print(txt)
    print()

print()
print("=== entries 6-12 ===")
for i, e in enumerate(traj[6:12], start=6):
    role = e["role"]
    txt = e["text"] or ""
    if len(txt) > 400:
        txt = txt[:400] + "...[truncated]..."
    print(f"--- {i} role={role} (len={len(e['text'] or '')}) ---")
    print(txt)
    print()

# Step count distribution across all resolved trajectories in this shard
resolved_df = df[df["target"]]
lens = [len(t) for t in resolved_df["trajectory"]]
print(f"resolved in shard: {len(lens)}")
print(
    f"step count: min={min(lens)} p25={statistics.quantiles(lens, n=4)[0]:.0f} "
    f"median={statistics.median(lens):.0f} p75={statistics.quantiles(lens, n=4)[2]:.0f} "
    f"p90={statistics.quantiles(lens, n=10)[8]:.0f} max={max(lens)}"
)
