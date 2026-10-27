import { getExecutionContext } from '@databricks/appkit';

/** Full SDK client; AppKit's client exposes only a subset of APIs. */
function sdk() {
  return getExecutionContext().client.toLegacyWorkspaceClient();
}

/**
 * Resolves scope reviewer values (one user email or one group name) to lower-case user names.
 * Mirrors phi_framework.directory.GroupDirectory so the App's pre-checks and ingestion's
 * authoritative validation agree: an existing workspace group wins; otherwise a value containing
 * "@" is a single user; anything else resolves to no one. Nested groups are expanded.
 */
const TTL_MS = 5 * 60_000;

type Entry = { at: number; users: Set<string> };
const cache = new Map<string, Entry>();
const userNames = new Map<string, string | null>();

async function userName(id: string): Promise<string | null> {
  if (!userNames.has(id)) {
    try {
      const u = await sdk().usersV2.get({ id });
      userNames.set(id, (u.userName ?? '').toLowerCase() || null);
    } catch {
      userNames.set(id, null); // deleted user or service principal
    }
  }
  return userNames.get(id) ?? null;
}

async function groupByName(name: string): Promise<{ id?: string } | undefined> {
  if (name.includes('"')) return undefined;
  for await (const g of sdk().groupsV2.list({ filter: `displayName eq "${name}"`, attributes: 'id,displayName' })) {
    if (g.displayName === name) return g;
  }
  return undefined;
}

async function expandGroup(rootId: string): Promise<Set<string>> {
  const client = sdk();
  const users = new Set<string>();
  const seen = new Set<string>();
  const stack = [rootId];
  while (stack.length) {
    const id = stack.pop() as string;
    if (seen.has(id)) continue;
    seen.add(id);
    const g = await client.groupsV2.get({ id });
    for (const m of g.members ?? []) {
      const ref = m.$ref ?? '';
      if (ref.startsWith('Groups/') && m.value) stack.push(m.value);
      else if (ref.startsWith('Users/') && m.value) {
        const n = await userName(m.value);
        if (n) users.add(n);
      }
    }
  }
  return users;
}

export async function principals(value: string | null | undefined): Promise<Set<string>> {
  const v = (value ?? '').trim();
  if (!v) return new Set();
  const hit = cache.get(v);
  if (hit && Date.now() - hit.at < TTL_MS) return hit.users;
  const group = await groupByName(v);
  let users: Set<string>;
  if (group?.id) users = await expandGroup(group.id);
  else if (v.includes('@')) users = new Set([v.toLowerCase()]);
  else users = new Set();
  cache.set(v, { at: Date.now(), users });
  return users;
}

export async function includes(user: string, value: string | null | undefined): Promise<boolean> {
  return (await principals(value)).has(user.toLowerCase());
}
