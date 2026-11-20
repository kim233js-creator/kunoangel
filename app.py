import json, sqlite3, datetime, pathlib, zipfile, io, html, sys, threading, webbrowser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
ROOT=pathlib.Path(sys.executable if getattr(sys, 'frozen', False) else __file__).resolve().parent
DB=ROOT/'universe.db'
SCHEMA='''
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS saves(id INTEGER PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS teams(id INTEGER PRIMARY KEY, save_id INTEGER NOT NULL REFERENCES saves(id), name TEXT NOT NULL, UNIQUE(save_id,name));
CREATE TABLE IF NOT EXISTS players(id INTEGER PRIMARY KEY, save_id INTEGER NOT NULL REFERENCES saves(id), external_id TEXT, name TEXT NOT NULL, birthdate TEXT, bats TEXT, throws TEXT, position TEXT, UNIQUE(save_id,external_id));
CREATE TABLE IF NOT EXISTS roster_moves(id INTEGER PRIMARY KEY, save_id INTEGER NOT NULL REFERENCES saves(id), player_id INTEGER NOT NULL REFERENCES players(id), team_id INTEGER REFERENCES teams(id), effective_date TEXT NOT NULL, move_type TEXT NOT NULL, from_level TEXT, to_level TEXT, note TEXT, source TEXT NOT NULL DEFAULT 'user', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS games(id INTEGER PRIMARY KEY, save_id INTEGER NOT NULL REFERENCES saves(id), external_id TEXT, game_date TEXT NOT NULL, away_team_id INTEGER NOT NULL REFERENCES teams(id), home_team_id INTEGER NOT NULL REFERENCES teams(id), away_runs INTEGER NOT NULL, home_runs INTEGER NOT NULL, away_hits INTEGER, home_hits INTEGER, away_errors INTEGER, home_errors INTEGER, innings_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'final', source TEXT NOT NULL DEFAULT 'user', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(save_id,external_id), CHECK(away_runs>=0 AND home_runs>=0));
CREATE TABLE IF NOT EXISTS performances(id INTEGER PRIMARY KEY, game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE, player_id INTEGER NOT NULL REFERENCES players(id), team_id INTEGER NOT NULL REFERENCES teams(id), role TEXT NOT NULL, stats_json TEXT NOT NULL, UNIQUE(game_id,player_id,role));
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, save_id INTEGER NOT NULL REFERENCES saves(id), event_date TEXT NOT NULL, event_type TEXT NOT NULL, importance TEXT NOT NULL DEFAULT 'C', headline TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', fact_status TEXT NOT NULL DEFAULT 'confirmed', source TEXT NOT NULL DEFAULT 'user', game_id INTEGER REFERENCES games(id), player_id INTEGER REFERENCES players(id), created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS stories(id INTEGER PRIMARY KEY, save_id INTEGER NOT NULL REFERENCES saves(id), event_id INTEGER REFERENCES events(id), game_id INTEGER REFERENCES games(id), created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, story_type TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL, fictional INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS settings(save_id INTEGER PRIMARY KEY REFERENCES saves(id), rules_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS imports(id INTEGER PRIMARY KEY, save_id INTEGER NOT NULL REFERENCES saves(id), content_hash TEXT NOT NULL, imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(save_id,content_hash));
CREATE INDEX IF NOT EXISTS idx_games_date ON games(save_id,game_date);
CREATE INDEX IF NOT EXISTS idx_events_date ON events(save_id,event_date);
'''
DEFAULT_RULES={'universe':'KBO UNIVERSE 2.0','source_of_truth':'Only user-provided OOTP data establishes factual results','tracked_team':'KIA 타이거즈','grades':{'S':'우승·대형 트레이드·리그 충격','A':'라이벌전·대기록·주축 부상','B':'유망주 데뷔·연승/연패','C':'일반 경기'},'article_order':['스코어보드','경기 흐름','선수 활약','가상 인터뷰','KIA 갤러리 반응'],'fiction_label':'가상 인터뷰 및 팬 반응','no_invented_stats':True}
def connect():
 c=sqlite3.connect(DB); c.row_factory=sqlite3.Row;c.execute('PRAGMA foreign_keys=ON');return c
def initialize():
 with connect() as c:
  c.executescript(SCHEMA)
  if not c.execute('SELECT 1 FROM saves').fetchone():
   c.execute('INSERT INTO saves(name) VALUES(?)',('새로운 KBO 세이브',))
   sid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
   c.execute('INSERT INTO settings(save_id,rules_json) VALUES(?,?)',(sid,json.dumps(DEFAULT_RULES,ensure_ascii=False)))
def json_response(handler,status,data,ctype='application/json; charset=utf-8'):
 body=json.dumps(data,ensure_ascii=False).encode() if not isinstance(data,bytes) else data
 handler.send_response(status);handler.send_header('Content-Type',ctype);handler.send_header('Content-Length',str(len(body)));handler.send_header('Cache-Control','no-store');handler.end_headers();handler.wfile.write(body)
def ensure_team(c,sid,name):
 c.execute('INSERT OR IGNORE INTO teams(save_id,name) VALUES(?,?)',(sid,name));return c.execute('SELECT id FROM teams WHERE save_id=? AND name=?',(sid,name)).fetchone()['id']
def ensure_player(c,sid,name):
 row=c.execute('SELECT id FROM players WHERE save_id=? AND name=?',(sid,name)).fetchall()
 if len(row)>1: raise ValueError('동명이인입니다. 선수 ID를 사용하세요.')
 if row:return row[0]['id']
 c.execute('INSERT INTO players(save_id,name) VALUES(?,?)',(sid,name));return c.execute('SELECT last_insert_rowid()').fetchone()[0]
def validate_date(d): datetime.date.fromisoformat(d)
def snapshot(c,sid):
 save=c.execute('SELECT * FROM saves WHERE id=?',(sid,)).fetchone()
 if not save:raise ValueError('세이브가 없습니다')
 tables=['teams','players','roster_moves','games','events','stories','settings']
 data={'format':'KBO_UNIVERSE_2','version':1,'exported_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'save':dict(save),'tables':{}}
 for table in tables:
  rows=c.execute(f'SELECT * FROM {table} WHERE save_id=?',(sid,)).fetchall()
  data['tables'][table]=[dict(r) for r in rows]
 data['tables']['performances']=[dict(r) for r in c.execute('SELECT p.* FROM performances p JOIN games g ON g.id=p.game_id WHERE g.save_id=?',(sid,))]
 return data
def restore(c,data):
 if data.get('format')!='KBO_UNIVERSE_2' or data.get('version')!=1:raise ValueError('지원하지 않는 백업 형식')
 tables=data['tables'];name=data['save']['name']+' (복원)'
 c.execute('INSERT INTO saves(name) VALUES(?)',(name,));sid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
 ids={}
 for table in ['teams','players','games','roster_moves','performances','events','stories','settings']:
  for original in tables.get(table,[]):
   r=dict(original);old=r.pop('id',None)
   if 'save_id' in r:r['save_id']=sid
   for col,ref in [('team_id','teams'),('away_team_id','teams'),('home_team_id','teams'),('player_id','players'),('game_id','games'),('event_id','events')]:
    if col in r and r[col] is not None:r[col]=ids[(ref,r[col])]
   cols=list(r);q=','.join('?' for _ in cols)
   c.execute(f'INSERT INTO {table}({",".join(cols)}) VALUES({q})',list(r.values()))
   if old is not None:ids[(table,old)]=c.execute('SELECT last_insert_rowid()').fetchone()[0]
 return sid
HTML='''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>KBO UNIVERSE 2.0</title><style>body{font:15px system-ui;background:#0e1420;color:#e8edf6;margin:0}header{padding:23px 5%;background:#172238;border-bottom:1px solid #344259}main{max-width:1150px;margin:auto;padding:24px}h1{margin:0}small,.muted{color:#9baec7}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:15px}.panel{background:#192438;padding:20px;border:1px solid #34445e;border-radius:13px;margin-bottom:16px}input,select,textarea,button{box-sizing:border-box;padding:10px;border-radius:7px;border:1px solid #4c607c;background:#101a2b;color:#fff;font:inherit;width:100%;margin:5px 0 10px}button{background:#3574d4;cursor:pointer;border:0;font-weight:600}button.secondary{background:#2b3c55}textarea{min-height:100px}table{border-collapse:collapse;width:100%;font-size:13px}td,th{padding:9px;border-bottom:1px solid #34445e;text-align:left}a{color:#9fc6ff}.stat{font-size:25px;font-weight:700}#message{white-space:pre-wrap;color:#9ddfb2}label{font-size:13px;color:#c1cce0}</style><header><h1>⚾ KBO UNIVERSE 2.0</h1><small>로컬 영구 저장 · OOTP 원본 우선 · AI 창작 분리</small></header><main><div class="panel"><label>활성 세이브</label><select id="save"></select><div class="grid"><input id="saveName" placeholder="새 세이브 이름"><button onclick="newSave()">새 세이브 생성</button></div><div id="message"></div></div><div class="grid"><div class="panel"><small>경기 수</small><div class="stat" id="countGames">0</div></div><div class="panel"><small>사건 수</small><div class="stat" id="countEvents">0</div></div><div class="panel"><small>기사 수</small><div class="stat" id="countStories">0</div></div></div><div class="grid"><section class="panel"><h2>경기 입력</h2><label>날짜</label><input id="date" type="date"><label>원정 / 홈</label><input id="away" placeholder="원정팀"><input id="home" placeholder="홈팀"><label>원정 득점 / 홈 득점</label><input id="ar" type="number" min="0" placeholder="원정 득점"><input id="hr" type="number" min="0" placeholder="홈 득점"><label>OOTP 경기 ID (선택, 중복 방지)</label><input id="gid" placeholder="예: 12948"><label>이닝별 점수 JSON (선택)</label><textarea id="innings" placeholder='{"away":[0,1,0],"home":[0,0,2]}'></textarea><button onclick="addGame()">경기 저장</button></section><section class="panel"><h2>세계관 사건</h2><label>날짜</label><input id="edate" type="date"><label>사건 등급</label><select id="grade"><option>S</option><option>A</option><option>B</option><option selected>C</option></select><label>사건 유형</label><select id="etype"><option>트레이드</option><option>부상</option><option>등록·말소</option><option>계약</option><option>유망주</option><option>기록</option><option>기타</option></select><input id="headline" placeholder="사건 제목"><textarea id="detail" placeholder="OOTP로 확인된 사실과 출처"></textarea><button onclick="addEvent()">사건 저장</button></section></div><div class="panel"><h2>선수단 등록·말소</h2><div class="grid"><input id="player" placeholder="선수 이름"><input id="rteam" placeholder="소속 구단"><input id="rdate" type="date"><select id="move"><option>콜업</option><option>말소</option><option>트레이드</option><option>영입</option><option>방출</option><option>부상자 명단</option><option>복귀</option></select></div><input id="rnote" placeholder="이동 사유/추가 설명"><button onclick="addRoster()">로스터 이력 저장</button></div><div class="grid"><div class="panel"><h2>최근 경기</h2><div id="games"></div></div><div class="panel"><h2>주요 사건</h2><div id="events"></div></div></div><div class="panel"><h2>세이브 복원 및 AI 연계</h2><div class="grid"><button onclick="downloadBackup()">전체 JSON 백업 다운로드</button><button class="secondary" onclick="downloadContext()">ChatGPT 인계 문서 다운로드</button></div><label>백업 파일에서 새 세이브로 복원 (기존 세이브 유지)</label><input type="file" id="restore" accept=".json"><button class="secondary" onclick="restoreBackup()">백업 복원</button><p class="muted">기사 생성은 인계 문서의 확정 기록만 사용하여 ChatGPT에서 수행합니다. 이 화면에서는 기사를 임의 생성하지 않습니다.</p></div></main><script>
const $=id=>document.getElementById(id), val=id=>$(id).value, sid=()=>Number(val('save'));async function api(path,method='GET',data){let r=await fetch(path,{method,headers:{'Content-Type':'application/json'},body:data===undefined?undefined:JSON.stringify(data)}),j=await r.json();if(!r.ok)throw Error(j.error||'오류');return j}function msg(t){$('message').textContent=t}function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}async function loadSaves(select){let d=await api('/api/saves');$('save').innerHTML=d.map(s=>`<option value="${s.id}">${esc(s.name)}</option>`).join('');if(select)$('save').value=select;await refresh()}async function refresh(){if(!sid())return;let d=await api('/api/dashboard?save_id='+sid());$('countGames').textContent=d.games.length;$('countEvents').textContent=d.events.length;$('countStories').textContent=d.story_count;$('games').innerHTML=d.games.map(g=>`<p>${esc(g.game_date)} · ${esc(g.away)} ${g.away_runs} : ${g.home_runs} ${esc(g.home)}</p>`).join('')||'저장된 경기가 없습니다';$('events').innerHTML=d.events.map(e=>`<p><b>[${esc(e.importance)}]</b> ${esc(e.event_date)} ${esc(e.headline)}</p>`).join('')||'저장된 사건이 없습니다'}async function run(f){try{await f();msg('저장되었습니다.');await refresh()}catch(e){msg('오류: '+e.message)}}async function newSave(){await run(async()=>{let d=await api('/api/saves','POST',{name:val('saveName')});await loadSaves(d.id)})}async function addGame(){await run(()=>api('/api/games','POST',{save_id:sid(),game_date:val('date'),away:val('away'),home:val('home'),away_runs:Number(val('ar')),home_runs:Number(val('hr')),external_id:val('gid')||null,innings:val('innings')?JSON.parse(val('innings')):{}}))}async function addEvent(){await run(()=>api('/api/events','POST',{save_id:sid(),event_date:val('edate'),importance:val('grade'),event_type:val('etype'),headline:val('headline'),detail:val('detail')}))}async function addRoster(){await run(()=>api('/api/roster','POST',{save_id:sid(),name:val('player'),team:val('rteam'),effective_date:val('rdate'),move_type:val('move'),note:val('rnote')}))}function dl(name,data,type){let a=document.createElement('a'),u=URL.createObjectURL(new Blob([data],{type}));a.href=u;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(u),1000)}async function downloadBackup(){let d=await api('/api/backup?save_id='+sid());dl('kbo-universe-save-'+sid()+'.json',JSON.stringify(d,null,2),'application/json')}async function downloadContext(){let d=await api('/api/context?save_id='+sid());dl('kbo-universe-context-'+sid()+'.md',d.text,'text/markdown')}async function restoreBackup(){try{let d=JSON.parse(await $('restore').files[0].text()),r=await api('/api/restore','POST',d);await loadSaves(r.id);msg('복원 완료. 원본 세이브는 유지되었습니다.')}catch(e){msg('복원 오류: '+e.message)}}$('save').onchange=refresh;loadSaves();</script></html>'''
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  try:
   from urllib.parse import parse_qs
   p=urlparse(self.path);q=parse_qs(p.query);sid=int(q.get('save_id',[1])[0])
   with connect() as c:
    if p.path=='/':return json_response(self,200,HTML.encode(),'text/html; charset=utf-8')
    if p.path=='/api/saves':return json_response(self,200,[dict(x) for x in c.execute('SELECT * FROM saves ORDER BY id DESC')])
    if p.path=='/api/backup':return json_response(self,200,snapshot(c,sid))
    if p.path in ('/api/dashboard','/api/context'):
     games=[dict(x) for x in c.execute('SELECT g.*,a.name away,h.name home FROM games g JOIN teams a ON a.id=g.away_team_id JOIN teams h ON h.id=g.home_team_id WHERE g.save_id=? ORDER BY game_date DESC,id DESC LIMIT 100',(sid,))]
     events=[dict(x) for x in c.execute('SELECT * FROM events WHERE save_id=? ORDER BY event_date DESC,id DESC LIMIT 100',(sid,))]
     moves=[dict(x) for x in c.execute('SELECT r.*,p.name player,t.name team FROM roster_moves r JOIN players p ON p.id=r.player_id LEFT JOIN teams t ON t.id=r.team_id WHERE r.save_id=? ORDER BY effective_date DESC,r.id DESC LIMIT 100',(sid,))]
     count=c.execute('SELECT COUNT(*) FROM stories WHERE save_id=?',(sid,)).fetchone()[0]
     if p.path=='/api/dashboard':return json_response(self,200,{'games':games,'events':events,'moves':moves,'story_count':count})
     settings=c.execute('SELECT rules_json FROM settings WHERE save_id=?',(sid,)).fetchone()
     lines=['# KBO UNIVERSE 2.0 — ChatGPT 인계 문서','', '## 불변 규칙',json.dumps(json.loads(settings[0]) if settings else DEFAULT_RULES,ensure_ascii=False,indent=2),'','## 확정 경기 기록']
     lines += [f"- {g['game_date']} {g['away']} {g['away_runs']}:{g['home_runs']} {g['home']} (OOTP ID: {g['external_id'] or '미기록'})" for g in games]
     lines+=['','## 사건 타임라인']+[f"- {e['event_date']} [{e['importance']}] {e['event_type']}: {e['headline']} — {e['detail']} (상태: {e['fact_status']})" for e in events]
     lines+=['','## 로스터 변동']+[f"- {r['effective_date']} {r['player']} / {r['team'] or '미지정'} / {r['move_type']} / {r['note'] or ''}" for r in moves]
     lines+=['','## 생성 지침','확정된 데이터 외 선수 성적·경기 결과·트레이드·부상·순위를 만들어내지 않는다. 가상 인터뷰와 팬 반응은 창작으로 표시한다. 누락된 자료는 미확인으로 표기한다.']
     return json_response(self,200,{'text':'\n'.join(lines)})
   return json_response(self,404,{'error':'경로 없음'})
  except Exception as e:return json_response(self,400,{'error':str(e)})
 def do_POST(self):
  try:
   size=int(self.headers.get('Content-Length','0'))
   if size>8_000_000:raise ValueError('최대 8MB')
   d=json.loads(self.rfile.read(size));p=urlparse(self.path).path
   with connect() as c:
    if p=='/api/saves':
     name=str(d['name']).strip();assert name
     c.execute('INSERT INTO saves(name) VALUES(?)',(name,));sid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
     c.execute('INSERT INTO settings VALUES(?,?)',(sid,json.dumps(DEFAULT_RULES,ensure_ascii=False)))
     return json_response(self,200,{'id':sid})
    if p=='/api/restore':return json_response(self,200,{'id':restore(c,d)})
    sid=int(d['save_id'])
    if not c.execute('SELECT 1 FROM saves WHERE id=?',(sid,)).fetchone():raise ValueError('세이브 없음')
    if p=='/api/games':
     validate_date(d['game_date']);away=str(d['away']).strip();home=str(d['home']).strip()
     if not away or not home or away==home:raise ValueError('원정/홈 팀을 확인하세요')
     ar=int(d['away_runs']);hr=int(d['home_runs']);assert ar>=0 and hr>=0
     innings=d.get('innings',{})
     if not isinstance(innings,dict):raise ValueError('이닝별 기록은 JSON 객체여야 합니다')
     for k,score in [('away',ar),('home',hr)]:
      if k in innings and (not isinstance(innings[k],list) or any(type(n)!=int or n<0 for n in innings[k]) or sum(innings[k])!=score):raise ValueError(k+' 이닝별 득점 합계 불일치')
     c.execute('INSERT INTO games(save_id,external_id,game_date,away_team_id,home_team_id,away_runs,home_runs,innings_json) VALUES(?,?,?,?,?,?,?,?)',(sid,d.get('external_id'),d['game_date'],ensure_team(c,sid,away),ensure_team(c,sid,home),ar,hr,json.dumps(innings,ensure_ascii=False)))
    elif p=='/api/events':
     validate_date(d['event_date']);assert d['importance'] in 'SABC';assert str(d['headline']).strip()
     c.execute('INSERT INTO events(save_id,event_date,event_type,importance,headline,detail) VALUES(?,?,?,?,?,?)',(sid,d['event_date'],d['event_type'],d['importance'],d['headline'],d.get('detail','')))
    elif p=='/api/roster':
     validate_date(d['effective_date']);assert str(d['name']).strip()
     pid=ensure_player(c,sid,d['name'].strip());tid=ensure_team(c,sid,d['team'].strip()) if d.get('team','').strip() else None
     c.execute('INSERT INTO roster_moves(save_id,player_id,team_id,effective_date,move_type,note) VALUES(?,?,?,?,?,?)',(sid,pid,tid,d['effective_date'],d['move_type'],d.get('note','')))
    else:return json_response(self,404,{'error':'경로 없음'})
    return json_response(self,200,{'ok':True})
  except Exception as e:return json_response(self,400,{'error':str(e)})
if __name__=='__main__':
 initialize()
 try:
  server=ThreadingHTTPServer(('127.0.0.1',8765),Handler)
 except OSError as exc:
  print('서버 시작 실패: 포트 8765를 확인하세요.', exc)
  input('종료하려면 Enter를 누르세요...')
  sys.exit(1)
 url='http://127.0.0.1:8765'
 print('KBO UNIVERSE 실행 중:',url,flush=True)
 print('이 창을 닫으면 프로그램이 종료됩니다.',flush=True)
 threading.Timer(0.8,lambda:webbrowser.open(url)).start()
 try:server.serve_forever()
 except KeyboardInterrupt:pass
 finally:server.server_close()
