# Métricas derivadas del Laboratorio

## Exposición por vela del backtest (`lab-1.15`)

`100 × velas con posición durante algún tramo / velas evaluadas`. Una entrada
en la apertura cuenta esa vela. Una salida intravela o al cierre también la
cuenta; una salida en la apertura por regla, gap de stop/objetivo o límite de
permanencia **no** la cuenta porque la posición ya no estuvo expuesta durante
el intervalo. Es una fracción de velas, no de segundos ni de capital en riesgo.

Los motores hasta `lab-1.14` contaban la vela de salida en apertura por gap o
límite de permanencia. Sus resultados guardados se reproducen con la versión
original; los runs nuevos usan `lab-1.15`. Esta corrección sólo cambia
`exposure_pct` y la hipótesis declarada: entradas, salidas, P/L, capital y
drawdown conservan la misma semántica.

## Volatilidad por vela del capital (`equity-risk-1.1`)

Para cada par consecutivo de puntos de la curva de capital ficticio, se calcula
el retorno simple `rᵢ = Eᵢ / Eᵢ₋₁ − 1`. La cifra mostrada es
`100 × √[Σ(rᵢ − r̄)² / (n − 1)]`, la desviación típica **muestral** de esos
retornos, en porcentaje. Incluye tramos en efectivo (retorno cero) y cambios
de valor no realizados de posiciones abiertas. No es la volatilidad del activo
subyacente.

Se requieren al menos tres puntos de capital, equivalentes a dos retornos, y
todos los denominadores deben ser positivos. En otro caso se muestra «—» con
motivo en la API. Se usa `Decimal` para el cálculo. La métrica **no se
anualiza**: una cifra por vela de 1 h no se compara directamente con una de
1 día. Tampoco es una predicción de riesgo futuro.

La curva, el dataset, la estrategia y el motor original permanecen intactos.
Esta métrica se deriva al consultar el backtest o la validación guardados, con
una versión analítica propia. Por eso también aparece en pruebas antiguas sin
reescribir su resultado persistido ni invalidar sesiones PAPER ligadas a la
semántica del motor LAB.

## Rentabilidad anualizada descriptiva (`equity-risk-1.1`)

Para una curva LAB sin aportaciones y con **al menos 365 días** entre el
primer y el último punto, se calcula
`100 × [(capital_final / capital_inicial)^(365 días / duración) − 1]`.
Se exige orden temporal estricto y capital positivo y finito en todos los
puntos; de otro modo no se publica la cifra. Usa `Decimal` y no reescribe
ningún backtest. Periodos inferiores a un año quedan sin anualizar, para no
presentar una operación breve como si acreditara un rendimiento anual.
La cifra sigue siendo histórica y teórica: no predice resultados futuros ni
vuelve comparables distintos regímenes, costes o mercados.

## Frecuencia de señales LAB frente a PAPER

`100 × señales de entrada / velas evaluadas`. En LAB, el numerador son los
eventos de estructura de un backtest verificado (incluye señales descartadas
por posición abierta) y el denominador son las filas de su curva de capital:
excluye velas previas al inicio efectivo de evaluación. En PAPER, el numerador
son los cierres observados con entrada verdadera y el denominador son los
cierres efectivamente observados, sólo si todas esas señales superan el replay
de la versión LAB. Se calcula con `Decimal`; la UI aproxima a un decimal.
Cada tasa incluye, cuando las marcas son válidas y están ordenadas, la primera
y última marca de vela realmente evaluada (UTC) en una ayuda accesible por
foco. El intervalo LAB procede de la curva de capital ya recortada al inicio
de evaluación; el PAPER procede de cierres observados, no de velas
reconstruidas ni de la hora de ejecución. Si faltan marcas consistentes, se
omite el periodo en vez de inferirlo desde las señales.

No se calcula cuando falta backtest verificado, no hay velas PAPER o el replay
falla. Una muestra LAB o PAPER con menos de 30 velas evaluadas **o** menos de
30 señales lleva aviso. El 30 es sólo un umbral visual de cautela, **no** una
prueba estadística. Cuando las marcas temporales están disponibles, las señales
en velas consecutivas del intervalo se agrupan en una *racha*: 30 señales
seguidas cuentan como una racha descriptiva, no como 30 casos independientes.
Si hay al menos 30 señales pero menos de 30 rachas, la UI muestra ambas cifras.
Las marcas duplicadas o incompletas impiden publicar ese recuento; una sesión
PAPER con replay fallido no lo muestra. Rachas separadas tampoco demuestran
independencia estadística. Las dos tasas son
descriptivas de periodos/regímenes distintos: no se resta una de otra, no
demuestran pérdida de eficacia ni equivalencia de ejecución o rentabilidad.
