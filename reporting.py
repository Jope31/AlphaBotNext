import os
import json
import time
import requests
import database
import glob
from datetime import datetime, timedelta

def generate_eod_snapshot(bot_state, current_date_str, is_post_rebalance=False, discord_webhook_url=None, live_prices=None):
    """Generates a two-stage daily post-mortem JSON snapshot and handles Discord alerts."""
    report_file = f"post_mortem_{current_date_str}.json"

    if not is_post_rebalance:
        # STAGE 1 (15:54 ET): Freeze Math & Shadow Returns
        if os.path.exists(report_file):
            return

        print(f"  -> Generating Stage 1 Post-Mortem (Locking Math): {report_file}", flush=True)

        report = {
            "date": current_date_str,
            "summary": {
                "total_monitored": 0,
                "total_triggered": 0,
                "positive_guard_alpha_count": 0,
            },
            "tomorrow_target_holdings": {"STATUS": "Pending Composer Rebalance"},
            "triggers": [],
            "daily_pnl": [],
        }

        for sym_id, sym in bot_state.items():
            if not isinstance(sym, dict):
                continue
                
            # Exclude system dictionaries (like account_totals) and circuit-broken ghost symphonies
            if "name" not in sym or sym.get("removed_by_user"):
                continue

            report["summary"]["total_monitored"] += 1

            is_triggered = sym.get("triggered", False)
            val = sym.get("current_value", 0.0)
            f_ret = sym.get("triggered_at_return", 0.0)
            live_ret = sym.get("current_return", 0.0)

            shadow_ret = live_ret if is_triggered else live_ret
            strat_ret = f_ret if is_triggered else live_ret

            report["daily_pnl"].append({
                "symphony_id": sym_id,
                "account_id": sym.get("account", "Unknown"),
                "value": val,
                "strat_ret": strat_ret,
                "held_ret": shadow_ret
            })

            if sym.get("triggered"):
                report["summary"]["total_triggered"] += 1

                f_ret = sym.get("triggered_at_return", 0.0)
                f_hwm = sym.get("triggered_at_hwm", sym.get("high_water_mark", 0.0))

                # Pull the live-tracked shadow values directly from the Ghost Symphony bot state
                shadow_return = sym.get("current_return", f_ret)
                
                # FIX: Force calculate the true final shadow return if live_prices are available
                if live_prices:
                    trigger_prices = sym.get("trigger_prices", {})
                    holdings = sym.get("triggered_basket_snapshot", [])
                    if trigger_prices and holdings:
                        post_trigger_move = 0.0
                        for h in holdings:
                            t = h.get("ticker")
                            alloc = h.get("weight", h.get("allocation", 0.0))
                            if t in trigger_prices and t in live_prices:
                                p_start = trigger_prices[t]
                                p_now = live_prices[t].get("last_price", 0.0)
                                if p_start > 0:
                                    post_trigger_move += alloc * ((p_now - p_start) / p_start)
                        shadow_return = f_ret + (post_trigger_move * 100.0)

                shadow_hwm = sym.get("shadow_hwm", f_hwm)
                
                guard_alpha = f_ret - shadow_return
                sym_val = sym.get("current_value", 0.0)
                saved_dollars = (sym_val * (guard_alpha / 100.0))

                if guard_alpha > 0:
                    report["summary"]["positive_guard_alpha_count"] += 1

                if f_ret == sym.get("triggered_at_stop"):
                    exit_reason = "Take-Profit"
                elif sym.get("triggered_reason"):
                    exit_reason = sym.get("triggered_reason")
                else:
                    exit_reason = "Trailing Stop"

                # Fetch strategy parameters for this specific symphony
                raw_name = sym.get("name", "Unknown")
                normalized_name = database.normalize_name(raw_name)
                strat = database.get_symphony_strategy(normalized_name)
                params = strat.get("params", {})

                report["triggers"].append(
                    {
                        "symphony_name": sym.get("name", "Unknown"),
                        "symphony_value": round(sym_val, 2),
                        "account_id": sym.get("account", "Unknown"),
                        "exit_reason": exit_reason,
                        "exit_return": round(f_ret, 2),
                        "attempted_trigger_level": round(sym.get("triggered_at_stop", 0.0), 2),
                        "shadow_return": round(shadow_return, 2),
                        "shadow_hwm": round(shadow_hwm, 2),
                        "saved_pct_guard_alpha": round(guard_alpha, 2),
                        "saved_dollars": round(saved_dollars, 2),
                        "hwm_at_trigger": round(f_hwm, 2),
                        "time_triggered": sym.get("triggered_at_time", ""),
                        "symphony_vol": round(sym.get("symphony_vol", 0.0), 2),
                        "prob_loss_dynamic": round(sym["prob_loss_dynamic"], 2) if sym.get("prob_loss_dynamic") is not None else None,
                        "dynamic_floor": round(sym["dynamic_floor"], 2) if sym.get("dynamic_floor") is not None else None,
                        "Session Regime": f"{sym.get('effective_regime', 'unknown')} | Corr: {sym.get('regime_correlation', 'Low')}",
                        "strategy_params": params,
                        "next_day_holdings": ["Pending..."],
                        "Parabolic Squeeze Engaged": sym.get("para_armed", False),
                    }
                )

        with open(report_file, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=4)

    else:
        # STAGE 2 (16:00 ET): Inject Tomorrow's Holdings and Fix Final Math
        if not os.path.exists(report_file):
            print("  -> Warning: Stage 1 snapshot missing. Cannot inject new holdings.", flush=True)
            return

        with open(report_file, "r", encoding="utf-8") as f:
            report = json.load(f)

        if "STATUS" not in report.get("tomorrow_target_holdings", {}):
            return

        print(f"  -> Generating Stage 2 Post-Mortem (Injecting Holdings & Correcting EOD Alpha): {report_file}", flush=True)

        portfolio_holdings_summary = {}

        for sym_id, sym in bot_state.items():
            if not isinstance(sym, dict):
                continue

            sym_holdings = [h.get("ticker") for h in sym.get("current_holdings", [])]

            for trigger in report.get("triggers", []):
                if trigger.get("symphony_name") == sym.get("name") and trigger.get("account_id") == sym.get("account"):
                    trigger["next_day_holdings"] = sym_holdings

            for holding in sym.get("current_holdings", []):
                ticker = holding.get("ticker", "UNKNOWN")
                weight = holding.get("weight", holding.get("allocation", 0.0))
                if ticker not in portfolio_holdings_summary:
                    portfolio_holdings_summary[ticker] = 0.0
                portfolio_holdings_summary[ticker] += weight

        pos_alpha_count = sum(1 for t in report.get("triggers", []) if t.get("saved_pct_guard_alpha", 0) > 0)
        report["summary"]["positive_guard_alpha_count"] = pos_alpha_count

        sorted_holdings = dict(
            sorted(portfolio_holdings_summary.items(), key=lambda item: item[1], reverse=True)
        )
        report["tomorrow_target_holdings"] = sorted_holdings

        with open(report_file, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=4)

        # We no longer send the Discord push directly from here.
        # It is handled by send_eod_discord_post() after the autotuner completes.

def send_eod_discord_post(current_date_str, report_file, optimization_results, discord_webhook_url):
    """Sends the finalized EOD report to Discord, including a multi-timeframe summary, historical chart, and optimization changes."""
    if not discord_webhook_url:
        return
        
    print("  -> Pushing EOD Snapshot to Discord...", flush=True)
    try:
        if not os.path.exists(report_file):
            print(f"  -> Error: Report file {report_file} not found.", flush=True)
            return

        with open(report_file, "r", encoding="utf-8") as f:
            report = json.load(f)
            
        total_monitored = report.get("summary", {}).get("total_monitored", 0)
        triggers = report.get("triggers", [])
        total_triggered = len(triggers)

        # 1. Time Series Data Extraction for Chart
        dates_list = []
        alpha_list = []
        saved_list = []
        win_rate_list = []
        
        all_pm_files = sorted(glob.glob("post_mortem_*.json"))
        # Limit to last 45 days if the list is getting too long
        chart_files = all_pm_files[-45:] if len(all_pm_files) > 45 else all_pm_files
            
        for f_path in chart_files:
            try:
                d_str = os.path.basename(f_path).replace("post_mortem_", "").replace(".json", "")
                with open(f_path, "r", encoding="utf-8") as f:
                    day_data = json.load(f)
                
                day_triggers = day_data.get("triggers", [])
                t_count = len(day_triggers)
                
                if t_count > 0:
                    d_alpha = sum(t.get("saved_pct_guard_alpha", 0.0) for t in day_triggers) / t_count
                    d_saved = sum(t.get("saved_dollars", 0.0) for t in day_triggers)
                    d_wins = sum(1 for t in day_triggers if t.get("saved_pct_guard_alpha", 0.0) > 0)
                    d_win_rate = (d_wins / t_count) * 100.0
                else:
                    d_alpha = 0.0
                    d_saved = 0.0
                    d_win_rate = 0.0
                
                dates_list.append(d_str)
                alpha_list.append(round(d_alpha, 2))
                saved_list.append(round(d_saved, 2))
                win_rate_list.append(round(d_win_rate, 1))
            except:
                continue

        # 2. QuickChart API POST Request
        chart_url = None
        if dates_list:
            chart_config = {
                "type": "line",
                "data": {
                    "labels": dates_list,
                    "datasets": [
                        {
                            "label": "Avg Guard Alpha (%)",
                            "borderColor": "#10b981", # emerald
                            "data": alpha_list,
                            "yAxisID": "y",
                            "fill": False
                        },
                        {
                            "label": "Win Rate (%)",
                            "borderColor": "#3b82f6", # blue
                            "borderDash": [5, 5],
                            "data": win_rate_list,
                            "yAxisID": "y",
                            "fill": False
                        },
                        {
                            "label": "Daily Saved ($)",
                            "type": "bar",
                            "backgroundColor": "rgba(245, 158, 11, 0.5)", # goldenrod/amber
                            "data": saved_list,
                            "yAxisID": "y1"
                        }
                    ]
                },
                "options": {
                    "scales": {
                        "yAxes": [
                            {"id": "y", "position": "left", "ticks": {"fontColor": "#cbd5e1"}},
                            {"id": "y1", "position": "right", "gridLines": {"display": False}, "ticks": {"fontColor": "#f59e0b"}}
                        ],
                        "xAxes": [{"ticks": {"fontColor": "#cbd5e1"}}]
                    },
                    "legend": {"labels": {"fontColor": "#cbd5e1"}}
                }
            }
            
            try:
                resp = requests.post(
                    "https://quickchart.io/chart/create",
                    json={
                        "chart": chart_config,
                        "width": 800,
                        "height": 400,
                        "backgroundColor": "#1e293b"
                    },
                    timeout=10
                )
                chart_url = resp.json().get('url')
            except Exception as e:
                print(f"  -> QuickChart failed: {e}", flush=True)

        # Multi-Timeframe Performance Stats (1d, 7d, 30d)
        windows = [1, 7, 30]
        
        # Initialize stats for each window
        stats = {w: {
            "total_saved": 0.0,
            "total_value": 0.0,
            "total_alpha": 0.0,
            "trigger_count": 0,
            "wins": 0,
            "by_reason": {}
        } for w in windows}
        
        all_reasons = set()
        
        try:
            end_date = datetime.strptime(current_date_str, "%Y-%m-%d")
            files = glob.glob("post_mortem_*.json")

            for f_path in files:
                try:
                    date_part = os.path.basename(f_path).replace("post_mortem_", "").replace(".json", "")
                    file_date = datetime.strptime(date_part, "%Y-%m-%d")
                    delta_days = (end_date - file_date).days
                    
                    if delta_days < 0:
                        continue # Skip future files
                        
                    active_windows = [w for w in windows if delta_days < w]
                    if not active_windows:
                        continue
                        
                    with open(f_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        
                    for t in data.get("triggers", []):
                        alpha_pct = t.get("saved_pct_guard_alpha", 0.0)
                        dollars = t.get("saved_dollars", 0.0)
                        sym_val = t.get("symphony_value", 0.0)
                        reason = t.get("exit_reason", "Unknown")
                        all_reasons.add(reason)
                        
                        for w in active_windows:
                            ws = stats[w]
                            ws["total_saved"] += dollars
                            ws["total_value"] += sym_val
                            ws["total_alpha"] += alpha_pct
                            ws["trigger_count"] += 1
                            if alpha_pct > 0:
                                ws["wins"] += 1
                            
                            if reason not in ws["by_reason"]:
                                ws["by_reason"][reason] = {"saved": 0.0, "value": 0.0, "alpha": 0.0, "count": 0, "wins": 0}
                            
                            rs = ws["by_reason"][reason]
                            rs["saved"] += dollars
                            rs["value"] += sym_val
                            rs["alpha"] += alpha_pct
                            rs["count"] += 1
                            if alpha_pct > 0:
                                rs["wins"] += 1
                except:
                    continue
        except Exception as e:
            print(f"  -> Minor error calculating history: {e}", flush=True)

        # 1. Main Summary Embed
        desc_lines = [
            f"**Total Monitored:** {total_monitored}",
            f"**Total Triggered:** {total_triggered}\n"
        ]
        
        for w in windows:
            ws = stats[w]
            avg_alpha = (ws["total_alpha"] / ws["trigger_count"]) if ws["trigger_count"] > 0 else 0.0
            win_rate = (ws["wins"] / ws["trigger_count"] * 100.0) if ws["trigger_count"] > 0 else 0.0
            
            desc_lines.append(f"**📅 {w}-Day Performance Summary**")
            desc_lines.append(f"• **Avg Guard Alpha:** {avg_alpha:+.2f}%")
            desc_lines.append(f"• **Total Saved:** ${ws['total_saved']:+,.2f}")
            desc_lines.append(f"• **Win Rate:** {win_rate:.0f}% ({ws['wins']}/{ws['trigger_count']})")
            
            # Nested Reason Breakdown for this specific window
            breakdown_parts = []
            for reason in sorted(list(all_reasons)):
                rs = ws["by_reason"].get(reason)
                if rs and rs["count"] > 0:
                    r_alpha = (rs["alpha"] / rs["count"]) if rs["count"] > 0 else 0.0
                    # FIXED: Replaced markdown hyphen with literal spaces and unicode hollow circle
                    breakdown_parts.append(f"    ◦ {reason}: {r_alpha:+.2f}% ({rs['wins']}/{rs['count']})")
            
            if breakdown_parts:
                desc_lines.append("• **Breakdown by Reason:**")
                desc_lines.extend(breakdown_parts)
                
            desc_lines.append("") # Spacer between windows
            
        main_desc = "\n".join(desc_lines)
        if len(main_desc) > 4096:
            main_desc = main_desc[:4093] + "..."

        embeds = [{
            "title": f"📊 AlphaBot EOD Analysis ({current_date_str})",
            "color": 3447003,
            "description": main_desc,
            "footer": {"text": "End of Day Post-Mortem"}
        }]
        
        if chart_url:
            embeds[0]["image"] = {"url": chart_url}

        # 2. Symphony Optimization Embeds (Delta-Only)
        if optimization_results:
            for sym_name, changes in optimization_results.items():
                sym_changes_text = ""
                baseline_text = ""
                if changes:
                    if "_baseline_chosen" in changes:
                        baseline_text = f"**Decision:** {changes['_baseline_chosen']}\n\n"

                    for var, vals in changes.items():
                        if var == "_baseline_chosen":
                            continue
                        # Delta-Only Filter: Only add to string if the value actually changed
                        if vals['old'] != vals['new']:
                            sym_changes_text += f"- `{var}`: {vals['old']} -> {vals['new']}\n"
                
                if not sym_changes_text:
                    sym_changes_text = "✅ Optimal parameters retained."
                
                embeds.append({
                    "title": f"⚙️ {sym_name.title()} Optimization",
                    "color": 10181046,
                    "description": baseline_text + sym_changes_text
                })
        else:
            embeds.append({
                "title": "⚙️ Optimization",
                "color": 10181046,
                "description": "No optimization changes."
            })

        # --- DISCORD PAYLOAD SEGREGATION ---
        
        # Split the main EOD summary embed from the optimization embeds
        main_embed = [embeds[0]]
        optimization_embeds = embeds[1:]

        with open(report_file, "rb") as f:
            file_data = f.read()

        # 1. Send the first message containing ONLY the EOD Summary and the JSON file attachment
        payload_data = {"payload_json": json.dumps({"embeds": main_embed})}
        files_payload = {"file": (report_file, file_data, "application/json")}
        try:
            requests.post(discord_webhook_url, data=payload_data, files=files_payload, timeout=10)
        except Exception as e:
            print(f"  -> Error sending main EOD Discord payload: {e}", flush=True)
        
        # 2. Loop through and send the optimization embeds in strict batches of 10 (Discord limit)
        for i in range(0, len(optimization_embeds), 10):
            time.sleep(1.5)  # Pause briefly to respect Discord's rate limits
            next_batch = optimization_embeds[i:i+10]
            try:
                requests.post(discord_webhook_url, json={"embeds": next_batch}, timeout=10)
            except Exception as e:
                print(f"  -> Error sending optimization batch {i//10 + 1}: {e}", flush=True)
                
        print("  -> Discord Push Complete.", flush=True)
        
    except Exception as e:
        print(f"Failed to send EOD Discord webhook: {e}", flush=True)

def send_discord_alert(
    symphony_name, current_return, prob_beating, stop_trigger_level, high_water_mark, is_live, discord_webhook_url, exit_reason="Trailing Stop", vwap_bleed_arm_pct=None, vwap_bleed_ticks=None, vwap_diff=None, vwap_breakdown_ticks=None, tp_threshold=None, vwap_bleed_multiplier=None, symphony_vol=None, prob_loss_dynamic=None, dynamic_floor=None, volatility_multiplier=None, effective_regime=None, regime_correlation=None
):
    if not discord_webhook_url:
        return

    if exit_reason == "Take-Profit":
        base_title = "🎯 Relative Peak Take-Profit"
        live_color = 5763719 if current_return > 0 else 15548997 # Green if positive, Orange if negative
    elif exit_reason in ["VWAP Breakdown", "VWAP Bleed Cut"]:
        base_title = "📉 VWAP Breakdown Exit" if exit_reason == "VWAP Breakdown" else "🩸 VWAP Bleed Cut"
        live_color = 15548997 # Red/Orange
    elif "Breakeven" in exit_reason:
        base_title = "🛡️ Breakeven Defense"
        live_color = 3447003 # Blue
    elif current_return > 0:
        base_title = "✅ Profit Locked"
        live_color = 5763719
    elif current_return < 0:
        base_title = "🛑 Bleed Stopped"
        live_color = 15548997
    else:
        base_title = "🛡️ Breakeven Locked"
        live_color = 3447003

    title = f"{base_title}: {exit_reason} Triggered" if is_live else f"⚠️ [DRY RUN] {base_title}"
    color = live_color if is_live else 16766720
    if exit_reason != "Take-Profit":
        action_text = "Sell-to-Cash Executed & Settlement Verified" if is_live else "Bypassed (Dry Run Mode)"
    else:
        action_text = "Executed 'Sell to Cash' via API. Trade queued for Composer execution window." if is_live else "Bypassed (Dry Run Mode)"

    fields = [
        {"name": "Symphony", "value": symphony_name, "inline": True},
        {"name": "Exit Return", "value": f"{current_return:.2f}%", "inline": True},
        {"name": "High Water Mark", "value": f"{high_water_mark:.2f}%", "inline": True},
        {"name": "Stop Level", "value": f"{stop_trigger_level:.2f}%", "inline": True},
        {"name": "MC Probability", "value": f"{prob_beating:.1f}%", "inline": True},
        {"name": "Action Taken", "value": action_text, "inline": False},
    ]

    if exit_reason == "Trailing Stop" and prob_loss_dynamic is not None and dynamic_floor is not None:
        fields.append({"name": "Downside Risk (Vol-Scaled)", "value": f"{prob_loss_dynamic:.1f}% chance of dropping below {dynamic_floor:.2f}% (Multiplier: {volatility_multiplier}x)", "inline": False})

    if exit_reason in ["VWAP Breakdown", "VWAP Bleed Cut"] and vwap_diff is not None:
        fields.append({"name": "VWAP Stats", "value": f"VWAP Diff: {vwap_diff * 100:.2f}% | Ticks Below: {vwap_breakdown_ticks}", "inline": False})

    if exit_reason == "Take-Profit" and tp_threshold is not None:
        fields.append({"name": "Take-Profit Threshold", "value": f"MC Prob Reached: {prob_beating:.1f}% >= {tp_threshold}%", "inline": False})

    if effective_regime and regime_correlation:
        fields.append({"name": "Session Regime", "value": f"{effective_regime} | Corr: {regime_correlation}", "inline": False})

    payload = {
        "embeds": [
            {
                "title": title,
                "color": color,
                "fields": fields,
                "footer": {"text": "Alpha Bot • Hybrid Defense Protocol"},
            }
        ]
    }
    time.sleep(1)
    try:
        requests.post(discord_webhook_url, json=payload, timeout=10)
    except Exception as e:
        print(f"!!! [DISCORD ERROR]: Failed to send alert: {e}", flush=True)

def send_circuit_breaker_alert(symphony_name, webhook_url):
    if not webhook_url:
        return

    payload = {
        "embeds": [
            {
                "title": "Manual Intervention Detected",
                "color": 15548997,
                "description": f"**{symphony_name}** is no longer active in Composer. Tracking has been safely suspended for the remainder of the session to protect API rate limits.",
                "footer": {"text": "Alpha Bot • Circuit Breaker"}
            }
        ]
    }
    time.sleep(1)
    try:
        requests.post(webhook_url, json=payload, timeout=10)
    except Exception as e:
        print(f"!!! [DISCORD ERROR]: Failed to send circuit breaker alert: {e}", flush=True)