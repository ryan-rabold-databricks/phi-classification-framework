import { describe, expect, test } from 'vitest';
import { checkDecision, selectGrant } from './rules';

const allowed = new Set(['name', 'mrn']);
const pending = { status: 'PENDING_STEWARD_REVIEW', suggested_tag_value: 'name' };

describe('checkDecision', () => {
  test('approves the suggestion the reviewer saw', () => {
    expect(
      checkDecision(pending, { decision: 'APPROVE_SUGGESTION', suggested_tag_value_seen: 'name' }, false, allowed)
    ).toEqual([]);
  });

  test('rejects approval when the suggestion changed after it was shown', () => {
    const errors = checkDecision(
      pending,
      { decision: 'APPROVE_SUGGESTION', suggested_tag_value_seen: 'mrn' },
      false,
      allowed
    );
    expect(errors.join()).toMatch(/changed/);
  });

  test('requires an allowed corrected value and comments where the design requires them', () => {
    expect(
      checkDecision(pending, { decision: 'CORRECT_CLASSIFICATION', corrected_tag_value: 'bogus' }, false, allowed)
    ).toHaveLength(1);
    expect(
      checkDecision(pending, { decision: 'CORRECT_CLASSIFICATION', corrected_tag_value: 'mrn' }, false, allowed)
    ).toEqual([]);
    expect(checkDecision(pending, { decision: 'CONFIRM_NOT_PHI', steward_comment: '  ' }, false, allowed)).toHaveLength(
      1
    );
    expect(checkDecision(pending, { decision: 'REQUEST_PRIVACY_REVIEW' }, false, allowed)).toHaveLength(1);
  });

  test('enforces queue status', () => {
    const planned = { status: 'PLANNED', suggested_tag_value: 'name' };
    expect(
      checkDecision(planned, { decision: 'CONFIRM_NOT_PHI', steward_comment: 'code' }, false, allowed)
    ).toHaveLength(1);
    const privacy = { status: 'NEEDS_PRIVACY_REVIEW', suggested_tag_value: null };
    expect(checkDecision(privacy, { decision: 'CONFIRM_NOT_PHI', steward_comment: 'not PHI' }, true, allowed)).toEqual(
      []
    );
    expect(
      checkDecision(privacy, { decision: 'REQUEST_PRIVACY_REVIEW', steward_comment: 'again' }, true, allowed)
    ).toHaveLength(1);
    expect(checkDecision(pending, { decision: 'CONFIRM_NOT_PHI', steward_comment: 'x' }, true, allowed)).toHaveLength(
      1
    );
  });
});

describe('selectGrant', () => {
  const me = 'reviewer@example.com';
  const scope = (roles: ('STEWARD' | 'GOVERNANCE_OWNER')[]) => ({ roles });

  test('review decisions prefer the steward role, then the governance owner', () => {
    expect(selectGrant(false, true, 'gov', me, scope(['STEWARD', 'GOVERNANCE_OWNER']))).toEqual({
      actingAs: 'STEWARD',
      via: me,
    });
    expect(selectGrant(false, false, 'gov', me, scope(['GOVERNANCE_OWNER']))).toEqual({
      actingAs: 'GOVERNANCE_OWNER',
      via: me,
    });
  });

  test('Data Governance membership alone does not authorize review decisions', () => {
    expect(selectGrant(false, true, 'gov', me, scope([]))).toBeUndefined();
    expect(selectGrant(false, false, 'gov', me, undefined)).toBeUndefined();
  });

  test('Privacy outcomes prefer Data Governance, then the governance owner; stewards cannot record them', () => {
    expect(selectGrant(true, true, 'gov', me, scope(['STEWARD']))).toEqual({ actingAs: 'DATA_GOVERNANCE', via: 'gov' });
    expect(selectGrant(true, false, 'gov', me, scope(['GOVERNANCE_OWNER']))).toEqual({
      actingAs: 'GOVERNANCE_OWNER',
      via: me,
    });
    expect(selectGrant(true, false, 'gov', me, scope(['STEWARD']))).toBeUndefined();
  });
});
