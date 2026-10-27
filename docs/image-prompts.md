# Промты для генерации изображений (Металлостал.рф)

Если нужен **фотореализм уровня «съёмка на складе»**, эти кадры лучше сделать в
генераторе картинок (Midjourney / SDXL / Flux / DALL·E). Ниже — готовые промты:
общий стиль + по кадру на каждый блок сайта.

В репозитории уже лежат процедурные заглушки, сгенерированные скриптом
`tools/gen_images.py` (PIL, 1200×800). Они замещаются файлами с теми же именами.

## Общие правила (добавлять к каждому промту)

**Style suffix (приклеивать в конец):**
```
photorealistic industrial photograph, shot on 35mm f/2.8, metal warehouse interior,
cool steel-grey palette with warm orange safety accents, dramatic side backlight from
open hangar doors, volumetric dust in light beams, shallow depth of field, fine grain,
no people, no text, no logos, no watermarks, high detail, 3:2 aspect ratio
```

**Negative prompt:**
```
cartoon, illustration, 3d render look, oversaturated, plastic metal, fantasy rust,
text, letters, watermark, logo, people, faces, hands, lowres, jpeg artifacts,
warped geometry, messy wires
```

**Требования к файлам на сайте:** 1200×800 px (слайдер — кроп 3:4), JPG q≈82,
`loading="lazy"` кроме первого кадра, осмысленный `alt`.

---

## 1. Главный экран (hero) — `assets/img/hero-steel.jpg`
```
Wide interior of a Russian metal warehouse, bundles of ribbed rebar (A500S reinforcement
bars) stacked on the concrete floor in the foreground, tall pallet racks with steel pipes
in the background, gantry crane overhead, daylight pouring through huge open hangar gates,
orange accents on the machinery
```

## 2. Слайдер «Хиты продаж» (6 слайдов, вертикальный кроп 3:4)

| Файл | Промт |
|---|---|
| `rebar.jpg` | `Close-up of a bundle of ribbed steel rebar bars, diagonal composition, ringed deformation ribs visible, binding wire, dark warehouse background, rim light on the cut ends` |
| `beam.jpg` | `Stack of hot-rolled I-beams seen from the end, rows of grey steel cross-sections receding into darkness, orange crane hook slightly out of focus` |
| `sheet.jpg` | `Neat stack of hot-rolled steel plates on wooden sleepers, painted edge marking, roller table in front, industrial hall behind` |
| `pipe.jpg` | `Bundle of round and square steel pipes, camera looking straight into the pipe ends, honeycomb pattern of circular sections, blue-grey sheen` |
| `channel.jpg` | `Row of steel U-channel profiles standing upright on a warehouse floor, long perspective lines, dust in the air, side light` |
| `inox.jpg` | `Polished stainless steel sheet and tube, mirror brushed surface reflecting soft studio light, cool silver tones, minimal dark background` |

## 3. Категории каталога (8 карточек, 3:2)

Те же шесть плюс два отдельных кадра:

| Файл | Промт |
|---|---|
| `angle.jpg` | `Group of equal-leg steel angle bars leaning against each other, L-shaped cross-sections facing camera, matte grey metal, slight mill scale` |
| `round.jpg` | `Pile of round steel bar stock, different diameters, cut ends with heat tint, industrial shelving behind` |

## 4. Блок преимуществ (4 карточки с фото)

| Файл | Промт |
|---|---|
| `adv-cut.jpg` | `Circular saw cutting a steel beam to size, bright shower of orange sparks, protective guard, motion-blurred blade, dark shop background` |
| `adv-delivery.jpg` | `Long-bed truck with knave manipulator crane loading a bundle of 12 m rebar at a metal base, orange cab, evening light, wet asphalt reflections` |
| `adv-docs.jpg` | `Quality passport and GOST certificate on a clipboard lying on a stack of steel plate, rubber stamp seal, factory spray-painted markings on the metal edge` |
| `adv-price.jpg` | `Price tag hanging on a bundle of reinforcement bars in a warehouse aisle, racks with pipes behind, shallow depth of field, tag area clean for text overlay` |

## 5. Блок «Как проходит поставка» (4 шага)

Сейчас в вёрстке переиспользуются `beam.jpg`, `sheet.jpg`, `adv-delivery.jpg`,
`adv-docs.jpg`. Если нужны уникальные кадры:

```
step 01: specification sheet over a calculator and short steel samples on a desk, warehouse bokeh
step 02: guillotine shear cutting a steel sheet, measuring tape on the metal, no sparks
step 03: crane lifting a packed bundle over a truck bed, slings and hooks, dusk light
step 04: signed paperwork package with invoice and waybill next to a laptop, industrial office
```

## 6. Блок «О компании» — `assets/img/about-warehouse.jpg`
```
Aerial view of an open-air metal storage yard, orderly stacks of plates, pipes and beams
on sleepers, gantry crane crossing the yard, truck between the stacks, overcast northern
light, wide angle
```

---

## Подсказки по консистентности
- Один и тот же seed / референс стиля для всех кадров — иначе лента выглядит сборным комплектом.
- Палитра: холодный серый металл `#7c8894` + оранжевый акцент `#ff7a1a` (фирменный).
- Свет контровой/боковой, фон тёмный — белый текст поверх карточек читается без затемнения.
- Нижняя треть кадра визуально тише: туда ложится подложка текста слайда.
- Никакого читаемого текста внутри кадра — подписи добавляет HTML.
