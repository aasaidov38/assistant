"""Версия 1.10.4: многостраничный сайт — страницы, ссылки, файлы, типы файлов (без базы, только файлы)."""
import re
import _env  # noqa: F401  (пути)
from pathlib import Path
from app import release, site

FAILS = []
REPO = Path(__file__).resolve().parent.parent
SITE = REPO / "core" / "app" / "static" / "site"


def check(cond, name, info=""):
    print(("  ✔ " if cond else "  ✖ ") + name + ("" if cond else f"  → {str(info)[:500]}"))
    if not cond:
        FAILS.append(name)


PAGES = ["index", "features", "money", "food", "tasks", "home", "channels", "examples", "screens",
         "how", "privacy", "changelog", "roadmap", "app", "faq"]

check(release.VERSION != "" and (REPO / "VERSION").read_text().strip() == release.VERSION, "версия в VERSION и release.py совпадает")
check(bool(release.notes("1.10.4")), "в CHANGELOG есть раздел 1.10.4", release.notes("1.10.4"))
check(".html" in site.TYPES, "сайт отдаёт файлы .html")

for p in PAGES:
    f = SITE / f"{p}.html"
    check(f.is_file(), f"страница {p}.html есть")
    if not f.is_file():
        continue
    html = f.read_text(encoding="utf-8")
    check('<base href="/site/">' in html, f"{p}: адрес-основа /site/ для главной по адресу «/»")
    check("<title>" in html and 'lang="ru"' in html, f"{p}: заголовок и язык")
    check("Иркутск" not in html, f"{p}: нет упоминания города")
    # все локальные ссылки и файлы существуют
    refs = set(re.findall(r'(?:href|src)="([^"#:]+?)"', html))
    for r in sorted(refs):
        if r.startswith(("http", "/", "mailto")):
            continue
        check((SITE / r).is_file(), f"{p}: файл {r} существует")

js = (SITE / "js" / "data.js").read_text(encoding="utf-8")
check("'1.10.4'" in js, "в истории сайта есть версия 1.10.4")
# картинки скриншотов и фото, которые подставляет скрипт
for name in re.findall(r"\['([a-z\-]+)', '[^']+', '[^']+'\],?\n", js.split("const DESK")[1].split("const LOG")[0]):
    check((SITE / "img" / "shots" / f"{name}.jpg").is_file(), f"скриншот {name}.jpg есть")
check(len(list((SITE / "img" / "ph").glob("*.webp"))) >= 30, "фотографии ассистента лежат в img/ph")
css = (SITE / "css" / "site.css").read_text(encoding="utf-8")
check("object-fit:cover" not in css.split(".ph img")[1].split("}")[0], "фото показываются без обрезки")

print("ИТОГ:", "всё прошло" if not FAILS else f"ошибок {len(FAILS)}: {FAILS}")
