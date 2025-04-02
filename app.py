from flask import Flask, render_template, request, redirect, url_for, jsonify, session, flash
import requests
import uuid
import sqlite3
import os
import datetime
import logging
from logging.handlers import RotatingFileHandler
import json

# Configure application logging
if not os.path.exists('logs'):
    os.makedirs('logs')
    
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        RotatingFileHandler('logs/app.log', maxBytes=10485760, backupCount=10),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)

# Add a specific QR payment logger
qr_logger = logging.getLogger('qr_payment')
qr_handler = RotatingFileHandler('logs/qr_payment.log', maxBytes=10485760, backupCount=5)
qr_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
qr_logger.addHandler(qr_handler)
qr_logger.addHandler(logging.StreamHandler())
qr_logger.setLevel(logging.DEBUG)

app = Flask(__name__)
app.secret_key = '094f4cacd1e7b665821c40c8a005f1abe9d7e25fe0897c4459d3132a1e55f180'

# Payment Gateway Configuration
API_URL = "https://pay.imb.org.in/api/create-order"
API_KEY = "5e491c82fc0f1aedddc986828462fc84"

# Initialize database
def init_db():
    """Initialize the SQLite database with the required tables and upgrade schema if needed"""
    conn = sqlite3.connect('payments.db')
    cursor = conn.cursor()
    
    # Create the table if it doesn't exist
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id TEXT UNIQUE,
        status TEXT,
        amount TEXT,
        mobile TEXT,
        email TEXT,
        utr TEXT,
        message TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    
    # Check if we need to add new columns
    cursor.execute("PRAGMA table_info(transactions)")
    columns = [column[1] for column in cursor.fetchall()]
    
    # Add request_log column if it doesn't exist
    if 'request_log' not in columns:
        cursor.execute('ALTER TABLE transactions ADD COLUMN request_log TEXT')
        logger.info("Added request_log column to transactions table")
    
    # Add response_log column if it doesn't exist
    if 'response_log' not in columns:
        cursor.execute('ALTER TABLE transactions ADD COLUMN response_log TEXT')
        logger.info("Added response_log column to transactions table")
    
    conn.commit()
    conn.close()
    logger.info("Database initialized and upgraded successfully!")

@app.route('/')
def home():
    """Home route with form to initiate payment"""
    return render_template('home.html')

@app.route('/process-payment', methods=['POST'])
def process_payment():
    """Process payment and redirect to payment gateway"""
    
    try:
        # Extract and validate form data
        amount = request.form.get('amount')
        mobile = request.form.get('mobile')
        email = request.form.get('email')

        # Log the initial form data
        qr_logger.info("===== QR PAYMENT FLOW START =====")
        qr_logger.info(f"Payment form submitted: Amount={amount}, Mobile={mobile}, Email={email}")

        if not amount or not mobile or not email:
            flash("Please fill all required fields", "error")
            qr_logger.warning("Form validation failed: Missing required fields")
            return redirect(url_for('home'))

        # Data validation
        if not amount.isdigit() or int(amount) <= 0:
            flash("Please enter a valid amount", "error")
            qr_logger.warning(f"Form validation failed: Invalid amount '{amount}'")
            return redirect(url_for('home'))

        if not mobile.isdigit() or len(mobile) != 10:
            flash("Please enter a valid 10-digit mobile number", "error")
            qr_logger.warning(f"Form validation failed: Invalid mobile number '{mobile}'")
            return redirect(url_for('home'))

        if '@' not in email or '.' not in email:
            flash("Please enter a valid email address", "error")
            qr_logger.warning(f"Form validation failed: Invalid email '{email}'")
            return redirect(url_for('home'))

        # Store data in session
        logger.info(f"Processing payment: Amount={amount}, Mobile={mobile}, Email={email}")
        session['amount'] = amount
        session['mobile'] = mobile
        session['email'] = email

        # Generate a unique transaction ID
        order_id = str(uuid.uuid4().hex[:12])
        session['order_id'] = order_id
        qr_logger.info(f"Generated order_id: {order_id}")

        # Get the host URL dynamically
        host_url = request.host_url.rstrip('/')
        redirect_url = f"{host_url}/payment-status?order_id={order_id}"
        qr_logger.info(f"Configured redirect URL: {redirect_url}")
        
        payload = {
            "customer_mobile": mobile,
            "user_token": API_KEY,
            "amount": amount,
            "order_id": order_id,
            "redirect_url": redirect_url,
            "remark1": email,
            "remark2": "Payment via Flask app"
        }

        headers = {
            "Content-Type": "application/x-www-form-urlencoded"
        }

        # Store initial transaction in database with request log
        conn = sqlite3.connect('payments.db')
        cursor = conn.cursor()
        request_log = json.dumps({
            'timestamp': datetime.datetime.now().isoformat(),
            'url': API_URL,
            'method': 'POST',
            'headers': dict(headers),
            'payload': payload
        })
        
        cursor.execute('''
        INSERT INTO transactions (order_id, status, amount, mobile, email, message, timestamp, request_log)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (order_id, 'pending', amount, mobile, email, 'Payment initiated', datetime.datetime.now(), request_log))
        conn.commit()
        conn.close()

        # Make API request to payment gateway
        qr_logger.info(f"=== QR REQUEST === Sending API request to: {API_URL}")
        qr_logger.info(f"=== QR REQUEST === Headers: {headers}")
        qr_logger.info(f"=== QR REQUEST === Payload: {payload}")
        
        response = requests.post(API_URL, data=payload, headers=headers)
        
        qr_logger.info(f"=== QR RESPONSE === Status Code: {response.status_code}")
        
        if response.status_code == 200:
            try:
                response_data = response.json()
                qr_logger.info(f"=== QR RESPONSE === Data: {response_data}")
                
                # Store response in database
                conn = sqlite3.connect('payments.db')
                cursor = conn.cursor()
                response_log = json.dumps({
                    'timestamp': datetime.datetime.now().isoformat(),
                    'status_code': response.status_code,
                    'response_data': response_data
                })
                cursor.execute('UPDATE transactions SET response_log = ? WHERE order_id = ?', (response_log, order_id))
                conn.commit()
                conn.close()
                
                payment_url = response_data.get('result', {}).get('payment_url')
                qr_logger.info(f"=== QR GENERATION === Payment URL with QR: {payment_url}")

                if payment_url:
                    # Redirect user to payment gateway
                    qr_logger.info(f"=== QR REDIRECT === Redirecting user to payment gateway URL")
                    return redirect(payment_url)
                else:
                    flash("Payment URL not found in response", "error")
                    qr_logger.error(f"Payment URL not found in response: {response_data}")
                    return redirect(url_for('home'))
            except Exception as e:
                qr_logger.error(f"=== QR RESPONSE ERROR === Failed to parse response: {str(e)}")
                qr_logger.error(f"Response content: {response.text}")
                flash("Error processing payment gateway response", "error")
                return redirect(url_for('home'))
        else:
            qr_logger.error(f"=== QR RESPONSE ERROR === Failed with status code: {response.status_code}")
            qr_logger.error(f"Response content: {response.text}")
            
            # Store error response in database
            conn = sqlite3.connect('payments.db')
            cursor = conn.cursor()
            response_log = json.dumps({
                'timestamp': datetime.datetime.now().isoformat(),
                'status_code': response.status_code,
                'error': response.text
            })
            cursor.execute('UPDATE transactions SET response_log = ?, status = ? WHERE order_id = ?', 
                           (response_log, 'failed', order_id))
            conn.commit()
            conn.close()
            
            flash(f"Failed to create payment order. Please try again.", "error")
            return redirect(url_for('home'))

    except Exception as e:
        qr_logger.exception(f"=== QR PROCESS ERROR === Unhandled exception: {str(e)}")
        flash("An error occurred during payment processing", "error")
        return redirect(url_for('home'))
    
@app.route('/payment-status', methods=['POST', 'GET'])
def payment_status():
    """Handle payment status verification from IMB webhook and redirects"""
    
    logger.info(f"Payment status request received - Method: {request.method}")
    qr_logger.info(f"=== QR CALLBACK === Request received - Method: {request.method}")

    # Handle webhook POST requests
    if request.method == 'POST':
        qr_logger.info(f"=== QR WEBHOOK === Headers: {dict(request.headers)}")
        
        # Capture raw request body
        raw_data = request.get_data().decode('utf-8')
        qr_logger.info(f"=== QR WEBHOOK === Raw Data: {raw_data}")
        
        # Handle both JSON and form-urlencoded data
        if request.is_json:
            data = request.get_json()
            qr_logger.info(f"=== QR WEBHOOK === JSON Data: {data}")
        else:
            data = request.form
            qr_logger.info(f"=== QR WEBHOOK === Form Data: {dict(data)}")

        order_id = data.get('order_id')
        utr = data.get('utr')
        status = data.get('status', 'unknown')

        if not order_id:
            qr_logger.error("=== QR WEBHOOK ERROR === Missing order_id in webhook")
            return jsonify({"error": "Missing order_id"}), 400

        # Extract additional data
        amount = data.get('amount', '0')
        mobile = data.get('customer_mobile', 'N/A')
        email = data.get('remark1', 'N/A')
        message = data.get('message', 'No message')

        # Update transaction in database
        try:
            conn = sqlite3.connect('payments.db')
            cursor = conn.cursor()

            # Check if transaction exists
            cursor.execute('SELECT * FROM transactions WHERE order_id = ?', (order_id,))
            transaction = cursor.fetchone()

            webhook_log = json.dumps({
                'timestamp': datetime.datetime.now().isoformat(),
                'method': 'POST',
                'headers': dict(request.headers),
                'data': dict(data) if not request.is_json else data,
                'raw_data': raw_data
            })

            if transaction:
                # Update existing transaction
                cursor.execute('''
                UPDATE transactions 
                SET status = ?, amount = ?, mobile = ?, email = ?, utr = ?, message = ?, response_log = ?
                WHERE order_id = ?
                ''', (status, amount, mobile, email, utr, message, webhook_log, order_id))
                qr_logger.info(f"=== QR WEBHOOK === Updated existing transaction: {order_id}")
            else:
                # Insert new transaction if not exists
                cursor.execute('''
                INSERT INTO transactions (order_id, status, amount, mobile, email, utr, message, timestamp, response_log)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (order_id, status, amount, mobile, email, utr, message, datetime.datetime.now(), webhook_log))
                qr_logger.info(f"=== QR WEBHOOK === Created new transaction from webhook: {order_id}")

            conn.commit()
            conn.close()

            qr_logger.info(f"=== QR WEBHOOK SUCCESS === Transaction {order_id} updated to status: {status}")
            return jsonify({"success": True, "message": "Payment status updated"}), 200

        except Exception as e:
            qr_logger.exception(f"=== QR WEBHOOK ERROR === Failed to update transaction: {str(e)}")
            return jsonify({"error": str(e)}), 500

    # Handle redirect GET requests
    else:  # GET request
        qr_logger.info(f"=== QR REDIRECT CALLBACK === Query parameters: {dict(request.args)}")
        
        # Get order_id from query params
        order_id = request.args.get('order_id')
        qr_logger.info(f"=== QR REDIRECT CALLBACK === Order ID: {order_id}")

        if not order_id:
            qr_logger.error("=== QR REDIRECT ERROR === Missing order_id in redirect")
            flash("Invalid payment response", "error")
            return redirect(url_for('home'))

        # Retrieve transaction from database and update session
        try:
            conn = sqlite3.connect('payments.db')
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            cursor.execute('SELECT * FROM transactions WHERE order_id = ?', (order_id,))
            transaction = cursor.fetchone()

            redirect_log = json.dumps({
                'timestamp': datetime.datetime.now().isoformat(),
                'method': 'GET',
                'query_params': dict(request.args),
                'headers': dict(request.headers)
            })

            if transaction:
                qr_logger.info(f"=== QR REDIRECT CALLBACK === Found transaction: {dict(transaction)}")
                
                # Update status to success if still pending
                if transaction['status'] == 'pending':
                    cursor.execute('''
                    UPDATE transactions 
                    SET status = ?, response_log = ?
                    WHERE order_id = ?
                    ''', ('success', redirect_log, order_id))
                    conn.commit()
                    qr_logger.info(f"=== QR REDIRECT CALLBACK === Updated status to success")
                
                # Explicitly set/update session variables
                session['order_id'] = order_id
                session['amount'] = transaction['amount']
                session['mobile'] = transaction['mobile']
                session['email'] = transaction['email']
                session['payment_success'] = True
                
                qr_logger.info(f"=== QR REDIRECT CALLBACK === Session updated with order data")
            else:
                qr_logger.warning(f"=== QR REDIRECT CALLBACK === Transaction not found for order_id: {order_id}")
                # Create minimal transaction record if somehow missing
                cursor.execute('''
                INSERT INTO transactions (order_id, status, timestamp, response_log)
                VALUES (?, ?, ?, ?)
                ''', (order_id, 'success', datetime.datetime.now(), redirect_log))
                conn.commit()
                
                # Set minimal session data
                session['order_id'] = order_id
                session['payment_success'] = True
                qr_logger.info(f"=== QR REDIRECT CALLBACK === Created new transaction from redirect")

            conn.close()

        except Exception as e:
            qr_logger.exception(f"=== QR REDIRECT ERROR === Database error: {str(e)}")
            # Even if error occurs, set minimal session data for redirect
            session['order_id'] = order_id
            session['payment_success'] = True

        # Use URL with query parameter to maintain data through redirect
        qr_logger.info(f"=== QR REDIRECT CALLBACK === Redirecting to payment_success with order_id: {order_id}")
        return redirect(url_for('payment_success', order_id=order_id))


@app.route('/payment-success')
def payment_success():
    """Display payment success message"""
    
    # Try to get order_id from query params first, then session
    order_id = request.args.get('order_id') or session.get('order_id')
    
    qr_logger.info(f"=== QR PAYMENT COMPLETED === Success page loaded for order_id: {order_id}")
    
    if not order_id:
        qr_logger.error("=== QR PAYMENT ERROR === Invalid session, no order_id")
        flash("Invalid payment session", "error")
        return redirect(url_for('home'))
    
    # Get transaction details from database
    try:
        conn = sqlite3.connect('payments.db')
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM transactions WHERE order_id = ?', (order_id,))
        transaction = cursor.fetchone()
        
        if transaction:
            # Use database values if available
            qr_logger.info(f"=== QR PAYMENT COMPLETED === Transaction details: {dict(transaction)}")
            amount = transaction['amount']
            mobile = transaction['mobile']
            email = transaction['email']
            status = transaction['status']
            timestamp = transaction['timestamp']
            
            # Update session with these values
            session['amount'] = amount
            session['mobile'] = mobile
            session['email'] = email
        else:
            # Fallback to session values
            qr_logger.warning(f"=== QR PAYMENT WARNING === Transaction not found in database, using session data")
            amount = session.get('amount', 'N/A')
            mobile = session.get('mobile', 'N/A')
            email = session.get('email', 'N/A')
            status = 'success'
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        conn.close()
        qr_logger.info(f"=== QR PAYMENT COMPLETED === Rendering success page with values: amount={amount}, status={status}")
        
        return render_template(
            'success.html', 
            order_id=order_id, 
            amount=amount, 
            mobile=mobile, 
            email=email,
            status=status,
            timestamp=timestamp
        )
    
    except Exception as e:
        qr_logger.exception(f"=== QR PAYMENT ERROR === Error retrieving transaction details: {str(e)}")
        # Use session values as fallback
        amount = session.get('amount', 'N/A')
        mobile = session.get('mobile', 'N/A')
        email = session.get('email', 'N/A')
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        return render_template(
            'success.html', 
            order_id=order_id, 
            amount=amount, 
            mobile=mobile, 
            email=email,
            status='success',
            timestamp=timestamp
        )

# Other routes remain the same...
@app.route('/verify-payment', methods=['POST'])
def verify_payment():
    """API endpoint to verify payments"""
    
    if not request.is_json:
        return jsonify({"error": "Invalid request format. JSON required"}), 400
        
    data = request.get_json()
    logger.info(f"Payment verification request: {data}")
    qr_logger.info(f"=== QR VERIFICATION === Request data: {data}")
    
    order_id = data.get('order_id')
    utr = data.get('utr')
    
    if not order_id:
        qr_logger.warning("=== QR VERIFICATION === Missing order_id in request")
        return jsonify({"error": "Missing order_id"}), 400
    
    # Check database for transaction status
    try:
        conn = sqlite3.connect('payments.db')
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM transactions WHERE order_id = ?', (order_id,))
        transaction = cursor.fetchone()
        conn.close()
        
        if transaction:
            # Convert SQLite row to dict
            transaction_dict = dict(transaction)
            qr_logger.info(f"=== QR VERIFICATION === Found transaction: {transaction_dict}")
            
            verification_result = {
                "status": "success",
                "result": {
                    "status": transaction_dict['status'],
                    "amount": transaction_dict['amount'],
                    "customer_mobile": transaction_dict['mobile'],
                    "remark1": transaction_dict['email'],
                    "utr": transaction_dict['utr'] or utr,
                    "message": transaction_dict['message'] or "Payment verified"
                }
            }
            
            qr_logger.info(f"=== QR VERIFICATION === Verification result: {verification_result}")
            return jsonify(verification_result)
        else:
            # If transaction not found but we have session data
            if order_id == session.get('order_id'):
                qr_logger.info(f"=== QR VERIFICATION === Transaction not in DB but found in session")
                verification_result = {
                    "status": "success",
                    "result": {
                        "status": "success",
                        "amount": session.get('amount', "0"),
                        "customer_mobile": session.get('mobile', "N/A"),
                        "remark1": session.get('email', "N/A"),
                        "utr": utr,
                        "message": "Payment successful"
                    }
                }
                return jsonify(verification_result)
            
            qr_logger.warning(f"=== QR VERIFICATION === Transaction not found for order_id: {order_id}")
            return jsonify({"error": "Transaction not found"}), 404
            
    except Exception as e:
        qr_logger.exception(f"=== QR VERIFICATION ERROR === {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/history')
def history():
    """Display payment history"""
    try:
        conn = sqlite3.connect('payments.db')
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute('SELECT * FROM transactions ORDER BY timestamp DESC')
        transactions = cursor.fetchall()
        conn.close()

        return render_template('history.html', transactions=transactions)

    except Exception as e:
        logger.exception(f"Error in history route: {str(e)}")
        flash(f"Error retrieving payment history: {str(e)}", "error")
        return redirect(url_for('home'))

@app.errorhandler(404)
def page_not_found(e):
    """Handle 404 errors"""
    return render_template('404.html'), 404

@app.errorhandler(500)
def server_error(e):
    """Handle 500 errors"""
    logger.exception("Server error")
    return render_template('500.html'), 500

if __name__ == '__main__':
    # Create logs directory if it doesn't exist
    if not os.path.exists('logs'):
        os.makedirs('logs')
        
    # Initialize database on startup
    init_db()
    
    # Run the Flask app
    app.run(debug=True, host='0.0.0.0', port=5000)
