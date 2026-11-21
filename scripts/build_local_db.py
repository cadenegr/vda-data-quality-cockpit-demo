"""Laptop mode: run the engine and write its tables into local/workspace.duckdb (schema vda_dq),
typed like the Databricks pipeline does (scripts/build_notebook.py `save`), plus the empty log tables of sql/setup.sql.

usage: python3 scripts/build_local_db.py [data_dir]
"""
import os
import re
import sys
import time

import duckdb
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from vda_dq.core import run  # noqa: E402

DB = os.path.join(ROOT, "local", "workspace.duckdb")


def numeric_columns():
    """The same list the Databricks notebook converts to numbers (single source: build_notebook.py)."""
    src = open(os.path.join(ROOT, "scripts", "build_notebook.py"), encoding="utf-8").read()
    block = src[src.index("for c in ("):src.index("if c in pdf")]
    return set(re.findall(r'\\?"(\w+)\\?"', block))


def setup_statements():
    """The tables of sql/setup.sql (fix_log, fix_log_carrier, fix_log_manual, cockpit_users) in DuckDB syntax."""
    sql = "\n".join(l for l in open(os.path.join(ROOT, "sql", "setup.sql"), encoding="utf-8")
                    if not l.startswith("--"))
    sql = re.sub(r"\s+COMMENT\s+'[^']*'", "", sql)           # DuckDB has no column/table comments
    sql = re.sub(r"\bSTRING\b", "VARCHAR", sql)
    sql = sql.replace("workspace.vda_dq.", "vda_dq.")
    return [s.strip() for s in sql.split(";") if s.strip()]


def main(data_dir):
    t0 = time.time()
    tables = run(data_dir)
    print(f"engine: {time.time() - t0:.0f}s")
    num = numeric_columns()
    tmp = DB + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    con = duckdb.connect(tmp)
    con.execute("CREATE SCHEMA vda_dq")
    for name, rows in tables.items():
        if name.startswith("_") or not rows:
            continue
        pdf = pd.DataFrame(rows).astype(str).replace({"": None, "None": None})
        for c in num & set(pdf.columns):
            pdf[c] = pd.to_numeric(pdf[c])
        con.register("pdf", pdf)
        con.execute(f"CREATE TABLE vda_dq.{name} AS SELECT * FROM pdf")
        con.unregister("pdf")
        print(f"{name:20} {len(pdf):>7} rows")
    for stmt in setup_statements():
        con.execute(stmt)
    con.close()
    # the file name is the catalog name: workspace.vda_dq.<table>, as on Databricks
    os.replace(tmp, DB)
    print(f"wrote {DB} in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "data"))
