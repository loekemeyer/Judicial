# Scraper de expedientes — Poder Judicial de la Nación (PJN)

Automatiza la descarga de expedientes desde el **Sistema de Consulta Web (SCW)**
del Poder Judicial de la Nación → https://scw.pjn.gov.ar

El script abre un navegador visible, hace una **pausa** para que vos ingreses el
apellido en *Parte* y resuelvas el **captcha** manualmente, y luego recorre la
tabla de resultados: entra a cada causa, registra todas las actuaciones en un
CSV y descarga los adjuntos (PDFs), organizados en carpetas por causa.

## Instalación

```bash
pip install -r requirements.txt
playwright install chromium
```

## Uso

```bash
python scraper_pjn.py
```

1. Se abre Chromium. Ingresá el apellido en el campo **Parte**, resolvé el
   captcha y hacé clic en **BUSCAR**.
2. Cuando veas la tabla de resultados, volvé a la terminal y presioná **ENTER**.
3. El script recorre las causas, guarda `actuaciones.csv` y descarga los
   adjuntos en `expedientes_pjn/<numero> - <caratula>/`.

## Notas sobre el SCW

- El SCW es una app **JSF/Seam** (no una SPA). Las URLs arrastran un parámetro
  de conversación `cid` que caduca, por eso el script re-localiza las filas en
  vez de guardar URLs.
- Las causas **penales** y de **familia** no aparecen en la consulta pública.
- Las esperas son por refresco de elementos (AJAX), no por cambio de URL.

## Ajustes

Los selectores marcados con `# >>> AJUSTAR` en `scraper_pjn.py` son genéricos.
Inspeccioná el HTML real (F12 → Inspeccionar) y reemplazálos según corresponda
a la versión vigente del sitio.
