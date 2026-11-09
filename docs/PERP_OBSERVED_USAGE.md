# Probar una estrategia de derivados con precios actuales

El modo PAPER usa una cuenta ficticia nueva para cada activación. No utiliza
saldo de una wallet ni coloca órdenes en GRVT. El modelo de margen y fills sigue
siendo experimental: no acredita profundidad, liquidez ni supervivencia entre
los puntos públicos observados.

1. En **Ajustes → Privacidad y seguridad**, protege/desbloquea LAB y GRVT PAPER.
   Las bases anteriores sin cifrar necesitan la protección explícita del usuario;
   no basta con que su estado diga `unlocked: false`.
   Elige **Plazo** al crear/desbloquear GRVT PAPER: 15 minutos, 1/4/24 horas,
   7 o 30 días. El valor inicial siempre es quince minutos. El plazo es fijo;
   consultar o navegar no lo renueva. La clave sólo permanece en memoria.
   [Plazos de los demás almacenes](LOCAL_OBSERVATION_SESSIONS.md).
   Al caducar, bloquear o cerrar ORBIS se interrumpe la observación y puede
   quedar exposición ficticia pendiente. No se garantiza servicio 24/7.
2. En **Laboratorio**, guarda datos, perfil y una prueba de derivados. Ejecuta
   una reserva cronológica o ventanas sucesivas en **Validación de derivados**.
3. Selecciona una validación con reservas completas y sin exposición pendiente.
   Pulsa **Probar en PAPER**. La comparación de recorridos sin reserva no permite
   este paso.
4. Revisa mercado, intervalo, dirección, capital ficticio y margen. Pulsa
   **Activar ficticio** para recoger precios actuales e iniciar la observación.
   La revisión caduca a los cinco minutos. El histórico inicial sólo prepara
   indicadores: no crea compras anteriores a la activación.
5. Consulta la sesión en **PAPER de derivados**, dentro de LAB, o en
   **Perpetuos PAPER → Estrategias validadas**. Ambas vistas usan los mismos
   registros. **Ver** reproduce el resultado y muestra gráfico, operaciones,
   patrimonio, costes, financiación, diario y procedencia.

Desde **Ver**, pulsa **Comparar reserva LAB** con LAB y GRVT PAPER desbloqueados. ORBIS reproduce la instantánea seleccionada y las reservas históricas de la versión original. Cada periodo muestra capital, resultado neto, rentabilidad sobre su propio capital, cierres, comisiones y financiación. Los resultados conservan la diferencia entre cotizaciones observadas y trayectorias históricas simuladas. Una posición abierta se identifica en PAPER.

Las entradas aparecen en ámbar con `L` o `S`; las salidas, en rojo con `×`.
Se dibujan sobre sus precios de ejecución y sus segundos registrados, incluso
cuando aún no existe una vela cerrada para ese instante. No se inventa una vela
para colocar una marca. Los tiempos completos permanecen en el diario/export.
El selector de rango y **Cargar anteriores** permiten elegir cuánto histórico
ver. Al cargar páginas anteriores se conserva el mismo snapshot comprobado.

**Parar entradas** impide nuevas posiciones. Si existe una, continúa observando
en modo «Sólo cierre». **Cerrar posición** solicita una salida a la próxima
cotización válida: pulsar no genera un precio de venta. Interrumpir el trabajador,
cerrar ORBIS o perder cobertura puede dejar exposición ficticia en «Interrumpido».
Eso no equivale a haber ejecutado un stop o una liquidación durante el hueco.
Para finalizar ese escenario, **Estimar cierre** calcula un resultado separado
con financiación verificable y precio actual, tras aceptar el hueco. No reanuda
el bot ni declara observadas sus salidas. [Guía](PERP_GAP_CLOSE_USAGE.md).

Si se pierde la respuesta de activación, usa **Consultar activación** o
**Cancelar activación**. ORBIS conserva el ID pendiente y consulta el resultado;
no envía otro arranque automáticamente. Una cancelación aceptada antes del
arranque impide un inicio tardío. Si ya había empezado, interrumpe su observación
sin prometer deshacer una operación previa.

**Reproducir** obtiene otro snapshot verificado. Los importes de la inspección
pertenecen a su fecha indicada; la biblioteca muestra por separado el estado
operativo y la cobertura del trabajador. **Exportar JSONL** descarga el seed y
la cadena verificable, con un cierre obligatorio que detecta exportaciones
incompletas. Reiniciar o desbloquear recupera registros, nunca reactiva bots.

Este flujo no habilita la nueva referencia de derivados para operar con dinero
real. Las integraciones REAL existentes mantienen sus controles y formatos;
la revisión específica de esta nueva procedencia continúa pendiente.
