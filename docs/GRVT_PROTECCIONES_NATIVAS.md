# GRVT — protección nativa TESTNET

Estado al 31-10-2026: compilación, firma, transporte, diario OCO cifrado,
revalidación, lectura tras reinicio, cancelación expresa y conciliación de
TP/SL **TESTNET** implementados, con panel manual y API local protegida.
«Enviar señal» continúa admitiendo únicamente IOC; esa entrada no deja
automáticamente un stop en GRVT. No se ha realizado un piloto con
credenciales Trade reales ni se ha habilitado mainnet.

Este bloque es un requisito previo de los bots LONG/SHORT reales, no una
sustitución de la meta de ejecución automática con límites y parada.

## Separación de responsabilidades

`grvt_testnet_protection.py` compila un cierre desde una posición observada,
valida el contrato TP/SL, firma y contrasta la orden observada. No maneja
credenciales persistentes, no reserva un intento y no autoriza un POST.
`GrvtTestnetOrderClient.submit_signed_protection` sigue siendo transporte
individual interno de un solo uso, no un servicio de colocación autónomo.
El OCO utiliza otro método nativo con su propio diario y revalidación:
`submit_oco_once`. No debe llamarse al transporte directamente desde una
nueva ruta sin ese circuito.

El contrato IOC y su límite de cinco minutos no se amplían. Ambos circuitos
comparten las conversiones decimales y primitivas EIP-712, pero tienen
validadores y métodos públicos diferentes. Una instancia de transporte sólo
dispone de un POST total, sea IOC o TP/SL. Un timeout o acuse incoherente
consume ese intento en memoria: no se reenvía por ese cliente. El servicio
OCO añade además una concesión duradera anterior a la red, que sobrevive a
un timeout o reinicio. No se pueden sustituir ambas garantías entre sí.

La bóveda v34 añade `grvt_testnet_oco_groups`, `grvt_testnet_oco_legs`,
`grvt_testnet_oco_claims` y `grvt_testnet_oco_observations`. V35 añade
`grvt_testnet_oco_cancels`. V36 añade auditorías financieras conjuntas y sus
recibos en `grvt_testnet_oco_audits` y `grvt_testnet_oco_reconciliations`.
Las migraciones conservan una preimagen cifrada
y verifican recuentos protegidos y relaciones antes de sustituir el archivo.
No se guardan claves, cookies, firmas completas o direcciones completas de
firmante/builder. Los términos económicos sí se conservan dentro de SQLCipher.

## Orden y cálculo

- Cadena testnet 326, un perpetuo, orden limitada GTT, `post_only=false`,
  `reduce_only=true`, tamaño explícito y comisión builder cero.
- Sólo `STOP_LOSS` o `TAKE_PROFIT`, referencia `LAST`. No se sustituye LAST
  por mark, bid, ask ni un precio PAPER. El ticker testnet conserva su campo
  `last_price`; si falta, no puede compilarse esta protección.
- La cantidad y el lado salen de la única posición observada: SELL para
  reducir LONG y BUY para reducir SHORT. No se propone capital ficticio.
  La lectura de riesgo existente comprueba modo, exposición, equity,
  frescura, reglas de margen y ausencia de órdenes observadas.
- SL LONG por debajo de LAST; TP LONG por encima. SL SHORT por encima y
  TP SHORT por debajo. Un disparador ya cumplido se rechaza, también por
  igualdad. Estos precios no garantizan un beneficio neto después de costes.
- Desviación autorizada de 0 a 100 bps respecto al disparador. Para BUY se
  redondea hacia abajo al tick; para SELL, hacia arriba. El redondeo nunca
  empeora ese límite. Cantidades, nocionales y conversión de precio a nueve
  decimales usan Decimal y conversión entera exacta, sin float ni truncamiento.
- El tope de nocional cubre tamaño × máximo(LAST, disparador, límite).
  Reglas de tamaño mínimo, precisión y nocional mínimo se vuelven a comprobar.
- Política inicial ORBIS: vigencia de un minuto a 24 horas al compilar. El
  límite de 24 h es propio de este contrato, no un límite atribuido a GRVT.
  El firmante conserva la expiración exacta autorizada.
- `close_position=false` e `is_split_position=false`: sólo las unidades
  explícitas; no se solicita un cierre dinámico de toda posición existente.
  Estos flags no vinculan la orden a una identidad de posición: una salida
  huérfana podría reducir una posición posterior del mismo instrumento. La
  futura coordinación debe bloquear nuevas entradas hasta resolver todas
  las salidas anteriores; reduce-only por sí solo no elimina ese riesgo.

Un stop-limit puede quedar sin ejecución ante un gap, falta de liquidez o
rechazo. El servicio OCO usa el agrupamiento nativo de GRVT; no envía dos
órdenes independientes ni implementa trailing stop nativo. El estado recibido
del proveedor no prueba ejecución futura o liquidación financiera.

## Firma y evidencia: dos garantías diferentes

Según el [esquema oficial de creación](https://api-docs.grvt.io/schemas/api_create_order_request/),
los metadatos no forman parte de la firma EIP-712 ni se transmiten al contrato.
El disparador TP/SL y el ID cliente son metadatos. La firma protege la orden
limitada, tamaño, lado, reduce-only, duración, builder y demás campos firmados;
**no demuestra criptográficamente el precio de disparo**.

`verify_testnet_protection` exige por separado el disparador esperado por
ORBIS. El transporte compara también los metadatos devueltos por GRVT, no sólo
los campos firmados. El OCO normaliza representaciones decimales equivalentes
sin redondear (`0.01` y `0.010000000`), así como mayúsculas hexadecimales;
no convierte tipos, flags o condiciones. Cero no sustituye a un booleano.
Los circuitos individuales conservan su contrato JSON estricto anterior.
Acuses alterados quedan inciertos y no se interpretan como fill.

`lookup_protection` coteja ID, instrumento, lado, tamaño, límite, firma,
expiración, reduce-only y disparador. Comprueba creación/actualización y
coherencia de tamaño ejecutado con estado terminal. La orden esperada se
valida al instante original, por lo que caducar no impide consultar. El
reloj local no convierte una orden OPEN en CANCELLED ni concede otro POST.

La respuesta es observacional: `protection_guaranteed=false` siempre. El
campo `active_observed` sólo indica OPEN con cantidad pendiente y vigencia
local; no prueba que siga esperando el disparador, que vaya a ejecutarse ni
que el historial de fills esté conciliado.

Se contrastan tipos y semántica con el
[firmante del SDK oficial](https://github.com/gravity-technologies/grvt-pysdk/blob/main/src/pysdk/grvt_raw_signing.py)
y las reglas públicas de
[TP/SL standalone](https://help.grvt.io/en/articles/11064863-standalone-direct-tpsl-take-profit-stop-loss).
No se añade dependencia del SDK ni se ejecuta código descargado.

El contraste detecta y corrige un caso del firmante compartido: con dirección
builder cero, el SDK usa el tipo EIP-712 `Order`, sin campos builder/fee en el
mensaje firmado. Con builder no cero usa `OrderWithBuilderFee`. La comisión
sigue fijada a cero en ambos casos. Dos pruebas con tipos independientes del
firmante ORBIS comprueban IOC y GTT sin builder; las firmas con builder del
circuito anterior conservan el mismo contrato.

## Diario y envío conjunto implementados

`prepare_oco` vincula SL/TP a la cantidad completa de la única posición
observada en una entrada propia ya conciliada. Conserva su recibo exacto y,
si existe, procedencia de la acción/estrategia; no inventa esa procedencia
para una entrada manual anterior. Reserva para siempre los IDs cliente de
ambas salidas, incluso al abandonar una preparación sin POST. El circuito
IOC no puede reutilizarlos y se bloquea mientras haya un OCO pendiente.

`submit_oco_once` contrasta reglas del instrumento, dos fotografías privadas
de riesgo, seis historiales desde el recibo previo y cotización LAST fresca.
Debe conservarse el saldo/posición de origen; las variaciones legítimas de
UPNL no se confunden con aportaciones o fills. Se archiva la fotografía final
y se vuelve a comprobar bajo exclusión de STOP/bloqueo antes de abrir red.

La concesión pasa irreversiblemente a `POST_CLAIMED` antes del único bulk:
`https://trades.testnet.grvt.io/full/v2/bulk_orders`. El contrato Full v2 usa
literalmente `order_i_ds` y `client_order_i_ds`; el envío de creación contiene
ambas órdenes y listas de cancelación vacías. No hay fallback secuencial.
Un acuse parcial, timeout o caída conserva el bloqueo, sin reenvío. Dos
acuses coherentes producen `ACKNOWLEDGED`, no un fill ni protección garantizada.
El acuse tardío se conserva aunque la firma haya caducado durante la red.

`observe_oco_orders` recupera cada ID con lectura privada, contrasta la firma
devuelta y el contrato canónico con las huellas guardadas, y sólo conserva
estados saneados de ambas salidas. Secuencia y hash anterior forman una cadena
validada también en copias. No admite regresión de estado, cantidades o ID,
ni un total ejecutado mayor que la posición de origen. Su alcance es
`ORDER_STATE_ONLY`: incluso dos estados terminales no liberan la subcuenta
sin la conciliación financiera independiente descrita más abajo.

## Cancelación expresa implementada

El [contrato oficial bulk](https://api-docs.grvt.io/schemas/api_bulk_orders_request/)
permite cancelar un grupo mediante el ID de un miembro. `cancel_oco_once`
exige STOP previo y consentimiento booleano explícito de que retirar SL/TP
no cierra la posición y puede dejarla sin protección. STOP por sí solo jamás
invoca este servicio ni cancela las protecciones existentes.

Se consultan y verifican ambas salidas antes de reservar una cancelación.
El objetivo es un miembro todavía PENDING/OPEN; no se envía nada si ambos
ya son terminales. La reserva guarda grupo, huella de plan, observación,
instante de STOP y un único objetivo. Se confirma en SQLCipher antes de red.
Un bulk vacío, TTL de 5000 ms y ese ID solicitan cancelar el grupo; no se usa
`cancel_all_orders` ni se toca otra cuenta. El transporte comparte la cuota
de una sola cancelación por cliente con el circuito IOC.

El acuse se conserva aparte y después se vuelven a consultar ambos IDs.
Respuesta perdida, fallo de lectura o reinicio no permiten otra cancelación;
sí permiten observar. Un acuse no convierte OPEN en CANCELLED, ni desaparece
un fill parcial. La expiración local tampoco se interpreta como cancelación.
El TTL no prueba que una creación retrasada nunca pueda llegar. La intención
original y la subcuenta siguen bloqueadas hasta una conciliación financiera
verificada; el acuse no concede nueva autorización de trading.

## Auditoría financiera conjunta y continuidad implementadas

`audit_oco` exige que ambas salidas sean terminales y que exista el preflight
del envío. Revalida la entrada de origen y ambas órdenes, fija un corte antes
de consultar historiales, agota fills de las dos salidas y cinco historiales
de movimientos, y finalmente lee cuenta/órdenes abiertas. La cuenta debe
cubrir el corte común; la observación debe completarse dentro de cinco
segundos. No hace un POST de creación/cancelación de órdenes.

Particiona el historial en fills de la entrada ya conocidos y fills de ambas
salidas. No confunde a la otra salida con actividad ajena ni permite ignorar
una operación desconocida. Contrasta límites firmados, cantidades totales y
estados terminales; conserva los fills parciales. Exige USDT, margen simple
cruzado, modo de un activo, sin órdenes abiertas ni otro colateral no nulo.

Con Decimal de precisión explícita comprueba:

- Posición posterior = posición de entrada + suma firmada de los fills de
  salida. Cancelar sin fills conserva la posición, no la deja ficticiamente a cero.
- Saldo posterior = saldo del recibo de entrada + P/L reportado de salida
  − comisiones USDT − financiación vinculada. Las comisiones pueden incluir
  rebates; financiación positiva es pago y negativa es cobro.
- Equity = saldo + UPNL. Si no queda posición, exige también márgenes y UPNL
  nulos. Con posición parcial no exige que UPNL permanezca inmóvil.

Los campos y sus signos se contrastan con los esquemas oficiales de
[fills](https://api-docs.grvt.io/schemas/fill/) y
[pagos de financiación](https://api-docs.grvt.io/schemas/funding_payment/).

La financiación debe vincularse por tx/trade a los fills propios de salida.
No se estima desde una tasa pública. Transferencias, retiradas, depósitos,
intereses u otra actividad no vacía no se atribuyen silenciosamente al OCO,
incluso si se compensan y dejan el saldo igual. Una discrepancia mantiene el
bloqueo. Las auditorías válidas forman una cadena cifrada; fills o pagos ya
observados no pueden cambiar o desaparecer. Se normalizan ceros decimales
equivalentes sin redondear importes ni convertir booleanos en números.

`reconcile_oco` requiere dos auditorías estables separadas al menos cinco
segundos, con la última de hasta treinta segundos. Guarda un recibo explícito,
inmutable e idempotente; leerlo después no renueva su frescura. Los recuentos,
huellas, relaciones y cálculos se repiten al leer/verificar copias, no se
confía únicamente en el SHA. No escribe el libro REAL ni borra STOP o IDs.

El recibo tiene alcance `ISOLATED_TESTNET_OCO_BALANCE_POSITION_MATCH`.
`causal_proof`, `mainnet_order_sent` y `real_book_events_created` son falsos:
se contrastan los datos reportados por GRVT; no se demuestra ausencia de
omisiones del proveedor ni se recalcula independientemente su liquidación.
El P/L se identifica expresamente como reportado, no fiscal ni garantizado.
`net_realized_pnl_usdt` describe el neto de **estas salidas** respecto al
recibo de entrada: no incluye otra vez las comisiones de apertura ya
descontadas de aquel saldo. La interfaz no debe presentarlo como beneficio
neto de todo el ciclo; para eso debe incorporar también costes de la entrada.

Una nueva orden utiliza el último recibo cronológico, OCO o IOC, para validar
saldo/posición. La evidencia de historial v3 parte del OCO conciliado e
incluye entrada, salidas y financiación conocidas. También contrasta ambas
salidas por sus contratos originales. Actividad posterior, cambios o datos
que desaparezcan vuelven a bloquear; ni el recibo ni un resultado «conciliado»
sustituyen preflight, permiso, límites y STOP de la siguiente orden. Los
historiales v1/v2 existentes se siguen verificando bajo su semántica original.

## Panel manual y API local

En «Patrimonio → Recuperación GRVT → Protecciones SL / TP · TESTNET»:

1. Consulta la subcuenta; el listado procede de SQLCipher y no conecta al exchange.
2. Selecciona una entrada propia ya conciliada y define precios absolutos SL/TP.
   La cantidad completa y el lado de cierre no son editables.
3. Introduce la API key **TESTNET** y pulsa «Preparar». Se revisan posición,
   saldo, reglas y LAST actuales; aún no se firma, reserva ni coloca nada.
4. Revisa cantidad, lado, disparadores y límites. Vuelve a introducir la API
   key y la clave de firma dedicada a la API Trade TESTNET, nunca una clave
   privada de wallet o mainnet; confirma «Enviar · TESTNET».
5. «Consultar» recupera ambos IDs; «Auditar» sólo puede conciliar estados
   terminales. Repite la auditoría tras al menos 5 s y pulsa «Conciliar» dentro
   de los 30 s posteriores a la última auditoría estable.
6. «Cancelar salidas» exige confirmar que retira SL/TP sin cerrar la posición.
   Aplica STOP primero y nunca lo borra al conciliar. «Descartar» sólo se
   admite para una preparación local sin concesión de envío.

El panel separa estado de transporte, estados observados y recibo financiero.
Muestra expresamente la posición restante y el **neto de salidas**, no el neto
del ciclo completo. No ofrece rearme automático ni activación mainnet.
Los botones no operan con importes ocultos. Página local de 50 grupos, con
controles para consultar los anteriores. Sin sondeo privado automático.

Rutas bajo `/real/exchanges/grvt/testnet/oco`, todas con acceso loopback,
origen local y sesión de bóveda, sin caché:

| Método / sufijo | Efecto |
| --- | --- |
| GET `?sub_account_id=…&offset=…` | Registro, candidatos y recibos locales |
| POST `/preview` | Preparación sin firma ni reserva |
| POST `/send` | Firma del contrato revisado y único bulk mediante el motor |
| POST `/{group_id}/observe` | Consulta y cadena de estados de ambas salidas |
| POST `/{group_id}/audit` | Auditoría financiera conjunta |
| POST `/{group_id}/reconcile` | Recibo explícito local e idempotente |
| POST `/{group_id}/cancel` | STOP y retirada expresa del grupo, nunca cierre |
| POST `/{group_id}/abandon` | Descarte de preparación aún sin concesión |

La preparación se autentica con HMAC de clave aleatoria efímera y token de
sesión, con dominio separado. Caduca en 120 s o antes si vence la orden.
Reinicio, bloqueo o nueva sesión la invalidan. Se vinculan origen y huella
del recibo, grupo, IDs, nonces, cantidades, precios, tope, expiración y reglas
del instrumento. No se acepta un objeto de órdenes arbitrario en `/send`.
Todos los tiempos ns e IDs grandes de la envoltura son cadenas: un viaje por
JSON/JavaScript no redondea el contrato. Enviar revalida origen, STOP y reserva
antes de autenticar con GRVT y pasa por el preflight/concesión del motor.

Política del panel: hasta **1000 USDT nocionales**, sólo salidas reduce-only
de TESTNET, builder cero y comisión builder cero. No amplía el tope de
100 USDT del puente de entradas IOC ni concede autoridad mainnet. El límite
se contrasta con LAST, disparadores y precios de salida; no es margen.
Clave API y clave de firma se usan por petición, se borran del formulario y
no se guardan en browser storage, SQLCipher, recibos ni respuestas. La
validación de estos cuerpos devuelve errores fijos sin repetir secretos.
Una respuesta perdida conduce al registro: no a un reenvío, incluso tras
bloquear/reabrir. Una reserva PREPARED sin POST puede descartarse; sus IDs
siguen sin poder reutilizarse.

## Entrada FOK y OTOCO — núcleo interno en desarrollo

El contrato oficial [bulk v2](https://api-docs.grvt.io/schemas/api_bulk_orders_request/)
admite padre + TP + SL con la misma cantidad e instrumento y lados opuestos.
Los hijos esperan al fill completo del padre. Se contrasta también el enum
[FOK = 4](https://api-docs.grvt.io/schemas/time_in_force/). La elección de FOK
es una política de ORBIS para esta futura entrada vinculada: puede ejecutarse
completa o no ejecutarse. No se convierte el IOC anterior silenciosamente.

`grvt_testnet_bracket.py` compila el plan desde la cuenta realmente plana,
riesgo, reglas y LAST frescos. Dimensiona los hijos desde la entrada solicitada
sin fabricar una posición observada. Firma padre FOK, SL/TP GTT reduce-only,
builder/fee cero; exige nonces/IDs distintos, niveles coherentes con entrada
y LAST, mínimos/ticks exactos y límites nocionales separados. El padre vence
en hasta cinco minutos; los hijos duran al menos un minuto, más que el padre,
y hasta 24 h. No implica que una firma pueda esperar cinco minutos tras una
señal PAPER: el coordinador descrito abajo acota también su vigencia causal.

El transporte interno prepara un único bulk de tres órdenes. Coteja cada
acuse por ID, contrato tipado y cantidades exactas; no necesita que el servidor
devuelva la misma ordenación. Un timeout, acuse incompleto o cambio de trigger
consume la cuota sin fallback ni reenvío. Esa cuota es ahora atómica entre
hilos y compartida con IOC y OCO, además de las concesiones duraderas que
debe aportar el servicio. La firma FOK no es aceptada por el validador IOC.

La observación contrasta las tres firmas/huellas y metadatos. Conserva un
parcial FOK reportado como violación a revisar, no como cero ni como entrada
protegida. Advierte ante parcial de salida, hijos huérfanos, sobrecierre,
resto tras estados terminales, expiración local o estados contradictorios.
Ni el reloj cancela órdenes ni el estado OPEN demuestra protección activa.
La cantidad restante inferida tiene alcance `ORDER_STATE_ONLY`, no es una
posición de cuenta conciliada. `protection_guaranteed` y `can_submit` son falsos.

SQLCipher **v37** añade grupos y reservas de los tres IDs en
`grvt_testnet_bracket_groups` y `grvt_testnet_bracket_legs`. Conserva procedencia
de señal, versión/dataset, límites de precio, términos y huellas; nunca las
firmas completas, claves o direcciones de firmante. La procedencia se valida
estructuralmente aquí, no se trata como prueba de replay ni autorización.
La preparación por sí sola no verifica LAB/PAPER; esa comprobación corresponde
al coordinador descrito abajo. Una preparación pendiente bloquea entradas IOC/OCO; un
descarte sólo libera el ámbito, no sus IDs. Cambiar límites de spread/slippage
con el mismo ID no reutiliza la preparación. Backup y migración verifican
reglas, cantidades, relaciones, tipos y recuentos, no sólo el hash.

### Coordinador causal, concesión y recuperación — v38

`submit_bracket_signal_once` comparte la verificación causal LAB/PAPER con
IOC mediante una función interna; no amplía el validador ni la interfaz IOC.
Exige una última acción OPEN ejecutada y reciente, asiento, posición abierta,
sesión activa, definición/versionado/dataset públicos y replay de observaciones
reproducibles. El bloqueo transaccional PAPER conserva estable el origen hasta
terminar el intento. No basta con pasar metadatos de una señal al transporte.

Contrasta términos públicos actuales, dos lecturas privadas de riesgo y la
continuidad de los seis historiales desde el recibo previo. La segunda cuenta
y sus reglas deben mantener iguales saldo, equity, posiciones, órdenes y
márgenes; exige cuenta plana, LAST/bid/ask recientes y políticas de precio
inmutables. Archiva la última revisión y la revalida al reclamar y justo antes
del único bulk. La expiración del padre no puede superar los **20 segundos
desde la acción PAPER**. Una corrección regresiva del reloj o señal vencida
impide el POST, aunque la concesión ya consumida deba conservarse.

SQLCipher v38 añade `grvt_testnet_bracket_claims` y
`grvt_testnet_bracket_observations`. El claim se confirma antes de red y nunca
regresa a PREPARED. Timeout, proceso interrumpido, acuse incompleto o respuesta
perdida no permiten reclamar otro envío, tampoco con otro cliente o al reabrir.
Los acuses guardan sólo IDs y huellas, no son fills. STOP y el último control
del POST comparten la exclusión de escritura de la bóveda.

La recuperación necesita únicamente los IDs del diario: obtiene los tres
contratos desde el venue y verifica firmas, metadatos y huellas originales
antes de guardar estados saneados. No necesita que el usuario conserve las
firmas. Valida sucesión, relojes, cantidades no decrecientes, IDs del acuse,
resumen derivado y una cadena con cabeza/recuento anclados en el claim; borrar
la cola o rehashear una falsa garantía no da una evidencia válida. Conserva
la cantidad parcial FOK realmente informada y aplica STOP persistente ante
estados que requieren revisión. No cancela órdenes, vende ni libera capital.

### Cancelación del grupo OTOCO — v39

`cancel_bracket_once` exige STOP y confirmación expresa de que cancelar retira
las protecciones sin cerrar la posición. Consulta los tres IDs y reclama en
SQLCipher una única cancelación antes de abrir red. El objetivo procede de
un miembro propio aún OPEN/PENDING, con contrato y observación verificados.
Utiliza el [bulk de GRVT](https://api-docs.grvt.io/schemas/api_bulk_orders_request/)
sin creaciones, con un ID de miembro y TTL de cinco segundos. Revalida el
estado, la cabeza de observación, STOP y la ventana de cinco segundos en la
exclusión final. La cuota de cancelación del cliente es atómica y compartida
entre cancelación individual, OCO y OTOCO; no consume la cuota de creación.

SQLCipher v39 añade `grvt_testnet_bracket_cancels`. Migración, lectura y backup
verifican alcance, observación, STOP y acuse, no sólo sus hashes. Acuse, timeout,
reinicio o caducidad después del claim no permiten repetir el POST. Recuperar
consulta los tres contratos; un grupo ya terminal no origina una cancelación.
Cancelar conserva los fills y la cantidad restante: no ejecuta una venta,
no declara una posición cerrada y no libera la subcuenta. El resultado incluye
`position_closed=false` y `financially_reconciled=false`.

**Hay panel manual y API OTOCO TESTNET**: véase la prueba de entrada protegida
más abajo. La automatización sigue pendiente del enlace operativo de
las salidas CLOSE, trailing y permanencia al ciclo del bot. Este coordinador
prueba la causalidad de **entrada** con SL/TP revisados, no la ejecución completa
de la estrategia: todavía no deriva esos niveles del precio efectivamente
ejecutado ni implementa todas sus reglas de salida. Tampoco hay credenciales
persistentes o permiso automático. Antes de activar un trabajador hay que resolver estas
puertas y comprobar el contrato con un piloto humano testnet.
No se debe invocar el transporte por su cuenta desde una ruta o un worker.
Este núcleo no activa bots GRVT ni mainnet. El panel manual OCO anterior
continúa operativo con su propia conciliación; no se anuncia como OTOCO.

### Conciliación financiera del grupo OTOCO — v40

`audit_bracket` verifica los tres contratos originales y sus estados terminales,
agota los fills de cada miembro y los cinco historiales financieros hasta un
corte común, y obtiene una cuenta posterior. La base es la cuenta plana del
preflight de entrada, no una posición ficticia. Los fills de los otros miembros
se explican como parte del mismo grupo; cualquier fill ajeno, omisión, comisión
en otra divisa o movimiento no explicado bloquea la conciliación.

Con precisión decimal de 100 dígitos:

- `saldo = saldo_previo + P/L_reportado - comisiones_USDT - financiación_pagada`.
- `posición = signo_entrada × (cantidad_entrada - cantidades_salidas)`.
- `neto_realizado = P/L_reportado - comisiones_USDT - financiación_pagada`.

Los precios deben respetar los límites firmados; las salidas no pueden exceder
ni preceder los fills de entrada. Los márgenes, equity y posiciones de la cuenta
deben cuadrar. El P/L procede del proveedor: `causal_proof=false` identifica el
alcance observacional, sin prometer una prueba independiente de su integridad.
Se conserva una salida parcial como exposición restante, con STOP activo.

`reconcile_bracket` exige dos auditorías estables, separadas al menos cinco
segundos y una última lectura de hasta treinta segundos. SQLCipher v40 añade
auditorías con cadena, cabeza/recuento y un recibo inmutable. Lectura y backup
recalculan importes y semántica; rehashear un booleano, corte o saldo falso no
lo valida. La migración conserva claim, contratos y observaciones y crea una
preimagen cifrada antes de reemplazar el archivo.

Una observación posterior no reescribe el recibo histórico. Si informa otro
estado, cantidad o actividad, impide la siguiente operación. La continuidad v4
enlaza las tres órdenes y financiación conocida con el siguiente preflight IOC;
vuelve a drenar historiales y comparar saldo/posición. El recibo no borra STOP,
no reutiliza IDs ni crea asientos REAL. No rearma el grupo ni activa un bot.

### Reducción de emergencia del grupo OTOCO — v41

`submit_bracket_emergency_once` es un coordinador interno TESTNET. Conserva
STOP y utiliza el diario IOC existente, sus dos controles de riesgo, historiales,
auditorías y conciliación. No añade una opción genérica para saltarse STOP:
requiere una concesión cifrada vinculada al grupo y al recibo anterior exacto.

Sólo admite un IOC firmado `reduce_only=true`, builder/fee cero, instrumento
original, lado opuesto y cantidad positiva como máximo igual a la posición
restante conciliada. Mantiene el nocional máximo y la política de precio del
grupo. Contrasta términos actuales, firma y cotización; la concesión sólo es
utilizable durante cinco segundos y la orden vence como máximo a los diez
segundos de su emisión. Antes de reclamar vuelve a consultar las tres órdenes
originales y obtiene una nueva cotización. Un cambio tardío impide el envío.

La semántica de IOC permite ejecución parcial, y reduce-only exige reducir
la posición o cancelar; por eso el acuse no significa «posición cerrada».
Referencia oficial consultada: [contrato de creación de orden GRVT](https://api-docs.grvt.io/schemas/api_create_order_request/).
Tras cada intento se observan fills/costes y cuenta con las guardas IOC
existentes. Dos auditorías estables permiten el recibo. Si queda exposición,
otro intento necesita primero ese recibo y un ID firmado nuevo. La subcuenta
continúa detenida después del cierre, sin rearme ni reactivación implícitos.

SQLCipher v41 añade `grvt_testnet_bracket_emergencies`, un ámbito por ID IOC
con claves foráneas al intento y al grupo. El preflight v3 ancla su digest;
borrar la concesión o cambiar subcuenta, alcance o recibo hace fallar lectura
y backup. La migración preserva el recibo financiero del grupo y STOP, con
preimagen cifrada. Se guardan términos y huellas, no la clave ni la firma.
El estado `POST_CLAIMED` se confirma antes de red y no puede volver atrás.
Ante timeout/reinicio/caducidad tras claim se consulta el ID sin repetir POST.

El panel manual descrito a continuación expone colocación y emergencia
TESTNET con revisión por intento. Faltan el ciclo operativo de señales
CLOSE/trailing/permanencia y la autorización del worker. Este cierre acotado
no es la activación automática de una estrategia.

## Prueba manual de entrada protegida y recuperación

En Patrimonio → Recuperación GRVT → «Entrada protegida · TESTNET» se puede
seleccionar una entrada AUTO PAPER reciente, indicar cantidades y límites
TESTNET independientes y revisar las tres órdenes antes de firmar. Es una
prueba manual de la entrada con SL/TP revisados: no despliega las demás salidas
de la estrategia ni inicia un bot. Una acción PAPER conserva su ventana de
veinte segundos; una revisión caducada se rechaza sin reservar ni enviar.

La vista previa reproduce las mismas observaciones LAB/PAPER que el envío.
`reviewed_testnet_signal` mantiene la transacción mientras se comprueban versión,
dataset público, asiento, señal y posición. No se crean firmas ficticias ni se
anulan validadores. El cliente privado se construye después de verificar el
origen, evitando incluso un login para una señal inválida. Preparar no reserva
IDs. Al enviar, el coordinador completo revalida riesgo/historial y confirma el
claim antes de un único bulk.

Las revisiones viajan con HMAC ligado al token de la bóveda y dominio separado
del OCO. Bloqueo/desbloqueo las revocan. Los importes y tiempos ns viajan como
cadenas exactas; no se convierten a números JavaScript para firmar. Entrada
limitada a 1.000 USDT de nocional y salidas a 10.000 USDT como máximo, con topes
editables dentro de esos límites. La clave de API y la clave dedicada de firma
TESTNET se borran del formulario tras cada petición; no se guardan en storage.

El mismo panel permite consultar los tres IDs, auditar dos veces, conciliar,
cancelar salidas con STOP y confirmación, y revisar el cierre del residuo
conciliado. Las emergencias comparten las auditorías/recibos IOC y permanecen
ligadas a su grupo. Se puede recuperar todo con LAB y PAPER bloqueados; sólo
una nueva entrada requiere sus fuentes disponibles. Un acuse nunca se presenta
como un fill. Los cierres parciales se conservan y STOP no se borra al conciliar.
Mientras exista una orden IOC pendiente en la subcuenta, el panel no ofrece otro
cierre. Preparar otra revisión también se rechaza antes de consultar o autenticar
al proveedor; la reserva transaccional vuelve a imponer el mismo control al enviar.

API local protegida bajo `/real/exchanges/grvt/testnet/brackets`:

- `GET`: registro paginado, sin red externa.
- `POST /preview` y `/send`: revisión autenticada y un único envío nativo.
- `POST /{parent}/observe|audit|reconcile|cancel|abandon`: recuperación del grupo.
- `POST /{parent}/emergency/preview|send`: revisión y cierre reduce-only.
- `POST /{parent}/emergencies/{cid}/audit|reconcile`: seguimiento del cierre.

Todas las rutas comparten las restricciones de loopback, Origin y sesión REAL.
La validación de cuerpos no refleja credenciales en errores. La interfaz
prioriza registros abiertos, mantiene la nueva entrada en un desplegable y
rechaza respuestas tardías tras bloqueo o cambio de subcuenta. No requiere
direcciones de wallet ni modifica el patrimonio registrado por estas pruebas.

## Cierre causal de estrategia desde la interfaz

`grvt_testnet_strategy_close` enlaza una acción CLOSE reciente con el grupo
nativo de su entrada. Reutiliza el replay LAB/PAPER, exige la misma sesión,
versión, dataset y posición PAPER, y comprueba también el asiento de apertura.
Compartir una versión de estrategia no autoriza a cerrar otra posición.

La solicitud se guarda cifrada en `grvt_testnet_strategy_closes` (SQLCipher
v42), junto a la huella del grupo, fuente CLOSE, identidad de posición, plazo
solicitado de 10 a 300 segundos y máximo de 1 a 3 reducciones. El tiempo gastado
en verificar la fuente se descuenta sin extender el vencimiento. El núcleo
acepta un vencimiento futuro de hasta 300 s; el rango de duración se valida en
la API. STOP se activa en la misma
transacción. Repetir la solicitud recupera el permiso original sin prolongarlo
ni aumentar el número de intentos. La migración conserva una preimagen cifrada
y los recibos v41; no se guardan claves, firmas ni cookies.

`advance_strategy_close` realiza un paso y `run_strategy_close` ejecuta la
secuencia sin clics intermedios: observar los tres contratos, reclamar una
cancelación si siguen activos, auditar dos veces con separación de cinco
segundos, conciliar y reducir exclusivamente el residuo conciliado. Una respuesta
perdida conduce a consultas y auditorías del claim existente. Un parcial permite
otro ID sólo tras su recibo y dentro del presupuesto de intentos. Se declara
COMPLETE únicamente con recibo de posición plana; el límite de intentos deja
visible el residuo pendiente, y STOP nunca se levanta automáticamente.

Una exclusión de archivos con permisos 0600 serializa los pasos entre conexiones
y procesos sin retener una transacción SQLite mientras llaman a otros servicios.
Las credenciales pertenecen a la llamada del ejecutor: no existe autostart, ni
una autoridad persistente de trading. Se admite parada entre pasos y revocación
por bloqueo/reapertura de la bóveda; una sesión nueva no hereda las claves de la
anterior. Los límites temporales se revisan también durante los preflights.

La interfaz permite «Cerrar por señal» desde el grupo correspondiente. «Cierre
· límites» configura intentos/plazo antes de preparar. Preparar fija la señal
en su ventana de veinte segundos, guarda la solicitud y activa STOP, sin
autenticar al exchange ni enviar órdenes. Después se introducen las claves
TESTNET y se confirma «Ejecutar cierre». Así no hay que introducir las claves
dentro de la ventana original de la señal. Los límites quedan fijados al
preparar; modificar los controles no renueva una solicitud ya registrada.

`GrvtStrategyCloseService` ejecuta el cierre en segundo plano con estado visible,
parada y continuación expresa dentro del plazo y presupuesto originales. Las
claves sólo viven en la llamada/hebra durante el trabajo y se liberan al terminar;
no se promete borrado físico de cadenas Python. Bloquear o proteger la vista
retira también este permiso aunque un bot MEXC tenga su propia autoridad
operativa. Los puntos de control revisan la parada antes de nuevos claims/POST;
una petición ya iniciada puede quedar incierta y se recupera por ID.

API local bajo `/real/exchanges/grvt/testnet/brackets/{parent}/strategy-close`:

- `POST /prepare`: fuente CLOSE verificada y límites; sin claves ni envío.
- `POST /start`: claves efímeras y confirmación, devuelve 202 y estado.
- `GET`: solicitud y progreso, sondeo pasivo sin prolongar el permiso.
- `POST /stop`: solicita parada, sin borrar posiciones ni recibos.

Se comparten sesión REAL, loopback y Origin con el panel manual; cuerpos inválidos
no reflejan claves. Las lecturas no retienen el cerrojo HTTP mientras esperan la
bóveda: sondear el registro o encolar otro inicio no bloquea la petición de parada.
La generación de bóveda se comprueba al reservar y ejecutar; una petición vieja
no se incorpora a una nueva sesión. Un contador efímero de permiso, global y por
grupo, invalida también los inicios pendientes al detener/proteger la vista:
la autoridad de este cierre no depende de que MEXC conserve abierta la bóveda.
Tras reiniciar no hay autostart ni claves
guardadas. Las solicitudes y recibos se recuperan; se requiere otra confirmación
y claves para continuar. Un presupuesto agotado o intento PREPARED que exige
revisión no se presenta como una continuación operativa en la UI.

El registro actualiza su presentación sólo si cambian los datos visibles; los
timestamps de sondeo no reconstruyen los botones ni eliminan el foco del teclado.
La firma aparece por encima del registro y la parada junto a la cabecera del
grupo, para que nuevas auditorías/recibos no desplacen esos controles.

Este recorrido propaga una decisión CLOSE PAPER comprobada y calcula la cantidad
a reducir desde recibos TESTNET. Aún no es un bot GRVT completo: falta observar
nuevas señales/entradas automáticamente y ejecutar SL/TP/trailing/permanencia
sobre los fills reales. Su evaluación puntual se describe a continuación.
La representación de estas ejecuciones TESTNET en el
gráfico, el rearme operativo y los pilotos del proveedor vivo siguen pendientes.

## Evaluación de salidas sobre fills TESTNET

SQLCipher v43 incorpora `grvt_testnet_bracket_strategies`. Al enviar una nueva
entrada desde PAPER se archivan definición normalizada, motor, temporalidad,
fuente y huella del grupo **antes del claim y del POST externo**. El replay
PAPER mantiene estable el origen mientras se captura. Una lectura o repetición
no sustituye la definición, aunque después se edite LAB. La migración conserva
los grupos antiguos sin inventar para ellos una definición histórica. El backup
verifica estas filas y su relación con el grupo.

`POST /real/exchanges/grvt/testnet/brackets/{parent}/strategy-exit/review` y
«Evaluar salidas» consultan la definición archivada, los tres contratos originales
y los fills de entrada. Se exige entrada completa, historial de fills coincidente,
ninguna salida nativa ejecutada, ninguna actividad ajena ni reducciones posteriores.
Al terminar se comprueba otra vez que los estados no hayan cambiado. LAB/PAPER
pueden permanecer bloqueados. No necesita clave de firma ni cambia las órdenes.

El evaluador `grvt_filled_strategy_exit.py` utiliza `Decimal` con precisión local
100. El precio de entrada es `sum(size * price) / sum(size)` de los fills auditados.
SL y TP son porcentajes sobre ese precio, sin sustituirlo por el límite firmado o
el precio PAPER. El trailing usa únicamente máximos/mínimos de velas completas
cuya apertura sea posterior a la vela del **último fill**; excluye toda la vela
de ejecución porque sus extremos pueden ser anteriores. La permanencia vence
en `último_fill_ns + max_holding_bars * intervalo_ns`, sin redondear hacia atrás
a la señal. Las reglas de salida reutilizan el evaluador LAB del motor actual
sobre la última vela completa. Prioridad: stop/trailing, objetivo, regla, tiempo.

`grvt_testnet_exit_market.py` consulta exclusivamente ticker/kline TESTNET;
`mark_price` es obligatorio y no se sustituye por `last_price`. El timestamp del
mark fija el último intervalo completo y toda la revisión caduca tras 5 s.
El prefijo de indicadores queda fijado a la entrada: al menos 500 intervalos,
ampliados según la regla de salida. No se desplaza entre consultas. El límite
actual de 5000 velas, la falta de calentamiento o cualquier hueco producen error,
sin truncar el histórico ni declarar «Mantener». No se mezclan velas mainnet.
Referencia de campos y endpoint: [API de mercado GRVT](https://api-docs.grvt.io/market_data_api/).

La respuesta incluye fills, huellas de estrategia/histórico, niveles y motivo;
la UI muestra la hora y «Consulta puntual · no ejecuta». La evaluación no es una
prueba de saldo, de liquidación ni de rentabilidad neta. No cancela protecciones,
no registra un CLOSE PAPER artificial y no concede por sí misma operación automática.
El seguimiento activable de esas salidas se describe a continuación.
Las ejecuciones que carecen de definición prearchivada no ofrecen esta acción.

## Seguimiento de salidas sobre fills — TESTNET

Después de una entrada protegida con definición archivada, «Seguir salidas»
permite autorizar el seguimiento de **esa posición**, sin abrir otras. La ruta
privada `POST .../brackets/{parent}/strategy-exit/start` exige bóveda visible,
confirmación `ACTIVAR_SALIDAS_ESTRATEGIA_GRVT_TESTNET`, API key y firma dedicada
TESTNET con firmante coincidente. Devuelve 202; el registro muestra «Observando»,
último tick, contador y plazo. «Detener seguimiento» usa la parada existente.
No se necesita que LAB/PAPER permanezcan desbloqueados ni una señal CLOSE PAPER.

La autoridad es efímera: 30–86400 s y 1–3 reducciones máximas, con vencimiento
anterior al de la protección nativa y 30 s de margen. No se amplía al consultar
estado. Claves y cliente viven sólo en la hebra; ni reiniciar ni desbloquear
restaura credenciales o arranca un trabajo. Bloquear, proteger la vista o detener
revoca también un inicio pendiente. La parada deja de ejecutar; no equivale a
cerrar la posición. Expirar sin señal conserva las órdenes nativas existentes;
su vigencia sigue limitada por los términos firmados del grupo.

`grvt_exit_watcher.py` recibe snapshots públicos exclusivamente de
`wss://market-data.testnet.grvt.io/ws/full`, con instrumento y mark verificables.
No sondea repetidamente el ticker REST. Reutiliza las mismas velas cerradas
entre ticks y descarga el histórico sólo cuando cambia el intervalo cerrado.
Un mark viejo, regresado, contradictorio o un hueco superior a 5 s detiene el
trabajo; no inventa una operación retrospectiva ni se reconecta automáticamente
para seguir enviando. El estado pasivo y STOP no esperan una autenticación/RPC
bloqueada. Referencia del host: [API de mercado GRVT](https://api-docs.grvt.io/market_data_api/).

Ante una salida, vuelve a contrastar contratos/fills y guarda en la bóveda la
solicitud `orbis.grvt.testnet.strategy.close.v2`: plan, snapshot, observación
nativa, fills, mark, velas completas, huellas, decisión y límites. El registro
y STOP se confirman en una transacción. SQLCipher v44 identifica este formato
nuevo; sigue leyendo solicitudes v1. Su migración conserva la preimagen cifrada
y el backup reproduce el cálculo, no sólo comprueba la huella JSON exterior.
No añade asientos PAPER ni REAL patrimoniales a partir de estas simulaciones.

Desde ahí usa el mismo coordinador: retirar protecciones una sola vez, dos
auditorías separadas, recibo de grupo, reducción del residuo y recibo IOC.
Un parcial puede consumir otro ID dentro del presupuesto original, con un
transporte nuevo; nunca se resetea un cliente consumido ni se reenvía un ID
incierto. Los residuos inferiores al mínimo del instrumento requieren revisión.
«Completado» exige posición plana conciliada, no un acuse. Una respuesta HTTP
local perdida se resuelve consultando el estado, sin repetir la activación.

La revisión incluye `control_revision` y la activación debe devolver ese par
como `permission_revision`. STOP lo revoca incluso si aún no hay trabajo:
una petición anterior que llegue después se rechaza antes de autenticar con
el proveedor. El estado de cada trabajo identifica su revisión; una respuesta
vieja no confirma una activación distinta. Esta revisión no sustituye al token
de bóveda ni a los controles del envío.

La UI limita las esperas de activación/parada a 5 s y la recuperación GET a
3 s; el historial también tiene un límite de 5 s. Al perder la respuesta
muestra «Activación sin confirmar», mantiene disponible STOP y no reenvía
el inicio. Aplica el acuse de parada sin esperar el historial y descarta una
consulta anterior a una mutación posterior. Los límites son de espera HTTP:
un timeout no demuestra que la operación haya fallado o terminado.

La solicitud de cierre sobrevive al reinicio. Para continuar requiere otra
confirmación y firma, conservando plazo e intentos originales. La autoridad y
las credenciales siguen siendo efímeras; el diario descrito abajo conserva
lo observado. El cierre nativo completo se incorpora mediante la conciliación
descrita abajo. Siguen pendientes nuevos ciclos de entrada/rearme y los pilotos
Trade testnet/mainnet. No se presenta esta función como un bot mainnet terminado.

### Diario persistente del seguimiento — v45

`grvt_exit_watch_archive.py` conserva en SQLCipher un registro por activación
y una cadena de eventos. Guarda inicio, versión/huellas de estrategia y plan,
plazo e intentos originales, cada precio evaluado con su tiempo/acción/motivo,
fases del cierre y parada/final cuando pueden confirmarse. Los precios son
texto decimal exacto. No guarda credenciales, firmas, direcciones ni autoridad
reutilizable. Cada activación expresa tiene otro identificador.

El registro inicial se confirma antes de autenticar con el proveedor. Una
observación se escribe antes de permitir que provoque un cierre; un fallo de
escritura detiene ese recorrido. STOP revoca primero en memoria. Su escritura
es oportunista y no espera un cerrojo retenido por red; si la bóveda ya está
bloqueada o no puede confirmar el final, no se inventa un evento terminal.
Al reabrir, un registro sin terminal aparece como «Interrumpido», con el
timestamp desde el que no se acredita seguimiento. No se reconstruyen señales
durante ese hueco. Un estado COMPLETE requiere recibo plano conciliado y no
posterior al evento, además de la integridad de la cadena.

«Historial de seguimiento» muestra las activaciones y «Observaciones» permite
consultar páginas de eventos (20 activaciones/100 eventos por página). Las
rutas privadas GET `/{parent}/exit-watches` y `/{parent}/exit-watches/{id}` son
pasivas y llevan `no-store`. El estado actual vive en RAM; después del reinicio
se carga el resumen archivado como inactivo y con cero permiso restante.
No reconstituye la revisión efímera de autorización ni conecta el proveedor.

La v45 añade `grvt_testnet_exit_watches` y `grvt_testnet_exit_watch_events`.
La migración preserva la preimagen cifrada y comprueba recuentos; las copias
verifican referencias, secuencias, cabeza/recuento, relojes y resúmenes derivados.
Las huellas detectan alteraciones accidentales; no autentican frente a quien
controle la base y pueda recalcular toda la cadena. Los motivos/precios de
observación no son un dataset completo para reproducir cada HOLD: las velas y
el cálculo completo se archivan en la decisión v2 de cierre. Quedan por medir
el coste de lectura y la retención de diarios de sesiones prolongadas.

### Conciliación automática de una salida nativa completa

`grvt_native_exit.py` distingue una salida nativa del CLOSE calculado por la
estrategia. Consulta los tres contratos antes de abrir el stream, también
cuando ya cerraron antes de activar el seguimiento. Mientras llegan marks,
comprueba los IDs con un intervalo mínimo de 5 s; el lector de evidencia
también detecta un fill nativo aparecido durante la revisión. La cotización
mark no se usa como prueba de ejecución: los disparadores nativos firmados
usan LAST. No se infiere que se ejecutó una orden porque el gráfico tocó un nivel.

Sólo toma este recorrido si la entrada FOK está llena y las ejecuciones
nativas explican su cierre completo, sin contradicciones de estado. Cierra
el stream, mantiene STOP local de la subcuenta y espera a que los tres
miembros sean terminales. Reutiliza las auditorías financieras del grupo:
dos lecturas estables separadas al menos 5 s, cobertura de fills/comisiones/
financiación y cuenta plana conciliable. Entonces emite el recibo inmutable
y el terminal COMPLETE en el diario v45. No crea un CLOSE PAPER, una solicitud
v2 artificial ni asientos patrimoniales REAL.

Este recorrido no firma ni envía cancelaciones/reducciones: sólo consultas al
proveedor y escrituras locales de STOP, auditorías, recibo e historial. Si el
exchange aún mantiene una hermana abierta, espera dentro del permiso original;
no la declara cancelada ni la cancela de oficio. Un parcial utiliza ahora el
recorrido v48 descrito más abajo; estados incoherentes o una posición de cuenta
no explicada requieren revisión. STOP, bloqueo o TTL
interrumpen también las esperas entre auditorías. Hay un límite de reloj
monotónico además del plazo comprobado por el servicio. Conciliar no levanta
STOP ni arma otra entrada.

Para activar este seguimiento se mantienen las comprobaciones de protección
y vencimiento originales: no habilita un nuevo seguimiento fuera de la ventana
admitida de protección. La recuperación manual de grupos vencidos conserva
sus acciones de observar/auditar/conciliar. Las huérfanas y las protecciones
vencidas compatibles utilizan ahora el recorrido v49 descrito más abajo.

## Parcial nativo y reducción conciliada, v48

La [API oficial de GRVT](https://api-docs.grvt.io/trading_api/) permite que una
orden OPEN tenga cantidad parcialmente ejecutada. ORBIS no equipara OPEN con
cantidad cero ni FILLED con un parcial. La entrada FOK debe seguir totalmente
llena; sólo una salida SL/TP puede explicar el parcial, sin otros avisos.

El permiso de seguimiento permite registrar
`orbis.grvt.testnet.native.partial.close.v1`: referencia inmutable de la
observación, motivo SL/TP, plazo e intentos. El estado observado no sustituye
fills ni cuenta. Tras la cancelación única de las protecciones que continúen
activas, las auditorías financieras terminales del grupo explican fills,
comisiones, financiación y saldo. El recibo estable fija el residuo exacto
para los IOC reduce-only. Cada IOC exige su propia conciliación antes de otro.

PARTIAL_EXIT mantiene STOP antes de solicitar ese cierre y no es rearmable.
AUTO_EXIT del mismo padre sólo se registra al guardar la solicitud. Una parada
manual o incidencia previa sigue enclavada. Si el parcial termina de llenarse
antes de esa solicitud se concilia sin otro envío. Si llegan fills adicionales
durante la cancelación se reduce el residuo posterior. Una respuesta perdida
se recupera por consultas; no se repite el POST. El bot acotado v47 puede seguir
al próximo ciclo sólo después del recibo plano y dentro de su permiso original.

## Recuperación de huérfanas y protección vencida, v49

La solicitud `orbis.grvt.testnet.native.recovery.close.v1` guarda el disparador
observado y los límites del cierre, sin atribuir fills ni saldo a un estado de
orden. Los recorridos admitidos y las exclusiones se detallan en
[GRVT_BOT_TESTNET.md](GRVT_BOT_TESTNET.md). Los casos sin exposición requieren
recibo plano y no generan IOC. Los residuos se reducen sólo después de la
conciliación financiera terminal, nunca por el tamaño de la señal PAPER.

La [especificación oficial de BulkCancel](https://api-docs.grvt.io/schemas/api_bulk_orders_request/)
describe la cancelación del grupo a partir de un miembro. ORBIS reutiliza ese
transporte acotado y observa los tres contratos después: ni la respuesta ni
el reloj local acreditan que las salidas hayan desaparecido. No vuelve a
enviar una cancelación perdida. Si siguen activas, no declara COMPLETE ni
reduce una cantidad sin conciliar.

El permiso original no se extiende al caducar. **Recuperar grupo** permite
autorizar sólo recuperación de esa posición, entre 30 y 300 s, con confirmación
propia. Su diario v2 registra modo RECOVERY y prohíbe observaciones de mercado;
no abre streams ni toma nuevas señales. Un grupo sano no se cancela por usar
esa acción. Los cierres ya registrados conservan plazo e intentos; los casos
con un permiso anterior vencido o cancelación previa mantienen la recuperación
manual existente, sin inventar un rearme automático. STOP manual o revisión
anterior sigue enclavado aun después del cierre.

## Pendientes del ciclo automático y mainnet

1. Ampliar conciliación a financiación tardía tras un recibo y otros
   movimientos no vacíos explicables, sin reinterpretar recibos anteriores.
   Actualmente bloquean. No se soportan otras divisas/modos de margen ni se
   declara conciliación causal general.
2. Completar nuevos permisos tras un cierre anterior vencido y movimientos
   externos. La continuidad acotada v47, parciales v48 y recuperaciones
   compatibles v49 están implementadas; no cubren esos casos restantes.
3. Piloto humano Trade testnet y después autorización/custodia operativa
   limitada y verificación específica de mainnet. Sin automatización GRVT
   real activa ni piloto con fondos del usuario por este bloque.

El rearme acotado de nuevas entradas se documenta en
[GRVT_BOT_TESTNET.md](GRVT_BOT_TESTNET.md). Reanudar un cierre ya solicitado
requiere confirmación y conserva sus límites; conciliar por sí solo no levanta
STOP ni reactiva una estrategia. No hay autostart mainnet.

Las pruebas de protección, OCO, cancelación y finanzas usan firma real con una clave
ficticia y HTTP sustituido únicamente en la frontera externa. No prueban
aceptación del proveedor vivo ni gastan fondos. La batería existente del
puente IOC, diario, auditorías y conciliación debe mantenerse correcta.
