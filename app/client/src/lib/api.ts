export type ActingAs = 'STEWARD' | 'GOVERNANCE_OWNER' | 'DATA_GOVERNANCE';
export type Decision = 'APPROVE_SUGGESTION' | 'CORRECT_CLASSIFICATION' | 'CONFIRM_NOT_PHI' | 'REQUEST_PRIVACY_REVIEW';

export interface ScopeRole {
  scope_id: string;
  catalog_name: string;
  schema_name: string;
  steward_emails: string[];
  governance_owner_emails: string[];
  risk_tier: string;
  roles: ActingAs[];
}

export interface Me {
  email: string;
  env: string;
  isGovernance: boolean;
  scopes: ScopeRole[];
}

export interface QueueItem {
  review_item_id: string;
  review_batch_id: string;
  scope_id: string;
  due_at: string;
  catalog_name: string;
  schema_name: string;
  table_name: string;
  table_type: string;
  column_name: string;
  full_data_type: string;
  column_comment: string | null;
  issue_type: string;
  current_tag_value: string | null;
  suggested_tag_value: string | null;
  suggestion_reason: string | null;
  status: string;
  decision: string | null;
  steward_comment: string | null;
  reviewed_by: string | null;
  last_errors: string | null;
  awaiting_validation_since: string | null;
}

export interface DecisionDraft {
  review_item_id: string;
  decision: Decision;
  corrected_tag_value?: string | null;
  steward_comment?: string | null;
  suggested_tag_value_seen?: string | null;
}

export interface ScopeRow {
  scope_id: string;
  catalog_name: string;
  schema_name: string;
  environment: string;
  risk_tier: string;
  /** JSON array of emails, as returned by the server */
  steward_emails: string;
  governance_owner_emails: string;
  scan_enabled: string;
  effective_date: string;
  expiration_date: string | null;
  audit_updated_at: string;
  audit_updated_by: string;
}

export interface MappingRow {
  class_tag: string;
  phi_tag_value: string;
  is_active: string;
  notes: string | null;
  audit_updated_at: string;
  audit_updated_by: string;
}

export class ApiError extends Error {
  readonly status: number;
  readonly details?: unknown;

  constructor(message: string, status: number, details?: unknown) {
    super(message);
    this.status = status;
    this.details = details;
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data: unknown = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = data as { error?: string; details?: unknown };
    throw new ApiError(err.error ?? `Request failed (${res.status})`, res.status, err.details);
  }
  return data as T;
}

export const api = {
  me: () => request<Me>('GET', '/api/review/me'),
  allowedValues: () => request<{ values: string[] }>('GET', '/api/review/allowed-values'),
  queue: (kind: 'review' | 'privacy') =>
    request<{ items: QueueItem[]; asOf: string }>('GET', `/api/review/queue?kind=${kind}`),
  submit: (kind: 'REVIEW' | 'PRIVACY', decisions: DecisionDraft[]) =>
    request<{ submitted: number; requestId: string }>('POST', '/api/review/submissions', { kind, decisions }),
  scopes: () => request<{ scopes: ScopeRow[]; governedCatalogPrefix: string }>('GET', '/api/review/scopes'),
  saveScope: (scopeId: string, body: Record<string, unknown>) =>
    request<{ saved: string }>('PUT', `/api/review/scopes/${encodeURIComponent(scopeId)}`, body),
  mappings: () => request<{ mappings: MappingRow[] }>('GET', '/api/review/mappings'),
  saveMapping: (classTag: string, body: Record<string, unknown>) =>
    request<{ saved: string }>('PUT', `/api/review/mappings/${encodeURIComponent(classTag)}`, body),
  requestCorrection: (column: string, reason: string) =>
    request<{ scanRunId: string }>('POST', '/api/review/corrections', { column, reason }),
};

/** Human-readable error list from a 422 response (per-item problems) or any other error. */
export function describeError(err: unknown): string[] {
  if (err instanceof ApiError && err.details && typeof err.details === 'object' && !Array.isArray(err.details)) {
    return Object.entries(err.details as Record<string, string[]>).map(([k, v]) => `${k}: ${v.join('; ')}`);
  }
  if (err instanceof ApiError && Array.isArray(err.details)) {
    return [err.message, ...(err.details as { message?: string }[]).map((d) => d.message ?? '')].filter(Boolean);
  }
  return [err instanceof Error ? err.message : String(err)];
}

export function fmtDate(v: string | null | undefined): string {
  if (!v) return '';
  const d = new Date(v.includes('T') || v.endsWith('Z') ? v : `${v.replace(' ', 'T')}Z`);
  return Number.isNaN(d.getTime()) ? v : d.toLocaleString();
}

/** Parse a JSON array of emails from the server; tolerate legacy or malformed values. */
export function parseEmails(json: string | null | undefined): string[] {
  try {
    const v: unknown = JSON.parse(json ?? '[]');
    return Array.isArray(v) ? v.map(String) : [];
  } catch {
    return [];
  }
}

/** Split user input (commas, semicolons, spaces, or new lines) into lower-case emails. */
export function splitEmails(text: string): string[] {
  return [
    ...new Set(
      text
        .split(/[\s,;]+/)
        .map((e) => e.trim().toLowerCase())
        .filter(Boolean)
    ),
  ];
}
