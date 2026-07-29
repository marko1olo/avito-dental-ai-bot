# -*- coding: utf-8 -*-
"""Тесты состояния. Запуск: python brain/tests/test_store.py

Порядок разделов отражает цену ошибки, а не порядок методов в контракте.

1. ДЕДУП. Повторная отправка пациенту того же ответа — единственная ошибка
   этого модуля, которую видит пациент. Поэтому проверяется не только «второй
   mark_seen вернул False», но и «второго ряда в таблице не появилось», и то же
   самое из двух потоков с разными соединениями к одному файлу: гарантию даёт
   UNIQUE в схеме, а не порядок вызовов.
2. КОНКУРЕНТНОСТЬ. Поллер, панель Telegram и отправщик пишут в один файл.
   «database is locked» здесь — не флаки-тест, а потерянное сообщение.
3. 152-ФЗ. Проверяется, что сырой номер в базу не проходит ни через API, ни
   в байтах файла.
4. Черновики и аудит — обычная функциональная проверка цикла.

Сеть и секреты не нужны: база создаётся во временном каталоге и удаляется в
finally. Временный каталог удаляется вместе с -wal и -shm.
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gate"))

import hours  # noqa: E402
from store.db import FOREVER, Store  # noqa: E402

NOW = datetime(2026, 8, 5, 14, 0, tzinfo=hours.tz())      # среда, окно записи открыто
RAW_PHONE = "+79271234567"                                 # в базу попасть не должен
GOOD_HASH = "a1b2c3d4e5f60718"                             # 16 hex — как у pii.phone_hash


class Checks:
    """Счётчик проверок. Печатает каждую строкой, копит провалы, считает итог."""

    def __init__(self) -> None:
        self.total = 0
        self.failures: list[str] = []

    def ok(self, label: str, condition: bool, detail: str = "") -> bool:
        self.total += 1
        if not condition:
            self.failures.append(f"{label}{': ' + detail if detail else ''}")
        print(f"  {'ок  ' if condition else 'ФЕЙЛ'} {label}"
              f"{'' if condition or not detail else '  -- ' + detail}")
        return condition

    def eq(self, label: str, got: object, want: object) -> bool:
        return self.ok(label, got == want, f"ждали {want!r}, получили {got!r}")

    def raises(self, label: str, exc: type[BaseException], fn, *args, **kwargs) -> bool:
        try:
            fn(*args, **kwargs)
        except exc:
            return self.ok(label, True)
        except BaseException as other:  # noqa: BLE001 - тип и есть предмет проверки
            return self.ok(label, False, f"ждали {exc.__name__}, получили "
                                         f"{type(other).__name__}: {other}")
        return self.ok(label, False, f"ждали {exc.__name__}, исключения не было")

    def section(self, title: str) -> None:
        print(f"\n--- {title} ---")


def check_open(c: Checks, store: Store) -> None:
    c.section("открытие базы")
    c.eq("journal_mode = WAL", store.journal_mode, "wal")

    # Белый ящик намеренно: foreign_keys — настройка соединения, и проверить, что
    # она включена именно у соединения Store, через публичное API невозможно —
    # queue_draft всегда создаёт диалог сам.
    c.eq("PRAGMA foreign_keys включён на соединении Store",
         store._conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
    c.raises("внешний ключ реально запрещает черновик без диалога",
             sqlite3.IntegrityError, store._conn.execute,
             "INSERT INTO drafts(chat_id, text, kind, reason, status, created_at) "
             "VALUES('нет-такого-чата', 'т', 'k', 'r', 'pending', ?)",
             (NOW.isoformat(),))
    c.eq("user_version проставлена", store._conn.execute(
        "PRAGMA user_version").fetchone()[0], 2)


def check_dedup(c: Checks, store: Store) -> None:
    c.section("дедуп")
    c.eq("первое сообщение — новое", store.mark_seen("msg-1", "chat-A", NOW), True)
    c.eq("повторный mark_seen с тем же external_id -> False",
         store.mark_seen("msg-1", "chat-A", NOW), False)
    c.eq("повтор с другим chat_id и временем — всё равно False",
         store.mark_seen("msg-1", "chat-Z", NOW + timedelta(minutes=5)), False)
    c.eq("второго ряда в seen не появилось", store._conn.execute(
        "SELECT COUNT(*) FROM seen WHERE external_id = 'msg-1'").fetchone()[0], 1)
    c.eq("seen() видит зарегистрированное", store.seen("msg-1"), True)
    c.eq("seen() не видит незнакомое", store.seen("msg-нет"), False)
    c.eq("другое сообщение того же чата — новое",
         store.mark_seen("msg-2", "chat-A", NOW + timedelta(minutes=1)), True)
    c.ok("mark_seen сам создал диалог", store.dialog("chat-A") is not None)
    c.raises("naive datetime отвергается", ValueError,
             store.mark_seen, "msg-naive", "chat-A", datetime(2026, 8, 5, 14, 0))


def check_dialog(c: Checks, store: Store) -> None:
    c.section("диалоги")
    store.touch_dialog("chat-B", patient_message_at=NOW)
    row = store.dialog("chat-B")
    c.ok("touch_dialog создал диалог", row is not None)
    assert row is not None
    c.eq("метка пациента записана", row.patient_last_message_at, NOW)
    c.ok("нашего сообщения ещё не было", row.our_last_message_at is None)
    c.eq("счётчик сообщений пациента", row.patient_messages, 1)

    store.touch_dialog("chat-B", our_message_at=NOW + timedelta(minutes=2))
    row = store.dialog("chat-B")
    assert row is not None
    c.eq("наша метка записана", row.our_last_message_at, NOW + timedelta(minutes=2))
    c.eq("метка пациента не потерялась", row.patient_last_message_at, NOW)
    c.eq("счётчик наших сообщений", row.our_messages, 1)

    # Пачка из поллера может прийти не по порядку; сдвиг метки назад превратил бы
    # живой диалог в «пациент замолчал» и вызвал дожим поверх ответа пациента.
    store.touch_dialog("chat-B", patient_message_at=NOW - timedelta(hours=3))
    row = store.dialog("chat-B")
    assert row is not None
    c.eq("метка пациента не сдвинулась назад", row.patient_last_message_at, NOW)
    c.eq("но сообщение посчитано", row.patient_messages, 2)

    store.touch_dialog("chat-C")
    c.ok("touch_dialog без аргументов регистрирует чат",
         store.dialog("chat-C") is not None)
    c.ok("незнакомый чат -> None", store.dialog("chat-нет") is None)


def check_ai_gate(c: Checks, store: Store) -> None:
    c.section("перехват и пауза ИИ")
    c.eq("незнакомый чат: ИИ работает", store.is_ai_active("chat-нет", NOW), True)

    store.touch_dialog("chat-P", patient_message_at=NOW)
    c.eq("свежий диалог: ИИ работает", store.is_ai_active("chat-P", NOW), True)

    store.set_ai_paused("chat-P", NOW + timedelta(hours=1))
    c.eq("пауза на час: сейчас молчим", store.is_ai_active("chat-P", NOW), False)
    c.eq("на границе срока молчим",
         store.is_ai_active("chat-P", NOW + timedelta(minutes=59)), False)
    c.eq("срок истёк -> ИИ снова работает",
         store.is_ai_active("chat-P", NOW + timedelta(hours=2)), True)
    c.eq("ровно в момент истечения -> работает",
         store.is_ai_active("chat-P", NOW + timedelta(hours=1)), True)

    store.set_ai_paused("chat-P", NOW - timedelta(seconds=1))
    c.eq("снятие паузы моментом в прошлом", store.is_ai_active("chat-P", NOW), True)

    store.set_ai_paused("chat-P", None)
    c.eq("пауза совсем (None): сейчас молчим", store.is_ai_active("chat-P", NOW), False)
    c.eq("пауза совсем: через десять лет тоже молчим",
         store.is_ai_active("chat-P", NOW + timedelta(days=3650)), False)
    row = store.dialog("chat-P")
    assert row is not None
    c.eq("бессрочность хранится датой-стражем, а не NULL", row.ai_paused_until, FOREVER)

    store.set_takeover("chat-T", NOW + timedelta(minutes=30))
    c.ok("перехват создал диалог сам", store.dialog("chat-T") is not None)
    c.eq("перехвачено: ИИ молчит", store.is_ai_active("chat-T", NOW), False)
    c.eq("перехват истёк: ИИ снова работает",
         store.is_ai_active("chat-T", NOW + timedelta(hours=1)), True)

    # Перехват и пауза независимы: снятие одного не включает бота, пока держит второй.
    store.set_takeover("chat-T", None)
    store.set_ai_paused("chat-T", NOW + timedelta(hours=1))
    store.set_ai_paused("chat-T", NOW - timedelta(seconds=1))
    c.eq("пауза снята, но перехват держит", store.is_ai_active("chat-T", NOW), False)
    store.set_takeover("chat-T", NOW - timedelta(seconds=1))
    c.eq("снято и то и другое -> ИИ работает", store.is_ai_active("chat-T", NOW), True)

    c.raises("naive moment отвергается", ValueError,
             store.is_ai_active, "chat-T", datetime(2026, 8, 5, 14, 0))


def check_phone(c: Checks, store: Store) -> None:
    c.section("152-ФЗ: только хэш")
    store.capture_phone("chat-B", GOOD_HASH)
    row = store.dialog("chat-B")
    assert row is not None
    c.eq("хэш записан", row.phone_hash, GOOD_HASH)
    c.eq("факт получения телефона виден", row.phone_captured, True)

    c.raises("сырой номер с плюсом отвергнут", ValueError,
             store.capture_phone, "chat-B", RAW_PHONE)
    c.raises("сырой номер без плюса отвергнут", ValueError,
             store.capture_phone, "chat-B", "89271234567")
    c.raises("десять цифр отвергнуты по длине", ValueError,
             store.capture_phone, "chat-B", "9271234567")
    c.raises("не-hex строка нужной длины отвергнута", ValueError,
             store.capture_phone, "chat-B", "z" * 16)
    row = store.dialog("chat-B")
    assert row is not None
    c.eq("после отказов хэш не испорчен", row.phone_hash, GOOD_HASH)

    # Схема, а не только проверка в коде: обход API через прямой SQL тоже падает.
    c.raises("CHECK в схеме не даёт записать номер прямым SQL",
             sqlite3.IntegrityError, store._conn.execute,
             "UPDATE dialogs SET phone_hash = ? WHERE chat_id = 'chat-B'", (RAW_PHONE,))


def check_drafts(c: Checks, store: Store) -> int:
    c.section("цикл черновика")
    first = store.queue_draft("chat-D", "Осмотр бесплатный, подберём время.",
                              kind="price", reason="рисковый маркер (price)")
    second = store.queue_draft("chat-D", "Ортодонт принимает по записи.",
                               kind="no_quote_topic", reason="тема без цены")
    c.ok("queue_draft вернул id", isinstance(first, int) and first > 0)
    row = store.draft(first)
    c.ok("черновик читается по id", row is not None)
    assert row is not None
    c.eq("статус нового черновика", row.status, "pending")
    c.eq("kind сохранён", row.kind, "price")
    c.eq("outgoing_text до правки — исходный текст", row.outgoing_text, row.text)

    pending = store.pending_drafts()
    c.eq("оба черновика в очереди", [d.id for d in pending], [first, second])
    c.eq("limit режет очередь", len(store.pending_drafts(limit=1)), 1)

    store.link_draft_message(first, 5001)
    found = store.draft_by_tg_message(5001)
    c.ok("черновик находится по сообщению Telegram", found is not None)
    assert found is not None
    c.eq("это тот самый черновик", found.id, first)
    c.ok("неизвестное сообщение -> None", store.draft_by_tg_message(999999) is None)

    store.link_draft_message(first, 5001)
    c.ok("повторная привязка того же сообщения — no-op", True)
    c.raises("привязка второго сообщения к тому же черновику запрещена",
             ValueError, store.link_draft_message, first, 5002)
    c.raises("привязка занятого сообщения к другому черновику запрещена",
             ValueError, store.link_draft_message, second, 5001)
    c.raises("привязка к несуществующему черновику", LookupError,
             store.link_draft_message, 10 ** 6, 5003)

    resolved = store.resolve_draft(first, action="sent", by="admin")
    c.eq("статус после отправки", resolved.status, "sent")
    c.ok("время решения записано", resolved.resolved_at is not None)
    c.eq("кто решил", resolved.resolved_by, "admin")
    c.eq("отправляется текст черновика", resolved.outgoing_text,
         "Осмотр бесплатный, подберём время.")
    c.eq("решённый черновик ушёл из очереди",
         [d.id for d in store.pending_drafts()], [second])

    again = store.resolve_draft(first, action="sent", by="admin2")
    c.eq("повторный тот же callback идемпотентен: время решения не переписано",
         again.resolved_at, resolved.resolved_at)
    c.eq("и автор решения не подменён", again.resolved_by, "admin")
    c.raises("сменить принятое решение нельзя", ValueError,
             store.resolve_draft, first, action="ignored")

    edited = store.resolve_draft(second, action="edited",
                                 final_text="Ортодонт принимает во вторник и четверг.",
                                 by="admin")
    c.eq("правка сохранена", edited.outgoing_text,
         "Ортодонт принимает во вторник и четверг.")
    c.eq("исходный текст черновика сохранён рядом с правкой", edited.text,
         "Ортодонт принимает по записи.")
    c.eq("очередь пуста", store.pending_drafts(), [])

    third = store.queue_draft("chat-D", "Третий", kind="unknown", reason="не распознано")
    c.raises("правка без текста запрещена", ValueError,
             store.resolve_draft, third, action="edited")
    c.raises("правка пробелами запрещена", ValueError,
             store.resolve_draft, third, action="edited", final_text="   ")
    c.raises("неизвестное решение запрещено", ValueError,
             store.resolve_draft, third, action="отправить")
    c.raises("решение по несуществующему черновику", LookupError,
             store.resolve_draft, 10 ** 6, action="sent")
    c.raises("пустой черновик не ставится в очередь", ValueError,
             store.queue_draft, "chat-D", "   ", kind="k", reason="r")
    c.ok("после отказов черновик остался pending",
         (store.draft(third) or Store).status == "pending")
    store.resolve_draft(third, action="ignored")
    c.eq("игнор не оставляет текста для отправки", store.draft(third).final_text
         if store.draft(third) else "нет ряда", None)
    return first


def check_audit(c: Checks, store: Store) -> None:
    c.section("аудит")
    store.audit("bot_started")
    store.audit("draft_queued", chat_id="chat-D",
                payload={"kind": "price", "amounts": [3800, 5000], "тема": "кариес"})
    rows = store.recent_audit(limit=10)
    c.ok("аудит читается", len(rows) >= 2)
    c.eq("свежие первыми", rows[0].event, "draft_queued")
    c.eq("chat_id сохранён", rows[0].chat_id, "chat-D")
    c.eq("payload вернулся тем же словарём", rows[0].payload,
         {"kind": "price", "amounts": [3800, 5000], "тема": "кариес"})
    c.eq("событие без payload читается пустым словарём", rows[1].payload, {})
    c.ok("событие без chat_id допустимо", rows[1].chat_id is None)
    c.eq("limit работает", len(store.recent_audit(limit=1)), 1)
    c.raises("событие без имени запрещено", ValueError, store.audit, "  ")

    # datetime в payload не должен ронять запись аудита: событие важнее типа поля.
    store.audit("llm_failed", payload={"at": NOW, "failure": "timeout"})
    c.eq("datetime в payload сериализован, а не уронил аудит",
         store.recent_audit(limit=1)[0].payload["failure"], "timeout")


def check_concurrency(c: Checks, path: Path) -> None:
    c.section("два соединения к одному файлу")
    shared = [f"race-{i}" for i in range(25)]
    per_thread = 150
    errors: list[str] = []
    wins: dict[str, list[str]] = {"A": [], "B": []}
    barrier = threading.Barrier(2)

    # Свои чаты, не chat-A/chat-B из предыдущих разделов: 150 записей в общий с
    # ними диалог сбили бы счётчики, которые проверяет check_reopen.
    def chat_of(tag: str) -> str:
        return f"chat-conc-{tag}"

    def worker(tag: str) -> None:
        try:
            with Store(path) as store:
                barrier.wait(timeout=15)
                for i in range(per_thread):
                    store.mark_seen(f"conc-{tag}-{i}", chat_of(tag), NOW)
                    if i < len(shared):
                        if store.mark_seen(shared[i], "chat-race", NOW):
                            wins[tag].append(shared[i])
                    store.touch_dialog(chat_of(tag), patient_message_at=NOW)
                    store.audit("polled", chat_id=chat_of(tag), payload={"i": i})
        except BaseException as exc:  # noqa: BLE001 - падение и есть предмет проверки
            errors.append(f"{tag}: {type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=worker, args=(tag,)) for tag in ("A", "B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    c.ok("ни одно соединение не упало (в том числе на блокировке)",
         not errors, "; ".join(errors))
    c.ok("оба потока дошли до конца", all(not t.is_alive() for t in threads))

    with Store(path) as store:
        c.eq("каждый спорный external_id достался ровно одному потоку",
             sorted(wins["A"] + wins["B"]), sorted(shared))
        c.eq("пересечения между потоками нет",
             set(wins["A"]) & set(wins["B"]), set())
        c.eq("рядов в seen по спорным id ровно столько, сколько id",
             store._conn.execute(
                 "SELECT COUNT(*) FROM seen WHERE chat_id = 'chat-race'").fetchone()[0],
             len(shared))
        for tag in ("A", "B"):
            row = store.dialog(chat_of(tag))
            c.eq(f"счётчик диалога {chat_of(tag)} не потерял ни одной записи",
                 row.patient_messages if row else None, per_thread)
        c.eq("аудит из двух потоков не потерялся", store._conn.execute(
            "SELECT COUNT(*) FROM audit WHERE event = 'polled'").fetchone()[0],
            per_thread * 2)


def check_no_raw_phone(c: Checks, path: Path) -> None:
    c.section("в файле базы нет сырых номеров")
    blobs = b""
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            blobs += candidate.read_bytes()
    digits = RAW_PHONE.lstrip("+").encode()
    c.ok("байт файла базы не содержит номер, который отдавали в capture_phone",
         digits not in blobs and RAW_PHONE.encode() not in blobs)
    c.ok("файл базы прочитан целиком", len(blobs) > 0, f"прочитано {len(blobs)} байт")


def check_reopen(c: Checks, path: Path, draft_id: int) -> None:
    c.section("переоткрытие существующей базы")
    with Store(path) as store:
        c.eq("схема применилась повторно, дедуп на месте", store.seen("msg-1"), True)
        row = store.dialog("chat-B")
        c.ok("диалог на месте", row is not None)
        assert row is not None
        c.eq("хэш телефона на месте", row.phone_hash, GOOD_HASH)
        c.eq("счётчики на месте", row.patient_messages, 2)
        c.eq("бессрочная пауза на месте", (store.dialog("chat-P") or row).ai_paused_until,
             FOREVER)
        draft = store.draft(draft_id)
        c.ok("черновик на месте", draft is not None)
        assert draft is not None
        c.eq("решение по черновику на месте", draft.status, "sent")
        c.eq("привязка к Telegram на месте", draft.tg_message_id, 5001)
        c.ok("аудит на месте", any(a.event == "draft_queued"
                                  for a in store.recent_audit(limit=500)))
        c.eq("повторное открытие не сбросило user_version", store._conn.execute(
            "PRAGMA user_version").fetchone()[0], 2)
        c.eq("новое сообщение в старой базе регистрируется",
             store.mark_seen("msg-после-переоткрытия", "chat-A", NOW), True)


def run() -> int:
    c = Checks()
    workdir = Path(tempfile.mkdtemp(prefix="avito-store-test-"))
    path = workdir / "state.sqlite3"
    try:
        with Store(path) as store:
            check_open(c, store)
            check_dedup(c, store)
            check_dialog(c, store)
            check_ai_gate(c, store)
            check_phone(c, store)
            draft_id = check_drafts(c, store)
            check_audit(c, store)
            check_no_raw_phone(c, path)
        check_concurrency(c, path)
        check_reopen(c, path, draft_id)
    finally:
        # Мусор за собой убираем сразу: -wal и -shm лежат рядом с файлом базы.
        shutil.rmtree(workdir, ignore_errors=True)

    print(f"\nИТОГ: {c.total - len(c.failures)}/{c.total}")
    for failure in c.failures:
        print(f"  {failure}")
    return 1 if c.failures else 0


if __name__ == "__main__":
    raise SystemExit(run())
