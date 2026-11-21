#!/usr/bin/env bash
# Deploy the project to Databricks.
#
#   scripts/deploy.sh setup      one-time: create fix_log* + cockpit_users tables + grant the app access
#   scripts/deploy.sh pipeline   rebuild notebook from vda_dq/core.py, upload it, run it (refreshes all tables)
#   scripts/deploy.sh genie      push genie/space.json to the Genie space
#   scripts/deploy.sh app        upload apps/cockpit and redeploy the app
#   scripts/deploy.sh all        all of the above
#
# Needs: databricks CLI, logged in (`databricks auth login --host <workspace-url>`), settings in deploy.env
set -euo pipefail
cd "$(dirname "$0")/.."
source deploy.env
export DATABRICKS_CONFIG_PROFILE="$DATABRICKS_PROFILE"

pipeline() {
  echo "==> building notebook"
  python3 scripts/build_notebook.py
  databricks workspace mkdirs "$WORKSPACE_DIR"
  databricks workspace import "$WORKSPACE_DIR/01_ingest_dq" --file notebooks/01_ingest_dq.py \
    --format SOURCE --language PYTHON --overwrite
  echo "==> running pipeline job (takes ~2-4 min)"
  databricks jobs submit --timeout 30m -o json --json "{
    \"run_name\": \"vda_dq pipeline\",
    \"tasks\": [{\"task_key\": \"ingest\", \"notebook_task\": {\"notebook_path\": \"$WORKSPACE_DIR/01_ingest_dq\"}}]
  }" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['state']['result_state'], d.get('run_page_url',''))"
}

genie() {
  echo "==> updating Genie space $GENIE_SPACE_ID"
  python3 - <<'EOF' > /tmp/genie_update.json
import json
s = json.load(open("genie/space.json"))
s["instructions"]["example_question_sqls"].sort(key=lambda x: x["id"])
s["config"]["sample_questions"].sort(key=lambda x: x["id"])
print(json.dumps({"serialized_space": json.dumps(s)}))
EOF
  databricks genie update-space "$GENIE_SPACE_ID" --json @/tmp/genie_update.json -o json > /dev/null
  echo "ok"
}

app() {
  echo "==> copying the engine (vda_dq) next to the app"
  rm -rf apps/cockpit/vda_dq && cp -r vda_dq apps/cockpit/vda_dq && rm -rf apps/cockpit/vda_dq/__pycache__
  echo "==> uploading app source"
  databricks workspace import-dir apps/cockpit "$WORKSPACE_DIR/cockpit" --overwrite > /dev/null
  echo "==> deploying $APP_NAME"
  databricks apps deploy "$APP_NAME" --source-code-path "$WORKSPACE_DIR/cockpit" -o json 2>&1 \
    | python3 -c "import json,sys; t=sys.stdin.read()
try: print(json.loads(t[t.index('{'):])['status'])
except Exception: print('DEPLOY FAILED (another deployment running? retry in a minute):', t.strip()[-500:]); sys.exit(1)"
}

sql_exec() {  # run one SQL statement on the warehouse
  python3 -c 'import json,sys; print(json.dumps({"warehouse_id": sys.argv[1], "wait_timeout": "50s", "statement": sys.argv[2]}))' \
    "$WAREHOUSE_ID" "$1" > /tmp/vda_dq_stmt.json
  databricks api post /api/2.0/sql/statements --json @/tmp/vda_dq_stmt.json \
    | python3 -c "import json,sys; s=json.load(sys.stdin)['status']; print(s['state'], (s.get('error') or {}).get('message',''))"
}

setup() {
  echo "==> creating tables the pipeline must not overwrite (sql/setup.sql)"
  # the statements API runs one statement per call: split the file on ';'
  python3 -c 'import sys; t="\n".join(l for l in open("sql/setup.sql") if not l.startswith("--"))
for s in t.split(";"):
    s = s.strip()
    if s: sys.stdout.write(s + "\0")' > /tmp/vda_dq_setup.sql
  while IFS= read -r -d '' stmt; do sql_exec "$stmt"; done < /tmp/vda_dq_setup.sql
  sp=$(databricks apps get "$APP_NAME" -o json | python3 -c "import json,sys; print(json.load(sys.stdin)['service_principal_client_id'])")
  echo "==> granting app service principal $sp"
  sql_exec "GRANT USE CATALOG ON CATALOG $CATALOG TO \`$sp\`"
  sql_exec "GRANT USE SCHEMA, SELECT ON SCHEMA $CATALOG.$SCHEMA TO \`$sp\`"
  for t in fix_log fix_log_carrier fix_log_manual; do
    sql_exec "GRANT SELECT, MODIFY ON TABLE $CATALOG.$SCHEMA.$t TO \`$sp\`"
  done
  sql_exec "GRANT READ VOLUME ON VOLUME $CATALOG.$SCHEMA.raw TO \`$sp\`"
}

case "${1:-}" in
  setup)    setup ;;
  pipeline) pipeline ;;
  genie)    genie ;;
  app)      app ;;
  all)      pipeline; app ;;   # genie dropped for now (run `deploy.sh genie` by hand to bring it back)
  *) sed -n '2,9p' "$0"; exit 1 ;;
esac
