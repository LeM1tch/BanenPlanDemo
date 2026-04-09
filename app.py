"""
Woningbouw Plattegrond Extractie Service
Genereert PDF rapporten met SVG plattegronden per woning.
Deploy op Railway, Render of Fly.io. Wordt aangeroepen vanuit n8n.

Verschil met originele banenplan app.py:
- Geen KLEUR_MAP / extraheer_polygonen (geen gekleurde vlakken in woningbouw PDFs)
- Polygonen komen van Claude Vision (via n8n), niet van PyMuPDF kleurdetectie
- Rapport is gegroepeerd per woning (niet per vloertype)
- SVG toont kamerindeling als gekleurde polygonen (niet banenplannen)
"""

from flask import Flask, request, jsonify, Response
import math
import json
from weasyprint import HTML as WeasyHTML

app = Flask(__name__)

# ── KLEUREN PER RUIMTETYPE ───────────────────────────────────────────────
KAMER_KLEUREN = {
    'Woonkamer+keuken': {'bg': '#dbeafe', 'border': '#3b82f6', 'label': 'Woonkamer+keuken'},
    'Slaapkamer 1':     {'bg': '#e0e7ff', 'border': '#6366f1', 'label': 'Slpk. 1'},
    'Slaapkamer 2':     {'bg': '#ede9fe', 'border': '#8b5cf6', 'label': 'Slpk. 2'},
    'Slaapkamer 3':     {'bg': '#f3e8ff', 'border': '#a855f7', 'label': 'Slpk. 3'},
    'Hobbykamer':       {'bg': '#fce7f3', 'border': '#ec4899', 'label': 'Hobby'},
    'Badkamer':         {'bg': '#cffafe', 'border': '#06b6d4', 'label': 'Badk.'},
    'WC':               {'bg': '#dcfce7', 'border': '#22c55e', 'label': 'WC'},
    'CV/MV/W':          {'bg': '#fef3c7', 'border': '#f59e0b', 'label': 'CV/MV/W'},
    'vkr':              {'bg': '#e2e8f0', 'border': '#475569', 'label': 'VKR'},
    'Berging':          {'bg': '#fef9c3', 'border': '#ca8a04', 'label': 'Berg.'},
    'Loggia':           {'bg': '#ecfdf5', 'border': '#10b981', 'label': 'Loggia'},
}
DEFAULT_KAMER_KLEUR = {'bg': '#f1f5f9', 'border': '#94a3b8', 'label': '?'}

WONING_TYPE_KLEUREN = {
    'type A':  '#3b82f6',
    'type B':  '#10b981',
    'type B1': '#8b5cf6',
    'type C':  '#f97316',
    'type C1': '#ef4444',
    'type C2': '#eab308',
}


# ── SVG POLYGON HELPERS ──────────────────────────────────────────────────

def polygon_centroid(punten):
    """Bereken zwaartepunt van polygoon."""
    n = len(punten)
    if n == 0:
        return (50, 50)
    return (sum(p[0] for p in punten) / n, sum(p[1] for p in punten) / n)


def polygon_to_svg_points(punten, scale_x, scale_y, offset_x, offset_y):
    """Converteer genormaliseerde punten (0-100) naar SVG path string."""
    parts = []
    for i, (x, y) in enumerate(punten):
        sx = offset_x + x * scale_x
        sy = offset_y + y * scale_y
        cmd = 'M' if i == 0 else 'L'
        parts.append(f'{cmd}{sx:.1f},{sy:.1f}')
    return ' '.join(parts) + ' Z'


def genereer_woning_svg(woning, svg_w=320, svg_h=220):
    """
    Genereert een SVG plattegrond voor één woning.
    Gebruikt polygoon-coördinaten uit Claude Vision analyse.
    Fallback: rechthoekige grid-layout op basis van m² verhoudingen.
    """
    kamers = woning.get('kamers', [])
    voids = woning.get('voids', [])

    if not kamers:
        return None

    pad = 8
    inner_w = svg_w - pad * 2
    inner_h = svg_h - pad * 2
    scale_x = inner_w / 100.0
    scale_y = inner_h / 100.0

    svg_parts = [
        f'<svg width="{svg_w}" height="{svg_h}" xmlns="http://www.w3.org/2000/svg">',
        f'<rect width="{svg_w}" height="{svg_h}" fill="#f8fafc" rx="6" '
        f'stroke="#cbd5e1" stroke-width="0.8"/>',
        # Hatch pattern voor loze ruimtes
        '<defs><pattern id="hatch" patternUnits="userSpaceOnUse" width="6" height="6" '
        'patternTransform="rotate(45)">'
        '<line x1="0" y1="0" x2="0" y2="6" stroke="#94a3b8" stroke-width="1" opacity="0.4"/>'
        '</pattern></defs>',
    ]

    # Teken loze ruimtes (gearceerd)
    for void_poly in voids:
        if not void_poly or len(void_poly) < 3:
            continue
        path = polygon_to_svg_points(void_poly, scale_x, scale_y, pad, pad)
        svg_parts.append(
            f'<path d="{path}" fill="#f1f5f9" stroke="#cbd5e1" '
            f'stroke-width="0.8" stroke-dasharray="3,2"/>'
        )
        svg_parts.append(f'<path d="{path}" fill="url(#hatch)"/>')

    # Teken kamers
    for kamer in kamers:
        naam = kamer.get('naam', '')
        m2 = kamer.get('m2', 0)
        poly = kamer.get('polygoon', [])
        kleur = KAMER_KLEUREN.get(naam, DEFAULT_KAMER_KLEUR)

        if not poly or len(poly) < 3:
            # Geen polygoon data — sla over (of gebruik fallback)
            continue

        path = polygon_to_svg_points(poly, scale_x, scale_y, pad, pad)

        # Kamer vulling + rand
        svg_parts.append(
            f'<path d="{path}" fill="{kleur["bg"]}" stroke="{kleur["border"]}" '
            f'stroke-width="1.8" stroke-linejoin="round"/>'
        )

        # Label positionering
        cx, cy = polygon_centroid(poly)
        tx = pad + cx * scale_x
        ty = pad + cy * scale_y

        # Bereken breedte/hoogte voor font sizing
        xs = [p[0] for p in poly]
        ys = [p[1] for p in poly]
        rw = (max(xs) - min(xs)) * scale_x
        rh = (max(ys) - min(ys)) * scale_y

        # Speciale label-positie voor L-vormige VKR
        if naam == 'vkr' and rw > rh * 2:
            # VKR is breed — zet label in het bredere deel
            cx_adj = (max(xs) + min(xs) + max(xs)) / 3  # bias naar rechts
            tx = pad + cx_adj * scale_x

        # Label tekst
        short_name = kleur.get('label', naam[:8])
        name_lines = short_name.split('+')
        if naam == 'Woonkamer+keuken':
            name_lines = ['Woonkamer', '+keuken']

        if rw > 24 and rh > 14:
            fs = min(10, rw / 7, rh / 4)
            fs = max(fs, 4.5)
            total_h = fs * (len(name_lines) + 1)
            start_y = ty - total_h / 2 + fs

            for i, line in enumerate(name_lines):
                svg_parts.append(
                    f'<text x="{tx:.1f}" y="{start_y + i * fs:.1f}" '
                    f'font-family="Helvetica,Arial,sans-serif" font-size="{fs:.1f}" '
                    f'font-weight="bold" fill="{kleur["border"]}" '
                    f'text-anchor="middle">{line}</text>'
                )

            # m² waarde
            m2_fs = max(fs - 1.5, 4)
            svg_parts.append(
                f'<text x="{tx:.1f}" y="{start_y + len(name_lines) * fs + 1:.1f}" '
                f'font-family="Helvetica,Arial,sans-serif" font-size="{m2_fs:.1f}" '
                f'fill="#64748b" text-anchor="middle">{m2} m²</text>'
            )
        elif rw > 10 and rh > 7:
            fs = min(5.5, rw / 3.5, rh / 2.5)
            svg_parts.append(
                f'<text x="{tx:.1f}" y="{ty + 2:.1f}" '
                f'font-family="Helvetica,Arial,sans-serif" font-size="{fs:.1f}" '
                f'font-weight="bold" fill="{kleur["border"]}" '
                f'text-anchor="middle">{name_lines[0][:6]}</text>'
            )

    svg_parts.append('</svg>')
    return '\n'.join(svg_parts)


# ── FALLBACK: RECHTHOEKIGE LAYOUT ────────────────────────────────────────

def genereer_fallback_polygonen(kamers):
    """
    Als Claude Vision geen polygonen levert, genereer een simpele
    grid-layout op basis van m² verhoudingen.
    Retourneert kamers verrijkt met 'polygoon' veld.
    """
    if not kamers:
        return kamers

    total_m2 = sum(k.get('m2', 0) for k in kamers)
    if total_m2 == 0:
        return kamers

    # Sorteer: grote kamers eerst
    sorted_k = sorted(kamers, key=lambda k: -k.get('m2', 0))

    # Simpele row-packing
    y_cursor = 0
    for k in sorted_k:
        frac = k.get('m2', 0) / total_m2
        row_h = max(frac * 100 * 1.5, 8)
        row_h = min(row_h, 50)

        if y_cursor + row_h > 100:
            row_h = 100 - y_cursor

        k['polygoon'] = [
            [0, y_cursor],
            [100, y_cursor],
            [100, y_cursor + row_h],
            [0, y_cursor + row_h]
        ]
        y_cursor += row_h + 1

    return kamers


# ── HTML RAPPORT GENERATOR ───────────────────────────────────────────────

def genereer_rapport_html(woningen, gemeenschappelijk, per_type, totalen):
    """Genereert volledig HTML rapport met SVG plattegronden."""

    # Overzichtstabel per type
    type_rijen = ''
    for wtype, data in sorted(per_type.items()):
        kleur = WONING_TYPE_KLEUREN.get(wtype, '#64748b')
        type_rijen += f'''<tr>
          <td><span style="display:inline-block;width:12px;height:12px;
            background:{kleur};border-radius:3px;margin-right:8px;vertical-align:middle">
            </span>{wtype}</td>
          <td style="text-align:center">{data.get("count", 0)}</td>
          <td>{", ".join(data.get("woningen", []))}</td>
          <td>{", ".join(data.get("voorbeeld_kamers", [])[:5])}</td>
        </tr>'''

    # Woningtabel
    woning_rijen = ''
    for i, w in enumerate(woningen):
        bg = '#ffffff' if i % 2 == 0 else '#f8fafc'
        kleur = WONING_TYPE_KLEUREN.get(w.get('woning_type', ''), '#64748b')
        wk = sum(k.get('m2', 0) for k in w.get('kamers', []) if k.get('naam') == 'Woonkamer+keuken')
        sk = sum(k.get('m2', 0) for k in w.get('kamers', []) if 'Slaapkamer' in k.get('naam', ''))
        bk = sum(k.get('m2', 0) for k in w.get('kamers', []) if k.get('naam') == 'Badkamer')
        woning_rijen += f'''<tr style="background:{bg}">
          <td><strong>{w.get("woning_nr", "")}</strong></td>
          <td><span style="color:{kleur};font-weight:600">{w.get("woning_type", "")}</span></td>
          <td style="text-align:center;font-weight:bold">{w.get("totaal_m2", "")}</td>
          <td style="text-align:center">{len(w.get("kamers", []))}</td>
          <td style="text-align:center">{wk:.1f}</td>
          <td style="text-align:center">{sk:.1f}</td>
          <td style="text-align:center">{bk:.1f}</td>
        </tr>'''

    # SVG plattegronden per woning
    svg_secties = ''
    for w in woningen:
        nr = w.get('woning_nr', '')
        wtype = w.get('woning_type', '')
        kleur = WONING_TYPE_KLEUREN.get(wtype, '#64748b')
        kamers = w.get('kamers', [])

        # Check of polygonen aanwezig zijn
        has_polys = any(k.get('polygoon') for k in kamers)
        if not has_polys:
            kamers = genereer_fallback_polygonen(kamers)
            w['kamers'] = kamers

        svg = genereer_woning_svg(w)
        if not svg:
            continue

        # Kamertabel
        kamer_rijen = ''
        for k in kamers:
            kk = KAMER_KLEUREN.get(k.get('naam', ''), DEFAULT_KAMER_KLEUR)
            kamer_rijen += f'''<tr>
              <td style="padding:4px 8px">
                <span style="display:inline-block;width:10px;height:10px;
                  background:{kk["bg"]};border:1.5px solid {kk["border"]};
                  border-radius:2px;margin-right:6px;vertical-align:middle"></span>
                {k.get("naam", "")}
              </td>
              <td style="text-align:center;font-weight:bold;padding:4px">{k.get("m2", "")}</td>
              <td style="text-align:center;color:#64748b;padding:4px;font-size:11px">
                {k.get("lengte_m") or "-"} × {k.get("breedte_m") or "-"}
              </td>
            </tr>'''

        total_kamers = round(sum(k.get('m2', 0) for k in kamers), 2)

        svg_secties += f'''
        <div style="break-inside:avoid;margin-bottom:24px;
          border:1px solid #e2e8f0;border-radius:8px;overflow:hidden">
          <div style="background:{kleur};padding:10px 16px;color:white;
            font-weight:bold;font-size:14px;font-family:Arial">
            Woning {nr} — {wtype} — {w.get("totaal_m2", "")} m²
          </div>
          <div style="display:flex;gap:16px;padding:16px;background:white">
            <div style="flex:0 0 40%">
              <table style="width:100%;font-size:12px;border-collapse:collapse">
                <tr style="border-bottom:1px solid #e2e8f0">
                  <th style="text-align:left;padding:4px 8px;color:#64748b;font-size:10px">RUIMTE</th>
                  <th style="text-align:center;padding:4px;color:#64748b;font-size:10px">M²</th>
                  <th style="text-align:center;padding:4px;color:#64748b;font-size:10px">L×B</th>
                </tr>
                {kamer_rijen}
                <tr style="border-top:2px solid {kleur}">
                  <td style="padding:6px 8px;font-weight:bold;color:{kleur}">Totaal</td>
                  <td style="text-align:center;font-weight:bold;color:{kleur};padding:6px">{total_kamers}</td>
                  <td></td>
                </tr>
              </table>
            </div>
            <div style="flex:1">
              {svg}
            </div>
          </div>
        </div>'''

    # Gemeenschappelijke ruimtes
    gem_rijen = ''
    for g in gemeenschappelijk:
        gem_rijen += f'''<tr>
          <td style="padding:4px 8px">{g.get("ruimte_id", g.get("id", ""))}</td>
          <td>{g.get("naam", "")}</td>
          <td style="text-align:center;font-weight:bold">{g.get("m2", "")}</td>
        </tr>'''

    gem_totaal = round(sum(g.get('m2', 0) for g in gemeenschappelijk), 2)

    html = f'''<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<style>
  body {{ font-family: Arial, sans-serif; margin: 40px; color: #1e293b; }}
  h1 {{ color: #1a365d; border-bottom: 3px solid #3b82f6; padding-bottom: 8px; font-size: 24px; }}
  h2 {{ color: #1a365d; margin-top: 32px; font-size: 18px; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 12px; }}
  th {{ background: #1a365d; color: white; padding: 8px; text-align: left; }}
  td {{ padding: 7px 8px; border-bottom: 1px solid #e2e8f0; }}
  .stat-box {{ display: inline-block; background: #f8fafc; border: 1px solid #e2e8f0;
    border-radius: 8px; padding: 16px 24px; margin-right: 12px; margin-bottom: 12px; }}
  .stat-label {{ font-size: 10px; color: #64748b; text-transform: uppercase; letter-spacing: 0.5px; }}
  .stat-value {{ font-size: 28px; font-weight: bold; margin-top: 4px; }}
  .meta {{ color: #94a3b8; font-size: 11px; margin-top: 6px; }}
</style></head><body>

<h1>Plattegrond Extractie Rapport</h1>
<div class="meta">AI-analyse via PyMuPDF + Claude Vision | Automatisch gegenereerd</div>

<div style="margin:20px 0">
  <div class="stat-box">
    <div class="stat-label">Woningen</div>
    <div class="stat-value" style="color:#1a365d">{totalen.get("woningen", 0)}</div>
  </div>
  <div class="stat-box">
    <div class="stat-label">Ruimtes</div>
    <div class="stat-value" style="color:#059669">{totalen.get("kamers", 0)}</div>
  </div>
  <div class="stat-box">
    <div class="stat-label">Woon m²</div>
    <div class="stat-value" style="color:#ea580c">{totalen.get("woonM2", 0)}</div>
  </div>
  <div class="stat-box">
    <div class="stat-label">Gemeensch. m²</div>
    <div class="stat-value" style="color:#64748b">{totalen.get("gemeenschappelijkM2", 0)}</div>
  </div>
</div>

<h2>Overzicht per woningtype</h2>
<table>
  <tr><th>Type</th><th>Aantal</th><th>Woningnrs</th><th>Kamers</th></tr>
  {type_rijen}
</table>

<h2>Alle woningen</h2>
<table>
  <tr><th>Nr.</th><th>Type</th><th>Totaal m²</th><th>Kamers</th>
  <th>Woonk.</th><th>Slpk.</th><th>Badk.</th></tr>
  {woning_rijen}
  <tr style="background:#d1fae5;font-weight:bold">
    <td colspan="2">TOTAAL</td>
    <td style="text-align:center">{totalen.get("woonM2", 0)}</td>
    <td colspan="4"></td>
  </tr>
</table>

<h2>Plattegronden per woning</h2>
{svg_secties}

<h2>Gemeenschappelijke ruimtes</h2>
<table>
  <tr><th>ID</th><th>Ruimte</th><th>m²</th></tr>
  {gem_rijen}
  <tr style="background:#d1fae5;font-weight:bold">
    <td colspan="2">TOTAAL</td>
    <td style="text-align:center">{gem_totaal}</td>
  </tr>
</table>

<p class="meta" style="margin-top:32px">Gegenereerd door Plattegrond Extractie Service | Eikom B.V.</p>
</body></html>'''

    return html


# ── API ENDPOINTS ────────────────────────────────────────────────────────

@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        'status': 'ok',
        'service': 'woningbouw-extractie',
        'version': '0.1'
    })


@app.route('/woningrapport', methods=['POST'])
def woningrapport():
    """
    Genereert PDF rapport op basis van geëxtraheerde woningdata + polygonen.

    Input JSON:
    {
      "woningen": [
        {
          "woning_nr": "001",
          "woning_type": "type A",
          "totaal_m2": 76.89,
          "kamers": [
            {
              "naam": "Woonkamer+keuken",
              "m2": 31.59,
              "lengte_m": 7.8,
              "breedte_m": 4.1,
              "polygoon": [[0,0], [44,0], [44,100], [0,100]]
            }
          ],
          "voids": [[[72,30], [78,30], [78,42], [72,42]]]
        }
      ],
      "gemeenschappelijk": [...],
      "perType": {...},
      "totalen": {...}
    }

    Output: PDF binary
    """
    data = request.get_json(force=True, silent=True)
    if isinstance(data, str):
        data = json.loads(data)

    if not data:
        return jsonify({'error': 'JSON body vereist'}), 400

    woningen = data.get('woningen', [])
    gemeenschappelijk = data.get('gemeenschappelijk', [])
    per_type = data.get('perType', {})
    totalen = data.get('totalen', {})

    if not woningen:
        return jsonify({'error': 'woningen array vereist'}), 400

    # Genereer HTML rapport
    html = genereer_rapport_html(woningen, gemeenschappelijk, per_type, totalen)

    # Converteer naar PDF
    pdf_bytes = WeasyHTML(string=html).write_pdf()

    return Response(
        pdf_bytes,
        mimetype='application/pdf',
        headers={
            'Content-Disposition': 'attachment; filename="extractie_rapport.pdf"',
        }
    )


@app.route('/woningrapport/html', methods=['POST'])
def woningrapport_html():
    """Zelfde als /woningrapport maar retourneert HTML."""
    data = request.get_json(force=True, silent=True)
    if isinstance(data, str):
        data = json.loads(data)

    if not data:
        return jsonify({'error': 'JSON body vereist'}), 400

    html = genereer_rapport_html(
        data.get('woningen', []),
        data.get('gemeenschappelijk', []),
        data.get('perType', {}),
        data.get('totalen', {})
    )

    return Response(
        html,
        mimetype='text/html',
        headers={'Content-Disposition': 'attachment; filename="extractie_rapport.html"'}
    )


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=False)
