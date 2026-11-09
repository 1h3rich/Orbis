# Resolución de swaps y llamadas DEX — desarrollo v83

Base cargada: **v84, esquema fuente 71**. La ampliación está disponible en la
interfaz local; no se ha desbloqueado ni migrado REAL del usuario para cargarla.
Evidencia: `~/.local/state/orbis/verification/v84-wallet-integrated-r1/verification.json`.
El objetivo completo sigue siendo LAB/PAPER → REAL, wallet ↔ MEXC, GRVT long/short
y DEX LONG/DCA con entrada, salida y contabilidad. Los apartados siguientes
conservan la evolución del desarrollo y sus cortes anteriores.

## Búsqueda por nonce

Desde v124, con el hash vacío, **Revisar resultado** localiza el recibo finalizado de
la wallet o la operadora. Para los planes de wallet sin bloque guardado,
la búsqueda retrocede desde el bloque finalizado en intervalos crecientes
y se limita a 262.144 bloques. Para la operadora, usa el bloque del plan y
comprueba la dirección con la concesión histórica. La búsqueda verifica la
coincidencia de emisor, nonce, bloque e índice antes de pasar el recibo
a la revisión existente.

Si el recibo acredita una llamada externa distinta, abre **Recibos externos**.
La confirmación de sus efectos sigue separada de la consulta. El hash puede
introducirse directamente si el RPC no tiene el historial necesario.

## Avance posterior al diario

Actualización de API e interfaz: `dex_call_resolution_api` incorpora POST de
revisión/confirmación y GET privado de estado. Wallet transitoria en cuerpo o
cabecera, nunca en URL. Revalida prueba EOA, generación, huellas del origen y
detención de trabajadores antes y después de la observación; la lectura de RPC
no retiene SQL ni cerrojos privados. No utiliza claves, firmadores ni emisores.
La revisión compartida muestra efectos y gas, y permite recuperar una respuesta
perdida por GET. Se enlazan los asientos desde ambos historiales sin modificar
los originales. La renovación todavía pendiente se identifica y deshabilita.
Cuatro recorridos EVM/navegador aprobados con pérdida real de respuesta, registro,
reapertura, privacidad local y marcadores propios aun sin OHLC, sin velas ficticias.
R2 terminó con 3.947 casos cotejados: 3.946 aprobados y un fallo de rearme DCA,
además de los 31 escenarios de navegador y dos manuales anteriores aprobados.
La reproducción controlada demuestra que Ganache retrocedía el bloque por debajo
del tiempo del recibo ya confirmado al minar con el reloj local más atrasado.
Se corrige sólo `mine()` de la EVM propia para conservar tiempo creciente. El mismo
diagnóstico pasa después; un nuevo caso HTTP de rearme con bloque intermedio
falló antes del cambio. Regresión focalizada aprobada: ocho casos. R3 en marcha con 3.948 casos,
1.029 fuentes y únicamente los dos archivos de pruebas distintos de R2. Se conservan
los informes fallidos, sin publicar ni cambiar las validaciones productivas.
Evidencia: `v83-call-ui-r2/baseline-summary.json`,
`v83-dca-rearm-clock-red2/probe.json` y `v83-dca-rearm-clock-green/probe.json`.

Actualización contable (posterior al corte DCA): nuevo contrato
`DEX_RECEIPT_BOOKING_V4`, fuente `CALL_RESOLUTION`, sobre las tablas genéricas ya
existentes. No cambia el significado de V1–V3 ni requiere otra tabla. El asiento
queda ligado al digest del diario sellado y a su recibo/efecto; el original sigue
pendiente e intacto. Sólo EXECUTED + swap conocido genera compra/venta. Cancelación,
reversión, aprobación y STOP conservan únicamente el gas que corresponda. No se
crean principal, aportación, retirada ni conversión BNB/USDT.

Se reutilizan cálculo decimal, elección de asientos existentes, revisión de
cartera, exclusión de eventos y transacción SQL de la contabilidad anterior.
El adaptador resuelto verifica también la decisión causal del bot. El gráfico
busca el asiento del resultado ganador, no el del original pendiente. La API
contable admite la nueva fuente y exige la misma sesión, origen, prueba EOA,
revisión y confirmación explícitas; no consulta RPC ni firma/envía.

La secuencia contable se comprueba entre swaps manuales, operadoras y llamadas
resueltas. Usa bloque y, cuando ambos están probados, índice de transacción;
un mismo emisor permite ordenar por nonce. Distintos emisores con índices antiguos
ausentes requieren conciliación: no se adivina por UUID o llegada HTTP. Si una
operación posterior ya está registrada en el mismo instante económico, tampoco
se falsea su fecha de registro para insertar otra antes. Esta conciliación
histórica sigue pendiente. Las rutas anteriores conservan su comportamiento
sin resoluciones; al existir una se comprueba la secuencia compartida.

La prueba de continuidad detectó y corrigió otra barrera: `claim_swap` sólo
miraba el estado original y bloqueaba una nueva operación tras resolverlo. Ahora
excluye únicamente resoluciones auténticas ya aceptadas; el nonce consumido y
los envíos realmente pendientes siguen bloqueados. Verificación final aprobada:
255 casos distintos, grupos 70/63/52/34/36, sin fallos, errores ni omitidos sobre
1.025 fuentes sin cambios. Se repiten todos los 185 casos anteriores; los cuatro
recorridos DCA ahora incluyen asientos y marcadores. Contratos/artefacto sin cambios,
se conservan sus 26 pruebas anteriores sin recompilar. Sintaxis JavaScript aprobada,
sin recorrido de navegador ni suite general. Informe `v83-call-booking-dev/verification.json`.
No publicado ni cargado en el servidor del usuario.

Actualización DCA: el diario y el trabajador ya reconocen v2 para continuidad
explícita bajo la misma política. La continuidad sella referencias de resolución
y hechos de activación; las decisiones v2 exigen testigos exactos y su propio
corte temporal. No se reinterpretan decisiones v1. Su lectura actual sí informa
de una resolución independiente aceptada después de detener aquella ejecución.
El supervisor elimina el pendiente resuelto y los datos del gráfico incluyen sólo
swaps efectivos. Cancelación y reversión conservan gas y activación, sin compra.
Cuatro recorridos propios aprobados, incluido v1 → v2 → v2 con dos cancelaciones,
siguiente activación real, copia cifrada y reapertura. Verificación agrupada final
aprobada: 185 casos distintos, grupos 63/52/34/36, sin fallos, errores ni omitidos,
sobre 1.023 fuentes sin cambios. Incluye todos los 177 casos del corte anterior.
Contratos/artefacto sin cambios, se conservan 26 pruebas sin recompilar. Informe
`v83-dca-integration-dev/verification.json`; no publicado, sin prueba general de
navegador ni suite completa. Dos tandas interrumpidas con SIGTERM se conservaron
y repitieron sólo tras comprobar sus procesos terminados. Los párrafos siguientes
describen el corte anterior, cuando sólo estaba preparado el motor puro.

El puerto de resultados verificados distingue pendiente, abandono sin envío,
ejecución, cancelación y reversión. Su corte temporal excluye resultados futuros;
sus objetos son inmutables y las cachés incluyen los bytes del diario resuelto.
La proyección LONG y el rearme v5 conservan posición, coste, gas y presupuesto,
con referencias de resolución explícitas en decisiones y continuidad. Las
versiones anteriores mantienen sus contratos. Rearme con política renovada sigue
pendiente; v5 cubre continuidad bajo la misma política.

La precondición de detención se ha precisado antes de publicar: para un bot se
sellan sus ejecuciones históricas detenidas, conservando la concesión activa si
procede. Una ejecución activa impide resolver. Para una operadora independiente
se exige detener la concesión. Las ejecuciones creadas después no invalidan una
resolución anterior. La verificación histórica usa testigos sellados y evita
recursión entre replay del bot y lectura de sus resultados.

El motor puro DCA v2 reconoce CANCELLED con gas real, sin gasto USDT y con su
activación consumida; no libera la clave para repetir una compra. El diario y
trabajador DCA aún necesitan integrar esta versión. Contabilidad, historial,
API, interfaz y verificación conjunta siguen pendientes. Nada de este tramo
está cargado en la aplicación del usuario. Evidencia focalizada aprobada:
177 casos distintos sobre 1.022 fuentes, sin fallos, errores ni omitidos.
Grupos de 59/52/30/36; contratos y artefacto sin cambios, 26 pruebas conservadas
de la etapa anterior. No incluye suite general ni integración de navegador.
`/home/rich/.local/state/orbis/verification/v83-outcome-projection-dev/verification.json`.

## Frontera y decisiones

Los originales de `wallet_swap` y `dex_operator_journal` conservan su formato,
hash, términos e historial. Una resolución adicional enlazará su definición y
estado exactos con la transacción finalizada del mismo emisor/nonce. No se
liberará un envío por timeout, nonce mayor o mera afirmación de hash.

Verificar primero firma EIP-155 o EIP-1559, cadena 56, nonce, emisor EOA, bloque,
índice, recibo, finalidad observada y gas real. La lectura de RPC queda fuera de
SQL y cerrojos. La llamada conocida se compila desde la definición original;
el cliente no introduce calldata ni pruebas. Las tarifas y gas cambiados son
efectos externos que deben revisarse, no nuevos límites de autorización.

Resultados verificables:

- Cancelación propia: valor cero, datos vacíos, destino al emisor EOA, recibo
  exitoso sin logs y 21.000 unidades de gas. No hubo swap ni cambios de posición.
- Llamada conocida ejecutada: mismos parámetros, destinatario y bytes; verificar
  transferencias/eventos y el resultado real. Permite recuperar una aceleración
  o una tarifa modificada por la wallet sin inventar el precio de mercado.
- Reversión: firma/nonce y recibo fallido sin logs; sólo gas, sin principal,
  compra, venta, aprobación ni cambios en posición. La verificación de llamadas
  fallidas distintas tendrá una entrada separada de sólo lectura; ningún
  firmador se amplía a transacciones arbitrarias.
- Una llamada distinta exitosa necesita conciliación de sus efectos. No se
  convierte automáticamente en cancelación ni en el swap del bot. Este caso
  sigue dentro del alcance pendiente y debe mostrarse como tal.

Extraer los validadores económicos existentes para reutilizarlos con la nueva
prueba de firma/gas, manteniendo el contrato estricto de los validadores v1.
Nunca fabricar una transacción legacy para que una prueba EIP-1559 pase por un
validador antiguo. Persistir sólo referencias HMAC, importes, anclas y resultados;
no direcciones, hashes completos, firmas ni transacciones crudas.

## Secuencia de implementación y aceptación

1. Núcleo de lectura/verificación y efectos compartidos. EVM propia con firmas
   reales, mempool con reemplazo y originales intactos. Casos manuales de compra,
   venta, aprobación/revocación, bot y cancelación/reversión, legacy/EIP-1559,
   gas exacto, firmas o logs manipulados y bloques/índices contradictorios.
2. Diario independiente versionado, migración cifrada con preimagen, exclusión
   cruzada de nonces/recibos y rollback. Adaptar guardas y proyecciones de bot:
   posiciones y gas se concilian sin reponer presupuestos, entradas o permisos.
3. Contabilidad y gráficos desde recibos resueltos, sin duplicar el origen.
   El orden de swaps entre distintas operadoras en un bloque exige prueba de
   índice adicional; no usar UUID, llegada HTTP ni orden de contabilización.
4. API privada y revisión visible para swap manual y bot. Tras pérdida de
   respuesta, recuperar por lectura; bloqueo/cambio de wallet impiden aceptar
   resultados tardíos. Reabrir no firma, envía ni reactiva el bot.
5. Regresión adecuada y navegador con bóvedas/EVM desechables propias antes de
   cargar la ampliación local. No usar fondos ni claves del usuario.

El fixture manual debe ejecutar realmente `multicall(uint256,bytes[])` y sus
transferencias; los recibos sintéticos anteriores no bastan para este recorrido.
Se ampliará sólo el contrato de pruebas, sin cambiar el ejecutor publicado.

## Núcleo implementado y verificado

`dex_call_resolution` lee recibo finalizado, firma legacy/EIP-1559, emisor EOA,
nonce consumido, inclusión del hash en el índice exacto del bloque y coste real.
Relee el bloque tras las consultas. Compila la llamada desde el original y
reutiliza `wallet_effects` / `operator_effects`; las entradas v1 conservan sus
restricciones de hash, tipo y tarifa. Una tarifa modificada en la wallet puede
ser el propio hash original: no se etiqueta forzosamente como otro envío.

La verificación de una llamada fallida distinta es sólo lectura y exige recibo
fallido sin logs. La entrada antigua `recover_wallet_call` sigue exigiendo valor
cero y calldata; no se ha ampliado ningún firmador. Un CREATE fallido conserva
gas de creación y no acredita activos. Una llamada externa exitosa se rechaza
en este núcleo y sigue pendiente de conciliación de sus efectos.

La prueba sanitizada conserva clases KNOWN/SELF_CANCEL/OTHER_CALL/CREATE,
resultado, anclas/índice, gas, efectos y referencias HMAC de transacción y bytes
de llamada. También fija la referencia de la llamada compilada. El futuro
diario debe sellar conjuntamente estas referencias y su origen antes de confiar
en ellas al reabrir. Un hash público de calldata podría correlacionarse con la
wallet; por eso estas referencias son locales y secretas. La repetición de
aritmética no requiere almacenar la dirección original y no sustituye el sello.

- **31 casos nuevos** aprobados, con firmas y minado reales en Anvil propio:
  cancelación al mismo nonce con original realmente pendiente, compras/ventas,
  aprobación/revocación y hash original con tarifa cambiada; bot BUY/SELL
  parcial/STOP; reversión conocida o distinta (con valor, fallback y CREATE),
  rechazo de éxito externo, ausencia de finalidad, firma/logs/gas/bloques/índices
  alterados, cambio de bloque durante lectura y repetición de prueba sanitizada.
- **67 regresiones** de swap, contabilidad manual y diario del bot aprobadas.
  La primera extracción detectó referencias locales mal trasladadas; el informe
  fallido se conserva. Tras corregirlas pasó esa tanda y se repitió con las
  fuentes finales de esos módulos.
- **40 regresiones** de firma, despliegue, lectura de propietario, transporte,
  resolución nativa, cancelaciones y API manual aprobadas. Son comprobaciones
  focalizadas, no una nueva suite general del proyecto.
- Fixture ampliado con `multicall` atómico y transferencias reales del pool para
  swaps manuales. Recompilación y **26 pruebas de contrato** aprobadas, formato
  aprobado. Ejecutable de aplicación y artefacto compilado conservan SHA-256
  `acd44b437da4c3d9b73afe33d1b4d857e7ec47f42a2a9c1ed189e8d8bd0a3a37`.
- Esa primera tanda está conservada en `core-verification.json`. No acredita
  el diario implementado después. Evidencia de ambas etapas:
  `/home/rich/.local/state/orbis/verification/v83-call-resolution-dev/`.

## Diario implementado; adaptación pendiente

Esquema fuente 71, tabla separada `real_dex_call_resolutions`,
con FK exclusiva a un swap manual o intención del operador. ID determinista por
clase/origen, contexto/propietario/firmante, nonce, digests originales fijados,
prueba sanitizada, aceptación de cambios, fecha y sello HMAC. Ningún formato
histórico se modifica. `orbis.dex.call.resolution.v1` todavía no se considera
publicado: faltan proyecciones, contabilidad, API e interfaz.

Las referencias de wallet/tx de `wallet_swap` usan otro dominio HMAC que las del
operador. La resolución deberá atestiguar además las identidades comunes para
exclusión de nonce/recibo contra CREATE, nativos y otros orígenes. No comparar
referencias de dominios diferentes como si fueran iguales, ni guardar direcciones
para poder convertirlas después. La creación verifica ambas desde la firma real
y sella su asociación; la reapertura valida sello, fuente y semántica.

La tabla exige FK exclusiva al origen manual o del operador, emisor/nonce
únicos y recibo común único. El escritor comprueba también los originales de
los demás diarios. Las resoluciones CREATE y nativa consultan esta exclusión
antes de aceptar otro resultado. Los originales resueltos no pueden avanzar
por una respuesta tardía; su nonce consumido permanece bloqueado y posteriores
quedan disponibles. La conciliación del operador requiere su concesión detenida.

La migración y la validación de copias incluyen tabla, recuentos y preimagen
cifrada. Una copia con filas nuevas escondidas bajo una versión anterior se
rechaza. Los hooks de esquemas anteriores no importan el modelo nuevo antes de
migrar, conservando la compatibilidad con el servidor previamente cargado.

El gas resuelto ya se proyecta sobre la concesión: gasto real una vez, reserva
original liberada, exceso explícito y disponibilidad mínima cero. Sólo el coste
externo probado que supera su reserva explica un exceso; la validación del
presupuesto original de las demás operaciones sigue aplicándose. La concesión
permanece detenida y no gana autoridad por conciliar.

La referencia de observación ahora autentica hechos canónicos, digests del
origen y de la concesión. Encabezados finalizados posteriores y anotaciones
auxiliares del RPC no invalidan un recibo fijo. Una prueba separada reprodujo
el fallo antes del ajuste y aprobó después. Al reabrir se verifica esta
atestación del núcleo además del sello del diario y la aritmética.

Última tanda focalizada: **46 casos** aprobados (31 del núcleo y 15 del diario).
Incluye compra manual ejecutada con hash original y tarifa cambiada, cancelación,
reversión distinta con aceptación explícita, concesión detenida, exceso de gas,
concurrencia, rollback, sellos/aritmética/origen alterados, migración 70→71 y
reapertura. Después del ajuste final de compatibilidad aprobaron también
**116 regresiones anteriores** (68 + 48) sobre las mismas fuentes. Los 162 casos
Python son distintos, sin fallos/errores/omitidos; no equivalen a una nueva suite
general ni a la integración de navegador. Se conservan las 26 pruebas del
contrato de la etapa anterior con sus fuentes/artefactos idénticos. Informe:
`/home/rich/.local/state/orbis/verification/v83-call-resolution-dev/journal-verification.json`.
No se ha cargado v83 ni abierto/migrado REAL del usuario.

Los lectores nuevos deben distinguir original y resultado efectivo al recorrer
posición, compras acumuladas, pérdidas, trailing, gas, rearme y renovación. No
reescribir el recibo v1 con una tarifa diferente ni pasarlo por su validador
antiguo. El gas externo puede agotar o superar una concesión: conservar el gasto,
mostrar el exceso y bloquear autoridad adicional, sin invalidar un hecho real
ni reponer presupuesto. Contabilidad y gráficos usarán una sola fuente ganadora.
La prueba del índice guardada aquí prepara el orden entre operadoras, pero no
resuelve por sí sola el orden de asientos históricos ya registrados.
Una cancelación propia exitosa no debe pasar a DCA como REVERTED ni como una
solicitud nunca enviada. Las nuevas proyecciones/testigos deben conservar su
resultado explícito y respetar los contratos de las versiones anteriores.

Fuentes primarias consultadas el 5-11-2026: [EIP-140](https://eips.ethereum.org/EIPS/eip-140),
[EIP-1559](https://eips.ethereum.org/EIPS/eip-1559),
[MulticallExtended](https://github.com/Uniswap/swap-router-contracts/blob/main/contracts/base/MulticallExtended.sol)
y [Multicall](https://github.com/Uniswap/v3-periphery/blob/main/contracts/base/Multicall.sol).
Las pruebas locales acreditan ejecución y mempool; no consenso mainnet.
