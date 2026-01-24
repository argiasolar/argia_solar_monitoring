import requests
from datetime import datetime

BASE = "https://server.growatt.com"

def login_server(session: requests.Session, username: str, password: str) -> None:
    session.get(f"{BASE}/login", timeout=20)
    r = session.post(f"{BASE}/login", data={"account": username, "password": password}, timeout=20)
    if "assToken" not in session.cookies.get_dict():
        raise RuntimeError("Login failed: assToken cookie missing")

def seed_context(session: requests.Session) -> None:
    # Nie zawsze wymagane, ale często stabilizuje sesję/UI-endpointy
    session.get(f"{BASE}/index", timeout=20)

def fetch_env_history_page(
    session: requests.Session,
    datalog_sn: str,
    addr: int,
    start_date: str,  # "YYYY-MM-DD"
    end_date: str,    # "YYYY-MM-DD"
    start: int = 0
) -> dict:
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{BASE}/index",
        "Origin": BASE,
    }
    data = {
        "datalogSn": datalog_sn,
        "addr": str(addr),
        "startDate": start_date,
        "endDate": end_date,
        "start": str(start),
    }
    r = session.post(f"{BASE}/device/getEnvHistory", data=data, headers=headers, timeout=30)

    # bywa, że server daje text/html mimo JSON
    try:
        return r.json()
    except Exception:
        raise RuntimeError(f"Non-JSON response: HTTP {r.status_code} body={r.text[:200]}")

def parse_env_points(resp: dict):
    """
    Zwraca listę punktów: timestamp + radiant W/m2 + tempy itd.
    """
    if resp.get("result") != 1:
        return []

    obj = resp.get("obj", {}) or {}
    datas = obj.get("datas", []) or []

    out = []
    for d in datas:
        cal = d.get("calendar", {}) or {}
        # month is 0-based in response
        ts = datetime(
            cal.get("year", 1970),
            cal.get("month", 0) + 1,
            cal.get("dayOfMonth", 1),
            cal.get("hourOfDay", 0),
            cal.get("minute", 0),
            cal.get("second", 0),
        )
        out.append({
            "ts": ts.isoformat(sep=" "),
            "datalogSn": d.get("dataLogSn") or d.get("datalogSn") or None,
            "addr": d.get("addr"),
            "radiant_wm2": d.get("radiant"),          # <-- irradiance
            "envTemp_c": d.get("envTemp"),
            "panelTemp_c": d.get("panelTemp"),
            "humidity_pct": d.get("envHumidity"),
            "windSpeed": d.get("windSpeed"),
            "windAngle": d.get("windAngle"),
        })
    return out, bool(obj.get("haveNext")), int(obj.get("start", 0))

def fetch_env_history_day(session, datalog_sn: str, addr: int, day: str, page_step: int = 80):
    """
    Pobiera wszystkie strony dla jednego dnia.
    page_step: u Ciebie widziałem start=80 w kolejnym page, więc default 80.
               Jeśli okaże się, że UI idzie co 20, zmień na 20.
    """
    all_points = []
    start = 0
    while True:
        resp = fetch_env_history_page(session, datalog_sn, addr, day, day, start=start)
        points, have_next, server_start = parse_env_points(resp)
        all_points.extend(points)

        if not have_next:
            break

        # server_start czasem zwraca "start" już przestawiony, ale bezpieczniej inkrementować
        start = start + page_step

        # guard: jeśli server zwraca to samo i utknęliśmy
        if start > 5000:
            break

    return all_points

if __name__ == "__main__":
    USER = "YOUR_LOGIN"
    PASS = "YOUR_PASS"

    datalog_sn = "DYD0E8501G"
    addr = 1
    day = "2026-01-24"

    s = requests.Session()
    login_server(s, USER, PASS)
    seed_context(s)

    pts = fetch_env_history_day(s, datalog_sn, addr, day, page_step=80)
    print(f"Fetched points: {len(pts)}")
    if pts:
        print("Sample:", pts[0])
