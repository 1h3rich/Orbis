# Activar un bot perpetuo probado

Estado: v110 verificado y cargado en el backend local el 6 de noviembre de
2026. Las pruebas del ciclo utilizan proveedores controlados; no acreditan
un piloto con fondos del propietario.

## Recorrido de uso

1. En LAB, guarda una validación perpetua fuera de muestra para el mercado
   público GRVT y su perfil LONG o SHORT.
2. Actívala en PAPER observado. La revisión REAL requiere velas observadas y
   al menos una operación cerrada; los cierres estimados tras GAP no cuentan.
3. Desbloquea REAL y conecta la API y la firma dedicada MAINNET en el panel
   GRVT. La conexión de consulta por sí sola no aporta una firma operativa.
4. En **Bot · MAINNET**, elige **Perpetuo · margen** y la sesión PAPER.
   El perfil conserva instrumento, versión, temporalidad, margen y apalancamiento.
5. Define colateral de la subcuenta dedicada, efectivo por entrada, topes de
   nominal, presupuesto acumulado, umbral de pérdida, entradas y duración.
   Confirma el uso de todo el colateral de esa subcuenta. Revisa comisiones y
   ejecución cuando necesites cambiar sus valores iniciales.
6. **Revisar bot** compara los originales, la configuración privada y un plan
   con equity REAL. Después de comprobar el resumen, autoriza esos límites y
   pulsa **Activar bot**. Una edición de parámetros descarta la revisión previa.
7. El bot recoge sus propias cotizaciones y espera una señal posterior a la
   autorización. No hereda el saldo PAPER ni ejecuta una señal histórica.
8. **Ver operación** muestra órdenes y fills; **Ver gráfico** sitúa las
   ejecuciones confirmadas. El neto procede de la conciliación de fills,
   comisiones, funding y saldo, no del precio que disparó la salida.
9. **Detener bot** revoca nuevos envíos. Revisa cualquier posición pendiente y,
   si procede, vuelve a conectar y autoriza su recuperación. Proteger la vista
   mantiene sólo la operativa para la que se eligió expresamente esa opción.

GRVT utiliza el saldo ya financiado de su subcuenta. El circuito de USDT por
BNB hacia MEXC o un DEX tiene sus propios controles y registros; esta activación
no añade un puente BNB→GRVT. El piloto humano con proveedor, permisos y fondos
reales sigue pendiente. Las pruebas de desarrollo no utilizaron las claves,
cuentas ni fondos del propietario.

## Revisión y contrato de activación

La revisión nueva es `orbis.grvt.mainnet.perp.bot.review.v1`. Contiene origen
LAB/PAPER, perfil, términos de entrada, política, ejemplo de plan, revisión de
control del bot, UUID de ejecución y vencimiento de 120 segundos. Su alcance
exterior es `SUPERVISED_FUTURE_SIGNALS_AND_OWN_POSITION_CLOSE`; el contrato de
entrada v1 conserva su alcance anterior sin reinterpretarlo.

La comparación v107 sigue siendo de consulta, con `execution_supported:false`.
La GUI distingue esa comparación de la revisión nueva que autoriza el bot.
Sólo ofrece el modelo perpetuo si el backend anuncia la capacidad correspondiente;
actualizar los archivos estáticos sobre un proceso anterior no lo habilita.

La revisión está ligada mediante MAC al propietario de la conexión, generación
de bóveda y revisión de permisos. La prueba original queda como capacidad sólo
en RAM, sin seed de 100.000 velas en la respuesta del navegador. Al activar se
consume una vez, se vuelven a comprobar los bytes originales y las épocas de
LAB/PAPER, y se recoge un seed actual nuevo. También se comprueba el vencimiento
durante la preparación; una revisión válida al inicio no permite activar tarde.

Un fallo detectado durante el desarrollo convertía nanosegundos del origen al
devolver la revisión ya firmada. Se corrigió normalizando primero el formato
JSON que se sella. El navegador devuelve exactamente lo revisado; no se relaja
la comparación ni se admiten enteros financieros redondeados por JavaScript.

La capacidad RAM no sustituye el replay. Antes de activar se comprueban otra
vez los eventos PAPER originales, incluso si una alteración conservase sus
hashes guardados. Se permiten anexos compatibles después del prefijo aprobado;
un bloqueo/reautenticación, cambio de original, GAP o fuente incompatible exige
nueva revisión. Los checkpoints guardados no se convierten en autoridad.

## API local

Con el prefijo `/real/exchanges/grvt/mainnet/bots`:

| Ruta | Función |
| --- | --- |
| `GET /capabilities` | Capacidades del backend, sin autoridad ni consultas al exchange |
| `GET /perp/sources?offset=0` | Índice paginado de PAPER observado; se verifica al revisar y activar |
| `POST /perp/preview` | Reproducir originales y recoger configuración/plan; no mueve fondos |
| `POST /perp/start` | Consumir una revisión exacta y conceder permiso RAM limitado |
| `GET /{run_id}` | Recuperar una activación incierta por su UUID, sin repetirla |
| `GET ?sub_account_id=…` | Historial compartido de bots anteriores y perpetuos nuevos |
| `POST /stop` | Parada compartida por subcuenta |

Las rutas requieren sesión REAL local y mismo origen; las respuestas no se
almacenan en caché. El cuerpo de activación no acepta claves, URLs, código,
planes sustituidos ni otra red. Los límites están validados con modelos
estrictos y cantidades decimales en texto.

El coordinador comparte la reserva de subcuenta, transporte de envío único y
recuperación con el flujo existente. Una respuesta perdida no devuelve la
revisión a estado utilizable: se consulta el UUID conocido. Dos peticiones de
activación concurrentes consumen una revisión y crean un único trabajo.
Reabrir la aplicación, desbloquear o consultar registros no arranca ese trabajo.

## Evidencia actual y aceptación pendiente

El recorrido de navegador usa UI, API, SQLCipher, fuente, trabajador,
coordinador, salida y conciliación reales. Sólo controla los I/O del proveedor
y el reloj. Verifica presupuesto editable, autorización expresa, respuesta
local perdida, una sola petición de activación, vista protegida, señal futura,
entrada, posición, marcadores, cierre y recuperación del historial. El precio
del disparador difiere del fill: se conserva el neto real de la muestra.

Las pruebas de API cubren, además, cambios de términos/MAC, otro tipo de
revisión, origen externo, vencimiento antes y durante la preparación,
revocación de conexión/control, cambio de épocas LAB/PAPER, alteración de
bytes originales y activación concurrente. No escriben en almacenes reales
del propietario. La implementación de supervisión se detalla en
[GRVT_PERP_SUPERVISION.md](GRVT_PERP_SUPERVISION.md).

Evidencia de v110: 4.790 casos / 401 archivos y quince recorridos actuales de
navegador. Se corrigieron dos versiones de recursos esperadas en una prueba y
se repitieron los 19 casos de ese archivo. Aplicación y resto de pruebas
conservaron exactamente sus bytes. El informe distingue esa corrección de
una repetición completa posterior.

El proceso anterior estaba inactivo y bloqueado; terminó de forma ordenada
antes de iniciar una sola instancia nueva. Se cotejaron fuentes/artefactos y
el smoke pasivo del local: UI sin errores, rutas nuevas registradas, 51
rechazos anónimos, REAL cerrado y cero acciones financieras. La consulta
pública real de ticker GRVT también devolvió datos actuales, sin claves.
Evidencia: `~/.local/state/orbis/verification/v110-perp-supervised-activation-r1/`.

El objetivo global conserva los pendientes de [aceptación](ACEPTACION_FUNCIONAL.md),
incluido el piloto humano, el cifrado gráfico del PAPER manual y la auditoría
del conjunto de flujos. Los almacenes legados del propietario siguen sin
proteger y cerrados hasta que elija cifrarlos en Ajustes; no se han migrado.
