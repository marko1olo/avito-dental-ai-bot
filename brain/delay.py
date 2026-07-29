# -*- coding: utf-8 -*-
"""Человеческая задержка перед отправкой ответа.

Мгновенный ответ выдаёт бота вернее любой формулировки: живой администратор
физически не отвечает через 300 мс. Слишком долгий ответ теряет лид — пациент
на Авито пишет в 3-5 клиник и уходит к тому, кто ответил первым. Поэтому
задержка не константа и не «побольше для реализма», а окно с потолком.

Три вещи, которые здесь делаются намеренно:

1. **Первый ответ дольше последующих.** Первый — это «администратор заметил
   уведомление» (40-90 с). Дальше диалог уже идёт, и пауза короче.
2. **Время набора зависит от длины ответа.** Отправить 400 знаков за 5 секунд
   невозможно; ~250 знаков в минуту — темп администратора с телефона.
3. **Дебаунс.** Пациент часто дописывает мысль вторым и третьим сообщением.
   Человек дожидается и отвечает один раз на всё; бот, отвечающий построчно,
   опознаётся мгновенно.

Плюс джиттер: два одинаковых интервала подряд — тоже признак автомата.
"""
from __future__ import annotations

import random
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

# Пакет ещё не устанавливается (нет setup/venv-шага в деплое на ноутбук),
# поэтому gate подключается по пути. Заменить на обычный импорт, когда
# появится pyproject.
sys.path.insert(0, str(Path(__file__).resolve().parent / "gate"))
import hours  # noqa: E402

FIRST_REPLY_RANGE = (40.0, 90.0)
FOLLOWUP_RANGE = (15.0, 45.0)
TYPING_CHARS_PER_MINUTE = 250.0
DEBOUNCE_SECONDS = 15.0
WORKING_HOURS_CAP = 180.0
AFTER_HOURS_OFFSET_RANGE = (3 * 60.0, 17 * 60.0)
MIN_DELTA_FROM_LAST = 4.0


@dataclass(frozen=True)
class Plan:
    """Когда отправлять и почему именно тогда."""

    send_at: datetime
    delay_seconds: float
    reason: str

    @property
    def deferred_to_opening(self) -> bool:
        return self.reason.startswith("вне рабочих часов")


def _typing_seconds(text: str) -> float:
    return len(text) / TYPING_CHARS_PER_MINUTE * 60.0


def _jitter(rng: random.Random, low: float, high: float, avoid: float | None) -> float:
    """Значение из окна, не совпадающее с прошлой задержкой."""
    for _ in range(8):
        value = rng.uniform(low, high)
        if avoid is None or abs(value - avoid) >= MIN_DELTA_FROM_LAST:
            return value
    return rng.uniform(low, high)


def plan_reply(
    reply_text: str,
    *,
    is_first_reply: bool,
    received_at: datetime | None = None,
    last_delay_seconds: float | None = None,
    rng: random.Random | None = None,
) -> Plan:
    """Во сколько отправить ответ на сообщение, пришедшее в `received_at`."""
    rng = rng or random.Random()
    received_at = received_at or hours.now()

    low, high = FIRST_REPLY_RANGE if is_first_reply else FOLLOWUP_RANGE
    delay = _jitter(rng, low, high, last_delay_seconds)
    if not is_first_reply:
        delay += _typing_seconds(reply_text)

    delay = min(delay, WORKING_HOURS_CAP)
    send_at = received_at + timedelta(seconds=delay)

    if hours.is_booking_open(send_at):
        kind = "первый ответ" if is_first_reply else "ответ в диалоге"
        return Plan(send_at, delay, f"{kind}, рабочее время")

    # Вне окна записи отвечать «мы закрыты» — тупик. Отвечаем к открытию,
    # но не ровно в 09:00:00: одновременный залп в открытие тоже палит бота.
    nxt = hours.next_booking_day(received_at.date())
    if nxt is None or nxt.opens is None:
        return Plan(send_at, delay, "график не подтверждён — решает администратор")

    opening = datetime.combine(nxt.day, nxt.opens, tzinfo=received_at.tzinfo)
    opening += timedelta(seconds=rng.uniform(*AFTER_HOURS_OFFSET_RANGE))
    total = (opening - received_at).total_seconds()
    return Plan(opening, total,
                f"вне рабочих часов — отложено до открытия {nxt.day.strftime('%d.%m')}")


def should_wait_for_more(seconds_since_last_message: float) -> bool:
    """Пациент ещё дописывает — не отвечать построчно."""
    return seconds_since_last_message < DEBOUNCE_SECONDS


if __name__ == "__main__":
    rng = random.Random(20260729)
    now = hours.now()
    samples = [
        ("Здравствуйте", True, None),
        ("Первичная консультация с осмотром бесплатно, занимает約 30 минут.".replace("約 ", "около "),
         False, 61.0),
        ("Лечение кариеса — 5–6 тысяч рублей за зуб, вместе с анестезией. "
         "Точную сумму врач назовёт после осмотра, всё зависит от объёма. "
         "Позвоните +7 800 555-35-35, подберём удобное время.", False, 22.0),
    ]
    print(f"сейчас {now.strftime('%d.%m %H:%M %Z')}, запись открыта: {hours.is_booking_open(now)}\n")
    for text, first, last in samples:
        p = plan_reply(text, is_first_reply=first, received_at=now,
                       last_delay_seconds=last, rng=rng)
        print(f"{len(text):>4} знаков | {p.delay_seconds:7.1f} с | "
              f"{p.send_at.strftime('%d.%m %H:%M:%S')} | {p.reason}")

    night = now.replace(hour=23, minute=12, second=0, microsecond=0)
    p = plan_reply("Администратор позвонит утром, оставьте номер.",
                   is_first_reply=True, received_at=night, rng=rng)
    print(f"\nночью в {night.strftime('%H:%M')}: отправка {p.send_at.strftime('%d.%m %H:%M:%S')} "
          f"(через {p.delay_seconds / 60:.0f} мин) — {p.reason}")
    print(f"дебаунс: через 6 с ждём={should_wait_for_more(6)}, через 20 с ждём={should_wait_for_more(20)}")
