import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { CONTRACT_ID_BASE, VERSION, schemaId } from "../dist/index.js";

const contractsVersion = readFileSync(new URL("../../VERSION", import.meta.url), "utf8").trim();
const pkg = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8"));

test("package version equals contracts/VERSION", () => {
  assert.equal(pkg.version, contractsVersion);
  assert.equal(VERSION, contractsVersion);
});

test("schema ids use the contract namespace", () => {
  assert.equal(schemaId("core", "plan-version"), `${CONTRACT_ID_BASE}core/v1/plan-version.json`);
});

test("bundled data carries the same version", () => {
  const bundled = readFileSync(new URL("../data/VERSION", import.meta.url), "utf8").trim();
  assert.equal(bundled, contractsVersion);
});
