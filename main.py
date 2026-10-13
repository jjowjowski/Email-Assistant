import os, json, time, imaplib, email, requests
from email.message import EmailMessage
from email.utils import parseaddr
from email.header import decode_header
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

MY_NAME = "Jett Groshinski"
MY_FIRST = MY_NAME.split()[0]
DRAFTS_FOLDER = '"[Gmail]/Drafts"'
SKIP_SENDERS = ["noreply", "no-reply", "donotreply", "do-not-reply", "notifications", "notify", "mailer-daemon", "railway.app"]

PROCESSED_FILE = "processed_uids.txt"
DAILY_DIGEST_FILE = "daily_digest.json"
NZ_TZ = pytz.timezone('Pacific/Auckland')

def get_calendar_service():
    try:
        creds_dict = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        creds = service_account.Credentials.from_service_account_info(
            creds_dict, scopes=['https://www.googleapis.com/auth/calendar.readonly'])
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
        end = now + timedelta(days=3)
        events = service.events().list(
            calendarId='primary', timeMin=now.isoformat(), timeMax=end.isoformat(),
            singleEvents=True, orderBy='startTime').execute().get('items', [])
        if not events:
            return "No meetings in the next 3 days"
        lines = []
        for ev in events[:8]:
            start = ev['start'].get('dateTime', ev['start'].get('date'))
            lines.append(f"{ev.get('summary', 'Busy')} at {start}")
        return "Upcoming: " + "; ".join(lines)
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
                    body = payload.decode(errors='ignore')[:1500]
                    break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            body = payload.decode(errors='ignore')[:1500]
    return body

def decode_subject(raw):
    try:
        out = ""
        for text, enc in decode_header(raw):
            out += text.decode(enc or "utf-8", errors="ignore") if isinstance(text, bytes) else text
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
                new_emails.append({
                    'uid': eid_str,
                    'from': msg.get("From", "unknown"),
                    'reply_to': msg.get("Reply-To") or msg.get("From", ""),
                    'subject': decode_subject(msg.get("Subject", "(no subject)")),
                    'message_id': msg.get("Message-ID", ""),
                    'references': msg.get("References", ""),
                    'body': get_email_body(msg),
                })
        m.close()
        m.logout()
        return new_emails
    except Exception as e:
        print(f"Email error: {e}")
        return []

def analyze_email(fr, subj, body):
    calendar_status = check_calendar_availability()
    prompt = f"""Return valid JSON only. No other text.

{{"summary": "1-2 sentence summary", "category": "Urgent|Needs Reply|FYI|Follow-up|Course/Admin", "needs_reply": true or false, "tone": "personal or secretary", "draft_body": "full email reply text, or empty string if no reply is needed"}}

STEP 1 - Choose the tone based on who is writing and why.
Use "secretary" (reply written by {MY_NAME}'s personal assistant, third person, e.g. "Hi Sam, thank you for your email. {MY_FIRST} would be glad to...", signed "Assistant to {MY_NAME}") for: recruiters and employers, lecturers and university administration, banks and official bodies, business enquiries, and anyone writing formally or contacting {MY_FIRST} for the first time.
Use "personal" (first person as {MY_FIRST}, warm and natural, signed "Cheers," or "Kind regards," then "{MY_FIRST}") for: friends, family, classmates, and anyone writing casually or who clearly knows {MY_FIRST} well.
If unsure, choose "personal".

STEP 2 - Write draft_body in the chosen tone. Keep it concise.
If a meeting or time is proposed, use the calendar below to accept, decline or suggest alternatives. Never invent facts, commitments or attachments. If you lack information, say you will follow up.
If subject or body contains URGENT, ASAP, EMERGENCY, CRITICAL, IMMEDIATE or HELP NOW, categorize as Urgent.
Newsletters, receipts, automated notifications and marketing never need a reply.

Subject: {subj}
From: {fr}
Body: {body}
Calendar: {calendar_status}"""
    try:
        r = requests.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-haiku-5-5", "max_tokens": 700, "messages": [{"role": "user", "content": prompt}]},
            timeout=45)
        if r.status_code == 200:
            blocks = r.json().get("content", [])
            if blocks:
                text = blocks[0].get("text", "").strip()
                if text.startswith("```"):
                    text = text.split("```")[1].lstrip("json").strip()
                try:
                    return json.loads(text)
                except Exception:
                    return {"summary": text[:100], "category": "Needs Reply", "needs_reply": False, "tone": "personal", "draft_body": ""}
        else:
            print(f"Claude API error {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"Claude error: {e}")
    return {"summary": "Could not analyze", "category": "Needs Reply", "needs_reply": False, "tone": "personal", "draft_body": ""}

def save_draft(e, body_text):
    try:
        to_addr = parseaddr(e['reply_to'])[1]
        if not to_addr or any(s in to_addr.lower() for s in SKIP_SENDERS):
            return False
        msg = EmailMessage()
        msg["From"] = EMAIL
        msg["To"] = to_addr
        subj = e['subject']
        msg["Subject"] = subj if subj.lower().startswith("re:") else f"Re: {subj}"
        if e['message_id']:
            msg["In-Reply-To"] = e['message_id']
            msg["References"] = (e['references'] + " " + e['message_id']).strip()
        msg.set_content(body_text)
        m = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        m.login(EMAIL, PWD)
        status, _ = m.append(DRAFTS_FOLDER, '\\Draft', imaplib.Time2Internaldate(time.time()), msg.as_bytes())
        m.logout()
        print(f"Draft saved ({status}) for: {subj}")
        return status == "OK"
    except Exception as ex:
        print(f"Draft error: {ex}")
        return False

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
        except Exception as ex:
            print(f"Telegram error: {ex}")
        time.sleep(1)

def briefing_due(digest):
    nz_now = datetime.now(NZ_TZ)
    return nz_now.hour >= 8 and digest.get("last_sent") != nz_now.strftime("%Y-%m-%d")

def draft_note(draft_saved, tone):
    if not draft_saved:
        return ""
    voice = "assistant voice" if tone == "secretary" else "your voice"
    return f"✍️ Draft reply waiting in Gmail Drafts ({voice})"

def send_daily_briefing(digest):
    nz_today = datetime.now(NZ_TZ).strftime("%Y-%m-%d")
    if digest["emails"]:
        msg = "📧 DAILY EMAIL BRIEFING\n\n"
        for idx, e in enumerate(digest["emails"], 1):
            msg += f"{idx}. From: {e['from']}\n   Subject: {e['subject']}\n"
            msg += f"   Summary: {e['analysis'].get('summary', 'N/A')}\n"
            msg += f"   Category: {e['analysis'].get('category', 'N/A')}\n"
            note = draft_note(e.get("draft_saved"), e['analysis'].get("tone"))
            if note:
                msg += f"   {note}\n"
            msg += "\n"
    else:
        msg = "📧 DAILY EMAIL BRIEFING\n\nNo new non-urgent emails since the last briefing."
    send_telegram(msg)
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
                tone = analysis.get("tone", "personal")
                print(f"Tone chosen: {tone}")

                draft_saved = False
                draft_body = (analysis.get("draft_body") or "").strip()
                if analysis.get("needs_reply") and draft_body:
                    draft_saved = save_draft(e, draft_body)

                if is_urgent(e['subject'], analysis.get("category")):
                    note = draft_note(draft_saved, tone)
                    note = f"\n{note}" if note else ""
                    send_telegram(f"🚨 URGENT EMAIL\nFrom: {e['from']}\nSubject: {e['subject']}\n\nSummary: {analysis.get('summary', 'N/A')}{note}")
                else:
                    digest["emails"].append({"from": e['from'], "subject": e['subject'],
                                             "analysis": analysis, "draft_saved": draft_saved})

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
    except Exception as ex:
        print(f"Error: {ex}")
        time.sleep(60)
