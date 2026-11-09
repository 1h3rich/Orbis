# PAPER manual desde Ajustes

v111 verificado y cargado. Las pruebas de creación y migración usan archivos
propios; los archivos del propietario conservan su contenido y formato.

Abre ORBIS con `scripts/run_orbis_local.py` y entra en **Ajustes → Privacidad y
seguridad → PAPER manual**. Según el archivo aparece una de estas acciones:

- **Crear PAPER cifrado**: introduce y repite una contraseña de al menos doce
  caracteres. Se prepara y verifica el esquema cifrado antes de publicar el
  archivo; una creación fallida no publica una SQLite en claro.
- **Desbloquear**: abre el archivo cifrado durante quince minutos. La clave sólo
  vive en memoria. Bloquear o una autenticación posterior cancela un desbloqueo
  pendiente. Las peticiones ya iniciadas pueden terminar.
- **Proteger PAPER**: para un legado plano. El servidor drena peticiones y
  trabajadores, verifica una copia cifrada, coteja el contenido lógico y vuelve
  al mismo puerto con PAPER bloqueado. Desbloquéalo para consultar el historial.
- **Crear copia cifrada**: copia verificada con clave independiente. No cambia
  el formato del archivo activo y requiere desbloquearlo si ya está cifrado.

La interfaz elimina los campos de contraseña al enviar o abandonar Ajustes.
Si se pierde la respuesta de protección, consulta el mismo ID de trabajo sin
repetirlo. El ID vive en `sessionStorage`; la contraseña no. Tras cerrar el
servidor se conserva el archivo y su copia, pero los trabajos y claves en RAM
desaparecen. Reabrir no reactiva bots ni reconecta credenciales.

LAB, DCA, Spot y Perpetuos PAPER mantienen su panel **Proteger legado**. Ambos
paneles comparten una exclusión de mantenimiento; el trabajo manual sólo toca
su archivo. REAL, almacenes ajenos, alias y escritores concurrentes se rechazan.
Es necesario detener seguimientos y bloquear las sesiones indicadas antes de
proteger. La protección no vende activos ni mueve fondos.

Conserva las claves fuera de ORBIS: no existe recuperación de contraseña. Las
copias antiguas `.bak`, exportaciones y bloques previos de disco no se borran
automáticamente. El cifrado protege el archivo en reposo; no protege una app
abierta frente a un sistema operativo comprometido.

El modo opcional `--terminal-paper-unlock` conserva el acceso oculto por consola.
Los comandos `scripts.backup_legacy_paper` y `scripts.encrypt_legacy_paper`
siguen disponibles. Un backend iniciado directamente sin el lanzador permite
desbloquear un archivo compatible, pero no anuncia protección con pausa si
carece del controlador de reinicio.
