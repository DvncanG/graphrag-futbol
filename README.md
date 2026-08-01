# GraphRAG sobre linajes de entrenadores de fútbol

Sistema de **GraphRAG** que construye un grafo de conocimiento a partir de
biografías de entrenadores de fútbol y responde preguntas que requieren
recorrer varias relaciones encadenadas.

Todo corre **en local**: sin claves de API, sin coste por token.

**[▶ Explorar el grafo](https://dvncang.github.io/graphrag-futbol/)** — visualización
interactiva: busca entidades y traza el camino entre dos personas.

---

## La pregunta que justifica el proyecto

> ¿Qué conecta a Marcelo Bielsa con Mikel Arteta?

No existe ningún documento del corpus donde ambos aparezcan juntos. Una
búsqueda vectorial clásica (RAG) no encuentra nada: no hay ningún párrafo
que responda a esa pregunta, así que devuelve fragmentos irrelevantes o
admite que no lo sabe.

El grafo la responde recorriendo relaciones extraídas de **tres artículos
distintos**:

```
Marcelo Bielsa  ──ENTRENO_A──▶  Newell's Old Boys  ◀──JUGO_EN──  Pochettino
                                                                      │
                                                                 ENTRENO_A
                                                                      ▼
Mikel Arteta    ──JUGO_EN──▶    Paris Saint-Germain  ◀────────────────┘
```

La respuesta no está escrita en ninguna parte. Está **repartida** entre
varios documentos, y sólo emerge al conectarlos.

---

## Arquitectura

```
Wikipedia (es)  ──▶  chunks  ──▶  LLM (extracción)  ──▶  Neo4j
                                        │
                                  esquema cerrado
                                        │
                                        ▼
                            resolución de entidades
                            (automática + revisión humana)
```

| Componente | Elección | Motivo |
|---|---|---|
| Base de grafos | Neo4j 5.26 Community + APOC | Índice vectorial nativo desde 5.11: grafo y embeddings en la misma BD, sin store externo |
| LLM extracción | `qwen2.5:7b-instruct` (Ollama) | Buen seguimiento de instrucciones en español; cabe en 8 GB de VRAM |
| Embeddings | `bge-m3` (1024 dims) | Multilingüe, imprescindible para corpus en español |
| Orquestación | LangChain (`LLMGraphTransformer`) | Extracción guiada por esquema |
| Infraestructura | Docker Compose | Neo4j reproducible; Ollama nativo en el host para acceso directo a GPU |
| Python | 3.12 | 3.14 aún no tiene soporte fiable en todo el stack de LangChain |

---

## El esquema del grafo

El esquema es **cerrado**: el modelo sólo puede usar estos tipos. Sin esta
restricción, un LLM inventa un tipo distinto para el mismo concepto
(`Entrenador`, `Técnico`, `Manager`, `DirectorTécnico`) y el grafo queda
inservible porque nada conecta con nada.

**Nodos:** `Entrenador`, `Jugador`, `Club`, `Seleccion`, `Competicion`,
`EstiloJuego`

**Relaciones:**

```
(Entrenador) ─ENTRENO_A────────▶ (Club | Seleccion)
(Entrenador) ─INFLUYO_EN───────▶ (Entrenador)
(Entrenador) ─FUE_ASISTENTE_DE─▶ (Entrenador)
(Entrenador) ─PRACTICA─────────▶ (EstiloJuego)
(Jugador)    ─JUGO_EN──────────▶ (Club)
(Jugador)    ─ENTRENADO_POR────▶ (Entrenador)
(Club)       ─GANO────────────▶ (Competicion)
(Entrenador) ─GANO_COMO_ENTRENADOR─▶ (Competicion)
(Jugador)    ─GANO_COMO_JUGADOR────▶ (Competicion)
```

`GANO` se separa por rol porque casi todos los entrenadores de élite fueron
antes jugadores: "cuántas Champions tiene Ancelotti" son 2 como jugador y 5
como entrenador. Con una relación única la pregunta es irresoluble.

Restringir también los extremos de cada relación evita disparates como
`(Competicion)-[ENTRENO_A]->(Jugador)`.

---

## Puesta en marcha

**Requisitos:** Docker, Python 3.12, [Ollama](https://ollama.com), GPU con
8 GB de VRAM (o paciencia).

```bash
# Modelos (~6 GB de descarga)
ollama pull qwen2.5:7b-instruct
ollama pull bge-m3

# Entorno
cp .env.example .env
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Base de datos
docker compose up -d

# Verificación: NO seguir si algo sale en rojo
python scripts/clients.py
```

Ejecutar `clients.py` directamente comprueba, de lo barato a lo caro: conexión Bolt, versión
de Neo4j, APOC, disponibilidad de los modelos, dimensión de los embeddings y
—lo crítico— que el LLM sepa devolver salida estructurada. Si eso último
falla, el pipeline entero es inviable, y es mejor saberlo en 30 segundos que
en el paso 3.

El proyecto pasa `ruff check` sin avisos. La configuración está en
`ruff.toml`, con dos excepciones documentadas: los `except Exception` de los
procesos por lotes (interesa registrar el fallo y seguir, no abortar) y el
límite de longitud de línea en `build_site.py`, que lleva incrustada una
plantilla HTML.

```bash
ruff check scripts        # ver problemas
ruff check scripts --fix  # arreglar los automatizables
ruff format scripts       # formatear
```

---

## Pipeline

```bash
python scripts/ingest.py --download          # 14 artículos de Wikipedia (es)
python scripts/ingest.py --reset           # ~5 min con GPU
python scripts/dedupe.py --clean              # previsualiza ruido
python scripts/dedupe.py --clean --confirm  # borra
python scripts/dedupe.py --propose             # candidatos a fusión
python scripts/curate.py --approve                     # aplica decisiones revisadas
python scripts/dedupe.py --apply              # fusiona
```

```bash
python scripts/curate.py --apply   # alias manuales y relaciones falsas
python scripts/query.py --index          # índice vectorial
python scripts/query.py                # preguntas en lenguaje natural
python scripts/build_site.py             # genera docs/index.html
```

La deduplicación es **iterativa**: cada fusión puede destapar duplicados que
antes quedaban ocultos. Repetir `--propose` / `--approve` / `--apply`
hasta que no aparezca nada nuevo.

Todo el pipeline es reproducible: el grafo depurado se reconstruye desde cero
ejecutando esa secuencia, sin ninguna intervención manual. Las decisiones que
requirieron criterio humano viven todas en `curate.py`:
fusiones revisadas, alias entre idiomas y relaciones verificadas como falsas.

---

## Hallazgos técnicos

### 1. Con Ollama, el esquema no llega al modelo por tool calling

`LLMGraphTransformer` usa por defecto function calling y mete el esquema en
la definición de la herramienta. **Ollama no traslada esa restricción al
modelo**, que extrae con sus tipos genéricos en inglés:

```
Con tool calling:    [Person] Arrigo Sacchi  -[COACHED]->  [Team] AC Milan
Con ignore_tool_usage=True:
                     [Entrenador] Arrigo Sacchi  -[ENTRENO_A]->  [Club] AC Milan
```

Peor aún: `strict_mode=True` descarta después esos tipos no permitidos **en
silencio**, dejando un grafo vacío sin ningún error. El síntoma era 0
relaciones extraídas sin traza alguna.

La solución es `ignore_tool_usage=True`, que mete el esquema en el prompt.
Contrapartida: es incompatible con `node_properties`, así que se renuncia a
las propiedades de nodo.

El diagnóstico se hizo comparando cuatro configuraciones (con y sin
esquema, con y sin `strict_mode`, con y sin tool calling) sobre el mismo
fragmento de texto.

### 2. Ninguna métrica sola resuelve la resolución de entidades

Cuatro detectores, cada uno con un punto ciego distinto:

| Detector | Caza | Punto ciego |
|---|---|---|
| Normalización | `Real Madrid CF` = `Real Madrid` | Alias en otro idioma |
| Prefijo | `Ajax` ⊂ `Ajax de Ámsterdam` | `Liga` ⊂ `Liga de Campeones` (falso) |
| Apellido | `Ancelotti` → `Carlo Ancelotti` | Apellidos compuestos (`van Gaal`) |
| Distancia de edición | `Johann`/`Johan Cruyff` | Años: `Copa América 1991` vs `1993` |

Un primer intento con **Jaro-Winkler + union-find** fracasó de forma
instructiva: la métrica premia los prefijos comunes, y el union-find encadena
transitivamente, así que un solo par falso contamina el grupo entero:

```
Real Madrid | Real Madrid CF | Real Sociedad      ← Real Sociedad no pinta nada
Marcelo Bielsa | Rafael Bielsa                    ← son hermanos
Carlo Ancelotti | Carlo Mazzone                   ← personas distintas
```

El enfoque definitivo compara **claves normalizadas por igualdad exacta** en
lugar de medir parecido, agrupa transitivamente sólo ahí, y descarta cualquier
propuesta ambigua (si `Bielsa` encaja con dos nombres completos, no se propone
nada).

Y un diccionario manual para lo que ninguna métrica puede ver:

```
PSG                    = Paris Saint-Germain
Champions League       = Liga de Campeones de la UEFA
Girondins de Bordeaux  = Girondins de Burdeos
Maguncia               = Mainz 05
Pep Guardiola          = Josep Guardiola
```

**La resolución de entidades determina qué preguntas puede responder el
sistema.** El camino Bielsa→Arteta no existía hasta fusionar `PSG` con
`Paris Saint-Germain`: Arteta llevaba todo el tiempo a un salto de distancia
y el grafo no lo sabía.

### 3. `shortestPath` encuentra caminos, no verdades

Primer resultado de la consulta estrella, antes de filtrar tipos de relación:

```
Bielsa ─JUGO_EN─▶ Newell's ◀─MENTIONS─ (Doc) ─MENTIONS─▶ PSG ─...─▶ Arteta
```

`MENTIONS` es fontanería del pipeline (`include_source=True`) y sólo significa
"estas dos entidades salieron en el mismo artículo". Como conecta cualquier
cosa con cualquier cosa, `shortestPath` la usa siempre de atajo y produce
caminos sintácticamente válidos y semánticamente vacíos.

Hay que restringir explícitamente a relaciones de dominio:

```cypher
MATCH camino = shortestPath(
  (a:Entrenador {id: 'Marcelo Bielsa'})
  -[:ENTRENO_A|JUGO_EN|ENTRENADO_POR|INFLUYO_EN|FUE_ASISTENTE_DE*..8]-
  (b {id: 'Mikel Arteta'})
)
RETURN camino
```

Y aun así, **cada arista del camino debe auditarse a mano** antes de darla por
buena: un camino con una relación alucinada se ve exactamente igual de bien.

### 4. Más contexto no es mejor contexto

Comparando ambos modos sobre la misma pregunta factual ("¿cuántas Champions
ganó Ancelotti?"):

```
RAG plano   4.682 caracteres de contexto  → respuesta correcta
GraphRAG    9.233 caracteres, 102 hechos  → respuesta peor
```

La expansión por grafo aportaba **una** línea útil y noventa de ruido, y
diluyó el fragmento de texto que contenía la respuesta. GraphRAG gana en
preguntas relacionales y **estorba** en preguntas factuales cuya respuesta
está en un solo párrafo.

De ahí el enrutado por tipo de pregunta implementado en `query.py`: si se
detectan dos o más entidades en la pregunta, se calcula el camino entre ellas
con `shortestPath` y se inyecta al principio del contexto. Con una sola
entidad, basta la búsqueda vectorial.

| Tipo | Ejemplo | Estrategia |
|---|---|---|
| Factual | "¿En qué año nació X?" | Vectorial a secas |
| Relacional | "¿Qué conecta a X con Y?" | Vectorial + `shortestPath` |
| Agregativa | "¿Cuántas X ganó Y?" | Requiere Cypher generado (pendiente) |

### 5. Un RAG mezcla el contexto con lo que el modelo ya sabía

Preguntado por los títulos de Ancelotti, el sistema respondió con años
concretos (2003, 2007, 2014, 2016, 2024). El grafo solo contiene **una**
relación `GANO_COMO_ENTRENADOR` hacia la Liga de Campeones, sin años: parte
de esa respuesta venía del entrenamiento del modelo, no de los datos.

Y sonaba exactamente igual de segura que una respuesta fundamentada. Se
detectó auditando el grafo, no leyendo la respuesta.

Mitigación en el prompt: exigir que cada afirmación se apoye en una línea
concreta del contexto y declarar el conocimiento previo como fuente no
válida. El resultado son respuestas más pobres y verificables.

### 6. Un modelo pequeño no obedece instrucciones negativas

Experimento: pedir al esquema que incluyera el año en los nombres de
competición, para poder contar títulos.

```
Sin año   ~60 nodos de Competicion
Con año   212 nodos, casi todos de grado 1
```

Y no resolvió el problema: faltaban tres de las cinco Champions de Ancelotti,
así que un `count()` seguiría dando mal. Además introdujo fechas desplazadas
(FA Cup 2010-11 en vez de 2009-10) y convirtió subcampeonatos en victorias.

Al revertirlo, la instrucción explícita *"NO incluyas años"* fue ignorada: el
modelo siguió generando "FA Cup 2010-11" y "Balón de Oro en 2011".

La solución no fue insistir en el prompt sino **normalizar después**: una
expresión regular que quita el año del final del nombre y fusiona los
duplicados resultantes (`curate.py`). Determinista, verificable, y no
depende de que nadie obedezca.

---

## Calidad de los datos

Medida con `evaluate.py`, que compara el grafo contra **Wikidata** (propiedades
`P54` y `P6087`). La referencia es una fuente estructurada e independiente, no
el criterio de quien escribió el proyecto: sin eso, cualquier porcentaje sería
autocomplaciente.

| Métrica | Valor | Qué significa |
|---|---|---|
| **Precisión** | 53/55 = **96%** | De lo que el modelo extrajo, cuánto es cierto |
| **Cobertura** | 53/88 = **60%** | De lo que es cierto, cuánto llegó a extraer |

Los dos casos contados como error **son en realidad correctos**: Sacchi dirigió
al Cesena (1983-85) y Bielsa al seleccionado de la Universidad de Buenos Aires.
Wikidata no los recoge. Es decir, la referencia también tiene huecos, y una
fuente estructurada es *más fiable*, no infalible.

### Las tres causas de discrepancia

Distinguirlas es lo que convierte un porcentaje en un diagnóstico:

**1. Errores de extracción** — culpa del pipeline. El patrón dominante era
la **confusión de rol**: personas relacionadas con un club a las que el modelo
asignaba `ENTRENO_A` sin distinguir el cargo.

```
José Mourinho -ENTRENO_A-> Rio Ave              (ahí jugó)
José Mourinho -ENTRENO_A-> FC Barcelona         (fue ayudante de Van Gaal)
Arrigo Sacchi -ENTRENO_A-> Real Madrid          (fue director deportivo)
Jürgen Klopp  -ENTRENO_A-> D-Juniors Fráncfort  (ahí jugó de niño)
```

Verificados uno a uno contra Wikidata y registrados en `curate.py` con su
motivo, de forma que la corrección es reproducible y auditable.

**2. Huecos del corpus** — no es culpa de nadie. Cada artículo se trunca a
8.000 caracteres, así que las carreras largas quedan cortadas. De Zerbi tiene
8 ausencias y Simeone 6, simplemente porque sus biografías no caben. El modelo
no falló: la información nunca estuvo en el texto.

**3. Huecos de la referencia** — los dos casos citados arriba.

Solo la primera categoría mide la calidad del sistema. Un informe que las
mezcle da un número mucho peor y mucho menos útil.

### Cómo se llegó al 96%

La primera medición dio **59%**, y casi todo era un fallo del comparador, no
de la extracción:

```
SOBRA  Tottenham Hotspur F. C.        ← el mismo club
falta  Tottenham Hotspur Football Club   contado como dos errores
```

Wikidata usa el nombre legal y el grafo el usual. La comparación exacta de
cadenas los tomaba por clubes distintos y los penalizaba dos veces. Se
resolvió comparando **conjuntos de palabras significativas** en lugar de
cadenas, más un diccionario de equivalencias entre fuentes para lo que
ninguna métrica puede deducir (`Selección neerlandesa` = `Selección de fútbol
de los Países Bajos`).

La lección es que **una métrica hay que auditarla antes de creérsela**. Ese
59% inicial habría sido una conclusión falsa sobre el pipeline.

---

## Limitaciones conocidas

- `INFLUYO_EN` es la relación más escasa (9 instancias). Wikipedia rara vez
  afirma explícitamente una influencia, y el prompt exige afirmación literal
  para evitar deducirla de haber coincidido en un club.
- Sin propiedades de nodo (años, nacionalidad), por la incompatibilidad
  descrita en el hallazgo 1.
- Las ediciones concretas de torneos (`Premier League 2019-20`) conviven con
  el torneo genérico. Un modelo correcto separaría `Competicion` y `Edicion`
  con una relación entre ambos.
- `langchain-experimental` está en proceso de retirada. Alternativa natural:
  `neo4j-graphrag`, mantenido por Neo4j y ya presente como dependencia.
- **El esquema no contempla cargos directivos.** Presidentes y directores
  deportivos acaban como `Entrenador` porque no hay un tipo mejor: es lo que
  produjo `Sacchi -ENTRENO_A-> Real Madrid`, donde fue director deportivo. Un
  esquema más maduro tendría `Directivo` con relaciones propias (`PRESIDIO`,
  `FUE_DIRECTOR_DEPORTIVO_DE`).
- **El esquema no distingue competiciones de premios individuales.** El Balón
  de Oro o el The Best acaban como `Competicion` porque no hay un tipo mejor.
  Un esquema más maduro tendría `Premio` con su relación `RECIBIO`, y
  separaría `Competicion` de `Edicion` para poder contar títulos por año.
- **Contar títulos no es viable desde este corpus.** Requiere una fuente
  estructurada (Wikidata) en lugar de biografías en prosa extraídas por un
  modelo pequeño.
- La resolución de entidades depende de un diccionario de alias mantenido a
  mano para lo que ninguna métrica puede deducir: `PSG` = `Paris
  Saint-Germain`, `Fráncfort` = `Frankfurt`, `Champions League` = `Liga de
  Campeones`. Eso no es un fallo del sistema, es la naturaleza del problema.

---

## Pendiente

- [ ] `GraphCypherQAChain` para preguntas agregativas ("cuántos", "quién más")
- [ ] API con FastAPI
- [ ] Paso de verificación: segunda pasada del LLM validando cada relación
      contra su fragmento de origen
- [ ] Migrar de `langchain-experimental` a `neo4j-graphrag`

---

## Estructura

```
graphrag-futbol/
├── docker-compose.yml
├── requirements.txt
├── .env.example
├── data/
│   ├── corpus/           # artículos descargados
│   └── merges.json       # decisiones de fusión revisadas
├── docs/                 # publicado en GitHub Pages
│   └── index.html        # visualización interactiva
└── scripts/
    ├── clients.py        # conexiones a Neo4j y Ollama (y su verificación)
    ├── names.py          # normalización de nombres de entidad
    ├── schema.py         # datos: tipos permitidos e instrucciones
    ├── ingest.py         # descarga del corpus + extracción + carga
    ├── dedupe.py         # resolución de entidades automática
    ├── curate.py         # decisiones humanas: alias y relaciones falsas
    ├── evaluate.py       # precisión y cobertura contra Wikidata
    ├── query.py          # índice vectorial + preguntas en lenguaje natural
    └── build_site.py     # genera docs/index.html
```

`schema.py` y el generado `ground_truth.py` son **datos**, no lógica: definen
qué puede extraer el modelo y qué sabemos que es cierto. Separarlos permite
iterar sobre ellos sin tocar el código que los consume.

El código está en inglés (convención del sector y del ecosistema de
dependencias); los comentarios y esta documentación, en español.

---

## Licencia

Código bajo MIT. El corpus procede de Wikipedia en español (CC BY-SA 4.0).