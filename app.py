# GeM Bid Intelligence Dashboard — Production Build
# ==============================================================================
import os
import re
import json
import time
import tempfile
import datetime
import requests
import pandas as pd
import streamlit as st
import altair as alt
from pypdf import PdfReader

st.set_page_config(page_title="GeM Bid Intelligence", layout="wide")
OUTPUT_CSV = "gem_master_bids.csv"

# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------
def pat(pattern, text, default="N/A"):
    m = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
    return m.group(1).strip() if m else default

def to_num(val):
    try:
        return float(re.sub(r'[^\d.]', '', str(val)) or 0)
    except:
        return 0.0

def fmt_inr(val):
    v = to_num(val)
    if v >= 1e7: return f"₹{v/1e7:.2f} Cr"
    if v >= 1e5: return f"₹{v/1e5:.2f} L"
    return f"₹{v:,.0f}"

def fmt_lakh(val):
    """Display eligibility turnover in lakhs without losing the source-scale cue."""
    if val is None or str(val).strip().lower() in {"", "n/a", "nan", "none"}:
        return "N/A"
    try:
        return f"₹{float(to_num(val)):,.1f} L"
    except Exception:
        return "N/A"

def fmt_years(val):
    """Display experience consistently as years, including one decimal place."""
    if val is None or str(val).strip().lower() in {"", "n/a", "nan", "none"}:
        return "N/A"
    try:
        return f"{float(to_num(val)):,.1f} Yrs"
    except Exception:
        return "N/A"

# ---------------------------------------------------------------------------
# DOCUMENT DOWNLOADER — cached, resilent
# ---------------------------------------------------------------------------
@st.cache_data(show_spinner=False, ttl=600)
def download_pdf_text(url):
    if not url or not url.startswith("http"):
        return ""
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        res = requests.get(url, timeout=20, stream=True, headers=headers)
        if res.status_code != 200:
            return ""
        if 'text/html' in res.headers.get('Content-Type', ''):
            return ""
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            sz = 0
            for chunk in res.iter_content(8192):
                tmp.write(chunk)
                sz += len(chunk)
                if sz > 5 * 1024 * 1024:
                    break
            tmp_path = tmp.name
        try:
            reader = PdfReader(tmp_path)
            txt = ""
            for i in range(min(8, len(reader.pages))):
                txt += (reader.pages[i].extract_text() or "") + " "
            os.remove(tmp_path)
            return re.sub(r'\s+', ' ', txt).strip()
        except:
            if os.path.exists(tmp_path): os.remove(tmp_path)
    except:
        pass
    return ""

# ---------------------------------------------------------------------------
# PDF READER
# ---------------------------------------------------------------------------
def read_pdf(f):
    text, links = "", []
    try:
        r = PdfReader(f)
        for page in r.pages:
            text += (page.extract_text() or "") + "\n"
            if "/Annots" in page:
                for annot in page["/Annots"]:
                    obj = annot.get_object()
                    if "/A" in obj and "/URI" in obj["/A"]:
                        links.append(obj["/A"]["/URI"])
    except Exception as e:
        st.error(f"PDF Read Error: {e}")
    return text, list(set(links))

# ---------------------------------------------------------------------------
# INTELLIGENT SENTENCE EXTRACTOR FOR HYPERLINKS
# ---------------------------------------------------------------------------
def extract_key_sentences(text, max_sentences=5):
    keywords = [
        "msme", "mse", "startup", "exemption", "relaxation", "turnover",
        "experience", "qualifying", "emd", "net worth", "liquid assets",
        "oem", "authorization", "penalty", "ld ", "liquidated damages",
        "sla", "uptime", "payment", "milestone", "eligibility",
        "pre-qualification", "earnest money", "epbg", "performance"
    ]
    sentences = re.split(r'(?<=[.!?\n])\s+', text)
    found = []
    for s in sentences:
        s = s.strip()
        if len(s) > 25 and any(kw in s.lower() for kw in keywords):
            found.append(s)
            if len(found) >= max_sentences:
                break
    return " | ".join(found) if found else ""

# ---------------------------------------------------------------------------
# MASTER EXTRACTOR — extracts ALL important GeM fields
# ---------------------------------------------------------------------------
def extract_bid(text, links):
    # Strip Hindi first
    ct = re.sub(r'[\u0900-\u097F]+', '', re.sub(r'\s+', ' ', text))

    d = {}

    # ---- CORE IDENTIFIERS ----
    d["Bid Number"]       = pat(r"(GEM/\d{4}/[A-Za-z]+/?\d+)", ct)
    d["Bid End Date"]     = pat(r"Bid End Date/?Time\s*(\d{2}-\d{2}-\d{4}\s*\d{2}:\d{2}:\d{2})", ct)
    d["Bid Opening Date"] = pat(r"Bid Opening Date/?Time\s*(\d{2}-\d{2}-\d{4}\s*\d{2}:\d{2}:\d{2})", ct)
    d["Bid Validity"]     = pat(r"Bid Offer Validity.*?(\d+\s*\(?Days?\)?)", ct)

    # ---- ORG DETAILS ----
    d["Ministry"]         = pat(r"Ministry/?State Name\s*(.*?)(?=Department|$)", ct)
    d["Department"]       = pat(r"Department Name\s*(.*?)(?=Organisation|Office|$)", ct)
    d["Organisation"]     = pat(r"Organisation Name\s*(.*?)(?=Office Name|$)", ct)
    d["Office"]           = pat(r"Office Name\s*(.*?)(?=Grievance|Contact|$)", ct)

    # ---- BID CORE ----
    d["Item Category"]    = pat(r"Item Category\s*(.*?)(?=Contract|$)", ct)
    d["Contract Period"]  = pat(r"Contract Period\s*(.*?)(?=Bidder|Minimum|$)", ct)
    d["Bid Type"]         = pat(r"Type of Bid\s*(.*?)(?=Time allowed|Payment|$)", ct)
    d["Evaluation Method"]= pat(r"Evaluation Method\s*(.*?)(?=Financial|Price|$)", ct)
    d["Payment Terms"]    = pat(r"Payment.*?(?:Timelines?|Terms?)\s*(.*?)(?=Evaluation|Price|$)", ct)

    # ---- FINANCIAL ----
    d["Estimated Bid Value"] = pat(r"Estimated Bid Value.*?INR\s*([\d,]+)", ct, "0")
    d["EMD Amount"]       = pat(r"EMD.*?Amount\s*(?:INR)?\s*([\d,]+)", ct, "0")
    d["ePBG %"]           = pat(r"ePBG.*?Percentage.*?(\d+\.?\d*)", ct)
    d["ePBG Duration"]    = pat(r"ePBG.*?Duration.*?(\d+)\s*(?:Months?)?", ct)

    # ---- ELIGIBILITY ----
    turnover_match = re.search(r"Minimum Average Annual Turnover.*?(\d[\d,.]*)\s*(Lakh|Lac|Cr)", ct, re.IGNORECASE | re.DOTALL)
    if turnover_match:
        turnover_value = float(turnover_match.group(1).replace(",", ""))
        if turnover_match.group(2).lower() == "cr":
            turnover_value *= 100
        d["Min Turnover (Lakhs)"] = f"{turnover_value:g}"
    else:
        d["Min Turnover (Lakhs)"] = "N/A"
    d["Min Experience (Yrs)"]  = pat(r"Years of Past Experience.*?(\d+(?:\.\d+)?)\s*Year", ct)
    d["Docs Required"]        = pat(r"Document required from seller\s*(.*?)(?=\*In case|$)", ct)

    # ---- MSE / STARTUP RELAXATIONS (EXACT QUOTES) ----
    d["MSE Turnover Relaxation"]     = pat(r"(MSE Relaxation for Turnover\s*(?:Yes|No)(?:\s*\|.*?(?:value|lakhs|lakh).*?\d[\d,]*(?:\s*\(in lakhs\))?)?)", ct)
    d["Startup Turnover Relaxation"] = pat(r"(Startup Relaxation for Turnover\s*(?:Yes|No)(?:\s*\|.*?(?:value|lakhs|lakh).*?\d[\d,]*(?:\s*\(in lakhs\))?)?)", ct)
    d["MSE Experience Exemption"]    = pat(r"((?:MSE|Startup)\s*(?:Exemption|Relaxation)\s*(?:for)?\s*(?:Years of)?\s*Experience.*?(?:Yes|No))", ct)

    # ---- TECHNICAL ----
    d["Tech Qualifying Marks"] = pat(r"(?:Minimum Qualifying Marks|Passing Technical Marks)\s*:?\s*(\d+)", ct)
    d["EMD Exemption"]         = pat(r"(EMD\s*(?:Fee)?\s*Exemption\s*(?:Allowed)?\s*:?\s*(?:Yes|No))", ct)

    # ---- BUYER CONTACT ----
    d["Buyer Email"] = pat(r"Buyer Email.*?([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)", ct)

    # ---- DOWNLOAD HYPERLINKED ATTACHMENTS ----
    atc_keywords = ["Scope of Work", "Payment Terms", "Pre-Qualification", "Buyer Specific ATC",
                     "Buyer Added Bid Specific Terms", "Consignee", "Special Terms"]
    ref_names = []
    for kw in atc_keywords:
        ref = pat(r"" + kw + r".*?([A-Za-z0-9_-]+\.pdf)", ct, None)
        if ref:
            ref_names.append(ref)

    attachment_full_text = ""
    hyperlink_summaries = {}
    for ref in ref_names:
        target_url = next((lnk for lnk in links if ref in lnk), None)
        
        if target_url:
            txt = download_pdf_text(target_url)
            if txt:
                attachment_full_text += " " + txt
                key_sents = extract_key_sentences(txt)
                hyperlink_summaries[ref] = {
                    "url": target_url,
                    "summary": key_sents if key_sents else "No critical clauses found in first 8 pages."
                }
            else:
                hyperlink_summaries[ref] = {
                    "url": target_url or "",
                    "summary": "Could not download from GeM server."
                }

    d["Hyperlinks_JSON"] = json.dumps(hyperlink_summaries)

    # ---- BUILD DECISION CARD from bid text + attachment text ----
    full = ct + " " + attachment_full_text
    d["Decision_Card"] = json.dumps(build_decision_card(full, d))

    return d

# ---------------------------------------------------------------------------
# DECISION CARD BUILDER — the core analysis engine
# ---------------------------------------------------------------------------
def build_decision_card(full_text, bid_data):
    """Builds the 'Should I Bid?' card with REAL extracted values only."""

    def find_exact(pattern):
        m = re.search(pattern, full_text, re.IGNORECASE)
        return m.group(1).strip() if m else None

    card = {}

    # --- Turnover ---
    mse_t = find_exact(r"(MSE Relaxation for Turnover\s*(?:Yes|No)(?:\s*\|.*?(?:value|lakhs|lakh).*?\d[\d,]*(?:\s*\(in lakhs\))?)?)")
    startup_t = find_exact(r"(Startup Relaxation for Turnover\s*(?:Yes|No)(?:\s*\|.*?(?:value|lakhs|lakh).*?\d[\d,]*(?:\s*\(in lakhs\))?)?)")
    min_t = bid_data.get("Min Turnover (Lakhs)", "N/A")
    if mse_t:
        card["Turnover"] = {"status": "✅" if "Yes" in mse_t else "❌", "details": mse_t, "min": fmt_lakh(min_t)}
    else:
        card["Turnover"] = {"status": "❓", "details": "Not mentioned", "min": fmt_lakh(min_t)}

    # --- Startup Turnover ---
    if startup_t:
        card["Startup Relaxation"] = {"status": "✅" if "Yes" in startup_t else "❌", "details": startup_t}
    else:
        card["Startup Relaxation"] = {"status": "❓", "details": "Not mentioned"}

    # --- Experience ---
    mse_exp = find_exact(r"((?:MSE|Startup)\s*(?:Exemption|Relaxation)\s*(?:for)?\s*(?:Years of)?\s*Experience.*?(?:Yes|No))")
    min_exp = bid_data.get("Min Experience (Yrs)", "N/A")
    if mse_exp:
        card["Bidder Experience"] = {"status": "✅" if "Yes" in mse_exp else "❌", "details": mse_exp, "min": fmt_years(min_exp)}
    else:
        card["Bidder Experience"] = {"status": "❓", "details": "Not mentioned", "min": fmt_years(min_exp)}

    # --- EMD ---
    emd_val = bid_data.get("EMD Amount", "0")
    emd_exempt = find_exact(r"(EMD\s*(?:Fee)?\s*Exemption\s*(?:Allowed)?\s*:?\s*(?:Yes|No))")
    mse_emd = find_exact(r"(MSE.{1,30}exempted from.*?EMD)")
    if emd_exempt and "Yes" in emd_exempt:
        card["EMD"] = {"status": "✅", "details": emd_exempt, "amount": fmt_inr(emd_val)}
    elif mse_emd:
        card["EMD"] = {"status": "✅", "details": mse_emd, "amount": fmt_inr(emd_val)}
    elif emd_val and to_num(emd_val) > 0:
        card["EMD"] = {"status": "❌", "details": f"EMD Required: {fmt_inr(emd_val)}", "amount": fmt_inr(emd_val)}
    else:
        card["EMD"] = {"status": "❓", "details": "Not mentioned", "amount": "N/A"}

    # --- ePBG ---
    epbg_pct = bid_data.get("ePBG %", "N/A")
    epbg_dur = bid_data.get("ePBG Duration", "N/A")
    if epbg_pct != "N/A":
        card["ePBG"] = {"status": "❌", "details": f"{epbg_pct}% for {epbg_dur} months" if epbg_dur != "N/A" else f"{epbg_pct}%"}
    else:
        card["ePBG"] = {"status": "❓", "details": "Not mentioned"}

    # --- Technical Score ---
    tech = find_exact(r"(?:Minimum Qualifying Marks|Passing Technical Marks)\s*:?\s*(\d+)")
    if tech:
        card["Technical Score"] = {"status": "❌", "details": f"Minimum {tech}/100 required"}
    else:
        card["Technical Score"] = {"status": "❓", "details": "Not mentioned"}

    # --- OEM Authorization ---
    oem = find_exact(r"(OEM\s*Authorization\s*(?:Certificate)?\s*:?\s*(?:Yes|No|Required|Mandatory))")
    if oem:
        card["OEM Authorization"] = {"status": "❌" if any(w in oem for w in ["Yes", "Required", "Mandatory"]) else "✅", "details": oem}
    else:
        card["OEM Authorization"] = {"status": "❓", "details": "Not mentioned"}

    # --- Liquid Assets ---
    la = find_exact(r"(Liquid Assets.{0,60})")
    card["Liquid Assets"] = {"status": "✅" if la and "relaxation" in la.lower() else "❓", "details": la or "Not mentioned"}

    # --- Net Worth ---
    nw = find_exact(r"(Net Worth.{0,60})")
    card["Net Worth"] = {"status": "✅" if nw and "relaxation" in nw.lower() else "❓", "details": nw or "Not mentioned"}

    # --- Profitability ---
    pf = find_exact(r"(Profitability.{0,60})")
    card["Profitability"] = {"status": "✅" if pf and "relaxation" in pf.lower() else "❓", "details": pf or "Not mentioned"}

    # --- Payment Terms (critical for cash flow) ---
    pay = find_exact(r"((?:Payment|Payments)\s*(?:shall|will|to).*?(?:days?|milestone).*?[.)])")
    card["Payment Terms"] = {"status": "ℹ️", "details": pay or "Not mentioned"}

    # --- Penalty / LD ---
    ld = find_exact(r"((?:Liquidated Damages|Penalty|LD)\s*.{0,80})")
    card["Penalty / LD"] = {"status": "⚠️" if ld else "❓", "details": ld or "Not mentioned"}

    return card

# ===========================================================================
# UI
# ===========================================================================
st.title("GeM Bid Intelligence Dashboard")

with st.sidebar:
    st.header("📥 Upload Tenders")
    with st.form("upload_form", clear_on_submit=True):
        files = st.file_uploader("Attach GeM PDFs", type=["pdf"], accept_multiple_files=True)
        submitted = st.form_submit_button("Extract & Analyze")

# --- PROCESSING ---
if submitted and files:
    with st.spinner("Extracting bid intelligence..."):
        rows = []
        slot = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        bar = st.progress(0)
        for i, f in enumerate(files):
            text, links = read_pdf(f)
            if text:
                row = extract_bid(text, links)
                row["Slot ID"] = slot
                rows.append(row)
            bar.progress((i + 1) / len(files))
        if rows:
            df_new = pd.DataFrame(rows)
            df_old = pd.read_csv(OUTPUT_CSV) if os.path.exists(OUTPUT_CSV) else pd.DataFrame()
            df_all = pd.concat([df_old, df_new], ignore_index=True).drop_duplicates(subset=['Bid Number'], keep='last') if not df_old.empty else df_new
            df_all.to_csv(OUTPUT_CSV, index=False)
            st.success("✅ Extraction complete!")
            st.rerun()

# --- LOAD DATA ---
df = pd.read_csv(OUTPUT_CSV) if os.path.exists(OUTPUT_CSV) else pd.DataFrame()

if df.empty:
    st.info("Upload GeM Bid PDFs from the sidebar to get started.")
    st.stop()

df['_val'] = df['Estimated Bid Value'].apply(to_num)
df['_emd'] = df['EMD Amount'].apply(to_num)

# ===========================================================================
# TAB LAYOUT
# ===========================================================================
tab1, tab2, tab3 = st.tabs(["📋 Bid Explorer", "⚖️ Compare Bids", "📊 Analytics & Insights"])

# ---------------------------------------------------------------------------
# TAB 1 — BID EXPLORER
# ---------------------------------------------------------------------------
with tab1:
    st.markdown("### Uploaded Bids")
    for idx, row in df.iterrows():
        bid = row.get('Bid Number', 'Unknown')
        org = row.get('Organisation', 'N/A')
        val = row.get('_val', 0)
        end_date = row.get('Bid End Date', 'N/A')

        with st.expander(f"📁 **{bid}** — {org} — {fmt_inr(val)} — Ends: {end_date}"):

            m1, m2, m3, m4, m5, m6 = st.columns(6)
            m1.metric("Bid Value", fmt_inr(val))
            m2.metric("EMD", fmt_inr(row.get('EMD Amount', 0)))
            m3.metric("Contract", row.get('Contract Period', 'N/A'))
            m4.metric("Min Turnover", fmt_lakh(row.get('Min Turnover (Lakhs)', 'N/A')))
            m5.metric("Min Exp", fmt_years(row.get('Min Experience (Yrs)', 'N/A')))
            m6.metric("Deadline", end_date)

            st.divider()

            # ---- DECISION CARD (Theme-safe) ----
            st.markdown("#### Should You Bid? — Quick Decision Card")
            try:
                card = json.loads(row.get("Decision_Card", "{}"))
            except:
                card = {}

            if card:
                criteria_keys = list(card.keys())
                cols = st.columns(3)
                for i, key in enumerate(criteria_keys):
                    c = card[key]
                    status = c.get("status", "❓")
                    details = c.get("details", "")
                    extra = c.get("min", c.get("amount", ""))
                    with cols[i % 3]:
                        if "✅" in status:
                            st.success(f"**{key}** {status}\n\n{details}" + (f"\n\nReq: {extra}" if extra else ""))
                        elif "❌" in status:
                            st.error(f"**{key}** {status}\n\n{details}" + (f"\n\nReq: {extra}" if extra else ""))
                        elif "⚠️" in status:
                            st.warning(f"**{key}** {status}\n\n{details}")
                        else:
                            st.info(f"**{key}** {status}\n\n{details}" + (f"\n\nReq: {extra}" if extra else ""))

            st.divider()

            # ---- FULL BID DETAILS ----
            st.markdown("#### 📄 Full Bid Details")
            detail_cols = ["Bid Number", "Bid End Date", "Bid Opening Date", "Bid Validity",
                           "Ministry", "Department", "Organisation", "Office",
                           "Item Category", "Contract Period", "Bid Type", "Evaluation Method",
                           "Estimated Bid Value", "EMD Amount", "ePBG %", "ePBG Duration",
                           "Min Turnover (Lakhs)", "Min Experience (Yrs)", "Payment Terms",
                           "MSE Turnover Relaxation", "Startup Turnover Relaxation",
                           "MSE Experience Exemption", "Tech Qualifying Marks", "EMD Exemption",
                           "Docs Required", "Buyer Email"]
            detail_data = {c: row.get(c, "N/A") for c in detail_cols if c in row.index}
            st.dataframe(pd.DataFrame(detail_data, index=["Value"]).T, use_container_width=True)

            st.divider()

            # ---- HYPERLINK SUMMARIES ----
            st.markdown("#### 🔗 Attachment Insights")
            try:
                h_data = json.loads(str(row.get("Hyperlinks_JSON", "{}")))
            except:
                h_data = {}
            if h_data:
                for doc, summary in h_data.items():
                    # New records retain the source URL; support legacy summary-only CSV records.
                    if isinstance(summary, dict):
                        url = summary.get("url", "")
                        text = summary.get("summary", "")
                    else:
                        url, text = "", str(summary)
                    label = f"[📄 {doc}]({url})" if url else f"**📄 {doc}**"
                    st.markdown(label)
                    st.caption(text or "No critical clauses found.")
            else:
                st.write("No attachments found or downloaded for this bid.")

            st.divider()
            if st.button(f"🗑️ Delete This Bid", key=f"del_{idx}"):
                df_upd = pd.read_csv(OUTPUT_CSV)
                df_upd = df_upd[df_upd['Bid Number'] != bid]
                df_upd.to_csv(OUTPUT_CSV, index=False)
                st.rerun()

    st.divider()
    c1, c2 = st.columns(2)
    with c1:
        st.download_button("⬇️ Download CSV", df.to_csv(index=False).encode('utf-8'), "gem_bids.csv", "text/csv")
    with c2:
        slots = df["Slot ID"].dropna().unique().tolist() if "Slot ID" in df.columns else []
        if slots:
            slot_del = st.selectbox("Delete Slot", slots)
            if st.button("🗑️ Delete Slot"):
                df = df[df["Slot ID"] != slot_del]
                df.to_csv(OUTPUT_CSV, index=False)
                st.rerun()

# ---------------------------------------------------------------------------
# TAB 2 — COMPARE BIDS
# ---------------------------------------------------------------------------
with tab2:
    st.markdown("### ⚖️ Select Bids to Compare")

    bid_options = df["Bid Number"].tolist()
    selected_bids = st.multiselect("Pick 2 or more bids to compare side-by-side", bid_options, default=bid_options[:min(3, len(bid_options))])

    if len(selected_bids) < 2:
        st.warning("Select at least 2 bids to compare.")
    else:
        cdf = df[df["Bid Number"].isin(selected_bids)].copy()

        # --- Custom side-by-side comparison table ---
        compare_cols = [
            "Bid Number", "Organisation", "Item Category", "Estimated Bid Value",
            "EMD Amount", "Contract Period", "Min Turnover (Lakhs)", "Min Experience (Yrs)",
            "Bid End Date", "Evaluation Method", "ePBG %", "ePBG Duration",
            "MSE Turnover Relaxation", "Startup Turnover Relaxation",
            "MSE Experience Exemption", "EMD Exemption", "Tech Qualifying Marks",
            "Payment Terms", "Buyer Email"
        ]
        available = [c for c in compare_cols if c in cdf.columns]
        selected_fields = st.multiselect(
            "Choose comparison slots",
            available,
            default=[c for c in ["Organisation", "Item Category", "Estimated Bid Value", "EMD Amount", "Contract Period", "Min Turnover (Lakhs)", "Min Experience (Yrs)", "Bid End Date"] if c in available],
            help="Only the fields you select are shown in the matrix and charts."
        )
        available = ["Bid Number"] + [c for c in selected_fields if c != "Bid Number"]
        display_df = cdf[available].set_index("Bid Number").T
        st.dataframe(display_df, use_container_width=True, height=min(700, 180 + len(available) * 34))

        st.divider()

        # --- Visual Decision Matrix ---
        st.markdown("### 🏆 Decision Matrix — Which Bid to Pick?")
        scores = []
        for _, row in cdf.iterrows():
            bid = row.get("Bid Number", "?")
            try:
                card = json.loads(row.get("Decision_Card", "{}"))
            except:
                card = {}
            total = len(card) if card else 1
            green = sum(1 for v in card.values() if "✅" in v.get("status", ""))
            red = sum(1 for v in card.values() if "❌" in v.get("status", ""))
            unknown = total - green - red
            score = int((green / total) * 100) if total else 0
            scores.append({"Bid": bid, "Score": score, "✅ Relaxed": green, "❌ Mandatory": red, "❓ Unknown": unknown})

        score_df = pd.DataFrame(scores).sort_values("Score", ascending=False)

        for _, s in score_df.iterrows():
            col_label, col_bar = st.columns([1, 3])
            with col_label:
                emoji = "🟢" if s["Score"] >= 60 else "🟡" if s["Score"] >= 30 else "🔴"
                st.markdown(f"{emoji} **{s['Bid']}**")
                st.caption(f"{s['✅ Relaxed']} ✅  {s['❌ Mandatory']} ❌  {s['❓ Unknown']} ❓")
            with col_bar:
                st.progress(s["Score"] / 100)
                st.caption(f"Bid-Friendliness Score: **{s['Score']}%**")

        st.divider()

        # --- Financial comparison chart ---
        st.markdown("### 💰 Selected Numeric Comparison")
        numeric_candidates = [c for c in selected_fields if c in {"Estimated Bid Value", "EMD Amount", "Min Turnover (Lakhs)", "Min Experience (Yrs)", "ePBG Duration", "Tech Qualifying Marks"}]
        if numeric_candidates:
            chart_rows = []
            for _, r in cdf.iterrows():
                for field in numeric_candidates:
                    chart_rows.append({"Bid": str(r.get("Bid Number", "?")), "Metric": field, "Value": to_num(r.get(field, 0))})
            chart_df = pd.DataFrame(chart_rows)
            compare_chart = alt.Chart(chart_df).mark_bar().encode(
                y=alt.Y("Bid:N", sort="-x", title="Bid"),
                x=alt.X("Value:Q", title="Value", axis=alt.Axis(format=",.0f")),
                color=alt.Color("Metric:N", title="Metric"),
                row=alt.Row("Metric:N", title=None),
                tooltip=["Bid", "Metric", alt.Tooltip("Value:Q", format=",.0f")]
            ).properties(height=max(180, 110 * len(numeric_candidates))).resolve_scale(x="independent")
            st.altair_chart(compare_chart, use_container_width=True)
        else:
            st.info("Select at least one numeric slot to show a comparison chart.")

# ---------------------------------------------------------------------------
# TAB 3 — ANALYTICS & INSIGHTS
# ---------------------------------------------------------------------------
with tab3:
    st.markdown("### Decision-ready bid insights")
    analytics_metrics = st.multiselect(
        "Choose metrics to visualize",
        ["Estimated Bid Value", "EMD Amount", "Min Turnover (Lakhs)", "Min Experience (Yrs)", "ePBG Duration", "Tech Qualifying Marks"],
        default=["Estimated Bid Value", "EMD Amount"],
        key="analytics_metrics"
    )

    st.divider()

    # --- Deadline Urgency Tracker ---
    st.subheader("⏰ Deadline Urgency Tracker")
    if "Bid End Date" in df.columns:
        deadline_rows = []
        today = datetime.datetime.now()
        for _, row in df.iterrows():
            raw = str(row.get("Bid End Date", ""))
            try:
                end_dt = datetime.datetime.strptime(raw.strip()[:10], "%d-%m-%Y")
                days_left = (end_dt - today).days
            except:
                days_left = None
            deadline_rows.append({
                "Bid": row.get("Bid Number", "?"),
                "Organisation": row.get("Organisation", "N/A"),
                "Deadline": raw,
                "Days Left": days_left if days_left is not None else "Unknown",
                "Bid Value": fmt_inr(row.get("_val", 0))
            })

        dl_df = pd.DataFrame(deadline_rows)
        dl_df_sorted = dl_df.sort_values("Days Left", key=lambda x: pd.to_numeric(x, errors='coerce'))

        for _, r in dl_df_sorted.iterrows():
            days = r["Days Left"]
            if isinstance(days, (int, float)) and days <= 7:
                st.error(f"🔴 **{r['Bid']}** — {r['Organisation']} — **{days} days left** — {r['Bid Value']}")
            elif isinstance(days, (int, float)) and days <= 15:
                st.warning(f"🟡 **{r['Bid']}** — {r['Organisation']} — **{days} days left** — {r['Bid Value']}")
            elif isinstance(days, (int, float)):
                st.success(f"🟢 **{r['Bid']}** — {r['Organisation']} — **{days} days left** — {r['Bid Value']}")
            else:
                st.info(f"ℹ️ **{r['Bid']}** — {r['Organisation']} — Deadline: {r['Deadline']} — {r['Bid Value']}")

    st.divider()

    # --- Readable, user-selected exposure charts ---
    st.subheader("💰 Selected Financial / Eligibility Metrics")
    metric_map = {
        "Estimated Bid Value": "_val", "EMD Amount": "_emd",
        "Min Turnover (Lakhs)": "Min Turnover (Lakhs)",
        "Min Experience (Yrs)": "Min Experience (Yrs)",
        "ePBG Duration": "ePBG Duration", "Tech Qualifying Marks": "Tech Qualifying Marks"
    }
    chosen = [metric_map[m] for m in analytics_metrics if metric_map[m] in df.columns]
    if chosen:
        rows = []
        for _, r in df.iterrows():
            for metric in chosen:
                label = next((k for k, v in metric_map.items() if v == metric), metric)
                rows.append({"Bid": str(r.get("Bid Number", "?")), "Metric": label, "Value": to_num(r.get(metric, 0))})
        analytics_df = pd.DataFrame(rows)
        analytics_chart = alt.Chart(analytics_df).mark_bar().encode(
            y=alt.Y("Bid:N", sort="-x", title="Bid"),
            x=alt.X("Value:Q", title="Value", axis=alt.Axis(format=",.0f")),
            color=alt.Color("Metric:N", title="Metric"),
            row=alt.Row("Metric:N", title=None),
            tooltip=["Bid", "Metric", alt.Tooltip("Value:Q", format=",.0f")]
        ).properties(height=max(220, len(chosen) * 110)).resolve_scale(x="independent")
        st.altair_chart(analytics_chart, use_container_width=True)
    else:
        st.info("Select one or more metrics to visualize.")

    st.divider()

    # --- Relaxation Heatmap ---
    st.subheader("📊 MSME/Startup Relaxation Heatmap")

    def get_status(json_str, key):
        try:
            return json.loads(json_str).get(key, {}).get("status", "❓")
        except:
            return "❓"

    if "Decision_Card" in df.columns:
        heat_keys = ["Turnover", "Startup Relaxation", "Bidder Experience", "EMD", "ePBG",
                     "Technical Score", "OEM Authorization", "Liquid Assets", "Net Worth",
                     "Profitability", "Payment Terms", "Penalty / LD"]
        heat_data = []
        for _, row in df.iterrows():
            bid = row.get("Bid Number", "?")
            for k in heat_keys:
                heat_data.append({"Bid": bid, "Criteria": k, "Status": get_status(row.get("Decision_Card", "{}"), k)})

        heat_df = pd.DataFrame(heat_data)
        heatmap = alt.Chart(heat_df).mark_rect().encode(
            x=alt.X("Bid:N", title="Bid Number"),
            y=alt.Y("Criteria:N", title="Criteria", sort=heat_keys),
            color=alt.Color("Status:N", scale=alt.Scale(
                domain=["✅", "❌", "❓", "⚠️", "ℹ️"],
                range=["#4CAF50", "#F44336", "#9E9E9E", "#FF9800", "#2196F3"]
            ))
        ).properties(height=400)
        st.altair_chart(heatmap, use_container_width=True)

    st.divider()

    # --- Actionable Recommendation ---
    st.subheader("🎯 Recommendation")
    if "Decision_Card" in df.columns:
        best_score, best_bid = 0, "N/A"
        for _, row in df.iterrows():
            try:
                card = json.loads(row.get("Decision_Card", "{}"))
            except:
                card = {}
            total = len(card) if card else 1
            green = sum(1 for v in card.values() if "✅" in v.get("status", ""))
            score = int((green / total) * 100) if total else 0
            if score > best_score:
                best_score = score
                best_bid = row.get("Bid Number", "?")

        if best_score > 0:
            st.success(f"**Best bid for your company: {best_bid}** with a Bid-Friendliness Score of **{best_score}%**. This bid has the most MSME/Startup relaxations in your current batch.")
        else:
            st.info("Not enough data to recommend a bid. Upload more PDFs to compare.")
