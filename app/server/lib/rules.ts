/**
 * Decision rules checked in the App before a submission is written, so reviewers get immediate
 * feedback. Ingestion (phi_framework.validation) re-checks every rule and is authoritative.
 */
export const DECISIONS = ['APPROVE_SUGGESTION', 'CORRECT_CLASSIFICATION', 'CONFIRM_NOT_PHI', 'REQUEST_PRIVACY_REVIEW'] as const;
export type Decision = (typeof DECISIONS)[number];

export const REVIEWABLE_STATUSES = ['PENDING_STEWARD_REVIEW', 'VALIDATION_FAILED'];
export const PRIVACY_STATUS = 'NEEDS_PRIVACY_REVIEW';

export type ActingAs = 'STEWARD' | 'GOVERNANCE_OWNER' | 'DATA_GOVERNANCE';

export interface ItemForCheck {
  status: string;
  suggested_tag_value: string | null;
}

export interface DecisionInput {
  decision: Decision;
  corrected_tag_value?: string | null;
  steward_comment?: string | null;
  suggested_tag_value_seen?: string | null;
}

function clean(v: string | null | undefined): string | null {
  const t = (v ?? '').trim();
  return t ? t : null;
}

export function checkDecision(item: ItemForCheck, d: DecisionInput, privacy: boolean, allowed: Set<string>): string[] {
  const errors: string[] = [];
  const corrected = clean(d.corrected_tag_value);
  const comment = clean(d.steward_comment);
  if (privacy && item.status !== PRIVACY_STATUS) errors.push('Item is not awaiting a Privacy outcome');
  if (!privacy && !REVIEWABLE_STATUSES.includes(item.status)) errors.push(`Item is ${item.status} and does not accept a decision`);
  switch (d.decision) {
    case 'APPROVE_SUGGESTION':
      if (!item.suggested_tag_value || !allowed.has(item.suggested_tag_value)) {
        errors.push('There is no allowed suggested value to approve');
      } else if (clean(d.suggested_tag_value_seen) !== item.suggested_tag_value) {
        errors.push('The suggested value changed after you opened the item; reload and review it again');
      }
      if (corrected) errors.push('Leave the corrected value empty when approving the suggestion');
      break;
    case 'CORRECT_CLASSIFICATION':
      if (!corrected || !allowed.has(corrected)) errors.push('Choose an allowed value for the corrected classification');
      break;
    case 'CONFIRM_NOT_PHI':
      if (!comment) errors.push('Explain why the column is not PHI');
      if (corrected) errors.push('Leave the corrected value empty when confirming not PHI');
      break;
    case 'REQUEST_PRIVACY_REVIEW':
      if (privacy) errors.push('An item already in Privacy review needs a final outcome');
      if (!comment) errors.push('Describe the question for Privacy');
      break;
  }
  return errors;
}
