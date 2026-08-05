/**
 * The Control Plane HTTP client.
 *
 * One place converts a failure response into an `ApiError` carrying the server's stable
 * code, so no component has to interpret a status code or parse an error body. The console
 * then branches on the code — `revision_conflict` means "reload and retry", not "something
 * went wrong".
 */

import type {
  AdapterProjection,
  ConnectorSummary,
  DeploymentSummary,
  Draft,
  ErrorEnvelope,
  ReviewUpdate,
  SnapshotSummary,
  VersionSummary,
} from './types'

export class ApiError extends Error {
  readonly code: string
  readonly status: number
  readonly pointer: string | null
  readonly details: { code: string; message: string }[]

  constructor(status: number, envelope: ErrorEnvelope) {
    super(envelope.error.message)
    this.name = 'ApiError'
    this.status = status
    this.code = envelope.error.code
    this.pointer = envelope.error.pointer ?? null
    this.details = envelope.error.details ?? []
  }
}

export interface ClientConfig {
  baseUrl: string
  adminToken: string
}

export function defaultConfig(): ClientConfig {
  return {
    baseUrl: import.meta.env.VITE_CONTROL_PLANE_URL ?? 'http://localhost:8080',
    adminToken: import.meta.env.VITE_ADMIN_TOKEN ?? 'dev-admin-token-change-me',
  }
}

export class ControlPlaneClient {
  constructor(private readonly config: ClientConfig) {}

  private async request<T>(method: string, path: string, body?: unknown): Promise<T> {
    const response = await fetch(`${this.config.baseUrl}${path}`, {
      method,
      headers: {
        'content-type': 'application/json',
        'x-toollayer-admin-token': this.config.adminToken,
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    })

    if (!response.ok) {
      let envelope: ErrorEnvelope
      try {
        envelope = (await response.json()) as ErrorEnvelope
      } catch {
        // A response that is not the shared envelope is still a failure. Synthesizing one
        // keeps every caller on a single error shape.
        envelope = {
          error: { code: 'unexpected_response', message: `HTTP ${response.status}` },
        }
      }
      throw new ApiError(response.status, envelope)
    }

    if (response.status === 204) {
      return undefined as T
    }
    return (await response.json()) as T
  }

  health(): Promise<{ status: string; contract_version: string }> {
    return this.request('GET', '/healthz')
  }

  listConnectors(): Promise<ConnectorSummary[]> {
    return this.request('GET', '/admin/v1/connectors')
  }

  registerConnector(input: {
    connector_key: string
    document: string
    document_filename: string
    base_url?: string | null
    display_name?: string | null
  }): Promise<Draft> {
    return this.request('POST', '/admin/v1/connectors', input)
  }

  getDraft(connectorKey: string): Promise<Draft> {
    return this.request('GET', `/admin/v1/connectors/${connectorKey}/draft`)
  }

  updateDraft(
    connectorKey: string,
    expectedRevision: number,
    operations: ReviewUpdate[],
    extra: { proposed_version?: string; base_url?: string } = {},
  ): Promise<Draft> {
    return this.request('PATCH', `/admin/v1/connectors/${connectorKey}/draft`, {
      expected_revision: expectedRevision,
      operations,
      ...extra,
    })
  }

  publish(connectorKey: string, expectedRevision: number, version?: string): Promise<VersionSummary> {
    return this.request('POST', `/admin/v1/connectors/${connectorKey}/publish`, {
      expected_revision: expectedRevision,
      ...(version ? { version } : {}),
    })
  }

  listVersions(connectorKey: string): Promise<VersionSummary[]> {
    return this.request('GET', `/admin/v1/connectors/${connectorKey}/versions`)
  }

  getVersion(
    connectorKey: string,
    version: string,
  ): Promise<{ summary: VersionSummary; document: Record<string, unknown> }> {
    return this.request('GET', `/admin/v1/connectors/${connectorKey}/versions/${version}`)
  }

  getAdapterProjection(
    connectorKey: string,
    version: string,
    provider: string,
  ): Promise<AdapterProjection> {
    return this.request(
      'GET',
      `/admin/v1/connectors/${connectorKey}/versions/${version}/adapters/${provider}`,
    )
  }

  listDeployments(): Promise<DeploymentSummary[]> {
    return this.request('GET', '/admin/v1/deployments')
  }

  createDeployment(input: { deployment_key: string; display_name: string }): Promise<DeploymentSummary> {
    return this.request('POST', '/admin/v1/deployments', input)
  }

  createSnapshot(
    deploymentKey: string,
    selections: { connector_key: string; version: string }[],
  ): Promise<SnapshotSummary> {
    return this.request('POST', `/admin/v1/deployments/${deploymentKey}/snapshots`, { selections })
  }

  listSnapshots(deploymentKey: string): Promise<SnapshotSummary[]> {
    return this.request('GET', `/admin/v1/deployments/${deploymentKey}/snapshots`)
  }
}
