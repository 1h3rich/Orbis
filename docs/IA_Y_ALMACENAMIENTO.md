# Datos locales e IA en Orbis

## Estado actual

| Entorno | Almacén predeterminado | Protección | Contenido |
| --- | --- | --- | --- |
| PAPER y legado | `./orbis.db`, relativo al directorio del proceso | El lanzador crea perfiles nuevos en SQLCipher; bases heredadas en claro requieren migración offline explícita; una ruta personalizada nueva sin exigencia de cifrado aún puede crear SQLite en claro | Operaciones ficticias, estrategias y estado heredado |
| REAL | `~/.local/share/orbis/real-vault.db` | SQLCipher con contraseña | Libro patrimonial manual y cuentas; copias cifradas en `backups/` |
| LAB | `~/.local/share/orbis/lab.db` | SQLCipher al crear una base nueva en Ajustes; SQLite heredado requiere migración offline | Velas, fuente, huella, versiones, eventos y backtests; copias nuevas cifradas en `lab-backups/` |
| DCA histórico/PAPER | `~/.local/share/orbis/automation.db` | SQLCipher al crear una base nueva en Ajustes; SQLite heredado requiere migración offline | Reglas versionadas, comparaciones y sesiones PAPER DCA; copia cifrada en `automation-backups/` |
| GRVT PAPER | `~/.local/share/orbis/paper-perp.db` | SQLCipher al crear una base nueva en Ajustes; SQLite heredado requiere migración offline | Cuentas, posiciones, señales y financiación ficticia; copia cifrada en `paper-perp-backups/` |
| Spot PAPER | `~/.local/share/orbis/spot-strategy-paper.db` | SQLCipher al crear una base nueva en Ajustes; SQLite heredado requiere migración offline | Sesiones y operaciones ficticias MEXC Spot; copia cifrada en `spot-paper-backups/` |
| Preferencias de interfaz | `localStorage` del navegador | Sin cifrar | Tema, diseños, watchlist y preferencias; nunca contraseña REAL |

Las sesiones privadas MEXC y GRVT viven sólo en la memoria del proceso local
mientras la bóveda REAL está desbloqueada. No son parte de ninguna base ni
copia. Los saldos observados siguen separados del libro salvo si el usuario
confirma expresamente un inventario inicial MEXC en una cuenta REAL vacía y
declara el coste de cada activo. Ese registro no prueba adquisiciones previas,
coste fiscal ni conciliación con el exchange.

Las variables `ORBIS_DATABASE_URL`, `ORBIS_REAL_VAULT_PATH`, `ORBIS_LAB_PATH`,
`ORBIS_AUTOMATION_PATH`, `ORBIS_PAPER_PERP_PATH` y `ORBIS_SPOT_STRATEGY_PATH`
pueden cambiar esas rutas. `.env.example` muestra
rutas **de ejemplo**, no es cargado automáticamente por el código. Una base
LAB, DCA, GRVT PAPER y Spot PAPER nuevos se crean expresamente desde Ajustes
con contraseña; una consulta o un arranque vacío no los inicializa. La bóveda
REAL tiene su propio alta cifrada. No hay dominio ni servidor remoto
de ORBIS. La consulta IA optativa de LAB contacta con OpenAI sólo tras
confirmación y con la clave aportada para esa petición. Las consultas de precios públicos sí contactan con el
proveedor seleccionado; no deben incluir el patrimonio ni direcciones.

La copia SQLCipher opcional de `orbis.db` se abre mediante el lanzador local,
que solicita la contraseña al inicio y la entrega por un descriptor privado.
Este almacén cifrado se bloquea desde Ajustes o tras 15 minutos. El lanzador
crea la base predeterminada cifrada cuando falta. Un arranque directo con la
ruta predeterminada ausente falla sin crearla; una ruta personalizada nueva
requiere `--require-encrypted-paper` para exigir el mismo comportamiento.
La `orbis.db` previa y sus copias antiguas no se borran.
Si el PAPER manual activo sigue en claro, «Ajustes → Privacidad y seguridad →
PAPER manual» permite crear una **copia SQLCipher verificada** con una clave
nueva. La respuesta indica la ruta y la huella; la base activa sigue en claro.
La clave no se guarda ni se incluye en la respuesta. La copia no reemplaza la
migración y debe conservarse su contraseña para poder recuperarla.
El comando offline `scripts.encrypt_legacy_paper` puede cifrar la base activa
tras respaldo SQLCipher verificado y confirmación literal, con ORBIS cerrado.
No se ejecuta automáticamente y no elimina copias antiguas en claro.

LAB permite descargar manualmente `orbis.lab.evidence.v2` para un backtest
verificado. El archivo local incluye histórico y procedencia, versión de
estrategia, señales, etiquetas futuras y operaciones **simuladas**; separa
condiciones conocidas al cierre de resultados observados después. La
versión v2 añade segundos hasta máximos/mínimos posteriores sólo cuando hay
seguimiento completo de siete días. `?schema_version=v1` conserva la
exportación anterior para consumidores existentes; los paquetes v1 ya
descargados no se modifican. La
exportación rechaza huellas alteradas y no accede a REAL/PAPER. No entrena un
modelo ni sube datos a ningún servicio. Si un CSV propio contiene información
privada, el usuario debe revisar el archivo antes de compartirlo.
Los extremos a siete días se dejan nulos cuando no existe seguimiento completo
y cada señal indica explícitamente su cobertura temporal.

La exportación opcional `?schema_version=v3` (selector «70/30 · IA» en LAB)
mantiene el histórico y las señales de v2, pero añade una partición cronológica
fija 70/30. Marca `development`, `validation`, los casos incompletos y las
señales de desarrollo cuyas etiquetas a siete días alcanzan el corte como
`purged_boundary`. Los índices recomendados de desarrollo/validación incluyen
sólo seguimiento completo; los casos excluidos siguen visibles para auditoría.
También ofrece índices de un subconjunto cronológico sin ventanas futuras
solapadas en cada fase; son una ayuda para entrenar/evaluar, no pruebas de
independencia estadística entre eventos de mercado.
El corte se calcula sobre velas, no sobre número de señales. Esta preparación
local no entrena una IA ni sella una reserva independiente: si se eligió la
estrategia después de inspeccionar todo el histórico, la parte de validación
ya está contaminada por esa selección.

### Conjunto local de consultas

En **Laboratorio → Estructuras → Combinar consultas**, selecciona entre dos y ocho consultas y el primer día de la reserva en UTC. El archivo JSON separa `development` de `validation`. Cada muestra conserva los datos disponibles al cierre de la señal y, por separado, la etiqueta posterior a siete días. El desarrollo excluye las etiquetas que cierran en la fecha de corte o después. Los casos con seguimiento incompleto se cuentan como excluidos.

Se fusionan casos idénticos de la misma serie, regla y vela. Cada muestra fusionada conserva los IDs de todas sus consultas. Si esas consultas discrepan en los datos del caso, se rechaza el conjunto. Se conservan las huellas de reglas, resultados, históricos y procedencias. El nombre libre de la fuente se sustituye por su huella. La huella `content_sha256` cubre el contenido restante del JSON.

El límite es de 40.000 casos de entrada. El conjunto exige muestras completas a ambos lados. La fecha de corte es común a todos los mercados y temporalidades. Las ventanas dentro de cada fase pueden solaparse, y si se eligieron las reglas mirando toda la historia, la reserva refleja esa selección. La exportación se genera en el equipo y no contacta servicios de IA.

LAB v8 también conserva las revisiones de huecos PAPER con velas públicas,
versión LAB y señales etiquetadas como reconstruidas. No incluyen saldo,
posición ni operación ficticia retrospectiva. Se recuperan y verifican por ID
en la API local; siguen separadas del diario de observaciones PAPER.

La investigación LAB puede derivar un informe de regímenes de volatilidad
sin guardar otra copia de los casos: calcula la desviación de retornos
anteriores a cada señal sobre el dataset ya archivado y verificado, y luego
agrupa las etiquetas posteriores. El umbral es configurable y no se ajusta
automáticamente con resultados futuros. Los casos con huecos o calentamiento
insuficiente quedan sin clasificar. La salida es local y descriptiva; no se
envía a la IA ni constituye un dataset de entrenamiento independiente.
El informe distingue además todos los casos de un subconjunto sin ventanas
futuras solapadas por horizonte, seleccionado cronológicamente entre todos
los regímenes. Ese filtro reduce dependencia mecánica, pero tampoco demuestra
independencia estadística ni validez fuera de muestra.
La vista puede separar desarrollo y reserva cronológica 60/40, 70/30 u
80/20. Para cada horizonte excluye del desarrollo las etiquetas que tocan o
cruzan el inicio de reserva; la reserva empieza en una vela fija del histórico.
Es una comprobación exploratoria, **no una reserva sellada**: si se elige el
umbral, la ventana o la partición después de ver sus resultados, ya no sirve
como validación independiente. No se altera el run guardado ni se activan
órdenes.

En «Preferencias → Privacidad y seguridad → Copias LAB» se crea una
instantánea SQLCipher verificada. Conserva datasets, investigación,
validaciones y backtests. Las bases LAB, DCA, Spot PAPER y GRVT PAPER heredadas
en claro pueden migrarse a SQLCipher con Orbis cerrado mediante los scripts de
`README.md`; sus claves se mantienen
temporalmente en memoria al desbloquearlos desde Ajustes. Las migraciones
crean copias cifradas verificadas y rechazan journals, cambios y escritores
externos activos. La restauración también es offline y conserva una preimagen
cifrada. Esto no cifra retroactivamente copias antiguas ni bloques borrados
del SSD. No hay botón de restauración en caliente.

## Concurrencia y crecimiento

SQLite local es adecuado para el uso personal actual. Las conexiones tienen
espera de bloqueo de cinco segundos; las escrituras de versionado usan
transacciones. LAB y DCA inicializan su esquema bajo bloqueo de escritura;
un smoke test con ocho peticiones simultáneas comprobó deduplicación de un
dataset, creación de reglas y secuencia de versiones. No se ha validado aún
ingestión masiva concurrente ni un
servicio multiusuario. Antes de ello: separar un escritor por almacén, usar
cola e idempotencia para ingesta, evaluar WAL con copias consistentes y medir
latencia/tamaño. El almacén REAL cifrado requiere una prueba independiente;
no activar WAL allí sin verificar seguridad y restauración. Entre LAB y DCA
no hay transacción distribuida: cada ejecución conserva la huella del dataset
para poder detectar una discrepancia.

No hace falta comprar un dominio para persistir datos ni para integrar una API
de IA desde el proceso local. Un dominio sólo tendría sentido para acceso
remoto o sincronización, y ampliaría la superficie de seguridad.

## Capa de investigación con IA: primer corte optativo

Una investigación o backtest LAB guardado ofrece «Analizar con IA». Antes de
pedir la clave, el backend repite la verificación y muestra el JSON exacto que
se enviaría: regla/estrategia, procedencia, estadísticas, supuestos y
advertencias. El backtest sólo añade recuentos de señales y operaciones, no
sus filas. Excluye velas, casos y operaciones individuales, cartera, saldos,
wallets, órdenes reales y credenciales. La confirmación exige la huella SHA-256 de esa vista previa; si
los datos cambian, se rechaza el envío. La pregunta y la clave se introducen
para cada solicitud; ORBIS nunca guarda la clave. La respuesta sólo se guarda
si el usuario marca «Guardar respuesta en LAB cifrado» y LAB está cifrado y
desbloqueado; de otro modo permanece temporal en la vista. El archivo enlaza
pregunta, respuesta, modelo, uso declarado y evidencia exacta con una huella
de integridad. Puede consultarse por ejecución desde el panel de IA; la copia
SQLCipher de LAB lo incluye y verifica. Se puede exportar un JSON versionado
con la procedencia y borrar un registro individual con confirmación y huella
esperada. El JSON exportado queda **sin cifrar** y la interfaz lo advierte;
las copias anteriores conservan el registro borrado. No se guarda nada en LAB en claro.
En **Laboratorio → Archivo de IA** se pueden consultar juntas las respuestas
guardadas de estructuras y backtests, aunque su ejecución original ya no
se pueda reproducir. Cada registro conserva su evidencia agregada y permite
exportar un JSON individual o borrarlo con confirmación. Si LAB se bloquea
durante una consulta, la respuesta pendiente se descarta aunque se vuelva
a abrir la bóveda antes de que llegue.

La petición usa
Responses API, `store:false`, sin herramientas y un máximo de 700 tokens de
salida. Se probó con transporte HTTP simulado, no con una clave real. Puede
tener coste y `store:false` **no** garantiza ausencia de registros de seguridad
del proveedor. ORBIS rechaza direcciones completas de wallet, hashes EVM de
32 bytes con `0x` y claves con prefijo `sk-` reconocibles en la pregunta o
evidencia antes de contactar al proveedor; en la pregunta libre también
bloquea 64 dígitos hexadecimales sin prefijo. Aplica el mismo filtro al
archivo optativo de la respuesta. Las huellas SHA-256 normales del paquete
LAB siguen permitidas; los alias parciales también. Esta detección es
heurística y no cubre todos los secretos posibles: hay que revisar la vista
previa antes de consentir.

La respuesta es interpretación externa no verificada: no modifica señales,
estadísticas ni órdenes. Modelo y huella de evidencia acompañan la respuesta.
El archivo optativo no es un historial de chat ni un corpus listo para entrenar:
faltan política de retención automática, presupuesto monetario y evaluación de
calidad del modelo. El borrado individual sólo afecta a la base LAB activa:
no purga copias ya creadas ni garantiza eliminación física de bloques antiguos.

### Comparar dos backtests con IA

En Laboratorio, selecciona Base y Alternativa y pulsa «Comparar». «Analizar comparación con IA» abre la vista previa. Al enviar, ORBIS verifica las dos pruebas y la huella de la pareja. Cambiar el orden o modificar una prueba requiere otra vista previa. Si difieren el histórico o el motor, se muestra el motivo y la consulta recibe ambos resúmenes sin diferencias calculadas. Los supuestos señalan variaciones de capital, exposición, costes, stops y permanencia.

La consulta lleva estadísticas agregadas, las reglas y huellas de las dos pruebas. Los nombres libres y las filas de mercado quedan en ORBIS. La petición usa Responses con store=false, sin herramientas y con el mismo tope de salida del análisis individual. [OpenAI Docs sobre el estado de la conversación](https://developers.openai.com/api/docs/guides/conversation-state).

Con «Guardar respuesta en LAB cifrado», el archivo muestra «Comparación» y conserva ambos identificadores. El JSONL local incluye las dos pruebas y la interpretación como anotación generada por modelo. El ensayo de esta entrega utilizó un proveedor controlado.

### Ejecuciones GRVT TESTNET JSONL

El esquema `orbis.grvt.testnet.bot-research.v1` tiene una fila por ciclo con señal seleccionada. `input` contiene el instrumento, intervalo, dirección, horas de señal y selección y límites conocidos en ese momento. `outcome` contiene la evidencia de entrada, hora de cierre conciliado cuando consta y fills con precio, cantidad, comisión y marca de tiempo exacta. Los decimales y nanosegundos viajan como cadenas. `RECONCILED_UNFILLED` acredita una entrada sin ejecución y `NO_VERIFIED_ENTRY` indica evidencia insuficiente.

`provenance` conserva el ID de versión LAB y las huellas de la definición, el dataset PAPER y el episodio. Cada `row_sha256` cubre la fila sin ese campo, ordenada por clave y codificada en UTF-8 sin espacios entre separadores. La huella de descarga cubre el archivo completo con sus saltos de línea. Los resultados posteriores pertenecen a las etiquetas de investigación. El archivo representa TESTNET y se genera localmente al confirmar.

### Swaps Uniswap REAL JSONL

El esquema `orbis.uniswap.real.bot-swaps.v1` guarda una fila por swap propio confirmado del bot. El instante del recibo, las cantidades exactas y el gas en BNB quedan separados de la vela y la cotización de referencia MEXC. Cada fila conserva la versión, PAPER, el backtest LAB y la reserva del permiso original cuando está archivada. El resultado en USDT por venta se expresa antes de gas. Los swaps confirmados aún sin asiento llevan `financially_booked: false`.

Los swaps manuales y los envíos pendientes, cancelados o revertidos quedan fuera. El archivo omite direcciones, hashes de transacción, identificadores operativos y claves. El acceso requiere la bóveda REAL abierta y la revisión vigente del gráfico. Las filas y el archivo tienen huellas SHA-256. Máximo 5.000 swaps y 4 MiB por descarga local en claro.

### Fills GRVT MAINNET JSONL

El esquema `orbis.grvt.real.fills.v1` contiene una fila por fill propio de una operación conciliada. Guarda precio, cantidad, nominal, comisión y nanosegundos como cadenas. Distingue entradas, protecciones y reducciones. Las reducciones con decisión archivada incluyen el índice de objetivo y el motivo. Cada orden tiene un ordinal dentro del archivo.

La procedencia distingue operación manual, bot de estrategia PAPER y PAPER observado. Para los bots conserva referencias LAB/PAPER archivadas, incluida la reserva cuando existe. No incluye claves, subcuentas, IDs de orden, saldos ni el resultado total del grupo. La fila y el archivo tienen huellas SHA-256. La lectura requiere la bóveda REAL desbloqueada y la misma revisión que el gráfico. Máximo 5.000 fills y 4 MiB por descarga local en claro.

### Fills MEXC REAL JSONL

El esquema `orbis.mexc.real.bot-fills.v1` contiene una fila por fill propio, final, conciliado y asentado. `provenance` conserva los identificadores locales de versión y sesión PAPER, las huellas de definición y dataset y la reserva LAB cuando está archivada. Cada fila indica el índice de la autorización original, incluso si después se rearmó el bot. Los bots antiguos sin reserva usan `reserve: null`. La operación y el fill se numeran dentro del archivo.

`fill` incluye hora en milisegundos, motivo, fechas de señal y decisión, lado, cantidad, precio, nominal y comisión. Los importes viajan como cadenas decimales. `row_sha256` cubre la fila sin ese campo. El nombre refleja la huella del archivo completo. Se omiten cuentas, credenciales, direcciones, IDs de orden, saldos, movimientos y ventas manuales atribuidas. El archivo sirve para estudiar ejecuciones y deslizamiento si se dispone del precio de referencia. Para estudiar señales sin fills o reconstruir el resultado de cartera hay que recurrir al diario correspondiente. Máximo 5.000 fills y 4 MiB por archivo.

### Diario Spot PAPER JSONL

En **Spot PAPER → Exportar JSONL**, confirma la descarga local en claro. El
esquema `orbis.paper.spot.research.v1` empieza con una cabecera de procedencia
que conserva el mercado, motor, versión, definición y velas públicas de la
semilla. El estado final de la sesión en la cabecera sirve como metadato del
archivo. El conjunto representa dinero ficticio.

Cada fila posterior conserva una observación o evento del diario.
`input` contiene la vela y las señales cuando existen, la cotización archivada
y si había posición al comenzar el evento. `outcome` conserva la decisión de
entrada y la acción simulada. Una venta comparte el número de episodio con
su compra. El P/L posterior pertenece a la venta y se mantiene fuera de la
entrada. Las observaciones sin señal permanecen en el conjunto. Un hueco de
riesgo conserva su intervalo y episodio sin fabricar velas ni operaciones.

La verificación reproduce las señales y coteja el libro, precios y comisiones
con las cotizaciones guardadas. Se exportan sólo campos definidos por el
formato. Los decimales y las marcas de tiempo en nanosegundos viajan como
cadenas. Cada fila y el archivo tienen huellas SHA-256. El límite es de
10.000 eventos y 12 MiB. La exportación requiere acceso a LAB y Spot PAPER,
permanece local y carece de autoridad para operar.

### Exportación de anotaciones JSONL

En **Laboratorio → Archivo de IA**, marca los análisis que quieres reunir y pulsa «Exportar JSONL». La selección permanece al paginar y se limpia al cambiar el filtro o cerrar el panel. El archivo queda en claro después de la confirmación. La selección admite hasta 50 análisis y 4 MiB por archivo. La API sólo lee LAB cifrado y desbloqueado. Si falta una fila, cambió su huella o se detecta un identificador sensible, se rechaza el lote completo.

Cada línea `orbis.lab.ai-corpus.v1` contiene `input`, con pregunta y evidencia, `annotation`, con texto, modelo y uso, y `provenance`, con la ejecución, el análisis y las huellas originales. `row_sha256` se calcula sobre el JSON canónico UTF-8 de esa línea sin el propio campo. Los primeros doce dígitos de la huella del archivo aparecen en su nombre. La evidencia puede incluir resultados posteriores de la investigación. Las respuestas son anotaciones del modelo pendientes de revisión humana. El archivo se destina a preparar ejemplos de interpretación histórica.

## Siguientes pasos

1. Empezar con herramientas **de lectura** sobre estadísticas calculadas por
   Orbis. La IA puede explicar resultados y proponer hipótesis; no calcula el
   libro contable ni emite órdenes. Cada respuesta debe enlazar los IDs de
   dataset, versión y ejecución, tamaño de muestra y límites de los datos.
2. Una instrucción de rol como «inversor profesional» no convierte la salida
   en una señal fiable. El motor determinista calcula indicadores y métricas;
   la IA interpreta y plantea pruebas falsables. Nada pasa automáticamente de
   LAB a PAPER o REAL.
3. Para uso sostenido, evaluar credencial en almacén seguro, límites de gasto
   verificables y registro local de consentimiento. Nunca enviar contraseñas,
   direcciones, identificadores de cartera ni importes REAL por defecto.
4. Conservar primero los **datos fuente y etiquetas reproducibles**:
   proveedor, hora UTC, cobertura, SHA-256, versión de estrategia y motor,
   señal, decisión, ejecución, costes y resultados posteriores. Separar datos
   públicos, simulados y patrimonio real. Un almacén analítico por archivos
   columnares sólo se justificará cuando volumen y consultas lo exijan.
5. Para un futuro modelo propio: construir dataset desidentificado, dividir
   entrenamiento/validación por tiempo y mercado, evitar información futura
   en las variables de entrada y comparar contra baselines simples. Guardar
   procedencia, licencias y versiones; entrenar no es el primer paso.

Pendientes: contrato de más herramientas de lectura, política de retención/borrado,
ensayo de concurrencia, pruebas de filtración de datos privados más amplias y
evaluación de calidad de respuestas. Este primer corte no habilita IA sobre
PAPER/REAL ni automatizaciones.
