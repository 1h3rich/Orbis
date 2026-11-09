# Proteger el legado desde Ajustes

Estado: v95 verificada y cargada. No se ha ejecutado sobre las bases del propietario.
Objetivo completo: proteger los datos existentes mediante un flujo gráfico con
contraseña elegida por el propietario, copia recuperable y verificación real.
No se cifran bases del propietario durante el desarrollo: sólo fixtures propios.

## Decisión de ejecución

Los migradores offline existentes se conservan: verifican el origen, generan una
copia SQLCipher, obtienen exclusividad, vuelven a comprobar identidad/contenido y
sustituyen el archivo de forma atómica. No se eliminan sus barreras para poder
invocarlos mientras el servidor está usando la base.

El lanzador local pausará el servidor HTTP tras entregar la aceptación del trabajo.
Esperará al drenaje de peticiones y trabajadores y a la liberación de las leases.
Ejecutará entonces los migradores y volverá a abrir el servidor en el mismo puerto.
El socket del lanzador conservará la reserva del puerto durante la pausa. Un
proceso iniciado directamente con uvicorn no anunciará esta capacidad disponible.

La primera integración comprende LAB, automatizaciones DCA, Spot PAPER y GRVT
PAPER. La bóveda REAL queda fuera. La base PAPER manual `orbis.db` conserva su
migrador offline; su integración gráfica es otro tramo y sigue pendiente. No se
presentará este alcance como cifrado de todos los archivos del producto.

Se reutiliza el mismo proceso del lanzador para no transportar claves mediante
argumentos, entorno, ficheros o un proceso auxiliar. La contraseña del trabajo
vive sólo en memoria, se retira al terminar y no reconecta almacenes ni bots.
Reabrir el servicio no recarga código: es una pausa para proteger datos.

## Contrato y barreras

- Trabajo local con ID, lista cerrada de almacenes y contraseña repetida. La
  confirmación es el botón «Proteger» del formulario; no pide claves de wallet.
- Estado y resultados sin contraseñas, rutas arbitrarias ni acceso a REAL.
  Errores de validación redactados: nunca se devuelve el cuerpo de la petición.
- Antes de aceptar: código actual sin cambios pendientes, capacidad del lanzador,
  REAL cerrado/sin operativa, ningún
  almacén con sesión de desbloqueo y ningún seguimiento PAPER activo legible.
  Los saldos y posiciones abiertas permanecen; la migración no vende ni cierra.
- Se fija identidad de los archivos seleccionados. Mientras se pausa, las demás
  rutas de datos quedan bloqueadas y sólo siguen disponibles UI/estado del trabajo.
  Tras drenar el servidor se repite la comprobación antes de cada migración.
- Se permiten únicamente archivos planos conocidos. Se rechazan duplicados,
  enlaces, coincidencias con REAL/PAPER manual y rutas alias entre almacenes.
- Idempotencia del ID con huella autenticada efímera del contenido, sin guardar
  la contraseña en el resultado. Una respuesta perdida se consulta por el mismo
  ID; no se vuelve a enviar la contraseña automáticamente.
- Cada archivo se migra atómicamente. Un lote no es una transacción entre cuatro
  bases: si uno falla, se informa del éxito parcial y se conservan copias/estados
  de cada archivo. No se deshacen automáticamente migraciones verificadas.
- Tras el trabajo se vuelve a servir la UI incluso si una migración falla. Los
  almacenes cifrados empiezan bloqueados. El usuario puede revisar el resultado,
  desbloquear con su clave y reactivar PAPER expresamente si corresponde.

## Verificación necesaria

1. Copias y libros idénticos en fixtures de los cuatro almacenes, reapertura y
   rechazo de clave incorrecta; ningún nuevo asiento, señal u orden.
2. Leases de otro proceso, cambio de archivo y fallo entre pasos: original intacto
   o estado parcial identificado, copias verificadas y servicio recuperado.
3. ID repetido con igual/distinto contenido, credenciales ausentes de JSON/logs/
   almacenamiento web/argv, validación/redacción y fronteras de origen/host.
4. Lanzador real y navegador: formulario → pausa → cifrado → mismo puerto →
   resultado → desbloqueo → datos originales y archivo de derivados utilizable.
5. Rechazo con REAL abierto, PAPER activo, uvicorn sin lanzador y almacenes no
   elegibles. Regresión de arranque/apagado, trabajadores y rutas existentes.

## Comprobaciones de desarrollo

- Migradores reales de los cuatro almacenes sobre fixtures propios: tablas/libros
  idénticos después del cifrado, copias verificadas y clave incorrecta rechazada.
- Lanzador real: pausa, liberación de leases, misma dirección/puerto, datos
  protegidos bloqueados y recuperación mediante los cuatro formularios existentes.
- Se rechaza REAL abierto sin cerrarlo automáticamente; un seguimiento PAPER
  sigue activo si el usuario no lo detiene y bloquea la petición.
- Navegador con dos procesos: migración desde Ajustes, navegación que limpia
  contraseñas sin enviarlas, foco del teclado conservado al actualizar estado,
  exportación exacta del archivo perpetuo y recuperación tras proceso nuevo.
- La primera prueba de reapertura inyectaba el ticket después de montar el
  componente. Se corrige el montaje para conservarlo antes de entrar en Ajustes;
  no se relajan los controles del producto. El caso completo pasa después.
- La regresión completa aprueba **4.129 casos / 364 archivos**, sin fallos,
  errores ni omitidas, en ocho procesos aislados sobre las mismas fuentes.
  Los 49 recorridos de navegador también aprueban sobre esas mismas fuentes.
  La revisión visual detecta después un aviso antiguo del resumen de privacidad;
  se retira, se actualiza la caché y se repiten los 19 casos de interfaz y dos
  recorridos completos de creación/reapertura. Se conservan las otras 4.110
  identidades y 47 recorridos, sin afirmar otra tanda completa de todos ellos.
  El servidor local carga los módulos correctos sin errores JS; proceso 85605,
  sólo loopback y 46 rutas REAL rechazadas sin token. El ajuste de texto se sirve
  desde el mismo proceso, sin otro reinicio. Evidencia:
  `~/.local/state/orbis/verification/v95-protection-r2/verification.json`.
  El PAPER manual continúa con migración
  offline; no se afirma protección gráfica de todos los archivos del producto.
