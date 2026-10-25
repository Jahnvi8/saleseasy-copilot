import { useEffect, useState } from 'react'

const URGENCY_CLASS = { low: 'u-low', medium: 'u-medium', high: 'u-high', critical: 'u-critical' }

export default function App() {
  const [text, setText] = useState('')
  const [scenarios, setScenarios] = useState([])
  const [health, setHealth] = useState(null)
  const [simulateFailure, setSimulateFailure] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [data, setData] = useState(null)
  const [showRaw, setShowRaw] = useState(false)

  // ngOnInit equivalent: load helper data once.
  useEffect(() => {
    fetch('/api/scenarios').then((r) => r.json()).then(setScenarios).catch(() => {})
    fetch('/api/health').then((r) => r.json()).then(setHealth).catch(() => {})
  }, [])

  const maxChars = health?.max_input_chars ?? 2000

  async function analyze(e) {
    e?.preventDefault()
    if (!text.trim() || loading) return
    setLoading(true)
    setError(null)
    setData(null)
    try {
      const res = await fetch('/api/analyze', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, simulate_failure: simulateFailure }),
      })
      const body = await res.json()
      if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`)
      setData(body)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  function loadScenario(s) {
    setText(s.text)
    setSimulateFailure(Boolean(s.simulate_failure))
    setData(null)
    setError(null)
  }

  const r = data?.result
  const m = data?.meta

  return (
    <div className="page">
      <header>
        <h1>SalesEasy <span>field-update copilot</span></h1>
        <p className="sub">
          Paste a rep's update. Get a triage card that separates verified facts from claims and tells you
          whether a human needs to step in.
          {health && (
            <span className="pill">
              LLM: {health.provider}{health.model ? ` / ${health.model}` : ' (no API key: mock mode)'}
            </span>
          )}
        </p>
      </header>

      <section className="card">
        <h2>Sample scenarios</h2>
        <div className="chips">
          {scenarios.map((s) => (
            <button key={s.id} className="chip" onClick={() => loadScenario(s)} title={s.text}>
              {s.id} · {s.title}
            </button>
          ))}
        </div>
      </section>

      <form className="card" onSubmit={analyze}>
        <label htmlFor="update">Field update (untrusted input)</label>
        <textarea
          id="update"
          rows={5}
          maxLength={maxChars}
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="e.g. Metro Fresh is out of stock on Cola 500ml, manager is upset..."
        />
        <div className="row">
          <span className="count">{text.length} / {maxChars}</span>
          <label className="check">
            <input type="checkbox" checked={simulateFailure} onChange={(e) => setSimulateFailure(e.target.checked)} />
            Simulate model/API failure
          </label>
          <button type="submit" className="primary" disabled={loading || !text.trim()}>
            {loading ? 'Analysing…' : 'Analyse'}
          </button>
        </div>
      </form>

      {error && <div className="card error">Request failed: {error}</div>}

      {r && (
        <section className="card result">
          {m.degraded && (
            <div className="banner warn">
              Model unavailable. A conservative fallback was returned and routed to a human. ({m.llm_error})
            </div>
          )}
          {r.needs_human_review && (
            <div className="banner review">
              <strong>Needs human review</strong>
              <div>{r.review_reason}</div>
            </div>
          )}

          <div className="badges">
            <span className="badge cat">{r.issue_category.replaceAll('_', ' ')}</span>
            <span className={`badge ${URGENCY_CLASS[r.urgency]}`}>urgency: {r.urgency}</span>
            {m.injection_detected && <span className="badge inj">injection detected</span>}
          </div>

          <h3>Recommended next action</h3>
          <p className="action">{r.recommended_next_action}</p>

          <h3>Evidence</h3>
          {r.evidence.length === 0 && <p className="muted">No evidence extracted.</p>}
          <ul className="evidence">
            {r.evidence.map((e, i) => (
              <li key={i}>
                <span className={`tag ${e.verified ? 'verified' : 'claim'}`}>
                  {e.verified ? 'VERIFIED · system table' : 'CLAIM · rep update'}
                </span>
                <q>{e.quote}</q>
              </li>
            ))}
          </ul>

          <h3>Missing information</h3>
          {r.missing_information.length === 0 ? (
            <p className="muted">Nothing flagged.</p>
          ) : (
            <ul>{r.missing_information.map((x, i) => <li key={i}>{x}</li>)}</ul>
          )}

          <details>
            <summary>Why? Policy rules that fired, matched data, raw JSON</summary>
            <h4>Verified rows matched</h4>
            {m.verified_matches.length === 0 ? (
              <p className="muted">None. Outlet or SKU not recognised.</p>
            ) : (
              <ul>
                {m.verified_matches.map((v, i) => (
                  <li key={i}>{v.outlet} · {v.sku} · {v.system_stock_units} units</li>
                ))}
              </ul>
            )}
            <h4>Policy overrides</h4>
            {m.policy_overrides.length === 0 ? (
              <p className="muted">None. Model output accepted as-is.</p>
            ) : (
              <ul>{m.policy_overrides.map((o, i) => <li key={i}><code>{o}</code></li>)}</ul>
            )}
            <p className="muted">
              provider {m.provider} · attempts {m.attempts} · {m.latency_ms} ms
            </p>
            <button type="button" className="link" onClick={() => setShowRaw(!showRaw)}>
              {showRaw ? 'Hide' : 'Show'} raw JSON
            </button>
            {showRaw && <pre>{JSON.stringify(data, null, 2)}</pre>}
          </details>
        </section>
      )}

      <footer>Fictional data only. Not a production system. Field updates are treated as untrusted input.</footer>
    </div>
  )
}
