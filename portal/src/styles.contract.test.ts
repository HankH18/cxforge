// Every status class the components can construct must have a rule in the stylesheet,
// and every status rule in the stylesheet must be one the components can construct.
//
// This exists because of a real defect that shipped and that nothing else could catch.
// `outcomeSlug` turns 'gated_sent' into a class ending `-gated-sent`, but the stylesheet
// defined `.badge--outcome-sent-after-review` — built from the *display label* instead of
// the *slug*. So the fully-autonomous send rendered green and the human-approved send —
// the whole payoff of the review gate — rendered as unstyled grey. The exact inverse of
// the emphasis the outcome column exists to create.
//
// Every existing display test asserts on text content, and the label 'sent after review'
// was correct, so none of them could see it. Only a class-level assertion can. The union
// members below are read from the generated api-types, so a new outcome or draft status
// arriving from the backend fails here until someone styles it.
//
// Updated for the docs/STYLE_GUIDE.md design system, which replaced `badge--outcome-*` /
// `badge--draft-*` / `badge--gate-*` with one flat `pill--{slug}` family plus
// `gate-flag--{state}`. Two things changed beyond the names, both making this stricter:
//
//   1. Matching is against parsed selector lists, not raw file text, so a class named
//      only inside a comment or a commented-out rule no longer satisfies it.
//   2. Each pill class must draw its background from the status token family the guide's
//      §4 table assigns it. That is what pins the *inverse* of the original defect: a
//      rule can exist for `.pill--approved` and still be wrong if it paints a
//      human-approved send in the machine's blue. `approved`/`gated-sent` must resolve to
//      --status-sent-*, `auto-sent` to --status-auto-*, and those cannot be the same.

import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

import type { DraftStatus, RunOutcome } from './api-types'
import { draftStatusSlug, outcomeSlug } from './format'

// Resolved from the vitest root (portal/), not from `import.meta.url` — under the jsdom
// environment that is an http: URL, and `fileURLToPath` rejects it with
// "The URL must be of scheme file".
const srcFile = (...parts: string[]): string => resolve(process.cwd(), 'src', ...parts)

// The shipped stylesheet is app.css plus the tokens it @imports; a rule that moved
// between the two files should still satisfy this contract.
const STYLESHEET = [
  readFileSync(srcFile('styles', 'app.css'), 'utf8'),
  readFileSync(srcFile('styles', 'tokens.css'), 'utf8'),
].join('\n')

// Comments are stripped first: a class mentioned in prose, or a rule commented out, must
// not be able to satisfy "this class is styled".
const CSS = STYLESHEET.replace(/\/\*[\s\S]*?\*\//g, '')

interface Rule {
  selectors: string[]
  declarations: string
}

/** Flat parse of `selector-list { declarations }`. Good enough because every rule this
 *  test cares about is declared at the top level of app.css. */
const RULES: Rule[] = [...CSS.matchAll(/([^{}]+)\{([^{}]*)\}/g)].map((match) => ({
  selectors: match[1].split(',').map((selector) => selector.trim()),
  declarations: match[2],
}))

const ALL_SELECTORS: string[] = RULES.flatMap((rule) => rule.selectors)

/** Does any rule's selector list use exactly this class? */
function hasRuleFor(className: string): boolean {
  // Match `.class` only at a selector boundary, so `.pill--auto-sent` is never
  // satisfied by `.pill--auto-sent-something`.
  const pattern = new RegExp(`\\.${className.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}(?![\\w-])`)
  return ALL_SELECTORS.some((selector) => pattern.test(selector))
}

/** Declarations of the rules whose selector is exactly `.className` (no descendant, no
 *  pseudo-element) — i.e. the ones that paint the element itself. */
function declarationsFor(className: string): string {
  return RULES.filter((rule) => rule.selectors.includes(`.${className}`))
    .map((rule) => rule.declarations)
    .join(' ')
}

// Kept in sync with api-types.ts by the assertions in the last test below.
const RUN_OUTCOMES: RunOutcome[] = ['auto_sent', 'gated_sent', 'rejected', 'escalated', 'off_topic']
const DRAFT_STATUSES: DraftStatus[] = ['pending', 'approved', 'rejected', 'auto_sent']
const GATE_STATES = ['on', 'off', 'unknown']

// docs/STYLE_GUIDE.md §4: which status token family each pill slug must paint itself from.
// `gated_sent` (a human released it) shares --status-sent-* with `approved`; `auto_sent`
// (nobody read it) is --status-auto-*. Those being different families is the whole point.
const PILL_TOKEN_FAMILY: Readonly<Record<string, string>> = {
  pending: 'pending',
  approved: 'sent',
  'gated-sent': 'sent',
  'auto-sent': 'auto',
  rejected: 'rejected',
  escalated: 'pending',
  awaiting: 'pending',
  'off-topic': 'none',
  none: 'none',
}

/** Every pill class the components can emit, from the same helpers they call. */
const EMITTABLE_PILLS: string[] = [
  ...[...RUN_OUTCOMES, null].map((outcome) => `pill--${outcomeSlug(outcome)}`),
  ...[...DRAFT_STATUSES, null].map((status) => `pill--${draftStatusSlug(status)}`),
]

describe('every status class the app can emit is styled', () => {
  it.each([...RUN_OUTCOMES, null])('outcome %s', (outcome) => {
    const slug = outcomeSlug(outcome)
    const className = `pill--${slug}`
    expect(hasRuleFor(className), `${className} is emitted but the stylesheet has no rule for it`)
      .toBe(true)
    expect(
      declarationsFor(className),
      `${className} must paint itself from the --status-${PILL_TOKEN_FAMILY[slug]}-* family`,
    ).toContain(`var(--status-${PILL_TOKEN_FAMILY[slug]}-soft)`)
  })

  it.each([...DRAFT_STATUSES, null])('draft status %s', (status) => {
    const slug = draftStatusSlug(status)
    const className = `pill--${slug}`
    expect(hasRuleFor(className), `${className} is emitted but the stylesheet has no rule for it`)
      .toBe(true)
    expect(
      declarationsFor(className),
      `${className} must paint itself from the --status-${PILL_TOKEN_FAMILY[slug]}-* family`,
    ).toContain(`var(--status-${PILL_TOKEN_FAMILY[slug]}-soft)`)
  })

  it.each(GATE_STATES)('gate state %s', (state) => {
    const className = `gate-flag--${state}`
    expect(hasRuleFor(className), `${className} is emitted but the stylesheet has no rule for it`)
      .toBe(true)
    // §4: the gate switch and its flag are never red when off — an off gate is a
    // configuration, not a fault.
    expect(
      declarationsFor(className),
      `${className} must not use a rejected/danger color`,
    ).not.toMatch(/rejected|danger/)
  })
})

describe('no dead status selectors', () => {
  it('every pill--* rule corresponds to a slug the app can produce', () => {
    const emittable = new Set(EMITTABLE_PILLS)
    const defined = ALL_SELECTORS.flatMap((selector) => [
      ...selector.matchAll(/\.(pill--[\w-]+)/g),
    ]).map((match) => match[1])
    const dead = [...new Set(defined)].filter((className) => !emittable.has(className))
    expect(
      dead,
      `the stylesheet styles pill classes nothing can emit: ${dead.join(', ')}`,
    ).toEqual([])
  })

  it('every gate-flag--* rule corresponds to a state the app can produce', () => {
    const emittable = new Set(GATE_STATES.map((state) => `gate-flag--${state}`))
    const defined = ALL_SELECTORS.flatMap((selector) => [
      ...selector.matchAll(/\.(gate-flag--[\w-]+)/g),
    ]).map((match) => match[1])
    const dead = [...new Set(defined)].filter((className) => !emittable.has(className))
    expect(
      dead,
      `the stylesheet styles gate classes nothing can emit: ${dead.join(', ')}`,
    ).toEqual([])
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
