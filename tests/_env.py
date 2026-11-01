"""Общие настройки тестов: пути к коду и подключение к тестовой PostgreSQL.

Подключение берётся из переменных PGHOST, PGPORT, PGUSER, PGDATABASE (как у psql).
По умолчанию — локальная тестовая база в песочнице Claude: сокет /tmp, порт 55432.
"""
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "stubs"), os.path.join(HERE, "..", "core")]
os.environ.setdefault("PGHOST", "/tmp")
os.environ.setdefault("PGPORT", "55432")
os.environ.setdefault("PGUSER", "assistant")
os.environ.setdefault("PGDATABASE", "assistant")
os.environ.setdefault("TZ", "Asia/Irkutsk")
os.environ.setdefault("DATABASE_URL", "x")
os.environ.setdefault("CORE_API_KEY", "x")
PSQL = ["psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1"]
