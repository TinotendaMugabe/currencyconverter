import requests
from flask import Flask, render_template, request

app = Flask(__name__)

API_BASE_URL = "https://api.frankfurter.app"

@app.route('/')
def index():
    try:
        # Fetch available currencies
        currencies_response = requests.get(f"{API_BASE_URL}/currencies")
        currencies_response.raise_for_status() # Raise an exception for bad status codes
        currencies = currencies_response.json()
        return render_template('index.html', currencies=currencies)
    except requests.exceptions.RequestException as e:
        return f"Error fetching currencies: {e}", 500

@app.route('/convert', methods=['POST'])
def convert():
    try:
        from_currency = request.form.get('from_currency')
        to_currency = request.form.get('to_currency')
        try:
            amount = float(request.form.get('amount'))
        except (ValueError, TypeError):
            return "Invalid amount entered.", 400

        if not from_currency or not to_currency or amount is None:
            return "Missing form data.", 400

        if from_currency == to_currency:
             converted_amount = amount
             rate = 1.0
        else:
            # Fetch conversion rate
            conversion_url = f"{API_BASE_URL}/latest?amount={amount}&from={from_currency}&to={to_currency}"
            conversion_response = requests.get(conversion_url)
            conversion_response.raise_for_status()
            data = conversion_response.json()

            if not data or 'rates' not in data or to_currency not in data['rates']:
                 return f"Could not get conversion rate for {from_currency} to {to_currency}", 500

            converted_amount = data['rates'][to_currency]
            rate = data['rates'][to_currency] / amount # Calculate the rate for display

        # Fetch currencies again for the template
        currencies_response = requests.get(f"{API_BASE_URL}/currencies")
        currencies_response.raise_for_status()
        currencies = currencies_response.json()

        return render_template('index.html',
                               currencies=currencies,
                               from_currency=from_currency,
                               to_currency=to_currency,
                               amount=amount,
                               converted_amount=f"{converted_amount:.2f}",
                               rate=f"{rate:.4f}")

    except requests.exceptions.RequestException as e:
        return f"Error during conversion: {e}", 500
    except Exception as e:
        # Catch other potential errors
        return f"An unexpected error occurred: {e}", 500


if __name__ == '__main__':
    app.run(debug=True) 