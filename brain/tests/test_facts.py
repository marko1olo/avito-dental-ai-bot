# -*- coding: utf-8 -*-
"""Тесты чтения фактов клиники. Запуск: python brain/tests/test_facts.py

Главное здесь — не то, что файл парсится, а что СТАТУСЫ соблюдаются.
Факт со статусом `internal` не должен утекать, факт со статусом `unknown`
не должен превращаться в ответ пациенту, а даты приходящего ортодонта
обязаны истекать: зашитые в файл 10 и 12 августа однажды станут прошлым,
и бот не имеет права приглашать на дату, которая была месяц назад.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import facts  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'ок  ' if condition else 'ФЕЙЛ'} {label}{'' if condition else ' — ' + detail}")
    if not condition:
        failures.append(label)


print("--- контакты ---")
c = facts.contacts()
check("адрес не пустой", bool(c.address), repr(c.address))
check("основной телефон городской 846", "846" in c.phone_primary, c.phone_primary)
check("резервный телефон мобильный 927", "927" in c.phone_secondary, c.phone_secondary)
check("два разных номера", c.phone_primary != c.phone_secondary)
check("метро перечислены", "Победа" in c.metro, c.metro)
flat = facts.clinic_contact_facts()
check("плоский набор содержит phone", flat.get("phone") == c.phone_primary)

print("\n--- консультация ---")
line = facts.consultation_line()
check("фраза непустая", bool(line), repr(line))
check("сказано про бесплатно", "бесплатн" in line.lower(), line)
check("статус pending_approval даёт мягкую добавку",
      "платите только за лечение" in line.lower(), line)

print("\n--- коронки ---")
offered = facts.crowns_offered()
retired = facts.crowns_retired()
check("предлагается ровно одна коронка", len(offered) == 1, str(offered))
check("это цирконий", "циркони" in offered[0].lower(), offered[0])
check("мёртвых позиций семь", len(retired) == 7, str(len(retired)))
check("металлокерамика среди мёртвых",
      any("еталлокерамик" in r for r in retired), str(retired))

print("\n--- ортодонтия: даты обязаны истекать ---")
before = facts.orthodontics_logistics(date(2026, 8, 1))
check("до 10.08 логистика есть", before is not None)
if before:
    check("названы обе ближайшие даты", "10.08" in before and "12.08" in before, before)
    check("сказано про КТ", "КТ" in before, before)
    check("цена НЕ названа", not any(ch.isdigit() and ch in "0123456789" for ch in
                                     before.replace("10.08", "").replace("12.08", "")
                                     .replace("15:00", "").replace("30", "")),
          before)

between = facts.orthodontics_logistics(date(2026, 8, 11))
check("11.08 показывает только 12.08", between is not None and "10.08" not in between,
      str(between))

after = facts.orthodontics_logistics(date(2026, 8, 13))
check("после 12.08 логистики нет — уходит администратору", after is None, str(after))

far = facts.orthodontics_logistics(date(2027, 1, 1))
check("через полгода тоже None", far is None, str(far))

print("\n--- internal не утекает ---")
internal = facts.internal_values()
check("почта врача помечена internal", len(internal) == 1, str(len(internal)))
leaked = [v for v in internal
          if any(v in (s or "") for s in
                 [before or "", between or "", line, str(flat), str(offered), str(retired)])]
check("ни один internal не попал в тексты для пациента", not leaked, str(leaked))

print("\n--- защита от тихого включения ортодонтических цен ---")
raw = facts.raw()
check("prices_may_be_quoted всё ещё false",
      raw["orthodontics"]["prices_may_be_quoted"] is False)
check("логистику называть можно",
      raw["orthodontics"]["logistics_may_be_quoted"] is True)

total = 22
print(f"\nИТОГ: {total - len(failures)}/{total}")
for f in failures:
    print(f"  провал: {f}")
raise SystemExit(1 if failures else 0)
