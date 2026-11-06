# Settle

_Chat to paid, helpers included._

**Paste a client chat. The agent drafts the invoice, you approve it, PayPal collects it, the AI chases late payers, and your helpers get their share the moment the client pays in full.**

Built for the PayPal AI Hackathon 2026 with PayPal Sandbox, an LLM, AG Grid and AG Charts.

- **Live app:** https://settle-rysn.onrender.com/dashboard (PayPal Sandbox, needs the admin key, see [For judges](#for-judges))
- **Judge Mode:** https://settle-rysn.onrender.com/dashboard?judge (guided replay on in-browser demo data, no key, nothing reaches PayPal)
- **Video:** see the Devpost submission

---

## The problem

Freelancers and small agencies agree on work in WhatsApp chats, DMs and email threads. Turning that into an invoice, chasing payment, and then splitting the money with the people who helped is manual, awkward, and easy to forget. Clients pay late, and helpers wait even longer.

## What it does

```
 client chat ──▶ AI draft ──▶ code checks ──▶ you approve ──▶ PayPal invoice ──▶ AI nudges ──▶ paid in full ──▶ helpers paid
                 (LLM)        (validate)      (one tap /      (Invoicing v2)     (remind)      (verified)       (Payouts)
                                               approval link)
```

1. **AI drafts from the chat.** Paste the client's message plus an optional private note (`split 20% to ada@example.com`). The LLM extracts client, email, currency, due date, line items and helper splits as JSON.
2. **Code checks the AI.** `agent.validate()` rejects bad emails, unsupported currencies, past due dates, zero or negative amounts, and splits over 100%. Anything wrong is saved as **Needs review** and never sent.
3. **A human approves.** Nothing goes out without the owner. Approval works on the dashboard or through a single-use, 48-hour approval link (only a SHA-256 hash of the token is stored). The API returns the link, so you can forward it to the owner by email or chat. A GET never sends, only a POST does, so link scanners can't trigger it.
4. **PayPal collects.** The approved draft becomes a real PayPal invoice (Invoicing API v2: create, then send), and the client pays on PayPal's own page.
5. **The AI nudges late payers.** For overdue invoices, the LLM writes a reminder whose tone fits how late it is. PayPal sends it through `/invoices/{id}/remind`. Automatic nudges go out at most once every 3 days per invoice.
6. **Verified full payment, then payouts.** The agent marks an invoice paid only when PayPal reports `paid_amount >= total`, because partial payments are tracked separately. It then pays each helper their percentage with **PayPal Payouts**. The batch id is fixed per invoice (`draft-<id>`), so a helper can never be paid twice, even on retries.
7. **Everything is logged.** Every AI, PayPal and payout action is an event, shown live in the agent feed and in an AG Grid activity log.

## The dashboard (Agent Console)

- **Money ribbon:** AI drafted → your approval → with client → collected → helpers paid. Tap a stage to filter the ledger.
- **Agent feed:** a live stream of what the agent just did.
- **Ledger (AG Grid):** inline editing until approval, an "Agent's next step" column, stage filters, quick search, multi-select bulk approve and nudge, CSV export, saved column layout, pinned totals and change-flashing cells.
- **Invoice sheet:** a printed-invoice preview that updates as you edit, PAID, OVERDUE and DRAFT stamps, a money-split bar (you vs. each helper), the lifecycle timeline, and the original chat.
- **Insights (AG Charts):** where the money is, totals by client, AI drafts, nudges sent, and average days to get paid.
- **Autopilot:** runs the agent (check payments → pay helpers → nudge overdue) every minute while the page is open.
- **Judge Mode:** a ~40-second guided replay on in-browser demo data that mirrors the API. It needs no keys and touches no PayPal. Open `/dashboard?judge`.

## PayPal APIs used

| What | Endpoint |
|---|---|
| Auth | `POST /v1/oauth2/token` |
| Create invoice | `POST /v2/invoicing/invoices` |
| Send invoice | `POST /v2/invoicing/invoices/{id}/send` |
| Read status, payments and the payer link | `GET /v2/invoicing/invoices/{id}` |
| AI reminder | `POST /v2/invoicing/invoices/{id}/remind` |
| Cancel | `POST /v2/invoicing/invoices/{id}/cancel` |
| Pay helpers | `POST /v1/payments/payouts` |

## Run it locally

```bash
git clone https://github.com/cipoklean/settle.git
cd settle
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then fill in your sandbox keys
python app.py serve       # http://localhost:8010/dashboard
```

Open the dashboard, tap **Live key** and enter your `ADMIN_KEY`, or choose **Watch the 40-second demo** to try it without keys.

### Environment

| Variable | Purpose |
|---|---|
| `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET` | Sandbox REST app (Invoicing and Payouts enabled) |
| `AI_BASE_URL`, `AI_API_KEY`, `AI_MODEL` | Any OpenAI-compatible chat endpoint. Default: NVIDIA's API with `nvidia/nemotron-3.5-lightning-30b-a3b` |
| `ADMIN_KEY` | Protects the dashboard API (`X-Admin-Key` header) |
| `BASE_URL` | Public URL, used in approval links |
| `CLIENT_EMAIL` | Sandbox only: routes every invoice to your Personal sandbox buyer so you can pay it |

### CLI

```bash
python app.py check   # poll PayPal, mark full payments, pay helpers (cron-friendly)
python app.py nudge   # AI reminders for overdue invoices
python app.py seed    # create a demo draft without AI
```

## Test it end to end (sandbox)

1. In the PayPal Developer Dashboard, create a sandbox **Business** account (you) and two **Personal** accounts (client and helper). Put the client's email in `CLIENT_EMAIL`.
2. On the dashboard, tap **Try a sample**. Change the note to `split 20% to <helper sandbox email>`, then tap **Draft invoice with AI**.
3. Tap **Approve & send via PayPal**. Open the invoice and use **Pay with PayPal** (the payer link PayPal returns) and pay as the Personal client.
4. Tap **Run agent**. The invoice turns **Paid**, and the helper receives 20% through Payouts (check the helper's sandbox account).
5. For a late payer, set a due date in the past while the invoice is still a draft, or wait. Then tap **Nudge** and read the AI's reminder in the feed.

Automated test with PayPal and the LLM mocked (draft → review → edit → approve → nudge → partial → full payment → single payout → cancel):

```bash
python test_mock_flow.py
```

## For judges

1. **No setup:** open [Judge Mode](https://settle-rysn.onrender.com/dashboard?judge). It replays the whole loop on demo data in your browser and never calls PayPal.
2. **The real thing:** open the [live app](https://settle-rysn.onrender.com/dashboard), tap **Live key**, and enter the admin key from the Devpost testing instructions. Paste a chat, approve it, then pay the invoice as the sandbox buyer listed there. Tap **Run agent** to watch the full-payment check and the helper payout.
3. **Run it yourself:** follow *Run it locally* with your own sandbox keys.

The Render free tier sleeps when idle, so the first load can take about a minute.

## Honest limits

- Sandbox only. No real money moves.
- Payments are detected by polling PayPal (`Run agent`, Autopilot, or `python app.py check` on a cron), not by webhooks.
- Autopilot runs only while the dashboard is open. Use the CLI on a cron for unattended runs.
- Settle doesn't send email itself. Approval links are returned by the API, and PayPal emails the invoices and reminders.
- On Render's free tier the SQLite database resets on each redeploy.

## Deploy (Render, free)

The repo includes `render.yaml`. In Render, choose **New → Blueprint**, pick this repo, and fill in the secret env vars. Set `BASE_URL` to the Render URL. The free tier's SQLite disk resets on redeploy. That's fine for a demo, and Judge Mode doesn't need it.

## API

`POST /drafts/from-chat` · `GET /drafts` · `POST /drafts/{id}/edit` · `POST /drafts/{id}/approve` · `POST /drafts/{id}/nudge` · `POST /drafts/{id}/cancel` · `POST /drafts/check-payments` · `POST /drafts/nudge-overdue` · `GET /events` · `GET /drafts/{id}/events` · `GET /approve/{id}?t=` · `POST /approve/{id}`

All `/drafts*` and `/events` routes require `X-Admin-Key`.

## Project layout

```
app.py              FastAPI server: storage, approval flow, PayPal, payment checks, nudges, payouts, CLI
agent.py            LLM extraction, validation, and reminder writing
dashboard.html      Agent Console (AG Grid + AG Charts, Judge Mode)
test_mock_flow.py   End-to-end test with PayPal and the LLM mocked
render.yaml         One-click Render deploy
```

## License

MIT. See [LICENSE](LICENSE).
