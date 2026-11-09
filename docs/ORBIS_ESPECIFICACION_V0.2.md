# Orbis — especificación funcional y técnica v0.2

**Estado:** dirección de producto aprobada para orientar el desarrollo; las funciones descritas no se presumen implementadas.  
**Fecha de incorporación:** 13 de octubre de 2026.  
**Origen:** especificación v0.2 proporcionada por el propietario del proyecto.

Este es el documento maestro del **producto objetivo**. El estado comprobado del
repositorio y la secuencia para llegar a él se encuentran en
[PLAN_TRANSICION_V0.2.md](PLAN_TRANSICION_V0.2.md). Los documentos de
`Qué_es/` reflejan la visión y el plan anteriores: conservan valor histórico,
pero en caso de conflicto de alcance o prioridad prevalece esta v0.2. Los
requisitos no funcionales anteriores continúan vigentes cuando sean compatibles.
Las tecnologías de §12 son candidatas, no una decisión de migración ya tomada.

## 1. Propósito y límites

Orbis será una plataforma personal y local de inversión, investigación de
mercados, gestión patrimonial y experimentación cuantitativa, inicialmente
centrada en criptomonedas. Debe conectar el análisis de una estructura de
mercado con la formulación de reglas, su prueba histórica, el examen de casos
individuales, el seguimiento en simulación actual y el registro del patrimonio.
La ejecución real automatizada queda para una fase opcional posterior.

La interfaz de escritorio se inspirará en terminales de mercado como MEXC y
TradingView: oscura, profesional, densa y configurable. Cada control visible
deberá tener una función real. La arquitectura permitirá añadir módulos sin
reconstruir la aplicación.

## 2. Tres entornos y reglas de separación

| Entorno | Datos y capital | Conocimiento temporal | Resultado |
| --- | --- | --- | --- |
| **Laboratorio** | Datos históricos y fondos ficticios; admite hipótesis teóricas imposibles de ejecutar | Se reproduce una ventana pasada bajo reglas explícitas | Simulación histórica, nunca rentabilidad real |
| **Paper Trading** | Fondos ficticios y mercado actual | La decisión sólo conoce observaciones disponibles en ese momento | Resultado simulado en seguimiento, distinto del backtest |
| **Cartera Real** | Operaciones y patrimonio introducidos manualmente al principio | Hechos registrados por el usuario y precios de referencia identificados | Patrimonio y rendimiento registrado, sin ejecución automática inicial |

Capitales, operaciones, métricas y consultas estarán separados por entorno y
cuenta. Los tres podrán compartir gráficos, precios públicos, indicadores y
herramientas estadísticas. Un resultado de laboratorio no se convertirá en
operación PAPER ni REAL al copiar una estrategia; cada activación requerirá una
acción explícita y conservará el vínculo con su versión de origen. La interfaz
mostrará siempre el entorno y la naturaleza observada, reconstruida o teórica
de cada cifra. Ninguna estrategia pasará automáticamente de simulación a
operación real.

## 3. Principios de producto

- **Modularidad:** proveedores, indicadores, reglas, gráficos y servicios se
  amplían sin alterar la semántica de los módulos existentes.
- **Control:** parámetros configurables con valores iniciales razonables y
  advertencias cuando una configuración sea costosa o incoherente.
- **Trazabilidad:** conservar entradas, condiciones, costes, datos, versión de
  reglas y motivos de cada cálculo y decisión.
- **Privacidad local:** no exigir cuenta central, telemetría de patrimonio ni
  direcciones completas de billeteras para registrar activos.
- **Reproducibilidad:** repetir una prueba con la misma estrategia, versión de
  datos y configuración de motor debe producir el mismo resultado.
- **Separación de resultados:** distinguir flujos de caja, cambio de valor,
  beneficio realizado, beneficio no realizado y resultados hipotéticos.
- **Fallos explícitos:** datos ausentes, estados ambiguos o cálculos inciertos
  se etiquetan; ninguna pantalla presenta estimaciones como hechos confirmados.

## 4. Interfaz y navegación

La primera versión se orienta a escritorio y pantallas grandes. La barra
superior mostrará identidad y estado de Orbis, búsqueda global de activos y
comandos, mercado, temporalidad, selector de entorno, conexión, frescura de
datos y acceso a configuración, privacidad y bloqueo. El menú permitirá ir a
Inicio, Mercados, Laboratorio, Backtesting y estadísticas, Paper Trading,
Automatizaciones y DCA, Cartera, Historial, Alertas y Configuración.

El espacio principal combinará gráficos, tablas filtrables y paneles
redimensionables, movibles y ocultables. Se podrán guardar espacios de trabajo
como «Trading», «Investigación» o «Cartera». La búsqueda global aceptará
acciones como «BTC», «Nueva estrategia» o «Registrar compra». Preferencias y
disposición persistirán localmente; un control ocultará importes sensibles al
instante. El tema oscuro será el principal, sin impedir temas posteriores.

### Inicio

El dashboard reunirá patrimonio y evolución, rendimiento realizado/no
realizado, distribución por activo, estrategias y pruebas recientes, DCA y
presupuestos, actividad, alertas y watchlist. Permitirá elegir divisa principal,
periodo y disposición de paneles. Las aportaciones y retiradas se mostrarán
como flujos de caja, no como beneficio o pérdida.

## 5. Mercados y análisis

Los gráficos admitirán velas, línea, área y Heikin-Ashi; temporalidades de
minutos a meses según el proveedor; zoom, desplazamiento, cursor y medición.
Podrán mostrar dibujos (líneas, soportes, resistencias, canales, Fibonacci,
anotaciones), indicadores, señales, entradas, stops, objetivos y cierres. Los
análisis se guardarán y recuperarán por contexto de mercado.

La selección inicial de indicadores comprende RSI, MACD, SMA, EMA, bandas de
Bollinger, ATR, volumen y volumen relativo. Se podrán añadir indicadores
propios, combinaciones y condiciones de varias temporalidades. La detección
de estructuras cubrirá máximos/mínimos ascendentes o descendentes, rupturas,
cruces de medias, patrones de velas, divergencias, cambios de tendencia y
volumen anómalo. El usuario podrá definir estructuras mediante reglas.

Cada serie mostrará proveedor, fecha de actualización, cobertura histórica,
lagunas, errores detectados y estado de la vela abierta. Watchlist y alertas
forman parte del flujo de análisis. Profundidad, libro de órdenes y últimas
operaciones quedan para ampliaciones cuando el proveedor las ofrezca.

## 6. Laboratorio de estrategias

### Constructor y definición

Un constructor visual permitirá crear reglas con **Y**, **O** y grupos
anidados, sin programar. Una estrategia podrá combinar indicadores, precio,
volumen, estructuras y varias temporalidades. Su definición incluirá:

- Identidad, descripción, mercados, activos, temporalidades y versión.
- Dirección long/short, capital inicial, tamaño de operación, exposición y
  número máximo de posiciones simultáneas.
- Apalancamiento realista o explícitamente hipotético, tipo de orden,
  condiciones de entrada, invalidación y salida independientes.
- Stop loss, take profit, trailing stop, cierres parciales y permanencia máxima.
- Comisiones, financiación, deslizamiento y restricciones del mercado.

Ejemplo de definición: BTC/USDT en 4 horas, long si RSI(14) < 30, precio >
EMA(200) y volumen actual > media de 20 velas; salida con objetivo +8 %,
stop −3 % u otra condición. Es un ejemplo de reglas, no una recomendación ni
un resultado medido.

La estrategia se podrá editar, duplicar, comparar, archivar y exportar. Cada
edición creará una versión inmutable para nuevas pruebas. Los backtests
anteriores conservarán su versión exacta.

### Modos de simulación

El modo **realista** considerará costes, tamaño, liquidez, slippage,
financiación, margen y liquidación cuando existan datos suficientes; indicará
qué elementos son aproximaciones. El modo **experimental** permitirá hipótesis
no ejecutables y mostrará inequívocamente que sus resultados son teóricos.

## 7. Backtesting e investigación

Una ejecución obtendrá datos con procedencia, calculará indicadores, detectará
señales, simulará operaciones y posiciones, calculará resultados, guardará
eventos y permitirá inspección y reproducción. Cada `backtest_run` conservará
la versión de estrategia, periodo, fuente y versión o huella de datos,
configuración del motor, supuestos de costes y resultado.

El dataset guardará **cada aparición** de una estructura, no sólo el total de
rentabilidad. Por evento incluirá identificadores de estrategia, versión y
prueba; activo, mercado y temporalidad; instante, estructura, condiciones e
indicadores; precio y volatilidad de referencia; resultados posteriores por
horizonte cuando haya cobertura; excursión favorable/adversa máxima (MFE/MAE),
tiempo hasta extremos, salida, costes y resultado neto. Las cifras del ejemplo
original (RSI 32,4, volumen 1,8×, variación 24 h +2,3 %) son ficticias.

Las estadísticas incluirán número de casos, win rate, beneficio neto, retorno
total y medio, expectativa, profit factor, relación beneficio/riesgo, máximo
drawdown, volatilidad, exposición, duración, rachas y segmentación por mercado,
activo, temporalidad y régimen. La rentabilidad anualizada sólo se calculará
cuando sea pertinente. Las vistas previstas son curva de capital, drawdown,
histograma, mapa de parámetros, dispersión, rachas y operaciones sobre el
gráfico original.

Se podrán comparar reglas y variaciones de parámetros. La optimización
deberá distinguir desarrollo y validación, incluir pruebas fuera de muestra y
análisis de sensibilidad; walk-forward llegará después. Se advertirá sobre
muestras pequeñas, inestabilidad y selección del mejor parámetro a posteriori.

El motor evitará información futura en la decisión. Registrará huecos,
velas incompletas y límites de ejecución. Cuando stop y objetivo se alcancen
dentro de una misma vela y no exista orden intravela conocido, aplicará una
política explícita o declarará el caso ambiguo. Distinguirá rentabilidad
teórica de potencialmente ejecutable. También permitirá investigar preguntas
sin estrategia cerrada, por ejemplo el comportamiento posterior a tres velas
bajistas con volumen creciente, y convertir hallazgos en reglas versionadas.

## 8. Paper Trading

Paper Trading utilizará precios y eventos que llegan en el mercado actual,
sin conocer datos futuros. Dispondrá de cuentas y capital ficticios,
asignaciones por estrategia, señales, órdenes y posiciones simuladas, costes,
motivos, diario de errores y estadísticas por periodo. Comparará lo observado
tras la activación con resultados históricos, manteniendo ambas series aparte.

Una estrategia LONG/SHORT destinada a GRVT deberá poder seguirse primero en
PAPER de ORBIS con dinero ficticio, vinculada a la misma versión de reglas y
al mercado/instrumento concreto. Para derivados, PAPER incluirá margen,
apalancamiento, financiación, liquidación y límites de posición; mientras
falte alguno se identificará como experimento incompleto y no se presentará
como validación para activar GRVT real. El entorno de pruebas de GRVT podrá
usarse además para verificar el adaptador, pero no sustituye al PAPER local.

Ante apagado o pérdida de datos registrará intervalos sin observación. Si se
rellenan desde un histórico posterior, los eventos resultantes se marcarán
como **reconstruidos**, nunca como órdenes observadas en directo.

## 9. Automatizaciones y DCA

Se podrán definir DCA periódico, compras por caída, reglas basadas en SMA/EMA,
RSI, volumen, volatilidad o estructuras y compras escalonadas. Cada regla
tendrá presupuesto, capital reservado, exposición máxima, número máximo de
activaciones, espera, activación e invalidación. Un simulador comparará DCA
fijo, DCA condicionado y compra única: aportaciones, unidades, coste medio,
valor, resultado, drawdown, calendario y comisiones.

La primera versión funcional permitirá definir la regla, probarla
históricamente, compararla con compra fija y activarla sólo en modo ficticio.
La ejecución con dinero real requeriría una fase separada con autorización
explícita, conexión segura, límites, saldo, idempotencia, auditoría y parada.

## 10. Cartera Real manual

La cartera registrará activos, cantidades, coste, precio medio informativo,
precio de referencia, valor de mercado, resultados, historial y notas. Una
compra o venta incluirá activo, fecha y hora, cantidad, precio, divisa de
pago, comisión y nota opcional. Se admitirán ajustes, recompensas y clases
posteriores mediante tipos explícitos. Una transferencia entre billeteras
propias podrá anotarse de forma abstracta y no se interpretará por sí sola
como compra o venta.

Las posiciones distinguirán coste de adquisición, beneficio realizado y no
realizado, cambio de precio y flujos de caja. El método de asignación de coste
para informes deberá quedar identificado y versionado; ningún cálculo fiscal
se presentará como definitivo sin revisión específica. Se prevén diario,
CSV de importación/exportación, detección de duplicados, filtros, varias
divisas y, más adelante, staking, recompensas, dividendos y swaps.

### Billeteras conectadas (ampliación optativa)

ORBIS deberá poder conectar billeteras de autocustodia como MetaMask y Trust
Wallet para consultar y, en una fase posterior, utilizar los activos que
contienen. La conexión no sustituirá a la cartera manual: ambas serán fuentes
distintas y el usuario vinculará explícitamente las posiciones coincidentes
para evitar doble cómputo. Se identificará siempre red, activo nativo o
contrato, cantidad, bloque/hora de consulta y procedencia; un ticker no basta
para identificar un token.

La evolución tendrá capacidades separadas de **lectura de saldos y
movimientos**, **activación de estrategia REAL** y **ejecución/retirada**.
El objetivo es ejecución automática dentro de límites y con parada, tras
autorizar expresamente una versión, mercado, cuenta, activos y presupuesto.
Conectar una wallet o ver su saldo no autoriza a gastar. Para operar sin
confirmación por transacción, la wallet deberá ofrecer una delegación
revocable y técnicamente acotada; si no existe, esa etapa requerirá firma
manual y se mostrará como tal. Ni la IA ni una señal aislada podrán ampliar
permisos. Las claves principales y frases semilla permanecerán fuera de ORBIS.

El capital podrá empezar en la wallet, depositarse directamente en MEXC o
GRVT, o estar ya en cualquiera de esos venues. ORBIS registrará depósitos,
saldos previos, órdenes,
ejecuciones parciales, comisiones, retiros y movimientos de regreso a la
wallet sin convertir transferencias propias en beneficio. Primer alcance
previsto: MEXC Spot, GRVT como integración separada sujeta a su modelo de
cuentas/margen, y un DEX relevante (Uniswap como candidato EVM), no veinte
proveedores. La posibilidad de obtener recompensas o airdrops de un venue no
se considerará rentabilidad garantizada ni justificará operaciones ficticias.
El diseño y las puertas de seguridad están en
[EJECUCION_REAL.md](EJECUCION_REAL.md).

La conexión funcionará sin guardar direcciones completas por defecto. Una
sincronización persistente sólo podrá guardar la dirección pública cifrada en
la bóveda REAL si el usuario lo autoriza; alternativamente se podrá usar sólo
durante la sesión. Consultar la cadena revelará esa dirección al nodo o
proveedor externo. Desconexión, revocación, cambios de cuenta/red y lagunas
de sincronización tendrán estados visibles. Esta ampliación no pertenece a
las fases 1–3 y sus controles no aparecerán como operativos antes de existir.

## 11. Privacidad y seguridad

La cartera manual no exigirá conocer dónde se guardan los activos. Se podrán
usar alias locales y fragmentos opcionales de identificador; no se guardarán
por defecto direcciones completas, destinos, semillas, claves privadas ni
datos innecesarios de custodios. La sincronización on-chain será una función
independiente y optativa: consultar un saldo exige revelar una dirección
pública al nodo o proveedor, aunque sólo se use temporalmente.

La aplicación objetivo tendrá datos locales cifrados en reposo, contraseña
principal y bloqueo automático, backups cifrados, importación con descarte de
columnas sensibles, diagnósticos sanitizados, ausencia de telemetría
patrimonial por defecto y ninguna nube obligatoria. El cifrado en reposo no
protege una sesión desbloqueada ni un sistema operativo comprometido.

## 12. Arquitectura objetivo y decisiones pendientes

Se propone una aplicación local de escritorio con React + TypeScript, Tauri,
Tailwind CSS y shadcn/ui para interfaz, Lightweight Charts para gráficos,
SQLite cifrado para almacenamiento, CCXT o adaptadores específicos para datos
públicos, un motor inicial TypeScript y Python opcional para análisis pesado;
Vitest y Playwright servirían para pruebas. **Esta lista es orientativa:**
compatibilidad, coste de migración y responsabilidades de cada proceso se
validarán antes de elegirla. El backend Python/FastAPI y los adaptadores
propios existentes son activos del proyecto, no componentes descartados por
esta propuesta.

Capas: interfaz → servicios y procesos → motores de reglas, análisis y
cartera → persistencia y caché → adaptadores externos. Módulos previstos:
`market-data`, `charts`, `indicators`, `strategies`, `backtesting`,
`paper-trading`, `analytics`, `portfolio`, `automation`, `alerts`, `storage`
y `security`. Backtesting y Paper Trading compartirán la interpretación de
las reglas; sus motores de ejecución podrán diferir, con semántica y límites
documentados.

Entidades iniciales propuestas: `assets`, `markets`, `candles`, `strategies`,
`strategy_versions`, `backtest_runs`, `detected_events`, `simulated_trades`,
`paper_accounts`, `paper_positions`, `portfolio_events`, `automation_rules`,
`automation_events` y `settings`. Son un modelo conceptual, no un esquema SQL
aprobado. La estrategia, el dataset y el motor deberán tener identificadores
de versión o huellas reproducibles; cada registro conservará entorno y
procedencia.

## 13. Flujo entre módulos

Investigar estructura → formalizar reglas → ejecutar backtest → inspeccionar
casos y estadísticas → guardar estrategia y dataset → activar una versión en
PAPER → comparar seguimiento e histórico → crear automatización ficticia.
Una conexión posterior con operaciones reales exigiría un proceso y una
autorización propios; los hechos reales entrarían en Cartera Real. Cada paso
conservará referencia al registro de origen, sin fusionar sus resultados.

## 14. Fases de producto

1. **Fundamentos y Cartera Local:** navegación, gráficos e histórico,
   almacenamiento protegido, compras/ventas manuales, posiciones,
   estadísticas y dashboard. Resultado: seguimiento patrimonial y análisis
   básico útiles.
2. **Laboratorio y Backtesting:** constructor long/short, reglas e
   indicadores, simulación histórica, eventos, estadísticas, gráficos,
   versiones y comparación. Resultado: hipótesis reproducibles.
3. **Paper Trading y DCA simulado:** cuentas ficticias, señales en mercado
   actual, diario, DCA periódico/condicionado, presupuestos y comparativas.
   Resultado: observación sin capital real.
4. **Ampliaciones:** optimización, sensibilidad, más mercados, importaciones,
   riesgos e integraciones reales opcionales, incluidas billeteras, MEXC,
   GRVT y Uniswap, con controles adicionales.

El repositorio ya contiene parte de las fases 1 y 3; estos números expresan
prioridad de producto, no el orden cronológico de los commits anteriores.

## 15. Ampliaciones previstas

Detección automática de estructuras, correlación, regímenes de volatilidad,
comparadores de carteras, Monte Carlo, walk-forward, extensiones de
indicadores/proveedores, asistente de lenguaje natural para traducir ideas a
reglas, riesgo avanzado y otros instrumentos. No son requisitos de la primera
versión funcional.

## 16. Aceptación mínima

**Laboratorio:** crear long/short, elegir mercado y temporalidad, obtener o
cargar histórico, ejecutar prueba, ver entradas/salidas/estadísticas,
inspeccionar operaciones y recuperar estrategia, versión y resultados tras
reiniciar.

**Cartera:** registrar compras y ventas de varios activos, comprobar cantidad
y coste medio, consultar historial/comisiones/resultados y recuperarlo tras
reiniciar sin almacenar direcciones completas ni destinos.

**Automatizaciones:** definir DCA, simularlo históricamente, limitar presupuesto
y exposición, compararlo con compra fija y activarlo en modo ficticio.

**Calidad:** fórmulas y resultados verificables, errores de red y datos
incompletos explícitos, migraciones sin pérdida de historial, interfaz fluida
con históricos grandes y ausencia de controles decorativos.
