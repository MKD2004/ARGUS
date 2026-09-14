import { useState } from 'react'

import { decide } from '../api.ts'

interface Props {
  incidentId: string
  approval: { allowed: boolean; reason: string }
  decidedBy: string
  onDecidedByChange: (name: string) => void
}

// The human approval control (PIPELINE.md §11). "Approve" is only enabled when
// the backend says approval is allowed, with its reason shown either way (D-038).
// The backend checks again regardless; this only saves a refused click.
export function DecisionPanel({ incidentId, approval, decidedBy, onDecidedByChange }: Props) {
  const [submitting, setSubmitting] = useState<'approved' | 'rejected' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const name = decidedBy.trim()

  const submit = async (decision: 'approved' | 'rejected') => {
    setSubmitting(decision)
    setError(null)
    try {
      await decide(incidentId, decision, name)
      // The run resumes in the background; the event stream shows what happens next.
    } catch (err) {
      setError((err as Error).message)
      setSubmitting(null)
    }
  }

  return (
    <section className="panel decision" aria-labelledby="decision-title">
      <h2 id="decision-title">Your decision</h2>
      <p className={approval.allowed ? 'approval-ok' : 'approval-blocked'}>
        {approval.allowed ? 'Ready to approve: ' : 'Approval not available: '}
        {approval.reason}
      </p>
      <p className="muted">
        {approval.allowed
          ? 'Approve opens a pull request with this exact diff. Reject opens an issue with the diagnosis instead.'
          : 'Reject opens a GitHub issue with the diagnosis and evidence for manual follow-up.'}
      </p>

      <label className="field">
        <span>Your name</span>
        <input
          type="text"
          value={decidedBy}
          onChange={(event) => onDecidedByChange(event.target.value)}
          placeholder="Recorded with the decision"
          autoComplete="name"
        />
      </label>

      <div className="decision-buttons">
        <button
          type="button"
          className="button approve"
          onClick={() => submit('approved')}
          disabled={!approval.allowed || !name || submitting !== null}
        >
          {submitting === 'approved' ? 'Approving…' : 'Approve'}
        </button>
        <button
          type="button"
          className="button reject"
          onClick={() => submit('rejected')}
          disabled={!name || submitting !== null}
        >
          {submitting === 'rejected' ? 'Rejecting…' : 'Reject'}
        </button>
      </div>
      {error && <p className="error" role="alert">{error}</p>}
    </section>
  )
}
