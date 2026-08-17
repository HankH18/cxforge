import { useState } from 'react'

import { setGate } from '../api'
import { describeError } from '../format'

interface GateToggleProps {
  enabled: boolean | null
  onChanged: (enabled: boolean) => void
}

// R11: a single boolean approval gate, all-or-nothing — no per-confidence
// granularity (explicit SPEC non-goal, deliberately not offered here).
export default function GateToggle({ enabled, onChanged }: GateToggleProps) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleToggle() {
    if (enabled === null || busy) return
    const next = !enabled
    setBusy(true)
    setError(null)
    try {
      const result = await setGate(next)
      onChanged(result.enabled)
    } catch (err) {
      setError(describeError(err))
    } finally {
      setBusy(false)
    }
  }

  const state = enabled === null ? 'unknown' : enabled ? 'on' : 'off'

  return (
    <section className="panel gate" aria-label="Approval gate">
      <header className="panel__header">
        <h2 className="panel__title">Approval gate</h2>
        <span className={`badge badge--gate-${state}`}>
          {state === 'unknown' ? '…' : state.toUpperCase()}
        </span>
      </header>

      <label className="gate__control">
        <input
          className="gate__switch"
          type="checkbox"
          role="switch"
          checked={enabled ?? false}
          disabled={enabled === null || busy}
          aria-checked={enabled ?? false}
          onChange={handleToggle}
        />
        <span className="gate__control-label">
          Hold every outbound reply for review (gate ON)
        </span>
      </label>

      <p className={`gate__state gate__state--${state}`}>
        {enabled === null
          ? 'Checking the current gate setting…'
          : enabled
            ? 'Gate is ON — every reply is held as a draft for approve/edit/reject.'
            : 'Gate is OFF — replies send autonomously.'}
      </p>

      {error && (
        <p className="alert alert--error" role="alert">
          Could not update gate: {error}
        </p>
      )}
    </section>
  )
}
