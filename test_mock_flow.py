import os, json, types
os.environ.update(ADMIN_KEY="k", DB_PATH="test_mock.db", PAYPAL_CLIENT_ID="x", PAYPAL_CLIENT_SECRET="y", AI_API_KEY="z", BASE_URL="http://localhost:8010")
if os.path.exists("test_mock.db"): os.remove("test_mock.db")
import requests, agent
from datetime import date, timedelta
due = str(date.today()+timedelta(days=10))
STATE = {"paid": 0.0, "status": "SENT", "calls": []}
class R:
    def __init__(s, code=200, j=None, text=""): s.status_code=code; s._j=j or {}; s.text=text or json.dumps(s._j)
    def json(s): return s._j
    def raise_for_status(s):
        if s.status_code>=400: e=requests.HTTPError(s.text); e.response=s; raise e
def fake_post(url, **kw):
    STATE["calls"].append(("POST", url, kw.get("json")))
    if "oauth2" in url: return R(j={"access_token":"t"})
    if "chat/completions" in url:
        p = kw["json"]["messages"][1]["content"]
        if "Extract an invoice" in p:
            if "BROKEN" in p:
                return R(j={"choices":[{"message":{"content":json.dumps({"client_name":"Bob","client_email":"bob","currency":"USD","due_date":due,"items":[{"name":"Logo","quantity":1,"unit_amount":300}],"splits":[],"missing_info":["email"]})}}]})
            return R(j={"choices":[{"message":{"content":"<think>x</think>"+json.dumps({"client_name":"Amaka","client_email":"amaka@x.co","currency":"USD","due_date":due,"items":[{"name":"Logo pack","quantity":1,"unit_amount":300},{"name":"Templates","quantity":4,"unit_amount":50}],"splits":[],"missing_info":[]})}}]})
        return R(j={"choices":[{"message":{"content":"Hi Amaka, gentle reminder that 500.00 USD is due. Thanks!"}}]})
    if url.endswith("/v2/invoicing/invoices"): return R(201, {"rel":"self","href":"https://api/v2/invoicing/invoices/INV2-TEST","method":"GET"})
    if url.endswith("/send") or url.endswith("/remind") or url.endswith("/cancel"): return R(200 if url.endswith("/send") else 204, {})
    if "payouts" in url: return R(201, {"batch_header":{"payout_batch_id":"BATCH1"}})
    raise Exception("unexpected "+url)
def fake_get(url, **kw):
    return R(j={"status":STATE["status"],"amount":{"value":"500.00"},"payments":{"paid_amount":{"value":str(STATE["paid"])}},"detail":{"metadata":{"recipient_view_url":"https://sandbox/pay/INV2-TEST"}}})
requests.post=fake_post; requests.get=fake_get
import app
from fastapi.testclient import TestClient
c = TestClient(app.app); H={"X-Admin-Key":"k"}
assert c.get("/", follow_redirects=False).status_code in (302,307)
assert c.get("/dashboard").status_code==200
assert c.get("/drafts").status_code==401
r=c.post("/drafts/from-chat",headers=H,json={"chat":"Amaka wants logo","note":"split 20% to ada@example.com"}).json(); print("chat1",r["status"],r["id"],r["total"],r["invoice"]["splits"],r["approve_link"])
assert r["status"]=="pending" and r["invoice"]["splits"]==[{"email":"ada@example.com","percent":20.0}] and r["approve_link"].startswith("http://testserver/approve/")
r2=c.post("/drafts/from-chat",headers=H,json={"chat":"BROKEN chat"}).json(); print("chat2",r2["status"],r2["errors"])
assert r2["status"]=="needs_review"
assert c.post(f"/drafts/{r2['id']}/approve",headers=H).status_code==409
e=c.post(f"/drafts/{r2['id']}/edit",headers=H,json={"client_email":"bob@x.co"}).json(); print("edit",e["status"],e["errors"],e["approve_link"])
assert e["status"]=="pending" and e["approve_link"]
e=c.post(f"/drafts/{r2['id']}/edit",headers=H,json={"items":[{"name":"Logo","quantity":2,"unit_amount":150},{"name":"","quantity":1,"unit_amount":1}],"splits":[{"email":"a@b.co","percent":120}]}).json(); print("edit2",e["status"],e["errors"])
assert e["status"]=="needs_review"
# approve draft 1
a=c.post(f"/drafts/{r['id']}/approve",headers=H).json(); print("approve",a)
assert c.post(f"/drafts/{r['id']}/approve",headers=H).status_code==409
assert c.post(f"/drafts/{r['id']}/edit",headers=H,json={"client_name":"x"}).status_code==409
rows={d["id"]:d for d in c.get("/drafts",headers=H).json()}; d=rows[r["id"]]; print("row",d["status"],d["pay_url"],d["total"],"token_hash" in d)
n=c.post(f"/drafts/{r['id']}/nudge",headers=H).json(); print("nudge",n)
print("overdue",c.post("/drafts/nudge-overdue",headers=H).json())
STATE["paid"]=200.0; STATE["status"]="PARTIALLY_PAID"
print("check partial",c.post("/drafts/check-payments",headers=H).json())
STATE["paid"]=500.0; STATE["status"]="PAID"
print("check full",c.post("/drafts/check-payments",headers=H).json())
print("check again",c.post("/drafts/check-payments",headers=H).json())
pays=[x for x in STATE["calls"] if "payouts" in x[1]]; print("payout calls",len(pays), pays[0][2]["items"][0]["amount"])
assert len(pays)==1 and pays[0][2]["items"][0]["amount"]["value"]=="100.00"
print("cancel",c.post(f"/drafts/{r2['id']}/cancel",headers=H).json()["status"])
ev=c.get("/events",headers=H).json(); print(len(ev),"events"); [print(" ",x["draft_id"],x["kind"],x["message"][:90]) for x in ev[::-1]]
# email approve flow
d3,link=app.create_draft({"client_name":"T","client_email":"t@x.co","currency":"USD","due_date":due,"items":[{"name":"a","quantity":1,"unit_amount":5}],"splits":[]})
tok=link.split("t=")[1]
assert "Approve" in c.get(f"/approve/{d3}?t={tok}").text
assert "Sent" in c.post(f"/approve/{d3}",data={"t":tok}).text
assert "Nothing was sent" in c.post(f"/approve/{d3}",data={"t":tok}).text
print("ALL OK")
