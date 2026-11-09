# Validación externa dentro de una lectura protegida

Desarrollo sobre REAL 78, sin publicar. No cambia formatos persistidos, fórmulas
financieras, reglas de permisos ni interpretación de recibos.

## Problema medido

El recorrido DCA de renovación, nueva compra, segunda preparación y copia fría
tardó 162,689 s. Se invocó `external_outcome` 1.395 veces y se capturó el mismo
prefijo aceptado 1.264 veces. La validación repetida de observaciones y originales
ocupó casi todo el tiempo. Evidencia propia, funcional y con fuentes congeladas:
`external-policy-dca-performance-dev-r8/performance.json`.

## Decisión

`external_outcome` conserva su resultado inmutable sólo dentro de un
`dex_verified_read.scope` ya existente. La identidad incluye la transacción
SQLAlchemy, conexión SQLite, `total_changes`, `user_version`, `schema_version`
y `data_version`.
La transacción de lectura debe estar abierta y la sesión sin cambios ORM
pendientes. Un pin distinto descarta el resultado y vuelve a autenticarlo.
Sin ese scope se valida cada llamada; no hay caché entre solicitudes o sesiones.

La primera lectura comprueba observación, certificado, booking, enlace y prefijo
completos. Después se aplica `as_of`: una ausencia histórica no se reutiliza como
ausencia actual. El resultado guardado es un hecho autenticado, sin saldos,
permisos de firma ni posiciones inferidas. Al salir del scope se descarta;
`force_full` borra también estos hechos. LONG v7/v8 conserva deshabilitada su
caché persistente de prefijos mientras falten sus dependencias financieras.

Los nuevos tests usan recibos firmados y SQLCipher propios. Comprueban lectura
reutilizada, corte histórico, invalidación forzada, salida del scope, borrado ORM,
cambio SQL directo, rollback/commit y cambio de versión de esquema. Alterar la
aceptación sigue impidiendo liberar el nonce. La primera tanda tuvo tres casos
correctos y dos fallos esperados de repetición; los cinco pasan tras el cambio.

## Comparación delimitada

Mismo código del recorrido y mismos recuentos de llamadas de alto nivel:

| Medida | Antes | Después |
| --- | ---: | ---: |
| Recorrido DCA completo | 162,689 s | 24,730 s |
| Capturas de aceptación | 1.264 | 105 |
| Lecturas verificadas de observación | 26.828 | 2.394 |
| Lecturas de originales del operador | 70.878 | 8.292 |

Ambos recorridos pasan todos sus asserts funcionales, sin usar datos o fondos del
propietario. La diferencia es de 6,58× en este recorrido, con instrumentación
equivalente y sin otra tanda propia ejecutándose a la vez. No acredita todo el
rendimiento de ORBIS, una sesión prolongada ni linajes extensos. Sólo cambian el
módulo de resultados externos y el archivo nuevo de pruebas frente al corte
anterior. Evidencia: `external-policy-dca-performance-dev-r9/comparison.json`.

La primera regresión de 252 casos/32 módulos pasa con identidades cotejadas y
fuentes iguales. No se promueve: una sonda posterior reproduce otro caso.
`ROLLBACK/BEGIN` SQL directo conserva el objeto de transacción ORM y el contador
local, mientras otra conexión cambia el enlace. El hecho anterior se reutilizaba
y la guarda de nonce lo admitía. Todo ocurre en SQLCipher/EVM propios; el código
no estaba publicado.

Se incluye `data_version` en la identidad. La nueva prueba falla antes de la
corrección y pasa después: se vuelve a autenticar el enlace y el nonce continúa
bloqueado. Los seis casos, incluida esa corrección, pasan; se amplía
la prueba de reinicio a modos DELETE/WAL. Una sonda WAL independiente confirma
que la versión se conserva dentro del snapshot y cambia al reiniciarlo.
La medida de 24,730 s anterior a esta última guarda se conserva como intermedia;
se vuelve a medir el recorrido con la corrección completa antes de publicarlo.

La repetición corregida (`external-policy-dca-performance-dev-r10`) pasa en
26,053 s: 6,24× frente al mismo recorrido inicial. Mantiene 1.395 llamadas al
lector de resultado, pero sólo 105 capturas del prefijo. Mismo código del
recorrido y recuentos de alto nivel; es una medición delimitada, no aceptación
de rendimiento global. La regresión final ampliada de 254 casos/32 módulos
termina con cuatro salidas 0, identidades cotejadas y las mismas 1.304 fuentes
que la medida corregida. Incluye siete casos nuevos; los focos y la tanda
intermedia no se suman otra vez. Informe:
`external-outcome-scope-final-dev-r7/verification.json`. Sigue sin publicar;
el objetivo global conserva pendientes de revisión conjunta, UI, linajes y
aceptación completa.
