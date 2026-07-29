# -*- coding: utf-8 -*-
"""Типизированное чтение data/clinic-facts.json.

Существует отдельно от сборщика промпта по двум причинам. Во-первых, факты
клиники нужны не только промпту: их читает деградированный режим роутера,
скраббер ПД (чтобы не вычищать наш собственный телефон) и панель Telegram.
Во-вторых, у каждого блока в этом файле есть поле `status`, и трактовать его —
отдельная ответственность, а не деталь шаблонизации.

Смысл `status` — единственное, что здесь по-настоящему важно:

    confirmed        — подтверждено владельцем, бот отвечает автоматически
    pending_approval — решено, но не утверждено формально; отвечаем мягче
    unknown          — бот НЕ отвечает, вопрос уходит администратору
    internal         — не показывать пациенту ни в каком виде

Слой, который читает факт со статусом `unknown` или `internal` и всё равно
подставляет его в текст для пациента, — это баг, а не вольность. Поэтому
доступ к таким полям идёт через явные функции, а не через словарь.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

FACTS_PATH = Path(__file__).resolve().parent.parent / "data" / "clinic-facts.json"

QUOTABLE_STATUSES = frozenset({"confirmed", "pending_approval"})


@lru_cache(maxsize=1)
def raw() -> dict:
    with FACTS_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


@dataclass(frozen=True)
class Contacts:
    address: str
    district: str
    metro: str
    phone_primary: str
    phone_secondary: str
    site: str
    booking: str
    vk: str

    @property
    def all_phones(self) -> tuple[str, str]:
        return (self.phone_primary, self.phone_secondary)


@lru_cache(maxsize=1)
def contacts() -> Contacts:
    data = raw()
    ident = data["identity"]
    phones = data["phones"]
    return Contacts(
        address=ident["address"],
        district=ident["district"],
        metro=" / ".join(ident["metro"]),
        phone_primary=phones["primary"]["number"],
        phone_secondary=phones["secondary"]["number"],
        site=ident["site"],
        booking=ident["online_booking"],
        vk=ident["vk"],
    )


def clinic_contact_facts() -> dict[str, str]:
    """Плоский набор строк для подстановки в шаблоны ответов."""
    c = contacts()
    return {
        "address": c.address,
        "district": c.district,
        "metro": c.metro,
        "phone": c.phone_primary,
        "phone_alt": c.phone_secondary,
        "booking": c.booking,
    }


def consultation_line() -> str:
    """Фраза про консультацию с поправкой на то, утверждена ли она формально."""
    block = raw()["consultation"]
    if block["status"] not in QUOTABLE_STATUSES:
        return ""
    base = block["phrase"]
    if block["status"] == "pending_approval":
        # Решение владельца есть, приказа нет. Формулировка не должна звучать
        # как незыблемое правило клиники, но и мяться тоже нельзя — это главный
        # крючок всей стратегии.
        return f"{base} Осмотр и план лечения бесплатны, платите только за лечение."
    return base


def crowns_offered() -> list[str]:
    """Что реально предлагается. Семь позиций прейскуранта мертвы."""
    return [item["name"] for item in raw()["crowns"]["offered"]]


def crowns_retired() -> list[str]:
    return [item["name"] for item in raw()["crowns"]["not_offered_anymore"]]


def orthodontics_logistics(today) -> str | None:
    """Логистика приходящего ортодонта. Цены не называются: не утверждены.

    Даты в файле жёстко зашиты и истекают. Прошедшие отфильтровываются, и если
    будущих не осталось, функция возвращает None — вопрос уходит администратору,
    а не превращается в приглашение на дату, которая была месяц назад.
    """
    from datetime import date as _date

    block = raw()["orthodontics"]
    if block["prices_may_be_quoted"]:
        raise AssertionError(
            "orthodontics.prices_may_be_quoted стал true — обновите ortho-prices.json "
            "и снимите запрет в guard.py осознанно, а не мимоходом")

    spec = block["visiting_specialist"]
    upcoming = []
    for slot in spec["known_dates_2026"]:
        when = _date.fromisoformat(slot["date"])
        if when >= today:
            upcoming.append((when, slot["from"]))
    if not upcoming:
        return None

    upcoming.sort()
    shown = upcoming[:2]
    dates = " и ".join(w.strftime("%d.%m") for w, _ in shown)
    return (f"Ортодонт принимает по записи в отдельные дни, ближайшие — {dates} "
            f"с {shown[0][1]}, консультация {spec['consultation_minutes']} минут. "
            f"Для неё нужен {spec['patient_must_bring']}. "
            "По стоимости вас сориентирует администратор.")


def internal_values() -> frozenset[str]:
    """Всё, что помечено `internal` и не должно попасть ни в промпт, ни в чат.

    Используется тестами как список того, чего быть не должно, — чтобы проверка
    не зависела от того, помнил ли автор конкретного слоя про почту врача.
    """
    values: set[str] = set()
    email = raw()["orthodontics"]["visiting_specialist"]["email"]
    if email.get("status") == "internal":
        values.add(email["value"])
    return frozenset(values)
