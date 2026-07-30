"""
Paso 3b: answer. RAG plano vs GraphRAG.

MODOS DE RECUPERACION

  plano   busqueda vectorial -> los K chunks mas parecidos -> al LLM.
          Un RAG normal: solo ve texto.

  grafo   Lo anterior MAS dos cosas:
            a) por cada chunk, salta a las entidades que menciona y recoge
               sus relaciones del grafo
            b) detecta las entidades nombradas en la propia pregunta y
               anade su neighbourhood, aunque ningun chunk las mencione

SOBRE NORMALIZAR LA PREGUNTA
La busqueda vectorial NO necesita que el usuario escriba bien: "pep
guardiola" y "Pep Guardiola" producen embeddings practicamente iguales,
porque capturan significado y no ortografia.

Donde si importa es en el paso (b): localizar una entidad en el grafo es
comparacion exacta de cadena, y ahi un acento o una mayuscula lo rompe. Por
eso normalizamos ambos lados (minusculas, sin acentos, sin puntuacion) antes
de comparar.

Uso:
    python scripts/answer.py --index        # crear el indice (una vez)
    python scripts/answer.py                  # modo repl
    python scripts/answer.py "tu pregunta"    # una sola consulta
"""

import argparse
import re
import sys
import textwrap
import time

from clients import embeddings, llm, neo4j_config
from dotenv import load_dotenv
from langchain_neo4j import Neo4jGraph, Neo4jVector
from names import strip_accents

load_dotenv()

INDEX_NAME = "chunks_vector"
K = 4  # chunks a recuperar
MIN_ENTITY_LEN = 4  # long. minima para buscar una entidad en la pregunta
DOMAIN_RELS = (
    "ENTRENO_A|JUGO_EN|ENTRENADO_POR|INFLUYO_EN|FUE_ASISTENTE_DE"
    "|GANO|GANO_COMO_ENTRENADOR|GANO_COMO_JUGADOR|PRACTICA"
)
# Solo relaciones de fetch_career: para shortest_paths entre personas, compartir un
# titulo no es un vinculo significativo.
CAREER = "ENTRENO_A|JUGO_EN|ENTRENADO_POR|INFLUYO_EN|FUE_ASISTENTE_DE"

GRAPH_EXPANSION = """
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

PROMPT = """Responde la pregunta usando SOLO el contexto proporcionado.

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

Si aparece un render_block "[Camino mas corto entre las entidades de la
pregunta]", contiene la cadena que las une paso a paso. Explicala respetando
el rol exacto de cada paso.

Si el contexto no contiene la respuesta, dilo claramente. No inventes.

CONTEXTO:
{contexto}

PREGUNTA: {pregunta}

RESPUESTA:"""


def normalize(t: str) -> str:
    """minusculas, sin acentos, sin puntuacion, espacios colapsados."""
    s = strip_accents(t.lower())
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def build_index(recrear: bool = False) -> int:
    """Calcula los embeddings de cada chunk y crea el indice vectorial.

    El vector se guarda como propiedad del propio nodo :Document, que ya
    tiene sus relaciones MENTIONS hacia las entidades. Por eso no hace falta
    un almacen vectorial aparte: una sola consulta Cypher puede buscar por
    similitud y saltar despues a las entidades relacionadas.
    """
    grafo = Neo4jGraph(**neo4j_config(), refresh_schema=False)
    n = grafo.query("MATCH (d:Document) RETURN count(d) AS n")[0]["n"]
    if n == 0:
        print("No hay nodos :Document. Ejecuta ingest.py primero.")
        return 1
    print(f"{n} chunks en el grafo")

    if recrear:
        print(f"Borrando indice '{INDEX_NAME}' y embeddings previos...")
        grafo.query(f"DROP INDEX {INDEX_NAME} IF EXISTS")
        grafo.query("MATCH (d:Document) REMOVE d.embedding")

    print("Calculando embeddings...")
    inicio = time.time()
    # Recorre los nodos sin embedding, lo calcula y crea el indice.
    # Es reanudable: si se corta a mitad, al relanzarlo sigue por donde iba.
    Neo4jVector.from_existing_graph(
        embedding=embeddings(),
        **neo4j_config(),
        index_name=INDEX_NAME,
        node_label="Document",
        text_node_properties=["text"],
        embedding_node_property="embedding",
    )
    print(f"Hecho en {time.time() - inicio:.0f}s")

    c = grafo.query(
        """
        MATCH (d:Document)
        RETURN count(d) AS total, count(d.embedding) AS con_vector,
               size(head(collect(d.embedding))) AS dimension
        """
    )[0]
    print(
        f"{c['con_vector']}/{c['total']} chunks con vector de "
        f"{c['dimension']} dimensiones"
    )
    if c["con_vector"] < c["total"]:
        print("AVISO: hay chunks sin embedding. Relanza el script.")
        return 1
    return 0


class Retriever:
    """Se construye una vez y se reutiliza en todas las preguntas.

    Crear el objeto Neo4jVector abre conexion y valida el indice; hacerlo en
    cada pregunta anadiria latencia inutil al modo repl.
    """

    def __init__(self) -> None:
        emb = embeddings()
        self.grafo = Neo4jGraph(**neo4j_config(), refresh_schema=False)
        self.modelo = llm()
        self.plano = Neo4jVector.from_existing_index(
            embedding=emb, **neo4j_config(), index_name=INDEX_NAME
        )
        self.expandido = Neo4jVector.from_existing_index(
            embedding=emb,
            **neo4j_config(),
            index_name=INDEX_NAME,
            retrieval_query=GRAPH_EXPANSION,
        )
        # Diccionario normalizado -> id real. Se carga una sola vez.
        filas = self.grafo.query(
            "MATCH (n) WHERE NOT n:Document AND n.id IS NOT NULL RETURN n.id AS id"
        )
        self.entidades = {normalize(f["id"]): f["id"] for f in filas}
        print(f"  {len(self.entidades)} entidades cargadas para deteccion\n")

    def detect_entities(self, pregunta: str) -> list[str]:
        """Entidades del grafo nombradas en la pregunta.

        Comparamos ambos lados normalizados, asi que da igual que el usuario
        escriba 'pep guardiola' o 'PEP GUARDIOLA'. El minimo de longitud
        evita que entidades de nombre corto salten con cualquier palabra.
        """
        p = f" {normalize(pregunta)} "
        halladas = [
            real
            for norm, real in self.entidades.items()
            if len(norm) >= MIN_ENTITY_LEN and f" {norm} " in p
        ]
        # Si encajan 'Guardiola' y 'Pep Guardiola', nos quedamos con el largo
        return [
            a
            for a in halladas
            if not any(a != b and normalize(a) in normalize(b) for b in halladas)
        ]

    def neighbourhood(self, entidades: list[str]) -> str:
        """Relaciones a un salto de las entidades detectadas."""
        if not entidades:
            return ""
        filas = self.grafo.query(
            f"""
            MATCH (n)-[r:{DOMAIN_RELS}]-(v)
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

    def shortest_paths(self, entidades: list[str]) -> str:
        """Camino mas corto entre cada par de entidades detectadas.

        POR QUE HACE FALTA
        neighbourhood() solo trae relaciones donde una de las entidades es
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
            for b in entidades[i + 1 :]:
                filas = self.grafo.query(
                    f"""
                    MATCH (x {{id: $a}}), (y {{id: $b}})
                    MATCH camino = shortestPath((x)-[:{CAREER}*..6]-(y))
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
        return "[Camino mas corto entre las entidades de la pregunta]\n" + "\n".join(
            lineas
        )

    def answer(self, pregunta: str, con_grafo: bool) -> dict:
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
            PROMPT.format(contexto=contexto, pregunta=pregunta)
        )
        return {
            "respuesta": respuesta.content.strip(),
            "fuentes": [d.metadata.get("fuente", "?") for d in docs],
            "hechos": sum(d.metadata.get("hechos", 0) for d in docs),
            "detectadas": detectadas,
            "camino": bool(ruta) if con_grafo else False,
            "caracteres": len(contexto),
        }


def render(buscador: Retriever, pregunta: str, con_grafo: bool, titulo: str) -> None:
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


def repl(buscador: Retriever) -> int:
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
            render(buscador, entrada, False, "RAG PLANO (solo vectorial)")
        if modo in ("grafo", "comparar"):
            render(buscador, entrada, True, "GRAPHRAG (vectorial + grafo)")
        print()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("question", nargs="?", help="si se omite, modo repl")
    parser.add_argument(
        "--mode", choices=["plano", "grafo", "comparar"], default="comparar"
    )
    parser.add_argument(
        "--index", action="store_true", help="crear el indice vectorial y salir"
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="con --index, borra el indice y lo rehace",
    )
    args = parser.parse_args()

    if args.index:
        return build_index(args.rebuild)

    print("\nConectando...")
    try:
        buscador = Retriever()
    except Exception as exc:
        print(f"Error: {str(exc)[:200]}")
        print("Comprueba que Neo4j esta arriba y que ejecutaste build_index.py")
        return 1

    if args.question:
        if args.mode in ("plano", "comparar"):
            render(buscador, args.question, False, "RAG PLANO (solo vectorial)")
        if args.mode in ("grafo", "comparar"):
            render(buscador, args.question, True, "GRAPHRAG (vectorial + grafo)")
        print()
        return 0

    return repl(buscador)


if __name__ == "__main__":
    sys.exit(main())