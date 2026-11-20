import json, sqlite3, datetime, pathlib, zipfile, io, html, sys, threading, webbrowser, re, hashlib
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

MONTHS={'January':1,'February':2,'March':3,'April':4,'May':5,'June':6,'July':7,'August':8,'September':9,'October':10,'November':11,'December':12}
def parse_ootp_log(raw):
 """Conservative parser: no inference from unverified lines."""
 if not isinstance(raw,str) or len(raw)>400000:raise ValueError('로그는 400KB 이하의 텍스트여야 합니다')
 text=raw.replace('\r','').replace('\xa0',' ').replace('：',':')
 match=re.search(r'(?:게임|Game)\s*ID\s*[:：]\s*(\d+)',text,re.I)
 game_id=match.group(1) if match else None
 dt=re.search(r'(?<!\d)(20\d{2})[-./]\s*(\d{1,2})[-./]\s*(\d{1,2})(?!\d)',text)
 if dt:
  y,m,d=map(int,dt.groups())
 else:
  dt=re.search(r'(20\d{2})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일',text)
  if dt:y,m,d=map(int,dt.groups())
  else:
   dt=re.search(r'([A-Za-z]+)\s+(\d{1,2}),?\s+(20\d{2})',text)
   if not dt or dt.group(1).title() not in MONTHS:
    dt2=re.search(r'(\d{1,2})\s*월\s*(\d{1,2})\s*일[^\n]{0,20}?(20\d{2})',text)
    if not dt2:raise ValueError('경기 날짜를 인식하지 못했습니다')
    m,d,y=map(int,dt2.groups())
   else:
    m=MONTHS[dt.group(1).title()];d=int(dt.group(2));y=int(dt.group(3))
 date=datetime.date(y,m,d).isoformat()
 lines=[re.split(r'\t+| {2,}',x.strip()) for x in text.splitlines() if x.strip()]
 candidates=[]
 for parts in lines:
  if len(parts)<3:continue
  name=parts[0].strip()
  if not re.search(r'[가-힣A-Za-z]',name) or re.search(r'^(?:팀|선수|이닝|기록|Total|Totals|R|H|E|Game|경기)',name,re.I):continue
  # OOTP score table has 9+ inning columns, followed by R/H/E.
  numeric=[]
  for token in parts[1:]:
   if re.fullmatch(r'\d+',token):numeric.append(int(token))
   else:break
  if len(numeric)<6:continue
  # For standard 9-inning games R/H/E are the last three numbers.
  if len(numeric)<9:continue
  innings=numeric[:-3];runs,hits,errors=numeric[-3:]
  if not (8<=len(innings)<=30 and sum(innings)==runs):continue
  candidates.append({'team':name,'runs':runs,'hits':hits,'errors':errors,'innings':innings})
 if len(candidates)<2:
  raise ValueError('이닝별 스코어보드 2개 행을 찾지 못했습니다. OOTP의 이닝별 R/H/E 표 전체를 복사해 주세요.')
 # Prefer first pair in source order; reject ambiguous consecutive tables.
 away,home=candidates[:2]
 if away['team']==home['team']:raise ValueError('두 팀 이름이 같습니다')
 return {'game_date':date,'external_id':game_id,'away':away['team'],'home':home['team'],
         'away_runs':away['runs'],'home_runs':home['runs'],
         'away_hits':away['hits'],'home_hits':home['hits'],
         'away_errors':away['errors'],'home_errors':home['errors'],
         'innings':{'away':away['innings'],'home':home['innings']},
         'warnings':([] if game_id else ['OOTP 경기 ID가 없어 날짜·팀 기준 중복 검사만 적용됩니다.'])}

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
HTML='<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>KBO UNIVERSE 2.0 · Front Office</title><style>\n:root{color-scheme:dark;--bg:#0a101c;--panel:#151f30;--panel2:#1c293d;--border:#304057;--muted:#9dafc7;--text:#eaf0fa;--blue:#5194f7;--red:#e94f61;--green:#58d5a0}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 system-ui,-apple-system,\'Malgun Gothic\',sans-serif}button,input,textarea,select{font:inherit}button{cursor:pointer}aside{width:246px;background:#111a29;border-right:1px solid var(--border);position:fixed;top:0;bottom:0;padding:23px 15px;display:flex;flex-direction:column;gap:12px;overflow:auto}.brand{font-size:19px;font-weight:850;letter-spacing:.2px;padding:5px 9px 18px}.brand span{color:var(--blue)}.tiny{font-size:11px;color:var(--muted);font-weight:500}.nav{border:0;background:transparent;color:#c2cee0;padding:13px 14px;text-align:left;border-radius:9px;font-weight:650}.nav:hover,.nav.active{background:#263954;color:#fff}.nav.active{border-left:3px solid var(--blue)}.nav small{float:right;color:var(--muted)}.sidefoot{margin-top:auto;border-top:1px solid var(--border);padding:18px 9px;color:var(--muted);font-size:12px}main{margin-left:246px;min-height:100vh}header{height:78px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;padding:0 35px;background:#101929;gap:16px}.heading{font-size:22px;font-weight:800}.sub{color:var(--muted);font-size:12px}.wrap{padding:26px 34px;max-width:1500px;margin:auto}.toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.toolbar select{width:230px}.btn{background:#286ed1;color:white;border:1px solid #3c7edb;border-radius:8px;padding:10px 14px;font-weight:700}.btn.secondary{background:#24354c;border-color:#3c526f}.btn.danger{background:#733242;border-color:#91475a}.btn:hover{filter:brightness(1.13)}.grid{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));gap:16px}.span3{grid-column:span 3}.span4{grid-column:span 4}.span5{grid-column:span 5}.span6{grid-column:span 6}.span7{grid-column:span 7}.span8{grid-column:span 8}.span12{grid-column:span 12}.panel{background:var(--panel);border:1px solid var(--border);border-radius:13px;padding:21px;min-width:0}.panel h2{font-size:16px;margin:0 0 16px}.panel h3{font-size:14px;margin:0 0 12px}.metric{font-size:30px;font-weight:850;margin:8px 0 1px}.metric-note{color:var(--muted);font-size:12px}.eyebrow{font-size:11px;color:var(--muted);letter-spacing:1px;font-weight:750;text-transform:uppercase}.hero{background:linear-gradient(120deg,#192d4d,#172033 70%);border:1px solid #395173;border-radius:15px;padding:28px 30px;display:flex;justify-content:space-between;align-items:center;gap:16px;margin-bottom:16px}.hero h1{font-size:29px;margin:4px 0}.hero p{color:#afc5e1;margin:0}.emblem{font-size:52px;opacity:.9}.empty{padding:22px 8px;color:var(--muted);text-align:center;border:1px dashed #3c4b60;border-radius:8px}table{border-collapse:collapse;width:100%;font-size:13px}th{text-align:left;color:#91a7c1;font-size:11px;font-weight:700;text-transform:uppercase}th,td{padding:11px 9px;border-bottom:1px solid #2a394e}tr:last-child td{border:0}td{overflow-wrap:anywhere}.tag{display:inline-block;padding:2px 8px;border-radius:5px;background:#2c405a;font-size:11px;font-weight:750}.tag.S{background:#6b3445;color:#ffb8c4}.tag.A{background:#6b4d2e;color:#ffd18c}.tag.B{background:#244f65;color:#9bdbff}.tag.C{background:#2a4250;color:#aabecb}label{display:block;font-size:12px;color:#b9c9df;margin:0 0 5px}input,select,textarea{width:100%;border:1px solid #42536b;background:#0d1727;color:#f2f5fc;border-radius:8px;padding:10px 11px;min-width:0}textarea{min-height:96px;resize:vertical}input:focus,textarea:focus,select:focus{outline:2px solid #437cbd}input::placeholder,textarea::placeholder{color:#72839b}.field{margin-bottom:13px}.formgrid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.notice{background:#1b3048;border:1px solid #38567a;color:#c9def7;padding:12px 15px;border-radius:8px;margin-bottom:17px}.hidden{display:none!important}.section{display:none}.section.active{display:block}.sectionhead{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:18px}.sectionhead h1{font-size:23px;margin:0}.sectionhead p{margin:2px 0;color:var(--muted)}.activity{padding:11px 0;border-bottom:1px solid #2b3a50}.activity:last-child{border:0}.activity b{font-size:13px}.activity .sub{margin-top:3px}.callout{padding:15px;border-left:3px solid var(--blue);background:#1a2a40;border-radius:5px;color:#cbdcf2}.right{text-align:right}.mono{font-variant-numeric:tabular-nums}.good{color:var(--green)}.bad{color:#ff8795}#toast{position:fixed;right:20px;bottom:22px;z-index:20;background:#204f41;border:1px solid #40886d;padding:13px 20px;border-radius:9px;box-shadow:0 10px 30px #0008;max-width:420px}#toast.error{background:#672c38;border-color:#ae5363}.muted{color:var(--muted)}.pills{display:flex;gap:8px;flex-wrap:wrap}.pill{border:1px solid #3b5371;background:#1d304a;color:#d1e0f5;border-radius:20px;padding:5px 10px;font-size:12px}code{background:#0b1625;padding:2px 5px;border-radius:3px}a{color:#9ac4ff}@media(max-width:1050px){.span3{grid-column:span 6}.span4,.span5,.span6,.span7,.span8{grid-column:span 12}}@media(max-width:700px){aside{position:static;width:auto;flex-direction:row;flex-wrap:wrap;padding:10px;gap:5px}.brand{width:100%;padding:4px}.nav{padding:8px}.sidefoot{display:none}main{margin:0}header{height:auto;padding:14px;flex-wrap:wrap}.wrap{padding:14px}.span3{grid-column:span 12}.formgrid{grid-template-columns:1fr}.hero{padding:18px}.hero h1{font-size:22px}}\n</style></head><body>\n<aside><div class="brand">⚾ KBO <span>UNIVERSE</span><div class="tiny">FRONT OFFICE · v0.3</div></div><button class="nav active" data-page="dashboard">▦ &nbsp; 운영 대시보드</button><button class="nav" data-page="games">▤ &nbsp; 경기 센터</button><button class="nav" data-page="roster">♟ &nbsp; 선수단 / 로스터</button><button class="nav" data-page="events">◇ &nbsp; 세계관 연혁</button><button class="nav" data-page="media">▣ &nbsp; 스포츠 미디어</button><button class="nav" data-page="saves">▥ &nbsp; 세이브 관리</button><div class="sidefoot">● 로컬 데이터베이스<br>OOTP 기록만 사실로 확정<br><br>AI 콘텐츠는 가상으로 구분</div></aside>\n<main><header><div><div class="heading" id="topTitle">구단 운영 대시보드</div><div class="sub">OOTP 23 · 장기 세이브 운영 시스템</div></div><div class="toolbar"><select id="save" aria-label="활성 세이브"></select><button class="btn secondary" onclick="refresh()">↻ 새로고침</button></div></header><div class="wrap">\n<section id="dashboard" class="section active"><div class="hero"><div><div class="eyebrow">KBO UNIVERSE · SEASON HUB</div><h1 id="heroTeam">KIA 타이거즈</h1><p id="heroSave">세이브 정보를 불러오는 중...</p></div><div class="emblem">⚾</div></div><div class="grid"><div class="panel span3"><div class="eyebrow">저장된 경기</div><div class="metric mono" id="mGames">0</div><div class="metric-note">전체 경기 기록</div></div><div class="panel span3"><div class="eyebrow">세계관 사건</div><div class="metric mono" id="mEvents">0</div><div class="metric-note">등급별 사건 누적</div></div><div class="panel span3"><div class="eyebrow">로스터 변동</div><div class="metric mono" id="mMoves">0</div><div class="metric-note">콜업 · 말소 · 트레이드</div></div><div class="panel span3"><div class="eyebrow">보관된 기사</div><div class="metric mono" id="mStories">0</div><div class="metric-note">창작 콘텐츠 별도 보관</div></div><div class="panel span7"><div class="sectionhead"><div><h2>최근 경기</h2><p>실제 입력된 OOTP 경기 결과</p></div><button class="btn secondary" onclick="page(\'games\')">경기 센터 →</button></div><div id="dashGames"></div></div><div class="panel span5"><div class="sectionhead"><div><h2>세계관 주요 뉴스</h2><p>확정된 사건 중심 타임라인</p></div><button class="btn secondary" onclick="page(\'events\')">전체 보기 →</button></div><div id="dashEvents"></div></div><div class="panel span6"><h2>최근 로스터 이동</h2><div id="dashMoves"></div></div><div class="panel span6"><h2>운영 규칙</h2><div class="callout">확정된 경기 결과와 선수 기록은 OOTP 데이터만 사용한다. 인터뷰·팬 반응은 가상 콘텐츠로 표시하며, 새 대화는 인계 문서로 복원한다.</div><div class="pills" style="margin-top:15px"><span class="pill">SQLite 영구 저장</span><span class="pill">JSON 백업</span><span class="pill">사건 등급 S/A/B/C</span></div></div></div></section>\n<section id="games" class="section"><div class="sectionhead"><div><h1>경기 센터</h1><p>경기 입력 · 스코어보드 · OOTP ID 중복 방지</p></div></div><div class="grid"><div class="panel span12"><h2>⚡ OOTP 경기 로그 자동 가져오기 <span class="tag B">v0.3</span></h2><p class="sub">날짜, 팀, R/H/E, 이닝별 득점과 경기 ID를 추출합니다. 미리보기 확인 후 저장하세요.</p><div class="field"><label>OOTP 원본 경기 로그</label><textarea id="rawLog" style="min-height:175px" placeholder="OOTP의 경기 로그와 이닝별 R/H/E 표를 붙여 넣으세요."></textarea></div><div class="toolbar"><button class="btn secondary" type="button" onclick="previewLog()">로그 분석 · 미리보기</button><button class="btn" id="importLogBtn" type="button" onclick="importLog()" disabled>확인한 경기 저장</button><span class="sub" id="importStatus">분석 전</span></div><div id="importPreview" style="margin-top:16px"></div></div><div class="panel span5"><h2>경기 결과 등록</h2><form id="gameForm"><div class="field"><label>경기 날짜</label><input name="game_date" type="date" required></div><div class="formgrid"><div class="field"><label>원정팀</label><input name="away" placeholder="KIA 타이거즈" required></div><div class="field"><label>홈팀</label><input name="home" placeholder="한화 이글스" required></div><div class="field"><label>원정 득점</label><input name="away_runs" type="number" min="0" required></div><div class="field"><label>홈 득점</label><input name="home_runs" type="number" min="0" required></div></div><div class="field"><label>OOTP 경기 ID (권장)</label><input name="external_id" placeholder="예: 12948"></div><div class="field"><label>이닝별 점수 JSON (선택)</label><textarea name="innings" placeholder=\'{"away":[0,1,0],"home":[0,0,2]}\'></textarea></div><button class="btn" type="submit">경기 저장</button></form></div><div class="panel span7"><h2>전체 경기 기록 <span class="tiny">(최근 100건 표시)</span></h2><div id="gameTable"></div></div></div></section>\n<section id="roster" class="section"><div class="sectionhead"><div><h1>선수단 · 로스터</h1><p>선수 등록 이력과 소속 변경 추적</p></div></div><div class="grid"><div class="panel span5"><h2>로스터 이동 등록</h2><form id="rosterForm"><div class="formgrid"><div class="field"><label>선수 이름</label><input name="name" required placeholder="선수 이름"></div><div class="field"><label>소속 구단</label><input name="team" placeholder="KIA 타이거즈"></div><div class="field"><label>적용 날짜</label><input name="effective_date" type="date" required></div><div class="field"><label>이동 유형</label><select name="move_type"><option>콜업</option><option>말소</option><option>트레이드</option><option>영입</option><option>방출</option><option>부상자 명단</option><option>복귀</option></select></div></div><div class="field"><label>이동 사유 및 추가 기록</label><textarea name="note" placeholder="OOTP에서 확인한 내용"></textarea></div><button class="btn" type="submit">로스터 이동 저장</button></form><div class="notice" style="margin-top:15px">선수 이름으로 등록하는 초기 버전입니다. 동명이인 구분과 실제 1군 명단 자동 산출은 추후 선수 ID 기반으로 개선할 예정입니다.</div></div><div class="panel span7"><h2>등록·말소 이력 <span class="tiny">(최근 100건 표시)</span></h2><div id="rosterTable"></div></div><div class="panel span12"><h2>선수 목록</h2><div id="playerTable"></div></div></div></section>\n<section id="events" class="section"><div class="sectionhead"><div><h1>세계관 연혁</h1><p>트레이드 · 부상 · 계약 · 기록 · 라이벌 서사</p></div></div><div class="grid"><div class="panel span5"><h2>새 사건 등록</h2><form id="eventForm"><div class="formgrid"><div class="field"><label>사건 날짜</label><input name="event_date" type="date" required></div><div class="field"><label>중요도</label><select name="importance"><option>S</option><option>A</option><option>B</option><option selected>C</option></select></div></div><div class="field"><label>사건 유형</label><select name="event_type"><option>트레이드</option><option>부상</option><option>등록·말소</option><option>계약</option><option>유망주</option><option>기록</option><option>기타</option></select></div><div class="field"><label>제목</label><input name="headline" required placeholder="사건 제목"></div><div class="field"><label>확인된 사실</label><textarea name="detail" placeholder="출처, 날짜, 선수 및 구단 관련 사실"></textarea></div><button class="btn" type="submit">사건 저장</button></form></div><div class="panel span7"><h2>사건 타임라인 <span class="tiny">(최근 100건 표시)</span></h2><div id="eventTimeline"></div></div></div></section>\n<section id="media" class="section"><div class="sectionhead"><div><h1>스포츠 미디어 센터</h1><p>기사 · 가상 인터뷰 · 팬 반응 보관소</p></div></div><div class="notice">AI 기사 자동 생성은 아직 연결되지 않았습니다. 현재는 ChatGPT에서 생성한 원고를 여기에 저장하고, 사실과 창작을 분리할 수 있습니다.</div><div class="grid"><div class="panel span5"><h2>기사 / 창작 콘텐츠 보관</h2><form id="storyForm"><div class="field"><label>콘텐츠 종류</label><select name="story_type"><option>경기 기사</option><option>특집 기사</option><option>가상 인터뷰</option><option>가상 팬 반응</option><option>시즌 연감</option></select></div><div class="field"><label>제목</label><input name="title" required placeholder="기사 제목"></div><div class="field"><label>본문</label><textarea name="body" required style="min-height:200px" placeholder="생성한 기사나 인터뷰 내용을 붙여 넣으세요"></textarea></div><button class="btn" type="submit">콘텐츠 저장</button></form></div><div class="panel span7"><h2>저장된 기사 <span class="tiny">(최근 100건 표시)</span></h2><div id="storyList"></div></div></div></section>\n<section id="saves" class="section"><div class="sectionhead"><div><h1>세이브 · 백업 관리</h1><p>장기 세계관 보존 및 ChatGPT 인계</p></div></div><div class="grid"><div class="panel span6"><h2>새로운 세계관 만들기</h2><form id="saveForm"><div class="field"><label>새 세이브 이름</label><input name="name" required placeholder="예: KIA 타이거즈 2022 새로운 역사"></div><button class="btn" type="submit">새 세이브 생성</button></form><div class="callout" style="margin-top:16px">새 세이브는 기존 기록과 분리됩니다. 다른 OOTP 세이브의 경기나 선수 서사가 자동으로 섞이지 않습니다.</div></div><div class="panel span6"><h2>백업 / 복원</h2><div class="field"><button class="btn" onclick="downloadBackup()">↓ 전체 JSON 백업 다운로드</button></div><div class="field"><button class="btn secondary" onclick="downloadContext()">↓ ChatGPT 인계 문서 다운로드</button></div><div class="field"><label>JSON 백업 파일 선택</label><input id="restoreFile" type="file" accept=".json,application/json"></div><button class="btn secondary" onclick="restoreBackup()">선택한 백업 복원 (새 세이브 생성)</button></div><div class="panel span12"><h2>데이터 보존 안내</h2><p class="muted">프로그램과 같은 폴더의 <code>universe.db</code>가 원본 데이터베이스입니다. EXE를 교체해도 같은 폴더에서 실행하면 기존 기록을 사용할 수 있습니다. 업그레이드 전에는 반드시 JSON 백업과 DB 파일 사본을 별도로 보관하세요.</p><p class="muted">ChatGPT 인계 문서는 최근 100건씩의 경기·사건·로스터 이동을 포함합니다. 전체 데이터 보존은 JSON 백업을 사용하세요.</p></div></div></section>\n</div></main><div id="toast" class="hidden" role="status"></div><script>\nconst $=id=>document.getElementById(id),esc=s=>String(s??\'\').replace(/[&<>"\']/g,c=>({\'&\':\'&amp;\',\'<\':\'&lt;\',\'>\':\'&gt;\',\'"\':\'&quot;\',"\'":\'&#39;\'}[c]));let dashboard={},currentPage=\'dashboard\',toastTimer;const titles={dashboard:\'구단 운영 대시보드\',games:\'경기 센터\',roster:\'선수단 · 로스터\',events:\'세계관 연혁\',media:\'스포츠 미디어\',saves:\'세이브 관리\'};function page(p){currentPage=p;document.querySelectorAll(\'.section\').forEach(e=>e.classList.toggle(\'active\',e.id===p));document.querySelectorAll(\'.nav\').forEach(e=>e.classList.toggle(\'active\',e.dataset.page===p));$(\'topTitle\').textContent=titles[p];window.scrollTo(0,0)}document.querySelectorAll(\'.nav\').forEach(b=>b.onclick=()=>page(b.dataset.page));function notify(message,error=false){let e=$(\'toast\');e.textContent=message;e.className=error?\'error\':\'\';clearTimeout(toastTimer);toastTimer=setTimeout(()=>e.classList.add(\'hidden\'),4800)}async function api(path,method=\'GET\',data){let r=await fetch(path,{method,headers:{\'Content-Type\':\'application/json\'},body:data===undefined?undefined:JSON.stringify(data)});let j=await r.json();if(!r.ok)throw Error(j.error||\'요청 실패\');return j}function sid(){return Number($(\'save\').value)}function table(headers,rows){if(!rows.length)return \'<div class="empty">저장된 기록이 없습니다.</div>\';return \'<div style="overflow-x:auto"><table><thead><tr>\'+headers.map(h=>\'<th>\'+h+\'</th>\').join(\'\')+\'</tr></thead><tbody>\'+rows.map(row=>\'<tr>\'+row.map(x=>\'<td>\'+x+\'</td>\').join(\'\')+\'</tr>\').join(\'\')+\'</tbody></table></div>\'}function fmtGame(g){return `${esc(g.away)} <b>${g.away_runs} : ${g.home_runs}</b> ${esc(g.home)}`}function tag(g){return `<span class="tag ${esc(g)}">${esc(g)}</span>`}function activity(items,render){return items.length?items.map(render).join(\'\'):\'<div class="empty">아직 기록이 없습니다.</div>\'}async function loadSaves(want){let saves=await api(\'/api/saves\');$(\'save\').innerHTML=saves.map(s=>`<option value="${s.id}">${esc(s.name)}</option>`).join(\'\');if(want&&saves.some(s=>s.id===want))$(\'save\').value=want;await refresh()}async function refresh(){if(!sid())return;try{dashboard=await api(\'/api/dashboard?save_id=\'+sid());render()}catch(e){notify(\'불러오기 실패: \'+e.message,true)}}function render(){let d=dashboard,g=d.games||[],e=d.events||[],m=d.moves||[],s=d.stories||[],p=d.players||[],c=d.counts||{};let saveName=$(\'save\').selectedOptions[0]?.textContent||\'세이브\';$(\'heroTeam\').textContent=d.tracked_team||\'KIA 타이거즈\';$(\'heroSave\').textContent=saveName+\' · 마지막 기록 \'+(g[0]?.game_date||e[0]?.event_date||\'아직 없음\');[[\'mGames\',\'games\'],[\'mEvents\',\'events\'],[\'mMoves\',\'moves\'],[\'mStories\',\'stories\']].forEach(([id,key])=>$(id).textContent=c[key]??0);$(\'dashGames\').innerHTML=table([\'날짜\',\'경기 결과\'],g.slice(0,7).map(x=>[esc(x.game_date),fmtGame(x)]));$(\'dashEvents\').innerHTML=activity(e.slice(0,5),x=>`<div class="activity">${tag(x.importance)} <b>${esc(x.headline)}</b><div class="sub">${esc(x.event_date)} · ${esc(x.event_type)}</div></div>`);$(\'dashMoves\').innerHTML=table([\'날짜\',\'선수\',\'이동\'],m.slice(0,6).map(x=>[esc(x.effective_date),esc(x.player),esc(x.move_type)]));$(\'gameTable\').innerHTML=table([\'날짜\',\'원정\',\'점수\',\'홈\',\'OOTP ID\'],g.map(x=>[esc(x.game_date),esc(x.away),`<b class="mono">${x.away_runs} : ${x.home_runs}</b>`,esc(x.home),esc(x.external_id||\'—\')]));$(\'rosterTable\').innerHTML=table([\'날짜\',\'선수\',\'구단\',\'이동\',\'설명\'],m.map(x=>[esc(x.effective_date),esc(x.player),esc(x.team||\'—\'),esc(x.move_type),esc(x.note||\'—\')]));$(\'playerTable\').innerHTML=table([\'선수\',\'포지션\',\'OOTP ID\'],p.map(x=>[esc(x.name),esc(x.position||\'미등록\'),esc(x.external_id||\'미등록\')]));$(\'eventTimeline\').innerHTML=activity(e,x=>`<div class="activity">${tag(x.importance)} <b>${esc(x.headline)}</b><div class="sub">${esc(x.event_date)} · ${esc(x.event_type)} · ${esc(x.fact_status)}</div><div style="white-space:pre-wrap;margin-top:6px">${esc(x.detail)}</div></div>`);$(\'storyList\').innerHTML=activity(s,x=>`<details class="activity"><summary><b>${esc(x.title)}</b> <span class="tag">${esc(x.story_type)}</span></summary><div class="sub">${esc(x.created_at)} · ${x.fictional?\'가상 콘텐츠\':\'확정 자료\'}</div><div style="white-space:pre-wrap;margin-top:10px">${esc(x.body)}</div></details>`)}function fields(form){return Object.fromEntries(new FormData(form).entries())}async function submit(form,endpoint,transform){let d=fields(form);d.save_id=sid();if(transform)transform(d);try{await api(endpoint,\'POST\',d);form.reset();notify(\'정상적으로 저장되었습니다.\');await refresh()}catch(e){notify(\'저장 실패: \'+e.message,true)}}$(\'gameForm\').onsubmit=e=>{e.preventDefault();submit(e.target,\'/api/games\',d=>{if(d.away_runs===\'\'||d.home_runs===\'\')throw Error(\'점수를 입력하세요\');d.away_runs=Number(d.away_runs);d.home_runs=Number(d.home_runs);d.external_id=d.external_id.trim()||null;d.innings=d.innings.trim()?JSON.parse(d.innings):{}})};$(\'eventForm\').onsubmit=e=>{e.preventDefault();submit(e.target,\'/api/events\')};$(\'rosterForm\').onsubmit=e=>{e.preventDefault();submit(e.target,\'/api/roster\')};$(\'storyForm\').onsubmit=e=>{e.preventDefault();submit(e.target,\'/api/stories\')};$(\'saveForm\').onsubmit=async e=>{e.preventDefault();try{let r=await api(\'/api/saves\',\'POST\',{name:fields(e.target).name});e.target.reset();await loadSaves(r.id);notify(\'새 세이브를 생성했습니다.\');page(\'dashboard\')}catch(x){notify(x.message,true)}};$(\'save\').onchange=refresh;function dl(name,data,type){let a=document.createElement(\'a\'),u=URL.createObjectURL(new Blob([data],{type}));a.href=u;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(u),1000)}async function downloadBackup(){try{let d=await api(\'/api/backup?save_id=\'+sid());dl(\'kbo-universe-save-\'+sid()+\'.json\',JSON.stringify(d,null,2),\'application/json\');notify(\'JSON 백업 다운로드 완료\')}catch(e){notify(e.message,true)}}async function downloadContext(){try{let d=await api(\'/api/context?save_id=\'+sid());dl(\'kbo-universe-context-\'+sid()+\'.md\',d.text,\'text/markdown\');notify(\'인계 문서 다운로드 완료\')}catch(e){notify(e.message,true)}}async function restoreBackup(){try{let file=$(\'restoreFile\').files[0];if(!file)throw Error(\'백업 파일을 먼저 선택하세요\');let d=JSON.parse(await file.text()),r=await api(\'/api/restore\',\'POST\',d);await loadSaves(r.id);notify(\'새 세이브로 복원했습니다. 기존 세이브는 유지됩니다.\');page(\'dashboard\')}catch(e){notify(\'복원 실패: \'+e.message,true)}}let parsedLog=null;\nasync function previewLog(){parsedLog=null;$(\'importLogBtn\').disabled=true;$(\'importPreview\').innerHTML=\'\';try{let r=await api(\'/api/parse-log\',\'POST\',{raw:$(\'rawLog\').value});parsedLog=r;$(\'importStatus\').textContent=\'분석 완료 · 저장 전\';$(\'importPreview\').innerHTML=\'<div class="callout"><b>\'+esc(r.game_date)+\' · \'+esc(r.away)+\' \'+r.away_runs+\' : \'+r.home_runs+\' \'+esc(r.home)+\'</b><p>OOTP ID: \'+esc(r.external_id||\'없음\')+\' · R/H/E: \'+r.away_runs+\'/\'+r.away_hits+\'/\'+r.away_errors+\' : \'+r.home_runs+\'/\'+r.home_hits+\'/\'+r.home_errors+\'</p><p>원정 이닝: \'+esc(r.innings.away.join(\' | \'))+\'</p><p>홈 이닝: \'+esc(r.innings.home.join(\' | \'))+\'</p><p>\'+esc(r.warnings.join(\' \'))+\'</p></div>\';$(\'importLogBtn\').disabled=false}catch(e){$(\'importStatus\').textContent=\'분석 실패\';notify(e.message,true)}}\nasync function importLog(){if(!parsedLog)return;try{await api(\'/api/import-log\',\'POST\',{save_id:sid(),raw:$(\'rawLog\').value});notify(\'OOTP 경기 저장 완료\');parsedLog=null;$(\'importLogBtn\').disabled=true;$(\'importStatus\').textContent=\'저장 완료\';await refresh()}catch(e){notify(\'가져오기 실패: \'+e.message,true)}}\n$(\'rawLog\').addEventListener(\'input\',()=>{parsedLog=null;$(\'importLogBtn\').disabled=true;$(\'importStatus\').textContent=\'로그가 변경되었습니다. 다시 분석하세요.\'});\nloadSaves().catch(e=>notify(e.message,true));\n</script></body></html>\n'
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
     totals={t:c.execute(f'SELECT COUNT(*) FROM {t} WHERE save_id=?',(sid,)).fetchone()[0] for t in ['games','events','roster_moves','stories']}
     players=[dict(x) for x in c.execute('SELECT * FROM players WHERE save_id=? ORDER BY name LIMIT 500',(sid,))]
     stories=[dict(x) for x in c.execute('SELECT * FROM stories WHERE save_id=? ORDER BY id DESC LIMIT 100',(sid,))]
     rules_row=c.execute('SELECT rules_json FROM settings WHERE save_id=?',(sid,)).fetchone()
     rules=json.loads(rules_row[0]) if rules_row else DEFAULT_RULES
     if p.path=='/api/dashboard':return json_response(self,200,{'games':games,'events':events,'moves':moves,'stories':stories,'players':players,'counts':{'games':totals['games'],'events':totals['events'],'moves':totals['roster_moves'],'stories':totals['stories']},'tracked_team':rules.get('tracked_team','KIA 타이거즈'),'story_count':count})
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
   if p=='/api/parse-log':return json_response(self,200,parse_ootp_log(d.get('raw','')))
   with connect() as c:
    if p=='/api/import-log':
     sid=int(d['save_id'])
     if not c.execute('SELECT 1 FROM saves WHERE id=?',(sid,)).fetchone():raise ValueError('세이브 없음')
     r=parse_ootp_log(d.get('raw',''))
     ext=r['external_id']
     if ext and c.execute('SELECT 1 FROM games WHERE save_id=? AND external_id=?',(sid,ext)).fetchone():raise ValueError('이미 저장된 OOTP 경기 ID입니다')
     if c.execute('SELECT 1 FROM games g JOIN teams a ON a.id=g.away_team_id JOIN teams h ON h.id=g.home_team_id WHERE g.save_id=? AND g.game_date=? AND a.name=? AND h.name=?',(sid,r['game_date'],r['away'],r['home'])).fetchone():raise ValueError('같은 날짜·원정·홈 경기가 이미 있습니다. 더블헤더라면 경기 ID를 확인하고 수동 검토하세요.')
     c.execute('INSERT INTO games(save_id,external_id,game_date,away_team_id,home_team_id,away_runs,home_runs,away_hits,home_hits,away_errors,home_errors,innings_json,source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(sid,ext,r['game_date'],ensure_team(c,sid,r['away']),ensure_team(c,sid,r['home']),r['away_runs'],r['home_runs'],r['away_hits'],r['home_hits'],r['away_errors'],r['home_errors'],json.dumps(r['innings']), 'ootp_log'))
     return json_response(self,200,{'ok':True,'game':r})
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
     ext=d.get('external_id')
     if ext is not None:ext=str(ext).strip() or None
     if ext and c.execute('SELECT 1 FROM games WHERE save_id=? AND external_id=?',(sid,ext)).fetchone():raise ValueError('이미 저장된 OOTP 경기 ID입니다')
     c.execute('INSERT INTO games(save_id,external_id,game_date,away_team_id,home_team_id,away_runs,home_runs,innings_json) VALUES(?,?,?,?,?,?,?,?)',(sid,ext,d['game_date'],ensure_team(c,sid,away),ensure_team(c,sid,home),ar,hr,json.dumps(innings,ensure_ascii=False)))
    elif p=='/api/events':
     validate_date(d['event_date']);assert d['importance'] in 'SABC';assert str(d['headline']).strip()
     c.execute('INSERT INTO events(save_id,event_date,event_type,importance,headline,detail) VALUES(?,?,?,?,?,?)',(sid,d['event_date'],d['event_type'],d['importance'],d['headline'],d.get('detail','')))
    elif p=='/api/stories':
     title=str(d.get('title','')).strip();body=str(d.get('body','')).strip()
     if not title or not body:raise ValueError('제목과 본문을 입력하세요')
     c.execute('INSERT INTO stories(save_id,story_type,title,body,fictional) VALUES(?,?,?,?,1)',(sid,str(d.get('story_type','기타')),title,body))
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
