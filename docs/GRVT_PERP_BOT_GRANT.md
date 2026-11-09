# Concesión REAL del modelo perpetuo

Estado: contrato v4 verificado/cargado desde v109. v110 añade una concesión v5
de supervisión y su [activación API/GUI](GRVT_PERP_BOT_ACTIVATION.md), conservando
la lectura v4 descrita aquí. Las lecturas guardadas conservan
`authorization:false` y nunca arrancan trabajadores.

## Origen y formatos

`verified_perp_bot_origin` reproduce LAB/PAPER y exige reserva OOS completa,
vela observada y operación cerrada observada, sin GAP, liquidación, suspensión,
cierre estimado o avance durante la revisión. Compara margen/apalancamiento
privados por instrumento. La revisión v107 sigue con autoridad NONE y
`execution_supported:false`; no se reinterpreta como autorización.

La fuente guardada identifica perfil, definición y prefijo PAPER probado,
sin copiar capital/posiciones ficticios. Una capacidad efímera aparte conserva
originales, archivo y época de desbloqueo. Antes de crear la concesión se
revalidan el prefijo LAB de precios, sus filas originales y el ancla PAPER.
Un avance posterior compatible puede conservar ese prefijo histórico; no se
presenta como replay del nuevo tail. Bloqueo, sustitución, alteración o cierre
estimado invalidan el handoff.

- Fuente: `orbis.grvt.perp.bot.source.v1`, autoridad NONE.
- Concesión: `orbis.grvt.mainnet.bot.grant.v4`, con perfil, definición, seed
  propio, presupuesto REAL, límites de entradas/pérdida/notional y plazos.
- Claim: `orbis.grvt.mainnet.bracket.claim.v3`, fuente
  `PERP_BOT_NATIVE_PROTECTED_ENTRY`, ligado al plan y señal seleccionados.

Se comparten las tablas cifradas `grvt_mainnet_bot_*`, exclusión de subcuenta,
STOP e identidades durables con bots anteriores. La reserva excluye entradas
manuales y otro bot de cualquier versión. No se falsifican filas PAPER.
SQLite sigue en esquema 72. Concesiones v1–v3 y claims v1/v2 mantienen sus
contratos; los binarios anteriores rechazan los tags nuevos.

## Selección y envío

`append_perp_market` verifica el replay y confirma el evento en una transacción
sin HTTP, antes de que el trabajador publique la candidata. Una escritura de
resultado desconocido no se reintenta ni recupera autoridad desde el archivo.

SELECT exige la misma fuente/perfil/definición, instrumento/temporalidad, señal
cerrada posterior a la autorización y, tras una operación, a su conciliación.
No utiliza señales producidas mientras la posición anterior seguía abierta.
Comprueba cobertura, caducidad, número de entradas, presupuesto y pérdidas.

El kernel recoge preflight actual y dimensiona con equity/presupuesto REAL.
SELECT fija el plan completo; acción y parent ID derivan de concesión/señal y
se reservan una sola vez. La selección se confirma antes de firmar/crear el
claim, y éste antes del POST. Una selección queda consumida aunque una
revocación impida enviar. La respuesta perdida se consulta; no se retransmite.

El claim debe conservar exactamente el plan seleccionado. Reetiquetarlo como
manual v2 o bot anterior v1 se rechaza, incluso con hashes recalculados.

## Replay y almacenamiento

El lector frío reconstruye señales, planes y enlaces financieros. El lector
activo reutiliza un prefijo RAM ligado a bóveda/archivo/cabecera, después de
huellar de nuevo los bytes y metadatos reales de cada evento. No confía sólo
en hashes SQL. Cambios, borrados y rollbacks fuerzan replay/validación.

La caché se vacía al bloquear y mantiene dos prefijos como máximo; los demás
se reproducen fríos. Evita recalcular indicadores y deserializar el seed en
cada quote, aunque recorre las huellas del diario: no promete coste constante
para un número arbitrario de bots. Las consultas no devuelven el seed entero.
El máximo es 50.000 eventos MARKET y 100.000 eventos totales, reservando espacio
para finalizar. Dos segundos entre consultas permiten las 24 horas de permiso.

## Cierre y financiación

Se reutilizan fills, comisiones, financiación, doble auditoría, conciliación y
recuperación nativas. SETTLE acepta sólo un recibo financiero verificable.
Finalizar activa STOP y libera la reserva de bot; no inventa un cierre remoto.

Para claim v3 se admite el identificador numérico común de los fills de una
ejecución como enlace de funding. GRVT documenta que esos fills comparten el
componente anterior a `-` y la hora, y el pago se refiere al trade/tx. Horas
contradictorias, alias ambiguos y pagos ajenos se rechazan. Los claims anteriores
mantienen el enlace exacto a `trade_id`, sin reinterpretar sus auditorías.
Fuentes revisadas el 6 de noviembre de 2026:
[Trading API](https://api-docs.grvt.io/trading_api/#fill-history),
[FundingPayment](https://api-docs.grvt.io/schemas/funding_payment/).

## Integración posterior de v110

Los requisitos siguientes se conectan en la concesión v5 y la activación
separada de v110; v4 conserva su alcance y nunca inicia el coordinador por una
lectura fría. El piloto humano continúa pendiente.

1. Coordinador compartido con reloj/tickets RAM sincronizados, ciclo de vida
   del trabajador y revocación de todas las credenciales.
2. Observación de modo/apalancamiento de la posición efectiva, discrepancias
   de costes, cierre con límites y pérdida de conexión.
3. Semántica explícita de protección LAST desde el límite firmado frente a
   salidas dinámicas desde fills. El techo de comisión sigue siendo estimado;
   no se ha supuesto una escala para los enteros `ClientTier` sin documentación.
4. Revisión sellada, autorización, activación/seguimiento GUI y aceptación
   navegador → API → diario → cierre/conciliación; después piloto humano mainnet.
