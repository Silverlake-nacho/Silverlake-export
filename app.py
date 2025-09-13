from flask import Flask, request, render_template, send_file, redirect, url_for, session, jsonify
import pandas as pd
from io import BytesIO
from datetime import datetime, timedelta
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from google.oauth2 import service_account
from googleapiclient.discovery import build
import base64

import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication

import sqlite3
import os
import json
DB_PATH = os.path.join('/var/data', 'carweb_cache.db')

# Initialize cache table
conn = sqlite3.connect(DB_PATH)
c = conn.cursor()
c.execute("""
CREATE TABLE IF NOT EXISTS carweb_cache (
  reg TEXT PRIMARY KEY,
  model TEXT,
  year INTEGER,
  engine_code TEXT,
  manufacturer TEXT,
  model_series TEXT,
  power_BHP INTEGER,
  engine_capacity INTEGER
)
""")
# Initialize search history table
c.execute("""
CREATE TABLE IF NOT EXISTS user_lookups (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT,
  reg TEXT,
  model TEXT,
  year INTEGER,
  engine_code TEXT,
  manufacturer TEXT,
  model_series TEXT,
  power_BHP INTEGER,
  timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
  engine_capacity INTEGER
)
""")
conn.commit()
conn.close()

# Create table to store user carts
conn = sqlite3.connect(DB_PATH)
c = conn.cursor()
c.execute("""
CREATE TABLE IF NOT EXISTS user_carts (
    username TEXT PRIMARY KEY,
    cart_json TEXT
)
""")
conn.commit()
conn.close()

# Create table to store order history
conn = sqlite3.connect(DB_PATH)
c = conn.cursor()
c.execute("""
CREATE TABLE IF NOT EXISTS order_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT,
    cart_json TEXT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
)
""")
conn.commit()
conn.close()

import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup
from flask import request, render_template_string
from collections import defaultdict

def rgb_to_hex(rgb):
    r = int(rgb.get('red', 1) * 255)
    g = int(rgb.get('green', 1) * 255)
    b = int(rgb.get('blue', 1) * 255)
    return '#{:02X}{:02X}{:02X}'.format(r, g, b)

def get_cached_lookup(reg):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT model, year, engine_code, manufacturer, model_series, power_BHP, engine_capacity FROM carweb_cache WHERE reg = ?", (reg,))
    row = c.fetchone()
    conn.close()
    if row:
        return {
            'model': row[0],
            'year': row[1],
            'engine_code': row[2],
            'manufacturer': row[3],
            'model_series': row[4],
            'power_BHP': row[5],
            'engine_capacity': row[6],
        }
    return None

def save_lookup_to_cache(reg, result):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT OR REPLACE INTO carweb_cache (reg, model, year, engine_code, manufacturer, model_series, power_BHP, engine_capacity)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            reg,
            result['model'],
            result['year'],
            result['engine_code'],
            result['manufacturer'],
            result['model_series'],
            result['power_BHP'],
            result['engine_capacity'],
        ))
    conn.commit()
    conn.close()

def log_user_lookup(username, reg, vehicle_data):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO user_lookups (username, reg, model, year, engine_code, manufacturer, model_series, power_BHP, engine_capacity)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        username,
        reg,
        vehicle_data.get('model', ''),
        vehicle_data.get('year', ''),
        vehicle_data.get('engine_code', ''),
        vehicle_data.get('manufacturer', ''),
        vehicle_data.get('model_series', ''),
        vehicle_data.get('power_BHP', ''),
        vehicle_data.get('engine_capacity', '')
    ))
    conn.commit()
    conn.close()

def get_matching_google_sheet_rows(engine_code):
    try:
        SCOPES = ['https://www.googleapis.com/auth/spreadsheets.readonly']
        creds = service_account.Credentials.from_service_account_file('credentials.json', scopes=SCOPES)

        SPREADSHEET_ID = '1I8XweK0R8OsxvDN783zBdQLLZpG50U_gaifYDMNFZS4'
        RANGE = 'Sheet1'

        service = build('sheets', 'v4', credentials=creds)

        values_result = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID, range=RANGE).execute()
        values = values_result.get('values', [])

        format_result = service.spreadsheets().get(
            spreadsheetId=SPREADSHEET_ID,
            ranges=[RANGE],
            fields='sheets.data.rowData.values.effectiveFormat.backgroundColor'
        ).execute()

        row_data = format_result['sheets'][0]['data'][0]['rowData']

        headers = values[0]
        rows = []

        try:
            engine_code_col_index = headers.index("Engine Code")
        except ValueError:
            print("Engine Code column not found")
            return []

        for i, row in enumerate(values[1:], start=1):
            # Defensive: skip row if engine_code_col_index >= length
            engine_code_cell = row[engine_code_col_index] if len(row) > engine_code_col_index else ""

            if engine_code_cell.lower().startswith(engine_code.lower()):
                # Build row dict only if match
                row_dict = {}
                for j, cell in enumerate(row):
                    if j in (18, 19):  # Skip columns R and S if needed
                        continue
                    cell_text = cell
                    bg_color = row_data[i]['values'][j].get('effectiveFormat', {}).get('backgroundColor', {})
                    hex_color = rgb_to_hex(bg_color)
                    key = headers[j]
                    row_dict[key] = {'value': cell_text, 'bg': hex_color}

                rows.append(row_dict)         

        return rows

    except Exception as e:
        print("Error accessing Google Sheets:", e)
        return []

file_path = 'WebFleet.csv'
df = pd.read_csv(file_path)

app = Flask(__name__)
app.secret_key = 'your_super_secret_key_here'

USERS = {
    'admin': 'Silverlake1!',
    'asm': 'ASMSilverlake1!',
    'marcio': 'Silverlake1!',
    'nacho': 'Silverlake1!'
}

last_search_result = None
search_details = None

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        session['username'] = username
        if username in USERS and USERS[username] == password:
            session['logged_in'] = True
            session['login_time'] = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
            return redirect(url_for('index'))
        else:
            error = 'Invalid Credentials. Please try again.'
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.before_request
def require_login():
    allowed_routes = ['login', 'static', 'autocomplete_model']
    if request.endpoint not in allowed_routes and not session.get('logged_in'):
        return redirect(url_for('login'))
      
    if session.get('logged_in'):
        login_time = session.get('login_time')
        if login_time:
            login_time = datetime.strptime(login_time, '%Y-%m-%d %H:%M:%S')
            if datetime.utcnow() - login_time > timedelta(hours=24):
                session.clear()
                return redirect(url_for('login'))

@app.route('/autocomplete_model', methods=['GET'])
def autocomplete_model():
    query = request.args.get('query', '')
    if query:
        filtered_models = df['Model'].dropna().unique()
        matches = [model for model in filtered_models if query.lower() in model.lower()]
        return {'models': matches}
    return {'models': []}

@app.route('/', methods=['GET', 'POST'])
def index():
    global last_search_result, search_details
    parts = None
    google_sheet_matches = []

    vehicle_info = {
        'model': '',
        'year': '',
        'engine_code': '',
    }    
    
    if request.method == 'POST':
        model = request.form['model']
        year = int(request.form['year'])
        engine_code = request.form.get('engine_code', '').strip()
        min_price = request.form.get('min_price')
        min_opportunity = request.form.get('min_opportunity')

        # Pick up vehicle info from hidden fields so we can show it after search
        vehicle_info = {
            'model': request.form.get('vehicle_info_model', ''),
            'year': request.form.get('vehicle_info_year', ''),
            'engine_code': request.form.get('vehicle_info_engine_code', ''),
            'manufacturer': request.form.get('vehicle_info_manufacturer', ''),
            'power_BHP': request.form.get('vehicle_info_power_BHP', ''),
            'model_series': request.form.get('vehicle_info_model_series', ''),
            'engine_capacity': request.form.get('vehicle_info_engine_capacity', ''),
            
        }      

        filtered = df[
            (df['Model'].str.lower() == model.lower()) &
            (df['IC Start Year'] <= year) &
            (df['IC End Year'] >= year)
        ]

         # if engine_code code

       # if engine_code:
         #   google_sheet_matches = get_matching_google_sheet_rows(engine_code)

        # Google Sheet search: fallback logic
        google_sheet_matches = []
        first_code, second_code = '', ''

        if engine_code:
            if '(' in engine_code and ')' in engine_code:
                first_code = engine_code.split('(')[0].strip()
                second_code = engine_code.split('(')[1].split(')')[0].strip()
            else:
                first_code = engine_code

            if first_code:
                google_sheet_matches = get_matching_google_sheet_rows(first_code)

            if not google_sheet_matches and second_code:
                google_sheet_matches = get_matching_google_sheet_rows(second_code)

    return render_template('index.html', parts=parts, search_details=search_details,
                           google_sheet_matches=google_sheet_matches, vehicle_info=vehicle_info, username=session.get('username'))
    
@app.route('/download')
def download():
    global last_search_result
    if last_search_result is not None:
        output = BytesIO()
        with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
            last_search_result.to_excel(writer, index=False, sheet_name='Parts')
        output.seek(0)
        return send_file(output, download_name="parts_opportunity.xlsx", as_attachment=True)
    return "No data to download", 400

from flask import Flask, request, jsonify
import requests
import xml.etree.ElementTree as ET

@app.route('/lookup_registration_carweb')
def lookup_registration_carweb():
    reg = request.args.get('reg', '').strip().upper()
    if not reg:
        return {'error': 'No registration provided'}, 400

    # 1️⃣ Check cache first
    cached = get_cached_lookup(reg)
    if cached:
        print(f"Using cached Carweb data for {reg}")
        if 'logged_in' in session and session.get('logged_in'):
            log_user_lookup(session.get('username', 'unknown'), reg, cached)
        return jsonify(cached)

    
    
   # {"UserName":"Silverlake",
   #  "Version":"0.31.1",
   #  "ServiceURL":"https://www1.carwebuk.com/CarweBVrrB2Bproxy/carwebVrrWebService.asmx",
   # "ClientDescription":"Silverlake Carbuyer",
   # "ClientRef":"Silverlake",
   # "Key":"lp22020411nM","Password":"Mn0929ap"}
    # 2️⃣ Make Carweb API call only if not cached
    try:
        username = 'Silverlake'
        password = 'Mn0929ap'
        validator_key = 'lp22020411nM'
        client_ref = 'Silverlake'
        client_desc = 'Silverlake Carbuyer'
        version = '0.31.1'

        # Prepare Carweb request
        params = {
            'strUserName': username,
            'strPassword': password,
            'strKey1': validator_key,
            'strClientRef': client_ref,
            'strClientDescription': client_desc,
            'strVRM': reg,
            'strVersion': version,
        }

        url = "https://ws.carwebuk.com/CarweBVRRB2Bproxy/carwebvrrwebservice.asmx/strB2BGetVehicleByVRM"
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()

        print("Carweb raw response:")
        print(response.text)
       
        # Parse XML robustly
        root = ET.fromstring(response.text)

        def find_text(root, tag):
            """Find first occurrence of tag ignoring namespace."""
            for elem in root.iter():
                if elem.tag.endswith(tag):
                    return elem.text.strip() if elem.text else ''
            return ''

        combined_make = find_text(root, 'Combined_Make')
        combined_model = find_text(root, 'Combined_Model')
        engine_code = find_text(root, 'EngineModelCode')
        model_year = find_text(root, 'DVLAYearOfManufacture')
        model_series = find_text(root, 'ModelSeries')
        power_BHP = find_text(root, 'MaximumPowerBHP')
        engine_capacity = find_text(root, 'Combined_EngineCapacity')

        result = {
            'model': combined_model,
            'year': int(model_year) if model_year.isdigit() else '',
            'engine_code': engine_code,
            'manufacturer': combined_make,
            'model_series': model_series,
            'power_BHP': int(float(power_BHP)) if power_BHP.replace('.', '', 1).isdigit() else '',
            'engine_capacity': int(float(engine_capacity)) if engine_capacity.replace('.', '', 1).isdigit() else '',
        }
        # 3️⃣ Save the result in the cache
        save_lookup_to_cache(reg, result)

        if 'logged_in' in session and session.get('logged_in'):
            log_user_lookup(session.get('username', 'unknown'), reg, result)
        
        return jsonify(result)

    except requests.RequestException as e:
        print(f"Carweb API request failed: {e}")
        return {'error': 'Failed to connect to Carweb API.'}, 502
    except ET.ParseError as e:
        print(f"XML parsing error: {e}")
        return {'error': 'Failed to parse Carweb API response.'}, 500
      
@app.route('/search_history')
def search_history():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    username = session.get('username', 'unknown')
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT reg, model, year, engine_code, manufacturer, model_series, power_BHP, timestamp
        FROM user_lookups
        WHERE username=?
        ORDER BY timestamp DESC
    """, (username,))
    history = c.fetchall()
    conn.close()

    return render_template('search_history.html', history=history, username=username)

@app.route('/order_history')
def order_history():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    username = session.get('username', 'unknown')
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, cart_json, timestamp FROM order_history WHERE username = ? ORDER BY timestamp DESC", (username,))
    rows = c.fetchall()
    conn.close()
    orders = []
    for order_id, cart_json, ts in rows:
        orders.append({'id': order_id, 'timestamp': ts, 'items': json.loads(cart_json)})
    return render_template('order_history.html', orders=orders, username=username)

@app.route('/send_cart_email', methods=['POST'])
def send_cart_email():
    if not session.get('logged_in'):
        return jsonify({'error': 'Not logged in'}), 401

    data = request.get_json()
    cart = data.get('cart', [])

    if not cart:
        return jsonify({'error': 'Cart is empty'}), 400

    username = session.get('username', 'unknown')
    subject = f"Parts Request from {username}"
    body = "Attached is the list of requested parts."

    # Create Excel attachment from cart
    df_cart = pd.DataFrame(cart)
    output = BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df_cart.to_excel(writer, index=False, sheet_name='Parts')
    output.seek(0)

    # Email configuration (adjust as needed)
    sender_email = "nacho@silverlake.co.uk"
    recipient_email = "nacho@silverlake.co.uk"
    smtp_server = "smtp.office365.com"
    smtp_port = 587
    smtp_user = "nacho@silverlake.co.uk"
    smtp_password = "!Cab34783"

    msg = MIMEMultipart()
    msg['From'] = sender_email
    msg['To'] = recipient_email
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'plain'))
    attachment = MIMEApplication(output.read(), _subtype='xlsx')
    attachment.add_header('Content-Disposition', 'attachment', filename='cart.xlsx')
    msg.attach(attachment)
  
    try:
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
          
        # Save order to history
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT INTO order_history (username, cart_json) VALUES (?, ?)", (username, json.dumps(cart)))
        conn.commit()
        conn.close()
      
        return jsonify({'message': 'Request sent successfully!'})
    except Exception as e:
        print("Email error:", e)
        return jsonify({'error': 'Failed to send email'}), 500
      
@app.route('/save_cart', methods=['POST'])
def save_cart():
    if not session.get('logged_in'):
        return {'error': 'Not logged in'}, 401

    data = request.get_json()
    username = session.get('username')
    cart_json = json.dumps(data.get('cart', []))

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO user_carts (username, cart_json)
        VALUES (?, ?)
        ON CONFLICT(username) DO UPDATE SET cart_json=excluded.cart_json
    """, (username, cart_json))
    conn.commit()
    conn.close()

    return {'message': 'Cart saved successfully'}

@app.route('/load_cart')
def load_cart():
    if not session.get('logged_in'):
        return {'error': 'Not logged in'}, 401

    username = session.get('username')
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT cart_json FROM user_carts WHERE username = ?", (username,))
    row = c.fetchone()
    conn.close()

    if row:
        return {'cart': json.loads(row[0])}
    else:
        return {'cart': []}


#Pinnacle Vin Decode
@app.route('/lookup_registration')
def lookup_registration():
    reg = request.args.get('reg', '').strip().upper()
    if not reg:
        return {'error': 'No registration provided'}, 400

    try:
        # Provide your own credentials here
        username = 'silverlake_web'
        password = 'SilverlakeSO322HL'
        credentials = f"{username}:{password}"
        auth_header = base64.b64encode(credentials.encode()).decode()

        url = f"http://api.carparts-uk.com/ops/v1/decode/vrm/{reg}"
        headers = {
            'Authorization': f'Basic {auth_header}',
            'Accept': 'application/json'
        }

        response = requests.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()

        # Convert list of dicts into key-value pairs
        vehicle_info = {item['key']: item['value'] for item in data}

        return {
            'Manufacturer': vehicle_info.get('MAKE', ''),
            'model': vehicle_info.get('MODEL', ''),
            'Trim Level': vehicle_info.get('TRIM_LEVEL', ''),
            'year': int(vehicle_info.get('MODEL_YEAR', 0) or vehicle_info.get('FIRST_REGISTERED', '0')[:4]),
            'engine_code': vehicle_info.get('GetVehicles.DataArea.Vehicles.Vehicle.EngineModelCode', ''),
            'Engine Size': int(vehicle_info.get('ENGINE_SIZE', 0)),
            'Doors': int(vehicle_info.get('DOORS', 0)),
            'Body Style': vehicle_info.get('BODY_STYLE', ''),
            'Fuel Type': vehicle_info.get('FUEL_TYPE', '')
        }

    except Exception as e:
        print(f"VIN Decode API error: {e}")
        return {'error': 'Failed to fetch vehicle data.'}, 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0')
