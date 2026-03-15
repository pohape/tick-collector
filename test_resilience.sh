#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TOXI="$SCRIPT_DIR/toxiproxy.sh"
DATA_DIR="${DATA_DIR:-data}"

check_rows() {
    local label="$1"
    local count
    count=$(find "$DATA_DIR" -name "*.csv" -exec cat {} + 2>/dev/null | wc -l)
    echo "  [$label] total CSV lines (incl. headers): $count"
}

wait_and_check() {
    local secs="$1"
    local label="$2"
    echo "  waiting ${secs}s..."
    sleep "$secs"
    check_rows "$label"
}

echo "=== Scenario 1: Baseline ==="
echo "  Checking rows are appearing..."
check_rows "before"
sleep 10
check_rows "after 10s"

echo ""
echo "=== Scenario 2: Drop Binance ==="
"$TOXI" binance_ws drop
wait_and_check 10 "during drop"
"$TOXI" binance_ws normal
wait_and_check 10 "after recovery"

echo ""
echo "=== Scenario 3: Freeze Bybit ==="
"$TOXI" bybit_ws freeze
wait_and_check 20 "during freeze"
"$TOXI" bybit_ws normal
wait_and_check 10 "after recovery"

echo ""
echo "=== Scenario 4: High latency Binance ==="
"$TOXI" binance_ws lag 2000 500
wait_and_check 15 "during lag"
"$TOXI" binance_ws normal
wait_and_check 10 "after recovery"

echo ""
echo "=== Scenario 5: Slow Bybit ==="
"$TOXI" bybit_ws slow
wait_and_check 15 "during slow"
"$TOXI" bybit_ws normal
wait_and_check 10 "after recovery"

echo ""
echo "=== Scenario 6: Both down ==="
"$TOXI" binance_ws drop
"$TOXI" bybit_ws drop
wait_and_check 15 "during both down"
"$TOXI" binance_ws normal
"$TOXI" bybit_ws normal
wait_and_check 10 "after recovery"

echo ""
echo "=== Scenario 7: Rapid flapping ==="
for i in $(seq 1 5); do
    echo "  flap $i/5: drop"
    "$TOXI" binance_ws drop
    "$TOXI" bybit_ws drop
    sleep 3
    echo "  flap $i/5: normal"
    "$TOXI" binance_ws normal
    "$TOXI" bybit_ws normal
    sleep 5
done
check_rows "after flapping"

echo ""
echo "=== All scenarios complete ==="
check_rows "final"
