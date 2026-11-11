import { createRequire } from "node:module";
import z from "@deepseek-ai/schemastery";
import { createHash } from "node:crypto";
import { copyFileSync, existsSync, lstatSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, realpathSync, rmSync, statSync, writeFileSync } from "node:fs";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import os, { homedir } from "node:os";
//#region src/core/errors.ts
/** dsh-fish 的全部可预期错误。 */
var FishError = class extends Error {
	code;
	hint;
	details;
	constructor(code, message, options = {}) {
		super(message, options.cause === void 0 ? void 0 : { cause: options.cause });
		this.name = "FishError";
		this.code = code;
		this.hint = options.hint;
		this.details = options.details;
	}
	/** 供日志/命令回显使用：不含凭证，可直接序列化。 */
	toJSON() {
		return {
			name: this.name,
			code: this.code,
			message: this.message,
			hint: this.hint,
			details: this.details
		};
	}
	/** 面向人（斜杠命令回显）的一行/两行文本。 */
	format() {
		return this.hint ? `${this.code}: ${this.message}\n下一步：${this.hint}` : `${this.code}: ${this.message}`;
	}
};
/** 契约门禁专用：版本 / 指纹 / 宿主版本不匹配。 */
var ContractMismatchError = class extends FishError {
	constructor(message, options = {}) {
		super("CONTRACT_MISMATCH", message, options);
		this.name = "ContractMismatchError";
	}
};
/** spec 层问题（缺文件、JSON 不合法、必需字段缺失、自校验失败）。 */
var SpecError = class extends FishError {
	constructor(code, message, options = {}) {
		super(code, message, options);
		this.name = "SpecError";
	}
};
/** 数据库层问题（打不开、schema 漂移、只读约束被违反）。 */
var DatabaseError = class extends FishError {
	constructor(code, message, options = {}) {
		super(code, message, options);
		this.name = "DatabaseError";
	}
};
/**
* 把任意 `unknown` 收敛成 `FishError`。
* 为什么需要：catch 到的可能是字符串、DOMException、SQLite 的原生错误；不收敛的话
* 上层永远只能 `String(err)`，而那会丢掉 code（=丢掉"该怎么修"）。
*/
function toFishError(error) {
	if (error instanceof FishError) return error;
	if (error instanceof Error) return new FishError("INTERNAL", error.message, { cause: error });
	return new FishError("INTERNAL", String(error));
}
/**
* 以只读方式打开 fish 主库。
* @throws DatabaseError `DB_UNAVAILABLE` —— 打不开（路径不存在 / 被独占锁住 / 不是 SQLite）
*/
function openReadOnly(dbPath, options = {}) {
	const busyTimeoutMs = options.busyTimeoutMs ?? 5e3;
	let db;
	try {
		db = new DatabaseSync(dbPath, { readOnly: true });
	} catch (error) {
		throw new DatabaseError("DB_UNAVAILABLE", `只读打开失败：${dbPath}`, {
			hint: "确认文件存在且不是被 Python 侧独占（`-wal` 文件存在是正常现象）",
			details: { dbPath },
			cause: error
		});
	}
	try {
		db.exec(`PRAGMA busy_timeout=${Math.max(0, Math.trunc(busyTimeoutMs))}`);
		db.exec("PRAGMA query_only=1");
	} catch (error) {
		db.close();
		throw new DatabaseError("DB_UNAVAILABLE", `设置只读 PRAGMA 失败：${error.message}`, {
			details: { dbPath },
			cause: error
		});
	}
	return db;
}
/** 表/视图/索引/触发器的数量统计（比"表数"更能发现"少读了一个文件"）。 */
function objectCounts(db) {
	const rows = db.prepare("SELECT type, COUNT(*) AS n FROM sqlite_master GROUP BY type ORDER BY type").all();
	const counts = {};
	for (const row of rows) counts[row.type] = row.n;
	return counts;
}
/**
* 业务表清单：排除 FTS 索引本体（`memory_fts`）、其影子表（`memory_fts_*`）与
* `sqlite_sequence`。口径 = 21 张（`03-调研-fish全量能力.md` §4 实测）。
*/
function businessTableNames(db) {
	return db.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name <> 'memory_fts' AND name NOT LIKE 'memory\\_fts\\_%' ESCAPE '\\' AND name <> 'sqlite_sequence' ORDER BY name").all().map((row) => row.name);
}
/**
* 自证只读：真去试一次写，确认 SQLite 拒绝它。
*
* 为什么"能写"必须被当成错误：影子期若有一条能写的路径，双写者会互相覆盖，
* 而覆盖的表现是"某些行莫名消失"——比崩溃难查得多。
* @returns `true` = 写被拒绝（正常）；`false` = 竟然写成功了（严重问题）
*/
function assertWriteRejected(db) {
	try {
		db.exec("CREATE TABLE __dsh_fish_readonly_probe (x INTEGER)");
	} catch {
		return true;
	}
	try {
		db.exec("DROP TABLE __dsh_fish_readonly_probe");
	} catch {}
	return false;
}
//#endregion
//#region src/core/fingerprint.ts
/**
* schema 指纹：把"库结构长什么样"变成一个可复算的字符串。
*
* 为什么影子期第一件事就是它：TS 侧只读、不迁移，但**读错结构**和"结构变了但没人告诉
* 插件"是同一种坏——后者会让投影查询静默返回空集。指纹是唯一能在启动一瞬间说
* "你读的库不是我以为的那个库"的手段。
*
* 规范化规则（必须可复算，写进产物里自证）：
*   1. 只取 `sqlite_master.sql` 非空的条目（自动索引没有 SQL，本来就不该进指纹）；
*   2. SQL 文本：删行尾 `;`、连续空白折叠成单个空格、首尾去空白；
*   3. 按 `(type, name)` 排序（顺序无关 >> 顺序相关，否则导出顺序变化会造假差异）；
*   4. 对 `[{type,name,table,sql}]` 的紧凑 JSON 求 sha256。
* 另给一份 `businessFingerprint`：排除 FTS 影子表（`memory_fts_*`）与 `sqlite_sequence`
* ——它们是 SQLite 的实现细节，重建/优化时可能变化，但**业务结构没变**。
*
* @module dsh-fish/core/fingerprint
*/
/** 规范化规则清单：产物的 `normalization` 字段直接引用它，避免两处措辞。 */
const NORMALIZATION_RULES = [
	"select sqlite_master rows where sql is not null",
	"sql: strip trailing semicolons",
	"sql: collapse whitespace runs to single space",
	"sql: trim",
	"sort objects by (type, name) asc",
	"sha256 over compact json of [{type,name,table,sql}]",
	"businessFingerprint: same, excluding memory_fts_* and sqlite_sequence"
];
/** 影子对象：SQLite 为实现 FTS5 自动建的表，不属于业务结构。 */
function isShadowObject(name) {
	return name.startsWith("memory_fts_") || name === "sqlite_sequence";
}
/** SQL 文本规范化（规则 2/3 的唯一实现）。 */
function normalizeSql(sql) {
	return sql.replace(/[\t\r\n]+/g, " ").replace(/ {2,}/g, " ").replace(/\s*;\s*$/g, "").trim();
}
/** sha256 十六进制串。 */
function sha256(text) {
	return createHash("sha256").update(text, "utf8").digest("hex");
}
/** 对一组对象算指纹（供全量与业务两档复用）。 */
function fingerprintOf(objects) {
	return sha256(JSON.stringify(objects.map((o) => ({
		type: o.type,
		name: o.name,
		table: o.table,
		sql: o.sql
	}))));
}
/**
* 从只读连接算指纹。
* @param db 已打开的连接（调用方负责只读）
*/
function computeSchemaFingerprint(db) {
	let rows;
	try {
		rows = db.prepare("SELECT type, name, tbl_name AS tbl_name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type ASC, name ASC").all();
	} catch (error) {
		throw new DatabaseError("DB_UNAVAILABLE", `读取 sqlite_master 失败：${error.message}`, {
			hint: "确认路径指向 fish 的 index.sqlite，且以只读方式能打开",
			cause: error
		});
	}
	const objects = rows.map((row) => {
		const sql = normalizeSql(row.sql);
		return {
			type: row.type,
			name: row.name,
			table: row.tbl_name,
			sql,
			hash: sha256(`${row.type}\u0000${row.name}\u0000${row.tbl_name}\u0000${sql}`).slice(0, 16)
		};
	});
	const counts = {};
	for (const object of objects) counts[object.type] = (counts[object.type] ?? 0) + 1;
	const business = objects.filter((object) => !isShadowObject(object.name));
	return {
		algorithm: "sha256",
		normalization: NORMALIZATION_RULES,
		source: "sqlite_master",
		counts,
		fingerprint: fingerprintOf(objects),
		businessFingerprint: fingerprintOf(business),
		objects
	};
}
/** 逐对象对比：比"两个 hash 不等"有用得多——它直接指出是哪张表变了。 */
function diffFingerprints(expected, actual) {
	const key = (o) => `${o.type}:${o.name}`;
	const expectedMap = new Map(expected.objects.map((o) => [key(o), o]));
	const actualMap = new Map(actual.objects.map((o) => [key(o), o]));
	const missing = [];
	for (const k of expectedMap.keys()) if (!actualMap.has(k)) missing.push(k);
	const unexpected = [];
	const changed = [];
	for (const [k, actualObject] of actualMap) {
		const expectedObject = expectedMap.get(k);
		if (expectedObject === void 0) unexpected.push(k);
		else if (expectedObject.hash !== actualObject.hash) changed.push(k);
	}
	return {
		fingerprintMatched: expected.fingerprint === actual.fingerprint,
		businessMatched: expected.businessFingerprint === actual.businessFingerprint,
		missing: missing.sort(),
		unexpected: unexpected.sort(),
		changed: changed.sort()
	};
}
//#endregion
//#region src/core/contract.ts
/**
* 契约门禁：**fail closed** —— 任一要素不匹配就抛错，插件拒绝启用。
*
* 为什么宁可"启动失败"也不要"带病启动"：影子期 TS 侧只读，读错结构的后果不是崩溃，
* 而是**静默返回空结果**（例如少了 `status='active'` 这一列）。那种坏会一路走到
* 注入包里说"这里什么都没有"。所以三处版本必须同时对上：
*
*   1. `contract.json.contractVersion` === 插件常量 `SUPPORTED_CONTRACT_VERSION`
*   2. `spec/schema.fingerprint.json` === 现网库实时算出的指纹
*   3. 宿主版本 >= `contract.json.minHostVersion`
*   4. 附带：`contract.json.specFiles` 里登记的文件摘要 === 文件实际摘要
*      （防止"只改了 events.json 忘了重算契约"这种半更新）
*
* `runStartupGate` 是 `apply()` 与验收脚本**共用的同一条路径**：验收脚本另写一条
* 轻量校验，等于验了个不存在的门。
*
* @module dsh-fish/core/contract
*/
/**
* 插件支持的契约版本。**升 spec 必须同时升这个常量**——这是"同一代"的唯一判据。
*/
const SUPPORTED_CONTRACT_VERSION = "v0";
/** 读取并解析 JSON 文件；失败时抛 `SpecError`（不是裸 SyntaxError）。 */
function readSpecJson(filePath) {
	let raw;
	try {
		raw = readFileSync(filePath, "utf8");
	} catch (error) {
		throw new SpecError("SPEC_MISSING", `读不到 spec 文件：${filePath}`, {
			hint: "跑 `node scripts/gen-spec.mjs` 重新生成 spec/",
			cause: error
		});
	}
	try {
		return JSON.parse(raw);
	} catch (error) {
		throw new SpecError("SPEC_INVALID", `spec 文件不是合法 JSON：${filePath}`, {
			hint: "检查该文件是否被手工改坏，或重新生成",
			cause: error
		});
	}
}
/** 文件 sha256（用于 spec 自校验）。 */
function fileSha256(filePath) {
	return createHash("sha256").update(readFileSync(filePath)).digest("hex");
}
/** 语义化版本比较：`a < b` 返回 -1，相等 0，`a > b` 返回 1。 */
function compareVersions(a, b) {
	const split = (value) => {
		const [corePart = "", prePart = ""] = value.trim().split("-", 2);
		return {
			core: corePart.split(".").map((piece) => {
				const n = Number.parseInt(piece, 10);
				return Number.isFinite(n) ? n : 0;
			}),
			pre: prePart === "" ? [] : prePart.split(".")
		};
	};
	const left = split(a);
	const right = split(b);
	const length = Math.max(left.core.length, right.core.length);
	for (let i = 0; i < length; i += 1) {
		const l = left.core[i] ?? 0;
		const r = right.core[i] ?? 0;
		if (l !== r) return l < r ? -1 : 1;
	}
	if (left.pre.length === 0 && right.pre.length === 0) return 0;
	if (left.pre.length === 0) return 1;
	if (right.pre.length === 0) return -1;
	const preLength = Math.max(left.pre.length, right.pre.length);
	for (let i = 0; i < preLength; i += 1) {
		const l = left.pre[i];
		const r = right.pre[i];
		if (l === void 0) return -1;
		if (r === void 0) return 1;
		const ln = Number.parseInt(l, 10);
		const rn = Number.parseInt(r, 10);
		const lNumeric = Number.isFinite(ln) && `${ln}` === l;
		const rNumeric = Number.isFinite(rn) && `${rn}` === r;
		if (lNumeric && rNumeric) {
			if (ln !== rn) return ln < rn ? -1 : 1;
		} else if (l !== r) return l < r ? -1 : 1;
	}
	return 0;
}
/** 校验 `contract.json` 的必需字段（缺字段=结构性错误，直接抛）。 */
function validateContractFile(raw, specDir) {
	const file = path.join(specDir, "contract.json");
	if (typeof raw !== "object" || raw === null) throw new SpecError("SPEC_INVALID", `contract.json 不是对象：${file}`);
	const candidate = raw;
	const version = candidate.contractVersion;
	const minHost = candidate.minHostVersion;
	if (typeof version !== "string" || version.trim() === "") throw new SpecError("SPEC_INVALID", `contract.json 缺少 contractVersion：${file}`);
	if (typeof minHost !== "string" || minHost.trim() === "") throw new SpecError("SPEC_INVALID", `contract.json 缺少 minHostVersion：${file}`);
	const implMatrix = candidate.implMatrix;
	if (!Array.isArray(implMatrix) || implMatrix.length === 0) throw new SpecError("SPEC_INVALID", `contract.json 缺少 implMatrix：${file}`);
	const capabilities = candidate.capabilities;
	if (!Array.isArray(capabilities) || capabilities.length === 0) throw new SpecError("SPEC_INVALID", `contract.json 缺少 capabilities：${file}`);
	const specFiles = candidate.specFiles;
	if (typeof specFiles !== "object" || specFiles === null) throw new SpecError("SPEC_INVALID", `contract.json 缺少 specFiles：${file}`);
	return {
		contractVersion: version,
		minHostVersion: minHost,
		implMatrix,
		capabilities,
		specFiles,
		...typeof candidate.concurrency === "object" && candidate.concurrency !== null ? { concurrency: candidate.concurrency } : {},
		...typeof candidate.shadowPolicy === "object" && candidate.shadowPolicy !== null ? { shadowPolicy: candidate.shadowPolicy } : {}
	};
}
/** 校验 spec 自摘要：登记文件必须存在且摘要一致。 */
function verifySpecDigests(contract, specDir) {
	const problems = [];
	const entries = Object.entries(contract.specFiles);
	for (const [relative, expected] of entries) {
		const filePath = path.join(specDir, relative);
		let actual;
		try {
			actual = fileSha256(filePath);
		} catch {
			problems.push(`${relative}: 文件缺失`);
			continue;
		}
		if (actual !== expected) problems.push(`${relative}: 摘要不一致（登记 ${expected.slice(0, 12)}…/实际 ${actual.slice(0, 12)}…）`);
	}
	return {
		verified: problems.length === 0,
		count: entries.length,
		problems
	};
}
/**
* 启动门禁：`apply()` 与验收脚本共用。
* @throws ContractMismatchError 版本 / 指纹 / 宿主版本不匹配
* @throws SpecError spec 文件缺失或不合法
*/
function runStartupGate(options) {
	const specDir = path.resolve(options.specDir);
	const contract = validateContractFile(readSpecJson(path.join(specDir, "contract.json")), specDir);
	const warnings = [];
	if (contract.contractVersion !== "v0") warnings.push(`契约版本不匹配：spec=${contract.contractVersion} / 插件支持=v0`);
	const digests = verifySpecDigests(contract, specDir);
	if (!digests.verified) warnings.push(`spec 自校验失败（${digests.problems.length} 个文件）：${digests.problems.slice(0, 3).join("; ")}`);
	const minHostVersion = contract.minHostVersion;
	const hostVersion = options.hostVersion ?? null;
	const strict = options.strictHostVersion === true;
	let hostVersionSatisfied = false;
	if (hostVersion !== null) if (compareVersions(hostVersion, minHostVersion) < 0) warnings.push(`宿主版本过低：${hostVersion} < ${minHostVersion}（继续运行，可能遇到 API 差异）`);
	else hostVersionSatisfied = true;
	else if (strict) throw new ContractMismatchError("探测不到宿主版本，且 strictHostVersion=true", {
		hint: "设置环境变量 DSH_HOST_VERSION，或把 strictHostVersion 关掉（默认关）",
		details: { minHostVersion }
	});
	const expected = readSpecJson(path.join(specDir, "schema.fingerprint.json"));
	if (expected.algorithm !== "sha256" || typeof expected.fingerprint !== "string") throw new SpecError("SPEC_INVALID", "schema.fingerprint.json 结构不合法（algorithm/fingerprint）", {
		hint: "跑 `node scripts/gen-spec.mjs` 重新生成",
		details: { algorithm: expected.algorithm }
	});
	const db = openReadOnly(options.dbPath);
	let actual;
	let objectCounts;
	try {
		actual = computeSchemaFingerprint(db);
		objectCounts = { ...actual.counts };
	} finally {
		db.close();
	}
	const delta = diffFingerprints(expected, actual);
	if (!delta.fingerprintMatched) warnings.push(`schema 指纹不匹配（missing ${delta.missing.length} / unexpected ${delta.unexpected.length} / changed ${delta.changed.length}）——继续运行，只读字段可能失效`);
	const businessTableCount = expected.objects.filter((object) => object.type === "table" && object.name !== "memory_fts" && !object.name.startsWith("memory_fts_") && object.name !== "sqlite_sequence").length;
	return {
		ok: true,
		contractVersion: contract.contractVersion,
		supportedContractVersion: "v0",
		minHostVersion,
		hostVersion,
		hostVersionSatisfied,
		fingerprintVerified: delta.fingerprintMatched && delta.businessMatched,
		fingerprint: actual.fingerprint,
		businessFingerprint: actual.businessFingerprint,
		specFingerprint: expected.fingerprint,
		objectCounts,
		businessTableCount,
		specDigestsVerified: digests.verified,
		specDigestCount: digests.count,
		warnings
	};
}
//#endregion
//#region src/core/host-version.ts
/**
* 宿主版本探测：契约门禁三要素之一（spec 版本 / schema 指纹 / 宿主版本）。
*
* 为什么"探测不到"不直接判死：插件可能被装在没有 `@deepseek-ai/dsh` 依赖的
* 独立进程里（例如验收脚本）。此时唯一诚实的结果是 `null`，由调用方按配置决定
* 「未知即放行」（默认，宽松）还是「未知即拒绝」（`strictHostVersion: true`，严）。
* 这两档的区别必须**可见**——所以门禁结果里永远带 `hostVersionSource`。
*
* @module dsh-fish/core/host-version
*/
const ENV_KEYS = ["DSH_HOST_VERSION", "DSH_DESKTOP_VERSION"];
/** 探测宿主版本（env 优先，其次解析 `@deepseek-ai/dsh` 的 package.json）。 */
function detectHostVersion() {
	for (const key of ENV_KEYS) {
		const raw = process.env[key];
		if (typeof raw === "string" && raw.trim() !== "") return {
			version: raw.trim(),
			source: "env",
			resolvedFrom: key
		};
	}
	try {
		const manifestPath = createRequire(import.meta.url).resolve("@deepseek-ai/dsh/package.json");
		const parsed = JSON.parse(readFileSync(manifestPath, "utf8"));
		if (typeof parsed === "object" && parsed !== null) {
			const version = parsed.version;
			if (typeof version === "string" && version.trim() !== "") return {
				version: version.trim(),
				source: "package",
				resolvedFrom: manifestPath
			};
		}
	} catch {}
	return {
		version: null,
		source: "unknown"
	};
}
//#endregion
//#region src/core/logger.ts
const LEVEL_ORDER = {
	debug: 10,
	info: 20,
	warn: 30,
	error: 40
};
function isHostLogger(value) {
	if (typeof value !== "object" || value === null) return false;
	const candidate = value;
	return typeof candidate.info === "function" && typeof candidate.error === "function";
}
/** 从 cordis 上下文尽力取出宿主 logger；取不到返回 `null`（调用方降级到 console）。 */
function pickHostLogger(ctx) {
	if (ctx === null) return null;
	const candidate = ctx.logger;
	if (typeof candidate === "function") {
		try {
			const produced = candidate("dsh-fish");
			if (isHostLogger(produced)) return produced;
		} catch {}
		return null;
	}
	return isHostLogger(candidate) ? candidate : null;
}
/** 创建日志器。`minLevel` 之下的一律丢弃（默认 `info`）。 */
function createLogger(ctx, options = {}) {
	const minLevel = options.minLevel ?? "info";
	return makeLogger(pickHostLogger(ctx), minLevel, options.scope ?? "dsh-fish");
}
function makeLogger(host, minLevel, scope) {
	const emit = (level, message, fields) => {
		if (LEVEL_ORDER[level] < LEVEL_ORDER[minLevel]) return;
		const record = {
			scope,
			message
		};
		if (fields !== void 0) record.fields = fields;
		if (host !== null) {
			host[level](record);
			return;
		}
		const line = JSON.stringify({
			level,
			...record
		});
		if (level === "error" || level === "warn") console.error(line);
		else console.log(line);
	};
	return {
		debug: (message, fields) => emit("debug", message, fields),
		info: (message, fields) => emit("info", message, fields),
		warn: (message, fields) => emit("warn", message, fields),
		error: (message, fields) => emit("error", message, fields),
		child: (subScope) => makeLogger(host, minLevel, `${scope}/${subScope}`)
	};
}
/** 静默日志器（测试用：断言输出时不该被日志干扰，连 error 一起吞掉）。 */
const SILENT_LOGGER = {
	debug: () => void 0,
	info: () => void 0,
	warn: () => void 0,
	error: () => void 0,
	child: () => SILENT_LOGGER
};
//#endregion
//#region src/core/paths.ts
/**
* 路径解析：**一处决定"库在哪、spec 在哪"**。
*
* 为什么不把路径散在各模块：影子期最容易出的事故是"读到了另一个库"（工作副本 / 备份 /
* 语料库），而这类事故的表现是"数据看起来少了几条"，不报错。所以路径只有这一个来源，
* 且默认值必须能自证（`specDir` 必须含 `contract.json`，否则宁可抛错）。
*
* 优先级：显式配置 > 环境变量 > 包内相对位置。
*
* @module dsh-fish/core/paths
*/
const PACKAGE_NAME = "dsh-fish";
/**
* 从当前模块向上找包根（含 `package.json` 且 `name === 'dsh-fish'`）。
* 同时适用于 tsx 直跑 `src/` 与构建后跑 `lib/` 两种布局——靠"向上找"而不是"数 ../../"，
* 因为数层级这种方式在加一层目录后必然静默指错。
*/
function findPackageRoot(startUrl = import.meta.url) {
	let dir = path.dirname(fileURLToPath(startUrl));
	for (let i = 0; i < 8; i += 1) {
		const manifest = path.join(dir, "package.json");
		if (existsSync(manifest)) try {
			const parsed = JSON.parse(readFileSync(manifest, "utf8"));
			if (typeof parsed === "object" && parsed !== null && parsed.name === PACKAGE_NAME) return dir;
		} catch {}
		const parent = path.dirname(dir);
		if (parent === dir) break;
		dir = parent;
	}
	throw new FishError("SPEC_INVALID", `找不到 ${PACKAGE_NAME} 的包根目录`, {
		hint: "从包内启动插件，或显式设置 specDir / fishHome 配置项",
		details: { startUrl }
	});
}
/** 环境变量覆盖（显式配置优先于它）。 */
function fromEnv(name) {
	const raw = process.env[name];
	return typeof raw === "string" && raw.trim() !== "" ? path.resolve(raw.trim()) : "";
}
/**
* 解析并校验全部路径。
* @throws FishError `SPEC_MISSING` —— specDir 里没有 `contract.json`
*/
function resolvePaths(overrides = {}, startUrl = import.meta.url) {
	const packageRoot = findPackageRoot(startUrl);
	const specDir = firstNonEmpty([
		overrides.specDir,
		fromEnv("DSH_FISH_SPEC_DIR"),
		path.join(packageRoot, "spec")
	]);
	if (!existsSync(path.join(specDir, "contract.json"))) throw new FishError("SPEC_MISSING", `契约层缺少 contract.json：${specDir}`, {
		hint: "先跑 `node scripts/gen-spec.mjs` 生成 spec/（生成脚本会校验产物齐全）",
		details: { specDir }
	});
	const fishHome = firstNonEmpty([
		overrides.fishHome,
		fromEnv("FISH_HOME"),
		path.resolve(packageRoot, "..", "p0")
	]);
	const dataDir = path.join(fishHome, "data");
	const dbPath = firstNonEmpty([
		overrides.dbPath,
		fromEnv("FISH_DB"),
		path.join(dataDir, "index.sqlite")
	]);
	if (!existsSync(dbPath)) throw new FishError("DB_UNAVAILABLE", `找不到 fish 主库：${dbPath}`, {
		hint: "显式设置 dbPath / FISH_DB，或确认 fishHome 指向含 data/index.sqlite 的目录",
		details: {
			dbPath,
			fishHome
		}
	});
	return {
		packageRoot,
		specDir,
		fishHome,
		dataDir,
		dbPath
	};
}
/** 取第一个非空字符串（`trim()` 后非空），返回绝对路径。 */
function firstNonEmpty(candidates) {
	for (const candidate of candidates) if (typeof candidate === "string" && candidate.trim() !== "") return path.resolve(candidate.trim());
	return "";
}
//#endregion
//#region src/domain/cli.ts
/**
* CLI 等价层：**`fish.py` 的每一条叶子命令都要有一个明确归属**。
*
* 宿主不允许插件给 `dsh` 二进制加子命令，所以"命令等价"只能落在斜杠命令面上：
* `/fish <一级> [二级...] [--k=v...]`。一级 35 个做命令名，二级起进 `rawInput`
* 原样透传（`ctx.commands` 只给 `rawInput`，没有参数 schema）。
*
* ## 为什么这张表是**人工裁决 + 机器校验**
*
* 命令树本身（谁是谁的子命令、几个叶子）由 `scripts/probe/dump-cli.py` 从 argparse
* 对象树里**机器枚举**出来——那是事实，不许手写。但"这条命令在影子期敢不敢执行"
* 是**裁决**，机器给不出答案：它取决于这条命令会不会写库、写文件、要不要独占、
* 会不会读到别的宿主才有的东西。
*
* 所以本模块只维护裁决（`CLI_ANNOTATIONS`），而 `buildCliMap()` 把裁决与枚举**交叉校验**：
*   · 枚举里有、表里没有 → 抛错（"漏一条"必须响亮，这正是判据 2 的红灯形态）；
*   · 表里有、枚举里没有 → 抛错（登记表不许声明一条不存在的命令）；
*   · `proxy-readonly` 必须 `kind==='read'` 且写清只读依据（源码行号）；
*   · `ts` 必须 `kind==='read'`（影子期 TS 侧没有写权限）；
*   · 非 `ts` 必须有替代命令，且替代命令**必须能在 `fish.py` 的命令树里解析到**
*     （或指向 spec 里真实存在的 MCP 工具名）——"未实现"三个字不许是终点。
*
* ## 四档归属（与架构方案 §6 对齐）
*
* | impl | 含义 | 影子期行为 |
* |---|---|---|
* | `ts` | TS 侧已有等价实现（T01–T03 交付的只读能力） | 直接调领域层 |
* | `proxy-readonly` | Python 侧命令是只读的，转发执行 | 在**只读快照副本**上跑 Python（见 `ports/python.ts`） |
* | `unsupported-shadow` | 写操作 | **拒绝执行** + 给替代（`fish <cmd>` / MCP 工具名） |
* | `python-only` | 长事务/独占/读宿主私有环境，只能人工在终端跑 | 只给指引，不执行 |
*
* @module dsh-fish/domain/cli
*/
/** 命令的副作用类别（与 `spec/tools.json` 的 `writeClass` 是两个口径，别混）。 */
const CLI_KINDS = [
	"read",
	"write",
	"ops"
];
/** 实现归属的**取值域**（生成器、判据、命令面共用这一份，绝不各写一份）。 */
const CLI_IMPLS = [
	"ts",
	"proxy-readonly",
	"unsupported-shadow",
	"python-only"
];
/**
* **裁决表**：76 条叶子逐条登记（`fish.py` 当前实测：35 一级 / 76 叶子 / 88 节点 / 最深 3 级）。
*
* 三条写表纪律（每一条都对着一次真实踩坑）：
*   1. `kind` 按**生产语义**判，不按"看起来像读"——`board show` 看着是读，实际会
*      `board_ensure` 建行（`p0/fish.py:11492` "取或建一块板"），所以它是 `write`；
*   2. `proxy-readonly` 只给**逐行核过没有写语句**的命令，`evidence` 写源码位置。
*      没核过的一律 `python-only`（宁可给人一条指引，不替人赌一次写）；
*   3. `reason` 说"为什么"，`replacement` 说"那我去哪做"——两样缺一条，这条标注就等于没说。
*/
const CLI_ANNOTATIONS = [
	{
		path: "remember",
		kind: "write",
		impl: "unsupported-shadow",
		reason: "写 memory（append 事件 + 投影 + FTS + links），影子期 TS 侧没有任何写路径",
		replacement: {
			cli: "remember",
			mcpTool: "remember",
			note: "终端里跑，或走仍在册的 MCP 工具"
		}
	},
	{
		path: "recall",
		kind: "read",
		impl: "ts",
		reason: "T02 已复刻 `searchMemories` / `renderRecall`，只读（不写 memory_access_log）"
	},
	{
		path: "list",
		kind: "read",
		impl: "proxy-readonly",
		reason: "只列 code_index（不含检索、不写审计），Python 侧是纯 SELECT；TS 未做同形渲染",
		evidence: "p0/fish.py:6919-6928（cmd_list：一条 SELECT code_index + 打印）",
		replacement: {
			cli: "list",
			note: "Python 侧纯读，可在快照副本上转发"
		}
	},
	{
		path: "neighbors",
		kind: "read",
		impl: "proxy-readonly",
		reason: "邻域五策略仍在 Python（TS 侧没有关联边读取原语：`links.ts` 已按第二批减重删除）",
		evidence: "p0/fish.py:6885-6916（cmd_neighbors）+ 6350-6407（neighbors()：无 INSERT/UPDATE/DELETE）",
		replacement: {
			cli: "neighbors",
			note: "Python 侧纯读，可在快照副本上转发"
		}
	},
	{
		path: "forgetting",
		kind: "write",
		impl: "unsupported-shadow",
		reason: "`--apply` 会把 dormant 落库并写事件（默认只打印计划）",
		replacement: {
			cli: "forgetting",
			note: "终端里先看计划，确认后再 --apply"
		}
	},
	{
		path: "distill",
		kind: "write",
		impl: "unsupported-shadow",
		reason: "`--write` 会落文件（同 tag 的 episode 聚成建议），影子期不产出文件",
		replacement: {
			cli: "distill",
			note: "终端里跑演练形态（不加 --write 就不写文件）"
		}
	},
	{
		path: "withdraw",
		kind: "write",
		impl: "unsupported-shadow",
		reason: "撤回一条记忆（改 status + 追加 events），是写操作",
		replacement: { cli: "withdraw" }
	},
	{
		path: "supersede",
		kind: "write",
		impl: "unsupported-shadow",
		reason: "把旧编码标记为被替代（事务写 + events），影子期只在 Python 侧做",
		replacement: {
			cli: "supersede",
			mcpTool: "supersede"
		}
	},
	{
		path: "owner",
		kind: "write",
		impl: "unsupported-shadow",
		reason: "`owner set` 会写 config/citizens（`show` 只读，但同一路径两种形态）",
		replacement: {
			cli: "owner",
			note: "终端里跑 `fish owner show` 只读；set 形态是写"
		}
	},
	{
		path: "owner-fix",
		kind: "write",
		impl: "unsupported-shadow",
		reason: "修正记忆归属（追加 events + 更新投影），MCP 面刻意不提供这条权限",
		replacement: { cli: "owner-fix" }
	},
	{
		path: "whoami",
		kind: "read",
		impl: "ts",
		reason: "T02 已复刻 `resolveIdentity` / `renderWhoami`（证据级别只降不升）"
	},
	{
		path: "events",
		kind: "read",
		impl: "proxy-readonly",
		reason: "只读 events 表最近 N 行；TS 未做这段渲染（事件只增不改，转发不改变语义）",
		evidence: "p0/fish.py:11418-11425（cmd_events：一条 SELECT … ORDER BY seq DESC LIMIT ?）",
		replacement: {
			cli: "events",
			note: "Python 侧纯读，可在快照副本上转发"
		}
	},
	{
		path: "init",
		kind: "ops",
		impl: "python-only",
		reason: "首次建库/建 home（SCHEMA + 目录），进程外一次性动作；插件内跑它等于\"插件替你建库\"",
		replacement: {
			cli: "init",
			note: "终端里跑（影子期插件不建库）"
		}
	},
	{
		path: "healthcheck",
		kind: "read",
		impl: "ts",
		reason: "T02 已复刻 `healthcheckReadings` / `renderHealthcheck`（七项读数）"
	},
	{
		path: "inject",
		kind: "read",
		impl: "python-only",
		reason: "TS 侧注入包复刻已按 2026-11-09 减重裁决删除（跟随 fish 漂移的主要耦合面）",
		replacement: {
			cli: "inject",
			note: "终端里跑 `fish inject`：Python 侧仍是注入包的权威口径"
		}
	},
	{
		path: "report",
		kind: "write",
		impl: "unsupported-shadow",
		reason: "渲染日报到 fish-home（写文件 + 可能有隐藏写路径），影子期不改 fish-home",
		replacement: { cli: "report" }
	},
	{
		path: "project-add",
		kind: "write",
		impl: "unsupported-shadow",
		reason: "登记一个新前缀（code_registry + events）",
		replacement: {
			cli: "project-add",
			mcpTool: "add_project"
		}
	},
	{
		path: "project retire",
		kind: "write",
		impl: "unsupported-shadow",
		reason: "退役前缀 = 停止分配新编码（`--apply` 才落库，默认只打印计划）",
		replacement: { cli: "project retire" }
	},
	{
		path: "project restore",
		kind: "write",
		impl: "unsupported-shadow",
		reason: "把 retired 改回 active（`--apply` 才落库）",
		replacement: { cli: "project restore" }
	},
	{
		path: "project close",
		kind: "write",
		impl: "unsupported-shadow",
		reason: "项目闭合（改状态 + events；`--apply` 才落库）",
		replacement: {
			cli: "project close",
			mcpTool: "close_project"
		}
	},
	{
		path: "project reopen",
		kind: "write",
		impl: "unsupported-shadow",
		reason: "把 closed 改回 active（`--apply` 才落库）",
		replacement: {
			cli: "project reopen",
			mcpTool: "reopen_project"
		}
	},
	{
		path: "project status",
		kind: "read",
		impl: "ts",
		reason: "T05 已复刻 `project_status_rows`（六列 + 每列来源 + 降级清单）。⚠ 生产**默认形态**会把渲染结果写进 `<home>/10-项目库/项目状态表.md`，`--json` 形态才纯读；影子期只实现纯读的那一半：同一份表格照常渲染，但**不落盘**，回执末尾明写\"没有写文件\"",
		evidence: "p0/fish.py:14517（project_status_rows 的 docstring 明写\"只读：不改任何表、不写盘\"）；写盘在 render_project_status:14645"
	},
	{
		path: "alias link",
		kind: "write",
		impl: "unsupported-shadow",
		reason: "建立名字别名（改检索语义；MCP 面刻意不给这条权限）",
		replacement: { cli: "alias link" }
	},
	{
		path: "alias unlink",
		kind: "write",
		impl: "unsupported-shadow",
		reason: "撤销别名（幂等写）",
		replacement: { cli: "alias unlink" }
	},
	{
		path: "alias list",
		kind: "read",
		impl: "ts",
		reason: "T05 已复刻 `cmd_alias` 的 list 分支：原始别名行 + 按 canonical 折叠的分组（单向展开，最多 8 跳）",
		evidence: "p0/fish.py:6722-6738（cmd_alias 的 else 分支：SELECT + resolve_name）"
	},
	{
		path: "workspace set",
		kind: "write",
		impl: "unsupported-shadow",
		reason: "workspace_map 是项目归属的唯一权威来源，set 是显式数据变更（写 events）",
		replacement: { cli: "workspace set" }
	},
	{
		path: "workspace remove",
		kind: "write",
		impl: "unsupported-shadow",
		reason: "删除映射（写 events 留痕）",
		replacement: { cli: "workspace remove" }
	},
	{
		path: "workspace list",
		kind: "read",
		impl: "ts",
		reason: "T05 已复刻 `_workspace_cmd_list`（`list_workspace_mappings` + 一行一条渲染）",
		evidence: "p0/fish.py:10036-10037 → 10065-10075（_workspace_cmd_list：list_workspace_mappings + 打印）"
	},
	{
		path: "workspace seed",
		kind: "read",
		impl: "proxy-readonly",
		reason: "打印建议**绝不自动写入**（源码 docstring 明写）",
		evidence: "p0/fish.py:10025-10028（\"list / seed 只读：seed 打印建议但绝不自动写入\"）",
		replacement: {
			cli: "workspace seed",
			note: "Python 侧纯读，可在快照副本上转发"
		}
	},
	{
		path: "board show",
		kind: "write",
		impl: "unsupported-shadow",
		reason: "读板前会 `board_ensure`（取或**建**一块板，会 INSERT boards 行），所以不是纯读",
		replacement: {
			cli: "board show",
			mcpTool: "board",
			note: "要只读看板内容请用 `/fish shadow presence`（只剩在场与动态两条 SELECT）"
		}
	},
	{
		path: "board checkin",
		kind: "write",
		impl: "unsupported-shadow",
		reason: "签到（upsert 自己的 presence 行 + events），影子期不写库",
		replacement: {
			cli: "board checkin",
			mcpTool: "board"
		}
	},
	{
		path: "board post",
		kind: "write",
		impl: "unsupported-shadow",
		reason: "往看板写一条动态（board_posts + events）",
		replacement: {
			cli: "board post",
			mcpTool: "board"
		}
	},
	{
		path: "board render",
		kind: "write",
		impl: "unsupported-shadow",
		reason: "把看板渲染成 fish-home 里的 Markdown（写文件 + 可能归一 boards.path）",
		replacement: { cli: "board render" }
	},
	{
		path: "board close",
		kind: "write",
		impl: "unsupported-shadow",
		reason: "关闭一块板（改 boards.status + events）",
		replacement: { cli: "board close" }
	},
	{
		path: "board sweep",
		kind: "write",
		impl: "unsupported-shadow",
		reason: "清理过期看板（DELETE + events）",
		replacement: { cli: "board sweep" }
	},
	{
		path: "layout render",
		kind: "write",
		impl: "unsupported-shadow",
		reason: "渲染门面文件树（写 fish-home/00-首页、10-项目库…），影子期不改 fish-home",
		replacement: {
			cli: "layout render",
			note: "终端里必须带 `--all`（本批只支持全量渲染）"
		}
	},
	{
		path: "layout verify",
		kind: "read",
		impl: "python-only",
		reason: "体检逻辑在 `verify_layout()` 辅助函数里（`p0/fish.py` 只做分发），未逐行核过写路径，不赌",
		replacement: {
			cli: "layout verify",
			note: "终端里跑，读数与文件树一致"
		}
	},
	{
		path: "kb tree",
		kind: "read",
		impl: "ts",
		reason: "T05 已复刻 `kb_tree`（统计层 + 显示层分开、双预算截断、深度口径）。影子期**不写 kb_access 审计**（生产每次读都追一条），所以\"未读/读次数\"里不会出现 DSH 的读取",
		evidence: "p0/fish_kb.py:1541-1665（kb_tree：只统计不读正文；`_kb_log_quiet(action=\"tree\")` 是唯一写点，影子期不写）"
	},
	{
		path: "kb open",
		kind: "read",
		impl: "ts",
		reason: "T05 已复刻 `kb_open`（8 类路径拒绝 + 敏感名单二次授权 + 编码/媒体双判 + 截断声明）。影子期不写 kb_access",
		evidence: "p0/fish_kb.py:1731-1834（kb_open；正文读取一律 read_bytes，审计写点不实现）"
	},
	{
		path: "kb search",
		kind: "read",
		impl: "ts",
		reason: "T05 已复刻 `kb_search`（按需扫描、不建索引、敏感文件不进片段、片段 240 字、排序 -occurrences/-mtime/rel）",
		evidence: "p0/fish_kb.py:1966-2108（kb_search：只扫 .md，query 只以 hash 进审计；影子期不写审计）"
	},
	{
		path: "kb stats",
		kind: "read",
		impl: "ts",
		reason: "T05 已复刻 `kb_stats`（规模/未读/最近变更/每库读写计数）；镜像/离线两档来源如实标注",
		evidence: "p0/fish_kb.py:2673-2729（kb_stats：_kb_scan_counts + 四条 kb_access 聚合，纯 SELECT）"
	},
	{
		path: "kb toc",
		kind: "read",
		impl: "proxy-readonly",
		reason: "目录快照（分类概览 + 前 3 标题 + 分页，2026-10-28 新增；纯读、不写 kb_access）——TS 侧尚未复刻，影子期在快照副本上只读转发",
		evidence: "p0/fish.py:13765（add_parser \"toc\"）+ 13411-13412（act==\"toc\" → _kb_cmd_toc）",
		replacement: {
			cli: "kb toc",
			note: "Python 侧纯读，可在快照副本上转发"
		}
	},
	{
		path: "kb doctor",
		kind: "ops",
		impl: "python-only",
		reason: "知识库体检（可能修索引/登记），运维动作只能在终端人工跑",
		replacement: { cli: "kb doctor" }
	},
	{
		path: "kb write",
		kind: "write",
		impl: "unsupported-shadow",
		reason: "写知识库 Markdown（追加/更新 + kb_access），影子期不写用户知识库",
		replacement: {
			cli: "kb write",
			mcpTool: "kb_write"
		}
	},
	{
		path: "kb rollback",
		kind: "write",
		impl: "unsupported-shadow",
		reason: "按备份回滚一次写入（改文件 + kb_access）",
		replacement: {
			cli: "kb rollback",
			mcpTool: "kb_write"
		}
	},
	{
		path: "kb vault list",
		kind: "read",
		impl: "ts",
		reason: "T05 已复刻 `_kb_cmd_vault` 的 list 分支（`kb_vault_list`：默认库在前，其次按名字；一行 `_kb_vault_line`）",
		evidence: "p0/fish_kb.py:567-571 + 2984-2993（kb_vault_list 纯 SELECT + list 渲染）"
	},
	{
		path: "kb vault add",
		kind: "write",
		impl: "unsupported-shadow",
		reason: "登记一个 vault（写 kb_vault + events）",
		replacement: { cli: "kb vault add" }
	},
	{
		path: "kb vault remove",
		kind: "write",
		impl: "unsupported-shadow",
		reason: "注销 vault（改 kb_vault 状态）",
		replacement: { cli: "kb vault remove" }
	},
	{
		path: "kb vault set-default",
		kind: "write",
		impl: "unsupported-shadow",
		reason: "改默认 vault（写 config/kb_vault）",
		replacement: { cli: "kb vault set-default" }
	},
	{
		path: "kb vault set-mode",
		kind: "write",
		impl: "unsupported-shadow",
		reason: "改 vault 读写模式（ro/rw）——这直接决定别人能不能写你的知识库",
		replacement: { cli: "kb vault set-mode" }
	},
	{
		path: "kb vault scan",
		kind: "ops",
		impl: "python-only",
		reason: "扫描 vault 目录（遍历文件系统 + 可能写登记），运维动作只在终端跑",
		replacement: { cli: "kb vault scan" }
	},
	{
		path: "kb backup prune",
		kind: "write",
		impl: "unsupported-shadow",
		reason: "按保留策略删知识库备份（删除不可逆）",
		replacement: { cli: "kb backup prune" }
	},
	{
		path: "corpus ingest",
		kind: "ops",
		impl: "python-only",
		reason: "入库是秒级以上长任务（独占 + 游标 + 脱敏），进程内跑会卡住宿主事件循环",
		replacement: {
			cli: "corpus ingest",
			note: "终端里跑（T03.5 已定档 audit/幂等，但执行面仍在 Python）"
		}
	},
	{
		path: "corpus stats",
		kind: "read",
		impl: "ts",
		reason: "T05 已复刻 `fish_corpus.stats`（段/会话/by_host/by_project/degraded/orphan/台账四档/滞后/体积）。读的是独立库 `corpus.sqlite`（只读打开），不是主库快照",
		evidence: "p0/fish_corpus.py:575-703（stats：库不存在返回 db_missing 不建库；readonly=True 打开）"
	},
	{
		path: "corpus search",
		kind: "read",
		impl: "ts",
		reason: "T05 已复刻 `fish_corpus.search`（MATCH + 过滤下推、0.6/0.2/0.2 排序、出口二次脱敏）。影子期不写 `corpus_access_log`（生产每次检索都追一条）",
		evidence: "p0/fish_corpus.py:892-967（search：只读连接；`_log_access` 是唯一写点，影子期不写）"
	},
	{
		path: "corpus read",
		kind: "read",
		impl: "ts",
		reason: "T05 已复刻 `fish_corpus.read`（门牌三档 + 段级脱敏 + 源文件缺失声明）。影子期不写访问审计、**也不回填** `push_log.reaction`（那是主库写）",
		evidence: "p0/fish_corpus.py:990-1043（read：投影正文 + source 状态；MCP 的采纳回填在 mcp_server.py:2056-2062）"
	},
	{
		path: "corpus retag",
		kind: "write",
		impl: "python-only",
		reason: "改语料标签（写 corpus.sqlite），长事务只能在终端跑",
		replacement: { cli: "corpus retag" }
	},
	{
		path: "corpus forget",
		kind: "write",
		impl: "python-only",
		reason: "按来源遗忘语料（DELETE，不可逆），必须在终端人工确认后跑",
		replacement: { cli: "corpus forget" }
	},
	{
		path: "corpus rebuild",
		kind: "ops",
		impl: "python-only",
		reason: "重建语料库（`--apply` 才落库，秒级长任务 + 独占）",
		replacement: { cli: "corpus rebuild" }
	},
	{
		path: "corpus doctor",
		kind: "ops",
		impl: "python-only",
		reason: "语料库体检（可能修索引），运维动作只在终端跑",
		replacement: { cli: "corpus doctor" }
	},
	{
		path: "push stats",
		kind: "read",
		impl: "proxy-readonly",
		reason: "只读推送效果度量（push_stats 无写语句）",
		evidence: "p0/fish.py:10753-10754 + 10612-10715（push_stats：全 SELECT/聚合）",
		replacement: {
			cli: "push stats",
			note: "Python 侧纯读，可在快照副本上转发"
		}
	},
	{
		path: "push status",
		kind: "read",
		impl: "proxy-readonly",
		reason: "只读推送配置（get_config 七次）",
		evidence: "p0/fish.py:10759-10764（cmd_push 的 else 分支：get_config 打印）",
		replacement: {
			cli: "push status",
			note: "Python 侧纯读，可在快照副本上转发"
		}
	},
	{
		path: "push sweep",
		kind: "write",
		impl: "unsupported-shadow",
		reason: "把超窗推送收口成 ignored（写 push_log.reaction）",
		replacement: { cli: "push sweep" }
	},
	{
		path: "activity stats",
		kind: "read",
		impl: "python-only",
		reason: "读数依赖 `data/_audit/hooks/pretool-asks.jsonl`（快照副本里没有）→ 转发会漏报撞车询问条数",
		replacement: {
			cli: "activity stats",
			note: "终端里跑（读的是本机审计文件）"
		}
	},
	{
		path: "activity prune",
		kind: "write",
		impl: "unsupported-shadow",
		reason: "删除超过保留期的 tool_activity 行（DELETE，不可逆）",
		replacement: { cli: "activity prune" }
	},
	{
		path: "session start",
		kind: "ops",
		impl: "python-only",
		reason: "session 是 CLI 专属命令族（MCP 面不签发凭证），要读宿主 env/payload 才能算身份",
		replacement: { cli: "session start" }
	},
	{
		path: "session status",
		kind: "read",
		impl: "python-only",
		reason: "同上：会话状态要读宿主环境（DSH_SESSION_*），插件进程里读到的是**它自己的**进程环境",
		replacement: { cli: "session status" }
	},
	{
		path: "session end",
		kind: "write",
		impl: "python-only",
		reason: "关闭会话（回收 token + 写 events），只能在会话真身所在进程外做",
		replacement: { cli: "session end" }
	},
	{
		path: "session agent",
		kind: "ops",
		impl: "python-only",
		reason: "给会话绑 agent 标识（读宿主载荷 + 写 session_tokens）",
		replacement: { cli: "session agent" }
	},
	{
		path: "prompt-push",
		kind: "ops",
		impl: "python-only",
		reason: "UserPromptSubmit hook 的子进程入口（读 stdin 的 hook payload），不是人手动跑的命令",
		replacement: { cli: "prompt-push" }
	},
	{
		path: "citizen-add",
		kind: "write",
		impl: "unsupported-shadow",
		reason: "登记一个公民（citizens + events）",
		replacement: { cli: "citizen-add" }
	},
	{
		path: "citizen-list",
		kind: "read",
		impl: "proxy-readonly",
		reason: "只列公民（无写语句），TS 未做这段渲染",
		evidence: "p0/fish.py:9992-10016（cmd_citizen_list：SELECT + 打印）",
		replacement: {
			cli: "citizen-list",
			note: "Python 侧纯读，可在快照副本上转发"
		}
	},
	{
		path: "citizen-retire",
		kind: "write",
		impl: "unsupported-shadow",
		reason: "退役公民（要求显式 --migrate-to/--force，避免静默丢归属）",
		replacement: { cli: "citizen-retire" }
	},
	{
		path: "citizen-merge",
		kind: "write",
		impl: "unsupported-shadow",
		reason: "合并两个公民（默认 dry-run 打印计划；`--apply` 会把 owner/session 归属迁到 dst 并写 events）——影子期不写，也不替用户决定\"哪两个身份其实是同一个\"",
		evidence: "p0/fish.py:8501-8540（citizen_merge：apply=False 只统计）+ 8543-8550（cmd_citizen_merge）",
		replacement: {
			cli: "citizen-merge",
			note: "终端里先跑 dry-run 看计划，确认后再 --apply"
		}
	},
	{
		path: "archive",
		kind: "write",
		impl: "unsupported-shadow",
		reason: "归档 = 搬行（INSERT 冷表 + DELETE 热表 + 写文件），默认 dry-run、`--apply` 才落库",
		replacement: {
			cli: "archive",
			note: "终端里先看计划，确认后再 --apply"
		}
	},
	{
		path: "rebuild",
		kind: "ops",
		impl: "python-only",
		reason: "重建 FTS/投影（长事务 + 独占），进程内跑会阻塞宿主事件循环",
		replacement: { cli: "rebuild" }
	},
	{
		path: "reindex",
		kind: "ops",
		impl: "python-only",
		reason: "重建索引（长事务 + 独占）",
		replacement: { cli: "reindex" }
	},
	{
		path: "config",
		kind: "write",
		impl: "unsupported-shadow",
		reason: "同一路径两种形态：无参列出（读）/ 带参 `set_config` 写库（p0/fish.py:10589）",
		replacement: {
			cli: "config",
			note: "终端里跑 `fish config`（只读列表）或 `fish config <key> <value>`（写）"
		}
	},
	{
		path: "ui serve",
		kind: "ops",
		impl: "python-only",
		reason: "起一个只读 HTTP 读口（绑回环、mode=ro + query_only）。属运维动作，影子期 TS 侧不提供",
		replacement: {
			cli: "ui serve",
			note: "终端里跑 `python p0/fish.py ui serve --port 8787`"
		}
	},
	{
		path: "ui dump",
		kind: "read",
		impl: "python-only",
		reason: "打印一份只读快照 JSON（调试/对拍用）。虽然只读，但要读整个主库并做撞车聚合，TS 侧没有同形实现，故不标 proxy-readonly",
		replacement: {
			cli: "ui dump",
			note: "终端里跑 `python p0/fish.py ui dump`"
		}
	}
];
/**
* dsh-fish **自己的**命令名（不来自 `fish.py`，因此不进 `cli-map.json`）。
*
* 为什么单独列：判据③要比"命令面注册项 vs cli-map"，把自有命令混进 cli-map 会让
* 覆盖率判据失去意义（它比的应该是"Python 命令树 == 映射表"）。所以自有命令显式登记在这里，
* 判据侧用 `NATIVE_COMMANDS ∪ cli-map 一级` 做全集比对。
*/
const NATIVE_COMMANDS = ["shadow", "hosts"];
/** `fish.py` 里一级命令名（35 个，来自枚举；本函数只做去重与排序）。 */
function cliRootPaths() {
	const roots = /* @__PURE__ */ new Set();
	for (const annotation of CLI_ANNOTATIONS) {
		const [root] = annotation.path.split(" ");
		if (root !== void 0 && root !== "") roots.add(root);
	}
	return [...roots].sort();
}
/** 全部叶子路径（76 条）。 */
function cliLeafPaths() {
	return CLI_ANNOTATIONS.map((annotation) => annotation.path).slice().sort();
}
/** 全部容器路径（`kb vault`、`kb backup` 这类只有子命令、没有自己动作的节点）。 */
function cliGroupPaths() {
	const groups = /* @__PURE__ */ new Set();
	for (const annotation of CLI_ANNOTATIONS) {
		const parts = annotation.path.split(" ");
		for (let i = 1; i < parts.length; i += 1) groups.add(parts.slice(0, i).join(" "));
	}
	return [...groups].sort();
}
/** 按路径取裁决（未登记返回 `null`）。 */
function findCliAnnotation(path) {
	return CLI_ANNOTATIONS.find((annotation) => annotation.path === path) ?? null;
}
/**
* 解析一条 CLI 调用（最长前缀匹配）。
*
* 为什么用"最长前缀"而不是"取前两个词"：命令树最深 3 级（`kb vault list`），
* 取固定长度会把 `kb vault` 当成叶子、把 `list` 当成位置参数——那条命令会**静默做错事**。
*/
function resolveCliRoute(tokens) {
	const leaves = new Set(cliLeafPaths());
	const groups = new Set(cliGroupPaths());
	for (let take = Math.min(tokens.length, 3); take >= 1; take -= 1) {
		const path = tokens.slice(0, take).join(" ");
		if (leaves.has(path)) return {
			path,
			annotation: findCliAnnotation(path),
			rest: tokens.slice(take)
		};
		if (groups.has(path)) return {
			path,
			annotation: null,
			rest: tokens.slice(take)
		};
	}
	return null;
}
/** 帮助文本（顶层：按一级命令分组列出；指定容器：列出子命令）。 */
function renderCliHelpText(path = null) {
	const header = "fish 命令等价面（`/fish <一级> [二级...] [--k=v...]`；二级起原样透传）";
	if (path !== null) {
		const children = CLI_ANNOTATIONS.filter((annotation) => annotation.path.startsWith(`${path} `));
		if (children.length === 0) {
			const annotation = findCliAnnotation(path);
			if (annotation === null) return `未知命令：${path}`;
			return [
				`fish ${annotation.path}`,
				`  类别：${annotation.kind}｜实现：${annotation.impl}`,
				`  理由：${annotation.reason}`,
				...annotation.evidence === void 0 ? [] : [`  只读依据：${annotation.evidence}`],
				`  替代：${formatReplacement(annotation.replacement)}`
			].join("\n");
		}
		return [`fish ${path} <子命令>`, ...children.map((annotation) => `  ${annotation.path}  [${annotation.impl}]  ${annotation.reason}`)].join("\n");
	}
	const roots = cliRootPaths();
	const lines = [header, `一级命令 ${roots.length} 个｜叶子 ${CLI_ANNOTATIONS.length} 条｜自有命令 ${NATIVE_COMMANDS.join(" / ")}`];
	for (const root of roots) {
		const own = findCliAnnotation(root);
		if (own !== null) {
			lines.push(`  ${root}  [${own.impl}/${own.kind}]`);
			continue;
		}
		const children = CLI_ANNOTATIONS.filter((annotation) => annotation.path.startsWith(`${root} `));
		lines.push(`  ${root}  <${children.length} 个子命令>`);
	}
	lines.push("  输入 `/fish <命令> --help` 看单条；输入 `/fish shadow status` 看影子期状态");
	return lines.join("\n");
}
/** 替代命令的人读渲染（没有替代时给一句明确的"没有替代"）。 */
function formatReplacement(replacement) {
	if (replacement === null || replacement === void 0) return "（无：影子期不执行，也没有等价入口）";
	const parts = [`fish ${replacement.cli}`];
	if (replacement.mcpTool !== void 0) parts.push(`MCP 工具 \`mcp__fish__${replacement.mcpTool}\``);
	const note = replacement.note === void 0 ? "" : `（${replacement.note}）`;
	return `${parts.join(" 或 ")}${note}`;
}
/**
* 影子期拒绝执行的文案（`unsupported-shadow`）。
*
* 为什么必须**明确说"没有写入"**：拒绝得含糊，读者会怀疑"到底写没写一半"；
* 而这条命令的整个意义就是"让它写不进去"。
*/
function renderCliRefusal(annotation, reason) {
	return [
		`影子期不执行写操作：fish ${annotation.path}`,
		`  原因：${reason}`,
		`  替代：${formatReplacement(annotation.replacement)}`,
		"  本次调用**没有**打开写连接、**没有**启动 Python、**没有**产生任何写入。",
		"  （影子期纪律：不注册模型工具 / 不注入上下文 / 不写 fish 库 / 不改宿主配置）"
	].join("\n");
}
/** 只给指引（`python-only`）的文案。 */
function renderCliGuidance(annotation, reason) {
	return [
		`影子期只给指引，不执行：fish ${annotation.path}`,
		`  原因：${reason}`,
		`  替代：${formatReplacement(annotation.replacement)}`,
		"  本次调用**没有**启动 Python、**没有**产生任何写入。"
	].join("\n");
}
/**
* 校验一条裁决的内部自洽（与命令树无关的那部分）。
*
* 为什么单独导出：`buildCliMap` 用的是**固定的**裁决表，等于"错误分支只有改源码才能触发"；
* 导出之后单元测试能拿合成裁决逐条喂进去（写命令标 ts / proxy 标写命令 / 缺替代 /
* 替代指向不存在的工具），这些分支才真的被覆盖过。
* @throws FishError `SPEC_INVALID`
*/
function validateCliAnnotation(annotation, knownTools) {
	const where = `cli-map 标注 ${annotation.path}`;
	if (!CLI_KINDS.includes(annotation.kind)) throw new FishError("SPEC_INVALID", `${where}：kind=${String(annotation.kind)} 不在取值域 ${CLI_KINDS.join("/")}`);
	if (!CLI_IMPLS.includes(annotation.impl)) throw new FishError("SPEC_INVALID", `${where}：impl=${String(annotation.impl)} 不在取值域 ${CLI_IMPLS.join("/")}`);
	if (annotation.reason.trim().length < 6) throw new FishError("SPEC_INVALID", `${where}：reason 太短（"未实现"三个字不是理由）`);
	if (annotation.impl === "ts" && annotation.kind !== "read") throw new FishError("SPEC_INVALID", `${where}：kind=${annotation.kind} 的命令不许标成 ts（影子期 TS 只读）`);
	if (annotation.impl === "proxy-readonly") {
		if (annotation.kind !== "read") throw new FishError("SPEC_INVALID", `${where}：proxy-readonly 只允许 kind=read，收到 ${annotation.kind}`);
		if ((annotation.evidence ?? "").trim().length < 6) throw new FishError("SPEC_INVALID", `${where}：proxy-readonly 必须写清只读依据（源码位置）`);
	}
	if (annotation.impl !== "ts") {
		if (annotation.replacement === void 0) throw new FishError("SPEC_INVALID", `${where}：impl=${annotation.impl} 必须给替代命令（未实现项不许是终点）`);
		const mcpTool = annotation.replacement.mcpTool;
		if (mcpTool !== void 0 && !knownTools.includes(mcpTool)) throw new FishError("SPEC_INVALID", `${where}：替代里写的 MCP 工具 ${mcpTool} 不在生产工具表里（登记了一条不存在的替代）`);
	} else if (annotation.replacement !== void 0) throw new FishError("SPEC_INVALID", `${where}：ts 实现不该有替代命令（它自己就是实现）`);
}
/**
* 把**机器枚举**的命令树与**人工裁决**表交叉校验，产出 `spec/cli-map.json` 的内容。
*
* 失败即抛 `SPEC_INVALID`（生成器在第 0 步调用它：**标注不合规就一个文件都不写**）。
* @param enumeration `scripts/probe/dump-cli.py` 的输出
* @param context 生产工具名清单（用于校验 `mcpTool` 替代确实存在）
*/
function buildCliMap(enumeration, context) {
	const leaves = enumeration.commands.filter((node) => node.kind === "leaf");
	const groups = enumeration.commands.filter((node) => node.kind === "group");
	const enumeratedPaths = leaves.map((node) => node.path);
	const annotatedPaths = CLI_ANNOTATIONS.map((annotation) => annotation.path);
	const duplicated = annotatedPaths.filter((path, index) => annotatedPaths.indexOf(path) !== index);
	if (duplicated.length > 0) throw new FishError("SPEC_INVALID", `cli-map 标注里有重复路径：${[...new Set(duplicated)].join(", ")}`);
	const missing = enumeratedPaths.filter((path) => !annotatedPaths.includes(path));
	if (missing.length > 0) throw new FishError("SPEC_INVALID", `命令树里有 ${missing.length} 条命令没有裁决：${missing.join(", ")}`, {
		hint: "在 src/domain/cli.ts 的 CLI_ANNOTATIONS 里补一条（未实现项必须显式登记，不许静默省略）",
		details: { missing }
	});
	const stale = annotatedPaths.filter((path) => !enumeratedPaths.includes(path));
	if (stale.length > 0) throw new FishError("SPEC_INVALID", `裁决表里有 ${stale.length} 条命令在命令树里不存在：${stale.join(", ")}`, {
		hint: "生产树删/改了命令名 → 同步删掉这条标注（否则登记表会继续声明一个不存在的契约）",
		details: { stale }
	});
	for (const annotation of CLI_ANNOTATIONS) validateCliAnnotation(annotation, context.knownTools);
	const entries = leaves.map((node) => {
		const annotation = findCliAnnotation(node.path);
		if (annotation === null) throw new FishError("SPEC_INVALID", `命令 ${node.path} 没有裁决（枚举与裁决表不同步）`);
		if (annotation.kind === "read" && node.hasApplyFlag) throw new FishError("SPEC_INVALID", `命令 ${node.path} 带 --apply 开关却被标成 kind=read（--apply 是写操作的强信号）`);
		return {
			path: node.path,
			level: node.depth,
			kind: annotation.kind,
			impl: annotation.impl,
			reason: annotation.reason,
			evidence: annotation.evidence ?? null,
			replacement: annotation.replacement ?? null,
			help: node.help,
			applyFlag: node.hasApplyFlag
		};
	}).sort((a, b) => a.path < b.path ? -1 : a.path > b.path ? 1 : 0);
	const groupEntries = groups.map((node) => ({
		path: node.path,
		level: node.depth,
		children: [...node.children].sort(),
		help: node.help
	})).sort((a, b) => a.path < b.path ? -1 : a.path > b.path ? 1 : 0);
	const implCounts = Object.fromEntries(CLI_IMPLS.map((impl) => [impl, entries.filter((entry) => entry.impl === impl).length]));
	const kindCounts = Object.fromEntries(CLI_KINDS.map((kind) => [kind, entries.filter((entry) => entry.kind === kind).length]));
	return {
		specVersion: "v0",
		source: {
			files: ["p0/fish.py"],
			method: "反射 argparse 对象树（捕获根 parser 后递归 _SubParsersAction.choices），不解析 --help 文本",
			fishPy: enumeration.fishPy,
			fishPySha256_12: enumeration.fishPySha256_12,
			python: enumeration.python,
			rootCommands: [...enumeration.rootCommands].sort()
		},
		counts: {
			level1: new Set(entries.map((entry) => entry.path.split(" ")[0])).size,
			leaves: entries.length,
			groups: groupEntries.length,
			maxDepth: entries.reduce((max, entry) => Math.max(max, entry.level), 0),
			level1AsCommands: enumeration.rootCommands.length
		},
		implCounts,
		kindCounts,
		entries,
		groups: groupEntries
	};
}
//#endregion
//#region src/ports/spec.ts
/**
* spec 加载器：**契约层是唯一真源，代码只是它的读者**。
*
* 为什么加载要带校验而不是 `JSON.parse` 了事：spec 是人工也会碰的文件，缺字段的
* spec 会让下游拿到 `undefined` 并一路走成"空结果"。所以这里对每个文件做结构校验，
* 失败即抛 `SpecError`（带"跑生成脚本"的下一步）。
*
* @module dsh-fish/ports/spec
*/
/**
* `writeClass` 的**取值域**（唯一真源）。
*
* 为什么留一个运行时数组、而不是只写一个联合类型：生成器要给每条标注做校验、
* `scripts/check-spec.mjs` 要给产物做校验——两处都读这一份。各写一份必然漂开，
* 而漂开的表现是"spec 上写着 `readwrite`，下游却查不到这个类别"。
*
* 语义：`none` 只读 / `audit` 只追加审计日志 / `state` 改状态 / `append` 只追加不覆盖 /
* `transaction` 多写必须在一个事务里。影子期 TS 只允许 `none` 与 `audit`（且必须走 Python）。
*/
const WRITE_CLASSES = [
	"none",
	"audit",
	"state",
	"append",
	"transaction"
];
/** `idempotent` 的取值域（同上：生成器与验收判据共用一份）。 */
const IDEMPOTENT_VALUES = [
	"yes",
	"no",
	"mode-dependent"
];
/** 必需字段校验的小工具：失败抛 `SpecError` 并指出哪个文件哪个字段。 */
function requireField(condition, file, field) {
	if (!condition) throw new SpecError("SPEC_INVALID", `${file} 缺少或非法字段：${field}`, { hint: "跑 `node scripts/gen-spec.mjs` 重新生成 spec/（不要手工补字段）" });
}
/** 校验 events.json。 */
function validateEvents(raw, file) {
	requireField(typeof raw.specVersion === "string", file, "specVersion");
	requireField(Array.isArray(raw.types), file, "types[]");
	requireField(typeof raw.declaredTypeCount === "number", file, "declaredTypeCount");
	if (raw.types.length !== raw.declaredTypeCount) throw new SpecError("SPEC_INVALID", `${file}: declaredTypeCount=${raw.declaredTypeCount} 与实际 ${raw.types.length} 条不符`, { hint: "重新运行生成脚本；手改条数是最容易被漏掉的半更新" });
	const seen = /* @__PURE__ */ new Set();
	for (const type of raw.types) {
		requireField(typeof type.type === "string" && type.type !== "", file, "types[].type");
		requireField(Array.isArray(type.requiredPayload), file, `types[${type.type}].requiredPayload`);
		requireField(Array.isArray(type.payloadKeys), file, `types[${type.type}].payloadKeys`);
		if (seen.has(type.type)) throw new SpecError("SPEC_INVALID", `${file}: 事件类型重复 ${type.type}`);
		seen.add(type.type);
	}
	return raw;
}
/** 校验 tools.json。 */
function validateTools(raw, file) {
	requireField(typeof raw.specVersion === "string", file, "specVersion");
	requireField(Array.isArray(raw.tools), file, "tools[]");
	if (raw.tools.length !== raw.declaredToolCount) throw new SpecError("SPEC_INVALID", `${file}: declaredToolCount=${raw.declaredToolCount} 与实际 ${raw.tools.length} 条不符`);
	const seen = /* @__PURE__ */ new Set();
	for (const tool of raw.tools) {
		requireField(typeof tool.name === "string" && tool.name !== "", file, "tools[].name");
		requireField(typeof tool.description === "string", file, `tools[${tool.name}].description`);
		requireField(tool.impl === "python", file, `tools[${tool.name}].impl（影子期必须是 python）`);
		if (seen.has(tool.name)) throw new SpecError("SPEC_INVALID", `${file}: 工具名重复 ${tool.name}`);
		seen.add(tool.name);
	}
	return raw;
}
/** 校验 injection.json：L0 必须存在且不可关，段名必须唯一。 */
function validateInjection(raw, file) {
	requireField(typeof raw.specVersion === "string", file, "specVersion");
	requireField(Array.isArray(raw.sections), file, "sections[]");
	const l0 = raw.sections.filter((section) => section.kind === "l0");
	if (l0.length !== 1) throw new SpecError("SPEC_INVALID", `${file}: 必须恰好有一个 l0 段（实得 ${l0.length}）`, { hint: "L0 身份段是注入包的立身之本，不可关闭、不可重复" });
	const names = /* @__PURE__ */ new Set();
	for (const section of raw.sections) {
		requireField(typeof section.name === "string" && section.name !== "", file, "sections[].name");
		requireField(Number.isInteger(section.order), file, `sections[${section.name}].order`);
		if (names.has(section.name)) throw new SpecError("SPEC_INVALID", `${file}: 段名重复 ${section.name}`);
		names.add(section.name);
	}
	return raw;
}
/** 校验 cut.json：规则必须与 Python 现状同档（window=2/step=1/latin-then-cjk）。 */
function validateCut(raw, file) {
	requireField(typeof raw.specVersion === "string", file, "specVersion");
	requireField(Array.isArray(raw.vectors), file, "vectors[]");
	if (raw.vectors.length < 20) throw new SpecError("SPEC_INVALID", `${file}: 向量少于 20 组（实得 ${raw.vectors.length}）`, { hint: "向量是切词对拍的唯一判据，少于 20 组等于没有覆盖" });
	const rules = raw.rules;
	requireField(typeof rules?.latin === "string", file, "rules.latin");
	requireField(typeof rules?.cjk === "string", file, "rules.cjk");
	if (rules.cjkRun?.mode !== "overlapping-bigram" || rules.cjkRun.window !== 2 || rules.cjkRun.step !== 1) throw new SpecError("SPEC_INVALID", `${file}: cjkRun 必须是 overlapping-bigram / window=2 / step=1（Python 现状），收到 ${JSON.stringify(rules.cjkRun)}`, { hint: "改这一项等于改检索行为；要改先改 Python 侧并重跑切词对拍" });
	if (rules.tokenOrder !== "latin-then-cjk") throw new SpecError("SPEC_INVALID", `${file}: tokenOrder 必须是 latin-then-cjk（Python 的两趟扫描顺序）`);
	return raw;
}
/**
* 校验 `cli-map.json`（T04）：**覆盖率与取值域都是机器可查的**。
*
* 为什么这里再校验一遍（生成器已经校验过）：spec 是人工也会碰的文件，
* 少一条命令的 spec 不会报错、只会让"命令等价"悄悄漏掉一条——那正是 A3 判据要防的事。
* 所以加载时把"条数自洽 + 取值域 + 替代解析"三条重新验一次，失败即抛。
*/
function validateCliMap(raw, file) {
	requireField(typeof raw.specVersion === "string", file, "specVersion");
	requireField(Array.isArray(raw.entries), file, "entries[]");
	requireField(Array.isArray(raw.groups), file, "groups[]");
	requireField(typeof raw.counts === "object" && raw.counts !== null, file, "counts");
	const declared = raw.counts.leaves;
	requireField(Number.isInteger(declared), file, "counts.leaves");
	if (raw.entries.length !== declared) throw new SpecError("SPEC_INVALID", `${file}: counts.leaves=${declared} 与实际 ${raw.entries.length} 条不符`, { hint: "跑 `node scripts/gen-spec.mjs` 重新生成（手改条数是最容易被漏掉的半更新）" });
	const paths = /* @__PURE__ */ new Set();
	for (const entry of raw.entries) {
		requireField(typeof entry.path === "string" && entry.path !== "", file, "entries[].path");
		requireField(Number.isInteger(entry.level), file, `entries[${entry.path}].level`);
		requireField(typeof entry.reason === "string" && entry.reason.trim() !== "", file, `entries[${entry.path}].reason`);
		if (!CLI_KIND_VALUES.includes(entry.kind)) throw new SpecError("SPEC_INVALID", `${file}: ${entry.path} 的 kind=${String(entry.kind)} 不在取值域 ${CLI_KIND_VALUES.join("/")}`);
		if (!CLI_IMPL_VALUES.includes(entry.impl)) throw new SpecError("SPEC_INVALID", `${file}: ${entry.path} 的 impl=${String(entry.impl)} 不在取值域 ${CLI_IMPL_VALUES.join("/")}`);
		if (entry.impl !== "ts" && entry.replacement === null) throw new SpecError("SPEC_INVALID", `${file}: ${entry.path} 是 ${entry.impl} 但没有替代命令（未实现项不许是终点）`);
		if (entry.impl === "proxy-readonly" && (entry.kind !== "read" || entry.evidence === null)) throw new SpecError("SPEC_INVALID", `${file}: ${entry.path} 标成 proxy-readonly，但 kind=${entry.kind} / evidence=${entry.evidence === null ? "缺" : "有"}`, { hint: "proxy 只允许只读命令，且必须写清只读依据（源码位置）" });
		if (paths.has(entry.path)) throw new SpecError("SPEC_INVALID", `${file}: 命令路径重复 ${entry.path}`);
		paths.add(entry.path);
	}
	return raw;
}
/**
* CLI 裁决的两个取值域（index.ts 与判据脚本共用）。
*
* 为什么从 `domain/cli.ts` 再导出一次而不是各写一份：spec 校验与命令面必须认同一份域，
* 否则"spec 上写着 proxy-readonly、命令面不认识这个档"会在运行时才炸。
*/
const CLI_KIND_VALUES = CLI_KINDS;
const CLI_IMPL_VALUES = CLI_IMPLS;
/** spec 内所有可摘要文件（与生成脚本写进 contract.json 的清单一致）。 */
const SPEC_FILES = [
	"contract.json",
	"schema.sql",
	"schema.fingerprint.json",
	"events.json",
	"tools.json",
	"injection.json",
	"layout/cut.json",
	"cli-map.json",
	"corpus.schema.sql"
];
/**
* 加载并校验整份 spec。
* @throws SpecError 文件缺失 / JSON 非法 / 结构不合法
*/
function loadSpec(specDir) {
	const dir = path.resolve(specDir);
	const contract = readSpecJson(path.join(dir, "contract.json"));
	const schemaFingerprint = readSpecJson(path.join(dir, "schema.fingerprint.json"));
	const events = validateEvents(readSpecJson(path.join(dir, "events.json")), "events.json");
	const tools = validateTools(readSpecJson(path.join(dir, "tools.json")), "tools.json");
	const injection = validateInjection(readSpecJson(path.join(dir, "injection.json")), "injection.json");
	const cut = validateCut(readSpecJson(path.join(dir, "layout", "cut.json")), "layout/cut.json");
	const cliMap = validateCliMap(readSpecJson(path.join(dir, "cli-map.json")), "cli-map.json");
	const fileDigests = {};
	for (const relative of SPEC_FILES) try {
		fileDigests[relative] = fileSha256(path.join(dir, relative));
	} catch {
		continue;
	}
	return {
		specDir: dir,
		contract,
		schemaFingerprint,
		events,
		tools,
		injection,
		cut,
		cliMap,
		fileDigests
	};
}
//#endregion
//#region src/core/time.ts
/**
* 时间原语：**库里用的 UTC 与输出层用的本地时区，是两个口径**。
*
* 从 `fish_common` 抄过来的三条规则（不是自己发明的）：
*   · 库里的时间戳一律 `2026-10-27T04:06:43Z`（`nowUtc` / `utcStamp`）——事实层；
*   · 给人看的 as-of 一律本地时区带偏移（`isoLocal`）——输出层；
*   · 无时区的入参按**本地时区**补（不是 UTC）：`--as-of 2026-10-26T00:00:00` 是人手写
*     的最常见形态，把它当 UTC 会让"今天"整体偏 8 小时。
*
* 为什么单独成模块：知识库 / 项目状态表 / 语料层三处都要这两条格式化，各写一份的
* 结局是"同一份回执里两个时间互相打脸"（T05 起三处共用这一份）。
*
* @module dsh-fish/core/time
*/
/** 两位补零。 */
function pad(value, width = 2) {
	return String(value).padStart(width, "0");
}
/** 库内时间戳（UTC + `Z`，与 Python `now_utc()` 同形）。 */
function utcStamp(date = /* @__PURE__ */ new Date()) {
	return `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())}T${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}:${pad(date.getUTCSeconds())}Z`;
}
/**
* 本地时区的 ISO8601（`2026-10-26T02:05:11+08:00`），与 Python `iso_local()` 同形。
*
* 为什么不用 `Date.toISOString()`：那个是 UTC + 毫秒。as-of 是**输出层**的东西，
* 读者看到的必须是自己的时区；而毫秒在两侧一定会差，逐字节对拍永远过不去。
*/
function isoLocal(date = /* @__PURE__ */ new Date()) {
	const offsetMinutes = -date.getTimezoneOffset();
	const sign = offsetMinutes >= 0 ? "+" : "-";
	const absolute = Math.abs(offsetMinutes);
	return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}${sign}${pad(Math.trunc(absolute / 60))}:${pad(absolute % 60)}`;
}
/**
* 解析一个时间入参：接受 `…Z` / `…+08:00` / 无时区（按本地时区补）。
* @returns 解析成功返回 `Date`；失败返回 `null`（调用方决定怎么报错，不在这里猜）
*/
function parseStamp(raw) {
	const text = (raw ?? "").trim();
	if (text === "") return null;
	const normalized = text.endsWith("Z") ? `${text.slice(0, -1)}+00:00` : text;
	const parsed = new Date(normalized);
	return Number.isNaN(parsed.getTime()) ? null : parsed;
}
/**
* 从时间串里取出**它自己声明的**时区偏移（分钟，东为正）；无偏移 → `null`。
*
* 为什么要它：`--as-of 2026-10-26T00:00:00+08:00` 冻结的是"北京时间的零点"，
* 而 `Date` 只保留瞬时值、丢掉偏移。项目状态表的"最近活跃"与"看板周期键"都要在
* **as-of 自己的时区**里算（Python 的 `astimezone(tzinfo)` 就是这个口径），
* 丢掉偏移会让同一份表里两处时间互相打脸（差 8 小时正好跨天）。
*/
function offsetMinutesOf(raw) {
	const text = (raw ?? "").trim();
	if (text === "") return null;
	if (text.endsWith("Z") || text.endsWith("z")) return 0;
	const match = /([+-])(\d{2}):?(\d{2})$/u.exec(text);
	if (match === null) return null;
	return (match[1] === "-" ? -1 : 1) * (Number(match[2]) * 60 + Number(match[3]));
}
/** 按**给定偏移**渲染 ISO8601（`2026-10-26T00:00:00+08:00`）。 */
function isoWithOffset(date, offsetMinutes) {
	const shifted = new Date(date.getTime() + offsetMinutes * 6e4);
	const sign = offsetMinutes >= 0 ? "+" : "-";
	const absolute = Math.abs(offsetMinutes);
	return `${shifted.getUTCFullYear()}-${pad(shifted.getUTCMonth() + 1)}-${pad(shifted.getUTCDate())}T${pad(shifted.getUTCHours())}:${pad(shifted.getUTCMinutes())}:${pad(shifted.getUTCSeconds())}${sign}${pad(Math.trunc(absolute / 60))}:${pad(absolute % 60)}`;
}
/** 按**给定偏移**取日期部分（`YYYY-MM-DD`）与周期键（`YYMMDD`）。 */
function datePartsInOffset(date, offsetMinutes) {
	const shifted = new Date(date.getTime() + offsetMinutes * 6e4);
	const year = shifted.getUTCFullYear();
	const month = shifted.getUTCMonth() + 1;
	const day = shifted.getUTCDate();
	return {
		year,
		month,
		day,
		ymd: `${year}-${pad(month)}-${pad(day)}`,
		period: `${pad(year % 100)}${pad(month)}${pad(day)}`
	};
}
//#endregion
//#region src/core/pytext.ts
/**
* Python 文本口径的两个小原语（`str()` / `repr()`）——**逐字节对拍**要的就是这个。
*
* 为什么需要它们：Python 的 f-string 把 `None` 印成字面 `None`、`True`/`False` 首字母大写；
* 而 `%r`（`repr`）把字符串印成**单引号**形态。JS 的 `String()` / `JSON.stringify()` 三处
* 全不一样。对拍时这些差异会以"看起来像实现不同"的红灯出现，追下去却是格式化口径——
* 所以口径本身收在这里，一处改、三族共用。
*
* @module dsh-fish/core/pytext
*/
/** Python `str(value)` 的渲染口径（`None` → `None`，布尔 → `True`/`False`）。 */
function pyText(value) {
	if (value === null || value === void 0) return "None";
	if (typeof value === "boolean") return value ? "True" : "False";
	return String(value);
}
/**
* Python `repr(str)` 的渲染口径（单引号、转义 `\\` / `'` / 换行 / 制表）。
*
* 与 CPython 一致的两条小规则：字符串里有 `'` 且没有 `"` 时改用双引号；其余情况用单引号。
* 不可打印字符只处理最常见的三个（`\n` `\r` `\t`）——回执里出现的也只会是它们。
*/
function pyRepr(value) {
	const text = String(value);
	const quote = text.includes("'") && !text.includes("\"") ? "\"" : "'";
	let out = "";
	for (const ch of text) if (ch === "\\") out += "\\\\";
	else if (ch === quote) out += `\\${quote}`;
	else if (ch === "\n") out += "\\n";
	else if (ch === "\r") out += "\\r";
	else if (ch === "	") out += "\\t";
	else out += ch;
	return `${quote}${out}${quote}`;
}
//#endregion
//#region src/layout/cut.ts
/**
* 中文切词（能力块 #12 布局/检索的公共纯函数）。
*
* 为什么这一小块必须逐 token 等价：SQLite 的 `unicode61` 不做中文分词，fish 自己把
* 文本切成**重叠 bigram** 再入 FTS（`p0/fish.py:3857`，ADR-005）。TS 侧若切得差一位，
* 检索不会报错，只会**静默漏召回**——那是影子期最贵的一类坏。
*
* 真源是 `spec/layout/cut.json`（规则 + 由 Python 冻结的向量）；本文件只实现规则，
* 不自己发明规则。向量对拍在 `scripts/check-spec.mjs` 与 `test/conformance/` 里跑。
*
* @module dsh-fish/layout/cut
*/
/** 与 Python 完全一致的默认规则（spec 生成时写入同一份值）。 */
const DEFAULT_CUT_RULES = {
	latin: "[A-Za-z0-9][A-Za-z0-9\\-_.]*",
	cjk: "[\\u4e00-\\u9fff]+",
	lowercase: true,
	tokenOrder: "latin-then-cjk",
	cjkRun: {
		mode: "overlapping-bigram",
		window: 2,
		step: 1,
		singleCharRun: "as-is"
	}
};
/** 与 Python 完全一致的默认 MATCH 规则。 */
const DEFAULT_MATCH_RULES = {
	quoteEveryToken: true,
	joiner: " AND ",
	emptyResult: null
};
/** 校验规则自洽（错规则必须响亮失败，而不是"切出另一种东西"）。 */
function validateRules(rules) {
	if (rules.cjkRun.mode === "overlapping-bigram") {
		if (!Number.isInteger(rules.cjkRun.window) || rules.cjkRun.window < 2) throw new SpecError("SPEC_INVALID", `cjkRun.window 必须是 >=2 的整数，收到 ${rules.cjkRun.window}`, { hint: "修正 spec/layout/cut.json 的 cjkRun.window（Python 现状为 2）" });
		if (!Number.isInteger(rules.cjkRun.step) || rules.cjkRun.step < 1) throw new SpecError("SPEC_INVALID", `cjkRun.step 必须是 >=1 的整数，收到 ${rules.cjkRun.step}`, { hint: "修正 spec/layout/cut.json 的 cjkRun.step（Python 现状为 1）" });
	}
	for (const [key, pattern] of [["latin", rules.latin], ["cjk", rules.cjk]]) try {
		new RegExp(pattern, "g");
	} catch (error) {
		throw new SpecError("SPEC_INVALID", `切词规则 ${key} 不是合法正则：${pattern}`, {
			hint: "修正 spec/layout/cut.json 的对应字段",
			cause: error
		});
	}
}
/** 编译切词规则。 */
function compileCut(rules = DEFAULT_CUT_RULES, match = DEFAULT_MATCH_RULES) {
	validateRules(rules);
	const latinRe = new RegExp(rules.latin, "g");
	const cjkRe = new RegExp(rules.cjk, "g");
	const { window, step, mode, singleCharRun } = rules.cjkRun;
	const cutCjkRun = (run) => {
		if (run.length === 1) return singleCharRun === "as-is" ? [run] : [];
		if (mode === "whole-run") return [run];
		const out = [];
		for (let i = 0; i + window <= run.length; i += step) out.push(run.slice(i, i + window));
		return out;
	};
	const cut = (text) => {
		const source = typeof text === "string" ? text : "";
		const latinTokens = [];
		for (const hit of source.matchAll(latinRe)) {
			const raw = hit[0];
			latinTokens.push({
				at: hit.index ?? 0,
				token: rules.lowercase ? raw.toLowerCase() : raw
			});
		}
		const cjkTokens = [];
		for (const hit of source.matchAll(cjkRe)) {
			const start = hit.index ?? 0;
			for (const token of cutCjkRun(hit[0])) cjkTokens.push({
				at: start,
				token
			});
		}
		if (rules.tokenOrder === "latin-then-cjk") return [...latinTokens.map((t) => t.token), ...cjkTokens.map((t) => t.token)];
		return [...latinTokens, ...cjkTokens].map((entry, order) => ({
			...entry,
			order
		})).sort((a, b) => a.at === b.at ? a.order - b.order : a.at - b.at).map((entry) => entry.token);
	};
	const matchExpression = (query) => {
		const tokens = cut(query).filter((token) => token !== "");
		if (tokens.length === 0) return match.emptyResult;
		return tokens.map((token) => match.quoteEveryToken ? `"${token.replace(/"/g, "\"\"")}"` : token).join(match.joiner);
	};
	return {
		rules,
		cut,
		matchExpression
	};
}
/** 便捷入口：用默认规则切一次（内部仍走 `compileCut`，保证只有一条实现）。 */
function cutTokens(text, rules = DEFAULT_CUT_RULES) {
	return compileCut(rules).cut(text);
}
/** 便捷入口：文本 → 入 FTS 的预处理串（空格连接）。 */
function cutText(text, rules = DEFAULT_CUT_RULES) {
	return cutTokens(text, rules).join(" ");
}
/** 便捷入口：查询串 → FTS5 MATCH 表达式；无 token 时 `null`（与 Python 的 `None` 对齐）。 */
function ftsMatchExpression(query, rules = DEFAULT_CUT_RULES, match = DEFAULT_MATCH_RULES) {
	return compileCut(rules, match).matchExpression(query);
}
/**
* 用 spec 内的向量校验切词实现。
* @returns 失败清单（空数组 = 全部通过）
*/
function verifyCutVectors(spec, rules = spec.rules) {
	const compiled = compileCut(rules, spec.match);
	const failures = [];
	for (const vector of spec.vectors) {
		const actual = compiled.cut(vector.input);
		const length = Math.min(actual.length, vector.tokens.length);
		let firstDiffAt = length;
		for (let i = 0; i < length; i += 1) if (actual[i] !== vector.tokens[i]) {
			firstDiffAt = i;
			break;
		}
		if (actual.length !== vector.tokens.length || firstDiffAt !== length) failures.push({
			id: vector.id,
			input: vector.input,
			expected: vector.tokens,
			actual,
			firstDiffAt
		});
	}
	return failures;
}
//#endregion
//#region src/domain/token.ts
/**
* token 计数与按字符截断（注入包 / 看板 / 候选三段共用的两个原语）。
*
* 为什么单独成模块：`countTokens` 是**契约**的一部分（`spec/injection.json` 的
* `budgetRule`），而它被四个段与包尾预留同时使用。放在 `inject.ts` 里会让
* `board.ts` / `push.ts` 反向依赖注入包（循环依赖），而循环依赖在 ESM 下的表现是
* "某个常量在模块求值期还是 undefined"——最难查的那种。
*
* ## ⚠ 计的是**码点**，不是 UTF-16 码元（2026-10-27 · T03 实测抓到的真 bug）
*
* Python 的 `len(str)` 数的是**码点**；JS 的 `str.length` 数的是 UTF-16 码元。
* 一个 astral 字符（如看板里的 `🔴` U+1F534）在 Python 里是 1，在 JS 里是 2。
* 快照对拍实测：同一条看板文本 Python `<!-- 930/1600 -->`、TS `<!-- 931/1600 -->`——
* 差的那 1 token 就是"一个 emoji 被数成两个"。这不只是计数偏差：预算裁剪的触发点
* 由它决定，差 1 就可能"该裁的段没裁"。
*
* 所以本模块按码点计数（`Array.from` / 手写遍历），截断也按码点切（否则会在代理对
* 中间切断，产出一个孤立代理项——两侧字节必然不等，且渲染出来是乱码）。
*
* @module dsh-fish/domain/token
*/
/** 字符串的**码点**长度（与 Python `len()` 同口径）。 */
function codePointLength(text) {
	let count = 0;
	for (const _ of text) count += 1;
	return count;
}
/** 按**码点**切片（与 Python 的 `text[start:end]` 同口径）。 */
function sliceCodePoints(text, start, end) {
	const from = Math.max(0, Math.trunc(start));
	const to = end === void 0 ? void 0 : Math.max(from, Math.trunc(end));
	let index = 0;
	let out = "";
	for (const ch of text) {
		if (index >= (to ?? Number.POSITIVE_INFINITY)) break;
		if (index >= from) out += ch;
		index += 1;
	}
	return out;
}
/**
* token 计数（影子期口径 = Python `count_tokens` 的 **fallback 分支**）：
* `int(len(text) * 1.2)`，`len` 是**码点**数。
*
* 为什么不是 tiktoken 精确值：TS 侧没有 cl100k_base。两条路的取舍写在
* `NOTES-T02-delivery.md`：**对拍时两侧统一走这一档**（Python 侧把 `_ENC` 钉成 False），
* 于是"预算裁剪在哪儿触发"也成了可对拍的事实。
*
* D3 裁决（2026-10-27）：这条口径写进了 `spec/injection.json` 的 `budgetRule`，
* 并由 `scripts/check-spec.mjs` 逐位比对实现——换心期若引入真实 tokenizer，
* **必须同时改 spec 与两侧实现**，否则视为契约破坏（判据会红）。
*/
function countTokens(text) {
	return Math.trunc(codePointLength(text) * 1.2);
}
/** `budgetRule.rule` 的合法取值（与 spec 里的字符串一一对应）。 */
const BUDGET_RULE_FALLBACK = "fallback";
/** 按**字符（码点）**截断（不是 token），并把换行压成空格（复刻 `_clip_chars`）。 */
function clipChars(text, limit) {
	const s = (text ?? "").trim().replace(/\r/g, " ").replace(/\n/g, " ");
	const length = codePointLength(s);
	if (limit <= 0 || length <= limit) return s;
	return `${sliceCodePoints(s, 0, Math.max(0, limit - 1))}…`;
}
//#endregion
//#region src/domain/corpus.ts
/**
* 语料层只读面（`corpus.sqlite`：search / read / stats）。
*
* ## 为什么 TS 侧可以直接读它（而不像 T04 那样标 `python-only`）
*
* T04 把 `corpus` 标成 `python-only` 的理由是"**快照副本里没有它**"——那是转发路径的
* 限制，不是读能力的限制。语料库是独立文件 `<data>/corpus.sqlite`（`DB_NAME`），
* 只读打开它就能算出同一份读数；读不出"假空"的根因（快照缺库）在这里不存在。
*
* ## 与生产的两处**有意差异**（都登记在 NOTES-T05 §5）
*
*   1. 生产 `search`/`read` 会把访问记进 `corpus_access_log`（`_log_access`，
*      `p0/fish_corpus.py:859`）。影子期**不写**——所以本模块没有 `logAccess`。
*   2. 生产 MCP 的 `corpus.read` 读完会回填 `push_log.reaction`（`mcp_server.py:2056`）。
*      那是**主库写**，影子期更不做；本模块只返回正文与来源状态。
*
* 代价：DSH 里的检索不出现在语料审计里；收益：读命令不可能改任何一个字节。
*
* ## 逐条复刻的生产语义（每条都能在源码里指到行）
*
*   · 门牌三档（`bad_pointer` / `not_found` / `ambiguous_pointer`）——**不许猜一个最像的**；
*   · 过滤先于排序：`MATCH` 与全部过滤在一条 SQL 里下推（`build_filter_sql`）；
*   · 排序 = 0.6×批内归一 bm25 + 0.2×命中词比 + 0.2×时间衰减（`DefaultRanker`）；
*   · **出口二次脱敏**：正文、标题、workspace、cwd 都在出口过一遍规则表；
*   · `query` 只以 sha256 形式出现（本模块不算它——不写审计就不需要它）。
*
* @module dsh-fish/domain/corpus
*/
/** 库文件名（`fish_corpus_store.DB_NAME`）。 */
const CORPUS_DB_NAME = "corpus.sqlite";
/** 检索缺省条数。 */
const SEARCH_DEFAULT_LIMIT = 8;
/** 检索条数硬上限（超出夹紧）。 */
const SEARCH_MAX_LIMIT = 32;
/** 送 ranker 的候选 = limit × 8 … */
const SEARCH_CANDIDATE_FACTOR = 8;
/** … 且不少于 64（太少会让排序退化成"谁先入库"）。 */
const SEARCH_CANDIDATE_MIN = 64;
/** 片段长度。 */
const SNIPPET_CHARS = 200;
/** `read` 的缺省字符上限。 */
const READ_DEFAULT_MAX_CHARS = 8e3;
/** 段级/会话级台账的作用域词表（`forget` 的四档）。 */
const CORPUS_SOURCE_LINE = "source=对话语料（corpus.sqlite 投影）";
/** 占位符（命中即替换并计数，**绝不整段删成空串**）。 */
const REDACT_PLACEHOLDER = "〔已脱敏:%s〕";
/**
* 脱敏规则（`fish_corpus_redact.RULES` 的同序复刻）。
*
* 顺序**有意义**：具体形态（`fsh_…`）先于泛化形态（`token=`），否则
* `token = fsh_xxx` 会先被泛化规则吃掉、具体类型就永远统计不到。
*/
const REDACT_RULES = [
	{
		name: "fish_token",
		pattern: /fsh_[A-Za-z0-9_-]{20,}/gu
	},
	{
		name: "openai_key",
		pattern: /sk-[A-Za-z0-9_-]{20,}/gu
	},
	{
		name: "github_token",
		pattern: /(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}/gu
	},
	{
		name: "aws_access_key",
		pattern: /(?:AKIA|ASIA)[0-9A-Z]{16}/gu
	},
	{
		name: "private_key",
		pattern: /BEGIN [A-Z ]*PRIVATE KEY/gu
	},
	{
		name: "bearer_token",
		pattern: /Bearer\s+\S{8,}/giu
	},
	{
		name: "token_env",
		pattern: /[a-z0-9_]*(?:token|secret|api[_-]?key)["']?\s*[=:：]\s*["']?\S+/giu
	},
	{
		name: "password",
		pattern: /(?:password|passwd|pwd|密码|口令)["']?\s*[=:：]\s*["']?\S+/giu
	}
];
/** 脱敏一段文本，返回 `[脱敏后文本, 命中数]`。 */
function redact(text) {
	let out = text === null || text === void 0 ? "" : String(text);
	let total = 0;
	for (const rule of REDACT_RULES) {
		rule.pattern.lastIndex = 0;
		out = out.replace(rule.pattern, () => {
			total += 1;
			return REDACT_PLACEHOLDER.replace("%s", rule.name);
		});
	}
	return [out, total];
}
/** **非正文**可回显列的出口二次脱敏（`workspace` / `cwd` / 标题族）。 */
function safeField(value) {
	if (value === null || value === void 0 || value === "") return "";
	return redact(String(value))[0];
}
/** 门牌解析失败（三档语义，**不许猜一个最像的**）。 */
var PointerError = class extends Error {
	code;
	candidates;
	constructor(code, message, candidates = []) {
		super(message);
		this.name = "PointerError";
		this.code = code;
		this.candidates = candidates;
	}
};
const POINTER_RE = /^(?<kind>log|conv):(?<host>[A-Za-z0-9_.-]+):(?<sid>[^#\s]+)#(?<loc>(?:seq|line)=\d+)(?:\.(?<part>\d+))?$/u;
/** 会话短址：剥掉可选 `session-` 前缀后取前 8 位。 */
function sid8(sessionId) {
	const text = String(sessionId ?? "");
	return text.startsWith("session-") ? text.slice(8, 16) : text.slice(0, 8);
}
/** 规范门牌：`log:<host>:<session_id>#<loc>[.<part>]`。 */
function canonicalPointer(host, sessionId, loc, part = 0) {
	const base = `log:${host}:${sessionId}#${loc}`;
	return part === 0 ? base : `${base}.${part}`;
}
/** 推送用缩写：`conv:<host>:<sid8>#<loc>[.<part>]`。 */
function pushPointer(host, sessionId, loc, part = 0) {
	const base = `conv:${host}:${sid8(sessionId)}#${loc}`;
	return part === 0 ? base : `${base}.${part}`;
}
/** 纯字符串解析（不查库）。 */
function parsePointer(pointer) {
	const text = String(pointer ?? "").trim();
	if (text === "") throw new PointerError("bad_pointer", "门牌是空串");
	const match = POINTER_RE.exec(text);
	if (match === null) throw new PointerError("bad_pointer", `门牌形态不对：${pyRepr(text)}（要 log:<host>:<session_id>#seq=123[.1] 或 conv:<host>:<sid8>#seq=123）`);
	const groups = match.groups ?? {};
	return {
		kind: String(groups.kind),
		host: String(groups.host),
		sid: String(groups.sid),
		loc: String(groups.loc),
		part: Number(groups.part ?? 0)
	};
}
/** 解析 + 补全：`conv:` 按 `sid8` 查回完整 `session_id`（多命中必须歧义报错）。 */
function resolvePointer(db, pointer) {
	const parsed = parsePointer(pointer);
	let sessionId;
	if (parsed.kind === "log") sessionId = parsed.sid;
	else {
		const ids = db.prepare("SELECT session_id FROM corpus_session WHERE host=? AND sid8=? ORDER BY session_id").all(parsed.host, parsed.sid).map((row) => String(row.session_id));
		if (ids.length === 0) throw new PointerError("not_found", `conv 缩写 ${pyRepr(pointer)} 找不到会话（host=${parsed.host}, sid8=${parsed.sid}）`);
		if (ids.length > 1) throw new PointerError("ambiguous_pointer", `conv 缩写 ${pyRepr(pointer)} 命中 ${ids.length} 个会话，请用完整 log: 门牌`, ids.slice(0, 5).map((sid) => canonicalPointer(parsed.host, sid, parsed.loc, parsed.part)));
		sessionId = ids[0];
	}
	return {
		kind: parsed.kind,
		host: parsed.host,
		sid: parsed.sid,
		sessionId,
		loc: parsed.loc,
		part: parsed.part,
		pointer: canonicalPointer(parsed.host, sessionId, parsed.loc, parsed.part),
		code: pushPointer(parsed.host, sessionId, parsed.loc, parsed.part)
	};
}
/** 语料库路径：`<root>/corpus.sqlite`（root 就是 `p0/data`）。 */
function corpusDbPath(dataDir) {
	return path.join(dataDir, CORPUS_DB_NAME);
}
/**
* 只读打开语料库；库不存在返回 `null`（**读不建库**，也不留空库文件）。
*
* 为什么 `query_only=1`：与主库同一条纪律——让 SQLite 自己拒绝写，
* 而不是"我们小心不写"。
*/
function openCorpusReadOnly(dataDir) {
	const target = corpusDbPath(dataDir);
	let db;
	try {
		db = new DatabaseSync(target, { readOnly: true });
	} catch {
		return null;
	}
	try {
		db.exec("PRAGMA busy_timeout=5000");
		db.exec("PRAGMA query_only=1");
	} catch {
		db.close();
		return null;
	}
	return db;
}
/** 批内 min-max 归一；退化解（全相等 / 只有一个）→ 全部 1.0。 */
function rankNorm(values) {
	if (values.length === 0) return [];
	const lo = Math.min(...values);
	const hi = Math.max(...values);
	if (hi === lo) return values.map(() => 1);
	return values.map((value) => (hi - value) / (hi - lo));
}
/** `2026-10-27T04:06:43Z` → 毫秒；解析失败返回 `null`（不猜、不抛）。 */
function parseUtc$1(stamp) {
	if (typeof stamp !== "string" || stamp === "") return null;
	if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/u.test(stamp)) return null;
	const parsed = new Date(stamp);
	return Number.isNaN(parsed.getTime()) ? null : parsed.getTime();
}
/** 段的年龄（天）；时间缺失/坏 → 0.0（当作"刚发生"）。 */
function ageDays(occurredAt, now) {
	const stamp = parseUtc$1(occurredAt);
	if (stamp === null) return 0;
	return Math.max(0, (now - stamp) / 864e5);
}
/** `why` 里的人类读数（"14 天前" / "今天"）。 */
function ageText(days) {
	if (days < 1) return "今天";
	if (days < 30) return `${Math.trunc(days)} 天前`;
	if (days < 365) return `${Math.max(1, Math.trunc(days / 30))} 个月前`;
	return `${(days / 365).toFixed(1)} 年前`;
}
/** 默认排序（BM25 + 命中词比 + 时间衰减；权重视为契约）。 */
function rankCandidates(cands, terms, context) {
	if (cands.length === 0) return [];
	const now = context.now ?? Date.now();
	const project = context.project ?? "";
	const norms = rankNorm(cands.map((cand) => cand.bm25));
	const scored = cands.map((cand, index) => {
		const doc = new Set(cutTokens(String(cand.text ?? "")));
		const matched = terms.filter((term) => doc.has(term)).length;
		const ratio = terms.length > 0 ? matched / terms.length : 0;
		const age = ageDays(cand.occurred_at, now);
		const recency = Math.exp(-age / 180);
		const score = .6 * norms[index] + .2 * ratio + .2 * recency;
		const why = [];
		if (terms.length > 0) why.push(`命中 ${matched}/${terms.length} 个词`);
		if (project !== "" && String(cand.project ?? "") === project) why.push("同项目");
		why.push(ageText(age));
		return {
			pointer: String(cand.pointer),
			score: round6(score),
			why
		};
	});
	scored.sort((left, right) => right.score !== left.score ? right.score - left.score : left.pointer < right.pointer ? -1 : left.pointer > right.pointer ? 1 : 0);
	return scored;
}
/** 六位小数（与 `round(score, 6)` 同用途）。 */
function round6(value) {
	return Number(value.toFixed(6));
}
/** 过滤键 → 列（全 AND；同一键多值 = OR；空列表 = 不限）。 */
const FILTER_COLUMNS = {
	host: "s.host",
	project: "ss.project",
	workspace: "ss.workspace",
	session: "s.session_id",
	citizen: "ss.citizen_id",
	role: "s.role",
	origin: "ss.origin"
};
const FTS_SELECT = "SELECT s.id AS seg_id, s.host, s.session_id, s.loc, s.part, s.role, s.occurred_at, s.text, s.chars, s.redacted_count, s.shadowed, ss.sid8, ss.title AS session_title, ss.project, ss.workspace, ss.citizen_id, ss.origin, bm25(corpus_fts) AS bm25 FROM corpus_fts JOIN corpus_segment s ON s.id=corpus_fts.seg_id LEFT JOIN corpus_session ss ON ss.host=s.host AND ss.session_id=s.session_id WHERE corpus_fts MATCH ?";
/** 过滤条件 → `[whereSql, args]`。未知键 / 本地时间形态**响亮失败**。 */
function buildFilterSql(filters, options = {}) {
	const where = [];
	const args = [];
	for (const [key, value] of Object.entries(filters ?? {})) {
		const column = FILTER_COLUMNS[key];
		if (column === void 0) throw new Error(`未知过滤键 ${JSON.stringify(key)}（只认 ${Object.keys(FILTER_COLUMNS).join("/")}）`);
		const values = value.map((item) => String(item));
		if (values.length === 0) continue;
		where.push(`${column} IN (${values.map(() => "?").join(",")})`);
		args.push(...values);
	}
	if (options.includeShadowed !== true) where.push("s.shadowed=0");
	for (const [name, value, operator] of [[
		"time_from",
		options.timeFrom ?? null,
		">="
	], [
		"time_to",
		options.timeTo ?? null,
		"<="
	]]) {
		if (value === null || value === "") continue;
		if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/u.test(value)) throw new Error(`${name} 必须是 UTC 'Z' 形式（如 2026-10-27T00:00:00Z），收到 ${JSON.stringify(value)}——本地时间会静默差 8 小时`);
		where.push(`s.occurred_at${operator}?`);
		args.push(value);
	}
	return [where.length > 0 ? where.join(" AND ") : "1=1", args];
}
/** 一次 SQL 把 MATCH + 全部过滤下推（过滤先于排序）。 */
function searchSegments(db, matchExpr, options) {
	const [where, args] = buildFilterSql(options.filters, { includeShadowed: options.includeShadowed });
	const sql = `${FTS_SELECT} AND ${where} ORDER BY bm25(corpus_fts) LIMIT ?`;
	return db.prepare(sql).all(matchExpr, ...args, Math.trunc(options.limit));
}
/** 同一次过滤下的**总命中数**。 */
function countMatching(db, matchExpr, options) {
	const [where, args] = buildFilterSql(options.filters, { includeShadowed: options.includeShadowed });
	const sql = "SELECT COUNT(*) AS n FROM corpus_fts JOIN corpus_segment s ON s.id=corpus_fts.seg_id LEFT JOIN corpus_session ss ON ss.host=s.host AND ss.session_id=s.session_id WHERE corpus_fts MATCH ? AND " + where;
	const row = db.prepare(sql).get(matchExpr, ...args);
	return Number(row?.n ?? 0);
}
/** 片段（**出口二次脱敏**：入库漏了、出口补上）。 */
function snippetOf(cand, chars = 200) {
	const [text, count] = redact(cand.text ?? "");
	return [codePointLength(text) > chars ? `${sliceCodePoints(text, 0, chars)}…` : text, Math.max(Number(cand.redacted_count ?? 0), count)];
}
/** 检索原话（设计 §6）：一次 SQL 把 `MATCH` 与全部过滤下推，排序交给 ranker。 */
function corpusSearch(dataDir, query, options = {}) {
	const rankerName = String(options.ranker ?? "default").trim().toLowerCase();
	if (rankerName !== "default") throw new Error(`未知 ranker：${JSON.stringify(options.ranker)}（已登记：default）`);
	const filters = { ...options.filters ?? {} };
	const limitValue = Math.max(1, Math.min(Math.trunc(options.limit ?? 8), 32));
	const now = options.now ?? Date.now();
	const degraded = [];
	const hits = [];
	let total = 0;
	let truncated = false;
	const db = openCorpusReadOnly(dataDir);
	try {
		if (db === null) degraded.push({
			reason: "db_missing",
			note: "语料库还没建：先跑 `fish corpus ingest`"
		});
		else {
			const terms = dedupe(cutTokens(String(query ?? "")));
			const expr = matchExpression(terms);
			if (expr !== null) {
				const cands = searchSegments(db, expr, {
					filters,
					limit: Math.max(limitValue * 8, 64)
				});
				total = countMatching(db, expr, { filters });
				for (const cand of cands) cand.pointer = canonicalPointer(cand.host, cand.session_id, cand.loc, cand.part);
				const ranked = rankCandidates(cands, terms, {
					project: filters.project?.[0] ?? "",
					now
				});
				const byPointer = new Map(cands.map((cand) => [String(cand.pointer), cand]));
				for (const item of ranked) {
					const cand = byPointer.get(item.pointer);
					if (cand === void 0) continue;
					const [snippet] = snippetOf(cand);
					hits.push({
						pointer: item.pointer,
						host: cand.host,
						session_id: cand.session_id,
						session_title: safeField(cand.session_title),
						role: cand.role,
						occurred_at: cand.occurred_at,
						project: String(cand.project ?? ""),
						workspace: safeField(cand.workspace),
						citizen_id: cand.citizen_id,
						origin: cand.origin,
						shadowed: Number(cand.shadowed ?? 0),
						snippet,
						chars: Number(cand.chars ?? 0),
						score: item.score,
						ranker: rankerName,
						why: item.why
					});
					if (hits.length >= limitValue) break;
				}
				truncated = total > cands.length || hits.length < ranked.length;
			}
		}
	} finally {
		db?.close();
	}
	return {
		hits,
		meta: {
			total_matched: total,
			returned: hits.length,
			ranker: rankerName,
			filters_echo: filters,
			degraded,
			as_of: utcStamp(/* @__PURE__ */ new Date()),
			truncated,
			limit: limitValue,
			budget_tokens: null
		}
	};
}
/** 去重（保序、丢空）。 */
function dedupe(items) {
	const seen = /* @__PURE__ */ new Set();
	const out = [];
	for (const item of items) {
		if (item === "" || seen.has(item)) continue;
		seen.add(item);
		out.push(item);
	}
	return out;
}
/** token 列表 → FTS5 MATCH 表达式（每个 token 加双引号，OR 连接）。 */
function matchExpression(terms) {
	const tokens = terms.map((term) => String(term).trim()).filter((term) => term !== "");
	if (tokens.length === 0) return null;
	return tokens.map((token) => `"${token.replace(/"/gu, "\"\"")}"`).join(" OR ");
}
/** 源文件现在还在不在（`source.missing`）。 */
function sourceState(db, host, sessionId) {
	const row = db.prepare("SELECT path, status FROM corpus_source WHERE host=? AND session_id=?").get(host, sessionId);
	if (row === void 0) return {
		path: null,
		missing: true,
		status: null,
		note: "没有采集游标：源文件路径未知（正文来自投影）"
	};
	const rawPath = String(row.path ?? "");
	let exists = false;
	if (rawPath !== "") exists = existsSync(rawPath);
	return {
		path: safeField(rawPath) === "" ? null : safeField(rawPath),
		missing: !exists,
		status: row.status === null || row.status === void 0 ? null : String(row.status),
		note: exists ? "" : "源文件已不在：正文来自投影（脱敏后的切片），不猜"
	};
}
/** 门牌 → 原文（源文件缺失时仍返回已存正文并声明）。 */
function corpusRead(dataDir, pointer, options = {}) {
	const db = openCorpusReadOnly(dataDir);
	if (db === null) throw new Error(`语料库不存在：先跑 \`fish corpus ingest\`（${corpusDbPath(dataDir)}）`);
	try {
		const resolved = resolvePointer(db, pointer);
		const seg = db.prepare("SELECT s.*, ss.title AS session_title, ss.project, ss.workspace, ss.citizen_id, ss.origin, ss.cwd FROM corpus_segment s LEFT JOIN corpus_session ss ON ss.host=s.host AND ss.session_id=s.session_id WHERE s.host=? AND s.session_id=? AND s.loc=? AND s.part=?").get(resolved.host, resolved.sessionId, resolved.loc, resolved.part);
		if (seg === void 0) throw new PointerError("not_found", `门牌 ${resolved.pointer} 在库里没有对应段（可能已 forget）`);
		const [text0, count] = redact(String(seg.text ?? ""));
		const limit = Math.max(1, Math.trunc(options.maxChars ?? 8e3));
		const truncated = codePointLength(text0) > limit;
		const text = truncated ? sliceCodePoints(text0, 0, limit) : text0;
		const source = sourceState(db, resolved.host, resolved.sessionId);
		return {
			pointer: resolved.pointer,
			code: resolved.code,
			host: resolved.host,
			session_id: resolved.sessionId,
			sid8: sid8(resolved.sessionId),
			loc: resolved.loc,
			part: resolved.part,
			role: String(seg.role ?? ""),
			occurred_at: String(seg.occurred_at ?? ""),
			session_title: safeField(seg.session_title),
			project: String(seg.project ?? ""),
			workspace: safeField(seg.workspace),
			citizen_id: seg.citizen_id === null || seg.citizen_id === void 0 ? null : String(seg.citizen_id),
			origin: seg.origin === null || seg.origin === void 0 ? null : String(seg.origin),
			shadowed: Number(seg.shadowed ?? 0),
			chars: Number(seg.chars ?? 0),
			redacted_count: Math.max(Number(seg.redacted_count ?? 0), count),
			text,
			truncated,
			truncated_in_store: Number(seg.chars ?? 0) > codePointLength(String(seg.text ?? "")),
			offset_space: String(seg.offset_space ?? ""),
			source,
			text_from: "projection",
			as_of: utcStamp(/* @__PURE__ */ new Date())
		};
	} finally {
		db.close();
	}
}
/** 读到整数型 meta 读数（缺失/脏值都算 0）。 */
function metaInt(db, key) {
	try {
		const row = db.prepare("SELECT v FROM corpus_meta WHERE k=?").get(key);
		const value = Number.parseInt(String(row?.v ?? "0"), 10);
		return Number.isNaN(value) ? 0 : value;
	} catch {
		return 0;
	}
}
/** `now - max(last_ingested_at)`：日报用它抓"假增量"。 */
function lagSeconds(db) {
	const stamp = db.prepare("SELECT MAX(last_ingested_at) AS v FROM corpus_source").get()?.v ?? null;
	if (stamp === null || stamp === "") return null;
	const parsed = parseUtc$1(stamp);
	if (parsed === null) return null;
	return Math.trunc(Math.max(0, (Date.now() - parsed) / 1e3));
}
/** 库体积分项（失败返回空对象：读体积失败不是业务失败）。 */
function dbBytes(db) {
	try {
		const pageSize = db.prepare("PRAGMA page_size").get();
		const pageCount = db.prepare("PRAGMA page_count").get();
		return {
			total: Number(pageSize?.page_size ?? 0) * Number(pageCount?.page_count ?? 0),
			segments: tableBytes(db, "corpus_segment"),
			fts: tableBytes(db, "corpus_fts")
		};
	} catch {
		return {};
	}
}
/** 一张表占的页字节（`dbstat` 不可用 → 0）。 */
function tableBytes(db, table) {
	try {
		const row = db.prepare("SELECT SUM(pgsize) AS n FROM dbstat WHERE name LIKE ?").get(`${table}%`);
		return Number(row?.n ?? 0);
	} catch {
		return 0;
	}
}
/** 台账 → 四档作用域（`scopes_from_tombstones` 的同形复刻）。 */
function tombstoneScopes(db) {
	const rows = db.prepare("SELECT * FROM corpus_tombstone ORDER BY created_at, scope_key").all();
	const scopes = {
		sessions: /* @__PURE__ */ new Set(),
		hosts: /* @__PURE__ */ new Set(),
		pointers: [],
		ranges: []
	};
	const isUtc = (value) => /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/u.test(value);
	for (const row of rows) {
		const scope = String(row.scope_type ?? "");
		const key = String(row.scope_key ?? "");
		if (scope === "session") {
			const index = key.indexOf(":");
			scopes.sessions.add(index >= 0 ? `${key.slice(0, index)}\u0000${key.slice(index + 1)}` : `${key}\u0000`);
		} else if (scope === "host") scopes.hosts.add(key);
		else if (scope === "pointer") {
			let parsed;
			try {
				parsed = parsePointer(key);
			} catch {
				continue;
			}
			if (parsed.kind !== "log") continue;
			scopes.pointers.push({
				host: parsed.host,
				sid: parsed.sid,
				loc: parsed.loc,
				part: parsed.part
			});
		} else if (scope === "range") {
			const colon = key.indexOf(":");
			const host = colon >= 0 ? key.slice(0, colon) : key;
			const window = colon >= 0 ? key.slice(colon + 1) : "";
			const dots = window.indexOf("..");
			const start = dots >= 0 ? window.slice(0, dots) : window;
			const end = dots >= 0 ? window.slice(dots + 2) : "";
			if (isUtc(start) && isUtc(end)) scopes.ranges.push({
				host: host === "" || host === "*" ? null : host,
				start,
				end
			});
		}
	}
	return scopes;
}
/** 这段对话是否被台账删过（`*:<sid>` 也算——跨宿主语义）。 */
function isSessionForgotten(scopes, host, sessionId) {
	if (scopes.hosts.has(host)) return true;
	return scopes.sessions.has(`${host}\u0000${sessionId}`) || scopes.sessions.has(`*\u0000${sessionId}`);
}
/** 采集读数（C-P0-9）：待采/已采/跳过/degraded/orphan 与文件数对账。 */
function corpusStats(dataDir, host) {
	const db = openCorpusReadOnly(dataDir);
	if (db === null) return {
		as_of: utcStamp(/* @__PURE__ */ new Date()),
		scope: host !== null && host !== void 0 && host !== "" ? { host } : {},
		segments: 0,
		sessions: 0,
		by_host: {},
		by_project: {},
		by_status: {},
		degraded: [],
		orphan: [],
		unmapped_cwds: [],
		lag_sec: null,
		db_bytes: {},
		db_missing: true,
		forgotten_sessions: 0,
		alarm: null,
		window_days: 0,
		skipped_by_window: 0,
		tombstones: {
			session: 0,
			host: 0,
			pointer: 0,
			range: 0
		},
		stall: {
			zero_streak: 0,
			alarm: null
		}
	};
	try {
		const scopes = tombstoneScopes(db);
		const liveRows = db.prepare("SELECT host, session_id, COUNT(*) AS n FROM corpus_segment GROUP BY host, session_id").all();
		const liveCounts = /* @__PURE__ */ new Map();
		for (const row of liveRows) {
			const key = `${row.host}\u0000${row.session_id}`;
			let live = Number(row.n);
			if (live > 0 && (scopes.pointers.length > 0 || scopes.ranges.length > 0)) live = countLiveSegments(db, scopes, row.host, row.session_id, live);
			liveCounts.set(key, live);
		}
		const byHost = {};
		const sourceRows = db.prepare("SELECT host, COUNT(*) AS files, COALESCE(SUM(segments_kept),0) AS segs FROM corpus_source GROUP BY host").all();
		for (const row of sourceRows) byHost[row.host] = {
			sources: Number(row.files),
			segments_kept: Number(row.segs),
			sessions: 0,
			segments: 0
		};
		let keptSessions = 0;
		let forgottenSeen = 0;
		const byProjectRaw = {};
		const sessionRows = db.prepare("SELECT host, session_id, project, segments FROM corpus_session ORDER BY host, session_id").all();
		for (const row of sessionRows) {
			if (isSessionForgotten(scopes, row.host, row.session_id)) {
				forgottenSeen += 1;
				continue;
			}
			keptSessions += 1;
			const key = `${row.host}\u0000${row.session_id}`;
			const segments = liveCounts.get(key) ?? 0;
			const slot = byHost[row.host] ??= {
				sources: 0,
				segments_kept: 0,
				sessions: 0,
				segments: 0
			};
			slot.sessions += 1;
			slot.segments += segments;
			const bucket = byProjectRaw[row.host] ??= /* @__PURE__ */ new Map();
			const item = bucket.get(String(row.project)) ?? {
				project: String(row.project),
				sessions: 0,
				segments: 0
			};
			item.sessions += 1;
			item.segments += segments;
			bucket.set(String(row.project), item);
		}
		const byProject = {};
		for (const [hostKey, bucket] of Object.entries(byProjectRaw)) byProject[hostKey] = [...bucket.keys()].sort().map((key) => bucket.get(key));
		let totalSegments = 0;
		for (const value of liveCounts.values()) totalSegments += value;
		const byStatus = {};
		const statusRows = db.prepare("SELECT host, status, COUNT(*) AS n FROM corpus_source GROUP BY host, status").all();
		for (const row of statusRows) {
			const slot = byStatus[row.host] ??= {};
			slot[row.status] = Number(row.n);
		}
		const degraded = db.prepare("SELECT * FROM corpus_degraded ORDER BY last_seen_at DESC").all().map((row) => ({
			...row,
			path: safeField(row.path)
		}));
		const orphan = db.prepare("SELECT host, session_id, path, status FROM corpus_source ORDER BY host, session_id").all().filter((row) => String(row.status ?? "") === "orphan").map((row) => ({
			host: String(row.host),
			session_id: String(row.session_id),
			path: safeField(row.path)
		}));
		const unmapped = db.prepare("SELECT workspace, COUNT(*) AS n FROM corpus_session WHERE project='' AND workspace<>'' GROUP BY workspace ORDER BY n DESC LIMIT 10").all().map((row) => ({
			workspace: safeField(row.workspace),
			sessions: Number(row.n)
		}));
		const alarmRow = db.prepare("SELECT v FROM corpus_meta WHERE k='stall.alarm'").get();
		const alarm = alarmRow?.v === void 0 || alarmRow.v === "" ? null : String(alarmRow.v);
		const streak = metaInt(db, "stall.zero_streak");
		return {
			as_of: utcStamp(/* @__PURE__ */ new Date()),
			scope: host !== null && host !== void 0 && host !== "" ? { host } : {},
			segments: totalSegments,
			sessions: keptSessions,
			by_host: byHost,
			by_project: byProject,
			by_status: byStatus,
			degraded,
			orphan,
			unmapped_cwds: unmapped,
			window_days: metaInt(db, "ingest.window_days"),
			skipped_by_window: metaInt(db, "ingest.skipped_by_window"),
			lag_sec: lagSeconds(db),
			db_bytes: dbBytes(db),
			forgotten_sessions: forgottenSeen,
			tombstones: {
				session: scopes.sessions.size,
				host: scopes.hosts.size,
				pointer: scopes.pointers.length,
				range: scopes.ranges.length
			},
			alarm,
			stall: {
				zero_streak: streak,
				alarm
			}
		};
	} finally {
		db.close();
	}
}
/** 台账命中的段要扣掉：数"这段会话还有几段是真活着"。 */
function countLiveSegments(db, scopes, host, sessionId, fallback) {
	const row = db.prepare("SELECT COUNT(*) AS n FROM corpus_segment WHERE host=? AND session_id=?").get(host, sessionId);
	const total = Number(row?.n ?? fallback);
	if (scopes.pointers.length === 0 && scopes.ranges.length === 0) return total;
	const clauses = [];
	const args = [host, sessionId];
	for (const pointer of scopes.pointers) {
		if (pointer.host !== host || pointer.sid !== sessionId) continue;
		clauses.push("(loc=? AND part=?)");
		args.push(pointer.loc, pointer.part);
	}
	for (const range of scopes.ranges) {
		if (range.host !== null && range.host !== host) continue;
		clauses.push("(occurred_at>=? AND occurred_at<=?)");
		args.push(range.start, range.end);
	}
	if (clauses.length === 0) return total;
	const hit = db.prepare(`SELECT COUNT(*) AS n FROM corpus_segment WHERE host=? AND session_id=? AND (${clauses.join(" OR ")})`).get(...args);
	return Math.max(0, total - Number(hit?.n ?? 0));
}
/** `fish corpus search <q>` 的回执。 */
function renderCorpusSearch(result) {
	const lines = [`命中 ${result.meta.total_matched} 条 / 返回 ${result.meta.returned} 条（ranker=${result.meta.ranker}${result.meta.truncated ? "，已截断" : ""}）`];
	for (const hit of result.hits) {
		const title = hit.session_title !== "" ? hit.session_title : hit.session_id.slice(0, 8);
		lines.push(`- ${hit.pointer}  score=${hit.score.toFixed(3)}  ${title}  ${hit.snippet.slice(0, 60).replace(/\n/gu, " ")}`);
		lines.push(`    理由：${hit.why.join("；")}`);
	}
	if (result.hits.length === 0) lines.push("  （没有命中：换个词，或先跑 `fish corpus ingest`）");
	for (const item of result.meta.degraded) lines.push(`  ⚠ ${item.reason}：${item.note}`);
	return lines.join("\n");
}
/** `fish corpus read <pointer>` 的回执（门牌错误也走这里，退 2）。 */
function renderCorpusRead(result, maxChars) {
	const lines = [
		result.pointer,
		`as_of=${result.as_of}｜${result.occurred_at}｜${result.role}｜${result.project !== "" ? result.project : "未归属"}｜${result.session_title !== "" ? result.session_title : result.sid8}`,
		"",
		result.text,
		""
	];
	if (result.truncated) lines.push(`（已截断到 ${maxChars} 字；库内 ${result.chars} 字）`);
	if (result.source.missing) lines.push(`（${result.source.note}）`);
	return lines.join("\n");
}
/** `fish corpus read` 的门牌错误回执（`run_cli` 的 `PointerError` 分支）。 */
function renderPointerError(error) {
	const lines = [`门牌错误（${error.code}）：${error.message}`];
	for (const candidate of error.candidates) lines.push(`  候选：${candidate}`);
	return lines.join("\n");
}
/** `fish corpus stats` 的回执：生产是 **JSON**（`run_command` 的 else 分支）。 */
function renderCorpusStats(result) {
	return JSON.stringify(result, null, 2);
}
/** 语料回执的固定三行头（MCP 面用；CLI 面不带——两处口径不同，如实分开）。 */
function corpusHeader(third, now = /* @__PURE__ */ new Date()) {
	return [
		CORPUS_SOURCE_LINE,
		`as_of=${isoLocal(now)}`,
		third
	];
}
//#endregion
//#region src/domain/health.ts
/**
* 自检（复刻 `cmd_healthcheck`，fish.py:10851 的**核心读数**）。
*
* 七项读数：事件数 / 活跃记忆 / 休眠 / 作废 / 断链 / FTS 是否落后 / owner 是否有效公民。
*
* 为什么断链必须在自检层报：`code_index.file_path` 指向的正文文件没了，记忆**依然查得到**
* （索引行还在），只有真去读正文时才会发现"这条记忆是空的"。这类失真没有任何报错。
*
* 为什么 FTS 落后要单独一条：索引落后 1 条时"存进去但搜不到"，是同一类静默失真。
*
* 为什么 owner 要查：`owner` 是"这条记忆归谁"的唯一字段，指向不存在的公民就是
* 一条指向幽灵的归属——它会被所有按 owner 的统计静默算错。
*
* @module dsh-fish/domain/health
*/
/** 采一次读数（**只读**：全部是 SELECT）。 */
function healthcheckReadings(db, dataRoot) {
	const countOf = (sql) => {
		const row = db.prepare(sql).get();
		return Number(row?.n ?? 0);
	};
	const events = countOf("SELECT COUNT(*) AS n FROM events");
	const activeMemories = countOf("SELECT COUNT(*) AS n FROM code_index WHERE status='active'");
	const dormant = countOf("SELECT COUNT(*) AS n FROM code_index WHERE dormant=1");
	const withdrawn = countOf("SELECT COUNT(*) AS n FROM code_index WHERE status='withdrawn'");
	const brokenLinks = [];
	const rows = db.prepare("SELECT code,file_path FROM code_index").all();
	for (const row of rows) {
		if (row.file_path === null || row.file_path === "") continue;
		if (!existsSync(path.join(dataRoot, row.file_path))) brokenLinks.push(row.code);
	}
	let fts = -1;
	try {
		fts = countOf("SELECT COUNT(*) AS n FROM memory_fts");
	} catch {
		fts = -1;
	}
	const total = countOf("SELECT COUNT(*) AS n FROM code_index");
	const ftsGap = fts < 0 ? 0 : total - fts;
	const citizens = db.prepare("SELECT citizen_id FROM citizens").all();
	const ids = new Set(citizens.map((row) => row.citizen_id));
	const ownerInvalid = [];
	if (ids.size > 0) {
		const owners = db.prepare("SELECT code,owner FROM code_index").all();
		for (const row of owners) {
			if (row.owner === "unknown") continue;
			if (row.owner === null || !ids.has(row.owner)) ownerInvalid.push(row.code);
		}
	}
	return {
		events,
		activeMemories,
		dormant,
		withdrawn,
		brokenLinks,
		fts,
		ftsGap,
		ownerInvalid,
		corpus: readCorpus(dataRoot)
	};
}
/** 语料层读数（读不出来 → `null`，不冒充"0 段"）。 */
function readCorpus(dataRoot) {
	try {
		const stats = corpusStats(dataRoot);
		return {
			segments: stats.segments,
			sessions: stats.sessions,
			degraded: stats.degraded.length,
			alarm: stats.alarm,
			zeroStreak: stats.stall.zero_streak
		};
	} catch {
		return null;
	}
}
/**
* 渲染自检文本（逐行对齐 `cmd_healthcheck` 的 print）。
*
* 文案里的下一步（`跑 fish reindex` / `跑 fish rebuild`）是**结论的一部分**：
* 只说"落后 3 条"而不说怎么补，读数就只是一句让人焦虑的话。
*/
function renderHealthcheck(readings) {
	const lines = [];
	lines.push(`事件 ${readings.events} 条 / 活跃记忆 ${readings.activeMemories} 条 / 休眠 ${readings.dormant} 条 / 作废 ${readings.withdrawn} 条 / 断链 ${readings.brokenLinks.length} 条`);
	for (const code of readings.brokenLinks) lines.push(`  断链：${code}`);
	if (readings.fts < 0) lines.push("  ⚠ 全文索引不可用（memory_fts 不存在）——跑 fish reindex 重建");
	else if (readings.ftsGap !== 0) lines.push(`  全文索引 ${readings.fts} 条  ⚠ 落后 ${readings.ftsGap} 条（跑 fish reindex 补齐）`);
	else lines.push(`  全文索引 ${readings.fts} 条（与记忆同步）`);
	if (readings.ownerInvalid.length > 0) lines.push(`  ⚠ owner 不是有效公民 ${readings.ownerInvalid.length} 条：${readings.ownerInvalid.slice(0, 5).join(", ")}（跑 fish rebuild 可归一到系统主人）`);
	const corpus = readings.corpus ?? null;
	if (corpus !== null) lines.push(`  语料 ${corpus.segments} 段 / 会话 ${corpus.sessions} / 降级 ${corpus.degraded}` + (corpus.alarm !== null ? `  ⚠ 停滞 ${corpus.alarm}（连续 ${corpus.zeroStreak} 轮 0 新增）` : ""));
	return `${lines.join("\n")}\n`;
}
//#endregion
//#region src/domain/config.ts
/**
* 配置键表（**逐条对齐** `fish.py:92` 的 `CONFIG_DEFAULTS`）。
*
* 只收键名/默认值/类型三件事：`enum` 的可选值只在写入侧校验，读侧不需要它，
* 但为了让"这张表就是契约"这件事完整，枚举键的 allowed 一并记录。
*/
const CONFIG_DEFAULTS = {
	"neighbors.strategy": {
		defaultValue: "mixed",
		type: "enum",
		allowed: [
			"code",
			"time",
			"tag",
			"type",
			"mixed"
		]
	},
	"neighbors.n": {
		defaultValue: 3,
		type: "int"
	},
	"neighbors.cross_project": {
		defaultValue: false,
		type: "bool"
	},
	"neighbors.include_dormant": {
		defaultValue: false,
		type: "bool"
	},
	timezone: {
		defaultValue: "Asia/Shanghai",
		type: "string"
	},
	"layout.home_root": {
		defaultValue: "",
		type: "string"
	},
	"board.sweep_days": {
		defaultValue: 7,
		type: "int"
	},
	"board.overlap_window_min": {
		defaultValue: 90,
		type: "int"
	},
	"inject.budget_tokens": {
		defaultValue: 1600,
		type: "int"
	},
	"inject.style": {
		defaultValue: "gentle",
		type: "enum",
		allowed: ["gentle", "strong"]
	},
	"inject.rules_per_source": {
		defaultValue: 3,
		type: "int"
	},
	"inject.board_max_ratio": {
		defaultValue: .25,
		type: "float"
	},
	owner_citizen: {
		defaultValue: "",
		type: "string"
	},
	default_citizen: {
		defaultValue: "",
		type: "string"
	},
	actor_name: {
		defaultValue: "",
		type: "string"
	},
	"decay.episode_days": {
		defaultValue: 21,
		type: "int"
	},
	"decay.fact_days": {
		defaultValue: 60,
		type: "int"
	},
	"decay.lesson_days": {
		defaultValue: 365,
		type: "int"
	},
	"decay.artifact_days": {
		defaultValue: 180,
		type: "int"
	},
	"decay.rule_days": {
		defaultValue: 0,
		type: "int"
	},
	"decay.identity_days": {
		defaultValue: 0,
		type: "int"
	},
	"decay.dormant_threshold": {
		defaultValue: .5,
		type: "float"
	},
	"decay.archive_threshold": {
		defaultValue: .1,
		type: "float"
	},
	"push.enabled": {
		defaultValue: true,
		type: "bool"
	},
	"push.serendipity_ratio": {
		defaultValue: .1,
		type: "float"
	},
	"push.open_budget_tokens": {
		defaultValue: 450,
		type: "int"
	},
	"push.after_tool_budget_tokens": {
		defaultValue: 100,
		type: "int"
	},
	"push.situational_budget_tokens": {
		defaultValue: 80,
		type: "int"
	},
	"push.after_tool_every_n_calls": {
		defaultValue: 10,
		type: "int"
	},
	"push.gap_window_days": {
		defaultValue: 14,
		type: "int"
	},
	"push.gap_others_read_min": {
		defaultValue: 3,
		type: "int"
	},
	"push.situational_window_min": {
		defaultValue: 10,
		type: "int"
	},
	"push.gap_early_phase_ratio": {
		defaultValue: .3,
		type: "float"
	},
	"push.outcome_window_hours": {
		defaultValue: 24,
		type: "int"
	},
	"board.auto_presence": {
		defaultValue: true,
		type: "bool"
	},
	"board.presence_session_rows": {
		defaultValue: true,
		type: "bool"
	},
	"board.active_window_sec": {
		defaultValue: 300,
		type: "int"
	},
	"board.collision_window_min": {
		defaultValue: 30,
		type: "int"
	},
	"board.collision_ask": {
		defaultValue: "warn",
		type: "enum",
		allowed: [
			"off",
			"warn",
			"ask"
		]
	},
	"board.collision_ask_cooldown_sec": {
		defaultValue: 60,
		type: "int"
	},
	"board.collision_min_segments": {
		defaultValue: 2,
		type: "int"
	},
	"push.prompt_enabled": {
		defaultValue: true,
		type: "bool"
	},
	"push.prompt_budget_tokens": {
		defaultValue: 120,
		type: "int"
	},
	"push.prompt_max_items": {
		defaultValue: 2,
		type: "int"
	},
	"push.prompt_min_interval_sec": {
		defaultValue: 90,
		type: "int"
	},
	"push.prompt_min_relevance": {
		defaultValue: .6,
		type: "float"
	},
	"push.prompt_stale_floor": {
		defaultValue: .35,
		type: "float"
	},
	"push.prompt_exclude_hours": {
		defaultValue: 24,
		type: "int"
	},
	"activity.enabled": {
		defaultValue: true,
		type: "bool"
	},
	"activity.retention_days": {
		defaultValue: 14,
		type: "int"
	},
	"activity.aggregate_window_min": {
		defaultValue: 30,
		type: "int"
	},
	"activity.snapshot_min_interval_sec": {
		defaultValue: 20,
		type: "int"
	},
	"activity.snapshot_max_age_sec": {
		defaultValue: 3600,
		type: "int"
	},
	"activity.presence_cache_min_interval_sec": {
		defaultValue: 20,
		type: "int"
	},
	"session.token_ttl_hours": {
		defaultValue: 0,
		type: "int"
	},
	"session.archive_after_days": {
		defaultValue: 0,
		type: "int"
	}
};
/** 取键定义；未知键**响亮失败**（与 Python `KeyError` 同义，但带下一步）。 */
function configKeyDef(key) {
	const def = CONFIG_DEFAULTS[key];
	if (def === void 0) throw new FishError("CONTRACT_INVALID", `未知配置键：${key}`, {
		hint: `可用键见 src/domain/config.ts 的 CONFIG_DEFAULTS（与 p0/fish.py 的 CONFIG_DEFAULTS 同表）`,
		details: { key }
	});
	return def;
}
/**
* 读一个配置键（复刻 `get_config`，fish.py:1870）。
*
* 口径逐条对齐 Python：库里没有该行 → 默认值；`int` 走 `parseInt`、`float` 走 `parseFloat`、
* `bool` 只在原值为字符串 `"true"` 时为真（Python 是 `raw == "true"`，**不是**真值判断）。
*/
function getConfig(db, key) {
	const def = configKeyDef(key);
	const row = db.prepare("SELECT value FROM config WHERE key=?").get(key);
	if (row === void 0) return def.defaultValue;
	const raw = row.value;
	switch (def.type) {
		case "int": {
			const parsed = Number.parseInt(raw, 10);
			if (Number.isNaN(parsed)) throw new FishError("CONTRACT_INVALID", `配置 ${key} 不是整数：${JSON.stringify(raw)}`, { hint: `fish config set ${key} <整数>` });
			return parsed;
		}
		case "float": {
			const parsed = Number.parseFloat(raw);
			if (Number.isNaN(parsed)) throw new FishError("CONTRACT_INVALID", `配置 ${key} 不是浮点数：${JSON.stringify(raw)}`, { hint: `fish config set ${key} <小数>` });
			return parsed;
		}
		case "bool": return raw === "true";
		default: return raw;
	}
}
/** 读整数配置（带范围夹紧的调用方自己夹，这里只保证类型）。 */
function getConfigInt(db, key) {
	const value = getConfig(db, key);
	return typeof value === "number" ? Math.trunc(value) : Number.parseInt(String(value), 10);
}
/** 读浮点配置。 */
function getConfigFloat(db, key) {
	const value = getConfig(db, key);
	return typeof value === "number" ? value : Number.parseFloat(String(value));
}
/** 读布尔配置。 */
function getConfigBool(db, key) {
	return getConfig(db, key) === true;
}
/** 读字符串配置。 */
function getConfigString(db, key) {
	const value = getConfig(db, key);
	return typeof value === "string" ? value : String(value);
}
/** 全部 `decay.*` 键的现值（复刻 `load_decay_cfg`，fish.py:6198）。 */
function loadDecayConfig(db) {
	const out = {};
	for (const key of Object.keys(CONFIG_DEFAULTS)) {
		if (!key.startsWith("decay.")) continue;
		out[key] = getConfigFloat(db, key);
	}
	return out;
}
//#endregion
//#region src/domain/identity.ts
/**
* 身份层（只读）：凭证校验 / 证据分级 / `whoami` 回执。
*
* 依据（逐条对齐 Python）：
*   · `lookup_session_token` / `validate_session_token`（fish.py:5191 / 5214）
*   · `credential_state_line` / `identity_source_line`（fish.py:5264 / 5248）
*   · `load_citizen` / `find_session_citizen` / `resolve_owner`（fish.py:5329 / 5315 / 4447）
*   · `related_citizens` / `related_citizens_lines`（fish.py:4581 / 4650）
*   · MCP `tool_whoami` 的**逐行文案**（mcp_server.py:1088），含 `IDENTITY_HINTS`（mcp_server.py:78）
*
* ## 两条不许越的线
*
* 1. **有效性判定只有一处**：`validateSessionToken`。吊销/过期/不存在都在它里面判完，
*    调用方只读结果、不自己拼 SQL——两处实现必然漂，而"什么算失效"是凭证体系里最不该漂的。
* 2. **证据级别只降不升**（`clampEvidence`）：凭证行里的 `evidence` 是**天花板**。
*    影子期输入来自参数（token / session_key），会话键本身不是持有点——拿它声称
*    `machine` 就是把自报当机器事实。`session_key` 单独使用时一律降为 `self-reported`，
*    与 Python 侧 `build_session_identity` 的"env → machine、自报 → self-reported、
*    都没有 → 匿名"同一套排序（fish.py:4999）。
*
* @module dsh-fish/domain/identity
*/
/** 证据级别（与 `SESSION_EVIDENCE_LEVELS` 同序）。 */
const EVIDENCE_LEVELS = [
	"machine",
	"inherited",
	"self-reported"
];
/** 证据级别的高低：数字越大越可信。**只用来降级，不用来升级**。 */
const EVIDENCE_RANK = {
	machine: 3,
	inherited: 2,
	"self-reported": 1
};
/** 身份解析状态（对齐 mcp_server 的 `IDENTITY_*` 常量）。 */
const IDENTITY_STATUS = {
	VALID: "valid",
	NONE: "none",
	INVALID: "invalid",
	REVOKED: "revoked",
	EXPIRED: "expired"
};
/** 纯匿名身份（没带 token）。 */
const IDENTITY_ANONYMOUS = {
	status: IDENTITY_STATUS.NONE,
	citizenId: null,
	sessionKey: null,
	evidence: null
};
/** 三档身份来源文案（`IDENTITY_SOURCE_*`，MCP 与 CLI 共用）。 */
const IDENTITY_SOURCE_TOKEN = "身份来源：token（session_tokens）";
const IDENTITY_SOURCE_REGISTER = "身份来源：register 登记（不参与归属）";
const IDENTITY_SOURCE_UNATTRIBUTED = "身份来源：未归属";
/** 凭证状态的固定文案（见 `credentialStateLine` 的五档）。 */
const CREDENTIAL_NO_TOKEN = "凭证状态：未提供凭证（无凭证写入的归属会记成 unattributed）";
const CREDENTIAL_UNKNOWN = "凭证状态：未知（这条 token 不在库中——明文只在签发时输出一次，丢了就重签：同一个 session_name → 同一个公民，只换凭证）";
/** `IDENTITY_HINTS`（mcp_server.py:78）——四种"为什么本次不能署名"的可执行下一步。 */
const IDENTITY_HINTS = {
	[IDENTITY_STATUS.NONE]: "⚠ 本次调用未带会话凭证，归属记为“未归属”（unattributed）。\n   拿凭证：**直接调 `session_start` 工具**（本进程内即可，不用跑 CLI）：\n     · 已登记过 → session_start(citizen=\"<你的 citizen_id>\", session_name=\"<稳定名字>\")\n     · 还没登记 → 先 register 拿 citizen_id，再按上面调\n   拿到 token 后，之后每次调用带 token=...（明文只出现一次）。\n   ⚠ 为什么不建议跑 CLI：CLI 的会话键与 MCP 不同，会建成另一个公民，两条通道的归属从此对不上且无法合并。",
	[IDENTITY_STATUS.INVALID]: "⚠ 凭证无效（session_tokens 表里没有这条 token），归属记为“未归属”（unattributed）。\n   调 `session_start`（带同一个 session_name）换一条新凭证：公民不变，只换 token。",
	[IDENTITY_STATUS.REVOKED]: "⚠ 凭证已失效（被 `fish session end` 吊销），归属记为“未归属”（unattributed）。\n   调 `session_start`（带同一个 session_name）换一条新凭证：公民不变，只换 token。",
	[IDENTITY_STATUS.EXPIRED]: "⚠ 凭证已失效（超过 TTL 自动过期），归属记为“未归属”（unattributed）。\n   调 `session_start`（带同一个 session_name）换一条新凭证：公民不变，只换 token。"
};
/** 库里只存 `sha256(token)`（明文没有第二份副本）。 */
function hashSessionToken(token) {
	return createHash("sha256").update(token ?? "", "utf8").digest("hex");
}
/** 当前 UTC 时间（`%Y-%m-%dT%H:%M:%SZ`，与库里的时间口径一致）。 */
function nowUtc(at = /* @__PURE__ */ new Date()) {
	return `${at.toISOString().slice(0, 19)}Z`;
}
/** 解析定长 UTC 时间戳；解析不了返回 `null`（复刻 `parse_utc`，fish.py:320）。 */
function parseUtc(value) {
	if (value === null || value === void 0 || value === "") return null;
	if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(value)) return null;
	const parsed = new Date(value);
	return Number.isNaN(parsed.getTime()) ? null : parsed;
}
/** `expires_at` 为空 = 永不失效；定长 UTC 的字典序就是时序（复刻 `_is_expired`）。 */
function isExpired(expiresAt, at = nowUtc()) {
	if (expiresAt === null || expiresAt === void 0 || expiresAt === "") return false;
	return expiresAt <= at;
}
/** 到 `expires_at` 还剩几小时：未过期向上取整、至少 1；已过期返回 0（复刻 `remaining_token_hours`）。 */
function remainingTokenHours(expiresAt, at = nowUtc()) {
	if (isExpired(expiresAt, at)) return 0;
	const moment = parseUtc(at);
	const expire = parseUtc(expiresAt);
	if (moment === null || expire === null) throw new Error(`到期时刻无法解析：${JSON.stringify(expiresAt)}`);
	const seconds = (expire.getTime() - moment.getTime()) / 1e3;
	return Math.max(1, Math.ceil(seconds / 3600));
}
/**
* **证据级别只降不升**。
*
* @param claimed 调用方声称的级别（可空 = 没声称）
* @param ceiling 凭证行/环境事实给出的级别（可空 = 没有任何证据）
* @returns `effective` = 两者里**更低**的那个；`downgraded` = 是否真的降了
*/
function clampEvidence(claimed, ceiling) {
	if (ceiling === null || ceiling === void 0) return {
		effective: null,
		downgraded: false
	};
	if (claimed === null || claimed === void 0) return {
		effective: ceiling,
		downgraded: false
	};
	if ((EVIDENCE_RANK[claimed] ?? 0) <= (EVIDENCE_RANK[ceiling] ?? 0)) return {
		effective: claimed,
		downgraded: false
	};
	return {
		effective: ceiling,
		downgraded: true
	};
}
/** 按明文查凭证行（**不判有效性**：吊销/过期也返回）；查不到返回 `null`。 */
function lookupSessionToken(db, token) {
	if (token === null || token === void 0 || token === "") return null;
	const row = db.prepare("SELECT token_hash,citizen_id,session_key,evidence,channel,parent_token,issued_at,expires_at,revoked_at FROM session_tokens WHERE token_hash=?").get(hashSessionToken(token));
	if (row === void 0) return null;
	const expired = isExpired(row.expires_at);
	return {
		...row,
		expired,
		valid: row.revoked_at === null && !expired
	};
}
/**
* **MCP 侧的唯一校验契约**：有效 → citizen_id；无效（吊销/过期/不存在）→ `null`。
*
* 对应的那条 SQL（fish.py:5217）：
*   `token_hash=? AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now)`
* 注意它**不是** SQL 里的一个 WHERE——Python 是取行后在 Python 里判，所以这里也照做：
* 差异点在 `expires_at <= now`（含等号）与 `revoked_at <= now`，不是 `<`。
*/
function validateSessionToken(db, token, at) {
	const info = lookupSessionToken(db, token);
	if (info === null) return null;
	const moment = at ?? nowUtc();
	if (info.revoked_at !== null && info.revoked_at <= moment) return null;
	if (info.expires_at !== null && info.expires_at <= moment) return null;
	return info.citizen_id;
}
/** 一行写明这次回执的身份是从哪来的（三档，MCP / CLI 共用）。 */
function identitySourceLine(citizenId, options = {}) {
	if (citizenId !== null && citizenId !== void 0 && citizenId !== "") return IDENTITY_SOURCE_TOKEN;
	return options.registered === true ? IDENTITY_SOURCE_REGISTER : IDENTITY_SOURCE_UNATTRIBUTED;
}
/**
* 本条凭证**处于什么状态**（一行五档）。
*
* 五档：没带凭证 / 不在库中 / 已吊销 / 已过期 / 永久·剩余 N 小时。
* 判定不在这一层：吊销与到期都读 `lookupSessionToken` 的现查结果，
* "什么算失效"永远只有 `validateSessionToken` 一个答案；这里只负责措辞。
*/
function credentialStateLine(db, token) {
	if (token === null || token === void 0 || token === "") return CREDENTIAL_NO_TOKEN;
	const info = lookupSessionToken(db, token);
	if (info === null) return CREDENTIAL_UNKNOWN;
	if (info.revoked_at !== null && info.revoked_at !== "") return `凭证状态：已吊销（${info.revoked_at}）——重跑 session_start 换一条`;
	if (info.expires_at === null || info.expires_at === "") return "凭证状态：永久（随会话 end 吊销）";
	if (parseUtc(info.expires_at) === null) return `凭证状态：到期时刻无法解析（${JSON.stringify(info.expires_at)}）——这条凭证不算有效（归属会被记成 unattributed）；重跑 session_start 换一条`;
	if (isExpired(info.expires_at)) return `凭证状态：已过期（${info.expires_at}）；重跑 session_start 换一条`;
	try {
		return `凭证状态：剩余 ${remainingTokenHours(info.expires_at)} 小时`;
	} catch {
		return `凭证状态：到期时刻无法解析（${JSON.stringify(info.expires_at)}）——重跑 session_start 换一条`;
	}
}
const CITIZEN_COLUMNS = "citizen_id,display_name,kind,role,host,status,account_id,host_session_key,last_seen_at,archived_at";
/** 读一个公民（走 `citizens_all`）；查不到返回 `null`。 */
function loadCitizen(db, citizenId) {
	if (citizenId === null || citizenId === void 0 || citizenId === "") return null;
	return db.prepare(`SELECT ${CITIZEN_COLUMNS} FROM citizens_all WHERE citizen_id=?`).get(citizenId) ?? null;
}
/** 按宿主会话标识找**活跃**会话公民（走 `citizens_all`）。 */
function findSessionCitizen(db, sessionKey) {
	if (sessionKey === null || sessionKey === void 0 || sessionKey === "") return null;
	return db.prepare(`SELECT ${CITIZEN_COLUMNS} FROM citizens_all WHERE host_session_key=? AND status='active' ORDER BY created_at LIMIT 1`).get(sessionKey) ?? null;
}
/** 系统主人：优先 `citizens.is_owner`；回退旧配置（`owner_citizen` → `default_citizen`）。 */
function resolveOwner(db) {
	const row = db.prepare("SELECT citizen_id FROM citizens WHERE is_owner=1 AND status='active'").get();
	if (row !== void 0) return row.citizen_id;
	return getConfigString(db, "owner_citizen") || getConfigString(db, "default_citizen");
}
/** 相关公民清单的关联锚（白名单：列名只能来自实现内部常量表）。 */
const RELATED_ANCHORS = [
	["session", "host_session_key"],
	["name", "display_name"],
	["account", "account_id"]
];
/**
* 与本会话上下文相关的公民（`whoami all=true` 的唯一取数口）：三条锚任一命中即算相关，
* 按 citizen_id 去重。**不是全库清单**：没有锚就空转返回。
*/
function relatedCitizens(db, options = {}) {
	const values = {
		session: options.hostSessionKey,
		name: options.displayName,
		account: options.accountId
	};
	const columns = "citizen_id,display_name,kind,status,host,last_seen_at,created_at";
	const found = /* @__PURE__ */ new Map();
	for (const [relation, column] of RELATED_ANCHORS) {
		const raw = values[relation];
		const anchor = typeof raw === "string" ? raw.trim() : raw;
		if (anchor === null || anchor === void 0 || anchor === "") continue;
		const rows = db.prepare(`SELECT ${columns} FROM citizens WHERE ${column}=? ORDER BY CASE WHEN status='active' THEN 0 ELSE 1 END, created_at, citizen_id`).all(anchor);
		for (const row of rows) {
			let item = found.get(row.citizen_id);
			if (item === void 0) {
				item = {
					...row,
					relations: [],
					token_count: 0
				};
				found.set(row.citizen_id, item);
			}
			if (!item.relations.includes(relation)) item.relations.push(relation);
		}
	}
	if (options.excludeCitizenId) found.delete(options.excludeCitizenId);
	if (found.size === 0) return [];
	const ids = [...found.keys()];
	const placeholders = ids.map(() => "?").join(",");
	const counts = /* @__PURE__ */ new Map();
	const countRows = db.prepare(`SELECT citizen_id,COUNT(*) AS n FROM session_tokens WHERE revoked_at IS NULL AND citizen_id IN (${placeholders}) GROUP BY citizen_id`).all(...ids);
	for (const row of countRows) counts.set(row.citizen_id, Number(row.n));
	const order = new Map(RELATED_ANCHORS.map(([relation], index) => [relation, index]));
	const items = [...found.values()].sort((a, b) => {
		const activeA = a.status === "active" ? 0 : 1;
		const activeB = b.status === "active" ? 0 : 1;
		if (activeA !== activeB) return activeA - activeB;
		const minA = Math.min(...a.relations.map((r) => order.get(r) ?? 99));
		const minB = Math.min(...b.relations.map((r) => order.get(r) ?? 99));
		if (minA !== minB) return minA - minB;
		const createdA = a.created_at ?? "";
		const createdB = b.created_at ?? "";
		if (createdA !== createdB) return createdA < createdB ? -1 : 1;
		return a.citizen_id < b.citizen_id ? -1 : a.citizen_id > b.citizen_id ? 1 : 0;
	});
	for (const item of items) {
		item.relations = RELATED_ANCHORS.map(([relation]) => relation).filter((relation) => item.relations.includes(relation));
		item.token_count = counts.get(item.citizen_id) ?? 0;
	}
	const cut = Math.trunc(options.limit ?? 20);
	return cut > 0 ? items.slice(0, cut) : [];
}
/** `whoami all=true`（MCP）与 `whoami --all`（CLI）的**唯一渲染**。 */
function relatedCitizensLines(db, citizenId) {
	const head = `相关公民（**不含你自己**；按 会话/同名/账号 关联，不是全库清单；token 数 = 未吊销凭证数，最多 20 条）：`;
	if (citizenId === null || citizenId === void 0 || citizenId === "") return [head, "- （未列出）这份清单需要一条**有效 token** 当锚：没凭证就没有会话键、公民名与账号可关联——这不等于“库里没有人”。"];
	const me = loadCitizen(db, citizenId) ?? {};
	const rows = relatedCitizens(db, {
		displayName: me.display_name ?? null,
		hostSessionKey: me.host_session_key ?? null,
		accountId: me.account_id ?? null,
		excludeCitizenId: citizenId
	});
	if (rows.length === 0) return [head, "- （没有匹配到相关公民：本会话、同名、同账号都没有别人）"];
	const lines = [head];
	for (const it of rows) lines.push(`- ${it.citizen_id}｜${it.display_name || "（无名）"}｜${it.kind || "unknown"}｜${it.status || "unknown"}｜宿主 ${it.host || "未记录"}｜最近活跃 ${it.last_seen_at || "未记录"}｜token ${it.token_count}｜关联：${it.relations.join("/")}`);
	if (rows.length >= 20) lines.push(`（已达上限 20 条，可能还有更多；全库清单请用 \`fish citizen list\`）`);
	return lines;
}
/** 同名告警（只对**有效 token** 的 whoami 说；已退役的同名者不算）。 */
function sameNameWarnings(db, citizenId) {
	if (citizenId === null || citizenId === void 0 || citizenId === "") return [];
	const name = (loadCitizen(db, citizenId)?.display_name ?? "").trim();
	if (name === "") return [];
	const rows = relatedCitizens(db, {
		displayName: name,
		excludeCitizenId: citizenId
	});
	const out = [];
	for (const it of rows) {
		if (!it.relations.includes("name") || it.status !== "active") continue;
		out.push(`⚠ 检测到同名公民 ${it.citizen_id}（${it.kind || "unknown"}，最近活跃 ${it.last_seen_at || "未记录"}）——系统不合并；本次写入记在 ${citizenId} 名下`);
	}
	return out;
}
/** CLI 入口那一行（FB-13）：`fishHome` = 含 `fish.py` 的目录。 */
function cliEntryHint(fishHome) {
	const p0 = fishHome === "" ? "" : path.resolve(fishHome);
	return `CLI 入口：python ${path.join(p0 === "" ? "." : p0, "fish.py")}（或 install_cli.py --apply 生成 fish.cmd）`;
}
/** 已经自己讲清身份状况的工具：**不再追加**通用提示（否则同一句话出现两遍）。 */
const HINT_SELF_EXPLAINED = [
	"whoami",
	"register",
	"session_start"
];
/**
* 归属提示（MCP `call_tool` 在**这一层**统一追加，mcp_server.py:1968）。
*
* 为什么必须也复刻这一层：漏了它，TS 侧回执就比模型今天看到的少一整段
* （"你的归属会记成 unattributed" + 怎么拿凭证）——而那段话是**唯一**告诉读者
* "这次写入不算你的"的地方。追加规则与 Python 逐字一致：
* `text.rstrip("\n") + "\n\n" + hint + "\n"`。
*/
function appendIdentityHint(text, identity, toolName) {
	if (HINT_SELF_EXPLAINED.includes(toolName)) return text;
	const hint = IDENTITY_HINTS[identity.status];
	if (hint === void 0) return text;
	return `${text.replace(/\n+$/, "")}\n\n${hint}\n`;
}
/**
* 把"本次调用带的凭证"解析成 `Identity`（**纯函数：不写任何状态**）。
*
* 与 MCP `_resolve_identity`（mcp_server.py:631）同口径：
*   · 没 token → `none`；
*   · 有 token 但库里查不到 → `invalid`；
*   · 查得到但已吊销 / 已过期 → `revoked` / `expired`（**只为选提示文案**，判定仍只有一处）；
*   · 有效 → `valid`，`session_key` / `evidence` 取**凭证行里的值**（不取环境变量）。
*/
function resolveIdentity(db, token) {
	if (token === null || token === void 0 || token === "") return IDENTITY_ANONYMOUS;
	const citizenId = validateSessionToken(db, token);
	const info = lookupSessionToken(db, token);
	if (info === null) return {
		status: IDENTITY_STATUS.INVALID,
		citizenId: null,
		sessionKey: null,
		evidence: null
	};
	if (citizenId === null) return {
		status: info.revoked_at !== null && info.revoked_at !== "" ? IDENTITY_STATUS.REVOKED : info.expired ? IDENTITY_STATUS.EXPIRED : IDENTITY_STATUS.INVALID,
		citizenId: null,
		sessionKey: null,
		evidence: info.evidence
	};
	return {
		status: IDENTITY_STATUS.VALID,
		citizenId,
		sessionKey: info.session_key,
		evidence: info.evidence
	};
}
/**
* 解析一次 `whoami`（token 优先；没有 token 时按 session_key 兜底）。
*
* 为什么要 session_key 兜底：影子期没有 `session_start` 工具，运维现场常常只有会话键。
* 但 session_key **不是持有点**——所以它给的证据级别只能是 `self-reported`，
* 并且 `status` 仍是 `valid`（会话键确实对应一个活跃公民，写入归属却不是它的功劳）。
*/
function whoami(db, input = {}) {
	const token = (input.token ?? "").trim();
	const sessionKey = (input.sessionKey ?? "").trim();
	let identity = resolveIdentity(db, token === "" ? null : token);
	let citizenSource = token === "" ? "none" : "token";
	let ceiling = identity.evidence;
	if (identity.status !== IDENTITY_STATUS.VALID && sessionKey !== "") {
		const citizen = findSessionCitizen(db, sessionKey);
		if (citizen !== null) {
			identity = {
				status: IDENTITY_STATUS.VALID,
				citizenId: citizen.citizen_id,
				sessionKey,
				evidence: "self-reported"
			};
			citizenSource = "session_key";
			ceiling = "self-reported";
		}
	}
	const citizen = identity.citizenId === null ? null : loadCitizen(db, identity.citizenId);
	const clamped = clampEvidence(input.evidenceClaim ?? null, ceiling);
	return {
		status: identity.status,
		citizen_id: identity.citizenId,
		display_name: citizen?.display_name ?? null,
		kind: citizen?.kind ?? null,
		session_key: identity.sessionKey,
		evidence_ceiling: ceiling,
		evidence_effective: clamped.effective,
		evidence_downgraded: clamped.downgraded,
		citizen_source: citizenSource,
		owner_id: resolveOwner(db)
	};
}
/**
* `whoami` 的**完整回执文本**（逐行对齐 MCP `tool_whoami`，mcp_server.py:1088）。
*
* @param fishHome 含 `fish.py` 的目录（CLI 入口行要用；由调用方给，模块不猜）
*/
function renderWhoami(db, input, fishHome) {
	const report = whoami(db, input);
	const token = (input.token ?? "").trim();
	const lines = [];
	if (report.status === IDENTITY_STATUS.VALID) {
		const name = report.display_name ?? report.citizen_id ?? "";
		const kind = report.kind ?? "unknown";
		lines.push(`登记身份：${name}（公民 ${report.citizen_id ?? ""}，形态 ${kind}）`);
		lines.push(`凭证：会话 ${report.session_key ?? ""}｜证据 ${report.evidence_ceiling ?? ""}（来自 session_tokens 表，本次调用现查，不存进程内存）`);
		lines.push("说明：这是你在 fish 的账户名（宿主级），与你的宿主身份不冲突。");
	} else {
		lines.push("身份未核：本次调用没有可用的会话凭证，无法回答“我是谁”。");
		lines.push("（fish 不再保留进程内身份：一个 MCP 进程被所有会话共享，“记住最后一个 register 的人”正是 2026-10-23 串号事故的根因。）");
		lines.push(IDENTITY_HINTS[report.status] ?? "");
	}
	lines.push(credentialStateLine(db, token === "" ? null : token));
	lines.push(identitySourceLine(report.status === IDENTITY_STATUS.VALID ? report.citizen_id : null));
	lines.push(...sameNameWarnings(db, report.status === IDENTITY_STATUS.VALID ? report.citizen_id : null));
	if (report.owner_id !== "") {
		const owner = loadCitizen(db, report.owner_id);
		if (owner !== null) lines.push(`主人：${owner.display_name ?? ""}（公民 ${report.owner_id}，形态 ${owner.kind ?? ""}）`);
	} else lines.push("主人：未设置（fish config set owner_citizen <id>）");
	if (input.all === true) {
		lines.push("");
		lines.push(...relatedCitizensLines(db, report.status === IDENTITY_STATUS.VALID ? report.citizen_id : null));
	}
	lines.push(cliEntryHint(fishHome));
	return {
		text: `${lines.join("\n")}\n`,
		report
	};
}
//#endregion
//#region src/domain/names.ts
/** 别名种类（Python `_ALIAS_KINDS`）。 */
const ALIAS_KINDS = ["project", "citizen"];
/** 别名链最多走几跳（Python `_ALIAS_MAX_HOPS`）。 */
const ALIAS_MAX_HOPS = 8;
/** 一次读出该 kind 的全部别名（from → to）。行数是个位数，不做增量查询。 */
function aliasMap(db, kind) {
	const rows = db.prepare("SELECT from_name, to_name FROM name_aliases WHERE kind=?").all(kind);
	const out = /* @__PURE__ */ new Map();
	for (const row of rows) out.set(row.from_name, row.to_name);
	return out;
}
/** 沿别名走到规范名；成环时停在环入口（**不抛错**：读要永远能出结果）。 */
function walkAlias(amap, start) {
	const chain = [start];
	let cur = start;
	for (let hop = 0; hop < 8; hop += 1) {
		const next = amap.get(cur);
		if (next === void 0 || next === "" || chain.includes(next)) break;
		chain.push(next);
		cur = next;
	}
	return {
		canonical: cur,
		chain
	};
}
/**
* 把一个名字解析成规范名（复刻 `resolve_name`）。
* @throws FishError `CONTRACT_INVALID` —— kind 非法
*/
function resolveName(db, kind, value) {
	if (!ALIAS_KINDS.includes(kind)) throw new Error(`kind 只能是 ${ALIAS_KINDS.join("/")}，收到 ${String(kind)}`);
	const start = (value ?? "").trim();
	const empty = {
		input: start,
		canonical: "",
		aliases: [],
		chain: [],
		status: null,
		found: false
	};
	if (start === "") return empty;
	const amap = aliasMap(db, kind);
	const { canonical, chain } = walkAlias(amap, start);
	const aliases = [...amap.keys()].filter((from) => from !== canonical && walkAlias(amap, from).canonical === canonical).sort();
	let status = null;
	let found = false;
	const row = kind === "project" ? db.prepare("SELECT status FROM code_registry WHERE prefix=?").get(canonical) : db.prepare("SELECT status FROM citizens WHERE display_name=? AND status='active' ORDER BY created_at, citizen_id LIMIT 1").get(canonical);
	if (row !== void 0) {
		found = true;
		status = row.status;
	}
	return {
		input: start,
		canonical,
		aliases,
		chain,
		status,
		found
	};
}
/**
* 给定一个项目名，返回"同一个项目"的全部名字（规范名 + 别名，按 `resolve_name` 次序）。
*
* 这是别名机制对检索的唯一承诺：`recall(project='cdql')` 也要看到 `CDQL` 的记忆。
* 返回空数组 = 输入为空（调用方据此决定"没给"还是"查不到"）。
*/
function resolveProjectNames(db, project) {
	const p = (project ?? "").trim();
	if (p === "") return [];
	const info = resolveName(db, "project", p);
	const out = [];
	for (const name of [info.canonical, ...info.aliases]) if (name !== "" && !out.includes(name)) out.push(name);
	return out.length > 0 ? out : [p];
}
/**
* 工作区路径归一（复刻 `normalize_workspace_path`）：正斜杠 · 无尾斜杠 · 全小写。
*
* 三个平台事实（写在 Python docstring 里，这里照搬）：
*   · Windows 上 `\` 与 `/` 混用是常态，不统一就会"明明映射了却匹配不上"；
*   · 尾斜杠差一个字符就漏（前缀匹配是字符串操作），根路径保留最小形态；
*   · Windows 路径比较不区分大小写（POSIX 上这条会误合并，已声明为平台取舍）。
*/
function normalizeWorkspacePath(target) {
	if (target === null || target === void 0) return "";
	const raw = String(target).trim();
	if (raw === "") return "";
	let s = raw.replace(/\\/g, "/").replace(/\/+/g, "/");
	s = posixNormalize(s);
	while (s.length > 1 && s.endsWith("/")) s = s.slice(0, -1);
	if (/^[A-Za-z]:$/.test(s)) s = `${s}/`;
	return s.toLowerCase();
}
/**
* POSIX 风格的 `normpath`（只实现路径归一必需的那部分）。
*
* 为什么不用 `node:path`：`path.posix.normalize` 与本函数的行为差异点在"盘符根"与
* "前导 `//`"，而 Windows 上 `path.win32` 又会把 `/` 转回 `\`——归一化白做。
*/
function posixNormalize(input) {
	const absolute = input.startsWith("/");
	const parts = input.split("/");
	const out = [];
	for (const part of parts) {
		if (part === "" || part === ".") continue;
		if (part === "..") {
			if (out.length > 0 && out[out.length - 1] !== "..") out.pop();
			else if (!absolute) out.push("..");
			continue;
		}
		out.push(part);
	}
	const joined = `${absolute ? "/" : ""}${out.join("/")}`;
	return joined === "" ? "." : joined;
}
/**
* 按**最长前缀 + 路径分量边界**把工作区路径解析成项目前缀；没有匹配返回 `null`。
*
* 为什么不按"包含"匹配：`/a/b` 是 `/a/bc` 的字符串前缀但不是它的父路径，必须按分量边界判。
* 为什么全表扫：`workspace_map` 是按项目维护的配置表（行数是个位数），
* 而用 SQL LIKE 就得转义 `_` / `%`——转义写错是**静默错配**，最难查的那类 bug。
*/
function resolveProjectByCwd(db, cwd) {
	const target = normalizeWorkspacePath(cwd);
	if (target === "") return null;
	const rows = db.prepare("SELECT path_prefix, project FROM workspace_map").all();
	let best = null;
	for (const row of rows) {
		const norm = normalizeWorkspacePath(row.path_prefix);
		if (norm === "") continue;
		if (target !== norm && !target.startsWith(`${norm}/`)) continue;
		const candidate = {
			length: norm.length,
			project: row.project
		};
		if (best === null || candidate.length > best.length || candidate.length === best.length && candidate.project < best.project) best = candidate;
	}
	return best === null ? null : best.project;
}
/** 当前所有开档期（按 `(from_ts, id)` 升序；复刻 `current_open_stints`）。 */
function currentOpenStints(db, citizenId) {
	return db.prepare("SELECT id,citizen_id,account_id,project,from_ts,set_by FROM project_stints WHERE citizen_id=? AND to_ts IS NULL ORDER BY from_ts ASC, id ASC").all(citizenId);
}
/** 当前开档期里**最近开的**那一个（多项目时的"主项目"）；没有返回 `null`。 */
function currentOpenStint(db, citizenId) {
	const stints = currentOpenStints(db, citizenId);
	return stints.length === 0 ? null : stints[stints.length - 1];
}
/** 按 display_name 查活跃公民（取最早创建的那条，结果确定）。 */
function findCitizenByName(db, name) {
	const key = (name ?? "").trim();
	if (key === "") return null;
	return db.prepare("SELECT citizen_id,display_name,kind,role,host FROM citizens WHERE display_name=? AND status='active' ORDER BY created_at, citizen_id LIMIT 1").get(key) ?? null;
}
//#endregion
//#region src/domain/memory.ts
/** LIKE 的转义声明（必须与 `likeLiteral` 成对出现，否则反斜杠被当字面量）。 */
const LIKE_ESCAPE_SQL = " ESCAPE '\\'";
/** 列出模式的"匹配来源"列：告诉读者这条不是搜到的，是列出来的。 */
const LIST_SOURCE = "列出";
/** 列出模式的默认条数（比检索的 10 宽）。 */
const LIST_DEFAULT_LIMIT = 20;
/** `tags` 单独用时的引导语（MCP 与 CLI 共用一处）。 */
const TAGS_ONLY_HINT = "tags 只作附加过滤；请加 project 限定（例：recall(project=\"anyi-studio\", tags=[\"fish-feedback\"])）";
/** 检索结果第一行的维度说明（MCP 与 CLI 共用）。 */
const RECALL_DIMENSION_NOTE = "说明：status 与 dormant 是两个维度（dormant=衰减休眠，可被召回唤醒）";
/** 检索结果第二行：告诉读者这是关键词匹配，不是语义检索。 */
const RECALL_KEYWORD_NOTE = "（关键词匹配，不是语义检索；同义词查不到——见 ADR-006）";
/** LIKE 的**字面量**转义：`\` / `%` / `_`（标签是用户数据，通配符会查错却查到）。 */
function likeLiteral(value) {
	return (value === null || value === void 0 ? "" : String(value)).replace(/\\/g, "\\\\").replace(/%/g, "\\%").replace(/_/g, "\\_");
}
/**
* 标签过滤的 LIKE 模式：`%"<转义后的标签>"%`。
*
* **两层转义，顺序不能反**：① 先过 JSON（库里的文本是 `json.dumps` 的产物，
* 标签里的 `\` 与 `"` 已被 JSON 转义过一次）；② 再过 LIKE（JSON 吐出来的反斜杠
* 在 LIKE 里必须再转义一次）。先 LIKE 后 JSON 等于把①的产物当用户数据再转义一遍。
*/
function tagLikePattern(tag) {
	return `%"${likeLiteral(JSON.stringify(tag === null || tag === void 0 ? "" : String(tag)).slice(1, -1))}"%`;
}
/** 归一 `tags` 入参：字符串视为单元素，过滤空值。 */
function normalizeTags(tags) {
	if (tags === null || tags === void 0) return [];
	return (typeof tags === "string" ? [tags] : tags).filter((tag) => typeof tag === "string" && tag !== "");
}
/**
* 检索记忆（复刻 `search_memories`）。
*
* 排序优先级：精确编码 > 编码片段 > 标题命中 > 正文命中。
* 返回的 `rows` 已按 `order` 去重并截到 `limit`；`sources` 是 code → 匹配来源。
*/
function searchMemories(db, options = {}) {
	const limit = Math.max(1, Math.trunc(options.limit ?? 10));
	const order = [];
	const sources = {};
	const add = (codes, source) => {
		for (const code of codes) if (code !== "" && code !== void 0 && code !== null && sources[code] === void 0) {
			sources[code] = source;
			order.push(code);
		}
	};
	const query = (options.query ?? "").trim();
	const code = options.code ?? null;
	const project = options.project ?? null;
	const includeSuperseded = options.includeSuperseded === true;
	const scope = options.scope ?? "all";
	const projectNames = project !== null && project !== void 0 && project !== "" ? resolveProjectNames(db, project) : [];
	const filters = [];
	const filterParams = [];
	if (!includeSuperseded) filters.push("ci.status = 'active'");
	if (project !== null && project !== void 0 && project !== "") {
		filters.push(`ci.project IN (${projectNames.map(() => "?").join(",")})`);
		filterParams.push(...projectNames);
	}
	for (const tag of normalizeTags(options.tags)) {
		filters.push(`ci.tags LIKE ?${LIKE_ESCAPE_SQL}`);
		filterParams.push(tagLikePattern(tag));
	}
	const ftsSql = `SELECT f.code AS code FROM (SELECT code FROM memory_fts WHERE memory_fts MATCH ?) AS f JOIN code_index ci ON ci.code = f.code WHERE 1=1${filters.length > 0 ? ` AND ${filters.join(" AND ")}` : ""} LIMIT ?`;
	if (code !== null && code !== "") add(db.prepare("SELECT code FROM code_index WHERE code=?").all(code).map((row) => row.code), "编码");
	if (query !== "") {
		add(db.prepare("SELECT code FROM code_index WHERE code=?").all(query).map((row) => row.code), "编码");
		add(db.prepare("SELECT code FROM code_index WHERE code LIKE ? ORDER BY code DESC LIMIT ?").all(`%${query}%`, limit).map((row) => row.code), "编码");
		const expr = ftsMatch(query);
		if (expr !== null) {
			if (scope === "all" || scope === "title") add(db.prepare(ftsSql).all(`title_idx:(${expr})`, ...filterParams, limit).map((row) => row.code), "标题");
			if (scope === "all" || scope === "body") add(db.prepare(ftsSql).all(`body_idx:(${expr})`, ...filterParams, limit).map((row) => row.code), "正文");
		}
	}
	if (order.length === 0) return {
		rows: [],
		sources: {}
	};
	let sql = `SELECT code,title,project,mem_type,status,file_path,dormant FROM code_index WHERE code IN (${order.map(() => "?").join(",")})`;
	const params = [...order];
	if (!includeSuperseded) sql += " AND status = 'active'";
	if (project !== null && project !== void 0 && project !== "") {
		sql += ` AND project IN (${projectNames.map(() => "?").join(",")})`;
		params.push(...projectNames);
	}
	for (const tag of normalizeTags(options.tags)) {
		sql += ` AND tags LIKE ?${LIKE_ESCAPE_SQL}`;
		params.push(tagLikePattern(tag));
	}
	const found = /* @__PURE__ */ new Map();
	for (const row of db.prepare(sql).all(...params)) found.set(row.code, row);
	const rows = [];
	for (const codeInOrder of order) {
		const row = found.get(codeInOrder);
		if (row === void 0) continue;
		rows.push([
			row.code,
			row.title ?? "",
			row.project ?? null,
			row.mem_type ?? "",
			row.status ?? "",
			row.file_path ?? null,
			Number(row.dormant ?? 0),
			sources[codeInOrder] ?? ""
		]);
		if (rows.length >= limit) break;
	}
	return {
		rows,
		sources
	};
}
/**
* 查询串 → FTS5 MATCH 表达式；没有可查 token 时 `null`。
*
* 切词复用 `src/layout/cut.ts`（与 Python `index_tokens` 逐 token 等价，已对拍 50/50），
* 这里只负责"每个 token 加双引号"——不加的话 `CR-12` 会被 FTS5 解析成「CR 列包含 12」。
*/
function ftsMatch(query) {
	const tokens = cutTokens(query).filter((token) => token !== "");
	if (tokens.length === 0) return null;
	return tokens.map((token) => `"${token.replace(/"/g, "\"\"")}"`).join(" AND ");
}
/**
* 未命中时给"最近的条目"作提示——**必须遵守与检索同样的过滤**（复刻 `recent_fallback`）。
*/
function recentFallback(db, options = {}) {
	const limit = Math.max(1, Math.trunc(options.limit ?? 5));
	let sql = "SELECT code,title FROM code_index";
	const where = [];
	const params = [];
	if (options.includeSuperseded !== true) where.push("status = 'active'");
	const project = options.project ?? null;
	if (project !== null && project !== void 0 && project !== "") {
		const names = resolveProjectNames(db, project);
		if (names.length <= 1) {
			where.push("project = ?");
			params.push(names.length === 1 ? names[0] ?? project : project);
		} else {
			where.push(`project IN (${names.map(() => "?").join(",")})`);
			params.push(...names);
		}
	}
	for (const tag of normalizeTags(options.tags)) {
		where.push(`tags LIKE ?${LIKE_ESCAPE_SQL}`);
		params.push(tagLikePattern(tag));
	}
	if (where.length > 0) sql += ` WHERE ${where.join(" AND ")}`;
	sql += " ORDER BY code DESC LIMIT ?";
	params.push(limit);
	return db.prepare(sql).all(...params);
}
/** 列出模式的 WHERE 子句（`count_memories` 与 `list_memories` 共用同一谓词）。 */
function listWhere(db, options) {
	const where = [];
	const params = [];
	if (options.includeSuperseded !== true) where.push("status = 'active'");
	const project = options.project ?? null;
	const names = project !== null && project !== void 0 && project !== "" ? resolveProjectNames(db, project) : [];
	if (names.length > 0) {
		where.push(`project IN (${names.map(() => "?").join(",")})`);
		params.push(...names);
	}
	for (const tag of normalizeTags(options.tags)) {
		where.push(`tags LIKE ?${LIKE_ESCAPE_SQL}`);
		params.push(tagLikePattern(tag));
	}
	return {
		clause: where.length > 0 ? ` WHERE ${where.join(" AND ")}` : "",
		params
	};
}
/** 列出模式命中多少条（**不含** limit）。 */
function countMemories(db, options = {}) {
	const { clause, params } = listWhere(db, options);
	try {
		const row = db.prepare(`SELECT COUNT(*) AS n FROM code_index${clause}`).get(...params);
		return Number(row?.n ?? 0);
	} catch {
		return 0;
	}
}
/** 列出模式：无 query 时按 项目（含别名展开）+ 标签 过滤，时间倒序。 */
function listMemories(db, options = {}) {
	const limit = Math.max(1, Math.trunc(options.limit ?? 20));
	const { clause, params } = listWhere(db, options);
	const sql = `SELECT code,title,project,mem_type,status,file_path,dormant FROM code_index${clause} ORDER BY recorded_at DESC, code DESC LIMIT ?`;
	return db.prepare(sql).all(...params, limit).map((row) => [
		row.code,
		row.title ?? "",
		row.project ?? null,
		row.mem_type ?? "",
		row.status ?? "",
		row.file_path ?? null,
		Number(row.dormant ?? 0),
		LIST_SOURCE
	]);
}
/** 列出模式回包的首行声明：说清这次按什么过滤、共几条。 */
function formatListDeclaration(input) {
	const conds = [];
	if (input.project !== null && input.project !== void 0 && input.project !== "") conds.push(`project=${input.project}`);
	const tags = normalizeTags(input.tags);
	if (tags.length > 0) conds.push(`tags=${tags.join("、")}`);
	let head = `已按 ${conds.length > 0 ? conds.join("、") : "无过滤"} 过滤，共 ${input.total} 条（时间倒序`;
	if (input.shown < input.total) head += `，仅列前 ${input.shown} 条`;
	return `${head}）`;
}
/** 状态列的唯一渲染点：`[类型/状态]`，休眠时合并成 `[类型/状态+dormant]`。 */
function formatStatusTag(memType, status, dormant) {
	return `[${memType}/${status}${dormant ? "+dormant" : ""}]`;
}
/**
* 影子期的 FTS 自愈闸门（替代 Python 的 `ensure_fts`）。
*
* Python 在"索引空、记忆非空"时会**自动重建**（写路径）。影子期 TS 只有 `mode=ro`，
* 写不了——所以这里**响亮失败**并给出该跑的命令，而不是"静默少召回"：
* 少召回的表现是"查不到"，与"本来就没有"长得一模一样。
*/
function ensureFtsReadable(db) {
	let have = 0;
	try {
		const row = db.prepare("SELECT COUNT(*) AS n FROM memory_fts").get();
		have = Number(row?.n ?? 0);
	} catch {
		return;
	}
	const needRow = db.prepare("SELECT COUNT(*) AS n FROM code_index").get();
	const need = Number(needRow?.n ?? 0);
	if (need > 0 && have === 0) throw new FishError("SHADOW_READ_ONLY", "全文索引为空而记忆非空：需要 `fish reindex` 重建（影子期只读，不能代劳）", {
		hint: "先跑 `fish reindex`（Python 侧写路径），再重试；或确认这不是一个空索引的新库",
		details: {
			memories: need,
			fts: have
		}
	});
}
/** `recall` 的完整回执（逐行对齐 MCP `tool_recall`，mcp_server.py:819）。 */
function renderRecall(db, options = {}, identity = IDENTITY_ANONYMOUS) {
	const query = (options.query ?? "").trim();
	const code = options.code ?? null;
	const project = options.project ?? null;
	const tags = options.tags ?? null;
	const includeSuperseded = options.includeSuperseded === true;
	/** 所有出口都过这一处：MCP 的归属提示是**统一追加**的，漏一个分支就少一整段。 */
	const finish = (text) => appendIdentityHint(text, identity, "recall");
	if (query === "" && (code === null || code === "")) {
		if (project === null || project === void 0 || project === "") {
			if (normalizeTags(tags).length > 0) return finish(`${TAGS_ONLY_HINT}\n`);
			throw new FishError("COMMAND_USAGE", "query / code / project 至少要给一个", { hint: "例：/fish shadow recall 协作约定｜/fish shadow recall --project anyi-studio" });
		}
		const rows = listMemories(db, {
			project,
			tags,
			limit: Math.trunc(options.limit ?? 20),
			includeSuperseded
		});
		const total = countMemories(db, {
			project,
			tags,
			includeSuperseded
		});
		const head = formatListDeclaration({
			project,
			tags,
			shown: rows.length,
			total
		});
		if (rows.length === 0) return finish(`${head}\n（该项目下没有符合筛选条件的条目；${normalizeTags(tags).length > 0 ? "去掉 tags 再列一次，或换 project" : "该项目下还没有记忆（可换 project，或先 remember 一条）"}）\n`);
		const lines = [head, RECALL_DIMENSION_NOTE];
		for (const row of rows) {
			const [c, t, p, m, s, , d, src] = row;
			lines.push(`- ${c} ${formatStatusTag(m, s, d)} ${t}  ← 匹配:${src}｜项目:${p ?? ""}｜正文用 read_body`);
		}
		return finish(`${lines.join("\n")}\n`);
	}
	ensureFtsReadable(db);
	const { rows } = searchMemories(db, {
		query,
		code,
		project,
		tags,
		scope: options.scope ?? "all",
		limit: options.limit ?? 10,
		includeSuperseded
	});
	if (rows.length === 0) {
		const label = query !== "" ? query : code ?? project ?? "";
		const candidates = recentFallback(db, {
			project,
			tags
		});
		let head = `未找到「${label}」`;
		if (project !== null && project !== void 0 && project !== "") head += `（已限定 project=${project}）`;
		return finish(`${head}。最近的条目：\n${candidates.map((row) => `- ${row.code} ${row.title}`).join("\n")}\n`);
	}
	const lines = [RECALL_DIMENSION_NOTE, RECALL_KEYWORD_NOTE];
	for (const row of rows) {
		const [c, t, , m, s, , d, src] = row;
		lines.push(`- ${c} ${formatStatusTag(m, s, d)} ${t}  ← 匹配:${src}｜正文用 read_body`);
	}
	return finish(`${lines.join("\n")}\n`);
}
//#endregion
//#region src/domain/presence.ts
/** 在场行的缺省条数。 */
const PRESENCE_LIMIT_DEFAULT = 20;
/** 动态的缺省条数（"最近几条"——面板只画最近这一段）。 */
const BOARD_POSTS_LIMIT_DEFAULT = 5;
/** 上限归一：非法值**响亮失败**（`presenceLimit=0` 不许静默变成默认值）。 */
function normalizeLimit(value, fallback, name) {
	if (value === void 0) return fallback;
	if (!Number.isInteger(value) || value <= 0) throw new FishError("COMMAND_USAGE", `${name} 必须是正整数，收到 ${String(value)}`, { hint: `例如 ${name}=${fallback}` });
	return value;
}
/**
* 板过滤条件（两条 SQL 共用一份口径：只认 `boards` 的两列）。
*
* 为什么拼字符串而不在两条查询里各写一遍 `if (scope)`：那是会漂开的两份真相
* （一个忘了带 period 就是"多读了别人的板"）。条件是**枚举出来的固定片段**，值一律走占位符。
*/
function boardFilter(options) {
	const conditions = [];
	const params = [];
	const scope = (options.scope ?? "").trim();
	if (scope !== "") {
		conditions.push("b.scope = ?");
		params.push(scope);
	}
	const periodKey = (options.periodKey ?? "").trim();
	if (periodKey !== "") {
		conditions.push("b.period_key = ?");
		params.push(periodKey);
	}
	return {
		sql: conditions.length === 0 ? "" : `WHERE ${conditions.join(" AND ")}`,
		params
	};
}
/**
* SQL ①：谁在场。
*
* 列名一律显式起别名：`node:sqlite` 的 `all()` 返回**对象**，裸列名拿不到值（T02 踩过一次）。
*/
function presenceRows(db, options = {}) {
	const filter = boardFilter(options);
	const limit = normalizeLimit(options.presenceLimit, 20, "presenceLimit");
	const sql = `SELECT p.citizen_id AS citizen_id, COALESCE(c.display_name, '') AS display_name, COALESCE(c.host, '') AS host, p.session_key AS session_key, p.status AS status, COALESCE(p.doing, '') AS doing, COALESCE(p.surface, '') AS surface, p.last_seen_at AS last_seen_at, b.scope AS scope, b.scope_key AS scope_key, b.period_key AS period_key FROM presence p LEFT JOIN citizens c ON c.citizen_id = p.citizen_id JOIN boards b ON b.id = p.board_id ${filter.sql === "" ? "" : `${filter.sql} `}ORDER BY p.last_seen_at DESC LIMIT ?`;
	return db.prepare(sql).all(...filter.params, limit);
}
/** SQL ②：最近几条动态（按 `id` 倒序——最新的在最上面，符合"此刻"的语义）。 */
function recentBoardPosts(db, options = {}) {
	const filter = boardFilter(options);
	const limit = normalizeLimit(options.postsLimit, 5, "postsLimit");
	const sql = `SELECT bp.citizen_id AS citizen_id, bp.kind AS kind, bp.text AS text, bp.created_at AS created_at, b.scope AS scope, b.scope_key AS scope_key, b.period_key AS period_key FROM board_posts bp JOIN boards b ON b.id = bp.board_id ${filter.sql === "" ? "" : `${filter.sql} `}ORDER BY bp.id DESC LIMIT ?`;
	return db.prepare(sql).all(...filter.params, limit);
}
/**
* 打开库 → 自证只读 → 两条 SELECT → 关库。面板侧的**唯一**入口。
*
* 顺序有意如此：先自证"写不进去"再读——自证失败时不该继续把数据递给面板。
* @throws DatabaseError `DB_UNAVAILABLE` —— 打不开（路径不存在 / 被独占锁住 / 不是 SQLite）
* @throws FishError `COMMAND_USAGE` —— 上限不是正整数
*/
function readPresencePanel(dbPath, options = {}) {
	const db = openReadOnly(dbPath);
	try {
		return {
			readOnly: true,
			writeRejected: assertWriteRejected(db),
			presence: presenceRows(db, options),
			posts: recentBoardPosts(db, options)
		};
	} finally {
		db.close();
	}
}
/** 面板数据的人读形态（`/fish shadow presence` 与判据脚本共用同一份渲染）。 */
function renderPresencePanel(panel) {
	const lines = [`面板数据源（只读两条 SELECT）：在场 ${panel.presence.length} 行 / 动态 ${panel.posts.length} 行`, `只读自证：${panel.writeRejected ? "写入被 SQLite 拒绝（正常）" : "⚠ 写入竟然成功——立刻停用插件"}`];
	if (panel.presence.length === 0) lines.push("", "在场：**还没有人签到**（`presence` 里没有行；未登记 = 未发生）");
	else {
		lines.push("", "在场（按 last_seen_at 倒序）：");
		for (const row of panel.presence) {
			const doing = row.doing.trim() === "" ? "（未写 doing）" : row.doing;
			const board = row.scope === "root" ? "根" : `${row.scope}:${row.scope_key === "" ? "（空）" : row.scope_key}`;
			const who = row.display_name === "" ? "（未填显示名）" : row.display_name;
			lines.push(`  ${who}（${row.citizen_id}）· ${board} · ${row.status} · ${doing}`);
			if (row.surface.trim() !== "") lines.push(`    声明占用面：${row.surface}`);
			lines.push(`    最后活跃：${row.last_seen_at}`);
		}
	}
	if (panel.posts.length === 0) lines.push("", "动态：最近没有动态");
	else {
		lines.push("", "动态（最新在上）：");
		for (const post of panel.posts) lines.push(`  ${post.created_at}  ${post.citizen_id}  [${post.kind}] ${post.text}`);
	}
	lines.push("", "口径：不做重叠判定 / 残留行 / 可读名消歧 / 自动面聚合（原文原样给人看）。");
	return lines.join("\n");
}
//#endregion
//#region src/domain/kb.ts
/**
* 知识库只读面（`p0/fish_kb.py` 的 tree / open / search / stats / vault list）。
*
* ## 影子期的三条硬边界（与生产语义逐条对齐，不是"简化版"）
*
*   1. **只读不索引**：知识库内容永不进 `memory_fts` / `recall` / 注入包；`search` 是
*      "按需扫描"，回执必须写明"未建索引"——它不是第二个 recall。
*   2. **忽略规则是硬边界**：`.obsidian/**`、`.trash/**`、`90-模板/**` 是**强制项**，
*      判定层每次合并（`effectiveGlobs`），删不掉。判定对象是"路径本身 + 每一级祖先目录"，
*      且先做逐段归一（大小写 + 尾随点/空格）再匹配——Windows 会吃掉组件尾随的空格，
*      不归一会让 `.obsidian /app.json` 绕过禁读。
*   3. **敏感面默认拒读**：命中内置名单（凭证/密钥/.env…）只给"文件名 + 大小 + 建议"，
*      正文要显式二次授权（`allowSensitive`）。判定同时看**字面串**与**解析后落点**。
*
* ## 与生产的一处**有意差异**（影形期纪律，登记在 NOTES-T05 §5）
*
* 生产 `kb_tree` / `kb_open` / `kb_search` 每次读都会往 `kb_access` 追一条审计
* （`p0/fish_kb.py:1643`、`:1826`、`:2083`，词表见 `KB_ACTIONS`）。影子期
* **一个字节都不写**，所以本模块**没有** `kbAccessLog` 的实现——不是漏了，是不能有。
* 代价：DSH 里的读取不会出现在"未读/读次数"里；收益：读命令永远不可能改库。
*
* ## 为什么不复用 node:sqlite 的 `readOnly` 之外的东西
*
* `kb_access` 的查询走调用方传进来的只读连接（`core/db.ts` 的 `openReadOnly`，
* 带 `query_only=1`）；文件读取一律 `readFileSync`。整个模块没有写 API。
*
* @module dsh-fish/domain/kb
*/
/** 强制忽略项（`KB_MANDATORY_DENY`）：判定层每次合并，用户删不掉。 */
const KB_MANDATORY_DENY = [
	".obsidian/**",
	".trash/**",
	"90-模板/**"
];
/** 敏感名单（`KB_SENSITIVE_PATTERNS`）：命中即"要显式二次授权"才能读正文。 */
const KB_SENSITIVE_PATTERNS = [
	"*凭证*",
	"*credential*",
	"*secret*",
	"*密码*",
	"*.pem",
	"*.key",
	"id_rsa*",
	".env*"
];
/** 上表里唯一需要负向断言的那条（`密码学` 是正常笔记）。 */
const KB_PASSWORD_PATTERN = "*密码*";
const KB_PASSWORD_RE = /密码(?!学)/u;
/** 媒体扩展名黑名单（改名成 `.md` 也拦得住——魔数双判的第二重）。 */
const KB_MEDIA_EXTS = /* @__PURE__ */ new Set([
	".mp4",
	".mov",
	".mkv",
	".avi",
	".webm",
	".png",
	".jpg",
	".jpeg",
	".webp",
	".gif",
	".bmp",
	".tif",
	".tiff",
	".svgz",
	".mp3",
	".wav",
	".flac",
	".m4a",
	".ogg",
	".zip",
	".7z",
	".rar",
	".gz",
	".tar",
	".pdf",
	".docx",
	".xlsx",
	".pptx",
	".bin",
	".exe",
	".dll",
	".so",
	".dylib",
	".iso"
]);
/** 首 8 KB 的魔数表（`(签名, 偏移, 名字)`）。 */
const KB_MAGIC_SIGNATURES = [
	{
		signature: Buffer.from([
			137,
			80,
			78,
			71,
			13,
			10,
			26,
			10
		]),
		offset: 0,
		name: "png"
	},
	{
		signature: Buffer.from([
			255,
			216,
			255
		]),
		offset: 0,
		name: "jpeg"
	},
	{
		signature: Buffer.from("GIF8", "ascii"),
		offset: 0,
		name: "gif"
	},
	{
		signature: Buffer.from("%PDF", "ascii"),
		offset: 0,
		name: "pdf"
	},
	{
		signature: Buffer.from([
			80,
			75,
			3,
			4
		]),
		offset: 0,
		name: "zip"
	},
	{
		signature: Buffer.from([
			55,
			122,
			188,
			175,
			39,
			28
		]),
		offset: 0,
		name: "7z"
	},
	{
		signature: Buffer.from([
			82,
			97,
			114,
			33,
			26,
			7
		]),
		offset: 0,
		name: "rar"
	},
	{
		signature: Buffer.from("ID3", "ascii"),
		offset: 0,
		name: "mp3"
	},
	{
		signature: Buffer.from("fLaC", "ascii"),
		offset: 0,
		name: "flac"
	},
	{
		signature: Buffer.from("OggS", "ascii"),
		offset: 0,
		name: "ogg"
	},
	{
		signature: Buffer.from([
			127,
			69,
			76,
			70
		]),
		offset: 0,
		name: "elf"
	},
	{
		signature: Buffer.from("MZ", "ascii"),
		offset: 0,
		name: "exe"
	},
	{
		signature: Buffer.from("SQLite format 3\0", "latin1"),
		offset: 0,
		name: "sqlite"
	},
	{
		signature: Buffer.from("WEBP", "ascii"),
		offset: 8,
		name: "webp"
	},
	{
		signature: Buffer.from("ftyp", "ascii"),
		offset: 4,
		name: "mp4"
	}
];
/** 单文件上限（Q11：2 MiB，可按 vault 配）。 */
const KB_DEFAULT_MAX_OPEN_BYTES = 2097152;
/** 检索时单文件上限（超过跳过并计数）。 */
const KB_SEARCH_MAX_FILE_BYTES = 2097152;
/** tree 每个目录最多列 20 项。 */
const KB_MAX_ITEMS_PER_DIR = 20;
/** tree 深度上限（更深请显式给 path）。 */
const KB_TREE_MAX_DEPTH = 4;
/** search 结果条数上限。 */
const KB_MAX_LIMIT = 30;
/** tree 的深度口径说明（**权威定义在这里**，与生产同一句话）。 */
const KB_TREE_DEPTH_RULE = "> 深度口径（唯一权威）：depth=N = **显示到第 N 层目录**，起点为第 0 层；每个被显示的目录都列出它的直接 `.md` 文件（相对库根的完整路径，可直接喂给 `kb open`）";
/** 知识库域错误（`code` 与生产 `KBError` 的词表逐字对齐，回执按生产格式渲染）。 */
var KbError = class extends Error {
	code;
	hint;
	constructor(code, message, hint) {
		super(message);
		this.name = "KbError";
		this.code = code;
		this.hint = hint;
	}
	/** 与 Python `KBError.__str__` 同形（含"下一步"两行）。 */
	format() {
		return this.hint === void 0 ? this.message : `${this.message}\n  下一步：${this.hint}`;
	}
};
/** `kb_vault_get` 的 SELECT 列序（与 `_KB_VAULT_COLS` 一致）。 */
const KB_VAULT_SELECT = "SELECT vault_id, name, path, path_key, mode, ignore_globs, max_open_bytes, is_default, last_scan_at, last_scan_files, last_scan_notes, last_scan_bytes, note, status, created_at, updated_at FROM kb_vault";
/** Windows 上 `normcase` = 小写（其它平台不改）。 */
function normCase(text) {
	return process.platform === "win32" ? text.toLowerCase() : text;
}
/** 忽略判定的单段归一：大小写 + 尾随点/空格（只用于判定，不改落点）。 */
function judgeComponent(part) {
	return normCase(String(part ?? "").replace(/[. ]+$/u, ""));
}
/**
* Python `fnmatch.fnmatchcase` 的等价实现。
*
* `*` 匹配任意串（**包括 `/`**——生产就是拿 `fnmatch` 比整条路径的，不是 `pathlib.match`）；
* `?` 匹配一个字符；`[seq]` 是字符类。大小写敏感（归一由调用方先做）。
*/
function fnmatchCase(name, pattern) {
	let source = "";
	for (let i = 0; i < pattern.length; i += 1) {
		const ch = pattern[i];
		if (ch === "*") source += ".*";
		else if (ch === "?") source += ".";
		else if (ch === "[") {
			let j = i + 1;
			if (j < pattern.length && (pattern[j] === "!" || pattern[j] === "^")) j += 1;
			if (j < pattern.length && pattern[j] === "]") j += 1;
			while (j < pattern.length && pattern[j] !== "]") j += 1;
			if (j >= pattern.length) source += "\\[";
			else {
				const stuff = pattern.slice(i + 1, j);
				const body = stuff.startsWith("!") ? `^${stuff.slice(1)}` : stuff;
				source += `[${body.replace(/\\/gu, "\\\\")}]`;
				i = j;
			}
		} else source += ch.replace(/[.*+?^${}()|[\]\\]/gu, "\\$&");
	}
	return new RegExp(`^${source}$`, "u").test(name);
}
/** **生效**忽略规则 = 强制项 ∪ 行内规则（去重、保序）。 */
function effectiveGlobs(globs) {
	const out = [...KB_MANDATORY_DENY];
	for (const raw of globs ?? []) {
		const text = String(raw ?? "");
		if (text !== "" && !out.includes(text)) out.push(text);
	}
	return out;
}
/**
* 命中忽略规则 → 返回**命中的那条规则**（没命中 → `null`）。
*
* 匹配对象 = "路径本身 + 每一级祖先目录（补 `/`）"：`.trash/**` 要能覆盖
* `.trash/a/b.md`，"任意深度的 _media 目录"这类规则也要能覆盖；而 `.obsidian/**` 既要挡住里面的文件，
* 也要挡住**这个目录本身**（否则 tree 会列出空目录节点）。
*/
function kbIgnored(relPosix, globs) {
	const trimmed = String(relPosix ?? "").replace(/\\/gu, "/").replace(/^\/+|\/+$/gu, "");
	if (trimmed === "") return null;
	const parts = trimmed.split("/").filter((part) => part !== "").map((part) => judgeComponent(part));
	if (parts.length === 0) return null;
	const rel = parts.join("/");
	const candidates = [rel, `${rel}/`];
	for (let i = 1; i < parts.length; i += 1) candidates.push(`${parts.slice(0, i).join("/")}/`);
	for (const pattern of effectiveGlobs(globs)) {
		const rawPattern = String(pattern ?? "");
		if (rawPattern === "") continue;
		const patNorm = rawPattern.replace(/\\/gu, "/").split("/").filter((part) => part !== "").map((part) => judgeComponent(part)).join("/");
		for (const candidate of candidates) if (fnmatchCase(candidate, patNorm) || fnmatchCase(rel, patNorm)) return rawPattern;
	}
	return null;
}
/** 路径命中内置敏感名单（**逐段**判，大小写不敏感）。 */
function kbIsSensitiveName(rel) {
	const parts = String(rel ?? "").replace(/\\/gu, "/").split("/").filter((part) => part !== "" && part !== "." && part !== "..");
	for (const part of parts) {
		const name = part.toLowerCase();
		for (const pattern of KB_SENSITIVE_PATTERNS) {
			if (pattern === KB_PASSWORD_PATTERN) continue;
			if (fnmatchCase(name, pattern.toLowerCase())) return true;
		}
		if (KB_PASSWORD_RE.test(name)) return true;
	}
	return false;
}
const KB_DEVICE_NAMES = /* @__PURE__ */ new Set([
	"con",
	"prn",
	"aux",
	"nul",
	...Array.from({ length: 9 }, (_v, i) => `com${i + 1}`),
	...Array.from({ length: 9 }, (_v, i) => `lpt${i + 1}`)
]);
/** `CON` / `NUL` / `COM1` / `CON.md` 全都是设备名（Windows 只看主干名）。 */
function kbIsDeviceName(part) {
	const stem = String(part ?? "").split(".")[0]?.trim().toLowerCase();
	return KB_DEVICE_NAMES.has(stem ?? "");
}
/** 判断一个路径分量是不是链接/联接点（遍历面：不列、不下降）。 */
function isReparsePoint(absPath) {
	try {
		return lstatSync(absPath).isSymbolicLink();
	} catch {
		return false;
	}
}
/** 逐级走到 target，返回第一个 reparse point 的路径（没有 → `null`）。 */
function firstReparse(realRoot, target) {
	const relative = path.relative(realRoot, target);
	if (relative === "") return null;
	if (relative.startsWith("..") || path.isAbsolute(relative)) return String(target);
	const parts = relative.split(path.sep).filter((part) => part !== "");
	let current = realRoot;
	for (const part of parts) {
		current = path.join(current, part);
		if (!existsSync(current)) return null;
		if (isReparsePoint(current)) return current;
	}
	return null;
}
/**
* 把"vault 内相对路径"解析成绝对路径，途中做全部拒绝。
*
* 拒绝清单（判 `code` 不判文案）：`..` / 绝对 / 盘符 / UNC / `\\?\` / 冒号(ADS) /
* 设备名 / NUL / 忽略目录 / 越界 / 链接 / 不存在。
*/
function kbResolveTarget(vault, rel, forWrite = false) {
	const root = vault.path;
	const rawText = rel === null || rel === void 0 ? "" : String(rel);
	if (rawText.includes("\0")) throw new KbError("path_invalid", "路径含 NUL 字符（\\x00），拒绝处理", "路径里不可能有 NUL；检查调用方是不是把二进制/拼接串当路径了");
	const raw = rawText.trim();
	const realRoot = realpathSync(root);
	if (raw === "") {
		if (forWrite) throw new KbError("path_invalid", "写路径不能为空", "给 vault 内相对路径，如 20-PPT/x.md");
		return {
			abs: realRoot,
			rel: "",
			realRoot,
			resolvedRel: "",
			sensitive: false
		};
	}
	if (raw.startsWith("/") || raw.startsWith("\\")) {
		if (raw.startsWith("\\\\?\\") || raw.startsWith("\\\\.\\")) throw new KbError("path_invalid", `不接受设备路径（\\\\?\\ …）：${raw}`, "给 vault 内相对路径，用 / 分隔");
		if (raw.startsWith("\\\\") || raw.startsWith("//")) throw new KbError("path_invalid", `不接受 UNC 路径：${raw}`, "给 vault 内相对路径，用 / 分隔");
		throw new KbError("path_invalid", `不接受绝对路径：${raw}`, "给 vault 内相对路径，用 / 分隔");
	}
	if (/^[A-Za-z]:/u.test(raw)) throw new KbError("path_invalid", `不接受盘符：${raw}`, "给 vault 内相对路径，如 20-PPT/x.md");
	const parts = raw.replace(/\\/gu, "/").split("/").filter((part) => part !== "" && part !== ".");
	if (parts.some((part) => part === "..")) throw new KbError("path_invalid", `路径含 \`..\` 穿越：${raw}`, "vault 内相对路径不允许向上跳");
	for (const part of parts) {
		if (part.includes(":")) throw new KbError("path_invalid", `路径含冒号（Windows 备用数据流 ADS）：${part}`, "文件名里不能有 `:`；ADS 形态（x.md:hidden）一律拒绝");
		if (kbIsDeviceName(part)) throw new KbError("path_invalid", `拒绝 Windows 设备名：${part}`, "CON/NUL/COM1 这类名字指向设备，不是库里的文件");
	}
	const relPosix = parts.join("/");
	if (relPosix === "") throw new KbError("path_invalid", `路径归一后为空：${raw}`, "给 vault 内相对路径，如 20-PPT/x.md");
	const hit = kbIgnored(relPosix, vault.ignoreGlobs);
	if (hit !== null) throw new KbError("ignored", `该路径按约定忽略：${relPosix}（规则 ${hit}）`, "换个路径；忽略规则用 `fish kb vault add --ignore` 配置");
	const lexical = path.join(realRoot, ...parts);
	const target = existsSync(lexical) ? realpathSync(lexical) : path.resolve(lexical);
	const relative = path.relative(realRoot, target);
	if (!(relative !== "" ? !relative.startsWith("..") && !path.isAbsolute(relative) : true)) throw new KbError("outside_vault", `越出知识库根：${relPosix}`, "只允许 vault 内的相对路径");
	const resolvedRel = relative.split(path.sep).join("/");
	const resolvedHit = kbIgnored(resolvedRel, vault.ignoreGlobs);
	if (resolvedHit !== null) throw new KbError("ignored", `该路径解析后落在忽略目录：${relPosix}` + (resolvedRel !== relPosix ? `（实际落点 ${resolvedRel}）` : "") + `（规则 ${resolvedHit}）`, "换个路径；忽略规则（含内置强制项）对解析后的真实落点同样生效");
	const sensitive = kbIsSensitiveName(resolvedRel) || kbIsSensitiveName(relPosix);
	const bad = firstReparse(realRoot, lexical);
	if (bad !== null) throw new KbError("reparse_point", `路径含链接/联接点：${bad}（已拒绝）`, "Obsidian vault 内不应有 junction；换成真实目录");
	if (!existsSync(target)) {
		if (!forWrite) throw new KbError("not_found", `知识库里没有：${relPosix}`, "fish kb tree 看目录，或用完整相对路径");
		const parent = path.dirname(lexical);
		if (parent !== realRoot && !existsSync(parent)) throw new KbError("not_found", `父目录不存在：${parent}`, "P0 只写已存在目录内（不自动建目录）");
		const parentBad = firstReparse(realRoot, parent);
		if (parentBad !== null) throw new KbError("reparse_point", `父目录链含链接/联接点：${parentBad}（已拒绝）`);
	}
	return {
		abs: target,
		rel: relPosix,
		realRoot,
		resolvedRel,
		sensitive
	};
}
/** 判"这是不是可以读的文本"（扩展名 + 首 8 KB 魔数双判）。 */
function kbDetectText(data, ext = "") {
	const suffix = String(ext ?? "").toLowerCase();
	if (suffix !== "" && KB_MEDIA_EXTS.has(suffix)) return {
		isText: false,
		encoding: null,
		media: true,
		reason: `extension:${suffix}`
	};
	const head = data.subarray(0, 8192);
	for (const { signature, offset, name } of KB_MAGIC_SIGNATURES) {
		const slice = head.subarray(offset, offset + signature.length);
		if (slice.length === signature.length && slice.equals(signature)) return {
			isText: false,
			encoding: null,
			media: true,
			reason: `magic:${name}`
		};
	}
	if (head.includes(0)) return {
		isText: false,
		encoding: null,
		media: true,
		reason: "nul_byte"
	};
	let encoding = null;
	const hasBom = data.length >= 3 && data[0] === 239 && data[1] === 187 && data[2] === 191;
	try {
		new TextDecoder("utf-8", { fatal: true }).decode(data);
		encoding = hasBom ? "utf-8-sig" : "utf-8";
	} catch {
		try {
			const withoutBom = hasBom ? data.subarray(3) : data;
			new TextDecoder("utf-8", { fatal: true }).decode(withoutBom);
			encoding = "utf-8-sig";
		} catch {
			try {
				new TextDecoder("gb18030", { fatal: true }).decode(data);
				encoding = "gb18030";
			} catch {
				encoding = null;
			}
		}
	}
	if (encoding === null) return {
		isText: false,
		encoding: null,
		media: false,
		reason: "bad_encoding"
	};
	const sample = new TextDecoder(encoding === "utf-8-sig" ? "utf-8" : encoding, { fatal: false }).decode(head);
	let controls = 0;
	for (const ch of sample) if ((ch.codePointAt(0) ?? 0) < 32 && ch !== "	" && ch !== "\n" && ch !== "\r") controls += 1;
	if (sample !== "" && controls / codePointLength(sample) > .05) return {
		isText: false,
		encoding: null,
		media: true,
		reason: "binary_ratio"
	};
	return {
		isText: true,
		encoding,
		media: false,
		reason: null
	};
}
/** 判换行风格（按**字节**判，解码前就能用；只取前 64 KB）。 */
function kbDetectEol(data) {
	const head = data.subarray(0, 65536);
	let crlf = 0;
	let lf = 0;
	for (let i = 0; i < head.length; i += 1) if (head[i] === 10) {
		lf += 1;
		if (i > 0 && head[i - 1] === 13) crlf += 1;
	}
	return crlf > lf - crlf ? "crlf" : "lf";
}
/** 从 `startAbs` 起遍历 vault（跳过忽略项、跳过链接），产出 `{rel,abs,isDir,size,mtime}`。 */
function* kbWalk(vault, startAbs) {
	const root = realpathSync(vault.path);
	const stack = [startAbs];
	while (stack.length > 0) {
		const current = stack.pop();
		let names;
		try {
			names = readdirSync(current).sort();
		} catch {
			continue;
		}
		for (const name of names) {
			const absPath = path.join(current, name);
			const relative = path.relative(root, absPath);
			if (relative.startsWith("..") || path.isAbsolute(relative)) continue;
			if (isReparsePoint(absPath)) continue;
			let isDir = false;
			try {
				isDir = lstatSync(absPath).isDirectory();
			} catch {
				isDir = false;
			}
			const rel = relative.split(path.sep).join("/");
			if (kbIgnored(isDir ? `${rel}/` : rel, vault.ignoreGlobs) !== null) continue;
			let size = 0;
			let mtime = 0;
			if (!isDir) try {
				const stat = lstatSync(absPath);
				size = stat.size;
				mtime = stat.mtimeMs / 1e3;
			} catch {
				size = 0;
				mtime = 0;
			}
			yield {
				rel,
				abs: absPath,
				isDir,
				size,
				mtime
			};
			if (isDir) stack.push(absPath);
		}
	}
}
const KB_FIT_MARKER_RE = /\n?…（(?:已按字符预算截断|已截断)）\s*$/u;
/** 按**双上限**截断（先字符、后 token），并说明截断原因。 */
function kbFitText(lines, maxChars, maxTokens) {
	const charsCap = Number.isFinite(maxChars) ? Math.trunc(maxChars) : 0;
	const tokenCap = Number.isFinite(maxTokens) ? Math.trunc(maxTokens) : 0;
	let text = lines.join("\n");
	const reasons = [];
	if (charsCap !== 0 && codePointLength(text) > charsCap) {
		text = `${sliceCodePoints(text, 0, Math.max(charsCap - 20, 0))}\n…（已按字符预算截断）`;
		reasons.push("chars");
	}
	if (tokenCap !== 0) {
		const marker = "\n…（已截断）";
		if (countTokens(text) > tokenCap) {
			const budget = tokenCap - countTokens(marker);
			if (budget <= 0) text = "";
			else {
				const kept = text.split("\n");
				while (kept.length > 0 && countTokens(kept.join("\n")) > budget) kept.pop();
				text = kept.length > 0 ? `${kept.join("\n")}${marker}` : "";
			}
			reasons.push("tokens");
		}
	}
	if (charsCap !== 0 && codePointLength(text) > charsCap) text = sliceCodePoints(text, 0, charsCap);
	return {
		text,
		chars: codePointLength(text),
		tokens: countTokens(text),
		truncated: reasons.length > 0,
		truncatedReasons: reasons
	};
}
/** 两个上限**同时**成立？ */
function kbWithinBudget(text, maxChars, maxTokens) {
	if (maxChars !== 0 && codePointLength(text) > maxChars) return false;
	if (maxTokens !== 0 && countTokens(text) > maxTokens) return false;
	return true;
}
/** 按双预算截断，并把 `tail` **钉在最后一行**（截断了就告诉你怎么深入）。 */
function kbFitTextWithTail(lines, tail, maxChars, maxTokens, shortTail = "") {
	const base = kbFitText(lines, maxChars, maxTokens);
	const tailText = String(tail ?? "").trim();
	if (tailText === "" || !base.truncated) return base;
	const short = String(shortTail ?? "").trim();
	const assemble = (body, sentence) => body !== "" ? `${body}\n${sentence}` : sentence;
	let text = String(base.text ?? "").replace(KB_FIT_MARKER_RE, "");
	let sentence = tailText;
	while (!kbWithinBudget(assemble(text, sentence), maxChars, maxTokens)) {
		const kept = text !== "" ? text.split("\n") : [];
		if (kept.length > 0) {
			text = kept.slice(0, -1).join("\n");
			continue;
		}
		if (short !== "" && sentence !== short) {
			sentence = short;
			continue;
		}
		sentence = kbFitText([short !== "" ? short : tailText], maxChars, maxTokens).text;
		break;
	}
	const candidate = assemble(text, sentence);
	return {
		text: candidate,
		chars: codePointLength(candidate),
		tokens: countTokens(candidate),
		truncated: true,
		truncatedReasons: [...base.truncatedReasons]
	};
}
/** `_kb_vault_dict`：行 → `KbVaultRow`（`ignore_globs` 解析成 list）。 */
function toVaultRow(row) {
	const rawGlobs = row.ignore_globs;
	let globs = [];
	if (Array.isArray(rawGlobs)) globs = rawGlobs.map((item) => String(item));
	else {
		const text = String(rawGlobs ?? "").trim();
		if (text !== "") {
			let parsed;
			try {
				parsed = JSON.parse(text);
			} catch {
				throw new KbError("bad_usage", `ignore_globs 不是合法 JSON：${text.slice(0, 60)}`);
			}
			if (!Array.isArray(parsed)) throw new KbError("bad_usage", `ignore_globs 必须是 JSON 数组，收到 ${typeof parsed}`);
			globs = parsed.map((item) => String(item));
		}
	}
	const mode = String(row.mode ?? "ro");
	return {
		vaultId: String(row.vault_id ?? ""),
		name: String(row.name ?? ""),
		path: String(row.path ?? ""),
		mode: mode === "rw" ? "rw" : "ro",
		ignoreGlobs: globs,
		maxOpenBytes: Number(row.max_open_bytes ?? 2097152),
		isDefault: Boolean(row.is_default),
		lastScanAt: row.last_scan_at === null || row.last_scan_at === void 0 ? null : String(row.last_scan_at),
		lastScanFiles: row.last_scan_files === null || row.last_scan_files === void 0 ? null : Number(row.last_scan_files),
		lastScanNotes: row.last_scan_notes === null || row.last_scan_notes === void 0 ? null : Number(row.last_scan_notes),
		lastScanBytes: row.last_scan_bytes === null || row.last_scan_bytes === void 0 ? null : Number(row.last_scan_bytes),
		note: row.note === null || row.note === void 0 ? null : String(row.note),
		status: String(row.status ?? "active")
	};
}
/** 全部 active vault（默认库在前，其次按名字）。 */
function kbVaultList(db) {
	return db.prepare(`${KB_VAULT_SELECT} WHERE status='active' ORDER BY is_default DESC, name`).all().map((row) => toVaultRow(row));
}
/** 按 name / vault_id / 缺省（默认库）取一条；取不到 → `KbError('vault_missing')`。 */
function kbVaultGet(db, key) {
	const text = String(key ?? "").trim();
	if (text === "") {
		const row = db.prepare(`${KB_VAULT_SELECT} WHERE is_default=1 AND status='active'`).get();
		if (row === void 0) throw new KbError("vault_missing", "还没有默认知识库", "fish kb vault add --name <名字> --path <绝对路径> --default");
		return toVaultRow(row);
	}
	const row = db.prepare(`${KB_VAULT_SELECT} WHERE (vault_id=? OR name=?) ORDER BY is_default DESC LIMIT 1`).get(text, text);
	if (row === void 0) throw new KbError("vault_missing", `没有这个知识库：${text}`, "fish kb vault list 看已登记的库");
	const vault = toVaultRow(row);
	if (vault.status !== "active") throw new KbError("vault_missing", `知识库已停用：${text}`, "fish kb vault add 重新登记，或换一个库");
	return vault;
}
/** vault 摘要（回执头用）。 */
function vaultBrief(vault) {
	return {
		name: vault.name,
		id: vault.vaultId,
		mode: vault.mode,
		path: vault.path
	};
}
/** `_kb_fmt_mtime`：epoch 秒 → 本地 ISO；空/坏 → `—`。 */
function formatMtime(ts) {
	if (ts === null || ts === void 0 || ts === 0 || Number.isNaN(ts)) return "—";
	return isoLocal(/* @__PURE__ */ new Date(ts * 1e3));
}
/** `_kb_vault_line`（vault list 的一行）。 */
function vaultLine(vault) {
	const flags = [];
	if (vault.isDefault) flags.push("default");
	if (vault.mode === "rw") flags.push("rw");
	const tail = flags.length > 0 ? `｜${flags.join("、")}` : "";
	const scan = vault.lastScanAt ?? "未扫描";
	return `${vault.name}\t\`${vault.path}\`\tmode=${vault.mode}\t最近扫描=${scan}${tail}\tvault_id=${vault.vaultId}`;
}
/** 统计层：递归汇总一个目录（只数不渲染）。 */
function treeDir(vault, absDir, level, maxDepth, globs) {
	const root = realpathSync(vault.path);
	let names = [];
	try {
		names = readdirSync(absDir).sort();
	} catch {
		names = [];
	}
	let notes = 0;
	let attachments = 0;
	let files = 0;
	let totalBytes = 0;
	let mtime = 0;
	const children = [];
	const subdirs = [];
	let hiddenDirs = 0;
	for (const name of names) {
		const absPath = path.join(absDir, name);
		const relative = path.relative(root, absPath);
		if (relative.startsWith("..") || path.isAbsolute(relative)) continue;
		if (isReparsePoint(absPath)) continue;
		let isDir = false;
		try {
			isDir = lstatSync(absPath).isDirectory();
		} catch {
			isDir = false;
		}
		const rel = relative.split(path.sep).join("/");
		if (kbIgnored(isDir ? `${rel}/` : rel, globs) !== null) continue;
		if (isDir) {
			if (level >= maxDepth) {
				hiddenDirs += 1;
				children.push({
					rel,
					name,
					isDir: true,
					notes: 0,
					attachments: 0,
					bytes: 0,
					mtime: 0,
					notScanned: true
				});
				continue;
			}
			const child = treeDir(vault, absPath, level + 1, maxDepth, globs);
			notes += child.notes;
			attachments += child.attachments;
			files += child.files;
			totalBytes += child.bytes;
			hiddenDirs += child.hiddenDirs;
			mtime = Math.max(mtime, child.mtime);
			children.push({
				rel,
				name,
				isDir: true,
				notes: child.notes,
				attachments: child.attachments,
				bytes: child.bytes,
				mtime: child.mtime,
				notScanned: false
			});
			subdirs.push(child);
			continue;
		}
		let size = 0;
		let itemMtime = 0;
		try {
			const stat = lstatSync(absPath);
			size = stat.size;
			itemMtime = stat.mtimeMs / 1e3;
		} catch {
			size = 0;
			itemMtime = 0;
		}
		files += 1;
		totalBytes += size;
		mtime = Math.max(mtime, itemMtime);
		const isNote = name.toLowerCase().endsWith(".md");
		if (isNote) notes += 1;
		else attachments += 1;
		children.push({
			rel,
			name,
			isDir: false,
			notes: 0,
			attachments: isNote ? 0 : 1,
			bytes: size,
			mtime: itemMtime,
			notScanned: false
		});
	}
	children.sort((left, right) => right.mtime !== left.mtime ? right.mtime - left.mtime : left.rel < right.rel ? -1 : left.rel > right.rel ? 1 : 0);
	subdirs.sort((left, right) => left.rel < right.rel ? -1 : left.rel > right.rel ? 1 : 0);
	return {
		rel: absDir === root ? "" : path.relative(root, absDir).split(path.sep).join("/"),
		notes,
		attachments,
		files,
		bytes: totalBytes,
		hiddenDirs,
		partial: hiddenDirs > 0,
		mtime,
		children: children.slice(0, 20),
		more: Math.max(0, children.length - 20),
		dirs: subdirs
	};
}
/** 显示层：`depth` 的唯一语义定义（渲染侧独立裁剪，不信任统计层）。 */
function treeLines(vault, startRel, tree, depth, asOf, ignoreGlobs) {
	const maxLevel = Math.max(0, Math.trunc(depth));
	const lines = [
		`# 知识库目录 · ${vault.name}`,
		"",
		`> source=知识库（未建索引）｜as_of ${asOf}`,
		`> vault=\`${vault.path}\`（mode=${vault.mode}）｜起点=\`${startRel !== "" ? startRel : "/"}\`｜深度=${depth}`,
		`> 生效忽略规则：${ignoreGlobs.length > 0 ? ignoreGlobs.join(", ") : "（无）"}`,
		KB_TREE_DEPTH_RULE,
		`> 统计面：数到第 ${depth} 层（更深的目录不显示、也不计入合计）；只统计不读正文：笔记 = .md 计数，附件 = 其余文件计数`,
		`> 每目录最多列 20 项（超出记 more=N）；更深请加大 --depth（上限 4），或给 path=<目录> 直接进入`,
		""
	];
	const emit = (node, level) => {
		const pad = "  ".repeat(level);
		const name = node.rel !== "" ? node.rel : vault.name;
		lines.push(`${pad}- ${name}/  ${node.notes} 篇笔记 / ${node.attachments} 附件 / ${node.bytes} B / 最近修改=${formatMtime(node.mtime)}`);
		for (const child of node.children) {
			if (child.isDir) continue;
			if (!child.name.toLowerCase().endsWith(".md")) continue;
			lines.push(`${pad}  - ${child.rel}  ${child.bytes} B  ${formatMtime(child.mtime)}`);
		}
		if (node.more !== 0) lines.push(`${pad}  - …还有 ${node.more} 项未列出（more=${node.more}）`);
		if (level >= maxLevel) return;
		for (const sub of node.dirs) emit(sub, level + 1);
	};
	emit(tree, 0);
	return lines;
}
/** 截断时钉在末尾的那一句（给一个能直接粘回去的目录）。 */
function treeTailHint(tree, startRel) {
	for (const sub of tree.dirs) if (sub.rel !== "") return `> 已截断：用 \`path=${sub.rel}\` 深入（或加大 --max-chars / --max-tokens；完整读数见 --json）`;
	if (startRel !== "") return `> 已截断：用 \`path=${startRel}\` 深入某一级子目录（或加大 --max-chars / --max-tokens）`;
	return "> 已截断：加大 --max-chars / --max-tokens 再看（起点下没有可深入的子目录）";
}
/** 看目录（只统计、不读正文、双预算）——§4.1 / KB-2 / KB-3。 */
function kbTree(vault, options = {}) {
	const rel = options.rel ?? "";
	const depthArg = options.depth ?? 2;
	const maxChars = options.maxChars ?? 8e3;
	const maxTokens = options.maxTokens ?? 1200;
	const now = options.now ?? /* @__PURE__ */ new Date();
	const wantDepthRaw = Math.trunc(Number(depthArg));
	if (!Number.isFinite(wantDepthRaw)) throw new KbError("bad_usage", `depth 必须是整数，收到 ${pyText(depthArg)}`);
	const wantDepth = Math.max(1, Math.min(wantDepthRaw, 4));
	const globs = effectiveGlobs(vault.ignoreGlobs);
	if (!existsSync(vault.path) || !statSync(vault.path).isDirectory()) {
		if (vault.lastScanAt !== null) {
			const fitted = kbFitText([[
				`# 知识库目录（离线）· ${vault.name}`,
				"",
				`> 知识库不在线（最后扫描 ${vault.lastScanAt}）：路径 \`${vault.path}\` 打不开。`,
				`> 上次扫描读数：${pyText(vault.lastScanFiles)} 文件 / ${pyText(vault.lastScanNotes)} 笔记 / ${pyText(vault.lastScanBytes)} 字节。`,
				"> 下面没有任何实时内容——不是空库，是离线。"
			].join("\n")], maxChars, maxTokens);
			return {
				vault: vaultBrief(vault),
				asOf: isoLocal(now),
				root: String(rel ?? "").replace(/\\/gu, "/").replace(/^\/+|\/+$/gu, ""),
				depth: wantDepth,
				ignoreGlobs: globs,
				dirs: [],
				summary: null,
				scan: {
					files: null,
					bytes: null,
					ms: 0,
					stale: true,
					partial: false,
					hiddenDirs: 0
				},
				text: fitted.text,
				chars: fitted.chars,
				tokens: fitted.tokens,
				truncated: fitted.truncated,
				truncatedReasons: fitted.truncatedReasons
			};
		}
		throw new KbError("offline", `知识库不在线：${vault.path}`, "检查盘符/目录是否改名；有历史扫描数时会降级显示");
	}
	const target = kbResolveTarget(vault, rel);
	if (!existsSync(target.abs)) throw new KbError("not_found", `知识库里没有这个目录：${target.rel}`, "fish kb tree 看根目录，再逐层进入");
	const startedAt = process.hrtime.bigint();
	const tree = treeDir(vault, target.abs, 0, wantDepth, globs);
	const ms = Number((process.hrtime.bigint() - startedAt) / 1000000n);
	const asOf = isoLocal(now);
	const fitted = kbFitTextWithTail(treeLines(vault, target.rel, tree, wantDepth, asOf, globs), treeTailHint(tree, target.rel), maxChars, maxTokens, "> 已截断：加大 --max-chars / --max-tokens 再看（或 --json）");
	const dirs = [...tree.dirs].sort((left, right) => left.rel < right.rel ? -1 : left.rel > right.rel ? 1 : 0);
	return {
		vault: vaultBrief(vault),
		asOf,
		root: target.rel,
		depth: wantDepth,
		dirs,
		summary: tree,
		ignoreGlobs: globs,
		scan: {
			files: tree.files,
			bytes: tree.bytes,
			ms,
			stale: false,
			partial: tree.partial,
			hiddenDirs: tree.hiddenDirs
		},
		text: fitted.text,
		chars: fitted.chars,
		tokens: fitted.tokens,
		truncated: fitted.truncated,
		truncatedReasons: fitted.truncatedReasons
	};
}
/** 严格布尔解析（`_strict_bool`）：`"false"` 是**拒绝**，模糊写法直接报错。 */
function parseStrictBool(value, name) {
	if (value === null || value === void 0) return false;
	if (typeof value === "boolean") return value;
	if (typeof value === "number") {
		if (value === 0) return false;
		if (value === 1) return true;
	} else if (typeof value === "string") {
		const text = value.trim().toLowerCase();
		if (text === "true" || text === "1") return true;
		if (text === "false" || text === "0") return false;
	}
	throw new KbError("bad_usage", `${name} 只接受 true / false（也接受 1 / 0），收到 ${pyText(value)}`, "布尔开关不接受模糊写法：写 true 或 false 才是明确的授权/拒绝；CLI 不带 `--allow-sensitive` 即拒绝");
}
/** 敏感文件的拒读说明（文件名 + 大小 + 建议）。 */
function sensitiveNotice(relPosix, size) {
	return `\`${relPosix}\` 是敏感文件（${size} B），默认不把正文送进模型上下文；建议在 Obsidian 里直接打开。确需由 AI 读取，请显式二次授权`;
}
/** 读单文件（必须给显式相对路径）——§4.2 / KB-7 / KB-8。 */
function kbOpen(vault, rel, options = {}) {
	const allowSensitive = parseStrictBool(options.allowSensitive, "allow_sensitive");
	options.now;
	const limit = vault.maxOpenBytes > 0 ? vault.maxOpenBytes : KB_DEFAULT_MAX_OPEN_BYTES;
	const target = kbResolveTarget(vault, rel);
	const cap = Math.max(1, Math.trunc(options.maxChars ?? 64e3));
	if (!Number.isFinite(cap)) throw new KbError("bad_usage", `max_chars 必须是整数，收到 ${pyText(options.maxChars)}`);
	if (existsSync(target.abs) && statSync(target.abs).isDirectory()) throw new KbError("bad_usage", `目标是目录，不是文件：${target.rel !== "" ? target.rel : "/"}`, "用 `fish kb tree <目录>` 看目录；open 只读单个文件");
	if (!existsSync(target.abs) || !statSync(target.abs).isFile()) throw new KbError("not_found", `知识库里没有这个文件：${target.rel}`, "`fish kb tree` 看目录结构，再给完整相对路径");
	const size = statSync(target.abs).size;
	if (size > limit) throw new KbError("too_large", `文件 ${size} B 超过单文件上限 ${limit} B`, "长文档请分段读：`kb open <path> --max-chars`，或直接用文件工具读");
	const sensitive = target.sensitive;
	if (sensitive && !allowSensitive) throw new KbError("sensitive", sensitiveNotice(target.rel, size), "CLI 加 `--allow-sensitive`；MCP 传 allow_sensitive=true");
	const raw = readFileSync(target.abs);
	const detected = kbDetectText(raw, path.extname(target.abs));
	if (detected.media) throw new KbError("media", `这是媒体/二进制文件（${detected.reason ?? ""}），不按文本读`, "用对应的查看器打开；知识库只读文本");
	if (!detected.isText || detected.encoding === null) throw new KbError("bad_encoding", "不是可识别的文本编码（UTF-8 / GB18030 都失败）", "另存为 UTF-8 再读");
	const text = new TextDecoder(detected.encoding === "utf-8-sig" ? "utf-8" : detected.encoding, { fatal: true }).decode(raw);
	const sha = createSha256(raw);
	const truncated = codePointLength(text) > cap;
	const shownText = truncated ? sliceCodePoints(text, 0, cap) : text;
	const shownBytes = truncated ? Buffer.byteLength(shownText, "utf8") : raw.length;
	const statements = [
		"来源=知识库（未建索引），不是 fish 记忆；不进 recall / 全文索引。",
		"本库未建索引；要检索请显式 `fish kb search`（按需扫描）。",
		(truncated ? "已截断" : "未截断") + `（${raw.length}/${limit} B 上限；显示 ${codePointLength(shownText)}/${codePointLength(text)} 字）`
	];
	if (sensitive) statements.push("本文件命中敏感名单：正文是在**显式授权**（allow_sensitive）下读出的。");
	return {
		rel: target.rel,
		abs: target.abs,
		bytes: raw.length,
		sha256: sha,
		encoding: detected.encoding,
		eol: kbDetectEol(raw),
		truncated,
		sensitive,
		shown: shownBytes,
		limit,
		charsShown: codePointLength(shownText),
		charsTotal: codePointLength(text),
		statements,
		text: shownText
	};
}
/** sha256（只在内存里算，不落盘）。 */
function createSha256(data) {
	return createHash("sha256").update(data).digest("hex");
}
/** 在 vault 内**按需扫描**检索（不建索引）——§4.3 / KB-10 / KB-9。 */
function kbSearch(vault, query, options = {}) {
	const q = String(query ?? "").trim();
	if (q === "") throw new KbError("bad_usage", "检索词不能为空", "fish kb search <关键词>");
	if (!existsSync(vault.path) || !statSync(vault.path).isDirectory()) throw new KbError("offline", `知识库不在线：${vault.path}`, "检查盘符/目录是否改名");
	const want = Math.max(1, Math.min(Math.trunc(options.limit ?? 10), 30));
	const maxChars = options.maxChars ?? 8e3;
	const maxTokens = options.maxTokens ?? 1800;
	const needle = q.toLowerCase();
	const asOf = isoLocal(options.now ?? /* @__PURE__ */ new Date());
	const startedAt = process.hrtime.bigint();
	let scannedFiles = 0;
	let scannedBytes = 0;
	let skippedLarge = 0;
	let skippedBinary = 0;
	let skippedEncoding = 0;
	let skippedSensitive = 0;
	const hits = [];
	for (const entry of kbWalk(vault, realpathSync(vault.path))) {
		if (entry.isDir || !entry.rel.toLowerCase().endsWith(".md")) continue;
		if (kbIsSensitiveName(entry.rel)) {
			skippedSensitive += 1;
			continue;
		}
		scannedFiles += 1;
		if (entry.size > 2097152) {
			skippedLarge += 1;
			continue;
		}
		let raw;
		try {
			raw = readFileSync(entry.abs);
		} catch {
			continue;
		}
		scannedBytes += raw.length;
		const detected = kbDetectText(raw, ".md");
		if (!detected.isText || detected.encoding === null) {
			if (detected.media) skippedBinary += 1;
			else skippedEncoding += 1;
			continue;
		}
		const text = new TextDecoder(detected.encoding === "utf-8-sig" ? "utf-8" : detected.encoding, { fatal: false }).decode(raw);
		const matched = [];
		let occurrences = 0;
		const lines = text.split(/\r\n|\r|\n/u);
		for (let i = 0; i < lines.length; i += 1) {
			const line = lines[i];
			const folded = line.toLowerCase();
			if (!folded.includes(needle)) continue;
			occurrences += folded.split(needle).length - 1;
			let snippet = line.trim();
			if (codePointLength(snippet) > 240) {
				const position = snippet.toLowerCase().indexOf(needle);
				const start = Math.max(0, position - 60);
				snippet = `${start > 0 ? "…" : ""}${sliceCodePoints(snippet, start, start + 240)}…`;
			}
			matched.push({
				lineno: i + 1,
				text: snippet
			});
		}
		if (matched.length > 0) hits.push({
			rel: entry.rel,
			abs: entry.abs,
			occurrences,
			lines: matched.slice(0, 3),
			mtime: entry.mtime
		});
	}
	const ms = Number((process.hrtime.bigint() - startedAt) / 1000000n);
	hits.sort((left, right) => {
		if (right.occurrences !== left.occurrences) return right.occurrences - left.occurrences;
		if (right.mtime !== left.mtime) return right.mtime - left.mtime;
		return left.rel < right.rel ? -1 : left.rel > right.rel ? 1 : 0;
	});
	const total = hits.length;
	const shown = hits.slice(0, want);
	const queryHash = sha12$2(q.toLowerCase());
	const lines = [
		`# 知识库检索 · ${vault.name}`,
		"",
		`> source=知识库（未建索引）｜as_of ${asOf}`,
		`> scan=${scannedFiles} files / ${scannedBytes} bytes / ${ms} ms｜命中 ${total} 文件，显示 ${shown.length} 个`,
		`> 跳过：大文件 ${skippedLarge} / 二进制 ${skippedBinary} / 编码不可读 ${skippedEncoding} / 敏感名单 ${skippedSensitive}（单文件上限 ${KB_SEARCH_MAX_FILE_BYTES} B）`,
		"> 只扫已登记 vault 的 `.md`；不建索引、不进 `memory_fts`——找 fish 记忆请用 `recall`。",
		""
	];
	for (const hit of shown) {
		lines.push(`- \`${hit.abs}\`（库内 \`${hit.rel}\`，命中 ${hit.occurrences} 处）`);
		for (const item of hit.lines) lines.push(`  - L${item.lineno}: ${item.text}`);
	}
	if (shown.length === 0) lines.push("- （没有命中）");
	const fitted = kbFitText(lines, maxChars, maxTokens);
	return {
		vault: vaultBrief(vault),
		asOf,
		queryHash,
		qlen: codePointLength(q),
		limit: want,
		scanned: {
			files: scannedFiles,
			bytes: scannedBytes,
			ms,
			skippedLarge,
			skippedBinary,
			skippedEncoding,
			skippedSensitive
		},
		hits: shown,
		total,
		shown: shown.length,
		text: fitted.text,
		chars: fitted.chars,
		tokens: fitted.tokens,
		truncated: fitted.truncated,
		truncatedReasons: fitted.truncatedReasons
	};
}
/** sha256 前 12 位（`query_hash` 与写路径的 `write_id` 前缀同口径）。 */
function sha12$2(text) {
	return createHash("sha256").update(text, "utf8").digest("hex").slice(0, 12);
}
/** 在线就现扫，离线就回退 `last_scan_*`（`source` 标明来源，不冒充实时）。 */
function scanCounts(vault) {
	if (!existsSync(vault.path) || !statSync(vault.path).isDirectory()) return {
		files: vault.lastScanFiles,
		notes: vault.lastScanNotes,
		bytes: vault.lastScanBytes,
		source: "last_scan",
		online: false
	};
	let files = 0;
	let notes = 0;
	let total = 0;
	for (const entry of kbWalk(vault, realpathSync(vault.path))) {
		if (entry.isDir) continue;
		files += 1;
		total += entry.size;
		if (entry.rel.toLowerCase().endsWith(".md")) notes += 1;
	}
	return {
		files,
		notes,
		bytes: total,
		source: "live",
		online: true
	};
}
/** 规模 / 未读 / 最近变更 / 每库读写计数 / `kb_access` 总行数。 */
function kbStats(db, key, now = /* @__PURE__ */ new Date()) {
	const vaults = String(key ?? "").trim() !== "" ? [kbVaultGet(db, key)] : kbVaultList(db);
	const asOf = isoLocal(now);
	const out = [];
	for (const vault of vaults) {
		const counts = scanCounts(vault);
		const scalar = (sql) => {
			const row = db.prepare(sql).get(vault.vaultId);
			return Number(row?.n ?? 0);
		};
		const opened = scalar("SELECT COUNT(DISTINCT path) AS n FROM kb_access WHERE vault_id=? AND action='open' AND result='ok'");
		const writes = scalar("SELECT COUNT(*) AS n FROM kb_access WHERE vault_id=? AND action IN ('create','append','update') AND result='ok'");
		const rollbacks = scalar("SELECT COUNT(*) AS n FROM kb_access WHERE vault_id=? AND action='rollback'");
		const reads = scalar("SELECT COUNT(*) AS n FROM kb_access WHERE vault_id=? AND action IN ('open','tree','search','search_hit')");
		const unread = counts.notes === null ? null : Math.max(0, counts.notes - opened);
		out.push({
			name: vault.name,
			vaultId: vault.vaultId,
			mode: vault.mode,
			path: vault.path,
			files: counts.files,
			notes: counts.notes,
			bytes: counts.bytes,
			countsSource: counts.source,
			online: counts.online,
			openedPaths: opened,
			unreadNotes: unread,
			reads,
			writes,
			rollbacks,
			lastScanAt: vault.lastScanAt
		});
	}
	const accessRow = db.prepare("SELECT COUNT(*) AS n FROM kb_access").get();
	const accessRows = Number(accessRow?.n ?? 0);
	const recent = db.prepare("SELECT id, ts, path, action, result, citizen_id FROM kb_access ORDER BY id DESC LIMIT 10").all().map((row) => ({
		id: Number(row.id),
		ts: String(row.ts ?? ""),
		path: String(row.path ?? ""),
		action: String(row.action ?? ""),
		result: String(row.result ?? ""),
		citizenId: row.citizen_id === null || row.citizen_id === void 0 ? null : String(row.citizen_id)
	}));
	const lines = [
		"# 知识库规模（stats）",
		"",
		`> as_of ${asOf}`,
		`> kb_access 总行数：${accessRows}`
	];
	for (const item of out) lines.push("", `## ${item.name}（mode=${item.mode}）`, `- 路径：${item.path}`, `- 规模：${pyText(item.files)} 文件 / ${pyText(item.notes)} 笔记 / ${pyText(item.bytes)} 字节（来源：${item.countsSource}）`, `- 未读（估算）：${pyText(item.unreadNotes)} 篇（无 open 记录）`, `- 读 ${item.reads} 次 / 写 ${item.writes} 次 / 回滚 ${item.rollbacks} 次`);
	lines.push("", "## 最近变更", "");
	if (recent.length > 0) for (const item of recent) lines.push(`- ${item.ts} ${item.action}(${item.result}) \`${item.path}\` by ${pyText(item.citizenId)}`);
	else lines.push("- （还没有 kb_access 记录）");
	return {
		asOf,
		vaults: out,
		accessRows,
		recent,
		text: lines.join("\n")
	};
}
/** `fish kb vault list` 的回执。 */
function renderKbVaultList(vaults) {
	if (vaults.length === 0) return "还没有登记任何知识库。登记入口：fish kb vault add --name <n> --path <绝对路径> [--mode ro|rw] [--default]";
	return [...vaults.map((vault) => vaultLine(vault)), `共 ${vaults.length} 个知识库`].join("\n");
}
/** `fish kb tree` 的回执（正文 + 一行合计）。 */
function renderKbTree(result) {
	const scan = result.scan;
	return [
		result.text,
		"",
		`合计：${pyText(scan.files)} 文件 / ${pyText(scan.bytes)} 字节 / ${scan.ms} ms` + (scan.stale ? "（离线：上次扫描数）" : "") + `｜chars=${result.chars} tokens=${result.tokens} truncated=${pyText(result.truncated)}`
	].join("\n");
}
/** `fish kb open` 的回执（三句声明 + 正文）。 */
function renderKbOpen(result) {
	return [
		...result.statements.map((line) => `> ${line}`),
		"",
		result.text
	].join("\n");
}
/** `fish kb search` 的回执。 */
function renderKbSearch(result) {
	return result.text;
}
/** `fish kb stats` 的回执。 */
function renderKbStats(result) {
	return result.text;
}
/** 知识库域错误的回执（与生产 `错误[<code>]：<message>` 同形）。 */
function renderKbError(error) {
	return `错误[${error.code}]：${error.format()}`;
}
//#endregion
//#region src/domain/openloop.ts
/** 未闭合的关键词表（标题命中任一即算 A 类信号）。 */
const ATTENTION_OPEN_KEYWORDS = [
	"未闭合",
	"待办",
	"下一步",
	"todo",
	"待确认",
	"待定",
	"待验证",
	"待补"
];
/** A 类「未闭合事项」的唯一谓词（`_open_loop_rows` 与 `count_open_loop_rows` 共用）。 */
function openLoopPredicate() {
	return {
		clauses: `status='active' AND dormant=0 AND recorded_at >= ? AND (${ATTENTION_OPEN_KEYWORDS.map(() => "INSTR(LOWER(COALESCE(title,'')), ?) > 0").join(" OR ")})`,
		keywords: ATTENTION_OPEN_KEYWORDS.map((keyword) => keyword.toLowerCase())
	};
}
/** A 类在窗口内**总共有多少条**（不受 `perType` 限制）——段头 N/M 的 M。 */
function countOpenLoopRows(db, sinceZ, exclude = []) {
	const predicate = openLoopPredicate();
	let sql = `SELECT COUNT(*) AS n FROM code_index WHERE ${predicate.clauses}`;
	const params = [sinceZ, ...predicate.keywords];
	const extra = exclude.filter((code) => code !== "");
	if (extra.length > 0) {
		sql += ` AND code NOT IN (${extra.map(() => "?").join(",")})`;
		params.push(...extra);
	}
	try {
		const row = db.prepare(sql).get(...params);
		return Number(row?.n ?? 0);
	} catch {
		return null;
	}
}
/**
* A 类「未闭合事项」：标题命中开放词表的 active 记忆（窗口内，时间降序）。
*
* `project`（T05 起）非空时只取该项目的条目——项目状态表的「未了事项」列走这一档。
* 为什么加在这里而不是另写一份 SQL：状态表与注入包的计数必须是**同一个谓词**，
* 否则会出现"注入包说有 3 条、状态表说 2 条"这种最难解释的分歧。
*/
function openLoopRows(db, sinceZ, limit, exclude = /* @__PURE__ */ new Set(), project = null) {
	const predicate = openLoopPredicate();
	let where = predicate.clauses;
	const params = [sinceZ, ...predicate.keywords];
	const key = (project ?? "").trim();
	if (key !== "") {
		where += " AND project = ?";
		params.push(key);
	}
	const sql = `SELECT code,title FROM code_index WHERE ${where} ORDER BY recorded_at DESC, code DESC LIMIT ?`;
	params.push(Math.trunc(limit) + exclude.size);
	let rows;
	try {
		rows = db.prepare(sql).all(...params);
	} catch {
		return [];
	}
	const out = [];
	for (const row of rows) {
		if (exclude.has(row.code)) continue;
		out.push({
			code: row.code,
			title: (row.title ?? "").trim()
		});
		if (out.length >= Math.trunc(limit)) break;
	}
	return out;
}
/** 窗口内 lesson 的总条数（不受 `perType` 限制）——段⑤ 段头 N/M 的 M。 */
function countLessonRows(db, sinceZ, exclude = []) {
	let sql = "SELECT COUNT(*) AS n FROM code_index WHERE mem_type='lesson' AND status='active' AND dormant=0 AND recorded_at >= ?";
	const params = [sinceZ];
	const extra = exclude.filter((code) => code !== "");
	if (extra.length > 0) {
		sql += ` AND code NOT IN (${extra.map(() => "?").join(",")})`;
		params.push(...extra);
	}
	try {
		const row = db.prepare(sql).get(...params);
		return Number(row?.n ?? 0);
	} catch {
		return null;
	}
}
/** 窗口内 lesson 的 `(code, title)`：时间降序；`sameProject` 给定时只取该项目。 */
function lessonRows(db, sinceZ, limit, exclude = /* @__PURE__ */ new Set(), sameProject = null) {
	let where = "recorded_at >= ?";
	const params = [sinceZ];
	if (sameProject !== null) {
		where += " AND project = ?";
		params.push(sameProject);
	}
	const sql = `SELECT code,title FROM code_index WHERE mem_type='lesson' AND status='active' AND dormant=0 AND ${where} ORDER BY recorded_at DESC, code DESC LIMIT ?`;
	params.push(Math.trunc(limit) + exclude.size);
	let rows;
	try {
		rows = db.prepare(sql).all(...params);
	} catch {
		return [];
	}
	const out = [];
	for (const row of rows) {
		if (exclude.has(row.code)) continue;
		out.push({
			code: row.code,
			title: (row.title ?? "").trim()
		});
		if (out.length >= Math.trunc(limit)) break;
	}
	return out;
}
/** B 类「最近的坑」：同项目优先，不足再用跨项目补齐（复刻 `_recent_lesson_rows`）。 */
function recentLessonRows(db, sinceZ, limit, exclude = /* @__PURE__ */ new Set(), project = null) {
	const key = (project ?? "").trim() === "" ? null : (project ?? "").trim();
	const out = key === null ? [] : lessonRows(db, sinceZ, limit, exclude, key);
	if (out.length < limit) {
		const taken = /* @__PURE__ */ new Set([...exclude, ...out.map((item) => item.code)]);
		out.push(...lessonRows(db, sinceZ, limit - out.length, taken, null));
	}
	return out;
}
//#endregion
//#region src/domain/project.ts
/**
* 项目与别名的**只读**面：`project status` / `alias list` / `workspace list`。
*
* ## 三个命令为什么都在这个模块里
*
* 它们读的是同一组"项目坐标系"表：`code_registry`（项目清单的唯一来源，裁决 Q6）、
* `name_aliases`（别名）、`workspace_map`（工作区归属）。三张表在生产的语义里互相引用
* （别名展开决定"同一个项目的全部名字"，工作区映射决定"这段记忆属于谁"），
* 拆成三个文件会让 `resolveName` 被复制三遍——而"同一件事两种语义"是本项目最贵的 bug 形态。
*
* ## 只读依据（逐条对过源码，不是"看起来像读"）
*
*   · `project_status_rows`（`p0/fish.py:14517`，docstring 明写"**只读**：不改任何表、不写盘"）；
*   · `alias` 的 list 分支（`p0/fish.py:6722-6738`：两条 SELECT + `resolve_name`）；
*   · `workspace` 的 list 分支（`p0/fish.py:10065-10076` → `list_workspace_mappings`，纯 SELECT）。
*
* ## 与生产的一处**有意差异**
*
* 生产 `fish project status` 默认会把渲染结果**写进** `<home>/10-项目库/项目状态表.md`
* （`render_project_status` → `_layout_write`）。影子期不写任何文件，所以本模块只做
* "同一份六列表 + 同一份 JSON"，并在回执末尾明写"没有落盘、终端里会写到哪"。
* `--json` 形态在生产里本来就不写文件（`cmd_project_status` 的 `if args.json` 分支），
* 因此 JSON 回执与生产**逐字节同形**——这也是对拍取 JSON 而不是取表格的原因。
*
* @module dsh-fish/domain/project
*/
/** 六列的**严格顺序**（§3.4）。 */
const PST_COLUMNS = [
	"项目",
	"状态",
	"最近活跃",
	"看板入口",
	"主责 AI",
	"未了事项"
];
/** 未了事项与最近活跃的窗口（与注入包的 `ATTENTION_OPEN_WINDOW_DAYS` **同一个常量**）。 */
const PST_OPEN_WINDOW_DAYS = 30;
/** 无开档期时回退"最近 7 天记忆 owner"。 */
const PST_OWNER_FALLBACK_DAYS = 7;
const PST_DASH_NO_MEMORY = "—（无记忆）";
const PST_DASH_NO_BOARD = "—（今日无看板）";
const PST_DASH_NO_OWNER = "—（无开档期）";
const PST_DASH_NONE = "—";
const PST_DASH_UNAVAILABLE = "—（数据源不可用）";
/** 口径常量：置顶到导出面，脚注与谓词都从这里取（两处各写一份必然漂开）。 */
const LAYOUT_STATUS_TABLE = "10-项目库/项目状态表.md";
/**
* 解析 `--as-of`（与 `fish._parse_as_of` 同口径：`Z` / `+08:00` / 无时区按本地补）。
* @throws FishError `COMMAND_USAGE` —— 解析失败（生产对应 exit 2）
*/
function parseAsOf(raw) {
	const text = String(raw ?? "").trim();
	if (text === "") return {
		date: /* @__PURE__ */ new Date(),
		offsetMinutes: -(/* @__PURE__ */ new Date()).getTimezoneOffset()
	};
	const date = parseStamp(text);
	if (date === null) throw new FishError("COMMAND_USAGE", `--as-of 无法解析：${JSON.stringify(raw)}（示例：2026-10-26T00:00:00+08:00 或 2026-10-26T00:00:00Z）`);
	return {
		date,
		offsetMinutes: offsetMinutesOf(text) ?? -date.getTimezoneOffset()
	};
}
/** as-of 的本地 ISO（`iso_local(as_of)`）。 */
function asOfIso(asOf) {
	return isoWithOffset(asOf.date, asOf.offsetMinutes);
}
/** 读全部别名行（`ORDER BY kind, from_name`）。 */
function aliasRows(db) {
	return db.prepare("SELECT kind,from_name,to_name,created_at,reason FROM name_aliases ORDER BY kind,from_name").all().map((row) => ({
		kind: String(row.kind),
		from: String(row.from_name),
		to: String(row.to_name),
		createdAt: String(row.created_at ?? ""),
		reason: row.reason === null || row.reason === void 0 ? "" : String(row.reason)
	}));
}
/**
* `fish alias list` 的回执：原始行 + "哪些名字解析成同一个"的分组。
*
* 分组顺序刻意与生产一致（**首次出现即入表**的 canonical 顺序）：换个顺序也能读，
* 但逐字节对拍会红，而"顺序差异"和"分组差异"在回执上长得一样——那种红最难判。
*/
function renderAliasList(db) {
	const rows = aliasRows(db);
	const lines = [];
	if (rows.length === 0) lines.push("（还没有任何别名）");
	for (const row of rows) lines.push(`${row.kind}\t${row.from} → ${row.to}\t${row.createdAt}\t${row.reason}`);
	for (const kind of ALIAS_KINDS) {
		const froms = db.prepare("SELECT from_name FROM name_aliases WHERE kind=?").all(kind).map((row) => String(row.from_name));
		const groups = /* @__PURE__ */ new Map();
		for (const from of froms) {
			const canonical = resolveName(db, kind, from).canonical;
			const bucket = groups.get(canonical);
			if (bucket === void 0) groups.set(canonical, [from]);
			else bucket.push(from);
		}
		for (const [canonical, names] of groups) lines.push(`[${kind}] ${canonical} ← ${[...names].sort().join(", ")}`);
	}
	return lines.join("\n");
}
/** 全部映射（按路径升序）。 */
function workspaceMappings(db) {
	return db.prepare("SELECT path_prefix, project, note, created_at FROM workspace_map ORDER BY path_prefix").all().map((row) => ({
		pathPrefix: String(row.path_prefix),
		project: String(row.project),
		note: row.note === null || row.note === void 0 ? "" : String(row.note),
		createdAt: String(row.created_at ?? "")
	}));
}
/** `fish workspace list` 的回执（一行一条 + 缩进备注；不做固定列宽）。 */
function renderWorkspaceList(db) {
	const rows = workspaceMappings(db);
	if (rows.length === 0) return "还没有任何映射。用 `fish workspace set <路径> <项目>` 添加，或 `fish workspace seed` 看看现有记忆里提到哪些工作区。";
	const lines = rows.map((row) => `${row.pathPrefix}  →  ${row.project}${row.note !== "" ? `（${row.note}）` : ""}`);
	lines.push("", `共 ${rows.length} 条映射（路径一律小写/正斜杠/无尾斜杠，匹配不区分大小写）`);
	return lines.join("\n");
}
/** 状态 → 人读标签；未知取值原样显示（脚注会说明"未登记状态"）。 */
function statusLabel(status) {
	return {
		active: "进行中",
		closed: "已闭合",
		retired: "已退役"
	}[status] ?? (status !== "" ? status : "—");
}
/** 表格单元格消毒：`|` 会破表、换行会断行；`null` 显示空（由调用方决定用哪个 `—`）。 */
function cell(value) {
	if (value === null || value === void 0) return "";
	return String(value).replace(/\|/gu, "\\|").replace(/\r/gu, " ").replace(/\n/gu, " ").trim();
}
/** 公民 id → 可读短 id（12 位足够在同一份索引里互相区分）。 */
function shortCitizenId(citizenId) {
	const text = String(citizenId ?? "").trim();
	if (text === "") return "";
	return text.length <= 12 ? text : `${text.slice(0, 12)}…`;
}
/** citizen_id → display_name（查不到 → `null`）。复用生产的两条既有读点（先按名字、再按 id）。 */
function displayNameOf(db, citizenId) {
	if (citizenId === "") return null;
	const byName = findCitizenByName(db, citizenId);
	if (byName !== null) return byName.display_name;
	const byId = db.prepare("SELECT citizen_id,display_name FROM citizens_all WHERE citizen_id=?").get(citizenId);
	return byId === void 0 ? null : String(byId.display_name ?? "");
}
/** 一个 attr 名字的展示串（display_name 优先，回落短 id）。 */
function displayOf(db, citizenId) {
	const display = (displayNameOf(db, citizenId) ?? "").trim();
	return display !== "" ? display : shortCitizenId(citizenId);
}
/** `recorded_at`（UTC Z）→ as-of 时区里的 `YYYY-MM-DD`。 */
function localDateOf(stamp, offsetMinutes) {
	const date = parseStamp(stamp);
	if (date === null) return String(stamp ?? "").slice(0, 10);
	return datePartsInOffset(date, offsetMinutes).ymd;
}
/** 项目清单（`code_registry`，排序：active → closed → retired → 未知，再按 prefix）。 */
function registryRows(db) {
	return db.prepare("SELECT prefix, namespace, status FROM code_registry ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'closed' THEN 1 WHEN 'retired' THEN 2 ELSE 3 END, prefix").all().map((row) => ({
		prefix: String(row.prefix ?? ""),
		namespace: String(row.namespace ?? ""),
		status: String(row.status ?? "")
	}));
}
/** 最近活跃：`max(code_index.recorded_at) WHERE project=? AND dormant=0 [AND <= as_of]`。 */
function lastActiveOf(db, prefix, asOf) {
	let sql = "SELECT MAX(recorded_at) AS v FROM code_index WHERE project=? AND dormant=0";
	const params = [prefix];
	sql += " AND recorded_at<=?";
	params.push(utcStamp(asOf.date));
	const value = db.prepare(sql).get(...params)?.v ?? null;
	if (value === null || value === "") return [PST_DASH_NO_MEMORY, "code_index.recorded_at(无记录)"];
	return [localDateOf(String(value), asOf.offsetMinutes), "code_index.recorded_at(max)"];
}
/** 看板入口：优先"as-of 当日的项目板"，否则"最近一条 open 板" → `(相对链接, 来源)`。 */
function boardEntryOf(db, prefix, asOf, homeRoot) {
	const periodKey = datePartsInOffset(asOf.date, asOf.offsetMinutes).period;
	let row = db.prepare("SELECT path, period_key FROM boards WHERE scope='project' AND scope_key=? AND period_key=? ORDER BY id DESC LIMIT 1").get(prefix, periodKey);
	let source = `boards.path(period=${periodKey})`;
	if (row === void 0) {
		row = db.prepare("SELECT path, period_key FROM boards WHERE scope='project' AND scope_key=? AND status='open' ORDER BY period_key DESC, id DESC LIMIT 1").get(prefix);
		source = "boards.path(最近 open 板)";
	}
	if (row === void 0) return [PST_DASH_NO_BOARD, "boards(无板)"];
	const relPath = String(row.path ?? "");
	const name = path.basename(relPath);
	let rel;
	if (homeRoot !== null) {
		const target = path.join(homeRoot, relPath);
		const base = path.join(homeRoot, "10-项目库");
		rel = path.relative(base, target).split(path.sep).join("/");
	} else rel = relPath.startsWith("10-项目库/") ? relPath.slice(7) : relPath;
	return [`[${name}](${rel})`, source];
}
/** 主责 AI（开档期优先 → 近 7 天 owner 回退 → `—（无开档期）`）。 */
function ownersOf(db, prefix, asOf) {
	const names = db.prepare("SELECT citizen_id, MIN(from_ts) AS first FROM project_stints WHERE project=? AND to_ts IS NULL GROUP BY citizen_id ORDER BY first ASC, citizen_id ASC").all(prefix).map((row) => displayOf(db, String(row.citizen_id)));
	if (names.length > 0) {
		if (names.length === 1) return [names[0], "project_stints(开档期)"];
		return [`${names.slice(0, 2).join("、")}等 ${names.length} 人`, "project_stints(开档期)"];
	}
	const sinceMs = asOf.date.getTime() - 7 * 864e5;
	const since = utcStamp(new Date(sinceMs));
	const recentNames = db.prepare("SELECT owner, MAX(recorded_at) AS last FROM code_index WHERE project=? AND recorded_at>=? AND owner IS NOT NULL AND TRIM(owner)<>'' GROUP BY owner ORDER BY last DESC, owner ASC LIMIT 2").all(prefix, since).map((row) => displayOf(db, String(row.owner)));
	if (recentNames.length > 0) return [recentNames.join("、"), "code_index.owner(recent_owner)"];
	return [PST_DASH_NO_OWNER, "none"];
}
/** 未了事项（窗口内命中 8 词词表的条数）→ `(n, 来源)`。 */
function openLoopsOf(db, prefix, asOf) {
	const sinceMs = asOf.date.getTime() - PST_OPEN_WINDOW_DAYS * 864e5;
	const since = utcStamp(new Date(sinceMs));
	db.prepare("SELECT 1 FROM code_index LIMIT 1").all();
	return [openLoopRows(db, since, 1e4, /* @__PURE__ */ new Set(), prefix).length, "code_index.title(8 词表)"];
}
/** 正文目录里"不属于任何 registry 前缀"的一级目录（只计数不列表）。 */
function nonPrefixDirs(db, dataRoot) {
	const base = path.join(dataRoot, "projects");
	if (pathExistsDir(base) === false) return {
		count: 0,
		names: []
	};
	let prefixes;
	try {
		prefixes = new Set(db.prepare("SELECT prefix FROM code_registry").all().map((row) => String(row.prefix)));
	} catch {
		return {
			count: 0,
			names: []
		};
	}
	const extra = listDirNames(base).filter((name) => !prefixes.has(name)).sort();
	return {
		count: extra.length,
		names: extra
	};
}
/** 目录存在且是目录？（读不到一律当"没有"）。 */
function pathExistsDir(target) {
	try {
		return statSync(target).isDirectory();
	} catch {
		return false;
	}
}
/** 目录下的名字（读不到 → 空）。 */
function listDirNames(target) {
	try {
		return readdirSync(target);
	} catch {
		return [];
	}
}
/**
* 六列 + 每列来源 + 降级清单（`project_status_rows` 的同形复刻）。
*
* 降级的粒度是**列**而不是整表：某一张表的列坏了，只该让那一格变 `—（数据源不可用）`，
* 其余五格照样有值——否则一次局部故障会让整张表看起来像"没有项目"。
*/
function projectStatusRows(db, options) {
	const asOf = options.asOf;
	const homeRoot = options.homeRoot ?? defaultHomeRoot(options.dataRoot);
	const degraded = [];
	const rows = [];
	let registry = [];
	try {
		registry = registryRows(db);
	} catch (error) {
		degraded.push({
			source: "code_registry",
			reason: describeError(error)
		});
		registry = [];
	}
	for (const item of registry) {
		const row = {
			prefix: item.prefix,
			namespace: item.namespace,
			status: item.status,
			status_label: statusLabel(item.status),
			last_active: null,
			last_active_source: null,
			board_entry: null,
			board_entry_source: null,
			owners: null,
			owners_source: null,
			open_loops: null,
			open_loops_source: null,
			sources: {}
		};
		if (![
			"active",
			"closed",
			"retired"
		].includes(item.status)) row.status_label = `${item.status !== "" ? item.status : "—"}（未登记状态）`;
		const columns = [
			{
				key: "last_active",
				run: () => lastActiveOf(db, item.prefix, asOf)
			},
			{
				key: "board_entry",
				run: () => boardEntryOf(db, item.prefix, asOf, homeRoot)
			},
			{
				key: "owners",
				run: () => ownersOf(db, item.prefix, asOf)
			},
			{
				key: "open_loops",
				run: () => openLoopsOf(db, item.prefix, asOf)
			}
		];
		for (const column of columns) try {
			const [value, source] = column.run();
			if (column.key === "open_loops") row.open_loops = Number(value);
			else if (column.key === "last_active") row.last_active = String(value);
			else if (column.key === "board_entry") row.board_entry = String(value);
			else row.owners = String(value);
			row[`${column.key}_source`] = source;
			row.sources[column.key] = source;
		} catch (error) {
			degraded.push({
				source: `${column.key}:${item.prefix}`,
				reason: describeError(error)
			});
			if (column.key === "open_loops") row.open_loops = null;
			else row[column.key] = PST_DASH_UNAVAILABLE;
			row[`${column.key}_source`] = "unavailable";
			row.sources[column.key] = "unavailable";
		}
		row.sources.status = "code_registry.status";
		rows.push(row);
	}
	const retiredAll = rows.filter((row) => row.status === "retired");
	const includeRetired = options.includeRetired === true;
	const visible = includeRetired ? rows : rows.filter((row) => row.status !== "retired");
	const retired = includeRetired ? [] : retiredAll;
	let nonPrefix;
	try {
		nonPrefix = nonPrefixDirs(db, options.dataRoot);
	} catch (error) {
		degraded.push({
			source: "data_root/projects",
			reason: describeError(error)
		});
		nonPrefix = {
			count: 0,
			names: []
		};
	}
	const keywords = ATTENTION_OPEN_KEYWORDS.join("、");
	const footnotes = [
		`窗口：最近活跃与未了事项都只看 \`as-of\` 前 ${PST_OPEN_WINDOW_DAYS} 天（与注入包的 \`ATTENTION_OPEN_WINDOW_DAYS\` 同口径）。`,
		"`—（无记忆）` = 该项目在 `code_index` 里没有任何 active 记忆；`—（今日无看板）` = 今日没有开过板（**不等于**没人干活，可能只是没签到）；`—（无开档期）` = 既无开档期、也没有近 7 天的记忆；`—` = 未了事项为 0。",
		`主责 AI 口径（Q4）：① 该项目当前开档期（\`project_stints.to_ts IS NULL\`）的公民，多名列前 2 + 「等 N 人」；② 无开档期时回退近 7 天内该项目记忆的 owner，并在来源里标 \`recent_owner\`；③ 都无 → \`—（无开档期）\`。`,
		`未了事项词表（${ATTENTION_OPEN_KEYWORDS.length} 个）：${keywords}。`,
		"项目清单以 `code_registry` 为准（裁决 Q6）。",
		`正文目录里不属于任何前缀的目录：${nonPrefix.count} 个` + (nonPrefix.count > 0 ? `（${nonPrefix.names.slice(0, 5).join(", ")}${nonPrefix.count > 5 ? "…" : ""}）` : "") + "——只计数不列表（裁决 Q6/Q18）。",
		"本页是投影：真相在 `data_root/index.sqlite`，重建命令 `fish project status`；同一 `--as-of` 重渲染逐字节一致（as-of 行除外）。"
	];
	return {
		as_of: asOfIso(asOf),
		home_root: homeRoot,
		columns: [...PST_COLUMNS],
		rows: visible,
		retired,
		retired_total: retiredAll.length,
		degraded,
		footnotes,
		non_prefix_dirs: nonPrefix
	};
}
/** 错误一行摘要（`_exc_text`：`TypeName: message`）。 */
function describeError(error) {
	if (error instanceof Error) return `${error.name}: ${error.message}`;
	return String(error);
}
/**
* 门面根解析的**只读子集**（`resolve_home_root` 的规则②/③）。
*
* 为什么只复刻两条：①/⓪ 是"显式指定"（影子期没有 `--home`，也不该替人改落点）；
* ②/③ 才是"没指定时落在哪"——项目状态表的相对链接要靠它，否则 `board_entry` 会算错基准。
*/
function defaultHomeRoot(dataRoot) {
	const base = path.resolve(dataRoot);
	const name = path.basename(base);
	const parent = path.dirname(base);
	if (name === "data" && path.basename(parent) === "p0") return path.join(path.dirname(parent), "fish-home");
	return path.join(parent, "fish-home");
}
/** 状态表单元格：`null`/空串 → `—`；0 条未了事项 → `—`（显式，不写 0）。 */
function pstCell(value) {
	if (value === null || value === void 0 || value === "") return "—";
	return cell(value);
}
/** 六列表的 markdown 渲染（`_pst_render_table` 的同形复刻）。 */
function renderProjectStatusTable(rows) {
	const lines = [`| ${PST_COLUMNS.join(" | ")} |`, `|${"---|".repeat(PST_COLUMNS.length)}`];
	if (rows.length === 0) {
		lines.push("| —（registry 里没有项目） | — | — | — | — | — |");
		return lines;
	}
	for (const row of rows) {
		const base = cell(row.namespace !== "" ? row.namespace : row.prefix) || "（未命名）";
		const name = row.namespace !== "" ? `${base}（\`${cell(row.prefix)}\`）` : `\`${cell(row.prefix)}\``;
		let loopsCell = row.open_loops === null || row.open_loops === 0 ? "—" : String(row.open_loops);
		if (row.open_loops === null) loopsCell = PST_DASH_UNAVAILABLE;
		lines.push(`| ${name} | ${cell(row.status_label) || "—"} | ${pstCell(row.last_active)} | ${row.board_entry !== null && row.board_entry !== "" ? row.board_entry : PST_DASH_NO_BOARD} | ${pstCell(row.owners)} | ${loopsCell} |`);
	}
	return lines;
}
/**
* `fish project status` 的**只读**回执：同一份标题/表格/降级段/脚注/折叠区，
* 但**不写文件**，末尾明写"终端里会写到哪"。
*/
function renderProjectStatus(result, options = {}) {
	const includeRetired = options.includeRetired === true;
	const lines = [
		"# 项目状态表",
		"",
		`> 生成器：\`fish project status\` ｜ as-of ${result.as_of}`,
		"> 每格的取值来源见 `--json` 的 `sources`；本页是投影，真相在 `data_root/index.sqlite`。",
		"> 六列严格顺序：项目 / 状态 / 最近活跃 / 看板入口 / 主责 AI / 未了事项。",
		"",
		...renderProjectStatusTable(result.rows),
		"",
		`主表 ${result.rows.length} 个项目` + (includeRetired ? "（含 retired，`--include-retired`）" : "")
	];
	for (const item of result.degraded) lines.push("", `## 数据源不可用：${item.source}`, "", `原因：\`${item.reason}\``, "该格显示 `—（数据源不可用）`，**不是 0**。修好数据源后重跑 `fish project status`。");
	lines.push("", "## 脚注", "");
	for (const note of result.footnotes) lines.push(`- ${note}`);
	if (result.retired.length > 0) lines.push("", `<details><summary>已退役（${result.retired.length}）</summary>`, "", ...renderProjectStatusTable(result.retired), "", "</details>", "");
	lines.push("", `（影子期不落盘：终端里 \`fish project status\` 会把同一份内容写到 \`${result.home_root}/${LAYOUT_STATUS_TABLE}\`；这里只读，一个文件都没写。要机器可比的形状请加 \`--json\`。）`);
	return lines.join("\n");
}
/** `project status --json` 的载荷（**字段顺序**与生产 `json.dumps` 一致）。 */
function projectStatusPayload(result) {
	return {
		as_of: result.as_of,
		home_root: result.home_root,
		columns: result.columns,
		rows: result.rows,
		retired: result.retired,
		degraded: result.degraded,
		footnotes: result.footnotes,
		retired_total: result.retired_total
	};
}
//#endregion
//#region src/ports/python.ts
/**
* Python 借用层：**只读转发到 `p0/fish.py`，但把库换成一次性快照**。
*
* ## 为什么不能"直接转发"
*
* 生产树里**每一条** `cmd_*` 都是这个开头（`p0/fish.py:980` 的 `connect()`）：
*
* ```python
* conn = connect(Path(args.root)); conn.executescript(SCHEMA); migrate(conn)
* ```
*
* `connect()` 会 `mkdir` + `PRAGMA journal_mode=WAL`，`executescript(SCHEMA)` 会跑一遍
* 建表脚本、`migrate(conn)` 可能补列。也就是说：**即使是一条纯读命令，Python 进程也
* 会以读写方式打开生产库并跑迁移**。影子期"零写入"是硬约束，所以"只读转发"不能
* 只靠命令分类来保证——必须让 Python **根本拿不到生产库的路径**。
*
* 做法：把 `index.sqlite` / `-wal` / `-shm` 三件套复制到临时目录（实测 15ms 量级），
* 用 `--root <临时目录>` 启动 Python。于是：
*   · 命令读到的数据 = 调用前那一刻的快照（`asOf` 会写进回显）；
*   · 它的 SCHEMA/migrate/审计追加全部落在**这次调用独有的临时副本**上，退出即删；
*   · 生产库从头到尾没有被任何写连接打开过（回显里给出前后 sha256 作为证据）。
*
* ## 边界
*
* 只给 `impl='proxy-readonly'` 且 `kind='read'` 的命令用（调用方断言）。
* 快照里没有的东西（`corpus.sqlite`、`_audit/hooks/*.jsonl`、fish-home 文件树写入）
* 一律不转发——那些命令在 `cli-map` 里是 `python-only`，理由写在表里。
*
* @module dsh-fish/ports/python
*/
/** 默认超时：Python 进程冷启动 ~0.3s，命令本身再慢也不该超过 8s（超了就是卡住了）。 */
const DEFAULT_PY_TIMEOUT_MS = 8e3;
/** 输出上限：命令面回显另有一层 60 行截断，这里防的是"一次跑出几百 MB"。 */
const MAX_BUFFER_BYTES = 4 * 1024 * 1024;
/** 取 Python 解释器：`FISH_PYTHON` 优先（与 gen-spec / 判据脚本同一口径）。 */
function resolvePythonBin() {
	const raw = (process.env.FISH_PYTHON ?? "").trim();
	return raw === "" ? "python" : raw;
}
/** 生产树里的 `fish.py`（不存在时抛错，别让 spawn 去报一个晦涩的 ENOENT）。 */
function resolveFishPy(fishHome) {
	const candidate = path.join(path.resolve(fishHome), "fish.py");
	if (!existsSync(candidate)) throw new FishError("SHADOW_UNSUPPORTED", `找不到生产树入口：${candidate}`, {
		hint: "设置 fishHome 配置项 / FISH_HOME 指向含 fish.py 的生产树",
		details: { fishHome }
	});
	return candidate;
}
/** 文件 sha256 前 12 位；读不到返回 `null`（有活跃写者时可能读不到，如实降级）。 */
function sha12$1(filePath) {
	try {
		return createHash("sha256").update(readFileSync(filePath)).digest("hex").slice(0, 12);
	} catch {
		return null;
	}
}
/**
* 复制一份只读快照到临时目录，并自证副本可用。
*
* 为什么是"复制三件套"而不是 `VACUUM INTO` / `backup()`：
*   · `VACUUM INTO` 在 `readOnly` 连接上直接报 `attempt to write a readonly database`
*     （实测：`PRAGMA query_only=1` 之后连 INTO 都不许）；
*   · `node:sqlite` 的 `backup()` 是**异步**的，而斜杠命令的 handler 是同步的；
*   · 三件套字节复制是同步的、15ms 量级，且 SQLite 打开副本时会自己应用 `-wal`。
*     最坏情况（复制期间正发生 checkpoint）得到的是**稍旧**的快照，不是坏库——
*     这里再用 `PRAGMA integrity_check` 兜一道，不 `ok` 就响亮报错。
*/
function copySnapshot(dbPath, options = {}) {
	if (!existsSync(dbPath)) throw new FishError("DB_UNAVAILABLE", `找不到生产主库：${dbPath}`, {
		hint: "设置 dbPath / FISH_DB，或确认 fishHome 指向含 data/index.sqlite 的目录",
		details: { dbPath }
	});
	const dir = options.dir ?? mkdtempSync(path.join(os.tmpdir(), "fish-proxy-"));
	const files = [];
	try {
		for (const suffix of [
			"",
			"-wal",
			"-shm"
		]) {
			const from = `${dbPath}${suffix}`;
			if (!existsSync(from)) continue;
			copyFileSync(from, path.join(dir, `index.sqlite${suffix}`));
			files.push(`index.sqlite${suffix}`);
		}
		const snapshotDb = path.join(dir, "index.sqlite");
		let integrity = "unchecked";
		const probe = new DatabaseSync(snapshotDb, { readOnly: true });
		try {
			integrity = probe.prepare("PRAGMA integrity_check").all().map((row) => String(row.integrity_check ?? "")).join(";") || "empty";
		} finally {
			probe.close();
		}
		if (integrity !== "ok") throw new FishError("DB_UNAVAILABLE", `快照副本完整性检查未通过：${integrity}`, {
			hint: "重跑一次（复制期间正好有 checkpoint 时会拿到旧副本；连续失败请先确认生产库是否损坏）",
			details: {
				dbPath,
				dir,
				integrity
			}
		});
		return {
			dir,
			dbPath: snapshotDb,
			files,
			bytes: statSync(snapshotDb).size,
			asOf: (/* @__PURE__ */ new Date()).toISOString(),
			integrity
		};
	} catch (error) {
		rmSync(dir, {
			recursive: true,
			force: true
		});
		throw error;
	}
}
/**
* 在**只读快照**上跑一条 `fish.py` 命令。
*
* 调用方必须自己保证"这条命令是只读的"（`cli-map` 的 `impl='proxy-readonly'`）；
* 本函数额外拒绝 `--apply` / `--write` 这类强写开关——双保险，因为"分类标错"
* 与"参数拼错"是两种不同的事故。
*/
function runFishCliOnSnapshot(options) {
	const timeoutMs = options.timeoutMs ?? 8e3;
	const forbidden = options.args.find((arg) => arg === "--apply" || arg === "--force" || arg === "--write");
	if (forbidden !== void 0) throw new FishError("SHADOW_READ_ONLY", `只读转发不许带写开关：${forbidden}`, {
		hint: "这条命令的写形态请在终端里跑（`fish …`），影子期不在生产库执行",
		details: { args: options.args }
	});
	const fishPy = resolveFishPy(options.fishHome);
	const productionShaBefore = sha12$1(options.dbPath);
	let snapshot = null;
	try {
		snapshot = copySnapshot(options.dbPath);
		const argv = [
			fishPy,
			"--root",
			snapshot.dir,
			...options.args
		];
		const child = spawnSync(resolvePythonBin(), argv, {
			encoding: "utf8",
			timeout: timeoutMs,
			maxBuffer: MAX_BUFFER_BYTES,
			windowsHide: true,
			env: {
				...process.env,
				PYTHONIOENCODING: "utf-8",
				PYTHONUTF8: "1"
			}
		});
		const timedOut = child.error !== void 0 && child.error.code === "ETIMEDOUT";
		return {
			ok: child.error === void 0 && child.status === 0,
			argv,
			exitCode: child.status,
			signal: child.signal,
			stdout: typeof child.stdout === "string" ? child.stdout : "",
			stderr: typeof child.stderr === "string" ? child.stderr : "",
			timedOut,
			snapshot,
			failure: child.error === void 0 ? child.status === 0 ? null : `fish.py 退出码 ${String(child.status)}` : `启动/执行失败：${child.error.message}`,
			productionShaBefore,
			productionShaAfter: sha12$1(options.dbPath)
		};
	} catch (error) {
		return {
			ok: false,
			argv: [],
			exitCode: null,
			signal: null,
			stdout: "",
			stderr: "",
			timedOut: false,
			snapshot,
			failure: error instanceof Error ? error.message : String(error),
			productionShaBefore,
			productionShaAfter: sha12$1(options.dbPath)
		};
	} finally {
		if (snapshot !== null) rmSync(snapshot.dir, {
			recursive: true,
			force: true
		});
	}
}
//#endregion
//#region src/surfaces/output.ts
/**
* 命令面的**回显收尾**：截断 + 落盘 + 路径回显。
*
* 为什么截断必须回显路径：斜杠命令的返回值会整段进对话区，一条 `health` 在断链几百条时
* 会把上下文吃掉一大块；但**"看不到"与"没有"必须能区分**，所以超限时一定要给出
* "完整输出在哪"。
*
* 文件名用**内容哈希**而不是时间戳：同一份输出连跑两次要得到逐字节相同的文本
* （`/fish hosts dry-run` 的幂等判据就靠这个），时间戳会把幂等变成"每次都不同"。
*
* @module dsh-fish/surfaces/output
*/
/** 命令输出超过这个行数就落盘，只在回显里给路径（命令面不该刷屏）。 */
const OUTPUT_LINE_LIMIT = 60;
/** 落盘目录与文件名规则（内容哈希 → 同一份输出永远是同一个文件名）。 */
function outputLogPath(packageRoot, label, text) {
	const digest = createHash("sha256").update(text).digest("hex").slice(0, 8);
	return path.join(path.resolve(packageRoot), ".tmp", `fish-${label}-${digest}.log`);
}
/**
* 截断过长的命令输出（超过 `OUTPUT_LINE_LIMIT` 行就落盘）。
* @returns `text` 回显文本（超限时带截断说明与完整路径）、`logPath` 落盘路径或 `null`
*/
function finalizeOutput(text, label, packageRoot) {
	const lines = text.split("\n");
	if (lines.length <= 60) return {
		text,
		logPath: null
	};
	const logPath = outputLogPath(packageRoot, label, text);
	mkdirSync(path.dirname(logPath), { recursive: true });
	writeFileSync(logPath, text, "utf8");
	return {
		text: `${lines.slice(0, 60).join("\n")}\n…（共 ${lines.length} 行，已截断到 60 行；完整输出：${logPath}）`,
		logPath
	};
}
/** 给较长回显加一行来源说明（命令面必须自解释：这段是从哪个库、哪个口径读出来的）。 */
function withHeader(header, body) {
	return `${header}\n${body}`;
}
//#endregion
//#region src/surfaces/cli.ts
/** 一条 `ts` 命令接受的开关（多一个就响亮失败：拼错的开关等于什么都没做）。 */
const TS_FLAGS = {
	recall: [
		"project",
		"code",
		"tags",
		"limit",
		"scope",
		"include-superseded",
		"token"
	],
	whoami: [
		"token",
		"session-key",
		"all"
	],
	healthcheck: []
};
/** 写类开关：连只读命令都不许带（`--apply` 一出现就说明调用方想写）。 */
const WRITE_FLAGS = [
	"apply",
	"write",
	"force",
	"overwrite"
];
/** 取字符串开关；`true`（只写了开关名）按空串处理。 */
function flagString$1(flags, name) {
	const value = flags[name];
	if (value === void 0) return void 0;
	return value === true ? "" : value;
}
/** 取整数开关（非法值响亮失败）。 */
function flagInt$1(flags, name) {
	const raw = flagString$1(flags, name);
	if (raw === void 0) return void 0;
	if (raw === "") throw new FishError("COMMAND_USAGE", `--${name} 需要一个整数`);
	const parsed = Number.parseInt(raw, 10);
	if (Number.isNaN(parsed)) throw new FishError("COMMAND_USAGE", `--${name} 需要整数，收到 ${JSON.stringify(raw)}`);
	return parsed;
}
/** 逗号/空白分隔的列表开关。 */
function flagList$1(flags, name) {
	const raw = flagString$1(flags, name);
	if (raw === void 0) return void 0;
	const items = raw.split(/[,\s]+/).filter((item) => item !== "");
	return items.length > 0 ? items : void 0;
}
/** 把 flags 还原成命令行参数（`--k=v` / `--k`）。 */
function flagsToArgv(flags) {
	const argv = [];
	for (const key of Object.keys(flags).sort()) {
		const value = flags[key];
		if (value === void 0) continue;
		argv.push(value === true ? `--${key}` : `--${key}=${value}`);
	}
	return argv;
}
/** 断言"这条只读命令没带写开关"。 */
function assertNoWriteFlags(flags, path) {
	const hit = WRITE_FLAGS.filter((name) => flags[name] !== void 0);
	if (hit.length > 0) throw new FishError("SHADOW_READ_ONLY", `/fish ${path} 是只读命令，不接受写开关：${hit.map((name) => `--${name}`).join(" ")}`, { hint: "写形态请在终端里跑 `fish …`（影子期插件不写任何库/文件）" });
}
/** 断言"这条 ts 命令只用了它支持的开关"。 */
function assertKnownFlags(flags, path, allowed) {
	const unknown = Object.keys(flags).filter((name) => !allowed.includes(name));
	if (unknown.length > 0) throw new FishError("COMMAND_USAGE", `/fish ${path} 不认识这些开关：${unknown.map((name) => `--${name}`).join(" ")}`, { hint: `可用开关：${allowed.map((name) => `--${name}`).join(" / ") || "（无）"}` });
}
/** `--json` 未实现时的说明（不假装成功，也不假装这个开关不存在）。 */
function jsonUnsupported(path) {
	return new FishError("COMMAND_USAGE", `/fish ${path} 的 --json 形态影子期未实现（只做了与终端逐字节同形的文本形态）`, { hint: `要看 JSON 请在终端里跑 \`fish ${path} --json\`` });
}
/** 读 flag 的布尔开关（`--flag` 即可，`--flag=false` 也算显式给了值）。 */
function flagBool(flags, name) {
	const value = flags[name];
	if (value === void 0) return false;
	if (value === true) return true;
	return value.trim().toLowerCase() === "true" || value.trim() === "1";
}
/** 必填位置参数（缺了就响亮报用法错误，不静默当空串）。 */
function requirePositional(rest, path, what) {
	const value = rest.join(" ").trim();
	if (value === "") throw new FishError("COMMAND_USAGE", `/fish ${path} 缺少${what}`, { hint: `用法：/fish ${path} <${what}>` });
	return value;
}
/**
* T05 的只读能力表：**知识库 / 语料 / 项目坐标**三族。
*
* 为什么这些命令进 `ts` 而不是 `proxy-readonly`：它们的依赖不在主库快照里——
* 知识库读的是 fish-home 里的文件树、语料读的是独立的 `corpus.sqlite`。
* 转发只会在副本上得到"库不存在"的假空（T04 §5.2 的同一结论），所以只在 TS 侧实现。
*/
const READ_ONLY_COMMANDS = {
	"kb vault list": {
		flags: [],
		run: ({ deps }) => {
			const db = openReadOnly(deps.paths.dbPath);
			try {
				return renderKbVaultList(kbVaultList(db));
			} finally {
				db.close();
			}
		}
	},
	"kb tree": {
		flags: [
			"vault",
			"depth",
			"max-chars",
			"json"
		],
		run: ({ rest, flags, deps }) => {
			if (flagBool(flags, "json")) throw jsonUnsupported("kb tree");
			const db = openReadOnly(deps.paths.dbPath);
			try {
				return renderKbTree(kbTree(kbVaultGet(db, flagString$1(flags, "vault") ?? null), {
					rel: rest.join(" ").trim(),
					depth: flagInt$1(flags, "depth") ?? 2,
					maxChars: flagInt$1(flags, "max-chars") ?? 8e3
				}));
			} finally {
				db.close();
			}
		}
	},
	"kb open": {
		flags: [
			"vault",
			"max-chars",
			"allow-sensitive",
			"json"
		],
		run: ({ rest, flags, deps }) => {
			if (flagBool(flags, "json")) throw jsonUnsupported("kb open");
			const db = openReadOnly(deps.paths.dbPath);
			try {
				return renderKbOpen(kbOpen(kbVaultGet(db, flagString$1(flags, "vault") ?? null), requirePositional(rest, "kb open", "文件名"), {
					maxChars: flagInt$1(flags, "max-chars") ?? 64e3,
					allowSensitive: flagBool(flags, "allow-sensitive")
				}));
			} finally {
				db.close();
			}
		}
	},
	"kb search": {
		flags: [
			"vault",
			"limit",
			"json"
		],
		run: ({ rest, flags, deps }) => {
			if (flagBool(flags, "json")) throw jsonUnsupported("kb search");
			const db = openReadOnly(deps.paths.dbPath);
			try {
				return renderKbSearch(kbSearch(kbVaultGet(db, flagString$1(flags, "vault") ?? null), requirePositional(rest, "kb search", "关键词"), { limit: flagInt$1(flags, "limit") ?? 10 }));
			} finally {
				db.close();
			}
		}
	},
	"kb stats": {
		flags: ["vault", "json"],
		run: ({ flags, deps }) => {
			if (flagBool(flags, "json")) throw jsonUnsupported("kb stats");
			const db = openReadOnly(deps.paths.dbPath);
			try {
				return renderKbStats(kbStats(db, flagString$1(flags, "vault") ?? null));
			} finally {
				db.close();
			}
		}
	},
	"corpus search": {
		flags: ["limit", "json"],
		run: ({ rest, flags, deps }) => {
			const query = requirePositional(rest, "corpus search", "关键词");
			const result = corpusSearch(deps.paths.dataDir, query, { limit: flagInt$1(flags, "limit") ?? 8 });
			if (flagBool(flags, "json")) return JSON.stringify(result, null, 2);
			return renderCorpusSearch(result);
		}
	},
	"corpus read": {
		flags: ["max-chars"],
		run: ({ rest, flags, deps }) => {
			const pointer = requirePositional(rest, "corpus read", "门牌");
			const maxChars = flagInt$1(flags, "max-chars") ?? 8e3;
			try {
				return renderCorpusRead(corpusRead(deps.paths.dataDir, pointer, { maxChars }), maxChars);
			} catch (error) {
				if (error instanceof PointerError) return {
					text: renderPointerError(error),
					kind: "error"
				};
				throw error;
			}
		}
	},
	"corpus stats": {
		flags: ["host"],
		run: ({ flags, deps }) => {
			const host = flagString$1(flags, "host") ?? "all";
			return renderCorpusStats(corpusStats(deps.paths.dataDir, host === "all" ? null : host));
		}
	},
	"project status": {
		flags: [
			"as-of",
			"include-retired",
			"json",
			"home",
			"allow-external-home"
		],
		run: ({ flags, deps }) => {
			const asOf = parseAsOf(flagString$1(flags, "as-of"));
			const includeRetired = flagBool(flags, "include-retired");
			const db = openReadOnly(deps.paths.dbPath);
			try {
				const result = projectStatusRows(db, {
					dataRoot: deps.paths.dataDir,
					asOf,
					includeRetired,
					homeRoot: flagString$1(flags, "home") ?? null
				});
				if (flagBool(flags, "json")) return JSON.stringify(projectStatusPayload(result), null, 2);
				return renderProjectStatus(result, { includeRetired });
			} finally {
				db.close();
			}
		}
	},
	"alias list": {
		flags: [],
		run: ({ deps }) => {
			const db = openReadOnly(deps.paths.dbPath);
			try {
				return renderAliasList(db);
			} finally {
				db.close();
			}
		}
	},
	"workspace list": {
		flags: [],
		run: ({ deps }) => {
			const db = openReadOnly(deps.paths.dbPath);
			try {
				return renderWorkspaceList(db);
			} finally {
				db.close();
			}
		}
	}
};
/**
* 跑一条 T05 的只读命令。
*
* 错误分三类，各自有自己的回执形态（**不能混**）：
*   · `KbError` → `错误[<code>]：<message>`（与终端 `fish kb …` 同形，含"下一步"）；
*   · `PointerError` → `门牌错误（<code>）：…`（生产 `run_cli` 的同一段）；
*   · 其它 `FishError`（开关拼错 / 缺少参数 / `--json` 未实现）→ 常规错误格式。
*/
function runReadOnlyCommand(annotation, handler, rest, flags, deps) {
	assertKnownFlags(flags, annotation.path, handler.flags);
	assertNoWriteFlags(flags, annotation.path);
	try {
		const outcome = handler.run({
			rest,
			flags,
			deps
		});
		if (typeof outcome === "string") return {
			kind: "success",
			text: outcome
		};
		return {
			kind: outcome.kind,
			text: outcome.text
		};
	} catch (error) {
		if (error instanceof KbError) return {
			kind: "error",
			text: renderKbError(error)
		};
		if (error instanceof PointerError) return {
			kind: "error",
			text: renderPointerError(error)
		};
		throw error;
	}
}
/** `ts` 档的执行：全部复用 T01–T03 的领域层（同一份实现，不另写一份）。 */
function runTsCommand(annotation, rest, flags, deps) {
	const readOnly = READ_ONLY_COMMANDS[annotation.path];
	if (readOnly !== void 0) return runReadOnlyCommand(annotation, readOnly, rest, flags, deps);
	const allowed = TS_FLAGS[annotation.path] ?? [];
	assertKnownFlags(flags, annotation.path, allowed);
	assertNoWriteFlags(flags, annotation.path);
	if (annotation.path === "healthcheck") {
		const db = openReadOnly(deps.paths.dbPath);
		try {
			return {
				kind: "success",
				text: renderHealthcheck(healthcheckReadings(db, deps.paths.dataDir))
			};
		} finally {
			db.close();
		}
	}
	const token = flagString$1(flags, "token") ?? (process.env.FISH_SESSION_TOKEN ?? "").trim();
	const db = openReadOnly(deps.paths.dbPath);
	try {
		const identity = resolveIdentity(db, token);
		if (annotation.path === "whoami") {
			const { text } = renderWhoami(db, {
				token,
				sessionKey: flagString$1(flags, "session-key") ?? "",
				all: flags["all"] === true
			}, deps.paths.fishHome);
			return {
				kind: "success",
				text
			};
		}
		if (annotation.path === "recall") return {
			kind: "success",
			text: renderRecall(db, {
				query: rest.join(" "),
				code: flagString$1(flags, "code") ?? null,
				project: flagString$1(flags, "project") ?? null,
				tags: flagList$1(flags, "tags") ?? null,
				scope: flagString$1(flags, "scope") ?? "all",
				limit: flagInt$1(flags, "limit") ?? 10,
				includeSuperseded: flags["include-superseded"] === true
			}, identity)
		};
		throw new FishError("SHADOW_UNSUPPORTED", `fish ${annotation.path} 的 ts 实现不在本仓（裁决表与实现层不同步）`, { hint: "在 src/domain/cli.ts 把这条改成 python-only 并写清理由，或补上实现" });
	} finally {
		db.close();
	}
}
/** `proxy-readonly` 档：只读快照上的 Python 转发。 */
function runProxyCommand(annotation, rest, flags, deps) {
	assertNoWriteFlags(flags, annotation.path);
	const args = [
		...annotation.path.split(" "),
		...rest,
		...flagsToArgv(flags)
	];
	const result = runFishCliOnSnapshot({
		fishHome: deps.paths.fishHome,
		dbPath: deps.paths.dbPath,
		args
	});
	if (!result.ok) {
		const detail = [result.failure, result.stderr.trim()].filter((line) => line !== null && line !== "").join("\n");
		return {
			kind: "error",
			text: [
				`/fish ${annotation.path} 执行失败`,
				`  原因：${detail === "" ? "未知（fish.py 无输出）" : detail}`,
				`  argv：${result.argv.join(" ")}`,
				`  快照：${result.snapshot === null ? "未建立" : `${result.snapshot.dir}（${result.snapshot.bytes} B，as-of ${result.snapshot.asOf}）`}`,
				"  说明：命令在**只读快照副本**上执行，生产库没有参与本次调用。"
			].join("\n")
		};
	}
	const snapshot = result.snapshot;
	const header = [
		`fish ${annotation.path} —— 只读转发（快照 as-of ${snapshot?.asOf ?? "未知"}）`,
		`生产库 sha256：${result.productionShaBefore ?? "读不到"} → ${result.productionShaAfter ?? "读不到"}${result.productionShaBefore !== null && result.productionShaBefore === result.productionShaAfter ? "（未变）" : "（有变化：可能来自其它写者）"}`,
		snapshot === null ? "快照：未建立（异常路径）" : `快照：${snapshot.files.join(" + ")}，${snapshot.bytes} B，integrity=${snapshot.integrity}`,
		"（Python 侧会跑 SCHEMA/migrate 与审计追加——它们全部落在这次调用独有的临时副本上，退出即删）"
	].join("\n");
	const body = result.stdout.trimEnd();
	return {
		kind: "success",
		text: `${header}\n${body === "" ? "（命令没有输出）" : body}`
	};
}
/**
* 执行一次已经解析好的 CLI 调用（纯函数，判据脚本直接复用）。
* @param path 命中的命令路径（叶子）
* @param rest 路径之后的 token（原样透传）
*/
function runFishCli(path, rest, flags, deps) {
	const annotation = findCliAnnotation(path);
	if (annotation === null) return {
		kind: "error",
		text: `未知命令：fish ${path}\n\n${renderCliHelpText()}`
	};
	if (flags.help === true) return {
		kind: "success",
		text: renderCliHelpText(path)
	};
	try {
		switch (annotation.impl) {
			case "ts": {
				const result = runTsCommand(annotation, rest, flags, deps);
				const { text } = finalizeOutput(result.text ?? "", `cli-${annotation.path.replace(/\s+/g, "-")}`, deps.paths.packageRoot);
				return {
					kind: result.kind,
					text
				};
			}
			case "proxy-readonly": {
				const result = runProxyCommand(annotation, rest, flags, deps);
				const { text } = finalizeOutput(result.text ?? "", `cli-${annotation.path.replace(/\s+/g, "-")}`, deps.paths.packageRoot);
				return {
					kind: result.kind,
					text
				};
			}
			case "unsupported-shadow": return {
				kind: "error",
				text: renderCliRefusal(annotation, annotation.reason)
			};
			case "python-only": return {
				kind: "error",
				text: renderCliGuidance(annotation, annotation.reason)
			};
			default: return {
				kind: "error",
				text: `命令 ${annotation.path} 的实现归属无法识别：${String(annotation.impl)}（cli-map 被人手改坏了？）`
			};
		}
	} catch (error) {
		const fishError = toFishError(error);
		deps.logger.error("CLI 命令失败", {
			path: annotation.path,
			code: fishError.code
		});
		return {
			kind: "error",
			text: fishError.format()
		};
	}
}
/**
* CLI 命令面的总入口：解析 → 分发。
* @param positional 命令 token（第一个是一级命令名）
*/
function handleCliInput(positional, flags, deps) {
	const route = resolveCliRoute(positional);
	if (route === null) return {
		kind: "error",
		text: `未知子命令：${positional.slice(0, 2).join(" ")}\n\n${renderCliHelpText()}`
	};
	if (route.annotation === null) return {
		kind: "success",
		text: renderCliHelpText(route.path)
	};
	const rest = route.rest;
	if (rest.length > 0) return runFishCli(route.path, rest, flags, deps);
	return runFishCli(route.path, [], flags, deps);
}
//#endregion
//#region src/hosts/registry.ts
/**
* 宿主登记表：**"哪份文件是哪个宿主的 hooks 配置"只在这里回答一次**。
*
* 事实源是生产树的 `p0/install_hooks.py`（`TARGETS` + `desired_hooks`）。本模块只复刻
* **读侧**需要的定位信息（家目录 / 文件路径 / 四个必装事件 / 每宿主的唯一采集器），
* 不复刻"目标配置长什么样"——那份事实在 dry-run 时由 Python 现场算出（见 `dryrun.ts`）。
*
* 为什么路径不许硬编码成 `C:\Users\15596\...`：那样换一台机器（或换个用户名）就静默
* 读到"没有配置"，而"没配"和"读了别处"在报告里长得一模一样。所以默认值走
* `os.homedir()`，且允许 `DSH_HOME` / `WORKBUDDY_HOME` 覆盖（与 install_hooks 同名）。
*
* @module dsh-fish/hosts/registry
*/
/** 四个必须安装的事件（`install_hooks.REQUIRED_EVENTS`，两宿主一致）。 */
const REQUIRED_EVENTS = [
	"SessionStart",
	"UserPromptSubmit",
	"PreToolUse",
	"PostToolUse"
];
/** 每宿主允许出现**恰好一个**采集器（两个都注册 = 双份行 + 双倍延迟）。 */
const COLLECTOR_SCRIPTS = ["fish_push_hook.py", "fish_posttool_hook.py"];
/** fish 的六个 hook 脚本（缺任何一个，安装器的 `--check` 就会红）。 */
const FISH_HOOK_SCRIPTS = [
	"fish_dsh_hook.py",
	"fish_session_hook.py",
	"fish_prompt_hook.py",
	"fish_pretool_hook.py",
	"fish_push_hook.py",
	"fish_posttool_hook.py"
];
/** 写类 matcher 必须覆盖的最小字面量集合。 */
const WRITE_MATCHER_REQUIRED = [
	"write",
	"Write",
	"edit",
	"Edit"
];
/** 环境变量读取（空串视为未设置）。 */
function envHome(name) {
	const raw = (process.env[name] ?? "").trim();
	return raw === "" ? "" : path.resolve(raw);
}
/**
* 解析两个宿主的位置（**只读**：不创建目录、不写文件）。
*
* 顺序：`DSH_HOME` / `WORKBUDDY_HOME` > `os.homedir()` 下的默认位置。
* 生产树 `install_hooks.py` 把默认值写死成 `C:\Users\15596\...`；本机两者等价，
* 但换机器时硬编码会静默指错——这是与生产的一处**有意偏差**（NOTES 里登记）。
*/
function resolveHostSpecs() {
	const dshHome = envHome("DSH_HOME") || path.join(homedir(), ".dsh");
	const workbuddyHome = envHome("WORKBUDDY_HOME") || path.join(homedir(), ".workbuddy");
	return [{
		id: "dsh",
		label: "DSH（DeepSeek Harness）",
		home: dshHome,
		homeEnv: "DSH_HOME",
		configPath: path.join(dshHome, "hooks.json"),
		collectorScript: "fish_push_hook.py",
		note: "hooks 是启动读一次；matcher 非法会让**整份配置被拒**（四个事件全部不注册）"
	}, {
		id: "workbuddy",
		label: "WorkBuddy",
		home: workbuddyHome,
		homeEnv: "WORKBUDDY_HOME",
		configPath: path.join(workbuddyHome, "settings.json"),
		collectorScript: "fish_posttool_hook.py",
		note: "与别的插件**共用** settings.json：写入必须合并，不许整层替换"
	}];
}
/** 按 id 取宿主（未知 id 抛错——静默返回空会让"没有这个宿主"变成"这个宿主没配置"）。 */
function findHostSpec(id, specs = resolveHostSpecs()) {
	const spec = specs.find((item) => item.id === id);
	if (spec === void 0) throw new Error(`未知宿主 ${JSON.stringify(id)}（可用：${specs.map((item) => item.id).join(" / ")}）`);
	return spec;
}
//#endregion
//#region src/hosts/health.ts
/**
* 宿主 hooks **心跳健康度**：把"配置里写着"与"真的触发过"分开。
*
* 事实源是生产树的 `fish_activity.heartbeat()`：每 `(host, event)` 一个文件，
* 路径 `<data>/_audit/hooks/heartbeat-<host>-<event>.json`，内容形如
* `{"ts":"2026-10-27T18:30:42+08:00","ts_utc":"…","event":"PostToolUse","host":"dsh",…}`。
*
* ## 为什么必须看心跳而不是只看配置
*
* hooks 是"启动读一次"的：配置写错了**不会报错**，只会"某个事件从此不再触发"，
* 症状是"某个功能悄悄没了"。而两种失败在配置里长得一模一样：
*   · 事件不支持 → 宿主忽略该键，其余照常；
*   · matcher 非法 → **整份配置被拒**，四个事件一个都不注册。
* 所以判活只有一条路：证明 hook **真的被执行过**（时间戳出现，且是真实触发的形态）。
*
* ## 一条纪律：不信 mtime，看内容
*
* 生产实现自己写明了「看内容，不看 mtime」：`{"bad_payload": true}` 那种行是手工演练
* 喂坏 JSON 留下的，不算"事件真的触发过"。本模块照同一口径判。
*
* @module dsh-fish/hosts/health
*/
/** 心跳目录（相对 fish 数据目录）：与 `fish_activity.HEARTBEAT_RELDIR` 同一路径。 */
const HEARTBEAT_RELDIR = ["_audit", "hooks"];
/** 陈旧阈值：2 小时（与 `03-调研` §5「hooks 活性 4/4 心跳 mtime ≤2h」同一口径）。 */
const HEARTBEAT_STALE_MS = 7200 * 1e3;
/** 心跳目录的绝对路径。 */
function heartbeatDir(dataDir) {
	return path.join(dataDir, ...HEARTBEAT_RELDIR);
}
/** 心跳文件名（与 `fish_activity.heartbeat()` 一致）。 */
function heartbeatFileName(host, event) {
	return `heartbeat-${host}-${event}.json`;
}
/** 解析 ISO 时间戳；失败返回 `null`。 */
function parseIso(raw) {
	if (typeof raw !== "string" || raw.trim() === "") return null;
	const ms = Date.parse(raw.trim());
	return Number.isFinite(ms) ? ms : null;
}
/** 读一条心跳（文件不存在也给一条读数——"缺失"必须能被看见，不能只是数组里少一项）。 */
function readHeartbeat(dataDir, host, event, now) {
	const base = {
		host,
		event,
		filePath: path.join(heartbeatDir(dataDir), heartbeatFileName(host, event))
	};
	if (!existsSync(base.filePath)) return {
		...base,
		exists: false,
		ts: null,
		ageMs: null,
		stale: true,
		badPayload: false,
		facts: {},
		problem: null
	};
	let payload;
	try {
		payload = JSON.parse(readFileSync(base.filePath, "utf8").replace(/^\uFEFF/, ""));
	} catch (error) {
		return {
			...base,
			exists: true,
			ts: null,
			ageMs: null,
			stale: true,
			badPayload: false,
			facts: {},
			problem: `心跳文件不是合法 JSON：${error instanceof Error ? error.message : String(error)}`
		};
	}
	const stamp = parseIso(payload.ts_utc) ?? parseIso(payload.ts);
	const ageMs = stamp === null ? null : Math.max(0, now - stamp);
	const badPayload = payload.bad_payload === true;
	const facts = {};
	for (const [key, value] of Object.entries(payload)) {
		if (key === "ts" || key === "ts_utc" || key === "event" || key === "host") continue;
		facts[key] = value;
	}
	return {
		...base,
		exists: true,
		ts: typeof payload.ts_utc === "string" ? payload.ts_utc : typeof payload.ts === "string" ? payload.ts : null,
		ageMs,
		stale: ageMs === null || ageMs > 72e5 || badPayload,
		badPayload,
		facts,
		problem: null
	};
}
/** 人类可读的时长。 */
function formatAge(ageMs) {
	if (ageMs === null) return "未知";
	const minutes = Math.round(ageMs / 6e4);
	if (minutes < 60) return `${minutes} 分钟`;
	const hours = Math.floor(minutes / 60);
	const rest = minutes % 60;
	if (hours < 48) return `${hours} 小时${rest === 0 ? "" : ` ${rest} 分`}`;
	return `${Math.floor(hours / 24)} 天`;
}
/**
* 判活：每宿主 × 4 事件各一条读数，并对三类矛盾给告警。
*
* @param dataDir fish 数据目录（含 `_audit/hooks/`）
* @param specs 宿主登记项
* @param configured `host → 配置里存在的事件名`（来自 `probe.ts` 的读数）
* @param now 当前时刻（毫秒；测试可注入）
*/
function checkHostHealth(dataDir, specs, configured, now) {
	const heartbeats = [];
	const alerts = [];
	for (const spec of specs) {
		const declared = configured[spec.id] ?? [];
		for (const event of REQUIRED_EVENTS) {
			const reading = readHeartbeat(dataDir, spec.id, event, now);
			heartbeats.push(reading);
			const configHas = declared.includes(event);
			if (configHas && !reading.exists) {
				alerts.push(`[${spec.id}/${event}] **配置里有这个事件，但心跳文件从未出现**——hook 一次都没跑过（排查顺序：宿主是否重启 / matcher 方言是否非法 / 脚本路径是否可执行）`);
				continue;
			}
			if (!configHas && reading.exists) {
				alerts.push(`[${spec.id}/${event}] 心跳存在但配置里没有该事件——hooks 配置被改过？`);
				continue;
			}
			if (configHas && reading.badPayload) {
				alerts.push(`[${spec.id}/${event}] 心跳只有 \`bad_payload\` 标记（手工演练），不算真实触发`);
				continue;
			}
			if (configHas && reading.stale) alerts.push(`[${spec.id}/${event}] 心跳陈旧：${formatAge(reading.ageMs)} 前（阈值 ${Math.round(HEARTBEAT_STALE_MS / 6e4)} 分钟）——最后一条 ${reading.ts ?? "时间戳读不出来"}`);
			if (reading.problem !== null) alerts.push(`[${spec.id}/${event}] ${reading.problem}`);
		}
	}
	const freshCount = heartbeats.filter((reading) => reading.exists && !reading.stale && !reading.badPayload).length;
	return {
		asOf: new Date(now).toISOString(),
		staleThresholdMs: HEARTBEAT_STALE_MS,
		heartbeats,
		alerts,
		expectedCount: specs.length * REQUIRED_EVENTS.length,
		freshCount
	};
}
/** 把健康度读数渲染成人读文本。 */
function renderHostHealth(result, dir) {
	const lines = [
		"# 跨宿主 hooks 心跳健康度（只读）",
		`心跳目录：${dir}`,
		`判活口径：每条 ≤ ${Math.round(result.staleThresholdMs / 6e4)} 分钟算新鲜；\`bad_payload\` 标记不算真实触发`,
		`读数时刻：${result.asOf}（新鲜 ${result.freshCount}/${result.expectedCount}）`,
		"",
		"| host | 事件 | 心跳 | 距今 | 判定 | 事实键 |",
		"|---|---|---|---|---|---|"
	];
	for (const reading of result.heartbeats) {
		const verdict = !reading.exists ? "**缺失**" : reading.badPayload ? "演练标记" : reading.stale ? "陈旧" : "新鲜";
		const facts = Object.entries(reading.facts).map(([key, value]) => `${key}=${typeof value === "object" ? JSON.stringify(value) : String(value)}`).join(" ");
		lines.push(`| ${reading.host} | ${reading.event} | ${reading.exists ? "有" : "无"} | ${formatAge(reading.ageMs)} | ${verdict} | ${facts || "—"} |`);
	}
	lines.push("");
	if (result.alerts.length === 0) lines.push("结论：✓ 配置与心跳一致，且全部新鲜");
	else {
		lines.push(`结论：✗ ${result.alerts.length} 条告警`);
		for (const alert of result.alerts) lines.push(`  · ${alert}`);
	}
	return lines.join("\n");
}
/** 列心跳目录里的全部文件（排障用：能看到 `warn-pending-*` 之类的旁证）。 */
function listHeartbeatDir(dataDir) {
	const dir = heartbeatDir(dataDir);
	if (!existsSync(dir)) return [];
	try {
		return readdirSync(dir).sort();
	} catch {
		return [];
	}
}
//#endregion
//#region src/hosts/probe.ts
/**
* 宿主 hooks **只读探针**：看清"现在配了什么"，一个字都不写。
*
* ## 为什么不复用 `install_hooks --check`
*
* `--check` 是**安装器的判据**（会 exit 2、会按 install_hooks.py 里那一刻的目标配置比），
* 而这里要的是**读数**：每宿主几条 fish 条目、每条挂在哪个事件、matcher 是什么、
* 脚本路径解析到哪、文件在不在。读数与判据是两件事——把读数做成 `--check` 的字符串解析，
* 下次安装器改一下输出格式，这里就会静默读错。
*
* ## 与 Python 侧的一致性
*
* `matcher_dialect` / `problems` 复刻的是 `p0/install_hooks.py` 的**判据语义**
* （F1 + Round 5/N1：只认纯字面备选这一档，锚定/括号/内联 flag 一律拒绝）。
* 复刻有漂移风险，所以 `test/hosts/dryrun-conformance.mjs` 拿 Python 现场的
* `validate_hooks()` 结果与本模块逐条比对（差异即红）——本模块只许落后被发现，不许悄悄漂开。
*
* @module dsh-fish/hosts/probe
*/
/** 纯字面备选：只允许 `[A-Za-z0-9_]` 与 `|`（宿主按字面比较、不编译正则）。 */
const LITERAL_ALTERNATIVES = /^[A-Za-z0-9_]+(?:\|[A-Za-z0-9_]+)*$/;
/** 内联 flag（`(?i)` / `(?i:…)`）：**策略性**拒绝——两个宿主的正则语义不可假设。 */
const INLINE_FLAG = /\(\?[A-Za-z]+[:)]/;
/** 读一份配置文本（保留 BOM/CRLF 判定；读不到返回 `null`）。 */
function readConfigText(configPath) {
	try {
		return readFileSync(configPath, "utf8");
	} catch {
		return null;
	}
}
/** 文件 sha256 前 12 位（读不到返回 `null`）。 */
function sha12(filePath) {
	try {
		return createHash("sha256").update(readFileSync(filePath)).digest("hex").slice(0, 12);
	} catch {
		return null;
	}
}
/**
* matcher 是否满足宿主方言契约（**原串**判定，不做 strip）。
*
* 判据必须用原串：宿主按原串解释，`" Write|write"` 的前导空格会让字面比较永不命中，
* 所以它也必须红（只对空值/纯空白判空）。
*/
function matcherDialect(matcher) {
	const raw = matcher === null || matcher === void 0 ? "" : String(matcher);
	if (raw.trim() === "") return {
		kind: "",
		reason: "matcher 为空（null/空串都算没写，但 PreToolUse 必须有）"
	};
	if (raw !== raw.trim()) return {
		kind: "",
		reason: `matcher 首尾含空白（${JSON.stringify(raw)}）：宿主按原串解释，字面比较会永不命中`
	};
	if (LITERAL_ALTERNATIVES.test(raw)) return {
		kind: "literal",
		reason: "纯字面备选（宿主不编译正则）"
	};
	if (INLINE_FLAG.test(raw)) return {
		kind: "",
		reason: "含内联 flag：只认纯字面备选，正则形态一律**策略性**拒绝（两宿主语义不可假设）"
	};
	return {
		kind: "",
		reason: "不是纯字面备选（只允许 [A-Za-z0-9_] 与 |）；锚定/括号等正则形态一律拒绝"
	};
}
/** matcher 是否覆盖写类工具的两种大小写。 */
function matcherCoversWriteTools(matcher) {
	if (matcherDialect(matcher).kind === "") return false;
	const literals = new Set((String(matcher ?? "").match(/[A-Za-z0-9_]+/g) ?? []).map((token) => token));
	return WRITE_MATCHER_REQUIRED.every((required) => literals.has(required));
}
/** 从一条命令串里取脚本路径（引号包裹优先，其次裸 token）。 */
function scriptPathOf(command) {
	const quoted = command.match(/"([^"]+\.py)"/g) ?? [];
	if (quoted.length > 0) return quoted[0].slice(1, -1);
	const bare = command.match(/(?:^|\s)([A-Za-z]:[\\/][^\s]+\.py|\/[^\s]+\.py|[\w.-]+\.py)/);
	return bare === null ? null : bare[1];
}
/** 事件表的形态判定（`{hooks:{…}}` / 裸事件表）。 */
function hooksOf(document) {
	if (typeof document !== "object" || document === null) return {
		hooks: {},
		shape: "hooks"
	};
	const candidate = document;
	const nested = candidate.hooks;
	if (typeof nested === "object" && nested !== null && !Array.isArray(nested)) return {
		hooks: nested,
		shape: "hooks"
	};
	const values = Object.values(candidate);
	if (values.length > 0 && values.every((value) => Array.isArray(value))) return {
		hooks: candidate,
		shape: "bare"
	};
	return {
		hooks: {},
		shape: "hooks"
	};
}
/** 遍历配置里的所有 (event, group, hook)。 */
function* iterHooks(hooks) {
	for (const [event, groups] of Object.entries(hooks)) {
		if (!Array.isArray(groups)) continue;
		for (const group of groups) {
			if (typeof group !== "object" || group === null) continue;
			const record = group;
			const list = record.hooks;
			if (!Array.isArray(list)) continue;
			for (const hook of list) {
				if (typeof hook !== "object" || hook === null) continue;
				yield {
					event,
					group: record,
					hook
				};
			}
		}
	}
}
/** 一条 hook 是不是 fish 的（命令里带 fish 的 hook 脚本名）。 */
function isFishScript(scriptName) {
	return FISH_HOOK_SCRIPTS.includes(scriptName) || scriptName.startsWith("fish_");
}
/**
* 探一个宿主（**只读**：只 `readFileSync`/`statSync`）。
* @param spec 宿主登记项
* @param fishHome fish 家目录（判"脚本文件在不在"用；install_hooks 用 `folder` 参数表达同一件事）
*/
function probeHost(spec, fishHome) {
	const text = readConfigText(spec.configPath);
	if (text === null) return {
		id: spec.id,
		label: spec.label,
		configPath: spec.configPath,
		exists: false,
		bytes: 0,
		sha256_12: null,
		shape: "missing",
		events: [],
		requiredEventsPresent: [],
		otherEvents: [],
		fishEntries: [],
		problems: [`配置文件不存在：${spec.configPath}`, `（需要安装时跑 \`python install_hooks.py --apply --host ${spec.id}\`，然后**重启宿主**）`],
		note: spec.note
	};
	let document = null;
	let parseError = null;
	try {
		document = JSON.parse(text.replace(/^\uFEFF/, ""));
	} catch (error) {
		parseError = error instanceof Error ? error.message : String(error);
	}
	const { hooks, shape } = hooksOf(document);
	const events = Object.keys(hooks);
	const entries = [];
	for (const { event, group, hook } of iterHooks(hooks)) {
		const command = typeof hook.command === "string" ? hook.command : "";
		const scriptPath = command === "" ? null : scriptPathOf(command);
		const script = scriptPath === null ? null : scriptPath.split(/[\\/]/).pop() ?? null;
		if (script === null || !isFishScript(script)) continue;
		const matcher = typeof group.matcher === "string" ? group.matcher : null;
		entries.push({
			event,
			matcher,
			command,
			script,
			scriptPath,
			scriptExists: scriptPath !== null && existsSync(scriptPath),
			timeoutSec: typeof hook.timeout === "number" ? hook.timeout : null,
			dialect: matcherDialect(matcher),
			collector: COLLECTOR_SCRIPTS.includes(script)
		});
	}
	const problems = [];
	if (parseError !== null) problems.push(`配置不是合法 JSON：${parseError}`);
	for (const event of REQUIRED_EVENTS) if (!events.includes(event)) problems.push(`缺事件 ${event}`);
	const collectors = entries.filter((entry) => entry.collector && REQUIRED_EVENTS.includes(entry.event));
	if (collectors.length !== 1) {
		const detail = collectors.length === 0 ? "无" : collectors.map((entry) => `${entry.event}:${entry.script ?? "?"}`).join("、");
		problems.push(`采集器数量应为 1，实际 ${collectors.length}（${detail}）`);
	}
	const preTool = entries.filter((entry) => entry.event === "PreToolUse");
	if (preTool.length !== 1) problems.push(`PreToolUse 应有 1 组 fish hook，实际 ${preTool.length}`);
	else {
		const entry = preTool[0];
		if (entry.dialect.kind === "") problems.push(`PreToolUse matcher 不满足宿主方言契约（${entry.dialect.reason}）：${JSON.stringify(entry.matcher)}`);
		else if (!matcherCoversWriteTools(entry.matcher)) problems.push(`PreToolUse matcher 未覆盖写类工具大小写：${JSON.stringify(entry.matcher)}`);
		if (entry.scriptPath === null) problems.push("PreToolUse 命令里没有 .py 脚本路径");
		else if (!entry.scriptExists) problems.push(`PreToolUse 脚本不存在：${entry.scriptPath}`);
	}
	for (const entry of entries) {
		if (entry.event === "PreToolUse") continue;
		if (entry.matcher === null) continue;
		if (entry.dialect.kind === "") problems.push(`${entry.event} matcher 不满足宿主方言契约（${entry.dialect.reason}）：${JSON.stringify(entry.matcher)}`);
	}
	for (const script of FISH_HOOK_SCRIPTS) if (!existsSync(path.join(fishHome, script))) problems.push(`缺脚本 ${script}（fish-home = ${fishHome}）`);
	const requiredEventsPresent = REQUIRED_EVENTS.filter((event) => events.includes(event));
	return {
		id: spec.id,
		label: spec.label,
		configPath: spec.configPath,
		exists: true,
		bytes: (() => {
			try {
				return statSync(spec.configPath).size;
			} catch {
				return text.length;
			}
		})(),
		sha256_12: sha12(spec.configPath),
		shape,
		events,
		requiredEventsPresent: [...requiredEventsPresent],
		otherEvents: events.filter((event) => !REQUIRED_EVENTS.includes(event)),
		fishEntries: entries,
		problems,
		note: spec.note
	};
}
/** 探全部宿主。 */
function probeAllHosts(specs, fishHome) {
	return specs.map((spec) => probeHost(spec, fishHome));
}
/** 把探针读数渲染成人读文本（Markdown 表格，命令面直接贴）。 */
function renderHostProbe(results) {
	const lines = ["# 跨宿主 hooks 只读探针（不写任何文件）"];
	for (const result of results) {
		lines.push("");
		lines.push(`## ${result.label}（host=${result.id}）`);
		lines.push(`配置：${result.configPath}`);
		lines.push(result.exists ? `存在：是（${result.bytes} B，sha256=${result.sha256_12 ?? "读不到"}，形态=${result.shape}）` : "存在：**否**");
		if (result.exists) {
			lines.push(`事件：${result.events.join(" / ") || "（无）"}`);
			lines.push(`fish 条目：${result.fishEntries.length} 条｜非 fish 事件（合并时必须保留）：${result.otherEvents.join(" / ") || "（无）"}`);
			lines.push("");
			lines.push("| 事件 | matcher | 脚本 | 脚本存在 | 超时 | 方言 |");
			lines.push("|---|---|---|---|---|---|");
			for (const entry of result.fishEntries) lines.push(`| ${entry.event} | ${entry.matcher === null ? "（未挂）" : entry.matcher} | ${entry.script ?? "?"} | ${entry.scriptExists ? "✓" : "✗"} | ${entry.timeoutSec === null ? "—" : `${entry.timeoutSec}s`} | ${entry.dialect.kind === "" ? "✗" : "literal"} |`);
		}
		lines.push("");
		if (result.problems.length === 0) lines.push("读数结论：✓ 通过（4 事件 / 1 采集器 / matcher 方言契约 + 大小写齐备 / 6 脚本齐全）");
		else {
			lines.push(`读数结论：✗ ${result.problems.length} 个问题`);
			for (const problem of result.problems) lines.push(`  · ${problem}`);
		}
	}
	return lines.join("\n");
}
//#endregion
//#region src/hosts/dryrun.ts
/**
* hooks 安装 **dry-run**：只输出"目标文件会变成什么"，一个字都不写。
*
* ## 事实源是生产树的安装器，不是本模块
*
* 目标配置、合并、序列化（缩进/行尾/BOM）、以及"别人的事件会不会被删"全部由
* `scripts/probe/dump-hooks-plan.py` 现场问 `p0/install_hooks.py` 算出来；
* 本模块只做两件本地的事：**渲染 diff** + **自证没写**。
*
* 为什么值钱：`merge_hooks` 的语义是"只覆盖/新增 fish 负责的四个事件，其它事件一个字节
* 不动"。预览若说"只会多 4 条"、真装下去却删了别人的 `Stop`，那是这套安装器最该防的事故——
* 而"预览与执行不是同一份实现"正是那种事故的温床。所以这里**不重写合并逻辑**。
*
* ## 幂等
*
* 输出里**不含时间戳**（用两侧 sha256 + 计划摘要代替"什么时候算的"）：同一份配置连跑两次
* 必须逐字节相同，否则"diff 预览"就无法作为判据（判据 5）。
*
* @module dsh-fish/hosts/dryrun
*/
/** 计划的默认超时：Python 冷启动 + 读两份配置，15s 足够。 */
const HOOKS_PLAN_TIMEOUT_MS = 15e3;
/** 单个宿主的 diff 最多输出这么多行（再多就落盘，只回显路径）。 */
const DIFF_LINE_LIMIT = 80;
/** 文件 sha256（不存在返回 `null`）。 */
function sha256Of(filePath) {
	try {
		return createHash("sha256").update(readFileSync(filePath)).digest("hex");
	} catch {
		return null;
	}
}
/** 按行切（保留行尾判定；`\r\n` 与 `\n` 都算行边界）。 */
function splitLines(text) {
	if (text === "") return [];
	const lines = text.replace(/\r\n/g, "\n").split("\n");
	if (lines.length > 0 && lines[lines.length - 1] === "") lines.pop();
	return lines;
}
/**
* 行级 LCS 统一 diff（`@@ -a,b +c,d @@` 分块）。
*
* 为什么自己写：只要"两边一行都没变"就必须输出空 diff（而不是"整个文件都重写"），
* 否则预览里的噪声会盖住真正的风险点（例如"其它事件会被删掉"）。
* 文件规模（1400 行级别）跑 O(n·m) 的 LCS 是毫秒级，没必要引第三方依赖。
*/
function unifiedDiff(beforeText, afterText, options = {}) {
	const context = options.context ?? 3;
	const left = splitLines(beforeText);
	const right = splitLines(afterText);
	const n = left.length;
	const m = right.length;
	const width = m + 1;
	const lcs = new Int32Array((n + 1) * width);
	for (let i = n - 1; i >= 0; i -= 1) for (let j = m - 1; j >= 0; j -= 1) lcs[i * width + j] = left[i] === right[j] ? lcs[(i + 1) * width + (j + 1)] + 1 : Math.max(lcs[(i + 1) * width + j], lcs[i * width + (j + 1)]);
	/** 0 = 相同，1 = 删除（仅左侧），2 = 新增（仅右侧）。 */
	const ops = [];
	let i = 0;
	let j = 0;
	while (i < n && j < m) if (left[i] === right[j]) {
		ops.push({
			kind: 0,
			text: left[i]
		});
		i += 1;
		j += 1;
	} else if (lcs[(i + 1) * width + j] >= lcs[i * width + (j + 1)]) {
		ops.push({
			kind: 1,
			text: left[i]
		});
		i += 1;
	} else {
		ops.push({
			kind: 2,
			text: right[j]
		});
		j += 1;
	}
	while (i < n) {
		ops.push({
			kind: 1,
			text: left[i]
		});
		i += 1;
	}
	while (j < m) {
		ops.push({
			kind: 2,
			text: right[j]
		});
		j += 1;
	}
	if (!ops.some((op) => op.kind !== 0)) return "";
	const interesting = /* @__PURE__ */ new Set();
	ops.forEach((op, index) => {
		if (op.kind === 0) return;
		for (let k = Math.max(0, index - context); k <= Math.min(ops.length - 1, index + context); k += 1) interesting.add(k);
	});
	const sorted = [...interesting].sort((a, b) => a - b);
	const hunks = [];
	for (const index of sorted) {
		const current = hunks[hunks.length - 1];
		if (current !== void 0 && index === current[current.length - 1] + 1) current.push(index);
		else hunks.push([index]);
	}
	const out = [];
	for (const hunk of hunks) {
		const start = hunk[0];
		const end = hunk[hunk.length - 1];
		const slice = ops.slice(start, end + 1);
		const leftBefore = ops.slice(0, start).filter((op) => op.kind !== 2).length;
		const rightBefore = ops.slice(0, start).filter((op) => op.kind !== 1).length;
		const leftCount = slice.filter((op) => op.kind !== 2).length;
		const rightCount = slice.filter((op) => op.kind !== 1).length;
		out.push(`@@ -${leftBefore + 1},${leftCount} +${rightBefore + 1},${rightCount} @@`);
		for (const op of slice) out.push(`${op.kind === 0 ? " " : op.kind === 1 ? "-" : "+"}${op.text}`);
	}
	return out.join("\n");
}
/** 计划摘要：只取"计划形态"字段（不含原文），同一配置连跑两次必然相同。 */
function planDigest(payload) {
	const shape = payload.hosts.map((host) => ({
		host: host.host,
		path: host.path,
		exists: host.exists,
		originalSha256: host.originalSha256,
		plannedSha256: host.plannedSha256,
		changed: host.changed,
		preserved: host.preserved,
		dropped: host.dropped
	}));
	return createHash("sha256").update(JSON.stringify(shape)).digest("hex");
}
/**
* 跑一次 dry-run：问 Python 要计划 → 本地渲染 diff → 自证两个目标文件没变。
*
* @param fishHome 生产树（含 `fish.py` / `install_hooks.py`）
* @param options `packageRoot` 用来定位本仓的探针脚本；`hostConfigPaths` 是**本地独立**
*   记录的目标文件（与 Python 的口径应一致），用于"跑之前/跑之后"两次 sha256 对照——
*   自证不能只让被验证方（Python 计划）自己说"我没写"。
* @throws FishError `SHADOW_UNSUPPORTED` —— 生产树缺安装器 / Python 失败
* @throws FishError `SHADOW_READ_ONLY` —— 目标文件在 dry-run 前后不一致
*/
function runHooksDryRun(fishHome, options) {
	const timeoutMs = options.timeoutMs ?? 15e3;
	const script = path.join(options.packageRoot, "scripts", "probe", "dump-hooks-plan.py");
	const before = /* @__PURE__ */ new Map();
	for (const configPath of options.hostConfigPaths ?? []) before.set(configPath, sha256Of(configPath));
	const child = spawnSync(resolvePythonBin(), [script], {
		encoding: "utf8",
		timeout: timeoutMs,
		maxBuffer: 16 * 1024 * 1024,
		windowsHide: true,
		env: {
			...process.env,
			FISH_HOME: fishHome,
			PYTHONIOENCODING: "utf-8",
			PYTHONUTF8: "1"
		}
	});
	if (child.error !== void 0) throw new FishError("SHADOW_UNSUPPORTED", `跑 hooks 计划失败：${child.error.message}`, {
		hint: "确认 python 可用（FISH_PYTHON），或先用 `python install_hooks.py --check` 在终端核对",
		details: { script }
	});
	if (child.status !== 0) throw new FishError("SHADOW_UNSUPPORTED", `dump-hooks-plan.py 退出码 ${String(child.status)}`, {
		hint: "看导出的 stderr；安装器读不出配置时它会主动非 0",
		details: { stderr: (child.stderr || "").slice(0, 2e3) }
	});
	let payload;
	try {
		payload = JSON.parse(child.stdout);
	} catch (error) {
		throw new FishError("SHADOW_UNSUPPORTED", `dump-hooks-plan.py 输出不是 JSON：${error.message}`, { details: { head: (child.stdout || "").slice(0, 400) } });
	}
	if (payload.writeAttempts !== 0) throw new FishError("SHADOW_READ_ONLY", `安装器报告 writeAttempts=${payload.writeAttempts}（dry-run 必须为 0）`);
	const hosts = payload.hosts.map((host) => {
		const recorded = before.get(host.path);
		const afterSha = sha256Of(host.path);
		const unchanged = host.targetUnchanged && (recorded === void 0 || recorded === afterSha);
		return {
			host: host.host,
			path: host.path,
			exists: host.exists,
			beforeSha256: host.originalSha256,
			afterSha256: host.afterSha256,
			plannedSha256: host.plannedSha256,
			changed: host.changed,
			shape: host.shape,
			preserved: host.preserved,
			dropped: host.dropped,
			lineEndings: host.lineEndings,
			bom: host.bom,
			diff: unifiedDiff(host.originalText, host.plannedText),
			diffLines: splitLines(unifiedDiff(host.originalText, host.plannedText)).length,
			problems: host.problems,
			pythonDiffLines: host.diffLines,
			targetUnchanged: unchanged
		};
	});
	const touched = hosts.filter((host) => !host.targetUnchanged);
	if (touched.length > 0) throw new FishError("SHADOW_READ_ONLY", `dry-run 之后目标文件变了：${touched.map((host) => host.path).join(", ")}`, { hint: "立刻核对（`git diff` / 备份）：dry-run 承诺零写入" });
	return {
		fishHome,
		installerPath: payload.installer.path,
		installerSha12: payload.installer.sha256_12,
		python: payload.python,
		hosts,
		writeAttempts: 0,
		planSha256: planDigest(payload)
	};
}
/** 把结果渲染成人读文本（Markdown + 统一 diff）。 */
function renderHostsDryRun(result) {
	const lines = [
		"# hooks 安装 dry-run（**只输出，不写盘**）",
		`生产树：${result.fishHome}｜安装器：${result.installerPath}（sha256=${result.installerSha12}）`,
		`计划摘要：${result.planSha256.slice(0, 16)}…｜写入次数：${result.writeAttempts}`,
		"（同一份配置连跑两次，本段输出应逐字节相同；目标文件 sha256 前后相同）"
	];
	for (const host of result.hosts) {
		lines.push("");
		lines.push(`## ${host.host} → ${host.path}`);
		lines.push(`现状：${host.exists ? "存在" : "**不存在（会新建）**"}｜形态=${host.shape}｜行尾=${host.lineEndings}${host.bom ? "｜BOM=有" : ""}`);
		lines.push(`sha256：现状 ${host.beforeSha256?.slice(0, 16) ?? "—"} → 计划 ${host.plannedSha256.slice(0, 16)}`);
		lines.push(`变更：${host.changed ? "**会改**" : "无（已与目标一致）"}`);
		if (host.dropped.length > 0) lines.push(`⚠ 将被**删除**的其它事件：${host.dropped.join(", ")}（合并模式不会发生，仅 --force 才会）`);
		lines.push(`其它事件（原样保留）：${host.preserved.join(", ") || "（无）"}`);
		if (host.pythonDiffLines.length > 0) {
			lines.push("安装器判据：");
			for (const line of host.pythonDiffLines) lines.push(`  ${line}`);
		}
		if (host.problems.length > 0) {
			lines.push(`安装器报告的问题（${host.problems.length}）：`);
			for (const problem of host.problems) lines.push(`  · ${problem}`);
		}
		lines.push("");
		if (host.diff === "") lines.push("diff：（空——现状已等于目标配置）");
		else {
			const diffLines = host.diff.split("\n");
			const shown = diffLines.slice(0, 80);
			lines.push("```diff");
			for (const line of shown) lines.push(line);
			if (diffLines.length > shown.length) lines.push(`…（diff 共 ${diffLines.length} 行，此处只显示前 80 行）`);
			lines.push("```");
		}
		lines.push(`目标文件未变：${host.targetUnchanged ? "✓" : "✗"}（sha256 ${host.afterSha256?.slice(0, 16) ?? "—"}）`);
	}
	lines.push("");
	lines.push("边界：本命令**不实现 apply**（换心期才做，且需用户逐次确认）。");
	return lines.join("\n");
}
//#endregion
//#region src/surfaces/hosts.ts
/** `/fish hosts` 的子命令（顺序即回显顺序）。 */
const HOSTS_SUBCOMMANDS = [
	"probe",
	"health",
	"dry-run"
];
/** `hosts` 的用法文本。 */
const HOSTS_USAGE = `用法：/fish hosts <子命令>
  probe     只读探针：每个宿主的配置文件 / fish 条目 / matcher / 脚本路径（不写任何文件）
  health    心跳健康度：每宿主 × 4 事件的最近心跳与新鲜度（>2h 视为陈旧）
  dry-run   安装预览：目标文件"会变成什么"的统一 diff（**只输出，不写盘**）

边界：不做 apply（换心期才做）；不改别宿主任何非 fish 段落；不改 hooks.json 一个字节。`;
/** 取宿主登记项（可被测试注入）。 */
function hostsFor(deps) {
	return resolveHostSpecs();
}
/** 只允许这几个开关：多一个就说明调用方以为这里有别的能力（响亮失败胜过静默忽略）。 */
const ALLOWED_FLAGS = /* @__PURE__ */ new Set(["help"]);
/** 校验开关白名单。 */
function assertFlags(flags, subcommand) {
	const unknown = Object.keys(flags).filter((name) => !ALLOWED_FLAGS.has(name));
	if (unknown.length > 0) throw new Error(`/fish hosts ${subcommand} 不认识这些开关：${unknown.map((name) => `--${name}`).join(" ")}（只支持 --help）`);
}
/** `probe`：只读探针。 */
function renderHostsProbe(deps) {
	const results = probeAllHosts(hostsFor(deps), deps.paths.fishHome);
	const failed = results.filter((result) => result.problems.length > 0);
	return `${`只读探针：${results.length} 个宿主｜有问题的 ${failed.length} 个｜fish-home=${deps.paths.fishHome}
（本命令只 readFileSync/statSync；没有创建、没有改名、没有写入任何宿主配置）`}\n${renderHostProbe(results)}`;
}
/** `health`：心跳健康度。 */
function renderHostsHealth(deps, now = Date.now()) {
	const specs = hostsFor(deps);
	const probes = probeAllHosts(specs, deps.paths.fishHome);
	const configured = {};
	for (const probe of probes) configured[probe.id] = probe.requiredEventsPresent;
	return `${renderHostHealth(checkHostHealth(deps.paths.dataDir, specs, configured, now), heartbeatDir(deps.paths.dataDir))}
注意：本会话自身的工具调用也会触发 hook 并刷新心跳——dsh 侧的"新鲜"不能单独当作"用户正在用"的证据；判活靠"配置有 + 心跳有 + 未陈旧"三者同时成立。`;
}
/** `dry-run`：安装预览（只输出 diff）。 */
function renderHostsDryRunCommand(deps) {
	const specs = hostsFor(deps);
	return renderHostsDryRun(runHooksDryRun(deps.paths.fishHome, {
		packageRoot: deps.paths.packageRoot,
		hostConfigPaths: specs.map((spec) => spec.configPath)
	}));
}
/**
* `/fish hosts …` 的入口（纯函数，便于单测与判据脚本直接调）。
* @param rest 子命令之后的 token（第一个是 `probe|health|dry-run`）
*/
function handleHostsCommand(rest, flags, deps) {
	const sub = (rest[0] ?? "").toLowerCase();
	try {
		if (sub === "" || sub === "help" || flags.help === true) return {
			kind: "success",
			text: HOSTS_USAGE
		};
		assertFlags(flags, sub);
		if (sub === "probe") return withTruncation(renderHostsProbe(deps), "hosts-probe", deps);
		if (sub === "health") return withTruncation(renderHostsHealth(deps), "hosts-health", deps);
		if (sub === "dry-run" || sub === "dryrun") return withTruncation(renderHostsDryRunCommand(deps), "hosts-dry-run", deps);
		return {
			kind: "error",
			text: `未知的 hosts 子命令：${sub}（可用 ${HOSTS_SUBCOMMANDS.join(" / ")}）\n\n${HOSTS_USAGE}`
		};
	} catch (error) {
		const fishError = toFishError(error);
		deps.logger.error("hosts 子命令失败", {
			sub,
			code: fishError.code
		});
		return {
			kind: "error",
			text: fishError.format()
		};
	}
}
/** 统一走命令面的截断（超 60 行落 `.tmp/` 并回显路径）。 */
function withTruncation(text, label, deps) {
	const { text: finalText } = finalizeOutput(text, label, deps.paths.packageRoot);
	return {
		kind: "success",
		text: finalText
	};
}
//#endregion
//#region src/surfaces/commands.ts
/** 命令名（不带斜杠）。 */
const COMMAND_NAME = "fish";
const USAGE = `用法：/fish <子命令>
  shadow status     影子期状态（契约门禁读数 / 只读自证 / 边界声明）
  shadow contract   契约摘要（版本 / 宿主最低版本 / 能力块归属）
  shadow spec       spec 文件清单与摘要
  shadow probe      只读不变量自检（切词向量 + 表计数）
  shadow presence   面板数据源（谁在场 + 最近几条动态；两条只读 SELECT）
  shadow recall <q> 检索记忆（query/code/project 至少给一个；无关键词时按 project 列出）
                   选项：--project P  --code C  --tags a,b  --limit N  --scope all|title|body
                         --include-superseded
  shadow whoami     身份回执（token / session_key；证据级别只降不升）
                   选项：--token T  --session-key K  --all
  shadow health     自检读数（事件 / 活跃 / 休眠 / 作废 / 断链 / FTS 落后 / owner 无效）
  hosts probe       跨宿主 hooks 只读探针（配置文件 / fish 条目 / matcher / 脚本路径）
  hosts health      跨宿主 hooks 心跳健康度（每宿主 × 4 事件，>2h 视为陈旧）
  hosts dry-run     安装预览：目标文件"会变成什么"的统一 diff（只输出，不写盘）
  <fish 命令>       76 条 fish CLI 叶子全部映射到这里，例如：
                    /fish healthcheck   /fish recall 协作约定   /fish list
                    /fish board checkin …（写命令：明确拒绝 + 给替代）
                    /fish kb vault list  /fish events --limit 20
                    输入 /fish <命令> --help 看单条；输入 /fish <容器> 看子命令

影子期纪律（三条同时生效）：不注册模型工具 / 不注入上下文 / 不写 fish 库、不改宿主配置。`;
/** 影子期纪律：这三条永远为真，回显时必须出现（不许靠记忆）。 */
const SHADOW_DISCIPLINE = [
	"不注册模型可见工具（与 mcp__fish__* 同层会抛错）",
	"不注入模型上下文（hooks.json 4 条仍在注入，避免双份）",
	"不写 fish 库（只读 mode=ro + PRAGMA query_only）"
];
/**
* 解析子命令之后的参数。
*
* 三种形态都认（T04 起必须认 `--k=v`：CLI 等价的透传口径是"原样交给 python"，
* 而 `fish.py` 的开关都是 `--key value` / `--key`；宿主里人更常写 `--k=v`）：
*   · `--flag value` · `--flag=value` · `--flag`
*
* 为什么手写而不是引一个解析库：命令面只有十来个开关，而库会把"未知开关"变成
* 静默忽略或一大段 usage——这里宁可对未知开关**响亮报错**（拼错的开关等于什么都没做）。
*/
function parseCommandInput(input) {
	const parts = input.split(/\s+/).filter((part) => part !== "");
	const positional = [];
	const flags = {};
	for (let i = 0; i < parts.length; i += 1) {
		const part = parts[i];
		if (part.startsWith("--")) {
			const body = part.slice(2);
			if (body === "") throw new Error("空的开关名：`--`");
			const eq = body.indexOf("=");
			if (eq >= 0) {
				const name = body.slice(0, eq);
				if (name === "") throw new Error(`开关名为空：${JSON.stringify(part)}`);
				flags[name] = body.slice(eq + 1);
				continue;
			}
			const next = parts[i + 1];
			if (next !== void 0 && !next.startsWith("--")) {
				flags[body] = next;
				i += 1;
			} else flags[body] = true;
			continue;
		}
		positional.push(part);
	}
	return {
		positional,
		flags
	};
}
/** 取字符串开关；`true`（只写了开关名）按空串处理，缺省返回 `undefined`。 */
function flagString(flags, name) {
	const value = flags[name];
	if (value === void 0) return void 0;
	return value === true ? "" : value;
}
/** 取整数开关（非法值响亮失败：`--limit 十个` 不许静默变成默认值）。 */
function flagInt(flags, name) {
	const raw = flagString(flags, name);
	if (raw === void 0) return void 0;
	if (raw === "") throw new Error(`--${name} 需要一个整数`);
	const parsed = Number.parseInt(raw, 10);
	if (Number.isNaN(parsed)) throw new Error(`--${name} 需要整数，收到 ${JSON.stringify(raw)}`);
	return parsed;
}
/** 逗号/空白分隔的列表开关。 */
function flagList(flags, name) {
	const raw = flagString(flags, name);
	if (raw === void 0) return void 0;
	const items = raw.split(/[,\s]+/).filter((item) => item !== "");
	return items.length > 0 ? items : void 0;
}
/** 把门禁读数渲染成短表（Markdown，命令结果支持文本）。 */
function renderGate(gate) {
	return [
		`契约版本：${gate.contractVersion}（插件支持 ${gate.supportedContractVersion}）`,
		`宿主版本：${gate.hostVersion ?? "未探测"}（最低 ${gate.minHostVersion}，${gate.hostVersionSatisfied ? "已满足" : "未校验"}）`,
		`schema 指纹：${gate.fingerprintVerified ? "匹配" : "不匹配"} ${gate.fingerprint.slice(0, 16)}…（业务 ${gate.businessFingerprint.slice(0, 12)}…）`,
		`spec 自摘要：${gate.specDigestsVerified ? `通过（${gate.specDigestCount} 个文件）` : "未通过"}`,
		`对象计数：${Object.entries(gate.objectCounts).map(([type, n]) => `${type}=${n}`).join(" ")}｜业务表 ${gate.businessTableCount} 张`
	].join("\n");
}
/** `shadow status` 文本。 */
function renderShadowStatus(deps) {
	const db = openReadOnly(deps.paths.dbPath);
	let counts;
	try {
		counts = objectCounts(db);
	} finally {
		db.close();
	}
	return [
		`dsh-fish · 模式 ${deps.config.mode}`,
		`库：${deps.paths.dbPath}（只读）`,
		`spec：${deps.paths.specDir}`,
		renderGate(deps.gate),
		`实时对象计数：${Object.entries(counts).map(([type, n]) => `${type}=${n}`).join(" ")}`,
		"",
		"影子期纪律（三条同时生效）：",
		...SHADOW_DISCIPLINE.map((line, i) => `  ${i + 1}. ${line}`)
	].join("\n");
}
/** `shadow contract` 文本。 */
function renderContract(deps) {
	const { contract } = deps.spec;
	const byImpl = {
		python: 0,
		shadow: 0,
		ts: 0
	};
	for (const entry of contract.implMatrix) byImpl[entry.impl] += 1;
	return [
		`contractVersion：${contract.contractVersion}`,
		`minHostVersion：${contract.minHostVersion}`,
		`能力块：${contract.capabilities.length} 个｜impl python=${byImpl.python} shadow=${byImpl.shadow} ts=${byImpl.ts}`,
		`spec 登记文件：${Object.keys(contract.specFiles).length} 个`,
		...contract.implMatrix.map((entry) => `  ${entry.id} ${entry.name} → ${entry.impl}`)
	].join("\n");
}
/** `shadow spec` 文本。 */
function renderSpecInventory(deps) {
	const lines = [`spec 目录：${deps.paths.specDir}`];
	const digests = deps.spec.fileDigests;
	for (const [relative, digest] of Object.entries(digests).sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0)) lines.push(`  ${relative}  ${digest.slice(0, 16)}…`);
	const registered = Object.keys(deps.spec.contract.specFiles).sort();
	const actual = Object.keys(digests).sort();
	const missing = registered.filter((name) => !actual.includes(name));
	const extra = actual.filter((name) => !registered.includes(name));
	lines.push(`摘要登记：登记 ${registered.length} / 实际 ${actual.length}` + (missing.length === 0 && extra.length === 0 ? "（一致）" : `（登记缺失 ${missing.length}，未登记 ${extra.length}）`));
	return lines.join("\n");
}
/** `shadow probe` 文本：只读不变量自检。 */
function renderProbe(deps) {
	const failures = verifyCutVectors(deps.spec.cut);
	const db = openReadOnly(deps.paths.dbPath);
	let counts;
	let businessTables = [];
	let writeRejected = false;
	try {
		counts = objectCounts(db);
		businessTables = businessTableNames(db);
		try {
			db.exec("CREATE TABLE __dsh_fish_command_probe (x INTEGER)");
		} catch {
			writeRejected = true;
		}
	} finally {
		db.close();
	}
	return [
		`切词向量：${deps.spec.cut.vectors.length} 组，失败 ${failures.length} 组${failures.length === 0 ? "（全部通过）" : ""}`,
		...failures.slice(0, 5).map((f) => `  ✗ ${f.id} 首个差异位置 ${f.firstDiffAt}`),
		`只读自证：${writeRejected ? "写入被 SQLite 拒绝（正常）" : "⚠ 写入竟然成功——立刻停用插件"}`,
		`业务表：${businessTables.length} 张（期望 21）`,
		`对象计数：${Object.entries(counts).map(([type, n]) => `${type}=${n}`).join(" ")}`
	].join("\n");
}
/**
* `shadow presence` 文本：面板数据源的人读形态。
*
* 为什么命令面要保留这一条：`board.ts` 被砍之后，面板需要的"谁在场 + 最近几条动态"
* 只剩 `domain/presence.ts` 这一处来源。给一个人手可复现的入口，等于让这份数据源
* 每天都被看见一次；没有入口的数据源，坏掉时只会安静地坏。
*/
function renderShadowPresence(deps) {
	return renderPresencePanel(readPresencePanel(deps.paths.dbPath));
}
/** `shadow recall` 文本。 */
function renderShadowRecall(positional, flags, deps) {
	const query = positional.join(" ");
	const token = flagString(flags, "token") ?? (process.env.FISH_SESSION_TOKEN ?? "").trim();
	const db = openReadOnly(deps.paths.dbPath);
	try {
		const identity = resolveIdentity(db, token);
		return renderRecall(db, {
			query,
			code: flagString(flags, "code") ?? null,
			project: flagString(flags, "project") ?? null,
			tags: flagList(flags, "tags") ?? null,
			scope: flagString(flags, "scope") ?? "all",
			limit: flagInt(flags, "limit") ?? 10,
			includeSuperseded: flags["include-superseded"] === true
		}, identity);
	} finally {
		db.close();
	}
}
/** `shadow whoami` 文本。 */
function renderShadowWhoami(flags, deps) {
	const token = flagString(flags, "token") ?? (process.env.FISH_SESSION_TOKEN ?? "").trim();
	const sessionKey = flagString(flags, "session-key") ?? "";
	const db = openReadOnly(deps.paths.dbPath);
	try {
		const { text } = renderWhoami(db, {
			token,
			sessionKey,
			all: flags["all"] === true
		}, deps.paths.fishHome);
		return text;
	} finally {
		db.close();
	}
}
/** `shadow health` 文本。 */
function renderShadowHealth(deps) {
	const db = openReadOnly(deps.paths.dbPath);
	try {
		return renderHealthcheck(healthcheckReadings(db, deps.paths.dataDir));
	} finally {
		db.close();
	}
}
/**
* 命令总入口（纯函数：便于单测）。
*
* 路由顺序：`shadow`（自有契约面）→ `hosts`（自有管理面）→ 其余一律当 **fish CLI**
* （76 条叶子）。为什么自有命令要单独判：它们不来自 `fish.py`，若混进 cli-map，
* 覆盖率判据就会在比"Python 命令树 == 映射表"时多出两条永远对不上的条目。
*
* @param rawInput 命令名之后的原始输入（宿主保证含前导分隔空白）
*/
function handleFishCommand(rawInput, deps) {
	const normalized = rawInput.trim().replace(/\s+/g, " ");
	try {
		const { positional, flags } = parseCommandInput(normalized);
		const first = positional[0] ?? "";
		if (normalized === "" || first === "help" || positional.length === 0 && flags.help === true) return {
			kind: "success",
			text: USAGE
		};
		if (first === "shadow") return handleShadowCommand(positional, flags, deps);
		if (first === "hosts") return handleHostsCommand(positional.slice(1), flags, deps);
		return handleCliInput(positional, flags, deps);
	} catch (error) {
		const fishError = toFishError(error);
		deps.logger.error("命令执行失败", {
			input: normalized,
			code: fishError.code
		});
		return {
			kind: "error",
			text: fishError.format()
		};
	}
}
/** `shadow ...` 这一族（T01–T03 的契约与只读能力面）。 */
function handleShadowCommand(positional, flags, deps) {
	const head = positional.slice(0, 2).join(" ");
	if (flags.help === true || positional.length === 1) return {
		kind: "success",
		text: USAGE
	};
	switch (head) {
		case "shadow status": return {
			kind: "success",
			text: renderShadowStatus(deps)
		};
		case "shadow contract": return {
			kind: "success",
			text: renderContract(deps)
		};
		case "shadow spec": return {
			kind: "success",
			text: renderSpecInventory(deps)
		};
		case "shadow probe": return {
			kind: "success",
			text: renderProbe(deps)
		};
		case "shadow health": return finalizeResult(renderShadowHealth(deps), "health", deps);
		case "shadow presence": return finalizeResult(renderShadowPresence(deps), "presence", deps);
		case "shadow whoami": return finalizeResult(renderShadowWhoami(flags, deps), "whoami", deps);
		case "shadow recall": return finalizeResult(renderShadowRecall(positional.slice(2), flags, deps), "recall", deps);
		default: return {
			kind: "error",
			text: `未知子命令：${positional.join(" ").slice(0, 80)}\n\n${USAGE}`
		};
	}
}
/** 收敛成 `CommandResult`（截断逻辑只在这里接一次）。 */
function finalizeResult(text, label, deps) {
	const { text: finalText } = finalizeOutput(text, label, deps.paths.packageRoot);
	return {
		kind: "success",
		text: finalText
	};
}
/**
* 注册 `/fish`（全局，不绑 agent）。
* @returns 注销函数（宿主 dispose 时调用）
*/
function registerFishCommand(ctx, deps) {
	if (!deps.config.commandEnabled) {
		deps.logger.warn("已通过配置关闭 /fish 命令（commandEnabled=false）");
		return () => void 0;
	}
	return ctx.commands.register({
		name: COMMAND_NAME,
		description: "fish（大肥鱼生命系统）影子期只读命令：shadow 契约面 / hosts 跨宿主管理面（probe|health|dry-run）/ 76 条 fish CLI 命令（写命令明确拒绝并给替代）",
		input: { hint: "shadow status | hosts probe | healthcheck | recall <q> | board checkin …（76 条 CLI 命令）" },
		handler: (invocation) => handleFishCommand(invocation.rawInput, deps)
	});
}
//#endregion
//#region src/index.ts
/** 插件名（与 package.json 一致；宿主冲突报错会带它）。 */
const name = "dsh-fish";
/**
* 影子期的模型面登记数——**恒为 0，且是判据**。
* 为什么把它写进代码：影子期的纪律如果只写在文档里，下一个人加工具时不会看到；
* 写成可断言的常量之后，`scripts/check-spec.mjs` 能直接把它钉住。
*/
const MODEL_SURFACE = {
	toolRegistrations: 0,
	promptInjections: 0
};
/** cordis 配置 schema：每个键都给默认值（缺省即影子期安全值）。 */
const Config = z.object({
	mode: z.string().default("shadow"),
	specDir: z.string().default(""),
	fishHome: z.string().default(""),
	dbPath: z.string().default(""),
	budgetTokens: z.number().default(1600),
	strictHostVersion: z.boolean().default(false),
	commandEnabled: z.boolean().default(true),
	logLevel: z.string().default("info")
});
const LOG_LEVELS = [
	"debug",
	"info",
	"warn",
	"error"
];
/** 把外部配置收敛成强类型配置（**非法值必须响亮失败**，不静默改默认）。 */
function normalizeConfig(raw = {}) {
	const logLevel = typeof raw.logLevel === "string" && raw.logLevel !== "" ? raw.logLevel : "info";
	if (!LOG_LEVELS.includes(logLevel)) throw new FishError("CONTRACT_INVALID", `logLevel 非法：${logLevel}（可选 ${LOG_LEVELS.join("/")}）`);
	const budgetTokens = typeof raw.budgetTokens === "number" && Number.isFinite(raw.budgetTokens) ? Math.trunc(raw.budgetTokens) : 1600;
	if (budgetTokens <= 0) throw new FishError("CONTRACT_INVALID", `budgetTokens 必须为正整数，收到 ${budgetTokens}`);
	return {
		mode: typeof raw.mode === "string" && raw.mode !== "" ? raw.mode : "shadow",
		specDir: typeof raw.specDir === "string" ? raw.specDir : "",
		fishHome: typeof raw.fishHome === "string" ? raw.fishHome : "",
		dbPath: typeof raw.dbPath === "string" ? raw.dbPath : "",
		budgetTokens,
		strictHostVersion: raw.strictHostVersion === true,
		commandEnabled: raw.commandEnabled !== false,
		logLevel
	};
}
/**
* 只做装配、不注册任何宿主面：契约门禁 + 影子初始化。
* @throws ContractMismatchError / SpecError / DatabaseError —— 任一不满足即抛（fail closed）
*/
function prepareShadow(rawConfig = {}, ctx = null) {
	const config = normalizeConfig(rawConfig);
	if (config.mode !== "shadow") throw new FishError("SHADOW_READ_ONLY", `影子期只支持 mode=shadow，收到 ${config.mode}`, { hint: "换心期（用户决定退役旧实现后）才引入写路径；当前请把 mode 改回 shadow" });
	const logger = createLogger(ctx, { minLevel: config.logLevel });
	const paths = resolvePaths({
		specDir: config.specDir,
		fishHome: config.fishHome,
		dbPath: config.dbPath
	});
	const spec = loadSpec(paths.specDir);
	const host = detectHostVersion();
	const gate = runStartupGate({
		specDir: paths.specDir,
		dbPath: paths.dbPath,
		hostVersion: host.version,
		strictHostVersion: config.strictHostVersion
	});
	logger.info("契约门禁通过", {
		contractVersion: gate.contractVersion,
		hostVersion: gate.hostVersion,
		hostVersionSource: host.source,
		fingerprint: gate.fingerprint.slice(0, 16),
		businessTables: gate.businessTableCount,
		tools: spec.tools.tools.length,
		events: spec.events.types.length,
		injectionSections: spec.injection.sections.length
	});
	return {
		config,
		paths,
		spec,
		gate
	};
}
/**
* cordis 插件入口。
* @param ctx 宿主上下文
* @param rawConfig 已由 schemastery 归一（这里再校验一次非法值）
*/
function apply(ctx, rawConfig = {}) {
	const runtime = prepareShadow(rawConfig, ctx);
	const logger = createLogger(ctx, { minLevel: runtime.config.logLevel }).child("commands");
	const deps = {
		paths: runtime.paths,
		spec: runtime.spec,
		gate: runtime.gate,
		logger,
		config: {
			mode: runtime.config.mode,
			budgetTokens: runtime.config.budgetTokens,
			commandEnabled: runtime.config.commandEnabled
		}
	};
	if (!runtime.config.commandEnabled) {
		logger.info("命令面已按配置关闭（commandEnabled=false）：本插件在模型面与人面都不留痕");
		return;
	}
	ctx.inject(["commands"], (scoped) => {
		const dispose = registerFishCommand(scoped, deps);
		scoped.effect(() => dispose, "dsh-fish: /fish command");
		logger.info("已注册 /fish 命令（影子期只读子集）");
	});
}
//#endregion
export { ALIAS_KINDS, ALIAS_MAX_HOPS, BOARD_POSTS_LIMIT_DEFAULT, BUDGET_RULE_FALLBACK, CLI_ANNOTATIONS, CLI_IMPLS, CLI_IMPL_VALUES, CLI_KINDS, CLI_KIND_VALUES, COLLECTOR_SCRIPTS, COMMAND_NAME, CONFIG_DEFAULTS, CORPUS_DB_NAME, CORPUS_SOURCE_LINE, Config, ContractMismatchError, DEFAULT_CUT_RULES, DEFAULT_PY_TIMEOUT_MS, DIFF_LINE_LIMIT, DatabaseError, EVIDENCE_LEVELS, FILTER_COLUMNS, FISH_HOOK_SCRIPTS, FishError, HEARTBEAT_RELDIR, HEARTBEAT_STALE_MS, HOOKS_PLAN_TIMEOUT_MS, HOSTS_SUBCOMMANDS, HOSTS_USAGE, IDEMPOTENT_VALUES, IDENTITY_ANONYMOUS, IDENTITY_HINTS, IDENTITY_SOURCE_REGISTER, IDENTITY_SOURCE_TOKEN, IDENTITY_SOURCE_UNATTRIBUTED, IDENTITY_STATUS, KB_DEFAULT_MAX_OPEN_BYTES, KB_MANDATORY_DENY, KB_MAX_ITEMS_PER_DIR, KB_MAX_LIMIT, KB_MEDIA_EXTS, KB_SEARCH_MAX_FILE_BYTES, KB_SENSITIVE_PATTERNS, KB_TREE_MAX_DEPTH, KbError, LAYOUT_STATUS_TABLE, LIKE_ESCAPE_SQL, LIST_DEFAULT_LIMIT, LIST_SOURCE, MODEL_SURFACE, NATIVE_COMMANDS, OUTPUT_LINE_LIMIT, PRESENCE_LIMIT_DEFAULT, PST_COLUMNS, PST_DASH_NONE, PST_DASH_NO_BOARD, PST_DASH_NO_MEMORY, PST_DASH_NO_OWNER, PST_DASH_UNAVAILABLE, PST_OPEN_WINDOW_DAYS, PST_OWNER_FALLBACK_DAYS, PointerError, READ_DEFAULT_MAX_CHARS, RECALL_DIMENSION_NOTE, RECALL_KEYWORD_NOTE, REDACT_PLACEHOLDER, REDACT_RULES, REQUIRED_EVENTS, SEARCH_CANDIDATE_FACTOR, SEARCH_CANDIDATE_MIN, SEARCH_DEFAULT_LIMIT, SEARCH_MAX_LIMIT, SILENT_LOGGER, SNIPPET_CHARS, SPEC_FILES, SUPPORTED_CONTRACT_VERSION, SpecError, TAGS_ONLY_HINT, WRITE_CLASSES, ageText, aliasMap, apply, asOfIso, assertWriteRejected, buildCliMap, buildFilterSql, businessTableNames, canonicalPointer, checkHostHealth, clampEvidence, cliEntryHint, cliGroupPaths, cliLeafPaths, cliRootPaths, clipChars, codePointLength, compareVersions, compileCut, computeSchemaFingerprint, configKeyDef, copySnapshot, corpusDbPath, corpusHeader, corpusRead, corpusSearch, corpusStats, countLessonRows, countMemories, countOpenLoopRows, countTokens, createLogger, credentialStateLine, currentOpenStint, currentOpenStints, cutText, cutTokens, datePartsInOffset, defaultHomeRoot, detectHostVersion, diffFingerprints, effectiveGlobs, ensureFtsReadable, finalizeOutput, findCitizenByName, findCliAnnotation, findHostSpec, findPackageRoot, findSessionCitizen, flagsToArgv, fnmatchCase, formatAge, formatListDeclaration, formatReplacement, formatStatusTag, ftsMatchExpression, getConfig, getConfigBool, getConfigFloat, getConfigInt, getConfigString, handleCliInput, handleFishCommand, handleHostsCommand, hashSessionToken, healthcheckReadings, heartbeatDir, heartbeatFileName, identitySourceLine, isExpired, isoLocal, isoWithOffset, judgeComponent, kbDetectEol, kbDetectText, kbFitText, kbIgnored, kbIsDeviceName, kbIsSensitiveName, kbOpen, kbResolveTarget, kbSearch, kbStats, kbTree, kbVaultGet, kbVaultList, kbWalk, lessonRows, likeLiteral, listHeartbeatDir, listMemories, loadCitizen, loadDecayConfig, loadSpec, lookupSessionToken, matchExpression, matcherCoversWriteTools, matcherDialect, name, normalizeConfig, normalizeWorkspacePath, nowUtc, objectCounts, offsetMinutesOf, openCorpusReadOnly, openLoopRows, openReadOnly, outputLogPath, parseAsOf, parseCommandInput, parsePointer, parseStamp, parseStrictBool, parseUtc, prepareShadow, presenceRows, probeAllHosts, probeHost, projectStatusPayload, projectStatusRows, pushPointer, pyRepr, pyText, rankCandidates, rankNorm, readHeartbeat, readPresencePanel, recentBoardPosts, recentFallback, recentLessonRows, redact, relatedCitizens, relatedCitizensLines, remainingTokenHours, renderAliasList, renderCliGuidance, renderCliHelpText, renderCliRefusal, renderCorpusRead, renderCorpusSearch, renderCorpusStats, renderHealthcheck, renderHostHealth, renderHostProbe, renderHostsDryRun, renderHostsHealth, renderHostsProbe, renderKbError, renderKbOpen, renderKbSearch, renderKbStats, renderKbTree, renderKbVaultList, renderPointerError, renderPresencePanel, renderProjectStatus, renderProjectStatusTable, renderRecall, renderWhoami, renderWorkspaceList, resolveCliRoute, resolveFishPy, resolveHostSpecs, resolveIdentity, resolveName, resolveOwner, resolvePaths, resolvePointer, resolveProjectByCwd, resolveProjectNames, resolvePythonBin, runFishCli, runFishCliOnSnapshot, runHooksDryRun, runStartupGate, safeField, sameNameWarnings, scriptPathOf, searchMemories, sid8, sliceCodePoints, tagLikePattern, toFishError, unifiedDiff, utcStamp, validateCliAnnotation, validateSessionToken, verifyCutVectors, walkAlias, whoami, withHeader, workspaceMappings };

//# sourceMappingURL=index.js.map