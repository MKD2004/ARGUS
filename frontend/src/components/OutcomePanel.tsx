import type { IncidentState } from '../types.ts'

// Where the incident ended up: PR or issue link, and the postmortem. The
// postmortem is shown as its Markdown source, not rendered HTML (D-047).
export function OutcomePanel({ state }: { state: IncidentState }) {
  const link = state.github_pr_url ?? state.github_issue_url
  if (!state.human_decision && !state.postmortem) return null

  return (
    <section className="panel outcome" aria-labelledby="outcome-title">
      <h2 id="outcome-title">Outcome</h2>
      <dl className="facts">
        {state.human_decision && (
          <>
            <dt>Decision</dt>
            <dd>
              {state.human_decision} by {state.human_decision_by}
              {state.human_decision_at ? ` at ${new Date(state.human_decision_at).toLocaleTimeString()}` : ''}
            </dd>
          </>
        )}
        {state.human_decision && (
          <>
            <dt>{state.human_decision === 'approved' ? 'Pull request' : 'Issue'}</dt>
            <dd>
              {link ? (
                <a href={link} target="_blank" rel="noreferrer">{link}</a>
              ) : state.status === 'resolved' ? (
                'None recorded (GitHub not configured, or creation failed)'
              ) : (
                'Opening…'
              )}
            </dd>
          </>
        )}
        {state.slack_notifications_sent.length > 0 && (
          <>
            <dt>Slack</dt>
            <dd>Summary posted</dd>
          </>
        )}
      </dl>

      {state.postmortem && (
        <details className="postmortem" open>
          <summary>Postmortem</summary>
          <div className="code-block">
            <pre>{state.postmortem}</pre>
          </div>
        </details>
      )}
    </section>
  )
}
