"""Read/write the dashboard's embedded dataset (gzip+base64 JSON in <script id=d1>/<script id=d2>)."""
import base64, gzip, json, re

COLS = ['Rating Agency', 'Date', 'Company Name', 'Industry / Sector', 'Instrument / Facility', 'Amount',
        'Rating', 'Outlook', 'Rating Action', 'Status', 'Report Title', 'Document Type', 'Source ID',
        'PDF File Name', 'Link', 'Additional Details']
_BLOCK = r'(<script type="text/plain" id="%s">)(.*?)(</script>)'


def read_rows(html_path):
    """Return (html_text, rows) where rows is a list of dicts keyed by column (None = empty)."""
    html = open(html_path, encoding='utf-8').read()
    b64 = ''.join(re.search(_BLOCK % i, html, re.S).group(2).strip() for i in ('d1', 'd2'))
    j = json.loads(gzip.decompress(base64.b64decode(b64)))
    cols = j['cols']
    n = len(j['data'][cols[0]]['c'])
    rows = [{} for _ in range(n)]
    for c in cols:
        d, codes = j['data'][c]['d'], j['data'][c]['c']
        for i, k in enumerate(codes):
            rows[i][c] = None if k < 0 else d[k]
    return html, rows


def write_html(html, rows, out_path, as_of=None):
    """Re-encode rows into the same dictionary format and write the dashboard file."""
    data = {}
    for c in COLS:
        index, d, codes = {}, [], []
        for r in rows:
            v = r.get(c)
            if v is None:
                codes.append(-1)
                continue
            k = index.get(v)
            if k is None:
                k = index[v] = len(d)
                d.append(v)
            codes.append(k)
        data[c] = {'d': d, 'c': codes}
    raw = json.dumps({'cols': COLS, 'data': data}, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    b64 = base64.b64encode(gzip.compress(raw, 9, mtime=0)).decode('ascii')
    half = len(b64) // 2
    for i, part in (('d1', b64[:half]), ('d2', b64[half:])):
        html = re.sub(_BLOCK % i, lambda m: m.group(1) + '\n' + part + '\n' + m.group(3), html, count=1, flags=re.S)
    if as_of:
        html = re.sub(r'Data as of \d{4}-\d{2}-\d{2}', 'Data as of ' + as_of, html, count=1)
    open(out_path, 'w', encoding='utf-8').write(html)
