// Regenerate src/sampleSpec.ts from the canonical example document.
//
// The console pre-fills a sample OpenAPI document. Keeping a hand-copied duplicate in the
// frontend would drift the first time the example changed, so it is generated instead.

import { readFileSync, writeFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const source = resolve(here, '../../../../examples/support-api.openapi.yaml')
const target = resolve(here, '../src/sampleSpec.ts')

const spec = readFileSync(source, 'utf8')
writeFileSync(
  target,
  `/**
 * The sample document the console pre-fills.
 *
 * Generated from \`examples/support-api.openapi.yaml\` by \`npm run sync:spec\`, so the console
 * and the demo script and the tests all exercise the same input. A hand-copied duplicate
 * would drift the first time the example changed.
 */

export const SAMPLE_SPEC = ${JSON.stringify(spec)}
`,
)
console.log(`sampleSpec.ts regenerated (${spec.length} characters)`)
