"""
Paso 1: verificar la infraestructura antes de escribir una sola linea del pipeline.

Uso:
    python scripts/smoke_test.py

Comprueba cuatro cosas, en orden de coste:
    1. Neo4j responde por Bolt y tiene APOC cargado
    2. Ollama esta vivo y tiene los dos modelos descargados
    3. El modelo de embeddings devuelve vectores de la dimension esperada
    4. El LLM soporta structured output (imprescindible para LLMGraphTransformer)

Si el 4 falla, el resto del proyecto no funciona. Mejor descubrirlo ahora.
"""

import os
import sys

import httpx
from dotenv import load_dotenv
from neo4j import GraphDatabase
from pydantic import BaseModel, Field

load_dotenv()

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USERNAME", "neo4j")
NEO4J_PASS = os.getenv("NEO4J_PASSWORD", "graphrag2026")
OLLAMA_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
LLM_MODEL = os.getenv("OLLAMA_LLM_MODEL", "qwen2.5:7b-instruct")
EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "bge-m3")
NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "8192"))

OK, FAIL = "  [OK]  ", "  [FAIL]"
errors: list[str] = []


def report(name: str, ok: bool, detail: str = "") -> None:
    print(f"{OK if ok else FAIL} {name}{(' -> ' + detail) if detail else ''}")
    if not ok:
        errors.append(name)


# --- 1. Neo4j -------------------------------------------------------------
print("\n1. Neo4j")
try:
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))
    driver.verify_connectivity()
    with driver.session() as session:
        version = session.run(
            "CALL dbms.components() YIELD versions RETURN versions[0] AS v"
        ).single()["v"]
        report("conexion Bolt", True, f"Neo4j {version}")

        # El indice vectorial nativo requiere >= 5.11
        major, minor = (int(x) for x in version.split(".")[:2])
        report(
            "version soporta indice vectorial",
            (major, minor) >= (5, 11),
            "necesitas 5.11 o superior",
        )

        apoc = session.run(
            "SHOW PROCEDURES YIELD name WHERE name STARTS WITH 'apoc' "
            "RETURN count(*) AS n"
        ).single()["n"]
        report("plugin APOC", apoc > 0, f"{apoc} procedimientos")
    driver.close()
except Exception as exc:
    report("conexion Bolt", False, str(exc)[:120])

# --- 2. Ollama ------------------------------------------------------------
print("\n2. Ollama")
try:
    tags = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=10).json()
    installed = {m["name"].split(":")[0] for m in tags.get("models", [])}
    report("servidor accesible", True, f"{len(installed)} modelos instalados")
    for model in (LLM_MODEL, EMBED_MODEL):
        base = model.split(":")[0]
        report(
            f"modelo {model}",
            base in installed,
            "" if base in installed else f"ejecuta: ollama pull {model}",
        )
except Exception as exc:
    report("servidor accesible", False, f"{str(exc)[:80]} - arranca 'ollama serve'")

# --- 3. Embeddings --------------------------------------------------------
print("\n3. Embeddings")
try:
    from langchain_ollama import OllamaEmbeddings

    embedder = OllamaEmbeddings(model=EMBED_MODEL, base_url=OLLAMA_URL)
    vector = embedder.embed_query("Johan Cruyff entreno al FC Barcelona")
    report("vector generado", len(vector) > 0, f"dimension {len(vector)}")
    print(f"         (anota esta dimension: la necesitas al crear el indice)")
except Exception as exc:
    report("vector generado", False, str(exc)[:120])

# --- 4. Structured output (lo importante) ---------------------------------
print("\n4. Structured output / tool calling")


class Relacion(BaseModel):
    """Una relacion entrenador-club extraida de un texto."""

    entrenador: str = Field(description="Nombre completo del entrenador")
    club: str = Field(description="Nombre del club")


try:
    from langchain_ollama import ChatOllama

    llm = ChatOllama(
        model=LLM_MODEL,
        base_url=OLLAMA_URL,
        temperature=0,
        num_ctx=NUM_CTX,  # sin esto Ollama trunca a 2048 en silencio
    )
    result = llm.with_structured_output(Relacion).invoke(
        "Pep Guardiola dirigio al FC Barcelona entre 2008 y 2012."
    )
    correcto = "Guardiola" in result.entrenador and "Barcelona" in result.club
    report("extraccion estructurada", correcto, f"{result.entrenador} / {result.club}")
except Exception as exc:
    report("extraccion estructurada", False, str(exc)[:150])

# --- Resumen --------------------------------------------------------------
print("\n" + "-" * 60)
if errors:
    print(f"FALLOS ({len(errors)}): " + ", ".join(errors))
    print("Arregla esto antes de seguir al paso 2.")
    sys.exit(1)
print("Todo verde. Listo para el paso 2 (ingesta del corpus).")
