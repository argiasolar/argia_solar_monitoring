import os
import json
from datetime import datetime
from googleapiclient.discovery import build
from google.oauth2 import service_account
from googleapiclient.errors import HttpError
import pandas as pd
import argia_SMA as sma

# Load Google Sheets credentials (file path or JSON content from env)
credentials_json = os.getenv('GOOGLE_CREDENTIALS', 'credentials.json')
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']

if os.path.exists(credentials_json):
    credentials = service_account.Credentials.from_service_account_file(
        credentials_json, scopes=SCOPES
    )
else:
    credentials_info = json.loads(credentials_json)
    credentials = service_account.Credentials.from_service_account_info(
        credentials_info, scopes=SCOPES
    )

service = build('sheets', 'v4', credentials=credentials)

# Google Sheet ID from env or default (set your own)
sheet_id = os.getenv('GOOGLE_SHEET_ID')

# Read config from 'Config' tab (assume A:B key-value pairs)
def read_config():
    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range='Config!A:B'
    ).execute()
    values = result.get('values', [])
    config = {row[0]: row[1] for row in values if len(row) > 1}
    return config, values  # return values for row indexing

# Update a config value in sheet
def update_config_key(key, value, config_values):
    try:
        row_index = next(i for i, row in enumerate(config_values) if len(row) > 0 and row[0] == key)
        cell_range = f'Config!B{row_index + 1}'
    except StopIteration:
        # Append if not found
        body = {'values': [[key, value]]}
        service.spreadsheets().values().append(
            spreadsheetId=sheet_id, range='Config!A1', valueInputOption='RAW', body=body
        ).execute()
        return

    body = {'values': [[value]]}
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id, range=cell_range, valueInputOption='RAW', body=body
    ).execute()

# Main logic
config, config_values = read_config()

environment = config.get('environment', 'sandbox')
client_id = config['client_id']
client_secret = config['client_secret']
login_hint = config['login_hint']

if environment == 'sandbox':
    base_url = 'https://sandbox.smaapis.de'
    auth_base = 'https://sandbox-auth.smaapis.de/oauth2'
    bc_base = 'https://sandbox.smaapis.de/oauth2/v2'
else:
    base_url = 'https://api.smaapis.de'
    auth_base = 'https://auth.smaapis.de/oauth2'
    bc_base = 'https://async-auth.smaapis.de/oauth2/v2'

token_url = f'{auth_base}/token'

# Get client access token (short-lived, fetch fresh each time)
client_token = sma.get_client_token(client_id, client_secret, token_url)

# Ensure consent (initiates if needed)
try:
    plants = sma.get_plants(client_token, base_url)
except Exception as e:
    if '403' in str(e) or 'unauthorized' in str(e).lower():  # Assume no consent error
        sma.ensure_consent(environment, login_hint, client_token, bc_base)
        plants = sma.get_plants(client_token, base_url)
    else:
        raise

# Get plant_oid (use first if not in config)
if 'plant_oid' not in config:
    if plants:
        plant_oid = plants[0]['oid']  # Assume structure [{'oid': '...'}]
        update_config_key('plant_oid', plant_oid, config_values)
    else:
        raise Exception('No plants available after consent')
else:
    plant_oid = config['plant_oid']

# Fetch today's measurements (example: AC power, 5-min resolution)
now = datetime.utcnow()
start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat() + 'Z'
end = now.isoformat() + 'Z'

data = sma.get_measurements(client_token, base_url, plant_oid, start, end)

# Process to DataFrame (assume data format: {'measurements': [{'channel': 'Pac', 'values': [{'time': iso, 'value': num}]}]})
if data and 'measurements' in data and data['measurements']:
    times = [v['time'] for v in data['measurements'][0]['values']]
    values = [v['value'] for v in data['measurements'][0]['values']]
    df = pd.DataFrame({'timestamp': times, 'power_ac': values})
else:
    df = pd.DataFrame()  # Empty if no data

# Write to 'Sandbox' tab (clear and write)
try:
    service.spreadsheets().values().clear(
        spreadsheetId=sheet_id, range='Sandbox!A:Z'
    ).execute()
except HttpError:
    pass  # Tab may not exist yet

values_to_write = [df.columns.tolist()] + df.values.tolist()
body = {'values': values_to_write}
service.spreadsheets().values().update(
    spreadsheetId=sheet_id, range='Sandbox!A1', valueInputOption='RAW', body=body
).execute()

print('Data fetched and written to Sandbox tab.')
