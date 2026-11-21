"""Talk to the cockpit assistant from the terminal, against the real Databricks tables.

usage: python3 scripts/try_agent.py <supplier|carrier> <code> "question" ["follow-up" ...]
Uses the Databricks CLI (logged in) for SQL and the model endpoint - no app needed.
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "cockpit"))
sys.path.insert(0, str(ROOT))  # the engine (vda_dq); in the app it is copied next to app.py
from agent import Agent  # noqa: E402
import instant  # noqa: E402

ENV = dict(line.strip().split("=", 1) for line in open(pathlib.Path(__file__).resolve().parents[1] / "deploy.env")
           if "=" in line and not line.startswith("#"))
T = f"{ENV['CATALOG']}.{ENV['SCHEMA']}"
MODEL = os.environ.get("AGENT_MODEL", "databricks-qwen3-next-80b-a3b-instruct")


def api_post(path, body):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(body, f)
    out = subprocess.run(["databricks", "api", "post", path, "--json", f"@{f.name}"], capture_output=True, text=True)
    os.unlink(f.name)
    return json.loads(out.stdout or out.stderr)


CAST = {"DOUBLE": float, "FLOAT": float, "DECIMAL": float, "LONG": int, "INT": int, "BOOLEAN": lambda v: v == "true"}


def q(statement, **params):
    d = api_post("/api/2.0/sql/statements", {
        "warehouse_id": ENV["WAREHOUSE_ID"], "wait_timeout": "50s", "statement": statement,
        "parameters": [{"name": k, "value": str(v)} for k, v in params.items()]})
    if d["status"]["state"] != "SUCCEEDED":
        raise RuntimeError(d["status"])
    cols = d["manifest"]["schema"]["columns"]
    df = pd.DataFrame((d.get("result") or {}).get("data_array") or [], columns=[c["name"] for c in cols])
    for c in cols:
        f = CAST.get(c["type_name"])
        if f is not None:
            df[c["name"]] = df[c["name"]].map(lambda v: None if v is None else f(v))
    return df


def llm(body):
    return api_post(f"/serving-endpoints/{MODEL}/invocations", body)


if __name__ == "__main__":
    role, code, *questions = sys.argv[1:]
    k = "supplier_code" if role == "supplier" else "carrier_code"
    name = q(f"SELECT display_name FROM {T}.scores_{role} WHERE {k} = :c", c=code).iloc[0, 0]
    known = {}

    def checker(text):
        if "k" not in known:
            known["k"] = instant.known_from_tables(q, T)
        return instant.check_message(text, known["k"])

    agent = Agent(q, T, role, code, name, llm, checker=checker)
    history = []
    for question in questions:
        if question.startswith("@"):  # @path/to/message.json -> paste that file
            question = "Please check this message: " + open(question[1:]).read()
        res = agent.run(history, question)
        print(f"\n🧑 {question[:120]}{'…' if len(question) > 120 else ''}\n🤖 {res['text']}")
        print(f"   tools: {[t['tool'] + json.dumps(t['args']) for t in res['trace']]}")
        if res["proposals"]:
            print(f"   proposals: {[(p['delivery_note'], p['chosen'], p['confidence']) for p in res['proposals']]}")
        history += [{"role": "user", "content": question}, {"role": "assistant", "content": res["text"]}]
