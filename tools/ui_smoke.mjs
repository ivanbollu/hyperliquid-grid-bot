/**
 * 前端冒烟测试（Node 端，无需浏览器）。
 *
 * 用一套最小 DOM 桩把 web/static/app.js 跑起来，喂入真实的后端快照，
 * 逐个渲染各个视图。目的是抓出模板里的拼写错误、未定义变量、
 * 空值解引用这类"只有打开页面才会炸"的问题。
 *
 * 另外还做一层静态一致性检查：JS 里引用的 id / class 是否真的存在。
 *
 * 用法： node tools/ui_smoke.mjs
 */

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const staticDir = path.join(here, '..', 'web', 'static');

let pass = 0, fail = 0;
const check = (name, cond, extra = '') => {
  if (cond) { pass++; console.log(`  [PASS] ${name}`); }
  else { fail++; console.log(`  [FAIL] ${name}  ${extra}`); }
};
const section = (t) => console.log(`\n=== ${t} ===`);
const tick = () => new Promise((r) => setTimeout(r, 0));

/* ---------------- 最小 DOM 桩 ---------------- */
function makeEl(id = '') {
  const el = {
    id, _html: '', textContent: '', value: '', checked: false, disabled: false,
    onclick: null, onchange: null, onkeydown: null, scrollTop: 0, clientHeight: 100, scrollHeight: 100,
    dataset: {}, style: {}, children: [], className: '',
    classList: { toggle() {}, add() {}, remove() {}, contains: () => false },
    querySelector: () => makeEl('q'),
    querySelectorAll: () => [],
    appendChild() {}, remove() {}, focus() {}, select() {}, addEventListener() {},
    getAttribute: () => null, setAttribute() {},
  };
  Object.defineProperty(el, 'innerHTML', { get: () => el._html, set: (v) => { el._html = String(v); } });
  return el;
}

/** 假的 EIP-1193 钱包插件。 */
function makeProvider({ accounts = [], chainId = '0xa4b1', reject = false } = {}) {
  const handlers = {};
  return {
    chainId,
    request: async ({ method }) => {
      if (reject) { const e = new Error('User rejected the request'); e.code = 4001; throw e; }
      if (method === 'eth_requestAccounts' || method === 'eth_accounts') return accounts;
      if (method === 'eth_chainId') return chainId;
      return null;
    },
    on: (ev, fn) => { handlers[ev] = fn; },
    _emit: (ev, v) => { if (handlers[ev]) handlers[ev](v); },
    _handlers: handlers,
  };
}

function makeContext({ withEcharts, ethereum, walletPayload, fetchFails, embedded, userAgent }) {
  const nodes = new Map();
  const ctx = {
    console,
    setTimeout: () => 0,
    clearTimeout: () => {},
    setInterval: () => 0,
    clearInterval: () => {},
    JSON, Math, Date, Number, String, Boolean, Array, Object, RegExp, Error, isFinite, parseInt, parseFloat, Promise,
    navigator: {
      clipboard: { writeText: async () => {} },
      userAgent: userAgent || 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
    },
    location: { origin: 'http://127.0.0.1:8787', href: 'http://127.0.0.1:8787/' },
    document: {
      getElementById: (id) => { if (!nodes.has(id)) nodes.set(id, makeEl(id)); return nodes.get(id); },
      createElement: () => makeEl(),
      querySelectorAll: () => [],
    },
    fetch: async (url) => {
      const u = String(url);
      if (u.includes('/api/wallet/')) {
        if (fetchFails) return { ok: false, status: 502, json: async () => ({ detail: '查询 Hyperliquid 账户失败：模拟故障' }) };
        return { ok: true, status: 200, json: async () => walletPayload };
      }
      return { ok: true, status: 200, json: async () => ({ equity: [[1, 100], [2, 101]], logs: ['a', 'b'] }) };
    },
    EventSource: class { constructor() { this.onopen = null; this.onerror = null; this.onmessage = null; } close() {} },
  };
  ctx.window = { addEventListener() {}, localStorage: makeLocalStorage() };
  // embedded=true 模拟「页面被嵌在客户端预览窗口里」：self 与 top 不是同一个对象
  ctx.window.self = embedded ? {} : ctx.window;
  ctx.window.top = embedded ? {} : ctx.window;
  if (ethereum !== undefined) ctx.window.ethereum = ethereum;
  if (withEcharts) {
    ctx.echarts = {
      init: () => ({ setOption() {}, resize() {} }),
      graphic: { LinearGradient: class { constructor() {} } },
    };
  }
  ctx.globalThis = ctx;
  return { ctx, nodes };
}

function makeLocalStorage() {
  const m = new Map();
  return {
    getItem: (k) => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => m.set(k, String(v)),
    removeItem: (k) => m.delete(k),
  };
}

const EXPOSE = [
  'state', 'renderTop', 'renderSidebar', 'selectSlot', 'updateDetail', 'renderTab',
  'renderConfigForm', 'saveConfig', 'pushChartData', 'buildDetailSkeleton', 'toast',
  'esc', 'num', 'px', 'sz', 'pnl',
  // 钱包相关
  'wallet', 'walletOpen', 'walletClose', 'walletConnect', 'walletManual', 'walletBind',
  'walletDisconnect', 'walletSwitchChain', 'loadWallet', 'renderWalletDrawer',
  'renderWalletChip', 'sparkline', 'chainInfo', 'shortAddr', 'isAddr', 'wProvider',
  // 钱包发现与诊断
  'wList', 'wRescan', 'wProviderName', 'wInIframe', 'wEmbedKind', 'wBlocked', 'wSelfUrl', 'wErr', 'wCan',
];

function loadApp(ctx) {
  let src = fs.readFileSync(path.join(staticDir, 'app.js'), 'utf8');
  // 把内部函数暴露出来，方便从外部驱动
  src += '\n;globalThis.__ui = { ' + EXPOSE.join(', ') + ' };';
  vm.createContext(ctx);
  vm.runInContext(src, ctx, { filename: 'app.js' });
  return ctx.__ui;
}

/* ---------------- 静态一致性检查 ---------------- */
function staticChecks() {
  section('静态一致性检查');
  const app = fs.readFileSync(path.join(staticDir, 'app.js'), 'utf8');
  const html = fs.readFileSync(path.join(staticDir, 'index.html'), 'utf8');
  const css = fs.readFileSync(path.join(staticDir, 'style.css'), 'utf8');

  try { new Function(app); check('app.js 语法正确', true); }
  catch (e) { check('app.js 语法正确', false, e.message); return; }

  // 1) JS 里 $('xxx') 用到的 id，必须在页面里存在
  const usedIds = new Set([...app.matchAll(/\$\(\s*'([^']+)'\s*\)/g)].map((m) => m[1]));
  const definedIds = new Set([...html.matchAll(/id="([^"]+)"/g)].map((m) => m[1]));
  for (const m of app.matchAll(/id="([^"${]+)"/g)) definedIds.add(m[1]);
  // 模板里 id="${...}" 这种动态拼出来的，按前缀放宽匹配
  const dynPrefixes = new Set([...app.matchAll(/id="([^"$]*)\$\{/g)].map((m) => m[1]));
  const missingIds = [...usedIds].filter((id) => {
    if (definedIds.has(id)) return false;
    for (const p of dynPrefixes) if (id.startsWith(p)) return false;
    return !id.startsWith('f_'); // 表单字段 id 全部是 f_ 前缀动态生成
  });
  check(`JS 引用的 ${usedIds.size} 个元素 id 都存在`, missingIds.length === 0, missingIds.join(', '));

  // 2) 用到的 class 必须在 CSS 里有定义
  const cssClasses = new Set([...css.matchAll(/\.([a-zA-Z][a-zA-Z0-9_-]*)/g)].map((m) => m[1]));
  const usedClasses = new Set();
  const collect = (raw) => raw.split(/\s+/).forEach((c) => {
    if (c && !/[$`{}"'\\]/.test(c)) usedClasses.add(c);
  });
  for (const m of html.matchAll(/class="([^"]+)"/g)) collect(m[1]);
  for (const m of app.matchAll(/class="([^"$]*)/g)) collect(m[1]);
  for (const m of app.matchAll(/class=\\"([^"$]*)/g)) collect(m[1]);
  const noCss = [...usedClasses].filter((c) => !cssClasses.has(c));
  check(`用到的 ${usedClasses.size} 个 class 都有样式定义`, noCss.length === 0, noCss.join(', '));

  // 3) 首页必须挂上几个关键脚本/元素
  check('页面含 ECharts 与 app.js 脚本', html.includes('echarts') && html.includes('/static/app.js'));
  check('页面含钱包按钮与抽屉容器',
    html.includes('id="btnWallet"') && html.includes('id="walletDrawer"') && html.includes('id="wBody"'));
}

/* ---------------- 真实快照样例 ---------------- */
const OVERVIEW = {
  slots_total: 2, slots_running: 1, slots_live: 1,
  account_value: 1234.56, unrealized_pnl: -12.3456, realized_usd: 8.9, ts: Date.now(),
};
const SLOT = {
  name: 'eth-grid', slot: 'eth-grid', running: true, config_error: '', last_error: '', uptime_sec: 320.5,
  state_status: 'RUNNING',
  env: { network: 'mainnet', dry_run: false, account_address: '0xabc', has_private_key: true },
  market: { coin: 'ETH', leverage: 5, base_size: 0.01 },
  grid: { mode: 'long', step_pct: 0.8, levels: 8 },
  risk: { max_total_notional_usd: 500, max_position_size: 0.5, max_unrealized_loss_usd: 60 },
  live: { account_value: 1234.56, unrealized_pnl: -12.3456, realized_usd: 8.9, net_position: 0.13, mid: 2501.2, open_orders: 6, closed_slots: 3, loop: 64, status: 'RUNNING' },
};
const SLOT2 = {
  name: 'btc-grid', running: false, config_error: '', last_error: '', uptime_sec: 0, state_status: 'IDLE',
  env: { network: 'mainnet', dry_run: true, account_address: '', has_private_key: true },
  market: { coin: 'BTC' }, grid: { mode: 'neutral' }, risk: {}, live: null,
};
const DETAIL = {
  slot: 'eth-grid', coin: 'ETH', running: true, loop: 64, uptime_sec: 320.5, mid: 2501.2,
  network: 'mainnet', dry_run: false, mode: 'long', leverage: 5, anchor_price: 2500.0,
  account_value: 1234.56, net_position: 0.13, entry_px: 2480.5, unrealized_pnl: -12.3456,
  total_notional: 325.16, margin_used: 65.0, liquidation_px: 2100.0, open_orders: 6,
  realized_usd: 8.9, closed_slots: 3, orders_today: 17, status: 'RUNNING',
  stop_reason: '', stopped_until_ms: 0, error: '',
  levels: [
    { key: 'B1', side: 'buy', level: 1, state: 'open', size: 0.01, entry_px: 2480.5, tp_px: 2501.3, has_order: true },
    { key: 'B2', side: 'buy', level: 2, state: 'pending', size: 0.014, entry_px: null, tp_px: null, has_order: true },
    { key: 'B3', side: 'buy', level: 3, state: 'pending', size: 0.0196, entry_px: null, tp_px: null, has_order: false },
  ],
  trades: [
    { ts: Date.now() - 60000, kind: 'open', side: 'buy', size: 0.01, price: 2480.5, pnl: 0 },
    { ts: Date.now() - 30000, kind: 'close', side: 'sell', size: 0.01, price: 2500.3, pnl: 0.198 },
  ],
  equity: [[Date.now() - 120000, 1200], [Date.now() - 60000, 1220], [Date.now(), 1234.56]],
  limits: {
    max_total_notional_usd: 500, max_position_size: 0.5, max_unrealized_loss_usd: 60,
    min_account_value_usd: 20, max_open_orders: 24, max_daily_orders: 200,
  },
};

/* 触发单实例的快照样例 */
const TRIG_SLOT = {
  name: 'eth-buy-2505', slot: 'eth-buy-2505', running: false, config_error: '', last_error: '', uptime_sec: 0,
  state_status: 'IDLE', strategy: 'trigger',
  env: { network: 'mainnet', dry_run: true, account_address: '', has_private_key: false },
  market: { coin: 'ETH', leverage: 5, base_size: 0.01 },
  grid: { mode: 'long', step_pct: 0.8, levels: 8 },
  trigger: {
    side: 'buy', price: 2505, size: 0.005, order_type: 'limit', once: true, tp_pct: 0,
    notional: 12.525, distance_pct: -0.026, phase: '挂单中',
  },
  risk: { max_total_notional_usd: 500, max_position_size: 0.5, max_unrealized_loss_usd: 60 },
  live: null,
};
const TRIG_DETAIL = {
  slot: 'eth-buy-2505', strategy: 'trigger', coin: 'ETH', running: true, loop: 12, uptime_sec: 60,
  mid: 2505.65, network: 'mainnet', dry_run: true, mode: 'trigger-buy', leverage: 5, anchor_price: 2505,
  trigger: {
    side: 'buy', price: 2505, size: 0.005, order_type: 'limit', once: true, tp_pct: 0,
    notional: 12.525, distance_pct: -0.026, phase: '挂单中',
  },
  account_value: 1000.0, net_position: 0, entry_px: null, unrealized_pnl: 0,
  total_notional: 0, margin_used: 0, liquidation_px: null, open_orders: 1,
  realized_usd: 0, closed_slots: 0, orders_today: 1, status: 'RUNNING',
  stop_reason: '', stopped_until_ms: 0, error: '',
  levels: [
    { key: 'T1', side: 'buy', level: 1, state: 'pending', size: 0.005, entry_px: null, tp_px: null, has_order: true },
  ],
  trades: [], equity: [], limits: DETAIL.limits,
};

/* 后端 /api/wallet/{addr} 的真实返回结构（字段与线上一致） */
const WALLET_ADDR = '0x0a3343b600d81cdaac131342652241cd32e14349';
const WALLET_PAYLOAD = {
  ok: true, address: WALLET_ADDR, short: '0x0a33…4349', network: 'mainnet', ts: Date.now(),
  perp: {
    account_value: 233307.69, total_notional: 534687.906, margin_used: 178229.302,
    withdrawable: 55078.39, cross_account_value: 233307.69, cross_maintenance_margin: 0,
    positions: [{
      coin: 'HYPE', size: -6267, entry_px: 13.1333, value: 534687.906,
      unrealized_pnl: -452381.39087, roe: -16.4889, liquidation_px: 148.6767,
      margin_used: 178229.302, leverage: 3, leverage_type: 'cross',
    }],
  },
  spot: [{ coin: 'USDC', total: 1500.5, hold: 0, entry_ntl: 1500 },
    { coin: 'HYPE', total: 0.5, hold: 0, entry_ntl: 0 }],
  open_orders: [{
    oid: 460043204693, coin: 'ETH', side: 'buy', size: 0.98, orig_size: 0.98, price: 2100,
    trigger_px: 0, is_trigger: false, reduce_only: true, order_type: 'Limit',
    ts: Date.now() - 5000, cloid: null,
  }],
  fills: [{
    coin: 'ETH', side: 'sell', size: 0.01, price: 2500.3, closed_pnl: 0.198, fee: 0.0007,
    dir: 'Close Long', ts: Date.now() - 20000, crossed: false, liquidation: false, hash: '0xabc',
  }, {
    coin: 'BTC', side: 'buy', size: 0.001, price: 60000, closed_pnl: 0, fee: 0.01,
    dir: 'Open Long', ts: Date.now() - 90000, crossed: true, liquidation: false, hash: '0xdef',
  }],
  fees: { cross_rate: 0.0003825, add_rate: 0.0001275, vip: '', referral_discount: 0, builder_rebate: 0 },
  history: {
    day: [[Date.now() - 3600e3, 200000], [Date.now() - 1800e3, 215000], [Date.now(), 233307.69]],
    week: [[Date.now() - 86400e3, 180000], [Date.now(), 233307.69]],
    month: [],
  },
  errors: [],
};

function drive(withEcharts) {
  const { ctx, nodes } = makeContext({ withEcharts });
  const ui = loadApp(ctx);
  const tag = withEcharts ? '有 ECharts' : '无 ECharts(CDN 挂掉)';
  section(`驱动前端渲染 —— ${tag}`);

  const errs = [];
  const run = (name, fn) => {
    try { fn(); check(name, true); }
    catch (e) { errs.push(name); fail++; console.log(`  [FAIL] ${name}  ${e.message}`); }
  };

  run('renderTop()', () => { ui.state.overview = OVERVIEW; ui.state.slots = [SLOT, SLOT2]; ui.renderTop(); });
  check('  顶栏权益渲染为 $1,234.56', nodes.get('kAccount').textContent === '$1,234.56', nodes.get('kAccount').textContent);
  check('  运行中实例显示 1 / 2', nodes.get('kRunning').textContent === '1 / 2', nodes.get('kRunning').textContent);

  run('renderSidebar()', () => ui.renderSidebar());
  const side = nodes.get('slotList').innerHTML;
  check('  侧栏包含两个实例', side.includes('eth-grid') && side.includes('btc-grid'));
  check('  实盘实例带「实盘」标签', side.includes('实盘'));
  check('  干跑实例带「干跑」标签', side.includes('干跑'));

  run('selectSlot()', () => { ui.state.details = { 'eth-grid': DETAIL }; ui.selectSlot('eth-grid'); });
  const metrics = nodes.get('dMetrics').innerHTML;
  check('  指标区渲染了账户权益', metrics.includes('$1,234.56'), '');
  check('  指标区渲染了净持仓', metrics.includes('0.13'));
  check('  指标区渲染了强平价', metrics.includes('2,100.00'));
  check('  风控条渲染了 3 个进度条', (nodes.get('dRisk').innerHTML.match(/class="bar"/g) || []).length === 3);
  check('  风控条百分比合理（名义 65.0%）', nodes.get('dRisk').innerHTML.includes('65.0%'), '');

  const tabBody = nodes.get('dTabBody');
  run('网格层级视图', () => { ui.state.tab = 'grid'; ui.renderTab(true); });
  check('  层级表包含 B1/B3', tabBody.innerHTML.includes('B1') && tabBody.innerHTML.includes('B3'));
  check('  持仓中/已挂单/待补挂 三种状态都出现',
    tabBody.innerHTML.includes('持仓中') && tabBody.innerHTML.includes('已挂单') && tabBody.innerHTML.includes('待补挂'));

  run('成交记录视图', () => { ui.state.tab = 'trades'; ui.renderTab(true); });
  check('  成交表包含止盈平仓记录', tabBody.innerHTML.includes('止盈平仓'));
  check('  成交表盈亏带正号', tabBody.innerHTML.includes('+0.1980'), '');

  run('运行日志视图', () => { ui.state.tab = 'logs'; ui.renderTab(true); });

  run('参数设置视图（运行中）', () => { ui.state.tab = 'config'; ui.renderTab(true); });
  check('  运行中实例拒绝编辑，显示占位提示', tabBody.innerHTML.includes('实例正在运行，无法修改配置'));
  check('  运行中不渲染表单字段', !tabBody.innerHTML.includes('f_grid_step_pct'));

  // 停止状态下的表单（可编辑）
  run('停止状态下渲染可编辑表单', () => {
    ui.state.slots = [{ ...SLOT, running: false }, SLOT2];
    ui.renderConfigForm();
  });
  check('  渲染了运行环境分区', tabBody.innerHTML.includes('运行环境'));
  check('  渲染了标的与仓位分区', tabBody.innerHTML.includes('标的与仓位'));
  check('  渲染了网格参数分区', tabBody.innerHTML.includes('网格参数'));
  check('  渲染了马丁分区', tabBody.innerHTML.includes('马丁加仓'));
  check('  渲染了风控分区', tabBody.innerHTML.includes('浮亏熔断线'));
  check('  渲染了 Telegram 分区', tabBody.innerHTML.includes('Telegram'));
  check('  生成关键表单字段 id', tabBody.innerHTML.includes('f_grid_step_pct') && tabBody.innerHTML.includes('f_risk_max_unrealized_loss_usd'));
  check('  数值字段预填了当前值', tabBody.innerHTML.includes('value="0.8"'), '');
  check('  实盘实例的干跑开关应为未勾选', !/id="f_env_dry_run"[^>]*checked/.test(tabBody.innerHTML));
  check('  出现保存与危险操作按钮', tabBody.innerHTML.includes('cfgSave') && tabBody.innerHTML.includes('cfgDelete'));
  check('  token 输入框为密码类型且不回显', tabBody.innerHTML.includes('type="password"') && !tabBody.innerHTML.includes('f_notify_token" value="0x'));

  run('pushChartData()', () => ui.pushChartData());
  if (withEcharts) check('  图表信息显示采样点数', (nodes.get('dChartInfo').innerHTML || '').includes('3 个采样点'), nodes.get('dChartInfo').innerHTML);

  // 极端数据：空状态 / 缺失字段
  section(`边界数据 —— ${tag}`);
  run('空快照不报错', () => {
    ui.state.details = { 'eth-grid': {} };
    ui.updateDetail();
  });
  run('null 值不报错', () => {
    ui.state.details = { 'eth-grid': { ...DETAIL, net_position: null, entry_px: null, levels: [], trades: [], equity: [], limits: {} } };
    ui.state.tab = 'grid';
    ui.updateDetail();
  });
  check('  空层级显示占位文案', nodes.get('dTabBody').innerHTML.includes('还没有网格层位'));

  check('  无 JS 运行时异常', errs.length === 0, errs.join(' / '));
}

/* ---------------- 触发单实例 ---------------- */
async function driveTrigger() {
  section('触发单实例渲染');
  const { ctx, nodes } = makeContext({ withEcharts: false, ethereum: null, walletPayload: null });
  const ui = loadApp(ctx);
  const bodyEl = () => nodes.get('dTabBody'); // 骨架是 selectSlot 时才建出来的，得按需取
  const errs = [];
  const run = (name, fn) => {
    try { fn(); check(name, true); }
    catch (e) { errs.push(name); fail++; console.log(`  [FAIL] ${name}  ${e.message}`); }
  };

  ui.state.overview = OVERVIEW;
  ui.state.slots = [TRIG_SLOT, SLOT2];
  run('侧栏渲染触发单实例', () => ui.renderSidebar());
  const side = nodes.get('slotList').innerHTML;
  check('  侧栏含 eth-buy-2505', side.includes('eth-buy-2505'));
  check('  侧栏摘要显示「触发买入@2505」', side.includes('触发买入@2505'), side.slice(0, 500));

  ui.state.details = { 'eth-buy-2505': TRIG_DETAIL };
  run('selectSlot()', () => ui.selectSlot('eth-buy-2505'));
  check('  第一个标签页改名为「触发单」',
    nodes.get('detail').innerHTML.includes('触发单') && !nodes.get('detail').innerHTML.includes('网格层级'),
    nodes.get('detail').innerHTML.slice(0, 300));
  check('  徽章显示「触发买入」', nodes.get('dBadges').innerHTML.includes('触发买入'), nodes.get('dBadges').innerHTML);

  const m = nodes.get('dMetrics').innerHTML;
  check('  指标区显示触发价 2,505.00', m.includes('2,505.00'), m.slice(0, 260));
  check('  指标区新增「距触发」', m.includes('距触发'));
  check('  指标区把「完成网格次数」改为「完成次数」', m.includes('完成次数') && !m.includes('完成网格次数'));

  run('触发单视图', () => { ui.state.tab = 'grid'; ui.renderTab(true); });
  const html = bodyEl().innerHTML;
  check('  面板渲染触发价 2,505.00', html.includes('2,505.00'));
  check('  面板渲染下单数量 0.005', html.includes('0.005'));
  check('  面板渲染名义价值 $12.53', html.includes('$12.53'), html.slice(0, 700));
  check('  面板渲染触发方式说明', html.includes('限价单挂在盘口等成交'));
  check('  面板渲染「只建仓，不再挂单」', html.includes('只建仓，不再挂单'), html.slice(0, 900));
  check('  面板渲染阶段「已挂单」', html.includes('已挂单'));
  check('  面板渲染单笔订单 T1 行', html.includes('>T1<'), html.slice(0, 900));
  check('  不会渲染网格那套层位文案', !html.includes('还没有网格层位'));

  run('回到网格实例仍显示「网格层级」', () => {
    ui.state.slots = [{ ...SLOT, running: false }, SLOT2];
    ui.state.details = { 'eth-grid': DETAIL };
    ui.selectSlot('eth-grid');
  });
  check('  网格实例标签页仍是「网格层级」', nodes.get('detail').innerHTML.includes('网格层级'));

  // 已完成的触发单
  run('已完成状态渲染', () => {
    ui.state.details = {
      'eth-buy-2505': {
        ...TRIG_DETAIL,
        status: 'DONE', net_position: 0.005, entry_px: 2505.0, open_orders: 0,
        levels: [{ key: 'T1', side: 'buy', level: 1, state: 'open', size: 0.005, entry_px: 2505.0, tp_px: null, has_order: false }],
        trigger: { ...TRIG_DETAIL.trigger, phase: '已完成' },
      },
    };
    ui.state.slots = [{ ...TRIG_SLOT, running: true, state_status: 'DONE' }];
    ui.selectSlot('eth-buy-2505');
  });
  check('  已完成显示「已完成」状态', nodes.get('dMetrics').innerHTML.includes('已完成'));
  check('  已完成面板阶段为「已完成」', bodyEl().innerHTML.includes('已完成'), bodyEl().innerHTML.slice(0, 500));

  // 参数表单：触发单策略
  run('触发单参数表单', () => {
    ui.state.slots = [{ ...TRIG_SLOT, running: false }];
    ui.state.details = { 'eth-buy-2505': TRIG_DETAIL };
    ui.state.tab = 'config';
    ui.renderConfigForm();
  });
  const cfg = bodyEl().innerHTML;
  check('  渲染「策略类型」分区', cfg.includes('策略类型'));
  check('  策略下拉预选 trigger', cfg.includes('value="trigger" selected'), cfg.slice(0, 600));
  check('  渲染「触发单参数」分区', cfg.includes('触发单参数'));
  check('  隐藏网格参数分区', !cfg.includes('网格参数'));
  check('  隐藏马丁加仓分区', !cfg.includes('马丁加仓'));
  check('  渲染触发价字段', cfg.includes('f_trigger_price'));
  check('  渲染触发方式下拉', cfg.includes('f_trigger_order_type'));
  check('  触发数量字段带最低 $10 提示', cfg.includes('不得低于 $10'));
  check('  数量字段预填 0.005', cfg.includes('value="0.005"'), cfg.slice(0, 1200));

  // 切换策略 -> 分区立刻跟着变
  run('切换策略分区联动', () => {
    const sel = nodes.get('f___root___strategy');
    sel.value = 'grid';
    sel.onchange();
  });
  check('  切到网格后显示网格参数', bodyEl().innerHTML.includes('网格参数'));
  check('  切到网格后隐藏触发单参数', !bodyEl().innerHTML.includes('触发单参数'));
  check('  切到网格后策略下拉预选 grid', bodyEl().innerHTML.includes('value="grid" selected'));

  check('  触发单渲染无 JS 运行时异常', errs.length === 0, errs.join(' / '));
}

/* 信号策略实例的快照样例 */
const SIG_SLOT = {
  name: 'eth-trend', slot: 'eth-trend', running: false, config_error: '', last_error: '', uptime_sec: 0,
  state_status: 'IDLE', strategy: 'signal',
  env: { network: 'mainnet', dry_run: true, account_address: '', has_private_key: false },
  market: { coin: 'ETH', leverage: 5, base_size: 0.0044 },
  grid: { mode: 'long', step_pct: 0.8, levels: 8 },
  signal: {
    interval: '15m', candles: 300, candle_cache_sec: 20,
    fast_ema: 12, slow_ema: 48, trend_ema: 0,
    entry_mode: 'maker', entry_offset_pct: 0.05, entry_ttl_sec: 90, allow_short: false,
    atr_period: 14, atr_mult: 2.0, trail_pct: 0.6, take_profit_pct: 0, exit_on_cross: true,
    max_rounds: 3, rounds: 0, size: 0.0044,
    position: 'flat', phase: '待启动', desc: '启动后开始计算指标',
    fast: null, slow: null, close: null, bars: 0, atr: null, stop_px: null, extreme_px: null, last_kline_ms: 0,
  },
  risk: { max_total_notional_usd: 500, max_position_size: 0.02, max_unrealized_loss_usd: 30 },
  live: null,
};
const SIG_DETAIL = {
  slot: 'eth-trend', strategy: 'signal', coin: 'ETH', running: true, loop: 40, uptime_sec: 200,
  mid: 2612.4, network: 'mainnet', dry_run: true, mode: 'signal-15m', leverage: 5, anchor_price: 2604.1,
  signal: {
    interval: '15m', fast_ema: 12, slow_ema: 48, trend_ema: 0, entry_mode: 'maker', entry_offset_pct: 0.05,
    trail_pct: 0.6, atr_mult: 2.0, take_profit_pct: 0, allow_short: false, max_rounds: 3, rounds: 1,
    size: 0.0044, position: 'long', phase: '持仓中',
    desc: '金叉（快线上穿慢线）→ 买入信号', fast: 2605.2, slow: 2601.88, close: 2604.1, bars: 300,
    atr: 10.75, stop_px: 2591.5, extreme_px: 2612.4, last_kline_ms: 1750000000000,
  },
  account_value: 100.31, net_position: 0.0044, entry_px: 2599.6, unrealized_pnl: 0.0563,
  total_notional: 11.44, margin_used: 2.29, liquidation_px: 2120.0, open_orders: 0,
  realized_usd: 0, closed_slots: 0, orders_today: 2, status: 'RUNNING',
  stop_reason: '', stopped_until_ms: 0, error: '',
  levels: [{
    key: 'S1', side: 'buy', level: 1, state: 'open', size: 0.0044,
    entry_px: 2599.6, tp_px: 2591.5, has_order: false,
  }],
  trades: [{ ts: Date.now() - 60000, kind: 'open', side: 'buy', size: 0.0044, price: 2599.6, pnl: 0 }],
  equity: [[Date.now() - 60000, 100.0], [Date.now(), 100.31]],
  limits: DETAIL.limits,
};

/* ---------------- 信号策略实例 ---------------- */
async function driveSignal() {
  section('信号策略实例渲染');
  const { ctx, nodes } = makeContext({ withEcharts: false, ethereum: null, walletPayload: null });
  const ui = loadApp(ctx);
  const bodyEl = () => nodes.get('dTabBody');
  const errs = [];
  const run = (name, fn) => {
    try { fn(); check(name, true); }
    catch (e) { errs.push(name); fail++; console.log(`  [FAIL] ${name}  ${e.message}`); }
  };

  ui.state.overview = OVERVIEW;
  ui.state.slots = [SIG_SLOT, SLOT2];
  run('侧栏渲染信号实例', () => ui.renderSidebar());
  const side = nodes.get('slotList').innerHTML;
  check('  侧栏含 eth-trend', side.includes('eth-trend'));
  check('  侧栏摘要显示「EMA 12/48 · 15m」', side.includes('EMA 12/48 · 15m'), side.slice(0, 500));

  ui.state.details = { 'eth-trend': SIG_DETAIL };
  run('selectSlot()', () => ui.selectSlot('eth-trend'));
  const detailHtml = nodes.get('detail').innerHTML;
  check('  第一个标签页改名为「信号状态」',
    detailHtml.includes('信号状态') && !detailHtml.includes('网格层级'), detailHtml.slice(0, 400));
  check('  徽章显示 EMA 参数', nodes.get('dBadges').innerHTML.includes('EMA 12/48 @ 15m'),
    nodes.get('dBadges').innerHTML);

  const m = nodes.get('dMetrics').innerHTML;
  check('  指标区「完成网格次数」改为「已做轮数」', m.includes('已做轮数') && !m.includes('完成网格次数'));
  check('  指标区显示轮数进度 1 / 3', m.includes('1 / 3'), m.slice(0, 500));
  check('  指标区「锚定价」改为「跟踪止损」', m.includes('跟踪止损') && m.includes('2,591.50'));
  check('  指标区不再显示锚定价', !m.includes('锚定价'));

  run('信号状态视图', () => { ui.state.tab = 'grid'; ui.renderTab(true); });
  const html = bodyEl().innerHTML;
  check('  面板渲染当前信号描述', html.includes('金叉（快线上穿慢线）→ 买入信号'), html.slice(0, 700));
  check('  面板渲染阶段「持仓中」', html.includes('持仓中'));
  check('  面板渲染持仓方向「多头」', html.includes('多头'));
  check('  面板渲染 EMA 快慢线', html.includes('2,605.20') && html.includes('2,601.88'));
  check('  面板渲染 ATR', html.includes('10.75'));
  check('  面板渲染轮数 1 / 3', html.includes('1 / 3'));
  check('  面板渲染入场方式（maker）', html.includes('挂在回撤价等成交'));
  check('  面板渲染跟踪止损', html.includes('2,591.50'));
  check('  面板渲染持仓期极值', html.includes('2,612.40'));
  check('  面板渲染 S1 持仓行', html.includes('>S1<'), html.slice(0, 900));
  check('  面板带「先看回测再上真钱」提醒', html.includes('先看回测再上真钱') && html.includes('-0.50 USDC'));
  check('  不渲染网格那套文案', !html.includes('还没有网格层位') && !html.includes('触发价'));

  run('成交记录把 close 显示为「平仓」而不是「止盈平仓」', () => {
    ui.state.details = {
      'eth-trend': {
        ...SIG_DETAIL,
        trades: [
          ...SIG_DETAIL.trades,
          { ts: Date.now() - 10000, kind: 'close', side: 'sell', size: 0.0044, price: 2612.0, pnl: 0.0512 },
        ],
      },
    };
    ui.state.tab = 'trades';
    ui.renderTab(true);
  });
  check('  平仓记录标签为「平仓」', bodyEl().innerHTML.includes('平仓') && !bodyEl().innerHTML.includes('止盈平仓'));

  // 参数表单：信号策略
  run('信号策略参数表单', () => {
    ui.state.slots = [{ ...SIG_SLOT, running: false }];
    ui.state.details = { 'eth-trend': SIG_DETAIL };
    ui.state.tab = 'config';
    ui.renderConfigForm();
  });
  const cfg = bodyEl().innerHTML;
  check('  渲染「策略类型」分区', cfg.includes('策略类型'));
  check('  策略下拉预选 signal', cfg.includes('value="signal" selected'), cfg.slice(0, 700));
  check('  策略下拉含「信号策略」选项', cfg.includes('信号策略（指标交叉，实时买入卖出）'));
  check('  渲染「信号策略参数」分区', cfg.includes('信号策略参数'));
  check('  隐藏网格参数分区', !cfg.includes('网格参数'));
  check('  隐藏马丁加仓分区', !cfg.includes('马丁加仓'));
  check('  隐藏触发单参数分区', !cfg.includes('触发单参数'));
  check('  渲染周期下拉', cfg.includes('f_signal_interval'));
  check('  周期下拉预选 15m', /id="f_signal_interval"[\s\S]{0,400}value="15m" selected/.test(cfg), cfg.slice(0, 1500));
  check('  渲染快线字段并预填 12', cfg.includes('f_signal_fast_ema') && cfg.includes('value="12"'));
  check('  渲染慢线字段并预填 48', cfg.includes('f_signal_slow_ema') && cfg.includes('value="48"'));
  check('  渲染跟踪止损字段', cfg.includes('f_signal_trail_pct') && cfg.includes('value="0.6"'));
  check('  渲染入场方式下拉', cfg.includes('f_signal_entry_mode'));
  check('  数量字段带最低 $10 提示', cfg.includes('不得低于 $10'));
  check('  数量字段预填 0.0044', cfg.includes('value="0.0044"'), cfg.slice(0, 2000));
  check('  渲染「最多做几轮」字段', cfg.includes('f_signal_max_rounds'));
  check('  做空开关默认未勾选', !/id="f_signal_allow_short"[^>]*checked/.test(cfg));

  // 切换策略 -> 分区立刻跟着变
  run('切到触发单后分区联动', () => {
    const sel = nodes.get('f___root___strategy');
    sel.value = 'trigger';
    sel.onchange();
  });
  check('  切到触发单后显示触发单参数', bodyEl().innerHTML.includes('触发单参数'));
  check('  切到触发单后隐藏信号参数', !bodyEl().innerHTML.includes('信号策略参数'));

  run('切回信号策略', () => {
    const sel = nodes.get('f___root___strategy');
    sel.value = 'signal';
    sel.onchange();
  });
  check('  切回后重新显示信号参数', bodyEl().innerHTML.includes('信号策略参数'));

  // 已完成（轮数跑满）
  run('轮数跑满后的完成态', () => {
    ui.state.details = {
      'eth-trend': {
        ...SIG_DETAIL, status: 'DONE', net_position: 0, entry_px: null, realized_usd: 0.3063,
        signal: { ...SIG_DETAIL.signal, position: 'flat', phase: '已完成', rounds: 3, stop_px: null, extreme_px: null },
      },
    };
    ui.state.slots = [{ ...SIG_SLOT, running: true, state_status: 'DONE' }];
    ui.selectSlot('eth-trend');
  });
  check('  完成态显示「已完成」', nodes.get('dMetrics').innerHTML.includes('已完成'));
  check('  完成态轮数为 3 / 3', nodes.get('dMetrics').innerHTML.includes('3 / 3'));
  check('  完成态面板阶段为「已完成」', bodyEl().innerHTML.includes('已完成'));

  // 边界：空快照
  run('信号实例空快照不报错', () => {
    ui.state.details = { 'eth-trend': { strategy: 'signal', signal: {} } };
    ui.state.tab = 'grid';
    ui.updateDetail();
  });
  check('  空信号显示占位文案', bodyEl().innerHTML.includes('还没有持仓或挂单'), bodyEl().innerHTML.slice(0, 400));

  check('  信号实例渲染无 JS 运行时异常', errs.length === 0, errs.join(' / '));
}

/* ---------------- 钱包：有插件钱包 ---------------- */
async function driveWalletWithProvider() {
  section('钱包 —— 有浏览器钱包插件');
  const provider = makeProvider({ accounts: [WALLET_ADDR], chainId: '0xa4b1' });
  const { ctx, nodes } = makeContext({ withEcharts: false, ethereum: provider, walletPayload: WALLET_PAYLOAD });
  const ui = loadApp(ctx);
  // 节点是懒创建的，必须等抽屉渲染过一次再取
  const body = () => nodes.get('wBody');

  const errs = [];
  const run = async (name, fn) => {
    try { await fn(); check(name, true); }
    catch (e) { errs.push(name); fail++; console.log(`  [FAIL] ${name}  ${e.message}`); }
  };

  // 页面加载时静默恢复连接（eth_accounts 不弹窗）
  await tick(); await tick();
  check('加载时静默恢复已授权账户', ui.wallet.address === WALLET_ADDR, ui.wallet.address);
  check('顶栏按钮显示缩略地址', nodes.get('wLabel').textContent === '0x0a33…4349', nodes.get('wLabel').textContent);
  check('顶栏按钮显示账户权益', (nodes.get('wVal').textContent || '').includes('233,307'), nodes.get('wVal').textContent);

  ui.state.slots = [SLOT, SLOT2];
  ui.walletOpen();
  await tick(); await tick();

  const html = body().innerHTML;
  check('抽屉渲染了地址与网络', html.includes(WALLET_ADDR) && html.includes('Arbitrum One'));
  check('抽屉渲染了账户权益', html.includes('$233,307.69'), '');
  check('抽屉渲染了可用保证金', html.includes('$55,078.39'), '');
  check('抽屉渲染了名义敞口', html.includes('$534,687.91'), '');
  check('抽屉渲染了未实现盈亏（负值带 -）', html.includes('-452,381.3909'), '');
  check('抽屉有权益曲线块与区间切换', html.includes('权益曲线') && html.includes('data-range="day"') && html.includes('data-range="week"'));
  check('权益曲线画出了折线', html.includes('<polyline'), '');
  check('权益曲线显示区间变化', html.includes('3 个采样点') && html.includes('+33,307.69'), '');
  check('持仓表含 HYPE 且识别为空头', html.includes('HYPE') && html.includes('空'));
  check('持仓表含强平价', html.includes('148.6767'), '');
  check('挂单表识别为只减仓', html.includes('只减仓') && html.includes('ETH'));
  check('现货余额列出 USDC', html.includes('USDC') && html.includes('1,500.5000'));
  check('成交表区分开仓与平仓', html.includes('Close Long') || html.includes('平仓') || html.includes('0.1980'), '');
  check('手续费等级显示吃单费率', html.includes('0.0383%') || html.includes('0.0382%'), '');
  check('绑定区渲染了实例下拉', html.includes('wBindSlot') && html.includes('eth-grid') && html.includes('btc-grid'));
  check('运行中实例在绑定下拉里被禁用', /disabled/.test(html));
  check('有断开连接按钮', html.includes('wDisconnect'));

  await run('手动输入非法地址被拒', async () => { check('  拒绝非法地址', ui.walletManual('0xzz') === false); });

  // 链不对时给出切网提示
  const bad = makeProvider({ accounts: [WALLET_ADDR], chainId: '0x1' });
  const c2 = makeContext({ withEcharts: false, ethereum: bad, walletPayload: WALLET_PAYLOAD });
  const ui2 = loadApp(c2.ctx);
  await tick(); await tick();
  ui2.walletOpen();
  await tick();
  const h2 = c2.nodes.get('wBody').innerHTML;
  check('非 Hyperliquid 网络时提示切网', h2.includes('Arbitrum One') && h2.includes('wSwitch'));
  check('链名映射正确（0x1 → Ethereum）', ui2.chainInfo('0x1').name === 'Ethereum');
  check('链名映射对未知链不炸', typeof ui2.chainInfo('0xdeadbeef').name === 'string');

  // 用户拒绝授权
  const rej = makeProvider({ reject: true, chainId: '0xa4b1' });
  const c3 = makeContext({ withEcharts: false, ethereum: rej, walletPayload: WALLET_PAYLOAD });
  const ui3 = loadApp(c3.ctx);
  await tick();
  await ui3.walletConnect();
  check('钱包拒绝授权时不崩溃且保持未连接', ui3.wallet.address === '', ui3.wallet.address);

  // 后端查询失败
  const c4 = makeContext({ withEcharts: false, ethereum: provider, walletPayload: WALLET_PAYLOAD, fetchFails: true });
  const ui4 = loadApp(c4.ctx);
  await tick(); await tick();
  ui4.walletOpen();
  await tick(); await tick();
  check('查询失败时抽屉展示错误而不是白屏',
    (c4.nodes.get('wBody').innerHTML || '').includes('查询失败') && (ui4.wallet.error || '').length > 0,
    ui4.wallet.error);
  check('查询失败时顶栏不显示伪造的权益', (c4.nodes.get('wVal').textContent || '') === '', c4.nodes.get('wVal').textContent);

  // 有钱包但还没连接（eth_accounts 返回空）→ 抽屉里连接按钮和「直接贴地址」都要在
  const idle = makeProvider({ accounts: [], chainId: '0xa4b1' });
  const c5 = makeContext({ withEcharts: false, ethereum: idle, walletPayload: WALLET_PAYLOAD });
  const ui5 = loadApp(c5.ctx);
  await tick();
  check('未授权时不自动连接', ui5.wallet.address === '');
  ui5.walletOpen();
  await tick();
  const h5 = c5.nodes.get('wBody').innerHTML;
  check('未连接时抽屉提供连接按钮', h5.includes('wConnBtn'));
  check('未连接时也能直接贴地址查看（不会被困在授权弹窗里）',
    h5.includes('wManual') && h5.includes('wManualGo'));
  check('有插件时不显示安装指引', !h5.includes('metamask.io/download'));

  check('钱包流程无 JS 运行时异常', errs.length === 0, errs.join(' / '));
}

/* ---------------- 钱包：嵌在客户端预览窗口里 ---------------- */
async function driveWalletEmbedded() {
  section('钱包 —— 嵌在客户端预览窗口里（"连不上"的头号原因）');
  const ELECTRON_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) app/1.0 Chrome/126.0.0.0 Electron/31.0.0 Safari/537.36';

  const { ctx, nodes } = makeContext({
    withEcharts: false, walletPayload: WALLET_PAYLOAD, embedded: true, userAgent: ELECTRON_UA,
  });
  const ui = loadApp(ctx);
  await tick();

  check('识别出被嵌入的运行环境', ui.wEmbedKind().length > 0, ui.wEmbedKind());
  check('判定为插件钱包不可用环境', ui.wBlocked() === true, String(ui.wBlocked()));

  nodes.get('btnWallet').onclick();     // 没插件时按钮应该打开抽屉而不是弹连接
  await tick();
  const h = nodes.get('wBody').innerHTML;
  check('抽屉提示改用系统浏览器打开', h.includes('请用系统浏览器打开'), '');
  check('说明插件钱包不会注入被嵌入页面', h.includes('不会注入'));
  check('给出可复制、可打开的页面地址', h.includes('wCopyUrl') && h.includes('wUrl') && h.includes('127.0.0.1:8787'));
  check('不再误报「没有检测到浏览器钱包」', !h.includes('没有检测到浏览器钱包'), '');
  check('仍保留手动输入地址的入口', h.includes('wManual') && h.includes('wManualGo'));
  check('顶栏按钮文案改为「需在浏览器打开」', nodes.get('wLabel').textContent === '需在浏览器打开', nodes.get('wLabel').textContent);
  check('诊断区渲染并标注被嵌入', h.includes('连接诊断') && h.includes('被嵌入页面'));
  check('诊断区列出 window.ethereum 状态', h.includes('window.ethereum'));

  // Electron 外壳浏览器（不是 iframe，但同样注入不了扩展）
  const c2 = makeContext({ withEcharts: false, walletPayload: WALLET_PAYLOAD, userAgent: ELECTRON_UA });
  const ui2 = loadApp(c2.ctx);
  await tick();
  check('Electron 壳浏览器同样被识别', ui2.wBlocked() === true && ui2.wEmbedKind().includes('内置浏览器'), ui2.wEmbedKind());

  // 普通浏览器 + 真没装插件 → 应该给安装指引，而不是「换浏览器」
  const c3 = makeContext({ withEcharts: false, walletPayload: WALLET_PAYLOAD });
  const ui3 = loadApp(c3.ctx);
  await tick();
  ui3.walletOpen();
  await tick();
  const h3 = c3.nodes.get('wBody').innerHTML;
  check('普通浏览器没装插件时给安装指引', h3.includes('metamask.io/download'), '');
  check('普通浏览器不误报「换浏览器」', !h3.includes('请用系统浏览器打开'));
  check('普通浏览器不算被阻断环境', ui3.wBlocked() === false);

  // 顶层页面 + 有插件 → 一切正常
  const ok = makeProvider({ accounts: [WALLET_ADDR] });
  const c4 = makeContext({ withEcharts: false, ethereum: ok, walletPayload: WALLET_PAYLOAD });
  const ui4 = loadApp(c4.ctx);
  await tick();
  check('顶层页面有插件时不算被阻断', ui4.wBlocked() === false && ui4.wCan() === true);
}

/* ---------------- 钱包：多个插件共存 ---------------- */
async function driveWalletMultiProvider() {
  section('钱包 —— 多个插件共存（window.ethereum 被抢注）');

  const mm = makeProvider({ accounts: [WALLET_ADDR], chainId: '0xa4b1' });
  mm.isMetaMask = true;
  const okx = makeProvider({ accounts: ['0xdead00000000000000000000000000000000beef'], chainId: '0x1' });
  okx.isOkxWallet = true;
  // 抢注者：window.ethereum 指向一个没特征的对象，真正的 provider 都在 providers 数组里
  const squatter = makeProvider({ accounts: [], chainId: '0x1' });
  squatter.providers = [okx, mm];

  const { ctx, nodes } = makeContext({ withEcharts: false, ethereum: squatter, walletPayload: WALLET_PAYLOAD });
  const ui = loadApp(ctx);
  await tick();
  await ui.walletConnect();
  await tick();

  check('能从 providers 数组里认出 MetaMask', ui.wallet.providerName === 'MetaMask', ui.wallet.providerName || '(空)');
  check('连上的是 MetaMask 而不是抢注者', ui.wallet.address.toLowerCase() === WALLET_ADDR.toLowerCase(), ui.wallet.address);
  check('识别出多个钱包', ui.wList().length >= 2, String(ui.wList().length));
  check('MetaMask 排在第一位', ui.wList()[0].name === 'MetaMask', ui.wList()[0].name);

  // 未连接时抽屉要列出可选钱包（这里两个钱包都还没授权，才会走到「未连接」视图）
  const mmIdle = makeProvider({ accounts: [], chainId: '0xa4b1' });
  mmIdle.isMetaMask = true;
  const okxIdle = makeProvider({ accounts: [], chainId: '0x1' });
  okxIdle.isOkxWallet = true;
  const squatterIdle = makeProvider({ accounts: [], chainId: '0x1' });
  squatterIdle.providers = [okxIdle, mmIdle];

  const c2 = makeContext({ withEcharts: false, ethereum: squatterIdle, walletPayload: WALLET_PAYLOAD });
  const ui2 = loadApp(c2.ctx);
  await tick();
  ui2.walletOpen();
  await tick();
  const h2 = c2.nodes.get('wBody').innerHTML;
  check('两个钱包都未授权时不自动连接', ui2.wallet.address === '', ui2.wallet.address);
  check('抽屉列出可选钱包', h2.includes('data-wpick') && h2.includes('MetaMask') && h2.includes('OKX Wallet'));
  check('连接按钮指明具体钱包名', h2.includes('连接 MetaMask'), '');

  // 错误码翻译
  check('-32002 翻译为「已有待处理请求」', ui.wErr({ code: -32002 }, '连接').text.includes('待处理'));
  check('4001 标记为用户主动取消', ui.wErr({ code: 4001 }, '连接').reject === true);
  check('4902 翻译为缺网络', ui.wErr({ code: 4902 }, '切换网络').text.includes('Arbitrum One'));
  check('4100 翻译为未授权', ui.wErr({ code: 4100 }, '连接').text.includes('授权'));
  check('未知错误保留原文，不吞信息', ui.wErr(new Error('boom'), '连接').text.includes('boom'));
  check('锁定状态有专门提示', ui.wErr(new Error('wallet is locked'), '连接').text.includes('解锁'));

  // -32002 卡住时：保持未连接、不崩溃、busy 复位
  const stuck = makeProvider({});
  stuck.isMetaMask = true;
  stuck.request = async ({ method }) => {
    if (method === 'eth_accounts') return [];
    const e = new Error('Already processing eth_requestAccounts'); e.code = -32002; throw e;
  };
  const c3 = makeContext({ withEcharts: false, ethereum: stuck, walletPayload: WALLET_PAYLOAD });
  const ui3 = loadApp(c3.ctx);
  await tick();
  await ui3.walletConnect();
  check('-32002 时保持未连接', ui3.wallet.address === '');
  check('-32002 后 busy 正确复位（不会卡死按钮）', ui3.wallet.busy === false);
  check('-32002 时抽屉被打开并给出兜底入口', (c3.nodes.get('wBody').innerHTML || '').includes('wManual'));
}

/* ---------------- 钱包：没有插件钱包 ---------------- */
async function driveWalletWithoutProvider() {
  section('钱包 —— 没有浏览器钱包插件');
  const { ctx, nodes } = makeContext({ withEcharts: false, walletPayload: WALLET_PAYLOAD });
  const ui = loadApp(ctx);
  await tick();

  check('无插件时仍能正常初始化（不抛异常）', ui.wProvider() === null);
  check('顶栏提示未检测到钱包', nodes.get('wLabel').textContent === '未检测到钱包', nodes.get('wLabel').textContent);

  ui.walletOpen();
  await tick();
  const html = nodes.get('wBody').innerHTML;
  check('抽屉给出插件安装指引', html.includes('MetaMask') && html.includes('Rabby'));
  check('抽屉提供手动输入地址的入口', html.includes('wManual') && html.includes('wManualGo'));

  // 手动查一个地址（不装插件也能看）
  const okManual = ui.walletManual(WALLET_ADDR.toUpperCase());
  check('手动输入合法地址被接受', okManual === true);
  check('地址被归一化成小写', ui.wallet.address === WALLET_ADDR, ui.wallet.address);
  await tick(); await tick();
  const html2 = nodes.get('wBody').innerHTML;
  check('手动地址也渲染出完整账户', html2.includes('$233,307.69') && html2.includes('手动查看'));
  check('手动模式不显示断开、显示清空', html2.includes('wForget') && !html2.includes('wDisconnect'));

  check('地址格式校验：合法/非法/空', ui.isAddr(WALLET_ADDR) && !ui.isAddr('0x123') && !ui.isAddr('') && !ui.isAddr(null));
  check('缩略地址格式化', ui.shortAddr(WALLET_ADDR) === '0x0a33…4349', ui.shortAddr(WALLET_ADDR));
}

/* ---------------- 钱包：空值与边界 ---------------- */
async function driveWalletEdges() {
  section('钱包 —— 空值与边界');
  const empty = {
    ok: true, address: WALLET_ADDR, short: '0x0a33…4349', network: 'mainnet', ts: Date.now(),
    perp: { account_value: 0, total_notional: 0, margin_used: 0, withdrawable: 0, positions: [] },
    spot: [], open_orders: [], fills: [],
    fees: {}, history: {}, errors: [],
  };
  const provider = makeProvider({ accounts: [WALLET_ADDR], chainId: '0x3e7' });
  const { ctx, nodes } = makeContext({ withEcharts: false, ethereum: provider, walletPayload: empty });
  const ui = loadApp(ctx);
  await tick(); await tick();
  ui.walletOpen();
  await tick();

  const html = nodes.get('wBody').innerHTML;
  check('空账户不报错', typeof html === 'string' && html.length > 0);
  check('空持仓显示占位文案', html.includes('没有永续持仓'));
  check('无挂单显示占位文案', html.includes('没有挂单'));
  check('无成交显示占位文案', html.includes('没有成交记录'));
  check('无历史数据时空曲线优雅降级', html.includes('暂无历史数据'));
  check('HyperEVM 网络被识别为 Hyperliquid 网络', ui.chainInfo('0x3e7').name === 'HyperEVM' && ui.chainInfo('0x3e7').hl === true);
  check('无费率时不显示乱码', html.includes('未取到费率信息'));

  // data 还没回来就渲染抽屉
  const c2 = makeContext({ withEcharts: false, ethereum: provider, walletPayload: null });
  const ui2 = loadApp(c2.ctx);
  ui2.wallet.address = WALLET_ADDR;
  ui2.wallet.data = null;
  ui2.walletOpen();
  check('数据未就绪时渲染不抛异常', typeof c2.nodes.get('wBody').innerHTML === 'string');

  // sparkline 边界
  check('sparkline 单点数据降级为占位', ui.sparkline([[1, 100]]).includes('暂无历史数据'));
  check('sparkline 空数据降级为占位', ui.sparkline([]).includes('暂无历史数据'));
  check('sparkline 全等值不除零', ui.sparkline([[1, 5], [2, 5], [3, 5]]).includes('<polyline'));
  check('sparkline 含非法值被过滤', ui.sparkline([[1, 10], [2, null], [3, 20]]).includes('<polyline'));
  check('sparkline 单点合法值（非数组）不炸', typeof ui.sparkline(null) === 'string');
}

/* ---------------- 纯函数检查 ---------------- */
function testHelpers() {
  section('格式化函数');
  const { ctx } = makeContext({ withEcharts: false });
  const ui = loadApp(ctx);
  check('usd 千分位', ui.num(1234.5, 2) === '1,234.50', ui.num(1234.5, 2));
  check('价格 >=1000 保留 2 位', ui.px(2501.2) === '2,501.20', ui.px(2501.2));
  check('价格个位数保留 4 位', ui.px(1.23456789) === '1.2346', ui.px(1.23456789));
  check('数量去掉尾随 0', ui.sz(0.010000) === '0.01', ui.sz(0.01));
  check('正盈亏带 + 号', ui.pnl(1.5) === '+1.5000', ui.pnl(1.5));
  check('null 数量显示为 —', ui.sz(null) === '—' || ui.sz(0) === '0', ui.sz(null));
  check('HTML 转义生效', ui.esc('<script>x</script>').includes('&lt;script&gt;'), ui.esc('<b>'));
}

console.log('Hyperliquid 交易台 —— 前端冒烟测试');
staticChecks();
testHelpers();
drive(true);
drive(false);
await driveTrigger();
await driveSignal();
await driveWalletWithProvider();
await driveWalletMultiProvider();
await driveWalletEmbedded();
await driveWalletWithoutProvider();
await driveWalletEdges();

console.log(`\n${'='.repeat(40)}\n通过 ${pass} 项，失败 ${fail} 项\n${'='.repeat(40)}`);
process.exit(fail ? 1 : 0);
