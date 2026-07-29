# -*- coding: utf-8 -*-
"""Демон решений: единственный процесс Python, который что-то делает сам.

Всё остальное в brain/ — чистые функции и запросы к базе, которые кто-то должен
вызвать. Вызывает их этот файл, и больше никто.

Устройство осознанно скучное: один цикл, четыре шага по очереди, между
итерациями сон. Ни воркеров, ни очередей в памяти, ни планировщика. Нагрузка
проекта — 10-30 обращений в день; всё, что сложнее одного цикла, здесь окупается
только новыми способами потерять сообщение.

    1. inbox   — разобрать входящие: роутер, вето, ответ в outbox или черновик
    2. tg      — забрать решения администратора и превратить их в отправки
    3. account — учесть то, что Node уже отправил (счётчики, аудит, дожимы)
    4. followup— дожать замолчавшие диалоги

Порядок не произволен. Учёт отправленного идёт ДО дожима: иначе дожим увидит
диалог, в котором наш ответ ещё «не отправлен», и посчитает молчание от старого
момента. Решения администратора — до учёта, чтобы одобренный минуту назад ответ
попал в очередь этой же итерацией, а не следующей.

Что этот файл НЕ делает: не открывает браузер, не трогает Авито, не отправляет
пациенту. Транспорт — capture/run-poll.mjs, отдельный процесс, общая шина в
SQLite (таблицы inbox/outbox, см. schema.sql). Разделение позволяет перезапускать
скрапер, который ломается от смены вёрстки чаще всего, не теряя ни одного
решённого черновика.

Аварийная остановка: SIGINT/SIGTERM доводят текущую итерацию до конца и выходят.
Убивать посреди шага можно — все шаги идемпотентны по построению, потому что
исходят из состояния в базе, а не из памяти процесса.

Запуск:
    python brain/run.py              # рабочий цикл
    python brain/run.py --once       # одна итерация, для проверки после правок
    python brain/run.py --dry-run    # решения считаются и логируются, outbox пуст
"""
from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

_BRAIN = Path(__file__).resolve().parent
sys.path.insert(0, str(_BRAIN))
sys.path.insert(0, str(_BRAIN / "gate"))

import delay as delay_mod  # noqa: E402
import followup as followup_mod  # noqa: E402
import hours  # noqa: E402
import pii  # noqa: E402
import router  # noqa: E402
from llm import client as llm  # noqa: E402
from prompt.builder import Turn  # noqa: E402
from store.db import Store  # noqa: E402
from tg import panel  # noqa: E402

# Пауза между итерациями. Секунды, не минуты: пациент, написавший в пять клиник,
# сравнивает в том числе скорость, а delay.plan_reply всё равно держит ответ
# 40-90 с. Опрос дешёвый — это SELECT по частичному индексу.
IDLE_SLEEP_SECONDS = 5.0

# Сколько входящих разбирать за итерацию. Ограничение существует, чтобы пачка,
# накопившаяся за ночь, не выжгла всю квоту LLM в первую минуту утра.
INBOX_BATCH = 10

# Курсоры в таблице cursors.
CURSOR_TG_OFFSET = "tg_offset"
CURSOR_FOLLOWUP_SWEEP = "followup_sweep_at"

# Дожимы проверяются редко: решение принимается по часам молчания, и чаще раза в
# несколько минут смысла в обходе нет.
FOLLOWUP_SWEEP_EVERY = timedelta(minutes=5)

# Сколько ждать зависшую отправку, прежде чем позвать человека.
STUCK_SENDING_SECONDS = 300.0


def _log(message: str) -> None:
    """Лог в stdout. Прогон через pii.scrub обязателен: сюда попадают куски
    переписки, а лог демона на ноутбуке никем не защищён."""
    print(f"{hours.now().isoformat(timespec='seconds')} {pii.scrub(message)}", flush=True)


@dataclass
class Counters:
    """Что сделала итерация. Печатается только когда не пусто: демон, который
    каждые пять секунд пишет «сделал ничего», делает лог нечитаемым и прячет
    в себе настоящие события."""

    processed: int = 0
    auto: int = 0
    drafts: int = 0
    resolved: int = 0
    accounted: int = 0
    followups: int = 0
    errors: int = 0

    def __bool__(self) -> bool:
        return any((self.processed, self.resolved, self.accounted,
                    self.followups, self.errors))

    def line(self) -> str:
        parts = []
        if self.processed:
            parts.append(f"разобрано {self.processed} (авто {self.auto}, "
                         f"черновиков {self.drafts})")
        if self.resolved:
            parts.append(f"решений администратора {self.resolved}")
        if self.accounted:
            parts.append(f"учтено отправок {self.accounted}")
        if self.followups:
            parts.append(f"дожимов {self.followups}")
        if self.errors:
            parts.append(f"ОШИБОК {self.errors}")
        return "; ".join(parts)


# --- шаг 1: входящие --------------------------------------------------------

def _history(store: Store, row) -> tuple[Turn, ...]:
    """История диалога для промпта, обрезанная позицией разбираемого сообщения.

    Время каждой реплики — `harvested_at`, а не то, что показал Авито: строку
    «вчера» в datetime не превратить, а промпту нужен порядок, который позиция
    уже задаёт.
    """
    return tuple(
        Turn(role=item.role, text=item.text, at=item.harvested_at)
        for item in store.chat_history(row.chat_id, before_position=row.position)
        if item.external_id != row.external_id
    )


async def step_inbox(store: Store, *, dry_run: bool) -> Counters:
    """Разобрать входящие. Один вызов LLM на сообщение, не больше."""
    counters = Counters()

    for row in store.pending_inbox(limit=INBOX_BATCH):
        incoming = router.Incoming(
            chat_id=row.chat_id,
            external_id=row.external_id,
            text=row.text,
            at=row.harvested_at,
            history=_history(store, row),
        )

        try:
            outcome = await router.handle(incoming, store)
        except Exception as exc:  # noqa: BLE001 - демон не имеет права упасть
            # Сообщение НЕ помечается разобранным: следующая итерация попробует
            # снова. Единственная альтернатива — потерять обращение молча.
            counters.errors += 1
            store.audit("router_error", chat_id=row.chat_id,
                        payload={"external_id": row.external_id,
                                 "error": type(exc).__name__})
            _log(f"ОШИБКА роутера на {row.external_id}: {type(exc).__name__}: {exc}")
            continue

        counters.processed += 1

        # Дебаунс — ЕДИНСТВЕННЫЙ отказ, после которого сообщение обязано
        # вернуться. Пациент дописывает мысль, и через 15 с его нужно
        # обработать снова. Пометить такое разобранным — значит потерять
        # обращение навсегда и молча: в inbox оно есть, в outbox его нет, и
        # ни одной ошибки в логе. Все остальные skip окончательны (дубликат,
        # перехват администратором, пауза ИИ) и помечаются.
        if outcome.route == "skip" and outcome.kind == "debounce":
            counters.processed -= 1
            continue

        if dry_run:
            _log(f"[dry-run] {row.chat_id}: {outcome.route} — {outcome.reason}")
            store.mark_inbox_processed(row.external_id)
            continue

        if outcome.route == "auto" and outcome.text:
            store.queue_outbox(row.chat_id, outcome.text, kind="reply",
                               send_after=outcome.send_at or hours.now(),
                               chat_url=row.chat_url)
            counters.auto += 1

        elif outcome.route == "draft":
            # Текст модели может отсутствовать (LLM недоступна) — тогда
            # администратор отвечает сам, а черновик несёт причину и вопрос.
            body = outcome.text or (
                f"Модель недоступна ({outcome.llm_failure}). "
                f"Вопрос пациента: {row.text}")
            draft_id = store.queue_draft(row.chat_id, body,
                                         kind=outcome.kind, reason=outcome.reason)
            await _publish_draft(store, draft_id, row)
            counters.drafts += 1

        # route == "ignore" / "skip" — записей не требуют, роутер уже отписал
        # в аудит; помечаем разобранным и идём дальше.

        # Пометка ПОСЛЕ записи решения: обратный порядок теряет сообщение при
        # падении между двумя операциями.
        store.mark_inbox_processed(row.external_id)

    return counters


async def _publish_draft(store: Store, draft_id: int, row) -> None:
    """Показать черновик администратору. Ошибка Telegram не теряет черновик.

    Черновик уже в базе, и `pending_drafts()` вернёт его следующей итерации:
    неопубликованный черновик — это отложенная публикация, а не пропавший лид.
    """
    draft = store.draft(draft_id)
    if draft is None:  # pragma: no cover - строку только что вставили
        return
    excerpt = f"{row.counterparty or 'Пациент'}: {row.text}"
    try:
        message_id = await panel.post_draft(draft, dialog_excerpt=excerpt)
        store.link_draft_message(draft_id, message_id)
    except Exception as exc:  # noqa: BLE001
        store.audit("draft_publish_failed", chat_id=row.chat_id,
                    payload={"draft_id": draft_id, "error": type(exc).__name__})
        _log(f"черновик {draft_id} не ушёл в Telegram: {type(exc).__name__}: {exc}")


async def publish_unposted(store: Store) -> int:
    """Догнать черновики, которые не удалось опубликовать раньше."""
    published = 0
    for draft in store.pending_drafts(limit=20):
        if draft.tg_message_id is not None:
            continue
        try:
            message_id = await panel.post_draft(
                draft, dialog_excerpt="(повторная публикация черновика)")
            store.link_draft_message(draft.id, message_id)
            published += 1
        except Exception as exc:  # noqa: BLE001
            _log(f"повторная публикация черновика {draft.id} не удалась: "
                 f"{type(exc).__name__}")
            break  # Telegram недоступен целиком — остальные тоже не уйдут
    return published


# --- шаг 2: решения администратора ------------------------------------------

async def step_telegram(store: Store) -> Counters:
    """Забрать нажатия кнопок и правки, применить их к базе.

    Offset getUpdates хранится в базе, а не в памяти: потерянный offset означает
    повторную выдачу старых нажатий, то есть повторную отправку пациенту уже
    отправленного. Второй предохранитель — идемпотентность `resolve_draft` и
    `queue_outbox` по draft_id, но полагаться на один слой не нужно.
    """
    counters = Counters()
    raw = store.cursor(CURSOR_TG_OFFSET)
    offset = int(raw) if raw else None

    try:
        updates = await panel.api.get_updates(offset=offset, timeout_s=0.0,
                                              config=panel.config())
    except Exception as exc:  # noqa: BLE001
        counters.errors += 1
        _log(f"Telegram недоступен: {type(exc).__name__}: {exc}")
        return counters

    for update in updates:
        update_id = update.get("update_id")
        try:
            action = await panel.parse_callback(update)
            if action.actionable:
                await _apply(store, action)
                counters.resolved += 1
        except Exception as exc:  # noqa: BLE001
            counters.errors += 1
            store.audit("tg_action_failed",
                        payload={"error": type(exc).__name__, "detail": str(exc)[:200]})
            _log(f"решение администратора не применено: {type(exc).__name__}: {exc}")
        finally:
            # Offset двигается ДАЖЕ при ошибке применения. Иначе один битый
            # апдейт заклинивает панель навсегда: Telegram будет отдавать его
            # снова и снова, а всё, что за ним, — никогда. Событие в аудите
            # есть, администратор нажмёт кнопку повторно.
            if isinstance(update_id, int):
                store.set_cursor(CURSOR_TG_OFFSET, str(update_id + 1))

    return counters


async def _apply(store: Store, action: panel.Action) -> None:
    """Применить одно решение администратора. Здесь и только здесь пишется
    в базу то, что нажали в Telegram."""
    kind = action.kind
    by = action.payload.get("by")

    if kind in ("send", "edit", "ignore"):
        if action.draft_id is None:
            return
        draft = store.draft(action.draft_id)
        if draft is None:
            await panel.ack(action, text="Черновик не найден", show_alert=True)
            return

        if kind == "ignore":
            store.resolve_draft(action.draft_id, action="ignored", by=by)
            await panel.ack(action, text="Пропущено")
            return

        final = action.payload.get("text") if kind == "edit" else None
        resolved = store.resolve_draft(
            action.draft_id,
            action="edited" if kind == "edit" else "sent",
            final_text=final, by=by)

        # Задержка считается от «сейчас»: администратор мог думать над черновиком
        # полчаса, и отсчитывать человеческую паузу от времени пациента бессмысленно.
        plan = delay_mod.plan_reply(resolved.outgoing_text, is_first_reply=False)
        store.queue_outbox(resolved.chat_id, resolved.outgoing_text, kind="reply",
                           send_after=plan.send_at, draft_id=resolved.id,
                           chat_url=_chat_url(store, resolved.chat_id))
        await panel.ack(action, text="Отправляется")
        return

    if kind == "takeover":
        chat_id = _chat_of(store, action)
        if chat_id:
            store.set_takeover(chat_id, action.payload.get("until"))
            store.audit("takeover", chat_id=chat_id, payload={"by": by})
            await panel.ack(action, text="Диалог за вами, бот молчит")
        return

    if kind == "pause":
        chat_id = _chat_of(store, action)
        if chat_id:
            store.set_ai_paused(chat_id, action.payload.get("until"))
            store.audit("ai_paused", chat_id=chat_id,
                        payload={"by": by, "variant": action.payload.get("variant")})
            await panel.ack(action, text="ИИ на паузе")
        return

    if kind == "resume":
        chat_id = _chat_of(store, action)
        if chat_id:
            # Снятие — момент в прошлом, и обязательно ОБА срока: перехват и
            # пауза в базе независимы, снятие одного не вернёт бота, пока держит
            # второй, а администратор нажал одну кнопку и ждёт одного эффекта.
            past = action.payload.get("until") or hours.now()
            store.set_ai_paused(chat_id, past)
            store.set_takeover(chat_id, past)
            store.audit("ai_resumed", chat_id=chat_id, payload={"by": by})
            await panel.ack(action, text="Бот снова отвечает")
        return


def _chat_of(store: Store, action: panel.Action) -> str | None:
    """Чат Авито, к которому относится действие.

    Панель `chat_id` не заполняет намеренно: кнопка несёт только `draft_id`, а
    подставить туда id чата TELEGRAM было бы прямой ошибкой — этим ключом
    адресуется диалог АВИТО.
    """
    if action.chat_id:
        return action.chat_id
    if action.draft_id is None:
        return None
    draft = store.draft(action.draft_id)
    return draft.chat_id if draft else None


def _chat_url(store: Store, chat_id: str) -> str | None:
    """URL чата из последнего входящего. Отправщик открывает чат по нему, а не
    по позиции в списке: список переставляется между чтением и отправкой."""
    history = store.chat_history(chat_id, limit=1)
    return history[0].chat_url if history else None


# --- шаг 3: учёт отправленного ----------------------------------------------

async def step_account(store: Store) -> Counters:
    """Учесть то, что Node уже отправил.

    Node подтверждает отправку появлением сообщения в переписке, но про счётчики
    диалога, монотонность our_last_message_at и дожимы он не знает и знать не
    должен. Пока строка не учтена, она видна в `sent_unaccounted()` — потеря
    учёта не тихая.
    """
    counters = Counters()

    for row in store.sent_unaccounted(limit=50):
        assert row.sent_at is not None  # гарантировано CHECK-ом в схеме
        store.touch_dialog(row.chat_id, our_message_at=row.sent_at)
        if row.kind == "followup":
            store.mark_followup_sent(row.chat_id, row.sent_at)
        store.audit("sent", chat_id=row.chat_id,
                    payload={"outbox_id": row.id, "kind": row.kind,
                             "draft_id": row.draft_id})
        store.mark_outbox_accounted(row.id)
        counters.accounted += 1

    # Зависшие отправки: человек, а не повтор. Ушло сообщение или нет —
    # неизвестно, а дубликат в переписке выдаёт бота вернее любой формулировки.
    stuck = store.stuck_sending(STUCK_SENDING_SECONDS)
    if stuck:
        ids = ", ".join(f"#{row.id}" for row in stuck[:5])
        await panel.notify(
            f"Отправка зависла: {ids}. Отправщик взял строки и не вернулся — "
            f"неизвестно, увидел ли их пациент. Проверьте переписку в Авито "
            f"руками: автоматически повторять нельзя, это риск дубликата.",
            level="alarm")

    return counters


# --- шаг 4: дожим -----------------------------------------------------------

async def step_followup(store: Store, *, dry_run: bool) -> Counters:
    """Дожать замолчавшие диалоги. Все причины промолчать — в followup.plan."""
    counters = Counters()

    last = store.cursor(CURSOR_FOLLOWUP_SWEEP)
    now = hours.now()
    if last and now - datetime.fromisoformat(last) < FOLLOWUP_SWEEP_EVERY:
        return counters
    store.set_cursor(CURSOR_FOLLOWUP_SWEEP, now.isoformat())

    auto_first = os.environ.get("AVITO_BOT_FOLLOWUP_1_AUTO", "0") == "1"
    first_route = followup_mod.Route.AUTO if auto_first else followup_mod.Route.DRAFT

    for dialog in store.dialogs_awaiting_reply(limit=50):
        if dialog.our_last_message_at is None:  # pragma: no cover - отсечено SQL
            continue

        state = followup_mod.DialogState(
            our_last_message_at=dialog.our_last_message_at,
            patient_last_message_at=dialog.patient_last_message_at,
            followups_sent=dialog.followups_sent,
            last_followup_at=dialog.last_followup_at,
            phone_captured=dialog.phone_captured,
            human_took_over=not dialog.ai_active(now),
            patient_texts=store.patient_texts(dialog.chat_id),
        )
        plan = followup_mod.plan(state, now, first_route=first_route)
        if plan is None:
            continue

        if dry_run:
            _log(f"[dry-run] дожим {plan.number} в {dialog.chat_id}: {plan.reason}")
            counters.followups += 1
            continue

        if plan.route is followup_mod.Route.AUTO:
            store.queue_outbox(dialog.chat_id, plan.text, kind="followup",
                               send_after=plan.send_at,
                               chat_url=_chat_url(store, dialog.chat_id))
        else:
            draft_id = store.queue_draft(dialog.chat_id, plan.text,
                                         kind="followup", reason=plan.reason)
            draft = store.draft(draft_id)
            if draft is not None:
                try:
                    message_id = await panel.post_draft(
                        draft, dialog_excerpt=f"(дожим {plan.number}) {plan.reason}")
                    store.link_draft_message(draft_id, message_id)
                except Exception as exc:  # noqa: BLE001
                    _log(f"дожим-черновик {draft_id} не ушёл: {type(exc).__name__}")
            # Счётчик дожимов растёт при ФАКТЕ отправки (step_account), а не
            # при постановке черновика: черновик администратор может и не
            # отправить, а лимит в два дожима — про то, что увидел пациент.

        counters.followups += 1

    return counters


# --- цикл -------------------------------------------------------------------

class Daemon:
    """Состояние процесса: база, флаг остановки, режим."""

    def __init__(self, db_path: str | Path, *, dry_run: bool = False) -> None:
        self.store = Store(db_path)
        self.dry_run = dry_run
        self.stopping = False

    def request_stop(self, *_signal: object) -> None:
        """Обработчик сигнала. Итерация доводится до конца, потом выход.

        Убить посреди шага тоже можно — все шаги исходят из состояния в базе, а
        не из памяти процесса, и повторяются без вреда. Но доведённая итерация
        не оставляет черновик неопубликованным.
        """
        if not self.stopping:
            _log("получен сигнал остановки, доводим итерацию до конца")
        self.stopping = True

    async def tick(self) -> Counters:
        """Одна итерация: четыре шага по очереди.

        Шаги — функции без аргументов, а не готовые корутины. Разница не
        стилистическая: список корутин создаётся целиком до первого await, и
        когда первый шаг падает, остальные так и остаются несозданными задачами
        («coroutine was never awaited»), то есть тихо теряются вместе со своей
        работой. Ленивый вызов гарантирует, что шаг либо выполнен, либо не начат.
        """
        steps = [
            lambda: step_inbox(self.store, dry_run=self.dry_run),
            lambda: step_followup(self.store, dry_run=self.dry_run),
        ]
        if not self.dry_run:
            # Telegram и учёт отправленного трогают внешний мир и состояние
            # диалога, поэтому в dry-run их нет вовсе. Порядок: решения
            # администратора → учёт отправленного → дожим. Учёт раньше дожима,
            # иначе дожим увидит диалог, в котором наш ответ ещё «не отправлен»,
            # и отсчитает молчание от старого момента.
            steps = [
                lambda: step_inbox(self.store, dry_run=self.dry_run),
                lambda: step_telegram(self.store),
                lambda: step_account(self.store),
                lambda: step_followup(self.store, dry_run=self.dry_run),
            ]

        total = Counters()
        for make_step in steps:
            part = await make_step()
            total.processed += part.processed
            total.auto += part.auto
            total.drafts += part.drafts
            total.resolved += part.resolved
            total.accounted += part.accounted
            total.followups += part.followups
            total.errors += part.errors
        return total

    async def run(self, *, once: bool = False) -> int:
        while True:
            try:
                counters = await self.tick()
                if counters:
                    _log(counters.line())
            except Exception as exc:  # noqa: BLE001
                # Цикл не имеет права остановиться из-за одной итерации: пока
                # он жив, следующая попытка через IDLE_SLEEP_SECONDS. Мёртвый
                # демон не отвечает никому и обнаруживается по жалобе клиники.
                self.store.audit("tick_failed", payload={"error": type(exc).__name__})
                _log(f"ИТЕРАЦИЯ УПАЛА: {type(exc).__name__}: {exc}")

            if once or self.stopping:
                return 0
            await asyncio.sleep(IDLE_SLEEP_SECONDS)


def db_path() -> Path:
    """Путь к базе. Одна переменная окружения, общая с capture/run-poll.mjs:
    две половины обязаны открыть ОДИН файл, иначе поллер пишет в пустоту."""
    raw = os.environ.get("AVITO_BOT_DB")
    if not raw:
        raise SystemExit(
            "AVITO_BOT_DB не задана. Заполните .env по образцу .env.example. "
            "Демон и поллер обязаны открыть один и тот же файл базы.")
    return Path(raw)


async def _main(args: argparse.Namespace) -> int:
    daemon = Daemon(db_path(), dry_run=args.dry_run)

    # Проверки при старте, а не при первом лиде. Отсутствие токена Telegram
    # обязано быть видно в момент запуска: черновик, который некому показать,
    # обнаружится иначе только когда пациент уже ждёт.
    if not args.dry_run:
        try:
            _log(panel.check_config())
        except Exception as exc:  # noqa: BLE001
            raise SystemExit(f"панель Telegram не настроена: {exc}") from exc

    health = llm.health()
    _log(f"LLM: ключей {health.get('keys_total', 0)}, "
         f"в кулдауне {health.get('keys_on_cooldown', 0)}, "
         f"моделей забанено {health.get('models_banned', 0)}")
    if not health.get("keys_total"):
        # Не отказ в старте: деградированный режим предусмотрен, белый список
        # отвечает и без модели, остальное уходит администратору. Но молча
        # работать без единого ключа демон не должен.
        _log("ВНИМАНИЕ: ни одного ключа LLM. Работаем в деградированном режиме: "
             "белый список отвечает сам, всё остальное уходит администратору.")

    if not args.dry_run:
        published = await publish_unposted(daemon.store)
        if published:
            _log(f"догнали неопубликованных черновиков: {published}")

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, daemon.request_stop)
        except NotImplementedError:
            # Windows: add_signal_handler в ProactorEventLoop не реализован.
            # signal.signal работает и там, только обработчик отработает между
            # await-ами, что нас устраивает — итерация всё равно доводится.
            signal.signal(sig, daemon.request_stop)

    mode = "dry-run" if args.dry_run else "рабочий"
    _log(f"демон запущен ({mode}), база {daemon.store.path}")
    try:
        return await daemon.run(once=args.once)
    finally:
        daemon.store.close()
        _log("демон остановлен")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Демон решений авито-бота «ДентаКлиника»")
    parser.add_argument("--once", action="store_true",
                        help="одна итерация и выход — для проверки после правок")
    parser.add_argument("--dry-run", action="store_true",
                        help="считать и логировать решения, ничего не ставить "
                             "в очередь отправки и не трогать Telegram")
    args = parser.parse_args()
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
