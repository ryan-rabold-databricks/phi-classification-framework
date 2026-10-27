import { z } from 'zod';

// Unity Catalog identifier part accepted for the governance catalog, schema, and catalog prefix.
const ident = z.string().regex(/^[A-Za-z0-9_]+$/, 'must contain only letters, digits, and underscores');

const EnvSchema = z.object({
  PHI_ENV: z.enum(['dev', 'qa', 'prod']),
  PHI_GOVERNANCE_CATALOG: ident,
  PHI_GOVERNANCE_SCHEMA: ident,
  PHI_GOVERNANCE_GROUP: z.string().min(1),
  PHI_GOVERNED_CATALOG_PREFIX: ident,
  PHI_TAG_KEY: z.string().min(1),
  PHI_SCAN_JOB_ID: z.string().regex(/^\d+$/),
  PHI_ALLOWED_VALUES_OVERRIDE: z
    .string()
    .default('')
    .transform((v) => v.replace(/^values:/, '')),
  DATABRICKS_WAREHOUSE_ID: z.string().min(1),
  // Local development only: identity used when the Apps proxy headers are absent.
  PHI_DEV_USER_EMAIL: z.string().default(''),
  PHI_DEV_USER_GROUPS: z.string().default(''),
  NODE_ENV: z.string().default('production'),
});

export type Settings = z.infer<typeof EnvSchema> & {
  /** Logical process name recorded in audit_*_process for every App write. */
  processName: string;
  /** Backtick-quoted governance schema, e.g. `dev_gov`.`data_governance`. */
  schemaFqn: string;
};

let cached: Settings | undefined;

export function settings(): Settings {
  if (cached) return cached;
  const env = EnvSchema.parse(process.env);
  if (env.PHI_ALLOWED_VALUES_OVERRIDE && env.PHI_ENV !== 'dev') {
    throw new Error('PHI_ALLOWED_VALUES_OVERRIDE is only permitted in dev');
  }
  cached = {
    ...env,
    processName: `${env.PHI_ENV}_phi_review_app`,
    schemaFqn: `\`${env.PHI_GOVERNANCE_CATALOG}\`.\`${env.PHI_GOVERNANCE_SCHEMA}\``,
  };
  return cached;
}

/** Fully qualified, quoted name of a governance table. */
export function table(name: string): string {
  return `${settings().schemaFqn}.\`${name}\``;
}

export const T = {
  scope: 'classification_scope_registry',
  mapping: 'classification_suggestion_mapping',
  batch: 'classification_review_batch',
  item: 'classification_review_item',
  event: 'classification_review_event',
  submission: 'classification_review_submission',
} as const;
