import html as htmllib
import re
import time
from datetime import date, datetime
from urllib.parse import quote

import requests

from .base import Adapter

BASE = 'https://www.indiaratings.co.in'
LIST_URL = BASE + '/pressReleases/GetListing_RAC'
DETAIL_URL = BASE + '/pressReleases/GetPressreleaseData'

# The first one ('corporates') is essential: if it fails the whole run fails.
# The others are best effort: if one fails you get a WARNING line in the log and the run continues.
SECTORS = ['corporates', 'financial institutions', 'infrastructure', 'public finance', 'structured finance']

# How new rows store the link. 'key' = only the short key (e.g. cpf53zcolzk8aw3gwe9vu1b6),
# 'url' = the full https://www.indiaratings.co.in/pressrelease/<key> address.
LINK_STYLE = 'key'
# Also copy the key into the 'Source ID' column (harmless if the dashboard does not use it).
ALSO_SOURCE_ID = True

MAX_PAGES = 200          # safety stop per sector
PAUSE_SECONDS = 0.5      # be polite to the site

HEADERS = {
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'en-GB,en-US;q=0.9,en;q=0.8',
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36'),
}


# ---------------------------------------------------------------- helpers
def _clean(s) -> str:
    return re.sub(r'\s+', ' ', htmllib.unescape(re.sub(r'<[^>]+>', ' ', str(s or '')))).strip()


_VERBS = (('affirm', 'Affirms'), ('upgrad', 'Upgrades'), ('downgrad', 'Downgrades'),
          ('assign', 'Assigns'), ('withdr', 'Withdraws'), ('revis', 'Revises'))


def _verb(text: str) -> str:
    """First action word in the text, in the wording the dashboard already uses (Affirms, Upgrades, ...)."""
    t = (text or '').lower()
    hits = sorted((t.find(k), w) for k, w in _VERBS if k in t)
    return hits[0][1] if hits else ''


def _title_verb(title: str) -> str:
    m = re.match(r'\s*India Ratings\s+(\w+)', title or '')
    if not m:
        return _verb(title)
    return _verb(m.group(1)) or m.group(1)


_OUTLOOK_RE = re.compile(r'^(stable|positive|negative|developing|evolving)$|^(rating\s*)?watch', re.I)


def split_rating(text: str):
    """'IND BBB+/Stable/IND A2' -> ('IND BBB+/IND A2', 'Stable')"""
    ratings, outlook = [], None
    for tok in (t.strip() for t in (text or '').split('/')):
        if not tok:
            continue
        if _OUTLOOK_RE.match(tok):
            outlook = tok[:1].upper() + tok[1:]
        else:
            ratings.append(tok)
    return '/'.join(ratings), outlook


def _cr(x: float) -> str:
    return f'Rs. {x:,.2f} Cr.'


def _amount(cell: str, head: str):
    m = re.search(r'[\d,]+(?:\.\d+)?', cell or '')
    if not m:
        return None
    v = float(m.group(0).replace(',', ''))
    h = (head or '').lower()
    if 'billion' in h:
        v *= 100
    elif 'crore' in h:
        pass
    elif 'lakh' in h:
        v /= 100
    else:                       # INR million (the usual header)
        v /= 10
    return _cr(v)


_TOKEN_RE = re.compile(r'^[a-z0-9]{24}$')


def _key_of(row: dict) -> str:
    """The press-release key, wherever the dashboard stored it (Link, Source ID, ...)."""
    found = []
    for col in ('Source ID', 'Link', 'PDF File Name', 'Additional Details'):
        v = str(row.get(col) or '').strip()
        if not v:
            continue
        m = re.search(r'pressrelease/([A-Za-z0-9]+)', v)
        tok = (m.group(1) if m else v.rstrip('/').split('/')[-1]).lower()
        found.append(tok)
    for tok in found:
        if _TOKEN_RE.match(tok):
            return tok
    return found[0] if found else ''


# ---------------------------------------------------------------- detail parsing
def parse_current_ratings(h: str) -> list:
    """Rows of the 'currentRatings' table -> [{'instrument','amount','rating','outlook','action'}]."""
    if not h:
        return []
    heads_raw = [_clean(x) for x in re.findall(r'<th[^>]*>(.*?)</th>', h, re.S | re.I)]
    heads = [x.lower() for x in heads_raw]

    def find(pred, default):
        for i, x in enumerate(heads):
            if pred(x):
                return i
        return default if len(heads) >= 8 or not heads else None

    i_inst = find(lambda x: 'instrument' in x, 0)
    i_size = find(lambda x: 'size' in x or 'amount' in x, 5)
    i_rate = find(lambda x: 'rating' in x and 'action' not in x, 6)
    i_act = find(lambda x: 'action' in x, 7)
    if i_inst is None or i_rate is None:
        return []
    body = re.search(r'<tbody[^>]*>(.*?)</tbody>', h, re.S | re.I)
    out = []
    for tr in re.findall(r'<tr[^>]*>(.*?)</tr>', body.group(1) if body else h, re.S | re.I):
        cells = [_clean(c) for c in re.findall(r'<td[^>]*>(.*?)</td>', tr, re.S | re.I)]
        if len(cells) <= max(i for i in (i_inst, i_rate) if i is not None):
            continue
        rating, outlook = split_rating(cells[i_rate])
        if not rating and not outlook:
            continue
        get = lambda i: cells[i] if i is not None and i < len(cells) else ''
        out.append({'instrument': cells[i_inst],
                    'amount': _amount(get(i_size), heads_raw[i_size] if i_size is not None and i_size < len(heads_raw) else ''),
                    'rating': rating, 'outlook': outlook or 'Nil',
                    'action': _verb(get(i_act))})
    return out


_TITLE_RATING_RE = re.compile(r"[‘'’]\s*(IND\s*[A-Za-z0-9+\-]+(?:\s*\((?:CE|SO)\))?)\s*[’'‘]")
_TITLE_OUTLOOK_RE = re.compile(r'(?:/|Outlook\s+|\bto\s+)\s*(Stable|Positive|Negative|Developing|Evolving)\b', re.I)


def parse_title(title: str):
    """Fallback when the detail table is unavailable: (rating, outlook) read from the headline."""
    ratings = []
    for m in _TITLE_RATING_RE.finditer(title or ''):
        r = re.sub(r'\s+', ' ', m.group(1)).strip()
        if r not in ratings:
            ratings.append(r)
    o = _TITLE_OUTLOOK_RE.search(title or '')
    return '/'.join(ratings), (o.group(1).capitalize() if o else None)


class IndiaRatingsAdapter(Adapter):
    name = 'India Ratings'
    enabled = True

    def key(self, row: dict) -> tuple:
        # One press release can give several rows, one per instrument.
        return (self.name, _key_of(row), row.get('Instrument / Facility') or '',
                row.get('Amount') or '', row.get('Rating') or '')

    # ---- network helpers -------------------------------------------------
    def _get_json(self, url, params, referer):
        last = None
        for attempt in range(3):
            try:
                r = self.s.get(url, params=params, headers={'Referer': referer}, timeout=60)
                r.raise_for_status()
                data = r.json()
                if not isinstance(data, list):
                    raise ValueError('unexpected response shape')
                return data
            except Exception as e:
                last = e
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f'India Ratings request failed ({params}): {last}')

    def _list_page(self, sector: str, page: int) -> list:
        data = self._get_json(LIST_URL, {'type': 'rac', 'year': '', 'sector': sector, 'pageNo': page,
                                         'searchText': ''},
                              f'{BASE}/raclisting/rac/sector/{sector.replace(" ", "%20")}')
        out = []
        for d in data:
            key = (d.get('urlKey') or '').strip()
            eff = (d.get('effectiveDate') or '')[:10]
            company = re.sub(r'\s+', ' ', d.get('issuerName') or '').strip()
            if not key or not eff or not company:
                continue
            datetime.strptime(eff, '%Y-%m-%d')            # raises if the format ever changes
            out.append({'key': key.lower(), 'date': eff, 'company': company,
                        'title': _clean(d.get('pressReleaseTitle')),
                        'industry': (d.get('industryName') or '').strip() or 'Other',
                        'concat': d.get('issuersName_concat') or ''})
        return out

    def _detail_table(self, item: dict):
        # The referer goes into an HTTP header, which cannot hold characters such as an en dash,
        # so the issuer part is percent-encoded.
        data = self._get_json(DETAIL_URL, {'pressReleaseId': item['key'], 'issuerconcat': item['concat']},
                              f"{BASE}/pressrelease/{item['key']}/{quote(item['concat'], safe='')}")
        return (data[0] or {}).get('currentRatings') if data else None

    # ---- row building ----------------------------------------------------
    def _base(self, item: dict) -> dict:
        link = item['key'] if LINK_STYLE == 'key' else f"{BASE}/pressrelease/{item['key']}"
        return {'Rating Agency': self.name, 'Date': item['date'], 'Company Name': item['company'],
                'Industry / Sector': item['industry'], 'PDF File Name': None, 'Link': link,
                'Source ID': item['key'] if ALSO_SOURCE_ID else None, 'Additional Details': None}

    def _rows_for(self, item: dict) -> list:
        base = self._base(item)
        try:
            parsed = parse_current_ratings(self._detail_table(item))
        except Exception as e:
            print(f"WARNING: detail request failed for {item['key']} ({item['company']}): {e}")
            parsed = []
        verb = _title_verb(item['title'])
        if not parsed:
            print(f"WARNING: could not read the rating table for {item['key']} ({item['company']}); "
                  'added with rating details from the headline only')
            rating, outlook = parse_title(item['title'])
            r = dict(base)
            r['Rating'] = rating or None
            r['Outlook'] = outlook
            r['Rating Action'] = verb or None
            return [r]
        rows = []
        for p in parsed:
            r = dict(base)
            r['Instrument / Facility'] = p['instrument'] or None
            r['Amount'] = p['amount']
            r['Rating'] = p['rating'] or None
            r['Outlook'] = p['outlook']
            r['Rating Action'] = p['action'] or verb or None
            rows.append(r)
        return rows

    # ---- main ------------------------------------------------------------
    def _fetch_sector(self, sector, since_iso, known, seen, core):
        out, prev = [], None
        for page in range(1, MAX_PAGES + 1):
            items = self._list_page(sector, page)
            if not items:
                if page == 1:
                    msg = f'India Ratings list "{sector}" page 1 returned no rows'
                    if core:
                        raise RuntimeError(msg + ' (site change or blocked?)')
                    print('WARNING: ' + msg + ' - sector name may be wrong; nothing added for it')
                break
            ids = [i['key'] for i in items]
            if ids == prev:
                break                       # site repeated the same page: end of list
            prev = ids
            for it in items:
                if it['key'] in known or it['key'] in seen or it['date'] < since_iso:
                    continue
                seen.add(it['key'])
                out.extend(self._rows_for(it))
                time.sleep(PAUSE_SECONDS)
            if all(i in known for i in ids) or all(i['date'] < since_iso for i in items):
                break
            time.sleep(PAUSE_SECONDS)
        return out

    def fetch(self, since: date, known_keys: set) -> list:
        since_iso = since.isoformat()
        known = {k[1] for k in known_keys if k and k[0] == self.name}
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        out, seen = [], set()
        for n, sector in enumerate(SECTORS):
            try:
                out.extend(self._fetch_sector(sector, since_iso, known, seen, core=(n == 0)))
            except Exception as e:
                if n == 0:
                    raise
                print(f'WARNING: India Ratings sector "{sector}" skipped: {e}')
        return out


# In case the existing __init__.py imports the stub under a different class name
IndiaRatingsAdapter_ = IndiaRatingsAdapter
IndiaRating = IndiaRatingsAdapter
