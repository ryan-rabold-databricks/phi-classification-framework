import type { Request } from 'express';
import { getExecutionContext } from '@databricks/appkit';
import { userEmail } from './identity';
import { settings } from './settings';

/**
 * Who the signed-in user is and which groups they belong to.
 *
 * Group membership comes from SCIM /Me called with the user's own token (forwarded by the
 * Apps proxy with the default iam.current-user:read user scope). It lists direct and inherited
 * groups, so nested groups count. The App service principal never reads group member lists,
 * which the workspace restricts to admins.
 *
 * Scope reviewers are email lists, matched against the user's email; these groups are used only
 * for the Data Governance group (Privacy outcomes, reference-data edits, corrections).
 */
export interface Principal {
  email: string;
  groups: Set<string>;
}

const TTL_MS = 5 * 60_000;
const cache = new Map<string, { at: number; groups: Set<string> }>();

interface ScimMe {
  userName?: string;
  groups?: { display?: string }[];
}

async function groupsFor(token: string): Promise<Set<string>> {
  const hit = cache.get(token);
  if (hit && Date.now() - hit.at < TTL_MS) return hit.groups;
  const host = (getExecutionContext().client.config.host ?? '').replace(/\/$/, '');
  const res = await fetch(`${host}/api/2.0/preview/scim/v2/Me?attributes=userName,groups`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!res.ok) throw new Error(`Could not read your group memberships (${res.status})`);
  const me = (await res.json()) as ScimMe;
  const groups = new Set((me.groups ?? []).map((g) => g.display ?? '').filter(Boolean));
  cache.set(token, { at: Date.now(), groups });
  return groups;
}

export async function principal(req: Request): Promise<Principal | undefined> {
  const email = userEmail(req);
  if (!email) return undefined;
  const token = req.header('x-forwarded-access-token') ?? '';
  if (token) return { email, groups: await groupsFor(token) };
  // Local development without a forwarded token: membership comes from PHI_DEV_USER_GROUPS.
  const s = settings();
  if (s.NODE_ENV === 'development') {
    return {
      email,
      groups: new Set(
        s.PHI_DEV_USER_GROUPS.split(',')
          .map((g) => g.trim())
          .filter(Boolean)
      ),
    };
  }
  throw new Error('The App did not receive your access token; check that user authorization is enabled');
}

export function includes(p: Principal, value: string | null | undefined): boolean {
  const v = (value ?? '').trim();
  if (!v) return false;
  return p.groups.has(v) || v.toLowerCase() === p.email;
}
