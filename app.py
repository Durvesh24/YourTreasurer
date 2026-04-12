from flask import Flask, render_template, request, flash, redirect, url_for, jsonify, session, Response
import csv, io
from werkzeug.security import generate_password_hash, check_password_hash
import os, re, threading, ssl, certifi
from pymongo import MongoClient
from bson.objectid import ObjectId
from datetime import datetime
from dotenv import load_dotenv
import cloudinary
import cloudinary.uploader
import cloudinary.api

load_dotenv(override=True)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "campuscoin_tracker_2026")

# ── Cloudinary Configuration ──────────────────────────────
cloudinary.config(
    cloud_name = os.environ.get('CLOUDINARY_NAME'),
    api_key    = os.environ.get('CLOUDINARY_KEY'),
    api_secret = os.environ.get('CLOUDINARY_SECRET'),
    secure     = True
)

# ── Jinja2 filter: Indian rupee format ───────────────────────
@app.template_filter('format_inr')
def format_inr(value):
    try:
        v = int(value); s = str(v)
        if len(s) <= 3: return f"₹{s}"
        last3 = s[-3:]; rest = s[:-3]
        parts = [rest[max(i-2,0):i] for i in range(len(rest), 0, -2)][::-1]
        return f"₹{','.join(p for p in parts if p)},{last3}"
    except Exception:
        return f"₹{value}"

# ──────────────────────────────────────────────────────────────
# MONGODB (direct MongoClient + certifi — fixes SSL on Py 3.13)
# ──────────────────────────────────────────────────────────────
_mongo_uri = os.environ.get("MONGO_URI", "")

class _DB:
    """Thin wrapper so mongo.db.xxx works throughout the codebase."""
    def __init__(self):
        self.db = None
        self._client = None

    def connect(self, uri):
        try:
            # Build a custom SSL context — fixes TLSV1_ALERT_INTERNAL_ERROR
            # on Python 3.13 / Windows OpenSSL with MongoDB Atlas
            ssl_ctx = ssl.create_default_context(cafile=certifi.where())
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode    = ssl.CERT_NONE
            ssl_ctx.minimum_version = ssl.TLSVersion.TLSv1_2

            self._client = MongoClient(
                uri,
                tls=True,
                tlsCAFile=certifi.where(),
                tlsAllowInvalidCertificates=True,
                serverSelectionTimeoutMS=20000,
            )
            dbname = uri.split('/')[-1].split('?')[0].strip() or 'yourtreasurer'
            self.db = self._client[dbname]
            self._client.admin.command('ping')   # fast-fail check
            print("[DB] Connected to MongoDB Atlas.")
        except Exception as err:
            print(f"[DB] Connection failed: {err}")
            self.db = None

mongo = _DB()
mongo.connect(_mongo_uri)

# Create indexes once (idempotent — Atlas ignores if they exist)
with app.app_context():
    if mongo.db is not None:
        try:
            mongo.db.users.create_index([("name_lower", 1)], unique=True, name="unique_name")
            mongo.db.users.create_index([("email_lower", 1)], unique=True, name="unique_email")
            print("[DB] Indexes ready.")
        except Exception as e:
            print(f"[DB] Index note: {e}")
    else:
        print("[DB] No DB connection — skipping indexes.")

@app.before_request
def ensure_db_connection():
    # If the app couldn't connect initially, try reconnecting on each user request
    if mongo.db is None:
        try:
            mongo.connect(_mongo_uri)
        except Exception:
            pass


app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # 5 MB

# ──────────────────────────────────────────────────────────────
# VALIDATION HELPERS
# ──────────────────────────────────────────────────────────────

def validate_name(name):
    n = (name or '').strip()
    if len(n) < 3:  return "Name must be at least 3 characters long."
    if len(n) > 40: return "Name must be 40 characters or fewer."
    if not re.match(r"^[A-Za-z][A-Za-z\s'\-]{2,39}$", n):
        return "Name must contain only letters (spaces, hyphens, apostrophes allowed)."
    if re.search(r"[\s\-']{2,}", n):
        return "Name cannot have consecutive spaces or hyphens."
    return None

def validate_email(email):
    e = (email or '').strip()
    if not e: return "Email address is required."
    if len(e) > 100: return "Email is too long (max 100 chars)."
    if not re.match(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$", e):
        return "Please enter a valid email address."
    if e.split('@')[-1].lower() in ['mailinator.com','guerrillamail.com','trashmail.com','yopmail.com']:
        return "Please use a real email address, not a disposable one."
    return None

def validate_password(password):
    p = password or ''
    if not p:              return "Password is required."
    if ' ' in p:           return "Password cannot contain spaces."
    if len(p) < 8:         return "Password must be at least 8 characters."
    if len(p) > 64:        return "Password cannot exceed 64 characters."
    if not re.search(r"[A-Z]", p): return "Password must contain an uppercase letter."
    if not re.search(r"[a-z]", p): return "Password must contain a lowercase letter."
    if not re.search(r"\d",    p): return "Password must contain a number."
    return None

def validate_budget(value):
    try: amount = float(value)
    except (ValueError, TypeError): return "Please enter a valid number for your budget."
    if amount < 100:       return "Budget must be at least Rs.100."
    if amount > 1_000_000: return "Budget cannot exceed Rs.10,00,000."
    return None

# ──────────────────────────────────────────────────────────────
# AUTH GUARD — Zero-Persistence (every request)
# ──────────────────────────────────────────────────────────────
_PUBLIC = {'home', 'my_profile', 'logout', 'about_us', 'static'}

@app.before_request
def require_login():
    if request.endpoint in _PUBLIC or request.endpoint is None:
        return
    if 'username' not in session:
        flash('Please log in to access your vault.', 'warning')
        return redirect(url_for('my_profile'))

# ──────────────────────────────────────────────────────────────
# EXPENSE CATEGORIES (shared by backend + templates)
# ──────────────────────────────────────────────────────────────
EXPENSE_CATEGORIES = [
    'Educational', 'Lifestyle', 'Healthy Food',
    'Junk Food', 'Hostel Rent', 'Travelling', 'Other'
]

# ──────────────────────────────────────────────────────────────
# TASK 6: GUARDIAN MAIL — BUDGET ALERTS (Email Dispatcher)
# ──────────────────────────────────────────────────────────────
import smtplib
from email.message import EmailMessage
import ssl

def send_alert_email_async(to_email, username, tier, limit, balance, spent, category=None, velocity_msg="", safepoint_msg=""):
    sender = os.environ.get('MAIL_USER', '')
    password = os.environ.get('MAIL_PASS', '')
    if not sender or not password or sender == 'your_email@gmail.com':
        print("[Mail] Skipping email; MAIL_USER or MAIL_PASS not configured in .env")
        return

    msg = EmailMessage()
    msg['From'] = f"YourTreasurer Alerts <{sender}>"
    msg['To'] = to_email

    if tier == '10':
        subject = f"⚠️ Budget Alert: 10% Remaining, {username}!"
        color = "#eab308" # Yellow
        title = "YELLOW ALERT"
        msg_text = "You are approaching your budget limit. Please monitor your spending over the coming days."
    elif tier == '5':
        subject = f"🚨 Critical Alert: Only 5% Remaining, {username}!"
        color = "#ef4444" # Red
        title = "CRITICAL ALERT"
        msg_text = "Your budget is critically low. Immediate spending cuts are advised to avoid overdraft."
    elif tier == '0':
        subject = f"🛑 OVER BUDGET: Limit Exceeded, {username}!"
        color = "#7f1d1d" # Deep Red
        title = "OVER BUDGET WARNING"
        msg_text = "You have exceeded your configured monthly limit. Please carefully review your latest transactions."
    elif tier == 'velocity':
        subject = f"🏃 Velocity Warning: High Spend Rate, {username}!"
        color = "#f97316" # Orange
        title = "PACE WARNING"
        msg_text = "Your current spending pace is significantly higher than recommended for your 30-day cycle."
    
    html = f"""
    <html>
    <body style="background-color:#f1f5f9; color:#334155; font-family:'Helvetica Neue', sans-serif; padding:20px; line-height:1.5;">
        <div style="max-width:600px; margin:0 auto; background-color:#ffffff; border-radius:12px; padding:30px; border-top: 6px solid {color}; box-shadow: 0 4px 20px rgba(0,0,0,0.06);">
            <div style="text-align:center; padding-bottom: 20px; border-bottom: 1px solid #e2e8f0;">
                <h2 style="color:{color}; margin:0; letter-spacing:2px; font-size:13px; font-weight:800; text-transform:uppercase;">{title}</h2>
                <h1 style="color:#0f172a; margin-top:8px; font-size:22px; font-weight:700;">YourTreasurer Guardian Module</h1>
            </div>
            
            <p style="font-size:16px; margin-top:25px; color:#1e293b;">Hello <strong>{username}</strong>,</p>
            <p style="font-size:15px; color:#475569;">{msg_text}</p>
            
            <div style="background-color:#f8fafc; padding:20px; border-radius:8px; margin:25px 0; border: 1px solid #e2e8f0;">
                <h3 style="margin:0 0 15px 0; color:#64748b; font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:1px;">Current Cycle Core Stats</h3>
                
                <table style="width:100%; border-collapse:collapse; font-size:15px;">
                    <tr>
                        <td style="padding:8px 0; color:#475569;">Monthly Limit</td>
                        <td style="padding:8px 0; text-align:right; font-weight:bold; color:#0f172a;">₹{limit:,.2f}</td>
                    </tr>
                    <tr>
                        <td style="padding:8px 0; color:#475569;">Total Spent</td>
                        <td style="padding:8px 0; text-align:right; font-weight:bold; color:#ef4444;">₹{spent:,.2f}</td>
                    </tr>
                    <tr>
                        <td style="padding:8px 0; color:#475569; border-top:1px dashed #cbd5e1; padding-top:12px; margin-top:4px;">Remaining Balance</td>
                        <td style="padding:8px 0; text-align:right; font-weight:bold; color:{color}; border-top:1px dashed #cbd5e1; padding-top:12px;">₹{balance:,.2f}</td>
                    </tr>
                </table>
            </div>
            """
    
    if velocity_msg or safepoint_msg:
        html += """<div style="background-color:#fefce8; padding:15px 20px; border-radius:8px; border-left:4px solid #eab308; margin-bottom:25px;">"""
        if velocity_msg:
            html += f"<p style='margin:0 0 8px 0; font-size:14.5px; color:#854d0e;'>{velocity_msg}</p>"
        if safepoint_msg:
            html += f"<p style='margin:0; font-size:14.5px; color:#854d0e;'>{safepoint_msg}</p>"
        html += "</div>"
        
    if category:
        html += f"<p style='color:#64748b; font-size:13px; text-align:center;'>This alert was dispatched immediately following your recent expense in <strong style='color:#0f172a;'>{category}</strong>.</p>"
        
    html += """
            <div style="text-align:center; margin-top:35px; margin-bottom: 20px;">
                <a href="http://127.0.0.1:5000/my_profile" style="background-color:#0ea5e9; color:#ffffff; padding:12px 28px; text-decoration:none; border-radius:50px; font-weight:bold; font-size: 14px; display:inline-block; box-shadow: 0 4px 12px rgba(14,165,233,0.3);">Access Dashboard</a>
            </div>
            
            <p style="font-size:11px; color:#94a3b8; text-align:center; margin-top:30px; letter-spacing:0.5px;">SECURELY DELIVERED BY AUTOMATED GUARDIAN SYSTEM.</p>
        </div>
    </body>
    </html>
    """

    msg.set_content(f"Budget Alert for {username}:\nLimit: ₹{limit}\nSpent: ₹{spent}\nBalance: ₹{balance}\nPlease log in to view your dashboard.")
    msg.add_alternative(html, subtype='html')

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
            server.login(sender, password)
            server.send_message(msg)
        print(f"[Mail] Successfully dispatched {tier}% alert to {to_email}")
    except Exception as e:
        print(f"[Mail] Failed to send email to {to_email}. Error: {e}")

def send_reminder_email_async(to_email, username, title, amount, due_date_str, days_left):
    sender = os.environ.get('MAIL_USER', '')
    password = os.environ.get('MAIL_PASS', '')
    if not sender or not password or sender == 'your_email@gmail.com':
        return

    msg = EmailMessage()
    msg['From'] = f"YourTreasurer Alerts <{sender}>"
    msg['To'] = to_email
    msg['Subject'] = f"📅 Payment Due Soon: {title} ({days_left} days left)!"

    html = f"""
    <html>
    <body style="background-color:#f8fafc; margin:0; padding:30px; font-family:'Segoe UI', Tahoma, Arial, sans-serif;">
        <div style="max-width:550px; margin:0 auto; background-color:#ffffff; border-radius:16px; padding:35px; border-top: 5px solid #ef4444; box-shadow: 0 10px 25px rgba(0,0,0,0.05);">
            <h2 style="color:#ef4444; margin-top:0; font-size:12px; font-weight:800; letter-spacing:2px; text-transform:uppercase;">Recurring Liability Alert</h2>
            <h1 style="color:#0f172a; font-size:24px; margin:10px 0 25px;">Upcoming Payment: <span style="color:#3b82f6;">{title}</span></h1>
            
            <p style="color:#475569; font-size:16px; line-height:1.6;">Hello <strong>{username}</strong>,<br>This is an automated reminder that your scheduled liability is due in <strong>{days_left} day(s)</strong>.</p>
            
            <div style="background-color:#f1f5f9; padding:20px; border-radius:12px; margin:30px 0; border: 1px solid #e2e8f0;">
                <p style="margin:5px 0; color:#64748b; font-size:14px; text-transform:uppercase; letter-spacing:1px;">Amount Due</p>
                <p style="margin:0; color:#0f172a; font-size:28px; font-weight:800;">₹{amount:,.2f}</p>
                
                <div style="height:1px; background-color:#cbd5e1; margin:15px 0;"></div>
                
                <p style="margin:5px 0; color:#64748b; font-size:12px; text-transform:uppercase; letter-spacing:1px;">Due Date</p>
                <p style="margin:0; color:#ef4444; font-size:18px; font-weight:700;">{due_date_str}</p>
            </div>
            
            <center>
                <a href="http://127.0.0.1:5000/interval_spend" style="background-color:#38bdf8; color:#ffffff; padding:14px 30px; text-decoration:none; border-radius:30px; font-weight:bold; font-size:15px; display:inline-block; box-shadow: 0 4px 15px rgba(56,189,248,0.4);">Review & Pay</a>
            </center>
        </div>
    </body>
    </html>
    """
    msg.set_content(f"Upcoming Payment: {title} allows {days_left} days left. Amount: {amount}")
    msg.add_alternative(html, subtype='html')

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
            server.login(sender, password)
            server.send_message(msg)
        print(f"[Mail] Reminder sent for {title} to {to_email}")
    except Exception as e:
        print(f"[Mail] Failed to send reminder. Error: {e}")

def send_loan_handshake_async(to_email, friend_name, owner_name, amount, desc, receipt_url, bcc_email):
    """Task 7: Automated Loan Handshake. Sends a professional notification to the borrower."""
    sender = os.environ.get('MAIL_USER', '')
    password = os.environ.get('MAIL_PASS', '')
    if not sender or not password: return

    msg = EmailMessage()
    msg['From'] = f"YourTreasurer Official <{sender}>"
    msg['To'] = to_email
    msg['Subject'] = f"Official Ledger Update: Pending Loan Recorded"
    if bcc_email:
        msg['Bcc'] = bcc_email

    color = "#3b82f6" # Professional Trust Blue
    
    html = f"""
    <html>
    <body style="background-color:#f8fafc; color:#334155; font-family:'Helvetica Neue', sans-serif; padding:20px; line-height:1.6;">
        <div style="max-width:600px; margin:0 auto; background-color:#ffffff; border-radius:12px; padding:30px; border-top: 6px solid {color}; box-shadow: 0 4px 20px rgba(0,0,0,0.06);">
            <div style="text-align:center; padding-bottom: 20px; border-bottom: 1px solid #e2e8f0;">
                <h2 style="color:{color}; margin:0; letter-spacing:2px; font-size:13px; font-weight:800; text-transform:uppercase;">OFFICIAL LEDGER HANDSHAKE</h2>
                <h1 style="color:#0f172a; margin-top:8px; font-size:22px; font-weight:700;">YourTreasurer System</h1>
            </div>
            
            <p style="font-size:16px; margin-top:25px; color:#1e293b;">Hello <strong>{friend_name}</strong>,</p>
            <p style="font-size:15px; color:#475569;">This is an automated notification from the YourTreasurer system. Your friend <strong>{owner_name}</strong> has successfully recorded a pending loan in their official ledger.</p>
            
            <div style="background-color:#eff6ff; padding:20px; border-radius:8px; margin:25px 0; border: 1px solid #bfdbfe; border-left: 4px solid #3b82f6;">
                <table style="width:100%; border-collapse:collapse; font-size:15px;">
                    <tr>
                        <td style="padding:8px 0; color:#475569;">Loan Purpose</td>
                        <td style="padding:8px 0; text-align:right; font-weight:bold; color:#0f172a;">{desc}</td>
                    </tr>
                    <tr>
                        <td style="padding:8px 0; color:#475569; border-top:1px dashed #93c5fd; padding-top:12px; margin-top:4px;">Principal Amount</td>
                        <td style="padding:8px 0; text-align:right; font-weight:800; font-size:18px; color:#1d4ed8; border-top:1px dashed #93c5fd; padding-top:12px;">₹{amount:,.2f}</td>
                    </tr>
                </table>
            </div>
            """
            
    if receipt_url:
        html += f"""
            <div style="text-align:center; margin: 30px 0;">
                <p style="font-size:13px; color:#64748b; margin-bottom: 12px;">A highly secure digital receipt was attached to this transaction.</p>
                <a href="{receipt_url}" style="background-color:#1e293b; color:#ffffff; padding:12px 24px; text-decoration:none; border-radius:8px; font-weight:600; font-size: 14px; display:inline-block;">View Digital Receipt</a>
            </div>
        """
        
    html += f"""
            <p style="font-size:14px; color:#475569; text-align:center; margin-top:20px;">Please coordinate directly with {owner_name} to settle this balance.</p>
            
            <p style="font-size:11px; color:#94a3b8; text-align:center; margin-top:35px; letter-spacing:0.5px;">THIS IS AN AUTOMATED SYSTEM EMAIL. PLEASE DO NOT REPLY.</p>
        </div>
    </body>
    </html>
    """

    msg.set_content(f"Loan Recorded: {owner_name} logged a loan of ₹{amount} for {desc}.")
    msg.add_alternative(html, subtype='html')

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
            server.login(sender, password)
            server.send_message(msg)
        print(f"[Handshake] Successfully sent loan email to {to_email}")
    except Exception as e:
        print(f"[Handshake] Failed to send to {to_email}. Error: {e}")

def send_gentle_reminder_async(to_email, friend_name, owner_name, amount, desc):
    """Task 13: Sends a gentle reminder HTML email asking for loan return."""
    sender = os.environ.get('MAIL_USER', '')
    password = os.environ.get('MAIL_PASS', '')
    if not sender or not password: return

    msg = EmailMessage()
    msg['From'] = f"{owner_name} via YourTreasurer <{sender}>"
    msg['To'] = to_email
    msg['Subject'] = f"Friendly Reminder: Pending Balance for {desc}"

    color = "#8b5cf6" # Friendly Purple
    html = f"""
    <html>
    <body style="background-color:#f8fafc; color:#334155; font-family:'Helvetica Neue', sans-serif; padding:20px; line-height:1.6;">
        <div style="max-width:600px; margin:0 auto; background-color:#ffffff; border-radius:12px; padding:30px; border-top: 6px solid {color}; box-shadow: 0 4px 20px rgba(0,0,0,0.06);">
            <div style="text-align:center; padding-bottom: 20px; border-bottom: 1px solid #e2e8f0;">
                <h2 style="color:{color}; margin:0; letter-spacing:2px; font-size:13px; font-weight:800; text-transform:uppercase;">GENTLE REMINDER</h2>
            </div>
            
            <p style="font-size:16px; margin-top:25px; color:#1e293b;">Hi <strong>{friend_name}</strong>,</p>
            <p style="font-size:15px; color:#475569;">Hope you're having a great day! This is just a quick, friendly automated note from <strong>{owner_name}</strong> regarding the pending balance for <strong style="color:#0f172a;">{desc}</strong>.</p>
            
            <div style="background-color:#f5f3ff; padding:20px; border-radius:8px; margin:25px 0; border: 1px solid #ddd6fe; text-align:center;">
                <p style="margin:0; color:#6d28d9; font-size:13px; font-weight:700; text-transform:uppercase; letter-spacing:1px;">Pending Amount</p>
                <p style="margin:8px 0 0 0; color:#5b21b6; font-size:28px; font-weight:800;">₹{amount:,.2f}</p>
            </div>
            
            <p style="font-size:14px; color:#475569; text-align:center; margin-top:20px;">Whenever you get a chance, you can coordinate directly with {owner_name} to settle this. Thanks!</p>
        </div>
    </body>
    </html>
    """
    msg.set_content(f"Reminder: Hi {friend_name}, please coordinate with {owner_name} to settle the ₹{amount} for {desc}.")
    msg.add_alternative(html, subtype='html')

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
            server.login(sender, password)
            server.send_message(msg)
        print(f"[Handshake] Successfully sent gentle reminder to {to_email}")
    except Exception as e:
        print(f"[Handshake] Failed to send reminder to {to_email}. Error: {e}")

def trigger_budget_alert(user, limit, balance, spent, category=None):
    """Checks budget thresholds, spawns a daemon thread for email, and returns flash message."""
    if not user.get('email'): return None
    
    from datetime import datetime
    
    # Intelligence Metrics: Safepoint & Velocity
    start_date = user.get('start_date')
    if not start_date:
        start_date = datetime.utcnow()
        
    days_used = max(1, (datetime.utcnow() - start_date).days)
    days_remaining = max(1, 30 - days_used)
    
    safe_daily_spend = max(0, balance) / days_remaining
    burn_rate = spent / days_used
    
    velocity_msg = ""
    safepoint_msg = ""
    
    if balance > 0:
        safepoint_msg = f"🛡️ <strong>Daily Safepoint:</strong> To safely survive the remaining {days_remaining} days of this cycle, limit average spending to <strong>₹{safe_daily_spend:.0f}/day</strong>."
    
    tier = None
    update_field = None
    
    # Check 100%, 5%, 10%
    if balance < 0 and not user.get('alert_0_sent', False):
        tier = '0'
        update_field = 'alert_0_sent'
    elif balance >= 0 and balance < (0.05 * limit) and not user.get('alert_5_sent', False):
        tier = '5'
        update_field = 'alert_5_sent'
    elif balance >= (0.05 * limit) and balance <= (0.10 * limit) and not user.get('alert_10_sent', False):
        tier = '10'
        update_field = 'alert_10_sent'
    else:
        # Check Velocity if outside standard thresholds
        if days_used >= 5 and burn_rate > 0:
            predicted_depletion_days = limit / burn_rate
            days_left_alive = int(predicted_depletion_days - days_used)
            # If they are on pace to run out before 30 days, AND it will happen in < 10 days
            if predicted_depletion_days < 30 and days_left_alive > 0 and days_left_alive < 10:
                if not user.get('alert_velocity_sent', False):
                    tier = 'velocity'
                    update_field = 'alert_velocity_sent'
                    velocity_msg = f"🏃 <strong>Pace Warning:</strong> At your current burn rate (₹{burn_rate:.0f}/day), your budget will completely dry up in <strong>{days_left_alive} days</strong>."
        
    if tier:
        from app import mongo
        mongo.db.users.update_one({'_id': user['_id']}, {'$set': {update_field: True}})
        
        # Async send email
        t = threading.Thread(
            target=send_alert_email_async, 
            args=(user['email'], user['name'], tier, limit, balance, spent, category, velocity_msg, safepoint_msg),
            daemon=True
        )
        t.start()
        
        if tier == '0':
            return "WARNING: You have just EXCEEDED your budget limit! An email alert has been dispatched.", 'error'
        elif tier == '5':
            return "CRITICAL: You are under 5% budget remaining! 🚨 Email alert dispatched.", 'error'
        elif tier == '10':
            return "NOTICE: You are under 10% budget remaining. ⚠️ Email warning scheduled.", 'warning'
        elif tier == 'velocity':
            return "PACE WARNING: You are burning through your budget too rapidly. 🏃‍♂️ Check your email for details.", 'warning'
    
    return None

# ──────────────────────────────────────────────────────────────
# ROUTES
# ──────────────────────────────────────────────────────────────

@app.route('/')
def home():
    """Public landing page & Logged-In Dashboard (Task 14)."""
    if 'username' in session and mongo.db is not None:
        user = mongo.db.users.find_one({'name': session['username']}, {'password': 0})
        if user:
            return render_template('index.html', user=user)
    return render_template('index.html')


@app.route('/my_profile', methods=['GET', 'POST'])
def my_profile():
    """Login / Register — The Secure Budget Gateway."""
    # If logged in → show profile dashboard instead of login form
    if 'username' in session:
        if mongo.db is None:
            flash('Database unavailable.', 'error')
            return redirect(url_for('home'))
        user = mongo.db.users.find_one({'name': session['username']}, {'password': 0})
        expense_count = mongo.db.daily_expenses.count_documents({'username': session['username']})
        return render_template('profile.html', user=user, expense_count=expense_count)

    if mongo.db is None:
        flash('Database is currently unreachable. Please try again later.', 'error')
        return render_template('profile.html', user=None)

    if request.method == 'POST':
        form_type = request.form.get('form_type', '').strip()
        name      = request.form.get('name', '').strip()
        password  = request.form.get('password', '').strip()

        name_err = validate_name(name)
        if name_err:
            flash(name_err, 'error'); return redirect(url_for('my_profile'))

        pass_err = validate_password(password)
        if pass_err:
            flash(pass_err, 'error'); return redirect(url_for('my_profile'))

        users = mongo.db.users

        # ── REGISTER ───────────────────────────────────────────────────────
        if form_type == 'register':
            email         = request.form.get('email', '').strip().lower()
            monthly_limit = request.form.get('monthly_limit', '').strip()

            email_err = validate_email(email)
            if email_err:
                flash(email_err, 'error'); return redirect(url_for('my_profile'))

            budget_err = validate_budget(monthly_limit)
            if budget_err:
                flash(budget_err, 'error'); return redirect(url_for('my_profile'))

            if users.find_one({'name_lower': name.lower()}):
                flash(f'The name "{name}" is already taken. Please choose another.', 'error')
                return redirect(url_for('my_profile'))

            if users.find_one({'email_lower': email}):
                flash('This email is already registered. Please log in instead.', 'error')
                return redirect(url_for('my_profile'))

            now = datetime.utcnow()
            try:
                users.insert_one({
                    'name': name, 'name_lower': name.lower(),
                    'email': email, 'email_lower': email,
                    'password': generate_password_hash(password, method='pbkdf2:sha256', salt_length=16),
                    'monthly_limit': float(monthly_limit),
                    'total_spent': 0.0, 'balance': float(monthly_limit),
                    'start_date': now, 'cycle_number': 1,
                    'alert_10_sent': False, 'alert_5_sent': False, 'over_budget': False,
                    'created_at': now, 'last_login': now, 'login_count': 1,
                })
            except Exception as e:
                print(f"[DB] Register error: {e}")
                flash('Could not create account. Please try again.', 'error')
                return redirect(url_for('my_profile'))

            session['username'] = name
            session['email'] = email
            flash(f'Welcome aboard, {name}! Your vault is ready.', 'success')
            return redirect(url_for('home'))

        # ── LOGIN ───────────────────────────────────────────────────────────
        elif form_type == 'login':
            user = users.find_one({'name_lower': name.lower()})
            if not user:
                flash('No account found with that name. Please register first.', 'error')
                return redirect(url_for('my_profile'))

            if not check_password_hash(user['password'], password):
                flash('Incorrect password. Please try again.', 'error')
                return redirect(url_for('my_profile'))

            # 30-day cycle reset
            start_date = user.get('start_date')
            if not start_date:
                start_date = datetime.utcnow()
                
            if (datetime.utcnow() - start_date).days >= 30:
                try:
                    mongo.db.monthly_archives.insert_one({
                        'username': user['name'], 'email': user.get('email',''),
                        'cycle_number': user.get('cycle_number',1),
                        'total_spent': user.get('total_spent',0.0),
                        'monthly_limit': user.get('monthly_limit',0.0),
                        'period_start': start_date, 'period_end': datetime.utcnow(),
                    })
                    users.update_one({'_id': user['_id']}, {'$set': {
                        'total_spent': 0.0, 'balance': user.get('monthly_limit',0.0),
                        'start_date': datetime.utcnow(),
                        'alert_10_sent': False, 'alert_5_sent': False, 'over_budget': False,
                    }, '$inc': {'cycle_number': 1}})
                    flash('30-day cycle complete! Budget reset for new month.', 'warning')
                except Exception as e:
                    print(f"[DB] Cycle reset error: {e}")

            try:
                users.update_one({'_id': user['_id']},
                    {'$set': {'last_login': datetime.utcnow()}, '$inc': {'login_count': 1}})
            except Exception as e:
                print(f"[DB] Login update error: {e}")

            session['username'] = user['name']
            session['email']    = user.get('email', '')
            flash(f'Welcome back, {user["name"]}! Vault unlocked.', 'success')
            return redirect(url_for('home'))

        flash('Invalid form submission.', 'error')
        return redirect(url_for('my_profile'))

    return render_template('profile.html', user=None)


@app.route('/update_budget', methods=['POST'])
def update_budget():
    """Allow logged in users to update their monthly limit and adjust their balance."""
    if 'username' not in session:
        return redirect(url_for('my_profile'))
        
    username = session['username']
    try:
        new_budget = float(request.form.get('new_budget', 0))
        if new_budget < 100:
            flash('Budget must be at least ₹100.', 'error')
            return redirect(url_for('my_profile'))
            
        user = mongo.db.users.find_one({'name': username})
        if user:
            # Calculate new balance
            total_spent = user.get('total_spent', 0.0)
            new_balance = round(new_budget - total_spent, 2)
            
            mongo.db.users.update_one(
                {'name': username},
                {'$set': {
                    'monthly_limit': new_budget,
                    'balance': new_balance,
                    'over_budget': new_balance < 0,
                    'alert_10_sent': False,
                    'alert_5_sent': False,
                    'alert_0_sent': False,
                    'alert_velocity_sent': False
                }}
            )
            flash(f'Monthly budget successfully updated to ₹{new_budget:,.2f}', 'success')
    except ValueError:
        flash('Invalid budget amount.', 'error')
        
    return redirect(url_for('my_profile'))


@app.route('/logout')
def logout():
    """Clear session and return to login."""
    username = session.get('username', 'User')
    session.clear()
    flash(f'Goodbye, {username}! You have been logged out.', 'success')
    return redirect(url_for('my_profile'))


@app.route('/change_password', methods=['POST'])
def change_password():
    """Feature 18: Secure password change with current password verification."""
    if 'username' not in session:
        return redirect(url_for('my_profile'))

    username   = session['username']
    current_pw = request.form.get('current_password', '').strip()
    new_pw     = request.form.get('new_password', '').strip()
    confirm_pw = request.form.get('confirm_password', '').strip()

    if not current_pw or not new_pw or not confirm_pw:
        flash('All password fields are required.', 'error')
        return redirect(url_for('my_profile'))

    if new_pw != confirm_pw:
        flash('New passwords do not match. Please try again.', 'error')
        return redirect(url_for('my_profile'))

    if len(new_pw) < 8:
        flash('New password must be at least 8 characters.', 'error')
        return redirect(url_for('my_profile'))

    user = mongo.db.users.find_one({'name': username})
    if not user or not check_password_hash(user['password'], current_pw):
        flash('Current password is incorrect.', 'error')
        return redirect(url_for('my_profile'))

    new_hash = generate_password_hash(new_pw, method='pbkdf2:sha256', salt_length=16)
    mongo.db.users.update_one({'name': username}, {'$set': {'password': new_hash}})
    flash('Password updated successfully! Your vault is now more secure. 🔒', 'success')
    return redirect(url_for('my_profile'))



@app.route('/export_data')
def export_data():
    """Generate and return a clean, Excel-formatted CSV of the user's entire expense history."""
    username = session.get('username')
    if not username: return redirect(url_for('home'))
    
    expenses = list(mongo.db.daily_expenses.find({'username': username}).sort('expense_date', -1))
    
    si = io.StringIO()
    cw = csv.writer(si)
    # Add a title header and a blank line for an Excel-friendly professional look
    cw.writerow([f"YourTreasurer Account Export — {username.upper()}"])
    cw.writerow([])
    
    cw.writerow(['Transaction Date', 'Category', 'Description', 'Amount (INR)', 'Transaction Type/Status'])
    
    for e in expenses:
        # User-friendly Date: 12 Apr 2026, 04:30 PM
        date_str = e.get('expense_date').strftime("%d %b %Y, %I:%M %p") if e.get('expense_date') else "Unknown"
        category = e.get('category', 'Uncategorized')
        desc = e.get('description', '')
        
        # Formatted Currency: ₹ 1,500.00
        raw_amount = e.get('amount', 0.0)
        amount_formatted = f"₹ {raw_amount:,.2f}"
        
        status = 'Loan: ' + e.get('loan_status', '').title() if e.get('is_loan') else 'Standard Expense'
        cw.writerow([date_str, category, desc, amount_formatted, status])
        
    output = si.getvalue()
    # Add UTF-8 BOM so Excel opens it with the Indian Rupee symbol correctly rendered
    output_with_bom = '\ufeff' + output
    
    return Response(
        output_with_bom,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment;filename={username}_vault_backup.csv"}
    )

def sync_user_ledger(username):
    """Bulletproof accounting mathematically syncing non-loan expenses and PENDING loans naturally into the total_spent cache."""
    user = mongo.db.users.find_one({'name': username})
    if not user: return 0, 0
    
    # 1. Sum standard expenses
    standard = list(mongo.db.daily_expenses.find({'username': username, 'is_loan': {'$ne': True}}))
    sum_standard = sum(e.get('amount', 0) for e in standard)
    
    # 2. Sum pending loans (Because letting money go IS an expense until it's returned!)
    pending = list(mongo.db.daily_expenses.find({'username': username, 'is_loan': True, 'loan_status': 'pending'}))
    sum_pending = sum(e.get('amount', 0) for e in pending)
    
    total_effective_spent = round(sum_standard + sum_pending, 2)
    new_bal = round(user.get('monthly_limit', 0.0) - total_effective_spent, 2)
    
    mongo.db.users.update_one({'name': username}, {'$set': {
        'total_spent': total_effective_spent,
        'balance': new_bal,
        'over_budget': new_bal < 0
    }})
    return total_effective_spent, new_bal


@app.route('/delete_account', methods=['POST'])
def delete_account():
    """Permanently destroy user account and all associated transactions."""
    username = session.get('username')
    if not username: return redirect(url_for('home'))
    
    confirm_text = request.form.get('confirm_text', '')
    if confirm_text != 'DELETE':
        flash('Account deletion failed. You must type DELETE exactly.', 'error')
        return redirect(url_for('my_profile'))
        
    try:
        # Wipe all transactions
        mongo.db.daily_expenses.delete_many({'username': username})
        # Wipe user account
        mongo.db.users.delete_one({'name': username})
        # Kill session
        session.clear()
        
        flash('Your account and all associated data have been permanently destroyed.', 'success')
        return redirect(url_for('home'))
    except Exception as e:
        print(f"[Delete] Error: {e}")
        flash('An error occurred while deleting your account. Please try again.', 'error')
        return redirect(url_for('my_profile'))


@app.route('/my_expenses')
def my_expenses():
    """Task 2 & 3 — Expense entry and history page."""
    username   = session.get('username')
    if mongo.db is None:
        return render_template('expenses.html', user=None, categories=EXPENSE_CATEGORIES, expenses=[])

    try:
        # Dynamically guarantee the ledger sums perfectly before loading profile 
        sync_user_ledger(username)
    except:
        pass
        
    user = mongo.db.users.find_one({'name': username}, {'password': 0})
    
    # Task 3: Real-Time spend history with auto-seeding
    expenses = list(mongo.db.daily_expenses.find({'username': username}).sort('expense_date', -1))
    
    # Auto-seed minimum 8 dummy expenses if none exist (per Competition Rule Task 3)
    # (Removed as per user request to start accounts empty)

    play_coins   = session.pop('play_coins', False)
    play_crumple = session.pop('play_crumple', False)

    # History stats for the summary bar (Updated to sync directly against the true total)
    non_loan_exps = [e for e in expenses if not e.get('is_loan', False)]
    pending_loans = [e for e in expenses if e.get('is_loan', False) and e.get('loan_status') == 'pending']
    
    true_total = round(sum(e['amount'] for e in non_loan_exps) + sum(e['amount'] for e in pending_loans), 2)
    
    history_stats = {
        'count':   len(expenses),
        'total':   true_total,
        'biggest': round(max((e['amount'] for e in non_loan_exps), default=0), 2),
        'loans':   sum(1 for e in expenses if e.get('is_loan', False)),
    }

    return render_template('expenses.html', user=user,
                           categories=EXPENSE_CATEGORIES, expenses=expenses,
                           play_coins=play_coins, play_crumple=play_crumple,
                           history_stats=history_stats)


@app.route('/analysis')
def analysis():
    """Task 8: Aggregation Dashboard rendering."""
    if 'username' not in session: return redirect(url_for('my_profile'))
    return render_template('analysis.html')

@app.route('/api/expense_breakdown')
def expense_breakdown():
    """Task 8 Aggregation Pipeline: Offload heavy grouping math to MongoDB natively."""
    username = session.get('username')
    if not username: return jsonify({'error': 'Unauthorized'}), 401
    
    # Use aggregation to sum expenses by category, safely dropping paid loans entirely mathematically.
    pipeline = [
        {"$match": {
            "username": username,
            "loan_status": {"$ne": "paid"}
        }},
        {"$project": {
            "amount": 1,
            "category": {
                "$cond": [
                    {"$eq": ["$is_loan", True]},
                    {"$cond": [{"$eq": ["$loan_status", "paid"]}, "Loans (Paid Back)", "Loans (Pending)"]},
                    "$category"
                ]
            }
        }},
        {"$group": {"_id": "$category", "total": {"$sum": "$amount"}}},
        {"$sort": {"total": -1}}
    ]
    
    results = list(mongo.db.daily_expenses.aggregate(pipeline))
    labels = [r['_id'] for r in results]
    data = [round(r['total'], 2) for r in results]
    
    return jsonify({"labels": labels, "data": data})


from dateutil.relativedelta import relativedelta
from datetime import datetime, timedelta

@app.route('/interval_spend')
def interval_spend():
    """Task 11: Interval Spend Manager."""
    if 'username' not in session: return redirect(url_for('my_profile'))
    
    if mongo.db is None:
        flash('Database unavailable.', 'error')
        return redirect(url_for('home'))
        
    username = session['username']
    now = datetime.now()
    
    # Fetch recurring records
    records_cursor = mongo.db.recurring_payments.find({'username': username}).sort('due_date', 1)
    recurring = []
    
    for rec in records_cursor:
        rec_due = rec.get('due_date')
        remind_days = rec.get('reminder_days', 0)
        
        # Danger calculation for glowing red pulse border (Task 12 setup)
        danger = False
        days_left = 0
        days_total = 30 # Assuming standard 30-day rolling subscriptions
        if rec_due:
            threshold_date = rec_due.date() - timedelta(days=remind_days)
            if now.date() >= threshold_date:
                danger = True
            
            # Mathematics for SVG Progress Visualization
            delta = rec_due.date() - now.date()
            days_left = max(0, delta.days)
            if days_left > days_total: days_total = days_left # Graceful fallback for non-monthly items
            
        rec['is_danger']  = danger
        rec['days_left']  = days_left
        rec['days_total'] = days_total
        recurring.append(rec)
        
    return render_template('interval_spend.html', recurring=recurring, current_date=now)

@app.route('/add_recurring', methods=['POST'])
def add_recurring():
    if 'username' not in session: return redirect(url_for('my_profile'))
    username = session['username']
    
    title = request.form.get('title', '').strip()
    amount_str = request.form.get('amount', '').strip()
    due_date_str = request.form.get('due_date', '').strip()
    reminder_days_str = request.form.get('reminder_days', '0').strip()
    auto_roll = request.form.get('auto_roll') == 'on'
    
    if not title or not amount_str or not due_date_str:
        flash('All required fields must be filled.', 'error')
        return redirect(url_for('interval_spend'))
        
    try:
        amount = round(float(amount_str), 2)
        if amount <= 0: raise ValueError
    except:
        flash('Invalid amount.', 'error')
        return redirect(url_for('interval_spend'))
        
    try:
        due_date = datetime.strptime(due_date_str, '%Y-%m-%d')
        reminder_days = int(reminder_days_str)
    except:
        flash('Invalid date or reminder days.', 'error')
        return redirect(url_for('interval_spend'))
        
    doc = {
        'username': username,
        'title': title,
        'amount': amount,
        'due_date': due_date,
        'reminder_days': reminder_days,
        'auto_roll': auto_roll,
        'status': 'pending',
        'created_at': datetime.utcnow()
    }
    
    mongo.db.recurring_payments.insert_one(doc)
    flash(f'Recurring payment "{title}" added!', 'success')
    return redirect(url_for('interval_spend'))

@app.route('/pay_recurring/<record_id>', methods=['POST'])
def pay_recurring(record_id):
    if 'username' not in session: return redirect(url_for('my_profile'))
    username = session['username']
    
    from bson.objectid import ObjectId
    try:
        rec = mongo.db.recurring_payments.find_one({'_id': ObjectId(record_id), 'username': username})
        if not rec:
            flash('Record not found.', 'error')
            return redirect(url_for('interval_spend'))
            
        # 1. Log as Daily Expense so budget goes down cleanly
        expense_doc = {
            'username': username,
            'category': 'Lifestyle', # or a custom category
            'amount': rec['amount'],
            'description': f"[AUTO-LOGGED] Interval Spend: {rec['title']}",
            'expense_date': datetime.now(),
            'is_loan': False,
            'created_at': datetime.utcnow()
        }
        mongo.db.daily_expenses.insert_one(expense_doc)
        
        # 2. Update overall user budget metrics
        user = mongo.db.users.find_one({'name': username})
        if user:
            new_spent = user.get('total_spent', 0) + rec['amount']
            new_bal = user.get('balance', 0) - rec['amount']
            mongo.db.users.update_one({'_id': user['_id']}, {'$set': {
                'total_spent': new_spent, 'balance': new_bal, 'over_budget': new_bal < 0
            }})
            # Fire an alert if crossed any boundary
            trigger_budget_alert(user, user.get('monthly_limit', 0), new_bal, new_spent, 'Interval Spend')
            
        # 3. Handle Auto Roll toggle requested by User
        if rec.get('auto_roll', False):
            try:
                import calendar
                old_date = rec['due_date']
                month = old_date.month % 12 + 1
                year = old_date.year + (old_date.month // 12)
                day = min(old_date.day, calendar.monthrange(year, month)[1])
                new_due_date = old_date.replace(year=year, month=month, day=day)
            except:
                # Fallback if standard shifting fails
                new_due_date = rec['due_date'] + timedelta(days=30)
                
            mongo.db.recurring_payments.update_one(
                {'_id': ObjectId(record_id)},
                {'$set': {'due_date': new_due_date}}
            )
            if request.headers.get('Accept') != 'application/json':
                flash(f'Payment submitted! Auto-rolled "{rec["title"]}" to exactly 1 month later.', 'success')
        else:
            mongo.db.recurring_payments.delete_one({'_id': ObjectId(record_id)})
            if request.headers.get('Accept') != 'application/json':
                flash(f'Payment submitted! Tracked your liability "{rec["title"]}" and removed it.', 'success')
            
        if request.headers.get('Accept') == 'application/json':
            new_date_str = new_due_date.strftime('%b %d, %Y') if 'new_due_date' in locals() else ''
            return jsonify({'success': True, 'action': 'rolled' if rec.get('auto_roll', False) else 'deleted', 'new_date': new_date_str})
            
    except Exception as e:
        print("Error paying recurring:", e)
        if request.headers.get('Accept') == 'application/json': return jsonify({'success': False, 'error': str(e)})
        flash('Error processing payment.', 'error')
        
    return redirect(url_for('interval_spend'))

@app.route('/delete_recurring/<record_id>', methods=['POST'])
def delete_recurring(record_id):
    if 'username' not in session: return redirect(url_for('my_profile'))
    from bson.objectid import ObjectId
    try:
        mongo.db.recurring_payments.delete_one({'_id': ObjectId(record_id), 'username': session['username']})
        if request.headers.get('Accept') == 'application/json': return jsonify({'success': True})
        flash('Interval spend profile deleted.', 'success')
    except Exception as e:
        if request.headers.get('Accept') == 'application/json': return jsonify({'success': False})
        flash('Error deleting record.', 'error')
    return redirect(url_for('interval_spend'))



@app.route('/about_us')
def about_us():
    return render_template('about_us.html')


@app.route('/add_expense', methods=['POST'])
def add_expense():
    """Task 2 — Save daily expense / loan to MongoDB."""
    username = session.get('username')

    if mongo.db is None:
        flash('Database unavailable. Please try again later.', 'error')
        return redirect(url_for('my_expenses'))

    try:
        category    = request.form.get('category', '').strip()
        amount_str  = request.form.get('amount', '').strip()
        description = request.form.get('description', '').strip()
        exp_date_str= request.form.get('expense_date', '').strip()
        is_loan     = request.form.get('is_loan') == 'on'

        if not category or category not in EXPENSE_CATEGORIES:
            flash('Please select a valid category.', 'error')
            return redirect(url_for('my_expenses'))

        try:
            amount = round(float(amount_str), 2)
            if amount <= 0:   raise ValueError
            if amount > 100000:
                flash('Single expense cannot exceed Rs.1,00,000.', 'error')
                return redirect(url_for('my_expenses'))
        except (ValueError, TypeError):
            flash('Please enter a valid amount.', 'error')
            return redirect(url_for('my_expenses'))

        try:
            if exp_date_str:
                parsed_dt = datetime.strptime(exp_date_str, '%Y-%m-%d')
                local_now = datetime.now()
                # If the user selects today's date, append the current time so it falls into correct hour bucket
                if parsed_dt.date() == local_now.date():
                    expense_date = datetime.combine(parsed_dt.date(), local_now.time())
                else:
                    expense_date = parsed_dt
            else:
                expense_date = datetime.now()
        except ValueError:
            expense_date = datetime.now()

        doc = {
            'username': username, 'category': category,
            'amount': amount, 'description': description or f'{category} expense',
            'expense_date': expense_date, 'is_loan': is_loan,
            'created_at': datetime.utcnow(),
        }

        if is_loan:
            friend_name  = request.form.get('friend_name', '').strip()
            friend_email = request.form.get('friend_email', '').strip()
            relationship = request.form.get('relationship', 'Friend').strip()

            if not friend_name or not friend_email:
                flash('Friend name and email are required for a loan.', 'error')
                return redirect(url_for('my_expenses'))
            if not re.match(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$", friend_email):
                flash('Please enter a valid email for your friend.', 'error')
                return redirect(url_for('my_expenses'))

            doc.update({'friend_name': friend_name, 'friend_email': friend_email,
                        'relationship': relationship, 'loan_status': 'pending'})

        # Cloudinary receipt upload (Task 5)
        receipt_url = None
        receipt_file = request.files.get('receipt')
        if receipt_file and receipt_file.filename:
            try:
                upload_result = cloudinary.uploader.upload(
                    receipt_file,
                    folder="yourtreasurer_receipts",
                    resource_type="auto"
                )
                receipt_url = upload_result.get('secure_url')
                doc['receipt_url'] = receipt_url
            except Exception as e:
                print(f"[Cloudinary] Upload Error: {e}")
                flash(f'Cloudinary Error: {str(e)}', 'error')

        mongo.db.daily_expenses.insert_one(doc)
        
        # TASK 7: Automated Loan Handshake Thread
        if is_loan and friend_email:
            bcc_email = session.get('email', '')
            t = threading.Thread(
                target=send_loan_handshake_async,
                args=(friend_email, friend_name, username, amount, description or f'{category} expense', receipt_url, bcc_email),
                daemon=True
            )
            t.start()

        # Universally sync user ledger to cleanly capture standard expenses and loans
        new_spent, new_bal = sync_user_ledger(username)
        
        user = mongo.db.users.find_one({'name': username})
        lim = user.get('monthly_limit', 0.0) if user else 0.0
                
        # TASK 6: Trigger Guardian Mail verification asynchronously
        alert_msg, alert_cat = trigger_budget_alert(user, lim, new_bal, new_spent, category) or (None, None)

        session['play_coins'] = True
        
        # Flash messages (only for non-ajax)
        if request.headers.get('Accept') != 'application/json':
            if is_loan:
                flash(f'Loan of Rs.{amount:,.2f} to {request.form.get("friend_name", "friend")} recorded.', 'success')
            else:
                flash(f'Rs.{amount:,.2f} tracked under {category}.', 'success')
                
            if not is_loan and alert_msg:
                flash(alert_msg, alert_cat)

        if request.headers.get('Accept') == 'application/json':
            return jsonify({'success': True, 'new_total': new_spent})

        return redirect(url_for('my_expenses'))

    except Exception as e:
        print(f'[Expense] Error: {e}')
        if request.headers.get('Accept') == 'application/json':
            return jsonify({'success': False, 'error': str(e)})
        flash('Something went wrong. Please try again.', 'error')
        return redirect(url_for('my_expenses'))

@app.route('/api/remind_loan/<expense_id>', methods=['POST'])
def remind_loan(expense_id):
    """Task 13 Debt Recovery Logic"""
    if 'username' not in session: 
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401
    
    from bson.objectid import ObjectId
    try:
        username = session['username']
        exp = mongo.db.daily_expenses.find_one({'_id': ObjectId(expense_id), 'username': username, 'is_loan': True})
        if not exp:
            return jsonify({'success': False, 'error': 'Record not found'}), 404
            
        friend_email = exp.get('friend_email')
        friend_name = exp.get('friend_name', 'Friend')
        desc = exp.get('description', 'Loan')
        amount = exp.get('amount', 0)
        
        if not friend_email:
            return jsonify({'success': False, 'error': 'No email on file for friend'}), 400
            
        import threading
        # Call the async sender defined earlier
        threading.Thread(target=send_gentle_reminder_async, args=(friend_email, friend_name, username, amount, desc)).start()
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/mark_loan_paid/<expense_id>', methods=['POST'])
def mark_loan_paid(expense_id):
    """Task 15 The Money Return Celebration Logic"""
    if 'username' not in session: 
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401
    
    from bson.objectid import ObjectId
    try:
        username = session['username']
        exp = mongo.db.daily_expenses.find_one({'_id': ObjectId(expense_id), 'username': username, 'is_loan': True})
        if not exp:
            return jsonify({'success': False, 'error': 'Record not found'}), 404
            
        # Update ledger status securely
        mongo.db.daily_expenses.update_one(
            {'_id': exp['_id']},
            {'$set': {'loan_status': 'paid'}}
        )
        
        # User requested: Decreasing expenses because they received the money back
        # Simply re-syncing the ledger automatically achieves this mathematically!
        new_spent, new_bal = sync_user_ledger(username)
        
        return jsonify({'success': True, 'new_balance': new_bal})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
@app.route('/delete_expense/<expense_id>', methods=['POST'])
def delete_expense(expense_id):
    """Task 4 — Expense Deletion logic."""
    username = session.get('username')
    if not username or mongo.db is None:
        flash('Unauthorized or database offline.', 'error')
        return redirect(url_for('my_profile'))

    try:
        exp = mongo.db.daily_expenses.find_one({'_id': ObjectId(expense_id), 'username': username})
        if not exp:
            flash('Expense not found or unauthorized.', 'error')
            return redirect(url_for('my_expenses'))

        mongo.db.daily_expenses.delete_one({'_id': ObjectId(expense_id)})

        # Universally sync user ledger safely regardless of loan status
        new_spent, new_bal = sync_user_ledger(username)

        session['play_crumple'] = True
        flash('Expense deleted successfully.', 'success')

    except Exception as e:
        print(f"[DB] Delete expense error: {e}")
        flash('Could not delete expense.', 'error')

    return redirect(url_for('my_expenses'))


@app.route('/api/spend_data')
def spend_data():
    """Chart.js data API (Task 8 placeholder)."""
    return jsonify({
        "categories": ["Educational","Lifestyle","Healthy Food","Junk Food","Hostel Rent","Travelling"],
        "amounts":    [1200, 500, 800, 300, 5000, 450]
    })

@app.route('/api/timeline_data')
def timeline_data():
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    from datetime import datetime, timedelta
    username = session['username']
    now = datetime.now()

    
    # Hourly
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    hourly_labels = [f"{h:02d}:00" for h in range(24)]
    hourly_data = [0] * 24
    
    for exp in mongo.db.daily_expenses.find({'username': username, 'expense_date': {'$gte': today_start}}):
        h = exp['expense_date'].hour
        hourly_data[h] += round(float(exp.get('amount', 0)), 2)
        
    # Daily (last 30 days)
    thirty_days_ago = today_start - timedelta(days=29)
    daily_labels = [(thirty_days_ago + timedelta(days=d)).strftime('%b %d') for d in range(30)]
    daily_data = [0] * 30
    
    for exp in mongo.db.daily_expenses.find({'username': username, 'expense_date': {'$gte': thirty_days_ago}}):
        days_diff = (exp['expense_date'] - thirty_days_ago).days
        if 0 <= days_diff < 30:
            daily_data[days_diff] += round(float(exp.get('amount', 0)), 2)
            
    return jsonify({
        'hourly': {'labels': hourly_labels, 'data': hourly_data},
        'daily': {'labels': daily_labels, 'data': daily_data}
    })


@app.errorhandler(404)
def page_not_found(error):
    return render_template('404.html'), 404


@app.errorhandler(413)
def file_too_large(error):
    flash('File is too large. Maximum size is 5MB.', 'error')
    return redirect(url_for('my_expenses'))


import threading
import time

def reminder_scheduler():
    """Background Daemon for Task 12: Smart Reminder Automation"""
    # Wait 5 seconds on startup as explicitly requested by USER
    time.sleep(5)
    
    with app.app_context():
        while True:
            try:
                now = datetime.now()
                for rec in mongo.db.recurring_payments.find({'status': 'pending'}):
                    rec_due = rec.get('due_date')
                    remind_days = rec.get('reminder_days', 0)
                    
                    if rec_due:
                        threshold_date = rec_due.date() - timedelta(days=remind_days)
                        if now.date() >= threshold_date:
                            # Danger zone detected! Check spam lock:
                            last_sent_for = rec.get('reminder_sent_for')
                            if last_sent_for != rec_due:
                                username = rec.get('username')
                                user = mongo.db.users.find_one({'name': username})
                                if user and user.get('email'):
                                    days_left = (rec_due.date() - now.date()).days
                                    send_reminder_email_async(
                                        user['email'], 
                                        username, 
                                        rec['title'], 
                                        rec['amount'], 
                                        rec_due.strftime('%b %d, %Y'), 
                                        max(0, days_left)
                                    )
                                    # Lock it
                                    mongo.db.recurring_payments.update_one(
                                        {'_id': rec['_id']},
                                        {'$set': {'reminder_sent_for': rec_due}}
                                    )
            except Exception as e:
                print("[Daemon] Error in reminder scheduler:", e)
                
            # Sleep for 6 hours
            time.sleep(21600)

# Start the thread securely outside the request context
threading.Thread(target=reminder_scheduler, daemon=True).start()

if __name__ == '__main__':
    app.run(debug=True, port=5000, use_reloader=False)