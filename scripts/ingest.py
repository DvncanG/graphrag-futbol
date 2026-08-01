"""
Paso 2b: construir el grafo.

Pipeline: .txt -> chunks -> LLMGraphTransformer -> Neo4j

Es reanudable: guarda el progreso en data/.progreso.json, asi que si petas
a mitad (o cierras el portatil) no vuelves a empezar de cero.

AVISO: esto tarda. Un 7B en una 3070 va a ~10-20s por chunk, y salen unos
6 chunks por articulo. Cuenta 20-30 minutos para los 14 articulos. Dejalo
corriendo y ve a por un cafe.

Uso:
    python scripts/ingest.py --download  # baja el corpus de Wikipedia
    python scripts/ingest.py            # ingesta normal (reanuda)
    python scripts/ingest.py --reset    # borra el grafo y empieza limpio
    python scripts/ingest.py --limit 2  # solo 2 articulos, para probar
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

import wikipediaapi
from clients import connect, llm
from dotenv import load_dotenv
from langchain_core.documents import Document
from langchain_experimental.graph_transformers import LLMGraphTransformer
from langchain_text_splitters import RecursiveCharacterTextSplitter
from schema import (
    ALLOWED_NODES,
    ALLOWED_RELATIONSHIPS,
    EXTRA_INSTRUCTIONS,
)

load_dotenv()

BASE_DIR = Path(__file__).parent.parent
CORPUS = BASE_DIR / "data" / "corpus"
PROGRESS_FILE = BASE_DIR / "data" / ".progreso.json"

# Chunks pequenos a proposito: un 7B pierde precision cuando le das mucho
# texto de golpe. Con OpenAI podrias irte a 4000 sin problema.
CHUNK_SIZE = 1500
CHUNK_OVERLAP = 200


# ======================================================================
# Descarga del corpus
# ======================================================================

MAX_CHARS = 8000

# Elegidos porque forman cadenas de influencia reales y cruzadas:
#   Michels -> Cruyff -> Guardiola -> Arteta
#   Bielsa -> Pochettino / Simeone
#   Van Gaal -> Mourinho (fue su asistente en el Barcelona)
#   Sacchi -> Ancelotti
ARTICLES = [
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


def download() -> int:
    """Baja los articulos de Wikipedia a data/corpus/.

    Se trunca cada uno a MAX_CHARS: un 7B tarda ~10-20s por chunk y un
    articulo completo son 40+. Con la parte inicial (biografia, trayectoria)
    ya estan las relaciones que interesan.
    """
    CORPUS.mkdir(parents=True, exist_ok=True)
    wiki = wikipediaapi.Wikipedia(
        user_agent="graphrag-futbol/0.1 (https://github.com/DvncanG)",
        language="es",
    )
    fallos = []
    for titulo in ARTICLES:
        pagina = wiki.page(titulo)
        if not pagina.exists():
            print(f"  [FAIL] {titulo} -> no existe en es.wikipedia")
            fallos.append(titulo)
            continue
        texto = pagina.text[:MAX_CHARS]
        destino = CORPUS / f"{slugify(titulo)}.txt"
        destino.write_text(texto, encoding="utf-8")
        print(f"  [OK]   {titulo:25} {len(texto):>6} chars -> {destino.name}")
    print(f"\n{len(ARTICLES) - len(fallos)}/{len(ARTICLES)} articulos en {CORPUS}")
    return 1 if fallos else 0


# ======================================================================
# Ingesta
# ======================================================================


def load_progress() -> set[str]:
    if PROGRESS_FILE.exists():
        return set(json.loads(PROGRESS_FILE.read_text()))
    return set()


def save_progress(hechos: set[str]) -> None:
    PROGRESS_FILE.write_text(json.dumps(sorted(hechos), ensure_ascii=False, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--download", action="store_true", help="bajar el corpus de Wikipedia y salir"
    )
    parser.add_argument("--reset", action="store_true", help="borrar grafo y progreso")
    parser.add_argument("--limit", type=int, default=None, help="limitar articulos")
    args = parser.parse_args()

    if args.download:
        return download()

    ficheros = sorted(CORPUS.glob("*.txt"))
    if not ficheros:
        print(
            f"No hay corpus en {CORPUS}. Ejecuta: python scripts/ingest.py --download"
        )
        return 1

    grafo = connect()

    if args.reset:
        print("Borrando grafo existente...")
        grafo.query("MATCH (n) DETACH DELETE n")
        PROGRESS_FILE.unlink(missing_ok=True)

    # Evita nodos duplicados con el mismo id cuando reejecutas
    grafo.query(
        "CREATE CONSTRAINT entidad_id IF NOT EXISTS "
        "FOR (e:__Entity__) REQUIRE e.id IS UNIQUE"
    )

    transformador = LLMGraphTransformer(
        llm=llm(),
        allowed_nodes=ALLOWED_NODES,
        allowed_relationships=ALLOWED_RELATIONSHIPS,
        # node_properties NO se puede usar con ignore_tool_usage=True: exige
        # function calling nativo. Renunciamos a las propiedades para poder
        # tener un esquema que el modelo respete. Las relaciones importan mas.
        additional_instructions=EXTRA_INSTRUCTIONS,
        strict_mode=True,  # descarta lo que no encaje en el esquema
        # CLAVE: por la via del tool calling, Ollama no traslada el esquema al
        # modelo y este extrae con tipos genericos en ingles (Person, Team...),
        # que strict_mode luego descarta enteros. Forzando extraccion por
        # prompt, el esquema va en el texto y el modelo si lo respeta.
        # Verificado comparando cuatro configuraciones sobre el mismo fragmento.
        ignore_tool_usage=True,
    )

    partidor = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        separators=["\n\n", "\n", ". ", " "],
    )

    hechos = load_progress()
    if args.limit:
        ficheros = ficheros[: args.limit]

    inicio_total = time.time()
    for i, fichero in enumerate(ficheros, 1):
        if fichero.name in hechos:
            print(f"[{i}/{len(ficheros)}] {fichero.stem} -- ya procesado, salto")
            continue

        texto = fichero.read_text(encoding="utf-8")
        chunks = partidor.split_text(texto)
        docs = [
            Document(page_content=c, metadata={"fuente": fichero.stem, "chunk": n})
            for n, c in enumerate(chunks)
        ]

        print(
            f"[{i}/{len(ficheros)}] {fichero.stem} -- {len(chunks)} chunks", flush=True
        )
        inicio = time.time()

        try:
            grafo_docs = transformador.convert_to_graph_documents(docs)
        except Exception as exc:
            print(f"    ERROR: {str(exc)[:150]}")
            print("    Sigo con el siguiente. Este quedara pendiente.")
            continue

        nodos = sum(len(g.nodes) for g in grafo_docs)
        rels = sum(len(g.relationships) for g in grafo_docs)

        grafo.add_graph_documents(
            grafo_docs,
            baseEntityLabel=True,  # anade :__Entity__ a todo, util para indexar
            include_source=True,  # crea nodos Document -> necesario en el paso 3
        )

        hechos.add(fichero.name)
        save_progress(hechos)
        print(f"    {nodos} nodos, {rels} relaciones en {time.time() - inicio:.0f}s")

    print(f"\nTotal: {(time.time() - inicio_total) / 60:.1f} min")

    resumen = grafo.query(
        "MATCH (n) WHERE NOT n:Document "
        "RETURN labels(n)[0] AS tipo, count(*) AS n ORDER BY n DESC"
    )
    print("\nNodos por tipo:")
    for fila in resumen:
        print(f"  {fila['tipo']:15} {fila['n']:>4}")

    rels = grafo.query(
        "MATCH ()-[r]->() WHERE type(r) <> 'MENTIONS' "
        "RETURN type(r) AS tipo, count(*) AS n ORDER BY n DESC"
    )
    print("\nRelaciones por tipo:")
    for fila in rels:
        print(f"  {fila['tipo']:20} {fila['n']:>4}")

    print("\nAhora abre http://localhost:7474 y ejecuta:")
    print("  MATCH (n)-[r]->(m) WHERE NOT n:Document RETURN n,r,m LIMIT 100")
    return 0


if __name__ == "__main__":
    sys.exit(main())