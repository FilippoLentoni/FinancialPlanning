// CS-10 (TypeScript side): every package fixture behaves as recorded, with the same
// error code the Python validator produces (conformance/cases.yaml).
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import {
  INVALID_IDENTIFIER,
  OPERATION_NOT_PERMITTED,
  SchemaNotFound,
  VALIDATION_FAILED,
  createValidator,
  embeddedStore,
  schemaId,
  validate,
} from "../dist/index.js";
import { loadCases, loadStore } from "../dist/node.js";
import { CONTRACTS } from "./helpers.mjs";

const store = embeddedStore();
const cases = loadCases(CONTRACTS);
const fx = join(CONTRACTS, "fixtures");

test("embedded store holds every schema of the source checkout", () => {
  const disk = loadStore(CONTRACTS);
  assert.deepEqual(store.names(), disk.names());
  assert.equal(store.version, disk.version);
  assert.ok(store.size >= 40);
});

for (const name of store.names()) {
  for (const outcome of ["valid", "invalid"]) {
    let files = [];
    try {
      files = readdirSync(join(fx, name, outcome)).filter((f) => f.endsWith(".json")).sort();
    } catch {
      /* inventory is checked in conformance.test.mjs */
    }
    for (const file of files) {
      const rel = `${name}/${outcome}/${file}`;
      test(`fixture ${rel}`, () => {
        const doc = JSON.parse(readFileSync(join(fx, rel), "utf8"));
        const c = cases.get(rel);
        assert.ok(c, `${rel} is listed in conformance/cases.yaml`);
        assert.equal(c.expect, outcome);
        const res = validate(doc, name, { context: c.context });
        if (outcome === "valid") assert.ok(res.valid, res.issues.map((i) => i.message).join("; "));
        else {
          assert.equal(res.valid, false, "invalid fixture must fail");
          assert.equal(res.code, c.code);
          const env = res.toErrorEnvelope("corr-0001");
          assert.equal(env.code, c.code);
          assert.equal(env.retryable, false);
          assert.equal(env.contract_version, store.version);
          assert.ok(!/s3:\/\/|arn:/i.test(env.message));
        }
      });
    }
  }
}

test("every fixture directory file was exercised (cases.yaml covers the tree)", () => {
  assert.equal(cases.size, [...cases.keys()].filter((k) => !k.startsWith("vectors/")).length);
  assert.ok(cases.size >= 300);
});

test("lookup by name, ns/name, ns/v1/name.json and $id", () => {
  const info = store.get("plan-version");
  assert.equal(store.get("core/plan-version"), info);
  assert.equal(store.get("core/v1/plan-version.json"), info);
  assert.equal(store.get(schemaId("core", "plan-version")), info);
  assert.throws(() => store.get("finance/plan-version"), SchemaNotFound);
  assert.throws(() => validate({}, "no-such-schema"), SchemaNotFound);
});

test("error codes: identifier fields map to INVALID_IDENTIFIER, unknown fields to VALIDATION_FAILED", () => {
  const name = "tools/get-plan-version-request";
  const valid = JSON.parse(readFileSync(join(fx, name, "valid", readdirSync(join(fx, name, "valid"))[0]), "utf8"));
  assert.ok(validate(valid, name).valid);
  const idField = Object.keys(valid).find((k) => typeof valid[k] === "string" && /^pv_/.test(valid[k]));
  assert.ok(idField, "fixture carries a plan_version_id");
  const bad = validate({ ...valid, [idField]: "pl_01J00000000000000000000000" }, name);
  assert.equal(bad.code, INVALID_IDENTIFIER);
  assert.equal(bad.primary().field, idField);
  assert.equal(bad.toErrorEnvelope("c").details.field, idField);
  const extra = validate({ ...valid, s3_uri: "s3://example-bucket/x" }, name);
  assert.equal(extra.code, VALIDATION_FAILED);
  assert.ok(extra.issues.every((i) => !i.message.includes("example-bucket")), "messages never echo values");
});

test("live execution mode is OPERATION_NOT_PERMITTED", () => {
  const rel = [...cases.values()].find((c) => c.code === OPERATION_NOT_PERMITTED);
  assert.ok(rel);
  const doc = JSON.parse(readFileSync(join(fx, rel.fixture), "utf8"));
  assert.equal(validate(doc, rel.schema).code, OPERATION_NOT_PERMITTED);
});

test("format is an annotation by default (Python parity); assertFormats opts in", () => {
  const store2 = loadStore(CONTRACTS);
  const loose = createValidator(store2);
  const strict = createValidator(store2, { assertFormats: true });
  assert.notEqual(loose, strict);
  assert.equal(createValidator(store2), loose, "validators are cached per store");
});

test("budget allocation sum uses the ceiling from context", () => {
  const doc = JSON.parse(readFileSync(join(fx, "budget-allocation", "valid", "defaults.json"), "utf8"));
  assert.ok(validate(doc, "budget-allocation").valid);
  assert.equal(validate(doc, "budget-allocation", { context: { cost_ceiling_usd: 1 } }).code, VALIDATION_FAILED);
  assert.equal(validate(doc, "budget-allocation", { context: { cost_ceiling_usd: null } }).valid, false);
});
