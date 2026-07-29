# -*- coding: utf-8 -*-
"""Состояние бота: дедуп, диалоги, черновики, аудит. Один файл SQLite.

Почему состояние вообще выносится из процесса. У Авито нет вебхуков — есть
поллер, который дёргает переписку по расписанию и может быть перезапущен в
любую секунду: обновление, падение, перезагрузка ноутбука, вторая копия,
запущенная руками. Всё, что жило в памяти процесса, при этом теряется, а
переписка — нет: те же сообщения придут снова.

Отсюда главное требование к этому модулю. **Дедупликация — единственное, что
стоит между пациентом и повторной отправкой того же ответа.** Поэтому она
сделана UNIQUE-констрейнтом в схеме (`seen.external_id PRIMARY KEY`), а не
проверкой «сначала SELECT, потом INSERT». Проверка в коде выглядит достаточной
ровно до момента, когда процессов становится два: между SELECT и INSERT
вклинивается второй, оба видят «сообщения ещё не было» и оба отвечают. UNIQUE
в схеме так сломать нельзя — конфликт разрешается внутри одной атомарной
операции SQLite, и `mark_seen` честно возвращает False проигравшему.

Почему в базе нет ни одного номера телефона. Записывается только `phone_hash`
(`pii.phone_hash`, sha256[:16]). Сам номер остаётся в переписке Авито, второй
копии он не требует ни для одной функции бота: чтобы понять «пациент дал
телефон — дожим больше не нужен», достаточно факта, а не значения. Взамен база
перестаёт быть хранилищем персональных данных, и 152-ФЗ упрощается радикально:
нет ПД — нет обязанностей по их защите, нет темы для уведомления РКН, нет
ущерба при утечке самого файла базы. Это осознанное проектное решение, а не
паранойя, и оно закреплено CHECK-констрейнтом в schema.sql: колонка физически
не примет строку, похожую на номер.

Границы ответственности: store не фильтрует ПД. Текст пациента, попадающий в
`audit(payload=...)`, обязан быть прогнан через `pii.scrub()` вызывающим кодом.
Модуль, который «на всякий случай» ещё раз чистит чужие данные своей регуляркой,
создаёт ложное чувство защиты и вторую точку правды.

Конкурентность: WAL + busy timeout. Писателей несколько (поллер, панель
Telegram, отправщик), и в WAL читатели не блокируют писателя, а writer-writer
конфликт разрешается ожиданием, а не ошибкой. Транзакции открываются как
BEGIN IMMEDIATE: в WAL повышение отложенной транзакции с чтения на запись
возвращает SQLITE_BUSY_SNAPSHOT, минуя busy handler, то есть падает мгновенно и
без повторов — а IMMEDIATE честно ждёт свою очередь.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

# Пакет ещё не устанавливается (нет setup/venv-шага в деплое на ноутбук),
# поэтому gate подключается по пути. Заменить на обычный импорт, когда
# появится pyproject.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gate"))
import hours  # noqa: E402

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

BUSY_TIMEOUT_SECONDS = 10.0

DRAFT_ACTIONS: frozenset[str] = frozenset({"sent", "edited", "ignored", "expired"})
DRAFT_PENDING = "pending"

# Виды строк очереди отправки. `manual` — ответ, поставленный руками через
# служебный скрипт (восстановление после зависшей отправки), а не решением бота.
OUTBOX_KINDS: frozenset[str] = frozenset({"reply", "followup", "manual"})

# Потолок дожимов ДЛЯ ОТБОРА КАНДИДАТОВ, а не для решения. Настоящий предел
# живёт в followup.MAX_FOLLOWUPS; здесь заведомо не меньшее число, чтобы SQL
# отсекал только безнадёжное, а решение осталось за одним модулем. Импортировать
# followup сюда нельзя: store не должен зависеть от политики диалога.
MAX_FOLLOWUPS_GUARD = 2

# «Пауза совсем» и «перехват до отмены» — это срок, а не отсутствие срока: NULL
# уже занят состоянием «никто не перехватывал». Дата-страж делает все сравнения
# однотипными (moment < until) и не требует отдельной ветки в SQL.
FOREVER = datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=hours.tz())

_PHONE_HASH_LENGTH = (16, 64)


# --- строки таблиц ----------------------------------------------------------

@dataclass(frozen=True)
class DialogRow:
    """Диалог целиком.

    Имена полей намеренно совпадают с `followup.DialogState` там, где смысл тот
    же (`patient_last_message_at`, `our_last_message_at`, `followups_sent`,
    `last_followup_at`, `phone_captured`), чтобы роутер собирал состояние дожима
    без переименований. Одно поле не совпадает и совпасть не может:
    `DialogState.human_took_over` — это bool «сейчас», а здесь хранится СРОК
    (`takeover_until`), потому что перехват истекает сам. Переводит одно в другое
    `takeover_active(moment)`: боту нужен ответ на конкретный момент, а не флаг,
    который кто-то обязан не забыть снять.
    """

    chat_id: str
    first_seen_at: datetime
    patient_last_message_at: datetime | None
    our_last_message_at: datetime | None
    patient_messages: int
    our_messages: int
    followups_sent: int
    last_followup_at: datetime | None
    takeover_until: datetime | None
    ai_paused_until: datetime | None
    phone_hash: str | None

    @property
    def phone_captured(self) -> bool:
        """Телефон получен. Сам номер здесь недоступен — и не нужен."""
        return self.phone_hash is not None

    def takeover_active(self, moment: datetime) -> bool:
        return self.takeover_until is not None and moment < self.takeover_until

    def ai_pause_active(self, moment: datetime) -> bool:
        return self.ai_paused_until is not None and moment < self.ai_paused_until

    def ai_active(self, moment: datetime) -> bool:
        """Правило «можно ли боту говорить» живёт здесь, в одном месте."""
        return not (self.takeover_active(moment) or self.ai_pause_active(moment))


@dataclass(frozen=True)
class DraftRow:
    id: int
    chat_id: str
    text: str
    kind: str
    reason: str
    status: str
    created_at: datetime
    resolved_at: datetime | None
    resolved_by: str | None
    final_text: str | None
    tg_message_id: int | None

    @property
    def is_pending(self) -> bool:
        return self.status == DRAFT_PENDING

    @property
    def outgoing_text(self) -> str:
        """Что реально уйдёт пациенту: правка администратора, если она была."""
        return self.final_text if self.final_text is not None else self.text


@dataclass(frozen=True)
class AuditRow:
    id: int
    at: datetime
    event: str
    chat_id: str | None
    payload: dict[str, Any]


@dataclass(frozen=True)
class InboxRow:
    """Сообщение, как его вычитал DOM-поллер.

    `at` намеренно отсутствует: время, которое Авито показывает человеку
    («14:32», «вчера»), в datetime не превращается — формат зависит от давности
    сообщения, и ошибка парсинга сдвинула бы весь диалог, а на времени диалога
    стоят дожимы. Есть `at_raw` для глаз администратора и `harvested_at` для
    любой арифметики.
    """

    external_id: str
    chat_id: str
    chat_url: str | None
    counterparty: str | None
    outgoing: bool
    text: str
    at_raw: str | None
    position: int
    harvested_at: datetime
    processed_at: datetime | None

    @property
    def role(self) -> str:
        """Роль в терминах prompt.builder.Turn."""
        return "clinic" if self.outgoing else "patient"


@dataclass(frozen=True)
class OutboxRow:
    id: int
    chat_id: str
    chat_url: str | None
    text: str
    kind: str
    draft_id: int | None
    send_after: datetime
    status: str
    attempts: int
    queued_at: datetime
    claimed_at: datetime | None
    sent_at: datetime | None
    confirmation: str | None
    last_error: str | None
    accounted_at: datetime | None


# --- время ------------------------------------------------------------------

def _moment(value: datetime, field: str) -> datetime:
    """Приводит момент к Europe/Moscow и запрещает naive datetime.

    Naive datetime здесь не «неудобен», а опасен: он сравнивается с tz-aware
    исключением, а записанный в базу — ломает лексикографический порядок ISO-строк,
    на котором держатся сравнения сроков паузы. Лучше внятная ошибка на входе.
    """
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"{field}: ожидается datetime с таймзоной, получен naive")
    return value.astimezone(hours.tz())


def _iso(value: datetime, field: str) -> str:
    """ISO-8601 фиксированной ширины. Ширина важна: Europe/Moscow — постоянный
    UTC+4 без перехода на летнее время, поэтому строки одинаковой длины
    сравниваются лексикографически ровно так же, как моменты хронологически, и
    SQL-сравнения сроков работают без парсинга."""
    return _moment(value, field).isoformat(timespec="microseconds")


def _parse(raw: str | None) -> datetime | None:
    return datetime.fromisoformat(raw) if raw else None


# --- отображение строк ------------------------------------------------------

def _dialog(row: sqlite3.Row) -> DialogRow:
    return DialogRow(
        chat_id=row["chat_id"],
        first_seen_at=datetime.fromisoformat(row["first_seen_at"]),
        patient_last_message_at=_parse(row["patient_last_message_at"]),
        our_last_message_at=_parse(row["our_last_message_at"]),
        patient_messages=row["patient_messages"],
        our_messages=row["our_messages"],
        followups_sent=row["followups_sent"],
        last_followup_at=_parse(row["last_followup_at"]),
        takeover_until=_parse(row["takeover_until"]),
        ai_paused_until=_parse(row["ai_paused_until"]),
        phone_hash=row["phone_hash"],
    )


def _draft(row: sqlite3.Row) -> DraftRow:
    return DraftRow(
        id=row["id"],
        chat_id=row["chat_id"],
        text=row["text"],
        kind=row["kind"],
        reason=row["reason"],
        status=row["status"],
        created_at=datetime.fromisoformat(row["created_at"]),
        resolved_at=_parse(row["resolved_at"]),
        resolved_by=row["resolved_by"],
        final_text=row["final_text"],
        tg_message_id=row["tg_message_id"],
    )


def _audit(row: sqlite3.Row) -> AuditRow:
    return AuditRow(
        id=row["id"],
        at=datetime.fromisoformat(row["at"]),
        event=row["event"],
        chat_id=row["chat_id"],
        payload=json.loads(row["payload"]) if row["payload"] else {},
    )


def _inbox(row: sqlite3.Row) -> InboxRow:
    return InboxRow(
        external_id=row["external_id"],
        chat_id=row["chat_id"],
        chat_url=row["chat_url"],
        counterparty=row["counterparty"],
        outgoing=bool(row["outgoing"]),
        text=row["text"],
        at_raw=row["at_raw"],
        position=row["position"],
        harvested_at=datetime.fromisoformat(row["harvested_at"]),
        processed_at=_parse(row["processed_at"]),
    )


def _outbox(row: sqlite3.Row) -> OutboxRow:
    return OutboxRow(
        id=row["id"],
        chat_id=row["chat_id"],
        chat_url=row["chat_url"],
        text=row["text"],
        kind=row["kind"],
        draft_id=row["draft_id"],
        send_after=datetime.fromisoformat(row["send_after"]),
        status=row["status"],
        attempts=row["attempts"],
        queued_at=datetime.fromisoformat(row["queued_at"]),
        claimed_at=_parse(row["claimed_at"]),
        sent_at=_parse(row["sent_at"]),
        confirmation=row["confirmation"],
        last_error=row["last_error"],
        accounted_at=_parse(row["accounted_at"]),
    )


class Store:
    """Состояние бота. Один экземпляр на процесс, файл общий для всех процессов."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

        # check_same_thread=False плюс собственный RLock: отправщик и панель
        # Telegram живут в разных задачах и потоках, а один и тот же курсор
        # sqlite3 из двух потоков даёт ProgrammingError. Лок дешевле, чем
        # соединение на поток, и не расходится с ожиданиями по количеству
        # писателей на файл.
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.path,
            timeout=BUSY_TIMEOUT_SECONDS,   # это и есть busy timeout SQLite
            isolation_level=None,           # транзакциями управляем сами
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row

        # journal_mode задаётся один раз на файл и переживает закрытие, но
        # выставляется всё равно при каждом открытии: база могла быть создана
        # чем-то другим (например, скриптом миграции) в режиме delete.
        self.journal_mode = str(
            self._conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]).lower()
        # foreign_keys — настройка соединения, по умолчанию ВЫКЛЮЧЕНА, и её
        # отсутствие ничего не ломает заметно: просто внешние ключи молча не
        # проверяются. Поэтому она выставляется здесь, а не в schema.sql.
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._apply_schema()

    def _apply_schema(self) -> None:
        """Схема применяется целиком при каждом открытии. Все CREATE — с
        IF NOT EXISTS, поэтому открытие существующей базы не трогает данные."""
        with self._lock:
            self._conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # --- транзакции ---------------------------------------------------------

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """Одна атомарная запись. BEGIN IMMEDIATE — см. докстринг модуля."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def _read(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # --- дедуп --------------------------------------------------------------

    def seen(self, external_id: str) -> bool:
        """Видели ли это сообщение раньше. Только для отчётов и логов: решение
        об обработке принимается результатом `mark_seen`, потому что между этим
        SELECT и последующей вставкой успевает вклиниться второй процесс."""
        rows = self._read("SELECT 1 FROM seen WHERE external_id = ?", (external_id,))
        return bool(rows)

    def mark_seen(self, external_id: str, chat_id: str, at: datetime) -> bool:
        """Зарегистрировать сообщение. True — оно новое и его нужно обработать.

        Единственная правильная точка входа для поллера: возвращаемое значение —
        это результат атомарной вставки, а не результат отдельной проверки.
        Повторный вызов с тем же external_id вернёт False и не создаст второй ряд
        даже если вызовы идут из двух процессов одновременно.
        """
        stamp = _iso(at, "at")
        with self._write() as conn:
            self._ensure_dialog(conn, chat_id, stamp)
            cursor = conn.execute(
                "INSERT INTO seen(external_id, chat_id, at, recorded_at) "
                "VALUES(?, ?, ?, ?) ON CONFLICT(external_id) DO NOTHING",
                (external_id, chat_id, stamp, _iso(hours.now(), "recorded_at")))
            # Целевой ON CONFLICT, а не INSERT OR IGNORE: OR IGNORE проглотил бы
            # заодно нарушение внешнего ключа и CHECK, то есть превратил бы баг
            # в тихое «сообщение уже было».
            return cursor.rowcount == 1

    # --- диалоги ------------------------------------------------------------

    @staticmethod
    def _ensure_dialog(conn: sqlite3.Connection, chat_id: str, stamp: str) -> None:
        """Строка диалога должна существовать раньше всего, что на неё ссылается.

        Вызывается внутри уже открытой транзакции и сама транзакций не открывает:
        BEGIN внутри BEGIN в SQLite — ошибка.
        """
        conn.execute(
            "INSERT INTO dialogs(chat_id, first_seen_at) VALUES(?, ?) "
            "ON CONFLICT(chat_id) DO NOTHING", (chat_id, stamp))

    def touch_dialog(self, chat_id: str, *, patient_message_at: datetime | None = None,
                     our_message_at: datetime | None = None) -> None:
        """Отметить активность в диалоге. Без аргументов просто регистрирует чат.

        Обе метки монотонны: назад время не сдвигается. Это не педантизм —
        followup.plan сравнивает our_last_message_at с patient_last_message_at,
        и если сообщение обработается не по порядку (перезапуск поллера отдаёт
        пачку не в том порядке), сдвиг назад превратит живой диалог в
        «пациент замолчал» и вызовет дожим поверх ответа пациента.
        """
        patient = _iso(patient_message_at, "patient_message_at") if patient_message_at else None
        our = _iso(our_message_at, "our_message_at") if our_message_at else None
        first = patient or our or _iso(hours.now(), "now")

        with self._write() as conn:
            conn.execute(
                """
                INSERT INTO dialogs(chat_id, first_seen_at, patient_last_message_at,
                                    our_last_message_at, patient_messages, our_messages)
                VALUES(:chat_id, :first, :patient, :our, :p_inc, :o_inc)
                ON CONFLICT(chat_id) DO UPDATE SET
                    patient_last_message_at = CASE
                        WHEN :patient IS NULL THEN patient_last_message_at
                        WHEN patient_last_message_at IS NULL
                             OR :patient > patient_last_message_at THEN :patient
                        ELSE patient_last_message_at END,
                    our_last_message_at = CASE
                        WHEN :our IS NULL THEN our_last_message_at
                        WHEN our_last_message_at IS NULL
                             OR :our > our_last_message_at THEN :our
                        ELSE our_last_message_at END,
                    patient_messages = patient_messages + :p_inc,
                    our_messages = our_messages + :o_inc
                """,
                {"chat_id": chat_id, "first": first, "patient": patient, "our": our,
                 "p_inc": 1 if patient else 0, "o_inc": 1 if our else 0})

    def dialog(self, chat_id: str) -> DialogRow | None:
        rows = self._read("SELECT * FROM dialogs WHERE chat_id = ?", (chat_id,))
        return _dialog(rows[0]) if rows else None

    def _set_until(self, chat_id: str, column: str, until: datetime | None) -> None:
        """Общая часть перехвата и паузы.

        `until=None` означает «бессрочно», а не «снять». Подпись контракта
        допускает оба чтения, и выбрано то, у которого безопасная сторона ошибки:
        если панель передаст None, имея в виду «совсем» (вариант паузы «совсем»
        существует, а способа выразить бесконечность иначе в подписи нет), бот
        промолчит — это стоит одного лида. При обратном чтении бот заговорил бы
        в диалоге, который администратор забрал себе, а это стоит пациента.
        Снятие выражается моментом в прошлом: `set_ai_paused(chat, hours.now())`.
        """
        if column not in ("takeover_until", "ai_paused_until"):
            raise ValueError(f"недопустимая колонка срока: {column!r}")
        stamp = _iso(until, column) if until is not None else _iso(FOREVER, column)
        with self._write() as conn:
            self._ensure_dialog(conn, chat_id, _iso(hours.now(), "now"))
            conn.execute(f"UPDATE dialogs SET {column} = ? WHERE chat_id = ?",
                         (stamp, chat_id))

    def set_takeover(self, chat_id: str, until: datetime | None) -> None:
        """Администратор забрал диалог себе. None — до отмены."""
        self._set_until(chat_id, "takeover_until", until)

    def set_ai_paused(self, chat_id: str, until: datetime | None) -> None:
        """Пауза ИИ в этом диалоге. None — совсем."""
        self._set_until(chat_id, "ai_paused_until", until)

    def is_ai_active(self, chat_id: str, moment: datetime) -> bool:
        """Можно ли боту отвечать в этом чате в этот момент.

        Неизвестный чат — можно: перехватить или поставить на паузу диалог,
        которого ещё не было, никто не мог.
        """
        row = self.dialog(chat_id)
        if row is None:
            return True
        return row.ai_active(_moment(moment, "moment"))

    def capture_phone(self, chat_id: str, phone_hash: str) -> None:
        """Записать ФАКТ получения телефона — хэш, не номер (см. докстринг модуля).

        Вход проверяется здесь, а не только CHECK-ом в схеме, чтобы вызывающий
        получил внятную ошибку вместо IntegrityError, если случайно передаст
        сырой номер.
        """
        low, high = _PHONE_HASH_LENGTH
        if not (low <= len(phone_hash) <= high) or any(
                ch not in "0123456789abcdef" for ch in phone_hash):
            raise ValueError(
                f"phone_hash должен быть hex-строкой {low}-{high} знаков "
                f"(pii.phone_hash); получено {len(phone_hash)} знаков — "
                "похоже на сырой номер, а номера в базу не пишутся")

        with self._write() as conn:
            self._ensure_dialog(conn, chat_id, _iso(hours.now(), "now"))
            conn.execute("UPDATE dialogs SET phone_hash = ? WHERE chat_id = ?",
                         (phone_hash, chat_id))

    def mark_followup_sent(self, chat_id: str, at: datetime) -> None:
        """Дожим отправлен. Без этого счётчика followup.plan не смог бы
        остановиться на втором дожиме: DialogState.followups_sent брать больше
        неоткуда. В контракте метода нет — см. отчёт, это добавление, а не
        изменение существующей подписи.
        """
        stamp = _iso(at, "at")
        with self._write() as conn:
            self._ensure_dialog(conn, chat_id, stamp)
            conn.execute(
                "UPDATE dialogs SET followups_sent = followups_sent + 1, "
                "last_followup_at = ? WHERE chat_id = ?", (stamp, chat_id))

    # --- черновики ----------------------------------------------------------

    def queue_draft(self, chat_id: str, text: str, *, kind: str, reason: str) -> int:
        """Поставить черновик в очередь администратору. Возвращает его id."""
        if not text.strip():
            raise ValueError("пустой черновик отправлять некому")
        stamp = _iso(hours.now(), "now")
        with self._write() as conn:
            self._ensure_dialog(conn, chat_id, stamp)
            cursor = conn.execute(
                "INSERT INTO drafts(chat_id, text, kind, reason, status, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (chat_id, text, kind, reason, DRAFT_PENDING, stamp))
            draft_id = cursor.lastrowid
        if draft_id is None:  # pragma: no cover - sqlite3 всегда отдаёт rowid
            raise RuntimeError("SQLite не вернул id вставленного черновика")
        return int(draft_id)

    def draft(self, draft_id: int) -> DraftRow | None:
        rows = self._read("SELECT * FROM drafts WHERE id = ?", (draft_id,))
        return _draft(rows[0]) if rows else None

    def pending_drafts(self, limit: int = 50) -> list[DraftRow]:
        """Неразобранные черновики, старые первыми: администратор отвечает в
        порядке поступления, а самый старый черновик — самый близкий к тому,
        чтобы пациент ушёл к конкуренту."""
        rows = self._read(
            "SELECT * FROM drafts WHERE status = ? ORDER BY created_at, id LIMIT ?",
            (DRAFT_PENDING, limit))
        return [_draft(row) for row in rows]

    def resolve_draft(self, draft_id: int, *, action: str, final_text: str | None = None,
                      by: str | None = None) -> DraftRow:
        """Закрыть черновик: sent | edited | ignored | expired.

        Повторный вызов с тем же action возвращает ту же строку и ничего не
        перезаписывает: Telegram доставляет один и тот же callback повторно, а
        второе «отправить» не должно выглядеть как новое решение и уж точно не
        должно менять время решения. Попытка сменить уже принятое решение падает
        с ValueError — администратор, нажавший «Игнор» после «Отправить», обязан
        увидеть, что ответ уже ушёл, а не тихо «отменить» его.
        """
        if action not in DRAFT_ACTIONS:
            raise ValueError(f"неизвестное решение {action!r}, "
                             f"допустимы {sorted(DRAFT_ACTIONS)}")
        if action == "edited" and not (final_text or "").strip():
            raise ValueError("правка без текста: отправлять будет нечего")

        with self._write() as conn:
            rows = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchall()
            if not rows:
                raise LookupError(f"черновик {draft_id} не найден")
            current = _draft(rows[0])

            if not current.is_pending:
                if current.status == action:
                    return current
                raise ValueError(
                    f"черновик {draft_id} уже закрыт решением {current.status!r}, "
                    f"сменить на {action!r} нельзя")

            # Отправка без правки уходит текстом черновика — в final_text он
            # дублируется намеренно: отправщик читает одну колонку и не решает,
            # какую из двух брать.
            if action == "sent":
                text = final_text if final_text is not None else current.text
            elif action == "edited":
                text = final_text
            else:
                text = None

            conn.execute(
                "UPDATE drafts SET status = ?, resolved_at = ?, resolved_by = ?, "
                "final_text = ? WHERE id = ?",
                (action, _iso(hours.now(), "now"), by, text, draft_id))
            updated = conn.execute("SELECT * FROM drafts WHERE id = ?",
                                   (draft_id,)).fetchall()[0]
            return _draft(updated)

    def link_draft_message(self, draft_id: int, tg_message_id: int) -> None:
        """Связать черновик с сообщением в Telegram: по нему приходят и нажатия
        кнопок, и «Правка» ответом на сообщение бота.

        Связь один-к-одному и проверяется в схеме (UNIQUE). Здесь конфликты
        превращаются во внятные ошибки, а повторная привязка того же сообщения к
        тому же черновику — в no-op: отправка в Telegram может быть повторена
        после таймаута, вернув тот же message_id.
        """
        with self._write() as conn:
            rows = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchall()
            if not rows:
                raise LookupError(f"черновик {draft_id} не найден")
            current = _draft(rows[0])
            if current.tg_message_id == tg_message_id:
                return
            if current.tg_message_id is not None:
                raise ValueError(
                    f"черновик {draft_id} уже привязан к сообщению "
                    f"{current.tg_message_id}, перепривязка запрещена")

            taken = conn.execute("SELECT id FROM drafts WHERE tg_message_id = ?",
                                 (tg_message_id,)).fetchall()
            if taken:
                raise ValueError(f"сообщение {tg_message_id} уже занято черновиком "
                                 f"{taken[0]['id']}")
            conn.execute("UPDATE drafts SET tg_message_id = ? WHERE id = ?",
                         (tg_message_id, draft_id))

    def draft_by_tg_message(self, tg_message_id: int) -> DraftRow | None:
        rows = self._read("SELECT * FROM drafts WHERE tg_message_id = ?", (tg_message_id,))
        return _draft(rows[0]) if rows else None

    # --- аудит --------------------------------------------------------------

    def audit(self, event: str, *, chat_id: str | None = None,
              payload: dict | None = None) -> None:
        """Записать событие. payload должен быть уже прогнан через pii.scrub().

        `default=str` в сериализации — чтобы datetime, Decimal и Enum в payload
        не роняли запись аудита: событие важнее точного типа поля. Исключения
        наружу не глотаются: аудит, который молча ничего не пишет, хуже, чем
        отсутствующий.
        """
        if not event.strip():
            raise ValueError("событие аудита без имени")
        blob = (json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
                if payload else None)
        with self._write() as conn:
            conn.execute("INSERT INTO audit(at, event, chat_id, payload) VALUES(?, ?, ?, ?)",
                         (_iso(hours.now(), "now"), event, chat_id, blob))

    def recent_audit(self, limit: int = 100) -> list[AuditRow]:
        """Последние события, свежие первыми: смотрят в него, когда нужно
        понять, что бот делал минуту назад."""
        rows = self._read("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))
        return [_audit(row) for row in rows]

    # --- шина с транспортом -------------------------------------------------
    # Методов этой секции в CONTRACTS.md нет: контракт описывает решения, а не
    # то, как два процесса передают друг другу работу. Ни одна существующая
    # подпись здесь не менялась — только добавлены новые.
    #
    # Разделение владения строгое и нарушать его нельзя:
    #   inbox  — пишет Node (capture/run-poll.mjs), читает Python;
    #   outbox — пишет Python, а ЗАБИРАЕТ И ЗАКРЫВАЕТ Node.
    # Поэтому здесь нет ни claim, ни mark_sent: SQL захвата строки живёт в одном
    # месте, у отправщика. Вторая реализация того же протокола на Python
    # означала бы две правды о том, что такое «занято», и рано или поздно —
    # два процесса, отправляющих пациенту один и тот же текст.

    def pending_inbox(self, limit: int = 20) -> list[InboxRow]:
        """Входящие, которых роутер ещё не разбирал. Старые первыми.

        Только чужие сообщения: свои пузыри лежат в той же таблице ради истории
        диалога, но отвечать на них нечего.
        """
        rows = self._read(
            "SELECT * FROM inbox WHERE processed_at IS NULL AND outgoing = 0 "
            "ORDER BY harvested_at, position, rowid LIMIT ?", (limit,))
        return [_inbox(row) for row in rows]

    def chat_history(self, chat_id: str, *, before_position: int | None = None,
                     limit: int = 40) -> list[InboxRow]:
        """История диалога для промпта, в порядке появления в переписке.

        `before_position` отсекает то, что в чате НИЖЕ разбираемого сообщения.
        Без этого модель увидела бы в «истории» сообщения, которых на момент
        обрабатываемого вопроса ещё не было, и ответила бы на реплику из
        будущего — при разборе накопившейся пачки это происходит постоянно.

        LIMIT берёт ХВОСТ истории, а не начало: свежие реплики важнее первых,
        а промпт не бесконечный.

        `rowid AS _rid` во вложенном запросе — не украшение: подзапрос своих
        служебных колонок наружу не отдаёт, и внешний ORDER BY по `rowid` падает
        с «no such column». Порядок вставки нужен как второй ключ сортировки,
        потому что `position` у пузырей одного прохода может совпасть.
        """
        if before_position is None:
            rows = self._read(
                "SELECT * FROM (SELECT *, rowid AS _rid FROM inbox WHERE chat_id = ? "
                "ORDER BY position DESC, _rid DESC LIMIT ?) ORDER BY position, _rid",
                (chat_id, limit))
        else:
            rows = self._read(
                "SELECT * FROM (SELECT *, rowid AS _rid FROM inbox WHERE chat_id = ? "
                "AND position < ? ORDER BY position DESC, _rid DESC LIMIT ?) "
                "ORDER BY position, _rid",
                (chat_id, before_position, limit))
        return [_inbox(row) for row in rows]

    def patient_texts(self, chat_id: str, limit: int = 20) -> tuple[str, ...]:
        """Тексты пациента для followup.DialogState: отказ, обещание позвонить.

        Ищутся по последним репликам, потому что «уже вылечил» в сообщении
        месячной давности к сегодняшнему дожиму отношения не имеет.
        """
        rows = self._read(
            "SELECT text FROM inbox WHERE chat_id = ? AND outgoing = 0 "
            "ORDER BY position DESC, rowid DESC LIMIT ?", (chat_id, limit))
        return tuple(row["text"] for row in rows)

    def mark_inbox_processed(self, external_id: str, at: datetime | None = None) -> bool:
        """Пометить входящее разобранным. False — такой строки нет или уже помечена.

        Помечать обязан демон ПОСЛЕ того, как решение принято и записано (ответ
        поставлен в outbox, черновик создан, событие в аудите). Обратный порядок
        теряет сообщение целиком при падении между двумя операциями: строка уже
        не «ждёт», а решения по ней нет.
        """
        stamp = _iso(at, "at") if at is not None else _iso(hours.now(), "now")
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE inbox SET processed_at = ? "
                "WHERE external_id = ? AND processed_at IS NULL", (stamp, external_id))
            return cursor.rowcount == 1

    def queue_outbox(self, chat_id: str, text: str, *, kind: str, send_after: datetime,
                     draft_id: int | None = None, chat_url: str | None = None) -> int:
        """Поставить ответ в очередь на отправку. Возвращает id строки outbox.

        Идемпотентно по `draft_id`: повторная постановка того же черновика
        возвращает id уже существующей строки и НЕ создаёт вторую. Это не
        удобство, а защита — Telegram доставляет один и тот же callback повторно,
        и два нажатия «Отправить» обязаны дать пациенту один ответ. Держится
        UNIQUE-индексом в схеме, а не проверкой перед вставкой: проверка
        проигрывает второму процессу.
        """
        if not text.strip():
            raise ValueError("пустой ответ отправлять нечего")
        if kind not in OUTBOX_KINDS:
            raise ValueError(f"неизвестный вид отправки {kind!r}, "
                             f"допустимы {sorted(OUTBOX_KINDS)}")

        stamp = _iso(hours.now(), "now")
        due = _iso(send_after, "send_after")
        with self._write() as conn:
            self._ensure_dialog(conn, chat_id, stamp)
            cursor = conn.execute(
                "INSERT INTO outbox(chat_id, chat_url, text, kind, draft_id, send_after, "
                "status, queued_at) VALUES(?, ?, ?, ?, ?, ?, 'queued', ?) "
                "ON CONFLICT(draft_id) DO NOTHING",
                (chat_id, chat_url, text, kind, draft_id, due, stamp))
            if cursor.rowcount == 1:
                return int(cursor.lastrowid or 0)
            existing = conn.execute("SELECT id FROM outbox WHERE draft_id = ?",
                                    (draft_id,)).fetchall()
        if not existing:  # pragma: no cover - вставка без draft_id конфликтовать не может
            raise RuntimeError("вставка в outbox не удалась и конфликтующей строки нет")
        return int(existing[0]["id"])

    def outbox(self, outbox_id: int) -> OutboxRow | None:
        rows = self._read("SELECT * FROM outbox WHERE id = ?", (outbox_id,))
        return _outbox(rows[0]) if rows else None

    def outbox_by_status(self, status: str, limit: int = 50) -> list[OutboxRow]:
        rows = self._read(
            "SELECT * FROM outbox WHERE status = ? ORDER BY send_after, id LIMIT ?",
            (status, limit))
        return [_outbox(row) for row in rows]

    def sent_unaccounted(self, limit: int = 50) -> list[OutboxRow]:
        """Отправленное, что демон ещё не учёл в состоянии диалога.

        Node подтверждает отправку, но не знает ни про счётчик дожимов, ни про
        монотонность our_last_message_at, ни про аудит. Учёт — отдельный шаг
        Python-а, и его наличие в базе делает потерю учёта видимой: строка не
        исчезает, пока её не учли.
        """
        rows = self._read(
            "SELECT * FROM outbox WHERE status = 'sent' AND accounted_at IS NULL "
            "ORDER BY sent_at, id LIMIT ?", (limit,))
        return [_outbox(row) for row in rows]

    def mark_outbox_accounted(self, outbox_id: int) -> bool:
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE outbox SET accounted_at = ? WHERE id = ? AND status = 'sent' "
                "AND accounted_at IS NULL", (_iso(hours.now(), "now"), outbox_id))
            return cursor.rowcount == 1

    def stuck_sending(self, older_than_seconds: float = 300.0) -> list[OutboxRow]:
        """Строки, зависшие в 'sending'. Требуют человека, а не повтора.

        Состояние 'sending' означает «отправщик взял строку и не вернулся».
        Ушло сообщение пациенту или нет — неизвестно, и автоматический повтор
        здесь может выдать пациенту дубликат. Дубликат в переписке выдаёт бота
        вернее любой формулировки; потерянный ответ стоит минуты администратора.
        Поэтому такие строки только показываются, а решение принимает человек.
        """
        cutoff = hours.now() - timedelta(seconds=older_than_seconds)
        rows = self._read(
            "SELECT * FROM outbox WHERE status = 'sending' AND claimed_at IS NOT NULL "
            "AND claimed_at < ? ORDER BY claimed_at, id",
            (_iso(cutoff, "cutoff"),))
        return [_outbox(row) for row in rows]

    def dialogs_awaiting_reply(self, limit: int = 50) -> list[DialogRow]:
        """Диалоги, где последними писали мы, а ответа нет — сырьё для дожима.

        Отбор нарочно широкий: здесь только «мы написали последними и лимит
        дожимов не исчерпан». Все содержательные причины промолчать — отказ,
        обещание позвонить, нерабочее время, остывший диалог — решает
        `followup.plan`, и дублировать их условием в SQL нельзя: разъедутся.
        """
        rows = self._read(
            "SELECT * FROM dialogs WHERE our_last_message_at IS NOT NULL "
            "AND (patient_last_message_at IS NULL "
            "     OR patient_last_message_at < our_last_message_at) "
            "AND followups_sent < ? ORDER BY our_last_message_at LIMIT ?",
            (MAX_FOLLOWUPS_GUARD, limit))
        return [_dialog(row) for row in rows]

    # --- курсоры процессов --------------------------------------------------

    def cursor(self, name: str) -> str | None:
        rows = self._read("SELECT value FROM cursors WHERE name = ?", (name,))
        return rows[0]["value"] if rows else None

    def set_cursor(self, name: str, value: str) -> None:
        """Запомнить позицию процесса (offset getUpdates, время обхода дожимов).

        Для offset-а Telegram это критично: потерянный offset означает повторную
        выдачу старых нажатий кнопок, то есть повторную отправку пациенту уже
        отправленного. Идемпотентность resolve_draft/queue_outbox это поймает,
        но полагаться на два предохранителя вместо одного не нужно.
        """
        with self._write() as conn:
            conn.execute(
                "INSERT INTO cursors(name, value, updated_at) VALUES(?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (name, value, _iso(hours.now(), "now")))
