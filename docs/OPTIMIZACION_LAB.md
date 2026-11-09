# Barrido acotado de parámetros LAB

El primer optimizador (`grid-stop-target-1.0`) explora únicamente distancias
de stop loss y take profit sobre **una versión de estrategia ya guardada**.
Capital, asignación, comisiones, slippage y reglas permanecen fijos. Admite
de uno a tres valores distintos para cada parámetro: como máximo nueve
backtests de desarrollo por exploración. Los valores se expresan como
fracciones (`0.02` equivale al 2 %). Para SHORT sintético el objetivo debe
ser menor del 100 % del precio de entrada.

El histórico sin huecos se divide cronológicamente como en la validación
fuera de muestra: al menos 30 velas por tramo. Cada candidato se ejecuta
sólo en desarrollo. Se consideran aptos los que producen diez operaciones
o más; entre ellos se elige el mayor beneficio neto después de costes. Los
empates se resuelven por menor stop y luego menor objetivo. Si ninguno es
apto, no se consulta la reserva.

El candidato elegido se ejecuta **una vez** sobre la reserva. Los indicadores
pueden usar el prefijo anterior al corte, conocido en aquel momento, pero
capital y posiciones empiezan de cero en la reserva. No se prueban los otros
candidatos allí. Se conservan la cuadrícula, métricas de desarrollo, versión
de estrategia, huella del dataset, corte, versión del optimizador y resultado
de reserva en SQLite LAB v5. La migración v4→v5 sólo añade una tabla en una
transacción; no reescribe runs previos.

Este diseño limita el sesgo de selección dentro de una exploración, pero
**no elimina el sobreajuste**: repetir cuadrículas o mover el corte después
de ver la reserva contamina la prueba. Tampoco es walk-forward, no modela
liquidez ni garantiza resultados futuros. Nunca crea órdenes PAPER o REAL.
