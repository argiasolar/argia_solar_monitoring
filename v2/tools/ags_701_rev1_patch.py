# -*- coding: utf-8 -*-
"""AGS-701 Rev 1 (2026-09-07): the operating monitor (Argia_Mont v2) is the
source of truth — the chapter now states what the fleet monitor actually
measures, the thresholds it alarms on, and what is NOT yet measured.
Slides 281-287 (1-based) in en / es / cs; quiz question 2 of AGS-701."""
import json, re, sys

SRC = "/tmp/ags.html"          # the live page, fetched with curl (knowledge.AGS_URL)
OUT = "/tmp/out/ARGIA_Golden_Standard_Designer_Training_WHITE.html"

def body(eyebrow, headline, inner):
    return ('\n              <div class="eyebrow">%s</div>\n              <h2 class="headline">%s</h2>\n'
            '              <div class="slide-body">%s</div>' % (eyebrow, headline, inner))

N = lambda x: '<span class="num">%s</span>' % x
S = '<strong>shall</strong>'

EN = {}
EN[281] = body("AGS-701 · Key Requirements", "Monitoring and performance evaluation (R1–R3)",
 '<p class="lead">Performance is judged <strong>weather-normalized</strong> — expected energy and PR_STC from the measured irradiance — never raw kWh against a sunny or cloudy year. Rev 1 (2026-09): the rules read as <strong>Argia_Mont</strong>, the operating fleet monitor, applies them.</p>'
 '<table class="tbl"><thead><tr><th>#</th><th>Rule</th></tr></thead><tbody>'
 '<tr><td>R1</td><td>A monitoring system ' + S + ' be installed per IEC 61724-1:2021 — <em>Class A</em> (heated, calibrated pyranometer) for bankable/larger systems, Class B otherwise. In operation the monitor collects <strong>inverter-level telemetry every 5 minutes</strong> from the vendor platform (Growatt, Huawei, SolarEdge), the site irradiance sensor where one is installed, and a <strong>satellite reference</strong> (Open-Meteo) as fallback and as a drift check on the site sensor (REVIEW when the measured/satellite ratio moves ' + N('&gt; 10 %') + ' against its 40-day baseline). A reconciled day needs completeness ' + N('≥ 95 %') + '.</td></tr>'
 '<tr><td>R2</td><td>Performance ' + S + ' be evaluated <strong>daily</strong> as PR and PR_STC (temperature-corrected to 25 °C; γ = ' + N('−0.35 %/°C') + ' until the module datasheet coefficient is configured per plant) against the plant\'s expected energy (kWp × in-plane irradiation × design PR), and <strong>monthly at the close</strong> against the contracted energy (PPA) — over ≥ 1 year for a bankable statement (IEC 61724-3). The AGS-601 commissioning baseline is the reference of the first operating year.</td></tr>'
 '<tr><td>R3</td><td>Underperformance investigation ' + S + ' trigger automatically: plant energy below ' + N('85 %') + ' of expected (WARNING) or ' + N('70 %') + ' (CRITICAL); specific yield below 85 / 70 % of the regional twin plant; an inverter below 85 / 70 % of its peers; a <em>new</em> string-diagnostic flag; inverter temperature ' + N('≥ 65 °C') + ' (WARNING) or ' + N('≥ 70 °C') + ' and ≥ 5 °C above its peers (CRITICAL, with the measured derating loss in kWh); no telemetry (dark) or stale &gt; 45 min in daylight. Root cause ' + S + ' be <em>found</em> — every alert carries the engine\'s explanation and the maintenance log records the action — never accepted.</td></tr>'
 '</tbody></table>')

EN[282] = body("AGS-701 · Key Requirements", "Maintenance, degradation, BESS, reporting (R4–R9)",
 '<p class="lead">Maintenance and tracking close on named targets — and the chapter says plainly what the monitor measures and what stays a site activity.</p>'
 '<table class="tbl"><thead><tr><th>#</th><th>Rule</th></tr></thead><tbody>'
 '<tr><td>R4</td><td>Preventive maintenance per IEC 62446-2: <strong>cleaning driven by the measured soiling ratio</strong> (the monitor rates cleaning NOT DUE / APPROACHING / DUE / OVERDUE from ≥ 7 days of PR history against the cleaning cost; PB-04 ' + N('3 %') + ' is the design allowance); annual IR thermography vs the AGS-601 baseline (IEC 62446-3), electrical re-test, torque/structural, vegetation/drainage — these four are <em>logged as maintenance events</em>, not measured by the monitor.</td></tr>'
 '<tr><td>R5</td><td>Corrective maintenance: acute alerts within <strong>30 minutes</strong> (07:00–19:30 MX) and a 07:07 daily digest; <strong>availability computed daily per inverter</strong> (share of daylight slots reporting) against the plant\'s SLA target (' + N('≈ 99 %') + ', PB-03, IEC 63019); MTTR from the maintenance log; AGS-401 strategic spares.</td></tr>'
 '<tr><td>R6</td><td>Degradation (IEC 61724-4) against the warranted ' + N('≤ 0.4 %/yr') + ' (PB-01) from the monthly PR_STC series — <em>available once two full operating years of PR_STC exist in the monitor; not yet a fleet KPI</em>. Exceedance raises an AGS-401 warranty claim.</td></tr>'
 '<tr><td>R7</td><td>BESS O&amp;M (SOH, cycle count vs warranty, AGS-301 augmentation, AGS-302 safety re-test) — <em>no BESS in the operating fleet today</em>; the rule applies when one is installed.</td></tr>'
 '<tr><td>R8</td><td>Warranty management: the monitor supplies the <strong>evidence</strong> — vendor fault codes, string flags, silent-inverter history, and the thermal-health record (peak temperature, hours ≥ 65 °C, ΔT vs peers, suspected derating in kWh and MXN).</td></tr>'
 '<tr><td>R9</td><td>Reporting: daily performance mail (PPA fleet), plant report pages and the <strong>monthly close on the portal</strong> (energy, expected, PR, PR_STC, availability, CO₂, invoice basis from reconciled counters), financial mail to owner/lender. KPI set: PR_STC, specific yield, availability, energy vs contracted — degradation when R6 becomes measurable.</td></tr>'
 '</tbody></table>')

EN[283] = body("AGS-701 · Methods", "The O&amp;M loop and the investigation sequence",
 '<p class="lead">The loop that closes the book: monitor, reconcile, evaluate on the design metric, compare to the contract, investigate, feed back.</p>'
 '<div class="flow"><div class="step"><strong>MONITOR</strong> — 5-min inverter telemetry + irradiance basis + satellite drift check</div>'
 '<div class="step">→ <strong>RECONCILE</strong> nightly against the vendor\'s own counters — PASS ' + N('≤ 1 %') + ' / REVIEW ' + N('≤ 3 %') + ' / FAIL daily; ' + N('0.5 / 1.5 %') + ' monthly; completeness ≥ 95 %; retried for 14 days when a counter returns</div>'
 '<div class="step">→ <strong>EVALUATE</strong> daily: energy vs expected, PR, PR_STC, availability, soiling ratio, thermal health</div>'
 '<div class="step">→ <strong>COMPARE</strong> monthly at the close: billable energy vs contracted (PPA) — closed months are frozen</div>'
 '<div class="step">→ <strong>ALERT &amp; INVESTIGATE</strong> at the R3 thresholds — the alert explains itself; a logged maintenance window silences a known-down plant without losing the trail</div>'
 '<div class="step">→ <strong>REPORT</strong> (daily mail, portal, monthly close, invoices) → feed model corrections back to AGS-202 / 207</div></div>'
 '<p><strong>Underperformance:</strong> weather-normalize → rank causes: soiling (ratio) → shading → string / inverter faults (flags, peer comparison, thermal derating) → degradation → availability / downtime → confirm on site by thermography / I-V vs the AGS-601 baseline → corrective action → verify PR_STC recovers.</p>'
 '<p><strong>Maintenance schedule</strong> (IEC 62446-2): cleaning (soiling-driven) · thermography (annual, vs baseline) · electrical re-test · torque/structural · vegetation/drainage · BESS checks + annual safety re-test where a BESS exists.</p>')

EN[284] = body("AGS-701 · Worked Example", "The 417 kWp San Luis Potosí project in operation",
 '<p class="lead">Every operating KPI checks against a reference set earlier in the book — and against what the fleet monitor computes every day.</p>'
 '<table class="tbl"><thead><tr><th>KPI / activity</th><th>Reference</th><th>Operating check (Argia_Mont)</th></tr></thead><tbody>'
 '<tr><td>Monitoring (R1)</td><td>IEC 61724-1:2021 Class A/B</td><td>5-min inverter telemetry; site sensor + satellite drift check; completeness ≥ 95 %</td></tr>'
 '<tr><td>Year-1 PR_STC (R2)</td><td>Model 0.87 (AGS-202) + commissioning baseline (AGS-601)</td><td>Daily PR and PR_STC vs expected energy; monthly close</td></tr>'
 '<tr><td>Energy vs contracted (R3)</td><td>629 MWh (95 % of 1-yr P90, AGS-207)</td><td>Billable kWh from reconciled counters vs contract at the close</td></tr>'
 '<tr><td>Underperformance (R3)</td><td>Thresholds of R3</td><td>Alerts: 85 / 70 % of expected, twin, peers, strings, thermal, dark/stale</td></tr>'
 '<tr><td>Soiling (R4)</td><td>PB-04 3 %</td><td>Soiling ratio → cleaning DUE / OVERDUE</td></tr>'
 '<tr><td>Thermography (R4)</td><td>AGS-601 baseline</td><td>Annual scan vs baseline — logged as a maintenance event</td></tr>'
 '<tr><td>Degradation (R6)</td><td>Warranted ≤ 0.4 %/yr (PB-01)</td><td>From the monthly PR_STC series after two operating years</td></tr>'
 '<tr><td>Availability (R5)</td><td>≈ 99 % (PB-03, IEC 63019)</td><td>Daily per inverter vs the plant SLA target; MTTR from the maintenance log</td></tr>'
 '<tr><td>BESS (R7)</td><td>SOH, cycles, augmentation (AGS-301)</td><td>n/a — no BESS in the operating fleet</td></tr>'
 '</tbody></table>')

EN[285] = body("AGS-701 · Worked Example / Acceptance", "A caught underperformance — and PASS / REVIEW / FAIL",
 '<p class="lead">Mid-year, operating PR_STC drops below expected. Weather-normalized (so it is not just a cloudy stretch), the investigation traces it to <strong>soiling above the 3 % model</strong>; cleaning restores PR_STC — the loop worked.</p>'
 '<table class="tbl"><thead><tr><th>Result</th><th>Condition</th></tr></thead><tbody>'
 '<tr><td><span class="badge pass">PASS</span></td><td>Monitoring class documented; daily PR / PR_STC vs expected and monthly energy vs contracted; alerts at the R3 thresholds with the root cause recorded; nightly reconciliation PASS; IEC 62446-2 PM scheduled incl. thermography vs baseline; availability vs the SLA target; degradation tracked once measurable; warranty evidence + reporting in place.</td></tr>'
 '<tr><td><span class="badge review">REVIEW</span></td><td>PR_STC ' + N('3–5 %') + ' below expected under investigation; soiling APPROACHING / DUE; reconciliation REVIEW (1–3 % daily); satellite drift REVIEW; Class B on a bankable system; degradation not yet measurable (&lt; 2 years).</td></tr>'
 '<tr><td><span class="badge fail">FAIL</span></td><td>Raw-kWh comparison (not weather-normalized); underperformance accepted without root cause; reconciliation FAIL or a month closed without reconciled counters; no thermography baseline use; degradation / availability untracked when the data exists; BESS safety re-test skipped where a BESS exists; no warranty / reporting.</td></tr>'
 '</tbody></table>')

EN[286] = body("AGS-701 · Common Errors", "What goes wrong in operation — and the Engine\'s watch",
 '<p class="lead">The classic failures all break the same rule: judging the plant on the wrong metric or against no reference.</p>'
 '<ul><li><strong>Comparing raw kWh</strong> — a cloudy month is not underperformance; normalize on expected energy and PR_STC (R2).</li>'
 '<li><strong>Trusting a portal without reconciliation</strong> — the vendor\'s own counter is the reference; a gap &gt; 3 % is a collection fault, not production (R2).</li>'
 '<li><strong>No commissioning baseline</strong> — without AGS-601 there is nothing to measure drift against (R2 / R4).</li>'
 '<li><strong>Accepting a low PR_STC</strong> — investigate to root cause: soiling, shading, faults, or real degradation (R3).</li>'
 '<li><strong>Paging on a flat temperature limit</strong> — inverter heat is judged against its peers and the site ambient; the loss is measured as derating kWh, not assumed from the number (R3).</li>'
 '<li><strong>Raw PR on a hot site</strong> — standard PR penalizes hot Mexican rooftops; benchmark on PR_STC (R2).</li>'
 '<li><strong>Degradation untracked vs warranty</strong> — actual above 0.4 %/yr is a claim protecting the bankability case (R6).</li>'
 '<li><strong>No owner/lender reporting</strong> — bankability is monitored continuously, not just at financing (R9).</li></ul>'
 '<p><strong>Engine:</strong> 🟢 monitoring class documented, PR_STC normalized, reconciliation PASS, alerts + reporting live · 🟡 any REVIEW — PR_STC 3–5 % below, reconciliation REVIEW, sensor drift, soiling DUE · 🔴 any FAIL — raw-kWh judgement, unexplained underperformance, reconciliation FAIL, untracked degradation when measurable.</p>')

EN[287] = body("AGS-701 · Chapter review", "Before the test: <em>remember</em> this.",
 '<ul class="review">'
 '<li>Install monitoring per IEC 61724-1:2021 — Class A (heated, calibrated pyranometer) for bankable/larger systems, Class B otherwise; in operation: 5-minute inverter telemetry, site sensor where installed, satellite reference with a 10 % drift check, completeness ≥ 95 %.</li>'
 '<li>Evaluate performance weather-normalized — daily PR and PR_STC vs expected energy, monthly at the close vs the contracted energy — never raw kWh.</li>'
 '<li>Reconcile every day against the vendor\'s own counters (PASS ≤ 1 %, REVIEW ≤ 3 %, FAIL; 0.5 / 1.5 % monthly) — a closed month is frozen.</li>'
 '<li>Investigate at the R3 thresholds: 85 / 70 % of expected, twin plant, peer inverters, new string flags, thermal derating, dark / stale data — find the root cause, never accept.</li>'
 '<li>Schedule IEC 62446-2 preventive maintenance: cleaning from the measured soiling ratio, annual thermography vs the AGS-601 baseline, electrical re-test, torque/structural, vegetation/drainage.</li>'
 '<li>Track availability daily per inverter vs the SLA target (≈ 99 %, PB-03) and degradation vs ≤ 0.4 %/yr (PB-01) once two operating years exist — exceedances become AGS-401 warranty claims backed by the monitor\'s evidence.</li>'
 '<li>Report the KPI set continuously (daily mail, portal, monthly close, invoices) and feed deviations back to AGS-202 / 207 — the standard checks its own predictions.</li></ul>')

ES = {}
D = '<strong>debe</strong>'
ES[281] = body("AGS-701 · Requisitos Clave", "Monitoreo y evaluación de desempeño (R1–R3)",
 '<p class="lead">El desempeño se juzga <strong>normalizado por clima</strong> — energía esperada y PR_STC a partir de la irradiancia medida — nunca kWh brutos contra un año soleado o nublado. Rev 1 (2026-09): las reglas se leen tal como las aplica <strong>Argia_Mont</strong>, el monitor operativo de la flota.</p>'
 '<table class="tbl"><thead><tr><th>#</th><th>Regla</th></tr></thead><tbody>'
 '<tr><td>R1</td><td>Un sistema de monitoreo ' + D + ' instalarse conforme a IEC 61724-1:2021 — <em>Clase A</em> (piranómetro calefactado y calibrado) para sistemas bancables/mayores, Clase B en los demás casos. En operación el monitor recoge <strong>telemetría por inversor cada 5 minutos</strong> de la plataforma del fabricante (Growatt, Huawei, SolarEdge), el sensor de irradiancia del sitio donde existe, y una <strong>referencia satelital</strong> (Open-Meteo) como respaldo y como control de deriva del sensor (REVIEW cuando la relación medido/satélite se mueve ' + N('&gt; 10 %') + ' frente a su línea base de 40 días). Un día conciliado requiere completitud ' + N('≥ 95 %') + '.</td></tr>'
 '<tr><td>R2</td><td>El desempeño ' + D + ' evaluarse <strong>a diario</strong> como PR y PR_STC (corregido a 25 °C; γ = ' + N('−0,35 %/°C') + ' hasta configurar el coeficiente de la hoja de datos por planta) contra la energía esperada de la planta (kWp × irradiación en el plano × PR de diseño), y <strong>mensualmente en el cierre</strong> contra la energía contratada (PPA) — sobre ≥ 1 año para una declaración bancable (IEC 61724-3). La línea base de puesta en marcha AGS-601 es la referencia del primer año.</td></tr>'
 '<tr><td>R3</td><td>La investigación de bajo desempeño ' + D + ' activarse automáticamente: energía de planta por debajo del ' + N('85 %') + ' de lo esperado (WARNING) o del ' + N('70 %') + ' (CRITICAL); rendimiento específico por debajo del 85 / 70 % de la planta gemela regional; un inversor por debajo del 85 / 70 % de sus pares; una bandera de diagnóstico de string <em>nueva</em>; temperatura de inversor ' + N('≥ 65 °C') + ' (WARNING) o ' + N('≥ 70 °C') + ' y ≥ 5 °C sobre sus pares (CRITICAL, con la pérdida por derating medida en kWh); sin telemetría (oscura) o datos viejos &gt; 45 min con luz de día. La causa raíz ' + D + ' <em>encontrarse</em> — cada alerta lleva la explicación del motor y la bitácora de mantenimiento registra la acción — nunca aceptarse.</td></tr>'
 '</tbody></table>')

ES[282] = body("AGS-701 · Requisitos Clave", "Mantenimiento, degradación, BESS, reportes (R4–R9)",
 '<p class="lead">El mantenimiento y el seguimiento cierran contra metas nombradas — y el capítulo dice claramente qué mide el monitor y qué sigue siendo una actividad en sitio.</p>'
 '<table class="tbl"><thead><tr><th>#</th><th>Regla</th></tr></thead><tbody>'
 '<tr><td>R4</td><td>Mantenimiento preventivo conforme a IEC 62446-2: <strong>limpieza guiada por la relación de ensuciamiento medida</strong> (el monitor califica la limpieza NOT DUE / APPROACHING / DUE / OVERDUE a partir de ≥ 7 días de historial de PR contra el costo de limpieza; PB-04 ' + N('3 %') + ' es la tolerancia de diseño); termografía IR anual contra la línea base AGS-601 (IEC 62446-3), re-prueba eléctrica, torque/estructura, vegetación/drenaje — estas cuatro se <em>registran como eventos de mantenimiento</em>, el monitor no las mide.</td></tr>'
 '<tr><td>R5</td><td>Mantenimiento correctivo: alertas agudas en <strong>30 minutos</strong> (07:00–19:30 MX) y un resumen diario a las 07:07; <strong>disponibilidad calculada a diario por inversor</strong> (fracción de intervalos diurnos reportando) contra la meta SLA de la planta (' + N('≈ 99 %') + ', PB-03, IEC 63019); MTTR desde la bitácora; refacciones estratégicas AGS-401.</td></tr>'
 '<tr><td>R6</td><td>Degradación (IEC 61724-4) contra la garantía ' + N('≤ 0,4 %/año') + ' (PB-01) a partir de la serie mensual de PR_STC — <em>disponible cuando existan dos años operativos completos en el monitor; aún no es un KPI de flota</em>. Excederla abre un reclamo de garantía AGS-401.</td></tr>'
 '<tr><td>R7</td><td>O&amp;M de BESS (SOH, ciclos vs garantía, ampliación AGS-301, re-prueba de seguridad AGS-302) — <em>hoy no hay BESS en la flota operativa</em>; la regla aplica cuando se instale uno.</td></tr>'
 '<tr><td>R8</td><td>Gestión de garantías: el monitor aporta la <strong>evidencia</strong> — códigos de falla del fabricante, banderas de string, historial de inversores silenciosos y el registro de salud térmica (pico de temperatura, horas ≥ 65 °C, ΔT vs pares, derating sospechado en kWh y MXN).</td></tr>'
 '<tr><td>R9</td><td>Reportes: correo diario de desempeño (flota PPA), páginas de reporte por planta y el <strong>cierre mensual en el portal</strong> (energía, esperada, PR, PR_STC, disponibilidad, CO₂, base de facturación desde contadores conciliados), correo financiero a propietario/financiador. Conjunto de KPI: PR_STC, rendimiento específico, disponibilidad, energía vs contratada — degradación cuando R6 sea medible.</td></tr>'
 '</tbody></table>')

ES[283] = body("AGS-701 · Métodos", "El ciclo de O&amp;M y la secuencia de investigación",
 '<p class="lead">El ciclo que cierra el libro: monitorear, conciliar, evaluar sobre la métrica de diseño, comparar con el contrato, investigar, retroalimentar.</p>'
 '<div class="flow"><div class="step"><strong>MONITOREAR</strong> — telemetría de inversor cada 5 min + base de irradiancia + control de deriva satelital</div>'
 '<div class="step">→ <strong>CONCILIAR</strong> cada noche contra los contadores del propio fabricante — PASS ' + N('≤ 1 %') + ' / REVIEW ' + N('≤ 3 %') + ' / FAIL diario; ' + N('0,5 / 1,5 %') + ' mensual; completitud ≥ 95 %; reintento durante 14 días cuando un contador vuelve</div>'
 '<div class="step">→ <strong>EVALUAR</strong> a diario: energía vs esperada, PR, PR_STC, disponibilidad, relación de ensuciamiento, salud térmica</div>'
 '<div class="step">→ <strong>COMPARAR</strong> mensualmente en el cierre: energía facturable vs contratada (PPA) — los meses cerrados se congelan</div>'
 '<div class="step">→ <strong>ALERTAR E INVESTIGAR</strong> en los umbrales de R3 — la alerta se explica sola; una ventana de mantenimiento registrada silencia una planta conocida sin perder el rastro</div>'
 '<div class="step">→ <strong>REPORTAR</strong> (correo diario, portal, cierre mensual, facturas) → retroalimentar correcciones del modelo a AGS-202 / 207</div></div>'
 '<p><strong>Bajo desempeño:</strong> normalizar por clima → ordenar causas: ensuciamiento (relación) → sombreado → fallas de string / inversor (banderas, comparación con pares, derating térmico) → degradación → disponibilidad / paros → confirmar en sitio por termografía / I-V contra la línea base AGS-601 → acción correctiva → verificar que el PR_STC se recupera.</p>'
 '<p><strong>Calendario de mantenimiento</strong> (IEC 62446-2): limpieza (por ensuciamiento) · termografía (anual, vs línea base) · re-prueba eléctrica · torque/estructura · vegetación/drenaje · revisiones de BESS + re-prueba anual de seguridad donde exista un BESS.</p>')

ES[284] = body("AGS-701 · Ejemplo Desarrollado", "El proyecto de 417 kWp en San Luis Potosí en operación",
 '<p class="lead">Cada KPI operativo se verifica contra una referencia fijada antes en el libro — y contra lo que el monitor de flota calcula cada día.</p>'
 '<table class="tbl"><thead><tr><th>KPI / actividad</th><th>Referencia</th><th>Verificación operativa (Argia_Mont)</th></tr></thead><tbody>'
 '<tr><td>Monitoreo (R1)</td><td>IEC 61724-1:2021 Clase A/B</td><td>Telemetría de inversor cada 5 min; sensor del sitio + control de deriva satelital; completitud ≥ 95 %</td></tr>'
 '<tr><td>PR_STC año 1 (R2)</td><td>Modelo 0,87 (AGS-202) + línea base de puesta en marcha (AGS-601)</td><td>PR y PR_STC diarios vs energía esperada; cierre mensual</td></tr>'
 '<tr><td>Energía vs contratada (R3)</td><td>629 MWh (95 % del P90 a 1 año, AGS-207)</td><td>kWh facturables desde contadores conciliados vs contrato en el cierre</td></tr>'
 '<tr><td>Bajo desempeño (R3)</td><td>Umbrales de R3</td><td>Alertas: 85 / 70 % de lo esperado, gemela, pares, strings, térmica, oscura/datos viejos</td></tr>'
 '<tr><td>Ensuciamiento (R4)</td><td>PB-04 3 %</td><td>Relación de ensuciamiento → limpieza DUE / OVERDUE</td></tr>'
 '<tr><td>Termografía (R4)</td><td>Línea base AGS-601</td><td>Barrido anual vs línea base — registrado como evento de mantenimiento</td></tr>'
 '<tr><td>Degradación (R6)</td><td>Garantía ≤ 0,4 %/año (PB-01)</td><td>Desde la serie mensual de PR_STC tras dos años operativos</td></tr>'
 '<tr><td>Disponibilidad (R5)</td><td>≈ 99 % (PB-03, IEC 63019)</td><td>Diaria por inversor vs la meta SLA de la planta; MTTR desde la bitácora</td></tr>'
 '<tr><td>BESS (R7)</td><td>SOH, ciclos, ampliación (AGS-301)</td><td>n/a — sin BESS en la flota operativa</td></tr>'
 '</tbody></table>')

ES[285] = body("AGS-701 · Ejemplo Desarrollado / Aceptación", "Un bajo desempeño detectado — y PASS / REVIEW / FAIL",
 '<p class="lead">A mitad de año, el PR_STC operativo cae por debajo de lo esperado. Normalizado por clima (no es solo un tramo nublado), la investigación lo rastrea hasta <strong>ensuciamiento por encima del 3 % del modelo</strong>; la limpieza restaura el PR_STC — el ciclo funcionó.</p>'
 '<table class="tbl"><thead><tr><th>Resultado</th><th>Condición</th></tr></thead><tbody>'
 '<tr><td><span class="badge pass">PASS</span></td><td>Clase de monitoreo documentada; PR / PR_STC diarios vs esperado y energía mensual vs contratada; alertas en los umbrales de R3 con causa raíz registrada; conciliación nocturna PASS; MP IEC 62446-2 programado incl. termografía vs línea base; disponibilidad vs meta SLA; degradación seguida cuando sea medible; evidencia de garantía + reportes en marcha.</td></tr>'
 '<tr><td><span class="badge review">REVIEW</span></td><td>PR_STC ' + N('3–5 %') + ' por debajo de lo esperado en investigación; ensuciamiento APPROACHING / DUE; conciliación REVIEW (1–3 % diario); deriva satelital REVIEW; Clase B en un sistema bancable; degradación aún no medible (&lt; 2 años).</td></tr>'
 '<tr><td><span class="badge fail">FAIL</span></td><td>Comparación de kWh brutos (sin normalizar); bajo desempeño aceptado sin causa raíz; conciliación FAIL o un mes cerrado sin contadores conciliados; sin uso de la línea base termográfica; degradación / disponibilidad sin seguimiento cuando existen los datos; re-prueba de seguridad de BESS omitida donde exista un BESS; sin garantías / reportes.</td></tr>'
 '</tbody></table>')

ES[286] = body("AGS-701 · Errores Comunes", "Qué sale mal en operación — y la vigilancia del Engine",
 '<p class="lead">Las fallas clásicas rompen la misma regla: juzgar la planta con la métrica equivocada o sin referencia.</p>'
 '<ul><li><strong>Comparar kWh brutos</strong> — un mes nublado no es bajo desempeño; normalizar sobre energía esperada y PR_STC (R2).</li>'
 '<li><strong>Confiar en un portal sin conciliar</strong> — el contador del propio fabricante es la referencia; una brecha &gt; 3 % es una falla de recolección, no de producción (R2).</li>'
 '<li><strong>Sin línea base de puesta en marcha</strong> — sin AGS-601 no hay contra qué medir la deriva (R2 / R4).</li>'
 '<li><strong>Aceptar un PR_STC bajo</strong> — investigar hasta la causa raíz: ensuciamiento, sombreado, fallas o degradación real (R3).</li>'
 '<li><strong>Alarmar con un límite plano de temperatura</strong> — el calor del inversor se juzga contra sus pares y el ambiente del sitio; la pérdida se mide como kWh de derating, no se supone del número (R3).</li>'
 '<li><strong>PR bruto en un sitio caliente</strong> — el PR estándar castiga los techos calientes de México; comparar con PR_STC (R2).</li>'
 '<li><strong>Degradación sin seguimiento vs garantía</strong> — real por encima de 0,4 %/año es un reclamo que protege el caso de bancabilidad (R6).</li>'
 '<li><strong>Sin reportes a propietario/financiador</strong> — la bancabilidad se monitorea de forma continua, no solo al financiar (R9).</li></ul>'
 '<p><strong>Engine:</strong> 🟢 clase de monitoreo documentada, PR_STC normalizado, conciliación PASS, alertas + reportes activos · 🟡 cualquier REVIEW — PR_STC 3–5 % por debajo, conciliación REVIEW, deriva de sensor, limpieza DUE · 🔴 cualquier FAIL — juicio por kWh brutos, bajo desempeño sin explicar, conciliación FAIL, degradación sin seguimiento cuando es medible.</p>')

ES[287] = body("AGS-701 · Repaso del capítulo", "Antes del test: <em>recuerda</em> esto.",
 '<ul class="review">'
 '<li>Instala monitoreo conforme a IEC 61724-1:2021 — Clase A (piranómetro calefactado y calibrado) para sistemas bancables/mayores, Clase B en los demás; en operación: telemetría de inversor cada 5 minutos, sensor del sitio donde exista, referencia satelital con control de deriva del 10 %, completitud ≥ 95 %.</li>'
 '<li>Evalúa el desempeño normalizado por clima — PR y PR_STC diarios vs energía esperada, mensual en el cierre vs energía contratada — nunca kWh brutos.</li>'
 '<li>Concilia cada día contra los contadores del propio fabricante (PASS ≤ 1 %, REVIEW ≤ 3 %, FAIL; 0,5 / 1,5 % mensual) — un mes cerrado se congela.</li>'
 '<li>Investiga en los umbrales de R3: 85 / 70 % de lo esperado, planta gemela, inversores pares, banderas de string nuevas, derating térmico, datos oscuros / viejos — encuentra la causa raíz, nunca aceptes.</li>'
 '<li>Programa el mantenimiento preventivo IEC 62446-2: limpieza por la relación de ensuciamiento medida, termografía anual vs línea base AGS-601, re-prueba eléctrica, torque/estructura, vegetación/drenaje.</li>'
 '<li>Sigue la disponibilidad a diario por inversor vs la meta SLA (≈ 99 %, PB-03) y la degradación vs ≤ 0,4 %/año (PB-01) cuando existan dos años operativos — los excesos se vuelven reclamos de garantía AGS-401 respaldados por la evidencia del monitor.</li>'
 '<li>Reporta el conjunto de KPI de forma continua (correo diario, portal, cierre mensual, facturas) y retroalimenta las desviaciones a AGS-202 / 207 — el estándar verifica sus propias predicciones.</li></ul>')

CS = {}
M = '<strong>musí</strong>'
CS[281] = body("AGS-701 · Klíčové požadavky", "Monitorování a vyhodnocení výkonnosti (R1–R3)",
 '<p class="lead">Výkonnost se posuzuje <strong>normalizovaně na počasí</strong> — očekávaná energie a PR_STC z naměřeného ozáření — nikdy hrubé kWh proti slunečnému či zataženému roku. Rev 1 (2026-09): pravidla jsou napsána tak, jak je uplatňuje <strong>Argia_Mont</strong>, provozní monitor flotily.</p>'
 '<table class="tbl"><thead><tr><th>#</th><th>Pravidlo</th></tr></thead><tbody>'
 '<tr><td>R1</td><td>Monitorovací systém ' + M + ' být instalován dle IEC 61724-1:2021 — <em>třída A</em> (vyhřívaný, kalibrovaný pyranometr) pro bankovatelné/větší systémy, jinak třída B. V provozu monitor sbírá <strong>telemetrii po střídačích každých 5 minut</strong> z platformy výrobce (Growatt, Huawei, SolarEdge), čidlo ozáření na místě tam, kde je instalováno, a <strong>satelitní referenci</strong> (Open-Meteo) jako zálohu a kontrolu driftu čidla (REVIEW, když se poměr měřené/satelit posune o ' + N('&gt; 10 %') + ' proti 40dennímu baseline). Sladěný den vyžaduje úplnost ' + N('≥ 95 %') + '.</td></tr>'
 '<tr><td>R2</td><td>Výkonnost ' + M + ' být vyhodnocena <strong>denně</strong> jako PR a PR_STC (teplotně korigováno na 25 °C; γ = ' + N('−0,35 %/°C') + ', dokud není pro elektrárnu nastaven koeficient z datasheetu modulu) proti očekávané energii elektrárny (kWp × ozáření v rovině × návrhové PR) a <strong>měsíčně při uzávěrce</strong> proti nasmlouvané energii (PPA) — přes ≥ 1 rok pro bankovatelné tvrzení (IEC 61724-3). Baseline z uvedení do provozu (AGS-601) je referencí prvního provozního roku.</td></tr>'
 '<tr><td>R3</td><td>Šetření podvýkonu se ' + M + ' spustit automaticky: energie elektrárny pod ' + N('85 %') + ' očekávané (WARNING) nebo pod ' + N('70 %') + ' (CRITICAL); měrný výnos pod 85 / 70 % regionální dvojčecí elektrárny; střídač pod 85 / 70 % svých sousedů; <em>nový</em> diagnostický příznak stringu; teplota střídače ' + N('≥ 65 °C') + ' (WARNING) nebo ' + N('≥ 70 °C') + ' a ≥ 5 °C nad sousedy (CRITICAL, s naměřenou ztrátou deratingem v kWh); žádná telemetrie (tma) nebo data starší než 45 min za dne. Kořenová příčina ' + M + ' být <em>nalezena</em> — každý alert nese vysvětlení enginu a deník údržby zaznamená zásah — nikdy akceptována.</td></tr>'
 '</tbody></table>')

CS[282] = body("AGS-701 · Klíčové požadavky", "Údržba, degradace, BESS, reporting (R4–R9)",
 '<p class="lead">Údržba a sledování se uzavírají proti pojmenovaným cílům — a kapitola říká otevřeně, co monitor měří a co zůstává činností na místě.</p>'
 '<table class="tbl"><thead><tr><th>#</th><th>Pravidlo</th></tr></thead><tbody>'
 '<tr><td>R4</td><td>Preventivní údržba dle IEC 62446-2: <strong>čištění řízené naměřeným poměrem znečištění</strong> (monitor hodnotí čištění NOT DUE / APPROACHING / DUE / OVERDUE z ≥ 7 dnů historie PR proti nákladům na čištění; PB-04 ' + N('3 %') + ' je návrhová rezerva); roční IR termografie vs baseline AGS-601 (IEC 62446-3), elektrická přezkouška, momenty/konstrukce, vegetace/odvodnění — tyto čtyři se <em>zapisují jako události údržby</em>, monitor je neměří.</td></tr>'
 '<tr><td>R5</td><td>Korektivní údržba: akutní alerty do <strong>30 minut</strong> (07:00–19:30 MX) a denní přehled v 07:07; <strong>dostupnost počítaná denně po střídačích</strong> (podíl denních intervalů s daty) proti cíli SLA elektrárny (' + N('≈ 99 %') + ', PB-03, IEC 63019); MTTR z deníku údržby; strategické náhradní díly AGS-401.</td></tr>'
 '<tr><td>R6</td><td>Degradace (IEC 61724-4) proti zaručeným ' + N('≤ 0,4 %/rok') + ' (PB-01) z měsíční řady PR_STC — <em>k dispozici, jakmile v monitoru existují dva celé provozní roky; zatím není KPI flotily</em>. Překročení otevírá záruční nárok AGS-401.</td></tr>'
 '<tr><td>R7</td><td>O&amp;M BESS (SOH, cykly vs záruka, rozšíření AGS-301, opakovaná zkouška bezpečnosti AGS-302) — <em>v provozní flotile dnes žádný BESS není</em>; pravidlo platí, jakmile bude instalován.</td></tr>'
 '<tr><td>R8</td><td>Správa záruk: monitor dodává <strong>důkazy</strong> — chybové kódy výrobce, příznaky stringů, historii mlčících střídačů a záznam tepelného zdraví (špičková teplota, hodiny ≥ 65 °C, ΔT vs sousedé, podezřelý derating v kWh a MXN).</td></tr>'
 '<tr><td>R9</td><td>Reporting: denní e-mail o výkonnosti (flotila PPA), stránky reportu po elektrárnách a <strong>měsíční uzávěrka na portálu</strong> (energie, očekávaná, PR, PR_STC, dostupnost, CO₂, fakturační základ ze sladěných počítadel), finanční e-mail vlastníkovi/věřiteli. Sada KPI: PR_STC, měrný výnos, dostupnost, energie vs nasmlouvaná — degradace, jakmile bude R6 měřitelné.</td></tr>'
 '</tbody></table>')

CS[283] = body("AGS-701 · Metody", "Smyčka O&amp;M a postup šetření",
 '<p class="lead">Smyčka, která uzavírá knihu: monitorovat, sladit, vyhodnotit na návrhové metrice, porovnat se smlouvou, šetřit, vracet zpět.</p>'
 '<div class="flow"><div class="step"><strong>MONITOROVAT</strong> — 5min telemetrie střídačů + základ ozáření + satelitní kontrola driftu</div>'
 '<div class="step">→ <strong>SLADIT</strong> každou noc s vlastními počítadly výrobce — PASS ' + N('≤ 1 %') + ' / REVIEW ' + N('≤ 3 %') + ' / FAIL denně; ' + N('0,5 / 1,5 %') + ' měsíčně; úplnost ≥ 95 %; opakování 14 dní, když se počítadlo vrátí</div>'
 '<div class="step">→ <strong>VYHODNOTIT</strong> denně: energie vs očekávaná, PR, PR_STC, dostupnost, poměr znečištění, tepelné zdraví</div>'
 '<div class="step">→ <strong>POROVNAT</strong> měsíčně při uzávěrce: fakturovatelná energie vs nasmlouvaná (PPA) — uzavřené měsíce jsou zmrazené</div>'
 '<div class="step">→ <strong>ALERTOVAT A ŠETŘIT</strong> na prazích R3 — alert se sám vysvětlí; zapsané okno údržby ztiší známý výpadek bez ztráty stopy</div>'
 '<div class="step">→ <strong>REPORTOVAT</strong> (denní e-mail, portál, měsíční uzávěrka, faktury) → vracet korekce modelu do AGS-202 / 207</div></div>'
 '<p><strong>Podvýkon:</strong> normalizovat na počasí → seřadit příčiny: znečištění (poměr) → stínění → poruchy stringů / střídačů (příznaky, srovnání se sousedy, tepelný derating) → degradace → dostupnost / prostoje → potvrdit na místě termografií / I-V vs baseline AGS-601 → nápravné opatření → ověřit, že se PR_STC zotaví.</p>'
 '<p><strong>Plán údržby</strong> (IEC 62446-2): čištění (dle znečištění) · termografie (ročně, vs baseline) · elektrická přezkouška · momenty/konstrukce · vegetace/odvodnění · kontroly BESS + roční zkouška bezpečnosti tam, kde BESS existuje.</p>')

CS[284] = body("AGS-701 · Řešený příklad", "Projekt 417 kWp San Luis Potosí v provozu",
 '<p class="lead">Každé provozní KPI se ověřuje proti referenci stanovené dříve v knize — a proti tomu, co monitor flotily počítá každý den.</p>'
 '<table class="tbl"><thead><tr><th>KPI / činnost</th><th>Reference</th><th>Provozní kontrola (Argia_Mont)</th></tr></thead><tbody>'
 '<tr><td>Monitorování (R1)</td><td>IEC 61724-1:2021 třída A/B</td><td>5min telemetrie střídačů; čidlo na místě + satelitní kontrola driftu; úplnost ≥ 95 %</td></tr>'
 '<tr><td>PR_STC v 1. roce (R2)</td><td>Model 0,87 (AGS-202) + baseline uvedení do provozu (AGS-601)</td><td>Denní PR a PR_STC vs očekávaná energie; měsíční uzávěrka</td></tr>'
 '<tr><td>Energie vs nasmlouvaná (R3)</td><td>629 MWh (95 % ročního P90, AGS-207)</td><td>Fakturovatelné kWh ze sladěných počítadel vs smlouva při uzávěrce</td></tr>'
 '<tr><td>Podvýkon (R3)</td><td>Prahy R3</td><td>Alerty: 85 / 70 % očekávané, dvojče, sousedé, stringy, teplota, tma/stará data</td></tr>'
 '<tr><td>Znečištění (R4)</td><td>PB-04 3 %</td><td>Poměr znečištění → čištění DUE / OVERDUE</td></tr>'
 '<tr><td>Termografie (R4)</td><td>Baseline AGS-601</td><td>Roční sken vs baseline — zapsán jako událost údržby</td></tr>'
 '<tr><td>Degradace (R6)</td><td>Zaručeno ≤ 0,4 %/rok (PB-01)</td><td>Z měsíční řady PR_STC po dvou provozních letech</td></tr>'
 '<tr><td>Dostupnost (R5)</td><td>≈ 99 % (PB-03, IEC 63019)</td><td>Denně po střídačích vs cíl SLA elektrárny; MTTR z deníku údržby</td></tr>'
 '<tr><td>BESS (R7)</td><td>SOH, cykly, rozšíření (AGS-301)</td><td>n/a — v provozní flotile žádný BESS</td></tr>'
 '</tbody></table>')

CS[285] = body("AGS-701 · Řešený příklad / Akceptace", "Zachycený podvýkon — a PASS / REVIEW / FAIL",
 '<p class="lead">V polovině roku klesne provozní PR_STC pod očekávání. Normalizováno na počasí (nejde jen o zatažené období), šetření jej dovede ke <strong>znečištění nad modelovými 3 %</strong>; čištění PR_STC obnoví — smyčka zafungovala.</p>'
 '<table class="tbl"><thead><tr><th>Výsledek</th><th>Podmínka</th></tr></thead><tbody>'
 '<tr><td><span class="badge pass">PASS</span></td><td>Třída monitorování zdokumentována; denní PR / PR_STC vs očekávání a měsíční energie vs nasmlouvaná; alerty na prazích R3 se zaznamenanou kořenovou příčinou; noční sladění PASS; preventivní údržba dle IEC 62446-2 naplánována vč. termografie vs baseline; dostupnost vs cíl SLA; degradace sledována, jakmile je měřitelná; záruční důkazy + reporting zavedeny.</td></tr>'
 '<tr><td><span class="badge review">REVIEW</span></td><td>PR_STC ' + N('3–5 %') + ' pod očekáváním v šetření; znečištění APPROACHING / DUE; sladění REVIEW (1–3 % denně); satelitní drift REVIEW; třída B na bankovatelném systému; degradace zatím neměřitelná (&lt; 2 roky).</td></tr>'
 '<tr><td><span class="badge fail">FAIL</span></td><td>Porovnání hrubých kWh (nenormalizované); podvýkon akceptován bez kořenové příčiny; sladění FAIL nebo měsíc uzavřený bez sladěných počítadel; nevyužitá termografická baseline; degradace / dostupnost nesledovány, ač data existují; vynechaná zkouška bezpečnosti BESS tam, kde BESS je; bez záruk / reportingu.</td></tr>'
 '</tbody></table>')

CS[286] = body("AGS-701 · Časté chyby", "Co se v provozu kazí — a dohled Engine",
 '<p class="lead">Klasická selhání porušují totéž pravidlo: posuzovat elektrárnu špatnou metrikou nebo bez reference.</p>'
 '<ul><li><strong>Porovnávání hrubých kWh</strong> — zatažený měsíc není podvýkon; normalizovat na očekávanou energii a PR_STC (R2).</li>'
 '<li><strong>Důvěra portálu bez sladění</strong> — referencí je vlastní počítadlo výrobce; rozdíl &gt; 3 % je chyba sběru dat, ne výroby (R2).</li>'
 '<li><strong>Bez baseline z uvedení do provozu</strong> — bez AGS-601 není proti čemu měřit drift (R2 / R4).</li>'
 '<li><strong>Akceptace nízkého PR_STC</strong> — šetřit ke kořenové příčině: znečištění, stínění, poruchy, nebo skutečná degradace (R3).</li>'
 '<li><strong>Alarm na plochý teplotní limit</strong> — teplo střídače se posuzuje proti sousedům a okolní teplotě místa; ztráta se měří jako kWh deratingu, nepředpokládá se z čísla (R3).</li>'
 '<li><strong>Hrubé PR na horkém místě</strong> — standardní PR penalizuje horké mexické střechy; srovnávat na PR_STC (R2).</li>'
 '<li><strong>Nesledovaná degradace vs záruka</strong> — skutečná nad 0,4 %/rok je nárok chránící bankovatelnost (R6).</li>'
 '<li><strong>Žádný reporting vlastníkovi/věřiteli</strong> — bankovatelnost se sleduje průběžně, ne jen při financování (R9).</li></ul>'
 '<p><strong>Engine:</strong> 🟢 třída monitorování zdokumentována, PR_STC normalizováno, sladění PASS, alerty + reporting v provozu · 🟡 jakékoli REVIEW — PR_STC 3–5 % pod, sladění REVIEW, drift čidla, čištění DUE · 🔴 jakékoli FAIL — posuzování hrubými kWh, nevysvětlený podvýkon, sladění FAIL, nesledovaná degradace, ač měřitelná.</p>')

CS[287] = body("AGS-701 · Shrnutí kapitoly", "Před testem: <em>tohle</em> si zapamatuj.",
 '<ul class="review">'
 '<li>Instaluj monitorování dle IEC 61724-1:2021 — třída A (vyhřívaný, kalibrovaný pyranometr) pro bankovatelné/větší systémy, jinak třída B; v provozu: 5minutová telemetrie střídačů, čidlo na místě tam, kde je, satelitní reference s 10% kontrolou driftu, úplnost ≥ 95 %.</li>'
 '<li>Vyhodnocuj výkonnost normalizovaně na počasí — denní PR a PR_STC vs očekávaná energie, měsíčně při uzávěrce vs nasmlouvaná energie — nikdy hrubé kWh.</li>'
 '<li>Slaďuj každý den s vlastními počítadly výrobce (PASS ≤ 1 %, REVIEW ≤ 3 %, FAIL; 0,5 / 1,5 % měsíčně) — uzavřený měsíc je zmrazený.</li>'
 '<li>Šetři na prazích R3: 85 / 70 % očekávané, dvojčecí elektrárna, sousední střídače, nové příznaky stringů, tepelný derating, tma / stará data — najdi kořenovou příčinu, nikdy neakceptuj.</li>'
 '<li>Plánuj preventivní údržbu IEC 62446-2: čištění podle naměřeného poměru znečištění, roční termografii vs baseline AGS-601, elektrickou přezkoušku, momenty/konstrukci, vegetaci/odvodnění.</li>'
 '<li>Sleduj dostupnost denně po střídačích vs cíl SLA (≈ 99 %, PB-03) a degradaci vs ≤ 0,4 %/rok (PB-01), jakmile existují dva provozní roky — překročení se stávají záručními nároky AGS-401 podloženými důkazy z monitoru.</li>'
 '<li>Reportuj sadu KPI průběžně (denní e-mail, portál, měsíční uzávěrka, faktury) a vracej odchylky do AGS-202 / 207 — standard ověřuje vlastní predikce.</li></ul>')

QUIZ = {
 "en": ("What triggers the underperformance investigation under R3?",
        ["Weather-normalized actual below the alert thresholds (85 % WARNING / 70 % CRITICAL of expected daily; twin-plant, peer-inverter, string, thermal and data alerts) or below the contracted energy at the monthly close",
         "Any month whose raw kWh is below the previous year's", "PR dropping during a heat wave", "A lender requesting an ad-hoc audit"], 0,
        "R3 triggers automatically at the monitor's thresholds — 85 / 70 % of expected energy, twin plant, peer inverters, new string flags, thermal derating, dark / stale data — and at the monthly close when billable energy falls below the contracted figure; the root cause is found, never accepted."),
 "es": ("¿Qué activa la investigación de bajo desempeño bajo R3?",
        ["El valor real normalizado por clima por debajo de los umbrales de alerta (85 % WARNING / 70 % CRITICAL de lo esperado diario; alertas de planta gemela, inversores pares, strings, térmicas y de datos) o por debajo de la energía contratada en el cierre mensual",
         "Cualquier mes cuyos kWh brutos estén por debajo del año anterior", "Una caída del PR durante una ola de calor", "Un financiador pidiendo una auditoría ad hoc"], 0,
        "R3 se activa automáticamente en los umbrales del monitor — 85 / 70 % de la energía esperada, planta gemela, inversores pares, banderas de string nuevas, derating térmico, datos oscuros / viejos — y en el cierre mensual cuando la energía facturable cae por debajo de lo contratado; la causa raíz se encuentra, nunca se acepta."),
 "cs": ("Co spouští šetření podvýkonu podle R3?",
        ["Skutečnost normalizovaná na počasí pod prahy alertů (85 % WARNING / 70 % CRITICAL očekávané denně; alerty dvojčecí elektrárny, sousedních střídačů, stringů, teploty a dat) nebo pod nasmlouvanou energii při měsíční uzávěrce",
         "Jakýkoli měsíc, jehož hrubé kWh jsou pod loňskem", "Pokles PR během vlny veder", "Věřitel žádající ad hoc audit"], 0,
        "R3 se spouští automaticky na prazích monitoru — 85 / 70 % očekávané energie, dvojčecí elektrárna, sousední střídače, nové příznaky stringů, tepelný derating, tma / stará data — a při měsíční uzávěrce, když fakturovatelná energie klesne pod nasmlouvanou; kořenová příčina se hledá, nikdy neakceptuje."),
}

DEEPLINK_MARK = "AGS deep-links v3"
DEEPLINK_JS = """<script>
/* AGS deep-links v3 (2026-09-07, v219): #lang=es&slide=281 opens slide 281 in
   Spanish; #lang=en&ags=AGS-701 opens the chapter in English. Ask ARGIA
   cites 'slide N' and links here in the language of the answer, so an
   English question never lands on a Spanish slide. Zero impact without a
   hash. Slide numbers are 1-based as printed on the page. */
(function(){
  function apply(){
    try{
      var h=decodeURIComponent(location.hash||"").replace(/^#/,"");
      if(!h||h.indexOf("=")<0) return;
      var kv={}; h.split("&").forEach(function(p){var i=p.indexOf("="); if(i>0) kv[p.slice(0,i).toLowerCase()]=p.slice(i+1);});
      var l=(kv.lang||"").toLowerCase(); if(l==="cz") l="cs";
      if(l&&DATA[l]&&typeof setLang==="function") setLang(l);
      if(kv.slide){ var n=parseInt(kv.slide,10); if(n>=1&&n<=slides.length) show(n-1); }
      else if(kv.ags){ setTimeout(function(){ location.replace("#ags="+kv.ags); },0); }  /* the v2 chapter jump takes over */
    }catch(e){}
  }
  window.addEventListener("hashchange",apply);
  apply();
})();
</script>
"""


def apply():
    page = open(SRC, encoding="utf-8").read()
    m = re.search(r"const\s+DATA\s*=\s*", page)
    data, end = json.JSONDecoder().raw_decode(page, m.end())
    changed = 0
    for lang, table in (("en", EN), ("es", ES), ("cs", CS)):
        for n, html in table.items():
            old = data[lang]["slides"][n - 1]
            assert "AGS-701" in old, (lang, n, old[:80])
            data[lang]["slides"][n - 1] = html
            changed += 1
        q, opts, correct, expl = QUIZ[lang]
        ch = data[lang]["quiz"][25]
        assert ch["code"] == "AGS-701"
        old_q = ch["quiz"][1]
        assert "R3" in old_q["explain"], old_q
        ch["quiz"][1] = {"q": q, "options": opts, "correct": correct, "explain": expl}
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    out = page[:m.end()] + blob + page[end:]
    # v219 deep links: #lang=es&slide=281 (Ask ARGIA citations) on top of the
    # existing #ags=AGS-701 chapter jump — language first, then the slide.
    if DEEPLINK_MARK not in out:
        anchor = "</body>"
        assert out.count(anchor) == 1
        out = out.replace(anchor, DEEPLINK_JS + anchor)
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(out)
    print("slides changed:", changed, "bytes:", len(out.encode("utf-8")))

if __name__ == "__main__":
    apply()
