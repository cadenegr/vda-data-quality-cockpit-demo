"""Laptop mode: stand-in for the `databricks` Python packages, so the cockpit (apps/cockpit/app.py) runs
unchanged on a laptop against a local DuckDB copy of the tables. Only on PYTHONPATH when started with
scripts/cockpit_local.sh; the real Databricks App never sees this folder."""
