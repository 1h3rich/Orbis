# Orden económico de recibos sin compra o venta

Estado: motor, puertos firmados, registro cifrado, criterios contables,
aceptación conjunta, catálogo e interfaz incorporados en v112; v113 añade el
[asistente de orden manual](ECONOMIC_ORDER_ASSISTANT.md) y v114 la
[revisión de varias wallets](ECONOMIC_MULTI_WALLET_REVIEW.md). La selección explícita
de gas incluye el ya registrado sin exigir un recibo nuevo. Cobertura de 5.208 casos /
435 archivos por identidad y 27 recorridos sobre la misma aplicación. Un montaje
Git de prueba se corrige tras la tanda inicial y se repiten los 18 casos del
módulo; se conservan el fallo y sus salidas. Las 1.275 fuentes finales sólo
difieren en ese test, sin afirmar otra tanda completa con cuatro salidas 0. Evidencia:
`~/.local/state/orbis/verification/v114-economic-multi-wallet-r1/verification.json`.
El esquema preparado es 75; la migración se prueba únicamente con almacenes
propios. El reinicio mantiene REAL cerrado y los seis archivos del propietario
sin cambios; no se abren sus bases desde el cargador, migran ni utilizan fondos.

## Modelo y cálculo implementados

`GasOrderClaim` identifica fuente, contexto y cuenta, prueba, bloque, índice,
emisor, nonce y entre 1 y 100 asientos de gas. No contiene una operación
ficticia. Admite el vocabulario completo de fuentes del flujo previsto: wallet,
operadora, resolución de llamada, despliegue, control, financiación/devolución
de gas y resoluciones de despliegue/movimiento nativo. Un puerto de origen
autenticado deberá construir la prueba antes de que pueda entrar en una
aceptación; un objeto puro por sí solo no acredita nada ni concede autoridad.

El motor comparte coordenadas, grafo, restricciones de nonce y aplicación
financiera entre operaciones y gas. Un principal transferido, aportación,
retirada, compra o comisión de otra moneda no puede declararse como gas. Los
grupos conservan el orden de bloque y sus asientos originales. Los traslados
internos mantienen sus dos patas y no se convierten en flujos externos.

`PendingReceipt.trade` puede ser explícitamente `None`. En ese caso prohíbe
seleccionar o confirmar una operación, conserva el total exacto de gas en wei
y prepara únicamente comisiones. La asignación se hace en el estado económico
de la ejecución: gas existente seleccionado, BNB en efectivo y posiciones BNB.
No inventa liquidez, divisas ni aportaciones. Las fechas e IDs originales se
conservan; los nuevos IDs siguen siendo deterministas por lote y rol.

Las pruebas sin gas independiente conservan `portfolio-economic-order-1.0`.
La presencia de `GasOrderClaim` selecciona `portfolio-economic-order-2.0`;
añadir operaciones posteriores no vuelve a la versión anterior. El informe
puro declara la versión realmente usada. La definición del evento original,
fórmulas y precisión decimal no cambian.

## Compatibilidad comprobada

Se conservaron copias anteriores de los tres módulos. Se comparan cuatro
proyecciones y ocho planes, con gas en caja/posición, fechas desplazadas y
reutilización de asientos. Resultados e inputs financieros completos coinciden.
La comparación adicional sobre 100.007 eventos de operaciones anteriores
conserva el mismo digest completo: 1,117 s antes y 1,155 s después en esta
máquina. Es una observación puntual del motor puro, no una medida de UI,
SQLite, linajes ni rendimiento de gas independiente.

Las pruebas nuevas incluyen las nueve familias de fuente, gas de posición en
su divisa de coste, mezcla en un bloque, nonces/posiciones duplicados,
asignación parcial, empate con traslado interno, reutilización explícita,
cien miembros, insuficiencia de fondos, contexto decimal y preservación de
versiones/originales.

## Fuentes firmadas y certificado de orden

`dex_gas_order_source` reabre las nueve familias con sus validadores originales,
incluidas definiciones, estados, resoluciones, decisiones y concesiones causales.
Captura un snapshot separado del libro y nunca crea una identidad ausente. Los
IDs, contexto, definiciones/estados originales y recibo quedan ligados por un
fingerprint. Conserva los dominios originales de hash y los normaliza a emisor,
propietario y transacción comunes para comparar posteriormente gas y operaciones.

`dex_gas_order` recupera la firma de los bytes reales, vincula nonce y emisor,
comprueba estado, tipo, gas, comisión, inclusión, finalización y posición. Reutiliza
la observación acotada de bloque con segunda lectura idéntica. Sus formas son:

- Creación: destino nulo y bytecode/constructor exactos; dirección derivada del
  contrato y gas de creación, incluso cuando revierte.
- Llamada conocida: destino y calldata originales, valor cero. Incluye permisos,
  revocaciones, STOP y swaps fallidos; prohíbe degradar un swap ejecutado a gas.
- Control: recompila la llamada original. Para configuración, decodifica el
  operador real y coteja su huella antes de comparar calldata y destino.
- Movimiento nativo: valor positivo firmado y destinatario propio identificado
  por huella; conserva el principal por separado de la comisión real.
- Cancelación: autocall vacío de valor cero firmado por el emisor real, que
  puede ser la operadora y no el propietario de la wallet.
- Llamada arbitraria fallida: exige estado revertido y el digest de llamada
  autenticado por su resolución; no se adapta a la llamada original conocida.

El certificado `orbis.dex.gas.order.proof.v1`, con propósito `NATIVE_GAS_ONLY`,
separa principal y gasto y conserva huellas locales de dirección/transacción
y hechos públicos de bloque e índice. Estos últimos permiten correlacionar una
operación con la cadena: no se afirma anonimato. Lo autentica un dominio HMAC
independiente del certificado financiero v1. Reabrirlo requiere originales y certificado válidos; no necesita
la dirección completa, hash completo, firma, calldata, RPC ni clave de trading.
El registro duradero de desarrollo conserva ese certificado y revalida sus
originales al abrirlo; el llamador no necesita conservarlo fuera de la bóveda.

Los snapshots vivos mantienen temporalmente la forma de llamada necesaria. Al
reabrir se descarta esa forma y no se permite usar el snapshot de archivo para
generar una nueva observación firmada. Una fuente movida a otro contexto pierde
su certificado anterior aunque su estructura heredada siga siendo válida.

El formato antiguo de wallet continúa siendo legacy. Una transacción EIP-1559
que ese formato rechaza se verifica mediante la resolución explícita existente,
incluida su aceptación de cambios; no se cambian sus bytes ni sus restricciones
para que parezca legacy. El firmante de una devolución se deriva de los términos
de la operadora, en lugar de asumir que es la wallet propietaria.

Las pruebas usan Anvil y SQLCipher propios: creaciones legacy/1559, controles,
permisos/revocaciones, llamadas fallidas, financiación y devolución originales,
reemplazos de creación y movimientos nativos, cambios aceptados de importe,
cancelaciones de wallet y operadora, ataques sobre firma/nonce/bloque/estado/gas,
certificados alterados, cambio de contexto y ausencia de sal. Captura y verificación
no escriben SQL, detienen procesos ni envían transacciones; los envíos pertenecen
únicamente a la preparación de esas EVM de prueba.

## Registro cifrado e historial común

El esquema 75 añade `real_dex_gas_order_proofs`, con una referencia foránea
exclusiva a una de las nueve familias. Conserva la tabla y el formato de pruebas
financieras anteriores. Las pruebas de migración cotejan también su DDL original,
los asientos, una aceptación previa y el archivo cifrado de versión 74.

Ambos escritores comprueban el historial común bajo `BEGIN IMMEDIATE`: una
transacción, posición de bloque o nonce no puede estar representada a la vez en
ambas tablas. Los recibos de un bloque deben compartir sus anclas y los nonces
mantener su orden. El verificador de copias reautentica todos los originales y
comprueba esas restricciones globales, incluido un registro oculto al reducir
indebidamente la versión del archivo. Las restricciones SQL propias de cada
tabla complementan esa comprobación común.

Previsualizar no escribe. Confirmar vuelve a capturar la fuente, comprueba el
digest y guarda exactamente una prueba; repetir o concurrir desde conexiones
distintas recupera el mismo registro. Un error posterior al `flush` revierte el
lote. Este registro no crea asientos ni contabiliza gas: sólo conserva evidencia
de orden. La observación RPC debe terminar antes de entrar en su transacción.

Las pruebas incluyen las nueve familias, EIP-1559, gas y operación reales en un
mismo bloque y una misma wallet a través de los dominios antiguos de identidad,
en ambos órdenes de importación. La reapertura y la verificación de copias no
necesitan dirección, RPC ni firma. La revisión contable utiliza esos registros
mediante los testigos y lotes descritos a continuación.

## API de observación (desarrollo)

`GET/POST /real/accounts/{contexto}/dex-receipts/{tipo}/{id}/gas-order-proof`
consulta o comprueba el certificado, respectivamente. El backend declara la
capacidad `dex_gas_order_proof`. El GET recibe la wallet en cabecera; el POST
acepta únicamente su dirección transitoria. No acepta un hash de transacción,
calldata, precios, importes, índice o bytes de firma suministrados por el cliente.

Un manejador compartido con la prueba financiera conserva sus límites y
revalidaciones: token actual y prueba de control EOA en el mismo contexto,
captura autenticada del original, descubrimiento acotado en el bloque fijado,
observación RPC fuera de SQL/cerrojos, segunda captura y generación de bóveda
iguales antes de guardar. La variante financiera conserva sus tres tipos y su
ruta/formato anteriores; no admite un swap ejecutado como gas independiente.

Las lecturas de estado y el sondeo pasivo no prolongan el desbloqueo. El estado
guardado se recupera sin RPC al repetir o reabrir la sesión, pero vuelve a exigir
control de wallet. Todas las respuestas privadas usan `no-store`; los fallos de
transporte no exponen endpoints. Revocar el token invalida una consulta pendiente
incluso si una concesión operativa mantiene abierta la misma generación cifrada.
La integración pasa **73 casos / 6 módulos**, incluidos **22 casos HTTP nuevos**,
y tres recorridos de navegador sobre **1.259 fuentes congeladas**. La evidencia
está en `~/.local/state/orbis/verification/dex-gas-order-api-dev-final-r2/verification.json`.
Se consolidan la tanda de 59 casos y navegador con 14 casos de permisos de bot
tras actualizar sólo otra expectativa del catálogo de capacidades. Las demás
1.258 fuentes son idénticas; no se presenta como una nueva tanda completa única.
El puerto de observación por sí solo no crea asientos ni autoridad de trading;
la aceptación y la interfaz usan las capas posteriores de este documento.

## Testigos y criterio aceptado de gas (desarrollo)

`orbis.portfolio.booked.gas.order.witness.v1` autentica la prueba de orden,
la contabilización y cada asiento original, incluidos cuenta, solicitud y fecha
de registro. Sólo admite entre uno y cien miembros `GAS`, sin operación ficticia.
La captura comprueba el vínculo actual de wallet y excluye cuentas MEXC; reabrir
necesita sus originales y sello, sin dirección, RPC ni credencial de trading.
Usa un dominio distinto del testigo financiero anterior y selecciona su perfil
por versión explícita, sin buscar otro tipo cuando falta un registro.

El criterio anterior `dex-trades-1` conserva revisión v1, testigo y resultados.
La primera revisión que aporta un testigo de gas selecciona explícitamente
`dex-trades-and-gas-2`, revisión v2 y borrador de aceptación v3. Exige todas las
contabilizaciones de gas y operaciones del cierre enlazado, con sus pruebas.
Un criterio antiguo no incorpora pruebas nuevas retrospectivamente: su prefijo
se sigue reproduciendo igual y el nuevo criterio conserva su vínculo de origen.
Una revisión posterior no puede omitir el gas ni reducir la versión del criterio.
El esquema SQL permanece en 75; no se añade una migración adicional.

El cálculo actual respeta el criterio aceptado de cada cuenta. Para las cuentas
que ya aceptaron gas, una contabilización posterior requiere su prueba antes
de registrar asientos y debe respetar la frontera del prefijo. Extiende el
cálculo mediante el vínculo histórico de propietario/cuenta, sin guardar una
dirección para ese fin. El gas nativo transferido conserva su principal aparte:
estos testigos acreditan comisiones, no crean aportaciones, compras o retiradas.
Si falta una dependencia, el cálculo falla explícitamente; no usa otro saldo.

El guardado conserva la admisión operativa y el rollback del archivo anterior.
Una solicitud de wallet pendiente, incluso en otro contexto, bloquea el cambio
de criterio sin cancelarla ni iniciar/detener trabajadores. Las pruebas propias
cubren las nueve fuentes, bytes legacy/1559, BNB de caja/posición, gas/operación
en el mismo bloque, prefijos anteriores, extensión posterior, dependencias y
sellos alterados, pérdida de respuesta y copia/reapertura cifrada. Pasan **145
casos / 18 módulos**, con **19 casos nuevos**, y **tres recorridos de navegador**
sobre **1.261 fuentes congeladas**. Dos comparaciones firmadas conservan idénticos
testigos, claims y revisiones completas de compra y venta anteriores. Evidencia:
`~/.local/state/orbis/verification/economic-gas-acceptance-dev-final-r1/verification.json`.
El catálogo optativo permite seleccionar esos perfiles, como se describe abajo.

## Registro y aceptación conjuntos (desarrollo)

`ReceiptSelection.gas_only` selecciona el perfil explícitamente. Nunca se vuelve
a probar como gas una compra/venta que falle su validación. Las nueve familias
requieren su prueba correspondiente; gas prohíbe una operación seleccionada o
confirmar una compra artificial. El constructor HTTP aplica también esa regla.

El manifiesto `orbis.portfolio.receipt.batch.v2` y el borrador de aceptación v4
conservan ese perfil en cada fuente. Los lotes anteriores mantienen manifiesto
v1 y borrador v2 sin campos nuevos. Un lote con criterio de gas puede incluir
compras/ventas con perfil financiero y comisiones independientes. Materializa
los formatos originales de asientos/contabilizaciones; las pruebas y testigos
deciden el orden, sin adaptar una transacción nativa a una operación ficticia.
Una previsualización siempre revierte; la confirmación guarda originales,
enlaces y criterio con un solo commit. Toda captura se repite antes de guardar.

El verificador de archivo liga fuentes, perfil, prueba, vínculos, asignación
de asientos existentes, IDs y solicitudes deterministas y fechas de los nuevos
originales. La recuperación tras respuesta perdida verifica la misma selección
y devuelve el prefijo exacto, aunque ya existan movimientos posteriores. Una
compra nueva conserva un criterio de gas previamente aceptado y su linaje.
No se cambia el esquema SQL 75 ni se crea autoridad de trading o de principal.

La captura de revisión autentica primero sus precedentes. La corrupción del
prefijo es fatal. Si falla sólo la extensión actual, `AccountingReadIssue`
conserva los precedentes y una identidad del historial, sin representar saldo.
El cálculo actual se muestra como error; una propuesta completa puede revisarse
y aceptarse después de validar los prefijos fuera de SQL. Si la extensión ni
siquiera permite obtener su revisión habitual, la metadata `revision_before`
del lote usa un digest de namespace `orbis.portfolio.review.book.identity.v1`,
ligado a originales y aceptaciones. Es identidad de revisión, nunca un saldo
alternativo ni una adopción del orden original como criterio actual.

Las escrituras ordinarias siguen prohibiendo insertar un movimiento anterior
que cambie un prefijo aceptado. La prueba de reparación simula un original
restaurado en una base propia y comprueba también que el comando público lo
rechace. No se añade un bypass a las escrituras de cartera.

Los casos de desarrollo cubren nueve fuentes, legacy/1559, mezcla en bloque,
gas parcial de un wei, rechazo de perfiles/manifiestos manipulados, rollback,
dos conexiones independientes, copias cifradas y HTTP sin RPC para previsualizar,
confirmar, recuperar y revisar un criterio existente. Pasan **141 casos / 16
módulos**, con **21 casos nuevos**, y **tres recorridos de navegador** sobre
**1.262 fuentes congeladas**. Dos comparaciones firmadas de los puertos previos
de captura, materialización y aceptación conservan borradores/digests financieros
completos; los motores puros compartidos tienen el mismo hash. Evidencia:
`~/.local/state/orbis/verification/economic-gas-batch-dev-final-r1/verification.json`.
El catálogo y la interfaz de desarrollo ya envían estas selecciones de gas.

## Catálogo y selección de gas (desarrollo)

La capacidad `economic_review_gas` y `dex_gas_order_proof`, ambas booleanas
exactas, habilitan el control compacto «Gas». Parte desmarcado; al activarlo
consulta fuentes adicionales con `include_gas:true`. El comportamiento sin
opt-in conserva el catálogo financiero anterior. Fuentes resueltas no se
duplican con sus originales; se autentican las nueve familias y su propietario.
Una fila de gas tiene cantidades de operación nulas y no ofrece asientos de
compra o venta. Las cantidades y el precio de una operación financiera no se
utilizan para representar una comisión independiente.

El opt-in se conserva en captura, previsualización, recaptura y confirmación.
Permite revisar gas ya contabilizado antes del primer criterio, sin crear
otro recibo. La selección nueva lleva `gas_only:true`; se comprueban los
bloques mediante `gas-order-proof`, con propósito `NATIVE_GAS_ONLY`, y se
conservan las pruebas financieras en su propia ruta. Un gas nuevo desmarcado
no exige contexto ni consulta de bloque; los gastos ya registrados sí necesitan
su evidencia. Las consultas posteriores a la primera son pasivas.

La interfaz conserva importes precisos en ayudas, oculta valores sensibles,
limpia el opt-in al cambiar de wallet/sesión y recupera por digest una respuesta
perdida sin reenviar la confirmación. Un backend anterior no muestra el control.
El diálogo separa desplazamiento del contenido de encabezado y pie para mantener
visibles el título y los botones en móvil.

Los recorridos reales nuevos usan sólo Anvil/SQLCipher propios: un lote mixto
con operación manual y un wei de gas reutilizados, y otro caso de gas previamente
registrado sin operaciones nuevas. Cubren pruebas de bloque reales desde la UI,
revisión, confirmación perdida, recuperación, bloqueo/reapertura y ausencia de
envíos de wallet. Pasan **186 casos / 15 módulos**, incluidos **20 casos nuevos**,
y **cinco recorridos de navegador** sobre **1.266 fuentes congeladas**. Evidencia:
`~/.local/state/orbis/verification/economic-gas-ui-dev-final-r1/verification.json`.
Ningún almacén del propietario se abre ni migra y v111 no se ha reiniciado.

## Integración pendiente y decisiones

Primera medida propia: el cálculo puro conserva importes y originales con
100.007 asientos (1,381 s). Una cadena de 50 aceptaciones selladas de un único
recibo firmado conserva el primer prefijo y la copia fría; captura actual
0,059 s / 479 consultas, reproducción pura 0,0003 s. Los tiempos se tomaron con
la batería general concurrente y no certifican catálogos ni muchos recibos o
cuentas. El crecimiento de consultas queda registrado para la próxima medida:
`~/.local/state/orbis/verification/economic-gas-lineage-performance-r1/performance.json`.

La incorporación de wallets nuevas simultáneas está cargada en v114.

1. Medir y mejorar catálogo, historial y linajes con volúmenes grandes. Se han
   medido 32 recibos firmados distintos y tres criterios, conservando una copia
   cifrada fría; 0,469 s / 2.078 consultas SQLAlchemy son medidas bajo carga, no SLA.
2. Completar la auditoría del proyecto completo. La batería general y los
   27 recorridos de v114 ya pasan; su cobertura no demuestra las funciones
   todavía parciales ni sustituye el piloto humano mainnet.
