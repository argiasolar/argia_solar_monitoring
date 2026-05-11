#!/usr/bin/env bash
# pi/check_env.sh — audit env vars required by Argia_Mont v2.
#
# Run on the Pi BEFORE the first cron execution:
#   bash pi/check_env.sh
#
# Prints OK / MISSING per variable. Never prints the actual values.

set -u  # error on unset variables (but we handle each one explicitly)

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
NC='\033[0m'  # no color

check_var() {
    local name="$1"
    local required="$2"  # "required" or "optional"
    # Use indirect parameter expansion to read the variable by name
    local value="${!name:-}"

    if [ -n "$value" ]; then
        local length=${#value}
        printf "  ${GREEN}OK${NC}      %-30s (len=%d)\n" "$name" "$length"
        return 0
    fi

    if [ "$required" = "required" ]; then
        printf "  ${RED}MISSING${NC} %-30s (REQUIRED)\n" "$name"
        return 1
    else
        printf "  ${YELLOW}MISSING${NC} %-30s (optional)\n" "$name"
        return 0
    fi
}

echo "================================================================"
echo "Argia_Mont v2 — environment variable audit"
echo "================================================================"
echo
echo "Google Sheets:"
check_var GOOGLE_SHEET_ID_V2 required
check_var GOOGLE_CREDENTIALS required
echo
echo "Growatt (need EITHER api_token OR username+password):"
check_var GROWATT_API_TOKEN optional
check_var GROWATT_USERNAME optional
check_var GROWATT_PASSWORD optional
echo
echo "Huawei (required if any HUAWEI plants are active):"
check_var HUAWEI_USERNAME optional
check_var HUAWEI_PASSWORD optional
echo
echo "SolarEdge (required if any SOLAREDGE plants are active):"
check_var SOLAREDGE_API_KEY optional
echo
echo "================================================================"

# Summary check: REQUIRED variables must all be set
errors=0
[ -z "${GOOGLE_SHEET_ID_V2:-}" ] && errors=$((errors + 1))
[ -z "${GOOGLE_CREDENTIALS:-}" ] && errors=$((errors + 1))

# Growatt: need EITHER api_token OR (username AND password)
if [ -z "${GROWATT_API_TOKEN:-}" ]; then
    if [ -z "${GROWATT_USERNAME:-}" ] || [ -z "${GROWATT_PASSWORD:-}" ]; then
        echo
        printf "  ${RED}WARNING${NC}: No Growatt credentials configured.\n"
        printf "           Set GROWATT_API_TOKEN, or both GROWATT_USERNAME + GROWATT_PASSWORD.\n"
        errors=$((errors + 1))
    fi
fi

# Huawei pair check
if [ -n "${HUAWEI_USERNAME:-}" ] && [ -z "${HUAWEI_PASSWORD:-}" ]; then
    echo
    printf "  ${YELLOW}WARNING${NC}: HUAWEI_USERNAME set but HUAWEI_PASSWORD missing.\n"
fi

if [ $errors -gt 0 ]; then
    echo
    printf "${RED}Audit failed with %d required variable(s) missing.${NC}\n" "$errors"
    exit 1
fi

echo
printf "${GREEN}All required variables are set. v2 can run.${NC}\n"
echo "Reminder: this script does NOT validate the values, only their presence."
echo "If credentials are wrong, you'll see auth errors at runtime."
