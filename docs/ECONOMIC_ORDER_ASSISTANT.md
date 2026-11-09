# Orden de movimientos simultáneos

Estado: incorporado en v113 y conservado en v114, en `127.0.0.1:8000`.

El libro ya admite `TieOrder` explícitos, incluidos ciclos de traslados entre
cuentas y gas asignado a caja/posición según el instante económico. Faltaba
revisar esos órdenes desde la interfaz. El asistente cubre recibos ya
contabilizados y lotes nuevos, financieros o de gas, sin cambiar los motores,
formatos publicados ni originales.

## Contrato

- Una ruta privada `order-options` captura las mismas fuentes autenticadas,
  selección y cierre de cuentas que la revisión. Fuera de SQL construye grupos
  por cuenta e instante con al menos un movimiento manual y dos nodos. Los
  grupos que sólo contienen aportaciones positivas se omiten porque conmutan. No
  calcula saldos, materializa asientos, consulta RPC ni concede permisos.
- Cada transacción comprobada es un nodo indivisible. Sus operaciones y gas
  permanecen juntos. El índice real fija la subsecuencia de nodos de red;
  los controles sólo cambian la posición relativa de movimientos manuales.
- El orden inicial es una propuesta. Sólo los grupos marcados «Fijar orden»
  generan `tie_orders`. La revisión financiera conserva las restricciones de
  red, nonces y traslados; un ciclo o inventario insuficiente sigue siendo error.
  Cuando el criterio vigente conserva exactamente los mismos nodos, se recupera
  su orden marcado como vigente. Si cambia la composición del grupo, vuelve a
  ser una propuesta; no se amplía automáticamente una decisión anterior.
- Una identidad de opciones liga revisiones originales, fuentes, precedentes,
  selección, nombres y perfil. Una previsualización con opciones antiguas se
  rechaza. La aceptación conserva el digest financiero existente y revalida
  todos los originales; recuperar no vuelve a enviar ni cambiar la selección.
- La capacidad booleana `economic_review_order` habilita el asistente. Un
  backend anterior conserva sus solicitudes. No se guardan direcciones,
  decisiones ni borradores en almacenamiento web; cambiar wallet/contexto,
  bloqueo o selección elimina las opciones anteriores.
- Grupos y nodos se paginan; no se trunca una selección ni un grupo grande.
  Importes exactos en ayudas, ocultación de valores, botones y foco accesibles.

## Aceptación requerida

1. Propuesta de operación y gas nuevos, con asientos manuales reutilizados.
2. Recibos existentes y un ciclo de traslados resoluble con orden explícito.
3. No invertir nodos de red, perder/duplicar nodos ni reutilizar asientos.
4. Cambios de libro/selección invalidan opciones; bloqueo/contexto durante
   captura y respuesta tardía no muestran ni guardan otro contexto.
5. Comparación y guardado atómico, recuperación por digest, reapertura cifrada
   y originales intactos con recibos firmados y bases propias.
6. Regresiones de v112, formatos financieros idénticos y escritorio/móvil.

La [revisión de wallets nuevas simultáneas](ECONOMIC_MULTI_WALLET_REVIEW.md)
se incorpora en v114. Los efectos de llamadas externas desconocidas y el
rendimiento de grandes catálogos siguen dentro del objetivo completo. La
evidencia v113 siguiente se conserva como historia de esta entrega.

## Evidencia de desarrollo

La batería final focalizada pasa **243 casos / 19 módulos**, con identidades
cotejadas, sin fallos, errores ni omitidos. Incluye 22 casos nuevos.
Siete casos puros y seis HTTP verifican nodos financieros y de gas, selección
de asientos, revisión existente/nueva, orden vigente, cambios de libro/perfil,
rechazo de índices invertidos y revocación mientras se calcula fuera de SQL.
El caso HTTP de ciclo restaura sólo metadata de cuatro eventos nuevos en una
base propia: el cálculo actual falla y se presenta como error, sin fallback;
la aceptación explícita repara el orden con caja y prefijo anterior intactos.

Nueve casos nuevos de Chromium cubren controles, capacidades booleanas,
solicitud exacta y respuesta perdida, paginación de grupos/nodos con foco,
ocultación y respuestas tardías tras cambiar wallet/sesión o cerrar el diálogo.
Un backend sin la nueva capacidad conserva la solicitud de revisión anterior.

Pasan dos recorridos nuevos de navegador con recibos firmados reales en Anvil
y SQLCipher propios: lote nuevo y criterio existente. Cambian visiblemente
el orden de compra/venta manual respecto a dos recibos y comparan unidades,
coste y P/L con un cálculo independiente a precisión 300. Comprueban recepción
perdida, prefijos intactos, cero envíos de wallet, escritorio/móvil y reapertura
que recupera el orden vigente. Pasan cinco regresiones de navegador adicionales
sobre las mismas 1.270 fuentes congeladas. El fallo inicial del recorrido nuevo
era su algoritmo de clics: dejaba la venta antes de la compra. Se corrigió sólo
la prueba y se conservan su DOM, captura y log.

La repetición adicional de los 19 recorridos básicos/de compatibilidad deja
**26 recorridos actuales** sobre las mismas fuentes, con MEXC, GRVT, DEX,
primer uso LAB/PAPER y cartera/DCA. Evidencia adicional:
`~/.local/state/orbis/verification/economic-order-assistant-basic-compat-final-r1/browser.json`.

El constructor puro de opciones procesa 100.007 asientos sin grupos relevantes
en 0,364 s y 10.007 con 5.000 grupos en 0,044 s, conservando originales, bajo
carga concurrente. No mide captura SQL ni pintura de 5.000 grupos. La batería
general pasa **5.189 casos / 432 archivos**, con identidades cotejadas, cuatro
salidas 0 y sin fallos, errores ni omitidos. Evidencia original:
`~/.local/state/orbis/verification/economic-order-assistant-dev-final-r1/`.

## Carga verificada original · v113

Ocho motores y puertos financieros/de archivo conservan sus hashes de v112.
No hay migración ni cambio de formatos financieros. La única capacidad nueva
es `economic_review_order`; la versión SQL preparada sigue en 75. No se
desbloquean, migran o protegen almacenes del propietario ni se utilizan claves
o fondos. El proceso v112 se detuvo tras comprobar identidad y estado inactivo.
v113 usa una única instancia, PID 824816 / start ticks 6312693, mismo puerto.
Se verifican 51 recursos y 58 rechazos privados anónimos, escritorio/móvil sin
errores JS ni escrituras durante la comprobación pasiva. Los seis archivos del
propietario conservan sus hashes; el cargador no los abre como bases de datos.
La herramienta del navegador no permitió recargar la pestaña del usuario;
la comprobación real de la instancia usa Chromium propio, sin desbloquear REAL.
`latest.json` apunta a la evidencia cargada:
`~/.local/state/orbis/verification/v113-economic-order-assistant-r1/verification.json`.
Los pendientes de wallets, efectos externos, rendimiento y auditoría global
continúan abiertos; esta entrega no completa el proyecto.
