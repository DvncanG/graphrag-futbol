"""
Paso 3a: crear el indice vectorial sobre los chunks.

Hasta ahora el grafo solo se consulta con Cypher: hay que saber el nombre
exacto del nodo. Con embeddings se puede buscar por significado, que es lo
que permite hacer preguntas en lenguaje natural.

Que hace exactamente:
  1. Coge cada nodo :Document (los chunks del texto original, creados por
     ingest.py con include_source=True)
  2. Calcula su embedding con bge-m3 (1024 dimensiones)
  3. Lo guarda como propiedad del propio nodo
  4. Crea un indice vectorial de Neo4j sobre esa propiedad

Lo importante: el vector vive DENTRO del grafo, en el mismo nodo que ya
tiene sus relaciones MENTIONS hacia las entidades. Por eso no hace falta
Chroma ni Qdrant: una sola consulta puede buscar por similitud y despues
saltar a las entidades relacionadas.

Uso:
    python scripts/indexar.py
    python scripts/indexar.py --recrear   # borra el indice y lo rehace
"""

import argparse
import os
import sys
import time

from dotenv import load_dotenv
from langchain_neo4j import Neo4jGraph, Neo4jVector
from langchain_ollama import OllamaEmbeddings

load_dotenv()

INDICE = "chunks_vector"


def conf() -> dict:
    return {
        "url": os.getenv("NEO4J_URI", "bolt://127.0.0.1:7687"),
        "username": os.getenv("NEO4J_USERNAME", "neo4j"),
        "password": os.getenv("NEO4J_PASSWORD", "graphrag2026"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--recrear", action="store_true")
    args = parser.parse_args()

    grafo = Neo4jGraph(**conf(), refresh_schema=False)

    n = grafo.query("MATCH (d:Document) RETURN count(d) AS n")[0]["n"]
    if n == 0:
        print("No hay nodos :Document. Ejecuta ingest.py primero.")
        return 1
    print(f"{n} chunks en el grafo")

    if args.recrear:
        print(f"Borrando indice '{INDICE}' y embeddings previos...")
        grafo.query(f"DROP INDEX {INDICE} IF EXISTS")
        grafo.query("MATCH (d:Document) REMOVE d.embedding")

    embeddings = OllamaEmbeddings(
        model=os.getenv("OLLAMA_EMBED_MODEL", "bge-m3"),
        base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
    )

    print("Calculando embeddings (1-2 min)...")
    inicio = time.time()

    # from_existing_graph recorre los nodos que aun no tengan la propiedad
    # de embedding, se la calcula, y crea el indice. Es reanudable: si se
    # corta a mitad, al relanzarlo sigue por donde iba.
    Neo4jVector.from_existing_graph(
        embedding=embeddings,
        **conf(),
        index_name=INDICE,
        node_label="Document",
        text_node_properties=["text"],
        embedding_node_property="embedding",
    )

    print(f"Hecho en {time.time() - inicio:.0f}s")

    # Verificacion: que todos los chunks tengan vector y de la dimension
    # correcta. Un chunk sin embedding es invisible para la busqueda.
    comprobacion = grafo.query(
        """
        MATCH (d:Document)
        RETURN count(d) AS total,
               count(d.embedding) AS con_vector,
               size(head(collect(d.embedding))) AS dimension
        """
    )[0]
    print(
        f"\n{comprobacion['con_vector']}/{comprobacion['total']} chunks "
        f"con vector de {comprobacion['dimension']} dimensiones"
    )

    if comprobacion["con_vector"] < comprobacion["total"]:
        print("AVISO: hay chunks sin embedding. Relanza el script.")
        return 1

    print("\nIndice listo. Siguiente: python scripts/consultar.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())