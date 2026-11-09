# Validación de perfiles LIMIT

Desde v119, en **Laboratorio → Validación de derivados**, selecciona un perfil
**LIMIT**, los datos y un método: **Reserva**, **Ventanas sucesivas** o
**Recorridos TRADE/MARK**. **Validar** guarda el informe. El botón **Ver** de cada
tramo abre la prueba exacta con sus operaciones y marcadores en el gráfico.
**Exportar JSON** guarda el informe completo. La **Referencia OOS** está
habilitada para los informes con reserva. Los recorridos no tienen reserva.

Cada tramo usa el mismo motor LIMIT que Pruebas de derivados. Conserva el precio
límite calculado con la distancia del perfil y el tiempo de vigencia. Empieza
con caja nueva, sin posiciones ni órdenes pendientes. Sólo se usa el prefijo
anterior para indicadores y señales ya conocidas. Las órdenes pendientes se
cancelan al final de cada tramo. El toque TRADE modela un fill completo, sin
modelar la cola del libro ni la liquidez real. No se reajustan parámetros.

Los contratos de LIMIT son `perp-validation-2.0`, `perp-ohlc-2.0`,
`orbis.lab.perp.validation.v2`, `orbis.lab.perp.validation.result.v2` y
`orbis.lab.perp.validation.bundle.v2`. Cada tramo vincula sus eventos y métricas
con el hash del resultado completo. La referencia `orbis.research.perp.reference.v2`
tiene autoridad `NONE`; no se acepta como referencia de mercado v1. Los archivos
de mercado conservan sus formatos y cálculo.

LAB mantiene SQL 12. La lectura, exportación, copia cifrada e inspección
comprueban la versión y los originales. Se reproduce el resultado completo de
cada tramo y se vuelve a comprobar la sesión antes de devolverlo o guardarlo.
El archivo deduplica resultados iguales. Para abrir una copia con informes LIMIT
se requiere una versión que reconozca sus contratos.

Desde v120, **Probar en PAPER** permite preparar las reservas LIMIT completas
para [PAPER observado](PERP_LIMIT_PAPER.md). El informe conserva autoridad
`NONE` y las hipótesis históricas se identifican como experimentales.

## Evidencia

Pasan 1.086 casos de 81 módulos del alcance afectado, con identidades cotejadas y
cuatro salidas 0. Incluyen 19 casos nuevos: oráculos LONG y SHORT, cancelación
entre tramos, invariancia ante precios futuros, archivo cifrado, reapertura,
rechazo de resultados rehashados y bloqueo tras cambiar la sesión. Las otras
áreas conservan la evidencia anterior.

Las 48 validaciones MARKET de comparación dan resultados completos idénticos
entre v118 y v119. Cubren los tres métodos, LONG/SHORT, margen aislado/cross y
ambos recorridos de TRADE y MARK. Seis recorridos de navegador comprueban los
informes y las pruebas de mercado y LIMIT, los marcadores, la reapertura y los
flujos existentes de PAPER observado y activación con proveedores controlados.

La publicación está registrada en
`~/.local/state/orbis/verification/v119-perp-limit-validation-r1/verification.json`.
