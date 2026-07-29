/**
 * Разовый вход в Авито руками.
 *
 * Автоматического логина здесь нет и не будет. Авито просит SMS-код, иногда
 * капчу, и любая попытка это обойти — прямой путь к блокировке рабочего
 * аккаунта клиники. Поэтому скрипт делает ровно три вещи: открывает ВИДИМЫЙ
 * Chromium на том самом персистентном профиле, которым потом будет работать
 * бот, ждёт, пока человек войдёт, и честно проверяет результат.
 *
 * Почему проверка идёт вторым запуском, уже headless. Бот в рантайме работает
 * headless, и профиль дописывается на диск при закрытии контекста. Проверять
 * сессию в том же видимом окне — значит проверять не то, чем будешь жить:
 * куки ещё в памяти браузера, а не в профиле. Здесь окно закрывается, профиль
 * сбрасывается на диск, и только потом поднимается headless-контекст — ровно
 * так, как это сделает `npm run health` и поллер. Если после этого мессенджер
 * не отрендерился, значит вход не сохранился, и знать это надо сейчас, а не в
 * момент первого обращения пациента.
 *
 * Отсутствие каталога профиля — нормальный первый запуск, а не ошибка.
 * openContext() из session.mjs на несуществующем профиле намеренно бросает
 * исключение (для поллера это верно: молча создать пустой профиль и годами
 * читать ноль чатов — худший из исходов). Здесь же создание профиля — цель
 * запуска, поэтому каталог создаётся явно и об этом печатается строка.
 *
 * Запуск:  npm run login        (из каталога capture)
 * Таймаут: AVITO_LOGIN_WAIT_MIN, по умолчанию 15 минут.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { openContext, checkSession, loadSelectors, MESSENGER_URL } from './session.mjs';

/**
 * Корень проекта. fileURLToPath, а не ручная правка URL.pathname: на Windows
 * pathname выглядит как «/C:/…», и path.resolve() поверх такой строки даёт
 * «c:\C:\…», плюс пробелы в пути остаются как «%20». Это проверено на этой
 * машине — вариант из session.mjs.main() возвращает битый путь.
 */
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');

const WAIT_MINUTES = Number(process.env.AVITO_LOGIN_WAIT_MIN || 15);
const POLL_INTERVAL_MS = 3_000;

/** Признаки стены логина по URL — не зависят от вёрстки, потому проверяются первыми. */
const LOGIN_URL_MARKERS = [/\/login/i, /\/auth/i, /authorize/i];

/**
 * Запасные селекторы на случай, когда capture/selectors.json ещё не заполнен —
 * при первом входе его не существует. Намеренно широкие: задача не «снять
 * структуру» (это делает discover.mjs), а отличить отрендеренный мессенджер от
 * формы входа. Число совпадений по такому OR-списку ЗАВЫШЕНО и количеством
 * чатов не является; в отчёте это подписано.
 */
const FALLBACK_CHAT_LIST = [
  '[data-marker*="channel"]',
  '[data-marker*="messenger"]',
  'a[href*="/messenger/channel"]',
  '[class*="channels-list"]',
  '[class*="channel-preview"]',
].join(', ');

const FALLBACK_CHAT_ITEM = [
  '[data-marker*="channel-preview"]',
  '[data-marker*="channel_preview"]',
  'a[href*="/messenger/channel"]',
  '[class*="channel-preview"]',
].join(', ');

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Снять со страницы набор независимых признаков вместо одного «да/нет».
 * Печатаются все: человек должен видеть доказательства, а не вердикт скрипта.
 */
async function probe(page, chatListSelector) {
  return page.evaluate((given) => {
    const count = (css) => {
      try {
        return document.querySelectorAll(css).length;
      } catch {
        return 0; // невалидный селектор из selectors.json не должен ронять проверку
      }
    };
    return {
      given: given ? count(given) : null,
      channelMarkers: count('[data-marker*="channel"]'),
      channelLinks: count('a[href*="/messenger/channel"]'),
      channelClasses: count('[class*="channel"]'),
      messengerShell: count('[data-marker*="messenger"], [class*="messenger"]'),
      // Признаки того, что мы всё ещё гость: форма входа и поле пароля.
      passwordFields: count('input[type="password"]'),
      loginForm: count('form[action*="login"], [data-marker*="login/"], [data-marker*="login-form"]'),
      // Признак авторизации, не зависящий от наличия чатов: выход из аккаунта.
      logoutLinks: count('a[href*="logout"], [data-marker*="logout"]'),
      title: document.title,
    };
  }, chatListSelector);
}

/** Мессенджер отрендерился? Считаем по совокупности, а не по одному селектору. */
function looksLikeMessenger(url, signals) {
  if (LOGIN_URL_MARKERS.some((re) => re.test(url))) return false;
  if (signals.passwordFields > 0) return false;
  if (signals.given !== null && signals.given > 0) return true;
  return (signals.channelMarkers + signals.channelLinks + signals.messengerShell) > 0;
}

function printInstructions(profile, selectorsKnown) {
  console.log('');
  console.log('=== Разовый вход в Авито руками ===');
  console.log(`Профиль Chromium: ${profile}`);
  console.log(`Ожидание: до ${WAIT_MINUTES} мин (переопределяется AVITO_LOGIN_WAIT_MIN).`);
  console.log('');
  console.log('Что сделать в открывшемся окне:');
  console.log('  1. Войти в РАБОЧИЙ аккаунт клиники: телефон, пароль, код из SMS.');
  console.log('  2. Капчу, если попросит, решить руками. Скрипт в это не вмешивается.');
  console.log('  3. Дойти до списка сообщений. Дальше ничего не делать —');
  console.log('     скрипт сам заметит мессенджер и продолжит.');
  console.log('');
  console.log('Окно можно закрыть самому, когда вход выполнен: проверка всё равно');
  console.log('пройдёт — она поднимает профиль заново, уже headless.');
  console.log('Ничего в чатах писать НЕ надо: это живая переписка с пациентами.');
  if (!selectorsKnown) {
    console.log('');
    console.log('capture/selectors.json пока нет — это ожидаемо до разведки.');
    console.log('Вход проверяется по запасным признакам; структуру страницы потом');
    console.log('снимет `node src/discover.mjs`.');
  }
  console.log('');
}

/** Фаза 1: видимое окно, ждём человека. */
async function waitForHumanLogin(selectors) {
  const ctx = await openContext({ headless: false });
  let windowClosed = false;
  ctx.on('close', () => { windowClosed = true; });

  const chatList = selectors ? selectors.chatList : FALLBACK_CHAT_LIST;
  const deadline = Date.now() + WAIT_MINUTES * 60_000;
  let lastLine = '';
  let rendered = false;

  try {
    const page = ctx.pages()[0] || (await ctx.newPage());
    await page.goto(MESSENGER_URL, { waitUntil: 'domcontentloaded', timeout: 60_000 })
      .catch((err) => console.log(`  (не удалось открыть мессенджер сразу: ${err.message})`));

    while (Date.now() < deadline && !windowClosed) {
      let url = '';
      let signals = null;
      try {
        const live = ctx.pages().at(-1);
        if (!live) throw new Error('нет открытых страниц');
        url = live.url();
        signals = await probe(live, chatList);
      } catch (err) {
        if (windowClosed) break;
        // Навигация в момент опроса — обычное дело, просто ждём следующий такт.
        await sleep(POLL_INTERVAL_MS);
        continue;
      }

      const stage = looksLikeMessenger(url, signals)
        ? 'мессенджер виден'
        : LOGIN_URL_MARKERS.some((re) => re.test(url))
          ? 'страница входа'
          : 'ждём';
      const line = `  [${stage}] ${url}`;
      if (line !== lastLine) {
        console.log(line);
        lastLine = line;
      }

      if (stage === 'мессенджер виден') {
        rendered = true;
        console.log('');
        console.log('Мессенджер отрендерился. Закрываю окно, чтобы профиль лёг на диск.');
        break;
      }
      await sleep(POLL_INTERVAL_MS);
    }
  } finally {
    // Закрытие контекста — это и есть сохранение профиля. Без него куки
    // останутся в памяти процесса, и headless-проверка увидит гостя.
    await ctx.close().catch(() => {});
  }

  return { rendered, windowClosed };
}

/**
 * Фаза 2: тот же профиль, но headless — ровно та конфигурация, в которой
 * работает бот. Проверка идёт через checkSession() из session.mjs, чтобы не
 * заводить второй, «свой» критерий живой сессии.
 */
async function verifyHeadless(selectors) {
  const ctx = await openContext({ headless: true });
  try {
    return await checkSession(ctx, selectors ?? {
      chatList: FALLBACK_CHAT_LIST,
      chatItem: FALLBACK_CHAT_ITEM,
    });
  } finally {
    await ctx.close().catch(() => {});
  }
}

export async function main() {
  // selectors.json при первом входе не существует — это не ошибка.
  let selectors = null;
  try {
    selectors = loadSelectors(ROOT);
  } catch {
    selectors = null;
  }

  const profile = process.env.AVITO_PROFILE;
  if (profile && !fs.existsSync(profile)) {
    fs.mkdirSync(profile, { recursive: true });
    console.log(`Каталог профиля не существовал, создан: ${profile}`);
    console.log('Для первого входа это нормальный путь, а не сбой.');
  }

  printInstructions(profile ?? '(AVITO_PROFILE не задан)', Boolean(selectors));

  const phase1 = await waitForHumanLogin(selectors);
  if (!phase1.rendered && !phase1.windowClosed) {
    console.log('');
    console.log(`ТАЙМАУТ: за ${WAIT_MINUTES} мин мессенджер так и не отрендерился.`);
    console.log('Вход не подтверждён. Запустите `npm run login` ещё раз.');
    return 1;
  }
  if (!phase1.rendered && phase1.windowClosed) {
    console.log('');
    console.log('Окно закрыто руками до того, как мессенджер был замечен.');
    console.log('Проверяю профиль как есть — вход мог успеть сохраниться.');
  }

  console.log('');
  console.log('=== Проверка сессии на том же профиле, headless ===');
  const result = await verifyHeadless(selectors);
  console.log(result.status);
  console.log(`  url: ${result.url}`);
  console.log(`  ${result.detail}`);

  if (result.status !== 'MESSENGER_RENDERED') {
    console.log('');
    console.log('Вход НЕ подтверждён. Что бывает в этом порядке частоты:');
    console.log('  - окно закрыли до фактического входа;');
    console.log('  - Авито не принял сессию с этого IP (нужен обычный IP клиники);');
    console.log('  - AVITO_USER_AGENT не совпадает с браузером, откуда переехали куки;');
    console.log('  - вёрстка сменилась, и selectors.json устарел (тогда — discover.mjs).');
    return 1;
  }

  if (!selectors) {
    console.log('');
    console.log('ВАЖНО: проверка шла по ЗАПАСНЫМ признакам, selectors.json ещё нет.');
    console.log(`Число «элементов» ${result.chats} — это совпадения широкого OR-списка,`);
    console.log('а не количество чатов. Следующий шаг: node src/discover.mjs');
  } else if (result.chats === 0) {
    console.log('');
    console.log('Сессия жива, но чатов ноль. Либо переписок действительно нет,');
    console.log('либо chatItem в selectors.json больше не совпадает с вёрсткой.');
  }

  console.log('');
  console.log('Вход подтверждён. Профиль сохранён и годен для headless-рантайма.');
  return 0;
}

if (import.meta.url === `file://${process.argv[1]}` ||
    process.argv[1]?.endsWith('login.mjs')) {
  main().then((code) => process.exit(code)).catch((err) => {
    console.error(`ОШИБКА: ${err.message}`);
    process.exit(2);
  });
}
