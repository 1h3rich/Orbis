# Núcleo LIMIT REAL para GRVT

Disponible desde v121 en el servidor local. LIMIT también se conserva en LAB y
PAPER. Este núcleo prepara y verifica una entrada independiente con la API de
GRVT. El transporte, el diario cifrado y la recuperación de la orden están
conectados. El panel REAL ofrece revisión, activación, historial
y reanudación de un intento que quedó pendiente.

La preparación reproduce la sesión cifrada de PAPER y su validación LAB.
Exige una operación observada cerrada, sesión detenida, cuenta ficticia plana
y ausencia de cierres estimados. El plan conserva las huellas de la sesión y
del perfil. Una fuente independiente vuelve a calentar las reglas y genera una
señal a partir de una vela cerrada nueva. El cálculo reproduce esa señal antes
de fijar el límite.

El preflight lee dos veces el saldo, las posiciones, las órdenes abiertas,
el modo de margen, el apalancamiento y las reglas del instrumento. El plan usa
un presupuesto REAL independiente y redondea el precio al tick. El vencimiento
firmado es el primero entre el final de las velas de vigencia, el máximo
autorizado de espera y el plazo de entrada.

La entrada se expresa como una orden GTT independiente. GRVT admite fills
parciales en GTT. Las protecciones vinculadas de tipo OTOCO se activan cuando
la entrada se completa. El supervisor concilia cada
fill antes de proteger o reducir la exposición acumulada.
[Órdenes y agrupaciones de GRVT](https://api-docs.grvt.io/schemas/api_bulk_orders_request/)

El lector verifica la firma, dos estados de la orden y todas las páginas de
fills del intervalo. El vencimiento local conserva la espera hasta que GRVT
acredita un estado terminal. Una cancelación con fills parciales conserva la
cantidad y el coste reales. En un SHORT, el precio límite es un mínimo.
Una ejecución a un precio superior puede sobrepasar la previsión en USDT.
La conciliación conserva esa ejecución y señala los topes superados.
[Estados de orden](https://api-docs.grvt.io/schemas/order_state/)
[Vigencia de la firma](https://api-docs.grvt.io/schemas/signature/)

## Diario, envío y cancelación

La migración aditiva a esquema 80 añade el intento, la reserva de subcuenta,
la cadena de eventos y los identificadores de reducción. El intento conserva la huella del contrato firmado,
el identificador, los originales del preflight y la señal reproducible. La
reserva comparte el control STOP con la operativa GRVT de mercado. Una
transacción confirma la reserva antes de transmitir. Una segunda confirma el
inicio del envío y consume ese intento de forma durable.

La conexión usa una autorización revocable en memoria. El diario permite
recuperar el intento sin restaurar ese permiso. Si se pierde la respuesta, la
consulta autentica la misma subcuenta y reconstruye el contrato a partir del
eco de GRVT. Se comprueban la firma, el identificador y los fills. El acuse de
envío acredita recepción. La conciliación posterior acredita ejecución.

Desde v122, si la consulta directa devuelve el error 1004 de GRVT, el lector busca una
coincidencia en el historial de órdenes de la misma subcuenta, mercado y
ventana firmada. Recorre hasta cuatro páginas de 500 órdenes y exige una
coincidencia única por identificador. La orden recuperada pasa las mismas
comprobaciones de firma, estado y fills que la consulta directa. Se aplica
tanto a la entrada GTT como a las reducciones IOC. Si el historial no
acredita la orden, el diario conserva el intento y su reserva.
[Consulta e historial de órdenes de GRVT](https://api-docs.grvt.io/trading_api/#order-history)

La cancelación registra el intento antes de llamar a GRVT por el ID de orden
confirmado. La lectura posterior conserva los fills que entraron durante el
cruce. Una orden terminal con cero fills libera la subcuenta tras dos lecturas
coherentes que acreditan cuenta plana, sin órdenes y con la misma caja.
El identificador y la vela de señal quedan consumidos en el diario. Una orden
con fills conserva la reserva hasta conciliar y gestionar su exposición.
[Cancelación de GRVT](https://api-docs.grvt.io/schemas/api_cancel_order_request/)

## Reducción y conciliación tras reapertura

Tras el estado terminal de entrada, el servicio compara sus fills con el
tamaño y el signo de la posición, las órdenes abiertas y la caja de la
subcuenta dedicada. Lee todos los historiales desde el preflight, vincula la
financiación a fills reconocidos y conserva comisiones y PNL en Decimal.
Requiere dos auditorías coherentes separadas por al menos cinco segundos.

Una autorización explícita independiente habilita una orden IOC reduce-only.
El contrato aplica el tope de USDT, el lote del instrumento y un precio
acotado por la horquilla y el deslizamiento autorizados. La cantidad nunca
supera el saldo de posición verificado. Una ejecución favorable puede
elevar el nocional de venta efectivo. El diario conserva y señala esa
diferencia, así como comisiones superiores al tope y fills tardíos.
[Vigencia IOC](https://api-docs.grvt.io/schemas/time_in_force/)

El identificador y la huella se confirman en la bóveda antes de transmitir.
Si falta el acuse, el servicio consulta ese identificador y verifica el eco
firmado, su estado terminal y sus fills. No se reenvía el identificador.
Una nueva reducción requiere otras dos auditorías, conserva los fills
confirmados en el prefijo y ajusta la cantidad al residuo. Solo se libera
la reserva cuando las lecturas prueban que la posición está plana y el
balance final cuadra con nocionales, comisiones y financiación. El diario
puede reproducirse sin red al reabrir la bóveda.
[Estado y cantidades de la orden](https://api-docs.grvt.io/schemas/order_state/)

Los recorridos de desarrollo ejercitan largos y cortos con dos reducciones,
respuesta perdida, bóveda cifrada y reconstrucción exacta del balance.

## Supervisión del intento reservado

El supervisor toma un intento previamente autorizado y confirma su exposición
con GRVT. Si la orden GTT sigue abierta tras el primer fill confirmado,
solicita cancelar el resto. Espera la confirmación terminal y considera todos
los fills, incluidos los que llegaron durante la cancelación.

Para la posición confirmada reutiliza las reglas de salida del perpetuo. El
stop, el trailing, el objetivo, el límite de permanencia y las reglas sobre
velas cerradas toman como base la cantidad y el nocional ejecutados. Un hueco
de datos, una cotización que envejece o un incumplimiento de los límites
de entrada deja una decisión de salida registrada. La reducción automática
conserva en el diario el enlace a esa decisión y exige dos auditorías
financieras concordantes. Un acuse perdido deja el supervisor en verificación.
El siguiente ciclo consulta el identificador registrado.

La autorización de supervisión se acota a un intento, una sesión de la
bóveda, un plazo y un máximo de reducciones. STOP revoca los envíos
automáticos. Cualquier exposición pendiente queda visible para revisión y
cierre con permiso separado. La reapertura reproduce las decisiones y
requiere una autorización explícita nueva para continuar.

## Revisión y trabajador continuo

La API privada `/real/exchanges/grvt/mainnet/bots/perp/limit` lista las pruebas
PAPER LIMIT elegibles. La revisión vuelve a comprobar LAB, PAPER, la subcuenta
dedicada y su saldo, margen y apalancamiento. El usuario confirma un resumen
sellado de un solo uso. La confirmación autoriza una entrada futura y sus
reducciones dentro de los plazos e importes revisados.

El trabajador calienta las reglas con datos actuales. A partir de ahí espera
una vela cerrada nueva y conserva la señal y el preflight que habilitaron la
orden. Concluye si vence la espera, y pasa al supervisor si GRVT admite la
entrada o el envío queda registrado con un acuse incierto. Puede volver a
consultar un identificador durante una demora de lectura, con un máximo de
cuatro intentos consecutivos. Cualquier nuevo envío exige sus propias
comprobaciones y se registra antes de transmitir.

El permiso vive en memoria y depende de la conexión firmante y la sesión
REAL. Proteger la vista, bloquear la bóveda, desconectar o pulsar STOP
revocan el trabajador. El historial devuelve los intentos y los resultados
verificados al reabrir.

## Panel REAL y reanudación

En Cartera REAL, abre Operar GRVT y Bot LIMIT. Con la subcuenta y su firma
conectadas, carga una prueba PAPER, ajusta importes y plazos y revisa la
autorización. Un cambio en el formulario descarta la revisión. El panel
consulta el historial si se pierde la respuesta de activación.

En un intento interrumpido, Retomar intento abre otra revisión de un solo
uso. Vincula la autorización al identificador, la firma dedicada, el hash
de la entrada y la última cabecera del diario. Utiliza los límites de
importe de la operación original y pide de nuevo la vigencia y el máximo
total de reducciones. El trabajador reanudado consulta y gestiona la
operación existente. Su permiso cubre ese intento exclusivamente. Si STOP
sigue activo, la subcuenta requiere rearme explícito.

Ver gráfico sitúa las ejecuciones confirmadas a su precio y hora en el
gráfico REAL. Las velas son datos públicos de GRVT. El gráfico conserva
los fills ya verificados mientras la siguiente reducción está pendiente
de comprobación.

Liberar reserva cierra un intento local si el diario demuestra que el
envío nunca se inició. Conserva el registro y el identificador. Una orden
con envío iniciado sigue el flujo de consulta y reanudación.

La versión se probó con un exchange controlado. Quedan el piloto con una
cuenta real y el resto de la aceptación del proyecto.

## Recuperar una autorización LIMIT

Antes de confirmar, revisa **Subcuenta** y los límites. Si el servidor devuelve un rechazo explícito, puedes preparar otra revisión. Cuando se pierde la respuesta, **Consultar historial** comprueba la revisión exacta del trabajador en memoria. Al reanudar se conserva el identificador del intento y se exige otro identificador de revisión. Una fila del historial cifrado acredita el intento, mientras que el trabajador acredita la autorización recuperada en ese proceso.

Si la revisión sigue sin confirmar, el panel mantiene el bloqueo de nuevos envíos. La parada de subcuenta sigue disponible y revoca el permiso local. Tras reiniciar, revisa los intentos guardados antes de emitir una autorización nueva.
