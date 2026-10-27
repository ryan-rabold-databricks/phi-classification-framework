# PHI Classification Review App

AppKit (TypeScript/React) Databricks App where Data Stewards and governance owners review PHI
classification items, Data Governance records Privacy outcomes, and Data Governance maintains the
scope registry and suggestion mapping. See the repository README ("The review App") for the
architecture, permissions, and deployment; the App is deployed with the bundle from
`resources/review_app.yml`.

## Layout

| Path                      | Purpose                                                                                        |
| ------------------------- | ---------------------------------------------------------------------------------------------- |
| `server/server.ts`        | AppKit server entry; registers the review API routes                                           |
| `server/routes.ts`        | `/api/review/*`: identity and roles, queues, submissions, scope registry, mapping, corrections |
| `server/lib/directory.ts` | Signed-in user's groups from SCIM `/Me` (user token); reviewer-value matching                  |
| `server/lib/rules.ts`     | Decision pre-checks shown to reviewers (ingestion re-validates every submission)               |
| `server/lib/sql.ts`       | Parameterized SQL on the App's warehouse as the App service principal                          |
| `server/lib/settings.ts`  | Environment configuration (set by the bundle's app `config`)                                   |
| `client/src/`             | React UI: review queue, Privacy queue, scope registry, suggestion mapping                      |

## Local development

```bash
cp .env.example .env          # fill in workspace, warehouse, governance schema, scan job id
npm install
npm run dev                   # http://localhost:8000
```

Locally there is no Apps proxy, so the server uses `PHI_DEV_USER_EMAIL` and `PHI_DEV_USER_GROUPS`
for identity and role checks (honored only when `NODE_ENV=development`).

## Checks

```bash
npm run typecheck
npm run lint
npx vitest run
```
