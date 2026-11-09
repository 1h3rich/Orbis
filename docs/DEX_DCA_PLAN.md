# DCA DEX: ejecución y continuidad

## Medias móviles

En PAPER DCA, selecciona una sesión verificada de BNB/USDT y pulsa «Preparar DEX». Selecciona la reserva y los límites antes de comprobar la preparación. Vincular el ejecutor, autorizar importes y activar la operadora siguen siendo pasos separados.

El filtro SMA y el ajuste SMA/EMA reutilizan el cálculo de LAB y PAPER. El importe se limita por asignación, mes, orden, exposición, saldo y la política firmada. La fracción inferior a la unidad mínima del token USDT se descarta. Con factor cero el diario guarda una espera. La EMA conserva el prefijo de cierres archivados incluso después de renovar la política. El recorrido de prueba usa una cadena EVM local. El piloto con una wallet real queda pendiente.

## Volumen relativo

Selecciona en PAPER DCA una sesión diaria de BNB/USDT con volumen relativo y pulsa **Preparar DEX**. Revisa la reserva histórica, la política y los límites antes de activar. MEXC aporta las velas cerradas de BNB/USDT para la señal. Uniswap v3 cotiza el intercambio de USDT por WBNB en BNB Chain.

La compra base y los extras por caída requieren que el volumen de la vela alcance el mínimo respecto al promedio de las velas cerradas previas. Si el volumen es insuficiente, el bot espera. Si cambia una vela de referencia, se detiene antes de cotizar o firmar. Las reglas de tamaño y los límites de presupuesto se mantienen. El diario y el JSONL conservan el periodo, el mínimo y el cociente exacto. El rearme es explícito y conserva el historial. La verificación de este flujo se hizo en una cadena local. El piloto con fondos reales sigue pendiente. [Velas de MEXC](https://www.mexc.com/api-docs/spot-v3/market-data-endpoints/klinecandlestick-data) · [Uniswap v3 en BNB Chain](https://developers.uniswap.org/docs/protocols/v3/deployments/v3-bnb-deployments).

## Corrección v80: recibos heredados y reloj de cadena

Un bloque finalizado puede estar unos segundos por delante del reloj local,
dentro de la tolerancia de 15 s que ya comprueba la observación. La validación
del permiso rearmado debe conservar los recibos conocidos por ese bloque.
La proyección final omitía la hora del bloque inicial al plegar esos recibos,
de modo que podía rechazar un rearme válido por una diferencia de milisegundos.

Ahora pasa `observed_chain_at=max(horizon, baseline.block_at)` al plegado de
hechos. La hora de decisión no cambia, la tolerancia sigue limitada a 15 s y el
gasto mensual continúa asignándose al instante real del recibo. No se habilitan
entradas históricas ni se modifica el formato de archivo DCA v1. La planificación
conserva su espera cuando el desfase cruza un periodo presupuestario.

La regresión HTTP fuerza 5 s de adelanto de cadena, detiene y rearma el bot,
comprueba gasto y claves consumidas, crea copia y recupera tras reabrir sin
autorun. El tiempo de cadena no retrocede. Falló antes del arreglo y aprobó
después, junto con 38 casos DCA de núcleo, API, renovación y cotización.
Caso: `test_dca_rearm_preserves_receipt_with_verified_chain_clock_ahead_of_local_clock`.
Evidencia en `/home/rich/.local/state/orbis/verification/v80-dca-diagnosis`.

## Implementación v75

Estado: v75 verificada y cargada localmente. Evaluador, diario causal,
trabajador, rearme, renovación de política, API e interfaz integrados.
Un trabajador DCA Uniswap requiere su propia asignación REAL finita,
operadora verificada, gas, política y activación explícita. Tener una regla
histórica, PAPER o MEXC no lo activa automáticamente.

## Recorrido que debe quedar operativo

Regla versionada → backtest reproducible → observación PAPER verificada →
asignación REAL finita → velas públicas entrantes → decisión DCA propia →
simulación del ejecutor → un envío → recibo finalizado → posición y gas propios.
La fuente probada se conserva como certificado; PAPER no decide compras REAL
ni aporta dinero, unidades, precio de entrada o consumo de presupuestos.

El evaluador debe reutilizar `dca_trigger_candidates` y las claves BASE/DIP
del motor existente. La ejecución DEX conserva sus importes enteros, gas BNB,
protecciones TWAP/cuotas y su propia salida de transacciones. No se convierte
el plan de órdenes MEXC en una llamada genérica firmable.

## Estado causal necesario

- Definición y versión fijadas, conjunto inicial de velas y referencia inicial
  verificable. Las velas posteriores provienen del mismo mercado público.
- Última vela comprobada, hora de observación y huecos. La recuperación rellena
  indicadores y marca el intervalo; no compra periodos pasados.
- Claves de activación consumidas, referencia máxima observada y última caída
  activada. Una decisión reservada consume su clave para evitar un segundo envío;
  una operación incierta se concilia con el mismo hash.
- Gasto total y mensual de recibos REAL confirmados. Los meses se asignan a la
  fecha UTC de ejecución; la revisión no desplaza gasto entre meses. El gas
  permanece en BNB, separado del presupuesto USDT.
- Posición y coste de compras DEX propias. Los saldos anteriores de la wallet
  no se adoptan como posición del bot. Los límites on-chain y mensuales se
  aplican simultáneamente.

Rearmar o renovar conserva referencia, claves, gasto mensual/total y gas de la
familia completa. No reinicia las caídas activadas ni el calendario. Se necesita
un formato DCA versionado, con cursor propio; no reinterpretar LONG v1–v4.

## Orden de implementación y prueba

1. Evaluador puro: DCA periódico y por caída con los candidatos compartidos.
   Casos de cruce de mes, varias caídas, referencia máxima, presupuesto residual,
   exposición, señal caducada y orden pendiente. Sin red ni firma.
2. Diario cifrado reproducible y recibos DEX. Una clave → un intento; conservar
   reservas, reversión, gas y resultado desconocido sin inventar una compra.
3. Trabajador, permisos y API explícita. Reutilizar observación pública, conexión,
   outbox y STOP ya verificados. Revalidar generación después de cada RPC.
4. Rearme y renovación conservando consumo y claves. Prueba que una respuesta
   perdida, dos pestañas o una copia/reapertura no repiten la compra.
5. Interfaz y navegador: comprar BASE y DIP en EVM desechable propia, comprobar
   recibos e importes, bloquear y recuperar sin autorun; retirar «pendiente»
   únicamente cuando ese flujo esté probado.

La validación humana de una configuración mainnet será independiente; estas
pruebas de desarrollo utilizan exclusivamente fondos y claves de EVM de prueba.

## Decisiones del evaluador v75

`dex_dca_plan.py` usa reglas nativas y `dca_trigger_candidates`, con cantidades
enteras de USDT/WBNB. Sus estructuras inmutables describen límites REAL, cursor,
intentos y cotización simulada. No tiene wallet, repositorio, RPC o emisor.
El adaptador del diario deberá construir los intentos desde decisiones y recibos
verificados; esta función pura no autentica objetos que reciba del llamador.

El gasto USDT procede sólo de compras FINALIZED y se asigna al mes UTC del
recibo, no al mes de preparación o confirmación en la interfaz. El gas de los
intentos se conserva en wei y no resta USDT. La concesión del operador mantiene
además su presupuesto completo, incluido STOP; no sumar dos veces ambos datos.
PENDING bloquea nuevas compras; REVERTED consume su clave y gas, sin inventar
un gasto de tokens. Sólo ABANDONED_UNSENT acreditado libera una clave nunca enviada.

El contrato fijado exige al menos 60 segundos entre compras. Una señal DCA diaria
puede activar BASE y varios DIP durante una ventana REAL configurable de 60 a
86.400 segundos, inicialmente **900 segundos** desde el cierre. Así respeta la
espera del contrato sin convertir la segunda compra en una señal caducada.
La ventana no amplía la frescura de precios: referencia de hasta 15 segundos,
observación continua de hasta 30 y cotización del ejecutor de hasta 15 segundos.
La ejecución también debe vencer antes de terminar esa ventana, la política y
el mes. No se admiten compras retrospectivas de días anteriores.

Al rearmar, conservar la ventana fijada y las claves. Si la vela diaria sigue
siendo la misma que se observó antes de parar, puede continuar un nivel pendiente
dentro de su ventana con precio actual. Si se atravesaron cierres no observados,
el histórico repone indicadores y queda una barrera que impide comprar esos
días: no recuperar automáticamente compras mensuales o niveles pasados.

La exposición se dimensiona con el patrimonio REAL asignado y la referencia
actual, excluyendo unidades ajenas de la wallet. Antes de autorizar, la segunda
comprobación usa la salida de la simulación DEX del bloque fijado, que puede
diferir de la referencia MEXC. Una simulación no se presenta como fill ni como
garantía del precio posterior; los límites on-chain siguen vigentes y el recibo
aporta las cantidades finales. La precisión de estas cuentas es independiente
del contexto Decimal del proceso.

## Diario y trabajador internos v75

`dex_dca_journal.py` conserva el formato propio `orbis.dex.bot.dca.v1` en las
tablas cifradas existentes, sin migración ni reinterpretación de LONG v1–v4.
Cada decisión guarda testigos de los intentos anteriores y recibos conocidos
en ese bloque. Conciliar posteriormente un envío incierto actualiza la
proyección actual sin reescribir las decisiones históricas.

El trabajador reutiliza outbox, conexión operadora, límites de gas y parada
existentes. Su plazo de ejecución queda acotado por política, fin de ventana
y cambio de mes UTC. Sólo un bloque efectivamente observado permite tolerar
el pequeño adelanto del reloj de cadena admitido por el lector; no permite
decisiones futuras. Si cadena y reloj atraviesan meses distintos, espera.

El rearme de la misma política conserva gasto, referencia, claves y recibos.
Una decisión sin outbox sólo se libera como nunca enviada después de sellar
su permiso anterior STOPPED. Un intento incierto con outbox sigue bloqueando
el rearme. La restauración nunca conecta ni inicia automáticamente el bot.

Pruebas de desarrollo: BASE → recibo → espera → DIP, respuesta POST perdida
sin segundo envío, conciliación original tras reapertura, rechazo de testigos
o presupuestos alterados incluso con hashes recalculados, copia cifrada y dos
variantes de rearme con/sin cierres observados. Las 34 pruebas específicas
aprobaron; la regresión más amplia se registra por separado. Estas piezas aún
eran internas durante esa primera etapa; la integración publicada se describe abajo.

## Renovación y activación v75

La revisión `orbis.dex.bot.dca.policy-rollover.v1` reutiliza el control de
wallet y sucesor de política existentes. Conserva límites del primer grant
REAL, gasto por mes del recibo, claves BASE/DIP, referencia y huella de todos
los intentos propios. El cursor DCA no incorpora trailing ni reglas de salida
LONG. Una política BUY-only agotada no se convierte en una autorización de
venta; la posición permanece en la wallet.

Tras CONFIGURE, los contadores físicos nuevos se mantienen separados del
consumo de toda la familia. El evaluador conserva la espera desde el último
recibo de compra, aunque el nuevo contrato muestre `last_entry_at = 0`.
Las dos renovaciones consecutivas, gas restante, reapertura y conservación
de BASE/DIP aprobaron junto con los siete casos del diario (ocho en total).

La API protegida compartida acepta DCA sólo con modo de permiso explícito.
`dca_activation_window_seconds` fija la ventana inicial; rearmar o renovar
no permite cambiarla. Una solicitud repetida recupera la misma sesión sin
devolver el claim de RAM ni repetir el arranque. Las solicitudes LONG previas
conservan su huella y semántica. El estado DCA expone cantidades como cadenas
decimales enteras y separa gasto acumulado de contadores de la política actual.

La interfaz reutiliza vínculo, operadora, gas, activación, parada y revisión
de renovación. Identifica DCA y muestra gasto y número de activaciones; la
ventana aparece sólo para DCA y queda fijada al rearmar. No guarda claves en
el navegador. La API ya aprobó BASE → parada → rearme → DIP → reapertura;
la interfaz aprobó también renovación confirmada por wallet, rearme y DIP,
vistas de escritorio/móvil y recuperación bloqueada sin autorun. Todas las
firmas y swaps de comprobación pertenecen a EVM desechables identificadas.

Evidencia final: `v75-dca-integrated-r3/verification.json` bajo
`/home/rich/.local/state/orbis/verification`. 198 pruebas de integración más
8 de interfaz/recursos, cinco recorridos DEX existentes repetidos y el nuevo
recorrido DCA. Las demás evidencias se conservan con procedencia explícita;
no se declara una nueva ejecución completa de toda la suite ni un piloto mainnet.
