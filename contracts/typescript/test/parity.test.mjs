// Cross-language parity (CS-10 "in both languages"): the TypeScript validator must give
// the same outcome, error code and (code, pointer) issue set as the Python validator on
// every fixture and on thousands of mutations of the valid fixtures.
//
// Needs the Python project's virtualenv (contracts/python/.venv, created by
// `uv sync`); runs it offline with `python -I`. Skipped when it is absent, e.g. when
// the npm package is tested outside the source checkout.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { createValidator, validate } from "../dist/index.js";
import { loadStore } from "../dist/node.js";
import { CONTRACTS, PKG_DIR } from "./helpers.mjs";

const python = join(CONTRACTS, "python", ".venv", "bin", "python");
const skip = existsSync(python) ? false : "contracts/python/.venv not present";

test("TypeScript and Python validators agree on fixtures and mutations", { skip, timeout: 300_000 }, () => {
  const dir = mkdtempSync(join(tmpdir(), "finplan-parity-"));
  try {
    const out = join(dir, "reference.json");
    const r = spawnSync(python, ["-I", join(PKG_DIR, "test", "parity", "py_reference.py"), "--root", CONTRACTS, "--out", out], {
      encoding: "utf8",
      env: { PATH: process.env.PATH ?? "", HOME: process.env.HOME ?? "" },
    });
    assert.equal(r.status, 0, r.stderr);
    const ref = JSON.parse(readFileSync(out, "utf8"));
    assert.ok(ref.length > 1000, "reference corpus is large");
    const validator = createValidator(loadStore(CONTRACTS));
    const diffs = [];
    for (const c of ref) {
      const t = validate(c.doc, c.schema, { validator, context: c.context ?? undefined });
      const ti = [...new Set(t.issues.map((i) => JSON.stringify([i.code, i.pointer])))].sort();
      const pi = c.issues.map((x) => JSON.stringify(x)).sort();
      if (t.valid !== c.valid || t.code !== c.code || JSON.stringify(ti) !== JSON.stringify(pi)) {
        diffs.push({ id: c.id, python: { valid: c.valid, code: c.code, issues: pi }, typescript: { valid: t.valid, code: t.code, issues: ti } });
      }
    }
    assert.deepEqual(diffs.slice(0, 5), [], `${diffs.length} of ${ref.length} documents differ`);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
