"""Manual recovery review and final user submission; locked by default."""
import hashlib,json,re,secrets,time
from flask import Blueprint,Response,redirect,render_template_string,request,session
import manual_recovery as recovery
from live_futures_engine import LiveSafetyError

SCHEMA='''CREATE TABLE IF NOT EXISTS luxalgo_manual_recovery_reviews (
 review_id TEXT PRIMARY KEY, owner_hash TEXT NOT NULL, prepared JSONB NOT NULL,
 expires_epoch BIGINT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())'''
PAGE='''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Manual recovery review</title><style>body{max-width:850px;margin:2rem auto;padding:1rem;background:#101827;color:#edf2fa;font:16px system-ui}
a{color:#8bd5ff}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#19283d;padding:1rem}label{display:block;margin:1rem 0}button,select{font:inherit;padding:.5rem}button:disabled{opacity:.5}</style>
<h1>Manual recovery review</h1><p>{{ gate }}</p>
<p>This action can only reduce the reviewed bot position. It never enables entries or clears HALT.
Unknown fills, partial fills, open orders or protective triggers require direct exchange review.</p>
{% if message %}<p role="status">{{ message }}</p>{% endif %}
{% if outcome %}<pre>{{ outcome|tojson(indent=2) }}</pre>{% endif %}
{% if prepared %}<h2>Exact recovery order</h2><pre>{{ prepared.review|tojson(indent=2) }}</pre>
<p>Review digest: <code>{{ prepared.review_digest }}</code></p><p>Expires after 30 seconds. This is a real-money close; no automatic retry is allowed.</p>
<form method="post" action="/terminal/recovery/submit"><input type="hidden" name="csrf_token" value="{{ csrf }}">
<input type="hidden" name="review_id" value="{{ review_id }}"><input type="hidden" name="review_digest" value="{{ prepared.review_digest }}">
<label><input type="checkbox" name="acknowledge" value="one-reduce-only-close" required>I reviewed this exact position and choose one reduce-only market close.</label>
<button {% if not released %}disabled{% endif %}>Submit reviewed recovery close</button></form>{% endif %}
<h2>Prepare review — no order</h2><form method="post" action="/terminal/recovery/prepare">
<input type="hidden" name="csrf_token" value="{{ csrf }}"><label>Bot execution
<select name="event_id" required>{% for row in rows %}<option value="{{ row.event_id }}">{{ row.contract }} {{ row.side }} · {{ row.state }} · {{ row.event_id }}</option>{% endfor %}</select></label>
<button>Prepare recovery review</button></form>
{% if not rows %}<p>No eligible bot execution records. A manual exchange trade cannot be treated as a bot fill.</p>{% endif %}
<p><a href="/terminal/acceptance">Acceptance review</a> · <a href="/">Trading dashboard</a></p></html>'''


def register(app,*,permitted,guard,csrf_valid,connect):
    bp=Blueprint('manual_recovery',__name__)
    def owner_hash():
        token=session.get('csrf_token')
        if not isinstance(token,str) or not token:raise LiveSafetyError('Authenticated session token missing')
        return hashlib.sha256(token.encode()).hexdigest()
    def display(message='',prepared=None,review_id='',outcome=None,status=200):
        try:recovery.require_release();released=True
        except LiveSafetyError:released=False
        try:
            with connect() as con:
                with con.cursor() as cur:
                    cur.execute('''SELECT event_id,contract,side,state FROM luxalgo_prod_execution_ledger
                        WHERE state IN ('ENTRY_CONFIRMED','PROTECTION_PENDING','HALTED') ORDER BY updated_at DESC LIMIT 20''')
                    rows=cur.fetchall()
        except Exception:
            rows=[];message=message or 'Execution records unavailable. No recovery can be prepared.'
        response=Response(render_template_string(PAGE,message=message,prepared=prepared,review_id=review_id,
            outcome=outcome,rows=rows,csrf=session.get('csrf_token',''),released=released,
            gate=('Recovery write permissions present; exact position and HALT checks still apply.' if released
                  else 'Live recovery submission locked pending release review.')),
            status=status,mimetype='text/html')
        response.headers.update({'Cache-Control':'private, no-store','X-Frame-Options':'DENY','X-Content-Type-Options':'nosniff'})
        return response
    @bp.get('/terminal/recovery')
    def page():
        if not permitted():return redirect('/login?next=/terminal/recovery',code=303)
        return display()
    @bp.post('/terminal/recovery/prepare')
    def prepare():
        if not permitted():return guard()
        if not csrf_valid():return Response('Invalid session CSRF',status=403)
        event=request.form.get('event_id','')
        if not re.fullmatch(r'[a-f0-9]{20}',event):return display('Select a stored bot event.',status=422)
        try:
            prepared=recovery.prepare(connect,event)
            review_id=secrets.token_hex(16)
            with connect() as con:
                con.execute(SCHEMA)
                con.execute('''INSERT INTO luxalgo_manual_recovery_reviews
                    (review_id,owner_hash,prepared,expires_epoch) VALUES (%s,%s,%s::jsonb,%s)''',
                    (review_id,owner_hash(),json.dumps(prepared),prepared['review']['prepared_epoch']+30))
            return display(prepared=prepared,review_id=review_id)
        except LiveSafetyError as exc:return display(str(exc),status=422)
        except Exception:return display('Recovery review unavailable. No order submitted.',status=503)
    @bp.post('/terminal/recovery/submit')
    def submit():
        if not permitted():return guard()
        if not csrf_valid():return Response('Invalid session CSRF',status=403)
        try:recovery.require_release()
        except LiveSafetyError:return display('Recovery submission blocked. No close submitted.',status=423)
        review_id=request.form.get('review_id','');digest=request.form.get('review_digest','')
        if (request.form.get('acknowledge')!='one-reduce-only-close'
                or not re.fullmatch(r'[a-f0-9]{32}',review_id) or not re.fullmatch(r'[a-f0-9]{64}',digest)):
            return display('Explicit acknowledgment and stored review required.',status=422)
        try:
            with connect() as con:
                with con.cursor() as cur:
                    cur.execute('''SELECT prepared FROM luxalgo_manual_recovery_reviews
                        WHERE review_id=%s AND owner_hash=%s AND expires_epoch>%s''',(review_id,owner_hash(),int(time.time())))
                    row=cur.fetchone()
                    if not row:raise LiveSafetyError('Recovery review expired or belongs to another session')
            return display(outcome=recovery.submit_once(connect,row['prepared'],digest))
        except LiveSafetyError as exc:return display(str(exc),status=409)
        except Exception:return display('Outcome unavailable. Never retry the close; inspect exchange and journal.',status=503)
    app.register_blueprint(bp)
