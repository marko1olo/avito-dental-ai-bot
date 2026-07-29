/**
 * Транспортный процесс: единственное место, которое разговаривает с Авито.
 *
 * Два цикла в одном процессе, по очереди, с паузой между проходами:
 *   опрос    — прочитать непрочитанные чаты и сложить сообщения в inbox
 *   отправка — забрать из outbox то, чему пришёл срок, напечатать, подтвердить
 *
 * Один процесс, а не два, по одной причине: сессия Авито одна. Персистентный
 * профиль Chromium держит блокировку каталога, и второй процесс его просто не
 * откроет. Значит, читать и писать обязан один владелец браузера.
 *
 * Решений здесь нет ни одного. Что отвечать, кому и когда — решил brain/, и
 * решение уже лежит в базе строкой outbox со сроком send_after. Этот файл
 * умеет только «прочитать DOM» и «напечатать текст». Разделение не косметика:
 * скрапер ломается от смены вёрстки чаще всего в проекте, и он не должен
 * унести с собой логику диалога.
 *
 * Главное правило отправки, из-за которого написан весь протокол статусов:
 * строка, взятая в работу, НЕ возвращается в очередь сама. Если процесс умер
 * между кликом и подтверждением, неизвестно, увидел пациент сообщение или нет.
 * Автоматический повтор в этом состоянии — шанс отправить один и тот же текст
 * дважды, а дубликат в переписке выдаёт бота вернее любой формулировки.
 * Потерянный ответ стоит минуты администратора, дубликат стоит пациента.
 * Поэтому 'sending' — тупик, из которого выводит человек (демон показывает
 * такие строки в Telegram через stuck_sending).
 */
import Database from './db-loader.mjs';
import path from 'node:path';
import process from 'node:process';

import { openContext, checkSession, loadSelectors, MESSENGER_URL } from './session.mjs';
import { pollOnce } from './poll.mjs';
import { sendMessage, SendFailed } from './send.mjs';

// Пауза между проходами. Опрос — это навигация по чужому сайту, и частить им
// нельзя: это и нагрузка на аккаунт с точки зрения антифрода, и лишние
// перерисовки страницы. 20 с при 10-30 обращениях в день с запасом.
const POLL_EVERY_MS = 20_000;

// Сколько чатов открывать за проход. Ограничение против ночной пачки: открыть
// сорок чатов подряд быстрее человека — заметный признак автомата.
const MAX_CHATS = 12;

// Сколько сообщений отправлять за проход. Больше одного за цикл не нужно:
// delay.plan_reply уже расставил сроки так, чтобы ответы не шли пачкой.
const MAX_SENDS = 3;

const TZ = 'Europe/Moscow';

/** ISO-строка того же формата, что пишет Python (db.py: _iso). */
function nowIso() {
  // Формат обязан совпадать с Python-овским: сравнения сроков в SQL
  // лексикографические, и строка другой ширины сломает их молча. Europe/Moscow —
  // постоянный UTC+4 без перехода на летнее время, поэтому смещение зашито.
  const parts = new Intl.DateTimeFormat('sv-SE', {
    timeZone: TZ, year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  }).formatToParts(new Date());
  const get = (type) => parts.find((p) => p.type === type).value;
  const micros = String(new Date().getMilliseconds() * 1000).padStart(6, '0');
  return `${get('year')}-${get('month')}-${get('day')}T` +
         `${get('hour')}:${get('minute')}:${get('second')}.${micros}+04:00`;
}

function log(message) {
  console.log(`${nowIso()} ${message}`);
}

/**
 * База — тот же файл, что у демона решений. Разные пути означают, что поллер
 * пишет в пустоту, а демон вечно ждёт входящих, поэтому переменная одна на два
 * процесса и её отсутствие — отказ в старте.
 */
function openDb() {
  const file = process.env.AVITO_BOT_DB;
  if (!file) {
    throw new Error(
      'AVITO_BOT_DB не задана. Заполните .env по образцу .env.example. Демон ' +
      'решений и поллер обязаны открыть ОДИН файл базы, иначе поллер пишет в пустоту.'
    );
  }
  const db = new Database(file);
  // WAL и busy timeout выставляет и Python, но соединение здесь своё: PRAGMA
  // busy_timeout — настройка соединения, а не файла. Без неё writer-writer
  // конфликт с демоном вернёт SQLITE_BUSY вместо ожидания.
  db.pragma('journal_mode = WAL');
  db.pragma('busy_timeout = 10000');
  db.pragma('foreign_keys = ON');
  // Схему создаёт Python: он владеет schema.sql, и две реализации DDL — это
  // две правды о структуре. Если демон ни разу не запускался, таблиц нет, и
  // это честная ошибка старта, а не повод их выдумать здесь.
  const ready = db.prepare(
    "SELECT count(*) AS n FROM sqlite_master WHERE type='table' " +
    "AND name IN ('inbox','outbox')").get();
  if (ready.n < 2) {
    db.close();
    throw new Error(
      `В базе ${file} нет таблиц inbox/outbox. Схему применяет brain/, ` +
      'запустите сначала `python brain/run.py --once`.'
    );
  }
  return db;
}

// --- опрос ------------------------------------------------------------------

/**
 * Сложить вычитанные сообщения в inbox.
 *
 * INSERT OR IGNORE по external_id — это дедуп на уровне ЧТЕНИЯ, отдельный от
 * `seen` (дедуп на уровне обработки). Каждый проход перечитывает переписку
 * открытого чата целиком, то есть предъявляет одни и те же пузыри снова; без
 * этого таблица росла бы копиями.
 *
 * Свои пузыри (outgoing) тоже пишутся: из них Python собирает историю диалога
 * для промпта, иначе модель отвечает как на первое сообщение.
 */
function storeHarvest(db, harvest, chatUrls) {
  const insert = db.prepare(
    'INSERT OR IGNORE INTO inbox(external_id, chat_id, chat_url, counterparty, ' +
    'outgoing, text, at_raw, position, harvested_at) ' +
    'VALUES(@externalId, @chatId, @chatUrl, @counterparty, @outgoing, @text, ' +
    '@atRaw, @position, @harvestedAt)');

  // Транзакция на весь проход: половина вычитанного чата в базе — это диалог,
  // в котором модель увидит дырку в истории.
  const write = db.transaction((rows) => {
    let added = 0;
    for (const row of rows) added += insert.run(row).changes;
    return added;
  });

  const stamp = nowIso();
  const rows = [];
  for (const chat of harvest) {
    for (const message of chat.all) {
      rows.push({
        externalId: message.externalId,
        chatId: chat.chatId,
        chatUrl: chatUrls.get(chat.chatId) ?? null,
        counterparty: chat.name,
        outgoing: message.outgoing ? 1 : 0,
        text: message.text,
        atRaw: message.at || null,
        position: message.position,
        harvestedAt: stamp,
      });
    }
  }
  return write(rows);
}

async function pollCycle(page, sel, db) {
  const harvest = await pollOnce(page, sel, { maxChats: MAX_CHATS });
  if (!harvest.length) return 0;

  // URL чата запоминается на момент чтения: отправщик открывает чат по нему, а
  // не по позиции в списке. Список отсортирован по времени и переставляется
  // между чтением и отправкой — отправка ответа не в тот чат была бы худшим
  // багом, который этот проект может допустить.
  const chatUrls = new Map();
  for (const chat of harvest) {
    if (!chatUrls.has(chat.chatId)) chatUrls.set(chat.chatId, page.url());
  }

  const added = storeHarvest(db, harvest, chatUrls);
  if (added) log(`опрос: чатов ${harvest.length}, новых сообщений ${added}`);
  return added;
}

// --- отправка ---------------------------------------------------------------

/**
 * Взять строку в работу атомарно.
 *
 * UPDATE ... WHERE status='queued' — это и есть захват: условие в самом UPDATE, а не
 * SELECT с последующей записью. Проверка перед записью проигрывает второму
 * процессу, и оба отправят пациенту один текст. Демон эту строку не захватывает
 * никогда: SQL захвата живёт в одном месте, у владельца браузера.
 */
function claim(db, limit) {
  const pick = db.prepare(
    "SELECT * FROM outbox WHERE status = 'queued' AND send_after <= ? " +
    'ORDER BY send_after, id LIMIT ?');
  const take = db.prepare(
    "UPDATE outbox SET status = 'sending', claimed_at = ?, attempts = attempts + 1 " +
    "WHERE id = ? AND status = 'queued'");

  const claimed = [];
  const stamp = nowIso();
  for (const row of pick.all(stamp, limit)) {
    // Транзакция на одну строку: захват должен быть виден остальным сразу.
    if (take.run(stamp, row.id).changes === 1) claimed.push(row);
  }
  return claimed;
}

/**
 * Найти чат по URL, запомненному при чтении. Позиция в списке для этого не
 * годится: между чтением и отправкой список переставился, и мы напечатали бы
 * ответ пациента другому человеку.
 */
async function openByUrl(page, sel, chatUrl) {
  if (!chatUrl) {
    throw new SendFailed(
      'у строки нет chat_url: чат читался до того, как поллер начал их запоминать, ' +
      'а искать чат по позиции в переставившемся списке нельзя'
    );
  }
  await page.goto(chatUrl, { waitUntil: 'domcontentloaded', timeout: 40_000 });
  await page.waitForSelector(sel.messageBubble, { timeout: 15_000 });
}

async function sendCycle(page, sel, db) {
  const rows = claim(db, MAX_SENDS);
  if (!rows.length) return 0;

  const markSent = db.prepare(
    "UPDATE outbox SET status = 'sent', sent_at = ?, confirmation = ? WHERE id = ?");
  const markFailed = db.prepare(
    "UPDATE outbox SET status = 'failed', last_error = ? WHERE id = ?");

  let sent = 0;
  for (const row of rows) {
    try {
      await openByUrl(page, sel, row.chat_url);
      // chatIndex здесь 0: чат уже открыт по URL, и sendMessage переоткрывать
      // его не должен. Нулевой индекс — это «первый элемент списка», то есть
      // текущий диалог в мессенджере Авито.
      const result = await sendMessage(page, sel, { chatIndex: 0, text: row.text });
      markSent.run(nowIso(), result.lastOutgoing.slice(0, 500), row.id);
      sent += 1;
      log(`отправлено #${row.id} (${row.kind}) в чат ${row.chat_id}`);
    } catch (err) {
      if (err instanceof SendFailed) {
        // SendFailed означает, что отправка НЕ подтверждена, и это единственный
        // случай, когда строку можно закрыть как провал: send.mjs проверяет
        // появление сообщения в переписке, а не факт клика. Демон покажет
        // 'failed' администратору, дубликата не будет.
        markFailed.run(String(err.detail).slice(0, 500), row.id);
        log(`ПРОВАЛ отправки #${row.id}: ${err.detail}`);
      } else {
        // Любая другая ошибка (сессия умерла, навигация, таймаут) оставляет
        // строку в 'sending' НАМЕРЕННО. Неизвестно, ушло сообщение или нет, и
        // решать это должен человек: демон увидит её в stuck_sending и позовёт
        // в Telegram. Дубликат в переписке дороже потерянного ответа.
        log(`ОТПРАВКА #${row.id} ОБОРВАЛАСЬ, строка осталась в sending: ${err.message}`);
        throw err;  // прервать проход: скорее всего сессия мертва
      }
    }
  }
  return sent;
}

// --- цикл -------------------------------------------------------------------

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function projectRoot() {
  // Тот же приём, что в session.mjs: import.meta.url на Windows даёт
  // /C:/... с ведущим слэшем, который path не понимает.
  return path.resolve(path.dirname(new URL(import.meta.url).pathname), '..', '..')
    .replace(/^[/\\]([A-Za-z]:)/, '$1');
}

async function main() {
  const root = projectRoot();
  const sel = loadSelectors(root);
  const db = openDb();
  const ctx = await openContext({ headless: true });

  let stopping = false;
  const stop = (signal) => {
    if (!stopping) log(`получен ${signal}, доводим проход до конца`);
    stopping = true;
  };
  process.on('SIGINT', () => stop('SIGINT'));
  process.on('SIGTERM', () => stop('SIGTERM'));

  try {
    const page = ctx.pages()[0] || (await ctx.newPage());
    const health = await checkSession(ctx, sel);
    if (health.status !== 'MESSENGER_RENDERED') {
      // Мёртвая сессия — это отказ в старте, а не повод крутиться впустую.
      // Поллер, который видит ноль непрочитанных из-за стены логина, годами
      // считает, что писем нет, и снаружи это выглядит как «бот сломался»,
      // причём без единой ошибки в логе.
      log(`СЕССИЯ НЕ ГОДНА: ${health.status} — ${health.detail}`);
      log('Нужен разовый вход руками: npm run login (на ноутбуке, headful).');
      return 1;
    }
    log(`сессия жива, чатов в списке ${health.chats}`);

    while (!stopping) {
      try {
        await pollCycle(page, sel, db);
        const sent = await sendCycle(page, sel, db);
        if (sent) await page.goto(MESSENGER_URL, { waitUntil: 'domcontentloaded' });
      } catch (err) {
        // Проход упал целиком. Проверяем сессию: если её больше нет, крутиться
        // бессмысленно — выходим с ненулевым кодом, чтобы это было видно тому,
        // кто запускал, а не пряталось в логе.
        log(`ПРОХОД УПАЛ: ${err.message}`);
        const again = await checkSession(ctx, sel).catch(() => null);
        if (!again || again.status !== 'MESSENGER_RENDERED') {
          log('сессия Авито мертва, выходим — нужен вход руками (npm run login)');
          return 1;
        }
      }
      if (!stopping) await sleep(POLL_EVERY_MS);
    }
    return 0;
  } finally {
    await ctx.close();
    db.close();
    log('поллер остановлен, браузер закрыт');
  }
}

if (process.argv[1]?.endsWith('run-poll.mjs')) {
  main().then((code) => process.exit(code)).catch((err) => {
    console.error(`ОШИБКА: ${err.message}`);
    process.exit(2);
  });
}
