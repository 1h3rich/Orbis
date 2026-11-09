# Orbis: requisitos no funcionales

Este documento reúne exigencias ya expresadas en
`Orbis_Requisitos_Funcionales_Fase_0.docx`,
`Orbis_Explicacion_del_Proyecto.docx` y `Orbis_Plan_de_Desarrollo.docx`.
No sustituye a los requisitos funcionales RF-001 a RF-171. «Parcial» indica que
hay una base implementada, pero todavía falta parte del criterio.

| ID | Exigencia y criterio verificable | Origen | Estado |
| --- | --- | --- | --- |
| RNF-01 | La instancia funciona localmente sin cuenta central ni envío de datos financieros a un servicio de Orbis por defecto. | Principios funcionales; RF-001, RF-008 | Parcial: API y SQLite locales. |
| RNF-02 | Seed phrases y claves privadas nunca se solicitan ni almacenan; las API keys quedan fuera del código, exportaciones y repositorio. | Principios; RF-029, RF-140 a RF-142; contexto §5 | Cumplido mientras no haya exchanges: ni seed phrases, claves privadas ni API keys aparecen en el código, la base de datos ni los scripts; revisar de nuevo al añadir conectores. |
| RNF-03 | Toda acción que pueda crear una orden pasa por Guard; estados críticos ambiguos bloquean nuevas órdenes y existe un kill switch. | Principios; RF-055, RF-063, RF-064 | Parcial: compras y ventas PAPER pasan por Guard (precio, existencia de posición, inventario negativo, límites y kill switch), pero falta el resto del flujo. |
| RNF-04 | PAPER y LIVE mantienen datos y acciones distinguibles en almacenamiento, consultas, límites e interfaz. | RF-011, RF-086, RF-119; contexto §12 | Parcial: el Ledger almacena `mode`, hay consultas separadas y capital PAPER persistido por moneda; falta una simulación completa. |
| RNF-05 | Los cálculos monetarios evitan errores de coma flotante, conservan moneda y procedencia, y los resultados históricos pueden reconstruirse. | RF-069 a RF-081, RF-109, RF-110 | Parcial: `Decimal` en Guard y Ledger; faltan reglas de redondeo y fórmulas versionadas. |
| RNF-06 | Una operación o decisión deja fecha, origen, estado, motivos y configuración aplicada; las correcciones son auditables. | Principios; RF-047, RF-105, RF-111, RF-121 a RF-126 | Parcial: metadatos básicos del Ledger; faltan versiones y auditoría. |
| RNF-07 | Un reinicio, reintento o ejecución concurrente no duplica órdenes ni supera límites; las órdenes inciertas se reconcilian antes de reintentar. | RF-046, RF-062, RF-063, RF-094 | Parcial: `request_id` con digest de la propuesta (incluido `side`) se guarda en la misma transacción que la operación, el Ledger ordena por `(timestamp, id)` y las escrituras se serializan en SQLite; hay pruebas de concurrencia de saldo, duplicado y límites. Faltan órdenes inciertas y reconciliación externa, que aún no aplican en PAPER. |
| RNF-08 | La API administrativa protege el acceso; Web y Mobile usan la API y nunca guardan secretos del exchange. | RF-003 a RF-009, RF-138, RF-140 a RF-145 | Pendiente: la API todavía no tiene autenticación, por lo que no debe publicarse fuera del equipo local. El dashboard `/ui` no añade auth: sirve solo en `127.0.0.1` y no expone secretos (los precios públicos no usan claves). |
| RNF-09 | Hay backups verificables y migraciones controladas antes de cambios de esquema incompatibles. | RF-152 a RF-157, RF-169, RF-170 | Parcial: no se usa Alembic; `scripts/check_schema.py` compara `Base.metadata` con el esquema real y falla listando las diferencias, y los cambios pendientes de `orbis.db` están documentados con su DDL manual. Faltan backups verificables y aplicación automatizada de esos cambios. |
| RNF-10 | Estrategias, exchanges, cálculos e interfaces se sustituyen sin acoplar el núcleo a MEXC ni a una app concreta. | Principios; RF-019, RF-048; contexto §10-11 | Parcial: módulos separados (estrategias, Guard, Ledger, cálculos y un `PriceProvider` intercambiable para precios —`kraken\|coingecko\|manual\|static`, solo valoración PAPER—); el dashboard `/ui` usa la API por HTTP sin acoplar el núcleo. Faltan interfaces de exchange. |
| RNF-11 | Pruebas cubren cálculos, Guard, persistencia y API, además de fallos de red, duplicados y estados ambiguos antes de LIVE. | Plan, fase 11; criterios globales | Parcial: hay pruebas locales; faltan conectores y fallos externos. |

Antes de conectar un exchange o exponer la API fuera del equipo local deben
estar resueltos RNF-02, RNF-03, RNF-07 y RNF-08. Antes de usar datos personales
durante meses también deben existir backup y migraciones de RNF-09.
