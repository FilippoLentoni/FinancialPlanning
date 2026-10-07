#!/usr/bin/env node
/**
 * `finplan-conformance-ts`: TypeScript side of the `finplan-conformance` runner.
 *
 *   finplan-conformance-ts conformance [--mode producer|consumer] [--root DIR]
 *                                      [--documents DIR --schema NAME] [--expect-version X] [--json]
 *   finplan-conformance-ts validate --schema NAME [--root DIR] [--context KEY=JSON]... [--envelope] FILE...
 *   finplan-conformance-ts configuration-id FILE...
 *   finplan-conformance-ts proxied-key --caller ID --env ENV --tool NAME --key KEY
 *
 * Exit codes: 0 pass, 1 fail, 2 usage error.
 */
import { readFileSync } from "node:fs";
import { parseArgs } from "node:util";
import { configurationIdFromJson } from "./canonical.js";
import { deriveProxiedKey } from "./keys.js";
import { formatProblem, loadStore, runConformance } from "./node.js";
import { SchemaNotFound, embeddedStore } from "./schemas.js";
import { validate } from "./validate.js";
import { VERSION } from "./version.js";

const USAGE = `finplan-conformance-ts ${VERSION}

usage: finplan-conformance-ts <subcommand> [args...]

subcommands:
  conformance       Run the fixture conformance suite (producer or consumer mode).
  validate          Validate JSON documents against a contract schema.
  configuration-id  Print the configuration_id of configuration JSON files.
  proxied-key       Print the derived idempotency key of a proxied call.
`;

function parseContext(items: string[]): Record<string, unknown> {
  const ctx: Record<string, unknown> = {};
  for (const item of items) {
    const at = item.indexOf("=");
    const key = at < 0 ? item : item.slice(0, at);
    const raw = at < 0 ? "" : item.slice(at + 1);
    try {
      ctx[key] = JSON.parse(raw);
    } catch {
      ctx[key] = raw;
    }
  }
  return ctx;
}

function cmdConformance(argv: string[]): number {
  const { values } = parseArgs({
    args: argv,
    options: {
      mode: { type: "string", default: "producer" },
      root: { type: "string" },
      documents: { type: "string" },
      schema: { type: "string" },
      "expect-version": { type: "string" },
      json: { type: "boolean", default: false },
    },
  });
  if (values.mode !== "producer" && values.mode !== "consumer") {
    console.error("error: --mode must be producer or consumer");
    return 2;
  }
  if (values.mode === "producer" && (values.documents || values["expect-version"])) {
    console.error("error: --documents and --expect-version are consumer-mode options");
    return 2;
  }
  const report = runConformance({
    mode: values.mode,
    root: values.root,
    documents: values.documents,
    schema: values.schema,
    expectVersion: values["expect-version"],
  });
  if (values.json) console.log(JSON.stringify(report, null, 2));
  else {
    for (const p of report.problems) console.log(formatProblem(p));
    console.log(
      `${report.ok ? "PASS" : "FAIL"}: ${report.mode} conformance (typescript), contracts ${report.contract_version}, ${report.schemas_checked} schemas, ${report.fixtures_checked} fixtures, ${report.problems.length} problems`,
    );
  }
  return report.ok ? 0 : 1;
}

function cmdValidate(argv: string[]): number {
  const { values, positionals } = parseArgs({
    args: argv,
    allowPositionals: true,
    options: {
      schema: { type: "string", short: "s" },
      root: { type: "string" },
      context: { type: "string", multiple: true, default: [] },
      envelope: { type: "boolean", default: false },
    },
  });
  if (!values.schema || positionals.length === 0) {
    console.error("usage: finplan-conformance-ts validate --schema NAME [--root DIR] [--context KEY=JSON]... [--envelope] FILE...");
    return 2;
  }
  let store;
  try {
    store = values.root ? loadStore(values.root) : embeddedStore();
    store.get(values.schema);
  } catch (err) {
    console.error(`error: ${err instanceof SchemaNotFound ? err.message : String(err)}`);
    return 2;
  }
  const ctx = parseContext(values.context as string[]);
  let rc = 0;
  for (const file of positionals) {
    let doc: unknown;
    try {
      doc = JSON.parse(readFileSync(file, "utf8"));
    } catch (err) {
      console.log(JSON.stringify({ file, valid: false, error: `unreadable JSON: ${(err as Error).name}` }));
      rc = 1;
      continue;
    }
    const res = validate(doc, values.schema, { store, context: ctx });
    const out: Record<string, unknown> = { file, ...res.toDict() };
    if (values.envelope && !res.valid) out.envelope = res.toErrorEnvelope("cli-validate-0001", store.version);
    console.log(JSON.stringify(out));
    if (!res.valid) rc = 1;
  }
  return rc;
}

function cmdConfigurationId(argv: string[]): number {
  if (!argv.length) {
    console.error("usage: finplan-conformance-ts configuration-id FILE...");
    return 2;
  }
  let rc = 0;
  for (const file of argv) {
    try {
      console.log(`${configurationIdFromJson(readFileSync(file))}  ${file}`);
    } catch (err) {
      console.error(`${file}: ${(err as Error).message}`);
      rc = 1;
    }
  }
  return rc;
}

function cmdProxiedKey(argv: string[]): number {
  const { values } = parseArgs({
    args: argv,
    options: { caller: { type: "string" }, env: { type: "string" }, tool: { type: "string" }, key: { type: "string" } },
  });
  if (!values.caller || !values.env || !values.tool || !values.key) {
    console.error("usage: finplan-conformance-ts proxied-key --caller ID --env ENV --tool NAME --key KEY");
    return 2;
  }
  try {
    console.log(deriveProxiedKey(values.caller, values.env, values.tool, values.key));
    return 0;
  } catch (err) {
    console.error(`error: ${(err as Error).message}`);
    return 2;
  }
}

export function main(argv: string[]): number {
  const [sub, ...rest] = argv;
  try {
    switch (sub) {
      case "conformance":
        return cmdConformance(rest);
      case "validate":
        return cmdValidate(rest);
      case "configuration-id":
        return cmdConfigurationId(rest);
      case "proxied-key":
        return cmdProxiedKey(rest);
      case undefined:
      case "-h":
      case "--help":
        process.stdout.write(USAGE);
        return sub === undefined ? 2 : 0;
      case "--version":
        console.log(VERSION);
        return 0;
      default:
        console.error(`unknown subcommand: ${sub}\n`);
        process.stderr.write(USAGE);
        return 2;
    }
  } catch (err) {
    console.error(`error: ${(err as Error).message}`);
    return 2;
  }
}

process.exitCode = main(process.argv.slice(2));
