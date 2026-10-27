import { randomUUID } from 'node:crypto';
import type { Application, Request, Response } from 'express';
import { getExecutionContext } from '@databricks/appkit';
import { z } from 'zod';
import { includes } from './lib/directory';
import { userEmail } from './lib/identity';
import { checkDecision, DECISIONS, PRIVACY_STATUS, REVIEWABLE_STATUSES, type ActingAs } from './lib/rules';
import { settings, table, T } from './lib/settings';
import { runSql, type Row, type SqlValue } from './lib/sql';

/*
 * Review App API. Every read is filtered on the server by the signed-in user's roles, and every
 * write is a parameterized statement run as the App service principal, which holds only:
 * SELECT on the review tables; MODIFY on submissions, the scope registry, the suggestion mapping,
 * and events; and CAN_MANAGE_RUN on the scan job. Ingestion re-validates every submission.
 */

const REFERENCE_DATA_BATCH_ID = 'REFERENCE_DATA';
const MAX_DECISIONS = 500;

interface ScopeRole {
  scope_id: string;
  catalog_name: string;
  schema_name: string;
  steward_group: string;
  governance_owner: string;
  risk_tier: string;
  roles: ActingAs[];
}

interface Me {
  email: string;
  env: string;
  isGovernance: boolean;
  scopes: ScopeRole[];
}

class HttpError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly details?: unknown,
  ) {
    super(message);
  }
}

function handler(fn: (req: Request, res: Response) => Promise<void>) {
  return async (req: Request, res: Response) => {
    try {
      await fn(req, res);
    } catch (err) {
      if (err instanceof HttpError) {
        res.status(err.status).json({ error: err.message, details: err.details });
      } else if (err instanceof z.ZodError) {
        res.status(400).json({ error: 'Invalid request', details: err.issues });
      } else {
        console.error(err);
        res.status(500).json({ error: 'The request failed; try again or contact Data Platform' });
      }
    }
  };
}

function requireUser(req: Request): string {
  const email = userEmail(req);
  if (!email) throw new HttpError(401, 'No authenticated user on the request');
  return email;
}

async function loadMe(email: string): Promise<Me> {
  const s = settings();
  const scopes = await runSql(
    `SELECT scope_id, catalog_name, schema_name, steward_group, governance_owner, risk_tier
     FROM ${table(T.scope)}
     WHERE environment = :env AND current_date() BETWEEN effective_date AND coalesce(expiration_date, DATE'9999-12-31')
     ORDER BY scope_id`,
    { env: s.PHI_ENV },
  );
  const out: ScopeRole[] = [];
  for (const r of scopes) {
    const roles: ActingAs[] = [];
    if (await includes(email, r.steward_group)) roles.push('STEWARD');
    if (await includes(email, r.governance_owner)) roles.push('GOVERNANCE_OWNER');
    out.push({
      scope_id: r.scope_id ?? '',
      catalog_name: r.catalog_name ?? '',
      schema_name: r.schema_name ?? '',
      steward_group: r.steward_group ?? '',
      governance_owner: r.governance_owner ?? '',
      risk_tier: r.risk_tier ?? '',
      roles,
    });
  }
  return { email, env: s.PHI_ENV, isGovernance: await includes(email, s.PHI_GOVERNANCE_GROUP), scopes: out };
}

async function requireGovernance(req: Request): Promise<string> {
  const email = requireUser(req);
  if (!(await includes(email, settings().PHI_GOVERNANCE_GROUP))) {
    throw new HttpError(403, `Only members of ${settings().PHI_GOVERNANCE_GROUP} can do this`);
  }
  return email;
}

async function allowedValues(): Promise<Set<string>> {
  const s = settings();
  if (s.PHI_ALLOWED_VALUES_OVERRIDE) {
    return new Set(s.PHI_ALLOWED_VALUES_OVERRIDE.split(',').map((v) => v.trim()).filter(Boolean));
  }
  const policy = await getExecutionContext().client.toLegacyWorkspaceClient().tagPolicies.getTagPolicy({ tag_key: s.PHI_TAG_KEY });
  return new Set((policy.values ?? []).map((v: { name?: string }) => v.name).filter((v): v is string => !!v));
}

/** Items the user may act on. Review: steward or governance owner. Privacy: Data Governance or owner. */
async function queue(me: Me, kind: 'review' | 'privacy'): Promise<Row[]> {
  const scopeIds =
    kind === 'review'
      ? me.scopes.filter((s) => s.roles.length > 0).map((s) => s.scope_id)
      : me.isGovernance
        ? me.scopes.map((s) => s.scope_id)
        : me.scopes.filter((s) => s.roles.includes('GOVERNANCE_OWNER')).map((s) => s.scope_id);
  if (scopeIds.length === 0) return [];
  const statuses = kind === 'review' ? REVIEWABLE_STATUSES : [PRIVACY_STATUS];
  return runSql(
    `WITH processed AS (
       SELECT DISTINCT discriminator AS submission_set_id FROM ${table(T.event)}
       WHERE event_type = 'SUBMISSION_PROCESSED'),
     waiting AS (
       SELECT s.review_item_id, max(s.reviewed_at) AS submitted_at FROM ${table(T.submission)} s
       LEFT ANTI JOIN processed p ON p.submission_set_id = s.submission_set_id
       GROUP BY s.review_item_id),
     last_error AS (
       SELECT review_item_id, to_json(event_details:errors) AS errors, event_timestamp
       FROM ${table(T.event)} WHERE event_type = 'VALIDATION_FAILED'
       QUALIFY row_number() OVER (PARTITION BY review_item_id ORDER BY event_timestamp DESC) = 1)
     SELECT i.review_item_id, i.review_batch_id, b.scope_id, b.due_at, i.catalog_name, i.schema_name, i.table_name,
            i.table_type, i.column_name, i.full_data_type, i.column_comment, i.issue_type, i.current_tag_value,
            i.suggested_tag_value, i.suggestion_reason, i.status, i.decision, i.steward_comment, i.reviewed_by,
            CASE WHEN i.status = 'VALIDATION_FAILED' OR i.status = 'NEEDS_PRIVACY_REVIEW' THEN e.errors END AS last_errors,
            w.submitted_at AS awaiting_validation_since
     FROM ${table(T.item)} i
     JOIN ${table(T.batch)} b ON b.review_batch_id = i.review_batch_id
     LEFT JOIN last_error e ON e.review_item_id = i.review_item_id AND e.event_timestamp >= i.audit_updated_at
     LEFT JOIN waiting w ON w.review_item_id = i.review_item_id
     WHERE array_contains(split(:scope_ids, ','), b.scope_id)
       AND array_contains(split(:statuses, ','), i.status)
     ORDER BY b.due_at, i.catalog_name, i.schema_name, i.table_name, i.column_name
     LIMIT 2000`,
    { scope_ids: scopeIds.join(','), statuses: statuses.join(',') },
  );
}

const DecisionSchema = z.object({
  review_item_id: z.string().regex(/^ITEM-[0-9A-F]{16}$/),
  decision: z.enum(DECISIONS),
  corrected_tag_value: z.string().max(200).nullish(),
  steward_comment: z.string().max(4000).nullish(),
  suggested_tag_value_seen: z.string().max(200).nullish(),
});
const SubmissionSchema = z.object({
  kind: z.enum(['REVIEW', 'PRIVACY']),
  decisions: z.array(DecisionSchema).min(1).max(MAX_DECISIONS),
});

function auditValues(prefix = ''): string {
  return `current_timestamp(), current_user(), :${prefix}proc, :${prefix}run`;
}

async function writeEvent(
  eventType: string,
  actor: string,
  details: unknown,
  discriminator: string,
  runId: string,
): Promise<void> {
  const s = settings();
  await runSql(
    `INSERT INTO ${table(T.event)} (event_id, review_batch_id, review_item_id, event_type, event_timestamp,
       business_actor, event_details, discriminator,
       audit_created_at, audit_created_by, audit_created_process, audit_created_run_id)
     SELECT ${s.schemaFqn}.make_key('EVT', array(:etype, :batch, CAST(NULL AS STRING), :run, :disc)),
            :batch, NULL, :etype, current_timestamp(), :actor, parse_json(:details), :disc, ${auditValues()}`,
    {
      etype: eventType,
      batch: REFERENCE_DATA_BATCH_ID,
      run: runId,
      disc: discriminator,
      actor,
      details: JSON.stringify(details),
      proc: s.processName,
    },
  );
}

const ScopeSchema = z.object({
  catalog_name: z.string().regex(/^[A-Za-z0-9_]+$/),
  schema_name: z.string().regex(/^([A-Za-z0-9_]+|\*)$/),
  risk_tier: z.enum(['HIGH', 'STANDARD']),
  steward_group: z.string().trim().min(1).max(256),
  governance_owner: z.string().trim().min(1).max(256),
  scan_enabled: z.boolean(),
  effective_date: z.string().regex(/^\d{4}-\d{2}-\d{2}$/),
  expiration_date: z
    .string()
    .regex(/^\d{4}-\d{2}-\d{2}$/)
    .nullish(),
});
const MappingSchema = z.object({
  phi_tag_value: z.string().trim().min(1).max(200),
  is_active: z.boolean(),
  notes: z.string().max(2000).nullish(),
});
const CorrectionSchema = z.object({
  column: z.string().regex(/^[^.\s`]+\.[^.\s`]+\.[^.\s`]+\.[^.\s`]+$/, 'Use catalog.schema.table.column'),
  reason: z.string().trim().min(5).max(2000),
});

export function registerRoutes(app: Application): void {
  app.get(
    '/api/review/me',
    handler(async (req, res) => {
      res.json(await loadMe(requireUser(req)));
    }),
  );

  app.get(
    '/api/review/allowed-values',
    handler(async (req, res) => {
      requireUser(req);
      res.json({ values: [...(await allowedValues())].sort() });
    }),
  );

  app.get(
    '/api/review/queue',
    handler(async (req, res) => {
      const kind = req.query.kind === 'privacy' ? 'privacy' : 'review';
      const me = await loadMe(requireUser(req));
      res.json({ items: await queue(me, kind), asOf: new Date().toISOString() });
    }),
  );

  app.post(
    '/api/review/submissions',
    handler(async (req, res) => {
      const email = requireUser(req);
      const body = SubmissionSchema.parse(req.body);
      const privacy = body.kind === 'PRIVACY';
      const me = await loadMe(email);
      const allowed = await allowedValues();
      const ids = [...new Set(body.decisions.map((d) => d.review_item_id))];
      if (ids.length !== body.decisions.length) throw new HttpError(400, 'Each item may appear only once');
      const items = new Map(
        (
          await runSql(
            `SELECT i.review_item_id, i.review_batch_id, i.status, i.suggested_tag_value, b.scope_id
             FROM ${table(T.item)} i JOIN ${table(T.batch)} b ON b.review_batch_id = i.review_batch_id
             WHERE array_contains(split(:ids, ','), i.review_item_id)`,
            { ids: ids.join(',') },
          )
        ).map((r) => [r.review_item_id ?? '', r]),
      );
      const problems: Record<string, string[]> = {};
      for (const d of body.decisions) {
        const it = items.get(d.review_item_id);
        if (!it) {
          problems[d.review_item_id] = ['Unknown review item'];
          continue;
        }
        const scope = me.scopes.find((s) => s.scope_id === it.scope_id);
        const authorized = privacy
          ? me.isGovernance || !!scope?.roles.includes('GOVERNANCE_OWNER')
          : !!scope && scope.roles.length > 0;
        const errors = authorized ? [] : ['You are not a reviewer for this scope'];
        errors.push(
          ...checkDecision({ status: it.status ?? '', suggested_tag_value: it.suggested_tag_value }, d, privacy, allowed),
        );
        if (errors.length) problems[d.review_item_id] = errors;
      }
      if (Object.keys(problems).length) throw new HttpError(422, 'Some decisions need changes; nothing was submitted', problems);

      // One submission set per batch, so ingestion validates and records each batch independently.
      const requestId = `APP-${randomUUID()}`;
      const byBatch = new Map<string, typeof body.decisions>();
      for (const d of body.decisions) {
        const batch = items.get(d.review_item_id)?.review_batch_id ?? '';
        byBatch.set(batch, [...(byBatch.get(batch) ?? []), d]);
      }
      const s = settings();
      const params: Record<string, SqlValue> = { proc: s.processName, run: requestId, stype: body.kind, by: email };
      const rows: string[] = [];
      let n = 0;
      for (const [batch, decisions] of byBatch) {
        const setKey = `set${rows.length}_${n}`;
        params[setKey] = `${requestId}:${batch}`;
        for (const d of decisions) {
          const p = `r${n++}_`;
          Object.assign(params, {
            [`${p}batch`]: batch,
            [`${p}item`]: d.review_item_id,
            [`${p}dec`]: d.decision,
            [`${p}corr`]: d.corrected_tag_value?.trim() || null,
            [`${p}cmt`]: d.steward_comment?.trim() || null,
            [`${p}seen`]: d.suggested_tag_value_seen ?? null,
          });
          rows.push(
            `(${s.schemaFqn}.make_key('SUB', array(:${setKey}, :${p}item)), :${setKey}, :stype, :${p}batch, :${p}item,
              :${p}dec, :${p}corr, :${p}cmt, :${p}seen, :by, current_timestamp(), ${auditValues()})`,
          );
        }
      }
      await runSql(
        `INSERT INTO ${table(T.submission)} (submission_id, submission_set_id, submission_type, review_batch_id,
           review_item_id, decision, corrected_tag_value, steward_comment, suggested_tag_value_seen, reviewed_by,
           reviewed_at, audit_created_at, audit_created_by, audit_created_process, audit_created_run_id)
         VALUES ${rows.join(',\n')}`,
        params,
      );
      res.json({ submitted: body.decisions.length, requestId });
    }),
  );

  app.get(
    '/api/review/scopes',
    handler(async (req, res) => {
      requireUser(req);
      res.json({
        scopes: await runSql(
          `SELECT scope_id, catalog_name, schema_name, environment, risk_tier, steward_group, governance_owner,
                  scan_enabled, effective_date, expiration_date, audit_updated_at, audit_updated_by
           FROM ${table(T.scope)} WHERE environment = :env ORDER BY scope_id`,
          { env: settings().PHI_ENV },
        ),
        governedCatalogPrefix: settings().PHI_GOVERNED_CATALOG_PREFIX,
      });
    }),
  );

  app.put(
    '/api/review/scopes/:scopeId',
    handler(async (req, res) => {
      const email = await requireGovernance(req);
      const scopeId = z.string().regex(/^[A-Z0-9_-]{3,64}$/, 'Use SCOPE-<DOMAIN>-<ENV> in upper case').parse(req.params.scopeId);
      const v = ScopeSchema.parse(req.body);
      const s = settings();
      if (!v.catalog_name.startsWith(s.PHI_GOVERNED_CATALOG_PREFIX)) {
        throw new HttpError(422, `The catalog must start with ${s.PHI_GOVERNED_CATALOG_PREFIX} in this environment`);
      }
      if (v.expiration_date && v.expiration_date < v.effective_date) {
        throw new HttpError(422, 'The expiration date must not be before the effective date');
      }
      const before = (await runSql(`SELECT * FROM ${table(T.scope)} WHERE scope_id = :id`, { id: scopeId }))[0] ?? null;
      const runId = `APP-${randomUUID()}`;
      await runSql(
        `MERGE INTO ${table(T.scope)} t
         USING (SELECT :id AS scope_id) s ON t.scope_id = s.scope_id
         WHEN MATCHED THEN UPDATE SET catalog_name = :cat, schema_name = :sch, risk_tier = :tier,
           steward_group = :stewards, governance_owner = :owner, scan_enabled = :enabled,
           effective_date = CAST(:eff AS DATE), expiration_date = CAST(:exp AS DATE),
           audit_updated_at = current_timestamp(), audit_updated_by = current_user(),
           audit_updated_process = :proc, audit_updated_run_id = :run
         WHEN NOT MATCHED THEN INSERT (scope_id, catalog_name, schema_name, environment, risk_tier, steward_group,
           governance_owner, scan_enabled, effective_date, expiration_date,
           audit_created_at, audit_created_by, audit_created_process, audit_created_run_id,
           audit_updated_at, audit_updated_by, audit_updated_process, audit_updated_run_id)
         VALUES (:id, :cat, :sch, :env, :tier, :stewards, :owner, :enabled, CAST(:eff AS DATE), CAST(:exp AS DATE),
           ${auditValues()}, ${auditValues()})`,
        {
          id: scopeId,
          cat: v.catalog_name,
          sch: v.schema_name,
          env: s.PHI_ENV,
          tier: v.risk_tier,
          stewards: v.steward_group,
          owner: v.governance_owner,
          enabled: v.scan_enabled,
          eff: v.effective_date,
          exp: v.expiration_date ?? null,
          proc: s.processName,
          run: runId,
        },
      );
      await writeEvent('SCOPE_REGISTRY_UPDATED', email, { scope_id: scopeId, before, after: { ...v, environment: s.PHI_ENV } }, scopeId, runId);
      res.json({ saved: scopeId });
    }),
  );

  app.get(
    '/api/review/mappings',
    handler(async (req, res) => {
      requireUser(req);
      res.json({
        mappings: await runSql(
          `SELECT class_tag, phi_tag_value, is_active, notes, audit_updated_at, audit_updated_by
           FROM ${table(T.mapping)} ORDER BY class_tag`,
        ),
      });
    }),
  );

  app.put(
    '/api/review/mappings/:classTag',
    handler(async (req, res) => {
      const email = await requireGovernance(req);
      const classTag = z.string().trim().regex(/^class\.[a-z0-9_.]+$/, 'Use a Data Classification class such as class.name').parse(req.params.classTag);
      const v = MappingSchema.parse(req.body);
      if (!(await allowedValues()).has(v.phi_tag_value)) {
        throw new HttpError(422, `${v.phi_tag_value} is not an allowed value of the governed tag`);
      }
      const before = (await runSql(`SELECT * FROM ${table(T.mapping)} WHERE class_tag = :k`, { k: classTag }))[0] ?? null;
      const runId = `APP-${randomUUID()}`;
      const s = settings();
      await runSql(
        `MERGE INTO ${table(T.mapping)} t
         USING (SELECT :k AS class_tag) s ON t.class_tag = s.class_tag
         WHEN MATCHED THEN UPDATE SET phi_tag_value = :val, is_active = :active, notes = :notes,
           audit_updated_at = current_timestamp(), audit_updated_by = current_user(),
           audit_updated_process = :proc, audit_updated_run_id = :run
         WHEN NOT MATCHED THEN INSERT (class_tag, phi_tag_value, is_active, notes,
           audit_created_at, audit_created_by, audit_created_process, audit_created_run_id,
           audit_updated_at, audit_updated_by, audit_updated_process, audit_updated_run_id)
         VALUES (:k, :val, :active, :notes, ${auditValues()}, ${auditValues()})`,
        { k: classTag, val: v.phi_tag_value, active: v.is_active, notes: v.notes?.trim() || null, proc: s.processName, run: runId },
      );
      await writeEvent('SUGGESTION_MAPPING_UPDATED', email, { class_tag: classTag, before, after: v }, classTag, runId);
      res.json({ saved: classTag });
    }),
  );

  app.post(
    '/api/review/corrections',
    handler(async (req, res) => {
      const email = await requireGovernance(req);
      const v = CorrectionSchema.parse(req.body);
      const s = settings();
      const run = await getExecutionContext().client.jobs.runNow({
        job_id: Number(s.PHI_SCAN_JOB_ID),
        job_parameters: { correction_column: v.column, correction_reason: v.reason, correction_requested_by: email },
      });
      const runId = String(run.run_id ?? '');
      await writeEvent('CORRECTION_REQUESTED', email, { column: v.column, reason: v.reason, scan_run_id: runId }, v.column, `APP-${randomUUID()}`);
      res.json({ scanRunId: runId });
    }),
  );
}
