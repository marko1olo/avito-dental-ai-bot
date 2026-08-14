/**
 * Гибридный загрузчик базы данных SQLite.
 *
 * Если установлен `better-sqlite3`, используется он.
 * Если `better-sqlite3` не установлен (например, в Node 24 без средств сборки C++),
 * используется встроенный в Node.js модуль `node:sqlite` (DatabaseSync).
 */
import { DatabaseSync } from "node:sqlite";

let Database;
try {
	Database = (await import("better-sqlite3")).default;
} catch {
	Database = class BuiltinDatabase {
		constructor(filename) {
			this._db = new DatabaseSync(filename);
		}
		pragma(str) {
			this._db.exec("PRAGMA " + str);
		}
		prepare(sql) {
			return this._db.prepare(sql);
		}
		exec(sql) {
			this._db.exec(sql);
		}
		transaction(fn) {
			return (...args) => {
				this._db.exec("BEGIN IMMEDIATE");
				try {
					const res = fn(...args);
					this._db.exec("COMMIT");
					return res;
				} catch (err) {
					this._db.exec("ROLLBACK");
					throw err;
				}
			};
		}
		close() {
			this._db.close();
		}
	};
}

export default Database;
