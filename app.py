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
        }      

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

       # if engine_code:
         #   google_sheet_matches = get_matching_google_sheet_rows(engine_code)

        # Google Sheet search: fallback logic
        google_sheet_matches = []

        if engine_code:
            first_code, second_code = '', ''
        if '(' in engine_code and ')' in engine_code:
            first_code = engine_code.split('(')[0].strip()
            second_code = engine_code.split('(')[1].split(')')[0].strip()
        else:
            first_code = engine_code

        # First try with first code
        if first_code:
            google_sheet_matches = get_matching_google_sheet_rows(first_code)

        # If no matches found, try second code
        if not google_sheet_matches and second_code:
            google_sheet_matches = get_matching_google_sheet_rows(second_code)

    return render_template('index.html', parts=parts, search_details=search_details,
                           google_sheet_matches=google_sheet_matches, vehicle_info=vehicle_info)
    
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

        result = {
            'model': combined_model,
            'year': int(model_year) if model_year.isdigit() else '',
            'engine_code': engine_code,
            'manufacturer': combined_make,
            'model_series': model_series,
            'power_BHP': int(float(power_BHP)) if power_BHP.replace('.', '', 1).isdigit() else '',
        }
        return jsonify(result)

    except requests.RequestException as e:
        print(f"Carweb API request failed: {e}")
        return {'error': 'Failed to connect to Carweb API.'}, 502
    except ET.ParseError as e:
        print(f"XML parsing error: {e}")
        return {'error': 'Failed to parse Carweb API response.'}, 500

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
