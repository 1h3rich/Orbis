# Supervisión y coordinador del bot perpetuo REAL

Estado: verificado y cargado en v110, con [activación API/GUI](GRVT_PERP_BOT_ACTIVATION.md).
Este trabajo no cierra la aceptación del proyecto completo ni acredita un
ensayo con dinero real.

## Contratos y separación

- `grvt-perp-exit-1.0` decide salidas sobre el feed propio de señales y fills
  confirmados. Una decisión CLOSE conserva su evidencia; no crea un fill.
- `grvt-perp-supervision-1.0` y la concesión nueva
  `orbis.grvt.mainnet.bot.grant.v5` registran vigilancia. v4, las concesiones
  v1/v2/v3 y los claims v1/v2/v3 anteriores conservan sus contratos.
- El coordinador nuevo compone `GrvtMainnetBotService`: comparte reserva de
  subcuenta, firmante revocable en RAM, transporte y recuperación nativos,
  observación, conciliación financiera, STOP e historial. El coordinador
  anterior conserva su fuente y su motor de salida.
- El trabajador público es una hebra independiente y real. No consulta capital
  PAPER para decidir entradas, ni construye una cuenta ficticia para REAL.
  Persiste cada observación antes de publicar candidatos.

## Salidas y posición

El stop/objetivo software usa bid para LONG y ask para SHORT. Los umbrales se
comparan como `precio × cantidad` frente al notional de los fills; no requieren
redondear un VWAP periódico. Las reglas e indicadores usan el prefijo completo
del feed y el evaluador LAB compartido, incluidas condiciones anidadas y otras
temporalidades cerradas. La permanencia cuenta cierres posteriores al fill,
incluida la vela parcial de entrada. Sus extremos anteriores al fill no
alimentan el trailing. La prioridad es regla de salida, permanencia, stop o
trailing y objetivo.

La confirmación tardía no se presenta como vigilancia software anterior. Las
protecciones nativas LAST, calculadas desde el límite firmado, siguen siendo
otra correspondencia declarada en los términos de entrada.

La vigilancia privada recopila configuración del instrumento, cuenta y otra
lectura de configuración en una ventana de cinco segundos. Comprueba modo y
apalancamiento efectivos, colateral dedicado USDT, tamaño y dirección propios,
ausencia de posiciones ajenas y metadatos ISOLATED cuando corresponda. La
fecha de una posición sin cambios no se sustituye por la fecha de recogida.
Una posición menor o plana pide volver a observar órdenes; no acredita cierre.
Una posición ajena exige revisión y no se reduce automáticamente.

El límite de pérdida intraposición usa equity actual frente al baseline REAL
del ciclo y resultados ya conciliados. Es un control de parada de una
subcuenta dedicada, no un recibo de PNL ni una garantía de ejecución a ese
precio. Los flujos externos aún no conciliados se declaran en su alcance.
Comisiones de entrada por encima del techo aprobado piden cierre y deshabilitan
nuevas entradas; las comisiones finales se comprueban antes de rearmar. El
techo sigue siendo una estimación del usuario, no una tarifa del exchange
verificada mediante una escala supuesta.

## Diario y concurrencia

Los eventos nuevos son `ENTRY_CONFIRMED`, `POSITION_REVIEW`,
`SUPERVISION_TICK` y `SOURCE_FAULT`. MARKET v5 envuelve el registro original de
señales con su checkpoint de vigilancia. La lectura fría vuelve a calcular las
decisiones; no deserializa un checkpoint como estado ejecutable.

El prefijo RAM comprueba también los bytes originales de claim, fills y
observación enlazados. Cambiar una fila y recalcular su hash no conserva una
caché de supervisión válida. No se hace HTTP dentro de estas transacciones.
Un fallo o respuesta incierta de archivo no reintenta MARKET: se verifica el
registro y se añade, como máximo una vez, un evento conservador de fallo.

La recogida pública usa una cadencia entre dos y cinco segundos y la vigilancia
privada se limita a una cada cinco segundos. Los límites del diario continúan
explícitos: 50.000 MARKET, 20.000 revisiones privadas, 5.000 controles de
frescura y 100.000 eventos totales. Un control de frescura sólo se añade cuando
el feed envejece, no en cada iteración.

El reloj compartido del permiso se comprueba bajo un bloqueo RAM breve. Las
comprobaciones de sesión/bóveda quedan fuera de ese bloqueo para evitar invertir
el orden con escritores de archivo. No se retiene el bloqueo durante HTTP.
STOP revoca el permiso y el productor antes de cualquier espera o escritura.
Una respuesta pública tardía no puede publicar una señal ni confirmar otro
evento. Fallar al arrancar la hebra gestora también revoca y borra el firmante.

## Ciclo integrado

1. Verificar el original LAB/OOS y el prefijo PAPER, y preparar un seed actual
   nuevo al activar. Ninguna señal del calentamiento autoriza entradas.
2. Reservar concesión v5, subcuenta y presupuesto. Arrancar el productor con
   permiso RAM separado y explícito.
3. Seleccionar únicamente una señal futura fresca. Recoger preflight y plan
   REAL, consumir selección y claim durables, y transmitir una sola vez.
4. Observar órdenes propias, confirmar fills y vigilar posición/mercado.
   Un fill nativo concurrente se vuelve a consultar sin inventar una entrada.
5. Cerrar mediante la recuperación nativa ya existente: cancelar órdenes
   propias, comprobar dos veces el residuo y reducir sólo ese residuo dentro
   del presupuesto. Una respuesta perdida se resuelve por identidad original.
6. Conciliar fills, costes, funding, posición plana y saldo; guardar recibo.
   Sólo después puede liberarse el STOP de observación del propio ciclo.
7. Aplicar límites agregados antes de otra entrada; la siguiente señal debe
   cerrar después del recibo anterior. Un fallo de fuente/configuración o
   supervisión requiere nueva revisión y autorización.

La lectura fría, copia, reapertura o concesión ACTIVE guardada no reconstruyen
autoridad, claves ni hebras. STOP y vencimiento tampoco ordenan un cierre
después de revocar permiso: el grupo pendiente conserva su evidencia y las
protecciones que realmente tenga, para recuperación autorizada.

## Verificación y pendientes

Las pruebas usan bóvedas cifradas propias, fuente/evaluador/trabajador y
coordinador reales. Sólo se sustituyen los I/O del proveedor y el reloj.
Cubren LONG/SHORT, CROSS/ISOLATED, paridad con PAPER observado, datos caducados,
configuración y pérdida, dos ciclos, respuesta perdida, STOP con HTTP bloqueado,
fallo al arrancar, cambios rehasheados y reapertura sin arranque automático.
La tanda conjunta dirigida final pasa 143 casos. Se conserva su XML y un
manifiesto de fuentes en `~/.local/state/orbis/verification/` con prefijo
`perp-supervision-dev-`; no es una verificación completa de nueva versión.

La prueba software dispara a 151/49, pero el proveedor controlado ejecuta la
reducción a 100. El recibo contiene `-0.03` de comisiones reales de la muestra,
sin atribuirle el beneficio teórico del disparador. Otra prueba de riesgo
falló al aportar equity incompatible con saldo y PNL abierto; se corrigió la
muestra, manteniendo el rechazo de balances inexplicados.

v110 añade revisión sellada consumible una vez y activación API/GUI con
presupuesto/permisos claros. El recorrido completo de navegador con proveedor
controlado, la regresión general y la carga local están verificados.
El piloto humano mainnet y la aceptación del resto de flujos wallet/MEXC/DEX
siguen siendo trabajo separado pendiente. No se han usado claves ni fondos
del propietario ni se han migrado sus almacenes.
