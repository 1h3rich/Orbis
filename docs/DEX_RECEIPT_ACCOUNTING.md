# Contabilidad de recibos DEX

El panel **Contabilidad DEX** enlaza recibos finalizados con el libro de la
wallet. No firma órdenes ni transfiere fondos. Es independiente de la vigencia
del bot, del permiso Trade y de la conexión de la operadora.

## Fuentes y efectos

| Fuente verificada | Efecto patrimonial |
| --- | --- |
| Despliegue CREATE | Comisión BNB; observar código sin recibo no acredita gas |
| Control CONFIGURE, STOP, APPROVE o REVOKE | Comisión BNB |
| Financiación de la operadora | Comisión BNB; principal como traslado interno |
| Operadora BUY / SELL confirmada | USDT/WBNB reales y comisión BNB |
| Operadora STOP o transacción revertida | Sólo comisión BNB |
| Devolución de gas | Comisión BNB; principal como traslado interno |

Cada fuente pasa por su verificador original. Las compras y ventas del bot
reproducen además su decisión causal y su versión. Una orden pendiente, un
nonce consumido o una observación de código no crean asientos de ejecución.

La cartera destino procede del vínculo de la wallet, aunque el recibo se haya
preparado desde otra cuenta REAL. Se exige control EOA en la sesión y un
vínculo con una cuenta propia separada de MEXC. La confirmación no permite
cambiar libremente la cuenta destino.

## Fórmulas y conciliación

Las cantidades de USDT y WBNB proceden de enteros de 18 decimales. La compra
consume el USDT entregado y añade el WBNB recibido. La venta consume el WBNB
entregado y añade el USDT recibido. El precio informativo usa 40 dígitos; si su
producto no reproduce el contravalor, se conserva el contravalor exacto con
`gross_override`. El cálculo usa contexto decimal local de 300 dígitos y
`ROUND_HALF_EVEN`, sin cálculos monetarios en coma flotante.

La comisión es `gas_used × gas_price` en wei BNB. Se descuenta primero del
saldo BNB registrado y después, si hace falta, de las posiciones BNB y su coste
histórico. WBNB es otro activo. No se inventa un precio de conversión BNB/USDT.
Si falta USDT, WBNB o BNB registrado, la revisión no puede confirmarse hasta
conciliar el historial; no crea una aportación para cubrir el déficit.

La cuenta registrada comprende la wallet y su reserva de gas en la operadora.
Enviar BNB propio a esa operadora y recuperarlo no modifica el patrimonio total
ni sus aportaciones. Sólo sus comisiones son gasto. Un saldo consultado de la
EOA no incluye necesariamente esa reserva ni otras posiciones del libro.

El coste asignado por el bot corresponde exclusivamente a sus unidades. El
libro patrimonial aplica su coste medio a todas las unidades WBNB de la cuenta,
incluidas compras anteriores ajenas al bot. Sus resultados pueden diferir y
se conservan ambos ámbitos.

Se revisa el orden de bloques y, dentro de un bloque y una misma operadora, el
nonce. No se contabiliza una venta antes de una ejecución anterior de la wallet
que siga sin registrar. Para varias operadoras con swaps en el mismo bloque,
los recibos antiguos no conservan el índice global de transacción: se exige
conciliar ese orden, sin deducirlo de UUIDs ni inventar instantes. Esa ampliación
sigue pendiente. El gráfico conserva el tiempo original del bloque.

## Revisión y persistencia

La propuesta fija el recibo y la definición originales, la revisión completa
del libro y los eventos existentes elegidos. Si cambia la cartera, se exige
revisar de nuevo. Los asientos manuales compatibles se muestran para enlazarlos;
no se adoptan silenciosamente. Se comparan activo, cantidad, contravalor,
comisiones e instante (tolerancia de dos minutos para un asiento existente).

Una transacción `BEGIN IMMEDIATE` guarda eventos y vínculo juntos. El ID del
vínculo y los request IDs de los eventos son estables. Repetir una confirmación
recupera el registro original. Los eventos reclamados por otro recibo, swap
manual o fill MEXC no pueden reutilizarse. Las comprobaciones son bidireccionales
entre el importador manual y el nuevo diario.

El esquema **66** añade `real_dex_receipt_bookings` y sus enlaces
`real_dex_receipt_booking_events`. La referencia de origen polimórfica se
resuelve sólo mediante una enumeración cerrada de verificadores. Los vínculos
con cuenta y eventos tienen claves foráneas. La lectura y las copias verifican
la semántica del recibo, hashes de eventos, exclusividad, importe total de gas
y una referencia HMAC que vincula propietario y cuenta destino. No se guardan
direcciones completas, claves, firmas ni hashes completos de transacción.

La migración conserva una preimagen cifrada, prepara otra copia cifrada,
comprueba integridad, historial y conteos, y reemplaza el archivo al terminar.
Una bóveda anterior con registros del esquema nuevo ocultos se rechaza.

## API e interfaz

Las rutas protegidas están bajo `/real/accounts/{account}/dex-receipts`:

- `POST /query`: consulta pasiva, paginada y con revisión. La dirección va en el
  cuerpo para evitar incluirla en URLs o registros de acceso.
- `POST /{kind}/{id}/booking-preview`: revisión sin asientos nuevos.
- `POST /{kind}/{id}/book`: confirmación `CONTABILIZAR_RECIBO_DEX`.

Las respuestas privadas llevan `no-store`. El panel borra su contexto al
cambiar wallet, cuenta, red, generación o control verificado. Ocultar importes
redacta también la revisión. Si se pierde una respuesta de confirmación, se
retira el botón de confirmar y se consulta la lista para recuperar el estado;
no se repite automáticamente el POST. El gráfico enlaza un asiento únicamente
después de verificar el registro contable.

## Verificación

Se utilizan bóvedas y EVM propias desechables. Se comprueban compra, ventas
parciales, coste patrimonial diferente del coste del bot, gas sin saldo,
revisión caducada, asientos ya existentes, confirmaciones concurrentes,
reapertura, recibos revertidos, CREATE legacy/EIP-1559, financiación, devolución,
DCA con compras iguales cercanas, manipulación rehasheada y migración 65→66.
El navegador recorre compra/venta, conciliación, pérdida de respuesta,
financiación interna, enlaces del gráfico y móvil. La evidencia definitiva se
publica en el registro de progreso al terminar los verificadores.
