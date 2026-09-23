# Orbis

Orbis es un proyecto local de automatización de inversiones. Hoy ofrece una API
FastAPI con estrategias guardadas, un Ledger básico, Orbis Guard y compras
simuladas. La aplicación web/móvil, el scheduler DCA y los conectores de exchange
siguen pendientes. No hay ejecución con dinero real.

## Desarrollo local

El entorno actual se ha probado con Python 3.14 y las dependencias de
`requirements.txt`. Desde la raíz del repositorio:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
uvicorn app.main:app --reload
```

La API y su documentación interactiva quedan en
`http://127.0.0.1:8000` y `http://127.0.0.1:8000/docs`.

Por defecto se crea `orbis.db` en el directorio de trabajo. Si necesitas otra
ruta, exporta `ORBIS_DATABASE_URL` antes de iniciar el proceso. `.env.example`
muestra el valor local; la aplicación no carga archivos `.env` automáticamente.

Para ejecutar las pruebas: `python -m pytest -q`. La mayoría de pruebas usan
bases de datos en memoria; importar la aplicación puede crear `orbis.db` en el
directorio de trabajo. No publiques esta API en una red
accesible por terceros: todavía no tiene autenticación administrativa.

## Estado de los datos

`POST /paper/scenarios` crea un escenario por moneda cotizada con capital inicial
y límites guardados. `GET /paper/scenarios` muestra sus saldos. El saldo
disponible se calcula restando las compras PAPER ejecutadas desde la creación
del escenario; el capital inicial no se puede editar todavía. `POST /paper/buy`
recibe el activo, moneda, importe, precio y comisión estimada, pero obtiene
presupuesto y límites del escenario almacenado. Guard comprueba la propuesta
y el Ledger registra las compras aprobadas en modo `PAPER`. El precio no viene
todavía de un mercado. `GET /portfolio/buys?mode=PAPER` resume compras por
activo y moneda cotizada; no representa el valor actual ni el P/L. El mismo
endpoint acepta `mode=LIVE` para consultar registros, aunque Orbis no ejecuta
operaciones reales.

Cada solicitud a `POST /paper/buy` debe incluir un `request_id` UUID generado
por el cliente. Si se reintenta con el mismo identificador y la misma propuesta,
la API devuelve el resultado ya guardado sin registrar otra operación, incluso
si Guard la había bloqueado. Reutilizar ese identificador con otra propuesta
devuelve `409`. La decisión y, si procede, la compra se guardan en una sola
transacción.

La API conserva `POST /operations` para registrar operaciones manuales. Todavía
no hay autenticación, control general de duplicados, migraciones ni backups. Dos
compras simultáneas con identificadores distintos podrían superar un límite
antes de que ambas se registren. Los modelos
de la base de datos se crean al importar la aplicación; cambios de esquema
posteriores requerirán migraciones explícitas.

Los requisitos funcionales están en `Qué_es/Orbis_Requisitos_Funcionales_Fase_0.docx`.
Los requisitos no funcionales derivados de los documentos del proyecto están en
`Qué_es/Orbis_Requisitos_No_Funcionales.md`.
