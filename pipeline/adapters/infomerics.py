import io
import re
import time
from datetime import date
 
import requests
 
from .base import Adapter
 
API = 'https://cms.infomerics.com/api/companies/recent-ratings'
PAGE_SIZE = 12
MAX_PAGES = 300          # safety stop
PAUSE_SECONDS = 0.5      # be polite to the site
 
# The dashboard stores only the file name, i.e. the part AFTER this prefix.
BLOB_PREFIX = 'https://infomericstorage.blob.core.windows.net/uploads/'
 
HEADERS = {
    'Accept': '*/*',
    'Accept-Language': 'en-GB,en-US;q=0.9,en;q=0.8',
    'Origin': 'https://www.infomerics.com',
    'Referer': 'https://www.infomerics.com/',
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36'),
}
 
 
def _file_name(link: str) -> str:
    """Last part of a link, lower-case. Works for a full URL or for the stored fragment."""
    return (link or '').strip().rstrip('/').split('/')[-1].lower()
 
 
def _num(s: str) -> float:
    return float(s.replace(',', ''))
 
 
def _cr(x: float) -> str:
    return f'Rs. {x:,.2f} Cr.'
 
 
def _action_word(text: str) -> str:
    t = (text or '').lower()
    for needle, word in (('upgrad', 'Upgraded'), ('downgrad', 'Downgraded'), ('reaffirm', 'Reaffirmed'),
                         ('assign', 'Assigned'), ('withdraw', 'Withdrawn'), ('revis', 'Revised')):
        if needle in t:
            return word
    return ''
 
 
# "Long Term Rating IVR A-/Stable (Rating Reaffirmed)"  |  "Short Term Rating IVR A2+ (Rating Reaffirmed)"
_RATING_RE = re.compile(
    r'(Long|Short)\s+Term\s+Rating\s+(IVR\s*[A-Za-z0-9+\-]+)'
    r'(?:\s*/\s*([^()]*?))?'
    r'(?:\s*\(([^)]*)\))?\s*(?:RBI|SEBI|MCA|\Z|(?=Long\s+Term|Short\s+Term|\^|Refer))')
_TOTAL_RE = re.compile(
    r'Rated\s*Rs\.?\s*([\d,]+\.?\d*)\s*Crore\s*\(\s*(Enhanced|Reduced)\s+from\s*Rs\.?\s*([\d,]+\.?\d*)\s*Crore\s*\)')
_TOTAL_AMT_RE = re.compile(r'Rated\s*Rs\.?\s*([\d,]+\.?\d*)\s*Crore')
_HIST_RE = re.compile(r'\b(LT|ST|Long Term|Short Term)\s+([\d,]+\.\d+)')
 
 
def parse_pr_text(text: str):
    """Return a list of {'term','rating','outlook','action','amount'} from the press-release text.
    Empty list if the layout is not recognised."""
    flat = re.sub(r'\s+', ' ', text)
    i = flat.find('Rating Action')
    if i < 0:
        return []
    j = flat.find('Refer Annexures', i)
    box = flat[i: j if j > 0 else i + 1500]
 
    classes = {}
    for m in _RATING_RE.finditer(box):
        term = 'LT' if m.group(1) == 'Long' else 'ST'
        if term in classes:
            continue
        outlook = (m.group(3) or '').strip().strip(';,') or 'Nil'
        action = _action_word(m.group(4))
        rating = re.sub(r'\s+', ' ', m.group(2)).strip()
        classes[term] = {'term': term, 'rating': rating, 'outlook': outlook, 'action': action}
    if not classes:
        return []
 
    # amounts per class from the "Rating History" table
    amounts = {}
    h = flat.find('Rating History for last three years')
    if h >= 0:
        k = flat.find('Annexure 1', h)
        for m in _HIST_RE.finditer(flat[h: k if k > 0 else h + 4000]):
            t = 'LT' if m.group(1) in ('LT', 'Long Term') else 'ST'
            amounts[t] = amounts.get(t, 0.0) + _num(m.group(2))
 
    total = _TOTAL_RE.search(box)
    total_amt = _TOTAL_AMT_RE.search(box)
    out = []
    for term in ('LT', 'ST'):
        c = classes.get(term)
        if not c:
            continue
        amt = amounts.get(term)
        if amt is None and total_amt and len(classes) == 1:
            amt = _num(total_amt.group(1))
        amount = _cr(amt) if amt is not None else None
        if amount and total and len(classes) == 1:
            amount += f' ({total.group(2)} from {_cr(_num(total.group(3)))})'
        rating = c['rating'] + (f" ({c['action']})" if c['action'] else '')
        out.append({'term': term, 'rating': rating, 'outlook': c['outlook'],
                    'action': c['action'] or None, 'amount': amount})
    return out
 
 
class InfomericsAdapter(Adapter):
    name = 'Infomerics'
    enabled = True
 
    def key(self, row: dict) -> tuple:
        # One press release (PDF) can give a Long Term and a Short Term row.
        return (self.name, _file_name(row.get('Link')), row.get('Instrument / Facility') or '')
 
    # ---- network helpers -------------------------------------------------
    def _get_page(self, page: int) -> dict:
        last_error = None
        for attempt in range(3):
            try:
                r = requests.get(API, params={'page': page, 'pageSize': PAGE_SIZE},
                                 headers=HEADERS, timeout=45)
                r.raise_for_status()
                data = r.json()
                if not isinstance(data, dict) or not isinstance(data.get('data'), list):
                    raise ValueError(f'Unexpected response shape on page {page}')
                return data
            except Exception as e:
                last_error = e
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f'Infomerics page {page} failed: {last_error}')
 
    def _pdf_text(self, url: str):
        """Download the PDF and return its text; None if the file is gone (404/410)."""
        from pypdf import PdfReader
        last_error = None
        for attempt in range(3):
            try:
                r = requests.get(url, headers=HEADERS, timeout=90)
                if r.status_code in (404, 410):
                    return None
                r.raise_for_status()
                reader = PdfReader(io.BytesIO(r.content))
                return '\n'.join((p.extract_text() or '') for p in reader.pages)
            except Exception as e:
                last_error = e
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f'PDF download/read failed for {url}: {last_error}')
 
    # ---- row building ----------------------------------------------------
    def _base(self, item: dict):
        pr = item.get('PressRelease') or {}
        file_ = ((pr.get('Document') or {}).get('DocumentFile')) or {}
        url = file_.get('url') or ''
        iso = (pr.get('Date') or item.get('Date') or '')[:10]
        company = (item.get('CompanyName') or '').strip()
        if not url or not iso or not company:
            return None
        fragment = url[len(BLOB_PREFIX):] if url.startswith(BLOB_PREFIX) else url
        return url, {
            'Rating Agency': self.name,
            'Date': iso,
            'Company Name': company,
            'Industry / Sector': 'Other',
            'PDF File Name': fragment,
            'Link': fragment,
            'Additional Details': item.get('slug') or None,
        }
 
    def _rows_for(self, url: str, base: dict) -> list:
        text = self._pdf_text(url)
        parsed = parse_pr_text(text) if text else []
        if not parsed:
            print(f"WARNING: could not read ratings from {base['Link']} ({base['Company Name']}); added without details")
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
        out, seen_files = [], set()
        for page in range(1, MAX_PAGES + 1):
            payload = self._get_page(page)
            items = payload['data']
            if not items:
                break
 
            bases = [b for b in (self._base(i) for i in items) if b]
            for url, base in bases:
                f = _file_name(base['Link'])
                if f in known_files or f in seen_files or base['Date'] < since_iso:
                    continue
                seen_files.add(f)
                out.extend(self._rows_for(url, base))
                time.sleep(PAUSE_SECONDS)
 
            all_known = bool(bases) and all(_file_name(b['Link']) in known_files for _, b in bases)
            all_older = bool(bases) and all(b['Date'] < since_iso for _, b in bases)
            more = (payload.get('meta') or {}).get('hasmoredata', True)
            if all_known or all_older or not more:
                break
            time.sleep(PAUSE_SECONDS)
        return out
