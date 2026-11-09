# Continuidad manual y recuperación de recibos

## v117 · consulta completa de cartera

La consulta de cuenta y la revisión previa comparten sus cabeceras verificadas durante la captura SQL completa. La lectura se abre antes del ámbito y su cierre descarta los datos temporales. Los resultados devueltos conservan los originales y las referencias del método contable. Una modificación SQL entre el registro y la captura posterior vuelve a validar los recibos y se rechaza.

La reproducción inicial comprueba dos autenticaciones de la misma cabecera en la consulta normal y cuatro en la revisión. Las dos pasan con una sola autenticación por lectura tras el cambio. También pasan la mutación intermedia, el corte histórico, la cola de operaciones posteriores, la conservación de coste en transferencias y la reapertura.

Cinco repeticiones de los puertos ordinarios usan la misma copia SQLCipher de doce recibos firmados y once criterios. La lectura de dos cuentas pasa de 7.956 a 3.088 consultas y su mediana de 878 a 306 ms. La revisión previa pasa de 14.911 a 4.333 consultas, de 1.673 a 412 ms. Ambas versiones producen el mismo digest de cantidades, costes y saldos y conservan los originales y criterios guardados. Son mediciones locales sobre ese conjunto de datos.

Una segunda prueba recorre veinticuatro transferencias distintas firmadas con Anvil y veintitrés criterios de aceptación. Verifica el flujo normal de la siguiente orden, las dos wallets, el inventario y coste derivados, el gas de cada cuenta, los primeros originales y la copia cifrada reabierta. En ese recorrido, la última aceptación necesita unos 21 segundos y la lectura de ambas cuentas 689 ms. Las mediciones de mayor catálogo y de preparación y aceptación siguen pendientes.

Pasan 792 casos de los 65 módulos afectados y siete recorridos de navegador con las mismas fuentes. Los diez focalizados forman parte del total. Los demás módulos conservan su evidencia de v116. La referencia de carga está en `~/.local/state/orbis/verification/v117-account-read-snapshot-r1/verification.json`. Las medidas están en `account-view-snapshot-dev-r1/` dentro del mismo directorio de verificación.

## Cambios

La siguiente orden manual admite un recibo externo cuya revisión contable fue
aceptada antes de solicitar la nueva orden. Conserva la comprobación de la
wallet y del plan, el plazo, el nonce nuevo y la concesión de envío de un uso.
El intento anterior conserva sus datos y su estado original. La aceptación
contable carece de autoridad para enviar por sí sola una orden.

La prueba del punto de entrada normal fallaba después de aceptar el recibo.
La comprobación global de nonce lo reconocía, pero la segunda comprobación de
acciones pendientes sólo reconocía la resolución DEX anterior. Ambas lecturas
reconocen ahora la revisión externa autenticada, con su corte temporal.
Se verifican también el rechazo antes de aceptar, la fecha anterior y el nonce
consumido. Los formatos publicados y las concesiones previas se conservan.

## Orden por cuenta

Un mismo nodo externo puede agrupar salida y entrada de una transferencia propia.
Por ello, los órdenes manuales y los prefijos se acotan por los grupos de cuenta y
fecha realmente presentes. La cota anterior por número de nodos rechazaba
agrupaciones válidas de dos cuentas. Continúan las comprobaciones de pertenencia,
duplicados, cobertura, dependencia cronológica y ciclos. Dos ejemplos decimales
independientes prueban el rechazo anterior y el mismo resultado financiero antes
y después de aplicar el orden explícito válido.

## Lectura autenticada

Las cabeceras aceptadas, los bookings externos y los grupos se verifican una vez
por cada lectura SQL protegida. La identidad compartida incluye transacción,
conexión, cambios acumulados y versiones de usuario, esquema y datos SQLite.
Se reutiliza la misma comprobación que ya protegía los resultados externos.

Una escritura SQL, un cambio ORM o un reinicio de transacción descartan las
lecturas previas. Cuando corresponde se caduca también el mapa de identidad ORM,
para que una actualización SQL directa resulte visible. Los borradores ORM
pendientes se conservan para su validación habitual. Las copias privadas de los
payloads impiden que una edición del consumidor cambie el documento verificado.
La información temporal se descarta al salir del ámbito. La respuesta de
recuperación abre una segunda lectura y vuelve a comprobar los originales.

Las pruebas cubren mutaciones de registro, observación, certificado, identidad,
asiento, miembros y enlaces. Incluyen rollback, commit, versiones de esquema y
otra conexión SQLCipher con journals DELETE y WAL. Se conservan las pruebas del
corte temporal y la prohibición de reutilizar el nonce consumido.

## Mediciones delimitadas

Con dos recibos firmados y 25 criterios, la primera comparación de cinco
repeticiones por puerto reduce las consultas de recuperación del grupo de 1.995
a 911 y las del recibo de 2.354 a 935. La repetición con las fuentes finales
mide medianas de 83 ms en ambos puertos, frente a los 272 y 309 ms iniciales. Son mediciones locales sobre el mismo archivo de prueba.

La prueba posterior mantiene dos wallets, 12 transferencias distintas firmadas
en Anvil y 11 criterios aceptados en SQLCipher. Comprueba cantidades, precios de
coste, saldos USDT, gasto de gas de cada wallet, conservación de los primeros
originales y recuperación cifrada. Al final mide unas 1.931 consultas y 224 ms
para el grupo, y 1.955 consultas y 224 ms para el recibo. La lectura conjunta de
ambas cuentas necesita 7.956 consultas y 1,04 segundos. El coste de preparar y
aceptar la duodécima transferencia alcanza 9,76 segundos.

Un experimento posterior con esa copia de doce recibos mide dos lecturas de
cuenta completas. Una envoltura de lectura más amplia en el script conserva los
mismos resultados y reduce las consultas de 7.956 a 2.239, con mediana de 930 a
230 ms. Ese experimento queda como siguiente trabajo de implementación y
verificación. La aplicación publicada utiliza los ámbitos descritos arriba.
Queda trabajo de
rendimiento para catálogos mayores y para las fases de preparación/aceptación.

La preparación de esta medición detectó los dos fallos funcionales anteriores.
Se conserva también el error inicial del script de prueba al acceder a la
conexión y su posterior ajuste de confirmación de operaciones similares.

## Lectura de Patrimonio

Desde v164 la pantalla usa `GET /real/accounts/{account_id}/snapshot?include_events=true`. El saldo, las posiciones y el historial efectivo salen de la misma lista de asientos. Los clientes que usan `snapshot` sin ese parámetro conservan su respuesta anterior. La consulta de precios y la exportación CSV siguen separadas.

La medición invoca la API con dos cuentas y 24 recibos firmados en Anvil, sobre dos copias SQLCipher equivalentes. Cinco repeticiones por versión produjeron respuestas JSON iguales, incluidos costes y criterio efectivo. La mediana local pasó de 1.431 s a 0.741 s para ambas cuentas y las consultas SQL de 12272 a 6136. La medición excluye el precio de mercado. Queda por medir la consulta de catálogos mayores.

## Evidencia

El primer cierre del registro de publicación detectó un campo de estado ausente
en el JSON del comprobador pasivo. Se corrigió la comprobación de sus campos
guardados y se completó el registro sobre el mismo proceso de ORBIS.

La tanda focalizada pasa 68 casos. La regresión posterior recoge completos los
módulos economic, accounting, DEX, wallet, REAL, EVM y portfolio, con 1.392 casos
por identidad en 147 archivos. Su resultado terminal y los 13 recorridos de
navegador quedan documentados en el informe de publicación. Los módulos restantes
conservan la evidencia histórica de la versión previa.

La referencia de publicación es
`~/.local/state/orbis/verification/v116-recovery-continuity-r1/verification.json`.
La medición y sus reproducciones están bajo
`~/.local/state/orbis/verification/economic-recovery-snapshot-dev-r1/`.
La regresión delimitada queda en `economic-recovery-snapshot-regression-r1/`
y los recorridos en `economic-recovery-snapshot-browser-r1/`.

Continúan pendientes los otros adaptadores de nonce, las funciones parciales de
la aceptación global, el rendimiento ampliado y el piloto mainnet configurado
por el propietario.
