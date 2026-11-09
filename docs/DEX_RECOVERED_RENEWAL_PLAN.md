# Renovación DEX tras recuperar resultados

Estado: v93 verificada y cargada. Evidencia:
`~/.local/state/orbis/verification/v93-recovered-renewal-r1/verification.json`.
La regresión actual pasa 793 casos/100 archivos y 27 recorridos de navegador;
3.267 casos y 20 recorridos anteriores sin cambios completan la cobertura por
identidad. No se afirma repetición completa sobre las fuentes finales.

La renovación histórica v1 sólo suma recibos originales FINALIZED/REVERTED. Un
resultado recuperado conserva su propio comprobante; nunca se cambia el estado
original para fingir que ese recibo se recibió a tiempo. El flujo v93 renueva desde esos hechos, conservando posición, consumos y gas ya pagado.

| Contrato | Existente, conservado | Nuevo, con resultados recuperados |
| --- | --- | --- |
| Revisión del propietario | control v3 | control v4 |
| Material de renovación LONG / DCA | policy-rollover v1 | policy-rollover v2 |
| Vínculo sucesor | binding v2 | binding v3 |
| Continuación LONG con política | long v4 | long v6 |
| Continuación DCA | dca v1/v2 | dca v3 |

Una revisión nueva adopta el formato recuperado si existen resultados aceptados
en ese corte o si el permiso ya conserva esa semántica. Las lecturas antiguas
seleccionan explícitamente su contrato anterior. No se modifican long v5, dca
v2, call-resolution v1 ni DEX_RECEIPT_BOOKING_V4. No hay cambio de tablas SQL.

El material nuevo fija los identificadores y hashes de las resoluciones. Una
aceptación posterior al corte no puede justificar una revisión anterior. Se
reconstruyen los efectos de compras/ventas ejecutadas; cancelaciones y reversiones
sólo consumen su gas real. Un STOP recuperado acredita su cambio de política
mediante su efecto verificado. Una operación todavía pendiente bloquea renovar.

La configuración confirmada debe conservar exactamente las unidades y su coste,
reiniciar únicamente los contadores físicos y derivar cuotas del presupuesto
original menos los consumos acumulados. El grant nuevo conserva el gas restante.
La continuación requiere otro permiso explícito y conserva vela de entrada,
trailing, referencia DCA y activaciones ya consumidas. Las cancelaciones DCA
siguen consumiendo su clave, como establece dca v2; no se reintenta una activación
al renovar. Sólo ABANDONED_UNSENT libera una clave según el contrato existente.

Verificación realizada: EVM propia con compra/venta/cancelación/reversión
recuperada, STOP, configuración del propietario, grant sucesor, rearme y nueva
ejecución; LONG y DCA. Además, corte temporal, cambio de comprobantes, límites
agotados, gas, renovaciones sucesivas, ausencia de reenvíos, copia cifrada y
reapertura sin autorun. Los originales permanecen idénticos y legibles.
