#!/usr/bin/env python3
"""Read-only setup check: token, permissions, access tier, and every id of one account in accounts.json.

Usage: check_setup.py <account_key> [adset_id]
  adset_id (optional): an ad set that already runs in Israel (built in Ads Manager); prints its
  regional_regulated_categories + regional_regulation_identities, to copy into the account's "regional" block.
Writes nothing to Meta. Prints OK / FAIL per line, never the token.
"""
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from metaapi import BASE, MetaClient, MetaError, SKILL_DIR, load_env  # noqa: E402

NEEDED_SCOPES = {"ads_management", "ads_read", "business_management", "pages_show_list"}


def line(ok: bool, what: str, detail: str = ""):
    print(f"{'OK  ' if ok else 'FAIL'} {what}" + (f" - {detail}" if detail else ""))
    return ok


def check(client: MetaClient, what: str, path: str, fields: str):
    try:
        d = client.get(path, fields=fields)
        return line(True, what, ", ".join(f"{k}={v}" for k, v in d.items() if k != "id")), d
    except MetaError as e:
        return line(False, what, e.user_msg or str(e)), None


def access_tier(token: str, act: str) -> str:
    q = urllib.parse.urlencode({"fields": "id", "limit": 1, "access_token": token})
    with urllib.request.urlopen(f"{BASE}/act_{act}/campaigns?{q}", timeout=60) as r:
        usage = json.loads(r.headers.get("X-Business-Use-Case-Usage") or "{}")
    tiers = {e.get("ads_api_access_tier") for entries in usage.values() for e in entries}
    return ",".join(sorted(t for t in tiers if t)) or "unknown"


def main():
    if len(sys.argv) not in (2, 3):
        sys.exit(__doc__)
    accounts_path = SKILL_DIR / "accounts.json"
    if not accounts_path.exists():
        sys.exit("accounts.json not found. Copy accounts.example.json to accounts.json first (SETUP.md, step 6).")
    accounts = json.loads(accounts_path.read_text(encoding="utf-8"))
    acct = accounts.get(sys.argv[1])
    if not acct:
        sys.exit(f"'{sys.argv[1]}' is not in accounts.json. Known: {', '.join(accounts)}")
    env = load_env()
    client = MetaClient(env.get("ACCESS_TOKEN", ""), env.get("APP_SECRET"))
    all_ok = True

    ok, _ = check(client, "token works (system user)", "me", "id,name")
    all_ok &= ok
    try:
        perms = client.get("me/permissions").get("data", [])
        granted = {p["permission"] for p in perms if p.get("status") == "granted"}
        missing = NEEDED_SCOPES - granted
        all_ok &= line(not missing, "token permissions", f"missing: {', '.join(sorted(missing))}" if missing else "all 4 granted")
    except MetaError as e:
        line(False, "token permissions", e.user_msg or str(e))

    act = str(acct.get("ad_account_id", "")).removeprefix("act_")
    ok, d = check(client, "ad account", f"act_{act}", "name,account_status,currency,timezone_name")
    all_ok &= ok
    if d and d.get("account_status") != 1:
        all_ok &= line(False, "ad account is active", f"account_status={d.get('account_status')} (1 = active)")
    if d and acct.get("currency") and d.get("currency") != acct["currency"]:
        line(False, "currency matches accounts.json", f"Meta says {d.get('currency')}")
    if acct.get("business_id"):
        all_ok &= check(client, "business portfolio", str(acct["business_id"]), "name")[0]
    ok, page = check(client, "Facebook page", str(acct.get("page_id")), "name,instagram_business_account")
    all_ok &= ok
    ig = ((page or {}).get("instagram_business_account") or {}).get("id")
    if ig and str(acct.get("instagram_user_id")) != ig:
        line(False, "instagram_user_id", f"the page's Instagram account is {ig}; put it in accounts.json")
    if acct.get("instagram_user_id"):
        all_ok &= check(client, "Instagram account", str(acct["instagram_user_id"]), "username")[0]
    if acct.get("pixel_id"):
        all_ok &= check(client, "pixel", str(acct["pixel_id"]), "name,last_fired_time")[0]
    if len(sys.argv) == 3:
        try:
            d = client.get(sys.argv[2], fields="name,regional_regulated_categories,regional_regulation_identities")
            print("     ad set " + json.dumps(d, ensure_ascii=False))
        except MetaError as e:
            line(False, "read ad set", e.user_msg or str(e))
    if acct.get("regional", {}).get("identity_business_id"):
        line(True, "Israel compliance block present", "identity_business_id set")
    else:
        line(False, "Israel compliance block", "missing; ad sets targeting IL will be rejected (SETUP.md, step 6)")
    try:
        tier = access_tier(client.token, act)
        line(True, "Marketing API access tier", tier)
    except Exception as e:  # informational only; the message is not printed, it may carry the request URL
        line(False, "Marketing API access tier", f"could not read ({type(e).__name__})")
    if not acct.get("write_allowed"):
        line(False, "write_allowed", "false: plan works, create is blocked until you set it to true")
    print("\nAll set." if all_ok else "\nFix the FAIL lines above (SETUP.md has the steps).")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
