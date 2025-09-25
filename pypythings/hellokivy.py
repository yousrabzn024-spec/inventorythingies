# app.py
# Streamlit Inventory Scanner (Random / Per-Box)
# Modified: editable quantities, delete rows, 13-digit barcode validation,
# added "System Template" export, and Database mode (Oracle) to load transfers.

import io
from datetime import datetime

import pandas as pd
import streamlit as st

# Optional DB client — required only when using Database mode
try:
    import oracledb
except Exception:
    oracledb = None

REQUIRED_DB_COLUMNS = ["box_no", "barcode", "item_code", "size", "color", "avl_qty"]


def safe_str(x):
    if pd.isna(x):
        return ""
    return str(x).strip()

def init_state():
    ss = st.session_state
    ss.setdefault("db_df", None)                     # full DB as DataFrame (current source)
    ss.setdefault("db_by_barcode", {})               # {barcode: {...}}
    ss.setdefault("boxes_in_db", set())              # set of box_no from DB
    ss.setdefault("mode", None)                      # "random" or "per_box"
    ss.setdefault("current_box", None)               # active box in per_box mode
    ss.setdefault("manual_boxes", set())             # boxes created manually
    ss.setdefault("scans", {})                       # {box_no: {barcode: {... item data ..., scanned_qty, anomalies(list)}}}
    ss.setdefault("admitted_boxes", set())           # boxes you admitted
    # DB caching
    ss.setdefault("transfers_df", None)              # raw transfers fetched from DB (if any)

def load_database(file):
    df = pd.read_excel(file)
    # normalize/validate
    cols_lower = {c.lower(): c for c in df.columns}
    for c in REQUIRED_DB_COLUMNS:
        if c not in cols_lower:
            raise ValueError(f"Missing required column: '{c}'. Found: {list(df.columns)}")

    # build normalized dataframe with correct column order/types
    ndf = pd.DataFrame({
        "box_no":    df[cols_lower["box_no"]].map(safe_str),
        "barcode":   df[cols_lower["barcode"]].map(safe_str),
        "item_code": df[cols_lower["item_code"]].map(safe_str),
        "size":      df[cols_lower["size"]].map(safe_str),
        "color":     df[cols_lower["color"]].map(safe_str),
        "avl_qty":   pd.to_numeric(df[cols_lower["avl_qty"]], errors="coerce").fillna(0).astype(int),
    })
    # drop empty barcode rows
    ndf = ndf[ndf["barcode"] != ""].reset_index(drop=True)
    if ndf.empty:
        raise ValueError("Database contains no valid rows (empty barcodes).")

    # map barcode -> row dict
    by_bc = {}
    for _, r in ndf.iterrows():
        by_bc[r["barcode"]] = r.to_dict()

    return ndf, by_bc, set(ndf["box_no"].unique())

def ensure_box(ss, box_no):
    ss["scans"].setdefault(box_no, {})  # create empty dict for that box

def is_valid_barcode_13(bc):
    return isinstance(bc, str) and bc.isdigit() and len(bc) == 13

def record_scan(ss, target_box, barcode):
    """Record a scan. Assumes barcode already validated (13 digits)."""
    scans = ss["scans"]
    ensure_box(ss, target_box)
    box_scans = scans[target_box]

    if barcode not in box_scans:
        if barcode in ss["db_by_barcode"]:
            info = ss["db_by_barcode"][barcode].copy()
            info["scanned_qty"] = 0
            info["anomalies"] = []

            # wrong box anomaly (per_box mode)
            if ss["mode"] == "per_box" and info.get("box_no") != target_box:
                info["anomalies"].append(
                    f"Wrong box: belongs to {info.get('box_no')} but scanned in {target_box}"
                )
        else:
            # unknown barcode
            info = {
                "box_no": target_box,
                "barcode": barcode,
                "item_code": "UNKNOWN",
                "size": "N/A",
                "color": "N/A",
                "avl_qty": 0,
                "scanned_qty": 0,
                "anomalies": ["Unknown barcode"],
            }
        box_scans[barcode] = info

    box_scans[barcode]["scanned_qty"] += 1

def compute_box_anomalies(ss, box_no):
    """Return a list of human-readable anomalies for a given box."""
    anomalies = []
    box_scans = ss["scans"].get(box_no, {})
    for bc, info in box_scans.items():
        scanned = info.get("scanned_qty", 0)
        avl = info.get("avl_qty", 0)
        # carry forward any anomalies already present (unknown, wrong box, etc.)
        pre = info.get("anomalies", []).copy()

        # scanned vs avl écart
        if scanned != avl:
            diff = scanned - avl
            pre.append(f"Ecart {diff:+} (scanned={scanned}, avl={avl})")

        # flatten all anomalies for this barcode
        if pre:
            anomalies.append(f"Box {box_no} – Barcode {bc}: " + " | ".join(pre))
    return anomalies

def export_to_excel_bytes(ss):
    """Build a multi-sheet Excel in memory (Scans + Anomalies) and return bytes."""
    rows = []
    anomalies_rows = []

    # Build rows for Scans sheet
    for box_no, items in ss["scans"].items():
        manual_flag = "Yes" if box_no in ss["manual_boxes"] else "No"
        for bc, info in items.items():
            # compile anomaly string for this item (including écart if any)
            item_anoms = info.get("anomalies", []).copy()
            scanned = info.get("scanned_qty", 0)
            avl = info.get("avl_qty", 0)
            if scanned != avl:
                diff = scanned - avl
                item_anoms.append(f"Ecart {diff:+} (scanned={scanned}, avl={avl})")

            anomaly_str = " | ".join(item_anoms)

            rows.append([
                box_no, bc, info.get("item_code",""), info.get("size",""),
                info.get("color",""), scanned, avl, anomaly_str, manual_flag
            ])

            # for Anomalies sheet, add only if there are anomalies
            if anomaly_str:
                anomalies_rows.append([
                    box_no, bc, info.get("item_code",""), info.get("size",""),
                    info.get("color",""), scanned, avl, anomaly_str, manual_flag
                ])

    scans_df = pd.DataFrame(rows, columns=[
        "box_no","barcode","item_code","size","color",
        "scanned_qty","avl_qty","anomalies","added_manually"
    ])

    anomalies_df = pd.DataFrame(anomalies_rows, columns=scans_df.columns)

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        scans_df.to_excel(writer, sheet_name="Scans", index=False)
        anomalies_df.to_excel(writer, sheet_name="Anomalies", index=False)
    output.seek(0)
    return output

def export_system_template_bytes(ss):
    """
    Export system template with columns:
      item_code, designation (size + ' ' + color), unité ('U'), scannedqty
    """
    rows = []
    for box_no, items in ss["scans"].items():
        for bc, info in items.items():
            rows.append({
                "item_code": info.get("item_code", ""),
                "designation": f"{info.get('size','')}".strip() + ((" " + info.get("color","").strip()) if info.get("color","").strip() else ""),
                "unité": "U",
                "scannedqty": info.get("scanned_qty", 0),
                "i_price":"",
                "box_no":info.get("box_no","")
                })
    df = pd.DataFrame(rows, columns=["item_code", "designation", "unité", "scannedqty","i_price","box_no"])
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="SystemTemplate", index=False)
    output.seek(0)
    return output

# ---------------- Oracle DB Helpers ----------------
def get_connection():
    try:
        # Force thick mode
        oracledb.init_oracle_client(lib_dir=r"C:/WINDOWS.X64_193000_db_home")

        db_user = "lotfi"
        db_password = "YS123"  # ⚠️ Use the exact same password as your working script
        db_dsn = oracledb.makedsn("172.16.8.36", 1521, service_name="ORCL")

        conn = oracledb.connect(user=db_user, password=db_password, dsn=db_dsn)
        return conn
    except Exception as e:
        st.error(f"❌ Database connection failed: {e}")
        return None

    except Exception as e:
        raise

def fetch_transfers_from_db():
    """Run the provided SQL and return a DataFrame of transfers."""
    sql = """
    SELECT 
    TR.TR_NO AS BOX_NO,
    BB.BARCODE,
    TR.I_CODE,
    I.ASSISTANT_NO AS SI_ZE,
    COLOR_NAME,
    TR.I_QTY,
    TR.F_W_CODE AS SENDER,
    TR.T_W_CODE AS RECIEVER
    FROM IAS20251.IAS_WHTRNS_DTL TR
    LEFT JOIN (
            SELECT I_CODE, MAX(BARCODE) AS BARCODE
            FROM IAS20251.IAS_ITM_UNT_BARCODE
            GROUP BY I_CODE
        ) BB ON BB.I_CODE = TR.I_CODE
    JOIN IAS20251.IAS_ITM_MST I ON I.I_CODE = TR.I_CODE
    LEFT JOIN (
            SELECT DETAIL_NO AS COLOR_CODE, DETAIL_A_NAME AS COLOR_NAME
            FROM IAS20251.IAS_DETAIL_GROUP
        ) G ON G.COLOR_CODE = I.DETAIL_NO
    """
    conn = None
    try:
        conn =  get_connection()
        df = pd.read_sql(sql, conn)
        return df
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

def map_transfer_df_to_dbschema(trans_df):
    """
    Map the transfer DataFrame columns to the internal schema:
      box_no, barcode, item_code, size, color, avl_qty
    """
    # Ensure columns exist
    df = trans_df.copy()
    # Normalize column names (case-insensitive)
    colmap = {c.upper(): c for c in df.columns}
    # Columns expected in fetched query: BOX_NO, BARCODE, I_CODE, SI_ZE, COLOR_NAME, I_QTY
    df_mapped = pd.DataFrame({
        "box_no": df[colmap.get("BOX_NO")].map(safe_str) if "BOX_NO" in colmap else df.get("BOX_NO", "").map(safe_str),
        "barcode": df[colmap.get("BARCODE")] .fillna("").map(safe_str) if "BARCODE" in colmap else "",
        "item_code": df[colmap.get("I_CODE")].map(safe_str) if "I_CODE" in colmap else df.get("I_CODE","").map(safe_str),
        "size": df[colmap.get("SI_ZE")].map(safe_str) if "SI_ZE" in colmap else df.get("SI_ZE","").map(safe_str),
        "color": df[colmap.get("COLOR_NAME")].map(safe_str) if "COLOR_NAME" in colmap else df.get("COLOR_NAME","").map(safe_str),
        "avl_qty": pd.to_numeric(df[colmap.get("I_QTY")], errors="coerce").fillna(0).astype(int) if "I_QTY" in colmap else 0
    })
    # drop rows without barcode? We keep them — unknown barcode entries will be created when scanning
    # but for mapping barcode->row, skip empty barcodes
    ndf = df_mapped.reset_index(drop=True)
    by_bc = {}
    for _, r in ndf.iterrows():
        bc = r["barcode"]
        if bc:
            by_bc[bc] = r.to_dict()
    boxes = set(ndf["box_no"].unique())
    return ndf, by_bc, boxes

# ----------------------- UI -----------------------
st.set_page_config(page_title="Inventory Scanner", page_icon="📦", layout="wide")
init_state()
ss = st.session_state

# --- Minimal styling for a modern feel ---
st.markdown("""
<style>
    .ok-badge {background:#16a34a11;color:#16a34a;padding:4px 8px;border-radius:8px;font-weight:600;border:1px solid #16a34a44;}
    .warn-badge {background:#f59e0b11;color:#b45309;padding:4px 8px;border-radius:8px;font-weight:600;border:1px solid #f59e0b44;}
    .err-badge {background:#ef444411;color:#991b1b;padding:4px 8px;border-radius:8px;font-weight:600;border:1px solid #ef444466;}
    .blk {font-weight:600;margin-left:4px;}
    .muted {color:#64748b;}
</style>
""", unsafe_allow_html=True)

# ---------------- Sidebar ----------------
with st.sidebar:
    st.header("📦 Inventory Scanner")

    # Data source: Excel vs Database
    st.subheader("Data Source")
    data_source = st.radio("Choose input source:", ["Excel Upload", "Database"], index=0)

    if data_source == "Excel Upload":
        db_file = st.file_uploader("Load Excel Database", type=["xlsx"])
        if db_file is not None:
            try:
                ss["db_df"], ss["db_by_barcode"], ss["boxes_in_db"] = load_database(db_file)
                st.success("Database loaded from Excel ✓")
            except Exception as e:
                st.error(f"Failed to load DB from Excel: {e}")

    else:
        # Database mode UI
        if oracledb is None:
            st.error("Database mode requires python-oracledb. Install it in your environment.")
        else:
            st.info("Database mode: fetch transfers from Oracle")
            if st.button("Fetch transfers from DB", use_container_width=True):
                try:
                    trans_df = fetch_transfers_from_db()
                    if trans_df is None or trans_df.empty:
                        st.warning("No transfers returned from database.")
                    else:
                        ss["transfers_df"] = trans_df
                        st.success(f"Fetched {len(trans_df)} rows from DB ✓")
                except Exception as e:
                    st.error(f"Failed to fetch transfers: {e}")

            if ss.get("transfers_df") is not None:
                tdf = ss["transfers_df"]
                # create sender/receiver choices
                senders = sorted(tdf["SENDER"].dropna().unique().tolist())
                sender = st.selectbox("Sender (F_W_CODE)", ["— choose —"] + senders)
                if sender and sender != "— choose —":
                    recs = sorted(tdf[tdf["SENDER"] == sender]["RECIEVER"].dropna().unique().tolist())
                else:
                    recs = sorted(tdf["RECIEVER"].dropna().unique().tolist())

                receiver = st.selectbox("Receiver (T_W_CODE)", ["— choose —"] + recs)

                # Transfer numbers filtered
                if sender and sender != "— choose —" and receiver and receiver != "— choose —":
                    trans_filtered = tdf[(tdf["SENDER"] == sender) & (tdf["RECIEVER"] == receiver)]
                elif sender and sender != "— choose —":
                    trans_filtered = tdf[tdf["SENDER"] == sender]
                else:
                    trans_filtered = tdf

                transfer_numbers = sorted(trans_filtered["BOX_NO"].dropna().unique().tolist())
                chosen_transfer = st.selectbox("Transfer No (TR_NO)", ["— choose —"] + transfer_numbers)

                if st.button("Load Transfer", use_container_width=True, disabled=(chosen_transfer == "— choose —")):
                    if chosen_transfer == "— choose —":
                        st.warning("Select a transfer number first.")
                    else:
                        sel_df = trans_filtered[trans_filtered["BOX_NO"] == chosen_transfer].copy()
                        if sel_df.empty:
                            st.error("Selected transfer has no rows.")
                        else:
                            # Map to internal schema
                            ndf, by_bc, boxes = map_transfer_df_to_dbschema(sel_df)
                            ss["db_df"] = ndf
                            ss["db_by_barcode"] = by_bc
                            ss["boxes_in_db"] = boxes
                            ss["manual_boxes"] = set()  # clear manual boxes
                            st.success(f"Loaded transfer {chosen_transfer} as database source. Boxes: {len(boxes)}")
                            # Optionally switch to per_box mode and set current_box if single box
                            if len(boxes) == 1:
                                only = list(boxes)[0]
                                ss["mode"] = "per_box"
                                ss["current_box"] = only
                                st.toast(f"Mode set to per_box; current box {only}", icon="📦")

    st.divider()

    # Mode selection (Random / Per Box)
    st.subheader("Scan Mode")
    colm1, colm2 = st.columns(2)
    if colm1.button("Random Mode", use_container_width=True):
        ss["mode"] = "random"
        ss["current_box"] = None
        st.toast("Random mode enabled.", icon="✅")
    if colm2.button("Per Box Mode", use_container_width=True):
        ss["mode"] = "per_box"
        st.toast("Per Box mode enabled. Choose a box.", icon="✅")

    if ss["mode"] == "per_box":
        st.write("**Select / Create Box**")
        boxes_sorted = sorted(list(ss["boxes_in_db"] | ss["manual_boxes"]))
        existing = ["— choose —"] + boxes_sorted
        chosen = st.selectbox("Box from DB / Manual:", existing, index=0)
        new_box = st.text_input("Or create new box", placeholder="Type new box number")

        colbx1, colbx2 = st.columns(2)
        if colbx1.button("Use Selected", use_container_width=True, disabled=(chosen == "— choose —")):
            ss["current_box"] = chosen
            st.toast(f"Scanning in Box {chosen}", icon="📦")

        if colbx2.button("Create/Use New", use_container_width=True, disabled=(not new_box.strip())):
            nb = new_box.strip()
            ss["current_box"] = nb
            ss["manual_boxes"].add(nb)
            st.toast(f"Manual box created: {nb}", icon="🛠️")

    st.divider()

    # Admit & Export
    if st.button("Admit Current Box", use_container_width=True, disabled=(ss["mode"] != "per_box" or not ss["current_box"])):
        if not ss.get("current_box"):
            st.warning("No active box.")
        else:
            box_no = ss["current_box"]
            if not ss["scans"].get(box_no):
                st.warning("Nothing scanned in this box.")
            else:
                anomalies = compute_box_anomalies(ss, box_no)
                if anomalies:
                    st.error("Anomalies found:")
                    for a in anomalies:
                        st.write(f"• {a}")
                else:
                    st.success(f"Box {box_no} admitted successfully ✓")
                ss["admitted_boxes"].add(box_no)
                ss["current_box"] = None

    # Export buttons
    st.download_button(
        "Export Excel (All Boxes)",
        data=export_to_excel_bytes(ss),
        file_name=f"scanned_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
        disabled=(len(ss["scans"]) == 0),
    )

    st.download_button(
        "Export System Template",
        data=export_system_template_bytes(ss),
        file_name=f"system_template_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
        disabled=(len(ss["scans"]) == 0),
    )

    if st.button("Reset All Scans", type="secondary", use_container_width=True):
        ss["scans"].clear()
        ss["manual_boxes"].clear()
        ss["admitted_boxes"].clear()
        ss["current_box"] = None
        st.toast("All scans reset.", icon="🧹")

# ---------------- Main Area ----------------
st.title("📦 Inventory Scanner")
if ss["db_df"] is None:
    st.info("Load your Excel database from the sidebar or load a transfer from Database mode to begin.")
    st.stop()

# Quick status row
c1, c2, c3, c4 = st.columns(4)
mode_label = ss["mode"] if ss["mode"] else "—"
c1.markdown(f"**Mode:** <span class='blk'>{mode_label}</span>", unsafe_allow_html=True)
cur_box = ss["current_box"] if ss["current_box"] else "—"
c2.markdown(f"**Current Box:** <span class='blk'>{cur_box}</span>", unsafe_allow_html=True)
c3.markdown(f"**Manual Boxes:** <span class='blk'>{len(ss['manual_boxes'])}</span>", unsafe_allow_html=True)
total_lines = sum(len(v) for v in ss["scans"].values())
c4.markdown(f"**Scanned Lines:** <span class='blk'>{total_lines}</span>", unsafe_allow_html=True)

st.divider()

# Scan form
if ss["mode"] is None:
    st.warning("Choose a scan mode in the sidebar.")
else:
    # Use single input for scanning, validate 13-digit barcodes
    scan_key = "scan_input_random" if ss["mode"] == "random" else "scan_input_box"
    with st.form("scan_form", clear_on_submit=True):
        st.text_input("Scan barcode", key=scan_key, placeholder="Focus here and scan…")
        submitted = st.form_submit_button("Add Scan")
        if submitted:
            barcode = safe_str(st.session_state.get(scan_key, ""))
            if not barcode:
                st.warning("Please scan a barcode.")
            elif not is_valid_barcode_13(barcode):
                st.error(f"Invalid barcode format: {barcode}. Barcode must be exactly 13 numeric digits.")
            else:
                if ss["mode"] == "random":
                    record_scan(ss, "RANDOM", barcode)
                    st.success(f"Scanned {barcode} (Random)")
                else:
                    if not ss["current_box"]:
                        st.warning("Select or create a box first (sidebar).")
                    else:
                        record_scan(ss, ss["current_box"], barcode)
                        st.success(f"Scanned {barcode} in Box {ss['current_box']}")

# Live summary
st.subheader("🔎 Live Scans")
tabs = st.tabs(["By Box", "All Items", "Database Preview"])

with tabs[0]:
    if not ss["scans"]:
        st.info("No scans yet.")
    else:
        for box_no, items in list(ss["scans"].items()):
            manual = "Yes" if box_no in ss["manual_boxes"] else "No"
            sub = st.expander(f"Box {box_no}  •  Manual: {manual}  •  Items: {len(items)}", expanded=(ss["current_box"] == box_no))

            # Build dataframe for editing
            df_box = []
            for bc, info in items.items():
                df_box.append({
                    "barcode": bc,
                    "item_code": info.get("item_code",""),
                    "size": info.get("size",""),
                    "color": info.get("color",""),
                    "scanned_qty": int(info.get("scanned_qty", 0)),
                    "avl_qty": int(info.get("avl_qty", 0)),
                    "anomalies": " | ".join(info.get("anomalies", [])),
                    "delete": False,
                })
            if not df_box:
                sub.info("No items scanned in this box yet.")
                continue

            df_display = pd.DataFrame(df_box)

            # Use st.data_editor for editable scanned_qty and delete checkbox
            editor_key = f"editor_{box_no}"
            edited = sub.data_editor(
                df_display,
                num_rows="dynamic",
                use_container_width=True,
                key=editor_key
            )

            # Buttons to commit changes or discard
            col1, col2 = sub.columns([1,1])
            if col1.button("Apply changes", key=f"apply_{box_no}"):
                # Process deletions
                to_delete = edited[edited.get("delete", False) == True]["barcode"].tolist() if "delete" in edited.columns else []
                for bc in to_delete:
                    if bc in ss["scans"].get(box_no, {}):
                        ss["scans"][box_no].pop(bc, None)
                # Process scanned_qty updates
                for _, row in edited.iterrows():
                    bc = safe_str(row["barcode"])
                    if bc == "" or bc not in ss["scans"].get(box_no, {}):
                        continue
                    # sanitize scanned_qty
                    try:
                        new_qty = int(row.get("scanned_qty", 0))
                        if new_qty < 0:
                            new_qty = 0
                    except Exception:
                        new_qty = 0
                    ss["scans"][box_no][bc]["scanned_qty"] = new_qty
                st.success("Changes applied ✓")

            if col2.button("Delete selected rows", key=f"del_{box_no}"):
                to_delete = edited[edited.get("delete", False) == True]["barcode"].tolist() if "delete" in edited.columns else []
                if not to_delete:
                    st.info("No rows selected for deletion (check 'delete').")
                else:
                    for bc in to_delete:
                        ss["scans"][box_no].pop(bc, None)
                    st.success(f"Deleted {len(to_delete)} row(s) from Box {box_no}.")

with tabs[1]:
    # Flatten all scans into one table
    rows = []
    for box_no, items in ss["scans"].items():
        for bc, info in items.items():
            rows.append({
                "box_no": box_no,
                "barcode": bc,
                "item_code": info.get("item_code",""),
                "size": info.get("size",""),
                "color": info.get("color",""),
                "scanned_qty": info.get("scanned_qty", 0),
                "avl_qty": info.get("avl_qty", 0),
                "anomalies": " | ".join(info.get("anomalies", [])),
                "added_manually": "Yes" if box_no in ss["manual_boxes"] else "No"
            })
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True)
    else:
        st.info("No scans yet.")

with tabs[2]:
    # Show the current db_df (from Excel or loaded transfer)
    st.subheader("Database Preview")
    if ss.get("db_df") is not None:
        st.dataframe(ss["db_df"], use_container_width=True)
    else:
        st.info("No database loaded yet (Excel or DB transfer).")
