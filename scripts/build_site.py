"""
Exporta el grafo a una pagina web interactiva de un solo fichero.

Genera docs/index.html con los datos embebidos. No necesita servidor: se
abre con doble click, y al estar en docs/ se puede publicar directo en
GitHub Pages (Settings -> Pages -> Deploy from branch -> /docs).

Que incluye:
  - grafo navegable (arrastrar, zoom, click)
  - buscador de entidades
  - camino mas corto entre dos personas, calculado en el navegador
  - filtros por tipo de relacion

Uso:
    python scripts/build_site.py
"""

import json
import sys
from pathlib import Path

from clients import neo4j_config
from dotenv import load_dotenv
from langchain_neo4j import Neo4jGraph

load_dotenv()

OUTPUT = Path(__file__).parent.parent / "docs" / "index.html"

# Solo relaciones de carrera e influencia. Las competiciones se dejan fuera
# a proposito: son cientos de nodos de grado 1 que convierten el grafo en
# una maranya ilegible sin aportar nada a la historia de los linajes.
EXPORTED_RELS = [
    "ENTRENO_A",
    "JUGO_EN",
    "ENTRENADO_POR",
    "INFLUYO_EN",
    "FUE_ASISTENTE_DE",
]

SPARQL_QUERY = f"""
MATCH (a)-[r:{"|".join(EXPORTED_RELS)}]->(b)
WHERE NOT a:Document AND NOT b:Document
RETURN a.id AS origen,  [l IN labels(a) WHERE l <> '__Entity__'][0] AS tipo_origen,
       b.id AS destino, [l IN labels(b) WHERE l <> '__Entity__'][0] AS tipo_destino,
       type(r) AS relacion
"""

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Linajes del banquillo</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Oswald:wght@300;500;700&family=Barlow:wght@400;500;600&display=swap" rel="stylesheet">
<script src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
<style>
:root{
  --pizarra:#101E18; --pizarra-2:#16281F; --panel:#0C1813;
  --tiza:#EDEAE0; --tiza-3:rgba(237,234,224,.42); --linea:rgba(237,234,224,.14);
  --ambar:#E8A33D; --azul:#6F9BD1; --violeta:#B98BD9; --verde:#8FA89B;
  --marca:#F5D547;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{
  background:var(--pizarra); color:var(--tiza);
  font-family:'Barlow',system-ui,sans-serif;
  display:grid; grid-template-columns:320px 1fr; overflow:hidden;
}
/* Marcas de campo: el fondo es una pizarra tactica, no un lienzo neutro */
#lienzo::before{
  content:''; position:absolute; inset:0; pointer-events:none; z-index:0;
  background:
    radial-gradient(circle at 50% 50%, transparent 118px, var(--linea) 118px,
                    var(--linea) 119px, transparent 119px),
    linear-gradient(to right, transparent calc(50% - .5px), var(--linea) calc(50% - .5px),
                    var(--linea) calc(50% + .5px), transparent calc(50% + .5px));
}
aside{
  background:var(--panel); border-right:1px solid var(--linea);
  padding:26px 22px; overflow-y:auto; z-index:2;
}
h1{
  font-family:'Oswald',sans-serif; font-weight:700; font-size:27px;
  letter-spacing:.055em; text-transform:uppercase; line-height:1.05;
}
h1 span{display:block; color:var(--marca); font-weight:300; font-size:15px;
  letter-spacing:.24em; margin-top:7px}
.sub{color:var(--tiza-3); font-size:13.5px; margin:14px 0 26px; line-height:1.55}
h2{
  font-family:'Oswald',sans-serif; font-weight:500; font-size:11.5px;
  letter-spacing:.19em; text-transform:uppercase; color:var(--tiza-3);
  margin:24px 0 11px; padding-bottom:7px; border-bottom:1px solid var(--linea);
}
input,select{
  width:100%; background:var(--pizarra-2); border:1px solid var(--linea);
  color:var(--tiza); padding:9px 11px; font-family:'Barlow',sans-serif;
  font-size:14px; border-radius:3px; margin-bottom:8px;
}
input:focus,select:focus{outline:2px solid var(--marca); outline-offset:1px}
button{
  width:100%; background:var(--marca); color:#101E18; border:0; padding:10px;
  font-family:'Oswald',sans-serif; font-weight:500; font-size:13px;
  letter-spacing:.11em; text-transform:uppercase; border-radius:3px;
  cursor:pointer;
}
button:hover{filter:brightness(1.12)}
button.fantasma{background:transparent; color:var(--tiza-3);
  border:1px solid var(--linea); margin-top:7px}
.rango{display:block; font-size:13px; color:var(--tiza-3); margin-bottom:12px}
.rango b{color:var(--marca); font-family:'Oswald',sans-serif}
.rango input[type=range]{width:100%; margin-top:6px; accent-color:var(--marca)}
.filtro{display:flex; align-items:center; gap:9px; padding:5px 0;
  font-size:13.5px; cursor:pointer}
.filtro input{width:auto; margin:0; accent-color:var(--marca)}
.clave{display:flex; align-items:center; gap:9px; padding:4px 0; font-size:13.5px}
.punto{width:11px; height:11px; border-radius:50%; flex:none}
#ruta{
  margin-top:13px; font-size:13.5px; line-height:1.65; color:var(--tiza-3);
  border-left:2px solid var(--marca); padding-left:12px; display:none;
}
#ruta b{color:var(--tiza); font-weight:600}
#ruta .rel{color:var(--marca); font-family:'Oswald',sans-serif; font-size:11px;
  letter-spacing:.09em}
#lienzo{position:relative; z-index:1}
#red{width:100%; height:100vh; position:relative; z-index:1}
#cifras{
  position:absolute; right:22px; bottom:20px; z-index:3; text-align:right;
  font-family:'Oswald',sans-serif; letter-spacing:.15em; font-size:11px;
  color:var(--tiza-3); text-transform:uppercase; line-height:1.9;
}
#cifras b{color:var(--marca); font-weight:500}
@media(max-width:860px){
  body{grid-template-columns:1fr; grid-template-rows:auto 1fr; overflow:auto}
  #red{height:65vh}
}
</style>
</head>
<body>
<aside>
  <h1>Linajes<br>del banquillo<span>Grafo de conocimiento</span></h1>
  <p class="sub">Construido con un LLM en local a partir de biografías de
  Wikipedia. Cada arista se extrajo de un texto y se verificó a mano.</p>

  <h2>Buscar</h2>
  <input id="buscar" list="entidades" placeholder="Cruyff, Barcelona…" autocomplete="off">
  <datalist id="entidades"></datalist>

  <h2>Trazar conexión</h2>
  <input id="desde" list="entidades" placeholder="Desde: Marcelo Bielsa" autocomplete="off">
  <input id="hasta" list="entidades" placeholder="Hasta: Mikel Arteta" autocomplete="off">
  <button id="trazar">Trazar</button>
  <button id="clean" class="fantasma">Ver todo</button>
  <div id="ruta"></div>

  <h2>Densidad</h2>
  <label class="rango">Conexiones mínimas: <b id="minVal">1</b>
    <input type="range" id="minGrado" min="1" max="6" value="1"></label>
  <label class="rango">Etiquetas
    <select id="etiquetas">
      <option value="clave">Solo nodos destacados</option>
      <option value="todas">Todas</option>
      <option value="ninguna">Ninguna</option>
    </select></label>

  <h2>Relaciones</h2>
  <div id="filtros"></div>

  <h2>Tipos</h2>
  <div id="match_key"></div>
</aside>

<div id="lienzo">
  <div id="red"></div>
  <div id="cifras"></div>
</div>

<script>
const DATOS = __DATOS__;

const COLOR = {
  Entrenador:'#EDEAE0', Jugador:'#8FA89B', Club:'#E8A33D',
  Seleccion:'#6F9BD1', EstiloJuego:'#B98BD9'
};
const ETIQUETA = {
  ENTRENO_A:'entrenó a', JUGO_EN:'jugó en', ENTRENADO_POR:'dirigido por',
  INFLUYO_EN:'influyó en', FUE_ASISTENTE_DE:'asistente de'
};
const MARCA = '#F5D547', TENUE = 'rgba(237,234,224,.10)';

const grados = {};
DATOS.aristas.forEach(a => {
  grados[a.from] = (grados[a.from]||0)+1;
  grados[a.to]   = (grados[a.to]||0)+1;
});

// Umbral por encima del cual un nodo se considera destacado y lleva
// etiqueta visible. Con 130 nodos, mostrarlas todas hace ilegible el grafo.
const DESTACADO = 3;
const etiqueta = n => (grados[n.id]||0) >= DESTACADO ? n.id : '';

const nodos = new vis.DataSet(DATOS.nodos.map(n => ({
  id:n.id, label:etiqueta(n), titulo:n.id, grupo:n.tipo,
  value: grados[n.id]||1,
  color:{background:COLOR[n.tipo]||'#8FA89B', border:COLOR[n.tipo]||'#8FA89B',
         highlight:{background:MARCA, border:MARCA}},
  font:{color:'#EDEAE0', size:13, face:'Barlow',
        strokeWidth:5, strokeColor:'#101E18'}
})));

const aristas = new vis.DataSet(DATOS.aristas.map((a,i) => ({
  id:i, from:a.from, to:a.to, tipo:a.tipo,
  color:{color:'rgba(237,234,224,.22)', highlight:MARCA},
  width:1, arrows:{to:{enabled:true, scaleFactor:.42}}
})));

const red = new vis.Network(document.getElementById('red'), {nodes:nodos, edges:aristas}, {
  nodes:{shape:'dot', scaling:{min:7, max:30}, borderWidth:0},
  edges:{smooth:{type:'continuous', roundness:.22}},
  physics:{barnesHut:{gravitationalConstant:-30000, centralGravity:.12,
                      springLength:250, springConstant:.02, damping:.55,
                      avoidOverlap:.55}, stabilization:{iterations:500}},
  interaction:{hover:true, tooltipDelay:120, navigationButtons:false}
});

document.getElementById('cifras').innerHTML =
  `<b>${DATOS.nodos.length}</b> entidades<br><b>${DATOS.aristas.length}</b> relaciones`;

// Listas y leyenda
const lista = document.getElementById('entidades');
DATOS.nodos.map(n=>n.id).sort().forEach(id => {
  const o = document.createElement('option'); o.value = id; lista.appendChild(o);
});
document.getElementById('match_key').innerHTML = Object.entries(COLOR)
  .map(([t,c]) => `<div class="match_key"><span class="punto" style="background:${c}"></span>${t}</div>`)
  .join('');
document.getElementById('filtros').innerHTML = Object.entries(ETIQUETA)
  .map(([t,e]) => `<label class="filtro"><input type="checkbox" data-tipo="${t}" checked>${e}</label>`)
  .join('');

document.querySelectorAll('#filtros input').forEach(c => c.addEventListener('change', () => {
  const activos = new Set([...document.querySelectorAll('#filtros input:checked')]
    .map(i => i.dataset.tipo));
  aristas.update(aristas.get().map(a => ({
    id:a.id, hidden: !activos.has(a.tipo)
  })));
}));

// Densidad: oculta los nodos con pocas conexiones. Los nodos de grado 1
// son la mayoria y aportan poco al mapa de linajes.
function aplicarDensidad(){
  const min = +document.getElementById('minGrado').value;
  document.getElementById('minVal').textContent = min;
  nodos.update(DATOS.nodos.map(n => ({
    id:n.id, hidden:(grados[n.id]||0) < min
  })));
}
document.getElementById('minGrado').addEventListener('input', aplicarDensidad);

document.getElementById('etiquetas').addEventListener('change', e => {
  const modo = e.target.value;
  nodos.update(DATOS.nodos.map(n => ({
    id:n.id,
    label: modo === 'todas' ? n.id
         : modo === 'ninguna' ? ''
         : etiqueta(n)
  })));
});

// Buscar: centra y selecciona
document.getElementById('buscar').addEventListener('change', e => {
  const id = e.target.value;
  if (!nodos.get(id)) return;
  red.focus(id, {scale:1.4, animation:{duration:520}});
  red.selectNodes([id]);
});

// Camino mas corto: BFS sobre la lista de adyacencia, en el navegador
const vecinos = {};
DATOS.aristas.forEach(a => {
  (vecinos[a.from] = vecinos[a.from]||[]).push({n:a.to, tipo:a.tipo, dir:1});
  (vecinos[a.to]   = vecinos[a.to]||[]).push({n:a.from, tipo:a.tipo, dir:-1});
});

function buscarCamino(a, b){
  if (a === b || !vecinos[a] || !vecinos[b]) return null;
  const previo = {[a]:null}, cola = [a];
  while (cola.length){
    const act = cola.shift();
    if (act === b) break;
    for (const v of (vecinos[act]||[])){
      if (previo[v.n] !== undefined) continue;
      previo[v.n] = {de:act, tipo:v.tipo, dir:v.dir};
      cola.push(v.n);
    }
  }
  if (previo[b] === undefined) return null;
  const pasos = [];
  let act = b;
  while (previo[act]){
    pasos.unshift({de:previo[act].de, a:act, tipo:previo[act].tipo, dir:previo[act].dir});
    act = previo[act].de;
  }
  return pasos;
}

function resaltar(pasos){
  const enRuta = new Set(), aristasRuta = new Set();
  pasos.forEach(p => { enRuta.add(p.de); enRuta.add(p.a); });
  DATOS.aristas.forEach((a,i) => {
    if (pasos.some(p => (p.de===a.from && p.a===a.to) || (p.de===a.to && p.a===a.from)))
      aristasRuta.add(i);
  });
  nodos.update(DATOS.nodos.map(n => ({
    id:n.id,
    color: enRuta.has(n.id)
      ? {background:COLOR[n.tipo]||'#8FA89B', border:MARCA}
      : {background:TENUE, border:TENUE},
    borderWidth: enRuta.has(n.id) ? 3 : 0,
    font:{color: enRuta.has(n.id) ? '#EDEAE0' : 'rgba(237,234,224,.18)',
          size:13, face:'Barlow', strokeWidth:4, strokeColor:'#101E18'}
  })));
  aristas.update(aristas.get().map(a => ({
    id:a.id,
    color:{color: aristasRuta.has(a.id) ? MARCA : 'rgba(237,234,224,.05)'},
    width: aristasRuta.has(a.id) ? 3 : 1
  })));
  red.fit({nodes:[...enRuta], animation:{duration:620}});
}

function verTodo(){
  nodos.update(DATOS.nodos.map(n => ({
    id:n.id,
    color:{background:COLOR[n.tipo]||'#8FA89B', border:COLOR[n.tipo]||'#8FA89B'},
    borderWidth:0,
    font:{color:'#EDEAE0', size:13, face:'Barlow', strokeWidth:4, strokeColor:'#101E18'}
  })));
  aristas.update(aristas.get().map(a => ({
    id:a.id, color:{color:'rgba(237,234,224,.22)'}, width:1
  })));
  document.getElementById('ruta').style.display = 'none';
  red.fit({animation:{duration:520}});
}

document.getElementById('trazar').addEventListener('click', () => {
  const a = document.getElementById('desde').value;
  const b = document.getElementById('hasta').value;
  const caja = document.getElementById('ruta');
  caja.style.display = 'block';

  if (!nodos.get(a) || !nodos.get(b)){
    caja.innerHTML = 'Escribe dos entidades que existan en el grafo. El desplegable las sugiere.';
    return;
  }
  const pasos = buscarCamino(a, b);
  if (!pasos){
    caja.innerHTML = `No hay ningún camino entre <b>${a}</b> y <b>${b}</b> con las relaciones del grafo.`;
    return;
  }
  caja.innerHTML = `<b>${pasos.length}</b> ${pasos.length===1?'salto':'saltos'}<br><br>` +
    pasos.map(p => {
      const [x,y] = p.dir===1 ? [p.de,p.a] : [p.a,p.de];
      return `<b>${x}</b> <span class="rel">${(ETIQUETA[p.tipo]||p.tipo).toUpperCase()}</span> <b>${y}</b>`;
    }).join('<br>');
  resaltar(pasos);
});

document.getElementById('clean').addEventListener('click', verTodo);

red.on('click', p => {
  if (!p.nodes.length) return;
  document.getElementById('buscar').value = p.nodes[0];
});
</script>
</body>
</html>
"""


def main() -> int:
    grafo = Neo4jGraph(**neo4j_config(), refresh_schema=False)

    filas = grafo.query(SPARQL_QUERY)
    if not filas:
        print("El grafo no tiene relaciones de carrera. Ejecuta ingest.py.")
        return 1

    tipos: dict[str, str] = {}
    aristas = []
    for f in filas:
        if not f["tipo_origen"] or not f["tipo_destino"]:
            continue
        tipos[f["origen"]] = f["tipo_origen"]
        tipos[f["destino"]] = f["tipo_destino"]
        aristas.append({"from": f["origen"], "to": f["destino"], "tipo": f["relacion"]})

    datos = {
        "nodos": [{"id": k, "tipo": v} for k, v in sorted(tipos.items())],
        "aristas": aristas,
    }

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        HTML_TEMPLATE.replace("__DATOS__", json.dumps(datos, ensure_ascii=False)),
        encoding="utf-8",
    )

    kb = OUTPUT.stat().st_size / 1024
    print(f"{len(datos['nodos'])} entidades, {len(aristas)} relaciones")
    print(f"-> {OUTPUT}  ({kb:.0f} KB)")
    print("\nAbrelo con doble click.")
    print("Para publicarlo: Settings -> Pages -> Deploy from branch -> main /docs")
    return 0


if __name__ == "__main__":
    sys.exit(main())