/**
 * Отправка одобренного ответа в чат Авито через DOM.
 *
 * Единственный модуль во всём проекте, который что-то говорит пациенту.
 * Поэтому он намеренно тупой: получает готовый текст и id чата, печатает,
 * отправляет, подтверждает факт отправки. Никаких решений, никакой генерации,
 * никаких подстановок. Всё, что можно было решить, решено раньше — роутером,
 * вето и администратором.
 *
 * Два неочевидных требования, оба из практики.
 *
 * Первое: текст набирается ПО КЛАВИШАМ с человеческой скоростью, а не
 * подставляется через fill(). Мгновенное появление 200 знаков в поле ввода —
 * такой же признак автомата, как мгновенный ответ, и Авито видит события ввода.
 *
 * Второе: после отправки обязательно подтверждение, что сообщение появилось
 * в переписке. Клик по кнопке, который ничего не отправил (перерисовка, потеря
 * фокуса, сдохшая сессия), молча теряет ответ пациенту — а мы уже пометили
 * черновик отправленным. Поэтому проверяем результат, а не факт клика.
 */

const TYPE_DELAY_MS = 28;          // ~35 знаков/с, темп администратора с клавиатуры
const CONFIRM_TIMEOUT_MS = 12_000;

export class SendFailed extends Error {
  constructor(detail) {
    super(`Отправка не подтверждена: ${detail}`);
    this.name = 'SendFailed';
    this.detail = detail;
  }
}

/** Найти и открыть чат по позиции в списке. */
async function openChat(page, sel, index) {
  await page.locator(sel.chatItem).nth(index).click();
  await page.waitForSelector(sel.messageBubble, { timeout: 15_000 });
}

/**
 * Напечатать текст в поле ввода. textarea и contenteditable ведут себя
 * по-разному: во второй fill() либо не работает, либо ломает разметку, поэтому
 * инструкция берётся из selectors.json, а не угадывается по тегу.
 */
async function typeReply(page, sel, text) {
  const field = page.locator(sel.input).first();
  await field.click();

  if (sel.inputKind === 'contenteditable') {
    await field.press('Control+A').catch(() => {});
    await field.press('Delete').catch(() => {});
  } else {
    await field.fill('');
  }

  await field.type(text, { delay: TYPE_DELAY_MS });

  const typed = sel.inputKind === 'contenteditable'
    ? ((await field.textContent()) || '').trim()
    : await field.inputValue();
  if (typed.replace(/\s+/g, ' ') !== text.replace(/\s+/g, ' ')) {
    throw new SendFailed(
      `поле ввода содержит не то, что набрано (${typed.length} знаков против ${text.length}) — ` +
      `вероятно, сменилась вёрстка или поле перерисовалось во время набора`
    );
  }
}

/**
 * Отправить и ПОДТВЕРДИТЬ. Возвращает текст последнего исходящего пузыря —
 * доказательство, что сообщение действительно в переписке, а не только в поле.
 */
export async function sendMessage(page, sel, { chatIndex, text }) {
  if (!text || !text.trim()) {
    throw new SendFailed('пустой текст — отправлять нечего');
  }

  await openChat(page, sel, chatIndex);

  const outgoingSelector = sel.outgoingMark
    ? `${sel.messageBubble}:has(${sel.outgoingMark})`
    : sel.messageBubble;
  const before = await page.locator(outgoingSelector).count();

  await typeReply(page, sel, text);

  if (sel.sendByEnter) {
    // Enter надёжнее клика: кнопку отправки Авито перерисовывает, и локатор
    // успевает устареть между поиском и кликом.
    await page.locator(sel.input).first().press('Enter');
  } else {
    await page.locator(sel.sendButton).first().click();
  }

  try {
    await page.waitForFunction(
      ({ selector, was }) => document.querySelectorAll(selector).length > was,
      { selector: outgoingSelector, was: before },
      { timeout: CONFIRM_TIMEOUT_MS }
    );
  } catch {
    throw new SendFailed(
      `за ${CONFIRM_TIMEOUT_MS / 1000} с в переписке не появилось нового исходящего сообщения. ` +
      `Черновик НЕ помечать отправленным: скорее всего сессия умерла или сработал файрвол`
    );
  }

  const bubbles = page.locator(outgoingSelector);
  const last = ((await bubbles.nth((await bubbles.count()) - 1).textContent()) || '').trim();
  return { confirmed: true, lastOutgoing: last };
}
