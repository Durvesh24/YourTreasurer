from flask import Flask, render_template, request, flash, redirect, url_for, jsonify, session
from flask_mail import Mail, Message
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

# ──────────────────────────────────────────────────────────────
# FLASK-MAIL (Task 6 & 7)
# ──────────────────────────────────────────────────────────────
app.config['MAIL_SERVER']   = 'smtp.gmail.com'
app.config['MAIL_PORT']     = 587
app.config['MAIL_USE_TLS']  = True
app.config['MAIL_USERNAME'] = os.environ.get("MAIL_USER", "")
app.config['MAIL_PASSWORD'] = os.environ.get("MAIL_PASS", "")
mail = Mail(app)

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
# ROUTES
# ──────────────────────────────────────────────────────────────

@app.route('/')
def home():
    """Public landing page."""
    return render_template('index.html')


@app.route('/my_profile', methods=['GET', 'POST'])
def my_profile():
    """Login / Register — The Secure Budget Gateway."""
    if 'username' in session:
        return redirect(url_for('home'))

    if mongo.db is None:
        flash('Database is currently unreachable. Please try again later.', 'error')
        return render_template('profile.html')

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
            start_date = user.get('start_date', datetime.utcnow())
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

    return render_template('profile.html')


@app.route('/logout')
def logout():
    """Clear session and return to login."""
    username = session.get('username', 'User')
    session.clear()
    flash(f'Goodbye, {username}! You have been logged out.', 'success')
    return redirect(url_for('my_profile'))


@app.route('/my_expenses')
def my_expenses():
    """Task 2 & 3 — Expense entry and history page."""
    username   = session.get('username')
    if mongo.db is None:
        return render_template('expenses.html', user=None, categories=EXPENSE_CATEGORIES, expenses=[])

    user = mongo.db.users.find_one({'name': username}, {'password': 0})
    
    # Task 3: Real-Time spend history with auto-seeding
    expenses = list(mongo.db.daily_expenses.find({'username': username}).sort('expense_date', -1))
    
    # Auto-seed minimum 8 dummy expenses if none exist (per Competition Rule Task 3)
    if not expenses:
        from datetime import timedelta
        now = datetime.utcnow()
        dummy_data = [
            {'username': username, 'category': 'Educational', 'amount': 1200.0, 'description': 'Textbooks bundle', 'expense_date': now - timedelta(days=1), 'is_loan': False, 'created_at': now},
            {'username': username, 'category': 'Hostel Rent', 'amount': 5000.0, 'description': 'Monthly rent', 'expense_date': now - timedelta(days=2), 'is_loan': False, 'created_at': now},
            {'username': username, 'category': 'Junk Food', 'amount': 350.0, 'description': 'Late night pizza', 'expense_date': now - timedelta(days=3), 'is_loan': False, 'created_at': now},
            {'username': username, 'category': 'Travelling', 'amount': 800.0, 'description': 'Train ticket home', 'expense_date': now - timedelta(days=5), 'is_loan': False, 'created_at': now},
            {'username': username, 'category': 'Healthy Food', 'amount': 450.0, 'description': 'Fruits and groceries', 'expense_date': now - timedelta(days=6), 'is_loan': False, 'created_at': now},
            {'username': username, 'category': 'Lifestyle', 'amount': 1500.0, 'description': 'New sneakers', 'expense_date': now - timedelta(days=8), 'is_loan': False, 'created_at': now},
            {'username': username, 'category': 'Other', 'amount': 200.0, 'description': 'Stationery supplies', 'expense_date': now - timedelta(days=10), 'is_loan': False, 'created_at': now},
            {'username': username, 'category': 'Lifestyle', 'amount': 500.0, 'description': 'Movie ticket', 'expense_date': now - timedelta(days=12), 'is_loan': True, 'friend_name': 'Rahul', 'friend_email': 'rahul@example.com', 'relationship': 'Classmate', 'loan_status': 'pending', 'created_at': now}
        ]
        try:
            mongo.db.daily_expenses.insert_many(dummy_data)
            
            # Recalculate total spent against the balance so the budget matches the seeds
            total_dummy_spent = sum(d['amount'] for d in dummy_data if not d['is_loan'])
            if user:
                new_spent = round(user.get('total_spent', 0.0) + total_dummy_spent, 2)
                lim       = user.get('monthly_limit', 0.0)
                new_bal   = round(lim - new_spent, 2)
                mongo.db.users.update_one({'name': username}, {'$set': {
                    'total_spent': new_spent, 'balance': new_bal, 'over_budget': new_bal < 0
                }})
            
            expenses = list(mongo.db.daily_expenses.find({'username': username}).sort('expense_date', -1))
        except Exception as e:
            print(f"[DB] Dummy seeding error: {e}")

    play_coins   = session.pop('play_coins', False)
    play_crumple = session.pop('play_crumple', False)
    return render_template('expenses.html', user=user,
                           categories=EXPENSE_CATEGORIES, expenses=expenses, 
                           play_coins=play_coins, play_crumple=play_crumple)


@app.route('/analysis')
def analysis():
    return render_template('analysis.html')


@app.route('/interval_spend')
def interval_spend():
    return render_template('interval_spend.html')


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
            expense_date = datetime.strptime(exp_date_str, '%Y-%m-%d') if exp_date_str else datetime.utcnow()
        except ValueError:
            expense_date = datetime.utcnow()

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
        receipt_file = request.files.get('receipt')
        if receipt_file and receipt_file.filename:
            try:
                upload_result = cloudinary.uploader.upload(
                    receipt_file,
                    folder="yourtreasurer_receipts",
                    resource_type="auto"
                )
                doc['receipt_url'] = upload_result.get('secure_url')
            except Exception as e:
                print(f"[Cloudinary] Upload Error: {e}")
                flash(f'Cloudinary Error: {str(e)}', 'error')

        mongo.db.daily_expenses.insert_one(doc)

        # Update user balance (not for loans)
        if not is_loan:
            user = mongo.db.users.find_one({'name': username})
            if user:
                new_spent = round(user.get('total_spent', 0.0) + amount, 2)
                lim       = user.get('monthly_limit', 0.0)
                new_bal   = round(lim - new_spent, 2)
                mongo.db.users.update_one({'name': username}, {'$set': {
                    'total_spent': new_spent, 'balance': new_bal, 'over_budget': new_bal < 0
                }})

        session['play_coins'] = True
        if is_loan:
            flash(f'Loan of Rs.{amount:,.2f} to {request.form.get("friend_name", "friend")} recorded.', 'success')
        else:
            flash(f'Rs.{amount:,.2f} tracked under {category}.', 'success')

        return redirect(url_for('my_expenses'))

    except Exception as e:
        print(f'[Expense] Error: {e}')
        flash('Something went wrong. Please try again.', 'error')
        return redirect(url_for('my_expenses'))


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

        # Reverse the budget impact if it wasn't a loan
        if not exp.get('is_loan', False):
            user = mongo.db.users.find_one({'name': username})
            if user:
                amount    = float(exp.get('amount', 0.0))
                new_spent = round(max(0.0, user.get('total_spent', 0.0) - amount), 2)
                lim       = user.get('monthly_limit', 0.0)
                new_bal   = round(lim - new_spent, 2)
                mongo.db.users.update_one({'name': username}, {'$set': {
                    'total_spent': new_spent, 'balance': new_bal, 'over_budget': new_bal < 0
                }})

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


@app.errorhandler(404)
def page_not_found(error):
    flash('Page not found.', 'warning')
    return redirect(url_for('home'))


@app.errorhandler(413)
def file_too_large(error):
    flash('File is too large. Maximum size is 5MB.', 'error')
    return redirect(url_for('my_expenses'))


if __name__ == '__main__':
    app.run(debug=True, port=5000)