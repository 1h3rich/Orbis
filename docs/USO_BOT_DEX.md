# Bot DEX: LONG y DCA

Estado de entrega: consulta `~/.local/state/orbis/verification/latest.json`.
La revisión v93, incluida la renovación tras recuperar operaciones, está verificada
y cargada en el servidor local.
Recarga la página para obtener los controles nuevos.

v81 incorporó cancelación de devoluciones BNB pendientes; v82 añade cancelación
de financiación desde la wallet. Detalles y límites:
[devolución](DEX_NATIVE_CANCELLATION_PLAN.md) y
[financiación](DEX_WALLET_CANCELLATION_PLAN.md).

El bot usa las reglas de una estrategia probada en LAB y PAPER. Lee BNB/USDT de
MEXC como referencia y ejecuta USDT/WBNB en Uniswap sobre BNB Chain. Evalúa el
histórico y las condiciones completas de la estrategia; no compara únicamente
dos velas parecidas.

## Preparación

1. Prueba una estrategia LONG BNB/USDT en Laboratorio, guarda su validación fuera
   de muestra y síguela en PAPER. La reserva debe ser anterior a la semilla PAPER.
2. En Cartera REAL, desbloquea tu cuenta y conecta tu wallet en BNB Chain.
3. Pulsa **Verificar control** y firma el mensaje de sesión. Esta prueba de
   control permite identificar la wallet; el permiso del bot se concede después.
4. Abre **Bot DEX**, elige **Reserva** y comprueba la estrategia con sus límites reales de entrada,
   presupuesto, exposición, pérdida y compras acumuladas.
5. En **Ejecutor · BNB**, crea o restaura la operadora y comprueba su copia
   cifrada. Elige su vigencia antes de crearla o restaurarla. Configura la
   política y las aprobaciones desde la wallet.
6. Guarda el vínculo de esa estrategia con el ejecutor y su presupuesto de gas.
7. En **Gas · BNB**, prepara la financiación de la operadora y confirma el envío
   en tu wallet. Espera el recibo confirmado.

La operadora paga el gas. Los USDT/WBNB de inversión permanecen en la wallet
propietaria y el ejecutor sólo puede utilizarlos dentro de la política aprobada.
Los tokens obtenidos en cada compra o venta vuelven directamente a esa wallet.

## Activación

1. Abre **Bot · Activar** y pulsa **Actualizar bots**.
2. Elige el vínculo preparado y revisa los límites mostrados.
3. Selecciona la vigencia: de un minuto a **24 horas**. El valor inicial es
   15 minutos. La hora efectiva aparece al activar y termina antes si caducan
   la operadora o su política. Pedir más tiempo no amplía esos otros permisos.
4. En **Gas y ejecución** puedes ajustar gas, precio máximo por orden y
   deslizamiento. Deben encajar en la política y el gas disponible.
5. Marca **Órdenes automáticas**. Si quieres que continúe con la vista protegida,
   marca también **Segundo plano**; de lo contrario proteger la vista lo detiene.
6. Pulsa **Activar**.

El estado **Activo** exige un trabajador vivo y su permiso vigente. El histórico
inicial sólo prepara los indicadores: la primera entrada espera otra vela
cerrada posterior a la activación. Un intervalo sin observación detiene nuevas
operaciones.

La autorización requiere que ORBIS siga abierto. La protección de vista oculta
los datos privados y exige contraseña para volver, pero un bot autorizado para
segundo plano mantiene su clave sólo en memoria hasta su caducidad original.
Volver a entrar y consultar el estado no renuevan ese plazo. No se reactiva al
reiniciar la aplicación.

### Revisar saldo y permisos

En **Bot · Activar**, selecciona el vínculo y pulsa **Revisar saldo**. ORBIS usa un bloque finalizado para mostrar los USDT y WBNB que el ejecutor puede utilizar, la entrada de referencia y la posición actual del bot. Las cantidades utilizables son el menor valor entre saldo de la wallet y permiso del ejecutor. Para DCA, el importe de una señal puede variar respecto a la entrada de referencia.

El gas disponible es el menor valor entre el saldo BNB de la operadora y el presupuesto restante de su política. Debe cubrir el coste de una orden con los valores elegidos más la reserva de STOP. El precio por unidad de gas también debe encajar en la política. Si falta BNB, la revisión indica cuánto falta. Al activar o rearmar se vuelve a comprobar el gas antes de conectar la firma. El panel de revisión sólo consulta y muestra el bloque observado.

## Seguimiento y parada

La tarjeta muestra posición propia, coste, compras/ventas y gas confirmado.
Una orden pendiente de recibo conserva esa etiqueta; una señal o simulación de
cotización no acredita una compra ni un beneficio.

Con la vista protegida, **Detener y bloquear** retira todos los permisos y
bloquea la bóveda. **Bloquear** hace lo mismo desde la vista abierta. La opción
de segundo plano no permite superar esas paradas.

**Detener** retira la firma local para nuevas operaciones y sigue disponible sin
tener la wallet conectada. Una transacción ya emitida puede finalizar después;
su recibo debe comprobarse. La parada local conserva las posiciones abiertas.

Si la activación tarda, **Detener activación** cancela también su preparación.
Si se pierde la respuesta, ORBIS consulta la misma solicitud y evita repetirla.

Ante un recibo pendiente después de cerrar la aplicación:

1. Desbloquea la cuenta y verifica de nuevo el control de la wallet.
2. Abre **Bot · Activar** y localiza el envío pendiente.
3. Introduce su hash original y pulsa **Comprobar recibo**.

La comprobación sólo consulta y registra evidencia; no vuelve a enviar la orden
ni reactiva el bot. Si el archivo de datos falla al detener, ORBIS retira la firma
y muestra **Parada local · Revisar**, con la posición sin verificar.

## Retomar un bot

Mientras siga vigente la misma política del ejecutor:

1. Pulsa **Detener** y espera a que termine su trabajador.
2. Si caducó la operadora o cerraste la bóveda, restaura su copia cifrada y
   verifica de nuevo el control de la wallet.
3. Selecciona el mismo vínculo, el plazo y el modo de ejecución.
4. Marca **Órdenes automáticas** y pulsa **Rearmar**.

El permiso nuevo conserva posición, coste, trailing, compras acumuladas,
pérdidas y gas gastado. No repone límites ni ejecuta entradas históricas.
Una orden incierta debe resolverse antes de retomar el bot. Durante la salida
del trabajador anterior, el rearme permanece deshabilitado.

Las tarjetas anteriores muestran **Histórico**; sus posiciones no se suman a la
posición actual del último permiso. Reabrir la aplicación recupera los registros,
pero no reactiva la firma ni el bot.

## DCA, gráfico y contabilidad

El selector de vínculo admite **Long** y **DCA**. El DCA usa una regla versionada
probada en LAB/PAPER y su propia asignación REAL; no se activa al crear la regla.
Su ventana de ejecución se configura al activar y se conserva al rearmar.
Detalles y límites: [DEX_DCA_PLAN.md](DEX_DCA_PLAN.md).

La tarjeta del bot permite abrir **Gráfico**, con compras y ventas confirmadas.
En **Contabilidad DEX**, pulsa **Actualizar recibos**, revisa cada operación y
confirma **Contabilizar** para registrarla en la cartera vinculada. El registro
incluye el gas BNB y permite enlazar asientos existentes sin duplicarlos.
Detalles: [DEX_EXECUTIONS.md](DEX_EXECUTIONS.md) y
[DEX_RECEIPT_ACCOUNTING.md](DEX_RECEIPT_ACCOUNTING.md).

Tras parar el bot y conciliar sus envíos, **Gas · Recuperar BNB** permite revisar
y devolver el gas residual de la operadora al propietario. Los tokens de las
compras y ventas ya regresan directamente a su wallet.
Recorrido: [DEX_GAS_RETURN.md](DEX_GAS_RETURN.md).

## Despliegue cancelado o reemplazado

En **Operaciones del ejecutor**, introduce el hash del reemplazo y pulsa
**Revisar reemplazo**. Revisa resultado y gas, acepta expresamente cualquier
sobrecoste mostrado y pulsa **Confirmar resolución**. ORBIS comprueba una
transacción ya finalizada; no envía otra. Una cancelación resuelta permite
preparar otro despliegue con el siguiente nonce. El gasto aparece en
**Contabilidad DEX**. Detalles: [DEX_DEPLOYMENT_RESOLUTION.md](DEX_DEPLOYMENT_RESOLUTION.md).

## Financiación o devolución reemplazada

En **Envíos de gas** o **Gas · Recuperar BNB**, introduce el hash del reemplazo
y pulsa **Revisar reemplazo**. Comprueba el resultado, importe y gas; acepta
expresamente los cambios mostrados y pulsa **Confirmar resolución**. El proceso
registra una transacción ya minada, sin emitir otra ni aumentar límites del bot.
Si se pierde la respuesta, pulsa **Actualizar gas** o **Actualizar devoluciones**.
Después de resolver, una nueva operación exige su propia revisión y confirmación.
El gas puede registrarse desde **Contabilidad DEX**; el principal sigue siendo
un traslado interno. Detalles: [DEX_NATIVE_RESOLUTION.md](DEX_NATIVE_RESOLUTION.md).

## Cancelar una devolución pendiente

1. Detén los bots, verifica la wallet y restaura la copia de su operadora.
2. En **Gas · Recuperar BNB**, pulsa **Actualizar devoluciones** y después
   **Cancelar devolución** en la operación pendiente.
3. Revisa precio, aumento mínimo y coste máximo. Pulsa **Revisar cancelación**
   y después **Confirmar cancelación**.
4. Conserva el hash y pulsa **Comprobar cancelación** si sigue pendiente.
   Si se pierde la respuesta, **Actualizar devoluciones** recupera el registro.
5. Tras resolver, **Revisar devolución** permite preparar un nuevo envío del
   BNB restante a la wallet. Es otra operación con su propio coste y confirmación.

La cancelación intenta sustituir el envío usando el mismo nonce y valor cero.
Puede ganar la devolución original; ORBIS registra el resultado que realmente
se confirmó. Un envío incierto no se repite. **Revisar otra cancelación** permite
autorizar otro intento más caro al mismo nonce, con otra revisión y confirmación.
Sólo el recibo ganador pasa a **Contabilidad DEX**. Detalles:
[DEX_NATIVE_CANCELLATION_PLAN.md](DEX_NATIVE_CANCELLATION_PLAN.md).

## Cancelar una financiación pendiente

1. Verifica el control de tu wallet en BNB Chain y abre **Gas · BNB**.
2. Pulsa **Actualizar gas** y **Cancelar financiación** en el envío pendiente.
3. Revisa precio, aumento y máximo solicitado. Si falta la tarifa firmada
   original o anterior, la aceptación de tarifa desconocida es explícita.
4. Pulsa **Revisar cancelación** y **Confirmar cancelación en wallet**.
   Comprueba en la wallet el nonce y la tarifa: el valor debe ser cero y el
   destino tu propia wallet. La wallet puede modificar la solicitud.
5. Usa **Comprobar cancelación** con su hash o **Vincular resultado** cuando
   el ganador ya esté verificado. Si la wallet cambió la tarifa/tipo, abre
   **Revisar cambios** para revisar y aceptar su efecto real.

No requiere restaurar la clave operadora. Si se pierde la concesión HTTP,
**Actualizar gas** recupera el registro sin abrir de nuevo la wallet. Si la
wallet sigue sin responder, termina primero su diálogo: cambiar de pantalla
no habilita una segunda solicitud. Después de un resultado incierto,
**Revisar otra cancelación** exige otro intento explícito y más caro al mismo
nonce. Puede ganar la financiación original; se registra el ganador real y
sólo su comisión. Detalles: [cancelación desde wallet](DEX_WALLET_CANCELLATION_PLAN.md).

## Funciones pendientes

Conciliar swaps o llamadas arbitrarias reemplazados sigue pendiente. Un envío
sin recibo ganador permanece incierto.
También quedan el orden de swaps históricos de distintas operadoras en un mismo
bloque, rendimiento de historiales grandes y validación humana en mainnet.

## Renovar política

1. Detén el bot y espera a que su trabajador termine. Resuelve cualquier recibo pendiente.
2. Restaura la copia de operadora si ha caducado, y verifica tu wallet en BNB Chain.
3. Elige la vigencia y pulsa **Renovar** en la última tarjeta del bot.
4. Revisa presupuesto y gas restantes; pulsa **Confirmar en wallet**.
5. En «Operaciones del ejecutor», comprueba el recibo y pulsa **Aplicar renovación**.
6. Selecciona el vínculo nuevo, marca **Órdenes automáticas** y pulsa **Rearmar**.

La renovación conserva los consumos originales; no añade capital ni repone cuotas.
Admite resultados recuperados y aceptados como ejecutados, cancelados o revertidos.
Incluye su gas real y conserva las claves DCA consumidas: una cancelación no
habilita repetir la misma activación. Un STOP recuperado debe acreditar la revisión
de política. Si queda una operación pendiente o falta gas para la reserva STOP,
se bloquea renovar. Los registros originales no se reescriben.
Si se agotaron las compras y queda posición, sólo permite cierre. Si se perdió la
respuesta al preparar, **Recuperar renovación** consulta el mismo ID. Una firma o
envío incierto se resuelve con su operación original, sin volver a solicitarlo.


## Investigación al activar o rearmar

Elige la reserva compatible en **Bot · Activar**. ORBIS repite los originales
LAB/DCA antes de conceder el permiso. Mantén accesible ese almacén durante la
activación; una vez arrancado, el trabajador conserva la prueba cifrada sin
necesitar LAB/PAPER abiertos. Al rearmar puedes seleccionar otra reserva de la
misma versión. Las pruebas anteriores y la posición no se sustituyen.

La reserva estudia señales MEXC, no ejecuciones históricas DEX. Gas, precios y
costes de Uniswap se comprueban y registran por separado al operar. Las compras
DCA comienzan con la referencia del cierre conocido al activar; el rearme
mantiene su referencia y las activaciones ya consumidas.
