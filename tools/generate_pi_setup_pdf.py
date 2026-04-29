from __future__ import annotations

import html
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph, Preformatted, SimpleDocTemplate, Spacer


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "pi_setup_guide.md"
OUTPUT = ROOT / "docs" / "pi_setup_guide.pdf"


def build_styles():
    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(
            name="DocTitle",
            parent=styles["Title"],
            fontName="Helvetica-Bold",
            fontSize=20,
            leading=24,
            alignment=TA_CENTER,
            spaceAfter=18,
            textColor=colors.HexColor("#0f172a"),
        )
    )
    styles.add(
        ParagraphStyle(
            name="Section1",
            parent=styles["Heading1"],
            fontName="Helvetica-Bold",
            fontSize=16,
            leading=20,
            spaceBefore=12,
            spaceAfter=8,
            textColor=colors.HexColor("#0f172a"),
        )
    )
    styles.add(
        ParagraphStyle(
            name="Section2",
            parent=styles["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=13,
            leading=16,
            spaceBefore=10,
            spaceAfter=6,
            textColor=colors.HexColor("#1d4ed8"),
        )
    )
    styles.add(
        ParagraphStyle(
            name="Body",
            parent=styles["BodyText"],
            fontName="Helvetica",
            fontSize=10.5,
            leading=14,
            spaceAfter=6,
            textColor=colors.HexColor("#111827"),
        )
    )
    styles.add(
        ParagraphStyle(
            name="BulletLine",
            parent=styles["BodyText"],
            fontName="Helvetica",
            fontSize=10.5,
            leading=14,
            leftIndent=12,
            firstLineIndent=-10,
            bulletIndent=0,
            spaceAfter=4,
            textColor=colors.HexColor("#111827"),
        )
    )
    styles.add(
        ParagraphStyle(
            name="CodeBlock",
            parent=styles["Code"],
            fontName="Courier",
            fontSize=8.6,
            leading=10.5,
            leftIndent=8,
            rightIndent=8,
            borderPadding=8,
            borderWidth=0.5,
            borderColor=colors.HexColor("#cbd5e1"),
            backColor=colors.HexColor("#f8fafc"),
            spaceBefore=4,
            spaceAfter=8,
        )
    )
    return styles


def inline_markup(text: str) -> str:
    escaped = html.escape(text.strip())
    parts: list[str] = []
    idx = 0
    while idx < len(escaped):
        if escaped.startswith("`", idx):
            end = escaped.find("`", idx + 1)
            if end == -1:
                parts.append(escaped[idx:])
                break
            parts.append(
                f'<font name="Courier" backcolor="#f1f5f9">{escaped[idx + 1:end]}</font>'
            )
            idx = end + 1
            continue
        parts.append(escaped[idx])
        idx += 1
    return "".join(parts)


def parse_markdown(source: Path):
    styles = build_styles()
    story = []
    lines = source.read_text(encoding="utf-8").splitlines()
    in_code = False
    code_lines: list[str] = []

    def flush_code():
        nonlocal code_lines
        if not code_lines:
            return
        story.append(Preformatted("\n".join(code_lines), styles["CodeBlock"]))
        code_lines = []

    for raw_line in lines:
        line = raw_line.rstrip()
        stripped = line.strip()

        if stripped.startswith("```"):
            if in_code:
                flush_code()
                in_code = False
            else:
                in_code = True
            continue

        if in_code:
            code_lines.append(line)
            continue

        if not stripped:
            story.append(Spacer(1, 0.12 * cm))
            continue

        if stripped.startswith("# "):
            story.append(Paragraph(inline_markup(stripped[2:]), styles["DocTitle"]))
            continue

        if stripped.startswith("## "):
            story.append(Paragraph(inline_markup(stripped[3:]), styles["Section1"]))
            continue

        if stripped.startswith("### "):
            story.append(Paragraph(inline_markup(stripped[4:]), styles["Section2"]))
            continue

        if stripped.startswith("- "):
            story.append(
                Paragraph(
                    inline_markup(stripped[2:]),
                    styles["BulletLine"],
                    bulletText="-",
                )
            )
            continue

        if (
            len(stripped) > 3
            and stripped[0].isdigit()
            and stripped[1:].startswith(". ")
        ):
            story.append(
                Paragraph(
                    inline_markup(stripped[3:]),
                    styles["BulletLine"],
                    bulletText=f"{stripped[0]}.",
                )
            )
            continue

        story.append(Paragraph(inline_markup(stripped), styles["Body"]))

    flush_code()
    return story


def main():
    doc = SimpleDocTemplate(
        str(OUTPUT),
        pagesize=A4,
        leftMargin=1.7 * cm,
        rightMargin=1.7 * cm,
        topMargin=1.6 * cm,
        bottomMargin=1.6 * cm,
        title="Guide de setup Raspberry Pi pour Cycle Analyst App",
        author="OpenAI Codex",
    )
    story = parse_markdown(SOURCE)
    doc.build(story)
    print(OUTPUT)


if __name__ == "__main__":
    main()
