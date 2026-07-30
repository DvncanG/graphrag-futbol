"""
Resolucion de entidades en dos fases.

El problema: el LLM extrae "Parma", "Parma FC", "Parma F. C." y "Parma
Football Club" como cuatro nodos distintos. Los caminos del grafo se rompen
y las consultas multi-salto dejan de encontrar cosas que si estan.

Por que NO se automatiza del todo: los detectores de similitud producen
falsos positivos que rompen el grafo en silencio. Casos reales de este
corpus, todos detectados como "duplicados" y todos falsos:

    "Copa de Europa"  dentro de  "Supercopa de Europa"
    "Copa de Italia"  dentro de  "Supercopa de Italia"
    "Liga"            dentro de  "Liga de Campeones"

Son competiciones distintas. Fusionarlas seria un error factual invisible:
el grafo seguiria pareciendo correcto. Por eso el flujo es proponer ->
revisar a mano -> aplicar.

Uso:
    python scripts/resolver_entidades.py --proponer
       Genera data/merges.json con los grupos candidatos.

    (abres data/merges.json y editas: aprobado true/false, y el canonico)

    python scripts/resolver_entidades.py --aplicar
       Fusiona solo los grupos con "aprobado": true

    python scripts/resolver_entidades.py --basura
       Lista nodos sospechosos de ser ruido, para borrarlos aparte.
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv
from langchain_neo4j import Neo4jGraph

load_dotenv()

SALIDA = Path(__file__).parent.parent / "data" / "merges.json"

# Umbral de distancia Jaro-Winkler. OJO: es distancia, no similitud.
# 0 = identicas, 1 = totalmente distintas. Cuanto MENOR, mas se parecen.
UMBRAL_DISTANCIA = 0.15


def conectar() -> Neo4jGraph:
    return Neo4jGraph(
        url=os.getenv("NEO4J_URI", "bolt://127.0.0.1:7687"),
        username=os.getenv("NEO4J_USERNAME", "neo4j"),
        password=os.getenv("NEO4J_PASSWORD", "graphrag2026"),
        refresh_schema=False,
    )


# --- Deteccion de candidatos ---------------------------------------------

# Detector 1: cadenas muy parecidas. Caza "Real Madrid" / "Real Madrid CF".
# Se le escapan los apellidos sueltos: Jaro-Winkler premia prefijos comunes
# y "Ancelotti" vs "Carlo Ancelotti" empiezan distinto.
CYPHER_SIMILITUD = """
MATCH (a), (b)
WHERE NOT a:Document AND NOT b:Document AND elementId(a) < elementId(b)
WITH a, b,
     [l IN labels(a) WHERE l <> '__Entity__'][0] AS tipo_a,
     [l IN labels(b) WHERE l <> '__Entity__'][0] AS tipo_b,
     apoc.text.jaroWinklerDistance(toLower(a.id), toLower(b.id)) AS dist
WHERE tipo_a = tipo_b AND tipo_a IS NOT NULL AND dist < $umbral
RETURN tipo_a AS tipo, a.id AS uno, b.id AS otro, dist AS score
"""

# Detector 2: una entidad contenida en otra, pero SOLO como prefijo y en
# frontera de palabra. El prefijo elimina el caso "Supercopa" (que contiene
# "copa" pero no empieza por ella). La frontera de palabra evita emparejar
# "Milan" con "Milanesa". Aun asi deja pasar "Liga" / "Liga de Campeones",
# que es justo por lo que hace falta revision humana.
CYPHER_PREFIJO = """
MATCH (a), (b)
WHERE NOT a:Document AND NOT b:Document
  AND elementId(a) <> elementId(b)
  AND size(a.id) < size(b.id)
  AND toLower(b.id) STARTS WITH (toLower(a.id) + ' ')
WITH a, b,
     [l IN labels(a) WHERE l <> '__Entity__'][0] AS tipo_a,
     [l IN labels(b) WHERE l <> '__Entity__'][0] AS tipo_b
WHERE tipo_a = tipo_b AND tipo_a IS NOT NULL
RETURN tipo_a AS tipo, a.id AS uno, b.id AS otro, 0.0 AS score
"""

# Detector 3: apellido suelto dentro de un nombre completo. Solo para
# personas, donde el patron "Ancelotti" / "Carlo Ancelotti" es habitual.
CYPHER_APELLIDO = """
MATCH (a), (b)
WHERE (a:Entrenador OR a:Jugador) AND (b:Entrenador OR b:Jugador)
  AND elementId(a) <> elementId(b)
  AND size(a.id) < size(b.id)
  AND NOT a.id CONTAINS ' '
  AND toLower(b.id) ENDS WITH (' ' + toLower(a.id))
WITH a, b,
     [l IN labels(a) WHERE l <> '__Entity__'][0] AS tipo_a,
     [l IN labels(b) WHERE l <> '__Entity__'][0] AS tipo_b
WHERE tipo_a = tipo_b
RETURN tipo_a AS tipo, a.id AS uno, b.id AS otro, 0.0 AS score
"""


class UnionFind:
    """Agrupa pares en clusters. Necesario porque Parma aparece en varios
    pares sueltos (Parma-Parma FC, Parma-Parma F. C.) y queremos un unico
    grupo con las cuatro variantes, no tres fusiones encadenadas."""

    def __init__(self) -> None:
        self.padre: dict[str, str] = {}

    def buscar(self, x: str) -> str:
        self.padre.setdefault(x, x)
        while self.padre[x] != x:
            self.padre[x] = self.padre[self.padre[x]]
            x = self.padre[x]
        return x

    def unir(self, a: str, b: str) -> None:
        ra, rb = self.buscar(a), self.buscar(b)
        if ra != rb:
            self.padre[rb] = ra


def proponer(grafo: Neo4jGraph) -> int:
    pares = []
    for nombre, cypher, params in [
        ("similitud", CYPHER_SIMILITUD, {"umbral": UMBRAL_DISTANCIA}),
        ("prefijo", CYPHER_PREFIJO, {}),
        ("apellido", CYPHER_APELLIDO, {}),
    ]:
        filas = grafo.query(cypher, params)
        print(f"  detector '{nombre}': {len(filas)} pares")
        for fila in filas:
            pares.append(fila)

    if not pares:
        print("No se han encontrado candidatos.")
        return 0

    uf = UnionFind()
    tipos: dict[str, str] = {}
    for p in pares:
        uf.unir(p["uno"], p["otro"])
        tipos[p["uno"]] = p["tipo"]
        tipos[p["otro"]] = p["tipo"]

    clusters: dict[str, list[str]] = defaultdict(list)
    for entidad in tipos:
        clusters[uf.buscar(entidad)].append(entidad)

    grupos = []
    for miembros in clusters.values():
        if len(miembros) < 2:
            continue
        miembros = sorted(miembros, key=len, reverse=True)
        grupos.append(
            {
                "tipo": tipos[miembros[0]],
                # Sugerencia: el nombre mas largo suele ser el mas completo.
                # Revisalo, no siempre acierta.
                "canonico": miembros[0],
                "variantes": miembros,
                # Por defecto FALSE: nada se fusiona sin que tu lo apruebes.
                "aprobado": False,
            }
        )

    grupos.sort(key=lambda g: (g["tipo"] or "", g["canonico"]))
    SALIDA.write_text(json.dumps(grupos, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n{len(grupos)} grupos candidatos -> {SALIDA}")
    print("\nRevisalos uno a uno:")
    for g in grupos:
        print(f"  [{g['tipo']}] {' | '.join(g['variantes'])}")
    print("\nPon \"aprobado\": true SOLO en los que sean el mismo concepto.")
    print("Cuidado con competiciones: Copa de Europa != Supercopa de Europa.")
    return 0


def aplicar(grafo: Neo4jGraph) -> int:
    if not SALIDA.exists():
        print(f"No existe {SALIDA}. Ejecuta primero --proponer.")
        return 1

    grupos = json.loads(SALIDA.read_text(encoding="utf-8"))
    aprobados = [g for g in grupos if g.get("aprobado")]

    if not aprobados:
        print("Ningun grupo aprobado. Edita el JSON y pon \"aprobado\": true.")
        return 0

    print(f"Fusionando {len(aprobados)} de {len(grupos)} grupos...\n")
    for g in aprobados:
        # El canonico va PRIMERO: apoc.refactor.mergeNodes conserva las
        # propiedades del primer nodo de la lista y descarta las del resto.
        orden = [g["canonico"]] + [v for v in g["variantes"] if v != g["canonico"]]
        resultado = grafo.query(
            """
            UNWIND $orden AS nombre
            MATCH (n {id: nombre})
            WITH collect(n) AS nodos
            WHERE size(nodos) > 1
            CALL apoc.refactor.mergeNodes(
                nodos, {properties: 'discard', mergeRels: true}
            ) YIELD node
            RETURN node.id AS resultante
            """,
            {"orden": orden},
        )
        if resultado:
            print(f"  [{g['tipo']}] {len(orden)} nodos -> {g['canonico']}")
        else:
            print(f"  [{g['tipo']}] {g['canonico']}: nada que fusionar (ya hecho?)")

    total = grafo.query(
        "MATCH (n) WHERE NOT n:Document RETURN count(*) AS n"
    )[0]["n"]
    print(f"\nQuedan {total} entidades en el grafo.")
    return 0


def basura(grafo: Neo4jGraph) -> int:
    """Nodos sospechosos de ser ruido: muy genericos o casi sin conexiones."""
    filas = grafo.query(
        """
        MATCH (n) WHERE NOT n:Document
        OPTIONAL MATCH (n)-[r]-() WHERE type(r) <> 'MENTIONS'
        WITH n, count(r) AS grado,
             [l IN labels(n) WHERE l <> '__Entity__'][0] AS tipo
        WHERE grado <= 1 OR size(n.id) <= 4 OR n.id =~ '(?i)^(el|la|los|las) .*'
        RETURN tipo, n.id AS entidad, grado
        ORDER BY grado, entidad
        """
    )
    print(f"{len(filas)} nodos sospechosos de ser ruido:\n")
    for f in filas:
        print(f"  [{f['tipo']}] {f['entidad']:45} grado={f['grado']}")
    print("\nPara borrar uno concreto, en el Browser:")
    print("  MATCH (n {id: 'nombre exacto'}) DETACH DELETE n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--proponer", action="store_true")
    parser.add_argument("--aplicar", action="store_true")
    parser.add_argument("--basura", action="store_true")
    args = parser.parse_args()

    grafo = conectar()
    if args.proponer:
        return proponer(grafo)
    if args.aplicar:
        return aplicar(grafo)
    if args.basura:
        return basura(grafo)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())