# Rearme DEX: continuidad del mismo bot

Estado: v74 cargada en el servidor local. El rearme y la renovación de política
están implementados y verificados en API y navegador de pruebas, con EVM propia.
Complementa los permisos v72; el piloto humano mainnet sigue pendiente.
Renovar tiempo no equivale a asignar nuevo capital.

## Datos que deben continuar

La identidad es el vínculo original con la estrategia y versión probadas, el
ejecutor y la wallet propietaria. Cada permiso de ejecución es una época nueva,
con su ID inmutable y referencia al cierre anterior. No se copian capital o
posiciones PAPER ni se adopta inventario ajeno.

Se conservan unidades propias, coste restante, compras acumuladas, entradas,
pérdidas realizadas acumuladas, WBNB adquirido acumulado, última entrada, gas
gastado/reservado, vela de entrada, máximo observado para trailing y motivo de
cierre parcial. Un beneficio o una venta no repone cuotas de compra ni pérdidas.

La recuperación registra el intervalo sin observación. Los históricos descargados
reponen indicadores; no crean operaciones retrospectivas ni entradas para una
señal ya pasada. El máximo de trailing ya observado no se reduce. Los precios
durante la desconexión no se presentan como observados en directo.

## Dos transiciones necesarias

1. **Misma política vigente.** Un permiso nuevo puede retomar el vínculo después
   de parar y conciliar el anterior. Se reutiliza la concesión de gas existente,
   que ya descuenta gastos y reservas. No se llama a `configure` y sus contadores
   siguen siendo los actuales. Cambiar sólo el plazo no cambia los límites.
2. **Política nueva.** Cuando la anterior ha caducado o se ha detenido en cadena,
   el propietario confirma una nueva configuración. El contrato conserva posición
   y coste, pero reinicia gasto, entradas, pérdidas, última entrada y cuota
   adquirida. La nueva configuración debe reflejar los remanentes del presupuesto
   original, y el diario debe demostrar la transición y su recibo finalizado.

Para la segunda transición, las cuotas se calculan con enteros:

«Original» significa la política y concesión realmente autorizadas al vincular
el bot, que pueden ser más restrictivas que la revisión LAB/PAPER. Renovar no
vuelve a usar el techo mayor de aquella revisión como presupuesto nuevo.

- Presupuesto nuevo = presupuesto original − compras acumuladas.
- Entradas nuevas = máximo original − entradas acumuladas.
- Pérdida permitida nueva = límite original − pérdidas acumuladas.
- Cuota WBNB nueva = posición actual + cuota original − compras WBNB acumuladas.
- Gas disponible nuevo = presupuesto original − gasto y reservas de todas las épocas.

Ejemplo: presupuesto 100 USDT y gasto 30 deja 70; cuota de 1 WBNB, adquisiciones
de 0,4 y posición actual de 0,1 permiten una cuota de política nueva de 0,7 WBNB.
El contrato comienza con 0,1 adquirido, por lo que sólo quedan 0,6 para comprar.
El límite por orden se reduce si el remanente es menor. La exposición y el límite
de salida se mantienen; la espera entre compras conserva la última entrada real,
aunque el contador en cadena haya vuelto a cero.

Si se agota un límite de compras y queda posición, sólo se permite configurar
cierre. Si no queda posición ni presupuesto, no se crea una política vacía ni se
simula un bot activo. Aumentar presupuesto necesita una asignación nueva y
explícita, diferenciada de un rearme.

## Confirmación y concurrencia

Antes de revisar: parar el trabajador anterior y retirar la firma; todos los
intentos deben tener resultado final verificado o descarte local demostrado.
Un nonce consumido o un timeout no prueban que no se haya ejecutado. Una operación
incierta bloquea el rearme hasta recuperar su recibo original.

La revisión une una cabeza causal comprobada, recibos y posición propia con el
estado finalizado actual del mismo ejecutor. Política, nonce, posición, coste y
límites deben coincidir. Una posición retirada manualmente o un cambio ajeno se
presentan como conflicto de asignación, sin corregir el inventario silenciosamente.

El commit confirma atómicamente la época anterior, la nueva autorización y el
cursor inicial. La generación de bóveda y cancelación se revalidan después de
cada lectura de red. STOP puede cancelar incluso antes del POST original y no
espera red. Un replay recupera la misma revisión, sin nueva firma, presupuesto
o trabajador; el callback de una época anterior no revoca una firma posterior.

El formato siguiente debe versionar sus términos; v1/v2 y sus copias continúan
reproduciéndose con la semántica publicada. Los ancestros se verifican con un
linaje plano acotado para evitar una recursión creciente entre permisos.

## Evidencia de aceptación

- Compra, STOP, reapertura, rearme, cierre: misma posición y coste real, sin compra
  repetida en la vela de recuperación y sin reiniciar máximos de trailing.
- Dos cambios de política: presupuesto, pérdidas, cuotas y gas acumulados nunca
  se reponen; sólo cierre al agotar entradas; no se reabre un permiso agotado.
- Cierre parcial e intervalo sin observación: conserva la procedencia y continúa
  únicamente las salidas que corresponden al estado actual.
- Intento desconocido, recibo alterado, inventario insuficiente o cambio de
  estrategia: revisión rechazada sin firma ni envío.
- Respuesta perdida y dos pestañas: una única época nueva y un único trabajador.
- STOP durante la revisión o bloqueo durante RPC: no hay arranque tardío.
- Copia/restauración: mismo linaje, importes y resultados, sin autorun.
- Recorrido de navegador en EVM propia, sin fondos ni credenciales del usuario.

## Integración v73 con política vigente

`POST /real/accounts/{account_id}/dex-bots/runs/{previous_id}/rearm` requiere
otro ID de solicitud, prueba EOA, copia de operadora verificada, nuevo modo de
permiso explícito y `REARMAR_BOT_DEX`. El mismo ID recupera su estado sin devolver
claim o iniciar otro trabajador. No llama a `configure`, repone gas ni cambia
límites de la política o de la concesión existente.

Los términos `orbis.dex.bot.long.v3` conservan el permiso anterior, sus huellas de
definición/estado, recibos finales, cursor inicial e intervalo sin observación.
Los ancestros se verifican en orden mediante un recorrido iterativo, no llamadas
recursivas. La posición inicial se reconstruye de los recibos propios y debe
coincidir exactamente con el bloque finalizado actual. Un cambio rehasheado de
trailing o gasto se rechaza también al verificar una copia.

Cada permiso proyecta sus propios recibos. Las tarjetas anteriores muestran
«Histórico» para distinguirlas de la posición del último permiso. La interfaz
sólo ofrece «Rearmar» cuando el backend anuncia que soporta la ruta; un servidor
anterior conserva sus controles deshabilitados.

Pendiente: política nueva con cuotas remanentes, DCA y optimización de lectura
de linajes largos. El recorrido iterativo evita el límite de recursión, pero la
caché actual de dos prefijos puede reproducir semánticamente ancestros de nuevo
cuando hay más épocas; todavía no se acredita rendimiento sostenido con cientos
de rearmes. La conservación financiera no depende de esa futura optimización.

## Integración v74 de una política nueva

Las entidades actuales imponen límites útiles: un vínculo v1 guarda un grant
inmutable y exige posición inicial vacía; LONG v3 retoma ese mismo vínculo y
grant. La política nueva no cabe en esas condiciones y necesita otro formato.
La integración completa siguiente aún no está habilitada.

La preparación/confirmación del control v3, el vínculo sucesor v2, la continuación
LONG v4 y la interfaz están integrados y probados con recibos de EVM propia.

- Crear un vínculo sucesor que conserve la identidad del vínculo raíz, el origen
  probado y la referencia al último permiso detenido. No modificar la procedencia
  ni los hashes de vínculos o permisos existentes. El origen ya guardado no se
  presenta como una observación PAPER nueva sólo por renovar la política.
- Obtener las cuotas originales del grant raíz realmente autorizado y reconstruir
  los consumos acumulados de sus recibos y de todos sus sucesores. Los límites
  remanentes son independientes del reinicio de contadores del contrato.
- Reutilizar el diario de controles del propietario para preparar y confirmar
  `CONFIGURE`. El recibo final debe acreditar el mismo ejecutor, wallet, política
  anterior y límites nuevos derivados de esa revisión. Una observación de estado
  parecido o un nonce consumido no sustituyen al recibo. La preparación no firma,
  envía, financia ni activa por sí sola.
- Confirmar el vínculo/grant sucesor únicamente después del recibo y una lectura
  finalizada actual. En el mismo commit, retirar la asignación anterior, guardar
  la continuidad financiera y registrar la política nueva. Sólo después se
  permite solicitar una autorización de ejecución explícita.

Los contadores físicos después de `configure` deben verificarse como tales:
política incrementada, nonce del ejecutor cero, gasto/entradas/pérdidas/última
entrada cero, cuota adquirida igual a la posición conservada. No se sobreescribe
esa evidencia con sumas del pasado. El gasto y riesgo de toda la vida del bot
son una proyección aparte que determina las cuotas nuevas.

La decisión del bot debe conservar además la última entrada real para respetar
su espera entre compras. Ese reloj efectivo puede diferir del contador físico
cero de la nueva política; ambos necesitan procedencia y replay versionado.
La vela de entrada, trailing y cierre parcial continúan desde el permiso previo.
LONG v4 distingue esta transición de v3 sin ampliar silenciosamente la semántica
de los formatos anteriores.

Si hubo un STOP en cadena entre ambas políticas, también se comprueba su origen
y la revisión adicional de política. Los recibos inciertos, una asignación
posterior distinta o cambios en posición/coste invalidan la revisión antes de
crear otro grant. La pérdida de una respuesta sólo recupera el mismo intento.

Prueba vertical verificada: comprar y vender parcialmente con pérdida; detener;
preparar límites remanentes; confirmar la nueva política en EVM propia; retomar
la posición restante y cerrarla. Comprobar coste, pérdidas, compras, cuota WBNB,
gas total y espera entre compras en dos renovaciones, además de copia/restauración
y cancelación concurrente. Evidencia en `v74-20261104-integration-r2/verification.json`
bajo `/home/rich/.local/state/orbis/verification`.
