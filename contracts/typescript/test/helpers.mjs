import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

export const PKG_DIR = join(dirname(fileURLToPath(import.meta.url)), "..");
export const CONTRACTS = join(PKG_DIR, "..");
export const readJson = (p) => JSON.parse(readFileSync(p, "utf8"));
export const vectors = (name) => readJson(join(CONTRACTS, "fixtures", "vectors", name));
