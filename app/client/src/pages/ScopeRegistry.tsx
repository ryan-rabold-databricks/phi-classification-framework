import { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  AlertDescription,
  AlertTitle,
  Badge,
  Button,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  Checkbox,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyTitle,
  Input,
  Label,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Skeleton,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
  Textarea,
} from '@databricks/appkit-ui/react';
import { Plus } from 'lucide-react';
import { api, describeError, fmtDate, parseEmails, splitEmails, type ScopeRow } from '../lib/api';

interface Form {
  scope_id: string;
  catalog_name: string;
  schema_name: string;
  risk_tier: string;
  steward_emails: string;
  governance_owner_emails: string;
  scan_enabled: boolean;
  effective_date: string;
  expiration_date: string;
}

const EMPTY: Form = {
  scope_id: '',
  catalog_name: '',
  schema_name: '*',
  risk_tier: 'STANDARD',
  steward_emails: '',
  governance_owner_emails: '',
  scan_enabled: true,
  effective_date: new Date().toISOString().slice(0, 10),
  expiration_date: '',
};

function toForm(r: ScopeRow): Form {
  return {
    scope_id: r.scope_id,
    catalog_name: r.catalog_name,
    schema_name: r.schema_name,
    risk_tier: r.risk_tier,
    steward_emails: parseEmails(r.steward_emails).join('\n'),
    governance_owner_emails: parseEmails(r.governance_owner_emails).join('\n'),
    scan_enabled: r.scan_enabled === 'true',
    effective_date: r.effective_date,
    expiration_date: r.expiration_date ?? '',
  };
}

export function ScopeRegistry({ canEdit }: { canEdit: boolean }) {
  const [rows, setRows] = useState<ScopeRow[] | null>(null);
  const [prefix, setPrefix] = useState('');
  const [error, setError] = useState<string[] | null>(null);
  const [form, setForm] = useState<Form | null>(null);
  const [isNew, setIsNew] = useState(false);
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string[] | null>(null);
  const [column, setColumn] = useState('');
  const [reason, setReason] = useState('');
  const [correction, setCorrection] = useState<{ ok?: string; err?: string[] }>({});

  const load = useCallback(async () => {
    setError(null);
    try {
      const r = await api.scopes();
      setRows(r.scopes);
      setPrefix(r.governedCatalogPrefix);
    } catch (err) {
      setError(describeError(err));
      setRows([]);
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);

  const save = async () => {
    if (!form) return;
    setSaving(true);
    setFormError(null);
    try {
      const { scope_id, steward_emails, governance_owner_emails, ...rest } = form;
      await api.saveScope(scope_id, {
        ...rest,
        steward_emails: splitEmails(steward_emails),
        governance_owner_emails: splitEmails(governance_owner_emails),
        expiration_date: rest.expiration_date || null,
      });
      setForm(null);
      await load();
    } catch (err) {
      setFormError(describeError(err));
    } finally {
      setSaving(false);
    }
  };

  const requestCorrection = async () => {
    setCorrection({});
    try {
      const r = await api.requestCorrection(column.trim(), reason.trim());
      setCorrection({
        ok: `Scan run ${r.scanRunId} started. The new item appears in the reviewers' queue when it finishes.`,
      });
      setColumn('');
      setReason('');
    } catch (err) {
      setCorrection({ err: describeError(err) });
    }
  };

  if (rows === null) return <Skeleton className="h-64 w-full" />;

  return (
    <div className="space-y-4">
      {error && (
        <Alert variant="destructive">
          <AlertTitle>Could not load the scope registry</AlertTitle>
          <AlertDescription>{error.join('; ')}</AlertDescription>
        </Alert>
      )}
      <div className="flex items-center gap-2">
        <p className="text-sm text-muted-foreground">
          Each scope routes review items to its steward emails and copies its governance owner emails. Every change is
          recorded in the audit trail.
        </p>
        {canEdit && (
          <Button
            className="ml-auto"
            onClick={() => {
              setIsNew(true);
              setForm({ ...EMPTY, catalog_name: prefix });
              setFormError(null);
            }}
          >
            <Plus className="h-4 w-4" /> Add scope
          </Button>
        )}
      </div>

      {rows.length === 0 ? (
        <Empty>
          <EmptyHeader>
            <EmptyTitle>No scopes registered</EmptyTitle>
            <EmptyDescription>Add a scope so the weekly scan covers a governed catalog or schema.</EmptyDescription>
          </EmptyHeader>
        </Empty>
      ) : (
        <Card>
          <CardContent className="p-0">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Scope</TableHead>
                  <TableHead>Catalog.schema</TableHead>
                  <TableHead>Risk tier</TableHead>
                  <TableHead>Steward emails</TableHead>
                  <TableHead>Governance owner emails</TableHead>
                  <TableHead>Active</TableHead>
                  <TableHead>Last changed</TableHead>
                  {canEdit && <TableHead />}
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((r) => (
                  <TableRow key={r.scope_id}>
                    <TableCell className="font-medium">{r.scope_id}</TableCell>
                    <TableCell>
                      {r.catalog_name}.{r.schema_name}
                    </TableCell>
                    <TableCell>
                      <Badge variant={r.risk_tier === 'HIGH' ? 'destructive' : 'secondary'}>{r.risk_tier}</Badge>
                    </TableCell>
                    <TableCell className="text-xs">{parseEmails(r.steward_emails).join(', ')}</TableCell>
                    <TableCell className="text-xs">{parseEmails(r.governance_owner_emails).join(', ')}</TableCell>
                    <TableCell>
                      {r.scan_enabled === 'true' ? 'Scanned' : 'Paused'}
                      <div className="text-xs text-muted-foreground">
                        {r.effective_date} to {r.expiration_date ?? 'open'}
                      </div>
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {fmtDate(r.audit_updated_at)}
                      <div>{r.audit_updated_by}</div>
                    </TableCell>
                    {canEdit && (
                      <TableCell>
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => {
                            setIsNew(false);
                            setForm(toForm(r));
                            setFormError(null);
                          }}
                        >
                          Edit
                        </Button>
                      </TableCell>
                    )}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      )}

      {canEdit && (
        <Card>
          <CardHeader>
            <CardTitle>Request a correction</CardTitle>
            <CardDescription>
              Reopens a resolved column for a new decision. The original decision and its evidence remain unchanged.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="grid gap-3 md:grid-cols-2">
              <div className="space-y-1">
                <Label htmlFor="corr-col">Column (catalog.schema.table.column)</Label>
                <Input id="corr-col" value={column} onChange={(e) => setColumn(e.target.value)} />
              </div>
              <div className="space-y-1">
                <Label htmlFor="corr-reason">Reason</Label>
                <Textarea id="corr-reason" value={reason} onChange={(e) => setReason(e.target.value)} />
              </div>
            </div>
            <Button
              variant="outline"
              disabled={!column.trim() || reason.trim().length < 5}
              onClick={() => void requestCorrection()}
            >
              Request correction
            </Button>
            {correction.ok && <p className="text-sm text-foreground">{correction.ok}</p>}
            {correction.err && <p className="text-sm text-destructive">{correction.err.join('; ')}</p>}
          </CardContent>
        </Card>
      )}

      <Dialog open={form !== null} onOpenChange={(open) => !open && setForm(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{isNew ? 'Add scope' : `Edit ${form?.scope_id}`}</DialogTitle>
            <DialogDescription>
              Catalogs must start with {prefix}. Use * for every schema in the catalog; an explicit schema overrides it.
            </DialogDescription>
          </DialogHeader>
          {form && (
            <div className="grid gap-3">
              {isNew && (
                <div className="space-y-1">
                  <Label htmlFor="scope-id">Scope ID</Label>
                  <Input
                    id="scope-id"
                    placeholder="SCOPE-CLINICAL-PROD"
                    value={form.scope_id}
                    onChange={(e) => setForm({ ...form, scope_id: e.target.value.toUpperCase() })}
                  />
                </div>
              )}
              <div className="grid grid-cols-2 gap-3">
                <div className="space-y-1">
                  <Label htmlFor="scope-cat">Catalog</Label>
                  <Input
                    id="scope-cat"
                    value={form.catalog_name}
                    onChange={(e) => setForm({ ...form, catalog_name: e.target.value })}
                  />
                </div>
                <div className="space-y-1">
                  <Label htmlFor="scope-sch">Schema</Label>
                  <Input
                    id="scope-sch"
                    value={form.schema_name}
                    onChange={(e) => setForm({ ...form, schema_name: e.target.value })}
                  />
                </div>
              </div>
              <div className="space-y-1">
                <Label htmlFor="scope-stewards">Steward emails (one per line)</Label>
                <Textarea
                  id="scope-stewards"
                  value={form.steward_emails}
                  onChange={(e) => setForm({ ...form, steward_emails: e.target.value })}
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="scope-owner">Governance owner emails (one per line)</Label>
                <Textarea
                  id="scope-owner"
                  value={form.governance_owner_emails}
                  onChange={(e) => setForm({ ...form, governance_owner_emails: e.target.value })}
                />
              </div>
              <div className="grid grid-cols-3 gap-3">
                <div className="space-y-1">
                  <Label>Risk tier</Label>
                  <Select value={form.risk_tier} onValueChange={(v) => setForm({ ...form, risk_tier: v })}>
                    <SelectTrigger aria-label="Risk tier">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="HIGH">HIGH (3 months)</SelectItem>
                      <SelectItem value="STANDARD">STANDARD (6 months)</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
                <div className="space-y-1">
                  <Label htmlFor="scope-eff">Effective</Label>
                  <Input
                    id="scope-eff"
                    type="date"
                    value={form.effective_date}
                    onChange={(e) => setForm({ ...form, effective_date: e.target.value })}
                  />
                </div>
                <div className="space-y-1">
                  <Label htmlFor="scope-exp">Expires</Label>
                  <Input
                    id="scope-exp"
                    type="date"
                    value={form.expiration_date}
                    onChange={(e) => setForm({ ...form, expiration_date: e.target.value })}
                  />
                </div>
              </div>
              <div className="flex items-center gap-2">
                <Checkbox
                  id="scope-enabled"
                  checked={form.scan_enabled}
                  onCheckedChange={(v) => setForm({ ...form, scan_enabled: v === true })}
                />
                <Label htmlFor="scope-enabled">Include in the weekly scan</Label>
              </div>
              {formError && <p className="text-sm text-destructive">{formError.join('; ')}</p>}
            </div>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => setForm(null)}>
              Cancel
            </Button>
            <Button disabled={saving || !form?.scope_id} onClick={() => void save()}>
              {saving ? 'Saving…' : 'Save'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
