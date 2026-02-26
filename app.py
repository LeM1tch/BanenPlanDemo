"""
Banenplan Service — extraheert kamervormen uit PDF en genereert SVG banenplannen
Deploy op Railway, Render of Fly.io. Wordt aangeroepen vanuit n8n via HTTP Request node.
"""

from flask import Flask, request, jsonify, Response
import fitz  # PyMuPDF
import base64
import math
import json
import re
from weasyprint import HTML as WeasyHTML

app = Flask(__name__)

# ── CONSTANTEN ─────────────────────────────────────────────────────────────
PT_PER_M = 28.35        # A1 formaat, schaal 1:100
BAANBREEDTE = 2.0       # meter
SNIJVERLIES = 0.05      # 5%
MIN_AREA_PX = 8000      # minimale vlakgrootte in px² (filtert kleine details weg)

# Kleur → vloertype mapping (RGB 0-1 afgerond op 2 decimalen)
KLEUR_MAP = {
    (1.0, 0.5, 0.25):  'marmo 3759',
    (0.5, 1.0, 0.5):   'marmo 83285',
    (0.5, 1.0, 1.0):   'gietvloer',
    (1.0, 0.71, 0.5):  'marmo 3573',
    (0.0, 1.0, 0.25):  'sportvloer 6011',
    (0.0, 0.5, 1.0):   'gietvloer',
    (0.5, 0.25, 0.0):  'Coral Brush 5774',
    (1.0, 1.0, 0.5):   'marmo 3759',
    (0.75, 0.5, 1.0):  'marmo 3573',
}

# Vloertype → display kleur voor SVG
VLOER_KLEUR = {
    'marmo 3759':     {'bg': '#FFF3E0', 'baan1': '#FFE0B2', 'baan2': '#FFCC80', 'rand': '#E65100'},
    'marmo 83285':    {'bg': '#E8F5E9', 'baan1': '#C8E6C9', 'baan2': '#A5D6A7', 'rand': '#2E7D32'},
    'marmo 3573':     {'bg': '#E3F2FD', 'baan1': '#BBDEFB', 'baan2': '#90CAF9', 'rand': '#1565C0'},
    'sportvloer 6011':{'bg': '#F3E5F5', 'baan1': '#E1BEE7', 'baan2': '#CE93D8', 'rand': '#6A1B9A'},
    'gietvloer':      {'bg': '#E0F7FA', 'baan1': '#B2EBF2', 'baan2': '#80DEEA', 'rand': '#006064'},
    'Coral Brush 5774':{'bg': '#FCE4EC','baan1': '#F8BBD9', 'baan2': '#F48FB1', 'rand': '#880E4F'},
}
DEFAULT_KLEUR = {'bg': '#F5F5F5', 'baan1': '#EEEEEE', 'baan2': '#E0E0E0', 'rand': '#424242'}


# ── POLYGON HELPERS ────────────────────────────────────────────────────────

def polygon_area(punten):
    """Shoelace formule voor oppervlakte polygoon."""
    n = len(punten)
    area = 0
    for i in range(n):
        j = (i + 1) % n
        area += punten[i][0] * punten[j][1]
        area -= punten[j][0] * punten[i][1]
    return abs(area) / 2


def clip_lijn_aan_polygoon(y, punten):
    """
    Geeft de x-segmenten waar horizontale lijn y het polygoon kruist.
    Gebruikt ray casting / scanline intersection.
    Returns: lijst van (x_start, x_end) paren (gesorteerd)
    """
    n = len(punten)
    xs = []
    for i in range(n):
        x1, y1 = punten[i]
        x2, y2 = punten[(i + 1) % n]
        # Check of lijn y het segment [y1,y2] kruist
        if (y1 <= y < y2) or (y2 <= y < y1):
            # Bereken x-kruispunt
            t = (y - y1) / (y2 - y1)
            x = x1 + t * (x2 - x1)
            xs.append(x)
    xs.sort()
    # Koppel xs in paren
    segmenten = []
    for i in range(0, len(xs) - 1, 2):
        segmenten.append((xs[i], xs[i + 1]))
    return segmenten


def punten_naar_svg_path(punten, scale, pad_x, pad_y, min_x, min_y):
    """Converteer punten lijst naar SVG path string."""
    parts = []
    for i, (x, y) in enumerate(punten):
        sx = (x - min_x) * scale + pad_x
        sy = (y - min_y) * scale + pad_y
        parts.append(f"{'M' if i == 0 else 'L'}{sx:.1f},{sy:.1f}")
    return ' '.join(parts) + ' Z'


# ── SVG GENERATOR ─────────────────────────────────────────────────────────

def genereer_banenplan_svg(ruimtenummer, naam, punten_m, vloertype,
                            netto_m2, bruto_m2, aantal_banen):
    """
    Genereert een SVG banenplan voor één kamer.
    Gebruikt exacte polygoonvorm met scanline banen.
    """
    if not punten_m or len(punten_m) < 3:
        return None

    kleur = VLOER_KLEUR.get(vloertype, DEFAULT_KLEUR)

    xs = [p[0] for p in punten_m]
    ys = [p[1] for p in punten_m]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    breedte = max_x - min_x
    hoogte = max_y - min_y

    # Schaal zodat SVG maximaal 400px breed of 500px hoog wordt
    max_w, max_h = 400, 480
    scale = min(max_w / breedte if breedte > 0 else max_w,
                max_h / hoogte if hoogte > 0 else max_h)
    scale = max(scale, 15)  # minimaal 15px/m voor leesbaarheid

    PAD_X, PAD_Y = 30, 30
    SVG_W = breedte * scale + PAD_X * 2
    SVG_H = hoogte * scale + PAD_Y * 2 + 50  # 50 voor legenda onderaan

    # Polygoon path
    poly_path = punten_naar_svg_path(punten_m, scale, PAD_X, PAD_Y, min_x, min_y)

    # Unieke clip-path ID per ruimte
    clip_id = f"clip_{ruimtenummer.replace('.', '_')}"

    # ── Scanline banen ──────────────────────────────────────────────────
    baan_rects = []
    baan_labels = []
    baan_nr = 1

    # Banen lopen langs de Y-as (kortste as = breedte, langste = legrichting)
    # We bepalen legrichting: banen langs langste wand = strepen langs kortste dimensie
    if breedte >= hoogte:
        # Banen verticaal (langs Y)
        staprichting = 'x'
        stap_start = min_x
        stap_einde = max_x
        scanmin = min_y
        scanmax = max_y
    else:
        # Banen horizontaal (langs X) — meest voorkomend
        staprichting = 'y'
        stap_start = min_y
        stap_einde = max_y
        scanmin = min_x
        scanmax = max_x

    baan_kleuren = [kleur['baan1'], kleur['baan2']]

    if staprichting == 'y':
        y = stap_start
        while y < stap_einde - 0.01:
            y_next = min(y + BAANBREEDTE, stap_einde)
            baan_kleur = baan_kleuren[(baan_nr - 1) % 2]

            # Meerdere y-samples voor nauwkeurige baan weergave
            for sample_y in [y + 0.01, y + BAANBREEDTE * 0.5, y_next - 0.01]:
                if sample_y >= stap_einde:
                    continue
                segs = clip_lijn_aan_polygoon(sample_y, punten_m)
                for (x0, x1) in segs:
                    rx = (x0 - min_x) * scale + PAD_X
                    ry = (y - min_y) * scale + PAD_Y
                    rw = (x1 - x0) * scale
                    rh = (y_next - y) * scale
                    if rw > 1:
                        baan_rects.append(
                            f'<rect x="{rx:.1f}" y="{ry:.1f}" width="{rw:.1f}" height="{rh:.1f}" '
                            f'fill="{baan_kleur}" opacity="0.85" clip-path="url(#{clip_id})"/>'
                        )

            # Baanlabel — midden van baan
            mid_segs = clip_lijn_aan_polygoon(y + BAANBREEDTE * 0.5, punten_m)
            if mid_segs:
                x0, x1 = mid_segs[0]
                lx = (x0 - min_x) * scale + PAD_X + 3
                ly = (y - min_y + BAANBREEDTE * 0.5) * scale + PAD_Y + 4
                baan_labels.append(
                    f'<text x="{lx:.1f}" y="{ly:.1f}" font-size="9" '
                    f'fill="#333" font-family="Arial" font-weight="bold">{baan_nr}</text>'
                )

            y = y_next
            baan_nr += 1

    else:  # staprichting == 'x'
        x = stap_start
        while x < stap_einde - 0.01:
            x_next = min(x + BAANBREEDTE, stap_einde)
            baan_kleur = baan_kleuren[(baan_nr - 1) % 2]

            # Transponeer de polygoon voor x-richting
            punten_t = [(p[1], p[0]) for p in punten_m]
            for sample_x in [x + 0.01, x + BAANBREEDTE * 0.5, x_next - 0.01]:
                if sample_x >= stap_einde:
                    continue
                segs = clip_lijn_aan_polygoon(sample_x, punten_t)
                for (y0, y1) in segs:
                    rx = (x - min_x) * scale + PAD_X
                    ry = (y0 - min_y) * scale + PAD_Y
                    rw = (x_next - x) * scale
                    rh = (y1 - y0) * scale
                    if rh > 1:
                        baan_rects.append(
                            f'<rect x="{rx:.1f}" y="{ry:.1f}" width="{rw:.1f}" height="{rh:.1f}" '
                            f'fill="{baan_kleur}" opacity="0.85" clip-path="url(#{clip_id})"/>'
                        )

            mid_segs = clip_lijn_aan_polygoon(x + BAANBREEDTE * 0.5, punten_t)
            if mid_segs:
                y0, y1 = mid_segs[0]
                lx = (x - min_x + BAANBREEDTE * 0.5) * scale + PAD_X
                ly = (y0 - min_y) * scale + PAD_Y + (y1 - y0) * scale * 0.5 + 4
                baan_labels.append(
                    f'<text x="{lx:.1f}" y="{ly:.1f}" font-size="9" '
                    f'fill="#333" font-family="Arial" font-weight="bold" '
                    f'text-anchor="middle">{baan_nr}</text>'
                )

            x = x_next
            baan_nr += 1

    # Rasterlijnen per baan
    raster_lijnen = []
    if staprichting == 'y':
        y = stap_start
        while y <= stap_einde + 0.01:
            ly = (y - min_y) * scale + PAD_Y
            raster_lijnen.append(
                f'<line x1="{PAD_X}" y1="{ly:.1f}" x2="{PAD_X + breedte*scale:.1f}" '
                f'y2="{ly:.1f}" stroke="{kleur["rand"]}" stroke-width="1.2" '
                f'stroke-dasharray="5,3" clip-path="url(#{clip_id})"/>'
            )
            y += BAANBREEDTE
    else:
        x = stap_start
        while x <= stap_einde + 0.01:
            lx = (x - min_x) * scale + PAD_X
            raster_lijnen.append(
                f'<line x1="{lx:.1f}" y1="{PAD_Y}" x2="{lx:.1f}" '
                f'y2="{PAD_Y + hoogte*scale:.1f}" stroke="{kleur["rand"]}" '
                f'stroke-width="1.2" stroke-dasharray="5,3" '
                f'clip-path="url(#{clip_id})"/>'
            )
            x += BAANBREEDTE

    # Maatpijlen (breedte en hoogte)
    pijl_kleur = kleur['rand']
    maat_svg = (
        f'<line x1="{PAD_X}" y1="{PAD_Y + hoogte*scale + 12}" '
        f'x2="{PAD_X + breedte*scale}" y2="{PAD_Y + hoogte*scale + 12}" '
        f'stroke="{pijl_kleur}" stroke-width="1.5" marker-end="url(#pijl)"/>'
        f'<text x="{PAD_X + breedte*scale/2}" y="{PAD_Y + hoogte*scale + 24}" '
        f'font-size="9" fill="{pijl_kleur}" text-anchor="middle" font-family="Arial">'
        f'{breedte:.1f}m</text>'
    )

    svg = f'''<svg width="{SVG_W:.0f}" height="{SVG_H:.0f}" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <clipPath id="{clip_id}">
      <path d="{poly_path}"/>
    </clipPath>
    <marker id="pijl" markerWidth="6" markerHeight="6" refX="6" refY="3" orient="auto">
      <path d="M0,0 L6,3 L0,6 Z" fill="{pijl_kleur}"/>
    </marker>
  </defs>

  <!-- Achtergrond kamer -->
  <path d="{poly_path}" fill="{kleur['bg']}" stroke="none"/>

  <!-- Banen geclipped -->
  {''.join(baan_rects)}

  <!-- Rasterlijnen -->
  {''.join(raster_lijnen)}

  <!-- Kamercontour -->
  <path d="{poly_path}" fill="none" stroke="{kleur['rand']}" stroke-width="2"/>

  <!-- Baannummers -->
  {''.join(baan_labels)}

  <!-- Maatpijl -->
  {maat_svg}

  <!-- Info label -->
  <text x="{PAD_X}" y="{SVG_H - 28}" font-size="10" font-weight="bold"
    fill="{kleur['rand']}" font-family="Arial">{ruimtenummer} {naam}</text>
  <text x="{PAD_X}" y="{SVG_H - 14}" font-size="9" fill="#555" font-family="Arial">
    {vloertype} | {netto_m2}m² netto | {aantal_banen} banen | {bruto_m2}m² bestellen</text>
</svg>'''

    return svg


# ── PDF POLYGON EXTRACTOR ─────────────────────────────────────────────────

def extraheer_polygonen(pdf_bytes):
    """
    Extraheert gekleurde kamervlakken uit PDF als polygonen in meters.
    Returns: dict van ruimtenummer -> {punten_m, vloertype, ...}
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = doc[0]
    paths = page.get_drawings()

    # Haal tekst labels op voor koppeling
    text_blocks = page.get_text('dict')['blocks']
    labels = []
    for b in text_blocks:
        if b['type'] == 0:
            for line in b['lines']:
                for span in line['spans']:
                    t = span['text'].strip()
                    if t:
                        labels.append({
                            'text': t,
                            'x': span['origin'][0],
                            'y': span['origin'][1]
                        })

    kamers = {}

    for p in paths:
        fill = p.get('fill')
        rect = p.get('rect')
        if not fill or not rect:
            continue

        fill_r = tuple(round(c, 2) for c in fill)
        if fill_r not in KLEUR_MAP:
            continue
        if fill_r == (1.0, 1.0, 1.0):  # wit = wand/achtergrond
            continue

        area_px = rect.width * rect.height
        if area_px < MIN_AREA_PX:
            continue

        # Haal polygon punten op
        items = p.get('items', [])
        punten_px = []
        for item in items:
            if item[0] == 'l':
                punten_px.append((item[1].x, item[1].y))
            elif item[0] == 're':
                r = item[1]
                punten_px += [(r.x0, r.y0), (r.x1, r.y0),
                               (r.x1, r.y1), (r.x0, r.y1)]

        if len(punten_px) < 3:
            continue

        # Dedupliceer aangrenzende punten
        uniek = [punten_px[0]]
        for pt in punten_px[1:]:
            if abs(pt[0] - uniek[-1][0]) > 0.5 or abs(pt[1] - uniek[-1][1]) > 0.5:
                uniek.append(pt)
        punten_px = uniek

        # Vind ruimtenummer via labels in dit vlak
        ruimtenummer = None
        vlak_labels = []
        for lbl in labels:
            if rect.x0 <= lbl['x'] <= rect.x1 and rect.y0 <= lbl['y'] <= rect.y1:
                vlak_labels.append(lbl['text'])
                if re.match(r'^\d+\.\d+$', lbl['text']) and not ruimtenummer:
                    ruimtenummer = lbl['text']

        if not ruimtenummer:
            continue

        # Converteer punten naar meters (relatief t.o.v. bounding box)
        min_x = min(pt[0] for pt in punten_px)
        min_y = min(pt[1] for pt in punten_px)
        punten_m = [
            (round((pt[0] - min_x) / PT_PER_M, 3),
             round((pt[1] - min_y) / PT_PER_M, 3))
            for pt in punten_px
        ]

        vloertype = KLEUR_MAP[fill_r]
        area_m2 = round(polygon_area(punten_m), 2)

        # Sla op — bij duplicaat ruimtenummer kies grootste vlak
        if ruimtenummer not in kamers or area_m2 > kamers[ruimtenummer]['area_m2']:
            kamers[ruimtenummer] = {
                'ruimtenummer': ruimtenummer,
                'vloertype': vloertype,
                'punten_m': punten_m,
                'area_m2': area_m2,
                'bounding_m': {
                    'breedte': round(rect.width / PT_PER_M, 2),
                    'hoogte': round(rect.height / PT_PER_M, 2)
                },
                'aantal_hoeken': len(punten_m),
                'is_l_vorm': len(punten_m) > 4,
                'labels': vlak_labels[:8]
            }

    doc.close()
    return kamers


# ── API ENDPOINTS ──────────────────────────────────────────────────────────

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'service': 'banenplan-service'})


@app.route('/polygonen', methods=['POST'])
def polygonen():
    """
    Extraheert polygonen uit PDF.
    Input: { "pdf_base64": "..." }
    Output: { "kamers": { "0.02": { punten_m, vloertype, ... }, ... } }
    """
    data = request.get_json()
    if not data or 'pdf_base64' not in data:
        return jsonify({'error': 'pdf_base64 vereist'}), 400

    pdf_bytes = base64.b64decode(data['pdf_base64'])
    kamers = extraheer_polygonen(pdf_bytes)
    return jsonify({'kamers': kamers, 'aantal': len(kamers)})


@app.route('/banenplan', methods=['POST'])
def banenplan():
    """
    Genereert SVG banenplannen op basis van:
    1. PDF binary (voor exacte polygonen)
    2. Claude JSON output (voor m², vloertype, banen)

    Input: {
      "pdf_base64": "...",
      "ruimtes": [ { ruimtenummer, naam, netto_m2, bruto_m2, aantal_banen, vloertype } ]
    }
    Output: {
      "svgs": { "0.02": "<svg>...</svg>", ... },
      "html_rapport": "<html>...</html>"
    }
    """
    data = request.get_json()
    if not data:
        return jsonify({'error': 'JSON body vereist'}), 400

    # Haal polygonen uit PDF
    pdf_bytes = base64.b64decode(data['pdf_base64'])
    polygonen_map = extraheer_polygonen(pdf_bytes)

    # Ruimtes array van Claude
    ruimtes = data.get('ruimtes', [])
    if not ruimtes:
        return jsonify({'error': 'ruimtes array vereist'}), 400

    svgs = {}
    gemiste_ruimtes = []

    for r in ruimtes:
        nr = r.get('ruimtenummer', '')
        naam = r.get('naam', '')
        netto = r.get('netto_m2', 0)
        bruto = r.get('bruto_m2', 0)
        banen = r.get('aantal_banen', 0)
        vloertype = r.get('vloertype', '')

        if nr in polygonen_map:
            poly = polygonen_map[nr]
            svg = genereer_banenplan_svg(
                ruimtenummer=nr,
                naam=naam,
                punten_m=poly['punten_m'],
                vloertype=vloertype or poly['vloertype'],
                netto_m2=netto,
                bruto_m2=bruto,
                aantal_banen=banen
            )
            if svg:
                svgs[nr] = svg
        else:
            gemiste_ruimtes.append(nr)

    # Genereer HTML rapport
    html = genereer_html_rapport(ruimtes, svgs)
    # Sla HTML ook op als apart endpoint beschikbaar
    app._last_html = html  # tijdelijk in memory voor /banenplan/html

    # Converteer naar PDF
    pdf_bytes = WeasyHTML(string=html).write_pdf()

    return Response(
        pdf_bytes,
        mimetype='application/pdf',
        headers={
            'Content-Disposition': 'attachment; filename="banenplan_rapport.pdf"',
        }
    )

def genereer_html_rapport(ruimtes, svgs):
    """Bouwt volledig HTML rapport met SVG banenplannen en uittrekstaat."""

    # Totalen
    totaal_netto = sum(r.get('netto_m2', 0) for r in ruimtes)
    totaal_bruto = sum(r.get('bruto_m2', 0) for r in ruimtes)

    # Per vloertype
    per_type = {}
    for r in ruimtes:
        vt = r.get('vloertype', 'onbekend')
        if vt not in per_type:
            per_type[vt] = {'netto': 0, 'bruto': 0, 'ruimtes': []}
        per_type[vt]['netto'] = round(per_type[vt]['netto'] + r.get('netto_m2', 0), 2)
        per_type[vt]['bruto'] = round(per_type[vt]['bruto'] + r.get('bruto_m2', 0), 2)
        per_type[vt]['ruimtes'].append(r.get('ruimtenummer', ''))

    # Uittrekstaat tabel
    rijen = ''
    for i, r in enumerate(ruimtes):
        bg = '#ffffff' if i % 2 == 0 else '#f1f8f1'
        rijen += f'''<tr style="background:{bg}">
          <td>{r.get("ruimtenummer","")}</td>
          <td>{r.get("naam","")}</td>
          <td>{r.get("vloertype","")}</td>
          <td style="text-align:center">{r.get("netto_m2","")}</td>
          <td style="text-align:center">{r.get("lengte_m","")}</td>
          <td style="text-align:center">{r.get("breedte_m","")}</td>
          <td style="text-align:center">{r.get("aantal_banen","")}</td>
          <td style="text-align:center;font-weight:bold">{r.get("bruto_m2","")}</td>
        </tr>'''

    # Vloertype samenvatting
    type_rijen = ''
    for vt, data in sorted(per_type.items(), key=lambda x: -x[1]['bruto']):
        kleur = VLOER_KLEUR.get(vt, DEFAULT_KLEUR)
        type_rijen += f'''<tr>
          <td style="display:flex;align-items:center;gap:8px">
            <span style="display:inline-block;width:16px;height:16px;
              background:{kleur["baan1"]};border:2px solid {kleur["rand"]};
              border-radius:3px"></span>{vt}
          </td>
          <td style="text-align:center">{len(data["ruimtes"])}</td>
          <td style="text-align:center">{data["netto"]}</td>
          <td style="text-align:center;font-weight:bold">{data["bruto"]}</td>
        </tr>'''

    # SVG secties — sorteer op bruto m² (grootste eerst)
    ruimtes_gesorteerd = sorted(
        [r for r in ruimtes if r.get('ruimtenummer') in svgs],
        key=lambda x: -x.get('bruto_m2', 0)
    )

    svg_secties = ''
    for r in ruimtes_gesorteerd:
        nr = r.get('ruimtenummer', '')
        if nr not in svgs:
            continue
        kleur = VLOER_KLEUR.get(r.get('vloertype', ''), DEFAULT_KLEUR)
        svg_secties += f'''
        <div style="break-inside:avoid;margin-bottom:32px;
          border:1px solid {kleur["rand"]};border-radius:8px;
          overflow:hidden;display:inline-block;margin-right:24px;
          vertical-align:top">
          <div style="background:{kleur["rand"]};padding:8px 14px;color:white;
            font-weight:bold;font-size:13px;font-family:Arial">
            {nr} — {r.get("naam","")}
          </div>
          <div style="padding:12px;background:white">
            {svgs[nr]}
          </div>
        </div>'''

    html = f'''<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<style>
  body {{ font-family: Arial, sans-serif; margin: 40px; color: #222; }}
  h1 {{ color: #1B5E20; border-bottom: 3px solid #1B5E20; padding-bottom: 8px; }}
  h2 {{ color: #1B5E20; margin-top: 32px; }}
  table {{ width:100%; border-collapse:collapse; margin-top:12px; font-size:12px; }}
  th {{ background:#1B5E20; color:white; padding:8px; text-align:left; }}
  td {{ padding:7px 8px; border-bottom:1px solid #ddd; }}
  .totaal {{ background:#C8E6C9 !important; font-weight:bold; }}
  .kader {{ background:#E8F5E9; border-left:4px solid #1B5E20;
            padding:12px 16px; margin:16px 0; border-radius:4px; }}
  .meta {{ color:#666; font-size:11px; margin-top:6px; }}
</style></head><body>

<h1>&#x1F4CB; Banenplan Rapport — Automatisch Gegenereerd</h1>
<div class="meta">Baanbreedte: {BAANBREEDTE}m | Snijverlies: {SNIJVERLIES*100:.0f}% | AI-analyse via Claude + PyMuPDF</div>

<div class="kader">
  <strong>Totaal netto m²:</strong> {round(totaal_netto,2)} m²&nbsp;&nbsp;&nbsp;
  <strong>Totaal bruto bestellen:</strong> {round(totaal_bruto,2)} m²&nbsp;&nbsp;&nbsp;
  <strong>Ruimtes:</strong> {len(ruimtes)}&nbsp;&nbsp;&nbsp;
  <strong>Banenplannen gegenereerd:</strong> {len(svgs)}
</div>

<h2>&#x1F9F5; Overzicht per vloertype</h2>
<table>
  <tr><th>Vloertype</th><th>Ruimtes</th><th>Netto m²</th><th>Bruto m²</th></tr>
  {type_rijen}
</table>

<h2>&#x1F4D0; Banenplannen per ruimte</h2>
<div style="margin-top:16px">
  {svg_secties}
</div>

<h2>&#x1F4CB; Volledige uittrekstaat</h2>
<table>
  <tr>
    <th>Nr.</th><th>Ruimtenaam</th><th>Vloertype</th>
    <th>Netto m²</th><th>Lengte m</th><th>Breedte m</th>
    <th>Banen</th><th>Bruto m²</th>
  </tr>
  {rijen}
  <tr class="totaal">
    <td colspan="3">TOTAAL</td>
    <td style="text-align:center">{round(totaal_netto,2)}</td>
    <td></td><td></td><td></td>
    <td style="text-align:center">{round(totaal_bruto,2)}</td>
  </tr>
</table>

<p class="meta">Gegenereerd door Banenplan Service | Puur Vloeren Groep demo</p>
</body></html>'''

    return html
@app.route('/banenplan/html', methods=['POST'])
def banenplan_html():
    """Zelfde als /banenplan maar geeft HTML terug in plaats van PDF."""
    data = request.get_json(force=True, silent=True)
    if isinstance(data, str):
        import json as json_module
        data = json_module.loads(data)

    pdf_b64 = data.get('pdf_base64', '')
    if ',' in pdf_b64:
        pdf_b64 = pdf_b64.split(',', 1)[1]

    pdf_bytes = base64.b64decode(pdf_b64)
    polygonen_map = extraheer_polygonen(pdf_bytes)
    ruimtes = data.get('ruimtes', [])

    svgs = {}
    for r in ruimtes:
        nr = r.get('ruimtenummer', '')
        if nr in polygonen_map:
            poly = polygonen_map[nr]
            svg = genereer_banenplan_svg(
                ruimtenummer=nr,
                naam=r.get('naam', ''),
                punten_m=poly['punten_m'],
                vloertype=r.get('vloertype') or poly['vloertype'],
                netto_m2=r.get('netto_m2', 0),
                bruto_m2=r.get('bruto_m2', 0),
                aantal_banen=r.get('aantal_banen', 0)
            )
            if svg:
                svgs[nr] = svg

    html = genereer_html_rapport(ruimtes, svgs)

    return Response(
        html,
        mimetype='text/html',
        headers={'Content-Disposition': 'attachment; filename="banenplan_rapport.html"'}
    )

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=False)
