"""Generate notebooks/01_ingest_dq.py (Databricks source format) from vda_dq/core.py."""
import os
import pathlib

os.chdir(pathlib.Path(__file__).resolve().parents[1])  # run from anywhere
core = open("vda_dq/core.py", encoding="utf-8").read()
SEP = "\n\n# COMMAND ----------\n\n"

cells = [
    "# Databricks notebook source\n"
    "# MAGIC %md\n"
    "# MAGIC # VDA Data Quality - ingest & check\n"
    "# MAGIC Reads the zipped VDA4933 T1 / VDA4945 / VDA4987 JSON messages from a Unity Catalog volume,\n"
    "# MAGIC flattens them, runs the quality rules and writes Delta tables to `CATALOG.SCHEMA`.",

    'CATALOG = "workspace"\n'
    'SCHEMA = "vda_dq"\n'
    'SOURCE = f"/Volumes/{CATALOG}/{SCHEMA}/raw"   # folder with the uploaded zip files\n'
    'import os\n'
    'print(sorted(os.listdir(SOURCE)))',

    "# --- generated from vda_dq/core.py, edit there and rebuild ---\n" + core,

    'import time\n'
    't0 = time.time()\n'
    'tables = run(SOURCE, progress=lambda i: print(f"... {i} files"))\n'
    'print(f"parsed in {time.time()-t0:.0f}s", tables["_stats"])\n'
    'for k, v in tables.items():\n'
    '    if not k.startswith("_"):\n'
    '        print(f"{k:16} {len(v):>7} rows")',

    'import pandas as pd\n'
    '\n'
    'def save(name, rows):\n'
    '    pdf = pd.DataFrame(rows).astype(str)   # all strings; cast in SQL views where needed\n'
    '    pdf = pdf.replace({"": None, "None": None})  # missing values -> real NULLs (Genie/SQL friendly)\n'
    '    for c in ("quality_score", "score_matching", "score_completeness", "score_format", "error_free_pct", "confidence_score", "parts_matched", "parts_total", "n_candidates", "verifiable", "correct", "accuracy_pct", "n_messages", "n_with_errors", "n_errors", "n_warnings", "n_info",\n'
    '              "n_4933", "n_4945", "n_4987", "n_positions", "n_items", "n_status_messages",\n'
    '              "n_units", "share_pct", "score_gain", "ready_fixes", "partner_rank", "global_rank", "priority", "n_partners",\n'
    '              "n_partners_systematic", "n_partners_still_happening"):\n'
    '        if c in pdf:\n'
    '            pdf[c] = pd.to_numeric(pdf[c])\n'
    '    (spark.createDataFrame(pdf).write.mode("overwrite").option("overwriteSchema", "true")\n'
    '        .saveAsTable(f"{CATALOG}.{SCHEMA}.{name}"))\n'
    '    print("wrote", f"{CATALOG}.{SCHEMA}.{name}", len(pdf))\n'
    '\n'
    'for name, rows in tables.items():\n'
    '    if not name.startswith("_") and rows:\n'
    '        save(name, rows)',

    "# MAGIC %sql\n"
    "# MAGIC SELECT rule_id, severity, responsible, count(*) AS n\n"
    "# MAGIC FROM workspace.vda_dq.findings GROUP BY ALL ORDER BY n DESC",
]

os.makedirs("notebooks", exist_ok=True)
with open("notebooks/01_ingest_dq.py", "w", encoding="utf-8") as fh:
    fh.write(SEP.join(cells) + "\n")
print("wrote notebooks/01_ingest_dq.py")
