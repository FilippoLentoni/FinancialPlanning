/**
 * @finplan/contracts - TypeScript entry point of the FinancialPlanning contract package.
 *
 * Runtime-neutral (no Node built-ins): the schemas are embedded at build time, so
 * this entry works in Node, bundled CDK code and browsers. File-system helpers and
 * the conformance runner live in `@finplan/contracts/node`.
 *
 * The package version comes from contracts/VERSION (see scripts/sync.mjs). Schemas,
 * fixtures and conformance cases also ship as files under `@finplan/contracts/data/*`.
 */
export { VERSION } from "./version.js";
export {
  ID_BASE,
  ID_BASE as CONTRACT_ID_BASE,
  ID_PATTERN,
  CORE_NAMESPACE,
  SchemaNotFound,
  SchemaStore,
  checkBundle,
  embeddedBundle,
  embeddedStore,
  schemaId,
} from "./schemas.js";
export type { BundledSchema, ContractBundle, JsonSchema, SchemaInfo } from "./schemas.js";
export {
  CHECKS,
  CODE_PRIORITY,
  ContractValidator,
  INVALID_IDENTIFIER,
  OPERATION_NOT_PERMITTED,
  VALIDATION_FAILED,
  ValidationResult,
  createValidator,
  isValid,
  pointerOf,
  registerCheck,
  resolvePointer,
  validate,
} from "./validate.js";
export type { CheckContext, CheckFn, ErrorEnvelope, ValidateOptions, ValidationIssue, ValidatorOptions } from "./validate.js";
export {
  CONFIGURATION_ID_PATTERN,
  CONFIGURATION_ID_PREFIX,
  CanonicalizationError,
  canonicalText,
  canonicalize,
  configurationId,
  configurationIdFromJson,
  parseStrict,
  requestHash,
} from "./canonical.js";
export type { JsonValue } from "./canonical.js";
export {
  DERIVED_KEY_PATTERN,
  DERIVED_KEY_PREFIX,
  ENVIRONMENTS,
  IDEMPOTENCY_KEY_PATTERN,
  TOOL_NAME_PATTERN,
  deriveProxiedKey,
  isDerivedKey,
  proxiedKey,
  proxiedPreimage,
} from "./keys.js";
export type { Environment } from "./keys.js";
export { sha256, sha256Hex } from "./sha256.js";
