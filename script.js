"use strict";

/*1 =========================================================
   Прайс-лист металлопроката (справочные цены «от», руб/т).
   Используется для подстановки цены в модалку «Узнать цену» по категории.
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
   Единый источник контактов (шапка, JSON-LD, PDF/DOCX).
   ========================================================= */
const CONTACTS = {
  phoneDisplay: "+7 (343) 202-00-01",
  email: "4499@metallostal.ru",
};

// Канал приёма заявок: Formspree/Telegram-bot URL.
// Пока пусто — лиды копятся в localStorage ("leads_queue"), не теряясь.
const LEAD_WEBHOOK = ""; // напр.: "https://formspree.io/f/xxxxxxx"
const METRIKA_ID = null; // id Яндекс.Метрики после подключения счётчика

/* =========================================================
   Бургер-меню
   ========================================================= */
const burger = document.getElementById("burger");
const nav = document.getElementById("nav");
burger.addEventListener("click", () => {
  nav.classList.toggle("open");
  burger.classList.toggle("open");
  burger.setAttribute("aria-expanded", String(nav.classList.contains("open")));
});
nav.querySelectorAll(".nav__link").forEach(link =>
  link.addEventListener("click", () => {
    nav.classList.remove("open");
    burger.classList.remove("open");
    burger.setAttribute("aria-expanded", "false");
  })
);

/* =========================================================
   Шапка при скролле + кнопка «наверх» + активный пункт меню
   ========================================================= */
const header = document.getElementById("header");
const toTop = document.getElementById("toTop");
const fabStack = document.getElementById("fabStack");
const stickyBar = document.getElementById("stickyBar");

window.addEventListener("scroll", () => {
  const y = window.scrollY;
  header.classList.toggle("scrolled", y > 10);
  toTop.classList.toggle("visible", y > 600);
  if (fabStack) fabStack.classList.toggle("visible", y > 400);
  if (stickyBar) stickyBar.classList.toggle("visible", y > 500);
}, { passive: true });

toTop.addEventListener("click", () => window.scrollTo({ top: 0, behavior: "smooth" }));

/* Активный пункт меню по секциям */
(function () {
  const links = [...document.querySelectorAll(".nav__link")];
  const map = new Map();
  links.forEach(l => {
    const sec = document.querySelector(l.getAttribute("href"));
    if (sec) map.set(sec, l);
  });
  const secIO = new IntersectionObserver(entries => {
    entries.forEach(en => {
      if (en.isIntersecting) {
        links.forEach(l => l.classList.remove("active"));
        const link = map.get(en.target);
        if (link) link.classList.add("active");
      }
    });
  }, { rootMargin: "-40% 0px -55% 0px" });
  map.forEach((_, sec) => secIO.observe(sec));
})();

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
const modalTitle = document.getElementById("modalTitle");
const modalText = document.getElementById("modalText");

const DEFAULT_MODAL_TEXT = "Оставьте телефон — перезвоним в течение 15 минут.";
let lastFocused = null;

function openModal(html, title) {
  lastFocused = document.activeElement;
  modalText.innerHTML = html || DEFAULT_MODAL_TEXT;
  modalTitle.textContent = title || "Заказать звонок";
  modal.classList.add("open");
  const input = document.getElementById("modalPhone");
  setTimeout(() => input && input.focus(), 60);
}
function closeModal() {
  modal.classList.remove("open");
  modalStatus.textContent = "";
  if (lastFocused && lastFocused.focus) lastFocused.focus();
}

// focus-trap: Tab не должен выходить за пределы открытой модалки
modal.addEventListener("keydown", e => {
  if (e.key !== "Tab" || !modal.classList.contains("open")) return;
  const focusables = modal.querySelectorAll("button, input, [href], select, textarea");
  if (!focusables.length) return;
  const first = focusables[0], last = focusables[focusables.length - 1];
  if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
});

document.getElementById("callBtn").addEventListener("click", () => openModal());

// ===== Кнопки «Узнать цену» в каталоге и модалка заказа =====
document.addEventListener("click", (e) => {
  const btn = e.target.closest(".js-order");
  if (!btn) return;
  const name = btn.dataset.name || "позиция";
  // справочная цена «от» из PRICE_DATA (первое совпадение по подстроке категории)
  const key = name.toLowerCase().split(" ")[0];
  const row = PRICE_DATA.find(i => i.name.toLowerCase().includes(key));
  const priceNote = row
    ? ` Ориентировочно от <b>${row.price.toLocaleString("ru-RU")} ₽/${row.unit}</b> — точную цену менеджер подтвердит под объём.`
    : "";
  openModal(
    `Категория: <b>${name}</b>. Оставьте телефон — менеджер перезвонит в течение 15 минут и назовёт актуальную цену и срок отгрузки.${priceNote}`,
    "Узнать цену: " + name
  );
});
modalClose.addEventListener("click", closeModal);
modal.addEventListener("click", e => { if (e.target === modal) closeModal(); });
document.addEventListener("keydown", e => { if (e.key === "Escape") closeModal(); });

modalForm.addEventListener("submit", async e => {
  e.preventDefault();
  const phone = document.getElementById("modalPhone").value;
  if (phone.replace(/\D/g, "").length < 11) {
    modalStatus.textContent = "Введите полный номер телефона";
    modalStatus.className = "form__status err";
    return;
  }
  const sent = await sendLead({ type: "callback", phone, page: location.href, ts: new Date().toISOString() });
  modalStatus.textContent = sent
    ? "Заявка принята! Перезвоним в течение 15 минут."
    : "Не удалось отправить. Позвоните сами: " + CONTACTS.phoneDisplay;
  modalStatus.className = "form__status " + (sent ? "ok" : "err");
  if (sent) setTimeout(closeModal, 2500);
});

/* =========================================================
   Форма заявки (валидация + отправка)
   ========================================================= */

async function sendLead(payload) {
  try {
    if (LEAD_WEBHOOK) {
      await fetch(LEAD_WEBHOOK, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
    } else {
      const q = JSON.parse(localStorage.getItem("leads_queue") || "[]");
      q.push(payload);
      localStorage.setItem("leads_queue", JSON.stringify(q));
    }
    return true;
  } catch (err) {
    console.error("Не удалось отправить заявку:", err);
    return false;
  }
}

document.addEventListener("click", e => {
  const goal = e.target.closest("#callBtn, .js-order, .header__phone, .fab--wa");
  if (goal && window.ym) { try { ym(METRIKA_ID, "reachGoal", "click_contact"); } catch (_) {} }
});

const leadForm = document.getElementById("leadForm");
const formStatus = document.getElementById("formStatus");

leadForm.addEventListener("submit", async e => {
  e.preventDefault();
  const name = leadForm.elements.name;
  const phone = leadForm.elements.phone;
  const agree = document.getElementById("agree");
  let ok = true;

  [name, phone].forEach(f => { f.classList.remove("invalid"); f.setAttribute("aria-invalid", "false"); });

  if (name.value.trim().length < 2) { name.classList.add("invalid"); name.setAttribute("aria-invalid", "true"); ok = false; }
  if (phone.value.replace(/\D/g, "").length < 11) { phone.classList.add("invalid"); phone.setAttribute("aria-invalid", "true"); ok = false; }
  if (!agree.checked) ok = false;

  if (!ok) {
    formStatus.textContent = "Проверьте поля: имя, телефон и согласие на обработку данных.";
    formStatus.className = "form__status err";
    return;
  }
  formStatus.textContent = "Отправляем…";
  formStatus.className = "form__status";
  const sent = await sendLead({
    type: "lead",
    name: name.value.trim(),
    phone: phone.value,
    message: leadForm.elements.message.value.trim(),
    page: location.href,
    ts: new Date().toISOString(),
  });
  formStatus.textContent = sent
    ? "Спасибо, " + name.value.trim() + "! Заявка принята — менеджер свяжется с вами."
    : "Не удалось отправить заявку. Позвоните нам: " + CONTACTS.phoneDisplay;
  formStatus.className = "form__status " + (sent ? "ok" : "err");
  if (sent) leadForm.reset();
  setTimeout(() => { formStatus.textContent = ""; }, 6000);
});

/* =========================================================
   Слайдер «Хиты продаж» (свайп + автопрокрутка)
   ========================================================= */
(function () {
  const viewport = document.getElementById("sliderViewport");
  const track = document.getElementById("sliderTrack");
  if (!viewport || !track) return;

  const slides = Array.from(track.children);
  const dotsBox = document.getElementById("sliderDots");
  const prev = document.getElementById("sliderPrev");
  const next = document.getElementById("sliderNext");
  let index = 0, timer = null;

  slides.forEach((s, i) => {
    const b = document.createElement("button");
    b.className = "slider__dot";
    b.type = "button";
    b.setAttribute("role", "tab");
    b.setAttribute("aria-label", "Слайд " + (i + 1) + ": " + s.querySelector(".slide__title").textContent);
    b.addEventListener("click", () => go(i, true));
    dotsBox.appendChild(b);
  });
  const dots = Array.from(dotsBox.children);

  function step() {
    const st = getComputedStyle(track);
    const gap = parseFloat(st.columnGap || st.gap) || 0;
    return slides[0].getBoundingClientRect().width + gap;
  }

  // сколько слайдов целиком влезает в окно
  function perView() {
    return Math.max(1, Math.floor((viewport.clientWidth + 16) / step()));
  }

  function maxIndex() {
    return Math.max(0, slides.length - perView());
  }

  function render() {
    track.style.transform = "translate3d(" + (-index * step()) + "px,0,0)";
    dots.forEach((d, i) => {
      d.classList.toggle("is-active", i === index);
      d.setAttribute("aria-selected", i === index ? "true" : "false");
    });
    prev.disabled = index === 0;
    next.disabled = index >= maxIndex();
  }

  function go(i, user) {
    index = Math.min(Math.max(0, i), maxIndex());
    render();
    if (user) restart();
  }

  function restart() {
    clearInterval(timer);
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    if (maxIndex() === 0) return;                 // на широком экране всё видно сразу
    timer = setInterval(() => go(index >= maxIndex() ? 0 : index + 1), 5200);
  }

  prev.addEventListener("click", () => go(index - 1, true));
  next.addEventListener("click", () => go(index + 1, true));
  window.addEventListener("resize", () => go(index));

  viewport.addEventListener("keydown", e => {
    if (e.key === "ArrowRight") go(index + 1, true);
    if (e.key === "ArrowLeft") go(index - 1, true);
  });

  // свайп: порог 40px, клик по ссылке гасим только если реально двигали
  let sx = 0, dragging = false, moved = 0;
  viewport.addEventListener("pointerdown", e => {
    dragging = true; sx = e.clientX; moved = 0;
    clearInterval(timer);
  }, { passive: true });
  viewport.addEventListener("pointermove", e => {
    if (!dragging) return;
    moved = e.clientX - sx;
  }, { passive: true });
  function end() {
    if (!dragging) return;
    dragging = false;
    if (moved < -40) { go(index + 1, true); lockClick(); }
    else if (moved > 40) { go(index - 1, true); lockClick(); }
    else restart();
  }
  let clickGuard = false;
  function lockClick() { clickGuard = true; setTimeout(() => { clickGuard = false; }, 250); }
  viewport.addEventListener("click", e => { if (clickGuard) e.preventDefault(); }, true);
  viewport.addEventListener("pointerup", end);
  viewport.addEventListener("pointercancel", end);
  viewport.addEventListener("pointerleave", end);

  viewport.addEventListener("mouseenter", () => clearInterval(timer));
  viewport.addEventListener("mouseleave", restart);

  render();
  restart();
})();
