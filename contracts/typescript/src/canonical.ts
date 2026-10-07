/**
 * RFC 8785 (JSON Canonicalization Scheme) and content-addressed identifiers.
 *
 * TypeScript twin of `finplan_contracts.canonical`; both must produce identical
 * bytes and identifiers for `fixtures/vectors/configuration_id.json` (ID-02).
 *
 * - `canonicalize(value)` returns the RFC 8785 canonical UTF-8 bytes of a JSON value.
 * - `configurationId(document)` = `"cfg_"` + lowercase hex SHA-256 of the canonical
 *   form of a configuration document (spec platform-identifiers, design D2).
 * - `requestHash(body)` = `"sha256:"` + hex SHA-256 of the canonical request body
 *   (the idempotency request hash of `core/v1/idempotency.json`).
 *
 * Rules (RFC 8785 section 3.2): object members sorted by the UTF-16 code units of
 * their names, no whitespace; strings escape only `"`, `\` and control characters
 * below U+0020 (`\b \t \n \f \r` short forms, otherwise `\u00xx` lowercase hex);
 * lone surrogates are rejected; numbers are serialized like ECMAScript
 * `Number.prototype.toString` (`-0` becomes `0`), NaN and infinities are rejected.
 *
 * `parseStrict` parses JSON text the way the Python side's `loads_strict` does:
 * duplicate member names and integer literals outside the exactly representable
 * range (|n| > 2**53 - 1) are rejected instead of silently collapsed or rounded.
 */
import { sha256Hex } from "./sha256.js";

export const CONFIGURATION_ID_PREFIX = "cfg_";
export const CONFIGURATION_ID_PATTERN = /^cfg_[0-9a-f]{64}$/;
const MAX_EXACT_INT = 2n ** 53n - 1n;

/** The value cannot be canonicalized (or parsed) under the RFC 8785 / I-JSON rules. */
export class CanonicalizationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "CanonicalizationError";
  }
}

export type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue };

// ------------------------------------------------------------------ strings
const SHORT_ESCAPES: Record<number, string> = { 0x08: "\\b", 0x09: "\\t", 0x0a: "\\n", 0x0c: "\\f", 0x0d: "\\r", 0x22: '\\"', 0x5c: "\\\\" };

/** Throw if `s` holds a lone (unpaired) UTF-16 surrogate. */
export function assertWellFormed(s: string, what = "strings"): void {
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    if (c >= 0xd800 && c <= 0xdbff) {
      const n = s.charCodeAt(i + 1);
      if (n >= 0xdc00 && n <= 0xdfff) {
        i++;
        continue;
      }
      throw new CanonicalizationError(`${what} must not contain lone surrogates`);
    }
    if (c >= 0xdc00 && c <= 0xdfff) throw new CanonicalizationError(`${what} must not contain lone surrogates`);
  }
}

function serializeString(s: string): string {
  assertWellFormed(s);
  let out = '"';
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    const esc = SHORT_ESCAPES[c];
    if (esc !== undefined) out += esc;
    else if (c < 0x20) out += "\\u" + c.toString(16).padStart(4, "0");
    else out += s[i];
  }
  return out + '"';
}

// ------------------------------------------------------------------ numbers
function serializeNumber(n: number): string {
  if (!Number.isFinite(n)) throw new CanonicalizationError("NaN and Infinity are not valid JSON numbers");
  if (n === 0) return "0"; // also normalizes -0
  return String(n); // ECMAScript Number.prototype.toString, as RFC 8785 requires
}

/** Compare by UTF-16 code units (RFC 8785 3.2.3). */
function compareUtf16(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0; // JS string comparison is by UTF-16 code units
}

function isPlainObject(v: object): boolean {
  const proto = Object.getPrototypeOf(v);
  return proto === Object.prototype || proto === null;
}

function serialize(value: unknown, out: string[]): void {
  if (value === null) out.push("null");
  else if (value === true) out.push("true");
  else if (value === false) out.push("false");
  else if (typeof value === "number") out.push(serializeNumber(value));
  else if (typeof value === "string") out.push(serializeString(value));
  else if (Array.isArray(value)) {
    out.push("[");
    value.forEach((item, i) => {
      if (i) out.push(",");
      serialize(item, out);
    });
    out.push("]");
  } else if (typeof value === "object" && isPlainObject(value)) {
    const obj = value as Record<string, unknown>;
    const keys = Object.keys(obj).sort(compareUtf16);
    out.push("{");
    keys.forEach((k, i) => {
      if (i) out.push(",");
      out.push(serializeString(k), ":");
      serialize(obj[k], out);
    });
    out.push("}");
  } else {
    throw new CanonicalizationError(`value of type ${value === undefined ? "undefined" : typeof value === "object" ? (value as object).constructor?.name ?? "object" : typeof value} is not JSON`);
  }
}

/** RFC 8785 canonical JSON text of `value`. */
export function canonicalText(value: unknown): string {
  const out: string[] = [];
  serialize(value, out);
  return out.join("");
}

/** RFC 8785 canonical JSON of `value` as UTF-8 bytes. */
export function canonicalize(value: unknown): Uint8Array {
  return new TextEncoder().encode(canonicalText(value));
}

// ------------------------------------------------------------- strict parser
/** Parse JSON text, rejecting duplicate member names and inexact integer literals (I-JSON). */
export function parseStrict(text: string | Uint8Array): JsonValue {
  const src = typeof text === "string" ? text : new TextDecoder("utf-8", { fatal: true }).decode(text);
  let i = 0;
  const fail = (msg: string): never => {
    throw new CanonicalizationError(`invalid JSON at offset ${i}: ${msg}`);
  };
  const ws = () => {
    while (i < src.length) {
      const c = src[i];
      if (c === " " || c === "\t" || c === "\n" || c === "\r") i++;
      else break;
    }
  };
  const parseString = (): string => {
    i++; // opening quote
    let out = "";
    for (;;) {
      if (i >= src.length) fail("unterminated string");
      const c = src.charCodeAt(i);
      if (c === 0x22) {
        i++;
        return out;
      }
      if (c < 0x20) fail("control character in string");
      if (c === 0x5c) {
        const e = src[i + 1];
        i += 2;
        switch (e) {
          case '"': out += '"'; break;
          case "\\": out += "\\"; break;
          case "/": out += "/"; break;
          case "b": out += "\b"; break;
          case "f": out += "\f"; break;
          case "n": out += "\n"; break;
          case "r": out += "\r"; break;
          case "t": out += "\t"; break;
          case "u": {
            const hex = src.slice(i, i + 4);
            if (!/^[0-9a-fA-F]{4}$/.test(hex)) fail("bad \\u escape");
            out += String.fromCharCode(parseInt(hex, 16));
            i += 4;
            break;
          }
          default:
            fail("bad escape");
        }
      } else {
        out += src[i];
        i++;
      }
    }
  };
  const parseNumber = (): number => {
    const m = /^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?/.exec(src.slice(i));
    if (!m || m[0] === "-") fail("bad number");
    const lit = m![0];
    i += lit.length;
    if (m![2] === undefined && m![3] === undefined) {
      const big = BigInt(lit);
      if (big > MAX_EXACT_INT || big < -MAX_EXACT_INT) {
        throw new CanonicalizationError(`integer ${lit} is outside the IEEE 754 exact range (|n| <= 2**53 - 1)`);
      }
    }
    const n = Number(lit);
    if (!Number.isFinite(n)) throw new CanonicalizationError("NaN and Infinity are not valid JSON numbers");
    return n;
  };
  const parseValue = (): JsonValue => {
    ws();
    const c = src[i];
    if (c === "{") {
      i++;
      const obj: Record<string, JsonValue> = {};
      const seen = new Set<string>();
      ws();
      if (src[i] === "}") {
        i++;
        return obj;
      }
      for (;;) {
        ws();
        if (src[i] !== '"') fail("expected member name");
        const key = parseString();
        if (seen.has(key)) throw new CanonicalizationError(`duplicate object member name ${JSON.stringify(key)}`);
        seen.add(key);
        ws();
        if (src[i] !== ":") fail("expected ':'");
        i++;
        const v = parseValue();
        Object.defineProperty(obj, key, { value: v, enumerable: true, writable: true, configurable: true });
        ws();
        if (src[i] === ",") {
          i++;
          continue;
        }
        if (src[i] === "}") {
          i++;
          return obj;
        }
        fail("expected ',' or '}'");
      }
    }
    if (c === "[") {
      i++;
      const arr: JsonValue[] = [];
      ws();
      if (src[i] === "]") {
        i++;
        return arr;
      }
      for (;;) {
        arr.push(parseValue());
        ws();
        if (src[i] === ",") {
          i++;
          continue;
        }
        if (src[i] === "]") {
          i++;
          return arr;
        }
        fail("expected ',' or ']'");
      }
    }
    if (c === '"') return parseString();
    if (src.startsWith("true", i)) {
      i += 4;
      return true;
    }
    if (src.startsWith("false", i)) {
      i += 5;
      return false;
    }
    if (src.startsWith("null", i)) {
      i += 4;
      return null;
    }
    if (c === "-" || (c !== undefined && c >= "0" && c <= "9")) return parseNumber();
    return fail("unexpected token");
  };
  const value = parseValue();
  ws();
  if (i !== src.length) fail("trailing data");
  return value;
}

// -------------------------------------------------------------- identifiers
/** `cfg_` + lowercase hex SHA-256 of the RFC 8785 canonical form of `document` (ID-02). */
export function configurationId(document: unknown): string {
  return CONFIGURATION_ID_PREFIX + sha256Hex(canonicalize(document));
}

/** `configurationId` of a configuration document given as raw JSON text. */
export function configurationIdFromJson(text: string | Uint8Array): string {
  return configurationId(parseStrict(text));
}

/** Idempotency request hash: `sha256:` + hex SHA-256 of the canonical request body. */
export function requestHash(body: unknown): string {
  return "sha256:" + sha256Hex(canonicalize(body));
}
