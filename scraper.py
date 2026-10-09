#!/usr/bin/env python3
"""
Scraper del tipo de cambio de VENTA del dólar en bancos de Bolivia.
Actualiza la tabla `tasas_bancarias` en Supabase.

USO:
  Probar sin tocar Supabase:   python scraper.py --test
  Probar un solo banco:        python scraper.py --test --only BCP
  Actualizar Supabase:         python scraper.py
                               (requiere variables SUPABASE_URL y SUPABASE_KEY)

Requisitos:
  pip install requests beautifulsoup4 supabase playwright
  playwright install chromium
"""

import os
import re
import sys
import argparse
import time
import logging
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup

# ───────────────────────── Configuración general ─────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("scraper")

TABLE = "tasas_bancarias"
TIMEOUT = 20  # segundos para peticiones HTTP

# Rango razonable para descartar datos basura. Ajústalo si el mercado cambia.
MIN_RATE, MAX_RATE = 6.0, 20.0

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "es-BO,es;q=0.9,en;q=0.8",
}

# Selectores genéricos para cerrar avisos emergentes (modales con una X).
# Se prueban solo si están visibles; si no hay aviso ese día, no pasa nada.
DEFAULT_DISMISS = [
    "[aria-label='Close']",
    "[aria-label='Cerrar']",
    "button:has-text('×')",
    "button:has-text('✕')",
    ".modal .close",
    ".modal-close",
    ".close",
]

# ───────────────────────── Configuración por banco ───────────────────────
# strategy:
#   "text"         -> descarga el HTML con requests, extrae el texto y aplica regex
#   "browser_text" -> abre la página con Chromium (Playwright), cierra avisos,
#                     extrae el texto visible y aplica regex
# pattern: expresión regular; el grupo ( ... ) debe capturar el número de VENTA
# enabled: ponlo en False para saltarte un banco
BANKS = [
    {   # Captura: "Tipos de cambio: Dólar Compra 11,27 | Dólar Venta 12,27"
        "name": "Banco Bisa",
        "strategy": "browser_text",   # cámbialo a "text" si el número sale en Ctrl+U
        "url": "https://www.bisa.com/home",
        "pattern": r"D[óo]lar Venta\s*:?\s*([\d.,]+)",
        "enabled": True,
    },
    {   # Verificado: el valor viene en el HTML estático
        "name": "BCP",
        "strategy": "browser_text",   # requests es bloqueado por BCP (ConnectionReset)
        "url": "https://www.bcp.com.bo/",
        "pattern": r"D[óo]lar Venta:\s*([\d.,]+)",
        "enabled": True,
    },
    {   # Captura: "Dólar: Compra: 10.87 ● Venta: 12.27 ● Oficial: 11.97"
        "name": "Banco Mercantil",
        "strategy": "browser_text",
        "url": "https://www.bmsc.com.bo/",
        "pattern": r"D[óo]lar:.{0,60}?Venta:\s*([\d.,]+)",
        "enabled": True,
    },
    {   # Captura: "Dólar Compra 11.47 Dólar Venta 12.27 Dólar Oficial 11.97"
        # Nota: su robots.txt puede restringir accesos automáticos; la decisión
        # de incluirlo es tuya.
        "name": "BNB",
        "strategy": "browser_text",
        "url": "https://www.bnb.com.bo/PortalBNB/Principal/BancaPersonas",
        "pattern": r"D[óo]lar Venta\s*:?\s*([\d.,]+)",
        "enabled": True,
    },
    {   # Captura: "Dólar Oficial 11,85 Compra BOB: 10,85 / Venta 12,05"
        "name": "Banco Unión",
        "strategy": "browser_text",   # cámbialo a "text" si el número sale en Ctrl+U
        "url": "https://bancounion.com.bo/",
        "pattern": r"Compra BOB:\s*[\d.,]+\s*/\s*Venta\s*:?\s*([\d.,]+)",
        "enabled": True,
    },
    {   # Captura: "Tipo de cambio compra: 10.67 - Tipo de cambio venta: 12.15"
        "name": "BancoSol",
        "strategy": "browser_text",   # cámbialo a "text" si el número sale en Ctrl+U
        "url": "https://www.bancosol.com.bo/",
        "pattern": r"Tipo de cambio venta\s*:?\s*([\d.,]+)",
        "enabled": True,
    },
]

# ───────────────────────── Utilidades ─────────────────────────
def build_session() -> requests.Session:
    """Sesión HTTP con User-Agent y reintentos automáticos."""
    s = requests.Session()
    s.headers.update(HEADERS)
    retry = Retry(
        total=3,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    return s


def parse_rate(raw) -> float:
    """
    Convierte textos como '12,27', '12.27' o 'Bs. 12,17' a float.
    Lanza ValueError si no es un número o está fuera del rango razonable.
    """
    if isinstance(raw, (int, float)):
        value = float(raw)
    else:
        match = re.search(r"\d[\d.,]*", str(raw))
        if not match:
            raise ValueError(f"No hay número en: {raw!r}")
        num = match.group(0).rstrip(".,")
        if "," in num and "." in num:
            # El último separador es el decimal
            if num.rfind(",") > num.rfind("."):
                num = num.replace(".", "").replace(",", ".")
            else:
                num = num.replace(",", "")
        else:
            num = num.replace(",", ".")
        value = float(num)

    if not (MIN_RATE <= value <= MAX_RATE):
        raise ValueError(f"Tasa fuera de rango razonable: {value}")
    return round(value, 4)


def extract_with_regex(text: str, pattern: str) -> float:
    """Busca el patrón en el texto de la página y devuelve la tasa."""
    text = re.sub(r"\s+", " ", text)  # normaliza espacios y saltos de línea
    m = re.search(pattern, text, flags=re.IGNORECASE)
    if not m:
        raise ValueError(f"Patrón no encontrado: {pattern}")
    return parse_rate(m.group(1))

# ───────────────────────── Estrategias de extracción ─────────────────────
def fetch_text(session, bank) -> float:
    """HTML estático -> texto -> regex."""
    r = session.get(bank["url"], timeout=TIMEOUT)
    r.raise_for_status()
    text = BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)
    return extract_with_regex(text, bank["pattern"])


def dismiss_popups(page, selectors):
    """Intenta cerrar avisos emergentes. Nunca lanza error si no existen."""
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                loc.click(timeout=2_000)
                page.wait_for_timeout(500)
                log.info("Aviso cerrado con %s", sel)
                return
        except Exception:
            continue


def fetch_browser_text(session, bank) -> float:
    """Página con JavaScript: renderiza con Chromium, cierra avisos, regex."""
    from playwright.sync_api import sync_playwright  # import perezoso

    selectors = bank.get("dismiss", []) + DEFAULT_DISMISS
    # JS que comprueba si el PATRÓN REAL de la cotización ya está en pantalla
    js_check = (
        "p => new RegExp(p, 'i').test("
        "document.body.innerText.replace(/\\s+/g, ' '))"
    )
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = None
        try:
            page = browser.new_page(user_agent=HEADERS["User-Agent"])
            # "domcontentloaded" en vez de "networkidle": muchas webs de bancos
            # nunca llegan a "red inactiva" (carruseles, analytics...) y se
            # quedaban esperando hasta el timeout.
            page.goto(bank["url"], timeout=60_000, wait_until="domcontentloaded")
            try:
                page.wait_for_function(js_check, arg=bank["pattern"], timeout=30_000)
            except Exception:
                # Quizá un aviso emergente estorba: ciérralo y espera otra vez
                dismiss_popups(page, selectors)
                page.wait_for_function(js_check, arg=bank["pattern"], timeout=15_000)
            dismiss_popups(page, selectors)
            return extract_with_regex(page.inner_text("body"), bank["pattern"])
        except Exception:
            # Captura de pantalla para entender qué estaba mostrando la página
            if page is not None:
                shot = f"debug_{bank['name'].replace(' ', '_')}.png"
                try:
                    page.screenshot(path=shot)
                    log.info("Captura de depuración guardada en %s", shot)
                except Exception:
                    pass
            raise
        finally:
            browser.close()


STRATEGIES = {"text": fetch_text, "browser_text": fetch_browser_text}

def get_rate(session, bank, attempts: int = 3) -> float:
    """
    Prueba cada estrategia del banco en orden, con varios reintentos cada una.
    `strategy` puede ser un texto ("text") o una lista (["text", "browser_text"]).
    """
    strategies = bank["strategy"]
    if isinstance(strategies, str):
        strategies = [strategies]

    last_exc = None
    for strat in strategies:
        for attempt in range(1, attempts + 1):
            try:
                return STRATEGIES[strat](session, bank)
            except Exception as exc:
                last_exc = exc
                log.warning(
                    "%s [%s] intento %d/%d: %s: %s",
                    bank["name"], strat, attempt, attempts,
                    type(exc).__name__, str(exc)[:200],
                )
                time.sleep(2 * attempt)
    raise last_exc


# ───────────────────────── Supabase ─────────────────────────
def save_rate(client, name: str, rate: float) -> None:
    """Upsert por `name` (requiere restricción UNIQUE en la columna name)."""
    client.table(TABLE).upsert(
        {
            "name": name,
            "rate": rate,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
        on_conflict="name",
    ).execute()

# ───────────────────────── Main ─────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true",
                        help="Solo muestra las tasas, no escribe en Supabase")
    parser.add_argument("--only", help="Procesar solo el banco con este nombre")
    args = parser.parse_args()

    client = None
    if not args.test:
        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_KEY")
        if not url or not key:
            log.error("Faltan SUPABASE_URL o SUPABASE_KEY (o usa --test)")
            return 2
        from supabase import create_client
        client = create_client(url, key)

    session = build_session()
    ok, failed = 0, []

    for bank in BANKS:
        name = bank["name"]
        if not bank.get("enabled", True):
            log.info("– %s: desactivado, se omite", name)
            continue
        if args.only and args.only.lower() != name.lower():
            continue

        try:
            rate = get_rate(session, bank)
            if client:
                save_rate(client, name, rate)
            log.info("✔ %s: %.2f%s", name, rate, " (test)" if args.test else "")
            ok += 1
        except Exception as exc:  # un banco caído no detiene a los demás
            log.error("✘ %s falló: %s: %s", name, type(exc).__name__, exc)
            failed.append(name)

    log.info("Resumen: %d OK, %d fallidos %s", ok, len(failed), failed)
    # Código != 0 solo si TODO falló, para que GitHub Actions te avise
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
