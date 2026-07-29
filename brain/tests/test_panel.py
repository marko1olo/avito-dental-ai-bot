# -*- coding: utf-8 -*-
"""Панель администратора: кнопки, разбор нажатий, текст черновика.

Сети нет: `api.send_message` / `api.answer_callback_query` подменяются на
записывающие заглушки. Токен и чат — фиктивные, через окружение.

Три проверки здесь важнее остальных.

`callback_data` ≤ 64 байт. Telegram не обрезает длинные данные — он отклоняет
сообщение с клавиатурой целиком, и черновик уходит без кнопок. Обнаружилось бы
это на живом лиде с большим draft_id, то есть через месяцы работы.

Мусорный апдейт не роняет разбор. Апдейт из чужого чата, устаревшая кнопка,
битый JSON — всё это приходит в реальности, и любое исключение здесь остановило
бы обработку решений администратора целиком.

Текст пациента в черновике НЕ скраблен. Скраббер стоит на логах, а не на том,
что видит человек: по «перезвоните на 999 12 34 56» администратор звонит, по
«перезвоните на [телефон]» — идёт искать чат в Авито руками.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

_BRAIN = Path(__file__).resolve().parents[1]
for _extra in (str(_BRAIN), str(_BRAIN / "gate"), str(_BRAIN / "tg")):
    if _extra not in sys.path:
        sys.path.insert(0, _extra)

# Фиктивная конфигурация до импорта панели: `load_config` обязана видеть
# переменные при первом обращении. Токен намеренно имеет форму настоящего
# (цифры:буквы) — иначе проверка «токен не течёт в лог» ничего не проверяет,
# потому что `api.redact` ищет именно эту форму.
FAKE_TOKEN = "123456789:AAFfakeTOKENforTESTSonly_notARealOne12345"
FAKE_CHAT = "-1001234567890"
os.environ["TELEGRAM_BOT_TOKEN"] = FAKE_TOKEN
os.environ["TELEGRAM_CHAT_ID"] = FAKE_CHAT

import hours  # noqa: E402
from tg import api, panel  # noqa: E402

NOW = datetime(2026, 7, 29, 14, 30, tzinfo=hours.tz())

# Номер пациента для проверки «оригинал не скраблен». Не из clinic-facts.json:
# свои номера панель не скрабит по определению, и такой пример прошёл бы тест,
# ничего не проверив.
PATIENT_PHONE = "+7 917 333-21-09"


@dataclass(frozen=True)
class FakeDraft:
    """Двойник DraftRow. Настоящий требует базу, а панели нужны только поля."""

    id: int = 42
    chat_id: str = "chat-abc"
    text: str = "Здравствуйте. Осмотр бесплатный, приходите."
    kind: str = "price"
    reason: str = "вето: выдуманная сумма"
    status: str = "pending"
    created_at: datetime = NOW
    resolved_at: datetime | None = None
    resolved_by: str | None = None
    final_text: str | None = None
    tg_message_id: int | None = None

    @property
    def is_pending(self) -> bool:
        return self.status == "pending"

    @property
    def outgoing_text(self) -> str:
        return self.final_text if self.final_text is not None else self.text


class Checks:
    """Тот же протокол, что в остальных сюитах проекта."""

    def __init__(self) -> None:
        self.passed = 0
        self.failed: list[str] = []

    def section(self, title: str) -> None:
        print(f"\n--- {title} ---")

    def ok(self, name: str, condition: bool) -> None:
        if condition:
            self.passed += 1
        else:
            self.failed.append(name)
            print(f"  ПРОВАЛ: {name}")

    def eq(self, name: str, got: object, want: object) -> None:
        if got == want:
            self.passed += 1
        else:
            self.failed.append(name)
            print(f"  ПРОВАЛ: {name}: ждали {want!r}, получили {got!r}")

    def raises(self, name: str, exc: type[BaseException], fn, *args, **kwargs) -> None:
        try:
            fn(*args, **kwargs)
        except exc:
            self.passed += 1
        except BaseException as other:  # noqa: BLE001
            self.failed.append(name)
            print(f"  ПРОВАЛ: {name}: ждали {exc.__name__}, получили {type(other).__name__}")
        else:
            self.failed.append(name)
            print(f"  ПРОВАЛ: {name}: исключения не было")

    @property
    def total(self) -> int:
        return self.passed + len(self.failed)


class Recorder:
    """Записывает вызовы вместо HTTP. Возвращает то, что вернул бы Telegram."""

    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.acks: list[dict] = []
        self.next_message_id = 5000

    async def send_message(self, text: str, *, reply_markup: dict | None = None,
                           reply_to_message_id: int | None = None,
                           config=None, **kwargs) -> int:
        self.next_message_id += 1
        self.messages.append({"text": text, "reply_markup": reply_markup,
                              "reply_to": reply_to_message_id})
        return self.next_message_id

    async def answer_callback_query(self, query_id: str, *, text: str | None = None,
                                    show_alert: bool = False, config=None,
                                    **kwargs) -> bool:
        self.acks.append({"query_id": query_id, "text": text, "alert": show_alert})
        return True


def install(recorder: Recorder) -> None:
    api.send_message = recorder.send_message           # type: ignore[assignment]
    api.answer_callback_query = recorder.answer_callback_query  # type: ignore[assignment]


def button(query_id: str, data: str, *, chat_id: str = FAKE_CHAT,
           username: str = "admin_anna") -> dict:
    """Апдейт нажатия кнопки в том виде, в каком его отдаёт getUpdates."""
    return {
        "update_id": 900,
        "callback_query": {
            "id": query_id,
            "from": {"id": 777, "username": username, "first_name": "Анна"},
            "message": {"message_id": 5001, "chat": {"id": int(chat_id)}},
            "data": data,
        },
    }


def reply(text: str, *, reply_to: int = 5001, chat_id: str = FAKE_CHAT) -> dict:
    """Апдейт правки: ответ на сообщение бота в группе."""
    return {
        "update_id": 901,
        "message": {
            "message_id": 6001,
            "chat": {"id": int(chat_id)},
            "from": {"id": 777, "username": "admin_anna"},
            "text": text,
            "reply_to_message": {"message_id": reply_to,
                                 "from": {"id": 1, "is_bot": True}},
        },
    }


# --- проверки ---------------------------------------------------------------

def check_callback_size(c: Checks) -> None:
    c.section("длина callback_data")

    # Живой draft_id вырастет до тысяч за годы, но проверять надо заведомо
    # худший случай: AUTOINCREMENT не переиспользует id, и предел — 2^63.
    for draft_id in (0, 1, 42, 10 ** 6, 10 ** 12, 2 ** 63 - 1):
        worst = max(len(data.encode("utf-8"))
                    for data in panel._all_callback_data(draft_id))
        c.ok(f"draft_id {draft_id}: худшая callback_data {worst} байт "
             f"<= {api.CALLBACK_DATA_LIMIT}", worst <= api.CALLBACK_DATA_LIMIT)

    c.eq("формат данных кнопки", panel.callback_data("send", 42), "d1:snd:42")
    c.eq("пауза несёт вариант", panel.callback_data("pause", 42, variant="morning"),
         "d1:pau:42:am")
    c.raises("неизвестный вид отвергается", ValueError,
             panel.callback_data, "выдумка", 42)
    c.raises("пауза без варианта отвергается", ValueError,
             panel.callback_data, "pause", 42)
    c.raises("вариант у не-паузы отвергается", ValueError,
             panel.callback_data, "send", 42, variant="hour")
    c.raises("отрицательный draft_id отвергается", ValueError,
             panel.callback_data, "send", -1)

    # Клавиатура целиком: Telegram проверяет каждую кнопку, и одна длинная
    # отклоняет всё сообщение.
    keyboard = panel.keyboard_for(FakeDraft(id=10 ** 12))
    datas = [b["callback_data"] for row in keyboard["inline_keyboard"] for b in row]
    c.ok("в клавиатуре все кнопки в пределах лимита",
         all(len(d.encode("utf-8")) <= api.CALLBACK_DATA_LIMIT for d in datas))
    c.eq("кнопок столько же, сколько данных для проверки длины",
         len(datas), len(panel._all_callback_data(10 ** 12)))

    labels = [b["text"] for row in keyboard["inline_keyboard"] for b in row]
    for required in (panel.LABEL_SEND, panel.LABEL_EDIT, panel.LABEL_IGNORE,
                     panel.LABEL_TAKEOVER, panel.LABEL_PAUSE_HOUR,
                     panel.LABEL_PAUSE_MORNING, panel.LABEL_PAUSE_FOREVER):
        c.ok(f"кнопка «{required}» на месте", required in labels)


def check_parsing(c: Checks) -> None:
    c.section("разбор апдейтов")
    run = asyncio.run

    action = run(panel.parse_callback(button("q1", panel.callback_data("send", 42))))
    c.eq("«Отправить» разобрано", action.kind, "send")
    c.eq("draft_id из кнопки", action.draft_id, 42)
    c.eq("кто нажал", action.payload.get("by"), "@admin_anna")
    c.eq("id запроса для гашения кнопки", action.payload.get("callback_query_id"), "q1")
    c.ok("действие требует записи в базу", action.actionable)

    action = run(panel.parse_callback(button("q2", panel.callback_data("ignore", 42))))
    c.eq("«Игнор» разобрано", action.kind, "ignore")

    action = run(panel.parse_callback(button("q3", panel.callback_data("edit", 42))))
    c.eq("«Правка» разобрано", action.kind, "edit")
    c.eq("правка по кнопке — это подсказка", action.payload.get("stage"), "prompt")
    c.ok("подсказка НЕ пишется в базу", not action.actionable)

    action = run(panel.parse_callback(reply("Приходите в четверг к 15:00")))
    c.eq("правка ответом разобрана", action.kind, "edit")
    c.eq("этап правки — текст", action.payload.get("stage"), "text")
    c.eq("текст правки сохранён", action.payload.get("text"),
         "Приходите в четверг к 15:00")
    c.ok("правка текстом пишется в базу", action.actionable)

    for variant in panel.PAUSE_VARIANTS:
        data = panel.callback_data("pause", 42, variant=variant)
        action = run(panel.parse_callback(button(f"qp-{variant}", data)))
        c.eq(f"пауза «{variant}» разобрана", action.kind, "pause")
        c.eq(f"вариант паузы «{variant}» сохранён",
             action.payload.get("variant"), variant)

    action = run(panel.parse_callback(button("q4", panel.callback_data("takeover", 42))))
    c.eq("«Перехватить» разобрано", action.kind, "takeover")
    action = run(panel.parse_callback(button("q5", panel.callback_data("resume", 42))))
    c.eq("«Вернуть ИИ» разобрано", action.kind, "resume")

    # chat_id здесь — это чат АВИТО, и панель его не знает. Подставить сюда id
    # чата Telegram было бы прямой ошибкой: этим ключом адресуется диалог.
    c.eq("chat_id не выдуман из апдейта Telegram", action.chat_id, None)


def check_garbage(c: Checks) -> None:
    c.section("устойчивость к мусору")
    run = asyncio.run

    cases = {
        "пустой апдейт": {},
        "апдейт без callback и без message": {"update_id": 1},
        "кнопка без данных": {"callback_query": {"id": "x", "message": {}}},
        "данные не строка": button("x", None),  # type: ignore[arg-type]
        "чужой формат данных": button("x", "какой-то мусор"),
        "устаревшая версия формата": button("x", "d0:snd:42"),
        "неизвестный код действия": button("x", "d1:xxx:42"),
        "draft_id не число": button("x", "d1:snd:абв"),
        "пауза без варианта": button("x", "d1:pau:42"),
        "неизвестный вариант паузы": button("x", "d1:pau:42:xx"),
        "битый JSON внутри": {"callback_query": "строка вместо объекта"},
        "message без reply_to": {"message": {"chat": {"id": int(FAKE_CHAT)},
                                            "text": "просто болтовня"}},
    }
    for name, update in cases.items():
        action = run(panel.parse_callback(update))
        c.eq(f"{name} -> unknown", action.kind, "unknown")
        c.ok(f"{name}: причина заполнена", bool(action.payload.get("reason")))
        c.ok(f"{name}: записи в базу не требует", not action.actionable)

    # Чужой чат — самая опасная из этих ситуаций: бота могут добавить в другую
    # группу, и посторонний человек нажмёт кнопку в пересланном сообщении.
    action = run(panel.parse_callback(button("x", panel.callback_data("send", 42),
                                            chat_id="-100999888777")))
    c.eq("нажатие из чужого чата отвергнуто", action.kind, "unknown")
    action = run(panel.parse_callback(reply("правка", chat_id="-100999888777")))
    c.eq("правка из чужого чата отвергнута", action.kind, "unknown")

    # Апдейт целиком должен пережить сериализацию в JSON: демон пишет его в
    # аудит, и объект с несериализуемым полем уронил бы запись события.
    c.ok("действие сериализуется в JSON",
         isinstance(json.dumps(action.payload, ensure_ascii=False, default=str), str))


def check_draft_text(c: Checks, recorder: Recorder) -> None:
    c.section("текст черновика")

    draft = FakeDraft(text=f"Перезвоните пациенту на {PATIENT_PHONE}, он ждёт")
    rendered = panel.render_draft(draft, dialog_excerpt=f"Мой номер {PATIENT_PHONE}")

    # Главная проверка модуля. Скраббер стоит на логах, а не на том, что видит
    # человек: по замазанному номеру администратор не позвонит.
    c.ok("номер пациента в черновике НЕ замазан", PATIENT_PHONE in rendered)
    c.ok("замены [телефон] в черновике нет", pii_placeholder() not in rendered)
    c.ok("причина черновика показана", draft.reason in rendered)
    c.ok("подсказка про правку ответом показана", panel.EDIT_HINT in rendered)
    c.ok("вид черновика по-русски", panel.KIND_RU["price"] in rendered)
    c.ok("номер черновика показан", f"#{draft.id}" in rendered)

    # Длинный текст обрезается нами с пометкой, а не Telegram отказом на всё
    # сообщение: 4096 знаков — жёсткий предел API.
    huge = FakeDraft(text="а" * 9000)
    rendered = panel.render_draft(huge, dialog_excerpt="б" * 9000)
    c.ok(f"длинный черновик обрезан до {api.TEXT_LIMIT}",
         len(rendered) <= api.TEXT_LIMIT)

    message_id = asyncio.run(panel.post_draft(draft, dialog_excerpt="Пациент: болит"))
    c.ok("post_draft вернул message_id", isinstance(message_id, int) and message_id > 0)
    c.eq("сообщение ушло одно", len(recorder.messages), 1)
    c.ok("клавиатура приложена",
         bool(recorder.messages[0]["reply_markup"].get("inline_keyboard")))


def pii_placeholder() -> str:
    """Заглушка скраббера. Берётся из pii, а не из строки в тесте: иначе тест
    разойдётся с модулем и перестанет проверять то, ради чего написан."""
    import pii
    return pii.PHONE_PLACEHOLDER


def check_notify(c: Checks, recorder: Recorder) -> None:
    c.section("уведомления")

    before = len(recorder.messages)
    asyncio.run(panel.notify(f"Пациент оставил {PATIENT_PHONE}", level="alarm"))
    c.eq("уведомление отправлено", len(recorder.messages), before + 1)
    text = recorder.messages[-1]["text"]

    # notify идёт в лог администраторов как служебное сообщение, и здесь
    # скраббер обязан работать: это не черновик, по которому звонят.
    c.ok("в уведомлении номер замазан", PATIENT_PHONE not in text)
    c.ok("уровень тревоги виден", panel.LEVEL_MARK["alarm"].strip() in text)
    c.ok("у уведомления нет кнопок", recorder.messages[-1]["reply_markup"] is None)


def check_secrets(c: Checks, recorder: Recorder) -> None:
    c.section("токен не течёт")

    # Всё, что панель могла произвести за прогон, плюс её собственные строки.
    produced = [m["text"] for m in recorder.messages]
    produced += [str(a) for a in recorder.acks]
    produced.append(panel.check_config())
    produced.append(repr(panel.config()))
    produced.append(str(panel.config()))
    produced.append(panel.render_draft(FakeDraft(), dialog_excerpt="текст"))

    for i, text in enumerate(produced):
        c.ok(f"токен не встречается в строке {i}", FAKE_TOKEN not in text)

    # api.redact — единственная защита от тел ошибок Telegram, где токен стоит
    # прямо в URL. Проверяется на форме, которую реально возвращает API.
    leaked = f"POST https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage 401"
    c.ok("redact вымарывает токен из URL", FAKE_TOKEN not in api.redact(leaked))
    c.ok("check_config говорит про лимит callback_data",
         str(api.CALLBACK_DATA_LIMIT) in panel.check_config())


def check_pause(c: Checks) -> None:
    c.section("сроки паузы")

    hour = panel.pause_until("hour", NOW)
    c.eq("пауза на час", hour, NOW + timedelta(hours=1))

    morning = panel.pause_until("morning", NOW)
    c.ok("«до утра» даёт момент в будущем", morning is not None and morning > NOW)
    c.ok("«до утра» не дальше суток с небольшим",
         morning is not None and morning - NOW <= timedelta(hours=48))

    # «Совсем» — это None, и в store это значит бессрочно, а не «снять».
    # Перепутанное чтение здесь вернуло бы бота в диалог, который забрал себе
    # администратор.
    c.eq("«совсем» — это None", panel.pause_until("forever", NOW), None)
    c.raises("неизвестный вариант отвергается", ValueError, panel.pause_until, "нет")


def check_ack(c: Checks, recorder: Recorder) -> None:
    c.section("гашение кнопки")
    run = asyncio.run

    action = run(panel.parse_callback(button("qA", panel.callback_data("send", 7))))
    c.ok("кнопка погашена", run(panel.ack(action, text="Отправляется")))
    c.eq("текст ответа доставлен", recorder.acks[-1]["text"], "Отправляется")

    unknown = run(panel.parse_callback({}))
    c.ok("гасить нечего у мусорного апдейта — False",
         not run(panel.ack(unknown)))

    # Telegram отвечает «query is too old» на всё, что пролежало больше минуты.
    # Ронять из-за этого применённое решение нельзя.
    async def boom(*_args, **_kwargs):
        raise api.TelegramApiError("answerCallbackQuery", "query is too old",
                                   error_code=400)

    api.answer_callback_query = boom  # type: ignore[assignment]
    c.ok("ошибка гашения не бросается наружу", not run(panel.ack(action)))
    api.answer_callback_query = recorder.answer_callback_query  # type: ignore[assignment]


def run() -> int:
    print("панель администратора: кнопки, разбор, черновики")
    c = Checks()
    recorder = Recorder()
    install(recorder)

    check_callback_size(c)
    check_parsing(c)
    check_garbage(c)
    check_draft_text(c, recorder)
    check_notify(c, recorder)
    check_secrets(c, recorder)
    check_pause(c)
    check_ack(c, recorder)

    print(f"\nИТОГ: {c.passed}/{c.total}")
    if c.failed:
        print("провалено:")
        for name in c.failed:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
