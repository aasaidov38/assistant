#!/usr/bin/env python3
"""Сборка публичного сайта «Семейный ассистент» из исходников в многостраничный набор файлов.

Запуск:  python3 build.py OUT_DIR [--repo]
  OUT_DIR  куда положить готовые страницы (css/, js/, img/ копируются рядом)
  --repo   режим сервера: во все страницы добавляется <base href="/site/">, чтобы главная по адресу «/» нашла свои файлы.
Исходники: site.css, site.js, data.js (рядом), страницы описаны ниже. Историю и дорожную карту править в data.js.
"""
import sys, shutil, pathlib, html

HERE = pathlib.Path(__file__).resolve().parent
OUT = pathlib.Path(sys.argv[1]).resolve()
REPO = '--repo' in sys.argv
VERSION = '1.10.7'
PANEL = 'https://assistant.saidov.ru/web/'
DEMO = 'https://demo.assistant.saidov.ru/'

# ---------- фотографии (файл, подпись для скринридера) ----------
PH = {
 'a01': 'Иллюстрация: ассистентка в тёмно-синем жакете приветственно машет рукой',
 'a02': 'Иллюстрация: ассистентка со стопкой папок в светлом коридоре',
 'a03': 'Иллюстрация: ассистентка в магазине с пакетом хлеба и зелени',
 'a04': 'Иллюстрация: ассистентка с планшетом-папкой и пакетом овощей',
 'a05': 'Иллюстрация: ассистентка за столом с копилкой, калькулятором и графиком',
 'a06': 'Иллюстрация: ассистентка с папками бумаг',
 'a07': 'Иллюстрация: ассистентка в магазине с тележкой и списком покупок',
 'a08': 'Иллюстрация: ассистентка с блокнотом у окна с растениями',
 'a09': 'Иллюстрация: ассистентка с чашкой кофе и блокнотом утром',
 'a10': 'Иллюстрация: ассистентка с наличными и телефоном в руках',
 'a11': 'Иллюстрация: ассистентка в магазине с пакетом продуктов и планшетом',
 'a12': 'Иллюстрация: ассистентка с блокнотом за рабочим столом',
 'a13': 'Иллюстрация: ассистентка с блокнотом среди комнатных растений',
 'a14': 'Иллюстрация: ассистентка с кофе и ноутбуком',
 'a15': 'Иллюстрация: ассистентка в гарнитуре за ноутбуком',
 'a16': 'Иллюстрация: ассистентка с календарём и настольным звонком',
 'b01': 'Иллюстрация: ассистентка с ноутбуком, кофе и блокнотом',
 'b02': 'Иллюстрация: ассистентка за ноутбуком с чашкой кофе',
 'b03': 'Иллюстрация: ассистентка у синей стены с блокнотом и планшетом',
 'b04': 'Иллюстрация: ассистентка пишет в блокноте у окна',
 'b05': 'Иллюстрация: ассистентка с телефоном и блокнотом у окна',
 'b06': 'Иллюстрация: ассистентка с телефоном на фоне цветных кругов',
 'b07': 'Иллюстрация: ассистентка записывает показания счётчика',
 'b08': 'Иллюстрация: ассистентка у настенного термостата с блокнотом',
 'b09': 'Иллюстрация: ассистентка с таблеткой-органайзером и стаканом воды',
 'b10': 'Иллюстрация: ассистентка вечером с планшетом, на экране график',
 'b11': 'Иллюстрация: ассистентка за столом с ноутбуком и мини-компьютером',
 'b12': 'Иллюстрация: ассистентка с планшетом, широкий кадр',
 'b13': 'Иллюстрация: ассистентка в светлом офисе, широкий кадр',
 'b14': 'Иллюстрация: ассистентка с блокнотом среди растений, широкий кадр',
}
WIDE = {'b12', 'b13', 'b14'}


def ph(key, cls='', cap='', small=False, eager=False):
    w, h = (1400, 600) if key in WIDE else (480 if small else 840,) * 2
    f = key + ('-s' if small else '')
    c = f'<figcaption>{html.escape(cap)}</figcaption>' if cap else ''
    return (f'<figure class="ph {cls}"><img src="img/ph/{f}.webp" width="{w}" height="{h}" alt="{html.escape(PH[key])}"'
            f'{"" if eager else " loading=\"lazy\" decoding=\"async\""}>{c}</figure>')


# ---------- структура сайта ----------
# (файл, название в меню, значок, описание)
PAGES = {
 'index': ('Главная', '🏠', 'Семейный ассистент: деньги, питание, дела, дом, гараж и поездки — обычными фразами в Telegram, голосом, через Алису и в веб-панели.'),
 'features': ('Все возможности', '🧩', 'Двадцать одна возможность семейного ассистента по группам: деньги, еда, дела, дом, каналы.'),
 'money': ('Деньги и рынок', '💰', 'Траты, платежи, выписки, накопления, долги и курсы валют: всё одной фразой.'),
 'food': ('Еда, покупки и здоровье', '🍽', 'КБЖУ по описанию и фото, разбор питания по тарелке Гарварда, общий список покупок, карты лояльности, привычки и лекарства.'),
 'tasks': ('Дела и напоминания', '📝', 'Дела с напоминаниями и TickTick, утренняя сводка, вопросы к данным, настройки.'),
 'home': ('Дом, гараж и поездки', '🏠', 'Документы и счётчики, гараж и ТО мотоцикла, бюджет и сборы в поездку.'),
 'channels': ('Каналы и приложение', '💬', 'Telegram, голос, Алиса, веб-панель и ярлык-приложение.'),
 'examples': ('Примеры переписки', '💬', 'Как выглядят ответы ассистента: примеры на демо-данных.'),
 'screens': ('Скриншоты', '🖼', 'Веб-панель и чат на компьютере и телефоне, светлая и тёмная тема.'),
 'how': ('Как устроено', '🧱', 'Каналы, ядро, база и внешние сервисы.'),
 'privacy': ('Данные и безопасность', '🔒', 'Данные семьи остаются у семьи: свой сервер, вход без паролей, копии, личные разделы.'),
 'changelog': ('История изменений', '📜', 'Выпуски ассистента с октября 2026 года.'),
 'roadmap': ('Дорожная карта', '🧭', 'Что будет дальше: почта, версия 2.0.'),
 'app': ('Приложение', '📲', 'Как установить ассистента ярлыком на компьютер, Android и iPhone.'),
 'faq': ('Вопросы', '❓', 'Коротко о главном: стоимость, подключение семьи, банки, документация.'),
}
FEAT = ['money', 'food', 'tasks', 'home', 'channels']
MENU = [  # (подпись, [страницы]) — одна страница без меню = ссылка
 ('Возможности', ['features'] + FEAT),
 ('Показываем', ['examples', 'screens']),
 ('О проекте', ['how', 'privacy', 'changelog', 'roadmap']),
 ('app', None), ('faq', None),
]


def link(p, cur):
    cur_attr = ' aria-current="page"' if p == cur else ''
    return f'href="{p}.html"{cur_attr}'


def header(cur):
    items = []
    for label, pages in MENU:
        if pages is None:
            items.append(f'<a {link(label, cur)}>{PAGES[label][0]}</a>')
            continue
        is_cur = cur in pages
        sub = ''.join(f'<a {link(p, cur)}><span class="e">{PAGES[p][1]}</span>{PAGES[p][0]}</a>' for p in pages)
        items.append(f'<div class="dd{" cur" if is_cur else ""}"><button type="button" aria-expanded="false">{label}</button><div class="menu">{sub}</div></div>')
    return f'''<header class="top"><div class="in">
  <a class="brand" href="index.html"><span class="mark">СА</span><span><b>Семейный ассистент</b><small>версия {VERSION}</small></span></a>
  <nav class="main" aria-label="Разделы сайта">{''.join(items)}</nav>
  <div class="r"><div class="theme" role="group" aria-label="Тема оформления"><button data-t="system" title="Как в системе" aria-label="Как в системе">🖥</button><button data-t="light" title="Светлая" aria-label="Светлая">☀️</button><button data-t="dark" title="Тёмная" aria-label="Тёмная">🌙</button></div>
  <a class="btn demo" href="{DEMO}">🧪 Демо</a><a class="btn pri" href="{PANEL}">Панель</a><button class="burger" type="button" aria-label="Меню" aria-expanded="false">☰</button></div>
</div></header>'''


def footer():
    def col(title, pages):
        return f'<div><h4>{title}</h4><ul>' + ''.join(f'<li><a href="{p}.html">{PAGES[p][0]}</a></li>' for p in pages) + '</ul></div>'
    return f'''<footer><div class="in">
  <div><a class="brand" href="index.html"><span class="mark">СА</span><span><b>Семейный ассистент</b><small>версия {VERSION}</small></span></a>
  <p style="margin-top:10px;color:var(--muted)">Личный проект для семьи. Живёт на собственном сервере.</p></div>
  {col('Возможности', FEAT)}{col('Проект', ['how', 'privacy', 'changelog', 'roadmap'])}{col('Ещё', ['examples', 'screens', 'app', 'faq'])}
  <div class="fine"><span>Семейный ассистент · личный проект · 2026</span><span><a href="{DEMO}">Демо-кабинет</a> · <a href="{PANEL}">Веб-панель</a> · вход по ссылке из бота <code>/web</code></span></div>
</div></footer>'''


def shell(cur, body, extra_js='', zoom=False, title=None):
    t = PAGES[cur][0]
    full = 'Семейный ассистент' if cur == 'index' else f'{t} — Семейный ассистент'
    base = '<base href="/site/">\n' if REPO else ''
    dlg = '<dialog id="zoom" aria-label="Скриншот"><button class="x" id="zx" aria-label="Закрыть">✕</button><img id="zimg" alt=""><p id="zcap"></p></dialog>' if zoom else ''
    return f'''<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{base}<title>{html.escape(title or full)}</title>
<meta name="description" content="{html.escape(PAGES[cur][2])}">
<meta name="theme-color" content="#33519a">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Unbounded:wght@500;700&family=Onest:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<script>try{{const t=localStorage.getItem('site-theme')||(JSON.parse(localStorage.getItem('ui')||'{{}}').theme);if(t==='light'||t==='dark')document.documentElement.dataset.theme=t;}}catch(e){{}}</script>
<link rel="stylesheet" href="css/site.css">
</head>
<body>
<a class="skip" href="#main">К содержимому</a>
{header(cur)}
<main id="main"><div class="wrap">
{body}
</div></main>
{footer()}
{dlg}
<script src="js/data.js"></script>
<script src="js/site.js"></script>
<script>{extra_js}</script>
</body>
</html>
'''


# ---------- общие блоки ----------
def crumbs(cur, parent=None):
    parts = ['<a href="index.html">Главная</a>']
    if parent:
        parts.append(f'<span><a href="{parent}.html">{PAGES[parent][0]}</a></span>')
    parts.append(f'<span>{PAGES[cur][0]}</span>')
    return f'<nav class="crumbs" aria-label="Путь">{"".join(parts)}</nav>'


def subnav(cur):
    items = [('features', 'Все')] + [(p, PAGES[p][0]) for p in FEAT]
    return '<div class="subnav" aria-label="Возможности">' + ''.join(
        f'<a href="{p}.html"{" aria-current=\"page\"" if p == cur else ""}>{n}</a>' for p, n in items) + '</div>'


def phead(kicker, title, lead, photo, cur, parent=None, sub=True):
    return f'''{crumbs(cur, parent)}
<div class="phead"><div class="tx"><span class="kicker">{kicker}</span><h1>{title}</h1><p class="lead">{lead}</p>
{subnav(cur) if sub else ''}</div>{photo}</div>'''


def pager(cur):
    i = FEAT.index(cur)
    prev = f'<a href="{FEAT[i-1]}.html"><small>← Назад</small><b>{PAGES[FEAT[i-1]][0]}</b></a>' if i > 0 else ''
    nxt = f'<a href="{FEAT[i+1]}.html"><small>Дальше →</small><b>{PAGES[FEAT[i+1]][0]}</b></a>' if i < len(FEAT) - 1 else \
        '<a href="examples.html"><small>Дальше →</small><b>Примеры переписки</b></a>'
    return f'<div class="pager">{prev}{nxt}</div>'


def cta(photo_key, title='Попробовать ассистента', text='Панель открывается по одноразовой ссылке из бота, а на телефон или компьютер ставится ярлыком — без магазинов приложений.'):
    return f'''<div class="cta"><div class="tx"><h2>{title}</h2><p class="text">{text}</p>
<div class="row"><a class="btn pri" href="{PANEL}">Войти в панель →</a><a class="btn" href="{DEMO}">🧪 Попробовать демо</a><a class="btn" href="app.html">Установить как приложение</a></div></div>
{ph(photo_key, 'wide')}</div>'''


def section(kicker, title, inner, text=None, cls=''):
    t = f'<p class="text">{text}</p>' if text else ''
    return f'<section class="{cls}"><span class="kicker">{kicker}</span><h2>{title}</h2>{t}{inner}</section>'


def split(photo, title, body_html, rev=False):
    return f'<div class="split{" rev" if rev else ""}"><div class="tx"><h2 style="font-size:clamp(21px,2.4vw,27px)">{title}</h2>{body_html}</div>{photo}</div>'


def ul(*items):
    return '<ul>' + ''.join(f'<li>{i}</li>' for i in items) + '</ul>'


PAGES_HTML = {}

# ================= Главная =================
hero = f'''<div class="hero">
  <div class="tx">
    <span class="kicker">Личный проект · для всей семьи</span>
    <h1>Пишете как человеку. <em>Ассистент</em> ведёт учёт.</h1>
    <p class="lead">Траты, еда, дела, лекарства, счётчики, гараж, поездки и долги — одной фразой в Telegram, голосом, через Алису или в веб-панели. Без форм, таблиц и выбора категорий.</p>
    <div class="row"><a class="btn pri" href="{DEMO}">🧪 Попробовать демо</a><a class="btn" href="examples.html">Посмотреть примеры</a><a class="btn" href="features.html">Что умеет</a></div>
    <div class="facts" aria-label="Проект в цифрах">
      <div class="fact"><b>13</b><span>разделов хозяйства</span></div>
      <div class="fact"><b>5</b><span>способов написать</span></div>
      <div class="fact"><b>10</b><span>выпусков с октября</span></div>
      <div class="fact"><b>8,6&nbsp;тыс.</b><span>строк кода ядра</span></div>
    </div>
  </div>
  <div class="stage">{ph('a01', 'sq', eager=True)}
    <div class="chat" aria-label="Пример переписки">
      <div class="msg me">Пятёрочка 1450 продукты</div>
      <div class="msg bot">💾 Записал: ➖ <b>1 450 ₽</b> 🛒 Продукты<div class="btns"><span>🏷 Категория</span><span>❌ Отменить</span></div></div>
      <div class="msg me">дал Диме в долг 5000 до 15 ноября</div>
      <div class="msg bot">🤝 Записал: вам должны <b>Дима</b> 5 000 ₽<br>⏰ Напомню в день срока.</div>
    </div>
  </div>
</div>'''

about = section('01 · О проекте', 'Один помощник на всё домашнее хозяйство', '''
<div class="principles">
  <div class="card"><span class="n">принцип 1</span><h3>Одна фраза — одна запись</h3><p>«Заправил мото 1200» — это трата на топливо, привязанная к мотоциклу. Категорию и дату ассистент поставит сам.</p></div>
  <div class="card"><span class="n">принцип 2</span><h3>Всё можно отменить</h3><p>Под каждой записью есть кнопка «Отменить», а отменённое можно вернуть. В боте достаточно написать «отмени последнюю трату».</p></div>
  <div class="card"><span class="n">принцип 3</span><h3>Личное и общее</h3><p>Траты и покупки общие для семьи, а калории, лекарства и заметки у каждого свои. Владелец меняет это в настройках.</p></div>
  <div class="card"><span class="n">принцип 4</span><h3>Напоминает сам</h3><p>Платёж через 3 дня, ОСАГО через месяц, пора менять масло, 20-е — время передать показания. Ночью молчит, если включены тихие часы.</p></div>
</div>
<div class="channels" aria-label="Каналы"><span class="chip">💬 Telegram-бот</span><span class="chip">🎙 Голосовые</span><span class="chip">🗣 Яндекс Алиса</span><span class="chip">🌐 Веб-панель с графиками</span><span class="chip">📲 Ярлык на рабочем столе</span></div>''',
    text='Семейный ассистент — личный проект. Он живёт на своём сервере и помогает семье вести деньги, питание, дела и всё, что обычно теряется в заметках и напоминалках: сроки полисов, показания счётчиков, ТО мотоцикла, лекарства, кто кому сколько должен. Каждую фразу разбирает ИИ: он понимает, что вы имели в виду, и раскладывает запись по нужному разделу.')

day = section('02 · Один день', 'Как ассистент проводит день', f'''
<div class="day">
  <div class="card daycard">{ph('a09')}<div class="b"><span class="t">Утро</span><h3>Сводка до завтрака</h3>
    <p>Погода, дела на сегодня и ближайшие сроки. Если ожидается магнитная буря, в сводке появится отдельная строка.</p>
    <div class="say"><code>/today</code></div></div></div>
  <div class="card daycard">{ph('a07')}<div class="b"><span class="t">День</span><h3>Покупки и траты</h3>
    <p>Список покупок общий и разложен по отделам магазина. Чек сам вычёркивает купленное, а траты записываются одной фразой.</p>
    <div class="say"><code>закончилось молоко</code><code>Пятёрочка 1450 продукты</code></div></div></div>
  <div class="card daycard">{ph('b10')}<div class="b"><span class="t">Вечер</span><h3>Что съели и потратили</h3>
    <p>Калории за день к дневной цели, расходы по категориям на графиках. Раз в неделю приходит разбор с советами.</p>
    <div class="say"><code>/month</code></div></div></div>
</div>''', text='Три момента, в которых ассистент обычно нужен. Всё остальное он делает по вашей фразе.')

tiles = ''.join(f'''<a class="card tile" href="{p}.html">{ph(k, small=True)}<div class="b"><h3>{PAGES[p][0]}</h3><span>{PAGES[p][2]}</span><span class="more">Подробнее →</span></div></a>'''
                for p, k in [('money', 'a05'), ('food', 'a04'), ('tasks', 'b03'), ('home', 'b07'), ('channels', 'a15')])
what = section('03 · Что умеет', 'Тринадцать разделов, пять страниц', f'<div class="tiles">{tiles}</div><div class="row"><a class="btn" href="features.html">Все 21 возможность списком</a></div>',
               text='Разделы собраны в пять групп. У каждой своя страница: что умеет, какими фразами пользоваться и как это выглядит в панели.')

more = section('04 · Дальше', 'Посмотреть и разобраться', '''<div class="tiles">
<a class="card tile" href="examples.html" style="padding:18px"><h3>💬 Примеры переписки</h3><span style="color:var(--ink2);font-size:14px;margin-top:6px">Ответы ассистента с кнопками под ними.</span><span class="more">Открыть →</span></a>
<a class="card tile" href="screens.html" style="padding:18px"><h3>🖼 Скриншоты</h3><span style="color:var(--ink2);font-size:14px;margin-top:6px">Панель и чат, светлая и тёмная тема.</span><span class="more">Открыть →</span></a>
<a class="card tile" href="privacy.html" style="padding:18px"><h3>🔒 Данные и безопасность</h3><span style="color:var(--ink2);font-size:14px;margin-top:6px">Свой сервер, вход без паролей, копии.</span><span class="more">Открыть →</span></a>
<a class="card tile" href="roadmap.html" style="padding:18px"><h3>🧭 Что дальше</h3><span style="color:var(--ink2);font-size:14px;margin-top:6px">Почта в 1.11 и версия 2.0.</span><span class="more">Открыть →</span></a>
</div>''')

PAGES_HTML['index'] = (f'{hero}{about}{day}{what}{more}{cta("b13")}', '', False)

# ================= Возможности (общая) =================
PAGES_HTML['features'] = (
    phead('Возможности', 'Всё, что умеет ассистент', 'Двадцать одна возможность в пяти группах. Выберите группу или листайте весь список: у каждой карточки есть фразы, которыми это включается.',
          ph('a12', eager=True), 'features') +
    '<section class="tight"><div class="row" style="justify-content:space-between;align-items:center"><div class="filters" id="filters" role="group" aria-label="Фильтр по группам"></div><span class="mono" id="count" style="color:var(--muted)"></span></div><div class="features" id="grid"></div></section>'
    + cta('b12'), 'renderFeatHub();', False)


# ================= Страницы групп =================
def group_page(cur, kicker, title, lead, head_photo, blocks, shots, shot_phone=False, ex=True):
    shots_html = section('На экране', 'Как это выглядит в панели', '<div id="sh"></div>') if shots else ''
    ex_html = section('Примеры', 'Так отвечает ассистент', '<div class="chats" id="ex"></div>', cls='tight') if ex else ''
    body = (phead(kicker, title, lead, head_photo, cur, 'features') +
            section('Что умеет', 'Возможности', '<div class="features" id="fg"></div>', cls='tight') + blocks + ex_html + shots_html + pager(cur) + cta('b13' if cur != 'channels' else 'b14'))
    js = f"renderFeat('#fg','{cur}');"
    if ex: js += f"renderEx('#ex','{cur}');"
    if shots: js += f"renderShots('#sh',{shots!r},{str(shot_phone).lower()});"
    return body, js, bool(shots)


PAGES_HTML['money'] = group_page('money', 'Возможности · деньги', 'Деньги и рынок',
    'Траты, доходы, платежи, накопления и долги записываются одной фразой. Сумму, категорию и дату ассистент находит сам, а любую запись можно отменить.',
    ph('a05', eager=True),
    split(ph('a10'), 'Долги, платежи и накопления',
          '<p class="text">Кто кому должен и до какого числа; кредиты, ЖКХ и подписки с напоминаниями; цели со сроком, под которые ассистент считает, сколько откладывать в месяц.</p>' +
          ul('Частичные возвраты долга и напоминание в день срока', 'Выписки Сбера (PDF) и Т-Банка (CSV) сверяются с уже записанным', 'Накопления: «копим на отпуск 150000 к июлю»')) +
    split(ph('b10'), 'Курсы и недельный разбор',
          '<p class="text">Курсы ЦБ и крипты, индексы Мосбиржи, S&amp;P 500 и NASDAQ, ключевая ставка и инфляция показываются справочно — на главной и в «Деньгах». Раз в неделю приходит разбор с советами.</p>' +
          ul('Лимиты по категориям и отчёт за месяц', 'Вопросы к данным: «сколько потратили на продукты в октябре?»'), rev=True),
    ['finance', 'debts', 'home'])

PAGES_HTML['food'] = group_page('food', 'Возможности · еда', 'Еда, покупки и здоровье',
    'Калории по описанию или фото тарелки, общий список покупок, карты магазинов в телефоне, вода, шаги и лекарства по расписанию.',
    ph('a04', eager=True),
    split(ph('a07'), 'Покупки без бумажки',
          '<p class="text">Список общий для семьи и разложен по отделам магазина. Купленное вычёркивается по чеку. Скидочные карты хранятся в панели: на кассе штрихкод или QR открывается крупно.</p>' +
          ul('«закончилось молоко» — и оно в списке', 'Карты лояльности всей семьи в одном месте')) +
    split(ph('b09'), 'Привычки и лекарства',
          '<p class="text">Вода, шаги, сон и вес с целями. Лекарства — по расписанию с остатком упаковки. Есть краткая справка к препарату: как принимать, побочные эффекты, ограничения. Дозы ассистент не советует: только напоминает и считает остаток.</p>' +
          ul('Личные разделы не видны другим членам семьи', 'Напоминания можно отключить по темам и включить тихие часы'), rev=True),
    ['health', 'cards', 'cards-dark-phone'])

reminders = '<div class="remind">' + ''.join(
    f'<div class="card"><span class="e">{e}</span><div><h3>{t}</h3><p>{d}</p></div></div>' for e, t, d in [
        ('💳', 'Платёж', 'Предупредит за 3 дня до срока.'),
        ('🛡', 'Полис, документ, гарантия', 'ОСАГО — за месяц до окончания, так же с паспортами и гарантиями.'),
        ('🏍', 'ТО мотоцикла', 'Напомнит, когда пора менять масло или чистить цепь.'),
        ('🔢', 'Счётчики', 'В нужный день напомнит передать показания.'),
        ('🤝', 'Долг', 'В день срока: «вернули всё» или перенести на неделю.'),
        ('💊', 'Лекарства', 'По расписанию, с остатком упаковки.')]) + '</div>'
PAGES_HTML['tasks'] = group_page('tasks', 'Возможности · дела', 'Дела и напоминания',
    'Задачи себе и членам семьи, напоминания о платежах, документах и счётчиках, утренняя сводка. Дела синхронизируются с TickTick в обе стороны.',
    ph('b03', eager=True),
    section('Напоминания', 'Что и когда напомнит ассистент', reminders, cls='tight') +
    split(ph('a16'), 'Утренняя сводка',
          '<p class="text">Каждое утро — погода и дела на сегодня. Если ожидается магнитная буря, добавится строка с прогнозом. Город для погоды у каждого свой: он выбирается в настройках.</p>' +
          ul('Ночью ассистент молчит, если включены тихие часы', 'Срочное, например нулевой баланс ИИ-сервиса, придёт в любом случае')) +
    split(ph('a08'), 'Дела и вопросы',
          '<p class="text">«Маше в субботу забрать посылку» — задача появится у Маши, а в TickTick — у всех. Можно спросить обычными словами, и ИИ посчитает по базе.</p>' +
          ul('Выполнено в TickTick — выполнено в боте, и наоборот', 'Настройки: тема, тихие часы, личные и общие разделы'), rev=True),
    ['home', 'chat-phone'], shot_phone=False)

PAGES_HTML['home'] = group_page('home', 'Возможности · дом', 'Дом, гараж и поездки',
    'Сроки паспортов и полисов, показания счётчиков, ТО мотоцикла и расход топлива, бюджет и сборы в поездку — всё в одном месте.',
    ph('b07', eager=True),
    split(ph('b08'), 'Документы и счётчики',
          '<p class="text">Сроки паспортов, полисов и гарантий, показания счётчиков с расходом за период, семейные заметки. Номера документов и карт ассистент не хранит: только названия и сроки.</p>' +
          ul('«холодная вода 128,7» — показания записаны, расход посчитан', 'ОСАГО, гарантии и паспорта с напоминаниями')) +
    section('Гараж и поездки', 'Мотоцикл и дорога',
            '<div class="features"><div class="card feat"><div class="t"><span class="ic">🏍</span><h3>ТО по регламенту</h3></div><p>Пробег, регламент обслуживания, расход топлива на 100 км, сезонная резина и консервация. Просрочка видна сразу.</p><div class="say"><code>пробег мото 23500</code></div></div>'
            '<div class="card feat"><div class="t"><span class="ic">🧳</span><h3>Бюджет поездки</h3></div><p>Траты по дням, список сборов для мотоцикла и погода по маршруту.</p><div class="say"><code>поездка на Алтай с 5 по 8 июля на мото, бюджет 30000</code></div></div></div>', cls='tight'),
    ['house', 'garage-dark', 'trips'])

PAGES_HTML['channels'] = group_page('channels', 'Возможности · каналы', 'Каналы и приложение',
    'Один и тот же ассистент отвечает в Telegram, по голосу, через Алису и в веб-панели. Запись из любого канала видна во всех остальных.',
    ph('a15', eager=True),
    section('Где писать', 'Пять способов',
            '<div class="features">' + ''.join(f'<div class="card feat"><div class="t"><span class="ic">{e}</span><h3>{t}</h3></div><p>{d}</p></div>' for e, t, d in [
                ('💬', 'Telegram', 'Текст, голосовые, фото чека или тарелки, файлы с выписками.'),
                ('🎙', 'Голос', 'Держите кнопку и говорите — в Telegram и в браузере. Голос превращается в текст сервисом Whisper.'),
                ('🗣', 'Яндекс Алиса', 'Навык «Семейный помощник»: трата из Алисы видна в панели.'),
                ('🌐', 'Веб-панель и чат', 'Графики, история и строка ввода на каждом разделе.'),
                ('📲', 'Ярлык', 'Панель как приложение со своим значком и push-уведомлениями.')]) + '</div>', cls='tight') +
    split(ph('b05'), 'Обновления и порядок',
          '<p class="text">Ассистент сам сообщает о новой версии и обновляется по кнопке. Если что-то пошло не так, возвращается прежняя. В настройках видно, когда были копии и сколько места занимают база и фото.</p>' +
          ul('Остаток на счёте ИИ-сервиса и предупреждение, когда деньги заканчиваются', 'Фото сжимаются при загрузке')),
    ['chat-phone', 'chat-dark-phone', 'settings-dark'], shot_phone=False)

# ================= Примеры, скриншоты =================
PAGES_HTML['examples'] = (
    f'''{crumbs('examples')}<div class="phead"><div class="tx"><span class="kicker">Примеры</span><h1>Как это выглядит в переписке</h1>
<p class="lead">Так отвечает ассистент — пример на демо-данных. Кнопки под ответом работают как в Telegram: исправить порцию, отметить оплату, перенести напоминание.</p></div>{ph('b04', eager=True)}</div>
<section class="tight"><div class="chats" id="ex"></div></section>{cta('b13')}''', "renderEx('#ex','all');", False)

PAGES_HTML['screens'] = (
    f'''{crumbs('screens')}<section class="tight"><span class="kicker">Скриншоты</span><h1 style="font-size:clamp(28px,4vw,44px)">Веб-панель и чат</h1>
<p class="text">Панель открывается по одноразовой ссылке из бота. На скриншотах — демо-семья «Алексей и Мария»: данные придуманы. Нажмите на картинку, чтобы увеличить.</p>
<div class="seg" role="tablist" aria-label="Устройство"><button id="tDesk" class="on" role="tab" aria-selected="true">Компьютер</button><button id="tPhone" role="tab" aria-selected="false">Телефон</button></div>
<div class="shots" id="shots"></div></section>{cta('b12')}''', 'screensPage();', True)

# ================= Как устроено, безопасность =================
arch = '''<div class="arch">
<div class="col"><span class="lbl">Каналы</span>
<div class="node">💬 Telegram<small>текст, голос, фото, файлы</small></div><div class="node">🗣 Яндекс Алиса<small>навык «Семейный помощник»</small></div>
<div class="node">🌐 Веб-панель<small>графики, история, настройки</small></div><div class="node">✦ Чат в панели<small>как в Telegram, в браузере</small></div>
<div class="node">📲 Ярлык<small>панель как приложение</small></div></div>
<div class="core"><span class="lbl">Ядро · Python, FastAPI</span><h3>Понимает фразу и раскладывает по разделам</h3>
<div class="mods"><span>brain</span><span>finance</span><span>nutrition</span><span>tasks</span><span>shopping</span><span>budgets</span><span>bills</span><span>bank</span><span>savings</span><span>garage</span><span>papers</span><span>meters</span><span>notes</span><span>meds</span><span>trips</span><span>debts</span><span>habits</span><span>undo</span><span>scope</span><span>prefs</span><span>digest</span><span>morning</span><span>analytics</span></div>
<div class="node">🗄 PostgreSQL 16<small>отдельная роль «только чтение» для вопросов к данным</small></div>
<div class="node">⏱ Фоновые задачи<small>напоминания, сводки, синхронизация — каждые 30 секунд</small></div></div>
<div class="col"><span class="lbl">Сервисы</span>
<div class="node">🧠 Claude<small>разбор фраз, фото, PDF-выписок</small></div><div class="node">🎧 Whisper (Groq)<small>голос в текст</small></div>
<div class="node">✅ TickTick<small>дела в обе стороны</small></div><div class="node">🌦 Open-Meteo<small>погода и маршрут поездки</small></div>
<div class="node">🔗 n8n<small>события и автоматизации</small></div></div></div>'''
steps = '<div class="principles">' + ''.join(f'<div class="card"><span class="n">шаг {i}</span><h3>{t}</h3><p>{d}</p></div>' for i, (t, d) in enumerate([
    ('Вы пишете', 'В любом канале — текстом, голосом или фото.'), ('Ядро разбирает', 'ИИ понимает, что имелось в виду: трата, еда, задача, долг.'),
    ('Запись ложится в раздел', 'С категорией, датой и привязкой к человеку или мотоциклу.'), ('Ответ с кнопками', 'Исправить, отметить, перенести или отменить.')], 1)) + '</div>'
PAGES_HTML['how'] = (
    crumbs('how') + '<section class="tight"><span class="kicker">Как устроено</span><h1 style="font-size:clamp(28px,4vw,44px)">Каналы → ядро → база</h1>'
    '<p class="text">Все каналы отправляют сообщение в одно ядро. Поэтому трата из Алисы видна в панели, а голосовое из приложения отменяется кнопкой в Telegram. Ядро и база работают в Docker на одном сервере; рядом — n8n для автоматизаций.</p>' + arch + '</section>'
    + section('Путь фразы', 'Что происходит после «отправить»', steps) + f'<div style="margin-top:50px">{ph("b14", "wide")}</div>' +
    '<div class="pager"><a href="features.html"><small>← Назад</small><b>Все возможности</b></a><a href="privacy.html"><small>Дальше →</small><b>Данные и безопасность</b></a></div>', '', False)

PAGES_HTML['privacy'] = (
    phead('Данные и безопасность', 'Данные семьи остаются у семьи', 'Ассистент работает на собственном сервере, не показывает личное другим членам семьи и не принимает пароли.', ph('b11', eager=True), 'privacy', 'how', sub=False) +
    '<section class="tight"><ul class="checks">' + ''.join(f'<li>{x}</li>' for x in [
        'Всё хранится на своём сервере, а не в чужом облаке.', 'В панель входят по одноразовой ссылке из бота, без паролей.',
        'На вопросы к данным ИИ отвечает с правами «только чтение» и видит только свою семью.',
        'Номера документов, карт, пароли и ПИН-коды ассистент не сохраняет и отказывается записывать.',
        'Личные разделы не видны другим членам семьи — ни в боте, ни в панели, ни в отчётах.',
        'Каждую ночь делается копия базы, а зашифрованная копия уходит в Яндекс Облако и в Telegram; восстановление проверяется раз в месяц.',
        'Курсы, погода и бури берутся из открытых источников без регистрации; наружу уходит только название или координаты города, данных семьи там нет.',
        'Дозы лекарств ассистент не советует: только напоминает и считает остаток.',
        'Удалённое можно вернуть: записи помечаются отменёнными, а не стираются.']) + '</ul></section>' + cta('b13', 'Всё остаётся на вашем сервере', 'Посмотрите, как это устроено внутри, или откройте панель.') +
    '<div class="pager"><a href="how.html"><small>← Назад</small><b>Как устроено</b></a><a href="changelog.html"><small>Дальше →</small><b>История изменений</b></a></div>', '', False)

# ================= История, карта, приложение, вопросы =================
PAGES_HTML['changelog'] = (
    phead('История изменений', 'Десять выпусков', 'С первого запуска в октябре ассистент вырос от Telegram-бота до панели с графиками, голосом и приложением-ярлыком.', ph('b01', eager=True), 'changelog', 'how', sub=False) +
    '<section class="tight"><div class="card chartcard"><h3>Сколько умеет ассистент</h3><p class="sub">Число возможностей из списка изменений, нарастающим итогом по выпускам</p><svg class="bars" id="growth" role="img" aria-label="Рост числа возможностей по версиям"></svg></div><div class="log" id="log"></div></section>' +
    '<div class="pager"><a href="privacy.html"><small>← Назад</small><b>Данные и безопасность</b></a><a href="roadmap.html"><small>Дальше →</small><b>Дорожная карта</b></a></div>', 'renderLog();', False)

PAGES_HTML['roadmap'] = (
    phead('Дорожная карта', 'Что дальше', 'Порядок выпусков согласован, сроки — по мере готовности. Каждый выпуск сначала проходит проверку на тестовой базе, потом приходит владельцу в бот кнопкой «Обновить».', ph('b02', eager=True), 'roadmap', 'how', sub=False) +
    '<section class="tight"><div class="legend"><span><i style="background:var(--signal-soft);border:1px solid var(--signal)"></i>следующий шаг</span><span><i style="background:var(--card);border:1px solid var(--line)"></i>в плане</span><span><span class="size">S</span>быстро</span><span><span class="size">M</span>средне</span><span><span class="size">L</span>много работы</span></div><div class="lanes" id="lanes"></div></section>' +
    '<div class="pager"><a href="changelog.html"><small>← Назад</small><b>История изменений</b></a><a href="app.html"><small>Дальше →</small><b>Приложение</b></a></div>', 'renderLanes();', False)

PAGES_HTML['app'] = (
    phead('Приложение', 'Как приложение, без магазина', 'Ассистент устанавливается ярлыком: одним нажатием из браузера. Получается отдельное окно со своим значком, без адресной строки, с push-уведомлениями. Ничего не нужно скачивать из магазинов, а обновляется оно вместе с сервером.', ph('b06', eager=True), 'app', sub=False) +
    section('Установка', 'Выберите своё устройство', '''<div class="apps">
<div class="card"><h3>💻 Компьютер <span class="st ok">работает</span></h3><p>Chrome или Edge: значок установки в адресной строке (или меню ⋮ → «Установить»). Ярлык появится на рабочем столе и в меню «Пуск».</p></div>
<div class="card"><h3>🤖 Android <span class="st ok">работает</span></h3><p>Chrome: меню ⋮ → «Добавить на главный экран» или «Установить приложение». Значок появится среди остальных приложений.</p></div>
<div class="card"><h3>🍏 iPhone <span class="st ok">работает</span></h3><p>Safari → «Поделиться» → «На экран Домой»: панель открывается как приложение и присылает push-уведомления (iOS 16.4+).</p></div></div>''', cls='tight') +
    split(ph('b05'), 'Проще всего — из бота', '<p class="text">В боте есть команда <code>/app</code>: она пришлёт подсказку для вашего устройства. На главной странице панели на телефоне есть блок «Установить».</p>' + ul('Android — в одно нажатие', 'iPhone — пошаговая подсказка')) +
    section('На экране', 'Как это выглядит на телефоне', '<div id="sh"></div>', cls='tight') + cta('b14'), "renderShots('#sh',['home-dark-phone','card-barcode-phone','chat-phone'],true);", True)

faqs = [
    ('Можно ли попробовать без установки?', 'Да. Откройте демо-кабинет: это настоящая панель с вымышленной семьёй и историей за полгода. Меняйте что угодно, ваши правки видны только вам и через 20 минут без действий сбрасываются. ИИ-чат в демо работает, но с лимитом сообщений.'),
    ('Можно подключить нашу семью?', 'Пока ассистент работает для одной семьи. В версии 2.0 появятся два варианта: «коробка» — установка на свой сервер одной командой, и общий сервис по приглашению для знакомых.'),
    ('Сколько это стоит?', 'Сервер — обычный недорогой VPS. Погода и бэкап в Яндекс в пределах 1 ГБ бесплатны. Платные только запросы к ИИ: для семьи это небольшие деньги в месяц.'),
    ('Что будет, если ИИ не понял фразу?', 'Ассистент переспросит или предложит кнопки выбора. Неверную запись можно поправить кнопкой под ответом или отменить фразой «отмени последнюю трату».'),
    ('Подтягивает ли он данные из банков?', 'Официального API для личных карт у банков пока нет. Сейчас работает импорт выписок Сбера (PDF) и Т-Банка (CSV) со сверкой с уже записанным.'),
    ('Нужно ли что-то скачивать из магазина приложений?', 'Нет. Панель ставится ярлыком из браузера на компьютер, Android и iPhone. Подробности — на странице «Приложение».'),
    ('Где посмотреть подробную инструкцию?', 'В репозитории на GitHub есть инструкция docs/INSTALL.md (41 раздел): от первой установки до каждой команды бота. Для обновлений есть готовые задания для Claude Code.')]
PAGES_HTML['faq'] = (
    phead('Вопросы', 'Коротко о главном', 'Стоимость, подключение семьи, банки и документация.', ph('a13', eager=True), 'faq', sub=False) +
    '<section class="tight"><div class="faq">' + ''.join(f'<details class="q"><summary>{q}</summary><p>{a}</p></details>' for q, a in faqs) + '</div></section>' + cta('b13', 'Остались вопросы?', 'Откройте панель и спросите ассистента обычными словами — он отвечает по вашим данным.'), '', False)


# ---------- запись ----------
def main():
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'css').mkdir(exist_ok=True); (OUT / 'js').mkdir(exist_ok=True)
    shutil.copy(HERE / 'site.css', OUT / 'css' / 'site.css')
    shutil.copy(HERE / 'site.js', OUT / 'js' / 'site.js')
    shutil.copy(HERE / 'data.js', OUT / 'js' / 'data.js')
    for cur, (body, js, zoom) in PAGES_HTML.items():
        (OUT / f'{cur}.html').write_text(shell(cur, body, js, zoom), encoding='utf-8')
    print('страниц:', len(PAGES_HTML))


if __name__ == '__main__':
    main()
