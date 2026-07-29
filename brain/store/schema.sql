-- Схема состояния бота. Применяется целиком при каждом открытии базы
-- (CREATE ... IF NOT EXISTS), поэтому файл обязан быть идемпотентным: ни одного
-- DROP, ни одного INSERT с данными, ни одной операции, которая при повторном
-- прогоне что-то теряет.
--
-- PRAGMA journal_mode и PRAGMA foreign_keys здесь НЕТ намеренно. foreign_keys —
-- настройка соединения, а не файла: применённая один раз при создании схемы, она
-- молча выключится у каждого следующего соединения, и внешние ключи перестанут
-- проверяться, о чём никто не узнает. Обе PRAGMA выставляет db.py на каждом
-- открытии.

-- --------------------------------------------------------------------------
-- Диалоги. Одна строка на чат Авито.
-- --------------------------------------------------------------------------
-- Номера телефонов пациентов в этой таблице нет и не будет — только phone_hash.
-- Номер остаётся в переписке Авито, второй копии он не требует, а база без
-- персональных данных радикально проще по 152-ФЗ: нет ПД — нет обязанностей по
-- их защите, уведомлений в РКН и рисков при утечке файла базы.
-- CHECK ниже — не украшение: он физически не даст записать в эту колонку
-- «+79271234567» или «8 927 712 99 26» (плюс, пробелы и цифры 8/9 вне 0-9a-f
-- отсекаются GLOB, а 10-11 цифр не проходят по длине). Требование выражено
-- схемой, а не дисциплиной вызывающего кода.
CREATE TABLE IF NOT EXISTS dialogs (
    chat_id                  TEXT    PRIMARY KEY NOT NULL,
    first_seen_at            TEXT    NOT NULL,
    patient_last_message_at  TEXT,
    our_last_message_at      TEXT,
    patient_messages         INTEGER NOT NULL DEFAULT 0,
    our_messages             INTEGER NOT NULL DEFAULT 0,
    followups_sent           INTEGER NOT NULL DEFAULT 0,
    last_followup_at         TEXT,
    -- NULL = не перехвачен / не на паузе. Дата в будущем = до этого момента.
    -- Бессрочно = дата-страж 9999-12-31 (db.py: FOREVER), чтобы «совсем» и
    -- «не выставлено» были разными состояниями, а не одним NULL.
    takeover_until           TEXT,
    ai_paused_until          TEXT,
    phone_hash               TEXT,
    CHECK (phone_hash IS NULL
           OR (length(phone_hash) BETWEEN 16 AND 64
               AND phone_hash NOT GLOB '*[^0-9a-f]*')),
    CHECK (patient_messages >= 0 AND our_messages >= 0 AND followups_sent >= 0)
);
-- Индекс по chat_id не создаётся отдельно: PRIMARY KEY на TEXT — это готовый
-- sqlite_autoindex, и все обращения к диалогу идут ровно по нему.

-- --------------------------------------------------------------------------
-- Дедупликация входящих. Единственное, что стоит между пациентом и повторной
-- отправкой того же ответа.
-- --------------------------------------------------------------------------
-- external_id — id сообщения на стороне Авито. PRIMARY KEY здесь несёт всю
-- нагрузку идемпотентности: проверка «SELECT, потом INSERT» на уровне кода
-- ломается, когда поллер перезапустился и на секунду работает в двух копиях —
-- между SELECT и INSERT успевает вклиниться второй процесс, и оба решают, что
-- сообщение новое. UNIQUE в схеме не ломается никогда: второй INSERT проиграет
-- внутри одной атомарной операции SQLite.
CREATE TABLE IF NOT EXISTS seen (
    external_id TEXT NOT NULL PRIMARY KEY,
    chat_id     TEXT NOT NULL REFERENCES dialogs(chat_id) ON DELETE CASCADE,
    at          TEXT NOT NULL,   -- время сообщения по данным Авито
    recorded_at TEXT NOT NULL    -- когда его увидели мы; расхождение = лаг поллера
);
-- Индекса по seen.chat_id нет намеренно. Это самая горячая на запись таблица
-- (строка на каждое опрошенное сообщение), а запросов «все сообщения чата» в
-- API нет: история диалога живёт в Авито. ON DELETE CASCADE отработает сканом,
-- но диалоги не удаляются.

-- --------------------------------------------------------------------------
-- Черновики администратору.
-- --------------------------------------------------------------------------
-- status пуст только в одном значении — 'pending'; остальные четыре совпадают с
-- action в resolve_draft. Пара CHECK ниже держит инвариант «решённый черновик
-- имеет время решения» и «правка не может быть пустой»: если правка потеряет
-- текст, отправлять будет нечего, и это должно упасть здесь, а не в send.mjs.
CREATE TABLE IF NOT EXISTS drafts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id       TEXT    NOT NULL REFERENCES dialogs(chat_id) ON DELETE CASCADE,
    text          TEXT    NOT NULL,
    kind          TEXT    NOT NULL,
    reason        TEXT    NOT NULL,
    status        TEXT    NOT NULL DEFAULT 'pending',
    created_at    TEXT    NOT NULL,
    resolved_at   TEXT,
    resolved_by   TEXT,
    final_text    TEXT,
    tg_message_id INTEGER,
    CHECK (status IN ('pending', 'sent', 'edited', 'ignored', 'expired')),
    CHECK (length(text) > 0),
    CHECK ((status = 'pending') = (resolved_at IS NULL)),
    CHECK (status <> 'edited' OR (final_text IS NOT NULL AND length(final_text) > 0))
);
-- Частичный индекс ровно под pending_drafts(): в нём лежат только неразобранные
-- черновики, а их единицы, тогда как разобранных со временем будут тысячи.
CREATE INDEX IF NOT EXISTS drafts_pending
    ON drafts(created_at) WHERE status = 'pending';
-- UNIQUE, а не просто индекс: одно сообщение в Telegram соответствует одному
-- черновику. Иначе «Правка» ответом на сообщение бота могла бы отредактировать
-- чужой ответ пациенту. NULL в SQLite не конфликтуют между собой, поэтому
-- непривязанных черновиков может быть сколько угодно.
CREATE UNIQUE INDEX IF NOT EXISTS drafts_by_tg_message
    ON drafts(tg_message_id);
-- Под «есть ли у этого чата неотвеченный черновик» и под каскад по внешнему ключу.
CREATE INDEX IF NOT EXISTS drafts_by_chat
    ON drafts(chat_id, created_at);

-- --------------------------------------------------------------------------
-- Аудит решений.
-- --------------------------------------------------------------------------
-- Внешнего ключа на dialogs здесь нет сознательно: аудит обязан принимать
-- событие раньше, чем появился диалог (отброшенный спам, ошибка ключа LLM,
-- запуск процесса), и вообще без chat_id. Аудит, который отказался записать
-- событие из-за ссылочной целостности, бесполезен именно в тот момент, когда
-- нужен больше всего.
-- payload — JSON. Вызывающий обязан прогнать текст пациента через pii.scrub()
-- ДО передачи: store не является фильтром ПД и не пытается им быть.
CREATE TABLE IF NOT EXISTS audit (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      TEXT    NOT NULL,
    event   TEXT    NOT NULL,
    chat_id TEXT,
    payload TEXT,
    CHECK (length(event) > 0),
    CHECK (payload IS NULL OR json_valid(payload))
);
-- Индекса по audit нет: единственное чтение — recent_audit(), это ORDER BY id
-- DESC по самому rowid. Появится экран «аудит одного чата» — тогда и добавить
-- audit(chat_id, id), не раньше.

-- --------------------------------------------------------------------------
-- Шина между Node и Python.
-- --------------------------------------------------------------------------
-- Транспорт (capture/, Node + Playwright) и решения (brain/, Python) — это два
-- отдельных процесса, и общаются они через этот файл, а не через сокет, порт или
-- очередь. Причины ровно три, и все три практические.
--
-- Первая: перезапуск. Скрапер ломается от смены вёрстки Авито чаще всего в
-- проекте, и он обязан падать и подниматься, не унося с собой ни одного
-- решённого черновика. Состояние в общем файле переживает перезапуск любой из
-- двух половин.
--
-- Вторая: у обеих сторон уже есть надёжный SQLite-клиент (sqlite3 в Python,
-- better-sqlite3 в Node), а WAL честно разрешает писателей из разных процессов.
-- Поднимать HTTP между двумя процессами на одном ноутбуке — это лишний порт,
-- лишний failure mode и лишняя вещь, которую надо не забыть закрыть от сети.
--
-- Третья: очередь на диске видна глазами. Когда клиника скажет «бот не ответил»,
-- ответ на «почему» лежит в двух SELECT-ах, а не в логах двух процессов.

-- Входящие, как их прочитал DOM-поллер. ПИШЕТ ТОЛЬКО NODE, ЧИТАЕТ ТОЛЬКО PYTHON.
--
-- Внешнего ключа на dialogs здесь нет намеренно: Node не знает и не должен знать
-- про соглашения Python по строке диалога, а inbox обязан принять сообщение
-- раньше, чем роутер решит его судьбу. Строку диалога создаёт mark_seen.
--
-- external_id PRIMARY KEY — это дедуп НА УРОВНЕ ЧТЕНИЯ, отдельный от `seen`
-- (дедуп на уровне обработки). Каждый проход поллера перечитывает всю переписку
-- открытого чата целиком, то есть предъявляет одни и те же пузыри снова и снова;
-- без этого ключа таблица росла бы копиями. Не путать с `seen`: там ответ на
-- «мы это уже обрабатывали», здесь — на «мы это уже вычитали из DOM».
--
-- Исходящие пузыри (outgoing = 1) тоже пишутся, хотя отвечать на них не надо.
-- Из них Python собирает историю диалога для промпта: своих сообщений в базе
-- иначе нет, а модель без истории отвечает как на первое сообщение.
--
-- at_raw — то, что Авито показал человеку («14:32», «вчера»). Парсить это в
-- datetime нельзя: формат зависит от давности сообщения и локали, и ошибка
-- парсинга сдвинула бы время диалога, на котором стоят дожимы. Единственное
-- время, которому можно верить, — harvested_at, поставленное нами при чтении.
CREATE TABLE IF NOT EXISTS inbox (
    external_id  TEXT    NOT NULL PRIMARY KEY,
    chat_id      TEXT    NOT NULL,
    -- URL чата, если Авито его отдаёт. Отправщик открывает чат по нему, а не по
    -- позиции в списке: список отсортирован по времени и переставляется между
    -- чтением и отправкой, а отправка ответа не в тот чат — это худший баг,
    -- который этот проект может допустить.
    chat_url     TEXT,
    counterparty TEXT,
    outgoing     INTEGER NOT NULL,
    text         TEXT    NOT NULL,
    at_raw       TEXT,
    position     INTEGER NOT NULL,
    harvested_at TEXT    NOT NULL,
    processed_at TEXT,             -- NULL = роутер ещё не разбирал
    CHECK (outgoing IN (0, 1)),
    CHECK (length(text) > 0),
    CHECK (position >= 0)
);
-- Частичный индекс ровно под очередь на разбор: только чужие и только
-- неразобранные. Исходящие в него не попадают вообще.
CREATE INDEX IF NOT EXISTS inbox_pending
    ON inbox(harvested_at, position) WHERE processed_at IS NULL AND outgoing = 0;
-- Под сборку истории диалога для промпта.
CREATE INDEX IF NOT EXISTS inbox_by_chat
    ON inbox(chat_id, position);

-- Одобренные ответы. ПИШЕТ PYTHON, ОТПРАВЛЯЕТ И ЗАКРЫВАЕТ NODE.
--
-- Главное решение этой таблицы: строка в состоянии 'sending' НЕ ВОЗВРАЩАЕТСЯ в
-- очередь сама. Если процесс отправщика умер между кликом и подтверждением, мы
-- не знаем, ушло сообщение пациенту или нет. Автоматический повтор в этом
-- состоянии — это шанс отправить пациенту один и тот же текст дважды, а
-- дубликат в переписке выдаёт бота вернее любой формулировки и стоит лида.
-- Поэтому 'sending' — тупик, из которого выводит человек, а демон показывает
-- такие строки в Telegram. Потерянный ответ стоит минуты администратора,
-- дубликат стоит пациента.
--
-- send_after — момент из delay.plan_reply(). Задержка держится здесь, а не
-- sleep-ом в Python: процесс решений можно перезапустить, не потеряв «этот
-- ответ уйдёт в 14:32», и мгновенного ответа не случится даже после рестарта.
CREATE TABLE IF NOT EXISTS outbox (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id       TEXT    NOT NULL,
    chat_url      TEXT,
    text          TEXT    NOT NULL,
    kind          TEXT    NOT NULL,   -- reply | followup | manual
    draft_id      INTEGER REFERENCES drafts(id) ON DELETE SET NULL,
    send_after    TEXT    NOT NULL,
    status        TEXT    NOT NULL DEFAULT 'queued',
    attempts      INTEGER NOT NULL DEFAULT 0,
    queued_at     TEXT    NOT NULL,
    claimed_at    TEXT,
    sent_at       TEXT,
    -- Текст последнего исходящего пузыря после отправки. Не украшение: send.mjs
    -- подтверждает отправку появлением сообщения в переписке, и это
    -- подтверждение обязано остаться на диске. «Кнопка нажата» — не отправка.
    confirmation  TEXT,
    last_error    TEXT,
    -- Когда Python учёл факт отправки: touch_dialog, счётчик дожимов, аудит.
    -- Node этого сделать не может — это логика диалога, а не транспорт.
    accounted_at  TEXT,
    CHECK (status IN ('queued', 'sending', 'sent', 'failed')),
    CHECK (kind IN ('reply', 'followup', 'manual')),
    CHECK (length(text) > 0),
    CHECK (attempts >= 0),
    CHECK ((status = 'sent') = (sent_at IS NOT NULL)),
    CHECK (accounted_at IS NULL OR sent_at IS NOT NULL)
);
-- Очередь отправщика: только то, что готово уйти, в порядке срока.
CREATE INDEX IF NOT EXISTS outbox_queued
    ON outbox(send_after) WHERE status = 'queued';
-- UNIQUE, а не индекс: один черновик = максимум одна отправка. Telegram
-- доставляет один и тот же callback повторно, и без этого констрейнта второе
-- «Отправить» поставило бы пациенту второй такой же ответ. NULL в SQLite между
-- собой не конфликтуют, поэтому дожимов и автоответов без черновика может быть
-- сколько угодно.
CREATE UNIQUE INDEX IF NOT EXISTS outbox_by_draft
    ON outbox(draft_id);
-- Под «что уже ушло, но ещё не учтено в диалоге».
CREATE INDEX IF NOT EXISTS outbox_unaccounted
    ON outbox(sent_at) WHERE status = 'sent' AND accounted_at IS NULL;

-- Курсоры процессов: offset getUpdates у Telegram, время последнего обхода
-- дожимов. Всё это обязано переживать перезапуск — offset особенно: потерянный
-- offset означает повторную обработку старых нажатий кнопок, то есть повторную
-- отправку пациенту уже отправленного. Отдельные файлы состояния для двух
-- чисел не нужны, а тащить их в audit — значит смешать «что произошло» с «где
-- мы остановились».
CREATE TABLE IF NOT EXISTS cursors (
    name       TEXT NOT NULL PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

PRAGMA user_version = 2;
