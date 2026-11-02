---
name: meta-launch
description: >
  העלאת קמפיינים למטא (פייסבוק/אינסטגרם) מתוך בריף, דרך ה-Marketing API הרשמי,
  עם אישור אנושי לפני כל יצירה, והכל נולד PAUSED. הפעל כשהמשתמש מבקש "להעלות קמפיין",
  "לבנות קמפיין במטא", "להריץ את הבאנרים כמודעות", "לעשות ad set / מודעות", או מזכיר
  meta-launch, גם בלי המילה "סקיל". לא לניתוח ודוחות, לא להפעלה של קמפיינים (תמיד ידני
  ב-Ads Manager), ולא לחשבונות שאינם ב-accounts.json.
---

# meta-launch: בריף → spec → אישור → קמפיין PAUSED

מבריף בעברית ומקבצי באנרים/וידאו לקמפיין מלא במטא (קמפיין → ad sets → creatives → מודעות),
בלי סיכון לחשבון: כל יצירה עוברת אישור של המשתמש, הכל נוצר PAUSED, וההפעלה תמיד ידנית.

אם זו הפעם הראשונה ואין `.env` או `accounts.json` בתיקיית הסקיל: לעצור ולהפנות ל-`SETUP.md`.

## כללי ברזל (לא לשבור, גם אם המשתמש ממהר)

1. **רק חשבונות מ-`accounts.json`** עם `write_allowed: true`. חשבון חדש נכנס לרשימה רק
   בפעולה מפורשת של המשתמש.
2. **אין ACTIVE ביצירה, אין מחיקה.** הסקריפט לא יודע להפעיל ולא למחוק. הפעלה = המשתמש,
   ב-Ads Manager, אחרי שבדק.
3. **אישור לפני יצירה.** `plan` מדפיס סיכום ו-hash. מציגים למשתמש את הסיכום ואת קישורי
   ה-preview, ורק אחרי אישור מפורש מריצים `create --approve <hash>`. כל שינוי ב-spec = hash
   חדש = אישור חדש.
4. **תקציבים:** יומי עד `max_daily_budget_ils` של החשבון (ברירת מחדל 300 ₪); מעל זה רק עם
   `override_budget_guard: true` ב-spec ואישור מפורש. `spend_cap_ils` חובה, מינימום 500 ₪
   (המינימום של מטא בשקלים).
5. **קצב:** הסקריפט משהה בין כתיבות ועוצר ב-75% מהמכסה של מטא. אין retry על יצירה.
6. **ישראל:** כל ad set שמכוון ל-IL מקבל אוטומטית את שדות המפרסם המאומת
   (`regional_regulated_categories` + `regional_regulation_identities`) מבלוק `regional`
   ב-`accounts.json`. בלי זה מטא דוחה: "Advertiser is missing". עם Advantage+ audience לא
   שולחים `age_max`, ו-`age_min` חייב להיות 18-25.
7. **קריאייטיב:** כל שיפורי ה-Advantage+ creative הגנרטיביים כבויים (טקסטים, תבניות, מוזיקה,
   תרגום, אנימציה...). המודעה רצה בדיוק כמו שהיא בקובץ.
8. **הטוקן** נקרא מ-`.env` בתיקיית הסקיל. לא מדפיסים אותו, לא מבקשים אותו בשיחה, לא
   מעתיקים אותו לשום קובץ אחר.

## התהליך

### 1. בריף → spec
לקרוא את הבריף ולכתוב `specs/<שם>.json` לפי הסכימה למטה (דוגמאות ב-`examples/`).
לשאול את המשתמש רק מה שחסר באמת: תקציב, מטרה, קהל, קישור נחיתה.
ברירות מחדל טובות: CBO, `OUTCOME_LEADS` עם פיקסל LEAD (או `OUTCOME_SALES` עם PURCHASE),
ישראל, Advantage+ audience, Advantage+ placements, CTA `LEARN_MORE`, UTM עם
`{{campaign.name}}` ו-`{{ad.name}}`.

מודעה לכל באנר או סרטון. מידות: 4:5 (1080×1350) לפיד, 9:16 לסטורי/רילס; קרוסלה 2-10
כרטיסים. בווידאו `thumbnail` אופציונלי (בלעדיו נחתך פריים מהשנייה הראשונה, אם ffmpeg מותקן).
וידאו מעל 40MB עולה אוטומטית בהעלאה מחולקת.

### 2. plan + validate
```bash
python3 ~/.claude/skills/meta-launch/scripts/launch.py plan specs/<file>.json
python3 ~/.claude/skills/meta-launch/scripts/launch.py validate specs/<file>.json
```
`plan` בודק את ה-spec מול כללי החשבון (לא פונה למטא). `validate` שולח את הקמפיין למטא
ב-`validate_only`: מטא בודקת ולא נוצר כלום. ad sets עוברים `validate_only` רגע לפני היצירה שלהם.

### 3. preview
```bash
python3 ~/.claude/skills/meta-launch/scripts/launch.py preview specs/<file>.json
```
מעלה את התמונות/סרטונים לחשבון ויוצר creatives (אובייקטים שלא רצים ולא עולים כסף, בלי קמפיין
ובלי מודעות), ומחזיר קישורי preview של מטא לפיד פייסבוק, פיד אינסטגרם וסטורי.
להציג למשתמש את הסיכום של `plan` + הקישורים, ולבקש אישור.

### 4. אישור → create
רק אחרי אישור מפורש:
```bash
python3 ~/.claude/skills/meta-launch/scripts/launch.py create specs/<file>.json --approve <hash>
```
נוצר: קמפיין PAUSED → לכל ad set: `validate_only` → יצירה PAUSED → creatives → מודעות PAUSED.
ה-ledger ב-`out/<hash>/ledger.json` מונע יצירה כפולה אם מריצים שוב אחרי תקלה.
להחזיר למשתמש את קישור ה-Ads Manager ולהזכיר שההפעלה ידנית.

### 5. status / pause
```bash
python3 ~/.claude/skills/meta-launch/scripts/launch.py status <campaign_id>
python3 ~/.claude/skills/meta-launch/scripts/launch.py pause <campaign|adset|ad id>
```
`status` מציג סטטוס, שלב למידה ומשוב בדיקה של כל מודעה. `pause` הוא כפתור העצירה היחיד של הסקיל.

## סכימת ה-spec

```json
{
  "account": "my-business",
  "campaign": {"name": "…", "objective": "OUTCOME_LEADS", "daily_budget_ils": 80,
               "spend_cap_ils": 500, "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
               "special_ad_categories": []},
  "adsets": [{
    "name": "…", "optimization_goal": "OFFSITE_CONVERSIONS", "conversion_event": "LEAD",
    "targeting": {"countries": ["IL"], "age_min": 25, "advantage_audience": true},
    "placements": "advantage",
    "ads": [{"name": "…", "creative": {
       "type": "image | video | carousel",
       "image": "path.png", "video": "path.mp4", "thumbnail": "path.jpg",
       "cards": [{"image": "path.png", "headline": "…", "description": "…", "link": "…"}],
       "message": "טקסט ראשי", "headline": "כותרת", "description": "תיאור",
       "link": "https://…", "cta": "LEARN_MORE", "utm": "utm_source=facebook&…"}}]
  }]
}
```

עוד אפשרויות:
- **תקציב:** `lifetime_budget_ils` + `campaign.end_time` במקום יומי. ABO: `"budget_mode": "ABO"` בקמפיין
  (בלי תקציב קמפיין) ו-`daily_budget_ils` בכל ad set, אופציונלית `bid_strategy` + `bid_amount_ils`.
  ב-CBO עם `COST_CAP` / `LOWEST_COST_WITH_BID_CAP`: `bid_amount_ils` בכל ad set.
- **קהל:** `age_max` (רק כש-`advantage_audience: false`), `genders` (1 גברים, 2 נשים), `locales`,
  `custom_audiences` / `excluded_custom_audiences` (מזהי קהלים; עם קהל ידני הרשימה נשארת סגורה),
  `custom_locations` לרדיוס סביב נקודה: `[{"latitude", "longitude", "radius", "distance_unit": "kilometer"}]`.
- **מיקומים ידניים:** `"placements": {"publisher_platforms": ["facebook", "instagram"], "instagram_positions": ["stream", "story", "reels"], ...}`.
- **טופס לידים של מטא:** `optimization_goal: "LEAD_GENERATION"`, `destination_type: "ON_AD"` ב-ad set,
  ו-`lead_form_id` בקריאייטיב (תמונה או וידאו). את הטופס בונים ב-Ads Manager או ב-Meta Business Suite.
- **נכס שכבר בחשבון:** `image_hash` / `video_id` / `thumbnail_hash` במקום קובץ.
- **תאריכים:** `start_time` / `end_time` ב-ad set, בפורמט `2026-11-10T09:00:00+02:00`.
- **קטגוריות מיוחדות:** `special_ad_categories` חובה למודעות דיור, תעסוקה (גיוס), אשראי ופוליטיקה,
  למשל `["EMPLOYMENT"]`. מטא מגבילה בהן את הטרגוט.
- `optimization_goal` חייב להתאים למטרת הקמפיין; `plan` מדפיס את הרשימה המותרת כשיש טעות.

## מה הסקיל לא עושה
- לא מפעיל, לא מוחק, לא עורך קמפיינים קיימים (תקציב, קהל, קריאייטיב). זה ב-Ads Manager.
- לא כותב לחשבונות שלא ברשימה, גם אם לטוקן יש גישה אליהם.
- לא בונה טפסי לידים, קהלים או המרות מותאמות. משתמש במזהים של מה שכבר קיים בחשבון.

## קבצים
- `scripts/launch.py`: הפקודות למעלה.
- `scripts/metaapi.py`: client דק ל-Graph API v26.0 (רישום כל כתיבה, השהיות, עצירה לפני מכסה, retry רק על תקלות זמניות).
- `scripts/check_setup.py`: בדיקת הקמה, קריאה בלבד.
- `accounts.json`: רשימת החשבונות המותרים (לא עולה לגיט). `.env`: הטוקן (לא עולה לגיט).
- `out/<hash>/`: plan, ledger, previews, created. `audit/audit.jsonl`: כל כתיבה למטא, בלי הטוקן.
