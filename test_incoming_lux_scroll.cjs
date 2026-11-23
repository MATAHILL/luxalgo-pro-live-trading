// Trade status table regression: the Incoming LuxAlgo Signals table is replaced
// by the live+closed trade status table, keeping the compact scroll viewport.
'use strict';
const fs=require('node:fs'),assert=require('node:assert/strict');
const app=fs.readFileSync('app.py','utf8');
const start=app.indexOf("PAGE = '''");
const end=app.indexOf("'''",start+10);
assert(start>=0&&end>start,'Original terminal page missing');
const page=app.slice(start,end);

// The old signal table is gone entirely.
assert(!page.includes('id="incomingLuxPanel"'),'Old incoming LuxAlgo panel must be deleted');
assert(!page.includes('id="incomingLuxRows"'),'Old incoming signal rows must be deleted');
assert(!page.includes('id="incomingLuxClear"'),'Old visible-reset button must be deleted');
assert(!page.includes('id="incomingLuxFilters"'),'Old status filters must be deleted');
assert(!page.includes('INCOMING LUXALGO ORDERS'),'Old signal heading must be deleted');

// The trade status table sits in its place, with the same scroll behaviour.
assert(page.includes('id="signalStatusPanel"'),'Trade status panel missing');
assert(page.includes('INCOMING LUXALGO SIGNALS STATUS'),'Trade status title missing');
assert(page.includes('id="signalStatusScroll"'),'Vertical scroll viewport missing');
assert(page.includes('class="tablewrap incoming-lux-scroll"'),'Compact scroll region not applied');
assert(page.includes('max-height:535px;overflow-y:auto;overflow-x:auto'),'Vertical scrolling and horizontal compatibility missing');
assert(page.includes('position:sticky;top:0'),'Sticky table header missing');

// Serial number first, then time, and a Status column at the end.
const bodyAt=page.indexOf('<tbody id="signalStatusBody"');
assert(bodyAt>0,'Trade status body missing');
const headOpen=page.lastIndexOf('<thead>',bodyAt);
const headClose=page.lastIndexOf('</thead>',bodyAt);
assert(headOpen>0&&headClose>headOpen,'Trade status header missing');
const head=page.slice(headOpen,headClose);
assert(head.includes('<th>S/N</th>'),'Serial number column missing');
assert(head.indexOf('<th>S/N</th>')<head.indexOf('<th>TIME (IST)</th>'),'S/N must precede the time column');
assert(head.includes('<th>STATUS</th>'),'Status column missing');
// The refusal reason is the authoritative answer to "why did this signal not
// become an order", so it is surfaced as the final column.
assert(head.includes('<th>WHY NOT EXECUTED</th>'),'Refusal reason column missing');
assert(head.indexOf('<th>STATUS</th>')<head.indexOf('<th>WHY NOT EXECUTED</th>'),
       'Status must precede the refusal reason column');
assert(head.trimEnd().endsWith('<th>WHY NOT EXECUTED</th></tr>'),
       'Refusal reason must be the last column');

// Both live and closed rows are rendered, with the Status label.
assert(page.includes("getJson('/terminal/live-positions')"),'Live positions must be fetched');
assert(page.includes("getJson('/api/completed-trades')"),'Closed trades must be fetched');
assert(page.includes("getJson('/terminal/incoming-signals?production=1')"),
       'Refusal reasons must be fetched for the WHY NOT EXECUTED column');
assert(page.includes("'Live'"),'Live status label missing');
assert(page.includes("'Closed'"),'Closed status label missing');
assert(page.includes("'Signal'"),'Signal-only status label missing');
assert(page.includes('n+=1;'),'Serial numbering missing');

// A signal refused before any order produces neither a live nor a closed row,
// so it must be rendered from the signals feed or its refusal reason is never
// visible anywhere in the panel.
assert(page.includes('for(const s of sigs)'),'Signal-only rows must be rendered');
assert(page.includes('shown.has(s.event_id)'),'Signal rows must not duplicate a live/closed row');
assert(page.includes('whyByEvent[s.event_id]'),'Refusal reason must reach the signal row');

// The old completed-trades table is gone from the bottom of the page.
assert(!page.includes('id="completedTradesBody"'),'Old completed trades table must be removed');
assert(!page.includes('COMPLETED TRADES \u00b7 EXIT RECORD'),'Old completed trades heading must be removed');

// Untouched sections must survive.
assert(page.includes('id="v4PaperCards"'),'Original protected position cards must stay');
assert(page.includes('id="perfWinRate"'),'Performance analytics must stay');
assert(page.includes('id="wrGauge"'),'Real-time win rate widget must stay');
assert(page.includes('id="radarBody"'),'Market radar must stay');

// Signal receipt and audit must remain intact on the backend.
assert(app.includes("ORDER BY i.received_at DESC LIMIT 25"),'Backend must retain 25 latest signals');
assert(app.includes("luxalgo_incoming_display_reset"),'Safety-preserving visible history reset missing');
assert(!app.includes("DELETE FROM luxalgo_dry_run_intents"),'Webhook audit must never be deleted');
console.log('PASS: signal table replaced by live+closed+signal-only trade status table with S/N, Status and refusal-reason columns');
