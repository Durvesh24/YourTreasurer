from app import app, mongo
from datetime import datetime

with app.app_context():
    now = datetime.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for exp in mongo.db.daily_expenses.find({'expense_date': {'$gte': today_start}}):
        print(exp.get('description'), exp.get('expense_date'))
