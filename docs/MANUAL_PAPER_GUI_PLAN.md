# PAPER manual: protección y reentrada desde Ajustes

Estado: v111 verificado y cargado. No se cifra ni se abre la base del propietario
durante las pruebas ni durante la carga.

La implementación pasa 4.812 casos / 404 archivos y 17 recorridos de navegador.
Tras ajustar únicamente dos componentes JS y su recorrido, se conservan iguales
backend y tests unitarios y se repiten 19 casos de recursos, cinco recorridos
afectados y cuatro regresiones de bloqueo/copias. Se verifican lock durante KDF,
creación disputada y respuesta perdida. La carga autorizada y el smoke pasivo
comprueban recursos, panel, 51 rechazos privados y archivos del propietario sin
cambios; REAL permanece bloqueado. Evidencia:
`~/.local/state/orbis/verification/v111-manual-paper-gui-r2/`.

## Resultado requerido

El usuario debe poder crear PAPER manual cifrado, volver a abrirlo tras un
reinicio y proteger un legado plano desde Ajustes. Debe conservar operaciones,
cantidades, sesiones, precios y preferencias, sin teclear comandos ni entregar
la contraseña al agente. Una copia verificada y la comprobación de contenido
forman parte de la operación, no de una reparación posterior opcional.

## Decisión de arquitectura

El motor SQLAlchemy actual vive en variables importadas por varios módulos.
Cambiar solamente el archivo dejaría conexiones abiertas sobre el inodo antiguo.
Se conservará un objeto engine estable con un creator gobernado por un runtime
local. El runtime fija explícitamente su modo (plano, cifrado o cerrado),
comprueba el acceso al obtener conexiones y no elige una caída automática a
SQLite plano si falla SQLCipher.

La protección manual se ejecutará sólo después de que Uvicorn drene peticiones
y trabajadores. Se dispondrán conexiones y se liberará la lease compartida del
PAPER manual antes de usar la migración offline existente. Al terminar se
reacquirirá la lease y se reanudará el mismo puerto. El PAPER recién cifrado
queda bloqueado y su contraseña de migración se descarta; no restaura autoridad.

El trabajo manual tendrá contrato y estado propios. Comparte con la protección
de los otros cuatro almacenes una exclusión de mantenimiento, evitando dos
trabajos simultáneos o dobles pausas. No cambia su contrato de destinos ni
incluye REAL. Se rechazan alias de otros almacenes, rutas no locales, enlaces,
archivos cambiados, escritor externo o falta de exclusividad.

El arranque desde escritorio debe poder servir UI con un PAPER cifrado cerrado
o sin inicializar; no crea SQLite plano ni intenta leerlo sin clave. La
creación/desbloqueo desde UI inicializa el esquema sólo después de verificar
la contraseña. Se conservará una opción explícita de desbloqueo por terminal
para los usos existentes, además del descriptor heredado sin secretos en argv.

Bloquear gana a una autenticación pendiente. Un desbloqueo anterior no puede
deshacer una parada ni una solicitud de autenticación posterior. La clave y
la intención de sesión sólo viven en RAM. Se conservan las reglas declaradas
para peticiones ya iniciadas; nuevas operaciones con PAPER cerrado se rechazan.

## Verificación requerida

- Motor real SQLite/SQLCipher: crear, comprar/vender, cerrar y desbloquear;
  recuperar cantidades exactas y mantener las claves foráneas activas.
- Migración real y copia: igualdad lógica antes/después, sin archivo activo
  plano ni temporal residual al completar; fallos y reemplazos rechazados.
- Lease/proceso real: no migrar si otro proceso usa PAPER; drenar HTTP y
  reanudar un único listener en el mismo puerto.
- Exclusión con LAB/DCA/Spot/GRVT, REAL abierto/operando y seguimientos activos.
- Claves incorrectas, lock y nueva intención concurrentes, expiración,
  reentrada sin arranque automático, formatos desconocidos y creación disputada.
- API sólo local/mismo origen, cuerpos limitados y errores sin contraseñas.
- Navegador desde creación/migración, sesión cerrada, pérdida de respuesta,
  recuperación por ID y reapertura en otro proceso.
- Regresión del arranque, PAPER, LAB, bots y otros cuatro almacenes. Los
  almacenes del propietario permanecen fuera de las pruebas.

Este trabajo cubre un pendiente concreto de fase 1. Conserva el alcance del
objetivo total y los demás pendientes de `ACEPTACION_FUNCIONAL.md`.
