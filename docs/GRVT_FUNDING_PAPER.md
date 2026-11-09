# Financiación GRVT en PAPER: estimación y asentamiento ficticio

Esta consulta utiliza registros públicos de financiación del instrumento
perpetuo de GRVT. No es el libro de cargos de una cuenta real GRVT. La
consulta «Financiación» es **una estimación**; «Asentar en PAPER» cambia el
libro ficticio local. Para posiciones AUTO PAPER, un observador local invoca
ese mismo asentamiento periódicamente y al cierre; las posiciones manuales
siguen requiriendo la acción explícita. No se envía orden ni pago a GRVT.

## Cálculo y procedencia

GRVT expresa `funding_rate` en puntos porcentuales. Para cada liquidación
posterior a la apertura y anterior al fin de la posición:

```text
pago estimado = tamaño base firmado × mark de liquidación × funding_rate / 100
```

El tamaño es positivo para LONG y negativo para SHORT. Un resultado positivo
se presenta como coste y uno negativo como cobro. Se usan `Decimal` y cadenas
decimales de extremo a extremo; no se suman importes con coma flotante.
`GET /paper/perp/accounts/{account_id}/positions/{position_id}/funding-estimate`
devuelve total, registros individuales, periodo, fuente y la etiqueta
`HISTORICAL_ESTIMATE_NOT_BOOKED`.

La unidad y los campos se contrastaron con la [documentación oficial de
GRVT](https://api-docs.grvt.io/schemas/api_funding_rate/). Se consulta
`FUNDING_INTERVAL`, no una agregación normalizada por horas.

## Asentamiento PAPER

`POST /paper/perp/accounts/{account_id}/positions/{position_id}/funding-settle`
requiere un `request_id` UUID. Para una posición abierta usa la hora de una
cotización pública reciente como corte; para una cerrada, su hora de salida.
El servidor vuelve a validar bajo bloqueo de escritura toda la cobertura
desde la apertura, compara registros previamente asentados y agrega sólo
liquidaciones nuevas. La clave única `(position_id, funding_time_ns)` y el
`request_id` evitan dobles cargos incluso con reintentos concurrentes.

Cada pago ficticio conserva tasa, mark, intervalo, fuente y hora en
`paper_perp_funding_events`. `funding_paid` es positivo para coste y negativo
para cobro. Reduce la equity aislada y el P/L al cerrar. Si se asienta después
del cierre, recalcula el resultado realizado y el saldo PAPER; el evento CLOSE
anterior permanece como registro de lo que se conocía entonces. Un cierre sin
cobertura completa se etiqueta como pendiente, no como neto final.

La migración aditiva de la base PAPER a versión 4 conserva posiciones
anteriores con `funding_paid=0` y cobertura `UNASSESSED`. Sólo una revisión
completa de su histórico permite marcarlas `CLOSED_COMPLETE`. Nada de esto
equivale a un asiento contable de la cuenta real GRVT.

## Cuándo se rechaza

La respuesta es 503 si faltan datos, la paginación es incompleta, los registros
están desordenados o duplicados, falta una liquidación esperada, los intervalos
no son continuos, o una apertura/cierre coincide exactamente con un instante
de liquidación. Se exige un registro anterior a la apertura como ancla. Así,
un histórico incierto nunca aparece como financiación cero.

## Límites

- El histórico público no demuestra que GRVT cobrase o abonase exactamente ese
  importe a una cuenta concreta ni confirma la secuencia de eventos intratick.
- La posición PAPER mantiene tamaño constante y no modela cambios de margen,
  primas, descuentos, liquidación real ni modificaciones de una posición GRVT.
- El asentamiento manual es optativo; para AUTO PAPER depende del observador
  local y de la disponibilidad del histórico público. Una posición abierta
  sólo está cubierta hasta el instante de su última revisión; el P/L agregado
  puede incluir posiciones pendientes. El estado del observador y la cobertura
  por posición deben revisarse antes de interpretar el rendimiento.
- Una estimación completada no hace ejecutable una estrategia ni habilita
  órdenes reales. Para considerar un P/L neto realista hacen falta conciliación
  y una prueba separada de la mecánica del venue.
