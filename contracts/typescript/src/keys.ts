/**
 * Idempotency keys for proxied calls (design D10; spec platform-identifiers, ID-10).
 *
 * TypeScript twin of `finplan_contracts.keys`. When a tool adapter proxies a
 * state-changing call to a producer under its own role, it forwards
 *
 *     "lt_" + lowercase hex SHA-256( UTF-8 "caller_identity|env|tool|idempotency_key" )
 *
 * (no spaces around the separators), so two end callers that choose the same
 * `idempotency_key` never collide, while each caller's retry maps to the same key.
 * Both languages must reproduce `fixtures/vectors/proxied_keys.json` exactly.
 */
import { assertWellFormed } from "./canonical.js";
import { sha256Hex } from "./sha256.js";

export const DERIVED_KEY_PREFIX = "lt_";
export const DERIVED_KEY_PATTERN = /^lt_[0-9a-f]{64}$/;
export const IDEMPOTENCY_KEY_PATTERN = /^[A-Za-z0-9_-]{1,128}$/;
export const TOOL_NAME_PATTERN = /^[a-z][a-z0-9_]{0,127}$/;
export const ENVIRONMENTS = ["beta", "gamma", "prod"] as const;
export type Environment = (typeof ENVIRONMENTS)[number];

/** The exact string hashed by `deriveProxiedKey` (after input validation). */
export function proxiedPreimage(callerIdentity: string, env: string, tool: string, idempotencyKey: string): string {
  if (typeof callerIdentity !== "string" || callerIdentity.length === 0) {
    throw new TypeError("caller_identity must be a non-empty string");
  }
  assertWellFormed(callerIdentity, "caller_identity");
  if (!(ENVIRONMENTS as readonly string[]).includes(env)) {
    throw new TypeError(`env must be one of ${ENVIRONMENTS.join(", ")}, got ${JSON.stringify(env)}`);
  }
  if (typeof tool !== "string" || !TOOL_NAME_PATTERN.test(tool)) {
    throw new TypeError(`tool must be a snake_case tool name, got ${JSON.stringify(tool)}`);
  }
  if (typeof idempotencyKey !== "string" || !IDEMPOTENCY_KEY_PATTERN.test(idempotencyKey)) {
    throw new TypeError("idempotency_key must be 1-128 characters from [A-Za-z0-9_-]");
  }
  return `${callerIdentity}|${env}|${tool}|${idempotencyKey}`;
}

/** Derived downstream idempotency key `lt_<sha256 hex>` (ID-10). */
export function deriveProxiedKey(callerIdentity: string, env: string, tool: string, idempotencyKey: string): string {
  return DERIVED_KEY_PREFIX + sha256Hex(proxiedPreimage(callerIdentity, env, tool, idempotencyKey));
}

/** Alias of `deriveProxiedKey`. */
export const proxiedKey = deriveProxiedKey;

export function isDerivedKey(value: unknown): boolean {
  return typeof value === "string" && DERIVED_KEY_PATTERN.test(value);
}
