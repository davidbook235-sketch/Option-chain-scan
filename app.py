import os
import streamlit as st
from streamlit_autorefresh import st_autorefresh

# Streamlit Cloud secrets -> env vars
for k in ["ANGEL_API_KEY", "ANGEL_CLIENT_ID", "ANGEL_PIN", "ANGEL_TOTP_SECRET", "TG_TOKEN", "TG_CHAT_ID"]:
    if k in st.secrets:
        os.environ[k] = st.secrets[k]

import scanner as sc

st.set_page_config(page_title="Nifty Option Scalper", layout="centered")
st.title("📈 Nifty Option Scalper")

auto = st.toggle("Auto-scan every 60s", value=False)
tg = st.toggle("Send Telegram alerts", value=True)
if auto:
    st_autorefresh(interval=60_000, key="r")
sc.CFG["min_score"] = st.slider("Min score", 3, 8, 5)

run = st.button("🔍 Scan now", use_container_width=True) or auto

if run:
    try:
        s = sc.run_once(send=tg, force=True)
    except Exception as e:
        st.error(f"Error: {e}")
        st.stop()

    if s["side"]:
        color = "green" if s["side"] == "CALL" else "red"
        st.markdown(f"## :{color}[{s['side']} BUY] — {s['symbol']}")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Entry", s["entry"])
        c2.metric("SL", s["sl"])
        c3.metric("T1", s["t1"])
        c4.metric("T2", s["t2"])
    else:
        st.markdown("## ⚪ NO TRADE")
        st.caption("Signal clear nahi hai, wait karo.")

    st.write(f"**Score** CALL {s['bull']} | PUT {s['bear']}  (min {sc.CFG['min_score']})")
    c1, c2, c3 = st.columns(3)
    c1.metric("Spot", f"{s['spot']:.0f}")
    c2.metric("PCR", s["pcr"])
    c3.metric("VIX", s["vix"])
    st.write(f"Support **{s['support']}** | Resistance **{s['resistance']}** | RSI {s['rsi']} | ATR {s['atr']}")
    for w in s["warns"]:
        st.warning(w)
    with st.expander("Factors"):
        for sd, pts, t in s["why"]:
            st.write(f"{'🟢' if sd == 'CALL' else '🔴'} +{pts} {sd}: {t}")
    st.caption(f"Expiry {s['expiry']} | ATM {s['atm']} | Scan {s['time']} IST")
else:
    st.info("Scan now dabao.")
  
