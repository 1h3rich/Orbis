# GRVT — bots automáticos acotados TESTNET

Estado de implementación, 03-11-2026: el panel **Patrimonio → Recuperación
GRVT → Bot · TESTNET** permite autorizar un episodio: esperar una nueva
señal OPEN de una sesión GRVT PAPER, abrir con FOK y SL/TP vinculados y
seguir las salidas hasta su conciliación. No requiere confirmar de nuevo la
orden cuando llega la señal. El valor inicial es **1 ciclo**. En **Límites**
se puede autorizar hasta 20 entradas, con presupuesto agregado y pérdida neta
explícitos. Ningún permiso se restaura después de reiniciar.

No habilita mainnet. La validación con un proveedor controlado no sustituye
el piloto con permisos Trade TESTNET del proveedor.

## Recorrido operativo

1. Crear/probar la estrategia en LAB y activar su sesión GRVT PAPER.
2. Desbloquear la bóveda REAL y abrir el panel Bot · TESTNET.
3. Elegir sesión y subcuenta de prueba. Definir nocional de entrada, tope de
   salidas, vigencia de entradas, seguimiento e intentos máximos de cierre.
   Para varios ciclos, indicar también entradas máximas, presupuesto total
   y límite de pérdida neta realizada.
4. Introducir API y firma dedicadas TESTNET, autorizar las entradas y activar.
   Antes de guardar el permiso se consultan instrumento y libro públicos. Si
   los mínimos o las protecciones no caben en los topes, se pide revisarlos,
   sin login privado ni concesión. La señal vuelve a validar todo al llegar.
5. El bot espera una OPEN **posterior a la activación**, sin login privado
   durante la espera. Una señal anterior, caducada o de otra sesión no sirve.
6. Al llegar, reproduce la evidencia LAB/PAPER y prepara una única entrada
   protegida. Los tres IDs se guardan antes de enviar. Una respuesta incierta
   exige revisar esos IDs; no genera otra orden.
7. El seguimiento existente evalúa las salidas sobre los fills auditados y
   mercado TESTNET. También detecta y concilia SL/TP ejecutados nativamente.
8. Sólo un recibo financiero con exposición cero completa un ciclo. Con varias
   entradas, el bot comprueba sus límites antes de liberar su STOP automático
   y esperar otra señal posterior al rearme. Al agotar entradas, presupuesto
   o pérdida neta termina la concesión; no inicia otra.

La señal procede de la sesión GRVT PAPER y de su dataset público archivado;
la ejecución, cotización, fills y evaluación posterior de salida usan TESTNET.
No se presenta el precio PAPER como precio ejecutado.

## Gráfico de fills

En **Recuperación GRVT → Bot · TESTNET**, consulta la subcuenta y abre **Gráfico de fills** en el ciclo. El gráfico marca precio y hora de entradas, stops y objetivos con auditoría financiera local. Las reducciones IOC aparecen al conciliarse. El selector conserva el importe, el precio, la comisión y la hora UTC de cada fill. Los fills del mismo tipo y segundo se agrupan en el último precio. Se muestran hasta los 5.000 más recientes del episodio.

La vista usa sólo el archivo cifrado de TESTNET. No carga velas, no mezcla señales PAPER con fills y no envía órdenes. Un ciclo sin fills auditados muestra el estado vacío. Cerrar la bóveda o cambiar de subcuenta retira el gráfico.

## Exportar un episodio

En **Gráfico de fills**, pulsa **Exportar JSONL** y confirma la descarga en claro. El archivo contiene todos los ciclos seleccionados del episodio dentro de un límite de 4 MiB. Los resultados se reconstruyen a partir del archivo cifrado. Si el tamaño supera el límite o la evidencia cambia, se rechaza la descarga completa. [Campos y huellas](IA_Y_ALMACENAMIENTO.md#ejecuciones-grvt-testnet-jsonl).

## Presupuesto y precios

- Entrada: nocional máximo de 1.000 USDT; salidas: máximo de 10.000 USDT.
  Son límites actuales de ORBIS para este circuito, independientes del saldo
  y del margen ficticios de PAPER.
- LONG toma ask; SHORT toma bid. El precio se ajusta al tick sin introducir
  una ampliación adicional del deslizamiento autorizado.
- Cantidad = suelo del presupuesto / precio, a la precisión base del
  instrumento. No se redondea hacia arriba para alcanzar el mínimo.
- SL/TP se calculan a partir de los porcentajes de la estrategia y del
  límite de entrada: stop hacia la entrada, objetivo hacia fuera. El
  observador posterior vuelve a evaluar el riesgo sobre el VWAP ejecutado.
- Se rechazan tamaños, bandas o salidas que violen tick, mínimos, LAST,
  liquidez, riesgo de cuenta, spread o límites. No se aumenta presupuesto
  ni se relaja el stop automáticamente para que encajen.
- Los cálculos usan Decimal con contexto de precisión 100; no dependen de
  la precisión global del llamador.
- Espera: 30–86.400 s. Seguimiento: 30–86.280 s. Las protecciones nativas
  conservan una reserva temporal mayor. El traspaso al observador conserva
  el plazo absoluto; no vuelve a sumar el presupuesto temporal.
- Varios ciclos: presupuesto agregado de hasta 1.000 USDT, entre 2 y 20
  entradas y pérdida neta explícita de hasta el presupuesto. Se reserva el
  tope completo de cada intento; no se reutiliza presupuesto al ganar ni se
  libera una reserva por perder la respuesta. Si el saldo de presupuesto no
  alcanza otra entrada completa, se detiene sin aumentar ni fraccionar el tope.
- El corte de pérdida usa la suma neta conciliada de todos los ciclos,
  incluyendo grupo nativo, reducciones, comisiones y financiación. Se calcula
  desde componentes verificados incluso cuando un recibo antiguo omite el
  campo neto. Se comprueba **al cerrar cada ciclo**; no garantiza un máximo
  de pérdida mientras la posición esté abierta.
- La vigencia de entradas es global y no se renueva en cada espera. Para
  varios ciclos, vigencia más plazo máximo de salidas no supera 24 horas.

Se reutiliza el contrato OTOCO del coordinador existente. La
[API oficial de GRVT](https://api-docs.grvt.io/trading_api/) describe FOK
como ejecución completa inmediata o cancelación, y OTOCO como padre con
TP/SL vinculados. El acuse no se usa como prueba de fill o de protección
garantizada; se mantienen la observación y la conciliación independientes.

## Permisos, concurrencia y recuperación

`GrvtBotService` posee el episodio, sus revisiones de permiso y la unión con
`GrvtStrategyCloseService`. Las claves sólo viven en argumentos/locales de
los trabajadores activos. El formulario las borra al activar y al desmontar;
no se guardan en almacenamiento del navegador o en la base.

La bóveda v46 añade `grvt_testnet_bot_runs`: cabecera inmutable con fuente y
límites, selección única previa al envío y resultado terminal. Cada parte
tiene su digest y su relación con la anterior. Una reserva única por
subcuenta impide dos episodios simultáneos, incluso entre procesos. La
verificación de backup contrasta las definiciones y grupos vinculados y
exige un recibo de exposición cero para un resultado COMPLETE.

La v47 conserva los runs de un ciclo y añade `grvt_testnet_bot_cycles` para
las concesiones v2. Su selección, resultado y rearme están encadenados y
anclados al run. Una cadena truncada, un presupuesto excedido o un resultado
económico distinto de los recibos impide validar el archivo.

`grvt_testnet_stop_controls` y `grvt_testnet_stop_events` registran procedencia
y revisión. MANUAL, REVIEW y LEGACY_STOP quedan enclavados: una petición
AUTO_EXIT posterior no los convierte en una parada automática. El rearme
interno requiere el padre cerrado y la revisión exacta capturada al conciliar.
Las cancelaciones y reducciones archivadas siguen probando el STOP histórico;
sus envíos actuales exigen la parada actual.

Un archivo no terminal se lee como INTERRUPTED, sin autoridad. **Retirar
permiso** libera su reserva explícitamente; no cancela órdenes ni declara
cerrada la posición. Los intentos pendientes del journal de órdenes siguen
bloqueando nuevos envíos hasta su resolución.

STOP revoca el permiso RAM antes de intentar persistir. Bloquear, proteger
la vista o usar la parada general revoca también el episodio. Una parada de
subcuenta invalida un /start retrasado aunque todavía no exista un ID visible.
Una respuesta antigua de activación no puede reemplazar una parada posterior
en la interfaz. Repetir STOP de un episodio terminado no revoca otro nuevo.

**Rearmar subcuenta** aparece sólo para un final satisfactorio cuyo STOP
automático sigue correspondiendo a esa entrada. Es una acción expresa y nueva:
exige recibo plano, revisión actual y ausencia de otra reserva. No arranca el
bot ni recupera claves; la próxima activación necesita credenciales, límites
y revisión nuevos. Una parada manual o incidencia posterior bloquea ese rearme.

Estado y parada siguen respondiendo durante un RPC del proveedor. Si la
bóveda está retenida, el historial devuelve el estado RAM con
`history_pending=true`; no inventa un historial vacío ni rehabilita Activar.
Una espera HTTP vencida se recupera por lecturas, sin repetir POST.

Los registros terminales usan el mismo límite de visibilidad que los demás
datos privados. Las rutas exigen origen local, sesión y token; las lecturas
son pasivas y no renuevan la sesión. Las respuestas usan `no-store` y
transportan nanosegundos/identidades grandes sin perder precisión JSON.

## Salidas nativas parciales, v48

El observador distingue una ejecución parcial de SL/TP de una entrada FOK
parcial o de estados contradictorios. Sólo admite una de las dos salidas
con cantidad ejecutada positiva, padre totalmente lleno y sin otros avisos
de estado. La observación mantiene STOP con causa PARTIAL_EXIT; no acredita
saldo, cantidad operable ni permiso de rearme.

Dentro del permiso de seguimiento, una solicitud cifrada
`orbis.grvt.testnet.native.partial.close.v1` referencia la secuencia y huella
exactas de esa observación. No fabrica un CLOSE PAPER ni necesita que el
mark cruce un nivel. El coordinador cancela una sola vez las protecciones
que sigan abiertas y exige las dos auditorías terminales estables habituales.
Únicamente el residuo conciliado puede originar reducciones IOC reduce-only,
con los intentos y plazo originales. Si llegan más fills durante la retirada,
la cantidad se toma del recibo posterior, no del primer parcial observado.

Si el grupo termina de cerrar antes de registrar la solicitud, se utiliza
la conciliación nativa de sólo lectura. La pérdida de respuestas no repite
POST. Un bloqueo, parada o plazo vencido retira el permiso. La reactivación
expresa de un cierre registrado conserva sus límites; no inicia un bot nuevo.
PARTIAL_EXIT no habilita rearme. AUTO_EXIT sólo aparece al registrar el cierre;
MANUAL, REVIEW y LEGACY_STOP anteriores continúan enclavados. El siguiente
ciclo requiere además el recibo plano verificado y la revisión exacta de STOP.

La v48 conserva las tablas de v47 y añade esta semántica versionada sobre copia
cifrada. Las lecturas y backups reproducen el motivo desde la observación
archivada y validan límites, referencias y huellas. Las auditorías financieras
terminales y sus recibos mantienen sus requisitos, sin relajarlos para un parcial.

## Huérfanas y recuperación de protección, v49

El estado de órdenes distingue cinco recorridos compatibles: padre cancelado
o rechazado sin fills con salidas huérfanas, intento entero ya cancelado sin
ejecución, protección vencida con residuo, cierre completo con una hermana
todavía abierta y protecciones terminales sin fills con posición restante.
Una entrada FOK parcialmente ejecutada, cantidades contradictorias o ambas
salidas parcialmente ejecutadas siguen necesitando revisión.

`orbis.grvt.testnet.native.recovery.close.v1` referencia la observación y sus
términos originales. Comparte la retirada única del grupo, auditorías y recibos
con el coordinador de cierre. No crea una orden nueva si el grupo está plano.
Si queda exposición, sólo reduce la cantidad del recibo financiero, con IOC
reduce-only y los límites otorgados. Una orden que sigue OPEN impide conciliar,
aunque exista un acuse de cancelación o se haya superado su vencimiento local.

Durante un permiso válido del bot se resuelven huérfanas y protecciones
terminadas compatibles. Una protección vencida no prolonga ese permiso:
**Recuperar grupo** otorga un permiso expreso independiente, de 30 a 300 s,
para esa posición y sin nuevas señales/entradas. Usa confirmación específica
y el mismo control de revisiones y STOP. Se archiva como seguimiento v2 con
`mode=RECOVERY`, que no admite observaciones de mercado. Los seguimientos
anteriores v1 conservan su formato. Un cierre ya registrado mantiene sus
intentos/plazo; esta acción no renueva un permiso anterior vencido. Si existe
cancelación previa o cierre vencido sin terminar, se conserva la recuperación
manual del diario: auditar, conciliar y revisar la reducción del residuo.

NATIVE_RECOVERY mantiene STOP pendiente antes de la solicitud y no admite
rearme. AUTO_EXIT exige registrar la recuperación; MANUAL/REVIEW/LEGACY
anteriores siguen enclavados. El rearme del bot requiere recibo plano y su
revisión exacta. El intento sin fills conserva presupuesto reservado y neto
cero sólo si saldo, comisiones e historiales lo acreditan. La proyección
`entry_execution`, derivada del recibo verificado, muestra **Sin ejecución**;
no inventa operaciones ni transforma un CLOSE PAPER en ejecución GRVT.

## Pendiente para el objetivo completo

- Recuperación con nuevos permisos tras un cierre previo vencido y casos
  de proveedor fuera de los recorridos compatibles descritos arriba.
- Contexto de mercado y señales descartadas para investigación futura.
- Piloto Trade TESTNET, habilitación mainnet con controles equivalentes y
  validación de los flujos de fondos reales del usuario.
- Bots DEX delegados e importación de formatos externos cuando se disponga
  de un archivo real de origen.

Estas tareas mantienen abierto el objetivo general de ORBIS.
