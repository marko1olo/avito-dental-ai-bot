# -*- coding: utf-8 -*-
"""Тесты дожима. Запуск: python brain/tests/test_followup.py

Проверяется в первую очередь, что модуль МОЛЧИТ там, где должен молчать.
Лишний дожим стоит дороже пропущенного: пропущенный — один лид, лишний —
жалоба на спам и риск аккаунта Авито.

Все сценарии считаются от рабочего момента (среда 14:00 по Самаре), потому
что вне окна записи дожим не отправляется вообще и остальные условия не
проверялись бы.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import followup  # noqa: E402
from followup import DialogState, Route  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gate"))
import hours  # noqa: E402

NOW = datetime(2026, 8, 5, 14, 0, tzinfo=hours.tz())      # среда, окно записи открыто
NIGHT = datetime(2026, 8, 5, 23, 30, tzinfo=hours.tz())   # среда, ночь
SATURDAY = datetime(2026, 8, 8, 12, 0, tzinfo=hours.tz())  # летняя суббота


def st(**kw) -> DialogState:
    base = dict(our_last_message_at=NOW - timedelta(hours=4),
                patient_last_message_at=NOW - timedelta(hours=4, minutes=5))
    base.update(kw)
    return DialogState(**base)


# (описание, состояние, момент, ожидаем номер дожима или None)
CASES: list[tuple[str, DialogState, datetime, int | None]] = [
    ("молчит 4 ч в рабочее время -> дожим 1", st(), NOW, 1),
    ("молчит 2 ч -> ещё рано", st(our_last_message_at=NOW - timedelta(hours=2)), NOW, None),
    ("молчит 3 ч ровно -> уже можно",
     st(our_last_message_at=NOW - timedelta(hours=3)), NOW, 1),
    ("молчит 3 суток -> диалог остыл, не пишем",
     st(our_last_message_at=NOW - timedelta(hours=72)), NOW, None),

    ("ночь -> не пишем, позвонить всё равно нельзя",
     st(our_last_message_at=NIGHT - timedelta(hours=4)), NIGHT, None),
    ("летняя суббота -> не пишем",
     st(our_last_message_at=SATURDAY - timedelta(hours=4)), SATURDAY, None),

    ("пациент ответил последним -> он не молчит",
     st(patient_last_message_at=NOW - timedelta(minutes=10)), NOW, None),

    ("телефон получен -> дожим не нужен", st(phone_captured=True), NOW, None),
    ("пациент сам написал номер -> не нужен",
     st(patient_texts=("мой номер 89271234567",)), NOW, None),
    ("пациент обещал позвонить -> не нужен",
     st(patient_texts=("хорошо, я сам позвоню завтра",)), NOW, None),

    ("отказ «не надо» -> стоп", st(patient_texts=("не надо спасибо",)), NOW, None),
    ("отказ «уже вылечил» -> стоп", st(patient_texts=("уже вылечил в другой",)), NOW, None),
    ("отказ «не актуально» -> стоп", st(patient_texts=("уже не актуально",)), NOW, None),

    ("администратор перехватил -> стоп", st(human_took_over=True), NOW, None),
    ("спам -> стоп", st(is_spam=True), NOW, None),

    ("первый дожим 10 ч назад -> для второго рано",
     st(followups_sent=1, last_followup_at=NOW - timedelta(hours=10)), NOW, None),
    ("первый дожим 25 ч назад -> дожим 2",
     st(followups_sent=1, last_followup_at=NOW - timedelta(hours=25)), NOW, 2),
    ("два дожима отправлено -> третьего нет",
     st(followups_sent=2, last_followup_at=NOW - timedelta(hours=48)), NOW, None),
]


def run() -> int:
    failures: list[str] = []

    for label, state, moment, want in CASES:
        got = followup.plan(state, moment)
        got_n = got.number if got else None
        ok = got_n == want
        if not ok:
            failures.append(f"{label}: ждали {want}, получили {got_n}")
        print(f"  {'ок  ' if ok else 'ФЕЙЛ'} {label}"
              f"{'' if not got else '  [' + got.route.value + '] ' + got.reason}")

    # Маршрутизация: по умолчанию оба дожима — черновик.
    first = followup.plan(st(), NOW)
    assert first is not None
    if first.route is not Route.DRAFT:
        failures.append("первый дожим по умолчанию должен быть черновиком")

    second = followup.plan(
        st(followups_sent=1, last_followup_at=NOW - timedelta(hours=25)), NOW)
    assert second is not None
    if second.route is not Route.DRAFT:
        failures.append("второй дожим обязан быть черновиком")

    # Даже если первый переведён в авто, второй остаётся черновиком.
    promoted = followup.plan(st(), NOW, first_route=Route.AUTO)
    assert promoted is not None
    if promoted.route is not Route.AUTO:
        failures.append("первый дожим не переводится в авто параметром")
    promoted_second = followup.plan(
        st(followups_sent=1, last_followup_at=NOW - timedelta(hours=25)), NOW,
        first_route=Route.AUTO)
    assert promoted_second is not None
    if promoted_second.route is not Route.DRAFT:
        first_route_leak = "второй дожим стал авто вслед за первым — так быть не должно"
        failures.append(first_route_leak)

    print(f"\nмаршруты: первый={first.route.value}, второй={second.route.value}, "
          f"первый с промоушеном={promoted.route.value}, "
          f"второй при промоушене={promoted_second.route.value}")

    # Вето не должно ругаться на текст дожимов.
    import guard  # noqa: E402
    for label, text in (("дожим 1", followup.FIRST_TEXT), ("дожим 2", followup.SECOND_TEXT)):
        verdict = guard.check(text)
        if not verdict.ok:
            failures.append(f"{label} не проходит вето: {verdict.reason}")
        print(f"  {'ок  ' if verdict.ok else 'ФЕЙЛ'} {label} проходит вето")

    total = len(CASES) + 6
    print(f"\nИТОГ: {total - len(failures)}/{total}")
    for f in failures:
        print(f"  {f}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run())
