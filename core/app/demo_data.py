"""Вымышленные данные для демо-кабинета (версия 1.10.7).

Вымышленная семья Ветровых: Алексей и Мария, двое детей (Соня и Миша). История за ~6 месяцев (demo_history_days):
траты и доходы, чеки по позициям, питание, вода/шаги/сон/вес, дела, покупки, регулярные платежи, накопления,
машина и мотоцикл (пробег, ТО), документы и гарантии, счётчики, заметки, лекарства, поездки, долги, карты, выписка.

seed(pool)         — собирает заготовку: всё до вчерашнего дня (вызывается раз в сутки, после миграций);
fresh_today(pool)  — дописывает «сегодня» по текущему времени (вызывается для каждого посетителя после копирования заготовки).

Данные случайные, но детерминированные (одно зерно): картина одна и та же, сдвигаются только даты. Настоящих людей,
адресов, номеров документов и банковских карт здесь нет; номера скидочных карт — придуманные.
Все SQL собираются текстом и выполняются одним сценарием (быстро и работает и с asyncpg, и с тестовой заглушкой).
"""
import json
import random
import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from .config import get_settings
from .util import now, today

SEED = 20261108
FAMILY = "Семья Ветровых"
USERS = [(1, "Алексей", 2200, "demo-alexey"), (2, "Мария", 1800, "demo-maria")]
CITY = {"name": "Москва", "label": "Москва, Россия", "lat": 55.7558, "lon": 37.6173, "tz": "Europe/Moscow"}
NS = uuid.UUID("5f0f2a52-6c2b-4a52-9d2f-3d5c1b7e0a11")


# ---------- литералы SQL ----------

class Arr(list):
    """Массив PostgreSQL: Arr([...], 'time')."""
    def __init__(self, items, cast):
        super().__init__(items)
        self.cast = cast


def q(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, Arr):
        return "ARRAY[" + ",".join(q(x) for x in v) + f"]::{v.cast}[]" if v else f"'{{}}'::{v.cast}[]"
    if isinstance(v, (int, float, Decimal)):
        return repr(round(v, 4)) if isinstance(v, float) else str(v)
    if isinstance(v, datetime):
        return "'" + v.isoformat() + "'"
    if isinstance(v, date):
        return "'" + v.isoformat() + "'"
    if isinstance(v, time):
        return "'" + v.strftime("%H:%M") + "'"
    if isinstance(v, (dict, list)):
        return "'" + json.dumps(v, ensure_ascii=False).replace("'", "''") + "'::jsonb"
    return "'" + str(v).replace("'", "''") + "'"


def ins(table: str, cols: list[str], rows: list[list], batch: int = 250) -> list[str]:
    out = []
    for i in range(0, len(rows), batch):
        vals = ",\n".join("(" + ",".join(q(x) for x in r) + ")" for r in rows[i:i + batch])
        out.append(f"INSERT INTO {table} ({', '.join(cols)}) VALUES\n{vals};")
    return out


def fix_seq(table: str) -> str:
    return f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), (SELECT coalesce(max(id), 1) FROM {table}));"


# ---------- справочники ----------

# (название, граммы, ккал, белки, жиры, углеводы)
BREAKFAST = [("Овсянка на молоке с бананом", 320, 340, 11, 8, 56), ("Омлет из двух яиц с помидором и тостом", 260, 380, 22, 24, 18),
             ("Сырники со сметаной", 220, 430, 25, 18, 40), ("Гречневая каша с маслом", 280, 310, 10, 7, 53),
             ("Бутерброды с сыром и кофе с молоком", 200, 420, 17, 20, 40), ("Творог с ягодами и мёдом", 230, 290, 25, 6, 31),
             ("Йогурт с мюсли", 280, 350, 12, 9, 55), ("Блины с творогом", 240, 520, 20, 22, 58)]
LUNCH = [("Борщ со сметаной", 400, 260, 11, 12, 28), ("Куриный суп с лапшой", 400, 210, 14, 6, 24),
         ("Гречка с куриной грудкой", 350, 520, 42, 12, 58), ("Паста с томатным соусом и сыром", 350, 610, 22, 18, 88),
         ("Плов с говядиной", 380, 640, 28, 28, 68), ("Салат Цезарь с курицей", 300, 430, 29, 26, 20),
         ("Рис с рыбой на пару", 360, 480, 32, 10, 64), ("Котлеты с картофельным пюре", 380, 690, 27, 36, 62),
         ("Суп-пюре из тыквы", 350, 240, 5, 12, 28), ("Щи с говядиной", 400, 240, 14, 10, 24),
         ("Бургер и картофель фри", 420, 1020, 33, 54, 98), ("Ролл с курицей и овощами", 260, 480, 26, 18, 52)]
DINNER = [("Запечённая курица с овощами", 380, 450, 40, 20, 24), ("Лосось с картофелем", 340, 580, 38, 28, 40),
          ("Греческий салат с сыром", 280, 330, 11, 26, 14), ("Овощное рагу с индейкой", 360, 390, 31, 14, 32),
          ("Вареники с картошкой", 300, 520, 14, 15, 80), ("Рыба с булгуром", 340, 470, 34, 14, 50),
          ("Тушёная капуста с колбасками", 350, 560, 21, 38, 30), ("Пицца, два куска", 260, 640, 26, 24, 80),
          ("Курица терияки с рисом", 380, 590, 37, 14, 76), ("Омлет с сыром и овощами", 250, 400, 24, 30, 5),
          ("Творожная запеканка", 220, 380, 28, 12, 38), ("Лазанья", 320, 620, 30, 28, 58)]
SNACK = [("Яблоко", 180, 95, 0.5, 0.3, 25), ("Банан", 120, 105, 1.3, 0.4, 27), ("Горсть орехов", 30, 190, 6, 17, 6),
         ("Протеиновый батончик", 50, 200, 15, 7, 20), ("Кофе с молоком и печенье", 200, 210, 5, 8, 28),
         ("Питьевой йогурт", 250, 150, 7, 3, 24), ("Хлебцы с хумусом", 80, 210, 7, 8, 28), ("Шоколад, 30 г", 30, 165, 2, 10, 17),
         ("Кефир", 250, 105, 7, 5, 10), ("Мандарины", 220, 90, 1.5, 0.5, 22)]

# продукты чека: (название, цена от, цена до, подгруппа, штучный ли товар)
GOODS = [("Молоко 3,2%", 89, 115, "молочное", 1), ("Кефир 1%", 75, 98, "молочное", 1), ("Яйца С1, 10 шт.", 105, 148, "молочное", 1),
         ("Сыр твёрдый", 290, 460, "молочное", 0), ("Творог 5%", 95, 150, "молочное", 1), ("Сметана 20%", 85, 120, "молочное", 1),
         ("Масло сливочное", 175, 260, "молочное", 1), ("Йогурт натуральный", 48, 70, "молочное", 1),
         ("Филе куриное", 310, 470, "мясо и рыба", 0), ("Фарш говяжий", 380, 560, "мясо и рыба", 0), ("Свинина шея", 460, 640, "мясо и рыба", 0),
         ("Сосиски молочные", 240, 330, "мясо и рыба", 1), ("Сёмга охлаждённая", 690, 980, "мясо и рыба", 0),
         ("Картофель", 45, 85, "овощи и фрукты", 0), ("Помидоры", 130, 260, "овощи и фрукты", 0), ("Огурцы", 110, 210, "овощи и фрукты", 0),
         ("Бананы", 80, 120, "овощи и фрукты", 0), ("Яблоки", 100, 180, "овощи и фрукты", 0), ("Морковь", 40, 70, "овощи и фрукты", 0),
         ("Лук репчатый", 30, 55, "овощи и фрукты", 0), ("Мандарины", 120, 210, "овощи и фрукты", 0), ("Зелень, пучок", 59, 99, "овощи и фрукты", 1),
         ("Хлеб бородинский", 55, 90, "хлеб", 1), ("Батон нарезной", 48, 75, "хлеб", 1), ("Лаваш", 45, 70, "хлеб", 1),
         ("Макароны", 70, 130, "бакалея", 1), ("Рис круглозёрный", 90, 150, "бакалея", 1), ("Гречка", 110, 170, "бакалея", 1),
         ("Масло подсолнечное", 130, 200, "бакалея", 1), ("Сахар", 60, 90, "бакалея", 1), ("Кофе в зёрнах", 450, 790, "бакалея", 1),
         ("Чай чёрный", 150, 280, "бакалея", 1), ("Хлопья овсяные", 70, 130, "бакалея", 1),
         ("Сок яблочный", 95, 150, "напитки", 1), ("Вода минеральная", 38, 65, "напитки", 1), ("Лимонад детский", 70, 110, "напитки", 1),
         ("Пельмени", 290, 420, "замороженное", 1), ("Овощная смесь замороженная", 120, 210, "замороженное", 1),
         ("Мороженое", 90, 190, "сладости", 1), ("Печенье овсяное", 80, 140, "сладости", 1), ("Шоколад молочный", 90, 160, "сладости", 1),
         ("Зефир", 130, 210, "сладости", 1)]
HOUSEHOLD = [("Порошок стиральный", 390, 780), ("Средство для посуды", 110, 220), ("Туалетная бумага, 12 рул.", 330, 520),
             ("Губки для посуды", 60, 120), ("Зубная паста", 130, 260), ("Шампунь", 220, 420), ("Пакеты для мусора", 80, 160),
             ("Салфетки бумажные", 60, 110), ("Гель для душа", 150, 280)]
GROCERS = [("Пятёрочка", 5), ("Магнит", 4), ("ВкусВилл", 3), ("Перекрёсток", 3), ("Лента", 2), ("Окей", 1)]
CAFES = ["Кофейня у дома", "Додо Пицца", "Теремок", "Шоколадница", "Вкусно — и точка", "Бургер Кинг", "Суши Wok", "Кофе с собой", "Ресторан «Хмели-Сунели»"]
FUEL = ["Лукойл", "Газпромнефть", "Роснефть", "Shell"]


def pick(rng: random.Random, pairs):
    names, weights = zip(*pairs)
    return rng.choices(names, weights)[0]


def money(rng, a, b, step=10):
    return round(rng.uniform(a, b) / step) * step


def ean13(rng) -> str:
    d = [2] + [rng.randint(0, 9) for _ in range(11)]
    s = sum(x * (3 if i % 2 else 1) for i, x in enumerate(d))
    return "".join(map(str, d)) + str((10 - s % 10) % 10)


def month_add(d: date, n: int) -> date:
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def clamp_day(y: int, m: int, day: int) -> date:
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return min(date(y, m, day), nxt - timedelta(days=1))


def next_day_of_month(day: int, after: date) -> date:
    """Ближайшая дата с этим числом месяца, не раньше after."""
    d = clamp_day(after.year, after.month, day)
    return d if d >= after else clamp_day(*month_add(after, 1).timetuple()[:2], day)


class Gen:
    def __init__(self, d0: date, days: int):
        self.s = get_settings()
        self.tz = self.s.zone
        self.d0 = d0                                   # сегодня; история заканчивается вчера
        self.start = d0 - timedelta(days=days)
        self.rng = random.Random(SEED)
        self.cat: dict[str, int] = {}
        self.st: list[str] = []
        self.tx: list[list] = []                        # строки transactions
        self.tx_id = 0
        self.items: list[list] = []
        self.item_id = 0

    # --- время ---
    def at(self, d: date, h: int, m: int = 0) -> datetime:
        return datetime.combine(d, time(h, m), tzinfo=self.tz)

    def rt(self, d: date, h1: int, h2: int) -> datetime:
        m = self.rng.randint(h1 * 60, h2 * 60 - 1)
        return self.at(d, m // 60, m % 60)

    def days(self, a: date | None = None, b: date | None = None):
        d = a or self.start
        end = b or (self.d0 - timedelta(days=1))
        while d <= end:
            yield d
            d += timedelta(days=1)

    # --- траты ---
    def add_tx(self, user: int, kind: str, amount, cat: str, merchant, note, d: date, source="text", group=None,
               vehicle=None, trip=None, created=None, deleted=None) -> int:
        self.tx_id += 1
        self.tx.append([self.tx_id, 1, user, kind, amount, "RUB", self.cat[cat], merchant, note, d, source, group,
                        created or self.rt(d, 9, 22), deleted, vehicle, trip])
        return self.tx_id

    def add_items(self, tx_id: int, goods: list[tuple]):
        for name, qty, amount, sub in goods:
            self.item_id += 1
            self.items.append([self.item_id, tx_id, name, qty, amount, sub])


def receipt_goods(rng, target: float) -> list[tuple]:
    """Позиции продуктового чека, сумма которых равна target."""
    goods, total = [], 0.0
    pool = GOODS[:]
    rng.shuffle(pool)
    for name, lo, hi, sub, piece in pool:
        if total >= target * 0.9 and len(goods) >= 4:
            break
        qty = rng.choice([1, 1, 2]) if piece else round(rng.uniform(0.3, 1.8), 3)
        price = round(rng.uniform(lo, hi), 2)
        amount = round(price * qty, 2)
        if total + amount > target - 20 and goods:
            continue
        goods.append((name, qty, amount, sub))
        total += amount
    if not goods:
        goods = [("Продукты", 1, target, None)]
        total = target
    diff = round(target - total, 2)
    n, q_, a, s = goods[-1]
    goods[-1] = (n, q_, round(a + diff, 2), s)
    return goods


def build(d0: date, days: int) -> list[str]:
    """SQL-сценарий заготовки (без вызова базы). Категории нужны заранее: g.cat заполняет вызывающий."""
    raise NotImplementedError


# ---------- сборка ----------

def build_sql(cats: dict[str, int], d0: date | None = None, days: int | None = None) -> list[str]:
    s = get_settings()
    d0 = d0 or today()
    g = Gen(d0, days or s.demo_history_days)
    g.cat = cats
    rng, tz = g.rng, g.tz
    out: list[str] = []
    start, yday = g.start, d0 - timedelta(days=1)
    ts = lambda d, h=12, m=0: g.at(d, h, m)

    # --- семья и люди ---
    out.append("UPDATE families SET name=" + q(FAMILY) + " WHERE id=1;" if False else "")
    out += ins("families", ["id", "name", "created_at", "sections"], [[1, FAMILY, ts(start - timedelta(days=40)), {}]])
    out += ins("users", ["id", "family_id", "name", "kcal_goal", "created_at", "settings"],
               [[uid, 1, name, kcal, ts(start - timedelta(days=40)), {"city": CITY, "dietitian": True}] for uid, name, kcal, _ in USERS])
    out += ins("user_identities", ["channel", "external_id", "user_id"], [["telegram", ext, uid] for uid, _n, _k, ext in USERS])
    out.append(fix_seq("families") + fix_seq("users"))

    # --- техника и поездки (нужны тратам) ---
    car_km0, moto_km0 = 79150, 15150
    vehicles = [[1, 1, 1, "Škoda Octavia", "car", None, None, None, ts(start - timedelta(days=30))],
                [2, 1, 1, "Honda CB500X", "moto", None, None, None, ts(start - timedelta(days=30))]]
    trips = [
        [1, 1, 2, "Казань на майские", "train", date(d0.year, 5, 1), date(d0.year, 5, 5), 45000],
        [2, 1, 1, "Сочи, летний отпуск", "plane", date(d0.year, 7, 12), date(d0.year, 7, 21), 150000],
        [3, 1, 1, "Мотопоход на Валдай", "moto", date(d0.year, 8, 15), date(d0.year, 8, 18), 30000],
        [4, 1, 1, "Новый год в Карелии", "car", date(d0.year, 12, 29), date(d0.year + 1, 1, 4), 80000],
    ]
    # даты поездок считаем от сегодняшнего года, но прошлые поездки должны быть в истории
    for t in trips[:3]:
        while t[5] >= d0:
            t[5], t[6] = t[5].replace(year=t[5].year - 1), t[6].replace(year=t[6].year - 1)
    if trips[3][5] < d0:
        trips[3][5], trips[3][6] = date(d0.year + 1, 12, 29), date(d0.year + 2, 1, 4)
    trips[3][5] = max(trips[3][5], d0 + timedelta(days=21))
    trips[3][6] = trips[3][5] + timedelta(days=6)

    def in_trip(d: date, tid: int) -> bool:
        t = trips[tid - 1]
        return t[5] <= d <= t[6]

    family_away = lambda d: in_trip(d, 1) or in_trip(d, 2)       # на этих поездках уезжает вся семья
    out += ins("vehicles", ["id", "family_id", "user_id", "name", "kind", "mileage", "mileage_at", "asked_at", "created_at"], vehicles)
    out += ins("trips", ["id", "family_id", "user_id", "title", "kind", "start_on", "end_on", "budget", "reminded", "created_at"],
               [t + [Arr(["before3", "before1"] if t[6] < d0 else [], "text"), ts(min(t[5], d0) - timedelta(days=25))] for t in trips])
    out.append(fix_seq("vehicles") + fix_seq("trips"))

    # --- доходы ---
    for d in g.days():
        if d.day == 5:
            g.add_tx(1, "income", 78000 + rng.choice([0, 0, 2500, -1800]), "Зарплата", "Работа", "Зарплата Алексея", d, created=g.rt(d, 10, 14))
        if d.day == 20:
            g.add_tx(1, "income", 55000, "Зарплата", "Работа", "Аванс Алексея", d, created=g.rt(d, 10, 14))
        if d.day in (10, 25):
            g.add_tx(2, "income", 48000 + (3000 if d.day == 25 and d.month % 3 == 0 else 0), "Зарплата", "Работа", "Зарплата Марии", d, created=g.rt(d, 10, 14))
    for off, amount, note in [(12, 18000, "Подработка: монтаж системы"), (68, 22000, "Подработка: консультация"), (128, 15000, "Подработка: проект")]:
        d = start + timedelta(days=off)
        if d < d0:
            g.add_tx(1, "income", amount, "Подработка", "Заказчик", note, d)
    g.add_tx(1, "income", 52000, "Возврат", "ФНС", "Налоговый вычет за ипотеку", start + timedelta(days=77))
    g.add_tx(2, "income", 1290, "Возврат", "Wildberries", "Возврат за товар", start + timedelta(days=95))
    g.add_tx(2, "income", 5000, "Подарок", None, "Подарок от родителей ко дню рождения", start + timedelta(days=24))

    # --- регулярные платежи (в таблице bills + платежи) ---
    bills_def = [  # id, title, amount, cat, due_day, period, user, remind
        (1, "Ипотека", 38500, "Кредиты", 5, 1, 1, 3), (2, "ЖКХ", None, "ЖКХ", 12, 1, 2, 3),
        (3, "Рассрочка на холодильник", 4800, "Кредиты", 15, 1, 2, 3), (4, "Яндекс Плюс", 399, "Развлечения", 14, 1, 2, 1),
        (5, "Интернет и ТВ", 850, "Связь и интернет", 3, 1, 1, 3), (6, "Мобильная связь, Алексей", 650, "Связь и интернет", 4, 1, 1, 2),
        (7, "Плавание, абонемент", 4200, "Дети", 1, 1, 2, 3), (8, "Школьное питание", 1800, "Дети", 25, 1, 2, 3),
        (9, "ОСАГО на Škoda", 9800, "Авто", 2, 12, 1, 14), (10, "КАСКО на Škoda", 42000, "Авто", 15, 12, 1, 14),
        (11, "Налог на имущество", 6200, "Прочее", 1, 12, 1, 14), (12, "Английский, Соня", 5600, "Дети", 3, 1, 2, 3),
    ]
    jkh = lambda d: {11: 8300, 12: 8700, 1: 9100, 2: 8900, 3: 8200, 4: 7400, 10: 7300}.get(d.month, 6100) + rng.randint(-250, 250)
    bill_pay: list[list] = []
    bp_id = 0
    skipped_plus = None
    for bid, title, amount, cat, dd, period, uid, _rd in bills_def:
        if period == 1:
            due_dates = [clamp_day(d.year, d.month, dd) for d in (month_add(start, i) for i in range(0, days_between(start, yday) // 28 + 2))]
        elif bid == 9:
            due_dates = [date(start.year, 6, 2)]
        elif bid == 10:
            due_dates = []                                           # КАСКО оплачено весной, до начала истории
        else:
            due_dates = []
        seen = set()
        for due in due_dates:
            if due in seen or due < start or due > yday:
                continue
            seen.add(due)
            bp_id += 1
            if bid == 4 and due.month == 6 and skipped_plus is None:      # один пропуск «без траты»
                skipped_plus = due
                bill_pay.append([bp_id, bid, due, None, True, uid, ts(due, 11)])
                continue
            amt = jkh(due) if bid == 2 else amount
            pay_day = due + timedelta(days=rng.choice([0, 0, 0, 1, 2]) if bid in (2, 8) else 0)
            pay_day = min(pay_day, yday)
            tid = g.add_tx(uid, "expense", amt, cat, title.split(",")[0], f"Оплата: {title}", pay_day, source="text",
                           vehicle=1 if bid in (9, 10) else None, created=g.rt(pay_day, 9, 20))
            bill_pay.append([bp_id, bid, due, tid, False, uid, ts(pay_day, 12)])
    bills_rows = []
    for bid, title, amount, cat, dd, period, uid, rd in bills_def:
        if bid == 10:
            nd = date(d0.year + (1 if d0 >= date(d0.year, 3, 15) else 0), 3, 15)
        elif bid == 9:
            nd = date(d0.year + (1 if d0 >= date(d0.year, 6, 2) else 0), 6, 2)
        elif bid == 11:
            nd = next_day_of_month(1, d0) if d0.month == 12 or d0.month < 12 else d0
            nd = date(d0.year, 12, 1) if d0 <= date(d0.year, 12, 1) else date(d0.year + 1, 12, 1)
        else:
            nd = next_day_of_month(dd, d0)
        bills_rows.append([bid, 1, uid, title, amount if amount else 8300, g.cat[cat], dd, period, nd, rd, ts(start - timedelta(days=20))])
    out += ins("bills", ["id", "family_id", "user_id", "title", "amount", "category_id", "due_day", "period_months", "next_due", "remind_days", "created_at"], bills_rows)

    # --- повседневные траты ---
    for d in g.days():
        away = family_away(d)
        wd = d.weekday()
        # продукты
        if not away:
            chance = 0.9 if wd < 5 else 0.97
            for _shop_i in range(1 + (rng.random() < 0.45)):
              if rng.random() < (chance if _shop_i == 0 else 0.7):
                shop = pick(rng, GROCERS)
                kind_r = rng.random()
                big = kind_r < 0.4
                if kind_r < 0.12:
                    amount = money(rng, 2600, 7200, 10) + rng.choice([0, 3, 7, 40, 90])
                elif big:
                    amount = money(rng, 1300, 2500, 10) + rng.choice([0, 3, 7, 40, 90])
                else:
                    amount = money(rng, 280, 1250, 1)
                user = rng.choice([1, 2]) if wd >= 5 else rng.choices([1, 2], [2, 3])[0]
                if big and rng.random() < 0.72:
                    grp = str(uuid.uuid5(NS, f"rc{d}{shop}{amount}"))
                    goods = receipt_goods(rng, float(amount))
                    house = None
                    if rng.random() < 0.35:
                        name, lo, hi = rng.choice(HOUSEHOLD)
                        hm = round(rng.uniform(lo, hi), 2)
                        house = (name, 1, hm, "хозтовары")
                        amount_food = round(float(amount) - hm, 2)
                        goods = receipt_goods(rng, amount_food)
                    created = g.rt(d, 16, 21)
                    tid = g.add_tx(user, "expense", sum(x[2] for x in goods), "Продукты", shop, "Чек по позициям", d, "receipt", grp, created=created)
                    g.add_items(tid, goods)
                    if house:
                        tid2 = g.add_tx(user, "expense", house[2], "Хозтовары", shop, "Чек по позициям", d, "receipt", grp, created=created)
                        g.add_items(tid2, [house])
                else:
                    g.add_tx(user, "expense", amount, "Продукты", shop, rng.choice([None, None, "Хлеб, молоко, овощи", "Ужин", "Докупили к выходным"]), d,
                             source=rng.choices(["text", "voice"], [4, 1])[0], created=g.rt(d, 8, 21))
        # топливо
        if not away:
            if rng.random() < 0.15:
                g.add_tx(1, "expense", money(rng, 2600, 3700, 10), "Топливо", rng.choice(FUEL), "Заправка, Octavia", d, vehicle=1, created=g.rt(d, 7, 19))
            if 5 <= d.month <= 10 and wd >= 4 and rng.random() < 0.24 and not in_trip(d, 3):
                g.add_tx(1, "expense", money(rng, 520, 980, 10), "Топливо", rng.choice(FUEL), "Заправка, мотоцикл", d, vehicle=2, created=g.rt(d, 9, 18))
        # кафе и развлечения
        if not away and rng.random() < (0.45 if wd < 5 else 0.75):
            g.add_tx(rng.choice([1, 2]), "expense", money(rng, 350, 1900, 10), "Кафе и рестораны", rng.choice(CAFES), None, d, created=g.rt(d, 12, 22))
        if not away and rng.random() < 0.12:
            g.add_tx(rng.choice([1, 2]), "expense", money(rng, 700, 2400, 10), "Развлечения", rng.choice(["Кинотеатр", "Боулинг", "Квест", "Батутный парк", "Музей"]), None, d)
        # транспорт
        if rng.random() < 0.14:
            g.add_tx(2, "expense", money(rng, 250, 700, 10), "Транспорт", "Яндекс Go", "Такси", d, created=g.rt(d, 8, 23))
        if d.day == 8:
            g.add_tx(2, "expense", 1500, "Транспорт", "Тройка", "Пополнение карты", d)
        # здоровье, одежда, дети, хозтовары, разное
        if rng.random() < 0.12:
            g.add_tx(rng.choice([1, 2]), "expense", money(rng, 250, 1500, 10), "Здоровье", rng.choice(["Аптека Ригла", "Аптека 36,6", "Аптека Озерки"]), None, d)
        if rng.random() < 0.08:
            g.add_tx(rng.choice([1, 2]), "expense", money(rng, 1500, 5200, 100), "Одежда", rng.choice(["Спортмастер", "Zara", "Uniqlo", "Wildberries", "Ostin"]), None, d)
        if rng.random() < 0.22:
            g.add_tx(2, "expense", money(rng, 500, 2400, 10), "Дети", rng.choice(["Детский мир", "Канцтовары", "Книжный магазин", "Игрушки"]), None, d)
        if not away and rng.random() < 0.1:
            g.add_tx(rng.choice([1, 2]), "expense", money(rng, 900, 3400, 10), "Хозтовары", rng.choice(["Fix Price", "Леруа Мерлен", "Хоум Кредит"][:2]), None, d)
        if rng.random() < 0.12:
            g.add_tx(rng.choice([1, 2]), "expense", money(rng, 500, 2400, 10), "Прочее", rng.choice(["Почта России", "Химчистка", "Ремонт обуви", "Ключи"]), None, d)
    # разовые и сезонные
    one_off = [(0.5, 1, "Одежда", "Мотоэкипировка", "Шлем и перчатки для мотоцикла", 18500, 2),
               (30, 1, "Здоровье", "Стоматология", "Лечение зуба", 7500, None), (62, 2, "Здоровье", "Лаборатория Инвитро", "Анализы", 3200, None),
               (40, 2, "Образование", "Онлайн-школа", "Курс английского для Марии", 3500, None), (70, 2, "Образование", "Онлайн-школа", "Курс английского для Марии", 3500, None),
               (101, 2, "Образование", "Онлайн-школа", "Курс английского для Марии", 3500, None),
               (112, 1, "Развлечения", "Аквапарк", "Аквапарк с детьми", 4800, None), (118, 2, "Дети", "Детский мир", "Школьная форма и канцелярия", 9500, None),
               (36, 1, "Подарки", "Магазин", "Подарок маме", 6500, None), (140, 2, "Подарки", "Магазин", "Подарок подруге", 3200, None),
               (160, 1, "Прочее", "Леруа Мерлен", "Ремонт в ванной, материалы", 8400, None), (150, 1, "Авто", "Шиномонтаж", "Переобувание на зимнюю резину", 2800, 1),
               (120, 1, "Авто", "Автомойка", "Мойка и химчистка салона", 3600, 1)]
    car_tx = {}
    for off, user, cat, merchant, note, amount, veh in one_off:
        d = start + timedelta(days=int(off))
        if start <= d <= yday:
            tid = g.add_tx(user, "expense", amount, cat, merchant, note, d, vehicle=veh)
            car_tx[note] = (tid, d)
    for d in g.days():          # пару раз месяц — мойка и аксессуары машины
        if d.day in (9, 24) and rng.random() < 0.7 and not family_away(d):
            g.add_tx(1, "expense", money(rng, 450, 900, 10), "Авто", "Автомойка", "Мойка", d, vehicle=1)

    # --- поездки: траты внутри дат ---
    def trip_spend(tid, spends):
        t = trips[tid - 1]
        for dd, user, cat, merchant, note, amount in spends:
            d = t[5] + timedelta(days=dd)
            if start <= d <= yday and d <= t[6]:
                g.add_tx(user, "expense", amount, cat, merchant, note, d, trip=tid, created=g.rt(d, 8, 22))
    trip_spend(1, [(0, 2, "Транспорт", "РЖД", "Билеты на поезд в Казань", 9600), (0, 2, "Прочее", "Гостиница Казань", "Отель, 4 ночи", 17200),
                   (1, 1, "Кафе и рестораны", "Чак-Чак", "Обед в центре", 3100), (1, 2, "Развлечения", "Кремль", "Экскурсия", 2400),
                   (2, 1, "Кафе и рестораны", "Ресторан татарской кухни", "Ужин", 4300), (3, 2, "Подарки", "Сувенирная лавка", "Сувениры и чак-чак", 2900),
                   (4, 1, "Транспорт", "РЖД", "Билеты обратно", 9600), (3, 2, "Развлечения", "Парк Горького", "Билеты и катание", 1800),
                   (2, 1, "Транспорт", "Яндекс Go", "Такси", 1100)])
    trip_spend(2, [(-1, 1, "Транспорт", "Аэрофлот", "Авиабилеты Москва — Сочи на четверых", 41800), (0, 1, "Прочее", "Отель в Адлере", "Проживание, 9 ночей", 62500),
                   (0, 1, "Транспорт", "Яндекс Go", "Такси из аэропорта", 1900), (1, 2, "Кафе и рестораны", "Кафе у моря", "Обед", 3600),
                   (2, 1, "Развлечения", "Сочи Парк", "Билеты на четверых", 7600), (3, 2, "Кафе и рестораны", "Ресторан «Море»", "Ужин", 6200),
                   (4, 1, "Развлечения", "Дельфинарий", "Билеты", 3400), (5, 2, "Продукты", "Магнит", "Продукты и фрукты", 2100),
                   (6, 1, "Развлечения", "Красная Поляна", "Канатка и обед", 9800), (7, 2, "Подарки", "Сувениры", "Магнитики и сладости", 2800),
                   (8, 1, "Кафе и рестораны", "Кафе", "Прощальный ужин", 5400), (8, 1, "Транспорт", "Яндекс Go", "Такси в аэропорт", 2100)])
    trip_spend(3, [(0, 1, "Топливо", "Лукойл", "Заправка по дороге", 900), (0, 1, "Прочее", "Гостевой дом, Валдай", "Три ночи", 9600),
                   (1, 1, "Кафе и рестораны", "Придорожное кафе", "Обед", 1200), (2, 1, "Развлечения", "Иверский монастырь", "Входные билеты", 800),
                   (2, 1, "Кафе и рестораны", "Кафе у озера", "Ужин", 1700), (3, 1, "Топливо", "Газпромнефть", "Заправка", 850)])

    out += ins("transactions", ["id", "family_id", "user_id", "kind", "amount", "currency", "category_id", "merchant", "note", "occurred_on",
                                "source", "group_id", "created_at", "deleted_at", "vehicle_id", "trip_id"], g.tx)
    out.append(fix_seq("transactions"))
    if g.items:
        out += ins("transaction_items", ["id", "transaction_id", "name", "qty", "amount", "subcategory"], g.items)
        out.append(fix_seq("transaction_items"))
    out += ins("bill_payments", ["id", "bill_id", "due_on", "transaction_id", "skipped", "user_id", "created_at"], bill_pay)
    out.append(fix_seq("bills") + fix_seq("bill_payments"))
    # две «удалённые» траты — чтобы в «Истории» была кнопка «Вернуть»
    g2_id = max(r[0] for r in g.tx) + 1
    deleted = [[g2_id, 1, 1, "expense", 450, "RUB", g.cat["Кафе и рестораны"], "Кофейня у дома", "Дубль, удалил", yday, "text", None, ts(yday, 12, 5), ts(yday, 12, 6), None, None],
               [g2_id + 1, 1, 2, "expense", 1190, "RUB", g.cat["Продукты"], "ВкусВилл", "Записала дважды", yday - timedelta(days=1), "text", None, ts(yday - timedelta(days=1), 19, 5), ts(yday - timedelta(days=1), 19, 20), None, None]]
    out += ins("transactions", ["id", "family_id", "user_id", "kind", "amount", "currency", "category_id", "merchant", "note", "occurred_on",
                                "source", "group_id", "created_at", "deleted_at", "vehicle_id", "trip_id"], deleted)
    out.append(fix_seq("transactions"))

    # --- лимиты ---
    limits = {"Продукты": 62000, "Кафе и рестораны": 12000, "Развлечения": 9000, "Одежда": 14000, "Транспорт": 5000, "Топливо": 14000,
              "Здоровье": 7000, "Дети": 22000, "Хозтовары": 5000, "Подарки": 8000}
    out += ins("budgets", ["family_id", "category_id", "monthly_limit", "updated_at"],
               [[1, g.cat[k], v, ts(start - timedelta(days=15))] for k, v in limits.items()])

    # --- питание, вода, шаги, сон, вес ---
    meals, metrics = [], []
    mid = mt = 0
    weight = {1: 86.4, 2: 62.6}
    for uid, _name, kcal, _ext in USERS:
        k = 1.0 if uid == 1 else 0.84
        for d in g.days():
            recent = (yday - d).days < 14
            if not recent and rng.random() < 0.10:
                continue                                                  # дни без записей — как в жизни
            plan = [("breakfast", BREAKFAST, 8), ("lunch", LUNCH, 13), ("dinner", DINNER, 19)]
            if rng.random() < 0.8 or recent:
                plan.append(("snack", SNACK, 16))
            if rng.random() < 0.35:
                plan.append(("snack", SNACK, 11 if rng.random() < 0.5 else 21))
            for mtype, cat, hour in plan:
                if not recent and rng.random() < 0.06 and mtype != "snack":
                    continue
                dish = rng.choice(cat)
                f = k * rng.uniform(0.82, 1.2)
                if d.weekday() >= 5 and mtype in ("lunch", "dinner") and rng.random() < 0.3:
                    f *= 1.25
                mid += 1
                when = g.at(d, hour, rng.randint(0, 50))
                meals.append([mid, uid, when, mtype, dish[0], round(dish[1] * f, 1), round(dish[2] * f, 1), round(dish[3] * f, 1),
                              round(dish[4] * f, 1), round(dish[5] * f, 1), rng.choices(["text", "photo", "voice"], [6, 3, 1])[0], when])
            # вода: 4–7 записей
            goal = 2200 if uid == 1 else 2000
            total = goal * rng.uniform(0.62, 1.12)
            n = rng.randint(4, 7)
            for j in range(n):
                mt += 1
                metrics.append([mt, uid, "water", int(total / n / 50) * 50 + 50, g.at(d, 8 + j * 2, rng.randint(0, 40)), "text", g.at(d, 8 + j * 2, 45)])
            # шаги: одна запись за вечер
            base = (9800 if uid == 1 else 7600) * (1.35 if d.weekday() >= 5 else 1)
            mt += 1
            metrics.append([mt, uid, "steps", int(base * rng.uniform(0.55, 1.25)), g.at(d, 21, rng.randint(0, 50)), "text", g.at(d, 21, 55)])
            mt += 1
            metrics.append([mt, uid, "sleep", round(rng.uniform(5.7, 8.6), 1), g.at(d, 7, rng.randint(10, 50)), "text", g.at(d, 7, 55)])
            if rng.random() < 0.34:
                weight[uid] += rng.uniform(-0.28, 0.14) if uid == 1 else rng.uniform(-0.14, 0.09)
                mt += 1
                metrics.append([mt, uid, "weight", round(weight[uid], 1), g.at(d, 7, rng.randint(15, 59)), "text", g.at(d, 7, 59)])
    deleted_meal = [mid + 1, 1, ts(yday, 15, 20), "snack", "Шоколад, 30 г", 30, 165, 2, 10, 17, "text", ts(yday, 15, 21)]
    meals.append(deleted_meal)
    mcols = ["id", "user_id", "eaten_at", "meal_type", "title", "grams", "kcal", "protein", "fat", "carbs", "source", "created_at"]
    out += ins("meals", mcols, meals)
    out.append(f"UPDATE meals SET deleted_at={q(ts(yday, 15, 40))} WHERE id={mid + 1};")
    out += ins("metrics", ["id", "user_id", "kind", "value", "measured_at", "source", "created_at"], metrics)
    out.append(fix_seq("meals") + fix_seq("metrics"))
    out += ins("metric_goals", ["user_id", "kind", "target"],
               [[1, "water", 2200], [1, "steps", 9000], [1, "sleep", 7.5], [1, "weight", 82], [2, "water", 2000], [2, "steps", 8000], [2, "sleep", 8], [2, "weight", 60]])

    # --- дела ---
    def due(off, h=12, m=0):
        return ts(d0 + timedelta(days=off), h, m)
    open_tasks = [  # название, заметки, автор, исполнитель, срок, весь день
        ("Забронировать домик в Карелии: подтвердить", "Позвонить в базу отдыха, уточнить заезд 29 декабря", 1, 1, due(3, 11), False),
        ("Родительское собрание в школе", "Класс Сони, актовый зал", 2, 2, due(1, 18, 30), False),
        ("Позвонить маме", None, 2, 2, due(0, 19), False),
        ("Записаться к стоматологу (Соня)", None, 2, 2, due(5, 12), True),
        ("Сдать показания счётчиков", "Окно подачи — с 20 по 25 число", 2, 2, due(12, 10), True),
        ("Заменить салонный фильтр на Octavia", "По регламенту — в ноябре", 1, 1, due(10, 12), True),
        ("Продлить ДМС", "Полис заканчивается в декабре", 1, 1, due(30, 12), True),
        ("Купить подарки детям на Новый год", "Соня — набор для рисования, Миша — конструктор", 2, 2, due(35, 12), True),
        ("Оплатить налог на имущество", None, 1, 1, due(22, 12), True),
        ("Проверить АКБ мотоцикла перед зимой", "Снять и поставить на зарядку", 1, 1, due(-4, 12), True),
        ("Заказать новогоднюю гирлянду", None, 2, 1, due(20, 12), True),
        ("Поменять батарейки в датчиках дыма", None, 1, 1, due(7, 12), True),
    ]
    done_titles = ["Купить лампочки", "Записать Мишу на плавание", "Оплатить интернет", "Отвезти машину на мойку", "Забрать посылку", "Купить билеты в Казань",
                   "Записаться на ТО", "Сдать анализы", "Купить школьную форму", "Заказать торт на день рождения", "Отдать вещи в химчистку",
                   "Забронировать отель в Сочи", "Купить крем от загара", "Починить велосипед", "Оформить загранпаспорт Соне", "Выбрать подарок маме",
                   "Купить зимнюю резину", "Забрать документы из МФЦ", "Сделать копии ключей", "Позвонить в управляющую компанию", "Купить корм для рыбок",
                   "Заказать фотокнигу из поездки", "Купить лыжи детям", "Продлить ОСАГО", "Подготовить мотоцикл к сезону"]
    tasks = []
    tid = 0
    for title, notes, uid, ass, dueat, allday in open_tasks:
        tid += 1
        tasks.append([tid, 1, uid, title, notes, dueat, allday, None, None, "open", dueat - timedelta(days=rng.randint(2, 14)), None, ass, "bot", None])
    for i, title in enumerate(done_titles):
        tid += 1
        created = g.rt(start + timedelta(days=i * 6 + rng.randint(0, 4)), 9, 20)
        done_at = created + timedelta(days=rng.randint(0, 6), hours=rng.randint(1, 20))
        uid = 1 if i % 2 == 0 else 2
        if done_at.date() >= d0:
            done_at = g.rt(yday, 9, 20)
        tasks.append([tid, 1, uid, title, None, done_at, True, None, None, "done", created, done_at, uid if i % 5 else (2 if uid == 1 else 1), "bot", done_at])
    out += ins("tasks", ["id", "family_id", "user_id", "title", "notes", "due_at", "all_day", "remind_at", "reminded_at", "status", "created_at",
                         "done_at", "assignee_id", "source", "updated_at"], tasks)
    out.append(fix_seq("tasks"))

    # --- список покупок ---
    shop_open = [("Молоко", "2 л", "молочное"), ("Яйца", "10 шт.", "молочное"), ("Сыр", "300 г", "молочное"), ("Хлеб бородинский", None, "хлеб"),
                 ("Куриное филе", "1 кг", "мясо и рыба"), ("Помидоры", "1 кг", "овощи и фрукты"), ("Огурцы", "0,5 кг", "овощи и фрукты"),
                 ("Бананы", None, "овощи и фрукты"), ("Гречка", "1 кг", "бакалея"), ("Кофе в зёрнах", None, "бакалея"), ("Сок яблочный", "2 шт.", "напитки"),
                 ("Пельмени", None, "замороженное"), ("Порошок для стирки", None, "хозтовары"), ("Зубная паста", None, "хозтовары"),
                 ("Батарейки AA", "4 шт.", None), ("Нурофен", None, "аптека")]
    shop = []
    sid = 0
    for i, (title, qty, dept) in enumerate(shop_open):
        sid += 1
        shop.append([sid, 1, title, qty, 1 if i % 3 else 2, g.rt(d0 - timedelta(days=rng.randint(0, 4)), 8, 21), None, None, dept])
    for i in range(34):
        sid += 1
        name, _lo, _hi, sub, _p = rng.choice(GOODS)
        d = start + timedelta(days=rng.randint(0, days_between(start, yday)))
        c = g.rt(d, 8, 20)
        shop.append([sid, 1, name, None, rng.choice([1, 2]), c, c + timedelta(hours=rng.randint(2, 40)), None, sub])
    out += ins("shopping_items", ["id", "family_id", "title", "qty", "added_by", "created_at", "bought_at", "deleted_at", "dept"], shop)
    out.append(fix_seq("shopping_items"))

    # --- накопления ---
    goals = [(1, 1, "Отпуск 2027: Турция", 250000, date(d0.year + 1, 6, 30), start - timedelta(days=60), None),
             (2, None, "Подушка безопасности", 500000, None, start - timedelta(days=200), None),
             (3, 1, "Экипировка и ТО для мотоцикла", 60000, date(d0.year + 1, 4, 15), start - timedelta(days=45), None),
             (4, 2, "Ноутбук Марии", 85000, None, start - timedelta(days=50), start + timedelta(days=104))]
    out += ins("savings_goals", ["id", "family_id", "user_id", "title", "target", "deadline", "created_at", "closed_at"],
               [[i, 1, u, t, tg, dl, ts(c), ts(cl) if cl else None] for i, u, t, tg, dl, c, cl in goals])
    moves, mv = [], 0
    for d in g.days():
        if d.day == 8:
            for gid, amount, who, note in [(1, 10000, 1, "Ежемесячный взнос"), (2, 12000, 2, "Откладываем с зарплаты"), (3, 2000, 1, "Взнос в месяц")]:
                mv += 1
                moves.append([mv, gid, who, amount, note, ts(d, 20)])
        if d.day == 21 and rng.random() < 0.6:
            mv += 1
            moves.append([mv, 1, 2, rng.choice([5000, 8000, 12000]), "Премия: часть в отпуск", ts(d, 19)])
    mv += 1
    moves.append([mv, 2, 1, 230000, "Накоплено ранее", ts(start, 12)])
    for i, amount in enumerate([30000, 30000, 25000]):
        mv += 1
        moves.append([mv, 4, 2, amount, "На ноутбук", ts(start + timedelta(days=i * 25 + 5), 18)])
    out += ins("savings_moves", ["id", "goal_id", "user_id", "amount", "note", "created_at"], moves)
    out.append(fix_seq("savings_goals") + fix_seq("savings_moves"))

    # --- гараж ---
    odo, oid = [], 0
    car_km, moto_km = car_km0, moto_km0
    for d in g.days():
        if d.day in (1, 11, 21) or d == yday:
            car_km += rng.randint(260, 360) if d != yday else rng.randint(50, 120)
            oid += 1
            odo.append([oid, 1, car_km, 1, ts(d, 20)])
        if 5 <= d.month <= 10 and (d.day in (6, 20) or (d.month == 10 and d.day == 12)):
            moto_km += rng.randint(240, 420)
            oid += 1
            odo.append([oid, 2, moto_km, 1, ts(d, 21)])
    out += ins("odometer", ["id", "vehicle_id", "km", "user_id", "created_at"], odo)
    out.append(f"UPDATE vehicles SET mileage={car_km}, mileage_at={q(ts(yday, 20))}, asked_at={q(ts(d0 - timedelta(days=9), 10))} WHERE id=1;"
               f"UPDATE vehicles SET mileage={moto_km}, mileage_at={q(ts(d0 - timedelta(days=27), 21))}, asked_at={q(ts(d0 - timedelta(days=27), 10))} WHERE id=2;"
               + fix_seq("odometer"))
    rules = [  # id, машина, название, каждые км, мес., последний км, последняя дата
        (1, 1, "Замена масла двигателя", 10000, 12, car_km - 3100, d0 - timedelta(days=51)), (2, 1, "Воздушный фильтр", 20000, 24, car_km - 14500, d0 - timedelta(days=430)),
        (3, 1, "Салонный фильтр", 15000, 12, car_km - 12900, d0 - timedelta(days=352)), (4, 1, "Тормозная жидкость", None, 24, None, d0 - timedelta(days=640)),
        (5, 1, "Свечи зажигания", 30000, None, car_km - 22000, d0 - timedelta(days=700)),
        (6, 2, "Замена масла двигателя", 6000, 12, moto_km - 5400, d0 - timedelta(days=210)), (7, 2, "Чистка и смазка цепи", 800, None, moto_km - 700, d0 - timedelta(days=30)),
        (8, 2, "Тормозная жидкость", None, 24, None, d0 - timedelta(days=560)), (9, 2, "Свечи зажигания", 12000, None, moto_km - 11600, d0 - timedelta(days=500)),
        (10, 2, "Воздушный фильтр", 12000, 24, moto_km - 11600, d0 - timedelta(days=500)),
    ]
    out += ins("service_rules", ["id", "vehicle_id", "title", "every_km", "every_months", "last_km", "last_date", "created_at"],
               [list(r) + [ts(start - timedelta(days=30))] for r in rules])
    oil_tx = next((t[0] for t in g.tx if t[8] and "Оплата: ОСАГО" in str(t[8])), None)
    slog = [  # id, машина, правило, название, км, дата, стоимость, транзакция
        (1, 1, 1, "Замена масла двигателя", car_km - 3100, d0 - timedelta(days=51), 6800, None),
        (2, 1, None, "Переобувание на зимнюю резину", car_km - 800, d0 - timedelta(days=24), 2800, car_tx.get("Переобувание на зимнюю резину", (None,))[0]),
        (3, 1, 3, "Салонный фильтр", car_km - 12900, d0 - timedelta(days=352), 1900, None),
        (4, 2, 7, "Чистка и смазка цепи", moto_km - 700, d0 - timedelta(days=30), 300, None),
        (5, 2, 7, "Чистка и смазка цепи", moto_km - 1500, d0 - timedelta(days=75), 300, None),
        (6, 2, None, "Замена передних колодок", moto_km - 3000, d0 - timedelta(days=128), 4300, None),
        (7, 2, 6, "Замена масла двигателя", moto_km - 5400, d0 - timedelta(days=210), 5200, None),
        (8, 1, None, "Мойка и химчистка салона", car_km - 1900, d0 - timedelta(days=60), 3600, car_tx.get("Мойка и химчистка салона", (None,))[0]),
    ]
    out += ins("service_log", ["id", "vehicle_id", "rule_id", "title", "km", "done_on", "cost", "transaction_id", "prev_km", "prev_date", "user_id", "created_at"],
               [[i, v, r, t, km, dn, c, tx, None, None, 1, ts(dn, 18)] for i, v, r, t, km, dn, c, tx in slog])
    out.append(fix_seq("service_rules") + fix_seq("service_log"))

    # --- документы, полисы, гарантии ---
    D = lambda days: d0 + timedelta(days=days)
    papers = [  # kind, название, чей, техника, срок, куплено, заметка
        ("document", "Загранпаспорт", "Алексей", None, date(d0.year + 2, 3, 14), None, None), ("document", "Загранпаспорт", "Мария", None, D(74), None, "Пора оформлять новый"),
        ("document", "Загранпаспорт", "Соня", None, date(d0.year + 5, 8, 2), None, None), ("document", "Водительское удостоверение", "Алексей", None, date(d0.year + 6, 6, 10), None, None),
        ("document", "Водительское удостоверение", "Мария", None, date(d0.year + 4, 2, 21), None, None),
        ("policy", "ОСАГО", None, 1, date(d0.year + (1 if d0 >= date(d0.year, 6, 2) else 0), 6, 2), None, "На Octavia"),
        ("policy", "КАСКО", None, 1, date(d0.year + (1 if d0 >= date(d0.year, 3, 15) else 0), 3, 15), None, "На Octavia"),
        ("policy", "ОСАГО", None, 2, date(d0.year + 1, 4, 10), None, "На мотоцикл"), ("policy", "ДМС", "Алексей", None, D(42), None, "Продлить до конца декабря"),
        ("policy", "Страхование ипотеки", None, None, D(84), None, None),
        ("warranty", "Стиральная машина", None, None, D(36), date(d0.year - 2, 12, 14), "Гарантия 2 года, магазин «М.Видео»"),
        ("warranty", "Холодильник", None, None, date(d0.year + 1, 3, 2), date(d0.year - 1, 3, 2), "Гарантия 2 года"),
        ("warranty", "Телевизор", None, None, D(17), date(d0.year - 3, d0.month, 20) if False else D(17).replace(year=D(17).year - 3), "Гарантия 3 года"),
        ("warranty", "Смартфон Марии", "Мария", None, D(-18), D(-18).replace(year=D(-18).year - 1), "Гарантия закончилась"),
        ("warranty", "Ноутбук Марии", "Мария", None, date(d0.year + 2, 8, 20) if d0 > date(d0.year, 8, 20) else date(d0.year + 1, 8, 20), None, "Куплен в августе"),
    ]
    out += ins("papers", ["id", "family_id", "user_id", "kind", "title", "owner", "vehicle_id", "expires_on", "purchased_on", "note", "created_at"],
               [[i + 1, 1, 1, k, t, o, v, e, p, n, ts(start - timedelta(days=rng.randint(0, 40)))] for i, (k, t, o, v, e, p, n) in enumerate(papers)])
    out.append(fix_seq("papers"))

    # --- счётчики ---
    meters = [(1, "Электроэнергия", "electricity", "кВт·ч", date(d0.year + 5, 3, 1), 18420.0, (320, 410)),
              (2, "Холодная вода", "cold_water", "м³", date(d0.year + 1, 8, 16), 412.3, (6.2, 8.4)),
              (3, "Горячая вода", "hot_water", "м³", D(27), 287.45, (4.1, 6.0))]
    out += ins("meters", ["id", "family_id", "user_id", "title", "kind", "unit", "verify_until", "submit_from", "submit_to", "created_at"],
               [[i, 1, 2, t, k, u, v, 20, 25, ts(start - timedelta(days=90))] for i, t, k, u, v, _s, _r in meters])
    readings, rid = [], 0
    for i, _t, _k, _u, _v, val, (lo, hi) in meters:
        m = month_add(start, -1)
        v = val
        while m <= month_add(d0, 0):
            sub = clamp_day(m.year, m.month, rng.randint(21, 24))
            if sub <= yday:
                v += rng.uniform(lo, hi) * (1.18 if i == 1 and m.month in (11, 12, 1, 2) else 1)
                rid += 1
                readings.append([rid, 1, i, round(v, 3), 2, ts(sub, rng.randint(9, 21))])
            m = month_add(m, 1)
    out += ins("meter_readings", ["id", "family_id", "meter_id", "value", "user_id", "created_at"], readings)
    out.append(fix_seq("meters") + fix_seq("meter_readings"))

    # --- заметки (в личных разделах — по автору) ---
    notes = [(1, "Размеры детей", "Соня: рост 134, одежда 134, обувь 33. Миша: рост 110, одежда 110, обувь 28. Проверять каждые полгода."),
             (2, "Идеи подарков", "Маме Алексея — плед и набор для чая. Папе Марии — рыболовный набор. Соне — набор для рисования, Мише — конструктор."),
             (2, "Рецепт: томатный соус", "Помидоры в собственном соку, чеснок, базилик, оливковое масло. Варить 25 минут, пробить блендером. Хватает на 4 порции пасты."),
             (1, "Маршрут по Карелии", "Петрозаводск, Кивач, Рускеала, Кижи, парк Паанаярви. Ночёвки в базах отдыха, ехать два дня, остановка в Вытегре."),
             (1, "Мотоцикл: чек-лист перед сезоном", "Давление в шинах, цепь, тормозная жидкость, АКБ, свет. Масло по регламенту."),
             (2, "Школа", "Родительское собрание в четверг в 18:30. Сдать деньги на экскурсию до пятницы. Форма на физкультуру — в понедельник."),
             (2, "Что взять на дачу", "Семена, перчатки, секатор, плёнка, пакеты для мусора, удобрение для газона."),
             (1, "Идеи на выходные", "Каток в Парке Горького, музей космонавтики, поездка в Коломну, кинотеатр с детьми."),
             (1, "Книги к прочтению", "«Атомные привычки», «Deep Work», «Сапиенс» — взять в библиотеке."),
             (2, "Меню на неделю", "Пн — паста, Вт — запечённая курица, Ср — рыба с рисом, Чт — суп-пюре, Пт — пицца дома, Сб — плов, Вс — борщ.")]
    out += ins("family_notes", ["id", "family_id", "user_id", "subject", "text", "created_at"],
               [[i + 1, 1, u, s, t, g.rt(start + timedelta(days=rng.randint(0, days_between(start, yday))), 9, 22)] for i, (u, s, t) in enumerate(notes)])
    out.append(fix_seq("family_notes"))

    # --- лекарства и приём ---
    meds = [(1, 1, None, "Витамин D3", "1 капсула", ["09:00"], None, d0 - timedelta(days=100), None, 38, 1, "капс", True),
            (2, 1, None, "Омега-3", "1 капсула", ["20:30"], None, d0 - timedelta(days=60), None, 21, 1, "капс", True),
            (3, 2, None, "Магний B6", "1 таблетка", ["22:00"], None, d0 - timedelta(days=90), None, 47, 1, "таб", True),
            (4, 2, "Миша", "Сироп от кашля (курс)", "5 мл", ["08:00", "20:00"], None, d0 - timedelta(days=4), d0 + timedelta(days=3), 60, 5, "мл", True),
            (5, 2, None, "Антигистаминное (сезонное)", "1 таблетка", ["21:00"], None, d0 - timedelta(days=170), d0 - timedelta(days=130), 0, 1, "таб", False)]
    out += ins("meds", ["id", "family_id", "user_id", "for_whom", "title", "dose", "times", "days", "start_on", "end_on", "stock", "per_intake", "unit", "active", "created_at"],
               [[i, 1, u, fw, t, ds, Arr([time.fromisoformat(x) for x in tm], "time"), Arr(dys, "int") if dys else None, so, eo, stk, per, un, act, ts(so)]
                for i, u, fw, t, ds, tm, dys, so, eo, stk, per, un, act in meds])
    intakes, iid = [], 0
    for i, u, fw, t, ds, tm, dys, so, eo, stk, per, un, act in meds:
        for d in g.days(max(so, d0 - timedelta(days=90)), min(eo, yday) if eo else yday):
            for tmv in tm:
                due_at = g.at(d, int(tmv[:2]), int(tmv[3:]))
                r = rng.random()
                status = "taken" if r < 0.89 else "skipped"
                iid += 1
                intakes.append([iid, i, due_at, status, 0, due_at + timedelta(minutes=rng.randint(1, 40)) if status == "taken" else None, u])
    out += ins("med_intakes", ["id", "med_id", "due_at", "status", "reminded", "taken_at", "user_id"], intakes)
    out.append(fix_seq("meds") + fix_seq("med_intakes"))

    # --- поездки: маршруты и сборы ---
    places = {1: [("Казань", 55.7887, 49.1221), ("Свияжск", 55.7717, 48.6639)], 2: [("Сочи", 43.5855, 39.7231), ("Красная Поляна", 43.6806, 40.2036)],
              3: [("Валдай", 57.9822, 33.2386), ("Иверский монастырь", 57.9899, 33.2397)],
              4: [("Петрозаводск", 61.7849, 34.3469), ("Кивач", 62.2833, 33.9667), ("Рускеала", 61.9456, 30.5703)]}
    out += ins("trip_places", ["id", "trip_id", "name", "lat", "lon", "sort"],
               [[i, t, n, la, lo, j] for i, (t, n, la, lo, j) in enumerate(((t, n, la, lo, j) for t, ps in places.items() for j, (n, la, lo) in enumerate(ps)), 1)])
    checklist = {1: (["Билеты на поезд", "Паспорта", "Зарядки и пауэрбанк", "Дождевики", "Наличные"], 5), 2: (["Авиабилеты", "Загранпаспорта не нужны, паспорта РФ", "Крем от солнца", "Купальники и полотенца", "Аптечка", "Зарядки", "Детские игрушки в самолёт"], 7),
                 3: (["Мотоэкипировка", "Дождевик", "Набор для цепи", "Карты офлайн", "Сумки на багажник"], 5),
                 4: (["Зимняя резина с шипами: проверить", "Термобельё на всех", "Подтвердить домик", "Страховка", "Термос и еда в дорогу", "Скачать офлайн-карты", "Игры и книги детям", "Санки и лыжи", "Аптечка"], 4)}
    cl, cid = [], 0
    for t, (items, ndone) in checklist.items():
        for j, title in enumerate(items):
            cid += 1
            cl.append([cid, t, title, j < ndone, j])
    out += ins("trip_checklist", ["id", "trip_id", "title", "done", "sort"], cl)
    out.append(fix_seq("trip_places") + fix_seq("trip_checklist"))

    # --- долги ---
    debts = [(1, 1, "lent", "Пётр", 15000, "На ремонт машины", d0 - timedelta(days=57), d0 + timedelta(days=22)),
             (2, 1, "lent", "Сергей (коллега)", 3500, "До зарплаты", d0 - timedelta(days=14), d0 + timedelta(days=2)),
             (3, 2, "borrowed", "Мама", 20000, "На первый взнос за курс", d0 - timedelta(days=95), date(d0.year, 12, 31)),
             (4, 1, "lent", "Дмитрий", 6000, "Одолжил на билеты", d0 - timedelta(days=140), d0 - timedelta(days=100))]
    out += ins("debts", ["id", "family_id", "user_id", "direction", "person", "amount", "note", "start_on", "due_on", "created_at"],
               [list(r[:1]) + [1] + list(r[1:]) + [ts(r[6])] for r in debts])
    dm = [(1, 1, "repay", 5000, d0 - timedelta(days=29), "Перевёл по СБП"), (2, 1, "add", 2000, d0 - timedelta(days=40), "Дал ещё на запчасти"),
          (3, 1, "repay", 3000, d0 - timedelta(days=8), None), (4, 3, "repay", 10000, d0 - timedelta(days=34), "Вернули часть"),
          (5, 4, "repay", 4000, d0 - timedelta(days=118), None), (6, 4, "repay", 2000, d0 - timedelta(days=96), "Закрыли")]
    out += ins("debt_moves", ["id", "debt_id", "user_id", "kind", "amount", "occurred_on", "note", "created_at"],
               [[i, d, 1 if d != 3 else 2, k, a, dt, n, ts(dt, 18)] for i, d, k, a, dt, n in dm])
    out.append(fix_seq("debts") + fix_seq("debt_moves"))

    # --- скидочные карты (номера придуманные) ---
    cards = [("Пятёрочка", ean13(rng), "ean13", 41), ("Магнит", ean13(rng), "ean13", 33), ("Перекрёсток", ean13(rng), "ean13", 18), ("ВкусВилл", "DEMO-VV-402913", "qr", 27),
             ("Лента", ean13(rng), "ean13", 12), ("Леруа Мерлен", ean13(rng), "ean13", 5), ("Аптека «Ригла»", "4600" + str(rng.randint(10**7, 10**8 - 1)), "code128", 9),
             ("Спортмастер", ean13(rng), "ean13", 4), ("Детский мир", ean13(rng), "ean13", 7)]
    out += ins("loyalty_cards", ["id", "family_id", "user_id", "store", "number", "format", "used_count", "used_at", "created_at"],
               [[i + 1, 1, 1 if i % 2 == 0 else 2, s, n, f, uc, ts(d0 - timedelta(days=rng.randint(1, 20)), 18), ts(start - timedelta(days=rng.randint(0, 40)))]
                for i, (s, n, f, uc) in enumerate(cards)])
    out.append(fix_seq("loyalty_cards"))

    # --- выписка Т-Банка за октябрь: часть совпала с записями, часть ждёт решения ---
    oct1, oct31 = date(d0.year, 10, 1), date(d0.year, 10, 31)
    if oct31 >= d0:
        oct1, oct31 = month_add(d0, -1), clamp_day(*month_add(d0, 0).timetuple()[:2], 1) - timedelta(days=1)
    out += ins("bank_imports", ["id", "family_id", "user_id", "filename", "bank", "date_from", "date_to", "created_at"],
               [[1, 1, 1, "tbank_oktyabr.csv", "Т-Банк", oct1, oct31, ts(oct31 + timedelta(days=2), 20)]])
    month_tx = [t for t in g.tx if t[3] == "expense" and oct1 <= t[9] <= oct31 and t[6] in (g.cat["Продукты"], g.cat["Топливо"], g.cat["Кафе и рестораны"])][:8]
    rows, rid = [], 0
    for t in month_tx:
        rid += 1
        rows.append([rid, 1, t[9], "expense", t[4], f"{t[7]} MOSKVA RUS", "Супермаркеты" if t[6] == g.cat["Продукты"] else "Рестораны", None, False, "matched", t[0]])
    rid += 1
    rows.append([rid, 1, oct1 + timedelta(days=8), "expense", 15000, "Перевод между своими счетами", "Переводы", None, True, "skipped", None])
    for desc, amount, bcat, cat in [("DNS SHOP MOSKVA", 4590, "Электроника", "Прочее"), ("OZON.RU", 2310, "Маркетплейсы", "Хозтовары"),
                                    ("ALIEXPRESS", 1280, "Маркетплейсы", "Прочее"), ("CINEMA PARK", 1450, "Развлечения", "Развлечения")]:
        rid += 1
        rows.append([rid, 1, oct1 + timedelta(days=rng.randint(10, 28)), "expense", amount, desc, bcat, cat, False, "new", None])
    out += ins("bank_rows", ["id", "import_id", "op_date", "kind", "amount", "description", "bank_category", "category", "is_transfer", "status", "transaction_id"], rows)
    out.append(fix_seq("bank_imports") + fix_seq("bank_rows"))
    return [x for x in out if x]


def days_between(a: date, b: date) -> int:
    return (b - a).days


# ---------- публичные функции ----------

async def _cats(pool) -> dict[str, int]:
    return {r["name"]: r["id"] for r in await pool.fetch("SELECT id, name FROM categories")}


async def seed(pool) -> None:
    """Заготовка: всё до вчерашнего дня. Таблицы должны быть пустыми (свежая база после миграций)."""
    stmts = build_sql(await _cats(pool))
    await pool.execute("\n".join(stmts))


async def fresh_today(pool) -> None:
    """«Сегодня» по текущему времени посетителя: завтрак, вода, шаги, сон, пара трат и приём лекарств до этого часа."""
    n = now()
    d0 = n.date()
    rng = random.Random(f"{SEED}-{d0}")
    cats = await _cats(pool)
    tz = get_settings().zone
    at = lambda h, m=0: datetime.combine(d0, time(h, m), tzinfo=tz)
    hour = n.hour + n.minute / 60
    out: list[str] = []
    for uid, _name, _kcal, _ext in USERS:
        k = 1.0 if uid == 1 else 0.84
        plan = [(8.5, "breakfast", BREAKFAST, 8, 20), (13.6, "lunch", LUNCH, 13, 15), (16.8, "snack", SNACK, 16, 30), (20.0, "dinner", DINNER, 19, 40)]
        rows = []
        for need, mtype, cat, h, m in plan:
            if hour >= need:
                dish = rng.choice(cat)
                f = k * rng.uniform(0.85, 1.15)
                when = at(h, m)
                rows.append([uid, when, mtype, dish[0], round(dish[1] * f, 1), round(dish[2] * f, 1), round(dish[3] * f, 1), round(dish[4] * f, 1),
                             round(dish[5] * f, 1), "text", when])
        out += ins("meals", ["user_id", "eaten_at", "meal_type", "title", "grams", "kcal", "protein", "fat", "carbs", "source", "created_at"], rows) if rows else []
        mrows = []
        for j, h in enumerate((8, 10, 12, 14, 16, 18, 20)):
            if hour >= h + 0.6:
                mrows.append([uid, "water", 250 + 50 * rng.randint(0, 3), at(h, 30), "text", at(h, 31)])
        if hour >= 8.2:
            mrows.append([uid, "sleep", round(rng.uniform(6.2, 8.2), 1), at(7, 30), "text", at(7, 31)])
        if hour >= 12:
            mrows.append([uid, "steps", int((9800 if uid == 1 else 7600) * min(hour, 21) / 21 * rng.uniform(0.8, 1.1)), n - timedelta(minutes=3), "text", n - timedelta(minutes=3)])
        out += ins("metrics", ["user_id", "kind", "value", "measured_at", "source", "created_at"], mrows) if mrows else []
    tx = []
    if hour >= 9.5:
        tx.append([1, "expense", 290, "RUB", cats["Кафе и рестораны"], "Кофейня у дома", "Кофе по дороге", d0, "text", at(9, 20)])
    if hour >= 15:
        tx.append([2, "expense", 1340 + rng.randint(0, 9), "RUB", cats["Продукты"], "Пятёрочка", "Продукты к ужину", d0, "text", at(14, 40)])
    if tx:
        out += ins("transactions", ["user_id", "kind", "amount", "currency", "category_id", "merchant", "note", "occurred_on", "source", "created_at"],
                   [[*r[:1], *r[1:]] for r in tx]) if False else [
            f"INSERT INTO transactions (family_id, user_id, kind, amount, currency, category_id, merchant, note, occurred_on, source, created_at) VALUES\n" +
            ",\n".join("(1," + ",".join(q(x) for x in r) + ")" for r in tx) + ";"]
    # приём лекарств: что уже прошло — принято, остальное ждёт (на кнопке «Принял»)
    meds = await pool.fetch("SELECT id, user_id, times, days, start_on, end_on FROM meds WHERE active AND deleted_at IS NULL")
    irows = []
    for m in meds:
        if m["start_on"] > d0 or (m["end_on"] and m["end_on"] < d0):
            continue
        if m["days"] and d0.weekday() not in m["days"]:
            continue
        for tm in m["times"]:
            due_at = datetime.combine(d0, tm, tzinfo=tz)
            if due_at <= n - timedelta(minutes=45):
                irows.append([m["id"], due_at, "taken", due_at + timedelta(minutes=rng.randint(2, 30)), m["user_id"]])
            else:
                irows.append([m["id"], due_at, "pending", None, m["user_id"]])
    if irows:
        vals = ",\n".join("(" + ",".join(q(x) for x in r[:3]) + ",0," + ",".join(q(x) for x in r[3:]) + ")" for r in irows)
        out.append(f"INSERT INTO med_intakes (med_id, due_at, status, reminded, taken_at, user_id) VALUES\n{vals} ON CONFLICT DO NOTHING;")
    if out:
        await pool.execute("\n".join(out))
