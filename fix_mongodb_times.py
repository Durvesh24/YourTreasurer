from app import app, mongo
from datetime import datetime, timedelta
import random

with app.app_context():
    now = datetime.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    cursor = mongo.db.daily_expenses.find({'expense_date': today_start})
    
    for exp in cursor:
        if exp['expense_date'].hour == 0 and exp['expense_date'].minute == 0:
            random_hour = random.randint(8, max(8, now.hour))
            random_min = random.randint(0, 59)
            new_time = today_start.replace(hour=random_hour, minute=random_min)
            mongo.db.daily_expenses.update_one(
                {'_id': exp['_id']},
                {'$set': {'expense_date': new_time}}
            )
            print(f"Updated {exp['description']} to {new_time}")
