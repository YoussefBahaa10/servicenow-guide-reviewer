"""Rule-based review checks. Everything runs locally; no external services.

Each check takes the list of extracted paragraphs and returns issue dicts. A paragraph is:
    {"location", "text", "kind" ("para"|"heading"|"table"|"empty"), "level", "style",
     "fonts", "sizes", "bold", "auto_list", "lang" ("en"|"ar"|"")}
"""

import json
import re
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path

from spellchecker import SpellChecker

HERE = Path(__file__).parent
AR_LETTER = r"ء-يٱ-ۓ"
AR_RE = re.compile(f"[{AR_LETTER}]")
LATIN_RE = re.compile(r"[A-Za-z]")
URL_RE = re.compile(r"(https?://\S+|www\.\S+|\S+@\S+\.\w+|\S+\.(?:pdf|docx?|xlsx?|png|jpg|csv|txt)\b)", re.I)

MAX_PER_RULE = 25  # keep reports readable on very long documents


def issue(location, issue_type, original, suggestion, explanation, severity, status, source=""):
    return {"location": location, "issue_type": issue_type, "original_text": original,
            "suggested_correction": suggestion, "explanation": explanation,
            "severity": severity, "status": status, "source": source}


def detect_lang(text):
    ar, lat = len(AR_RE.findall(text)), len(LATIN_RE.findall(text))
    if not ar and not lat:
        return ""
    return "ar" if ar >= 0.1 * (ar + lat) else "en"  # Arabic text often embeds English UI labels


def snippet(text, start, end, pad=30):
    s, e = max(0, start - pad), min(len(text), end + pad)
    return ("…" if s else "") + text[s:e].strip() + ("…" if e < len(text) else "")


def locations(locs, limit=4):
    uniq = list(dict.fromkeys(locs))
    more = f" (+{len(uniq) - limit} more)" if len(uniq) > limit else ""
    return "; ".join(uniq[:limit]) + more


# ---------------------------------------------------------------- resources

class Terminology:
    def __init__(self, path=HERE / "servicenow_terminology.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        self.default_source = data["official_sources"]["product_documentation"]
        self.terms = {}        # canonical term -> source
        self.enforce_case = []  # canonical terms whose exact spelling/case is checked
        for section in ("core_terms", "product_areas"):
            for t in data.get(section, []):
                self.terms[t["term"]] = t.get("source", self.default_source)
                for alias in t.get("aliases", []):
                    self.terms[alias] = t.get("source", self.default_source)
                if t.get("enforce_case"):
                    self.enforce_case.append(t["term"])
        labels = data.get("interface_labels", {})
        for label in labels.get("labels", []):
            self.terms.setdefault(label, labels.get("source", self.default_source))
        self.words = {w.lower() for term in self.terms for w in re.findall(r"[A-Za-z]+", _split_camel(term))}
        self.words |= {t.lower() for t in self.terms}
        self.multiword = [t for t in self.terms if " " in t]


def _split_camel(term):
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", term)


@lru_cache(maxsize=1)
def custom_words():
    lines = (HERE / "custom_words.txt").read_text(encoding="utf-8").splitlines()
    return {l.strip().lower() for l in lines if l.strip() and not l.startswith("#")}


@lru_cache(maxsize=1)
def english_checker():
    return SpellChecker(language="en")


@lru_cache(maxsize=1)
def arabic_checker():
    return SpellChecker(language="ar")


GENERAL_ACRONYMS = {
    "IT", "HR", "ID", "IDS", "PDF", "URL", "UI", "UX", "API", "FAQ", "FAQS", "OK", "PC", "VPN", "SSO", "MFA",
    "PIN", "CSV", "HTML", "PNG", "JPG", "USB", "OS", "AM", "PM", "TBD", "NA", "N/A", "IP", "LAN", "CEO",
    "AI", "ML", "KPI", "DOCX", "XLSX", "EN", "AR", "USA", "UAE", "KSA", "GMT", "UTC",
}


# ---------------------------------------------------------------- English language

A_AN_EXCEPTIONS_A = {"one", "once", "user", "users", "use", "used", "useful", "unit", "unique", "universal",
                     "usual", "usually", "uniform", "union", "utility", "eu", "euro", "european", "ubiquitous",
                     "url", "uri", "usb", "ui", "ux", "uk", "us", "ufo"}
A_AN_EXCEPTIONS_AN = {"hour", "hours", "honest", "honor", "honour", "heir", "hourly", "honorary"}
VOWEL_SOUND_LETTERS = set("AEFHILMNORSX")  # letters pronounced with a leading vowel sound
ABBREVIATIONS = {"e.g", "i.e", "etc", "vs", "approx", "no", "fig", "dept", "mr", "mrs", "dr", "st", "ex"}
VAGUE_PHRASES = ["etc.", "and/or", "and so on", "as appropriate", "as needed", "somehow", "and the like"]
LONG_SENTENCE_WORDS = 35


def _an_expected(word):
    if word.isupper() and len(word) > 1:  # acronym: depends on how the first letter is spoken
        return word[0] in VOWEL_SOUND_LETTERS
    lw = word.lower()
    if lw in A_AN_EXCEPTIONS_A or lw.startswith(("uni", "use", "usu", "eu")):
        return False
    if lw in A_AN_EXCEPTIONS_AN:
        return True
    return lw[0] in "aeiou"


def check_english(paras):
    out = []
    for p in paras:
        if p["kind"] == "empty" or p["lang"] != "en":
            continue
        text, loc = p["text"], p["location"]
        clean = URL_RE.sub(lambda m: " " * len(m.group()), text)

        for m in re.finditer(r"\b(a|an)\s+([A-Za-z][\w-]*)", clean, re.I):
            article, word = m.group(1), m.group(2)
            expected = "an" if _an_expected(word) else "a"
            if article.lower() != expected:
                fixed = expected.capitalize() if article[0].isupper() else expected
                out.append(issue(loc, "Grammar", m.group(), f"{fixed} {word}",
                                 f'Use "{expected}" before "{word}" (based on its sound).', "Low", "Confirmed"))

        for m in re.finditer(r"\s+([,;:!?]|\.(?!\.))(?=\s|$)", clean):
            out.append(issue(loc, "Grammar", snippet(text, m.start(), m.end()), "Remove the space before the punctuation mark.",
                             "There should be no space before punctuation.", "Low", "Confirmed"))
        for m in re.finditer(r"(?<=[A-Za-z]),(?=[A-Za-z])|(?<=[a-z]{2})[.;](?=[A-Z][a-z])", clean):
            out.append(issue(loc, "Grammar", snippet(text, m.start(), m.end(), 15), "Add a space after the punctuation mark.",
                             "A space is missing after the punctuation mark.", "Low", "Confirmed"))
        for m in re.finditer(r"([,;:!?])\1+|(?<!\.)\.\.(?!\.)", clean):
            out.append(issue(loc, "Grammar", snippet(text, m.start(), m.end(), 15), "Use a single punctuation mark.",
                             "Duplicated punctuation.", "Low", "Confirmed"))

        for m in re.finditer(r"(?<=[.!?])\s+([a-z]\w*)", clean):
            before = clean[:m.start()].rstrip(".!?").split()
            if before and before[-1].lower().rstrip(".") in ABBREVIATIONS:
                continue
            word = m.group(1)
            out.append(issue(loc, "Grammar", snippet(text, m.start(1), m.end(1)), word.capitalize(),
                             "A sentence should start with a capital letter.", "Low", "Confirmed"))
        for m in re.finditer(r"(?<![\w'])i(?=[\s'’][a-z]|\s)", clean):
            out.append(issue(loc, "Grammar", snippet(text, m.start(), m.end(), 15), "I",
                             'The pronoun "I" is always capitalized.', "Low", "Confirmed"))

        if p["kind"] == "para":
            for sentence in re.split(r"(?<=[.!?])\s+", text):
                if len(sentence.split()) > LONG_SENTENCE_WORDS:
                    out.append(issue(loc, "Clarity", sentence[:160] + ("…" if len(sentence) > 160 else ""),
                                     "Consider splitting this sentence into shorter sentences or steps.",
                                     f"Sentence has {len(sentence.split())} words; long instructions are harder to follow.",
                                     "Low", "Suggestion"))
            for phrase in VAGUE_PHRASES:
                for m in re.finditer(re.escape(phrase), text, re.I):
                    out.append(issue(loc, "Clarity", snippet(text, m.start(), m.end()),
                                     "State the exact items, values, or conditions.",
                                     f'"{phrase}" can be vague in step-by-step instructions.', "Low", "Suggestion"))
    return out


def check_english_spelling(paras, terminology):
    spell = english_checker()
    allowed = custom_words() | terminology.words
    found = defaultdict(list)
    for p in paras:
        if p["kind"] == "empty" or not LATIN_RE.search(p["text"]):
            continue
        text = URL_RE.sub(" ", p["text"])
        for m in re.finditer(r"(?<![\w-])[A-Za-z]+(?:['’][A-Za-z]+)?(?![\w-])", text):
            word = m.group()
            if len(word) < 3 or "'" in word or "’" in word:
                continue
            if any(c.isupper() for c in word[1:]):  # acronyms, CamelCase product names
                continue
            if word[0].isupper():
                prev = text[:m.start()].rstrip()
                if prev and prev[-1] not in ".!?:•-–\"“(" and not prev[-1].isdigit():
                    continue  # capitalized mid-sentence: likely a name or interface label
            lw = word.lower()
            if lw in ABBREVIATIONS and text[m.end():m.end() + 1] == ".":
                continue
            if lw not in allowed:
                found[lw].append((p["location"], word))
    out = []
    for lw in sorted(spell.unknown(found.keys())):
        correction = spell.correction(lw)
        if not correction or correction == lw:
            continue  # no confident suggestion: probably a name; do not guess
        locs, original = [l for l, _ in found[lw]], found[lw][0][1]
        if original[0].isupper():
            correction = correction.capitalize()
        close = _edit_distance(lw, correction.lower()) <= 1
        out.append(issue(locations(locs), "Spelling", original, correction,
                         f"Not found in the English dictionary (seen {len(locs)}×)." +
                         ("" if close else " The suggestion is uncertain; verify manually."),
                         "Medium" if close else "Low", "Confirmed" if close else "Needs Manual Review"))
    return out


def _edit_distance(a, b):
    """Edit distance where swapping two adjacent letters counts as one edit."""
    d = [[i + j if not i or not j else 0 for j in range(len(b) + 1)] for i in range(len(a) + 1)]
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (a[i - 1] != b[j - 1]))
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[-1][-1]


# ---------------------------------------------------------------- Arabic language

# Well-established spelling rules (hamzat al-qat' / hamzat al-wasl). Wrong form -> correct form.
ARABIC_FIXED = {
    "الى": "إلى", "اذا": "إذا", "انشاء": "إنشاء", "اضافة": "إضافة", "ادخال": "إدخال", "ارسال": "إرسال",
    "اعادة": "إعادة", "الغاء": "إلغاء", "اغلاق": "إغلاق", "اختيار": None, "اسم": None,
    "إختيار": "اختيار", "إستخدام": "استخدام", "إنتقال": "انتقال", "إسم": "اسم", "إستعراض": "استعراض",
    "إستلام": "استلام", "إختر": "اختر", "إضغط": "اضغط", "إفتح": "افتح", "إنتظر": "انتظر", "إثنين": "اثنين",
    "هاذا": "هذا", "هاذه": "هذه", "لاكن": "لكن", "ذالك": "ذلك", "اولا": "أولًا", "ايضا": "أيضًا",
}
ARABIC_FIXED = {k: v for k, v in ARABIC_FIXED.items() if v}
AR_PREFIXES = ("وال", "بال", "فال", "كال", "لل", "ال", "و", "ف", "ب", "ل", "ك", "س")
AR_SUFFIXES = ("ها", "هم", "كم", "نا", "ه", "ك", "ي", "ة", "ات", "ين", "ون")


def _ar_known(spell, word):
    if word in spell:
        return True
    for pre in AR_PREFIXES:
        if word.startswith(pre) and len(word) - len(pre) >= 2:
            stem = word[len(pre):]
            if stem in spell or any(stem.endswith(s) and stem[:-len(s)] in spell for s in AR_SUFFIXES):
                return True
    return any(word.endswith(s) and len(word) - len(s) >= 2 and word[:-len(s)] in spell for s in AR_SUFFIXES)


def check_arabic(paras):
    out = []
    for p in paras:
        if p["kind"] == "empty" or not AR_RE.search(p["text"]):
            continue
        text, loc = p["text"], p["location"]
        for m in re.finditer(f"[{AR_LETTER}]+", text):
            word = m.group()
            for pre in ("", "و", "ف", "ب", "ل", "ك", "وب", "فب", "ول"):  # attached conjunctions/prepositions
                stem = word[len(pre):]
                if word.startswith(pre) and stem in ARABIC_FIXED:
                    out.append(issue(loc, "Spelling", word, pre + ARABIC_FIXED[stem],
                                     "Standard Arabic spelling (hamza / letter form).", "Medium", "Confirmed"))
                    break
        for m in re.finditer(f"(?<=[{AR_LETTER}])\\s*([,?;])", text):
            right = {",": "،", "?": "؟", ";": "؛"}[m.group(1)]
            out.append(issue(loc, "Grammar", snippet(text, m.start(), m.end(), 15), right,
                             "Arabic text normally uses Arabic punctuation marks.", "Low", "Suggestion"))
        for m in re.finditer(r"\s+([،؛؟.!:])(?=\s|$)", text):
            out.append(issue(loc, "Grammar", snippet(text, m.start(), m.end(), 15), "Remove the space before the punctuation mark.",
                             "There should be no space before punctuation.", "Low", "Confirmed"))
        for m in re.finditer(f"[،؛](?=[{AR_LETTER}])", text):
            out.append(issue(loc, "Grammar", snippet(text, m.start(), m.end(), 15), "Add a space after the punctuation mark.",
                             "A space is missing after the punctuation mark.", "Low", "Confirmed"))
    return out


def check_arabic_spelling(paras):
    """Dictionary-based typo detection plus frequency checks for hamza / taa marbuta / alef maqsura."""
    spell = arabic_checker()
    freq = spell.word_frequency
    found = defaultdict(list)
    for p in paras:
        if p["kind"] == "empty":
            continue
        for m in re.finditer(f"[{AR_LETTER}]+", p["text"]):
            w = m.group().replace("ـ", "")
            if len(w) >= 3 and w not in ARABIC_FIXED:
                found[w].append(p["location"])
    out = []
    for w, locs in sorted(found.items()):
        if not _ar_known(spell, w):
            correction = spell.correction(w)
            if correction and correction != w and _edit_distance(w, correction) == 1:
                out.append(issue(locations(locs), "Spelling", w, correction,
                                 f"Not found in the Arabic dictionary (seen {len(locs)}×).", "Medium", "Needs Manual Review"))
            continue
        # Word is known, but a variant with hamza / ة / ى may be far more common (corpus frequency).
        variants = []
        if w.endswith("ه"):
            variants.append(w[:-1] + "ة")
        if w.endswith("ي"):
            variants.append(w[:-1] + "ى")
        for pre in ("", "ال", "وال", "بال", "و", "ف", "ب"):
            if w.startswith(pre + "ا") and len(w) > len(pre) + 2:
                variants += [w[:len(pre)] + h + w[len(pre) + 1:] for h in ("أ", "إ")]
        own = freq[w]
        for v in variants:
            if freq[v] >= 20 * max(own, 1) and freq[v] > 1000:
                out.append(issue(locations(locs), "Spelling", w, v,
                                 "This spelling is much less common than the suggested form (check hamza, taa marbuta, or alef maqsura).",
                                 "Low", "Suggestion"))
                break
    return out


# ---------------------------------------------------------------- shared language checks

def check_common(paras):
    out = []
    for p in paras:
        if p["kind"] == "empty":
            continue
        text, loc = p["text"], p["location"]
        for m in re.finditer(r"(?<![\w])(\w{2,})\s+\1(?![\w])", text, re.I):
            if m.group(1).isdigit():
                continue
            out.append(issue(loc, "Grammar", snippet(text, m.start(), m.end(), 15), m.group(1),
                             "Repeated word.", "Medium", "Confirmed"))
        if re.search(r"\S {2,}\S", text):
            m = re.search(r"\S {2,}\S", text)
            out.append(issue(loc, "Formatting", snippet(text, m.start(), m.end(), 20), "Use a single space.",
                             "Multiple consecutive spaces.", "Low", "Confirmed"))
    # mixed digit systems in Arabic text
    western = [p["location"] for p in paras if p["lang"] == "ar" and re.search(r"[0-9]", p["text"])]
    eastern = [p["location"] for p in paras if p["lang"] == "ar" and re.search(r"[٠-٩]", p["text"])]
    if western and eastern:
        out.append(issue(locations(eastern if len(eastern) < len(western) else western), "Formatting",
                         "Mixed digits (0-9 and ٠-٩)", "Use one digit style consistently in the Arabic text.",
                         "Arabic sections use both Western and Arabic-Indic digits.", "Low", "Suggestion"))
    return out


# ---------------------------------------------------------------- ServiceNow terminology

def check_terminology(paras, terminology):
    out = []
    seen_unverified = set()
    variants = defaultdict(lambda: defaultdict(list))  # term -> exact form -> locations

    patterns = []
    for term in terminology.enforce_case:
        words = _split_camel(term).split()
        patterns.append((term, re.compile(r"(?<![\w-])" + r"[\s\-]?".join(map(re.escape, words)) + r"(?![\w-])", re.I)))
    multi = [(t, re.compile(r"(?<![\w-])" + r"\s+".join(map(re.escape, t.split())) + r"(?![\w-])", re.I))
             for t in terminology.multiword if t not in terminology.enforce_case]

    for p in paras:
        if p["kind"] == "empty":
            continue
        text, loc = p["text"], p["location"]
        for term, pat in patterns:
            for m in pat.finditer(text):
                if m.group() != term:
                    out.append(issue(loc, "Terminology", m.group(), term,
                                     f'The official name is written "{term}".', "High" if term == "ServiceNow" else "Medium",
                                     "Confirmed", terminology.terms[term]))
        for term, pat in multi:
            for m in pat.finditer(text):
                prev = text[:m.start()].rstrip()
                sentence_start = not prev or prev[-1] in ".!?:•"
                if not sentence_start and not m.group().islower():  # lowercase = generic prose, not the label
                    variants[term][m.group()].append(loc)

        # possible misspellings of multi-word ServiceNow terms, e.g. "Assignment Gorup"
        words = list(re.finditer(r"[A-Za-z]+", text))
        for term in terminology.multiword + [t for t in terminology.enforce_case if " " in t]:
            n = len(term.split())
            target = term.lower().split()
            for i in range(len(words) - n + 1):
                gram = [w.group().lower() for w in words[i:i + n]]
                if gram == target:
                    continue
                diffs = [(g, t) for g, t in zip(gram, target) if g != t]
                if len(diffs) == 1 and len(diffs[0][1]) >= 4 and _edit_distance(*diffs[0]) == 1 \
                        and diffs[0][0] not in english_checker():
                    original = text[words[i].start():words[i + n - 1].end()]
                    out.append(issue(loc, "Terminology", original, term,
                                     f'Possible misspelling of the ServiceNow term "{term}".', "Medium", "Confirmed",
                                     terminology.terms[term]))

        # unfamiliar acronyms / CamelCase names: never assume they are wrong
        if sum(c.isupper() for c in text) > 0.6 * max(1, sum(c.isalpha() for c in text)):
            continue  # all-caps text such as banners
        for m in re.finditer(r"(?<![\w-])([A-Z]{2,6}s?|[A-Z][a-z]+[A-Z][A-Za-z]*)(?![\w-])", text):
            token = m.group()
            base = token[:-1] if token.endswith("s") and token[:-1].isupper() else token
            if (base in terminology.terms or base.upper() in GENERAL_ACRONYMS or base in seen_unverified
                    or base.lower() in custom_words()):
                continue
            if any(base.lower() == t.lower().replace(" ", "") for t in terminology.terms):
                continue  # handled by the case checks above
            seen_unverified.add(base)
            out.append(issue(loc, "Terminology", base, base,
                             "Not in the verified ServiceNow terminology reference. It may be a legitimate product name, "
                             "acronym, or customized label; verify it in official ServiceNow documentation.",
                             "Low", "Needs Manual Review", terminology.default_source))

    for term, forms in variants.items():
        if len(forms) > 1:
            main = max(forms, key=lambda f: len(forms[f]))
            for form, locs in forms.items():
                if form != main:
                    out.append(issue(locations(locs), "Terminology", form, main,
                                     f'"{term}" is written inconsistently ("{main}" is used {len(forms[main])}×).',
                                     "Low", "Suggestion", terminology.terms[term]))
    return out


def check_ui_references(paras):
    """Same button/field referred to with different action verbs (click vs. select vs. press)."""
    verbs = defaultdict(lambda: defaultdict(list))
    pat = re.compile(r"\b(click|press|select|tap|choose|hit)\s+(?:on\s+)?(?:the\s+)?[\"'“‘*]?([A-Z]\w*(?:\s[A-Z]\w*){0,3})")
    for p in paras:
        for m in pat.finditer(p["text"]):
            verbs[m.group(2)][m.group(1).lower()].append(p["location"])
    out = []
    for label, used in verbs.items():
        if len(used) > 1:
            summary = ", ".join(f'"{v} {label}" ({len(l)}×)' for v, l in used.items())
            out.append(issue(locations([l for ls in used.values() for l in ls]), "Clarity", summary,
                             f"Use one verb consistently, e.g. \"{max(used, key=lambda v: len(used[v]))} {label}\".",
                             "The same interface element is referred to with different action verbs.", "Low", "Suggestion"))
    return out


# ---------------------------------------------------------------- formatting

LIST_NUM_RE = re.compile(r"^\s*(?:Step\s+)?([0-9٠-٩]+)\s*([.)\-–:])\s+", re.I)
BULLET_RE = re.compile(r"^\s*([•●○◦▪■□►▶➢✓\-–*·])\s+")
AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


def check_formatting(paras, source_kind):
    out = []
    headings = [p for p in paras if p["kind"] == "heading"]
    body = [p for p in paras if p["kind"] == "para"]

    # heading appearance per level
    by_level = defaultdict(list)
    for h in headings:
        by_level[h["level"]].append(h)
    for level, hs in by_level.items():
        look = Counter(_appearance(h) for h in hs)
        if len(look) > 1:
            common = look.most_common(1)[0][0]
            for h in hs:
                if _appearance(h) != common:
                    out.append(issue(h["location"], "Formatting", h["text"][:120], f"Format like other level-{level} headings ({common}).",
                                     f"Heading uses {_appearance(h)} while most level-{level} headings use {common}.",
                                     "Medium", "Confirmed" if source_kind == "docx" else "Suggestion"))

    # heading hierarchy (DOCX styles only)
    if source_kind == "docx":
        last = 0
        for h in headings:
            if last and h["level"] > last + 1:
                out.append(issue(h["location"], "Formatting", h["text"][:120], f"Use a level-{last + 1} heading here.",
                                 f"Heading jumps from level {last} to level {h['level']}.", "Medium", "Confirmed"))
            last = h["level"]
        if headings:
            for p in body:
                t = p["text"].strip()
                if p["bold"] == "all" and len(t) < 70 and not t.endswith((".", ":", "،")) and not LIST_NUM_RE.match(t):
                    out.append(issue(p["location"], "Formatting", t, "Apply a Heading style.",
                                     "This looks like a heading but uses body text style, so it won't appear in the document outline.",
                                     "Low", "Suggestion"))

    # heading punctuation and capitalization style
    ends = Counter(h["text"].strip()[-1] in ".:" for h in headings if h["text"].strip())
    if len(ends) > 1:
        minority = min(ends, key=ends.get)
        for h in headings:
            if (h["text"].strip()[-1] in ".:") == minority:
                out.append(issue(h["location"], "Formatting", h["text"][:120],
                                 "Remove the final punctuation." if minority else "Match other headings (no final punctuation).",
                                 "Headings end with punctuation inconsistently.", "Low", "Suggestion"))
    styles = {}
    for h in headings:
        words = [w for w in re.findall(r"[A-Za-z]+", h["text"])
                 if len(w) > 3 and not any(c.isupper() for c in w[1:])]  # skip ServiceNow, CMDB, etc.
        if len(words) >= 2 and h["lang"] == "en":
            styles[h["location"]] = ("Title Case" if all(w[0].isupper() for w in words)
                                     else "Sentence case" if not any(w[0].isupper() for w in words[1:]) else None)
    counts = Counter(s for s in styles.values() if s)
    if len(counts) > 1:
        minority = min(counts, key=counts.get)
        for h in headings:
            if styles.get(h["location"]) == minority:
                out.append(issue(h["location"], "Formatting", h["text"][:120], f"Use {max(counts, key=counts.get)} like the other headings.",
                                 f"Heading uses {minority}; most headings use {max(counts, key=counts.get)}.", "Low", "Suggestion"))

    # body text font size / family
    out += _outliers(body, "sizes", "font size", lambda v: "the style's default size" if v is None else f"{v:g} pt", source_kind)
    out += _outliers(body, "fonts", "font", lambda v: "the style's default font" if v is None else v, source_kind)

    # PDF: heading sizes that differ only slightly were probably meant to be the same
    if source_kind == "pdf":
        sizes = Counter(next(iter(h["sizes"])) for h in headings if h["sizes"])
        ordered = sorted(sizes)
        for a, b in zip(ordered, ordered[1:]):
            if b - a <= 1:
                odd = a if sizes[a] <= sizes[b] else b
                main = b if odd == a else a
                for h in headings:
                    if odd in h["sizes"]:
                        out.append(issue(h["location"], "Formatting", h["text"][:120], f"Use {main:g} pt like similar headings.",
                                         f"Heading is {odd:g} pt while similar headings are {main:g} pt.", "Low", "Suggestion"))

    # spacing: consecutive empty paragraphs
    run = []
    for p in paras + [{"kind": "end"}]:
        if p["kind"] == "empty":
            run.append(p)
            continue
        if len(run) >= 2:
            out.append(issue(f"{run[0]['location']} – {run[-1]['location']}", "Formatting", f"{len(run)} empty paragraphs",
                             "Use paragraph spacing instead of empty lines.", "Unnecessary blank lines.", "Low", "Confirmed"))
        run = []

    out += _check_lists(paras)
    return out


def _appearance(p):
    size = "/".join(f"{s:g}pt" for s in sorted(p["sizes"])) or "default size"
    font = "/".join(sorted(p["fonts"])) or "default font"
    return f"{font}, {size}{', bold' if p['bold'] else ''}"


def _outliers(body, field, label, fmt, source_kind):
    values = {id(p): (p[field] or {None}) for p in body}  # None = inherited from the style
    counts = Counter()
    for p in body:
        for v in values[id(p)]:
            counts[v] += len(p["text"])
    if len(counts) < 2:
        return []
    main = counts.most_common(1)[0][0]
    out = []
    for p in body:
        odd = [v for v in values[id(p)] if v != main]
        if odd and len(p["text"]) > 15:
            out.append(issue(p["location"], "Formatting", p["text"][:100], f"Use {fmt(main)} like the rest of the body text.",
                             f"Body text uses {', '.join(fmt(v) for v in odd)} instead of {fmt(main)}.",
                             "Low", "Confirmed" if source_kind == "docx" else "Suggestion"))
    return out[:MAX_PER_RULE]


def _check_lists(paras):
    out = []
    delims, bullets = Counter(), Counter()
    seq, prev_last = [], 0
    auto_items = manual_items = 0

    def close(seq, prev_last):
        if not seq:
            return prev_last
        nums = [n for n, _ in seq]
        if nums[0] != 1 and nums[0] != prev_last + 1:
            out.append(issue(seq[0][1]["location"], "Formatting", seq[0][1]["text"][:100], "Start the list at 1.",
                             f"Numbered list starts at {nums[0]}.", "Medium", "Confirmed"))
        for (a, _), (b, p) in zip(seq, seq[1:]):
            if b != a + 1:
                out.append(issue(p["location"], "Formatting", p["text"][:100], f"{a + 1}",
                                 f"Numbering goes from {a} to {b}.", "Medium", "Confirmed"))
        return nums[-1]

    for p in paras:
        if p["kind"] == "empty":
            continue
        text = p["text"]
        m = LIST_NUM_RE.match(text)
        if m:
            manual_items += 1
            n = int(m.group(1).translate(AR_DIGITS))
            delims[m.group(2)] += 1
            seq.append((n, p))
            continue
        if p["auto_list"]:
            auto_items += 1
        b = BULLET_RE.match(text)
        if b:
            bullets[b.group(1)] += 1
        if p["kind"] == "heading":  # a new section restarts list numbering
            close(seq, prev_last)
            seq, prev_last = [], 0
        elif seq:
            prev_last = close(seq, prev_last)
            seq = []
    close(seq, prev_last)

    if len(delims) > 1:
        out.append(issue("Document", "Formatting", ", ".join(f'"1{d}" ({c}×)' for d, c in delims.items()),
                         f'Use one numbering style, e.g. "1{delims.most_common(1)[0][0]}".',
                         "Numbered lists use different number formats.", "Low", "Suggestion"))
    if len(bullets) > 1:
        out.append(issue("Document", "Formatting", ", ".join(f'"{b}" ({c}×)' for b, c in bullets.items()),
                         f'Use one bullet symbol, e.g. "{bullets.most_common(1)[0][0]}".',
                         "Bullet lists use different bullet symbols.", "Low", "Suggestion"))
    if auto_items and manual_items:
        out.append(issue("Document", "Formatting", f"{manual_items} typed numbers, {auto_items} automatic list items",
                         "Use Word's automatic numbering for all lists.",
                         "Some lists are numbered by hand and others automatically, which can produce inconsistent numbering.",
                         "Low", "Suggestion"))
    return out


# ---------------------------------------------------------------- content

def _norm(sentence):
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", sentence.lower())).strip()


def check_content(paras, terminology):
    out = []
    seen = defaultdict(list)
    for p in paras:
        if p["kind"] != "para":
            continue
        for s in re.split(r"(?<=[.!?؟])\s+", p["text"]):
            if len(s.split()) >= 6:
                seen[_norm(s)].append((p["location"], s.strip()))
    for occurrences in seen.values():
        locs = list(dict.fromkeys(l for l, _ in occurrences))
        if len(locs) > 1:
            out.append(issue(locations(locs), "Clarity", occurrences[0][1][:200], "Keep one occurrence or refer back to it.",
                             f"The same sentence appears {len(occurrences)} times.", "Low", "Suggestion"))

    # bilingual comparison
    en = [p for p in paras if p["lang"] == "en" and p["kind"] != "empty"]
    ar = [p for p in paras if p["lang"] == "ar" and p["kind"] != "empty"]
    en_len, ar_len = sum(len(p["text"]) for p in en), sum(len(p["text"]) for p in ar)
    # only compare when the Arabic part looks like a translation of the English part (similar length)
    if len(en) >= 3 and len(ar) >= 3 and 0.5 <= ar_len / max(en_len, 1) <= 2:
        def terms_in(ps):
            text = " ".join(p["text"] for p in ps).lower()
            return {t for t in terminology.terms if len(t) > 3 and re.search(r"(?<![\w])" + re.escape(t.lower()) + r"(?![\w])", text)}
        en_terms, ar_terms = terms_in(en), terms_in(ar)
        def outer(terms):  # "Knowledge" is covered by "Knowledge Article"
            return {t for t in terms if not any(t != o and t.lower() in o.lower() for o in terms)}
        if ar_terms:  # the Arabic text keeps English interface labels, so the sets are comparable
            for missing, where, other in ((outer(en_terms - ar_terms), "Arabic sections", "English"),
                                          (outer(ar_terms - en_terms), "English sections", "Arabic")):
                if missing:
                    out.append(issue(where, "Terminology", ", ".join(sorted(missing)),
                                     f"Check whether the {where.split()[0]} text should also reference these terms.",
                                     f"These ServiceNow terms appear only in the {other} text.", "Low", "Needs Manual Review"))

        def steps(ps):
            return sum(1 for p in ps if LIST_NUM_RE.match(p["text"]) or p["auto_list"])
        se, sa = steps(en), steps(ar)
        if se and sa and se != sa:
            out.append(issue("Document", "Clarity", f"English: {se} list items, Arabic: {sa} list items",
                             "Check that both versions contain the same steps.",
                             "The English and Arabic versions have a different number of numbered/list steps.",
                             "Medium", "Needs Manual Review"))
        he = sum(1 for p in en if p["kind"] == "heading")
        ha = sum(1 for p in ar if p["kind"] == "heading")
        if he and ha and he != ha:
            out.append(issue("Document", "Clarity", f"English: {he} headings, Arabic: {ha} headings",
                             "Check that both versions have the same sections.",
                             "The English and Arabic versions have a different number of headings.", "Low", "Needs Manual Review"))
    return out


# ---------------------------------------------------------------- entry point

def run_all(paras, source_kind):
    terminology = Terminology()
    results = []
    for rule in (lambda: check_terminology(paras, terminology),
                 lambda: check_english_spelling(paras, terminology),
                 lambda: check_arabic(paras),
                 lambda: check_arabic_spelling(paras),
                 lambda: check_english(paras),
                 lambda: check_common(paras),
                 lambda: check_formatting(paras, source_kind),
                 lambda: check_ui_references(paras),
                 lambda: check_content(paras, terminology)):
        found = rule()
        results += _dedupe(found)[:MAX_PER_RULE * 4]
    return _dedupe(results)


def _dedupe(found):
    seen, out = set(), []
    for i in found:
        key = (i["location"], i["issue_type"], i["original_text"], i["explanation"])
        if key not in seen:
            seen.add(key)
            out.append(i)
    return out
