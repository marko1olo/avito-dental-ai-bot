# Контракты интерфейсов

Задаёт архитектор. Модули пишутся под эти подписи и **не меняют их** — по ним стыкуются части,
которые пишутся параллельно и не видят друг друга. Если подпись мешает, это повод сказать
архитектору, а не переименовать у себя.

Общие правила для всего Python-кода проекта:

- Python 3.11+. `from __future__ import annotations` в каждом файле.
- Никаких заглушек, `TODO`, `pass  # позже` и моков в продуктовом коде. Мок допустим только
  внутри `brain/tests/`.
- Секреты читаются из окружения (`os.environ`), никогда не хардкодятся, никогда не попадают
  в лог, в исключение, в файл состояния или в текст для пациента.
- Данные читаются из `avito-bot/data/*.json`. Эти файлы — единственный источник правды по
  ценам, фактам клиники и графику. Дублировать их значения в коде запрещено.
- Все деньги — целые рубли (`int`). Все моменты времени — `datetime` c таймзоной
  `Europe/Moscow`, берётся через `brain/gate/hours.py: tz()`.
- Тесты запускаются как `python brain/tests/test_<модуль>.py`, возвращают ненулевой код при
  провале и печатают человекочитаемый итог `ИТОГ: N/M`.

## Уже существует и менять нельзя

```python
# brain/gate/hours.py
def tz() -> ZoneInfo
def now() -> datetime
def day_status(day: date) -> DayStatus          # .open_for_booking .certain .opens .last_appointment
def is_booking_open(moment: datetime | None = None) -> bool
def next_booking_day(after: date | None = None, horizon_days: int = 14) -> DayStatus | None
def describe_schedule() -> str
def describe_now() -> str

# brain/gate/intent.py
def normalize(text: str) -> str
def classify(raw_text: str) -> Decision         # .route Route .kind Kind .topic .matched .reason
class Route(str, Enum): AUTO DRAFT IGNORE
class Kind(str, Enum): SAFE_FACT PRICE MEDICAL BOOKING NO_QUOTE_TOPIC JUNK UNKNOWN

# brain/guard.py
def check(reply: str, *, topic: str | None = None) -> Verdict   # .ok .violations .reason
def money_amounts(text: str) -> list[int]
def allowed_amounts() -> frozenset[int]

# brain/delay.py
def plan_reply(reply_text: str, *, is_first_reply: bool, received_at: datetime | None = None,
               last_delay_seconds: float | None = None, rng=None) -> Plan   # .send_at .delay_seconds .reason
def should_wait_for_more(seconds_since_last_message: float) -> bool

# brain/followup.py
def plan(state: DialogState, now: datetime | None = None, *, first_route=Route.DRAFT) -> Followup | None
```

## Пишется сейчас

### `brain/llm/client.py`

```python
@dataclass(frozen=True)
class LlmResult:
    text: str | None          # None, если все попытки провалились
    model: str                # фактически ответившая модель
    provider: str             # "gemini" | "groq"
    attempts: int
    latency_ms: int
    failure: str | None       # None при успехе, иначе код причины

async def complete(system: str, user: str, *, temperature: float = 0.35,
                   max_tokens: int = 400, purpose: str = "reply",
                   timeout_s: float = 25.0) -> LlmResult
def health() -> dict         # {"keys_total", "keys_on_cooldown", "models_banned", "last_failure"}
```

Коды `failure`: `no_keys`, `all_keys_on_cooldown`, `all_models_banned`, `rate_limited`,
`model_overloaded`, `key_denied`, `timeout`, `empty_response`, `request_failed`.

Требования, вынесенные из разбора stomchat (`brain/gemini_client.py` — **справочный материал,
импортировать нельзя**, его первая строка тянет `config.py`, который делает `sys.exit(1)`):

- Ключи: `GOOGLE_API_KEYS` и `GROQ_API_KEYS`, comma-separated в одной переменной.
  На ноутбуке лежат 10 и 7 соответственно.
- Ключи перемешиваются, кулдаунные исключаются ДО цикла попыток, каждая попытка берёт свой ключ.
- **429 проверяется РАНЬШЕ 5xx.** Тела 429 часто содержат `quota_limit_value: 500 per day`,
  и проверка 5xx первой банит здоровые модели. На этом уже обжигались.
- 429 → кулдаун ключа 300 с, без бана модели. 5xx/`deadline`/`unavailable` → бан модели 1200 с.
  401/403 → следующий ключ.
- Состояние кулдаунов и банов — в файлах рядом с `data/`, ключ на диске только как
  `sha256(provider:key)[:16]`, никогда в открытом виде.
- В лог не попадает ни ключ, ни сырой текст исключения провайдера (провайдеры возвращают
  тела вида `401 invalid api key <key>`).
- Каскад моделей — в `brain/llm/cascade.py` как данные, а не внутри функции.
  Идентификаторы моделей проверить обращением к живому API перед тем, как зашивать.

### `brain/prompt/builder.py`

```python
@dataclass(frozen=True)
class Turn:
    role: Literal["patient", "clinic"]
    text: str
    at: datetime

def build_system_prompt(*, topics: Sequence[str], moment: datetime | None = None) -> str
def build_user_prompt(history: Sequence[Turn], incoming: str) -> str
def allowed_topics() -> frozenset[str]
```

- Текст промпта — из `docs/sales-strategy.md` (раздел «Системный промпт») и
  `docs/dialogue-playbook.md`. Формулировки оттуда, не свои.
- Блок цен собирается **только** из записей `patient-quotes.json` с `quote_allowed: true`,
  и только по темам из `topics`. Полный прайс в промпт не подаётся.
- Из `ortho-prices.json` в промпт не попадает **ничего**: `prices_may_be_quoted: false`.
  `internal_cost_rub` не должен просочиться ни при каких входных данных — это отдельный тест.
- Почта ортодонта в `clinic-facts.json` помечена `status: internal` — в промпт не идёт.
- График вставляется через `hours.describe_schedule()` / `describe_now()`, не строкой.

### `brain/store/db.py`

```python
class Store:
    def __init__(self, path: str | Path) -> None      # WAL, foreign_keys ON
    # дедуп
    def seen(self, external_id: str) -> bool
    def mark_seen(self, external_id: str, chat_id: str, at: datetime) -> bool   # False если уже был
    # диалоги
    def touch_dialog(self, chat_id: str, *, patient_message_at: datetime | None = None,
                     our_message_at: datetime | None = None) -> None
    def dialog(self, chat_id: str) -> DialogRow | None
    def set_takeover(self, chat_id: str, until: datetime | None) -> None
    def set_ai_paused(self, chat_id: str, until: datetime | None) -> None
    def is_ai_active(self, chat_id: str, moment: datetime) -> bool
    def capture_phone(self, chat_id: str, phone_hash: str) -> None    # ХЭШ, не номер
    # черновики
    def queue_draft(self, chat_id: str, text: str, *, kind: str, reason: str) -> int
    def draft(self, draft_id: int) -> DraftRow | None
    def pending_drafts(self, limit: int = 50) -> list[DraftRow]
    def resolve_draft(self, draft_id: int, *, action: str, final_text: str | None = None,
                      by: str | None = None) -> DraftRow
    def link_draft_message(self, draft_id: int, tg_message_id: int) -> None
    def draft_by_tg_message(self, tg_message_id: int) -> DraftRow | None
    # аудит
    def audit(self, event: str, *, chat_id: str | None = None, payload: dict | None = None) -> None
    def recent_audit(self, limit: int = 100) -> list[AuditRow]
```

- `mark_seen` идемпотентен: повторный вызов с тем же `external_id` возвращает `False` и не
  создаёт второй ряд. У Авито уникальности id не гарантировано ничем на нашей стороне.
- **Номера телефонов пациентов в базу не пишутся** — только `phone_hash`. Сам номер живёт
  в переписке Авито, дублировать его в своей БД незачем, а 152-ФЗ это упрощает радикально.
- Схема — в `brain/store/schema.sql`, применяется идемпотентно при открытии.
- `action` в `resolve_draft`: `sent` | `edited` | `ignored` | `expired`.

### `brain/pii.py`

```python
def scrub(text: str) -> str                  # для логов и аудита
def find_phones(text: str) -> list[str]      # нормализованные, для детекции «дал телефон»
def phone_hash(phone: str) -> str            # sha256[:16] от нормализованного номера
def has_pii(text: str) -> bool
```

- Ловит российские номера во всех живых формах: `+7 999 123-45-67`, `8(999)1234567`,
  `9991234567`, с точками и пробелами. Плюс email.
- **Наши собственные номера не считаются ПД** и не скрабятся: `+7 800 555-35-35`
  и `+7 900 000-00-00` берутся из `clinic-facts.json`, не из константы в коде.
- Кириллические имена регуляркой не ловятся надёжно — не пытаться, лучше явно не уметь,
  чем уметь наполовину и создать ложное чувство защиты. Так и написать в docstring.
- В dvachbot есть `common/secret_redaction.py` (6 паттернов токенов, ноль ПД) — оттуда
  берётся только идея logging-фильтра, ПД надо писать с нуля.

### `brain/tg/panel.py`

```python
@dataclass(frozen=True)
class Action:
    kind: Literal["send", "edit", "ignore", "takeover", "pause", "resume", "unknown"]
    draft_id: int | None
    chat_id: str | None
    payload: dict

async def post_draft(draft: DraftRow, *, dialog_excerpt: str) -> int      # -> tg message_id
async def parse_callback(update: dict) -> Action
async def notify(text: str, *, level: Literal["info", "warn", "alarm"] = "info") -> None
def keyboard_for(draft: DraftRow) -> dict
```

- Токен только `os.environ["TELEGRAM_BOT_TOKEN"]`, чат только `os.environ["TELEGRAM_CHAT_ID"]`.
  Отсутствие переменной — внятная ошибка при старте, а не падение в момент первого лида.
  **Токен не хардкодить, не печатать, не писать в файл ни при каких условиях.**
- Кнопки: `Отправить` · `Правка` · `Игнор` · `Перехватить диалог` · `Пауза ИИ`
  (варианты паузы: 1 час / до утра / совсем).
- «Правка» работает как **ответ на сообщение бота** в группе — приватность бота остаётся
  включённой, чтобы бот не читал всю переписку администраторов.
- Текст пациента в Telegram уходит через `pii.scrub()` только в логи; в самом черновике
  администратор должен видеть оригинал, иначе он не сможет ответить осмысленно.
- HTTP — `httpx.AsyncClient`. Никакой библиотеки-фреймворка бота: нужны три метода
  (`sendMessage`, `answerCallbackQuery`, `getUpdates`), тянуть aiogram ради них не надо.

## Как это соединяется (пишет архитектор, агентам знать для контекста)

```
capture/poll.mjs  ──SQLite──▶  store.mark_seen (дедуп)
                                     │
                              router.handle()
                                     ├── intent.classify        (фоллбек и вето)
                                     ├── prompt.build_*         (только разрешённые цены)
                                     ├── llm.complete           (17 ключей, каскад)
                                     ├── guard.check            (вето поверх ответа модели)
                                     └── delay.plan_reply
                                     │
                        AUTO ────────┴──────── DRAFT
                          │                      │
                  store.queue → send      tg.post_draft → кнопки → resolve_draft
                          │                                            │
                  capture/send.mjs ◀────────────SQLite─────────────────┘
```
