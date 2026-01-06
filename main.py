import argparse
import subprocess
import sys
import os

def run_script(script_name):
    """Uruchamia podprojekt"""
    print(f"Uruchamiam {script_name}...")
    result = subprocess.run([sys.executable, script_name], capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Błąd w {script_name}:")
        print(result.stderr)
    else:
        print(result.stdout)

def main():
    parser = argparse.ArgumentParser(description="Argia Solar Monitoring – launcher")
    parser.add_argument(
        '--inverter',
        choices=['all', 'growatt', 'solaredge', 'huawei'],
        default='all',
        help='Który inwerter uruchomić (domyślnie wszystkie)'
    )
    args = parser.parse_args()

    if args.inverter in ['all', 'growatt']:
        if os.path.exists('main_growatt.py'):
            run_script('main_growatt.py')
        else:
            print("Plik main_growatt.py nie istnieje!")

    if args.inverter in ['all', 'solaredge']:
        if os.path.exists('main_solaredge.py'):
            run_script('main_solaredge.py')
        else:
            print("Plik main_solaredge.py jeszcze nie istnieje – pomiń.")

    if args.inverter in ['all', 'huawei']:
        if os.path.exists('main_huawei.py'):
            run_script('main_huawei.py')
        else:
            print("Plik main_huawei.py jeszcze nie istnieje – pomiń.")

if __name__ == '__main__':
    main()
