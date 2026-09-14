#!/usr/bin/env python3
"""
align_fleet.py
Automated fleet alignment script for SuperTrend strategies across all XTS client containers.
Can run in dry-run mode (default) or execution mode (--execute).
"""
import sys
import os
import json
import time
import datetime
import argparse
import subprocess

def ts_now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S IST")

DEFAULT_CLIENT_IDS = [
    "abk01", "abk02", "abk03", "abk04", "abk05",
    "abk06", "abk09", "abk10", "abk11", "abk12", "dm933"
]

def get_active_client_ids():
    """Dynamically discovers all running xts_client_* containers via Docker."""
    try:
        cmd = ["docker", "ps", "--filter", "name=xts_client_", "--format", "{{.Names}}"]
        output = subprocess.check_output(cmd, timeout=5).decode("utf-8")
        found = [
            line.strip().replace("xts_client_", "")
            for line in output.splitlines()
            if line.strip().startswith("xts_client_")
        ]
        if found:
            return sorted(found)
    except Exception:
        pass
    return DEFAULT_CLIENT_IDS

def query_telemetry(client_id, retries=2):
    for attempt in range(retries + 1):
        try:
            cmd = ["docker", "exec", f"xts_client_{client_id}", "curl", "-s", "--max-time", "5", "http://127.0.0.1:8000/internal/telemetry"]
            res = subprocess.check_output(cmd, timeout=8).decode("utf-8")
            return json.loads(res)
        except Exception as e:
            if attempt < retries:
                time.sleep(1.0)
                continue
            return {"error": str(e)}

def sync_strategy(client_id, strategy_id, retries=2):
    for attempt in range(retries + 1):
        try:
            cmd = [
                "docker", "exec", f"xts_client_{client_id}",
                "curl", "-s", "--max-time", "15", "-X", "POST",
                "-H", "Content-Type: application/json",
                "-d", json.dumps({"strategy_id": strategy_id}),
                f"http://127.0.0.1:8000/internal/supertrend/sync-trend?strategy_id={strategy_id}"
            ]
            res = subprocess.check_output(cmd, timeout=18).decode("utf-8")
            return json.loads(res)
        except Exception as e:
            if attempt < retries:
                time.sleep(2.0)
                continue
            return {"error": str(e)}

def main():
    parser = argparse.ArgumentParser(description="Fleet alignment for SuperTrend strategies.")
    parser.add_argument("--execute", action="store_true", help="Execute real alignment orders via sync-trend.")
    parser.add_argument("--client", type=str, default="", help="Optional single client filter (e.g. abk06).")
    args = parser.parse_args()

    target_clients = [args.client.lower()] if args.client else get_active_client_ids()

    print("=" * 75)
    print(f"[{ts_now()}] XTS FLEET SUPERTREND ALIGNMENT (Mode: {'EXECUTE' if args.execute else 'DRY RUN'})")
    print("=" * 75)

    misaligned_count = 0
    aligned_count = 0
    exec_success_count = 0
    exec_fail_count = 0

    for cid in target_clients:
        t_data = query_telemetry(cid)
        if "error" in t_data:
            print(f"[{ts_now()}] [{cid.upper()}] ❌ Failed to fetch telemetry: {t_data['error']}")
            continue

        strategies = t_data.get("supertrend", {}).get("strategies", [])
        if not strategies:
            print(f"[{ts_now()}] [{cid.upper()}] ⚠️ No active SuperTrend strategies configured.")
            continue

        print(f"\n--- CLIENT {cid.upper()} ---")
        for s in strategies:
            strat_id = s.get("id")
            sym = s.get("symbol")
            tf = s.get("timeframe")
            qty = s.get("quantity", 0)
            trend = s.get("current_trend", "INITIALIZING")
            vpos = s.get("virtual_position", 0)
            bpos = s.get("current_broker_quantity", 0)
            target_pos = -qty if trend == "BEARISH" else (qty if trend == "BULLISH" else 0)

            is_aligned = (vpos == target_pos) and (bpos == target_pos)
            status_icon = "✅" if is_aligned else "⚠️"

            print(f"  {status_icon} [{sym} ({tf})] Trend={trend} | Virtual={vpos:+d} | Broker={bpos:+d} | Target={target_pos:+d}")

            if not is_aligned:
                misaligned_count += 1
                if args.execute:
                    print(f"     [{ts_now()}] 🚀 Executing sync-trend for {strat_id}...")
                    sync_res = sync_strategy(cid, strat_id)
                    res_status = sync_res.get("status") if isinstance(sync_res, dict) else "UNKNOWN"
                    print(f"     [{ts_now()}] 👉 Result: {sync_res}")
                    if res_status in ("SUCCESS", "ALREADY_SYNCED"):
                        exec_success_count += 1
                    else:
                        exec_fail_count += 1
                else:
                    print(f"     ℹ️ Needs alignment to target {target_pos:+d} lots (pass --execute to align).")
            else:
                aligned_count += 1

    print("\n" + "=" * 75)
    if args.execute:
        print(f"[{ts_now()}] SUMMARY: {aligned_count} already aligned, {exec_success_count} synced, {exec_fail_count} failed, {misaligned_count} total targeted.")
    else:
        print(f"[{ts_now()}] SUMMARY: {aligned_count} aligned, {misaligned_count} misaligned.")
    print("=" * 75)

if __name__ == "__main__":
    main()
