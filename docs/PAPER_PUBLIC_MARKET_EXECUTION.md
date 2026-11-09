# Órdenes PAPER con cotización pública validada

## Contrato

`POST /paper/market/buy` y `POST /paper/market/sell` reciben `request_id`,
activo, divisa, proveedor y tamaño/comisión. **No aceptan `price`.** El
servidor consulta `/market/snapshot` internamente para el par exacto. Una
fuente ausente, sin hora de recepción, caducada (>60 s), con hora de mercado
atrasada/inválida/futura o con precio no representable por el Ledger bloquea
la operación (`503`, `code=market_quote_unavailable`) sin crear asientos.
El servidor revalida la edad después de reclamar el ID en SQLite, por si
esperó a otro escritor.
Con el claim adquirido, también recarga el escenario PAPER: un cierre o
cambio de límites ocurrido durante la consulta del precio se respeta antes
de decidir la operación. Si la sesión se cerró, conserva el intento denegado
y la evidencia de la cotización, pero no inserta una operación financiera.

Las cantidades aceptadas se ajustan conservadoramente a los 12 decimales del
libro: en compra se truncan hacia abajo las unidades recibidas; en venta, el
efectivo neto. Nunca se crean unidades ni efectivo por redondeo hacia arriba.
La respuesta incluye `rounding_residual_quote`, reconstruible desde el asiento
(`gasto − comisión − unidades×precio` en compra;
`unidades×precio − comisión − efectivo` en venta). Es una diferencia de
precisión en la divisa cotizada, no rendimiento ni una comisión adicional.
En compra se rechaza si el residuo supera el 0,01 % del importe neto: una
orden diminuta no debe perder una fracción importante sólo por la escala del
libro. Este límite de seguridad es fijo hasta migrar el almacenamiento.
El P/L realizado de una venta se calcula con el efectivo neto **asentado**, no
con el producto teórico `precio×unidades`; así coincide con el cambio de caja
incluso cuando se trunca una fracción submínima.

Para compra se toma `ask`, para venta `bid`. Si ese lado no está publicado,
se usa el último precio como **aproximación**, identificada en la respuesta y
en `paper_market_evidence`. Si el proveedor no publica la hora del evento,
también se marca como aproximación: la hora de recepción local no demuestra
por sí sola la frescura del mercado. El precio final puede diferir de la
referencia mostrada antes del clic; la respuesta enseña el precio realmente
usado y su proveedor. Ninguna orden llega al exchange ni usa dinero real.

## Idempotencia y auditoría

El digesto de la solicitud depende de lado, par, tamaño, comisión y proveedor,
pero **no** del precio variable. Repetir el mismo `request_id` con la misma
propuesta devuelve el resultado anterior sin consultar otra vez el mercado;
reutilizarlo con otra propuesta da `409`. El claim, la evaluación de riesgo,
la operación y `paper_market_evidence` se confirman o revierten juntos. La
evidencia conserva símbolo, proveedor, fuente, precio exacto en texto, base
`ask`/`bid`/`last`, recepción local y hora/estado del evento de mercado.
El formato actual del digesto no redondea el decimal al serializarlo. Para
reintentos históricos acepta el formato anterior sólo si los valores tenían
como máximo 28 dígitos significativos; un ID antiguo con mayor precisión no
puede validarse sin riesgo de colisión y responde `409`.
Si la consulta de precio falla mientras otro intento con el mismo ID confirma
la operación, el servidor relee el resultado guardado antes de responder. Un
`503` sigue siendo potencialmente provisional si el otro escritor aún no ha
confirmado; la interfaz conserva el ID y pide reintentar con los mismos datos,
sin afirmar que no haya habido operación.

Las rutas `/paper/buy` y `/paper/sell` anteriores siguen existiendo para
compatibilidad con operaciones manuales heredadas; aceptan precio del cliente
y **no** son aptas para seguimiento automático ni ejecución real. Las vistas
Operativa y Estudio usan exclusivamente las rutas nuevas. El aislamiento
PAPER/REAL no cambia.
En Operativa, el precio que aparece antes de enviar se etiqueta «Precio ref.»:
sirve para el estimado, no fija el precio de la orden. Tras responder el
servidor se muestra el «Precio aplicado PAPER» junto a la fuente de la
cotización realmente utilizada. El tamaño se transmite como cadena decimal,
sin pasar por el tipo numérico de JavaScript.
Los estimados previos se calculan en el navegador con enteros de escala
decimal (`decimal.js`) y se etiquetan como informativos: no convierten importe,
comisión, cantidad ni precio a `Number`. La cantidad de compra y el neto de
venta se truncan a la unidad mínima del Ledger (`10^-12`); la vista presenta
un máximo de ocho decimales de activo y dos de EUR/USD. Una entrada inválida
no se interpreta como comisión cero. Guard y el motor vuelven a calcular y
validar la orden con una cotización nueva al enviarla.
La ruta de sólo lectura `/paper/market/preview` consulta el mismo proveedor
PAPER y aplica el mismo validador de frescura y procedencia a `ask` para
compra y `bid` para venta. Si falta un lado se indica la aproximación `last`;
si la cotización no es válida, no muestra precio utilizable. La previsualización
no reserva precio, no crea operaciones y no sustituye la nueva validación al
enviar la orden.
Cada lado se valida por separado: un `ask` inválido no oculta un `bid` válido
ni bloquea la venta PAPER, y viceversa. Un error del proveedor deja ambos
lados sin precio, sin convertirlo en una orden ni en una respuesta engañosa.

Los importes financieros de las respuestas PAPER (operaciones, escenarios,
cartera, precios y resúmenes) se serializan como cadenas decimales. Un número
JSON grande pasaría por coma flotante y perdería dígitos; los clientes deben
tratar esas cadenas como importes, no como números binarios. La interfaz
formatea las cadenas directamente para mostrar dinero y cantidades.

## Límites pendientes

- La simulación Spot no modela profundidad, impacto de mercado ni un fill
  garantizado. Sin bid/ask, `last` es una aproximación, no precio ejecutable.
- Las columnas financieras heredadas conservan su esquema `Numeric(30, 12)`.
  ORBIS escribe ahora los decimales nuevos como texto etiquetado dentro de
  esas columnas, evitando que SQLite los convierta a `REAL`; las lecturas de
  filas anteriores siguen disponibles. El motor rechaza cotizaciones fuera
  de escala y tamaños que producirían cero unidades o efectivo. Los dígitos
  que SQLite ya perdió en filas antiguas no se pueden reconstruir. Las
  consultas externas no deben sumar ni ordenar directamente esas columnas:
  deben usar el lector decimal de ORBIS.
- La automatización PAPER de otras familias debe adoptar esta misma
  validación causal donde aplique; una ruta REAL requerirá un contrato y
  controles independientes, no reutilizará estas órdenes ficticias.
