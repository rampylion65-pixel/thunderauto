import os, uuid
import requests
from smart import schedule, metadata, IST, norm
from library import parse_content, choose
from urllib.parse import urlencode, urlparse
import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
import psycopg
from psycopg.rows import dict_row
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from flask import Flask, request, redirect, render_template, abort, session
from werkzeug.utils import secure_filename
from functools import wraps
import secrets
app=Flask(__name__)
import hashlib, base64, tempfile, json
from cryptography.fernet import Fernet
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
app.secret_key=os.environ.get('APP_SECRET_KEY') or hashlib.sha256((os.environ.get('ADMIN_PASSWORD','')+os.environ.get('YOUTUBE_CLIENT_SECRET','')).encode()).hexdigest()
app.config.update(SESSION_COOKIE_SECURE=True, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax')

app.config['MAX_CONTENT_LENGTH']=100*1024*1024
ROOT=os.getenv('DATA_DIR','data'); os.makedirs(ROOT,exist_ok=True)
def storage():
 return boto3.client('s3', endpoint_url=os.environ['R2_ENDPOINT_URL'], aws_access_key_id=os.environ['R2_ACCESS_KEY_ID'], aws_secret_access_key=os.environ['R2_SECRET_ACCESS_KEY'], region_name='auto', config=Config(signature_version='s3v4', connect_timeout=10, read_timeout=30))
def bucket(): return os.environ['R2_BUCKET_NAME']
@app.errorhandler(ClientError)
@app.errorhandler(BotoCoreError)
def storage_error(error):
 app.logger.warning('R2 operation failed: %s', type(error).__name__)
 return 'Cloud storage operation failed. Check Render configuration and R2 permissions.', 502
@contextmanager
def db():
 url=os.environ.get('DATABASE_URL')
 if not url: raise RuntimeError('DATABASE_URL is required')
 with psycopg.connect(url, row_factory=dict_row, connect_timeout=10) as c:
  yield c
with db() as c:
 c.execute('CREATE TABLE IF NOT EXISTS thunder_jobs(id TEXT PRIMARY KEY,title TEXT,platform TEXT,scheduled TEXT,file TEXT,status TEXT)')
with db() as c:
 c.execute('CREATE TABLE IF NOT EXISTS thunder_youtube(id INTEGER PRIMARY KEY, token TEXT NOT NULL)')
 c.execute('ALTER TABLE thunder_jobs ADD COLUMN IF NOT EXISTS youtube_id TEXT')
 c.execute('ALTER TABLE thunder_jobs ADD COLUMN IF NOT EXISTS error TEXT')
with db() as c:
 c.execute("ALTER TABLE thunder_jobs ADD COLUMN IF NOT EXISTS auto_enabled BOOLEAN NOT NULL DEFAULT FALSE")
 c.execute("ALTER TABLE thunder_jobs ADD COLUMN IF NOT EXISTS kids BOOLEAN")
 c.execute("ALTER TABLE thunder_jobs ADD COLUMN IF NOT EXISTS description TEXT DEFAULT ''")
 c.execute("ALTER TABLE thunder_jobs ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0")
 c.execute("ALTER TABLE thunder_jobs ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ")
with db() as c:
 c.execute("CREATE TABLE IF NOT EXISTS thunder_profiles(id TEXT PRIMARY KEY, name TEXT NOT NULL, owner_id TEXT NOT NULL DEFAULT 'admin')")
 c.execute("INSERT INTO thunder_profiles(id,name) VALUES('default','Main Channel') ON CONFLICT DO NOTHING")
 c.execute("CREATE TABLE IF NOT EXISTS thunder_connections(profile_id TEXT PRIMARY KEY REFERENCES thunder_profiles(id), token TEXT NOT NULL)")
 c.execute("INSERT INTO thunder_connections SELECT 'default',token FROM thunder_youtube WHERE id=1 ON CONFLICT DO NOTHING")
 c.execute("ALTER TABLE thunder_jobs ADD COLUMN IF NOT EXISTS profile_id TEXT NOT NULL DEFAULT 'default' REFERENCES thunder_profiles(id)")
 c.execute("ALTER TABLE thunder_jobs ADD COLUMN IF NOT EXISTS file_cleaned BOOLEAN NOT NULL DEFAULT FALSE")
with db() as c:
 c.execute("CREATE TABLE IF NOT EXISTS thunder_facebook(profile_id TEXT PRIMARY KEY REFERENCES thunder_profiles(id), page_id TEXT NOT NULL, page_name TEXT NOT NULL, token TEXT NOT NULL)")
 c.execute("ALTER TABLE thunder_jobs ADD COLUMN IF NOT EXISTS facebook_id TEXT")

with db() as c:
 c.execute('CREATE TABLE IF NOT EXISTS thunder_title_memory(key TEXT PRIMARY KEY)')
 c.execute('CREATE TABLE IF NOT EXISTS thunder_knowledge(topic TEXT,phrase TEXT,PRIMARY KEY(topic,phrase))')
 c.execute('CREATE TABLE IF NOT EXISTS thunder_drafts(id TEXT PRIMARY KEY,profile_id TEXT NOT NULL,file TEXT NOT NULL,name TEXT NOT NULL,committed BOOLEAN DEFAULT FALSE,created TIMESTAMPTZ DEFAULT now())')

with db() as c:
 c.execute("CREATE TABLE IF NOT EXISTS thunder_topics(id TEXT PRIMARY KEY,name TEXT NOT NULL,hashtags TEXT NOT NULL DEFAULT '',keywords TEXT NOT NULL DEFAULT '')")
 c.execute("CREATE TABLE IF NOT EXISTS thunder_topic_titles(topic_id TEXT REFERENCES thunder_topics(id),key TEXT,title TEXT NOT NULL,description TEXT NOT NULL,hashtags TEXT NOT NULL,keywords TEXT NOT NULL,PRIMARY KEY(topic_id,key))")
 for name,tags,keys in [('Lord Buddha','#LordBuddha #Buddhism #Mindfulness #Shorts','Buddha, mindfulness, wisdom, compassion'),('Lord Hanuman','#LordHanuman #HanumanJi #Bajrangbali #Shorts','Hanuman Ji, devotion, Ram Bhakti, courage')]:
  c.execute('INSERT INTO thunder_topics VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING',(norm(name),name,tags,keys))

@app.get('/topics')
def topics_list():
 profile_id()
 with db() as c:
  rows=c.execute("SELECT t.*, (SELECT count(*) FROM thunder_topic_titles l WHERE l.topic_id=t.id) AS total,(SELECT count(*) FROM thunder_topic_titles l WHERE l.topic_id=t.id AND EXISTS(SELECT 1 FROM thunder_title_memory m WHERE m.key=l.key)) AS used FROM thunder_topics t ORDER BY name").fetchall()
 return {'topics':rows}
@app.post('/topics/save')
def topics_save():
 profile_id(); name=request.form.get('name','').strip(); tags=request.form.get('hashtags','').strip(); keys=request.form.get('keywords','').strip()
 if not norm(name) or len(name)>60 or len(tags)>400 or len(keys)>1000: return {'error':'Invalid topic or metadata length'},400
 with db() as c: c.execute('INSERT INTO thunder_topics VALUES(%s,%s,%s,%s) ON CONFLICT(id) DO UPDATE SET hashtags=EXCLUDED.hashtags,keywords=EXCLUDED.keywords',(norm(name),name,tags,keys))
 return {'ok':True}
@app.post('/topics/import')
def topics_import():
 profile_id(); tid=request.form.get('topic_id','')
 try: rows=parse_content(request.form.get('content',''),request.form.get('format','txt'))
 except ValueError as e: return {'error':str(e)},400
 with db() as c:
  if not c.execute('SELECT id FROM thunder_topics WHERE id=%s',(tid,)).fetchone(): return {'error':'Unknown topic'},400
  with c.cursor() as cur: cur.executemany('INSERT INTO thunder_topic_titles VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',[(tid,k,*v) for k,v in rows.items()]); added=cur.rowcount
 return {'added':added}
@app.get('/topics/titles')
def topics_titles():
 profile_id(); tid=request.args.get('topic_id',''); offset=max(0,request.args.get('offset',0,type=int))
 with db() as c: rows=c.execute('SELECT l.*,EXISTS(SELECT 1 FROM thunder_title_memory m WHERE m.key=l.key) AS used FROM thunder_topic_titles l WHERE topic_id=%s ORDER BY key LIMIT 100 OFFSET %s',(tid,offset)).fetchall()
 return {'titles':rows}
@app.post('/topics/delete-title')
def topics_delete_title():
 profile_id()
 with db() as c: c.execute('DELETE FROM thunder_topic_titles WHERE topic_id=%s AND key=%s',(request.form.get('topic_id'),request.form.get('key')))
 return {'ok':True}

def graph(path, method='GET', token=None, **kwargs):
 version=os.getenv('META_GRAPH_VERSION','v24.0')
 headers={'Authorization':'Bearer '+token} if token else {}
 response=requests.request(method,'https://graph.facebook.com/'+version+'/'+path,headers=headers,timeout=30,**kwargs)
 if not response.ok: raise RuntimeError('Meta API HTTP '+str(response.status_code))
 return response.json()

@app.get('/oauth/facebook')
def facebook_connect():
 ident=profile_id(); state=secrets.token_urlsafe(32)
 session['fb_state']=state; session['fb_profile']=ident
 args={'client_id':os.environ['META_APP_ID'],'redirect_uri':os.environ['META_REDIRECT_URI'],'state':state,'response_type':'code'}
 if os.getenv('META_CONFIG_ID'): args['config_id']=os.environ['META_CONFIG_ID']
 else: args['scope']='pages_show_list,pages_manage_posts,pages_read_engagement'
 return redirect('https://www.facebook.com/'+os.getenv('META_GRAPH_VERSION','v24.0')+'/dialog/oauth?'+urlencode(args))

@app.get('/oauth/facebook/callback')
def facebook_callback():
 state=session.pop('fb_state',None); ident=session.pop('fb_profile',None)
 if not state or not ident or not secrets.compare_digest(state,request.args.get('state','')): abort(400,'Invalid OAuth state')
 if not request.args.get('code'): abort(400,'Facebook authorization declined')
 try:
  token=graph('oauth/access_token',params={'client_id':os.environ['META_APP_ID'],'client_secret':os.environ['META_APP_SECRET'],'redirect_uri':os.environ['META_REDIRECT_URI'],'code':request.args['code']})['access_token']
  token=graph('oauth/access_token',params={'grant_type':'fb_exchange_token','client_id':os.environ['META_APP_ID'],'client_secret':os.environ['META_APP_SECRET'],'fb_exchange_token':token})['access_token']
  pages=graph('me/accounts',token=token,params={'fields':'id,name,access_token,tasks','limit':100})['data']
 except Exception: abort(502,'Facebook authorization failed. Check app configuration and permissions.')
 # Keep tokens server-side in an encrypted database record, never in the session cookie.
 nonce=secrets.token_urlsafe(24)
 with db() as c:
  c.execute("CREATE TABLE IF NOT EXISTS thunder_fb_choices(nonce TEXT PRIMARY KEY, profile_id TEXT NOT NULL, payload TEXT NOT NULL, expires TIMESTAMPTZ NOT NULL)")
  c.execute("DELETE FROM thunder_fb_choices WHERE expires < now()")
  c.execute("INSERT INTO thunder_fb_choices VALUES(%s,%s,%s,now()+interval '10 minutes')",(nonce,ident,cipher().encrypt(json.dumps(pages).encode()).decode()))
 session['fb_choice']=nonce; session.setdefault('csrf',secrets.token_hex(32))
 return render_template('facebook_pages.html',pages=[{'id':x['id'],'name':x['name']} for x in pages],active=ident)

@app.post('/facebook/select')
def facebook_select():
 nonce=session.pop('fb_choice',None); ident=profile_id()
 with db() as c:
  row=c.execute('DELETE FROM thunder_fb_choices WHERE nonce=%s AND profile_id=%s AND expires>now() RETURNING payload',(nonce,ident)).fetchone()
  if not row: abort(400,'Reconnect Facebook')
  pages=json.loads(cipher().decrypt(row['payload'].encode()))
  page=next((x for x in pages if x['id']==request.form.get('page_id')),None)
  if not page or not page.get('access_token'): abort(400,'Invalid Page')
  c.execute('INSERT INTO thunder_facebook VALUES(%s,%s,%s,%s) ON CONFLICT(profile_id) DO UPDATE SET page_id=EXCLUDED.page_id,page_name=EXCLUDED.page_name,token=EXCLUDED.token',(ident,page['id'],page['name'],cipher().encrypt(page['access_token'].encode()).decode()))
 return back(ident,'accounts')

@app.post('/facebook/disconnect')
def facebook_disconnect():
 ident=profile_id()
 with db() as c: c.execute('DELETE FROM thunder_facebook WHERE profile_id=%s',(ident,))
 return back(ident,'accounts')

def process_facebook():
 with db() as c:
  r=c.execute("SELECT * FROM thunder_jobs WHERE platform='Facebook' AND auto_enabled AND status='Pending' AND facebook_id IS NULL AND scheduled<=%s AND EXISTS(SELECT 1 FROM thunder_facebook f WHERE f.profile_id=thunder_jobs.profile_id) ORDER BY scheduled LIMIT 1 FOR UPDATE SKIP LOCKED",(datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M'),)).fetchone()
  if not r: return 'no_job'
  t=c.execute('SELECT * FROM thunder_facebook WHERE profile_id=%s',(r['profile_id'],)).fetchone()
  if not t: return 'not_connected'
  c.execute("UPDATE thunder_jobs SET status='Uploading',attempts=attempts+1,started_at=now() WHERE id=%s",(r['id'],))
 try:
  token=cipher().decrypt(t['token'].encode()).decode()
  path=t['page_id']+'/video_reels'
  start=graph(path,'POST',token,data={'upload_phase':'start'})
  vid=start['video_id']; url=start['upload_url']; parsed=urlparse(url)
  if parsed.scheme!='https' or parsed.hostname!='rupload.facebook.com' or parsed.username or parsed.password: raise RuntimeError('Unexpected upload host')
  with tempfile.TemporaryDirectory() as tmp:
   local=os.path.join(tmp,'video.mp4'); storage().download_file(bucket(),r['file'],local)
   with open(local,'rb') as f:
    response=requests.post(url,headers={'Authorization':'OAuth '+token,'offset':'0','file_size':str(os.path.getsize(local))},data=f,timeout=120,allow_redirects=False)
    if not response.ok or not response.json().get('success'): raise RuntimeError('Reel transfer failed')
  result=graph(path,'POST',token,data={'upload_phase':'finish','video_id':vid,'video_state':'PUBLISHED','description':(r['title']+'\n'+(r['description'] or ''))[:5000]})
  if not result.get('success'): raise RuntimeError('Reel finish failed')
  with db() as c: c.execute("UPDATE thunder_jobs SET status='Submitted to Facebook',facebook_id=%s,error=NULL WHERE id=%s",(vid,r['id']))
  return 'facebook_submitted'
 except Exception as e:
  app.logger.warning('Facebook publishing failed: %s',type(e).__name__)
  with db() as c: c.execute("UPDATE thunder_jobs SET status='Needs review',error='Facebook upload failed; check Page before retry' WHERE id=%s",(r['id'],))
  return 'needs_review'

@app.post('/facebook/status/<ident>')
def facebook_status(ident):
 profile=profile_id()
 with db() as c:
  r=c.execute("SELECT facebook_id FROM thunder_jobs WHERE id=%s AND profile_id=%s AND status='Submitted to Facebook'",(ident,profile)).fetchone()
  t=c.execute('SELECT token FROM thunder_facebook WHERE profile_id=%s',(profile,)).fetchone()
 if not r or not t: abort(400)
 try: status=graph(r['facebook_id'],token=cipher().decrypt(t['token'].encode()).decode(),params={'fields':'status'})['status']
 except Exception: abort(502,'Unable to check Facebook status')
 phase=status.get('publishing_phase',{}).get('status')
 if phase=='complete' and status.get('video_status')=='ready':
  with db() as c: c.execute("UPDATE thunder_jobs SET status='Published' WHERE id=%s AND status='Submitted to Facebook'",(ident,))
 return back(profile)

@app.get('/privacy')
@app.get('/terms')
@app.get('/data-deletion')
def public_policy():
 return render_template('policy.html',kind=request.path[1:],contact=os.getenv('POLICY_CONTACT_EMAIL',''))

def profile_id():
 ident=request.values.get('profile','default')
 with db() as c:
  if not c.execute('SELECT id FROM thunder_profiles WHERE id=%s',(ident,)).fetchone(): abort(404)
 return ident
def back(ident,tab='queue'):
 return redirect('/?profile='+ident+'#'+tab)
@app.post('/profiles')
def create_profile():
 name=request.form.get('name','').strip()[:80]
 if not name: abort(400,'Enter profile name')
 ident=uuid.uuid4().hex
 with db() as c: c.execute('INSERT INTO thunder_profiles(id,name) VALUES(%s,%s)',(ident,name))
 return back(ident,'dashboard')
def cipher():
 return Fernet(base64.urlsafe_b64encode(hashlib.sha256(app.secret_key.encode()).digest()))
def flow(state=None):
 return Flow.from_client_config({'web':{'client_id':os.environ['YOUTUBE_CLIENT_ID'],'client_secret':os.environ['YOUTUBE_CLIENT_SECRET'],'auth_uri':'https://accounts.google.com/o/oauth2/auth','token_uri':'https://oauth2.googleapis.com/token'}}, scopes=['https://www.googleapis.com/auth/youtube.upload'],state=state,redirect_uri=os.environ['YOUTUBE_REDIRECT_URI'])
@app.get('/oauth/youtube')
def youtube_connect():
 session['oauth_profile']=profile_id(); f=flow(); url,state=f.authorization_url(access_type='offline',prompt='consent'); session['oauth_state']=state; session['oauth_verifier']=f.code_verifier; return redirect(url)
@app.get('/oauth/youtube/callback')
def youtube_callback():
 state=session.pop('oauth_state',None)
 if not state or not secrets.compare_digest(state,request.args.get('state','')): abort(400,'Invalid OAuth state')
 if request.args.get('error'): return 'Google authorization declined',400
 f=flow(state); f.code_verifier=session.pop('oauth_verifier',None)
 f.fetch_token(code=request.args.get('code'))
 if not f.credentials.refresh_token: abort(400,'Reconnect and grant offline access')
 token=cipher().encrypt(f.credentials.to_json().encode()).decode()
 ident=session.pop('oauth_profile','default')
 with db() as c:
  c.execute('INSERT INTO thunder_connections VALUES(%s,%s) ON CONFLICT(profile_id) DO UPDATE SET token=EXCLUDED.token',(ident,token))
 return back(ident,'accounts')
def process_job(ident=None, manual=False, kids=None, description=''):
 with db() as c:
  if manual:
   r=c.execute("SELECT * FROM thunder_jobs WHERE id=%s AND platform='YouTube' AND status IN ('Pending','Due - API not connected') AND youtube_id IS NULL FOR UPDATE SKIP LOCKED",(ident,)).fetchone()
  else:
   r=c.execute("SELECT * FROM thunder_jobs WHERE platform='YouTube' AND EXISTS (SELECT 1 FROM thunder_connections t WHERE t.profile_id=thunder_jobs.profile_id) AND auto_enabled AND kids IS NOT NULL AND status='Pending' AND youtube_id IS NULL AND scheduled<=%s ORDER BY scheduled LIMIT 1 FOR UPDATE SKIP LOCKED",(datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M'),)).fetchone()
  if not r: return 'no_job'
  t=c.execute('SELECT token FROM thunder_connections WHERE profile_id=%s',(r['profile_id'],)).fetchone()
  if not t: return 'not_connected'
  ident=r['id']
  if not r['file'].startswith('videos/'): return 'old_file'
  c.execute("UPDATE thunder_jobs SET status='Uploading',attempts=attempts+1,started_at=now() WHERE id=%s",(ident,))
 submitted=False
 try:
  creds=Credentials.from_authorized_user_info(json.loads(cipher().decrypt(t['token'].encode())))
  with tempfile.TemporaryDirectory() as tmp:
   path=os.path.join(tmp,'video.mp4'); storage().download_file(bucket(),r['file'],path)
   status={'privacyStatus':'public','selfDeclaredMadeForKids':kids if manual else r['kids']}
   api=build('youtube','v3',credentials=creds,cache_discovery=False)
   req=api.videos().insert(part='snippet,status',body={'snippet':{'title':r['title'][:100],'description':(description if manual else r['description'] or '')[:5000],'categoryId':'22'},'status':status},media_body=MediaFileUpload(path,mimetype='video/mp4',resumable=True))
   submitted=True
   result=req.execute()
  with db() as c: c.execute("UPDATE thunder_jobs SET status='Uploaded public',youtube_id=%s,error=NULL WHERE id=%s",(result['id'],ident))
  return 'uploaded_public'
 except Exception as e:
  app.logger.warning('YouTube upload failed: %s',type(e).__name__)
  retry=not submitted and not manual and r['attempts']<2
  with db() as c: c.execute("UPDATE thunder_jobs SET status=%s,error=%s WHERE id=%s",('Pending' if retry else 'Needs review',type(e).__name__,ident))
  return 'retry_later' if retry else 'needs_review'
@app.post('/youtube/publish/<ident>')
def youtube_publish(ident):
 if request.form.get('kids') not in ('yes','no'): abort(400,'Choose audience')
 result=process_job(ident,True,request.form['kids']=='yes',request.form.get('description',''))
 if result in ('not_connected','no_job','old_file'): abort(400,result)
 return back(request.form.get('profile','default'))
@app.post('/scheduler/run')
def scheduler_run():
 with db() as c:
  c.execute("UPDATE thunder_jobs SET status='Needs review',error='Interrupted upload: check destination platform before retry' WHERE status='Uploading' AND started_at < now()-interval '30 minutes'")
 result=process_job()
 if result=='no_job': result=process_facebook()
 return {'result':result}
@app.before_request
def auth():
 if request.method=='GET' and request.path in ('/privacy','/terms','/data-deletion'): return
 if request.path=='/scheduler/run':
  token=os.getenv('SCHEDULER_TOKEN','')
  if not token or not secrets.compare_digest(request.headers.get('Authorization',''),'Bearer '+token): abort(401)
  return
 password=os.getenv('ADMIN_PASSWORD')
 if not password: abort(503,'Set ADMIN_PASSWORD before use')
 a=request.authorization
 if not a or not secrets.compare_digest(a.username or '', 'admin') or not secrets.compare_digest(a.password or '',password): return ('Login required',401,{'WWW-Authenticate':'Basic realm="Thunder"'})
@app.before_request
def csrf():
 if request.method=='POST' and request.path!='/scheduler/run':
  if not session.get('csrf') or not secrets.compare_digest(session['csrf'],request.form.get('csrf','')): abort(403,'Refresh page and try again')
@app.get('/')
def home():
 session.setdefault('csrf',secrets.token_hex(32))
 with db() as c:
  profiles=c.execute("SELECT p.*, EXISTS(SELECT 1 FROM thunder_connections t WHERE t.profile_id=p.id) AS connected,(SELECT count(*) FROM thunder_jobs j WHERE j.profile_id=p.id AND j.status IN ('Pending','Uploading')) AS pending FROM thunder_profiles p ORDER BY name").fetchall()
  active=profile_id()
  rows=c.execute('SELECT * FROM thunder_jobs WHERE profile_id=%s ORDER BY scheduled',(active,)).fetchall()
  connected=bool(c.execute('SELECT profile_id FROM thunder_connections WHERE profile_id=%s',(active,)).fetchone())
 with db() as c:
  facebook=c.execute('SELECT page_id,page_name FROM thunder_facebook WHERE profile_id=%s',(active,)).fetchone()
 return render_template('index.html',facebook=facebook,rows=rows, connected=connected, profiles=profiles, active=active, counts={k:sum(r['status'] in states for r in rows) for k,states in [('Pending',('Pending','Due - API not connected')),('Published',('Uploaded public','Published')),('Failed',('Needs review','Failed'))]})
@app.post('/smart/preview')
def smart_preview():
 profile=profile_id()
 try:
  names=json.loads(request.form['names']); topic=request.form['topic']; daily=int(request.form['daily']); mode=request.form.get('mode','continue')
  if mode not in ('continue','new','instant','gaps') or not isinstance(names,list) or not names: raise ValueError('Invalid batch')
  with db() as c:
   used={r['key'] for r in c.execute('SELECT key FROM thunder_title_memory').fetchall()}
   used.update(norm(r['title']) for r in c.execute('SELECT title FROM thunder_jobs').fetchall())
   extra=[r['phrase'] for r in c.execute('SELECT phrase FROM thunder_knowledge WHERE topic=%s',(norm(topic),)).fetchall()]
   occupied=[datetime.fromisoformat(r['scheduled']).replace(tzinfo=timezone.utc).astimezone(IST) for r in c.execute("SELECT DISTINCT scheduled FROM thunder_jobs WHERE profile_id=%s AND status IN ('Pending','Uploading','Due - API not connected')",(profile,)).fetchall()]
  dates=schedule(len(names),daily,request.form['start'],occupied=occupied,mode=mode)
  content_mode=request.form.get('content_mode','hybrid')
  if content_mode not in ('hybrid','library','internal'): raise ValueError('Invalid content mode')
  with db() as c:
   selected=c.execute('SELECT * FROM thunder_topics WHERE id=%s',(norm(topic),)).fetchone()
   if not selected: raise ValueError('Choose a saved topic')
   library_rows=c.execute('SELECT * FROM thunder_topic_titles WHERE topic_id=%s ORDER BY key',(selected['id'],)).fetchall() if content_mode!='internal' else []
  items=choose(selected['name'],names,used,extra,library_rows,selected['hashtags'],selected['keywords'],content_mode)
  for item,date in zip(items,dates): item['scheduled']=date
  return {'items':items}
 except ValueError as e: return {'error':str(e)},400
@app.get('/smart/knowledge')
def knowledge_read():
 profile_id(); topic=norm(request.args.get('topic',''))
 with db() as c:
  rows=c.execute('SELECT phrase FROM thunder_knowledge WHERE topic=%s ORDER BY phrase',(topic,)).fetchall()
 return {'phrases':[r['phrase'] for r in rows]}
@app.post('/smart/knowledge')
def smart_knowledge():
 profile_id(); name=request.form.get('topic','').strip(); topic=norm(name)
 phrases=list(dict.fromkeys(x.strip() for x in request.form.get('phrases','').splitlines() if x.strip()))
 if not topic or len(name)>100 or not phrases: return {'error':'Enter a topic and at least one phrase'},400
 if any(len(x)>60 for x in phrases): return {'error':'Each phrase must be at most 60 characters'},400
 with db() as c:
  c.execute('INSERT INTO thunder_topics(id,name) VALUES(%s,%s) ON CONFLICT DO NOTHING',(topic,name))
  added=0
  for phrase in phrases:
   added+=c.execute('INSERT INTO thunder_knowledge VALUES(%s,%s) ON CONFLICT DO NOTHING',(topic,phrase)).rowcount
  rows=c.execute('SELECT phrase FROM thunder_knowledge WHERE topic=%s ORDER BY phrase',(topic,)).fetchall()
 return {'ok':True,'added':added,'phrases':[r['phrase'] for r in rows]}
@app.post('/smart/stage')
def smart_stage():
 profile=profile_id(); ident=request.form.get('id',''); f=request.files.get('video')
 if not f or not f.filename.lower().endswith('.mp4') or len(ident)!=32 or any(x not in '0123456789abcdef' for x in ident): abort(400)
 key='videos/'+ident+'.mp4'
 with db() as c:
  c.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',(ident,))
  r=c.execute('SELECT * FROM thunder_drafts WHERE id=%s',(ident,)).fetchone()
  if r:
   if r['profile_id']!=profile: abort(403)
   return {'ok':True}
  storage().upload_fileobj(f.stream,bucket(),key,ExtraArgs={'ContentType':'video/mp4'})
  c.execute('INSERT INTO thunder_drafts(id,profile_id,file,name) VALUES(%s,%s,%s,%s)',(ident,profile,key,f.filename[:255]))
 return {'ok':True}
@app.get('/smart/drafts')
def smart_drafts():
 profile=profile_id()
 with db() as c: return {'files':c.execute('SELECT id,name FROM thunder_drafts WHERE profile_id=%s AND NOT committed ORDER BY created',(profile,)).fetchall()}
@app.post('/smart/upload')
def smart_upload():
 profile=profile_id(); platforms=request.form.getlist('platform'); kids=request.form.get('kids'); mode=request.form.get('mode','continue')
 try: items=json.loads(request.form['items']); ids=json.loads(request.form['ids'])
 except Exception: abort(400,'Invalid metadata')
 if not ids or len(set(ids))!=len(ids) or len(items)!=len(ids) or not platforms or len(set(platforms))!=len(platforms) or any(x not in ('YouTube','Facebook') for x in platforms) or kids not in ('yes','no'): abort(400,'Invalid batch')
 with db() as c:
  c.execute('LOCK TABLE thunder_jobs IN EXCLUSIVE MODE'); c.execute('LOCK TABLE thunder_title_memory IN EXCLUSIVE MODE')
  drafts=[c.execute('SELECT * FROM thunder_drafts WHERE id=%s AND profile_id=%s FOR UPDATE',(i,profile)).fetchone() for i in ids]
  if any(not d for d in drafts): abort(400,'Missing uploaded video')
  if all(d['committed'] for d in drafts): return {'ok':True,'url':'/?profile='+profile+'#queue'}
  if any(d['committed'] for d in drafts): abort(409,'Some videos already scheduled')
  for platform in platforms:
   table='thunder_connections' if platform=='YouTube' else 'thunder_facebook'
   if not c.execute('SELECT profile_id FROM '+table+' WHERE profile_id=%s',(profile,)).fetchone(): abort(400,platform+' not connected')
  used={norm(r['title']) for r in c.execute('SELECT title FROM thunder_jobs').fetchall()}
  occupied={r['scheduled'] for r in c.execute("SELECT scheduled FROM thunder_jobs WHERE profile_id=%s AND status IN ('Pending','Uploading','Due - API not connected')",(profile,)).fetchall()}
  for d,item in zip(drafts,items):
   title=item['title'].strip(); k=norm(title)
   if not k or len(title)>99 or len(item['description'])>5000 or k in used or not c.execute('INSERT INTO thunder_title_memory VALUES(%s) ON CONFLICT DO NOTHING RETURNING key',(k,)).fetchone(): abort(409,'Duplicate or invalid title; regenerate')
   used.add(k)
   date=datetime.fromisoformat(item['scheduled']).replace(tzinfo=IST)
   if mode=='instant': date=datetime.now(IST)
   elif date<=datetime.now(IST): abort(409,'Schedule expired; regenerate')
   utc=date.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M')
   if mode!='instant' and utc in occupied: abort(409,'Schedule conflict; regenerate')
   for platform in platforms:
    c.execute('INSERT INTO thunder_jobs(id,title,platform,scheduled,file,status,auto_enabled,kids,description,profile_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',(uuid.uuid4().hex,title,platform,utc,d['file'],'Pending',True,kids=='yes',item['description'],profile))
   c.execute('UPDATE thunder_drafts SET committed=TRUE WHERE id=%s',(d['id'],))
 return {'ok':True,'url':'/?profile='+profile+'#queue'}

@app.post('/upload')
def upload():
 profile=profile_id()
 files=request.files.getlist('video'); title=request.form.get('title','').strip(); platform=request.form['platform']; scheduled=request.form['scheduled']
 interval=int(request.form.get('interval','0'))
 if not 0<=interval<=10080: abort(400,'Invalid interval')
 auto=request.form.get('auto')=='yes' and platform in ('YouTube','Facebook')
 kids=request.form.get('kids')
 if auto and platform=='YouTube' and kids not in ('yes','no'): abort(400,'Choose audience')
 scheduled=(datetime.strptime(scheduled,'%Y-%m-%dT%H:%M')-timedelta(hours=5,minutes=30)).strftime('%Y-%m-%dT%H:%M')
 if not files or platform not in ('YouTube','Facebook','Instagram') or any(not f.filename.lower().endswith('.mp4') for f in files): abort(400)
 client=storage(); uploaded=[]
 try:
  with db() as c:
   c.execute('LOCK TABLE thunder_jobs IN EXCLUSIVE MODE')
   for i,f in enumerate(files):
    ident=uuid.uuid4().hex; name='videos/'+ident+'.mp4'
    uploaded.append(name)
    client.upload_fileobj(f.stream,bucket(),name,ExtraArgs={'ContentType':'video/mp4'})
    c.execute('INSERT INTO thunder_jobs(id,title,platform,scheduled,file,status,auto_enabled,kids,description,profile_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',(ident,title or secure_filename(f.filename),platform,(datetime.strptime(scheduled,'%Y-%m-%dT%H:%M')+timedelta(minutes=i*interval)).strftime('%Y-%m-%dT%H:%M'),name,'Pending',auto,kids=='yes' if kids in ('yes','no') else None,request.form.get('description','')[:5000],profile))
 except Exception:
  for name in uploaded:
   try: client.delete_object(Bucket=bucket(),Key=name)
   except Exception: app.logger.warning('R2 cleanup failed for %s',name)
  raise
 return back(request.form.get('profile','default'))
@app.post('/delete/<ident>')
def delete(ident):
 with db() as c:
  c.execute('LOCK TABLE thunder_jobs IN EXCLUSIVE MODE')
  r=c.execute("SELECT file FROM thunder_jobs WHERE id=%s AND status <> 'Uploading' FOR UPDATE",(ident,)).fetchone()
  if r:
   c.execute('DELETE FROM thunder_jobs WHERE id=%s',(ident,))
   if r['file'].startswith('videos/') and not c.execute('SELECT 1 FROM thunder_jobs WHERE file=%s AND NOT file_cleaned',(r['file'],)).fetchone():
    storage().delete_object(Bucket=bucket(),Key=r['file'])
   elif not r['file'].startswith('videos/'):
    try: os.remove(os.path.join(ROOT,os.path.basename(r['file'])))
    except FileNotFoundError: pass
 return back(request.form.get('profile','default'))
@app.post('/edit/<ident>')
def edit(ident):
 scheduled=(datetime.strptime(request.form['scheduled'],'%Y-%m-%dT%H:%M')-timedelta(hours=5,minutes=30)).strftime('%Y-%m-%dT%H:%M')
 with db() as c: c.execute("UPDATE thunder_jobs SET title=%s,scheduled=%s,status='Pending' WHERE id=%s AND status IN ('Pending','Due - API not connected')",(request.form['title'],scheduled,ident))
 return back(request.form.get('profile','default'))
@app.template_filter('ist')
def ist(value): return (datetime.strptime(value,'%Y-%m-%dT%H:%M')+timedelta(hours=5,minutes=30)).strftime('%Y-%m-%dT%H:%M')
@app.post('/check')
def check():
 with db() as c: c.execute("UPDATE thunder_jobs SET status='Due - API not connected' WHERE status='Pending' AND NOT auto_enabled AND scheduled<=%s",(datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M'),))
 return redirect('/')
@app.post('/cleanup')
def cleanup():
 ident=profile_id(); mode=request.form.get('mode')
 if mode not in ('files','history'): abort(400)
 with db() as c:
  c.execute('LOCK TABLE thunder_jobs IN EXCLUSIVE MODE')
  rows=c.execute("SELECT * FROM thunder_jobs WHERE profile_id=%s AND status IN ('Uploaded public','Published') AND (youtube_id IS NOT NULL OR facebook_id IS NOT NULL) FOR UPDATE",(ident,)).fetchall()
  for r in rows:
   if not r['file_cleaned'] and r['file'].startswith('videos/') and not c.execute('SELECT 1 FROM thunder_jobs WHERE file=%s AND id<>%s AND NOT file_cleaned',(r['file'],r['id'])).fetchone():
    storage().delete_object(Bucket=bucket(),Key=r['file'])
   if mode=='history': c.execute('DELETE FROM thunder_jobs WHERE id=%s',(r['id'],))
   else: c.execute('UPDATE thunder_jobs SET file_cleaned=TRUE WHERE id=%s',(r['id'],))
 return back(ident,'settings')
if __name__=='__main__': app.run(port=int(os.getenv('PORT','5000')))