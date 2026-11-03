#!/usr/bin/env python3
"""
fpl-feed v1 (3 Nov 2026): saves public Fantasy Premier League API data for one mini-league into a folder that the
GitHub Action publishes. Standard library only. No login, no cookies, no secrets: every endpoint used here is public.

Usage: python3 feed.py <task> <data_dir>
  auto     what the schedule runs every 2 hours: core, plus daily if the last daily run is 20+ hours old
  core     the json file (bootstrap-static), all fixtures, event-status, league standings (every page), each member's
           entry + history (chips) + picks for every GW whose deadline has passed, my transfers, and event/N/live while
           a GW is live (plus each finished GW once, frozen after FPL marks it data_checked)
  daily    element-summary for my squad, all members' current picks, config watchlist, top-N by ownership and by
           ep_next; team/set-piece-notes
  probe    one request per endpoint type; writes probe.json (status code, bytes, time, sanity check)
  archive  ~2 h before each deadline: dated xz-compressed copy of the json file in archive/<season>/ (sleeps until
           T-2h when the next deadline is 2-4.2 h away; archives at once if it is under 2 h away and not yet done)

Files are compact JSON exactly as FPL returned them. A file is rewritten only when its content changed; for the json
file, changes in ownership/transfer counters alone do not count (they move every minute), so the snapshot is not
rewritten just for those. manifest.json lists every file with sha256, bytes and the time its content last changed;
status.json records the run (requests, errors, what changed, current GW, next deadline, repo size).
"""
import datetime as dt, gzip, hashlib, json, lzma, os, sys, time, urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, 'config.json')))
API = os.environ.get('FPL_API', 'https://fantasy.premierleague.com/api')  # override only for local tests
UA = 'fpl-feed/1.0 (personal mini-league archive; GitHub Actions; public endpoints only)'
PAUSE = float(os.environ.get('FEED_PAUSE', '0.35'))  # seconds between requests: be polite
NOW = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
STAMP = NOW.isoformat().replace('+00:00', 'Z')
LOG, ERRORS, WARN = [], [], []
VOLATILE = {'selected_by_percent', 'transfers_in', 'transfers_out', 'transfers_in_event', 'transfers_out_event',
            'form', 'value_form', 'value_season', 'form_rank', 'form_rank_type', 'selected_rank',
            'selected_rank_type', 'price_change_percent'}


class FetchError(Exception):
    pass


def get(path, tries=4):
    url = f'{API}/{path}'
    err, code = '', None
    for k in range(tries):
        t0 = time.time()
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'application/json',
                                                       'Accept-Encoding': 'gzip'})
            with urllib.request.urlopen(req, timeout=40) as r:
                body, code = r.read(), r.status
                if r.headers.get('Content-Encoding') == 'gzip': body = gzip.decompress(body)
            obj = json.loads(body)
            LOG.append(dict(path=path, code=code, bytes=len(body), ms=int((time.time() - t0) * 1000), tries=k + 1))
            time.sleep(PAUSE)
            return obj
        except urllib.error.HTTPError as e:
            code, err = e.code, e.read()[:160].decode('utf-8', 'replace')
        except Exception as e:  # timeouts, resets, JSON errors (FPL serves an HTML page while 'being updated')
            code, err = None, repr(e)[:160]
        if code == 404: break
        time.sleep(min(60, 4 * 2 ** k))
    LOG.append(dict(path=path, code=code, err=err, tries=k + 1))
    raise FetchError(f'{path}: HTTP {code} {err}')


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def boot_fp(b):
    """What counts as a change of the json file: everything except the constantly moving counters."""
    return dict(events=[{k: e.get(k) for k in ('id', 'deadline_time', 'finished', 'data_checked', 'is_current',
                                                'is_next', 'is_previous')} for e in b.get('events', [])],
                teams=b.get('teams'), element_types=b.get('element_types'),
                elements=[{k: v for k, v in e.items() if k not in VOLATILE} for e in b.get('elements', [])])


class Tree:
    def __init__(self, root):
        self.root = root
        os.makedirs(root, exist_ok=True)
        self.changed = []
        self.man = self.read('manifest.json') or {}
        self.status_old = self.read('status.json') or {}
        self.n_before = len(self.files())

    def path(self, rel):
        return os.path.join(self.root, rel)

    def read(self, rel):
        try:
            return json.load(open(self.path(rel), encoding='utf-8'))
        except (FileNotFoundError, ValueError):
            return None

    def exists(self, rel):
        return os.path.exists(self.path(rel))

    def files(self):
        out = []
        for d, _, fs in os.walk(self.root):
            if '.git' in d.split(os.sep): continue
            out += [os.path.relpath(os.path.join(d, f), self.root) for f in fs]
        return [f for f in out if not f.startswith('.git') and f not in ('manifest.json', 'status.json')]

    def put(self, rel, obj, fp=None, raw=None):
        data = raw if raw is not None else dumps(obj)
        p = self.path(rel)
        old = open(p, 'rb').read() if os.path.exists(p) else None
        if old == data: return False
        if fp is not None and old is not None:
            try:
                if dumps(fp(json.loads(old))) == dumps(fp(obj)): return False
            except ValueError:
                pass
        os.makedirs(os.path.dirname(p) or '.', exist_ok=True)
        tmp = p + '.tmp'
        open(tmp, 'wb').write(data); os.replace(tmp, p)
        self.changed.append(rel)
        self.man[rel] = dict(sha256=hashlib.sha256(data).hexdigest(), bytes=len(data), changed_at=STAMP)
        return True

    def finish(self, extra):
        files = sorted(self.files())
        if len(files) < self.n_before:
            raise SystemExit(f'SAFETY STOP: {self.n_before} files before, {len(files)} after; nothing published')
        man = {}
        for f in files:
            m = self.man.get(f)
            if m is None or f in self.changed or not m.get('sha256'):
                data = open(self.path(f), 'rb').read()
                m = dict(sha256=hashlib.sha256(data).hexdigest(), bytes=len(data), changed_at=(m or {}).get('changed_at', STAMP))
            man[f] = m
        old_hb = self.status_old.get('generated_at', '2000-01-01T00:00:00Z')
        age_h = (NOW - dt.datetime.fromisoformat(old_hb.replace('Z', '+00:00'))).total_seconds() / 3600
        if not self.changed and not ERRORS and age_h < 6 and not extra.get('force_status'):
            print('Nothing changed; no commit.'); return False
        open(self.path('manifest.json'), 'wb').write(json.dumps(dict(generated_at=STAMP, files=man), indent=0).encode())
        st = dict(generated_at=STAMP, run_url=run_url(), requests=len([x for x in LOG if 'err' not in x]),
                  failed=[x for x in LOG if 'err' in x], errors=ERRORS, warnings=WARN, changed=self.changed,
                  n_files=len(files), data_bytes=sum(m['bytes'] for m in man.values()),
                  daily_at=self.status_old.get('daily_at'), live_final=self.status_old.get('live_final', []))
        st.update({k: v for k, v in extra.items() if k != 'force_status'})
        open(self.path('status.json'), 'wb').write(json.dumps(st, ensure_ascii=False, indent=1).encode())
        msg = f"feed {STAMP} {extra.get('task', '')}: {len(self.changed)} file(s) changed" + (f', {len(ERRORS)} error(s)' if ERRORS else '')
        open(os.path.join(os.environ.get('RUNNER_TEMP', '/tmp'), 'commit_msg.txt'), 'w').write(msg + '\n\n' + '\n'.join(self.changed[:200]))
        print(msg); return True


def run_url():
    s, r, i = (os.environ.get(k) for k in ('GITHUB_SERVER_URL', 'GITHUB_REPOSITORY', 'GITHUB_RUN_ID'))
    return f'{s}/{r}/actions/runs/{i}' if s and r and i else None


def repo_size_kb():
    r, tok = os.environ.get('GITHUB_REPOSITORY'), os.environ.get('GH_TOKEN')
    if not r: return None
    try:
        req = urllib.request.Request(f'https://api.github.com/repos/{r}', headers={'User-Agent': UA,
                                     **({'Authorization': f'Bearer {tok}'} if tok else {})})
        return json.load(urllib.request.urlopen(req, timeout=20)).get('size')
    except Exception as e:
        WARN.append(f'repo size check failed: {e!r}'[:200]); return None


def ts(s):
    return dt.datetime.fromisoformat(s.replace('Z', '+00:00')) if s else None


def gw_state(b):
    ev = b['events']
    started = [e['id'] for e in ev if e.get('deadline_time') and ts(e['deadline_time']) <= NOW]
    cur = next((e for e in ev if e.get('is_current')), None) or (next(e for e in ev if e['id'] == max(started)) if started else None)
    nxt = next((e for e in ev if e.get('deadline_time') and ts(e['deadline_time']) > NOW), None)
    return started, cur, nxt


def validate_boot(b):
    n_el, n_ev, n_tm = len(b.get('elements', [])), len(b.get('events', [])), len(b.get('teams', []))
    if n_el < 500 or n_ev != 38 or n_tm != 20:
        raise FetchError(f'bootstrap-static looks wrong: {n_el} players, {n_ev} events, {n_tm} teams')
    return n_el


def guarded(T, label, fn):
    try:
        return fn()
    except FetchError as e:
        ERRORS.append(f'{label}: {e}'[:300]); print('ERROR', label, e); return None


def core(T):
    b = get('bootstrap-static/')
    n_el = validate_boot(b)
    T.put('bootstrap-static.json', b, fp=boot_fp)
    started, cur, nxt = gw_state(b)
    fx = guarded(T, 'fixtures', lambda: get('fixtures/'))
    if fx is not None:
        if len(fx) != 380: WARN.append(f'fixtures: {len(fx)} (expected 380)')
        T.put('fixtures.json', fx)
    es = guarded(T, 'event-status', lambda: get('event-status/'))
    if es is not None: T.put('event-status.json', es)
    L = CFG['league_id']; res, page, meta = [], 1, None
    while page <= 20:
        s = guarded(T, f'standings p{page}', lambda: get(f'leagues-classic/{L}/standings/?page_standings={page}'))
        if s is None: break
        meta = meta or s; res += s['standings']['results']
        if not s['standings'].get('has_next'): break
        page += 1
    members = []
    if meta is not None:
        meta = dict(meta); meta['standings'] = dict(meta['standings'], results=res, page=1, has_next=False)
        T.put('league/standings.json', meta)
        members = [r['entry'] for r in res]
    if CFG['me'] not in members: members.append(CFG['me']); WARN.append('my entry is not in the league standings')
    done_final = set(T.status_old.get('live_final', []))
    ev_by = {e['id']: e for e in b['events']}
    for e in members:
        guarded(T, f'entry {e}', lambda: T.put(f'entries/{e}/entry.json', get(f'entry/{e}/')))
        guarded(T, f'history {e}', lambda: T.put(f'entries/{e}/history.json', get(f'entry/{e}/history/')))
        for g in started:
            rel = f'picks/gw{g}/{e}.json'
            settled = ev_by[g].get('data_checked') and g in done_final
            if T.exists(rel) and (settled or g < (cur['id'] if cur else g) - 1): continue
            try:
                T.put(rel, get(f'entry/{e}/event/{g}/picks/', tries=2))
            except FetchError as x:
                if 'HTTP 404' not in str(x): ERRORS.append(f'picks {e} gw{g}: {x}'[:300])
    guarded(T, 'transfers', lambda: T.put(f"entries/{CFG['me']}/transfers.json", get(f"entry/{CFG['me']}/transfers/")))
    live = False
    finals = list(done_final)
    for g in started:
        evg = ev_by[g]; rel = f'live/gw{g}.json'
        is_live = cur is not None and g == cur['id'] and not (evg.get('finished') and evg.get('data_checked'))
        need_final = evg.get('finished') and evg.get('data_checked') and g not in done_final
        if is_live or need_final or not T.exists(rel):
            r = guarded(T, f'live gw{g}', lambda: get(f'event/{g}/live/'))
            if r is not None:
                T.put(rel, r)
                if need_final: finals.append(g)
        live = live or is_live
    return dict(n_players=n_el, current_gw=cur['id'] if cur else None, live=live, live_final=sorted(set(finals)),
                next_gw=nxt['id'] if nxt else None, next_deadline=nxt['deadline_time'] if nxt else None,
                members=len(members)), b


def daily(T, b):
    _, cur, _ = gw_state(b)
    ids = set(int(i) for i in CFG.get('watchlist', []))
    if cur:
        for f in os.listdir(T.path(f"picks/gw{cur['id']}")) if T.exists(f"picks/gw{cur['id']}") else []:
            p = T.read(f"picks/gw{cur['id']}/{f}") or {}
            ids |= {x['element'] for x in p.get('picks', [])}
    tr = T.read(f"entries/{CFG['me']}/transfers.json") or []
    ids |= {t['element_in'] for t in tr if cur and t['event'] > cur['id']}  # pending transfers for the next GW
    el = b['elements']
    own = sorted(el, key=lambda e: -float(e.get('selected_by_percent') or 0))[:CFG.get('top_owned', 120)]
    ep = sorted(el, key=lambda e: -float(e.get('ep_next') or 0))[:CFG.get('top_ep', 40)]
    ids |= {e['id'] for e in own + ep}
    for i in sorted(ids):
        guarded(T, f'element-summary {i}', lambda: T.put(f'element-summary/{i}.json', get(f'element-summary/{i}/')))
    sp = guarded(T, 'set-piece-notes', lambda: get('team/set-piece-notes/'))
    if sp is not None: T.put('set-piece-notes.json', sp)
    return dict(daily_at=STAMP, daily_players=len(ids))


def probe(T):
    rows = []
    def tryit(label, path, check):
        t0 = time.time()
        try:
            r = get(path, tries=1); ok, note = check(r)
            rows.append(dict(endpoint=label, path=path, ok=ok, code=LOG[-1]['code'], bytes=LOG[-1]['bytes'],
                             ms=int((time.time() - t0) * 1000), check=note)); return r
        except FetchError as e:
            rows.append(dict(endpoint=label, path=path, ok=False, code=LOG[-1].get('code'), bytes=0,
                             ms=int((time.time() - t0) * 1000), check=str(e)[:160])); return None
    b = tryit('json file', 'bootstrap-static/', lambda r: (len(r['elements']) > 500 and len(r['events']) == 38,
                                                          f"{len(r['elements'])} players, {len(r['events'])} events"))
    cur = gw_state(b)[1]['id'] if b and gw_state(b)[1] else 1
    me, L = CFG['me'], CFG['league_id']
    tryit('fixtures', 'fixtures/', lambda r: (len(r) == 380, f'{len(r)} fixtures'))
    tryit('event-status', 'event-status/', lambda r: ('status' in r, f"{len(r.get('status', []))} days"))
    tryit('standings', f'leagues-classic/{L}/standings/', lambda r: (len(r['standings']['results']) > 0,
                                                                    f"{len(r['standings']['results'])} teams"))
    tryit('entry', f'entry/{me}/', lambda r: (r.get('id') == me, f"id {r.get('id')}"))
    tryit('history', f'entry/{me}/history/', lambda r: ('current' in r, f"{len(r['current'])} GWs, {len(r['chips'])} chips"))
    tryit('picks', f'entry/{me}/event/{cur}/picks/', lambda r: (len(r['picks']) == 15, f"GW{cur}: {len(r['picks'])} picks"))
    tryit('transfers', f'entry/{me}/transfers/', lambda r: (isinstance(r, list), f'{len(r)} transfers'))
    tryit('live', f'event/{cur}/live/', lambda r: (len(r['elements']) > 500, f"GW{cur}: {len(r['elements'])} players"))
    tryit('element-summary', 'element-summary/1/', lambda r: ('history' in r, f"{len(r['history'])} matches"))
    tryit('set-piece-notes', 'team/set-piece-notes/', lambda r: ('teams' in r, f"{len(r.get('teams', []))} teams"))
    out = dict(generated_at=STAMP, run_url=run_url(), all_ok=all(r['ok'] for r in rows), results=rows)
    T.put('probe.json', None, raw=json.dumps(out, indent=1).encode())
    lines = ['| endpoint | HTTP | OK | bytes | ms | check |', '|---|---|---|---|---|---|'] + [
        f"| {r['endpoint']} | {r['code']} | {'yes' if r['ok'] else 'NO'} | {r['bytes']} | {r['ms']} | {r['check']} |" for r in rows]
    summary = f"## FPL probe {STAMP}: {'ALL OK' if out['all_ok'] else 'PROBLEMS'}\n\n" + '\n'.join(lines) + '\n'
    print(summary)
    if os.environ.get('GITHUB_STEP_SUMMARY'): open(os.environ['GITHUB_STEP_SUMMARY'], 'a').write(summary)
    return out


def archive(root):
    season = CFG.get('season', '2026-27'); d = os.path.join(root, 'archive', season); os.makedirs(d, exist_ok=True)
    idx_p = os.path.join(d, 'index.json')
    idx = json.load(open(idx_p)) if os.path.exists(idx_p) else []
    b = get('bootstrap-static/'); validate_boot(b)
    _, _, nxt = gw_state(b)
    if nxt is None: print('No deadline ahead.'); return False
    gw, dl = nxt['id'], ts(nxt['deadline_time'])
    if any(x['gw'] == gw for x in idx) and not os.environ.get('FORCE'):
        print(f'GW{gw} already archived.'); return False
    h = (dl - dt.datetime.now(dt.timezone.utc)).total_seconds() / 3600
    if h > 4.2 and not os.environ.get('FORCE'):
        print(f'GW{gw} deadline in {h:.1f} h: nothing to do yet.'); return False
    if h > 2.0 and not os.environ.get('FORCE'):
        wait = (h - 2.0) * 3600; print(f'GW{gw} deadline in {h:.2f} h: sleeping {wait / 60:.0f} min until T-2h.', flush=True)
        time.sleep(wait)
        b = get('bootstrap-static/'); validate_boot(b)
    t = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    hb = round((dl - t).total_seconds() / 3600, 2)
    raw = dumps(b)
    name = f"gw{gw:02d}_bootstrap-static_{t.strftime('%Y-%m-%dT%H%MZ')}.json.xz"
    open(os.path.join(d, name), 'wb').write(lzma.compress(raw, preset=9))
    idx.append(dict(gw=gw, deadline=nxt['deadline_time'], archived_at=t.isoformat().replace('+00:00', 'Z'),
                    hours_before=hb, file=name, sha256_json=hashlib.sha256(raw).hexdigest(),
                    bytes_json=len(raw), players=len(b['elements'])))
    json.dump(idx, open(idx_p, 'w'), indent=1)
    msg = f'archive GW{gw} json file, {hb} h before the deadline'
    open(os.path.join(os.environ.get('RUNNER_TEMP', '/tmp'), 'commit_msg.txt'), 'w').write(msg)
    print(msg); return True


def main(task, root):
    if task == 'archive':
        archive(root); return
    T = Tree(root)
    extra = dict(task=task)
    if task == 'probe':
        out = probe(T); extra.update(probe_ok=out['all_ok'], force_status=True)
        T.finish(extra); sys.exit(0 if out['all_ok'] else 1)
    try:
        info, b = core(T); extra.update(info)
    except FetchError as e:  # the json file itself failed: publish the error, then fail the run (GitHub emails you)
        ERRORS.append(f'json file: {e}'[:300]); extra.update(force_status=True); T.finish(extra); sys.exit(1)
    last = ts(T.status_old.get('daily_at'))
    if task == 'daily' or (task == 'auto' and (last is None or (NOW - last).total_seconds() > 20 * 3600)):
        extra.update(daily(T, b))
    size = repo_size_kb(); extra['repo_size_kb'] = size
    if size and size > 700_000: WARN.append(f'repo size {size / 1024:.0f} MB: above 700 MB (limit we keep: 1 GB)')
    T.finish(extra)
    if size and size > 900_000: raise SystemExit(f'repo size {size / 1024:.0f} MB > 900 MB: stop and clean up')
    if len(ERRORS) > 5: raise SystemExit(f'{len(ERRORS)} fetch errors this run (see status.json)')


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] not in ('auto', 'core', 'daily', 'probe', 'archive'):
        raise SystemExit(__doc__)
    main(sys.argv[1], sys.argv[2])
