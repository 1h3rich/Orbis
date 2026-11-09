# Renovar una política tras revisión externa

Desarrollo verificado sobre REAL 78, sin publicar. La renovación anterior se conserva con
sus formatos; este tramo no transforma una revisión manual en ejecución del bot.

## Formatos y autoridad

- Control `orbis.dex.control.v5` y asignación sucesora `orbis.dex.bot.binding.v4`.
- Prueba LONG `orbis.dex.bot.policy-rollover.v3` y DCA
  `orbis.dex.bot.dca.policy-rollover.v3`.
- Continuación LONG v8 y DCA v5. También pueden continuar en la misma asignación
  sucesora anterior cuando una llamada externa se acepta después de crearla.

La revisión reconstruye todas las operaciones a su fecha de corte, conserva los
testigos externos autenticados y exige el estado físico finalizado del ejecutor.
Cada cambio previo de política necesita su recibo STOP probado. Un evento externo
revisado no aporta contadores de compra, pérdida, exposición ni posición.

La política nueva autoriza sólo el remanente del presupuesto raíz. El gas real
externo se descuenta una vez; los recibos y consumos de políticas anteriores
siguen enlazados. DCA conserva activaciones y límites mensuales, incluidos los
intentos externos que no fueron compras. No se habilita firma, conexión o worker
por aceptar efectos, preparar una renovación o registrar su recibo.

El usuario confirma CONFIGURE, se verifica su recibo exacto y se crea un único
sucesor. Continuar requiere reconectar la operadora y conceder un permiso nuevo.
Los lectores rechazan pérdida/corrupción de la aceptación financiera y formatos
que intenten omitir sus testigos.

## Transacción y pruebas

La captura financiera fija una transacción SQL explícita. La renovación abre
`BEGIN IMMEDIATE` antes de leer ese prefijo y compone el escritor de control con
`begin=False`; no abre otro BEGIN ni revierte las lecturas para escribir después.
El escritor independiente conserva `begin=True` por defecto. La captura no hace
un nuevo cálculo financiero ni RPC dentro del lease.

Los dos casos firmados LONG/DCA propios fallaron primero en el bloqueo anterior
de nonce pendiente. Tras conectar los perfiles, revelaron un segundo BEGIN;
se conserva esa evidencia y se corrige la composición transaccional. La tanda
`green-dev-r3` alcanza el permiso nuevo, pero el test intenta parar dentro de su
propia transacción de lectura. Se separa esa operación como hace el trabajador;
no se relaja el escritor productivo de parada.

`external-policy-renewal-green-dev-r4` pasa ambos casos: configuración/recibo
reales propios, sucesor, permiso nuevo, decisión LONG y compra DCA posterior,
gas, copia cifrada fría y rechazo de un enlace de aceptación eliminado. Son
173 segundos para ambos recorridos completos; no es una medida aislada de API
ni una aceptación de rendimiento. No son pruebas mainnet ni usan bases, fondos
o claves del propietario.

La ampliación añade una segunda preparación y comprueba el consumo acumulado,
incluso cuando el permiso actual ya no contiene un recibo externo propio.
La regresión afectada `external-policy-renewal-regression-dev-r5` termina con
214 casos/27 módulos y cuatro salidas 0. `external-policy-compatibility-dev-r6`
añade 33 casos/cuatro módulos diferentes: vinculación, API y procedencia OOS.
Las 247 identidades, sin solapamiento, coinciden con sus colecciones y XML sobre
las mismas 1.303 fuentes. Incluye dos casos nuevos. No se suman los 182 del corte
anterior ni los focos repetidos. Informe: `external-policy-final-dev-r7/verification.json`.

Faltan aceptación conjunta de varios
pendientes, interfaz completa, rendimiento de linajes y verificación/publicación.
