const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const test = require('node:test');
const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'settings.html'), 'utf8');
const source = html.slice(html.indexOf('    const ICON_SYNONYMS'), html.indexOf("    document.addEventListener('keydown'", html.indexOf('    const ICON_SYNONYMS')));
const packs = fs.readdirSync(path.join(root, 'assets/icon_packs'), {withFileTypes: true}).filter(d => d.isDirectory()).map(d => ({
  name: d.name,
  icons: fs.readdirSync(path.join(root, 'assets/icon_packs', d.name)).filter(f => /\.png$/i.test(f)).map(f => ({name: f.replace(/\.png$/i, ''), url: d.name + '/' + f, path: f}))
}));

function context() {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {style: {}, classList: {add() {}, remove() {}}, appendChild() {}});
    return elements.get(id);
  };
  const pending = [];
  const ctx = vm.createContext({document: {getElementById: element, createElement: () => ({})}, fetch: () => new Promise(resolve => pending.push(resolve)), escapeHtml: x => x, renderPackTabs() {}, renderPackGrid() {}});
  vm.runInContext('let iconPacksCache = null, currentConfig = null, editingItemIdx = null, editingChildIdx = null, iconModalRequest = 0;', ctx);
  vm.runInContext(source, ctx);
  return {ctx, element, pending};
}

const {ctx} = context();
ctx.packs = packs;
vm.runInContext('iconPacksCache = packs;', ctx);
function suggestions(item) {
  ctx.item = item;
  return vm.runInContext('getSuggestedIcons(item)', ctx).map(x => x.ic.name);
}

test('accented battery and custom battery labels select battery icons', () => {
  for (const item of [{name: 'Batería', type: 'battery'}, {name: 'Carga portátil', type: 'battery'}]) {
    const names = suggestions(item);
    assert.ok(names.length);
    assert.ok(names.every(n => /bater[ií]a|battery/i.test(n)), names.join(', '));
  }
});
test('Excel macro workbooks are recognized by extension', () => {
  assert.match(suggestions({name: '11. NOVIEMBRE 26', target: 'C:\\Reports\\11. NOVIEMBRE 26.xlsm', type: 'app'})[0], /excel/i);
});
test('PWA hosts and Microsoft do not replace the selected application', () => {
  assert.match(suggestions({name: 'WhatsApp', target: 'explorer.exe', type: 'app'})[0], /whatsapp/i);
  const teams = suggestions({name: 'Microsoft Teams', target: 'explorer.exe', type: 'app'});
  assert.match(teams[0], /teams/i);
  assert.ok(!teams.some(n => /forms|explorer|explorador/i.test(n)));
});
test('unknown items do not show arbitrary collection entries', () => {
  assert.equal(suggestions({name: 'zzzzunmatched987', target: '', type: 'app'}).length, 0);
});
test('folders and application launcher use their own meanings', () => {
  assert.match(suggestions({name: 'CARPETAS', type: 'group'})[0], /carpeta|folder/i);
  assert.ok(!suggestions({name: 'Aplicaciones', type: 'action', action_type: 'app_launcher'}).some(n => /apple pay|apple music/i.test(n)));
});
test('late responses cannot overwrite a newer selection', async () => {
  const {ctx, element, pending} = context();
  ctx.config = {items: [{name: 'Chrome', type: 'app'}, {name: 'Batería', type: 'battery'}]};
  vm.runInContext('currentConfig = config;', ctx);
  const older = vm.runInContext('openIconModal(0)', ctx);
  const newer = vm.runInContext('openIconModal(1)', ctx);
  pending[1]({json: async () => ({packs})});
  await newer;
  pending[0]({json: async () => ({packs})});
  await older;
  assert.equal(element('icon-suggestions-label').innerText, 'Sugerencias para "Batería"');
});
