"""Document extraction, review orchestration, and report export. Runs fully offline."""

import io
import re
import unicodedata
from collections import Counter

import docx
import pymupdf

import checks

MIN_TEXT_PER_PAGE = 30  # pages with less text than this are treated as likely scanned

ISSUE_TYPES = ["Spelling", "Grammar", "Formatting", "Terminology", "Clarity"]
SEVERITIES = ["High", "Medium", "Low"]
STATUSES = ["Confirmed", "Suggestion", "Needs Manual Review"]


def _para(location, text, kind="para", level=0, style="", fonts=(), sizes=(), bold="", auto_list=False):
    return {"location": location, "text": text, "kind": kind, "level": level, "style": style,
            "fonts": set(fonts), "sizes": set(sizes), "bold": bold, "auto_list": auto_list,
            "lang": checks.detect_lang(text)}


# ---------------------------------------------------------------- extraction

def extract_pdf(data):
    """Return (paragraphs, limitations). Each PDF text block becomes one paragraph."""
    paras, limitations, scanned, blocks = [], [], [], []
    with pymupdf.open(stream=data, filetype="pdf") as pdf:
        for page_no, page in enumerate(pdf, start=1):
            page_chars = 0
            for b_no, block in enumerate((b for b in page.get_text("dict")["blocks"] if b.get("lines")), start=1):
                spans = [s for line in block["lines"] for s in line["spans"] if s["text"].strip()]
                if not spans:
                    continue
                text = " ".join(" ".join(s["text"] for s in line["spans"]).strip() for line in block["lines"])
                text = unicodedata.normalize("NFKC", text).strip()  # turns Arabic presentation forms into letters
                page_chars += len(text)
                sizes = Counter()
                for s in spans:
                    sizes[round(s["size"] * 2) / 2] += len(s["text"])
                blocks.append({"page": page_no, "n": b_no, "text": text,
                               "size": sizes.most_common(1)[0][0],
                               "fonts": {re.sub(r"[-,].*", "", s["font"]) for s in spans},
                               "bold": all("bold" in s["font"].lower() or s["flags"] & 16 for s in spans)})
            if page_chars < MIN_TEXT_PER_PAGE and page.get_images():
                scanned.append(page_no)

    if blocks:
        weights = Counter()
        for b in blocks:
            weights[b["size"]] += len(b["text"])
        body_size = weights.most_common(1)[0][0]
        heading_sizes = sorted({b["size"] for b in blocks if b["size"] > body_size + 0.5}, reverse=True)
        for b in blocks:
            short = len(b["text"]) < 120 and not b["text"].endswith((".", "،"))
            is_heading = short and (b["size"] > body_size + 0.5 or (b["bold"] and len(b["text"]) < 80))
            level = heading_sizes.index(b["size"]) + 1 if b["size"] in heading_sizes else len(heading_sizes) + 1
            paras.append(_para(f"Page {b['page']}, paragraph {b['n']}", b["text"],
                               "heading" if is_heading else "para", level if is_heading else 0,
                               fonts=b["fonts"], sizes={b["size"]}, bold="all" if b["bold"] else ""))

    if scanned:
        limitations.append(
            f"Page(s) {', '.join(map(str, scanned))} appear to be scanned images with no extractable text. "
            "They were not reviewed (OCR is not supported).")
    limitations.append(
        "PDF formatting is inferred from font names and sizes only. Heading levels are estimated from font size, "
        "so PDF formatting findings are marked as suggestions.")
    if any(p["lang"] == "ar" for p in paras):
        limitations.append("Some PDFs store Arabic text in visual (reversed) order. If Arabic findings look "
                           "reversed or garbled, review the DOCX version instead.")
    return paras, limitations


def extract_docx(data):
    """Return (paragraphs, limitations) with style information for each paragraph."""
    document = docx.Document(io.BytesIO(data))
    paras, limitations = [], []
    para_no, section = 0, ""
    for para in document.paragraphs:
        text = para.text
        para_no += 1
        loc = f"Paragraph {para_no}" + (f" (section “{section[:40]}”)" if section else "")
        if not text.strip():
            paras.append(_para(loc, "", "empty"))
            continue
        style = para.style.name if para.style is not None else ""
        level = 0
        m = re.match(r"(?:Heading|عنوان)\s*(\d)", style)
        if m:
            level = int(m.group(1))
        elif style == "Title":
            level = 1
        runs = [r for r in para.runs if r.text.strip()]
        style_bold = para.style.font.bold if para.style is not None else None
        bold = "all" if runs and all(r.bold or (r.bold is None and style_bold) for r in runs) else ""
        if level:
            section = text.strip()
        ppr = para._p.pPr
        auto_list = (ppr is not None and ppr.numPr is not None) or "List" in style
        paras.append(_para(loc, text, "heading" if level else "para", level, style,
                           fonts={r.font.name for r in runs if r.font.name},
                           sizes={r.font.size.pt for r in runs if r.font.size},
                           bold=bold, auto_list=auto_list))

    for t_no, table in enumerate(document.tables, start=1):
        for r_no, row in enumerate(table.rows, start=1):
            seen = []
            for cell in row.cells:  # merged cells repeat; keep each once
                if cell.text.strip() and cell.text not in seen:
                    seen.append(cell.text)
                    paras.append(_para(f"Table {t_no}, row {r_no}", cell.text.strip(), "table"))

    if document.inline_shapes:
        limitations.append(f"The document contains {len(document.inline_shapes)} embedded image(s). "
                           "Text inside images (e.g. screenshots) was not reviewed.")
    limitations.append("Font checks use formatting applied directly to text; fonts inherited from styles count as "
                       "\"default\". Automatic list numbers are not visible to the reader, so only typed numbers are "
                       "checked for sequence errors.")
    return paras, limitations


def extract(filename, data):
    name = filename.lower()
    if name.endswith(".pdf"):
        return extract_pdf(data)
    if name.endswith(".docx"):
        return extract_docx(data)
    raise ValueError("Unsupported file type. Please upload a PDF or DOCX file.")


# ---------------------------------------------------------------- review

def review_document(filename, data, ai=None):
    """Extract the document and run all checks. Returns a dict with issues and limitations.

    `ai`, if given, is a callable (paragraphs, rule_issues) -> extra issues, used for the ChatGPT review."""
    paras, limitations = extract(filename, data)
    text = " ".join(p["text"] for p in paras)
    if len(text.strip()) < MIN_TEXT_PER_PAGE:
        return {"issues": [], "has_arabic": False,
                "limitations": limitations + ["No reviewable text could be extracted from this document."]}

    issues = checks.run_all(paras, "pdf" if filename.lower().endswith(".pdf") else "docx")
    for i in issues:
        i["engine"] = "Rules"
    if ai:
        issues += ai(paras, issues)
    order = {s: i for i, s in enumerate(STATUSES)}
    sev = {s: i for i, s in enumerate(SEVERITIES)}
    issues.sort(key=lambda i: (order[i["status"]], sev[i["severity"]]))
    return {"issues": issues, "limitations": limitations, "has_arabic": bool(checks.AR_RE.search(text))}


# ---------------------------------------------------------------- report

def report_markdown(filename, result):
    issues = result["issues"]
    out = [f"# ServiceNow User Guide Review: {filename}", "", f"**Total issues:** {len(issues)}", ""]
    for t in ISSUE_TYPES:
        out.append(f"- {t}: {sum(1 for i in issues if i['issue_type'] == t)}")
    out.append("")
    for s in STATUSES:
        out.append(f"- {s}: {sum(1 for i in issues if i['status'] == s)}")
    if result["limitations"]:
        out += ["", "## Limitations", ""] + [f"- {x}" for x in result["limitations"]]
    out += ["", "## Issues", ""]
    for n, i in enumerate(issues, start=1):
        out += [
            f"### {n}. {i['issue_type']} ({i['severity']}, {i['status']})",
            f"- **Location:** {i['location']}",
            f"- **Original text:** {i['original_text']}",
            f"- **Suggested correction:** {i['suggested_correction']}",
            f"- **Explanation:** {i['explanation']}",
            f"- **Found by:** {i.get('engine', 'Rules')}",
        ]
        if i.get("source"):
            out.append(f"- **Source:** {i['source']}")
        out.append("")
    return "\n".join(out)
