import re
import time
from datetime import date, datetime

import requests
from html.parser import HTMLParser     # standard library: no extra package needed

from .base import Adapter

BASE = 'https://connect.acuite.in'
LIST_URL = BASE + '/liveratings'
INSTR_URL = BASE + '/latest-intrument'      # (sic) the site's own spelling
LINK_FMT = BASE + '/fcompany-details/{company}/{date}'
MAX_PAGES = 30
PAUSE_SECONDS = 0.5
RATING_PREFIX = 'ACUITE '     # CHECK: how existing Acuité ratings are stored in the dashboard

HEADERS = {
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-GB,en-US;q=0.9,en;q=0.8',
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36'),
}


# ------------------------------------------------------------ helpers
def _cr(x: float) -> str:
    return f'Rs. {x:,.2f} Cr.'


def _clean(s) -> str:
    return re.sub(r'\s+', ' ', s or '').strip()


def parse_list_date(text: str):
    """'12th  Nov 26' -> date(2026, 11, 12); None if unreadable."""
    s = re.sub(r'^(\d+)(?:st|nd|rd|th)\b', r'\1', _clean(text))
    try:
        return datetime.strptime(s, '%d %b %y').date()
    except ValueError:
        return None


def make_link(company: str, date_text: str) -> str:
    """Matches stored rows: .../fcompany-details/ATUL_PUMPS_PRIVATE_LIMITED/4th_Nov_26"""
    return LINK_FMT.format(company=re.sub(r'\s+', '_', company.strip()),
                           date=re.sub(r'\s+', '_', date_text.strip()))


def is_blocked(r) -> bool:
    head = (r.text or '')[:5000].lower()
    return (r.status_code in (403, 429, 503)
            or 'just a moment' in head or 'cf-chl' in head or 'challenge-platform' in head)


class _ListParser(HTMLParser):
    """Rows of a liveratings page: <tr id=..> with th.td-date and td.td-company a."""
    def __init__(self):
        super().__init__()
        self.rows, self.row = [], None
        self.in_date = self.in_td_company = self.in_a = False

    @staticmethod
    def _cls(attrs):
        return (dict(attrs).get('class') or '').split()

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        cls = self._cls(attrs)
        if tag == 'tr' and d.get('id'):
            self.row = {'id': d['id'], 'date': '', 'company': '', 'city': None,
                        'code': None, 'master': None}
        elif self.row is None:
            return
        elif tag == 'th' and 'td-date' in cls:
            self.in_date = True
        elif tag == 'td' and 'td-company' in cls:
            self.in_td_company = True
        elif tag == 'a':
            if 'instrument-link' in cls:
                self.row['code'] = d.get('data-company')
                self.row['master'] = d.get('data-master')
            if self.in_td_company and not self.row['company']:
                self.in_a = True
                self.row['city'] = d.get('title')
                if self.row['code'] is None:
                    self.row['code'] = d.get('data-company')
                    self.row['master'] = d.get('data-master')

    def handle_endtag(self, tag):
        if self.row is None:
            return
        if tag == 'th':
            self.in_date = False
        elif tag == 'a':
            self.in_a = False
        elif tag == 'td':
            self.in_td_company = False
        elif tag == 'tr':
            if self.row['date'] and self.row['company']:
                self.rows.append(self.row)
            self.row = None

    def handle_data(self, data):
        if self.row is None:
            return
        if self.in_date:
            self.row['date'] += data
        elif self.in_a:
            self.row['company'] += data


class _InstrParser(HTMLParser):
    """Cells of table.instrument-table tbody rows."""
    def __init__(self):
        super().__init__()
        self.rows, self.cells = [], None
        self.in_table = self.in_td = False
        self.text = ''

    def handle_starttag(self, tag, attrs):
        if tag == 'table' and 'instrument-table' in (dict(attrs).get('class') or ''):
            self.in_table = True
        elif self.in_table and tag == 'tr':
            self.cells = []
        elif self.in_table and tag == 'td' and self.cells is not None:
            self.in_td, self.text = True, ''

    def handle_endtag(self, tag):
        if tag == 'td' and self.in_td:
            self.cells.append(_clean(self.text))
            self.in_td = False
        elif tag == 'tr' and self.cells is not None:
            if self.cells:
                self.rows.append(self.cells)
            self.cells = None
        elif tag == 'table':
            self.in_table = False

    def handle_data(self, data):
        if self.in_td:
            self.text += data


def parse_list(html: str) -> list:
    """Rows of one liveratings page -> [{'company','city','date_text','date','company_code','master_id'}]"""
    p = _ListParser()
    p.feed(html or '')
    return [{'company': _clean(r['company']), 'city': r['city'],
             'date_text': _clean(r['date']), 'date': parse_list_date(r['date']),
             'company_code': r['code'], 'master_id': r['master'] or r['id']}
            for r in p.rows]


def parse_instruments(html: str) -> list:
    """Rows of the 'Instruments' modal table."""
    p = _InstrParser()
    p.feed(html or '')
    out = []
    for td in p.rows:
        if len(td) < 8:
            continue
        # Sr, Date, ISIN, Instrument, Bank, Term, Quantum, Rating, Status, Listed, Regulator
        out.append({'instrument': td[3], 'bank': td[4], 'term': td[5], 'quantum': td[6],
                    'rating': td[7], 'status': td[8] if len(td) > 8 else '', 'isin': td[2]})
    return out


class AcuiteAdapter(Adapter):
    name = 'Acuité'
    enabled = True

    def key(self, row: dict) -> tuple:
        return (self.name, row.get('Link') or '', row.get('Instrument / Facility') or '',
                row.get('Amount') or '', row.get('Rating') or '')

    # ---- network ---------------------------------------------------------
    def _get(self, url, params=None, headers=None):
        last = None
        for attempt in range(3):
            try:
                r = self.s.get(url, params=params, headers=headers, timeout=90)
                if is_blocked(r):
                    raise PermissionError(
                        f'Acuité blocked the request (HTTP {r.status_code}, Cloudflare). '
                        'No rows were added.')
                r.raise_for_status()
                return r
            except PermissionError:
                raise                       # retrying will not help
            except Exception as e:
                last = e
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f'Acuité request failed: {url}: {last}')

    def _instruments(self, item: dict) -> list:
        r = self._get(INSTR_URL,
                      params={'company_code': item['company_code'],
                              'rcm_master_id': item['master_id'], 'page': ''},
                      headers={'X-Requested-With': 'XMLHttpRequest',
                               'Referer': LIST_URL})
        return parse_instruments(r.text)

    # ---- row building ----------------------------------------------------
    def _rows_for(self, item: dict, link: str, instruments: list) -> list:
        rows = []
        for p in instruments:
            status = p['status'].replace('*', '')
            extra = None
            m = re.search(r'\s*&\s*(Issuer not co-?operating)', status, re.I)
            if m:
                extra = 'Issuer not co-operating'
                status = status.replace(m.group(0), '').strip()
            rating = p['rating']
            if rating and not rating.upper().startswith('ACUITE'):
                rating = RATING_PREFIX + rating
            try:
                amount = _cr(float(p['quantum'].replace(',', '')))
            except ValueError:
                amount = None
            rows.append({'Rating Agency': self.name, 'Date': item['date'].isoformat(),
                         'Company Name': item['company'], 'Industry / Sector': 'Other',
                         'Instrument / Facility': p['instrument'], 'Amount': amount,
                         'Rating': rating, 'Outlook': 'Nil', 'Rating Action': status,
                         'PDF File Name': None, 'Link': link, 'Additional Details': extra})
        return rows

    # ---- main ------------------------------------------------------------
    def fetch(self, since: date, known_keys: set) -> list:
        known_links = {k[1] for k in known_keys if k and k[0] == self.name}
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self._get(LIST_URL)                  # cookies; also fails loudly if blocked
        out, seen = [], set()
        for page in range(1, MAX_PAGES + 1):
            items = parse_list(self._get(LIST_URL, params={'page': page}).text)
            if not items:
                if page == 1:
                    print('WARNING: Acuité list page 1 had no rows (layout changed?)')
                break
            new_on_page, reached_old = 0, False
            for it in items:
                if it['date'] is None:
                    print(f"WARNING: Acuité: unreadable date '{it['date_text']}' ({it['company']}); skipped")
                    continue
                if it['date'] < since:
                    reached_old = True
                    continue
                link = make_link(it['company'], it['date_text'])
                if link in known_links or link in seen:
                    continue
                new_on_page += 1
                try:
                    instruments = self._instruments(it)
                except PermissionError:
                    raise
                except Exception as e:
                    print(f"WARNING: Acuité: {it['company']}: {e}; skipped, will retry next run")
                    continue
                if not instruments:
                    print(f"WARNING: Acuité: no instruments for {it['company']} ({it['date_text']}); skipped, will retry next run")
                    continue
                seen.add(link)
                out.extend(self._rows_for(it, link, instruments))
                time.sleep(PAUSE_SECONDS)
            if reached_old or new_on_page == 0:
                break                        # newest-first: nothing newer on later pages
        return out
