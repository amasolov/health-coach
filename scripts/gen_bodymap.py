#!/usr/bin/env python3
"""Generate anatomical body-map panel for the Strength Grafana dashboard.

Uses SVG body paths from react-native-body-highlighter (MIT license) by HichamELBSI.
Produces a getOption JS string for the Volkov Labs Business Charts (ECharts) panel.
"""

import json, re, os

RNBH_DIR = "/tmp/rnbh"

# ── slug → database muscle_group mapping ──────────────────────────────────
SLUG_TO_DB = {
    "chest":      "chest",
    "obliques":   "abdominals",
    "abs":        "abdominals",
    "biceps":     "biceps",
    "triceps":    "triceps",
    "deltoids":   "shoulders",
    "trapezius":  "upper_back",
    "upper-back": "upper_back",
    "lower-back": "lats",
    "quadriceps": "quadriceps",
    "tibialis":   "calves",
    "calves":     "calves",
    "hamstring":  "hamstrings",
    "gluteal":    "glutes",
    "adductors":  "quadriceps",
    "forearm":    "biceps",
}

SKIP_SLUGS = {"neck", "knees", "hands", "feet", "ankles", "head", "hair"}


def parse_body_file(path):
    with open(path) as f:
        content = f.read()
    muscles = {}
    for match in re.finditer(r'slug:\s*"([^"]+)"', content):
        slug = match.group(1)
        start = match.start()
        idx = content.rfind("{", 0, start)
        brace_count = 0
        for i in range(idx, len(content)):
            if content[i] == "{":
                brace_count += 1
            elif content[i] == "}":
                brace_count -= 1
            if brace_count == 0:
                block = content[idx : i + 1]
                break
        paths = {"left": [], "right": []}
        for side in ["left", "right"]:
            pattern = side + r":\s*\[([\s\S]*?)\]"
            side_match = re.search(pattern, block)
            if side_match:
                for p in re.finditer(r'"([^"]+)"', side_match.group(1)):
                    paths[side].append(p.group(1))
        muscles[slug] = paths
    return muscles


def parse_outline(path, side):
    """Extract body outline path from the wrapper component."""
    with open(path) as f:
        content = f.read()
    label = f"body-outline-{side}"
    idx = content.find(label)
    if idx < 0:
        return ""
    # The d="..." attribute can be very long; look back far enough
    block = content[max(0, idx - 15000) : idx]
    # Find the last d="..." in the block
    matches = list(re.finditer(r'd="([^"]+)"', block))
    if not matches:
        matches = list(re.finditer(r'd="([\s\S]+?)"', block))
    if matches:
        return matches[-1].group(1).strip()
    return ""


def build_svg_template(muscles, outline_path, viewbox):
    """Build an SVG string with placeholders for fill colours."""
    lines = []
    lines.append(f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='{viewbox}'>")
    lines.append(
        "<defs>"
        "<filter id='gl'><feGaussianBlur stdDeviation='8' result='b'/>"
        "<feMerge><feMergeNode in='b'/><feMergeNode in='SourceGraphic'/></feMerge></filter>"
        "</defs>"
    )

    if outline_path:
        lines.append(
            f"<path d='{outline_path}' fill='none' stroke='#5a5a80' stroke-width='2.5'/>"
        )

    for slug, sides in muscles.items():
        if slug in SKIP_SLUGS:
            continue
        db_key = SLUG_TO_DB.get(slug)
        if not db_key:
            continue
        placeholder = "{{CLR_" + db_key + "}}"
        stroke_ph = "{{STR_" + db_key + "}}"
        for side_name in ["left", "right"]:
            for p in sides[side_name]:
                safe = p.replace("'", "\\'")
                lines.append(
                    f"<path d='{safe}' fill='{placeholder}' "
                    f"stroke='{stroke_ph}' stroke-width='1.5' opacity='0.92'/>"
                )

    lines.append("</svg>")
    return "".join(lines)


def build_js(
    male_front_svg, male_back_svg,
    female_front_svg, female_back_svg,
    male_front_vb, male_back_vb,
    female_front_vb, female_back_vb,
):
    all_db_keys = sorted(
        {v for k, v in SLUG_TO_DB.items() if k not in SKIP_SLUGS}
    )

    js = []
    js.append("const s = context.panel.data.series[0];")
    js.append("const s2 = context.panel.data.series[1];")

    js.append(
        "const gV = (n) => {"
        " if (!s||!s.fields.length) return 0;"
        " const m=s.fields.find(f=>f.name==='muscle'),"
        " v=s.fields.find(f=>f.name==='sets');"
        " if (!m||!v) return 0;"
        " const a=Array.isArray(m.values)?m.values:Array.from(m.values),"
        " b=Array.isArray(v.values)?v.values:Array.from(v.values),"
        " i=a.indexOf(n); return i>=0?b[i]:0;"
        "};"
    )

    # gender from second query (returns 'male' or 'female')
    js.append(
        "let gender = 'male';"
        " if (s2 && s2.fields.length) {"
        "   const gf = s2.fields.find(f=>f.name==='gender');"
        "   if (gf) { const gv = Array.isArray(gf.values)?gf.values:Array.from(gf.values);"
        "     if (gv.length && gv[0]) gender = gv[0];"
        "   }"
        " }"
    )

    js.append(
        "const clr = (v) => {"
        " if (!v) return '#252545';"
        " const t = Math.min(v/18, 1);"
        " return 'rgb(' + (20+t*30|0) + ',' + (70+t*170|0) + ',' + (120+t*135|0) + ')';"
        "};"
    )
    js.append("const stk = (v) => v > 0 ? 'rgba(80,200,255,0.6)' : 'rgba(70,70,110,0.5)';")

    for key in all_db_keys:
        js.append(f"const _{key} = gV('{key}');")

    # Embed all four SVG templates as JS strings
    for name, svg_tmpl in [
        ("mf", male_front_svg),
        ("mb", male_back_svg),
        ("ff", female_front_svg),
        ("fb", female_back_svg),
    ]:
        safe = svg_tmpl.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")
        js.append(f"const _{name} = `{safe}`;")

    js.append("let svgF = gender === 'female' ? _ff : _mf;")
    js.append("let svgB = gender === 'female' ? _fb : _mb;")

    # Replace colour placeholders
    for key in all_db_keys:
        js.append(f"svgF = svgF.split('{{{{CLR_{key}}}}}').join(clr(_{key}));")
        js.append(f"svgF = svgF.split('{{{{STR_{key}}}}}').join(stk(_{key}));")
        js.append(f"svgB = svgB.split('{{{{CLR_{key}}}}}').join(clr(_{key}));")
        js.append(f"svgB = svgB.split('{{{{STR_{key}}}}}').join(stk(_{key}));")

    js.append(
        "const uriF = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svgF);"
    )
    js.append(
        "const uriB = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svgB);"
    )

    js.append(
        "const W = context.panel.chart.getWidth(),"
        " H = context.panel.chart.getHeight();"
    )
    js.append("const halfW = W / 2 - 8;")

    # Title + legend as ECharts graphic elements
    js.append(
        "const els = ["
        " {type:'text',style:{text:'FRONT',x:halfW/2,y:8,fill:'#777',fontSize:12,"
        "  fontWeight:'bold',textAlign:'center',fontFamily:'sans-serif'}},"
        " {type:'text',style:{text:'BACK',x:halfW+16+halfW/2,y:8,fill:'#777',fontSize:12,"
        "  fontWeight:'bold',textAlign:'center',fontFamily:'sans-serif'}},"
        " {type:'image',style:{image:uriF,x:0,y:22,width:halfW,height:H-50}},"
        " {type:'image',style:{image:uriB,x:halfW+16,y:22,width:halfW,height:H-50}},"
        "];"
    )

    # Legend
    js.append(
        "const lg = [{v:0,l:'0'},{v:3,l:'1-5'},{v:8,l:'6-10'},{v:14,l:'11-15'},{v:20,l:'16+'}];"
    )
    js.append(
        "lg.forEach((it,i) => {"
        " const lx = 10 + i * (W/5.5);"
        " els.push({type:'rect',shape:{x:lx,y:H-22,width:12,height:12,r:2},"
        "  style:{fill:clr(it.v)}});"
        " els.push({type:'text',style:{text:it.l+' sets',x:lx+15,y:H-16,"
        "  fill:'#777',fontSize:9,fontFamily:'sans-serif'}});"
        "});"
    )

    js.append("return {graphic:{elements:els}};")
    return "\n".join(js)


def main():
    # Parse muscle paths
    mf_muscles = parse_body_file(os.path.join(RNBH_DIR, "assets/bodyFront.ts"))
    mb_muscles = parse_body_file(os.path.join(RNBH_DIR, "assets/bodyBack.ts"))
    ff_muscles = parse_body_file(os.path.join(RNBH_DIR, "assets/bodyFemaleFront.ts"))
    fb_muscles = parse_body_file(os.path.join(RNBH_DIR, "assets/bodyFemaleBack.ts"))

    # Parse outlines
    mf_outline = parse_outline(os.path.join(RNBH_DIR, "components/SvgMaleWrapper.tsx"), "front")
    mb_outline = parse_outline(os.path.join(RNBH_DIR, "components/SvgMaleWrapper.tsx"), "back")
    ff_outline = parse_outline(os.path.join(RNBH_DIR, "components/SvgFemaleWrapper.tsx"), "front")
    fb_outline = parse_outline(os.path.join(RNBH_DIR, "components/SvgFemaleWrapper.tsx"), "back")

    print(f"  Male front outline: {len(mf_outline)} chars, muscles: {len(mf_muscles)}")
    print(f"  Male back  outline: {len(mb_outline)} chars, muscles: {len(mb_muscles)}")
    print(f"  Female front outline: {len(ff_outline)} chars, muscles: {len(ff_muscles)}")
    print(f"  Female back  outline: {len(fb_outline)} chars, muscles: {len(fb_muscles)}")

    # ViewBoxes from the wrapper components
    mf_vb = "0 0 724 1448"
    mb_vb = "724 0 724 1448"
    ff_vb = "-50 -40 734 1538"
    fb_vb = "756 0 774 1448"

    # Build SVG templates
    mf_svg = build_svg_template(mf_muscles, mf_outline, mf_vb)
    mb_svg = build_svg_template(mb_muscles, mb_outline, mb_vb)
    ff_svg = build_svg_template(ff_muscles, ff_outline, ff_vb)
    fb_svg = build_svg_template(fb_muscles, fb_outline, fb_vb)

    print(f"  SVG sizes: mf={len(mf_svg)//1024}KB mb={len(mb_svg)//1024}KB "
          f"ff={len(ff_svg)//1024}KB fb={len(fb_svg)//1024}KB")

    # Build JS getOption code
    js_code = build_js(mf_svg, mb_svg, ff_svg, fb_svg, mf_vb, mb_vb, ff_vb, fb_vb)
    print(f"  getOption JS: {len(js_code)//1024}KB")

    # Update dashboard
    dash_path = os.path.join(
        os.path.dirname(__file__), "..", "grafana", "dashboards", "strength.json"
    )
    with open(dash_path) as f:
        dash = json.load(f)

    for panel in dash["dashboard"]["panels"]:
        if panel.get("id") == 202:
            panel["title"] = "Body Map — This Week"
            panel["options"]["getOption"] = js_code
            # Add gender query
            if len(panel["targets"]) < 2:
                panel["targets"].append({
                    "refId": "B",
                    "rawSql": "SELECT COALESCE((SELECT gender FROM users WHERE id = $user), 'male') AS gender",
                    "format": "table",
                })
            print(f"  Updated panel {panel['id']}: {panel['title']}")
            break

    with open(dash_path, "w") as f:
        json.dump(dash, f, indent=2)
    print("  Dashboard JSON saved")


if __name__ == "__main__":
    main()
