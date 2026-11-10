"""Authenticated manual acceptance review. Production release stays source locked."""
import hashlib
import json
import re
import secrets
import time
from flask import Blueprint, Response, redirect, render_template_string, request, session
import controlled_acceptance as execution
from live_futures_engine import GateLiveClient, LiveSafetyError

REVIEW_SCHEMA='''CREATE TABLE IF NOT EXISTS luxalgo_acceptance_reviews (
 review_id TEXT PRIMARY KEY, owner_hash TEXT NOT NULL, prepared JSONB NOT NULL,
 expires_epoch BIGINT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())'''
SIGNAL_SQL='''SELECT h.event_id,h.symbol,h.side,h.timeframe,h.level_status,
 h.order_type,h.source_bar_id,h.received_at,l.entry_price,l.stop_loss,l.take_profit
 FROM luxalgo_live_signal_handoff h JOIN luxalgo_signal_levels l USING(event_id)
 WHERE h.event_id=%s'''
PAGE='''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Single-order acceptance review</title>
<style>body{margin:2rem auto;padding:0 1rem;max-width:850px;background:#101827;color:#edf2fa;font:16px system-ui}
a{color:#8bd5ff}label{display:block;margin:1rem 0}input,select,button{font:inherit;padding:.5rem;max-width:100%}
pre{white-space:pre-wrap;overflow-wrap:anywhere;padding:1rem;background:#19283d}button:disabled{opacity:.5}
.status{padding:1rem;border:1px solid #e9b464;border-radius:.5rem}small{display:block;color:#c3ccda}</style>
<h1>Single-order acceptance review</h1>
<p class="status">{{ release_status }}</p>
<p>Preparing a review sends no order. A live test is separate from continuous automation.
This page cannot clear emergency HALT, release source locks or create certification.</p>
{% if message %}<p role="status">{{ message }}</p>{% endif %}
{% if outcome %}<pre>{{ outcome|tojson(indent=2) }}</pre>{% endif %}
{% if prepared %}
<h2>Exact plan for your review</h2><pre>{{ prepared.review|tojson(indent=2) }}</pre>
<p>Review digest: <code>{{ prepared.review_digest }}</code></p>
<p>This review expires within 30 seconds. All signal and account checks run again before submission.</p>
<form method="post" action="/terminal/acceptance/submit">
<input type="hidden" name="csrf_token" value="{{ csrf }}">
<input type="hidden" name="review_id" value="{{ review_id }}">
<input type="hidden" name="review_digest" value="{{ prepared.review_digest }}">
<label><input type="checkbox" name="acknowledge" value="one-real-money-test" required>
I have reviewed this exact plan and choose to submit one real-money test with its native SL and TP.</label>
<small>A position may remain open if protection fails. An uncertain response requires manual exchange review; never retry the entry.</small>
<button type="submit" {% if not released %}disabled{% endif %}>Submit one reviewed test</button></form>
{% endif %}
<h2>Prepare a new review</h2>
<form method="post" action="/terminal/acceptance/prepare">
<input type="hidden" name="csrf_token" value="{{ csrf }}">
<label>Stored LuxAlgo signal <select name="event_id" required>
{% for signal in signals %}<option value="{{ signal.event_id }}">{{ signal.symbol }} {{ signal.side }} · {{ signal.received_at }}</option>{% endfor %}
</select></label>
<small>Signals older than 60 seconds cannot be submitted. No historical signal is replayed as a fresh one.</small>
<p>Quantity is exactly one whole contract. Enter your own maximum limits below;
the software may reject one contract if it exceeds any limit.</p>
{% for name,label,ceiling in fields %}<label>{{ label }} (ceiling {{ ceiling }})
<input name="{{ name }}" type="number" min="0.00000001" max="{{ ceiling }}" step="any" required></label>{% endfor %}
<label>Leverage (must match exchange settings; maximum 5)
<input name="leverage" type="number" min="1" max="5" step="1" required></label>
<button type="submit">Prepare review — no order</button></form>
<p><a href="/live-activation">Activation status</a> · <a href="/">Trading dashboard</a></p></html>'''


def stored_signal(row):
    if not row or not str(row.get('symbol','')).endswith('USDT'):
        raise LiveSafetyError('Stored qualified signal unavailable')
    return dict(event_id=row['event_id'],contract=row['symbol'][:-4]+'_USDT',
                side=row['side'],timeframe=row['timeframe'],levels_status=row['level_status'],
                order_type=row['order_type'],source_bar_id=row['source_bar_id'],
                received_epoch=int(row['received_at'].timestamp()),entry=str(row['entry_price']),
                sl=str(row['stop_loss']),tp=str(row['take_profit']))


def register(app, *, permitted, guard, csrf_valid, connect):
    bp=Blueprint('single_acceptance',__name__)

    def owner_hash():
        token=session.get('csrf_token')
        if not isinstance(token,str) or not token:
            raise LiveSafetyError('Authenticated session token unavailable')
        return hashlib.sha256(token.encode()).hexdigest()

    def display(*, message='', prepared=None, review_id='', outcome=None, status=200):
        try:
            execution.require_release();released=True
        except LiveSafetyError:
            released=False
        try:
            with connect() as con:
                with con.cursor() as cur:
                    cur.execute('''SELECT event_id,symbol,side,received_at
                      FROM luxalgo_live_signal_handoff WHERE level_status='VALID'
                        AND order_type='MARKET' AND source_bar_id IS NOT NULL
                      ORDER BY received_at DESC LIMIT 10''')
                    signals=cur.fetchall()
        except Exception:
            signals=[]
            message=message or 'Stored signal list unavailable; preparation is blocked.'
        response=Response(render_template_string(PAGE,message=message,prepared=prepared,
            review_id=review_id,outcome=outcome,signals=signals,csrf=session.get('csrf_token',''),
            released=released,release_status=('Acceptance source/environment permissions present; HALT and account checks still apply.'
                if released else 'Live submission locked. Software release and independent acceptance review are incomplete.'),
            fields=[('max_margin','Maximum margin USDT','1.70'),
                    ('max_notional','Maximum notional USDT','8.30'),
                    ('max_modeled_loss','Maximum modeled loss USDT','0.10')]),status=status,mimetype='text/html')
        response.headers.update({'Cache-Control':'private, no-store','X-Frame-Options':'DENY',
                                 'X-Content-Type-Options':'nosniff'})
        return response

    @bp.get('/terminal/acceptance')
    def page():
        if not permitted():return redirect('/login?next=/terminal/acceptance',code=303)
        return display()

    @bp.post('/terminal/acceptance/prepare')
    def prepare():
        if not permitted():return guard()
        if not csrf_valid():return Response('Invalid session CSRF',status=403)
        event=request.form.get('event_id','')
        if not re.fullmatch(r'[a-f0-9]{20}',event):return display(message='Select a stored event.',status=422)
        try:
            with connect() as con:
                with con.cursor() as cur:
                    cur.execute(SIGNAL_SQL,(event,));signal=stored_signal(cur.fetchone())
            now=int(time.time())
            scope=dict(scope_id=secrets.token_hex(16),contract=signal['contract'],side=signal['side'],
                       contracts=1,start_epoch=now,end_epoch=now+300,
                       leverage=request.form.get('leverage'),
                       **{key:request.form.get(key) for key in execution.CAPS})
            prepared=execution.prepare(scope,signal,GateLiveClient())
            review_id=secrets.token_hex(16)
            with connect() as con:
                con.execute(REVIEW_SCHEMA)
                con.execute('''INSERT INTO luxalgo_acceptance_reviews
                    (review_id,owner_hash,prepared,expires_epoch) VALUES (%s,%s,%s::jsonb,%s)''',
                    (review_id,owner_hash(),json.dumps(prepared),prepared['review']['prepared_epoch']+30))
            return display(prepared=prepared,review_id=review_id)
        except LiveSafetyError as exc:
            return display(message=str(exc),status=422)
        except Exception:
            return display(message='Review preparation unavailable; no order was submitted.',status=503)

    @bp.post('/terminal/acceptance/submit')
    def submit():
        if not permitted():return guard()
        if not csrf_valid():return Response('Invalid session CSRF',status=403)
        # Locks precede every lookup, claim and exchange request.
        try:execution.require_release()
        except LiveSafetyError:return display(message='Submission blocked by release gates. No order submitted.',status=423)
        if request.form.get('acknowledge') != 'one-real-money-test':
            return display(message='Explicit review acknowledgment required.',status=422)
        review_id=request.form.get('review_id','');digest=request.form.get('review_digest','')
        if not re.fullmatch(r'[a-f0-9]{32}',review_id) or not re.fullmatch(r'[a-f0-9]{64}',digest):
            return display(message='Valid stored review required.',status=422)
        try:
            with connect() as con:
                with con.cursor() as cur:
                    cur.execute('''SELECT prepared FROM luxalgo_acceptance_reviews
                        WHERE review_id=%s AND owner_hash=%s AND expires_epoch>%s''',
                        (review_id,owner_hash(),int(time.time())))
                    row=cur.fetchone()
                    if not row:raise LiveSafetyError('Review expired or belongs to another session')
                    prepared=row['prepared']
                    cur.execute(SIGNAL_SQL,(prepared['review']['plan']['event_id'],))
                    signal=stored_signal(cur.fetchone())
            outcome=execution.submit_once(connect,prepared,digest,signal)
            return display(outcome=outcome,message='Review exchange state before any further action.')
        except LiveSafetyError as exc:
            return display(message=str(exc),status=409)
        except Exception:
            return display(message='Outcome unavailable. Do not retry; inspect exchange and execution journal.',status=503)

    app.register_blueprint(bp)
