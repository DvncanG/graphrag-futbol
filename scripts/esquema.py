"""
Esquema cerrado del grafo.

Este es EL fichero del proyecto. Si lo dejas abierto, el LLM inventa un tipo
de nodo distinto cada vez que ve el mismo concepto: "Entrenador", "Tecnico",
"Manager", "DirectorTecnico"... y el grafo queda inservible porque nada
conecta con nada.

Al cerrarlo, obligamos al modelo a encajar todo lo que lee en estas cajas.
Extrae menos, pero lo que extrae es consistente y consultable.

Regla practica: empieza con POCOS tipos. Es mas facil anadir uno que limpiar
un grafo lleno de duplicados semanticos.
"""

NODOS_PERMITIDOS = [
    "Entrenador",
    "Jugador",
    "Club",
    "Seleccion",
    "Competicion",
    "EstiloJuego",
]

# Tuplas (origen, RELACION, destino). Restringir tambien los extremos evita
# barbaridades como (Competicion)-[ENTRENO_A]->(Jugador).
RELACIONES_PERMITIDAS = [
    ("Entrenador", "ENTRENO_A", "Club"),
    ("Entrenador", "ENTRENO_A", "Seleccion"),
    ("Entrenador", "INFLUYO_EN", "Entrenador"),
    ("Entrenador", "FUE_ASISTENTE_DE", "Entrenador"),
    ("Entrenador", "PRACTICA", "EstiloJuego"),
    ("Jugador", "JUGO_EN", "Club"),
    ("Jugador", "ENTRENADO_POR", "Entrenador"),
    ("Club", "GANO", "Competicion"),
    # Separado por rol: casi todos los entrenadores de elite fueron antes
    # jugadores, y "cuantas Champions tiene Ancelotti" tiene dos respuestas
    # distintas (2 como jugador, 5 como entrenador). Con un unico GANO la
    # pregunta es irresoluble.
    ("Entrenador", "GANO_COMO_ENTRENADOR", "Competicion"),
    ("Jugador", "GANO_COMO_JUGADOR", "Competicion"),
]

# SIN USO ACTUALMENTE. node_properties exige function calling nativo, y
# nosotros extraemos por prompt (ignore_tool_usage=True) porque es la unica
# forma de que Ollama respete el esquema. Se queda aqui por si algun dia
# migramos a un modelo por API, donde si se podria activar.
PROPIEDADES_NODO = ["nacionalidad", "anio_inicio", "anio_fin"]

# Se inyecta en el prompt de extraccion. Aqui es donde se corrigen los fallos
# que veas al inspeccionar el grafo: si el modelo confunde jugar con entrenar,
# lo dices explicitamente aqui.
INSTRUCCIONES_EXTRA = """
Estas analizando biografias de entrenadores de futbol en espanol.

Reglas estrictas:
- Un mismo nombre puede ser Jugador y Entrenador en etapas distintas. Usa
  Entrenador solo cuando el texto hable de su etapa dirigiendo equipos.
- INFLUYO_EN va SIEMPRE de persona a persona, nunca a un estilo de juego.
  Para vincular un entrenador con un estilo usa PRACTICA.
- INFLUYO_EN solo si el texto afirma explicitamente una influencia, mentoria
  o discipulado. No lo deduzcas por haber coincidido en un club.
- Cuidado con la direccion de INFLUYO_EN: si el texto dice que A se inspiro
  en B, o que A admiraba a B, la relacion es (B)-[INFLUYO_EN]->(A).
  El origen es quien influye, no quien recibe la influencia.
- FUE_ASISTENTE_DE exige un cargo formal de segundo entrenador o ayudante,
  afirmado literalmente en el texto. Inspirarse en alguien, admirarlo o
  copiar su estilo NO es haber sido su asistente. Ante la duda, usa
  INFLUYO_EN o no extraigas nada.
- Escribe los nombres propios con su capitalizacion normal: "AC Milan",
  no "Ac Milan". Los estilos de juego en minuscula: "futbol total".
- EstiloJuego para conceptos tacticos con nombre propio: futbol total,
  tiki-taka, gegenpressing, catenaccio, juego de posicion.
- Usa siempre el nombre mas completo y canonico de la entidad. "Pep" y
  "Guardiola" son el mismo nodo: Pep Guardiola.
- Los titulos SIEMPRE se atribuyen con rol. Las secciones de palmares de
  Wikipedia suelen venir separadas en "Como jugador" y "Como entrenador":
  respeta esa division. Si el texto describe la etapa de entrenador, usa
  GANO_COMO_ENTRENADOR; si describe la de jugador, GANO_COMO_JUGADOR.
  Si el rol no se puede determinar con seguridad, NO extraigas el titulo.
- NO incluyas anos ni temporadas en el nombre de la competicion: escribe
  "Liga de Campeones de la UEFA", nunca "Liga de Campeones 2014". Probado:
  el modelo desplaza las fechas y triplica los nodos sin ganar precision.
- Extrae un titulo SOLO si se gano. Subcampeonatos, finales perdidas y
  puestos en la clasificacion NO son victorias.
- Si un fragmento no contiene ninguna relacion clara, no extraigas nada.
  Es preferible un grafo pequeno y correcto a uno grande y sucio.
"""