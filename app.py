import html
import time
from pathlib import Path

import pandas as pd
import streamlit as st

import ai_review
import chatgpt_auth
import reviewer

st.set_page_config(page_title="ServiceNow User Guide Reviewer", page_icon="📘", layout="wide")

st.markdown(
    """
    <style>
    table.issues {width:100%; border-collapse:collapse; font-size:0.9rem;}
    table.issues th, table.issues td {border:1px solid rgba(128,128,128,.3); padding:6px 8px; vertical-align:top;}
    table.issues th {background:rgba(128,128,128,.12); text-align:left;}
    .sev-High {color:#c62828; font-weight:600;}
    .sev-Medium {color:#ef6c00; font-weight:600;}
    .sev-Low {color:#2e7d32; font-weight:600;}
    [dir=auto] {unicode-bidi: plaintext;}
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("ServiceNow User Guide Reviewer")
st.caption("Upload a ServiceNow user guide (English, Arabic, or both). Built-in checks run on this server with no "
           "API keys or paid services. Your document is processed in memory and never stored or modified.")


def running_locally():
    """Sign in with ChatGPT plan usage is only supported for locally hosted apps (127.0.0.1 callback)."""
    if Path.cwd().as_posix().startswith("/mount/src"):  # Streamlit Community Cloud
        return False
    host = (st.context.headers.get("Host") or "").split(":")[0]
    return host in ("localhost", "127.0.0.1", "[::1]")


@st.dialog("You're using your ChatGPT plan")
def welcome():
    st.write("Eligible usage in this app uses your ChatGPT plan. Manage usage in your "
             f"[ChatGPT settings]({chatgpt_auth.USAGE_URL}).")
    if st.button("Got it", type="primary"):
        chatgpt_auth.mark_welcomed()
        st.rerun()


@st.fragment(run_every=2)
def wait_for_sign_in():
    status = chatgpt_auth.sign_in_status()
    if status is None:
        st.info("Waiting for you to finish signing in to ChatGPT in your browser...")
        st.markdown(f"Browser did not open? [Open the sign-in page]({st.session_state.auth_url})")
        if st.button("Cancel sign-in"):
            chatgpt_auth.cancel_sign_in()
            st.session_state.pop("auth_url", None)
            st.rerun()
        return
    st.session_state.pop("auth_url", None)
    chatgpt_auth.cancel_sign_in()
    if status != "ok":
        st.session_state.auth_error = status
    st.rerun()


ai_engine = None  # ("chatgpt" | "free", model)
if ai_review.free_api_key():
    with st.container(border=True):
        st.markdown("**Free AI review (optional)**: an open-source model adds context-aware analysis on top of the "
                    "built-in checks. It's free, but limited to roughly 10–15 guides per day, and long guides take "
                    "a few minutes.")
        free_model = st.selectbox("Model", list(ai_review.FREE_MODELS), format_func=ai_review.FREE_MODELS.get,
                                  key="free_model")
        if st.checkbox("I authorize sending this document's text to Groq (the free AI service) for review.",
                       key="free_consent"):
            ai_engine = ("free", free_model)
        st.caption("AI findings are checked automatically: quotes that don't exist in the document are discarded, and "
                   "unverified ServiceNow claims are marked *Needs Manual Review*. Always review the results.")

if running_locally():
    with st.container(border=True):
        st.markdown("**ChatGPT review (optional)**: adds context-aware analysis on top of the built-in checks.")
        account = chatgpt_auth.load_account()
        if st.session_state.get("auth_error"):
            st.error(f"Sign-in failed: {st.session_state.pop('auth_error')}")
        if st.session_state.get("auth_url"):
            wait_for_sign_in()
        elif chatgpt_auth.is_signed_in():
            if not account.get("welcomed"):
                welcome()
            try:
                if "models" not in st.session_state:
                    st.session_state.models = chatgpt_auth.list_models()
                models = st.session_state.models
            except chatgpt_auth.AuthError as e:
                models = []
                st.error(str(e))
            c1, c2 = st.columns([3, 1])
            c1.markdown(f"Signed in as **{html.escape(account.get('email', 'ChatGPT user'))}** · Using ChatGPT plan · "
                        f"[Manage usage]({chatgpt_auth.USAGE_URL})")
            if c2.button("Sign out"):
                confirmed = chatgpt_auth.sign_out()
                st.session_state.pop("models", None)
                if not confirmed:
                    st.session_state.auth_error = ("signed out on this computer, but remote revocation was not "
                                                   "confirmed. You can disconnect the app in ChatGPT Settings.")
                st.rerun()
            if models:
                labels = {slug: name for slug, name in models}
                chosen = st.selectbox("Model", list(labels), format_func=labels.get)
                if st.checkbox("I authorize sending this document's text to OpenAI for review using my ChatGPT plan."):
                    ai_engine = ("chatgpt", chosen)  # takes priority over the free AI if both are ticked
        else:
            if account.get("client_id") and account.get("email"):
                st.caption(f"Previously connected: {account['email']}")
            st.write("Use your ChatGPT plan to review documents with ChatGPT.")
            if st.button("Continue with ChatGPT"):
                st.session_state.auth_url = chatgpt_auth.start_sign_in()
                st.rerun()
elif not ai_review.free_api_key():
    st.info("This online version uses the built-in checks. AI review is available when the app runs on your own "
            "computer (ChatGPT) or when a free AI key is configured.")

uploaded = st.file_uploader("Upload a PDF or DOCX file", type=["pdf", "docx"])

if uploaded and st.button("Review Document", type="primary"):
    ai_errors, ai_notes = [], []

    def ai(paras, rule_issues):
        engine, model = ai_engine
        name = "ChatGPT" if engine == "chatgpt" else "The free AI"
        bar = st.progress(0.0, text=f"{name} is reviewing the document...")
        bar_value = [0.0]

        def step(n, total):
            bar_value[0] = (n - 1) / total
            bar.progress(bar_value[0], text=f"{name}: part {n} of {total}...")

        def wait(seconds):  # free tier: pause until the per-minute limit resets
            bar.progress(bar_value[0], text=f"Waiting {seconds:.0f}s for the free AI's per-minute limit...")
            time.sleep(seconds)
        try:
            if engine == "chatgpt":
                return ai_review.review(paras, rule_issues, model, step)
            found, note = ai_review.review_free(paras, rule_issues, model, step, wait)
            if note:
                ai_notes.append(note)
            return found
        except (ai_review.AIReviewError, chatgpt_auth.AuthError) as e:
            ai_errors.append(e)
            return []
        finally:
            bar.empty()

    with st.spinner("Reviewing document..."):
        try:
            st.session_state.result = reviewer.review_document(uploaded.name, uploaded.getvalue(),
                                                               ai if ai_engine else None)
            st.session_state.filename = uploaded.name
            st.session_state.result["ai_used"] = bool(ai_engine) and not ai_errors
        except Exception as e:  # show a readable error instead of a stack trace
            st.error(f"Review failed: {e}")
    if "result" in st.session_state:
        st.session_state.result["limitations"] = st.session_state.result["limitations"] + ai_notes
    for e in ai_errors:
        st.error(f"AI review was skipped: {e} Showing built-in check results only.")
        if getattr(e, "usage_limit", False) and ai_engine[0] == "chatgpt":
            st.markdown("**Usage limit reached.** Review your plan or this app's limit in ChatGPT settings: "
                        f"[Manage usage]({chatgpt_auth.USAGE_URL})")

result = st.session_state.get("result")
if result:
    issues = result["issues"]
    filename = st.session_state.filename

    st.subheader("Review Summary")
    cols = st.columns(len(reviewer.ISSUE_TYPES) + 1)
    cols[0].metric("Total issues", len(issues))
    for col, t in zip(cols[1:], reviewer.ISSUE_TYPES):
        col.metric(t, sum(1 for i in issues if i["issue_type"] == t))
    st.write(
        " · ".join(f"**{s}:** {sum(1 for i in issues if i['status'] == s)}"
                   for s in reviewer.STATUSES)
        + (f" · **Found by AI:** {sum(1 for i in issues if i.get('engine', 'Rules') != 'Rules')}"
           if result.get("ai_used") else "")
    )

    for note in result["limitations"]:
        st.warning(note)

    if not issues:
        st.success("No issues were found.")
    else:
        st.subheader("Identified Issues")
        f1, f2, f3 = st.columns(3)
        types = f1.multiselect("Issue type", reviewer.ISSUE_TYPES, default=reviewer.ISSUE_TYPES)
        sevs = f2.multiselect("Severity", reviewer.SEVERITIES, default=reviewer.SEVERITIES)
        statuses = f3.multiselect("Status", reviewer.STATUSES, default=reviewer.STATUSES)
        shown = [i for i in issues if i["issue_type"] in types and i["severity"] in sevs and i["status"] in statuses]

        def cell(value):
            return f'<td dir="auto">{html.escape(str(value or ""))}</td>'

        def source_cell(source):
            href = (source or "").split(" ")[0]
            if ai_review.OFFICIAL_SOURCE.match(href):
                return f'<td><a href="{html.escape(href)}" target="_blank">{html.escape(source)}</a></td>'
            return cell(source)

        rows = "".join(
            "<tr>" + cell(n) + cell(i["location"]) + cell(i["issue_type"]) + cell(i["original_text"])
            + cell(i["suggested_correction"]) + cell(i["explanation"])
            + f'<td class="sev-{html.escape(i["severity"])}">{html.escape(i["severity"])}</td>'
            + cell(i["status"]) + cell(i.get("engine", "Rules")) + source_cell(i.get("source")) + "</tr>"
            for n, i in enumerate(shown, start=1)
        )
        st.markdown(
            '<table class="issues"><tr><th>#</th><th>Location</th><th>Issue Type</th><th>Original Text</th>'
            "<th>Suggested Correction</th><th>Explanation</th><th>Severity</th><th>Status</th><th>Found by</th><th>Source</th></tr>"
            f"{rows}</table>",
            unsafe_allow_html=True,
        )

    st.subheader("Download Report")
    base = filename.rsplit(".", 1)[0]
    d1, d2 = st.columns(2)
    d1.download_button("Download report (Markdown)", reviewer.report_markdown(filename, result).encode("utf-8"),
                       file_name=f"{base}_review.md", mime="text/markdown")
    csv = pd.DataFrame(issues, columns=["location", "issue_type", "original_text", "suggested_correction",
                                        "explanation", "severity", "status", "engine", "source"]).to_csv(index=False)
    # UTF-8 BOM so Excel displays Arabic correctly
    d2.download_button("Download report (CSV)", ("﻿" + csv).encode("utf-8"),
                       file_name=f"{base}_review.csv", mime="text/csv")
