# -*- coding: utf-8 -*-
"""Вето поверх ответа модели. Последний слой перед отправкой пациенту.

Зачем он существует. Нейронка ведёт диалог, и это правильно — она понимает
падежи, сленг и эмоцию несопоставимо лучше правил. Но у неё есть один изъян,
который не лечится промптом: **она способна назвать цифру, которой ей не
давали**. «Лечение пульпита будет 12 500 ₽» — синтаксически безупречное,
уверенное и полностью выдуманное предложение. Пациент придёт с этой цифрой
на ресепшен.

Регулярка не понимает контекст, зато она физически не может выдумать число.
Поэтому здесь она стоит НЕ вместо модели, а после неё: модель пишет, вето
сверяет каждую денежную величину со списком разрешённых и роняет ответ в
черновик администратору, если нашла лишнюю.

Вето никогда не удаляет ответ молча. Любое срабатывание — это черновик с
причиной: администратор видит и текст, и то, что в нём не понравилось.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
QUOTES_PATH = DATA / "patient-quotes.json"
FACTS_PATH = DATA / "clinic-facts.json"

MAX_REPLY_CHARS = 500
MAX_QUESTIONS = 2
BARE_NUMBER_IS_MONEY_FROM = 100


@dataclass(frozen=True)
class Verdict:
    ok: bool
    violations: tuple[str, ...] = field(default_factory=tuple)

    @property
    def reason(self) -> str:
        return "; ".join(self.violations) if self.violations else "проверки пройдены"


# --- разрешённые суммы ------------------------------------------------------

@lru_cache(maxsize=1)
def allowed_amounts() -> frozenset[int]:
    """Каждая сумма, которую боту разрешено произнести.

    Собирается ТОЛЬКО из записей с quote_allowed: true. Вариант внутри записи
    может отменить разрешение для себя (например, 4-канальный пульпит —
    экстраполяция, её называть нельзя).
    """
    with QUOTES_PATH.open(encoding="utf-8") as fh:
        data = json.load(fh)

    values: set[int] = set()
    for quote in data["quotes"]:
        if not quote.get("quote_allowed"):
            continue
        for key in ("price_rub", "min_rub", "max_rub"):
            if isinstance(quote.get(key), int):
                values.add(quote[key])
        addon = quote.get("upsell")
        if isinstance(addon, dict) and isinstance(addon.get("price_rub"), int):
            values.add(addon["price_rub"])
        for variant in quote.get("variants", ()):
            if variant.get("quote_allowed") is False:
                continue
            for key in ("price_rub", "min_rub", "max_rub"):
                if isinstance(variant.get(key), int):
                    values.add(variant[key])
    values.discard(0)  # «бесплатно» — слово, а не сумма
    return frozenset(values)


@lru_cache(maxsize=1)
def allowed_links() -> frozenset[str]:
    with FACTS_PATH.open(encoding="utf-8") as fh:
        ident = json.load(fh)["identity"]
    return frozenset({ident["site"], ident["vk"], ident["online_booking"]})


# --- вырезание всего, что выглядит как число, но не деньги ------------------

_MASKS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\+?\s*7[\s\-()]*\d{3}[\s\-()]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}"),  # телефон
    re.compile(r"\b\d{3}[\s\-]\d{2}[\s\-]\d{2}\b"),                              # 555-35-35
    re.compile(r"\b\d{1,2}\s*[:.]\s*\d{2}\b"),                                   # 18:00
    re.compile(r"\b(?:до|с|в|к|после|около)\s+\d{1,2}(?:\s*(?:часов|час|ч))?\b"), # до 18, с 9
    re.compile(r"\b\d+\s*(?:мин\w*|час\w*|дн\w*|день|недел\w*|месяц\w*|год\w*|лет)\b"),
    re.compile(r"\b\d{1,2}\s*(?:янв|фев|март|апрел|мая|июн|июл|август|сентябр|октябр|ноябр|декабр)\w*"),
    re.compile(r"\b\d+\s*(?:канал\w*|зуб\w*|штук\w*|раз\w*|этап\w*|посещен\w*|визит\w*)"),
    re.compile(r"\b\d+[,.]\d\s*(?:на|из)\s*(?:яндекс|карт\w*)", re.IGNORECASE),   # рейтинг 4,9
    re.compile(r"\b(?:4[,.]9|87|46)\b"),                                          # рейтинг/отзывы
    re.compile(r"\b\d+\s*%"),                                                     # «100%» — не сумма
)

_RANGE = re.compile(r"(\d+(?:[,.]\d+)?)\s*[-–—]\s*(\d+(?:[,.]\d+)?)\s*(тыс\w*|т\.?\s?р\.?|₽|руб\w*)")
_WITH_UNIT = re.compile(r"(\d[\d\s]*(?:[,.]\d+)?)\s*(тыс\w*|т\.?\s?р\.?|₽|руб\w*)")
_BARE = re.compile(r"\b(\d[\d\s]{2,})\b")


def _to_rub(raw: str, unit: str) -> int:
    value = float(raw.replace(" ", "").replace(",", "."))
    if unit.startswith(("тыс", "т.р", "тр", "т р")):
        value *= 1000
    return int(round(value))


def money_amounts(text: str) -> list[int]:
    """Все суммы, которые пациент прочтёт как цену."""
    masked = text
    for pattern in _MASKS:
        masked = pattern.sub(lambda m: "#" * len(m.group(0)), masked)

    found: list[int] = []
    consumed = masked

    for match in _RANGE.finditer(masked):
        unit = match.group(3)
        found.append(_to_rub(match.group(1), unit))
        found.append(_to_rub(match.group(2), unit))
        consumed = consumed.replace(match.group(0), "#" * len(match.group(0)), 1)

    for match in _WITH_UNIT.finditer(consumed):
        found.append(_to_rub(match.group(1), match.group(2)))
        consumed = consumed.replace(match.group(0), "#" * len(match.group(0)), 1)

    for match in _BARE.finditer(consumed):
        value = int(match.group(1).replace(" ", ""))
        if value >= BARE_NUMBER_IS_MONEY_FROM:
            found.append(value)

    return found


# --- содержательные запреты -------------------------------------------------

DIAGNOSIS = re.compile(
    r"\bу вас\s+(?:кариес|пульпит|периодонтит|киста|гранулема|флюс|пародонтит)\b"
    r"|\bэто\s+(?:кариес|пульпит|периодонтит|точно)\b"
    r"|\bнужно\s+(?:удал|депульпир|лечить канал)"
    r"|\bпридется\s+удал"
    r"|\bдиагноз\b",
    re.IGNORECASE)

PROMISE = re.compile(
    r"\bгарантир\w*|\b100\s*%|\bточно помож|\bбез боли\b|\bнавсегда\b"
    r"|\bобещаю\b|\bизлеч\w*\s+полностью",
    re.IGNORECASE)

CONTACT_LEAK = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+|https?://\S+|\bwww\.\S+")
ORTHO_TERMS = re.compile(r"\bбрекет\w*|\bэлайнер\w*|\bортодонт\w*|\bкапп?[аы]\b|\bприкус\w*", re.IGNORECASE)


def check(reply: str, *, topic: str | None = None) -> Verdict:
    """Пропускать ли этот текст пациенту."""
    violations: list[str] = []

    if len(reply) > MAX_REPLY_CHARS:
        violations.append(f"слишком длинно ({len(reply)} знаков, лимит {MAX_REPLY_CHARS})")

    if reply.count("?") > MAX_QUESTIONS:
        violations.append(f"больше {MAX_QUESTIONS} вопросов подряд ({reply.count('?')})")

    for link in CONTACT_LEAK.findall(reply):
        if link not in allowed_links():
            violations.append(f"посторонний контакт или ссылка: {link}")

    amounts = money_amounts(reply)
    permitted = allowed_amounts()
    for amount in amounts:
        if amount not in permitted:
            violations.append(f"сумма {amount} ₽ не входит в разрешённые котировки")

    is_ortho = topic == "orthodontics" or bool(ORTHO_TERMS.search(reply))
    if is_ortho and amounts:
        violations.append("цена в ортодонтическом контексте — прайс не утверждён")

    if DIAGNOSIS.search(reply):
        violations.append("постановка диагноза")

    if PROMISE.search(reply):
        violations.append("обещание результата или гарантия")

    return Verdict(not violations, tuple(violations))
