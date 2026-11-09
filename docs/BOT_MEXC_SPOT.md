# Bot nativo MEXC Spot: núcleo de ejecución REAL

Estado: activación explícita desde «Bots · REAL», API local y trabajador
implementados. Verificado con un exchange controlado, no con claves, cuentas
o fondos reales. Las rutas están cargadas en la entrega local verificada.
El rearme explícito conserva la posición propia con una nueva apertura y
política. Las rutas manuales no arrancan este núcleo ni convierten PAPER en REAL.
Admite LONG y DCA sólo compras, con planificadores separados y el mismo
journal, guardián, transporte y libro de fills reales.
Las activaciones nuevas observan sus propias velas MEXC en un archivo REAL;
PAPER sigue siendo la prueba/origen autorizado, no el reloj de las compras.

## Uso inicial

1. Guardar y repetir un backtest LAB y una validación fuera de muestra de esa
   versión con datos públicos MEXC. Abrir su versión en Spot PAPER con el
   backtest fijado y esperar una observación válida. Desbloquear el origen.
2. En Cartera REAL, elegir la cuenta MEXC/USDT, conectar View y verificar
   Trade de esa misma cuenta. Cuadrar el libro con el inventario y guardar
   límites para esa versión y par. No introducir secretos en el chat.
3. Abrir «Bots · REAL», elegir la prueba PAPER, su reserva y límites e introducir
   el presupuesto por entrada. Confirmar expresamente la operativa automática.
   Se exige Spot PAPER cifrado desbloqueado y uso exclusivo de la cuenta.
4. Consultar estado, permiso restante, posición propia y diario. El diario
   identifica decisiones y estados de pedido; sólo fills verificados cambian
   el libro. Una decisión BUY no se presenta como una compra ejecutada.
   «Gráfico» muestra los fills REAL propios a sus precios/horas, separados de
   las velas públicas MEXC. Seleccionar una ejecución o su marca permite
   inspeccionar cantidad, comisión, motivo y referencia al pedido/asiento.
5. «Detener bot» bloquea nuevos envíos. No cancela órdenes abiertas ni vende
   posiciones: revisar «Órdenes MEXC» y la operativa manual si queda exposición.
   Bloquear/caducar la bóveda, cambiar claves o cerrar el servidor lo detiene.
   No se reinicia automáticamente al volver a abrir la app.
   En el diario, «Parar y descartar» libera sólo preparaciones que nunca
   iniciaron envío. «Seguir orden» detiene y consulta pedidos enviados/inciertos
   con View, sin reenviar; registra los fills confirmados una única vez.
6. Para continuar, «Reanudar» abre el mismo formulario: elegir una observación
   PAPER actual de la misma versión/temporalidad, su reserva y revisar presupuesto/límites.
   «Revisar y reanudar» comprueba otra apertura, fills, saldo libre y pedidos.
   Si había STOP de cuenta, su reapertura se confirma en la misma transacción.
   Aceptar el hueco sin vigilancia no reconstruye operaciones anteriores: se
   espera otra vela para comprar, aunque una posición existente puede venderse
   al recuperar el seguimiento. No se garantiza su precio de salida.

En el modo predeterminado **Sesión**, el permiso sigue la inactividad de REAL
(15 minutos por defecto) y exige el origen PAPER desbloqueado. El sondeo no
prolonga ese permiso. **Operativo** y **Continuo** son concesiones separadas y
optativas, descritas a continuación. Ninguno equivale a un servicio permanente 24/7.

## Preparación y estado de los bots

Desde v90 una autorización nueva requiere una reserva reproducible compatible.
La reserva usa la misma versión, motor, par, temporalidad y datos públicos MEXC;
su histórico debe terminar antes de las observaciones PAPER. Puede ser un
dataset distinto más antiguo; las velas comunes deben coincidir. Si no aparece
ninguna opción, «Validar» abre LAB o DCA para guardarla. Una prueba con datos de
otro proveedor o un CSV sin procedencia pública no sustituye esta evidencia.
Las opciones se repiten al autorizar, no se certifican sólo por aparecer en la lista.

El identificador queda en la tarjeta y en la autorización cifrada. «Legado · sin
reserva» conserva la interpretación de bots antiguos; repetir un pedido nunca
les renueva permisos, y reanudarlos exige elegir una reserva. En DCA el contexto
REAL se reinicia al activar y se conserva al reanudar: su precio de referencia
puede diferir del histórico. No se importa efectivo ni se exige rentabilidad
positiva. El [protocolo OOS](VALIDACION_OOS_PLAN.md) describe la evidencia exacta.

El indicador junto a «Nuevo bot» o «Reanudar» describe **ese formulario**, no
el permiso de los trabajadores existentes. «PAPER · no disponible» impide una
nueva activación/revisión; no afirma que un bot Operativo/Continuo se haya
detenido. Su estado, diario y STOP siguen en la tarjeta de cada bot. En Sesión
se mantiene la obligación anterior de conservar PAPER desbloqueado.

La preparación comprueba pruebas, límites y Trade. Durante la consulta o si
falla/no se reconoce su respuesta, no se puede confirmar una autorización,
ni siquiera disparando el submit por código. Una respuesta antigua no
reemplaza otra más reciente; el sondeo de los bots tiene una secuencia
independiente y no espera a que terminen esas consultas. Un fallo de estado
marca «Sin verificar», conserva STOP y no inventa una pausa ni actividad.
Al recuperar datos verificables se muestran otra vez sus estados efectivos.

Las indicaciones son compactas, con detalle contextual sincronizado en
`data-help`, título y descripción accesible. Una confirmación válida de rearme
vuelve a «Nuevo bot», sin presentarla como error ni generar otro intento.
La preparación nunca sustituye las comprobaciones financieras y de autoridad
del backend al confirmar; no cambia límites, claves o caducidades.

### Verificar origen

En **Bots · REAL**, elige la prueba PAPER y su reserva y pulsa **Verificar origen**. La consulta reproduce el backtest de origen, el tramo de validación y la última observación cerrada de PAPER. Muestra la versión, el periodo y las velas de reserva y la hora UTC de la última vela PAPER. La huella completa está disponible al situar el puntero sobre el título del resumen.

Al activar o rearmar LONG o DCA desde la interfaz, ORBIS verifica el origen, lo muestra en la confirmación y guarda la misma huella en la autorización. Si la reserva o la última vela cambian antes de registrar el permiso, hay que revisar el origen de nuevo. La consulta funciona con los archivos locales desbloqueados y la autoridad sigue dependiendo de la confirmación, la cuenta, los límites y el saldo. Los historiales anteriores mantienen su evidencia original.

### Cuenta seleccionada

Al abrir **Preparar MEXC REAL** desde PAPER, el formulario conserva la sesión elegida y lee el estado del libro REAL seleccionado. Usa **Conectar View**, **Elegir cuenta** o **Revisar vínculo** si aparece esa acción. Con el libro vinculado, **Configurar Trade** abre la verificación de la clave correspondiente. Si hay una orden o retirada pendiente, el acceso abre su apartado de revisión.

El estado local se consulta mientras el panel permanece abierto. El STOP bloquea una activación nueva. La reanudación de un bot existente puede incluir la revisión de reapertura, una vez resueltas órdenes y retiradas pendientes. Al confirmar se vuelven a comprobar el saldo, las órdenes, los permisos y la evidencia de la estrategia. Si se pierde la respuesta a una activación nueva, el panel consulta el registro de la cuenta para recuperar esa misma autorización.

## Permiso Operativo: vista protegida, política con caducidad

Seleccionar «Permiso → Operativo · hasta política» exige otra confirmación
explícita al activar o reanudar. `OPERATIONAL_V1` fija ID de concesión, política,
origen, hashes y fecha de caducidad en el historial cifrado. La fecha es la de
la política **ya revisada**, como máximo 24 h desde su apertura; puede ser menor.
No se amplía el plazo de la política ni se renueva automáticamente la evidencia
de saldo. Tampoco cambian presupuesto, comisiones, exposición, límites o STOP.

- **Proteger vista** revoca el token HTTP y limpia conexiones interactivas,
  importes y secretos de pantalla. Los bots con concesión operativa siguen
  usando sus clientes View/Trade sólo en memoria y su mercado REAL aprobado.
  Pueden continuar después de cerrar/bloquear PAPER: su prueba y reglas quedaron
  fijadas al autorizar. No adopta cambios nuevos de estrategia por ese cierre.
- La inactividad hace esa misma protección si queda una concesión operativa.
  Sin ella mantiene el bloqueo criptográfico anterior. La vista protegida
  indica «Operativa activa»; **no afirma que la bóveda esté cerrada** mientras
  el trabajador conserva la clave en memoria. El aviso completo vive en
  Configuración → Privacidad y seguridad, además de la confirmación explícita.
- La contraseña vuelve a abrir la vista, no reinicia el trabajador ni renueva
  el permiso. Una contraseña errónea no lo amplía ni concede acceso. Las
  lecturas y la actividad HTTP no extienden su reloj UTC o monotónico.
- **Bloquear** o **Detener y bloquear** revoca todos los permisos y cierra la
  bóveda tras esperar llamadas ya iniciadas. La pantalla protegida ofrece una
  parada local sin contraseña: sólo acepta la confirmación de detener, no
  puede iniciar bots, leer importes, enviar órdenes, vender ni retirar. Rechaza
  orígenes ajenos; un programa del mismo usuario local ya puede terminar el
  proceso. Esa parada puede dejar posiciones abiertas y pedidos por revisar.
- Al expirar o fallar el último bot operativo con la vista protegida, se
  descartan clientes y se programa el cierre de la clave. El callback no
  espera al cerrojo HTTP desde la hebra del bot, para no bloquear STOP.
  Si el archivo impide guardar la pausa, el proceso conserva el aviso de
  revisión para el estado público y las paradas posteriores. Cerrar de nuevo
  o reautenticar no lo convierte en una reparación del archivo.
- Cerrar/reiniciar el proceso destruye el permiso en memoria. El descriptor
  cifrado conserva auditoría, **no autoriza auto-reanudación**. Volver a operar
  requiere revisar origen/apertura y confirmar una nueva concesión.

La ejecución necesita servidor y equipo despiertos. Suspensión, desconexión,
errores o pedidos inciertos conservan sus puertas de revisión; no se rellenan
compras retrospectivas ni se garantiza ejecución/protección de precios. Un
permiso Operativo diario revisado no se amplía automáticamente. La continuidad
requiere el opt-in diferente descrito a continuación.

## Permiso Continuo: fecha y límites fijos, evidencia diaria

«Permiso → Continuo · con caducidad» permite escoger «Hasta»: máximo 30 días,
sin superar el borrador de límites. Exige su propia confirmación al activar o
reanudar y conserva los controles de vista protegida, clave/credenciales en
memoria y parada del modo Operativo. Sesión sigue siendo el predeterminado.

El [contrato de continuidad](MEXC_CONTINUIDAD.md) fija fecha, cuenta, bot,
origen, versión, presupuesto y límites. Antes de caducar la evidencia diaria,
dos barridos View causales pueden crear una política hija dentro de esa misma
concesión. No extienden la fecha final, no reinician gasto/activaciones DCA,
no adoptan operaciones manuales ni levantan STOP. El panel muestra la fecha y
las renovaciones; el diario y las copias mantienen el linaje completo.

Reautenticar o repetir una petición no crea otra hebra ni amplía el permiso.
Al caducar la concesión o fallar la evidencia se detiene; con vista protegida
también se cierra la clave. Reiniciar ORBIS nunca recupera autoridad. Los
errores, huecos y suspensión requieren revisión; el equipo debe permanecer
encendido. El flujo está verificado con exchange ficticio más allá del primer
día, no con fondos del usuario ni como garantía de disponibilidad 24/7.

## Wallet → bot → wallet

1. Registrar el saldo inicial en una cuenta wallet separada de la cuenta MEXC,
   y guardar su vínculo EOA en BNB Chain. Los USDT deben figurar como efectivo
   USDT, no como una posición valorada en otra divisa. La cuenta MEXC también
   puede contener fondos previos, correctamente registrados y revisados.
2. En la cuenta MEXC, conectar View y la wallet, verificar control y consultar
   «USDT · BNB». Seguir el [ingreso USDT/BSC](WALLET_FUNDING_BNB.md).
   Tras recibo y abono View, «Contabilizar → Desde [wallet] · traslado USDT»
   registra ambos lados. El ingreso no es beneficio ni habilita el bot.
3. Verificar Trade, elegir versión LAB/PAPER y sus límites, y autorizar
   «Bots · REAL». Se revisa otra apertura. Compra/venta y comisiones se
   registran sólo después de observar fills propios; PAPER no aporta dinero.
4. Para volver a wallet, «Parar operativa» impide nuevos envíos. Resolver antes
   órdenes pendientes y comprobar qué efectivo USDT hay: parar no vende BTC
   ni elimina cantidades residuales inferiores al lote o al mínimo de orden.
5. Seguir la [retirada USDT/BSC](MEXC_WITHDRAWAL_BNB.md), con destino guardado
   en MEXC y credencial independiente de retirada. Comprobar recepción y
   registrar «Traslado interno → [wallet]». Neto y comisión quedan separados;
   la cartera se actualiza aunque el panel del bot esté cerrado. STOP permanece.

Este recorrido está verificado por API y Chromium sobre una única bóveda,
wallet y exchange ficticios con saldos compartidos. La prueba cubre respuesta
de retirada perdida, consulta View/RPC, respuesta contable perdida, reapertura
y copia cifrada. Sus precios/señales son de prueba: no demuestra rentabilidad,
disponibilidad del proveedor ni éxito mainnet. El ensayo humano mínimo con
credenciales introducidas localmente sigue pendiente.

## DCA REAL: sólo compras

1. En DCA, guardar una versión, un backtest repetible sobre un dataset diario
   MEXC Spot/USDT verificado, fijarlo en PAPER y esperar una observación válida.
   El origen DCA y LAB tienen que estar desbloqueados; DCA sin cifrar no se
   autoriza para operar REAL.
2. En Cartera real, guardar «Límites de estrategia» para esa versión DCA,
   verificar View/Trade y cuadrar el inventario como en LONG. El selector
   distingue DCA de LONG; no se fabrica una estrategia LONG para las compras.
3. En «Bots · REAL», seleccionar la prueba DCA y sus límites. Introducir
   presupuesto por orden, **asignación total REAL** y **límite mensual REAL**.
   Confirmar compras automáticas. Activar no compra una vela ya revisada.
4. Una nueva vela diaria cerrada puede dar BASE por calendario o DIP por caída.
   Hay como máximo una compra de cada activación; BASE y DIP diferentes pueden
   compartir vela, pero se envían secuencialmente, esperando finalización y
   asiento de cada pedido. Cada decisión conserva tipo, referencia, regla,
   presupuesto calculado y origen. El gráfico muestra sólo fills ejecutados.
5. Detener y reanudar conserva asignación, gasto, compras, niveles consumidos y
   referencia. La revisión no repone capital, no borra el gasto mensual ni
   compra señales anteriores. Un hueco de velas requiere revisión.

El helper de condiciones es compartido con histórico/PAPER, no su cartera.
La referencia de caída inicial es el último cierre conocido al activar REAL;
`high_watermark` sólo avanza con cierres conocidos durante ese seguimiento.
Calendario y mes se interpretan en UTC. Un intento durable consume su clave
BASE/periodo o DIP/nivel incluso si se cancela o acaba sin fill: no hay reintento
automático ni replay de un POST incierto. El cooldown de compras adicionales
y su número máximo se calculan desde esos intentos reales, no desde PAPER.

La asignación no es una aportación ni una reserva bancaria: es un techo de
autorización local. Se descuenta exclusivamente el coste de fills propios
asentados, sumando comisiones quote; la comisión en base reduce unidades, no
se suma otra vez a la caja. Cada mes usa la fecha de ejecución real. El techo
mensual efectivo es el menor entre la concesión REAL y el de la regla probada.
El importe también respeta petición BASE/DIP, capital restante, saldo libre,
exposición de unidades propias/capital asignado, mínimo, lote, comisiones,
slippage y todas las restricciones del guardián compartido. Fondos ingresados
después no amplían por sí solos esa concesión. El rearme no permite cambiarla.

DCA acumula activos: **no vende ni coloca stops**. Para volver a USDT se debe
detener, revisar pedidos y utilizar la operativa manual; retirar sólo permite
el efectivo disponible. El permiso sigue siendo temporal (15 min por defecto),
requiere servidor/orígenes desbloqueados y no ofrece ejecución desatendida 24/7.
PAPER observa una ventana de 15 min, pero un envío REAL respeta la vigencia más
estricta de la política; una observación tardía no provoca una compra caducada.
La ruta utiliza el mismo [pedido LIMIT oficial de MEXC](https://www.mexc.com/api-docs/spot-v3/spot-account-trade/new-order),
sin endpoint ni permisos de retirada nuevos.

`dca_bot_source.py` verifica versión, backtest fijado, dataset y ledger PAPER;
`dca_bot_plan.py` decide con capital/fills REAL. El tipo DCA, sus techos y
referencia viven en la configuración/cursor cifrados existentes; no añade
tablas ni altera la semántica de simulaciones antiguas (`dca-1.6`).

### Compartir un plan DCA

En Simulación PAPER → DCA, elige un plan y una versión y pulsa «Exportar versión». Para abrir un archivo recibido, usa «Importar JSON», revisa los parámetros y pulsa «Guardar copia». Luego puedes elegir histórico y ejecutar «Comparar». La huella verifica el contenido del archivo. La copia empieza en versión 1 y tiene sus propias pruebas, sesiones y autorizaciones.

Una plantilla usa el mismo modelo de regla que el formulario. Introduce los importes y proporciones como cadenas decimales. Las frecuencias admitidas son `daily`, `weekly` y `monthly`.

~~~json
{
  "name": "DCA mensual",
  "definition": {
    "frequency": "monthly",
    "amount": "100",
    "budget_total": "1200",
    "monthly_limit": "150",
    "max_exposure": "0.8",
    "fee_rate": "0.001",
    "slippage_rate": "0.001"
  }
}
~~~

### Medias móviles

En Simulación PAPER → DCA, guarda la versión con SMA o EMA, ejecuta su backtest y reserva cronológica y deja que PAPER registre una observación. «Preparar MEXC REAL» lleva esa sesión a la cuenta elegida. Selecciona límites para su versión, asignación total, máximo mensual y presupuesto por compra. La confirmación del bot es el paso que concede permiso.

Cada compra usa la media de cierres verificados y respeta los límites REAL. El diario muestra el importe solicitado antes de limitarlos. Con factor cero el estado indica «Importe ×0». Si rearmas el mismo bot, el origen PAPER debe conservar el historial de cierres ya archivado. El piloto con la cuenta del propietario sigue pendiente.

### Volumen relativo

Prueba el plan DCA en histórico y en PAPER. Desde la sesión usa **Preparar MEXC REAL** y revisa la misma versión, la reserva, la cuenta vinculada, el presupuesto total, el máximo mensual y el límite por compra. El permiso se concede al confirmar la activación. Esta ruta está disponible para bots MEXC Spot, con velas diarias del par de la estrategia.

Antes de evaluar una compra, ORBIS comprueba que el volumen y los precios de todas las velas de referencia coincidan con el archivo del bot. Usa el volumen de la vela cerrada, comparado con la media de las anteriores. Una vela abierta queda fuera. Si el proveedor cambia alguna vela de referencia, el bot se pausa para revisión. Si el volumen es inferior al mínimo, la compra queda en espera. Al alcanzarlo, el diario conserva el periodo, el umbral y el volumen relativo. [API de velas de MEXC](https://www.mexc.com/api-docs/spot-v3/market-data-endpoints/klinecandlestick-data)

El rearme coteja los cierres y el volumen ya archivados y conserva los límites y el gasto acumulado. La prueba de extremo a extremo usa un exchange controlado. Sigue pendiente el piloto con fondos reales.

## Contrato LONG

- LONG Spot/USDT, una posición propia y un bot activo por cuenta. No préstamo,
  apalancamiento, shorts ni mezcla de unidades con el inventario anterior.
- Requiere una versión actual LAB, un backtest fijado y repetible y al menos
  una observación íntegra de una sesión Spot PAPER activa. No certifica
  rentabilidad, robustez, tamaño suficiente de muestra ni idoneidad de la
  estrategia: la autorización no sustituye ese análisis.
- La activación revisa además View/Trade, saldo de apertura y límites REAL.
  Política y bot se guardan en una transacción: un fallo no deja una política
  activa huérfana. La activación no transmite órdenes y espera otra vela.
- El presupuesto por compra se expresa en USDT real, separado de capital,
  asignación, precios y posiciones ficticias. Conserva comisiones publicadas,
  precisión/tamaño de lote y bandas de slippage del guardián REAL.
  El tamaño reserva la mayor tarifa entre la publicada y el máximo aprobado
  en la política, para que el presupuesto cubra ese peor coste autorizado.
  Si un fill cobra por encima del máximo o excede el presupuesto, incluso
  cuando termina en un ciclo de seguimiento posterior, se conserva
  el hecho ejecutado y se pausa el bot para revisión; nunca se finge deshacerlo.
- La entrada usa una vela cerrada nueva, su regla LAB reproducida y una señal
  todavía vigente al enviar. No repite la entrada durante la misma vela.
  Un salto de velas o una sesión PAPER detenida exige revisión.
- Las salidas usan la posición y el coste efectivo de fills REAL propios.
  Stop/objetivo se comparan con `coste_restante / unidades_restantes`, incluido
  el coste de comisiones. Stop móvil usa máximos de bid observados desde la
  entrada, no máximos de una vela anteriores al fill. Regla de salida y límite
  de permanencia se evalúan con una vela cerrada nueva.
- Tamaños de venta reservan también la posible comisión en base. Un resto
  inferior al mínimo del exchange sigue perteneciendo al bot; no se muestra
  como una liquidación completa ni se vende inventario ajeno para completarlo.
- Un intervalo de más de 30 s sin observación de riesgo con posición pausa el
  bot. Se guarda la hora del ticker realmente usado, no la hora de acabar
  peticiones. Sincronizar una orden no inventa cobertura de precios.
- Las órdenes son LIMIT: pueden quedar abiertas o parciales. No existe una
  promesa de ejecución o stop garantizado. Mientras no estén finales,
  asentadas y conciliadas, no se prepara otra orden. Un timeout posterior al
  POST conserva el pedido y pausa; no se reenvía por suponer que falló.

## Módulos y datos

`spot_bot_source.py` comprueba versión, backtest, archivo PAPER encadenado y
evidencia de entrada/salida. Su contrato no exporta caja, unidades ni acciones
PAPER. El coste y las unidades reales se reconstruyen en
`spot_bot_position.py` desde fills propios con comisiones en base o quote.
`spot_bot_plan.py` calcula un paso puro y provisional. El coordinador
`mexc_spot_bot.py` reclama un ciclo, archiva la decisión y utiliza el mismo
transporte y seguimiento que la operativa manual.

### Mercado propio REAL y preparación de ejecución continua

`spot_bot_market.py` incorpora `REAL_BOT_MARKET_V1` a las activaciones nuevas.
Al autorizar se fija una semilla cifrada: versión/reglas, histórico conocido,
backtest de origen, observación PAPER y hora de revisión. El bot solicita
directamente las siguientes [velas públicas MEXC](https://www.mexc.com/api-docs/spot-v3/market-data-endpoints/klinecandlestick-data)
sin esperar una compra o una nueva observación PAPER. Reproduce las mismas
reglas/indicadores con esa semilla y sus cierres observados. No hereda caja,
ejecuciones ni unidades de la simulación.

Sólo añade una vela cerrada contigua por paso, después de contrastar dos velas
de solape. Rechaza cambios, duplicados, ventanas ajenas y saltos; no descarga un
hueco para convertirlo en compras retrospectivas. Si aún sólo están publicados
los dos cierres conocidos, espera sin comprar dentro de la vigencia de la
política. Superar esa ventana o recibir datos incoherentes exige revisión.
Una vela abierta nunca se utiliza para decidir una entrada.

La bóveda **v31** añade `real_spot_bot_market_streams` y
`real_spot_bot_market_observations`. La semilla queda ligada a la configuración
aprobada; cada observación encadena su predecesora y queda ligada también a la
cabeza guardada en el cursor. El cierre del ciclo conserva esa cabeza incluso
si la evaluación posterior falla. Las decisiones referencian exactamente el
stream, secuencia, vela y digest que usaron. El verificador de copias comprueba
semillas, cadenas, cabezas y referencias; una observación no se convierte en un
fill por estar archivada. Las huellas detectan incoherencias locales, no son una
firma externa ni defienden contra quien pueda reescribir toda la bóveda.
La ausencia completa de una semilla también se detecta, incluso antes de la
primera orden. Un diario incoherente no impide bloquear: se revocan permisos,
se borran referencias a credenciales y se cierra la bóveda en todo caso. Si no
se pudo guardar la pausa, el bloqueo confirmado avisa de revisión necesaria;
no se finge reparar o conciliar el archivo. Si se pierde la respuesta HTTP,
la pantalla protege sus datos y distingue falta de confirmación de un bloqueo
confirmado por el servidor.

El rearme crea otra semilla para la política revisada y conserva los streams
anteriores, igual que conserva sus órdenes/fills. La migración v30→v31 mantiene
una preimagen cifrada y verifica recuentos: no fabrica observaciones de mercado
para bots antiguos. Éstos mantienen su contrato PAPER anterior; sus datos
siguen legibles y no se convierten en otra fuente mediante una migración.
«Mercado REAL» identifica el seguimiento propio y la última vela en el panel;
no es una etiqueta de ejecución 24/7 ni de rentabilidad.

**Decisión de seguridad:** separar los datos no amplía por sí solo la autoridad.
Sesión mantiene sus bloqueos/plazos anteriores. Los opt-in Operativo y Continuo permiten
proteger la vista y cerrar PAPER conservando la clave para la concesión acotada;
lo declara antes de confirmar y mientras opera. STOP, caducidad y ausencia de
auto-reinicio siguen vigentes. Ningún sondeo renueva un permiso financiero.

`CONTINUOUS_V1` añade renovación causal diaria bajo duración/límites humanos
inmutables; `OPERATIONAL_V1` no cambia de contrato. No se aumenta simplemente
el temporizador HTTP: se conserva mercado aprobado, permisos operativos en
memoria, linaje de políticas y revocación. Sigue pendiente el ensayo humano
mainnet autorizado; no hay auto-reanudación ni servicio permanente garantizado.

El resultado `BUY`/`SELL` del ciclo identifica una decisión, no garantiza un
fill. La UI muestra por separado el estado del journal y la
ejecución externa; sólo los fills verificados modifican el libro o la posición.

### Preparación atómica y recuperación

El protocolo `DECISION_RESERVATION_ATOMIC_V1` guarda decisión, puntero de pedido
y reserva/rechazo en un mismo `BEGIN IMMEDIATE`. Una caída no deja una decisión
nueva sin su journal. La función de reserva tiene una variante dentro de la
sesión existente: no abre/cierra otra conexión ni libera el bloqueo exterior.
Conserva el cálculo de límites, con sumas decimales en contexto de 300 dígitos.
Una única decisión puede prepararse en cada ciclo, también entre instancias.

Sólo el ciclo que acaba de confirmar esa preparación recibe una continuación
en memoria, de un solo uso, ligada a instancia/generación de bóveda, ciclo,
decisión y huella exacta del pedido. No admite copia/serialización ni se
reconstruye al abrir la aplicación. Una reserva guardada sin esa continuación
devuelve su estado, nunca un permiso de envío. El trabajador sigue necesitando
su autorización vigente, View/Trade, reglas/datos recientes y todas las puertas
de riesgo; las marcas durables previas al único POST permanecen intactas.

Las decisiones nuevas indican el protocolo y ciclo en su payload cifrado;
lecturas y copias verifican su relación con la reserva exacta. No añade tablas
ni cambia SQLCipher v29. Los formatos anteriores conservan sus huellas. Una
decisión antigua sin reserva local aparece en el diario con «Consultar en
MEXC». Con View de la misma cuenta, ORBIS coteja los términos de la orden
y sus fills. Si coinciden, muestra el estado de MEXC y el número de fills.
Una consulta sin resultado mantiene la decisión sin verificar. Para
decisiones de siete días o más, usa el ID que muestra ORBIS para buscar en
el historial web de MEXC.

Si MEXC confirma cancelación y cero fills, ORBIS ofrece guardar el cierre. Al confirmar vuelve a consultar la orden y guarda la evidencia en la bóveda, junto a la decisión original. El bot queda pausado. Para continuar, abre «Reanudar» y confirma la revisión normal de saldo y límites. Con fills de una orden final, «Consultar en MEXC» permite conciliar si
ORBIS reproduce la solicitud original con su huella archivada. La vista
previa muestra unidades, contravalor y comisiones. Al confirmar, el libro
resultante debe cuadrar con el saldo de MEXC. Un posible duplicado manual
requiere revisión de cartera. El bot sigue pausado hasta un nuevo rearme.
Las órdenes abiertas, las solicitudes originales que no se pueden
reproducir y las comisiones en un tercer activo quedan pendientes.

Si la preparación queda `RESERVED`, el diario permite parar el bot y usar el
descarte local existente, que sólo admite estado/motivo inequívocamente previo
al envío y ausencia de orden externa observada. Deja un `STOPPED` permanente,
libera la reserva y no altera el patrimonio. Si el envío ya empezó, se rechaza
ese descarte y se sigue con View. Una ausencia en el exchange no concede replay.
El seguimiento puede registrar ejecuciones y cerrar la reserva, no habilita
órdenes nuevas ni reanuda el bot. Hace falta otra apertura/revisión explícita.

El diario abierto se vuelve a consultar pasivamente; ante un error no presenta
la copia anterior como verificada. Después de cada recuperación se relee incluso
si se perdió su respuesta HTTP. Repetir descarte/seguimiento no duplica órdenes
ni asientos. Las credenciales de retirada no intervienen en este recorrido.

La bóveda SQLCipher v25 incorpora `real_spot_bots` y
`real_spot_bot_decisions`. Configuración, cursor y decisiones tienen huellas;
las decisiones incluyen observación PAPER, revisión de fills propios, bid,
ask, hora y posición REAL usada. Los pedidos y fills permanecen en las tablas
de ejecución existentes. No se guarda una credencial o autorización efímera.
La migración trabaja sobre copia cifrada, preserva una preimagen y verifica
recuentos e integridad; no borra ni convierte registros anteriores.

Un `cycle_id` durable excluye ciclos concurrentes entre instancias de bóveda.
Si el proceso se interrumpe no caduca ni se roba esa reclamación. El rearme
sólo la invalida tras detener el bot y comprobar que todas las decisiones
tienen un resultado final verificable, sin reservas ni envíos inciertos.
Una fila `ACTIVE` tampoco es un permiso para
reiniciar automáticamente un trabajador al abrir la app. El llamante debe
conservar una autorización efímera comprobable hasta el cerrojo final del
POST. Las órdenes manuales quedan bloqueadas si la cuenta tiene bot activo
o si se intenta reutilizar una política reservada al bot.

### Rearme y trazabilidad

`spot_bot_rearm.py` reutiliza la revisión de apertura y la reapertura de STOP.
Mantiene el mismo `bot_id`, todas sus decisiones y fills. La configuración v25
añade un historial JSON plano de autorizaciones, compatible con los bots
anteriores de una sola política: no requiere migrar ni reescribir decisiones.
Cada revisión enlaza política nueva/anterior, origen LAB/PAPER, presupuesto,
huella de fills/libro, unidades/coste, bid/ask y el intervalo sin vigilancia.
La sesión/dataset PAPER puede renovarse, pero no la versión, reglas, mercado
o temporalidad del bot. Cambiar de estrategia exige otro bot.

Las políticas antiguas siguen vinculadas al bot y nunca se convierten en
operativa manual. Un replay devuelve el registro sin iniciar otra hebra ni
rearmar una parada posterior. El trabajador está ligado a una política:
un ciclo o callback antiguo no puede enviar ni pausar la autorización nueva.
STOP, bloqueo y cambio de credenciales revocan la revisión antes de esperar
a la red/SQLite. Nueva política, cursor, historial y eventual reapertura de
STOP se confirman juntos; cualquier fallo conserva la pausa.

### Reanudar tras cambios de efectivo

En **Bots · REAL**, elige **Reanudar** sobre el bot pausado y revisa sus límites. Si ORBIS identifica sólo movimientos posteriores de efectivo del mismo par, muestra el importe exacto, las unidades propias y su coste. Confirma la revisión para conservar la posición y continuar con los límites elegidos. La revisión por sí sola no envía órdenes ni reasigna inventario. El rearme vuelve a comprobar el saldo en MEXC y sólo acepta el mismo libro, la misma posición y la revisión vigente. Si cambian, vuelve a revisar.

ORBIS verifica las huellas de los asientos y reproduce la huella anterior al retirar del cálculo hasta 64 movimientos de efectivo recientes. Las compras manuales conciliadas se revisan según el procedimiento siguiente. Los bots antiguos con unidades y sin huella también requieren revisión. Un saldo residual sigue siendo exposición.

### Reanudar tras una compra manual

Pausa el bot y ejecuta la compra en **Orden Spot**. Una vez conciliada, vuelve a **Bots · REAL → Reanudar**. La revisión distingue las unidades netas compradas manualmente, la variación de efectivo y la posición y coste propios del bot. La confirmación conserva el inventario manual separado y autoriza la reanudación con los límites revisados.

El cálculo reproduce el libro anterior y acepta hasta 64 movimientos recientes del mismo par. Para una compra exige una orden manual ORBIS cerrada, su observación, fills y asientos exactos. Una comisión en base reduce las unidades netas de la compra. La apertura y la confirmación verifican de nuevo la asignación y el saldo. Las ventas manuales se revisan según el procedimiento siguiente. Otros movimientos de activos y los bots con posición y sin huella conservan el bloqueo de atribución.

### Reanudar tras una venta manual

Después de conciliar la venta en **Orden Spot**, abre **Bots · REAL → Reanudar**. La revisión muestra la cantidad vendida, el inventario manual antes y después de los movimientos y la posición propia del bot. Confirma la asignación vigente y los límites para reanudar.

ORBIS consume primero las unidades ajenas al bot en el momento de la venta. La comisión en el activo vendido se suma a las unidades consumidas. Si queda un débito atribuible a un solo bot, la confirmación muestra las unidades y el coste antes y después, junto con el ingreso neto que le corresponde. Reanudar conserva los fills originales y usa la posición restante para las siguientes órdenes. El gráfico sigue mostrando los fills de órdenes emitidas por el bot. La ficha indica por separado los fills de venta manual atribuidos.

Si hay varios bots con unidades del activo, si el orden de las ventas resulta ambiguo o si faltan originales de la conciliación, el bot sigue pausado. El libro patrimonial conserva su criterio contable.

Las guardas del POST y la confirmación de rearme reutilizan la sesión SQL
existente. No abren/cerran otra sesión sobre la conexión SQLCipher del hilo,
lo que liberaría `BEGIN IMMEDIATE` por un rollback implícito. Hay una prueba
específica de conservación del cerrojo tras la autorización final.

## Pendientes

1. Revisión de ventas ambiguas entre bots, otros movimientos de activos y decisiones
   heredadas huérfanas sin reserva. Las nuevas preparaciones son atómicas.
   Reservas no enviadas se liberan explícitamente desde el diario;
   pedidos inciertos se sincronizan antes de otra apertura, nunca se borran
   o reenvían desde «Reanudar».
2. Avisos ampliados de muestra/robustez. Las marcas de fills REAL ya están
   disponibles en el gráfico propio del bot, no en todos los gráficos generales.
3. Ensayo humano con credenciales locales y un importe mínimo. Nunca usar
   claves de retirada para operar Spot ni pedir secretos por el chat.

El [ingreso USDT/BEP-20](WALLET_FUNDING_BNB.md) tiene un módulo separado de
plan, solicitud confirmada en wallet, cotejo recibo/abono View y registro
contable explícito. No activa ni amplía un presupuesto del bot por sí mismo.
La [retirada USDT/BSC](MEXC_WITHDRAWAL_BNB.md) también dispone de solicitud única,
recuperación View, recibo observado y asiento explícito, probados con exchange
controlado. Bots de formatos externos y ensayo humano mainnet siguen pendientes.
Este núcleo no transmite fondos entre wallet y exchange.

## Gráfico de ejecuciones REAL

La API pasiva `/{bot_id}/executions-page` verifica los mismos fills que la
posición: intención y decisión propias de cualquier autorización del bot,
orden final, reserva conciliada, fill íntegro y asiento correspondiente.
Una decisión, límite de orden, señal PAPER o pedido pendiente no se convierte
en un precio ejecutado. Si falta cierre o evidencia, el gráfico se declara
no verificable; no conserva marcas antiguas como si fueran una revisión actual.
La lectura funciona sin View/Trade/PAPER conectados y no renueva su permiso.

La paginación usa páginas de hasta 500 y una huella de la instantánea completa;
si llegan otros fills entre páginas, rechaza mezclar versiones. No usa el
límite de 100 decisiones del diario para reconstruir la gráfica. No modifica
esquema, configuración, reglas, pedidos ni asientos; sólo expone una proyección
de datos cifrados ya existentes. Los cálculos siguen siendo decimales exactos.

El componente separado `mexc-bot-chart.js` reutiliza Lightweight Charts local
v5.0.9 y el selector común de años/fechas. Las marcas se sitúan en puntos reales
de ejecución, no se desplazan a la vela de la señal ni se inventan OHLC para
anclarlas. Para varios fills del mismo lado en un segundo, agrupa la marca en
el último precio observado de ese grupo, con contador; la inspección conserva
todas las horas a milisegundo, cantidades y comisiones, sin redondeo contable.
BUY/SELL dentro del mismo segundo conservan sus dos marcas. El render convierte
precios a números gráficos, nunca calcula saldos o resultados con ellos.

Las velas de contexto se piden exclusivamente a MEXC para el mercado y marco
del bot, inicialmente hasta 1.000. «Histórico anterior» pagina sin mezclar
fuentes, hasta 12.000 velas por carga. Se identifica cobertura, fills sin OHLC,
vela en formación y errores. Si no hay velas, siguen visibles sólo los puntos
de fills; nunca se cambia a otro exchange sin avisar. Fechas anteriores/posteriores
a los OHLC se pueden seleccionar si contienen ejecuciones reales, marcadas sin
OHLC. La inspección textual permite recorrer los fills sin depender del ratón.
El navegador acota a 100.000 fills por instantánea y muestra error explícito
si se supera, no un subconjunto presentado como historial completo.

La UI no persiste fills, cantidades o credenciales en almacenamiento del
navegador. Bloquear/cambiar cuenta/navegar descarta el contexto y las respuestas
tardías; la ocultación de importes también cubre la inspección. Las lecturas
públicas no llevan el token REAL. Verificado con Chromium y exchange de prueba,
no con dinero real.

### Exportar fills REAL

En **Gráfico**, pulsa **Exportar JSONL** y confirma la descarga en claro. Se lee la misma instantánea verificada del gráfico. Si cambia el historial, actualiza el gráfico antes de descargar. El archivo funciona tras desbloquear REAL, incluso con View y Trade desconectados. Si ocultas importes, el control se deshabilita. [Campos y huellas](IA_Y_ALMACENAMIENTO.md#fills-mexc-real-jsonl).

## Motivo actual de una pausa

La tarjeta de un bot inactivo prioriza su motivo guardado o la falta de una
autorización vigente. Usa etiquetas como «Parada manual», «Revisar ciclo»,
«Revisar orden» o «Permiso no vigente». Un último resultado WAIT pertenece al
historial y no se presenta como el estado actual de un bot pausado. El diario
y los controles de recuperación siguen disponibles; consultar el motivo no
reanuda el bot ni modifica órdenes.
