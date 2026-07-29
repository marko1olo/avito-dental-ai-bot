# -*- coding: utf-8 -*-
"""Гейт гибридного режима: что бот отвечает сам, а что уходит человеку.

Проектное решение, важное настолько, что его стоит объяснить прямо здесь.

Наивный подход — спросить LLM «это медицинский вопрос?» и по ответу решить,
отправлять автоматически или нет. Это **чёрный список**, и он ломается на
первом же неожиданном сообщении: всё, что классификатор не распознал как
опасное, летит пациенту без человека. Плюс лишний вызов модели на каждое
сообщение.

Здесь сделано наоборот — **белый список**. Автоматически уходит только то,
что уверенно распознано как безобидный факт (адрес, график, как добраться),
и только если в сообщении нет ни одного рискового маркера. Всё остальное,
включая непонятное, по умолчанию становится черновиком администратору.
LLM в этом гейте не участвует вообще: он детерминированный, бесплатный и
проверяемый тестом.

Цена решения: часть безобидных вопросов уйдёт человеку зря. Это правильный
размен — ошибка в сторону черновика стоит минуты администратора, ошибка в
сторону автоответа стоит пациента.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache
from pathlib import Path

QUOTES_PATH = Path(__file__).resolve().parents[2] / "data" / "patient-quotes.json"


class Route(str, Enum):
    AUTO = "auto"      # бот отвечает сам
    DRAFT = "draft"    # черновик администратору в Telegram
    IGNORE = "ignore"  # не реагировать (спам, пустое)


class Kind(str, Enum):
    SAFE_FACT = "safe_fact"
    PRICE = "price"
    MEDICAL = "medical"
    BOOKING = "booking"
    NO_QUOTE_TOPIC = "no_quote_topic"
    JUNK = "junk"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Decision:
    route: Route
    kind: Kind
    reason: str
    topic: str | None = None
    matched: tuple[str, ...] = field(default_factory=tuple)


# --- нормализация -----------------------------------------------------------

_PUNCT = re.compile(r"[^\w\s]+", re.UNICODE)
_SPACE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Складывает ё→е, режет пунктуацию и регистр. Без этого «Ещё» и «еще» —
    два разных слова, а пациенты пишут и так, и так."""
    folded = unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")
    return _SPACE.sub(" ", _PUNCT.sub(" ", folded)).strip()


# --- белый список: только это уходит без человека ---------------------------

SAFE_PATTERNS: dict[str, tuple[str, ...]] = {
    "address": (
        r"\bгде вы\b", r"\bгде наход", r"\bкакой адрес\b", r"\bадрес\b",
        r"\bкак (?:до)?(?:браться|ехать|доехать|найти)\b", r"\bна карте\b",
        r"\bметро\b", r"\bориентир\b",
    ),
    "schedule": (
        r"\bграфик\b", r"\bрежим работы\b", r"\bво сколько (?:вы )?(?:работ|откр|закр)",
        r"\bдо скольки\b", r"\bс каког[оь] часа\b", r"\bкогда вы работ",
        r"\bработаете ли\b", r"\bвы (?:сейчас )?работаете\b", r"\bприемные часы\b",
        r"\bв выходные\b", r"\bв субботу\b", r"\bв воскресенье\b",
    ),
    "parking": (r"\bпарковк", r"\bгде припарк"),
    "greeting": (
        r"^(?:здравствуйте|добрый день|добрый вечер|доброе утро|привет|здрасте|доброго дня)"
        r"(?:\s+\w+){0,2}$",
    ),
}

# --- маркеры, при которых автоответ запрещён независимо от белого списка ----

RISK_PATTERNS: dict[str, tuple[str, ...]] = {
    # Приставки здесь принципиальны: «подешевле» и «недорого» не имеют
    # границы слова перед корнем, поэтому \b на стыке ломает совпадение.
    "price": (
        r"\bсколько\b", r"\bцена\b", r"\bцены\b", r"\bстоит\b", r"\bстоимость\b",
        r"\bпочем\b", r"\bпрайс\b", r"скидк", r"рассрочк", r"\bкредит\b",
        r"дорог", r"дешев", r"бесплатн", r"\bбюджет",
    ),
    "medical": (
        r"\bболит\b", r"\bболь\b", r"\bноет\b", r"\bопух", r"\bфлюс\b", r"\bгной\b",
        r"\bкров", r"\bтемператур", r"\bвоспал", r"\bшатает", r"\bсломал",
        r"\bвыпал", r"\bчувствит", r"\bреагирует на\b", r"\bкист", r"\bгранулем",
        r"\bберемен", r"\bдиабет", r"\bаллерг", r"\bдавлени", r"\bсердц",
        r"\bчто со мной\b", r"\bчто делать\b", r"\bопасно ли\b", r"\bнорм(?:а|ально) ли\b",
        r"\bфото\b", r"\bснимок\b", r"\bпосмотрите\b",
    ),
    "booking": (
        r"\bзапиш", r"\bзапис(?:ать|аться|аться)\b", r"\bможно (?:ли )?прийти\b",
        r"\bсвободн", r"\bокошк", r"\bталон", r"\bна какое время\b",
        r"\bсегодня можно\b", r"\bзавтра можно\b", r"\bсрочно\b",
    ),
}

JUNK_PATTERNS: tuple[str, ...] = (
    r"^\W*$",
    r"\b(?:куплю|продам|сдам|сотрудничеств|реклам|продвижен|seo|трафик|подписчик)\b",
    r"\b(?:вакансия|резюме|работу ищу|ищу работу)\b",
)

MIN_MEANINGFUL_CHARS = 2
MAX_AUTO_LENGTH = 200  # длинное сообщение = история болезни, не вопрос про адрес


def _stem_pattern(ask: str) -> re.Pattern[str]:
    """Записи `ask` в data/patient-quotes.json — это НАЧАЛА слов, а не слова.

    Простое вхождение подстроки здесь не работает: «ребенку» не содержит
    «ребенок», «пломбу» не содержит «пломба». Падежей в русском больше, чем
    имеет смысл перечислять, поэтому в данных лежат корни («ребен», «пломб»),
    а совпадение ищется от границы слова. Граница обязательна: без неё «кт»
    нашлось бы внутри «доктор».
    """
    return re.compile(r"\b" + re.escape(normalize(ask)))


@lru_cache(maxsize=1)
def _no_quote_topics() -> tuple[tuple[str, tuple[re.Pattern[str], ...]], ...]:
    with QUOTES_PATH.open(encoding="utf-8") as fh:
        data = json.load(fh)
    return tuple(
        (t["key"], tuple(_stem_pattern(a) for a in t["ask"]))
        for t in data["no_quote_topics"]["topics"]
    )


@lru_cache(maxsize=1)
def _quotable_topics() -> tuple[tuple[str, tuple[re.Pattern[str], ...], bool], ...]:
    with QUOTES_PATH.open(encoding="utf-8") as fh:
        data = json.load(fh)
    return tuple(
        (q["key"], tuple(_stem_pattern(a) for a in q["ask"]), bool(q["quote_allowed"]))
        for q in data["quotes"]
    )


def _hits(text: str, patterns: tuple[str, ...]) -> list[str]:
    return [p for p in patterns if re.search(p, text)]


def classify(raw_text: str) -> Decision:
    """Единственная точка принятия решения. Возвращает маршрут, а не ответ."""
    text = normalize(raw_text)

    if len(text) < MIN_MEANINGFUL_CHARS or any(re.search(p, text) for p in JUNK_PATTERNS):
        return Decision(Route.IGNORE, Kind.JUNK, "пусто или не про лечение")

    # Темы, по которым цены нет или она не утверждена, — всегда человеку,
    # даже если пациент не спросил цену: ответ всё равно потребует оговорок.
    for topic, needles in _no_quote_topics():
        matched = [n.pattern for n in needles if n.search(text)]
        if matched:
            return Decision(Route.DRAFT, Kind.NO_QUOTE_TOPIC,
                            f"тема без утверждённой цены: {topic}",
                            topic=topic, matched=tuple(matched))

    risks = {name: _hits(text, pats) for name, pats in RISK_PATTERNS.items()}
    risks = {k: v for k, v in risks.items() if v}

    if risks:
        # Приоритет: медицина важнее цены, цена важнее записи.
        for name, kind in (("medical", Kind.MEDICAL), ("price", Kind.PRICE),
                           ("booking", Kind.BOOKING)):
            if name in risks:
                topic = None
                if kind is Kind.PRICE:
                    topic = next((key for key, needles, _ in _quotable_topics()
                                  if any(n.search(text) for n in needles)), None)
                return Decision(Route.DRAFT, kind,
                                f"рисковый маркер ({name}) — решает человек",
                                topic=topic, matched=tuple(risks[name]))

    safe_hits: list[str] = []
    safe_topic: str | None = None
    for topic, pats in SAFE_PATTERNS.items():
        found = _hits(text, pats)
        if found:
            safe_hits.extend(found)
            safe_topic = safe_topic or topic

    if safe_hits and len(raw_text) <= MAX_AUTO_LENGTH:
        return Decision(Route.AUTO, Kind.SAFE_FACT,
                        f"белый список: {safe_topic}",
                        topic=safe_topic, matched=tuple(safe_hits))

    if safe_hits:
        return Decision(Route.DRAFT, Kind.SAFE_FACT,
                        "белый список сработал, но сообщение слишком длинное "
                        "для автоответа — вероятно, там ещё и жалоба",
                        topic=safe_topic, matched=tuple(safe_hits))

    return Decision(Route.DRAFT, Kind.UNKNOWN,
                    "не распознано — по умолчанию человеку")
