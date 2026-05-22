import re
from bs4 import BeautifulSoup


def heure_en_decimal(texte):
    texte = texte.strip()
    match = re.search(r'(\d+)h(\d+)?min', texte)
    if match:
        h = int(match.group(1))
        m = int(match.group(2)) if match.group(2) else 0
        return round(h + m / 60, 2)
    return 0.0


def parse_planning(filepath):
    with open(filepath, encoding="utf-8") as f:
        soup = BeautifulSoup(f, "html.parser")

    rows = soup.find_all("tr", class_="period-row")
    vus = set()
    employes = []

    for row in rows:
        cols = row.find_all("td")
        if len(cols) < 7:
            continue

        prenom = cols[0].get_text(strip=True).replace("▾", "").strip()
        if prenom in vus:
            continue
        vus.add(prenom)

        trame      = heure_en_decimal(cols[1].get_text(strip=True))
        travailles = heure_en_decimal(cols[2].get_text(strip=True))
        ferie      = heure_en_decimal(cols[3].get_text(strip=True))
        absences   = cols[4].get_text(separator=" ", strip=True)
        total      = heure_en_decimal(cols[5].get_text(strip=True))
        heures_sup = heure_en_decimal(cols[6].get_text(strip=True))

        employes.append({
            "prenom":     prenom,
            "trame":      trame,
            "travailles": travailles,
            "ferie":      ferie,
            "absences":   absences,
            "total":      total,
            "heures_sup": heures_sup,
        })

    return employes
