const SHEET_NAME = 'Sheet1'; // change if your tab has another name
const SECRET = 'PASTE_A_LONG_RANDOM_STRING_HERE'; // must match bot config (keep your existing value!)
const REQUIRED_HEADERS = ['No.', 'Name', 'I want to pray for', 'Prayer request', 'Update', 'How can others support you?'];
// Optional bookkeeping columns, added automatically by ensureExtraHeaders_().
const EXTRA_HEADERS = ['Submitter ID', 'Readers', 'Last read notify'];

function getSheet_() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sh = ss.getSheetByName(SHEET_NAME);
  if (!sh) sh = ss.insertSheet(SHEET_NAME);
  if (sh.getLastRow() === 0) {
    sh.appendRow(REQUIRED_HEADERS.concat(['Date']));
    sh.setFrozenRows(1);
  }
  ensureExtraHeaders_(sh);
  return sh;
}

// Add the optional bookkeeping columns on the fly so existing sheets upgrade in place.
function ensureExtraHeaders_(sh) {
  const map = headerMap_(sh);
  for (let i = 0; i < EXTRA_HEADERS.length; i++) {
    if (!map[EXTRA_HEADERS[i].toLowerCase()]) {
      sh.getRange(1, sh.getLastColumn() + 1).setValue(EXTRA_HEADERS[i]);
    }
  }
}

// Map header name (trimmed, lowercased) -> column number.
// Position-independent: columns can be added/moved freely as long as row 1 names match.
function headerMap_(sh) {
  const ncols = Math.max(sh.getLastColumn(), REQUIRED_HEADERS.length);
  const values = sh.getRange(1, 1, 1, ncols).getValues()[0];
  const map = {};
  for (let i = 0; i < values.length; i++) {
    const h = String(values[i]).trim().toLowerCase();
    if (h) map[h] = i + 1;
  }
  return map;
}

function missingRequired_(map) {
  return REQUIRED_HEADERS.filter(function (h) { return !map[h.toLowerCase()]; });
}

function getCell_(map, vals, h) {
  const c = map[h];
  return c ? vals[c - 1] : '';
}

// Build the JSON object for one prayer row (shared by 'recent' and 'get_by_no').
function rowItem_(map, vals, tz) {
  let date = null;
  if (map['date']) {
    const d = getCell_(map, vals, 'date');
    if (d instanceof Date) date = Utilities.formatDate(d, tz, 'dd MMM yyyy');
    else if (String(d).trim()) date = String(d).trim(); // manually typed text
  }
  return { no: getCell_(map, vals, 'no.'), name: getCell_(map, vals, 'name'),
           topic: getCell_(map, vals, 'i want to pray for'),
           request: getCell_(map, vals, 'prayer request'), update: getCell_(map, vals, 'update'),
           support: getCell_(map, vals, 'how can others support you?'), date: date,
           submitter_id: getCell_(map, vals, 'submitter id'),
           readers: getCell_(map, vals, 'readers'),
           last_read_notify: getCell_(map, vals, 'last read notify') };
}

function jsonOut_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

function dateDisplay_() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  return Utilities.formatDate(new Date(), ss.getSpreadsheetTimeZone(), 'dd MMM yyyy');
}

function doPost(e) {
  try {
    const body = JSON.parse(e.postData.contents);
    if (body.token !== SECRET) return jsonOut_({ ok: false, error: 'bad token' });

    if (body.action === 'add') {
      const sh = getSheet_();
      const map = headerMap_(sh);
      const missing = missingRequired_(map);
      if (missing.length) {
        return jsonOut_({ ok: false, error: 'sheet is missing header(s): ' + missing.join(', ') });
      }

      const no = sh.getLastRow(); // row 1 is header -> this is the next No.
      const ncols = Math.max(sh.getLastColumn(), REQUIRED_HEADERS.length);
      const row = new Array(ncols).fill('');
      row[map['no.'] - 1] = no;
      row[map['name'] - 1] = body.name || '';
      row[map['i want to pray for'] - 1] = body.topic || '';
      row[map['prayer request'] - 1] = body.request || '';
      row[map['update'] - 1] = body.update || '';
      row[map['how can others support you?'] - 1] = body.support || '';

      if (map['submitter id']) {
        row[map['submitter id'] - 1] = body.submitter_id != null ? String(body.submitter_id) : '';
      }

      const dateStr = dateDisplay_();
      sh.appendRow(row);
      if (map['date']) {
        // store a real Date value (sortable/filterable), formatted for humans in the sheet
        sh.getRange(sh.getLastRow(), map['date']).setValue(new Date()).setNumberFormat('dd mmm yyyy');
      }
      return jsonOut_({ ok: true, no: no, date: dateStr });
    }

    if (body.action === 'recent') {
      const sh = getSheet_();
      const map = headerMap_(sh);
      const tz = SpreadsheetApp.getActiveSpreadsheet().getSpreadsheetTimeZone();
      const ncols = Math.max(sh.getLastColumn(), REQUIRED_HEADERS.length);
      const last = sh.getLastRow();
      // Optional limit from the bot (/prayers --number N); default 10, capped at 100.
      const requested = Number(body.limit);
      const want = (isFinite(requested) && requested > 0) ? Math.min(Math.floor(requested), 100) : 10;
      const count = Math.min(want, Math.max(0, last - 1));
      const items = [];
      for (let i = last; i > last - count && i >= 2; i--) {
        const vals = sh.getRange(i, 1, 1, ncols).getValues()[0];
        items.push(rowItem_(map, vals, tz));
      }
      return jsonOut_({ ok: true, items: items });
    }

    if (body.action === 'get_by_no') {
      // Exact lookup by No. — works even for prayers outside the recent list window.
      const sh = getSheet_();
      const map = headerMap_(sh);
      if (!map['no.']) return jsonOut_({ ok: false, error: "sheet is missing the 'No.' column" });
      const target = Number(body.no);
      const last = sh.getLastRow();
      const ncols = Math.max(sh.getLastColumn(), REQUIRED_HEADERS.length);
      const tz = SpreadsheetApp.getActiveSpreadsheet().getSpreadsheetTimeZone();
      for (let i = 2; i <= last; i++) {
        if (Number(sh.getRange(i, map['no.']).getValue()) !== target) continue;
        const vals = sh.getRange(i, 1, 1, ncols).getValues()[0];
        return jsonOut_({ ok: true, item: rowItem_(map, vals, tz) });
      }
      return jsonOut_({ ok: false, error: 'prayer not found: ' + body.no });
    }

    if (body.action === 'mark_read') {
      const sh = getSheet_();
      const map = headerMap_(sh);
      if (!map['readers']) return jsonOut_({ ok: false, error: "sheet is missing the 'Readers' column" });
      const noNum = Number(body.no);
      const readerId = String(body.reader_id || '');
      const last = sh.getLastRow();
      for (let i = 2; i <= last; i++) {
        if (Number(sh.getRange(i, map['no.']).getValue()) !== noNum) continue;
        const cell = sh.getRange(i, map['readers']);
        const existing = String(cell.getValue() || '');
        const readers = existing ? existing.split(',').map(function (s) { return s.trim(); }).filter(Boolean) : [];
        if (readerId && readers.indexOf(readerId) === -1) {
          readers.push(readerId);
          cell.setValue(readers.join(','));
        }
        if (body.notify_date && map['last read notify']) {
          sh.getRange(i, map['last read notify']).setValue(String(body.notify_date));
        }
        return jsonOut_({ ok: true, count: readers.length });
      }
      return jsonOut_({ ok: false, error: 'prayer not found: ' + body.no });
    }

    return jsonOut_({ ok: false, error: 'unknown action' });
  } catch (err) {
    return jsonOut_({ ok: false, error: String(err) });
  }
}

function doGet(e) {
  if (e.parameter && e.parameter.token === SECRET) return jsonOut_({ ok: true });
  return jsonOut_({ ok: false, error: 'bad token' });
}
