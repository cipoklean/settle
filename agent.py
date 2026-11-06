"""Settle agent: chat text -> invoice dict (LLM), validation (code), reminder text (LLM)."""
import os, re, json, requests
from datetime import date
from dotenv import load_dotenv
load_dotenv(".env")

CURRENCIES = {"USD", "EUR", "GBP", "CAD", "AUD"}

def extract(chat: str) -> dict:
    prompt = f"""Today is {date.today()}. Extract an invoice from the client chat below.
Return ONLY a JSON object, no prose, with exactly these keys:
{{"client_name": str, "client_email": str, "currency": "ISO code",
  "due_date": "YYYY-MM-DD",
  "items": [{{"name": str, "quantity": number, "unit_amount": number}}],
  "splits": [{{"email": str, "percent": number}}],
  "missing_info": [str]}}
Use only facts stated in the chat. If something is missing, use "" and list it in missing_info.
"splits" are helpers the freelancer wants to pay a share to (e.g. "20% to ada@x.com"); use [] if none are mentioned.
Chat:
{chat}"""
    base = os.getenv("AI_BASE_URL", "https://integrate.api.nvidia.com/v1").rstrip("/")
    key = os.environ["AI_API_KEY"]
    model = os.getenv("AI_MODEL", "nvidia/nemotron-3.5-lightning-30b-a3b")
    r = requests.post(f"{base}/chat/completions", timeout=120,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": model, "temperature": 0, "max_tokens": 6000,
              "messages": [{"role": "system", "content": "/no_think You output strict JSON only."},
                           {"role": "user", "content": prompt}]})
    r.raise_for_status()
    msg = r.json()["choices"][0]["message"]
    text = re.sub(r"<think>.*?</think>", "", msg.get("content") or "", flags=re.S)
    if "{" not in text:  # reasoning models may leave content empty
        text = msg.get("reasoning_content") or msg.get("reasoning") or text
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"No JSON in AI reply: {text[:200]!r}")
    inv = json.loads(m.group(0))
    if not inv.get("splits"):  # small models sometimes miss "20% to x@y.com"
        found = re.findall(r"(\d+(?:\.\d+)?)\s*%\s*(?:to|for)\s+([^@\s]+@[^@\s]+\.[A-Za-z]+)", chat)
        inv["splits"] = [{"email": e, "percent": float(p)} for p, e in found]
    return inv

EMAIL = r"[^@\s]+@[^@\s]+\.[^@\s]+"

def validate_splits(splits):
    errors, total = [], 0.0
    if not isinstance(splits, list): return ["splits must be a list"]
    for s in splits:
        try:
            p = float(s["percent"])
            if not re.fullmatch(EMAIL, str(s["email"]).strip()): errors.append(f"bad helper email: {s['email']}")
            if not 0 < p <= 100: errors.append(f"bad percent: {p}")
            total += p
        except (KeyError, TypeError, ValueError):
            errors.append(f"malformed split: {s}")
    if total > 100: errors.append("splits add up to more than 100%")
    return errors

def validate(inv: dict):
    """Returns (errors, total). Code checks the AI's work; the AI never decides alone."""
    errors = []
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", str(inv.get("client_email", ""))):
        errors.append("bad email")
    if inv.get("currency") not in CURRENCIES:
        errors.append("unsupported currency")
    try:
        if date.fromisoformat(str(inv.get("due_date", ""))) < date.today():
            errors.append("due date in past")
    except ValueError:
        errors.append("bad due date")
    items = inv.get("items") or []
    if not items:
        errors.append("no items")
    total = 0.0
    for it in items:
        try:
            q, u = float(it["quantity"]), float(it["unit_amount"])
            if q <= 0 or u <= 0:
                errors.append(f"bad amount: {it.get('name')}")
            total += q * u
        except (KeyError, TypeError, ValueError):
            errors.append(f"malformed item: {it}")
    errors += validate_splits(inv.get("splits") or [])
    for m in inv.get("missing_info") or []:
        errors.append(f"missing: {m}")
    return errors, round(total, 2)

def _chat(prompt, max_tokens=1500):
    base = os.getenv("AI_BASE_URL", "https://integrate.api.nvidia.com/v1").rstrip("/")
    key = os.environ["AI_API_KEY"]
    model = os.getenv("AI_MODEL", "nvidia/nemotron-3.5-lightning-30b-a3b")
    r = requests.post(f"{base}/chat/completions", timeout=90,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": model, "temperature": 0.4, "max_tokens": max_tokens,
              "messages": [{"role": "system", "content": "/no_think You write short, warm, professional messages."},
                           {"role": "user", "content": prompt}]})
    r.raise_for_status()
    msg = r.json()["choices"][0]["message"]
    text = re.sub(r"<think>.*?</think>", "", msg.get("content") or "", flags=re.S).strip()
    return text or (msg.get("reasoning_content") or "").strip()

def nudge_text(inv: dict, total: float, paid: float, days_late: int) -> str:
    """AI-written payment reminder. Tone gets firmer the later it is; falls back to a template."""
    due = total - paid
    tone = "friendly and light" if days_late <= 0 else ("polite but clear" if days_late < 14 else "firm and direct, still courteous")
    when = "is due soon" if days_late <= 0 else f"is {days_late} day(s) overdue"
    prompt = (f"Write a payment reminder note (max 60 words, no subject line, no placeholders) to "
              f"{inv.get('client_name') or 'the client'}. The invoice for "
              f"{', '.join(i['name'] for i in inv.get('items', []))} {when}. "
              f"Amount still owed: {due:.2f} {inv['currency']}. Tone: {tone}. They can pay with the PayPal button on the invoice.")
    try:
        t = _chat(prompt).strip().strip('"')
        if 10 < len(t) < 600: return t
    except Exception:
        pass
    return (f"Hi {inv.get('client_name') or 'there'}, a quick reminder that {due:.2f} {inv['currency']} "
            f"{'is due soon' if days_late <= 0 else f'is now {days_late} days overdue'}. "
            "You can pay securely with the PayPal button on the invoice. Thank you!")
