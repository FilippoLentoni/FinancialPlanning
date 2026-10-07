// 6.5 (TypeScript side): the conformance runner in producer and consumer modes, and the CLI.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, test } from "node:test";
import { REQUIRED_SCHEMAS, PUBLISHED_TOOLS, bundledRoot, checkEmbeddedMatches, loadStore, runConformance, syntheticExceptions } from "../dist/node.js";
import { CONTRACTS, PKG_DIR, vectors } from "./helpers.mjs";

const CLI = join(PKG_DIR, "dist", "cli.js");
const tmp = mkdtempSync(join(tmpdir(), "finplan-conf-"));
after(() => rmSync(tmp, { recursive: true, force: true }));

const cli = (...args) => spawnSync(process.execPath, [CLI, ...args], { encoding: "utf8", env: { ...process.env, FINPLAN_CONTRACTS_ROOT: "" } });

function copyRoot(name) {
  const root = join(tmp, name);
  for (const part of ["VERSION", "core", "finance", "domains", "fixtures", "conformance"]) cpSync(join(CONTRACTS, part), join(root, part), { recursive: true });
  return root;
}

test("producer conformance passes on the source checkout", () => {
  const r = runConformance({ mode: "producer" });
  assert.deepEqual(r.problems, []);
  assert.equal(r.ok, true);
  assert.equal(r.root, CONTRACTS);
  assert.ok(r.fixtures_checked >= 300 && r.schemas_checked >= 40);
});

test("consumer conformance passes on the package's bundled data and embedded schemas match it", () => {
  const r = runConformance({ mode: "consumer" });
  assert.deepEqual(r.problems, []);
  assert.equal(r.root, bundledRoot());
  assert.deepEqual(checkEmbeddedMatches(loadStore(bundledRoot())), []);
});

test("consumer mode: pinned version mismatch fails", () => {
  const r = runConformance({ mode: "consumer", expectVersion: "99.0.0" });
  assert.equal(r.ok, false);
  assert.match(r.problems.map((p) => p.message).join("\n"), /pins 99\.0\.0/);
});

test("consumer mode: the consumer's own documents are validated (failure blocks the build)", () => {
  const docs = join(tmp, "docs");
  mkdirSync(docs, { recursive: true });
  const fx = join(CONTRACTS, "fixtures", "tools", "get-plan-request");
  const good = JSON.parse(readFileSync(join(fx, "valid", spawnSync("ls", [join(fx, "valid")], { encoding: "utf8" }).stdout.split("\n")[0]), "utf8"));
  writeFileSync(join(docs, "good.json"), JSON.stringify(good));
  let r = runConformance({ mode: "consumer", documents: docs, schema: "tools/get-plan-request" });
  assert.deepEqual(r.problems, []);
  writeFileSync(join(docs, "bad.json"), JSON.stringify({ ...good, bucket: "example-bucket" }));
  r = runConformance({ mode: "consumer", documents: docs, schema: "tools/get-plan-request" });
  assert.equal(r.ok, false);
  assert.equal(r.problems.length, 1);
  assert.match(r.problems[0].subject, /bad\.json$/);
  const res = cli("conformance", "--mode", "consumer", "--documents", docs, "--schema", "tools/get-plan-request");
  assert.equal(res.status, 1);
  assert.match(res.stdout, /FAIL: consumer conformance \(typescript\)/);
  assert.throws(() => runConformance({ mode: "producer", documents: docs }), /consumer-mode/);
});

test("broken fixtures are reported: valid fails, invalid passes, wrong code, missing synthetic flag", () => {
  const root = copyRoot("broken");
  const fx = join(root, "fixtures");
  // a valid fixture that no longer validates
  const vdir = join(fx, "plan-version", "valid");
  const vfile = spawnSync("ls", [vdir], { encoding: "utf8" }).stdout.split("\n")[0];
  const vdoc = JSON.parse(readFileSync(join(vdir, vfile), "utf8"));
  writeFileSync(join(vdir, vfile), JSON.stringify({ ...vdoc, plan_id: 5 }));
  // an invalid fixture replaced by a valid document
  const idir = join(fx, "plan-version", "invalid");
  const ifile = spawnSync("ls", [idir], { encoding: "utf8" }).stdout.split("\n")[0];
  writeFileSync(join(idir, ifile), JSON.stringify(vdoc));
  // a fixture without the synthetic flag
  const { synthetic, ...unflagged } = vdoc;
  assert.equal(synthetic, true);
  writeFileSync(join(vdir, "zz-unflagged.json"), JSON.stringify(unflagged));
  // a wrong expected code
  const casesPath = join(root, "conformance", "cases.yaml");
  writeFileSync(casesPath, readFileSync(casesPath, "utf8").replace(/code: INVALID_IDENTIFIER/, "code: OPERATION_NOT_PERMITTED"));
  // an orphan fixture directory
  mkdirSync(join(fx, "no-such-schema", "valid"), { recursive: true });
  const r = runConformance({ mode: "producer", root });
  const text = r.problems.map((p) => `[${p.check}] ${p.subject}: ${p.message}`).join("\n");
  assert.equal(r.ok, false);
  assert.match(text, new RegExp(`plan-version/valid/${vfile}: valid fixture fails`));
  assert.match(text, new RegExp(`plan-version/invalid/${ifile}: invalid fixture validates`));
  assert.match(text, /\[CS-09\] plan-version\/valid\/zz-unflagged\.json/);
  assert.match(text, /expected error code OPERATION_NOT_PERMITTED, got INVALID_IDENTIFIER/);
  assert.match(text, /\[CS-02\] no-such-schema: fixture directory has no matching schema/);
  const res = cli("conformance", "--root", root, "--json");
  assert.equal(res.status, 1);
  assert.equal(JSON.parse(res.stdout).ok, false);
});

test("inventory: a missing tool schema and a schema without invalid fixtures are reported", () => {
  const root = copyRoot("inventory");
  rmSync(join(root, "core", "v1", "tools", "get-plan-response.json"));
  rmSync(join(root, "fixtures", "tools", "get-plan-response"), { recursive: true });
  rmSync(join(root, "fixtures", "plan-version", "invalid"), { recursive: true });
  const r = runConformance({ mode: "producer", root });
  const text = r.problems.map((p) => `[${p.check}] ${p.subject}: ${p.message}`).join("\n");
  assert.equal(r.ok, false);
  assert.match(text, /\[CS-02\] tools\/get-plan-response: tool 'get_plan' has no response schema/);
  assert.match(text, /\[CS-02\] plan-version: no invalid fixture under fixtures\/plan-version\/invalid\//);
  assert.match(text, /cases\.yaml lists a fixture that does not exist/);
});

test("conformance lists stay in step with the Python runner", { skip: existsSync(join(CONTRACTS, "python", "src", "finplan_contracts", "conformance.py")) ? false : "Python sources absent" }, () => {
  const py = readFileSync(join(CONTRACTS, "python", "src", "finplan_contracts", "conformance.py"), "utf8");
  const req = py.split("REQUIRED_SCHEMAS")[1].split("}")[0];
  assert.deepEqual([...req.matchAll(/"([a-z-]+)":/g)].map((m) => m[1]), Object.keys(REQUIRED_SCHEMAS));
  const tools = py.split("PUBLISHED_TOOLS")[1].split(")")[0];
  assert.deepEqual([...tools.matchAll(/"([a-z_]+)"/g)].map((m) => m[1]), [...PUBLISHED_TOOLS]);
});

test("synthetic exceptions are read from fixtures/README.md", () => {
  const ex = syntheticExceptions(CONTRACTS);
  assert.ok(ex.size >= 1);
  for (const rel of ex) assert.match(rel, /^[a-z0-9/-]+\/(valid|invalid)\/[a-z0-9-]+\.json$/);
});

test("CLI: conformance, validate, configuration-id, proxied-key, usage", () => {
  let r = cli("conformance");
  assert.equal(r.status, 0, r.stdout + r.stderr);
  assert.match(r.stdout, /^PASS: producer conformance \(typescript\), contracts \d+\.\d+\.\d+, \d+ schemas, \d+ fixtures, 0 problems$/m);
  r = cli("conformance", "--mode", "consumer");
  assert.equal(r.status, 0);

  const fx = join(CONTRACTS, "fixtures", "error");
  const ls = (d) => spawnSync("ls", [d], { encoding: "utf8" }).stdout.split("\n").filter(Boolean).map((f) => join(d, f));
  r = cli("validate", "--schema", "error", ...ls(join(fx, "valid")));
  assert.equal(r.status, 0, r.stdout);
  r = cli("validate", "--schema", "error", "--envelope", ...ls(join(fx, "invalid")));
  assert.equal(r.status, 1);
  const lines = r.stdout.trim().split("\n").map((l) => JSON.parse(l));
  assert.ok(lines.every((l) => l.valid === false && l.envelope.retryable === false));
  r = cli("validate", "--schema", "nope", ...ls(join(fx, "valid")));
  assert.equal(r.status, 2);
  r = cli("validate", "--schema", "budget-allocation", "--context", "cost_ceiling_usd=1", join(CONTRACTS, "fixtures", "budget-allocation", "valid", "defaults.json"));
  assert.equal(r.status, 1);

  const v = vectors("configuration_id.json").cases[0];
  const cfg = join(tmp, "cfg.json");
  writeFileSync(cfg, v.input_json);
  r = cli("configuration-id", cfg);
  assert.equal(r.status, 0);
  assert.equal(r.stdout.split(" ")[0], v.configuration_id);

  const k = vectors("proxied_keys.json").cases[0];
  r = cli("proxied-key", "--caller", k.caller_identity, "--env", k.env, "--tool", k.tool, "--key", k.idempotency_key);
  assert.equal(r.stdout.trim(), k.derived_key);
  assert.equal(cli("proxied-key", "--caller", "c", "--env", "dev", "--tool", "t", "--key", "k").status, 2);
  assert.equal(cli().status, 2);
  assert.equal(cli("bogus").status, 2);
  assert.equal(cli("--help").status, 0);
});
