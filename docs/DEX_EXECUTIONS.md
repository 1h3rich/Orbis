# Ejecuciones DEX en el gráfico

La API `GET /real/accounts/{account_id}/dex-bots/runs/{run_id}/executions-page`
proyecta recibos propios verificados, sin RPC, firma ni escritura. Requiere la
sesión REAL de esa cuenta y no renueva permisos operativos. La respuesta lleva
`Cache-Control: no-store` y no incluye direcciones, claves ni hashes completos
de transacción. La proyección original v77 no modifica el esquema 65. Desde v78 lee también
los vínculos contables verificados del esquema 66, sin crear asientos.

## Fuente y precisión

Un marcador representa un swap `FINALIZED` vinculado a una decisión y a su
versión de estrategia o DCA. Un BUY pendiente no es una compra realizada. Un
swap `REVERTED` incrementa su contador y gas, sin marcador de compra o venta.
STOP y otros controles quedan fuera de esta proyección de swaps.

La cantidad WBNB y el contravalor USDT proceden de los enteros de 18 decimales
del recibo. El precio es contravalor dividido por cantidad con precisión de
80 dígitos significativos (`Decimal`, redondeo `ROUND_HALF_EVEN`). Cantidad,
contravalor, gas y resultado permanecen como texto decimal exacto. La
representación gráfica convierte exclusivamente coordenadas y precios a
`Number`; el inspector conserva el texto original.

El instante es el del bloque del recibo. `signal_candle_t` conserva aparte la
vela de señal; `reference_price`, la referencia MEXC usada al decidir. No se
sustituyen el precio ni el instante ejecutados por los de la vela o referencia.
El gas se expresa en BNB nativo, separado de WBNB y USDT.

## Historial y paginación

El recorrido de permisos es raíz → ejecución elegida. Incluye una sola vez los
fills de sus antecesores; elegir un permiso antiguo no incluye ejecuciones de
sucesores futuros. DCA conserva las claves BASE/DIP originales.

Cada página devuelve hasta 500 de un máximo de 100.000 swaps. A partir del
segundo tramo se exige la revisión del primero. La revisión incorpora las
identidades y estados de los intents, incluidos los pendientes y revertidos;
un recibo nuevo invalida una paginación anterior. Una decisión WAIT sin cambios
económicos no invalida el gráfico. La consulta enlaza los índices de decisiones
e intents, sin construir una lista SQL de todos los WAIT.

## Componente compartido

`real-fill-chart.js` gestiona OHLC, rangos, histórico, inspección, ciclo de vida
y controles comunes. Los adaptadores `mexc-bot-chart.js` y `dex-bot-chart.js`
validan su propia evidencia. MEXC sigue exigiendo fills conciliados y asentados;
DEX exige recibos confirmados y muestra **pendiente de contabilizar** o el
asiento verificado. Cada fill conserva `financially_booked`, `booking_id` y
`event_id`; los IDs son nulos cuando no hay asiento. `booked_count` cuenta los
fills asentados y el indicador global sólo es verdadero si lo están todos.
No crea un identificador de asiento inexistente.

Las velas públicas MEXC están identificadas como referencia. Si fallan, los
recibos continúan visibles y se indica la ausencia de OHLC. No se generan velas
artificiales. Varias ejecuciones del mismo lado en un segundo comparten una
flecha en el último precio de ese grupo; el selector y el inspector conservan
todas las ejecuciones individuales. La precisión temporal del bloque EVM es un
segundo, sin desplazar artificialmente operaciones para separarlas.

Cerrar, bloquear o cambiar el contexto invalida respuestas pendientes y elimina
los datos retenidos del panel. Ocultar importes redacta cantidad, precio,
referencia y comisión del inspector. Sólo el rango, una preferencia visual,
puede persistirse localmente.

## Exportar swaps para investigación

Abre **Gráfico → Exportar JSONL** con los importes visibles. La confirmación indica que la descarga se guarda en claro. La exportación utiliza la misma revisión que el gráfico y conserva el estado contable de cada swap. Un permiso anterior conserva su propio tramo del linaje. [Formato de investigación](IA_Y_ALMACENAMIENTO.md#swaps-uniswap-real-jsonl).

## Contabilidad

Estos marcadores no contabilizan el swap. El panel «Contabilidad DEX» revisa y
registra el recibo; después el gráfico comprueba su vínculo con el libro. Se
conserva la distinción entre ejecución, comisiones y patrimonio registrado.
Véase `DEX_RECEIPT_ACCOUNTING.md` para fuentes, fórmulas y límites.
