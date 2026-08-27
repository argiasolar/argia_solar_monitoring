import requests
import time
from urllib.parse import quote

def get_client_token(client_id, client_secret, token_url):
    data = {
        'grant_type': 'client_credentials',
        'client_id': client_id,
        'client_secret': client_secret
    }
    resp = requests.post(token_url, data=data)
    if resp.status_code == 200:
        return resp.json()['access_token']
    else:
        raise Exception(f'Failed to get client token: {resp.status_code} - {resp.text}')

def ensure_consent(environment, login_hint, client_token, bc_base):
    headers = {
        'Authorization': f'Bearer {client_token}',
        'Content-Type': 'application/json'
    }
    data = {"loginHint": login_hint}

    # Initiate back-channel request
    resp = requests.post(f'{bc_base}/bc-authorize', headers=headers, json=data)
    if resp.status_code not in (201, 200):  # 201 Created or OK if already exists
        raise Exception(f'Back-channel initiation failed: {resp.status_code} - {resp.text}')

    if environment == 'sandbox':
        # Simulate consent in sandbox
        put_headers = {'Authorization': f'Bearer {client_token}', 'Content-Type': 'application/json'}
        resp = requests.put(f'{bc_base}/bc-authorize/{quote(login_hint)}/status', headers=put_headers, data='"accepted"')
        if resp.status_code != 200:
            raise Exception(f'Sandbox consent simulation failed: {resp.status_code} - {resp.text}')
    else:
        # Poll for consent in production
        interval = 5  # Poll every 5s (override doc's 1800 for script)
        max_polls = 120  # ~10 min timeout
        for _ in range(max_polls):
            resp = requests.get(f'{bc_base}/bc-authorize/{quote(login_hint)}', headers=headers)
            if resp.status_code == 200:
                status = resp.json()
                if status['state'] == 'accepted':
                    return
                elif status['state'] in ['rejected', 'expired', 'revoked']:
                    raise Exception(f'Consent status: {status["state"]}')
            time.sleep(interval)
        raise Exception('Consent timeout. Please check your email and approve the consent link from SMA.')

def get_plants(client_token, base_url):
    url = f'{base_url}/plant'
    headers = {'Authorization': f'Bearer {client_token}'}
    resp = requests.get(url, headers=headers)
    if resp.status_code == 200:
        return resp.json()  # Assume list of plants [{'oid': '...', ...}]
    else:
        raise Exception(f'Failed to get plants: {resp.status_code} - {resp.text}')

def get_measurements(client_token, base_url, plant_oid, start_date, end_date, resolution='5m', channel='Pac'):
    url = f'{base_url}/plant/{plant_oid}/measurements'
    headers = {'Authorization': f'Bearer {client_token}'}
    params = {
        'start': start_date,
        'end': end_date,
        'resolution': resolution,
        'channel': channel
    }
    resp = requests.get(url, headers=headers, params=params)
    if resp.status_code == 200:
        return resp.json()
    else:
        raise Exception(f'Failed to get measurements: {resp.status_code} - {resp.text}')
