#!/usr/bin/env bash
# Единая точка приёмки: L1..L5
set -uo pipefail
cd "$(dirname "$0")/.."
rc=0
echo "--- L1 JS ---"; node --check script.js && echo "node --check: OK" || rc=1
echo "--- L2/L3/L5 ---"; python3 tools/check_site.py . || rc=1
echo "--- L4 roles ---"; bash tools/check_roles.sh || rc=1
echo "=== check.sh итог: $([ $rc -eq 0 ] && echo PASS || echo FAIL)"
exit $rc
