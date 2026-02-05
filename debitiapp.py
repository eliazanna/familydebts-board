# debitiapp.py
# Family Debts Board (Streamlit + Google Sheets backend + Telegram notifications)
# Requirements:
#   pip install streamlit pandas gspread google-auth requests

import streamlit as st
import pandas as pd
import uuid
from datetime import datetime, date
import json
import gspread
import requests
from google.oauth2.service_account import Credentials

st.set_page_config(page_title="Lavagna Debiti Famiglia", page_icon="🧾", layout="wide")

# ---------- CONFIG ----------
SHEET_NAME = "family_ledger"
PEOPLE = ["Elia", "Tommy", "Mamma", "Papà", "Alice"]
CATEGORIES = ["Università", "Salute", "Spesa", "Casa", "Viaggi", "Regali", "Altro"]

# ---------- Google Sheets (Service Account) ----------
@st.cache_resource(ttl=600)
def get_sheet():
    # Usa la config in secrets in formato TOML-table:
    # [gcp_service_account]
    # type="service_account" ...
    info = dict(st.secrets["gcp_service_account"])

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    client = gspread.authorize(creds)
    sh = client.open(SHEET_NAME)
    return sh.sheet1


def sheet_header(ws):
    return ws.row_values(1)


def sheet_to_df():
    ws = get_sheet()
    data = ws.get_all_records()  # lista di dict (usa header riga 1)
    expected = [
        "id", "debtor", "creditor", "amount_cents", "description", "category",
        "due_date", "status", "created_at", "paid_at", "notified_7d_at"
    ]
    if not data:
        return pd.DataFrame(columns=expected)

    df = pd.DataFrame(data)
    for c in expected:
        if c not in df.columns:
            df[c] = ""
    df = df[expected].copy()
    return df


def append_row_to_sheet(row: dict):
    ws = get_sheet()
    header = sheet_header(ws)
    values = [row.get(h, "") for h in header]
    ws.append_row(values, value_input_option="USER_ENTERED")


def find_row_index_by_id(txn_id: str):
    ws = get_sheet()
    header = sheet_header(ws)
    if "id" not in header:
        return None
    col_idx = header.index("id") + 1
    col_vals = ws.col_values(col_idx)
    for i, v in enumerate(col_vals):
        if v == txn_id:
            return i + 1  # 1-based row index
    return None


def update_cells_in_row(row_index: int, updates: dict):
    ws = get_sheet()
    header = sheet_header(ws)
    cells = []
    for col_name, val in updates.items():
        if col_name in header:
            col_idx = header.index(col_name) + 1
            cells.append(gspread.Cell(row_index, col_idx, val))
    if cells:
        ws.update_cells(cells, value_input_option="USER_ENTERED")


def delete_row(row_index: int):
    ws = get_sheet()
    ws.delete_rows(row_index)


# ---------- Telegram ----------
def get_telegram_token() -> str:
    return st.secrets["telegram"]["bot_token"]

def get_chat_ids() -> dict:
    raw = st.secrets["telegram"]["chat_ids_json"]
    return json.loads(raw) if isinstance(raw, str) else raw



def send_telegram_message(bot_token: str, chat_id: int, text: str) -> None:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    r = requests.post(url, json=payload, timeout=15)
    r.raise_for_status()


def build_due_soon_message(row: dict, days_left: int) -> str:
    debtor = str(row.get("debtor", "")).strip()
    creditor = str(row.get("creditor", "")).strip()
    desc = str(row.get("description", "")).strip()
    due = str(row.get("due_date", "")).strip()
    try:
        amount = int(str(row.get("amount_cents", "0")).strip() or 0) / 100.0
    except Exception:
        amount = 0.0

    return (
        f"⏰ <b>Promemoria debito</b>\n\n"
        f"Ciao <b>{debtor}</b>!\n"
        f"Tra <b>{days_left} giorni</b> scade:\n"
        f"• {desc}\n"
        f"• Importo: <b>{amount:.2f} €</b>\n"
        f"• Da pagare a: <b>{creditor}</b>\n"
        f"• Scadenza: <b>{due}</b>"
    )


def ensure_column_exists(col_name: str):
    ws = get_sheet()
    header = sheet_header(ws)
    if col_name in header:
        return

    # aggiungo colonna in fondo
    ws.update_cell(1, len(header) + 1, col_name)


def run_due_soon_notifications(days_threshold: int = 7) -> dict:
    """
    Invia notifiche Telegram per voci OPEN con due_date entro days_threshold.
    Non invia se notified_7d_at è già compilato.
    Aggiorna notified_7d_at nello sheet.
    """
    token = get_telegram_token()
    chat_ids = get_chat_ids()

    if not token:
        return {"ok": False, "error": "Manca TELEGRAM_BOT_TOKEN nei secrets."}
    if not chat_ids:
        return {"ok": False, "error": "Manca TELEGRAM_CHAT_IDS_JSON nei secrets (o è vuoto)."}

    ensure_column_exists("notified_7d_at")

    ws = get_sheet()
    header = sheet_header(ws)
    if "notified_7d_at" not in header:
        return {"ok": False, "error": "Non riesco a creare/vedere la colonna notified_7d_at."}

    # scarico records
    rows = ws.get_all_records()
    today = date.today()

    # indice colonna
    col_notified = header.index("notified_7d_at") + 1

    sent = 0
    skipped_no_chatid = 0
    skipped_already = 0
    skipped_not_due = 0

    for sheet_row_index, row in enumerate(rows, start=2):  # data starts row 2
        if str(row.get("status", "")).strip() != "OPEN":
            continue

        due_str = str(row.get("due_date", "")).strip()
        if not due_str:
            continue

        already = str(row.get("notified_7d_at", "")).strip()
        if already:
            skipped_already += 1
            continue

        try:
            due = datetime.strptime(due_str, "%Y-%m-%d").date()
        except Exception:
            continue

        days_left = (due - today).days
        if not (0 <= days_left <= days_threshold):
            skipped_not_due += 1
            continue

        debtor = str(row.get("debtor", "")).strip()
        chat_id = chat_ids.get(debtor)
        if not chat_id:
            skipped_no_chatid += 1
            continue

        msg = build_due_soon_message(row, days_left)
        send_telegram_message(token, int(chat_id), msg)

        # mark notified
        ws.update_cell(sheet_row_index, col_notified, datetime.now().isoformat(timespec="seconds"))
        sent += 1

    return {
        "ok": True,
        "sent": sent,
        "skipped_no_chatid": skipped_no_chatid,
        "skipped_already": skipped_already,
        "skipped_not_due": skipped_not_due,
    }


# ---------- small utils ----------
def euros_from_cents(cents):
    try:
        return round(int(cents) / 100.0, 2)
    except Exception:
        return 0.0


def cents_from_euros(euros):
    return int(round(float(euros) * 100))


def due_badge(due_iso):
    if not due_iso:
        return "—"
    try:
        d = datetime.strptime(due_iso, "%Y-%m-%d").date()
    except Exception:
        return due_iso
    today = date.today()
    if d < today:
        return f"⏰ SCADUTO ({d.strftime('%d/%m/%Y')})"
    if (d - today).days <= 7:
        return f"⚠️ entro 7gg ({d.strftime('%d/%m/%Y')})"
    return d.strftime("%d/%m/%Y")


def person_filter_df(df: pd.DataFrame, person: str) -> pd.DataFrame:
    if person == "Tutti":
        return df
    return df[(df["debtor"] == person) | (df["creditor"] == person)]


# ---------- UI: sidebar ----------
def sidebar_add_form():
    st.sidebar.header("➕ Nuova voce")

    with st.sidebar.form("add_form", clear_on_submit=True):
        debtor = st.selectbox("Debitore (chi deve pagare)", PEOPLE, index=0)
        creditor = st.selectbox("Creditore (chi deve ricevere)", PEOPLE, index=1)
        amount_eur = st.number_input("Importo (€)", min_value=0.01, value=10.00, step=1.0, format="%.2f")
        category = st.selectbox("Categoria", CATEGORIES, index=0)
        description = st.text_input("Descrizione", placeholder="es. Rata universitaria, visita medico...")
        has_due = st.checkbox("Imposta scadenza", value=True)
        due = st.date_input("Scadenza", value=date.today()) if has_due else None

        submitted = st.form_submit_button("Aggiungi alla lavagna ✅")

        if submitted:
            if debtor == creditor:
                st.sidebar.error("Debitore e creditore devono essere diversi.")
                return
            if not description.strip():
                st.sidebar.error("Inserisci una descrizione.")
                return
            cents = cents_from_euros(amount_eur)
            if cents <= 0:
                st.sidebar.error("Importo non valido.")
                return

            ensure_column_exists("notified_7d_at")

            due_iso = due.isoformat() if due else ""
            txn = {
                "id": str(uuid.uuid4()),
                "debtor": debtor,
                "creditor": creditor,
                "amount_cents": str(cents),
                "description": description.strip(),
                "category": category,
                "due_date": due_iso,
                "status": "OPEN",
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "paid_at": "",
                "notified_7d_at": ""
            }
            append_row_to_sheet(txn)
            st.sidebar.success("Aggiunto alla lavagna!")


def sidebar_notifications_box():
    st.sidebar.header("🔔 Notifiche Telegram")

    st.sidebar.caption("Invia promemoria per scadenze entro 7 giorni (manuale per ora).")

    disabled = (not get_telegram_token())
    if disabled:
        st.sidebar.warning("Manca TELEGRAM_BOT_TOKEN nei secrets.")

    if st.sidebar.button("📨 Invia notifiche (7 giorni)", use_container_width=True, disabled=disabled):
        with st.spinner("Invio notifiche in corso..."):
            res = run_due_soon_notifications(days_threshold=7)

        if not res.get("ok"):
            st.sidebar.error(res.get("error", "Errore sconosciuto"))
        else:
            st.sidebar.success(f"Notifiche inviate: {res['sent']}")
            if res["skipped_no_chatid"]:
                st.sidebar.info(f"Senza chat_id: {res['skipped_no_chatid']} (aggiungili poi nei secrets)")
            if res["skipped_already"]:
                st.sidebar.info(f"Già notificate: {res['skipped_already']}")


# ---------- UI: main pages ----------
def page_lavagna():
    st.title("🧾 Lavagna (Aperti)")

    df = sheet_to_df()
    if df.empty:
        st.info("Nessuna voce nel foglio.")
        return

    df_open = df[df["status"] == "OPEN"].copy()
    df_open["amount_eur"] = df_open["amount_cents"].apply(lambda x: euros_from_cents(x))

    col1, col2, col3, col4 = st.columns(4)
    total_open = df_open["amount_eur"].sum() if not df_open.empty else 0
    overdue = 0
    if not df_open.empty:
        today_iso = date.today().isoformat()
        overdue = int((df_open["due_date"].fillna("9999-12-31") < today_iso).sum())

    col1.metric("Voci aperte", 0 if df_open.empty else len(df_open))
    col2.metric("Totale aperto", f"{total_open:.2f} €")
    col3.metric("Scadute", overdue)
    col4.metric("Persone", len(PEOPLE))

    st.divider()

    if df_open.empty:
        st.info("Nessuna voce aperta. La lavagna è pulita ✨")
        return

    fcol1, fcol2, fcol3 = st.columns([1, 1, 2])
    with fcol1:
        person = st.selectbox("Filtro persona", ["Tutti"] + PEOPLE, index=0)
    with fcol2:
        show_overdue = st.checkbox("Solo scadute", value=False)
    with fcol3:
        q = st.text_input("Cerca (descrizione)", placeholder="es. rata, medico, spesa...")

    view = person_filter_df(df_open, person)
    if show_overdue:
        today_iso = date.today().isoformat()
        view = view[view["due_date"].fillna("9999-12-31") < today_iso]
    if q.strip():
        view = view[view["description"].str.contains(q.strip(), case=False, na=False)]

    if view.empty:
        st.warning("Nessun risultato con questi filtri.")
        return

    for _, row in view.iterrows():
        txn_id = row["id"]
        debtor = row["debtor"]
        creditor = row["creditor"]
        desc = row["description"]
        cat = row["category"]
        amount = row["amount_eur"]
        due = due_badge(row["due_date"])

        c1, c2, c3, c4, c5 = st.columns([4.5, 1.5, 2.0, 1.0, 1.0])

        with c1:
            st.markdown(
                f"**{debtor} → {creditor}**  |  {desc}  \n"
                f"<span style='color: #777'>Categoria:</span> {cat}",
                unsafe_allow_html=True,
            )
        with c2:
            st.markdown(f"### {amount:.2f} €")
        with c3:
            st.write(due)
        with c4:
            if st.button("✅ Saldata", key=f"paid_{txn_id}", use_container_width=True):
                r_idx = find_row_index_by_id(txn_id)
                if r_idx:
                    now_iso = datetime.now().isoformat(timespec="seconds")
                    update_cells_in_row(r_idx, {"status": "PAID", "paid_at": now_iso})
                    st.rerun()
                else:
                    st.error("Riga non trovata.")
        with c5:
            if st.button("🗑️", key=f"del_{txn_id}", help="Elimina (solo se inserita per errore)", use_container_width=True):
                r_idx = find_row_index_by_id(txn_id)
                if r_idx:
                    delete_row(r_idx)
                    st.rerun()
                else:
                    st.error("Riga non trovata.")

        st.divider()


def page_storico():
    st.title("📚 Storico (Saldate)")

    df = sheet_to_df()
    if df.empty:
        st.info("Nessuna voce nel foglio.")
        return

    df_paid = df[df["status"] == "PAID"].copy()
    if df_paid.empty:
        st.info("Nessuna voce saldata ancora.")
        return

    df_paid["amount_eur"] = df_paid["amount_cents"].apply(lambda x: euros_from_cents(x))
    df_paid["paid_date"] = pd.to_datetime(df_paid["paid_at"], errors="coerce").dt.date
    df_paid["due_date_parsed"] = pd.to_datetime(df_paid["due_date"], errors="coerce").dt.date

    f1, f2, f3, f4, f5 = st.columns([1.2, 1.2, 1.3, 2.0, 2.3])

    with f1:
        person = st.selectbox("Persona", ["Tutti"] + PEOPLE, index=0)
    with f2:
        category = st.selectbox("Categoria", ["Tutte"] + CATEGORIES, index=0)
    with f3:
        years = sorted([d.year for d in df_paid["paid_date"].dropna().unique()], reverse=True)
        year_opt = ["Tutti"] + [str(y) for y in years] if years else ["Tutti"]
        year = st.selectbox("Anno (pagamento)", year_opt, index=0)
    with f4:
        min_d = df_paid["paid_date"].min() or date.today()
        max_d = df_paid["paid_date"].max() or date.today()
        dr = st.date_input("Range date (pagamento)", value=(min_d, max_d))
    with f5:
        q = st.text_input("Cerca", placeholder="descrizione contiene...")

    view = df_paid.copy()
    view = person_filter_df(view, person)
    if category != "Tutte":
        view = view[view["category"] == category]
    if year != "Tutti":
        y = int(year)
        view = view[view["paid_date"].apply(lambda x: x.year if pd.notna(x) else None) == y]
    if isinstance(dr, tuple) and len(dr) == 2:
        start, end = dr
        view = view[(view["paid_date"] >= start) & (view["paid_date"] <= end)]
    if q.strip():
        view = view[view["description"].str.contains(q.strip(), case=False, na=False)]

    tot = view["amount_eur"].sum() if not view.empty else 0
    st.metric("Totale nel filtro", f"{tot:.2f} €")

    out = view[
        ["debtor", "creditor", "description", "category", "amount_eur", "due_date_parsed", "paid_date"]
    ].rename(
        columns={
            "debtor": "Debitore",
            "creditor": "Creditore",
            "description": "Descrizione",
            "category": "Categoria",
            "amount_eur": "Importo (€)",
            "due_date_parsed": "Scadenza",
            "paid_date": "Data saldo",
        }
    )

    st.dataframe(out, use_container_width=True, hide_index=True)
    csv = out.to_csv(index=False).encode("utf-8")
    st.download_button("⬇️ Scarica CSV", data=csv, file_name="storico_debiti.csv", mime="text/csv")


# ---------- MAIN ----------
def main():
    sidebar_add_form()
    sidebar_notifications_box()

    page = st.sidebar.radio("Navigazione", ["Lavagna", "Storico"], index=0)
    if page == "Lavagna":
        page_lavagna()
    else:
        page_storico()

    st.sidebar.caption("Dati salvati su Google Sheet 'family_ledger'")

if __name__ == "__main__":
    main()
