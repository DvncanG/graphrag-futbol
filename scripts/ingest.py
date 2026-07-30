"""
Paso 2b: construir el grafo.

Pipeline: .txt -> chunks -> LLMGraphTransformer -> Neo4j

Es reanudable: guarda el progreso en data/.progreso.json, asi que si petas
a mitad (o cierras el portatil) no vuelves a empezar de cero.

AVISO: esto tarda. Un 7B en una 3070 va a ~10-20s por chunk, y salen unos
6 chunks por articulo. Cuenta 20-30 minutos para los 14 articulos. Dejalo
corriendo y ve a por un cafe.

Uso:
    python scripts/ingest.py            # ingesta normal (reanuda)
    python scripts/ingest.py --reset    # borra el grafo y empieza limpio
    python scripts/ingest.py --limit 2  # solo 2 articulos, para probar
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from langchain_core.documents import Document
from langchain_experimental.graph_transformers import LLMGraphTransformer
from langchain_neo4j import Neo4jGraph
from langchain_ollama import ChatOllama
from langchain_text_splitters import RecursiveCharacterTextSplitter

from esquema import (
    INSTRUCCIONES_EXTRA,
    NODOS_PERMITIDOS,
    RELACIONES_PERMITIDAS,
)

load_dotenv()

BASE = Path(__file__).parent.parent
CORPUS_DIR = BASE / "data" / "corpus"
PROGRESO = BASE / "data" / ".progreso.json"

# Chunks pequenos a proposito: un 7B pierde precision cuando le das mucho
# texto de golpe. Con OpenAI podrias irte a 4000 sin problema.
CHUNK_SIZE = 1500
CHUNK_OVERLAP = 200


def construir_llm() -> ChatOllama:
    return ChatOllama(
        model=os.getenv("OLLAMA_LLM_MODEL", "qwen2.5:7b-instruct"),
        base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
        temperature=0,  # extraccion, no creatividad
        num_ctx=int(os.getenv("OLLAMA_NUM_CTX", "8192")),
    )


def construir_grafo() -> Neo4jGraph:
    return Neo4jGraph(
        url=os.getenv("NEO4J_URI", "bolt://localhost:7687"),
        username=os.getenv("NEO4J_USERNAME", "neo4j"),
        password=os.getenv("NEO4J_PASSWORD", "graphrag2026"),
        refresh_schema=False,  # el grafo esta vacio al principio
    )


def cargar_progreso() -> set[str]:
    if PROGRESO.exists():
        return set(json.loads(PROGRESO.read_text()))
    return set()


def guardar_progreso(hechos: set[str]) -> None:
    PROGRESO.write_text(json.dumps(sorted(hechos), ensure_ascii=False, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="borrar grafo y progreso")
    parser.add_argument("--limit", type=int, default=None, help="limitar articulos")
    args = parser.parse_args()

    ficheros = sorted(CORPUS_DIR.glob("*.txt"))
    if not ficheros:
        print(f"No hay corpus en {CORPUS_DIR}. Ejecuta download_corpus.py primero.")
        return 1

    grafo = construir_grafo()

    if args.reset:
        print("Borrando grafo existente...")
        grafo.query("MATCH (n) DETACH DELETE n")
        PROGRESO.unlink(missing_ok=True)

    # Evita nodos duplicados con el mismo id cuando reejecutas
    grafo.query(
        "CREATE CONSTRAINT entidad_id IF NOT EXISTS "
        "FOR (e:__Entity__) REQUIRE e.id IS UNIQUE"
    )

    transformador = LLMGraphTransformer(
        llm=construir_llm(),
        allowed_nodes=NODOS_PERMITIDOS,
        allowed_relationships=RELACIONES_PERMITIDAS,
        # node_properties NO se puede usar con ignore_tool_usage=True: exige
        # function calling nativo. Renunciamos a las propiedades para poder
        # tener un esquema que el modelo respete. Las relaciones importan mas.
        additional_instructions=INSTRUCCIONES_EXTRA,
        strict_mode=True,  # descarta lo que no encaje en el esquema
        # CLAVE: por la via del tool calling, Ollama no traslada el esquema al
        # modelo y este extrae con tipos genericos en ingles (Person, Team...),
        # que strict_mode luego descarta enteros. Forzando extraccion por
        # prompt, el esquema va en el texto y el modelo si lo respeta.
        # Verificado con scripts/debug_extraccion.py: pasada B vs pasada D.
        ignore_tool_usage=True,
    )

    partidor = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        separators=["\n\n", "\n", ". ", " "],
    )

    hechos = cargar_progreso()
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

        print(f"[{i}/{len(ficheros)}] {fichero.stem} -- {len(chunks)} chunks", flush=True)
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
            include_source=True,   # crea nodos Document -> necesario en el paso 3
        )

        hechos.add(fichero.name)
        guardar_progreso(hechos)
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