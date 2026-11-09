# Manual Estudio — checklist ejecutable

Base: Estudio abierto en `#/estudio` con `BTC/EUR` + Kraken.

1. Cambiar par a `ETH/EUR` y pulsar Cargar: el gráfico recarga con ese mercado.
2. Cambiar exchange a Binance: la meta muestra "Proveedor resuelto: BINANCE".
3. Poner provider AUTO con `BTC/EUR`: resuelto Kraken visible, sin mezclar.
4. Pedir `BTC/EUR` en un provider sin ese par: mensaje + Reintentar/Cambiar proveedor.
5. Recorrer TF 1m,5m,15m,30m,1h,4h,1d,1w: cada uno carga sus velas nativas.
6. Pedir TF no soportado (URL `timeframe=3m`): 400 honesto.
7. Tipo Velas: se ven mechas y cuerpos verdes/rojos.
8. Tipo Línea y Área y Barras: cambian sin perder el rango visible.
9. Zoom con rueda: la escala temporal se estira/encoge.
10. Pan por arrastre: se desplaza; al llegar al borde izquierdo hace backfill.
11. Backfill: aparece "Cargando más datos… (vista conservada)" sin saltos ni duplicados.
12. Crosshair: el pie muestra OHLC + precio + fecha en español.
13. Último precio visible en la meta tras cada carga.
14. Reset vista: vuelve al encuadre completo.
15. Volumen: histograma en pane propio sincronizado (verde/rojo por vela).
16. Activar SMA 20: línea sobre el precio; cambiar periodo a 50 recalcula.
17. Activar EMA + Bollinger (20, 2): tres líneas sobre el precio.
18. Activar RSI 14 y MACD: paneles propios; desactivar los quita.
19. Recargar la página: indicadores y periodos persisten (mercado+provider+TF).
20. Comparar `ETH/EUR`: línea % normalizada; Quitar la elimina.
21. Añadir alerta `>` sobre el último precio y `<` bajo él: items visibles.
22. Dibujar una línea desde el gráfico (herramienta Alerta línea): crea alerta.
23. Menú contextual (clic derecho): eliminar/bloquear/ocultar/alerta.
24. Markers: toggles PAPER y DCA/planes muestran/ocultan marcas con tooltip
    (fecha/cantidad/precio/importe/PnL en PAPER).
25. Precisión: BTC con 2 decimales; un activo barato (p. ej. ADA) con 8.
26. Pantalla completa y Esc: entra y sale; Esc también desactiva la herramienta.
27. Watchlist: añadir `SOL/EUR`, recargar (persiste), Abrir lo carga en el Estudio.
28. Bid/ask visibles en "Datos bid/ask" tras cada snapshot.
30. En «Periodo visible», elegir 1, 5 o 10 años. Estudio conserva el intervalo elegido y pide más velas a la misma fuente si admite paginación. Tras 12 páginas puedes continuar al desplazar el gráfico a la izquierda. Si falta cobertura, se muestra su límite.
31. En BTC/USD con intervalo 1d y fuente automática o Bitstamp, «Todo disponible» carga el archivo de Bitstamp desde 2012 en la gráfica normal. En BTC/EUR carga su propio histórico desde 2016-04-16. «Año» enfoca un año del archivo.
29. Con posición PAPER abierta en el mercado visible: línea ámbar
    discontinua en el coste medio (etiqueta "Entrada …") + línea en el
    precio vivo con el PnL no realizado en % (verde/rojo, sigue a cada
    tick); sin posición, con la casilla PAPER apagada o en otro mercado no
    hay línea de entrada.
