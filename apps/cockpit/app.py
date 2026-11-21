"""VDA Data Quality Cockpit - v0 (Databricks App, Streamlit).

A supplier or carrier picks themselves, sees their data-quality score, their top
problems with the exact VDA field and fix, a trend, and can ask the assistant questions.
"""
import os

import pandas as pd
import streamlit as st
from databricks import sql
from databricks.sdk import WorkspaceClient
from databricks.sdk.core import Config

import alarms_ui
import check_box
import fixes
import warnings_ui
from agent import Agent

T = "workspace.vda_dq"
WAREHOUSE_ID = os.environ.get("DATABRICKS_WAREHOUSE_ID", "")
GENIE_SPACE_ID = os.environ.get("GENIE_SPACE_ID", "")
GENIE_ON = False  # Genie dropped for now: the assistant answers from its own tools only. True brings it back.

st.set_page_config(page_title="VDA Data Quality Cockpit", page_icon="🚚", layout="wide")
cfg = Config()


@st.cache_resource
def connection():
    return sql.connect(
        server_hostname=cfg.host,
        http_path=f"/sql/1.0/warehouses/{WAREHOUSE_ID}",
        credentials_provider=lambda: cfg.authenticate,
    )


def qlive(statement, **params):
    """Uncached query - for data the user changes (fix_log)."""
    with connection().cursor() as cur:
        cur.execute(statement, params or None)
        return cur.fetchall_arrow().to_pandas()


@st.cache_data(ttl=600, show_spinner=False)
def q(statement, **params):
    return qlive(statement, **params)


def execute(statement, params=None):
    with connection().cursor() as cur:
        cur.execute(statement, params or None)




ROLES = {
    "Supplier": {
        "icon": "🏭", "key": "supplier_code",
        "who": "You **make the parts** and ship them to the plant.",
        "sends": "**Transport orders** (VDA 4933 – \"please pick up my parts\") and "
                 "**delivery notes** (VDA 4987 – \"these parts are on the truck\").",
    },
    "Carrier": {
        "icon": "🚚", "key": "carrier_code",
        "who": "You **drive the trucks** from the supplier to the plant.",
        "sends": "**Transport status** updates (VDA 4945 – GPS position, truck plate, gate timeslot).",
    },
}


def load_partners(role):
    df = q(f"SELECT * FROM {T}.scores_{role.lower()} WHERE n_messages >= 5 ORDER BY n_messages DESC")
    k = ROLES[role]["key"]
    df["display_name"] = df["display_name"].fillna(df[k])
    df["label"] = (df["display_name"] + "  ·  code " + df[k] + "  ·  " + df["n_messages"].astype(int).astype(str)
                   + " messages  ·  score " + df["quality_score"].round(0).astype(int).astype(str))
    return df


def choose(role, row):
    st.session_state.role, st.session_state.code = role, row[ROLES[role]["key"]]
    st.rerun()


CARD_CSS = """
<style>
/* Role cards on the home page: the whole bordered box is the button.
   The real st.button is stretched invisibly over the card, so it stays keyboard/screen-reader friendly. */
[class*="st-key-rolecard_"] { position: relative; transition: box-shadow .15s, transform .15s; border-radius: .5rem; }
[class*="st-key-rolecard_"]:hover { box-shadow: 0 4px 14px rgba(0, 30, 80, .18); transform: translateY(-1px); }
[class*="st-key-rolebtn_"] { position: absolute; inset: -1rem; z-index: 5; margin: 0 !important; }
[class*="st-key-rolebtn_"] .stButton, [class*="st-key-rolebtn_"] button { width: 100%; height: 100%; }
[class*="st-key-rolebtn_"] button { opacity: 0; cursor: pointer; }
[class*="st-key-rolebtn_"] button:focus-visible { opacity: .12; }
</style>
"""


def go_home():
    """Back to the home page from anywhere: forget the role, the company and the search box."""
    for k_ in ("role", "code", "pick_role", "find_Supplier", "find_Carrier"):
        st.session_state.pop(k_, None)


def back_to_list(role_):
    go_home()
    st.session_state.pick_role = role_


def top_bar(*crumbs, list_role=None):
    """Header used on every page except home: breadcrumb, optional 'back to list', and the Home button."""
    c1, c2, c3 = st.columns([5, 1.3, 1], vertical_alignment="center")
    c1.markdown("  ›  ".join(["🚚 **VDA Data Quality Cockpit**", *crumbs]))
    if list_role:
        c2.button(f"← All {list_role.lower()}s", use_container_width=True, on_click=back_to_list,
                  args=(list_role,), key="list_top")
    c3.button("🏠 Home", use_container_width=True, on_click=go_home, key="home_top")
    st.divider()


def company_table(df, role, key_, height="auto", extra=None):
    """A clickable company table; clicking a row opens that company."""
    k = ROLES[role]["key"]
    cols = ["display_name", k, "quality_score", "n_messages"] + (extra or [])
    cfg_ = {
        "display_name": st.column_config.TextColumn("Company"),
        k: st.column_config.TextColumn("Code"),
        "quality_score": st.column_config.ProgressColumn("Score", min_value=0, max_value=100, format="%.0f"),
        "n_messages": st.column_config.NumberColumn("Messages", format="%d"),
        "score_matching": st.column_config.NumberColumn("🔗 Matching", format="%.0f%%"),
        "score_completeness": st.column_config.NumberColumn("📋 Complete", format="%.0f%%"),
        "score_format": st.column_config.NumberColumn("🔤 Format", format="%.0f%%"),
    }
    # a fresh key after every pick, so an old row selection never re-opens a company
    nonce = st.session_state.get("table_nonce", 0)
    ev = st.dataframe(df[cols], hide_index=True, use_container_width=True, height=height, column_config=cfg_,
                      on_select="rerun", selection_mode="single-row", key=f"{key_}_{nonce}")
    if ev.selection.rows:
        st.session_state.table_nonce = nonce + 1
        choose(role, df.iloc[ev.selection.rows[0]])


def company_sidebar(role, df):
    """Left sidebar: search + the full list of companies of this kind."""
    k = ROLES[role]["key"]
    with st.sidebar:
        st.markdown(f"### {ROLES[role]['icon']} All {role.lower()}s")
        term = st.text_input("Search", placeholder="Search name or code…", key=f"find_{role}",
                             label_visibility="collapsed")
        view = df
        if term:
            hit = (df["display_name"].str.contains(term, case=False, regex=False)
                   | df[k].astype(str).str.contains(term, case=False, regex=False))
            view = df[hit]
        view = view.sort_values("display_name")
        st.caption(f"{len(view)} of {len(df)} · click a row to open")
        company_table(view, role, f"side_{role}", height=520)


def home_page():
    st.title("🚚 VDA Data Quality Cockpit")
    st.markdown("Check the logistics data you send to **the plant**: see what is wrong and how to fix it, "
                "so your trucks drive straight through the gate.")
    st.markdown(CARD_CSS, unsafe_allow_html=True)
    for r, info in ROLES.items():
        with st.container(border=True, key=f"rolecard_{r}"):
            st.markdown(f"### {info['icon']} {r}")
            st.markdown(info["who"])
            st.markdown(f"You send: {info['sends']}")
            if st.button(f"Open {r.lower()} view", key=f"rolebtn_{r}"):
                st.session_state.pick_role = r
                st.rerun()
    st.caption("Data: generated demo data – fictional companies and messages, June 2026.")


def company_picker(role):
    info = ROLES[role]
    df = load_partners(role)
    top_bar(f"{info['icon']} {role}s")
    company_sidebar(role, df)
    st.subheader(f"{info['icon']} Top priority {role.lower()}s")
    st.caption("Lowest quality score among companies with at least 20 messages: fixing these helps the plant most. "
               "Click a row to open, or search all companies in the left panel.")
    prio = df[df["n_messages"] >= 20].sort_values("quality_score").head(10)
    company_table(prio, role, f"prio_{role}",
                  extra=["score_matching", "score_completeness", "score_format"])


def entry_screen():
    role_ = st.session_state.get("pick_role")
    if role_ is None:
        home_page()
    else:
        company_picker(role_)


# ------------------------------------------------------------------ login
def whoami():
    """Databricks Apps sends the login email in a header; empty when run outside the app.
    Laptop mode has no header: COCKPIT_LOCAL_EMAIL pretends a login (see scripts/cockpit_local.sh)."""
    try:
        email = st.context.headers.get("X-Forwarded-Email") or ""
    except Exception:
        email = ""
    return (email or os.environ.get("COCKPIT_LOCAL_EMAIL", "")).strip().lower()


EMAIL = whoami()
LOGIN = (qlive(f"SELECT * FROM {T}.cockpit_users WHERE lower(user_email) = :e", e=EMAIL)
         if EMAIL else pd.DataFrame())
IS_MAPPED, IS_ADMIN = not LOGIN.empty, (not LOGIN.empty and LOGIN.iloc[0]["role"] == "admin")

if IS_MAPPED and not IS_ADMIN:
    # login-based isolation: a mapped user always sees exactly their own company
    st.session_state.role = "Supplier" if LOGIN.iloc[0]["role"] == "supplier" else "Carrier"
    st.session_state.code = LOGIN.iloc[0]["code"]
elif EMAIL:
    st.info(f"👤 **{EMAIL}** is not mapped to a company yet – you are browsing the demo. "
            "An admin can map you in `cockpit_users` to pin your own view.")


def admin_sicht(sup, car):
    """Admin navigation: a Team button plus separate scrollable supplier and carrier lists."""
    on_team = st.session_state.get("code") is None
    if st.button("👥 Team overview", type=("primary" if on_team else "secondary"), disabled=on_team,
                 use_container_width=True):
        for k_ in ("role", "code"):
            st.session_state.pop(k_, None)
        st.rerun()
    for role_, df in (("Supplier", sup), ("Carrier", car)):
        st.markdown(f"**{ROLES[role_]['icon']} {role_}s**")
        company_table(df.sort_values("display_name"), role_, f"sicht_{role_}", height=190)


def team_screen():
    """Team-wide dashboard for admins; the sidebar switches to any single firm."""
    sup, car = load_partners("Supplier"), load_partners("Carrier")
    with st.sidebar:
        st.markdown("### 🚚 DQ Cockpit")
        with st.container(border=True):
            st.caption(f"👤 {EMAIL} · admin")
            st.markdown(f"**{LOGIN.iloc[0]['name']}**")
        admin_sicht(sup, car)
        st.caption("Pick a company to open exactly the cockpit they see.")
    top_bar("👥 Team overview")
    st.title("👥 Team overview")
    st.markdown("Every firm sending logistics data to **the plant** – supplier and carrier side combined. "
                "Pick a company in the sidebar to open any firm's cockpit.")
    n_msgs = int(sup["n_4933"].sum() + sup["n_4987"].sum() + car["n_4945"].sum())
    c = st.columns(4)
    c[0].metric("🏭 Suppliers", f"{len(sup):,}", f"avg score {sup['quality_score'].mean():.0f}")
    c[1].metric("🚚 Carriers", f"{len(car):,}", f"avg score {car['quality_score'].mean():.0f}")
    c[2].metric("📨 Messages", f"{n_msgs:,}")
    n_fix = 0
    try:
        n_fix = int(q(f"SELECT count(*) AS n FROM {T}.fix_log")["n"][0]) + \
                int(q(f"SELECT count(*) AS n FROM {T}.fix_log_carrier")["n"][0])
    except Exception:
        pass
    c[3].metric("🧾 Fix decisions", f"{n_fix:,}")

    def ranking(df, role, asc):
        k = ROLES[role]["key"]
        r = df.sort_values("quality_score", ascending=asc).head(5)
        return r.rename(columns={k: "code", "display_name": "company"})[
            ["company", "code", "n_messages", "quality_score", "score_matching",
             "score_completeness", "score_format"]]

    def clickable(df, role, key_):
        """A ranking table; clicking a row opens that company (fresh key after each pick, like company_table)."""
        nonce = st.session_state.get("table_nonce", 0)
        ev = st.dataframe(df, hide_index=True, use_container_width=True, on_select="rerun",
                          selection_mode="single-row", key=f"{key_}_{nonce}")
        if ev.selection.rows:
            st.session_state.table_nonce = nonce + 1
            open_company(role, df.iloc[ev.selection.rows[0]]["code"])

    def open_company(role, code):
        """Open a firm's cockpit if it has a page (≥ 5 messages); False otherwise."""
        df = sup if role == "Supplier" else car
        if (df[ROLES[role]["key"]] == code).any():
            st.session_state.role, st.session_state.code = role, code
            st.rerun()
        return False

    alarms_ui.team_board(q, T, open_company)

    st.caption("Click a row to open that company's cockpit.")
    l, r = st.columns(2, gap="large")
    with l.container(border=True):
        st.markdown("**🏭 Suppliers · worst 5** (fix these first)")
        clickable(ranking(sup, "Supplier", True), "Supplier", "rank_sup_worst")
        st.markdown("**⭐ best in class**")
        clickable(ranking(sup, "Supplier", False), "Supplier", "rank_sup_best")
    with r.container(border=True):
        st.markdown("**🚚 Carriers · worst 5** (fix these first)")
        clickable(ranking(car, "Carrier", True), "Carrier", "rank_car_worst")
        st.markdown("**⭐ best in class**")
        clickable(ranking(car, "Carrier", False), "Carrier", "rank_car_best")

    warnings_ui.alarm_board(q, T, open_company)

    top = q(f"""
        SELECT r.category, f.severity, r.msg_type, f.responsible, r.problem,
               count(*) AS findings, count(DISTINCT f.supplier_code) AS sup, count(DISTINCT f.carrier_code) AS car
        FROM {T}.findings f JOIN {T}.rules r USING (rule_id)
        WHERE f.severity IN ('error', 'warning')
        GROUP BY ALL ORDER BY findings DESC LIMIT 10""")
    icon = {"error": "🔴", "warning": "🟠"}
    rows = [{"": icon.get(s["severity"], ""), "issue": s["problem"], "type": s["msg_type"],
             "who": s["responsible"], "findings": int(s["findings"]),
             "firms affected": int(s["sup"] + s["car"])} for _, s in top.iterrows()]
    st.subheader("Top issues across all firms")
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

    try:
        recent = qlive(f"""
            (SELECT user_email, decision, ts, message_key FROM {T}.fix_log)
            UNION ALL (SELECT user_email, decision, ts, message_key FROM {T}.fix_log_carrier)
            ORDER BY ts DESC LIMIT 8""")
        if not recent.empty:
            st.subheader("Latest fix decisions")
            st.dataframe(recent, hide_index=True, use_container_width=True)
    except Exception:
        pass
    st.stop()


if "code" not in st.session_state:
    if IS_ADMIN:
        team_screen()
    else:
        entry_screen()
        st.stop()

role, code = st.session_state.role, st.session_state.code
key = ROLES[role]["key"]
scores = load_partners(role)
me = scores[scores[key] == code].iloc[0]

# ------------------------------------------------------------------ header
top_bar(f"{ROLES[role]['icon']} {role}s", me["display_name"], list_role=(None if IS_MAPPED else role))
if not IS_MAPPED:
    company_sidebar(role, scores)
else:
    with st.sidebar:
        with st.container(border=True):
            st.caption(f"👤 {EMAIL} · {LOGIN.iloc[0]['role']}")
            st.markdown(f"**{me['display_name']}**")
            st.caption(f"code {code}")
        if IS_ADMIN:
            admin_sicht(load_partners("Supplier"), load_partners("Carrier"))
            st.caption("Switch to the team overview or any firm.")
        st.caption(f"What you send: {ROLES[role]['sends']}")
st.title(f"{ROLES[role]['icon']} {me['display_name']}")
st.markdown("You're viewing the data this company sends to the plant: "
            + (f"**{int(me['n_4933'])} transport orders** and **{int(me['n_4987'])} delivery notes**." if role == "Supplier"
               else f"**{int(me['n_4945'])} truck status updates**."))

# real-time alarms first: what is needed in how many days so no truck waits at the gate
alarms_ui.partner_banner(q, T, role, code)

rank = (scores["quality_score"] < me["quality_score"]).mean() * 100
c = st.columns(4)
c[0].metric("Overall quality score", f"{me['quality_score']:.0f} / 100", f"better than {rank:.0f}% of {role.lower()}s",
            delta_color="off")
c[1].metric("🔗 Matching", f"{me['score_matching']:.0f}%", help="Share of your messages that can be linked into the digital transport file (TO number, delivery note, references).")
c[2].metric("📋 Completeness", f"{me['score_completeness']:.0f}%", help="Share of messages with all mandatory content (parts, quantities, timeslot, plate, GPS).")
c[3].metric("🔤 Format", f"{me['score_format']:.0f}%", help="Share of messages with well-formed values (DUNS, dates, encoding, structure).")

# top priority: prevent wrong data before it is sent, then stop what is going wrong right now
check_box.render(q, qlive, execute, T, role, code)

WHY_MATTERS = {
    "Supplier": (
        "**Why your data quality matters.** Your parts go straight from the truck onto an assembly line at "
        "the plant. Three electronic messages follow every truck: your **transport order** "
        "(VDA 4933, the booking of the pick-up), your **delivery note** (VDA 4987, what is on the truck) and the "
        "carrier's **status updates** (VDA 4945, where the truck is). The plant links all three into one **digital "
        "transport file**. If a number is missing or wrong, the file has a gap: the truck cannot be matched at "
        "the gate, someone must check it by hand, unloading is delayed – and a missing part can stop the whole "
        "line. Clean messages mean your truck drives straight through the gate and your parts reach the line "
        "on time."),
    "Carrier": (
        "**Why your data quality matters.** Your truck carries parts that an assembly line at the plant "
        "needs at a fixed minute. Your **status messages** (VDA 4945: GPS position, truck plate, "
        "gate timeslot) are what tell the plant gate that you are coming, when, and with which delivery. The plant "
        "links them with the supplier's **transport order** (VDA 4933) and **delivery note** (VDA 4987) into "
        "one **digital transport file**. If the delivery note number or order number is missing, your status "
        "cannot be matched: the gate slot is wasted, you wait at the ramp, and everything must be checked by "
        "hand. Clean messages mean your truck is expected, waved through and unloaded on time."),
}
with st.expander("❓ Why does data quality matter for you? (1 minute)"):
    st.markdown(WHY_MATTERS[role])

warnings_ui.partner_box(q, T, role, code)
st.divider()

if role == "Supplier":
    fixes.render(q, qlive, execute, T, code, me)
else:
    fixes.render_carrier(q, qlive, execute, T, code, me)
st.divider()

left = st.container()

# ------------------------------------------------------------------ problems
with left:
    missing = q(f"""
        SELECT f.msg_type, r.problem, r.field_path, r.fix, count(*) AS n, count(DISTINCT f.message_key) AS messages
        FROM {T}.findings f JOIN {T}.rules r USING (rule_id)
        WHERE f.{key} = :code AND f.responsible = :role AND f.severity IN ('error', 'warning')
        GROUP BY ALL ORDER BY f.msg_type, n DESC""", code=code, role=role.lower())
    if not missing.empty:
        st.subheader("📋 What's missing in your messages")
        TYPE_NAMES = {"4933": ("transport orders", "the pick-up booking you give the carrier"),
                      "4987": ("delivery notes", "what exactly is on the truck"),
                      "4945": ("truck statuses", "GPS, plate and gate timeslot of your truck")}
        for t_ in missing["msg_type"].unique():
            sub = missing[missing["msg_type"] == t_].head(5)
            name, what = TYPE_NAMES.get(t_, (t_, ""))
            st.markdown(f"**VDA {t_} – {name}** ({what})")
            st.dataframe(sub.rename(columns={"problem": "what is missing or wrong", "field_path": "VDA field",
                                            "n": "× found", "messages": "messages affected"})[
                             ["what is missing or wrong", "VDA field", "× found", "messages affected", "fix"]],
                         hide_index=True, use_container_width=True)
        st.divider()
    st.subheader("Your top problems")
    problems = q(f"""
        SELECT f.rule_id, r.category, r.problem, r.field_path, r.fix, r.msg_type,
               -- one row per rule: some rules are error in one context and warning in another; keep the worst
               -- (two rows for one rule would give two widgets the same key and crash the page)
               min(CASE f.severity WHEN 'error' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END) AS sev_rank,
               count(*) AS n, count(DISTINCT f.message_key) AS messages
        FROM {T}.findings f JOIN {T}.rules r USING (rule_id)
        WHERE f.{key} = :code AND f.responsible = :role
        GROUP BY ALL
        ORDER BY sev_rank, n DESC
    """, code=code, role=role.lower())
    problems["severity"] = problems["sev_rank"].map({0: "error", 1: "warning", 2: "info"})
    # which problems the suggested fixes above solve, and which need action in the company's own system
    FIXABLE_RULES = fixes.FIXABLE if role == "Supplier" else fixes.FIXABLE_CARRIER
    pend, appr, manual_n = {}, {}, {}
    try:
        fxstat = fixes.fix_status(q, T, role, code)
        pend = {r["rule_id"]: int(r["n"]) for _, r in fxstat[fxstat["kind"] == "pending"].iterrows()}
        appr = {r["rule_id"]: int(r["n"]) for _, r in fxstat[fxstat["kind"] == "approved"].iterrows()}
    except Exception:
        pass
    try:
        mstat = qlive(f"SELECT rule_id, count(*) AS n FROM {T}.fix_log_manual "
                      "WHERE role = :r AND company_code = :c GROUP BY 1", r=role.lower(), c=code)
        manual_n = {r["rule_id"]: int(r["n"]) for _, r in mstat.iterrows()}
    except Exception:
        pass
    if problems.empty:
        st.success("No problems found in your messages. 🎉")
    else:
        n_here = problems[problems["rule_id"].isin(FIXABLE_RULES)]["rule_id"].nunique()
        n_own = problems[~problems["rule_id"].isin(FIXABLE_RULES)]["rule_id"].nunique()
        st.caption(f"🔧 **{n_here} problem(s) can be fixed right here** with the suggested fixes above · "
                   f"🛠 **{n_own} need a change in your own system** – the 'How to fix' text and the "
                   "IT note tell you what to do.")

    def text_cols(df, min_px=70, max_px=420):
        """Column widths that adapt to the longest text in each column, so no field is cut off."""
        if df.empty:
            return {}
        return {c: st.column_config.TextColumn(
                    width=min(max_px, max(min_px, int(df[c].astype(str).str.len().max() * 8) + 30)))
                for c in df.columns}

    def status_line(rule_id):
        """Colored one-liner above each problem: where this problem gets solved."""
        if rule_id in FIXABLE_RULES:
            if pend.get(rule_id):
                done = f" · ✅ {appr[rule_id]:,} already fixed" if appr.get(rule_id) else ""
                return (f":blue[🔧 **Fix in the cockpit** – {pend[rule_id]:,} open suggestion(s){done} "
                        f"– decide in the 🔧 section above]")
            if appr.get(rule_id):
                return f":green[✅ {appr[rule_id]:,} already fixed via the cockpit – nothing left to decide]"
            return ":blue[🔧 Detective domain – no automatic suggestion found for this yet]"
        noted = f" · ✏️ {manual_n[rule_id]:,} correction(s) noted" if manual_n.get(rule_id) else ""
        return f":orange[🛠 **Your action** – fix this in your own system{noted}]"

    icon = {"error": "🔴", "warning": "🟠", "info": "🔵"}
    for _, p in problems.head(8).iterrows():
        st.markdown(status_line(p["rule_id"]))
        with st.expander(f"{icon.get(p['severity'], '')} **{p['problem']}** — {p['messages']} message(s) · {p['msg_type']}"):
            st.markdown(f"**How to fix:** {p['fix']}")
            ex = q(f"""
                SELECT f.msg_date AS date,
                       CASE f.msg_type WHEN '4933' THEN 'Transport order'
                                       WHEN '4987' THEN 'Delivery note'
                                       WHEN '4945' THEN 'Truck status' ELSE f.msg_type END AS document,
                       coalesce(CASE WHEN f.msg_type = '4933' THEN concat('order ', f.message_key)
                                     WHEN f.msg_type = '4987' AND f.message_key NOT LIKE '%.zip%'
                                       THEN concat('delivery note ', f.message_key)
                                END,
                                concat_ws(' · ',
                                  CASE WHEN m.to_number IS NOT NULL AND m.to_number <> 'null' AND m.to_number <> ''
                                       THEN concat('order ', m.to_number) END,
                                  CASE WHEN m.shipment_number IS NOT NULL THEN concat('delivery note ', m.shipment_number) END,
                                  CASE WHEN m.document_id IS NOT NULL THEN concat('status ', m.document_id) END,
                                  CASE WHEN m.plate IS NOT NULL THEN concat('plate ', m.plate) END),
                                concat('file ', regexp_extract(coalesce(f.source, f.message_key, ''), '[^/]+$', 0))) AS ref,
                       coalesce(f.value, '(not sent)') AS sent_value,
                       f.value AS raw_value, f.msg_type, f.message_key, f.source
                FROM {T}.findings f
                LEFT JOIN {T}.msg_4945 m ON f.msg_type = '4945'
                     AND concat(m.document_id, '#', m.shipment_index) = f.message_key
                     AND m.carrier_code = f.carrier_code
                WHERE f.{key} = :code AND f.responsible = :role AND f.rule_id = :rule
                ORDER BY f.msg_date DESC LIMIT 5""", code=code, role=role.lower(), rule=p["rule_id"])
            st.caption("Latest affected messages – in which document, which value")
            view = ex[["date", "document", "ref", "sent_value"]]
            st.dataframe(view, hide_index=True, use_container_width=True, column_config=text_cols(view))
            if p["rule_id"] not in FIXABLE_RULES and not ex.empty:
                # ✏️ note the correct value where no automatic fix exists
                ex = ex.reset_index(drop=True)
                tab = pd.DataFrame({"reference": ex["ref"], "sent": ex["sent_value"]})
                tab["correct value"] = ""
                edited = st.data_editor(tab, hide_index=True, use_container_width=True,
                                        key=f"man::{p['rule_id']}", disabled=["reference", "sent"],
                                        column_config={**text_cols(tab[["reference", "sent"]]),
                                                       "correct value": st.column_config.TextColumn(width=200)})
                filled = edited["correct value"].astype(str).str.strip().ne("")
                n_filled = int(filled.sum())
                if st.button(f"💾 Save {n_filled} correction(s)", key=f"save::{p['rule_id']}", type="primary",
                             disabled=n_filled == 0, use_container_width=True):
                    rows = [{"user_email": fixes.current_user(), "role": role.lower(), "company_code": code,
                             "rule_id": p["rule_id"], "msg_type": ex["msg_type"][i],
                             "message_key": ex["message_key"][i], "ref_number": str(ex["ref"][i]),
                             "old_value": "" if pd.isna(ex["raw_value"][i]) else str(ex["raw_value"][i]),
                             "new_value": str(edited["correct value"][i]), "source": str(ex["source"][i])}
                            for i in range(len(edited)) if filled[i]]
                    fixes.log_manual(execute, T, rows)
                    st.toast(f"{len(rows)} correction(s) saved")
                    st.rerun()
                noted_rows = qlive(f"""SELECT ref_number AS reference, old_value AS sent,
                                              new_value AS `correct value`, user_email, ts
                                       FROM {T}.fix_log_manual
                                       WHERE rule_id = :rule AND role = :r AND company_code = :code
                                       ORDER BY ts DESC LIMIT 10""", rule=p["rule_id"], r=role.lower(), code=code)
                if not noted_rows.empty:
                    st.caption(f"Already noted corrections (latest {len(noted_rows)}):")
                    st.dataframe(noted_rows, hide_index=True, use_container_width=True,
                                 column_config=text_cols(noted_rows))

    st.subheader("Trend")
    if role == "Supplier":
        msgs = f"""SELECT requested_shipment_date AS d, '4933' AS t, coalesce(to_number, workflow_id) AS k FROM {T}.msg_4933 WHERE supplier_code = :code
                   UNION ALL
                   SELECT despatch_date, '4987', coalesce(shipment_number, asn_number, source) FROM {T}.msg_4987 WHERE supplier_code = :code"""
    else:
        msgs = f"""SELECT geo_date AS d, '4945' AS t, document_id || '#' || shipment_index AS k FROM {T}.msg_4945 WHERE carrier_code = :code"""
    trend = q(f"""
        WITH m AS ({msgs}),
             f AS (SELECT DISTINCT msg_type AS t, message_key AS k FROM {T}.findings
                   WHERE {key} = :code AND responsible = :role AND severity IN ('error','warning'))
        SELECT to_date(m.d) AS day, count(*) AS messages,
               round(100 * (count(*) - count(f.k)) / count(*), 1) AS pct_clean
        FROM m LEFT JOIN f ON m.t = f.t AND m.k = f.k
        WHERE m.d IS NOT NULL GROUP BY 1 ORDER BY 1""", code=code, role=role.lower())
    if not trend.empty:
        st.caption("% of your messages per day without errors or warnings")
        st.line_chart(trend.set_index("day")["pct_clean"], height=200)

# ------------------------------------------------------------------ assistant
AGENT_MODEL = os.environ.get("AGENT_MODEL", "databricks-qwen3-next-80b-a3b-instruct")
TOOL_LABEL = {
    "get_my_overview": "your scores", "get_issue_details": "problem details", "get_message": "your message",
    "ask_statistics": "statistics (Genie)", "list_fix_suggestions": "fix suggestions",
    "explain_fix_suggestion": "evidence", "propose_fix": "fix proposal",
}


def llm(body):
    return WorkspaceClient().api_client.do("POST", f"/serving-endpoints/{AGENT_MODEL}/invocations", body=body)


def genie(question):
    """Ad-hoc statistics via the Genie space: returns (text, rows)."""
    w = WorkspaceClient()
    msg = w.genie.start_conversation_and_wait(GENIE_SPACE_ID, question)
    text, rows = [], []
    for a in msg.attachments or []:
        if a.text and a.text.content:
            text.append(a.text.content)
        if a.query:
            try:
                res = w.genie.get_message_attachment_query_result(GENIE_SPACE_ID, msg.conversation_id, msg.id, a.attachment_id)
                sr = res.statement_response
                cols_ = [c_.name for c_ in sr.manifest.schema.columns]
                rows = [dict(zip(cols_, r)) for r in (sr.result.data_array or [])]
            except Exception:
                pass
    return "\n".join(text), rows


@st.dialog("💬 Assistant", width="large")
def assistant_dialog():
    st.caption("Ask in your own words, in any language. It looks up **your** messages, explains problems "
               "and can propose fixes – you always decide.")
    chat = st.session_state.setdefault(f"agent::{role}::{code}", [])
    starters = (["What should I fix first?", "Fix my delivery notes", "Explain my score"] if role == "Supplier"
                else ["What should I fix first?", "Show me a real example", "Explain my score"])
    clicked = None
    if not chat:
        st.info("Try: *What should I fix first?* · *Show me an example of my most common error* · "
                + ("*Fix my delivery notes*" if role == "Supplier" else "*What must I change in my messages?*"))
        cols = st.columns(len(starters))
        for i, s_ in enumerate(starters):
            if cols[i].button(s_, key=f"start::{i}", use_container_width=True):
                clicked = s_
    # a question submitted via the bottom input in the previous run is answered first,
    # so the newest exchange lands inside the conversation – like a real chat history
    prompt = st.session_state.pop(f"pending::{role}::{code}", None) or clicked
    if prompt:
        window = fixes.WINDOWS.get(st.session_state.get(f"window::{code}", ""), "next_day")
        ag = Agent(q, T, role, code, me["display_name"], llm, genie if GENIE_ON and GENIE_SPACE_ID else None, window)
        history = [{"role": m["role"], "content": m["content"]} for m in chat]
        chat.append({"role": "user", "content": prompt, "id": len(chat)})
        with st.spinner("Looking into your data…"):
            try:
                res = ag.run(history, prompt)
            except Exception as e:
                res = {"text": f"⚠️ Sorry, something went wrong: {e}", "proposals": [], "trace": []}
        chat.append({"role": "assistant", "content": res["text"], "trace": res["trace"],
                     "proposals": res["proposals"], "id": len(chat)})
    for m in chat:
        with st.chat_message(m["role"]):
            st.markdown(m["content"])
            if m.get("trace"):
                used = dict.fromkeys(TOOL_LABEL.get(t["tool"], t["tool"]) for t in m["trace"])
                st.caption("🔧 looked up: " + ", ".join(used))
            for j, p in enumerate(m.get("proposals", [])):
                with st.container(border=True):
                    dot = "🟢" if p["confidence"] == "high" else "🟡"
                    st.markdown(f"{dot} **Fix proposal** · delivery note **{p['delivery_note']}**: order number "
                                f"`{p['current_value'] or 'empty'}` → **`{p['chosen']}`**")
                    if p.get("status"):
                        st.caption(f"{'✅ approved' if p['status'] == 'approved' else '❌ rejected'} – see ✅ Done above")
                    else:
                        c1, c2 = st.columns(2)
                        if c1.button("✅ Approve", key=f"pa::{m['id']}::{j}", type="primary", use_container_width=True):
                            fixes.log(execute, T, [fixes.row_for_log(p, "approved", p["chosen"])])
                            p["status"] = "approved"
                            st.toast("Fix approved – see ✅ Done in the 🔧 section")
                        if c2.button("❌ Reject", key=f"pr::{m['id']}::{j}", use_container_width=True):
                            fixes.log(execute, T, [fixes.row_for_log(p, "rejected")])
                            p["status"] = "rejected"
                            st.toast("Fix rejected")

    # the input sits at the bottom, like in a real chat; submissions are answered on the next run
    prompt = st.chat_input("Ask the assistant…", key=f"chat_in::{role}::{code}")
    if prompt:
        st.session_state[f"pending::{role}::{code}"] = prompt
        st.rerun(scope="fragment")


# floating chat bubble, bottom right: opens the assistant dialog on click
BUBBLE_CSS = """
<style>
.st-key-chat_bubble { position: fixed; bottom: 1.4rem; right: 1.4rem; z-index: 9998; width: auto !important; }
.st-key-chat_bubble .stCaption { text-align: right; margin: 0 .3rem .3rem 0; }
.st-key-chat_bubble button {
    border-radius: 9999px !important; width: 3.2rem; height: 3.2rem; font-size: 1.45rem;
    padding: 0 !important; box-shadow: 0 6px 18px rgba(0, 30, 80, .35);
}
</style>
"""
st.markdown(BUBBLE_CSS, unsafe_allow_html=True)
with st.container(key="chat_bubble"):
    st.caption("Questions?")
    if st.button("💬", key="open_assistant", type="primary", help="Ask the assistant about your data"):
        assistant_dialog()
