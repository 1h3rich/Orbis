# Orbis: requisitos no funcionales

Este documento reúne exigencias ya expresadas en
`Orbis_Requisitos_Funcionales_Fase_0.docx`,
`Orbis_Explicacion_del_Proyecto.docx` y `Orbis_Plan_de_Desarrollo.docx`.
No sustituye a los requisitos funcionales RF-001 a RF-171. «Parcial» indica que
hay una base implementada, pero todavía falta parte del criterio.

| ID | Exigencia y criterio verificable | Origen | Estado |
| --- | --- | --- | --- |
| RNF-01 | La instancia funciona localmente sin cuenta central ni envío de datos financieros a un servicio de Orbis por defecto. | Principios funcionales; RF-001, RF-008 | Parcial: API y SQLite locales. |
| RNF-02 | Seed phrases y claves privadas nunca se solicitan ni almacenan; las API keys quedan fuera del código, exportaciones y repositorio. | Principios; RF-029, RF-140 a RF-142; contexto §5 | Parcial: aún no hay integración de secretos. |
| RNF-03 | Toda acción que pueda crear una orden pasa por Guard; estados críticos ambiguos bloquean nuevas órdenes y existe un kill switch. | Principios; RF-055, RF-063, RF-064 | Parcial: compras PAPER pasan por Guard, pero falta el resto del flujo. |
| RNF-04 | PAPER y LIVE mantienen datos y acciones distinguibles en almacenamiento, consultas, límites e interfaz. | RF-011, RF-086, RF-119; contexto §12 | Parcial: el Ledger almacena `mode`, hay consultas separadas y capital PAPER persistido por moneda; falta una simulación completa. |
| RNF-05 | Los cálculos monetarios evitan errores de coma flotante, conservan moneda y procedencia, y los resultados históricos pueden reconstruirse. | RF-069 a RF-081, RF-109, RF-110 | Parcial: `Decimal` en Guard y Ledger; faltan reglas de redondeo y fórmulas versionadas. |
| RNF-06 | Una operación o decisión deja fecha, origen, estado, motivos y configuración aplicada; las correcciones son auditables. | Principios; RF-047, RF-105, RF-111, RF-121 a RF-126 | Parcial: metadatos básicos del Ledger; faltan versiones y auditoría. |
| RNF-07 | Un reinicio, reintento o ejecución concurrente no duplica órdenes ni supera límites; las órdenes inciertas se reconcilian antes de reintentar. | RF-046, RF-062, RF-063, RF-094 | Parcial: `request_id` persistido evita duplicados al reintentar la misma compra PAPER; faltan exclusión entre compras distintas y reconciliación externa. |
| RNF-08 | La API administrativa protege el acceso; Web y Mobile usan la API y nunca guardan secretos del exchange. | RF-003 a RF-009, RF-138, RF-140 a RF-145 | Pendiente. |
| RNF-09 | Hay backups verificables y migraciones controladas antes de cambios de esquema incompatibles. | RF-152 a RF-157, RF-169, RF-170 | Pendiente. |
| RNF-10 | Estrategias, exchanges, cálculos e interfaces se sustituyen sin acoplar el núcleo a MEXC ni a una app concreta. | Principios; RF-019, RF-048; contexto §10-11 | Parcial: módulos separados; faltan interfaces de exchange. |
| RNF-11 | Pruebas cubren cálculos, Guard, persistencia y API, además de fallos de red, duplicados y estados ambiguos antes de LIVE. | Plan, fase 11; criterios globales | Parcial: hay pruebas locales; faltan conectores y fallos externos. |

Antes de conectar un exchange o exponer la API fuera del equipo local deben
estar resueltos RNF-02, RNF-03, RNF-07 y RNF-08. Antes de usar datos personales
durante meses también deben existir backup y migraciones de RNF-09.
