"""SQLite state management for AlphaBot with Account-Level Strategies."""

import sqlite3
import json
import time
from datetime import datetime

DB_FILE = "alphabot_state.db"

# DEFAULT STRATEGY PARAMETERS (Used when a new account is detected)
DEFAULT_STRATEGY = {
    "TRIGGER_THRESHOLD_PCT": 15.0,
    "TAKE_PROFIT_MC_PCT": 5.0,
    "VWAP_CROSS_HWM_PCT": 1.0,
    "VWAP_BAND_MULTIPLIER": 0.10,
    "VOLATILITY_MAGNITUDE_MULTIPLIER": 1.5,
    "VOLATILITY_CLOSE_MULTIPLIER": 0.5,
    "PARABOLIC_VELOCITY_THRESHOLD": 2.0,
    "MAX_PARABOLIC_SQUEEZE": 0.50,
    "VWAP_BLEED_MULTIPLIER": 1.5,
    "VWAP_BLEED_TICKS": 10,
    "HARD_STOP_LOSS_MULT": 1.5,
    "HARD_STOP_LOSS_MIN_PCT": 1.5
}

# By default, we lock the non-user-specified variables so BO only tunes the requested
DEFAULT_LOCKED_VARS = [
    "TRIGGER_THRESHOLD_PCT"
]

def get_connection():
    conn = sqlite3.connect(DB_FILE, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn

def init_db():
    conn = get_connection()
    cursor = conn.cursor()
    
    # Execution & State Tracking
    cursor.execute("CREATE TABLE IF NOT EXISTS bot_state (id INTEGER PRIMARY KEY, data TEXT)")
    cursor.execute("CREATE TABLE IF NOT EXISTS execution_lock (id INTEGER PRIMARY KEY, is_locked INTEGER, timestamp REAL)")
    cursor.execute("CREATE TABLE IF NOT EXISTS chart_history (id INTEGER PRIMARY KEY, data TEXT)")
    cursor.execute("CREATE TABLE IF NOT EXISTS chart_archive (date TEXT, symphony_id TEXT, data TEXT, UNIQUE(date, symphony_id))")
    
    # NEW: Symphony-Level Strategy Storage
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS symphony_strategies (
            symphony_name TEXT PRIMARY KEY,
            parameters TEXT,
            locked_vars TEXT
        )
    """)

    # NEW: Proxy Mapping Storage
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ticker_proxy_map (
            ticker TEXT PRIMARY KEY,
            proxy_ticker TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS daily_symphony_proxy (
            symphony_name TEXT,
            proxy_ticker TEXT,
            date TEXT,
            UNIQUE(symphony_name, date)
        )
    """)

    # NEW: Autotune Runs Table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS autotune_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            symphony_name TEXT,
            num_trials INTEGER,
            pbo_score REAL,
            haircut_sortino REAL,
            results_json TEXT,
            status TEXT
        )
    """)

    cursor.execute("INSERT OR IGNORE INTO execution_lock (id, is_locked, timestamp) VALUES (1, 0, 0)")
    cursor.execute("INSERT OR IGNORE INTO bot_state (id, data) VALUES (1, '{}')")
    cursor.execute("INSERT OR IGNORE INTO chart_history (id, data) VALUES (1, '{}')")
    
    conn.commit()
    conn.close()

# --- Lock Management ---
def acquire_lock(lease_duration=60):
    conn = get_connection()
    cursor = conn.cursor()
    current_time = time.time()
    cursor.execute("SELECT is_locked, timestamp FROM execution_lock WHERE id = 1")
    row = cursor.fetchone()
    if row[0] == 1 and (current_time - row[1] < lease_duration):
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

def wipe_transient_state(state_dict):
    """Wipes transient state keys for all symphonies to prevent bleeding across sessions."""
    for s_id, s_data in state_dict.items():
        if s_id in ["account_totals", "date", "last_execution_mode", "post_mortem_run"]:
            continue
        if isinstance(s_data, dict):
            s_data["high_water_mark"] = -999.0
            s_data["shadow_hwm"] = -999.0
            s_data["highest_stop_level"] = -999.0
            s_data["prev_return"] = None
            s_data["armed"] = False
            s_data["tp_armed"] = False
            s_data["para_armed"] = False
            s_data["triggered"] = False
            s_data["breakeven_locked"] = False
            s_data["lowest_mc_seen"] = 100.0
            s_data["lock_engaged_ticks"] = 0
            s_data["lock_engaged_at"] = None
            s_data["below_lock_count_a"] = 0
            s_data["below_lock_count_b"] = 0
            s_data["below_stop_count"] = 0
            s_data["below_hard_stop_count"] = 0
            s_data["above_tp_count"] = 0
            s_data["vwap_ticks"] = 0
            s_data["hwm_hold_ticks"] = 0
            s_data["hwm_time"] = None
            s_data["mc_history"] = []
            
            # Remove any trigger-related snapshot data
            for k in ["triggered_reason", "triggered_at_return", "triggered_at_hwm", 
                      "triggered_at_stop", "triggered_at_time", "trigger_prices", 
                      "triggered_basket_snapshot"]:
                if k in s_data:
                    del s_data[k]
    return state_dict

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

def get_rolling_60day_chart(current_date_str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT date FROM chart_archive ORDER BY date DESC LIMIT 60")
    dates = [row[0] for row in cursor.fetchall()]
    if not dates:
        conn.close()
        return {}

    placeholders = ",".join("?" * len(dates))
    cursor.execute(f"SELECT date, symphony_id, data FROM chart_archive WHERE date IN ({placeholders})", dates)
    history_60d = {}
    for row in cursor.fetchall():
        date, sym_id, data_json = row[0], row[1], row[2]
        if sym_id not in history_60d:
            history_60d[sym_id] = {}
        history_60d[sym_id][date] = json.loads(data_json)
    conn.close()
    return history_60d

def log_autotune_result(date_str, symphony_name, num_trials, pbo_score, haircut_sortino, results_dict, status):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO autotune_runs 
        (date, symphony_name, num_trials, pbo_score, haircut_sortino, results_json, status)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (date_str, symphony_name, num_trials, pbo_score, haircut_sortino, json.dumps(results_dict), status))
    conn.commit()
    conn.close()

def normalize_name(name):
    return name.strip().lower()

# --- Symphony Strategy Management (NEW) ---
def get_symphony_strategy(symphony_name):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT parameters, locked_vars FROM symphony_strategies WHERE symphony_name = ?", (symphony_name,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {"params": json.loads(row[0]), "locked_vars": json.loads(row[1])}
    
    # Initialize with defaults if not found
    save_symphony_strategy(symphony_name, DEFAULT_STRATEGY, DEFAULT_LOCKED_VARS)
    return {"params": DEFAULT_STRATEGY.copy(), "locked_vars": DEFAULT_LOCKED_VARS.copy()}

def save_symphony_strategy(symphony_name, params, locked_vars):
    # Round all float parameters to 4 decimal places to prevent "decimals too long" issues
    cleaned_params = {}
    for k, v in params.items():
        if isinstance(v, float):
            if v.is_integer():
                cleaned_params[k] = int(v)
            else:
                cleaned_params[k] = round(v, 4)
        else:
            cleaned_params[k] = v

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO symphony_strategies (symphony_name, parameters, locked_vars) VALUES (?, ?, ?)",
        (symphony_name, json.dumps(cleaned_params), json.dumps(locked_vars))
    )
    conn.commit()
    conn.close()

# --- Proxy Mapping Management (NEW) ---
def get_proxy_for_ticker(ticker):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT proxy_ticker FROM ticker_proxy_map WHERE ticker = ?", (ticker,))
        row = cursor.fetchone()
        return row[0] if row else None

def save_proxy_for_ticker(ticker, proxy_ticker):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO ticker_proxy_map (ticker, proxy_ticker) VALUES (?, ?)", (ticker, proxy_ticker))
        conn.commit()

def save_daily_symphony_proxy(symphony_name, proxy_ticker, date_str):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO daily_symphony_proxy (symphony_name, proxy_ticker, date) VALUES (?, ?, ?)",
            (symphony_name, proxy_ticker, date_str)
        )
        conn.commit()


# --- Symphony Logging (NEW) ---
SYMPHONY_LOGS_FILE = "symphony_logs.json"

def get_symphony_logs(symphony_id, date_str=None):
    if date_str is None:
        date_str = datetime.utcnow().strftime("%Y-%m-%d")
    log_file = f"symphony_logs_{date_str}.json"
    try:
        with open(log_file, "r", encoding="utf-8") as f:
            logs = json.load(f)
            return logs.get(symphony_id, [])
    except (FileNotFoundError, json.JSONDecodeError):
        return []

def log_symphony_event(symphony_id, message, event_type="info", date_str=None):
    if date_str is None:
        date_str = datetime.utcnow().strftime("%Y-%m-%d")
    log_file = f"symphony_logs_{date_str}.json"
    logs = {}
    try:
        with open(log_file, "r", encoding="utf-8") as f:
            logs = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
        
    if symphony_id not in logs:
        logs[symphony_id] = []
        
    timestamp = datetime.utcnow().isoformat() + "Z"
    logs[symphony_id].append({
        "timestamp": timestamp,
        "event_type": event_type,
        "message": message
    })
    
    try:
        with open(log_file, "w", encoding="utf-8") as f:
            json.dump(logs, f)
    except Exception as e:
        print(f"Error saving symphony logs to {log_file}: {e}", flush=True)

# Initialize tables on import
init_db()

def execute_system_flush(account_id):
    import alpha_bot_execution
    import os
    import json
    
    print(f"[OVERHAUL] Commencing clean slate protocol for account: {account_id}", flush=True)
    
    # 1. Fetch the fresh active holdings payload from Composer
    live_symphonies = alpha_bot_execution.fetch_symphony_stats(account_id)
    active_live_ids = {sym["id"] for sym in live_symphonies}
    
    # 2. Load the transient state dictionary
    bot_state = load_state()
    
    # Identify ghost symphonies vs re-used symphonies
    keys_to_delete = []
    for s_id, s_data in bot_state.items():
        if s_id in ["account_totals", "date", "last_execution_mode", "post_mortem_run"]:
            continue
        if isinstance(s_data, dict) and s_data.get("account") == account_id:
            if s_id not in active_live_ids and not s_data.get("triggered"):
                keys_to_delete.append(s_id)
                
    # Purge completely deleted ghost symphonies from transient memory
    for s_id in keys_to_delete:
        sym_name = bot_state[s_id].get("name", s_id)
        print(f"  -> Completely removed ghost symphony from state memory: {sym_name} ({s_id})", flush=True)
        del bot_state[s_id]
        

                    
    save_state(bot_state)
    
    # 3. Clean up strategy settings panels for completely dropped symphony configurations
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT symphony_name FROM symphony_strategies")
    stored_names = [row[0] for row in cursor.fetchall()]
    
    state_names = {normalize_name(v.get("name", "")) for v in bot_state.values() if isinstance(v, dict)}
    for stored_name in stored_names:
        if stored_name not in state_names:
            print(f"  -> Pruning old unused strategy tuning configuration panel for: {stored_name}", flush=True)
            cursor.execute("DELETE FROM symphony_strategies WHERE symphony_name = ?", (stored_name,))
    conn.commit()
    conn.close()
    
    # 4. Invalidate the historical cache layer to force a fresh walk-forward calculation
    if os.path.exists("history_cache.json"):
        try:
            os.remove("history_cache.json")
            print("  -> Invalidated global historical cache to refresh synthetic simulations.", flush=True)
        except Exception as e:
            print(f"  -> Cache removal skipped: {e}", flush=True)
            
    # 5. Clear ONLY ghost symphonies from chart_history to prevent visualization bleed
    chart_history = load_chart_history()
    if isinstance(chart_history.get("symphonies"), dict):
        for s_id in keys_to_delete:
            if s_id in chart_history["symphonies"]:
                del chart_history["symphonies"][s_id]
        # REMOVED: Wiping active_live_ids chart history so data persists across app restarts
        save_chart_history(chart_history)
        
    # 6. Instant asset proxy initialization for newly funded symphonies
    print("  -> Initializing sector proxy maps for newly funded positions...", flush=True)
    try:
        alpha_bot_execution.run_morning_initialization()
    except Exception as e:
        print(f"  -> Asset proxy mapping skipped: {e}", flush=True)

    print(f"[OVERHAUL] Synchronization and system flush complete for account {account_id[:8]}.", flush=True)