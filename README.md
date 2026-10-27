# МЕТАЛЛОСТАЛЬ — лендинг

Одностраничный сайт компании: каталог металлопроката, услуги металлообработки,
вступление в СРО, этапы работ, доставка и оплата.

## Структура
- `index.html` — разметка всех секций + JSON-LD (LocalBusiness)
- `styles.css` — стили (токены в `:root`)
- `script.js` — слайдер, калькулятор (единый источник цен `PRICE_DATA`),
  формы/лиды (`LEAD_WEBHOOK`, fallback в localStorage), генерация PDF/CSV/DOCX
- `assets/` — изображения, лого, реквизиты
- `sitemap.xml`, `robots.txt` — для поисковых систем

## Запуск локально
```bash
python3 -m http.server 8080
# открыть http://localhost:8080
```

## Деплой
GitHub Pages из ветки `main` (Settings → Pages → Deploy from branch → `/ (root)`).
Адрес: https://hale040.github.io/work_site/

## Перед боевым запуском (TODO владельца)
1. Заполнить `LEAD_WEBHOOK` в `script.js` (Formspree / Telegram-бот / CRM).
2. Подключить Яндекс.Метрику: `METRIKA_ID` в `script.js`.
3. Заменить тестовые реквизы (ИНН/ОГРН) на реальные; обновить `assets/rekvizity.pdf`.
4. Обновлять дату прайса в `<html data-price-updated>` при изменении `PRICE_DATA`.
