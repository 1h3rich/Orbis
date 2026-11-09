# Proyección contable DEX

Estado: motor, puerto, revisión y archivo implementados en desarrollo. Cartera,
riesgo diario, valoración, importación CSV y prefijos MEXC incorporan el método
aceptado; el núcleo de registro conjunto está implementado y probado en
desarrollo. API/GUI de conciliación y coordinación con operativa siguen
pendientes. La instancia cargada continúa en v111.
No se usa ni migra el libro del propietario.

## Registro y aceptación conjuntos: núcleo implementado en desarrollo

[Contrato, verificación y límites actuales](DEX_JOINT_ACCEPTANCE.md).

El comando recibirá fuentes verificadas y selecciones de asientos existentes;
no recibirá saldos, precios ni operaciones arbitrarias del cliente. Admitirá
swaps manuales, operadora y llamadas recuperadas en un mismo lote. Cada fuente
necesita su prueba de bloque, wallet acreditada y cuenta destino enlazada.

1. Capturar originales, cierre de cuentas, pruebas, vínculos y precedentes en
   una vista SQL corta. Fijar ID del lote y fecha de registro de la revisión.
2. Fuera de SQL/cerrojos, ordenar el grafo completo con el mismo planificador
   del replay. Materializar gas en el punto de ejecución: primero BNB en caja,
   después posiciones por el criterio existente. Reutilizar los asientos
   seleccionados, sin duplicarlos ni perder su fecha original. Reproducir el
   resultado completo y los traslados dependientes, sin aportaciones ficticias.
3. Materializar temporalmente eventos y registros de recibo en una transacción
   corta, comprobar originales y capturar testigos/prefijos/precedentes; revertir
   siempre esa materialización al preparar la vista previa. El cálculo pesado
   de revisión se ejecuta después de liberar la sesión.
4. Confirmar revalidando la misma preimagen, materialización y revisión bajo
   `BEGIN IMMEDIATE`. Guardar registros, asientos y aceptación juntos. Cualquier
   fallo o cambio de generación revierte todo; nunca se confirma un libro que
   necesite una aceptación todavía no guardada para poder calcularse.

El archivo aceptado conservará una definición versionada del lote sin
direcciones completas, incluyendo fuentes, selecciones y registro fijado. Una
respuesta perdida recuperará ese prefijo exacto aunque haya movimientos nuevos.
No se cambia el formato de los recibos ni de los eventos publicados. El camino
actual continúa validando sus operaciones antes de guardarlas.

El primer paso de código es extraer el planificador puro y la transición por
miembro del motor ya probado. El planificador permite resolver el gas en su
estado económico real y evita introducir una segunda interpretación financiera.
Los planes de preparación quedan separados de los `ProjectionInputs`
autenticados; sólo la captura de originales materializados produce testigos
admisibles por el archivo de aceptación.

## Auditoría de consumidores

`fold_events` vuelve a ordenar por fecha, registro e ID: ordenar previamente una
lista no cambia el cálculo. `list_events` y `account_snapshot` alimentan cartera,
valoración, riesgo diario, guardas de compras/retiradas, CSV, transferencias y
copias. Las revisiones MEXC reconstruyen además prefijos a fecha de registro;
una conciliación posterior no puede cambiar silenciosamente esos resultados.

El detalle de un evento y el CSV publicados conservan datos originales. Los
enlaces de recibos verifican sus inputs originales, incluidos los manuales con
tolerancia de dos minutos. Las nuevas fechas/posiciones económicas deben ser
metadatos de una proyección aceptada, nunca sustituciones de esos inputs.

Las transferencias pareadas conservan un coste asignado en ambas cuentas. Si
el nuevo orden cambia ese coste, recalcular sólo la wallet produciría otro
inventario incorrecto en la cuenta receptora. Se incluye el cierre transitivo
de cuentas enlazadas por transferencias, sin crear aportaciones ni eventos.

## Motor puro y versionado

- Extraer la transición financiera de un evento del fold existente y reutilizar
  exactamente sus fórmulas. El fold legado conserva orden y contrato cuando no
  exista una proyección aceptada.
- Representar cada transacción DEX certificada como grupo atómico de eventos.
  El puerto autenticado comprueba antes recibo, libro y vínculos. El motor
  exige miembros únicos y una sola compra/venta con sus gastos de gas BNB.
- Usar bloque e índice para ordenar grupos de red; los IDs o llegada HTTP no
  determinan ese orden. Los gastos parciales de un recibo mantienen su orden
  interno declarado y quedan juntos en su instante comprobado.
- Los demás eventos conservan sus fechas. En empates se propone un orden
  estable basado en el registro original; el propietario podrá revisar la
  posición de eventos manuales mediante restricciones explícitas. No se
  permite una restricción que invierta fechas económicas distintas.
- Procesar un grafo acíclico por instante económico: grupos DEX, eventos
  manuales y dependencias salida→entrada de transferencias. Rechazar ciclos,
  grupos duplicados, pares incompletos y contradicciones de bloques/nonces.
- Recalcular el coste de una transferencia de activos desde el inventario
  fuente y trasladar exactamente ese coste al destino. Conservar los dos
  eventos originales y mostrar el coste efectivo como resultado derivado.
  No convertir el traslado en compra/venta ni alterar aportaciones.
- Publicar eventos proyectados separados: original, input económico derivado,
  posición y motivo. El cálculo usa Decimal con contexto propio y devuelve
  snapshots reproducibles de todas las cuentas incluidas.

## Desarrollo de la revisión

`economic_projection_source` une una prueba de orden con su contabilización
original y todos sus asientos. El testigo sellado conserva cuenta, IDs,
digest del input, request y fecha de registro. Se vuelven a comprobar los
originales en cada lectura; cambiar el testigo o el registro de un asiento
invalida su uso. No guarda direcciones, hashes completos de transacción ni
calldata. La reapertura del testigo se ha probado desde una copia SQLCipher.

El destino de un asiento wallet legado no se deduce de su hash público. La
captura comprueba el vínculo actual de la wallet y sella expresamente la cuenta
receptora. Retirar ese vínculo impide nuevas capturas; no borra la evidencia
histórica ya sellada ni le concede permisos de trading.
La verificación exige una identidad ya existente: no usa el helper de primer
uso que crea una sal. Las pruebas con identidad ausente y cambios pendientes
comprueban que una lectura inválida no emite escrituras ni hace autoflush.

`economic_projection_review.capture_projection_inputs` toma una única vista
SQLite, rechaza cambios ORM pendientes y descubre todas las cuentas del cierre
de transferencias. Exige pruebas para cada operación DEX ya contabilizada en
ese cierre, sin aceptar un subconjunto favorable. Devuelve eventos y pruebas
inmutables y huellas de todas las cuentas. La sesión y su cerrojo se liberan
antes de llamar a `review_projection`, que sólo calcula en memoria. Se prueba
el bloqueo de un commit de otra conexión durante la captura y el recálculo
posterior con la bóveda bloqueada.

La revisión compara caja, unidades, coste, comisiones, aportaciones y resultado
por divisa. Identifica las cuentas afectadas y los costes de transferencias
derivados; no sobrescribe el libro ni activa un criterio nuevo al mostrarla.
Un caso con una venta real en una EVM propia y un desempate manual revisado
cambia el resultado de la cuenta origen de −0,01 a −0,02 USDT. El coste llega
a otras dos cuentas por transferencias y cambia el beneficio de una venta
posterior, conservando exactamente caja y aportaciones. Los resultados son
datos de prueba, no operaciones del propietario ni rentabilidades reales.

Estos puertos aún no tienen API/GUI de conciliación. La función cargada de v111
mantiene su semántica anterior; el código de desarrollo ya selecciona el método
aceptado para `account_snapshot`, con originales separados de la interpretación
económica. No se ha reiniciado ni migrado la instancia del propietario.

## Archivo de aceptación de desarrollo

Esquema REAL 74, sólo probado en archivos propios: `real_economic_acceptances`
y `real_economic_acceptance_accounts`. La primera tabla conserva la revisión,
IDs exactos del prefijo por cuenta, versión/límites del motor, vínculos a las
aceptaciones anteriores, digest y sello HMAC. La segunda materializa miembros
y precedentes con FKs y restricciones para impedir bifurcaciones por cuenta.
Los asientos no se cambian. En el código de desarrollo, una aceptación selecciona
el método de las lecturas de cartera, sin mover fondos ni dar permiso para operar.
`accounting_method_active` identifica esa selección; `applied_to_ledger=false`
indica que los hechos originales no se reescriben.

El comando de confirmación captura datos y vínculos actuales, libera SQL y el
cerrojo de bóveda, reproduce el cálculo y vuelve a comprobar datos/precedentes
bajo `BEGIN IMMEDIATE`. El digest recibido del cliente no sustituye al cálculo.
Un bloqueo desde otro hilo durante el replay cancela la generación y evita el
guardado. Dos conexiones SQL independientes confirman una sola fila; un fallo
tras insertar revierte fila y miembros juntos.

Un reintento de respuesta perdida recupera su prefijo original aunque después
se contabilicen operaciones o se unan cuentas. Comprueba por separado la
wallet actual; no exige que el prefijo antiguo se convierta en la revisión
nueva. Se detectó y corrigió esta brecha con dos wallets reales de una EVM
propia, dos aceptaciones independientes y una transferencia que une sus cuentas.

La captura histórica reconstruye los IDs sellados y sus huellas; no utiliza
una consulta temporal aproximada ni incorpora filas añadidas posteriormente.
La selección por `accepted_through` escoge la aceptación vigente entonces.
Se verifica el registro de miembros antes de inferir ausencia o última revisión:
un miembro borrado produce un error, sin volver silenciosamente al fold legado.

Las copias cifradas verifican archivo, linajes, fuentes y replay desde originales.
Los saldos guardados son datos de comparación; incluso un resultado incorrecto
con sello válido, inyectado por una prueba de fallo del escritor, se rechaza al
reproducir y copiar. La migración 73→74 conserva la preimagen cifrada y añade
tablas vacías. Datos de aceptación ocultos bajo un esquema anterior se rechazan
antes de sustituir el archivo activo.

Pendientes del archivo: medición con muchos prefijos/linajes y revisión de su
coste de lectura. No se declara listo el flujo completo hasta verificar el resto
de los consumidores y conectar API/GUI y registro conjunto.

## Lecturas y operaciones posteriores en desarrollo

`accounting_view` captura el cierre de cuentas a la fecha de registro solicitada,
reabre los prefijos aceptados y comprueba los recibos nuevos. El replay conserva
el orden relativo de cada prefijo por cuenta, incluso con empates. Las nuevas
operaciones pueden seguirlo; una inserción anterior exige otra revisión. No se
vuelve al cálculo anterior cuando falta una prueba o aparece una contradicción.

Un nuevo swap certificado puede continuar el libro sin una nueva aceptación
cuando su relación propietario→cuenta ya es única y está probada por el prefijo.
Se comprueban los originales y el índice del nuevo recibo, sin guardar dirección
ni convertir referencias HMAC entre dominios por suposición. Un propietario nuevo,
un vínculo ambiguo o una ejecución anterior requieren revisión explícita.

`PortfolioEvent.input` conserva el original. Su contexto económico contiene el
input efectivo, ordinal, aceptación, prueba de orden y coste de traslado derivado.
El fold consume ese contexto y la API entrega ambos datos separados. Los cálculos
de coste/PNL aceptados y el riesgo diario usan el redondeo canónico del motor;
el contrato anterior se mantiene cuando no existe una aceptación aplicable.

Las escrituras manuales y lotes CSV validan todos los cambios antes de guardarlos.
Los traslados y registros DEX comprueban además el estado resultante antes del
commit. Un recibo nuevo sin prueba de orden se rechaza desde la vista previa;
no deja un libro confirmado que no se pueda calcular. La captura pública de
lectura libera sólo su propia transacción temporal, preservando la transacción
de escritura del llamador. Se reprodujo y corrigió el conflicto lectura→CSV en
una misma sesión, con prueba de rollback del llamador.

La revisión del libro distingue datos y criterio contable. El hash activo liga
las cuentas dependientes y sus fuentes; el criterio identifica las aceptaciones
sin variar por cada operación posterior normal. MEXC compara el criterio actual
con el de su apertura: si cambia, exige una apertura observada nueva. Una revisión
causal archivada anterior se reproduce con el criterio vigente entonces. El caso
se prueba con una wallet de EVM propia y un venue MEXC controlado, sin órdenes.

El núcleo de registro/aceptación atómicos de recibos anteriores o de una wallet
nueva ya se verifica con originales reales en una EVM propia. La presentación
de fechas/costes efectivos y el etiquetado del CSV original también están
implementados en desarrollo. Continúan pendientes la recogida automática de
pruebas, API/GUI de aceptación, coordinación con operativa y el resto de límites
del contrato de lote. No se presenta la inyección de datos de un escritor
anterior como prueba del comando nuevo: éste tiene casos propios de guardado.

## Integración requerida antes de darla por terminada

La aceptación se guardará en un registro cifrado independiente, ligado a
originales, pruebas, revisión del libro, versión del motor y aceptación previa.
Se verificará desde originales al leer/copiar, sin adoptar checkpoints como
saldos ni generar autoridad de trading.

El repositorio incorporará la proyección sólo tras aceptarla. Los metadatos
económicos deberán conservarse al filtrar un prefijo y usarse en todas las
fórmulas temporales. La revisión del libro incluirá la aceptación aplicable;
los prefijos anteriores a ella conservarán su semántica histórica. El CSV
original no se anunciará como una copia completa de una cuenta conciliada.
La copia cifrada sí debe recuperar originales, aceptación y resultado completo.

Para fijar esa integración quedan estas invariantes:

- La aceptación conservará los IDs exactos del prefijo revisado y sus huellas,
  además de la fecha de aceptación. Filtrar sólo por fecha económica perdería
  anotaciones retroactivas; una fecha de registro compartida tampoco identifica
  por sí sola un prefijo exacto.
- Cada cuenta afectada enlazará su aceptación anterior. Unir dos grupos por
  una transferencia debe reconocer ambos linajes, sin dejar una cuenta usando
  un coste anterior y la otra uno nuevo.
- Los eventos posteriores y cronológicamente compatibles podrán prolongar el
  cálculo aceptado. Una inserción anterior que cambie su orden exigirá revisión;
  nunca se vuelve silenciosamente al fold legado al quedar obsoleta una prueba.
- Las lecturas «tal como se conocía entonces» elegirán la aceptación vigente a
  la fecha consultada. Una corrección actual no cambia un prefijo ya utilizado
  por continuidad MEXC ni aparenta que se conocía durante una decisión antigua.
- Captura, cálculo y confirmación serán pasos separados. Confirmar revalidará
  huellas y linajes bajo transacción corta, además de la generación y wallet
  actuales. Una revisión de resultados recibida del cliente no será autoridad
  para guardar sus saldos ni un nuevo orden.
- La copia cifrada incluirá pruebas y aceptaciones reproducibles. El CSV actual
  conserva eventos originales: su exportación debe identificar esa limitación
  cuando una cuenta esté conciliada, sin anunciar una recuperación completa.

La API y la UI mostrarán antes/después de caja, unidades, coste y resultado,
así como las cuentas dependientes. RPC y cálculo pesado fuera de transacciones
y mutex; se revalidan sesión, wallet, generación y originales al confirmar.
Se comprueban respuesta perdida, edición, revocación y reapertura sin autorun.

## Aceptación

Pruebas con cálculos independientes: compra/venta invertidas en el mismo bloque,
anotaciones manuales ±2 minutos, fees en caja/posiciones BNB, ventas posteriores,
transferencia de activos con comisión y cuentas dependientes, flujos de caja,
prefijos temporales, cambios de contexto Decimal y ciclos. Regresión del libro
legado y después de los consumidores enumerados arriba. Finalmente, recorrido
de navegador con transacciones reales en una EVM propia y copias SQLCipher.

Este diseño conserva los formatos financieros publicados. El núcleo puro y el
archivo de orden por sí solos no cierran la conciliación de cuenta ni las demás
brechas del objetivo completo.
