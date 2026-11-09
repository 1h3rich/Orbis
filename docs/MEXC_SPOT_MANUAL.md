# Compra y venta manual MEXC Spot

Estado: recorrido UI→API→transporte→observación→libro implementado. Verificado
con MEXC controlado, sin credenciales ni fondos reales. El primer ensayo con
una cuenta real sigue pendiente. Los límites manuales están disponibles desde v138.

## Guía Wallet ↔ MEXC

Los accesos de la cartera REAL muestran la red, el control de la wallet y la consulta de USDT de BNB Chain. «Clave View» abre la conexión o el vínculo según la sesión. «Ingresar» lleva a la consulta de USDT cuando falta esa lectura. «Orden Spot», «Bot» y «Retirar» conservan sus controles y confirmaciones.

La API de la guía consulta sólo la sesión y los registros cifrados de la cuenta seleccionada. No renueva el plazo de desbloqueo ni consulta MEXC. El vínculo mostrado es local. Cada operación sigue comprobando su identidad, saldo, límites y condiciones propias al revisarla. El estado local de órdenes no sustituye la conciliación del exchange.

### Órdenes pendientes

Desde **Orden Spot** o **Bot** en la guía **Wallet ↔ MEXC**, abre el diario de la cuenta cuando veas **Revisa la reserva**, **Orden por sincronizar** o **Sincroniza la orden**. Tras reabrir la bóveda, el diario se lee sin conectar View.

**Liberar reserva** se ofrece para una intención cuyo envío nunca empezó. Si el envío pudo llegar a MEXC, usa **Sincronizar orden** con la clave View vinculada. La revisión puede registrar fills y cerrar una reserva tras comprobar el saldo. Una orden abierta conserva su estado hasta la siguiente sincronización o cancelación. Para cancelar el resto abierto se requiere STOP y Trade. El historial completo conserva las órdenes ya resueltas.

### Ejecuciones parciales

En **Orden Spot**, pulsa **Revisar orden pendiente** para ir al diario. Si hay un único pendiente de la cuenta, ORBIS lleva el foco a esa orden. Las cantidades solicitadas, ejecutadas y abiertas proceden del pedido original y de los fills de MEXC archivados. La consulta local conserva la hora de la observación y aplica el control de privacidad. Cuando falta evidencia suficiente, las cantidades quedan sin mostrar.

Tras detener la cuenta, **Cancelar orden** solicita cancelar el resto abierto. Al comprobar el estado final, ORBIS contabiliza los fills observados una vez y reconfirma el saldo para cerrar la reserva. El diario conserva las unidades ejecutadas y las canceladas. Con una cancelación sin fills, se cierra la reserva sin crear un asiento de compra o venta. [Estados oficiales de MEXC](https://www.mexc.com/api-docs/spot-v3/public-api-definitions#order-status).

### View renovada

Conecta la clave nueva en **MEXC View** y selecciona el libro donde se envió la orden. Abre **Wallet ↔ MEXC → Orden Spot** y pulsa **Recuperar con View renovada**. La coincidencia debe ser inequívoca con la huella del UID archivado. La clave necesita permisos de lectura de cuenta y órdenes. Si la orden sigue abierta, queda pendiente. Si terminó, ORBIS puede asentar los fills y cerrar la reserva cuando cuadren el estado y el saldo.

Para un ingreso ya enviado, usa **Comprobar ingreso** o **Comprobar en BNB** tras conectar la clave nueva de la cuenta original. En una retirada ya solicitada, **Comprobar recepción**, **Consultar en BNB** y **Contabilizar retirada** también aceptan la View renovada. Se mantienen el cotejo del recibo, la comprobación del saldo y la confirmación del asiento. [Ingresos](WALLET_FUNDING_BNB.md#comprobar-el-ingreso-desde-orbis) · [Retiradas](MEXC_WITHDRAWAL_BNB.md#consulta-desde-orbis).

### Renovar vínculo View

Conecta la clave nueva y selecciona el libro MEXC original. Abre **Wallet ↔ MEXC → Clave View → Renovar vínculo View**. El botón aparece cuando la clave corresponde inequívocamente al UID del libro. Al confirmar, ORBIS conserva el historial, pausa sus bots y aplica STOP. La bóveda cifrada guarda sólo una huella. Tras desconectar o bloquear, introduce de nuevo la clave y su secreto. ORBIS verifica el UID y recupera el libro original en modo de lectura. La clave original queda fuera del vínculo operativo. Otra renovación vuelve a aplicar STOP.

Revisa las órdenes o retiradas pendientes, verifica Trade y abre **Orden Spot → Reabrir operativa** o **Bot → Reanudar**. Ambas acciones requieren revisar el saldo y los límites vigentes. Las credenciales se mantienen en memoria.

### Red de la wallet

Si la wallet conectada está en otra red, pulsa «Cambiar a BNB Chain» y revisa la solicitud en la extensión. Si la extensión indica que falta la red, pulsa «Añadir BNB Chain» para solicitar su alta. ORBIS vuelve a consultar la red activa. Si el alta conserva la red anterior, pulsa «Cambiar a BNB Chain» de nuevo. Al confirmar la red, verifica el control de la wallet en esa sesión. [Cambio de red](https://eips.ethereum.org/EIPS/eip-3326), [alta de red](https://eips.ethereum.org/EIPS/eip-3085) y [configuración oficial de BNB Chain](https://docs.bnbchain.org/bnb-smart-chain/developers/wallet-configuration/).

### Saldo que ya está en MEXC

Conecta View y selecciona un libro REAL vacío. En Orden Spot, abre «Registrar saldo inicial», lee los saldos y declara el coste por unidad de los activos que lo requieran. Revisa y confirma la vista previa. El efectivo queda como aportación y los activos como inventario con coste declarado. Para operar, verifica Trade, configura límites y revisa la apertura de la operativa por separado.

Si se pierde la respuesta, «Reintentar registro» envía los mismos datos. Durante la misma sesión y conexión View, ORBIS conserva un recibo local en memoria de las últimas confirmaciones. El libro cifrado conserva los asientos al bloquear y reiniciar. Tras cambiar de cuenta o cerrar sesión, consulta el historial para comprobar el registro.

### Ingresos hechos fuera de ORBIS

En MEXC View, pulsa **Consultar y guardar** y abre **Registrar ingresos USDT**. Elige los ingresos acreditados y pulsa **Revisar ingresos**. La suma debe coincidir con la diferencia en USDT. Los demás saldos deben cuadrar y los fondos deben estar libres. Confirma **Fondos fuera de ORBIS** sólo si el importe no figura en otra cuenta del libro. Para fondos registrados en otra cuenta, usa un traslado.

La confirmación consulta de nuevo el historial View y ambos saldos. Conserva la fecha, las referencias seudónimas y los asientos en la bóveda cifrada. Si se pierde la respuesta, **Reintentar registro** recupera el mismo asiento. Para operar con el saldo nuevo, revisa la apertura de Orden Spot o del bot.

## Uso

1. En Cartera real, desbloquear la bóveda y seleccionar la cuenta MEXC en USDT.
2. Conectar View y confirmar su vínculo. Registrar el saldo existente mediante
   inventario inicial, o revisar los movimientos del libro hasta que las
   cantidades coincidan. El coste de activos anteriores lo declara el usuario.
3. En «Orden Spot», abrir «Configurar límites manuales». Introducir el par,
   máximos de orden y volumen diario, exposición, pérdida, comisión y
   deslizamiento. Guardar selecciona el borrador y muestra sus valores.
   Los límites manuales sirven exclusivamente para órdenes confirmadas.
   Para un bot se configuran los límites de su estrategia por separado.
4. Verificar una clave Trade separada de la misma cuenta. La clave admite sólo
   permisos Spot de lectura/trading; no se acepta permiso de retirada en ella.
5. Abrir «Operar MEXC · REAL», elegir los límites y pulsar «Revisar saldo».
   Si las unidades coinciden, se ofrece «Habilitar operativa» para confirmar
   el uso exclusivo de la cuenta. La apertura comprueba también la actividad
   y los límites antes de guardar la política activa.
6. Elegir comprar o vender, cantidad base y precio límite. Revisar y confirmar
   los datos mostrados. ORBIS comprueba la cuenta, ensaya la orden en el endpoint
   de prueba y vuelve a leerla antes del único POST negociable.
7. Si MEXC comunica ejecución final, se asientan sus fills y se reconfirma el
   saldo antes de cerrar la reserva. Una orden abierta se sigue desde «Órdenes
   MEXC» con «Sincronizar orden»; una orden LIMIT no garantiza ejecución inmediata.

8. Para cancelar el resto abierto, usa «Detener cuenta» y abre «Órdenes y
   conciliación REAL». Si la orden aún no está observada, pulsa «Sincronizar orden».
   En la orden abierta seleccionada, pulsa «Cancelar orden» y confirma. El permiso
   Spot Trade permite enviar una cancelación dirigida. Los fills ya ejecutados se
   conservan. Si el resultado es incierto, sincroniza la orden para confirmar su
   estado y el saldo antes de retirar. [API de cancelación de MEXC](https://www.mexc.com/api-docs/spot-v3/spot-account-trade/cancel-order).

La conexión View y la clave Trade se borran de memoria al bloquear. Reabrir
la bóveda conserva políticas, órdenes y asientos, pero exige volver a conectar
las credenciales. No se envían ni se guardan secretos en el chat.

## Revisar saldo antes de habilitar

Con MEXC View vinculada, abre **Orden Spot**, elige los límites y pulsa **Revisar saldo**. Si el libro y las dos lecturas de MEXC coinciden, ORBIS guarda la comprobación en la bóveda. Con Trade verificada ofrece **Habilitar operativa** o **Reabrir operativa**. Cada una pide confirmación y vuelve a verificar la actividad y los límites antes de guardar la política. La autorización de una nueva operativa requiere esa confirmación.

Cuando hay diferencias, el panel muestra las cantidades del libro y de MEXC. **Buscar ingreso MEXC** lleva a la consulta de transferencias si la única diferencia es un saldo superior de USDT en MEXC. El registro del ingreso exige comprobación y confirmación propias. Después, vuelve a revisar el saldo. Si cambias de cuenta, límites o caduca la comprobación, repite la revisión.

## Revisar orden

En **Orden Spot**, selecciona la operativa activa, el lado, la cantidad y el precio límite. Pulsa **Revisar orden**. ORBIS consulta el saldo, la actividad, la identidad View y Trade y las reglas del instrumento. La comisión estimada usa la tasa máxima admitida multiplicada por el importe de la orden. Si cambias un campo, la revisión anterior se retira.

La revisión es informativa y de sólo lectura. No crea una reserva ni envía una orden al endpoint de prueba o al negociable. **Enviar orden real** requiere confirmación y repite las comprobaciones, ya que los datos pueden cambiar.

### Calcular por importe

En **Orden Spot**, elige compra o venta, introduce un importe en la divisa del par y pulsa **Calcular orden**. En compra, el presupuesto incluye la comisión máxima estimada. En venta, el importe se refiere al bruto calculado al precio límite y la revisión muestra el neto estimado. ORBIS propone cantidad y precio si pasan las comprobaciones habituales.

El presupuesto de una compra calculada se vuelve a comprobar con la comisión vigente al enviar. Editar a mano la cantidad o el precio pasa a la orden manual. El cálculo no reserva fondos. **Enviar orden real** conserva la confirmación propia. Una orden LIMIT puede quedar abierta si el mercado se mueve antes de ejecutarla.

### Calcular el máximo vendible

En **Orden Spot → Vender**, pulsa **Máximo vendible**. ORBIS usa el mejor precio de compra observado como referencia del límite, ajusta la cantidad al incremento y a la precisión del par y aplica los topes por orden. Descuenta reservas y deja margen para una posible comisión en el activo vendido. La revisión muestra el saldo disponible, el neto estimado si la comisión se cobra en la divisa del par y las unidades fuera de la orden. Los límites de una sesión anterior se comprueban con su política archivada. [Reglas del instrumento en MEXC](https://www.mexc.com/api-docs/spot-v3/market-data-endpoints/exchange-information) · [Cotización bid/ask](https://www.mexc.com/api-docs/spot-v3/market-data-endpoints/symbol-order-book-ticker).

El cálculo sólo rellena el formulario cuando la propuesta pasa las comprobaciones. **Enviar orden real** pide confirmación y revalida los términos. La orden puede quedar abierta o ejecutarse parcialmente si cambia la liquidez. El resto queda disponible según el resultado conciliado.

## Contrato de la apertura

La política activa referencia `RealMexcParityObservation` en su JSON cifrado;
su introducción no necesitó cambiar el esquema v24. El núcleo de bots añade
después dos tablas independientes en v25. Los campos nuevos opcionales se
omiten de la representación canónica cuando tienen su valor inicial, para
conservar las huellas de políticas antiguas.

- Duración máxima: 24 horas desde la apertura, o la caducidad del borrador si
  llega antes. Luego debe revisarse otra apertura.
- Un par Spot por política. La cuenta puede conservar otros activos registrados.
  Todas las cantidades deben coincidir con MEXC, con fondos libres y sin órdenes
  abiertas observadas. La compra usa el efectivo del par y la venta usa su
  inventario en la divisa elegida. Opera en Spot con unidades disponibles.
- Si queda MX fuera del par, View debe confirmar que la opción de pagar
  comisiones con MX está desactivada. ORBIS consulta el ajuste durante la revisión
  y antes del envío, y el usuario puede cambiarlo en MEXC.
  [Consulta de MEXC](https://www.mexc.com/api-docs/spot-v3/spot-account-trade/query-mx-deduct-status).
- El prefijo del libro anterior a la apertura debe conservar exactamente su
  huella. Cada evento posterior debe corresponder a un fill propio íntegro,
  asentado y con reserva cerrada.
- Las ventanas completas de fills y depósitos/retiros deben cubrir la apertura
  y llegar a una observación reciente. Un traslado posterior o uno anterior
  sin estado completado bloquea la sesión hasta revisar su apertura.
- Las dos lecturas de cuenta deben ser estables y la identidad View conocida.
  Justo antes del POST se vuelven a comprobar View y Trade. El libro no puede
  cambiar durante el barrido y sus cantidades deben coincidir con MEXC.
- Una reserva enviada o incierta impide otra orden en esa cuenta. La segunda
  comprobación excluye sólo la reserva aún no enviada de la misma petición.

Este contrato cubre un punto de partida revisado y su actividad posterior;
no reconstruye el pasado completo ni excluye movimientos entre lecturas
externas. Requiere cuenta dedicada mientras se usa la sesión. Una consulta
de órdenes sin par se usa como detector adicional de conflictos, no como
prueba de exhaustividad.

## Límites y recuperación

El límite de pérdidas incluye P/L realizado diario y pérdida latente al bid
respecto al coste registrado. Las políticas nuevas permiten ventas Spot que
reducen inventario aunque se haya agotado el presupuesto de nuevas compras:
siguen exigiendo unidades disponibles, comisión, precio, identidad y datos
válidos. La parada persistente bloquea todos los envíos.

Cada pedido usa un UUID estable. Repetir el mismo ID devuelve el estado local;
no repite el POST, tampoco después de un timeout. Si cambia cantidad, precio,
dirección o política con el mismo ID, se rechaza. Tras una ejecución confirmada,
la interfaz vacía la cantidad antes de permitir preparar otra orden.

Si el fallo ocurrió antes de iniciar el envío, «Órdenes MEXC» ofrece
«Liberar reserva». La acción sólo acepta una reserva inequívocamente no
enviada y no observada en MEXC. Conserva el pedido original como abandonado;
su identificador no puede usarse para enviarlo después. La liberación compite
con la primera marca durable de envío bajo la misma transacción: sólo una de
las dos acciones puede ganar. Una orden enviada o incierta nunca se libera
por ausencia de respuesta; debe sincronizarse.

«Detener cuenta» persiste la parada y libera las reservas aún no enviadas,
sin afirmar que las órdenes externas se hayan cancelado. Para reabrir se
seleccionan límites y se confirma «Reabrir operativa». ORBIS exige cero
intenciones pendientes y una apertura de saldo posterior a la parada; revisa
inventario, actividad e identidad mientras STOP sigue activo. Guardar una
política nueva y levantar la parada comparten una transacción. Una parada
adicional o cambio del libro durante la revisión impide reabrir. Las políticas
anteriores permanecen inhabilitadas incluso en las puertas internas de envío.
La reapertura no envía órdenes ni arranca un bot.

El seguimiento de órdenes antiguas está acotado por la cobertura del exchange.
Las acciones locales se serializan con las operaciones ya en curso: STOP no
revoca un POST iniciado ni constituye un botón de liquidación de posiciones.

El [bot nativo REAL](BOT_MEXC_SPOT.md) tiene una autorización y un rearme
separados de este recorrido manual. El [ingreso USDT/BEP-20](WALLET_FUNDING_BNB.md)
ya prepara un plan y solicita confirmación en la extensión, sin acreditar el
libro por recibir un hash. El cotejo de recibo observado/abono View y asiento
explícito están implementados con proveedores de prueba. La [retirada USDT/BSC](MEXC_WITHDRAWAL_BNB.md)
tiene su autorización puntual, recuperación View, recibo observado y asiento
de neto/comisión; el ensayo humano mainnet sigue pendiente. Los ensayos LAB/PAPER no activan
automáticamente ninguna de esas operaciones.

Referencias verificadas: [nueva orden y permiso Spot de escritura](https://www.mexc.com/api-docs/spot-v3/spot-account-trade/new-order),
[órdenes abiertas](https://www.mexc.com/api-docs/spot-v3/spot-account-trade/current-open-orders),
[ejecuciones](https://www.mexc.com/api-docs/spot-v3/spot-account-trade/account-trade-list).
