# -*- coding: utf-8 -*-
"""Тесты каскада попыток. Запуск: python -X utf8 brain/tests/test_client.py

Без сети и без настоящих ключей: подменяется ровно одна функция,
`transport.post_chat`, — та, у которой единственная ответственность «сходить в
сокет». Ключи берутся из окружения, поэтому в окружение теста кладутся
поддельные, но правдоподобной формы (`AIza…`, `gsk_…`): проверка на утечку
имеет смысл только против ключа, который скраббер обязан узнать.

Порядок разделов — по цене ошибки, а не по порядку функций в модуле.

1. **429 не банит модель.** Самая дорогая регрессия пакета: тело отказа по
   квоте содержит `quota_limit_value: 500 per day`, и разбор, который смотрит
   5xx раньше 429, банит здоровую модель на 20 минут при живом втором ключе.
   Тело в этом тесте специально содержит и «500», и слово «deadline».
2. **Утечка ключа.** Проверяются все четыре выхода наружу: журнал, `health()`,
   `repr` результата и файлы состояния. Провайдеры отдают тела вида
   `401 invalid api key <ключ>`, и такое тело здесь подаётся дословно.
3. **Ротация ключей.** Каждая попытка обязана взять другой ключ; повтор одного
   и того же означает, что кулдаун съедает попытки вместо запросов.
4. Остальные коды провала и деградация до `text is None`.

Состояние изолировано: `AVITO_LLM_STATE_DIR` уводится во временный каталог
(имя переменной — из `state.py`), каталог удаляется в `finally`. Тест, который
сбивает боевые баны моделей, ломает работающего бота сильнее, чем ловит
регрессий.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

_BRAIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_BRAIN))
sys.path.insert(0, str(_BRAIN / "gate"))

# Каталог состояния подменяется ДО первого обращения к state: боевые файлы
# кулдаунов лежат в корне проекта, и тест не имеет права их трогать.
_WORKDIR = Path(tempfile.mkdtemp(prefix="avito-llm-test-"))
os.environ["AVITO_LLM_STATE_DIR"] = str(_WORKDIR)

# Поддельные ключи правдоподобной формы. Приставки настоящие — именно их
# опознаёт keys.redact() без белого списка.
GOOGLE_KEYS = [
    "AIzaSyTESTkeyGOOGLEoneZZ0000001",
    "AIzaSyTESTkeyGOOGLEtwoZZ0000002",
    "AIzaSyTESTkeyGOOGLEthreeZ000003",
]
GROQ_KEYS = [
    "gsk_TESTkeyGROQoneZZZZZZZZZ00001",
    "gsk_TESTkeyGROQtwoZZZZZZZZZ00002",
]
ALL_KEYS = GOOGLE_KEYS + GROQ_KEYS

os.environ["GOOGLE_API_KEYS"] = ",".join(GOOGLE_KEYS)
os.environ["GROQ_API_KEYS"] = ",".join(GROQ_KEYS)

from llm import cascade, keys, state, transport  # noqa: E402
from llm import client  # noqa: E402

# Тело отказа по квоте — настоящей формы Gemini. Содержит «500» в описании
# лимита и слово «deadline»: и то, и другое обязано быть прочитано как квота,
# а не как перегрузка.
QUOTA_BODY = json.dumps({
    "error": {
        "code": 429,
        "status": "RESOURCE_EXHAUSTED",
        "message": ("You exceeded your current quota. quota_limit_value: 500 per day. "
                    "Retry after deadline expires."),
    }
})

# Тело, каким его отдаёт провайдер при отозванном ключе: с ключом внутри.
def denied_body(api_key: str) -> str:
    return json.dumps({"error": {"code": 401, "message": f"invalid api key {api_key}"}})


OVERLOAD_BODY = json.dumps({"error": {"code": 503, "message": "model is overloaded"}})


def ok_body(text: str = "Здравствуйте. Приходите на бесплатную консультацию.") -> str:
    return json.dumps({"choices": [{"message": {"role": "assistant", "content": text}}]})


EMPTY_BODY = json.dumps({"choices": [{"message": {"role": "assistant", "content": "   "}}]})


class Checks:
    """Счётчик проверок: печатает каждую строкой, копит провалы, считает итог."""

    def __init__(self) -> None:
        self.total = 0
        self.failures: list[str] = []

    def ok(self, label: str, condition: bool, detail: str = "") -> bool:
        self.total += 1
        if not condition:
            self.failures.append(f"{label}{': ' + detail if detail else ''}")
        print(f"  {'ок  ' if condition else 'ФЕЙЛ'} {label}"
              f"{'' if condition or not detail else '  -- ' + detail}")
        return bool(condition)

    def eq(self, label: str, got: object, want: object) -> bool:
        return self.ok(label, got == want, f"ждали {want!r}, получили {got!r}")


class Provider:
    """Поддельный `transport.post_chat`: отвечает по сценарию и всё записывает.

    Сценарий — список ответов вида `(статус, тело)`; последний повторяется,
    когда список исчерпан. Записываются использованные ключи, модели и payload:
    без этого «ротация ключей» проверялась бы по журналу, то есть по строке,
    которую тест же и форматирует.
    """

    def __init__(self, script: list[tuple[int, str]] | None = None,
                 always: tuple[int, str] | None = None) -> None:
        self.script = list(script or [])
        self.always = always
        self.used_keys: list[str] = []
        self.models: list[str] = []
        self.payloads: list[dict] = []

    async def __call__(self, provider: str, api_key: str, payload: dict,
                       timeout_s: float) -> transport.HttpReply:
        self.used_keys.append(api_key)
        self.models.append(f"{provider}:{payload.get('model')}")
        self.payloads.append(payload)
        if self.script:
            status, body = self.script.pop(0)
        elif self.always is not None:
            status, body = self.always
        else:
            status, body = 200, ok_body()
        if callable(body):  # тело, зависящее от ключа (401 с ключом внутри)
            body = body(api_key)
        # Настоящий HttpReply, а не заглушка: вымарывание в его конструкторе —
        # часть того, что проверяется.
        return transport.HttpReply(status, body, 7)

    @property
    def calls(self) -> int:
        return len(self.used_keys)


class LogSink(logging.Handler):
    """Собирает всё, что пакет пишет в журнал, уже после фильтра вымарывания."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def reset_state(*, keep_failure: bool = False) -> None:
    """Чистое состояние перед проверкой: ни кулдаунов, ни банов.

    `keep_failure` оставляет файл последнего провала: он нужен там, где
    проверяется, что удачный вызов НЕ стирает историю провалов — `health()`
    отвечает на вопрос «что было в последний раз», а не «всё ли хорошо сейчас».
    """
    names = [state.KEY_COOLDOWN_FILE, state.MODEL_BAN_FILE]
    if not keep_failure:
        names.append(state.LAST_FAILURE_FILE)
    for name in names:
        (_WORKDIR / name).unlink(missing_ok=True)


def call(provider: Provider, **kwargs) -> client.LlmResult:
    """Вызов ровно в той форме, в которой его делает brain/router.py."""
    transport.post_chat = provider  # type: ignore[assignment]
    try:
        return asyncio.run(client.complete(
            "системный промпт", "болит зуб, сколько стоит осмотр?",
            temperature=kwargs.pop("temperature", 0.35),
            max_tokens=kwargs.pop("max_tokens", 400),
            purpose=kwargs.pop("purpose", "reply"),
            **kwargs,
        ))
    finally:
        transport.post_chat = _REAL_POST_CHAT  # type: ignore[assignment]


_REAL_POST_CHAT = transport.post_chat


# --- 1. контракт --------------------------------------------------------------

def check_contract(c: Checks) -> None:
    print("\n--- контракт LlmResult и подписи ---")
    reset_state()
    result = call(Provider())
    fields = tuple(result.__dataclass_fields__)
    c.eq("поля LlmResult", fields,
         ("text", "model", "provider", "attempts", "latency_ms", "failure"))
    c.ok("LlmResult frozen", result.__dataclass_params__.frozen)
    c.eq("успех: text", result.text,
         "Здравствуйте. Приходите на бесплатную консультацию.")
    c.eq("успех: failure is None", result.failure, None)
    c.eq("успех: attempts", result.attempts, 1)
    c.eq("успех: модель первая в каскаде", result.model, cascade.CHAT[0].id)
    c.eq("успех: провайдер", result.provider, cascade.CHAT[0].provider)
    c.ok("успех: latency_ms >= 0", isinstance(result.latency_ms, int) and result.latency_ms >= 0,
         f"{result.latency_ms!r}")
    c.ok("успех: модель не забанена", state.model_bans() == {})
    c.ok("успех: ключ не остывает", state.key_cooldowns() == {})

    health = client.health()
    c.eq("health: ключи все", health["keys_total"], len(ALL_KEYS))
    c.eq("health: в кулдауне никого", health["keys_on_cooldown"], 0)
    c.eq("health: банов нет", health["models_banned"], [])
    c.eq("health: набор полей", sorted(health),
         ["keys_on_cooldown", "keys_total", "last_failure", "models_banned"])


# --- 2. 429 раньше 5xx -------------------------------------------------------

def check_quota_does_not_ban(c: Checks) -> None:
    print("\n--- 429 с quota_limit_value: 500 per day НЕ банит модель ---")
    reset_state()
    provider = Provider(script=[(429, QUOTA_BODY), (200, ok_body("ответ со второго ключа"))])
    result = call(provider)

    c.eq("текст получен со второй попытки", result.text, "ответ со второго ключа")
    c.eq("attempts", result.attempts, 2)
    c.ok("модель НЕ забанена (главная регрессия)", state.model_bans() == {},
         f"баны: {sorted(state.model_bans())}")
    c.eq("ключ отправлен в кулдаун", len(state.key_cooldowns()), 1)
    c.eq("вторая попытка — другой ключ", len(set(provider.used_keys)), 2)
    c.eq("та же модель, что и на первой попытке",
         provider.models[0], provider.models[1])
    c.eq("модель в ответе — первая в каскаде", result.model, cascade.CHAT[0].id)

    reset_state()
    # То же тело, но статусом 200 быть не может: проверяем, что слово deadline
    # в теле 429 не переводит разбор в перегрузку и на других моделях.
    provider = Provider(always=(429, QUOTA_BODY))
    result = call(provider)
    c.eq("все 429: failure", result.failure, "rate_limited")
    c.eq("все 429: text is None", result.text, None)
    c.ok("все 429: ни одна модель не забанена", state.model_bans() == {},
         f"баны: {sorted(state.model_bans())}")
    c.eq("все 429: остывают все использованные ключи",
         len(state.key_cooldowns()), len(set(provider.used_keys)))
    c.eq("все 429: каждая попытка — свой ключ",
         len(set(provider.used_keys)), provider.calls)


# --- 3. 5xx и таймаут банят модель -------------------------------------------

def check_server_error_bans(c: Checks) -> None:
    print("\n--- 5xx банит модель, таймаут тоже ---")
    reset_state()
    provider = Provider(always=(503, OVERLOAD_BODY))
    result = call(provider)

    c.eq("503: failure", result.failure, "model_overloaded")
    c.eq("503: text is None", result.text, None)
    c.eq("503: забанен весь каскад", sorted(state.model_bans()),
         sorted(m.key for m in cascade.CHAT))
    c.eq("503: по одной попытке на модель", provider.calls, len(cascade.CHAT))
    c.ok("503: кулдаунов ключей нет", state.key_cooldowns() == {},
         f"{sorted(state.key_cooldowns())}")
    c.eq("503: бан на MODEL_BAN_SECONDS из state.py",
         round(min(state.model_bans().values()) - __import__("time").time()) // 60,
         state.MODEL_BAN_SECONDS // 60)

    # Каскад пуст — сети не касаемся вовсе.
    provider = Provider(always=(200, ok_body()))
    result = call(provider)
    c.eq("каскад забанен: failure", result.failure, "all_models_banned")
    c.eq("каскад забанен: запросов не было", provider.calls, 0)
    c.eq("каскад забанен: attempts", result.attempts, 0)

    reset_state()
    provider = Provider(always=(transport.STATUS_TIMEOUT, "timeout"))
    result = call(provider)
    c.eq("таймаут: failure", result.failure, "timeout")
    c.eq("таймаут: модели забанены", len(state.model_bans()), len(cascade.CHAT))


# --- 4. 401/403 — следующий ключ ---------------------------------------------

def check_key_denied(c: Checks) -> None:
    print("\n--- 401 с ключом в теле: следующий ключ, без бана и без кулдауна ---")
    reset_state()
    provider = Provider(script=[(401, denied_body), (200, ok_body("ответ после 401"))])
    result = call(provider)

    c.eq("401: текст со второго ключа", result.text, "ответ после 401")
    c.eq("401: attempts", result.attempts, 2)
    c.ok("401: модель не забанена", state.model_bans() == {})
    c.ok("401: кулдаун не ставится (через 300 с ключ не оживёт)",
         state.key_cooldowns() == {})
    c.eq("401: ключи разные", len(set(provider.used_keys)), 2)

    reset_state()
    provider = Provider(always=(403, denied_body))
    result = call(provider)
    c.eq("403 везде: text is None", result.text, None)
    c.eq("403 везде: failure", result.failure, "key_denied")
    c.ok("403 везде: модели не забанены", state.model_bans() == {})


# --- 5. пустой пул и полный кулдаун ------------------------------------------

def check_no_keys(c: Checks) -> None:
    print("\n--- нет ключей и все ключи в кулдауне ---")
    reset_state()
    saved = (os.environ["GOOGLE_API_KEYS"], os.environ["GROQ_API_KEYS"])
    os.environ["GOOGLE_API_KEYS"] = ""
    os.environ["GROQ_API_KEYS"] = ""
    try:
        provider = Provider(always=(200, ok_body()))
        result = call(provider)
        c.eq("пустой пул: failure", result.failure, "no_keys")
        c.eq("пустой пул: text is None", result.text, None)
        c.eq("пустой пул: запросов не было", provider.calls, 0)
        c.eq("пустой пул: attempts", result.attempts, 0)
        c.eq("пустой пул: health keys_total", client.health()["keys_total"], 0)
        c.eq("пустой пул: last_failure в health",
             client.health()["last_failure"]["code"], "no_keys")
    finally:
        os.environ["GOOGLE_API_KEYS"], os.environ["GROQ_API_KEYS"] = saved

    reset_state()
    for provider_name, pool in (("gemini", GOOGLE_KEYS), ("groq", GROQ_KEYS)):
        for api_key in pool:
            state.set_key_cooldown(keys.fingerprint(provider_name, api_key))
    fake = Provider(always=(200, ok_body()))
    result = call(fake)
    c.eq("все в кулдауне: failure", result.failure, "all_keys_on_cooldown")
    c.eq("все в кулдауне: text is None", result.text, None)
    c.eq("все в кулдауне: запросов не было", fake.calls, 0)
    c.eq("все в кулдауне: health считает всех",
         client.health()["keys_on_cooldown"], len(ALL_KEYS))

    # Один ключ отпущен — вызов обязан пройти именно им.
    state.clear_key_cooldown(keys.fingerprint("groq", GROQ_KEYS[1]))
    fake = Provider(always=(200, ok_body("ответ единственным живым ключом")))
    result = call(fake)
    c.eq("один живой ключ: текст есть", result.text, "ответ единственным живым ключом")
    c.eq("один живой ключ: он и использован", fake.used_keys, [GROQ_KEYS[1]])
    c.eq("один живой ключ: модель — groq из каскада", result.provider, "groq")


# --- 6. пустой ответ и неразбираемое тело ------------------------------------

def check_empty_and_broken(c: Checks) -> None:
    print("\n--- 200 без текста и 200 не-JSON ---")
    reset_state()
    provider = Provider(script=[(200, EMPTY_BODY), (200, ok_body("нормальный ответ"))])
    result = call(provider)
    c.eq("пустой content: переход к следующей модели", result.text, "нормальный ответ")
    c.eq("пустой content: модель другая", result.model, cascade.CHAT[1].id)
    c.ok("пустой content: бана нет (молчание — не перегрузка)",
         state.model_bans() == {})

    reset_state()
    provider = Provider(always=(200, EMPTY_BODY))
    result = call(provider)
    c.eq("везде пусто: failure", result.failure, "empty_response")
    c.eq("везде пусто: text is None", result.text, None)
    c.eq("везде пусто: по одной попытке на модель", provider.calls, len(cascade.CHAT))

    reset_state()
    provider = Provider(always=(200, "<html>502 Bad Gateway</html>"))
    result = call(provider)
    c.eq("200 не-JSON: failure", result.failure, "request_failed")
    c.eq("200 не-JSON: text is None", result.text, None)


# --- 7. max_completion_tokens ------------------------------------------------

def check_token_field(c: Checks) -> None:
    print("\n--- 400 про max_tokens: повтор тем же ключом в другой форме ---")
    reset_state()
    bad = json.dumps({"error": {"message": "unsupported parameter max_tokens, "
                                          "use max_completion_tokens"}})
    provider = Provider(script=[(400, bad), (200, ok_body("ответ во второй форме"))])
    result = call(provider)

    c.eq("текст получен", result.text, "ответ во второй форме")
    c.eq("ключ тот же (дело не в ключе)", len(set(provider.used_keys)), 1)
    c.eq("attempts не потрачен на форму поля", result.attempts, 1)
    c.ok("первый запрос с max_tokens", "max_tokens" in provider.payloads[0])
    c.ok("второй запрос с max_completion_tokens",
         "max_completion_tokens" in provider.payloads[1]
         and "max_tokens" not in provider.payloads[1])
    c.ok("модель не забанена", state.model_bans() == {})
    c.eq("payload: температура на месте", provider.payloads[0]["temperature"], 0.35)
    c.eq("payload: роли system и user",
         [m["role"] for m in provider.payloads[0]["messages"]], ["system", "user"])


# --- 8. утечка ключей --------------------------------------------------------

def check_no_key_leak(c: Checks) -> None:
    print("\n--- ключ не течёт в журнал, health(), результат и файлы состояния ---")
    reset_state()
    sink = LogSink()
    root = logging.getLogger()
    saved_level = root.level
    root.addHandler(sink)
    root.setLevel(logging.DEBUG)
    try:
        # Все виды отказов подряд, и каждое тело содержит настоящий ключ.
        provider = Provider(script=[
            (401, denied_body),
            (429, QUOTA_BODY),
            (503, OVERLOAD_BODY),
            (500, denied_body),
        ], always=(403, denied_body))
        result = call(provider)
        rendered = "\n".join([
            sink.text,
            repr(result),
            json.dumps(client.health(), ensure_ascii=False),
            json.dumps({"outcome": result.failure}),
        ])
        for name in (state.KEY_COOLDOWN_FILE, state.MODEL_BAN_FILE,
                     state.LAST_FAILURE_FILE):
            path = _WORKDIR / name
            if path.exists():
                rendered += "\n" + path.read_text(encoding="utf-8")

        c.ok("журнал не пуст (иначе проверка ничего не значит)",
             len(sink.lines) > 0, f"{len(sink.lines)} строк")
        for api_key in ALL_KEYS:
            c.ok(f"нет ключа {api_key[:10]}… нигде", api_key not in rendered)
        c.ok("нет приставки AIzaSy в выводе", "AIzaSy" not in rendered)
        c.ok("нет приставки gsk_ в выводе", "gsk_" not in rendered)
        c.ok("отпечаток ключа в журнале есть (диагностика не потеряна)",
             any(keys.fingerprint("gemini", k) in sink.text
                 or keys.fingerprint("groq", k) in sink.text for k in ALL_KEYS))
        c.eq("итог провала", result.text, None)

        # Тело провайдера вымарано на границе объекта, а не по дороге в журнал.
        reply = transport.HttpReply(401, denied_body(GOOGLE_KEYS[0]), 5)
        c.ok("HttpReply вымарывает тело в конструкторе",
             GOOGLE_KEYS[0] not in reply.body, reply.body)
        c.ok("keys.redact() узнаёт поддельный ключ",
             GOOGLE_KEYS[0] not in keys.redact(f"401 invalid api key {GOOGLE_KEYS[0]}"))
    finally:
        root.removeHandler(sink)
        root.setLevel(saved_level)


# --- 9. деградация для роутера -----------------------------------------------

def check_router_contract(c: Checks) -> None:
    print("\n--- то, на что смотрит brain/router.py ---")
    reset_state()
    provider = Provider(always=(503, OVERLOAD_BODY))
    result = call(provider)
    # router.py: if result.text is None -> деградированный режим, в аудит
    # уходит result.failure. Оба поля обязаны быть пригодны без проверок типа.
    c.ok("text is None при полном провале", result.text is None)
    c.ok("failure — код из закрытого списка state.FAILURE_CODES",
         result.failure in state.FAILURE_CODES, f"{result.failure!r}")
    c.ok("failure подставляется в строку без падения",
         isinstance(f"LLM недоступна ({result.failure})", str))

    # Баны снимаем, а историю провала оставляем: сейчас проверяется, что
    # удачный вызов не стирает last_failure — ops должен видеть, что было.
    reset_state(keep_failure=True)
    result = call(Provider(always=(200, ok_body("текст для вето"))))
    c.ok("успех: result.text.strip() работает", result.text.strip() == "текст для вето")
    c.ok("успех: result.model годится в текст причины",
         isinstance(result.model, str) and result.model != "")

    failure = state.last_failure()
    c.ok("last_failure остаётся историей после успеха", failure is not None)
    c.ok("last_failure с таймзоной Europe/Moscow",
         failure is not None and failure.at.tzinfo is not None)


def run() -> int:
    c = Checks()
    try:
        check_contract(c)
        check_quota_does_not_ban(c)
        check_server_error_bans(c)
        check_key_denied(c)
        check_no_keys(c)
        check_empty_and_broken(c)
        check_token_field(c)
        check_no_key_leak(c)
        check_router_contract(c)
    finally:
        # Каталог состояния — наш, и убираем его сразу: боевые кулдауны лежат в
        # другом месте только потому, что переменная окружения подменена.
        shutil.rmtree(_WORKDIR, ignore_errors=True)

    print(f"\nИТОГ: {c.total - len(c.failures)}/{c.total}")
    for failure in c.failures:
        print(f"  {failure}")
    return 1 if c.failures else 0


if __name__ == "__main__":
    raise SystemExit(run())
