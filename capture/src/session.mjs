/**
 * Сессия Авито: запуск персистентного профиля и честная проверка авторизации.
 *
 * Почему это отдельный модуль и почему проверка именно такая.
 *
 * Официального API у Авито нет — ключи выдаются только платным кабинетам по
 * заявке, а вебхуки требуют публичного HTTPS, которого у клиники за домашним
 * NAT нет. Поэтому единственный транспорт — браузер с живой сессией, и всё
 * висит на одном хрупком месте: сессия может умереть молча. Авито просто
 * отдаст форму входа вместо списка чатов, поллер увидит ноль непрочитанных и
 * будет годами считать, что писем нет. Снаружи это выглядит как «бот сломался»,
 * причём без единой ошибки в логе.
 *
 * Поэтому проверка НЕ сводится к «куки на месте». Куки могут лежать, а Авито
 * их не принимать. Проверяется рендер: пришёл список чатов или стена логина.
 *
 * Профиль — персистентный, а не storageState.json. Снимок не сохраняет
 * обновлённые куки, и через неделю сессию выкинет.
 *
 * Браузер — Chromium с подменённым userAgent, а НЕ настоящий Яндекс.Браузер.
 * Куки переехали из Яндекс.Браузера, и по отпечатку правильнее было бы поднять
 * его же, но Playwright не запустит профиль, который держит живой браузер, а
 * администратор этим браузером пользуется. Настоящий Яндекс.Браузер остаётся
 * для разового логина руками.
 */
import { chromium } from 'playwright';
import fs from 'node:fs';
import path from 'node:path';

export const MESSENGER_URL = 'https://www.avito.ru/profile/messenger';

/** Признаки того, что нас выкинуло. Проверяются до селекторов страницы. */
const LOGIN_URL_MARKERS = [/\/login/i, /\/auth/i, /authorize/i];

export class SessionDead extends Error {
  constructor(detail) {
    super(`Сессия Авито мертва: ${detail}`);
    this.name = 'SessionDead';
    this.detail = detail;
  }
}

function requiredEnv(name, fallback = undefined) {
  const value = process.env[name] ?? fallback;
  if (!value) {
    throw new Error(
      `Переменная окружения ${name} не задана. Заполните .env по образцу .env.example — ` +
      `падать в момент первого лида хуже, чем не стартовать.`
    );
  }
  return value;
}

/**
 * Селекторы страницы живут в данных, а не в коде: Авито меняет вёрстку без
 * предупреждения, и правка одного JSON силами администратора дешевле, чем
 * правка модуля. Отсутствие файла — внятная ошибка на старте.
 */
export function loadSelectors(root) {
  const file = path.join(root, 'capture', 'selectors.json');
  if (!fs.existsSync(file)) {
    throw new Error(
      `Нет файла селекторов ${file}. Он заполняется по разведке живой страницы ` +
      `сообщений (см. capture/selectors.schema.json). Без него транспорт не знает, ` +
      `куда смотреть, и угадывать он не будет.`
    );
  }
  const parsed = JSON.parse(fs.readFileSync(file, 'utf8'));
  const missing = ['chatList', 'chatItem', 'unreadMark', 'messageBubble', 'input', 'sendButton']
    .filter((k) => !parsed[k]);
  if (missing.length) {
    throw new Error(`В selectors.json не заполнены обязательные ключи: ${missing.join(', ')}`);
  }
  return parsed;
}

export async function openContext({ headless = true } = {}) {
  const profile = requiredEnv('AVITO_PROFILE');
  const userAgent = requiredEnv('AVITO_USER_AGENT');

  if (!fs.existsSync(profile)) {
    throw new Error(
      `Профиль ${profile} не существует. Сначала нужен разовый вход в Авито руками ` +
      `на этой машине — автоматически залогиниться мы не можем и не пытаемся.`
    );
  }

  return chromium.launchPersistentContext(profile, {
    headless,
    viewport: headless ? { width: 1440, height: 900 } : null,
    locale: 'ru-RU',
    timezoneId: 'Europe/Moscow',
    userAgent,
    // Профиль общий с ручным входом; заглушки автоматизации снижают шанс,
    // что Авито опознает нас как бота на первом же запросе.
    args: ['--disable-blink-features=AutomationControlled', '--no-first-run',
           '--no-default-browser-check'],
  });
}

/**
 * @returns {Promise<{status: 'MESSENGER_RENDERED'|'LOGIN_WALL'|'UNKNOWN', url: string, chats: number|null, detail: string}>}
 */
export async function checkSession(ctx, selectors) {
  const page = ctx.pages()[0] || (await ctx.newPage());
  await page.goto(MESSENGER_URL, { waitUntil: 'domcontentloaded', timeout: 40_000 });

  // Редирект на логин — самый однозначный признак, и он не зависит от вёрстки.
  const url = page.url();
  if (LOGIN_URL_MARKERS.some((re) => re.test(url))) {
    return { status: 'LOGIN_WALL', url, chats: null, detail: 'редирект на страницу входа' };
  }

  try {
    await page.waitForSelector(selectors.chatList, { timeout: 15_000 });
  } catch {
    return {
      status: 'UNKNOWN',
      url,
      chats: null,
      detail: `контейнер списка чатов (${selectors.chatList}) не отрендерился за 15 с — ` +
              `либо сессия мертва, либо Авито сменил вёрстку и selectors.json устарел`,
    };
  }

  const chats = await page.locator(selectors.chatItem).count();
  return {
    status: 'MESSENGER_RENDERED',
    url,
    chats,
    detail: `список чатов отрендерился, элементов ${chats}`,
  };
}

/** Точка входа для health-check: печатает одно слово статуса и код возврата. */
export async function main() {
  const root = path.resolve(path.dirname(new URL(import.meta.url).pathname), '..', '..')
    .replace(/^[/\\]([A-Za-z]:)/, '$1');
  const selectors = loadSelectors(root);
  const ctx = await openContext({ headless: true });
  try {
    const result = await checkSession(ctx, selectors);
    console.log(result.status);
    console.log(`  url: ${result.url}`);
    console.log(`  ${result.detail}`);
    return result.status === 'MESSENGER_RENDERED' ? 0 : 1;
  } finally {
    await ctx.close();
  }
}

if (import.meta.url === `file://${process.argv[1]}` ||
    process.argv[1]?.endsWith('session.mjs')) {
  main().then((code) => process.exit(code)).catch((err) => {
    console.error(`ОШИБКА: ${err.message}`);
    process.exit(2);
  });
}
