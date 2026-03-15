#!/usr/bin/env bash
set -euo pipefail

API="${TOXIPROXY_API:-127.0.0.1:8474}"
SCRIPT_NAME="${0##*/}"

usage() {
    cat <<'EOF'
Usage:
  SCRIPT_NAME <proxy> normal
  SCRIPT_NAME <proxy> lag [latency_ms] [jitter_ms]
  SCRIPT_NAME <proxy> drop
  SCRIPT_NAME <proxy> freeze
  SCRIPT_NAME <proxy> slow [latency_ms] [jitter_ms] [rate_kb] [avg_size] [size_var] [delay_us]
  SCRIPT_NAME <proxy> inspect
  SCRIPT_NAME <proxy> toxics

Examples:
  SCRIPT_NAME bybit_public normal
  SCRIPT_NAME bybit_public lag
  SCRIPT_NAME bybit_public lag 800 0
  SCRIPT_NAME bybit_trade drop
  SCRIPT_NAME bybit_private freeze
  SCRIPT_NAME bybit_public slow 150 50 32 256 64 20000
  TOXIPROXY_API=10.0.0.5:8474 SCRIPT_NAME bybit_public inspect
EOF
}

print_usage() {
    usage | sed "s/SCRIPT_NAME/$SCRIPT_NAME/g"
}

cli() {
    toxiproxy-cli -h "$API" "$@"
}

clear_toxics() {
    local proxy="$1"
    local toxic_name

    while IFS= read -r toxic_name; do
        [[ -n "$toxic_name" ]] || continue
        cli toxic remove -n "$toxic_name" "$proxy" >/dev/null 2>&1 || true
    done < <(cli inspect "$proxy" 2>/dev/null | awk '{print $1}')
}

require_proxy() {
    cli inspect "$1" >/dev/null 2>&1 || {
        echo "proxy '$1' not found on Toxiproxy API $API" >&2
        exit 2
    }
}

require_uint() {
    local value="$1"
    local name="$2"

    [[ "$value" =~ ^[0-9]+$ ]] || {
        echo "invalid $name: '$value' (must be non-negative integer)" >&2
        exit 1
    }
}

require_max_args() {
    local mode="$1"
    local max="$2"
    local got="$3"

    (( got <= max )) || {
        echo "too many args for mode '$mode': got $got, max $max" >&2
        print_usage
        exit 1
    }
}

[[ $# -ge 2 ]] || {
    print_usage
    exit 1
}

PROXY="$1"
MODE="$2"
shift 2

case "$MODE" in
    normal|drop|freeze|inspect|toxics)
        require_max_args "$MODE" 0 "$#"
        ;;
    lag)
        require_max_args "$MODE" 2 "$#"
        ;;
    slow)
        require_max_args "$MODE" 6 "$#"
        ;;
    *)
        print_usage
        exit 1
        ;;
esac

require_proxy "$PROXY"

case "$MODE" in
    normal)
        clear_toxics "$PROXY"
        echo "[$PROXY] all toxics removed"
        ;;

    lag)
        LATENCY="${1:-250}"
        JITTER="${2:-50}"
        require_uint "$LATENCY" "latency_ms"
        require_uint "$JITTER" "jitter_ms"
        clear_toxics "$PROXY"
        cli toxic add -t latency -n lag -a latency="$LATENCY" -a jitter="$JITTER" "$PROXY"
        echo "[$PROXY] latency=${LATENCY}ms jitter=${JITTER}ms"
        ;;

    drop)
        clear_toxics "$PROXY"
        cli toxic add -t reset_peer -n drop "$PROXY"
        echo "[$PROXY] reset_peer enabled"
        ;;

    freeze)
        clear_toxics "$PROXY"
        cli toxic add -t timeout -n freeze -a timeout=0 "$PROXY"
        echo "[$PROXY] timeout blackhole enabled"
        ;;

    slow)
        LATENCY="${1:-150}"
        JITTER="${2:-50}"
        RATE="${3:-32}"
        AVG_SIZE="${4:-256}"
        SIZE_VAR="${5:-64}"
        DELAY_US="${6:-20000}"

        require_uint "$LATENCY" "latency_ms"
        require_uint "$JITTER" "jitter_ms"
        require_uint "$RATE" "rate_kb"
        require_uint "$AVG_SIZE" "avg_size"
        require_uint "$SIZE_VAR" "size_var"
        require_uint "$DELAY_US" "delay_us"

        clear_toxics "$PROXY"
        cli toxic add -t latency -n slow_lat -a latency="$LATENCY" -a jitter="$JITTER" "$PROXY"
        cli toxic add -t bandwidth -n slow_bw -a rate="$RATE" "$PROXY"
        cli toxic add -t slicer -n slow_slice -a average_size="$AVG_SIZE" -a size_variation="$SIZE_VAR" -a delay="$DELAY_US" "$PROXY"

        echo "[$PROXY] slow profile: latency=${LATENCY}ms jitter=${JITTER}ms rate=${RATE}KB/s avg_size=${AVG_SIZE} size_var=${SIZE_VAR} delay=${DELAY_US}us"
        ;;

    inspect)
        cli inspect "$PROXY"
        ;;

    toxics)
        cli inspect "$PROXY"
        ;;

    *)
        print_usage
        exit 1
        ;;
esac

exit 0
