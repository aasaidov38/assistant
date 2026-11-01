from datetime import date, datetime

from .config import get_settings

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def now() -> datetime:
    return datetime.now(get_settings().zone)


def today() -> date:
    return now().date()


def esc(s: str | None) -> str:
    """Экранирование для HTML-разметки Telegram."""
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def plural(n: int, one: str, few: str, many: str) -> str:
    """plural(3, 'запись', 'записи', 'записей') -> '3 записи'"""
    n10, n100 = abs(n) % 10, abs(n) % 100
    w = one if n10 == 1 and n100 != 11 else few if 2 <= n10 <= 4 and not 12 <= n100 <= 14 else many
    return f"{n} {w}"


def parse_date(v) -> date | None:
    """«2027-05-15», «15.05.2027» или null → дата."""
    if not v:
        return None
    v = str(v).strip()
    try:
        return date.fromisoformat(v[:10])
    except ValueError:
        pass
    import re
    m = re.fullmatch(r"(\d{1,2})[./](\d{1,2})[./](\d{2,4})", v)
    if m:
        d, mo, y = map(int, m.groups())
        try:
            return date(y + 2000 if y < 100 else y, mo, d)
        except ValueError:
            return None
    return None


def add_months(d: date, months: int, day: int | None = None) -> date:
    """Сдвиг даты на N месяцев; число месяца не больше, чем дней в месяце (31 → 30 или 28/29)."""
    import calendar
    y, m = divmod(d.month - 1 + months, 12)
    y, m = d.year + y, m + 1
    last = calendar.monthrange(y, m)[1]
    return date(y, m, min(day or d.day, last))


def num(v) -> float | None:
    """Число из ответа ИИ или текста: «1 450,5» → 1450.5."""
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(" ", "").replace("\u00a0", "").replace(",", "."))
    except ValueError:
        return None


def fnum(v, digits: int = 2) -> str:
    """Число по-русски без лишних нулей: 123.450 → «123,45»."""
    s = f"{float(v):.{digits}f}".rstrip("0").rstrip(".")
    return s.replace(".", ",")

