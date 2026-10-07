// ID-02: RFC 8785 canonicalization and configuration_id, cross-language vectors.
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  CONFIGURATION_ID_PATTERN,
  CanonicalizationError,
  canonicalText,
  canonicalize,
  configurationId,
  configurationIdFromJson,
  parseStrict,
  requestHash,
} from "../dist/index.js";
import { vectors } from "./helpers.mjs";

const V = vectors("configuration_id.json");

test("vector file is synthetic and non-empty", () => {
  assert.equal(V.synthetic, true);
  assert.ok(V.cases.length >= 5);
  assert.equal(V.algorithm.prefix, "cfg_");
});

for (const c of V.cases) {
  test(`configuration_id vector '${c.name}' is reproduced exactly`, () => {
    const doc = parseStrict(c.input_json);
    assert.equal(canonicalText(doc), c.canonical_json);
    assert.deepEqual(Buffer.from(canonicalize(doc)), Buffer.from(c.canonical_json, "utf8"));
    assert.equal(configurationId(doc), c.configuration_id);
    assert.equal(configurationIdFromJson(c.input_json), c.configuration_id);
    assert.equal(configurationIdFromJson(Buffer.from(c.input_json, "utf8")), c.configuration_id);
    assert.match(c.configuration_id, CONFIGURATION_ID_PATTERN);
  });
}

test("ID-02: equal groups share one id, all other cases differ (value sensitivity)", () => {
  const byName = new Map(V.cases.map((c) => [c.name, configurationIdFromJson(c.input_json)]));
  const grouped = new Set();
  for (const group of V.equal_groups) {
    const ids = new Set(group.map((n) => byName.get(n)));
    assert.equal(ids.size, 1, `group ${group.join(",")} must share one id`);
    group.forEach((n) => grouped.add(n));
  }
  const groupIds = V.equal_groups.map((g) => byName.get(g[0]));
  const singles = [...byName].filter(([n]) => !grouped.has(n)).map(([, id]) => id);
  assert.equal(new Set([...groupIds, ...singles]).size, groupIds.length + singles.length);
});

test("ID-02: key order invariance on JS objects", () => {
  const a = { domain: "finance", payload: { b: 1, a: [1, 2], c: { y: true, x: null } }, synthetic: true };
  const b = { synthetic: true, payload: { c: { x: null, y: true }, a: [1, 2], b: 1 }, domain: "finance" };
  assert.equal(configurationId(a), configurationId(b));
  assert.notEqual(configurationId(a), configurationId({ ...a, payload: { ...a.payload, b: 2 } }));
});

test("RFC 8785 number serialization (ECMAScript Number.prototype.toString)", () => {
  const cases = [
    [0, "0"], [-0, "0"], [1, "1"], [-1, "-1"], [0.1, "0.1"], [1e21, "1e+21"], [1e20, "100000000000000000000"],
    [1.5e-7, "1.5e-7"], [0.000001, "0.000001"], [1e-7, "1e-7"], [9007199254740991, "9007199254740991"],
    [5e-324, "5e-324"], [1.7976931348623157e308, "1.7976931348623157e+308"], [333333333.3333333, "333333333.3333333"],
    [4.35, "4.35"], [0.002, "0.002"], [-1.5, "-1.5"],
  ];
  for (const [n, s] of cases) assert.equal(canonicalText(n), s, `number ${n}`);
});

test("RFC 8785 sample from section 3.2.2 / appendix", () => {
  const input = '{"numbers":[333333333.33333329,1E30,4.50,2e-3,0.000000000000000000000000001],"string":"\\u20ac$\\u000F\\u000aA\'\\u0042\\u0022\\u005c\\\\\\"\\/","literals":[null,true,false]}';
  const expected = '{"literals":[null,true,false],"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27],"string":"€$\\u000f\\nA\'B\\"\\\\\\\\\\"/"}';
  assert.equal(canonicalText(parseStrict(input)), expected);
});

test("member names sort by UTF-16 code units", () => {
  const doc = { "\u20ac": "Euro", "\r": "CR", "\ufb33": "Hebrew", "1": "One", "\ud83d\ude00": "Emoji", "\u0080": "Control", "\u00f6": "Latin" };
  assert.equal(canonicalText(doc), '{"\\r":"CR","1":"One","\u0080":"Control","ö":"Latin","€":"Euro","\ud83d\ude00":"Emoji","\ufb33":"Hebrew"}');
});

test("string escaping: only quote, backslash and C0 controls", () => {
  assert.equal(canonicalText("a\u0001b\u001f/\u007f\u2028"), '"a\\u0001b\\u001f/\u007f\u2028"');
  assert.equal(canonicalText("\b\t\n\f\r"), '"\\b\\t\\n\\f\\r"');
});

test("rejects non-JSON values and I-JSON violations", () => {
  for (const bad of [NaN, Infinity, -Infinity, undefined, () => 1, 10n, new Date(0), new Map(), Symbol("x")]) {
    assert.throws(() => canonicalText(bad), CanonicalizationError);
  }
  assert.throws(() => canonicalText({ a: undefined }), CanonicalizationError);
  assert.throws(() => canonicalText("\ud800"), CanonicalizationError);
  assert.throws(() => canonicalText({ "\udc00": 1 }), CanonicalizationError);
});

test("parseStrict rejects duplicates, inexact integers, NaN tokens and bad JSON", () => {
  assert.throws(() => parseStrict('{"a":1,"a":2}'), /duplicate/);
  assert.throws(() => parseStrict('{"a":{"b":1,"b":1}}'), /duplicate/);
  assert.throws(() => parseStrict("9007199254740992"), /exact range/);
  assert.throws(() => parseStrict("-9007199254740992"), /exact range/);
  assert.equal(parseStrict("9007199254740991"), 9007199254740991);
  assert.equal(parseStrict("9007199254740993.0"), 9007199254740992);
  assert.throws(() => parseStrict("1e400"), CanonicalizationError);
  for (const bad of ["NaN", "Infinity", "[1,]", "{\"a\":1,}", "01", "'a'", "{} x", "\"a\u0001\"", ""]) {
    assert.throws(() => parseStrict(bad), CanonicalizationError, bad);
  }
  assert.throws(() => canonicalText(parseStrict('"\\ud800"')), CanonicalizationError);
  const proto = parseStrict('{"__proto__":{"x":1}}');
  assert.deepEqual(Object.keys(proto), ["__proto__"]);
  assert.equal(canonicalText(proto), '{"__proto__":{"x":1}}');
});

test("requestHash is sha256: + canonical body hash", () => {
  assert.equal(requestHash({ b: 1, a: 2 }), requestHash({ a: 2, b: 1 }));
  assert.match(requestHash({}), /^sha256:[0-9a-f]{64}$/);
  assert.equal(requestHash({}), "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a");
});
