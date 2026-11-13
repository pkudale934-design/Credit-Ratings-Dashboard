import re
import time
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup          # needs beautifulsoup4 in requirements

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


def parse_list(html: str) -> list:
    """Rows of one liveratings page -> [{'company','city','date_text','date','company_code','master_id'}]"""
    soup = BeautifulSoup(html, 'html.parser')
    out = []
    for tr in soup.select('tr[id]'):
        th = tr.select_one('th.td-date')
        a = tr.select_one('td.td-company a')
        if not th or not a:
            continue
        link_el = tr.select_one('a.instrument-link') or a
        d = parse_list_date(th.get_text())
        out.append({'company': _clean(a.get_text()), 'city': a.get('title'),
                    'date_text': _clean(th.get_text()), 'date': d,
                    'company_code': link_el.get('data-company'),
                    'master_id': link_el.get('data-master') or tr.get('id')})
    return out


def parse_instruments(html: str) -> list:
    """Rows of the 'Instruments' modal table."""
    soup = BeautifulSoup(html, 'html.parser')
    out = []
    for tr in soup.select('table.instrument-table tbody tr'):
        td = [_clean(c.get_text()) for c in tr.find_all('td')]
        if len(td) < 8:
            continue
        # Sr, Date, ISIN, Instrument, Bank, Term, Quantum, Rating, Status, Listed, Regulator
        out.append({'instrument': td[3], 'bank': td[4], 'term': td[5], 'quantum': td[6],
                    'rating': td[7], 'status': td[8] if len(td) > 8 else '',
                    'isin': td[2]})
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
