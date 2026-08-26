"""CFE tariff-page probe — feasibility test for automated monthly tariff fetch.

CFE (app.cfe.mx) sits behind an Incapsula WAF that 403s plain HTTP clients
(verified from pio06, 2026-08-26). This probe uses a real browser engine
(Playwright Chromium) to test whether a GitHub Actions runner can pass the
JS challenge, and captures the page DOM + a screenshot so the full scraper
can be written against the real structure.

Run in CI:  python scripts/cfe_probe.py  (artifacts land in cfe_probe_out/)
"""
from __future__ import annotations

import json
import os
import sys

URL = ('https://app.cfe.mx/Aplicaciones/CCFE/Tarifas/TarifasCRENegocio/'
       'Tarifas/GranDemandaMTH.aspx')
OUT = 'cfe_probe_out'


def main() -> int:
    from playwright.sync_api import sync_playwright

    os.makedirs(OUT, exist_ok=True)
    result = {'url': URL, 'ok': False}
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(
            locale='es-MX',
            user_agent=('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                        'AppleWebKit/537.36 (KHTML, like Gecko) '
                        'Chrome/151.0.0.0 Safari/537.36'))
        page.goto(URL, wait_until='domcontentloaded', timeout=60000)
        page.wait_for_timeout(12000)          # let the Incapsula JS challenge settle
        # a second navigation often lands the real page once the WAF cookie exists
        page.goto(URL, wait_until='networkidle', timeout=60000)
        page.wait_for_timeout(3000)
        html = page.content()
        result['title'] = page.title()
        result['bytes'] = len(html)
        result['blocked'] = 'Incapsula' in html or '_Incapsula_Resource' in html
        result['has_selects'] = html.count('<select')
        with open(f'{OUT}/page.html', 'w', encoding='utf-8') as fh:
            fh.write(html)
        page.screenshot(path=f'{OUT}/page.png', full_page=True)
        # enumerate form controls for the future scraper
        controls = page.eval_on_selector_all(
            'select, input[type=submit], input[type=button]',
            'els => els.map(e => ({tag: e.tagName, id: e.id, name: e.name, '
            'options: e.tagName === "SELECT" ? '
            'Array.from(e.options).slice(0, 8).map(o => o.text.trim()) : null}))')
        result['controls'] = controls
        result['ok'] = not result['blocked'] and result['has_selects'] > 0
        browser.close()
    with open(f'{OUT}/result.json', 'w', encoding='utf-8') as fh:
        json.dump(result, fh, indent=1, ensure_ascii=False)
    print(json.dumps({k: v for k, v in result.items() if k != 'controls'}, indent=1))
    print('PROBE', 'PASSED — full scraper is feasible from CI' if result['ok']
          else 'BLOCKED — fetch must run from a Mexican machine (laptop/Pi)')
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
