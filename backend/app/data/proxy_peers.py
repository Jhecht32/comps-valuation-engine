"""Proxy-statement peer groups: models, name index, and the parser.

Public companies disclose the peer group their compensation committee
benchmarks against in the annual proxy statement (SEC form DEF 14A),
usually in the Compensation Discussion and Analysis under a heading or
lead-in such as "Peer Group" or "compensation peer group", as a table
of company names. That list is the company's own stated competitive
set, sourced and defensible in a way a screen on a data vendor's
industry buckets is not, so it is the first place peer discovery looks.
It is a compensation benchmark, though, not a valuation one: a company
that competes for executives with Microsoft will list Microsoft.

This module is network-free. It holds the result models, the name
index that maps proxy names to tickers (built from the SEC's
company_tickers.json), the HTML-to-lines conversion, and the parser.
app.data.edgar fetches the filings.

Extraction is heuristic, because it parses prose. The rule: after a
line mentioning a peer group, find the first line that is a company
name in the index, then take the run of consecutive name lines,
tolerating a couple of non-name lines for page headers and table
columns; a paragraph of prose ends the run. Names in the run the index
cannot map are returned separately, never dropped. The run with the
most distinct tickers wins; a comma-separated list inside one sentence
is tried when no run is found. Confidence says how far to trust the
result, and the service falls back to the industry screen below medium.
"""

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from html.parser import HTMLParser
from typing import Iterable, Mapping

from pydantic import BaseModel

# --- Errors ---------------------------------------------------------------


class ProxyPeersError(Exception):
    """A proxy peer group could not be produced for a ticker; the message
    says why. Never fatal to a peer suggestion: the service turns it into
    a flag and falls back to screening."""

    def __init__(self, ticker: str, detail: str):
        self.ticker = ticker
        self.detail = detail
        super().__init__(f"{ticker}: {detail}")


class ProxyFilingNotFoundError(ProxyPeersError):
    """The SEC does not know the ticker, or it has no DEF 14A on file
    (foreign private issuers and new listings file none)."""


class PeerGroupNotFoundError(ProxyPeersError):
    """The filing was fetched but no list of peer companies was recognised."""


class EdgarUnavailableError(ProxyPeersError):
    """EDGAR could not be reached or refused the request."""


# --- Models ---------------------------------------------------------------


class Confidence(str, Enum):
    HIGH = "high"        # eight or more names mapped to tickers, most of the list
    MEDIUM = "medium"    # four to seven mapped, or a list with many unmapped names
    LOW = "low"          # one to three mapped; not used as a peer set


HIGH_CONFIDENCE_MIN = 8
MEDIUM_CONFIDENCE_MIN = 4


def confidence_for(matched: int, unmatched: int) -> Confidence:
    if matched >= HIGH_CONFIDENCE_MIN and unmatched <= matched:
        return Confidence.HIGH
    if matched >= MEDIUM_CONFIDENCE_MIN:
        return Confidence.MEDIUM
    return Confidence.LOW


class ProxyPeer(BaseModel):
    name: str    # as written in the proxy
    ticker: str  # SEC ticker the name mapped to


class ExtractedPeerGroup(BaseModel):
    """What the parser found in one document, before filing metadata."""

    peers: list[ProxyPeer]
    unmatched: list[str]
    confidence: Confidence
    context: str  # the line that introduced the list, for the reader to check


class ProxyPeerGroup(ExtractedPeerGroup):
    """A company's disclosed compensation peer group with its provenance."""

    ticker: str
    company_name: str | None = None
    filing_date: date
    accession_number: str
    document_url: str

    @property
    def proxy_year(self) -> int:
        return self.filing_date.year


# --- Source interface -----------------------------------------------------


class ProxyPeerSource(ABC):
    @abstractmethod
    def proxy_peer_group(self, ticker: str) -> ProxyPeerGroup:
        """The most recent proxy statement's peer group for a ticker.
        Raises a ProxyPeersError subclass when there is none to give."""


class FixtureProxyPeerSource(ProxyPeerSource):
    """Canned groups (or canned failures) keyed by ticker, for tests."""

    def __init__(self, groups: Mapping[str, ProxyPeerGroup | Exception] | None = None):
        self._groups = {k.strip().upper(): v for k, v in (groups or {}).items()}
        self.calls: list[str] = []

    def proxy_peer_group(self, ticker: str) -> ProxyPeerGroup:
        key = ticker.strip().upper()
        self.calls.append(key)
        item = self._groups.get(key)
        if item is None:
            raise ProxyFilingNotFoundError(key, "no proxy statement on file")
        if isinstance(item, Exception):
            raise item
        return item.model_copy(deep=True)


# --- Name index -----------------------------------------------------------

# Legal-form suffixes stripped from the end of a name, repeatedly, so
# "The TJX Companies, Inc." and "TJX COMPANIES INC /DE/" meet at "tjx".
# Distinguishing words such as Holdings or Group are kept.
_LEGAL_SUFFIXES = frozenset(
    "inc incorporated corp corporation co cos company companies ltd limited plc llc lp llp "
    "ag se sa nv spa".split()
)

# Words that mark a line as a company name even when the index cannot
# map it, so a foreign peer such as "adidas AG" is reported, not lost.
_CORPORATE_WORDS = _LEGAL_SUFFIXES | frozenset(
    "group holdings holding international industries brands stores technologies technology "
    "systems foods motors energy partners trust bancorp financial enterprises worldwide "
    "global resources".split()
)

# Single words that read as a company name in the index (Target, Total)
# but are compensation vocabulary when they stand alone on a line.
_STOP_KEYS = frozenset(
    "target threshold maximum minimum total base cash equity stock performance options other "
    "none company companies peer peers revenue revenues market median average name ticker "
    "industry sector notes".split()
)

_STOP_LINES = frozenset({"table of contents", "peer group", "retail peers", "peer companies", "market cap",
                         "market capitalization", "company name", "peer"})


def name_key(name: str) -> str:
    """Normalise a company name for matching: casefold, drop state-of-
    incorporation tags and parentheticals, strip a leading "the" and
    trailing legal suffixes, then remove every non-alphanumeric so
    "O'Reilly", "O Reilly", and "O’Reilly" agree."""
    s = name.casefold().replace("’", "'")
    s = re.sub(r"\s*/\s*[a-z]{2,4}\b/?", " ", s)  # /DE/ /NEW /MD SA/NV: state or listing tags
    s = re.sub(r"\(.*?\)", " ", s)  # (NEW), (DEL), footnote markers, tickers
    s = re.sub(r"\b([a-z])\.([a-z])\.?", r"\1\2", s)  # l.p. -> lp, n.v. -> nv
    s = s.replace("'", "")
    tokens = re.sub(r"[^a-z0-9]+", " ", s).split()
    while tokens and tokens[0] == "the":
        tokens.pop(0)
    while len(tokens) > 1 and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
    return "".join(tokens)


def normalise_sec_ticker(ticker: str) -> str:
    """SEC and Yahoo both write share classes with a hyphen (BRK-B); a
    dotted form is accepted and folded."""
    return ticker.strip().upper().replace(".", "-")


class IndexEntry(BaseModel):
    ticker: str
    cik: int
    title: str


class NameIndex:
    """Company name -> ticker and CIK, from the SEC's company_tickers.json
    (a mapping of row number to {cik_str, ticker, title}). The first row
    for a name wins, which keeps the primary listing of a dual-class
    company since the file lists larger listings first."""

    def __init__(self, entries: Iterable[IndexEntry]):
        self._by_key: dict[str, IndexEntry] = {}
        self._by_ticker: dict[str, IndexEntry] = {}
        for e in entries:
            self._by_key.setdefault(name_key(e.title), e)
            self._by_ticker.setdefault(normalise_sec_ticker(e.ticker), e)

    @classmethod
    def from_sec_company_tickers(cls, payload: Mapping) -> "NameIndex":
        rows = payload.values() if isinstance(payload, Mapping) else payload
        return cls(
            IndexEntry(ticker=str(r["ticker"]), cik=int(r["cik_str"]), title=str(r["title"]))
            for r in rows
        )

    def __len__(self) -> int:
        return len(self._by_ticker)

    def lookup(self, name: str) -> IndexEntry | None:
        key = name_key(name)
        return self._by_key.get(key) if key else None

    def by_ticker(self, ticker: str) -> IndexEntry | None:
        return self._by_ticker.get(normalise_sec_ticker(ticker))


# --- Filing selection -----------------------------------------------------


class Filing(BaseModel):
    accession_number: str
    filing_date: date
    primary_document: str
    form: str


def latest_filing(submissions: Mapping, *, form: str = "DEF 14A") -> Filing | None:
    """The newest filing of one form type in a submissions API payload
    (data.sec.gov/submissions/CIK##########.json). Only the recent block
    is read; it spans years for any regular filer."""
    recent = submissions.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    rows = zip(forms, recent.get("accessionNumber", []), recent.get("filingDate", []), recent.get("primaryDocument", []))
    best: Filing | None = None
    for f, accession, filed, doc in rows:
        if f != form or not doc:
            continue
        candidate = Filing(accession_number=accession, filing_date=date.fromisoformat(filed), primary_document=doc, form=f)
        if best is None or candidate.filing_date > best.filing_date:
            best = candidate
    return best


def document_url(cik: int, accession_number: str, primary_document: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession_number.replace('-', '')}/{primary_document}"


# --- HTML to lines --------------------------------------------------------


class _LineExtractor(HTMLParser):
    _BLOCK = frozenset(
        "p div tr td th li br h1 h2 h3 h4 h5 h6 table ul ol section article header footer".split()
    )

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        if tag in self._BLOCK:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        if tag in self._BLOCK:
            self._parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def html_to_lines(html: str) -> list[str]:
    """One line per block element or table cell, whitespace collapsed,
    empty lines dropped. Inline markup (spans, bold, links) is joined,
    so a sentence split across styled spans reads as one line."""
    parser = _LineExtractor()
    parser.feed(html)
    parser.close()
    text = parser.text().replace("\xa0", " ").replace("​", "")
    lines = (re.sub(r"\s+", " ", line).strip() for line in text.split("\n"))
    return [line for line in lines if line]


# --- Parser ---------------------------------------------------------------

ANCHOR = re.compile(
    r"peer\s+group|compensation\s+peer|peer\s+compan|comparator\s+group|peer\s+set|benchmark(?:ing)?\s+(?:group|peers)",
    re.IGNORECASE,
)
LOOKAHEAD = 40   # lines after an anchor within which the list must begin
MAX_GAP = 4      # consecutive non-name lines tolerated inside a list (a page break is three)
MAX_RUN = 150    # lines a list may span, table columns included
PROSE_LENGTH = 90  # a line this long is a paragraph, which ends a list

_FOOTNOTE = re.compile(r"\(\d+\)|\[\d+\]|[*†‡§]+|(?<=\S)\d$")


def _clean(line: str) -> str:
    return _FOOTNOTE.sub("", line).strip(" ,;:-–—")


def _tokens(line: str) -> list[str]:
    return re.sub(r"[^a-z0-9&]+", " ", line.casefold()).split()


def _has_corporate_word(line: str) -> bool:
    return any(t in _CORPORATE_WORDS for t in _tokens(line))


@dataclass
class _Line:
    text: str
    entry: IndexEntry | None      # index match for the whole line
    namelike: bool                # unmatched but reads as a company name
    prose: bool                   # a paragraph or lead-in; ends a list


def _classify(line: str, index: NameIndex, target: str) -> _Line:
    cleaned = _clean(line)
    prose = len(line) > PROSE_LENGTH or line.endswith(":")
    if prose or not cleaned:
        return _Line(line, None, False, prose)
    entry = index.lookup(cleaned)
    if entry is not None:
        key = name_key(cleaned)
        if key in _STOP_KEYS and not _has_corporate_word(cleaned):
            entry = None  # "Target" alone is a bonus level, "Target Corporation" a company
        elif normalise_sec_ticker(entry.ticker) == target:
            entry = None  # the filer naming itself is not a peer
    namelike = (
        entry is None
        and cleaned.casefold() not in _STOP_LINES
        and len(_tokens(cleaned)) <= 8
        and "$" not in cleaned
        and _has_corporate_word(cleaned)
    )
    return _Line(cleaned if entry or namelike else line, entry, namelike, prose)


_FIGURE = re.compile(r"^[\s$€£(]*[-–—]?[\d.,]+\s*[%x)]*(?:\s*(?:mm|bn|million|billion))?$", re.IGNORECASE)


def _is_figure(text: str) -> bool:
    return bool(_FIGURE.match(text)) or text in {"—", "–", "-", "n/a", "N/A"}


def _interior_name(line: _Line) -> bool:
    """A line between two matched names that could be a company the
    index lacks: short, wordy, no figures."""
    t = line.text
    return (
        not line.prose
        and t.casefold() not in _STOP_LINES
        and 0 < len(_tokens(t)) <= 6
        and not re.search(r"[\d$%]", t)
        and re.search(r"[A-Za-z]", t) is not None
    )


@dataclass
class _Run:
    start: int
    context: str
    peers: list[ProxyPeer] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)
    tickers: set[str] = field(default_factory=set)

    @property
    def score(self) -> int:
        return len(self.tickers)

    def add(self, line: _Line) -> None:
        if line.entry is not None:
            if line.entry.ticker not in self.tickers:
                self.tickers.add(line.entry.ticker)
                self.peers.append(ProxyPeer(name=line.text, ticker=line.entry.ticker))
        elif line.text not in self.unmatched:
            self.unmatched.append(line.text)


def _run_from(lines: list[_Line], start: int, context: str) -> _Run:
    run = _Run(start=start, context=context)
    pending: list[_Line] = []  # interior lines awaiting a later match to confirm them
    gap = 0
    last_name = start
    for i in range(start, min(start + MAX_RUN, len(lines))):
        line = lines[i]
        if line.prose:
            break
        if line.entry is not None:
            for p in pending:
                run.add(p)
            pending.clear()
            run.add(line)
            gap = 0
            last_name = i
        elif line.namelike:
            pending.append(line)
            gap = 0
        elif _interior_name(line):
            pending.append(line)
            gap += 1
        elif not _is_figure(line.text):
            gap += 1  # figures are table columns beside a name, not a gap
        if gap > MAX_GAP:
            break
    # Names after the last index match count only when they carry a
    # corporate word, so a heading that follows the table is not swept in.
    for p in pending:
        if p.namelike:
            run.add(p)
    return run


def _prose_run(lines: list[str], i: int, index: NameIndex, target: str) -> _Run | None:
    """A comma-separated list inside one sentence: "our peer group
    consisted of A, B, C and D"."""
    line = lines[i]
    if len(line) < 40 or re.search(r"exclud", line, re.IGNORECASE):
        return None
    run = _Run(start=i, context=line[:160])
    for piece in re.split(r",|;|\band\b", line):
        cleaned = _clean(piece)
        if not cleaned:
            continue
        words = cleaned.split()
        if len(words) > 8:
            # The lead-in rides on the first name ("...consisted of Target
            # Corporation"): try the trailing words as a name, longest first.
            for k in range(8, 0, -1):
                tail = _classify(" ".join(words[-k:]), index, target)
                if tail.entry is not None:
                    run.add(tail)
                    break
            continue
        classified = _classify(cleaned, index, target)
        if classified.entry is not None or classified.namelike:
            run.add(classified)
    return run if run.score >= 2 else None


def extract_peer_group(lines: list[str], index: NameIndex, *, target_ticker: str) -> ExtractedPeerGroup:
    """Find the peer group in a proxy statement's lines. Raises
    PeerGroupNotFoundError when no list with at least one mapped name
    follows any peer-group mention."""
    target = normalise_sec_ticker(target_ticker)
    classified = [_classify(line, index, target) for line in lines]
    runs: dict[int, _Run] = {}  # by start line; the nearest anchor names the context
    for i, line in enumerate(lines):
        if not ANCHOR.search(line):
            continue
        for j in range(i + 1, min(i + 1 + LOOKAHEAD, len(lines))):
            if classified[j].entry is None:
                continue
            if j in runs:
                runs[j].context = line[:160]
            else:
                runs[j] = _run_from(classified, j, context=line[:160])
            break
        prose = _prose_run(lines, i, index, target)
        if prose is not None:
            runs.setdefault(-i, prose)
    best = max(runs.values(), key=lambda r: (r.score, -abs(r.start)), default=None)
    if best is None or best.score == 0:
        raise PeerGroupNotFoundError(target, "no list of peer companies was recognised in the proxy statement")
    return ExtractedPeerGroup(
        peers=best.peers,
        unmatched=best.unmatched,
        confidence=confidence_for(len(best.peers), len(best.unmatched)),
        context=best.context,
    )
