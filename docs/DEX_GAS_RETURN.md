# Devolución de BNB residual de la operadora

Estado: v76 verificada y cargada localmente. Lectura, plan, diario cifrado,
firma dedicada, envío único, API e interfaz integrados. Suite completa:
3.774 pruebas / 327 archivos aprobados, sin fallos, errores ni omitidas;
veinte recorridos de navegador actuales aprobados, sólo con fondos de prueba.

Ampliación v81 cargada: una devolución pendiente puede cancelarse mediante
un plan, diario y firmador de valor cero separados. Conserva este formato v1
y registra el recibo que realmente ganó. Uso y verificación actuales:
[DEX_NATIVE_CANCELLATION_PLAN.md](DEX_NATIVE_CANCELLATION_PLAN.md).

## Recorrido

Bot detenido y trabajador terminado → conciliar envíos originales → restaurar
la copia cifrada de la misma operadora → revisar BNB recuperable y coste →
confirmar devolución al propietario → reservar nonce → firmar llamada nativa
exacta → archivar hash e intento antes del único POST → verificar recibo.
Bloquear/reabrir nunca repite automáticamente una devolución.

Sólo admite el propietario ya probado y enlazado a esa operadora; no hay campo
de destino, calldata o transacción arbitrarios. Las órdenes DEX siguen usando
su firmador actual, que continúa rechazando transferencias nativas.

## Plan y lectura

`dex_gas_return.py` lee red, bloque finalizado, código vacío del propietario y
operadora, saldo y nonce en ese mismo bloque. Contrasta nonce pendiente y vuelve
a comprobar número, hash y timestamp del bloque. El balance y las cuentas son
transitorios; en el diario se conservarán identidades locales con HMAC.

La transferencia legacy de BNB sin datos entre EOA reserva 21.000 unidades de
gas al precio exacto revisado. Devuelve saldo menos ese coste, con enteros wei;
si el saldo no supera el coste no se prepara un envío. Precio y coste máximo
mantienen las cotas del kernel existente. La lectura exige observación de hasta
15 s y bloque de hasta 30 s, con tolerancia de cadena de hasta 15 s hacia delante.
Una cola de nonces o código no vacío bloquean el plan.

El comprobador de firma exige bytes RLP exactos, cadena 56 con EIP-155, nonce,
precio, gas, destino propietario, valor y datos vacíos. Comprueba firma low-S,
remitente y hash. No firma ni emite; tampoco demuestra por sí solo autoridad
para usar una clave del usuario.

Referencias de protocolo: [EIP-155](https://eips.ethereum.org/EIPS/eip-155) y
[JSON-RPC](https://ethereum.org/developers/docs/apis/json-rpc/). La comprobación
de cadena/recibo depende del RPC configurado, como el resto del kernel; no se
presenta como una prueba de consenso independiente.

## Diario, concurrencia y permisos

- Tabla `real_dex_gas_return_intents`, esquema de bóveda 65 y formato
  `orbis.dex.gas.return.v1`. Mantiene independientes financiación owner →
  operadora, swaps y devoluciones. Migración sobre copia cifrada, preimagen
  recuperable y comprobación de que no cambian los registros anteriores.
- La revisión conserva importe, gas, nonce, propietario y operadora. En la
  confirmación se relee y se exige el mismo saldo/nonce antes de firmar; un
  cambio obliga a revisar de nuevo. No ampliar importes con un saldo posterior.
- Sólo familias detenidas y sin intentos pendientes. Una reserva de devolución
  bloquea conexión, financiación, arranque y otros usos de ese nonce de operadora.
  La concesión y la guarda de nonce se confirman en una misma transacción SQL.
- Firma dedicada desde la preparación verificada, con generación y revisión
  vigentes. No usar la lease de swaps ni exponer claves, firma o bytes en HTTP.
- Continuación no copiable, un único POST, guardas de bloqueo/revocación antes
  de concederlo y red fuera de los cerrojos. Resultado incierto conserva su
  hash y reserva, sin inferir éxito por nonce o timeout.
- PREPARED cancelable; reservas/firmas nunca emitidas abandonables sólo por
  barrera acreditada. Una emisión ya concedida requiere el recibo original.
- Recibo final verifica firma de transferencia y efecto exactos. BNB devuelto
  es transferencia interna, gas BNB es coste; ninguno se convierte en beneficio
  USDT o en presupuesto nuevo del bot. La comisión de devolución corresponde
  a su confirmación manual propia; no renueva la autorización automática del bot.
- Copia, migración y reapertura validan el nuevo diario semánticamente. No
  migrar el REAL del usuario desde las herramientas de desarrollo.

La API `/dex-gas-returns` exige sesión local, prueba de wallet, preparación
cifrada verificada y confirmación `DEVOLVER_GAS_OPERADORA_DEX`. No admite destino,
importe arbitrario, clave o transacción del cliente. Un cambio de saldo, nonce
o gas obliga a revisar de nuevo. Reservas/firmas no emitidas se descartan por
barrera SQL; una emisión concedida requiere su recibo original.

La interfaz «Gas · Recuperar BNB» muestra recuperable y coste, conserva los
importes ocultables y permite cancelar revisiones o descartar reservas no
emitidas. Si se pierde la respuesta del envío, consulta el mismo registro;
no repite una firma. El hash original sólo vive en memoria de la petición de
servidor y de la sesión cliente; puede introducirse para conciliar tras reabrir.

Verificación de desarrollo: 98 casos del núcleo/migraciones aprobados antes de
integrar la API, cuatro del outbox y tres de API (grupos solapados con otras
regresiones; no sumar sus cifras sin comprobar identidades). El navegador
comprobó compra, venta, parada, devolución exacta del saldo menos gas, pérdida
de respuesta HTTP recuperada por GET, escritorio/móvil y reapertura sin autorun.
No se han utilizado fondos ni claves del usuario. La nueva función no se
presenta como una operación mainnet ejecutada ni como contabilidad general ya
conciliada; el asiento patrimonial DEX sigue en el objetivo posterior.

Evidencia final: `v76-gas-return-integrated-r2/verification.json` bajo
`/home/rich/.local/state/orbis/verification`. Bóveda REAL del usuario cerrada
durante la carga; las migraciones se verificaron únicamente sobre fixtures.
