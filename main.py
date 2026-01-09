import argparse
import subprocess
import sys
import os

def run_script(script_name):
    """Uruchamia podprojekt i wyświetla jego wynik"""
    if not os.path.exists(script_name):
        print(f"Plik {script_name} nie istnieje – pomijam.")
        return
    
    print(f"\n=== Uruchamiam: {script_name} ===")
    result = subprocess.run(
        [sys.executable, script_name],
        capture_output=True,
        text=True
    )
    
    if result.returncode != 0:
        print(f"Błąd w {script_name}:")
        print(result.stderr)
    else:
        print(result.stdout)
    
    print(f"=== Koniec: {script_name} ===\n")

def main():
    parser = argparse.ArgumentParser(description="Argia Solar Monitoring – launcher wszystkich modułów")
    parser.add_argument(
        '--inverter',
        choices=['all', 'growatt', 'huawei', 'solaredge', 'weather'],
        default='all',
        help='Który moduł uruchomić (domyślnie wszystkie)'
    )
    args = parser.parse_args()

    print(f"Uruchamianie modułów: {args.inverter.upper() if args.inverter != 'all' else 'WSZYSTKIE'}")

    if args.inverter in ['all', 'growatt']:
        run_script('main_growatt.py')

    if args.inverter in ['all', 'huawei']:
        run_script('main_huawei.py')

    if args.inverter in ['all', 'solaredge']:
        run_script('main_solaredge.py')

    if args.inverter in ['all', 'weather']:
        run_script('main_weather.py')

    print("=== Wszystkie wybrane moduły zakończone ===")

if __name__ == '__main__':
    main()
