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
   Скачивание документов (PDF / CSV / DOCX) из раздела «Документы»
   ========================================================= */
(function () {
  const COMPANY = "ООО «СтальПром» · +7 (495) 123-45-67 · info@stalprom-test.ru";

  function triggerDownload(blob, filename) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  /* ---- Мини-генератор PDF (без библиотек) ---- */
  // Транслитерация: в стандартных шрифтах PDF нет кириллицы
  const TRANSLIT = {
    "а":"a","б":"b","в":"v","г":"g","д":"d","е":"e","ё":"e","ж":"zh","з":"z","и":"i",
    "й":"y","к":"k","л":"l","м":"m","н":"n","о":"o","п":"p","р":"r","с":"s","т":"t",
    "у":"u","ф":"f","х":"kh","ц":"ts","ч":"ch","ш":"sh","щ":"shch","ъ":"","ы":"y","ь":"",
    "э":"e","ю":"yu","я":"ya"
  };
  function translit(str) {
    return str.split("").map(ch => {
      const low = ch.toLowerCase();
      let t = TRANSLIT[low];
      if (t === undefined) return ch;
      return ch === low ? t : t.charAt(0).toUpperCase() + t.slice(1);
    }).join("");
  }
  function pdfEscape(str) {
    return str.replace(/\\/g, "\\\\").replace(/\(/g, "\\(").replace(/\)/g, "\\)");
  }

  function buildPdfPrice() {
    const lines = [];
    lines.push({ text: "STALPROM - METAL ROLLING PRICE LIST", size: 16, bold: true });
    lines.push({ text: translit(COMPANY), size: 9 });
    lines.push({ text: "Date: 24.10.2026  |  Prices in RUB per ton, incl. VAT", size: 9 });
    lines.push({ text: "", size: 9 });
    lines.push({ text: pad("PRODUCT", 30) + pad("SIZE / GRADE", 26) + pad("UNIT", 6) + "PRICE", size: 10, bold: true });
    lines.push({ text: "-".repeat(72), size: 10 });
    PRICE_DATA.forEach(i => {
      const row = pad(translit(i.name), 30) + pad(translit(i.size), 26) + pad(i.unit, 6) + i.price.toLocaleString("en-US");
      lines.push({ text: row, size: 10 });
    });
    lines.push({ text: "-".repeat(72), size: 10 });
    lines.push({ text: translit("Prices are test data for demo purposes."), size: 9 });

    function pad(s, n) { s = String(s); return s.length >= n ? s.slice(0, n - 1) + " " : s + " ".repeat(n - s.length); }

    const content = [];
    content.push("BT /F1 10 Tf 50 780 Td 14 TL");
    let first = true;
    lines.forEach(l => {
      content.push("/F" + (l.bold ? 2 : 1) + " " + l.size + " Tf");
      if (!first) content.push("T*");
      content.push("(" + pdfEscape(l.text) + ") Tj");
      first = false;
    });
    content.push("ET");
    const stream = content.join("\n");

    const objects = [
      "<< /Type /Catalog /Pages 2 0 R >>",
      "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
      "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> /Contents 4 0 R >>",
      "<< /Length " + stream.length + " >>\nstream\n" + stream + "\nendstream",
      "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
      "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>"
    ];

    let pdf = "%PDF-1.4\n";
    const offsets = [];
    objects.forEach((obj, idx) => {
      offsets.push(pdf.length);
      pdf += (idx + 1) + " 0 obj\n" + obj + "\nendobj\n";
    });
    const xrefPos = pdf.length;
    pdf += "xref\n0 " + (objects.length + 1) + "\n0000000000 65535 f \n";
    offsets.forEach(o => { pdf += String(o).padStart(10, "0") + " 00000 n \n"; });
    pdf += "trailer\n<< /Size " + (objects.length + 1) + " /Root 1 0 R >>\nstartxref\n" + xrefPos + "\n%%EOF";

    return new Blob([pdf], { type: "application/pdf" });
  }

  /* ---- Генератор DOCX (WordprocessingML в zip без сжатия) ---- */
  async function crc32(bytes) {
    if (!crc32.table) {
      crc32.table = new Uint32Array(256).map((_, n) => {
        let c = n;
        for (let k = 0; k < 8; k++) c = c & 1 ? 0xEDB88320 ^ (c >>> 1) : c >>> 1;
        return c >>> 0;
      });
    }
    let c = 0xFFFFFFFF;
    for (const b of bytes) c = crc32.table[(c ^ b) & 0xFF] ^ (c >>> 8);
    return (c ^ 0xFFFFFFFF) >>> 0;
  }

  async function buildDocxRequest() {
    const xml = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:body>
<w:p><w:r><w:rPr><w:b/><w:sz w:val="32"/></w:rPr><w:t>ZAYAVKA NA POSAVKU METALLOPROKATA</w:t></w:r></w:p>
<w:p><w:r><w:t>To: OOO Stalprom, tel. +7 (495) 123-45-67, info@stalprom-test.ru</w:t></w:r></w:p>
<w:p><w:r><w:t>Date: ______  Request No.: ______</w:t></w:r></w:p>
<w:p><w:r><w:rPr><w:b/></w:rPr><w:t>Customer:</w:t></w:r></w:p>
<w:p><w:r><w:t>Company / FIO: ________________________________________</w:t></w:r></w:p>
<w:p><w:r><w:t>Contact phone: ______________  E-mail: ______________</w:t></w:r></w:p>
<w:p><w:r><w:t>Delivery address: ____________________________________</w:t></w:r></w:p>
<w:p><w:r><w:rPr><w:b/></w:rPr><w:t>Requested products:</w:t></w:r></w:p>
<w:tbl>
<w:tblPr><w:tblW w:w="5000" w:type="pct"/><w:tblBorders><w:top w:val="single"/><w:left w:val="single"/><w:bottom w:val="single"/><w:right w:val="single"/><w:insideH w:val="single"/><w:insideV w:val="single"/></w:tblBorders></w:tblPr>
<w:tr><w:tc><w:p><w:r><w:rPr><w:b/></w:rPr><w:t>No.</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:rPr><w:b/></w:rPr><w:t>Product</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:rPr><w:b/></w:rPr><w:t>Size / grade</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:rPr><w:b/></w:rPr><w:t>Qty</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:rPr><w:b/></w:rPr><w:t>Notes</w:t></w:r></w:p></w:tc></w:tr>
${[1,2,3,4,5].map(n => `<w:tr><w:tc><w:p><w:r><w:t>${n}</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t> </w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t> </w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t> </w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t> </w:t></w:r></w:p></w:tc></w:tr>`).join("")}
</w:tbl>
<w:p><w:r><w:t>Cutting to size: yes / no. Delivery: yes / no.</w:t></w:r></w:p>
<w:p><w:r><w:t>Signature: ______________</w:t></w:r></w:p>
</w:body></w:document>`;

    const contentTypes = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>`;

    const rels = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>`;

    const enc = new TextEncoder();
    const files = [
      { name: "[Content_Types].xml", data: enc.encode(contentTypes) },
      { name: "_rels/.rels", data: enc.encode(rels) },
      { name: "word/document.xml", data: enc.encode(xml) },
    ];

    // ZIP (store, без сжатия)
    const chunks = [];
    const central = [];
    let offset = 0;
    const le16 = n => new Uint8Array([n & 0xFF, (n >> 8) & 0xFF]);
    const le32 = n => new Uint8Array([n & 0xFF, (n >> 8) & 0xFF, (n >> 16) & 0xFF, (n >>> 24) & 0xFF]);

    for (const f of files) {
      const nameBytes = enc.encode(f.name);
      const crc = await crc32(f.data);
      const local = [
        le32(0x04034b50), le16(20), le16(0), le16(0), le16(0), le16(0),
        le32(crc), le32(f.data.length), le32(f.data.length),
        le16(nameBytes.length), le16(0)
      ];
      const head = concatU8(local);
      chunks.push(head, nameBytes, f.data);
      central.push({ nameBytes, crc, size: f.data.length, offset });
      offset += head.length + nameBytes.length + f.data.length;
    }

    let cdSize = 0;
    for (const c of central) {
      const rec = concatU8([
        le32(0x02014b50), le16(20), le16(20), le16(0), le16(0), le16(0), le16(0),
        le32(c.crc), le32(c.size), le32(c.size),
        le16(c.nameBytes.length), le16(0), le16(0), le16(0), le16(0), le32(0), le32(c.offset)
      ]);
      chunks.push(rec, c.nameBytes);
      cdSize += rec.length + c.nameBytes.length;
    }
    chunks.push(concatU8([
      le32(0x06054b50), le16(0), le16(0),
      le16(central.length), le16(central.length),
      le32(cdSize), le32(offset), le16(0)
    ]));

    return new Blob(chunks, { type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document" });
  }

  function concatU8(arrays) {
    const total = arrays.reduce((s, a) => s + a.length, 0);
    const out = new Uint8Array(total);
    let p = 0;
    for (const a of arrays) { out.set(a, p); p += a.length; }
    return out;
  }

  function downloadCsv() {
    const header = ["Наименование", "Размер / марка", "Ед. изм.", "Цена, руб/т", "Наличие"];
    const lines = [header.join(";")];
    PRICE_DATA.forEach(i => lines.push([i.name, i.size, i.unit, i.price, i.stock].join(";")));
    triggerDownload(new Blob(["\uFEFF" + lines.join("\r\n")], { type: "text/csv;charset=utf-8;" }), "price_stalprom_2026-10.csv");
  }

  document.querySelectorAll("[data-doc]").forEach(btn => {
    btn.addEventListener("click", async () => {
      const kind = btn.dataset.doc;
      try {
        if (kind === "price-pdf") triggerDownload(buildPdfPrice(), "price_stalprom_2026-10.pdf");
        else if (kind === "price-csv") downloadCsv();
        else if (kind === "request-docx") triggerDownload(await buildDocxRequest(), "zayavka_stalprom.docx");
        else if (kind === "certs") alert("Тестовая страница: файлы сертификатов появятся здесь после загрузки на сервер.");
      } catch (err) {
        console.error(err);
        alert("Не удалось сформировать файл: " + err.message);
      }
    });
  });
})();

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
