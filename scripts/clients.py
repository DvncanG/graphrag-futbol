"""
Clientes de los servicios externos: Neo4j y Ollama.

Ejecutado directamente, verifica que todo responde:

    python scripts/clients.py

Un modulo que sabe construir las conexiones deberia saber comprobarlas.

Un unico punto donde se leen las variables de entorno y se construyen las
conexiones. Antes esta configuracion estaba copiada en siete ficheros, asi
que cambiar el puerto de Neo4j obligaba a tocar siete sitios y era cuestion
de tiempo que uno se quedase atras.
"""

import os
import sys

import httpx
from dotenv import load_dotenv
from langchain_neo4j import Neo4jGraph
from langchain_ollama import ChatOllama, OllamaEmbeddings
from neo4j import GraphDatabase
from pydantic import BaseModel, Field

load_dotenv()


def neo4j_config() -> dict:
    """Credenciales de Neo4j.

    127.0.0.1 y no localhost: en Docker Desktop sobre Windows, localhost
    resuelve primero a IPv6 y el proxy acepta la conexion pero no la
    reenvia. El sintoma es un error 'closed', no 'refused'.
    """
    return {
        "url": os.getenv("NEO4J_URI", "bolt://127.0.0.1:7687"),
        "username": os.getenv("NEO4J_USERNAME", "neo4j"),
        "password": os.getenv("NEO4J_PASSWORD", "graphrag2026"),
    }


def connect(refresh_schema: bool = False) -> Neo4jGraph:
    return Neo4jGraph(**neo4j_config(), refresh_schema=refresh_schema)


def llm(temperature: float = 0) -> ChatOllama:
    """num_ctx es CRITICO: el default de Ollama son 2048 tokens y trunca en
    silencio, sin lanzar error. El modelo recibe medio chunk y extrae
    basura, y no hay ninguna senal de que este pasando."""
    return ChatOllama(
        model=os.getenv("OLLAMA_LLM_MODEL", "qwen2.5:7b-instruct"),
        base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
        temperature=temperature,
        num_ctx=int(os.getenv("OLLAMA_NUM_CTX", "8192")),
    )


def embeddings() -> OllamaEmbeddings:
    return OllamaEmbeddings(
        model=os.getenv("OLLAMA_EMBED_MODEL", "bge-m3"),
        base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
    )


# ======================================================================
# Verificacion (python scripts/clients.py)
# ======================================================================


class _Relacion(BaseModel):
    """Solo para comprobar que el modelo sabe devolver salida estructurada."""

    entrenador: str = Field(description="Nombre completo del entrenador")
    club: str = Field(description="Nombre del club")


def check() -> int:
    """Comprueba la infraestructura, de lo barato a lo caro.

    El orden importa: si Neo4j no responde no tiene sentido cargar 4,7 GB
    de modelo en VRAM para descubrirlo despues. Y la ultima comprobacion es
    la critica: si el LLM no sabe devolver salida estructurada, el pipeline
    de extraccion entero es inviable.
    """
    errores = []

    def report(nombre: str, ok: bool, detalle: str = "") -> None:
        print(
            f"  [{'OK' if ok else 'FAIL'}] {nombre}"
            f"{' -> ' + detalle if detalle else ''}"
        )
        if not ok:
            errores.append(nombre)

    cfg = neo4j_config()

    print("\n1. Neo4j")
    try:
        driver = GraphDatabase.driver(
            cfg["url"], auth=(cfg["username"], cfg["password"])
        )
        driver.verify_connectivity()
        with driver.session() as ses:
            version = ses.run(
                "CALL dbms.components() YIELD versions RETURN versions[0] AS v"
            ).single()["v"]
            report("conexion Bolt", True, f"Neo4j {version}")
            mayor, menor = (int(x) for x in version.split(".")[:2])
            soporta = (mayor, menor) >= (5, 11)
            report(
                "indice vectorial disponible",
                soporta,
                "" if soporta else "hace falta 5.11 o superior",
            )
            apoc = ses.run(
                "SHOW PROCEDURES YIELD name WHERE name STARTS WITH 'apoc' "
                "RETURN count(*) AS n"
            ).single()["n"]
            report("plugin APOC", apoc > 0, f"{apoc} procedimientos")
        driver.close()
    except Exception as exc:
        report("conexion Bolt", False, str(exc)[:110])

    print("\n2. Ollama")
    url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    modelo_llm = os.getenv("OLLAMA_LLM_MODEL", "qwen2.5:7b-instruct")
    modelo_emb = os.getenv("OLLAMA_EMBED_MODEL", "bge-m3")
    try:
        tags = httpx.get(f"{url}/api/tags", timeout=10).json()
        instalados = {m["name"].split(":")[0] for m in tags.get("models", [])}
        report("servidor accesible", True, f"{len(instalados)} modelos")
        for m in (modelo_llm, modelo_emb):
            base = m.split(":")[0]
            report(
                f"modelo {m}",
                base in instalados,
                "" if base in instalados else f"ejecuta: ollama pull {m}",
            )
    except Exception as exc:
        report("servidor accesible", False, f"{str(exc)[:70]} - arranca 'ollama serve'")

    print("\n3. Embeddings")
    try:
        vector = embeddings().embed_query("Johan Cruyff entrenó al FC Barcelona")
        report("vector generado", len(vector) > 0, f"dimension {len(vector)}")
    except Exception as exc:
        report("vector generado", False, str(exc)[:110])

    print("\n4. Salida estructurada (lo critico)")
    try:
        r = (
            llm()
            .with_structured_output(_Relacion)
            .invoke("Pep Guardiola dirigió al FC Barcelona entre 2008 y 2012.")
        )
        ok = "Guardiola" in r.entrenador and "Barcelona" in r.club
        report("extraccion estructurada", ok, f"{r.entrenador} / {r.club}")
    except Exception as exc:
        report("extraccion estructurada", False, str(exc)[:140])

    print("\n" + "-" * 60)
    if errores:
        print(f"FALLOS ({len(errores)}): " + ", ".join(errores))
        return 1
    print("Todo verde. Siguiente: python scripts/ingest.py --download")
    return 0


if __name__ == "__main__":
    sys.exit(check())