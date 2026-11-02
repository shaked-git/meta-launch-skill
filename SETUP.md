# הקמה: מאפס עד קמפיין ראשון

פעם אחת, בערך 30-45 דקות. צריך: הרשאת אדמין בתיק העסקי (Business portfolio) שמחזיק את חשבון
המודעות ואת עמוד הפייסבוק, Claude Code מותקן, ו-Python 3.9 ומעלה (`python3 --version`).
אופציונלי: `ffmpeg`, בשביל תמונה ממוזערת אוטומטית לסרטונים.

## 1. אפליקציה במטא למפתחים

1. להיכנס ל-https://developers.facebook.com/apps (אם צריך, לפתוח חשבון מפתח עם אותו משתמש פייסבוק).
2. **Create app**. שם: משהו כמו "Claude Ads Launcher".
3. Use case: **Create & manage ads with Marketing API**. אם לא מופיע: **Other** ואז סוג **Business**.
4. לחבר את האפליקציה לתיק העסקי שלכם (Business portfolio).
5. ב-App settings → Basic למלא מה שמטא מבקשת לפרסום (בדרך כלל כתובת מדיניות פרטיות, קטגוריה ואייקון).
6. **Publish** (בתפריט של האפליקציה). חובה: אפליקציה שלא פורסמה לא יכולה ליצור מודעות עם פוסט של עמוד
   (שגיאה 1885183, "app is in development mode"). הפרסום לא פותח את האפליקציה לאנשים אחרים, היא נשארת שלכם.

## 2. משתמש מערכת (System user)

משתמש מערכת הוא "עובד" דיגיטלי של התיק העסקי. הטוקן שלו לא תלוי בסיסמה האישית שלכם ולא פג כשמתנתקים.

1. https://business.facebook.com/settings → **Users → System users → Add**.
2. שם: למשל `claude-launch`. תפקיד: **Employee** (מספיק; Admin רק אם בשלב 4 לא מופיעות כל ההרשאות).

## 3. שיוך נכסים למשתמש המערכת

באותו מסך, על משתמש המערכת: **Assign assets** (או Add assets):

| נכס | הרשאה |
|---|---|
| Apps → האפליקציה משלב 1 | Manage app / Full control |
| Ad accounts → חשבון המודעות | **Manage campaigns** |
| Pages → עמוד הפייסבוק | **Create ads** (אפשר גם Create content) |

חשבון האינסטגרם צריך להיות מחובר לעמוד (Business settings → Accounts → Instagram accounts), כדי שהמודעות
ירוצו בשמו.

## 4. טוקן

1. על משתמש המערכת: **Generate new token**.
2. לבחור את האפליקציה משלב 1.
3. תפוגה: 60 יום (ולשים תזכורת ביומן לחידוש) או ללא תפוגה. ללא תפוגה נוח יותר, ודורש שמירה טובה יותר.
4. הרשאות, בדיוק ארבע: `ads_management`, `ads_read`, `pages_show_list`, `business_management`.
5. **Generate** ולהעתיק. מטא מציגה את הטוקן פעם אחת בלבד.

הטוקן הזה שולט בקמפיינים שלכם. לא מדביקים אותו בצ'אט עם Claude, לא בשום מסמך ולא בגיט.
אם דלף: באותו מסך, **Revoke tokens**, ומייצרים חדש.

## 5. התקנת הסקיל

```bash
git clone https://github.com/shaked-git/meta-launch-skill.git ~/.claude/skills/meta-launch
cd ~/.claude/skills/meta-launch
cp .env.example .env
cp accounts.example.json accounts.json
```

לפתוח את `.env` בעורך טקסט (לא דרך Claude) ולהדביק את הטוקן אחרי `ACCESS_TOKEN=`.
`APP_SECRET` אופציונלי: App settings → Basic → App secret. כשהוא קיים, כל קריאה נחתמת בו.

## 6. accounts.json: המזהים של החשבון

המפתח (`my-business` בדוגמה) הוא השם שתכתבו ב-spec. אפשר כמה חשבונות, בלוק לכל אחד.

| שדה | מאיפה |
|---|---|
| `ad_account_id` | Ads Manager: המספר אחרי `act=` בכתובת. בלי `act_` |
| `business_id` | Business settings → Business info, או המספר אחרי `business_id=` בכתובת |
| `page_id` | Business settings → Accounts → Pages, או בעמוד: About → Page transparency |
| `instagram_user_id` | לא שם המשתמש. בדיקת ההקמה (שלב 7) מדפיסה אותו מתוך העמוד |
| `pixel_id` | Events Manager → מקור הנתונים → המספר מתחת לשם הפיקסל |
| `conversion_domain` | הדומיין של דף הנחיתה, בלי `https://` ובלי `www` (רצוי מאומת ב-Brand safety → Domains) |
| `currency` | המטבע של חשבון המודעות. כל הסכומים ב-spec הם ביחידות שלו (השדות נקראים `_ils`) |
| `default_link` | קישור ברירת מחדל למודעה בלי `link` |
| `max_daily_budget_ils` | התקרה היומית שהסקיל לא יעבור בלי אישור מפורש. ברירת מחדל 300 |
| `min_spend_cap_ils` | המינימום של מטא לתקרת הוצאה בקמפיין. בשקלים 500 |
| `regional` | ראו למטה. חובה לכל מודעה שמכוונת לישראל |
| `write_allowed` | להשאיר `false` עד שבדיקת ההקמה עוברת, ואז `true` |

### הבלוק `regional` (ישראל)

מטא דורשת בכל ad set שמכוון לישראל מפרסם מאומת: מי המוטב ומי המשלם. בלי זה היצירה נדחית
עם "Advertiser is missing". מי שכבר הריץ מודעות לישראל מ-Ads Manager מילא את זה שם (מטא מבקשת
בפעם הראשונה). הדרך הכי בטוחה להשיג את הערכים: מזהה של ad set קיים שרץ בישראל (Ads Manager →
לשונית Ad sets → עמודת ID), ולהריץ:

```bash
python3 ~/.claude/skills/meta-launch/scripts/check_setup.py my-business <adset_id>
```

הפלט מדפיס את `regional_regulated_categories` ואת `regional_regulation_identities` של אותו ad set.
את הקטגוריה מעתיקים ל-`categories`, ואת המזהה של `universal_beneficiary` ל-`identity_business_id`.
אם אין עדיין ad set כזה: ליצור אחד ב-Ads Manager (אפשר להשאיר אותו כבוי), למלא שם את פרטי המפרסם, ולקרוא ממנו.

## 7. בדיקת הקמה

```bash
python3 ~/.claude/skills/meta-launch/scripts/check_setup.py my-business
```

קריאה בלבד, לא כותב כלום. בודק שהטוקן עובד ושיש לו את ארבע ההרשאות, שחשבון המודעות פעיל,
שהעמוד, האינסטגרם והפיקסל נגישים, שבלוק ישראל קיים, ומדפיס את רמת הגישה של האפליקציה
(`development_access` מספיקה לעסק קטן; `standard_access` נותנת מכסות גבוהות יותר, דרך App Review).
לתקן כל שורת FAIL, ואז לשנות `write_allowed` ל-`true`.

## 8. קמפיין ראשון

לפתוח את Claude Code בתיקייה שבה הבאנרים, ולכתוב משהו כמו:

> תעלה קמפיין לידים במטא לחשבון my-business: 80 ₪ ליום, תקרה 500, ישראל 25+, שלושת הבאנרים בתיקייה,
> לינק ל-https://... , טקסט: ...

Claude כותב spec, מריץ `plan` ו-`validate` (לא נוצר כלום), אחר כך `preview` ומחזיר קישורי תצוגה.
רק אחרי שתאשרו בצ'אט הוא מריץ `create`. הכל נוצר כבוי (PAUSED): עוברים ב-Ads Manager על הקמפיין
ומדליקים בעצמכם.

רוצים לבדוק בלי Claude? יש שתי דוגמאות ב-`examples/` (שימו קבצים משלכם ב-`examples/assets/`):

```bash
python3 ~/.claude/skills/meta-launch/scripts/launch.py plan ~/.claude/skills/meta-launch/examples/spec-image.json
```

## תקלות נפוצות

| הודעה | מה לעשות |
|---|---|
| `1885183` / development mode | האפליקציה לא פורסמה. שלב 1, סעיף 6 |
| Advertiser is missing | חסר בלוק `regional` או שהמזהה בו לא נכון. שלב 6 |
| `(#200)` / permission | נכס לא משויך למשתמש המערכת, או הרשאה חסרה בטוקן. שלבים 3-4 |
| Invalid OAuth / token expired | הטוקן פג או בוטל. שלב 4, ולהחליף ב-`.env` |
| age_min must be 18..25 | עם Advantage+ audience מטא מגבילה את גיל המינימום. לגיל גבוה יותר: `advantage_audience: false` |
| Rate-limit usage at 75% | הסקיל עצר לפני המכסה של מטא. לחכות את הזמן שמודפס ולהריץ שוב (ה-ledger ממשיך מאיפה שעצר) |
