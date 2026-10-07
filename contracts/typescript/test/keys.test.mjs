// ID-10: proxied-call key derivation, cross-language vectors.
import assert from "node:assert/strict";
import { test } from "node:test";
import { DERIVED_KEY_PATTERN, IDEMPOTENCY_KEY_PATTERN, deriveProxiedKey, isDerivedKey, proxiedKey, proxiedPreimage } from "../dist/index.js";
import { vectors } from "./helpers.mjs";

const V = vectors("proxied_keys.json");

test("vector file is synthetic and non-empty", () => {
  assert.equal(V.synthetic, true);
  assert.equal(V.algorithm.prefix, "lt_");
  assert.ok(V.cases.length >= 4);
});

for (const c of V.cases) {
  test(`proxied key vector '${c.name}' is reproduced exactly`, () => {
    assert.equal(proxiedPreimage(c.caller_identity, c.env, c.tool, c.idempotency_key), c.preimage);
    assert.equal(deriveProxiedKey(c.caller_identity, c.env, c.tool, c.idempotency_key), c.derived_key);
    assert.equal(proxiedKey(c.caller_identity, c.env, c.tool, c.idempotency_key), c.derived_key);
    assert.ok(isDerivedKey(c.derived_key));
    // the derived key is itself a valid idempotency_key
    assert.match(c.derived_key, IDEMPOTENCY_KEY_PATTERN);
  });
}

test("ID-10: retries collide, different callers / envs / tools do not", () => {
  const byName = new Map(V.cases.map((c) => [c.name, deriveProxiedKey(c.caller_identity, c.env, c.tool, c.idempotency_key)]));
  const grouped = new Set();
  for (const g of V.equal_groups) {
    assert.equal(new Set(g.map((n) => byName.get(n))).size, 1);
    g.forEach((n) => grouped.add(n));
  }
  const reps = [...V.equal_groups.map((g) => byName.get(g[0])), ...[...byName].filter(([n]) => !grouped.has(n)).map(([, k]) => k)];
  assert.equal(new Set(reps).size, reps.length);
  const a = deriveProxiedKey("gateway:beta:subject-a", "beta", "create_override_version", "k1");
  const b = deriveProxiedKey("gateway:beta:subject-b", "beta", "create_override_version", "k1");
  assert.notEqual(a, b);
  assert.equal(a, deriveProxiedKey("gateway:beta:subject-a", "beta", "create_override_version", "k1"));
});

test("input validation mirrors the Python helper", () => {
  assert.throws(() => deriveProxiedKey("", "beta", "get_plan", "k"), TypeError);
  assert.throws(() => deriveProxiedKey("c", "dev", "get_plan", "k"), /env must be one of/);
  assert.throws(() => deriveProxiedKey("c", "beta", "Get-Plan", "k"), /tool/);
  assert.throws(() => deriveProxiedKey("c", "beta", "get_plan", ""), /idempotency_key/);
  assert.throws(() => deriveProxiedKey("c", "beta", "get_plan", "a|b"), /idempotency_key/);
  assert.throws(() => deriveProxiedKey("c", "beta", "get_plan", "x".repeat(129)), /idempotency_key/);
  assert.throws(() => deriveProxiedKey("c\ud800", "beta", "get_plan", "k"), /surrogate/);
  assert.match(deriveProxiedKey("c", "prod", "get_plan", "x".repeat(128)), DERIVED_KEY_PATTERN);
  assert.ok(!isDerivedKey("lt_ABC"));
  assert.ok(!isDerivedKey(42));
});
