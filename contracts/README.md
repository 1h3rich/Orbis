# Ejecutor BNB de ORBIS — núcleo en desarrollo

Contrato local de pruebas para compras/ventas USDT ↔ WBNB bajo una política
explícita. No está publicado en mainnet ni conectado a un bot activable.
La arquitectura e integración pendiente se describen en
[DEX_BOT_BNB.md](../docs/DEX_BOT_BNB.md).

Requiere Node 22 o superior; se ha verificado con Node 24.19.0. Desde este directorio:

```sh
npm ci --ignore-scripts
npm run build
UWS_USE_FALLBACK=1 npm test
```

Las pruebas usan Ganache en memoria con cadena 56 y contratos de tokens, pool
y router controlados. No abren un puerto ni consultan wallets del usuario.
El flag permite usar la implementación JavaScript de µWS con Node 24.
No son operaciones mainnet ni una auditoría independiente del contrato.

`artifacts/BoundedSwapExecutor.json` contiene ABI, bytecode de creación,
plantilla de runtime, referencias inmutables, compilador, opciones y hashes
de fuentes/dependencias. Se genera con optimización de 200 pasadas, objetivo
EVM Paris y sin hash de metadatos en bytecode. No contiene propietario, claves,
un despliegue aprobado ni autorización para emitir transacciones. Compilar
dos veces con los mismos archivos produce el mismo artefacto.

El compilador también exporta las posiciones de los inmutables por nombre
AST (`owner`, `pool`, `poolFee`). La aplicación carga una copia fijada por hash
en `app/live_execution/artifacts/bnb_executor_v01.json` para componer CREATE y
verificar el runtime; Node no es necesario para ejecutar el servidor.

Las pruebas Python de integración del diario requieren antes `npm ci` en
este directorio. `support/deployment-fixture.mjs` expone una EVM en un puerto
efímero exclusivamente loopback; el cliente de pruebas verifica su identidad
aleatoria antes de enviar escrituras y cierra el proceso al terminar cada caso. Prueban
el mismo constructor, firma/recibo, programa, recuperación y bóveda cifrada
usados por la API. Sus cuentas y BNB son de prueba. No usan mainnet ni la
bóveda del usuario. Este puente no se inicia con la aplicación.

Las salidas se envían a la wallet propietaria. Sólo ésta configura políticas,
recupera fondos enviados por error y declara la separación de una posición tras
una intervención manual. El operador puede operar dentro del permiso y detener
su autoridad; no puede renovar límites. Las renovaciones consumen una revisión
de política para que STOP invalide las que estaban pendientes.

Las librerías Solidity compiladas tienen licencia MIT: OpenZeppelin Contracts
y los archivos TickMath, BitMath y CustomRevert de Uniswap v4-core. El resto de
v4-core, cuyo paquete general usa otra licencia, no se incorpora al artefacto;
el compilador rechaza importaciones v4 sin cabecera MIT. Véanse las cabeceras
y avisos de los paquetes fijados en package-lock.json.
