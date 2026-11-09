# PAPER de larga duración — decisión pendiente de implementación

## Límite actual

MEXC Spot y GRVT conservan en cada sesión un JSON con la semilla LAB y todas
las velas posteriores. El replay comprueba ese prefijo completo y la
continuidad. Al llegar a 5000 velas, la sesión pasa a `CAPACITY` y deja de
crear señales y operaciones; **no** traslada ni cierra posiciones ficticias.
El detalle API expone `capacity_remaining` y la interfaz avisa en las últimas
500 velas. Esto es un límite operativo visible, no continuidad resuelta.

## Diseño objetivo

1. Guardar las velas observadas en una tabla por sesión con índice y tiempo
   únicos, digest encadenado y referencia inmutable a dataset, versión LAB y
   motor. Migrar el JSON existente en una transacción, sin cambiar sus señales
   ni el libro PAPER.
2. Persistir el estado exacto de indicadores entre velas, versionado con el
   evaluador. EMA y MACD no se pueden recalcular con sólo las últimas N velas
   sin cambiar su resultado; RSI, ATR, reglas multi-temporalidad y estructuras
   también necesitan pruebas de paridad en los límites de segmento.
3. Confirmar vela, observación, intención y estado de indicadores de forma
   atómica. Tras un reinicio, reanudar desde la siguiente vela esperada;
   si se perdió la ventana, entrar en `GAP`, nunca inventar una ejecución.
4. Reproducir la sesión por segmentos y paginar velas, eventos y marcas en la
   API y los gráficos. El verificador debe detectar un segmento alterado y
   no presentar una comparación LAB como fiable.

## Puertas de aceptación

- Más de 10000 velas en una sesión sin transición `CAPACITY` ni crecimiento
  lineal del trabajo por sondeo.
- Señales idénticas al evaluador completo para EMA, MACD, RSI, ATR, reglas
  anidadas y multi-temporalidad, antes y después de reiniciar.
- Posición abierta al cruzar un límite de segmento: ni doble entrada ni
  cierre ficticio; `GAP`, parada y cierre manual conservan su semántica.
- Copia, restauración y migración de bases PAPER cifradas verificadas;
  alteración de una vela archivada detectada antes de comparar o actuar.

No se aumentará únicamente el número 5000: eso aplazaría el corte, pero
mantendría el crecimiento del JSON y el replay completo por cada sondeo.

## Planificación del observador Spot

El planificador consulta únicamente las columnas de las sesiones activas
necesarias para calcular cuándo vence el siguiente cierre o el control de
una posición abierta. El observador devuelve un resultado breve y evita
construir el detalle que consume la interfaz. En un control pendiente, el
motor sigue cotejando el historial, la cadena y el estado antes de actuar.
Los estados de riesgo incoherentes pasan a GAP y conservan el diario.

Cada observación y cierre manual se vincula a la sesión de desbloqueo con la
que empezó. La revocación se comprueba al confirmar la transacción. Si se
bloquea o se renueva el acceso mientras llegan los datos o se escribe, se
revierten la vela, el asiento y los cambios de estado que seguían pendientes.
El sondeo conserva el comportamiento tras una interrupción. Cuando se pierde
la ventana, registra el hueco y conserva el saldo ficticio.

Sigue vigente el límite de 5000 velas. Retirarlo requiere sustituir el JSON
creciente y validar el replay de sesiones activas por segmentos.

## Avance implementado

MEXC Spot PAPER tiene una tabla `observed_candles` con hora única por
sesión. Las bases v1/v2 se migran en una transacción y se rellenan desde las
observaciones archivadas, comprobándolas contra el histórico de la sesión.
GRVT PAPER tiene el archivo equivalente `paper_signal_candles`; las bases
v8 se migran y cotejan del mismo modo. Cada vela nueva se guarda en la misma
transacción que su observación y el cambio de estado o posición ficticia. Las
verificaciones de copia exigen concordancia entre tabla, evento e histórico.
El seguimiento activo también comprueba esa concordancia antes de evaluar
una nueva vela o ejecutar una intención PAPER; si falla, pasa a `GAP` y no
genera nuevas operaciones ficticias.
Spot v5 y GRVT v11 añaden además `session_candles` y
`paper_signal_all_candles`: semilla y observaciones, con origen explícito,
backfill transaccional y lectura API paginada (máximo 1000 velas por página).
Las copias y el sondeo comprueban la concordancia con el JSON de sesión; un
fallo de escritura revierte la observación. La API local también pagina los
eventos Spot y las observaciones GRVT mediante cursores estables. Aún faltan
el replay segmentado y retirar el JSON creciente. Las marcas de señales GRVT del gráfico
se cargan por páginas del rango visible. El gráfico Spot obtiene velas y
marcas por páginas de la sesión, preserva los cierres manuales y descarta
mezclas de páginas si cambia la sesión durante la carga. Su detalle sigue
incluyendo los eventos completos como ruta de compatibilidad, pero la vista
consulta un resumen sin diario y lee los eventos recientes por páginas
descendentes. «Ver anteriores» conserva acceso al historial sin descargarlo
entero al abrir la sesión.
Se mantiene el JSON
íntegro, el trabajo lineal de verificación y el límite de 5000 en ambos
módulos; esta tabla sola no resuelve la continuidad de larga duración.

Spot PAPER v6 añade `session_archive_chain`: una huella SHA-256 encadenada
por vela, que incorpora sesión, tiempo, origen y el JSON exacto archivado.
La migración desde v5 rellena la cadena en una transacción; cada observación
nueva guarda vela, evento, estado y huella en la misma transacción. El sondeo
y la verificación de copias detectan una huella divergente antes de actuar.
No es una firma contra modificaciones intencionadas de toda la base ni
elimina todavía el trabajo lineal o el límite de 5000 velas.
GRVT PAPER v12 aplica el mismo mecanismo a `paper_signal_archive_chain`:
migración v11→v12 y escritura de vela, observación, estado y huella en una
única transacción. El monitor y la copia verifican la cadena antes de tomar
otra decisión ficticia.
Las páginas de velas de ambos módulos cotejan ahora su tramo con la huella
anterior, dentro de una instantánea de lectura. Una página alterada da error
en vez de llegar a la gráfica. Esto verifica la consistencia local del tramo;
no reproduce todavía las reglas de estrategia ni autentica el ancla previa
por sí sola. Una prueba de archivo supera 10.000 velas en páginas de 512,
pero las sesiones activas conservan su límite de 5000.

Ya existe un primer bloque de estado incremental serializable para EMA, MACD
y ATR. Las pruebas contrastan cada punto durante 10050 velas, con
serialización y recuperación a mitad de la serie, tanto a precisión actual
como legada. MACD conserva una peculiaridad del motor anterior: sus EMA
internas usan precisión 80 incluso si la resta de líneas usa precisión 28.
Spot PAPER v5 y GRVT PAPER v11 guardan este estado para reglas de su
temporalidad base, lo actualizan en la transacción de observación y cotejan
su contenido con el histórico al verificar una copia. **No gobierna todavía
las señales**: el evaluador completo sigue siendo la autoridad. Faltan los
demás indicadores, estructuras, temporalidades superiores y paridad de
estrategia completa antes de cambiar el límite operativo.

Hay un evaluador secundario de ventana acotada (512 velas) para reglas de
temporalidad base y el motor actual. Se han cotejado coincidencia y evidencia
de todos los tipos de regla base en límites de ventana y tras 10050 velas.
Spot y GRVT ejecutan esa comparación en paralelo al alcanzar 512 velas; si
discrepa, pasan a `GAP`. El evaluador completo sigue decidiendo. Motores
legados y reglas multitemporales conservan sólo el camino histórico hasta
demostrar una equivalencia específica.

La comparación LAB↔PAPER reproduce ahora también todas las observaciones de
reglas base con la semilla LAB, estado incremental y una ventana máxima de
512 velas. Lee velas encadenadas por páginas y observaciones mediante cursor;
no necesita el JSON histórico de sesión para este segundo recorrido. Coteja
versión y dataset LAB, semilla, continuidad, huellas, velas observadas y
evidencia de cada señal. Si discrepa, la comparación deja de considerarse
verificada. La prueba incluye reglas anidadas a través de 10.050 velas y el
cruce de la frontera de calentamiento. El motor histórico completo sigue
siendo autoridad y la vista todavía carga su JSON y eventos completos antes
de ejecutar ambos recorridos; no se ha retirado el límite operativo.

El stop móvil GRVT al cierre de vela y en el feed de riesgo se reconstruye
desde el archivo encadenado por páginas, sin leer `candles_json` para ese
cálculo. La decisión al cierre excluye la vela recién archivada cuando aún no
forma parte del estado previo. El feed intravela conserva en memoria sólo el
nivel derivado de la misma posición y última vela cerrada; tras reiniciar lo
recalcula. Antes de cada tick sigue verificándose el archivo frente al JSON
de sesión, de modo que la caché no elude una divergencia. Esa verificación
completa aún cuesta O(n) por tick: falta un estado persistido y verificable
para retirar el JSON creciente y el límite de 5000 velas.
