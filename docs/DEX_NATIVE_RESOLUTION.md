# Resolver financiación y devolución BNB reemplazadas

Implementación v80 verificada y cargada localmente. Complementa la recuperación de CREATE de
v79. No firma cancelaciones ni vuelve a emitir transferencias inciertas.

## Revisión del efecto real

Dos fuentes cerradas: GAS_FUNDING (wallet → operadora) y GAS_RETURN (operadora →
wallet). Una resolución exige el emisor y nonce originales, cadena 56, firma
legacy EIP-155 o EIP-1559 reconstruida, hash, índice y recibo finalizado ligados
al mismo bloque. Comprueba código vacío de ambos extremos en ese bloque, datos
vacíos, 21.000 unidades de gas usadas y precio efectivo exacto. Relee el bloque
después de consultar el código y el nonce del emisor.

- Cancelación: valor cero, destino propio y recibo exitoso. No acredita BNB
  transferido al destinatario del plan.
- Transferencia: valor positivo al destinatario original, verificado por su
  identidad HMAC. Conserva el importe efectivo del recibo y su gas.
- Reversión: no acredita principal; conserva sólo su gasto comprobado.

El importe externo puede diferir del plan. El panel muestra importe original,
transferido, gas real y límites originales. Si cambia el importe firmado o se
supera el precio/máximo de gas revisado, exige **Aceptar cambios de importe y
gas** y una nueva revisión fijada. Se concilia una transacción ya ocurrida;
esto no amplía presupuestos, cuotas, exposición ni permisos del bot.

Una llamada con datos o a otro destinatario requiere conciliación adicional.
No se clasifica como cancelación propia ni se libera el pendiente por el mero
hecho de observar un nonce mayor. La evidencia del RPC es una observación de
ese proveedor, no una prueba independiente del consenso.

## Persistencia

Esquema 68: `real_dex_native_resolutions`. Vínculo único mediante FK al diario de
financiación o devolución, ID derivado de fuente e ID original, emisor/nonce
únicos y propietario separado del firmante. El formato
`orbis.dex.native.resolution.v1` conserva definición y estado originales fijados,
prueba sanitizada, aceptación de cambios, fecha y sello HMAC. El original queda
intacto byte a byte; no se sustituye su hash ni se afirma que se minó aquél.

Sin direcciones, claves, firmas o hashes completos de transacción persistidos.
La migración se ejecuta sobre copia cifrada, con preimagen y recuentos. Lecturas,
copias y reapertura validan sello, semántica, referencias y duplicados, también
contra las resoluciones de CREATE. No se modifica el significado de formatos
publicados de financiación, devolución, resolución CREATE o contabilidad v1/v2.

La guarda permite nonces posteriores al resuelto; el consumido continúa
reservado. La financiación resuelta deja de bloquear la devolución y la
devolución resuelta deja de bloquear una nueva revisión o conexión. Una
notificación tardía del original no puede reescribir la resolución. Cada nuevo
envío sigue exigiendo preparación, condiciones actuales y confirmación propias.

## Contabilidad

Nueva fuente NATIVE_RESOLUTION y contrato `DEX_RECEIPT_BOOKING_V3`. La cartera
destino procede del vínculo de la wallet propietaria. El firmante puede ser la
operadora; no se atribuye su wallet como propietaria del patrimonio.

El principal entre wallet y su operadora es traslado interno: no crea capital,
beneficio ni nuevas unidades patrimoniales. Sólo el gas verificado genera gasto
BNB, con revisión y exclusión de asientos ya reclamados. Una confirmación
repetida o concurrente devuelve el mismo registro; la solicitud pendiente
original no puede proporcionar un segundo recibo para duplicar ese gasto.

## API e interfaz

POST privados:
`/real/accounts/{account}/dex-native-resolutions/{kind}/{source_id}/resolution-preview`
y `/resolve`, con kind GAS_FUNDING o GAS_RETURN. JSON estricto: address, tx_hash,
accept_changes; confirmación añade preview_digest, source_state_digest y
`RESOLVER_REEMPLAZO_BNB`. No admite pruebas RPC, URL, clave, firma o transacción
del cliente. Usa el RPC configurado por ORBIS.

Exige sesión, origen permitido y control probado de la wallet propietaria.
Ningún cerrojo privado ni sesión SQL cruza la red; revalida generación, wallet y
estado después de leer. La resolución puede hacerse tras reabrir sin restaurar
la clave operadora, ya que no firma. Respuestas no almacenables en caché.

En los historiales **Envíos de gas** y **Gas · Recuperar BNB**, introducir el hash
y pulsar **Revisar reemplazo** → revisar importes/gas → **Confirmar resolución**.
Una respuesta HTTP perdida retira la revisión y se recupera con Actualizar;
nunca repite el POST automáticamente. Al recuperar la tarjeta resuelta se
limpia el aviso anterior de respuesta incierta. El componente es compartido con
CREATE y mantiene instancias y ámbito de cuenta/wallet/sesión independientes.

### Búsqueda por nonce

Desde v123, con el campo de hash vacío, **Revisar reemplazo** solicita
`POST /real/accounts/{account}/dex-native-resolutions/{kind}/{source_id}/discover`.
El servidor busca el nonce del emisor dentro de los primeros 262.144 bloques
posteriores al ancla del plan, con el tope del bloque finalizado. Localiza el
salto del contador con búsqueda binaria y coteja el hash, el bloque y el índice
con el recibo. La firma y el efecto pasan por la revisión nativa habitual.

Para una devolución, recupera la dirección pública de la operadora en el
bloque de su concesión original y verifica su referencia local. Funciona tras
reabrir la bóveda sin restaurar la clave de firma. El hash encontrado vive en
la revisión de navegador y el diario cifrado conserva su referencia HMAC.
**Confirmar resolución** sigue siendo un paso separado.

La búsqueda requiere un RPC que pueda consultar el estado de esos bloques.
Si el RPC carece del estado histórico, el usuario puede introducir el hash
directamente. Un nonce sin recibo finalizado conserva su estado pendiente.
[API de finalidad de BNB Chain](https://docs.bnbchain.org/bnb-smart-chain/developers/json_rpc/bsc-api-list/)

## Verificación y alcance pendiente

Pruebas con EVM y bóvedas propias: cancelaciones de financiación y devolución,
legacy/EIP-1559, transferencias reemplazadas con distintos importes y comisiones,
concurrencia, bloqueo durante RPC, cambio de finalidad, reapertura, gas único,
migración 67→68 y manipulación rehasheada. El navegador recorre financiación
cancelada → respuesta perdida → financiación siguiente → devolución incierta →
cancelación externa propia → reapertura → devolución siguiente → cuatro gastos
contabilizados. Sin fondos ni claves del usuario.

La emisión de cancelaciones de devolución se añadió en un módulo separado v81,
con su propio plan, firmador y outbox:
[DEX_NATIVE_CANCELLATION_PLAN.md](DEX_NATIVE_CANCELLATION_PLAN.md).
La emisión desde wallet sigue pendiente. Tampoco se resuelven aquí swaps cancelados o llamadas
arbitrarias de reemplazo. Quedan el orden global entre operadoras del mismo
bloque, historiales grandes y piloto humano mainnet.

Verificación final: 3.808 pruebas en 334 archivos y 25 recorridos de navegador,
aprobados sobre 986 fuentes sin cambios durante ambas ejecuciones. Incluye la
regresión del desfase de cadena en rearme DCA, detectada y corregida antes de
repetir la suite completa. Servidor cargado, UI 200 y 23 rutas privadas anónimas
401, bóveda cerrada. Evidencia:
`/home/rich/.local/state/orbis/verification/v80-native-integrated-r2/verification.json`.
Resolución nativa v1 y contabilidad v3 quedan publicadas con estos significados;
las ampliaciones posteriores necesitarán formatos/migraciones propios.

Referencias primarias: [EIP-155](https://eips.ethereum.org/EIPS/eip-155),
[EIP-1559](https://eips.ethereum.org/EIPS/eip-1559) y
[JSON-RPC](https://ethereum.org/developers/docs/apis/json-rpc/).
