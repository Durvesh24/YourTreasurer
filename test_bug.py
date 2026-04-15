import requests

session = requests.Session()
login_data = {
    'form_type': 'login',
    'name': 'Duru',
    'password': '12Sheth34'
}

response = session.post('http://127.0.0.1:5000/my_profile', data=login_data)
print("Login Status:", response.status_code)

expense_data = {
    'category': 'Others',
    'amount': '50',
    'description': 'Test',
}
# Set headers to get json response which returns the actual error!
headers = {'Accept': 'application/json'}

res = session.post('http://127.0.0.1:5000/add_expense', data=expense_data, headers=headers)
print("Expense Response:", res.status_code)
print("Body:", res.text)
