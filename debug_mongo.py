from app import app, mongo
with app.app_context():
    users = list(mongo.db.users.find())
    for u in users:
        print(f"User: {u['name']} | Limit: {u['monthly_limit']} | Spent: {u['total_spent']} | Balance: {u['balance']}")
        exps = list(mongo.db.daily_expenses.find({'username': u['name']}))
        sum_non_loan = sum(e['amount'] for e in exps if not e.get('is_loan', False))
        sum_all = sum(e['amount'] for e in exps)
        loans = [e['amount'] for e in exps if e.get('is_loan', False)]
        paid_loans = [e['amount'] for e in exps if e.get('is_loan', False) and e.get('loan_status') == 'paid']
        print(f"  -> Aggregated Non-Loans: {sum_non_loan}")
        print(f"  -> Aggregated All: {sum_all}")
        print(f"  -> Loans: {loans}")
        print(f"  -> Paid Loans: {paid_loans}")
        print('-'*30)
