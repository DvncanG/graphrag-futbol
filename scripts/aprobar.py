"""
Aplica las decisiones de revisión sobre data/merges.json.

POR QUE EXISTE
`--proponer` regenera merges.json desde cero y se lleva por delante las
ediciones manuales. Si vuelves a proponer despues de haber aprobado cosas,
pierdes el trabajo. Este script deja las decisiones en codigo, asi que
recuperarlas es relanzarlo.

Hace dos cosas:
  1. Pone "aprobado": true en los grupos cuyo canonico este en APROBAR
  2. Anade los grupos MANUALES, que ningun detector puede encontrar

Uso:
    python scripts/aprobar.py
"""

import json
import sys
from pathlib import Path

RUTA = Path(__file__).parent.parent / "data" / "merges.json"

# Grupos propuestos por los detectores que hemos verificado como correctos.
# Se identifican por su "canonico".
APROBAR = {
    "Johan Cruyff",            # apellido: Cruyff -> Johan Cruyff
    "Ajax de Ámsterdam",       # prefijo:  Ajax
    "Atlético de Madrid",      # prefijo:  Atlético
    "Liga española de fútbol", # prefijo:  Liga española
}

# Casos que ningun detector de cadenas puede encontrar, porque el parecido
# no esta en las letras sino en el conocimiento del dominio.
MANUALES = [
    {
        "detector": "manual",
        "tipo": "Entrenador",
        "canonico": "Marcelo Bielsa",
        "variantes": ["Marcelo Bielsa", "Marcelo Alberto Bielsa Caldera"],
        "aprobado": True,
        "nota": "nombre completo con segundo apellido",
    },
    {
        "detector": "manual",
        "tipo": "Entrenador",
        "canonico": "José Mourinho",
        "variantes": [
            "José Mourinho",
            "José Mário dos Santos Mourinho Félix",
            "Mourinho",
        ],
        "aprobado": True,
        "nota": "nombre portugues completo: acaba en Felix, no en Mourinho",
    },
    {
        "detector": "manual",
        "tipo": "Entrenador",
        "canonico": "Pep Guardiola",
        "variantes": ["Pep Guardiola", "Josep Guardiola", "Guardiola"],
        "aprobado": True,
        "nota": "Pep es hipocoristico de Josep: cero parecido de cadena",
    },
]

# Grupos que los detectores proponen pero que NO deben fusionarse.
# Se listan para dejar constancia de la decision, no solo del olvido.
RECHAZADOS = {
    "Eintracht Fráncfort II": "el II es el filial, otro equipo",
    "Recopa de Europa": "distinta de la Copa de Europa",
    "Ronaldo": "Rivaldo es otro jugador",
    "Copa Intercontinental de la FIFA 2024": "distinta de la historica",
    "Liga Europa Conferencia": "distinta de la Liga Europa",
    "Campeonato Europeo de Fútbol (UEFA Euro) 2012": "ediciones distintas",
    "Copa América 1993": "ediciones distintas",
    "Copa Confederaciones 2013": "ediciones distintas",
    "Liga de las temporadas 1998-1999": "temporadas distintas",
    "UEFA Champions League 2019": "ediciones distintas",
    "Copa de Francia 2021": "edicion concreta, no el torneo",
    "Copa de la UEFA 2005-06": "edicion concreta, no el torneo",
    "Premier League 2019-20": "edicion concreta, no el torneo",
    "Supercopa de Europa 2019-20": "edicion concreta, no el torneo",
    "Supercopa de Francia 2021": "edicion concreta, no el torneo",
}


def main() -> int:
    if not RUTA.exists():
        print(f"No existe {RUTA}. Ejecuta antes --proponer.")
        return 1

    grupos = json.loads(RUTA.read_text(encoding="utf-8"))
    canonicos = {g["canonico"] for g in grupos}

    # Aviso si algo que esperabamos aprobar ya no esta en el fichero:
    # significa que ese duplicado ya se fusiono en una ronda anterior.
    for c in APROBAR - canonicos:
        print(f"  aviso: '{c}' no esta en merges.json (ya fusionado?)")

    aprobados = 0
    for g in grupos:
        # Los de 'normalizacion' se aprueban en bloque: la clave normalizada
        # es igualdad exacta, no parecido, y en la revision manual no dio
        # ni un falso positivo. Identificarlos por 'canonico' era fragil,
        # porque ese nombre lo elige el propio script y cambia entre ingestas.
        aprobar = g["detector"] == "normalizacion" or g["canonico"] in APROBAR
        g["aprobado"] = aprobar
        aprobados += aprobar

    # Evita duplicar los manuales si el script se ejecuta dos veces
    existentes = {g["canonico"] for g in grupos if g["detector"] == "manual"}
    nuevos = [m for m in MANUALES if m["canonico"] not in existentes]
    grupos.extend(nuevos)

    RUTA.write_text(json.dumps(grupos, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n{aprobados} grupos de detector aprobados")
    print(f"{len(nuevos)} grupos manuales anadidos")
    print(f"{len(grupos)} grupos en total, "
          f"{sum(1 for g in grupos if g['aprobado'])} aprobados\n")
    for g in grupos:
        if g["aprobado"]:
            print(f"  [{g['detector']}] {' | '.join(g['variantes'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())