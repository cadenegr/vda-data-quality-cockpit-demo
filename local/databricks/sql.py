"""Stand-in for databricks.sql: connect() returns a DuckDB connection that understands the cockpit's SQL."""
import os
import re
import threading

import duckdb

DB = os.environ.get("COCKPIT_LOCAL_DB", os.path.join(os.path.dirname(__file__), "..", "workspace.duckdb"))
_lock = threading.Lock()  # Streamlit runs sessions in threads; one DuckDB connection, used one at a time

# Databricks SQL -> DuckDB, only for what the cockpit uses
_PARAM = re.compile(r"(?<![:\w]):([A-Za-z_]\w*)")              # :code -> $code   (not ::casts, not 08:00)
_COUNT2 = re.compile(r"count\(\s*DISTINCT\s+([^(),]+?),\s*([^(),]+?)\)", re.I)  # count(DISTINCT a, b)


_GROUP_ALL_ORDER = re.compile(r"^(.*\bGROUP BY ALL\b)\s+(ORDER BY\b.*?)(\s+LIMIT\s+\d+)?\s*$", re.I | re.S)


def _collect_set(s):
    """collect_set(x) -> list_distinct(list(x)), with x possibly containing parentheses."""
    out, i = [], 0
    while True:
        j = s.lower().find("collect_set(", i)
        if j < 0:
            return "".join(out) + s[i:]
        k, depth = j + len("collect_set("), 1
        while depth:
            depth += {"(": 1, ")": -1}.get(s[k], 0)
            k += 1
        out.append(s[i:j] + "list_distinct(list(" + s[j + len("collect_set("):k - 1] + "))")
        i = k


def translate(statement):
    s = _PARAM.sub(r"$\1", statement)
    s = _collect_set(s)
    s = _COUNT2.sub(r"count(DISTINCT (\1, \2))", s)
    s = re.sub(r"\bcurrent_timestamp\(\)", "current_timestamp", s, flags=re.I)  # no () in DuckDB
    s = re.sub(r"`([^`]*)`", r'"\1"', s)  # `quoted name` -> "quoted name"
    # to_date(x) -> DATE, NULL when x is not a date (like Databricks with ANSI off)
    s = re.sub(r"\bto_date\(([^()]*)\)", r"try_cast(\1 AS DATE)", s, flags=re.I)
    s = re.sub(r"\bdatediff\(\s*DAY\s*,", "datediff('day',", s, flags=re.I)  # Databricks unit keyword -> DuckDB string
    # Databricks may ORDER BY f.col after GROUP BY ALL; DuckDB can't bind that -> sort the grouped result instead
    m = _GROUP_ALL_ORDER.match(s)
    if m:
        order = re.sub(r"\b[A-Za-z_]\w*\.([A-Za-z_]\w*)", r"\1", m.group(2))
        s = f"SELECT * FROM ({m.group(1)}) AS grouped {order}{m.group(3) or ''}"
    return s


class _Cursor:
    def __init__(self, con):
        self.con, self.result = con, None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, statement, params=None):
        s = translate(statement)
        used = set(re.findall(r"\$([A-Za-z_]\w*)", s))
        p = {k: v for k, v in (params or {}).items() if k in used}
        with _lock:
            cur = self.con.cursor()
            cur.execute(s, p or None)
            self.result = cur.fetch_arrow_table() if cur.description else None

    def fetchall_arrow(self):
        import pyarrow as pa
        return self.result if self.result is not None else pa.table({})

    def close(self):
        pass


class _Connection:
    def __init__(self):
        if not os.path.exists(DB):
            raise RuntimeError(f"No local database {DB}: run scripts/cockpit_local.sh (it builds it first).")
        self.con = duckdb.connect()
        self.con.execute(f"ATTACH '{os.path.abspath(DB)}' AS workspace")  # catalog name as on Databricks
        self.con.execute("USE workspace.vda_dq")

    def cursor(self):
        return _Cursor(self.con)

    def close(self):
        pass


def connect(**_):
    return _Connection()
