const SHEET_NAME = 'Sheet1'; // change if your tab has another name
const SECRET = 'PASTE_A_LONG_RANDOM_STRING_HERE'; // must match bot config (keep your existing value!)
const REQUIRED_HEADERS = ['No.', 'Name', 'I want to pray for', 'Prayer request', 'Update', 'How can others support you?'];

function getSheet_() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sh = ss.getSheetByName(SHEET_NAME);
  if (!sh) sh = ss.insertSheet(SHEET_NAME);
  if (sh.getLastRow() === 0) {
    sh.appendRow(REQUIRED_HEADERS.concat(['Date']));
    sh.setFrozenRows(1);
  }
  return sh;
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
      const count = Math.min(10, Math.max(0, last - 1));
      const items = [];
      for (let i = last; i > last - count && i >= 2; i--) {
        const vals = sh.getRange(i, 1, 1, ncols).getValues()[0];
        let date = null;
        if (map['date']) {
          const d = getCell_(map, vals, 'date');
          if (d instanceof Date) date = Utilities.formatDate(d, tz, 'dd MMM yyyy');
          else if (String(d).trim()) date = String(d).trim(); // manually typed text
        }
        items.push({ no: getCell_(map, vals, 'no.'), name: getCell_(map, vals, 'name'),
                     topic: getCell_(map, vals, 'i want to pray for'),
                     request: getCell_(map, vals, 'prayer request'), update: getCell_(map, vals, 'update'),
                     support: getCell_(map, vals, 'how can others support you?'), date: date });
      }
      return jsonOut_({ ok: true, items: items });
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
