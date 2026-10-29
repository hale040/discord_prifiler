#!/usr/bin/env bash
# L4: наличие всех 6 ролей конвейера в истории ветки
set -uo pipefail
roles=("Аналитик" "Архитектор" "UI-дизайнер" "Разработчик" "Верстальщик" "Тестировщик")
missing=0
for r in "${roles[@]}"; do
  if git log --format='%s' | grep -q "\[$r\]"; then echo "OK   роль пройдена: $r"
  else echo "MISS роль отсутствует: $r"; missing=1; fi
done
echo "ИТОГ L4:"; [ $missing -eq 0 ] && echo "PASS" || echo "FAIL"
exit $missing
