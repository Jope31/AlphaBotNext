import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo
import alpha_bot_execution
import database
import math_engine

@pytest.fixture
def base_bot_state():
    return {
        "date": "2026-07-02",
        "account_totals": {},
        "account_performance": {},
        "post_mortem_run": None,
        "last_execution_mode": True
    }

@pytest.fixture
def mock_symphony_cache():
    return {
        "fake-account-id": [
            {
                "id": "fake-symphony-id",
                "name": "v5 USD Autotuned Flat (with FR) | NOVA",
                "value": 10000.0,
                "current_value": 10000.0,
                "last_percent_change": -0.10,  # -10% return
                "holdings": [
                    {"ticker": "SOXL", "weight": 0.5},
                    {"ticker": "SOXX", "weight": 0.5}
                ]
            }
        ]
    }

@patch("alpha_bot_execution.LIVE_EXECUTION", True)
@patch("alpha_bot_execution.COMPOSER_KEY_ID", "fake_key")
@patch("alpha_bot_execution.ALPACA_KEY", "fake_key")
@patch("alpha_bot_execution.ACCOUNT_UUIDS", ["fake-account-id"])
@patch("alpha_bot_execution.ACCOUNT_ENABLED_MAP", {"fake-account-id": True})
@patch("database.acquire_lock", return_value=True)
@patch("database.release_lock")
def test_hard_stop_loss_trigger(mock_release, mock_acquire, mock_symphony_cache, base_bot_state):
    # Set up time to 10:05 AM Eastern Time
    fake_now = datetime(2026, 7, 2, 10, 5, tzinfo=ZoneInfo("US/Eastern"))
    
    # Mock data providers
    state = base_bot_state.copy()
    
    # Strategy settings: Vol-Scaled Hard Stop (1.5x vol, min 1.5%)
    strategy_params = {
        "params": {
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
        },
        "locked_vars": []
    }

    # Volatility is 5%, meaning stop-loss is at -7.5% return.
    # Return is -10.0%, which breaches the -7.5% stop.
    with patch("alpha_bot_execution.get_current_et", return_value=fake_now), \
         patch("alpha_bot_execution.fetch_symphony_stats", return_value=mock_symphony_cache["fake-account-id"]), \
         patch("alpha_bot_execution.fetch_alpaca_history", return_value={"2026-07-02": {"SPY": {"daily_ret": 0.0}}}), \
         patch("alpha_bot_execution.fetch_intraday_vwaps", return_value={}), \
         patch("database.load_state", return_value=state), \
         patch("database.save_state") as mock_save_state, \
         patch("database.load_chart_history", return_value={"symphonies": {}, "date": "2026-07-02"}), \
         patch("database.save_chart_history"), \
         patch("database.get_symphony_strategy", return_value=strategy_params), \
         patch("math_engine.calculate_20d_vol", return_value=5.0), \
         patch("math_engine.calculate_14d_vwatr_pct", return_value=5.0), \
         patch("math_engine.run_monte_carlo", return_value=(100.0, 0.0, 0.0)), \
         patch("database.log_symphony_event") as mock_log_event, \
         patch("alpha_bot_execution.execute_sell_to_cash") as mock_sell:
        
        # Patch sys.argv to bypass time bounds checking if needed, or rely on force flag
        with patch("sys.argv", ["main.py"]):
            
            # --- Tick 1 ---
            alpha_bot_execution.main()
            assert state["fake-symphony-id"]["below_hard_stop_count"] == 1
            assert not state["fake-symphony-id"]["triggered"]
            mock_sell.assert_not_called()
            
            # --- Tick 2 ---
            alpha_bot_execution.main()
            assert state["fake-symphony-id"]["below_hard_stop_count"] == 2
            assert not state["fake-symphony-id"]["triggered"]
            mock_sell.assert_not_called()
            
            # --- Tick 3 (Should Trigger Exit) ---
            alpha_bot_execution.main()
            assert state["fake-symphony-id"]["below_hard_stop_count"] == 3
            mock_sell.assert_called_once_with("fake-symphony-id", "fake-account-id", state, "fake-symphony-id")
            
            # Verify event was logged
            mock_log_event.assert_any_call("fake-symphony-id", "HARD STOP LOSS HIT FOR v5 USD Autotuned Flat (with FR) | NOVA. Level: -7.50", "triggered", "2026-07-02")


@patch("alpha_bot_execution.LIVE_EXECUTION", True)
@patch("alpha_bot_execution.COMPOSER_KEY_ID", "fake_key")
@patch("alpha_bot_execution.ALPACA_KEY", "fake_key")
@patch("alpha_bot_execution.ACCOUNT_UUIDS", ["fake-account-id"])
@patch("alpha_bot_execution.ACCOUNT_ENABLED_MAP", {"fake-account-id": True})
@patch("database.acquire_lock", return_value=True)
@patch("database.release_lock")
def test_hard_stop_loss_min_floor(mock_release, mock_acquire, mock_symphony_cache, base_bot_state):
    # Set up time to 10:05 AM
    fake_now = datetime(2026, 7, 2, 10, 5, tzinfo=ZoneInfo("US/Eastern"))
    
    state = base_bot_state.copy()
    
    # Low-volatility strategy: vol is 0.5%. 1.5x vol is 0.75%.
    # But min floor is 1.5%. So stop-loss is at -1.5% return.
    strategy_params = {
        "params": {
            "HARD_STOP_LOSS_MULT": 1.5,
            "HARD_STOP_LOSS_MIN_PCT": 1.5
        },
        "locked_vars": []
    }

    # Tick return is -1.0%. This is below 1.5x vol (-0.75%) but ABOVE the -1.5% minimum floor.
    # It should NOT increment the tick counter.
    with patch("alpha_bot_execution.get_current_et", return_value=fake_now), \
         patch("alpha_bot_execution.fetch_symphony_stats", return_value=mock_symphony_cache["fake-account-id"]), \
         patch("alpha_bot_execution.fetch_alpaca_history", return_value={"2026-07-02": {"SPY": {"daily_ret": 0.0}}}), \
         patch("alpha_bot_execution.fetch_intraday_vwaps", return_value={}), \
         patch("database.load_state", return_value=state), \
         patch("database.save_state"), \
         patch("database.load_chart_history", return_value={"symphonies": {}, "date": "2026-07-02"}), \
         patch("database.save_chart_history"), \
         patch("database.get_symphony_strategy", return_value=strategy_params), \
         patch("math_engine.calculate_20d_vol", return_value=0.5), \
         patch("math_engine.calculate_14d_vwatr_pct", return_value=0.5), \
         patch("math_engine.run_monte_carlo", return_value=(100.0, 0.0, 0.0)), \
         patch("alpha_bot_execution.execute_sell_to_cash") as mock_sell:
        
        # Modify Return to -1.0%
        mock_symphony_cache["fake-account-id"][0]["last_percent_change"] = -0.01
        
        with patch("sys.argv", ["main.py"]):
            alpha_bot_execution.main()
            # Counter should remain 0 because -1.0% return does not breach the -1.5% minimum floor
            assert state["fake-symphony-id"]["below_hard_stop_count"] == 0
            mock_sell.assert_not_called()
            
            # Now drop return to -2.0% (breaching -1.5% floor)
            mock_symphony_cache["fake-account-id"][0]["last_percent_change"] = -0.02
            alpha_bot_execution.main()
            assert state["fake-symphony-id"]["below_hard_stop_count"] == 1


@patch("alpha_bot_execution.LIVE_EXECUTION", True)
@patch("alpha_bot_execution.COMPOSER_KEY_ID", "fake_key")
@patch("alpha_bot_execution.ALPACA_KEY", "fake_key")
@patch("alpha_bot_execution.ACCOUNT_UUIDS", ["fake-account-id"])
@patch("alpha_bot_execution.ACCOUNT_ENABLED_MAP", {"fake-account-id": True})
@patch("database.acquire_lock", return_value=True)
@patch("database.release_lock")
def test_time_locked_vwap_trigger(mock_release, mock_acquire, mock_symphony_cache, base_bot_state):
    # Set up time to 10:05 AM (elapsed_mins = 35 mins)
    fake_now = datetime(2026, 7, 2, 10, 5, tzinfo=ZoneInfo("US/Eastern"))
    
    state = base_bot_state.copy()
    
    # VWAP parameters. HWM threshold is 1.0% return.
    # Set HARD_STOP_LOSS_MULT to a very high value (100.0) so it doesn't trigger hard stop-loss.
    strategy_params = {
        "params": {
            "VWAP_CROSS_HWM_PCT": 1.0,
            "VWAP_BAND_MULTIPLIER": 0.10,
            "HARD_STOP_LOSS_MULT": 100.0,
            "HARD_STOP_LOSS_MIN_PCT": 1.5
        },
        "locked_vars": []
    }

    # Symphony return is -10.1%, HWM is -10% (safe_hwm = -10.0%).
    # We drop return slightly below safe_hwm (-10.1%) to satisfy current_return < safe_hwm.
    # Set VWAP diff so it breaches buffer: return is -10.1% while VWAP is -5.0%.
    # Weighted VWAP diff is (return - vwap) = -5.1% (or -0.051).
    # Volatility is 1.0%, so buffer is -0.1%. Diff (-5.1%) is below buffer (-0.1%).
    with patch("alpha_bot_execution.get_current_et", return_value=fake_now), \
         patch("alpha_bot_execution.fetch_symphony_stats", return_value=mock_symphony_cache["fake-account-id"]), \
         patch("database.load_state", return_value=state), \
         patch("database.save_state"), \
         patch("database.load_chart_history", return_value={"symphonies": {}, "date": "2026-07-02"}), \
         patch("database.save_chart_history"), \
         patch("database.get_symphony_strategy", return_value=strategy_params), \
         patch("math_engine.calculate_20d_vol", return_value=1.0), \
         patch("math_engine.calculate_14d_vwatr_pct", return_value=1.0), \
         patch("math_engine.run_monte_carlo", return_value=(100.0, 0.0, 0.0)), \
         patch("database.log_symphony_event") as mock_log_event, \
         patch("alpha_bot_execution.execute_sell_to_cash") as mock_sell:
        
        # Return is -10.1%, HWM is set in state to -10.0% (so safe_hwm = -10.0%)
        mock_symphony_cache["fake-account-id"][0]["last_percent_change"] = -0.101
        state["fake-symphony-id"] = {
            "account": "fake-account-id",
            "name": "v5 USD Autotuned Flat (with FR) | NOVA",
            "high_water_mark": -10.0,
            "shadow_hwm": -10.0,
            "prev_return": -10.0,
            "vwap_ticks": 0,
            "below_hard_stop_count": 0,
            "triggered": False
        }
        
        # Mock weighted_vwap_diff to be -0.06 (meaning return is 6% below VWAP)
        # We mock valid_vwap_weight > 0.5 (say 1.0)
        # We mock live_vwaps to achieve this!
        # Holdings: SOXL (weight 0.5), SOXX (weight 0.5)
        # Let's say historical closes are 100 for both.
        # Live VWAPs are 90 for both.
        # Then (live_vwap - close) / close = -10% = -0.10.
        # weighted_vwap_diff = 0.5 * (-0.10) + 0.5 * (-0.10) = -0.10.
        # valid_vwap_weight = 0.5 + 0.5 = 1.0.
        # This perfectly satisfies the conditions!
        live_vwaps_mock = {
            "SOXL": {"vwap": 90.0, "last_price": 84.0},
            "SOXX": {"vwap": 90.0, "last_price": 84.0}
        }
        
        with patch("alpha_bot_execution.fetch_intraday_vwaps", return_value=live_vwaps_mock), \
             patch("alpha_bot_execution.fetch_alpaca_history", return_value={
                 "2026-07-02": {
                     "SOXL": {"c": 100.0, "close": 100.0, "daily_ret": 0.0},
                     "SOXX": {"c": 100.0, "close": 100.0, "daily_ret": 0.0},
                     "SPY": {"daily_ret": 0.0}
                 }
             }), \
             patch("sys.argv", ["main.py"]):
             
            # --- Tick 1 ---
            alpha_bot_execution.main()
            assert state["fake-symphony-id"]["vwap_ticks"] == 1
            mock_sell.assert_not_called()
            
            # --- Tick 2 ---
            alpha_bot_execution.main()
            assert state["fake-symphony-id"]["vwap_ticks"] == 2
            mock_sell.assert_not_called()
            
            # --- Tick 3 (Trigger) ---
            alpha_bot_execution.main()
            assert state["fake-symphony-id"]["vwap_ticks"] == 3
            mock_sell.assert_called_once_with("fake-symphony-id", "fake-account-id", state, "fake-symphony-id")
            
            # Verify event logged
            mock_log_event.assert_any_call("fake-symphony-id", "VWAP BREAKDOWN HIT FOR v5 USD Autotuned Flat (with FR) | NOVA. Level: -10.00", "triggered", "2026-07-02")
