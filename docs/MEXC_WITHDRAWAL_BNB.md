# Retirada MEXC → wallet, USDT/BSC

Estado: preparación, autorización puntual, POST único, recuperación por View,
recepción RPC desde wallet u ORBIS y contabilidad explícita implementados y verificados con
exchange/wallet ficticios. Un acuse MEXC no significa dinero recibido. La lectura RPC es una observación, no una prueba independiente de consenso.
**Pendiente el ensayo humano mainnet**; no se ha probado con claves ni dinero reales.

## Recorrido disponible

1. Desbloquear REAL, conectar View y seleccionar su libro MEXC.
2. Conectar la wallet en BNB Chain, verificar control EOA y consultar USDT.
3. Guardar previamente esa dirección USDT/BSC en MEXC mediante sus propios
   controles. ORBIS consulta la lista completa y exige una coincidencia única
   sin memo. No añade destinos ni cambia la seguridad del exchange.
4. En «Retirar a wallet · BNB», «Parar operativa» detiene nuevos envíos y bots.
   No liquida posiciones ni cancela órdenes ya transmitidas. Resolver primero
   reservas/órdenes inciertas; la retirada exige STOP y ningún bot activo.
5. Introducir importe USDT y filtro previo de comisión. «Preparar retirada»
   consulta red BSC habilitada, contrato, comisión/mínimo/máximo/múltiplo y
   destino guardado; comprueba identidad View antes/después y saldo libre.
   El efectivo USDT del libro debe coincidir con View y no estar bloqueado.
6. El plan cifrado dura dos minutos. Fija cuenta, red, activo, contrato,
   huella del destino, precisión, unidades, revisión del libro y fecha de STOP.
   No realiza un POST al prepararlo. «Descartar plan» sólo cancela planes que
   aún no se intentaron; no cancela una retirada en MEXC.
7. Revisar el destino y la previsión. Introducir localmente una credencial
   independiente con **únicamente** `SPOT_ACCOUNT_READ`, `SPOT_WITHDRAW_READ`
   y `SPOT_WITHDRAW_WRITE`. La clave Trade del bot no se reutiliza ni se admite
   una clave que mezcle trading/retiradas. No enviar secretos por el chat.
8. «Solicitar retirada» exige otra confirmación explícita, comprueba UID igual
   a View, vuelve a consultar términos/saldo y consume el permiso antes del
   único POST. Puede haber comprobaciones, restricciones y esperas adicionales
   de MEXC que ORBIS no sortea. No operar esa cuenta desde otro cliente mientras
   se revisa/ejecuta este flujo: las observaciones externas no son atómicas.
9. «Comprobar recepción» consulta View con el mismo UID. Busca exactamente el
   `withdrawOrderId` propio o el ID de acuse guardado, incluso tras perder la
   respuesta del POST. Una ausencia no prueba rechazo. Exige retirada externa
   con estado 7, destino original y hash inequívoco. Consulta transacción,
   recibo, bloque canónico y bloque `finalized` mediante la wallet en BNB.
   Comprueba contrato USDT fijado, una única entrada al destinatario y las
   cantidades; no acepta salidas simultáneas de ese token desde la wallet.
   Un lote con otras wallets sí es válido; varias entradas propias ambiguas no.
10. «Contabilizar retirada» permite elegir traslado a otro libro wallet, salida
    fuera de ORBIS o vincular un asiento ya existente. Vuelve a consultar View
    y exige saldo libre exactamente explicado por el libro, neto y comisión.
    No mueve dinero: registra una única relación contable y mantiene STOP.
    La cartera y su historial se actualizan después de comprobar el resultado,
    también si se perdió la respuesta de registro; no depende del sondeo del bot.
    Una nueva apertura revisada y una autorización explícita siguen siendo
    necesarias para volver a operar o rearmar el bot.

Las credenciales sólo viven durante esa petición; no se conectan al bot,
persisten ni reaparecen tras bloquear. Los campos se limpian al terminar.
Cambiar wallet/red/cuenta/sesión descarta el contexto local, no una solicitud
externa ya iniciada. Desbloquear de nuevo no arranca ni reenvía retiradas.

### Calcular máximo

En **Retirar a wallet · BNB**, pulsa **Calcular máximo**. Si el filtro está vacío, ORBIS utiliza la comisión observada. La propuesta reserva el filtro en el saldo libre conciliado, respeta el máximo de la ruta y baja el importe al múltiplo publicado y a la precisión admitida. Comprueba que se cubran el mínimo y una recepción positiva. [Catálogo de MEXC](https://www.mexc.com/api-docs/spot-v3/wallet-endpoints/query-the-currency-information).

El cálculo rellena el formulario y muestra el débito y el neto previstos. **Preparar retirada** vuelve a consultar los datos antes de guardar un plan. **Solicitar retirada** conserva la credencial separada y la confirmación explícita. MEXC puede modificar su comisión. [Parámetros de retirada](https://www.mexc.com/api-docs/spot-v3/wallet-endpoints/withdrawnew).

### Revisar un plan guardado

Tras recargar ORBIS, desbloquea REAL y elige la cuenta original. Conecta su
MEXC View, verifica el control de la misma wallet en BNB Chain y consulta USDT.
En **Retirar a wallet → Consultar retiradas**, elige **Revisar plan** mientras
siga vigente. ORBIS verifica los términos guardados y vuelve a cotejar la
ruta, la comisión, el saldo y STOP.

La revisión conserva el identificador y el vencimiento de dos minutos desde
la preparación. Si la revisión falla por un cambio de condiciones, consulta
el historial y prepara un plan nuevo tras resolver el anterior. Para ejecutar
la retirada se mantiene **Solicitar retirada**, con credencial y confirmación
independientes. La revisión por sí sola no envía ni reserva de nuevo.

### Consulta desde ORBIS

Si la wallet no devuelve el recibo, usa **Consultar en BNB** en el historial de la retirada. Verifica la wallet de destino y conecta View de la cuenta vinculada. ORBIS consulta el hash que confirma MEXC y comprueba la transacción en su índice del bloque finalizado. El RPC recibe el hash y las referencias de bloque. La dirección se coteja localmente con la transferencia registrada en el recibo. [API de finalidad de BNB Chain](https://docs.bnbchain.org/bnb-smart-chain/developers/json_rpc/bsc-api-list/#economic-finality-api)

La fuente del recibo se conserva en el historial. Una consulta pendiente permite volver a comprobar. Para registrar el movimiento usa **Contabilizar retirada**. STOP se mantiene hasta revisar y autorizar otra apertura.

## Comisiones: comprobación local, no garantía del exchange

La API oficial no documenta un parámetro de comisión máxima ni un presupuesto
de débito. El campo de la interfaz filtra la comisión **observada antes** de
la petición; no impone un límite en MEXC. La confirmación lo advierte y exige
aceptar `fee_quote_only=true`. No se promete que el exchange no pueda variar
su comisión después de la última lectura.

Para no inventar si la comisión se descuenta del importe o se añade, la
previsión reserva conservadoramente `importe + filtro` de saldo y muestra
`importe - filtro` como recepción prevista inferior, siempre positiva. Son
previsiones del modelo, no condiciones que MEXC se comprometa a ejecutar.
Ninguna previsión se transforma en un gasto, rentabilidad o asiento REAL.

La recepción fija el neto real y la comisión USDT publicada por View. Admite
comisión descontada (`neto = importe - comisión`) o añadida (`neto = importe`),
si el importe informado por MEXC coincide con el solicitado, neto o débito.
`débito = neto + comisión` debe explicar exactamente el saldo posterior.
Si la comisión/débito supera el filtro previo o el neto resulta inferior,
conserva los hechos pero exige aceptar expresamente el exceso al contabilizar.
El gas de la transacción del exchange no se añade como si lo hubiera pagado
la wallet del usuario. Una interpretación no cubierta requiere revisión,
no un redondeo o ajuste silencioso del saldo.

`withdrawIntegerMultiple` se trata como el incremento publicado por el
catálogo, sin reinterpretar un entero como número de decimales ni redondear
el importe. Si es nulo no se inventa una precisión adicional; se respetan las
unidades exactas del token y MEXC puede rechazar restricciones no informadas.
Estas convenciones siguen requiriendo contraste humano mainnet antes de
dar por verificada una retirada real completa.

## Estados, bloqueo y recuperación

`PREPARED` puede pasar a `CANCELLED` sin envío o a `SUBMISSION_UNKNOWN` al
consumir su permiso durable. Sólo un acuse válido añade `ACKNOWLEDGED`.
La ventana entre guardar el permiso y llamar a red ya es incierta si el proceso
se interrumpe; no se reutiliza por timeout. Todo fallo HTTP, acuse malformado
o respuesta perdida exige revisar el historial de MEXC, nunca repetir el POST.
`withdrawOrderId` es una referencia estable, no una garantía asumida de
idempotencia del proveedor. El transporte también consume su permiso en
memoria bajo cerrojo y no tiene reintentos ni redirecciones.

Una retirada incierta o aceptada mantiene STOP y bloquea nuevos planes y la
reapertura manual/del bot. Un preparado vigente también bloquea reabrir; puede
descartarse o caducar. Una retirada intentada **no caduca ni se borra** para
permitir volver a operar. La recepción sin asiento no libera ese bloqueo.
Tras contabilizar, permite revisar la apertura, pero nunca quita STOP sola.
El permiso original sigue consumido incluso después de resolver el seguimiento.

Un estado 8/9 permite «Revisar rechazo» únicamente sin hash en ninguna captura,
sin comisión, sin recepción y sin cargo: libro sin cambios y saldo libre igual
al original. Esa resolución no crea asientos ni reenvía. Un timeout, una ausencia
en el historial, un hash observado o un saldo inexplicado mantienen la revisión.
Las contradicciones posteriores de identidad/red/destino/estado final/importe/
comisión/hash se conservan y detienen la cuenta; no se elige el dato conveniente.

Las consultas de recuperación abarcan 7, 30 o hasta 90 días según la antigüedad,
con ventanas completas y límites de densidad. Fuera de cobertura o si faltan
referencias inequívocas no se adivina una relación. Si MEXC omite `network`,
la evidencia lo indica: BSC procede del plan propio, hash exacto y RPC de la
cadena, no de que un símbolo o una dirección sean compatibles con varias redes.

## Persistencia y concurrencia

SQLCipher v28 añade `real_mexc_withdrawals`. Términos inmutables y estados
con huellas/cronología; dirección y referencia externa sólo como HMAC con sal
secreta local. Guarda activo/red y versión del consentimiento sobre comisión
previa. No almacena direcciones de destino completas, UID, claves, firmas ni
cuerpos de peticiones. El contrato es metadato público y el request ID se
genera localmente. Migración sobre copia cifrada con preimagen y verificación
de registros en las copias de seguridad.

La v29 añade `real_mexc_withdrawal_observations`, `real_mexc_withdrawal_receipts`
y `real_mexc_withdrawal_bookings`. Conserva huellas HMAC de destinatario, hash e
identidades privadas; contrato y bloques son metadatos públicos. Las capturas
View y RPC originales no se guardan con direcciones completas. El recibo es
inmutable, ligado al plan y captura exactos. Las copias revalidan estas
relaciones y los asientos, preservando evidencia histórica ante conflictos
posteriores. La migración conserva solicitudes v28 en curso y una preimagen.

La contabilidad crea asiento(s) y vínculo en un único `BEGIN IMMEDIATE`, con
unicidad por solicitud, fuente y asiento. Repetir una petición o usar dos
conexiones no cobra dos veces. Un traslado interno lleva sólo el neto a la
wallet y registra la comisión una vez en origen, sin nueva aportación ni
retirada de capital. Una salida fuera de ORBIS separa neto retirado de comisión:
el principal no es una pérdida; la comisión sí es coste realizado. Sólo estas
dos salidas de efectivo admiten esa comisión, con precisión decimal exacta.
Las relaciones manuales previas se respetan: hay que enlazar el mismo asiento,
no crear otro para una retirada ya contabilizada.

La interfaz limita a 3 s la espera de la respuesta de **contabilizar**. Un
timeout no demuestra fallo ni revierte la transacción: muestra estado no
confirmado y consulta el registro, sin repetir ese POST. Las lecturas del
historial tienen hasta dos intentos de 2 s; si ninguno se confirma, se muestra
el error y se conserva la posibilidad de consultar de nuevo. La revisión de
lectura impide que una respuesta anterior repinte «sin contabilizar» después
de confirmar el asiento. Esto no cambia los plazos ni la concesión de un
envío de fondos a MEXC: afecta al registro local y a su recuperación visual.

`BEGIN IMMEDIATE` serializa preparación y concesión entre procesos. La
reapertura comprueba retiradas pendientes en su transacción final, antes de
crear la política y quitar STOP. El coordinador no adquiere `RealAccess` dentro
de la sesión SQL: comprueba referencias revocables, generación y plazo en
memoria, evitando inversión de cerrojos. El bloqueo espera un POST ya abierto
y su acuse; no puede deshacerlo. Los tests usan un servidor temporal cifrado,
no el almacenamiento del usuario.

## Fuentes revisadas el 30-10-2026

- [MEXC, nueva retirada y permisos](https://www.mexc.com/api-docs/spot-v3/wallet-endpoints/withdrawnew).
- [MEXC, catálogo de rutas, límites y comisión](https://www.mexc.com/api-docs/spot-v3/wallet-endpoints/query-the-currency-information).
- [MEXC, destinos guardados y paginación](https://www.mexc.com/api-docs/spot-v3/wallet-endpoints/withdraw-address-supporting-network).
- [MEXC, formato de cuerpo y firma Spot V3](https://www.mexc.io/api-docs/spot-v3/introduction).
- [MEXC, historial con red posiblemente ausente](https://www.mexc.com/api-docs/spot-v3/wallet-endpoints/withdraw-history-supporting-network).
- [MEXC, estados de retirada](https://www.mexc.com/api-docs/spot-v3/public-api-definitions).
- [BNB Chain, RPC y etiqueta finalized](https://docs.bnbchain.org/bnb-smart-chain/developers/json_rpc/bsc-api-list/).
