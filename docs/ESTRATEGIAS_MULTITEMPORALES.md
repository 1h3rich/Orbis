# Condiciones multitemporales en LAB y PAPER

Desde el motor `lab-1.13`, una condición puede declarar `timeframe` (por
ejemplo, `4h`) además de su `kind` y `period`. Si se omite, usa la temporalidad
base del dataset. Los grupos Y/O pueden combinar condiciones base y superiores,
tanto para entrada como para salida. La temporalidad superior debe ser un
múltiplo entero de la base; no se interpola ni se consulta otro proveedor.

El motor forma cada vela superior a partir de todas sus velas base contiguas y
alineadas a UTC. Usa la primera apertura, el máximo de máximos, el mínimo de
mínimos, el último cierre y la suma de volumen. **La vela superior no existe
para la regla hasta cerrar la última vela base de su intervalo.** Mientras el
intervalo superior está abierto, se usa sólo el intervalo superior anterior,
si estaba completo; un hueco impide reutilizar una vela obsoleta como señal
actual. La explicación guarda `base_timeframe`, `timeframe` y
`higher_candle_t` (apertura UTC de esa vela superior).

LAB evalúa la señal al cierre de la vela base y simula la entrada en la
siguiente apertura. Spot PAPER y GRVT PAPER comparten el evaluador; sólo
observan velas cerradas cuando llegan, y su ejecución ficticia sigue sus
respectivos modelos de bid/ask y cobertura. Un backtest `lab-1.12` o anterior
permanece reproducible con su versión; ninguna estrategia con `timeframe`
puede ejecutarse con un motor anterior a `lab-1.13`.

Este mecanismo no crea datos históricos nuevos ni elimina la necesidad de
una muestra suficiente. Una EMA de 200 periodos en 1d necesita al menos 200
velas diarias completas derivables del histórico base. El editor permite
guardar la definición antes de elegir dataset; al ejecutar o activar PAPER
se rechaza una pareja de temporalidades incompatible.

Al activar Spot PAPER o GRVT PAPER, ORBIS también comprueba la capacidad
máxima de su sesión de 5000 velas base. Cuenta las velas superiores UTC que
podrían completarse desde el inicio del histórico, incluso si todos los datos
futuros llegaran sin huecos. Si una regla de entrada o salida no pudiera
alcanzar el calentamiento mínimo de su indicador antes de ese límite, rechaza
la activación e indica la regla y el máximo posible. Una regla que sí pasa
esta comprobación puede seguir sin estar lista hoy: esto demuestra sólo que
no es imposible por capacidad. En retesteos y estructuras, cuyo mínimo
suficiente no es fijo, sólo se exige que quepa al menos una vela superior;
no se presume que vaya a aparecer una señal.

Después de verificar un backtest, el informe cuenta las velas superiores
completas por condición. Para RSI, medias, bandas, ATR, volumen y cruces
muestra también el mínimo de calentamiento. Si una EMA 200 de 4h sólo tiene
cuatro velas 4h completas, aparece `4/200 · insuficiente`: cero señales no
se presenta como evidencia de que la hipótesis falló. Para retesteos y
estructuras se muestra sólo la cobertura, sin afirmar que un número de velas
garantice que la estructura sea evaluable. Este diagnóstico se recalcula
desde el dataset y la versión verificados; no modifica el run inmutable ni
su huella.

El detalle de una sesión Spot PAPER o GRVT PAPER vuelve a contar la cobertura
desde las velas que esa sesión ha guardado. Una etiqueta ámbar como
`Entrada · 4h 1/2 · calentando` significa que esa condición todavía no tiene
histórico suficiente, no que la hipótesis haya fallado. Cuando alcanza el
mínimo aparece `evaluable`, que sólo acredita calentamiento, nunca una señal
ni una ejecución. Las condiciones de estructura sin mínimo fijo se etiquetan
`cobertura`. Las estrategias exclusivamente de marco base no reconstruyen
este diagnóstico.
