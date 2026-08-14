/**
 * Разведка живой страницы мессенджера Авито.
 *
 * Зачем отдельный скрипт. selectors.json нельзя угадать: у Авито нет ни API,
 * ни стабильной вёрстки, а угаданный селектор хуже отсутствующего — он молча
 * читает ноль непрочитанных, и снаружи это выглядит как «пациенты не пишут».
 * Поэтому селекторы снимаются с ЖИВОЙ страницы, и решение принимает человек:
 * скрипт печатает КАНДИДАТОВ с числом совпадений и примером содержимого.
 *
 * Три правила, из которых следует всё остальное.
 *
 * Первое: только чтение. Это рабочий аккаунт клиники с живой перепиской.
 * Скрипт не печатает в поле ввода, ничего не отправляет и не открывает
 * непрочитанные диалоги — открытие пометило бы их прочитанными и увело бы
 * обращение пациента из-под носа администратора. Для съёма пузырей выбирается
 * диалог БЕЗ признаков непрочитанного.
 *
 * Второе: кандидат проверяется в той области, где его будут применять.
 * poll.mjs ищет unreadMark/counterpartyName/lastPreview ОТНОСИТЕЛЬНО элемента
 * чата (`item.locator(...)`), а outgoingMark/incomingMark/messageTime —
 * относительно пузыря. Селектор, найденный по документу, в этом контексте
 * может не совпасть ни разу. Поэтому все вложенные ключи считаются внутри
 * родителя, по каждому родителю отдельно.
 *
 * Третье: число совпадений показывается всегда, даже когда оно позорное.
 * Селектор, ловящий 1 элемент там, где чатов 12, очевидно неверен. Скрывать
 * это — значит отдать человеку красивый отчёт и сломанный бот.
 *
 * Направление сообщения — самое хрупкое место схемы. Без него бот примет свой
 * ответ за вопрос пациента и уйдёт в самопереписку. Поэтому кандидат на
 * направление не просто печатается: показывается РАСКЛАДКА, сколько пузырей он
 * относит к своим и сколько к чужим. Деление 0/N или N/0 означает, что признак
 * неверен, и об этом написано прямым текстом.
 *
 * Выхлоп: отчёт в stdout плюс ЧЕРНОВИК capture/selectors.discovered.json.
 * Именно .discovered: файл, от которого зависит отправка текста пациенту,
 * человек создаёт сам, осознанно, скопировав проверенные строки.
 *
 * Запуск:  node src/discover.mjs        (из каталога capture)
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
	checkSession,
	loadSelectors,
	MESSENGER_URL,
	openContext,
} from "./session.mjs";

/**
 * Корень проекта. fileURLToPath, а не ручная правка URL.pathname: на Windows
 * pathname выглядит как «/C:/…», path.resolve() поверх такой строки даёт
 * «c:\C:\…», а пробелы в пути остаются как «%20».
 */
const ROOT = path.resolve(
	path.dirname(fileURLToPath(import.meta.url)),
	"..",
	"..",
);
const OUT_FILE = path.join(ROOT, "capture", "selectors.discovered.json");

/** Сколько примеров содержимого печатать на кандидата. */
const SAMPLES = 3;
/** Обрезка примеров: в отчёт не должна попадать переписка целиком. */
const SAMPLE_LEN = 60;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** Признаки стены логина по URL — не зависят от вёрстки, проверяются первыми. */
const LOGIN_URL_MARKERS = [/\/login/i, /\/auth/i, /authorize/i];

/**
 * Широкие запасные селекторы для checkSession(), когда selectors.json ещё нет.
 * При первой разведке его и не может быть, а loadSelectors() на отсутствующем
 * файле бросает исключение — это верно для поллера и неверно здесь.
 * OR-список намеренно грубый: его задача — отличить мессенджер от формы входа,
 * а не снять структуру. Число совпадений по нему количеством чатов НЕ является.
 */
const FALLBACK_CHAT_LIST = [
	'[data-marker*="channel"]',
	'[data-marker*="messenger"]',
	'a[href*="/messenger/channel"]',
	'[class*="channels-list"]',
	'[class*="channel-preview"]',
].join(", ");

const FALLBACK_CHAT_ITEM = [
	'[data-marker*="channel-preview"]',
	'[data-marker*="channel_preview"]',
	'a[href*="/messenger/channel"]',
	'[class*="channel-preview"]',
].join(", ");

/**
 * Сбор кандидатов выполняется ОДНОЙ функцией в контексте страницы.
 *
 * Так, а не построением строк селекторов снаружи: любая сборка CSS из значений
 * атрибутов требует экранирования кавычек, скобок и юникода в именах классов, а
 * Авито кладёт в data-marker и пробелы, и кириллицу. Здесь селектор никогда не
 * собирается из текста — элементы обходятся напрямую, а наружу отдаётся уже
 * готовая строка вместе с числом совпадений, посчитанным тут же.
 *
 * Функция сериализуется Playwright-ом и не видит ничего из модуля, поэтому все
 * помощники объявлены внутри, а параметры приходят одним объектом.
 */
function probePage({ sampleCount, sampleLen, fallbackItem }) {
	const CSS_ESCAPE = (value) =>
		window.CSS && CSS.escape
			? CSS.escape(value)
			: String(value).replace(/[^\w-]/g, (ch) => `\\${ch}`);

	const cut = (text) => {
		const clean = (text || "").replace(/\s+/g, " ").trim();
		return clean.length > sampleLen ? `${clean.slice(0, sampleLen)}…` : clean;
	};

	/** Устойчивые селекторы, которыми элемент можно адресовать. Порядок = приоритет. */
	const selectorsFor = (el) => {
		const out = [];
		const tag = el.tagName.toLowerCase();

		// data-marker — собственная система разметки Авито для автотестов. Она
		// переживает смену вёрстки и обфускацию классов, поэтому идёт первой.
		for (const attr of el.attributes) {
			if (attr.name === "data-marker" || attr.name.startsWith("data-marker-")) {
				out.push(`[${attr.name}="${attr.value}"]`);
			}
		}
		// Роли и aria — второй по устойчивости слой: они завязаны на смысл.
		const role = el.getAttribute("role");
		if (role) out.push(`${tag}[role="${role}"]`);
		if (el.getAttribute("aria-label")) {
			out.push(`${tag}[aria-label="${el.getAttribute("aria-label")}"]`);
		}
		if (tag === "textarea" || tag === "input") {
			const name = el.getAttribute("name");
			if (name) out.push(`${tag}[name="${CSS_ESCAPE(name)}"]`);
			out.push(tag);
		}
		if (el.isContentEditable) out.push('[contenteditable="true"]');

		// Классы — последний слой и самый хрупкий: у Авито они хешированные
		// (channel-preview-root-XyZ12). Берётся стабильная часть до хеша, и такой
		// кандидат помечается в отчёте как ненадёжный.
		for (const cls of el.classList) {
			const stem = cls
				.split(/[-_]/)
				.filter((p) => !/^[a-zA-Z0-9]{5,}$/.test(p) || /^[a-z]+$/.test(p));
			if (stem.length >= 2)
				out.push(`[class*="${stem.slice(0, 2).join("-")}"]`);
		}
		return out;
	};

	/** Кандидат = селектор + сколько ловит + примеры. Считается в заданной области. */
	const measure = (selector, scopes) => {
		let total = 0;
		let scopesHit = 0;
		const samples = [];
		for (const scope of scopes) {
			let found;
			try {
				found = scope.querySelectorAll(selector);
			} catch {
				return null; // невалидный CSS — молча выбрасываем, а не роняем разведку
			}
			if (found.length) scopesHit += 1;
			total += found.length;
			for (const el of found) {
				if (samples.length < sampleCount) samples.push(cut(el.textContent));
			}
		}
		return { selector, total, scopesHit, scopes: scopes.length, samples };
	};

	/** Собрать и отранжировать кандидатов по набору элементов-образцов. */
	const candidatesFrom = (elements, scopes) => {
		const seen = new Map();
		for (const el of elements) {
			for (const selector of selectorsFor(el)) {
				if (seen.has(selector)) continue;
				const measured = measure(selector, scopes);
				if (measured && measured.total) seen.set(selector, measured);
			}
		}
		// Лучший кандидат покрывает больше областей: селектор, который нашёлся в
		// одном чате из двенадцати, поймал случайность вёрстки одного диалога.
		return [...seen.values()]
			.sort((a, b) => b.scopesHit - a.scopesHit || a.total - b.total)
			.slice(0, 6);
	};

	const doc = [document];
	const report = { keys: {}, notes: [], url: location.href };

	// --- список чатов --------------------------------------------------------
	const itemNodes = [...document.querySelectorAll(fallbackItem)];
	report.keys.chatItem = candidatesFrom(itemNodes.slice(0, 8), doc);
	report.counts = { chatItemsSeen: itemNodes.length };

	// Контейнер списка — общий родитель элементов чата, а не отдельная эвристика:
	// так он найдётся даже когда своей разметки у контейнера нет.
	const parents = new Set(
		itemNodes.map((el) => el.parentElement).filter(Boolean),
	);
	report.keys.chatList = candidatesFrom([...parents].slice(0, 4), doc);

	// --- внутри элемента чата -------------------------------------------------
	// Считается ПО КАЖДОМУ чату отдельно: poll.mjs ищет эти ключи относительно
	// item, и селектор, найденный по документу, здесь может не совпасть ни разу.
	const scopes = itemNodes.slice(0, 12);
	if (scopes.length) {
		const badges = [];
		const names = [];
		const previews = [];
		for (const item of scopes) {
			for (const el of item.querySelectorAll("*")) {
				const text = (el.textContent || "").trim();
				const marker = el.getAttribute("data-marker") || "";
				// Непрочитанное — это счётчик (короткое число) или явный маркер.
				if (
					/unread|badge|counter/i.test(marker) ||
					/unread|badge/i.test(el.className)
				) {
					badges.push(el);
				} else if (/^\d{1,3}$/.test(text) && el.children.length === 0) {
					badges.push(el);
				}
				if (/name|title|interlocutor|user/i.test(marker)) names.push(el);
				if (/preview|last|snippet|text/i.test(marker)) previews.push(el);
			}
		}
		report.keys.unreadMark = candidatesFrom(badges.slice(0, 10), scopes);
		report.keys.counterpartyName = candidatesFrom(names.slice(0, 10), scopes);
		report.keys.lastPreview = candidatesFrom(previews.slice(0, 10), scopes);
	}

	return report;
}

/**
 * Съём открытого диалога: пузыри, направление, поле ввода, кнопка отправки.
 *
 * Отдельным проходом от списка чатов, потому что выполняется на другой
 * странице — после того, как диалог открыт.
 */
function probeDialog({ sampleCount, sampleLen }) {
	const cut = (text) => {
		const clean = (text || "").replace(/\s+/g, " ").trim();
		return clean.length > sampleLen ? `${clean.slice(0, sampleLen)}…` : clean;
	};

	const attrSelectors = (el) => {
		const out = [];
		const tag = el.tagName.toLowerCase();
		for (const attr of el.attributes) {
			if (attr.name === "data-marker" || attr.name.startsWith("data-marker-")) {
				out.push(`[${attr.name}="${attr.value}"]`);
			}
		}
		const role = el.getAttribute("role");
		if (role) out.push(`${tag}[role="${role}"]`);
		const label = el.getAttribute("aria-label");
		if (label) out.push(`${tag}[aria-label="${label}"]`);
		for (const cls of el.classList) {
			const stem = cls.split(/[-_]/).slice(0, 2).join("-");
			if (stem.length >= 4) out.push(`[class*="${stem}"]`);
		}
		return out;
	};

	const measure = (selector, scopes) => {
		let total = 0;
		let scopesHit = 0;
		const samples = [];
		for (const scope of scopes) {
			let found;
			try {
				found = scope.querySelectorAll(selector);
			} catch {
				return null;
			}
			if (found.length) scopesHit += 1;
			total += found.length;
			for (const el of found) {
				if (samples.length < sampleCount) samples.push(cut(el.textContent));
			}
		}
		return { selector, total, scopesHit, scopes: scopes.length, samples };
	};

	const candidatesFrom = (elements, scopes) => {
		const seen = new Map();
		for (const el of elements) {
			for (const selector of attrSelectors(el)) {
				if (seen.has(selector)) continue;
				const measured = measure(selector, scopes);
				if (measured && measured.total) seen.set(selector, measured);
			}
		}
		return [...seen.values()]
			.sort((a, b) => b.scopesHit - a.scopesHit || a.total - b.total)
			.slice(0, 6);
	};

	const report = { keys: {}, flags: {}, notes: [], url: location.href };

	// --- пузыри ---------------------------------------------------------------
	// Пузырь опознаётся структурно: лист дерева с осмысленным текстом, у которого
	// много одноуровневых соседей такой же формы. Эвристика по маркерам сюда не
	// годится — у Авито пузырь бывает без своего data-marker.
	const textNodes = [...document.querySelectorAll("div, li, article")].filter(
		(el) => {
			const text = (el.textContent || "").trim();
			if (text.length < 2 || text.length > 2000) return false;
			return el.querySelectorAll("div, li, article").length <= 3;
		},
	);

	// Группируем по родителю: лента сообщений — это родитель с наибольшим числом
	// однотипных детей.
	const byParent = new Map();
	for (const el of textNodes) {
		const parent = el.parentElement;
		if (!parent) continue;
		if (!byParent.has(parent)) byParent.set(parent, []);
		byParent.get(parent).push(el);
	}
	const feed = [...byParent.entries()].sort(
		(a, b) => b[1].length - a[1].length,
	)[0];
	const bubbles = feed ? feed[1] : [];
	report.counts = { bubblesSeen: bubbles.length };
	report.keys.messageBubble = candidatesFrom(bubbles.slice(0, 12), [document]);

	// --- направление ----------------------------------------------------------
	// Самое хрупкое место всей схемы: без признака направления бот примет свой
	// ответ за вопрос пациента и уйдёт в самопереписку. Поэтому кандидат не
	// просто печатается — показывается, КАК он делит ленту.
	const splits = [];
	const tried = new Set();
	for (const bubble of bubbles.slice(0, 12)) {
		for (const selector of attrSelectors(bubble)) {
			if (tried.has(selector)) continue;
			tried.add(selector);
			let matched = 0;
			for (const b of bubbles) {
				try {
					if (b.matches(selector) || b.querySelector(selector)) matched += 1;
				} catch {
					matched = -1;
					break;
				}
			}
			if (matched <= 0 || matched >= bubbles.length) {
				// Признак, который относит к своим все пузыри или ни одного, не
				// является признаком направления. Это не «слабый кандидат», это
				// заведомо неверный — и в отчёт он идёт с явной пометкой.
				if (matched === 0 || matched === bubbles.length) {
					splits.push({
						selector,
						matched,
						total: bubbles.length,
						usable: false,
						why:
							matched === 0
								? "не совпал ни с одним пузырём"
								: "совпал со ВСЕМИ пузырями",
					});
				}
				continue;
			}
			splits.push({
				selector,
				matched,
				total: bubbles.length,
				usable: true,
				why: `делит ленту ${matched} / ${bubbles.length - matched}`,
			});
		}
	}
	// Годные вперёд, среди годных — те, что делят ленту ближе к пополам:
	// в живом диалоге своих и чужих сообщений примерно поровну.
	report.keys.direction = splits
		.sort((a, b) => {
			if (a.usable !== b.usable) return a.usable ? -1 : 1;
			const balance = (s) => Math.abs(s.matched - (s.total - s.matched));
			return balance(a) - balance(b);
		})
		.slice(0, 8);

	// --- поле ввода -----------------------------------------------------------
	// Вид поля определяется ФАКТИЧЕСКИ, а не угадывается по тегу: в
	// contenteditable fill() либо не работает, либо ломает разметку, и send.mjs
	// берёт эту инструкцию из selectors.json.
	const inputs = [
		...document.querySelectorAll(
			'textarea, input[type="text"], [contenteditable="true"], [role="textbox"]',
		),
	];
	report.keys.input = candidatesFrom(inputs, [document]);
	const field = inputs.find((el) => el.offsetParent !== null) || inputs[0];
	if (field) {
		report.flags.inputKind = field.isContentEditable
			? "contenteditable"
			: "textarea";
		report.flags.inputTag = field.tagName.toLowerCase();
	} else {
		report.notes.push(
			"поле ввода не найдено: диалог открыт? возможно, чат в архиве",
		);
	}

	// --- кнопка отправки ------------------------------------------------------
	const buttons = [
		...document.querySelectorAll('button, [role="button"]'),
	].filter((el) => {
		const hint = `${el.getAttribute("aria-label") || ""} ${el.getAttribute("data-marker") || ""} ${el.textContent || ""}`;
		return /send|отправ/i.test(hint);
	});
	report.keys.sendButton = candidatesFrom(buttons, [document]);
	if (!buttons.length) {
		report.notes.push(
			"кнопка отправки не опознана — вероятно, отправка по Enter",
		);
	}

	// --- id сообщения и id чата ----------------------------------------------
	const idAttrs = new Set();
	for (const bubble of bubbles.slice(0, 12)) {
		for (const attr of bubble.attributes) {
			if (/^(data-)?(id|key|message-id)$/i.test(attr.name) && attr.value) {
				idAttrs.add(attr.name);
			}
		}
	}
	report.keys.messageId = [...idAttrs];

	return report;
}

// --- отчёт ------------------------------------------------------------------

function printCandidates(title, candidates, { required = false } = {}) {
	console.log(`\n${title}${required ? " (обязательный)" : ""}`);
	if (!candidates || !candidates.length) {
		console.log("  кандидатов не найдено");
		return null;
	}
	for (const item of candidates) {
		const scope =
			item.scopes > 1 ? `, найден в ${item.scopesHit} из ${item.scopes}` : "";
		console.log(`  ${item.selector}`);
		console.log(`      совпадений ${item.total}${scope}`);
		if (item.samples?.length) {
			console.log(
				`      примеры: ${item.samples.filter(Boolean).join(" | ") || "(без текста)"}`,
			);
		}
	}
	return candidates[0].selector;
}

function printDirection(candidates) {
	console.log("\nПРИЗНАК НАПРАВЛЕНИЯ (свои сообщения против чужих)");
	console.log(
		"  Без него бот примет свой ответ за вопрос пациента и уйдёт в самопереписку.",
	);
	if (!candidates || !candidates.length) {
		console.log(
			"  кандидатов не найдено — заполнять outgoingMark/incomingMark руками",
		);
		return null;
	}
	let best = null;
	for (const item of candidates) {
		const verdict = item.usable ? "годен" : "НЕ ГОДЕН";
		console.log(`  [${verdict}] ${item.selector}`);
		console.log(`      ${item.why}`);
		if (item.usable && !best) best = item.selector;
	}
	if (!best) {
		console.log(
			"\n  Ни один кандидат не делит ленту. Это НЕ повод выбрать лучший из них:",
		);
		console.log(
			"  признак, совпадающий со всеми пузырями или ни с одним, направление",
		);
		console.log("  не определяет. Смотреть разметку глазами через DevTools.");
	}
	return best;
}

/** Проверка сессии без selectors.json: его при первой разведке ещё нет. */
async function sessionAlive(page) {
	await page.goto(MESSENGER_URL, {
		waitUntil: "domcontentloaded",
		timeout: 40_000,
	});
	await sleep(2000); // список чатов дорисовывается асинхронно
	const url = page.url();
	if (LOGIN_URL_MARKERS.some((re) => re.test(url))) {
		return { alive: false, url, detail: "редирект на страницу входа" };
	}
	const chats = await page.locator(FALLBACK_CHAT_ITEM).count();
	if (!chats) {
		return {
			alive: false,
			url,
			detail:
				"ни одного элемента чата по широким запасным селекторам — " +
				"либо сессия мертва, либо вёрстка изменилась целиком",
		};
	}
	return { alive: true, url, chats, detail: `элементов чата ${chats}` };
}

async function main() {
	// Существующий selectors.json не обязателен, но если он есть — показать, с
	// чем сравнивать: разведка чаще всего запускается после того, как Авито сломал
	// рабочие селекторы, и разница важнее абсолютных значений.
	let current = null;
	try {
		current = loadSelectors(ROOT);
	} catch {
		current = null;
	}

	const ctx = await openContext({ headless: true });
	try {
		const page = ctx.pages()[0] || (await ctx.newPage());

		const health = await sessionAlive(page);
		if (!health.alive) {
			console.error(`СЕССИЯ НЕ ГОДНА: ${health.detail}`);
			console.error(`  url: ${health.url}`);
			console.error(
				"  Кандидатов со страницы логина не выдаём — они были бы мусором.",
			);
			console.error("  Нужен разовый вход руками: npm run login");
			return 1;
		}
		console.log(`Сессия жива: ${health.detail}`);
		console.log(
			`Файл selectors.json: ${current ? "есть, сверяйтесь с ним" : "отсутствует"}`,
		);

		const list = await page.evaluate(probePage, {
			sampleCount: SAMPLES,
			sampleLen: SAMPLE_LEN,
			fallbackItem: FALLBACK_CHAT_ITEM,
		});

		console.log("\n================ СПИСОК ЧАТОВ ================");
		console.log(`Элементов чата на странице: ${list.counts.chatItemsSeen}`);
		const draft = {};
		draft.chatList = printCandidates("chatList", list.keys.chatList, {
			required: true,
		});
		draft.chatItem = printCandidates("chatItem", list.keys.chatItem, {
			required: true,
		});
		draft.unreadMark = printCandidates("unreadMark", list.keys.unreadMark, {
			required: true,
		});
		draft.counterpartyName = printCandidates(
			"counterpartyName",
			list.keys.counterpartyName,
		);
		draft.lastPreview = printCandidates("lastPreview", list.keys.lastPreview);

		// Открывается ПРОЧИТАННЫЙ диалог: открытие непрочитанного пометило бы его
		// прочитанным и увело обращение пациента из-под носа администратора.
		const items = page.locator(draft.chatItem || FALLBACK_CHAT_ITEM);
		const total = await items.count();
		let opened = -1;
		for (let i = 0; i < total; i += 1) {
			if (!draft.unreadMark) {
				opened = i;
				break;
			}
			if ((await items.nth(i).locator(draft.unreadMark).count()) === 0) {
				opened = i;
				break;
			}
		}
		if (opened < 0) {
			console.log(
				"\nВсе диалоги непрочитаны — не открываем ни один, чтобы не пометить",
			);
			console.log(
				"их прочитанными. Запустите разведку позже или после ответа администратора.",
			);
			writeDraft(draft, list, null);
			return 0;
		}

		await items.nth(opened).click();
		await sleep(2500);

		const dialog = await page.evaluate(probeDialog, {
			sampleCount: SAMPLES,
			sampleLen: SAMPLE_LEN,
		});

		console.log("\n================ ОТКРЫТЫЙ ДИАЛОГ ================");
		console.log(`Пузырей в ленте: ${dialog.counts.bubblesSeen}`);
		draft.messageBubble = printCandidates(
			"messageBubble",
			dialog.keys.messageBubble,
			{ required: true },
		);
		const direction = printDirection(dialog.keys.direction);
		draft.input = printCandidates("input", dialog.keys.input, {
			required: true,
		});
		draft.sendButton = printCandidates("sendButton", dialog.keys.sendButton, {
			required: true,
		});

		if (dialog.flags.inputKind) {
			console.log(
				`\ninputKind: ${dialog.flags.inputKind} (тег ${dialog.flags.inputTag})`,
			);
			draft.inputKind = dialog.flags.inputKind;
		}
		if (dialog.keys.messageId?.length) {
			console.log(
				`messageId: атрибуты на пузыре — ${dialog.keys.messageId.join(", ")}`,
			);
			draft.messageId = dialog.keys.messageId[0];
		} else {
			console.log(
				"messageId: стабильного id в DOM нет — дедуп пойдёт по отпечатку",
			);
		}

		// chatIdFromUrl проверяется по фактическому URL открытого диалога.
		const match = /\/messenger\/channel\/([A-Za-z0-9_-]+)/.exec(page.url());
		if (match) {
			draft.chatIdFromUrl = "/messenger/channel/([A-Za-z0-9_-]+)";
			console.log(`chatIdFromUrl: подходит, id чата «${match[1]}»`);
		} else {
			console.log(
				`chatIdFromUrl: шаблон не совпал с ${page.url()} — правьте вручную`,
			);
		}

		for (const note of [...list.notes, ...dialog.notes])
			console.log(`ЗАМЕЧАНИЕ: ${note}`);
		writeDraft(draft, list, direction);
		return 0;
	} finally {
		await ctx.close();
	}
}

function writeDraft(draft, list, direction) {
	if (direction) draft.outgoingMark = direction;
	// sendByEnter не определяется автоматически: проверить его можно только
	// отправкой, а этот скрипт ничего не отправляет. Значение по умолчанию
	// осознанно консервативное — клик по кнопке виден в DOM, нажатие Enter нет.
	draft.sendByEnter = false;

	const missing = [
		"chatList",
		"chatItem",
		"unreadMark",
		"messageBubble",
		"input",
		"sendButton",
	].filter((key) => !draft[key]);
	const payload = {
		_:
			"ЧЕРНОВИК разведки. Проверьте каждую строку и скопируйте в selectors.json " +
			"руками: от этого файла зависит, кому уйдёт текст.",
		_missing: missing,
		_direction: direction
			? "признак направления проверен на живой ленте, см. отчёт"
			: "ПРИЗНАК НАПРАВЛЕНИЯ НЕ ОПРЕДЕЛЁН — заполнить руками, иначе бот ответит сам себе",
		...draft,
	};
	fs.writeFileSync(OUT_FILE, `${JSON.stringify(payload, null, 2)}\n`, "utf8");

	console.log(`\nЧерновик записан: ${OUT_FILE}`);
	if (missing.length) {
		console.log(`НЕ НАЙДЕНЫ обязательные ключи: ${missing.join(", ")}`);
	}
	if (!direction) {
		console.log(
			"НЕ НАЙДЕН признак направления — без него запускать поллер нельзя.",
		);
	}
	console.log(
		"Это черновик, а не selectors.json. Скопируйте проверенные строки сами.",
	);
}

if (process.argv[1]?.endsWith("discover.mjs")) {
	main()
		.then((code) => process.exit(code))
		.catch((err) => {
			console.error(`ОШИБКА: ${err.message}`);
			process.exit(2);
		});
}
