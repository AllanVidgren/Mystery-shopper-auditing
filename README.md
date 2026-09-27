# AI Mystery Shopper for legal compliance

**Companies write compliant terms, but customers experience conversations.** This tool tests
those conversations: AI "mystery shoppers" talk to a company's customer-service chat, and
every conversation is checked against consumer protection and data protection rules. Each
finding comes with a quote from the conversation and its legal basis, and a lawyer confirms
it before anyone acts on it.

> **Background.** The idea came out of a legal AI hackathon, where our team's concept for a
> home security company's consumer channels was chosen as the case company's favourite. This
> engine is my continued development of the idea after the event, built with AI-assisted
> coding. All companies, customers and data in this repository are fictional.

## What a finding looks like

*Illustrative example against the bundled demo agent, which has deliberately planted flaws.*

```
! W1  fail   legal   0.92  Was the price information complete and correct?  -> lawyer
      turn 3: "Plans start from 29.90 EUR per month"
      The customer asked for the total cost and contract length. The agent gave only
      a "from" price and no installation fee or contract length.
      basis: Consumer Rights Directive 2011/83/EU Art. 6(1)(e); UCPD Art. 7(4)(c)
      consequence: charges not disclosed cannot be collected (CRD Art. 6(6))
      fix: "It is 39.90 EUR/month plus a 199 EUR installation fee; minimum term 36 months."
```

## How it works

1. **AI customers.** Synthetic personas (a confused 86-year-old, a price-focused buyer, a
   jailbreaker, a social engineer) hold real conversations with a chatbot through its API or
   through a real browser.
2. **A rulebook lawyers maintain.** 20 rules for sales, cancellation and chat, written in a
   YAML file: what to check, whether it is critical, and whether a failure is a **legal** or a
   **policy** breach. Lawyers change the rules without touching code.
3. **Defensible grading.** The AI grades one rule at a time and returns structured data. The
   legal basis is always attached from the rulebook, never written by the AI. Every quote is
   automatically checked against the transcript; a failure without real evidence becomes
   "unclear". A second, independent review runs, and disagreements and critical failures go
   to a lawyer. Every lawyer decision is logged with name and time.
4. **Privacy by design.** A local lawyer sees their own country's conversations; group legal
   sees only patterns that repeat across countries. The API enforces this split. Transcripts
   are deleted after a retention period while findings and quotes are kept.
5. **Low AI cost.** Economy mode uses scripted questions and keyword pre-checks that can only
   pass a rule, never fail it, cutting an audit from about 35 AI requests to about 2–8.

**Tech:** Python · Claude or any OpenAI-compatible model (e.g. Gemini) · structured outputs via
function calling · Playwright · SQLite · Starlette REST API · Docker · 21 offline tests.

---

## For developers

## Quick start

```bash
pip install -r requirements.txt

# fastest start with a free Google Gemini key (economy mode, asks for the key):
bash run_gemini_demo.sh

# or configure any provider yourself:
cp .env.example .env            # add ANTHROPIC_API_KEY; set MS_MODEL to a current Claude model
export $(grep -v '^#' .env | xargs)

# 1. demo against a simulated agent with planted flaws
python -m mystery_shopper run --country FI --persona marcus --scenario website_chat \
    --target demo-sim-flawed --targets config/targets.example.yaml

# 2. real HTTP round trip against the bundled flawed demo bot
python examples/demo_chatbot.py &
python -m mystery_shopper run --country FI --persona max --scenario website_chat \
    --target local-demo-bot --targets config/targets.example.yaml

# 3. a whole campaign, then the group view
python -m mystery_shopper campaign examples/campaign.demo.yaml
python -m mystery_shopper group --include-simulated --include-pending

# 4. API for the dashboard
python -m mystery_shopper serve --targets config/targets.example.yaml --port 8000
```

Tests run offline, with no API key. A scripted LLM stands in for Claude, and local bots stand in for the company:

```bash
python -m unittest discover -s tests -v
```

## Economy mode (free tiers, low cost)

Set `MS_ECONOMY=1`. A typical audit drops from about 35 AI requests to about 2–8:

| Step | Normal | Economy |
|---|---|---|
| Shopper messages | AI writes every message | **Scripted questions** from the rulebook (`probe`, per language) for ordinary personas. Vulnerable, hostile and adversarial personas still improvise with AI, because their behaviour is the test. |
| Clear-cut rules | graded by AI | **Keyword pre-checks** (`precheck`) can *pass* a rule when the required information is plainly in the agent's words, e.g. "14 days" or "I'm an AI assistant". A pre-check can never fail a rule; anything else goes to the AI grader. |
| Grading | one request per rule | **All rules in one request** |
| Second review | every rule | **Only failures and low-confidence results** |
| Conversation length | scenario maximum | at most 6 shopper messages |

Against a real chatbot, a scripted run can need only one or two AI requests in total. Against the simulated demo agent, the agent's own replies also use AI.

## Choosing the AI provider

The default is Claude (`MS_PROVIDER=anthropic`, `ANTHROPIC_API_KEY`). To use any provider with an OpenAI-compatible API (OpenAI, Google Gemini, Mistral, Groq, OpenRouter, or local Ollama), set `MS_PROVIDER=openai`, `MS_BASE_URL`, `MS_LLM_API_KEY` and `MS_MODEL`. `.env.example` lists the base URLs.

The model must support function calling, and grading needs a strong model. Before trusting a new model, re-run the accuracy check against your lawyers' grades.

## How a run works

```
persona + scenario ──► Shopper (Claude) ◄──► Target (simulated | http | browser)
                                   │
                               transcript
                                   ▼
            Grader: per rule ─ evidence guard ─ second review ─ lawyer flag
                                   ▼
          SQLite: runs · findings · append-only lawyer decisions
                                   ▼
          API: local view (one country, full detail) · group view (patterns)
```

## Configuration

| File | What it holds |
|---|---|
| `config/rulebook.yaml` | Rules S1–S7 (sales), C1–C7 (cancellation), W1–W7 (chat). Each rule has a check, critical flag, applicable customer types, legal or policy basis, and consequence. A rule with a `law` basis produces a **legal breach**; a rule with only `policy` bases produces a **policy breach**. |
| `config/personas.yaml` | Synthetic shoppers. Traits (`vulnerable`, `hostile`, `adversarial`) switch on the rules that need them. |
| `config/scenarios.yaml` | Goals and maximum turns per channel. |
| `config/targets.yaml` | Your channels. Start from `targets.example.yaml`. |

### Targets
- `simulated`: Claude plays the company agent, with facts and "habits" (planted flaws). Runs are stored with `simulated=1` and are excluded from group numbers unless you pass `include_simulated`.
- `http`: any chatbot with a JSON API. Configure the request body template (`{{message}}`, `{{session_id}}`), `reply_path` (a dot path into the response), an optional `start` call and `session_path`, and rate limiting (`min_interval_s`). Secrets come from the environment, e.g. `${CHAT_TOKEN}`. Requests carry `X-Mystery-Shopper: 1`.
- `browser`: a website chat widget driven by Playwright. Configure the steps to open the widget, an optional iframe, the input field, the send button and the bot-message selectors.

Real targets are **refused** unless they have `authorized: true` and `authorized_by`.

## API

Send `X-MS-Role: local:FI` or `X-MS-Role: group`, plus `Authorization: Bearer $MS_API_TOKEN` if a token is set. In production, map these to your SSO claims.

| Method | Path | Who | What |
|---|---|---|---|
| GET | `/api/library` | both | rules, personas, scenarios, targets |
| POST | `/api/runs` | local (own country) / group | start a run: `{country, persona, scenario, target, language?, test_account?}`; returns 202 and a run id |
| GET | `/api/runs` | local | runs for your country |
| GET | `/api/runs/{id}` | local | transcript and findings |
| GET | `/api/countries/{cc}/findings` | local | findings, with those waiting for review first |
| POST | `/api/findings/{id}/decision` | local | `{action: confirm \| overturn \| escalate \| note \| fixed \| verified, by, note}` |
| GET | `/api/group/overview` | group | KPIs, systemic patterns, heatmap, country cards, escalations |
| POST | `/api/exposure` | both | `{monthly_volume: {FI: {chat: 20000}}, value_per_case: {W1: 199}}`; returns the estimated monthly exposure |

**Group view rules:**
- Only failures a lawyer has confirmed count as failures.
- A rule becomes a *systemic pattern* when it fails in 2 or more countries.
- Individual findings appear only when a local lawyer escalates them.
- No transcripts and no team labels.

## Legal and privacy guardrails built in
- **Synthetic shoppers only.** Cancellation tests use test accounts the company provides, never real customer data.
- **Process, not person.** Results carry a team or script-version label, never an agent's name. There is no emotion or voice analysis (see AI Act Art. 5(1)(f)), and nothing feeds into employee evaluation (see AI Act Annex III(4)).
- **Attributable decisions.** Every lawyer decision is logged with a name and timestamp, and the log is append-only.
- **Retention.** `python -m mystery_shopper purge --days 30` removes full transcripts older than 30 days and keeps the findings with their quoted evidence.
- **Authorisation.** Only authorised channels can be tested, and test traffic is labelled.

Before production: run a DPIA, inform staff, and in Finland complete co-operation negotiations for technical monitoring if you add real-conversation analysis.

## Roadmap
- **Voice:** a telephony adapter (e.g. Twilio with speech-to-text and text-to-speech) implementing the same `Target` interface (`start`, `send`, `close`).
- **Real-conversation analysis:** pseudonymised samples graded by the same rulebook and shown at team level.
- **National rule layers:** per-country rule overrides for local law.
- **Scheduled campaigns and re-testing after fixes:** run with cron or a job runner, e.g. `python -m mystery_shopper campaign campaigns/fi-weekly.yaml`.
