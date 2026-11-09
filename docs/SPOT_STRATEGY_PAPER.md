# Estrategias LAB → Spot PAPER MEXC

Estado: seguimiento y operaciones **ficticias** sobre mercado actual. No usa
credenciales privadas ni envía órdenes a MEXC, DEX o cartera real.

## Contrato operativo

- Una sesión fija versión LAB, huella de dataset público MEXC Spot/USDT,
  temporalidad, definición y versión del evaluador. Sólo acepta `LONG`;
  el short sin préstamo pertenece al laboratorio o a GRVT PAPER.
- Cada sesión empieza plana con el capital ficticio definido en su estrategia.
  Tiene caja, unidades y P/L independientes de las demás sesiones y de la
  Cartera PAPER manual. No existe una bolsa común entre estrategias.
- El dataset inicial debe terminar en la última vela cerrada y no tener huecos.
  El observador local comprueba la siguiente vela tras su cierre, dentro de
  una ventana acotada (30–900 s según temporalidad). Fija el inicio y el fin
  de la consulta para recibir sólo velas cerradas. Coteja dos velas de
  solape y evalúa las mismas reglas de entrada y salida que LAB.
- Una entrada gasta `caja × asignación`, incluida la comisión, al ask público
  reciente más slippage. Se venden todas las unidades ficticias al bid menos
  slippage, descontando comisión. Aritmética `Decimal` de precisión 80;
  cada observación, cotización, regla y variación de saldo queda en SQLite.
- El valor de cierre mostrado no es caja + unidades × bid bruto. Calcula con
  `Decimal` la venta hipotética de las unidades restantes al bid guardado,
  menos slippage y comisión de salida: `caja + unidades × bid ×
  (1 − slippage) × (1 − comisión)`. El P/L abierto sería ese ingreso neto menos
  el coste de entrada, incluida su comisión. Si no hay cotización para una
  posición abierta, no se inventa un valor. Es una estimación a cotización
  potencialmente antigua, no un fill ni una garantía de liquidez.
- Con una posición abierta, el observador comprueba el bid público
  aproximadamente cada 5 segundos mientras la app local funciona. Si ese bid
  observado cruza stop u objetivo, cierra ficticiamente al bid menos slippage
  y comisión y guarda hora, cotización y motivo. El cierre de vela conserva
  también su evaluación de stop, objetivo y regla independiente.
- No se observan todos los ticks: un cruce entre sondeos, durante una caída de
  red o con la app apagada puede perderse. **No existe una orden protectora en
  MEXC** ni garantía de precio o liquidez. El diario muestra la salida intravela al registrarse.
  El gráfico marca el instante de la salida aunque su vela siga abierta. Las comprobaciones sin disparo sólo conservan
  la cotización más reciente, no un archivo de todos los bids consultados.
- Si una posición pasa más de 30 s sin comprobación, la sesión entra en `GAP`
  y guarda el intervalo no observado. No ejecuta una salida ficticia usando el
  precio recuperado ni vuelve a activar la estrategia por sí sola; conserva
  unidades para revisión y cierre manual con bid fresco. El estado persiste
  tras reiniciar. El corte es una regla local de calidad, no una garantía de
  que todos los movimientos entre sondeos de 5 s hayan sido capturados.
- Si faltan velas, se revisan las previas o vence la ventana, la sesión entra
  en `GAP` y no reconstruye compras ni ventas. Puede quedar una posición
  ficticia abierta. El cierre manual pide un bid nuevo y queda registrado;
  repetirlo no vende dos veces.
- La base `ORBIS_SPOT_STRATEGY_PATH` (por defecto
  `~/.local/share/orbis/spot-strategy-paper.db`) sólo contiene dinero ficticio,
  reglas y datos públicos. Es SQLite local con permisos `0600`; admite
  migración **optativa** a SQLCipher, pero una instalación sin migrar y sus
  copias antiguas permanecen sin cifrar. Nunca deben guardarse secretos en
  ella. La bóveda REAL permanece separada.

## API y prueba

`/paper/spot/strategies`: crear/listar sesiones; `/{id}` detalle;
`/{id}/poll` comprobar, `/{id}/stop` detener y `/{id}/close` cerrar
ficticiamente una posición; `GET /{id}/verify` comprueba bajo demanda la
versión LAB, la huella del dataset, la semilla y la continuidad PAPER sin
modificar la sesión. Puede devolver origen divergente o no verificable. No
certifica precios posteriores ni ejecución real. Todas las rutas exigen origen local. Activar
requiere observador en marcha. `/{id}/chart` devuelve velas guardadas y
marcas de su misma sesión. La interfaz «Spot PAPER» muestra caja, unidades,
P/L realizado, último valor al bid, el diario y las marcas sobre las velas:
inicio de observación, señal, señal descartada por posición o capital, compra
PAPER y venta PAPER. La señal se fija en la vela evaluada y la flecha
en el segundo de ejecución y el precio simulado. Una ejecución anterior al
cierre de su vela se muestra sin OHLC. Las velas anteriores al inicio de
PAPER proceden de la semilla histórica.

«Reglas» abre las condiciones archivadas, incluidos los grupos Y/O.
«Ver ejecución» muestra la hora UTC archivada y el bid y el ask utilizados.
El inspector enlaza ambas vistas cuando la operación surge de una evaluación
de vela cerrada. Los cierres manuales e intravela muestran su propio motivo.
La API conserva `time` como inicio del intervalo para los clientes existentes
y expone `execution_time` en segundos y `observed_at_ns_exact` como texto exacto.

Pruebas: operación de compra/venta decimal, persistencia tras reinicio,
idempotencia y concurrencia, reversión conjunta de evento y saldo ante fallo,
rechazo de SHORT y duplicados, huecos, origen HTTP y recorrido real de
navegador con mercado controlado y sin red externa.

## Comparar reserva LAB

Guarda una validación cronológica o por ventanas sucesivas en Laboratorio y
selecciona **Spot PAPER → Comparar reserva LAB** en la sesión. Deben coincidir
versión, mercado MEXC Spot, temporalidad y motor. La reserva usa datos
anteriores a la activación y todas las velas comunes con la semilla PAPER
deben coincidir. La vista indica si la validación se guardó después de iniciar
PAPER. Cada ventana de LAB conserva su capital inicial y su cierre propio.

ORBIS reproduce la validación y coteja la semilla, señales y diario de PAPER
sin consultar el mercado. Comprueba los precios, costes y efectivo contra
el bid o ask archivado según cada acción. El corte de PAPER llega al último
evento archivado. Si la posición sigue abierta, el capital final y la
rentabilidad se muestran al cerrarla. La tabla conserva el P/L realizado
y las comisiones ya registradas.

LAB y PAPER conservan su periodo, exposición y modelo de ejecución. La
tabla presenta las métricas de cada tramo por separado y señala muestras
pequeñas y huecos de PAPER. El informe carece de autoridad para operar.

**Exportar JSONL** guarda el diario verificado de la sesión para investigación. Cada descarga pide confirmar el archivo local en claro. [Formato y alcance](IA_Y_ALMACENAMIENTO.md#diario-spot-paper-jsonl).

## Límites y trabajo pendiente

- La sesión se detiene al llegar a 5000 velas archivadas, incluida la semilla.
  El detalle muestra las velas restantes y avisa en las últimas 500; no
  transfiere capital ni posiciones a otra sesión. La continuidad de larga
  duración requiere el diseño de [PAPER_CONTINUITY.md](PAPER_CONTINUITY.md).
- No hay ejecución spot real, wallet, stop garantizado ni simulación de liquidez
  o impacto de mercado. Cada sesión tiene presupuesto propio; no se suman sus
  saldos como patrimonio.
- Comparar con LAB exige normalizar periodo, exposición y ejecución. El
  backtest entra en la apertura siguiente; PAPER observa una cotización
  después del cierre. Una divergencia no debe ocultarse ni presentarse como
  prueba de superioridad.
- Se comprobaron tres cierres consecutivos en BTC/USDT y BNB/USDT de un
  minuto con MEXC público. El ensayo usó fondos ficticios y evaluó señales
  reproducibles. Quedan ensayos de mayor duración y el piloto real.
