// Вставьте в Apps Script таблицы тем; добавьте сервис Google Sheets.
const KOVALSKY_TABS = ['Темы', 'Ключевые слова', 'Исключения', 'География'];
const KOVALSKY_COLUMNS = {'Темы':3,'Ключевые слова':3,'Исключения':3,'География':3};

function setupKovalsky() {
  const sheet = SpreadsheetApp.getActiveSpreadsheet();
  if (!sheet || KOVALSKY_TABS.some(name => !sheet.getSheetByName(name))) {
    throw new Error('Запустите настройку из таблицы с четырьмя вкладками темника');
  }
  // Authorize the advanced Sheets service before deployment.
  Sheets.Spreadsheets.get(sheet.getId(), {fields: 'spreadsheetId'});
  const props = PropertiesService.getScriptProperties();
  const oldId = props.getProperty('KOVALSKY_SHEET_ID');
  if (oldId && oldId !== sheet.getId()) throw new Error('Скрипт уже привязан к другой таблице');
  const secret = props.getProperty('KOVALSKY_SECRET') || Utilities.getUuid().replace(/-/g, '') + Utilities.getUuid().replace(/-/g, '');
  props.setProperties({KOVALSKY_SHEET_ID: sheet.getId(), KOVALSKY_SECRET: secret});
  SpreadsheetApp.getUi().showModalDialog(HtmlService.createHtmlOutput(
    '<p>Скопируйте код доступа в кабинет Ковальски → Источники → Обучение через таблицу.</p>' +
    '<textarea readonly style="width:100%;height:65px">' + secret + '</textarea>' +
    '<p>Затем разверните скрипт как веб-приложение: выполнять от вашего имени, доступ — Все. В кабинет внесите адрес /exec.</p>'
  ).setWidth(480).setHeight(240), 'Подключение Ковальски');
}

function doPost(e) {
  let lock;
  try {
    if (!e || !e.postData || e.postData.contents.length > 100000) throw new Error('REQUEST_INVALID');
    const request = JSON.parse(e.postData.contents);
    const props = PropertiesService.getScriptProperties();
    const secret = props.getProperty('KOVALSKY_SECRET');
    if (!secret || typeof request.secret !== 'string' || request.secret.length !== secret.length) throw new Error('ACCESS_DENIED');
    let difference = 0;
    for (let i = 0; i < secret.length; i++) difference |= secret.charCodeAt(i) ^ request.secret.charCodeAt(i);
    if (difference) throw new Error('ACCESS_DENIED');
    const id = props.getProperty('KOVALSKY_SHEET_ID');
    if (!id || request.spreadsheet_id !== id) throw new Error('WRONG_TABLE');
    lock = LockService.getScriptLock();
    if (!lock.tryLock(20000)) throw new Error('BUSY');
    const metadata = Sheets.Spreadsheets.get(id, {fields: 'spreadsheetId,sheets(properties(sheetId,title,gridProperties))'});
    const allowed = metadata.sheets.filter(s => Object.prototype.hasOwnProperty.call(KOVALSKY_COLUMNS, s.properties.title));
    if (KOVALSKY_TABS.some(name => !allowed.some(s => s.properties.title === name))) throw new Error('TABS_MISSING');
    let result;
    if (request.method === 'GET' && request.suffix === '?fields=sheets(properties(sheetId,title,gridProperties))') {
      result = {sheets: allowed};
    } else if (request.method === 'GET' && typeof request.suffix === 'string' && request.suffix.indexOf('/values:batchGet?') === 0) {
      const ranges = request.suffix.split('?')[1].split('&').map(part => {
        const pieces = part.split('=');
        if (pieces[0] !== 'ranges' || pieces.length !== 2) throw new Error('RANGE_INVALID');
        return decodeURIComponent(pieces[1].replace(/\+/g, ' '));
      });
      if (!ranges.length || ranges.length > 7 || new Set(ranges).size !== ranges.length || ranges.some(r => !allowed.some(s => {
        const widths = s.properties.title === 'Ключевые слова' ? [3, 4, 5].filter(w => w <= s.properties.gridProperties.columnCount) : [KOVALSKY_COLUMNS[s.properties.title]];
        return widths.some(w => r === "'" + s.properties.title + "'!A1:" + String.fromCharCode(64 + w) + Math.min(10000, s.properties.gridProperties.rowCount));
      }))) throw new Error('RANGE_INVALID');
      result = Sheets.Spreadsheets.Values.batchGet(id, {ranges: ranges});
    } else if (request.method === 'POST' && request.suffix === ':batchUpdate') {
      const requests = request.data && request.data.requests;
      if (!Array.isArray(requests) || !requests.length || requests.length > 20) throw new Error('BATCH_INVALID');
      const flatKeywords = Sheets.Spreadsheets.Values.get(id, "'Ключевые слова'!A1").values[0][0] === 'Ключевик';
      requests.forEach(operation => validateKovalskyOperation(operation, allowed, flatKeywords));
      // Sheets applies the entire validated batch atomically, including conditional replacements.
      result = Sheets.Spreadsheets.batchUpdate({requests: requests}, id);
    } else {
      throw new Error('OPERATION_INVALID');
    }
    return kovalskyResponse({ok: true, spreadsheet_id: id, result: result});
  } catch (error) {
    const permitted = ['REQUEST_INVALID', 'ACCESS_DENIED', 'WRONG_TABLE', 'BUSY', 'TABS_MISSING', 'RANGE_INVALID', 'BATCH_INVALID', 'OPERATION_INVALID'];
    return kovalskyResponse({ok: false, error: permitted.indexOf(error.message) >= 0 ? error.message : 'GOOGLE_OPERATION_FAILED'});
  } finally {
    if (lock) lock.releaseLock();
  }
}

function validateKovalskyOperation(operation, sheets, flatKeywords) {
  if (!operation || Object.keys(operation).length !== 1) throw new Error('OPERATION_INVALID');
  const name = Object.keys(operation)[0];
  const value = operation[name];
  const r = value && value.range;
  const sheet = r && sheets.find(s => s.properties.sheetId === r.sheetId);
  if (!sheet) throw new Error('RANGE_INVALID');
  const keyword = sheet.properties.title === 'Ключевые слова';
  const flat = keyword && flatKeywords;
  const width = flat ? 4 : KOVALSKY_COLUMNS[sheet.properties.title];
  const flagColumn = flat ? 1 : width - 1;
  const history = false;
  if (name === 'insertDimension') {
    if (Object.keys(value).some(k => ['range','inheritFromBefore'].indexOf(k) < 0) || Object.keys(r).some(k => ['sheetId','dimension','startIndex','endIndex'].indexOf(k) < 0) || r.dimension !== 'ROWS' || !Number.isInteger(r.startIndex) || r.startIndex < 1 || r.startIndex > 10000 || r.endIndex !== r.startIndex + 1 || value.inheritFromBefore !== true) throw new Error('OPERATION_INVALID');
    return;
  }
  if (Object.keys(r).some(k => ['sheetId','startRowIndex','endRowIndex','startColumnIndex','endColumnIndex'].indexOf(k) < 0) || !Number.isInteger(r.startRowIndex) || r.startRowIndex < 0 || r.startRowIndex > 10000 || r.endRowIndex !== r.startRowIndex + 1) throw new Error('RANGE_INVALID');
  if (name === 'findReplace') {
    if (history || (r.startRowIndex === 0 && (value.find !== (flat ? 'Ключевик' : 'Слово или фраза') || value.replacement !== value.find))) throw new Error('OPERATION_INVALID');
    if (Object.keys(value).some(k => ['range','find','replacement','matchCase','matchEntireCell','searchByRegex','includeFormulas'].indexOf(k) < 0) || r.startColumnIndex !== (flat && r.startRowIndex === 0 ? 0 : 1) || r.endColumnIndex !== (flat && r.startRowIndex === 0 ? 1 : 2) || value.matchCase !== true || value.matchEntireCell !== true || value.searchByRegex !== false || value.includeFormulas !== false || typeof value.find !== 'string' || typeof value.replacement !== 'string') throw new Error('OPERATION_INVALID');
  } else if (name === 'updateCells') {
    if (Object.keys(value).some(k => ['range','rows','fields'].indexOf(k) < 0) || r.startRowIndex < 1 || value.fields !== 'userEnteredValue' || !Array.isArray(value.rows) || value.rows.length !== 1) throw new Error('OPERATION_INVALID');
    const cells = value.rows[0].values;
    if (r.startColumnIndex === 0 && r.endColumnIndex === width) {
      if (!Array.isArray(cells) || cells.length !== width || cells.some((cell,i) => (history || i !== flagColumn) ? typeof cell.userEnteredValue.stringValue !== 'string' : cell.userEnteredValue.boolValue !== true)) throw new Error('OPERATION_INVALID');
    } else if (!history && r.startColumnIndex === flagColumn && r.endColumnIndex === flagColumn + 1) {
      if (!Array.isArray(cells) || cells.length !== 1 || cells[0].userEnteredValue.boolValue !== false) throw new Error('OPERATION_INVALID');
    } else throw new Error('RANGE_INVALID');
    if (cells.some(cell => Object.keys(cell).length !== 1 || Object.keys(cell.userEnteredValue).length !== 1)) throw new Error('OPERATION_INVALID');
  } else if (name !== 'insertDimension') throw new Error('OPERATION_INVALID');
}

function kovalskyResponse(value) {
  return ContentService.createTextOutput(JSON.stringify(value)).setMimeType(ContentService.MimeType.JSON);
}

// Разовая передача настройки, если кабинет сервера недоступен владельцу.
// Создаётся новая закрытая таблица, не вкладка публичного темника.
function exportKovalskyConnection() {
  const props = PropertiesService.getScriptProperties();
  const secret = props.getProperty('KOVALSKY_SECRET');
  const id = props.getProperty('KOVALSKY_SHEET_ID');
  const url = ScriptApp.getService().getUrl();
  if (!secret || !id || !url) {
    throw new Error('Сначала выполните setupKovalsky и разверните веб-приложение');
  }
  const file = Sheets.Spreadsheets.create({
    properties: {title: 'Ковальски — временное подключение'},
    sheets: [{properties: {title: 'Подключение'}}]
  });
  Sheets.Spreadsheets.Values.update({
    values: [['url', url], ['secret', secret], ['spreadsheet_id', id]]
  }, file.spreadsheetId, "'Подключение'!A1:B3", {valueInputOption: 'RAW'});
  SpreadsheetApp.getUi().alert(
    'Откройте созданный закрытый файл и пришлите в чат только ссылку на него:\n' +
    file.spreadsheetUrl + '\nДоступ по ссылке включать не нужно.'
  );
}
