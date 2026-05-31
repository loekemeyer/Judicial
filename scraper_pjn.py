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
  4. Entra a cada causa, abre la pestaña/solapa de "Actuaciones", recorre TODAS
     las páginas usando la barra azul de paginación (dataScroller de RichFaces),
     registra cada movimiento en un CSV y descarga todos los adjuntos (PDFs).
  5. Guarda todo en carpetas nombradas por número de causa + carátula.

INSTALACIÓN:
    pip install playwright
    playwright install chromium

EJECUCIÓN:
    python scraper_pjn.py            # corrida normal
    python scraper_pjn.py --debug    # vuelca diagnóstico de la página actual

DISEÑO / POR QUÉ "SE ADAPTA SOLO":
  El SCW es una app JSF/Seam con RichFaces (NO una SPA). Dos consecuencias:
    a) Las URLs arrastran un parámetro de conversación `cid` que CADUCA. Por eso
       NO guardamos URLs de expedientes: entramos a la causa, la procesamos y
       volvemos atrás (go_back) para re-localizar la siguiente fila.
    b) Los `id` de los componentes JSF son largos y volátiles
       (p. ej. "j_idt123:tabla:0:link"). Adivinarlos a ciegas es frágil. Por eso
       este script DETECTA la estructura por contenido:
         - la tabla de resultados se identifica por el texto de sus encabezados,
         - las columnas (carátula / número) se mapean por nombre de encabezado,
         - el paginador se busca por su rol/towards ("siguiente", ">", "»"…),
         - los adjuntos se descargan leyendo el href cuando existe, y si no,
           manejando la descarga o el popup del visor PDF.
  Si tu instancia del SCW difiere, podés FIJAR selectores en la sección
  OVERRIDES (más abajo): si están seteados, tienen prioridad sobre la detección.

  Las causas PENALES y de FAMILIA no aparecen en la consulta pública.
================================================================================
"""

import re
import sys
import time
import csv
import argparse
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout


# ------------------------------------------------------------------ CONFIG ----
URL_CONSULTA    = "https://scw.pjn.gov.ar/scw/home.seam"
CARPETA_RAIZ    = Path("expedientes_pjn")
ESPERA_CORTA    = 0.8      # pausa cortés entre acciones (segundos)
ESPERA_TABLA    = 20000    # timeout (ms) para que aparezcan/refresquen las tablas
ESPERA_DESCARGA = 30000    # timeout (ms) para una descarga individual
MAX_PAGINAS_ACT = 300      # tope de seguridad de páginas de actuaciones por causa

# Palabras clave para reconocer cosas por contenido (sin depender de ids JSF).
KW_ENCABEZADOS_RESULTADOS = ("carátula", "caratula", "expediente", "dependencia",
                             "jurisdicción", "jurisdiccion", "situación", "situacion")
KW_COL_CARATULA = ("carátula", "caratula", "autos")
KW_COL_NUMERO   = ("número", "numero", "nº", "n°", "expediente", "expte")
KW_TAB_ACTUACIONES = ("actuaciones",)
# Textos típicos del botón "página siguiente" en el dataScroller de RichFaces.
KW_SIGUIENTE = (">", "»", "siguiente", "next", "›")
KW_ULTIMA    = (">>", "»»", "última", "ultima", "last")

# ----------------------------------------------------------------- OVERRIDES --
# Si la autodetección falla en tu instancia, fijá acá los selectores reales
# (inspeccioná con F12). Si quedan en None, se usa la detección automática.
OVR_TABLA_RESULTADOS  = None   # p.ej. "table.rich-table"
OVR_FILAS_RESULTADOS  = None   # p.ej. "table.rich-table tbody tr.rich-table-row"
OVR_LINK_ENTRAR       = None   # p.ej. "a[id$='verExpediente']"
OVR_IDX_COL_CARATULA  = None   # índice de columna (int) si lo sabés
OVR_IDX_COL_NUMERO    = None
OVR_TAB_ACTUACIONES   = None   # p.ej. "a:has-text('Actuaciones')"
OVR_TABLA_ACT         = None
OVR_FILAS_ACT         = None
OVR_ACT_PAG_SIGUIENTE = None   # control "siguiente" de la barra azul
OVR_LINKS_DESCARGA    = None
OVR_BTN_VOLVER        = None


# --------------------------------------------------------------- UTILIDADES ----
def limpiar_nombre(texto: str, largo_max: int = 150) -> str:
    """Convierte una carátula/número en un nombre de carpeta/archivo válido."""
    texto = (texto or "").strip()
    texto = re.sub(r'[<>:"/\\|?*\n\r\t]+', " ", texto)
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto[:largo_max].rstrip(" .") or "sin_nombre"


def esperar_corto():
    time.sleep(ESPERA_CORTA)


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


# ------------------------------------------------------- DETECCIÓN DE TABLA ----
def detectar_tabla_resultados(page):
    """
    Devuelve (selector_tabla, selector_filas, idx_caratula, idx_numero).

    Estrategia: recorre todas las <table> de la página y elige la primera cuyos
    encabezados contengan palabras clave de expedientes. Mapea las columnas de
    carátula y número por el texto del encabezado. No depende de ids JSF.
    """
    if OVR_TABLA_RESULTADOS:
        filas = OVR_FILAS_RESULTADOS or f"{OVR_TABLA_RESULTADOS} tbody tr"
        idx_c = OVR_IDX_COL_CARATULA if OVR_IDX_COL_CARATULA is not None else 1
        idx_n = OVR_IDX_COL_NUMERO if OVR_IDX_COL_NUMERO is not None else 0
        return OVR_TABLA_RESULTADOS, filas, idx_c, idx_n

    tablas = page.locator("table")
    total = tablas.count()
    for t in range(total):
        tabla = tablas.nth(t)
        # Encabezados: th de la tabla (o primera fila si no hay th).
        ths = tabla.locator("th")
        encabezados = []
        if ths.count() > 0:
            for h in range(ths.count()):
                encabezados.append(_norm(ths.nth(h).inner_text()))
        else:
            primera = tabla.locator("tr").first
            tds = primera.locator("td")
            for h in range(tds.count()):
                encabezados.append(_norm(tds.nth(h).inner_text()))

        texto_enc = " ".join(encabezados)
        if not any(kw in texto_enc for kw in KW_ENCABEZADOS_RESULTADOS):
            continue

        # Mapear columnas por nombre de encabezado.
        idx_caratula = _buscar_col(encabezados, KW_COL_CARATULA, defecto=1)
        idx_numero   = _buscar_col(encabezados, KW_COL_NUMERO,   defecto=0)

        # Construir selectores nth-of-type estables para esta tabla.
        sel_tabla = f"table >> nth={t}"
        sel_filas = f"table >> nth={t} >> tbody tr"
        # Verificar que haya filas reales (con celdas).
        filas = page.locator(sel_filas)
        if filas.count() == 0:
            sel_filas = f"table >> nth={t} >> tr"
        log(f"Tabla de resultados detectada (#{t}). Encabezados: {encabezados}")
        log(f"  -> col carátula={idx_caratula}, col número={idx_numero}")
        return sel_tabla, sel_filas, idx_caratula, idx_numero

    raise RuntimeError(
        "No pude detectar la tabla de resultados por contenido. "
        "Corré con --debug y/o fijá OVR_TABLA_RESULTADOS."
    )


def _buscar_col(encabezados, claves, defecto):
    for i, enc in enumerate(encabezados):
        if any(k in enc for k in claves):
            return i
    return defecto


# ----------------------------------------------------- FASE 1: RECOLECTAR ----
def recolectar_causas(page) -> list[dict]:
    """
    Recorre la(s) página(s) del listado de resultados y devuelve, para cada
    causa, su carátula, número y el indice_fila que permite re-localizarla.
    """
    sel_tabla, sel_filas, idx_c, idx_n = detectar_tabla_resultados(page)
    # Guardamos los selectores detectados en el page para reusarlos.
    page._scw = {  # type: ignore[attr-defined]
        "sel_tabla": sel_tabla, "sel_filas": sel_filas,
        "idx_c": idx_c, "idx_n": idx_n,
    }

    causas = []
    page.wait_for_selector(sel_tabla, timeout=ESPERA_TABLA)
    filas = page.locator(sel_filas)
    n = filas.count()
    log(f"Listado: {n} causas encontradas.")

    for i in range(n):
        fila = filas.nth(i)
        celdas = fila.locator("td")
        try:
            caratula = celdas.nth(idx_c).inner_text().strip()
        except Exception:
            caratula = f"causa_{i}"
        try:
            numero = celdas.nth(idx_n).inner_text().strip()
        except Exception:
            numero = ""
        # Filtrar filas vacías / de cabecera.
        if not (caratula or numero):
            continue
        causas.append({"indice_fila": i, "caratula": caratula, "numero": numero})

    return causas


# ------------------------------------------- DETECCIÓN ENTRAR A LA CAUSA ----
def entrar_a_causa(page, indice_fila: int):
    """Hace clic en el link/lupa de la fila indicada para abrir el expediente."""
    info = page._scw  # type: ignore[attr-defined]
    fila = page.locator(info["sel_filas"]).nth(indice_fila)

    if OVR_LINK_ENTRAR:
        fila.locator(OVR_LINK_ENTRAR).first.click()
        return

    # Heurística: el link para entrar suele ser una lupa/ícono o un <a> sobre la
    # carátula. Probamos candidatos en orden de probabilidad.
    candidatos = [
        "a:has(img)",                       # ícono/lupa
        "a[onclick]",                       # postback JSF
        "a[href]:not([href='#'])",
        "a",
        "input[type='image']",
        "button",
    ]
    for sel in candidatos:
        loc = fila.locator(sel)
        if loc.count() > 0:
            loc.first.click()
            return
    raise RuntimeError(f"No encontré cómo entrar a la causa en la fila {indice_fila}.")


# ------------------------------------------- DETECCIÓN PESTAÑA ACTUACIONES ----
def abrir_actuaciones(page):
    """Hace clic en la solapa/pestaña 'Actuaciones' dentro del expediente."""
    if OVR_TAB_ACTUACIONES:
        page.wait_for_selector(OVR_TAB_ACTUACIONES, timeout=ESPERA_TABLA)
        page.locator(OVR_TAB_ACTUACIONES).first.click()
        return

    # Buscar por texto, de forma tolerante (link, tab o span clickeable).
    for sel in (
        "a:has-text('Actuaciones')",
        "span:has-text('Actuaciones')",
        "td:has-text('Actuaciones')",
        "text=Actuaciones",
    ):
        loc = page.locator(sel)
        if loc.count() > 0:
            try:
                loc.first.click()
                esperar_corto()
                return
            except Exception:
                continue
    # Puede que la lista de actuaciones ya esté visible sin pestaña: no es fatal.
    log("  (no encontré la pestaña 'Actuaciones'; asumo que ya está visible)")


# ------------------------------------------- DETECCIÓN TABLA ACTUACIONES ----
def detectar_tabla_actuaciones(page):
    """Devuelve (sel_tabla, sel_filas) de la tabla de actuaciones (la más larga)."""
    if OVR_TABLA_ACT:
        return OVR_TABLA_ACT, (OVR_FILAS_ACT or f"{OVR_TABLA_ACT} tbody tr")

    # Elegimos la tabla con más filas de datos (la de actuaciones suele serlo).
    tablas = page.locator("table")
    mejor_idx, mejor_filas = None, -1
    for t in range(tablas.count()):
        filas = tablas.nth(t).locator("tbody tr")
        c = filas.count()
        if c == 0:
            filas = tablas.nth(t).locator("tr")
            c = filas.count()
        if c > mejor_filas:
            mejor_filas, mejor_idx = c, t
    if mejor_idx is None:
        raise RuntimeError("No hay tablas en la página de actuaciones.")
    sel_tabla = f"table >> nth={mejor_idx}"
    sel_filas = f"table >> nth={mejor_idx} >> tbody tr"
    if page.locator(sel_filas).count() == 0:
        sel_filas = f"table >> nth={mejor_idx} >> tr"
    return sel_tabla, sel_filas


# ------------------------------------------- DETECCIÓN PAGINADOR (barra azul) ----
def localizar_siguiente(page):
    """
    Devuelve un Locator del control 'página siguiente' del dataScroller de
    RichFaces (la barra azul), o None si no hay más páginas.
    """
    if OVR_ACT_PAG_SIGUIENTE:
        loc = page.locator(OVR_ACT_PAG_SIGUIENTE)
        return loc.first if loc.count() else None

    # Candidatos típicos del dataScroller de RichFaces y de tablas JSF.
    candidatos = [
        "td.rich-datascr-button:has-text('»')",
        "td.rich-datascr-button:has-text('>')",
        ".rich-datascr-button[onclick*='next' i]",
        "a[id$='next']", "a[id$='fastforward']",
        "a:has-text('Siguiente')", "a:has-text('siguiente')",
        "[title*='siguiente' i]", "[title*='next' i]",
    ]
    for sel in candidatos:
        loc = page.locator(sel)
        if loc.count() > 0:
            return loc.first

    # Último recurso: cualquier celda/enlace de la barra cuyo texto sea ">" o "»".
    for txt in (">", "»", "›"):
        loc = page.get_by_text(txt, exact=True)
        if loc.count() > 0:
            return loc.first
    return None


# ----------------------------------------------- FASE 2: PROCESAR ACTUACIONES ----
def descargar_adjuntos_de_pagina(page, contexto, carpeta: Path, contador: int) -> int:
    """Descarga todos los adjuntos visibles en la página de actuaciones actual."""
    sel = OVR_LINKS_DESCARGA or (
        "a[href$='.pdf'], a[href*='.pdf?'], a[href*='descarga' i], "
        "a[href*='verDocumento' i], a[href*='documento' i], "
        "a:has(img[alt*='descargar' i]), a:has(img[src*='pdf' i]), "
        "a[title*='descargar' i], a[title*='ver documento' i]"
    )
    enlaces = page.locator(sel)
    total = enlaces.count()
    for j in range(total):
        enlace = enlaces.nth(j)
        try:
            ok = _descargar_un_enlace(page, contexto, enlace, carpeta, contador)
            if ok:
                contador += 1
                esperar_corto()
        except Exception as e:
            log(f"    ! Error en adjunto #{j}: {e}")
    return contador


def _descargar_un_enlace(page, contexto, enlace, carpeta: Path, contador: int) -> bool:
    """
    Descarga un único adjunto. Tres caminos, en orden:
      1) Si tiene href real (no '#'/javascript): bajar por HTTP con la sesión.
      2) Clic que dispara una descarga directa (expect_download).
      3) Clic que abre el PDF en una pestaña nueva (popup): bajar esa URL.
    """
    # 1) href directo -> usar la sesión del contexto (comparte cookies).
    try:
        href = enlace.get_attribute("href")
    except Exception:
        href = None
    if href and not href.strip().lower().startswith(("#", "javascript:")):
        url = urljoin(page.url, href)
        try:
            resp = contexto.request.get(url, timeout=ESPERA_DESCARGA)
            if resp.ok:
                nombre = _nombre_desde_respuesta(resp, url, contador)
                destino = carpeta / f"{contador:03d}_{limpiar_nombre(nombre)}"
                destino.write_bytes(resp.body())
                log(f"    ↓ {destino.name}")
                return True
        except Exception:
            pass  # caemos a los métodos por clic

    # 2) Descarga directa por clic.
    try:
        with page.expect_download(timeout=ESPERA_DESCARGA) as dl_info:
            enlace.click()
        descarga = dl_info.value
        nombre = descarga.suggested_filename or f"adjunto_{contador}.pdf"
        destino = carpeta / f"{contador:03d}_{limpiar_nombre(nombre)}"
        descarga.save_as(str(destino))
        log(f"    ↓ {destino.name}")
        return True
    except PWTimeout:
        pass

    # 3) Popup con visor PDF: buscar la pestaña nueva y bajar su URL.
    paginas = contexto.pages
    if len(paginas) > 1:
        popup = paginas[-1]
        try:
            popup.wait_for_load_state("domcontentloaded", timeout=ESPERA_DESCARGA)
        except Exception:
            pass
        url = popup.url
        try:
            if url and url.lower().startswith("http"):
                resp = contexto.request.get(url, timeout=ESPERA_DESCARGA)
                if resp.ok:
                    nombre = _nombre_desde_respuesta(resp, url, contador)
                    destino = carpeta / f"{contador:03d}_{limpiar_nombre(nombre)}"
                    destino.write_bytes(resp.body())
                    log(f"    ↓ {destino.name} (desde visor)")
                    return True
        finally:
            try:
                popup.close()
            except Exception:
                pass

    log("    (enlace sin descarga utilizable; revisar manualmente)")
    return False


def _nombre_desde_respuesta(resp, url: str, contador: int) -> str:
    """Deriva un nombre de archivo del Content-Disposition o de la URL."""
    try:
        cd = resp.headers.get("content-disposition", "")
    except Exception:
        cd = ""
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd, re.I)
    if m:
        return m.group(1)
    base = url.split("?")[0].rstrip("/").split("/")[-1] or f"adjunto_{contador}"
    if "." not in base:
        base += ".pdf"
    return base


def registrar_y_descargar(page, contexto, carpeta: Path):
    """
    Recorre TODAS las páginas de actuaciones (barra azul), registra cada
    movimiento en un CSV y descarga los adjuntos de cada página.
    """
    sel_tabla, sel_filas = detectar_tabla_actuaciones(page)
    csv_path = carpeta / "actuaciones.csv"
    contador_archivos = 0

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["pagina_act", "fila", "texto_movimiento"])

        pagina_act = 0
        while pagina_act < MAX_PAGINAS_ACT:
            page.wait_for_selector(sel_tabla, timeout=ESPERA_TABLA)
            filas = page.locator(sel_filas)
            n = filas.count()
            log(f"  Actuaciones pág. {pagina_act}: {n} movimientos.")

            for i in range(n):
                texto = filas.nth(i).inner_text().strip().replace("\n", " | ")
                if texto:
                    writer.writerow([pagina_act, i, texto])

            contador_archivos = descargar_adjuntos_de_pagina(
                page, contexto, carpeta, contador_archivos
            )

            siguiente = localizar_siguiente(page)
            if siguiente is None:
                break
            # ¿El control está deshabilitado? (RichFaces lo marca con clase/disabled)
            try:
                cls = (siguiente.get_attribute("class") or "").lower()
                if "dsbld" in cls or "disabled" in cls:
                    break
                if not siguiente.is_enabled():
                    break
            except Exception:
                pass

            # Huella para confirmar que la tabla cambió tras el clic (JSF AJAX).
            try:
                huella_previa = page.locator(sel_filas).first.inner_text()
            except Exception:
                huella_previa = ""

            try:
                siguiente.click()
            except Exception:
                break

            # Esperar refresco AJAX: la primera fila debe cambiar de contenido.
            try:
                page.wait_for_function(
                    """([prevText, selector]) => {
                        const fila = document.querySelector(selector);
                        return fila && fila.innerText.trim() !== prevText.trim();
                    }""",
                    arg=[huella_previa, _primera_fila_css(sel_filas)],
                    timeout=ESPERA_TABLA,
                )
            except Exception:
                page.wait_for_timeout(1500)  # fallback
            esperar_corto()
            pagina_act += 1

    log(f"  Movimientos guardados en {csv_path.name} | {contador_archivos} archivos.")


def _primera_fila_css(sel_filas: str) -> str:
    """
    Convierte el selector de filas (puede usar la sintaxis '>>' de Playwright) en
    un selector CSS simple para querySelector dentro de wait_for_function.
    """
    # Tomamos el último tramo CSS tras el último '>>'.
    css = sel_filas.split(">>")[-1].strip()
    # Quitar pseudo-engine de nth si quedó.
    css = re.sub(r"nth=\d+", "", css).strip()
    return css or "tbody tr"


def procesar_causa(page, contexto, causa: dict):
    """Entra a una causa, procesa actuaciones y vuelve al listado."""
    carpeta_nombre = limpiar_nombre(f"{causa['numero']} - {causa['caratula']}")
    carpeta = CARPETA_RAIZ / carpeta_nombre
    carpeta.mkdir(parents=True, exist_ok=True)
    log(f"→ Procesando: {carpeta_nombre}")

    entrar_a_causa(page, causa["indice_fila"])
    esperar_corto()

    abrir_actuaciones(page)
    esperar_corto()

    registrar_y_descargar(page, contexto, carpeta)

    # Volver al listado de resultados.
    if OVR_BTN_VOLVER:
        try:
            page.locator(OVR_BTN_VOLVER).first.click()
        except Exception:
            page.go_back()
    else:
        page.go_back()
    esperar_corto()
    # Re-detectar la tabla (el cid cambió; los selectores nth siguen valiendo).
    page.wait_for_selector(page._scw["sel_tabla"], timeout=ESPERA_TABLA)  # type: ignore[attr-defined]


# ------------------------------------------------------------------ DEBUG ----
def volcar_diagnostico(page, carpeta_dbg=Path("debug_scw")):
    """Vuelca HTML y un resumen de tablas para ayudar a afinar selectores."""
    carpeta_dbg.mkdir(parents=True, exist_ok=True)
    (carpeta_dbg / "pagina.html").write_text(page.content(), encoding="utf-8")
    page.screenshot(path=str(carpeta_dbg / "pagina.png"), full_page=True)

    resumen = []
    tablas = page.locator("table")
    for t in range(tablas.count()):
        tabla = tablas.nth(t)
        ths = tabla.locator("th")
        heads = [_norm(ths.nth(h).inner_text()) for h in range(ths.count())]
        nfilas = tabla.locator("tbody tr").count() or tabla.locator("tr").count()
        resumen.append(f"Tabla #{t}: filas={nfilas} | encabezados={heads}")
    (carpeta_dbg / "tablas.txt").write_text("\n".join(resumen), encoding="utf-8")
    log("Diagnóstico volcado en ./debug_scw/ (pagina.html, pagina.png, tablas.txt)")
    for r in resumen:
        log("  " + r)


# ------------------------------------------------------------------- MAIN ----
def main():
    ap = argparse.ArgumentParser(description="Scraper de expedientes PJN (SCW).")
    ap.add_argument("--debug", action="store_true",
                    help="Tras la pausa manual, vuelca diagnóstico y termina.")
    args = ap.parse_args()

    CARPETA_RAIZ.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        navegador = p.chromium.launch(headless=False)  # VISIBLE para el captcha
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

        if args.debug:
            volcar_diagnostico(page)
            input(">> ENTER para cerrar el navegador... ")
            navegador.close()
            return

        # ---------------- FASE 1: recolectar causas ----------------
        try:
            causas = recolectar_causas(page)
        except Exception as e:
            log(f"No pude leer el listado de resultados: {e}")
            log("Sugerencia: corré 'python scraper_pjn.py --debug' para diagnosticar.")
            navegador.close()
            sys.exit(1)

        log(f"Total de causas a procesar: {len(causas)}")
        if not causas:
            log("No hay causas para procesar.")
            navegador.close()
            return

        # ---------------- FASE 2: procesar cada causa ----------------
        for idx, causa in enumerate(causas, 1):
            log(f"[{idx}/{len(causas)}]")
            try:
                procesar_causa(page, contexto, causa)
            except Exception as e:
                log(f"! Error procesando '{causa.get('caratula')}': {e}")
                try:
                    page.go_back()
                    esperar_corto()
                    page.wait_for_selector(page._scw["sel_tabla"], timeout=ESPERA_TABLA)  # type: ignore[attr-defined]
                except Exception:
                    pass

        log("Listo. Descarga finalizada.")
        input(">> ENTER para cerrar el navegador... ")
        navegador.close()


if __name__ == "__main__":
    main()
