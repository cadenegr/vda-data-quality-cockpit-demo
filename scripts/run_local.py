"""Run the VDA quality checks locally and write CSVs to out/.

usage: python3 scripts/run_local.py [source_dir_or_zip] [out_dir]
"""
import csv
import os
import pathlib
import sys
from collections import Counter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from vda_dq.core import run  # noqa: E402

src = sys.argv[1] if len(sys.argv) > 1 else "data"
out = sys.argv[2] if len(sys.argv) > 2 else "out"
os.makedirs(out, exist_ok=True)

tables = run(src, progress=lambda i: print(f"  ... {i} files", file=sys.stderr))
for name, rows in tables.items():
    if name.startswith("_") or not rows:
        continue
    cols = list(dict.fromkeys(k for r in rows for k in r))
    with open(os.path.join(out, f"{name}.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    print(f"{name:16} {len(rows):>7} rows")

print("stats:", tables["_stats"])
print("\nfindings by rule:")
for (rule, sev), n in Counter((f["rule_id"], f["severity"]) for f in tables["findings"]).most_common():
    print(f"  {sev:7} {rule:28} {n:>7}")
