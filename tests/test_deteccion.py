#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tests de la lógica de AUTODETECCIÓN del scraper, sin depender del sitio real.

Cargan HTML que imita la estructura del SCW (RichFaces/JSF) y verifican que:
  - se detecta la tabla de resultados por sus encabezados,
  - las columnas (carátula / número) se mapean por nombre,
  - se recolectan las causas,
  - se detecta la tabla de actuaciones (la más larga),
  - se localiza el botón "siguiente" del dataScroller (la barra azul).

Requisitos:
    pip install playwright
    playwright install chromium

Ejecución:
    python tests/test_deteccion.py
"""

import sys
from pathlib import Path

# Permitir importar scraper_pjn desde la raíz del repo.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright
import scraper_pjn as s


RESULTADOS = """
<table class="rich-table"><thead><tr>
<th>Nº Expediente</th><th>Carátula</th><th>Dependencia</th><th>Situación</th>
</tr></thead><tbody>
<tr><td>CIV 12345/2020</td><td>PEREZ JUAN c/ GOMEZ s/ DAÑOS</td><td>Juzgado 5</td><td>En trámite</td></tr>
<tr><td>CIV 67890/2021</td><td>PEREZ JUAN c/ ACME SA s/ COBRO</td><td>Juzgado 9</td><td>En trámite</td></tr>
</tbody></table>
"""

ACTUACIONES = """
<table class="rich-table"><tbody>
<tr><td>01/02/2024</td><td>Despacho</td><td>Provee escrito</td><td><a href="doc1.pdf">PDF</a></td></tr>
<tr><td>03/02/2024</td><td>Cédula</td><td>Notificación</td><td><a href="doc2.pdf">PDF</a></td></tr>
<tr><td>05/02/2024</td><td>Sentencia</td><td>Resuelve</td></tr>
</tbody></table>
<div class="rich-dtascroller-table"><table><tbody><tr>
<td class="rich-datascr-button">«</td>
<td class="rich-datascr-button">‹</td>
<td class="rich-datascr-act">1</td>
<td class="rich-datascr-inact">2</td>
<td class="rich-datascr-button">›</td>
<td class="rich-datascr-button">»</td>
</tr></tbody></table></div>
"""


def main():
    with sync_playwright() as p:
        navegador = p.chromium.launch(headless=True)
        page = navegador.new_page()

        # 1) Tabla de resultados + columnas
        page.set_content(RESULTADOS)
        sel_t, sel_f, idx_c, idx_n = s.detectar_tabla_resultados(page)
        assert idx_c == 1 and idx_n == 0, (idx_c, idx_n)

        # 2) Recolección de causas
        causas = s.recolectar_causas(page)
        assert len(causas) == 2, causas
        assert causas[0]["numero"].startswith("CIV 12345"), causas[0]
        assert "PEREZ JUAN" in causas[0]["caratula"], causas[0]

        # 3) Tabla de actuaciones (la más larga)
        page.set_content(ACTUACIONES)
        sel_ta, sel_fa = s.detectar_tabla_actuaciones(page)
        assert page.locator(sel_fa).count() == 3

        # 4) Paginador "barra azul"
        sig = s.localizar_siguiente(page)
        assert sig is not None and sig.inner_text() in (">", "»", "›")

        # 5) Helpers puros
        assert s._primera_fila_css(sel_fa)
        assert s.limpiar_nombre('A c/ B s/ C: 1/2') == "A c B s C 1 2"

        navegador.close()
    print("OK: todos los tests de detección pasaron.")


if __name__ == "__main__":
    main()
