# Resolver un despliegue cancelado o reemplazado

Implementación v79 verificada y cargada localmente. La resolución
observa una transacción ya minada. No firma cancelaciones, no despliega contratos
por sí misma ni activa una estrategia.

## Resultados admitidos

- **Cancelado**: llamada propia de valor cero, sin datos, al mismo emisor EOA,
  con el nonce de la solicitud y recibo exitoso de 21.000 unidades de gas.
- **Ejecutor recuperado**: CREATE firmado con el mismo propietario y nonce,
  constructor exacto, propietario/pool/fee y runtime esperados.
- **Despliegue revertido**: CREATE exacto con recibo revertido y sin código.

Sólo se aceptan firmas legacy protegidas por chain56 o EIP-1559, verificadas
reconstruyendo los bytes firmados y su hash. La recuperación de pagos nativos
positivos y los firmadores de swaps conservan sus restricciones. Una llamada
arbitraria a otro contrato no se clasifica como cancelación propia; ese caso
requiere una conciliación adicional y sigue pendiente.

## Revisión

El usuario indica el hash del reemplazo en las operaciones del ejecutor y pulsa
**Revisar reemplazo**. El backend usa el RPC configurado, sin URLs ni pruebas RPC
aportadas por el cliente. Comprueba transacción, emisor, nonce, destino, valor,
datos, cadena, recibo, bloque, índice, estado finalizado y código en el bloque.
La lectura de red no mantiene cerrojos REAL ni una sesión de base de datos.
Tras ella se vuelven a comprobar token, control de wallet, generación y estado.

Se muestran resultado, gas real y máximo anterior. Si el precio firmado o su
coste máximo superan la revisión original se exige aceptarlo expresamente.
Esto concilia un gasto ya ocurrido; no concede permiso para otro envío.
**Confirmar resolución** vuelve a verificar el recibo y la revisión. La revisión
fija datos inmutables comprobados; el avance de la punta finalizada o campos
adicionales del proveedor no la invalidan cuando la transacción sigue igual.
Un cambio real de bloque, importe, recibo o solicitud sí obliga a revisarla.

## Persistencia y compatibilidad

El esquema 67 añade `real_dex_deployment_resolutions`, un vínculo único con la
solicitud original. El formato `orbis.dex.deployment.resolution.v1` guarda prueba
sanitizada, definición original, estado anterior y posterior, aceptación del
gas y un sello HMAC. No guarda direcciones, firmas, runtime ni hashes completos
de transacción. Lecturas, migración cifrada y copias comprueban la semántica y
el sello, además de la unicidad de nonce y transacción.

Una cancelación o reversión conserva el diario original byte a byte. Un CREATE
válido puede añadir la transición histórica ya existente CODE_OBSERVED, con su
prueba del código. No sustituye el hash original ni afirma que aquel envío se
minó. Así se conserva la interpretación de los formatos v1 y el ejecutor
comprobado puede pasar al flujo normal de configuración por su propietario.
Los informes de wallet tardíos no pueden reescribir una solicitud resuelta.

La guarda libera la solicitud resuelta para nonces posteriores y sigue
rechazando el nonce consumido. No basta con observar un contador mayor. La
resolución no crea capital, posiciones, permisos de trading ni concesiones de
gas. Confirmaciones repetidas o concurrentes devuelven el mismo registro.

El gas se registra mediante la fuente DEPLOYMENT_RESOLUTION y el contrato
contable `DEX_RECEIPT_BOOKING_V2`. Las cinco fuentes v1 conservan su formato.
Hay un solo gasto del reemplazo, enlazado al recibo; no se imputa otro gasto al
CREATE original pendiente. La cola de contabilidad permite revisarlo como las
demás comisiones BNB, con sus controles de revisión y duplicados.

## Límites y comprobación

Los recibos y la finalidad son observaciones del RPC seleccionado, no una
prueba independiente del consenso. Un resultado incierto no se reenvía. Perder
la respuesta HTTP de confirmación retira el botón y permite recuperar el estado
consultando el historial. Bloquear durante una lectura impide confirmar tarde.

Se comprueban legacy/EIP-1559, hash perdido o antiguo, cancelación, CREATE
recuperado/revertido, nonce/emisor/red/recibo incorrectos, sobrecoste explícito,
avance de finalidad, concurrencia, bloqueo, manipulación rehasheada,
contabilidad única y migración 66→67. El navegador usa una EVM propia para
cancelar, resolver, perder una respuesta, reabrir y crear con el siguiente
nonce. Las transferencias nativas inciertas de la operadora requieren otro
recorrido específico; no quedan resueltas por este módulo.

Verificación final: 3.795 casos en 332 archivos y 24 recorridos de navegador
aprobados sobre 980 fuentes sin cambios durante la ejecución. Servidor local
cargado, UI 200, bóveda cerrada y 19 rutas privadas rechazadas sin sesión.
Evidencia: `/home/rich/.local/state/orbis/verification/v79-resolution-integrated-r1/verification.json`.
Los formatos de resolución v1 y contabilidad v2 descritos aquí quedan publicados;
cualquier cambio de significado necesita su propia versión y migración.

Referencias primarias: [EIP-155](https://eips.ethereum.org/EIPS/eip-155),
[EIP-1559](https://eips.ethereum.org/EIPS/eip-1559) y
[JSON-RPC](https://ethereum.org/developers/docs/apis/json-rpc/).
