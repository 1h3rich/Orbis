# GRVT: ejecución automática PAPER por señal

Estado: funcional para simulación local; **no ejecuta órdenes GRVT reales**.

## Contrato

- La activación es optativa por cuenta, par, dataset público GRVT verificado y
  versión LAB inmutable. El modo predeterminado sigue siendo `OBSERVE`.
- La API rechaza `AUTO_PAPER` si los observadores locales de vela, ticker de
  riesgo y financiación no están en marcha. La cuenta detenida y un
  apalancamiento superior al límite tampoco se arman.
  Un objetivo SHORT de al menos el 100 % del precio se rechaza por inalcanzable.
- `AUTO_PAPER` exige cantidad de activo base y apalancamiento explícitos. Sólo
  puede haber una sesión automática activa por cuenta y par. El libro PAPER
  aplica los mismos límites de saldo, nocional, exposición, posiciones,
  especificación y margen que a una apertura manual.
- La regla se evalúa exclusivamente sobre la nueva vela TRADE cerrada y
  verificada en su ventana. Una entrada abre una posición ficticia al
  bid/ask público posterior más el slippage configurado. La salida por regla
  sigue decidiéndose al cierre de vela. Stop y objetivo se vigilan además con
  el `mark_price` del ticker público `v1.ticker.s` por WebSocket cada vez que
  llega un mensaje mientras ORBIS está abierto; el cierre PAPER usa el
  bid/ask **de ese mismo mensaje** más el slippage configurado. Si un cierre
  de vela llega antes que el tick de riesgo, prevalece la primera intención
  persistida en esa vela. No hay orden protectora colocada en GRVT ni garantía
  de precio, liquidez o ejecución real.
- La lectura de la siguiente vela exige las dos velas cerradas de solape y la
  nueva en su instante esperado. Si GRVT aún no entrega las tres, devuelve
  OHLC inválido o sólo velas antiguas, la API indica indisponibilidad temporal
  (`503`): no guarda observación ni acción PAPER y el trabajador reintenta
  dentro de la ventana. Al vencer ésta, la sesión entra en `GAP`; una revisión
  de velas previas también causa `GAP`.
- El feed se valida por instrumento, selector, hora de evento y bid/ask. Si
  pasan más de 20 s entre ticks observados (o desde la apertura), la sesión
  pasa a `GAP`; la posición ficticia **no** se cierra a un precio supuesto y
  queda disponible para cierre manual. Un reinicio con cobertura perdida no
  reconstruye supuestas activaciones intravela. `GAP` no equivale a un stop
  real ejecutado.
- Antes de simular un cierre por stop/objetivo, cada ticker compara la equity
  aislada —incluida la financiación **ya asentada**— con el mantenimiento
  calculado con reglas públicas refrescadas cada 30 s (caché válida como máximo
  60 s). Si llega al
  umbral, la sesión pasa a `STOPPED`, se invalidan cierres pendientes y la
  posición queda abierta para revisión manual. Se conserva un evento de
  riesgo con mark, equity, exigencia y cobertura de financiación. Esto **no**
  afirma que GRVT haya liquidado: la financiación aún no asentada y cambios
  posteriores de reglas alteran el resultado. Si las reglas públicas actuales
  faltan, son inválidas o caducan, se detiene igualmente sin inventar un fill.
- La valoración manual PAPER añade un **umbral teórico de mantenimiento**
  aislado cuando puede resolverlo dentro de los tramos publicados. Para cada
  tramo busca el mark positivo que satisface
  `colateral + (mark − entrada) × tamaño_firmado − comisión_entrada − funding_asentado
  = |tamaño| × mark × tasa_mantenimiento − importe_acumulado`.
  Sólo se presenta el primer cruce adverso, con brackets continuos y con la
  posición todavía por encima de mantenimiento. No es el precio de liquidación:
  no predice financiación futura, cambios de reglas, saltos del mark, precio de
  ejecución ni la decisión del contrato GRVT. Si no hay solución verificable,
  se omite en vez de mostrar cero. La [documentación oficial de GRVT](https://help.grvt.io/en/articles/10255070-what-s-the-liquidation-process-on-grvt)
  distingue el disparador de mantenimiento de la ejecución de liquidación.
- Las posiciones AUTO revisan financiación pública al abrir, como máximo cada
  hora mientras sigan abiertas, y al cerrarse. Se reutiliza el mismo cálculo
  exacto y asentamiento idempotente de PAPER manual. Los históricos ausentes o
  incoherentes mantienen `UNASSESSED`/`PARTIAL`; nunca se supone un coste cero.
  El P/L de una posición cerrada puede corregirse después de completar la
  cobertura. No hay liquidación simulada continua ni pagos reales.

## Persistencia y fallos

- Esquema PAPER v10: el archivo `paper_signal_candles` conserva cada vela
  observada junto al histórico de sesión; se migra desde v8 y la copia coteja
  archivo, observación e histórico. También se conserva una instantánea
  incremental parcial de EMA, MACD y ATR, cotejada con el evaluador completo;
  aún no decide señales ni retira el límite de 5000 velas.
  Cada señal que requiere operación
  crea una intención `PENDING` en la misma transacción que la observación.
  Tiene `request_id` y digest propios. La apertura/cierre, evento contable,
  respuesta idempotente, enlace de posición y transición a `EXECUTED` se
  confirman en **una sola transacción SQLite**.
- Tras un reinicio se reintenta únicamente mientras siga abierta la ventana
  de la vela correspondiente. Si vence, se marca `EXPIRED` y la sesión pasa a
  `GAP`; nunca se inventa una ejecución retroactiva. Un límite rechazado
  deja la intención `REJECTED` y detiene la sesión.
- Detener la sesión cancela entradas pendientes y no cierra una posición ya
  abierta. Detener la cuenta impide nuevas entradas, pero permite completar
  un cierre pendiente y hacer cierres manuales. Si una posición se cerró por
  fuera de la sesión, ésta se detiene al observar la siguiente vela.
- Si Orbis se apaga, se pierde una ventana o el proveedor falla, una posición
  ya abierta puede quedar abierta. La interfaz muestra el estado y permite
  cerrarla manualmente. El observador de riesgo publica estado de conexión y
  edad del último ticker; no se anuncia una protección siempre activa.
- El libro verifica dentro de la transacción la continuidad de los ticks y la
  vigencia de la intención. Para una salida decidida por vela, admite una
  cotización desde su cierre y antes del cierre de la siguiente. Un tick
  anterior puede actualizar el riesgo, y la salida espera otro precio. Si
  REST devuelve un precio superado por el WebSocket, la intención también
  sigue pendiente. Ambas vías comparten el identificador de solicitud y
  registran un solo evento. Si vence el plazo, la sesión pasa a `GAP` y la
  posición queda abierta para revisión manual.
- El observador de financiación consulta `FUNDING_INTERVAL` público, no pagos
  de una cuenta GRVT. Para una posición abierta corta la revisión 15 s antes
  de la hora actual, de modo que no se adelante a una cotización de cierre
  aún válida. El libro rechaza un cierre fechado antes de la cobertura ya
  contabilizada. Las posiciones AUTO abiertas y cerradas pendientes se
  recuperan de SQLite al reiniciar; las manuales no se asientan solas.

## Separaciones

- Las marcas de señal y las flechas de apertura/cierre PAPER son entidades
  distintas. Cada posición automática conserva `origin_session_id` y la
  versión y vela se recuperan desde su sesión/acción.
- `GET /paper/perp/signals/{session_id}/verify` contrasta bajo demanda el
  motor, versión LAB, huella del dataset, prefijo y continuidad histórica
  de la sesión. Un origen cambiado es divergente; uno inaccesible es no
  verificable. La comprobación no muta ni detiene PAPER y no valida las
  cotizaciones o fills posteriores.
- `GET /paper/perp/signals/{session_id}/comparison` usa como referencia sólo
  un run LAB guardado con la misma versión y huella del dataset. No lee todas
  sus operaciones para refrescar el panel; muestra sus métricas ya guardadas.
  El lado PAPER cuenta observaciones y acciones de la sesión, y suma con
  precisión decimal sólo el P/L realizado de sus posiciones cerradas.
  Ninguno de esos importes es una rentabilidad normalizada comparable: cambian
  periodo, capital, tamaño, modelo de ejecución y cobertura de financiación.
  Si falta el run exacto, se muestra como ausente, no se sustituye por otro.
- No se usan credenciales privadas, endpoints de órdenes, balances reales ni
  transferencias. Esto **no habilita** trading real ni autoriza usar las claves
  de solo lectura compartidas anteriormente.

## Pendiente para nivel profesional

- La sesión conserva hasta 5000 velas, incluida la semilla, y después se
  detiene en `CAPACITY` sin mover ni cerrar la posición ficticia. El detalle
  y la UI advierten de la capacidad restante. La continuidad exacta está
  diseñada en [PAPER_CONTINUITY.md](PAPER_CONTINUITY.md), aún incompleta.
- Supervisión prolongada del stream público, latencia bajo carga y modelado
  de fills y liquidez más fiel que el mejor bid/ask instantáneo.
- Liquidación realista, financiación completa y cierre controlado tras
  desconexión; la alarma sólo compara equity y mantenimiento con una instantánea
  pública reciente, no reconstruye la liquidación del exchange.
  conciliación con pagos privados de cuenta si más adelante se autoriza una
  conexión de sólo lectura, y ensayos prolongados del asentamiento AUTO.
- Ensayos PAPER prolongados y evaluación de desviación LAB→PAPER. Las
  integraciones privadas y órdenes reales necesitan proyecto de seguridad,
  permisos de trading nuevos, pruebas en testnet y aprobación separada.
