"""Static plan/elevation preview for offline geometry review."""

import html
from pathlib import Path


def write_preview(path, report, grids, points, tags, inset_cells):
    origin = report["origin_xy"]
    height, width = report["shape_yx"]
    scale = 60
    cell = report["cell_m"]

    def xy(point):
        return ((point[0] - origin[0]) * scale, (height * cell - point[1] + origin[1]) * scale)

    def poly(boundary):
        return " ".join(f"{x:.2f},{y:.2f}" for x, y in map(xy, boundary))

    overlay = []
    edges = set()
    for x0, y0, x1, y1 in inset_cells:
        corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
        for a, b in zip(corners, corners[1:], strict=False):
            key = tuple(sorted((tuple(round(v, 6) for v in a), tuple(round(v, 6) for v in b))))
            if key in edges:
                edges.remove(key)
            else:
                edges.add(key)
    for a, b in sorted(edges):
        x, y = xy(a)
        ex, ey = xy(b)
        overlay.append(f'<path d="M{x},{y} L{ex},{ey}" stroke="#525d67" stroke-width="1.5"/>')
    for ix in range(int(width * cell) + 1):
        x, y = xy([origin[0] + ix, origin[1]])
        overlay.append(f'<text x="{x}" y="{y + 18}">{origin[0] + ix:g}</text>')
    for iy in range(int(height * cell) + 1):
        x, y = xy([origin[0], origin[1] + iy])
        overlay.append(f'<text x="{x - 25}" y="{y}">{origin[1] + iy:g}</text>')
    for point in points:
        x, y = xy(point)
        overlay.append(f'<circle cx="{x}" cy="{y}" r="1.5" fill="#111"/>')
    for tag in tags:
        point = [tag["x"], tag["y"], tag["z"]]
        x, y = xy(point)
        overlay.append(f'<text x="{x + 4}" y="{y - 4}">Tag {tag["id"]}</text>')
        for j, color in enumerate(("#ba251e", "#087443", "#1655bd")):
            end = [point[i] + 0.3 * tag["T_map_tag"][i][j] for i in range(3)]
            ex, ey = xy(end)
            overlay.append(f'<path d="M{x},{y} L{ex},{ey}" stroke="{color}" stroke-width="2"/>')
    route = report["route"]["tube"]
    line = poly(route["centerline"])
    overlay.append(
        f'<polyline points="{line}" fill="none" stroke="#245dca" opacity=".35" '
        f'stroke-width="{2 * route["half_width_m"] * scale}" '
        'stroke-linecap="round" stroke-linejoin="round"/>'
    )
    for formation in report["formations"]:
        color = "#117548" if formation["candidate"] else "#b42318"
        volume = formation["volume"]
        overlay.append(
            f'<polygon points="{poly(volume["polygon"])}" fill="none" '
            f'stroke="{color}" stroke-width="3"/>'
        )
        x, y = xy(volume["polygon"][0])
        overlay.append(
            f'<text x="{x}" y="{y + 16}">{html.escape(formation["id"])} '
            f"{volume['z_min']:g}–{volume['z_max']:g} m</text>"
        )
    sections = []
    for name, rows in grids.items():
        pixels = []
        for iy, row in enumerate(rows):
            for ix, blocked in enumerate(row):
                x, y = xy([origin[0] + ix * cell, origin[1] + (iy + 1) * cell])
                color = "#d0d3d6" if blocked else "#e7f4e9"
                pixels.append(
                    f'<rect x="{x:.2f}" y="{y:.2f}" width="{cell * scale}" '
                    f'height="{cell * scale}" fill="{color}"/>'
                )
        sections.append(
            f'<section><h2>{html.escape(name)}</h2><svg viewBox="-35 -30 '
            f'{width * cell * scale + 80} {height * cell * scale + 80}" role="img" '
            f'aria-label="Occupancy plan for {html.escape(name)}">'
            + "".join(pixels + overlay)
            + "</svg></section>"
        )
    heights = [report["floor_elevation_m"], report["floor_elevation_m"] + 2.4]
    heights.extend(p[2] for p in points)
    heights.extend(t["z"] for t in tags)
    heights.extend(f["volume"][key] for f in report["formations"] for key in ("z_min", "z_max"))
    z_top, z_bottom = max(heights) + 0.5, min(heights) - 0.5
    elevation = []
    for point in points:
        x, z = (point[0] - origin[0]) * scale, (z_top - point[2]) * scale
        elevation.append(f'<circle cx="{x}" cy="{z}" r="2" fill="#111"/>')
    for tag in tags:
        p = [tag["x"], tag["y"], tag["z"]]
        x, z = (p[0] - origin[0]) * scale, (z_top - p[2]) * scale
        for j, color in enumerate(("#ba251e", "#087443", "#1655bd")):
            ex = (p[0] + 0.3 * tag["T_map_tag"][0][j] - origin[0]) * scale
            ez = (z_top - p[2] - 0.3 * tag["T_map_tag"][2][j]) * scale
            elevation.append(f'<path d="M{x},{z} L{ex},{ez}" stroke="{color}" stroke-width="2"/>')
    for formation in report["formations"]:
        v = formation["volume"]
        xs = [p[0] for p in v["polygon"]]
        x, z = (min(xs) - origin[0]) * scale, (z_top - v["z_max"]) * scale
        elevation.append(
            f'<rect x="{x}" y="{z}" width="{(max(xs) - min(xs)) * scale}" '
            f'height="{(v["z_max"] - v["z_min"]) * scale}" fill="none" stroke="#7c3e9b"/>'
        )
    sections.append(
        '<section><h2>Elevation X / Z</h2><svg viewBox="-35 -30 '
        f'{width * cell * scale + 80} {(z_top - z_bottom) * scale + 60}" '
        'role="img" aria-label="Cloud and formation elevation">'
        + "".join(elevation)
        + "</svg></section>"
    )
    route_status = (
        "clear within authored evidence" if report["route"]["geometry_clear"] else "blocked"
    )
    atrium_status = (
        "candidate pending measurements"
        if report["formations"][1]["candidate"]
        else "rejected; kitchen fallback needs acceptance"
    )
    summary = (
        f"{report['evidence_kind'].capitalize()} evidence. Route geometry: {route_status}. "
        f"Atrium: {atrium_status}. Route height: {route['z_min']:g}–{route['z_max']:g} m."
    )
    Path(path).write_text(
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>Offline map geometry</title><style>body{font:16px system-ui;"
        "margin:2rem;max-width:1100px;color:#17212b}main{display:grid;"
        "grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:2rem}"
        "svg{width:100%;border:1px solid #bbb}text{font-size:11px}h2{font-size:16px}"
        "</style><h1>Offline map geometry</h1><p>" + html.escape(summary) + "</p>"
        "<p>Gray: blocked or unknown. Pale green: candidate cells. Blue: route tube. "
        "Formation outlines: green candidate, red rejected. Dark gray outline: inset geofence. "
        "Black dots: scan points. Overlays use their declared altitude intervals. "
        "Tag axes: red right, green up, blue front. Plan axes: +x right, +y up. "
        "Elevation: +z up. All axes use meters.</p><p>Owner acceptance and flight "
        "authorization remain pending. Tag proximity does not establish visibility.</p>"
        "<main>" + "".join(sections) + "</main></html>",
        encoding="utf-8",
    )
