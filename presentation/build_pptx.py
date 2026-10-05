"""Generate a native PowerPoint (.pptx) version of the Azure -> AWS migration deck.

Run: python presentation/build_pptx.py
Output: presentation/Azure-to-AWS-Migration.pptx
"""
from __future__ import annotations

import os

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Emu, Pt

# 16:9 canvas
EMU = 914400
SW, SH = int(13.333 * EMU), int(7.5 * EMU)

AZURE = RGBColor(0x00, 0x78, 0xD4)
ACCENT = RGBColor(0x5B, 0x5B, 0xD6)
AWS = RGBColor(0xFF, 0x99, 0x00)
INK = RGBColor(0x1B, 0x27, 0x33)
MUTED = RGBColor(0x5B, 0x6B, 0x7B)
SOFT = RGBColor(0xEE, 0xF3, 0xF8)
LINE = RGBColor(0xD9, 0xE2, 0xEC)
GOOD = RGBColor(0x2E, 0x7D, 0x32)
WARN = RGBColor(0xB8, 0x86, 0x0B)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
LLM_BG = RGBColor(0xFF, 0xF3, 0xE6)
LLM_FG = RGBColor(0xD2, 0x69, 0x1E)
GATE_BG = RGBColor(0xFF, 0xF8, 0xE1)
END_BG = RGBColor(0xE7, 0xF6, 0xEA)
AUTO_BG = RGBColor(0xEF, 0xEC, 0xFB)

prs = Presentation()
prs.slide_width = SW
prs.slide_height = SH
BLANK = prs.slide_layouts[6]


def inch(v):
    return Emu(int(v * EMU))


def add_slide():
    s = prs.slides.add_slide(BLANK)
    bg = s.background
    bg.fill.solid()
    bg.fill.fore_color.rgb = WHITE
    # top accent bar
    bar = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, SW, inch(0.09))
    bar.fill.solid()
    bar.fill.fore_color.rgb = ACCENT
    bar.line.fill.background()
    bar.shadow.inherit = False
    return s


def _set_fill(shape, color):
    if color is None:
        shape.fill.background()
    else:
        shape.fill.solid()
        shape.fill.fore_color.rgb = color


def box(s, x, y, w, h, fill=None, line=None, line_w=1.0, radius=True):
    shp = s.shapes.add_shape(
        MSO_SHAPE.ROUNDED_RECTANGLE if radius else MSO_SHAPE.RECTANGLE,
        inch(x), inch(y), inch(w), inch(h),
    )
    _set_fill(shp, fill)
    if line is None:
        shp.line.fill.background()
    else:
        shp.line.color.rgb = line
        shp.line.width = Pt(line_w)
    shp.shadow.inherit = False
    return shp


def text(s, x, y, w, h, runs, align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP,
         space_after=4, line_spacing=1.0):
    """runs: list of paragraphs; each paragraph is a list of (txt, size, color, bold, italic)."""
    tb = s.shapes.add_textbox(inch(x), inch(y), inch(w), inch(h))
    tf = tb.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = anchor
    for i, para in enumerate(runs):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        p.space_after = Pt(space_after)
        p.space_before = Pt(0)
        p.line_spacing = line_spacing
        for (txt, size, color, bold, italic) in para:
            r = p.add_run()
            r.text = txt
            r.font.size = Pt(size)
            r.font.color.rgb = color
            r.font.bold = bold
            r.font.italic = italic
            r.font.name = "Segoe UI"
    return tb


def R(txt, size=18, color=INK, bold=False, italic=False):
    return (txt, size, color, bold, italic)


def eyebrow(s, label):
    text(s, 0.7, 0.42, 11, 0.4, [[R(label.upper(), 13, ACCENT, True, False)]])


def footer(s, n, topic):
    box(s, 0.7, 7.02, 11.93, 0.013, fill=LINE, line=None, radius=False)
    text(s, 0.7, 7.08, 6, 0.3, [[R("Azure \u2192 AWS IaC Migration", 11, MUTED, False, False)]])
    text(s, 7.0, 7.08, 5.63, 0.3, [[R(f"Slide {n} \u00b7 {topic}", 11, MUTED, False, False)]],
         align=PP_ALIGN.RIGHT)


def title(s, t, size=30):
    text(s, 0.7, 0.82, 11.9, 1.0, [[R(t, size, INK, True, False)]], line_spacing=1.05)


def bullets(s, x, y, w, h, items, size=15.5, gap=5):
    paras = []
    for it in items:
        paras.append([R("\u2022  ", size, AZURE, True, False)] + it)
    text(s, x, y, w, h, paras, space_after=gap, line_spacing=1.05)


def card(s, x, y, w, h, title_txt, body, tcolor=AZURE, fill=SOFT, line=LINE, tag=None):
    box(s, x, y, w, h, fill=fill, line=line)
    cy = y + 0.18
    if tag:
        text(s, x + 0.22, cy, w - 0.4, 0.3, [[R(tag.upper(), 11, ACCENT, True, False)]])
        cy += 0.32
    text(s, x + 0.22, cy, w - 0.4, 0.4, [[R(title_txt, 15.5, tcolor, True, False)]])
    text(s, x + 0.22, cy + 0.42, w - 0.44, h - (cy - y) - 0.5,
         [[R(body, 12.5, MUTED, False, False)]], line_spacing=1.03)


def flownode(s, x, y, w, h, label, body, kind="det"):
    fills = {"det": SOFT, "llm": LLM_BG, "gate": GATE_BG, "end": END_BG,
             "src": RGBColor(0xEA, 0xF3, 0xFB), "auto": AUTO_BG}
    lines = {"det": LINE, "llm": AWS, "gate": WARN, "end": GOOD, "src": AZURE, "auto": ACCENT}
    labelc = {"det": AZURE, "llm": LLM_FG, "gate": WARN, "end": GOOD, "src": AZURE, "auto": ACCENT}
    box(s, x, y, w, h, fill=fills[kind], line=lines[kind], line_w=1.25)
    text(s, x + 0.08, y + 0.12, w - 0.16, 0.3,
         [[R(label, 11, labelc[kind], True, False)]], align=PP_ALIGN.CENTER)
    text(s, x + 0.08, y + 0.44, w - 0.16, h - 0.5,
         [[R(body, 10.5, INK, False, False)]], align=PP_ALIGN.CENTER, line_spacing=0.98)


def arrow(s, x, y):
    text(s, x, y, 0.3, 0.4, [[R("\u203a", 20, MUTED, True, False)]], align=PP_ALIGN.CENTER)


def garrow(s, shape, x, y, w, h, color=RGBColor(0x90, 0xA4, 0xB8)):
    a = s.shapes.add_shape(shape, inch(x), inch(y), inch(w), inch(h))
    a.fill.solid()
    a.fill.fore_color.rgb = color
    a.line.fill.background()
    a.shadow.inherit = False
    return a


def chevron(s, x, y, w, h, name, color):
    shp = s.shapes.add_shape(MSO_SHAPE.CHEVRON, inch(x), inch(y), inch(w), inch(h))
    shp.fill.solid()
    shp.fill.fore_color.rgb = color
    shp.line.fill.background()
    shp.shadow.inherit = False
    tf = shp.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    r = p.add_run()
    r.text = name
    r.font.size = Pt(13.5)
    r.font.bold = True
    r.font.color.rgb = WHITE
    r.font.name = "Segoe UI"
    return shp


# ----------------------------------------------------------------------------
# Slide 1 - Title
# ----------------------------------------------------------------------------
s = add_slide()
box(s, 0, 0, 13.333, 7.5, fill=RGBColor(0xF7, 0xFA, 0xFD), line=None, radius=False)
box(s, 0, 0, 13.333, 0.09, fill=ACCENT, line=None, radius=False)
text(s, 0.9, 1.3, 11, 0.4, [[R("CAPSTONE PROJECT \u00b7 AGENTIC CLOUD MIGRATION", 14, ACCENT, True, False)]])
text(s, 0.9, 1.85, 11.5, 1.8,
     [[R("Azure Bicep \u2192 AWS CloudFormation", 40, INK, True, False)],
      [R("Migration Agent", 40, INK, True, False)]], line_spacing=1.05)
text(s, 0.9, 3.85, 11.0, 1.0,
     [[R("An agentic, human-gated pipeline that translates Azure infrastructure-as-code into "
         "AWS CloudFormation \u2014 safely, auditably, and with the human in control.", 18, MUTED, False, False)]],
     line_spacing=1.2)
badges = ["7-Agent LangGraph Pipeline", "AWS Bedrock Reasoning", "Human-in-the-Loop Gates",
          "Security Guardrails", "RAG Knowledge Base"]
bx = 0.9
for b in badges:
    w = 0.28 + len(b) * 0.098
    box(s, bx, 5.1, w, 0.42, fill=SOFT, line=LINE)
    text(s, bx, 5.17, w, 0.3, [[R(b, 12, INK, True, False)]], align=PP_ALIGN.CENTER)
    bx += w + 0.18
footer(s, 1, "Overview")

# ----------------------------------------------------------------------------
# Slide 2 - Business Problem
# ----------------------------------------------------------------------------
s = add_slide()
eyebrow(s, "Business Problem")
title(s, "Why organizations migrate \u2014 and why it stalls")
text(s, 0.7, 1.75, 6.3, 0.9,
     [[R("Companies move workloads from Azure to AWS for real business reasons \u2014 but the "
         "infrastructure-as-code rewrite becomes the bottleneck that delays the whole program.",
         16, INK, False, False)]], line_spacing=1.15)
bullets(s, 0.7, 2.95, 6.3, 3.0, [
    [R("Cost optimization", 15.5, INK, True, False), R(" \u2014 consolidating onto a preferred cloud or better pricing.", 15.5, INK, False, False)],
    [R("Mergers & acquisitions", 15.5, INK, True, False), R(" \u2014 two companies, two clouds, one target platform.", 15.5, INK, False, False)],
    [R("Avoiding vendor lock-in", 15.5, INK, True, False), R(" \u2014 staying portable across providers.", 15.5, INK, False, False)],
    [R("Customer & compliance mandates", 15.5, INK, True, False), R(" \u2014 running where the business must.", 15.5, INK, False, False)],
], size=15.5, gap=9)
kpis = [("Weeks", "of manual rework per workload", AZURE),
        ("High", "cost of specialist cloud engineers", AWS),
        ("Outages", "risk from manual translation errors", AZURE),
        ("Leaks", "exposure of secrets & access", AWS)]
kx, ky = 7.35, 1.85
for i, (big, lbl, col) in enumerate(kpis):
    x = kx + (i % 2) * 2.72
    y = ky + (i // 2) * 1.55
    box(s, x, y, 2.5, 1.35, fill=SOFT, line=LINE)
    text(s, x, y + 0.2, 2.5, 0.6, [[R(big, 26, col, True, False)]], align=PP_ALIGN.CENTER)
    text(s, x + 0.1, y + 0.78, 2.3, 0.5, [[R(lbl, 11.5, MUTED, False, False)]],
         align=PP_ALIGN.CENTER, line_spacing=0.98)
text(s, 0.7, 6.25, 11.9, 0.6,
     [[R("The goal: ", 15, INK, True, False),
       R("cut migration time, cost, and risk with an automated, auditable, repeatable pipeline "
         "\u2014 so teams ship migrations with confidence.", 15, MUTED, False, False)]], line_spacing=1.1)
footer(s, 2, "Business Problem")

# ----------------------------------------------------------------------------
# Slide 3 - The Problem (technical)
# ----------------------------------------------------------------------------
s = add_slide()
eyebrow(s, "The Problem")
title(s, "Moving infrastructure between clouds is hard and risky")
text(s, 0.7, 1.75, 6.4, 1.0,
     [[R("Teams migrating from Azure to AWS must rewrite their infrastructure code by hand. "
         "This is slow, error-prone, and easy to get wrong.", 16, INK, False, False)]], line_spacing=1.15)
bullets(s, 0.7, 2.95, 6.4, 3.2, [
    [R("Manual rewrites", 15.5, INK, True, False), R(" of Bicep into CloudFormation take time and introduce mistakes.", 15.5, INK, False, False)],
    [R("No audit trail", 15.5, INK, True, False), R(" \u2014 unclear why each resource was mapped a certain way.", 15.5, INK, False, False)],
    [R("Security risks", 15.5, INK, True, False), R(" \u2014 secrets can leak and over-permissive rules slip through.", 15.5, INK, False, False)],
    [R("Existing tools fall short", 15.5, INK, True, False), R(" \u2014 they work within one cloud, not source-to-source across clouds.", 15.5, INK, False, False)],
], size=15.5, gap=9)
card(s, 7.45, 1.85, 5.15, 3.6, "Why not just ask an LLM?",
     "Asking a model to write CloudFormation directly in one shot is unreliable and unauditable. "
     "It can hallucinate syntax, leak secrets, and leaves no record of its decisions. "
     "This project takes a safer, structured approach.")
footer(s, 3, "The Problem")

# ----------------------------------------------------------------------------
# Slide 4 - The Solution
# ----------------------------------------------------------------------------
s = add_slide()
eyebrow(s, "The Solution")
title(s, "A multi-agent pipeline with one reasoning step and human checkpoints")
text(s, 0.7, 1.95, 11.9, 0.9,
     [[R("Seven agents work together in a LangGraph state machine. Deterministic code does the "
         "parsing, rendering, and deploying. A single LLM call does only the reasoning. Humans "
         "approve the important decisions.", 16, MUTED, False, False)]], line_spacing=1.2)
cy = 3.35
cw = 3.78
card(s, 0.7, cy, cw, 2.9, "Structured, not freeform",
     "The LLM emits a structured JSON migration plan \u2014 never raw template text \u2014 so every "
     "decision is reviewable and replayable.", tag="Auditable")
card(s, 0.7 + cw + 0.28, cy, cw, 2.9, "Humans stay in control",
     "Plan review and guardrail scanning are automatic checkpoints; only a real stack conflict "
     "ever pauses the run for a human decision.", tag="Safe")
card(s, 0.7 + 2 * (cw + 0.28), cy, cw, 2.9, "Self-correcting",
     "Validation failures loop back to the reasoning step with feedback, so the pipeline fixes "
     "its own mistakes.", tag="Reliable")
footer(s, 4, "The Solution")

# ----------------------------------------------------------------------------
# Slide 5 - Differentiation
# ----------------------------------------------------------------------------
s = add_slide()
eyebrow(s, "Differentiation")
title(s, "How this is different from existing tools")
text(s, 0.7, 1.7, 11.9, 0.8,
     [[R("Existing tools either reverse-engineer a live cloud account or move workloads within one "
         "cloud. None do source-code-level, cross-cloud IaC translation with self-correction, "
         "guardrails, and gated deployment.", 15, MUTED, False, False)]], line_spacing=1.15)
# table
headers = ["Tool", "Approach", "Cross-cloud", "Self-correct", "Security gate", "Human-gated"]
colx = [0.7, 2.3, 6.3, 7.75, 9.2, 10.9]
colw = [1.6, 4.0, 1.45, 1.45, 1.7, 1.73]
rows = [
    ("Former2", "Reverse-engineers a live AWS account into IaC", "No", "\u2014", "\u2014", "\u2014", None),
    ("cf-terraform", "Converts CloudFormation \u2194 Terraform", "No", "\u2014", "\u2014", "\u2014", None),
    ("Azure Migrate", "Moves workloads within / into Azure", "No", "\u2014", "\u2014", "\u2014", None),
    ("This project", "Source Bicep \u2192 CloudFormation across clouds", "Yes", "Yes", "Yes", "Yes", END_BG),
]
ty = 2.75
# header row
for i, h in enumerate(headers):
    text(s, colx[i], ty, colw[i], 0.3, [[R(h.upper(), 10.5, MUTED, True, False)]])
ty += 0.4
box(s, 0.7, ty - 0.04, 11.93, 0.012, fill=LINE, radius=False)
for row in rows:
    name, appr, c1, c2, c3, c4, hl = row
    rh = 0.62
    if hl:
        box(s, 0.62, ty, 12.05, rh, fill=hl, line=GOOD, line_w=1.0)
    cells = [name, appr, c1, c2, c3, c4]
    for i, c in enumerate(cells):
        bold = (i == 0) or (hl is not None and i >= 2)
        col = INK if bold else MUTED
        text(s, colx[i], ty + 0.14, colw[i], 0.4, [[R(c, 12, col, bold, False)]], line_spacing=0.95)
    ty += rh + 0.06
text(s, 0.7, 6.35, 11.9, 0.6,
     [[R("The unique combination: ", 14, INK, True, False),
       R("deterministic-render / LLM-reason split, security guardrails, secret protection, and a "
         "self-extending knowledge base \u2014 purpose-built for safe cross-cloud migration.", 14, MUTED, False, False)]],
     line_spacing=1.1)
footer(s, 5, "Differentiation")

# ----------------------------------------------------------------------------
# Slide 6 - Core Design Principle
# ----------------------------------------------------------------------------
s = add_slide()
eyebrow(s, "Core Design Principle")
title(s, "Separate reasoning from execution")
text(s, 0.7, 1.75, 11.9, 0.7,
     [[R("The key rule: ", 16, INK, True, False),
       R("the LLM only reasons and produces a plan \u2014 it never writes CloudFormation syntax "
         "directly. Everything else is deterministic code.", 16, MUTED, False, False)]], line_spacing=1.15)
box(s, 0.7, 2.85, 5.85, 2.6, fill=LLM_BG, line=AWS, line_w=1.25)
text(s, 0.95, 3.05, 5.4, 0.4, [[R("LLM reasoning (AWS Bedrock)", 16, LLM_FG, True, False)]])
bullets(s, 0.95, 3.6, 5.4, 1.8, [
    [R("Maps each Azure resource to its AWS equivalent", 14, INK, False, False)],
    [R("Produces a structured migration plan (resources, params, outputs, conditions)", 14, INK, False, False)],
    [R("One call per run \u2014 the only non-deterministic step", 14, INK, False, False)],
], size=14, gap=7)
box(s, 6.75, 2.85, 5.85, 2.6, fill=SOFT, line=LINE)
text(s, 7.0, 3.05, 5.4, 0.4, [[R("Deterministic code (no LLM)", 16, AZURE, True, False)]])
bullets(s, 7.0, 3.6, 5.4, 1.8, [
    [R("Compile Bicep \u2192 ARM JSON", 14, INK, False, False)],
    [R("Build a cloud-neutral representation", 14, INK, False, False)],
    [R("Render CloudFormation YAML, lint, deploy, verify", 14, INK, False, False)],
], size=14, gap=7)
text(s, 0.7, 5.75, 11.9, 0.7,
     [[R("Result: ", 15, INK, True, False),
       R("an auditable, replayable pipeline where no component makes surprise network calls or "
         "mutates AWS state outside the deploy step.", 15, MUTED, False, False)]], line_spacing=1.1)
footer(s, 6, "Design Principle")

# ----------------------------------------------------------------------------
# Slide 7 - Conceptual Architecture
# ----------------------------------------------------------------------------
s = add_slide()
eyebrow(s, "Conceptual Architecture")
title(s, "A layered view of the system")
text(s, 0.7, 1.72, 11.9, 0.4,
     [[R("The migration flows through six stages \u2014 reasoning is isolated to a single stage, "
         "everything else is deterministic and governed.", 14, MUTED, False, False)]], line_spacing=1.1)
stages = [
    ("Source", RGBColor(0x00, 0x78, 0xD4), "Azure Bicep / live resource group"),
    ("Ingest & Normalize", RGBColor(0x2B, 0x7F, 0xC4), "Compile to ARM, build neutral model"),
    ("Reason", RGBColor(0xD2, 0x69, 0x1E), "LLM plan grounded by the RAG KB"),
    ("Govern", RGBColor(0xB8, 0x86, 0x0B), "Automatic checkpoints + stack-conflict gate"),
    ("Execute", RGBColor(0x3E, 0x7C, 0xB1), "Render, deploy and verify"),
    ("Target", RGBColor(0x2E, 0x7D, 0x32), "AWS CloudFormation stack"),
]
cw = 2.3
step = 1.9
cx = 0.68
cy = 2.4
ch = 0.95
for i, (name, col, desc) in enumerate(stages):
    chevron(s, cx, cy, cw, ch, name, col)
    text(s, cx + 0.02, cy + ch + 0.18, step - 0.02, 0.7,
         [[R(desc, 11, MUTED, False, False)]], align=PP_ALIGN.CENTER, line_spacing=1.0)
    cx += step
# cross-cutting foundations band
box(s, 0.7, 4.5, 11.92, 1.78, fill=RGBColor(0xF3, 0xF0, 0xFF), line=ACCENT, line_w=1.25)
text(s, 0.95, 4.66, 11.4, 0.4,
     [[R("Cross-cutting foundations \u00b7 applied across every stage", 15, ACCENT, True, False)]])
cc = [("Secret protection", "Masked wrappers and log redaction end-to-end."),
      ("Observability", "Per-run artifacts and opt-in tracing."),
      ("Evaluation & calibration", "Benchmark scores and reliability metrics.")]
ccx = 0.95
for t, b in cc:
    box(s, ccx, 5.18, 3.72, 0.92, fill=WHITE, line=LINE)
    text(s, ccx + 0.18, 5.3, 3.4, 0.35, [[R(t, 13.5, INK, True, False)]])
    text(s, ccx + 0.18, 5.66, 3.36, 0.4, [[R(b, 11.5, MUTED, False, False)]], line_spacing=1.0)
    ccx += 3.9
footer(s, 7, "Conceptual Architecture")

# ----------------------------------------------------------------------------
# Slide 8 - Pipeline
# ----------------------------------------------------------------------------
s = add_slide()
eyebrow(s, "Architecture")
title(s, "The 7-agent LangGraph pipeline")

nw, nh = 2.5, 0.95
X = [0.7, 3.83, 6.96, 10.09]
r1y, r2y, r3y = 1.9, 3.35, 4.8

# Row 1 (left -> right)
row1 = [("Agent 0", "Export resource\ngroup (optional)", "det"),
        ("Agent 1", "Validate & check\nKB coverage", "det"),
        ("Agent 2", "Build cloud-\nneutral model", "det"),
        ("Agent 3", "Map resources\n(Bedrock LLM)", "llm")]
for i, (lbl, body, kind) in enumerate(row1):
    flownode(s, X[i], r1y, nw, nh, lbl, body, kind)
    if i < 3:
        garrow(s, MSO_SHAPE.RIGHT_ARROW, X[i] + nw + 0.065, r1y + nh / 2 - 0.14, 0.5, 0.28)

# down connector: Agent 3 -> plan check (right side)
garrow(s, MSO_SHAPE.DOWN_ARROW, X[3] + nw / 2 - 0.15, r1y + nh + 0.06, 0.3, 0.38)

# Row 2 (right -> left): plan check (auto), Agent 4, Agent 5, guardrail scan (auto)
row2 = [("Plan check", "Auto-approved once\ncfn-lint is clean", "auto"),
        ("Agent 4", "Render\nCloudFormation", "det"),
        ("Agent 5", "Validate with\ncfn-lint", "det"),
        ("Guardrail scan", "Auto security scan\n(logs findings)", "auto")]
r2X = [X[3], X[2], X[1], X[0]]
for i, (lbl, body, kind) in enumerate(row2):
    flownode(s, r2X[i], r2y, nw, nh, lbl, body, kind)
    if i < 3:
        gap_center = (r2X[i] + (r2X[i + 1] + nw)) / 2
        garrow(s, MSO_SHAPE.LEFT_ARROW, gap_center - 0.25, r2y + nh / 2 - 0.14, 0.5, 0.28)

# down connector: guardrail scan -> stack gate (left side)
garrow(s, MSO_SHAPE.DOWN_ARROW, X[0] + nw / 2 - 0.15, r2y + nh + 0.06, 0.3, 0.38)

# Row 3 (left -> right): the one human gate, then deploy / verify / report
row3 = [("Stack gate", "Human: delete and\nrecreate, or cancel", "gate"),
        ("Agent 6", "Deploy via\nboto3", "det"),
        ("Agent 6b", "Post-deploy\nverify", "det"),
        ("Agent 7", "Report + history\n+ calibration", "end")]
for i, (lbl, body, kind) in enumerate(row3):
    flownode(s, X[i], r3y, nw, nh, lbl, body, kind)
    if i < 3:
        garrow(s, MSO_SHAPE.RIGHT_ARROW, X[i] + nw + 0.065, r3y + nh / 2 - 0.14, 0.5, 0.28)

# self-correction note
text(s, 0.7, r3y + nh + 0.08, 11.9, 0.26,
     [[R("\u21BA Self-correction: ", 11.5, WARN, True, False),
       R("a non-clean cfn-lint result loops back from Agent 5 to Agent 3, up to the retry limit.",
         11.5, MUTED, False, False)]])

# legend
leg = [("Deterministic code", SOFT, LINE), ("LLM reasoning", LLM_BG, AWS),
       ("Auto checkpoint", AUTO_BG, ACCENT),
       ("Human gate", GATE_BG, WARN), ("Report / end", END_BG, GOOD)]
lx = 0.7
for t, f, ln in leg:
    box(s, lx, 6.12, 0.24, 0.24, fill=f, line=ln)
    text(s, lx + 0.32, 6.08, 2.1, 0.3, [[R(t, 11.5, MUTED, False, False)]])
    lx += 2.42
footer(s, 8, "Pipeline")

# ----------------------------------------------------------------------------
# Slide 9 - Human gates
# ----------------------------------------------------------------------------
s = add_slide()
eyebrow(s, "Safety")
title(s, "One human gate, two automatic checkpoints")
text(s, 0.7, 1.75, 11.9, 0.8,
     [[R("Every migration plan is reviewed and every template is security-scanned before deploy "
         "\u2014 but only a real stack conflict ever stops the run for a human decision.", 16, MUTED, False, False)]],
     line_spacing=1.15)
gcw = 3.78
gy = 2.85
card(s, 0.7, gy, gcw, 2.5, "Plan approval",
     "The migration plan and resource mapping table are logged for review once cfn-lint is fully "
     "clean; approval is automatic.", tcolor=ACCENT, fill=AUTO_BG, line=ACCENT, tag="Automatic")
card(s, 0.7 + gcw + 0.28, gy, gcw, 2.5, "Guardrail scan",
     "checkov plus custom checks run next. Every finding is logged to the report regardless of "
     "severity, and the run always continues.", tcolor=ACCENT, fill=AUTO_BG, line=ACCENT, tag="Automatic")
card(s, 0.7 + 2 * (gcw + 0.28), gy, gcw, 2.5, "Stack conflict",
     "The only interactive prompt \u2014 fires only when the target stack already exists. Choose "
     "delete-and-recreate or cancel.",
     tcolor=WARN, fill=GATE_BG, line=WARN, tag="Human decision")
text(s, 0.7, 5.7, 11.9, 0.9,
     [[R("Once the stack gate clears, deployment is fully automatic", 15, INK, True, False),
       R(" \u2014 no confirmation prompt and no manual parameter entry. Parameter values resolve from "
         "files, environment variables, or the source Key Vault in a clear priority order.", 15, MUTED, False, False)]],
     line_spacing=1.1)
footer(s, 9, "Human Gates")

# ----------------------------------------------------------------------------
# Slide 10 - Security
# ----------------------------------------------------------------------------
s = add_slide()
eyebrow(s, "Security & Guardrails")
title(s, "Scanning for risks and protecting secrets")
text(s, 0.7, 1.8, 5.9, 0.4, [[R("Guardrail security scan", 16, AZURE, True, False)]])
bullets(s, 0.7, 2.3, 5.9, 3.0, [
    [R("checkov", 14, INK, True, False), R(" \u2014 ~1000 built-in CloudFormation policy checks (encryption, public access, logging).", 14, INK, False, False)],
    [R("Hardcoded secrets", 14, INK, True, False), R(" \u2014 flags literal secret values outside NoEcho parameters.", 14, INK, False, False)],
    [R("Over-permissive IAM", 14, INK, True, False), R(" \u2014 flags wildcard Action / Resource / Principal.", 14, INK, False, False)],
    [R("Open network ingress", 14, INK, True, False), R(" \u2014 flags security groups open to 0.0.0.0/0 on sensitive ports.", 14, INK, False, False)],
], size=14, gap=8)
text(s, 6.85, 1.8, 5.75, 0.4, [[R("Secrets handling", 16, AZURE, True, False)]])
bullets(s, 6.85, 2.3, 5.75, 3.0, [
    [R("Secret values print as ", 14, INK, False, False), R("***", 14, INK, True, False), R(" in logs and state dumps.", 14, INK, False, False)],
    [R("A root logging filter redacts secrets before any handler.", 14, INK, False, False)],
    [R("Plaintext never written to disk", 14, INK, True, False), R("; revealed only at the AWS API boundary.", 14, INK, False, False)],
    [R("No secrets via CLI flags \u2014 avoids shell history exposure.", 14, INK, False, False)],
], size=14, gap=8)
box(s, 0.7, 5.5, 11.92, 1.0, fill=SOFT, line=LINE)
text(s, 0.95, 5.72, 11.4, 0.7,
     [[R("Severity drives visibility, not blocking: ", 14.5, INK, True, False),
       R("every finding is logged to the run report and CLI, HIGH / CRITICAL ones are called out, "
         "and the run always auto-continues.", 14.5, MUTED, False, False)]], line_spacing=1.1)
footer(s, 10, "Security")

# ----------------------------------------------------------------------------
# Slide 11 - Knowledge base
# ----------------------------------------------------------------------------
s = add_slide()
eyebrow(s, "Knowledge Base & RAG")
title(s, "Grounded mappings, not guesswork")
text(s, 0.7, 1.95, 11.9, 0.9,
     [[R("Each Azure resource type maps to a human-reviewed markdown document describing its AWS "
         "equivalent. The LLM retrieves the right docs before reasoning \u2014 so its plan is grounded "
         "in trusted guidance.", 16, MUTED, False, False)]], line_spacing=1.2)
cw = 3.78
cy = 3.4
card(s, 0.7, cy, cw, 2.5, "Mapping docs",
     "Concept differences plus resource, parameter, and property mappings for each Azure \u2192 AWS pair.")
card(s, 0.7 + cw + 0.28, cy, cw, 2.5, "Hybrid retrieval",
     "Vector search (Chroma) fused with BM25 keyword scoring via reciprocal rank fusion for better recall.")
card(s, 0.7 + 2 * (cw + 0.28), cy, cw, 2.5, "Seed-and-grow",
     "An unmapped resource type is logged and skipped automatically; the pipeline still migrates "
     "every resource it does understand.")
text(s, 0.7, 6.15, 11.9, 0.6,
     [[R("Coverage grows by authoring a new mapping doc \u2014 no silent guessing, and no resource is "
         "ever migrated without a trusted, human-reviewed mapping behind it.", 15, MUTED, False, False)]], line_spacing=1.1)
footer(s, 11, "Knowledge Base")

# ----------------------------------------------------------------------------
# Slide 12 - Resource coverage
# ----------------------------------------------------------------------------
s = add_slide()
eyebrow(s, "Resource Coverage")
title(s, "What the agent can migrate today")
cov = [
    ("Key Vault + secrets", "Secrets Manager", "Vaults and secret values"),
    ("Virtual Network", "VPC + Subnets", "NSGs, route tables, NAT gateways, public IPs"),
    ("Azure Functions", "Lambda + IAM + S3", "Triggers and packaging workflows"),
    ("Blob Storage container", "S3 Bucket", "Standalone application blob data"),
    ("Storage Queue / Service Bus", "SQS / SNS", "Queues, topics, subscriptions"),
]
# header
text(s, 0.7, 1.85, 3.4, 0.3, [[R("AZURE", 11, MUTED, True, False)]])
text(s, 4.9, 1.85, 3.0, 0.3, [[R("AWS", 11, MUTED, True, False)]])
text(s, 8.0, 1.85, 4.6, 0.3, [[R("SCOPE", 11, MUTED, True, False)]])
ty = 2.25
box(s, 0.7, ty - 0.04, 11.93, 0.012, fill=LINE, radius=False)
for az, aws, scope in cov:
    text(s, 0.7, ty + 0.12, 3.5, 0.5, [[R(az, 14, INK, True, False)]], line_spacing=0.95)
    text(s, 4.25, ty + 0.12, 0.5, 0.4, [[R("\u2192", 14, AWS, True, False)]])
    text(s, 4.9, ty + 0.12, 3.0, 0.5, [[R(aws, 14, INK, False, False)]], line_spacing=0.95)
    text(s, 8.0, ty + 0.12, 4.6, 0.5, [[R(scope, 13, MUTED, False, False)]], line_spacing=0.95)
    ty += 0.72
    box(s, 0.7, ty - 0.04, 11.93, 0.01, fill=LINE, radius=False)
text(s, 0.7, 6.15, 11.9, 0.6,
     [[R("Coverage is intentionally growing. New resource families are added incrementally by "
         "authoring a mapping doc and registering it in the knowledge base index.", 14, MUTED, False, False)]],
     line_spacing=1.1)
footer(s, 12, "Coverage")

# ----------------------------------------------------------------------------
# Slide 13 - Evaluation
# ----------------------------------------------------------------------------
s = add_slide()
eyebrow(s, "Evaluation & Calibration")
title(s, "Measuring quality and reliability")
text(s, 0.7, 1.8, 5.9, 0.4, [[R("Benchmark harness", 16, AZURE, True, False)]])
bullets(s, 0.7, 2.3, 5.9, 3.0, [
    [R("Curated reference examples with human-reviewed expectations.", 14, INK, False, False)],
    [R("Scores the LLM step: plan validity, parameter hygiene, resource-type accuracy, retrieval quality.", 14, INK, False, False)],
    [R("Runs a trimmed pipeline (Agents 1\u20135) that never deploys \u2014 safe for CI-style checks.", 14, INK, False, False)],
    [R("Supports prompt-version comparison to tune the reasoning step.", 14, INK, False, False)],
], size=14, gap=9)
text(s, 6.85, 1.8, 5.75, 0.4, [[R("Run-history calibration", 16, AZURE, True, False)]])
bullets(s, 6.85, 2.3, 5.75, 3.0, [
    [R("Every real run appends its outcome to a history log.", 14, INK, False, False)],
    [R("Aggregate metrics: pass rate, human-intervention rate, deploy success rate.", 14, INK, False, False)],
    [R("Timing and ", 14, INK, False, False), R("Brier score", 14, INK, True, False), R(" track reliability over time.", 14, INK, False, False)],
    [R("A calibration report is regenerated after each run.", 14, INK, False, False)],
], size=14, gap=9)
box(s, 0.7, 5.6, 11.92, 0.9, fill=SOFT, line=LINE)
text(s, 0.95, 5.8, 11.4, 0.6,
     [[R("This two-layer approach separates ", 14.5, MUTED, False, False),
       R("prompt quality", 14.5, INK, True, False),
       R(" from ", 14.5, MUTED, False, False),
       R("operational reliability", 14.5, INK, True, False),
       R(" \u2014 important for a portfolio-grade capstone.", 14.5, MUTED, False, False)]], line_spacing=1.1)
footer(s, 13, "Evaluation")

# ----------------------------------------------------------------------------
# Slide 14 - Tech stack
# ----------------------------------------------------------------------------
s = add_slide()
eyebrow(s, "Technology")
title(s, "Built on a focused, modern stack")
stack = [
    ("LangGraph", "State-graph orchestration with human-in-the-loop interrupts."),
    ("AWS Bedrock", "The single LLM reasoning step that produces the migration plan."),
    ("boto3", "Real CloudFormation deploy and post-deploy verification."),
    ("cfn-lint", "Deterministic template validation with a self-correction loop."),
    ("checkov", "Security policy scanning of the rendered template."),
    ("Chroma + BM25", "Hybrid retrieval over the knowledge base."),
    ("LangSmith", "Opt-in tracing and evaluation with secret redaction."),
    ("Python + pytest", "Deterministic nodes and guardrails, fully testable."),
]
cw, ch = 2.87, 1.65
gx, gy = 0.7, 1.85
for i, (t, b) in enumerate(stack):
    x = gx + (i % 4) * (cw + 0.12)
    y = gy + (i // 4) * (ch + 0.18)
    box(s, x, y, cw, ch, fill=SOFT, line=LINE)
    text(s, x + 0.2, y + 0.18, cw - 0.4, 0.4, [[R(t, 15, AZURE, True, False)]])
    text(s, x + 0.2, y + 0.62, cw - 0.4, 0.9, [[R(b, 12, MUTED, False, False)]], line_spacing=1.03)
text(s, 0.7, 5.7, 11.9, 0.7,
     [[R("Persistence is simple flat files \u2014 per-run artifacts, a history log, and a calibration "
         "report. No database required at this scale.", 14, MUTED, False, False)]], line_spacing=1.1)
footer(s, 14, "Tech Stack")

# ----------------------------------------------------------------------------
# Slide 15 - Summary
# ----------------------------------------------------------------------------
s = add_slide()
eyebrow(s, "Summary")
title(s, "Why this approach works")
stats = [("7", "agents in one LangGraph pipeline", AZURE),
         ("1", "LLM reasoning step \u2014 everything else deterministic", AWS),
         ("1", "human decision point before AWS is touched", AZURE)]
sx = 0.7
sw = 3.78
for big, lbl, col in stats:
    box(s, sx, 1.85, sw, 1.5, fill=SOFT, line=LINE)
    text(s, sx, 2.0, sw, 0.7, [[R(big, 40, col, True, False)]], align=PP_ALIGN.CENTER)
    text(s, sx + 0.2, 2.85, sw - 0.4, 0.5, [[R(lbl, 12.5, MUTED, False, False)]],
         align=PP_ALIGN.CENTER, line_spacing=1.0)
    sx += sw + 0.28
bullets(s, 0.7, 3.7, 11.9, 2.3, [
    [R("Auditable: ", 15.5, INK, True, False), R("structured plans and per-run artifacts make every decision reviewable.", 15.5, INK, False, False)],
    [R("Safe: ", 15.5, INK, True, False), R("automatic plan review and guardrail scanning, a stack-conflict gate, and secret protection guard every deploy.", 15.5, INK, False, False)],
    [R("Reliable: ", 15.5, INK, True, False), R("self-correction plus calibration metrics keep quality measurable over time.", 15.5, INK, False, False)],
    [R("Extensible: ", 15.5, INK, True, False), R("a seed-and-grow knowledge base adds new resource families incrementally.", 15.5, INK, False, False)],
], size=15.5, gap=10)
text(s, 0.7, 6.1, 11.9, 0.6,
     [[R("A practical, trustworthy path from Azure Bicep to AWS CloudFormation.", 18, INK, True, False)]])
footer(s, 15, "Thank You")

# ----------------------------------------------------------------------------
# Speaker notes (one per slide, in order)
# ----------------------------------------------------------------------------
NOTES = [
    # 1 Title
    "Welcome. Today I'm presenting an agentic pipeline that migrates Azure Bicep "
    "infrastructure-as-code to AWS CloudFormation. The key idea: wrap deterministic code and "
    "human approval around a single LLM reasoning step, so migrations are safe and auditable. "
    "I'll cover the business problem, how we're different, the architecture, safety, and results.",
    # 2 Business Problem
    "Organizations move from Azure to AWS for real reasons: cost optimization, mergers and "
    "acquisitions, avoiding vendor lock-in, and customer or compliance mandates. The blocker is "
    "the infrastructure-as-code rewrite \u2014 weeks of manual work per workload, expensive "
    "specialist engineers, and real risk of outages and secret leaks. Our goal is to cut that "
    "time, cost, and risk with an automated, repeatable pipeline.",
    # 3 The Problem
    "Technically, rewriting Bicep into CloudFormation by hand is slow and error-prone, leaves no "
    "audit trail, and can introduce security gaps. You might ask: why not just have an LLM write "
    "the template? Because one-shot generation is unreliable and unauditable \u2014 it can "
    "hallucinate syntax and leak secrets with no record of its decisions. So we take a structured "
    "approach instead.",
    # 4 Solution
    "Our solution is seven agents working together in a LangGraph state machine. Deterministic "
    "code handles parsing, rendering, and deploying; a single LLM call does only the reasoning; "
    "and humans approve the important decisions. Three pillars: it's auditable because the LLM "
    "emits a structured plan, safe because humans stay in control, and reliable because it "
    "self-corrects on validation failures.",
    # 5 Differentiation
    "This is genuinely different from existing tools. Former2 reverse-engineers a live AWS "
    "account, cf-terraform converts within the same provider, and Azure Migrate moves workloads "
    "within Azure. None do source-code-level, cross-cloud translation. Our unique combination is "
    "the deterministic-render / LLM-reason split, security guardrails, secret protection, and a "
    "self-extending knowledge base \u2014 purpose-built for safe cross-cloud migration.",
    # 6 Design Principle
    "The core design rule: the LLM only reasons and produces a structured plan \u2014 it never "
    "writes CloudFormation syntax directly. Everything downstream, rendering YAML, linting, and "
    "deploying, is deterministic code. This separation makes the pipeline auditable and "
    "replayable, and guarantees no component makes surprise network calls or mutates AWS outside "
    "the deploy step.",
    # 7 Conceptual Architecture
    "At a conceptual level the system flows through six stages: Source, Ingest and Normalize, "
    "Reason, Govern, Execute, and Target. The important point is that reasoning is isolated to a "
    "single stage \u2014 everything else is deterministic and governed. Underneath, three "
    "cross-cutting foundations apply at every stage: secret protection, observability, and "
    "evaluation and calibration.",
    # 8 Pipeline
    "Here's the detailed pipeline. Agents 0 to 2 export and normalize the source. Agent 3 is the "
    "single Bedrock LLM call that produces the migration plan. Agents 4 and 5 render and lint the "
    "template; if linting fails, we loop back to Agent 3 with the error for self-correction. Once "
    "lint is clean, the plan check and guardrail scan run automatically with no prompt, then the "
    "one human gate checks for a stack conflict before Agents 6 and 7 deploy, verify, and report.",
    # 9 Human Gates
    "There's exactly one interactive gate. The plan review and guardrail scan both run "
    "automatically once lint is clean \u2014 they log everything but never block. The stack-conflict "
    "gate is the only prompt, and only fires when the target stack already exists: delete and "
    "recreate, or cancel. Once it clears, deployment is fully automatic, with parameter values "
    "resolved non-interactively from files, environment variables, or the source Key Vault.",
    # 10 Security
    "Security has two layers. The guardrail scan runs checkov plus custom checks for hardcoded "
    "secrets, over-permissive IAM, and open network ingress. Secrets handling wraps values so "
    "they print as asterisks, redacts them from logs, never writes plaintext to disk, and only "
    "reveals them at the AWS API boundary. Severity drives visibility, not blocking \u2014 every "
    "finding is logged, and the run always continues automatically.",
    # 11 Knowledge Base
    "Mappings are grounded, not guessed. Each Azure resource type maps to a human-reviewed "
    "markdown document describing its AWS equivalent. Before reasoning, the LLM retrieves the "
    "relevant docs using hybrid vector plus keyword search. The knowledge base follows a "
    "seed-and-grow model: an unmapped resource type is logged and skipped automatically rather "
    "than guessed at, and coverage expands over time as new mapping docs are added.",
    # 12 Coverage
    "Today the agent migrates Key Vault to Secrets Manager, Virtual Networks to VPCs, Azure "
    "Functions to Lambda with IAM and S3, Blob containers to S3 buckets, and Storage Queue and "
    "Service Bus to SQS and SNS. Coverage is intentionally growing \u2014 adding a new family is just "
    "authoring a mapping doc and registering it in the knowledge base index.",
    # 13 Evaluation
    "We measure quality on two layers. The benchmark harness scores the LLM step on plan "
    "validity, parameter hygiene, resource-type accuracy, and retrieval quality, and can run a "
    "trimmed pipeline that never deploys. The run-history calibration layer tracks real runs over "
    "time \u2014 pass rate, intervention rate, deploy success, and a Brier score. This separates "
    "prompt quality from operational reliability.",
    # 14 Tech Stack
    "The stack is focused and modern: LangGraph for orchestration, AWS Bedrock for the reasoning "
    "step, boto3 for deploy and verify, cfn-lint for validation, checkov for security scanning, "
    "Chroma and BM25 for retrieval, LangSmith for optional tracing, and Python with pytest for "
    "testable deterministic nodes. Persistence is simple flat files \u2014 no database needed at this "
    "scale.",
    # 15 Summary
    "To summarize: seven agents, one LLM reasoning step, and a single human decision point. The "
    "result is auditable through structured plans and per-run artifacts, safe through automatic "
    "checkpoints, guardrails, and the stack-conflict gate, reliable through self-correction and "
    "calibration, and extensible through a seed-and-grow knowledge base. In short, a practical and "
    "trustworthy path from Azure Bicep to AWS CloudFormation. Thank you \u2014 happy to take "
    "questions.",
]

for _slide, _note in zip(prs.slides, NOTES):
    _slide.notes_slide.notes_text_frame.text = _note

# ----------------------------------------------------------------------------
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Azure-to-AWS-Migration.pptx")
try:
    prs.save(out)
except PermissionError:
    import time
    alt = out.replace(".pptx", f"-{time.strftime('%H%M%S')}.pptx")
    prs.save(alt)
    out = alt
    print("Original file was locked (open in PowerPoint); saved a new copy instead.")
print(f"Saved: {out}  ({len(prs.slides._sldIdLst)} slides)")
