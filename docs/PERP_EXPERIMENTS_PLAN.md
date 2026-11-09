# Pruebas históricas de perpetuos — v97

Estado: v97 verificada y cargada; evidencia final por identidad en
`v97-perp-engine-r2/verification.json`. La referencia funcional sigue siendo la especificación completa,
no este tramo. No se ha usado una wallet, clave de exchange ni fondos del dueño.

## Contratos y decisiones

- `orbis.lab.perp.profile.v1` fija versión/hash de estrategia, evaluador
  `lab-1.16`, libro `perp-account-0.1`, modo de margen, apalancamiento y dos
  trayectorias OHLC. Sólo admite el modo `EXPERIMENTAL_CURRENT_RULES`.
  Capital, asignación, fees, slippage, stops, objetivo, trailing y regla de
  salida proceden de la definición inmutable; no se sustituyen silenciosamente.
- `perp-ohlc-1.0` simula una posición neta. Cada TRADE/MARK sigue de forma
  independiente apertura→máximo→mínimo→cierre o apertura→mínimo→máximo→cierre.
  Los extremos sintéticos se sitúan a un tercio y dos tercios de vela. No se
  presentan estos recorridos como ticks observados, límites estadísticos ni
  alternativas que garanticen contener toda posible ejecución real.
- Funding interior inserta su MARK real como ancla. Si coincide con un extremo
  sintético, éste se desplaza 1 ns. Funding en apertura se asienta primero y
  después se aplica el MARK de apertura. Un MARK de funding fuera del OHLC
  correspondiente se rechaza como datos incompatibles.
- Una vela cerrada genera señales para la apertura siguiente. La frontera
  asume cierre anterior→funding→apertura; MARK/liquidación precede a una salida
  TRADE simultánea. No se atribuye esa secuencia a un mercado observado.
- Se calcula el primer cruce dentro del segmento. Los cambios de tramo de
  riesgo se comprueban incluso si ambos extremos son sanos. La salud monetaria
  redondeada puede alcanzar cero y recuperarse 1 quantum: se enumeran sus
  discontinuidades alrededor del umbral, con cruces estrictos y no estrictos
  sobre el precio cuantizado. No se presume monotonicidad del saldo redondeado.
  Si resolver ese intervalo exige más de 4.096 discontinuidades, se rechaza la
  resolución a esa precisión en vez de inventar ausencia de liquidación.
- La asignación incluye margen inicial, pérdida inmediata adversa frente a
  MARK y comisión. Se comprueba cada bracket por separado y se redondea tamaño
  al paso del instrumento. El beneficio MARK inmediato no financia otra entrada.
- Las órdenes son de mercado con deslizamiento adverso y tick. No se inventan
  liquidez, libro, fills parciales observados ni financiación con tasas actuales
  en lugar de sus liquidaciones históricas. El núcleo admite fills parciales
  explícitos, pero este matcher OHLC no afirma reconstruirlos.
- El trailing sólo incorpora el extremo una vez cerrada la vela. Tras liquidación
  se detienen entradas nuevas. Una salida rechazada detiene la simulación con
  exposición abierta y periodo procesado explícito; no fabrica una venta.
- Al final se modela un cierre artificial a TRADE, no a MARK, antes del funding
  desconocido en la frontera final exclusiva. Ventanas independientes conservan
  el prefijo para indicadores y reinician caja/posiciones; aún no constituyen
  una referencia formal de validación OOS consumible por REAL.

## Archivo y seguridad

LAB 10→11 añade `perp_profiles` y `perp_runs` con claves hacia versión y dataset
perpetuo. Es una migración aditiva en transacción; no cambia datasets, operaciones
o métricas anteriores. Los lectores de datasets compartidos aceptan explícitamente
11 porque las tablas legadas mantienen su forma. Versiones desconocidas siguen
rechazadas. Las copias verifican las tablas nuevas y reproducen sus resultados.

Un perfil se deduplica por hash; una prueba idéntica recupera su registro sin
duplicarlo. Cálculo y reproducción ocurren fuera del bloqueo de escritura SQL.
Antes de guardar se comprueban otra vez originales, archivo, contraseña efímera y
época de sesión. Bloquear y desbloquear con la misma clave no revive el trabajo
anterior. El commit corto se serializa frente al bloqueo/reautenticación. La misma
barrera se aplica a la descarga de datos perpetuos.

`orbis.lab.perp.run.v1` fija inputs, ventana, motor y resultado. El listado sólo
declara hash/originales comprobados; un detalle o exportación recalcula desde los
originales y compara el resultado completo. `orbis.lab.perp.bundle.v1` exporta
datos, perfil, definición y resultado, con nanosegundos como texto y dinero exacto.
No concede permisos PAPER/REAL ni cambia los contratos de investigación existentes.

## Recorrido de usuario

1. Crear/desbloquear LAB cifrado en Ajustes, o proteger el legado existente.
2. Conservar TRADE/MARK/funding desde **Datos de derivados** y crear una estrategia.
3. En **Pruebas de derivados**, elegir versión, datos, margen, apalancamiento y
   recorridos hipotéticos; pulsar **Probar**.
4. Inspeccionar capital, costes, funding, drawdown, muestra y operaciones. El
   gráfico amplio permite alternar TRADE/MARK sin perder zoom y utiliza los
   controles comunes de años/fechas. Los marcadores incluyen entradas, salidas y
   liquidaciones; cada operación y evento conserva sus importes/instantes exactos.
5. Recuperar una prueba guardada, reproducirla o exportar JSON. Editar la estrategia
   no altera la versión del perfil ni el resultado anterior. La biblioteca de
   pruebas pagina; el selector de datos mantiene el límite actual de 100 archivos.

El gráfico agrupa los eventos de equity a segundos para dibujar; las métricas y
el diario mantienen todos los eventos. Los marcadores se sitúan en la vela que
contiene la ejecución y el detalle conserva el instante exacto. El dibujo usa
representación numérica visual; el dinero guardado/exportado sigue siendo texto
decimal. No se almacena la clave en el formulario ni en exportaciones.

### Comparar pruebas guardadas

Abre el backtest base y elige otro en **Comparar pruebas guardadas**. ORBIS reproduce ambas ejecuciones con sus perfiles y datos originales. La tabla distingue periodo, mercado, versión y estado. Con las mismas condiciones y dos tramos completos, la diferencia se calcula como alternativa menos base. Las diferencias de porcentajes se expresan en puntos porcentuales. Los cambios de capital inicial, costes, apalancamiento, margen o recorridos intravela se muestran como motivos de incompatibilidad para el cálculo.

Las curvas muestran el capital en las fronteras de vela. Cada punto usa la última observación del motor hasta esa frontera, y el último usa el cierre verificado. Las métricas usan todos los eventos del modelo. **Exportar comparación** vuelve a reproducir los originales y guarda la tabla, las curvas y las huellas en un JSON local sin cifrar.

### Sensibilidad de costes

Abre una prueba guardada y elige **Sensibilidad de costes**. Los dos campos
empiezan con los porcentajes originales. Modifica la comisión por ejecución o el
deslizamiento y pulsa **Comparar costes**. La tabla muestra ambos escenarios y
la diferencia calculada como alternativa menos base. Los porcentajes de
rentabilidad y drawdown se comparan en puntos porcentuales.

El motor conserva el histórico TRADE/MARK, el periodo, la financiación, el
capital, el margen y las reglas, incluidos los límites de entrada y los cierres
parciales. La comisión es una tasa uniforme por ejecución del modelo. El
deslizamiento sigue las reglas del tipo de orden y los límites de precio del
perfil. Si un tramo se interrumpe, deja exposición abierta o no alcanza el final
previsto, se omiten las diferencias. El JSON incluye el motivo y el instante
hasta el que avanzó cada escenario.

**Exportar JSON** vuelve a reproducir la comparación, verifica su huella y
descarga un archivo local sin cifrar tras confirmación. La prueba archivada
conserva su versión. El escenario es experimental y carece de autorización de
ejecución.

### Margen y apalancamiento

Abre un backtest guardado y elige **Margen y apalancamiento**. Introduce hasta
tres apalancamientos separados por espacios y selecciona el margen aislado,
cruzado o ambos. La primera fila reproduce el original y sirve de referencia.
La tabla muestra el capital en el corte de cada escenario. Las filas
interrumpidas indican su estado y si queda posición abierta. Una liquidación
incluye la hora y el MARK que la disparó en el modelo. Las entradas bloqueadas
muestran la causa disponible en el diario.

El periodo, el capital inicial, los datos TRADE/MARK, las reglas del
instrumento, la financiación y los costes se conservan. Cada escenario aplica
su propia asignación de tamaño con el apalancamiento elegido. Los perfiles
LIMIT y los objetivos parciales siguen sus reglas originales. Se pueden
procesar hasta 30.000 velas sumadas entre todas las repeticiones, incluido el
archivo completo que el motor coteja en cada una.

El resultado es experimental. **Exportar JSON** vuelve a ejecutar la
comparación, comprueba su huella y guarda un archivo local en claro tras
confirmar. La comparación conserva el archivo original y carece de autoridad
de ejecución.

## Optimización cronológica de salidas

**Laboratorio → Optimizar derivados** compara hasta cuatro stops con hasta
cuatro objetivos finales. Se introducen porcentajes separados por espacios.
El perfil, el capital, el margen, las comisiones, la financiación y las
reglas de señal permanecen fijos. La cuadrícula admite perfiles MARKET,
LIMIT y perfiles con objetivos parciales compatibles. Cada tramo requiere
30 velas cerradas. Se limita el coste estimado a 30.000 velas por simulación
del archivo multiplicadas por el número de combinaciones más una.

El desarrollo usa entre el 50 % y el 80 % de las primeras velas. Para
participar en la selección se requieren al menos diez operaciones cerradas,
sin liquidación, sin posición abierta y con una ejecución completa.
Gana la mayor variación neta de capital. En empate gana el menor drawdown,
seguido por el stop y el objetivo de menor valor. Sólo se evalúa el ganador
en la reserva cronológica. Si ninguno reúne los requisitos, no se evalúa
la reserva. Cada tramo empieza con una cuenta nueva, y la reserva sólo usa
el prefijo previo para las señales causales. Se muestran las interrupciones,
liquidaciones y muestras pequeñas de la reserva.

El informe es experimental. Una cuadrícula elegida tras observar la reserva
puede contaminarla. El cierre al terminar el tramo y las trayectorias
intrabar siguen siendo supuestos del simulador. El informe no es una versión
publicada de estrategia ni otorga autoridad de ejecución.

LAB 14 añade `perp_optimizations` con referencias al perfil y al archivo
histórico originales, plan canónico, huellas de ejecución y resultados.
Los informes iguales se guardan una vez. La reapertura y la copia cifrada
reproducen el cálculo. La exportación incluye los originales sin cifrar y
pide confirmación en la interfaz. La migración 13 → 14 añade la tabla y su
índice, conservando los datos existentes.

### Mapa de calor

Los optimizadores de LAB muestran el neto de desarrollo por stop (filas) y objetivo (columnas). La intensidad se escala con el mayor valor absoluto elegible de cada mapa. Una pérdida y una ganancia del mismo tamaño tienen la misma intensidad. Las combinaciones excluidas conservan su valor y se muestran con una trama. Un borde y la etiqueta **Elegida** identifican la selección archivada. Al posar el puntero se ve el valor decimal exacto.

En **Ventanas sucesivas**, cada ventana tiene su mapa y el desarrollo completo aparece antes de la reserva final. La intensidad se escala por separado en cada mapa. Para comparar ventanas, usa los importes. La reserva queda en su propio informe. El mapa lee las métricas archivadas y no vuelve a ejecutar ni seleccionar candidatos.

### Guardar y abrir la variante elegida

**Guardar variante** crea una versión de la estrategia de origen con los
parámetros ganadores y conserva su perfil de ejecución. En **Ver desarrollo**
y **Ver reserva** se abren las pruebas archivadas con el diario, las operaciones
y las marcas en el gráfico. **Abrir versión** selecciona esa versión exacta
en el editor, aunque haya versiones posteriores.

El motor conserva en memoria sólo las dos ejecuciones elegidas y reproduce
el informe antes de guardar. El resultado íntegro de cada prueba coincide
con la huella archivada en la cuadrícula. LAB 15 añade una relación entre
optimización, versión, perfil y ambas pruebas. Una sola transacción guarda
los registros. Los reintentos recuperan sus identificadores y no duplican
versiones ni pruebas. La exportación y la copia cifrada conservan la relación.

La versión y las dos pruebas pertenecen a LAB. El flujo de validación que
prepara PAPER mantiene su revisión independiente.

### Preparar PAPER desde la reserva de una variante

Después de **Guardar variante**, **Preparar PAPER** abre la revisión de la
reserva original. La revisión muestra el tramo, el capital ficticio, el
margen, el stop y el objetivo. **Activar ficticio** es el paso separado
que crea el observador. La bóveda PAPER debe estar desbloqueada para
revisar y activar. La validación se conserva en LAB y se puede reabrir
en **Validación de derivados → Reserva optimizada**.

La preparación vuelve a reproducir la cuadrícula y las pruebas de la
variante. El informe de validación vincula la optimización, la versión y
las pruebas de desarrollo y reserva por sus identificadores y huellas.
El perfil y el corte cronológico proceden de los originales. Una reserva
liquidada, interrumpida o con una posición abierta queda excluida.
Durante la confirmación se comprueba de nuevo el vínculo antes de guardar
la sesión PAPER.

El informe usa el contrato `orbis.lab.perp.validation.optimized.v1`, v2
o v3 según el perfil. Se conserva en la misma tabla cifrada de LAB 15.
Las validaciones previas conservan sus formatos, y la referencia PAPER
fija la huella de todo el nuevo informe. La simulación usa una cuenta
ficticia nueva y permite comparar su resultado con la reserva archivada.
Si se vuelve a consultar el mismo informe, la preparación reutiliza su
validación archivada.

### Revisar el origen antes de autorizar en GRVT

Tras seleccionar una prueba PAPER, la revisión de los bots perpetuos
MARKET y LIMIT incluye **Origen LAB y PAPER**. La sección muestra la versión
exacta, la validación, el periodo observado, las operaciones cerradas y
el prefijo del diario PAPER. Para una variante optimizada incluye también
la optimización y la prueba de reserva originales. Al pasar el cursor sobre
los identificadores abreviados se puede consultar el identificador o la huella
completa.

La versión se lee de LAB bajo la sesión verificada y se incorpora a la misma
revisión temporal sellada que contiene los límites REAL. Una modificación
del original invalida la confirmación. Consultar el origen deja la revisión
a la espera de autorización y la activación conserva el paso explícito.
La reanudación LIMIT sigue revisando el intento existente.

## Evidencia de desarrollo

- Oráculos LONG/SHORT con fees: cuatro combinaciones de dirección/trayectoria,
  cross/aislado, funding antes/después de entrada y financiación interior.
- Stop anterior a una liquidación posterior, cruce de mantenimiento por cambio
  de bracket aunque los extremos sean sanos y umbral contrastado con fracciones
  racionales independientes.
- Un caso de redondeo con extremos sanos y 80 trayectorias comparadas con un
  oráculo exhaustivo a nanosegundos. Contextos Decimal externos no cambian el run.
- Regla de salida, trailing, cierre final, prefijo de calentamiento, no heredar
  posiciones, invariancia ante sufijo futuro y salida rechazada sin falsa venta.
- SQLCipher, copia y reapertura exactas; resultado alterado rechazado incluso
  recalculando su hash; revocación/reautenticación durante cálculo o reproducción;
  originales modificados en el mismo inode; paginación y acceso/origen/DTO estricto.
- Navegador desde cero: crear LAB, descargar por adaptador real con mercado
  controlado, crear estrategia por UI, probar, ver marcas y curva, exportar,
  editar versión, copiar y recuperar en otro proceso. Prueba respuesta atrasada,
  bloqueo y manipulación con nuevo hash, escritorio/móvil y cero acciones REAL.
- La revisión visual detectó un rango capturado antes de pintar el frame que
  comprimía las velas al alternar TRADE/MARK. Se reutiliza el gráfico y sólo cambia
  su serie. El navegador comprueba las coordenadas reales de las velas, no sólo
  una etiqueta del DOM. Los intentos fallidos quedan conservados en los dev runs.
- Las primeras pruebas antiguas de migración conservaban tablas futuras al
  rebajar `user_version`; se corrigen sus montajes, manteniendo DDL estricto.
  La última comprobación preliminar pasa 270 casos. La regresión completa
  bajo `v97-perp-engine-r1` recorre 4.230 casos: 4.229 aprobados y
  una aserción antigua que aún consideraba desconocido LAB 11. Se conserva el
  fallo y se corrige sólo el test usando la siguiente versión desconocida.
  En r2 se repiten sus tres casos; la cobertura reúne 3 actuales + 4.227
  anteriores sin cambios. Los 50 recorridos se repiten con aplicación idéntica.
  No es otra tanda completa tras modificar ese test. Backend cargado y
  comprobado; fuentes fijadas, módulos/activos correctos y cero acciones privadas.

## Pendiente después de este tramo

Comparación/sensibilidad de recorridos, OOS/rolling persistido y sus referencias,
semántica compatible en PAPER, reglas históricas acreditadas para un modo realista,
mayor número de posiciones/activos y ejecución avanzada de la especificación.
Continúan además el cifrado gráfico de PAPER manual, la conciliación DEX restante,
la auditoría global de funciones/rendimiento y el piloto humano mainnet. No se
considera terminado el proyecto por cerrar esta interfaz.

## Contraste público y siguiente contrato de validación

El 5 de noviembre se descargan cuatro velas horarias BTC TRADE/MARK reales,
financiación con ancla y snapshots públicos. El motor completa una prueba sin
señales y otra LONG tras dos velas bajistas con una operación hipotética. Los
inputs y resultados quedan en la verificación local; no se usan credenciales ni
archivos del dueño. El primer intento del script tenía periodo 1 y el validador
lo rechaza; se corrige a 2 y se reutiliza la misma descarga. Esta comprobación no
acredita liquidez, fills de exchange, reglas históricas ni rentabilidad.

El [tipo oficial ApiFundingRate](https://api-docs.grvt.io/schemas/api_funding_rate/)
define `mark_price` en el instante `funding_time`, no como un promedio del intervalo.
Eso justifica tratar ese dato como ancla puntual. No prueba el recorrido entre
anclas ni el orden entre eventos simultáneos, que siguen siendo hipótesis visibles.

El siguiente corte debe fijar una **versión única de estrategia/perfil** y separar
las ventanas cronológicas antes de calcularlas. Se prevé un contrato nuevo de
validación perpetua, no una reinterpretación de `holdout-1.0` ni de referencias
GRVT v1. La reserva reinicia caja, colateral, posiciones, órdenes y funding
consumido; sólo utiliza el prefijo anterior para indicadores. La primera entrada
puede depender de la última vela cerrada anterior al corte. Los funding anteriores
no se cobran a una posición que acaba de empezar; los del instante de apertura
preceden a la entrada según el contrato del motor.

Criterios para ese corte, todavía pendientes:

- Reserva fija y ventanas sucesivas disjuntas, sin elegir parámetros en el tramo
  reservado. Mínimos de velas y alerta de muestra insuficiente explícitos.
- Resultado conjunto con ventanas, motores, hashes originales y resultados
  separados. No sumar las curvas de carteras reiniciadas como capital continuo.
- Registro inmutable, deduplicación, replay completo, copia cifrada y reapertura.
  Cálculo fuera del bloqueo SQL; época de acceso comprobada antes del commit.
- Comparación de los cuatro pares de recorridos TRADE/MARK con el mismo capital,
  reglas, versión y datos. La sensibilidad observada no es un intervalo garantizado.
- UI compacta con selección de ventana, operaciones/marcas y resultados propios;
  ninguna acción de validación activa un trabajador ni concede autoridad REAL.
- Nuevo protocolo de referencia perpetua tipado, exportable y de autoridad NONE,
  verificado contra originales antes de ofrecer una futura revisión PAPER/REAL.
- Oráculos independientes, invariancia al sufijo futuro, ausencia de exposición
  heredada y rechazo de resultados u originales alterados, incluso con hash nuevo.
