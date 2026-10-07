/**
 * Schema bundle and registry (TypeScript twin of `finplan_contracts.schemas`).
 *
 * A `ContractBundle` holds every contract schema of one contracts version (core and
 * domain adapter namespaces), the domain registry and the version. The default
 * bundle is embedded in the package at build time by `scripts/sync.mjs` from
 * `contracts/core`, `contracts/<domain>` and `contracts/domains`, so validation needs
 * no file system and no network (`$id`s are identifiers only, never fetched).
 *
 * Schema *names* are the path below `<namespace>/v<major>/` without `.json` (for
 * example `plan-version` or `tools/get-plan-request`); names are unique across
 * namespaces.
 */
import { BUNDLE } from "./bundle.js";

/** Base of every contract `$id` (design D3). */
export const ID_BASE = "https://contracts.finplan.invalid/";
export const ID_PATTERN = /^https:\/\/contracts\.finplan\.invalid\/(?<ns>[a-z][a-z0-9_]*)\/v(?<major>[0-9]+)\/(?<name>[a-z0-9/-]+)\.json$/;
export const CORE_NAMESPACE = "core";

export type JsonSchema = Record<string, unknown>;

export interface BundledSchema {
  name: string;
  namespace: string;
  major: number;
  id: string;
  schema: JsonSchema;
}

export interface ContractBundle {
  version: string;
  schemas: BundledSchema[];
  domainRegistry: Record<string, unknown>;
}

export interface SchemaInfo extends BundledSchema {
  /** Semantic checks declared in `x-finplan-checks`. */
  checks: string[];
}

export class SchemaNotFound extends Error {
  constructor(public readonly key: string) {
    super(`schema not found: ${key}`);
    this.name = "SchemaNotFound";
  }
}

/** Build a contract schema `$id`, for example `schemaId("core", "plan-version")`. */
export function schemaId(namespace: string, name: string, major = 1): string {
  return `${ID_BASE}${namespace}/v${major}/${name}.json`;
}

/** Check that a bundle's schemas carry `$id`s matching their location and unique names. */
export function checkBundle(bundle: ContractBundle): void {
  const names = new Set<string>();
  for (const s of bundle.schemas) {
    const expected = schemaId(s.namespace, s.name, s.major);
    if (s.schema["$id"] !== expected || s.id !== expected) {
      throw new Error(`${s.namespace}/v${s.major}/${s.name}.json: $id ${JSON.stringify(s.schema["$id"])} does not match its location (expected ${JSON.stringify(expected)})`);
    }
    if (names.has(s.name)) throw new Error(`duplicate schema name ${JSON.stringify(s.name)}`);
    names.add(s.name);
  }
}

/** All contract schemas of one bundle, indexed by name and `$id`. */
export class SchemaStore {
  readonly version: string;
  private readonly byName = new Map<string, SchemaInfo>();
  private readonly byId = new Map<string, SchemaInfo>();
  private readonly registry: Record<string, unknown>;
  /** Where the bundle came from (a directory for file-system stores, "embedded" otherwise). */
  readonly origin: string;

  constructor(bundle: ContractBundle = BUNDLE, origin = "embedded") {
    checkBundle(bundle);
    this.version = bundle.version;
    this.registry = bundle.domainRegistry;
    this.origin = origin;
    for (const s of [...bundle.schemas].sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0))) {
      const raw = s.schema["x-finplan-checks"];
      const info: SchemaInfo = { ...s, checks: Array.isArray(raw) ? raw.map(String) : [] };
      this.byName.set(s.name, info);
      this.byId.set(s.id, info);
    }
  }

  names(): string[] {
    return [...this.byName.keys()];
  }

  all(): SchemaInfo[] {
    return [...this.byName.values()];
  }

  get size(): number {
    return this.byName.size;
  }

  private resolveKey(key: string): SchemaInfo | undefined {
    const direct = this.byId.get(key) ?? this.byName.get(key);
    if (direct) return direct;
    // Accept "<namespace>/<name>" and "<namespace>/v1/<name>(.json)" forms.
    const m = /^(?<ns>[a-z][a-z0-9_]*)\/(v(?<major>[0-9]+)\/)?(?<name>[a-z0-9/-]+?)(\.json)?$/.exec(key);
    if (m?.groups) {
      const info = this.byName.get(m.groups.name);
      if (info && info.namespace === m.groups.ns && (m.groups.major === undefined || Number(m.groups.major) === info.major)) return info;
    }
    return undefined;
  }

  has(key: unknown): boolean {
    return typeof key === "string" && this.resolveKey(key) !== undefined;
  }

  /** Look up a schema by name (`plan-version`), `ns/name` or `$id`. */
  get(key: string): SchemaInfo {
    const info = this.resolveKey(key);
    if (!info) throw new SchemaNotFound(key);
    return info;
  }

  domainRegistry(): Record<string, unknown> {
    return this.registry;
  }
}

let defaultStore: SchemaStore | undefined;

/** The store over the schemas embedded in this package version. */
export function embeddedStore(): SchemaStore {
  return (defaultStore ??= new SchemaStore(BUNDLE, "embedded"));
}

/** The embedded bundle (schemas, domain registry, version). */
export function embeddedBundle(): ContractBundle {
  return BUNDLE;
}
