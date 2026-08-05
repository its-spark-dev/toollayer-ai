/**
 * The side-by-side is the console's reason to exist, so its behavior is asserted rather than
 * assumed: a reviewer must be able to see the source operation and the generated tool, and
 * the provider tabs must say what they cannot show yet instead of showing nothing.
 */

import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { TransformationView } from './TransformationView'
import type { AnalyzedOperation, ToolDefinition } from '../api/types'

const tool: ToolDefinition = {
  tool_name: 'list_support_tickets',
  display_name: 'List support tickets',
  description: 'Search the support queue.',
  input_schema: {
    $schema: 'https://json-schema.org/draft/2020-12/schema',
    type: 'object',
    properties: { status: { type: 'string' } },
    required: [],
    additionalProperties: false,
  },
  operation: {
    protocol: 'http',
    method: 'GET',
    path_template: '/v1/tickets',
    bindings: [{ argument_pointer: '/status', target: 'query', target_name: 'status' }],
    request_body_media_type: null,
  },
  policy: {
    effect_class: 'read',
    requires_confirmation: false,
    access: { access_mode: 'public', allowed_roles: [] },
  },
  provenance: {
    source_operation_id: 'listSupportTickets',
    source_path: '/v1/tickets',
    source_method: 'get',
    tags: ['tickets'],
    deprecated: false,
    description_origin: 'source',
  },
}

const operation: AnalyzedOperation = {
  key: 'get /v1/tickets',
  path: '/v1/tickets',
  method: 'get',
  pointer: '/paths/~1v1~1tickets/get',
  source_operation: { operationId: 'listSupportTickets', summary: 'List support tickets' },
  tool,
  diagnostics: [],
}

describe('TransformationView', () => {
  it('shows the generated tool definition first', () => {
    render(<TransformationView operation={operation} tool={tool} />)
    expect(screen.getByTestId('pane-canonical')).toHaveTextContent('list_support_tickets')
    expect(screen.getByTestId('pane-canonical')).toHaveTextContent('additionalProperties')
  })

  it('shows the source operation the tool was derived from', async () => {
    render(<TransformationView operation={operation} tool={tool} />)
    await userEvent.click(screen.getByRole('tab', { name: 'Source operation' }))
    expect(screen.getByTestId('pane-source')).toHaveTextContent('listSupportTickets')
  })

  it('states that the canonical format is project-defined, not a standard', () => {
    render(<TransformationView operation={operation} tool={tool} />)
    expect(screen.getByText(/not an industry standard/i)).toBeInTheDocument()
  })

  it('explains the provider projection rather than showing nothing', async () => {
    render(<TransformationView operation={operation} tool={tool} />)
    await userEvent.click(screen.getByRole('tab', { name: 'OpenAI projection' }))
    expect(screen.getByText(/widened to accept/i)).toBeInTheDocument()
    expect(screen.getByTestId('pane-openai')).toHaveTextContent('Publish this connector')
  })

  it('renders a provider projection when one is available', async () => {
    render(
      <TransformationView
        operation={operation}
        tool={tool}
        projections={{
          openai: {
            provider: 'openai',
            connector_key: 'support-api',
            version: '0.1.0',
            complete: true,
            tools: [
              {
                type: 'function',
                function: { name: 'list_support_tickets', parameters: {}, strict: true },
              },
            ],
            diagnostics: [],
          },
        }}
      />,
    )
    await userEvent.click(screen.getByRole('tab', { name: 'OpenAI projection' }))
    expect(screen.getByTestId('pane-openai')).toHaveTextContent('"strict": true')
  })
})
