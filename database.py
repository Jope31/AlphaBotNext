"""SQLite Database Manager for AlphaBot State & Concurrency."""

import sqlite3
import json
import time

DB_PATH = "alphabot.db"

def init_db():
    """Initializes the SQLite database and tables."""
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        # Core State Tables
        conn.execute("CREATE TABLE IF NOT EXISTS bot_state (key TEXT PRIMARY KEY, data TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS chart_history (date TEXT, data TEXT)")
        
        # Concurrency & Cache Tables
        conn.execute("CREATE TABLE IF NOT EXISTS locks (lock_name TEXT PRIMARY KEY, timestamp REAL)")
        conn.execute("CREATE TABLE IF NOT EXISTS system_cache (key TEXT PRIMARY KEY, data TEXT, timestamp REAL)")

def acquire_lock():
    """Acquires an execution lock via SQLite to prevent overlapping minute runs."""
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        cur = conn.execute("SELECT timestamp FROM locks WHERE lock_name='execution'")
        row = cur.fetchone()
        current_time = time.time()
        
        # If lock exists and is younger than 3 minutes (180s), block execution
        if row and (current_time - row[0]) < 180:
            return False 
        
        # Otherwise, claim/overwrite the lock atomically
        conn.execute("INSERT OR REPLACE INTO locks (lock_name, timestamp) VALUES ('execution', ?)", (current_time,))
        return True

def release_lock():
    """Releases the execution lock."""
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        conn.execute("DELETE FROM locks WHERE lock_name='execution'")

def load_state():
    """Loads the core dictionary state."""
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        cur = conn.execute("SELECT data FROM bot_state WHERE key='current_state'")
        row = cur.fetchone()
        return json.loads(row[0]) if row else {}

def save_state(state):
    """Saves the core dictionary state atomically."""
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        conn.execute("INSERT OR REPLACE INTO bot_state (key, data) VALUES (?, ?)",
                     ('current_state', json.dumps(state)))

def load_chart_history():
    """Loads the chart timeseries dictionary."""
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        cur = conn.execute("SELECT data FROM chart_history WHERE date='current_chart'")
        row = cur.fetchone()
        return json.loads(row[0]) if row else {}

def save_chart_history(history):
    """Saves the chart timeseries dictionary."""
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        conn.execute("INSERT OR REPLACE INTO chart_history (date, data) VALUES (?, ?)",
                     ('current_chart', json.dumps(history, separators=(",", ":"))))

def get_vix_cache():
    """Retrieves cached VIX data to prevent Yahoo Finance API bans."""
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        cur = conn.execute("SELECT data, timestamp FROM system_cache WHERE key='vix'")
        row = cur.fetchone()
        if row:
            return {"vix_value": float(row[0]), "timestamp": row[1]}
        return None

def set_vix_cache(vix_value):
    """Updates the VIX cache."""
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        conn.execute("INSERT OR REPLACE INTO system_cache (key, data, timestamp) VALUES (?, ?, ?)",
                     ('vix', str(vix_value), time.time()))

# Auto-initialize tables when this module is imported
init_db()