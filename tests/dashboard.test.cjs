const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.join(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
const script = html.match(/<script>\s*([\s\S]*?)<\/script>/)[1];
// CI uses synthetic records, never personal collections. Set MYDBDB_TEST_DATA
// explicitly to validate a local export with the same complete aggregation test.
const realData = process.env.MYDBDB_TEST_DATA
  ? JSON.parse(fs.readFileSync(process.env.MYDBDB_TEST_DATA, 'utf8'))
  : ['movie', 'tv', 'book', 'game', 'music', 'performance', 'podcast'].flatMap((category, index) =>
      ['wishlist', 'complete', 'progress', 'dropped'].map((status, month) =>
        record(`${2024 + index % 3}-${String(month * 3 + 1).padStart(2, '0')}-15`, category, status)));

// Exercise the actual inline page script. DOM and Chart are test doubles;
// these tests check data flow and rendering inputs, not browser layout.
function element() {
  let markup = '';
  return {
    children: [], style: {}, listeners: {}, textContent: '', className: '',
    classList: {add(){}, remove(){}, toggle(){}},
    get innerHTML() { return markup; },
    set innerHTML(value) { markup = value; this.children = []; this.textContent = ''; },
    appendChild(child) { this.children.push(child); },
    addEventListener(name, fn) { this.listeners[name] = fn; },
  };
}

function page(rows, {fail = false, deferred = false} = {}) {
  const nodes = new Map();
  const charts = [];
  let release;
  const document = {
    getElementById(id) {
      if (!nodes.has(id)) nodes.set(id, element());
      return nodes.get(id);
    },
    createElement: element,
    addEventListener(){},
    querySelectorAll(selector) {
      return selector === '.yr-btn' ? document.getElementById('d-yr-btns').children : [];
    },
  };
  class Chart {
    constructor(canvas, config) { this.canvas = canvas; this.config = config; this.destroyed = false; charts.push(this); }
    destroy() { assert.equal(this.destroyed, false); this.destroyed = true; }
  }
  const state = {rows, fail};
  const response = () => ({ok: !state.fail, json: async () => state.rows});
  const context = vm.createContext({document, Chart, window: {},
    localStorage: {getItem: () => null, setItem(){}},
    fetch: async (url) => {
      assert.match(url, /^media_data\.json\?t=/);
      if (deferred) { await new Promise(resolve => {release = resolve;}); deferred = false; }
      return response();
    },
  });
  const ready = vm.runInContext(script, context);
  return {nodes, charts, context, state, ready,
    run: code => vm.runInContext(code, context),
    value: code => JSON.parse(vm.runInContext(`JSON.stringify(${code})`, context)),
    release: () => release(),
  };
}

function record(timestamp, category = 'movie', status = 'complete') {
  return {title: 'Test', timestamp, category, status, rating: null, comment: '', year: '', poster: null};
}

test('JSON records drive totals, every date bucket, charts and collection views', async () => {
  const p = page(realData);
  await p.ready;
  const expected = {total: realData.length, status: {}, category: {}};
  const daily = {}, monthly = {}, yearly = {};
  for (const row of realData) {
    expected.status[row.status] = (expected.status[row.status] || 0) + 1;
    expected.category[row.category] = (expected.category[row.category] || 0) + 1;
    const day = row.timestamp, month = day.slice(0, 7), year = day.slice(0, 4);
    daily[day] ??= {}; monthly[month] ??= {};
    daily[day][row.category] = (daily[day][row.category] || 0) + 1;
    monthly[month][row.category] = (monthly[month][row.category] || 0) + 1;
    yearly[year] = (yearly[year] || 0) + 1;
  }
  assert.deepEqual(p.value('dCounts'), expected);
  assert.deepEqual(p.value('DAILY'), daily);
  assert.deepEqual(p.value('MONTHLY'), monthly);
  assert.deepEqual(p.value('YEARLY'), yearly);
  const latest = realData.map(r => r.timestamp).sort().at(-1);
  assert.equal(p.value('dActiveYear'), Number(latest.slice(0, 4)));
  assert.ok(p.value('MONTHLY')[latest.slice(0, 7)]);
  const stats = p.nodes.get('d-stats').children;
  assert.equal(stats.length, 8);
  assert.ok(stats[0].innerHTML.includes(realData.length.toLocaleString()));
  assert.equal(p.value('gFiltered.length'), realData.length);
  assert.equal(p.value('tFiltered.length'), realData.length);
  const monthChart = p.charts[0].config.data;
  for (let month = 0; month < 12; month++) {
    const key = `${latest.slice(0, 4)}-${String(month + 1).padStart(2, '0')}`;
    const actual = monthChart.datasets.reduce((sum, d) => sum + d.data[month], 0);
    assert.equal(actual, realData.filter(r => r.timestamp.startsWith(key)).length);
  }
  assert.equal(p.charts[1].config.data.datasets[0].data.reduce((a, b) => a + b, 0), realData.length);
});

test('dashboard waits for JSON instead of showing stale hard-coded numbers', async () => {
  const p = page([record('2027-04-01')], {deferred: true});
  p.run('initDash()');
  assert.equal(p.nodes.has('d-stats'), false);
  assert.equal(p.charts.length, 0);
  p.release();
  await p.ready;
  assert.equal(p.value('dCounts.total'), 1);
  assert.equal(p.value('dActiveYear'), 2027);
});

test('year buttons update heatmap, monthly data and annual highlighting', async () => {
  const p = page([record('2013-06-22', 'book'), record('2026-10-15')]);
  await p.ready;
  assert.deepEqual(p.value('D_YEARS'), [2013, 2026]);
  p.nodes.get('d-yr-btns').children[0].onclick();
  assert.equal(p.value('dActiveYear'), 2013);
  assert.equal(p.charts.at(-2).config.data.datasets[2].data[5], 1);
  assert.equal(p.charts.at(-1).config.data.datasets[0].backgroundColor[0], '#EF9F27cc');
  assert.equal(p.nodes.get('d-hm-cells').children.filter(cell => cell.style.background).length, 1);
  assert.equal(p.charts.filter(c => !c.destroyed).length, 2);
});

test('reload replaces totals and charts without duplicate cards or controls', async () => {
  const p = page([record('2026-03-01')]);
  await p.ready;
  p.state.rows = [record('2026-10-15'), record('2026-10-15', 'book', 'wishlist')];
  await p.run('loadData()');
  assert.equal(p.value('dCounts.total'), 2);
  assert.deepEqual(p.value('MONTHLY'), {'2026-10': {movie: 1, book: 1}});
  assert.equal(p.nodes.get('d-stats').children.length, 8);
  assert.equal(p.nodes.get('d-legend').children.length, 7);
  assert.equal(p.nodes.get('d-yr-btns').children.length, 1);
  assert.equal(p.charts.filter(c => !c.destroyed).length, 2);
  p.run('initDash()');
  assert.equal(p.charts.length, 4);
});

test('empty JSON shows zero totals and empty chart series', async () => {
  const p = page([]);
  await p.ready;
  assert.deepEqual(p.value('dCounts'), {total: 0, status: {}, category: {}});
  assert.deepEqual(p.value('D_YEARS'), []);
  assert.equal(p.nodes.get('d-stats').children.length, 8);
  assert.deepEqual(p.value('YEARLY'), {});
});

test('undated records count in totals, all statuses count, unsupported categories do not', async () => {
  const p = page([record('', 'movie', 'progress'), record('2026-04-01', 'book', 'dropped'), record('2026-05-01', 'unknown')]);
  await p.ready;
  assert.equal(p.value('dCounts.total'), 2);
  assert.deepEqual(p.value('dCounts.status'), {progress: 1, dropped: 1});
  assert.deepEqual(p.value('YEARLY'), {'2026': 1});
});

test('failed initial fetch shows an error and a successful retry renders stats', async () => {
  const p = page([record('2026-10-15')], {fail: true});
  await p.ready;
  assert.equal(p.nodes.get('d-stats').textContent, '数据加载失败，请刷新重试');
  assert.equal(p.charts.length, 0);
  p.state.fail = false;
  await p.run('loadData()');
  assert.equal(p.nodes.get('d-stats').textContent, '');
  assert.equal(p.nodes.get('d-stats').children.length, 8);
});
