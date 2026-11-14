#!/usr/bin/env python3
"""Фото сайта: PNG из папки -> WebP без обрезки + ph_dims.json (размеры для width/height).
Запуск: python3 site-src/make_images.py ПАПКА_С_PNG [ВЫХОД=core/app/static/site/img/ph]
Кадр сохраняется целиком, в исходных пропорциях (3:2, 4:5, 21:9 и т.п.). Крупный — 1200 px по ширине
для горизонтальных, 840 px для вертикальных, 1680 px для широких; плитка *-s — 480 px по ширине."""
import json, sys, pathlib
from PIL import Image

SMALL = {'a01', 'a04', 'a05', 'b03', 'b07', 'a15'}   # плитки на главной
src = pathlib.Path(sys.argv[1])
out = pathlib.Path(sys.argv[2] if len(sys.argv) > 2 else 'core/app/static/site/img/ph')
out.mkdir(parents=True, exist_ok=True)
dims_file = pathlib.Path(__file__).with_name('ph_dims.json')
dims = json.loads(dims_file.read_text()) if dims_file.exists() else {}

def save(im, name, width):
    h = round(im.height * width / im.width)
    r = im.resize((width, h), Image.LANCZOS)
    r.save(out / f'{name}.webp', 'WEBP', quality=82, method=6)
    dims[name] = [width, h]

import hashlib
seen = {}
PREFER = {'h01'}   # при дубле остаётся этот ключ (кадр с таблетницей — это h01, а не b03)
for f in sorted(src.glob('*.png'), key=lambda f: (f.stem not in PREFER, f.stem)):
    k = f.stem
    if k == 'ref' or k.startswith('polza'):
        continue
    h = hashlib.md5(f.read_bytes()).hexdigest()
    if h in seen:
        print(f'ПРОПУЩЕН {k}: файл совпадает с {seen[h]} (прислан дубль)')
        continue
    seen[h] = k
    im = Image.open(f).convert('RGB')
    ratio = im.width / im.height
    big = 1680 if ratio > 2 else (1200 if ratio > 1 else 840)
    save(im, k, min(big, im.width))
    if k in SMALL:
        save(im, k + '-s', 480)
    print(k, im.size, '->', dims[k])
dims_file.write_text(json.dumps(dims, ensure_ascii=False, sort_keys=True, indent=0) + '\n')
