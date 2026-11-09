# Orden de ejecución de los recibos DEX

Estado: desarrollo. La versión cargada del propietario se mantiene en v111
hasta verificar y publicar el conjunto contable completo.

## Uso

**Comprobar bloque** aparece junto a las compras y ventas finalizadas del bot
en **Contabilidad DEX**, y junto a los swaps manuales confirmados en
**Intercambiar · BNB → Historial DEX**. También admite operaciones cuyo resultado
se haya recuperado mediante una resolución de llamada. Las cancelaciones y
los permisos no presentan este control financiero.

La wallet debe haber acreditado su control en la sesión y en el contexto del
recibo. El botón consulta el bloque original, localiza la transacción por su
referencia HMAC y verifica firma, recibo, posición, bloque finalizado y una
segunda lectura del bloque. No pide copiar un hash ni firmar otra transacción.
**Bloque ✓** indica que se ha conservado esa prueba. El título muestra el
número de bloque y la posición (desde 1); ocultar importes oculta ese detalle.

Una respuesta perdida deja **Bloque sin confirmar · Reintenta**. Reintentar
consulta o recupera la misma prueba de forma idempotente. No hay repetición
automática, asientos nuevos, aportaciones ni envíos de fondos. Cerrar la vista,
cambiar wallet/cuenta/red/generación o perder el control verificado descarta
una respuesta tardía en la interfaz.

## Contrato técnico

- `GET /real/vault/status` publica
  `capabilities.dex_receipt_order_proof: true`. El cliente usa una consulta
  pasiva. Un backend anterior o inaccesible conserva el flujo anterior y no
  presenta el nuevo control.
- `POST /real/accounts/{context}/dex-receipts/{kind}/{id}/order-proof` recibe
  únicamente `{ "address": "…" }`, con token local. `kind` sólo admite
  `WALLET_SWAP`, `OPERATOR` y `CALL_RESOLUTION`. Rechaza coordenadas o pruebas
  aportadas por el cliente.
- `GET` en la misma ruta es pasivo; la wallet transitoria va en
  `X-Orbis-Wallet`. Devuelve si ya existe una prueba válida, sin consultar RPC.
- Las respuestas son `no-store`. Direcciones y hashes completos no aparecen
  en las URLs ni se añaden al diario. Los errores de transporte se redactan.

La captura y las llamadas RPC no mantienen cerrojos de acceso ni transacciones
SQL. Antes de escribir se vuelven a comprobar token, generación, wallet,
contexto y recibo original. La prueba se guarda una vez en el diario cifrado,
con autenticación y restricciones de unicidad para firma/nonce y bloque/índice.
La firma manual y la de la operadora comparten referencias verificadas para
poder ordenar ambas dentro del mismo bloque.

`dex-receipt-order.js` comparte disponibilidad, validación de respuesta,
redacción y timeout entre ambos paneles. Cada panel conserva su propia
protección contra respuestas tardías y su identidad de contexto.

## Límite contable

Guardar la prueba no acepta un criterio contable ni altera los asientos
originales. Es un requisito del replay económico: permite distinguir el orden
de ejecución del orden de llegada de los registros. La aceptación completa de
una cuenta y el registro conjunto de recibos anteriores o de nuevas wallets
siguen requiriendo su revisión y coordinación específicas antes de exponer esa
función como operativa. Una prueba nunca concede permiso para operar.

El historial de cartera presenta la fecha, orden y coste efectivos con la
etiqueta **Criterio aceptado**. La ayuda conserva la fecha original/de registro
y el coste anterior, ocultando importes cuando se solicite. El CSV conserva los
inputs originales: **CSV · Original**, su nombre y su confirmación identifican
que no incluye el criterio. La copia cifrada conserva ambos para recuperarlos.

## Verificación

La API se prueba sobre SQLCipher y transacciones firmadas en Anvil propio:
fuentes manuales, operadora y resoluciones; legacy/EIP-1559; contexto/propietario
incorrecto; bloqueo durante ambas fases RPC; originales alterados; bloque
duplicado/incorrecto; errores redactados; confirmaciones simultáneas y reapertura.

Las pruebas de navegador del widget cubren compatibilidad con un backend sin
capacidad, respuesta perdida, datos incoherentes, importes ocultos y cambios de
contexto durante la consulta. Los recorridos integrados usan la API real y una
EVM desechable para verificar comprobación, registro contable, respuesta perdida,
reapertura y escritorio/móvil. No utilizan fondos ni claves del propietario.
