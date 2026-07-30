"""
Diagnostico: por que el transformador devuelve 0 relaciones.

Coge un chunk real del corpus y lo pasa por el LLMGraphTransformer con
cuatro configuraciones distintas. Comparando las salidas se ve exactamente
en que punto se pierde la informacion:

  A) Sin restricciones      -> que produce el modelo por su cuenta
  B) Esquema, sin strict    -> el esquema guia, pero no se filtra nada
  C) Esquema, con strict    -> lo que estabamos usando
  D) Sin tool calling       -> extraccion por prompt en vez de tools

Lectura de resultados:
  A da nodos y B no  -> el esquema en si confunde al modelo
  B da nodos y C no  -> strict_mode esta descartando por tipos que no encajan
  D mejor que C      -> el tool calling del modelo local es el problema

Uso:
    python scripts/debug_extraccion.py
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from langchain_core.documents import Document
from langchain_experimental.graph_transformers import LLMGraphTransformer
from langchain_ollama import ChatOllama
from langchain_text_splitters import RecursiveCharacterTextSplitter

from esquema import (
    INSTRUCCIONES_EXTRA,
    NODOS_PERMITIDOS,
    PROPIEDADES_NODO,
    RELACIONES_PERMITIDAS,
)

load_dotenv()

CORPUS = Path(__file__).parent.parent / "data" / "corpus"


def llm() -> ChatOllama:
    return ChatOllama(
        model=os.getenv("OLLAMA_LLM_MODEL", "qwen2.5:7b-instruct"),
        base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
        temperature=0,
        num_ctx=int(os.getenv("OLLAMA_NUM_CTX", "8192")),
    )


def probar(etiqueta: str, transformador: LLMGraphTransformer, doc: Document) -> None:
    print(f"\n{'=' * 60}\n{etiqueta}\n{'=' * 60}")
    try:
        salida = transformador.convert_to_graph_documents([doc])[0]
    except Exception as exc:
        print(f"  EXCEPCION: {str(exc)[:200]}")
        return

    print(f"  NODOS ({len(salida.nodes)}):")
    for nodo in salida.nodes:
        print(f"    [{nodo.type}] {nodo.id}")

    print(f"  RELACIONES ({len(salida.relationships)}):")
    for rel in salida.relationships:
        print(
            f"    ({rel.source.type}:{rel.source.id}) "
            f"-[{rel.type}]-> "
            f"({rel.target.type}:{rel.target.id})"
        )
    if not salida.relationships:
        print("    (ninguna)")


def main() -> int:
    fichero = CORPUS / "arrigo_sacchi.txt"
    if not fichero.exists():
        print(f"Falta {fichero}. Ejecuta download_corpus.py primero.")
        return 1

    # Mismo troceado que ingest.py, para que sea representativo
    partidor = RecursiveCharacterTextSplitter(
        chunk_size=1500, chunk_overlap=200, separators=["\n\n", "\n", ". ", " "]
    )
    chunk = partidor.split_text(fichero.read_text(encoding="utf-8"))[0]
    doc = Document(page_content=chunk)

    print("TEXTO ANALIZADO (primeros 400 chars):")
    print("-" * 60)
    print(chunk[:400] + "...")

    probar(
        "A) Sin restricciones",
        LLMGraphTransformer(llm=llm()),
        doc,
    )

    probar(
        "B) Esquema, strict_mode=False",
        LLMGraphTransformer(
            llm=llm(),
            allowed_nodes=NODOS_PERMITIDOS,
            allowed_relationships=RELACIONES_PERMITIDAS,
            strict_mode=False,
        ),
        doc,
    )

    probar(
        "C) Esquema completo, strict_mode=True (config actual)",
        LLMGraphTransformer(
            llm=llm(),
            allowed_nodes=NODOS_PERMITIDOS,
            allowed_relationships=RELACIONES_PERMITIDAS,
            node_properties=PROPIEDADES_NODO,
            additional_instructions=INSTRUCCIONES_EXTRA,
            strict_mode=True,
        ),
        doc,
    )

    probar(
        "D) Esquema, sin tool calling (ignore_tool_usage=True)",
        LLMGraphTransformer(
            llm=llm(),
            allowed_nodes=NODOS_PERMITIDOS,
            allowed_relationships=RELACIONES_PERMITIDAS,
            strict_mode=False,
            ignore_tool_usage=True,
        ),
        doc,
    )

    print("\n" + "=" * 60)
    print("Compara las cuatro salidas y mira donde se pierden los nodos.")
    return 0


if __name__ == "__main__":
    sys.exit(main())