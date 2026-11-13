import html as htmllib
import re
import time
from datetime import date, datetime
from urllib.parse import quote, unquote

import requests

from .base import Adapter

LIST_URL = ('https://www.crisilratings.com/content/crisilratings/en/home/our-business/ratings/'
            'rating-rationale/_jcr_content/wrapper_100_par/ratingresultlisting.results.json')
LIST_REFERER = 'https://www.crisilratings.com/en/home/our-business/ratings/rating-rationale.html'
# The dashboard stores only the file name, i.e. the part AFTER this prefix.
DOC_PREFIX = 'https://www.crisilratings.com/mnt/winshare/Ratings/RatingList/RatingDocs/'
PAGE_LIMIT = 100
MAX_PAGES = 400          # safety stop
PAUSE_SECONDS = 0.5      # be polite to the site

HEADERS = {
    'Accept-Language': 'en-GB,en-US;q=0.9,en;q=0.8',
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36'),
}


def _file_name(link: str) -> str:
    """Last part of a link, URL-decoded and lower-case. Works for a full URL or for the stored fragment."""
    return unquote((link or '').strip()).rstrip('/').split('/')[-1].strip().lower()


def _num(s: str) -> float:
    return float(s.replace(',', ''))


def _cr(x: float) -> str:
    return f'Rs. {x:,.2f} Cr.'


_ACTIONS = (('upgrad', 'Upgraded'), ('downgrad', 'Downgraded'), ('reaffirm', 'Reaffirmed'),
            ('assign', 'Assigned'), ('withdraw', 'Withdrawn'), ('migrat', 'Migrated'), ('revis', 'Revised'))


def _action_word(text: str) -> str:
    """First action word that appears in the text, e.g. 'Rating Revised from ...' -> 'Revised'."""
    t = (text or '').lower()
    hits = sorted((t.find(k), w) for k, w in _ACTIONS if k in t)
    return hits[0][1] if hits else ''


def _html_text(h: str) -> str:
    h = re.sub(r'(?is)<(script|style).*?</\1>', ' ', h)
    h = re.sub(r'(?i)</t[dh]>|<br\s*/?>|</tr>|</p>|</div>', ' ', h)
    h = re.sub(r'<[^>]+>', ' ', h)
    return re.sub(r'\s+', ' ', htmllib.unescape(h)).strip()


# ------------------------------------------------------------- rationale page parsing
_LABEL_RE = re.compile(r'\b(Long|Short)\s+Term\s+Rating\b', re.I)
_REG_TAIL_RE = re.compile(r'\s+(?:RBI|SEBI|MCA|IRDAI|NHB|PFRDA|IFSCA|NABARD|NA)\s*$')
_RATING_RE = re.compile(
    r'^\s*(?:Crisil|CRISIL)\s+(.+?)\s*(?:/\s*(Stable|Positive|Negative|Developing)\b)?\s*(?:\((.*)\)\s*)?$')
_TOTAL_RE = re.compile(r'Rs\.?\s*([\d,]+(?:\.\d+)?)\s*Crore(?:\s*\(\s*(Enhanced|Reduced)\s+from\s*Rs\.?\s*'
                       r'([\d,]+(?:\.\d+)?)\s*Crore\s*\))?', re.I)
_HIST_RE = re.compile(r'\b(LT|ST)\s+([\d,]+(?:\.\d+)?)\s+(?=Crisil|CRISIL|--|NA\b|Withdrawn)')


def parse_page_text(text: str, heading: str = '') -> list:
    """Return [{'term','rating','outlook','action','amount'}] from the rationale's text. [] if not recognised."""
    flat = re.sub(r'[|\s]+', ' ', text or '')
    i = flat.find('Rating Action')
    if i < 0:
        return []
    ends = [p for p in (flat.find('Note:', i), flat.find('Refer to Annexure', i), flat.find('Detailed Rationale', i))
            if p > 0]
    box = flat[i: min(ends) if ends else i + 1500]

    labels = list(_LABEL_RE.finditer(box))
    classes = {}
    for n, m in enumerate(labels):
        term = 'LT' if m.group(1).lower() == 'long' else 'ST'
        if term in classes:
            continue
        seg = box[m.end(): labels[n + 1].start() if n + 1 < len(labels) else len(box)].strip()
        seg = _REG_TAIL_RE.sub('', seg)
        seg = re.sub(r'\(\s*(CE|SO)\s*\)', r'[\1]', seg)         # keep (CE)/(SO) out of the action bracket
        mm = _RATING_RE.match(seg)
        if not mm:
            continue
        symbol = re.sub(r'\[(CE|SO)\]', r'(\1)', mm.group(1)).strip()
        action = _action_word(mm.group(3)) or _action_word(heading)
        classes[term] = {'symbol': symbol, 'outlook': mm.group(2) or 'Nil', 'action': action}
    if not classes:
        return []

    amounts = {}
    h = flat.find('Rating History')
    if h >= 0:
        k = flat.find('All amounts are in', h)
        for m in _HIST_RE.finditer(flat[h: k if k > 0 else h + 4000]):
            amounts[m.group(1)] = amounts.get(m.group(1), 0.0) + _num(m.group(2))
    total = _TOTAL_RE.search(box)

    out = []
    for term in ('LT', 'ST'):
        c = classes.get(term)
        if not c:
            continue
        amt = amounts.get(term)
        if amt is None and total and len(classes) == 1:
            amt = _num(total.group(1))
        amount = _cr(amt) if amt is not None else None
        if amount and total and total.group(2) and len(classes) == 1:
            amount += f' ({total.group(2).capitalize()} from {_cr(_num(total.group(3)))})'
        rating = 'CRISIL ' + c['symbol'] + (f" ({c['action']})" if c['action'] else '')
        out.append({'term': term, 'rating': rating, 'outlook': c['outlook'] if term == 'LT' else 'Nil',
                    'action': c['action'] or None, 'amount': amount})
    return out


class CrisilAdapter(Adapter):
    name = 'CRISIL'
    enabled = True

    def key(self, row: dict) -> tuple:
        # One rationale (file) can give a Long Term and a Short Term row.
        return (self.name, _file_name(row.get('Link')), row.get('Instrument / Facility') or '')

    # ---- network helpers -------------------------------------------------
    def _get_page(self, start: int) -> dict:
        last = None
        for attempt in range(3):
            try:
                r = self.s.get(LIST_URL, params={'cmd': 'RR', 'start': start, 'limit': PAGE_LIMIT,
                                                 'filters': '{}', '_': int(time.time() * 1000)},
                               headers={'Accept': 'application/json, text/javascript, */*; q=0.01',
                                        'Referer': LIST_REFERER, 'X-Requested-With': 'XMLHttpRequest'},
                               timeout=60)
                r.raise_for_status()
                data = r.json()
                if not isinstance(data, dict) or not isinstance(data.get('docs'), list):
                    raise ValueError(f'Unexpected response shape at start={start}')
                return data
            except Exception as e:
                last = e
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f'CRISIL list (start={start}) failed: {last}')

    def _page_text(self, filename: str):
        """Rationale page text; None if gone (404/410). Raises on repeated network failure."""
        last = None
        for attempt in range(3):
            try:
                r = self.s.get(DOC_PREFIX + quote(filename, safe=''), timeout=90)
                if r.status_code in (404, 410):
                    return None
                r.raise_for_status()
                return _html_text(r.text)
            except Exception as e:
                last = e
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f'CRISIL page download failed for {filename}: {last}')

    # ---- row building ----------------------------------------------------
    def _base(self, doc: dict):
        fn = (doc.get('ratingFileName') or '').strip()
        raw_date = (doc.get('ratingDate') or doc.get('transDate') or '').strip()
        company = (doc.get('companyName') or '').strip()
        if not fn or not raw_date or not company:
            return None
        iso = datetime.strptime(raw_date, '%b %d, %Y').date().isoformat()
        heading = re.sub(r'\s+', ' ', doc.get('heading') or '').strip()
        return {
            'Rating Agency': self.name, 'Date': iso, 'Company Name': company,
            'Industry / Sector': 'Other', 'PDF File Name': fn, 'Link': fn,
            'Additional Details': None,
        }, heading

    def _rows_for(self, base: dict, heading: str) -> list:
        fn = base['Link']
        quick = fn.lower().endswith('.pdf') or heading.lower().startswith(('rating update', 'update on'))
        if quick:                      # updates / press notes: no rating-action table to read
            return [dict(base)]
        text = self._page_text(fn)
        parsed = parse_page_text(text, heading) if text else []
        if not parsed:
            print(f"WARNING: could not read ratings from {fn} ({base['Company Name']}); added without details")
            return [dict(base)]
        rows = []
        for p in parsed:
            r = dict(base)
            r['Instrument / Facility'] = 'Long Term Rating' if p['term'] == 'LT' else 'Short Term Rating'
            r['Amount'] = p['amount']
            r['Rating'] = p['rating']
            r['Outlook'] = p['outlook']
            r['Rating Action'] = p['action']
            rows.append(r)
        return rows

    # ---- main ------------------------------------------------------------
    def fetch(self, since: date, known_keys: set) -> list:
        since_iso = since.isoformat()
        known_files = {k[1] for k in known_keys if k and k[0] == self.name}
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        out, seen, start = [], set(), 0
        for _ in range(MAX_PAGES):
            payload = self._get_page(start)
            docs = payload['docs']
            if not docs:
                if start == 0:
                    raise RuntimeError('CRISIL list returned no rows (site change or blocked?)')
                break
            bases = [b for b in (self._base(d) for d in docs) if b]
            for base, heading in bases:
                f = _file_name(base['Link'])
                if f in known_files or f in seen or base['Date'] < since_iso:
                    continue
                seen.add(f)
                out.extend(self._rows_for(base, heading))
                time.sleep(PAUSE_SECONDS)
            all_known = bool(bases) and all(_file_name(b['Link']) in known_files for b, _ in bases)
            all_older = bool(bases) and all(b['Date'] < since_iso for b, _ in bases)
            start += len(docs)
            total = payload.get('numFound')
            if all_known or all_older or (isinstance(total, int) and start >= total):
                break
            time.sleep(PAUSE_SECONDS)
        return out


# In case the existing __init__.py imports the stub under a different class name
CRISILAdapter = CrisilAdapter
