# Orbis

La v128 conecta las salidas parciales de GRVT LIMIT con la activación, reanudación y el gráfico REAL. [Uso y alcance](docs/GRVT_PERP_PARTIAL_REAL.md).

La v127 lleva las salidas parciales de derivados a PAPER observado. [Uso y alcance](docs/PERP_LIMIT_PAPER.md#salidas-parciales).

La v126 añade salidas parciales de derivados en LAB. [Uso y alcance](docs/DERIVADOS_HISTORICOS_PLAN.md#salidas-parciales-en-lab).

La v125 guarda y compara capturas fechadas de reglas de GRVT en LAB. [Uso y alcance](docs/DERIVADOS_HISTORICOS_PLAN.md#historial-de-reglas-observadas).

La v124 recupera recibos de swaps, operadoras y despliegues DEX por nonce. [Uso y alcance](docs/DEX_CALL_RESOLUTION_PLAN.md#búsqueda-por-nonce).

La v123 busca en BNB Chain los recibos de financiación y devolución sin pedir el hash. [Uso y alcance](docs/DEX_NATIVE_RESOLUTION.md#búsqueda-por-nonce).

La v122 recupera órdenes LIMIT de GRVT desde su historial cuando la búsqueda directa falla. [Uso y alcance](docs/PERP_LIMIT_REAL_CORE.md).

La v121 añade el bot LIMIT en GRVT REAL, su recuperación y sus fills en el gráfico. [Uso y alcance](docs/PERP_LIMIT_REAL_CORE.md).

La v120 añade órdenes LIMIT en PAPER observado. [Uso y alcance](docs/PERP_LIMIT_PAPER.md).

La v119 añade la validación de perfiles LIMIT. [Uso y alcance](docs/PERP_LIMIT_VALIDATION.md).

La v118 añade entradas limitadas y vencimiento en las pruebas LONG/SHORT de LAB. [Uso y alcance](docs/PERP_LIMIT_ENTRIES.md).

La v117 mejora la consulta de cartera y la revisión de recibos acumulados. [Decisiones y mediciones](docs/ECONOMIC_READ_SNAPSHOT.md).

La v116 corrige la siguiente orden manual después de conciliar un recibo externo y mejora su recuperación. [Continuidad de órdenes y lecturas verificadas](docs/ECONOMIC_READ_SNAPSHOT.md).

Orbis es una aplicación local de análisis, estrategias y gestión patrimonial.
Utiliza Python/FastAPI, una interfaz web de escritorio y almacenes locales
separados. LAB, PAPER y REAL conservan capital, operaciones y resultados propios.

- **Laboratorio:** históricos conservados, estrategias LONG/SHORT versionadas,
  backtesting reproducible, operaciones, eventos y estadísticas.
- **Paper:** seguimiento local de estrategias Spot, DCA y señales de derivados
  con fondos ficticios, diario y observación de interrupciones.
- **Patrimonio:** libro manual REAL cifrado con SQLCipher, compras, ventas,
  aportaciones, retiradas, transferencias internas, comisiones y CSV.
- **MEXC Spot:** órdenes manuales y bots nativos LONG/DCA con límites, permiso
  temporal y parada. Flujo USDT en BNB Chain entre wallet y exchange, revisión
  de ingreso/retirada y registro explícito sin duplicar aportaciones.
- **GRVT:** observación View, pruebas PAPER y recorridos de órdenes y bots
  LONG/SHORT en TESTNET/MAINNET con autorización, controles y recuperación. En MAINNET, View puede recuperar
  [un cierre IOC registrado](docs/GRVT_BOT_MAINNET.md#consultar-una-operación-con-view). La consulta usa el historial de GRVT si la búsqueda directa
  devuelve «orden no encontrada».
- **Wallet BNB:** swap manual USDT Binance-Peg ↔ WBNB y ejecutor acotado para
  bots LONG/DCA. La interfaz incluye despliegue y permisos confirmados en wallet,
  operadora dedicada, financiación de gas, activación, parada, rearme y devolución
  de gas. La wallet propietaria conserva los activos; su clave no pasa por ORBIS.

Las funciones REAL pueden mover fondos al habilitarlas. Los recorridos se
verifican con exchanges y wallets controlados y EVM propia; **el ensayo humano
mainnet sigue pendiente**. No se ha desplegado el ejecutor DEX usando fondos del
usuario. No hay aplicación móvil nativa ni servicio garantizado 24/7.

Las credenciales de exchange y la clave operadora conectada viven en memoria;
la operadora dispone de copia cifrada verificable. Reiniciar no reconecta claves
ni autoriza operaciones automáticamente. El libro REAL y las observaciones del
exchange tienen funciones distintas: consultar saldos no crea asientos ni concede
permiso para operar. Los ingresos, retiradas y recibos requieren comprobación y
registro explícitos.

La revisión **v128 está cargada y verificada en el servidor local**. La revisión
contable DEX permite comprobar bloques, reutilizar asientos manuales y registrar
recibos y gas conjuntamente, con aceptación explícita y recuperación de respuestas
perdidas. El control optativo «Gas» incorpora gastos ya registrados; no crea
compras ficticias ni concede permisos de trading. Los movimientos simultáneos
permiten fijar el orden manual sin invertir recibos de la red. La reapertura
recupera el criterio vigente y una revisión antigua se rechaza si cambió el
libro o la selección. [Asistente de orden](docs/ECONOMIC_ORDER_ASSISTANT.md).
[Uso y límites](docs/DEX_GAS_ORDER_EXTENSION.md). La revisión reúne varias wallets
mediante proveedor y cuenta elegidos explícitamente, con pruebas de control
separadas y recibos vinculados a su cartera. Cambiar cuenta/red descarta la
revisión; no concede permiso de gasto. [Guía](docs/ECONOMIC_MULTI_WALLET_REVIEW.md). PAPER
manual permite crear, desbloquear y proteger su archivo desde Ajustes. El
lanzador pausa, verifica copia y contenido, y vuelve al mismo puerto bloqueado;
una respuesta perdida se recupera por ID sin repetir la protección.
[Uso](docs/MANUAL_PAPER_GUI_USAGE.md). El
Laboratorio conserva datos TRADE/MARK, financiación, perfiles y pruebas de
margen aislado/cross con apalancamiento, costes y liquidación. La validación
compara reservas cronológicas, ventanas sucesivas y recorridos OHLC.

Una reserva completa sin exposición permite **Probar en PAPER → Activar
ficticio**. El mismo libro Decimal procesa cotizaciones públicas, financiación
y velas nuevas; el histórico inicial sólo prepara indicadores. LAB y
**Perpetuos PAPER → Estrategias validadas** comparten biblioteca, controles,
gráfico con entradas/salidas, patrimonio, diario y exportación verificable.
Reabrir recupera registros sin reactivar bots. [Guía](docs/PERP_OBSERVED_USAGE.md).

El modelo sigue siendo **experimental**: reglas actuales y trayectorias OHLC
hipotéticas, sin prueba de fills históricos ni liquidez. Las referencias tienen
autoridad NONE. Una posición interrumpida permite **Estimar cierre**, aceptar
el hueco y guardar un resultado separado con precio actual/funding verificable,
marca violeta, diario y exportación. No reconstruye stops entre puntos ni
modifica la cuenta observada original. [Uso](docs/PERP_GAP_CLOSE_USAGE.md).
**Patrimonio → GRVT → Revisar PAPER → REAL** reproduce los originales LAB/PAPER
y compara margen y apalancamiento con dos lecturas privadas del instrumento.
Detecta diferencias, cambios de fuente y revocaciones; excluye cierres estimados
y mantiene autoridad NONE. En **Bot · MAINNET → Perpetuo · margen** puedes
elegir la prueba, revisar un presupuesto REAL separado y autorizar el bot
dentro de límites. Su fuente independiente espera una señal nueva; vigila la
posición efectiva y cierra con fills y conciliación propios. El gráfico muestra
las ejecuciones confirmadas. Una respuesta perdida se consulta por ID sin
repetir la activación; reabrir no reactiva el trabajo.
[Recorrido y límites](docs/GRVT_PERP_BOT_ACTIVATION.md). Requiere saldo ya
financiado en GRVT, una subcuenta dedicada y configuración compatible.
La carga inicial de estado MEXC/GRVT ya no puede ocultar una conexión posterior
al llegar tarde. En Ajustes, LAB, DCA, Spot y GRVT PAPER permiten elegir
plazo de quince minutos hasta treinta días, fijo y sólo en memoria. Consultar
no lo renueva; bloquear cancela también un desbloqueo pendiente. DCA necesita
LAB y DCA disponibles al comprar. Las sesiones ACTIVE de Spot/DCA conservan su
política de seguimiento; el nuevo motor observado no reanuda al reabrir.
[Guía de plazos](docs/LOCAL_OBSERVATION_SESSIONS.md). No garantiza servicio 24/7.

El panel **Recibos externos** permite comparar y registrar recibos de una o varias wallets. Los registros guardados se pueden recuperar al reabrir la bóveda. [Guía de uso](docs/EXTERNAL_RECOVERY_UI.md).

La tanda general de 5.427 casos pasa 5.426 y detecta una expectativa antigua en la lista de capacidades. Se corrige sólo esa expectativa y pasan los 14 casos del módulo. La cobertura final coteja las 5.427 identidades en 454 archivos. Pasan también 13 recorridos de navegador sobre las mismas fuentes de aplicación. El esquema REAL preparado es 79. Evidencia en `~/.local/state/orbis/verification/v115-external-recovery-r1/verification.json`.

En v114 pasan **5.208 casos / 435 archivos**, sin fallos, errores ni omitidos, con
identidades cotejadas. La tanda inicial detecta un montaje Git dependiente
del checkout; corregido sólo ese test, se repiten los 18 casos de su módulo,
con la aplicación intacta y el fallo original conservado. No se presenta como
otra tanda completa con cuatro salidas 0. Pasan también
**27 recorridos de navegador sobre la misma aplicación**: primer uso,
LAB/PAPER, cartera/DCA, MEXC, GRVT, wallet y DEX. Contratos: 26 conservados con
fuentes y ambos artefactos cotejados. El esquema REAL preparado es 75; sus
migraciones se prueban sólo en archivos propios. La carga mantiene cerrada la
bóveda del propietario y conserva los seis archivos de datos sin cambios.
Se comprueban 51 recursos de interfaz y 58 rutas privadas rechazando acceso
anónimo; escritorio/móvil sin errores JS ni solicitudes de escritura durante
la comprobación pasiva. Evidencia: `~/.local/state/orbis/verification/v114-economic-multi-wallet-r1/`.
No hay nuevas migraciones ni cambios en los ocho motores/puertos financieros.
En v115, la revisión de efectos externos cubre las fuentes DEX publicadas. Siguen pendientes otros adaptadores de nonce, el rendimiento de catálogos grandes y la aceptación global. Las
pruebas propias no sustituyen el piloto humano mainnet.

Catálogo, reglas y ticker públicos GRVT se han contrastado con el núcleo nuevo
y capital sintético. Esa comprobación no consulta una cuenta privada ni prueba
ejecución. Las pruebas de órdenes/fills/costes utilizan proveedores controlados.

La prueba de carga propia de v106 conserva 100.000 velas, una posición, ocho eventos
originales y 721 registros de financiación para un hueco simulado de treinta
días. Cierre 8,47 s y reproducción 4,06 s bajo carga concurrente; patrimonio
coincidente con cálculo independiente. No acredita treinta días de observación
real, un linaje largo de operaciones, latencia del proveedor ni pintura UI.

El LAB legado local continúa sin cifrar, igual que los almacenes legados DCA,
Spot PAPER y GRVT PAPER. **Guardar el nuevo archivo requiere LAB cifrado**;
**Ajustes → Privacidad y seguridad → Proteger legado** permite elegir LAB,
DCA, Spot PAPER y GRVT PAPER, introducir una contraseña y proteger sus archivos
con copias cifradas verificadas. El lanzador pausa y reabre el mismo servicio;
los almacenes protegidos quedan bloqueados. REAL está excluido; PAPER manual
tiene una protección independiente en su propio panel de Ajustes. No se cifra ningún archivo del
propietario automáticamente. `unlocked: false` en un almacén plano no significa acceso bloqueado
ni cifrado. No se ha abierto ni migrado la bóveda REAL del propietario.
Quedan pendientes la medición de continuidad prolongada,
órdenes/posiciones avanzadas y reglas históricas acreditadas, además del piloto
humano mainnet del flujo REAL nuevo.
[Arquitectura y siguiente tramo](docs/DERIVADOS_HISTORICOS_PLAN.md).
La [aceptación funcional](docs/ACEPTACION_FUNCIONAL.md) recoge la cobertura y las
brechas del objetivo completo, incluidas funciones parciales y el piloto mainnet.
La evidencia activa está indexada en `~/.local/state/orbis/verification/latest.json`.
El [registro de progreso](docs/PROGRESO_V0.2.md) conserva los cortes anteriores y
sus límites; las descripciones de versiones antiguas son históricas.

Guías: [intercambio manual BNB](docs/WALLET_SWAP_BNB.md),
[bot MEXC](docs/BOT_MEXC_SPOT.md), [bot GRVT mainnet](docs/GRVT_BOT_MAINNET.md),
[ejecutor y bot DEX](docs/DEX_BOT_BNB.md),
[resolución de llamadas](docs/DEX_CALL_RESOLUTION_PLAN.md) y
[ejecución real](docs/EJECUCION_REAL.md).

## Dirección del proyecto

La verificación repetible está en `scripts/verify_orbis.py`. Usa `--run` con
un directorio persistente privado y `--phase manifest`, seguido de `contracts`,
`targeted`, `full` y `browser`. Conserva XML, logs e identidad de fuentes/casos;
las pruebas utilizan bóvedas y proveedores controlados. No reinicia el servidor
ni desbloquea la bóveda del usuario.

La [especificación funcional y técnica v0.2](docs/ORBIS_ESPECIFICACION_V0.2.md)
es el documento maestro del producto objetivo: Laboratorio, Paper Trading y
Cartera Real manual como entornos separados. El
[plan de transición](docs/PLAN_TRANSICION_V0.2.md) compara esa visión con este
repositorio y establece hitos verificables. Los documentos anteriores de
`Qué_es/` se conservan como antecedentes; las propuestas tecnológicas de la
v0.2 siguen pendientes de decisión.
La [arquitectura de fase 1](docs/ARQUITECTURA_V0.2.md) recoge las decisiones
actuales y el [registro de progreso](docs/PROGRESO_V0.2.md) distingue lo
completado de lo pendiente.

## Desarrollo local

Para usar ORBIS sin recarga de desarrollo, desde cualquier directorio:

```bash
/ruta/a/Orbis/.venv/bin/python /ruta/a/Orbis/scripts/run_orbis_local.py
```

El lanzador fija la raíz del proyecto para no crear otra base PAPER legado por
error, escucha sólo en `127.0.0.1:8000` y abre `/ui/` cuando responde.
`--port 0` elige un puerto local libre; `--no-browser` deja la URL en pantalla
sin abrirla. Cerrar el proceso detiene ORBIS y sus observadores locales; no
se inician órdenes reales ni se reconectan credenciales automáticamente.

El entorno actual se ha probado con Python 3.14 y las dependencias de
`requirements.txt`. Desde la raíz del repositorio:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Uvicorn escucha en `127.0.0.1` por defecto: mantén el bind en loopback con
`--host 127.0.0.1` y nunca uses `--host 0.0.0.0` (esta API v1 no tiene
autenticación administrativa, RNF-08 pendiente). `/ui` es solo loopback:
`10.0.2.2` es solo el alias del host para el emulador de
Android (campo «API base» de la UI o `window.ORBIS_API_BASE`), no una
exposición en LAN.
La API HTTP y WebSocket rechaza clientes de red no locales aunque el proceso
se haya enlazado accidentalmente a otra interfaz; también exige host local y
origen coincidente cuando el navegador envía `Origin`. `10.0.2.2` sólo se
admite como alias de petición si la conexión llega localmente; no habilita
acceso LAN ni elimina la restricción adicional de `/real`.

La API y su documentación interactiva quedan en
`http://127.0.0.1:8000` y `http://127.0.0.1:8000/docs`.

La interfaz avisa «Reinicio pendiente» cuando el código Python local cambió
después de arrancar el servidor. No reinicia automáticamente: cierra ORBIS
cuando no haya operaciones en curso y vuelve a abrirlo con las contraseñas
de los almacenes cifrados que correspondan. «Servidor sin verificar» indica
que el proceso abierto es anterior a esta comprobación o apunta a otro servidor.

En Patrimonio, una copia REAL gestionada puede comprobarse antes de restaurarla.
Orbis verifica una instantánea temporal cifrada con la clave de la bóveda
desbloqueada y deja intactos tanto el archivo original como el libro activo.
El resultado sólo vale para esa lectura: la restauración revalida la copia y
no se presenta como autenticación criptográfica frente a un atacante local.

## Dashboard `/ui`

Un dashboard servido en `http://127.0.0.1:8000/ui` (mismo origen, sin
framework, sin npm ni CDN): operativa PAPER (abrir escenario, comprar, vender),
cartera PAPER valorada, estrategias, ledger y Cartera Real manual. En
«Patrimonio» abre el registro REAL manual; el saldo ficticio PAPER se consulta
desde «Simulación» → «Saldo». Inicio rotula las cifras ficticias como PAPER y
las mantiene separadas del patrimonio registrado. La cabecera conserva la fuente de precios sin
repetir un selector de entorno. No hay un control activo de trading real:
registrar una compra no envía órdenes.
«Laboratorio» usa capital ficticio y nunca modifica PAPER ni REAL.
Sirve siempre
en `127.0.0.1`; no se expone fuera de la máquina.

La primera apertura de «Cartera real» crea una bóveda en
`~/.local/share/orbis/real-vault.db` con contraseña principal de al menos 12
caracteres. `ORBIS_REAL_VAULT_PATH` cambia esta ruta antes de iniciar el
servidor. Guarda la contraseña fuera de Orbis: no hay recuperación automática.
La bóveda v17 migra versiones anteriores sobre una copia cifrada recuperable;
además protege una huella seudónima del UID MEXC usado por el transporte
interno, para impedir que otra clave reinicie límites locales sin revisión.
Las nuevas reservas internas originadas en señales MEXC Spot PAPER conservan
también su procedencia en la bóveda; no habilitan órdenes reales.
La contraseña y el token sólo permanecen en memoria; la sesión se bloquea tras
15 minutos de inactividad. «Copia cifrada» crea y comprueba una copia en el
subdirectorio `backups/` junto a la bóveda. «Restaurar desde una copia cifrada»
requiere elegir una copia gestionada y escribir `RESTAURAR`; valida el archivo,
conserva el estado anterior cifrado como `pre-restore-*.db`, lo reemplaza y
bloquea la sesión. Sólo admite copias de la misma contraseña y versión de
esquema. No introduzcas direcciones completas como alias.

Las copias de Laboratorio son distintas: «Preferencias → Privacidad y
seguridad → Copias LAB» crea un SQLCipher completo, cifrado con una contraseña
independiente y verificado en `lab-backups/` junto a `lab.db`. Una base LAB
nueva se crea cifrada desde «Preferencias → Privacidad y seguridad → LAB activo»;
las rutas de escritura no la inicializan automáticamente. Una base LAB antigua
sin cifrar sigue funcionando para no alterar su historial: requiere migración
offline si quieres protegerla. Las copias antiguas sin cifrar se señalan como
tales. La contraseña no se guarda; sin ella no se recupera la copia. La
antigua API `POST /lab/backup` ya no crea copias sin cifrar (responde 410); la
UI usa `POST /lab/backup/encrypted`. La
restauración no se hace desde la app porque PAPER puede estar leyendo LAB.
Si LAB activo ya es SQLCipher, Ajustes permite desbloquearlo durante 15
minutos y bloquearlo. Para cifrar una base LAB existente, cierra Orbis por
completo y, en Linux/POSIX, ejecuta:

```bash
python scripts/encrypt_lab.py --confirm 'CIFRAR LAB'
```

El script pide dos veces una nueva contraseña (o dos líneas con
`--passphrase-stdin`), verifica LAB y crea una copia SQLCipher recuperable
antes de sustituir la base. Rechaza journals y otros procesos SQLite. No
borra automáticamente copias antiguas sin cifrar ni garantiza borrado físico
de bloques antiguos en SSD. Las bases PAPER heredadas pueden seguir en claro;
Spot PAPER nuevo se crea cifrado y una base Spot anterior requiere migración.
En Linux, con Orbis **completamente cerrado**, usa:

```bash
python scripts/restore_lab.py --list
python scripts/restore_lab.py --inspect NOMBRE_DE_COPIA_CIFRADA
python scripts/restore_lab.py --restore NOMBRE_DE_COPIA_CIFRADA --confirm RESTAURAR
```

Si configuraste `ORBIS_LAB_PATH`, consérvalo igual para el script o pasa
`--lab-db RUTA`. El script pide la contraseña cifrada en la terminal sin
mostrarla (o puede leerla por `--passphrase-stdin`; nunca como argumento).
Si LAB activo ya está cifrado y restauras una copia, también pedirá la clave
de la base activa; con stdin, `--active-passphrase-stdin` lee esa clave en una
segunda línea después de la de la copia.
Sólo admite nombres gestionados, verifica la copia, rechaza la restauración
mientras el servidor mantenga abierta su lease y conserva el LAB anterior en
otra copia cifrada `pre-restore-lab-secure-*.db`. Las copias antiguas sin
cifrar todavía se pueden inspeccionar y restaurar, pero su preimagen también
será sin cifrar. También rechaza journals pendientes. Fuera de Linux/POSIX la
restauración automatizada aún no está disponible; las copias pueden
inspeccionarse sin modificar la base.
La cuenta seleccionada también puede exportar sus movimientos como CSV para
auditoría. Ese archivo descargado **no está cifrado** y no sustituye a una
copia de seguridad: no contiene toda la bóveda. La importación acepta sólo el
CSV REAL v1 de ORBIS; ofrece vista previa y aplica todos los movimientos
válidos de forma atómica, omitiendo duplicados exactos por `request_id`.
Dentro de la misma bóveda se pueden crear varias cuentas REAL por alias,
cada una con historial y divisa base propios; no son conexiones a wallets ni
exchanges.

Desde un móvil/emulador en la misma máquina que no resuelve `127.0.0.1`
(p. ej. Android Emulator, donde `10.0.2.2` es el host), el servidor sigue
arrancando con `--host 127.0.0.1` y la UI permite sobreescribir la base de la
API sin cambiar el bind: variable `window.ORBIS_API_BASE` o el campo
«API base» del pie (se guarda en `localStorage`), p. ej.
`http://10.0.2.2:8000`.
Las rutas `/real` se restringen al navegador y servidor locales; la opción de
API remota anterior sólo aplica a las vistas no sensibles.

Reglas de la UI (acordadas para no romper la suite):

* Los importes viajan como **cadenas**. Los saldos y resultados
  (`equity`/`cash`/`positions`/P&L) los calcula el servidor y la API los
  serializa como cadenas decimales; la UI los formatea sin convertirlos a
  `float`. Sólo los estimados previos de Operativa se calculan en el
  navegador con `decimal.js`/`BigInt`, sin sustituir la validación del motor.
* `cash_balance` se muestra como **efectivo real** (`available_balance` ignora
  ventas: es solo histórico). El P/L no se recalcula en cliente.
* Los `timestamps` naive del servidor son UTC: se añade `Z` antes de
  formatear.
* Cada envío genera un `request_id` UUIDv4 propio sobre
  `crypto.getRandomValues` (el helper nativo falla en `http://` no seguro).
* No se envía `strategy_id` en buy/sell (el esquema lo prohíbe con 422), la
  moneda siempre en mayúsculas (`^[A-Z0-9]{2,12}$`) y `POST /operations` no se
  expone; ahora rechaza nuevas filas LIVE sin cifrar. No existe una pantalla
  de trading real: las URL antiguas `#/live` vuelven a Inicio.

## Precios públicos (valoración PAPER y referencia REAL)

`ORBIS_PRICE_PROVIDER` elige el proveedor de valoración sin tocar código (`kraken` por
defecto para demo local): `kraken` (público, primario EUR, sin claves), `coingecko` (público de
recurso para BTC/ETH, con rate-limit), `manual` (tabla `price_quotes`) o
`static` (memoria, pruebas). Este ajuste no selecciona la fuente de las
órdenes PAPER nuevas: estas llevan su propio proveedor público. En la API
heredada `/paper/buy` y `/paper/sell` el cliente aún aporta `price`. En
`/paper/market/buy` y `/paper/market/sell` el servidor exige cotización
pública vigente y un fallo de red sí bloquea la nueva operación; la
valoración puede usar el último precio negociado o el coste medio, pero se
marca como estimada.
`GET /paper/portfolio` identifica en cada posición `price_source`,
`price_as_of`, `price_age_seconds` y `price_stale`. Su `valuation_status` es
`CURRENT_REFERENCE`, `ESTIMATED` o `CASH_ONLY`: un precio manual, fijo,
almacenado o de una operación anterior no se presenta como cotización actual.
La pantalla PAPER y el inicio rotulan como estimados el patrimonio y el
resultado cuando alguna posición usa esos precios. Los importes y las reglas
de P/L existentes no cambian; REAL mantiene su política más estricta de no
publicar un total cuando falta una cotización válida.

La Cartera Real usa el mismo dato público únicamente si trae fuente, fecha y
no está obsoleto. Si falta una cotización o hay efectivo negativo por una
aportación no registrada, no publica un total patrimonial para esa divisa.
Nunca recurre al coste medio como si fuese un precio de mercado actual.

## Laboratorio inicial `#/laboratorio`

Se puede cargar un CSV local con columnas `time,open,high,low,close,volume`
(`time` UTC en segundos, milisegundos o ISO) o descargar un rango cerrado de
Binance Spot, MEXC Spot o GRVT PERP. El dataset se guarda completo en `~/.local/share/orbis/lab.db`
(`ORBIS_LAB_PATH` cambia la ruta) con fuente, cobertura y SHA-256. LAB no
contiene patrimonio real. Su SQLite no se cifra por defecto, aunque puede
migrarse offline a SQLCipher; no introduzcas claves ni secretos. Las velas abiertas, incoherentes o
duplicadas se rechazan; los huecos se muestran y bloquean la ejecución.

El editor crea versiones inmutables LONG o SHORT con grupos Y/O anidados para
entrada y cierre independiente (máximo ocho elementos por grupo, cuatro
niveles y 64 nodos por estrategia), además de stop, objetivo, comisión y
slippage. La
estructura «Ruptura + retesteo» calcula un rango sobre 2–100 velas anteriores,
exige ruptura al cierre y un primer retesteo confirmado en 1–10 velas, con
tolerancia configurable; no compara simplemente dos velas parecidas. La
regla «Tendencia ↑/↓ · máximos/mínimos» exige tres máximos y tres mínimos
locales alternados, ascendentes o descendentes, dentro de una ventana. Cada
giro espera 2–10 velas cerradas a su derecha para confirmarse; sólo el giro
nuevo emite señal. LAB y Spot PAPER comparten esta semántica (`lab-1.12`).
La estructura no predice el siguiente precio ni habilita órdenes reales.
La señal usa sólo la vela cerrada; entrada y cierre por regla se simulan en la
siguiente apertura. Una vela que toca stop y objetivo se resuelve por stop.
SHORT es sintético experimental: no modela préstamo, financiación ni
liquidación. Las operaciones, eventos, curva de capital, métricas y la versión
exacta quedan guardados y pueden reabrirse. No interpretes la mejor prueba
histórica como promesa de rentabilidad futura.

«Exportar versión» descarga sólo la definición LAB seleccionada y su huella;
«Importar JSON» verifica formato, huella y reglas antes de crear una estrategia
nueva, conservando el origen declarado en LAB. No importa backtests, datasets,
cuentas ni permisos de ejecución. La huella detecta cambios accidentales, pero
no autentica quién creó el archivo: revisa las reglas antes de utilizarlo.

La exploración de parámetros ofrece «Reserva final» y «Ventanas sucesivas».
La segunda reelige stop/objetivo en cada prefijo de desarrollo y evalúa sólo
esa elección en la siguiente reserva. Guarda candidatos, operaciones y
métricas por ventana y permite verificar su reproducción tras reiniciar.
No hay una reserva final adicional intacta; los beneficios de ventanas con
capital reiniciado no forman una cartera continua, y muestras pequeñas se
advierten explícitamente.

«Comparar pruebas» exige elegir dos backtests: ORBIS verifica y repite ambos,
y sólo calcula diferencias de rentabilidad porcentual y drawdown si comparten
el mismo dataset y la misma versión de motor. Los supuestos de capital,
costes y riesgo se consultan en los encabezados. No compara beneficios netos
absolutos de históricos distintos ni sustituye una prueba fuera de muestra.

En «Spot PAPER», una sesión de estrategia LONG enlaza con el backtest de la
misma versión y el mismo dataset si existe. El panel verifica ese run antes
de mostrar sus métricas y lo separa de compras/ventas ficticias observadas;
no presenta ambos periodos como rentabilidades directamente comparables.
«PAPER DCA» hace el mismo enlace con la simulación DCA condicionada de su
versión e histórico exactos; muestra compras y capital usado en cada entorno
por separado, sin sumar ni comparar rentabilidades de periodos distintos.

La sección «Estructuras» permite investigar sin guardar antes una estrategia:
por ejemplo tres velas bajistas consecutivas con volumen creciente, o las
reglas del editor. Guarda cada aparición y las distribuciones posteriores
completas a 1 h, 4 h, 1 d y 7 d; las señala sobre el gráfico, permite
inspeccionarlas y copiarlas a un **borrador** de estrategia. No crea órdenes,
ni backtests ni resultados netos: son variaciones brutas descriptivas, con
casos que pueden solaparse. LAB guarda estas consultas en el mismo `lab.db`.

La ayuda breve de cada pantalla vive en sus controles; las explicaciones de
privacidad, seguridad y cookies están en `#/config` → «Privacidad y seguridad».
En Inicio, Operativa y Cartera PAPER, los rangos de años cargan páginas
anteriores si la fuente lo permite; si no llega a la fecha elegida, muestra
la cobertura parcial. «Todo disponible» está acotado por un máximo de 12.000
velas diarias por carga, señalado cuando se alcanza. Las páginas no mezclan
proveedores y los botones manuales vuelven a consultar el histórico.
La meta de ejecución real con wallet, MEXC, GRVT y un DEX está documentada en
[EJECUCION_REAL.md](docs/EJECUCION_REAL.md). Está implementada la
[compra/venta manual MEXC Spot](docs/MEXC_SPOT_MANUAL.md), con activación
explícita, límites, saldo de apertura revisado, petición única y asiento de
ejecuciones. Su prueba de navegador usa un exchange controlado; todavía no
se ha probado una orden con credenciales reales. El [ingreso USDT/BEP-20](docs/WALLET_FUNDING_BNB.md)
permite preparar un plan, solicitar el envío en wallet, cotejar su recibo con
el abono View MEXC y contabilizarlo explícitamente sin duplicar saldo ni crear
beneficio ficticio. La [retirada USDT/BSC](docs/MEXC_WITHDRAWAL_BNB.md) dispone de
plan, permiso separado y un POST único; recupera el envío mediante View,
comprueba recepción con el RPC de la wallet y registra neto/comisión con
confirmación, sin duplicados ni reabrir la operativa automáticamente.
Ambos recorridos están probados con proveedores controlados; el ensayo humano
mainnet sigue pendiente y el RPC observado no es una prueba independiente de consenso.
El ciclo conjunto wallet → ingreso MEXC → bot BUY/SELL → retirada a la misma
wallet está probado por API y navegador en una sola bóveda y con saldos
compartidos ficticios. El [recorrido completo](docs/BOT_MEXC_SPOT.md#wallet--bot--wallet)
separa autorización del bot, recepción del dinero y contabilidad; no promete
liquidar residuos Spot ni sustituye una prueba mainnet.
El [núcleo del bot nativo MEXC](docs/BOT_MEXC_SPOT.md) ya conecta internamente
una estrategia LAB/PAPER probada con decisiones, compra/venta y fills propios
REAL; se verifica con transporte controlado. El panel «Bots · REAL» permite
autorizarlo con límites y presupuesto, consultar su posición/diario y detener
nuevos envíos. Sesión caduca con la pantalla. El opt-in Operativo permite
proteger la vista y cerrar PAPER, manteniendo clave/credenciales sólo en memoria
hasta la caducidad de la política revisada (máximo 24 h), sin renovación.
El opt-in **Continuo** añade una fecha final explícita (hasta 30 días y nunca
posterior a los límites aprobados) y renueva evidencia diaria dentro de esa
concesión, sin ampliar fecha, presupuesto o alcance ni reiniciar el gasto DCA.
Bloquear o Detener y bloquear lo revoca; cerrar/reiniciar no reanuda bots. El
rearme revisado conserva el mismo bot y sus fills con otra apertura y política.
Las activaciones nuevas tienen seguimiento de velas propio en REAL: no esperan
que PAPER avance para decidir. Guardan semilla y observaciones encadenadas en
la bóveda cifrada v31, vinculadas a cada decisión. Esto todavía no elimina los
bloqueos/plazos de autorización ni habilita ejecución 24/7.
El [permiso operativo](docs/BOT_MEXC_SPOT.md#permiso-operativo-vista-protegida-política-con-caducidad)
es independiente del token HTTP; no es una concesión permanente ni extiende la
apertura revisada de un día. La pantalla protegida informa de que sigue operando
y permite detenerlo localmente sin mostrar importes.
La [continuidad causal](docs/MEXC_CONTINUIDAD.md) encadena aperturas, políticas
y mercado bajo la misma concesión. Está probada por API y navegador más allá
del primer día con exchange ficticio, incluida caducidad/STOP; el ensayo humano
mainnet sigue pendiente. No hay autoarranque ni garantía de ejecución 24/7.
La decisión y su reserva se guardan juntas. En el diario puedes parar y descartar
lo que nunca se envió, o seguir con View un envío incierto; ninguna recuperación
reenvía órdenes ni rearma el bot. Las decisiones antiguas sin reserva se conservan
bloqueadas hasta revisión asistida.
«Gráfico» permite inspeccionar sus fills REAL sobre velas MEXC, con tiempo,
precio, cantidad, comisión y referencia al asiento. Sólo marca ejecuciones
finales asentadas; nunca una señal PAPER o una intención como compra realizada.
Si hay órdenes inciertas o movimientos ajenos con exposición, se bloquea para
revisión; no se reasignan activos automáticamente.
«Cartera real» incluye una conexión EVM voluntaria, de lectura al conectar, para
extensiones compatibles con EIP-6963/EIP-1193: muestra red, saldo nativo y
consulta opcional de un ERC-20 por contrato en un bloque, sin incorporarlos al
libro ni habilitar gasto automáticamente. El ingreso BEP-20 requiere una
autorización y confirmación separadas. Las direcciones completas no se guardan en ORBIS,
pero la extensión/RPC las recibe para consultar los saldos. Ver
[alcance y privacidad](docs/WALLET_EVM_READONLY.md).
Una comprobación optativa cruza un recibo EVM observado por la wallet con el
`txId` de un depósito MEXC guardado; no verifica aún el importe ni el destino
on-chain salvo para transferencias nativas simples o ERC-20 singulares en
Ethereum mainnet, cuando MEXC aporta dirección sin memo. Incluso entonces no
verifica la identidad del contrato ni convierte la coincidencia en
conciliación o permiso de gasto.
Si la consulta View de MEXC devuelve una ficha ETH con contrato único y
depósitos habilitados, ORBIS coteja también ese contrato con el evento
ERC-20. Es metadato observado, no autorización ni prueba histórica definitiva.
La misma consulta indica si el ingreso tiene un traslado REAL vinculado
explícitamente y aún verificable. Un vínculo antiguo sin identidad View o
datos que hayan cambiado se señalan para revisión; ningún estado concilia la
cuenta completa ni permite operar.
Para retiros ETH/ERC20, una captura nueva puede conservar huellas HMAC del
hash de cadena y del destinatario publicados por MEXC. La wallet permite
cruzar una recepción EVM con esas huellas en solo lectura; el importe, la
comisión y el libro REAL siguen sin conciliar. Para un único movimiento puede
indicar si su valor numérico coincide con `amount` o con `amount −
transactionFee`; ambas son hipótesis, no una regla contable confirmada.
Si la ficha MEXC consultada al archivar el retiro publica un contrato ETH
único con `withdrawEnable`, se compara con el contrato del evento ERC-20.
Una coincidencia de ficha no acredita identidad histórica del token.

## Señales GRVT PAPER `#/perpetuos`

Una cuenta ficticia puede seguir una versión LAB sobre un histórico GRVT PERP
público y verificado. El observador local evalúa automáticamente cada vela
cerrada mientras ORBIS esté abierto; se conserva la señal de entrada/salida,
su explicación y si se observó de modo automático o manual. Una interrupción
que pierde la ventana queda como `GAP`, sin señales retrospectivas. La pantalla
muestra el latido del observador y permite comprobar o detener la sesión.
El modo predeterminado sólo observa; `AUTO_PAPER`, cuando se activa de forma
explícita, abre y cierra posiciones **ficticias** con límites y parada. El
ticker público vigila stop/objetivo mientras el servidor está abierto y un
observador separado asienta financiación pública verificable en PAPER. Una
desconexión deja la posición pendiente de cierre manual; no hay órdenes
protectoras ni liquidación automática del venue. La financiación asentada no
representa pagos de la cuenta real GRVT.
Al valorar manualmente una posición, ORBIS también calcula un **colchón de
mantenimiento puntual** con el mark y las reglas públicas vigentes de GRVT.
Una advertencia de colchón agotado no significa que GRVT haya liquidado: la
valoración puntual incluye únicamente financiación PAPER ya asentada, no la
todavía pendiente. El stream de riesgo de AUTO PAPER no equivale a una
reproducción del margen ni la liquidación real.

La vista «Cartera real» también puede consultar una clave GRVT mainnet View:
presenta equity, saldos agregados y posiciones de subcuentas accesibles sin
importarlos al libro. ORBIS no puede verificar por esta API el permiso View;
usa una clave creada exclusivamente con ese permiso. El cliente sólo llama a
autenticación y tres endpoints de lectura. No uses la clave compartida en el
chat como mecanismo de conexión; introdúcela en el formulario local y rótala
si ya quedó expuesta.

La [operativa protegida GRVT MAINNET](docs/GRVT_MAINNET_PROTEGIDO.md)
está disponible en Patrimonio → «Operar GRVT · MAINNET», separada de TESTNET.
Permite conectar firma dedicada, calcular/revisar una entrada LONG o SHORT con
SL/TP nativos, autorizar su envío único, consultar ejecuciones y cerrar el residuo
mediante IOC reduce-only acotadas. STOP retira la firma y detiene nuevos envíos;
no sustituye a «Cerrar posición». Los resultados sólo se consideran conciliados
tras cotejar contratos, fills, costes y saldo. API/UI verificadas con el HTTP
externo controlado; todavía no hay piloto Trade del proveedor.
El [bot automático MAINNET](docs/GRVT_BOT_MAINNET.md) permite elegir una sesión
AUTO PAPER activa, revisar y autorizar topes, presupuesto, pérdida realizada,
máximo de entradas y plazos. Espera señales nuevas y aplica salidas desde fills
y mercado MAINNET, con cierre y conciliación. La bóveda v57 conserva los ciclos
y decisiones reproducibles. La opción «Continuar con vista protegida» se autoriza
en esa revisión; el bloqueo completo y STOP revocan la custodia RAM. No se
restaura autoridad al reiniciar. Son órdenes reales cuando se habilitan, y la
verificación con HTTP controlado no sustituye al piloto del proveedor.
Si un cierre vence o agota intentos, «Renovar cierre» permite revisar y autorizar
otro plazo e intentos para el residuo, conservando el registro anterior.
«Límites de cierre» admite revisar expresamente otro tope de salida hasta
10.000 USDT, sin cambiar el permiso original ni elevar límites automáticamente.
Una IOC aún abierta bloquea esa renovación.
«Ver gráfico» muestra entradas/salidas desde fills verificados, con precio y
cantidad efectivos, velas GRVT y controles de intervalo/rango. Una entrada
observada no se presenta como resultado conciliado. Si un bot detenido se cierra
después manualmente, su recibo se muestra sin reactivar ni alterar el diario
original, y se identifica la intervención manual.
El [bot TESTNET](docs/GRVT_BOT_TESTNET.md) comprueba mínimos públicos antes de
guardar un permiso y vuelve a comprobarlos cuando llega la señal.

El panel «Recuperación GRVT → Enviar señal · TESTNET» permite firmar y enviar
una sola acción reciente de `AUTO_PAPER`, LONG o SHORT, con credenciales
Trade **testnet** dedicadas, tamaño/precio explícitos y tope de 100 USDT.
Los cierres son reduce-only. Carga después el diario por ID, sin reenvíos ni
contabilizar un acuse como fill. No es ejecución autónoma ni habilita mainnet;
la clave View anterior no sirve. [Contrato y uso](docs/EJECUCION_REAL.md#uso-del-envío-grvt-testnet-desde-la-interfaz).
Dos auditorías independientes pueden habilitar «Conciliar · TESTNET» si fills,
saldo y posición cuadran exactamente y no aparece actividad externa. El recibo
cifrado permite avanzar a otra señal/orden en esa subcuenta sin reenviar la
anterior ni quitar STOP. La financiación USDT vinculada a fills propios de una
reducción se contrasta con el saldo y se muestra junto al neto de la orden.
No importa resultados a cartera REAL; pagos sin vínculo y otros movimientos
no vacíos siguen requiriendo revisión. Antes de otra orden se
revisan seis historiales con un corte temporal común y se archiva su evidencia
cifrada; movimientos compensados también bloquean. No es una garantía de
atomicidad ni habilita mainnet. [Alcance de la conciliación](docs/EJECUCION_REAL.md#conciliación-acotada-y-circuito-de-entradasalida-grvt-testnet).

Las protecciones TP/SL nativas tienen un [panel manual TESTNET](docs/GRVT_PROTECCIONES_NATIVAS.md#panel-manual-y-api-local)
de compilación, firma y contraste, diario cifrado OCO, un único envío conjunto,
recuperación por ambos IDs y cancelación expresa sin reenvío tras timeout.
Dos auditorías conjuntas pueden conciliar fills, costes y posición restante;
el siguiente preflight utiliza ese recibo, no la antigua posición de entrada.
STOP conserva las protecciones existentes y nunca se borra al conciliar.
Desde «Patrimonio → Recuperación GRVT → Protecciones SL / TP» se preparan,
revisan y firman las dos salidas de una entrada propia conciliada; el registro
permite consultarlas, auditarlas, conciliarlas o cancelarlas expresamente.
La cantidad se deriva de la posición, no del capital PAPER. Faltan integración
automática del ciclo de entrada/salidas, emergencia/rearme y piloto. El envío IOC anterior no debe interpretarse
como una entrada con stop protector en GRVT.

## Histórico de patrimonio

En **Patrimonio → Evolución patrimonial**, elige el periodo, la divisa y la fuente. **Consultar** carga cierres diarios de hasta diez años y reconstruye la cartera desde el libro local. El selector **Rango** cambia la vista del gráfico con los datos cargados. **Valores diarios** permite recorrer el resultado de cien en cien.

Los días sin cierre del proveedor aparecen como huecos. Se conservan caja, aportaciones y retiradas del libro en esos días. Kraken ofrece una ventana reciente de hasta 720 registros OHLC. Para BTC/EUR se verificó una consulta larga con Bitstamp. [Kraken](https://docs.kraken.com/api-reference/market-data/get-ohlc-data) · [Bitstamp](https://www.bitstamp.net/api/#tag/Market-info).

## DCA histórico y PAPER `#/dca`

Permite guardar versiones de una regla periódica con compras adicionales por
caídas, presupuesto, límite mensual, exposición máxima y costes. Compara DCA
condicionado, DCA fijo y compra inicial sobre el mismo dataset LAB; guarda
compras y cifras exactas en `~/.local/share/orbis/automation.db` por defecto.
El informe compara rentabilidad sobre el presupuesto, drawdown y volatilidad
del patrimonio por vela (no anualizada, con el efectivo incluido); los runs
antiguos mantienen las métricas de su versión original.
El gráfico marca la decisión en el cierre conocido y la compra ficticia en la
siguiente apertura. El panel PAPER permite fijar una versión sobre un histórico
MEXC Spot diario descargado y verificado y registrar compras ficticias según
la misma regla. El primer intento es al día siguiente de activar la sesión.
El observador local comprueba automáticamente la ventana de los 15 minutos
posteriores a las 00:00 UTC mientras el servidor esté abierto; el botón
«Comprobar ahora» también permite inspeccionar manualmente. Usa la vela
anterior cerrada para
la señal y un bid/ask reciente para aproximar la ejecución y la valoración.
Si se pierde la ventana o cambia el histórico, la sesión se detiene con un
hueco: **no rellena compras retrospectivas**. Cerrar ORBIS detiene el
observador; reabrirlo después de una ventana perdida registra el hueco.
Esta pantalla no envía órdenes. Una versión DCA probada y observada en PAPER
puede autorizarse por separado en «Cartera real → Bots · REAL», sólo para compras
MEXC Spot/USDT, con asignación total, tope mensual y presupuesto por orden REAL
explícitos; no hereda efectivo ni unidades ficticias. No vende ni pone stops,
y no es un servicio 24/7. Véase [DCA REAL](docs/BOT_MEXC_SPOT.md#dca-real-sólo-compras).
### Comparar PAPER DCA con la reserva LAB

En **Fuera de muestra**, guarda una validación del histórico diario de la versión que vas a observar. En **PAPER DCA**, abre esa sesión y pulsa **Comparar reserva LAB**. El selector reúne las reservas de la misma versión y el mismo histórico. ORBIS vuelve a calcular la reserva y repite las decisiones PAPER usando las velas y cotizaciones archivadas.

La tabla separa periodos, compras, gastos, comisiones, caja y patrimonio. El patrimonio es caja más unidades por precio. LAB usa el cierre de cada vela y PAPER el bid de cada observación. Rentabilidad se calcula sobre el presupuesto inicial. Drawdown mide el retroceso máximo desde un máximo del patrimonio, contando el saldo inicial. El resultado cubre los días observados hasta la última captura. Los periodos, las cotizaciones y el modo de ejecución se muestran por separado.

### Volumen relativo en DCA

En **DCA → Condición de compra → Volumen relativo**, introduce un periodo de 2 a 120 velas y un multiplicador. Un valor de **2** exige que el volumen de la última vela cerrada alcance el doble de la media de las velas anteriores. La propia vela de señal queda fuera de esa media. La igualdad cumple la condición. Si falta historial o la media es cero, ORBIS omite la compra y conserva pendiente la aportación de ese periodo.

El filtro afecta a la compra periódica y a los extras por caída de la variante condicionada. **Comparar** conserva DCA fijo y compra inicial como referencias. El motor **dca-1.9** archiva el filtro, su versión y el resultado exacto. En PAPER se conserva además la ventana de velas cerradas. **Investigar hueco** aplica el filtro a una rama teórica, separada de las compras observadas. El filtro se usa como alternativa a SMA/EMA. Una versión probada y observada puede pasar a MEXC mediante [Preparar MEXC REAL](docs/BOT_MEXC_SPOT.md#volumen-relativo). Para BNB/USDT también se puede usar [Preparar DEX](docs/DEX_DCA_PLAN.md#volumen-relativo) con Uniswap en BNB Chain.

### Almacenamiento DCA

La base DCA nueva se crea cifrada
desde «Preferencias → Privacidad y seguridad → DCA y automatizaciones» con
una contraseña de al menos 12 caracteres. Abrir Orbis o consultar listas
vacías no la crea, y guardar una regla antes de inicializarla se rechaza.
La clave sólo vive temporalmente en memoria; conserva la contraseña, porque
sin ella no podrás recuperar los planes. Las bases DCA existentes sin cifrar
siguen funcionando para no alterar sus datos: migra una explícitamente si
necesitas protegerla.
Para cifrar una base DCA existente, **cierra Orbis**, conserva la contraseña
y, en Linux/POSIX, ejecuta:

```bash
python scripts/encrypt_automation.py --confirm 'CIFRAR DCA'
```

Pide la contraseña dos veces, verifica todas las tablas y deja una copia
SQLCipher independiente en `automation-backups/`. Ajustes permite desbloquear
la base cifrada durante 15 minutos y bloquearla; el observador PAPER reintenta
cuando está cerrada, sin inventar compras. Para volver a una copia gestionada,
con Orbis cerrado:

```bash
python scripts/restore_automation.py --restore NOMBRE_DE_COPIA --confirm 'RESTAURAR DCA'
```

La restauración guarda una preimagen cifrada. Usa la misma contraseña para
base activa y copia; no se elimina el archivo anterior de forma segura a nivel
físico en SSD. Si configuraste `ORBIS_AUTOMATION_PATH`, usa la misma ruta en
ambos scripts o pasa `--automation-db RUTA`. Nunca pases la contraseña como
argumento de consola; `--passphrase-stdin` es sólo para automatización local
controlada. La base heredada permanece sin cifrar.

Spot PAPER usa `~/.local/share/orbis/spot-strategy-paper.db`. Las sesiones,
señales y operaciones ficticias nuevas se guardan en SQLCipher: crea la base
desde «Preferencias → Privacidad y seguridad → Spot PAPER» antes de activar
una sesión. Abrir Orbis o consultar sesiones vacías no crea ya el archivo.
Las bases Spot PAPER anteriores sin cifrar permanecen legibles para no perder
historial. Para migrar una de ellas a SQLCipher, **cierra Orbis** y ejecuta:

```bash
python scripts/encrypt_spot_paper.py --confirm 'CIFRAR SPOT PAPER'
```

El script verifica las tablas y el contenido JSON, compara huellas de filas,
crea una copia cifrada en `spot-paper-backups/` y sólo entonces sustituye la
base activa. En Ajustes puedes desbloquearla durante 15 minutos o bloquearla.
Mientras está bloqueada, la API responde `423` y el observador no inventa
compras; una ventana perdida puede quedar registrada como hueco. Para
restaurar una copia gestionada, con Orbis cerrado y la base bloqueada:

```bash
python scripts/restore_spot_paper.py --restore NOMBRE_DE_COPIA --confirm 'RESTAURAR SPOT PAPER'
```

La restauración conserva una preimagen cifrada. Si cambiaste
`ORBIS_SPOT_STRATEGY_PATH`, pasa `--paper-db RUTA` a ambos scripts. La
migración no se ejecuta automáticamente ni cifra copias antiguas o bloques
físicos previos del SSD.

GRVT PAPER usa `~/.local/share/orbis/paper-perp.db`. Sus cuentas, posiciones,
señales y eventos nuevos se guardan en SQLCipher: crea la base desde
«Preferencias → Privacidad y seguridad → GRVT PAPER» antes de abrir una cuenta
ficticia. Un arranque limpio ya no crea el archivo. Las bases anteriores en
claro siguen legibles; pueden migrarse con Orbis completamente cerrado:

```bash
python scripts/encrypt_paper_perp.py --confirm 'CIFRAR GRVT PAPER'
```

El script pide la clave dos veces, verifica todas las tablas y crea una copia
cifrada en `paper-perp-backups/` antes de sustituir la base activa. Ajustes
permite elegir un plazo de quince minutos hasta treinta días, o bloquearla;
mientras está bloqueada, las
rutas PAPER responden `423` y los observadores no inventan operaciones. Una
ventana perdida al desbloquear puede dejar una sesión en `GAP`, no se recupera
como si hubiera sido observada en directo. Para restaurar una copia, con Orbis
cerrado:

```bash
python scripts/restore_paper_perp.py --restore NOMBRE_DE_COPIA --confirm 'RESTAURAR GRVT PAPER'
```

La restauración conserva la base anterior como preimagen cifrada. Si cambiaste
`ORBIS_PAPER_PERP_PATH`, pasa la misma ruta con `--paper-db`. La migración no
se ha ejecutado sobre tu base actual. Copias antiguas sin cifrar y bloques
físicos previos del SSD no quedan cifrados retroactivamente.
El [mapa de almacenamiento y propuesta de IA](docs/IA_Y_ALMACENAMIENTO.md)
explica rutas, concurrencia y límites actuales.
LAB exporta evidencia v2 por defecto y permite elegir v3 con partición
cronológica 70/30, purga de etiquetas que cruzan el corte y un subconjunto
sin ventanas futuras solapadas. Ambos archivos se
descargan localmente; v3 no entrena una IA ni demuestra validación independiente.

El Laboratorio admite condiciones de temporalidad superior derivadas del
mismo dataset desde `lab-1.13`; sólo usa velas completas y conserva la
versión anterior para reproducir pruebas antiguas. Véase el
[contrato multitemporal](docs/ESTRATEGIAS_MULTITEMPORALES.md).

* `GET /market/price?asset=BTC&quote_currency=EUR`: precio actual con edad
  (`age_seconds`), fuente y marca `stale`, según `ORBIS_PRICE_PROVIDER`:
  `kraken` usa Kraken como primario (con recurso a CoinGecko y a manual),
  `coingecko` usa CoinGecko con recurso a manual, y `manual`/`static` no
  tocan la red (la edad en fallback a manual sale de `updated_at`).
* `GET /market/history?...&timeframe=live|1h|24h|7d`: OHLC de Kraken solo si
  el proveedor configurado es `kraken` (intervalos 1|60|1440); `live` no
  tiene serie y, si no hay histórico, se devuelve una nota honesta sin
  inventar datos.
* La cabecera del portfolio muestra el precio + «hace X s», con refresco
  configurable (15s/1m/5m/off) y selector de timeframe.

El lanzador local abre la interfaz con PAPER
cerrado si falta el archivo o ya está cifrado. **Ajustes → Privacidad y seguridad
→ PAPER manual** permite crearlo cifrado, desbloquearlo y proteger un legado
plano conservando sus operaciones. Nunca crea SQLite plano en una ruta nueva.
La protección pausa el servidor, verifica una copia y el contenido, y vuelve
al mismo puerto con PAPER bloqueado. Crear una copia es una acción independiente.
[Uso y límites](docs/MANUAL_PAPER_GUI_USAGE.md).

Para perfiles nuevos, usa `.venv/bin/python -m scripts.run_orbis_local`. Si
necesitas otra ruta, exporta `ORBIS_DATABASE_URL`. Un arranque directo sin el
lanzador conserva el rechazo de una ruta ausente; `--require-encrypted-paper`
rechaza un legado plano. `--terminal-paper-unlock` mantiene la entrada oculta
de contraseña antes de servir la app; el valor se entrega por un descriptor
privado, sin URL, argumentos ni variables de entorno. `.env.example` muestra
el valor local; la aplicación no carga archivos `.env` automáticamente.

`orbis.db` heredado sigue **sin cifrar**. Para crear una copia SQLCipher sin
alterar la base activa, cierra Orbis y ejecuta desde la raíz del proyecto
`.venv/bin/python -m scripts.backup_legacy_paper --source /ruta/orbis.db`.
La contraseña se introduce de forma oculta, no como argumento. La copia se
guarda en `legacy-backups/` junto a esa base y se verifica antes de publicarla.
Esta operación no migra ni cifra `orbis.db`; tampoco elimina copias antiguas
que sigan en claro.

Si PAPER ya está cifrado, Ajustes → Privacidad y seguridad → PAPER manual
permite crear una nueva copia SQLCipher **después de desbloquearlo**. La copia
puede llevar una contraseña independiente de la base activa; Orbis comprueba
integridad, tablas, versión y huella de contenido al reabrirla. La base activa
no se sustituye ni se desbloquea de forma permanente. Conserva la contraseña
de la copia fuera de Orbis: sin ella no podrás recuperarla.

Para migrar la base PAPER heredada activa **con ORBIS cerrado**, ejecuta
`.venv/bin/python -m scripts.encrypt_legacy_paper --source /ruta/orbis.db`.
Puedes añadir `--check` para comprobar formato, bloqueo y journals sin
introducir contraseña ni modificar la base. El comando normal realiza esta
comprobación antes de pedir la clave y repite los controles durante la
migración.
Exige escribir `CIFRAR PAPER` y confirmar una contraseña nueva. Antes de
reemplazar, crea y verifica una copia SQLCipher en `legacy-backups/`; rechaza
journals, uso de ORBIS y cambios concurrentes. El archivo activo queda
cifrado en la misma ruta y el lanzador permitirá desbloquearlo desde Ajustes.
La migración no borra otras copias antiguas en claro (`.bak`, exportaciones)
ni garantiza borrado físico de bloques anteriores en SSD. Haz primero una
copia externa segura y verifica que puedes abrir la copia cifrada.

Una copia PAPER SQLCipher puede abrirse como almacén activo configurando
`ORBIS_DATABASE_URL=sqlite:////ruta/copia.db` y arrancando con
`.venv/bin/python -m scripts.run_orbis_local --require-encrypted-paper`.
Esta opción no crea SQLite en claro: un archivo ausente queda pendiente de
creación explícita desde Ajustes; un legado plano se rechaza. El archivo
cifrado existente arranca bloqueado. La opción adicional
`--terminal-paper-unlock` conserva el desbloqueo previo por terminal. Esta ruta permite trabajar
con una base cifrada, pero **no migra automáticamente** la `orbis.db` original,
no fusiona cambios entre ambas. El PAPER cifrado se bloquea desde Ajustes
o tras 15 minutos; se desbloquea allí con la contraseña. El bloqueo impide
nuevas operaciones y cierra conexiones reutilizables, aunque una petición ya
iniciada puede terminar. Mantén cerrada la app al cambiar de archivo.

Para ejecutar las pruebas: `python -m pytest -q`. Las pruebas usan una base de
datos propia en memoria y nunca escriben en `orbis.db`. No publiques esta API en
una red accesible por terceros: todavía no tiene autenticación administrativa.

`tests/conftest.py` es necesario para la suite: fija `ORBIS_DATABASE_URL` a
SQLite en memoria **antes** de importar la aplicación, crea un esquema
aislado por test e inyecta `get_db` por test. No lo borres ni lo ignores con
`git clean` (está versionado a propósito): sin él, las pruebas podrían apuntar
a `orbis.db`. No hagas `git commit` sin pedirlo antes.

Novedades UX (solo presentación, sin duplicar lógica):

* `GET /paper/operations/summary` acepta los mismos filtros que
  `GET /paper/operations` y devuelve conteos, importes, volumen y comisiones.
* `GET /paper/operations` acepta `limit` (recorta al final) y `order`
  (`asc` por defecto, `desc` invierte); por defecto no cambia nada. También
  acepta `start_at` inclusivo y `end_before` exclusivo (ISO 8601 con zona).
  Estudio consulta el par y periodo de velas cargadas sin límite arbitrario
  de 200, para no ocultar entradas recientes entre operaciones de otros pares.
* `GET /paper/portfolio` incluye `cash_weight` y `weight_percentage` por
  posición (Decimal del servidor, solo presentación).

Al arrancar, `check_schema` compara el esquema esperado por los modelos con el
esquema real de la base de datos y avisa por consola si faltan columnas o
índices. El mismo informe se puede ejecutar a mano con
`python scripts/check_schema.py` (sale con código 1 si hay diferencias), y la
suite de pruebas falla al arrancar si su base de datos no coincide con los
modelos. Esta comprobación sustituye a las migraciones mientras no se adopte
Alembic.

## Estado de los datos

`POST /paper/scenarios` crea una sesión de pruebas con capital inicial y
límites. Solo puede haber una sesión abierta por moneda cotizada; la columna
`generation` numera las sesiones sucesivas y `closed_at` registra el cierre
mediante `POST /paper/scenarios/{quote_currency}/close`. `GET /paper/scenarios`
lista las sesiones abiertas con su saldo, que se calcula como capital inicial
más ventas menos compras ejecutadas en esa sesión. `GET /paper/sessions` lista
también las cerradas.

`POST /paper/buy` y `POST /paper/sell` heredados reciben activo, moneda, importe, precio y
comisión estimada, pero obtienen presupuesto y límites de la sesión almacenada.
Guard comprueba la propuesta (precio, posición, inventario, límites y kill
switch) y el Ledger registra las operaciones aprobadas en modo `PAPER`, con
orden de venta simétrico al de compra: la venta reduce la posición manteniendo
el coste medio y, al agotarla, su coste se convierte en beneficio realizado.

Operativa y Estudio usan ahora `POST /paper/market/buy` y
`POST /paper/market/sell`: reciben par, tamaño, comisión, proveedor y UUID,
**sin precio del cliente**. El servidor consulta y valida una cotización
pública; emplea `ask` para comprar, `bid` para vender o el último precio como
aproximación visible si falta el lado del libro. Guarda la fuente, hora y
base del precio en `paper_market_evidence`. El reintento del mismo UUID
devuelve el resultado anterior sin recotizar. El contrato y sus límites están
en [Órdenes PAPER con cotización pública](docs/PAPER_PUBLIC_MARKET_EXECUTION.md).
La previsualización valida compra y venta independientemente y nunca reserva
un precio. Los estimados de la interfaz usan decimal exacto, pero la respuesta
de la orden prevalece: puede llegar una cotización nueva al enviarla.

`GET /paper/operations` lista el histórico de operaciones PAPER con filtros de
fecha, tipo y moneda; los importes se suman con decimales, sin fugas de coma
flotante. `GET /paper/portfolio` devuelve la cartera valorada: posición,
coste medio, precio de valoración (proveedor de precios, último precio
negociado o coste medio), capital inicial, efectivo, valor total, coste
invertido y P/L total, realizado y no realizado, con
`total = realizado + no realizado` y `efectivo = inicial − compras + ventas`.
`GET /paper/prices` y `POST /paper/prices` consultan y fijan precios; los
publicados se conservan en la tabla `price_quotes` con su marca de tiempo. Los
precios valoran la cartera: sólo las rutas heredadas de compra y venta siguen
exigiendo `price` en la petición; las rutas `/paper/market/*` no lo admiten.

Cada solicitud a `POST /paper/buy` o `POST /paper/sell` debe incluir un
`request_id` UUID generado por el cliente. El digest de idempotencia incluye
también `side` y `quote_currency`, de modo que reutilizar un identificador con la misma propuesta
(incluido el sentido y la moneda) devuelve el resultado ya guardado sin registrar otra
operación, incluso si Guard la había bloqueado, y reutilizarlo con otra
propuesta, con el sentido cambiado o con otra moneda devuelve `409`. La decisión y, si procede,
la operación se guardan en una sola transacción.

`GET /portfolio/buys?mode=PAPER` resume compras por
activo y moneda cotizada; no representa el valor actual ni el P/L. Las rutas
antiguas `/operations`, `/portfolio/summary` y `/portfolio/buys` sólo publican
PAPER: `mode=LIVE` devuelve `410` en las dos últimas. Las posibles filas LIVE
legadas permanecen intactas en la base anterior, sin clasificar ni migrar,
pero ya no se sirven por esta API sin bóveda.

La API conserva `POST /operations` para registrar operaciones manuales PAPER;
rechaza `mode=LIVE` para no escribir patrimonio en la base sin cifrar. Es una
excepción documentada a la idempotencia: no acepta `request_id` y cada llamada
crea una operación nueva; la idempotencia con `request_id` existe solo en el
motor PAPER (`POST /paper/buy`, `POST /paper/sell`) y este endpoint manual
preexistente queda fuera de él. La API antigua no tiene autenticación
administrativa; los backups y el desbloqueo descritos arriba son exclusivos de
la bóveda REAL. No hay migraciones automáticas. Las escrituras se
serializan en SQLite y hay pruebas de concurrencia que comprueban que dos
conexiones simultáneas no superan el saldo ni los límites y que un mismo
`request_id` concurrente solo crea una operación. Los modelos se crean al
importar la aplicación; los cambios de esquema posteriores se detectan con
`scripts/check_schema.py` y deben aplicarse con DDL explícito mientras no se
adopte Alembic.

Los requisitos funcionales están en `Qué_es/Orbis_Requisitos_Funcionales_Fase_0.docx`.
Los requisitos no funcionales derivados de los documentos del proyecto están en
`Qué_es/Orbis_Requisitos_No_Funcionales.md`.
