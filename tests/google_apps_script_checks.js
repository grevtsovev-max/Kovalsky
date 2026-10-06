function runKovalskyBridgeChecks(source) {
  let calls = [], unlocks = 0, transfer;
  const secret = 'a'.repeat(64), id = 'table';
  const tabs = ['Темы','Ключевые слова','Исключения','География'];
  const properties = {KOVALSKY_SECRET:secret,KOVALSKY_SHEET_ID:id};
  const metadata = {spreadsheetId:id,sheets:tabs.map((title,i)=>({properties:{sheetId:i+1,title,gridProperties:{rowCount:20}}}))};
  const mockSheets = {Spreadsheets: {
    get: () => metadata,
    create: body => { assert(body.sheets.length === 1, 'Transfer must use a separate file'); return {spreadsheetId:'private-file',spreadsheetUrl:'https://docs.google.com/spreadsheets/d/private-file/edit'}; },
    Values: {update: (body,table,range,options) => { transfer={body,table,range,options}; }, batchGet: (table, options) => {
      calls.push(['read', options]);
      return {valueRanges: options.ranges.map(r => ({range:r, values:[['Header']]}))};
    }},
    batchUpdate: (body, table) => {
      calls.push(['write', body]);
      return {replies: body.requests.map(r => r.findReplace ? {findReplace:{occurrencesChanged:1}} : {})};
    }
  }};
  const script = new Function('PropertiesService','LockService','ContentService','Sheets','SpreadsheetApp','HtmlService','Utilities','ScriptApp',source+'\nreturn {doPost, setupKovalsky, exportKovalskyConnection};')(
    {getScriptProperties:()=>({getProperty:k=>properties[k],setProperties:v=>Object.assign(properties,v)})},
    {getScriptLock:()=>({tryLock:()=>true,releaseLock:()=>{unlocks++}})},
    {MimeType:{JSON:'json'},createTextOutput:body=>({setMimeType:()=>body})},mockSheets,
    {getActiveSpreadsheet:()=>({getId:()=>id,getSheetByName:n=>tabs.includes(n)}),getUi:()=>({showModalDialog:()=>{},alert:()=>{}})},
    {createHtmlOutput:()=>({setWidth(){return this},setHeight(){return this}})},{getUuid:()=>{throw Error('Existing secret must be preserved')}},
    {getService:()=>({getUrl:()=> 'https://script.google.com/macros/s/'+'x'.repeat(30)+'/exec'})});
  const assert=(value,message)=>{if(!value)throw Error(message)};
  const send=body=>JSON.parse(script.doPost({postData:{contents:JSON.stringify({secret,spreadsheet_id:id,...body})}}));
  const find={findReplace:{range:{sheetId:2,startRowIndex:0,endRowIndex:1,startColumnIndex:1,endColumnIndex:2},find:'Слово или фраза',replacement:'Слово или фраза',matchCase:true,matchEntireCell:true,searchByRegex:false,includeFormulas:false}};
  script.setupKovalsky();assert(properties.KOVALSKY_SECRET===secret,'Setup changed access code');
  assert(send({secret:'b'.repeat(64)}).error==='ACCESS_DENIED','Wrong access code accepted');
  assert(send({spreadsheet_id:'other'}).error==='WRONG_TABLE','Another table accepted');
  assert(calls.length===0,'Denied request accessed table');
  assert(send({method:'GET',suffix:'?fields=sheets(properties(sheetId,title,gridProperties))'}).result.sheets.length===4,'Metadata read failed');
  const ranges=tabs.map(n=>"'"+n+"'!A1:C20");
  const suffix='/values:batchGet?'+ranges.map(r=>'ranges='+encodeURIComponent(r)).join('&');
  assert(send({method:'GET',suffix}).result.valueRanges.length===4,'Current rows read failed');
  const before=calls.length;
  assert(!send({method:'GET',suffix:'/values:batchGet?ranges=Other!A1:C20'}).ok,'Unlisted tab allowed');
  const bad=JSON.parse(JSON.stringify(find));bad.findReplace.range.sheetId=99;
  assert(!send({method:'POST',suffix:':batchUpdate',data:{requests:[bad]}}).ok,'Unlisted tab edit allowed');
  assert(!send({method:'POST',suffix:':batchUpdate',data:{requests:[{deleteSheet:{sheetId:1}}]}}).ok,'Deletion accepted');
  const formula={updateCells:{range:{sheetId:1,startRowIndex:1,endRowIndex:2,startColumnIndex:0,endColumnIndex:3},rows:[{values:[{userEnteredValue:{formulaValue:'=1'}},{userEnteredValue:{stringValue:'word'}},{userEnteredValue:{boolValue:true}}]}],fields:'userEnteredValue'}};
  assert(!send({method:'POST',suffix:':batchUpdate',data:{requests:[formula]}}).ok,'Formula accepted');
  assert(calls.length===before,'Invalid mutation reached Google');
  const insert={insertDimension:{range:{sheetId:2,dimension:'ROWS',startIndex:2,endIndex:3},inheritFromBefore:true}};
  const update={updateCells:{range:{sheetId:2,startRowIndex:2,endRowIndex:3,startColumnIndex:0,endColumnIndex:3},rows:[{values:[{userEnteredValue:{stringValue:'Работа'}},{userEnteredValue:{stringValue:'платёжный агент'}},{userEnteredValue:{boolValue:true}}]}],fields:'userEnteredValue'}};
  const batch=[find,insert,update];
  assert(send({method:'POST',suffix:':batchUpdate',data:{requests:batch}}).result.replies.length===3,'Valid changes failed');
  assert(JSON.stringify(calls[calls.length-1][1].requests)===JSON.stringify(batch),'Conditional atomic batch changed');
  assert(unlocks>=5,'Script lock not released');
  const editorialNames=['Редакторские правила','Примеры редактуры','История обучения'];
  editorialNames.forEach((title,i)=>metadata.sheets.push({properties:{sheetId:i+5,title,gridProperties:{rowCount:20}}}));
  const editorialSuffix='/values:batchGet?'+editorialNames.map((title,i)=>'ranges='+encodeURIComponent("'"+title+"'!A1:"+(i===2?'E':'D')+'20')).join('&');
  assert(send({method:'GET',suffix:editorialSuffix}).result.valueRanges.length===3,'Editorial reads failed');
  const editorialInsert={insertDimension:{range:{sheetId:5,dimension:'ROWS',startIndex:2,endIndex:3},inheritFromBefore:true}};
  const editorialUpdate={updateCells:{range:{sheetId:5,startRowIndex:2,endRowIndex:3,startColumnIndex:0,endColumnIndex:4},rows:[{values:[{userEnteredValue:{stringValue:'Язык и тон'}},{userEnteredValue:{stringValue:'Убирай канцелярит'}},{userEnteredValue:{stringValue:'Образец'}},{userEnteredValue:{boolValue:true}}]}],fields:'userEnteredValue'}};
  assert(send({method:'POST',suffix:':batchUpdate',data:{requests:[editorialInsert,editorialUpdate]}}).ok,'Editorial additions failed');
  const wrongFlag=JSON.parse(JSON.stringify(editorialUpdate));wrongFlag.updateCells.rows[0].values[3].userEnteredValue={stringValue:'TRUE'};
  assert(!send({method:'POST',suffix:':batchUpdate',data:{requests:[wrongFlag]}}).ok,'String checkbox accepted');
  const auditEdit=JSON.parse(JSON.stringify(find));auditEdit.findReplace.range.sheetId=7;
  assert(!send({method:'POST',suffix:':batchUpdate',data:{requests:[auditEdit]}}).ok,'Audit history replacement allowed');
  script.exportKovalskyConnection();
  assert(transfer.table === 'private-file' && transfer.options.valueInputOption === 'RAW', 'Transfer wrote original table or formulas');
  assert(transfer.body.values[1][1] === secret && transfer.body.values[2][1] === id, 'Transfer lost table binding');
  return 'Apps Script checks passed: setup, access, table binding, reads, insertions, atomic writes, forbidden formulas/deletions.';
}
