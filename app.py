import os, uuid
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
  c.execute("UPDATE thunder_jobs SET status='Needs review',error='Interrupted upload: check YouTube Studio before retry' WHERE status='Uploading' AND started_at < now()-interval '30 minutes'")
 return {'result':process_job()}
@app.before_request
def auth():
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
 return render_template('index.html',rows=rows, connected=connected, profiles=profiles, active=active, counts={k:sum(r['status'] in states for r in rows) for k,states in [('Pending',('Pending','Due - API not connected')),('Published',('Uploaded public','Published')),('Failed',('Needs review','Failed'))]})
@app.post('/upload')
def upload():
 profile=profile_id()
 files=request.files.getlist('video'); title=request.form.get('title','').strip(); platform=request.form['platform']; scheduled=request.form['scheduled']
 interval=int(request.form.get('interval','0'))
 if not 0<=interval<=10080: abort(400,'Invalid interval')
 auto=request.form.get('auto')=='yes' and platform=='YouTube'
 kids=request.form.get('kids')
 if auto and kids not in ('yes','no'): abort(400,'Choose audience')
 scheduled=(datetime.strptime(scheduled,'%Y-%m-%dT%H:%M')-timedelta(hours=5,minutes=30)).strftime('%Y-%m-%dT%H:%M')
 if not files or platform not in ('YouTube','Facebook','Instagram') or any(not f.filename.lower().endswith('.mp4') for f in files): abort(400)
 client=storage(); uploaded=[]
 try:
  with db() as c:
   c.execute('LOCK TABLE thunder_jobs IN EXCLUSIVE MODE')
   if c.execute('SELECT count(*) AS total FROM thunder_jobs WHERE profile_id=%s AND status IN (\'Pending\',\'Uploading\',\'Due - API not connected\')',(profile,)).fetchone()['total']+len(files)>100: abort(400,'Queue full')
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
  r=c.execute("SELECT file FROM thunder_jobs WHERE id=%s AND status <> 'Uploading' FOR UPDATE",(ident,)).fetchone()
  if r:
   c.execute('DELETE FROM thunder_jobs WHERE id=%s',(ident,))
   if r['file'].startswith('videos/'):
    storage().delete_object(Bucket=bucket(),Key=r['file'])
   else:
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
  rows=c.execute("SELECT * FROM thunder_jobs WHERE profile_id=%s AND status IN ('Uploaded public','Published') AND youtube_id IS NOT NULL FOR UPDATE",(ident,)).fetchall()
  for r in rows:
   if not r['file_cleaned'] and r['file'].startswith('videos/'):
    storage().delete_object(Bucket=bucket(),Key=r['file'])
   if mode=='history': c.execute('DELETE FROM thunder_jobs WHERE id=%s',(r['id'],))
   else: c.execute('UPDATE thunder_jobs SET file_cleaned=TRUE WHERE id=%s',(r['id'],))
 return back(ident,'settings')
if __name__=='__main__': app.run(port=int(os.getenv('PORT','5000')))