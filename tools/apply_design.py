#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Дизайн-правки P0/P1: SVG вместо эмодзи, дедуп сертификатов, стрелки этапов."""
import io, sys

P = '/workspace/site/index.html'
h = io.open(P, encoding='utf-8').read()
orig = h

ico = lambda d: ('<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
                 'stroke-width="1.8" aria-hidden="true">' + d + '</svg>')
SVG_BLDG  = ico('<path d="M4 21V5a1 1 0 0 1 1-1h9a1 1 0 0 1 1 1v16M15 9h4a1 1 0 0 1 1 1v11M7 8h4M7 12h4M7 16h4"/>')
SVG_PHONE = ico('<path d="M5 3h4l2 5-2.5 1.5a12 12 0 0 0 6 6L16 13l5 2v4a2 2 0 0 1-2 2A16 16 0 0 1 3 5a2 2 0 0 1 2-2z"/>')
SVG_MAIL  = ico('<rect x="2" y="4" width="20" height="16" rx="2"/><path d="m2 7 10 6L22 7"/>')
SVG_CLOCK = ico('<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.5 2"/>')

def rep(old, new, count=1):
    global h
    if old not in h:
        print('MISS:', old[:70]); return False
    h = h.replace(old, new, count); print('OK  :', old[:60]); return True

# 1) Эмодзи -> SVG (контакты, sticky-bar)
rep('<p><b>🏢 Офис (единственный):</b>', '<p>' + SVG_BLDG + '<b>Офис (единственный):</b>')
rep('<p>📞 <a href="tel:+73432020001">',   '<p>' + SVG_PHONE + '<a href="tel:+73432020001">')
rep('<p>✉️ <a href="mailto:4499@metallostal.ru">', '<p>' + SVG_MAIL + '<a href="mailto:4499@metallostal.ru">')
rep('<p>🕒 пн-вс 10:00–18:00</p>', '<p>' + SVG_CLOCK + 'пн-вс 10:00–18:00</p>')
rep('class="sticky-bar__call">📞 Позвонить', 'class="sticky-bar__call">' + SVG_PHONE + 'Позвонить')

# 2) Дедуп «сертификаты качества»: лид преимуществ -> уникальный текст
old_lead = ('<p class="adv__lead reveal"><b class="adv__cert">Вся продукция, реализуемая компанией «МЕТАЛЛОСТАЛЬ», '
            'имеет сертификаты качества.</b> С нами вы можете не беспокоиться о задержке доставки или ошибочном сорте '
            'металлопроката. Мы оправдываем репутацию ответственного поставщика и ценим наших клиентов. Узнать '
            'дополнительную информацию о компании, услугах, сроках выполнения индивидуального заказа, ценах на '
            'продукцию можно в офисе или отправив сообщение.</p>')
new_lead = ('<p class="adv__lead reveal">Отгружаем только по паспорту качества: контроль партии на заводе-изготовителе, '
            '<b class="adv__cert">сертификаты и РТ-приёмка на каждую позицию</b>. Цены и сроки индивидуального заказа '
            'уточняйте в офисе или сообщением менеджеру — ответим в рабочее время 10:00–18:00.</p>')
rep(old_lead, new_lead)

# 3) Этапы: соединительные стрелки между карточками 01->02->03->04
arrow = '<span class="stage__arrow" aria-hidden="true"></span>\n              </li>\n              <li class="stage">'
for num in ('01', '02', '03'):
    # вставляем стрелку перед закрывающим </li> того этапа, за которым идёт следующий
    idx = h.find('>' + num + '<')
    if idx == -1: print('MISS stage', num); continue
    close = h.find('</li>', idx)
    h = h[:close] + '\n              ' + arrow.split('\n              ', 1)[0] + h[close:]
    print('OK  : arrow after stage', num)

io.open(P, 'w', encoding='utf-8').write(h)
print('changed bytes:', len(h) - len(orig))
