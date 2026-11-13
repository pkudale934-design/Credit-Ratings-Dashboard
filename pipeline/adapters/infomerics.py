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


class InfomericsAdapter(Adapter):
    name = 'Infomerics'
    enabled = True

    def key(self, row: dict) -> tuple:
        # New rows from this feed have no rating/instrument yet, so the default key would not
        # match the richer rows already in the dashboard. The PDF file name identifies a press release.
        return (self.name, _file_name(row.get('Link')))

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
            except Exception as e:      # retry, then raise
                last_error = e
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f'Infomerics page {page} failed: {last_error}')

    def _to_row(self, item: dict):
        pr = item.get('PressRelease') or {}
        doc = pr.get('Document') or {}
        file_ = doc.get('DocumentFile') or {}
        url = file_.get('url') or ''
        iso = (pr.get('Date') or item.get('Date') or '')[:10]
        company = (item.get('CompanyName') or '').strip()
        if not url or not iso or not company:
            return None
        fragment = url[len(BLOB_PREFIX):] if url.startswith(BLOB_PREFIX) else url
        return {
            'Rating Agency': self.name,
            'Date': iso,
            'Company Name': company,
            'Report Title': (doc.get('DocumentTitle') or company).strip(),
            'Source ID': str(pr.get('id') or ''),
            'Link': fragment,
            'Additional Details': item.get('slug') or '',
        }

    def fetch(self, since: date, known_keys: set) -> list:
        since_iso = since.isoformat()
        out, seen = [], set()
        for page in range(1, MAX_PAGES + 1):
            payload = self._get_page(page)
            items = payload['data']
            if not items:
                break

            page_rows = [r for r in (self._to_row(i) for i in items) if r]
            new_on_page = 0
            for row in page_rows:
                k = self.key(row)
                if k in known_keys or k in seen:
                    continue
                seen.add(k)
                if row['Date'] >= since_iso:
                    out.append(row)
                    new_on_page += 1

            # Stop when the whole page is already known, or is older than the start date,
            # or the site says there is nothing more.
            all_known = bool(page_rows) and all(self.key(r) in known_keys for r in page_rows)
            all_older = bool(page_rows) and all(r['Date'] < since_iso for r in page_rows)
            more = (payload.get('meta') or {}).get('hasmoredata', True)
            if all_known or all_older or not more:
                break
            time.sleep(PAUSE_SECONDS)
        return out
