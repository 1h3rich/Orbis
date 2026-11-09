# Entradas LIMIT en pruebas de derivados

La versión v118 incorpora entradas limitadas LONG/SHORT en el laboratorio.
El perfil fija la distancia respecto al cierre de la vela de señal y una
vigencia en velas. Guarda órdenes, ejecuciones, vencimientos, operaciones y
curva de capital junto con los datos y la versión de estrategia originales.

En Laboratorio, abrir **Pruebas de derivados**, elegir **Entrada → Límite** y
configurar **Distancia %** y **Vigencia · velas**. **Probar** guarda el perfil y
el resultado. **Ver** muestra el precio de entrada y los marcadores del gráfico.
**Reproducir** vuelve a calcular los originales. **Exportar JSON** conserva los
importes exactos y el diario completo. Al reabrir LAB se recupera la misma prueba.

## Semántica del modelo

La señal utiliza una vela cerrada. La orden se admite en la apertura siguiente.
Su referencia es el cierre TRADE de la vela de señal. Una compra usa esa
referencia multiplicada por `1 − distancia`; una venta por `1 + distancia`.
El límite de compra redondea al tick inferior y el de venta al superior.
La distancia se guarda como texto decimal. La representación de distancia cero
se normaliza para conservar una identidad estable.

La cantidad y las reservas se calculan al admitir la orden. Un toque posterior
de TRADE modela una ejecución completa, con deslizamiento adverso protegido
por el límite. Una apertura favorable puede mejorar el precio. MARK sigue
actuando sobre el margen y se comprueba de nuevo el estado antes de ejecutar.
Si la orden deja de poder ejecutarse, se cancela con su motivo.

La vigencia incluye la vela de admisión. Al comenzar la vela posterior a las
velas permitidas se comprueba el vencimiento antes de una posible ejecución en
esa apertura. Una orden pendiente al terminar la ventana se cancela. Ambas
cancelaciones conservan su orden original y liberan reservas. Carecen de fill,
comisión y operación inventados.

Funding y MARK conservan la secuencia del perfil. Un pago anterior a la entrada
no se atribuye a la posición posterior. El trailing utiliza sólo puntos TRADE
posteriores a la entrada y se actualiza al cerrar la vela. El máximo o mínimo
anterior a una entrada intravela no estrecha su stop. Stops, objetivos y cierres
siguen usando el libro compartido de margen, costes y liquidación.

## Contratos y almacenamiento

Se añaden `orbis.lab.perp.profile.v2`, `orbis.lab.perp.run.v2` y
`orbis.lab.perp.bundle.v2`, con motor `perp-ohlc-2.0`. Los perfiles de mercado v1
mantienen `perp-ohlc-1.0`. Cada formato se comprueba contra su perfil antes de
reproducirlo. Una etiqueta de motor de mercado sobre un perfil LIMIT se rechaza,
incluso si se recalcula el hash exterior.

El libro `perp-account-0.1` incorpora motivos explícitos de cancelación por
vencimiento y fin de ventana. Su cancelación ordinaria mantiene el motivo
anterior. LAB continúa en esquema SQL 12 y conserva las tablas existentes.
La ampliación del formato de los registros está identificada en sus payloads.
El software anterior a esta capacidad requiere actualizarse para leer esos
registros v2. Las copias cifradas verifican ambos tipos de perfil y prueba.

Los cálculos y la reproducción siguen ocurriendo fuera del bloqueo de escritura.
La sesión y los originales se comprueban otra vez antes de guardar. Los importes
son decimales de precisión fijada. El gráfico usa números para su representación
visual y conserva los importes exactos en detalle y exportación.

## Alcance

El modo continúa identificado como experimental. Las reglas del instrumento
son las capturas actuales fijadas en el dataset. El toque LIMIT es una hipótesis
de ejecución completa. La cola del libro, su liquidez y los fills parciales
observados requieren otros datos y otro modelo.

Desde v119 están disponibles la [validación de LIMIT](PERP_LIMIT_VALIDATION.md) y
la referencia OOS sin autoridad. Desde v120, las reservas compatibles se pueden
activar en [PAPER observado](PERP_LIMIT_PAPER.md). Desde v121, la revisión
[Bot LIMIT en REAL](PERP_LIMIT_REAL_CORE.md) requiere una prueba PAPER completa.

Desde v126, LAB admite [salidas parciales](DERIVADOS_HISTORICOS_PLAN.md#salidas-parciales-en-lab).
Desde v127, se pueden probar también en [PAPER](PERP_LIMIT_PAPER.md#salidas-parciales).
Desde v128 se pueden activar en [GRVT REAL](GRVT_PERP_PARTIAL_REAL.md). Continúan pendientes las entradas acumuladas y varias posiciones
históricas, reglas históricas acreditadas, los otros adaptadores de nonce,
la aceptación completa del proyecto, el rendimiento de grandes catálogos y el
piloto mainnet configurado por el propietario.

## Evidencia

La regresión de esta entrega pasa 1.067 casos en 79 archivos, con identidades
cotejadas, cuatro procesos terminados con salida 0 y cero fallos, errores u
omisiones. Incluye cálculo, LAB, archivos cifrados, PAPER perpetuo, puertas GRVT,
protección de almacenamiento y lanzador. Los demás módulos conservan su evidencia
previa.

Hay 25 casos nuevos de LIMIT. Los oráculos comprueban precio y fees LONG/SHORT,
CROSS/ISOLATED, órdenes sin toque, vencimiento antes de una apertura favorable,
MARK adverso, funding antes y después de la entrada, calentamiento, sufijo futuro,
trailing sin extremos previos y configuración estricta. Los archivos nuevos
conviven con los antiguos, deduplican y se recuperan desde una copia cifrada.
Los resultados alterados y suplantados con hashes nuevos se rechazan.

Una comparación independiente produce resultados completos idénticos en 32
escenarios de mercado entre v117 y la candidata, incluidos diarios, métricas,
perfiles y supuestos. Cinco recorridos de navegador cubren mercado, LIMIT,
validación, PAPER observado y activación del bot con proveedores controlados.
Los cuatro primeros cierran y reabren los datos en otro proceso. Se verifican
los marcadores reales del gráfico, el precio de entrada, la precisión del
formulario, exportación, copia y rechazo de resultados alterados. Se corrigieron
dos comprobadores de interfaz durante el desarrollo y se conservaron sus logs.

La referencia de publicación está en
`~/.local/state/orbis/verification/v118-perp-limit-entries-r1/verification.json`.
La regresión está en `perp-limit-regression-r1/`, los recorridos en
`perp-limit-browser-final-r1/` y la comparación en
`perp-limit-legacy-compatibility-r1/`, dentro del directorio de verificación.
