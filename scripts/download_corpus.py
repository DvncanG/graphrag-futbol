"""
Paso 2a: descargar el corpus.

Baja articulos de la Wikipedia en espanol sobre entrenadores y los guarda
en data/corpus/ como .txt planos.

Se trunca cada articulo a MAX_CHARS a proposito: un 7B tarda ~10-20s por
chunk, y un articulo completo de Guardiola son 40+ chunks. Con la parte
inicial (biografia, trayectoria) ya tenemos las relaciones que interesan.

Uso:
    python scripts/download_corpus.py
"""

import re
import sys
from pathlib import Path

import wikipediaapi

CORPUS_DIR = Path(__file__).parent.parent / "data" / "corpus"
MAX_CHARS = 8000

# Elegidos porque forman cadenas de influencia reales y cruzadas:
#   Michels -> Cruyff -> Guardiola -> Arteta
#   Bielsa -> Pochettino / Simeone
#   Van Gaal -> Mourinho (fue su asistente en el Barcelona)
#   Sacchi -> Ancelotti
ARTICULOS = [
    "Rinus Michels",
    "Johan Cruyff",
    "Louis van Gaal",
    "Arrigo Sacchi",
    "Marcelo Bielsa",
    "Pep Guardiola",
    "José Mourinho",
    "Carlo Ancelotti",
    "Mauricio Pochettino",
    "Diego Simeone",
    "Mikel Arteta",
    "Jürgen Klopp",
    "Xavi Hernández",  # "Xavi" a secas es una pagina de desambiguacion
    "Roberto De Zerbi",
]


def slugify(titulo: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", titulo.lower()).strip("_")


def main() -> int:
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)

    # Wikipedia exige un user-agent identificable. Pon tu email real.
    wiki = wikipediaapi.Wikipedia(
        user_agent="graphrag-futbol/0.1 (https://github.com/DvncanG)",
        language="es",
    )

    fallos = []
    for titulo in ARTICULOS:
        pagina = wiki.page(titulo)
        if not pagina.exists():
            print(f"  [FAIL] {titulo} -> no existe en es.wikipedia")
            fallos.append(titulo)
            continue

        texto = pagina.text[:MAX_CHARS]
        destino = CORPUS_DIR / f"{slugify(titulo)}.txt"
        destino.write_text(texto, encoding="utf-8")
        print(f"  [OK]   {titulo:25} {len(texto):>6} chars -> {destino.name}")

    print(f"\n{len(ARTICULOS) - len(fallos)}/{len(ARTICULOS)} articulos en {CORPUS_DIR}")
    if fallos:
        print("Revisa los titulos fallidos en es.wikipedia.org y corrige la lista.")
    return 0


if __name__ == "__main__":
    sys.exit(main())