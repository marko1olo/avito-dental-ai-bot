# -*- coding: utf-8 -*-
"""Рабочее время «Денталии-2». Europe/Moscow, UTC+4 — не московское.

Ключевая тонкость, из-за которой этот модуль вообще существует отдельно:
**18:00 — это время последней записи, а не время закрытия.** Директор,
2026-07-29: «по врмеени 18 считаем ласт запись». Если бот скажет «работаем
до 18:00», пациент, которому назначено на 18:00, решит, что не успевает, и
отвалится. Поэтому наружу отдаётся формулировка «записываем до 18:00».

Второе: по выходным летом клиника не работает, а вне лета политика
неизвестна — на собственном сайте клиники три разных графика. Поэтому
`can_auto_answer` честно возвращает False для внелетних выходных: такой
вопрос уходит черновиком администратору, а не в автоответ.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

FACTS_PATH = Path(__file__).resolve().parents[2] / "data" / "clinic-facts.json"

_WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@lru_cache(maxsize=1)
def _facts() -> dict:
    with FACTS_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


@lru_cache(maxsize=1)
def tz() -> ZoneInfo:
    return ZoneInfo(_facts()["identity"]["timezone"])


def now() -> datetime:
    return datetime.now(tz())


def _parse_hhmm(raw: str) -> time:
    hh, mm = raw.split(":")
    return time(int(hh), int(mm))


@dataclass(frozen=True)
class DayStatus:
    """Что известно про конкретную дату."""

    day: date
    open_for_booking: bool
    opens: time | None
    last_appointment: time | None
    certain: bool
    reason: str

    @property
    def auto_answerable(self) -> bool:
        """Можно ли отвечать про этот день без человека."""
        return self.certain


def day_status(day: date) -> DayStatus:
    facts = _facts()
    hours = facts["hours"]
    key = _WEEKDAY_KEYS[day.weekday()]

    weekday_block = hours["weekdays"]
    if key in weekday_block["days"]:
        return DayStatus(
            day=day,
            open_for_booking=True,
            opens=_parse_hhmm(weekday_block["opens"]),
            last_appointment=_parse_hhmm(weekday_block["last_appointment"]),
            certain=True,
            reason="будний день",
        )

    summer = hours["summer_weekends"]
    if day.month in summer["months"]:
        return DayStatus(
            day=day,
            open_for_booking=bool(summer["open"]),
            opens=None,
            last_appointment=None,
            certain=True,
            reason="выходной, лето — не работаем",
        )

    return DayStatus(
        day=day,
        open_for_booking=False,
        opens=None,
        last_appointment=None,
        certain=False,
        reason="выходной вне лета — политика клиники не подтверждена",
    )


def is_booking_open(moment: datetime | None = None) -> bool:
    """Идёт ли сейчас окно, в которое можно записать пациента."""
    moment = moment or now()
    status = day_status(moment.date())
    if not status.open_for_booking or status.opens is None:
        return False
    assert status.last_appointment is not None
    return status.opens <= moment.timetz().replace(tzinfo=None) <= status.last_appointment


def next_booking_day(after: date | None = None, horizon_days: int = 14) -> DayStatus | None:
    """Ближайший день, про который мы уверенно знаем, что запись идёт."""
    cursor = (after or now().date()) + timedelta(days=1)
    for _ in range(horizon_days):
        status = day_status(cursor)
        if status.open_for_booking and status.certain:
            return status
        cursor += timedelta(days=1)
    return None


def can_auto_answer(question_about: date | None = None) -> bool:
    """Разрешено ли отвечать про график автоматически.

    Про будни и про летние выходные — да. Про выходные вне лета — нет,
    потому что клиника этого не подтвердила, а публичные источники
    противоречат друг другу.
    """
    if question_about is None:
        return True
    return day_status(question_about).certain


def describe_schedule() -> str:
    """Формулировка графика для пациента. Только подтверждённое."""
    hours = _facts()["hours"]
    wd = hours["weekdays"]
    summer = hours["summer_weekends"]
    line = f"Работаем с понедельника по пятницу, запись с {wd['opens']} до {wd['last_appointment']}."
    if not summer["open"] and now().month in summer["months"]:
        line += " По выходным летом не принимаем."
    return line


def describe_now() -> str:
    """Что сказать про «вы сейчас работаете?»."""
    moment = now()
    if is_booking_open(moment):
        status = day_status(moment.date())
        assert status.last_appointment is not None
        return f"Да, сегодня записываем до {status.last_appointment.strftime('%H:%M')}."

    nxt = next_booking_day(moment.date())
    if nxt is None:
        return describe_schedule()

    weekday_ru = ("в понедельник", "во вторник", "в среду", "в четверг",
                  "в пятницу", "в субботу", "в воскресенье")[nxt.day.weekday()]
    assert nxt.opens is not None and nxt.last_appointment is not None
    return (f"Сейчас уже нет, ближайшая запись {weekday_ru} "
            f"{nxt.day.strftime('%d.%m')} с {nxt.opens.strftime('%H:%M')} "
            f"до {nxt.last_appointment.strftime('%H:%M')}.")
