import { existsSync, readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { expect, test } from 'vitest'

test('the operator entrypoint cannot reach synthetic clients, catalogs or maps', () => {
  const pending = [resolve(process.cwd(), 'src/main.tsx')]
  const visited = new Set<string>()
  while (pending.length) {
    const path = pending.pop()!
    if (visited.has(path)) continue
    visited.add(path)
    expect(path).not.toMatch(/\/testing\/|\.test\.|ReferenceModule|StatesGallery|MissionTracker/)
    const source = readFileSync(path, 'utf8')
    expect(source).not.toMatch(/new FixtureRelayClient|new FixtureCatalogClient|fixtureMapEndpoint\(/)
    // Follow code imports and re-exports, including dynamic imports. Tests may use
    // synthetic peers; nothing reachable from the operator boot may import them.
    for (const match of source.matchAll(/(?:from\s*|import\s*\(?\s*)['"](\.[^'"]+)['"]/g)) {
      const base = resolve(dirname(path), match[1])
      // Recorded artifact JSON is test evidence too; do not let the extension
      // shortcut hide a fixture import from the production dependency walk.
      expect(base).not.toMatch(/\/testing\/|\.test\./)
      if (/\.(css|svg|png|jpg|json)$/.test(base)) continue
      const dependency = [base, `${base}.ts`, `${base}.tsx`, `${base}/index.ts`, `${base}/index.tsx`]
        .find((candidate) => /\.tsx?$/.test(candidate) && existsSync(candidate))
      if (dependency) pending.push(dependency)
    }
  }
  expect(visited.size).toBeGreaterThan(40)
})
