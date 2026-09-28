"""Who is using the app, for sharing it with a link (scripts/share.py).

- On this machine (no proxy headers): the owner, no login, exactly as before.
- Through the share tunnel: a login. APP_OWNER_PASSWORD gives full access; APP_VIEWER_PASSWORD gives
  read-only access: every page can be browsed, but nothing is edited, no screen or alert check is
  started, and the LLM lenses use cached answers only (no calls on the owner's API key or Claude
  subscription). With no passwords set, remote requests are refused.

The role lives in the session; views ask `is_owner()` before showing any control that writes or
spends. A remote visitor is recognised by the headers the tunnel adds (REMOTE_REQUEST_HEADERS).
"""

from __future__ import annotations

import hmac

import streamlit as st

import config

OWNER, VIEWER = "owner", "viewer"
ROLE_KEY = "auth_role"


def is_remote() -> bool:
    try:
        headers = {k.lower() for k in st.context.headers.keys()}
    except Exception:  # no request context (tests, scripts): local
        return False
    return any(h in headers for h in config.REMOTE_REQUEST_HEADERS)


def role() -> str | None:
    """The signed-in role; a local session is always the owner (no login on this machine)."""
    r = st.session_state.get(ROLE_KEY)
    if r is None and not is_remote():
        return OWNER
    return r


def is_owner() -> bool:
    return role() == OWNER


def _check(entered: str, expected: str) -> bool:
    return bool(expected) and hmac.compare_digest(entered.encode(), expected.encode())


def gate() -> str:
    """The session's role; shows the login and stops the page until there is one."""
    if st.session_state.get(ROLE_KEY) is not None:
        return st.session_state[ROLE_KEY]
    if not is_remote():
        st.session_state[ROLE_KEY] = OWNER
        return OWNER
    pw = config.app_passwords()
    st.title("Value Stock Analyzer")
    if not (pw["APP_OWNER_PASSWORD"] or pw["APP_VIEWER_PASSWORD"]):
        st.error("Sharing isn't set up on this app (no passwords configured), so remote access is refused.")
        st.stop()
    with st.form("login"):
        entered = st.text_input("Password", type="password")
        if st.form_submit_button("Sign in"):
            if _check(entered, pw["APP_OWNER_PASSWORD"]):
                st.session_state[ROLE_KEY] = OWNER
            elif _check(entered, pw["APP_VIEWER_PASSWORD"]):
                st.session_state[ROLE_KEY] = VIEWER
            else:
                st.error("Wrong password.")
    if st.session_state.get(ROLE_KEY) is None:
        st.caption("Personal research tool, shared read-only for feedback. Not investment advice.")
        st.stop()
    st.rerun()


def sign_out() -> None:
    st.session_state.pop(ROLE_KEY, None)
