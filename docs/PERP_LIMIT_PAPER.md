# Órdenes LIMIT en PAPER observado

En **Laboratorio → Validación de derivados**, abre una reserva completa de un
perfil LIMIT y pulsa **Probar en PAPER**. La preparación muestra el mercado,
la dirección, la caja ficticia, el margen y la vigencia. **Activar ficticio**
empieza una nueva cuenta. El histórico previo aporta el contexto de las reglas.

Una vela nueva cerrada puede admitir una orden LONG o SHORT. El cierre de señal,
el desplazamiento y el tick fijan el precio límite con la misma fórmula de LAB.
PAPER usa el ask para compras y el bid para ventas. El modelo considera una
ejecución completa al cruzar el límite con una cotización posterior a la
admisión. El deslizamiento se limita al precio de la orden. La cola del libro
y la cantidad ofrecida en el mejor precio quedan fuera de este modelo.

**Ver** muestra el límite pendiente, el saldo disponible y su vencimiento UTC.
Una ejecución abre una posición y aparece como marcador en el gráfico. Al
alcanzar el vencimiento se libera la reserva antes de evaluar otra ejecución.
**Parar entradas** cancela la orden. Una pérdida de cobertura cancela la orden
en el diario al registrar la interrupción. Si el proceso termina sin registrar
ese evento, el último estado se conserva como antecedente de una sesión inactiva.

El seed y el checkpoint LIMIT tienen versión 2 y motor `perp-observed-2.0`.
El formato de cotizaciones, financiación y controles se comparte con v1.
La cuenta se recupera reproduciendo esos eventos y verificando sus hashes.
La base PAPER existente admite ambos formatos. El JSONL exportado permite
repetir el cálculo fuera del proceso que observó las cotizaciones.

Una prueba completa permite revisar el [bot LIMIT en REAL](PERP_LIMIT_REAL_CORE.md) desde v121.
Las sesiones PAPER conservan autoridad `NONE`.

## Salidas parciales

Abre una reserva completa de un perfil con salidas parciales y pulsa
**Probar en PAPER**. La cuenta ficticia aplica los mismos porcentajes de LAB
tanto para entrada a mercado como LIMIT. Los objetivos se calculan desde el
precio ejecutado. La cantidad de cada salida se calcula sobre el tamaño
inicial y se redondea al lote.

PAPER evalúa el bid para cerrar LONG y el ask para cerrar SHORT. Si una
cotización cruza varios objetivos, los procesa en orden al precio observado
con el deslizamiento del perfil. Un objetivo inferior al mínimo se registra
como omitido. MARK y financiación se aplican antes de evaluar las salidas de
la cotización. Un stop o cierre manual cierra la cantidad restante.

**Ver** distingue **P1**, **P2** y el cierre final. El diario conserva
cantidades, precios, comisiones y resultado. Una pérdida de cobertura
conserva la posición restante y sus parciales. Un posible cierre estimado
se guarda separado del registro observado.

El seed, el checkpoint y la referencia de reserva v3 se verifican al reproducir
y exportar. Los originales LAB v3 conservan sus huellas e identificadores
históricos. La base cifrada existente admite v1, v2 y v3. Los perfiles con
salidas parciales se activan en PAPER. Las entradas LIMIT también se pueden activar en [GRVT REAL](GRVT_PERP_PARTIAL_REAL.md).

## Verificación

Pasan 1.092 casos en 82 módulos afectados, incluidos seis casos nuevos.
Los casos nuevos cubren LONG y SHORT, orden pendiente, vencimiento, parada,
interrupción, archivo cifrado y rechazo de la revisión REAL. Se comparan
24 resultados LIMIT históricos y ocho secuencias MARKET observadas con v119.
Los 32 resultados completos coinciden.

Ocho recorridos de navegador incluyen activación, orden pendiente, marcadores,
cierre y reapertura LIMIT en otro proceso. El proveedor controlado del recorrido
entrega los datos de cotización. La reapertura recupera y verifica el JSONL con
PAPER desbloqueado y LAB cerrado.
