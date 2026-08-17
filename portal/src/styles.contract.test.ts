// Every badge class the components can construct must have a rule in styles.css.
//
// This exists because of a real defect that shipped and that nothing else could catch.
// `outcomeSlug` turns 'gated_sent' into the class `badge--outcome-gated-sent`, but the
// stylesheet defined `.badge--outcome-sent-after-review` — built from the *display label*
// instead of the *slug*. So the fully-autonomous send rendered green and the
// human-approved send — the whole payoff of the review gate — rendered as unstyled grey.
// The exact inverse of the emphasis the outcome column exists to create.
//
// Every existing display test asserts on text content, and the label 'sent after review'
// was correct, so none of them could see it. Only a class-level assertion can. The union
// members below are read from the generated api-types, so a new outcome or draft status
// arriving from the backend fails here until someone styles it.

import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

import type { DraftStatus, RunOutcome } from './api-types'
import { outcomeSlug } from './format'

// Resolved from the vitest root (portal/), not from `import.meta.url` — under the jsdom
// environment that is an http: URL, and `fileURLToPath` rejects it with
// "The URL must be of scheme file".
const srcFile = (name: string): string => resolve(process.cwd(), 'src', name)

const STYLESHEET = readFileSync(srcFile('styles.css'), 'utf8')

// Kept in sync with api-types.ts by the assertions in the last test below.
const RUN_OUTCOMES: RunOutcome[] = ['auto_sent', 'gated_sent', 'rejected', 'escalated', 'off_topic']
const DRAFT_STATUSES: DraftStatus[] = ['pending', 'approved', 'rejected', 'auto_sent']
const GATE_STATES = ['on', 'off', 'unknown']

/** Does styles.css contain a rule whose selector list includes exactly this class? */
function hasRuleFor(className: string): boolean {
  // Match `.class` only at a selector boundary, so `.badge--outcome-auto-sent` is never
  // satisfied by `.badge--outcome-auto-sent-something`.
  return new RegExp(`\\.${className.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}(?![\\w-])`).test(
    STYLESHEET,
  )
}

describe('every badge class the app can emit is styled', () => {
  it.each([...RUN_OUTCOMES, null])('outcome %s', (outcome) => {
    const className = `badge--outcome-${outcomeSlug(outcome)}`
    expect(hasRuleFor(className), `${className} is emitted but styles.css has no rule for it`).toBe(
      true,
    )
  })

  it.each([...DRAFT_STATUSES, null])('draft status %s', (status) => {
    const className = `badge--draft-${status ?? 'none'}`
    expect(hasRuleFor(className), `${className} is emitted but styles.css has no rule for it`).toBe(
      true,
    )
  })

  it.each(GATE_STATES)('gate state %s', (state) => {
    const className = `badge--gate-${state}`
    expect(hasRuleFor(className), `${className} is emitted but styles.css has no rule for it`).toBe(
      true,
    )
  })
})

describe('no dead badge selectors', () => {
  it('every badge--outcome-* rule corresponds to a slug the app can produce', () => {
    const emittable = new Set([...RUN_OUTCOMES, null].map((o) => `badge--outcome-${outcomeSlug(o)}`))
    const defined = [...STYLESHEET.matchAll(/\.(badge--outcome-[\w-]+)/g)].map((m) => m[1])
    const dead = [...new Set(defined)].filter((c) => !emittable.has(c))
    expect(dead, `styles.css styles outcome classes nothing can emit: ${dead.join(', ')}`).toEqual(
      [],
    )
  })

  it('every badge--draft-* rule corresponds to a status the app can produce', () => {
    const emittable = new Set([...DRAFT_STATUSES, null].map((s) => `badge--draft-${s ?? 'none'}`))
    const defined = [...STYLESHEET.matchAll(/\.(badge--draft-[\w-]+)/g)].map((m) => m[1])
    const dead = [...new Set(defined)].filter((c) => !emittable.has(c))
    expect(dead, `styles.css styles draft classes nothing can emit: ${dead.join(', ')}`).toEqual([])
  })
})

describe('the union literals above still match the generated api-types', () => {
  const apiTypes = readFileSync(srcFile('api-types.ts'), 'utf8')

  function unionMembers(typeName: string): string[] {
    const line = apiTypes.match(new RegExp(`export type ${typeName} = ([^\\n]+)`))
    if (line === null) throw new Error(`${typeName} not found in api-types.ts`)
    return [...line[1].matchAll(/'([^']+)'/g)].map((m) => m[1]).sort()
  }

  it('RunOutcome', () => {
    expect(unionMembers('RunOutcome')).toEqual([...RUN_OUTCOMES].sort())
  })

  it('DraftStatus', () => {
    expect(unionMembers('DraftStatus')).toEqual([...DRAFT_STATUSES].sort())
  })
})
