#!/usr/bin/env python3
"""meta-launch (lite): campaign spec -> validate -> preview -> approve -> create (always PAUSED).

Commands
  plan     <spec.json>                 resolve + guardrails + print summary and approval hash
  validate <spec.json>                 plan + Meta validate_only on the campaign payload
  preview  <spec.json>                 upload assets, create creatives, print Meta preview links
  create   <spec.json> --approve HASH  create campaign, ad sets, creatives, ads. Everything PAUSED.
  status   <campaign_id>               read back statuses, learning stage, review feedback
  pause    <object_id>                 kill switch: set status=PAUSED on a campaign / ad set / ad

Never activates, never deletes. See SKILL.md.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from metaapi import MetaClient, MetaError, SKILL_DIR, load_env  # noqa: E402

ACCOUNTS_PATH = SKILL_DIR / "accounts.json"
OUT_DIR = SKILL_DIR / "out"

OBJECTIVES = {"OUTCOME_AWARENESS", "OUTCOME_TRAFFIC", "OUTCOME_ENGAGEMENT", "OUTCOME_LEADS",
              "OUTCOME_SALES", "OUTCOME_APP_PROMOTION"}
GOALS_BY_OBJECTIVE = {
    "OUTCOME_AWARENESS": ["REACH", "IMPRESSIONS", "AD_RECALL_LIFT", "THRUPLAY", "TWO_SECOND_CONTINUOUS_VIDEO_VIEWS"],
    "OUTCOME_TRAFFIC": ["LINK_CLICKS", "LANDING_PAGE_VIEWS", "OFFSITE_CONVERSIONS", "IMPRESSIONS", "POST_ENGAGEMENT", "REACH", "CONVERSATIONS", "THRUPLAY", "VISIT_INSTAGRAM_PROFILE", "PROFILE_VISIT", "QUALITY_CALL", "REMINDERS_SET"],
    "OUTCOME_ENGAGEMENT": ["THRUPLAY", "POST_ENGAGEMENT", "EVENT_RESPONSES", "PAGE_LIKES", "IMPRESSIONS", "REACH", "TWO_SECOND_CONTINUOUS_VIDEO_VIEWS", "VIDEO_VIEWS", "LINK_CLICKS", "CONVERSATIONS", "OFFSITE_CONVERSIONS", "LANDING_PAGE_VIEWS", "QUALITY_CALL"],
    "OUTCOME_LEADS": ["OFFSITE_CONVERSIONS", "LEAD_GENERATION", "QUALITY_LEAD", "LANDING_PAGE_VIEWS", "LINK_CLICKS", "IMPRESSIONS", "REACH", "VALUE", "CONVERSATIONS", "QUALITY_CALL"],
    "OUTCOME_SALES": ["OFFSITE_CONVERSIONS", "VALUE", "LANDING_PAGE_VIEWS", "IMPRESSIONS", "POST_ENGAGEMENT", "REACH", "LINK_CLICKS", "CONVERSATIONS"],
    "OUTCOME_APP_PROMOTION": ["APP_INSTALLS", "OFFSITE_CONVERSIONS", "IMPRESSIONS", "LINK_CLICKS", "REACH", "VALUE", "VIDEO_VIEWS"],
}
BID_STRATEGIES = {"LOWEST_COST_WITHOUT_CAP", "LOWEST_COST_WITH_BID_CAP", "COST_CAP", "LOWEST_COST_WITH_MIN_ROAS"}
PIXEL_GOALS = {"OFFSITE_CONVERSIONS", "VALUE", "LANDING_PAGE_VIEWS"}
# Advantage+ creative features we opt OUT of (brand safety: nothing generative or text-altering).
ENHANCEMENTS_OFF = [
    "image_templates", "text_optimizations", "image_animation", "image_background_gen", "music_generation",
    "video_highlights", "inline_comment", "description_automation", "text_overlay_translation", "text_translation",
    "translate_voiceover", "image_touchups", "add_text_overlay", "generate_cta", "replace_media_text",
    "multi_photo_to_video", "video_to_image", "creative_stickers", "reveal_details_over_time", "show_summary",
    "ads_with_benefits", "media_type_automation", "site_extensions", "profile_card", "profile_extension",
    "show_destination_blurbs", "text_extraction_for_headline", "text_extraction_for_tap_target",
    "product_browsing",  # an in-app product page before the site; default-on for existing ads since late 2026
]
PREVIEW_FORMATS = ["MOBILE_FEED_STANDARD", "INSTAGRAM_STANDARD", "INSTAGRAM_STORY"]
DEV_MODE_SUBCODE = 1885183  # "creative post created by an app in development mode; app must be public"


# ----------------------------------------------------------------------------- helpers
def die(msg: str):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(2)


def load_json(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        die(f"file not found: {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def load_accounts() -> dict:
    if not ACCOUNTS_PATH.exists():
        die(f"{ACCOUNTS_PATH} not found. Copy accounts.example.json to accounts.json and fill it in (SETUP.md, step 6).")
    return load_json(ACCOUNTS_PATH)


def ils(amount) -> int:
    """Major units (ILS) -> minor units (agorot): Meta wants minor units."""
    return int(round(float(amount) * 100))


def spec_hash(resolved: dict) -> str:
    canon = json.dumps(resolved, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:12]


def ads_manager_url(act: str, obj_id: str) -> str:
    return f"https://www.facebook.com/adsmanager/manage/campaigns/edit?act={act}&selected_campaign_ids={obj_id}"


def resolve_path(p: str, base: Path) -> Path:
    path = Path(p).expanduser()
    if not path.is_absolute():
        path = (base / path).resolve()
    return path


# ----------------------------------------------------------------------------- resolve + guardrails
def resolve(spec_path: str) -> dict:
    spec = load_json(spec_path)
    base = Path(spec_path).resolve().parent
    accounts = load_accounts()
    errors, warnings = [], []

    acct_key = spec.get("account")
    if acct_key not in accounts:
        die(f"account '{acct_key}' is not in accounts.json (allowlist). Known: {', '.join(accounts)}")
    acct = accounts[acct_key]
    if not acct.get("write_allowed"):
        die(f"account '{acct_key}' has write_allowed=false in accounts.json")

    camp = spec.get("campaign") or {}
    if camp.get("objective") not in OBJECTIVES:
        errors.append(f"campaign.objective must be one of {sorted(OBJECTIVES)}")
    if not camp.get("name"):
        errors.append("campaign.name is required")
    daily = camp.get("daily_budget_ils")
    lifetime = camp.get("lifetime_budget_ils")
    abo = camp.get("budget_mode", "CBO") == "ABO"  # ABO: budgets and bidding live on the ad sets
    if abo and (daily or lifetime):
        errors.append("budget_mode ABO: remove the campaign budget, set daily_budget_ils on every ad set")
    if not abo and not daily and not lifetime:
        errors.append("campaign.daily_budget_ils (CBO) is required (or lifetime_budget_ils + end_time), or set budget_mode: \"ABO\"")
    if daily and lifetime:
        errors.append("use daily_budget_ils OR lifetime_budget_ils, not both")
    if lifetime and not camp.get("end_time"):
        errors.append("lifetime_budget_ils needs campaign.end_time")
    max_daily = float(acct.get("max_daily_budget_ils", 300))
    if daily and float(daily) > max_daily and not spec.get("override_budget_guard"):
        errors.append(f"daily budget {daily} ILS exceeds the account guard of {max_daily} ILS "
                      f"(set override_budget_guard: true in the spec only with the user's explicit approval)")
    cap = camp.get("spend_cap_ils")
    min_cap = float(acct.get("min_spend_cap_ils", 500))
    if cap is None:
        errors.append(f"campaign.spend_cap_ils is required (Meta minimum for ILS: {min_cap:.0f})")
    elif float(cap) < min_cap:
        errors.append(f"spend_cap_ils must be >= {min_cap:.0f} ILS (Meta minimum)")
    bid = camp.get("bid_strategy", "LOWEST_COST_WITHOUT_CAP")
    if bid not in BID_STRATEGIES:
        errors.append(f"bid_strategy must be one of {sorted(BID_STRATEGIES)}")

    adsets = spec.get("adsets") or []
    if not adsets:
        errors.append("at least one ad set is required")
    if abo:
        total = sum(float(x.get("daily_budget_ils") or 0) for x in adsets)
        if total > max_daily and not spec.get("override_budget_guard"):
            errors.append(f"ad set budgets add up to {total:.0f} ILS/day, above the account guard of {max_daily:.0f} ILS "
                          f"(override_budget_guard: true only with the user's explicit approval)")
    resolved_adsets = []
    for i, a in enumerate(adsets):
        key = f"adsets[{i}]"
        if not a.get("name"):
            errors.append(f"{key}.name is required")
        goal = a.get("optimization_goal", "OFFSITE_CONVERSIONS")
        allowed = GOALS_BY_OBJECTIVE.get(camp.get("objective"), [])
        if allowed and goal not in allowed:
            errors.append(f"{key}.optimization_goal '{goal}' is not valid for {camp.get('objective')}: {allowed}")
        t = a.get("targeting") or {}
        countries = t.get("countries") or ["IL"]
        adv = t.get("advantage_audience", True)
        age_min = int(t.get("age_min", 18))
        age_max = t.get("age_max")
        if adv and age_max is not None:
            warnings.append(f"{key}: age_max={age_max} dropped, Meta keeps the max age at 65 under Advantage+ audience")
            age_max = None
        if adv and not (18 <= age_min <= 25):
            errors.append(f"{key}: with Advantage+ audience, age_min must be 18..25 (got {age_min}); "
                          f"for a higher floor set advantage_audience: false")
        geo = {"countries": countries}
        if t.get("custom_locations"):  # radius pins; countries still drive the Israel compliance fields
            geo = {"custom_locations": [{k: loc[k] for k in ("latitude", "longitude", "radius", "distance_unit") if k in loc}
                                        for loc in t["custom_locations"]]}
            for loc in t["custom_locations"]:
                if not {"latitude", "longitude", "radius"} <= set(loc):
                    errors.append(f"{key}: custom_locations entries need latitude, longitude, radius")
        targeting = {"geo_locations": geo, "age_min": age_min}
        if age_max:
            targeting["age_max"] = int(age_max)
        if t.get("genders"):
            targeting["genders"] = t["genders"]
        if t.get("locales"):
            targeting["locales"] = t["locales"]
        if t.get("custom_audiences"):
            targeting["custom_audiences"] = [{"id": x} for x in t["custom_audiences"]]
        if t.get("excluded_custom_audiences"):
            targeting["excluded_custom_audiences"] = [{"id": x} for x in t["excluded_custom_audiences"]]
        targeting["targeting_automation"] = {"advantage_audience": 1 if adv else 0}
        if t.get("custom_audiences") and not adv:
            # keep a manual custom audience a closed list: Meta's default treats it as a suggestion and can
            # deliver far outside a small list within hours
            targeting["targeting_relaxation_types"] = {"lookalike": 0, "custom_audience": 0}
        placements = a.get("placements", "advantage")
        if isinstance(placements, dict):
            targeting.update(placements)  # manual placements: publisher_platforms, facebook_positions, ...
        promoted = None
        # Traffic optimised for landing page views takes no promoted object: the pixel on the page counts the views.
        traffic_lpv = goal == "LANDING_PAGE_VIEWS" and camp.get("objective") == "OUTCOME_TRAFFIC"
        if (goal in PIXEL_GOALS and not traffic_lpv) or a.get("conversion_event"):
            promoted = {"pixel_id": a.get("pixel_id") or acct.get("pixel_id"),
                        "custom_event_type": a.get("conversion_event", "LEAD")}
            if not promoted["pixel_id"]:
                errors.append(f"{key}: pixel_id missing (account has none, and none given)")
        elif goal in {"LEAD_GENERATION", "QUALITY_LEAD"}:
            promoted = {"page_id": acct["page_id"]}
        as_daily = a.get("daily_budget_ils")
        as_bid = a.get("bid_strategy", "LOWEST_COST_WITHOUT_CAP")
        if abo:
            if not as_daily:
                errors.append(f"{key}: daily_budget_ils is required in ABO mode")
            if as_bid not in BID_STRATEGIES:
                errors.append(f"{key}: bid_strategy must be one of {sorted(BID_STRATEGIES)}")
            if as_bid in {"LOWEST_COST_WITH_BID_CAP", "COST_CAP"} and not a.get("bid_amount_ils"):
                errors.append(f"{key}: bid_amount_ils is required with {as_bid}")
        elif as_daily:
            errors.append(f"{key}: ad set budget only in ABO mode (campaign.budget_mode: \"ABO\")")
        # CBO with COST_CAP / bid cap: the strategy lives on the campaign, each ad set still needs its own bid_amount_ils
        regional = None
        if "IL" in countries and acct.get("regional"):
            regional = {"categories": acct["regional"]["categories"],
                        "identities": {"universal_beneficiary": acct["regional"]["identity_business_id"],
                                       "universal_payer": acct["regional"]["identity_business_id"]}}
        elif "IL" in countries:
            warnings.append(f"{key}: targets Israel but the account has no 'regional' block; Meta will reject the ad set "
                            f"with 'Advertiser is missing' (SETUP.md, step 6)")
        ads = a.get("ads") or []
        if not ads:
            errors.append(f"{key}: at least one ad is required")
        resolved_ads = []
        for j, ad in enumerate(ads):
            akey = f"{key}.ads[{j}]"
            cr = ad.get("creative") or {}
            ctype = cr.get("type", "image")
            if ctype not in {"image", "video", "carousel"}:
                errors.append(f"{akey}: creative.type must be image | video | carousel")
            if not ad.get("name"):
                errors.append(f"{akey}.name is required")
            if not cr.get("message"):
                errors.append(f"{akey}: creative.message (primary text) is required")
            lead_form = str(cr["lead_form_id"]) if cr.get("lead_form_id") else None
            if lead_form and ctype not in {"image", "video"}:
                errors.append(f"{akey}: lead_form_id is supported on image and video creatives")
            link = cr.get("link") or ("http://fb.me/" if lead_form else None) or acct.get("default_link")
            if not link:
                errors.append(f"{akey}: creative.link is required")
            assets = {}
            if ctype == "image":
                if cr.get("image_hash"):
                    assets["image_hash"] = cr["image_hash"]
                elif cr.get("image"):
                    p = resolve_path(cr["image"], base)
                    if not p.exists():
                        errors.append(f"{akey}: image not found: {p}")
                    assets["image"] = str(p)
                else:
                    errors.append(f"{akey}: creative.image (file) or image_hash is required")
            elif ctype == "video":
                if cr.get("video_id"):
                    assets["video_id"] = cr["video_id"]
                elif cr.get("video"):
                    p = resolve_path(cr["video"], base)
                    if not p.exists():
                        errors.append(f"{akey}: video not found: {p}")
                    assets["video"] = str(p)
                else:
                    errors.append(f"{akey}: creative.video (file) or video_id is required")
                if cr.get("thumbnail_hash"):
                    assets["thumbnail_hash"] = cr["thumbnail_hash"]
                elif cr.get("thumbnail"):
                    p = resolve_path(cr["thumbnail"], base)
                    if not p.exists():
                        errors.append(f"{akey}: thumbnail not found: {p}")
                    assets["thumbnail"] = str(p)
            elif ctype == "carousel":
                cards = cr.get("cards") or []
                if not 2 <= len(cards) <= 10:
                    errors.append(f"{akey}: carousel needs 2..10 cards")
                assets["cards"] = []
                for k, card in enumerate(cards):
                    c = dict(card)
                    if c.get("image"):
                        p = resolve_path(c["image"], base)
                        if not p.exists():
                            errors.append(f"{akey}.cards[{k}]: image not found: {p}")
                        c["image"] = str(p)
                    elif not c.get("image_hash"):
                        errors.append(f"{akey}.cards[{k}]: image or image_hash required")
                    c.setdefault("link", link)
                    assets["cards"].append(c)
            resolved_ads.append({
                "name": ad["name"] if ad.get("name") else f"ad-{j}",
                "creative": {
                    "type": ctype, "name": cr.get("name") or ad.get("name"),
                    "message": cr.get("message", ""), "headline": cr.get("headline"),
                    "description": cr.get("description"), "link": link,
                    "cta": cr.get("cta", "LEARN_MORE"), "utm": cr.get("utm"),
                    "assets": assets,
                    **({"lead_form_id": lead_form} if lead_form else {}),
                },
            })
        resolved_adsets.append({
            "name": a["name"] if a.get("name") else f"adset-{i}",
            "optimization_goal": goal,
            "billing_event": a.get("billing_event", "IMPRESSIONS"),
            "destination_type": a.get("destination_type", "WEBSITE"),
            "promoted_object": promoted,
            "daily_budget": ils(as_daily) if (abo and as_daily) else None,
            "bid_strategy": as_bid if abo else None,
            "bid_amount": ils(a["bid_amount_ils"]) if a.get("bid_amount_ils") else None,
            "targeting": targeting,
            "regional": regional,
            "start_time": a.get("start_time"),
            "end_time": a.get("end_time"),
            "ads": resolved_ads,
        })

    acct_view = {k: acct[k] for k in ("ad_account_id", "business_id", "page_id", "instagram_user_id",
                                      "pixel_id", "conversion_domain", "currency") if k in acct}
    resolved = {
        "account_key": acct_key,
        "account": acct_view,
        "campaign": {
            "name": camp.get("name"), "objective": camp.get("objective"),
            "daily_budget": ils(daily) if daily else None,
            "lifetime_budget": ils(lifetime) if lifetime else None,
            "end_time": camp.get("end_time"),
            "spend_cap": ils(cap) if cap is not None else None,
            "budget_mode": "ABO" if abo else "CBO",
            "adset_budget_sharing": bool(camp.get("adset_budget_sharing", False)) if abo else None,
            "bid_strategy": None if abo else bid,
            "special_ad_categories": camp.get("special_ad_categories", []),
        },
        "adsets": resolved_adsets,
    }
    resolved["hash"] = spec_hash({k: v for k, v in resolved.items() if k != "hash"})
    resolved["_errors"], resolved["_warnings"] = errors, warnings
    return resolved


def geo_label(geo: dict) -> str:
    parts = [f"{l['latitude']:.4f},{l['longitude']:.4f} r{l['radius']}{l.get('distance_unit', 'mile')[0]}"
             for l in geo.get("custom_locations") or []]
    return " + ".join(parts) or ",".join(geo.get("countries", []))


def print_plan(res: dict):
    acct = res["account"]
    camp = res["campaign"]
    cur = acct.get("currency", "ILS")
    print(f"\n=== PLAN {res['hash']} | account {res['account_key']} (act_{acct['ad_account_id']}) ===")
    if camp.get("budget_mode") == "ABO":
        total = sum((a.get("daily_budget") or 0) for a in res["adsets"]) / 100
        budget = f"ABO, ad set budgets add up to {total:.0f} {cur}/day"
    else:
        budget = "CBO " + (f"{camp['daily_budget'] / 100:.0f} {cur}/day" if camp["daily_budget"] else f"{camp['lifetime_budget'] / 100:.0f} {cur} lifetime")
    cap_txt = f"{camp['spend_cap'] / 100:.0f} {cur}" if camp["spend_cap"] else "NONE"
    print(f"Campaign: {camp['name']} | {camp['objective']} | {budget} | spend cap {cap_txt} | "
          f"{camp['bid_strategy'] or 'bidding on the ad sets'} | status PAUSED")
    n_ads = 0
    for i, a in enumerate(res["adsets"]):
        t = a["targeting"]
        adv = "Advantage+ audience" if t["targeting_automation"]["advantage_audience"] else "manual audience"
        if t.get("custom_audiences") and not t["targeting_automation"]["advantage_audience"]:
            adv += " (closed list)"
        ages = f"{t.get('age_min')}-{t.get('age_max', 65)}"
        reg = "IL compliance ✓" if a["regional"] else ""
        po = a["promoted_object"] or {}
        print(f"  Ad set {i + 1}: {a['name']} | {a['optimization_goal']} ({po.get('custom_event_type', po.get('page_id', '-'))}) | "
              f"{geo_label(t['geo_locations'])} {ages} | {adv} | placements: {'manual' if 'publisher_platforms' in t else 'Advantage+'} {reg}")
        extras = []
        if a.get("daily_budget"):
            extras.append(f"{a['daily_budget'] / 100:.0f} {cur}/day {a['bid_strategy']}" + (f" bid {a['bid_amount'] / 100:.2f}" if a.get("bid_amount") else ""))
        elif a.get("bid_amount"):
            extras.append(f"bid {a['bid_amount'] / 100:.2f} (campaign strategy)")
        if a.get("end_time"):
            extras.append(f"ends {a['end_time']}")
        extras.append(f"include {len(t.get('custom_audiences', []))} / exclude {len(t.get('excluded_custom_audiences', []))} audiences")
        print("     " + " | ".join(extras))
        for j, ad in enumerate(a["ads"]):
            c = ad["creative"]
            src = c["assets"].get("image") or c["assets"].get("video") or c["assets"].get("image_hash") or c["assets"].get("video_id") or f"{len(c['assets'].get('cards', []))} cards"
            dest = f"lead form {c['lead_form_id']}" if c.get("lead_form_id") else f"link {c['link']}"
            print(f"     Ad {j + 1}: {ad['name']} | {c['type']} | {Path(str(src)).name} | CTA {c['cta']} | {dest}"
                  f"{' | UTM' if c['utm'] else ''} | AI enhancements off")
            n_ads += 1
    print(f"Totals: 1 campaign, {len(res['adsets'])} ad sets, {n_ads} ads. Everything is created PAUSED.")
    for w in res["_warnings"]:
        print(f"  warning: {w}")
    for e in res["_errors"]:
        print(f"  ERROR: {e}")
    if res["_errors"]:
        print("Fix the errors above before validate/preview/create.")
    else:
        print(f"Approval hash: {res['hash']}  (create with: launch.py create <spec> --approve {res['hash']})")


# ----------------------------------------------------------------------------- Meta payload builders
def campaign_params(res: dict) -> dict:
    c = res["campaign"]
    p = {
        "name": c["name"], "objective": c["objective"], "status": "PAUSED", "buying_type": "AUCTION",
        "special_ad_categories": json.dumps(c["special_ad_categories"]),
    }
    if c.get("bid_strategy"):  # CBO only; in ABO bidding lives on the ad sets
        p["bid_strategy"] = c["bid_strategy"]
    if c.get("budget_mode") == "ABO":  # Meta requires an explicit choice; false = every ad set keeps its own budget
        p["is_adset_budget_sharing_enabled"] = "true" if c.get("adset_budget_sharing") else "false"
    if c["daily_budget"]:
        p["daily_budget"] = c["daily_budget"]
    if c["lifetime_budget"]:
        p["lifetime_budget"] = c["lifetime_budget"]
    if c["spend_cap"]:
        p["spend_cap"] = c["spend_cap"]
    return p


def adset_params(res: dict, a: dict, campaign_id: str) -> dict:
    p = {
        "name": a["name"], "campaign_id": campaign_id, "status": "PAUSED",
        "optimization_goal": a["optimization_goal"], "billing_event": a["billing_event"],
        "destination_type": a["destination_type"], "targeting": json.dumps(a["targeting"]),
    }
    if a["promoted_object"]:
        p["promoted_object"] = json.dumps(a["promoted_object"])
    if a.get("daily_budget"):
        p["daily_budget"] = a["daily_budget"]
        p["bid_strategy"] = a["bid_strategy"]
        if a.get("bid_amount"):
            p["bid_amount"] = a["bid_amount"]
    elif a.get("bid_amount"):  # CBO campaign with a capped bid strategy
        p["bid_amount"] = a["bid_amount"]
    if a["regional"]:
        p["regional_regulated_categories"] = json.dumps(a["regional"]["categories"])
        p["regional_regulation_identities"] = json.dumps(a["regional"]["identities"])
    if a.get("start_time"):
        p["start_time"] = a["start_time"]
    if a.get("end_time"):
        p["end_time"] = a["end_time"]
    elif res["campaign"].get("lifetime_budget") and res["campaign"].get("end_time"):
        p["end_time"] = res["campaign"]["end_time"]
    return p


def enhancements_spec() -> dict:
    """degrees_of_freedom_spec: every generative / text-altering Advantage+ creative feature OPT_OUT."""
    return {"creative_features_spec": {f: {"enroll_status": "OPT_OUT"} for f in ENHANCEMENTS_OFF}}


def creative_spec(res: dict, c: dict, hashes: dict) -> dict:
    """object_story_spec + extras, from the resolved creative and the uploaded asset hashes."""
    acct = res["account"]
    link = c["link"]
    cta = {"type": c["cta"], "value": {"link": link}}
    if c.get("lead_form_id"):  # instant form: the shape Ads Manager writes (form id + the fb.me placeholder link)
        cta["value"] = {"lead_gen_form_id": c["lead_form_id"], "link": link}
    oss = {"page_id": acct["page_id"]}
    if acct.get("instagram_user_id"):
        oss["instagram_user_id"] = acct["instagram_user_id"]
    if c["type"] == "image":
        ld = {"link": link, "message": c["message"], "image_hash": hashes["image_hash"], "call_to_action": cta}
        if c.get("headline"):
            ld["name"] = c["headline"]
        if c.get("description"):
            ld["description"] = c["description"]
        oss["link_data"] = ld
    elif c["type"] == "video":
        vd = {"video_id": hashes["video_id"], "message": c["message"], "call_to_action": cta}
        if hashes.get("thumbnail_hash"):
            vd["image_hash"] = hashes["thumbnail_hash"]
        if c.get("headline"):
            vd["title"] = c["headline"]
        if c.get("description"):
            vd["link_description"] = c["description"]
        oss["video_data"] = vd
    else:
        children = []
        for k, card in enumerate(c["assets"]["cards"]):
            ch = {"link": card.get("link", link), "image_hash": hashes["cards"][k]}
            if card.get("headline"):
                ch["name"] = card["headline"]
            if card.get("description"):
                ch["description"] = card["description"]
            ch["call_to_action"] = {"type": card.get("cta", c["cta"]), "value": {"link": card.get("link", link)}}
            children.append(ch)
        ld = {"link": link, "message": c["message"], "child_attachments": children, "multi_share_optimized": False}
        if c.get("headline"):
            ld["name"] = c["headline"]
        oss["link_data"] = ld
    spec = {"name": c["name"], "object_story_spec": oss}
    if c.get("utm"):
        spec["url_tags"] = c["utm"]
    spec["degrees_of_freedom_spec"] = enhancements_spec()
    return spec


# ----------------------------------------------------------------------------- ledger
class Ledger:
    """Per-plan record of what was already created, so a re-run never creates twice."""

    def __init__(self, h: str):
        self.dir = OUT_DIR / h
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "ledger.json"
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value):
        self.data[key] = value
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=1))


def upload_image_once(client: MetaClient, act: str, path: str, ledger: Ledger) -> str:
    key = f"image:{path}"
    h = ledger.get(key)
    if not h:
        h = client.upload_image(act, path)["hash"]
        ledger.set(key, h)
    return h


def ensure_assets(client: MetaClient, res: dict, c: dict, ledger: Ledger) -> dict:
    """Upload images/videos once (ledger-cached) and return hashes/ids for the creative."""
    act = res["account"]["ad_account_id"]
    assets = c["assets"]
    out = {}
    if c["type"] == "image":
        out["image_hash"] = assets.get("image_hash") or upload_image_once(client, act, assets["image"], ledger)
    elif c["type"] == "video":
        if assets.get("video_id"):
            out["video_id"] = assets["video_id"]
        else:
            key = f"video:{assets['video']}"
            vid = ledger.get(key)
            if not vid:
                vid = client.upload_video(act, assets["video"], title=c["name"])
                ledger.set(key, vid)
            client.wait_video(vid)
            out["video_id"] = vid
        if assets.get("thumbnail_hash"):
            out["thumbnail_hash"] = assets["thumbnail_hash"]
        thumb = assets.get("thumbnail")
        if not thumb and not assets.get("thumbnail_hash") and assets.get("video") and shutil.which("ffmpeg"):
            # no thumbnail given: take the frame at 1 s
            thumb = str(ledger.dir / (Path(assets["video"]).stem + "-thumb.jpg"))
            if not Path(thumb).exists():
                subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "1", "-i", assets["video"],
                                "-frames:v", "1", "-q:v", "2", thumb], check=True)
        if thumb:
            out["thumbnail_hash"] = upload_image_once(client, act, thumb, ledger)
    else:
        out["cards"] = [card.get("image_hash") or upload_image_once(client, act, card["image"], ledger)
                        for card in assets["cards"]]
    return out


def ensure_creative(client: MetaClient, res: dict, ad: dict, ledger: Ledger, i: int, j: int) -> str:
    """Create the ad creative once (ledger-cached). Creatives are inert objects: no delivery, no spend."""
    ckey = f"creative:{i}:{j}"
    crid = ledger.get(ckey)
    if crid:
        return crid
    act = res["account"]["ad_account_id"]
    hashes = ensure_assets(client, res, ad["creative"], ledger)
    spec = creative_spec(res, ad["creative"], hashes)
    payload = {"name": spec["name"], "object_story_spec": json.dumps(spec["object_story_spec"]),
               "degrees_of_freedom_spec": json.dumps(spec["degrees_of_freedom_spec"])}
    if spec.get("url_tags"):
        payload["url_tags"] = spec["url_tags"]
    try:
        crid = client.post(f"act_{act}/adcreatives", payload)["id"]
    except MetaError as e:
        if e.subcode == DEV_MODE_SUBCODE:
            die("Meta refuses creatives from an app in development mode. Publish the app "
                "(App Dashboard -> Publish), see SETUP.md step 1, then run again.")
        raise
    ledger.set(ckey, crid)
    return crid


def creative_previews(client: MetaClient, crid: str) -> dict:
    """/{creative_id}/previews works with a system-user token."""
    urls = {}
    for fmt in PREVIEW_FORMATS:
        try:
            d = client.get(f"{crid}/previews", ad_format=fmt)
            body = (d.get("data") or [{}])[0].get("body", "")
            m = re.search(r'src="([^"]+)"', body)
            urls[fmt] = m.group(1).replace("&amp;", "&") if m else ""
        except MetaError as e:
            urls[fmt] = f"ERROR: {e.user_msg}"
    return urls


def get_client(env: dict) -> MetaClient:
    return MetaClient(env.get("ACCESS_TOKEN", ""), env.get("APP_SECRET"))


# ----------------------------------------------------------------------------- commands
def cmd_plan(args):
    res = resolve(args.spec)
    print_plan(res)
    (OUT_DIR / res["hash"]).mkdir(parents=True, exist_ok=True)
    (OUT_DIR / res["hash"] / "plan.json").write_text(json.dumps(res, ensure_ascii=False, indent=1))
    return 1 if res["_errors"] else 0


def cmd_validate(args):
    res = resolve(args.spec)
    print_plan(res)
    if res["_errors"]:
        return 1
    client = get_client(load_env())
    act = res["account"]["ad_account_id"]
    try:
        client.validate(f"act_{act}/campaigns", campaign_params(res))
        print("Meta validate_only: campaign payload OK (nothing created).")
    except MetaError as e:
        print(f"Meta validate_only: campaign REJECTED -> {e}")
        return 1
    print("Ad sets are validated with validate_only right before each create (they need a campaign id).")
    return 0


def cmd_preview(args):
    res = resolve(args.spec)
    if res["_errors"]:
        print_plan(res)
        return 1
    client = get_client(load_env())
    ledger = Ledger(res["hash"])
    previews = {}
    for i, a in enumerate(res["adsets"]):
        for j, ad in enumerate(a["ads"]):
            crid = ensure_creative(client, res, ad, ledger, i, j)
            urls = creative_previews(client, crid)
            urls["creative_id"] = crid
            previews[f"adset{i + 1}/ad{j + 1} {ad['name']}"] = urls
    (ledger.dir / "previews.json").write_text(json.dumps(previews, ensure_ascii=False, indent=1))
    print(f"\n=== PREVIEWS for plan {res['hash']} (assets + creatives created; no campaign, ad set or ad) ===")
    for name, urls in previews.items():
        print(name)
        for fmt, u in urls.items():
            print(f"   {fmt}: {u}")
    print(f"Saved: {ledger.dir / 'previews.json'}")
    return 0


def cmd_create(args):
    res = resolve(args.spec)
    if res["_errors"]:
        print_plan(res)
        return 1
    if args.approve != res["hash"]:
        die(f"approval hash mismatch: spec resolves to {res['hash']}, you passed {args.approve}. "
            f"Run `plan` again and get the user's approval before creating.")
    client = get_client(load_env())
    act = res["account"]["ad_account_id"]
    ledger = Ledger(res["hash"])
    created = {"campaign": None, "adsets": []}

    cid = ledger.get("campaign")
    if cid:
        print(f"campaign already created in this plan: {cid} (ledger)")
    else:
        cid = client.post(f"act_{act}/campaigns", campaign_params(res))["id"]
        ledger.set("campaign", cid)
        print(f"campaign created PAUSED: {cid}")
    created["campaign"] = cid

    for i, a in enumerate(res["adsets"]):
        akey = f"adset:{i}"
        asid = ledger.get(akey)
        if asid:
            print(f"ad set {i + 1} already created: {asid} (ledger)")
        else:
            params = adset_params(res, a, cid)
            try:
                client.validate(f"act_{act}/adsets", params)
            except MetaError as e:
                print(f"ad set {i + 1} '{a['name']}' REJECTED by validate_only: {e}")
                return 1
            asid = client.post(f"act_{act}/adsets", params)["id"]
            ledger.set(akey, asid)
            print(f"ad set {i + 1} created PAUSED: {asid}")
        ads_out = []
        for j, ad in enumerate(a["ads"]):
            had = ledger.get(f"creative:{i}:{j}")
            crid = ensure_creative(client, res, ad, ledger, i, j)
            print(f"   creative {'reused' if had else 'created'}: {crid}")
            adkey = f"ad:{i}:{j}"
            adid = ledger.get(adkey)
            if not adid:
                ad_payload = {"name": ad["name"], "adset_id": asid, "status": "PAUSED",
                              "creative": json.dumps({"creative_id": crid})}
                if res["account"].get("conversion_domain"):
                    ad_payload["conversion_domain"] = res["account"]["conversion_domain"]
                adid = client.post(f"act_{act}/ads", ad_payload)["id"]
                ledger.set(adkey, adid)
                print(f"   ad {j + 1} created PAUSED: {adid}")
            else:
                print(f"   ad {j + 1} already created: {adid} (ledger)")
            ads_out.append({"id": adid, "creative_id": crid, "name": ad["name"]})
        created["adsets"].append({"id": asid, "name": a["name"], "ads": ads_out})

    summary = {"plan": res["hash"], "account": act, "created": created,
               "ads_manager": ads_manager_url(act, cid)}
    (ledger.dir / "created.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    print("\n=== CREATED (all PAUSED) ===")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    print("Activation is manual, in Ads Manager, after review. This tool never activates.")
    return 0


def cmd_status(args):
    client = get_client(load_env())
    cid = args.object_id
    camp = client.get(cid, fields="id,name,status,effective_status,objective,daily_budget,lifetime_budget,spend_cap,account_id")
    print(json.dumps(camp, ensure_ascii=False, indent=1))
    adsets = client.get(f"{cid}/adsets", fields="id,name,status,effective_status,optimization_goal,learning_stage_info", limit=50)
    for a in adsets.get("data", []):
        print(f"ad set {a['id']} | {a['name']} | {a.get('effective_status')} | learning: {(a.get('learning_stage_info') or {}).get('status', '-')}")
    ads = client.get(f"{cid}/ads", fields="id,name,adset_id,status,effective_status,ad_review_feedback,issues_info,preview_shareable_link", limit=100)
    for ad in ads.get("data", []):
        fb = ad.get("ad_review_feedback") or ad.get("issues_info") or ""
        print(f"ad {ad['id']} | {ad['name']} | {ad.get('effective_status')} | {ad.get('preview_shareable_link', '')} {fb if fb else ''}")
    return 0


def cmd_pause(args):
    client = get_client(load_env())
    d = client.post(args.object_id, {"status": "PAUSED"})
    print(f"paused {args.object_id}: {d}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "validate", "preview"):
        sub.add_parser(name).add_argument("spec")
    s = sub.add_parser("create")
    s.add_argument("spec")
    s.add_argument("--approve", required=True, help="approval hash printed by `plan`")
    sub.add_parser("status").add_argument("object_id", help="campaign id")
    sub.add_parser("pause").add_argument("object_id", help="campaign / ad set / ad id")
    args = ap.parse_args()
    fn = {"plan": cmd_plan, "validate": cmd_validate, "preview": cmd_preview, "create": cmd_create,
          "status": cmd_status, "pause": cmd_pause}[args.cmd]
    try:
        sys.exit(fn(args))
    except MetaError as e:
        print(f"Meta API error: {e}", file=sys.stderr)
        sys.exit(3)


if __name__ == "__main__":
    main()
