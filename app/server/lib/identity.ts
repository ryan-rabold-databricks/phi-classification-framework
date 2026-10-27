import type { Request } from 'express';
import { settings } from './settings';

/**
 * Email of the signed-in user. The Databricks Apps proxy authenticates every request and sets
 * X-Forwarded-Email; clients cannot reach the app without passing through it.
 * PHI_DEV_USER_EMAIL is honored only in local development.
 */
export function userEmail(req: Request): string | undefined {
  const forwarded = req.header('x-forwarded-email') ?? '';
  if (forwarded.trim()) return forwarded.trim().toLowerCase();
  const s = settings();
  if (s.NODE_ENV === 'development' && s.PHI_DEV_USER_EMAIL) return s.PHI_DEV_USER_EMAIL.trim().toLowerCase();
  return undefined;
}
