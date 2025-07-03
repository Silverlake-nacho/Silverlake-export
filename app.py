from flask import Flask, request, render_template, send_file, redirect, url_for, session, jsonify
import pandas as pd
from io import BytesIO
from datetime import datetime, timedelta
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from google.oauth2 import service_account
from googleapiclient.discovery import build
import base64

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

        for i, row in enumerate(values[1:], start=1):
            row_dict = {}
            for j, cell in enumerate(row):
                if j in (18, 19):  # Skip columns R and S
                    continue
                cell_text = cell
                bg_color = row_data[i]['values'][j].get('effectiveFormat', {}).get('backgroundColor', {})
                hex_color = rgb_to_hex(bg_color)
                key = headers[j]
                row_dict[key] = {'value': cell_text, 'bg': hex_color}
            if any(engine_code.lower() in str(c).lower() for c in row):
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
    if request.method == 'POST':
        model = request.form['model']
        year = int(request.form['year'])
        engine_code = request.form.get('engine_code', '').strip()
        min_price = request.form.get('min_price')
        min_opportunity = request.form.get('min_opportunity')

        filtered = df[
            (df['Model'].str.lower() == model.lower()) &
            (df['IC Start Year'] <= year) &
            (df['IC End Year'] >= year)
        ]

        if engine_code:
            def custom_filter(row):
                description = str(row['IC Description'])
                if 'engine code' in description.lower():
                    return engine_code.lower() in description.lower()
                return True

            filtered = filtered[filtered.apply(custom_filter, axis=1)]

        if not filtered.empty:
            filtered['Potential_Profit'] = (filtered['Backorders'] + filtered['Not Found 180 days']) * filtered['B Price']
            filtered['Sales_Speed'] = filtered['Parts Sold All'] / (filtered['Parts in Stock'] + 1)
            filtered['Opportunity_Score'] = filtered['Potential_Profit'] * filtered['Sales_Speed']

            if min_price:
                filtered = filtered[filtered['B Price'] >= float(min_price)]
            if min_opportunity:
                filtered = filtered[filtered['Opportunity_Score'] >= float(min_opportunity)]

            parts = filtered[['Part', 'IC Start Year', 'IC End Year', 'IC Description', 'B Price', 'Parts in Stock', 'Backorders',
                              'Parts Sold All', 'Not Found 180 days', 'Potential_Profit', 'Sales_Speed', 'Opportunity_Score']]
            parts = parts.sort_values(by=['Backorders', 'Opportunity_Score'], ascending=False).head(50)
            last_search_result = parts
            search_details = {'model': model, 'year': year, 'engine_code': engine_code}
            parts = parts.to_dict('records')

        if engine_code:
            google_sheet_matches = get_matching_google_sheet_rows(engine_code)

    return render_template('index.html', parts=parts, search_details=search_details, google_sheet_matches=google_sheet_matches)

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
   # {"UserName":"Silverlake",
   #  "Version":"0.31.1",
   #  "ServiceURL":"https://www1.carwebuk.com/CarweBVrrB2Bproxy/carwebVrrWebService.asmx",
   # "ClientDescription":"Silverlake Carbuyer",
   # "ClientRef":"Silverlake",
   # "Key":"lp22020411nM","Password":"Mn0929ap"}
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

        # Parse XML
        root = ET.fromstring(response.text)

        # Example: extract Combined_Make and Combined_Model
        combined_make = root.find('.//Combined_Make')
        combined_model = root.find('.//Combined_Model')
        engine_code = root.find('.//EngineModelCode')
        if engine_code is not None and engine_code.text:
            # Extract everything before the first space or first '('
            raw_engine_code = engine_code.text.strip()
            first_part = raw_engine_code.split()[0].split('(')[0].strip()
        else:
            first_part = ''
        model_year = root.find('.//DVLAYearOfManufacture')
        model_series = root.find('.//ModelSeries')
        power_BHP = root.find('.//MaximumPowerBHP')

        result = {
            'model': combined_model.text if combined_model is not None else '',
            'year': int(model_year.text) if model_year is not None and model_year.text.isdigit() else '',
            'engine_code': first_part,
            'manufacturer': combined_make.text if combined_make is not None else '',
            'model_series': model_series.text if model_series is not None else '',
            'power_BHP': int(power_BHP.text) if power_BHP is not None and power_BHP.text.isdigit() else '',
        }
        return jsonify(result)

    except requests.RequestException as e:
        print(f"Carweb API request failed: {e}")
        return {'error': 'Failed to connect to Carweb API.'}, 502
    except ET.ParseError as e:
        print(f"XML parsing error: {e}")
        return {'error': 'Failed to parse Carweb API response.'}, 500


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
