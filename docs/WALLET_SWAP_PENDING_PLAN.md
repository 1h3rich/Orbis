# Swap manual: wallet sin respuesta

Estado: **implementado y cargado en v84**. Evidencia definitiva en
`~/.local/state/orbis/verification/v84-wallet-integrated-r1/verification.json`.
La espera visual vence a los 30 s; la solicitud viva sigue impidiendo otro envío
hasta terminar. Se permiten consultas y contabilidad de recibos ya verificados.
Cambiar de wallet, bloquear o montar de nuevo no concede autoridad a una respuesta
antigua. Los apartados siguientes conservan diagnóstico y criterios del desarrollo.
La concesión durable consumida tampoco se repite al recargar JavaScript.

## Evidencia

`~/.local/state/orbis/verification/v84-wallet-pending-probe/held-flow.py` utiliza
la API y bóveda propias de la prueba manual en 127.0.0.1:8803. El proveedor EVM
controlado mantiene pendiente la respuesta de la firma/envío. No hay claves,
servicios financieros ni fondos del usuario. `result.json` conserva:

- Un envío solicitado y ningún error JavaScript.
- Historial actualizado con el original `WALLET_REQUESTED`.
- Entrada de hash y «Revisar resultado» deshabilitados.
- «Confirma en la wallet · no repitas» sin límite de espera de interfaz.

El caso rojo exige que la revisión de sólo lectura esté disponible y falla en
esa condición exacta. El servidor aislado terminó al acabar la prueba.

## Causa y alcance

`wallet-swap.js::send` espera directamente la promesa `eth_sendTransaction`.
`busy` y `walletPending` permanecen activos hasta su resolución, y las mismas
banderas bloquean tanto los envíos como la consulta/revisión de resultados.
Los cuatro recorridos nuevos v83 parten de una intención persistida o una
respuesta HTTP perdida; no cubren una promesa viva del proveedor que no termina.

## Criterios implementados y verificados

1. Separar espera de interfaz, solicitud financiera pendiente y lectura del
   historial/recibo. Vencer una espera de interfaz no cancela la firma ni permite
   otro envío mientras el proveedor pueda completar el primero.
2. Preservar la barrera de solicitud pendiente entre clear, remount, cambio de
   cuenta y cambio de proveedor. La concesión durable consumida nunca se repite.
3. Permitir introducir hash y revisar/recuperar el resultado en el contexto
   verificado actual. No reenviar, firmar, restaurar operadora ni activar bots.
4. Un resultado tardío no escribe con credenciales o identidad nuevas, no
   sobrescribe un recibo ya resuelto y no desbloquea otra operación en curso.
   Una reapertura no supone permiso para repetir.
5. Conservar errores 4001, rechazo desconocido, pérdida del POST de hash y
   consulta posterior. Una respuesta tardía inválida no inventa rechazo o fill.
6. Probar promesa retenida, timeout visual, revisión por lectura, respuesta
   tardía, lock, remount y cambio de wallet. Contar solicitudes financieras y
   registros SQL; validar compras, ventas, gas, reapertura y privacidad.

Reutilizar la separación de promesa viva/timeout ya verificada en
`wallet-dex-funding-cancellation.js`, adaptándola al diario manual. No copiar su
permiso de cancelación: este corte sólo recupera la observación. Antes de cargar,
repetir los recorridos manuales antiguos y los cuatro de resolución v83.

## Ensayo aislado anterior a la integración

Implementación preparada fuera del árbol congelado en
`~/.local/state/orbis/verification/v84-wallet-pending-dev/`. No está aplicada ni
cargada en ORBIS. El navegador recibe esos bytes únicamente mediante una ruta
local controlada de pruebas; API y bóveda son propias de cada caso.

Pasan cuatro casos de promesa retenida: timeout y recibo recuperado antes de
respuesta tardía, clear, remount con lectura nueva retenida y rechazo 4001 tardío.
Se verifica una sola solicitud/concesión, ausencia de escritura en contexto
antiguo, preservación del busy de la lectura nueva y ningún error JS. También
pasan los dos recorridos manuales anteriores de compra/venta, gas y enlace de
asientos existentes con esos bytes. También pasan los dos recorridos con recibos de EVM
propia, compra y cancelación: revisión, pérdida HTTP, lectura de recuperación,
contabilidad y reapertura. Se añaden cambio de wallet y bloqueo/desbloqueo antes de la respuesta: ninguno
escribe el hash antiguo en el contexto nuevo. Son diez recorridos distintos
aprobados en total; informe `v84-wallet-pending-dev/verification.json`. Esto no acredita todavía integración,
publicación o recuperación automática tras un reinicio de JavaScript.

## Integración definitiva

`tests/browser_wallet_swap_common.py` contiene el proveedor de pruebas importable.
`tests/browser_wallet_pending_flow.py` implementa los seis escenarios permanentes,
registrados en `scripts/verify_orbis.py`. Los dos recorridos manuales antiguos ya
usan ese proveedor y guardan capturas en el directorio de verificación. Las pruebas
finales utilizan bytes servidos por la aplicación, sin sustituir sus scripts.
El ensayo de `v84-wallet-pending-dev` sigue siendo evidencia histórica, no una
segunda ejecución de la suite final; `promotion.json` enlaza la versión cargada.
