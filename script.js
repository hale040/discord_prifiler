"use strict";

/* =========================================================
   Тестовые данные: прайс-лист металлопроката
   ========================================================= */
const PRICE_DATA = [
  { name: "Арматура А500С", size: "⌀12 мм", unit: "т", price: 42000, stock: "В наличии" },
  { name: "Арматура А500С", size: "⌀14 мм", unit: "т", price: 42500, stock: "В наличии" },
  { name: "Арматура А500С", size: "⌀16 мм", unit: "т", price: 43000, stock: "Мало" },
  { name: "Арматура А240 (гладкая)", size: "⌀8 мм", unit: "т", price: 44000, stock: "В наличии" },
  { name: "Балка двутавровая", size: "20Б1 / С255", unit: "т", price: 58000, stock: "В наличии" },
  { name: "Балка двутавровая", size: "30Ш1 / Ст3сп5", unit: "т", price: 60500, stock: "Под заказ" },
  { name: "Швеллер", size: "12П / Ст3", unit: "т", price: 54000, stock: "В наличии" },
  { name: "Швеллер", size: "16У / 09Г2С", unit: "т", price: 57500, stock: "Мало" },
  { name: "Уголок равнополочный", size: "50×50×5 / Ст3", unit: "т", price: 48000, stock: "В наличии" },
  { name: "Уголок равнополочный", size: "75×75×6 / 09Г2С", unit: "т", price: 51000, stock: "В наличии" },
  { name: "Лист горячекатаный", size: "10×1500×6000 / Ст3", unit: "т", price: 51000, stock: "В наличии" },
  { name: "Лист горячекатаный", size: "20×1500×6000 / 09Г2С", unit: "т", price: 55500, stock: "Мало" },
  { name: "Лист холоднокатаный", size: "1.0×1250×2500 / 08пс", unit: "т", price: 62000, stock: "В наличии" },
  { name: "Лист рифлёный", size: "4×1250×2500 / ФРЧ", unit: "т", price: 64000, stock: "Под заказ" },
  { name: "Труба профильная", size: "60×60×3 / Ст3", unit: "т", price: 56000, stock: "В наличии" },
  { name: "Труба профильная", size: "100×100×4 / 09Г2С", unit: "т", price: 59000, stock: "В наличии" },
  { name: "Труба электросварная", size: "⌀108×4 / Ст3", unit: "т", price: 57000, stock: "Мало" },
  { name: "Круг горячекатаный", size: "⌀30 мм / Ст3", unit: "т", price: 49000, stock: "В наличии" },
  { name: "Полоса", size: "40×4 мм / Ст3", unit: "т", price: 50000, stock: "В наличии" },
  { name: "Лист нержавеющий", size: "3×1000×2000 / AISI 304", unit: "т", price: 145000, stock: "Мало" },
  { name: "Круг нержавеющий", size: "⌀20 мм / AISI 316", unit: "т", price: 190000, stock: "Под заказ" },
];

const fmt = n => n.toLocaleString("ru-RU") + " ₽";

/* =========================================================
   Рендер таблицы прайса + поиск
   ========================================================= */
const priceTbody = document.querySelector("#priceTable tbody");
const priceSearch = document.getElementById("priceSearch");

function stockClass(stock) {
  if (stock === "В наличии") return "stock--yes";
  if (stock === "Мало") return "stock--low";
  return "stock--no";
}

function renderPrice(filter = "") {
  const q = filter.trim().toLowerCase();
  const rows = PRICE_DATA.filter(item =>
    !q || (item.name + " " + item.size).toLowerCase().includes(q)
  );
  priceTbody.innerHTML = rows.map(item => `
    <tr>
      <td>${item.name}</td>
      <td>${item.size}</td>
      <td>${item.unit}</td>
      <td class="num">${fmt(item.price)}</td>
      <td class="${stockClass(item.stock)}">${item.stock}</td>
    </tr>`).join("") ||
    `<tr><td colspan="5" style="text-align:center;padding:1.5rem;">Ничего не найдено 🤷</td></tr>`;
}
renderPrice();
priceSearch.addEventListener("input", e => renderPrice(e.target.value));

/* =========================================================
   Скачивание прайса (CSV, с BOM для корректной кириллицы в Excel)
   ========================================================= */
document.getElementById("downloadPriceBtn").addEventListener("click", () => {
  const header = ["Наименование", "Размер / марка", "Ед. изм.", "Цена, руб/т", "Наличие"];
  const lines = [header.join(";")];
  PRICE_DATA.forEach(i => {
    lines.push([i.name, i.size, i.unit, i.price, i.stock].join(";"));
  });
  const csv = "\uFEFF" + lines.join("\r\n");
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "price_stalprom_2026-10.csv";
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
});

/* =========================================================
   Бургер-меню
   ========================================================= */
const burger = document.getElementById("burger");
const nav = document.getElementById("nav");
burger.addEventListener("click", () => {
  nav.classList.toggle("open");
  burger.classList.toggle("open");
});
nav.querySelectorAll(".nav__link").forEach(link =>
  link.addEventListener("click", () => {
    nav.classList.remove("open");
    burger.classList.remove("open");
  })
);

/* =========================================================
   Шапка при скролле + кнопка «наверх» + активный пункт меню
   ========================================================= */
const header = document.getElementById("header");
const toTop = document.getElementById("toTop");

window.addEventListener("scroll", () => {
  header.classList.toggle("scrolled", window.scrollY > 10);
  toTop.classList.toggle("visible", window.scrollY > 600);
}, { passive: true });

toTop.addEventListener("click", () => window.scrollTo({ top: 0, behavior: "smooth" }));

/* =========================================================
   Появление блоков при скролле (IntersectionObserver)
   ========================================================= */
const io = new IntersectionObserver(entries => {
  entries.forEach(en => {
    if (en.isIntersecting) {
      en.target.classList.add("visible");
      io.unobserve(en.target);
    }
  });
}, { threshold: 0.12 });
document.querySelectorAll(".reveal").forEach(el => io.observe(el));

/* =========================================================
   Анимация счётчиков в hero
   ========================================================= */
const counterIO = new IntersectionObserver(entries => {
  entries.forEach(en => {
    if (!en.isIntersecting) return;
    const el = en.target;
    const target = +el.dataset.count;
    const dur = 1200;
    const start = performance.now();
    function tick(now) {
      const p = Math.min((now - start) / dur, 1);
      el.textContent = Math.round(target * (1 - Math.pow(1 - p, 3))).toLocaleString("ru-RU");
      if (p < 1) requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);
    counterIO.unobserve(el);
  });
}, { threshold: 0.5 });
document.querySelectorAll(".stat__num").forEach(el => counterIO.observe(el));

/* =========================================================
   Калькулятор
   ========================================================= */
const calcType = document.getElementById("calcType");
const calcWeight = document.getElementById("calcWeight");
const calcCut = document.getElementById("calcCut");
const calcDelivery = document.getElementById("calcDelivery");
const calcSum = document.getElementById("calcSum");

// наполняем select уникальными категориями по цене "от"
const calcOptions = [
  { label: "Арматура — 42 000 ₽/т", price: 42000 },
  { label: "Уголок — 48 000 ₽/т", price: 48000 },
  { label: "Круг / полоса — 49 000 ₽/т", price: 49000 },
  { label: "Лист ГК — 51 000 ₽/т", price: 51000 },
  { label: "Швеллер — 54 000 ₽/т", price: 54000 },
  { label: "Труба — 56 000 ₽/т", price: 56000 },
  { label: "Балка — 58 000 ₽/т", price: 58000 },
  { label: "Нержавейка AISI 304 — 145 000 ₽/т", price: 145000 },
];
calcType.innerHTML = calcOptions.map((o, i) =>
  `<option value="${i}">${o.label}</option>`).join("");

function calcUpdate() {
  const opt = calcOptions[+calcType.value || 0];
  const w = parseFloat(calcWeight.value) || 0;
  let sum = opt.price * w;
  if (calcCut.checked) sum += 1500 * w;
  if (calcDelivery.checked) sum += 8000;
  // скидка за объём
  if (w >= 20) sum *= 0.95;
  else if (w >= 5) sum *= 0.97;
  calcSum.textContent = w > 0 ? fmt(Math.round(sum)) : "—";
}
[calcType, calcWeight, calcCut, calcDelivery].forEach(el =>
  el.addEventListener("change", calcUpdate));
calcWeight.addEventListener("input", calcUpdate);
calcUpdate();

document.getElementById("calcOrder").addEventListener("click", () => {
  openModal(`Расчёт: ${calcOptions[+calcType.value].label}, ${calcWeight.value} т ≈ ${calcSum.textContent}. Оставьте телефон — менеджер уточнит цену.`);
});

/* =========================================================
   Маска телефона (простая)
   ========================================================= */
function phoneMask(input) {
  input.addEventListener("input", () => {
    let d = input.value.replace(/\D/g, "").slice(0, 11);
    if (d.startsWith("8")) d = "7" + d.slice(1);
    if (!d.startsWith("7")) d = "7" + d;
    let out = "+7";
    if (d.length > 1) out += " (" + d.slice(1, 4);
    if (d.length >= 5) out += ") " + d.slice(4, 7);
    if (d.length >= 8) out += "-" + d.slice(7, 9);
    if (d.length >= 10) out += "-" + d.slice(9, 11);
    input.value = out;
  });
}
phoneMask(document.getElementById("phoneInput"));
phoneMask(document.getElementById("modalPhone"));

/* =========================================================
   Модальное окно «Заказать звонок»
   ========================================================= */
const modal = document.getElementById("modal");
const modalClose = document.getElementById("modalClose");
const modalForm = document.getElementById("modalForm");
const modalStatus = document.getElementById("modalStatus");

function openModal(text) {
  if (text) modal.querySelector("p").textContent = text;
  modal.classList.add("open");
}
function closeModal() {
  modal.classList.remove("open");
  modalStatus.textContent = "";
}
document.getElementById("callBtn").addEventListener("click", () => openModal());
modalClose.addEventListener("click", closeModal);
modal.addEventListener("click", e => { if (e.target === modal) closeModal(); });
document.addEventListener("keydown", e => { if (e.key === "Escape") closeModal(); });

modalForm.addEventListener("submit", e => {
  e.preventDefault();
  const phone = document.getElementById("modalPhone").value;
  if (phone.replace(/\D/g, "").length < 11) {
    modalStatus.textContent = "Введите полный номер телефона";
    modalStatus.className = "form__status err";
    return;
  }
  modalStatus.textContent = "Заявка принята! Перезвоним в течение 15 минут.";
  modalStatus.className = "form__status ok";
  setTimeout(closeModal, 2500);
});

/* =========================================================
   Форма заявки (валидация)
   ========================================================= */
const leadForm = document.getElementById("leadForm");
const formStatus = document.getElementById("formStatus");

leadForm.addEventListener("submit", e => {
  e.preventDefault();
  const name = leadForm.elements.name;
  const phone = leadForm.elements.phone;
  const agree = document.getElementById("agree");
  let ok = true;

  [name, phone].forEach(f => f.classList.remove("invalid"));

  if (name.value.trim().length < 2) { name.classList.add("invalid"); ok = false; }
  if (phone.value.replace(/\D/g, "").length < 11) { phone.classList.add("invalid"); ok = false; }
  if (!agree.checked) ok = false;

  if (!ok) {
    formStatus.textContent = "Проверьте поля: имя, телефон и согласие на обработку данных.";
    formStatus.className = "form__status err";
    return;
  }
  formStatus.textContent = "Спасибо, " + name.value.trim() + "! Заявка отправлена (демо).";
  formStatus.className = "form__status ok";
  leadForm.reset();
  setTimeout(() => { formStatus.textContent = ""; }, 6000);
});
