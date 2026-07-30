"""
Resolucion de entidades, version 2.

POR QUE HAY UNA VERSION 2
La v1 usaba distancia Jaro-Winkler + union-find y produjo estos grupos:

    Real Madrid | Real Madrid CF | Real Sociedad
    Manchester United | Manchester City
    Carlo Ancelotti | Carlo Mazzone | Ancelotti
    Marcelo Bielsa | Rafael Bielsa          (son hermanos, no la misma persona)

Dos causas:
  1. Jaro-Winkler premia los prefijos comunes. Esa propiedad ayuda con
     "Real Madrid"/"Real Madrid CF" y es letal con "Real Sociedad".
  2. Union-find encadena: un solo par falso contamina el cluster entero.

ENFOQUE DE LA V2
  - Normalizar y comparar por igualdad exacta, en vez de medir parecido.
    Solo aqui se agrupa transitivamente, porque la igualdad si es una
    relacion de equivalencia.
  - Los detectores debiles (prefijo, apellido, erratas) proponen pares
    sueltos, nunca cadenas.
  - Ambiguedad: si una forma corta encaja con varias largas, se descarta.
    "Bielsa" encaja con Marcelo y con Rafael -> no se propone nada.

Uso:
    python scripts/resolver_entidades2.py --limpiar     (ver ruido)
    python scripts/resolver_entidades2.py --limpiar --confirmar
    python scripts/resolver_entidades2.py --proponer
    (editar data/merges.json)
    python scripts/resolver_entidades2.py --aplicar
"""

import argparse
import json
import os
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv
from langchain_neo4j import Neo4jGraph

load_dotenv()

SALIDA = Path(__file__).parent.parent / "data" / "merges.json"

# Formas juridicas y abreviaturas que no distinguen un club de otro.
# OJO con lo que NO esta aqui: 'b' no se quita, porque "Barcelona B" es el
# filial y es una entidad legitimamente distinta de "Barcelona".
RUIDO_CLUB = {
    "a", "c", "f", "d", "r", "s", "u", "ac", "fc", "cf", "cd", "ud", "sc",
    "sl", "rcd", "afc", "acf", "usc", "football", "club", "calcio",
}

# Nodos que no son entidades: nombres de tipo, articulos, marcadores de
# fallo del modelo. Comparado en minusculas y sin acentos.
BASURA_EXACTA = {
    "el", "la", "los", "las", "un", "una", "club", "competicion", "entrenador",
    "entrenadores", "jugador", "jugadores", "seleccion", "liga", "ligas",
    "copa", "copas", "campeonato", "campeonatos", "titulos", "unknown", "n/a",
    "na", "none", "null", "actualmente", "se convirtio", "su padre",
    "temporada", "equipo", "equipos", "partido", "partidos",
}


def sin_acentos(texto: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", texto)
        if unicodedata.category(c) != "Mn"
    )


def base(texto: str) -> str:
    """Minusculas, sin acentos, sin puntuacion, espacios colapsados."""
    s = sin_acentos(texto.lower())
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def clave(nombre: str, tipo: str) -> str:
    """Clave de agrupacion. Para clubes quita ademas la forma juridica:
    'Real Madrid C. F.' y 'Real Madrid' -> 'real madrid'
    'Real Sociedad'                     -> 'real sociedad'  (no colisiona)
    """
    s = base(nombre)
    if tipo == "Club":
        s = " ".join(t for t in s.split() if t not in RUIDO_CLUB)
    return s.strip()


def distancia_edicion(a: str, b: str, tope: int = 2) -> int:
    """Levenshtein con corte temprano. Para cazar erratas del modelo:
    'Johann Cruyff' vs 'Johan Cruyff'."""
    if abs(len(a) - len(b)) > tope:
        return tope + 1
    previa = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        actual = [i]
        for j, cb in enumerate(b, 1):
            actual.append(
                min(previa[j] + 1, actual[j - 1] + 1, previa[j - 1] + (ca != cb))
            )
        if min(actual) > tope:
            return tope + 1
        previa = actual
    return previa[-1]


def conectar() -> Neo4jGraph:
    return Neo4jGraph(
        url=os.getenv("NEO4J_URI", "bolt://127.0.0.1:7687"),
        username=os.getenv("NEO4J_USERNAME", "neo4j"),
        password=os.getenv("NEO4J_PASSWORD", "graphrag2026"),
        refresh_schema=False,
    )


def leer_entidades(grafo: Neo4jGraph) -> list[dict]:
    return grafo.query(
        """
        MATCH (n) WHERE NOT n:Document
        OPTIONAL MATCH (n)-[r]-() WHERE type(r) <> 'MENTIONS'
        RETURN n.id AS nombre,
               [l IN labels(n) WHERE l <> '__Entity__'][0] AS tipo,
               count(r) AS grado
        """
    )


# --- Limpieza -------------------------------------------------------------

def es_basura(nombre: str) -> str | None:
    """Devuelve el motivo si el nodo es ruido, o None si parece legitimo."""
    b = base(nombre)
    if not b:
        return "vacio"
    if b in BASURA_EXACTA:
        return "termino generico"
    if len(nombre) > 55:
        return "frase, no entidad"
    if re.match(r"^\d+\s", nombre):
        return "empieza por cifra (recuento de titulos)"
    # 9 y no 7: "Copa Mundial de Clubes de la FIFA 2019-20" son 8 palabras
    # y es una competicion legitima.
    if len(nombre.split()) > 9:
        return "demasiadas palabras"
    # Un partido concreto no es una competicion.
    if re.match(r"(?i)^(partido|partidos|generacion|generación)\b", nombre):
        return "evento o descripcion, no entidad"
    if re.search(r"(?i)\b(subcampeon|puesto|posicion|rendimiento)\b", nombre):
        return "resultado, no competicion"
    return None


def limpiar(grafo: Neo4jGraph, confirmar: bool) -> int:
    entidades = leer_entidades(grafo)
    sospechosos = [
        (e, motivo) for e in entidades if (motivo := es_basura(e["nombre"]))
    ]

    if not sospechosos:
        print("Nada que limpiar.")
        return 0

    print(f"{len(sospechosos)} nodos detectados como ruido:\n")
    for e, motivo in sorted(sospechosos, key=lambda x: x[1]):
        print(f"  [{e['tipo']}] {e['nombre'][:50]:52} ({motivo})")

    if not confirmar:
        print(f"\nEsto es solo una previsualizacion: no se ha borrado nada.")
        print("Revisa la lista y, si estas de acuerdo, repite con --confirmar")
        return 0

    nombres = [e["nombre"] for e, _ in sospechosos]
    grafo.query(
        "UNWIND $nombres AS n MATCH (x {id: n}) DETACH DELETE x",
        {"nombres": nombres},
    )
    print(f"\n{len(nombres)} nodos borrados.")
    return 0


# --- Propuesta de fusiones ------------------------------------------------

def proponer(grafo: Neo4jGraph) -> int:
    entidades = [e for e in leer_entidades(grafo) if e["tipo"]]
    por_nombre = {e["nombre"]: e for e in entidades}

    grupos: list[dict] = []
    ya_agrupado: set[str] = set()

    # Detector 1: clave normalizada identica. Es el unico donde agrupamos
    # transitivamente, porque la igualdad si es una relacion de equivalencia.
    por_clave: dict[tuple[str, str], list[str]] = defaultdict(list)
    for e in entidades:
        por_clave[(e["tipo"], clave(e["nombre"], e["tipo"]))].append(e["nombre"])

    for (tipo, _), miembros in por_clave.items():
        if len(miembros) < 2:
            continue
        miembros = sorted(miembros, key=lambda n: (-por_nombre[n]["grado"], -len(n)))
        grupos.append(
            {
                "detector": "normalizacion",
                "tipo": tipo,
                "canonico": miembros[0],
                "variantes": miembros,
                "aprobado": False,
                "nota": "alta confianza: mismo nombre tras normalizar",
            }
        )
        ya_agrupado.update(miembros)

    # Detectores debiles: proponen PARES, nunca cadenas, y solo cuando la
    # correspondencia es unica.
    def emitir_pares(nombre_det: str, pares: list[tuple[str, str]], nota: str):
        # Cuenta cuantas veces aparece cada forma corta. Si aparece mas de
        # una vez, es ambigua y se descarta entera.
        conteo: dict[str, int] = defaultdict(int)
        for corto, _ in pares:
            conteo[corto] += 1

        for corto, largo in pares:
            if conteo[corto] > 1:
                continue  # ambiguo: "Bielsa" encaja con Marcelo y con Rafael
            if corto in ya_agrupado or largo in ya_agrupado:
                continue
            grupos.append(
                {
                    "detector": nombre_det,
                    "tipo": por_nombre[largo]["tipo"],
                    "canonico": largo,
                    "variantes": [largo, corto],
                    "aprobado": False,
                    "nota": nota,
                }
            )
            ya_agrupado.update([corto, largo])

        descartados = sorted({c for c, n in conteo.items() if n > 1})
        if descartados:
            print(f"  {nombre_det}: {len(descartados)} descartados por ambiguos "
                  f"-> {', '.join(descartados[:6])}"
                  f"{'...' if len(descartados) > 6 else ''}")

    # Detector 2: forma corta que es prefijo exacto de una larga.
    # 'Ajax' dentro de 'Ajax de Amsterdam'. El espacio obliga a frontera de
    # palabra, y el prefijo descarta 'Copa de Europa' vs 'Supercopa de Europa'.
    pares_prefijo = []
    for corto in entidades:
        for largo in entidades:
            if corto["tipo"] != largo["tipo"] or corto["nombre"] == largo["nombre"]:
                continue
            bc, bl = base(corto["nombre"]), base(largo["nombre"])
            if bc and len(bc) < len(bl) and bl.startswith(bc + " "):
                pares_prefijo.append((corto["nombre"], largo["nombre"]))

    # Detector 3: apellido suelto al final de un nombre completo.
    # Jaro-Winkler no llega aqui porque premia prefijos comunes y
    # 'Ancelotti' vs 'Carlo Ancelotti' empiezan distinto.
    pares_apellido = []
    for corto in entidades:
        if corto["tipo"] not in ("Entrenador", "Jugador") or " " in corto["nombre"]:
            continue
        for largo in entidades:
            if largo["tipo"] != corto["tipo"] or " " not in largo["nombre"]:
                continue
            if base(largo["nombre"]).split()[-1] == base(corto["nombre"]):
                pares_apellido.append((corto["nombre"], largo["nombre"]))

    # Detector 4: erratas de una o dos letras ('Johann' vs 'Johan').
    pares_errata = []
    vistos: set[tuple[str, str]] = set()
    for a in entidades:
        for b in entidades:
            if a["tipo"] != b["tipo"] or a["nombre"] >= b["nombre"]:
                continue
            ba, bb = base(a["nombre"]), base(b["nombre"])
            if len(ba) < 6 or abs(len(ba) - len(bb)) > 2:
                continue
            if 0 < distancia_edicion(ba, bb) <= 2:
                par = (a["nombre"], b["nombre"])
                if par not in vistos:
                    vistos.add(par)
                    pares_errata.append(par)

    print("Detectores debiles:")
    emitir_pares("prefijo", pares_prefijo, "revisar: forma corta contenida")
    emitir_pares("apellido", pares_apellido, "revisar: apellido suelto")
    emitir_pares("errata", pares_errata, "revisar: difieren en 1-2 letras")

    grupos.sort(key=lambda g: (g["detector"], g["tipo"], g["canonico"]))
    SALIDA.write_text(json.dumps(grupos, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n{len(grupos)} grupos -> {SALIDA}\n")
    det_actual = None
    for g in grupos:
        if g["detector"] != det_actual:
            det_actual = g["detector"]
            print(f"\n--- {det_actual} ---")
        print(f"  [{g['tipo']}] {' | '.join(g['variantes'])}")

    print("\nLos de 'normalizacion' suelen ser seguros; revisalos igualmente.")
    print("Los demas son propuestas: aprueba solo lo que sepas que es cierto.")
    return 0


def aplicar(grafo: Neo4jGraph) -> int:
    if not SALIDA.exists():
        print(f"No existe {SALIDA}. Ejecuta primero --proponer.")
        return 1

    grupos = json.loads(SALIDA.read_text(encoding="utf-8"))
    aprobados = [g for g in grupos if g.get("aprobado")]
    if not aprobados:
        print('Ningun grupo aprobado. Edita el JSON y pon "aprobado": true.')
        return 0

    print(f"Fusionando {len(aprobados)} de {len(grupos)} grupos...\n")
    for g in aprobados:
        # El canonico primero: mergeNodes conserva las propiedades del
        # primer nodo de la lista y descarta las del resto.
        orden = [g["canonico"]] + [v for v in g["variantes"] if v != g["canonico"]]
        res = grafo.query(
            """
            UNWIND $orden AS nombre
            MATCH (n {id: nombre})
            WITH collect(n) AS nodos
            WHERE size(nodos) > 1
            CALL apoc.refactor.mergeNodes(
                nodos, {properties: 'discard', mergeRels: true}
            ) YIELD node
            RETURN node.id AS r
            """,
            {"orden": orden},
        )
        estado = "ok" if res else "sin cambios"
        print(f"  [{g['tipo']}] {g['canonico']:40} {len(orden)} nodos  {estado}")

    total = grafo.query("MATCH (n) WHERE NOT n:Document RETURN count(*) AS n")[0]["n"]
    print(f"\nQuedan {total} entidades.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--limpiar", action="store_true")
    p.add_argument("--confirmar", action="store_true", help="con --limpiar, borra")
    p.add_argument("--proponer", action="store_true")
    p.add_argument("--aplicar", action="store_true")
    args = p.parse_args()

    grafo = conectar()
    if args.limpiar:
        return limpiar(grafo, args.confirmar)
    if args.proponer:
        return proponer(grafo)
    if args.aplicar:
        return aplicar(grafo)
    p.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())