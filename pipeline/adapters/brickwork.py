import html as htmllib
import io
import re
import time
from datetime import date
from urllib.parse import unquote, urljoin

import requests

from .base import Adapter

SITE = 'https://www.brickworkratings.com/'
FEED_URL = SITE + 'PressRelease.aspx'    # "Latest Rationales" list: plain HTML, newest first
PAUSE_SECONDS = 0.4                      # be polite to the site

HEADERS = {
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-GB,en-US;q=0.9,en;q=0.8',
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36'),
}

MONTHS = {m: i for i, m in enumerate(
    ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'], 1)}

# one list entry: aria-label="BWR BB + : Company Name Long Term Bank Loan  ₹10.73 Crore PDF File Opens in a new window"
_ENTRY_RE = re.compile(r'hlPressRelease_\d+"\s+aria-label="([^"]*)"\s+href="([^"]*)"')
_LABEL_RE = re.compile(r'^(BWR[^:]*?)\s*:\s*(.+?)\s+((?:Long|Short)\s+Term.*?)\s*PDF File Opens')
_AMOUNT_RE = re.compile(r'\s*₹\s*([\d,]+(?:\.\d+)?)\s*Crore\s*')
_DATE_RE = re.compile(r'\b(\d{1,2})\s*([A-Za-z]{3,9})\s*(\d{4})\b')
_FILE_DATE_RE = re.compile(r'(\d{1,2})([A-Z][a-z]{2})(\d{4})')
_OUTLOOK_RE = re.compile(
    r'(BWR\s*[A-D]{1,3}(?:\s*\d)?\s*[+\-]?)\s*/\s*(Stable|Positive|Negative|Developing)', re.I)


# ------------------------------------------------------------ helpers
def _clean(s) -> str:
    return re.sub(r'\s+', ' ', s or '').strip()


def norm_rating(r: str) -> str:
    """'BWR BB +' -> 'BWR BB+' (the way the dashboard stores it)."""
    r = _clean(r)
    return re.sub(r'\s+([+\-])', r'\1', r)


def _rating_key(r: str) -> str:
    return re.sub(r'\s+', '', r or '').upper()


def doc_id(link: str) -> str:
    """Identity of a rating document, whether the row holds a full URL or a short one.
    bcrisp page -> 'id222798'; PDF -> its file name in lower case."""
    s = (link or '').strip()
    m = re.search(r'[?&]id=(\d+)', s)
    if m:
        return 'id' + m.group(1)
    name = unquote(s.rstrip('/').split('/')[-1]).lower()
    return re.sub(r'\s+', ' ', name)


def _mk_date(d, mon, y):
    m = MONTHS.get(mon[:3].lower())
    if not m:
        return None
    try:
        return date(int(y), m, int(d))
    except ValueError:
        return None


def date_from_filename(link: str):
    """'.../GHV-India-5Nov2026.pdf' -> date(2026, 11, 5); None if there is no date in the name."""
    m = _FILE_DATE_RE.search(unquote(link or ''))
    return _mk_date(*m.groups()) if m else None


def date_from_text(text: str):
    """First readable date in a page, e.g. '05Nov2026' or '05 November 2026'."""
    for m in _DATE_RE.finditer(text or ''):
        d = _mk_date(*m.groups())
        if d and 2000 <= d.year <= 2100:
            return d
    return None


def html_to_text(html: str) -> str:
    t = re.sub(r'<(script|style)[^>]*>.*?</\1>', ' ', html or '', flags=re.S | re.I)
    t = re.sub(r'<[^>]+>', ' ', t)
    return _clean(htmllib.unescape(t))


def find_outlook(text: str, rating: str):
    """Outlook that goes with a Long Term rating, read from the page's rating table.
    The table shows the previous rating first and the present one after it, so the LAST match wins.
    Returns 'Stable' / 'Positive' / 'Negative' / 'Developing', or None."""
    cut = text.find('Note:')
    head = text[: cut if cut > 0 else 6000]
    want = _rating_key(rating)
    found = None
    for m in _OUTLOOK_RE.finditer(head):
        if _rating_key(m.group(1)) == want:
            found = m.group(2).capitalize()
    return found


def parse_entry(label: str):
    """'BWR B - : Ravitej Projects Pvt. Ltd. Long Term Bank Loan  ₹3.87 Crore' ->
    dict(rating, company, term, instrument, amount) or None."""
    m = _LABEL_RE.match(_clean(htmllib.unescape(label)))
    if not m:
        return None
    rating = norm_rating(m.group(1))
    company = _clean(m.group(2))
    rest = m.group(3)
    term = 'LT' if rest.lower().startswith('long') else 'ST'
    am = _AMOUNT_RE.search(rest)
    amount = None
    if am:
        try:
            amount = 'Rs. {:,.2f} Cr.'.format(float(am.group(1).replace(',', '')))
        except ValueError:
            amount = None
        rest = (rest[:am.start()] + ' ' + rest[am.end():])
    instrument = _clean(rest)
    return {'rating': rating, 'company': company, 'term': term, 'instrument': instrument, 'amount': amount}


def parse_feed(html: str) -> list:
    """List of {'link', 'doc', 'rating', 'company', 'term', 'instrument', 'amount'} in page order."""
    out = []
    for label, href in _ENTRY_RE.findall(html or ''):
        e = parse_entry(label)
        if not e:
            print(f'WARNING: Brickwork: could not read list entry: {_clean(label)[:120]}')
            continue
        href = htmllib.unescape(href).strip()
        e['link'] = urljoin(SITE, href)
        e['doc'] = doc_id(e['link'])
        out.append(e)
    return out


class BrickworkAdapter(Adapter):
    name = 'Brickwork Ratings'
    enabled = True

    def key(self, row: dict) -> tuple:
        return (self.name, doc_id(row.get('Link')), row.get('Instrument / Facility') or '',
                row.get('Amount') or '', row.get('Rating') or '')

    # ---- network ---------------------------------------------------------
    def _get(self, url: str, allow_missing=False, timeout=90):
        """GET with retries. Returns the response, or None for 404/410 when allow_missing."""
        last = None
        for attempt in range(3):
            try:
                r = requests.get(url, headers=HEADERS, timeout=timeout)
                if allow_missing and r.status_code in (404, 410):
                    return None
                r.raise_for_status()
                return r
            except Exception as e:
                last = e
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f'Brickwork request failed: {url}: {last}')

    def _pdf_text(self, url: str):
        """Text of a PDF; None if it cannot be downloaded or read (callers treat this as optional)."""
        try:
            from pypdf import PdfReader
            r = self._get(url, allow_missing=True)
            if r is None:
                return None
            reader = PdfReader(io.BytesIO(r.content))
            pages = reader.pages[:3]
            return _clean('\n'.join((p.extract_text() or '') for p in pages))
        except Exception as e:
            print(f'WARNING: Brickwork: could not read PDF {url}: {e}')
            return None

    # ---- one document ----------------------------------------------------
    def _doc_info(self, link: str):
        """Returns (date or None, page text or None). Date is essential; the text gives the outlook."""
        if link.lower().endswith('.pdf'):
            text = self._pdf_text(link)
            d = date_from_filename(link) or (date_from_text(text[:1500]) if text else None)
            return d, text
        r = self._get(link, allow_missing=True)
        if r is None:
            return None, None
        text = html_to_text(r.text)
        return date_from_text(text[:3000]), text

    # ---- main ------------------------------------------------------------
    def fetch(self, since: date, known_keys: set) -> list:
        known_docs = {k[1] for k in known_keys if k and k[0] == self.name}
        feed = parse_feed(self._get(FEED_URL).text)
        if not feed:
            raise RuntimeError('Brickwork list page had no entries (layout changed or site error)')

        by_doc, order = {}, []
        for e in feed:
            if e['doc'] not in by_doc:
                by_doc[e['doc']] = []
                order.append(e['doc'])
            by_doc[e['doc']].append(e)

        out, skipped = [], []
        for d in order:
            entries = by_doc[d]
            if d in known_docs:
                continue
            link = entries[0]['link']
            when, text = self._doc_info(link)
            time.sleep(PAUSE_SECONDS)
            if when is None:
                skipped.append(f"{entries[0]['company']} ({link})")
                continue
            if when < since:
                continue
            for e in entries:
                outlook = find_outlook(text, e['rating']) if (text and e['term'] == 'LT') else None
                out.append({
                    'Rating Agency': self.name,
                    'Date': when.isoformat(),
                    'Company Name': e['company'],
                    'Industry / Sector': None,
                    'Instrument / Facility': e['instrument'],
                    'Amount': e['amount'],
                    'Rating': e['rating'],
                    'Outlook': outlook,
                    'Rating Action': None,
                    'Link': e['link'],
                })
        if skipped:
            print(f'WARNING: Brickwork: {len(skipped)} document(s) skipped (page missing or no date found); '
                  'they will be tried again next run. First ones: ' + '; '.join(skipped[:5]))
        return out


BrickworkRatingsAdapter = BrickworkAdapter    # in case pipeline/adapters/__init__.py uses this name
