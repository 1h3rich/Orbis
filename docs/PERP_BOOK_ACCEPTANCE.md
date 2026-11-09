# Aceptación del libro perpetuo interno

Implementación: `app/lab/perp_account.py`, tipos `perp_book_types.py`.
Versión interna: `perp-account-0.1`. No es un formato importable ni un nuevo
backtest disponible en la interfaz. No cambia SQL, permisos ni motores previos.

| Requisito del núcleo | Evidencia directa |
| --- | --- |
| LONG/SHORT, aumento, cierre parcial y reversión | Oráculo de cuatro fills y funding con saldos y costes fijados a mano, en CROSS e ISOLATED |
| Nocional distinto de patrimonio | Saldo, coste y colateral comprobados por separado en cada paso; nocional no entra en caja |
| Órdenes, reservas y cancelación | Extremos LONG 2,5 / SHORT 2,6 reservan 52 + 0,41; cancelar libera reservas sin alterar caja |
| Fill parcial | Sólo cantidad ejecutada y fee real; resto pendiente; cancelación libera 40,2 de reserva del ejemplo |
| `reduce_only` | Rechaza abrir/aumentar/invertir, cancela cantidades obsoletas tras otro fill y permite cierre por debajo del margen inicial |
| Revisión antes del fill | Cambios de marca, reglas o pérdida/fee de ejecución cancelan sin fill, comisión ni posición nueva |
| Funding asentado y unidades | LONG/SHORT × tasa positiva/negativa; porcentaje dividido una sola vez; modifica caja/colateral |
| Funding sin duplicación | Segunda liquidación del mismo instrumento/instante rechazada; caso sin posición consumido sin cargo retrospectivo |
| Aislado | Bolsa completa perdida; otros fondos preservados; remanente 8 o déficit 20 explícitos en ejemplos independientes |
| Cross | Dos posiciones consumen el fondo común; transferencia 47,9 o cobertura 222,1; bolsillo SOL de 20 preservado y aún cerrable |
| Mantenimiento por eventos diferentes | Liquidación tras MARK, funding, cambio de reglas o fee de un cierre parcial |
| Pérdida de ejecución extrema | Se cancela antes de generar equity negativa; no traslada pérdidas aisladas a efectivo ajeno ni fabrica cobertura |
| Secuencia y datos actuales | Cursor monotónico con ordinal explícito; MARK completo del instante requerido; no se presume orden real intravela |
| Decimal | Flotantes/bool/NaN/exponentes extremos rechazados; contexto independiente; coste residual totalmente consumido |
| Diario e invariantes | Cada delta coincide con caja, colateral, P/L, fees, funding y fondo; identidad exacta después de toda transición |
| Reproducción de cálculo | Misma secuencia bajo contextos externos diferentes produce estados y diarios iguales; dos secuencias aleatorias fijas |

Evidencia de desarrollo: 50 casos nuevos; 382 casos actuales de 16 archivos.
Informe: `~/.local/state/orbis/verification/v96-account-core-r1/targeted.xml`.
Una primera ampliación tenía dos aserciones enlazadas al objeto inmutable antiguo
antes de enviar la orden. Se separa envío/lectura en ese montaje; el libro no se
relaja. Las pruebas finales recorren el archivo completo.

La medida de 1.000 órdenes pertenece sólo al núcleo en memoria. El diario de una
ejecución histórica aún no se guarda ni recupera desde SQLCipher. Tampoco hay
perfil/estrategia conectado, simulación OHLC, OOS ni interfaz nueva. La aceptación
del [histórico completo](DERIVADOS_HISTORICOS_PLAN.md) sigue abierta; esta tabla
no sustituye los requisitos de la especificación ni el piloto humano mainnet.
