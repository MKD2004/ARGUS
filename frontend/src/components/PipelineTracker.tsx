import type { Stage, StageStatus } from '../incidentModel.ts'

const STATUS_TEXT: Record<StageStatus, string> = {
  done: 'Done',
  active: 'In progress',
  waiting: 'Not started',
  skipped: 'Skipped',
  failed: 'Failed',
}

const STATUS_ICON: Record<StageStatus, string> = {
  done: '✓',
  active: '●',
  waiting: '○',
  skipped: '–',
  failed: '✗',
}

// Live agent-status view: one step per pipeline stage (PIPELINE.md order).
export function PipelineTracker({ stages }: { stages: Stage[] }) {
  return (
    <section className="panel pipeline" aria-labelledby="pipeline-title">
      <h2 id="pipeline-title">Pipeline</h2>
      <ol className="stages">
        {stages.map((stage) => (
          <li key={stage.id} className={`stage stage-${stage.status}`}>
            <span className="stage-icon" aria-hidden="true">{STATUS_ICON[stage.status]}</span>
            <span className="stage-body">
              <span className="stage-label">{stage.label}</span>
              <span className="stage-meta">
                <span className="visually-hidden">{STATUS_TEXT[stage.status]}. </span>
                {stage.detail || STATUS_TEXT[stage.status]}
              </span>
            </span>
          </li>
        ))}
      </ol>
    </section>
  )
}
