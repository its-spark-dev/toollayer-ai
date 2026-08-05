/**
 * The side-by-side that makes the pipeline reviewable.
 *
 * The default view puts the source OpenAPI operation next to the tool definition generated
 * from it. That pairing is the whole point of the console: a reviewer approving a
 * transformation should be able to see both ends of it, not just the output.
 *
 * The provider tabs then show the same tool definition projected into two public formats,
 * which is how the provider-neutrality claim is made inspectable rather than asserted.
 */

import { useMemo, useState } from 'react'
import type { AdapterProjection, AnalyzedOperation, ToolDefinition } from '../api/types'

type Pane = 'compare' | 'source' | 'canonical' | 'openai' | 'anthropic'

interface Props {
  operation: AnalyzedOperation
  tool: ToolDefinition
  projections?: Record<string, AdapterProjection>
}

// Short labels so the row never wraps at the console's working width. The longer names are
// on the pane captions and the explanatory text, where there is room for them.
const TABS: [Pane, string][] = [
  ['compare', 'Side by side'],
  ['source', 'Source operation'],
  ['canonical', 'Tool definition'],
  ['openai', 'OpenAI'],
  ['anthropic', 'Anthropic'],
]

export function TransformationView({ operation, tool, projections }: Props) {
  const [pane, setPane] = useState<Pane>('compare')

  const providerContent = useMemo(() => {
    if (pane !== 'openai' && pane !== 'anthropic') return null
    const projection = projections?.[pane]
    if (!projection) {
      return { note: `Publish this connector to see its ${pane} projection.` }
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
  }, [pane, tool, projections])

  return (
    <section className="transformation">
      <div className="tabs" role="tablist" aria-label="Transformation stage">
        {TABS.map(([value, label]) => (
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

      {pane === 'compare' && (
        <>
          <p className="hint">
            The source operation on the left, and the provider-neutral tool definition generated
            from it on the right. Conversion is deterministic: the same document always produces
            the same tools.
          </p>
          <div className="compare">
            <figure className="compare__pane">
              <figcaption className="compare__label">
                <span className="compare__step">Input</span> OpenAPI operation
              </figcaption>
              <pre className="code" data-testid="pane-source">
                {JSON.stringify(operation.source_operation, null, 2)}
              </pre>
            </figure>
            <div className="compare__arrow" aria-hidden="true">
              →
            </div>
            <figure className="compare__pane">
              <figcaption className="compare__label">
                <span className="compare__step compare__step--out">Output</span> Tool definition
              </figcaption>
              <pre className="code" data-testid="pane-canonical">
                {JSON.stringify(tool, null, 2)}
              </pre>
            </figure>
          </div>
        </>
      )}

      {pane === 'source' && (
        <>
          <p className="hint">
            The operation exactly as the uploaded document declares it, with same-document
            references already resolved.
          </p>
          <pre className="code code--tall" data-testid="pane-source">
            {JSON.stringify(operation.source_operation, null, 2)}
          </pre>
        </>
      )}

      {pane === 'canonical' && (
        <>
          <p className="hint">
            This representation is defined by this project. It is not an industry standard — the
            provider tabs show it projected into two public formats.
          </p>
          <pre className="code code--tall" data-testid="pane-canonical">
            {JSON.stringify(tool, null, 2)}
          </pre>
        </>
      )}

      {(pane === 'openai' || pane === 'anthropic') && (
        <>
          <p className="hint">
            {pane === 'openai'
              ? 'Strict function calling requires every property to be listed as required, so optional arguments are widened to accept null. The runtime reverses that before validating against the canonical schema.'
              : 'This target accepts ordinary JSON Schema, so optionality survives unchanged and nothing has to be normalized.'}
          </p>
          <pre className="code code--tall" data-testid={`pane-${pane}`}>
            {JSON.stringify(providerContent, null, 2)}
          </pre>
        </>
      )}
    </section>
  )
}
