# Ingreso desde wallet a MEXC — USDT en BNB Chain

Estado: plan local cifrado, permiso de un solo uso y solicitud de transferencia
`transfer` BEP-20 mediante la extensión implementados. Probado con wallet y
MEXC controlados, no con fondos ni credenciales del usuario. También están
implementados el cotejo de recibo/abono y el registro contable explícito.
La retirada MEXC→wallet está implementada con View/RPC observado y asiento
explícito; véase su contrato al final. El ensayo humano mainnet sigue pendiente.

## Recorrido

1. Desbloquear REAL y elegir la cuenta MEXC, conectar su View y vincularla.
2. Conectar una extensión EVM en BNB Chain mainnet (`0x38`) y verificar su
   control EOA en esta sesión. Conectar o firmar ese mensaje no autoriza gasto.
3. En «Token», pulsar «USDT · BNB». ORBIS consulta el contrato actualmente
   publicado por MEXC para depósitos BSC habilitados y lee sus decimales y saldo
   en la red de la wallet. No presupone que BSC usa los seis decimales de USDT
   en otras redes ni identifica el token sólo por su símbolo.
4. En «Enviar a MEXC · BNB», introduce importe, gas máximo y el mínimo que
   muestra MEXC al elegir USDT en BNB Chain. «Consultar en MEXC» abre esa pantalla.
   Si MEXC indica que no hay mínimo, introduce 0. ORBIS compara el importe con
   el mínimo indicado en unidades del token antes de preparar el plan.
   «Preparar ingreso» revisa wallet/red, código EOA/token, saldo, nonce pendiente,
   simulación `transfer` que devuelve `true`, estimación de gas y gas disponible.
   Se utiliza un margen de gas del 20 %, sin superar dos millones de unidades
   ni el presupuesto indicado. Los cálculos usan enteros/Decimal exactos.
5. Revisar cantidad, contrato, destino y gas. Confirmar en ORBIS y después en
   la wallet. Antes de solicitar la firma se revalida el destino MEXC, identidad,
   nonce, sesión y vigencia. El plan permite iniciar la solicitud durante dos
   minutos; no cancela un diálogo de wallet ya abierto cuando ese tiempo acaba.
   Si cambia el mínimo indicado, actualiza el campo y prepara un plan nuevo.
6. La wallet conserva claves y autorización final. ORBIS no usa `approve`,
   Permit2, semillas, claves de retirada ni un firmante propio. Si se cambian
   nonce, importe o comisión en la wallet, quedan fuera del plan local revisado.
7. El hash recibido se registra como referencia, no como recibo confirmado.
   Comprobar actividad de wallet y acreditación MEXC antes de contabilizar o
   utilizar capital nuevo. No se crea una aportación ni un beneficio por
   preparar, solicitar o recibir un hash.
8. «Comprobar ingreso» solicita el txId original y lee transacción, recibo,
   bloque canónico y bloque `finalized` desde el proveedor de la wallet. El
   backend valida cadena 56, envío legacy exacto (sin cambios en nonce, gas,
   precio, contrato, destino ni unidades), estado correcto y un único evento
   `Transfer` del token esperado. El bloque debe concordar y estar finalizado.
   Si el RPC no admite finalización, el recibo está pendiente/fallido o cambió
   lo firmado, se exige revisión: no se reenvía ni se contabiliza.
9. ORBIS obtiene otra captura MEXC View con identidad de cuenta estable y
   contratos BSC del catálogo. Exige depósito acreditado (estado 5), misma red,
   contrato, destino, txId, unidades y hora compatible. Capturas contradictorias,
   datos de destino ausentes o estado regresivo bloquean la confirmación.
   El recibo aportado por el navegador no puede acreditar por sí solo MEXC.
10. «Contabilizar» ofrece un asiento coincidente ya registrado (aportación o
    traslado de efectivo USDT) o «Fondos nuevos · fuera de ORBIS». Esta última
    opción exige confirmar que no estaban en otra cuenta y la misma wallet
    original, sin vínculo local previo. Una wallet vinculada a una cuenta
    separada ofrece «Desde [cuenta] · traslado USDT»: revisa saldo anterior,
    importe y saldo restante, y registra ambos lados con una confirmación.
    Si el traslado ya estaba registrado, se enlaza su entrada existente.
    No se inventa una conversión desde un activo valorado en EUR.
    La aportación no es beneficio; enlazar un asiento no añade saldo.

El resultado de cadena se etiqueta `WALLET_RPC_OBSERVED`: comprueba coherencia
de respuestas del RPC elegido por la wallet, no una prueba de consenso ni una
verificación independiente del nodo. Se necesita además el abono View MEXC.
El gas real se conserva en wei/BNB como evidencia, separado de USDT: todavía
no crea automáticamente un gasto en la cuenta BNB de origen. Si se lleva ese
saldo en ORBIS, requiere ajuste/revisión explícita. Confirmar el depósito no
autoriza un bot ni sustituye la paridad y límites de operativa Spot.
Un asiento ajeno al bot puede invalidar su apertura/paridad: si mantiene
exposición, el rearme conserva la guarda de asignación y puede exigir revisión.
No se atribuye el depósito nuevo a su posición ni se amplía su presupuesto.

La identidad del token y el destino dependen de la ficha actual de MEXC y de
la wallet/RPC. No se audita bytecode histórico ni se promete ejecución,
finalización, ausencia de impuestos del token o acreditación del exchange.
Se rechazan redes, memo, contratos, catálogos o destinos ambiguos/no activos.
No se usa automáticamente otra red cuando BSC no esté disponible.

## Wallet registrada: traslado contable integrado

La vista previa local requiere desbloqueo y control EOA vigente de la wallet
original en BNB Chain. Deriva la cuenta de origen de su vínculo local; no
permite elegir arbitrariamente otra cuenta ni usar una cuenta vinculada a
MEXC como si fuera una wallet. Sin wallet conectada se pueden seguir enlazando
asientos existentes, pero no se ofrece una aportación o traslado nuevos.

`NEW_INTERNAL` usa exclusivamente efectivo USDT ya registrado. La vista previa
reproduce el historial en la fecha del abono MEXC; saldo insuficiente, capital
posterior o activos/divisas distintos requieren revisión. La revisión del libro
de origen se comprueba de nuevo dentro de `BEGIN IMMEDIATE`; si cambió, hay que
abrir otra vista previa. Un asiento compatible sin vincular obliga a revisarlo
y enlazarlo antes de crear otro traslado. Los asientos ya asignados a otros
depósitos no se ofrecen como candidatos de este ingreso.

Salida de wallet, entrada MEXC y vínculo con la acreditación se confirman juntos.
Una excepción revierte los tres; dos procesos o una respuesta perdida devuelven
el mismo registro, no otro débito. Ambos asientos usan la fecha contable del
abono View; la hora/bloque del envío permanece en la evidencia de cadena.
No se crea aportación, ganancia ni gasto de gas ficticio. El gas BNB sigue
documentado aparte, pendiente de un ajuste explícito si se lleva su saldo.

Se conservan UUID y huella del lado de salida, nunca la dirección completa.
Lecturas y backups verifican ambos lados y su relación con la evidencia. Quitar
el vínculo wallet actual no reescribe una transferencia histórica. No añade
tablas ni cambia el esquema SQLCipher v29; las relaciones anteriores conservan
su formato y semántica. La cartera se actualiza tras registrar o recuperar una
respuesta incierta, sin exigir otra recarga ni autorizar operativa.

## Estados y reintentos

```text
PREPARED ── permiso durable único ──> WALLET_REQUESTED
                                       ├─ hash comunicado ──> HASH_REPORTED
                                       └─ error 4001 ──────> WALLET_REJECTED
```

`WALLET_REQUESTED` incluye el tramo entre guardar el permiso y recibir un
resultado. Si se interrumpe allí, no se supone que no hubo emisión. Un replay
del mismo ID nunca concede otra llamada a `eth_sendTransaction`. La concesión
se serializa entre procesos mediante `BEGIN IMMEDIATE`; una respuesta perdida
tampoco se reemplaza por una nueva concesión. No hay timeout/reenvío automático
para la llamada de gasto. Las lecturas RPC sí tienen timeout.

Otra solicitud de la misma wallet queda bloqueada mientras exista una sin
resultado. Un hash comunicado no prueba la cadena, pero mantiene reservado
ese nonce frente a otra solicitud. Una transferencia adicional requiere un
plan explícito y otro nonce observado. El rechazo 4001 se conserva como
informe de la wallet, no como prueba independiente de inexistencia on-chain.

«Consultar envíos» es pasivo y permite ver pendientes tras reiniciar, sin
conectar otra vez la wallet. «Registrar txId» recupera una referencia obtenida
de su actividad; no transmite nada, no verifica un depósito y no permite
sustituir un hash ya registrado por otro. Un resultado desconocido exige esa
revisión; no se limpia por tiempo ni con «Nuevo plan».

Cambiar red/cuenta/token, perder control EOA o bloquear REAL invalida el plan
de la pestaña. No revoca una solicitud de firma ya entregada a la extensión,
ni una transacción emitida: rechazar el diálogo en la wallet cuando proceda.

### Recuperar un txId en BNB

En **Enviar a MEXC · BNB → Consultar envíos**, elige **Buscar en BNB**. Conecta y verifica la misma wallet en BNB Chain. Si el envío está finalizado y coincide con el plan, ORBIS guarda su referencia y la usa para **Comprobar ingreso**. La comprobación de MEXC View y **Contabilizar** siguen separados.

El nodo BNB configurado recibe la dirección para consultar el nonce. ORBIS coteja el bloque finalizado, la transacción, el recibo, el destino, el importe y el gas. La búsqueda es acotada a 262.144 bloques y depende de la cobertura histórica del nodo. Un recibo pendiente o un reemplazo fuera de los casos reconocidos conserva la revisión. Para operaciones fuera del tramo consultado se puede usar **Registrar txId**. [RPC de BNB Chain](https://docs.bnbchain.org/bnb-smart-chain/developers/json_rpc/bsc-api-list/)

### Comprobar el ingreso desde ORBIS

En el historial de un ingreso con txId registrado, pulsa **Comprobar en BNB**. Requiere verificar la wallet de origen y conectar View de la cuenta MEXC vinculada. Si el txId sigue en la vista, el RPC recibe el hash y referencias de bloque. Si falta al desbloquear, la búsqueda por nonce consulta la dirección pública de origen al nodo BNB. ORBIS verifica el bloque finalizado, el plan original y el abono de MEXC. [API de finalidad de BNB Chain](https://docs.bnbchain.org/bnb-smart-chain/developers/json_rpc/bsc-api-list/#economic-finality-api)

El historial conserva la última consulta que coincide con el recibo y los datos del ingreso. Puede mostrar importe insuficiente, preabono, restricción, revisión, devolución indicada u otro estado comunicado por MEXC. La hora de consulta permite distinguir la última observación de una lectura actual. [Estados de depósito de MEXC](https://www.mexc.io/api-docs/spot-v3/public-api-definitions#deposit-status).

Para actualizar ese estado, pulsa **Comprobar ingreso** o **Comprobar en BNB**. Tras verificar el abono de MEXC, usa **Contabilizar** para registrar el saldo en la cartera.

### Cierre en BNB

**Buscar en BNB** también registra un envío original revertido o una cancelación ya finalizada. Para la cancelación se verifica la firma de una transacción de valor cero a la misma wallet, con el nonce del envío, datos vacíos y recibo sin movimientos de tokens. El cierre registra el gas observado en BNB. No crea un abono ni asientos. Para otro intento hay que preparar un plan con un nonce posterior. [Cancelación por nonce en MetaMask](https://support.metamask.io/manage-crypto/transactions/how-to-speed-up-or-cancel-a-pending-transaction)

La evidencia y las huellas se guardan en la bóveda. La búsqueda conserva el estado pendiente si el nodo no permite comprobar el recibo o si la transacción no encaja en estos casos. Un ingreso acreditado conserva su evidencia de MEXC y no admite este cierre.

### Guía Wallet ↔ MEXC

Al abrir la cuenta vinculada, **Ingresar** y **Retirar** llevan a sus paneles. La guía muestra el movimiento pendiente más reciente de cada tipo y su siguiente paso. Las indicaciones proceden del historial local y aparecen al desbloquear, aunque View esté desconectada. Las comprobaciones externas y el registro conservan sus requisitos de conexión y confirmación.

Al pulsar **Ingresar** o **Retirar** con un movimiento pendiente, la guía
abre la ficha exacta y la resalta en el historial local de la cuenta. Si la
ficha queda fuera de las cien entradas recientes, se incluye al abrirla desde
la guía. Las comprobaciones, firmas y asientos se inician desde sus propios
controles.

### Estimar gas

Introduce el importe y el mínimo de depósito que muestra MEXC para USDT en BNB Chain. **Estimar gas** comprueba el destino, simula la transferencia y rellena el límite editable en BNB. La estimación usa el precio de gas observado y un 20 % de margen en sus unidades. Cambiar el importe o la wallet descarta la estimación. **Preparar ingreso** vuelve a calcular el gas y exige que el resultado quepa en el límite. La firma se pide después, desde **Confirmar en wallet**.

### Ver recorrido

En **Enviar a MEXC · BNB**, abre el historial de un ingreso y pulsa
**Ver recorrido**. La bóveda REAL desbloqueada permite consultar los datos
de la cuenta sin reconectar la wallet ni MEXC View. **Ocultar importes**
conserva los estados y oculta saldos y comisiones del informe.

El tramo empieza al contabilizar el ingreso. Termina al solicitar la
primera retirada vigente o al contabilizar el siguiente ingreso USDT,
incluidos los registrados desde MEXC View. La compra y la venta requieren
fills conciliados del mismo par Spot y asientos en ese tramo. El informe
separa el crédito y el gas, los cambios de cada orden y el neto de la retirada.

Las nuevas retiradas ligan por HMAC la cuenta, el plan y su destino a una
huella compatible con el ingreso. El informe puede distinguir otra wallet
desde el archivo cifrado y exige recepción y asiento para dar por terminado
el regreso. Las retiradas antiguas sin esa huella conservan **Destino por
verificar**. Si verificas la wallet de origen en BNB Chain y vuelves a abrir
el informe, ORBIS compara su destino en el historial.

El informe identifica movimientos contabilizados en el tramo. La atribución
de unidades fungibles queda fuera de la comprobación.

### Ingresos directos MEXC

En **REAL → Wallet ↔ MEXC → Ingresos directos** puedes consultar los abonos USDT registrados desde MEXC View. Los abonos contabilizados en la misma confirmación aparecen juntos. Basta con desbloquear la bóveda REAL para consultar el informe.

El tramo empieza en la contabilización y termina ante el siguiente ingreso de capital registrado o la primera solicitud efectiva de retirada a través de ORBIS. Muestra una compra y una venta posterior del mismo par Spot cuando sus ejecuciones y asientos están verificados. La retirada de USDT por BNB Chain muestra si falta comprobar el recibo, contabilizarlo o si el asiento ya está hecho.

Los importes corresponden a cada movimiento de la cuenta y se ocultan con el control de privacidad. El origen figura como desconocido. El informe describe la secuencia de la cuenta y los fondos se contabilizan en un saldo común.

### Continuar el recorrido

Cuando el ingreso ya está contabilizado, **Ver recorrido** e **Ingresos directos MEXC** muestran el siguiente panel del tramo vigente. **Abrir operativa Spot** lleva a la compra o venta y muestra el diario si hay una orden pendiente. Cuando hay compra y venta verificadas, **Abrir retirada** lleva a preparar la salida o revisar la recepción y su asiento. Abrir esos paneles no crea ninguna orden ni solicitud de retirada. Cada paso conserva su propia revisión y autorización.

El atajo desaparece al contabilizar la retirada o si otro ingreso cierra el tramo. El informe conserva sus movimientos para consulta.

## Persistencia y límites

SQLCipher v26 añadió `real_wallet_funding_intents`, separado de órdenes Spot,
fills y movimientos patrimoniales. Guarda términos, HMAC de wallet/destino,
contrato público, importe exacto, mínimo indicado, nonce, gas y un historial
de estados con huellas. El mínimo queda ligado al plan. Los planes antiguos
sin permiso necesitan otra preparación y las solicitudes iniciadas se pueden
recuperar. El txId se transforma en HMAC con sal secreta de la bóveda. No se
guardan direcciones completas de wallet/destino ni hashes públicos de tx.
La respuesta temporal sí los puede mostrar; la extensión/RPC y MEXC conocen
sus respectivas direcciones. No se usan `localStorage` ni `sessionStorage`.

La migración trabaja sobre copia cifrada y conserva una preimagen. Backups
verifican también términos, estados, huellas y cronología de estos registros.
Ningún registro nuevo rellena huecos ni modifica operaciones existentes.

La v27 añade `real_wallet_funding_credits` y `real_wallet_funding_bookings`.
La evidencia acreditada es inmutable y separada del estado de solicitud;
conserva bloques públicos, gas observado, huella HMAC del paquete RPC y
referencia/huella del depósito View. No conserva las direcciones privadas ni
el hash público de la transacción. La contabilidad usa `BEGIN IMMEDIATE` y
restricciones únicas por depósito/asiento; asiento y vínculo se confirman en
la misma sesión y transacción. Repetir tras perder una respuesta o desde otro
proceso devuelve el mismo asiento. Se comprueban también enlaces manuales de
traslados para impedir asociar el mismo depósito a dos asientos diferentes.
Las copias cifradas reabren evidencia, identidad View y relación contable.

El regreso desde MEXC tiene su propio permiso separado, recepción BSC observada
y contabilidad descritos a continuación; no reutiliza el cruce de recibos Ethereum.
Sigue pendiente la prueba humana mainnet con un importe mínimo y claves
introducidas localmente.

## Regreso: retirada USDT/BSC

La [retirada USDT/BSC](MEXC_WITHDRAWAL_BNB.md) tiene plan, autorización puntual,
transporte de un solo intento, recuperación View, comprobación del recibo BSC
y asiento de neto/comisión, verificados con datos ficticios. Una respuesta
perdida no se reenvía. Sólo una resolución contable o un rechazo sin cargo
verificado permite revisar la apertura; STOP nunca se quita automáticamente.

La API oficial de retirada exige `SPOT_WITHDRAW_WRITE`, separado del permiso
Trade del bot. El transporte exige aprobación propia, destino wallet
comprobado, presupuesto/STOP y un único intento durable. La presencia
del campo opcional `withdrawOrderId` no se toma como garantía documentada de
idempotencia para repetir un POST tras perder su respuesta. Primero se consulta
el historial y se conserva el resultado incierto, sin reenviar.

El historial puede omitir `network`; no se debe deducir BSC sólo por USDT o una
dirección EVM. El estado MEXC tampoco sustituye la recepción observada en la
wallet. El plan propio, el hash exacto y el recibo BSC observado deben concordar;
no constituyen una prueba independiente de consenso. Las reglas de importe,
comisión y múltiplos del catálogo tienen pruebas controladas, pendientes de
contraste humano mainnet. Ninguna de estas consultas de documentación
configura claves, permisos ni una retirada en la cuenta.

## Fuentes revisadas el 30-10-2026

- [MEXC: catálogo de redes/contratos y depósito habilitado](https://www.mexc.com/api-docs/spot-v3/wallet-endpoints/query-the-currency-information).
- [MEXC: destino de depósito por red, sólo lectura](https://www.mexc.com/api-docs/spot-v3/wallet-endpoints/deposit-address-supporting-network).
- [MEXC: historial de depósitos, sólo lectura](https://www.mexc.com/api-docs/spot-v3/wallet-endpoints/deposit-historysupporting-network).
- [MEXC: nueva retirada y permiso separado](https://www.mexc.com/api-docs/spot-v3/wallet-endpoints/withdrawnew).
- [MEXC: historial de retiradas y red opcional en respuestas](https://www.mexc.com/api-docs/spot-v3/wallet-endpoints/withdraw-history-supporting-network).
- [EIP-1193: proveedor, cambios de cuenta/red y error de rechazo](https://eips.ethereum.org/EIPS/eip-1193).
- [ERC-20: precisión, `transfer` y comprobación de retorno booleano](https://eips.ethereum.org/EIPS/eip-20).
- [MetaMask: solicitud `eth_sendTransaction`](https://docs.metamask.io/metamask-connect/evm/guides/send-transactions/).
- [BNB Chain: consulta de bloques finalizados](https://docs.bnbchain.org/bnb-smart-chain/developers/json_rpc/bsc-api-list/).
