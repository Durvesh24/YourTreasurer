from flask import Flask, render_template, request, flash, redirect, url_for, jsonify, session
from flask_pymongo import PyMongo
from flask_mail import Mail, Message
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
import os
import re
import uuid
import threading
import time
import cloudinary
import cloudinary.uploader
from datetime import datetime, timedelta

from dotenv import load_dotenv
load_dotenv()  # Loads variables from .env into os.environ

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "campuscoin_tracker_2026")

# ── Jinja2 custom filter: Indian number format ──────────────────────────────
@app.template_filter('format_inr')
def format_inr(value):
    """Format a number as Indian rupee with commas (e.g. 1,00,000)."""
    try:
        value = int(value)
        s = str(value)
        if len(s) <= 3:
            return f"₹{s}"
        last3 = s[-3:]
        rest   = s[:-3]
        parts  = [rest[max(i-2,0):i] for i in range(len(rest), 0, -2)][::-1]
        return f"₹{','.join(p for p in parts if p)},{last3}"
    except Exception:
        return f"₹{value}"

# ──────────────────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────────────────

# 1. Cloudinary Setup (Task 5 — receipt uploads)
cloudinary.config(
    cloud_name = os.environ.get("CLOUDINARY_NAME", "your_cloud_name"),
    api_key    = os.environ.get("CLOUDINARY_KEY",  "your_api_key"),
    api_secret = os.environ.get("CLOUDINARY_SECRET","your_api_secret")
)

# 2. MongoDB Atlas — full URI stored in .env
app.config["MONGO_URI"] = os.environ.get("MONGO_URI", "")
mongo = PyMongo(app)

# 3. Flask-Mail (Task 6 & 7)
app.config['MAIL_SERVER']   = 'smtp.gmail.com'
app.config['MAIL_PORT']     = 587
app.config['MAIL_USE_TLS']  = True
app.config['MAIL_USERNAME'] = os.environ.get("MAIL_USER", "your_email@gmail.com")
app.config['MAIL_PASSWORD'] = os.environ.get("MAIL_PASS", "your_app_password")
mail = Mail(app)

app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # 5 MB receipt limit

# ──────────────────────────────────────────────────────────
# DB INDEXES — created once at startup (auto-ignored if they already exist)
# ──────────────────────────────────────────────────────────
with app.app_context():
    try:
        # name_lower and email_lower are unique — prevents duplicate accounts
        mongo.db.users.create_index([("name_lower", 1)], unique=True,
                                     name="unique_name_lower")
        mongo.db.users.create_index([("email_lower", 1)], unique=True,
                                     name="unique_email_lower")
        print("[DB] ✅ Indexes ready.")
    except Exception as e:
        print(f"[DB] Index note: {e}")

# ──────────────────────────────────────────────────────────
# VALIDATION HELPERS
# ──────────────────────────────────────────────────────────

def validate_name(name):
    """
    - Min 3, max 40 characters
    - Letters only (spaces, hyphens, apostrophes allowed between letters)
    - No numbers, no special symbols, must start with a letter
    """
    n = name.strip() if name else ''
    if len(n) < 3:
        return "Name must be at least 3 characters long."
    if len(n) > 40:
        return "Name must be 40 characters or fewer."
    if not re.match(r"^[A-Za-z][A-Za-z\s'\-]{2,39}$", n):
        return "Name must contain only letters. Spaces, hyphens, and apostrophes are allowed."
    if re.search(r"[^A-Za-z\s'\-]", n):
        return "Name cannot contain numbers or special characters."
    # No consecutive spaces or hyphens
    if re.search(r"[\s\-']{2,}", n):
        return "Name cannot have consecutive spaces, hyphens, or apostrophes."
    return None

def validate_email(email):
    """
    - Required field
    - Valid format (user@domain.tld)
    - Max 100 characters
    - Blocks known disposable email providers
    """
    if not email or not email.strip():
        return "Email address is required."
    e = email.strip()
    if len(e) > 100:
        return "Email address is too long (max 100 characters)."
    pattern = r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$"
    if not re.match(pattern, e):
        return "Please enter a valid email address (e.g. you@gmail.com)."
    blocked_domains = ['mailinator.com', 'guerrillamail.com', 'trashmail.com',
                       'tempmail.com', 'throwaway.email', 'yopmail.com']
    domain = e.split('@')[-1].lower()
    if domain in blocked_domains:
        return "Please use a real email address, not a disposable one."
    return None

def validate_password(password):
    """
    - Min 8 characters
    - At least 1 uppercase letter
    - At least 1 lowercase letter
    - At least 1 digit
    - No spaces allowed
    """
    if not password:
        return "Password is required."
    if ' ' in password:
        return "Password cannot contain spaces."
    if len(password) < 8:
        return "Password must be at least 8 characters long."
    if len(password) > 64:
        return "Password must be 64 characters or fewer."
    if not re.search(r"[A-Z]", password):
        return "Password must contain at least one uppercase letter (A-Z)."
    if not re.search(r"[a-z]", password):
        return "Password must contain at least one lowercase letter (a-z)."
    if not re.search(r"\d", password):
        return "Password must contain at least one number (0-9)."
    return None

def validate_budget(value):
    """Monthly budget: numeric, between ₹100 and ₹10,00,000."""
    try:
        amount = float(value)
    except (ValueError, TypeError):
        return "Please enter a valid number for monthly budget."
    if amount < 100:
        return "Monthly budget must be at least ₹100."
    if amount > 1_000_000:
        return "Monthly budget cannot exceed ₹10,00,000."
    return None

# ──────────────────────────────────────────────────────────
# ASYNC EMAIL HELPER
# ──────────────────────────────────────────────────────────

def send_async_email(app, msg):
    """Send email in a background thread so the UI stays responsive."""
    with app.app_context():
        try:
            mail.send(msg)
            print("[MAIL] ✅ Email sent successfully.")
        except Exception as e:
            print(f"[MAIL] ❌ Background mail error: {e}")

# ──────────────────────────────────────────────────────────
# ZERO-PERSISTENCE AUTH GUARD (before every request)
# ──────────────────────────────────────────────────────────

# Routes that are publicly accessible without login
_PUBLIC_ROUTES = {'my_profile', 'logout', 'about_us', 'static'}

@app.before_request
def require_login():
    """
    Task 1 — Zero-Persistence Rule:
    Every protected page checks the session. No cached user state.
    If not logged in, redirect to the Budget Gateway.
    """
    if request.endpoint in _PUBLIC_ROUTES or request.endpoint is None:
        return  # Public page — allow through
    if 'username' not in session:
        flash('Please log in to access your vault. 🔐', 'warning')
        return redirect(url_for('my_profile'))

# ──────────────────────────────────────────────────────────
# CORE ROUTES
# ──────────────────────────────────────────────────────────

# Valid expense categories
EXPENSE_CATEGORIES = [
    'Educational', 'Lifestyle', 'Healthy Food',
    'Junk Food', 'Hostel Rent', 'Travelling', 'Other'
]

@app.route('/')
def home():
    """Home dashboard — fetches live budget stats and today's expenses."""
    username = session.get('username')

    # Zero-Persistence: always re-fetch from DB
    user = mongo.db.users.find_one({'name': username}, {'password': 0})

    # Today's expense summary
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    today_expenses = list(
        mongo.db.daily_expenses
        .find({'username': username, 'created_at': {'$gte': today_start}})
        .sort('created_at', -1)
        .limit(5)
    )
    today_total = sum(
        e.get('amount', 0) for e in today_expenses if not e.get('is_loan', False)
    )

    # Check if coins animation should play (set by add_expense on success)
    play_coins = session.pop('play_coins', False)

    return render_template(
        'index.html',
        user=user,
        today_expenses=today_expenses,
        today_total=today_total,
        categories=EXPENSE_CATEGORIES,
        play_coins=play_coins,
    )


@app.route('/my_profile', methods=['GET', 'POST'])
def my_profile():
    """
    Task 1 — The Secure Budget Gateway.
    GET  → Show login/register form.
    POST → Handle login or new-user registration with full validation + 30-day reset.
    """
    # Already logged in → go home
    if 'username' in session:
        return redirect(url_for('home'))

    if request.method == 'POST':
        form_type = request.form.get('form_type', '').strip()
        name      = request.form.get('name', '').strip()
        password  = request.form.get('password', '').strip()

        # ── Universal field validation ────────────────────────────────────
        name_err = validate_name(name)
        if name_err:
            flash(name_err, 'error')
            return redirect(url_for('my_profile'))

        pass_err = validate_password(password)
        if pass_err:
            flash(pass_err, 'error')
            return redirect(url_for('my_profile'))

        users = mongo.db.users  # Atlas auto-creates this collection on first write

        # ═══════════════════════════════════════════════════════════════
        # REGISTER — Create a new user vault
        # ═══════════════════════════════════════════════════════════════
        if form_type == 'register':
            email         = request.form.get('email', '').strip().lower()
            monthly_limit = request.form.get('monthly_limit', '').strip()

            # Validate email
            email_err = validate_email(email)
            if email_err:
                flash(email_err, 'error')
                return redirect(url_for('my_profile'))

            # Validate budget
            budget_err = validate_budget(monthly_limit)
            if budget_err:
                flash(budget_err, 'error')
                return redirect(url_for('my_profile'))

            # Duplicate name check (case-insensitive)
            if users.find_one({'name_lower': name.lower()}):
                flash(f'The name "{name}" is already taken. Please choose a different name or log in.', 'error')
                return redirect(url_for('my_profile'))

            # Duplicate email check
            if users.find_one({'email_lower': email}):
                flash('This email is already registered. Please log in instead.', 'error')
                return redirect(url_for('my_profile'))

            # Hash password securely (bcrypt via werkzeug)
            hashed_pw = generate_password_hash(password, method='pbkdf2:sha256', salt_length=16)

            now = datetime.utcnow()

            # ── Full user document stored in MongoDB ──────────────────────
            user_doc = {
                # Identity
                'name'          : name,          # Display name (original casing)
                'name_lower'    : name.lower(),  # For case-insensitive lookups & unique index
                'email'         : email,         # Stored lowercase
                'email_lower'   : email,         # Used by unique index
                'password'      : hashed_pw,     # Bcrypt hash — never stored plain

                # Budget
                'monthly_limit' : float(monthly_limit),
                'total_spent'   : 0.0,
                'balance'       : float(monthly_limit),  # Precomputed for quick display

                # 30-Day cycle
                'start_date'    : now,
                'cycle_number'  : 1,             # How many 30-day cycles completed

                # Guardian mail flags (Task 6) — reset each cycle
                'alert_10_sent' : False,
                'alert_5_sent'  : False,
                'over_budget'   : False,

                # Meta
                'created_at'    : now,
                'last_login'    : now,
                'login_count'   : 1,
            }

            try:
                users.insert_one(user_doc)
            except Exception as e:
                print(f"[DB] Register error: {e}")
                flash('Could not create account. Please try again.', 'error')
                return redirect(url_for('my_profile'))

            # Auto-login after registration
            session['username']      = name
            session['email']         = email
            session['monthly_limit'] = float(monthly_limit)
            flash(f'Welcome aboard, {name}! 🎉 Your vault is ready.', 'success')
            return redirect(url_for('home'))

        # ═══════════════════════════════════════════════════════════════
        # LOGIN — Authenticate existing user
        # ═══════════════════════════════════════════════════════════════
        elif form_type == 'login':
            # Zero-Persistence: ALWAYS fetch fresh from DB — no cached state
            user = users.find_one({'name_lower': name.lower()})

            if not user:
                flash('No account found with that name. Please register first.', 'error')
                return redirect(url_for('my_profile'))

            if not check_password_hash(user['password'], password):
                flash('Incorrect password. Please try again.', 'error')
                return redirect(url_for('my_profile'))

            # ── 30-Day Temporal Reset Logic ───────────────────────────────
            start_date  = user.get('start_date', datetime.utcnow())
            days_passed = (datetime.utcnow() - start_date).days

            if days_passed >= 30:
                # Archive the closing month's data
                try:
                    mongo.db.monthly_archives.insert_one({
                        'username'      : user['name'],
                        'email'         : user.get('email', ''),
                        'cycle_number'  : user.get('cycle_number', 1),
                        'total_spent'   : user.get('total_spent', 0.0),
                        'monthly_limit' : user.get('monthly_limit', 0.0),
                        'period_start'  : start_date,
                        'period_end'    : datetime.utcnow(),
                        'archived_at'   : datetime.utcnow(),
                    })
                except Exception as e:
                    print(f"[DB] Archive error: {e}")

                # Reset for new 30-day cycle
                new_limit = user.get('monthly_limit', 0.0)
                try:
                    users.update_one(
                        {'_id': user['_id']},
                        {'$set': {
                            'total_spent'   : 0.0,
                            'balance'       : new_limit,
                            'start_date'    : datetime.utcnow(),
                            'alert_10_sent' : False,
                            'alert_5_sent'  : False,
                            'over_budget'   : False,
                        },
                        '$inc': {'cycle_number': 1}}
                    )
                except Exception as e:
                    print(f"[DB] Reset error: {e}")

                # Re-fetch updated document
                user = users.find_one({'_id': user['_id']})
                flash('📅 30-day cycle complete! Your budget has been reset for the new month.', 'warning')

            # Update last_login and increment login_count
            try:
                users.update_one(
                    {'_id': user['_id']},
                    {'$set': {'last_login': datetime.utcnow()},
                     '$inc': {'login_count': 1}}
                )
            except Exception as e:
                print(f"[DB] Login update error: {e}")

            # ── Store session (minimal — DB is source of truth) ───────────
            session['username']      = user['name']             # Original casing
            session['email']         = user.get('email', '')
            session['monthly_limit'] = user.get('monthly_limit', 0.0)

            flash(f'Welcome back, {user["name"]}! 🔓 Vault unlocked.', 'success')
            return redirect(url_for('home'))

        else:
            flash('Invalid form submission.', 'error')
            return redirect(url_for('my_profile'))

    # GET — show the gateway page
    return render_template('profile.html')


@app.route('/logout')
def logout():
    """Securely clear session and return to Budget Gateway."""
    username = session.get('username', 'User')
    session.clear()
    flash(f'👋 Goodbye, {username}! You have been logged out securely.', 'success')
    return redirect(url_for('my_profile'))


# ──────────────────────────────────────────────────────────
# OTHER NAVIGATION ROUTES
# ──────────────────────────────────────────────────────────

@app.route('/my_expenses')
def my_expenses():
    return render_template('expenses.html')

@app.route('/analysis')
def analysis():
    return render_template('analysis.html')

@app.route('/interval_spend')
def interval_spend():
    return render_template('interval_spend.html')

@app.route('/about_us')
def about_us():
    return render_template('about_us.html')

# ──────────────────────────────────────────────────────────
# DATA SUBMISSION ROUTES (Tasks 2–7 — TODO placeholders)
# ──────────────────────────────────────────────────────────

@app.route('/add_expense', methods=['POST'])
def add_expense():
    """
    Task 2 — Daily Expense & Loan Entry.
    Validates input, inserts into daily_expenses collection,
    updates user's total_spent & balance, sets coin animation flag.
    """
    username = session.get('username')

    try:
        # ── Read form fields ──────────────────────────────────────────────
        category    = request.form.get('category', '').strip()
        amount_str  = request.form.get('amount', '').strip()
        description = request.form.get('description', '').strip()
        exp_date_str= request.form.get('expense_date', '').strip()
        is_loan     = request.form.get('is_loan') == 'on'

        # ── Validate required fields ──────────────────────────────────────
        if not category or category not in EXPENSE_CATEGORIES:
            flash('Please select a valid expense category.', 'error')
            return redirect(url_for('home'))

        if not amount_str:
            flash('Amount is required.', 'error')
            return redirect(url_for('home'))

        try:
            amount = round(float(amount_str), 2)
            if amount <= 0:
                flash('Amount must be greater than zero.', 'error')
                return redirect(url_for('home'))
            if amount > 100000:
                flash('Single expense cannot exceed \u20b91,00,000.', 'error')
                return redirect(url_for('home'))
        except ValueError:
            flash('Please enter a valid number for amount.', 'error')
            return redirect(url_for('home'))

        # ── Parse expense date ────────────────────────────────────────────
        try:
            expense_date = datetime.strptime(exp_date_str, '%Y-%m-%d') if exp_date_str else datetime.utcnow()
        except ValueError:
            expense_date = datetime.utcnow()

        # ── Build base expense document ───────────────────────────────────
        expense_doc = {
            'username'     : username,
            'category'     : category,
            'amount'       : amount,
            'description'  : description or f'{category} expense',
            'expense_date' : expense_date,
            'is_loan'      : is_loan,
            'created_at'   : datetime.utcnow(),
        }

        # ── Loan-specific fields ──────────────────────────────────────────
        if is_loan:
            friend_name  = request.form.get('friend_name', '').strip()
            friend_email = request.form.get('friend_email', '').strip()
            relationship = request.form.get('relationship', '').strip()

            if not friend_name or not friend_email:
                flash('Friend name and email are required for a loan entry.', 'error')
                return redirect(url_for('home'))

            # Basic email check
            if not re.match(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$", friend_email):
                flash('Please enter a valid email for your friend.', 'error')
                return redirect(url_for('home'))

            expense_doc.update({
                'friend_name' : friend_name,
                'friend_email': friend_email,
                'relationship': relationship or 'Friend',
                'loan_status' : 'pending',   # pending | returned
            })

        # ── Insert into daily_expenses (auto-created by Atlas) ────────────
        mongo.db.daily_expenses.insert_one(expense_doc)

        # ── Update user's running totals (only for real expenses, not loans) ──
        if not is_loan:
            user = mongo.db.users.find_one({'name': username})
            if user:
                new_spent   = round(user.get('total_spent', 0.0) + amount, 2)
                monthly_lim = user.get('monthly_limit', 0.0)
                new_balance = round(monthly_lim - new_spent, 2)

                mongo.db.users.update_one(
                    {'name': username},
                    {'$set': {
                        'total_spent': new_spent,
                        'balance'    : new_balance,
                        'over_budget': new_balance < 0,
                    }}
                )

                # ── Guardian threshold check (Task 6 placeholder) ─────────
                if monthly_lim > 0:
                    remaining_pct = (new_balance / monthly_lim) * 100
                    alert_10 = user.get('alert_10_sent', False)
                    alert_5  = user.get('alert_5_sent', False)

                    if remaining_pct <= 0 and not user.get('over_budget', False):
                        # TODO Task 6: send over-budget email every time
                        pass
                    elif remaining_pct <= 5 and not alert_5:
                        # TODO Task 6: send 5% warning email once
                        pass
                    elif remaining_pct <= 10 and not alert_10:
                        # TODO Task 6: send 10% caution email once
                        pass

        # ── Trigger coin animation on next home load ──────────────────────
        session['play_coins'] = True

        if is_loan:
            flash(f'Loan of \u20b9{amount:,.2f} to {request.form.get("friend_name", "friend")} recorded! \U0001f91d', 'success')
        else:
            flash(f'\u20b9{amount:,.2f} tracked under {category}! Keep it up! \U0001f4b0', 'success')

        return redirect(url_for('home'))

    except Exception as e:
        print(f'[Expense] Unexpected error: {e}')
        flash('Something went wrong. Please try again.', 'error')
        return redirect(url_for('home'))

@app.route('/add_friend_loan', methods=['POST'])
def add_friend_loan():
    """Handles logging a friend loan (Task 7)."""
    try:
        # TODO Task 7: Save loan, send Flask-Mail to friend's email
        return redirect(url_for('my_expenses'))
    except Exception as e:
        return "Internal Error", 500

@app.route('/add_interval_spend', methods=['POST'])
def add_interval_spend():
    """Handles EMIs / rent / subscriptions (Task 11)."""
    try:
        # TODO Task 11: Save to recurring_payments, calculate due date
        return redirect(url_for('interval_spend'))
    except Exception as e:
        return "Internal Error", 500

# ──────────────────────────────────────────────────────────
# API ROUTES (Tasks 8–10)
# ──────────────────────────────────────────────────────────

@app.route('/api/spend_data')
def spend_data():
    """API for Chart.js doughnut / line charts (Task 8)."""
    # TODO Task 8: Replace dummy data with real MongoDB $group aggregation
    dummy_data = {
        "categories": ["Educational", "Lifestyle", "Healthy Food", "Junk Food", "Hostel Rent", "Travelling"],
        "amounts":    [1200, 500, 800, 300, 5000, 450]
    }
    return jsonify(dummy_data)

# ──────────────────────────────────────────────────────────
# ERROR HANDLERS
# ──────────────────────────────────────────────────────────

@app.errorhandler(413)
def request_entity_too_large(error):
    return ("<h1>Receipt file is too large!</h1>"
            "<p>Please keep your screenshot under 5MB.</p>"
            "<a href='/my_expenses'>Try Again</a>"), 413

@app.errorhandler(404)
def page_not_found(error):
    flash('Page not found. Redirecting to home.', 'warning')
    return redirect(url_for('home'))

# ──────────────────────────────────────────────────────────
# RUN
# ──────────────────────────────────────────────────────────

if __name__ == '__main__':
    app.run(debug=True, port=5000)