"""Smoke test of the cockpit without a browser: renders every main page headless (Streamlit AppTest) against
the laptop-mode database and prints OK / FAIL per page. Run after every change to apps/cockpit or the engine.

usage: python3 scripts/smoke_cockpit.py [path/to/workspace.duckdb]

- Works on a temporary COPY of the database, so it runs while the local cockpit (scripts/cockpit_local.sh) is up
  and never writes into your local data.
- Pages: home, both company lists, admin team overview, the worst supplier / carrier, and the supplier / carrier
  with the most suggested fixes - anonymous and logged in as admin@local.
- Clicking a button that does st.rerun(scope="fragment") fails in AppTest (it can't do fragment reruns); that is a
  limit of the test tool, not an app bug. This script only renders pages, it does not click.
"""
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "apps", "cockpit", "app.py")


def pick_companies(db):
    import duckdb
    c = duckdb.connect(db, read_only=True)
    one = lambda sql: (c.sql(sql).fetchone() or [None])[0]
    picks = {
        "worst supplier": ("Supplier", one("SELECT supplier_code FROM vda_dq.scores_supplier WHERE n_messages >= 20 "
                                           "ORDER BY quality_score LIMIT 1")),
        "supplier with most fixes": ("Supplier", one("SELECT supplier_code FROM vda_dq.fix_suggestions "
                                                     "GROUP BY 1 ORDER BY count(*) DESC LIMIT 1")),
        "worst carrier": ("Carrier", one("SELECT carrier_code FROM vda_dq.scores_carrier WHERE n_messages >= 20 "
                                         "ORDER BY quality_score LIMIT 1")),
    }
    tables = {r[0] for r in c.sql("SELECT table_name FROM duckdb_tables()").fetchall()}
    if "fix_suggestions_carrier" in tables:
        picks["carrier with most fixes"] = ("Carrier", one("SELECT carrier_code FROM vda_dq.fix_suggestions_carrier "
                                                           "GROUP BY 1 ORDER BY count(*) DESC LIMIT 1"))
    c.close()
    return {k: v for k, v in picks.items() if v[1]}


def run(cases, admin):
    from streamlit.testing.v1 import AppTest
    if admin:
        os.environ["COCKPIT_LOCAL_EMAIL"] = "admin@local"
    else:
        os.environ.pop("COCKPIT_LOCAL_EMAIL", None)
    failed = 0
    for name, state in cases.items():
        at = AppTest.from_file(APP, default_timeout=240)
        for k, v in state.items():
            at.session_state[k] = v
        at.run()
        who = "admin" if admin else "anon "
        if at.exception:
            failed += 1
            e = at.exception[0]
            where = [l.strip() for l in e.stack_trace if "apps/cockpit/" in l][-1:]
            print(f"FAIL {who} {name}: {str(e.value)[:300]}  {where}")
        else:
            heads = [s.value for s in at.subheader][:4]
            print(f"OK   {who} {name}: {len(at.dataframe)} tables, {len(at.metric)} metrics · {heads}")
    return failed


def main(src):
    if not os.path.exists(src):
        sys.exit(f"No database {src}: run scripts/cockpit_local.sh once (it builds it).")
    tmp = tempfile.mkdtemp(prefix="smoke_")
    db = os.path.join(tmp, "workspace.duckdb")
    shutil.copy(src, db)
    import duckdb
    c = duckdb.connect(db)
    c.execute("CREATE TABLE IF NOT EXISTS vda_dq.cockpit_users (user_email VARCHAR, role VARCHAR, code VARCHAR, name VARCHAR)")
    if not c.sql("SELECT count(*) FROM vda_dq.cockpit_users WHERE user_email = 'admin@local'").fetchone()[0]:
        c.execute("INSERT INTO vda_dq.cockpit_users VALUES ('admin@local', 'admin', '', 'Local admin')")
    c.close()
    os.environ["COCKPIT_LOCAL_DB"] = db
    sys.path[:0] = [os.path.join(ROOT, "local"), os.path.join(ROOT, "apps", "cockpit")]

    companies = {f"{k} {code}": {"role": role, "code": code} for k, (role, code) in pick_companies(db).items()}
    anon = {"home": {}, "supplier list": {"pick_role": "Supplier"}, "carrier list": {"pick_role": "Carrier"}, **companies}
    admin = {"team overview": {}, **companies}
    failed = run(anon, admin=False) + run(admin, admin=True)
    shutil.rmtree(tmp, ignore_errors=True)
    print("ALL OK" if not failed else f"{failed} page(s) FAILED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "local", "workspace.duckdb"))
