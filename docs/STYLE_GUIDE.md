# Othram Support Review Portal — Style Guide

> ## Provenance and status
>
> **Source:** the owner's Claude Design project **“Admin Portal Style Guide”**, supplied
> out of band on **2026-08-17**. Committed here because `portal/src/styles/tokens.css`,
> `portal/src/styles/app.css`, `portal/src/format.ts`, `portal/src/App.tsx`,
> `portal/src/main.tsx` and `portal/src/styles.contract.test.ts` cite it by section as
> the authority for their design decisions, and until now those citations pointed at a
> file that was not in the tree.
>
> **`portal/src/styles/` implements it** — `tokens.css` is §2, `app.css` is §§1–7.
> The text below §0 is the guide **verbatim, as received**; it is an input, not a
> record of what shipped. Where the implementation departs from it, §0 says so and why.

## 0. Deviations — what shipped is not exactly what is written below

Seven, all deliberate. None of them changes a visible string, a `role`, an `aria-label`
or the DOM — the guide's hard constraints hold. Only (1) changes what a reader sees, and
only in dark mode, which is opt-in and has no switcher.

1. **§2's dark block never remaps `--status-none` / `--status-none-soft`**, so in dark
   mode the "no draft" and "off-topic" pills kept a near-white background under muted
   ink and measured **2.03:1** against §7's own "Contrast ≥ 4.5:1 … for pill ink".
   Two tokens were added to the dark block; re-measured **5.93:1** in a real browser
   (canvas-resolved sRGB), light mode unchanged. §2 is an input and §7 is an acceptance
   criterion, so the criterion won. The reasoning, and the 2.03:1 measurement against the
   8.79–9.01:1 every other dark pill scores, are in the comment at
   `portal/src/styles/tokens.css:96`.

2. **§3 and §6 conflict on the `<h1>`.** §3's type table gives it the 28px `display`
   role; §6's file map makes it the brand mark inside the 56px app bar. The brand mark
   shipped, so the `display` type role is **unused** (`app.css:128`).

3. **§4's `draft_status` label column is wrong.** It lists `approved` → "sent
   (approved)" and `auto_sent` → "auto-sent". `format.ts` renders **"approved"** and
   **"sent, no review"**. §4's own sentence — *"Labels stay exactly as `Feed.tsx`
   renders them today"* — is what shipped; the table's **token/slug** column is what was
   implemented. No visible string changed.

4. **§4's error state hardcodes `1px solid oklch(0.90 0.04 25)`**, which §7 forbids
   ("No literal color outside `tokens.css`"). `var(--danger-border)` shipped
   (`app.css`, `.alert--error`).

5. **§4's optional 44×4px confidence track was skipped.** It needs a nested element
   inside the cell — a DOM change, which the guide's own hard constraints forbid. The
   `formatPercent` mono/tabular right-aligned cell shipped without it.

6. **§4's "sticky top" `thead`** required `.table-scroll` to become the scroll region —
   sticky resolves against the nearest scrolling ancestor, so without that wrapper the
   header pins to nothing. It is also what satisfies §7's "50 rows scrolls without layout
   shift". Not a departure from the spec so much as an addition the spec did not name.

7. **Two premises in the text were already false when it arrived.** "The portal
   currently renders unstyled semantic HTML" — Track D had shipped 488 lines of CSS
   (ADR-012), which `app.css` supersedes. "All 5 tests must still pass" — there were
   **36** tests across 5 files; 36 still pass.

---

<!-- Everything below this line is the guide exactly as received. Do not edit it to
     match the implementation — record the difference in §0 instead. -->

# Othram Support Review Portal — Style Guide (as received)

Implementation spec for `portal/` (React 19 + Vite + TS). The portal currently
renders unstyled semantic HTML; this document is the complete visual layer to
add on top of it.

**Hard constraints**

- No new runtime dependencies. Plain CSS with custom properties. No Tailwind,
  no CSS-in-JS, no component or icon library.
- Do not change component structure, `aria-label`, `role`, label text, option
  values, or visible strings. `portal/src/App.test.tsx` and
  `App.flows.test.tsx` query by accessible role and text; all 5 tests must
  still pass (`npm test`), and `npm run build` must stay green.
- Components gain `className` attributes and the two shell wrappers described
  in §6. Nothing else in the TSX changes.
- Only `tokens.css` may declare a raw color. Everything else uses `var(--…)`.

## 1. Principles

1. **The draft is the product.** Reply text gets the largest type, highest
   contrast, most room. Chrome recedes.
2. **Color is a status channel.** Green, amber, red, blue mean sent, pending,
   rejected, automatic. Nothing decorative may use them.
3. **Irreversible actions look it.** Approve sends text to a customer. It is
   the only solid-filled button on screen.
4. **Machine values are monospaced.** Ticket IDs, confidence, latency,
   timestamps — tabular figures so columns scan.

## 2. `portal/src/styles/tokens.css`

```css
:root {
  /* surfaces — cool neutral */
  --bg:              oklch(0.975 0.004 240);
  --surface:         #ffffff;
  --surface-sunken:  oklch(0.962 0.005 240);
  --surface-hover:   oklch(0.968 0.005 240);
  --border:          oklch(0.905 0.008 240);
  --border-strong:   oklch(0.860 0.010 240);
  --border-subtle:   oklch(0.940 0.006 240);

  /* ink */
  --text:            oklch(0.24 0.015 250);
  --text-muted:      oklch(0.50 0.012 250);
  --text-faint:      oklch(0.62 0.012 250);
  --text-inverse:    oklch(0.98 0.003 240);

  /* accent — signal green, hue 152 */
  --accent:          oklch(0.48 0.10 152);
  --accent-hover:    oklch(0.42 0.10 152);
  --accent-ink:      oklch(0.40 0.09 152);
  --accent-soft:     oklch(0.955 0.028 152);
  --accent-ring:     oklch(0.48 0.10 152 / 0.16);

  /* status */
  --status-pending:       oklch(0.62 0.13 75);
  --status-pending-ink:   oklch(0.44 0.10 75);
  --status-pending-soft:  oklch(0.965 0.035 85);
  --status-sent:          oklch(0.48 0.10 152);
  --status-sent-ink:      oklch(0.40 0.09 152);
  --status-sent-soft:     oklch(0.955 0.028 152);
  --status-auto:          oklch(0.52 0.10 245);
  --status-auto-ink:      oklch(0.44 0.09 245);
  --status-auto-soft:     oklch(0.960 0.025 245);
  --status-rejected:      oklch(0.54 0.16 25);
  --status-rejected-ink:  oklch(0.46 0.14 25);
  --status-rejected-soft: oklch(0.960 0.028 25);
  --status-none:          oklch(0.72 0.010 250);
  --status-none-soft:     oklch(0.955 0.005 240);
  --danger-border:        oklch(0.88 0.05 25);
  --danger-ink:           oklch(0.50 0.16 25);

  /* type */
  --font-sans: 'Instrument Sans', ui-sans-serif, system-ui, -apple-system, sans-serif;
  --font-mono: 'IBM Plex Mono', ui-monospace, SFMono-Regular, monospace;

  /* space / shape / motion */
  --sp-1: 4px;  --sp-2: 8px;  --sp-3: 12px; --sp-4: 16px;
  --sp-5: 24px; --sp-6: 32px; --sp-7: 48px; --sp-8: 64px;
  --r-control: 6px;
  --r-card: 10px;
  --r-pill: 999px;
  --shadow-card: 0 1px 2px oklch(0.24 0.015 250 / 0.04);
  --shadow-drawer: -16px 0 48px oklch(0.24 0.015 250 / 0.05);
  --dur-fast: 120ms;
  --dur-panel: 180ms;
  --ease: cubic-bezier(0.2, 0, 0, 1);
}

[data-theme='dark'] {
  --bg:             oklch(0.18 0.014 250);
  --surface:        oklch(0.22 0.016 250);
  --surface-sunken: oklch(0.25 0.018 250);
  --surface-hover:  oklch(0.27 0.018 250);
  --border:         oklch(0.32 0.020 250);
  --border-strong:  oklch(0.40 0.022 250);
  --border-subtle:  oklch(0.28 0.018 250);
  --text:           oklch(0.96 0.004 240);
  --text-muted:     oklch(0.74 0.012 240);
  --text-faint:     oklch(0.62 0.014 240);
  --text-inverse:   oklch(0.18 0.020 152);
  --accent:         oklch(0.62 0.12 152);
  --accent-hover:   oklch(0.68 0.12 152);
  --accent-ink:     oklch(0.86 0.08 152);
  --accent-soft:    oklch(0.30 0.05 152);
  --accent-ring:    oklch(0.62 0.12 152 / 0.24);
  --status-pending-ink:   oklch(0.88 0.09 85);
  --status-pending-soft:  oklch(0.32 0.06 75);
  --status-sent-ink:      oklch(0.86 0.08 152);
  --status-sent-soft:     oklch(0.30 0.05 152);
  --status-auto-ink:      oklch(0.86 0.06 245);
  --status-auto-soft:     oklch(0.30 0.05 245);
  --status-rejected-ink:  oklch(0.86 0.08 25);
  --status-rejected-soft: oklch(0.30 0.06 25);
  --danger-border:  oklch(0.42 0.08 25);
  --danger-ink:     oklch(0.80 0.11 25);
}
```

Ship light first. Dark is opt-in via `data-theme="dark"` on `<html>`; no theme
switcher UI is in scope.

## 3. Typography

Load in `portal/index.html` `<head>`:

```html
<link rel="preconnect" href="https://fonts.googleapis.com" />
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
<link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet" />
```

| Role | Spec | Used by |
|---|---|---|
| display | 28px / 600 / -0.025em / 1.15 | `<h1>` page title |
| title | 20px / 600 / -0.015em / 1.25 | `<h2>` section + drawer heading |
| subtitle | 16px / 600 | `<h3>` (escalations, sent/rejected heading) |
| body | 14px / 400 / 1.6 | table cells, prose, labels |
| editor | 15px / 400 / 1.65 | the draft `<textarea>` and sent `<pre>` |
| small | 13px / 400 / 1.5 | helper text, gate explanation |
| label | 11px / 500 / mono / 0.14em / uppercase | `<dt>`, column headers, metric captions |
| mono | 13px / 500 / mono / tabular-nums | ticket id, confidence, latency, timestamps |

Base `font-size: 14px` on `body`. Prose max width `66ch`; set
`text-wrap: pretty` on paragraphs.

## 4. Component rules

### Buttons

Height 34px (26–28px inside table rows), radius `--r-control`, 13px/500,
`transition: background var(--dur-fast) var(--ease)`.

- **Primary — Approve only.** `background: var(--accent)`, white text, border
  same as background. Hover `--accent-hover`.
- **Neutral — Close, View, Review.** `--surface` background, `--border`,
  `--text`. Hover `--surface-hover` + `--border-strong`.
- **Danger — Reject.** Outlined: `--surface` background,
  `border: 1px solid var(--danger-border)`, `color: var(--danger-ink)`. Hover
  fills `--status-rejected-soft`. Never solid red — one solid button per view.
- **Disabled (`busy`).** `--surface-sunken` background, `--text-faint`,
  `cursor: not-allowed`, no hover change.
- Focus-visible on every interactive element:
  `outline: 2px solid var(--accent); outline-offset: 2px`.

### Status pills

Soft background + colored ink + 6px dot, 12px/500, padding `5px 9px`, pill
radius. Map from `draft_status` in one lookup object — never from `outcome`,
never from color alone. Labels stay exactly as `Feed.tsx` renders them today:

| `draft_status` | Label | Token prefix |
|---|---|---|
| `pending` | pending review | `--status-pending` |
| `approved` | sent (approved) | `--status-sent` |
| `auto_sent` | auto-sent | `--status-auto` |
| `rejected` | rejected | `--status-rejected` |
| `null` | no draft | `--status-none` |

### Confidence

`formatPercent` output in mono with `font-variant-numeric: tabular-nums`,
right-aligned, followed by an optional 44×4px neutral track
(`--border` background, `--text-muted` fill). The track is never colored by
threshold — confidence is information, not a verdict. `null` → `—`.

### Gate switch

Keep `<input type="checkbox" role="switch">`. Visually: 40×22 track, 16px
knob, `appearance: none`, `:checked` → `--accent`, unchecked →
`--border-strong`. Never red when off. Knob transition 120ms. Explanation text
sits below at `small` size in `--text-muted`.

### Feed table

- Semantic `<table>` retained. `border-collapse: collapse`, full width.
- `thead th`: `label` type, `--surface-sunken` background, sticky top,
  bottom `1px solid var(--border)`, `text-align: left` (right for Confidence).
- `td`: padding `13px 18px` (compact variant `8px 18px`), bottom
  `1px solid var(--border-subtle)`, `vertical-align: middle`.
- Row hover: `--surface-hover`.
- Selected row (`[aria-current='true']`): `background: var(--accent-soft)` +
  `box-shadow: inset 3px 0 0 var(--accent)`.
- Route and escalation reason truncate with ellipsis; full value in `title`.
- Trace renders as a text link with a trailing `↗`.
- **Never animate rows on poll.** No fade/slide on the 5s refresh — rows must
  not move under a reviewer mid-read.

### Draft detail (drawer)

400px fixed-width right panel, `--surface`, left `1px solid var(--border)`,
`--shadow-drawer`. Enters with a 180ms translateX; respect
`prefers-reduced-motion: reduce` (no transform, opacity only).

Layout order: ticket heading + meta line, Close button top-right, the `<dl>`
as a two-column `auto 1fr` grid (label type on `dt`, body on `dd`), then the
editor block, then actions.

- `<textarea>`: full width, `editor` type, `--r-control`… use `8px`, padding
  14px, `resize: vertical`, min 9 rows.
- Focus: `border-color: var(--accent)` + `box-shadow: 0 0 0 3px var(--accent-ring)`.
- The dirty notice ("Edited from the original draft…") renders in
  `--status-pending-ink` with a 6px pending dot. Keep the string verbatim.
- Actions row: Approve then Reject, `gap: 10px`, Approve 36px tall.
- Non-pending state: `<pre>` gets `--surface-sunken`, 14px mono, `1px solid
  var(--border)`, padding 16px, `white-space: pre-wrap`.

### Metrics

The `<dl>` becomes a 3-up card grid (`repeat(auto-fit, minmax(200px, 1fr))`,
`gap: 12px`): `dt` = label type, `dd` = 30px/600 tabular figure. Escalations by
reason stay a list — reason in body type, count in mono, right-aligned, one
row per line with a `--border-subtle` divider. The caveat paragraph about
multi-reason counting stays verbatim at `small` / `--text-muted`.

### Feedback states

- **Error (`role="alert"`)**: `--status-rejected-soft` background,
  `1px solid oklch(0.90 0.04 25)`, radius 8px, padding `12px 14px`; bold
  prefix in `--status-rejected-ink`, detail in body type. Polling errors never
  blank data already on screen.
- **Loading**: `--surface-sunken` strip, neutral dot, `--text-muted` text.
- **Empty**: dashed `--border-strong` box, 36px vertical padding, centered
  title (14px/500) + one line of `--text-muted` context.

## 5. Layout shell

```
.app          min-height:100vh; background:var(--bg); color:var(--text)
.app-bar      56px, sticky, --surface, bottom border; brand left,
              GateToggle right (label + switch + ON/OFF in mono)
.app-grid     display:grid; grid-template-columns: minmax(0,1fr) 400px;
              align-items:start
.app-main     padding:20px; display:grid; gap:16px  (metrics, then feed)
```

Below 1080px the drawer becomes a full-width panel stacked under the feed
(`grid-template-columns: 1fr`). Below 720px the feed table drops the Route and
Received columns via `@media` — no horizontal scroll.

Cards use borders, not shadows. `--shadow-drawer` is the only shadow above the
1px card shadow.

## 6. File map

| File | Adds | Notes |
|---|---|---|
| `portal/index.html` | font links | nothing else |
| `portal/src/main.tsx` | `import './styles/app.css'` | single stylesheet entry |
| `portal/src/styles/tokens.css` | new | §2 verbatim |
| `portal/src/styles/app.css` | new | `@import './tokens.css'`, reset, shell, components |
| `portal/src/App.tsx` | `.app`, `.app-bar`, `.app-grid`, `.app-main` wrappers | move `<GateToggle>` into the bar; `<h1>` becomes the brand mark; keep `<main>` as `.app` root |
| `components/GateToggle.tsx` | `.gate` | CSS-only switch on the existing input |
| `components/MetricsPanel.tsx` | `.metrics`, `.metric-card` | `dl` → card grid |
| `components/Feed.tsx` | `.feed`, `.feed-table`, `.pill.pill--{status}` | status→class via lookup map |
| `components/DraftDetail.tsx` | `.drawer`, `.drawer-meta`, `.editor` | keep `section[aria-label="Draft detail"]` |

## 7. Acceptance

- `npm run build` and `npm test` pass unchanged.
- No literal color outside `tokens.css`.
- Keyboard: every action reachable, visible focus ring on all of them.
- Contrast ≥ 4.5:1 for body text and pill ink on their own backgrounds.
- `prefers-reduced-motion: reduce` disables the drawer transform.
- Feed at 50 rows scrolls without layout shift on a poll tick.
