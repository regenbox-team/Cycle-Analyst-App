from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import PageBreak, Paragraph, Preformatted, SimpleDocTemplate, Spacer


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "pi_setup_guide.md"
OUTPUT = ROOT / "docs" / "pi_setup_guide.pdf"
ORDERED_LIST_RE = re.compile(r"^(\d+)\.\s+(.*)$")
SECTION_NUMBER_RE = re.compile(r"^(\d+)\.")
TOC_GROUPS = (
    ("Preparation du Raspberry Pi", 1, 5),
    ("Installation de l'application", 6, 9),
    ("Carte offline et peripheriques", 10, 12),
    ("Services et acces reseau", 13, 18),
    ("Maintenance et monitor", 19, 21),
    ("Validation et depannage", 22, 24),
)


@dataclass(frozen=True)
class Heading:
    level: int
    text: str
    anchor: str


class AnchoredHeading(Paragraph):
    def __init__(self, text, style, anchor: str, outline_text: str, outline_level: int):
        super().__init__(text, style)
        self.anchor = anchor
        self.outline_text = outline_text
        self.outline_level = outline_level

    def draw(self):
        self.canv.bookmarkPage(self.anchor)
        self.canv.addOutlineEntry(
            self.outline_text,
            self.anchor,
            level=self.outline_level,
            closed=False,
        )
        super().draw()


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
            name="TocTitle",
            parent=styles["Heading1"],
            fontName="Helvetica-Bold",
            fontSize=15,
            leading=19,
            spaceBefore=8,
            spaceAfter=10,
            textColor=colors.HexColor("#0f172a"),
        )
    )
    styles.add(
        ParagraphStyle(
            name="TocItem1",
            parent=styles["BodyText"],
            fontName="Helvetica",
            fontSize=10,
            leading=13,
            leftIndent=0,
            spaceAfter=3,
            textColor=colors.HexColor("#1d4ed8"),
        )
    )
    styles.add(
        ParagraphStyle(
            name="TocGroup",
            parent=styles["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=10.5,
            leading=14,
            spaceBefore=8,
            spaceAfter=3,
            textColor=colors.HexColor("#0f172a"),
        )
    )
    styles.add(
        ParagraphStyle(
            name="TocItem2",
            parent=styles["BodyText"],
            fontName="Helvetica",
            fontSize=9.2,
            leading=12,
            leftIndent=14,
            spaceAfter=2,
            textColor=colors.HexColor("#475569"),
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


def slugify(text: str, used: set[str]) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-") or "section"
    candidate = slug
    suffix = 2
    while candidate in used:
        candidate = f"{slug}-{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def collect_headings(lines: list[str]) -> list[Heading]:
    headings: list[Heading] = []
    used: set[str] = set()
    in_code = False

    for raw_line in lines:
        stripped = raw_line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue

        if stripped.startswith("## "):
            text = stripped[3:].strip()
            headings.append(Heading(2, text, slugify(text, used)))
            continue
        if stripped.startswith("### "):
            text = stripped[4:].strip()
            headings.append(Heading(3, text, slugify(text, used)))

    return headings


def section_number(heading: Heading) -> int | None:
    match = SECTION_NUMBER_RE.match(heading.text)
    if not match:
        return None
    return int(match.group(1))


def build_toc(headings: list[Heading], styles):
    story = [
        Paragraph("Sommaire", styles["TocTitle"]),
        Paragraph(
            "Clique sur une section pour aller directement a la bonne partie du guide.",
            styles["Body"],
        ),
    ]

    main_headings = [heading for heading in headings if heading.level == 2]
    for group_title, first_section, last_section in TOC_GROUPS:
        group_headings = [
            heading
            for heading in main_headings
            if (number := section_number(heading)) is not None
            and first_section <= number <= last_section
        ]
        if not group_headings:
            continue

        story.append(Paragraph(group_title, styles["TocGroup"]))
        for heading in group_headings:
            label = inline_markup(heading.text)
            story.append(
                Paragraph(f'<a href="#{heading.anchor}">{label}</a>', styles["TocItem1"])
            )

    story.append(PageBreak())
    return story


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
    headings = collect_headings(lines)
    heading_iter = iter(headings)
    in_code = False
    code_lines: list[str] = []
    toc_inserted = False

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
            if not toc_inserted:
                story.extend(build_toc(headings, styles))
                toc_inserted = True
            heading = next(heading_iter)
            story.append(
                AnchoredHeading(
                    inline_markup(heading.text),
                    styles["Section1"],
                    heading.anchor,
                    heading.text,
                    0,
                )
            )
            continue

        if stripped.startswith("### "):
            heading = next(heading_iter)
            story.append(
                AnchoredHeading(
                    inline_markup(heading.text),
                    styles["Section2"],
                    heading.anchor,
                    heading.text,
                    1,
                )
            )
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

        ordered_match = ORDERED_LIST_RE.match(stripped)
        if ordered_match:
            story.append(
                Paragraph(
                    inline_markup(ordered_match.group(2)),
                    styles["BulletLine"],
                    bulletText=f"{ordered_match.group(1)}.",
                )
            )
            continue

        story.append(Paragraph(inline_markup(stripped), styles["Body"]))

    flush_code()
    if not toc_inserted and headings:
        story.extend(build_toc(headings, styles))
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
