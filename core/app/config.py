"""Настройки ядра. Все значения берутся из переменных окружения (файл .env)."""
from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    # Общие
    tz: str = "Asia/Irkutsk"
    database_url: str
    core_api_key: str
    public_url: str = ""          # например https://assistant.saidov.ru (не обязательно)
    currency: str = "RUB"
    default_kcal_goal: int = 2000
    family_name: str = "Семья"
    family_desc: str = "2 взрослых и 2 детей"   # для рецептов: на сколько человек готовить

    # Недельный разбор: день недели (0 = понедельник ... 6 = воскресенье) и час. WEEKLY_DIGEST_DAY=-1 выключает.
    weekly_digest_day: int = 6
    weekly_digest_hour: int = 20

    # Утренняя сводка: время по умолчанию (каждый может поменять командой /morning 7:00) и место для погоды
    morning_time: str = "07:30"
    weather_lat: float = 52.29       # Иркутск
    weather_lon: float = 104.28
    weather_place: str = "Иркутск"

    # Веб-панель: сколько дней действует вход
    web_session_days: int = 30

    # Яндекс Алиса: секретная часть адреса вебхука (https://.../alice/<секрет>). Пусто = Алиса выключена.
    alice_secret: str = ""

    # Кто может пользоваться: "123456789:Александр,987654321:Яна"
    allowed_users: str = ""

    # Языковая модель (распознавание текста и фото)
    llm_provider: str = "anthropic"   # anthropic | openai (любой OpenAI-совместимый)
    llm_api_key: str = ""
    llm_model: str = "claude-haiku-5-5"
    llm_vision_model: str = ""        # пусто = та же модель
    llm_base_url: str = ""            # для openai-совместимых: https://openrouter.ai/api/v1 и т.п.
    llm_fallback_model: str = ""      # запасная модель: включается, если основная перегружена

    # Распознавание голоса (OpenAI-совместимый API: Groq, OpenAI и т.п.)
    stt_api_key: str = ""
    stt_base_url: str = "https://api.groq.com/openai/v1"
    stt_model: str = "whisper-large-v3-turbo"

    # Каналы доставки уведомлений
    telegram_bot_token: str = ""

    # TickTick
    ticktick_client_id: str = ""
    ticktick_client_secret: str = ""

    # Кому слать напоминания о платежах, документах, счётчиках и гараже:
    # owner — владельцу (первый в ALLOWED_USERS); author — тому, кто завёл запись; all — всем;
    # или имя, например "Яна". Напоминания о лекарствах всегда получает тот, кто их завёл.
    reminders_to: str = "owner"
    # Во сколько (по местному времени) слать дневные напоминания о платежах, документах, гараже, счётчиках
    daily_reminder_hour: int = 10

    # n8n: сюда ядро отправляет события (новая трата, новая задача и т.п.)
    n8n_webhook_url: str = ""

    # Демо-кабинет (1.10.7): отдельный экземпляр ядра для сайта, у каждого посетителя своя копия вымышленных данных.
    # В обычной установке не нужны. Включается только в сервисе demo (docker-compose.yml, профиль demo).
    demo_mode: bool = False
    demo_max_sessions: int = 8          # сколько демо открыто одновременно
    demo_idle_min: int = 20             # минут без действий, после чего копия удаляется
    demo_max_min: int = 60              # предельная длительность одного сеанса
    demo_ai_messages: int = 10          # сообщений ИИ-чату за сеанс
    demo_ai_calls: int = 30             # всех обращений к ИИ за сеанс (чат, диетолог, вопросы, справка к лекарству)
    demo_ai_daily: int = 300            # всех обращений к ИИ в сутки по всему демо
    demo_new_per_hour: int = 10         # новых сеансов в час с одного адреса
    demo_history_days: int = 183        # глубина вымышленной истории
    demo_site_url: str = "https://assistant.saidov.ru"   # куда ведёт ссылка «на сайт проекта» из демо

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    @property
    def vision_model(self) -> str:
        return self.llm_vision_model or self.llm_model

    @property
    def ticktick_redirect_uri(self) -> str:
        if self.public_url:
            return self.public_url.rstrip("/") + "/ticktick/callback"
        return "http://localhost:8080/ticktick/callback"

    def allowed(self) -> list[tuple[str, str]]:
        """Список (telegram_id, имя) из ALLOWED_USERS."""
        out = []
        for part in self.allowed_users.split(","):
            part = part.strip()
            if not part:
                continue
            tid, _, name = part.partition(":")
            out.append((tid.strip(), name.strip() or tid.strip()))
        return out


@lru_cache
def get_settings() -> Settings:
    return Settings()
