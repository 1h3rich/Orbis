# Operaciones DEX: marcadores y contabilidad

Estado v78: implementación de asientos, revisión y enlaces al gráfico
terminada, verificada y cargada; evidencia en el registro de progreso. Fuentes, fórmulas
y límites en `DEX_RECEIPT_ACCOUNTING.md`; gráficos en `DEX_EXECUTIONS.md`.
La recuperación de nonces cancelados/reemplazados sigue pendiente, descrita
en `DEX_NONCE_RECOVERY_PLAN.md`. El registro de progreso acredita cada carga.

## Fuentes existentes comprobadas

- `dex_operator_receipt.py` comprueba firma, llamada exacta, programa, evento
  único y transferencias USDT/WBNB del propietario. Conserva salida real,
  posición propia, coste asignado, resultado realizado y gas BNB.
- `dex_bot_journal.py` vincula decisión, versión LAB/PAPER, concesión y recibo.
  DCA añade claves BASE/DIP, calendario y presupuestos propios.
- `wallet_swap_booking.py` ya genera propuestas contables desde swaps manuales:
  cantidad y contravalor exactos, `gross_override` si la división no termina,
  revisión del libro, idempotencia y descuento de gas BNB sin inventar FX.
- `dex_deployments.py` recupera código de CREATE en la dirección esperada;
  todavía no resuelve una cancelación/reemplazo del nonce por otra transacción.

## Marcadores

Generar un DTO protegido desde intents FINALIZED con recibo verificado. Un
intento PENDING, una cotización o una decisión BUY no son un fill. REVERTED se
muestra como incidencia y coste de gas, sin marcador de compra realizada.

Cada ejecución conserva intent/run/binding/version, dirección, instante del
bloque y cantidad/contravalor reales. El precio es USDT dividido por WBNB del
recibo, con precisión decimal documentada. La referencia MEXC del mismo momento
no sustituye ese precio. El gráfico puede usar velas públicas MEXC, identificando
su fuente y la ejecución Uniswap. La vela de señal y la ejecución son tiempos
diferentes; no desplazar un fill al cierre que generó la decisión.

Reutilizar los componentes de gráficos de operaciones MEXC/GRVT cuando su
contrato admita la semántica. Mostrar compras y ventas parciales por separado,
con enlaces al recibo y su motivo. Historial de permisos/renovaciones conserva
la familia, pero no vuelve a dibujar un fill en cada época de permiso.

## Asientos y flujos

La cartera destino procede del vínculo de wallet, no de una cuenta CEX abierta
por casualidad. Compra: USDT real de entrada y WBNB real recibido. Venta:
WBNB entregado y USDT recibido. No adoptar WBNB previo de la wallet como posición
del bot ni asumir que el coste del bot coincide con el coste de toda la cartera.

El coste asignado por el ejecutor pertenece a sus unidades propias. El libro
patrimonial puede contener WBNB de otras compras y calcula su coste según el
criterio contable del libro. Ambos resultados deben poder explicarse por su
ámbito; no forzar su igualdad cambiando el histórico.

BNB nativo de gas y WBNB adquirido son activos diferentes. Registrar el gas
real en BNB, sin restarlo directamente del contravalor USDT ni atribuirle un
precio MEXC inventado. Si falta BNB registrado, presentar una conciliación
pendiente; no crear saldo o aportaciones para hacer pasar un asiento.

Financiar la operadora y devolver su principal son movimientos internos del
patrimonio. Sus comisiones reales sí son costes. Mantener identificadores
locales/alias de la operadora y evitar guardar direcciones completas. Aclarar
el alcance del patrimonio registrado frente al saldo de una EOA consultada.

## Persistencia y comprobación

Propuesta revisable con identidad del recibo, revisión del libro y eventos
estables. Confirmar una vez en SQL; repetir recupera la misma contabilización.
Vincular eventos existentes sólo si coinciden cantidades, contravalor, activo,
instante y comisiones. Un enlace manual no permite duplicar un gasto.

Comprobar compra, venta parcial/completa, revertida con gas, múltiples permisos
y políticas, contexto de otra cuenta, evento ya registrado, revisión del libro
cambiada, comisiones sin saldo, intento incierto, manipulación con hashes
recalculados, copia/reapertura y navegador con EVM desechable propia.

La recuperación de CREATE cancelado y de nonces de envíos nativos sin recibo
necesita otro recorrido explícito. Un nonce consumido no demuestra que el envío
original tuviera éxito. Una posible cancelación debe fijar propietario, nonce,
valor/calldata y coste; nunca convertirse en un firmador de transacciones
arbitrarias ni en un reintento automático de un resultado incierto.
