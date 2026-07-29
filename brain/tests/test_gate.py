# -*- coding: utf-8 -*-
"""Тесты гейта и графика. Запуск: python brain/tests/test_gate.py

Гейт детерминированный, поэтому его поведение можно зафиксировать целиком.
Главное, что здесь проверяется — не «работает ли», а **куда падает
неопределённость**: любое сообщение, которое гейт не понял, обязано уйти
человеком, а не пациенту.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gate"))

import hours  # noqa: E402
import intent  # noqa: E402
from intent import Kind, Route  # noqa: E402

# (сообщение, ожидаемый маршрут, ожидаемый тип)
CASES: list[tuple[str, Route, Kind]] = [
    # --- белый список: уходит без человека ---
    ("Здравствуйте", Route.AUTO, Kind.SAFE_FACT),
    ("какой у вас адрес?", Route.AUTO, Kind.SAFE_FACT),
    ("Где вы находитесь?", Route.AUTO, Kind.SAFE_FACT),
    ("как до вас добраться от метро", Route.AUTO, Kind.SAFE_FACT),
    ("до скольки вы работаете", Route.AUTO, Kind.SAFE_FACT),
    ("вы сейчас работаете?", Route.AUTO, Kind.SAFE_FACT),
    ("работаете ли в субботу", Route.AUTO, Kind.SAFE_FACT),
    ("парковка есть рядом?", Route.AUTO, Kind.SAFE_FACT),

    # --- цена: всегда человеку, даже про подтверждённый кариес ---
    ("сколько стоит вылечить кариес", Route.DRAFT, Kind.PRICE),
    ("почем коронка на цирконии", Route.DRAFT, Kind.PRICE),
    ("а подешевле варианты есть?", Route.DRAFT, Kind.PRICE),
    ("рассрочка бывает?", Route.DRAFT, Kind.PRICE),
    ("консультация бесплатная?", Route.DRAFT, Kind.PRICE),

    # --- медицина: человеку безусловно ---
    ("у меня болит зуб снизу справа", Route.DRAFT, Kind.MEDICAL),
    ("опухла щека и температура 38", Route.DRAFT, Kind.MEDICAL),
    ("выпала пломба что делать", Route.DRAFT, Kind.MEDICAL),
    ("я беременна можно лечить зубы", Route.DRAFT, Kind.MEDICAL),
    ("посмотрите фото пожалуйста", Route.DRAFT, Kind.MEDICAL),

    # --- запись ---
    ("можно записаться на завтра", Route.DRAFT, Kind.BOOKING),
    ("есть свободные окошки сегодня", Route.DRAFT, Kind.BOOKING),

    # --- темы без утверждённой цены: приоритет выше рисковых маркеров ---
    ("сколько стоят брекеты", Route.DRAFT, Kind.NO_QUOTE_TOPIC),
    ("делаете элайнеры?", Route.DRAFT, Kind.NO_QUOTE_TOPIC),
    ("ребенку 5 лет примете?", Route.DRAFT, Kind.NO_QUOTE_TOPIC),
    ("рентген у вас делают", Route.DRAFT, Kind.NO_QUOTE_TOPIC),
    ("виниры сколько", Route.DRAFT, Kind.NO_QUOTE_TOPIC),

    # --- мусор ---
    ("", Route.IGNORE, Kind.JUNK),
    ("Предлагаю продвижение SEO вашему сайту", Route.IGNORE, Kind.JUNK),

    # --- неизвестное падает человеку, а не пациенту ---
    ("а вы что вообще умеете", Route.DRAFT, Kind.UNKNOWN),
    ("Ольга Петровна на месте?", Route.DRAFT, Kind.UNKNOWN),

    # --- ловушка: адрес + жалоба в одном сообщении ---
    ("Здравствуйте, подскажите адрес. У меня уже третий день ноет зуб, "
     "щеку раздуло, не могу спать, раньше лечили этот зуб дважды и оба раза "
     "пломба выпадала, боюсь что придется удалять", Route.DRAFT, Kind.MEDICAL),
]


def run() -> int:
    failures: list[str] = []
    for text, want_route, want_kind in CASES:
        got = intent.classify(text)
        if got.route is not want_route or got.kind is not want_kind:
            failures.append(
                f"  {text[:58]!r}\n"
                f"      ждали {want_route.value}/{want_kind.value}, "
                f"получили {got.route.value}/{got.kind.value} ({got.reason})"
            )
    print(f"intent: {len(CASES) - len(failures)}/{len(CASES)} ок")
    for f in failures:
        print(f)

    # Ни одно сообщение не должно уходить автоматически с рисковым маркером.
    leaks = [t for t, _, _ in CASES
             if intent.classify(t).route is Route.AUTO
             and any(intent._hits(intent.normalize(t), p)
                     for p in intent.RISK_PATTERNS.values())]
    if leaks:
        failures.append(f"  УТЕЧКА: автоответ на рисковое сообщение: {leaks}")
        print(failures[-1])

    # --- график ---
    print("\nhours:")
    print(f"  сейчас: {hours.now().strftime('%Y-%m-%d %H:%M %Z')}")
    print(f"  расписание: {hours.describe_schedule()}")
    print(f"  сейчас работаем: {hours.describe_now()}")

    checks = [
        ("будни (ср 2026-08-05)", date(2026, 8, 5), True, True),
        ("летняя суббота (2026-08-08)", date(2026, 8, 8), False, True),
        ("зимняя суббота (2026-11-07)", date(2026, 11, 7), False, False),
    ]
    for label, day, want_open, want_certain in checks:
        st = hours.day_status(day)
        ok = st.open_for_booking is want_open and st.certain is want_certain
        print(f"  {'ок  ' if ok else 'ФЕЙЛ'} {label}: "
              f"запись={st.open_for_booking} уверенно={st.certain} ({st.reason})")
        if not ok:
            failures.append(f"  hours: {label}")

    nxt = hours.next_booking_day()
    print(f"  ближайший день записи: {nxt.day if nxt else 'не найден'}")
    if nxt is None:
        failures.append("  hours: не нашёл ближайший рабочий день в горизонте 14 дней")

    print("\nИТОГ:", "всё зелёное" if not failures else f"{len(failures)} провал(ов)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run())
