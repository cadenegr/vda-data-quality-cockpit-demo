"""⏰ Real-time alarms: which missing / wrong info is needed in how many days so no truck waits at the gate.

Reads the engine table `alarms` (one row per open error × unit, with `known_from` = the day the plant got the message and
`needed_by` = the truck date). A day switch simulates a past day: only messages sent up to that day are known.
Colours (user's choice): 🔴 red = the truck would be stopped at the gate AND the info is needed today/tomorrow,
🟠 orange = stopped at the gate but later, 🟡 yellow = wrong data, delivery still works. Hidden if the table is missing.
"""
from datetime import date

import pandas as pd
import streamlit as st

DAYS = {"15 June 2026": "2026-06-15", "22 June 2026": "2026-06-22", "Live (today)": None}
DEFAULT_DAY = "22 June 2026"
COLOUR = {"red": ("🔴", 0), "orange": ("🟠", 1), "yellow": ("🟡", 2)}


def _sql(T, partner=False):
    return f"""
        SELECT *, datediff(DAY, to_date(:day), to_date(needed_by)) AS days_left,
               CASE WHEN level = 'critical' AND datediff(DAY, to_date(:day), to_date(needed_by)) <= 1 THEN 'red'
                    WHEN level = 'critical' THEN 'orange' ELSE 'yellow' END AS colour
        FROM {T}.alarms
        WHERE known_from <= :day AND needed_by >= :day
              {"AND role = :role AND partner_code = :code" if partner else ""}
        ORDER BY days_left, colour"""


def _load(q, sql, **params):
    try:
        return q(sql, **params)
    except Exception:  # table missing on an older pipeline run
        return None


def _units(unit, n):
    if n == 1:
        return unit
    return "shipments on a truck" if unit.startswith("shipment") else f"{unit}s"


def _when(days):
    days = int(days)
    return "needed today" if days <= 0 else "needed tomorrow" if days == 1 else f"in {days} days"


def _day(where):
    """The simulated-day switch; the choice is shared between Team overview and the partner pages."""
    pick = st.segmented_control("Day", list(DAYS), default=st.session_state.get("alarm_day", DEFAULT_DAY),
                                key=f"alarm_day::{where}", label_visibility="collapsed")
    pick = pick or st.session_state.get("alarm_day", DEFAULT_DAY)
    st.session_state.alarm_day = pick
    iso = DAYS[pick] or date.today().isoformat()
    return pick, iso


def _no_alarms(q, T, pick, iso, who="any partner"):
    if DAYS[pick] is None:
        last = _load(q, f"SELECT max(known_from) AS d FROM {T}.alarms")
        d = "" if last is None or last.empty else str(last["d"][0])
        st.success(f"✅ No open alarms for {who} today ({iso}). Honest note: the newest message in our data is "
                   f"from {d}, so nothing new is known. New messages → pipeline run → alarms appear by themselves.")
    else:
        st.success(f"✅ No open alarms for {who} on {pick}.")


def _honesty():
    st.caption("Deadlines are the partner's own dates (truck arrival in the delivery note, ETA or gate report in the "
               "truck status); where a delivery note has no arrival date we estimate despatch day + 1. "
               "Simulated day: only messages sent up to that day count. 'Fix ready' uses all data, also later days.")


def team_board(q, T, open_company=None):
    """Team overview: red banner + one row per partner × problem, most urgent first; click opens the firm."""
    if _load(q, f"SELECT 1 FROM {T}.alarms LIMIT 1") is None:
        return
    st.subheader("⏰ Real-time alarms – info needed before the truck arrives")
    pick, iso = _day("team")
    a = _load(q, _sql(T), day=iso)
    if a is None or a.empty:
        _no_alarms(q, T, pick, iso)
        return
    n = a["colour"].value_counts()
    red = a[a["colour"] == "red"]
    if len(red):
        st.error(f"🔴 **{len(red):,} red alarms at {red['partner_code'].nunique():,} partners** – info needed "
                 f"today or tomorrow, or the truck is stopped at the gate.")
    st.caption(f"On {pick if DAYS[pick] else 'today (' + iso + ')'}: 🔴 {int(n.get('red', 0)):,} · 🟠 {int(n.get('orange', 0)):,} (truck stopped, later) · "
               f"🟡 {int(n.get('yellow', 0)):,} (wrong data, truck still gets through). Click a row to open the firm.")

    a["rank"] = a["colour"].map(lambda c: COLOUR[c][1])
    a["is_ready"] = a["ready_fix"].astype(str).str.lower().isin(("true", "1"))
    g = (a.groupby(["role", "partner_code", "partner_name", "rule_id", "title", "unit", "who_fixes"], dropna=False)
         .agg(worst=("rank", "min"), n=("unit_key", "size"), n_red=("colour", lambda c: int((c == "red").sum())),
              days=("days_left", "min"), needed_by=("needed_by", "min"), ready=("is_ready", "sum"))
         .reset_index().sort_values(["worst", "days", "n"], ascending=[True, True, False]))
    icon = {v[1]: v[0] for v in COLOUR.values()}
    view = pd.DataFrame({
        "": g["worst"].map(icon),
        "firm": g["partner_name"].fillna(g["partner_code"]), "code": g["partner_code"],
        "who": g["role"],
        "problem": g["title"],
        "open": [f"{int(k):,} {_units(u, int(k))}" for k, u in zip(g["n"], g["unit"])],
        "red": g["n_red"].astype(int),
        "first deadline": [f"{d} · {_when(x)}" for d, x in zip(g["needed_by"], g["days"])],
        "fix ready": g["ready"].astype(int),
        "who fixes": g["who_fixes"],
    })
    nonce = st.session_state.get("table_nonce", 0)
    ev = st.dataframe(view, hide_index=True, use_container_width=True, height=380,
                      on_select="rerun", selection_mode="single-row", key=f"alarms_team_{nonce}",
                      column_config={"problem": st.column_config.TextColumn(width="large")})
    if ev.selection.rows and open_company:
        st.session_state.table_nonce = nonce + 1
        r = g.iloc[ev.selection.rows[0]]
        if open_company("Supplier" if r["role"] == "supplier" else "Carrier", r["partner_code"]) is False:
            st.warning("This firm has no cockpit page (fewer than 5 messages or unknown sender).")
    _honesty()


def partner_banner(q, T, role, code):
    """Partner page, at the top: red banner per problem (needed today/tomorrow), then orange / yellow, and the list."""
    if _load(q, f"SELECT 1 FROM {T}.alarms LIMIT 1") is None:
        return
    c1, c2 = st.columns([2, 3], vertical_alignment="center")
    c1.markdown("**⏰ Real-time alarms** · simulated day")
    with c2:
        pick, iso = _day("partner")
    a = _load(q, _sql(T, partner=True), day=iso, role=role.lower(), code=code)
    if a is None or a.empty:
        _no_alarms(q, T, pick, iso, "you")
        return
    a["is_ready"] = a["ready_fix"].astype(str).str.lower().isin(("true", "1"))
    show = {"red": st.error, "orange": st.warning, "yellow": st.info}
    for colour in ("red", "orange", "yellow"):
        for (rule, title, unit), g in a[a["colour"] == colour].groupby(["rule_id", "title", "unit"], sort=False):
            first = g.sort_values("days_left").iloc[0]
            k, ready = len(g), int(g["is_ready"].sum())
            tail = ("or the truck is stopped at the gate." if colour != "yellow"
                    else "– the truck gets through, but the plant gets wrong data.")
            text = (f"{COLOUR[colour][0]} **{k:,} {_units(unit, k)}** – {title}. First one needed by "
                    f"**{first['needed_by']}** ({_when(first['days_left'])}) {tail}")
            if ready:
                text += f"  \n🔧 **{ready:,} fix ready** – approve below in 🔧 Suggested fixes."
            show[colour](text)
    with st.expander(f"Which ones? ({len(a):,} open on {pick if DAYS[pick] else 'today'})"):
        icon = {k: v[0] for k, v in COLOUR.items()}
        st.dataframe(pd.DataFrame({
            "": a["colour"].map(icon), "problem": a["title"], "what": a["unit_key"],
            "needed by": a["needed_by"], "when": a["days_left"].map(_when),
            "deadline from": a["deadline_basis"], "fix ready": a["is_ready"].map({True: "🔧 yes", False: ""}),
        }), hide_index=True, use_container_width=True, height=min(36 * (len(a) + 1) + 4, 380))
        _honesty()
