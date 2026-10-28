import { getExecutionContext } from '@databricks/appkit';
import { settings } from './settings';

/** Value bound to a named :parameter. null binds SQL NULL. */
export type SqlValue = string | number | boolean | null;
export type Row = Record<string, string | null>;

const POLL_MS = 500;
const MAX_WAIT_MS = 120_000;

function sqlType(v: SqlValue): string {
  if (typeof v === 'boolean') return 'BOOLEAN';
  if (typeof v === 'number') return Number.isInteger(v) ? 'BIGINT' : 'DOUBLE';
  return 'STRING';
}

/**
 * Run one statement on the App's SQL warehouse as the App service principal.
 * Every value is bound as a named parameter; callers never interpolate input into SQL.
 */
export async function runSql(statement: string, params: Record<string, SqlValue> = {}): Promise<Row[]> {
  const { client } = getExecutionContext();
  const parameters = Object.entries(params).map(([name, value]) =>
    value === null ? { name, type: 'STRING' } : { name, value: String(value), type: sqlType(value) }
  );
  let res = await client.statementExecution.executeStatement({
    warehouse_id: settings().DATABRICKS_WAREHOUSE_ID,
    statement,
    parameters,
    wait_timeout: '30s',
    on_wait_timeout: 'CONTINUE',
    disposition: 'INLINE',
    format: 'JSON_ARRAY',
  });
  const started = Date.now();
  while (res.status?.state === 'PENDING' || res.status?.state === 'RUNNING') {
    if (Date.now() - started > MAX_WAIT_MS) throw new Error('SQL statement timed out');
    await new Promise((r) => setTimeout(r, POLL_MS));
    res = await client.statementExecution.getStatement({ statement_id: res.statement_id ?? '' });
  }
  if (res.status?.state !== 'SUCCEEDED') {
    throw new Error(res.status?.error?.message ?? `SQL statement ${res.status?.state ?? 'failed'}`);
  }
  const cols = res.manifest?.schema?.columns?.map((c) => c.name ?? '') ?? [];
  return (res.result?.data_array ?? []).map((values) => Object.fromEntries(cols.map((c, i) => [c, values[i] ?? null])));
}
