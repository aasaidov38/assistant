"""Тестовая замена asyncpg: выполняет запросы через psql (в песочнице нет pip-пакета asyncpg)."""
import json, re, subprocess, uuid
from datetime import date, datetime, timedelta
from decimal import Decimal

class Pool: pass
class Connection: pass
PSQL = ["psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1"]

def lit(v):
    if v is None: return "NULL"
    if isinstance(v, bool): return "true" if v else "false"
    if isinstance(v, (int, float, Decimal)): return str(v)
    if isinstance(v, list) and not any(isinstance(x, dict) for x in v):
        return "'{" + ",".join(str(x.isoformat() if hasattr(x, "isoformat") else x) for x in v) + "}'"
    if isinstance(v, bytes): return "'\\x" + v.hex() + "'"
    if isinstance(v, (dict, list)): v = json.dumps(v, ensure_ascii=False, default=str)
    if isinstance(v, timedelta): v = f"{v.total_seconds()} seconds"
    if isinstance(v, (datetime, date)): v = v.isoformat()
    return "'" + str(v).replace("'", "''") + "'"

TS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d")
from datetime import time as _time
def conv(v):
    if isinstance(v, list): return [conv(x) for x in v]
    if isinstance(v, str) and re.fullmatch(r"\d\d:\d\d:\d\d", v): return _time.fromisoformat(v)
    if isinstance(v, str):
        if TS.match(v): return datetime.fromisoformat(v)
        if re.fullmatch(r"\d{4}-\d\d-\d\d", v): return date.fromisoformat(v)
    if isinstance(v, (int, float)) and not isinstance(v, bool) and isinstance(v, float): return Decimal(str(v))
    return v

FAKE_NOW = None   # тест может подменить «сейчас» в запросах

def run(sql, args, rows, dbname=None):
    psql = PSQL + (['-d', dbname] if dbname else [])
    if not args and not rows and (re.match(r"\s*(create|drop|alter)", sql, re.I) or sql.count(';') > 1):
        p = subprocess.run(psql, input=sql, capture_output=True, text=True)   # DDL и сценарии из нескольких команд
        if p.returncode:
            raise RuntimeError(p.stderr.strip() + '\nSQL: ' + sql[:300])
        return ''
    if FAKE_NOW is not None:
        ts = FAKE_NOW.isoformat()
        sql = re.sub(r"\bnow\(\)", f"'{ts}'::timestamptz", sql)
        sql = re.sub(r"\bcurrent_date\b", f"('{ts}'::timestamptz)::date", sql)
    name = "p" + uuid.uuid4().hex[:8]
    ex = f"EXECUTE {name}({', '.join(lit(a) for a in args)})" if args else f"EXECUTE {name}"
    if rows and re.match(r"\s*(insert|update|delete)", sql, re.I):
        script = (f"PREPARE {name} AS WITH r AS ({sql}) SELECT coalesce(jsonb_agg(r), '[]')::text FROM r;\n{ex};")
    elif rows:
        script = (f"PREPARE {name} AS {sql};\nCREATE TEMP TABLE r_{name} AS {ex};\n"
                  f"SELECT coalesce(jsonb_agg(t), '[]')::text FROM r_{name} t;")
    else:
        script = f"PREPARE {name} AS {sql};\n\\set QUIET off\n{ex};"
    p = subprocess.run(psql, input=script, capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError(p.stderr.strip() + "\nSQL: " + sql)
    out = p.stdout.strip()
    if rows:
        return [{k: conv(v) for k, v in r.items()} for r in json.loads(out.splitlines()[-1])]
    return out.splitlines()[-1] if out else ""

class FakeConn:
    db = None          # имя базы; None — база из переменных PG*
    async def fetch(self, sql, *a): return run(sql, a, True, self.db)
    async def fetchrow(self, sql, *a):
        r = run(sql, a, True, self.db) if re.match(r"\s*(select|with)", sql, re.I) or re.search(r"\breturning\b", sql, re.I) else None
        return r[0] if r else None
    async def fetchval(self, sql, *a):
        r = await self.fetchrow(sql, *a)
        return next(iter(r.values())) if r else None
    async def execute(self, sql, *a): return run(sql, a, False, self.db)
    async def close(self): pass
    def transaction(self, **k): return _Ctx(self)
    def acquire(self): return _Ctx(self)
class _Ctx:
    def __init__(self, c): self.c = c
    async def __aenter__(self): return self.c
    async def __aexit__(self, *e): return False


class FakePool(FakeConn):
    def __init__(self, dbname): self.db = dbname

async def create_pool(dsn=None, **kw):
    """Пул на указанную в dsn базу (имя — последняя часть адреса)."""
    return FakePool(re.sub(r"[?].*$", "", dsn or "").rsplit("/", 1)[-1] or None)
