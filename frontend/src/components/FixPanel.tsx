import { diffLines } from '../incidentModel.ts'
import type { IncidentState } from '../types.ts'

// Fix strategies, every patch attempt, and the latest diff with its test result.
export function FixPanel({ state }: { state: IncidentState }) {
  const latest = state.patches[state.patches.length - 1]
  if (!state.candidate_fix_strategies.length && !latest) return null

  return (
    <section className="panel fix" aria-labelledby="fix-title">
      <h2 id="fix-title">Fix</h2>

      {state.candidate_fix_strategies.length > 0 && (
        <ol className="strategies">
          {state.candidate_fix_strategies.map((strategy) => (
            <li key={strategy.id} className={strategy.id === state.chosen_fix_strategy?.id ? 'chosen' : ''}>
              <span className="strategy">{strategy.description}</span>
              <span className="reason">Tradeoffs: {strategy.tradeoffs}</span>
            </li>
          ))}
        </ol>
      )}

      {state.patches.length > 0 && (
        <>
          <h3>
            Patch attempts <span className="count">{state.patches.length}/{state.max_patch_retries}</span>
          </h3>
          <ul className="attempts">
            {state.patches.map((patch) => (
              <li key={patch.id} className={`attempt attempt-${patch.test_result}`}>
                Attempt {patch.attempt_number}: <strong>{patch.test_result}</strong>
                {patch.failure_traceback && (
                  <span className="reason">{patch.failure_traceback.trim().split('\n')[0]}</span>
                )}
              </li>
            ))}
          </ul>
        </>
      )}

      {latest && latest.diff && (
        <div className="code-block" role="region" aria-label={`Diff for attempt ${latest.attempt_number}`}>
          <pre className="diff">
            {diffLines(latest.diff).map((line, index) => (
              <span key={index} className={`diff-${line.kind}`}>
                {line.text}
                {'\n'}
              </span>
            ))}
          </pre>
        </div>
      )}

      {latest?.failure_traceback && (
        <details className="traceback">
          <summary>Why attempt {latest.attempt_number} failed</summary>
          <div className="code-block">
            <pre>{latest.failure_traceback}</pre>
          </div>
        </details>
      )}
    </section>
  )
}
