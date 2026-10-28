-- Monitoring and metrics queries. Replace <gov> with <governance_catalog>.<governance_schema>
-- and <tag_key> with the governed tag key.

-- 1. Classification completeness per scope (primary coverage metric):
--    (directly tagged columns + active non-PHI attestations) / in-scope columns.
--    Uses the most recent scan's snapshot of in-scope columns.
SELECT s.scope_id,
       count(*)                                                        AS in_scope_columns,
       count_if(s.current_tag_value IS NOT NULL)                       AS tagged,
       count_if(a.attestation_id IS NOT NULL AND s.current_tag_value IS NULL) AS attested_not_phi,
       round((count_if(s.current_tag_value IS NOT NULL)
              + count_if(a.attestation_id IS NOT NULL AND s.current_tag_value IS NULL)) / count(*), 4)
                                                                       AS classification_completeness
FROM <gov>.scan_findings_stage s
LEFT JOIN <gov>.classification_attestation a
  ON a.catalog_name = s.catalog_name AND a.schema_name = s.schema_name
 AND a.table_name = s.table_name AND a.column_name = s.column_name
 AND a.superseded_at IS NULL AND a.expires_at > current_timestamp()
GROUP BY s.scope_id
ORDER BY classification_completeness;

-- 2. Open items by steward and age (drives reminders at 5 and escalation at 10 business days).
--    assigned_stewards is the scope's steward_emails list; each steward gets one row per batch.
SELECT steward, b.review_batch_id, b.due_at, i.status, count(*) AS items,
       datediff(current_date(), to_date(b.created_at)) AS age_days,
       current_timestamp() > b.due_at AS overdue
FROM <gov>.classification_review_item i
JOIN <gov>.classification_review_batch b USING (review_batch_id)
LATERAL VIEW explode(i.assigned_stewards) s AS steward
WHERE i.status NOT IN ('TAG_VERIFIED', 'ATTESTED_NOT_PHI', 'CLOSED_NO_ACTION')
GROUP BY ALL
ORDER BY overdue DESC, age_days DESC;

-- 3. Steward review time (median / p90 hours from batch creation to decision).
SELECT percentile_approx(timestampdiff(HOUR, b.created_at, i.reviewed_at), 0.5) AS median_hours,
       percentile_approx(timestampdiff(HOUR, b.created_at, i.reviewed_at), 0.9) AS p90_hours
FROM <gov>.classification_review_item i
JOIN <gov>.classification_review_batch b USING (review_batch_id)
WHERE i.reviewed_at IS NOT NULL;

-- 4. Application failures by reason.
SELECT regexp_extract(application_error, '^([A-Za-z]+Error)', 1) AS error_class, count(*) AS items
FROM <gov>.classification_review_item
WHERE status = 'APPLICATION_FAILED'
GROUP BY ALL;

-- 5. Changes submitted for already-accepted items (review for possible corrections).
SELECT e.event_timestamp, e.review_batch_id, e.review_item_id, e.business_actor,
       e.event_details:submitted AS submitted, e.event_details:recorded AS recorded
FROM <gov>.classification_review_event e
WHERE e.event_type = 'CHANGE_IGNORED_ALREADY_RESOLVED'
ORDER BY e.event_timestamp DESC;

-- 6. Recertifications due in the next 30 days.
SELECT 'ATTESTATION' AS kind, catalog_name, schema_name, table_name, column_name, expires_at AS due
FROM <gov>.classification_attestation
WHERE superseded_at IS NULL AND expires_at < current_timestamp() + INTERVAL 30 DAYS;

-- 7. Batches closed within SLA.
SELECT count_if(closed_at <= due_at) / count(*) AS pct_closed_within_sla
FROM <gov>.classification_review_batch
WHERE status = 'CLOSED' AND last_error IS NULL;

-- 8. Full evidence trail for one column.
SELECT e.event_timestamp, e.event_type, e.business_actor, e.event_details, e.audit_created_process
FROM <gov>.classification_review_event e
JOIN <gov>.classification_review_item i USING (review_item_id)
WHERE i.catalog_name = '<catalog>' AND i.schema_name = '<schema>'
  AND i.table_name = '<table>' AND i.column_name = '<column>'
ORDER BY e.event_timestamp;
