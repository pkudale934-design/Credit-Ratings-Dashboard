import html as htmllib
import io
import re
import time
from datetime import date, datetime

import requests

from .base import Adapter

BASE = 'https://www.icra.in'
LIST_PAGE = BASE + '/Rating/AllRatingRationales'
LIST_API = BASE + '/Rating/GetAllRatingRational'
# The stored Link is just the report Id (dashboard adds the 'ShowRationaleReport?Id=' prefix).
PDF_URLS = [BASE + '/Rating/ShowRationalReportFilePdfViewer/{id}',
            BASE + '/Rating/GetRationalReportFilePdf?Id={id}']
MAX_PAGES = 400          # safety stop
PAUSE_SECONDS = 0.5      # be polite to the site

HEADERS = {
    'Accept-Language': 'en-GB,en-US;q=0.9,en;q=0.8',
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36'),
}


# ---------------------------------------------------------------- list page parsing
_ROW_RE = re.compile(r'<tr[^>]*>(.*?)</tr>', re.S | re.I)
_LINK_RE = re.compile(r'ShowRationaleReport\?Id=(\d+)["\'][^>]*>(.*?)</a>', re.S | re.I)
_DATE_RE = re.compile(r'(\d{1,2})\s+([A-Za-z]{3})[A-Za-z]*\s+(\d{4})')
_CAT_RE = re.compile(r'ratingCategoryName[^>]*>(.*?)</span>', re.S | re.I)
_TOKEN_RES = [
    re.compile(r'<input[^>]*name=["\']__RequestVerificationToken["\'][^>]*value=["\']([^"\']+)["\']', re.I),
    re.compile(r'<input[^>]*value=["\']([^"\']+)["\'][^>]*name=["\']__RequestVerificationToken["\']', re.I),
]


def _clean(s: str) -> str:
    return re.sub(r'\s+', ' ', htmllib.unescape(re.sub(r'<[^>]+>', ' ', s or ''))).strip()


def parse_list(html: str) -> list:
    """Rows of the ICRA list table -> [{'id','date','category','title','company'}]."""
    out = []
    for m in _ROW_RE.finditer(html):
        row = m.group(1)
        link = _LINK_RE.search(row)
        if not link:
            continue
        d = _DATE_RE.search(row)
        if not d:
            raise ValueError(f'No date found for ICRA report {link.group(1)}')
        iso = datetime.strptime(f'{d.group(1)} {d.group(2)} {d.group(3)}', '%d %b %Y').date().isoformat()
        title = _clean(link.group(2))
        cat = _CAT_RE.search(row)
        company = title.split(': ', 1)[0].strip() if ': ' in title else title
        out.append({'id': link.group(1), 'date': iso, 'title': title,
                    'category': _clean(cat.group(1)) if cat else '', 'company': company})
    return out


# ---------------------------------------------------------------- PDF parsing
_REG_RE = re.compile(r'(?:(?<=\s)|^)(?:RBI|SEBI|IRDAI|NHB|PFRDA|IFSCA|NABARD|NA)(?=\s|$)')
# [previous amount] current amount, followed by a rating that starts with [ICRA] or Provisional [ICRA]
_AMT_RE = re.compile(r'(?:(?<![\d.,])([\d,]+\.\d+)\s+)?(?<![\d.,])([\d,]+(?:\.\d+)?)\s+'
                     r'(?=(?:Provisional\s*)?\[ICRA\])')
_ACTIONS = (('upgrad', 'Upgraded'), ('downgrad', 'Downgraded'), ('reaffirm', 'Reaffirmed'),
            ('assign', 'Assigned'), ('withdraw', 'Withdrawn'), ('revis', 'Revised'))


def _num(s: str) -> float:
    return float(s.replace(',', ''))


def _cr(x: float) -> str:
    return f'Rs. {x:,.2f} Cr.'


def _actions(text: str) -> str:
    t = (text or '').lower()
    found = sorted((t.find(k), w) for k, w in _ACTIONS if k in t)
    return ', '.join(w for _, w in found)


def _split_rating(t: str):
    """'[ICRA]A (Stable)/[ICRA]A2+; assigned' -> ('A/A2+', 'Stable', 'Assigned')"""
    part, _, act = t.partition(';')
    part = re.sub(r'Provisional\s*\[ICRA\]', 'Provisional [ICRA]', part)
    outlook = None
    m = re.search(r'\((Stable|Positive|Negative|Developing)\)', part, re.I)
    if m:
        outlook = m.group(1).capitalize()
        part = part.replace(m.group(0), ' ')
    else:
        w = re.search(r'Rating Watch.*$', part, re.I)
        if w:
            outlook = w.group(0).strip()
            part = part[:w.start()]
    rating = re.sub(r'\s+', ' ', part.replace('[ICRA]', '')).strip()
    rating = re.sub(r'\s*/\s*', '/', rating)
    return rating, outlook, _actions(act)


def parse_summary(text: str) -> list:
    """Read the 'Summary of rating action' table. One dict per table row; [] if layout not recognised."""
    flat = re.sub(r'\s+', ' ', text or '')
    s = re.search(r'Summary of rating action', flat, re.I)
    if not s:
        return []
    body = flat[s.end(): s.end() + 4000]
    e = re.search(r'\*\s*Instrument details|#\s*SEBI', body)
    if e:
        body = body[:e.start()]
    h = re.search(r'Financial\s+sector\s+regulator\s*#?', body, re.I)
    if h:
        body = body[h.end():]
    rows, pos = [], 0
    while True:
        m = _AMT_RE.search(body, pos)
        if not m:
            break
        instrument = body[pos:m.start()].strip(' -–')
        rest = body[m.end():]
        r = _REG_RE.search(rest)
        rating_text = (rest[:r.start()] if r else rest).strip()
        rating_text = re.sub(r'\s+Total\s+[\d,.\s]+$', '', rating_text).strip()
        pos = m.end() + (r.end() if r else len(rest))
        rating, outlook, action = _split_rating(rating_text)
        if not rating:
            continue
        rows.append({'instrument': instrument, 'amount': _num(m.group(2)),
                     'previous': _num(m.group(1)) if m.group(1) else None,
                     'rating': rating, 'outlook': outlook, 'action': action})
    return rows


def _id_of(link: str) -> str:
    m = re.search(r'(\d+)\s*$', (link or '').strip())
    return m.group(1) if m else (link or '').strip()


class ICRAAdapter(Adapter):
    name = 'ICRA'
    enabled = True

    def key(self, row: dict) -> tuple:
        # One report (Id) can give several rows, one per facility/instrument.
        return (self.name, _id_of(row.get('Link')), row.get('Instrument / Facility') or '',
                row.get('Amount') or '', row.get('Rating') or '')

    # ---- network helpers -------------------------------------------------
    def _open_session(self):
        last = None
        for attempt in range(3):
            try:
                s = requests.Session()
                s.headers.update(HEADERS)
                r = s.get(LIST_PAGE, timeout=60)
                r.raise_for_status()
                for rx in _TOKEN_RES:
                    m = rx.search(r.text)
                    if m:
                        return s, m.group(1)
                raise ValueError('anti-forgery token not found on ICRA list page')
            except Exception as e:
                last = e
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f'ICRA could not open list page: {last}')

    def _get_page(self, page: int) -> list:
        last = None
        for attempt in range(3):
            try:
                r = self.s.post(
                    LIST_API, params={'page': page, 'type': 'IsESR'},
                    data={'__RequestVerificationToken': self.token, 'CompanyName': '',
                          'FromDate': '', 'ToDate': '', 'RatingCategoryName': ''},
                    headers={'Accept': '*/*', 'Origin': BASE, 'Referer': LIST_PAGE,
                             'X-Requested-With': 'XMLHttpRequest',
                             'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'},
                    timeout=60)
                r.raise_for_status()
                return parse_list(r.text)
            except Exception as e:
                last = e
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f'ICRA list page {page} failed: {last}')

    def _pdf_text(self, rid: str):
        """PDF text; None if the report has no readable PDF (404 / not a PDF). Raises on network failure."""
        from pypdf import PdfReader
        last = None
        for tmpl in PDF_URLS:
            url = tmpl.format(id=rid)
            for attempt in range(3):
                try:
                    r = self.s.get(url, timeout=90)
                    if r.status_code in (404, 410):
                        break
                    r.raise_for_status()
                    if not r.content.startswith(b'%PDF'):
                        break
                    reader = PdfReader(io.BytesIO(r.content))
                    return '\n'.join((p.extract_text() or '') for p in reader.pages)
                except Exception as e:
                    last = e
                    time.sleep(2 * (attempt + 1))
            else:
                continue
        if last is not None:
            raise RuntimeError(f'ICRA PDF download/read failed for report {rid}: {last}')
        return None

    # ---- row building ----------------------------------------------------
    def _rows_for(self, item: dict) -> list:
        base = {
            'Rating Agency': self.name,
            'Date': item['date'],
            'Company Name': item['company'],
            'Industry / Sector': 'Other',
            'PDF File Name': item['id'],
            'Link': item['id'],
            'Additional Details': item['category'] or None,
        }
        text = self._pdf_text(item['id'])
        parsed = parse_summary(text) if text else []
        if not parsed:
            print(f"WARNING: could not read ratings from ICRA report {item['id']} ({item['company']}); added without details")
            r = dict(base)
            r['Instrument / Facility'] = item['category'] or None
            return [r]
        rows = []
        for p in parsed:
            r = dict(base)
            r['Instrument / Facility'] = p['instrument'] or item['category'] or None
            amount = _cr(p['amount'])
            if p['previous'] and abs(p['previous'] - p['amount']) > 0.004:
                amount += f" ({'Enhanced' if p['amount'] > p['previous'] else 'Reduced'} from {_cr(p['previous'])})"
            r['Amount'] = amount
            r['Rating'] = p['rating']
            r['Outlook'] = p['outlook'] or 'Nil'
            r['Rating Action'] = p['action'] or None
            rows.append(r)
        return rows

    # ---- main ------------------------------------------------------------
    def fetch(self, since: date, known_keys: set) -> list:
        since_iso = since.isoformat()
        known_ids = {k[1] for k in known_keys if k and k[0] == self.name}
        self.s, self.token = self._open_session()
        out, seen, prev_page_ids = [], set(), None
        for page in range(1, MAX_PAGES + 1):
            items = self._get_page(page)
            if not items:
                if page == 1:
                    raise RuntimeError('ICRA list page 1 returned no rows (site change or blocked?)')
                break
            ids = [i['id'] for i in items]
            if ids == prev_page_ids:
                break                       # site repeated the same page: end of list
            prev_page_ids = ids
            for it in items:
                if it['id'] in known_ids or it['id'] in seen or it['date'] < since_iso:
                    continue
                seen.add(it['id'])
                out.extend(self._rows_for(it))
                time.sleep(PAUSE_SECONDS)
            if all(i in known_ids for i in ids) or all(i['date'] < since_iso for i in items):
                break
            time.sleep(PAUSE_SECONDS)
        return out


# In case the existing __init__.py imports the stub under a different class name
IcraAdapter = ICRAAdapter
ICRA = ICRAAdapter
