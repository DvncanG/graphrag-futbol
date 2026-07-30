"""
Normalizacion de nombres de entidad.

La base de toda la resolucion de entidades. Comparar cadenas crudas haria
que "Jose Mourinho" y "José Mourinho" fuesen dos entidades distintas, y el
grafo quedaria partido en trozos que no conectan.

Lo usan dedupe.py (detectar duplicados), evaluate.py (comparar contra la
referencia de Wikidata) y query.py (localizar en la pregunta del usuario las
entidades que existen en el grafo).
"""

import re
import unicodedata


def strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    )


def normalize_text(text: str) -> str:
    """Minusculas, sin acentos, sin puntuacion, espacios colapsados.

    'Real Madrid C. F.' -> 'real madrid c f'
    'José Mourinho'     -> 'jose mourinho'
    """
    s = strip_accents(text.lower())
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()