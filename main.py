import os, json, time, imaplib, email, requests
from datetime import datetime, timedelta, timezone
from google.oauth2 import service_account
from googleapiclient.discovery import build
import pytz

KEY = os.getenv("ANTHROPIC_KEY")
EMAIL = os.getenv("GMAIL_ADDRESS")
PWD = os.getenv("GMAIL_PASSWORD")
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_ID = os.getenv("TELEGRAM_CHAT_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT")
FORCE_BRIEFING = os.getenv("FORCE_BRIEFING") == "1"

PROCESSED_FILE = "processed_uids.txt"
DAILY_DIGEST_FILE = "daily_digest.json"
NZ_TZ = pytz.timezone('Pacific/Auckland')

def get_calendar_service():
    try:
        creds_dict = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        creds = service_account.Credentials.from_service_account_info(
            creds_dict,
            scopes=['https://www.googleapis.com/auth/calendar.readonly']
        )
        return build('calendar', 'v3', credentials=creds)
    except Exception as e:
        print(f"Calendar service error: {e}")
        return None

def check_calendar_availability():
    try:
        service = get_calendar_service()
        if not service:
            return "Calendar unavailable"
        now = datetime.now(timezone.utc)
        end = now + timedelta(days=1)
        events_result = service.events().list(
            calendarId='primary',
            timeMin=now.isoformat(),
            timeMax=end.isoformat(),
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        events = events_result.get('items', [])
        if events:
            next_event = events[0]
            start_time = next_event['start'].get('dateTime', next_event['start'].get('date'))
            return f"Next meeting: {next_event.get('summary', 'Busy')} at {start_time}"
        return "No meetings scheduled"
    except Exception as e:
        print(f"Calendar check error: {e}")
        return "Calendar unavailable"

def load_processed_uids():
    if os.path.exists(PROCESSED_FILE):
        with open(PROCESSED_FILE, 'r') as f:
            return set(f.read().strip().split('\n'))
    return set()

def save_processed_uid(uid):
    with open(PROCESSED_FILE, 'a') as f:
        f.write(f"{uid}\n")

def load_daily_digest():
    if os.path.exists(DAILY_DIGEST_FILE):
        with open(DAILY_DIGEST_FILE, 'r') as f:
            return json.load(f)
    return {"emails": [], "last_sent": None}

def save_daily_digest(digest):
    with open(DAILY_DIGEST_FILE, 'w') as f:
        json.dump(digest, f)

def mark_email_unread(email_id):
    try:
        m = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        m.login(EMAIL, PWD)
        m.select("INBOX")
        m.store(email_id, '-FLAGS', '\\Seen')
        m.close()
        m.logout()
    except Exception as e:
        print(f"Error marking unread: {e}")

def get_email_body(msg):
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    body = payload.decode(errors='ignore')[:1000]
                    break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            body = payload.decode(errors='ignore')[:1000]
    return body

def decode_subject(raw):
    try:
        from email.header import decode_header
        parts = decode_header(raw)
        out = ""
        for text, enc in parts:
            if isinstance(text, bytes):
                out += text.decode(enc or "utf-8", errors="ignore")
            else:
                out += text
        return out
    except Exception:
        return raw

def get_new_emails():
    try:
        m = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        m.login(EMAIL, PWD)
        m.select("INBOX")
        _, msgs = m.search(None, "UNSEEN")
        processed = load_processed_uids()
        new_emails = []
        for eid in msgs[0].split():
            eid_str = eid.decode()
            if eid_str not in processed:
                _, d = m.fetch(eid, "(RFC822)")
                msg = email.message_from_bytes(d[0][1])
                subj = decode_subject(msg.get("Subject", "(no subject)"))
                fr = msg.get("From", "unknown")
                body = get_email_body(msg)
                new_emails.append({'uid': eid_str, 'from': fr, 'subject': subj, 'body': body})
        m.close()
        m.logout()
        return new_emails
    except Exception as e:
        print(f"Email error: {e}")
        return []

def analyze_email(fr, subj, body):
    calendar_status = check_calendar_availability()
    prompt = f"""Return valid JSON only. No other text.

{{"summary": "1-2 sentence summary", "category": "Urgent|Needs Reply|FYI|Follow-up|Course/Admin", "draft_reply": "Brief reply or No reply needed"}}

Analyze this email:
Subject: {subj}
From: {fr}
Body: {body}

If subject or body contains URGENT, ASAP, EMERGENCY, CRITICAL, IMMEDIATE or HELP NOW - categorize as Urgent.
Calendar: {calendar_status}"""
    try:
        r = requests.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-haiku-5-5", "max_tokens": 300, "messages": [{"role": "user", "content": prompt}]},
            timeout=30)
        if r.status_code == 200:
            blocks = r.json().get("content", [])
            if blocks:
                text = blocks[0].get("text", "").strip()
                if text.startswith("```"):
                    text = text.split("```")[1].lstrip("json").strip()
                try:
                    return json.loads(text)
                except Exception:
                    return {"summary": text[:100], "category": "Needs Reply", "draft_reply": "N/A"}
        else:
            print(f"Claude API error {r.status_code}: {r.text[:200]}")
        return {"summary": "API error", "category": "Needs Reply", "draft_reply": "N/A"}
    except Exception as e:
        print(f"Claude error: {e}")
        return {"summary": "Error", "category": "Needs Reply", "draft_reply": "N/A"}

def is_urgent(subj, category):
    keywords = ["urgent", "asap", "emergency", "critical", "immediate", "help now", "need help"]
    return any(k in subj.lower() for k in keywords) or category == "Urgent"

def send_telegram(msg):
    chunks = [msg[i:i + 3900] for i in range(0, len(msg), 3900)] or [msg]
    for chunk in chunks:
        try:
            r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_ID, "text": chunk}, timeout=10)
            print("Telegram sent" if r.status_code == 200 else f"Telegram error: {r.status_code} {r.text[:200]}")
        except Exception as e:
            print(f"Telegram error: {e}")
        time.sleep(1)

def briefing_due(digest):
    nz_now = datetime.now(NZ_TZ)
    today = nz_now.strftime("%Y-%m-%d")
    return nz_now.hour >= 8 and digest.get("last_sent") != today

def send_daily_briefing(digest):
    nz_today = datetime.now(NZ_TZ).strftime("%Y-%m-%d")
    if digest["emails"]:
        msg = "📧 DAILY EMAIL BRIEFING\n\n"
        for idx, e in enumerate(digest["emails"], 1):
            msg += f"{idx}. From: {e['from']}\n"
            msg += f"   Subject: {e['subject']}\n"
            msg += f"   Summary: {e['analysis'].get('summary', 'N/A')}\n"
            msg += f"   Category: {e['analysis'].get('category', 'N/A')}\n"
            msg += f"   Draft: {e['analysis'].get('draft_reply', 'N/A')}\n\n"
    else:
        msg = "📧 DAILY EMAIL BRIEFING\n\nNo new non-urgent emails since the last briefing."
    send_telegram(msg)
    print("Daily briefing sent")
    digest["emails"] = []
    digest["last_sent"] = nz_today
    save_daily_digest(digest)

print("Email Assistant started")
send_telegram("✅ Email assistant is online and checking your inbox every 20 minutes.")

first_loop = True

while True:
    try:
        emails = get_new_emails()
        digest = load_daily_digest()

        if emails:
            print(f"Found {len(emails)} emails")
            for e in emails:
                print(f"Processing: {e['subject']}")
                analysis = analyze_email(e['from'], e['subject'], e['body'])

                if is_urgent(e['subject'], analysis.get("category")):
                    send_telegram(f"""🚨 URGENT EMAIL
From: {e['from']}
Subject: {e['subject']}

Summary: {analysis.get('summary', 'N/A')}

Draft Reply: {analysis.get('draft_reply', 'N/A')}""")
                else:
                    digest["emails"].append({"from": e['from'], "subject": e['subject'], "analysis": analysis})

                save_processed_uid(e['uid'])
                mark_email_unread(e['uid'])
                time.sleep(1)
            save_daily_digest(digest)

        if (FORCE_BRIEFING and first_loop) or briefing_due(digest):
            send_daily_briefing(digest)
        else:
            print(f"Checked at {datetime.now(NZ_TZ)}")

        first_loop = False
        time.sleep(1200)

    except KeyboardInterrupt:
        print("Stopped")
        break
    except Exception as e:
        print(f"Error: {e}")
        time.sleep(60)
