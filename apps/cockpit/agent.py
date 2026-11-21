"""The DQ assistant: an LLM agent with tools that only see ONE supplier's or carrier's data.

Pure Python (no Streamlit) so it can be tested outside the app. The caller injects:
  q(sql, **params) -> DataFrame     read access to the vda_dq tables
  llm(body) -> dict                 one chat-completions call (OpenAI format) to a Databricks model endpoint
  genie(question) -> (text, rows)   optional: ad-hoc statistics via the Genie space
The agent never writes data: propose_fix only returns a proposal; the UI shows Approve/Reject buttons.
"""
import json
import re

MAX_STEPS = 6

ROLE_TEXT = {
    "supplier": "the supplier (they make the parts; they send transport orders VDA 4933 and delivery notes VDA 4987)",
    "carrier": "the carrier (they drive the trucks; they send transport status messages VDA 4945)",
}

SYSTEM = """You are the assistant inside the "VDA Data Quality Cockpit". You are talking to {role_text}
"{name}" (code {code}). Their messages go to the plant.

Background, explain it simply when useful:
- For every truck three standard messages exist: the transport order (VDA 4933, supplier -> carrier:
  "pick up these parts"), the transport status (VDA 4945, carrier -> plant: GPS, licence plate, 15-digit
  gate timeslot) and the delivery note / despatch advice (VDA 4987, supplier -> plant: exactly which parts
  are on the truck).
- The plant links the three into a digital transport file. Only if they are complete and reference each other
  can the truck drive straight through the gate instead of stopping for a manual check.
- Linking order: 1) transport order number, 2) delivery note number, 3) supplier + parts + date.
- Field meanings: transport ORDER number = the supplier's VDA 4933 order id (in 4945: transportInstructionNumber,
  in 4987: RFF+AAO). TRANSPORT number = the carrier's own trip number (4945 transportNumber) - a different thing.
  Delivery note number = supplier's shipment number. Timeslot ID = 15-digit gate booking.
- Scores (0-100): matching (can the message be linked?), completeness (mandatory content present?),
  format (values well-formed?). Each = % of the company's messages without errors or warnings in that
  category; the overall score is their average.
{fix_text}
How to work:
- Use your tools for every fact, number, message or order number. Never invent any of them, never
  estimate future scores yourself, never use made-up placeholder values like "TO12345" as illustrations,
  and never guess reasons (e.g. why a truck status is missing). Only quote
  percentages exactly as the tools give them.
- You only see this company's data. If asked about other companies, say you cannot show them.
- You cannot change data. {propose_text}
- When the user asks for an example, ALWAYS fetch real ones (get_issue_details returns the latest affected
  messages, get_message shows one message). Never make up example messages, plates, numbers or formats.
  The gate timeslot ID is exactly 15 digits; do not describe its internal structure.
- If the question has a wrong premise (e.g. "why is my score low" but it is high compared to peers), say so
  politely, using the numbers.
- Be short and concrete: a few sentences or bullets, plain words, explain jargon. Say what to do next.
- Answer in the language the user writes in. Do not mention tool or function names.
"""

FIX_TEXT_SUPPLIER = """- The "detective" proposes the missing transport order number for delivery notes. Green = high confidence
  (all parts in exactly one order; always right in the back-test). Yellow = several orders fit; the best
  match was right in most back-test cases, so the user must check the evidence.
"""
PROPOSE_SUPPLIER = ("To fix a delivery note you MUST call propose_fix in the same turn - that is what creates the "
                    "Approve / Reject buttons. Never say you proposed a fix without calling it, and never say a fix "
                    "is saved: only the user's click saves it.")
PROPOSE_CARRIER = ("Carriers fix problems in their own transport system; tell them exactly which field to "
                   "send and with which value.")


def _tool(name, desc, props=None, required=()):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props or {}, "required": list(required)}}}


class Agent:
    def __init__(self, q, T, role, code, name, llm, genie=None, window="next_day"):
        self.q, self.T, self.role, self.code, self.name = q, T, role.lower(), str(code), name
        self.llm, self.genie, self.window = llm, genie, window
        self.key = "supplier_code" if self.role == "supplier" else "carrier_code"

    # ------------------------------------------------------------------ tool definitions
    def tools(self):
        t = [
            _tool("get_my_overview", "Score, sub-scores, rank among peers, message counts and top problems of the user's company."),
            _tool("get_issue_details", "Explain one problem type: what it means, which VDA field, how to fix it, how often, latest examples.",
                  {"rule_id": {"type": "string", "description": "rule id from get_my_overview, e.g. ASN_TO_MISSING"}}, ["rule_id"]),
            _tool("get_message", "Look up one of the company's messages by its number (delivery note, transport order, "
                  "or for carriers a transport number) and list its problems.",
                  {"number": {"type": "string"}}, ["number"]),
        ]
        if self.genie:  # only offered when a Genie space is wired in (switched off for now, see app.py GENIE_ON)
            t.append(_tool("ask_statistics", "Ad-hoc statistics over the company's quality findings when no other tool "
                           "fits (e.g. trends per day, counts per problem and message type). Slow (~20 s).",
                           {"question": {"type": "string"}}, ["question"]))
        if self.role == "supplier":
            t += [
                _tool("list_fix_suggestions", "Open (not yet decided) suggestions for the missing transport order number.",
                      {"confidence": {"type": "string", "enum": ["high", "medium", "all"]},
                       "limit": {"type": "integer"}}),
                _tool("explain_fix_suggestion", "Evidence for the suggestion of one delivery note: parts and quantities "
                      "compared, dates, truck confirmation, alternatives, back-test accuracy.",
                      {"delivery_note": {"type": "string"}}, ["delivery_note"]),
                _tool("propose_fix", "Propose to fill the transport order number of a delivery note. Shows Approve/Reject "
                      "buttons to the user; nothing is saved without their click.",
                      {"delivery_note": {"type": "string"}, "transport_order": {"type": "string"}},
                      ["delivery_note", "transport_order"]),
            ]
        return t

    # ------------------------------------------------------------------ tool implementations
    def _rows(self, df, n=10):
        return json.loads(df.head(n).to_json(orient="records", date_format="iso"))

    def get_my_overview(self):
        T, k = self.T, self.key
        me = self.q(f"SELECT * FROM {T}.scores_{self.role} WHERE {k} = :c", c=self.code)
        if me.empty:
            return {"error": "no data for this company"}
        peers = self.q(f"SELECT quality_score FROM {T}.scores_{self.role} WHERE n_messages >= 5")
        m = me.iloc[0]
        top = self.q(f"""SELECT f.rule_id, f.severity, r.category, r.problem, count(*) AS n
                         FROM {T}.findings f JOIN {T}.rules r USING (rule_id)
                         WHERE f.{k} = :c AND f.responsible = :role GROUP BY ALL
                         ORDER BY CASE f.severity WHEN 'error' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, n DESC""",
                     c=self.code, role=self.role)
        out = {
            "company": m["display_name"], "messages_sent": int(m["n_messages"]),
            "overall_score": float(m["quality_score"]), "matching": float(m["score_matching"]),
            "completeness": float(m["score_completeness"]), "format": float(m["score_format"]),
            "better_than_pct_of_peers": round(100 * float((peers["quality_score"] < m["quality_score"]).mean()), 0),
            "top_problems": self._rows(top, 6),
        }
        if self.role == "supplier":
            fx = self._open_suggestions()
            fx = fx.drop_duplicates("message_key")
            out["open_fix_suggestions"] = {"high": int((fx["confidence"] == "high").sum()),
                                           "medium": int((fx["confidence"] == "medium").sum())}
            out["matching_if_all_open_high_fixes_approved"] = self._matching_after(set(fx.loc[fx["confidence"] == "high", "message_key"]))
            out["matching_if_all_open_fixes_approved"] = self._matching_after(set(fx["message_key"]))
        return out

    def get_issue_details(self, rule_id):
        T, k = self.T, self.key
        r = self.q(f"SELECT * FROM {T}.rules WHERE rule_id = :r", r=rule_id)
        if r.empty:
            return {"error": f"unknown rule {rule_id}"}
        ex = self.q(f"""SELECT msg_date, message_key, value FROM {T}.findings
                        WHERE {k} = :c AND responsible = :role AND rule_id = :r ORDER BY msg_date DESC LIMIT 5""",
                    c=self.code, role=self.role, r=rule_id)
        n = self.q(f"SELECT count(*) AS n FROM {T}.findings WHERE {k} = :c AND responsible = :role AND rule_id = :r",
                   c=self.code, role=self.role, r=rule_id)
        return {**self._rows(r, 1)[0], "count_for_you": int(n.iloc[0]["n"]), "latest_examples": self._rows(ex)}

    def get_message(self, number):
        T, k, num = self.T, self.key, str(number).strip()
        found = {}
        if self.role == "supplier":
            asn = self.q(f"""SELECT shipment_number AS delivery_note, asn_number, despatch_date, to_number AS transport_order_number,
                                    plant_code, carrier_name, n_items, part_numbers
                             FROM {T}.msg_4987 WHERE supplier_code = :c AND (shipment_number = :n OR asn_number = :n) LIMIT 3""",
                         c=self.code, n=num)
            to = self.q(f"""SELECT to_number AS transport_order_number, requested_shipment_date, requested_shipment_time,
                                   plant_code, unloading_point, carrier_name, n_positions, part_numbers
                            FROM {T}.msg_4933 WHERE supplier_code = :c AND to_number = :n LIMIT 1""", c=self.code, n=num)
            if not asn.empty:
                found["delivery_note_VDA4987"] = self._rows(asn, 3)
            if not to.empty:
                found["transport_order_VDA4933"] = self._rows(to, 1)
        else:
            st_ = self.q(f"""SELECT process, geo_date, geo_time, plate, timeslot_id, to_number, shipment_number, transport_number
                             FROM {T}.msg_4945 WHERE carrier_code = :c
                               AND (to_number = :n OR transport_number = :n OR shipment_number = :n)
                             ORDER BY geo_date, geo_time""", c=self.code, n=num)
            if not st_.empty:
                found["transport_status_VDA4945"] = {
                    "status_messages": len(st_), "first": self._rows(st_, 1)[0],
                    "last": json.loads(st_.tail(1).to_json(orient="records"))[0],
                    "plates": sorted(set(st_["plate"].dropna()))[:5],
                    "timeslots": sorted(set(st_["timeslot_id"].dropna()))[:5]}
        if not found:
            return {"error": f"no message with number {num} found for your company"}
        fnd = self.q(f"""SELECT DISTINCT f.rule_id, f.severity, r.problem, r.fix FROM {T}.findings f JOIN {T}.rules r USING (rule_id)
                         WHERE f.{k} = :c AND f.responsible = :role
                           AND (f.message_key = :n OR f.message_key LIKE concat(:n, '#%') OR f.value = :n)""",
                     c=self.code, role=self.role, n=num)
        found["problems"] = self._rows(fnd)
        return found

    def ask_statistics(self, question):
        if not self.genie:
            return {"error": "statistics service not available"}
        ctx = (f"Only use data where {self.key} = '{self.code}' and findings.responsible = '{self.role}'. "
               f"Use date_window = 'next_day' for fix_suggestions. Question: {question}")
        text, rows = self.genie(ctx)
        return {"answer": text, "rows": rows[:10]}

    def _open_suggestions(self):
        T = self.T
        return self.q(f"""SELECT s.* FROM {T}.fix_suggestions s
            LEFT JOIN (SELECT message_key, decision FROM (
                         SELECT message_key, decision, row_number() OVER (PARTITION BY message_key ORDER BY ts DESC) rn
                         FROM {T}.fix_log WHERE supplier_code = :c) WHERE rn = 1) d USING (message_key)
            WHERE s.supplier_code = :c AND s.date_window = :w AND (d.decision IS NULL OR d.decision = 'undone')
            ORDER BY CASE s.confidence WHEN 'high' THEN 0 ELSE 1 END, s.despatch_date DESC""", c=self.code, w=self.window)

    FIXABLE = {"ASN_TO_MISSING", "ASN_TO_IMPLAUSIBLE", "ASN_NO_STATUS"}

    def _matching_after(self, extra_keys):
        """Matching score (%) if approved fixes + extra_keys were applied (same logic as the cockpit preview)."""
        T = self.T
        n = int(self.q(f"SELECT n_messages FROM {T}.scores_supplier WHERE supplier_code = :c", c=self.code).iloc[0, 0])
        bad = self.q(f"""SELECT msg_type, message_key, to_json(collect_set(rule_id)) AS rules FROM {T}.findings
                         WHERE supplier_code = :c AND responsible = 'supplier' AND category = 'matching'
                           AND severity IN ('error', 'warning') GROUP BY 1, 2""", c=self.code)
        approved = set(self.q(f"""SELECT message_key FROM (SELECT message_key, decision, row_number() OVER
                                  (PARTITION BY message_key ORDER BY ts DESC) rn FROM {T}.fix_log WHERE supplier_code = :c)
                                  WHERE rn = 1 AND decision = 'approved'""", c=self.code)["message_key"])
        fixed = approved | set(extra_keys)
        still = sum(1 for r in bad.itertuples()
                    if not (r.msg_type == "4987" and r.message_key in fixed and set(json.loads(r.rules)) <= self.FIXABLE))
        return round(100 * max(0, n - still) / n, 1) if n else 0.0

    def list_fix_suggestions(self, confidence="all", limit=10):
        fx = self._open_suggestions()
        if confidence in ("high", "medium"):
            fx = fx[fx["confidence"] == confidence]
        cols = ["delivery_note", "despatch_date", "current_value", "suggested_value", "confidence", "reason", "alternatives"]
        return {"open_total": len(fx), "shown": self._rows(fx[cols], min(int(limit or 10), 20))}

    def explain_fix_suggestion(self, delivery_note):
        T = self.T
        s = self.q(f"""SELECT * FROM {T}.fix_suggestions WHERE supplier_code = :c AND delivery_note = :d AND date_window = :w""",
                   c=self.code, d=str(delivery_note), w=self.window)
        if s.empty:
            return {"error": f"no suggestion for delivery note {delivery_note} of your company"}
        r = s.iloc[0]
        note = self.q(f"SELECT part_number, quantity FROM {T}.items_4987 WHERE source = :s", s=r["source"])
        order = self.q(f"SELECT part_number, quantity FROM {T}.items_4933 WHERE to_number = :t", t=r["suggested_value"])
        head = self.q(f"""SELECT requested_shipment_date, requested_shipment_time, unloading_point
                          FROM {T}.msg_4933 WHERE to_number = :t LIMIT 1""", t=r["suggested_value"])
        acc = self.q(f"SELECT correct, verifiable FROM {T}.detective_quality WHERE date_window = :w AND confidence = :cf",
                     w=self.window, cf=r["confidence"])
        a = {p: float(x or 0) for p, x in zip(note["part_number"], note["quantity"])}
        b = {p: float(x or 0) for p, x in zip(order["part_number"], order["quantity"])}
        same = [p for p in a if p in b and abs(a[p] - b[p]) < 1e-6]
        other_qty = [p for p in a if p in b and abs(a[p] - b[p]) >= 1e-6]
        return {
            "delivery_note": r["delivery_note"], "despatched": r["despatch_date"],
            "suggested_transport_order": r["suggested_value"], "confidence": r["confidence"],
            "order_pick_up": self._rows(head, 1)[0] if not head.empty else None,
            "parts_on_delivery_note": len(a), "same_part_same_quantity": len(same),
            "same_part_other_quantity": len(other_qty), "parts_missing_in_order": len([p for p in a if p not in b]),
            "confirmed_by_truck_status": bool(r["status_confirmed"]) and str(r["status_confirmed"]).lower() != "false",
            "reason": r["reason"], "alternatives": r["alternatives"],
            "backtest": (f"{int(acc.iloc[0]['correct'])} of {int(acc.iloc[0]['verifiable'])} comparable cases correct "
                         f"({100 * int(acc.iloc[0]['correct']) / int(acc.iloc[0]['verifiable']):.0f}%)"
                         if not acc.empty and acc.iloc[0]["verifiable"] else "n/a"),
        }

    def propose_fix(self, delivery_note, transport_order):
        T = self.T
        s = self.q(f"""SELECT * FROM {T}.fix_suggestions WHERE supplier_code = :c AND delivery_note = :d AND date_window = :w""",
                   c=self.code, d=str(delivery_note), w=self.window)
        if s.empty:
            return {"error": f"delivery note {delivery_note} has no open fix for your company"}
        r = s.iloc[0]
        done = self.q(f"""SELECT decision, new_value, ts FROM {T}.fix_log WHERE supplier_code = :c AND message_key = :k
                          ORDER BY ts DESC LIMIT 1""", c=self.code, k=r["message_key"])
        if not done.empty and done.iloc[0]["decision"] in ("approved", "rejected"):
            d = done.iloc[0]
            return {"info": f"already {d['decision']} on {str(d['ts'])[:16]}"
                            + (f" with transport order {d['new_value']}" if d["decision"] == "approved" else "")
                            + " - the user can undo it in the Done list"}
        allowed = {r["suggested_value"]} | {x.split(" ")[0] for x in (r["alternatives"] or "").split("; ") if x}
        if str(transport_order) not in allowed:
            own = self.q(f"SELECT 1 FROM {T}.msg_4933 WHERE supplier_code = :c AND to_number = :t LIMIT 1",
                         c=self.code, t=str(transport_order))
            if own.empty:
                return {"error": f"transport order {transport_order} is not one of your orders"}
        self.proposals.append({**json.loads(s.head(1).to_json(orient="records"))[0], "chosen": str(transport_order)})
        return {"status": "proposal shown to the user with Approve / Reject buttons - not saved yet"}

    @staticmethod
    def _unsupported_percentages(text, msgs, user_text):
        """Percentages in the answer that appear in no tool result (rounded to whole numbers)."""
        evidence = " ".join(m["content"] for m in msgs if m["role"] == "tool") + " " + user_text
        known = set()
        for x in re.findall(r"\d+(?:\.\d+)?", evidence):
            known |= {str(round(float(x))), str(int(float(x)))}
        found = re.findall(r"(\d+(?:[.,]\d+)?)\s*%", text)
        return sorted({f + "%" for f in found if str(round(float(f.replace(",", ".")))) not in known})

    # ------------------------------------------------------------------ the loop
    def run(self, history, user_text):
        """history: list of {"role": "user"|"assistant", "content": str}. Returns dict(text, proposals, trace)."""
        self.proposals, trace = [], []
        system = SYSTEM.format(role_text=ROLE_TEXT[self.role], name=self.name, code=self.code,
                               fix_text=FIX_TEXT_SUPPLIER if self.role == "supplier" else "",
                               propose_text=PROPOSE_SUPPLIER if self.role == "supplier" else PROPOSE_CARRIER)
        msgs = [{"role": "system", "content": system}] + history[-10:] + [{"role": "user", "content": user_text}]
        tools = self.tools()
        nudged = checked = False
        for _ in range(MAX_STEPS):
            resp = self.llm({"messages": msgs, "tools": tools, "max_tokens": 900, "temperature": 0.1})
            m = resp["choices"][0]["message"]
            calls = m.get("tool_calls") or []
            if not calls:
                text = (m.get("content") or "").strip()
                if (self.role == "supplier" and not self.proposals and not nudged
                        and any(w in text.lower() for w in ("approve", "genehmig", "freigeb"))
                        and any(w in user_text.lower() for w in ("fix", "korrig", "approve", "yes", "ja", "ok", "do it", "mach"))):
                    nudged = True  # it promised buttons without creating them: make it call propose_fix
                    msgs += [{"role": "assistant", "content": text},
                             {"role": "user", "content": "(system check) No proposal was created - the user sees no buttons. "
                              "If a fix should be proposed, call propose_fix now; otherwise answer without mentioning Approve."}]
                    continue
                unsupported = self._unsupported_percentages(text, msgs, user_text)
                if unsupported and not checked:
                    checked = True  # numbers must come from tools: ask for a corrected answer once
                    msgs += [{"role": "assistant", "content": text},
                             {"role": "user", "content": f"(system check) These percentages are not in any tool result: "
                              f"{', '.join(unsupported)}. Rewrite the answer without them (or fetch them with a tool)."}]
                    continue
                return {"text": text, "proposals": self.proposals, "trace": trace}
            msgs.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": calls})
            for c in calls:
                name = c["function"]["name"]
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                fn = getattr(self, name, None) if name in {t["function"]["name"] for t in tools} else None
                try:
                    result = fn(**args) if fn else {"error": f"unknown tool {name}"}
                except Exception as e:  # a failing tool must not kill the conversation
                    result = {"error": f"{type(e).__name__}: {e}"}
                trace.append({"tool": name, "args": args})
                msgs.append({"role": "tool", "tool_call_id": c.get("id", name), "content": json.dumps(result, default=str)[:6000]})
        return {"text": "Sorry, that took too many steps. Could you ask more specifically?", "proposals": self.proposals,
                "trace": trace}
