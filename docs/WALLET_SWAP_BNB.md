# Intercambio manual Uniswap v3 — BNB Chain

Estado: compra y venta USDT Binance-Peg ↔ WBNB implementadas como acciones
manuales, con permisos exactos, diario cifrado y cotejo de recibos. Verificadas
con una extensión sintética, un RPC controlado, una cadena de ensayo y
cotización pública real; **sin ensayo
humano de envío mainnet ni autorización delegada para bots**. Ethereum conserva
su cotizador anterior, sin ejecución. Esto no convierte un bot PAPER en bot DEX.
El registro explícito de operaciones y gas está implementado en SQLCipher v33;
observar un recibo o conectar la wallet nunca contabiliza por sí solo.

## Alcance y recorrido

1. Desbloquear REAL, elegir una cuenta y conectar una extensión EVM en BNB
   Chain (`56` / `0x38`). Verificar control EOA en esta sesión. No requiere MEXC,
   claves de exchange, una dirección de depósito ni importar saldos al libro.
2. En «Intercambiar · BNB», elegir USDT→WBNB o WBNB→USDT, cantidad exacta,
   deslizamiento (0,01–5 %, inicial 0,5 %) y gasto máximo de gas en BNB.
   **WBNB no es BNB nativo**; el BNB nativo se reserva aquí para comisiones.
3. «Cotizar» usa el RPC de la wallet y el mismo bloque para código EOA/token,
   contratos, decimales, saldo, permiso y cuatro pools directos. Exige 18
   decimales en ambos tokens, bloque reciente y saldo suficiente. Elige la
   mayor salida bruta entre respuestas válidas; un pool fallido no cuenta
   como cero. No optimiza gas ni rutas múltiples ni promete mejor precio.
4. La cotización caduca a los 30 s, antes si el bloque deja de ser reciente.
   Con permiso cero, «Preparar permiso» compila `approve(router, amount)`;
   nunca `uint256.max`, Permit2 ni un spender arbitrario. Si existe un permiso
   positivo insuficiente, se revoca primero con `approve(router, 0)`.
   «Revocar» también permite retirar un permiso suficiente desde una cotización
   válida. Confirmar en ORBIS y después en la extensión; cuesta gas real.
5. «Comprobar recibo» consulta el RPC BNB de ORBIS con el hash registrado.
   Verifica firma, posición en el bloque por número, `finalized`, llamada, gas
   y efectos. En un permiso coteja el evento `Approval` y la lectura en el mismo
   bloque. Si el gas o la llamada difieren, abre «Revisar resultado». Después
   hay que cotizar de nuevo.
6. Con permiso suficiente, «Preparar swap» fija importe de entrada, receptor
   igual a la wallet original, fee de pool y mínimo de salida. Usa únicamente
   `SwapRouter02.multicall(deadline, [exactInputSingle(...)])`: el deadline
   de 120 s se comprueba en el contrato, no sólo en un temporizador de UI.
   No permite calldata, destinatario, red, token ni router arbitrarios.
7. Se estima gas con margen del 20 %, máximo dos millones de unidades y sin
   superar el presupuesto BNB. Antes del permiso durable se repiten saldo,
   allowance, nonce, cuenta/red, vigencia y simulación. Tras concederlo se
   comprueban otra vez sesión, cuenta/red, nonce y vigencia antes de pedir
   `eth_sendTransaction`. La extensión firma; ORBIS no posee claves.
8. El hash no significa ejecución. El cotejo exige envío legacy exacto
   (incluidos nonce, gas y precio), misma cadena y bloques, finalización y
   transferencias de entrada/salida entre la misma wallet y el pool publicado
   por la factory en ese bloque. La entrada debe coincidir exactamente y la
   salida alcanzar el mínimo. Logs duplicados, eliminados o contradictorios
   no finalizan el registro. Una operación revertida conserva el gas sin
   inventar intercambio; puede haber revertido por caducidad del contrato.
9. Para vender y volver a USDT se repite el recorrido en sentido WBNB→USDT.
   Los tokens permanecen en la misma wallet, no en un saldo ficticio de ORBIS.
10. En el historial, «Revisar registro» muestra la operación y el gas en la
    cuenta REAL vinculada a la EOA original, aunque la operación se hubiese
    abierto desde otra cuenta. La cuenta de wallet no puede estar vinculada
    como cuenta MEXC. Registrar requiere control EOA vigente y confirmación
    aparte; nunca vuelve a solicitar envío ni firma a la extensión.

Los recibos nuevos muestran **«Confirmada»** tras cotejarlos con el RPC BNB
configurado en ORBIS, que puede conocer el hash y las direcciones consultadas.
La comprobación depende del nodo elegido. **«Finalizado según RPC»** identifica
los recibos históricos comprobados con el proveedor de la wallet. No se modifica automáticamente el libro REAL, su
coste de adquisición, su P/L ni la contabilidad de gas. El usuario puede
registrarlos según el contrato siguiente. No se atribuye capital a un bot ni
se amplían sus límites por contabilizar una operación.

### Usar el saldo máximo

Elige USDT o WBNB y pulsa **Máximo**. Se carga la cantidad exacta observada y se muestra aparte el BNB nativo para gas. Luego pulsa **Cotizar** para calcular el mínimo de salida. El saldo y la cotización consultan el RPC de la wallet por número de bloque y comprueban que su cabecera no haya cambiado. La lectura de saldo caduca en un máximo de 30 segundos. Antes de pedir la firma se vuelven a comprobar los fondos disponibles, el permiso y el nonce. [JSON-RPC de BNB Chain](https://docs.bnbchain.org/bnb-smart-chain/developers/json_rpc/bsc-api-list/).

### Revisar pool y recibo

**Cotizar** consulta los pools directos en la fábrica de Uniswap v3 y comprueba que tienen código en el bloque usado para cotizar. La revisión muestra cuántos devolvieron una cotización válida. Al situar el puntero sobre la línea del bloque se puede consultar la dirección pública del pool elegido. Al preparar el swap se muestra la salida de la simulación junto al mínimo del plan y el gas máximo. ORBIS vuelve a comprobar el pool y la simulación antes de solicitar la firma. [Despliegue de Uniswap v3 en BNB Chain](https://developers.uniswap.org/docs/protocols/v3/deployments/v3-bnb-deployments).

Cuando se comprueba el recibo, **Historial DEX** muestra las unidades recibidas y el mínimo de salida de ese swap. Se pueden volver a consultar desde la bóveda desbloqueada con la wallet desconectada. El control de privacidad oculta ambos importes. El asiento sigue disponible desde **Revisar registro**.

## Registro contable, costes y duplicados

`WALLET_SWAP_BOOKING_V1` enlaza una acción finalizada/revertida con sus asientos:

- Swap exitoso: una compra o venta WBNB/USDT con unidades y contravalor del
  recibo, no de la cotización previa. El precio unitario informativo se
  calcula con 40 cifras significativas; `gross_override` conserva el
  contravalor exacto cuando el cociente no es representable sin redondear.
- Permiso, revocación y swap revertido: sólo gasto de red. Un error no crea
  unidades, compra, venta, retirada de capital ni una aportación.
- Gas en BNB: primero caja BNB y después posiciones BNB, priorizando la
  divisa base de la cuenta y ordenando las restantes. La revisión indica
  las fuentes y unidades. Si falta saldo registrado a la fecha del recibo,
  se exige revisar el libro; no se importa ni inventa financiación.
- `FEE` en caja resta unidades y acumula gasto en esa misma divisa. `FEE`
  desde inventario consume unidades y su coste medio asignado; reduce el
  coste vivo y el resultado realizado por ese coste, sin inventar una venta
  ni convertir el gas a un precio de mercado o a otra moneda. Si se consume
  toda una posición, se asigna todo el coste, sin residuo decimal.
  Es el modelo de seguimiento patrimonial de ORBIS, **no un criterio fiscal**.

Se ofrecen asientos manuales compatibles de la misma cuenta y hasta dos
minutos alrededor del bloque. Operación, unidades, contravalor y tipo deben
coincidir; las comisiones existentes deben representar unidades BNB exactas.
Si aparecen candidatos, hay que revisarlos y elegir los ya anotados antes de
crear otro asiento. Una comisión puede cubrir parte del gas y el resto se
propone como gasto nuevo. No se reescriben fecha, notas ni precio informativo
de un asiento enlazado. La coincidencia es declarada por el usuario, no una
prueba causal independiente de su origen on-chain.

Una misma fila no se vincula a dos acciones DEX ni se toma un fill MEXC
ya contabilizado como otro swap. Dentro de `BEGIN IMMEDIATE` se vuelven a
verificar recibo, wallet, vínculo, revisión del libro y selección. Operación,
gas y referencias se confirman juntos o se revierten juntos. Reintentar la
misma confirmación devuelve el registro existente, también después de una
respuesta perdida, reapertura o desvinculación posterior de la wallet. Cambiar
el libro durante la revisión obliga a abrir otra; no se reutiliza una aprobación
contable antigua sobre otro saldo.

Lecturas y copias cifradas validan las referencias, importes, gas total y
digest del recibo. Exportar conserva los eventos `FEE` en CSV REAL v3; v1/v2
continúan siendo importables para eventos anteriores. El CSV no sustituye la
copia cifrada para restaurar vínculos con evidencias DEX. Una corrección
retroactiva legítima sigue recalculando el libro según sus hechos registrados.

## Incertidumbre y recuperación

```text
PREPARED → WALLET_REQUESTED → HASH_REPORTED → FINALIZED | REVERTED
                       └──→ WALLET_REJECTED (informe 4001)
```

El permiso se guarda con `BEGIN IMMEDIATE` **antes** de contactar con la
extensión. Cada UUID sólo lo concede una vez. Una acción sin respuesta o
con recibo pendiente bloquea otro envío DEX de esa wallet, incluso con otro
plan o tras reiniciar. La llamada de gasto no tiene timeout/reintento; las
lecturas sí. Un error distinto de 4001 no demuestra rechazo ni ausencia de
emisión. Si se perdió el hash, hay que copiar el original desde la actividad
de la wallet y comprobarlo en el historial. No se sustituye otro hash ni se
reutiliza el de otro plan. No hay botón para borrar incertidumbre por tiempo.

La guarda de nonce es compartida con el ingreso wallet→MEXC: una solicitud
sin respuesta bloquea ambos módulos para esa wallet, y un nonce ya asociado
a un hash no se puede reclamar en el otro flujo. Se comprueba bajo la misma
transacción de escritura, sin abrir sesiones SQLite anidadas. No pretende
controlar operaciones iniciadas directamente desde otras aplicaciones.

Cambiar cuenta/red, desconectar, bloquear REAL o salir de la vista invalida
el plan y vacía los datos privados en memoria. **No cancela** un diálogo ya
entregado a la wallet ni una transacción emitida, y **no revoca** allowances
en cadena. Revisar/rechazar allí cualquier diálogo pendiente. Un permiso
persistente se puede retirar con otra transacción explícita desde ORBIS
cuando haya una cotización válida, o desde las herramientas de la wallet.

## Registro local y componentes

- `wallet-swap.js`: interacción EIP-1193, BigInt, cotización, simulación,
  compilación/cotejo local, confirmaciones y recuperación; sin claves.
- `wallet_swap.py`: compilador ABI canónico, invariantes financieras y diario
  idempotente. `wallet_swap_receipt.py`: verificación conservadora del RPC.
- `wallet_swap_api.py`: API local protegida por sesión REAL, origen permitido
  y prueba EOA del mismo usuario/cuenta/red para preparar y reclamar.
- SQLCipher **v32**, tabla `real_wallet_swap_intents`, separada de MEXC,
  operaciones PAPER y eventos patrimoniales. Migración en copia cifrada con
  preimagen recuperable, conservación de cuentas/historial y verificación.
- SQLCipher **v33** añade `real_wallet_swap_bookings` y sus referencias únicas
  en `real_wallet_swap_booking_events`, sin modificar recibos anteriores.
  `wallet_swap_booking.py` gestiona revisión, enlace, escritura atómica y
  verificación. Los nuevos gastos `FEE` usan la misma persistencia decimal
  del libro; no guardan direcciones, claves ni txIds en notas.
- Guarda importes enteros, términos versionados por contrato, estados,
  tiempos y evidencia mínima; wallet y txId se guardan sólo como HMAC con sal
  privada de la bóveda. Direcciones/hashes completos y calldata con receptor
  viajan temporalmente al backend local; no quedan en SQLite ni localStorage.
  Contratos públicos de protocolo/token no son direcciones de usuario.
- Ningún importe usa coma flotante. Mínimo = `floor(quote * (10000-bps)/10000)`;
  gasto máximo = `gas * gasPrice`, todo en unidades enteras. El registro de
  gas confirmado conserva `gasUsed * effectiveGasPrice` en wei.

## Registro cerrado y fuentes

| Componente | Contrato BNB Chain |
| --- | --- |
| USDT Binance-Peg | `0x55d398326f99059ff775485246999027b3197955` |
| WBNB | `0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c` |
| QuoterV2 | `0x78d78e420da98ad378d7799be8f4af69033eb077` |
| SwapRouter02 | `0xb971ef87ede563556b2ed4b1c0b0019111dd85d2` |
| Factory v3 | `0xdb1d10011ad0ff90774d0c6bb92e5c5c8b4461f7` |

Direcciones contrastadas con los [despliegues oficiales Uniswap BNB](https://developers.uniswap.org/docs/protocols/v3/deployments/v3-bnb-deployments),
su [lista de tokens](https://github.com/Uniswap/default-token-list/blob/main/src/tokens/bnb.json)
y el [catálogo BSC de PancakeSwap](https://tokens.pancakeswap.finance/pancakeswap-extended.json).
ABI [exactInputSingle de SwapRouter02](https://github.com/Uniswap/swap-router-contracts/blob/main/contracts/interfaces/IV3SwapRouter.sol)
y [multicall con deadline](https://github.com/Uniswap/swap-router-contracts/blob/main/contracts/base/MulticallExtended.sol).
SwapRouter02 es el adaptador v3 delimitado de este corte; no se confunde con
Universal Router (entrada preferida actualmente por Uniswap), v4 ni Permit2.
Su repositorio está archivado; el bytecode desplegado es independiente de
ese estado. No se declara auditado por ORBIS ni se asumen contratos iguales
entre redes. No se añaden proveedores o tokens por símbolo.

Pruebas de la comprobación actual en `test_dex_call_resolution_api.py` y
`browser_wallet_swap_flow.py`. Los recorridos controlados usan API y SQLCipher
reales, incluyendo reapertura y registro explícito. `live_uniswap_bnb_quote_smoke.py` es una consulta pública
optativa, sin wallet ni emisión, no una prueba de ejecución real. Quedan
pendientes piloto humano, móviles,
delegación compatible con límites/parada y bots DEX autónomos. Nunca se
presenta una confirmación manual como automatización de la wallet.
