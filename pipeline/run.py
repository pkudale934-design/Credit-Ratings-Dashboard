"""Daily refresh: python -m pipeline.run --html site/index.html [--seed seed/index.html]"""
import argparse, json, os, shutil, sys, traceback
from datetime import date, datetime, timedelta, timezone
from .htmlio import read_rows, write_html
from .adapters import ALL

OVERLAP_DAYS = 5      # re-fetch this many days before the latest date to catch late/backdated items
ALERT_ZERO_RUNS = 3   # fail the job if an enabled adapter adds nothing this many runs in a row


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--html', required=True, help='dashboard file to update in place')
    ap.add_argument('--seed', help='used to create --html on the very first run')
    ap.add_argument('--only', help='run a single agency name, e.g. ICRA')
    a = ap.parse_args(argv)

    if not os.path.exists(a.html):
        os.makedirs(os.path.dirname(a.html) or '.', exist_ok=True)
        shutil.copy(a.seed, a.html)
    log_path = os.path.join(os.path.dirname(a.html) or '.', 'run_log.json')
    history = json.load(open(log_path)) if os.path.exists(log_path) else []

    html, rows = read_rows(a.html)
    # repair: India Ratings links stored as a bare key -> full address (safe to run every time)
    import re as _re
    for _r in rows:
        if _r.get('Rating Agency') == 'India Ratings':
            _l = (_r.get('Link') or '').strip()
            if _re.fullmatch(r'[A-Za-z0-9]{24}', _l):
                _r['Link'] = 'https://www.indiaratings.co.in/pressrelease/' + _l
    problems, report = [], {}
    for ad in ALL:
        if a.only and ad.name != a.only:
            continue
        if not ad.enabled:
            report[ad.name] = {'status': 'disabled'}
            continue
        mine = [r for r in rows if r['Rating Agency'] == ad.name]
        latest = max((r['Date'] for r in mine if r['Date']), default='2000-01-01')
        since = date.fromisoformat(latest) - timedelta(days=OVERLAP_DAYS)
        known = {ad.key(r) for r in mine}
        try:
            got = ad.fetch(since, known)
            new = []
            for r in got:
                r['Rating Agency'] = ad.name
                k = ad.key(r)
                if k not in known:
                    known.add(k)
                    new.append({c: r.get(c) for c in rows[0].keys()})
            rows.extend(new)
            report[ad.name] = {'status': 'ok', 'fetched': len(got), 'added': len(new), 'since': since.isoformat()}
        except Exception as e:
            traceback.print_exc()
            report[ad.name] = {'status': 'error', 'error': str(e)[:300]}
            problems.append(f'{ad.name}: error {e}')
            
# stale-source alert: nothing added today AND nothing added on the previous days (by calendar day, not by run)
    today = datetime.now(timezone.utc).date().isoformat()
    day_added = {}
    for h in history:
        d = h['ts'][:10]
        if d == today:
            continue
        for n, r in h['agencies'].items():
            day_added.setdefault(n, {})
            day_added[n][d] = day_added[n].get(d, 0) + (r.get('added') or 0)
    for name, rep in report.items():
        if rep['status'] == 'ok' and rep['added'] == 0:
            days = sorted(day_added.get(name, {}))[-(ALERT_ZERO_RUNS - 1):]
            if len(days) == ALERT_ZERO_RUNS - 1 and all(day_added[name][d] == 0 for d in days):
                problems.append(f'{name}: no new rows for {ALERT_ZERO_RUNS} days (site layout change?)')

    as_of = max((r['Date'] for r in rows if r['Date']), default=None)
    write_html(html, rows, a.html, as_of=as_of)
    history.append({'ts': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'total_rows': len(rows),
                    'as_of': as_of, 'agencies': report})
    json.dump(history[-60:], open(log_path, 'w'), indent=1, ensure_ascii=False)
    print(json.dumps(report, indent=1, ensure_ascii=False))
    if problems:
        print('PROBLEMS:\n' + '\n'.join(problems), file=sys.stderr)
        sys.exit(1)   # a failed job makes GitHub email you


if __name__ == '__main__':
    main()
