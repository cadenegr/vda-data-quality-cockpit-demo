"""Warning system in the cockpit: what a partner must fix first, and the plant's alarm board across all partners.

Reads the engine tables `warnings` (one row per partner x problem, ranked) and `warning_overview`
(one row per problem across all partners). Both sections hide themselves when the tables are not there yet
(pipeline not re-run since the warning system was added), so the rest of the page keeps working.
"""
from urllib.parse import quote

import pandas as pd
import streamlit as st

from vda_dq.core import warning_level  # same levels as the engine (critical = stops the truck at the gate)

LEVEL_COLOR = {"critical": "#d62728", "high": "#ff7f0e", "medium": "#e6b800"}
LEVEL = {"critical": ("🔴", "Critical", "the truck is stopped and checked by hand at the gate"),
         "high": ("🟠", "High", "The plant receives wrong or incomplete data"),
         "medium": ("🟡", "Medium", "data is weak or unusual, the delivery still works")}
PATTERN = {"systematic": "🔁 every time – probably one setting in your system",
           "frequent": "frequent", "occasional": "occasional"}
TOP_N = 3
_MSG = {"4933": "transport bookings (VDA 4933)", "4945": "truck status messages (VDA 4945)",
        "4987": "delivery notes (VDA 4987)"}


def _load(q, sql, **params):
    try:
        return q(sql, **params)
    except Exception:  # table missing on an older pipeline run
        return None


def _true(v):
    return str(v).strip().lower() in ("true", "1")


def _timelines(q, T, role, code):
    """Per (rule, level): affected messages per day, on one shared day axis (days without problems = 0)."""
    key = "supplier_code" if role == "Supplier" else "carrier_code"
    d = _load(q, f"""SELECT rule_id, severity, msg_date AS day, count(DISTINCT message_key) AS n
                     FROM {T}.findings
                     WHERE {key} = :code AND responsible = :role AND severity IN ('error', 'warning')
                       AND msg_date IS NOT NULL
                     GROUP BY rule_id, severity, msg_date""", code=code, role=role.lower())
    if d is None or d.empty:
        return {}, None
    d["day"] = pd.to_datetime(d["day"], errors="coerce")
    d = d.dropna(subset=["day"])
    if d.empty:
        return {}, None
    d["level"] = [warning_level(r, s) for r, s in zip(d["rule_id"], d["severity"])]
    days = pd.date_range(d["day"].min(), d["day"].max(), freq="D")
    series = {k: g.groupby("day")["n"].sum().reindex(days, fill_value=0)
              for k, g in d.groupby(["rule_id", "level"])}
    by_level = (d.pivot_table(index="day", columns="level", values="n", aggfunc="sum")
                .reindex(days, fill_value=0).fillna(0))
    return series, by_level


def _card(w, series=None):
    icon, label, meaning = LEVEL.get(w["level"], ("", w["level"], ""))
    with st.container(border=True):
        st.markdown(f"{icon} **{label}** · #{int(w['partner_rank'])} · **{w['title']}**  \n"
                    f"<span style='opacity:.75'>{meaning}</span>", unsafe_allow_html=True)
        facts = [f"**{int(w['n_units']):,} {w['unit']}** ({w['share_pct']:.0f} %)",
                 PATTERN.get(w["pattern"], w["pattern"]),
                 "missing data" if w["kind"] == "missing" else "wrong data"]
        if w["score_gain"] and float(w["score_gain"]) > 0:
            facts.append(f"📈 +{float(w['score_gain']):g} score points")
        if w["ready_fixes"] and int(w["ready_fixes"]) > 0:
            facts.append(f"🔧 {int(w['ready_fixes']):,} fixable with one click in 🔧 Suggested fixes")
        facts.append("⏱ still happening" if _true(w["still_happening"]) else "✅ not seen lately – maybe already fixed")
        st.markdown(" · ".join(facts))
        s = series.get((w["rule_id"], w["level"])) if series else None
        if s is not None and s.sum() > 0:
            last_day = s.index.max()
            st.bar_chart(s.rename("affected messages"), height=110, color=LEVEL_COLOR.get(w["level"]),
                         x_label="", y_label="")
            st.caption(f"Affected messages per day · last day ({last_day:%d %b}): **{int(s.iloc[-1])}** · "
                       f"last seen {w['last_seen']}")
        with st.expander("How to fix"):
            st.markdown(f"**Who fixes it:** {w['who_fixes']}  \n**Why it matters:** {w['impact']}  \n"
                        f"**VDA field:** `{w['field_path']}`  \n**Fix:** {w['fix']}")
            if w.get("example_values"):
                st.caption(f"Values we received: {w['example_values']}")
            st.caption(f"Seen from {w['first_seen']} to {w['last_seen']} · {int(w['n_messages']):,} messages · "
                       f"rule {w['rule_id']}")


def partner_box(q, T, role, code):
    """Company page: the warnings of this partner on a timeline. What is still happening comes first (that is
    where wrong data can still be stopped), ranked by urgency; problems not seen lately come last."""
    ws = _load(q, f"""SELECT * FROM {T}.warnings WHERE role = :role AND partner_code = :code
                      ORDER BY partner_rank""", role=role.lower(), code=code)
    if ws is None:
        return
    title = "🚨 **Fix these first – happening now**"
    if ws.empty:
        with st.expander(f"{title} · ✅ nothing to fix"):
            st.success("No errors or warnings in your messages – nothing to fix. 🎉")
        return
    rows = ws.to_dict("records")
    now = [w for w in rows if _true(w["still_happening"])]
    stopped = [w for w in rows if not _true(w["still_happening"])]
    n = pd.Series([w["level"] for w in now], dtype=object).value_counts()
    counts = " · ".join(f"{LEVEL[lv][0]} {int(n[lv])} {LEVEL[lv][1].lower()}" for lv in LEVEL if lv in n)
    tail = f" · ✅ {len(stopped)} stopped lately" if stopped else ""
    with st.expander(f"{title} · {(counts + ' still happening') if counts else 'nothing still happening'}{tail}"):
        _partner_body(q, T, role, code, now, stopped, n)


def _partner_body(q, T, role, code, now, stopped, n):
    series, by_level = _timelines(q, T, role, code)
    st.caption("Every day these keep coming, more trucks wait at the gate – stop them in your system first."
               if now else "Nothing is going wrong right now.")
    if by_level is not None and by_level.to_numpy().sum() > 0:
        cols = [lv for lv in LEVEL if lv in by_level.columns]
        chart = by_level[cols].rename(columns={lv: f"{LEVEL[lv][0]} {LEVEL[lv][1]}" for lv in cols})
        st.bar_chart(chart, height=170, color=[LEVEL_COLOR[lv] for lv in cols], x_label="", y_label="")
        st.caption("Timeline: your messages with a problem, per day and level.")

    for w in now[:TOP_N]:
        _card(w, series)
    if len(now) > TOP_N:
        with st.expander(f"Show the other {len(now) - TOP_N} problem(s) still happening"):
            for w in now[TOP_N:]:
                _card(w, series)
    if stopped:
        with st.expander(f"✅ Stopped lately ({len(stopped)}) – not seen in your latest messages, maybe already fixed"):
            for w in stopped:
                _card(w, series)


def alarm_board(q, T, open_company=None):
    """Team overview: one row per problem across all partners, most urgent first. Clicking a problem lists every
    firm that has it; clicking a firm opens its cockpit via open_company(role, code) (False = no page)."""
    ov = _load(q, f"SELECT * FROM {T}.warning_overview ORDER BY priority")
    if ov is None or ov.empty:
        return
    st.subheader("🚨 Alarm board")
    st.caption("Every problem across all suppliers and carriers, most urgent first: 🔴 critical stops the truck at the "
               "gate, 🟠 high means wrong or incomplete data, 🟡 medium is weak data. 'Systematic' firms send it "
               "every time – one setting in their system fixes all of it.")
    view = pd.DataFrame({
        "": ov["level"].map(lambda lv: LEVEL.get(lv, ("",))[0]),
        "problem": ov["title"],
        "who": ov["role"],
        "firms": ov["n_partners"].astype(int),
        "affected": ov["n_units"].astype(int).map("{:,}".format) + " " + ov["unit"],
        "systematic firms": ov["n_partners_systematic"].astype(int),
        "still happening": ov["n_partners_still_happening"].astype(int),
        "1-click fixes": ov["ready_fixes"].astype(int),
        "worst firms": ov["top_partners"],
    })
    st.caption("Click a problem to see every firm that has it.")
    ev = st.dataframe(view, hide_index=True, use_container_width=True, height=min(36 * (len(view) + 1) + 4, 420),
                      column_config={"worst firms": st.column_config.TextColumn(width="large")},
                      on_select="rerun", selection_mode="single-row", key="alarm_board")
    if ev.selection.rows:
        _firms(q, T, ov.iloc[ev.selection.rows[0]], open_company)


def _firms(q, T, p, open_company):
    """All firms with one alarm-board problem, most affected first; a click opens a contact card for that firm."""
    icon, label, meaning = LEVEL.get(p["level"], ("", p["level"], ""))
    ws = _load(q, f"""SELECT * FROM {T}.warnings WHERE rule_id = :rule AND level = :level AND role = :role
                      ORDER BY n_units DESC, partner_code""", rule=p["rule_id"], level=p["level"], role=p["role"])
    with st.container(border=True):
        st.markdown(f"{icon} **{p['title']}** · {label} – {meaning}")
        if ws is None or ws.empty:
            st.info("No firm details for this problem.")
            return
        contacted = st.session_state.setdefault("contacted", set())
        st.caption(f"{len(ws)} firm(s) · {p['who_fixes']} · click a firm to contact it or open its cockpit")
        view = pd.DataFrame({
            "firm": ws["partner_name"], "code": ws["partner_code"],
            "affected": ws["n_units"].astype(int).map("{:,}".format) + " " + ws["unit"],
            "share": ws["share_pct"].astype(float).map(lambda v: f"{v:.0f} %"),
            "pattern": ws["pattern"],
            "still happening": ws["still_happening"].map(lambda v: "⏱ yes" if _true(v) else "✅ no"),
            "1-click fixes": ws["ready_fixes"].astype(int),
            "last seen": ws["last_seen"],
            "contacted": [("📨 yes" if (p["rule_id"], c) in contacted else "") for c in ws["partner_code"]],
        })
        ev = st.dataframe(view, hide_index=True, use_container_width=True, height=min(36 * (len(view) + 1) + 4, 380),
                          on_select="rerun", selection_mode="single-row", key=f"alarm_firms_{p['priority']}")
        if ev.selection.rows:
            _contact(ws.iloc[ev.selection.rows[0]], p, open_company)


def _message(w, p):
    """Subject and body of the alert to one firm, in plain words, built only from the warnings row."""
    _, label, meaning = LEVEL.get(w["level"], ("", w["level"], ""))
    subject = f"[Plant] {label} data problem: {w['title']}"
    lines = [
        f"Dear {w['partner_name']} team,",
        "",
        f"The plant's data quality cockpit found a problem in the {_MSG.get(str(w['msg_type'])[-4:], w['msg_type'])} "
        "you send us "
        f"(partner code {w['partner_code']}).",
        "",
        f"Problem: {w['title']} ({label.lower()} – {meaning}).",
        f"How much: {int(w['n_units']):,} {w['unit']} ({float(w['share_pct']):.0f} % of yours), "
        f"seen from {w['first_seen']} to {w['last_seen']}"
        + (" – it is still happening." if _true(w["still_happening"]) else "."),
    ]
    if w["pattern"] == "systematic":
        lines.append("It happens every time, so it is probably one setting in your system: one fix solves all.")
    lines += ["", f"Why it matters: {w['impact']}", f"VDA field: {w['field_path']}", f"How to fix: {w['fix']}"]
    if w.get("example_values"):
        lines.append(f"Values we received: {w['example_values']}")
    if int(w["ready_fixes"] or 0) > 0:
        lines.append(f"{int(w['ready_fixes']):,} of them can be corrected with one click in your cockpit "
                     "(🔧 Suggested fixes).")
    lines += ["", "Please check before you send with 🛡 'Check before you send' in your cockpit.", "",
              "Thank you,", "Plant logistics – data quality"]
    return subject, "\n".join(lines)


def _contact(w, p, open_company):
    """Contact card for one firm: demo contact, ready-written alert, email / copy / mark as contacted / open."""
    code, rule = str(w["partner_code"]), p["rule_id"]
    role = "Supplier" if p["role"] == "supplier" else "Carrier"
    subject, body = _message(w, p)
    with st.container(border=True):
        st.markdown(f"#### 📨 Contact {w['partner_name']} · code {code}")
        c1, c2 = st.columns(2)
        email = f"dq-{code}@{role.lower()}.example"  # demo: the hackathon data has no contacts
        c1.markdown(f"**📧 Email:** `{email}`  \n**📞 Phone:** from the OEM's supplier portal")
        c2.markdown(f"**Who fixes it:** {w['who_fixes']}")
        st.caption("Demo contact – the hackathon data has no contact details. In production the address and phone "
                   "come from the OEM's supplier portal (partner master data).")
        subject = st.text_input("Subject", subject, key=f"mail_subj::{rule}::{code}")
        body = st.text_area("Message", body, height=260, key=f"mail_body::{rule}::{code}")
        contacted = st.session_state.setdefault("contacted", set())
        b1, b2, b3 = st.columns(3)
        b1.link_button("📧 Open in email app", f"mailto:{email}?subject={quote(subject)}&body={quote(body)}",
                       use_container_width=True)
        done = (rule, code) in contacted
        if b2.button("✅ Contacted" if done else "📨 Mark as contacted", disabled=done, use_container_width=True,
                     key=f"mail_done::{rule}::{code}"):
            contacted.add((rule, code))
            st.toast(f"Marked {w['partner_name']} as contacted (demo – not stored)")
            st.rerun()
        if open_company and b3.button("🏠 Open their cockpit", use_container_width=True,
                                      key=f"mail_open::{rule}::{code}"):
            if open_company(role, code) is False:
                st.warning("This firm has no cockpit page (fewer than 5 messages or unknown sender).")
        with st.expander("📋 Copy the message"):
            st.code(f"To: {email}\nSubject: {subject}\n\n{body}", language=None)
