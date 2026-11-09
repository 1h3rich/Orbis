# Histórico de derivados: arquitectura y aceptación

El modelo de mercado v1 conserva archivo compuesto, núcleo, pruebas experimentales, reproducción, gráficos, OOS y PAPER observado. Sus puertas de revisión y ejecución REAL se verifican con proveedores controlados en v110 y en las regresiones posteriores. El piloto mainnet del propietario sigue pendiente.

v118 añade [entradas LIMIT en LAB](PERP_LIMIT_ENTRIES.md), con perfil/motor v2, vigencia y cancelación. Sus formatos de archivo conviven con los de mercado. v119 conecta la [validación LIMIT](PERP_LIMIT_VALIDATION.md). v120 conecta [LIMIT en PAPER](PERP_LIMIT_PAPER.md). v121 incorpora [LIMIT en GRVT REAL](PERP_LIMIT_REAL_CORE.md). v122 añade recuperación por historial de GRVT. Las órdenes y posiciones avanzadas y las reglas históricas acreditadas continúan pendientes. Los apartados históricos de abajo conservan la evidencia y el estado de sus respectivas entregas.

## Confirmación de cierre GRVT

Los lectores TRADE y MARK comprueban `close_time` en nanosegundos. El cierre confirmado coincide con el inicio más la duración del intervalo. Una vela anterior incompleta invalida el tramo. Si la última vela falta o sigue en formación y el resto está completo, los observadores admiten un reintento dentro de los límites de señal y cobertura vigentes. Las descargas LAB requieren el rango completo para guardarlo. Se mantiene el formato de los archivos ya guardados.

## Historial de reglas observadas

Desde v125, en **Laboratorio → Datos de derivados → Historial de reglas GRVT**,
**Capturar reglas** consulta margen y restricciones del instrumento elegido.
Las nuevas descargas de datos también archivan esa pareja en la misma
transacción. El archivo usa LAB cifrado y conserva las marcas exactas de
ambas consultas, sus valores y la huella de cada observación. El historial
compara muestras consecutivas y permite exportar las capturas cargadas en JSON.

Las muestras indican qué respondió GRVT en dos instantes de consulta. No
acreditan desde cuándo regía un cambio ni su vigencia entre capturas. Si dos
observaciones distintas comparten fecha, la comparación queda ambigua. Una
captura nueva verifica que las dos consultas quepan en 30 segundos. Los
datasets antiguos conservan sus muestras en el archivo original y siguen
siendo exportables. Su formato y los cálculos de los backtests se mantienen.
[Consulta pública de reglas por instrumento](https://api-docs.grvt.io/schemas/api_get_margin_rules_request/)

## Sensibilidad a las reglas GRVT

En **Pruebas de derivados**, abre una prueba guardada y luego **Comparar
reglas GRVT**. Elige otra captura archivada del mismo símbolo. ORBIS
reproduce el original con las reglas fijadas en su dataset y calcula el
escenario alternativo con la otra captura. Ambos usan las mismas velas
TRADE y MARK, financiación, ventana, estrategia, apalancamiento y
trayectorias. Cada captura se aplica como supuesto fijo a todo el tramo.

La tabla incluye capital, operaciones, financiación, comisiones,
drawdown y liquidación. La diferencia de capital sólo se muestra con
dos tramos completos hasta el mismo instante. **Exportar comparación**
repite la verificación y descarga un JSON en claro. Contiene las dos
capturas y las huellas de la prueba, perfil, datos y resultados. Para
repetirlo, conserva la prueba original y su captura alternativa. El
informe se recalcula bajo demanda y queda separado de PAPER y REAL.
GRVT permite consultar reglas por instrumento y no recibe una fecha de
vigencia en esa consulta. [Contrato de GRVT](https://api-docs.grvt.io/schemas/api_get_margin_rules_request/).

### Capturas dentro de una prueba

Abre una prueba guardada y despliega **Comparar reglas GRVT**. El panel lee su ventana solicitada con inicio incluido y fin excluido. Usa la hora en que termina cada pareja de consultas. Muestra las observaciones más cercanas fuera del tramo y cuenta los instantes que coinciden, difieren o presentan valores contradictorios dentro del tramo.

La opción **Durante** marca el instante distinto más reciente con valores coherentes. Puede elegirse para un escenario experimental. Cada captura aporta evidencia puntual. La vigencia entre consultas sigue sin acreditarse y el resultado original conserva sus reglas. Si hay más de 2.000 capturas en el tramo o en un límite, hay que acotar la prueba. [Consulta pública de margen](https://api-docs.grvt.io/schemas/api_get_margin_rules_request/).

## Salidas parciales en LAB

En **Pruebas de derivados → Salidas parciales · LAB**, se pueden definir hasta
cinco objetivos tanto para entrada a mercado como LIMIT. **Objetivo %** mide
el movimiento favorable desde el precio de entrada. **Parte %** se calcula
sobre la posición inicial y redondea al lote del instrumento. Los objetivos
van en orden, preceden al objetivo final y dejan cantidad para ese cierre.
El stop, el trailing, la regla de salida y el límite de tiempo cierran el resto.

Cada objetivo ejecutado es una orden simulada reduce-only. Los gaps usan
el precio disponible en el recorrido declarado y los objetivos que no
alcanzan el mínimo quedan registrados. MARK conserva prioridad para liquidación
y la financiación se aplica a la cantidad abierta en su instante. El gráfico
distingue **P1**, **P2** y los cierres completos. Cada operación conserva los
fills parciales, comisiones, resultado y cantidad restante.

Perfil, prueba, exportación y validación v3 conviven con v1 y v2. La validación
puede reproducir sus tramos y abrirlos en el gráfico. Una reserva completa
puede preparar el mismo perfil en [PAPER observado](PERP_LIMIT_PAPER.md#salidas-parciales).
Las entradas LIMIT con parciales se pueden activar en [GRVT REAL](GRVT_PERP_PARTIAL_REAL.md).

## Validación v98

[Contrato y aceptación](PERP_VALIDATION_PLAN.md): reserva fija, ventanas sucesivas
con cuentas independientes, sensibilidad a cuatro pares TRADE/MARK, archivo,
replay completo, copia/reapertura y referencia OOS tipada. **Ver** recupera el
run exacto del tramo y muestra sus marcas en el gráfico común. El modo continúa
siendo experimental; la referencia no concede permisos ni cambia las puertas
REAL existentes. El PAPER compatible, pendiente entonces, se incorpora en v103.

## Integración v97

[Pruebas de derivados](PERP_EXPERIMENTS_PLAN.md) publica perfil y ejecución
inmutables, coste/funding/margen/liquidación que modifican el saldo, gráfico
TRADE/MARK con marcas, curva, diario, exportación y reapertura cifrada. El motor
`perp-ohlc-1.0` sólo admite `EXPERIMENTAL_CURRENT_RULES`; sus trayectorias son
hipótesis explícitas, sin supuesto de fills observados ni reglas históricas.

La regresión final reúne 4.230 casos por identidad y 50 recorridos de navegador.
La tanda inicial detecta una aserción obsoleta de esquema desconocido; se corrige
sólo su archivo y se repiten sus tres casos. Aplicación y recorridos permanecen
idénticos. La evidencia cargada es `v97-perp-engine-r2/verification.json` en el
registro local. Una consulta pública de cuatro velas BTC completa el modelo con
una operación hipotética; no utiliza cuentas ni fondos y no prueba rentabilidad.

El dataset conserva `historical_execution_model: NOT_RUN` porque identifica los
inputs originales, no cada uso posterior. Las pruebas son registros separados
ligados al hash del dataset; ejecutarlas no reescribe el archivo ni concede
autoridad. Faltan validación fuera de muestra, sensibilidad de trayectorias,
PAPER compatible, órdenes avanzadas y datos de reglas históricas acreditados.

## Evidencia actual

`lab.engine` publica `lab-1.16`: señales sobre velas cerradas, entrada en apertura
siguiente y costes configurables. Sus SHORT son teóricos. El motor
de señales anterior utiliza histórico TRADE; v94 añade descarga y archivo MARK
independiente mediante `GrvtAdapter`.
En v94, el nuevo archivo conserva MARK separado, TRADE, financiación con ancla
y capturas actuales de reglas/instrumento. `derivatives.isolated` y `margin_threshold` calculan contabilidad y umbral teórico
sobre una foto. `derivatives.funding` calcula los pagos; el PAPER existente ya
los registra mediante `AutoFunding` y `PaperPerpStore.settle_funding`, conserva
cobertura/anchors y ajusta el resultado de posiciones cerradas. Su modelo aislado,
sus estados de riesgo y su contabilidad conservan la semántica publicada y no
son el libro nuevo. La investigación GRVT v1 declara expresamente
`PRICE_RULES_WITHOUT_DERIVATIVES_MARGIN_OR_FUNDING`. Estos contratos se conservan.

Fuentes primarias revisadas el 5 de noviembre de 2026:

- [Tipos de velas](https://api-docs.grvt.io/schemas/api_candlestick_request/):
  TRADE y MARK son tipos distintos del mismo instrumento/intervalo. La API
  pagina en sentido inverso; el motor debe verificar toda la cobertura pedida.
- [Mercado y financiación](https://api-docs.grvt.io/market_data_api/#funding-rate):
  los registros de liquidación de funding incluyen instante, marca, tasa y
  duración. El adaptador existente ya distingue porcentajes de fracciones.
- [Consulta de reglas](https://api-docs.grvt.io/schemas/api_get_margin_rules_request/):
  el contrato documentado pide instrumento, sin parámetro histórico. Por tanto,
  una consulta actual no acredita la vigencia de esas reglas en todo el pasado.
- [Margen](https://help.grvt.io/en/articles/10255049-what-are-the-margin-requirements-on-grvt):
  la salud de la cuenta cross y las órdenes abiertas afectan al margen exigido;
  el margen de una posición aislada no sustituye ese cálculo de cuenta.
- [Liquidación](https://help.grvt.io/en/articles/10255070-what-s-the-liquidation-process-on-grvt):
  la documentación distingue liquidación completa aislada y cross. Alcanzar
  un umbral no equivale a vender al precio exacto del umbral conservando el resto.

Las reglas cambian: cada ejecución conservará datos y supuestos fijados. La falta
de reglas históricas verificadas exige identificar el modo experimental, sin
atribuirle la fidelidad de una reconstrucción realista de GRVT.

## Decisiones

1. Nuevo motor versionado independiente de `lab-1.16`. Compartir evaluador de
   señales e indicadores, sin modificar resultados antiguos ni PAPER existente.
2. Dataset compuesto inmutable: TRADE para señal/ejecución modelada, MARK para
   margen, liquidaciones de financiación, reglas de instrumento y comisiones.
   Identidad incluye proveedor, instrumento, tipo de precio, periodo, cobertura,
   captura, origen y hash. Nunca cachear MARK bajo la identidad de TRADE.
3. Cronología explícita de órdenes, fills, marcas, financiación y cambios de
   reglas. Decisión sólo con información disponible. Instantes iguales requieren
   secuencia conocida o una ambigüedad registrada; no se supone el orden rentable.
4. Decimal con contexto propio, cuantización de tick/tamaño y validación de rango.
   El capital no se multiplica por apalancamiento: éste limita la exposición y
   consume margen. Los costes y pagos sí alteran el saldo y la capacidad de abrir.
5. Aislado y cross como modos explícitos. Mantener libro de caja, posición neta
   por instrumento, órdenes y reservas. Apertura, aumento, reducción, reversión,
   cierres parciales y `reduce_only` tienen asientos e invariantes distintos.
6. Comprobar margen al admitir la orden y antes del fill; mantenimiento tras
   marcas, financiación y cambios de reglas. Una orden rechazada/cancelada no
   genera fills. Límites de posiciones y exposición se comprueban en cuenta.
7. Liquidación modelada como evento separado: cancela las órdenes correspondientes,
   registra pérdida de colateral/transferencia al fondo según el modo, costes y
   déficit si procede. No limitar silenciosamente una pérdida al saldo para
   esconder deuda o datos insuficientes. Con OHLC, marcar incertidumbre intravela.
8. Persistir en LAB cifrado mediante migración aditiva y copias verificadas.
   Dataset, configuración, motor y eventos fijados participan en la reproducción,
   comparación, exportación y reserva fuera de muestra. Ninguna prueba concede
   permisos REAL. La investigación nueva necesita otro formato; v1 no cambia.

## Tramos implementables

| Tramo | Resultado de usuario | Verificación necesaria |
| --- | --- | --- |
| Datos | Cargar TRADE + MARK + funding con origen y reglas/supuestos visibles | Paginación inversa, tipos aislados, huecos, duplicados, velas abiertas, unidades y hashes |
| Motor | Probar LONG/SHORT con margen y financiación que cambian realmente el saldo | Cálculos independientes a mano; aislado/cross, aumentos, reducción, reversión, órdenes y liquidación |
| Archivo | Guardar, reproducir, comparar y recuperar tras cerrar | Alteración de datos rechazada, migración/copia cifrada, versión anterior intacta, cero autorun |
| UI y reserva | Elegir modo, inspeccionar eventos en gráfico, comparar dentro/fuera de muestra | Primer uso desde cero, marcas de entrada/salida/liquidación, costes y brechas visibles, escritorio/móvil |
| PAPER y autoridad | Usar semántica compatible al pasar de investigación a PAPER y después revisar REAL | No heredar capital ficticio, originales repetidos, formato nuevo, permisos/límites/STOP sin relajación |

No se presentan tramos internos como funcionalidades completas en la UI. La
primera tarea es incorporar el histórico MARK sin alterar las consultas TRADE,
con pruebas de tipo, cobertura y procedencia. Después se construye el motor y
se verifica el recorrido completo; un adaptador o una fórmula aislados no cierran
el requisito de backtesting de derivados.

## Archivo de datos v94

`perp_dataset` define `orbis.lab.perp.dataset.v1` con tipado estricto, fuentes y
clases de precio explícitas. Requiere ventanas TRADE/MARK idénticas y completas;
financiación con ancla anterior, cobertura del extremo final y unidades originales.
Los nanosegundos se conservan como cadenas para exportación exacta en navegador.
Las reglas se capturan dentro del intervalo de consulta, pero se identifican
`CURRENT_SNAPSHOT_NOT_HISTORICAL`. `NOT_RUN` impide confundir el archivo con un resultado.

LAB 9→10 añade únicamente `perp_datasets`; no inserta estos datos en `datasets`,
por lo que no pueden seleccionarse accidentalmente en un backtest de precios ni
activar PAPER o REAL. Cada lectura y copia verifica estructura y SHA-256. No hay
bloqueo SQLite durante HTTP; un bloqueo/reemplazo de LAB durante la descarga
impide guardarla después. La UI ofrece descarga, consulta y exportación, con
«Datos · sin backtest de margen» visible. Los modelos anteriores se conservan.

Una consulta pública real de tres horas de BTC verificó TRADE, MARK, financiación
y snapshots; detectó que el último tramo de riesgo puede omitir `notional_cap`,
como documenta la API. Se admite esa ausencia como límite superior abierto. No se
usan credenciales de exchange ni se mueven fondos. Las pruebas propias incluyen
SQLCipher, copia, reapertura en otro proceso, bloqueo, corrupción y respuesta
atrasada. La cobertura final por identidad reúne 4.108 casos: la tanda inicial
pasa 4.107, falla un montaje antiguo y, corregido sólo ese test, se repiten los
91 casos de su archivo. Los 48 recorridos de navegador pasan con la misma
aplicación. Evidencia cargada: `v94-perp-data-r3/verification.json` bajo el
registro local de verificación. El legado local LAB sigue plano: el nuevo
archivo exige cifrado. En v95 se añade su migración desde Ajustes, comprobada
con archivos propios; el propietario todavía no ha enviado ese formulario.

## Diseño previo a v97: ejecución y aceptación financiera

El diseño siguiente se conserva como historia de la decisión. Perfil, cronología,
archivo y gráficos se implementan en v97 según su contrato publicado; OOS,
PAPER y reglas históricas continúan pendientes:

- Un perfil perpetuo versionado referenciará la versión LAB de señales y su propia
  configuración de ejecución. Mantendrá capital, apalancamiento, modo de margen,
  comisiones, granularidad, órdenes y política intravela fijados. Así se comparte
  el evaluador sin añadir campos que los lectores `lab-1.16` interpretarían mal.
- Cada ejecución fijará perfil, dataset compuesto, motor, reglas asumidas y diario
  de órdenes/fills/marcas/funding/liquidaciones. Guardar un perfil, reproducir un
  resultado o copiarlo a PAPER nunca debe conceder autoridad REAL.
- El libro tendrá caja libre, colateral aislado, posiciones netas por instrumento,
  órdenes y reservas. Una orden se valida al entrar y antes de su fill; una
  ejecución parcial, reducción, aumento o reversión deja sus asientos exactos.
- La financiación se asentará en el saldo/colateral que corresponda, con tasa
  expresada en puntos porcentuales convertida una sola vez. No bastará mostrar
  una estimación al lado de un P/L que siga ignorándola.
- El margen inicial, el mantenimiento y la liquidación serán eventos diferentes.
  En el modelo de liquidación completa no se registrará una venta voluntaria al
  umbral que conserve un remanente ficticio. Se explicitarán transferencia al
  fondo y déficit cubierto o deuda según el modelo elegido.
- Las velas no prueban el orden de eventos dentro de ellas. Una apertura, cierre,
  stop o funding en el mismo instante exige una secuencia observada o un supuesto
  de ejecución identificado como tal. Una cotización TRADE no sustituye MARK.
- Mientras se utilicen capturas actuales de reglas, el modo será experimental;
  la UI no podrá ofrecerlo como reconstrucción histórica realista de GRVT. La
  capacidad de recibir reglas históricas con vigencia y procedencia queda en el
  contrato de datos futuro, sin inventarlas a partir del endpoint actual.
- Persistencia, copia cifrada, reproducción y reserva fuera de muestra deben
  alcanzar el mismo perfil exacto. PAPER y revisión REAL necesitarán nuevos
  contratos de procedencia; no se cambia el significado de la referencia GRVT v1.

Casos de aceptación independientes para el libro, antes de conectar la UI:

| Caso | Resultado que debe comprobarse con cifras manuales |
| --- | --- |
| Compra LONG / venta SHORT | El nocional no se suma al patrimonio; caja, colateral, fee y exposición se distinguen |
| Cierre parcial y aumento | P/L sólo sobre la parte cerrada; coste de la posición restante y reservas correctos |
| Reversión | Cierre de la posición anterior y apertura del exceso, con costes explícitos |
| Orden pendiente / cancelada / reduce-only | Reserva de margen coherente; sin fill no hay posición; reduce-only no aumenta ni invierte exposición |
| Funding positivo y negativo | Cargo/cobro firmado correcto; modifica equity y puede provocar un evento de margen |
| Aislado frente a cross | La pérdida de una posición aislada no consume otros bolsillos; cross usa la salud de la cuenta |
| Liquidación con equity restante o déficit | Se registra el destino del remanente y la cobertura/deuda; no se oculta una pérdida mediante truncado |
| Cambios de reglas y tiempos iguales | Sólo se usa la versión vigente según datos/supuestos; ninguna secuencia se presenta como observada sin evidencia |
| Reapertura, edición y reproducción | Mismos datos/perfil/motor producen mismos eventos; una edición no cambia resultados anteriores |

La lista de archivos perpetuos actual presenta los 100 más recientes. La
paginación y medición con muchos archivos pertenecen a la aceptación pendiente
de rendimiento; no se afirma una biblioteca ilimitada desde la UI actual.

## Libro de ejecución: contrato interno del siguiente tramo

Implementado el 5 de noviembre como núcleo interno `perp-account-0.1`, todavía
sin backtest publicado ni botones operativos. `lab.perp_account` y
`lab.perp_book_types` no alteran el libro GRVT PAPER existente ni las métricas
guardadas con `lab-1.16`. Son transiciones deterministas de estado y eventos,
sin HTTP, reloj del sistema, SQLite ni firma. La persistencia y la reproducción
consumirán el mismo núcleo posteriormente.

Estado: efectivo no asignado, un bolsillo por posición aislada, posiciones netas
por instrumento, órdenes con cantidad pendiente y reservas, marcas y versiones
de reglas. Los acumulados separarán P/L realizado, comisiones, financiación,
transferencia al fondo y déficit cubierto. El nocional nunca se sumará al saldo.
Cada evento tendrá secuencia explícita; dos sucesos con el mismo instante sin
secuencia observada necesitarán una política experimental fijada en el perfil.

- En cross: equity = efectivo + P/L no realizado cross. El capital asignado
  a posiciones aisladas queda excluido de esta garantía.
- Para reservar órdenes se consideran ambos extremos del instrumento:
  `max(q, 0) + compras_pendientes` y `min(q, 0) - ventas_pendientes`.
  Se toma el mayor margen de esos extremos y se suma entre instrumentos.
  Las reservas de comisión son explícitas. La misma revisión se repite antes
  de cada fill parcial; una admisión anterior no garantiza ejecución futura.
- La documentación de margen y la de apalancamiento configurable no expresan
  de forma consistente el mínimo/máximo aplicado a tasas. El perfil experimental
  declarará `max(tasa_inicial_del_tramo, 1/apalancamiento)`; no lo presentará
  como una reproducción acreditada del algoritmo privado del exchange.
- La posición neta guarda coste de entrada, no sólo una media redondeada.
  Aumentar conserva el coste previo; reducir realiza únicamente la fracción
  cerrada; invertir cierra primero y abre el exceso. `reduce_only` no puede
  abrir, aumentar ni invertir y se revalida después de otros fills.
- La financiación pública en puntos porcentuales se divide una sola vez
  entre 100 y se carga/cobra en el efectivo cross o el bolsillo aislado.
  Tras asentarla se comprueba mantenimiento, incluso sin cambio de precio.
- La liquidación completa es un asiento diferente de una venta voluntaria.
  Un bolsillo aislado pierde todo su colateral; cross pierde efectivo y
  posiciones cross. El remanente y el déficit a la marca se registran como
  transferencia al fondo o cobertura, respectivamente. Otros bolsillos aislados
  permanecen separados. Las órdenes canceladas no producen fills.
- El núcleo usará Decimal con contexto propio. Antes de publicar un formato
  se fijarán precisión, redondeo de liquidación y asignación del coste residual;
  una división repetida no puede crear saldo, borrar deuda ni dejar una posición
  de cantidad cero con coste o colateral pendiente.

Pruebas independientes del adaptador: entrada LONG/SHORT con fee, aumento a
precios distintos, cierre parcial y reversión, varias órdenes en ambos sentidos,
cancelación y reserva liberada, reduce-only después de otro fill, rechazo
pre-trade por marca/reglas, funding positivo/negativo, liquidación tras funding,
cross con varias posiciones y aislamiento frente a otro bolsillo. La identidad
contable se comprobará después de cada transición, no sólo en el saldo final.

Fuentes primarias revisadas de nuevo: [margen de GRVT](https://help.grvt.io/en/articles/10255049-what-are-the-margin-requirements-on-grvt),
[apalancamiento configurable](https://help.grvt.io/en/articles/10393222-how-is-configurable-leverage-calculated)
y [liquidación completa](https://help.grvt.io/en/articles/10255070-what-s-the-liquidation-process-on-grvt).
Estos documentos describen el comportamiento actual; no acreditan reglas
históricas ni orden intravela de un backtest.

### Implementación del núcleo v96

El libro admite varios instrumentos con un modo fijo por instrumento y una
posición neta por cada uno. Órdenes MARKET/LIMIT incluyen límite protector;
el núcleo sólo asienta fills explícitos y no inventa un motor de matching.
Se reservan ambos extremos pendientes y costes, y se revisan de nuevo margen,
límites y exposición antes y después del fill. `reduce_only` puede cerrar una
posición sana por mantenimiento aunque esté por debajo del nuevo margen inicial;
no abre ni invierte. Otra ejecución puede cancelar órdenes de reducción obsoletas.

Las posiciones conservan coste restante. Aumentos aislados cubren margen inicial
y pérdida inmediata de ejecución desde caja, sin usar beneficios aislados no
realizados como efectivo disponible. Al reducir, el colateral se libera en
proporción y una pérdida consume sólo ese bolsillo. Un fill voluntario que
produciría equity negativa se cancela antes de asentarlo; no genera una cobertura
ficticia del fondo por elegir un precio de ejecución extremo.

La financiación modifica caja o colateral. Un mismo instrumento/instante sólo se
asienta una vez, incluso si la primera liquidación no tenía posición. Un lote MARK
de todos los instrumentos en el mismo instante precede a órdenes, fills, funding
y cambios de reglas; la secuencia explícita resuelve el orden del modelo, sin
presentarlo como observado. Mark, financiación, reglas o comisiones de un fill
pueden producir liquidación completa. El diario distingue ésta de una venta,
registra el remanente transferido al fondo y el déficit cubierto, y conserva
otros bolsillos aislados al liquidarse la cuenta cross.

Política numérica interna: contexto propio de 120 dígitos y flags/traps fijados;
liquidación monetaria a 18 decimales. Comisiones y reservas iniciales redondean
hacia arriba; P/L, funding y reparto de coste/colateral usan HALF_EVEN. El último
cierre consume todo el residuo de coste y colateral. Esta precisión es una
hipótesis experimental, no una afirmación sobre la precisión USDT de GRVT.

`test_perp_account.py` aporta 50 casos, incluidos los oráculos de abajo, datos
inexactos rechazados, lecturas bajo otro contexto Decimal, cierres sucesivos con
coste fraccionario, dos secuencias aleatorias reproducibles y comparación de cada
delta del diario con caja, colateral, P/L, fees, funding y fondo. Pasan 382 casos
de 16 archivos con las regresiones de PAPER, LAB, cifrado, lanzador y fronteras.
Los cuatro recorridos actuales de navegador también pasan. La consolidación por
identidad reúne 4.179 casos y conserva 45 de los 49 recorridos anteriores; no es
una repetición completa de todos ellos en esta entrega.

Una medida del núcleo procesa 1.000 órdenes y 3.000 transiciones en 0,234 s en
este equipo. No mide descarga, persistencia, reconstrucción OHLC ni fluidez UI;
la aceptación de rendimiento de la biblioteca completa sigue pendiente.

En v96 faltaban perfil, planificación, archivo, reproducción, OOS, gráfico y
PAPER. v97 conecta los primeros elementos a la aplicación; OOS y el paso
compatible a PAPER siguen pendientes.
Los formatos de investigación y permisos REAL existentes permanecen intactos.

### Oráculo contable independiente

Caso de prueba del libro propuesto, no un backtest: capital 1.000 USDT, comisión
0,1 % por fill, apalancamiento 5, margen inicial constante 20 %, mantenimiento
5 %, sin financiación en esta secuencia. Comisión pagada desde efectivo y
colateral aislado reasignado proporcionalmente en las reducciones. Cada fill
usa una marca igual a su precio y el aumento cubre el margen del tamaño neto
a esa marca (3 × 130 × 20 % = 78). Los precios
son artificiales y las cifras siguientes se calculan a mano.

| Fill | Posición final / coste | P/L realizado acumulado | Fees acumuladas | Efectivo cross | Efectivo aislado + colateral |
| --- | --- | ---: | ---: | ---: | ---: |
| BUY 2 a 100 | LONG 2 / 200 | 0 | 0,20 | 999,80 | 959,80 + 40 |
| BUY 1 a 130 | LONG 3 / 330 | 0 | 0,33 | 999,67 | 921,67 + 78 |
| SELL 1 a 140 | LONG 2 / 220 | 30 | 0,47 | 1.029,53 | 977,53 + 52 |
| SELL 3 a 90 | SHORT 1 / 90 | -10 | 0,74 | 989,26 | 971,26 + 18 |

Al último precio de ejecución, ambos modos deben dar equity 989,26; su distinto
reparto entre efectivo y colateral no cambia el rendimiento. Una financiación
posterior de +0,1 % con mark 90 paga -0,09 para ese SHORT: aumenta el efectivo
cross o el bolsillo aislado en 0,09 y la equity pasa a 989,35.

Caso aislado independiente: después sólo del primer BUY, mark 84 deja equity
del bolsillo 8 y mantenimiento 8,4. La liquidación completa deja efectivo
959,80, posición/colateral cero, P/L realizado -32, fees 0,20 y transferencia
al fondo 8. Con un salto directo a mark 70, el bolsillo tiene déficit 20:
se conserva efectivo 959,80, P/L realizado -60 y cobertura del fondo 20;
ninguno de esos resultados es una venta voluntaria al precio umbral.

Caso de reservas cross: posición LONG 1, mark 100, compras pendientes 1,5 y
ventas pendientes 2,6. Los extremos son LONG 2,5 y SHORT 2,6: a una tasa inicial
del 20 % reservan 52, más 0,41 para fees al mismo precio guardado. Cancelar la
venta reduce el requisito a 50 + 0,15. Una cantidad pendiente nunca cuenta
como fill ni altera caja, coste de posición o P/L realizado.
