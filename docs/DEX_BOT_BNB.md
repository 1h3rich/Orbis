# Bot DEX BNB — ejecución acotada en desarrollo

Estado actual del código: integración local con API, trabajador y panel de
activación LONG/DCA, operadora dedicada, límites, parada, rearme y retorno de gas.
Los recorridos están verificados en EVM propia. v84 está cargada: conserva
resolución revisada de llamadas, contabilidad del resultado ganador y continuidad
LONG/DCA, y añade recuperación manual con la respuesta de wallet aún pendiente.
La renovación tras esos resultados sigue pendiente. No se ha realizado el piloto
humano ni desplegado el ejecutor mainnet con wallets o fondos del usuario.
Las secciones que describen cortes v58–v67 conservan el contexto de aquellos
hitos; no indican la disponibilidad actual. Véase el
[progreso vigente](PROGRESO_V0.2.md) y la
[resolución de llamadas](DEX_CALL_RESOLUTION_PLAN.md).

## Decisión de autorización

La documentación actual de MetaMask incluye BNB Chain entre las redes con
permisos ERC-7715. Sin embargo, `erc20-token-allowance` y `erc20-token-periodic`
usan `ERC20PeriodTransferEnforcer`: exige una llamada simple `transfer` al token,
no `approve`, `exactInputSingle` ni un lote de swaps. No se presentará una
concesión de transferencias como autorización segura de trading. Trust Wallet
no se supone compatible con ese mecanismo por el mero hecho de ser EVM.

Se prepara un ejecutor por wallet, con propietario inmutable y protocolo
cerrado: BNB Chain 56, USDT Binance-Peg, WBNB, Uniswap v3 Router02 y factory
oficial. El propietario autoriza una política y los permisos ERC-20 desde su
extensión. La clave de la wallet principal no pasa por ORBIS. El ejecutor sólo
recibe el importe de cada operación y entrega la salida a la misma wallet;
no es un depósito de patrimonio ni permite destinatarios arbitrarios.

## Controles del núcleo

- Clave operadora separada; sólo el propietario puede configurar y sustituir
  la política. Propietario u operador pueden detenerla. Configurar consume la
  revisión previa: una parada confirmada invalida revisiones anteriores que
  aún no se hayan aplicado.
  Identificador de política y nonce evitan repetir una
  orden o recuperar autoridad con una operación encolada de otro permiso.
- Presupuesto acumulado de compras, importe por entrada, exposición limitada
  tanto a coste como a valor TWAP antes/después del swap, máximo de entradas y
  caducidad. Las pérdidas realizadas acumuladas bloquean nuevas compras; no
  se compensan con beneficios ni incluyen el gas BNB del operador.
  Vender sólo reduce la cantidad atribuida al bot; no vende WBNB ajeno.
- Cuota acumulada WBNB para permitir aprobaciones finitas de ambos tokens.
  Vender no repone el presupuesto USDT ni la cuota de unidades adquiridas.
  Al conceder otra política, las posiciones y su coste permanecen y las unidades
  anteriores cuentan para la nueva cuota. No exige aprobación ilimitada.
- Permisos de compra y venta independientes. DCA sólo de compras no autoriza
  ventas. Un permiso exclusivamente de cierre tiene presupuesto de entrada cero.
- Salida a la wallet original. Cantidades reales verificadas mediante cambios
  de saldo, permiso exacto al router y retirada de ese permiso tras el swap.
- Mínimo de salida comparado con TWAP del pool canónico. No se sustituye por
  precio puntual ni se acepta una cotización aportada por el operador sin límite
  independiente. Datos insuficientes del pool impiden operar.
- Los límites de nuevas compras no impiden reducir una posición propia dentro
  del plazo autorizado. STOP revoca el operador; las unidades siguen en la wallet.
- Aritmética entera de 18 decimales y coste proporcional con resto asignado al
  cierre completo. Las cifras locales y fiscales se mantienen separadas.
- Los tokens/BNB enviados accidentalmente al ejecutor sólo se recuperan hacia
  el propietario. Tras una intervención externa se puede desligar la posición
  declarada, con STOP y revisión exacta del estado; no inventa un fill o P/L.

El contrato fija máximos de 1.000 USDT por compra, 10.000 de presupuesto,
exposición y salida, hasta 1.000 entradas y permiso de 24 horas. El TWAP admite
300–86.400 segundos y la desviación máxima respecto al TWAP 1–500 bps.
Los intercambios tienen un plazo
de transacción máximo de 120 segundos. Cambiar de versión requiere otro
despliegue explícito; el ejecutor no tiene proxy de actualización ni ejecuta
calldata arbitraria. El TWAP es un control de precio, no una garantía contra
manipulación ni un stop loss. El mínimo también puede bloquear una salida si
el mercado cae rápidamente respecto al TWAP; el operador no puede omitirlo.
Los stops de estrategia necesitan el trabajador y no garantizan ejecución. Una
intervención desde la wallet se registra aparte, sin atribuir un fill al bot.

## Fórmulas y alcance de los resultados

Todas las unidades monetarias del contrato son enteros de 18 decimales.
Comprar suma el USDT realmente transferido al coste y el WBNB realmente
recibido a la cantidad propia. Vender una parte asigna
`floor(coste_anterior × cantidad_vendida / cantidad_anterior)`; vender todo
asigna el coste restante completo. La pérdida acumulada suma únicamente
`max(coste_asignado − USDT_recibido, 0)`, sin compensar beneficios anteriores.
El gas BNB no forma parte de esa magnitud; debe registrarse en su cuenta real.

La referencia usa `floor((tick_acumulado_actual − tick_acumulado_anterior) / segundos)`.
El redondeo también va hacia menos infinito para valores negativos. TickMath
convierte el tick medio a raíz de precio Q96 y Math.mulDiv produce una salida
entera conservadora; se sigue la convención de Uniswap de precio desde tick
medio, no una media aritmética de cotizaciones de otro exchange. El mínimo
declarado no puede ser inferior a `floor(salida_TWAP × (10000 − desviación_bps) / 10000)`.
El núcleo cliente v61 añade su slippage sobre una simulación actual de la
operación completa; no se confunde ese parámetro con la desviación histórica
del TWAP. Falta integrarlo con el diario de envíos y el trabajador.

## Claves, gas y privacidad del cliente

El operador deberá ser una cuenta dedicada distinta de la wallet principal,
cuya clave/recuperación conserve el usuario fuera de ORBIS. El cliente utilizará
esa clave temporalmente en RAM, siguiendo las sesiones revocables existentes;
no guardará claves ni semillas en SQLCipher. Así el BNB de gas de esa cuenta
seguirá siendo recuperable desde su wallet aunque ORBIS se cierre. No se creará
una cuenta efímera financiada cuya clave se pierda al cerrar el proceso.

La financiación de gas será una operación separada y revisada. El ejecutor
no gasta el BNB de la wallet principal. El saldo de gas no se confunde con
capital USDT ni con beneficio. Bloquear la app revocará la firma local;
un STOP en cadena exige una transacción y su confirmación, y una transacción
ya emitida no se considera cancelada por cerrar una pantalla.

El diario de despliegue v58 conserva huellas y magnitudes, no direcciones
completas ni claves. Para CREATE desde la EOA reconstruye la dirección del
ejecutor con la wallet conectada y el nonce de creación archivado. La
comprobación del programa no concede permisos de firma ni activa un bot.
La v59 incorpora lectura actual de política y configuración/STOP; la v60 añade
permisos ERC-20 finitos y revocación. La v61 implementa el núcleo de firma
operadora en RAM; la v62 añade su diario duradero y el transporte de emisión
única. Siguen pendientes financiación de gas desde la interfaz, vínculo
LAB/PAPER, trabajador e interfaz conjunta.

## Diario de despliegue v58

El artefacto de la aplicación se fija por SHA-256
`acd44b437da4c3d9b73afe33d1b4d857e7ec47f42a2a9c1ed189e8d8bd0a3a37`.
El compilador exporta posiciones de los inmutables por sus nombres AST:
`owner`, `pool` y `poolFee`. Python compone el constructor y contrasta el
runtime completo con esas sustituciones; no ejecuta un compilador ni acepta
bytecode/calldata proporcionado por el cliente. El programa Solidity sigue
siendo v0.1; esta ampliación del artefacto añade metadatos de verificación.

La API privada está en `/real/accounts/{account_id}/dex-deployments`:
preparar y listar; `/{id}/claim`, `/hash`, `/rejected`, `/check`, `/recover`.
Preparar, conceder y recuperar exigen prueba EOA BNB de la misma cuenta y
sesión REAL; todos los accesos requieren sesión REAL y las escrituras rechazan
orígenes externos. Los resultados privados llevan `Cache-Control: no-store`.
El cliente pide el envío a la wallet sólo tras una concesión durable única.
El backend no firma ni envía el despliegue.

| Estado | Significado |
| --- | --- |
| `PREPARED` | Revisión de 120 s; ninguna solicitud a wallet concedida. |
| `WALLET_REQUESTED` | Concesión consumida; un resultado desconocido bloquea cualquier nonce de esa EOA. |
| `WALLET_REJECTED` | Rechazo explícito 4001 comunicado por el cliente, sin inferirlo de un timeout. |
| `HASH_REPORTED` | Huella del hash original archivada; aún no acredita un recibo. |
| `CODE_OBSERVED` | Programa exacto observado en la dirección CREATE y bloque finalizado; gasto desconocido sin recibo. |
| `FINALIZED` | Transacción firmada, recibo y runtime concordantes observados por RPC. |
| `REVERTED` | Creación fallida con gas observado; ningún ejecutor verificado. |

`executor_verified` indica una observación histórica del programa, no su
política actual ni permiso para operar. `automatic_execution` permanece falso
en este módulo. El alta no crea asientos de cartera ni una estrategia activa;
la configuración, integración LAB/PAPER y ejecución siguen pendientes.

Se reconstruyen los bytes firmados legacy EIP-155 o tipo 2 EIP-1559, se recupera
el remitente y se contrasta el hash. En legacy la red se acredita por `v`,
incluidos RPC que omiten `chainId`; si lo informan debe coincidir. Se comprueban
constructor exacto, nonce, valor cero, creación sin destinatario, límites de gas,
enlaces de bloque/recibo, finalización y programa/inmutables. No se admiten otros
tipos ni listas de acceso no vacías. Las comisiones que ajuste la wallet deben
cumplir `gas_limit × fee_cap ≤ max_gas_wei` revisado. El gasto observado es
`gas_used × effective_gas_price`; en tipo 2 se verifica
`effective_gas_price = min(max_fee, base_fee + priority_fee)`.
`estimated_gas_wei` conserva la estimación inicial, separada del gasto real.

La recuperación sin hash reconstruye la dirección CREATE a partir de EOA/nonce,
contrasta su código en bloque finalizado y exige que el nonce observado de la
EOA ya sea superior al utilizado. No inventa un recibo, gasto ni venta; conserva
la exclusión del nonce usado y permite otros nonces. El hash original puede
añadirse después y verificarse para completar el gasto. Nunca reenvía CREATE.

Se almacenan términos, historial, magnitudes y huellas HMAC de wallet/hash/prueba,
no EOA, dirección del ejecutor, firmas, RLP ni bytecode particularizado. La sal
permanece dentro de SQLCipher. La migración 57→58 se realiza sobre copia cifrada
con preimagen y verifica los registros; las copias posteriores repasan historial
y aritmética. Las huellas locales no autentican frente a un atacante que controle
la base y pueda recalcularlas. Los bloques/recibos/código se observan mediante el
RPC elegido: no constituyen una prueba independiente del consenso de BNB.
Una versión futura del programa deberá conservar el verificador y artefacto v1
para que los registros antiguos sigan siendo reproducibles.

## Desarrollo por tramos

1. Contrato compilable, pruebas EVM reales con tokens/router controlados y
   artefacto reproducible. Ninguna publicación o activación de UI en este tramo.
2. Plan/revisión local, despliegue y configuración confirmados por el usuario en
   wallet; verificación de bytecode, política, red y propietario. Diario cifrado,
   nonce e incertidumbre; operador/gas según el contrato anterior, sin importar
   claves de la wallet principal ni autoarrancar al reiniciar.
3. Vincular versión LAB y prueba PAPER, ejecutar DCA y LONG con fuente/indicadores
   explícitos, seguir recibos y posiciones, mostrar marcas y permitir STOP.
4. Recorrido de wallet, compras/ventas y recuperación con proveedores controlados,
   seguido de verificación humana del proveedor/mainnet con autorización concreta.

Las pruebas del primer tramo no acreditan un bot completo ni un contrato auditado.
Se conserva como pendiente la integración real hasta verificar todos los tramos.

## Construcción y verificación

El paquete aislado [contracts](../contracts/README.md) usa Solidity 0.8.38,
OpenZeppelin 5.6.1 y únicamente las bibliotecas matemáticas MIT de v4-core 1.0.2;
el protocolo de ejecución sigue siendo Uniswap v3 Router02. El compilador exige
esas condiciones y conserva hashes del código importado. Node/Ganache/Viem son
herramientas de desarrollo; la aplicación Python no incorpora una dependencia
de ejecución Node ni telemetría del SDK de MetaMask.

El primer tramo cuenta con 26 pruebas EVM correctas, sin casos omitidos, y un
artefacto idéntico tras `npm ci` y compilación en otro directorio. Se contrastan
nombres de casos y reporte XML. La lectura pública del bloque BNB 131476846
verifica el pool 500 y su TWAP de 1.800 s, con 17 llamadas de lectura y ninguna
wallet/transacción. Ese resultado puntual no prueba envío ni finalización mainnet.

## Fuentes verificadas el 03-11-2026

- [MetaMask: redes compatibles](https://docs.metamask.io/smart-accounts-kit/get-started/supported-networks/).
- [MetaMask: permisos ERC-20](https://docs.metamask.io/smart-accounts-kit/guides/advanced-permissions/use-permissions/erc20-token/).
- [Implementación de ERC20PeriodTransferEnforcer](https://github.com/MetaMask/delegation-framework/blob/main/src/enforcers/ERC20PeriodTransferEnforcer.sol).
- [Despliegues oficiales Uniswap](https://developers.uniswap.org/deployments).
- [OpenZeppelin SafeERC20](https://docs.openzeppelin.com/contracts/5.x/api/token/erc20).


## Configuración y STOP v59 — backend

`PolicyLimits` compone exactamente el struct v1, con importes enteros en unidades
USDT/WBNB de 18 decimales, gas en wei, booleanos estrictos y vencimiento máximo
de 24 horas. Admite compra/DCA, compra+venta y sólo cierre. Configurar consume
una revisión concreta; STOP conserva posición/coste e invalida renovaciones
antiguas. STOP no exige que una posición permanezca inmóvil durante la revisión.

`dex_state.py` verifica el programa completo con owner/pool/fee y consulta
política, contadores, saldos y allowances en un mismo bloque; vuelve a cotejar
su hash al terminar. Distingue nonce del bloque y pendiente. La lectura caduca
a los 30 s y una EOA operadora candidata con código se rechaza. Fallos o datos
ausentes nunca se convierten en saldo cero. No consulta TWAP/oracle para
controlar permisos, por lo que un fallo de histórico del pool no inutiliza STOP.
El transporte admite sólo seis métodos de lectura, con límites de consultas,
respuesta y tiempo; no firma ni envía. Utiliza `ORBIS_BSC_RPC_URL` de configuración
local o el nodo público predeterminado. No registra URL/errores del proveedor
ni direcciones. La representación de las observaciones oculta sus direcciones.

API privada: `/real/accounts/{account_id}/dex-controls`, para listado/preparación;
`/state` consulta el contrato, y `/{id}/claim`, `/hash`, `/rejected`, `/check` y
`/recover` completan la revisión. Preparar, conceder, consultar y recuperar
exigen prueba EOA para esa cuenta y sesión REAL. Configuración y STOP tienen
confirmaciones diferentes. El replay de una concesión consumida no vuelve a
consultar la red ni abre otra solicitud de wallet.

Las consultas RPC no mantienen el cerrojo REAL: al terminar se revalidan token,
wallet y generación de sesión. Bloquear durante la consulta impide guardar un
plan después. El diario `real_dex_control_intents` vincula despliegue, revisión,
límites, gas, nonce y observación inicial mediante magnitudes y HMAC, sin
direcciones de owner/ejecutor/operador, firmas ni hash de transacción en claro.
Comparte exclusión de nonce con despliegue, swap e ingreso MEXC. La migración
cifrada 58→59 preserva la preimagen y los despliegues, sin activar estrategias.

El cotejo reconstruye la firma EIP-155/EIP-1559 y contrasta destino, valor cero,
calldata, nonce, máximo de gas, bloque finalizado y programa. Exige el evento
exacto `PolicyConfigured` o `Stopped`, con revisión/operador cuando corresponda.
El gasto es `gas_used × effective_gas_price`, separado de la estimación. Las
copias repasan historia, política y aritmética, incluso si se ha recalculado
la huella local de un recibo alterado. La firma no demuestra por sí sola
inclusión: los recibos continúan etiquetados como observaciones RPC.
`FINALIZED` describe un recibo histórico; no acredita autoridad actual para un
trabajador. El `expires_at`/`expired` del plan es la revisión de 120 s;
`limits.expires_at` es el vencimiento independiente de la política.

Ante respuesta perdida, `/recover` requiere un bloque `finalized` fresco donde
el nonce de la EOA supere el de la solicitud. Conserva `NONCE_OBSERVED`, no un
recibo: no inventa gas ni atribuye una ejecución al mero consumo de nonce.
`effect_matches` compara los términos con el estado observado, sin afirmar
que su origen sea esa solicitud. Una sustitución de wallet puede consumir el
nonce y dejar la configuración sin aplicar; esa diferencia queda registrada.
Se mantiene excluido el nonce usado y se permiten otros. Después puede
registrarse el hash original y cotejarse el recibo. No se repite el envío.

El soporte de `finalized` se contrasta con la
[referencia oficial de BNB](https://docs.bnbchain.org/bnb-smart-chain/developers/json_rpc/bsc-api-list/).
La lectura pública del bloque 131487528 verificó su hash y una antigüedad de
1,781 s: tres lecturas, ninguna wallet ni envío. Es evidencia puntual de
compatibilidad RPC, no de operación del bot ni consenso independiente.

La interfaz conjunta y el trabajador siguen pendientes. Esta API no conecta
una clave operadora. La v60 descrita a continuación añade los permisos ERC-20
al ejecutor; los approvals manuales para SwapRouter02 son independientes.
Antes de autorizar firma se
volverá a comprobar el estado actual; un recibo antiguo nunca basta.


## Permisos ERC-20 v60 — backend

La API de controles admite `APPROVE` y `REVOKE`, para `USDT` o `WBNB`, con
`allowance_units` como entero decimal exacto. Nunca admite un spender ni
calldata del cliente: el destinatario es el token registrado y el spender
se reconstruye como ejecutor desde la EOA/nonce de creación verificados.
La transacción tiene valor BNB cero; el gas es independiente. La wallet firma
la solicitud tras una concesión durable única; ORBIS no la firma ni la envía.
Las confirmaciones son `AUTORIZAR_TOKENS_DEX` y `REVOCAR_TOKENS_DEX`.

El límite de USDT es `buyBudget − buySpent`. Para WBNB es `ownedWbnb` más
`maxWbnbAcquired − acquiredWbnb` si se permiten compras futuras; un permiso
sólo de cierre se limita a la posición propia. Así se puede autorizar la venta
de compras futuras sin incluir las unidades ajenas que ya estuvieran en la
wallet. Vender no repone presupuesto ni cuota de adquisición. Los caps son
enteros finitos: no se genera un approve ilimitado. El contrato sigue limitando
exposición, pérdidas, entradas, deslizamiento TWAP y operaciones sobre unidades
propias de forma independiente del allowance.

Un permiso positivo exige política vigente, operador no detenido y sin código,
posición cubierta y allowance anterior cero. Para cambiar un permiso no nulo
se revoca primero; no se sustituye silenciosamente. Se revalidan política,
operador, contadores, nonce y saldo de gas al conceder la solicitud. Revocar
fija cero y continúa disponible tras STOP, con política vencida o ante cambios
de posición: no añade autoridad. Un approve no contiene una revisión de
política en el protocolo ERC-20; su asociación a una revisión es una guarda
de ORBIS, y toda activación/ejecución futura volverá a leer la cadena.

Se reutiliza el diario de controles con payload `orbis.dex.control.v2`; los
registros de configuración/STOP v1 mantienen sus bytes y semántica. Se guardan
token, unidades, allowance anterior, cap y verificación EOA del operador, sin
direcciones completas ni firmas. La migración cifrada 59→60 no añade tablas ni
reinterpreta los registros previos. Una copia con datos v2 requiere esquema
60 y repasa el cap derivado de la política y sus contadores.

El cotejo exige firma/transacción exacta, gas, bloque finalizado, programa del
ejecutor y evento `Approval(owner, executor, units)` emitido por el token
correcto. Rechaza receptor, token o unidades distintos. El recibo registra
`approved_units`, no una nueva política ni una posición inventada. Describe
un permiso histórico, no el allowance disponible después de otros gastos.
La recuperación sin hash conserva el nonce consumido y el allowance observado
en un bloque finalizado; puede coincidir o no con la solicitud. No inventa
gasto ni origen de la ejecución y no vuelve a emitir la transacción.

El ABI y evento se contrastan con la
[especificación BEP-20 de BNB Chain](https://github.com/bnb-chain/BEPs/blob/master/BEPs/BEP20.md).
Se verificaron `approve(1)` y `approve(0)` de ambos tokens registrados mediante
`eth_call` en el bloque BNB **131490384**: llamadas simuladas, allowance real
antes/después idéntico, once lecturas/simulaciones y ninguna firma ni envío.
El origen fue una dirección sintética, no una wallet del usuario. BscScan
rechazó con 403 la lectura del código verificado; no se afirma haberlo leído.
Esta comprobación puntual acredita compatibilidad de llamada, no una aprobación
publicada en mainnet ni una auditoría de seguridad de los tokens.

El núcleo de firma RAM se incorpora en v61, descrito a continuación. Continúan
pendientes su diario de envíos, financiación de gas desde la interfaz, vínculo
LAB/PAPER, trabajador DCA/LONG e interfaz conjunta. `automatic_execution`
continúa falso; ningún recibo/allowance antiguo activa un bot.


## v61 — firma operadora y cotización completa, sin emisión automática

`dex_trade.py` observa el programa/política actuales, nonce confirmado y
pendiente del operador y saldo BNB. Cotiza mediante `eth_call` de
`executeSwap` desde la EOA operadora sobre el mismo bloque numerado: se
comprueban realmente los permisos ERC-20, saldos, restricciones del programa,
router, TWAP y cambios de posición. No se usa una cotización externa para
suplantar esas condiciones. La simulación no modifica saldos ni acredita una
operación realizada. El transporte continúa admitiendo únicamente lecturas.

El mínimo es el mayor de 1, `floor(salida_simulada × (10000 − slippage_bps) /
10000)` y el suelo TWAP del programa. Deslizamiento de 1–500 bps y plazo de
1–120 s, recortado por la caducidad de la política. Se coteja otra vez el hash
del bloque al terminar. El plan pierde vigencia a los 15 s y no acepta nonce
pendiente distinto del confirmado, importes no enteros o gas fuera de límites.
El recorrido de compra usa **31 lecturas/simulaciones**, dentro del límite de
32 del transporte; las direcciones permanecen sólo en RAM.

`DexOperatorSession` importa exclusivamente la clave dedicada que coincide
con la EOA operadora vigente y exige confirmar su recuperación externa. Nunca
genera una clave efímera que pueda perder los fondos de gas al cerrar la app.
La concesión está vinculada a propietario, ejecutor, operador, revisión,
límites exactos y generación de bóveda. Caduca en un máximo de 900 s o antes
si caduca la política. Cambiar la política no renueva la firma silenciosamente.

La clave vive en un buffer RAM revocable, sin SQLCipher, archivos ni logs.
Bloquear, proteger la vista, reautenticar o agotar el plazo retira la firma.
Se limpia el buffer controlado por ORBIS; Python y las bibliotecas de firma
pueden crear copias temporales, por lo que no se promete borrado criptográfico
de toda la memoria del proceso. Un temporizador antiguo que ya esté ejecutándose
no puede cancelar una conexión posterior; un fallo al iniciar el temporizador
retira también la clave.

Cada firma reserva conservadoramente `gas_limit × gas_price` de un presupuesto
de sesión finito, máximo 0,1 BNB. Las compras/ventas deben dejar además
`100000 × policy.maxGasPrice` para STOP, tanto dentro del presupuesto local
como en el saldo observado del operador. Esta reserva no es una comisión
pagada ni bloquea fondos en cadena. No se devuelve presupuesto al ver un precio
de gas inferior sin un recibo cotejado. STOP tiene su propio plan sin cotización
de swap: permite parar aunque una intervención externa deje la posición sin
cobertura. Firmarlo retira la firma local, pero sólo el envío/recibo posterior
puede demostrar STOP en cadena. Los controles del propietario permanecen aparte.

La firma compone internamente sólo `executeSwap` o `stop`, destino el ejecutor
exacto, red 56 y valor BNB cero. No acepta diccionarios de transacción, firmas
genéricas, transfers, destinatarios o calldata del cliente. Usa transacciones
legacy protegidas con chain ID mediante
[`eth-account Account.sign_transaction`](https://eth-account.readthedocs.io/en/latest/eth_account.html#eth_account.account.Account.sign_transaction)
y [EIP-155](https://eips.ethereum.org/EIPS/eip-155). No firma ni transmite la
clave propietaria.

El nonce se consume una vez en memoria, conservándose también entre
reconexiones dentro del proceso; una firma que aún no se haya difundido no
permite firmar otra variante con ese nonce. **Esta exclusión no sobrevive a un
reinicio**. Por eso este núcleo no tiene ruta de envío ni trabajador activo:
el próximo tramo debe incorporar un diario duradero para el nonce del operador,
hash calculado antes de emitir, recibos/reconstrucción y resultados inciertos.
Las firmas y transacciones firmadas son transitorias y no se serializan a logs.
El estado sigue anunciando `automatic_execution: false`.

Las pruebas ejecutan firmas reales con claves Ganache ficticias: compra,
venta parcial, cierre y STOP. La salida vuelve al propietario y las unidades
WBNB previas permanecen ajenas al bot. Incluyen gas/reserva, impacto de precio,
revocación ERC-20, movimiento externo de posición, cambio de política, sesión,
caducidad, nonce pendiente, reorganización de bloque y errores ABI. No hay
envíos mainnet ni uso de wallets del usuario. Bóveda SQLCipher y contrato
conservan esquema 60 y artefacto v0.1, respectivamente. La v62 siguiente
añade la durabilidad que faltaba, con migración de esquema 60→61.


## v62 — diario duradero y emisión única del operador

La bóveda incorpora `real_dex_operator_grants` y `real_dex_operator_intents`.
Una concesión vincula cuenta, despliegue verificado, operador, revisión y límites
exactos de la política. Tiene presupuesto finito de gas, reserva STOP e historial;
la unicidad despliegue/revisión impide reponer el presupuesto reconectando la
clave o usando otro ID. Las intenciones conservan receta, observación, identidad
de decisión, nonce EOA y nonce del contrato. Se guardan magnitudes y HMAC de
wallet/operador/hash; no direcciones completas, claves, RLP o firmas.

La migración cifrada 60→61 preserva controles/despliegues anteriores y verifica
las tablas nuevas en copias y restauraciones. Los registros del operador no
pueden ocultarse en un archivo declarado v60. La verificación repasa historial,
aritmética, presupuesto, referencias y nonces duplicados. Las huellas locales
no son una prueba de consenso ni autentican frente a un atacante que controla
la base y puede recalcularlas.

| Estado | Significado |
| --- | --- |
| `RESERVED` | Receta y gas reservados de forma durable; ninguna concesión de POST. |
| `SIGNED` | Firma exacta contrastada y HMAC del hash original archivado; los bytes sólo viven en una continuación RAM. |
| `BROADCAST_ATTEMPTED` | Concesión única consumida y confirmada en disco antes de llamar al transporte; no acredita inclusión ni éxito. |
| `NONCE_OBSERVED` | Nonce original consumido en bloque finalizado; sin recibo no se atribuyen ejecución, comisión ni P/L. |
| `FINALIZED` | Firma, programa, eventos, movimientos de tokens del propietario y gas cotejados. |
| `REVERTED` | Transacción original fallida; gas real, ningún fill o P/L inventado. |
| `ABANDONED_UNSENT` | Intención descartada antes de cualquier concesión de emisión; se liberan nonce y reserva, conservando su historial. |

Las reservas se insertan con `BEGIN IMMEDIATE`. Comparten exclusión de nonce
con las acciones de esa EOA en wallet→MEXC, swap, despliegue y controles.
Dos procesos con motores SQLCipher separados no pueden reservar el mismo
nonce. Una decisión ya registrada no vuelve a convertirse en una entrada.
Un resultado pendiente impide nuevas emisiones de esa EOA; tras observar su
nonce consumido pueden usarse otros nonces, pero una concesión con resultado
económico sin resolver sigue bloqueando entradas y permite únicamente STOP.

La continuación RAM no puede copiarse, serializarse o reconstruirse leyendo
la base. Está ligada a la instancia/generación de bóveda y revisión de la
firma local. Bloquear, retirar la clave, reautenticar o detener la concesión
antes de la marca durable impide emitir la preparación. Incluso si la bóveda
sigue abierta para otro servicio, la revocación local del operador invalida
su continuación. STOP firmado retira su clave y conserva exclusivamente la
continuación de esa parada hasta que se consuma o se revoque.

La marca durable ordena la emisión respecto a STOP local. Después de consumirla,
el POST ya está autorizado y puede terminar aunque se solicite una parada;
la llamada de red no mantiene cerrojos de REAL, SQLCipher o firma. Una parada
local no puede prometer cancelar una transacción en vuelo. STOP confirmado en
cadena invalida la revisión del programa, sin vender la posición.

`RESERVED` y `SIGNED` pueden descartarse mientras no exista concesión de POST.
Es posible porque los bytes firmados no tienen salida por API, exportación o
logs y el único camino de emisión exige aquella marca durable. Cambiar el
estado a `ABANDONED_UNSENT` invalida también una continuación vieja que aún
esté en RAM; la preparación nueva conserva su propio control de nonce.
Nunca se descarta como «no enviado» un `BROADCAST_ATTEMPTED`, aunque el RPC
haya respondido con timeout. Ese caso necesita cotejo del original.

`BnbOperatorSender` contrasta firma legacy protegida, destino, calldata, nonce,
gas, precio, red 56 y valor nativo cero con el plan exacto. Efectúa una sola
llamada `eth_sendRawTransaction`, sin reintentos ni redirecciones, y comprueba
el hash devuelto. Red incorrecta, timeout o hash inesperado no conceden otro
POST ni se convierten en un fill. Los errores remotos no exponen URL o RLP.
`BnbOperatorReadClient` sólo añade lectura de transacción/recibo al lector
público; recolecta bloque, finalización, pool y programa y vuelve a cotejar
el hash del bloque. Un recibo pendiente sigue siendo pendiente.
Referencia: [JSON-RPC de Ethereum](https://ethereum.org/developers/docs/apis/json-rpc/),
[EIP-1474](https://eips.ethereum.org/EIPS/eip-1474).

El verificador reconstruye el original y exige el evento `SwapExecuted` o
`Stopped` de ese ejecutor. Para swaps coteja los movimientos exactos del
propietario y el cambio neto cero del ejecutor; no presupone que el router y
el pool sean la misma cuenta. Contrasta revisión/nonce, dirección, importe,
mínimo, posición, coste, plazo y gasto nativo. El ABI de evento es exacto,
sin prefijos inválidos ni bytes sobrantes.

La venta parcial asigna `floor(coste_anterior × unidades_vendidas /
unidades_anteriores)` y el cierre completo asigna todo el resto. El P/L USDT
es salida menos coste asignado. Gas BNB se mantiene separado. Cada recibo
cotejado sustituye su reserva máxima de gas por el gasto real; una lectura
de saldo o un nonce consumido no lo hace. Ante respuesta perdida se conserva
el gas máximo reservado y no se inventa resultado. STOP tiene su recibo propio
y no cuenta como venta. Las pruebas incluyen un caso de coste 17, compra de
25 unidades, venta de 7 con coste 4 y cierre de 18 con el resto 13.

Este corte no añade ruta de activación del bot, trabajador ni UI de bot DEX:
`automatic_execution` permanece falso. Faltan el vínculo LAB/PAPER verificable,
financiación de gas desde la interfaz, trabajador DCA/LONG y recorrido completo
del usuario. Los envíos de prueba se hacen sólo a Ganache, con comprobación de
identidad del fixture; no hay despliegue o piloto mainnet ni fondos del usuario.

## Preparación LAB/PAPER v63

`Patrimonio registrado → Wallet EVM → Bot DEX · Preparar` permite consultar
sesiones LONG y DCA cifradas, repetir su backtest y revisar los límites REAL.
El catálogo público MEXC incluye BNB/USDT. La señal pertenece explícitamente a
MEXC BNB/USDT; la ejecución prevista pertenece a USDT/WBNB en Uniswap v3 BNB.
No se aplica una estrategia BTC o ETH a otro activo por similitud de velas.

La preparación exige una sesión PAPER activa con observación íntegra de la
última vela cerrada, la versión y dataset originales, hashes concordantes y
backtest reproducible. Las reglas anidadas y de distintas temporalidades se
interpretan mediante el mismo verificador que los bots Spot existentes.
La lista señala las sesiones que requieren revisión, sin declararlas aptas.

Los límites de entrada, presupuesto, exposición y pérdida se introducen de
nuevo en USDT; la cuota acumulada se introduce en WBNB. Cada cantidad se
convierte exactamente a enteros de 18 decimales, rechazando fracciones menores,
valores no finitos, JSON numérico y entrada superior a presupuesto/exposición.
No se importa el capital o la posición ficticia. Para DCA el límite mensual
efectivo es el menor entre el REAL elegido y el de las reglas probadas; se
conservan las reglas y no se concede permiso de venta.

API privada: `GET /real/accounts/{account_id}/dex-bots/sources` y
`POST /real/accounts/{account_id}/dex-bots/preview`. Requiere sesión REAL,
cuenta existente y mismo origen para el POST. Verificar un backtest no mantiene
el cerrojo REAL ni el de la bóveda: bloquear continúa disponible y revoca la
respuesta pendiente. Respuestas `no-store`, revisión válida 60 s, huella de
contenido, sin persistencia de wallet, claves o presupuesto ficticio.

**Es preparación, no activación**: `automatic_execution` permanece falso.
La pantalla no contiene un botón que simule arrancar el bot. Despliegue,
política en cadena, permisos de tokens, gas del operador y trabajador todavía
necesitan integrarse en el flujo. El ejecutor no se ha publicado en mainnet.

Disponibilidad de BNBUSDT comprobada el 04-11-2026 mediante `GET
/api/v3/exchangeInfo?symbol=BNBUSDT`: base BNB, cotización USDT, estado `1`,
Spot permitido. Sólo se realizó esa lectura pública, sin claves.
Referencia: [API oficial MEXC, información de mercados](https://mexcdevelop.github.io/apidocs/spot_v3_en/#exchange-information).

## Vínculo duradero v64

«Vincular ejecutor» guarda la preparación con un despliegue ya verificado y
su política observada en un bloque finalizado. Exige prueba EOA BNB de esa
cuenta REAL y origen LAB/PAPER vigente. «Guardar vínculo» no despliega, configura, financia gas, conecta claves ni
arranca un trabajador. El asistente «Ejecutor · BNB» incorporado en v65 cubre
despliegue/configuración y permisos del propietario; gas, conexión operadora y
activación causal siguen pendientes.

La bóveda v62 incorpora `real_dex_bot_bindings`. El vínculo contiene definición,
identificadores/hashes de versión, dataset y backtest, velas iniciales/observadas,
resultado histórico y JSON original de la observación PAPER. Se pueden repetir
el backtest y sus condiciones desde esa copia aun sin las bases LAB/PAPER.
Todo ese contenido está en `origin` y sigue siendo teoría/PAPER; no se convierte
en saldo, posición, beneficio ni fill REAL. No guarda wallet, operador, ejecutor,
hash real, clave o firma en claro.

La concesión de gas y el vínculo se confirman en la misma transacción
`BEGIN IMMEDIATE`; un fallo posterior a crear la concesión deshace ambos.
Cada política admite una sola concesión; un ejecutor admite un solo vínculo
sin retirar. Para el primer vínculo se exige una política nueva sin actividad
ni posición propia, evitando adoptar operaciones sin procedencia. No permite
cambiar silenciosamente de estrategia con una posición previa: el rearme y la
continuidad de posiciones necesitan su flujo propio posterior.

Los límites en cadena no pueden superar entrada, presupuesto, exposición,
pérdida realizada y cuota WBNB revisados. Se muestran los límites efectivos.
LONG necesita compra/venta; DCA exige sólo compra y conserva su límite mensual
REAL efectivo. «Pérdida realizada» es acumulada y excluye gas y resultados no
realizados: no garantiza un stop de mercado.

La API privada usa `/real/accounts/{account_id}/dex-bots/bindings`: guardar,
listar, `/{id}` y `/{id}/stop`. Un ID ya confirmado se recupera sólo con su
misma wallet, origen y magnitudes canónicas; no consulta RPC ni LAB/PAPER, no
renueva presupuesto y no cambia la foto de origen. La interfaz conserva el
mismo intento ante respuesta incierta. Los errores 4xx de la primera revisión
permiten corregir los campos. La consulta del historial funciona con sesión
REAL aunque PAPER y la wallet estén cerrados.

`READY` significa **procedencia guardada**, nunca ejecución activa. Las reservas
BUY/SELL de una concesión vinculada se rechazan en el propio diario hasta
implementar la activación con decisiones causales verificadas; no basta un
`decision_ref` arbitrario. La reserva de STOP queda permitida y se verifica en
EVM. «Retirar vínculo» detiene la concesión local atómicamente y conserva el
historial; no envía STOP en cadena ni liquida activos. Un envío que ya obtuvo
su concesión durable no se considera cancelado retrospectivamente.

La migración 61→62 conserva bytes de los diarios anteriores. Copias y
restauraciones rechazan vínculos ocultos bajo v61 y repiten los backtests;
recalcular los hashes de un beneficio inventado no lo hace válido. También
se comprueban exclusión entre instancias de bóveda y recuperación de respuesta
perdida. No acredita contrato auditado ni envío humano mainnet.


## Preparación desde wallet v65

En «Ejecutor · BNB», prepara un despliegue, revisa su gas máximo y confirma en
la wallet. El servidor comprueba el recibo finalizado y el programa exacto.
Después de comprobar la estrategia y sus límites REAL, introduce sólo la
**dirección pública** de una cuenta operadora separada y prepara la política.
Vigencia, espera, entradas, TWAP, desviación y gas son configurables; las
magnitudes monetarias proceden de la revisión. DCA concede sólo compras.

Autorizar USDT/WBNB usa cuotas finitas calculadas desde la política y los
contadores actuales. Un permiso previo se revoca antes de sustituirlo.
«Preparar STOP» necesita confirmación en wallet; revocar tokens sigue disponible
tras la parada. Los activos siguen en la wallet y configurar no inicia el bot.

Las operaciones inciertas se comprueban en su historial. Puedes pegar su hash
original para verificar firma, llamada, bloque finalizado y gasto real. Si no
hay hash, observar el contrato esperado o el nonce consumido no inventa un
recibo. La ausencia de contrato tras un CREATE incierto todavía no basta para
liberar ese intento: no lo repitas a ciegas. La evidencia de cancelación por
nonce consumido sin código queda pendiente de integrar en ese diario.
