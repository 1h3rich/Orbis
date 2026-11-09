# Derivados observados: continuidad de LAB a PAPER

Estado: núcleo, archivo cifrado, recogida pública, trabajador, coordinación, API
e interfaz cargados y verificados en v103. El recorrido nuevo pasa en dos
procesos con gráfico, diario, exportación y reapertura sin autoarranque.
La evidencia está en `~/.local/state/orbis/verification/v103-observed-ui-r3/verification.json`.
[Uso](PERP_OBSERVED_USAGE.md). El PAPER GRVT anterior conserva sus contratos,
financiación y controles; el objetivo completo sigue activo.

## Plazo de observación — v104 cargado y verificado

El plazo fijo de quince minutos impedía evaluar estrategias de horas o días.
`PaperPerpAccess` permite ahora elegir una duración al crear/desbloquear el
almacén; el API exige segundos enteros entre 900 y 2.592.000. La UI ofrece
15 minutos, 1/4/24 horas y 7/30 días. Omitir el campo conserva los quince minutos.
No se escribe una política ni contraseña en disco y las consultas no renuevan
el vencimiento monotónico. Se devuelve la duración elegida y el tiempo restante,
sin exponer claves ni conceder permisos REAL.

Bloquear, caducar, reautenticar o cerrar ORBIS mantienen la revocación de épocas.
Reautenticar incluso con la misma contraseña crea otra época y no traslada
trabajadores. Reabrir empieza bloqueado; el formulario vuelve a quince minutos.
Un bloqueo puede impedir guardar GAP: se informa de ello y se conserva la
exposición anterior, sin inventar un cierre. Extender el plazo no garantiza
cobertura entre puntos ni ejecución 24/7. Los demás almacenes PAPER mantienen
sus plazos actuales; no se cambia LAB, Spot, DCA ni la bóveda REAL.

Las pruebas usan relojes controlados para comprobar los límites exactos, sin
pretender haber observado treinta días de mercado. También comprueban un
trabajador real con exposición, continuación después del umbral de quince
minutos con clave larga y expiración de la predeterminada. Los navegadores
verifican creación de 24 horas, desbloqueo de siete días, contraseña borrada,
reapertura bloqueada/default y el recorrido completo de compra/cierre.

## Reglas de ejecución

La cuenta compartirá `perp-account-0.1`, el evaluador `lab-1.16`, sizing, costes,
modos cross/aislado y política de posiciones con la investigación. Tendrá capital
ficticio nuevo, sin heredar el resultado LAB. Una referencia OOS reproducida fija
versión, perfil y datos de origen; no concede autoridad REAL.

El consumidor de observaciones requiere otro motor, `perp-observed-1.0`, sin
reinterpretar `perp-ohlc-1.0`. No interpola entre ticks ni usa el precio de última
operación como precio ejecutable. La compra usa ask y la venta bid, con el
deslizamiento adverso/tick configurados. Stops/objetivos se activan con el lado
ejecutable: bid al cerrar LONG, ask al cerrar SHORT. El spread observado puede
cambiar el resultado respecto al backtest. MARK decide margen/liquidación.

Los fills ficticios se registran en el instante de recepción verificada, con
el tiempo del proveedor separado. Una vela debe estar cerrada y ser conocida al
decidir. El seed inicial sólo calienta indicadores; nunca genera entradas
retroactivas. La primera señal operable procede de una nueva vela recibida después
de activar. Trailing y reglas de salida usan velas cerradas, igual que LAB.
Si la entrada llega después de abrir su vela, no se atribuye a la posición el
máximo/mínimo completo de esa vela parcial: podría haber ocurrido antes de entrar.
Se registra la exclusión y se espera una vela completa posterior para ese trailing.

Funding debe tener ancla, continuidad y cobertura hasta la recepción que se va
a procesar. Un pago nuevo se aplica una vez, antes del MARK/fill del mismo
instante. Se usa la posición que existía entonces. Un cambio retroactivo de datos
ya consumidos se rechaza. Se conserva la limitación pública conocida: una nueva
cadencia posterior al último registro puede no ser detectable antes de aparecer.

Las reglas actuales y MARK recibidos juntos deben aplicarse atómicamente antes
de comprobar mantenimiento. Comprobar primero el nuevo MARK con reglas obsoletas
podría liquidar una cuenta que las reglas nuevas mantienen solvente. No se
aplicarán reglas conocidas posteriormente a un funding anterior. Sigue siendo
un modelo experimental de puntos observados, no un libro privado del exchange
ni prueba de supervivencia entre cotizaciones.

## Historial de margen en PAPER

Abre **Ver** en una sesión observada y despliega **Margen GRVT**. La vista presenta las reglas iniciales y hasta 20 cambios de posición máxima o tramos de margen, con valores decimales exactos. La hora de captura corresponde a la consulta de GRVT. La hora de aplicación es la recepción registrada en PAPER. Las consultas repetidas con los mismos valores actualizan la última captura aplicada y no añaden cambios.

La vista se reconstruye desde las transiciones verificadas del snapshot seleccionado y sirve también para sesiones guardadas antes de v196. **Exportar JSONL** conserva todos los inputs de la sesión, incluidos los anteriores al límite de la vista. Las reglas aplicadas en PAPER son evidencia puntual. Su vigencia en el exchange entre capturas sigue sin acreditarse. La financiación con sello previo a la recepción de una cotización se liquida bajo las reglas de la observación anterior.

## Interrupciones y control

Cotizaciones futuras, antiguas, contradictorias o sin cobertura no permiten
operar. Un hueco de observación no autoriza stops ni compras retrospectivos.
La sesión conserva posición/caja y el intervalo desconocido; la reconstrucción,
si se incorpora, tendrá etiqueta y protocolo distintos de la observación.

Parar sin posición termina la sesión. Parar con posición pasa a cierre solamente:
no abre más, mantiene financiación y protección hasta cerrar. Un cierre manual
es una petición de salida a la siguiente cotización verificable, no una venta
inventada al pulsar. Reabrir la app o desbloquear una base no reactiva sesiones.

## Cortes de aceptación

1. Motor puro y oráculos: LONG/SHORT, bid/ask, fees, sizing, margen, cambios de
   reglas atómicos, funding/duplicados/cobertura, stops, trailing, salida por regla,
   tiempo máximo, STOP, huecos y ausencia de datos futuros. Misma secuencia de
   eventos debe producir el mismo libro que las operaciones contables compartidas.
2. Archivo cifrado de estado/observaciones/eventos, época de acceso, cadena e
   identidad fijadas, reapertura detenida, replay y exportación verificables.
   Migración aditiva en GRVT PAPER, conservando tablas/filas antiguas.
3. Adaptadores/trabajador con observaciones actuales, financiación y datos de
   reglas completos; no HTTP durante bloqueos SQL. Drenaje y leases integrados.
4. API local e interfaz: elegir validación, revisar capital, activar ficticio,
   inspeccionar marcas/diario/estado, parar y recuperar. Recorrido en dos procesos
   con fuentes controladas, sin claves/fondos del dueño ni creación de autoridad REAL.
5. Después: revisión REAL versionada que verifique esta nueva procedencia sin
   cambiar el significado de las referencias y concesiones ya publicadas.

## Corte interno v99 comprobado (histórico)

`derivatives.perp_observed` y `perp_observed_types` implementan el primer corte.
`mark_with_rules` es una operación contable aditiva: los nodos AST anteriores de
`perp_account` son idénticos a v98. No cambia `mark_all` ni `replace_rules` y ningún
módulo previo importa todavía el consumidor observado. No hay tablas, API,
sesiones, trabajador ni botón nuevos publicados.

Pasan **25 casos nuevos** con XML persistido: LONG/SHORT y cross/aislado con
cifras manuales, recepción y quote separados, seed sin entradas, ausencia de
cierre final artificial, MARK y financiación antes de salidas, pagos/cobros una
sola vez, cambios de reglas atómicos, STOP/cierre en cotización nueva, huecos,
duplicados, velas futuras, contexto Decimal, trailing y su vela parcial, salida
por regla/tiempo, y conformidad contra la secuencia contable compartida.

La comprobación preliminar ampliada pasa **157 casos / 7 archivos** sobre este
código; su salida queda en la sesión 63248 y no tiene XML. La evidencia XML de
este corte cubre los 25 casos nuevos; no se afirma otra regresión completa del
producto. Manifiesto, prueba AST y alcance en
`~/.local/state/orbis/verification/v99-observed-core-r1/verification.json`.
El índice de entrega cargada continúa apuntando a v98. No se reinicia el servidor
ni se abren bases de datos o utilizan claves/fondos del dueño para este corte interno.

## Archivo cifrado v100 y observador v101: internos

El esquema GRVT PAPER 13 añade sesiones, inputs/eventos, velas y operaciones
observadas, conservando las tablas y filas anteriores. Sólo admite el nuevo
archivo cifrado y desbloqueado. El origen es una reserva OOS reproducida con
versión/perfil/definición y prefijo TRADE exactos. Las sondas LAB permanecen
fijadas hasta publicar el seed, sin trasladar su beneficio al capital ficticio.

El checkpoint es evidencia, nunca una vía para cargar una cuenta financiera.
La reapertura, exportación y comprobación de copia repiten los inputs desde el
seed y comparan libro completo, señales, operaciones, eventos, velas y head.
Una lectura puede fijar un prefijo mientras llega otro tick, indicando si ese
snapshot sigue siendo el último. Los eventos paginados verifican su hash
individual y no se anuncian como repetición completa. El export JSONL exige un
footer final con recuento/head/SHA256: truncarlo no produce un export válido.

Durante la observación se fija en RAM el head esperado y un estado inmutable
calculado por el motor. Cada append exige la misma época de desbloqueo, archivo,
head y runtime antes/después de SQLCipher; un input idempotente no se ejecuta
dos veces. La comprobación de cada tick no recorre de nuevo todo el prefijo
archivado. Una alteración de inputs antiguos se detecta al repetir/exportar y
no cambia el estado financiero ya calculado en RAM. No se afirma un escaneo
completo contra manipulación en cada tick.

Las identidades en caché y el origen efímero contienen un HMAC con secreto local
de proceso, dispositivo/inodo y época; no conservan otra copia de la contraseña.
Bloquear, caducar o reautenticar incluso con la misma contraseña invalida y
descarta el cursor. No se promete borrado seguro de objetos de memoria Python.
Las copias online cifrado→cifrado usan una contraseña independiente, se repiten
antes de publicar y no pasan por un archivo en texto plano. Cambiar la sesión
durante la copia cancela la publicación. Si falla el sync después de publicar,
se informa de la copia que sí existe en lugar de afirmar que no se guardó.

`GrvtObservedSource` utiliza los adaptadores públicos existentes, sin claves:
extiende el prefijo con páginas cerradas, coteja dos velas solapadas, exige
funding real y consulta bid/ask/MARK después de los demás datos. Los metadatos
se refrescan a los 30 segundos conservando su hora original de consulta. Si una
petición cruza un cierre o liquidación de funding, se recogen los datos que
acaban de vencer y una nueva cotización. No se interpreta un error como tasa
cero. La recolección de cada tick sólo convierte la cola necesaria del histórico.

`ObservedSessionWorker` requiere arranque explícito y un único observador por
cursor. No hace HTTP dentro de un bloqueo SQL. Diferencia hebra viva,
recogiendo datos y cobertura reciente; los puntos REST no se presentan como
observación continua por WebSocket. Un cierre solicitado se ejecuta a la
siguiente cotización verificada. Apagar el trabajador conserva una posición
abierta y registra GAP, sin generar un cierre artificial. Un resultado que
llega después de cancelar se descarta. Si PAPER ya está bloqueado y no puede
escribirse GAP, el estado RAM se revoca y se indica que la interrupción no se
guardó; no se anuncia una escritura que no ocurrió.

No se inicia automáticamente una sesión de otra vida del proceso. Leer una
sesión guardada no la conecta a datos ni concede autoridad REAL. En v101 aún
faltaban coordinación, cancelación HTTP y registro de trabajadores bajo lease;
v102 los integra como se describe abajo. Controles compactos, marcas y recorrido
completo en dos procesos siguen pendientes. También falta el protocolo para
reconciliar una posición tras GAP: no se podrá presentar ese tramo como
observación continua ni abrir nueva autoridad REAL a partir de él.

## Evidencia de archivo y trabajador

El corte v100 previo pasa **494 casos / 22 archivos** y cuatro recorridos de
navegador sobre su manifiesto, sin saltos. Es anterior a la corrección de
identidades HMAC y al nuevo trabajador, y no los acredita. La corrección de
identidades pasa 59 casos focalizados. Se conservan los intentos de desarrollo,
incluidos fallos de granularidad de fixture, llamada incorrecta al codec y
comparación textual de decimales equivalentes; no se relajaron fórmulas.

La regresión v101 ejecuta **515 casos / 24 archivos**: 514 pasan y falla una
expectativa del nuevo apagado con posición. La prueba esperaba 1.000 de caja
libre, omitiendo la garantía aislada. Se corrige sólo la prueba para exigir
500,02 libres + 499,98 de garantía y la cuenta exactamente igual antes/después
de apagar. Los siete casos de su archivo pasan en la repetición. Todas las
fuentes de aplicación y los demás tests son idénticos a la tanda de 24 archivos.
El consolidado acredita **515 casos**, cero fallos/errores/omitidos, como tanda
más reparación focalizada, sin inventar otra repetición completa. Incluye
**78 casos del consumidor/archivo/codec/recogida/trabajador** y el transporte
GRVT de producción contra endpoints propios controlados; no comprueba el
servicio público ni opera con fondos del dueño.

Manifiestos, XML originales/consolidado, alcance y brechas:
`~/.local/state/orbis/verification/v101-observed-worker-r2/verification.json`.
El índice de entrega cargada continúa en v98; no se reinicia el backend ni se
abren bases del propietario. No hay handoff nuevo operativo en la app.

## Siguiente recorrido de producto: activación explícita

1. Desde una validación con reserva completa, preparar una revisión de versión,
   mercado, temporalidad, capital ficticio nuevo y perfil de ejecución. Repetir
   los originales LAB y fijar su identidad. Una comparación de recorridos sin
   reserva no puede preparar esta activación.
2. Al confirmar, recoger el seed y los datos públicos frescos fuera de SQL.
   Comprobar de nuevo acceso/originales; publicar sesión e iniciar un único
   trabajador. Una petición repetida debe devolver el resultado original,
   incluso tras perder la respuesta, sin resetear capital ni duplicar hebras.
3. Cancelar antes o durante esa preparación debe impedir un arranque tardío.
   El registro del servicio y sus preparaciones necesitan límites, caducidad
   y drenaje; no basta el registro individual del trabajador. El lifespan debe
   retirar primero todas las autorizaciones de arranque, después esperar a
   trabajos/hebras y por último liberar la lease PAPER.
4. Mostrar preparación, conexión, cobertura y estado guardado por separado.
   Un cursor adjunto, una hebra viva o una respuesta antigua no equivalen a
   observar precios recientes. Inspeccionar las velas, marcas, operaciones,
   diario y procedencia; STOP con exposición significa sólo cierre, mientras
   que apagar el observador produce una interrupción.
5. Verificar el recorrido completo en dos procesos con bases propias cifradas:
   validar/preparar/activar, señal nueva, entrada, cierre, exportar, reiniciar y
   recuperar el mismo resultado sin autoarranque. Comprobar además cancelación,
   respuesta perdida, bloqueo, hueco y apagado con exposición. Sólo entonces
   publicar el handoff en la aplicación y revisar después su procedencia REAL.

## Coordinación y HTTP v102 (desarrollo, no cargado)

`ObservedPaperService` registra preparaciones y activaciones distintas. Preparar
repite LAB y fija su identidad y la época PAPER; no consulta precios ni crea
una cuenta. La revisión dura cinco minutos. Activar sólo acepta esa preparación,
recoge datos actuales fuera de los cerrojos, comprueba ambas identidades y
publica capital nuevo antes de iniciar un único trabajador. La identidad PAPER
esperada se propaga hasta el commit para impedir que bloquear/desbloquear
durante la descarga adopte silenciosamente una nueva sesión.

Una petición repetida devuelve la misma sesión, incluso tras perder la respuesta
o reiniciar el proceso. En una reapertura, eso no vuelve a conectar el libro.
Solicitudes concurrentes con un ID compartido y preparaciones distintas se
rechazan como conflicto, sin reconocerlas ambas como la misma configuración.
El registro conserva cancelaciones recibidas antes del POST; no arranca después
de cancelar durante datos o después del commit del seed. Si el trabajador ya
había arrancado, la cancelación solicita interrumpirlo: puede conservar una
posición, nunca promete deshacer una operación ya observada.

Los límites actuales son 32 revisiones, ocho observadores y 256 solicitudes
retenidas. Los registros publicados ya terminados se pueden retirar de RAM
porque SQL conserva su idempotencia. Las cancelaciones permanecen hasta el fin
del proceso para no permitir reintentos tardíos. No se arranca durante drenaje.
Bloquear/desbloquear PAPER revoca revisiones y cursores anteriores; no renueva
el plazo de las claves ni reconstruye autoridad. Al cerrar la aplicación se
retiran primero todos los permisos de arranque, se espera a los trabajos HTTP
en curso y se unen los trabajadores antes de liberar la lease PAPER.

La API `/paper/perp/observed` ofrece preparación, activación, consulta/cancelación
de petición, sesiones, replay, eventos, STOP, cierre y export JSONL con footer.
Exige loopback/mismo origen y PAPER cifrado desbloqueado. La cancelación funciona
también bloqueado, devolviendo sólo el estado mínimo del control. Los modelos
rechazan opciones REAL y valores adicionales. Los controles usan recepción del
servidor; un reintento conserva la hora original en vez de crear otro input.
La exportación comienza a responder sólo después de la primera reproducción.

Se añaden 13 pruebas de servicio y cinco HTTP. El recorrido HTTP usa un
trabajador real con datos públicos propios controlados: entrada por vela nueva,
posición, petición de cierre, cash final 1.074,997, replay y export verificables.
Pasan preliminarmente **19 casos** incluyendo el ciclo de vida. La regresión
pasa **560 casos / 30 archivos**. El navegador detecta un mensaje de bloqueo
inconsistente: `strategy-versions` rechazado con 423 puede convertirse en
«LAB no disponible» por el orden de rechazo de Promise.all. Se corrige esa rama,
se versiona su asset a v2 y se fuerza el orden que fallaba en el recorrido.
Pasan 19 casos del archivo web; todo Python de aplicación y los demás tests son
idénticos a la tanda de 30 archivos. El consolidado conserva 560 casos sin
fallos/errores/omitidos, como tanda más reparación focalizada, no otra repetición
completa. Pasan los cuatro recorridos actuales sobre fuentes finales: básico,
validación y protección con reapertura en otro proceso, y handoff GRVT anterior.
Los fixtures `sqlite://` conservan errores legacy preexistentes de preferencias
y afinidad de hilos en rutas ajenas; no se afirma que estos recorridos auditen
todas las respuestas HTTP. Evidencia final, originales y alcance en
`~/.local/state/orbis/verification/v102-observed-api-r2/verification.json`.

La API está integrada al nuevo lifespan en el árbol de desarrollo; el servidor
del propietario sigue en v98. No hay un nuevo control de activación publicado
en la interfaz, ni operaciones, claves o bases del propietario utilizadas.

## Interfaz y snapshots v103 (verificación general en curso)

La validación muestra **Probar en PAPER** sólo para reservas completas sin
exposición. Abre la revisión de mercado, versión/perfil, dirección y capital
ficticio; **Activar ficticio** sigue siendo otra decisión. El widget reutilizado
en LAB y Perpetuos PAPER muestra biblioteca paginada, estado/cobertura, STOP,
cierre, inspección, gráfico, operaciones, diario y exportación. Una respuesta
perdida conserva sólo IDs pendientes en sessionStorage y permite consultar o
cancelar, sin repetir POST de activación. Desmontar un arranque todavía pendiente
envía cancelación; no detiene una sesión ya confirmada al navegar.

`ObservedReplayView` toma selecciones acotadas de las transiciones calculadas
durante el replay completo, no de filas cargadas como si fueran cuentas. El
checkpoint sólo elige una ventana; su integridad debe comprobarse antes de
devolverla. Usa el cálculo compartido de equity y Decimal para importes. La
conversión a Number sólo representa coordenadas visuales.

El gráfico conserva velas cerradas y puntos de fills separados. Las entradas
ámbar y salidas rojas tienen series distintas: no se sustituye el precio de
entrada por el de salida si ambas comparten segundo. No se clampa la entrada
a una vela anterior ni se fabrica OHLC para una vela abierta. La marca visual
usa segundos y el diario conserva nanosegundos. Una liquidación sin precio de
fill no se dibuja como venta; su hecho permanece en operaciones/diario.

El rango es el componente común, con carga hacia atrás. Las páginas anteriores
fijan secuencia/hash del snapshot que se estaba inspeccionando: se repite su
prefijo histórico antes de mezclar velas/marcas. La respuesta indica si sigue
siendo el último, sin cargar el estado financiero desde esa evidencia guardada.
La biblioteca puede actualizar cobertura; los importes inspeccionados conservan
su fecha hasta otro replay. PAPER bloqueado retira los valores del widget.

La prueba nueva usa bases propias cifradas, servicio/worker/HTTP reales y datos
públicos controlados. Crea una validación en la UI, revisa/activa, observa una
vela nueva, compra/cierra, comprueba P/L y export, carga >1.000 velas y recupera
el mismo resultado en otro proceso sin autoarranque. Comprueba coordenadas y
píxeles de las marcas, escritorio/móvil, respuesta perdida sin otro POST,
cancelación anterior al POST y acceso desde las dos vistas. Sus rutas legacy UI
usan una base en memoria propia compartida y serializada, para no aceptar los
500 de preferencias que tenían otros fixtures anteriores.

En desarrollo pasan 41 casos del API/archivo/web y 12 del API/vista más el
recorrido nuevo completo. La primera expectativa de motivo de cierre usó
`MANUAL_REQUEST`; se corrigió el test al código existente
`MANUAL_CLOSE_OBSERVED`, sin cambiar el motor. La tanda final está fijada en
`~/.local/state/orbis/verification/v103-observed-ui-r1/`: 4.365 casos/378 archivos
en ocho procesos y once recorridos de navegador. No se declaran aún aprobados
ni cargados. Continúan la reconciliación tras GAP, revisión REAL, reglas
históricas acreditadas, ejecución avanzada y el resto de brechas del proyecto.

## Aceptación del consumidor y corrección final de v103

La batería general pasa 4.365 casos/378 archivos en ocho procesos. Después se
reproduce una carrera: el proveedor ya había entregado una quote y un control
de cierre se registraba antes de que el consumidor la aceptara. Interpretar la
hora de recogida como aceptación provocaba GAP aunque el punto siguiera siendo
válido. Descartar ese punto tampoco sería correcto: podría borrar un MARK de
liquidación conocido.

El consumidor fija ahora la recepción al aceptar bajo su cerrojo, conservando
`collected_at_ns` y el tiempo del proveedor. Valida que quote, metadatos,
financiación y velas ya fueran conocidos al recogerlos, y que sigan siendo
válidos al aceptarlos. MARK/funding mantienen prioridad sobre el cierre. Un
punto fresco recogido antes del control puede aceptarse después; no se
retrofecha el fill. Los inputs anteriores que omiten este campo conservan su
payload, resultado y hash exactos al reproducir; dos exports antiguos lo
comprueban. Las comprobaciones nuevas incluyen cierre a 99 tras entrar a 100
(cash 975,001), liquidación previa al cierre voluntario y rechazo de datos
futuros que sólo vencerían durante esa espera.

La cobertura visual exige tanto recepción reciente como quote del proveedor de
como máximo 15 segundos. Una hebra viva con un precio vencido no muestra
«Observando». El estado financiero no se calcula de nuevo para comprobar esa
bandera operativa.

Pasan 575 casos focalizados tras la corrección de aceptación y otros 28 tras
la bandera de frescura. El consolidado comprueba las **4.374 identidades actuales**
de tests; no afirma otra ejecución completa adicional de la suite. Las fuentes
ajenas a esas reparaciones permanecen idénticas. Once recorridos afectados
quedan comprobados por etapas, repitiendo el nuevo en dos procesos tras cada
cambio del trabajador. El manual antiguo sólo necesitó apuntar su mock a la
ruta paginada publicada; no se alteraron sus cálculos.

Benchmark propio previo al ajuste de ingreso: 100.000 velas, cuenta plana y tres
eventos; creación 3,56 s, append 0,27/0,36 s y replay/vista 3,70 s, mientras
corría la suite. La respuesta visual conserva 1.000 velas/87,5 KB. Pico de RSS
615.704 KiB, incluidos datos/fixtures de ese proceso. No mide pintura UI,
`accept_quote`, un linaje largo de trades ni latencia del proveedor; esas
mediciones siguen abiertas.

Evidencia cargada v103:
`~/.local/state/orbis/verification/v103-observed-ui-r3/verification.json`.
En ese corte la clave duraba quince minutos. v104 añade plazo explícito hasta
30 días: 392 casos nuevos/repetidos de 19 archivos y cuatro recorridos afectados
aprobados, con colección final exacta (4.399 casos, 4.007 retenidos). Fuentes
ajenas al alcance siguen idénticas. Evidencia cargada:
`~/.local/state/orbis/verification/v104-paper-duration-r1/verification.json`.
No se afirma observación real durante treinta días ni servicio 24/7.
GAP/reconciliación y autoridad REAL continúan pendientes.
