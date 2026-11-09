# Ejecución REAL del modelo perpetuo: siguiente integración

Estado actual: v110 verificado y cargado con [activación del bot](GRVT_PERP_BOT_ACTIVATION.md).
Este plan conserva las diferencias y criterios que guiaron esa integración.
El piloto humano y la aceptación del proyecto completo siguen pendientes.
La revisión v107 compara originales y
configuración, pero mantiene autoridad NONE. El [núcleo de entrada](GRVT_PERP_ENTRY_KERNEL.md)
añade presupuesto, preflight, firma y registro propios del modelo nuevo;
está verificado desde v108 y no habilita el bot en GUI. Este documento conserva el alcance necesario para
cerrar **estrategia probada → bot REAL → cierre y conciliación**, sin sustituirlo
por la revisión ya implementada.

v109 incorpora la [fuente independiente de señales](GRVT_PERP_SIGNALS.md):
recogida pública, evaluador causal, registro reproducible y trabajador revocable
independiente del seguimiento de órdenes. La [concesión v4 y escritor REAL](GRVT_PERP_BOT_GRANT.md)
también están verificados/cargados, con selección durable y claim v3.
Falta conectar ese conjunto al coordinador y a la GUI; v109 no lo activa.

v110 incorpora [coordinador y supervisión v5](GRVT_PERP_SUPERVISION.md), con
productor independiente, fills propios, vigilancia, cierre reconciliado y
revisión/activación API/GUI separadas. La comparación v107 conserva su contrato
de consulta y continúa declarando `execution_supported: false`.

## Diferencias que hay que resolver

- `grvt_bot_source` consume `paper_signal_sessions`/`paper_signal_actions` del
  modelo anterior. El nuevo flujo vive en `paper_observed_*`. No se copiarán
  eventos a tablas antiguas para fingir compatibilidad.
- Las concesiones MAINNET v1/v2/v3, sus hashes y sus fuentes deben seguir
  siendo legibles. El perfil nuevo requiere otro contrato versionado y una
  fuente explícita, conservando los lectores anteriores.
- `MainnetBracketDraft` no configura margen/apalancamiento. Los ajustes del
  instrumento se leen separadamente. `account_summary.margin_type` identifica
  la cuenta agregada; no acredita el modo del instrumento.
- El preflight anterior exige saldo USDT sin otras posiciones/órdenes y equity
  de al menos dos veces el notional. Es conservador y corresponde al contrato
  anterior; no demuestra la ejecución del perfil apalancado nuevo.
- `_entry_draft` anterior dimensiona con un límite de notional REAL. El nuevo
  perfil dimensiona por asignación de efectivo, margen, colchón de pérdida y
  comisiones. No se debe sustituir una fórmula por otra silenciosamente.

## Contrato REAL independiente

La autorización debe fijar estrategia/perfil y versiones de motores, origen
OOS, prefijo PAPER reproducido, subcuenta, instrumento, dirección y condiciones
compatibles. El usuario aprueba **capital REAL independiente**, importe máximo
por entrada, exposición, pérdidas, número de entradas, plazo y parada.

En CROSS, el exchange puede utilizar el colateral disponible de la subcuenta:
un presupuesto virtual menor no aísla ese resto del saldo. Se debe resolver
explícitamente la cuenta dedicada y su colateral autorizado, además del tamaño
de posición. En ISOLATED hay que verificar el margen/apalancamiento aplicado a
la posición real. La revisión de configuración por sí sola no resuelve nada de
esto ni establece límites duros de pérdida durante movimientos de mercado.

No se cambiará el ajuste de GRVT automáticamente para conseguir una coincidencia.
Si más adelante se implementa configurar margen, será otra acción firmada,
autorizada y reconciliada; no un efecto secundario de **Revisar**.

## Flujo de implementación y aceptación

1. Fuente nueva: replay causal de señales observadas, perfil fijo, identidad
   propia de señal/orden, sólo eventos posteriores a la autorización y límite
   de edad. GAP/STOP/cambio de clave impiden nuevas entradas. El histórico de
   calentamiento y los cierres estimados nunca originan órdenes reales.
   La prueba PAPER es evidencia del origen, no una cuenta operativa que deba
   seguir financiando señales ficticias. El flujo REAL debe recopilar sus
   propios datos actuales y evaluar las mismas reglas; una posición o saldo
   ficticio no puede decidir si el bot REAL entra. No falsificar filas PAPER.
2. Concesión RAM versionada, presupuesto y dimensionamiento REAL precisos.
   La referencia guardada sigue siendo evidencia, no autoridad. Una lectura
   fría, exportación, respuesta perdida o reapertura no inicia trabajadores.
3. Preflight específico del modelo: configuración por instrumento, reglas
   actuales, colateral/margen y exposición reales, liquidez, precio, costes y
   señales frescas. Guardar revisión/claim antes de transmitir y comprobar la
   revocación durante la red. Los límites del exchange prevalecen sobre perfiles
   experimentales no ejecutables, que se rechazan explícitamente.
4. Reutilizar transporte nativo y protecciones cuando su semántica coincida;
   adaptar el diario/lector por versión, sin relajar contratos anteriores.
   Diferenciar autorización, acuse, fill parcial/total, posición y conciliación.
   El núcleo nuevo declara FOK limitado, protecciones iniciales LAST desde el
   límite de entrada y comisión estimada por techo. Antes de habilitar el bot,
   resolver y mostrar la relación con el precio medio de fills, la tarifa
   efectiva y la observación de margen/apalancamiento de la posición real.
5. Seguimiento y cierre: stop/objetivo, reglas de salida compatibles, comisiones,
   financiación real, pérdida de conexión, STOP y recuperación. Sin nuevo envío
   por una respuesta perdida; consulta por identidad durable. No inventar un
   cierre ni una liquidación a partir del precio teórico.
6. UI completa: elegir prueba, revisar cambios y presupuesto, autorizar dentro
   de límites, ver señales/operaciones reales en gráfico, parar y recuperar.
   No habilitar **Activar** hasta que el recorrido exista de extremo a extremo.
7. Pruebas propias: LONG/SHORT y CROSS/ISOLATED, distintos capitales PAPER/REAL,
   límites, discrepancias y cambios de configuración, ejecución parcial,
   respuesta perdida, bloqueo concurrente, reinicio, cierre/funding/conciliación
   y compatibilidad de archivos anteriores. Recorrido real navegador→API→diario
   cifrado con proveedores controlados, después piloto humano mainnet separado.

La doble lectura local de configuración no impide modificaciones externas entre
la revisión y el envío ni después. El diseño debe comprobarlas antes de cada
entrada y durante el seguimiento, detener nuevas entradas al detectarlas y
explicar ese límite; no afirmar una atomicidad que la API no proporciona.

## Decisiones para conectar el coordinador nuevo

Revisión de código del 6 de noviembre: reutilizar sin adaptación
`review_mainnet_bot_exit` no reproduce el modelo PAPER perpetuo nuevo. El
coordinador anterior utiliza MARK y un prefijo independiente de hasta 5.000
velas; PAPER observado emplea bid LONG / ask SHORT y el seed completo. Además,
su límite temporal usa el tiempo exacto del fill, mientras PAPER cuenta cierres
de velas posteriores a la entrada. La integración nueva debe resolverlo así:

- Señales/indicadores desde el feed propio de la concesión y su histórico
  completo, sin sustituirlo silenciosamente por 500 velas de calentamiento.
  Una EMA no conserva necesariamente el mismo valor al cambiar el prefijo.
- Stop/objetivo dinámicos sobre fills propios y bid/ask observados, con un
  motor de salida versionado. Las protecciones nativas LAST, calculadas desde
  el límite firmado, siguen siendo una correspondencia distinta y explícita.
- Una condición de salida sólo puede usar un cierre posterior al fill real.
  El cierre de la vela que contiene la entrada puede evaluar la regla, pero
  su extremo anterior a la entrada no debe alimentar el trailing. No usar
  una condición ya cierta antes de que existiese la posición.
- El máximo de permanencia debe definir expresamente si cuenta cierres
  posteriores a la entrada, como PAPER, o tiempo transcurrido; no introducir
  el segundo como si fuera el primero.
- Vigilar configuración por instrumento y modo/apalancamiento de la posición
  efectiva. GRVT documenta `margin_type`, `leverage` y, para ISOLATED,
  `isolated_balance`, `isolated_im`, `isolated_mm`. No deducirlos del modo
  agregado de la cuenta ni asumir valores cuando falten. La hora de una
  posición sin cambios no equivale a la hora de recogida de la cuenta.
- Guardar la evidencia de vigilancia/salida y sus vínculos con señal/fills,
  posición y mercado. El replay frío y el prefijo RAM deben comprobar también
  las filas referenciadas, no sólo el texto de eventos de la concesión.
- El feed continúa durante el seguimiento de órdenes. GAP, discrepancia de
  configuración/costes y revocación impiden nuevas entradas; el cierre o la
  conservación de protecciones debe seguir el permiso y límites aprobados,
  sin inventar una ejecución ante una desconexión.

Referencia primaria de campos de posición:
[Positions, GRVT](https://api-docs.grvt.io/schemas/positions/).
La documentación de [ClientTier](https://api-docs.grvt.io/schemas/client_tier/)
publica las tarifas como enteros sin explicar allí su escala: no se ha inferido
esa escala a partir de campos de interés o builder fees diferentes.
