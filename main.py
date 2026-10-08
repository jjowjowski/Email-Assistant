import os, json, time, imaplib, email, requests
from email.header import decode_header
from datetime import datetime, timedelta
from google.oauth2 import service_account
from googleapiclient.discovery import build

KEY = os.getenv("ANTHROPIC_KEY")
EMAIL = os.getenv("GMAIL_ADDRESS")
PWD = os.getenv("GMAIL_PASSWORD")
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_ID = os.getenv("TELEGRAM_CHAT_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT")

PROCESSED_FILE = "processed_uids.txt"

def get_calendar_service():
    try:
        creds_dict = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        creds = service_account.Credentials.from_service_account_info(creds_dict, scopes=['https://www.googleapis.com/auth/calendar.readonly'])
        return build('calendar', 'v3', credentials=creds)
    except Exception as e:
        print(f"Calendar service error: {e}")
        return None

def check_calendar_availability():
    try:
        service = get_calendar_service()
        if not service:
            return "Calendar unavailable"
        
        now = datetime.utcnow()
        end = now + timedelta(days=1)
        
        events_result = service.events().list(
            calendarId='primary',
            timeMin=now.isoformat() + 'Z',
            timeMax=end.isoformat() + 'Z',
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        
        events = events_result.get('items', [])
        
        if events:
            next_event = events[0]
            start_time = next_event['start'].get('dateTime', next_event['start'].get('date'))
            return f"Calendar: Next meeting is {next_event['summary']} at {start_time}"
        else:
            return "Calendar: You're free tomorrow"
    except Exception as e:
        print(f"Calendar check error: {e}")
        return "Calendar: unable to check"

def load_processed_uids():
    if os.path.exists(PROCESSED_FILE):
        with open(PROCESSED_FILE, 'r') as f:
            return set(f.read().strip().split('\n'))
    return set()

def save_processed_uid(uid):
    with open(PROCESSED_FILE, 'a') as f:
        f.write(f"{uid}\n")

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
                subj = msg.get("Subject", "(no subject)")
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
    
    prompt = f"""Analyze this email and respond ONLY with valid JSON (no other text):

From: {fr}
Subject: {subj}
Body: {body}

Calendar status: {calendar_status}

{{
    "summary": "1-2 sentence summary of what the email is asking/saying",
    "category": "one of: Urgent, Needs Reply, FYI, Follow-up, Course/Admin",
    "draft_reply": "A brief professional draft reply (2-3 sentences). If meeting requested, reference your calendar availability. If no reply needed, say 'No reply needed.'"
}}"""
    
    try:
        r = requests.post("https://api.anthropic.com/v1/messages", 
            headers={"x-api-key": KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"}, 
            json={"model": "claude-haiku-5-5", "max_tokens": 300, "messages": [{"role": "user", "content": prompt}]},
            timeout=30)
        
        if r.status_code == 200:
            text = r.json()["content"][0]["text"].strip()
            if text.startswith("```"):
                text = text.split("```")[1].lstrip("json").strip()
            return json.loads(text)
        return {"summary": "Error", "category": "Needs Reply", "draft_reply": "Error analyzing"}
    except Exception as e:
        print(f"Claude error: {e}")
        return {"summary": "Error", "category": "Needs Reply", "draft_reply": "Error analyzing"}

def send_telegram(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_ID, "text": msg, "parse_mode": "HTML"})
        print("Telegram sent")
    except Exception as e:
        print(f"Telegram error: {e}")

print("Personal Email Assistant started with Calendar access")

while True:
    try:
        emails = get_new_emails()
        if emails:
            print(f"Found {len(emails)} new emails")
            for e in emails:
                print(f"Processing: {e['subject']}")
                analysis = analyze_email(e['from'], e['subject'], e['body'])
                
                msg = f"""<b>📧 NEW EMAIL</b>
<b>From:</b> {e['from']}
<b>Subject:</b> {e['subject']}

<b>📋 Summary:</b> {analysis['summary']}

<b>🏷️ Category:</b> {analysis['category']}

<b>✍️ Draft Reply:</b>
{analysis['draft_reply']}"""
                
                send_telegram(msg)
                save_processed_uid(e['uid'])
                time.sleep(1)
        else:
            print(f"No new emails at {datetime.now()}")
        
        print("Waiting 20 minutes...")
        time.sleep(1200)
    except KeyboardInterrupt:
        print("Stopped")
        break
    except Exception as e:
        print(f"Error: {e}")
        time.sleep(60)
