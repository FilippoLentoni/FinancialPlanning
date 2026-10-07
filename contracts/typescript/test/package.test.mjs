// The npm artifact (`npm pack`) carries the compiled validators, the CLI and the
// contract data, at the version in contracts/VERSION.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { test } from "node:test";
import { PKG_DIR, readJson } from "./helpers.mjs";
import { join } from "node:path";

test("npm pack contents", { timeout: 120_000 }, () => {
  const npm = process.platform === "win32" ? "npm.cmd" : "npm";
  const r = spawnSync(npm, ["pack", "--dry-run", "--json", "--ignore-scripts", "--offline"], { cwd: PKG_DIR, encoding: "utf8" });
  assert.equal(r.status, 0, r.stderr);
  const [info] = JSON.parse(r.stdout.slice(r.stdout.indexOf("[")));
  const files = new Set(info.files.map((f) => f.path));
  const pkg = readJson(join(PKG_DIR, "package.json"));
  assert.equal(info.version, pkg.version);
  for (const f of ["package.json", "README.md", "dist/index.js", "dist/index.d.ts", "dist/node.js", "dist/cli.js", "dist/bundle.js", "data/VERSION", "data/domains/registry.json", "data/conformance/cases.yaml", "data/fixtures/vectors/configuration_id.json", "data/fixtures/vectors/proxied_keys.json", "data/core/v1/identifiers.json"]) {
    assert.ok(files.has(f), `${f} is packed`);
  }
  for (const f of files) assert.ok(!/^(src|test|scripts|node_modules)\//.test(f), `${f} must not be packed`);
  assert.deepEqual(pkg.bin, { "finplan-conformance-ts": "dist/cli.js" });
});
