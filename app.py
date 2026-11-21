"""
Interfaz gráfica: análisis de inventario y punto de venta.

Ejecutar con:
  streamlit run app.py
"""
import io

import pandas as pd
import streamlit as st

from inventario import clasificacion, datos, descuentos, punto_venta

st.set_page_config(page_title="Inventario y descuentos", layout="wide")
st.title("Gestión de inventario y descuentos automáticos")

# ---------- Barra lateral ----------
with st.sidebar:
    seccion = st.radio("Sección", ["Análisis", "Punto de venta", "Inventario"])

    st.header("Datos")
    archivo = st.file_uploader("Inventario (CSV o Excel)", type=["csv", "xlsx", "xls"])
    ruta_local = st.text_input(
        "O ruta de un archivo local",
        placeholder="inventario_supermercado_SIMULADO_extenso.csv",
        help="Ruta relativa a la carpeta desde donde corriste 'streamlit run app.py'.",
    )
    if archivo is None and not ruta_local.strip():
        st.caption("Sin archivo: se usa un dataset de ejemplo generado.")

    st.header("Parámetros")
    max_desc = st.slider("Tope máximo de descuento (%)", 5, 60, 30) / 100
    margen = st.slider("Margen mínimo sobre costo (%)", 0, 50, 10) / 100
    semilla = st.number_input("Semilla aleatoria", value=42, step=1)


@st.cache_data(show_spinner="Validando datos y entrenando el modelo...")
def procesar(contenido, nombre, semilla):
    """Valida datos, etiqueta y entrena el modelo. Se guarda en caché mientras no cambie el archivo."""
    if contenido is None:
        crudo = datos.generar_ejemplo(semilla=semilla)
    else:
        buf = io.BytesIO(contenido)
        buf.name = nombre
        crudo = pd.read_excel(buf) if nombre.lower().endswith((".xlsx", ".xls")) else pd.read_csv(buf)

    df, problemas = datos.validar(crudo)

    regla = clasificacion.etiquetar_regla(df)
    if datos.COLUMNA_ETIQUETA not in df.columns:
        df[datos.COLUMNA_ETIQUETA] = regla
    else:
        df[datos.COLUMNA_ETIQUETA] = df[datos.COLUMNA_ETIQUETA].fillna(regla)

    train, val, test = clasificacion.dividir(df, semilla)
    modelo, f1_val = clasificacion.entrenar(train, val, semilla)
    metricas = clasificacion.evaluar(modelo, test)
    metricas["f1_macro_validacion"] = f1_val
    return df, problemas, modelo, metricas


try:
    if archivo is not None:
        contenido, nombre = archivo.getvalue(), archivo.name
    elif ruta_local.strip():
        nombre = ruta_local.strip()
        with open(nombre, "rb") as f:
            contenido = f.read()
    else:
        contenido, nombre = None, "ejemplo.csv"
    df, problemas, modelo, metricas = procesar(contenido, nombre, int(semilla))
except Exception as e:
    st.error(f"No se pudo procesar el archivo: {e}")
    st.stop()

with st.sidebar:
    st.header("Punto de venta")
    st.caption("Cargar este inventario reemplaza el inventario actual y borra las ventas registradas.")
    if st.button("Cargar inventario al punto de venta"):
        punto_venta.inicializar(df, modelo)
        st.success("Inventario cargado en el punto de venta.")


# ---------- Sección: análisis ----------
def pagina_analisis():
    st.subheader("1. Datos")
    st.write(f"Registros válidos: **{len(df)}**")
    for p in problemas:
        st.warning(p)
    if not problemas:
        st.success("Sin problemas de calidad detectados.")

    st.subheader("2. Desempeño del modelo (conjunto de prueba)")
    c1, c2, c3 = st.columns(3)
    c1.metric("Exactitud por clase", f"{metricas['accuracy_clases']:.1%}")
    c2.metric("Precisión (candidatos)", f"{metricas['precision_candidatos_descuento']:.1%}")
    c3.metric("Recall (candidatos)", f"{metricas['recall_candidatos_descuento']:.1%}")

    with st.expander("Matriz de confusión"):
        mc = metricas["matriz_confusion"]
        matriz = pd.DataFrame(
            mc["valores"],
            index=[f"Real: {l}" for l in mc["labels"]],
            columns=[f"Predicho: {l}" for l in mc["labels"]],
        )
        st.dataframe(matriz)

    etiquetas_ia = clasificacion.predecir(modelo, df)
    recs = descuentos.generar_recomendaciones(df, etiquetas_ia, max_desc, margen)
    candidatos = recs[recs["candidato_descuento"]]

    st.subheader("3. Recomendaciones de descuento")
    k1, k2, k3 = st.columns(3)
    k1.metric("Candidatos a descuento", f"{len(candidatos)} de {len(recs)}")
    k2.metric("Capital inmovilizado", f"${candidatos['capital_inmovilizado'].sum():,.0f}")
    k3.metric(
        "Descuento promedio",
        f"{candidatos['descuento_pct'].mean():.1f}%" if len(candidatos) else "—",
    )

    st.markdown("**Capital inmovilizado por categoría (solo candidatos)**")
    if len(candidatos):
        st.bar_chart(candidatos.groupby("categoria")["capital_inmovilizado"].sum())
    else:
        st.info("No hay productos candidatos con estos parámetros.")

    f1, f2 = st.columns(2)
    etiquetas_sel = f1.multiselect(
        "Filtrar por etiqueta",
        sorted(recs["etiqueta"].unique()),
        default=sorted(recs["etiqueta"].unique()),
    )
    categorias_sel = f2.multiselect(
        "Filtrar por categoría",
        sorted(recs["categoria"].unique()),
        default=sorted(recs["categoria"].unique()),
    )
    vista = recs[recs["etiqueta"].isin(etiquetas_sel) & recs["categoria"].isin(categorias_sel)]
    st.dataframe(vista, width="stretch", hide_index=True)

    st.download_button(
        "Descargar recomendaciones (CSV)",
        data=recs.to_csv(index=False).encode("utf-8"),
        file_name="recomendaciones.csv",
        mime="text/csv",
    )


# ---------- Sección: punto de venta ----------
def etiqueta_producto(fila):
    txt = f"{fila['nombre']} — ${fila['precio_sugerido']:.2f}"
    if fila["descuento_pct"] > 0:
        txt += f" (desc. {fila['descuento_pct']:.0f}%)"
    return txt + f" · stock {int(fila['existencia_actual'])}"


def pagina_punto_venta():
    if not punto_venta.esta_inicializado():
        st.info("Carga primero un inventario con el botón de la barra lateral.")
        return

    if "carrito" not in st.session_state:
        st.session_state.carrito = {}
    if "mensaje_venta" in st.session_state:
        st.success(st.session_state.pop("mensaje_venta"))

    recs = punto_venta.recomendaciones_actuales(punto_venta.CARPETA_DEFAULT, max_desc, margen)
    rec_idx = recs.set_index("id_producto")
    resumen = punto_venta.resumen_del_dia()

    m1, m2, m3 = st.columns(3)
    m1.metric("Ventas de hoy", f"${resumen['ventas']:,.2f}")
    m2.metric("Tickets de hoy", resumen["tickets"])
    m3.metric("Descuentos otorgados hoy", f"${resumen['descuentos']:,.2f}")

    col_prod, col_ticket = st.columns([3, 2])
    carrito = st.session_state.carrito

    with col_prod:
        st.subheader("Productos")
        buscar = st.text_input("Buscar producto")
        mostrar = recs[recs["nombre"].str.contains(buscar, case=False, na=False)] if buscar else recs
        if mostrar.empty:
            st.info("Ningún producto coincide con la búsqueda.")
        else:
            pid = st.selectbox(
                "Producto",
                mostrar["id_producto"].tolist(),
                format_func=lambda p: etiqueta_producto(rec_idx.loc[p]),
            )
            cantidad = st.number_input("Cantidad", min_value=1, value=1, step=1)
            if st.button("Agregar al carrito"):
                carrito[pid] = carrito.get(pid, 0) + int(cantidad)
                st.rerun()

    with col_ticket:
        st.subheader("Ticket actual")
        if not carrito:
            st.caption("Sin productos en el carrito.")
        else:
            filas = []
            total = 0.0
            for p, c in carrito.items():
                r = rec_idx.loc[p]
                importe = round(r["precio_sugerido"] * c, 2)
                total += importe
                filas.append({
                    "Producto": r["nombre"],
                    "Cant.": c,
                    "Precio": f"${r['precio_sugerido']:.2f}",
                    "Importe": f"${importe:.2f}",
                })
            st.dataframe(pd.DataFrame(filas), hide_index=True, width="stretch")
            st.metric("Total", f"${total:,.2f}")

            b1, b2 = st.columns(2)
            if b1.button("Cobrar", type="primary"):
                try:
                    _, total_cobrado = punto_venta.registrar_venta(
                        carrito, max_desc, margen, punto_venta.CARPETA_DEFAULT
                    )
                    st.session_state.carrito = {}
                    st.session_state.mensaje_venta = f"Venta registrada por ${total_cobrado:,.2f}."
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))
            if b2.button("Vaciar carrito"):
                st.session_state.carrito = {}
                st.rerun()

    with st.expander("Historial de ventas de hoy"):
        ventas = punto_venta.cargar_ventas(punto_venta.CARPETA_DEFAULT)
        hoy = ventas[ventas["fecha"].str.startswith(pd.Timestamp.now().date().isoformat())] if len(ventas) else ventas
        if hoy.empty:
            st.caption("Todavía no hay ventas registradas hoy.")
        else:
            st.dataframe(hoy.iloc[::-1], hide_index=True, width="stretch")


# ---------- Sección: inventario ----------
def pagina_inventario():
    if not punto_venta.esta_inicializado():
        st.info("Carga primero un inventario con el botón de la barra lateral.")
        return

    if "version_inv" not in st.session_state:
        st.session_state.version_inv = 0
    if "mensaje_inv" in st.session_state:
        st.success(st.session_state.pop("mensaje_inv"))

    inv = punto_venta.cargar_inventario(punto_venta.CARPETA_DEFAULT)

    st.subheader("Editar inventario")
    st.caption(
        "Cambia precios, existencias y datos directamente en la tabla. "
        "Para agregar un producto usa la última fila vacía. Para eliminar uno, "
        "selecciona la fila y pulsa Supr. Cada cambio queda en la bitácora al guardar."
    )
    editado = st.data_editor(
        inv,
        num_rows="dynamic",
        hide_index=True,
        width="stretch",
        key=f"editor_inv_{st.session_state.version_inv}",
    )
    if st.button("Guardar cambios", type="primary"):
        try:
            n = punto_venta.guardar_cambios(editado, punto_venta.CARPETA_DEFAULT)
            st.session_state.version_inv += 1
            st.session_state.mensaje_inv = (
                f"Cambios guardados. Movimientos registrados: {n}."
                if n else "No había cambios que guardar."
            )
            st.rerun()
        except ValueError as e:
            st.error(str(e))

    st.subheader("Entrada de mercancía")
    nombres = dict(zip(inv["id_producto"], inv["nombre"]))
    with st.form("entrada_mercancia", clear_on_submit=True):
        pid = st.selectbox(
            "Producto",
            list(nombres.keys()),
            format_func=lambda p: f"{nombres[p]} ({p})",
        )
        cantidad = st.number_input("Cantidad que entra", min_value=1, value=1, step=1)
        costo = st.number_input(
            "Costo unitario nuevo (0 = no cambiar)", min_value=0.0, value=0.0, step=0.5
        )
        if st.form_submit_button("Registrar entrada"):
            try:
                punto_venta.registrar_entrada(
                    pid, int(cantidad), costo if costo > 0 else None, punto_venta.CARPETA_DEFAULT
                )
                st.session_state.version_inv += 1
                st.session_state.mensaje_inv = f"Entrada registrada: +{int(cantidad)} de {nombres[pid]}."
                st.rerun()
            except ValueError as e:
                st.error(str(e))

    with st.expander("Bitácora de movimientos"):
        mov = punto_venta.cargar_movimientos(punto_venta.CARPETA_DEFAULT)
        if mov.empty:
            st.caption("Todavía no hay movimientos registrados.")
        else:
            st.dataframe(mov.iloc[::-1], hide_index=True, width="stretch")


if seccion == "Análisis":
    pagina_analisis()
elif seccion == "Punto de venta":
    pagina_punto_venta()
else:
    pagina_inventario()
