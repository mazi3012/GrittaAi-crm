/**
 * ─────────────────────────────────────────────────────────────────────────────
 *  Gretta AI — Google Sheets mirror endpoint (paste into the SHEET's Apps Script)
 * ─────────────────────────────────────────────────────────────────────────────
 *  SETUP (one time, ~3 minutes):
 *  1. Open "CRM - Instagram Data" → Extensions → Apps Script.
 *  2. Delete the sample code and paste THIS entire file. In Apps Script,
 *     open Project Settings → Script Properties and add:
 *       GOOGLE_SHEET_SECRET = the same value used by the bot
 *       SPREADSHEET_ID = your spreadsheet ID
 *  3. Deploy → New deployment → type: Web app
 *     - Execute as: Me
 *     - Who has access: Anyone          ← required; the secret is the real gate
 *  4. Authorize, copy the /exec URL into GOOGLE_SHEET_WEBAPP_URL.
 *  5. /importsheet in Telegram pulls your existing tabs into the CRM;
 *     /syncsheet pushes the CRM back into per-setter tabs + Closer + Tracker.
 *
 *  PUSH (doPost): body = {secret, action:"replace_all", headers, groups,
 *  closer, tracker, syncedAt}. Every tab in `groups` (one per setter) is
 *  rewritten with the full column set; Closer gets warm leads; Tracker gets
 *  a per-setter summary; each sync is logged in "Sync Log".
 *
 *  PULL (doGet): ?secret=... → {ok, tabs:{name:{headers, rows}}} for every
 *  tab except "Sync Log", using DISPLAY values (dates come back "23 Aug").
 */

// Store these values in Apps Script Project Settings / Script Properties.
// Do not commit live credentials to this file.
const SECRET = PropertiesService.getScriptProperties().getProperty('GOOGLE_SHEET_SECRET') || '';
const SPREADSHEET_ID = PropertiesService.getScriptProperties().getProperty('SPREADSHEET_ID');
const LOG_SHEET = 'Sync Log';
const CLOSER_TAB = 'Closer';
const TRACKER_TAB = 'Tracker';

/** Open the target spreadsheet by ID (works for standalone scripts). */
function getSS_() {
  if (!SPREADSHEET_ID) throw new Error('Missing SPREADSHEET_ID script property');
  return SpreadsheetApp.openById(SPREADSHEET_ID);
}

function doPost(e) {
  const out = ContentService.createTextOutput();
  out.setMimeType(ContentService.MimeType.JSON);
  try {
    const body = JSON.parse(e.postData.contents);
    if (!body || (SECRET && body.secret !== SECRET)) {
      out.setContent(JSON.stringify({ ok: false, error: 'bad secret' }));
      return out;
    }
    if (body.action !== 'replace_all' || !Array.isArray(body.headers)) {
      out.setContent(JSON.stringify({ ok: false, error: 'unsupported action' }));
      return out;
    }
    const ss = getSS_();
    const groups = body.groups || {};
    let total = 0;
    Object.keys(groups).forEach(function (tab) {
      writeTable_(getTab_(ss, tab), body.headers, groups[tab] || []);
      total += (groups[tab] || []).length;
    });
    if (body.closer) {
      writeTable_(getTab_(ss, CLOSER_TAB),
                  body.closer.headers || body.headers, body.closer.rows || []);
    }
    if (body.tracker) {
      writeTable_(getTab_(ss, TRACKER_TAB),
                  body.tracker.headers || ['Setter', 'Total'],
                  body.tracker.rows || []);
    }
    appendLog_(ss, body.syncedAt || new Date(), total,
               Object.keys(groups).length);
    out.setContent(JSON.stringify({
      ok: true,
      detail: total + ' leads across ' + Object.keys(groups).length +
              ' setter tabs (+ Closer & Tracker refreshed)'
    }));
  } catch (err) {
    out.setContent(JSON.stringify({ ok: false, error: String(err) }));
  }
  return out;
}

function doGet(e) {
  const out = ContentService.createTextOutput();
  out.setMimeType(ContentService.MimeType.JSON);
  try {
    if (SECRET && ((e.parameter || {}).secret || '') !== SECRET) {
      out.setContent(JSON.stringify({ ok: false, error: 'bad secret' }));
      return out;
    }
    const ss = getSS_();
    const tabs = {};
    ss.getSheets().forEach(function (sh) {
      const name = sh.getName();
      if (name === LOG_SHEET || sh.getLastRow() === 0) return;
      const values = sh.getDataRange().getDisplayValues();
      tabs[name] = { headers: values[0], rows: values.slice(1) };
    });
    out.setContent(JSON.stringify({ ok: true, tabs: tabs }));
  } catch (err) {
    out.setContent(JSON.stringify({ ok: false, error: String(err) }));
  }
  return out;
}

function getTab_(ss, name) {
  return ss.getSheetByName(name) || ss.insertSheet(name);
}

/** Replace a tab's contents; ISO dates in "(Date)" columns -> '23 Aug'. */
function writeTable_(sheet, headers, rows) {
  const nCols = Math.max(headers.length, 1);
  sheet.clear();
  const data = [headers].concat(rows || []);
  if (data.length) {
    sheet.getRange(1, 1, data.length, nCols).setValues(
      data.map(function (row) {
        return headers.map(function (h, i) {
          let v = row[i] !== undefined && row[i] !== null ? row[i] : '';
          if (String(h).indexOf('(Date)') !== -1 &&
              /^\d{4}-\d{2}-\d{2}$/.test(String(v))) {
            const p = String(v).split('-');
            v = Utilities.formatDate(
              new Date(Number(p[0]), Number(p[1]) - 1, Number(p[2])),
              Session.getScriptTimeZone() || 'GMT', 'd MMM');
          }
          return v;
        });
      })
    );
  }
  const head = sheet.getRange(1, 1, 1, nCols);
  head.setFontWeight('bold').setBackground('#4f46e5').setFontColor('#ffffff');
  sheet.setFrozenRows(1);
}

/** Append a "Sync Log" line, trimmed to the newest 100 entries. */
function appendLog_(ss, when, rowCount, tabCount) {
  const log = getTab_(ss, LOG_SHEET);
  if (log.getLastRow() === 0) {
    log.appendRow(['Synced At', 'Leads', 'Setter Tabs']);
  }
  log.appendRow([when, rowCount, tabCount]);
  const extra = log.getLastRow() - 101;
  if (extra > 0) log.deleteRows(2, extra);
}

/** Run once from the editor (▶ Run) to pre-create tabs & grant permissions. */
function setupSheets() {
  const ss = getSS_();
  const headers = ['Lead Number', 'Full Name (Lead)', 'User name (Lead)',
    'Profile Link', 'Followers Count', 'Sender Name', 'Sender Profile',
    'First Touchpoint (Date)', 'Note', 'Status', 'Last Touchpoint (Date)',
    'Next Touchpoint (Date)', 'Replied', 'Number Received', 'Number',
    'Follow up 1', 'Follow up 1 (Date)', 'Follow up 2', 'Follow up 2 (Date)',
    'Follow up 3', 'Follow up 3 (Date)', 'Follow up 4', 'Follow up 4 (Date)',
    'Discovery Call', 'Discovery Date', 'Closing Call Status',
    'Closed (Won/Lost)'];
  writeTable_(getTab_(ss, CLOSER_TAB), headers, []);
  appendLog_(ss, new Date(), 0, 0);
}
