/**
 * Types mirroring the Control Plane's HTTP surface.
 *
 * These are hand-written rather than generated. The console is a consumer of a versioned
 * API, and writing the types by hand means a server change that the console has not been
 * taught about shows up as a type error rather than as a silently regenerated shape.
 */

export type EffectClass = 'read' | 'write' | 'destructive'
export type AccessMode = 'public' | 'restricted'
export type Selection = 'included' | 'excluded'
export type DescriptionOrigin = 'source' | 'generated' | 'assisted' | 'human'
export type Severity = 'error' | 'warning'

export interface ArgumentBinding {
  argument_pointer: string
  target: 'path' | 'query' | 'header' | 'body'
  target_name: string
}

export interface ToolOperation {
  protocol: 'http'
  method: string
  path_template: string
  bindings: ArgumentBinding[]
  request_body_media_type: string | null
}

export interface ToolAccessPolicy {
  access_mode: AccessMode
  allowed_roles: string[]
}

export interface ToolPolicy {
  effect_class: EffectClass
  requires_confirmation: boolean
  access: ToolAccessPolicy
}

export interface ToolProvenance {
  source_operation_id: string | null
  source_path: string
  source_method: string
  tags: string[]
  deprecated: boolean
  description_origin: DescriptionOrigin
}

export interface ToolDefinition {
  tool_name: string
  display_name: string
  description: string
  input_schema: Record<string, unknown>
  operation: ToolOperation
  policy: ToolPolicy
  provenance: ToolProvenance
}

export interface Diagnostic {
  code: string
  message: string
  pointer: string
  severity: Severity
  operation_key: string | null
}

export interface AnalyzedOperation {
  key: string
  path: string
  method: string
  pointer: string
  source_operation: Record<string, unknown>
  tool: ToolDefinition | null
  diagnostics: Diagnostic[]
}

export interface Analysis {
  analyzer_version: string
  spec_version: string
  api_title: string
  api_summary: string
  base_url: string | null
  diagnostics: Diagnostic[]
  operations: AnalyzedOperation[]
}

export interface OperationReview {
  operation_key: string
  selection: Selection
  description: string
  description_origin: DescriptionOrigin
  effect_class: EffectClass
  requires_confirmation: boolean
  access_mode: AccessMode
  allowed_roles: string[]
}

export interface Draft {
  connector_key: string
  display_name: string
  summary: string
  revision: number
  proposed_version: string
  base_url: string | null
  auth_profile_ref: string | null
  source: {
    filename: string
    digest: string
    byte_length: number
    format: string
    spec_version: string
  }
  analyzer_version: string
  analysis: Analysis
  review: { operations: OperationReview[] }
  readiness: { ready: boolean; issues: string[] }
  updated_at: string
}

export interface ConnectorSummary {
  connector_key: string
  display_name: string
  summary: string
  has_draft: boolean
  draft_revision: number | null
  published_versions: string[]
  latest_version: string | null
  created_at: string
  updated_at: string
}

export interface VersionSummary {
  connector_key: string
  version: string
  document_digest: string
  tool_count: number
  tool_names: string[]
  published_at: string
  published_by: string
  disabled: boolean
  disabled_reason: string | null
}

export interface AdapterProjection {
  provider: string
  connector_key: string
  version: string
  complete: boolean
  tools: Record<string, unknown>[]
  diagnostics: { tool_name: string; code: string; message: string; pointer: string }[]
}

export interface DeploymentSummary {
  deployment_key: string
  display_name: string
  description: string
  created_at: string
  snapshot_count: number
  active_revision: number | null
  active_snapshot_id: string | null
}

export interface SnapshotSummary {
  deployment_key: string
  revision: number
  snapshot_id: string
  snapshot_digest: string
  connector_count: number
  tool_count: number
  active: boolean
  created_at: string
  created_by: string
}

/** The shared failure shape. Clients branch on `code`, never on `message`. */
export interface ErrorEnvelope {
  error: {
    code: string
    message: string
    pointer?: string | null
    details?: { code: string; message: string; pointer?: string | null; severity?: Severity }[]
    request_id?: string | null
  }
}

export interface ReviewUpdate {
  operation_key: string
  selection?: Selection
  description?: string
  description_origin?: DescriptionOrigin
  effect_class?: EffectClass
  requires_confirmation?: boolean
  access_mode?: AccessMode
  allowed_roles?: string[]
}
