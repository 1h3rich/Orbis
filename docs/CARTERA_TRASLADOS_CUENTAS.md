# Traslados contables entre cuentas REAL

Estado: registro **manual y local** de un traslado ya realizado. No solicita
una transacción a wallet, DEX, MEXC ni GRVT; no acredita que el activo haya
llegado al destino.

Dos cuentas de la bóveda REAL pueden compartir un traslado de un activo y una
misma divisa de coste. ORBIS registra `TRANSFER_OUT` y `TRANSFER_IN` en una sola
transacción SQLite cifrada, con el mismo `request_id`. La unidad y la base de
coste salen de la primera y entran en la segunda; no hay venta, beneficio
realizado ni flujo de efectivo. El coste parcial usa el promedio ponderado del
inventario existente en la fecha del traslado. Un traslado total conserva el
coste restante exacto, sin residuo por división. Se valida el historial entero
de ambas cuentas antes del commit, incluidas ventas posteriores: una fecha
retroactiva que produzca inventario negativo se rechaza en ambas cuentas.

`POST /real/accounts/{source_id}/transfers/preview` recibe `request_id`,
`target_account_id`, `asset`, `quote_currency`, `quantity`, `occurred_at` y
`note` opcional. Valida ambos historiales y devuelve coste y saldo disponible
en la fecha indicada, pero revierte la transacción sin escribir eventos. El
`POST /real/accounts/{source_id}/transfers` repite la validación y exige
`expected_cost_basis`: si cambió desde la vista previa responde `409` y no
guarda el par. Repetir la misma petición confirmada devuelve los mismos dos
eventos; reutilizar su ID con datos distintos también da `409`. La API exige
bóveda desbloqueada y origen local. La interfaz lo presenta en un panel
plegado «Traslado entre cuentas», disponible si hay al menos dos cuentas, y
pide confirmación con cantidad, destino y coste antes de escribir.

Las copias cifradas y la restauración revalidan que cada `request_id` de
traslado tenga exactamente una salida y una entrada concordantes. Una copia
con un lado ausente o modificado se rechaza antes de sustituir la bóveda.

Una comisión de red o de retiro requiere un asiento adicional explícito. La
primera versión no convierte bases de coste entre divisas: ambos lados usan
la misma divisa indicada, aunque las cuentas tengan distintas divisas base.
No se guardan direcciones completas. Las instantáneas de transferencias MEXC
y los saldos wallet siguen separados: el usuario debe contrastar el traslado
externo antes de registrarlo. Los dos eventos aparecen en los historiales
respectivos con su coste trasladado, no como ingreso o gasto.

«Transferencias guardadas → Comparar con cuenta seleccionada» busca
coincidencias orientativas entre los traslados pareados de esa cuenta REAL y
una fotografía MEXC cifrada. Supone que la cuenta local seleccionada representa
MEXC; **no verifica ese vínculo**. Si ya existe una asignación local previa de
fills a otra cuenta o a otra referencia MEXC, se detiene la comparación y
muestra el conflicto. Una asignación coincidente tampoco prueba titularidad.
Busca igual activo e importe y una diferencia
temporal de hasta 24 h, y señala ambigüedad cuando hay varias filas posibles o
varios asientos que apuntan a la misma fila. La comparación es bidireccional:
también enumera filas MEXC sin **traslado pareado candidato**, que podrían
tener otro asiento o estar fuera del registro local. Un asiento cercano al
borde de la instantánea se marca «ventana parcial» si no hay candidato; no se
interpreta como ausencia. La fotografía no guarda txid ni
direcciones, por lo que ni siquiera una coincidencia singular demuestra
titularidad, finalización o llegada de fondos. La comparación es de sólo
lectura: no asienta movimientos ni habilita trading.

El mismo panel admite **Efectivo**: `CASH_TRANSFER_OUT` y `CASH_TRANSFER_IN`
trasladan el mismo importe de una divisa entre dos cuentas locales. Se exige
saldo suficiente en el origen en la fecha económica. No se contabiliza como
aportación, retirada ni beneficio en ninguna cuenta. La vista previa usa
`POST /real/accounts/{source_id}/cash-transfers/preview` y enseña saldo antes
y después; el commit usa `/cash-transfers` y exige
`expected_source_balance` para rechazar un cambio entre revisión y guardado.
Ambos asientos son atómicos, idempotentes y comprobados también al restaurar
una copia cifrada. La UI **no envía dinero**; sólo registra un movimiento
externo que el usuario ya realizó y debe verificar por separado. No hay
cambio de divisa automático ni enlace causal con un depósito MEXC observado.

Un CSV de una sola cuenta con `TRANSFER_IN`, `TRANSFER_OUT` o un traslado de
efectivo pareado sirve como
consulta, pero **no** puede reimportar de forma segura el par completo; la UI
lo advierte antes de exportar. Para restaurar todos los traslados, usar la
copia cifrada de la bóveda. No importar cada lado por separado como ajuste.
