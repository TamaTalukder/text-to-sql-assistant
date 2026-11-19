import re
import time
import sqlite3
import tempfile
import urllib.request
import os

import pandas as pd
import streamlit as st
from google import genai


# ---------------- Settings ----------------
DB_PATH = "northwind.db"
MODEL = "gemini-3.5-flash-lite"
SQLITE_MAGIC = b"SQLite format 3\x00"


# ---------------- Database access ----------------
def open_northwind():
    # read-only connection: the database itself refuses to change
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, check_same_thread=False)


def ensure_database_exists():
    if not os.path.exists(DB_PATH):
        url = "https://raw.githubusercontent.com/jpwhite3/northwind-SQLite3/main/dist/northwind.db"
        urllib.request.urlretrieve(url, DB_PATH)


# ---------------- Schema helpers (same as Step 2) ----------------
def get_tables_with_data(conn):
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name != 'sqlite_sequence';")
    all_tables = [row[0] for row in cursor.fetchall()]
    tables_with_data = []
    for table in all_tables:
        cursor.execute(f'SELECT COUNT(*) FROM "{table}";')
        if cursor.fetchone()[0] > 0:
            tables_with_data.append(table)
    return tables_with_data


def get_schema_text(conn, tables):
    cursor = conn.cursor()
    schema_lines = []
    for table in tables:
        cursor.execute(f'PRAGMA table_info("{table}");')
        columns = cursor.fetchall()
        col_descriptions = []
        for col in columns:
            col_name, col_type, is_pk = col[1], col[2], col[5]
            desc = f"{col_name} ({col_type})"
            if is_pk:
                desc += " [PRIMARY KEY]"
            col_descriptions.append(desc)
        schema_lines.append(f'TABLE "{table}":')
        schema_lines.append("  " + ", ".join(col_descriptions))
        schema_lines.append("")
    return "\n".join(schema_lines)


def get_relationships_text(conn, tables):
    cursor = conn.cursor()
    lines = []
    for table in tables:
        cursor.execute(f'PRAGMA foreign_key_list("{table}");')
        for fk in cursor.fetchall():
            table_name = f'"{table}"' if " " in table else table
            lines.append(f'- {table_name}.{fk[3]} -> {fk[2]}.{fk[4]}')
    if not lines:
        return ""
    return "Relationships (foreign keys):\n" + "\n".join(lines)


def get_date_context(conn, tables):
    cursor = conn.cursor()
    date_notes = []
    for table in tables:
        cursor.execute(f'PRAGMA table_info("{table}");')
        for col in cursor.fetchall():
            col_name = col[1]
            if "date" in col_name.lower():
                cursor.execute(f'SELECT MAX("{col_name}") FROM "{table}";')
                max_val = cursor.fetchone()[0]
                if max_val:
                    date_notes.append(f"- {table}.{col_name}: most recent value is '{max_val}'")
    if not date_notes:
        return ""
    return "\nDate reference points (use these instead of today's real date):\n" + "\n".join(date_notes)


# ---------------- Prompt and AI calls (same as Steps 3 and 4) ----------------
def build_system_prompt(conn):
    tables = get_tables_with_data(conn)
    full_schema = get_schema_text(conn, tables)
    relationships = get_relationships_text(conn, tables)
    if relationships:
        full_schema += "\n" + relationships
    date_context = get_date_context(conn, tables)

    return f"""You are a SQL assistant for a SQLite database.

Your job: convert a plain-English question into a single, valid SQLite SELECT query.

DATABASE SCHEMA:
{full_schema}
{date_context}

RULES:
1. Only write SELECT queries. Never write INSERT, UPDATE, DELETE, DROP, ALTER, or anything else that changes data.
2. Use exactly the table and column names shown above (case-sensitive).
3. If a table name has a space in it, wrap it in double quotes.
4. Return ONLY the raw SQL. No explanation, no markdown fences, no extra text.
5. If the question can't be answered with this schema, return exactly: NO_QUERY_POSSIBLE
6. If a question needs two separate results combined (like "highest and lowest"), use UNION ALL with each half wrapped as its own subquery, like: SELECT * FROM (SELECT ... ORDER BY ... LIMIT 1) UNION ALL SELECT * FROM (SELECT ... ORDER BY ... LIMIT 1). Never write two full statements separated by a semicolon.
"""


def call_gemini_with_retry(client, model, contents, config=None, max_retries=5):
    for attempt in range(max_retries):
        try:
            if config:
                return client.models.generate_content(model=model, contents=contents, config=config)
            return client.models.generate_content(model=model, contents=contents)
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            message = str(e)
            if "429" in message or "RESOURCE_EXHAUSTED" in message:
                # free plan limit: wait as long as Google asks, then try again
                found = re.search(r"retry in ([\d.]+)s", message)
                wait_seconds = (float(found.group(1)) + 3) if found else 30
                time.sleep(min(wait_seconds, 90))
            else:
                time.sleep(5 * (attempt + 1))


def question_to_sql(question, system_prompt, client):
    response = call_gemini_with_retry(client, MODEL, question, config={"system_instruction": system_prompt})
    sql = response.text.strip()
    sql = sql.replace("```sql", "").replace("```", "").strip()
    return sql


# ---------------- Safety check (same as Step 5) ----------------
FORBIDDEN_WORDS = ["DROP", "DELETE", "UPDATE", "INSERT", "ALTER", "TRUNCATE", "REPLACE", "ATTACH", "DETACH", "PRAGMA", "CREATE"]


def is_safe_select(sql: str) -> bool:
    if not sql or not sql.strip():
        return False
    cleaned = sql.strip()
    if cleaned == "NO_QUERY_POSSIBLE":
        return False
    stripped = cleaned.rstrip(";").strip()
    if ";" in stripped:
        return False
    if not re.match(r"^\s*SELECT\b", stripped, re.IGNORECASE):
        return False
    tokens = re.findall(r"[A-Za-z]+", stripped.upper())
    if any(word in FORBIDDEN_WORDS for word in tokens):
        return False
    return True


# ---------------- Run, fix and explain (same as Steps 6 to 8) ----------------
def run_query(sql, conn):
    try:
        df = pd.read_sql_query(sql, conn)
        return df, None
    except Exception as e:
        return None, str(e)


# give the AI one chance to correct its own SQL
def fix_sql(question, bad_sql, error_message, system_prompt, client):
    prompt = f"""The user asked: "{question}"

You wrote this SQL:
{bad_sql}

SQLite gave this error:
{error_message}

Write a corrected SQL query. Follow the same rules as before. Return ONLY the raw SQL."""
    response = call_gemini_with_retry(client, MODEL, prompt, config={"system_instruction": system_prompt})
    sql = response.text.strip()
    sql = sql.replace("```sql", "").replace("```", "").strip()
    return sql


# tell the AI (and the user) when the result is longer than what is sent
MAX_ROWS_FOR_AI = 20


def explain_answer(question, df, client):
    total_rows = len(df)
    preview_df = df.head(MAX_ROWS_FOR_AI)
    result_text = preview_df.to_string(index=False)

    cut_off_note = ""
    if total_rows > MAX_ROWS_FOR_AI:
        cut_off_note = (
            f"\nIMPORTANT: the full result has {total_rows} rows, but only the first "
            f"{MAX_ROWS_FOR_AI} are shown above. Say clearly that the list is partial "
            f"(first {MAX_ROWS_FOR_AI} of {total_rows}).\n"
        )

    prompt = f"""A user asked: "{question}"

Here is the data that answers it:
{result_text}
{cut_off_note}
Write the answer following these formatting rules:
- If the answer involves MULTIPLE items (a list of customers, products, etc.), format it as a bulleted markdown list, one item per line.
- If the answer is a SINGLE value or fact, write it as 1 plain sentence.
- Format dollar amounts with a $ sign, commas for thousands, and 2 decimal places.
- Don't mention SQL, tables, or dataframes."""
    response = call_gemini_with_retry(client, MODEL, prompt)
    return response.text.strip()


def get_result(question, conn, client):
    """Returns (df, sql, error, retried)."""
    system_prompt = build_system_prompt(conn)
    sql = question_to_sql(question, system_prompt, client)

    if not is_safe_select(sql):
        return None, sql, f"Blocked or invalid query: {sql}", False

    df, run_error = run_query(sql, conn)
    retried = False
    if run_error:
        retried = True
        fixed_sql = fix_sql(question, sql, run_error, system_prompt, client)
        if not is_safe_select(fixed_sql):
            return None, fixed_sql, f"The fixed query was blocked: {fixed_sql}", True
        sql = fixed_sql
        df, run_error = run_query(sql, conn)
        if run_error:
            return None, sql, f"SQL failed even after one retry: {run_error}", True

    return df, sql, None, retried


def answer_question(question, conn, client):
    df, sql, error, retried = get_result(question, conn, client)
    if error:
        return None, None, sql, error
    if df.empty:
        return df, "No data matched that question.", sql, None
    answer = explain_answer(question, df, client)
    return df, answer, sql, None


# ---------------- UPLOAD: load your own SQLite or CSV file (read-only) ----------------
def load_uploaded_db(file_bytes):
    if file_bytes[:16] != SQLITE_MAGIC:
        return None, "That doesn't look like a valid SQLite database file."
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.write(file_bytes)
    tmp.close()
    try:
        # read-only
        conn = sqlite3.connect(f"file:{tmp.name}?mode=ro", uri=True, check_same_thread=False)
        tables = get_tables_with_data(conn)
        if not tables:
            return None, "This database file has no tables with data in it."
        return conn, None
    except Exception as e:
        return None, f"Couldn't open this database file: {e}"


def load_uploaded_csv(uploaded_file):
    try:
        first_line = uploaded_file.readline()
        if isinstance(first_line, bytes):
            first_line = first_line.decode("utf-8", errors="strict")
        raw_columns = [c.strip() for c in first_line.strip().split(",")]
        uploaded_file.seek(0)
    except Exception:
        return None, "Couldn't read this file. Please upload a clean CSV with a proper header row."

    if len(raw_columns) != len(set(raw_columns)):
        return None, "This file has duplicate column names. Please upload a clean CSV with unique headers."

    try:
        df = pd.read_csv(uploaded_file)
    except Exception:
        return None, "Couldn't read this file. Please upload a clean CSV with a proper header row."

    if df.empty:
        return None, "This file has no data rows."

    try:
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        df.to_sql("uploaded_data", conn, index=False)
        # after loading, only reading is allowed
        conn.execute("PRAGMA query_only = ON")
        return conn, None
    except Exception as e:
        return None, f"Couldn't load this data: {e}"


# ---------------- STREAMLIT UI ----------------
st.set_page_config(page_title="Text-to-SQL Assistant (Northwind)", page_icon="🗄️")
st.title("🗄️ Text-to-SQL Assistant")
st.caption("Ask a plain-English question about the Northwind business database — or upload your own.")

# Northwind loads automatically every time — no upload needed to try it
if "conn" not in st.session_state:
    ensure_database_exists()
    st.session_state.conn = open_northwind()
    st.session_state.db_label = "Northwind (default)"
    st.session_state.is_northwind = True



with st.sidebar:
    typed_key = st.text_input("Gemini API key (optional)", type="password",
                              help="Leave empty to use the demo key, or paste your own. Free, no credit card - get one at aistudio.google.com/apikey")
    # the demo key stays on the server and is never sent to the visitor's browser
    api_key = typed_key or os.environ.get("GEMINI_API_KEY", "")

    st.markdown("---")
    st.subheader("📊 Active dataset")
    dataset_label_box = st.empty()   # filled in below, after the upload / reset code has run

    st.markdown("---")
    st.subheader("ℹ️ About this project")
    st.markdown("""
This tool turns plain-English questions into real SQL, runs it safely
against a database, and explains the answer in plain English.

**How it works:**
1. Reads the database's real structure automatically
2. Gemini writes the SQL
3. Every query is checked — only SELECT allowed, and the database is opened read-only
4. If the SQL fails, the AI gets one chance to fix it
5. The result gets explained back in plain English

Built with Python, SQLite, Streamlit, and the Gemini API.
""")



with st.expander("📁 Use your own data instead (optional)"):
    st.caption("Upload a SQLite (.db) file or a CSV with a proper header row. Clean files work best. Please don't upload confidential data: your question, the table structure and a sample of the results are sent to Gemini.")

    uploaded_file = st.file_uploader("Upload", type=["db", "sqlite", "sqlite3", "csv"])

    # load a file only once, so "Reset to Northwind" is not undone by the next click
    file_id = (uploaded_file.name, uploaded_file.size) if uploaded_file is not None else None
    if uploaded_file is not None and st.session_state.get("loaded_file") != file_id:
        if uploaded_file.name.endswith(".csv"):
            new_conn, error = load_uploaded_csv(uploaded_file)
        else:
            new_conn, error = load_uploaded_db(uploaded_file.getvalue())

        if error:
            st.error(error)
        else:
            st.session_state.conn = new_conn
            st.session_state.db_label = uploaded_file.name
            st.session_state.is_northwind = False
            st.session_state.loaded_file = file_id
            st.success(f"Using: {uploaded_file.name}")

    if st.button("Reset to Northwind (default)"):
        ensure_database_exists()
        st.session_state.conn = open_northwind()
        st.session_state.db_label = "Northwind (default)"
        st.session_state.is_northwind = True

    st.caption(f"Active dataset: **{st.session_state.db_label}**")

    if st.session_state.is_northwind:
        st.markdown("---")
        st.markdown("**Try asking:**")
        st.markdown("- Which employee sold the most last quarter?")
        st.markdown("- What are the top 3 products by revenue?")
        st.markdown("- Which category has the most products?")

dataset_label_box.info(f"**{st.session_state.db_label}**")

if "question_text" not in st.session_state:
    st.session_state.question_text = ""

def set_question(q):
    st.session_state.question_text = q

if st.session_state.is_northwind:
    st.markdown("**Try one of these:**")
    col1, col2, col3 = st.columns(3)
    with col1:
        st.button("Top employee", on_click=set_question, args=("Which employee sold the most last quarter?",))
    with col2:
        st.button("Top products", on_click=set_question, args=("What are the top 3 products by revenue?",))
    with col3:
        st.button("Top category", on_click=set_question, args=("Which category has the most products?",))

question = st.text_input("Your question:", value=st.session_state.question_text,
                          placeholder="e.g. Which employee sold the most last quarter?")
ask_clicked = st.button("Ask")



if ask_clicked:
    if not api_key:
        st.error("Please enter your Gemini API key in the sidebar.")
    elif not question.strip():
        st.warning("Type a question first.")
    else:
        client = genai.Client(api_key=api_key)
        with st.spinner("Thinking..."):
            df, answer, sql, error = answer_question(question, st.session_state.conn, client)
        if error:
            st.error(error)
        else:
            st.success(answer)
            if df is not None and len(df) > MAX_ROWS_FOR_AI:
                st.caption(f"The explanation above uses the first {MAX_ROWS_FOR_AI} of {len(df)} rows. "
                           f"Open the section below to see all rows.")
            with st.expander("See the generated SQL and raw result"):
                st.code(sql, language="sql")
                if df is not None and not df.empty:
                    st.dataframe(df)
