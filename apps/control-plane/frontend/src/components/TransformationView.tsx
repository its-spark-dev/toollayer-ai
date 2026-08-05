/**
 * The side-by-side that makes the pipeline reviewable.
 *
 * Four panes, left to right: the source OpenAPI operation, the generated provider-neutral
 * tool definition, and the two provider projections. Putting them next to each other is the
 * whole point of the console — a reviewer approving a transformation should be able to see
 * both ends of it, not just the output.
 */

import { useMemo, useState } from 'react'
import type { AdapterProjection, AnalyzedOperation, ToolDefinition } from '../api/types'

type Pane = 'source' | 'canonical' | 'openai' | 'anthropic'

interface Props {
  operation: AnalyzedOperation
  tool: ToolDefinition
  projections?: Record<string, AdapterProjection>
}

export function TransformationView({ operation, tool, projections }: Props) {
  const [pane, setPane] = useState<Pane>('canonical')

  const content = useMemo(() => {
    switch (pane) {
      case 'source':
        return operation.source_operation
      case 'canonical':
        return tool
      default: {
        const projection = projections?.[pane]
        if (!projection) {
          return {
            note: `Publish this connector to see its ${pane} projection.`,
          }
        }
        const projected = projection.tools.find(
          (entry) =>
            (entry as { name?: string }).name === tool.tool_name ||
            ((entry as { function?: { name?: string } }).function?.name ?? '') === tool.tool_name,
        )
        return (
          projected ?? {
            note: `This tool could not be projected for ${pane}.`,
            diagnostics: projection.diagnostics.filter((d) => d.tool_name === tool.tool_name),
          }
        )
      }
    }
  }, [pane, operation, tool, projections])

  return (
    <section className="transformation">
      <div className="tabs" role="tablist" aria-label="Transformation stage">
        {(
          [
            ['source', 'Source operation'],
            ['canonical', 'Tool definition'],
            ['openai', 'OpenAI projection'],
            ['anthropic', 'Anthropic projection'],
          ] as [Pane, string][]
        ).map(([value, label]) => (
          <button
            key={value}
            role="tab"
            aria-selected={pane === value}
            className={pane === value ? 'tab tab--active' : 'tab'}
            onClick={() => setPane(value)}
          >
            {label}
          </button>
        ))}
      </div>

      {pane === 'canonical' && (
        <p className="hint">
          This representation is defined by this project. It is not an industry standard — the
          provider tabs show it projected into two public formats.
        </p>
      )}
      {pane === 'openai' && (
        <p className="hint">
          Strict function calling requires every property to be listed as required, so optional
          arguments are widened to accept <code>null</code>. The runtime reverses that before
          validating against the canonical schema.
        </p>
      )}

      <pre className="code" data-testid={`pane-${pane}`}>
        {JSON.stringify(content, null, 2)}
      </pre>
    </section>
  )
}
