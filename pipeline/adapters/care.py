import io
import re
import time
from datetime import date, datetime, timedelta
from urllib.parse import quote

import requests

from .base import Adapter

LIST_URL = 'https://www.careratings.com/rrcompany'
PAGE_URL = 'https://www.careratings.com/find-ratings'
PDF_BASE = 'https://www.careratings.com/upload/CompanyFiles/PR/'
YEAR_ID = '2022'          # copied from the browser request; see notes
PAUSE_SECONDS = 0.5

HEADERS = {
    'Accept': 'application/json, text/javascript, */*; q=0.01',
    'Accept-Language': 'en-GB,en-US;q=0.9,en;q=0.8',
    'X-Requested-With': 'XMLHttpRequest',
    'Referer': PAGE_URL,
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36'),
}


# ------------------------------------------------------------ helpers
def _file_name(link: str) -> str:
    """Last part of a link (decoded, lower-case). Works for a full URL or a bare file name."""
    from urllib.parse import unquote
    return unquote((link or '').strip().rstrip('/').split('/')[-1]).lower()


def _cr(x: float) -> str:
    return f'Rs. {x:,.2f} Cr.'


_WATCH = {'RWD': 'Rating Watch with Developing Implications',
          'RWP': 'Rating Watch with Positive Implications',
          'RWN': 'Rating Watch with Negative Implications'}


def split_rating(text: str):
    """'CARE A; Stable / CARE A1' -> ('CARE A / CARE A1', 'Stable'); '(RWD)' becomes the outlook."""
    s = re.sub(r'\s+', ' ', text or '').strip()
    outlook = None
    m = re.search(r'\((RWD|RWP|RWN)\)', s)
    if m:
        outlook = _WATCH[m.group(1)]
        s = s.replace(m.group(0), ' ')
    m = re.search(r';\s*(Stable|Positive|Negative|Developing)\b', s, re.I)
    if m:
        outlook = outlook or m.group(1).capitalize()
        s = re.sub(r';\s*(Stable|Positive|Negative|Developing)\b', '', s, flags=re.I)
    s = re.sub(r'\s+', ' ', s).strip(' ;/')
    return s, outlook


_ROW_RE = re.compile(
    r'(?P<inst>.+?)\s+'
    r'(?:(?P<reg>RBI|SEBI|MCA|IRDAI|PFRDA|NHB|IFSCA|NABARD)\s+)?'
    r'(?P<amt>\d[\d,]*\.\d+)\s+'
    r'(?P<mid>.*?)\s*'
    r'(?P<act>Provisionally Assigned|Reaffirmed and Withdrawn|Assigned|Reaffirmed|Revised|Upgraded|'
    r'Downgraded|Withdrawn|Migrated|Moved|Continued|Reinstated|Suspended)'
    r'(?:(?<=Withdrawn)\s+Withdrawn\b)?')


def parse_pr_text(text: str) -> list:
    """Rows of the 'Facilities/Instruments ... Rating Action' box on page 1 of a CARE press release.
    Returns [{'instrument','amount','rating','outlook','action'}]; empty list if the layout is not recognised."""
    flat = re.sub(r'\s+', ' ', text or '')
    end = flat.find('Details of instruments')
    if end < 0:
        return []
    start = flat.rfind('Action', 0, end)
    if start < 0:
        return []
    box = flat[start + len('Action'):end]
    box = re.sub(r'^\s*(?:(?:Cash|Over|Collateral)\s+)+', '', box)       # securitisation header leftovers
    out = []
    for m in _ROW_RE.finditer(box):
        inst = re.sub(r'\s+', ' ', m.group('inst')).strip()
        rm = re.search(r'(?:Provisional\s+)?CARE\b.*', m.group('mid') or '')
        rating, outlook = split_rating(rm.group(0)) if rm else ('', None)
        action = m.group('act')
        if not rating:
            rating = 'Withdrawn' if 'Withdrawn' in action else None
        out.append({'instrument': inst, 'amount': _cr(float(m.group('amt').replace(',', ''))),
                    'rating': rating, 'outlook': outlook or 'Nil', 'action': action})
    return out


class CareAdapter(Adapter):
    name = 'CARE Ratings'
    enabled = True

    def key(self, row: dict) -> tuple:
        # One PDF can give several rows (one per facility line).
        return (self.name, _file_name(row.get('Link')), row.get('Instrument / Facility') or '',
                row.get('Amount') or '', row.get('Rating') or '')

    # ---- network helpers -------------------------------------------------
    def _list(self, frm: date, to: date) -> list:
        params = {'companyName': '', 'YearID': YEAR_ID,
                  'fdate': frm.strftime('%m/%d/%Y'), 'tdate': to.strftime('%m/%d/%Y')}
        last = None
        for attempt in range(3):
            try:
                r = self.s.get(LIST_URL, params=params, timeout=90)
                r.raise_for_status()
                data = r.json()
                if not isinstance(data, dict) or not isinstance(data.get('data'), list):
                    raise ValueError('unexpected response shape')
                return data['data']
            except Exception as e:
                last = e
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f'CARE list request failed: {last}')

    def _pdf_text(self, file_url: str):
        """Download the PDF and return its text; None if the file is gone (404/410)."""
        from pypdf import PdfReader
        url = PDF_BASE + quote(file_url, safe="/()&+-._,~!'")
        last = None
        for attempt in range(3):
            try:
                r = self.s.get(url, timeout=90)
                if r.status_code in (404, 410):
                    return None
                r.raise_for_status()
                reader = PdfReader(io.BytesIO(r.content))
                return '\n'.join((p.extract_text() or '') for p in reader.pages[:2])   # summary is on page 1
            except Exception as e:
                last = e
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f'PDF download/read failed for {file_url}: {last}')

    # ---- row building ----------------------------------------------------
    def _rows_for(self, d: dict) -> list:
        file_url = (d.get('FileURL') or '').strip()
        company = re.sub(r'\s+', ' ', d.get('CompanyName') or '').strip()
        iso = (d.get('PublishedDate') or '')[:10]
        base = {'Rating Agency': self.name, 'Date': iso, 'Company Name': company,
                'Industry / Sector': 'Other', 'PDF File Name': file_url,
                'Link': PDF_BASE + file_url, 'Additional Details': None}
        try:
            text = self._pdf_text(file_url)
            parsed = parse_pr_text(text) if text else []
        except Exception as e:
            print(f'WARNING: {file_url}: {e}')
            parsed = []
        if not parsed:
            print(f'WARNING: could not read ratings from {file_url} ({company}); added without details')
            return [dict(base)]
        rows = []
        for p in parsed:
            r = dict(base)
            r['Instrument / Facility'] = p['instrument']
            r['Amount'] = p['amount']
            r['Rating'] = p['rating']
            r['Outlook'] = p['outlook']
            r['Rating Action'] = p['action']
            rows.append(r)
        return rows

    # ---- main ------------------------------------------------------------
    def fetch(self, since: date, known_keys: set) -> list:
        known_files = {k[1] for k in known_keys if k and k[0] == self.name}
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        try:
            self.s.get(PAGE_URL, timeout=60)      # picks up the site's session cookies; failure is harmless
        except Exception:
            pass
        items = self._list(since, date.today())
        if not items:
            print(f'WARNING: CARE list returned no records for {since} onwards')
            return []
        out, seen = [], set()
        for d in items:
            file_url = (d.get('FileURL') or '').strip()
            iso = (d.get('PublishedDate') or '')[:10]
            if not file_url or not iso or not (d.get('CompanyName') or '').strip():
                continue
            datetime.strptime(iso, '%Y-%m-%d')               # raises if the format ever changes
            f = _file_name(file_url)
            if f in known_files or f in seen or iso < since.isoformat():
                continue
            seen.add(f)
            out.extend(self._rows_for(d))
            time.sleep(PAUSE_SECONDS)
        return out


# In case the existing __init__.py imports the stub under a different class name
CareRatingsAdapter = CareAdapter
CareRatings = CareAdapter
