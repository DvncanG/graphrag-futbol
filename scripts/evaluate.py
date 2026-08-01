"""
Audita el grafo contra datos verificados, y descarga esos datos.

DE DONDE SALE LA REFERENCIA
De Wikidata, no de Wikipedia. Wikipedia es prosa: hay que interpretarla, y
ahi es donde el LLM se equivoca. Wikidata es la misma informacion en forma
estructurada y curada por personas:

    P54    miembro de equipo deportivo    (etapa como jugador)
    P6087  entrenador de equipo deportivo (etapa como entrenador)

Eso es lo que hace que la metrica signifique algo: se compara la extraccion
contra una fuente independiente y citable, no contra el recuerdo de nadie.

Compara lo que el LLM extrajo con lo que sabemos que es cierto y produce
tres listas:

  CORRECTA      esta en el grafo y en la referencia
  SOBRA         esta en el grafo y NO en la referencia -> probable alucinacion
  FALTA         esta en la referencia y NO en el grafo -> no se extrajo

Con eso sale una metrica honesta de precision y cobertura, que es lo que
distingue un proyecto medido de uno que solo "parece" funcionar.

La comparacion se hace sobre nombres normalizados (minusculas, sin acentos,
sin formas juridicas), asi que "Atlético de Madrid" y "Club Atletico de
Madrid" cuentan como el mismo club.

Uso:
    python scripts/evaluate.py --review         # ver que trae Wikidata
    python scripts/evaluate.py --download       # generar verdad.py
    python scripts/evaluate.py                   # informe, no toca nada
    python scripts/evaluate.py --delete-extra   # borra las ENTRENO_A falsas
    python scripts/evaluate.py --add-missing   # crea las ENTRENO_A ausentes
"""

import argparse
import json
import sys
import time
from pathlib import Path

import httpx
from clients import connect
from dotenv import load_dotenv
from names import normalize_text

load_dotenv()

# ======================================================================
# Descarga de la referencia desde Wikidata
# ======================================================================

GROUND_TRUTH_FILE = Path(__file__).parent / "ground_truth.py"
SEARCH_API = "https://www.wikidata.org/w/api.php"
SPARQL = "https://query.wikidata.org/sparql"

# Wikidata exige un user-agent identificable en su politica de uso.
HEADERS = {"User-Agent": "graphrag-futbol/0.1 (https://github.com/DvncanG)"}

PEOPLE = [
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
    "Xavi Hernández",
    "Roberto De Zerbi",
]

SPARQL_QUERY = """
SELECT ?tipo ?equipoLabel ?inicio ?fin WHERE {
  {
    wd:%s p:P54 ?st .
    ?st ps:P54 ?equipo .
    BIND("JUGO_EN" AS ?tipo)
  } UNION {
    wd:%s p:P6087 ?st .
    ?st ps:P6087 ?equipo .
    BIND("ENTRENO_A" AS ?tipo)
  }
  OPTIONAL { ?st pq:P580 ?inicio }
  OPTIONAL { ?st pq:P582 ?fin }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "es,en". }
}
"""


def find_qid(cliente: httpx.Client, nombre: str) -> str | None:
    """Q-id de la persona. Se filtra por descripcion para no confundir
    homonimos: hay varios 'Xavi' y varios 'Simeone' en Wikidata."""
    r = cliente.get(
        SEARCH_API,
        params={
            "action": "wbsearchentities",
            "search": nombre,
            "language": "es",
            "uselang": "es",
            "format": "json",
            "type": "item",
            "limit": 5,
        },
    )
    r.raise_for_status()
    resultados = r.json().get("search", [])
    if not resultados:
        return None

    pistas = ("futbol", "fútbol", "entrenador", "football", "soccer")
    for item in resultados:
        desc = (item.get("description") or "").lower()
        if any(p in desc for p in pistas):
            return item["id"]
    return resultados[0]["id"]  # sin descripcion util, el primero


def fetch_career(cliente: httpx.Client, qid: str) -> list[dict]:
    r = cliente.get(
        SPARQL,
        params={"query": SPARQL_QUERY % (qid, qid), "format": "json"},
        timeout=60,
    )
    r.raise_for_status()
    filas = []
    for b in r.json()["results"]["bindings"]:
        equipo = b["equipoLabel"]["value"]
        if equipo.startswith("Q") and equipo[1:].isdigit():
            continue  # sin etiqueta traducida, no aporta
        filas.append(
            {
                "tipo": b["tipo"]["value"],
                "equipo": equipo,
                "inicio": b.get("inicio", {}).get("value", "")[:4],
                "fin": b.get("fin", {}).get("value", "")[:4],
            }
        )
    return filas


FILE_HEADER = '''"""
Datos de referencia: carreras reales, descargadas de Wikidata.

GENERADO AUTOMATICAMENTE por scripts/evaluate.py --download. No editar a mano:
los cambios se pierden en la siguiente descarga.

Fuente: Wikidata, propiedades P54 (miembro de equipo deportivo) y P6087
(entrenador de equipo deportivo). Datos estructurados y curados por
personas, no prosa interpretada por un modelo.

Esto es lo que hace que evaluate.py signifique algo: comparar la extraccion
del LLM contra una referencia independiente y citable, en lugar de contra
el recuerdo de alguien.
"""

'''


def write_ground_truth(datos: dict) -> None:
    def render_block(nombre: str, match_key: str) -> str:
        lineas = [f"{nombre} = {{"]
        for persona, filas in datos.items():
            equipos = sorted({f["equipo"] for f in filas if f["tipo"] == match_key})
            if not equipos:
                continue
            # json.dumps y no comillas a mano: Wikidata devuelve nombres con
            # comillas dentro ('Fútbol Club Barcelona "C"') que romperian el
            # literal de Python.
            lineas.append(f"    {json.dumps(persona, ensure_ascii=False)}: [")
            for e in equipos:
                lineas.append(f"        {json.dumps(e, ensure_ascii=False)},")
            lineas.append("    ],")
        lineas.append("}")
        return "\n".join(lineas)

    GROUND_TRUTH_FILE.write_text(
        FILE_HEADER
        + render_block("ENTRENO_A", "ENTRENO_A")
        + "\n\n"
        + render_block("JUGO_EN", "JUGO_EN")
        + "\n",
        encoding="utf-8",
    )


def download(revisar: bool) -> int:
    datos = {}
    with httpx.Client(headers=HEADERS, timeout=45, follow_redirects=True) as c:
        for nombre in PEOPLE:
            try:
                qid = find_qid(c, nombre)
                if not qid:
                    print(f"  [FAIL] {nombre}: no encontrado en Wikidata")
                    continue
                filas = fetch_career(c, qid)
                datos[nombre] = filas
                ent = sum(1 for f in filas if f["tipo"] == "ENTRENO_A")
                jug = sum(1 for f in filas if f["tipo"] == "JUGO_EN")
                print(f"  [OK]   {nombre:24} {qid:9} {ent:2} entrenó / {jug:2} jugó")
                if revisar:
                    for f in sorted(filas, key=lambda x: (x["tipo"], x["inicio"])):
                        rango = f"{f['inicio']}-{f['fin']}".strip("-")
                        print(f"           {f['tipo']:10} {f['equipo']:34} {rango}")
            except Exception as exc:
                print(f"  [FAIL] {nombre}: {str(exc)[:110]}")
            time.sleep(1)  # cortesia con el endpoint publico

    if not datos:
        print("\nNo se ha descargado nada. Revisa la conexión.")
        return 1
    if revisar:
        print("\nModo revisión: no se ha escrito ground_truth.py")
        return 0
    write_ground_truth(datos)
    print(f"\n{len(datos)} personas -> {GROUND_TRUTH_FILE}")
    print("Siguiente: python scripts/evaluate.py")
    return 0


# ======================================================================
# Auditoria del grafo
# ======================================================================

# Palabras que no distinguen un club de otro: formas juridicas y genericos.
# Wikidata usa el nombre legal completo ("Fútbol Club Barcelona") y el grafo
# el usual ("FC Barcelona"), asi que hay que quitarlas antes de comparar.
NAME_NOISE = {
    "a",
    "b",
    "c",
    "d",
    "f",
    "r",
    "s",
    "u",
    "ac",
    "fc",
    "cf",
    "cd",
    "ud",
    "sc",
    "sl",
    "sd",
    "rcd",
    "afc",
    "acf",
    "usc",
    "fsv",
    "ssd",
    "football",
    "futbol",
    "futebol",
    "fussball",
    "calcio",
    "soccer",
    "club",
    "clube",
    "kulubu",
    "sports",
    "sportiva",
    "sporting",
    "societa",
    "association",
    "associazione",
    "seleccion",
    "nacional",
    "equipo",
    "unione",
    "union",
    "deportivo",
    "deportiva",
    "atletico",
    "athletic",
    "de",
    "del",
    "la",
    "las",
    "los",
    "el",
}

# Marcadores de filial, comprobados SOLO en la ultima palabra. Si se miran
# en cualquier posicion, la "C" de "R. C. D. Espanyol" o la de "A.C. Milan"
# se confunden con el equipo C de una cantera.
YOUTH_MARKERS = {
    "atletic",
    "ii",
    "iii",
    "b",
    "c",
    "sub19",
    "sub21",
    "sub23",
    "juvenil",
    "inferiores",
    "juniors",
}


# Equivalencias entre el nombre usual (el del grafo, extraido de Wikipedia)
# y el nombre legal o alternativo que usa Wikidata. Ninguna metrica de
# cadenas puede deducirlas: no comparten palabras.
CROSS_SOURCE_ALIASES = [
    {"Selección neerlandesa", "Selección de fútbol de los Países Bajos"},
    {"Selección de Chile", "Selección Chilena de Fútbol"},
    {"Athletic Bilbao", "Athletic Club"},
    {"Parma F. C.", "Parma Calcio 1913", "Parma"},
    {"Lille", "Lille Olympique Sporting Club"},
    {"R. C. D. Espanyol", "Real Club Deportivo Espanyol de Barcelona"},
    {"Colonia", "F. C. Colonia", "1. FC Köln"},
    {"Bayer Leverkusen", "Bayer 04 Leverkusen"},
    {"Oporto", "Fútbol Club Oporto", "Porto"},
    {"Sporting de Lisboa", "Sporting Clube de Portugal"},
    {"Palermo", "Palermo FC", "USC Palermo"},
    {"Darfo Boario", "U.S. Darfo Boario S.S.D."},
    {"Sassuolo", "Unione Sportiva Sassuolo Calcio"},
    {"Foggia Calcio", "Calcio Foggia 1920"},
    {"Napoli", "Società Sportiva Calcio Napoli"},
    {"SL Benfica", "Sport Lisboa e Benfica", "Benfica"},
    {"Leiria", "União Desportiva de Leiria"},
    {"Fenerbahçe", "Fenerbahçe Spor Kulübü"},
    {"Leeds United", "Leeds United Association Football Club"},
    {"Estudiantes", "Club Estudiantes de La Plata"},
    {"River Plate", "Club Atlético River Plate"},
    {"San Lorenzo", "Club Atlético San Lorenzo de Almagro"},
    {"Catania", "Catania Football Club"},
    {"Atlas", "Club Atlas de Guadalajara"},
    {"América", "Club de Fútbol América"},
    {"Brighton & Hove Albion", "Brighton & Hove Albion Football Club"},
    {"Shajtar Donetsk", "FK Shajtar Donetsk"},
    {"Reggiana", "A. C. Reggiana 1919", "Associazione Calcio Reggiana 1919"},
]


def tokens(name: str) -> set[str]:
    """Palabras significativas de un nombre de club."""
    return {
        t for t in normalize_text(name).split() if t not in NAME_NOISE and len(t) > 2
    }


def same_entity(a: str, b: str) -> bool:
    """Compara nombres de club entre fuentes distintas.

    Wikidata da el nombre legal y el grafo el usual, asi que la igualdad
    exacta no sirve. Se comparan conjuntos de palabras significativas: hay
    coincidencia si uno esta contenido en el otro.

        "Newell's Old Boys"                {newell, boys}
        "Club Atlético Newell's Old Boys"  {newell, boys}   -> igual
        "Manchester City"                  {manchester, city}
        "Manchester United Football Club"  {manchester, united}  -> distintos

    Se exige IGUALDAD y no subconjunto: {milan} esta contenido en {inter,
    milan}, y el Milan no es el Inter. El subconjunto parece mas permisivo
    y en realidad solo anade falsos positivos.

    Los marcadores de filial se comprueban en la ultima palabra: sin eso,
    "Barcelona" encajaria con "Barcelona Atlètic", que es otro equipo.
    """
    if a == b:
        return True
    for grupo in CROSS_SOURCE_ALIASES:
        if a in grupo and b in grupo:
            return True

    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return False

    def es_filial(nombre: str) -> bool:
        """Ultima palabra en YOUTH_MARKERS, salvo que venga precedida de otra
        inicial suelta: "Tottenham Hotspur F. C." acaba en "c" y NO es un
        filial, es la abreviatura de Football Club. "Barcelona B" si lo es."""
        partes = normalize_text(nombre).split()
        if not partes or partes[-1] not in YOUTH_MARKERS:
            return False
        return not (len(partes) >= 2 and len(partes[-2]) == 1)

    if es_filial(a) != es_filial(b):
        return False
    return ta == tb


def match_key(nombre: str) -> str:
    """Clave laxa: quita formas juridicas para que 'Atletico de Madrid' y
    'Club Atletico de Madrid' colisionen."""
    return " ".join(t for t in normalize_text(nombre).split() if t not in NAME_NOISE)


def compare(g, relacion: str, referencia: dict):
    """Devuelve (correctas, sobran, faltan) por persona."""
    resultado = {}
    for persona, esperados in referencia.items():
        filas = g.query(
            f"MATCH (p {{id: $p}})-[:{relacion}]->(c) RETURN c.id AS club",
            {"p": persona},
        )
        existe = g.query("MATCH (p {id: $p}) RETURN count(p) AS n", {"p": persona})[0][
            "n"
        ]
        if not existe:
            resultado[persona] = None
            continue

        en_grafo = sorted({f["club"] for f in filas})
        correctas = [c for c in en_grafo if any(same_entity(c, e) for e in esperados)]
        sobran = [c for c in en_grafo if c not in correctas]
        faltan = [e for e in esperados if not any(same_entity(c, e) for c in en_grafo)]
        resultado[persona] = (correctas, sobran, faltan)
    return resultado


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--review",
        action="store_true",
        help="ver que trae Wikidata sin escribir nada",
    )
    parser.add_argument(
        "--download", action="store_true", help="generar ground_truth.py desde Wikidata"
    )
    parser.add_argument("--delete-extra", action="store_true")
    parser.add_argument("--add-missing", action="store_true")
    args = parser.parse_args()

    if args.review or args.download:
        return download(args.review)

    # Import diferido: verdad.py puede no existir todavia si aun no se ha
    # descargado, y no queremos que eso impida ejecutar --download.
    try:
        from ground_truth import ENTRENO_A, JUGO_EN
    except ImportError:
        print("Falta scripts/ground_truth.py.")
        print("Ejecuta: python scripts/evaluate.py --download")
        return 1

    g = connect()

    print("=" * 70)
    print("ENTRENO_A  (lista cerrada: lo que sobra se considera falso)")
    print("=" * 70)

    ent = compare(g, "ENTRENO_A", ENTRENO_A)
    tot_ok = tot_sobra = tot_falta = 0

    for persona in sorted(ent):
        r = ent[persona]
        if r is None:
            print(f"\n{persona}\n  (no existe en el grafo)")
            continue
        correctas, sobran, faltan = r
        tot_ok += len(correctas)
        tot_sobra += len(sobran)
        tot_falta += len(faltan)
        print(
            f"\n{persona}   {len(correctas)} ok / {len(sobran)} sobran "
            f"/ {len(faltan)} faltan"
        )
        for c in sobran:
            print(f"  SOBRA  {c}")
        for f in faltan:
            print(f"  falta  {f}")

    total = tot_ok + tot_sobra
    print("\n" + "-" * 70)
    if total:
        print(
            f"Precision  {tot_ok}/{total} = {100 * tot_ok / total:.0f}%"
            "   (de lo extraido, cuanto es cierto)"
        )
    if tot_ok + tot_falta:
        print(
            f"Cobertura  {tot_ok}/{tot_ok + tot_falta} = "
            f"{100 * tot_ok / (tot_ok + tot_falta):.0f}%"
            "   (de lo cierto, cuanto se extrajo)"
        )

    print("\n" + "=" * 70)
    print("JUGO_EN  (solo informativo: puede faltar algun club amateur)")
    print("=" * 70)
    jug = compare(g, "JUGO_EN", JUGO_EN)
    for persona in sorted(jug):
        r = jug[persona]
        if r is None:
            continue
        correctas, sobran, faltan = r
        if not sobran and not faltan:
            continue
        print(f"\n{persona}")
        for c in sobran:
            print(f"  revisar  {c}")
        for f in faltan:
            print(f"  falta    {f}")

    # --- acciones ---------------------------------------------------------
    if args.delete_extra:
        n = 0
        for persona, r in ent.items():
            if not r:
                continue
            for club in r[1]:
                g.query(
                    "MATCH (p {id: $p})-[r:ENTRENO_A]->(c {id: $c}) DELETE r",
                    {"p": persona, "c": club},
                )
                n += 1
        print(f"\n{n} relaciones ENTRENO_A falsas borradas.")

    if args.add_missing:
        n = 0
        for persona, r in ent.items():
            if not r:
                continue
            for club in r[2]:
                # MERGE crea el club si no existe. Se marca el origen para
                # poder distinguir despues lo extraido de lo curado a mano.
                g.query(
                    """
                    MATCH (p {id: $p})
                    MERGE (c:Club:__Entity__ {id: $c})
                      ON CREATE SET c.origen = 'curado'
                    MERGE (p)-[r:ENTRENO_A]->(c)
                      ON CREATE SET r.origen = 'curado'
                    """,
                    {"p": persona, "c": club},
                )
                n += 1
        print(f"{n} relaciones ENTRENO_A anadidas desde la referencia.")

    if not args.delete_extra and not args.add_missing:
        print("\nInforme sin cambios. Opciones: --delete-extra  --add-missing")
    return 0


if __name__ == "__main__":
    sys.exit(main())