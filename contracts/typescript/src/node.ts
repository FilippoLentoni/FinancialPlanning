/**
 * Node-only helpers of @finplan/contracts: load a contracts root from disk and run
 * the fixture conformance suite (TypeScript side of `finplan-conformance conformance`).
 *
 * Producer mode (the contract package build, CS-02/CS-09/CS-10) runs against the
 * source checkout; consumer mode runs against the contract data bundled in the
 * installed npm package (`data/`, the version a consumer pinned) and can also
 * validate the consumer's own documents. Both check:
 *
 * - every `fixtures/<schema>/valid/*.json` validates (schema + semantic checks) and
 *   every `invalid/*.json` fails with the code recorded in `conformance/cases.yaml`;
 * - inventory (CS-02): required schemas, a request/response pair per published tool,
 *   valid and invalid fixtures per schema, no orphaned fixture directory;
 * - fixture hygiene, flag part (CS-09): `"synthetic": true` or a README exception;
 * - in consumer mode, the embedded schemas equal the bundled data files.
 *
 * The leak scan, domain-neutrality and copied-`$id` checks are Python-only
 * (`finplan-conformance conformance`); this runner proves the TypeScript validator
 * agrees with the fixtures.
 */
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { parse as parseYaml } from "yaml";
import { canonicalText } from "./canonical.js";
import { ID_BASE, SchemaStore, embeddedBundle, type BundledSchema, type ContractBundle } from "./schemas.js";
import { createValidator, validate } from "./validate.js";

export const ENV_ROOT = "FINPLAN_CONTRACTS_ROOT";
export const NON_SCHEMA_DIRS = new Set(["fixtures", "domains", "ownership", "conformance", "python", "typescript", "templates", "node_modules"]);

const here = dirname(fileURLToPath(import.meta.url));
const looksLikeRoot = (p: string) => existsSync(join(p, "VERSION")) && existsSync(join(p, "core")) && statSync(join(p, "core")).isDirectory();

/** Contract data bundled in this npm package (`<package>/data`), if present. */
export function bundledRoot(): string | undefined {
  const p = resolve(here, "..", "data");
  return looksLikeRoot(p) ? p : undefined;
}

/** The source checkout's contracts root (nearest parent holding VERSION and core/), if any. */
export function checkoutRoot(): string | undefined {
  let dir = resolve(here, "..");
  for (;;) {
    const parent = dirname(dir);
    if (parent === dir) return undefined;
    dir = parent;
    if (looksLikeRoot(dir)) return dir;
  }
}

/**
 * Contracts root: an explicit `root`, then `FINPLAN_CONTRACTS_ROOT`, then (by
 * `prefer`) the source checkout or the bundled package data.
 */
export function contractsRoot(root?: string, prefer: "checkout" | "bundled" = "bundled"): string {
  if (root) return resolve(root);
  const env = process.env[ENV_ROOT];
  if (env) return resolve(env);
  const order = prefer === "checkout" ? [checkoutRoot(), bundledRoot()] : [bundledRoot(), checkoutRoot()];
  const found = order.find((p) => p !== undefined);
  if (!found) throw new Error(`contracts root not found; set ${ENV_ROOT}`);
  return found;
}

function walkJson(dir: string): string[] {
  return readdirSync(dir)
    .sort()
    .flatMap((name) => {
      const p = join(dir, name);
      return statSync(p).isDirectory() ? walkJson(p) : name.endsWith(".json") ? [p] : [];
    });
}

const toPosix = (p: string) => p.split(sep).join("/");

/** Read every schema, the domain registry and VERSION of a contracts root. */
export function loadBundle(root: string): ContractBundle {
  const schemas: BundledSchema[] = [];
  for (const ns of readdirSync(root).sort()) {
    const nsDir = join(root, ns);
    if (NON_SCHEMA_DIRS.has(ns) || ns.startsWith(".") || !statSync(nsDir).isDirectory()) continue;
    for (const majorName of readdirSync(nsDir).sort()) {
      const majorDir = join(nsDir, majorName);
      if (!/^v[0-9]+$/.test(majorName) || !statSync(majorDir).isDirectory()) continue;
      const major = Number(majorName.slice(1));
      for (const file of walkJson(majorDir)) {
        const name = toPosix(relative(majorDir, file)).replace(/\.json$/, "");
        const schema = JSON.parse(readFileSync(file, "utf8"));
        schemas.push({ name, namespace: ns, major, id: `${ID_BASE}${ns}/v${major}/${name}.json`, schema });
      }
    }
  }
  return {
    version: readFileSync(join(root, "VERSION"), "utf8").trim(),
    schemas,
    domainRegistry: JSON.parse(readFileSync(join(root, "domains", "registry.json"), "utf8")),
  };
}

const stores = new Map<string, SchemaStore>();

/** A cached `SchemaStore` over the schemas of a contracts root on disk. */
export function loadStore(root?: string): SchemaStore {
  const r = contractsRoot(root);
  let s = stores.get(r);
  if (!s) {
    s = new SchemaStore(loadBundle(r), r);
    stores.set(r, s);
  }
  return s;
}

// ------------------------------------------------------------ conformance data
/** Coverage list of the contract-schemas spec ("Contract package coverage"), by schema name. */
export const REQUIRED_SCHEMAS: Record<string, string> = {
  identifiers: "identifiers",
  "input-snapshot": "input snapshot metadata",
  plan: "plan",
  "plan-version": "plan version",
  publication: "publication",
  execution: "execution",
  configuration: "configuration",
  "job-submission": "job submission",
  "job-status": "job status",
  "job-result": "job result",
  "artifact-ref": "trusted artifact reference",
  error: "error envelope",
  "error-codes": "registered error codes",
  capability: "capability description",
  "release-manifest": "release manifest",
  "domain-envelope": "domain envelope",
  "staged-output-manifest": "staged-output manifest",
  "production-strategy": "production-strategy document (1.1.0)",
  "excel-plan-template": "Excel plan template",
  "import-report": "import report",
  "tool-catalog": "tool catalog",
  caller: "on-behalf-of caller block",
  "cost-estimate": "job cost-estimate block",
  "budget-allocation": "budget allocation",
  idempotency: "idempotency fragment",
  concurrency: "concurrency fragment",
};

/** Tools published by FinanceLambdasTool; each needs a request/response schema pair. */
export const PUBLISHED_TOOLS = [
  "describe_capabilities",
  "query_market_data",
  "get_plan",
  "get_plan_version",
  "list_plan_versions",
  "get_job_status",
  "get_experiment_result",
  "refresh_market_data",
  "submit_experiment",
  "create_override_version",
  "validate_plan_version",
  "publish_plan_version",
  "production_strategy",
  "get_publication",
  "list_publications",
  "list_executions",
  "get_performance_evidence",
  "recommend_portfolio",
  "recommend_classical_portfolio",
  "explain_classical_recommendation",
  "compare_classical_plans",
  "evaluate_classical_performance",
  "get_classical_analysis",
  "list_classical_analyses",
  "research_portfolio_models",
  "research_market_events",
  "run_portfolio_research",
  "submit_portfolio_feedback",
  "get_portfolio_history",
  "list_portfolio_decisions",
  "get_portfolio_decision",
  "resolve_portfolio_decision",
  "list_market_snapshots",
  "record_agent_activity",
  "list_agent_activity",
  "explain_portfolio_decision",
  "compare_portfolio_decisions",
  "evaluate_portfolio_decision",
] as const;

const RESERVED_FIXTURE_DIRS = new Set(["vectors"]);
const EXCEPTIONS_START = "<!-- synthetic-exceptions:start -->";
const EXCEPTIONS_END = "<!-- synthetic-exceptions:end -->";

export interface ConformanceCase {
  fixture: string;
  schema?: string;
  expect?: "valid" | "invalid";
  code?: string;
  context?: Record<string, unknown>;
  covers?: string[];
}

export interface Problem {
  check: string;
  subject: string;
  message: string;
}

export interface Report {
  root: string;
  mode: "producer" | "consumer";
  language: "typescript";
  contract_version: string;
  schemas_checked: number;
  fixtures_checked: number;
  ok: boolean;
  problems: Problem[];
}

/** `conformance/cases.yaml`, keyed by fixture path relative to `fixtures/`. */
export function loadCases(root: string): Map<string, ConformanceCase> {
  const p = join(root, "conformance", "cases.yaml");
  if (!existsSync(p)) return new Map();
  const data = (parseYaml(readFileSync(p, "utf8")) ?? {}) as { cases?: ConformanceCase[] };
  return new Map((data.cases ?? []).map((c) => [c.fixture, c]));
}

/** Fixture paths (relative to `fixtures/`) exempt from the synthetic flag. */
export function syntheticExceptions(root: string): Set<string> {
  const p = join(root, "fixtures", "README.md");
  if (!existsSync(p)) return new Set();
  const text = readFileSync(p, "utf8");
  if (!text.includes(EXCEPTIONS_START)) return new Set();
  const block = text.split(EXCEPTIONS_START)[1].split(EXCEPTIONS_END)[0];
  return new Set([...block.matchAll(/^\s*-\s*`([^`]+)`/gm)].map((m) => m[1]));
}

/** Every fixture file (relative posix paths under `fixtures/`). */
export function fixtureFiles(root: string): string[] {
  const fx = join(root, "fixtures");
  return existsSync(fx) ? walkJson(fx).map((p) => toPosix(relative(fx, p))).sort() : [];
}

const listJson = (dir: string) => (existsSync(dir) ? readdirSync(dir).filter((n) => n.endsWith(".json")).sort() : []);

/** CS-02: required schemas exist and every schema has valid and invalid fixtures. */
export function checkInventory(store: SchemaStore, root: string): Problem[] {
  const problems: Problem[] = [];
  for (const [name, label] of Object.entries(REQUIRED_SCHEMAS)) {
    if (!store.has(name)) problems.push({ check: "CS-02", subject: name, message: `required schema missing (${label})` });
  }
  for (const tool of PUBLISHED_TOOLS) {
    const kebab = tool.replace(/_/g, "-");
    for (const part of ["request", "response"]) {
      if (!store.has(`tools/${kebab}-${part}`)) problems.push({ check: "CS-02", subject: `tools/${kebab}-${part}`, message: `tool '${tool}' has no ${part} schema` });
    }
  }
  const fx = join(root, "fixtures");
  for (const name of store.names()) {
    for (const outcome of ["valid", "invalid"]) {
      if (!listJson(join(fx, name, outcome)).length) problems.push({ check: "CS-02", subject: name, message: `no ${outcome} fixture under fixtures/${name}/${outcome}/` });
    }
  }
  const names = new Set(store.names());
  const walkDirs = (dir: string): string[] =>
    readdirSync(dir)
      .sort()
      .flatMap((n) => {
        const p = join(dir, n);
        return statSync(p).isDirectory() ? [p, ...walkDirs(p)] : [];
      });
  if (existsSync(fx)) {
    for (const d of walkDirs(fx)) {
      const base = d.split(sep).pop()!;
      if (base !== "valid" && base !== "invalid") continue;
      const schemaName = toPosix(relative(fx, dirname(d)));
      if (!names.has(schemaName) && !RESERVED_FIXTURE_DIRS.has(schemaName.split("/")[0])) {
        problems.push({ check: "CS-02", subject: schemaName, message: "fixture directory has no matching schema" });
      }
    }
  }
  return problems;
}

/** Valid fixtures validate; invalid fixtures fail with the recorded code (CS-10). */
export function checkFixtures(store: SchemaStore, root: string, cases: Map<string, ConformanceCase>): { count: number; problems: Problem[] } {
  const problems: Problem[] = [];
  let count = 0;
  const fx = join(root, "fixtures");
  const validator = createValidator(store);
  for (const name of store.names()) {
    for (const outcome of ["valid", "invalid"] as const) {
      for (const file of listJson(join(fx, name, outcome))) {
        const rel = `${name}/${outcome}/${file}`;
        count++;
        const c = cases.get(rel);
        let doc: unknown;
        try {
          doc = JSON.parse(readFileSync(join(fx, name, outcome, file), "utf8"));
        } catch (err) {
          problems.push({ check: "CS-10", subject: rel, message: `not valid JSON: ${(err as Error).message}` });
          continue;
        }
        const res = validate(doc, name, { validator, context: c?.context });
        if (outcome === "valid" && !res.valid) {
          problems.push({ check: "CS-10", subject: rel, message: "valid fixture fails: " + res.issues.slice(0, 3).map((i) => i.message).join("; ") });
        } else if (outcome === "invalid") {
          if (res.valid) problems.push({ check: "CS-10", subject: rel, message: "invalid fixture validates" });
          else if (c?.code && res.code !== c.code) problems.push({ check: "CS-10", subject: rel, message: `expected error code ${c.code}, got ${res.code}` });
        }
        if (c && c.schema !== undefined && c.schema !== null && c.schema !== name) problems.push({ check: "CS-10", subject: rel, message: `cases.yaml names schema ${c.schema}, fixture lives under ${name}` });
        if (c && c.expect !== undefined && c.expect !== null && c.expect !== outcome) problems.push({ check: "CS-10", subject: rel, message: `cases.yaml expects ${c.expect}, fixture lives under ${outcome}/` });
      }
    }
  }
  const existing = new Set(fixtureFiles(root));
  for (const rel of [...cases.keys()].sort()) {
    if (!existing.has(rel)) problems.push({ check: "CS-10", subject: rel, message: "cases.yaml lists a fixture that does not exist" });
  }
  return { count, problems };
}

/** CS-09 (flag part): every fixture is flagged synthetic or listed as an exception. */
export function checkSyntheticFlags(root: string): Problem[] {
  const problems: Problem[] = [];
  const exceptions = syntheticExceptions(root);
  const fx = join(root, "fixtures");
  for (const rel of fixtureFiles(root)) {
    if (exceptions.has(rel)) continue;
    let doc: unknown;
    try {
      doc = JSON.parse(readFileSync(join(fx, rel), "utf8"));
    } catch {
      problems.push({ check: "CS-09", subject: rel, message: "not valid JSON" });
      continue;
    }
    if (!(typeof doc === "object" && doc !== null && !Array.isArray(doc) && (doc as Record<string, unknown>).synthetic === true)) {
      problems.push({ check: "CS-09", subject: rel, message: 'fixture lacks "synthetic": true and is not listed in fixtures/README.md' });
    }
  }
  for (const rel of [...exceptions].sort()) {
    if (!existsSync(join(fx, rel))) problems.push({ check: "CS-09", subject: rel, message: "listed as a synthetic exception but does not exist" });
  }
  return problems;
}

/** The schemas embedded in the JS build must equal the data files shipped beside them. */
export function checkEmbeddedMatches(store: SchemaStore): Problem[] {
  const problems: Problem[] = [];
  const embedded = embeddedBundle();
  if (embedded.version !== store.version) problems.push({ check: "CS-10", subject: "VERSION", message: `embedded schemas are ${embedded.version}, data files are ${store.version}` });
  const onDisk = new Map(store.all().map((s) => [s.id, canonicalText(s.schema)]));
  const inJs = new Map(embedded.schemas.map((s) => [s.id, canonicalText(s.schema)]));
  for (const id of new Set([...onDisk.keys(), ...inJs.keys()])) {
    if (onDisk.get(id) !== inJs.get(id)) problems.push({ check: "CS-10", subject: id, message: "embedded schema differs from the data file; rebuild the package" });
  }
  return problems;
}

export interface ConformanceOptions {
  mode?: "producer" | "consumer";
  root?: string;
  /** Consumer mode: directory of the consumer's own JSON documents to validate against `schema`. */
  documents?: string;
  schema?: string;
  /** Consumer mode: the pinned contract version; fail if the package data differs. */
  expectVersion?: string;
}

/** Run the conformance suite (producer: source checkout; consumer: package data). */
export function runConformance(options: ConformanceOptions = {}): Report {
  const mode = options.mode ?? "producer";
  const root = contractsRoot(options.root, mode === "producer" ? "checkout" : "bundled");
  const store = loadStore(root);
  const problems: Problem[] = [];
  problems.push(...checkInventory(store, root));
  const { count, problems: fp } = checkFixtures(store, root, loadCases(root));
  let fixturesChecked = count;
  problems.push(...fp);
  problems.push(...checkSyntheticFlags(root));
  if (mode === "consumer") {
    if (!options.root && root === bundledRoot()) problems.push(...checkEmbeddedMatches(store));
    if (options.expectVersion !== undefined && store.version !== options.expectVersion.trim()) {
      problems.push({ check: "CS-10", subject: "VERSION", message: `installed contract package is ${store.version}, the build pins ${options.expectVersion.trim()}` });
    }
    if (options.documents !== undefined) {
      if (!options.schema) problems.push({ check: "CS-10", subject: options.documents, message: "--documents requires --schema" });
      else {
        const validator = createValidator(store);
        for (const p of walkJson(resolve(options.documents))) {
          fixturesChecked++;
          let doc: unknown;
          try {
            doc = JSON.parse(readFileSync(p, "utf8"));
          } catch (err) {
            problems.push({ check: "CS-10", subject: p, message: `not valid JSON: ${(err as Error).message}` });
            continue;
          }
          const res = validate(doc, options.schema, { validator });
          if (!res.valid) problems.push({ check: "CS-10", subject: p, message: res.issues.slice(0, 3).map((i) => i.message).join("; ") });
        }
      }
    }
  } else if (options.documents !== undefined || options.expectVersion !== undefined) {
    throw new Error("documents and expectVersion are consumer-mode options");
  }
  return {
    root,
    mode,
    language: "typescript",
    contract_version: store.version,
    schemas_checked: store.size,
    fixtures_checked: fixturesChecked,
    ok: problems.length === 0,
    problems,
  };
}

export const formatProblem = (p: Problem) => `[${p.check}] ${p.subject}: ${p.message}`;
