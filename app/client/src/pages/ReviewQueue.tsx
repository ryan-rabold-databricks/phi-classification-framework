import { useCallback, useEffect, useMemo, useState } from 'react';
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
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyTitle,
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
import { CheckCheck, RefreshCw } from 'lucide-react';
import { api, describeError, fmtDate, type Decision, type DecisionDraft, type QueueItem } from '../lib/api';

const DECISION_LABELS: Record<Decision, string> = {
  APPROVE_SUGGESTION: 'Approve suggestion',
  CORRECT_CLASSIFICATION: 'Correct classification',
  CONFIRM_NOT_PHI: 'Confirm not PHI',
  REQUEST_PRIVACY_REVIEW: 'Request Privacy review',
};

interface Props {
  kind: 'review' | 'privacy';
  batchFilter?: string;
}

type Drafts = Record<string, { decision?: Decision; corrected?: string; comment?: string }>;

function parseErrors(raw: string | null): string[] {
  if (!raw) return [];
  try {
    const v: unknown = JSON.parse(raw);
    return Array.isArray(v) ? v.map(String) : [String(v)];
  } catch {
    return [raw];
  }
}

export function ReviewQueue({ kind, batchFilter }: Props) {
  const [items, setItems] = useState<QueueItem[] | null>(null);
  const [asOf, setAsOf] = useState<string>('');
  const [allowed, setAllowed] = useState<string[]>([]);
  const [error, setError] = useState<string[] | null>(null);
  const [drafts, setDrafts] = useState<Drafts>({});
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [submitting, setSubmitting] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const privacy = kind === 'privacy';
  const decisions = (Object.keys(DECISION_LABELS) as Decision[]).filter(
    (d) => !(privacy && d === 'REQUEST_PRIVACY_REVIEW')
  );

  const load = useCallback(async () => {
    setError(null);
    try {
      const [q, av] = await Promise.all([api.queue(kind), api.allowedValues()]);
      setItems(q.items);
      setAsOf(q.asOf);
      setAllowed(av.values);
    } catch (err) {
      setError(describeError(err));
      setItems([]);
    }
  }, [kind]);

  useEffect(() => {
    void load();
  }, [load]);

  const visible = useMemo(
    () => (items ?? []).filter((i) => !batchFilter || i.review_batch_id === batchFilter),
    [items, batchFilter]
  );
  const actionable = visible.filter((i) => !i.awaiting_validation_since);
  const ready: DecisionDraft[] = actionable
    .filter((i) => drafts[i.review_item_id]?.decision)
    .map((i) => {
      const d = drafts[i.review_item_id];
      return {
        review_item_id: i.review_item_id,
        decision: d.decision as Decision,
        corrected_tag_value: d.decision === 'CORRECT_CLASSIFICATION' ? (d.corrected ?? null) : null,
        steward_comment: d.comment ?? null,
        suggested_tag_value_seen: i.suggested_tag_value,
      };
    });

  const setDraft = (id: string, patch: Drafts[string]) =>
    setDrafts((prev) => ({ ...prev, [id]: { ...prev[id], ...patch } }));

  const approveSelected = () => {
    for (const id of selected) {
      const it = actionable.find((i) => i.review_item_id === id);
      if (it?.suggested_tag_value) setDraft(id, { decision: 'APPROVE_SUGGESTION' });
    }
  };

  const submit = async () => {
    setSubmitting(true);
    setError(null);
    setNotice(null);
    try {
      const r = await api.submit(privacy ? 'PRIVACY' : 'REVIEW', ready);
      setNotice(
        `Submitted ${r.submitted} decision(s). Validation usually completes within a few minutes; ` +
          'rejected decisions return to this list with the reason.'
      );
      setDrafts({});
      setSelected(new Set());
      await load();
    } catch (err) {
      setError(describeError(err));
    } finally {
      setSubmitting(false);
    }
  };

  if (items === null) {
    return (
      <div className="space-y-3">
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  const dueSoonest = actionable.map((i) => i.due_at).sort()[0];
  const returned = actionable.filter((i) => i.status === 'VALIDATION_FAILED').length;

  return (
    <div className="space-y-4">
      <div className="grid gap-3 md:grid-cols-3">
        <Card>
          <CardHeader>
            <CardDescription>{privacy ? 'Awaiting a Privacy outcome' : 'Awaiting your decision'}</CardDescription>
            <CardTitle className="text-2xl">{actionable.length} columns</CardTitle>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader>
            <CardDescription>Earliest due date</CardDescription>
            <CardTitle className="text-2xl">{dueSoonest ? fmtDate(dueSoonest) : 'None'}</CardTitle>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader>
            <CardDescription>Returned for correction</CardDescription>
            <CardTitle className="text-2xl">{returned}</CardTitle>
          </CardHeader>
        </Card>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        {!privacy && (
          <Button variant="outline" onClick={approveSelected} disabled={selected.size === 0}>
            <CheckCheck className="h-4 w-4" /> Approve suggestions for selected ({selected.size})
          </Button>
        )}
        <Button onClick={() => void submit()} disabled={ready.length === 0 || submitting}>
          {submitting ? 'Submitting…' : `Submit ${ready.length} decision(s)`}
        </Button>
        <Button variant="ghost" onClick={() => void load()}>
          <RefreshCw className="h-4 w-4" /> Refresh
        </Button>
        <span className="text-sm text-muted-foreground ml-auto">
          {batchFilter ? `Batch ${batchFilter} · ` : ''}As of {fmtDate(asOf)} · Source: governance review tables
        </span>
      </div>

      {notice && (
        <Alert>
          <AlertTitle>Submitted</AlertTitle>
          <AlertDescription>{notice}</AlertDescription>
        </Alert>
      )}
      {error && (
        <Alert variant="destructive">
          <AlertTitle>Nothing was submitted</AlertTitle>
          <AlertDescription>
            <ul className="list-disc pl-4">
              {error.map((e) => (
                <li key={e}>{e}</li>
              ))}
            </ul>
          </AlertDescription>
        </Alert>
      )}

      {visible.length === 0 ? (
        <Empty>
          <EmptyHeader>
            <EmptyTitle>{privacy ? 'No items await a Privacy outcome' : 'Nothing awaits your review'}</EmptyTitle>
            <EmptyDescription>
              You will receive an email when the weekly scan finds columns in your scopes that need a decision.
            </EmptyDescription>
          </EmptyHeader>
        </Empty>
      ) : (
        <Card>
          <CardContent className="p-0">
            <Table>
              <TableHeader>
                <TableRow>
                  {!privacy && <TableHead className="w-8" />}
                  <TableHead>Column</TableHead>
                  <TableHead>Issue</TableHead>
                  <TableHead>Suggestion</TableHead>
                  <TableHead className="w-56">Decision</TableHead>
                  <TableHead className="w-72">Comment</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {visible.map((i) => {
                  const d = drafts[i.review_item_id] ?? {};
                  const waiting = !!i.awaiting_validation_since;
                  const errors = parseErrors(i.last_errors);
                  return (
                    <TableRow key={i.review_item_id}>
                      {!privacy && (
                        <TableCell>
                          <Checkbox
                            aria-label={`Select ${i.column_name}`}
                            disabled={waiting}
                            checked={selected.has(i.review_item_id)}
                            onCheckedChange={(v) =>
                              setSelected((prev) => {
                                const next = new Set(prev);
                                if (v) next.add(i.review_item_id);
                                else next.delete(i.review_item_id);
                                return next;
                              })
                            }
                          />
                        </TableCell>
                      )}
                      <TableCell className="align-top">
                        <div className="font-medium text-foreground">
                          {i.table_name}.{i.column_name}
                        </div>
                        <div className="text-xs text-muted-foreground">
                          {i.catalog_name}.{i.schema_name} · {i.full_data_type}
                        </div>
                        {i.column_comment && <div className="text-xs text-muted-foreground">{i.column_comment}</div>}
                        <div className="text-xs text-muted-foreground">Due {fmtDate(i.due_at)}</div>
                      </TableCell>
                      <TableCell className="align-top">
                        <Badge variant="outline">{i.issue_type}</Badge>
                        {i.current_tag_value && (
                          <div className="text-xs text-muted-foreground mt-1">Current tag: {i.current_tag_value}</div>
                        )}
                        {waiting && (
                          <Badge className="mt-1" variant="secondary">
                            Submitted, awaiting validation
                          </Badge>
                        )}
                        {errors.length > 0 && (
                          <div className="text-xs text-destructive mt-1">Returned: {errors.join('; ')}</div>
                        )}
                        {privacy && i.steward_comment && (
                          <div className="text-xs mt-1">
                            Question from {i.reviewed_by}: {i.steward_comment}
                          </div>
                        )}
                      </TableCell>
                      <TableCell className="align-top">
                        <div className="text-foreground">{i.suggested_tag_value ?? 'No suggestion'}</div>
                        {i.suggestion_reason && (
                          <div className="text-xs text-muted-foreground">{i.suggestion_reason}</div>
                        )}
                      </TableCell>
                      <TableCell className="align-top space-y-2">
                        <Select
                          disabled={waiting}
                          value={d.decision ?? ''}
                          onValueChange={(v) => setDraft(i.review_item_id, { decision: v as Decision })}
                        >
                          <SelectTrigger aria-label="Decision">
                            <SelectValue placeholder="Choose a decision" />
                          </SelectTrigger>
                          <SelectContent>
                            {decisions.map((k) => (
                              <SelectItem
                                key={k}
                                value={k}
                                disabled={k === 'APPROVE_SUGGESTION' && !i.suggested_tag_value}
                              >
                                {DECISION_LABELS[k]}
                              </SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                        {d.decision === 'CORRECT_CLASSIFICATION' && (
                          <Select
                            value={d.corrected ?? ''}
                            onValueChange={(v) => setDraft(i.review_item_id, { corrected: v })}
                          >
                            <SelectTrigger aria-label="Corrected value">
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
                        )}
                      </TableCell>
                      <TableCell className="align-top">
                        <Textarea
                          aria-label="Comment"
                          disabled={waiting}
                          placeholder={
                            d.decision === 'CONFIRM_NOT_PHI'
                              ? 'Required: why is this column not PHI?'
                              : d.decision === 'REQUEST_PRIVACY_REVIEW'
                                ? 'Required: the question for Privacy'
                                : 'Optional comment'
                          }
                          value={d.comment ?? ''}
                          onChange={(e) => setDraft(i.review_item_id, { comment: e.target.value })}
                        />
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      )}
      <p className="text-xs text-muted-foreground">
        Decisions are recorded under your signed-in identity and the time you submit. Column data is never shown; only
        metadata and Data Classification detections.
      </p>
    </div>
  );
}
