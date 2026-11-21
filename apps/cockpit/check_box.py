"""'🔍 Check a message': paste or upload one VDA message and see instantly what is wrong (nothing is stored,
except an Approve/Reject decision the user clicks, which goes to fix_log like everywhere else)."""
import json

import streamlit as st

import fixes
import instant

VERDICT = {"ok": "🟢 Looks good", "warning": "🟠 Has warnings", "error": "🔴 Has errors"}
SEVERITY_ICON = {"error": "🔴", "warning": "🟠", "info": "🔵"}


def _stored_suggestion(q, T, supplier_code, delivery_note, window):
    """The fix_suggestions row for this delivery note, needed to write a proper fix_log row (None if brand-new)."""
    rows = q(f"""SELECT * FROM {T}.fix_suggestions WHERE supplier_code = :c AND delivery_note = :dn
                 AND date_window = :w""", c=supplier_code, dn=delivery_note, w=window)
    return None if rows.empty else rows.iloc[0]


def _suggestion(res, q, qlive, execute, T, role, code, kp="chk"):
    s_ = res["suggestion"]
    sender = res["sender"]
    with st.container(border=True):
        st.markdown(f"💡 Suggested booking number **{s_['suggested_value']}** "
                    f"({'🟢 sure' if s_.get('confidence') == 'high' else '🟡 please check'})")
        st.caption(s_.get("reason", ""))

        # same rule as the assistant: only the logged-in company may decide about its own messages
        if str(sender.get("code")) != str(code) or sender.get("role") != role.lower():
            st.info(f"This message belongs to another company ({sender.get('name')}, code {sender.get('code')}). "
                    "Only they can approve the fix.")
            return
        window = fixes.WINDOWS.get(st.session_state.get(f"window::{code}", ""), "next_day")
        row = _stored_suggestion(q, T, code, res["summary"].get("delivery_note", ""), window)
        if row is None:
            st.caption("This delivery note is not in the plant's data yet, so the suggestion is shown for information only.")
            return
        dec = fixes.latest_decisions(qlive, T, code)
        done = dec[dec["message_key"] == row["message_key"]]
        if not done.empty:
            d = done.iloc[0]
            what = f"approved with booking **{d['new_value']}**" if d["decision"] == "approved" else "rejected"
            st.success(f"Already decided: {what}. Change it under 🔧 Suggested fixes → ✅ Done.")
            return
        c1, c2, _ = st.columns([1, 1, 2])
        if c1.button("✅ Approve", type="primary", use_container_width=True, key=f"{kp}_approve"):
            fixes.log(execute, T, [fixes.row_for_log(row, "approved", s_["suggested_value"])])
            st.toast(f"Approved: delivery note {row['delivery_note']} → booking {s_['suggested_value']}")
            st.rerun()  # full rerun: fixes section, score and gate list update too
        if c2.button("❌ Reject", use_container_width=True, key=f"{kp}_reject"):
            fixes.log(execute, T, [fixes.row_for_log(row, "rejected")])
            st.toast(f"Rejected the suggestion for delivery note {row['delivery_note']}")
            st.rerun()


def _result(res, q, qlive, execute, T, role, code, kp="chk"):
    if res.get("error"):
        st.error(f"Could not check this message: {res['error']}")
        return
    sender, summary = res["sender"], res["summary"]
    st.markdown(f"#### {VERDICT.get(res['verdict'], res['verdict'])} · {res['type']}")
    st.markdown(f"Sent by **{sender.get('name')}** (code {sender.get('code')})")
    st.caption(" · ".join(f"{k.replace('_', ' ')}: **{v if v not in ('', None) else '(empty)'}**"
                          for k, v in summary.items()))
    if not res["findings"]:
        st.success("No problems found in this message. 🎉")
    for f in res["findings"]:
        with st.container(border=True):
            st.markdown(f"{SEVERITY_ICON.get(f['severity'], '')} **{f['plain_title']}**")
            st.markdown(f"**Who fixes it:** {f['who_fixes']}  \n{f['impact']}")
            with st.expander("Technical details"):
                st.markdown(f"Field `{f['field_path']}` · rule `{f['rule_id']}`"
                            + (f" · sent value `{f['value']}`" if f.get("value") else ""))
                st.markdown(f"**How to fix:** {f['fix']}")
    if res.get("suggestion"):
        _suggestion(res, q, qlive, execute, T, role, code, kp)


@st.fragment
def render(q, qlive, execute, T, role, code):
    """The whole box. A fragment, so checking a message does not reload the rest of the page."""
    key = f"check::{code}"
    # collapsed to save space; stays open while a result is shown
    with st.expander("🛡 **Check before you send** – test one message before it reaches the plant",
                     expanded=bool(st.session_state.get(key))):
        st.caption("Stop wrong data before it reaches the plant: paste one VDA message (JSON) or upload a .json file "
                   "**before you send it**, and see instantly what the plant would find in it – the same checks as below. "
                   "Nothing is stored.")
        c1, c2 = st.columns([3, 2])
        text = c1.text_area("Message text", height=130, placeholder='{ "messageType": … }',
                            label_visibility="collapsed", key=f"chk_text::{code}")
        up = c2.file_uploader("…or upload a .json file", type=["json"], key=f"chk_file::{code}")
        b1, b2, _ = st.columns([1, 1, 3])
        if b1.button("Check", type="primary", use_container_width=True, key=f"chk_go::{code}"):
            msg = up.getvalue().decode("utf-8", errors="replace") if up is not None else text
            if not msg.strip():
                st.warning("Paste a message or choose a file first.")
            else:
                with st.spinner("Checking…"):
                    try:
                        st.session_state[key] = instant.check(msg, q, T)
                    except Exception as e:
                        st.session_state[key] = {"error": str(e)}
        if st.session_state.get(key) and b2.button("Clear", use_container_width=True, key=f"chk_clear::{code}"):
            st.session_state.pop(key, None)
            st.rerun(scope="fragment")
        if st.session_state.get(key):
            _result(st.session_state[key], q, qlive, execute, T, role, code)


AAO = "rff_AAO_transportOrderNumber"


def _pretty(raw):
    try:
        return json.dumps(json.loads(raw), indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        return raw or ""


def _fill_booking(text_key, value):
    """on_click: write the booking number into RFF+AAO of the message in the text box."""
    try:
        d = json.loads(st.session_state[text_key])
        d.setdefault("references", {})[AAO] = {"qualifier": "AAO", "value": value}
        st.session_state[text_key] = json.dumps(d, indent=2, ensure_ascii=False)
    except (ValueError, AttributeError):
        pass


def try_fix(q, qlive, execute, T, role, code, r):
    """'✏️ Try a fix' inside a delivery note's detail: its original message, editable, checked on demand.
    Below it, the upload for a message from the supplier's own system."""
    k = r["message_key"]
    text_key, res_key = f"tf_text::{code}::{k}", f"tf_res::{code}::{k}"
    st.markdown("#### ✏️ Try a fix")
    if text_key not in st.session_state:
        raw = q(f"""SELECT raw_json FROM {T}.msg_raw_4987 WHERE supplier_code = :c AND message_key = :k
                    AND source = :s LIMIT 1""", c=code, k=k, s=r["source"])     # one row only: up to 150 KB
        st.session_state[text_key] = _pretty(raw.iloc[0]["raw_json"]) if not raw.empty else ""
    if not st.session_state[text_key]:
        st.caption("We don't have the original text of this delivery note.")
    else:
        st.caption("This is the delivery note exactly as the plant received it. Change it (for example fill in the booking "
                   f"number in `references.{AAO}`) and press Check to see what the plant would find. Nothing is sent or stored.")
        st.text_area("Delivery note (JSON)", key=text_key, height=260, label_visibility="collapsed")
        b1, b2, b3, _ = st.columns([1, 2, 1, 2])
        if b1.button("Check", type="primary", use_container_width=True, key=f"tf_go::{k}"):
            with st.spinner("Checking…"):
                try:
                    st.session_state[res_key] = instant.check(st.session_state[text_key], q, T)
                except Exception as e:
                    st.session_state[res_key] = {"error": str(e)}
        booking = r["booking"] if r["status"].startswith(("🟡", "🟢 linked by")) else ""
        if booking:
            b2.button(f"✍️ Fill in booking {booking}", use_container_width=True, key=f"tf_fill::{k}",
                      on_click=_fill_booking, args=(text_key, booking))
        if b3.button("↺ Original", use_container_width=True, key=f"tf_reset::{k}"):
            st.session_state.pop(text_key, None)
            st.session_state.pop(res_key, None)
            st.rerun()
        if st.session_state.get(res_key):
            _result(st.session_state[res_key], q, qlive, execute, T, role, code, kp=f"tf::{k}")

    # a message from the supplier's own system (e.g. after their IT changed the mapping)
    with st.expander("…or check a message from your own system (.json)"):
        up = st.file_uploader("Message file", type=["json"], key=f"tf_file::{code}", label_visibility="collapsed")
        if up is not None and st.button("Check this file", key=f"tf_file_go::{code}"):
            with st.spinner("Checking…"):
                try:
                    st.session_state[f"tf_fileres::{code}"] = instant.check(
                        up.getvalue().decode("utf-8", errors="replace"), q, T)
                except Exception as e:
                    st.session_state[f"tf_fileres::{code}"] = {"error": str(e)}
        if up is not None and st.session_state.get(f"tf_fileres::{code}"):
            _result(st.session_state[f"tf_fileres::{code}"], q, qlive, execute, T, role, code, kp="tffile")
