# Recuperación de nonces DEX

Seguimiento posterior a la contabilidad v78. El primer tramo, resolución de
CREATE cancelado, reemplazado o revertido, está verificado y cargado en v79.
Véase [DEX_DEPLOYMENT_RESOLUTION.md](DEX_DEPLOYMENT_RESOLUTION.md).
La resolución de financiación/devolución nativas con reemplazo finalizado está
verificada y cargada en v80: [DEX_NATIVE_RESOLUTION.md](DEX_NATIVE_RESOLUTION.md).
La emisión de cancelaciones de devoluciones BNB está cargada en v81:
[DEX_NATIVE_CANCELLATION_PLAN.md](DEX_NATIVE_CANCELLATION_PLAN.md).
La financiación desde wallet se puede cancelar en v82:
[DEX_WALLET_CANCELLATION_PLAN.md](DEX_WALLET_CANCELLATION_PLAN.md).

## Comportamiento comprobado en el código

- `dex_deployments.recover_deployment` conserva su recuperación de código en la
  dirección CREATE prevista. La nueva resolución independiente comprueba un
  reemplazo finalizado y libera una solicitud resuelta para nonces posteriores,
  conservando el bloqueo del nonce consumido y el historial original.
- `wallet_nonce_guard.assert_wallet_nonce_available` separa desconocido, hash
  observado y recibo. No convierte un nonce consumido en una operación exitosa.
- `dex_operator_journal.recover_intent` conserva NONCE_OBSERVED y permite buscar
  el recibo original. No acredita salidas ni compras por esa observación.
- `dex_gas_return` tiene un contrato v1 publicado y un envío de un solo POST.
  Si el envío no llegó al nodo y no existe recibo, permanece incierto. Sus
  validadores y guardas conservan esa incertidumbre.
- `dex_native_resolution` permite conciliar un reemplazo nativo ya finalizado.
  Conserva ambos originales, distingue propietario y firmante, bloquea el nonce
  consumido y libera posteriores sólo con su prueba y aceptación necesarias.
  Su fuente contable v3 registra un gasto sin duplicar la solicitud original.

## Prueba necesaria para una cancelación

Comprobar transacción firmada, emisor original, chain56, nonce exacto, recibo
finalizado, bloque y hashes coherentes. Una transferencia de valor cero, datos
vacíos y destino propio EOA puede demostrar un reemplazo sin el efecto original.
No aceptar una captura de saldo, un timeout, un nonce mayor ni un hash cualquiera.
Una aceleración que sí ejecutó CREATE o la transferencia debe tratarse como
reemplazo ejecutado, con su efecto y gasto reales, no como cancelación.

Mantener las definiciones y estados históricos v1. Una resolución adicional
necesita un contrato versionado propio, vinculado a la definición y al recibo
exactos del origen. Las guardas y copias deben verificar esa resolución antes
de liberar el pendiente, sin reescribir el significado de los registros viejos.
Su comisión deberá poder pasar por la contabilidad de recibos como un gasto
nuevo verificado, sin volver a cobrar la comisión del origen.

Una eventual emisión de cancelación desde la operadora exige un plan y firmador
específicos: mismo nonce, valor cero, destino propio fijado, calldata vacía,
gas limitado, revisión, confirmación, revocación y concesión de un único POST.
No ampliar el firmador de swaps ni repetir automáticamente el envío incierto.
La política de sustitución del proveedor y la carrera entre el original y su
reemplazo deben verificarse antes de implementar la emisión.

## Orden entre ejecuciones

La contabilidad v78 conoce bloques y nonce del emisor. Los recibos históricos
no guardan transactionIndex, por lo que bloquea swaps de distintas operadoras
en el mismo bloque cuando no puede justificar su orden. La ampliación requiere
una prueba adicional del índice global, ligada al bloque y al hash original
mediante su HMAC. No inferir orden a partir de UUIDs, llegada de HTTP ni momento
de contabilización. Cualquier cambio en el criterio de orden del libro y en
sus registros existentes necesita migración y pruebas específicas.

## Casos de aceptación

v79 cubre CREATE perdido o con hash original, cancelación finalizada, CREATE
reemplazado o revertido, nonce/emisor/red/recibo incorrectos, sobrecoste explícito,
avance de finalidad, concurrencia, bloqueo durante lectura, respuesta perdida,
reapertura, gas único, manipulación y migración cifrada. La regresión general
(3.795 casos) y los 24 recorridos de navegador aprobaron antes de cargarla.

v80 amplía los casos a financiación/devolución nativas, importes externos
distintos, gas único, reapertura y devolución posterior con nonce nuevo. También
corrige el rearme DCA con recibos dentro de la tolerancia de reloj ya verificada.
Suite completa de v80: 3.808 casos y 25 recorridos aprobados.

v81 añade plan, firma, diario y transporte propios para cancelar devoluciones
pendientes: mismo nonce, cero al propio operador y gas revisado. Archivo y
recibo ganador se conservan juntos, sin reenvíos al reabrir. Suite completa de
3.840 casos y 26 navegadores; tras ajustar el formulario se repitieron tres
navegadores afectados con backend/pruebas/contratos idénticos a la suite completa.

v82 añade concesión de una sola solicitud a la wallet, recuperación sin otro
diálogo, bloqueo de promesa pendiente incluso al cambiar de pantalla y
conciliación del ganador sin gas duplicado. Suite completa: 3.867 casos y
27 recorridos de navegador nuevos sobre las mismas fuentes, aprobados.

v115 incorpora observación y revisión financiera de reemplazos externos para WALLET_SWAP y OPERATOR. v116 comprueba también la siguiente orden manual por su punto de entrada habitual después de la revisión aceptada. [Continuidad y evidencia](ECONOMIC_READ_SNAPSHOT.md).

v123 conecta la búsqueda finalizada por nonce a la financiación y devolución BNB.
Permite revisar un recibo cuando ya no está disponible su hash y conserva la
confirmación separada. [Recorrido y límites](DEX_NATIVE_RESOLUTION.md#búsqueda-por-nonce).

v124 extiende la búsqueda a despliegues del ejecutor, operaciones de
wallet y llamadas de operadoras detenidas. Para originales sin un ancla,
parte del bloque finalizado y conserva la revisión manual por hash.
[Llamadas y recibos externos](DEX_CALL_RESOLUTION_PLAN.md#búsqueda-por-nonce).

Quedan por extender los adaptadores de observación y revisión externa a las otras familias de nonce. Las emisiones sin un recibo ganador mantienen su estado incierto. Las pruebas utilizan EVM desechable y bóvedas propias.
