# Bot GRVT MAINNET — activación, límites y trazabilidad

Estado 03-11-2026: coordinador, API e interfaz implementados sobre la bóveda
v57. Patrimonio → «Operar GRVT · MAINNET» → «Bot · MAINNET» permite elegir una
sesión AUTO PAPER, revisar límites y autorizar un bot real LONG o SHORT. Las
pruebas utilizan LAB, PAPER, firmas y SQLCipher reales, y HTTP externo controlado;
no son un piloto del proveedor con fondos reales. La verificación y actualización
del proceso local se registran por separado en PROGRESO_V0.2.md.

## Decisiones

Integración v91 cargada y verificada: las revisiones nuevas requieren «Reserva».
Guarda un backtest y una validación fuera de muestra con datos públicos GRVT de
la misma versión y temporalidad; activa GRVT PAPER desde ese backtest y espera
una observación. En «Bot · MAINNET» consulta «Sesiones PAPER», elige sesión y
reserva, y revisa los límites. Las sesiones antiguas sin backtest fijado no se
completan eligiendo uno implícitamente: utiliza el recorrido LAB → PAPER.

La revisión repite backtest, reserva y señales observadas. Su prefijo PAPER
puede conservarse mientras llegan velas posteriores; no puede alterarse. La
activación vuelve a comprobar los originales y archiva la referencia con la
concesión v3. Las concesiones v1/v2 permanecen legibles y se identifican como
legado sin reserva; abrirlas no les añade esa comprobación ni firma en memoria.

El histórico LAB actual evalúa reglas de precio, sin simular el margen ni la
financiación de derivados. La revisión lo muestra como «Sin margen/funding»;
no se presenta su rentabilidad como equivalente a la ejecución apalancada.
El [protocolo de investigación](VALIDACION_OOS_PLAN.md) fija los campos exactos,
versiones y límites de esta evidencia. La versión cargada se consulta en el
índice de verificación, no se deduce de esta guía de implementación.

La definición de estrategia se fija al conceder: sesión AUTO_PAPER activa,
versión LAB, dataset público GRVT verificado, hash, motor y temporalidad. La
entrada sólo acepta una OPEN posterior a la concesión y al recibo real del
ciclo anterior, y con antigüedad máxima
de 20 s. Se reproduce el histórico y las observaciones bajo transacción PAPER,
y se cotejan acción, posición y asiento. La reproducción compartida está en
`grvt_paper_binding.py`; no autentica ni autoriza una red. TESTNET conserva su
API y sus IDs anteriores. MAINNET usa otra derivación de ID y reserva propia.

La concesión guardada es evidencia, **no autoridad**. Cada envío exige un
callback de permiso RAM vigente, generación de bóveda, clave dedicada y
preflight MAINNET actuales. Reiniciar no restaura firma ni ejecuta el historial.
El coordinador revoca en RAM antes de esperar al almacenamiento al detener
o bloquear; comprueba caducidad monotónica y de reloj antes de cada operación. Guardar un final por sí solo
no interrumpe una petición que ya se está enviando al proveedor.

## Límites y fórmulas

`MainnetBotPolicy` recoge subcuenta, topes de entrada/salida, presupuesto
agregado, umbral de pérdida neta realizada, máximo de entradas (1–20), plazos
y spread/slippage máximos. Entrada ≤1000 USDT, salida/presupuesto ≤10000 USDT;
el importe concreto autorizado sigue siendo el introducido por el usuario.
El plazo conjunto de entrada, protección y recuperación no supera 24 h. Son
límites del contrato. Una conexión manual dura 15 minutos; al activar el bot se
transfiere una copia RAM de la firma exclusivamente a la concesión revisada,
con su propia caducidad. No se guarda ni se amplía mediante lecturas de la UI.

Cada selección reserva conservadoramente su tope de entrada completo:
`presupuesto reservado = selecciones × tope por entrada`. Un intento sin fill
o con respuesta perdida también consume esa reserva. No es dinero retirado
ni volumen negociado. Otra selección requiere el recibo financiero de todas
las anteriores, presupuesto disponible y `−suma(netos conciliados) < umbral`.
El umbral detiene nuevas entradas después de una pérdida realizada; no limita
por sí solo una pérdida de ejecución ni garantiza un precio de salida.

Tamaño siempre hacia abajo al paso del activo desde el libro MAINNET actual.
SL se redondea hacia la entrada y TP hacia fuera, usando los porcentajes de la
versión conservada. Se guardan cotización y metadatos para repetir el cálculo.
No se toma el capital, la cantidad ni el precio ficticios de PAPER como fondos
o ejecuciones reales. El contrato firmado/preflight conserva sus validaciones
de tick, mínimos, costes supuestos, liquidez, cuenta aislada y riesgo.

## Diario y exclusión

`grvt_mainnet_bot_runs` guarda cabecera inmutable y ancla de eventos;
`grvt_mainnet_bot_events` encadena selección, conciliación y final;
`grvt_mainnet_bot_selections` reserva una referencia única de señal y el ID
MAINNET. Se persiste selección antes del claim y el claim antes del POST.
Una selección fallida permanece quemada; reabrir o repetir no genera otro ID.

Mientras la concesión está reservada no se permite una entrada manual ni otro
bot en esa subcuenta. El único envío admitido debe corresponder a su última
selección, cotización, términos, instrumento y plazo. El claim conserva un
origen explícito `BOT_NATIVE_PROTECTED_ENTRY`, distinto del manual. Las órdenes
previas conservan sus hashes y su interpretación.

La conciliación de selección sólo enlaza un recibo financiero MAINNET ya
verificado. Acuse, posición paper o declaración del coordinador no bastan.
COMPLETE/límites exigen los recibos; STOPPED/REVIEW pueden conservar exposición
pendiente y no declaran cerrada la cuenta. Finalizar activa STOP. Nuevas órdenes
requieren revisar/rearmar y otra autorización; no se reanuda un permiso viejo.
Migraciones sobre copia cifrada y backups validan diarios y enlaces. v53→v54
conserva los permisos como evidencia sin inferir autoridad RAM ni procedencia
de un STOP antiguo.

## Recorrido operativo y salidas

1. Crear y probar una estrategia LAB sobre datos GRVT, y activar su AUTO PAPER.
2. Conectar API Trade y firma dedicada MAINNET en el panel local.
3. Elegir sesión, topes, presupuesto, umbral de pérdida, entradas y plazos.
4. «Revisar bot» prepara una revisión exacta de un uso, válida 120 s. No envía
   órdenes ni crea la concesión. Cambiar condiciones invalida la revisión.
5. Marcar «Autorizar operaciones reales con estos límites» y «Activar bot».
   Se espera una señal nueva; una posición PAPER existente no se copia.
6. Consultar estado, entradas, presupuesto reservado, neto conciliado y «Ver
   operación». STOP impide nuevos envíos; no afirma haber cerrado posiciones.

El ID del bot se conoce antes de activar. Si se pierde la respuesta local,
la UI consulta ese ID sin repetir activaciones. Cada orden usa también su ID
propio persistido antes del POST: las respuestas perdidas se investigan sin
retransmitir. No se reinician trabajos al reabrir ORBIS.

Las protecciones nativas SL/TP usan LAST y quedan en el exchange hasta su
caducidad. El seguimiento de estrategia usa fills propios completos y VWAP
real, mark MAINNET reciente (máximo 5 s) y velas trade cerradas y continuas.
No sustituye un mark ausente por LAST ni precios PAPER. El evaluador compartido
con TESTNET aplica SL/TP desde VWAP, trailing con velas completas posteriores
al fill, reglas de salida y permanencia desde el último fill. La caché sólo
reutiliza velas de la misma cohorte cerrada; el mark se consulta de nuevo.

Una decisión CLOSE se conserva antes de cancelar/cerrar. `grvt_mainnet_bot_exits`
contiene fills, observación propia, mercado, hash y cálculo reproducible; los
backups repiten el cálculo, además de cotejar hashes. El cierre usa el mismo
coordinador manual: cancelación única, dos snapshots estables, IOC reduce-only
con tope e intentos acotados, y conciliación financiera. Cancelar o recibir un
acuse no demuestra que se haya cerrado una posición.

`grvt_mainnet_stop_origins` diferencia STOP manual, por observación y final de
bot. Sólo se rearma automáticamente el STOP por observación del mismo grupo
ya conciliado, con revisión exacta y permiso RAM vigente. STOP manual, desconocido
o de otro grupo exige revisión expresa. Si un bloqueo impide guardar el final,
el historial muestra la interrupción; «Detener bot» tras desbloquear puede retirar
la concesión sin restaurar autoridad ni declarar cerrada la exposición.

## Vista protegida y bloqueo

«Continuar con vista protegida» forma parte de la revisión autorizada. Si se
acepta, ocultar la vista revoca el token HTTP y la conexión manual, conservando
sólo la custodia RAM del bot hasta su plazo original. El estado público muestra
cuántas operativas siguen activas y su caducidad, sin importes ni subcuentas.
Reautenticar permite observarlas y no renueva su concesión. Sin esa opción,
proteger la vista detiene el bot. El bloqueo completo y la parada global revocan
todos los trabajos. Cuando termina el último bot y no hay vista autenticada, la
bóveda se bloquea. El proceso debe permanecer abierto y disponer de datos.

## Consultar una operación con View

Después de desbloquear Patrimonio, abre «Operar GRVT», selecciona la subcuenta que aparece en el registro y abre «Conexión View». Introduce su clave View y pulsa «Conectar View». «Consultar» actualiza las órdenes de la operación guardada. La consulta puede conciliar en la bóveda una posición que GRVT ya cerró si la evidencia financiera coincide. [Consultar órdenes en GRVT](https://api-docs.grvt.io/trading_api/#get-order).

Si se perdió la respuesta de una orden de cierre IOC ya archivada, **Consultar** comprueba su estado por el identificador original. **Residuo según órdenes** muestra la cantidad que queda después de los fills observados. Si las órdenes cubren toda la posición, ORBIS espera dos comprobaciones separadas por al menos cinco segundos antes de archivar el recibo. Ambas deben coincidir en ejecución, saldo y posición cerrada. El recibo sigue visible tras desconectar View y reabrir la bóveda. [Consulta de orden](https://api-docs.grvt.io/trading_api/#get-order) · [Historial de ejecuciones](https://api-docs.grvt.io/trading_api/#fill-history).

Cuando la consulta directa devuelve «orden no encontrada», ORBIS busca en el historial de la misma subcuenta e instrumento. Revisa hasta cuatro páginas de la ventana archivada y exige una coincidencia única, terminal y conforme al contrato original. Si el historial resulta insuficiente o contradictorio, la operación conserva su estado pendiente. [Historial de órdenes](https://api-docs.grvt.io/trading_api/#order-history).

La conexión caduca a los 15 minutos y conserva la clave únicamente en memoria. Si ya hay una conexión Trade válida, se mantiene activa. Para enviar, cerrar una posición abierta o renovar el cierre, usa «Conexión Trade» y confirma la revisión correspondiente.

## Exportar fills MAINNET

Abre **Ver gráfico** en una operación conciliada y pulsa **Exportar JSONL**. Revisa la confirmación de descarga local en claro. En un bot LIMIT, el archivo conserva el objetivo parcial o la salida final cuando el diario los acredita. Si el gráfico sólo muestra fills observados, la descarga sigue deshabilitada hasta conciliar la operación. [Formato de investigación](IA_Y_ALMACENAMIENTO.md#fills-grvt-mainnet-jsonl).

## Pendiente

- Piloto Trade del proveedor con autorización y fondos del usuario; las pruebas
  controladas no verifican permisos ni comportamiento de su cuenta real.
- Resolución expresa de residuos bajo mínimos. La renovación de plazo/intentos
  y el cambio del tope de salida ya disponen de revisión manual independiente;
  nunca se conceden desde el bot automáticamente.
- Verificación humana del circuito wallet ↔ exchange y de los flujos restantes
  de ORBIS, incluidos los bots DEX delegados ya disponibles con proveedores
  controlados, cuyo piloto humano sigue pendiente.

Estos puntos siguen siendo parte del objetivo de ORBIS.


## Resultado tras una intervención manual

El diario del bot conserva su FINISH original. Si después se cierra/recupera su
grupo manualmente, el resultado visible se deriva del recibo financiero de ese
grupo, enlazado por la selección ya archivada. No se añade SETTLE tras FINISH,
no se reescribe la concesión ni se restaura autoridad. `financial_outcomes`
contiene referencias de recibo, fecha, neto y si el coordinador lo asentó.

La presentación suma cada recibo una sola vez y etiqueta «Cierre conciliado
fuera del bot» cuando falta el SETTLE del coordinador. Un bot detenido sigue
detenido aunque su posición ya esté conciliada. Los límites y decisiones del
motor continúan evaluándose desde los eventos originales; estos resultados
retrospectivos no cambian qué datos conocía al operar. La intervención manual
debe distinguirse al comparar rendimiento de la estrategia automática.
