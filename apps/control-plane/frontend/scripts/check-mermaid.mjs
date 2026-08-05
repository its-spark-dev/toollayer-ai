// Parse every Mermaid diagram in the repository's Markdown.
//
// A diagram that fails to parse renders as an error box on GitHub. Nothing else in the test
// suite notices, because the diagrams are prose as far as Python is concerned — so this check
// exists, and it caught three broken sequence diagrams the first time it ran. (A semicolon
// inside message text terminates the statement, which silently breaks the whole diagram.)
//
// It lives in the console workspace rather than in the repository's `scripts/` directory for
// one practical reason: Node resolves bare imports from the importing module's directory, and
// this is the only place in the repository with a `node_modules`. Adding a second Node
// toolchain at the root just to hold two devDependencies would cost more than it explains.
//
//     npm --prefix apps/control-plane/frontend run check:diagrams

import { readdirSync, readFileSync, statSync } from 'node:fs'
import { dirname, join, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { JSDOM } from 'jsdom'

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '../../../..')
const SKIP = new Set(['node_modules', '.git', '.venv', 'private-notes', 'dist', 'data'])

function markdownFiles(directory) {
  const found = []
  for (const entry of readdirSync(directory)) {
    if (SKIP.has(entry)) continue
    const path = join(directory, entry)
    if (statSync(path).isDirectory()) {
      found.push(...markdownFiles(path))
    } else if (entry.endsWith('.md')) {
      found.push(path)
    }
  }
  return found
}

function diagrams(path) {
  const text = readFileSync(path, 'utf8')
  const blocks = []
  const pattern = /```mermaid\n([\s\S]*?)```/g
  let match
  let index = 0
  while ((match = pattern.exec(text)) !== null) {
    const line = text.slice(0, match.index).split('\n').length
    blocks.push({ source: match[1], index: index++, line })
  }
  return blocks
}

// Mermaid sanitizes diagram text through DOMPurify, which needs a DOM. Without one, every
// flowchart reports a spurious "DOMPurify.addHook is not a function" and hides real errors.
const dom = new JSDOM('<!doctype html><html><body></body></html>', { pretendToBeVisual: true })
globalThis.window = dom.window
globalThis.document = dom.window.document
Object.defineProperty(globalThis, 'navigator', {
  value: dom.window.navigator,
  configurable: true,
})

const mermaid = (await import('mermaid')).default
mermaid.initialize({ startOnLoad: false, securityLevel: 'loose' })

let checked = 0
let failed = 0

for (const path of markdownFiles(REPO_ROOT).sort()) {
  for (const block of diagrams(path)) {
    checked += 1
    try {
      await mermaid.parse(block.source)
    } catch (error) {
      failed += 1
      const where = `${relative(REPO_ROOT, path)}:${block.line}`
      console.error(`FAILED  ${where} (diagram #${block.index})`)
      console.error(
        String(error.message)
          .split('\n')
          .slice(0, 3)
          .map((line) => `        ${line}`)
          .join('\n'),
      )
    }
  }
}

if (failed > 0) {
  console.error(`\n${failed} of ${checked} diagrams failed to parse.`)
  process.exit(1)
}

console.log(`${checked} Mermaid diagrams parse.`)
