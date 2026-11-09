# Cancelación de financiación desde wallet — v82

v82 verificada y cargada en el servidor local. Núcleo, diario, API e interfaz
wallet están implementados. La regresión completa y los 27 recorridos integrados
han aprobado sobre las mismas fuentes. No se han usado fondos ni credenciales
del usuario.

## Arquitectura y alcance

- Conservar los formatos publicados de financiación, resolución nativa,
  cancelación de devolución y contabilidad. La financiación original sólo
  cambiará si se verifica su propio recibo mediante el flujo v1 existente.
- Nuevo `dex_wallet_cancellation` para revisar una financiación concreta.
  La firma corresponde al proveedor de la wallet; no se usa la clave operadora
  ni un firmador del servidor. La solicitud exacta fija wallet → misma wallet,
  valor cero, calldata vacía, cadena 56, gas 21.000 y nonce del origen.
- `evm_replacement_state` comparte la lectura de EOAs, saldo y nonce
  finalizado/último/pendiente/actual. `CancellationState` de v81 conserva todos
  sus campos y significado mediante un adaptador. No hay cambio de formato.
- Si se conoce el hash original, reconstruir y comprobar la firma de la
  transferencia: emisor, destinatario, importe, nonce, cadena y tarifa. Releer
  antes de devolver la revisión. Una transacción minada, aparecida o cambiada
  durante la lectura invalida la revisión; se comprueba su recibo.
- La tarifa real firmada puede diferir de la pedida a la wallet. Para un
  original EIP-1559 se usa su máximo firmado, no el `gasPrice` efectivo mostrado
  por el RPC. Solicitar una sustitución legacy con precio mayor al máximo
  incrementado cubre también la propina conocida. La aceptación en un nodo no
  se garantiza: su política y las carreras de minado siguen siendo externas.
- Si no existe hash o el RPC no devuelve el original, no usar el precio del
  borrador como si fuese firmado. La revisión conserva tarifa desconocida y
  exige aceptación explícita. Un original presente con firma/alcance incorrectos
  se rechaza; no se convierte en evidencia desconocida para saltarse la revisión.
- El coste revisado describe la solicitud enviada al proveedor de wallet.
  La wallet puede modificarla antes de firmar. El recibo real y sus cambios
  se revisan después; no prometer un límite impuesto al software de la wallet.

## Diario y concurrencia implementados

Tabla independiente `real_dex_wallet_cancellation_intents`, esquema fuente
70 y formato publicado `orbis.dex.wallet.cancellation.v1`, de significado fijado.
FK al envío BNB,
cuenta, referencias HMAC de propietario/operadora, términos/estados sellados,
fechas y huella del prefijo WALLET_REQUESTED original. Sin direcciones completas,
hashes completos, firmas, claves ni transacciones crudas persistidas.

Estados: PREPARED → WALLET_REQUESTED → HASH_REPORTED o WALLET_REJECTED;
recibo ganador → SETTLED. PREPARED puede retirarse. Una solicitud incierta no se
descarta como si nunca se hubiera invocado la wallet ni se repite al reabrir.
La concesión durable precede al único `eth_sendTransaction` del cliente.
La respuesta perdida de la concesión no abre otro diálogo. Una promesa wallet
sin resolver bloquea otra solicitud local aunque expire la espera de interfaz
o se desmonte y vuelva a montar el panel. Un resultado tardío no se publica en
otra cuenta, wallet o sesión. Esta guarda en memoria termina al resolverse la
promesa; tras reiniciar, el diario incierto exige una nueva revisión explícita.

Un nuevo intento exige otro ID y otra revisión/confirmación, el mismo nonce y
un precio superior a los precios conocidos/concedidos. Si las tarifas reales
anteriores siguen desconocidas, se mantiene esa incertidumbre explícita. Los
metadatos nuevos no se aceptan del cliente: el servidor los obtiene del diario.

La guarda global de nonce conserva su función pública sin bypass: una entrada
interna específica sólo excluye el origen BNB verificado
y sus cancelaciones del mismo nonce, comprobando las demás solicitudes de esa
wallet. Las rutas normales siguen bloqueadas hasta acreditar el resultado.
No tener el hash no autoriza a incrementar el nonce para evitar el bloqueo.

## Conciliación del resultado

Si gana el envío original, comprobarlo con financiación v1 y conservar su
importe real. Si gana una cancelación que cumple los términos revisados,
resolver el origen mediante native-resolution v1, sin reescribirlo. Si la wallet
cambió la tarifa/tipo fuera del mandato, usar la revisión explícita de reemplazo
v80 antes de aceptar su gas. Sólo el recibo ganador será una fuente contable.

El diario procesa evidencia ya leída, sin llamadas de red ni claves. La
interfaz consume la API, que la obtiene fuera de cerrojos/SQL y vuelve a comprobar sesión,
propietario, generación, origen y revisión. El recibo y el cierre del intento
se guardan en la misma transacción SQL. Las lecturas del diario no conceden
otro envío al reabrir.

## API e interfaz

Nueve rutas privadas bajo `/real/accounts/{account}/dex-wallet-cancellations`:
GET de lista y detalle, POST de revisión y acciones `claim`, `observe`, `hash`,
`rejected`, `cancel` y `reconcile`. Cuerpos estrictos, sesión, origen y prueba
EOA; consultas sin autoridad de envío. Sólo la primera concesión devuelve la
transacción. `reconcile` enlaza un resultado ya verificado sin necesitar que
el usuario conserve el hash completo en la aplicación.

En «Envíos de gas», «Cancelar financiación» abre la revisión. Los campos de
precio, aumento y máximo se recuperan de la revisión original. Se explicita la
aceptación de tarifas desconocidas. La wallet recibe el cero a sí misma, nunca
una nueva financiación al destinatario. Rechazo 4001 y respuesta incierta son
estados diferentes. «Vincular resultado» cierra los intentos a partir del ganador
conocido sin duplicar su comisión. Si el backend todavía no tiene estas rutas
(404 durante una actualización), el historial de gas anterior sigue funcionando
y muestra «Cancelación: reinicio pendiente»; no presenta un botón operativo.

## Evidencia actual

- Siete casos del núcleo aprobados con Anvil propio: legacy y EIP-1559 firmados,
  sustitución real al mismo nonce, principal intacto y sólo gas del ganador;
  tarifa ausente con aceptación estricta; firma/alcance incorrectos; carrera en
  la que gana el original; vinculación exacta de tarifa, importe, wallet y nonce;
  máximos y precios previos. No son pruebas de API/UI ni consenso mainnet.
- Treinta y dos casos v81 aprobados tras extraer el lector compartido. Los campos
  persistidos y las reglas de la cancelación operadora → wallet no cambiaron.
- Quince casos del diario aprobados: concesión única concurrente, nueva revisión
  tras incertidumbre, hash perdido, resultado original/cancelación, nonce global,
  rechazo/retirada/caducidad, tarifas/tipo modificados con aceptación externa,
  rollback atómico y recuperación, gasto único, privacidad del archivo, reapertura,
  migración 69→70 con preimagen y fuente intacta, sellos rehasheados, anclas e
  idempotencia que no concede ni modifica una revisión al repetirla.
- Setenta y siete regresiones aprobadas en financiación DEX, API, resolución
  nativa, ingreso MEXC/wallet, swaps y diario de devoluciones. Incluyen el esquema
  fuente nuevo; no equivalen a una suite general ni a una verificación UI v82.
- Cinco casos HTTP aprobados con firma real en la wallet de la EVM propia:
  permisos/origen/proof, cuerpo estricto, concesión única sin clave operadora,
  hash perdido y recibo, tarifa desconocida explícita, reapertura sin otro permiso,
  original minado antes de conceder, conciliación del resultado conocido y
  bloqueo durante RPC sin concesión tardía.
  Las lecturas del backend nunca incluyen `eth_sendTransaction` ni envío raw.
- El primer intento falló por comparar direcciones checksum del fixture con
  direcciones canónicas en minúscula. Se corrigió la expectativa del test;
  no se relajó la normalización de producción. Informes conservados en
  `/home/rich/.local/state/orbis/verification/v82-wallet-cancellation-dev/`.
- Navegador focalizado aprobado dos veces con transacciones firmadas y minadas
  realmente en Anvil: permiso HTTP perdido sin abrir wallet; promesa retenida
  más de 30 segundos y cambio de pantalla; hash tardío no enviado a otra sesión;
  cancelación ganadora, siguiente financiación, rechazo wallet y original que
  gana antes de conceder. Dos recibos contables únicos y pérdida de patrimonio
  igual a su gas real. La segunda tanda comprueba también el backend anterior
  sin la ruta nueva. Sin errores JS ni desbordamiento en escritorio/móvil.
  Evidencia: `v82-wallet-ui-preflight-r2/browser-dex_wallet_cancellation/` bajo
  el directorio de verificación.
- Tanda integrada aprobada: 3.867 casos / 342 archivos, cero fallos/errores/
  omitidos e identidad exacta comprobada; 27 navegadores nuevos y 1.012 fuentes
  congeladas. Las 26 pruebas de contrato se conservan de v81 con fuentes,
  dependencias, helpers y artefactos idénticos, sin recompilación nueva.
  Publicación: `/home/rich/.local/state/orbis/verification/v82-wallet-integrated-r1/verification.json`.
  Servidor PID 387988, UI 200, sin reinicio pendiente y 39 rutas privadas 401.
  La bóveda REAL del usuario permaneció cerrada y no se migró.

Fuentes primarias consultadas el 5-11-2026:
[EIP-1559](https://eips.ethereum.org/EIPS/eip-1559) y
[cancelación en MetaMask](https://support.metamask.io/manage-crypto/transactions/how-to-speed-up-or-cancel-a-pending-transaction).
La política BSC del aumento mínimo está enlazada en
[DEX_NATIVE_CANCELLATION_PLAN.md](DEX_NATIVE_CANCELLATION_PLAN.md).
