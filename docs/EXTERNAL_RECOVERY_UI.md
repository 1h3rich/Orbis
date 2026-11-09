# Recibos externos · recuperación en ORBIS

La revisión de recibos externos está disponible en ORBIS local v115.

Desde **Intercambiar · BNB → Historial DEX** o el historial de un bot parado,
**Recibo externo** abre la lectura de un reemplazo ya minado. Se revisa su
observación y se guarda su orden de bloque. Un fallo en la segunda lectura
conserva la observación verificada para continuar, sin repetir transacciones.

El panel **Recibos externos** agrupa los recibos guardados. Para revisar,
verifica las wallets de la custodia y sus contextos. Las otras wallets se
eligen por alias y proveedor EIP-6963. Los controles firman únicamente el
desafío de control EOA. Las cuentas se muestran abreviadas en el selector.

Cada recibo muestra gas y movimientos observados. **Traslado** usa las dos
custodias probadas, conserva unidades y deja que el motor derive el coste de
los activos. **Compra** y **Venta** permiten declarar WBNB/USDT a partir del
saldo neto observado; su importe bruto se conserva como decimal exacto y el
precio unitario informativo se deriva con enteros. **Sin movimiento** declara
que los efectos seleccionados no cambian la cartera; el servidor lo comprueba
en unidades exactas. El gas admite un gasto nuevo o asignar asientos originales.
La declaración avanzada permite otros asientos, asignaciones, anotaciones y
precisiones; sus validaciones siguen en el servidor.

La casilla **Recibo revisado** declara la cobertura restante y los permisos.
Los registros no clasificados permanecen `UNKNOWN` hasta declararlos y bloquean
la confirmación. **Revisar selección** muestra saldo, aportaciones, retiradas,
posiciones, costes y resultado actual/propuesto, junto con los bloqueos. Uno o
varios recibos usan la misma interfaz; el conjunto produce un solo criterio y
un registro por origen. Revisar no escribe asientos.

**Confirmar registro** conserva la selección y su huella. Si se pierde la
respuesta se consulta la aceptación guardada; **Comprobar estado** permite
repetir sólo esa lectura. El historial muestra **Revisada · externa** y
**Ver registro**. Después de reabrir la bóveda, la lista de registros guardados
se puede consultar sin conectar ninguna wallet ni llamar al proveedor. Ninguna
de estas acciones inicia bots ni convierte efectos externos en compras de una
estrategia.

**Ocultar** en el panel retira los importes y el editor de la vista, y desactiva
la confirmación mientras están ocultos. Cambiar de cuenta, red o proveedor,
cerrar el panel o bloquear la bóveda cancela las lecturas pendientes, invalida
sus respuestas y borra borradores, hashes y direcciones transitorias del panel.
Si ya se había confirmado, se puede recuperar el registro desde la bóveda.
No se usan almacenamiento del navegador ni copias de datos del propietario.

## Decisiones y pruebas

- `economic_external_workspace_api` añade catálogo de fuentes con EOA/contexto,
  datos de edición privados y catálogo cifrado de registros guardados. Limita
  candidatos originales a dos minutos alrededor del recibo, conserva sus IDs
  y excluye los ya asignados. Respuestas sin direcciones ni hashes completos.
- El componente común `external-review.js` usa los puertos de observación,
  orden, aceptación individual y conjunta existentes. La vista usa la misma
  lectura SQL y Decimal para comparar libros. No calcula el criterio en JS.
- Los recorridos mixtos LONG/DCA con dos firmas de una EVM propia comprobaron
  gas separado, pausa del trabajador y ausencia de fills inferidos. El informe
  inicial de dos casos es `external-joint-mixed-dev-r10`.
- Los dos recorridos de navegador con SQLCipher y Anvil pasaron en
  `external-review-integrated-dev-r17`: desde historial a recibo nuevo y desde
  dos wallets a un criterio, incluidos reapertura y respuesta perdida. La misma
  tanda tenía dos aserciones de prueba erróneas sobre la forma pública del
  resultado del bot; se conservan los fallos y se comprueba su testigo real en
  la siguiente tanda. No se suman tandas parciales como una ejecución completa.
- La fuente candidata de 1.319 archivos está fijada en
  `external-recovery-candidate-dev-r19/source-manifest.json`. Sobre esa fuente
  pasan **13 recorridos de navegador** y **5 pruebas focales** de las fuentes
  mixtas, catálogo y reapertura. La tanda general de 5.427 casos pasa 5.426 y detecta una expectativa antigua en la lista de capacidades. Se corrige sólo esa expectativa y pasan los 14 casos del módulo. La cobertura final coteja las 5.427 identidades en 454 archivos. Las cinco focales están
  incluidas en esa cobertura. Evidencia en `~/.local/state/orbis/verification/v115-external-recovery-r1/verification.json`.
- La comprobación previa de publicación compara 1.275 fuentes de v114:
  49 modificaciones y 44 incorporaciones, sin cambios concurrentes en esos
  archivos, colisiones ni eliminaciones. La corrección posterior añade un
  cambio de test a ese conjunto antes de la carga.

Quedan rendimiento con historiales extensos, otros adaptadores de nonce y la
aceptación de todos los requisitos de ORBIS, incluido el piloto configurado por
el propietario. Las pruebas propias no operan con sus fondos o claves.

En una bóveda propia, dos recibos firmados y 24 criterios posteriores conservan
el coste y el gas tras recuperar el primer grupo y reabrir una copia cifrada.
La recuperación a los 25 criterios usa 1.995 consultas y tarda 0,273 s bajo
carga compartida. El informe `external-joint-signed-lineage-dev-r20/result.json`
delimita la muestra a dos recibos.
