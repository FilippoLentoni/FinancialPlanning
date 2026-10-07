/**
 * Validate documents against contract schemas (TypeScript twin of
 * `finplan_contracts.validate`).
 *
 * `validate(document, schema)` runs JSON Schema 2020-12 validation with Ajv
 * (`ajv/dist/2020`; schemas resolve offline from the bundle) and then every semantic
 * check the schema declares in `x-finplan-checks`. Results are meant to agree with
 * the Python validator: same valid/invalid outcome and same error code, with the
 * same issue pointers wherever both libraries report the same failure.
 *
 * Parity notes (kept deliberately):
 * - `format` is an annotation, not an assertion, as in the Python validator (which
 *   uses `jsonschema` without a format checker). `ajv-formats` is registered so the
 *   formats are known; pass `{ assertFormats: true }` to `createValidator` to assert them.
 * - Errors inside `anyOf`/`oneOf` are reduced to one sub-error with the same
 *   relevance heuristic as `jsonschema.exceptions.best_match`, so the error code of
 *   a failing branch matches the Python side.
 *
 * Error-code mapping: a failure inside a subschema annotated `x-finplan-error-code`
 * takes that code (identifiers carry `INVALID_IDENTIFIER`, the execution `mode`
 * carries `OPERATION_NOT_PERMITTED`); everything else is `VALIDATION_FAILED`.
 * Messages never echo instance values.
 */
import { Ajv2020 } from "ajv/dist/2020.js";
import type { ErrorObject, ValidateFunction } from "ajv";
import addFormatsMod from "ajv-formats";
import { ID_BASE, SchemaNotFound, SchemaStore, embeddedStore, type SchemaInfo } from "./schemas.js";

const addFormats = ((addFormatsMod as unknown as { default?: unknown }).default ?? addFormatsMod) as (ajv: Ajv2020) => Ajv2020;

export const VALIDATION_FAILED = "VALIDATION_FAILED";
export const INVALID_IDENTIFIER = "INVALID_IDENTIFIER";
export const OPERATION_NOT_PERMITTED = "OPERATION_NOT_PERMITTED";
/** When several issues carry different codes, the envelope uses the first of these. */
export const CODE_PRIORITY = [OPERATION_NOT_PERMITTED, INVALID_IDENTIFIER, VALIDATION_FAILED] as const;

type Token = string | number;
type Json = unknown;
type Obj = Record<string, unknown>;

const isObj = (v: unknown): v is Obj => typeof v === "object" && v !== null && !Array.isArray(v);
const isNum = (v: unknown): v is number => typeof v === "number";

// --------------------------------------------------------------------- results
export interface ValidationIssue {
  code: string;
  pointer: string;
  message: string;
  field?: string;
  keyword?: string;
  schemaPath?: string;
}

export interface ErrorEnvelope {
  code: string;
  message: string;
  retryable: false;
  details: Record<string, unknown>;
  correlation_id: string;
  contract_version: string;
}

export class ValidationResult {
  constructor(
    readonly schemaId: string,
    readonly schemaName: string,
    readonly issues: ValidationIssue[],
    private readonly contractVersion: string,
  ) {}

  get valid(): boolean {
    return this.issues.length === 0;
  }

  /** The error code an API would return for this result (null when valid). */
  get code(): string | null {
    if (!this.issues.length) return null;
    const codes = this.codes;
    for (const c of CODE_PRIORITY) if (codes.has(c)) return c;
    return [...codes].sort()[0];
  }

  get codes(): Set<string> {
    return new Set(this.issues.map((i) => i.code));
  }

  primary(): ValidationIssue | undefined {
    const code = this.code;
    return this.issues.find((i) => i.code === code);
  }

  toDict(): Record<string, unknown> {
    return {
      schema: this.schemaName,
      schema_id: this.schemaId,
      valid: this.valid,
      code: this.code,
      issues: this.issues.map(issueToDict),
    };
  }

  /** Build a `core/v1/error.json` envelope for an invalid result. */
  toErrorEnvelope(correlationId: string, contractVersion?: string): ErrorEnvelope {
    const primary = this.primary();
    if (!primary) throw new Error("result is valid; there is no error to report");
    const details: Record<string, unknown> = { pointer: primary.pointer, schema_id: this.schemaId, errors: this.issues.map(issueToDict) };
    if (primary.field !== undefined) details.field = primary.field;
    else if (primary.code === INVALID_IDENTIFIER) details.field = primary.pointer.split("/").pop() || "/";
    const where = primary.pointer || "/";
    const message =
      primary.code === INVALID_IDENTIFIER
        ? `invalid identifier in field '${details.field}'`
        : primary.code === OPERATION_NOT_PERMITTED
          ? `operation not permitted at '${where}'`
          : `validation failed at '${where}'`;
    return {
      code: primary.code,
      message,
      retryable: false,
      details,
      correlation_id: correlationId,
      contract_version: contractVersion ?? this.contractVersion,
    };
  }
}

function issueToDict(i: ValidationIssue): Record<string, unknown> {
  const d: Record<string, unknown> = { code: i.code, pointer: i.pointer, message: i.message };
  if (i.field !== undefined) d.field = i.field;
  if (i.keyword !== undefined) d.keyword = i.keyword;
  return d;
}

// ------------------------------------------------------------------- helpers
const esc = (t: Token) => String(t).replace(/~/g, "~0").replace(/\//g, "~1");
export const pointerOf = (path: readonly Token[]) => path.map((p) => "/" + esc(p)).join("");

/** Resolve a JSON pointer; returns [found, value]. */
export function resolvePointer(doc: Json, pointer: string): [boolean, Json] {
  if (pointer === "" || pointer === "/") return [true, doc];
  let cur: Json = doc;
  for (const raw of pointer.replace(/^\//, "").split("/")) {
    const tok = raw.replace(/~1/g, "/").replace(/~0/g, "~");
    if (isObj(cur) && Object.prototype.hasOwnProperty.call(cur, tok)) cur = cur[tok];
    else if (Array.isArray(cur) && /^[0-9]+$/.test(tok) && Number(tok) < cur.length) cur = cur[Number(tok)];
    else return [false, undefined];
  }
  return [true, cur];
}

/** Split an Ajv instance path into tokens, typing array indexes as numbers by walking `doc`. */
function tokensOf(doc: Json, instancePath: string): Token[] {
  if (!instancePath) return [];
  const out: Token[] = [];
  let cur: Json = doc;
  for (const raw of instancePath.slice(1).split("/")) {
    const tok = raw.replace(/~1/g, "/").replace(/~0/g, "~");
    if (Array.isArray(cur) && /^[0-9]+$/.test(tok)) {
      out.push(Number(tok));
      cur = cur[Number(tok)];
    } else {
      out.push(tok);
      cur = isObj(cur) ? cur[tok] : undefined;
    }
  }
  return out;
}

function* iterStrings(doc: Json, path: Token[] = []): Generator<[Token[], string]> {
  if (typeof doc === "string") yield [path, doc];
  else if (Array.isArray(doc)) for (let i = 0; i < doc.length; i++) yield* iterStrings(doc[i], [...path, i]);
  else if (isObj(doc)) for (const [k, v] of Object.entries(doc)) yield* iterStrings(v, [...path, k]);
}

const SAFE_TOKEN = /^[a-z][a-z0-9_.]{0,63}$/;
/** Echo a value only if it is a short, harmless token; otherwise redact. */
const safe = (v: unknown) => (typeof v === "string" && SAFE_TOKEN.test(v) ? `'${v}'` : "<redacted>");
const lastStringToken = (path: readonly Token[]) => {
  for (let i = path.length - 1; i >= 0; i--) if (typeof path[i] === "string") return path[i] as string;
  return undefined;
};

/** Python `json.dumps` for the simple values used in messages. */
function pyDumps(v: unknown): string {
  if (Array.isArray(v)) return "[" + v.map(pyDumps).join(", ") + "]";
  return JSON.stringify(v);
}

/** Python `str()` of a JSON value, for `enum` messages. */
function pyStr(v: unknown): string {
  if (v === null) return "None";
  if (v === true) return "True";
  if (v === false) return "False";
  if (typeof v === "string") return v;
  return JSON.stringify(v);
}

/** Python `format(x, "g")`-like rendering for budget messages. */
function fmtG(n: number): string {
  return Number(n.toPrecision(6)).toString();
}

// ---------------------------------------------------- Ajv and error reduction
interface Node {
  keyword: string;
  tokens: Token[]; // absolute, within the validated document
  rel: Token[]; // relative to the parent anyOf/oneOf instance (best_match "path")
  parentSchema: Obj;
  schemaValue: unknown; // value of the failing keyword
  params: Record<string, unknown>;
  instance: unknown;
  schemaPath: string;
  context: Node[];
}

const WEAK = new Set(["anyOf", "oneOf"]);

export interface ValidatorOptions {
  /** Assert `format` (default false, matching the Python validator). */
  assertFormats?: boolean;
}

/** Ajv-backed validator over one schema store. */
export class ContractValidator {
  readonly ajv: Ajv2020;
  private readonly subCache = new WeakMap<object, ValidateFunction | null>();

  constructor(readonly store: SchemaStore, options: ValidatorOptions = {}) {
    this.ajv = new Ajv2020({
      allErrors: true,
      verbose: true,
      strict: true,
      strictTypes: false,
      strictTuples: false,
      strictRequired: false,
      allowUnionTypes: true,
      validateFormats: options.assertFormats === true,
      unicodeRegExp: true,
    });
    addFormats(this.ajv);
    // x-finplan-* (and any other x-*) keywords are annotations for the semantic checks.
    const extra = new Set<string>();
    const collect = (v: unknown) => {
      if (Array.isArray(v)) v.forEach(collect);
      else if (isObj(v)) for (const [k, sub] of Object.entries(v)) {
        if (k.startsWith("x-")) extra.add(k);
        collect(sub);
      }
    };
    for (const info of store.all()) collect(info.schema);
    for (const k of [...extra].sort()) this.ajv.addKeyword({ keyword: k, schemaType: undefined });
    for (const info of store.all()) this.ajv.addSchema(info.schema, info.id);
  }

  compiled(info: SchemaInfo): ValidateFunction {
    const fn = this.ajv.getSchema(info.id);
    if (!fn) throw new SchemaNotFound(info.id);
    return fn;
  }

  private sub(schema: unknown): ValidateFunction | null {
    if (typeof schema === "boolean") schema = schema ? {} : { not: {} };
    if (!isObj(schema)) return null;
    if (this.subCache.has(schema)) return this.subCache.get(schema)!;
    let fn: ValidateFunction | null = null;
    try {
      fn = this.ajv.compile(schema);
    } catch {
      fn = null; // e.g. a branch with a local "#/..." reference; fall back to the flat list
    }
    this.subCache.set(schema, fn);
    return fn;
  }

  private errorsOf(fn: ValidateFunction, instance: unknown): ErrorObject[] {
    return fn(instance) ? [] : [...(fn.errors ?? [])];
  }

  /**
   * Turn Ajv's flat `allErrors` list into a jsonschema-like error tree: the errors of
   * `anyOf`/`oneOf` branches become the context of that error, the per-item errors of
   * `contains` are dropped, and Ajv's `if` / `propertyNames` wrapper errors are removed.
   */
  private build(errors: ErrorObject[], doc: Json, prefix: Token[], relBase: number): Node[] {
    const out: Node[] = [];
    let i = errors.length - 1;
    while (i >= 0) {
      const e = errors[i];
      if (e.keyword === "if" || e.keyword === "propertyNames") {
        i--;
        continue;
      }
      const tokens = [...prefix, ...tokensOf(doc, e.instancePath)];
      const node: Node = {
        keyword: e.keyword,
        tokens,
        rel: tokens.slice(relBase),
        parentSchema: (isObj(e.parentSchema) ? e.parentSchema : {}) as Obj,
        schemaValue: e.schema,
        params: (e.params ?? {}) as Record<string, unknown>,
        instance: e.data,
        schemaPath: e.schemaPath,
        context: [],
      };
      if (e.keyword === "anyOf" || e.keyword === "oneOf" || e.keyword === "contains") {
        const parts: Array<{ fn: ValidateFunction; instance: unknown }> = [];
        let ok = true;
        if (e.keyword === "contains") {
          const fn = this.sub(e.schema);
          if (!fn || !Array.isArray(e.data)) ok = false;
          else e.data.forEach((item) => parts.push({ fn, instance: item }));
        } else if (Array.isArray(e.schema)) {
          for (const branch of e.schema) {
            const fn = this.sub(branch);
            if (!fn) ok = false;
            else parts.push({ fn, instance: e.data });
          }
        } else ok = false;
        if (ok) {
          const perPart = parts.map((p) => this.errorsOf(p.fn, p.instance));
          const n = perPart.reduce((s, errs) => s + errs.length, 0);
          const preceding = errors.slice(Math.max(0, i - n), i);
          const consistent = n <= i && preceding.every((p) => p.instancePath === e.instancePath || p.instancePath.startsWith(e.instancePath + "/"));
          if (consistent) {
            const multiplePass = e.keyword === "oneOf" && Array.isArray((e.params as Obj)?.passingSchemas);
            if (e.keyword !== "contains" && !multiplePass) {
              const base = tokens.length;
              node.context = perPart.flatMap((errs, idx) => {
                const inst = parts[idx].instance;
                return this.build(errs, inst, tokens, base);
              });
            }
            out.push(node);
            i -= n + 1;
            continue;
          }
        }
      }
      out.push(node);
      i--;
    }
    return out.reverse();
  }

  /** Raw schema issues of `document` against `info`, with paths prefixed by `prefix`. */
  schemaIssues(info: SchemaInfo, document: Json, prefix: Token[] = []): ValidationIssue[] {
    const fn = this.compiled(info);
    const errors = this.errorsOf(fn, document);
    const nodes = this.build(errors, document, [], 0);
    return nodes.map((n) => issueFromNode(n, prefix));
  }
}

// jsonschema's type checker, for `_matches_type`.
function isType(instance: unknown, type: string): boolean {
  switch (type) {
    case "null": return instance === null;
    case "boolean": return typeof instance === "boolean";
    case "string": return typeof instance === "string";
    case "number": return typeof instance === "number";
    case "integer": return typeof instance === "number" && Number.isInteger(instance);
    case "array": return Array.isArray(instance);
    case "object": return isObj(instance);
    default: return false;
  }
}

function matchesType(n: Node): boolean {
  const t = n.parentSchema["type"];
  if (typeof t === "string") return isType(n.instance, t);
  if (Array.isArray(t)) return t.some((x) => isType(n.instance, String(x)));
  return false;
}

type Key = [number, Token[], boolean, boolean, boolean];
const relevance = (n: Node): Key => [-n.rel.length, n.rel, !WEAK.has(n.keyword), false, !matchesType(n)];

function cmpToken(a: Token, b: Token): number {
  if (typeof a === typeof b) return a < b ? -1 : a > b ? 1 : 0;
  return typeof a === "number" ? -1 : 1;
}
function cmpKey(a: Key, b: Key): number {
  if (a[0] !== b[0]) return a[0] < b[0] ? -1 : 1;
  const len = Math.min(a[1].length, b[1].length);
  for (let i = 0; i < len; i++) {
    const c = cmpToken(a[1][i], b[1][i]);
    if (c) return c;
  }
  if (a[1].length !== b[1].length) return a[1].length < b[1].length ? -1 : 1;
  for (let i = 2; i < 5; i++) if (a[i] !== b[i]) return a[i] ? 1 : -1;
  return 0;
}

/** Port of `jsonschema.exceptions.best_match`. */
function bestMatch(nodes: Node[]): Node | undefined {
  let best: Node | undefined;
  for (const n of nodes) if (!best || cmpKey(relevance(n), relevance(best)) > 0) best = n;
  if (!best) return undefined;
  while (best.context.length) {
    const sorted = best.context.map((n, idx) => ({ n, idx, k: relevance(n) })).sort((x, y) => cmpKey(x.k, y.k) || x.idx - y.idx);
    if (sorted.length >= 2 && cmpKey(sorted[0].k, sorted[1].k) === 0) return best;
    best = sorted[0].n;
  }
  return best;
}

const BOUNDS = new Set(["minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "minLength", "maxLength", "minItems", "maxItems", "minProperties", "maxProperties", "multipleOf"]);

function issueFromNode(n: Node, prefix: Token[]): ValidationIssue {
  if (n.context.length) {
    const sub = bestMatch(n.context);
    if (sub) return issueFromNode(sub, prefix);
  }
  let path: Token[] = [...prefix, ...n.tokens];
  const schema = n.parentSchema;
  const code = typeof schema["x-finplan-error-code"] === "string" ? (schema["x-finplan-error-code"] as string) : VALIDATION_FAILED;
  const kw = n.keyword;
  let field = lastStringToken(path);
  const vv = n.schemaValue;
  let message: string;
  if (kw === "required") {
    const missing = n.params.missingProperty as string | undefined;
    if (missing !== undefined) {
      path = [...path, missing];
      field = missing;
    }
    message = `missing required property '${field}'`;
  } else if (kw === "additionalProperties" && isObj(n.instance)) {
    const allowed = new Set(Object.keys(isObj(schema.properties) ? schema.properties : {}));
    const extras = Object.keys(n.instance).filter((k) => !allowed.has(k)).sort();
    if (extras.length) {
      path = [...path, extras[0]];
      field = extras[0];
    }
    message = "unknown propert" + (extras.length > 1 ? "ies " : "y ") + extras.map((e) => (SAFE_TOKEN.test(e) ? `'${e}'` : "<redacted>")).join(", ");
  } else if ("x-finplan-identifier" in schema) {
    message = `field '${field}' must be a valid ${schema["x-finplan-identifier"]}`;
  } else if (kw === "pattern") {
    message = "value does not match the required format";
  } else if (kw === "enum") {
    message = "value is not one of the allowed values: " + (Array.isArray(vv) ? vv.map(pyStr).join(", ") : "");
  } else if (kw === "const") {
    message = `value must be ${pyDumps(vv)}`;
  } else if (kw === "type") {
    message = `expected type ${pyDumps(vv)}`;
  } else if (kw === "not" && isObj(vv) && Array.isArray(vv.required) && isObj(n.instance)) {
    const inst = n.instance;
    const present = (vv.required as string[]).filter((r) => r in inst);
    if (present.length === 1) {
      path = [...path, present[0]];
      field = present[0];
    }
    message = "propert" + (present.length > 1 ? "ies " : "y ") + present.map((r) => `'${r}'`).join(", ") + " not allowed in this state";
  } else if (kw === "not") {
    message = "value matches a forbidden form";
  } else if (kw === "propertyNames") {
    message = "property name is not allowed";
  } else if (BOUNDS.has(kw)) {
    message = `value violates ${kw} ${typeof vv === "number" ? String(vv) : JSON.stringify(vv)}`;
  } else if (kw === "uniqueItems") {
    message = "array items must be unique";
  } else if (kw === "contains") {
    message = "array does not contain a required item";
  } else if (kw === "format") {
    message = `value is not a valid ${vv}`;
  } else {
    message = `value violates '${kw}'`;
  }
  const pointer = pointerOf(path);
  return { code, pointer, message: `${pointer || "/"}: ${message}`, field, keyword: kw, schemaPath: n.schemaPath };
}

function sortIssues(issues: ValidationIssue[]): ValidationIssue[] {
  const cmp = (a: string, b: string) => (a < b ? -1 : a > b ? 1 : 0);
  const sorted = [...issues].sort((a, b) => cmp(a.pointer, b.pointer) || cmp(a.code, b.code) || cmp(a.message, b.message));
  const seen = new Set<string>();
  const out: ValidationIssue[] = [];
  for (const i of sorted) {
    const key = JSON.stringify([i.code, i.pointer, i.message]);
    if (!seen.has(key)) {
      seen.add(key);
      out.push(i);
    }
  }
  return out;
}

// ------------------------------------------------------------- semantic checks
export interface CheckContext {
  document: Json;
  info: SchemaInfo;
  store: SchemaStore;
  validator: ContractValidator;
  context: Record<string, unknown>;
}
export type CheckFn = (ctx: CheckContext) => ValidationIssue[];
export const CHECKS = new Map<string, CheckFn>();

/** Register a semantic check referenced by name from a schema's `x-finplan-checks`. */
export function registerCheck(name: string, fn: CheckFn): void {
  CHECKS.set(name, fn);
}

function fail(pointer: string, message: string, field?: string, code: string = VALIDATION_FAILED, keyword?: string): ValidationIssue {
  const i: ValidationIssue = { code, pointer, message: `${pointer || "/"}: ${message}` };
  if (field !== undefined) i.field = field;
  if (keyword !== undefined) i.keyword = keyword;
  return i;
}

function domainEntry(store: SchemaStore, domain: unknown): Obj | undefined {
  const domains = store.domainRegistry().domains;
  return Array.isArray(domains) ? (domains.find((d) => isObj(d) && d.domain === domain) as Obj | undefined) : undefined;
}

/** Domain registered, version served, and each declared payload valid for its adapter (DOM-01/02). */
registerCheck("domain_payload", (ctx) => {
  const { document: doc, store } = ctx;
  if (!isObj(doc)) return [];
  const issues: ValidationIssue[] = [];
  const checkDomainAt = (base: string): Obj | undefined => {
    const [ok, holder] = resolvePointer(doc, base);
    if (!ok || !isObj(holder) || !("domain" in holder)) return undefined;
    const domain = holder.domain;
    const entry = domainEntry(store, domain);
    if (!entry) {
      issues.push(fail(`${base}/domain`, `domain ${safe(domain)} is not registered`, "domain", VALIDATION_FAILED, "x-finplan-domain"));
      return undefined;
    }
    const dsv = holder.domain_schema_version;
    const served = Array.isArray(entry.domain_schema_versions) ? entry.domain_schema_versions : [];
    if (dsv !== undefined && dsv !== null && !served.some((v) => v === dsv)) {
      const shown = typeof dsv === "string" && dsv.length < 16 ? JSON.stringify(dsv) : "<redacted>";
      issues.push(fail(`${base}/domain_schema_version`, `domain_schema_version ${shown} is not served by domain ${safe(domain)}`, "domain_schema_version", VALIDATION_FAILED, "x-finplan-domain"));
      return undefined;
    }
    return entry;
  };
  const rootEntry = checkDomainAt("");
  const specs = ctx.info.schema["x-finplan-domain-payloads"];
  for (const spec of Array.isArray(specs) ? (specs as Obj[]) : []) {
    const pointer = String(spec.pointer);
    const [ok, payload] = resolvePointer(doc, pointer);
    if (!ok) continue; // absence is handled by "required" in the schema itself
    const domPtr = typeof spec.domain_pointer === "string" ? spec.domain_pointer : "";
    const entry = domPtr === "" ? rootEntry : checkDomainAt(domPtr);
    if (domPtr) {
      const [okR] = resolvePointer(doc, "/domain");
      const [okN, nested] = resolvePointer(doc, `${domPtr}/domain`);
      if (okR && okN && nested !== doc.domain) issues.push(fail(`${domPtr}/domain`, "nested domain differs from the envelope domain", "domain"));
    }
    if (!entry) continue;
    let kind: unknown;
    if ("kind_pointer" in spec) {
      const [okK, k] = resolvePointer(doc, String(spec.kind_pointer));
      if (!okK) continue;
      kind = k;
    } else kind = spec.kind;
    const schemas = isObj(entry.payload_schemas) ? entry.payload_schemas : {};
    const target = typeof kind === "string" && Object.prototype.hasOwnProperty.call(schemas, kind) ? schemas[kind] : undefined;
    if (target === undefined || target === null) {
      issues.push(fail(typeof spec.kind_pointer === "string" ? spec.kind_pointer : pointer, `payload kind ${safe(kind)} is not registered for domain ${safe(entry.domain)}`, undefined, VALIDATION_FAILED, "x-finplan-domain"));
      continue;
    }
    let payloadInfo: SchemaInfo;
    try {
      payloadInfo = store.get(String(target));
    } catch {
      issues.push(fail(pointer, "registered payload schema is missing from the package", undefined, VALIDATION_FAILED, "x-finplan-domain"));
      continue;
    }
    const prefix: Token[] =
      pointer === "" || pointer === "/"
        ? []
        : pointer.replace(/^\//, "").split("/").map((t) => t.replace(/~1/g, "/").replace(/~0/g, "~")).map((t) => (/^[0-9]+$/.test(t) ? Number(t) : t));
    issues.push(...ctx.validator.schemaIssues(payloadInfo, payload, prefix));
  }
  return issues;
});

/** Raw storage locations: storage URIs, ARNs, S3 endpoints. Used for requests and errors. */
const STORAGE_URI = /(\b(s3a?|s3n|gs|hdfs|file|ftp):\/\/|\barn:[a-z0-9-]*:|\.s3[.-]([a-z0-9-]+\.)?amazonaws\.com)/i;
/** Caller-chosen locations in requests: any URI, absolute/home/relative paths, backslash paths. */
const PATH_LIKE = /(^\s*[a-z][a-z0-9+.-]*:\/\/|^\s*(\/|~\/|\.{1,2}\/)|\\)/i;
const TRACEBACK = /Traceback \(most recent call last\)|File "[^"]+", line [0-9]+/;

/** Reject caller-chosen storage locations: s3:// and other URIs, ARNs, path-like values (CS-08). */
registerCheck("no_storage_locations", (ctx) => {
  const out: ValidationIssue[] = [];
  for (const [path, s] of iterStrings(ctx.document)) {
    if (s.startsWith(ID_BASE)) continue; // contract $ids are identifiers, never fetched
    if (STORAGE_URI.test(s) || PATH_LIKE.test(s)) {
      out.push(fail(pointerOf(path), "storage locations, URIs, ARNs and path-like values are not accepted; use a trusted artifact reference", lastStringToken(path), VALIDATION_FAILED, "x-finplan-no-storage-locations"));
    }
  }
  return out;
});

/** Error envelopes must not carry stack traces or raw storage locations (CS-05). */
registerCheck("no_leaks", (ctx) => {
  const doc = ctx.document;
  if (!isObj(doc)) return [];
  const out: ValidationIssue[] = [];
  for (const key of ["message", "details"]) {
    if (!(key in doc)) continue;
    for (const [path, s] of iterStrings(doc[key], [key])) {
      if (TRACEBACK.test(s) || STORAGE_URI.test(s)) out.push(fail(pointerOf(path), "error envelopes must not contain stack traces or raw storage locations", undefined, VALIDATION_FAILED, "x-finplan-no-leaks"));
    }
  }
  return out;
});

/** Codes registered with a fixed retryable value must carry it (CS-06). */
registerCheck("error_retryable", (ctx) => {
  const doc = ctx.document;
  if (!isObj(doc)) return [];
  const codes = ctx.store.get("error-codes").schema["x-finplan-error-codes"];
  const reg = isObj(codes) && typeof doc.code === "string" && Object.prototype.hasOwnProperty.call(codes, doc.code) ? codes[doc.code] : undefined;
  if (isObj(reg) && reg.retryable_fixed && typeof doc.retryable === "boolean" && doc.retryable !== reg.retryable) {
    return [fail("/retryable", `code ${doc.code} must have retryable ${JSON.stringify(reg.retryable)}`, "retryable")];
  }
  return [];
});

/** Budget categories must sum to at most the cost ceiling (ENV-17); ceiling from context or the schema default. */
registerCheck("budget_allocation_within_ceiling", (ctx) => {
  const doc = ctx.document;
  if (!isObj(doc)) return [];
  const ceiling = "cost_ceiling_usd" in ctx.context ? ctx.context.cost_ceiling_usd : ctx.info.schema["x-finplan-default-ceiling-usd"];
  if (!isNum(ceiling)) return [fail("", "cost ceiling is unknown", undefined, VALIDATION_FAILED, "x-finplan-budget")];
  const total = Object.values(doc).filter(isNum).reduce((s, v) => s + v, 0);
  if (total > ceiling + 1e-9) return [fail("", `budget categories sum to ${fmtG(total)} USD, above the ceiling of ${fmtG(ceiling)} USD`, undefined, VALIDATION_FAILED, "x-finplan-budget")];
  return [];
});

function parseTs(v: unknown): number | undefined {
  if (typeof v !== "string") return undefined;
  if (!/^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?(Z|[+-]\d{2}:?\d{2})?$/.test(v)) return undefined;
  const t = Date.parse(v.replace(" ", "T"));
  return Number.isNaN(t) ? undefined : t;
}

/** Idempotency records are retained at least the registered minimum (7 days). */
registerCheck("idempotency_retention", (ctx) => {
  const doc = ctx.document;
  if (!isObj(doc)) return [];
  const start = parseTs(doc.recorded_at);
  const end = parseTs(doc.retain_until);
  const raw = ctx.info.schema["x-finplan-min-retention-days"];
  const days = isNum(raw) ? raw : 7;
  if (start !== undefined && end !== undefined && end - start < days * 86_400_000) {
    return [fail("/retain_until", `idempotency records must be retained at least ${days} days`, "retain_until")];
  }
  return [];
});

/** Every contract `$id` named in the document exists in this package version. */
registerCheck("schema_ids_resolve", (ctx) => {
  const out: ValidationIssue[] = [];
  for (const [path, s] of iterStrings(ctx.document)) {
    if (s.startsWith(ID_BASE) && !ctx.store.has(s)) out.push(fail(pointerOf(path), "schema $id does not resolve in this contract version", undefined, VALIDATION_FAILED, "x-finplan-schema-id"));
  }
  return out;
});

/** Catalog entries are unique and reference Lambda parameters of the catalog's environment. */
registerCheck("tool_catalog_environment", (ctx) => {
  const doc = ctx.document;
  if (!isObj(doc) || !Array.isArray(doc.tools)) return [];
  const out: ValidationIssue[] = [];
  const seen: unknown[] = [];
  const env = doc.environment;
  doc.tools.forEach((t, i) => {
    if (!isObj(t)) return;
    const name = t.name;
    if (seen.includes(name)) out.push(fail(`/tools/${i}/name`, "duplicate tool name", "name"));
    seen.push(name);
    const ref = t.lambda_ref_parameter;
    if (typeof ref === "string" && ref.split("/").length - 1 >= 3 && ref.split("/")[2] !== env) {
      out.push(fail(`/tools/${i}/lambda_ref_parameter`, "Lambda reference parameter is not in the catalog's environment", "lambda_ref_parameter"));
    }
  });
  return out;
});

/** Manifest outputs are parameters under the repo's own segment, same environment or shared (ENV-07). */
registerCheck("manifest_outputs_own_segment", (ctx) => {
  const doc = ctx.document;
  if (!isObj(doc) || !isObj(doc.outputs)) return [];
  const out: ValidationIssue[] = [];
  for (const [key, name] of Object.entries(doc.outputs)) {
    if (typeof name !== "string" || name.split("/").length - 1 < 5) continue;
    const parts = name.split("/");
    const env = parts[2];
    const repo = parts[3];
    if (repo !== doc.repo) out.push(fail(`/outputs/${esc(key)}`, "output parameter is outside the repository's own segment", key));
    else if (env !== doc.environment && env !== "shared") out.push(fail(`/outputs/${esc(key)}`, "output parameter is in another environment", key));
  }
  return out;
});

/**
 * GPU approval rule (ENV-20; port of Python `gpu_rule.violations`): a GPU job
 * (`compute_class` `gpu` or cost-estimate `budget_category` `gpu`) without a
 * recorded approval cannot leave `awaiting_approval`; an approval must precede the
 * first transition out of `awaiting_approval` and cover the cost estimate.
 */
const AWAITING = "awaiting_approval";
const GPU_KW = "x-finplan-gpu-approval";
function gpuFail(pointer: string, message: string): ValidationIssue {
  return fail(pointer, message, pointer.slice(pointer.lastIndexOf("/") + 1), VALIDATION_FAILED, GPU_KW);
}
function pyRepr(v: unknown): string {
  return typeof v === "string" ? `'${v}'` : v === undefined || v === null ? "None" : JSON.stringify(v);
}
registerCheck("gpu_approval_required", (ctx) => {
  const doc = ctx.document;
  if (!isObj(doc)) return [];
  const est = doc.cost_estimate;
  const isGpu = doc.compute_class === "gpu" || (isObj(est) && est.budget_category === "gpu");
  if (!isGpu) return [];
  const out: ValidationIssue[] = [];
  const transitions = Array.isArray(doc.transitions) ? doc.transitions : [];
  const leaves = (s: unknown) => s !== AWAITING && s !== undefined && s !== null;
  const appr = doc.approval;
  const approved = isObj(appr) && Boolean(appr.approved_by) && Boolean(appr.approved_at);
  if (!approved) {
    if (leaves(doc.state)) out.push(gpuFail("/state", `GPU job without a recorded user approval cannot leave ${AWAITING} (state is ${pyRepr(doc.state)})`));
    transitions.forEach((t, i) => {
      if (isObj(t) && leaves(t.state)) out.push(gpuFail(`/transitions/${i}/state`, `GPU job without a recorded user approval cannot transition to ${pyRepr(t.state)}`));
    });
    return out;
  }
  const approvedAt = parseTs((appr as Obj).approved_at);
  const firstExit = transitions.find((t) => isObj(t) && leaves(t.state)) as Obj | undefined;
  if (approvedAt !== undefined && firstExit !== undefined) {
    const leftAt = parseTs(firstExit.at);
    if (leftAt !== undefined && leftAt < approvedAt) out.push(gpuFail("/approval/approved_at", "GPU job left awaiting_approval before the recorded approval"));
  }
  const approvedUsd = (appr as Obj).approved_estimate_usd;
  if (isObj(est) && isNum(approvedUsd) && isNum(est.estimated_usd_upper_bound) && est.estimated_usd_upper_bound > approvedUsd + 1e-9) {
    out.push(gpuFail("/approval/approved_estimate_usd", "the cost estimate exceeds the approved estimate; a new approval is required"));
  }
  return out;
});

// --------------------------------------------------------------------- public
const validators = new WeakMap<SchemaStore, ContractValidator>();

/** The (cached) Ajv-backed validator of `store` (default: the embedded schemas). */
export function createValidator(store: SchemaStore = embeddedStore(), options: ValidatorOptions = {}): ContractValidator {
  if (options.assertFormats) return new ContractValidator(store, options);
  let v = validators.get(store);
  if (!v) {
    v = new ContractValidator(store);
    validators.set(store, v);
  }
  return v;
}

export interface ValidateOptions {
  /** Values a semantic check needs from outside the document, e.g. `{ cost_ceiling_usd: 50 }`. */
  context?: Record<string, unknown>;
  store?: SchemaStore;
  validator?: ContractValidator;
}

/** Validate `document` against `schema` (name, `ns/name` or `$id`). */
export function validate(document: unknown, schema: string, options: ValidateOptions = {}): ValidationResult {
  const validator = options.validator ?? createValidator(options.store ?? embeddedStore());
  const store = validator.store;
  const info = store.get(schema);
  const issues = validator.schemaIssues(info, document);
  const ctx: CheckContext = { document, info, store, validator, context: { ...(options.context ?? {}) } };
  for (const name of info.checks) {
    const fn = CHECKS.get(name);
    if (!fn) throw new Error(`schema ${info.name} declares unknown semantic check ${JSON.stringify(name)}`);
    issues.push(...fn(ctx));
  }
  return new ValidationResult(info.id, info.name, sortIssues(issues), store.version);
}

export function isValid(document: unknown, schema: string, options: ValidateOptions = {}): boolean {
  return validate(document, schema, options).valid;
}
