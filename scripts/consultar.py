"""
Paso 3b: consultar. RAG plano vs GraphRAG.

MODOS DE RECUPERACION

  plano   busqueda vectorial -> los K chunks mas parecidos -> al LLM.
          Un RAG normal: solo ve texto.

  grafo   Lo anterior MAS dos cosas:
            a) por cada chunk, salta a las entidades que menciona y recoge
               sus relaciones del grafo
            b) detecta las entidades nombradas en la propia pregunta y
               anade su vecindario, aunque ningun chunk las mencione

SOBRE NORMALIZAR LA PREGUNTA
La busqueda vectorial NO necesita que el usuario escriba bien: "pep
guardiola" y "Pep Guardiola" producen embeddings practicamente iguales,
porque capturan significado y no ortografia.

Donde si importa es en el paso (b): localizar una entidad en el grafo es
comparacion exacta de cadena, y ahi un acento o una mayuscula lo rompe. Por
eso normalizamos ambos lados (minusculas, sin acentos, sin puntuacion) antes
de comparar.

Uso:
    python scripts/consultar.py                  # modo interactivo
    python scripts/consultar.py "tu pregunta"    # una sola consulta
"""

import argparse
import os
import re
import sys
import textwrap
import unicodedata

from dotenv import load_dotenv
from langchain_neo4j import Neo4jGraph, Neo4jVector
from langchain_ollama import ChatOllama, OllamaEmbeddings

load_dotenv()

INDICE = "chunks_vector"
K = 4                    # chunks a recuperar
MIN_ENTIDAD = 4          # long. minima para buscar una entidad en la pregunta
DOMINIO = ("ENTRENO_A|JUGO_EN|ENTRENADO_POR|INFLUYO_EN|FUE_ASISTENTE_DE"
           "|GANO|GANO_COMO_ENTRENADOR|GANO_COMO_JUGADOR|PRACTICA")
# Solo relaciones de carrera: para caminos entre personas, compartir un
# titulo no es un vinculo significativo.
CARRERA = "ENTRENO_A|JUGO_EN|ENTRENADO_POR|INFLUYO_EN|FUE_ASISTENTE_DE"

EXPANSION = """
WITH node, score

OPTIONAL MATCH (node)-[:MENTIONS]->(e)-[r]->(destino)
WHERE type(r) <> 'MENTIONS'
WITH node, score,
     collect(DISTINCT e.id + ' -' + type(r) + '-> ' + destino.id) AS salientes

OPTIONAL MATCH (origen)-[r2]->(e2)<-[:MENTIONS]-(node)
WHERE type(r2) <> 'MENTIONS'
WITH node, score, salientes,
     collect(DISTINCT origen.id + ' -' + type(r2) + '-> ' + e2.id) AS entrantes

WITH node, score, (salientes + entrantes)[0..30] AS hechos
RETURN
  node.text +
  CASE WHEN size(hechos) > 0
       THEN '\\n\\n[Relaciones del grafo]\\n' + apoc.text.join(hechos, '\\n')
       ELSE '' END AS text,
  score,
  {fuente: node.fuente, hechos: size(hechos)} AS metadata
"""

PLANTILLA = """Responde la pregunta usando SOLO el contexto proporcionado.

El contexto incluye hechos en formato  entidad -RELACION-> entidad
Traduce cada relacion LITERALMENTE segun este glosario:

  A -ENTRENO_A-> B          A fue entrenador de B
  A -JUGO_EN-> B            A fue jugador de B
  A -ENTRENADO_POR-> B      A fue dirigido por B
  A -FUE_ASISTENTE_DE-> B   A fue segundo entrenador de B
  A -INFLUYO_EN-> B         A influyo en B
  A -GANO-> B               A gano B
  A -GANO_COMO_ENTRENADOR-> B   A gano B siendo entrenador
  A -GANO_COMO_JUGADOR-> B      A gano B siendo jugador
  A -PRACTICA-> B           A practica el estilo B

REGLA CRITICA 1: no deduzcas nada que no este escrito. Si dos personas se
relacionan con el mismo club, NO significa que coincidieran ni que jugaran
juntas: pudieron pasar por el en epocas distintas y con roles distintos.
Di solo que ambas pasaron por ese club, indicando el rol de cada una.

REGLA CRITICA 2: cada afirmacion debe apoyarse en una linea concreta del
contexto. Si un dato (una fecha, un numero, un club) no aparece
literalmente ahi, NO lo incluyas, aunque lo sepas por otra via. Tu
conocimiento previo no es una fuente valida en esta tarea.

REGLA CRITICA 3: para preguntas de cantidad ("cuantas", "cuantos"), cuenta
SOLO las lineas del contexto que lo respalden. Si el contexto registra el
hecho pero no permite contarlo con exactitud, dilo asi en vez de dar un
numero.

Si aparece un bloque "[Camino mas corto entre las entidades de la
pregunta]", contiene la cadena que las une paso a paso. Explicala respetando
el rol exacto de cada paso.

Si el contexto no contiene la respuesta, dilo claramente. No inventes.

CONTEXTO:
{contexto}

PREGUNTA: {pregunta}

RESPUESTA:"""


def sin_acentos(t: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", t)
        if unicodedata.category(c) != "Mn"
    )


def normalizar(t: str) -> str:
    """minusculas, sin acentos, sin puntuacion, espacios colapsados."""
    s = sin_acentos(t.lower())
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def conf() -> dict:
    return {
        "url": os.getenv("NEO4J_URI", "bolt://127.0.0.1:7687"),
        "username": os.getenv("NEO4J_USERNAME", "neo4j"),
        "password": os.getenv("NEO4J_PASSWORD", "graphrag2026"),
    }


def embeddings() -> OllamaEmbeddings:
    return OllamaEmbeddings(
        model=os.getenv("OLLAMA_EMBED_MODEL", "bge-m3"),
        base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
    )


def llm() -> ChatOllama:
    return ChatOllama(
        model=os.getenv("OLLAMA_LLM_MODEL", "qwen2.5:7b-instruct"),
        base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
        temperature=0,
        num_ctx=int(os.getenv("OLLAMA_NUM_CTX", "8192")),
    )


class Buscador:
    """Se construye una vez y se reutiliza en todas las preguntas.

    Crear el objeto Neo4jVector abre conexion y valida el indice; hacerlo en
    cada pregunta anadiria latencia inutil al modo interactivo.
    """

    def __init__(self) -> None:
        emb = embeddings()
        self.grafo = Neo4jGraph(**conf(), refresh_schema=False)
        self.modelo = llm()
        self.plano = Neo4jVector.from_existing_index(
            embedding=emb, **conf(), index_name=INDICE
        )
        self.expandido = Neo4jVector.from_existing_index(
            embedding=emb, **conf(), index_name=INDICE,
            retrieval_query=EXPANSION,
        )
        # Diccionario normalizado -> id real. Se carga una sola vez.
        filas = self.grafo.query(
            "MATCH (n) WHERE NOT n:Document AND n.id IS NOT NULL RETURN n.id AS id"
        )
        self.entidades = {normalizar(f["id"]): f["id"] for f in filas}
        print(f"  {len(self.entidades)} entidades cargadas para deteccion\n")

    def detectar(self, pregunta: str) -> list[str]:
        """Entidades del grafo nombradas en la pregunta.

        Comparamos ambos lados normalizados, asi que da igual que el usuario
        escriba 'pep guardiola' o 'PEP GUARDIOLA'. El minimo de longitud
        evita que entidades de nombre corto salten con cualquier palabra.
        """
        p = f" {normalizar(pregunta)} "
        halladas = [
            real
            for norm, real in self.entidades.items()
            if len(norm) >= MIN_ENTIDAD and f" {norm} " in p
        ]
        # Si encajan 'Guardiola' y 'Pep Guardiola', nos quedamos con el largo
        return [
            a for a in halladas
            if not any(a != b and normalizar(a) in normalizar(b) for b in halladas)
        ]

    def vecindario(self, entidades: list[str]) -> str:
        """Relaciones a un salto de las entidades detectadas."""
        if not entidades:
            return ""
        filas = self.grafo.query(
            f"""
            MATCH (n)-[r:{DOMINIO}]-(v)
            WHERE n.id IN $ids
            RETURN DISTINCT
              CASE WHEN startNode(r) = n
                   THEN n.id + ' -' + type(r) + '-> ' + v.id
                   ELSE v.id + ' -' + type(r) + '-> ' + n.id END AS hecho
            LIMIT 60
            """,
            {"ids": entidades},
        )
        if not filas:
            return ""
        hechos = "\n".join(f["hecho"] for f in filas)
        return f"[Relaciones de las entidades de la pregunta]\n{hechos}"

    def caminos(self, entidades: list[str]) -> str:
        """Camino mas corto entre cada par de entidades detectadas.

        POR QUE HACE FALTA
        vecindario() solo trae relaciones donde una de las entidades es
        extremo. Para "que conecta a Bielsa con Arteta", el eslabon es
        Pochettino, y sus relaciones no aparecen porque ni Bielsa ni Arteta
        son extremo de ellas. El LLM no puede encadenar hechos que no ve.

        Esto es enrutado por tipo de pregunta: si se nombran dos o mas
        entidades, la pregunta probablemente sea relacional, y un camino
        explicito responde mejor que cien hechos sueltos.
        """
        if len(entidades) < 2:
            return ""

        lineas = []
        for i, a in enumerate(entidades):
            for b in entidades[i + 1:]:
                filas = self.grafo.query(
                    f"""
                    MATCH (x {{id: $a}}), (y {{id: $b}})
                    MATCH camino = shortestPath((x)-[:{CARRERA}*..6]-(y))
                    RETURN [rel IN relationships(camino) |
                            startNode(rel).id + ' -' + type(rel) + '-> '
                            + endNode(rel).id] AS pasos
                    """,
                    {"a": a, "b": b},
                )
                if filas and filas[0]["pasos"]:
                    pasos = "\n".join(f"  {p}" for p in filas[0]["pasos"])
                    lineas.append(f"{a} <-> {b}:\n{pasos}")

        if not lineas:
            return ""
        return "[Camino mas corto entre las entidades de la pregunta]\n" + \
               "\n".join(lineas)

    def consultar(self, pregunta: str, con_grafo: bool) -> dict:
        almacen = self.expandido if con_grafo else self.plano
        docs = almacen.similarity_search(pregunta, k=K)
        partes = [d.page_content for d in docs]

        detectadas = []
        if con_grafo:
            detectadas = self.detectar(pregunta)
            vecinos = self.vecindario(detectadas)
            if vecinos:
                partes.insert(0, vecinos)
            # El camino va PRIMERO: es lo mas especifico que tenemos y un
            # modelo pequeno atiende mejor al principio del contexto.
            ruta = self.caminos(detectadas)
            if ruta:
                partes.insert(0, ruta)

        contexto = "\n\n---\n\n".join(partes)
        respuesta = self.modelo.invoke(
            PLANTILLA.format(contexto=contexto, pregunta=pregunta)
        )
        return {
            "respuesta": respuesta.content.strip(),
            "fuentes": [d.metadata.get("fuente", "?") for d in docs],
            "hechos": sum(d.metadata.get("hechos", 0) for d in docs),
            "detectadas": detectadas,
            "camino": bool(ruta) if con_grafo else False,
            "caracteres": len(contexto),
        }


def mostrar(buscador: Buscador, pregunta: str, con_grafo: bool, titulo: str) -> None:
    print(f"\n{'=' * 70}\n{titulo}\n{'=' * 70}")
    r = buscador.consultar(pregunta, con_grafo)
    print(f"Chunks: {', '.join(str(f) for f in r['fuentes'])}")
    if con_grafo:
        if r["detectadas"]:
            print(f"Entidades detectadas: {', '.join(r['detectadas'])}")
        print(f"Camino entre entidades: {'si' if r['camino'] else 'no'}")
        print(f"Hechos del grafo: {r['hechos']}")
    print(f"Contexto: {r['caracteres']} caracteres\n")
    print(textwrap.fill(r["respuesta"], width=70))


def interactivo(buscador: Buscador) -> int:
    modo = "comparar"
    print("Escribe tu pregunta y pulsa Enter.")
    print("Comandos:  /plano   /grafo   /comparar   /salir\n")

    while True:
        try:
            entrada = input(f"[{modo}] > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nHasta luego.")
            return 0

        if not entrada:
            continue
        if entrada in ("/salir", "/exit", "/q"):
            print("Hasta luego.")
            return 0
        if entrada in ("/plano", "/grafo", "/comparar"):
            modo = entrada[1:]
            print(f"Modo: {modo}\n")
            continue
        if entrada.startswith("/"):
            print("Comandos: /plano  /grafo  /comparar  /salir\n")
            continue

        if modo in ("plano", "comparar"):
            mostrar(buscador, entrada, False, "RAG PLANO (solo vectorial)")
        if modo in ("grafo", "comparar"):
            mostrar(buscador, entrada, True, "GRAPHRAG (vectorial + grafo)")
        print()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pregunta", nargs="?", help="si se omite, modo interactivo")
    parser.add_argument(
        "--modo", choices=["plano", "grafo", "comparar"], default="comparar"
    )
    args = parser.parse_args()

    print("\nConectando...")
    try:
        buscador = Buscador()
    except Exception as exc:
        print(f"Error: {str(exc)[:200]}")
        print("Comprueba que Neo4j esta arriba y que ejecutaste indexar.py")
        return 1

    if args.pregunta:
        if args.modo in ("plano", "comparar"):
            mostrar(buscador, args.pregunta, False, "RAG PLANO (solo vectorial)")
        if args.modo in ("grafo", "comparar"):
            mostrar(buscador, args.pregunta, True, "GRAPHRAG (vectorial + grafo)")
        print()
        return 0

    return interactivo(buscador)


if __name__ == "__main__":
    sys.exit(main())