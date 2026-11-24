## Empiece aquí {#start}

Esta página explica cada número de las páginas de monitoreo del portal ARGIA: de dónde viene, cómo se calcula, qué supuestos usa y dónde están sus límites. Está escrita para el equipo de O&M, para los ingenieros de nuestros clientes y para cualquiera que vea una cifra y se pregunte "¿por qué este valor?".

**Cómo encontrar una respuesta rápido**

- Cada tarjeta de la página de una planta tiene un pequeño **?** junto a su título. Abre esta página en la sección que explica esa tarjeta.
- El buscador de la izquierda filtra el contenido. El cambio de idioma (EN / ES) conserva su lugar.
- Haga clic en cualquier captura para verla en tamaño completo.

**Principios detrás de cada cifra**

1. **Primero lo medido, después lo estimado, y siempre se dice cuál es cuál.** Un valor estimado lleva la palabra *estimado*, *esperado* o *diseño*, y la regla que lo produjo está escrita aquí.
2. **Los cocientes de un periodo son cocientes de sumas**, nunca el promedio de cocientes diarios (una excepción, la tarjeta de PR de 30 días, se nombra en su sección).
3. **Un dato faltante nunca es cero.** Una conexión perdida es "sin datos", no "sin producción". La energía que prueba el propio contador del inversor nunca se pierde.
4. **El contador acumulado del fabricante es el control de facturación.** Los datos de 5 minutos son para operación y diagnóstico; nunca pueden bajar una factura.
5. **Un cálculo, muchas páginas.** La página en vivo, los correos diarios, los reportes y las alertas leen las mismas cifras guardadas, así que no pueden contradecirse.

Las capturas vienen del sitio demo de ARGIA (plantas reales sin nombres) y, para las tarjetas que muestran datos de ingeniería del cliente (distribución de cadenas, diagnóstico, consumo), de una planta de prueba inventada.

## El portal de un vistazo {#portal}

![La página de inicio: un mosaico por área del portal](home)

El portal tiene cuatro áreas: **Reportes** (reportes diarios, mensuales y financieros, anexos de factura), **Monitoreo** (páginas de planta en vivo, desempeño, diseño, pérdidas, conciliación), **Mapa** y **Ahorros**. El encabezado siempre tiene los mismos tres botones: Inicio, Pregunte a ARGIA (preguntas en lenguaje natural sobre los mismos datos) y el menú de su cuenta, donde cambia el idioma y encuentra esta documentación.

![Monitoreo: un mosaico por planta, con el color de su estado en vivo](fleet)

Cada mosaico muestra la potencia en vivo, la energía del día y un color de estado (sección [Estado en vivo](#status)). Haga clic en un mosaico para abrir la página de la planta.

![La parte superior de la página de una planta: cifras en vivo, PR y disponibilidad de los últimos 30 días](plant_top)

La página de una planta se lee de arriba abajo: cifras en vivo y estado, después las tarjetas. Cada tarjeta se puede plegar con la flecha de su título; el portal recuerda lo que plegó, en este navegador, para todas las plantas.

![Tarjetas plegadas: la tarjeta de diagnóstico cerrada, las demás abiertas; ? abre esta página en la tarjeta](folded)

El selector de fecha muestra cualquier día pasado; un día pasado es una copia fija de ese día tal como se calculó.

## Cómo fluyen los datos {#architecture}

```formula
inversores, dataloggers, medidores, estación meteorológica
  -> plataforma en la nube del fabricante (API)
  -> colectores ARGIA (cada 5 a 15 minutos)
  -> base de datos ARGIA (muestras de 5 minutos, cifras diarias, contadores)
  -> cálculos (diarios, nocturnos, mensuales)
  -> páginas del portal (se regeneran cada 5 minutos), correos, reportes
```

ARGIA no instala software en los inversores. Todo se lee por las plataformas documentadas de los fabricantes, solo lectura. La tabla muestra qué corre y cuándo (hora de la Ciudad de México).

| Qué | Cuándo | Qué hace |
|---|---|---|
| Telemetría de inversores | cada 5 min, 05:00 a 21:55 | potencia, energía del día, estado, fallas, temperaturas, corrientes y voltajes de cadenas, irradiancia |
| Telemetría, plataformas por hora | :03 / :23 / :43, 06:00 a 20:59 | lo mismo para plataformas que publican datos de 15 minutos |
| Medidores de red | :02 / :17 / :32 / :47, todo el día | importación, exportación, potencia con signo (sección [Consumo](#consumption)) |
| Páginas del portal | cada 5 min | cada página y tarjeta se reconstruye desde la base de datos |
| Alertas agudas | :00 y :30, 07:00 a 19:59 | condiciones que una sola muestra puede probar (sección [Alertas](#alerts)) |
| KPI de fin de día | 06:00 | energía, irradiación, PR, energía esperada y disponibilidad de ayer |
| Alertas diarias | 06:30 | revisiones de día completo: energía vs esperado, inversor vs pares, térmico |
| Verificación satelital | 06:20 | sensor de irradiancia del sitio contra irradiancia satelital |
| Correo diario de desempeño | 19:00 | el día de cada planta PPA |
| Estadística de cadenas | 21:15 | amperes-hora del día por cadena |
| Conciliación | 23:50 | el día contra los contadores del propio fabricante, con reintento de 14 días |
| Salud térmica | 01:10 | temperaturas, derrateo y su costo de ayer |
| Cierre mensual | día 1, 06:10 | el mes contra los contadores mensual y total del fabricante; un mes cerrado queda fijo |

**Hora.** Cada marca de tiempo se guarda en UTC y se muestra en hora de la Ciudad de México (UTC-6, sin horario de verano). Un día es un día calendario local, de medianoche a medianoche.

**Vigencia.** Una planta cuya muestra más reciente tiene más de 30 minutos entre las 06:00 y las 20:00 se muestra como sin señal. Cada página muestra la antigüedad de sus datos junto a los valores en vivo.

## Qué se recolecta y cómo se limpia {#data}

### Por inversor, cada 5 minutos

| Campo | Uso |
|---|---|
| Potencia CA (W) | potencia en vivo, estado, disponibilidad, curvas intradía |
| Contador de energía del día (kWh desde medianoche) | energía diaria, barras por hora, prueba de producción durante un hueco |
| Contador de energía total (kWh) | conciliación, recuperación tras un contador diario congelado |
| Estado y códigos de falla | estado, alertas (el diagnóstico del propio equipo) |
| Temperatura interna (°C) | salud térmica |
| Por cadena: corriente (A); por MPPT: corriente y voltaje | monitoreo de cadenas, EPI-600 |
| Modo de derrateo (donde el fabricante lo publica) | derrateo térmico confirmado por el fabricante |

### Clima

- **Irradiancia (W/m²)** del sensor propio de la planta (o del sensor ARGIA más cercano si la planta no tiene), con **temperatura de panel y ambiente** donde la estación las mide.
- **Nubosidad (%)** de un modelo meteorológico público en las coordenadas de la planta (se dibuja en la gráfica intradía y sirve de respaldo para la irradiación).
- **Irradiancia satelital** (diaria, análisis de modelo) solo para detectar un sensor que deriva, nunca en un cálculo que llegue a una cifra del cliente.

### Reglas de limpieza (antes de cualquier cálculo)

- **Registros repetidos.** Una API que deja de actualizarse sigue devolviendo su último registro. Una muestra que repite exactamente la anterior (misma potencia y mismo contador del día, potencia mayor a 0) es una repetición, no una medición nueva, y se ignora. Un contador que no se mueve mientras el inversor reporta al menos {{c:gaps.STALE_MIN_W}} W en dos muestras seguidas también se trata como congelado.
- **Arrastre de medianoche.** Algunos inversores reinician su contador del día unos minutos después de medianoche, así que las primeras muestras del día pueden traer el total de ayer. Un tramo inicial plano que luego cae es el contador de ayer y se elimina.
- **Reinicios.** Un contador que cae a media jornada después de crecer es un reinicio; la energía del día es la suma de los tramos.
- **Calidad de irradiancia** (IEA PVPS T13-28, según IEC 61724-1/-3): valores fuera de {{c:engine.GHI_MIN}} a {{c:engine.GHI_MAX}} W/m² son inválidos; más de {{c:engine.NIGHT_MAX_GHI}} W/m² con el sol bajo el horizonte es inválido; un valor congelado dentro de {{c:engine.FROZEN_DELTA}} W/m² durante {{c:engine.FROZEN_RUN}} o más muestras de 5 minutos seguidas por encima de {{c:engine.FROZEN_MIN_GHI}} W/m² es inválido desde su segunda muestra. Las muestras inválidas salen de todos los modelos; se cuentan y se muestran.
- **Ventanas del colector.** Cuando el propio colector de ARGIA estuvo caído para todas las plantas a la vez, esa ventana se excluye antes de juzgar cualquier inversor.

### Irradiación diaria

```formula
H [kWh/m²] = integral trapezoidal de las muestras de irradiancia del día / 1000
```

La regla trapezoidal promedia cada par de muestras vecinas (una suma de rectángulos sobrestima). Se usa el sensor del sitio cuando da al menos 10 muestras válidas repartidas en el día; si no, la irradiación del día viene del modelo de nubosidad y el día queda marcado así. El sol que no se midió nunca se toma prestado de otros días ni de otras plantas.

### Verificación diaria de la estación meteorológica

Cada cifra de desempeño se juzga contra el sol y la temperatura de módulo de la estación, así que cada estación se verifica a diario:

- **Cobertura** - irradiancia, temperatura de módulo y ambiente en al menos {{pct:station.COV_MIN}} de los pasos de 5 minutos de 08:00 a 17:00. Debajo de eso **el PR del día no se usa**.
- **Sensor de módulo montado** - el voltaje de las cadenas baja cerca de 0.25 a 0.35 % por K al calentarse los módulos, así que la pendiente del voltaje MPPT normalizado contra la temperatura de módulo debe quedar debajo de {{c:station.COUPLE_MAX_SLOPE}} %/K (mediana de los últimos {{c:station.COUPLE_WINDOW}} días, al menos {{c:station.COUPLE_MIN_DAYS}} días juzgados).
- **Módulo menos ambiente** a 700 a 950 W/m2 contra los primeros {{c:station.REF_DAYS}} días de la propia estación y contra las demás: más de {{c:station.DRIFT_MAX_K}} K por encima del cambio de la flota falla (el sensor de SAG: cerca de +8 K desde julio).
- Las plantas que comparten estación (irradiancia diaria idéntica) se nombran y usan una sola referencia.
- La verificación satelital se muestra pero no hace fallar el día. El viento y la humedad no se guardan, así que esas pruebas no pueden correr.

## Estado en vivo {#status}

### Estado del mosaico y de la página de la planta

El estado se decide ahora, con las muestras más recientes, entre las 06:00 y las 20:00. Gana la primera regla que se cumple:

| Estado | Regla |
|---|---|
| sin datos hoy (rojo) | ninguna muestra de la planta hoy |
| sin señal (rojo) | ningún inversor tiene una muestra de menos de 30 minutos |
| inversor(es) sin datos (ámbar) | hoy reportaron menos inversores de los configurados |
| falla (ámbar) | un inversor reporta estado de falla |
| 0 W mientras los demás producen (ámbar) | un inversor reporta 0 W mientras un hermano produce |
| inversores sin señal (ámbar) | algunos inversores tienen más de 30 minutos |
| potencia cero a mediodía (ámbar) | todos los inversores en 0 W entre 09:00 y 17:00 |
| alerta crítica de equipo (ámbar) | una alerta crítica abierta en un equipo de la planta |
| todo reportando (verde) | nada de lo anterior |
| noche (gris) | fuera de 06:00 a 20:00 |

El mapa usa los mismos umbrales que los correos de alerta, así que el mapa y los correos nunca se contradicen.

![El mapa: cada planta con su estado en vivo](map)

### Vocabulario de estado de inversores

FALLA (lo dice el fabricante), FUERA DE LÍNEA (sol arriba pero sin datos o en cero), DERRATEADO (bandera de derrateo mientras produce), BAJO DESEMPEÑO (produce, por debajo de sus pares por kW nominal), NOCHE, SIN DATOS, EN LÍNEA. Un solo clasificador alimenta todas las páginas y correos.

## KPI de la planta: energía, PR, disponibilidad {#kpis}

### Energía

```formula
E_día [kWh] = suma por inversor del contador de energía del día (considera reinicios y arrastre)
rendimiento específico [kWh/kWp] = E_día / kWp_CD
```

"Energía hoy (intervalos)" en la página en vivo es la suma del último contador del día de cada inversor; "Contador del fabricante" es el contador de planta que reporta el propio fabricante. El contador del fabricante es la referencia de facturación (sección [Conciliación](#reconciliation)).

### Índice de desempeño (PR)

```formula
PR = E_día / (kWp_CD x H)                     IEC 61724-1
PR_STC = PR / (1 + gamma x (T_celda,ef - 25 °C))
```

- **H** es la irradiación del día en el plano que mide el sensor (kWh/m², ver [Datos](#data)).
- **gamma** es el coeficiente de temperatura de potencia del módulo (negativo, cerca de -0.0034 /°C); **T_celda,ef** es la temperatura del módulo ponderada por irradiancia, así las horas calientes del mediodía, que traen la energía, también traen la corrección. Sin temperatura de módulo medida o sin gamma, no se calcula PR_STC.
- Una planta limpia y nueva en México marca cerca de 0.78 a 0.85; el calor y la suciedad lo bajan.

**La tarjeta "PR 30d"** es el promedio de los PR diarios de los últimos 30 días y necesita al menos {{c:mg.MIN_PR_DAYS}} días con valor. Los días con PR mayor a {{c:mg.PR_MAX}} se excluyen: ese día el sol se midió de menos (sensor sombreado, congelado u horas faltantes), no es una súper planta. Esta tarjeta es la única cifra del portal que promedia cocientes diarios.

### Disponibilidad

La disponibilidad sigue la idea de IEC 61724-3: **el tiempo sin información no es tiempo fuera de servicio**.

```formula
disponibilidad = ARRIBA / (ARRIBA + ABAJO)     ponderada por los kW nominales de cada inversor
cobertura      = (ARRIBA + ABAJO) / tiempo evaluado
```

- El día se corta en pasos de {{c:availability.STEP_MIN}} minutos. Solo se evalúan los pasos con el sol al menos {{c:availability.SUN_MIN_ELEV}}° sobre el horizonte (amanecer, atardecer y el arranque tardío de un inversor pequeño no son falla).
- Por inversor y paso: **ARRIBA** = una lectura con potencia mayor a 0 (diga lo que diga el código de estado); **ABAJO** = lecturas, todas en 0 W, salvo que el contador del día creciera durante el paso (un 0 W congelado en la API); **DESCONOCIDO** = sin lectura.
- Un paso DESCONOCIDO pasa a ARRIBA cuando el propio contador del día prueba que produjo durante el hueco: su energía por kW nominal llega a {{pct:availability.COUNTER_PROOF}} de la mediana de sus pares.
- Cuando toda la planta queda sin datos (internet o datalogger caído: los inversores siguen produciendo y el fabricante recibe los datos después), el tiempo sin datos es ARRIBA si la energía del día llega a {{pct:availability.PLANT_PROOF}} de lo que la planta suele hacer en un día así.
- Un inversor que quedó sin datos después de pararse (última lectura 0 W o falla) sigue ABAJO mientras sus pares reportan.
- Los pasos DESCONOCIDOS que nada prueba se dejan fuera y se reportan como cobertura. Una planta oscura es "sin datos", nunca 0 % ni 100 %.

**30 días:** las disponibilidades diarias ponderadas por su cobertura; un día sin registro suma tiempo debido sin tiempo conocido.

**Por inversor** (reporte de planta, tabla de inversores): los mismos pasos, guardados por inversor en el recálculo nocturno. Disponibilidad = sus ARRIBA / (ARRIBA + ABAJO), Cobertura = su parte conocida; la fila **Planta** pondera los inversores por kW nominales sobre los mismos pasos, así que coincide con la cifra de planta. Un inversor en línea a 0 W mientras sus pares producen está ABAJO; un hueco de conexión nunca es una falla.

**Un valor que el proveedor repite no es una lectura.** Cuando un inversor deja de comunicarse, algunas nubes de fabricante siguen sirviendo sus últimos valores (Huawei, Vitalmex 7 de noviembre: 0 W, el mismo contador del día y la misma temperatura todo el día). Una lectura idéntica a la anterior del inversor en potencia, contador y temperatura se descarta: el paso se juzga con las lecturas reales. Un inversor cuya última lectura real fue 0 W mientras un par producía (y cuyo contador no creció después) sigue ABAJO durante el silencio; uno que calló mientras producía queda DESCONOCIDO. En la tabla de inversores, **Cobertura** es la fracción de pasos juzgados con una lectura real de ese inversor.

```formula
disponibilidad_30d = suma(disponibilidad_d x cobertura_d) / suma(cobertura_d)
cobertura_30d      = suma(cobertura_d) / días debidos
```

![Desempeño: PR, PR_STC, disponibilidad y cobertura por planta](performance)

## Energía esperada: los modelos {#expected}

"Esperado" siempre significa: lo que la planta debió producir con el sol que realmente hubo. ARGIA usa cuatro modelos; cada uno tiene una función.

| Modelo | Fórmula | Se usa para |
|---|---|---|
| Factor del contrato | E_esp = kWp_CD x H x factor_esperado | el anexo de la factura del cliente ("teórica"); los demás nunca lo cambian |
| Diseño HelioScope | kWh a red_mes / H_diseño,mes x H_medida x envejecimiento | desempeño interno (EPI, página de diseño, línea intradía) |
| Modelo por cadena EPI-600 | por cadena y paso de 5 minutos, con la irradiancia en cada plano y la temperatura de celda | tarjeta de diagnóstico, EPI-600 / Disponibilidad-150 |
| Pares | kWh/kWp de las vecinas x kWp de esta planta x su relación usual con ellas | pérdidas (sección [Pérdidas](#losses)) |

### Línea base de diseño HelioScope (interna)

Cada planta PPA tiene su reporte HelioScope tal como se construyó (módulos, cadenas, inclinación, azimut, perfil de suciedad, pérdidas). Por mes el reporte da la energía a la red y la irradiación en el plano del sensor.

```formula
kWh de diseño (mes)    = kWh a red_mes x envejecimiento(mitad de mes)
kWh esperado de diseño = kWh a red_mes / H_diseño,mes x H_medida x envejecimiento
envejecimiento         = (1 - {{pct1:design.DEFAULT_DEGRADATION}} por año) desde la interconexión
EPI (energía)          = real / esperado de diseño        en línea = dentro de {{pct:design.IN_LINE}}
```

### Modelo EPI-600 (5 minutos, por cadena)

Es el modelo de la especificación de desempeño ARGIA v0.7, con los cambios de su auditoría (noviembre 2026).

```formula
1  G_POA,s   = irradiancia en el plano de la cadena s (separación Erbs + transposición Perez 1990)
2  T_celda   = T_panel + {{c:model.T_CELL_OFFSET_K}} x min(G_POA / 1000, 1.2)
3  PR0       = b0 + b1 x T_celda + b2 x g + b3 x g²          g = G_POA / 1000
4  D_s       = 1 - degradación(módulo, años desde la energización de la cadena)
5  E_s [W]   = kWp_s x 1000 x D_s x g x PR0 x (1 - S_mes) x B_mes
6  tope      = min(1, (límite CA / {{c:model.INVERTER_EFFICIENCY}}) / suma de E_s del inversor)
7  esperado  = E_s x tope
```

- **b0..b3** vienen del modelo de la planta (Anexo B); **S_mes** es la suciedad de diseño del mes (Anexo C).
- **B_mes** (sesgo mensual) se calcula con el año de diseño para que cada planta dé exactamente 100 % con su propio clima de diseño; nunca se escribe a mano.
- **Degradación** por tipo de módulo: pérdida del primer año y luego una tasa anual (por ejemplo 1.0 % y luego 0.40 %/año).
- **El límite del inversor** recorta la expectativa CD en su límite CA medido o de hoja técnica dividido entre la eficiencia del inversor.
- Posición del sol: NOAA/Meeus; transposición y masa de aire como en pvlib (equivalencia probada dentro de 0.02° y 1 %).

### La línea teórica intradía

```formula
teórica_h [kWh] = irradiancia media de la hora [W/m²] / 1000 x kWp_CD x PR_base
```

PR_base es el PR de la planta limpia (0.80 si no está definido). Con un diseño HelioScope, el factor de diseño del día sustituye a kWp x PR_base.

## Producción intradía {#intraday}

![Producción intradía: barras de 60 minutos por inversor, la línea teórica y la nubosidad](intraday)

- **Barras:** energía de cada hora por inversor, del contador del día: el mayor contador de la hora menos el mayor de la hora anterior. Una primera hora antes de las 07:00 cuenta desde medianoche; una recolección que empieza a media jornada no dibuja barra de recuperación.
- **Línea oscura punteada:** la energía teórica de cada hora para la irradiancia medida (sección [Esperado](#expected)).
- **Línea gris punteada:** nubosidad, %, eje derecho.

## Tabla de inversores {#inverters}

![La tabla de inversores: potencia, energía, kWh/kWp, cadenas, EPI-600, comparación con pares, temperatura](inverters)

```formula
vs pares = (E_i / P_nominal,i) / mediana entre pares productivos j != i de (E_j / P_nominal,j)
```

- **vs pares** compara energía por kW nominal, así un inversor más pequeño no se marca por error. La mediana excluye al propio inversor; una planta de dos inversores se compara con su hermano. Ámbar bajo {{pct:health.DEFAULT_WARN_BELOW}}, rojo bajo {{pct:health.DEFAULT_CRIT_BELOW}}.
- **°C** última muestra / pico del día; ámbar desde 65 °C, rojo desde 75 °C.
- **kWh/kWp** energía del día entre los kWp del inversor (Anexo A); **Cadenas** cadenas con salida hoy / cadenas; **EPI-600** día / mes a la fecha (sección [Diagnóstico](#troubleshooting)).

## Diagnóstico: planta, inversores, cadenas {#troubleshooting}

Una tarjeta, seis vistas, todas sobre los mismos datos de 5 minutos. El área amarilla detrás de las curvas es la irradiancia de la estación (eje derecho), así se ve de inmediato qué tan soleado estaba el momento.

Las flechas y la fecha arriba a la derecha de la tarjeta pasan al día anterior o siguiente (o a cualquier día) y vuelven a esta tarjeta; la vista y el inversor elegidos se mantienen. **Hoy** regresa al día en vivo.

### Planta

![Vista de planta: generación CA, diseño y consumo del sitio](epi_plant)

La potencia CA de la planta (azul), el diseño para el clima medido (punteado) y, donde hay un medidor de red validado, el consumo del sitio (naranja, sección [Consumo](#consumption)). Pase el cursor para ver los valores del momento.

![Al pasar el cursor: los valores de un paso de 5 minutos](epi_tooltip)

### Inversores

![Vista de inversores: W por kWp de cada inversor, su diseño y las caídas frente a sus pares](epi_inverters)

```formula
potencia específica [W/kWp] = suma de CD de las cadenas (I x V) / sus kWp
                              (sin muestra de cadenas: CA / {{c:model.INVERTER_EFFICIENCY}}, marcado "desde CA")
caída: real/diseño < {{pct:views.GAP_SHARE}} x mediana(real/diseño de los inversores de la planta)
       con diseño >= {{c:views.GAP_MIN_WKWP}} W/kWp, durante {{c:views.GAP_MIN_STEPS}} pasos o más
```

Juzgar contra los demás inversores (no solo contra el diseño) evita que una nube sobre la estación o la sombra de sol bajo en toda la planta marquen a cada inversor cada tarde. Un inversor marcado tiene un botón **Abrir ticket** con la caída ya llenada.

### Cadenas

![Vista de cadenas: todas las cadenas de un inversor, la banda de sus pares y las que salen de ella](epi_strings)

```formula
índice_s  = (W/kWp de la cadena s) / (su propio W/kWp de diseño)    (justo entre planos de techo)
banda     = {{pct:views.BAND_LO}} a {{pct:views.BAND_HI}} x índice mediano del inversor, por paso
fuera     = fuera de la banda {{c:views.OUT_MIN_STEPS}} pasos o más con G_POA >= {{c:views.OUT_MIN_GPOA}} W/m²
sin salida = índice medio < {{pct:views.ZERO_SHARE}} de la mediana con G_POA >= {{c:views.ZERO_GPOA}} W/m²
```

Las cadenas sin salida se dejan fuera de la mediana y la banda (una cadena muerta de cuatro movería la mediana cerca de 15 %). Cada cadena fuera de banda lleva su desviación mediana mientras estuvo fuera, su peor momento y, si está baja o muerta, un botón de ticket ya llenado.

### Semana y mes

![Vista de mes: EPI-600 diario por inversor sobre las bandas objetivo, barras de insolación y las cadenas por día](epi_month)

EPI-600 diario de la planta y de cada inversor sobre las bandas (verde 97 % o más, ámbar 93 a 97 %, rojo bajo 93 %), la insolación del día como barras amarillas (kWh/m², eje derecho) y un mapa cadenas x días. Las cifras de la semana y de 30 días son cocientes de sumas de los días mostrados. Una columna completa en rojo es toda la planta ese día (nubes sobre la estación, un sensor); una fila que sigue en rojo es la cadena.

### Consumo

![Vista de consumo: 30 días de solar consumida en sitio, importación y exportación, y el día por hora](epi_consumption)

Se muestra donde hay un medidor de red validado; la sección [Consumo](#consumption) explica cada cifra.

### EPI-600 por cadena, mes a la fecha

![EPI-600 por cadena e inversor, mes a la fecha: un punto por cadena sobre las bandas objetivo](epi_bystring)

## EPI-600 y Disponibilidad-150 {#epi}

```formula
EPI-600 = suma(Wh reales) / suma(Wh esperados)
          sobre exactamente las mismas muestras válidas de 5 minutos con G_POA >= 600 W/m²
EPI-150 = lo mismo con G_POA >= 150 W/m²
Disponibilidad-150 = ARRIBA / (ARRIBA + ABAJO) por inversor en los pasos con GHI de estación > 150 W/m²
disponibilidad de planta = promedio de los inversores ponderado por kWp
```

**Muestra válida** = irradiancia, temperatura de panel y ambiente de la estación presentes y aprobadas por los controles de calidad ([Datos](#data)); corriente y voltaje de la cadena presentes; la cadena energizada (las cadenas antes de su fecha de energización no existen); el inversor produciendo, es decir su CD al menos {{pct:model.PRODUCING_SHARE}} de su CD esperada. Un inversor que no produce sale del EPI y cuenta ABAJO para disponibilidad. Sin datos del inversor en un paso = FALTANTE: ni ARRIBA ni ABAJO.

**Bandas:** verde ≥ {{pct:model.GREEN}}, ámbar ≥ {{pct:model.AMBER}}, rojo debajo; arriba de {{pct:model.SUSPECT}} = sospechoso (el sensor o el modelo, no la planta); menos de {{c:model.MIN_VALID_SAMPLES}} muestras válidas = datos insuficientes.

**Mes a la fecha** = se suman los registros (Wh reales, Wh esperados, muestras) de cada día del mes y se divide una sola vez. Un día cerrado se calcula una vez, {{c:live.SETTLE_H}} horas después de terminar (en ese tiempo llegan las cargas tardías), y se guarda; cambiar la configuración o las reglas lo recalcula.

### En el reporte de planta: el titular

El titular de desempeño del reporte de planta es el **EPI-600 contra el diseño congelado, mes a la fecha** (cociente de sumas sobre los días cerrados del mes; verde ≥ 97 %, ámbar 93 a 97 %, rojo debajo). Junto a él: los **últimos 7 días** y el **último día**, para distinguir una pérdida en curso (suciedad) de una ya terminada, y el mes **sin días con eventos** - días con mantenimiento registrado o disponibilidad de planta debajo de 98 % (un paro) - nombrados por fecha. La tarjeta de PR se marca como cruda y su brecha dice que compara el PR normalizado por temperatura con la línea base limpia configurada.

### Huecos de comunicación

La energía nunca se inventa. En un hueco, el contador del día del inversor da la energía exacta, E = C(después) - C(antes); esa energía se reparte en los pasos de 5 minutos del hueco en proporción a la potencia esperada para la irradiancia medida (o a una forma de cielo despejado donde la estación es inválida). Cada paso queda marcado como medido, reconstruido o desconocido. Después de la última muestra del día no se sabe nada, salvo que exista el total diario del fabricante.

```formula
el contador subió >= 50 % de la energía esperada en el hueco  -> ARRIBA
el contador subió <=  5 %                                     -> ABAJO (probado)
en otro caso                                                  -> DESCONOCIDO
```

## Cadenas en la distribución de la planta {#strings}

![La tarjeta de distribución: cada punto numerado es una cadena, con el color de su corriente frente a sus vecinas](layout)

```formula
cociente_s(t) = I_s(t) / corriente mediana de sus pares en t   (pares: mismo módulo, mismo plano)
índice del día = suma de I_s en los pasos evaluados / suma de la mediana de pares en los mismos pasos
```

- Corriente, no potencia: las cadenas de un mismo plano reciben la misma luz, así que sus corrientes coinciden sin importar el número de módulos.
- Un paso se evalúa solo cuando la mediana de los pares es al menos {{c:layout.DIM_A}} A (amanecer, atardecer y nublado fuerte no se evalúan). Un hueco de datos de un inversor no puede bajar a sus cadenas.
- Clases: bien ≥ {{pct:layout.OK_RATIO}}; bajo {{pct:layout.LOW_RATIO}} a {{pct:layout.OK_RATIO}}; muy bajo debajo de {{pct:layout.LOW_RATIO}}; sin corriente = menos de {{c:layout.ZERO_A}} A mientras los pares llevan {{c:layout.DIM_A}} A.
- El normal propio de una cadena es la mediana de su índice diario en los últimos {{c:layout.NORMAL_DAYS}} días, así una cadena que siempre lee un poco menos (más corta, una fila con algo de sombra) no se marca; solo un cambio. Una cadena cuyo normal está bajo {{pct:layout.CHRONIC}} nunca se muestra como bien.
- Un plano con menos de {{c:layout.MIN_GROUP}} cadenas toma prestadas las cadenas de la planta del mismo módulo.
- Un punto rojo u oscuro tiene un botón de ticket; si un ticket abierto ya cubre esa entrada, el botón muestra el número del ticket.

### Verificación del registro (nocturna)

El registro de cadenas (Anexo A del diseño, la distribución y los ajustes de canal) debe seguir el cableado real. Cada noche se compara con la corriente de mediodía (11:00 a 13:59, mediana) de cada entrada del inversor, en los últimos {{c:register.DAYS}} días en que el inversor produjo (al menos la mitad de sus entradas registradas a {{c:register.LIVE_A}} A o más):

- **Cadena muerta** - una entrada registrada debajo de {{c:register.DEAD_A}} A en cada uno de esos días: se pierde energía, ADVERTENCIA (por correo).
- **Entrada no registrada** - una entrada con {{c:register.LIVE_A}} A o más que ninguna línea del registro nombra: el registro no sigue el campo. INFO, solo en el portal.

Ambas dicen "confirmar en sitio"; la bandera se cierra sola cuando registro y campo coinciden.

**Índice desde el último cambio.** Cuando cambia el conjunto de entradas activas de un inversor (cadenas reparadas o perdidas) y el nuevo conjunto se mantiene {{c:register.STABLE_DAYS}} mediodías soleados, la tabla de inversores muestra su índice desde ese día, contra sus pares en los mismos días, para que una falla reparada no lo siga penalizando.

## Consumo y cobertura solar {#consumption}

Se muestra solo para plantas cuyo medidor de red fue revisado (qué mide, su signo y el balance de energía contra los inversores).

```formula
potencia de red    > 0 = tomada de la red (importación), < 0 = enviada a la red (exportación)
consumo            = solar + importación - exportación
solar consumida    = solar - exportación
cobertura solar    = solar consumida en sitio / consumo
varios días        = suma(solar consumida) / suma(consumo)
```

- **La importación y exportación de un día** vienen del mejor registro que da el medidor: sus propios registros diarios; si no, los contadores totales (última lectura del día menos la última del día anterior); si no, la potencia de 5 minutos integrada (cada muestra se mantiene hasta la siguiente; huecos de más de 15 minutos no se integran).
- **Por hora:** la diferencia del contador total entre lecturas consecutivas cuando ambas tienen contador (exacta con lecturas de hasta 3 horas de separación); si no, la potencia integrada. La energía se asigna a la hora de la lectura anterior.
- **Curva en vivo:** la potencia del medidor en el eje de 5 minutos de la tarjeta, lineal entre lecturas de como máximo 20 minutos de separación; consumo = potencia CA de la planta + potencia de red.
- **Un día con más exportación que solar** (más de 2 % o 2 kWh) no puede ser un sitio detrás de un solo medidor: se muestra en gris, sin cobertura, y queda fuera de las sumas.
- **Cuando un fabricante niega el historial de 5 minutos**, una lectura en tiempo real por corrida (cada 15 minutos) mantiene el día actual; el historial se trae después y llena los huecos.
- **Validación** (20 de noviembre de 2026, datos reales): contadores contra potencia integrada dentro de cerca de 1 % por día; registros diarios contra potencia integrada dentro de 1 a 4 %; todos los días del mes balanceados. El recibo de CFE sigue siendo la referencia de facturación; esta vista es para operación.

## Salud térmica {#thermal}

![Salud térmica: temperatura pico, tiempo caliente, desviación frente a pares y ambiente, derrateo y su costo](thermal)

- **Bandas** sobre la temperatura interna (umbrales operativos de ARGIA, no límites de garantía): normal < 50, vigilancia 50 a 60, aviso 60 a 65, alta 65 a 70, crítica ≥ 70 °C (inspección inmediata ≥ 75 °C).
- **ΔT pares** = este inversor menos la mediana de los demás inversores de la planta: la señal más fuerte de un problema de enfriamiento (disipador, ventilador, espacio libre), porque el ambiente, la irradiancia y la carga son compartidos. Se cuenta solo con carga similar y estable: el inversor y los pares con que se compara han trabajado a {{pct:thermal.REF_MIN_SP}} de su nominal o más durante {{c:thermal.WARM_BUCKETS}} intervalos seguidos de 5 minutos (un inversor detenido o recién arrancado está frío y no es una referencia justa).
- **No se muestra calificación de enfriamiento** (buena / vigilar / mala) hasta validarla contra las temperaturas crudas; las columnas de evidencia se mantienen.
- **ΔT ambiente** = interna menos ambiente del sitio.
- **Derrateo sospechado** en un intervalo de 5 minutos: el inversor está a {{c:thermal.T_HOT}} °C o más, al menos {{c:thermal.DT_PEER_MIN}} °C más caliente que sus pares frescos, y produce al menos {{c:thermal.LOSS_MIN_PCT}} % menos por kW nominal que ellos (corregido por su propia relación en clima fresco); los pares deben trabajar a {{pct:thermal.REF_MIN_SP}} de su nominal o más. **kWh perdidos** = esa diferencia sumada.
- **Derrateo del fabricante** = minutos en que el propio inversor reportó un modo de derrateo por temperatura (donde el fabricante lo publica).
- Una alerta de temperatura es CRÍTICA solo con evidencia de producción perdida; si no, es un aviso o una bandera informativa.

## Pérdidas y su costo {#losses}

![Últimos 7 días: energía, esperado, kWh y MXN perdidos, y la causa principal](last7)

Energía esperada con la planta 100 % disponible, de dos formas independientes:

```formula
pares: E_esp = mediana de vecinas sanas a menos de {{c:losses.PEER_MAX_KM}} km de (kWh/kWp)
               x kWp de esta planta x la relación usual de esta planta con ellas
clima: E_esp = kWp_CD x H x factor_esperado  (escalado por la relación usual real/modelo de la planta)
```

Una vecina cuenta un día solo si produjo al menos {{pct:losses.HEALTHY_FRAC}} de su propia expectativa de clima. Se usa la cifra de pares cuando existe; la relación usual necesita {{c:losses.MIN_CALIB_DAYS}} días sanos compartidos y debe estar dentro de {{c:losses.CALIB_BOUNDS}}.

```formula
perdido = max(0, esperado - real), repartido en este orden, cada parte limitada a lo que queda:
  1 indisponibilidad  el esperado se reparte en los pasos de 5 minutos del día según la irradiancia;
                      los pasos en 0 W pierden su parte (los pasos sin datos se juzgan con el contador)
  2 sobrecalentamiento la pérdida por derrateo medida por el trabajo térmico
  3 bajo desempeño    el resto
```

- **Recuperación:** un contador diario que se congeló durante un hueco de datos se corrige con el salto del contador total de la noche siguiente antes de juzgar el día.
- **Tolerancia:** la parte estimada (bajo desempeño y pasos sin datos) cuenta solo desde {{pct:losses.UNDERPERF_TOL}} del esperado (dos veces la dispersión normal de día a día); 0 W y calor son medidos y siempre cuentan.
- **Excusado:** las ventanas de mantenimiento del cliente aprobadas se facturan como energía considerada, así que no son pérdida.
- **MXN:** la tarifa PPA del mes; las plantas CAPEX no tienen tarifa por kWh y muestran solo kWh.
- Pase el cursor sobre cualquier cifra de la página para ver cómo se calculó ese día.

![Pérdidas: el mes por planta, en kWh y MXN](losses_page)

## Conciliación y control de facturación {#reconciliation}

![Conciliación diaria: nuestro total de 5 minutos contra el contador del fabricante](recon)

| Control | Compara |
|---|---|
| 1 | suma de nuestra energía de 5 minutos vs suma de los contadores diarios del fabricante |
| 2 | suma de los contadores diarios del fabricante vs su contador mensual |
| 3 | diferencia del contador total en el mes (nuestras propias capturas nocturnas) |
| 4 | diferencia del contador total vs contador mensual del fabricante |

**Base de facturación**, de la más difícil de alterar a la menos: diferencia del contador total > contador mensual del fabricante > suma de contadores diarios del fabricante > suma de datos de 5 minutos. Un hueco de conexión entre las 13:00 y las 17:00 nunca puede reducir una factura: el contador sí registró esa energía.

| Estado | Día | Mes |
|---|---|---|
| PASA | dentro de {{c:recon.DAILY_PASS_PCT}} % | dentro de {{c:recon.MONTHLY_PASS_PCT}} % |
| REVISAR | dentro de {{c:recon.DAILY_REVIEW_PCT}} % | dentro de {{c:recon.MONTHLY_REVIEW_PCT}} % |
| FALLA | más allá | más allá |

- Un día está completo cuando llegó al menos {{c:recon.COMPLETENESS_MIN_PCT}} % de sus datos de 5 minutos; un día menos completo lo dice en su página.
- **Nunca bajar:** una corrección del fabricante solo llena o sube un día guardado; un valor por encima del fabricante se marca para una persona, nunca se reduce en silencio.
- **Reintentos:** la corrida nocturna regresa 14 días y vuelve a pedir al fabricante cada día que no pasó por un hueco de nuestro lado.
- **Los meses cerrados quedan fijos** después del cierre mensual; ninguna energía se mueve entre meses.

![Conciliación: cada planta-mes con su base y su estado](recon_month)

## Alertas, correos y tickets {#alerts}

**Dos niveles.** El nivel **agudo** corre cada 30 minutos de día y solo usa condiciones que una muestra puede probar (un código de falla, toda la planta en 0 W con sus contadores detenidos, un inversor muy caliente, un inversor en 0 W 30 minutos o más junto a un hermano que produce y con su propio contador plano, datos más viejos que el límite de vigencia). El nivel **diario** corre a las 06:30 con cifras de día completo (energía vs esperado, inversor vs pares por kW nominal, plantas gemelas, térmico) y es el único que cierra alertas.

**Severidad**

| Severidad | Significado | Por correo |
|---|---|---|
| CRÍTICA | se está perdiendo energía ahora y los datos lo prueban | sí |
| AVISO | una pérdida medida o el aviso del propio equipo | sí, una vez |
| INFO | una bandera sin pérdida probada (posible problema, datos faltantes) | no, se muestra en el portal |

- Una alerta se envía una vez; los problemas que siguen abiertos se listan en el correo de la mañana; una alerta cubierta por un ticket de mantenimiento abierto no se repite.
- Una alerta de temperatura es CRÍTICA solo con evidencia de producción perdida; una bandera de cadena es aviso solo cuando los datos del día muestran una pérdida.
- Datos faltantes varios días seguidos se convierten en un correo de aviso; un día faltante es una posible pérdida, nunca una medida.
- Los umbrales (por ejemplo inversor bajo 85 % de sus pares = aviso, bajo 70 % = crítica) los puede configurar por planta un administrador.

![Alertas abiertas en la página de una planta](alerts)

**Tickets.** Desde las tarjetas de diagnóstico y de distribución se abre un ticket de mantenimiento ya llenado con planta, número de serie del inversor, entrada, la desviación medida y el día. Un ticket abierto se reconoce por planta, número de serie y la entrada que nombra su título ("input 4", "MPPT 2"); un ticket sin entrada cubre todo el inversor. Los tickets abiertos se envían por correo una vez por semana y cuando cambia su estado.

## Cómo agregamos, promediamos y estimamos {#rules}

| Cifra | En el tiempo | Entre equipos |
|---|---|---|
| Energía | suma de contadores del día | suma |
| PR (día) | cociente E / (kWp x H) | nivel planta |
| Tarjeta PR 30d | promedio de PR diarios (se excluyen días > {{c:mg.PR_MAX}}) | nivel planta |
| Disponibilidad | ARRIBA / (ARRIBA + ABAJO), ponderada por cobertura entre días | ponderada por kW nominal |
| EPI-600 / EPI-150 | cociente de sumas de Wh sobre las mismas muestras | cociente de sumas |
| Índice de cadena, distribución | cociente de sumas de corriente en los pasos evaluados | mediana de pares |
| Inversor vs pares | energía del día por kW nominal | mediana sin el propio inversor |
| Cobertura solar | cociente de sumas de kWh | planta |
| Pérdidas | suma de pérdidas diarias | planta |
| Irradiación | integral trapezoidal | un sensor por planta |

**Medianas vs promedios.** Las comparaciones entre equipos usan la mediana (un equipo descompuesto no mueve la referencia). Los totales usan sumas. Los cocientes de un periodo son cocientes de sumas.

**Redondeo.** Los cálculos usan valores sin redondear; las páginas redondean para mostrar (kWh a 0 o 1 decimal, cocientes a 0.1 %).

**Datos faltantes.** No son cero ni se interpolan de otros días o plantas. Los contadores prueban la energía a través de los huecos; lo que nada prueba se muestra como desconocido y se reporta su proporción (cobertura, completitud).

**Las estimaciones llevan su nombre.** Los valores esperados, de diseño, teóricos, reconstruidos y considerados se nombran así dondequiera que aparezcan.

**Límites conocidos**

- Los sensores de irradiancia de las estaciones aún no están calibrados contra una referencia; las bandas EPI son provisionales. Las comparaciones dentro de una planta (inversor con inversor, cadena con cadena) usan el mismo sensor y son confiables.
- Una planta sin sensor propio usa el sensor ARGIA más cercano; las nubes locales pueden diferir.
- La tarjeta de PR de 30 días promedia cocientes diarios (ver [KPI](#kpis)).

## Conectar una planta nueva: requisitos de API {#integration}

La herramienta de ARGIA funciona con inversores, dataloggers y medidores de cualquier marca que ofrezca una interfaz de datos documentada. Esto es lo que necesitamos, en orden de importancia.

### Requerido

| Dato | Resolución | Notas |
|---|---|---|
| Potencia CA del inversor | 5 min (se acepta 15 min) | por inversor, con la hora de la medición y su zona horaria |
| Contador de energía del día del inversor | mismas muestras | kWh desde la medianoche local |
| Contador de energía total del inversor | al menos diario | kWh, nunca se reinicia |
| Estado y códigos de falla/alarma del inversor | mismas muestras | con la lista de códigos o su documentación |
| Energía diaria, mensual y total de la planta | diaria | los contadores de la propia plataforma (control de facturación) |
| Historial | historial de 5 minutos de al menos 7 días (mejor 30 o más) | para llenar huecos tras una caída |
| Lista de equipos | bajo pedido | números de serie, modelo, potencia nominal |

### Muy recomendado

| Dato | Resolución | Para |
|---|---|---|
| Corriente por cadena (A) y corriente y voltaje por MPPT | 5 min | monitoreo de cadenas, EPI-600 |
| Irradiancia en el plano de los módulos u horizontal (W/m²), temperatura de módulo y ambiente | 5 min | PR, EPI-600, energía esperada |
| Temperatura interna del inversor y modo de derrateo | 5 min | salud térmica |
| Lista de alarmas con hora de inicio y fin | 15 min | alarmas del fabricante |

### Para consumo y cobertura

| Dato | Resolución |
|---|---|
| Medidor de red en el punto de conexión: potencia activa con signo, o potencias de importación y exportación | 5 min |
| Contadores totales de energía importada y exportada (o registros diarios) | mismas muestras |
| Historial | al menos 7 días |

### Interfaz

- **Aceptado:** una API en la nube del fabricante (REST/JSON, documentada), o un datalogger del sitio accesible por Modbus TCP / SunSpec mediante una pasarela que ARGIA pueda leer.
- **Autenticación:** una llave de API o cuenta de solo lectura. Nunca necesitamos acceso de escritura; ARGIA solo lee.
- **Límites de llamadas:** suficientes para una llamada por planta cada 5 minutos más una consulta diaria de historial; los límites deben estar publicados (ARGIA espacia sus llamadas y nunca inicia sesión más de lo necesario).
- **Marcas de tiempo:** hora de la medición, no de la carga; con zona horaria.
- **Estabilidad:** endpoints versionados; aviso antes de cambios.

### Integraciones probadas

Growatt (ShineServer web y API), Huawei FusionSolar (API Northbound), SolarEdge (API de monitoreo), SMA (datos por la API del fabricante). Otras plataformas se agregan con el mismo proceso: un colector, un intérprete probado con respuestas capturadas, y los mismos cálculos para todas las marcas.

### Pasos de incorporación

1. Acceso de solo lectura a la planta en la plataforma del fabricante.
2. Registro de la planta: kWp CD y CA, coordenadas, inversores con potencia nominal, cadenas por entrada (Anexo A), tipo de módulo, inclinación y azimut.
3. Reporte de diseño (HelioScope o PVsyst) para la energía esperada.
4. Primera semana: ARGIA revisa cada equipo contra el registro, los signos y los contadores, y la planta entra en vivo al portal.

## Glosario y preguntas {#glossary}

| Término | Significado |
|---|---|
| kWp CD | potencia de módulos instalada en condiciones estándar de prueba |
| H, irradiación | energía solar por m² en un día, kWh/m² |
| G_POA | irradiancia en el plano de los módulos, W/m² |
| PR | índice de desempeño, energía / (kWp x H) |
| EPI | índice de desempeño energético, real / esperado |
| Disponibilidad | proporción del tiempo diurno evaluado en que un inversor produjo |
| Cobertura | proporción del tiempo evaluado del que conocemos el estado |
| Contador del día, contador total | los registros de energía del propio inversor o medidor |
| Energía considerada | energía facturada por una interrupción aprobada del cliente |
| MPPT | la entrada del inversor que sigue el punto de máxima potencia de sus cadenas |
| Pares | equipos comparados entre sí: misma planta, o mismo módulo y plano |

**¿Por qué una planta muestra "sin datos" y no 0 %?** Porque no lo sabemos. Una conexión perdida no es una falla hasta que un contador lo prueba.

**¿Por qué la energía de hoy en la página en vivo puede diferir de la app del fabricante?** La cifra en vivo es la suma del último contador del día de cada inversor; la cifra de planta del fabricante puede ir una carga atrás o adelante. La conciliación compara ambas cada noche.

**¿Por qué falta el PR de un día en la tarjeta de 30 días?** Un PR mayor a {{c:mg.PR_MAX}} significa que el sol se midió de menos; ese día se excluye en lugar de inflar el promedio.

**¿Por qué una cadena se ve ámbar si el inversor se ve bien?** La cadena se compara con sus vecinas del mismo plano y con su propio normal. Una caída de 10 a 25 % en una cadena casi no mueve el total del inversor, pero es justo lo que el equipo de O&M debe revisar.

**¿A quién le pregunto?** Use Pregunte a ARGIA en el encabezado para preguntas sobre los datos, o contacte a su equipo de O&M de ARGIA.
