import { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  AlertDescription,
  AlertTitle,
  Badge,
  Button,
  Card,
  CardContent,
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
import { api, describeError, fmtDate, type MappingRow } from '../lib/api';

interface Form {
  class_tag: string;
  phi_tag_value: string;
  is_active: boolean;
  notes: string;
}

export function SuggestionMapping({ canEdit }: { canEdit: boolean }) {
  const [rows, setRows] = useState<MappingRow[] | null>(null);
  const [allowed, setAllowed] = useState<string[]>([]);
  const [error, setError] = useState<string[] | null>(null);
  const [form, setForm] = useState<Form | null>(null);
  const [isNew, setIsNew] = useState(false);
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string[] | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [m, av] = await Promise.all([api.mappings(), api.allowedValues()]);
      setRows(m.mappings);
      setAllowed(av.values);
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
      await api.saveMapping(form.class_tag.trim(), {
        phi_tag_value: form.phi_tag_value,
        is_active: form.is_active,
        notes: form.notes || null,
      });
      setForm(null);
      await load();
    } catch (err) {
      setFormError(describeError(err));
    } finally {
      setSaving(false);
    }
  };

  if (rows === null) return <Skeleton className="h-64 w-full" />;

  return (
    <div className="space-y-4">
      {error && (
        <Alert variant="destructive">
          <AlertTitle>Could not load the suggestion mapping</AlertTitle>
          <AlertDescription>{error.join('; ')}</AlertDescription>
        </Alert>
      )}
      <div className="flex items-center gap-2">
        <p className="text-sm text-muted-foreground">
          Maps each Data Classification class to the value suggested to reviewers. A suggestion is never applied without
          a reviewer&apos;s decision. Deactivate a mapping instead of deleting it.
        </p>
        {canEdit && (
          <Button
            className="ml-auto"
            onClick={() => {
              setIsNew(true);
              setForm({ class_tag: 'class.', phi_tag_value: '', is_active: true, notes: '' });
              setFormError(null);
            }}
          >
            <Plus className="h-4 w-4" /> Add mapping
          </Button>
        )}
      </div>
      {rows.length === 0 ? (
        <Empty>
          <EmptyHeader>
            <EmptyTitle>No mappings</EmptyTitle>
            <EmptyDescription>Without mappings, reviewers classify every column directly.</EmptyDescription>
          </EmptyHeader>
        </Empty>
      ) : (
        <Card>
          <CardContent className="p-0">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Data Classification class</TableHead>
                  <TableHead>Suggested value</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Notes</TableHead>
                  <TableHead>Last changed</TableHead>
                  {canEdit && <TableHead />}
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((r) => (
                  <TableRow key={r.class_tag}>
                    <TableCell className="font-medium">{r.class_tag}</TableCell>
                    <TableCell>{r.phi_tag_value}</TableCell>
                    <TableCell>
                      <Badge variant={r.is_active === 'true' ? 'secondary' : 'outline'}>
                        {r.is_active === 'true' ? 'Active' : 'Inactive'}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground">{r.notes}</TableCell>
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
                            setForm({
                              class_tag: r.class_tag,
                              phi_tag_value: r.phi_tag_value,
                              is_active: r.is_active === 'true',
                              notes: r.notes ?? '',
                            });
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
      <Dialog open={form !== null} onOpenChange={(open) => !open && setForm(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{isNew ? 'Add mapping' : `Edit ${form?.class_tag}`}</DialogTitle>
            <DialogDescription>The suggested value must be an allowed value of the governed tag.</DialogDescription>
          </DialogHeader>
          {form && (
            <div className="grid gap-3">
              {isNew && (
                <div className="space-y-1">
                  <Label htmlFor="map-class">Data Classification class</Label>
                  <Input
                    id="map-class"
                    value={form.class_tag}
                    onChange={(e) => setForm({ ...form, class_tag: e.target.value })}
                  />
                </div>
              )}
              <div className="space-y-1">
                <Label>Suggested value</Label>
                <Select value={form.phi_tag_value} onValueChange={(v) => setForm({ ...form, phi_tag_value: v })}>
                  <SelectTrigger aria-label="Suggested value">
                    <SelectValue placeholder="Allowed value" />
                  </SelectTrigger>
                  <SelectContent>
                    {allowed.map((v) => (
                      <SelectItem key={v} value={v}>
                        {v}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="flex items-center gap-2">
                <Checkbox
                  id="map-active"
                  checked={form.is_active}
                  onCheckedChange={(v) => setForm({ ...form, is_active: v === true })}
                />
                <Label htmlFor="map-active">Active</Label>
              </div>
              <div className="space-y-1">
                <Label htmlFor="map-notes">Notes</Label>
                <Textarea
                  id="map-notes"
                  value={form.notes}
                  onChange={(e) => setForm({ ...form, notes: e.target.value })}
                />
              </div>
              {formError && <p className="text-sm text-destructive">{formError.join('; ')}</p>}
            </div>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => setForm(null)}>
              Cancel
            </Button>
            <Button disabled={saving || !form?.phi_tag_value} onClick={() => void save()}>
              {saving ? 'Saving…' : 'Save'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
