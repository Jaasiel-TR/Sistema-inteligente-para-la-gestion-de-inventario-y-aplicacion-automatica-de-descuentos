"""
Punto de venta.

Guarda el inventario y las ventas en una carpeta (por defecto ./punto_venta):
  - inventario_actual.csv : existencias que se van descontando con cada venta
  - ventas.csv            : historial de tickets (una fila por producto vendido)
  - modelo.joblib         : modelo de clasificación entrenado con el inventario inicial

Al cobrar, el sistema aplica el descuento recomendado vigente para cada producto,
así que el precio de venta depende del inventario en ese momento.
"""
import os
from datetime import datetime

import joblib
import pandas as pd

from . import clasificacion, datos, descuentos

CARPETA_DEFAULT = "punto_venta"
COLUMNAS_VENTAS = [
    "fecha", "id_venta", "id_producto", "nombre", "cantidad",
    "precio_lista", "precio_unitario", "descuento_pct", "total_linea",
]


COLUMNAS_MOVIMIENTOS = ["fecha", "id_producto", "tipo", "detalle"]
CAMPOS_EDITABLES = [
    "nombre", "categoria", "precio_compra", "precio_venta", "existencia_actual",
    "unidades_vendidas", "dias_periodo", "dias_desde_ultima_venta",
]


def _rutas(carpeta):
    return {
        "inventario": os.path.join(carpeta, "inventario_actual.csv"),
        "ventas": os.path.join(carpeta, "ventas.csv"),
        "modelo": os.path.join(carpeta, "modelo.joblib"),
        "movimientos": os.path.join(carpeta, "movimientos.csv"),
    }


def esta_inicializado(carpeta=CARPETA_DEFAULT):
    r = _rutas(carpeta)
    return os.path.exists(r["inventario"]) and os.path.exists(r["modelo"])


def inicializar(df, modelo, carpeta=CARPETA_DEFAULT):
    """Carga un inventario nuevo. Reemplaza el inventario y borra el historial de ventas."""
    os.makedirs(carpeta, exist_ok=True)
    r = _rutas(carpeta)
    df.to_csv(r["inventario"], index=False)
    joblib.dump(modelo, r["modelo"])
    pd.DataFrame(columns=COLUMNAS_VENTAS).to_csv(r["ventas"], index=False)
    pd.DataFrame(columns=COLUMNAS_MOVIMIENTOS).to_csv(r["movimientos"], index=False)


def cargar_inventario(carpeta=CARPETA_DEFAULT):
    return pd.read_csv(_rutas(carpeta)["inventario"], dtype={"id_producto": str})


def cargar_ventas(carpeta=CARPETA_DEFAULT):
    return pd.read_csv(
        _rutas(carpeta)["ventas"],
        dtype={"id_venta": str, "id_producto": str},
    )


def recomendaciones_actuales(carpeta, max_descuento, margen_minimo):
    """Descuentos vigentes según el inventario actual (se recalculan con cada venta)."""
    r = _rutas(carpeta)
    inv = cargar_inventario(carpeta)
    modelo = joblib.load(r["modelo"])
    etiquetas = clasificacion.predecir(modelo, inv)
    etiquetas[inv["existencia_actual"] == 0] = "Normal"  # sin existencia no hay nada que descontar
    return descuentos.generar_recomendaciones(inv, etiquetas, max_descuento, margen_minimo)


def registrar_venta(carrito, max_descuento, margen_minimo, carpeta=CARPETA_DEFAULT):
    """
    carrito: dict {id_producto: cantidad}.
    Aplica los descuentos vigentes, descuenta existencias y guarda el ticket.
    Devuelve (ticket_df, total). Lanza ValueError si el carrito no es válido.
    """
    carrito = {str(k): int(v) for k, v in carrito.items() if int(v) > 0}
    if not carrito:
        raise ValueError("El carrito está vacío.")

    inv = cargar_inventario(carpeta)
    inv_idx = inv.set_index("id_producto")
    recs = recomendaciones_actuales(carpeta, max_descuento, margen_minimo).set_index("id_producto")

    # Validar existencias antes de tocar nada
    errores = []
    for pid, cantidad in carrito.items():
        if pid not in inv_idx.index:
            errores.append(f"El producto {pid} no existe")
            continue
        disponible = int(inv_idx.loc[pid, "existencia_actual"])
        if cantidad > disponible:
            errores.append(f"{inv_idx.loc[pid, 'nombre']}: solo hay {disponible} en existencia")
    if errores:
        raise ValueError("; ".join(errores))

    # Construir el ticket con los precios vigentes
    ahora = datetime.now().isoformat(timespec="seconds")
    id_venta = datetime.now().strftime("%Y%m%d%H%M%S%f")
    lineas = []
    for pid, cantidad in carrito.items():
        precio_unitario = float(recs.loc[pid, "precio_sugerido"])
        lineas.append({
            "fecha": ahora,
            "id_venta": id_venta,
            "id_producto": pid,
            "nombre": recs.loc[pid, "nombre"],
            "cantidad": cantidad,
            "precio_lista": float(inv_idx.loc[pid, "precio_venta"]),
            "precio_unitario": precio_unitario,
            "descuento_pct": float(recs.loc[pid, "descuento_pct"]),
            "total_linea": round(precio_unitario * cantidad, 2),
        })
        inv_idx.loc[pid, "existencia_actual"] -= cantidad

    ticket = pd.DataFrame(lineas, columns=COLUMNAS_VENTAS)
    total = round(float(ticket["total_linea"].sum()), 2)

    r = _rutas(carpeta)
    inv_idx.reset_index()[inv.columns].to_csv(r["inventario"], index=False)
    ticket.to_csv(r["ventas"], mode="a", header=False, index=False)
    return ticket, total


def resumen_del_dia(carpeta=CARPETA_DEFAULT):
    """Totales de las ventas registradas hoy."""
    vacio = {"tickets": 0, "ventas": 0.0, "descuentos": 0.0}
    ventas = cargar_ventas(carpeta)
    if ventas.empty:
        return vacio
    hoy = datetime.now().date().isoformat()
    ventas = ventas[ventas["fecha"].str.startswith(hoy)]
    if ventas.empty:
        return vacio
    ahorro = ((ventas["precio_lista"] - ventas["precio_unitario"]) * ventas["cantidad"]).sum()
    return {
        "tickets": int(ventas["id_venta"].nunique()),
        "ventas": round(float(ventas["total_linea"].sum()), 2),
        "descuentos": round(float(ahorro), 2),
    }


# ---------- Gestión de inventario ----------

def cargar_movimientos(carpeta=CARPETA_DEFAULT):
    r = _rutas(carpeta)
    if not os.path.exists(r["movimientos"]):
        return pd.DataFrame(columns=COLUMNAS_MOVIMIENTOS)
    return pd.read_csv(r["movimientos"], dtype={"id_producto": str})


def registrar_movimiento(carpeta, id_producto, tipo, detalle):
    """Agrega una línea a la bitácora de movimientos del inventario."""
    r = _rutas(carpeta)
    fila = pd.DataFrame([{
        "fecha": datetime.now().isoformat(timespec="seconds"),
        "id_producto": id_producto,
        "tipo": tipo,
        "detalle": detalle,
    }])
    fila.to_csv(r["movimientos"], mode="a", header=not os.path.exists(r["movimientos"]), index=False)


def _iguales(a, b):
    if pd.isna(a) and pd.isna(b):
        return True
    return a == b


def guardar_cambios(nuevo, carpeta=CARPETA_DEFAULT):
    """
    Reemplaza el inventario con la tabla editada. Valida los datos antes de guardar
    y registra en la bitácora cada alta, baja y cambio de campo.
    Devuelve el número de movimientos registrados. Lanza ValueError si los datos no son válidos.
    """
    actual = cargar_inventario(carpeta)
    nuevo = nuevo.copy()
    nuevo["id_producto"] = nuevo["id_producto"].astype(str).str.strip()
    if nuevo["id_producto"].isin(["", "nan", "None"]).any():
        raise ValueError("Todos los productos deben tener id_producto.")
    if nuevo["id_producto"].duplicated().any():
        duplicados = nuevo.loc[nuevo["id_producto"].duplicated(), "id_producto"].tolist()
        raise ValueError(f"Hay id_producto repetidos: {', '.join(duplicados)}")

    limpio, problemas = datos.validar(nuevo)
    if len(limpio) != len(nuevo):
        raise ValueError("No se guardó. Revisa estos datos: " + "; ".join(problemas))

    limpio = limpio.set_index("id_producto")
    act = actual.set_index("id_producto")
    movimientos = 0

    for p in limpio.index.difference(act.index):
        registrar_movimiento(carpeta, p, "alta", f"Nuevo producto: {limpio.loc[p, 'nombre']}")
        movimientos += 1

    for p in act.index.difference(limpio.index):
        registrar_movimiento(carpeta, p, "baja", f"Producto eliminado: {act.loc[p, 'nombre']}")
        movimientos += 1

    for p in limpio.index.intersection(act.index):
        for campo in CAMPOS_EDITABLES:
            antes, despues = act.loc[p, campo], limpio.loc[p, campo]
            if not _iguales(antes, despues):
                if campo == "existencia_actual":
                    tipo = "existencia"
                elif campo.startswith("precio"):
                    tipo = "precio"
                else:
                    tipo = "edicion"
                registrar_movimiento(carpeta, p, tipo, f"{campo}: {antes} → {despues}")
                movimientos += 1

    resultado = limpio.reset_index().reindex(columns=actual.columns)
    resultado.to_csv(_rutas(carpeta)["inventario"], index=False)
    return movimientos


def registrar_entrada(id_producto, cantidad, costo_unitario=None, carpeta=CARPETA_DEFAULT):
    """Suma existencias por una entrada de mercancía. Opcionalmente actualiza el costo unitario."""
    if cantidad <= 0:
        raise ValueError("La cantidad debe ser mayor a cero.")
    pid = str(id_producto)
    inv = cargar_inventario(carpeta)
    mascara = inv["id_producto"] == pid
    if not mascara.any():
        raise ValueError(f"El producto {pid} no existe.")

    detalle = f"+{int(cantidad)} unidades"
    inv.loc[mascara, "existencia_actual"] += int(cantidad)
    if costo_unitario:
        antes = inv.loc[mascara, "precio_compra"].iloc[0]
        inv.loc[mascara, "precio_compra"] = float(costo_unitario)
        detalle += f"; costo: {antes} → {costo_unitario}"

    inv.to_csv(_rutas(carpeta)["inventario"], index=False)
    registrar_movimiento(carpeta, pid, "entrada", detalle)
