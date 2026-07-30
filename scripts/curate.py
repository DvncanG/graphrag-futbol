"""
Correcciones que ninguna heuristica puede deducir.

POR QUE EXISTE ESTE FICHERO
Durante la revision del grafo se hicieron a mano, desde el Browser, una
serie de fusiones y borrados. Ese trabajo vivia solo en el historial de
consultas: reingestar lo borraba todo y habia que repetirlo.

Aqui queda en codigo. Eso convierte el pipeline en reproducible: se puede
reingestar cuantas veces haga falta y recuperar el mismo grafo depurado
ejecutando una secuencia de scripts.

Tambien aplica las decisiones de revision sobre data/merges.json (--approve),
porque son lo mismo: conocimiento humano que el pipeline no puede deducir.

Es idempotente: si un nodo ya no existe o ya esta fusionado, lo salta sin
error. Se puede ejecutar las veces que se quiera.

Uso:
    python scripts/curate.py --approve  # marca merges.json revisado
    python scripts/curate.py            # previsualiza correcciones
    python scripts/curate.py --apply
"""

import argparse
import json
import re
import sys
from pathlib import Path

from clients import connect
from dotenv import load_dotenv
from langchain_neo4j import Neo4jGraph

load_dotenv()

MERGES_FILE = Path(__file__).parent.parent / "data" / "merges.json"


APPROVE = {
    "Johan Cruyff",  # apellido: Cruyff -> Johan Cruyff
    "Ajax de Ámsterdam",  # prefijo:  Ajax
    "Atlético de Madrid",  # prefijo:  Atlético
    "Liga española de fútbol",  # prefijo:  Liga española
}

# Casos que ningun detector de cadenas puede encontrar, porque el parecido
# no esta en las letras sino en el conocimiento del dominio.
MANUAL_GROUPS = [
    {
        "detector": "manual",
        "tipo": "Entrenador",
        "canonico": "Marcelo Bielsa",
        "variantes": ["Marcelo Bielsa", "Marcelo Alberto Bielsa Caldera"],
        "aprobado": True,
        "nota": "nombre completo con segundo apellido",
    },
    {
        "detector": "manual",
        "tipo": "Entrenador",
        "canonico": "José Mourinho",
        "variantes": [
            "José Mourinho",
            "José Mário dos Santos Mourinho Félix",
            "Mourinho",
        ],
        "aprobado": True,
        "nota": "nombre portugues completo: acaba en Felix, no en Mourinho",
    },
    {
        "detector": "manual",
        "tipo": "Entrenador",
        "canonico": "Pep Guardiola",
        "variantes": ["Pep Guardiola", "Josep Guardiola", "Guardiola"],
        "aprobado": True,
        "nota": "Pep es hipocoristico de Josep: cero parecido de cadena",
    },
]

# Grupos que los detectores proponen pero que NO deben fusionarse.
# Se listan para dejar constancia de la decision, no solo del olvido.
REJECTED = {
    "Eintracht Fráncfort II": "el II es el filial, otro equipo",
    "Recopa de Europa": "distinta de la Copa de Europa",
    "Ronaldo": "Rivaldo es otro jugador",
    "Copa Intercontinental de la FIFA 2024": "distinta de la historica",
    "Liga Europa Conferencia": "distinta de la Liga Europa",
    "Campeonato Europeo de Fútbol (UEFA Euro) 2012": "ediciones distintas",
    "Copa América 1993": "ediciones distintas",
    "Copa Confederaciones 2013": "ediciones distintas",
    "Liga de las temporadas 1998-1999": "temporadas distintas",
    "UEFA Champions League 2019": "ediciones distintas",
    "Copa de Francia 2021": "edicion concreta, no el torneo",
    "Copa de la UEFA 2005-06": "edicion concreta, no el torneo",
    "Premier League 2019-20": "edicion concreta, no el torneo",
    "Supercopa de Europa 2019-20": "edicion concreta, no el torneo",
    "Supercopa de Francia 2021": "edicion concreta, no el torneo",
}


def approve() -> int:
    """Marca en merges.json los grupos revisados como correctos."""
    if not MERGES_FILE.exists():
        print(f"No existe {MERGES_FILE}. Ejecuta antes dedupe.py --propose")
        return 1
    grupos = json.loads(MERGES_FILE.read_text(encoding="utf-8"))

    n = 0
    for gr in grupos:
        # Los de 'normalizacion' se aprueban en render_block: la match_key normalizada
        # es igualdad exacta, no parecido, y en la revision manual no dio ni
        # un falso positivo. Identificarlos por 'canonico' era fragil, porque
        # ese nombre lo elige el propio script y cambia entre ingestas.
        ok = gr["detector"] == "normalizacion" or gr["canonico"] in APPROVE
        gr["aprobado"] = ok
        n += ok

    existentes = {gr["canonico"] for gr in grupos if gr["detector"] == "manual"}
    nuevos = [m for m in MANUAL_GROUPS if m["canonico"] not in existentes]
    grupos.extend(nuevos)

    MERGES_FILE.write_text(
        json.dumps(grupos, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        f"{n} grupos aprobados, {len(nuevos)} manuales anadidos, {len(grupos)} en total"
    )
    print("Siguiente: python scripts/dedupe.py --apply")
    return 0


# Alias que ninguna metrica de cadenas puede relacionar: siglas, traducciones
# entre idiomas, hipocoristicos. Formato: canonico -> variantes a absorber.
ALIASES = {
    "FC Barcelona": ["Fútbol Club Barcelona", "F. C. Barcelona", "Barcelona"],
    "Paris Saint-Germain": ["PSG"],
    "Frank Rijkaard": ["Rijkaard"],
    # Duplicados destapados al comparar el grafo contra Wikidata.
    "Selección de Uruguay": ["Uruguay"],
    "Reggiana": ["A. C. Reggiana 1919"],
    # El modelo pega gentilicios al nombre del club: "AZ Alkmaar neerlandés",
    # "Antiguoko donostiarra". El detector de prefijo los propone, pero elige
    # como canonico el nombre LARGO, que es justo el incorrecto. Por eso van
    # aqui: el alias fija ademas cual es el nombre bueno.
    "AZ Alkmaar": ["AZ Alkmaar neerlandés"],
    "Antiguoko": ["Antiguoko donostiarra"],
    "Girondins de Burdeos": ["Girondins de Bordeaux"],
    "Mainz 05": ["Maguncia", "FSV Maguncia 05", "FSV Mainz 05"],
    "Liga de Campeones de la UEFA": [
        "Liga de Campeones",
        "Champions League",
        "Champions",
        "Copa de Europa",
    ],
    "Copa de la UEFA": ["Liga Europa", "Europa League"],
    "Selección Argentina": ["Argentina"],
    "Selección de fútbol de Italia": ["Selección italiana",
                                      "Selección nacional de Italia"],
    "R. C. D. Espanyol": ["Real Club Deportivo Espanyol", "Espanyol"],
    "Newell's Old Boys": ["Newell's Old Boys de Rosario"],
    "Louis van Gaal": ["Van Gaal", "van Gaal", "De Van Gaal"],
    "Pep Guardiola": ["Josep Guardiola", "Guardiola", "Pep"],
    "José Mourinho": [
        "José Mário dos Santos Mourinho Félix",
        "José Manuel Mourinho Félix",
        "Mourinho",
    ],
    "Marcelo Bielsa": ["Marcelo Alberto Bielsa Caldera", "Bielsa"],
    "Johan Cruyff": ["Johann Cruyff", "Cruyff"],
    "Xavi Hernández": ["Xavi"],
    "Mikel Arteta": ["Mikel Arteta Amatriain", "Arteta"],
    # Klopp y Simeone: la comprobacion de ambiguedad los bloquea porque
    # existen Norbert Klopp (padre) y Carmelo Simeone (padre). En ESTE
    # corpus, el apellido suelto se refiere siempre al hijo.
    "Jürgen Klopp": ["Jürgen Norbert Klopp", "Norbert Klopp", "Klopp"],
    "Diego Simeone": ["Diego Pablo Simeone", "Simeone"],
    "Mauricio Pochettino": ["Mauricio Roberto Pochettino", "Pochettino"],
    # Traducciones de topónimos: ninguna metrica de cadenas las relaciona.
    "Eintracht Fráncfort": ["Eintracht Frankfurt", "Eintracht Francfort"],
    "Bayern de Múnich": [
        "Bayern Munich",
        "Bayern de Munich",
        "FC Bayern de Múnich",
        "Bayern Múnich",
    ],
    "Milan": ["AC Milan", "A. C. Milan", "Milán", "AC Milán"],
    "Inter de Milán": ["Inter de Milan", "Internazionale", "Inter"],
    "Oporto": ["Porto", "FC Porto"],
    "Carlo Ancelotti": ["Ancelotti"],
    "Arrigo Sacchi": ["Sacchi"],
}

# Nodos que no son entidades y que las reglas automaticas no cazan.
DELETE_NODES = [
    "N/A",
    "unknown",
    "El",
    "Club",
    "Selección",
    "Competicion",
    "Premio Konex Diploma al Mérito 2010",
    "Entrenador sudamericano del año 2009",
    "semifinales de la Liga de Campeones",
    "subcampeones de la Premier League",
    "Serie A 1996-97 subcampeonato",
    # Fragmentos de frase que el modelo extrajo como si fueran entidades.
    # Forman islas desconectadas en el grafo.
    "Se estrenó",
    "se estrenó",
    "pasó",
    "Pasó",
    "Trayectoria",
    "trayectoria",
    "Técnico neerlandés",
    "técnico neerlandés",
    "Tecnico neerlandes",
]

# Relaciones extraidas mal, verificadas una a una contra la realidad.
# (persona, tipo_relacion, entidad, motivo)
DELETE_RELS = [
    ("Arrigo Sacchi", "ENTRENO_A", "Real Madrid", "fue director deportivo"),
    ("Arrigo Sacchi", "ENTRENO_A", "ACF Fiorentina", "nunca dirigio al Fiorentina"),
    ("Jürgen Klopp", "ENTRENO_A", "Selección de Alemania", "nunca dirigio a Alemania"),
    ("Jürgen Klopp", "ENTRENO_A", "D-Juniors de Fráncfort", "ahi jugo de nino"),
    ("Mauricio Pochettino", "JUGO_EN", "Paris Saint-Germain", "lo entreno, no jugo"),
    ("Mauricio Pochettino", "ENTRENO_A", "Manchester City", "nunca dirigio al City"),
    ("Xavi Hernández", "JUGO_EN", "Milan", "nunca jugo en el Milan"),
    ("Xavi Hernández", "ENTRENO_A", "Milan", "nunca entreno al Milan"),
    # Mourinho: confusion de rol. En estos clubes jugo o fue ayudante,
    # nunca primer entrenador. Detectado comparando contra Wikidata.
    ("José Mourinho", "ENTRENO_A", "Rio Ave", "ahi jugo, no entreno"),
    ("José Mourinho", "ENTRENO_A", "Vitória de Setúbal", "ahi jugo, no entreno"),
    ("José Mourinho", "ENTRENO_A", "Sporting de Lisboa", "fue ayudante, no primero"),
    ("José Mourinho", "ENTRENO_A", "FC Barcelona", "fue ayudante de Van Gaal"),
    ("José Mourinho", "ENTRENO_A", "Paris Saint-Germain", "nunca dirigio al PSG"),
    ("Mikel Arteta", "JUGO_EN", "Newcastle", "nunca jugo en el Newcastle"),
]


# Lista blanca: para estas personas, SOLO estos clubes son correctos.
# Se usa cuando un nodo de apellido suelto acumulo relaciones de otros y
# las traspaso al fusionarse.
CLUB_WHITELIST = {
    "Pep Guardiola": [
        "FC Barcelona",
        "Bayern de Múnich",
        "Manchester City",
        "Barcelona B",
    ],
    "Johan Cruyff": ["Ajax de Ámsterdam", "FC Barcelona"],
}


# Anos y temporadas al final del nombre de una competicion.
# "FA Cup 2010-11" -> "FA Cup" ; "Balon de Oro en 2011" -> "Balon de Oro"
YEAR_RE = re.compile(r"\s+(?:en\s+|de\s+|del\s+)?\d{4}(?:\s*[-/]\s*\d{2,4})?$")


def strip_year(nombre: str) -> str:
    previo = None
    while previo != nombre:  # "Serie A 1996-97 2000" -> "Serie A"
        previo = nombre
        nombre = YEAR_RE.sub("", nombre).strip()
    return nombre


def strip_competition_years(g: Neo4jGraph, seco: bool) -> None:
    """Quita anos de los nombres de competicion y fusiona los duplicados.

    POR QUE AQUI Y NO EN EL PROMPT
    Al esquema se le pide explicitamente que no incluya anos, y el modelo
    los incluye igual: un 7B no obedece bien las instrucciones negativas.
    Discutir con el modelo es una via muerta; quitar un ano del final de
    una cadena es una regex, determinista y verificable.
    """
    filas = g.query("MATCH (c:Competicion) RETURN c.id AS id ORDER BY c.id")
    grupos: dict[str, list[str]] = {}
    for f in filas:
        limpio = strip_year(f["id"])
        if limpio:
            grupos.setdefault(limpio, []).append(f["id"])

    cambios = {k: v for k, v in grupos.items() if len(v) > 1 or v[0] != k}
    print(f"\nCompeticiones con ano: {len(cambios)} grupos")
    for canonico, variantes in sorted(cambios.items()):
        print(f"  {canonico}  <-  {', '.join(variantes)}")
        if seco:
            continue
        g.query(
            """
            UNWIND $variantes AS nombre
            MATCH (n:Competicion {id: nombre})
            WITH collect(n) AS nodos
            CALL apoc.refactor.mergeNodes(
                nodos, {properties: 'discard', mergeRels: true}
            ) YIELD node
            SET node.id = $canonico
            RETURN node.id
            """,
            {"variantes": variantes, "canonico": canonico},
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--approve",
        action="store_true",
        help="marca merges.json con las decisiones revisadas",
    )
    args = parser.parse_args()

    if args.approve:
        return approve()

    g = connect()
    seco = not args.apply

    if seco:
        print("PREVISUALIZACION: no se modifica nada. Usa --apply.\n")

    # --- 1. Fusiones de alias ---------------------------------------------
    print("Alias:")
    for canonico, variantes in ALIASES.items():
        presentes = g.query(
            "MATCH (n) WHERE n.id IN $ids RETURN collect(n.id) AS hay",
            {"ids": [canonico] + variantes},
        )[0]["hay"]
        if len(presentes) < 2:
            continue
        print(f"  {canonico}  <-  {', '.join(x for x in presentes if x != canonico)}")
        if seco:
            continue
        # El canonico primero: mergeNodes conserva sus propiedades
        # El primero de la lista debe EXISTIR, o mergeNodes no lo encuentra
        # y las propiedades se toman de otro. Si el canonico no esta en el
        # grafo, arrancamos por la primera variante presente y renombramos
        # despues.
        orden = [x for x in [canonico] + variantes if x in presentes]
        g.query(
            """
            UNWIND $orden AS nombre
            MATCH (n {id: nombre})
            WITH collect(n) AS nodos WHERE size(nodos) > 1
            CALL apoc.refactor.mergeNodes(
                nodos, {properties: 'discard', mergeRels: true}
            ) YIELD node
            // Renombrar es imprescindible: si el canonico no existia tras la
            // reingesta, el nodo fusionado se quedaria con otro nombre y las
            // correcciones de la siguiente vuelta ya no lo encontrarian.
            SET node.id = $canonico
            RETURN node.id
            """,
            {"orden": orden, "canonico": canonico},
        )

    # --- 2. Nodos que no son entidades ------------------------------------
    hay = g.query(
        "MATCH (n) WHERE n.id IN $ids RETURN collect(n.id) AS hay",
        {"ids": DELETE_NODES},
    )[0]["hay"]
    print(f"\nNodos a borrar: {len(hay)}")
    for x in hay:
        print(f"  {x}")
    if hay and not seco:
        g.query("MATCH (n) WHERE n.id IN $ids DETACH DELETE n", {"ids": hay})

    # --- 3. Relaciones falsas ---------------------------------------------
    print("\nRelaciones falsas:")
    for persona, tipo, entidad, motivo in DELETE_RELS:
        existe = g.query(
            f"MATCH ({{id: $p}})-[r:{tipo}]->({{id: $e}}) RETURN count(r) AS n",
            {"p": persona, "e": entidad},
        )[0]["n"]
        if not existe:
            continue
        print(f"  {persona} -{tipo}-> {entidad}  ({motivo})")
        if not seco:
            g.query(
                f"MATCH ({{id: $p}})-[r:{tipo}]->({{id: $e}}) DELETE r",
                {"p": persona, "e": entidad},
            )

    # --- 4. Listas blancas de clubes --------------------------------------
    print("\nClubes fuera de lista blanca:")
    for persona, validos in CLUB_WHITELIST.items():
        sobran = g.query(
            """
            MATCH ({id: $p})-[r:ENTRENO_A]->(c)
            WHERE NOT c.id IN $validos
            RETURN collect(c.id) AS sobran
            """,
            {"p": persona, "validos": validos},
        )[0]["sobran"]
        if not sobran:
            continue
        print(f"  {persona}: {', '.join(sobran)}")
        if not seco:
            g.query(
                """
                MATCH ({id: $p})-[r:ENTRENO_A]->(c)
                WHERE NOT c.id IN $validos
                DELETE r
                """,
                {"p": persona, "validos": validos},
            )

    # --- 5. Anos en nombres de competicion --------------------------------
    strip_competition_years(g, seco)

    total = g.query("MATCH (n) WHERE NOT n:Document RETURN count(*) AS n")[0]["n"]
    print(f"\n{total} entidades en el grafo")
    if seco:
        print("Nada modificado. Repite con --apply para ejecutar.")
    return 0


if __name__ == "__main__":
    sys.exit(main())