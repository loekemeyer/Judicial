#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
 Scraper de expedientes - Poder Judicial de la Nación (PJN)
 Sistema de Consulta Web (SCW) -> https://scw.pjn.gov.ar
================================================================================

FLUJO:
  1. Abre Chromium VISIBLE en la consulta pública del SCW.
  2. PAUSA: vos ingresás el apellido en "Parte", resolvés el captcha y hacés clic
     en BUSCAR manualmente. Después volvés a la terminal y presionás ENTER.
  3. El script retoma, recorre la tabla de resultados y junta todas las causas.
  4. Entra a cada causa, recorre TODAS las páginas de "Actuaciones" usando la
     barra azul de paginación, registra cada movimiento en un CSV y descarga
     todos los adjuntos (PDFs).
  5. Guarda todo en carpetas nombradas por carátula + número de causa.

INSTALACIÓN:
    pip install playwright
    playwright install chromium

NOTAS IMPORTANTES SOBRE EL SCW (leer antes de ejecutar):
  - El SCW es una app JSF/Seam, NO una SPA. Las páginas usan postbacks/AJAX y
    arrastran un parámetro de conversación `cid` en la URL. Por eso NO conviene
    guardar URLs de expedientes y navegarlas con goto(): el `cid` caduca. La
    estrategia robusta es: entrar a la causa -> procesar -> volver atrás
    (go_back) -> re-localizar la siguiente fila. Eso es lo que hace este script.
  - Las causas PENALES y de FAMILIA no aparecen en la consulta pública.
  - Las esperas son por aparición/refresco de elementos (JSF AJAX), no por
    cambios de URL.

AJUSTES: todos los selectores marcados con  # >>> AJUSTAR  son genéricos.
Inspeccioná el HTML real (F12 -> Inspeccionar) y reemplazálos.
================================================================================
"""

import re
import sys
import time
import csv
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout


# ------------------------------------------------------------------ CONFIG ----
URL_CONSULTA   = "https://scw.pjn.gov.ar/scw/home.seam"  # >>> AJUSTAR si tu punto de entrada es otro
CARPETA_RAIZ   = Path("expedientes_pjn")                 # carpeta donde se baja todo
ESPERA_CORTA   = 0.8     # pausa cortés entre acciones (segundos)
ESPERA_TABLA   = 15000   # timeout (ms) para que aparezcan/refresquen las tablas
MAX_PAGINAS_ACT = 200    # tope de seguridad de páginas de actuaciones por causa


# ----------------------------------------------- SELECTORES (placeholders) ----
# >>> AJUSTAR TODOS según el HTML real de la página de resultados / actuaciones.

# Tabla de resultados (listado de causas tras buscar por "Parte")
SEL_TABLA_RESULTADOS = "table.dataTable"          # >>> AJUSTAR
SEL_FILAS_RESULTADOS = "table.dataTable tbody tr" # >>> AJUSTAR
SEL_LINK_ENTRAR      = "a"                         # >>> AJUSTAR: enlace/botón dentro de la fila para entrar a la causa
# Columnas de la fila de resultados (para nombrar la carpeta). Ajustá los índices:
IDX_COL_CARATULA = 1   # >>> AJUSTAR
IDX_COL_NUMERO   = 0   # >>> AJUSTAR

# Paginación del LISTADO de resultados (si las causas vienen en varias páginas).
# Dejalo en None si tu listado entra en una sola página.
SEL_RESULT_PAG_SIGUIENTE = None   # >>> AJUSTAR p.ej. "a.paginate_button.next:not(.disabled)"

# Dentro de la causa: pestaña de actuaciones
SEL_TAB_ACTUACIONES  = "text=Actuaciones"          # >>> AJUSTAR
SEL_TABLA_ACT        = "table"                      # >>> AJUSTAR: tabla de actuaciones
SEL_FILAS_ACT        = "table tbody tr"             # >>> AJUSTAR
# Barra azul de paginación de actuaciones -> botón "siguiente página"
SEL_ACT_PAG_SIGUIENTE = "a:has-text('Siguiente')"  # >>> AJUSTAR (la barra azul)
# Detección de página actual / total (opcional, para logging)
SEL_ACT_PAG_INFO      = None                        # >>> AJUSTAR si existe un "Página X de Y"

# Adjuntos: enlaces/íconos de descarga dentro de cada página de actuaciones
SEL_LINKS_DESCARGA    = "a[href*='.pdf'], a:has(img[alt*='descargar' i]), a[title*='descargar' i]"  # >>> AJUSTAR

# Volver al listado de resultados desde la causa (si no, se usa page.go_back())
SEL_BTN_VOLVER        = None   # >>> AJUSTAR p.ej. "a:has-text('Volver')"


# --------------------------------------------------------------- UTILIDADES ----
def limpiar_nombre(texto: str, largo_max: int = 150) -> str:
    """Convierte una carátula/número en un nombre de carpeta/archivo válido."""
    texto = (texto or "").strip()
    texto = re.sub(r'[<>:"/\\|?*\n\r\t]+', " ", texto)   # chars inválidos
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto[:largo_max].rstrip(" .") or "sin_nombre"


def esperar_corto():
    time.sleep(ESPERA_CORTA)


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ----------------------------------------------------- FASE 1: RECOLECTAR ----
def recolectar_causas(page) -> list[dict]:
    """
    Recorre la(s) página(s) del listado de resultados y devuelve, para cada
    causa, su carátula, número y el (pagina_listado, indice_fila) que permiten
    re-localizarla luego. Guardamos posición y NO la URL por el tema del `cid`.
    """
    causas = []
    pagina_listado = 0

    while True:
        page.wait_for_selector(SEL_TABLA_RESULTADOS, timeout=ESPERA_TABLA)
        filas = page.locator(SEL_FILAS_RESULTADOS)
        n = filas.count()
        log(f"Listado pág. {pagina_listado}: {n} causas encontradas.")

        for i in range(n):
            fila = filas.nth(i)
            celdas = fila.locator("td")
            try:
                caratula = celdas.nth(IDX_COL_CARATULA).inner_text().strip()
            except Exception:
                caratula = f"causa_{pagina_listado}_{i}"
            try:
                numero = celdas.nth(IDX_COL_NUMERO).inner_text().strip()
            except Exception:
                numero = ""
            causas.append({
                "pagina_listado": pagina_listado,
                "indice_fila": i,
                "caratula": caratula,
                "numero": numero,
            })

        # ¿Hay más páginas en el LISTADO de resultados?
        if not SEL_RESULT_PAG_SIGUIENTE:
            break
        siguiente = page.locator(SEL_RESULT_PAG_SIGUIENTE)
        if siguiente.count() == 0 or not siguiente.first.is_enabled():
            break
        siguiente.first.click()
        esperar_corto()
        pagina_listado += 1

    return causas


def ir_a_pagina_listado(page, pagina_objetivo: int):
    """Lleva el listado de resultados a la página indicada (desde la pág. 0)."""
    page.wait_for_selector(SEL_TABLA_RESULTADOS, timeout=ESPERA_TABLA)
    if not SEL_RESULT_PAG_SIGUIENTE or pagina_objetivo == 0:
        return
    for _ in range(pagina_objetivo):
        page.locator(SEL_RESULT_PAG_SIGUIENTE).first.click()
        esperar_corto()
        page.wait_for_selector(SEL_TABLA_RESULTADOS, timeout=ESPERA_TABLA)


# ----------------------------------------------- FASE 2: PROCESAR ACTUACIONES ----
def descargar_adjuntos_de_pagina(page, carpeta: Path, contador_inicial: int) -> int:
    """Descarga todos los adjuntos visibles en la página de actuaciones actual."""
    contador = contador_inicial
    enlaces = page.locator(SEL_LINKS_DESCARGA)
    total = enlaces.count()
    for j in range(total):
        enlace = enlaces.nth(j)
        try:
            with page.expect_download(timeout=ESPERA_TABLA) as dl_info:
                enlace.click()
            descarga = dl_info.value
            nombre = descarga.suggested_filename or f"adjunto_{contador}.pdf"
            destino = carpeta / f"{contador:03d}_{limpiar_nombre(nombre)}"
            descarga.save_as(str(destino))
            log(f"    ↓ {destino.name}")
            contador += 1
            esperar_corto()
        except PWTimeout:
            # El clic puede abrir un visor en pestaña nueva en vez de descargar.
            # >>> AJUSTAR: si ese es el caso, manejá el popup con page.expect_popup()
            log(f"    (sin descarga directa en el enlace #{j}; revisar manualmente)")
        except Exception as e:
            log(f"    ! Error al descargar enlace #{j}: {e}")
    return contador


def registrar_y_descargar(page, carpeta: Path):
    """
    Recorre TODAS las páginas de la pestaña de actuaciones (barra azul),
    registra cada movimiento en un CSV y descarga los adjuntos de cada página.
    """
    csv_path = carpeta / "actuaciones.csv"
    contador_archivos = 0

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["pagina_act", "fila", "texto_movimiento"])  # >>> AJUSTAR columnas

        pagina_act = 0
        while pagina_act < MAX_PAGINAS_ACT:
            page.wait_for_selector(SEL_TABLA_ACT, timeout=ESPERA_TABLA)
            filas = page.locator(SEL_FILAS_ACT)
            n = filas.count()
            log(f"  Actuaciones pág. {pagina_act}: {n} movimientos.")

            for i in range(n):
                texto = filas.nth(i).inner_text().strip().replace("\n", " | ")
                writer.writerow([pagina_act, i, texto])

            # Descargar adjuntos de esta página
            contador_archivos = descargar_adjuntos_de_pagina(
                page, carpeta, contador_archivos
            )

            # ¿Hay página siguiente en la barra azul?
            siguiente = page.locator(SEL_ACT_PAG_SIGUIENTE)
            if siguiente.count() == 0 or not siguiente.first.is_enabled():
                break
            # Tomamos una "huella" de la tabla para confirmar que cambió tras el clic
            try:
                huella_previa = page.locator(SEL_FILAS_ACT).first.inner_text()
            except Exception:
                huella_previa = ""
            siguiente.first.click()
            # Esperar a que la tabla refresque (JSF AJAX): el contenido debe cambiar
            try:
                page.wait_for_function(
                    """(prev) => {
                        const fila = document.querySelector(arguments[1]);
                        return fila && fila.innerText !== prev;
                    }""",
                    arg=huella_previa,
                    timeout=ESPERA_TABLA,
                )
            except Exception:
                page.wait_for_timeout(1500)  # fallback
            esperar_corto()
            pagina_act += 1

    log(f"  Movimientos guardados en {csv_path.name} | {contador_archivos} archivos.")


def procesar_causa(page, causa: dict):
    """Entra a una causa, procesa actuaciones y vuelve al listado."""
    carpeta_nombre = limpiar_nombre(f"{causa['numero']} - {causa['caratula']}")
    carpeta = CARPETA_RAIZ / carpeta_nombre
    carpeta.mkdir(parents=True, exist_ok=True)
    log(f"→ Procesando: {carpeta_nombre}")

    # Posicionarse en la página de listado correcta y re-localizar la fila
    ir_a_pagina_listado(page, causa["pagina_listado"])
    fila = page.locator(SEL_FILAS_RESULTADOS).nth(causa["indice_fila"])
    enlace = fila.locator(SEL_LINK_ENTRAR).first
    enlace.click()
    esperar_corto()

    # Ir a la pestaña de actuaciones
    page.wait_for_selector(SEL_TAB_ACTUACIONES, timeout=ESPERA_TABLA)
    page.locator(SEL_TAB_ACTUACIONES).first.click()
    esperar_corto()

    # Registrar movimientos + descargar adjuntos en todas las páginas
    registrar_y_descargar(page, carpeta)

    # Volver al listado de resultados
    if SEL_BTN_VOLVER:
        page.locator(SEL_BTN_VOLVER).first.click()
    else:
        page.go_back()
    esperar_corto()


# ------------------------------------------------------------------- MAIN ----
def main():
    CARPETA_RAIZ.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        navegador = p.chromium.launch(headless=False)  # VISIBLE para resolver el captcha
        contexto = navegador.new_context(accept_downloads=True)
        page = contexto.new_page()

        log(f"Abriendo {URL_CONSULTA} ...")
        page.goto(URL_CONSULTA, wait_until="load")

        # ---------------- PAUSA MANUAL (captcha) ----------------
        print("\n" + "=" * 70)
        print("  ACCIÓN MANUAL REQUERIDA")
        print("  1) Ingresá tu apellido en el campo 'Parte'.")
        print("  2) Resolvé el captcha.")
        print("  3) Hacé clic en BUSCAR y esperá a ver la tabla de resultados.")
        print("  4) Volvé a esta terminal y presioná ENTER para continuar.")
        print("=" * 70)
        input(">> ENTER cuando la tabla de resultados esté visible... ")

        # ---------------- FASE 1: recolectar causas ----------------
        try:
            page.wait_for_selector(SEL_TABLA_RESULTADOS, timeout=ESPERA_TABLA)
        except PWTimeout:
            log("No encontré la tabla de resultados. Revisá SEL_TABLA_RESULTADOS.")
            navegador.close()
            sys.exit(1)

        causas = recolectar_causas(page)
        log(f"Total de causas a procesar: {len(causas)}")

        # ---------------- FASE 2: procesar cada causa ----------------
        for idx, causa in enumerate(causas, 1):
            log(f"[{idx}/{len(causas)}]")
            try:
                procesar_causa(page, causa)
            except Exception as e:
                log(f"! Error procesando '{causa.get('caratula')}': {e}")
                # Intentar recuperar el listado para seguir con la próxima causa
                try:
                    page.go_back()
                    esperar_corto()
                except Exception:
                    pass

        log("Listo. Descarga finalizada.")
        input(">> ENTER para cerrar el navegador... ")
        navegador.close()


if __name__ == "__main__":
    main()
