# -*- coding: utf-8 -*-
"""Панель администратора: черновик с кнопками, разбор нажатий, уведомления.

Модуль отвечает на один вопрос: **что администратор видит и что означает его
нажатие**. Он ничего не отправляет пациенту, ничего не пишет в базу и ничего не
решает сам. Наружу он отдаёт `Action` — намерение администратора, — а применяет
его демон (`brain/run.py`), потому что применение решения это транзакция в
`store` плюс отправка в Авито, и оба конца принадлежат демону, а не панели.

Четыре решения, из-за которых файл выглядит именно так.

**`callback_data` — не JSON, а `d1:pau:1234:am`.** Лимит Telegram на это поле —
64 БАЙТА, не символа (`api.CALLBACK_DATA_LIMIT`). JSON тратит байты на кавычки
и повторяющиеся ключи в каждой кнопке, а кириллица внутри съедала бы лимит
вдвое быстрее — поэтому здесь латинские коды фиксированной длины. Что важнее
экономии: превышение лимита Telegram не обрезает, он отклоняет ВСЮ клавиатуру
ошибкой 400, то есть черновик уходит администратору без единой кнопки, и лид
разбирается руками. Поэтому длина проверяется в `_pack()` при каждой сборке и
ещё раз в `check_config()` при старте — до первого лида, а не на нём.

**В `callback_data` нет `chat_id`.** Идентификатор чата Авито (`u2i-…`) — это
20-30 байт, и вместе с ним пауза «до утра» подошла бы к лимиту вплотную:
достаточно чуть более длинного id, чтобы кнопки перестали доходить. Вместо
этого в данных кнопки лежит `draft_id` — первичный ключ, по которому демон
берёт `store.draft(draft_id).chat_id`. Один источник правды вместо двух, и
разбор остаётся идемпотентным: одна и та же кнопка, нажатая дважды (Telegram
доставляет апдейты повторно), даёт один и тот же `Action`, а идемпотентность
самого решения обеспечивает `store.resolve_draft`.

**Оригинал текста пациента в черновике не скрабится.** Это не недосмотр, а
требование: администратор должен ответить осмысленно, а `[телефон]` вместо
номера, по которому пациент просит перезвонить, делает черновик бесполезным.
`pii.scrub()` стоит на другом пути — в логах этого модуля и в `notify()`,
куда идут системные события и цитаты для аудита. Разделение простое: **тело
черновика — оригинал, всё остальное — через скраббер.**

**Токен здесь не существует.** Панель не знает ни его значения, ни его длины:
за конфигурацией она ходит в `api.load_config()`, а сетевые вызовы делает
через `api.send_message`/`api.answer_callback_query`, которые вымарывают токен
из любой ошибки (`api.redact`). Поэтому в этом файле нет ни одного `log`,
куда токен мог бы попасть, и `Config` наружу не логируется даже в отладке.

Чего в модуле нет намеренно: библиотеки-фреймворка бота. Нужны три метода Bot
API, они уже есть в `api.py`; роутеры и FSM aiogram принесли бы второй источник
правды о состоянии диалога, а он живёт в SQLite.
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

# Пакет не устанавливается (нет venv-шага в деплое на ноутбук клиники), поэтому
# соседние модули подключаются по пути — так же, как в brain/store/db.py.
# Заменить на обычный импорт, когда появится pyproject.
_BRAIN = Path(__file__).resolve().parents[1]
for _extra in (str(_BRAIN), str(_BRAIN / "gate")):
    if _extra not in sys.path:
        sys.path.insert(0, _extra)

import hours  # noqa: E402
import pii  # noqa: E402

try:  # обычный путь: панель импортируется как `tg.panel`
    from . import api
except ImportError:  # запуск с brain/tg в sys.path
    import api  # type: ignore[no-redef]  # noqa: E402

if TYPE_CHECKING:  # DraftRow нужен только для аннотаций (см. `from __future__`)
    from store.db import DraftRow

log = logging.getLogger("tg.panel")

Kind = Literal["send", "edit", "ignore", "takeover", "pause", "resume", "unknown"]

#: Версия формата `callback_data`. Кнопки живут в чате администраторов вечно, и
#: после смены формата в чате остаются сообщения со старыми данными. Префикс с
#: версией превращает такое нажатие в «unknown» вместо неверно разобранного
#: действия: устаревшая кнопка «Игнор» не должна однажды сработать как «Отправить».
CALLBACK_VERSION = "d1"
_SEP = ":"

#: Код в `callback_data` -> вид действия. Коды по три латинских буквы: пять
#: байт экономии на каждой кнопке и никакой неоднозначности при разборе.
_CODE_TO_KIND: dict[str, Kind] = {
    "snd": "send",
    "edt": "edit",
    "ign": "ignore",
    "tko": "takeover",
    "pau": "pause",
    "res": "resume",
}
_KIND_TO_CODE: dict[str, str] = {kind: code for code, kind in _CODE_TO_KIND.items()}

#: Варианты паузы ИИ. Значение — код в `callback_data`.
PAUSE_VARIANTS: dict[str, str] = {"hour": "1h", "morning": "am", "forever": "inf"}
_CODE_TO_VARIANT: dict[str, str] = {code: name for name, code in PAUSE_VARIANTS.items()}

PAUSE_HOUR = timedelta(hours=1)
#: Резервный срок для «до утра», если график на 14 дней вперёд не даёт ни одного
#: дня с известным временем открытия. Часы клиники берутся из
#: `data/clinic-facts.json` через `hours`, дублировать их здесь запрещено —
#: поэтому фоллбек выражен относительным сроком, а не временем «09:00».
MORNING_FALLBACK = timedelta(hours=12)

#: Тексты кнопок. Набор задан контрактом: Отправить · Правка · Игнор ·
#: Перехватить диалог · Пауза ИИ (1 час / до утра / совсем). «Вернуть ИИ» —
#: добавление: без неё «Пауза совсем» необратима из Telegram, а `Action`
#: контракта содержит вид `resume`, то есть панель обязана уметь его выдать.
#: `api.py` не содержит `editMessageReplyMarkup`, поэтому клавиатура
#: неизменяема после отправки: все варианты паузы показываются сразу, а не
#: вторым шагом по нажатию «Пауза ИИ».
LABEL_SEND = "Отправить"
LABEL_EDIT = "Правка"
LABEL_IGNORE = "Игнор"
LABEL_TAKEOVER = "Перехватить диалог"
LABEL_PAUSE_HOUR = "Пауза ИИ: 1 час"
LABEL_PAUSE_MORNING = "до утра"
LABEL_PAUSE_FOREVER = "совсем"
LABEL_RESUME = "Вернуть ИИ"

#: Подсказка, которую демон показывает алертом на нажатие «Правка». Кнопка не
#: может открыть поле ввода — правка приходит ОТВЕТОМ на сообщение бота, и это
#: единственный способ при включённой приватности бота в группе (иначе бот
#: читал бы всю переписку администраторов, см. `api.get_updates`).
EDIT_HINT = ("Ответьте на это сообщение текстом правки — он уйдёт пациенту "
             "вместо черновика.")

#: Сколько символов оригинала пациента и текста ИИ попадает в одно сообщение.
#: Сумма с заголовком заведомо меньше `api.TEXT_LIMIT`: обрезать должен наш
#: `clamp` с пометкой, а не Telegram отказом на всё сообщение.
EXCERPT_LIMIT = 1200
REPLY_LIMIT = 2200

#: Человеческие названия видов черновика. Значения приходят из
#: `gate/intent.py: Kind`; неизвестное показывается как есть.
KIND_RU: dict[str, str] = {
    "safe_fact": "безобидный факт",
    "price": "цена",
    "medical": "симптомы",
    "booking": "запись",
    "no_quote_topic": "тема без цены",
    "junk": "мусор",
    "unknown": "не распознано",
}

LEVEL_MARK: dict[str, str] = {"info": "", "warn": "ВНИМАНИЕ. ", "alarm": "ТРЕВОГА. "}

__all__ = [
    "Action", "post_draft", "parse_callback", "notify", "keyboard_for",
    "render_draft", "callback_data", "ack", "check_config", "config",
    "pause_until", "CALLBACK_VERSION", "PAUSE_VARIANTS", "EDIT_HINT",
]


@dataclass(frozen=True)
class Action:
    """Намерение администратора. Применяет его демон, не панель.

    `chat_id` заполнен только там, где панель его действительно знает (сейчас
    таких путей нет: кнопка несёт `draft_id`, а чат демон берёт из ряда
    черновика). Поле оставлено контрактом и не выдумывается из апдейта:
    подставить сюда id ЧАТА TELEGRAM было бы прямой ошибкой — в `store` этим
    ключом адресуется диалог АВИТО.

    `payload` для каждого вида:
    `send`     — {}
    `edit`     — `stage`: "prompt" (нажата кнопка, нужен ответ-подсказка) либо
                 "text" (пришёл ответ на сообщение бота): тогда `text` —
                 оригинал правки, `tg_message_id` — сообщение черновика.
    `ignore`   — {}
    `takeover` — `until`: None, то есть до отмены (см. `store._set_until`).
    `pause`    — `variant`: "hour" | "morning" | "forever";
                 `until`: `datetime` либо None для «совсем».
    `resume`   — `until`: момент в прошлом; демон применяет его И к
                 `set_ai_paused`, И к `set_takeover` — в базе это независимые
                 сроки, и снятие одного не вернёт бота, пока держит второй.
    `unknown`  — `reason`: почему апдейт не разобран.

    В любом виде, пришедшем от нажатия кнопки, есть `callback_query_id` и `by`.
    """

    kind: Kind
    draft_id: int | None = None
    chat_id: str | None = None
    payload: dict = field(default_factory=dict)

    @property
    def actionable(self) -> bool:
        """Требует ли действие записи в базу. `unknown` и подсказка — нет."""
        if self.kind == "unknown":
            return False
        return not (self.kind == "edit" and self.payload.get("stage") == "prompt")


# --- конфигурация -----------------------------------------------------------

_CONFIG: api.Config | None = None


def config(*, reload: bool = False) -> api.Config:
    """Токен и чат из окружения, один раз на процесс.

    Кэш нужен не ради скорости, а ради предсказуемости: панель должна работать с
    той же конфигурацией, что проверил `check_config()` при старте, даже если
    кто-то поменяет переменные окружения на живом процессе.
    """
    global _CONFIG
    if _CONFIG is None or reload:
        _CONFIG = api.load_config()
    return _CONFIG


def check_config() -> str:
    """Проверка при старте. Возвращает строку для лога БЕЗ токена.

    Кроме окружения проверяет то, что иначе обнаружится только на живом лиде:
    длину `callback_data` для всех кнопок на заведомо большом `draft_id`.
    Превышение 64 байт Telegram не обрезает — он отклоняет сообщение с
    клавиатурой целиком, и черновик уходит без кнопок.
    """
    line = api.check_config()
    config(reload=True)
    worst = max(len(data.encode("utf-8")) for data in _all_callback_data(10 ** 12))
    return f"{line}; панель: кнопок {len(_all_callback_data(10 ** 12))}, " \
           f"худшая callback_data {worst} байт из {api.CALLBACK_DATA_LIMIT}"


def _all_callback_data(draft_id: int) -> list[str]:
    """Все данные кнопок для одного черновика — для проверок длины."""
    out = [callback_data(kind, draft_id)
           for kind in ("send", "edit", "ignore", "takeover", "resume")]
    out += [callback_data("pause", draft_id, variant=name) for name in PAUSE_VARIANTS]
    return out


# --- данные кнопок ----------------------------------------------------------

def callback_data(kind: str, draft_id: int, *, variant: str | None = None) -> str:
    """Собирает `callback_data` кнопки: `d1:<код>:<draft_id>[:<вариант>]`."""
    code = _KIND_TO_CODE.get(kind)
    if code is None:
        raise ValueError(f"нет кода callback для вида {kind!r}, "
                         f"известны {sorted(_KIND_TO_CODE)}")
    if kind == "pause":
        if variant not in PAUSE_VARIANTS:
            raise ValueError(f"неизвестный вариант паузы {variant!r}, "
                             f"допустимы {sorted(PAUSE_VARIANTS)}")
    elif variant is not None:
        raise ValueError(f"вид {kind!r} не принимает вариант")
    if draft_id < 0:
        raise ValueError("draft_id не может быть отрицательным")
    return _pack(code, draft_id, PAUSE_VARIANTS[variant] if variant else "")


def _pack(code: str, draft_id: int, arg: str) -> str:
    parts = [CALLBACK_VERSION, code, str(draft_id)]
    if arg:
        parts.append(arg)
    data = _SEP.join(parts)
    size = len(data.encode("utf-8"))
    if size > api.CALLBACK_DATA_LIMIT:
        # Молча обрезать нельзя: обрезанные данные разобрались бы в другое
        # действие. Лучше не собрать кнопку, чем собрать не ту.
        raise ValueError(
            f"callback_data {size} байт > {api.CALLBACK_DATA_LIMIT}: "
            f"код {code!r}, draft_id {draft_id}. Telegram отклонит всю "
            "клавиатуру, черновик уйдёт без кнопок")
    return data


def _unpack(data: object) -> tuple[Kind, int | None, str | None, str]:
    """Разбирает данные кнопки. Никогда не бросает: мусор -> ("unknown", …).

    Возвращает (вид, draft_id, вариант, причина). Причина заполнена только для
    «unknown» и годится для лога.
    """
    if not isinstance(data, str) or not data:
        return "unknown", None, None, "в апдейте нет строки data"
    parts = data.split(_SEP)
    if len(parts) < 3 or parts[0] != CALLBACK_VERSION:
        return "unknown", None, None, f"чужой или устаревший формат data ({len(data)} байт)"
    kind = _CODE_TO_KIND.get(parts[1])
    if kind is None:
        return "unknown", None, None, f"неизвестный код действия {parts[1]!r}"
    if not parts[2].isdigit():
        return "unknown", None, None, "draft_id не число"
    draft_id = int(parts[2])
    variant: str | None = None
    if kind == "pause":
        if len(parts) < 4:
            return "unknown", None, None, "пауза без варианта"
        variant = _CODE_TO_VARIANT.get(parts[3])
        if variant is None:
            return "unknown", None, None, f"неизвестный вариант паузы {parts[3]!r}"
    return kind, draft_id, variant, ""


# --- клавиатура -------------------------------------------------------------

def keyboard_for(draft: DraftRow) -> dict:
    """Inline-клавиатура под черновиком.

    У решённого черновика «Отправить»/«Правка»/«Игнор» не показываются: решение
    уже принято, и повторное нажатие всё равно упёрлось бы в `resolve_draft`.
    Управление диалогом остаётся — перехват и пауза относятся к чату, а не к
    этому черновику, и нужны как раз после того, как черновик разобран.
    """
    draft_id = int(draft.id)
    rows: list[list[dict[str, str]]] = []
    if getattr(draft, "is_pending", True):
        rows.append([
            {"text": LABEL_SEND, "callback_data": callback_data("send", draft_id)},
            {"text": LABEL_EDIT, "callback_data": callback_data("edit", draft_id)},
        ])
        rows.append([
            {"text": LABEL_IGNORE, "callback_data": callback_data("ignore", draft_id)},
        ])
    rows.append([
        {"text": LABEL_TAKEOVER, "callback_data": callback_data("takeover", draft_id)},
    ])
    rows.append([
        {"text": LABEL_PAUSE_HOUR,
         "callback_data": callback_data("pause", draft_id, variant="hour")},
        {"text": LABEL_PAUSE_MORNING,
         "callback_data": callback_data("pause", draft_id, variant="morning")},
        {"text": LABEL_PAUSE_FOREVER,
         "callback_data": callback_data("pause", draft_id, variant="forever")},
    ])
    rows.append([
        {"text": LABEL_RESUME, "callback_data": callback_data("resume", draft_id)},
    ])
    return {"inline_keyboard": rows}


# --- срок паузы -------------------------------------------------------------

def pause_until(variant: str, moment: datetime | None = None) -> datetime | None:
    """Момент, до которого молчит ИИ. None означает «совсем» (см. `store`).

    «До утра» — это время открытия клиники, а не абстрактные 09:00: администратор
    нажимает эту кнопку вечером, чтобы бот не отвечал ночью, и ждёт возврата к
    началу приёма. Время берётся из `hours` (то есть из `clinic-facts.json`),
    потому что дублировать график в коде запрещено.
    """
    if variant not in PAUSE_VARIANTS:
        raise ValueError(f"неизвестный вариант паузы {variant!r}")
    now = moment or hours.now()
    if variant == "forever":
        return None
    if variant == "hour":
        return now + PAUSE_HOUR
    return _next_opening(now)


def _next_opening(now: datetime) -> datetime:
    today = hours.day_status(now.date())
    if today.opens is not None:
        candidate = now.replace(hour=today.opens.hour, minute=today.opens.minute,
                                second=0, microsecond=0)
        if candidate > now:
            return candidate
    nxt = hours.next_booking_day(now.date())
    if nxt is not None and nxt.opens is not None:
        return datetime.combine(nxt.day, nxt.opens, tzinfo=hours.tz())
    # Ни одного дня с известным временем открытия на горизонте: молчим 12 часов.
    # Это заведомо переживает ночь и не выдумывает график.
    return now + MORNING_FALLBACK


# --- текст черновика --------------------------------------------------------

def render_draft(draft: DraftRow, *, dialog_excerpt: str) -> str:
    """Сообщение администратору. **Текст пациента здесь не скраблен.**

    Скраббер стоит только на логах и `notify()`. Здесь администратор обязан
    видеть оригинал: по «перезвоните на 999 12 34 56» он звонит, а по
    «перезвоните на [телефон]» — идёт искать чат в Авито руками.
    """
    kind = KIND_RU.get(draft.kind, draft.kind)
    head = [f"Черновик #{draft.id} — {kind}",
            f"причина: {draft.reason}",
            f"чат: {draft.chat_id} · {draft.created_at.strftime('%d.%m %H:%M')}"]
    if not getattr(draft, "is_pending", True):
        head.append(f"уже решён: {draft.status}"
                    + (f" ({draft.resolved_by})" if draft.resolved_by else ""))

    body = [
        "\n".join(head),
        "Пациент:\n" + api.clamp(dialog_excerpt.strip() or "(пусто)", EXCERPT_LIMIT),
        "Ответ ИИ:\n" + api.clamp(draft.text.strip(), REPLY_LIMIT),
        EDIT_HINT,
    ]
    # Финальный clamp — страховка на случай неожиданно длинной причины или
    # chat_id: `send_message` обрежет и сам, но текст должен быть годным до
    # отправки, чтобы его можно было проверить тестом.
    return api.clamp("\n\n".join(body))


# --- отправка ---------------------------------------------------------------

async def post_draft(draft: DraftRow, *, dialog_excerpt: str) -> int:
    """Отправляет черновик администратору. Возвращает `message_id` в Telegram.

    Ошибка Bot API наружу не глотается: без `message_id` демону нельзя вызывать
    `store.link_draft_message`, а тихо «отправленный» черновик, которого нет в
    чате, — это потерянный лид, о котором никто не узнает.
    """
    text = render_draft(draft, dialog_excerpt=dialog_excerpt)
    message_id = await api.send_message(text, reply_markup=keyboard_for(draft),
                                        config=config())
    log.info("черновик %s (%s) отправлен администратору сообщением %s; пациент: %s",
             draft.id, draft.kind, message_id, pii.scrub(dialog_excerpt))
    return message_id


async def notify(text: str, *, level: Literal["info", "warn", "alarm"] = "info") -> None:
    """Служебное уведомление администратору. Текст идёт через `pii.scrub()`.

    Здесь скраббер обязателен, в отличие от черновика: сюда попадают цитаты из
    переписки внутри сообщений об ошибках, а решать по ним ничего не нужно.

    Исключение наружу не выпускается: `notify` вызывают в том числе из обработки
    сбоя, и падение уведомителя превратило бы одну проблему в две. `info`
    отправляется без звука — ночные «поллер жив» не должны будить.
    """
    mark = LEVEL_MARK.get(level)
    if mark is None:
        log.warning("неизвестный уровень уведомления %r, отправляю как warn", level)
        mark = LEVEL_MARK["warn"]
    body = api.clamp(mark + pii.scrub(text))
    try:
        await api.send_message(body, silent=(level == "info"), config=config())
    except api.TelegramApiError as exc:
        # Текст уже вымаран в api; redact повторно — на случай, если сюда
        # когда-нибудь попадёт исключение другого происхождения.
        log.error("уведомление уровня %s не доставлено: %s", level,
                  api.redact(str(exc)))


async def ack(action: Action, *, text: str | None = None,
              show_alert: bool = False) -> bool:
    """Гасит «часики» на кнопке. Возвращает False, если погасить не удалось.

    Отдельно от `parse_callback` по двум причинам. Разбор обязан быть
    бессетевым: апдейт из чужого чата или от устаревшей кнопки не должен
    вызывать HTTP. И текст ответа зависит от того, что демон УЖЕ сделал —
    «Отправлено» и «Черновик уже был отправлен» это разные сообщения, а панель
    об этом не знает.

    Ошибка не бросается: Telegram отвечает `query is too old` на всё, что
    пролежало больше минуты, и это не причина ронять обработку решения, которое
    к тому моменту уже применено.
    """
    query_id = action.payload.get("callback_query_id")
    if not query_id:
        return False
    try:
        return await api.answer_callback_query(str(query_id), text=text,
                                               show_alert=show_alert,
                                               config=config())
    except api.TelegramApiError as exc:
        log.warning("не удалось погасить кнопку: %s", api.redact(str(exc)))
        return False


# --- разбор апдейтов --------------------------------------------------------

def _unknown(reason: str, **extra: Any) -> Action:
    payload: dict[str, Any] = {"reason": reason}
    payload.update(extra)
    return Action(kind="unknown", draft_id=None, chat_id=None, payload=payload)


def _actor(node: Any) -> str:
    """Кто нажал. Для `resolve_draft(by=…)` и для аудита."""
    if not isinstance(node, dict):
        return "неизвестный"
    who = node.get("from")
    if not isinstance(who, dict):
        return "неизвестный"
    username = who.get("username")
    if isinstance(username, str) and username:
        return "@" + username
    name = " ".join(str(who[key]) for key in ("first_name", "last_name")
                    if isinstance(who.get(key), str) and who[key])
    return name or f"id{who.get('id', '?')}"


def _is_our_chat(chat: Any) -> bool:
    """Апдейт из нашего чата администраторов?

    Бота могут добавить в другую группу или написать ему в личку — тогда
    посторонний человек нажмёт кнопку в переслаyнном сообщении и отправит ответ
    пациенту. Поэтому чат сверяется с `TELEGRAM_CHAT_ID` до разбора данных.
    """
    if not isinstance(chat, dict):
        return False
    expected = config().chat_id
    if expected.startswith("@"):
        username = chat.get("username")
        return isinstance(username, str) and "@" + username == expected
    return str(chat.get("id")) == expected


async def parse_callback(update: dict) -> Action:
    """Превращает апдейт Telegram в `Action`. Не бросает исключений.

    Устойчивость здесь не формальность: `getUpdates` отдаёт всё, что накопилось,
    включая апдейты от кнопок предыдущей версии бота, сообщения из группы,
    сервисные события и просто мусор. Единственное исключение, которое пройдёт
    наружу, — `TelegramConfigError`: отсутствие переменных окружения это не
    мусор во входных данных, а неработающая установка, и заметать её в
    «unknown» значит получить демон, который бодро крутится и ничего не делает.
    """
    try:
        return _parse(update)
    except api.TelegramConfigError:
        raise
    except Exception as exc:  # noqa: BLE001 — чужой апдейт не роняет демон
        log.warning("апдейт не разобран (%s), пропускаю", type(exc).__name__)
        return _unknown(f"исключение при разборе: {type(exc).__name__}")


def _parse(update: dict) -> Action:
    if not isinstance(update, dict):
        return _unknown("апдейт не словарь")

    query = update.get("callback_query")
    if isinstance(query, dict):
        return _parse_button(query)

    message = update.get("message")
    if isinstance(message, dict):
        return _parse_reply(message)

    return _unknown("апдейт без callback_query и message")


def _parse_button(query: dict) -> Action:
    query_id = query.get("id")
    message = query.get("message")
    common: dict[str, Any] = {"by": _actor(query)}
    if isinstance(query_id, str) and query_id:
        common["callback_query_id"] = query_id
    if isinstance(message, dict) and isinstance(message.get("message_id"), int):
        common["tg_message_id"] = message["message_id"]

    if not _is_our_chat((message or {}).get("chat")):
        # `callback_query_id` намеренно НЕ отдаётся: гасить кнопку в чужом чате
        # значит отвечать постороннему человеку от имени клиники.
        return _unknown("нажатие не из чата администраторов", by=common["by"])

    kind, draft_id, variant, reason = _unpack(query.get("data"))
    if kind == "unknown":
        log.warning("нажата кнопка, которую не удалось разобрать: %s", reason)
        return _unknown(reason, **common)

    payload = dict(common)
    if kind == "edit":
        # Кнопка не открывает поле ввода. Она лишь просит демон подсказать, что
        # правку надо прислать ответом на это сообщение (`EDIT_HINT`).
        payload["stage"] = "prompt"
    elif kind == "takeover":
        payload["until"] = None          # до отмены
    elif kind == "pause":
        payload["variant"] = variant
        payload["until"] = pause_until(str(variant))
    elif kind == "resume":
        # Снятие выражается моментом в прошлом — см. `store._set_until`.
        payload["until"] = hours.now() - timedelta(seconds=1)

    return Action(kind=kind, draft_id=draft_id, chat_id=None, payload=payload)


def _parse_reply(message: dict) -> Action:
    """«Правка»: ответ администратора на сообщение бота с черновиком.

    Приватность бота в группе оставлена включённой, поэтому до нас доходят
    только команды и ответы на наши собственные сообщения — обычное обсуждение
    пациентов между администраторами бот не видит и видеть не должен.
    """
    if not _is_our_chat(message.get("chat")):
        return _unknown("сообщение не из чата администраторов")

    reply_to = message.get("reply_to_message")
    if not isinstance(reply_to, dict):
        return _unknown("сообщение не является ответом на черновик")

    target = reply_to.get("message_id")
    if not isinstance(target, int):
        return _unknown("в ответе нет message_id черновика")

    who = reply_to.get("from")
    if isinstance(who, dict) and who.get("is_bot") is False:
        return _unknown("это ответ на сообщение человека, а не на черновик бота")

    text = message.get("text")
    if not isinstance(text, str) or not text.strip():
        # Фото снимка или голосовое — не правка. Демон такое пропустит, а
        # администратор увидит, что черновик остался в очереди.
        return _unknown("ответ без текста")
    text = text.strip()
    if text.startswith("/"):
        return _unknown(f"команда {text.split()[0][:32]!r}, а не правка")

    # Текст правки — ОРИГИНАЛ: он уйдёт пациенту. В лог идёт только длина:
    # правка администратора содержит те же ПД, что и переписка.
    log.info("правка ответом на сообщение %s, %d символов", target, len(text))
    return Action(kind="edit", draft_id=None, chat_id=None,
                  payload={"stage": "text", "text": text, "tg_message_id": target,
                           "by": _actor(message),
                           "message_id": message.get("message_id")})
