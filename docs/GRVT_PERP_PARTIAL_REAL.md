# Salidas parciales en GRVT REAL

En v128, los perfiles LIMIT con parciales se activan desde **Patrimonio → GRVT → Bot LIMIT**.

## Origen y presupuesto

La preparación exige una prueba PAPER v3 terminada, sin exposición y con al
menos un parcial observado en una operación cerrada. Vuelve a reproducir LAB y
PAPER y fija el prefijo cifrado. El presupuesto REAL y los límites de intentos
se introducen por separado. El presupuesto ficticio no se usa para la subcuenta.

El seed v3 usa velas y cotizaciones públicas con continuidad verificada. Su
prefijo queda ligado a la revisión PAPER. Sus eventos se pueden reproducir
sin iniciar una sesión PAPER ni solicitar información privada al exchange.

## Cálculo de una reducción

El objetivo se compara contra el nocional exacto de los fills de entrada y se
usa el bid para LONG o el ask para SHORT. El tamaño se redondea al lote sobre
la cantidad inicialmente ejecutada. Antes de emitir un contrato IOC
`reduce_only` se comprueban el saldo de posición conciliado, la frescura de la
cotización, los mínimos, el límite nocional y el deslizamiento permitido.

Los intentos siguen el orden de objetivos. El terminal con cantidad parcial
descuenta sólo lo ejecutado. El terminal sin ejecución deja la cantidad intacta.
Un intento sin conciliar bloquea otro envío, incluso con un stop pendiente.
Cuando se concilia, las protecciones tienen prioridad sobre los parciales.
Se reservan intentos para el cierre final y se evita dejar un residuo conocido
por debajo del mínimo. Un contrato preparado no constituye una orden enviada.
La API de GRVT distingue el estado de la orden y su cantidad acumulada
ejecutada. [Estados de orden](https://api-docs.grvt.io/schemas/order_state/)

## Diario LIMIT cifrado

La entrada v2 fija la política de parciales, el instrumento y el prefijo
PAPER. Cada reducción fija el objetivo, la cantidad, la cotización, el límite
de precio, la decisión de supervisión y las dos auditorías privadas. El diario
reconstruye el contrato a partir de esos datos. El registro se confirma antes
de transmitir. El siguiente intento requiere un estado terminal de GRVT y
dos auditorías concordantes de fills, posición y caja.

Si se pierde la respuesta, el identificador se conserva al reabrir la bóveda.
La recuperación consulta GRVT con ese identificador. La posición sólo se
declara cerrada después de conciliar el saldo y la ausencia de exposición.
El diario distingue los fills de cada objetivo y el cierre final.

## Activación y reanudación

Carga una prueba PAPER LIMIT terminada con algún parcial ejecutado. Revisa
el presupuesto, los objetivos P1 a P5 y los intentos totales y reservados
para el cierre. Una confirmación separada habilita la ejecución durante
el permiso mostrado. Al retomar, la confirmación renueva el permiso y
conserva los límites originales. **Ver gráfico** distingue cada parcial
del cierre final a partir de los fills conciliados.

## Pendientes de activación

- Completar MARKET con permiso temporal, parada, límites y revisión de
  operaciones ambiguas. Verificar su convivencia con las protecciones nativas.
- Completar el piloto con una cuenta configurada por el propietario.

## Revisión de la protección MARKET en desarrollo

Un MARKET de ORBIS se abre con un FOK acotado y un grupo nativo SL/TP.
GRVT documenta OTOCO con el mismo tamaño inicial en las tres órdenes. Su
cancelación y reemplazo permite cambiar las dos salidas del grupo, pero no
describe una operación atómica que también reduzca la posición. La referencia
de posición vinculada incluye restricciones para la creación mediante API.
[Grupos y errores de órdenes GRVT](https://api-docs.grvt.io/trading_api/#bulk-orders)

La revisión interna consulta los tres contratos firmados, los intentos IOC
identificados por su contrato original, los fills, la posición con signo y
el saldo. Cruza los contratos activos con la lista de órdenes abiertas y
repite la lectura individual tras consultar la cuenta. Rechaza lecturas
incompletas, cambios durante la revisión, fills ajenos al grupo y cualquier
IOC que no tenga estado terminal. Distingue el tamaño exacto del stop
observado, una diferencia de tamaño, el vencimiento, la desactivación y los
fills nativos. El estado devuelto es una observación de sólo lectura, sin
autoridad de envío. La API de GRVT distingue el estado general de la orden y
los datos del trigger. [Estados de orden](https://api-docs.grvt.io/schemas/order_state/)

Para activar parciales MARKET hace falta probar con GRVT TESTNET qué ocurre
cuando un IOC reduce el tamaño antes de que dispare el stop original, y cuando
el stop dispara antes o durante el IOC. El ensayo debe registrar ambas piernas
nativas, sus fills y rechazos, el residuo firmado y la reconciliación de caja.
Si la prueba exige cancelar y reemplazar, hace falta verificar el intervalo
entre órdenes y el comportamiento con acuse perdido antes de diseñar la
emisión automática. El flujo LIMIT conserva su propio diario y sus permisos.

### Ensayo de proveedor pendiente

Usar una subcuenta TESTNET aislada, con permiso Trade de ese entorno y
capital de prueba. Guardar estados normalizados y cantidades, sin claves,
cookies ni firmas. Leer las reglas del instrumento y el libro en cada
escenario. La consulta pública del 7 de noviembre de 2026 respondió para
`BTC_USDT_Perp` con lote mínimo 0,001 y nocional mínimo 100 USDT. Son
observaciones de ese momento.
[Entorno de pruebas GRVT](https://help.grvt.io/en/articles/9588408-getting-started)

| Secuencia | Evidencia a conservar |
| --- | --- |
| FOK completo, sin reducción | Órdenes que aparecen en la búsqueda individual y en `open_orders`. Tamaño inicial y estado de ambos triggers. |
| IOC terminal sin fill | Ambas piernas y posición conservan sus cantidades. |
| IOC con fill, stop por disparar | Tamaño y estado de ambas piernas antes y después. Confirmar si el stop se ajusta, permanece o se cancela. |
| Stop después de reducir | Fill o motivo de rechazo, estado de la otra pierna, tamaño restante y conciliación. |
| Stop durante un IOC y acuse perdido | IDs originales consultados hasta estado terminal, fills sin duplicados y saldo final. |
| Lecturas contradictorias o datos faltantes | Repetir observación sin transmitir y comprobar que la reserva se conserva. |

Repetir en LONG y SHORT. Si `open_orders` omite triggers no disparados,
verificar qué lectura autenticada permite distinguir una omisión esperada
de una desaparición. La revisión actual exige concordancia con las
consultas individuales. La respuesta HTTP a una orden nunca acredita un fill.
[Órdenes abiertas](https://api-docs.grvt.io/trading_api/#open-orders)

### Capturar el ensayo

Desde **Patrimonio → Recuperación GRVT → Entrada protegida · TESTNET**, abre el **Registro** del grupo y elige **Muestra de parciales**. Introduce una clave **View de Testnet** vinculada a esa subcuenta y pulsa **Capturar**. Deja vacío el ID IOC para la primera muestra. Para repetirla después del IOC, indica su ID de exchange o el ID cliente de la API. Pulsa **Descargar JSON** para guardar cada muestra por separado. La clave se borra del formulario al capturar. Cerrar el panel, cambiar de subcuenta o bloquear la vista descarta la muestra en memoria. La descarga contiene cantidades, fills e identificadores de Testnet.

El JSON distingue el tamaño declarado del stop, la cantidad que informa GRVT para el libro y la presencia en órdenes abiertas. Ninguno de esos campos acredita por sí solo cobertura del stop. [Estado de orden de GRVT](https://api-docs.grvt.io/schemas/order_state/)

El capturador de terminal ofrece los mismos datos observados. Solicita la clave View interactivamente y guarda el archivo con permisos 0600.

Desde la raíz del proyecto, con una subcuenta de prueba aislada:

```sh
.venv/bin/python scripts/capture_grvt_market_probe.py \
  --sub-account SUB_TESTNET --parent ID_ENTRADA_ORBIS --out antes.json
.venv/bin/python scripts/capture_grvt_market_probe.py \
  --sub-account SUB_TESTNET --parent ID_ENTRADA_ORBIS \
  --ioc-order-id ID_ORDEN_GRVT --out despues.json
```

Entre ambas capturas, el operador puede enviar una LIMIT con vigencia IOC
y opción Reduce-Only desde la interfaz de GRVT Testnet. Para un IOC creado
mediante API también se admite `--ioc-client-id`. Conservar los mismos
identificadores al capturar la ejecución o cancelación posterior del stop.
Si cambia una orden durante una muestra, repetir con los mismos IDs y
un archivo nuevo. La herramienta verifica cantidades, fills y posición al
corte de la cuenta. El saldo se conserva como observación para su conciliación
posterior. [Opciones de vigencia y Reduce-Only](https://help.grvt.io/en/articles/16812785-how-do-you-execute-a-limit-order)

### Muestras guardadas

Desde **Muestra de parciales**, **Capturar** también guarda la muestra del grupo en la bóveda SQLCipher. La clave View se usa durante la consulta. Después, el selector permite abrir y descargar la captura con View desconectada. **Comparar muestras** permite elegir dos o tres muestras guardadas del mismo grupo o cargar los JSON en el modo **Archivos JSON**. Si se pierde la respuesta, la lista vuelve a consultar el archivo local. Repetir la misma solicitud recupera la captura anterior.

El esquema 82 añade una tabla de diagnóstico con la huella de la muestra y del plan original. Las copias cifradas vuelven a validar ambos y conservan hasta 32 muestras por grupo, de hasta 4 MiB cada una. Estas observaciones conservan el alcance TESTNET. El ensayo con el proveedor autenticado sigue pendiente. [Estado de orden de GRVT](https://api-docs.grvt.io/schemas/order_state/).

### Comparar muestras

En **Muestra de parciales → Comparar muestras**, elige dos o tres JSON del mismo grupo, de hasta 4 MiB por archivo y del más antiguo al más reciente. La tercera captura permite seguir la posición antes del IOC, tras el IOC y tras el stop en una sola tabla. ORBIS coteja el grupo y los fills previos en cada tramo. El servidor local analiza los archivos sin consultar GRVT. Cerrar el panel limpia los archivos seleccionados y la tabla.

La tabla separa tamaño declarado, tamaño informado en el libro y presencia en órdenes abiertas. El orden se deduce de las marcas de tiempo de los fills nuevos del IOC y del stop. Si sus intervalos se cruzan o empatan, se muestra la ambigüedad. El informe resta el PnL realizado menos comisión a la variación de saldo de cada tramo. Un cero indica coincidencia en ese tramo. La verificación autenticada del proveedor se hace por separado. [Campos de ejecución](https://api-docs.grvt.io/schemas/fill/)
