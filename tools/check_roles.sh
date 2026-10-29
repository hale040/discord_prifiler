#!/usr/bin/env bash
# L4: наличие всех 6 ролей конвейера в истории ветки.
# UTF-8 принудительно: grep по кириллице ломается при C locale (проверено: false-negative MISS).
set -uo pipefail
export LC_ALL=C.UTF-8 LANG=C.UTF-8
roles=("Аналитик" "Архитектор" "UI-дизайнер" "Разработчик" "Верстальщик" "Тестировщик")
missing=0
hist=$(git log --format='%s')
for r in "${roles[@]}"; do
  if printf '%s\n' "$hist" | grep -qF "[$r]"; then echo "OK   роль пройдена: $r"
  else echo "MISS роль отсутствует: $r"; missing=1; fi
done
echo "ИТОГ L4:"; [ $missing -eq 0 ] && echo "PASS" || echo "FAIL"
exit $missing
