#!/usr/bin/env python3
"""Mechanical statistics for a piece of writing, to support a human-quality review.

Usage:
    python text_stats.py <file> [--limit N] [--json]

Supports .txt, .md, .docx (stdlib only) and .pdf (needs `pdftotext` or the
`pypdf` package). Pass "-" to read plain text from stdin.

The output is evidence for the reviewer to check, not a verdict: e.g. a
"passive" hit may be perfectly appropriate in a methods section.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import zipfile
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree as ET

STOPWORDS = set("""
a about above after again against all also am an and any are as at be because been
before being below between both but by can could did do does doing down during each
few for from further had has have having he her here hers herself him himself his how
i if in into is it its itself just me more most my myself no nor not now of off on
once only or other our ours ourselves out over own same she should so some such than
that the their theirs them themselves then there these they this those through to too
under until up very was we were what when where which while who whom why will with
would you your yours yourself yourselves one two may might must shall us upon within
without however therefore thus also etc e g i.e eg ie et al
""".split())

HEDGES = [
    "very", "really", "quite", "rather", "somewhat", "basically", "actually",
    "literally", "extremely", "clearly", "obviously", "arguably", "perhaps",
    "in order to", "due to the fact that", "it is important to note",
    "it should be noted", "it can be seen that", "a number of", "in terms of",
    "the fact that", "at the end of the day", "needless to say",
]

# Phrases that read as generic / machine-written; flag as a style issue only.
AI_TELLS = [
    "delve", "tapestry", "testament to", "in today's", "fast-paced world",
    "ever-evolving", "ever-changing", "navigate the complexities", "multifaceted",
    "plays a crucial role", "plays a pivotal role", "pivotal", "underscore",
    "showcasing", "realm", "a myriad of", "seamlessly", "robust", "leverage",
    "holistic", "in conclusion, it is clear", "shed light on", "paving the way",
    "it is worth noting", "not only", "furthermore,", "moreover,",
]

CONFUSABLES = [
    "affect", "effect", "its", "it's", "their", "there", "they're", "then", "than",
    "principal", "principle", "complement", "compliment", "lose", "loose",
    "whose", "who's", "your", "you're", "less", "fewer", "comprise", "comprised of",
    "criteria", "phenomena", "data is", "alot", "could of", "should of", "would of",
]

PASSIVE_RE = re.compile(
    r"\b(am|is|are|was|were|be|been|being)\s+(\w+ly\s+)?(\w+ed|\w+en|made|done|found|shown|given|taken|seen|known|held|built|set|put|cut|read|led|sought|thought|brought|taught)\b",
    re.I,
)
CITE_AUTHOR_YEAR = re.compile(r"\([A-Z][A-Za-z'\-]+(?: et al\.?| (?:and|&) [A-Z][A-Za-z'\-]+)?,? \d{4}[a-z]?(?:[,;] ?(?:p+\. ?)?\d+(?:[-–]\d+)?)?\)")
CITE_NUMERIC = re.compile(r"\[\d+(?:[,–-]\s?\d+)*\]")


def read_docx(path):
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    paras = []
    for p in root.iter(f"{{{ns['w']}}}p"):
        texts = []
        for node in p.iter():
            if node.tag == f"{{{ns['w']}}}t" and node.text:
                texts.append(node.text)
            elif node.tag == f"{{{ns['w']}}}tab":
                texts.append("\t")
        paras.append("".join(texts))
    return "\n\n".join(paras)


def read_pdf(path):
    if shutil.which("pdftotext"):
        out = subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True)
        if out.returncode == 0:
            return out.stdout
    try:
        from pypdf import PdfReader
    except ImportError:
        sys.exit("Cannot read PDF: install poppler-utils (pdftotext) or `pip install pypdf`.")
    return "\n\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)


def load(path_str):
    if path_str == "-":
        return sys.stdin.read()
    path = Path(path_str)
    suffix = path.suffix.lower()
    if suffix == ".docx":
        return read_docx(path)
    if suffix == ".pdf":
        return read_pdf(path)
    return path.read_text(encoding="utf-8", errors="replace")


def split_paragraphs(text):
    paras = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n|\r\n\s*\r\n", text)]
    if len(paras) <= 1:  # single-newline documents
        paras = [re.sub(r"\s+", " ", p).strip() for p in text.splitlines()]
    return [p for p in paras if p]


ABBREV = r"(?<!\be\.g\.)(?<!\bi\.e\.)(?<!\bet al\.)(?<!\bMr\.)(?<!\bMrs\.)(?<!\bDr\.)(?<!\bvs\.)(?<!\bcf\.)(?<!\bp\.)(?<!\bpp\.)(?<!\bno\.)(?<!\bFig\.)"


def split_sentences(para):
    parts = re.split(ABBREV + r"(?<=[.!?])[\"')\]]?\s+(?=[\"'(\[]?[A-Z0-9])", para)
    return [s.strip() for s in parts if s.strip()]


def words_of(text):
    return re.findall(r"[A-Za-z][A-Za-z'\-]*", text)


def syllables(word):
    w = word.lower().strip("'")
    if len(w) <= 3:
        return 1
    w = re.sub(r"(?:[^laeiouy]es|ed|[^laeiouy]e)$", "", w)
    w = re.sub(r"^y", "", w)
    return max(1, len(re.findall(r"[aeiouy]{1,2}", w)))


def is_heading(p):
    return len(words_of(p)) <= 12 and not re.search(r"[.!?]$", p)


def analyse(text, limit=None):
    paras = split_paragraphs(text)
    body = [p for p in paras if not is_heading(p)]
    all_words = words_of(text)
    wc = len(all_words)

    sentences = []  # (para_no, sentence)
    for i, p in enumerate(body, 1):
        for s in split_sentences(p):
            sentences.append((i, s))
    sent_lens = [len(words_of(s)) for _, s in sentences] or [0]

    syl = sum(syllables(w) for w in all_words)
    n_sent = max(1, len(sentences))
    flesch = 206.835 - 1.015 * (wc / n_sent) - 84.6 * (syl / max(1, wc))
    fk_grade = 0.39 * (wc / n_sent) + 11.8 * (syl / max(1, wc)) - 15.59

    lower = [w.lower() for w in all_words]
    content = [w for w in lower if w not in STOPWORDS and len(w) > 3]
    top = Counter(content).most_common(15)

    starts = Counter(" ".join(words_of(s)[:2]).lower() for _, s in sentences if len(words_of(s)) >= 2)
    rep_starts = [(k, v) for k, v in starts.most_common(8) if v >= 3]

    lt = text.lower()

    def count_phrases(phrases):
        found = {}
        for ph in phrases:
            n = len(re.findall(r"(?<![a-z])" + re.escape(ph) + r"(?![a-z])", lt))
            if n:
                found[ph] = n
        return dict(sorted(found.items(), key=lambda kv: -kv[1]))

    doubled = sorted(set(m.group(0) for m in re.finditer(r"\b(\w+)\s+\1\b", text, re.I) if not m.group(1).isdigit()))

    passive = [(p, s) for p, s in sentences if PASSIVE_RE.search(s)]

    para_lens = [(i, len(words_of(p))) for i, p in enumerate(body, 1)]

    result = {
        "word_count": wc,
        "word_limit": limit,
        "limit_status": None,
        "paragraphs": len(body),
        "headings_detected": len(paras) - len(body),
        "sentences": len(sentences),
        "avg_sentence_words": round(sum(sent_lens) / n_sent, 1),
        "longest_sentence_words": max(sent_lens),
        "long_sentences_over_40_words": [
            {"para": p, "words": len(words_of(s)), "start": " ".join(s.split()[:12]) + "…"}
            for p, s in sentences if len(words_of(s)) > 40
        ],
        "very_short_paragraphs_under_40_words": [i for i, n in para_lens if n < 40],
        "very_long_paragraphs_over_250_words": [i for i, n in para_lens if n > 250],
        "flesch_reading_ease": round(flesch, 1),
        "flesch_kincaid_grade": round(fk_grade, 1),
        "lexical_diversity_type_token": round(len(set(lower)) / max(1, wc), 3),
        "most_frequent_content_words": top,
        "repeated_sentence_openings": rep_starts,
        "passive_voice_candidates": {"count": len(passive), "share_of_sentences": round(len(passive) / n_sent, 2),
                                     "examples": [{"para": p, "sentence": s[:140]} for p, s in passive[:8]]},
        "hedges_and_filler": count_phrases(HEDGES),
        "generic_ai_style_phrases": count_phrases(AI_TELLS),
        "commonly_confused_words_present": count_phrases(CONFUSABLES),
        "doubled_words": doubled,
        "citations": {"author_year": len(CITE_AUTHOR_YEAR.findall(text)), "numeric": len(CITE_NUMERIC.findall(text))},
        "first_person_i_we": sum(1 for w in lower if w in ("i", "we", "my", "our", "me", "us")),
        "contractions": len(re.findall(r"\b\w+'(?:t|s|re|ve|ll|d|m)\b", text, re.I)),
        "exclamation_marks": text.count("!"),
    }
    if limit:
        diff = wc - limit
        pct = diff / limit * 100
        result["limit_status"] = f"{'over' if diff > 0 else 'under'} by {abs(diff)} words ({pct:+.1f}%)"
    return result


def print_report(r):
    print("=== TEXT STATISTICS (evidence to check, not verdicts) ===")
    print(f"Words: {r['word_count']}" + (f"  | limit {r['word_limit']}: {r['limit_status']}" if r["word_limit"] else ""))
    print(f"Paragraphs: {r['paragraphs']} (+{r['headings_detected']} heading-like lines) | Sentences: {r['sentences']}")
    print(f"Avg sentence: {r['avg_sentence_words']} words | Longest: {r['longest_sentence_words']}")
    print(f"Flesch reading ease: {r['flesch_reading_ease']} | F-K grade: {r['flesch_kincaid_grade']} | Type-token ratio: {r['lexical_diversity_type_token']}")
    print(f"Citations — author-year: {r['citations']['author_year']}, numeric: {r['citations']['numeric']}")
    print(f"First-person words: {r['first_person_i_we']} | Contractions: {r['contractions']} | '!': {r['exclamation_marks']}")
    sections = [
        ("Long sentences (>40 words)", r["long_sentences_over_40_words"]),
        ("Short paragraphs (<40 words), ¶#", r["very_short_paragraphs_under_40_words"]),
        ("Long paragraphs (>250 words), ¶#", r["very_long_paragraphs_over_250_words"]),
        ("Most frequent content words", r["most_frequent_content_words"]),
        ("Repeated sentence openings (≥3)", r["repeated_sentence_openings"]),
        ("Hedges / filler", r["hedges_and_filler"]),
        ("Generic / AI-style phrases", r["generic_ai_style_phrases"]),
        ("Commonly confused words present (check usage)", r["commonly_confused_words_present"]),
        ("Doubled words", r["doubled_words"]),
    ]
    for title, val in sections:
        if val:
            print(f"\n{title}:")
            items = val.items() if isinstance(val, dict) else val
            for item in items:
                print(f"  - {item}")
    pv = r["passive_voice_candidates"]
    print(f"\nPassive-voice candidates: {pv['count']} ({pv['share_of_sentences']:.0%} of sentences)")
    for ex in pv["examples"]:
        print(f"  - ¶{ex['para']}: {ex['sentence']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file")
    ap.add_argument("--limit", type=int, help="word limit to check against")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    args = ap.parse_args()
    r = analyse(load(args.file), args.limit)
    if args.json:
        print(json.dumps(r, indent=2, ensure_ascii=False))
    else:
        print_report(r)


if __name__ == "__main__":
    main()
