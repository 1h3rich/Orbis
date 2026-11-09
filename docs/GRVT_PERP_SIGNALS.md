# Señales perpetuas independientes para REAL

Estado: código interno verificado y cargado en v109. No
habilita una ruta de activación, no crea una concesión MAINNET y no sustituye
el coordinador pendiente. La prueba de módulos con proveedor controlado no
acredita el recorrido final de autorización/API/GUI.

## Decisión de diseño

El origen LAB/PAPER demuestra qué estrategia se probó. El saldo ficticio, las
órdenes rechazadas por margen PAPER y una posición PAPER abierta no deciden
las entradas REAL. `grvt_perp_signals` evalúa directamente las reglas `lab-1.16`
con el mismo perfil y definición, sin construir una cuenta, posición, fill,
margen ni financiación ficticios.

Los formatos nuevos identifican `grvt-perp-signals-1.0`. No se reinterpretan
sesiones anteriores, referencias con autoridad NONE ni la revisión v107 con
`execution_supported:false`. No se escriben filas en tablas PAPER antiguas.

## Causalidad y tiempos

- El seed valida la definición normalizada, su huella y versión de perfil,
  temporalidad y capacidad multitemporal. El prefijo es contiguo y termina en
  la última vela cerrada. Todo el seed es calentamiento: cero señales pasadas.
- La fuente pública comprueba el original LAB y su sesión/archivo antes y
  después de HTTP. Extiende el prefijo verificando las dos velas solapadas y
  la cobertura exacta de cada página. Si cruza una frontera temporal, vuelve
  a recoger el cierre y una cotización posterior. No usa MARK como vela TRADE.
- Se conservan tiempo del proveedor, recogida y aceptación. La cola no puede
  añadir más de cinco segundos; no se cambia la hora de recogida para
  rejuvenecer datos. Una cotización no puede ser futura ni tener más de
  quince segundos al aceptarla.
- Cada vela nueva debe estar cerrada al recogerla y llegar dentro de quince
  segundos de su cierre. Las reglas anidadas y temporales superiores usan
  el evaluador común y sólo agregados completos. Entrada y salida se registran
  como condiciones; no se confunden con una ejecución.
- Más de veinte segundos sin una observación nueva, un cierre ausente/atrasado
  o el límite de cien mil velas detiene la fuente. No hay interpolación,
  reconstrucción ni reanudación automática. Una cotización repetida queda
  registrada sin renovar la cobertura; no acredita velas nuevas.

## Registro y lectura fría

Cada evento conserva input, enlace al anterior, decisión, explicación y
checkpoint acotado. `verify_signal_record` vuelve a calcular desde el seed y
los inputs anteriores. Recalcular un hash tras editar una decisión, cuenta de
señales, hora o vela no sustituye ese replay.

El checkpoint no contiene el histórico completo ni permite deserializar un
estado operativo. Las cotizaciones sin cierre nuevo conservan el prefijo
inmutable; no se serializan/recalculan cien mil velas en cada consulta.
Una lectura fría sólo produce evidencia con autoridad NONE, sin red, claves,
órdenes o autoarranque.

## Concurrencia y parada

`PerpSignalWorker` recoge mercado en una hebra independiente. El ejecutor puede
estar consultando órdenes, fills o una salida sin impedir esa observación.
El llamador debe aportar permiso RAM revocable y un escritor durable. No se
publica una candidata hasta que ese escritor confirma su evento.

STOP revoca en memoria antes de esperar al proveedor o almacenamiento. Si
una solicitud pública termina después, no puede publicar otra entrada. Un
fallo del escritor, incluso con commit de resultado desconocido, detiene la
hebra sin repetir la escritura. Un acuse de almacenamiento excesivamente lento
tampoco publica señales. Los fallos quedan diferenciados en el estado local.

Una candidata sólo es evidencia reciente. Caduca al superar quince segundos
desde el cierre/señal o cinco segundos desde la última observación. El ejecutor
todavía debe verificar concesión, identidad de la señal y estrategia,
presupuesto, selección durable única, permiso RAM y preflight actual antes
del envío. Consultar una candidata no concede autoridad ni la consume.

## Integración aún necesaria

1. La [concesión v4](GRVT_PERP_BOT_GRANT.md), el origen LAB/PAPER, el escritor
   cifrado, el prefijo RAM y la selección durable ya se implementan en el árbol
   de desarrollo. Se conserva la lectura de concesiones anteriores.
2. Integrar ese escritor y la revisión exacta con el ciclo de vida del
   coordinador; las pruebas de módulos no crean autoridad de producción.
4. Integrar la hebra con permisos, STOP, cierre de claves y coordinador REAL.
   El reloj/ticket compartido necesita comprobación RAM sincronizada entre
   hebras, sin mantener ese bloqueo durante HTTP o almacenamiento.
   Vigilar configuración de margen/apalancamiento y posición efectivas;
   resolver tarifas y protecciones LAST frente al precio medio real de fills.
5. Revisión/activación/seguimiento GUI y pruebas completas navegador → API →
   diario cifrado → proveedor controlado → cierre/conciliación. Después,
   piloto humano mainnet separado.

Las pruebas conectan fuente, diario y núcleo de entrada con permisos explícitos
de fixtures. Falta la integración del coordinador/GUI; no se presentan como
aceptación de un bot de producción.
