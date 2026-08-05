/**
 * The controls a reviewer actually has over one operation.
 *
 * Only the decisions a human should own are editable: whether to publish the operation at
 * all, the model-facing description, how much effect it may have, and which roles may call
 * it. The input schema, path, method, and bindings are shown but not editable — they are
 * derived from the source document, and hand-editing them would mean the published tool no
 * longer described the API it claims to describe.
 */

import type { AnalyzedOperation, OperationReview, ReviewUpdate } from '../api/types'

const KNOWN_ROLES = ['support-agent', 'support-lead', 'auditor']

interface Props {
  operation: AnalyzedOperation
  review: OperationReview | undefined
  selected: boolean
  onSelect: () => void
  onChange: (update: ReviewUpdate) => void
  disabled: boolean
}

export function OperationReviewCard({
  operation,
  review,
  selected,
  onSelect,
  onChange,
  disabled,
}: Props) {
  const tool = operation.tool
  const errors = operation.diagnostics.filter((d) => d.severity === 'error')
  const warnings = operation.diagnostics.filter((d) => d.severity === 'warning')

  if (!tool || !review) {
    return (
      <li className="operation operation--blocked">
        <div className="operation__head">
          <code className="method">{operation.method.toUpperCase()}</code>
          <span className="path">{operation.path}</span>
        </div>
        <ul className="diagnostics">
          {errors.map((diagnostic) => (
            <li key={diagnostic.code + diagnostic.pointer} className="diagnostic diagnostic--error">
              <strong>{diagnostic.code}</strong> {diagnostic.message}
            </li>
          ))}
        </ul>
        <p className="hint">
          This operation is not published. The converter refuses what it cannot represent
          faithfully rather than approximating it.
        </p>
      </li>
    )
  }

  const included = review.selection === 'included'

  return (
    <li
      className={
        'operation' +
        (selected ? ' operation--selected' : '') +
        (included ? '' : ' operation--excluded')
      }
    >
      <button className="operation__head" onClick={onSelect} aria-pressed={selected}>
        <code className="method">{operation.method.toUpperCase()}</code>
        <span className="path">{operation.path}</span>
        <code className="tool-name">{tool.tool_name}</code>
        <span className={`badge badge--${review.effect_class}`}>{review.effect_class}</span>
        {review.access_mode === 'restricted' && <span className="badge badge--locked">restricted</span>}
      </button>

      <div className="operation__body">
        <label className="field field--inline">
          <input
            type="checkbox"
            checked={included}
            disabled={disabled}
            onChange={(event) =>
              onChange({
                operation_key: review.operation_key,
                selection: event.target.checked ? 'included' : 'excluded',
              })
            }
          />
          <span>Publish this operation as a tool</span>
        </label>

        <label className="field">
          <span className="field__label">
            Model-facing description
            <em className="origin">{review.description_origin}</em>
          </span>
          <textarea
            rows={3}
            defaultValue={review.description}
            disabled={disabled || !included}
            onBlur={(event) => {
              if (event.target.value.trim() !== review.description) {
                onChange({
                  operation_key: review.operation_key,
                  description: event.target.value.trim(),
                })
              }
            }}
          />
        </label>

        <div className="field-row">
          <label className="field">
            <span className="field__label">Effect</span>
            <select
              value={review.effect_class}
              disabled={disabled || !included}
              onChange={(event) =>
                onChange({
                  operation_key: review.operation_key,
                  effect_class: event.target.value as OperationReview['effect_class'],
                })
              }
            >
              <option value="read">read</option>
              <option value="write">write</option>
              <option value="destructive">destructive</option>
            </select>
          </label>

          <label className="field field--inline">
            <input
              type="checkbox"
              checked={review.requires_confirmation}
              disabled={disabled || !included}
              onChange={(event) =>
                onChange({
                  operation_key: review.operation_key,
                  requires_confirmation: event.target.checked,
                })
              }
            />
            <span>Require explicit confirmation</span>
          </label>
        </div>

        <fieldset className="field" disabled={disabled || !included}>
          <legend className="field__label">Who may call this tool</legend>
          <label className="field--inline">
            <input
              type="radio"
              name={`access-${review.operation_key}`}
              checked={review.access_mode === 'public'}
              onChange={() =>
                onChange({
                  operation_key: review.operation_key,
                  access_mode: 'public',
                  allowed_roles: [],
                })
              }
            />
            <span>Any caller of this deployment</span>
          </label>
          <label className="field--inline">
            <input
              type="radio"
              name={`access-${review.operation_key}`}
              checked={review.access_mode === 'restricted'}
              onChange={() =>
                onChange({
                  operation_key: review.operation_key,
                  access_mode: 'restricted',
                  allowed_roles: review.allowed_roles.length ? review.allowed_roles : ['support-lead'],
                })
              }
            />
            <span>Only these roles</span>
          </label>
          {review.access_mode === 'restricted' && (
            <div className="roles">
              {KNOWN_ROLES.map((role) => (
                <label key={role} className="field--inline">
                  <input
                    type="checkbox"
                    checked={review.allowed_roles.includes(role)}
                    onChange={(event) => {
                      const next = event.target.checked
                        ? [...review.allowed_roles, role]
                        : review.allowed_roles.filter((entry) => entry !== role)
                      // A restricted tool with no roles is refused by the server, so the
                      // console does not offer it as a reachable state.
                      if (next.length === 0) return
                      onChange({
                        operation_key: review.operation_key,
                        access_mode: 'restricted',
                        allowed_roles: next,
                      })
                    }}
                  />
                  <code>{role}</code>
                </label>
              ))}
            </div>
          )}
        </fieldset>

        {warnings.length > 0 && (
          <ul className="diagnostics">
            {warnings.map((diagnostic) => (
              <li key={diagnostic.code} className="diagnostic diagnostic--warning">
                <strong>{diagnostic.code}</strong> {diagnostic.message}
              </li>
            ))}
          </ul>
        )}
      </div>
    </li>
  )
}
