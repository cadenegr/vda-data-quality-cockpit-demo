#!/usr/bin/env bash
# Laptop mode: run the cockpit on this machine against a local copy of the tables (no Databricks needed).
#   scripts/cockpit_local.sh            start (builds local/workspace.duckdb first if it's missing)
#   scripts/cockpit_local.sh rebuild    re-run the engine first (after changing vda_dq/core.py)
# Input: data/ if it exists (real VDA zips, never committed), else demo_data/ (fictional, generated on first run).
# Then open http://localhost:8501. The assistant and Genie need Databricks and don't work offline.
# Decisions (Approve/Reject) go to the local fix_log only, never to Databricks.
# Pretend a login (there is no Databricks login header on localhost):
#   COCKPIT_LOCAL_EMAIL=admin@local scripts/cockpit_local.sh   -> admin: team overview + every company
# The email must be in the local cockpit_users table (admin@local is added automatically).
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ "${1:-}" == "rebuild" || ! -f local/workspace.duckdb ]]; then
  if [[ -d data ]]; then SRC=data; else SRC=demo_data; [[ -d demo_data ]] || python3 scripts/make_demo_data.py; fi
  python3 scripts/build_local_db.py "$SRC"
fi
python3 -c "import duckdb; c = duckdb.connect('local/workspace.duckdb')
c.execute(\"CREATE TABLE IF NOT EXISTS vda_dq.cockpit_users (user_email VARCHAR, role VARCHAR, code VARCHAR, name VARCHAR)\")
if not c.sql(\"SELECT count(*) FROM vda_dq.cockpit_users WHERE user_email = 'admin@local'\").fetchone()[0]:
    c.execute(\"INSERT INTO vda_dq.cockpit_users VALUES ('admin@local', 'admin', '', 'Local admin (laptop mode)')\")"
# same engine copy the deploy makes (apps/cockpit/vda_dq is git-ignored)
rm -rf apps/cockpit/vda_dq && cp -r vda_dq apps/cockpit/vda_dq && rm -rf apps/cockpit/vda_dq/__pycache__
export PYTHONPATH="$PWD/local${PYTHONPATH:+:$PYTHONPATH}"
cd apps/cockpit
exec python3 -m streamlit run app.py --server.headless true --server.address localhost --browser.gatherUsageStats false
