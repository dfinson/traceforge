import pyarrow.parquet as pq
from pathlib import Path

p = Path(
    "data/interim/labeling-corpus/swe-agent-nebius/Melevir__cognitive_complexity-15__swe-agent-llama-70b__a000.parquet"
)
t = pq.read_table(p)
df = t.to_pandas()
print("rows:", len(df))
print("kinds:", df["kind"].value_counts().to_dict())
print("tool_names:", df["tool_name"].dropna().value_counts().to_dict())
print()
print("first 10 events:")
for i in range(min(10, len(df))):
    r = df.iloc[i]
    print(
        f"  seq={r['seq']:>3} kind={r['kind']:<25} tool={r['tool_name']!s:<15} phases={r['phase_signals']}"
    )

print()
ref = next(Path("data/interim/labeling-corpus").glob("*.parquet"))
ref_cols = set(pq.read_table(ref).column_names)
new_cols = set(t.column_names)
print("missing in new:", ref_cols - new_cols)
print("extra   in new:", new_cols - ref_cols)
