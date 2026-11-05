"""Settle: AI drafts PayPal invoices from client chats, a human approves,
PayPal collects, the agent nudges late payers and pays helpers their share.

Run:    python app.py serve     (dashboard at http://localhost:8010/dashboard)
Check:  python app.py check     (poll PayPal, mark full payments, pay helpers)
Nudge:  python app.py nudge     (AI reminders for overdue invoices, max one per 3 days)
Seed:   python app.py seed      (demo draft without AI)
"""
import os, sys, json, html, sqlite3, secrets, hashlib, time
from datetime import date
from urllib.parse import parse_qs
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import HTMLResponse, RedirectResponse

load_dotenv(".env")
PP = "https://api-m.sandbox.paypal.com"
DB = os.environ.get("DB_PATH", "invoice_agent.db")
TOKEN_TTL = 48 * 3600          # approval links expire after 48h
NUDGE_GAP = 3 * 86400          # never remind the same client more than once per 3 days
EDITABLE = ("needs_review", "pending", "send_failed")
UNPAID = ("sent", "partially_paid")

app = FastAPI(title="Settle")

# ---------- storage ----------
def db():
    c = sqlite3.connect(DB, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS drafts(
        id INTEGER PRIMARY KEY, data TEXT NOT NULL, status TEXT NOT NULL,
        token_hash TEXT, token_expires REAL, approved_at REAL,
        paypal_id TEXT, error TEXT, created_at REAL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS events(
        id INTEGER PRIMARY KEY, draft_id INTEGER, at REAL, kind TEXT, message TEXT)""")
    cols = {r[1] for r in c.execute("PRAGMA table_info(drafts)")}
    for col, typ in (("payout_status", "TEXT"), ("payout_batch_id", "TEXT"), ("payout_error", "TEXT"),
                     ("paid_amount", "REAL"), ("paypal_status", "TEXT"), ("pay_url", "TEXT"),
                     ("last_checked", "REAL"), ("reminders", "INTEGER"), ("last_reminded", "REAL"),
                     ("sent_at", "REAL"), ("paid_at", "REAL"), ("source_chat", "TEXT")):
        if col not in cols: c.execute(f"ALTER TABLE drafts ADD COLUMN {col} {typ}")
    return c

def log(did, kind, message):
    db().execute("INSERT INTO events(draft_id,at,kind,message) VALUES(?,?,?,?)",
                 (did, time.time(), kind, str(message)[:500]))

def h(token): return hashlib.sha256(token.encode()).hexdigest()

def inv_total(inv):
    return round(sum(float(i["quantity"]) * float(i["unit_amount"]) for i in inv.get("items") or []), 2)

def public_base(request: Request = None):
    """BASE_URL if set to a public URL, else the URL the request came in on (works behind tunnels)."""
    env = os.environ.get("BASE_URL", "")
    if env and "localhost" not in env and "127.0.0.1" not in env: return env.rstrip("/")
    if request is not None: return str(request.base_url).rstrip("/")
    return env.rstrip("/") or "http://localhost:8010"

def new_token(did, base):
    token = secrets.token_urlsafe(32)          # raw token only ever lives in the link
    db().execute("UPDATE drafts SET token_hash=?, token_expires=? WHERE id=?",
                 (h(token), time.time() + TOKEN_TTL, did))
    return f"{base}/approve/{did}?t={token}"

def create_draft(inv: dict, status="pending", base=None, errors=None, chat=None):
    with db() as c:
        did = c.execute("INSERT INTO drafts(data,status,error,created_at,source_chat,reminders) VALUES(?,?,?,?,?,0)",
                        (json.dumps(inv), status, "; ".join(errors or []) or None, time.time(), chat)).lastrowid
    link = new_token(did, base or public_base()) if status == "pending" else None
    return did, link

def get(did):
    row = db().execute("SELECT * FROM drafts WHERE id=?", (did,)).fetchone()
    if not row: raise HTTPException(404, "Draft not found")
    return row

def row_out(r):
    d = dict(r)
    d.pop("token_hash", None); d.pop("token_expires", None)
    d["data"] = json.loads(d["data"])
    d["total"] = inv_total(d["data"])
    if d["status"] == "paid" and d.get("paid_amount") is None: d["paid_amount"] = d["total"]  # paid before tracking existed
    return d

# ---------- PayPal ----------
def pp_headers():
    t = requests.post(f"{PP}/v1/oauth2/token",
        auth=(os.environ["PAYPAL_CLIENT_ID"], os.environ["PAYPAL_CLIENT_SECRET"]),
        data={"grant_type": "client_credentials"}, timeout=30)
    t.raise_for_status()
    return {"Authorization": f"Bearer {t.json()['access_token']}", "Content-Type": "application/json"}

def pp_error(e):
    return getattr(getattr(e, "response", None), "text", "") or str(e)

def to_paypal(inv):
    cur = inv["currency"]
    # Sandbox: CLIENT_EMAIL (a sandbox Personal account) receives every invoice so test payments work
    email = os.environ.get("CLIENT_EMAIL") or inv["client_email"]
    return {
        "detail": {"currency_code": cur, "note": f"Thanks {inv.get('client_name','')}!".strip(),
                   "payment_term": {"term_type": "DUE_ON_DATE_SPECIFIED", "due_date": inv["due_date"]}},
        "primary_recipients": [{"billing_info": {"email_address": email,
                                                  **({"name": {"full_name": inv["client_name"]}} if inv.get("client_name") else {})}}],
        "items": [{"name": i["name"][:200], "quantity": str(i["quantity"]),
                   "unit_amount": {"currency_code": cur, "value": f"{float(i['unit_amount']):.2f}"}}
                  for i in inv["items"]],
    }

def send_to_paypal(did):
    row = get(did)
    try:
        H = pp_headers()
        pid = row["paypal_id"]
        if not pid:  # idempotent: only create once, even if send is retried
            r = requests.post(f"{PP}/v2/invoicing/invoices", headers=H,
                              json=to_paypal(json.loads(row["data"])), timeout=30)
            r.raise_for_status()
            j = r.json()  # minimal reply is a link: {"rel","href","method"}
            pid = j.get("id") or j["href"].rstrip("/").rsplit("/", 1)[-1]
            db().execute("UPDATE drafts SET paypal_id=? WHERE id=?", (pid, did))
            log(did, "paypal", f"PayPal draft invoice created: {pid}")
        r = requests.post(f"{PP}/v2/invoicing/invoices/{pid}/send", headers=H,
                          json={"send_to_invoicer": True}, timeout=30)
        r.raise_for_status()
        pay_url = None
        try:
            g = requests.get(f"{PP}/v2/invoicing/invoices/{pid}", headers=H, timeout=30).json()
            pay_url = g.get("detail", {}).get("metadata", {}).get("recipient_view_url")
        except Exception:
            pass
        db().execute("UPDATE drafts SET status='sent', paypal_status='SENT', error=NULL, sent_at=?, pay_url=? WHERE id=?",
                     (time.time(), pay_url, did))
        log(did, "sent", f"Invoice sent to client through PayPal ({pid})")
        return pid
    except Exception as e:
        msg = pp_error(e)
        db().execute("UPDATE drafts SET status='send_failed', error=? WHERE id=?", (msg[:500], did))
        log(did, "error", f"PayPal send failed: {msg[:200]}")
        raise HTTPException(502, f"PayPal error: {msg[:300]}")

def claim(did, token):
    """Atomically consume the token: valid hash, not expired, not used. Single-use."""
    cur = db().execute(
        "UPDATE drafts SET status='approving', approved_at=?, token_hash=NULL "
        "WHERE id=? AND token_hash=? AND status='pending' AND token_expires>?",
        (time.time(), did, h(token), time.time()))
    return cur.rowcount == 1

# ---------- approval pages (email link flow) ----------
def page(title, body):
    return HTMLResponse(f"""<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>{title}</title><body style="font-family:system-ui;max-width:520px;margin:40px auto;padding:0 16px">
{body}<p style="margin-top:32px"><a href="/dashboard">Open dashboard</a></p></body>""")

@app.get("/")
def root(): return RedirectResponse("/dashboard")

@app.get("/approve/{did}")
def approve_page(did: int, t: str = ""):
    row = get(did)
    if row["status"] != "pending" or not t or row["token_hash"] != h(t) or (row["token_expires"] or 0) < time.time():
        return page("Link invalid", "<h2>This approval link is invalid, used, or expired.</h2>")
    inv = json.loads(row["data"]); e = html.escape
    rows = "".join(f"<tr><td>{e(i['name'])}</td><td>{e(str(i['quantity']))}</td>"
                   f"<td>{float(i['unit_amount']):.2f}</td></tr>" for i in inv["items"])
    # GET never sends anything. Only the POST below does (link scanners can't trigger a send).
    return page("Approve invoice", f"""<h2>Approve invoice #{did}?</h2>
<p><b>{e(inv.get('client_name',''))}</b> &lt;{e(inv['client_email'])}&gt;<br>Due {e(inv['due_date'])}</p>
<table border=1 cellpadding=6 style="border-collapse:collapse;width:100%">
<tr><th>Item</th><th>Qty</th><th>Unit</th></tr>{rows}</table>
<h3>Total: {inv_total(inv):.2f} {e(inv['currency'])}</h3>
<form method=post><input type=hidden name=t value="{e(t)}">
<button style="font-size:18px;padding:12px 24px">Approve &amp; send via PayPal</button></form>""")

@app.post("/approve/{did}")
async def approve_submit(did: int, request: Request):
    t = parse_qs((await request.body()).decode()).get("t", [""])[0]
    if not claim(did, t):
        return page("Not sent", "<h2>Link invalid, already used, or expired. Nothing was sent.</h2>")
    log(did, "approved", "Approved by owner via email link")
    pid = send_to_paypal(did)
    return page("Sent", f"<h2>Sent ✅</h2><p>PayPal invoice {html.escape(pid)}</p>")

# ---------- API for dashboard ----------
def admin(key):
    if not os.environ.get("ADMIN_KEY") or key != os.environ["ADMIN_KEY"]:
        raise HTTPException(401, "Bad admin key")

@app.post("/drafts")
def new_draft(inv: dict, request: Request, x_admin_key: str = Header("")):
    admin(x_admin_key)
    from agent import validate
    errors, total = validate(inv)
    did, link = create_draft(inv, "needs_review" if errors else "pending", public_base(request), errors)
    log(did, "drafted", f"Draft created manually ({total:.2f} {inv.get('currency','')})")
    return {"id": did, "status": "needs_review" if errors else "pending", "errors": errors, "approve_link": link}

@app.post("/drafts/from-chat")
def draft_from_chat(body: dict, request: Request, x_admin_key: str = Header("")):
    """Chat text -> AI extraction -> code validation -> draft (+ approval link when clean)."""
    admin(x_admin_key)
    from agent import extract, validate
    chat = str(body.get("chat", "")).strip()
    note = str(body.get("note", "")).strip()
    if not chat: raise HTTPException(400, "Paste the client's message first")
    full = chat + ("\n\nFreelancer's private note (not from the client): " + note if note else "")
    try:
        inv = extract(full)
    except Exception as e:
        raise HTTPException(502, f"AI error: {str(e)[:300]}")
    inv.setdefault("items", []); inv.setdefault("splits", [])
    errors, total = validate(inv)
    if not errors: inv.pop("missing_info", None)
    status = "needs_review" if errors else "pending"
    did, link = create_draft(inv, status, public_base(request), errors, chat)
    log(did, "ai", f"AI read the chat and drafted {len(inv['items'])} item(s), {total:.2f} {inv.get('currency','')}"
                   + (f", {len(inv['splits'])} helper split(s)" if inv["splits"] else ""))
    if errors: log(did, "review", "Needs your review: " + "; ".join(errors))
    return {"status": status, "id": did, "total": total, "currency": inv.get("currency"),
            "errors": errors, "approve_link": link, "invoice": inv}

@app.get("/drafts")
def list_drafts(x_admin_key: str = Header("")):
    admin(x_admin_key)
    return [row_out(r) for r in db().execute("SELECT * FROM drafts ORDER BY id DESC").fetchall()]

@app.post("/drafts/{did}/edit")
def edit_draft(did: int, body: dict, request: Request, x_admin_key: str = Header("")):
    """Edit any field of a draft before approval (grid cell edits, line items, helpers).
    Re-validates; a clean draft becomes 'pending' and gets a fresh approval link."""
    admin(x_admin_key)
    from agent import validate
    row = get(did)
    if row["status"] not in EDITABLE:
        raise HTTPException(409, "Locked: this invoice is already approved")
    inv = json.loads(row["data"])
    changed = []
    for k in ("client_name", "client_email", "currency", "due_date"):
        if k in body:
            v = str(body[k]).strip()
            inv[k] = v.upper() if k == "currency" else v
            changed.append(k)
    if "items" in body:
        inv["items"] = [{"name": str(i.get("name", "")).strip(), "quantity": float(i.get("quantity") or 0),
                         "unit_amount": float(i.get("unit_amount") or 0)}
                        for i in body["items"] if str(i.get("name", "")).strip()]
        changed.append("items")
    if "splits" in body:
        inv["splits"] = [{"email": str(s.get("email", "")).strip(), "percent": float(s.get("percent") or 0)}
                         for s in body["splits"] if str(s.get("email", "")).strip()]
        changed.append("helpers")
    inv.pop("missing_info", None)      # a human has now reviewed it
    errors, total = validate(inv)
    status = "needs_review" if errors else ("send_failed" if row["status"] == "send_failed" else "pending")
    db().execute("UPDATE drafts SET data=?, status=?, error=? WHERE id=? AND status IN ('needs_review','pending','send_failed')",
                 (json.dumps(inv), status, "; ".join(errors) or None, did))
    link = new_token(did, public_base(request)) if status == "pending" and row["status"] != "pending" else None
    log(did, "edited", f"Edited {', '.join(changed) or 'nothing'}" +
        (f" - still needs: {'; '.join(errors)}" if errors else " - ready to approve"))
    return {**row_out(get(did)), "errors": errors, "approve_link": link}

@app.post("/drafts/{did}/approve")
def dashboard_approve(did: int, x_admin_key: str = Header("")):
    admin(x_admin_key)
    with db() as c:  # dashboard approval also consumes the email token
        ok = c.execute("UPDATE drafts SET status='approving', approved_at=?, token_hash=NULL "
                       "WHERE id=? AND status IN ('pending','send_failed')", (time.time(), did)).rowcount
    if not ok: raise HTTPException(409, "Not approvable (needs review, or already sent)")
    log(did, "approved", "Approved by owner on dashboard")
    return {"paypal_id": send_to_paypal(did)}

@app.post("/drafts/{did}/cancel")
def cancel_draft(did: int, x_admin_key: str = Header("")):
    admin(x_admin_key)
    row = get(did)
    if row["status"] in ("paid", "cancelled"): raise HTTPException(409, f"Already {row['status']}")
    if row["paypal_id"] and row["status"] in UNPAID:
        try:
            r = requests.post(f"{PP}/v2/invoicing/invoices/{row['paypal_id']}/cancel", headers=pp_headers(),
                              json={"subject": "Invoice cancelled", "note": "This invoice was cancelled.",
                                    "send_to_invoicer": True, "send_to_recipient": True}, timeout=30)
            r.raise_for_status()
        except Exception as e:
            raise HTTPException(502, f"PayPal error: {pp_error(e)[:300]}")
    db().execute("UPDATE drafts SET status='cancelled', token_hash=NULL, paypal_status=COALESCE(?,paypal_status) WHERE id=?",
                 ("CANCELLED" if row["paypal_id"] else None, did))
    log(did, "cancelled", "Invoice cancelled" + (" in PayPal" if row["paypal_id"] else ""))
    return row_out(get(did))

@app.get("/drafts/{did}/events")
def draft_events(did: int, x_admin_key: str = Header("")):
    admin(x_admin_key)
    return [dict(r) for r in db().execute("SELECT * FROM events WHERE draft_id=? ORDER BY id DESC", (did,))]

@app.get("/events")
def all_events(limit: int = 300, x_admin_key: str = Header("")):
    admin(x_admin_key)
    return [dict(r) for r in db().execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (min(limit, 1000),))]

# ---------- payment detection ----------
def check_payment(did):
    """Ask PayPal for the invoice and mark it paid ONLY when fully paid.
    PAID-style events can fire on partial payments, so we compare amounts."""
    row = get(did)
    if not row["paypal_id"]: return {"id": did, "status": row["status"]}
    if row["status"] == "paid": return {"id": did, "status": "paid"}
    r = requests.get(f"{PP}/v2/invoicing/invoices/{row['paypal_id']}", headers=pp_headers(), timeout=30)
    r.raise_for_status()
    j = r.json()
    total = float(j.get("amount", {}).get("value", 0))
    paid = float(j.get("payments", {}).get("paid_amount", {}).get("value", 0))
    pp_status = j.get("status", "")
    pay_url = j.get("detail", {}).get("metadata", {}).get("recipient_view_url") or row["pay_url"]
    if pp_status in ("PAID", "MARKED_AS_PAID") and paid + 0.005 >= total:
        new = "paid"
    elif paid > 0:
        new = "partially_paid"
    elif pp_status == "CANCELLED":
        new = "cancelled"
    else:
        new = row["status"]
    db().execute("UPDATE drafts SET status=?, paid_amount=?, paypal_status=?, pay_url=?, last_checked=?, "
                 "paid_at=CASE WHEN ?='paid' THEN ? ELSE paid_at END WHERE id=? AND status!='paid'",
                 (new, paid, pp_status, pay_url, time.time(), new, time.time(), did))
    if new != row["status"]:
        log(did, new, f"PayPal reports {paid:.2f} of {total:.2f} paid ({pp_status})")
    return {"id": did, "paypal_id": row["paypal_id"], "paypal_status": pp_status,
            "total": total, "paid": paid, "status": new}

def check_all():
    rows = db().execute("SELECT id FROM drafts WHERE status IN ('sent','partially_paid') "
                        "OR (status='paid' AND (payout_status IS NULL OR payout_status='failed'))").fetchall()
    out = []
    for r in rows:
        try:
            res = check_payment(r["id"])
            if res["status"] == "paid":
                res["payout"] = run_payout(r["id"])
        except Exception as e:
            res = {"id": r["id"], "status": "check_failed", "error": pp_error(e)[:200]}
        out.append(res)
    return out

@app.post("/drafts/check-payments")
def api_check_all(x_admin_key: str = Header("")):
    admin(x_admin_key)
    return check_all()

# ---------- AI nudges (PayPal reminders) ----------
def days_late(inv):
    try: return (date.today() - date.fromisoformat(inv["due_date"])).days
    except Exception: return 0

def nudge(did, force=False):
    """AI writes a reminder whose tone fits how late it is; PayPal emails it to the client."""
    from agent import nudge_text
    row = get(did)
    if row["status"] not in UNPAID or not row["paypal_id"]:
        raise HTTPException(409, "Only sent, unpaid invoices can be nudged")
    if not force and row["last_reminded"] and time.time() - row["last_reminded"] < NUDGE_GAP:
        return {"id": did, "skipped": "reminded in the last 3 days"}
    inv = json.loads(row["data"]); late = days_late(inv)
    note = nudge_text(inv, inv_total(inv), float(row["paid_amount"] or 0), late)
    subject = "Friendly reminder" if late <= 0 else f"Invoice #{did} is {late} day(s) overdue"
    try:
        r = requests.post(f"{PP}/v2/invoicing/invoices/{row['paypal_id']}/remind", headers=pp_headers(),
                          json={"subject": subject, "note": note, "send_to_invoicer": True,
                                "send_to_recipient": True}, timeout=30)
        r.raise_for_status()
    except Exception as e:
        log(did, "error", f"Reminder failed: {pp_error(e)[:200]}")
        raise HTTPException(502, f"PayPal error: {pp_error(e)[:300]}")
    db().execute("UPDATE drafts SET reminders=COALESCE(reminders,0)+1, last_reminded=? WHERE id=?", (time.time(), did))
    log(did, "nudged", f"AI reminder sent via PayPal: \"{note}\"")
    return {"id": did, "note": note, "days_late": late}

def nudge_overdue():
    out = []
    for r in db().execute("SELECT id,data FROM drafts WHERE status IN ('sent','partially_paid')").fetchall():
        if days_late(json.loads(r["data"])) > 0:
            try: out.append(nudge(r["id"]))
            except HTTPException as e: out.append({"id": r["id"], "error": e.detail})
    return out

@app.post("/drafts/{did}/nudge")
def api_nudge(did: int, x_admin_key: str = Header("")):
    admin(x_admin_key)
    return nudge(did, force=True)

@app.post("/drafts/nudge-overdue")
def api_nudge_overdue(x_admin_key: str = Header("")):
    admin(x_admin_key)
    return nudge_overdue()

# ---------- helper payouts ----------
def run_payout(did):
    """Pay each helper their % of a FULLY paid invoice, exactly once.
    sender_batch_id is fixed per draft, so PayPal itself refuses a second batch."""
    row = get(did)
    inv = json.loads(row["data"])
    splits = inv.get("splits") or []
    if row["status"] != "paid": return "not fully paid yet"
    if not splits:
        db().execute("UPDATE drafts SET payout_status='none' WHERE id=? AND payout_status IS NULL", (did,))
        return "no helpers"
    claimed = db().execute("UPDATE drafts SET payout_status='sending' WHERE id=? AND "
                           "(payout_status IS NULL OR payout_status='failed')", (did,)).rowcount
    if not claimed: return "already handled"
    total = inv_total(inv)
    cur = inv["currency"]
    items = [{"recipient_type": "EMAIL", "receiver": sp["email"],
              "amount": {"currency": cur, "value": f"{total * float(sp['percent']) / 100:.2f}"},
              "note": f"Your {float(sp['percent']):g}% share of invoice #{did}",
              "sender_item_id": f"draft-{did}-{n}"} for n, sp in enumerate(splits)]
    body = {"sender_batch_header": {"sender_batch_id": f"draft-{did}",
                                    "email_subject": "You have a payout",
                                    "email_message": f"Your share of invoice #{did}"},
            "items": items}
    try:
        r = requests.post(f"{PP}/v1/payments/payouts", headers=pp_headers(), json=body, timeout=30)
        if r.status_code == 400 and "SENDER_BATCH_ID_ALREADY_USED" in r.text:
            db().execute("UPDATE drafts SET payout_status='sent', payout_error=NULL WHERE id=?", (did,))
            return "already sent earlier"
        r.raise_for_status()
        bid = r.json()["batch_header"]["payout_batch_id"]
        db().execute("UPDATE drafts SET payout_status='sent', payout_batch_id=?, payout_error=NULL WHERE id=?",
                     (bid, did))
        log(did, "payout", "Helpers paid via PayPal Payouts: " +
            ", ".join(f"{i['receiver']} {i['amount']['value']} {cur}" for i in items) + f" (batch {bid})")
        return f"sent {bid}"
    except Exception as e:
        msg = pp_error(e)
        db().execute("UPDATE drafts SET payout_status='failed', payout_error=? WHERE id=?", (msg[:500], did))
        log(did, "error", f"Payout failed: {msg[:200]}")
        return f"failed: {msg[:200]}"

@app.post("/drafts/{did}/splits")
def set_splits(did: int, body: dict, request: Request, x_admin_key: str = Header("")):
    """Kept for older clients; same as /edit with splits."""
    return edit_draft(did, {"splits": body.get("splits") or []}, request, x_admin_key)

@app.get("/dashboard")
def dashboard():
    return HTMLResponse(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")).read())

# ---------- CLI ----------
if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "seed":
        inv = {"client_name": "Tunde", "client_email": "tunde@acme.io", "currency": "USD",
               "due_date": str(date.fromordinal(date.today().toordinal() + 14)),
               "items": [{"name": "Landing page redesign", "quantity": 1, "unit_amount": 250},
                         {"name": "Extra revision round", "quantity": 2, "unit_amount": 25}],
               "splits": []}
        did, link = create_draft(inv)
        log(did, "drafted", "Demo draft seeded")
        print("Draft", did, "\nApprove:", link)
    elif cmd == "check":
        for x in check_all(): print(x)
    elif cmd == "nudge":
        for x in nudge_overdue(): print(x)
    else:
        import uvicorn
        uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8010)),
                    proxy_headers=True, forwarded_allow_ips="*")
