import re

h = open('index.html').read()

# ---------- 1. Extract offers product grid, then remove old #offers section ----------
i_off = h.find('<section class="section section--alt" id="offers">')
i_adv = h.find('<section class="section section--accent" id="advantages">')
assert i_off != -1 and i_adv != -1 and i_off < i_adv, (i_off, i_adv)
offers_seg = h[i_off:i_adv]

m = re.search(r'<div class="offers__grid">.*?</article>\s*</div>', offers_seg, re.S)
assert m, "offers grid not found"
offers_grid = m.group(0).rstrip()

ls_off = h.rfind('\n', 0, i_off) + 1
h = h[:ls_off] + h[i_adv:]

# ---------- 2. Append "Наши предложения" inside #catalog ----------
anchor = '<h3 class="gcard__title">Метизы</h3>'
ai = h.find(anchor)
assert ai != -1
tail = h[ai:]
close_idx = tail.find('</div>', tail.find('</a>'))
insert_at = ai + close_idx + len('</div>')
block = '''

        <div class="offers__head reveal">
          <h2 class="section__title">Наши предложения</h2>
          <span class="offers__updated">Обновлено: 28 октября 2026</span>
        </div>
''' + offers_grid
h = h[:insert_at] + block + h[insert_at:]

# ---------- 3. Replace services-delivery with 4 new sections ----------
i_sd = h.find('<section class="section section--alt" id="services-delivery">')
i_faq = h.find('<section class="section section--dark" id="faq">')
assert i_sd != -1 and i_faq != -1 and i_sd < i_faq, (i_sd, i_faq)
ls_sd = h.rfind('\n', 0, i_sd) + 1

new_sections = open('tools/new_sections.txt').read()
h = h[:ls_sd] + new_sections + h[i_faq:]

# ---------- 4. Update nav ----------
old_nav = '''        <a href="#catalog" class="nav__link">Каталог</a>
        <a href="#offers" class="nav__link">Предложения</a>
        <a href="#hit-slider" class="nav__link nav__link--optional">Хиты</a>
        <a href="#advantages" class="nav__link">Преимущества</a>
        <a href="#how" class="nav__link nav__link--optional">Этапы</a>
        <a href="#calculator" class="nav__link nav__link--optional">Калькулятор</a>
        <a href="#services-delivery" class="nav__link nav__link--optional">Доставка</a>'''
new_nav = '''        <a href="#catalog" class="nav__link">Каталог</a>
        <a href="#services" class="nav__link">Услуги</a>
        <a href="#sro" class="nav__link nav__link--optional">СРО</a>
        <a href="#stages" class="nav__link nav__link--optional">Этапы</a>
        <a href="#advantages" class="nav__link">Преимущества</a>
        <a href="#calculator" class="nav__link nav__link--optional">Калькулятор</a>
        <a href="#delivery" class="nav__link nav__link--optional">Доставка</a>'''
assert old_nav in h, "nav not matched"
h = h.replace(old_nav, new_nav)

open('index.html', 'w').write(h)
print("PATCH OK")
