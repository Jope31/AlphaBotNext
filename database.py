"""SQLite state management for AlphaBot with Account-Level Strategies."""

import sqlite3
import json
import time

DB_FILE = "alphabot_state.db"

# DEFAULT STRATEGY PARAMETERS (Used when a new account is detected)
DEFAULT_STRATEGY = {
    "TRIGGER_THRESHOLD_PCT": 15.0,
    "TAKE_PROFIT_MC_PCT": 5.0,
    "LOSS_ARM_PCT": 1.5,
    "MAX_SQUEEZE_FLOOR": 0.20,
    "VIX_LOW_MULT": 1.5,
    "VIX_MID_MULT": 2.0,
    "VIX_HIGH_MULT": 2.5,
    "MIN_MULTIPLIER_FLOOR": 0.5,
    "TRAILING_STOP_PCT": 1.5,
    "ENDING_STOP_PCT": 0.5,
    "BREAKEVEN_ACTIVATION_PCT": 2.0,
    "VWAP_CROSS_HWM_PCT": 1.0,
    "PARABOLIC_VELOCITY_THRESHOLD": 2.0,
    "MAX_PARABOLIC_SQUEEZE": 0.50
}

# By default, we lock the non-user-specified variables so BO only tunes the 9 requested
DEFAULT_LOCKED_VARS = [
    "TRIGGER_THRESHOLD_PCT", 
    "LOSS_ARM_PCT", 
    "TRAILING_STOP_PCT", 
    "ENDING_STOP_PCT", 
    "BREAKEVEN_ACTIVATION_PCT"
]

def get_connection():
    return sqlite3.connect(DB_FILE, timeout=10.0)

def init_db():
    conn = get_connection()
    cursor = conn.cursor()
    
    # Execution & State Tracking
    cursor.execute("CREATE TABLE IF NOT EXISTS bot_state (id INTEGER PRIMARY KEY, data TEXT)")
    cursor.execute("CREATE TABLE IF NOT EXISTS execution_lock (id INTEGER PRIMARY KEY, is_locked INTEGER, timestamp REAL)")
    cursor.execute("CREATE TABLE IF NOT EXISTS chart_history (id INTEGER PRIMARY KEY, data TEXT)")
    cursor.execute("CREATE TABLE IF NOT EXISTS vix_cache (id INTEGER PRIMARY KEY, vix_value REAL, timestamp REAL)")
    cursor.execute("CREATE TABLE IF NOT EXISTS chart_archive (date TEXT, symphony_id TEXT, data TEXT, UNIQUE(date, symphony_id))")
    
    # NEW: Account-Level Strategy Storage
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS account_strategies (
            account_id TEXT PRIMARY KEY,
            parameters TEXT,
            locked_vars TEXT
        )
    """)

    cursor.execute("INSERT OR IGNORE INTO execution_lock (id, is_locked, timestamp) VALUES (1, 0, 0)")
    cursor.execute("INSERT OR IGNORE INTO bot_state (id, data) VALUES (1, '{}')")
    cursor.execute("INSERT OR IGNORE INTO chart_history (id, data) VALUES (1, '{}')")
    cursor.execute("INSERT OR IGNORE INTO vix_cache (id, vix_value, timestamp) VALUES (1, 20.0, 0)")
    
    conn.commit()
    conn.close()

# --- Lock Management ---
def acquire_lock():
    conn = get_connection()
    cursor = conn.cursor()
    current_time = time.time()
    cursor.execute("SELECT is_locked, timestamp FROM execution_lock WHERE id = 1")
    row = cursor.fetchone()
    if row[0] == 1 and (current_time - row[1] < 60):
        conn.close()
        return False
    cursor.execute("UPDATE execution_lock SET is_locked = 1, timestamp = ? WHERE id = 1", (current_time,))
    conn.commit()
    conn.close()
    return True

def release_lock():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE execution_lock SET is_locked = 0 WHERE id = 1")
    conn.commit()
    conn.close()

# --- State Management ---
def load_state():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT data FROM bot_state WHERE id = 1")
    row = cursor.fetchone()
    conn.close()
    return json.loads(row[0]) if row else {}

def save_state(state_dict):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE bot_state SET data = ? WHERE id = 1", (json.dumps(state_dict),))
    conn.commit()
    conn.close()

# --- Chart History & Archive ---
def load_chart_history():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT data FROM chart_history WHERE id = 1")
    row = cursor.fetchone()
    conn.close()
    return json.loads(row[0]) if row else {}

def save_chart_history(chart_dict):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE chart_history SET data = ? WHERE id = 1", (json.dumps(chart_dict),))
    conn.commit()
    conn.close()

def save_chart_archive(date_str, symphony_id, data):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO chart_archive (date, symphony_id, data) VALUES (?, ?, ?)", (date_str, symphony_id, json.dumps(data)))
    conn.commit()
    conn.close()

def get_rolling_5day_chart(current_date_str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT date FROM chart_archive ORDER BY date DESC LIMIT 5")
    dates = [row[0] for row in cursor.fetchall()]
    if not dates:
        conn.close()
        return {}
    placeholders = ",".join("?" * len(dates))
    cursor.execute(f"SELECT date, symphony_id, data FROM chart_archive WHERE date IN ({placeholders})", dates)
    history_5d = {}
    for row in cursor.fetchall():
        date, sym_id, data_json = row[0], row[1], row[2]
        if sym_id not in history_5d:
            history_5d[sym_id] = {}
        history_5d[sym_id][date] = json.loads(data_json)
    conn.close()
    return history_5d

# --- VIX Cache ---
def get_vix_cache():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT vix_value, timestamp FROM vix_cache WHERE id = 1")
    row = cursor.fetchone()
    conn.close()
    return {"vix_value": row[0], "timestamp": row[1]} if row else None

def set_vix_cache(vix_value):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE vix_cache SET vix_value = ?, timestamp = ? WHERE id = 1", (vix_value, time.time()))
    conn.commit()
    conn.close()

# --- Account Strategy Management (NEW) ---
def get_account_strategy(account_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT parameters, locked_vars FROM account_strategies WHERE account_id = ?", (account_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {"params": json.loads(row[0]), "locked_vars": json.loads(row[1])}
    
    # Initialize with defaults if not found
    save_account_strategy(account_id, DEFAULT_STRATEGY, DEFAULT_LOCKED_VARS)
    return {"params": DEFAULT_STRATEGY.copy(), "locked_vars": DEFAULT_LOCKED_VARS.copy()}

def get_all_account_strategies():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT account_id, parameters, locked_vars FROM account_strategies")
    rows = cursor.fetchall()
    conn.close()
    strategies = {}
    for r in rows:
        strategies[r[0]] = {"params": json.loads(r[1]), "locked_vars": json.loads(r[2])}
    return strategies

def save_account_strategy(account_id, params, locked_vars):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO account_strategies (account_id, parameters, locked_vars) VALUES (?, ?, ?)",
        (account_id, json.dumps(params), json.dumps(locked_vars))
    )
    conn.commit()
    conn.close()

# Initialize tables on import
init_db()