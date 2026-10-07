import assert from "node:assert/strict";
import { createHash, randomBytes } from "node:crypto";
import { test } from "node:test";
import { sha256Hex } from "../dist/index.js";

test("sha256 matches node:crypto on known and random inputs", () => {
  assert.equal(sha256Hex(""), "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
  assert.equal(sha256Hex("abc"), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
  for (let len = 0; len < 300; len++) {
    const buf = randomBytes(len);
    assert.equal(sha256Hex(new Uint8Array(buf)), createHash("sha256").update(buf).digest("hex"), `length ${len}`);
  }
  const big = randomBytes(1 << 20);
  assert.equal(sha256Hex(new Uint8Array(big)), createHash("sha256").update(big).digest("hex"));
  assert.equal(sha256Hex("Café €"), createHash("sha256").update("Café €", "utf8").digest("hex"));
});
