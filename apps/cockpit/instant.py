"""Instant check of one pasted / uploaded VDA message, for the cockpit and the assistant (no Streamlit inside).

Uses the engine's check_message (vda_dq/core.py, copied next to the app by scripts/deploy.sh) and rebuilds
"what the plant already knows" from the tables: transport orders with their parts, and what the tracking references.
"""
import json

from vda_dq.core import check_message


def known_from_tables(q, T):
    t1 = q(f"SELECT to_number, supplier_code, requested_shipment_date, plant_code FROM {T}.msg_4933")
    items = q(f"SELECT to_number, part_number, quantity FROM {T}.items_4933")
    parts = {to: g[["part_number", "quantity"]].to_dict("records") for to, g in items.groupby("to_number")}
    t1_rows = [{"to_number": r.to_number or "", "supplier_code": r.supplier_code or "",
                "requested_shipment_date": r.requested_shipment_date or "", "plant_code": r.plant_code or "",
                "_parts": parts.get(r.to_number, [])}  # with amounts: the detective's tie-breaker needs them
               for r in t1.itertuples()]
    ts = q(f"SELECT DISTINCT to_number, shipment_number, supplier_code, plate FROM {T}.msg_4945")
    ts_rows = [{"to_number": r.to_number or "", "shipment_number": r.shipment_number or "",
                "supplier_code": r.supplier_code or "", "plate": r.plate or ""} for r in ts.itertuples()]
    return {"t1": t1_rows, "ts": ts_rows}


def check(text, q, T):
    return check_message(text, known_from_tables(q, T))


def extract_json(text):
    """The JSON object inside a chat message (people paste it with words around it), or None."""
    i, j = text.find("{"), text.rfind("}")
    if i < 0 or j <= i:
        return None
    candidate = text[i:j + 1]
    try:
        json.loads(candidate)
        return candidate
    except json.JSONDecodeError:
        return None


def compact(res):
    """Short version of a check result for the language model."""
    if res.get("error"):
        return {"error": res["error"]}
    return {
        "message_type": res["type"], "verdict": res["verdict"], "sender": res["sender"], "summary": res["summary"],
        "problems": [{k: f.get(k) for k in ("severity", "plain_title", "who_fixes", "impact", "fix", "field_path", "value")}
                     for f in res["findings"]],
        "suggested_booking_number": res["suggestion"],
        "feedback_goes_to": f"{res['sender']['name']} (code {res['sender']['code']}) via the supplier contacts in the OEM's portal",
    }
