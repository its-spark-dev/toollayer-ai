/**
 * The Tool Control Plane console.
 *
 * One page, four stages, in the order the pipeline actually runs: Register → Review →
 * Publish → Deploy. The console never publishes a document it constructed — it sends review
 * decisions, and the server rebuilds the artifact from its own stored state. That is why a
 * bug here cannot publish a definition nobody reviewed.
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { ApiError, ControlPlaneClient, defaultConfig } from './api/client'
import type {
  AdapterProjection,
  ConnectorSummary,
  DeploymentSummary,
  Draft,
  ReviewUpdate,
  SnapshotSummary,
  ToolDefinition,
  VersionSummary,
} from './api/types'
import { OperationReviewCard } from './components/OperationReviewCard'
import { TransformationView } from './components/TransformationView'
import { SAMPLE_SPEC } from './sampleSpec'

const CONNECTOR_KEY = 'support-api'
const DEPLOYMENT_KEY = 'demo-workspace'

type Stage = 'register' | 'review' | 'publish' | 'deploy'

/**
 * Pick the operation to open first after analysis.
 *
 * Opening whichever operation happens to sort first is a poor introduction — it is often a
 * parameterless list endpoint whose generated schema is empty, which makes the conversion look
 * like it did nothing. Preferring the operation with the most arguments shows a reviewer what
 * the converter actually produces: named properties, enums, bounds, and bindings.
 */
function mostIllustrative(draft: Draft): string | null {
  const convertible = draft.analysis.operations.filter((entry) => entry.tool)
  if (convertible.length === 0) return null
  const scored = convertible
    .map((entry) => ({
      key: entry.key,
      arguments: Object.keys(entry.tool?.input_schema.properties ?? {}).length,
    }))
    .sort((a, b) => b.arguments - a.arguments || a.key.localeCompare(b.key))
  return scored[0]?.key ?? null
}

export default function App() {
  const client = useMemo(() => new ControlPlaneClient(defaultConfig()), [])

  const [stage, setStage] = useState<Stage>('register')
  const [connectors, setConnectors] = useState<ConnectorSummary[]>([])
  const [draft, setDraft] = useState<Draft | null>(null)
  const [versions, setVersions] = useState<VersionSummary[]>([])
  const [deployments, setDeployments] = useState<DeploymentSummary[]>([])
  const [snapshots, setSnapshots] = useState<SnapshotSummary[]>([])
  const [projections, setProjections] = useState<Record<string, AdapterProjection>>({})
  const [publishedDocument, setPublishedDocument] = useState<Record<string, unknown> | null>(null)
  const [selectedKey, setSelectedKey] = useState<string | null>(null)
  const [document, setDocument] = useState(SAMPLE_SPEC)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const report = useCallback((thrown: unknown) => {
    if (thrown instanceof ApiError) {
      const detail = thrown.details.map((entry) => entry.message).join('; ')
      setError(`${thrown.code}: ${thrown.message}${detail ? ` — ${detail}` : ''}`)
    } else if (thrown instanceof Error) {
      setError(thrown.message)
    } else {
      setError('the request could not be completed')
    }
  }, [])

  const run = useCallback(
    async (action: () => Promise<void>) => {
      setBusy(true)
      setError(null)
      try {
        await action()
      } catch (thrown) {
        report(thrown)
      } finally {
        setBusy(false)
      }
    },
    [report],
  )

  const refreshConnectors = useCallback(async () => {
    setConnectors(await client.listConnectors())
  }, [client])


  const loadVersions = useCallback(async () => {
    const list = await client.listVersions(CONNECTOR_KEY)
    setVersions(list)
    const latest = list.at(-1)
    if (latest) {
      const stored = await client.getVersion(CONNECTOR_KEY, latest.version)
      setPublishedDocument(stored.document)
      const entries = await Promise.all(
        ['openai', 'anthropic'].map(async (provider) => {
          const projection = await client.getAdapterProjection(
            CONNECTOR_KEY,
            latest.version,
            provider,
          )
          return [provider, projection] as const
        }),
      )
      setProjections(Object.fromEntries(entries))
    }
  }, [client])

  // Load what already exists on mount. Without this the Publish and Deploy stages look empty
  // until the current session happens to publish something — which is wrong for anyone opening
  // the console against a Control Plane that has been used before.
  useEffect(() => {
    void run(async () => {
      await refreshConnectors()
      const published = await client.listVersions(CONNECTOR_KEY).catch(() => [])
      if (published.length > 0) {
        await loadVersions()
      }
      const existing = await client.listDeployments().catch(() => [])
      setDeployments(existing)
      if (existing.some((entry) => entry.deployment_key === DEPLOYMENT_KEY)) {
        setSnapshots(await client.listSnapshots(DEPLOYMENT_KEY).catch(() => []))
      }
    })
  }, [run, refreshConnectors, client, loadVersions])

  const register = () =>
    run(async () => {
      const created = await client.registerConnector({
        connector_key: CONNECTOR_KEY,
        document,
        document_filename: 'support-api.openapi.yaml',
      })
      setDraft(created)
      setSelectedKey(mostIllustrative(created))
      setStage('review')
      setNotice(
        `Analyzed ${created.analysis.operations.length} operations from ${created.source.byte_length} bytes.`,
      )
      await refreshConnectors()
    })

  const applyReview = (update: ReviewUpdate) =>
    run(async () => {
      if (!draft) return
      // The revision the console read is sent back. If someone else changed the draft in
      // between, the server rejects rather than merging, and the console reloads.
      setDraft(await client.updateDraft(CONNECTOR_KEY, draft.revision, [update]))
    })

  const publish = () =>
    run(async () => {
      if (!draft) return
      const published = await client.publish(CONNECTOR_KEY, draft.revision, draft.proposed_version)
      setDraft(null)
      setNotice(
        `Published ${published.version} with ${published.tool_count} tools — digest ${published.document_digest.slice(0, 23)}…`,
      )
      await loadVersions()
      await refreshConnectors()
      setStage('publish')
    })

  const deploy = () =>
    run(async () => {
      const latest = versions.at(-1)
      if (!latest) throw new Error('publish a version first')
      const existing = await client.listDeployments()
      if (!existing.some((entry) => entry.deployment_key === DEPLOYMENT_KEY)) {
        await client.createDeployment({
          deployment_key: DEPLOYMENT_KEY,
          display_name: 'Demo Workspace',
        })
      }
      const snapshot = await client.createSnapshot(DEPLOYMENT_KEY, [
        { connector_key: CONNECTOR_KEY, version: latest.version },
      ])
      setNotice(
        `Snapshot revision ${snapshot.revision} pins ${snapshot.connector_count} connector and ${snapshot.tool_count} tools.`,
      )
      setDeployments(await client.listDeployments())
      setSnapshots(await client.listSnapshots(DEPLOYMENT_KEY))
      setStage('deploy')
    })

  const selected = draft?.analysis.operations.find((entry) => entry.key === selectedKey) ?? null
  const selectedReview = draft?.review.operations.find(
    (entry) => entry.operation_key === selectedKey,
  )

  // What the active snapshot actually contains, read back from the published documents rather
  // than from what the console believes it sent. This is the same set the Runtime will serve.
  const servedTools = useMemo(() => {
    const latest = versions.at(-1)
    if (!latest || !publishedDocument) return []
    const tools = (publishedDocument.tools ?? []) as ToolDefinition[]
    return tools.map((tool) => ({
      tool_name: tool.tool_name,
      description: tool.description,
      effect_class: tool.policy.effect_class,
      requires_confirmation: tool.policy.requires_confirmation,
      restricted: tool.policy.access.access_mode === 'restricted',
      allowed_roles: tool.policy.access.allowed_roles,
    }))
  }, [versions, publishedDocument])

  return (
    <div className="app">
      <header className="app__header">
        <div>
          <h1>ToolLayer AI</h1>
          <p className="tagline">Build once. Orchestrate anywhere.</p>
        </div>
        <nav className="stages" aria-label="Pipeline stage">
          {(
            [
              ['register', '1 · Register'],
              ['review', '2 · Review'],
              ['publish', '3 · Publish'],
              ['deploy', '4 · Deploy'],
            ] as [Stage, string][]
          ).map(([value, label]) => (
            <button
              key={value}
              className={stage === value ? 'stage stage--active' : 'stage'}
              onClick={() => setStage(value)}
            >
              {label}
            </button>
          ))}
        </nav>
      </header>

      {error && (
        <div className="banner banner--error" role="alert">
          {error}
        </div>
      )}
      {notice && !error && (
        <div className="banner banner--notice" role="status">
          {notice}
        </div>
      )}

      <main className="app__main">
        {stage === 'register' && (
          <section className="panel">
            <h2>Register an API description</h2>
            <p>
              Upload an OpenAPI 3.0 or 3.1 document. The Control Plane keeps the exact bytes and
              their SHA-256 digest, then analyzes every operation into a provider-neutral tool
              definition. Nothing is published yet.
            </p>
            <textarea
              className="spec-input"
              rows={18}
              value={document}
              spellCheck={false}
              onChange={(event) => setDocument(event.target.value)}
              aria-label="OpenAPI document"
            />
            <button className="primary" onClick={register} disabled={busy}>
              {busy ? 'Analyzing…' : 'Analyze document'}
            </button>
            {connectors.length > 0 && (
              <p className="hint">
                Registered connectors:{' '}
                {connectors
                  .map(
                    (entry) =>
                      `${entry.connector_key}${entry.latest_version ? ` (${entry.latest_version})` : ''}`,
                  )
                  .join(', ')}
              </p>
            )}
          </section>
        )}

        {stage === 'review' && draft && (
          <section className="panel panel--split">
            <div className="panel__left">
              <h2>Review the proposal</h2>
              <p className="hint">
                Draft revision {draft.revision} · analyzer {draft.analyzer_version} · source{' '}
                <code>{draft.source.digest.slice(0, 20)}…</code>
              </p>
              {!draft.readiness.ready && (
                <ul className="diagnostics">
                  {draft.readiness.issues.map((issue) => (
                    <li key={issue} className="diagnostic diagnostic--error">
                      {issue}
                    </li>
                  ))}
                </ul>
              )}
              <ul className="operations">
                {draft.analysis.operations.map((operation) => (
                  <OperationReviewCard
                    key={operation.key}
                    operation={operation}
                    review={draft.review.operations.find(
                      (entry) => entry.operation_key === operation.key,
                    )}
                    selected={operation.key === selectedKey}
                    onSelect={() => setSelectedKey(operation.key)}
                    onChange={applyReview}
                    disabled={busy}
                  />
                ))}
              </ul>
              <button
                className="primary"
                onClick={publish}
                disabled={busy || !draft.readiness.ready}
              >
                Publish {draft.proposed_version}
              </button>
            </div>
            <div className="panel__right">
              {selected?.tool && selectedReview ? (
                <TransformationView
                  operation={selected}
                  tool={{ ...selected.tool, description: selectedReview.description }}
                  projections={projections}
                />
              ) : (
                <p className="hint">Select an operation to see its transformation.</p>
              )}
            </div>
          </section>
        )}

        {stage === 'publish' && (
          <section className="panel">
            <h2>Published versions</h2>
            <p>
              A published version is immutable. Changing it means publishing a new version; the
              digest below covers the entire document, so a consumer can verify what it received.
            </p>
            <button onClick={() => run(loadVersions)} disabled={busy}>
              Refresh
            </button>
            <table className="table">
              <thead>
                <tr>
                  <th>Version</th>
                  <th>Tools</th>
                  <th>Digest</th>
                  <th>Published</th>
                  <th>State</th>
                </tr>
              </thead>
              <tbody>
                {versions.map((version) => (
                  <tr key={version.version}>
                    <td>
                      <code>{version.version}</code>
                    </td>
                    <td>{version.tool_count}</td>
                    <td>
                      <code>{version.document_digest.slice(0, 23)}…</code>
                    </td>
                    <td>{version.published_at}</td>
                    <td>{version.disabled ? 'disabled' : 'published'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {versions.length === 0 && <p className="hint">Nothing published yet.</p>}
            <button className="primary" onClick={deploy} disabled={busy || versions.length === 0}>
              Create a deployment snapshot
            </button>
          </section>
        )}

        {stage === 'deploy' && (
          <section className="panel">
            <h2>Deployment snapshots</h2>
            <p>
              A snapshot pins exactly one published version per connector. It is never edited —
              a change produces the next revision, and the previous one stays byte-identical.
              The Runtime recomputes the digest to check the content, then verifies the
              signature to check who produced it, before serving anything.
            </p>
            <table className="table">
              <thead>
                <tr>
                  <th>Revision</th>
                  <th>Snapshot</th>
                  <th>Connectors</th>
                  <th>Tools</th>
                  <th>Digest</th>
                  <th>Active</th>
                </tr>
              </thead>
              <tbody>
                {snapshots.map((snapshot) => (
                  <tr key={snapshot.revision}>
                    <td>{snapshot.revision}</td>
                    <td>
                      <code>{snapshot.snapshot_id.slice(0, 18)}…</code>
                    </td>
                    <td>{snapshot.connector_count}</td>
                    <td>{snapshot.tool_count}</td>
                    <td>
                      <code>{snapshot.snapshot_digest.slice(0, 20)}…</code>
                    </td>
                    <td>{snapshot.active ? 'yes' : 'superseded'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {deployments.length > 0 && (
              <p className="hint">
                Deployment <code>{DEPLOYMENT_KEY}</code> is serving revision{' '}
                {deployments[0]?.active_revision ?? '—'}.
              </p>
            )}

            {servedTools.length > 0 && (
              <>
                <h3 className="subhead">Tools this deployment may serve</h3>
                <p className="hint">
                  Exactly this set, from exactly these versions. The Runtime resolves a tool name
                  against the snapshot and refuses anything that is not here.
                </p>
                <ul className="served">
                  {servedTools.map((tool) => (
                    <li key={tool.tool_name} className="served__item">
                      <code className="tool-name">{tool.tool_name}</code>
                      <span className={`badge badge--${tool.effect_class}`}>
                        {tool.effect_class}
                      </span>
                      {tool.restricted && (
                        <span className="badge badge--locked">
                          {tool.allowed_roles.join(', ')}
                        </span>
                      )}
                      {tool.requires_confirmation && (
                        <span className="badge badge--confirm">confirmation</span>
                      )}
                      <span className="served__desc">{tool.description}</span>
                    </li>
                  ))}
                </ul>
              </>
            )}
          </section>
        )}
      </main>

      <footer className="app__footer">
        <p>
          The runtime is provided as a reference implementation rather than a full chatbot
          product. This console is a demonstration interface for the Tool Control Plane.
        </p>
      </footer>
    </div>
  )
}
