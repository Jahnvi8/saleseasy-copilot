# SalesEasy – a reliable AI sales copilot

Take-home exercise for Futurelab Studios. A small assistant that reads an FMCG field-sales
update, classifies the issue, cites traceable evidence, recommends a next action and decides
whether a human must review it.

**Stack:** React (Vite) · Python 3.12 · FastAPI · Pydantic · Google Gemini (`gemini-3.5-flash`)

- Demo URL: _see submission email_
- Evaluation results: [EVALUATION.md](EVALUATION.md)
- All data is fictional.

---

## How it works

```
 rep text ──► sanitise ──► lookup verified table ──► Gemini (JSON mode, temp 0, retry ×2,
                                                            │   model fallback chain on 503/429)
                                   ┌────────────────────────┘
                                   ▼
                         Pydantic validation ──fail──► safe fallback (needs_human_review=true, meta.degraded=true)
                                   │
                                   ▼
                      deterministic policy layer (R1–R9)  ──►  JSON response + meta
```

**The LLM proposes; code disposes.** Gemini produces a first draft of the JSON. Nothing in it is
trusted until it passes schema validation and the rule layer in `backend/app/policy.py`.
Rules can only make an answer *safer* (raise urgency, force review, strip unauthorised actions,
drop ungrounded quotes); they never lower urgency or remove a review flag the model set.

### Output contract

| field | type / allowed values |
|---|---|
| `issue_category` | `stockout` · `stock_data_conflict` · `competitor_pricing` · `product_quality_safety` · `delivery_logistics` · `prompt_injection` · `insufficient_information` · `other` |
| `urgency` | `low` (next visit) · `medium` (this week) · `high` (today) · `critical` (immediately: safety / legal / major exposure) |
| `evidence[]` | `{ source: "field_update" \| "verified_table", quote, verified }` – a `field_update` quote must be a verbatim substring of the input; a `verified_table` quote must be a matched row. `verified` is always `false` for rep claims. |
| `recommended_next_action` | string, never a commercial action (price change, discount, credit, refund) |
| `missing_information[]` | strings |
| `needs_human_review` | boolean |
| `review_reason` | string or null |

The response also carries a `meta` object (provider, attempts, `degraded`, `injection_detected`,
matched verified rows, the list of policy rules that fired, latency). Full definitions:
[`backend/app/schemas.py`](backend/app/schemas.py).

### Human-review policy

`needs_human_review` is forced to `true` when any of these fire:

| rule | trigger |
|---|---|
| R1 | consumer-health / product-safety words (unwell, sick, injured, contaminated …). Category → `product_quality_safety`, urgency → `critical`, next action replaced with a fixed escalation text so the model can never diagnose. |
| R2 | rep claims a stockout but the verified table shows stock > 0. Category → `stock_data_conflict`. |
| R3 | a commercial action is requested or recommended (price match, discount, credit note, refund…). Any such recommendation is replaced with "forward to pricing team; nothing authorised". |
| R4 | prompt-injection patterns ("ignore instructions", "mark this resolved", "you are now", "developer mode" …). Urgency cannot be `low`. |
| R5 | urgency is `critical`. |
| R6 | urgency is `high` but there is zero verified evidence. |
| R7 | the model's text contains a number that appears neither in the update nor in the verified table (possible fabricated figure). |
| R8 | the model failed twice / returned invalid JSON → fallback. |
| R9 | *(not a review rule)* rep claims lost sales without a figure → the figure is added to `missing_information` instead of being guessed. |

### Treating the update as untrusted input

- The update is wrapped in `<field_update>` tags and the system prompt states it is data, not instructions.
- R4 pattern-matches injection phrases in code, independent of the model.
- Evidence quotes are checked against the raw text; anything not verbatim is dropped.
- Verified rows are injected by code from the table, never taken from the model.
- Input is length-limited (2 000 chars) and rate-limited (20 req / min / IP). Only the length is logged.

---

## Run locally

### 1. Backend

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate          # Windows   |   source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
copy .env.example .env          # then put your GEMINI_API_KEY in .env
uvicorn main:app --reload --port 8000
```

- Swagger UI: http://localhost:8000/docs
- No key? Set `LLM_PROVIDER=mock` in `.env`. The app runs on a clearly-labelled keyword mock.
- `GEMINI_MODEL` accepts one model or a comma-separated chain. On a transient provider error
  (503 overload, 429 quota, timeout) the same request is retried on the next model before the
  app gives up and returns the human-review fallback. Auth and schema errors are not retried.

### 2. Frontend (dev)

```bash
cd frontend
npm install
npm run dev                      # http://localhost:5173, proxies /api → :8000
```

### 3. Single-service build (what the deployment uses)

```bash
cd frontend && npm run build     # writes to backend/static
cd ../backend && uvicorn main:app
# open http://localhost:8000 – FastAPI now serves the React app too
```

### Docker

```bash
docker build -t saleseasy .
docker run -p 8000:8000 -e GEMINI_API_KEY=... saleseasy
```

---

## API

`POST /api/analyze`

```json
{ "text": "Metro Fresh is out of stock on Cola 500ml, manager is upset.", "simulate_failure": false }
```

`simulate_failure: true` forces the provider to raise, to demonstrate the failure path.
Other endpoints: `GET /api/health`, `GET /api/verified-data`, `GET /api/scenarios`.

Sample inputs: [`backend/tests/scenarios.json`](backend/tests/scenarios.json).

---

## Tests

```bash
cd backend
pytest                 # offline, deterministic mock adapter: validation + policy + API wiring
RUN_LIVE=1 pytest -s   # same scenarios through real Gemini (needs GEMINI_API_KEY)
```

Each run writes `backend/tests/results_<provider>.json` with expected-vs-actual for every
scenario. See [EVALUATION.md](EVALUATION.md) for the recorded results, the failures found and
the fixes made.

---

## Known limitations

- Outlet / SKU matching is substring + regex based. Misspellings ("Indranagar") are not matched,
  which degrades to "outlet not recognised" rather than a wrong row, but is still a gap.
- Rules R1–R4 are keyword based. They are deliberately conservative (false positives route to a
  human) but can be evaded by paraphrase; the LLM is the second line of defence, not a guarantee.
- The rate limiter is in-memory per process; it resets on restart and does not share state across
  replicas.
- Only three verified rows exist. A real system would query inventory, outlet master data and
  the pricing policy.
- The mock adapter proves the deterministic layers, not model quality. Model quality is only
  evidenced by the live-run results in EVALUATION.md.
- No persistence, auth or audit log. Human reviewers get a flag, not a queue.
- Gemini safety filters may block a prompt; that path is handled as a provider error (fallback).
- Gemini free-tier models are frequently overloaded (503). The fallback chain mitigates this but
  adds latency (up to ~35 s observed when two models were busy). No request-level timeout budget
  is enforced across the chain yet.

## What I would do for production

1. Replace keyword rules with a small classifier + keep the rules as hard guard-rails.
2. Structured audit log per decision (prompt hash, model version, rules fired, reviewer outcome).
3. Golden-set regression suite run on every prompt or model change; track pass rate over time.
4. Real inventory integration with freshness timestamps, so "conflict" can also mean stale data.
5. Review queue UI with accept / override, feeding back into the eval set.
6. Per-tenant rate limits and auth; Redis-backed limiter.

## AI-tool usage disclosure

Built with Claude Code (Anthropic) as a pair-programming assistant for scaffolding, code
generation and this documentation, under my direction and review. The LLM inside the product
is Google Gemini. All design decisions, the review policy and the test criteria were reviewed
and edited by me.
