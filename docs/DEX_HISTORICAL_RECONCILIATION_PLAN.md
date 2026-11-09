# Conciliación histórica DEX

Estado actual: v114 carga pruebas de orden, criterios contables, registro
conjunto de recibos/gas, orden manual y revisión de varias wallets desde la
interfaz. Cobertura de 5.208 casos por identidad y 27 recorridos; las 1.275
fuentes finales sólo cambian después en un montaje Git de prueba, cuyo módulo
se repite completo. La evidencia inicial fallida se conserva.
[Evidencia y límites](DEX_GAS_ORDER_EXTENSION.md).
Los apartados siguientes conservan el diseño y las entregas intermedias;
sus notas de desarrollo no significan que v111 siga cargado. Permanecen
pendientes efectos externos desconocidos y rendimiento ampliado.
Este trabajo no concede permisos ni utiliza fondos del propietario.

## Verificación de copias con recibos distintos

Desde v141, la verificación de copias REAL autentica el registro de
aceptaciones y sus enlaces una vez por instantánea SQLite. A continuación
verifica el prefijo de eventos de cada revisión y reproduce su cálculo
financiero completo. Durante esa reproducción, se reutilizan testigos
con JSON canónico idéntico en una caché limitada a 2.048. La caché se
descarta al terminar. Un cambio de transacción invalida la comprobación.

Se midió con copias SQLCipher de 4, 12 y 24 criterios acumulados, cada uno
con un recibo de gas nuevo firmado en Anvil. A 24 recibos, las consultas
bajaron de 10.102 a 2.409 y la ejecución medida de 1,143 s a 0,381 s.
Las dos versiones reprodujeron las 24 revisiones y preservaron el mismo
archivo de entrada. La medición se limita a la verificación de copias de
esta cartera de prueba. Siguen pendientes recorridos más largos con
recibos mixtos, otras operaciones y el piloto mainnet.

## Brecha comprobada

Los recibos antiguos de swaps y operadoras conservan bloque, gas y efectos,
pero no el índice global de transacción. `dex_call_booking._compare` puede
ordenar un mismo emisor por nonce, pero rechaza distintos emisores en un bloque
sin sus índices. Además, un asiento posterior ya registrado en el mismo instante
impide insertar una ejecución anterior: el libro conserva el desempate publicado
por `occurred_at`, `recorded_at` e ID. Cambiar fechas para forzar el cálculo
falsearía el historial. Ambas barreras son explícitas y no se eliminarán sin
prueba de orden y una conciliación contable verificada.

## Decisión de arquitectura

1. **Prueba complementaria de orden.** Leer el hash suministrado temporalmente,
   recuperar firma y comprobar la transacción/recibo/bloque finalizado contra
   el recibo ya autenticado. El índice debe coincidir en transacción, recibo y
   lista del bloque. La firma y el hash no sustituyen el consenso: la finalidad
   continúa dependiendo del RPC seleccionado. Se relee el bloque al terminar.
2. **Archivo independiente.** Conservar sólo referencias HMAC, identidad y hashes
   de los originales, bloque e índice. No añadir campos a los recibos publicados,
   reetiquetar resoluciones, guardar direcciones completas ni hashes de tx.
   El futuro diario debe volver a autenticar originales y sello al reabrir.
3. **Revisión contable explícita.** Fijar el prefijo completo del libro, recibos y
   vínculos existentes. Proponer el orden económico comprobado y reproducirlo
   antes de aceptar. Mantener intactos eventos/fechas originales, coste anterior
   y trazabilidad del cambio; no conceder al ordenador de recibos autoridad para
   cambiar saldos ni duplicar asientos. El formato de conciliación y su lector
   tendrán versión propia. Las proyecciones actuales conservarán su contrato
   cuando no exista una conciliación aceptada.
4. **Concurrencia y recuperación.** RPC sin transacción SQL ni cerrojo privado;
   revalidación de token, generación, wallet, originales y revisión al aceptar.
   Guardar de forma atómica y recuperar una respuesta perdida por ID. Una nueva
   operación invalida una revisión antigua; reabrir no reactiva bots.
5. **Llamadas externas exitosas.** Una transacción diferente que tenga éxito no
   es una cancelación ni el swap previsto. Requiere su propia clasificación y
   prueba de efectos. La prueba de orden sólo acepta ejecuciones económicas ya
   verificadas; no levanta esta otra barrera. Se mantiene dentro del alcance
   pendiente, con revisión de efectos antes de devolver autoridad al bot.

## Modelo de datos y compatibilidad

El primer núcleo publica sólo un formato de desarrollo de prueba, no una nueva
fila financiera. Usa dos módulos: `live_execution.dex_receipt_order` verifica
firma/bloque/índice y `portfolio.dex_receipt_order_source` carga originales por
sus verificadores publicados. El segundo adapta los dominios HMAC antiguos de
wallet/operadora a referencias complementarias comunes; no compara como iguales
digests que pertenecen a dominios diferentes.

La implementación de desarrollo añade el esquema 73 y la tabla independiente
`real_dex_receipt_order_proofs`: ID determinista por origen, contexto de cuenta,
una FK exclusiva a swap, operadora o resolución, payload y sello. Rechaza una
segunda prueba incompatible para el mismo origen y verifica originales y
coordenadas al leer. Forma parte de la comprobación de copias y de la migración
con preimagen cifrada. Sólo se ha probado en archivos propios; no se ha migrado
ningún archivo REAL del propietario ni cargado este cambio en su instancia.

La prueba del núcleo vincula el contexto de cuenta y deriva una referencia
común de transacción de sus bytes firmados. Conserva además la referencia
original de wallet u operadora: esos dominios HMAC son distintos. El registro
no compara digests de dominios distintos ni acepta una conversión declarada por
el cliente. La reapertura no necesita una dirección completa: reconstruye el
origen autenticado y verifica la correspondencia sellada antes de entregarla.

Guardar usa `BEGIN IMMEDIATE`, revalida origen y revisión, y no crea asientos.
La tabla impide duplicar transacción, nonce de un emisor y posición global de
bloque. Las comparaciones de nonces/bloques usan longitud y orden decimal
canónico, sin pasar por INTEGER de 64 bits ni coma flotante. Se admite importar
primero una prueba posterior y después otra anterior cuando el orden es coherente.

La conciliación de cuenta tendrá otro registro append-only: revisión del libro,
pruebas y eventos fijados, versión de proyección, orden económico explícito y
enlace a la revisión anterior. Las fechas originales continuarán accesibles.
Los empates con eventos manuales ajenos o enlaces con fecha distinta requieren
una revisión visible de su posición; no se infiere su instante de un UUID. La
proyección anterior seguirá reproduciéndose como tal. Antes de integrar esto
en `account_snapshot` y en las guardas de nuevas operaciones deben verificarse
todos sus consumidores, incluido CSV, movimientos internos y gas pagado desde
posiciones. Una revisión de orden no puede recuperar autoridad de trading.

Bloque e índice permiten localizar una transacción en la red aunque se omitan
su hash y dirección. El cifrado protege estos metadatos en reposo; las
referencias HMAC no prometen anonimato al revelar coordenadas de bloque.

## Tramos y aceptación

| Tramo | Resultado | Prueba necesaria |
| --- | --- | --- |
| Orden verificable | Núcleo sin escrituras y adaptador de originales WALLET_SWAP, OPERATOR y CALL_RESOLUTION | EVM propia, firma legacy/1559, índices ausentes/contradictorios, fork, finalidad, gas y referencias alteradas; privacidad y originales intactos |
| Archivo | Pruebas selladas y recuperación independiente de los originales | Duplicados, contexto, dos emisores, nonce grande, rollback, copia cifrada, fuente alterada y migración 72→73 |
| Proyección | Conciliación de empates ya registrados, con versión independiente | Coste medio/PNL/caja a mano, eventos existentes sin duplicados, fechas originales intactas y todos los consumidores del libro |
| API y GUI | Revisar y aceptar orden/efectos desde Contabilidad DEX | Cambio de wallet/libro, lock durante RPC, respuesta perdida, escritorio/móvil y cero firmas/envíos |
| Continuidad completa | Renovación/rearme y linajes prolongados con conciliaciones | Presupuestos/pérdidas/gas conservados, prefijos revalidados, límites de memoria/tiempo y lectura fría |

El primer núcleo no se anuncia como función operativa de la aplicación. La
aceptación del pendiente exige el recorrido conectado hasta el libro y la UI,
incluidas llamadas externas exitosas y medición con linajes largos.

## Desarrollo verificado del núcleo

El núcleo y el puerto de originales pasan once casos iniciales, más el caso
con dos emisores económicos reales dentro de un bloque. Éste recupera un
índice que ambos recibos antiguos omiten, sin alterar ninguno. Las firmas
legacy/EIP-1559, los tres tipos de origen, integridad de originales, copia y
reapertura se comprueban con archivos y EVM propios. El orden por coordenadas
no modifica ni recalcula todavía la cartera.

La primera ejecución del caso mixto detectó que Ganache omite `chainId` en su
respuesta de transacción, campo exigido por el verificador wallet v1 publicado.
El diagnóstico registró el `KeyError` en esa comprobación. Se eligió Anvil para
ese caso, que devuelve el campo, sin modificar ni relajar el verificador
productivo. Se conservan los informes fallido y correcto. La regresión conjunta
de las fuentes finales pasa 55 casos en cinco archivos: doce del núcleo y
43 de contabilidad, gas, resolución y privacidad existentes. No es la suite
general ni acredita una entrega conectada. Evidencia de desarrollo:
`~/.local/state/orbis/verification/dex-order-kernel-dev-r2/verification.json`.

El formato vincula también identidad y hash de la definición de la llamada,
concesión original y referencias comunes de propietario/emisor. El verificador
vuelve a derivar esas referencias de la firma y del propietario real antes de
atestiguar el índice. Su límite de 10.000 fuentes es por lote de ordenación;
la futura proyección debe verificar linajes/paginación sin convertir ese límite
en pérdida ni truncado silencioso del historial.

## Archivo de desarrollo verificado

Pasan 23 casos de núcleo/archivo y 87 regresiones de cartera, migraciones,
investigación, contabilidad de recibos, resoluciones y swaps manuales. Las
fuentes actuales no se modifican durante esas tandas. Incluyen dos emisores
reales en un bloque, importación en orden inverso, firmas legacy/1559, dos
conexiones SQL independientes confirmando una sola vez, rollback posterior al
flush, rehash de datos alterados, copia/reapertura sin direcciones y esquema
antiguo con pruebas ocultas rechazado sin modificar el original.

La primera tanda del archivo detectó una comparación incorrecta del digest
de transacción de operadora con el dominio común. Se incorporó una referencia
común explícita al certificado verificado desde la firma, conservando la
referencia original. La tanda fallida se conserva. Los formatos complementarios
siguen siendo de desarrollo y no se han publicado como función operativa.

Evidencia: `~/.local/state/orbis/verification/dex-order-journal-dev-r1/`.
Faltan la proyección contable, API/UI, efectos externos exitosos y linajes largos;
la batería focalizada no acredita la suite completa ni finalización del objetivo.

## Fuentes de la prueba de orden

Consultadas el 6 de noviembre de 2026:

- [Ethereum JSON-RPC](https://ethereum.org/developers/docs/apis/json-rpc/):
  `eth_getTransactionReceipt` incluye posición en bloque; el bloque devuelve su
  lista de transacciones. No se usa el orden de recepción HTTP como orden económico.
- [BNB Chain RPC](https://docs.bnbchain.org/bnb-smart-chain/developers/json_rpc/bsc-api-list/):
  documenta consulta de bloques finalizados. La disponibilidad efectiva del
  proveedor debe comprobarse; no se sustituye por `latest` al faltar finalidad.
