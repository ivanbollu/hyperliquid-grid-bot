/* Hyperliquid 网格交易台 —— 前端逻辑（无框架，纯 DOM） */

'use strict';

const state = {
  slots: [],
  overview: {},
  details: {},
  selected: null,
  tab: 'grid',
  fullEquity: null,      // 选中实例的完整权益曲线
  chart: null,
  chartBroken: false,
  autoScrollLogs: true,
  connected: false,
};

/* ==================== 工具 ==================== */
const $ = (id) => document.getElementById(id);

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
function num(v, d = 2) {
  const n = Number(v);
  if (!isFinite(n)) return '—';
  return n.toLocaleString('zh-CN', { minimumFractionDigits: d, maximumFractionDigits: d });
}
function usd(v) { return '$' + num(v, 2); }
function pnl(v) { const n = Number(v) || 0; return (n > 0 ? '+' : '') + num(n, 4); }
function pnlCls(v) { const n = Number(v) || 0; return n > 0 ? 'up' : (n < 0 ? 'down' : 'dim'); }
function px(v) {
  const n = Number(v);
  if (!isFinite(n) || n === 0) return '—';
  if (n >= 1000) return n.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  if (n >= 1) return n.toFixed(4);
  return n.toPrecision(5);
}
function sz(v) { const n = Number(v); return isFinite(n) ? n.toFixed(6).replace(/0+$/, '').replace(/\.$/, '') : '—'; }
function tstr(ms) {
  if (!ms) return '—';
  const d = new Date(Number(ms));
  const p = (x) => String(x).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
function hms(ms) {
  const d = new Date(Number(ms));
  const p = (x) => String(x).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function toast(msg, kind = '') {
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.textContent = msg;
  $('toasts').appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; el.style.transition = 'opacity .3s'; }, 4200);
  setTimeout(() => el.remove(), 4600);
}

function confirmDialog(opts) {
  return new Promise((resolve) => {
    const box = $('modalBox');
    box.innerHTML = `
      <h3>${esc(opts.title || '确认操作')}</h3>
      <p>${opts.body || ''}</p>
      <div class="actions">
        <button class="btn" id="mdCancel">取消</button>
        <button class="btn ${opts.danger ? 'danger' : 'primary'}" id="mdOk">${esc(opts.okText || '确认')}</button>
      </div>`;
    $('modal').classList.remove('hidden');
    const close = (v) => { $('modal').classList.add('hidden'); resolve(v); };
    $('mdCancel').onclick = () => close(false);
    $('mdOk').onclick = () => close(true);
    $('modal').onclick = (e) => { if (e.target === $('modal')) close(false); };
  });
}

function promptDialog(opts) {
  return new Promise((resolve) => {
    const box = $('modalBox');
    box.innerHTML = `
      <h3>${esc(opts.title || '输入')}</h3>
      <p>${opts.body || ''}</p>
      <input type="text" id="mdInput" placeholder="${esc(opts.placeholder || '')}" value="${esc(opts.value || '')}" />
      <div class="actions">
        <button class="btn" id="mdCancel">取消</button>
        <button class="btn primary" id="mdOk">${esc(opts.okText || '确定')}</button>
      </div>`;
    $('modal').classList.remove('hidden');
    const inp = $('mdInput');
    inp.focus(); inp.select();
    const close = (v) => { $('modal').classList.add('hidden'); resolve(v); };
    $('mdCancel').onclick = () => close(null);
    $('mdOk').onclick = () => close(inp.value.trim() || null);
    inp.onkeydown = (e) => { if (e.key === 'Enter') close(inp.value.trim() || null); };
    $('modal').onclick = (e) => { if (e.target === $('modal')) close(null); };
  });
}

async function api(method, path, body) {
  const opt = { method, headers: {} };
  if (body !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  let data = {};
  try { data = await r.json(); } catch (e) { /* ignore */ }
  return { ok: r.ok && (data.ok !== false), message: data.message || (r.ok ? '完成' : `请求失败 (${r.status})`), data };
}

/* ==================== 实时流 ==================== */
function connect() {
  const es = new EventSource('/api/stream');

  es.onopen = () => {
    state.connected = true;
    $('connState').className = 'sub online';
    $('connState').textContent = '实时连接正常';
  };
  es.onerror = () => {
    state.connected = false;
    $('connState').className = 'sub offline';
    $('connState').textContent = '连接中断，正在重连…';
  };
  es.onmessage = (ev) => {
    let d;
    try { d = JSON.parse(ev.data); } catch (e) { return; }
    if (d.error) { $('connState').textContent = '服务端错误：' + d.error; return; }
    state.overview = d.overview || {};
    state.slots = d.slots || [];
    state.details = d.detail || {};
    renderTop();
    renderSidebar();
    if (state.selected) {
      if (!state.slots.some((s) => s.name === state.selected)) {
        state.selected = null;
        $('detail').innerHTML = '<div class="empty big">该实例已被删除，请从左侧重新选择</div>';
      } else {
        updateDetail();
      }
    }
  };
}

/* ==================== 顶栏 ==================== */
function renderTop() {
  const o = state.overview || {};
  $('kAccount').textContent = o.slots_running ? usd(o.account_value) : '—';
  const up = $('kUpnl');
  up.textContent = o.slots_running ? pnl(o.unrealized_pnl) : '—';
  up.className = o.slots_running ? pnlCls(o.unrealized_pnl) : '';
  const rz = $('kRealized');
  rz.textContent = o.slots_running ? pnl(o.realized_usd) : '—';
  rz.className = o.slots_running ? pnlCls(o.realized_usd) : '';
  $('kRunning').textContent = `${o.slots_running || 0} / ${o.slots_total || 0}`;

  const missingKey = state.slots.length > 0 && state.slots.every((s) => !((s.env || {}).has_private_key));
  $('keyWarn').classList.toggle('hidden', !missingKey);
}

/* ==================== 侧栏 ==================== */
function slotDotClass(s) {
  if (s.last_error) return 'error';
  if (s.running) return 'running';
  if (s.state_status === 'STOPPED') return 'stopped';
  if (s.state_status === 'DONE') return 'done';
  return 'idle';
}

/** 侧栏副标题：网格显示方向，触发单显示「触发 买入@2505」，信号显示「EMA 12/48 · 15m」。 */
function slotSummary(s) {
  if (s.strategy === 'trigger') {
    const t = s.trigger || {};
    const side = t.side === 'sell' ? '卖出' : '买入';
    return t.price ? `触发${side}@${t.price}` : '触发单';
  }
  if (s.strategy === 'signal') {
    const g = s.signal || {};
    return g.interval ? `EMA ${g.fast_ema}/${g.slow_ema} · ${g.interval}` : '信号策略';
  }
  return (s.grid || {}).mode || '';
}

function renderSidebar() {
  const box = $('slotList');
  if (!state.slots.length) {
    box.innerHTML = '<div class="empty">还没有交易实例。<br>点右上角「新建实例」创建一个。</div>';
    return;
  }
  box.innerHTML = state.slots.map((s) => {
    const live = s.live || {};
    const dry = (s.env || {}).dry_run !== false;
    const active = s.name === state.selected ? ' active' : '';
    const line2 = s.live
      ? `<div class="row2"><span class="dim">${esc(s.market?.coin || '')} ${px(live.mid)}</span>
         <span class="${pnlCls(live.unrealized_pnl)}">${pnl(live.unrealized_pnl)}</span></div>
         <div class="row2"><span class="faint">净仓 ${sz(live.net_position)}</span>
         <span class="faint">已实现 ${num(live.realized_usd, 2)}</span></div>`
      : `<div class="row2"><span class="faint">${esc(s.market?.coin || '未配置')} · ${esc(slotSummary(s))}</span>
         <span class="faint">${s.state_status === 'DONE' ? '已完成' : '未运行'}</span></div>`;
    return `
      <div class="slotCard${active}" data-slot="${esc(s.name)}">
        <div class="row1">
          <span class="dot ${slotDotClass(s)}"></span>
          <span class="nm">${esc(s.name)}</span>
          <span class="badge ${dry ? 'paper' : 'live'}">${dry ? '干跑' : '实盘'}</span>
        </div>
        ${line2}
      </div>`;
  }).join('');

  box.querySelectorAll('.slotCard').forEach((el) => {
    el.onclick = () => selectSlot(el.dataset.slot);
  });
}

function selectSlot(name) {
  state.selected = name;
  state.fullEquity = null;
  state.tab = 'grid';
  state.autoScrollLogs = true;
  buildDetailSkeleton();
  updateDetail();
  renderSidebar();
  loadEquity();
}

/* ==================== 详情骨架 ==================== */
function buildDetailSkeleton() {
  // 触发单 / 信号策略的第一个标签页都不叫「网格层级」
  const cur = (state.slots.find((x) => x.name === state.selected) || {});
  const firstTab = cur.strategy === 'trigger' ? '触发单' : (cur.strategy === 'signal' ? '信号状态' : '网格层级');
  $('detail').innerHTML = `
    <div class="panel">
      <div class="panelHead">
        <span class="dot idle" id="dDot"></span>
        <h2 id="dName">—</h2>
        <span id="dBadges"></span>
        <span class="spacer"></span>
        <button class="btn ok sm" id="dStart">▶ 启动</button>
        <button class="btn sm" id="dStop">⏸ 停止</button>
        <button class="btn sm" id="dCancel">✕ 撤单</button>
        <button class="btn danger sm" id="dClose">■ 平仓</button>
      </div>
      <div class="metrics" id="dMetrics"></div>
      <div class="riskBars" id="dRisk"></div>
    </div>

    <div class="panel">
      <div class="panelHead">
        <h2>收益曲线</h2>
        <span class="spacer"></span>
        <span class="dim" id="dChartInfo"></span>
        <button class="btn ghost sm" id="dChartRefresh">刷新</button>
      </div>
      <div class="panelBody"><div id="chart"></div></div>
    </div>

    <div class="panel">
      <div class="tabs" id="dTabs">
        <div class="tab active" data-tab="grid">${firstTab}</div>
        <div class="tab" data-tab="trades">成交记录</div>
        <div class="tab" data-tab="logs">运行日志</div>
        <div class="tab" data-tab="config">参数设置</div>
      </div>
      <div class="tabBody" id="dTabBody"></div>
    </div>`;

  $('dStart').onclick = () => actStart();
  $('dStop').onclick = () => actStop();
  $('dCancel').onclick = () => actCancel();
  $('dClose').onclick = () => actClose();
  $('dChartRefresh').onclick = () => { state.fullEquity = null; loadEquity(); };

  $('dTabs').querySelectorAll('.tab').forEach((t) => {
    t.onclick = () => {
      state.tab = t.dataset.tab;
      $('dTabs').querySelectorAll('.tab').forEach((x) => x.classList.toggle('active', x === t));
      renderTab(true);
    };
  });

  initChart();
}

/* ==================== 详情刷新 ==================== */
function updateDetail() {
  const s = state.slots.find((x) => x.name === state.selected);
  const d = state.details[state.selected] || {};
  if (!s) return;

  $('dDot').className = 'dot ' + slotDotClass(s);
  $('dName').textContent = s.name + '  ·  ' + (s.market?.coin || '');

  const dry = (s.env || {}).dry_run !== false;
  const isTrig = s.strategy === 'trigger';
  const isSig = s.strategy === 'signal';
  const modeName = isTrig
    ? `触发${(s.trigger || {}).side === 'sell' ? '卖出' : '买入'}`
    : isSig
      ? `EMA ${((d.signal || s.signal || {}).fast_ema)}/${((d.signal || s.signal || {}).slow_ema)} @ ${((d.signal || s.signal || {}).interval)}`
      : ({ long: '做多网格', short: '做空网格', neutral: '双向网格' }[s.grid?.mode] || s.grid?.mode || '');
  $('dBadges').innerHTML =
    `<span class="badge ${dry ? 'paper' : 'live'}">${dry ? '干跑模拟' : '实盘真钱'}</span>
     <span class="badge mode">${esc(modeName)}</span>
     <span class="badge mode">${esc(s.env?.network || '')}</span>
     <span class="badge mode">${num(s.market?.leverage, 0)}x</span>
     ${s.running ? `<span class="badge mode">运行 ${Math.round(s.uptime_sec || 0)}s</span>` : ''}
     ${s.last_error ? `<span class="badge live">错误</span>` : ''}`;

  const running = !!s.running;
  $('dStart').disabled = running;
  $('dStop').disabled = !running;
  $('dCancel').disabled = !d.loop && !s.live;
  $('dClose').disabled = false;

  const st = d.status || s.state_status || 'IDLE';
  const stMap = {
    RUNNING: ['running', '运行中'], STOPPED: ['stopped', '已熔断停机'],
    IDLE: ['idle', '未启动'], DONE: ['done', '已完成'],
  };
  const [cls, txt] = stMap[st] || ['idle', st];
  const tg = d.trigger || s.trigger || {};
  const sg = d.signal || s.signal || {};
  const distPct = Number(tg.distance_pct);
  const distCls = !isFinite(distPct) || distPct === 0 ? 'dim' : (distPct < 0 ? 'up' : 'dim');
  const roundsLabel = sg.max_rounds > 0 ? `${sg.rounds || 0} / ${sg.max_rounds}` : `${sg.rounds || 0} / 不限`;

  $('dMetrics').innerHTML = `
    <div class="m"><label>账户权益</label><b>${running || d.account_value ? usd(d.account_value) : '—'}</b></div>
    <div class="m"><label>净持仓</label><b>${sz(d.net_position)}</b></div>
    <div class="m"><label>开仓均价</label><b>${px(d.entry_px)}</b></div>
    <div class="m"><label>浮动盈亏</label><b class="${pnlCls(d.unrealized_pnl)}">${pnl(d.unrealized_pnl)}</b></div>
    <div class="m"><label>名义价值</label><b>${usd(d.total_notional)}</b></div>
    <div class="m"><label>强平价</label><b>${px(d.liquidation_px)}</b></div>
    <div class="m"><label>交易所挂单</label><b>${d.open_orders || 0}</b></div>
    <div class="m"><label>累计已实现</label><b class="${pnlCls(d.realized_usd)}">${pnl(d.realized_usd)}</b></div>
    <div class="m"><label>${isTrig ? '完成次数' : (isSig ? '已做轮数' : '完成网格次数')}</label><b>${isSig ? roundsLabel : (d.closed_slots || 0)}</b></div>
    <div class="m"><label>今日下单</label><b>${d.orders_today || 0}</b></div>
    <div class="m"><label>${isTrig ? '触发价' : (isSig ? '跟踪止损' : '锚定价')}</label><b>${px(isTrig ? tg.price : (isSig ? sg.stop_px : d.anchor_price))}</b></div>
    ${isTrig ? `<div class="m"><label>距触发</label><b class="${distCls}">${isFinite(distPct) ? num(distPct, 2) + '%' : '—'}</b></div>` : ''}
    <div class="m"><label>状态</label><b><span class="pill ${cls}">${txt}</span></b></div>`;

  // 风控进度条
  const L = d.limits || {};
  const notionalPct = L.max_total_notional_usd ? Math.min(100, (d.total_notional / L.max_total_notional_usd) * 100) : 0;
  const posPct = L.max_position_size ? Math.min(100, (Math.abs(d.net_position) / L.max_position_size) * 100) : 0;
  const lossPct = L.max_unrealized_loss_usd && d.unrealized_pnl < 0
    ? Math.min(100, (Math.abs(d.unrealized_pnl) / L.max_unrealized_loss_usd) * 100) : 0;
  const barCls = (p) => (p >= 85 ? 'danger' : p >= 60 ? 'warn' : '');

  $('dRisk').innerHTML = `
    <div class="rb"><label><span>名义持仓 / 上限</span>
      <b>${usd(d.total_notional)} / ${usd(L.max_total_notional_usd)}（${notionalPct.toFixed(1)}%）</b></label>
      <div class="bar"><i class="${barCls(notionalPct)}" style="width:${notionalPct}%"></i></div></div>
    <div class="rb"><label><span>净持仓 / 上限</span>
      <b>${sz(Math.abs(d.net_position))} / ${sz(L.max_position_size)}（${posPct.toFixed(1)}%）</b></label>
      <div class="bar"><i class="${barCls(posPct)}" style="width:${posPct}%"></i></div></div>
    <div class="rb"><label><span>浮亏 / 熔断线</span>
      <b>${d.unrealized_pnl < 0 ? pnl(d.unrealized_pnl) : '—'} / -${num(L.max_unrealized_loss_usd, 2)}（${lossPct.toFixed(1)}%）</b></label>
      <div class="bar"><i class="${barCls(lossPct)}" style="width:${lossPct}%"></i></div></div>`;

  if (s.last_error) {
    $('dRisk').innerHTML += `<div class="rb"><label><span class="down">⚠ 最近错误</span></label>
      <div style="font-size:12px;color:var(--danger);word-break:break-all">${esc(s.last_error)}</div></div>`;
  }

  renderTab(false);
  pushChartData();
}

/* ==================== Tabs ==================== */
function renderTab(force) {
  if (state.tab === 'config' && !force) return; // 表单正在编辑，别覆盖用户输入
  const d = state.details[state.selected] || {};
  const box = $('dTabBody');
  if (!box) return;

  if (state.tab === 'grid' && d.strategy === 'trigger') {
    box.innerHTML = triggerPanel(d);
    return;
  }

  if (state.tab === 'grid' && d.strategy === 'signal') {
    box.innerHTML = signalPanel(d);
    return;
  }

  if (state.tab === 'grid') {
    const levels = d.levels || [];
    if (!levels.length) { box.innerHTML = '<div class="empty">还没有网格层位。启动实例后会自动铺出。</div>'; return; }
    box.innerHTML = `<table class="tbl"><thead><tr>
        <th>层位</th><th>方向</th><th>状态</th><th>数量</th><th>建仓价</th><th>止盈价</th><th>目标层价</th>
      </tr></thead><tbody>${levels.map((l) => `
        <tr>
          <td class="mono">${esc(l.key)}</td>
          <td><span class="pill ${l.side}">${l.side === 'buy' ? '买' : '卖'}</span></td>
          <td><span class="pill ${l.state === 'open' ? 'open' : 'pending'}">${l.state === 'open' ? '持仓中' : (l.has_order ? '已挂单' : '待补挂')}</span></td>
          <td class="mono">${sz(l.size)}</td>
          <td class="mono">${px(l.entry_px)}</td>
          <td class="mono">${px(l.tp_px)}</td>
          <td class="mono faint">第 ${l.level} 层</td>
        </tr>`).join('')}</tbody></table>`;
    return;
  }

  if (state.tab === 'trades') {
    const tr = (d.trades || []).slice().reverse();
    if (!tr.length) { box.innerHTML = '<div class="empty">还没有成交记录。</div>'; return; }
    const kindMap = { open: ['建仓', 'pending'], close: ['止盈平仓', 'open'], close_all: ['一键平仓', 'idle'] };
    if (d.strategy === 'signal') kindMap.close = ['平仓', 'open'];
    if (d.strategy === 'trigger') kindMap.open = ['触发成交', 'pending'];
    box.innerHTML = `<table class="tbl"><thead><tr>
        <th>时间</th><th>类型</th><th>方向</th><th>数量</th><th>价格</th><th>盈亏</th>
      </tr></thead><tbody>${tr.map((t) => {
        const [kt, kc] = kindMap[t.kind] || [t.kind, 'idle'];
        return `<tr>
          <td class="mono faint">${tstr(t.ts)}</td>
          <td><span class="pill ${kc}">${kt}</span></td>
          <td><span class="pill ${t.side}">${t.side === 'buy' ? '买' : '卖'}</span></td>
          <td class="mono">${sz(t.size)}</td>
          <td class="mono">${px(t.price)}</td>
          <td class="mono ${t.kind === 'close' ? pnlCls(t.pnl) : 'faint'}">${t.kind === 'close' ? pnl(t.pnl) : '—'}</td>
        </tr>`;
      }).join('')}</tbody></table>`;
    return;
  }

  if (state.tab === 'logs') {
    const vh = box.querySelector('pre.logs');
    if (vh) {
      state.autoScrollLogs = vh.scrollTop + vh.clientHeight >= vh.scrollHeight - 24;
    }
    box.innerHTML = '<pre class="logs" id="logBox">加载中…</pre>';
    loadLogs();
    return;
  }

  if (state.tab === 'config') {
    renderConfigForm();
  }
}

/** 触发单实例的详情面板。 */
function triggerPanel(d) {
  const t = d.trigger || {};
  const slot = (d.levels || [])[0] || null;
  const isBuy = t.side !== 'sell';
  const how = t.order_type === 'market' ? '价格穿越后立即市价成交' : '限价单挂在盘口等成交';
  const phaseMap = {
    '待启动': ['idle', '待启动'],
    '待挂单': ['idle', '待挂单'],
    '等待触发': ['pending', '等待触发'],
    '挂单中': ['pending', '已挂单'],
    '已成交·持仓中': ['open', '已成交·持仓中'],
    '已完成': ['done', '已完成'],
  };
  const [pc, pt] = phaseMap[t.phase] || ['idle', t.phase || '—'];

  const rows = [];
  rows.push(`<div class="m"><label>方向</label><b><span class="pill ${isBuy ? 'buy' : 'sell'}">${isBuy ? '买入' : '卖出'}</span></b></div>`);
  rows.push(`<div class="m"><label>触发价</label><b>${px(t.price)}</b></div>`);
  rows.push(`<div class="m"><label>当前中间价</label><b>${px(d.mid)}</b></div>`);
  rows.push(`<div class="m"><label>距触发</label><b class="${Number(t.distance_pct) < 0 ? 'up' : 'dim'}">${num(t.distance_pct, 2)}%</b></div>`);
  rows.push(`<div class="m"><label>下单数量</label><b>${sz(t.size)} ${esc(d.coin || '')}</b></div>`);
  rows.push(`<div class="m"><label>名义价值</label><b>${usd(t.notional)}</b></div>`);
  rows.push(`<div class="m"><label>触发方式</label><b style="font-size:13px">${esc(how)}</b></div>`);
  rows.push(`<div class="m"><label>成交后</label><b style="font-size:13px">${t.tp_pct > 0 ? `自动止盈 +${num(t.tp_pct, 2)}%` : (t.once ? '只建仓，不再挂单' : '重新挂单等下一轮')}</b></div>`);
  rows.push(`<div class="m"><label>阶段</label><b><span class="pill ${pc}">${esc(pt)}</span></b></div>`);

  let table = '';
  if (slot) {
    table = `<table class="tbl"><thead><tr>
        <th>单号</th><th>方向</th><th>状态</th><th>数量</th><th>成交价</th><th>止盈价</th>
      </tr></thead><tbody><tr>
        <td class="mono">${esc(slot.key)}</td>
        <td><span class="pill ${slot.side}">${slot.side === 'buy' ? '买' : '卖'}</span></td>
        <td><span class="pill ${slot.state === 'open' ? 'open' : 'pending'}">${slot.state === 'open' ? '已成交' : (slot.has_order ? '已挂单' : '待补挂')}</span></td>
        <td class="mono">${sz(slot.size)}</td>
        <td class="mono">${px(slot.entry_px)}</td>
        <td class="mono">${px(slot.tp_px)}</td>
      </tr></tbody></table>`;
  } else {
    table = '<div class="empty">还没生成触发单。启动实例后会把限价单挂到盘口。</div>';
  }

  return `<div class="metrics" style="padding:0 2px 12px">${rows.join('')}</div>${table}`;
}

/* ==================== 信号策略面板 ==================== */
function signalPanel(d) {
  const g = d.signal || {};
  const slot = (d.levels || [])[0] || null;
  const phaseMap = {
    '待启动': ['idle', '待启动'],
    '等待信号': ['idle', '等待信号'],
    '挂单中': ['pending', '挂单中'],
    '持仓中': ['open', '持仓中'],
    '已完成': ['done', '已完成'],
  };
  const [pc, pt] = phaseMap[g.phase] || ['idle', g.phase || '—'];

  const entryHow = g.entry_mode === 'taker'
    ? '市价立刻成交（taker 0.045%）'
    : `挂在回撤价等成交（maker 0.015%，偏离 ${num(g.entry_offset_pct, 3)}%）`;

  // 浮盈 / 名义：持仓时才有意义
  const posTxt = { long: '多头', short: '空头', flat: '空仓' }[g.position] || '空仓';
  const posCls = g.position === 'long' ? 'buy' : (g.position === 'short' ? 'sell' : 'idle');
  const roundsTxt = g.max_rounds > 0 ? `${g.rounds || 0} / ${g.max_rounds}` : `${g.rounds || 0} / 不限`;

  const rows = [];
  rows.push(`<div class="m" style="grid-column:1/-1"><label>当前信号</label>
    <b style="font-size:13px">${esc(g.desc || '预热中')}</b></div>`);
  rows.push(`<div class="m"><label>阶段</label><b><span class="pill ${pc}">${esc(pt)}</span></b></div>`);
  rows.push(`<div class="m"><label>持仓方向</label><b><span class="pill ${posCls}">${posTxt}</span></b></div>`);
  rows.push(`<div class="m"><label>K 线周期</label><b>${esc(g.interval || '—')}（${g.bars || 0} 根）</b></div>`);
  rows.push(`<div class="m"><label>EMA 快线 / 慢线</label><b>${px(g.fast)} / ${px(g.slow)}</b></div>`);
  rows.push(`<div class="m"><label>最新收盘价</label><b>${px(g.close)}</b></div>`);
  rows.push(`<div class="m"><label>ATR(${num(g.atr_mult, 1)}×)</label><b>${px(g.atr)}</b></div>`);
  rows.push(`<div class="m"><label>下单数量</label><b>${sz(g.size)} ${esc(d.coin || '')}</b></div>`);
  rows.push(`<div class="m"><label>入场方式</label><b style="font-size:13px">${esc(entryHow)}</b></div>`);
  rows.push(`<div class="m"><label>跟踪止损</label><b>${g.stop_px ? px(g.stop_px) : '—'}</b></div>`);
  rows.push(`<div class="m"><label>持仓期最高/最低</label><b>${g.extreme_px ? px(g.extreme_px) : '—'}</b></div>`);
  rows.push(`<div class="m"><label>止盈</label><b>${g.take_profit_pct > 0 ? `+${num(g.take_profit_pct, 2)}%` : '只用跟踪止损'}</b></div>`);
  rows.push(`<div class="m"><label>轮数</label><b>${roundsTxt}</b></div>`);
  rows.push(`<div class="m"><label>做空</label><b>${g.allow_short ? '允许（死叉开空）' : '关闭'}</b></div>`);

  let table = '';
  if (slot) {
    const stTxt = slot.state === 'open' ? '持仓中' : (slot.has_order ? '已挂单' : '待成交');
    table = `<table class="tbl"><thead><tr>
        <th>单号</th><th>方向</th><th>状态</th><th>数量</th><th>成交价</th><th>跟踪止损</th>
      </tr></thead><tbody><tr>
        <td class="mono">${esc(slot.key)}</td>
        <td><span class="pill ${slot.side}">${slot.side === 'buy' ? '买' : '卖'}</span></td>
        <td><span class="pill ${slot.state === 'open' ? 'open' : 'pending'}">${stTxt}</span></td>
        <td class="mono">${sz(slot.size)}</td>
        <td class="mono">${px(slot.entry_px)}</td>
        <td class="mono">${px(slot.tp_px)}</td>
      </tr></tbody></table>`;
  } else {
    table = '<div class="empty">还没有持仓或挂单。策略会在 EMA 交叉且 K 线收盘后才动手。</div>';
  }

  const warn = `<div class="keyWarn" style="margin-top:10px">
    <b>先看回测再上真钱。</b>ETH 15m / 最近 31 天的实测里，EMA 12/48 + 跟踪止损
    是净亏的（22 笔，胜率 14%，-0.50 USDC），同期「买了不动」是 +3.65%。
    原因不是引擎写错了，而是手续费（往返 0.03%~0.09%）加上震荡行情里的反复假信号。
    先用干跑观察，或自己改参数后用 <code>python tools/backtest.py --sweep</code> 验证。
  </div>`;

  return `<div class="metrics" style="padding:0 2px 12px">${rows.join('')}</div>${table}${warn}`;
}

async function loadLogs() {
  if (!state.selected) return;
  const r = await fetch(`/api/slots/${encodeURIComponent(state.selected)}/logs?n=250`);
  const data = await r.json().catch(() => ({ logs: [] }));
  const box = $('logBox');
  if (!box) return;
  const lines = data.logs || [];
  box.innerHTML = lines.map((l) => {
    let cls = 'l-info';
    if (l.includes('[WARNING]')) cls = 'l-warn';
    else if (l.includes('[ERROR]') || l.includes('[CRITICAL]')) cls = 'l-err';
    return `<span class="${cls}">${esc(l)}</span>`;
  }).join('\n');
  if (state.autoScrollLogs) box.scrollTop = box.scrollHeight;
}

/* ==================== 权益曲线 ==================== */
function initChart() {
  const el = $('chart');
  if (!el) return;
  if (typeof echarts === 'undefined') {
    state.chartBroken = true;
    el.innerHTML = '<div class="chartFallback">图表库（ECharts CDN）加载失败，曲线不可用。<br>实例功能不受影响。</div>';
    return;
  }
  state.chart = echarts.init(el, null, { renderer: 'canvas' });
  state.chart.setOption({
    grid: { left: 62, right: 18, top: 18, bottom: 34 },
    tooltip: {
      trigger: 'axis',
      formatter: (ps) => {
        const p = ps[0];
        return `${hms(p.value[0])}<br/><b>$${num(p.value[1], 2)}</b>`;
      },
    },
    xAxis: {
      type: 'time',
      axisLine: { lineStyle: { color: '#d8dde3' } },
      axisLabel: { color: '#8b949e', fontSize: 11, formatter: (v) => hms(v).slice(0, 5) },
      splitLine: { show: false },
    },
    yAxis: {
      type: 'value',
      scale: true,
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { color: '#8b949e', fontSize: 11, formatter: (v) => '$' + num(v, 0) },
      splitLine: { lineStyle: { color: '#eef1f4' } },
    },
    series: [{
      type: 'line',
      smooth: true,
      showSymbol: false,
      lineStyle: { width: 2, color: '#2b6cb0' },
      areaStyle: {
        color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
          { offset: 0, color: 'rgba(43,108,176,.22)' },
          { offset: 1, color: 'rgba(43,108,176,.02)' },
        ]),
      },
      data: [],
    }],
  });
  window.addEventListener('resize', () => state.chart && state.chart.resize());
}

async function loadEquity() {
  if (!state.selected) return;
  try {
    const r = await fetch(`/api/slots/${encodeURIComponent(state.selected)}/equity?limit=4000`);
    const data = await r.json();
    state.fullEquity = data.equity || [];
  } catch (e) {
    state.fullEquity = [];
  }
  pushChartData();
}

function pushChartData() {
  if (state.chartBroken || !state.chart) return;
  const d = state.details[state.selected] || {};
  const streamed = d.equity || [];
  let pts = state.fullEquity;
  // 实时推送里的最新点比全量接口新，合并一下（按时间戳去重）
  if (pts && pts.length) {
    const lastTs = pts[pts.length - 1][0];
    const extra = streamed.filter((p) => p[0] > lastTs);
    pts = pts.concat(extra);
  } else {
    pts = streamed;
  }
  state.chart.setOption({ series: [{ data: pts }] });

  const el = $('dChartInfo');
  if (el) {
    if (pts && pts.length) {
      const first = pts[0][1], last = pts[pts.length - 1][1];
      const diff = last - first;
      el.innerHTML = `${pts.length} 个采样点 · 区间变化 <b class="${pnlCls(diff)}">${diff >= 0 ? '+' : ''}${num(diff, 2)}</b>`;
    } else {
      el.textContent = '暂无数据（实例运行后每 30 秒采样一次）';
    }
  }
}

/* ==================== 参数表单 ==================== */
// only: 'grid' | 'trigger' | 'signal' —— 该分区只在对应策略下显示
const FORM_SCHEMA = [
  {
    section: '__root__', title: '策略类型', fields: [
      {
        k: 'strategy', l: '策略', t: 'select',
        o: [
          ['grid', '网格 + 马丁（区间内反复吃差价）'],
          ['trigger', '触发单（到价买入/卖出一笔）'],
          ['signal', '信号策略（指标交叉，实时买入卖出）'],
        ],
        hint: '切换后下面会显示对应的参数分区；保存后需重新启动实例才生效',
      },
    ],
  },
  {
    section: 'env', title: '运行环境', fields: [
      { k: 'network', l: '网络', t: 'select', o: [['mainnet', 'mainnet（主网）'], ['testnet', 'testnet（测试网）']] },
      { k: 'dry_run', l: '干跑模式（不下真实订单）', t: 'bool', hint: '强烈建议先保持开启，确认逻辑正确后再关闭' },
      { k: 'account_address', l: '主钱包地址', t: 'text', hint: '使用 API 钱包交易时必填；用主钱包私钥可留空' },
    ],
  },
  {
    section: 'market', title: '标的与仓位', fields: [
      { k: 'coin', l: '交易对', t: 'text', hint: '如 ETH / BTC / SOL' },
      { k: 'leverage', l: '杠杆（倍）', t: 'num', hint: '网格不要开高杠杆，建议 3~5' },
      { k: 'margin_mode', l: '保证金模式', t: 'select', o: [['isolated', '逐仓 isolated'], ['cross', '全仓 cross']] },
      { k: 'base_size', l: '每层基础下单量（币）', t: 'num' },
      { k: 'reduce_only_tp', l: '止盈单用 reduce-only', t: 'bool' },
      { k: 'close_slippage_pct', l: '平仓滑点容忍（%）', t: 'num' },
    ],
  },
  {
    section: 'grid', title: '网格参数', only: 'grid', fields: [
      { k: 'mode', l: '方向', t: 'select', o: [['long', '做多网格'], ['short', '做空网格'], ['neutral', '双向网格']] },
      { k: 'anchor_price', l: '锚定价（0=自动）', t: 'num' },
      { k: 'step_pct', l: '格间距（%）', t: 'num' },
      { k: 'levels', l: '层数', t: 'num' },
      { k: 'take_profit_pct', l: '每层止盈（%）', t: 'num' },
      { k: 'reanchor', l: '价格脱离区间后自动重锚', t: 'bool' },
      { k: 'recenter_threshold_pct', l: '重锚触发偏离（%）', t: 'num' },
    ],
  },
  {
    section: 'martingale', title: '马丁加仓', only: 'grid', fields: [
      { k: 'enabled', l: '启用马丁', t: 'bool' },
      { k: 'multiplier', l: '加仓倍数', t: 'num', hint: '1.0 = 等量网格；越大摊薄越快、爆仓风险越高' },
      { k: 'max_size_multiple', l: '单笔最大放大倍数（硬上限）', t: 'num' },
    ],
  },
  {
    section: 'trigger', title: '触发单参数', only: 'trigger', fields: [
      { k: 'side', l: '方向', t: 'select', o: [['buy', '买入 buy'], ['sell', '卖出 sell']] },
      { k: 'price', l: '触发价', t: 'num', hint: 'limit 模式=挂单价；market 模式=穿越判定价' },
      {
        k: 'size', l: '下单数量（币）', t: 'num',
        hint: '⚠ 交易所单笔名义价值不得低于 $10，低于会被拒单（ETH 最小约 0.004）',
      },
      {
        k: 'order_type', l: '触发方式', t: 'select',
        o: [['limit', '限价单挂在盘口等成交'], ['market', '价格穿越后立即市价成交']],
      },
      { k: 'max_slippage_pct', l: '市价滑点容忍（%）', t: 'num', hint: '仅 market 模式使用' },
      { k: 'once', l: '成交后即视为完成（不再补挂）', t: 'bool' },
      { k: 'tp_pct', l: '成交后自动止盈（%），0=不挂', t: 'num' },
    ],
  },
  {
    section: 'signal', title: '信号策略参数', only: 'signal', fields: [
      {
        k: 'interval', l: 'K 线周期', t: 'select',
        o: [['1m', '1m（约 3.5 天历史）'], ['5m', '5m（约 17.5 天）'], ['15m', '15m（约 30 天）'], ['30m', '30m（约 60 天）'], ['1h', '1h（约 60 天）']],
        hint: '周期越小信号越密，但手续费也吃掉越多',
      },
      { k: 'candles', l: '取多少根 K 线算指标', t: 'num', hint: '至少要 > slow_ema，建议 300' },
      { k: 'candle_cache_sec', l: 'K 线缓存（秒）', t: 'num', hint: '别设太小，省接口额度' },
      { k: 'fast_ema', l: '快线 EMA 周期', t: 'num' },
      { k: 'slow_ema', l: '慢线 EMA 周期（必须大于快线）', t: 'num' },
      { k: 'trend_ema', l: '趋势过滤均线（0=关闭）', t: 'num', hint: '>0 时只在收盘价位于该均线同侧才开仓' },
      {
        k: 'entry_mode', l: '入场方式', t: 'select',
        o: [['maker', 'maker：挂回撤价等成交（0.015%）'], ['taker', 'taker：立即市价（0.045%）']],
      },
      { k: 'entry_offset_pct', l: 'maker 挂单偏离（%）', t: 'num', hint: '买单挂在现价下方、卖单挂上方这么多' },
      { k: 'entry_ttl_sec', l: '挂单超时撤单（秒）', t: 'num', hint: '价格没回撤就是不该追，撤掉等下次信号' },
      { k: 'allow_short', l: '死叉时反手做空', t: 'bool', hint: '不确定就先关掉' },
      { k: 'atr_period', l: 'ATR 周期', t: 'num' },
      { k: 'atr_mult', l: '跟踪止损：ATR 倍数', t: 'num', hint: '止损距离 = max(百分比, ATR×倍数)' },
      { k: 'trail_pct', l: '跟踪止损：百分比', t: 'num', hint: '0.6% ≈ 从最高点回撤 $15 就走' },
      { k: 'take_profit_pct', l: '固定止盈（%），0=不设', t: 'num', hint: '趋势策略通常只用跟踪止损' },
      { k: 'exit_on_cross', l: '反向交叉立即平仓', t: 'bool' },
      { k: 'size', l: '下单数量（币）', t: 'num', hint: '⚠ 交易所单笔名义价值不得低于 $10，低于会被拒单（ETH 最小约 0.004）' },
      { k: 'max_rounds', l: '最多做几轮（0=不限）', t: 'num', hint: '一轮 = 开 + 平。先设小值试探' },
    ],
  },
  {
    section: 'risk', title: '风控（不要随手调大）', fields: [
      { k: 'max_total_notional_usd', l: '名义持仓上限（USD）', t: 'num' },
      { k: 'max_position_size', l: '净持仓上限（币）', t: 'num' },
      { k: 'max_unrealized_loss_usd', l: '浮亏熔断线（USD）', t: 'num', hint: '触发后：撤单 → 市价平仓 → 停机冷却' },
      { k: 'min_account_value_usd', l: '账户权益下限（USD）', t: 'num' },
      { k: 'max_open_orders', l: '同时挂单上限', t: 'num' },
      { k: 'max_daily_orders', l: '每日下单上限', t: 'num' },
      { k: 'cooldown_after_stop_sec', l: '熔断冷却（秒）', t: 'num' },
    ],
  },
  {
    section: 'runtime', title: '运行参数', fields: [
      { k: 'loop_interval_sec', l: '主循环间隔（秒）', t: 'num' },
      { k: 'equity_interval_sec', l: '权益采样间隔（秒）', t: 'num' },
      { k: 'log_level', l: '日志级别', t: 'select', o: [['DEBUG', 'DEBUG'], ['INFO', 'INFO'], ['WARNING', 'WARNING']] },
    ],
  },
  {
    section: 'notify', title: 'Telegram 通知', fields: [
      { k: 'enabled', l: '启用通知', t: 'bool' },
      { k: 'telegram_bot_token', l: 'Bot Token', t: 'password', hint: '留空表示不修改已保存的值' },
      { k: 'telegram_chat_id', l: 'Chat ID', t: 'text' },
      { k: 'on_stop', l: '启动/停止/熔断时通知', t: 'bool' },
      { k: 'on_fill', l: '每次网格止盈都通知', t: 'bool', hint: '比较频繁，按需开启' },
    ],
  },
];

function renderConfigForm() {
  const s = state.slots.find((x) => x.name === state.selected);
  const box = $('dTabBody');
  if (!s || !box) return;
  if (s.running) {
    box.innerHTML = `<div class="empty">实例正在运行，无法修改配置。<br>请先点「停止」，改完保存后重新启动。</div>`;
    return;
  }

  const curStrategy = ['trigger', 'signal'].includes(s.strategy) ? s.strategy : 'grid';
  const sections = FORM_SCHEMA
    .filter((sec) => !sec.only || sec.only === curStrategy)
    .map((sec) => {
    const vals = sec.section === '__root__' ? { strategy: curStrategy } : (s[sec.section] || {});
    const fields = sec.fields.map((f) => {
      const v = vals[f.k];
      if (f.t === 'bool') {
        return `<div class="field check">
          <input type="checkbox" id="f_${sec.section}_${f.k}" data-sec="${sec.section}" data-k="${f.k}" data-t="bool" ${v ? 'checked' : ''}>
          <label for="f_${sec.section}_${f.k}">${esc(f.l)}</label>
        </div>`;
      }
      if (f.t === 'select') {
        return `<div class="field"><label>${esc(f.l)}</label>
          <select id="f_${sec.section}_${f.k}" data-sec="${sec.section}" data-k="${f.k}" data-t="text">
            ${f.o.map(([ov, ol]) => `<option value="${esc(ov)}" ${String(v) === ov ? 'selected' : ''}>${esc(ol)}</option>`).join('')}
          </select>${f.hint ? `<span class="hint">${esc(f.hint)}</span>` : ''}</div>`;
      }
      const type = f.t === 'num' ? 'number' : (f.t === 'password' ? 'password' : 'text');
      const val = f.t === 'password' ? '' : (v === undefined || v === null ? '' : esc(v));
      const ph = f.t === 'password' ? (vals.has_token ? '已保存，留空则不修改' : '尚未设置') : '';
      return `<div class="field"><label>${esc(f.l)}</label>
        <input type="${type}" ${f.t === 'num' ? 'step="any"' : ''} id="f_${sec.section}_${f.k}"
               data-sec="${sec.section}" data-k="${f.k}" data-t="${f.t}" value="${val}" placeholder="${esc(ph)}">
        ${f.hint ? `<span class="hint">${esc(f.hint)}</span>` : ''}</div>`;
    }).join('');
    return `<div class="formSection"><h3>${esc(sec.title)}</h3><div class="formGrid">${fields}</div></div>`;
  }).join('');

  box.innerHTML = `${s.config_error ? `<div class="keyWarn">配置有问题：${esc(s.config_error)}</div>` : ''}
    ${sections}
    <div style="display:flex;gap:10px;align-items:center;margin-top:4px">
      <button class="btn primary" id="cfgSave">保存配置</button>
      <button class="btn" id="cfgReload">放弃修改</button>
      <button class="btn" id="cfgNotifyTest">发送通知测试</button>
      <span class="dim" style="font-size:12px">保存后需重新启动实例才会生效</span>
    </div>
    <div class="dangerZone">
      <h3>危险操作</h3>
      <p>「清除本地状态」会把网格层位记录、累计盈亏、熔断冷却全部重置。
         用于熔断后想立刻重新开始。已挂的交易所订单不会被撤销，请先用「撤单」。</p>
      <button class="btn danger sm" id="cfgResetState">清除本地状态</button>
      <button class="btn danger sm" id="cfgDelete">删除此实例</button>
    </div>`;

  $('cfgSave').onclick = saveConfig;
  $('cfgReload').onclick = () => renderConfigForm();
  // 切换策略时立刻换成对应的参数分区，不用保存再刷新
  const stratSel = $('f___root___strategy');
  if (stratSel) {
    stratSel.onchange = () => {
      s.strategy = stratSel.value; // 只用于本次渲染，真正保存以表单值为准
      renderConfigForm();
    };
  }
  $('cfgNotifyTest').onclick = async () => {
    const r = await api('POST', `/api/slots/${encodeURIComponent(state.selected)}/notify-test`);
    toast(r.message, r.ok ? 'ok' : 'err');
  };
  $('cfgResetState').onclick = async () => {
    const ok = await confirmDialog({
      title: '清除本地状态？',
      body: '网格层位记录、累计盈亏、熔断冷却时间都会被重置。<br>如果还有挂单，请先执行「撤单」。',
      okText: '确认清除', danger: true,
    });
    if (!ok) return;
    const r = await api('POST', `/api/slots/${encodeURIComponent(state.selected)}/reset-state`);
    toast(r.message, r.ok ? 'ok' : 'err');
  };
  $('cfgDelete').onclick = async () => {
    const ok = await confirmDialog({
      title: `删除实例 ${state.selected}？`,
      body: '配置文件会被改名备份（不是真删除），之后可以手工恢复。<br>不会平仓、不会撤单。',
      okText: '删除', danger: true,
    });
    if (!ok) return;
    const r = await api('DELETE', `/api/slots/${encodeURIComponent(state.selected)}`);
    toast(r.message, r.ok ? 'ok' : 'err');
    if (r.ok) { state.selected = null; $('detail').innerHTML = '<div class="empty big">从左侧选择一个实例</div>'; }
  };
}

async function saveConfig() {
  const payload = {};
  const nums = new Set();
  document.querySelectorAll('#dTabBody [data-sec]').forEach((el) => {
    const sec = el.dataset.sec, k = el.dataset.k, t = el.dataset.t;
    if (sec === '__root__') { payload[k] = el.value.trim(); return; } // 顶层字段，如 strategy
    payload[sec] = payload[sec] || {};
    if (t === 'bool') payload[sec][k] = el.checked;
    else if (t === 'num') {
      const v = el.value.trim();
      if (v === '') return;
      const n = Number(v);
      if (!isFinite(n)) { nums.add(`${sec}.${k}`); return; }
      payload[sec][k] = n;
    } else payload[sec][k] = el.value.trim();
  });
  if (nums.size) { toast('这些字段不是合法数字：' + [...nums].join(', '), 'err'); return; }

  const risky = [];
  if (payload.env && payload.env.dry_run === false) risky.push('• 关闭了干跑，将用真实资金下单');
  if (payload.risk && payload.risk.max_unrealized_loss_usd) risky.push(`• 浮亏熔断线设为 $${payload.risk.max_unrealized_loss_usd}`);
  if (payload.martingale && payload.martingale.multiplier > 1.5) risky.push(`• 马丁倍数 ${payload.martingale.multiplier}（>1.5，风险偏高）`);

  if (risky.length) {
    const ok = await confirmDialog({
      title: '请确认参数变更',
      body: `即将保存以下高风险设置：<br><span class="warnText">${risky.join('<br>')}</span><br><br>确认保存？`,
      okText: '确认保存', danger: true,
    });
    if (!ok) return;
  }

  const r = await api('PUT', `/api/slots/${encodeURIComponent(state.selected)}/config`, payload);
  toast(r.message, r.ok ? 'ok' : 'err');
  if (r.ok) renderConfigForm();
}

/* ==================== 控制动作 ==================== */
async function actStart() {
  const s = state.slots.find((x) => x.name === state.selected);
  if (!s) return;
  if ((s.env || {}).dry_run === false) {
    const tg = s.trigger || {};
    const sg = s.signal || {};
    const detail = s.strategy === 'trigger'
      ? `触发方式：<b>${tg.order_type === 'market' ? '价格穿越后市价成交' : '限价单挂盘口等成交'}</b>　
         触发价：<b>${px(tg.price)}</b>　数量：<b>${sz(tg.size)}</b>　名义：<b>${usd(tg.notional || (tg.size * tg.price))}</b>`
      : s.strategy === 'signal'
        ? `指标：<b>EMA ${sg.fast_ema}/${sg.slow_ema} @ ${esc(sg.interval || '')}</b>　
           入场：<b>${sg.entry_mode === 'taker' ? '市价' : '挂回撤价'}</b>　
           跟踪止损：<b>${num(sg.trail_pct, 2)}% / ATR×${num(sg.atr_mult, 1)}</b><br>
           每轮数量：<b>${sz(sg.size)}</b>　最多 <b>${sg.max_rounds > 0 ? sg.max_rounds : '不限'}</b> 轮　
           ${sg.allow_short ? '<span class="warnText">允许做空</span>' : '只做多'}`
        : `层数：<b>${s.grid?.levels}</b>　格间距：<b>${num(s.grid?.step_pct, 2)}%</b>`;
    const stratName = s.strategy === 'trigger' ? '触发单' : (s.strategy === 'signal' ? '信号策略（会主动平仓）' : '网格 + 马丁');
    const ok = await confirmDialog({
      title: '实盘启动确认',
      body: `<b>${esc(s.name)}</b> 配置为<b>实盘模式</b>，启动后会立即用真实资金下单。<br><br>
             策略：<b>${stratName}</b>　
             标的：<b>${esc(s.market?.coin)}</b>　杠杆：<b>${num(s.market?.leverage, 0)}x</b><br>
             ${detail}<br>
             名义上限：<b>$${num(s.risk?.max_total_notional_usd, 0)}</b><br><br>
             确认启动？`,
      okText: '确认启动实盘', danger: true,
    });
    if (!ok) return;
  }
  toast('正在启动，请稍候…');
  const r = await api('POST', `/api/slots/${encodeURIComponent(state.selected)}/start`);
  toast(r.message, r.ok ? 'ok' : 'err');
}

async function actStop() {
  const s = state.slots.find((x) => x.name === state.selected);
  const hasPos = (state.details[state.selected] || {}).net_position !== 0;
  const ok = await confirmDialog({
    title: '停止实例',
    body: hasPos
      ? '当前<b>还有持仓</b>。选择「仅停止」会保留挂单和持仓，重启后继续接管；<br>选择「停止并平仓」会撤掉全部挂单并市价平掉持仓。'
      : '该实例当前无持仓。停止后挂单和状态会保留，重启可继续。',
    okText: hasPos ? '仅停止（保留仓位）' : '确认停止',
  });
  if (!ok) return;
  const r = await api('POST', `/api/slots/${encodeURIComponent(state.selected)}/stop`, { close: false });
  toast(r.message, r.ok ? 'ok' : 'err');
}

async function actCancel() {
  const ok = await confirmDialog({
    title: '撤销全部挂单？',
    body: '会撤销该实例在交易所的全部挂单。<b>持仓不受影响。</b>',
    okText: '确认撤单',
  });
  if (!ok) return;
  const r = await api('POST', `/api/slots/${encodeURIComponent(state.selected)}/cancel-all`);
  toast(r.message, r.ok ? 'ok' : 'err');
}

async function actClose() {
  const d = state.details[state.selected] || {};
  const ok = await confirmDialog({
    title: '⚠️ 一键平仓',
    body: `将<b>撤销全部挂单</b>并<b>市价平掉 ${esc(d.coin || '')} 的全部持仓</b>。<br><br>
           当前净持仓：<b>${sz(d.net_position)}</b>　浮动盈亏：<b>${pnl(d.unrealized_pnl)}</b><br><br>
           <span class="warnText">平仓会把浮动亏损变成实际亏损，且无法撤销。</span>`,
    okText: '确认平仓', danger: true,
  });
  if (!ok) return;
  const r = await api('POST', `/api/slots/${encodeURIComponent(state.selected)}/close-all`);
  toast(r.message, r.ok ? 'ok' : 'err');
}

/* ==================== 全局按钮 ==================== */
$('btnNew').onclick = async () => {
  const name = await promptDialog({
    title: '新建交易实例',
    body: '会基于默认模板在 <code>configs/</code> 下创建一个 yaml 配置文件。<br>创建后可在「参数设置」里调整。',
    placeholder: '例如 btc-grid',
    okText: '创建',
  });
  if (!name) return;
  const r = await api('POST', '/api/slots', { name });
  toast(r.message, r.ok ? 'ok' : 'err');
  if (r.ok) setTimeout(() => selectSlot(name), 400);
};

$('btnRefresh').onclick = async () => {
  const r = await fetch('/api/slots');
  const d = await r.json();
  state.slots = d.slots || [];
  renderSidebar();
  toast('已刷新', 'ok');
};

$('btnEmergency').onclick = async () => {
  const running = state.slots.filter((s) => s.running);
  if (!running.length) { toast('当前没有正在运行的实例', 'warn'); return; }
  const list = running.map((s) => {
    const l = s.live || {};
    return `• <b>${esc(s.name)}</b>（${esc(s.market?.coin)}）净仓 ${sz(l.net_position)}，浮盈 ${pnl(l.unrealized_pnl)}`;
  }).join('<br>');
  const ok = await confirmDialog({
    title: '⛔ 全局急停',
    body: `将对以下 <b>${running.length}</b> 个运行中的实例执行：<br>
           <b>停止 → 撤销全部挂单 → 市价平仓</b><br><br>${list}<br><br>
           <span class="warnText">这是最后的刹车，会把所有浮动亏损变成实际亏损。</span>`,
    okText: '确认全局急停', danger: true,
  });
  if (!ok) return;
  toast('正在执行急停…');
  const r = await api('POST', '/api/emergency-stop');
  toast(r.message, r.ok ? 'ok' : 'err');
};

/* ==================== 钱包 ====================
 * 用浏览器插件钱包（EIP-1193）连接，读取你在 Hyperliquid 上的真实账户。
 *
 * 边界（很重要）：
 *   - 这个页面只向钱包请求「账户地址」，不请求任何签名。
 *   - 它永远不接触私钥 / 助记词；下单依然由后端的 .env 私钥完成。
 *   - 因此连接钱包是纯只读行为，点错也不会造成任何资产风险。
 * ============================================== */

const HL_CHAINS = {
  '0x1': { name: 'Ethereum', hint: '不是 Hyperliquid 常用网络' },
  '0xa4b1': { name: 'Arbitrum One', hint: 'Hyperliquid 入金网络', hl: true },
  '0x3e7': { name: 'HyperEVM', hint: 'Hyperliquid L1 EVM', hl: true },
  '0xa': { name: 'OP Mainnet', hint: '不是 Hyperliquid 常用网络' },
  '0x2105': { name: 'Base', hint: '不是 Hyperliquid 常用网络' },
  '0x38': { name: 'BNB Chain', hint: '不是 Hyperliquid 常用网络' },
  '0x89': { name: 'Polygon', hint: '不是 Hyperliquid 常用网络' },
};
const HL_ADD_CHAIN = {
  '0xa4b1': {
    chainId: '0xa4b1', chainName: 'Arbitrum One',
    nativeCurrency: { name: 'Ether', symbol: 'ETH', decimals: 18 },
    rpcUrls: ['https://arb1.arbitrum.io/rpc'],
    blockExplorerUrls: ['https://arbiscan.io'],
  },
};

const wallet = {
  address: '',
  chainId: '',
  network: 'mainnet',
  data: null,
  error: '',
  loading: false,
  open: false,
  manual: false,   // 地址是手输的，不是钱包连上的
  timer: null,
  range: 'day',
  bindSlot: '',
  pollMs: 12000,
  busy: false,        // 正在跟钱包交互（连接/切网），与数据加载分开
  providerName: '',   // 连上的钱包名字（MetaMask / Rabby …）
  diagOpen: false,    // 抽屉里是否展开了诊断信息
};

/* ==================== 插件钱包发现（EIP-6963 + EIP-1193） ==================== */

/** EIP-6963 广播出来的钱包（MetaMask / OKX / Rabby 等新版都会广播）。 */
const w6963 = [];
/** 缓存扫到的 provider 列表；MetaMask 排在最前。 */
let _wList = [];
let _wScanAt = 0;
/** 用户在抽屉里手动选定的钱包（多钱包共存时用）。 */
let _wChosen = null;

function wHasReq(p) {
  try { return !!(p && typeof p.request === 'function'); } catch (e) { return false; }
}
function wIsMetaMask(p) {
  try { return !!(wHasReq(p) && p.isMetaMask && !p.isBraveWallet); } catch (e) { return false; }
}
function wProviderName(p) {
  const flags = [
    ['isMetaMask', 'MetaMask'], ['isRabby', 'Rabby Wallet'], ['isOkxWallet', 'OKX Wallet'],
    ['isOKExWallet', 'OKX Wallet'], ['isCoinbaseWallet', 'Coinbase Wallet'], ['isBraveWallet', 'Brave Wallet'],
    ['isTrust', 'Trust Wallet'], ['isTokenPocket', 'TokenPocket'], ['isBitKeep', 'Bitget Wallet'],
    ['isImToken', 'imToken'], ['isPhantom', 'Phantom'],
  ];
  for (let i = 0; i < flags.length; i++) {
    try { if (p && p[flags[i][0]]) return flags[i][1]; } catch (e) { /* ignore */ }
  }
  return '浏览器钱包';
}

/** 收集页面上所有可用 provider（去重，MetaMask 优先）。 */
function wCollect() {
  const out = [];
  const seen = [];
  const add = (p, name, rdns) => {
    if (!wHasReq(p)) return;
    for (let i = 0; i < seen.length; i++) if (seen[i] === p) return;
    seen.push(p);
    out.push({ provider: p, name: name || wProviderName(p), rdns: rdns || '' });
  };

  for (let i = 0; i < w6963.length; i++) {
    const d = w6963[i];
    add(d.provider, d.info && d.info.name, d.info && d.info.rdns);
  }

  let eth = null;
  try { eth = window.ethereum; } catch (e) { eth = null; }
  if (eth) {
    // 多个钱包插件共存时，window.ethereum 常常是被抢注的那个，
    // 真正的各家 provider 挂在 window.ethereum.providers 数组里。
    let arr = null;
    try { arr = eth.providers; } catch (e) { arr = null; }
    if (arr && typeof arr.length === 'number' && arr.length) {
      for (let i = 0; i < arr.length; i++) add(arr[i]);
    }
    add(eth);
  }

  out.sort((a, b) => (wIsMetaMask(b.provider) ? 1 : 0) - (wIsMetaMask(a.provider) ? 1 : 0));
  return out;
}

function wRescan() {
  _wList = wCollect();
  _wScanAt = Date.now();
  if (_wChosen) {
    let still = false;
    for (let i = 0; i < _wList.length; i++) if (_wList[i].provider === _wChosen.provider) still = true;
    if (!still) _wChosen = null;
  }
  return _wList;
}
function wList() {
  if (!_wList.length || Date.now() - _wScanAt > 8000) wRescan();
  return _wList;
}
function wProvider() {
  if (_wChosen) return _wChosen.provider;
  const l = wList();
  return l.length ? l[0].provider : null;
}
function wCan() { return !!wProvider(); }

/** 页面是否被嵌在别的页面里（IDE 内置预览面板、网页 iframe 等）。 */
function wInIframe() {
  if (typeof window === 'undefined') return false;
  try { return window.self !== window.top; } catch (e) { return true; }  // 跨域读取会抛错，说明确实被嵌了
}
function wUA() {
  try { return String(navigator.userAgent || ''); } catch (e) { return ''; }
}
/** 宿主环境描述；普通顶层浏览器返回空串。 */
function wEmbedKind() {
  const ua = wUA();
  if (wInIframe()) {
    return /Electron|CefSharp|QtWebEngine/i.test(ua) ? '桌面客户端内嵌的预览窗口' : '网页里的 iframe';
  }
  if (/Electron|CefSharp|QtWebEngine/i.test(ua)) return '桌面客户端内置浏览器';
  return '';
}
/** 钱包插件注入不进来的环境（内置预览窗口、Electron 壳浏览器）。 */
function wBlocked() { return !wList().length && !!wEmbedKind(); }
function wSelfUrl() {
  try { return String(location.origin || '') || 'http://127.0.0.1:8787'; } catch (e) { return 'http://127.0.0.1:8787'; }
}

/** 装 EIP-6963 发现监听 + 延迟重扫（部分钱包注入较慢，页面刚打开时扫不到）。 */
function wInstallDiscovery() {
  if (typeof window === 'undefined' || !window.addEventListener) return;

  window.addEventListener('eip6963:announceProvider', (ev) => {
    const d = ev && ev.detail;
    if (!d || !d.provider) return;
    w6963.push(d);
    const before = _wList.length;
    wRescan();
    if (_wList.length !== before) {
      renderWalletChip();
      if (wallet.open) renderWalletDrawer();
    }
  });
  try { window.dispatchEvent(new Event('eip6963:requestProvider')); } catch (e) { /* ignore */ }

  // MetaMask 注入完成时会派发这个事件
  try {
    window.addEventListener('ethereum#initialized', () => {
      wRescan(); renderWalletChip(); if (wallet.open) renderWalletDrawer();
    }, { once: true });
  } catch (e) { /* ignore */ }

  // 兜底：延迟多扫几次，覆盖注入慢的浏览器
  [150, 600, 1500, 3000].forEach((ms) => {
    setTimeout(() => {
      const before = _wList.length;
      wRescan();
      if (_wList.length !== before) { renderWalletChip(); if (wallet.open) renderWalletDrawer(); }
    }, ms);
  });
}

/** 钱包抛出的错误 → 人话。 */
function wErr(e, action) {
  const code = (e && e.code !== undefined) ? e.code : (e && e.data ? e.data.code : undefined);
  const msg = String((e && (e.message || e.reason)) || e || '');
  if (code === 4001 || /user rejected|user denied|rejected by user/i.test(msg)) {
    return { reject: true, text: '你在钱包里取消了这次' + action };
  }
  if (code === -32002 || /already processing|already pending/i.test(msg)) {
    return { text: '钱包里已经有一个待处理的请求 —— 点开浏览器右上角的钱包图标（会带红点），把它处理掉再回来重试' };
  }
  if (code === 4900 || code === 4901) return { text: '钱包未连接到本页面：请打开钱包，选择连接到这个站点' };
  if (code === 4902) return { text: '你的钱包里还没有 Arbitrum One 网络，需要先添加' };
  if (code === 4100) return { text: '钱包拒绝了这次授权（账户未授权给本页面）' };
  if (/locked/i.test(msg)) return { text: '钱包处于锁定状态，请先解锁再重试' };
  return { text: '钱包调用失败：' + (msg || '未知错误') };
}

function lsGet(k) { try { return (window.localStorage && window.localStorage.getItem(k)) || null; } catch (e) { return null; } }
function lsSet(k, v) { try { if (window.localStorage) window.localStorage.setItem(k, v); } catch (e) { /* 隐私模式 */ } }
function lsDel(k) { try { if (window.localStorage) window.localStorage.removeItem(k); } catch (e) { /* ignore */ } }

function isAddr(a) { return /^0[xX][0-9a-fA-F]{40}$/.test(String(a || '').trim()); }
function shortAddr(a) { return a ? String(a).slice(0, 6) + '…' + String(a).slice(-4) : ''; }

function chainHex(v) {
  if (v === undefined || v === null || v === '') return '';
  try {
    if (typeof v === 'string' && v.toLowerCase().startsWith('0x')) return '0x' + parseInt(v, 16).toString(16);
    return '0x' + Number(v).toString(16);
  } catch (e) { return ''; }
}
function chainInfo(id) {
  const k = chainHex(id);
  return HL_CHAINS[k] || { name: k ? '未知网络' : '未知网络', hint: '' };
}

/* ---------------- 抽屉开合 ---------------- */
function walletOpen() {
  wallet.open = true;
  const dr = $('walletDrawer');
  if (dr) { dr.classList.add('show'); dr.setAttribute('aria-hidden', 'false'); }
  const sc = $('wScrim');
  if (sc) sc.classList.remove('hidden');
  renderWalletDrawer();
  if (wallet.address) loadWallet(true);
  if (!wallet.timer && wallet.address) wallet.timer = setInterval(() => loadWallet(false), wallet.pollMs);
}

function walletClose() {
  wallet.open = false;
  const dr = $('walletDrawer');
  if (dr) { dr.classList.remove('show'); dr.setAttribute('aria-hidden', 'true'); }
  const sc = $('wScrim');
  if (sc) sc.classList.add('hidden');
  if (wallet.timer) { clearInterval(wallet.timer); wallet.timer = null; }
}

/* ---------------- 连接 / 断开 ---------------- */
async function walletConnect() {
  if (wallet.busy) return;
  wRescan();

  // 刚打开页面时钱包可能还没注入完，给它一点时间
  if (!_wList.length) {
    for (let i = 0; i < 8 && !_wList.length; i++) {
      await new Promise((r) => setTimeout(r, 250));
      wRescan();
    }
  }
  if (!_wList.length) {
    walletOpen();   // 抽屉里有明确的原因、解决办法和「直接输入地址」
    toast(wBlocked()
      ? '当前窗口里插件钱包用不了，请改用系统浏览器打开'
      : '没有检测到浏览器钱包插件', 'warn');
    return;
  }

  const pick = _wChosen || _wList[0];
  const p = pick.provider;
  wallet.busy = true;
  renderWalletChip();

  try {
    let accts = null;
    try {
      accts = await p.request({ method: 'eth_requestAccounts' });
    } catch (e1) {
      const t = wErr(e1, '连接');
      if (t.reject) throw e1;
      // 少数情况下（已授权但账户被冻结/钱包状态异常）eth_requestAccounts 会失败，
      // 用显式权限请求兜一次
      try {
        await p.request({ method: 'wallet_requestPermissions', params: [{ eth_accounts: {} }] });
        accts = await p.request({ method: 'eth_accounts' });
      } catch (e2) { throw e1; }
    }
    if (!accts || !accts.length) {
      throw new Error('钱包没有返回任何账户（可能处于未解锁状态，或钱包里还没有账户）');
    }

    wallet.address = String(accts[0]);
    wallet.manual = false;
    wallet.providerName = pick.name;
    try { wallet.chainId = (await p.request({ method: 'eth_chainId' })) || p.chainId || ''; } catch (e) { /**/ }
    lsSet('hlbot.wallet', wallet.address);
    toast('已连接 ' + pick.name + ' · ' + shortAddr(wallet.address), 'ok');
    await loadWallet(true);
    if (!wallet.timer) wallet.timer = setInterval(() => loadWallet(false), wallet.pollMs);
  } catch (e) {
    const t = wErr(e, '连接');
    toast(t.text, t.reject ? 'warn' : 'err');
    if (!t.reject) walletOpen();   // 把抽屉打开，用户可以看诊断或退回「直接输入地址」
  } finally {
    wallet.busy = false;
    renderWalletChip();
    if (wallet.open) renderWalletDrawer();
  }
}

function walletDisconnect() {
  wallet.address = '';
  wallet.data = null;
  wallet.error = '';
  wallet.manual = false;
  lsDel('hlbot.wallet');
  if (wallet.timer) { clearInterval(wallet.timer); wallet.timer = null; }
  renderWalletChip();
  renderWalletDrawer();
  toast('已断开（钱包插件里的连接授权仍然保留，可在插件里撤销）');
}

/** 不装钱包也能查：直接把地址贴进来。 */
function walletManual(text) {
  const a = String(text || '').trim();
  if (!isAddr(a)) { toast('地址格式不对，应为 0x 开头的 40 位十六进制', 'err'); return false; }
  wallet.address = a.toLowerCase();
  wallet.manual = true;
  wallet.data = null;
  lsSet('hlbot.wallet', wallet.address);
  renderWalletChip();
  loadWallet(true);
  if (!wallet.timer) wallet.timer = setInterval(() => loadWallet(false), wallet.pollMs);
  return true;
}

/** 页面加载时静默恢复（不会弹钱包授权框）。 */
async function walletInit() {
  wInstallDiscovery();   // EIP-6963 是同步派发的，这里装完监听再扫一次
  wRescan();

  const saved = lsGet('hlbot.wallet');
  const p = wProvider();
  if (p && p.on) {
    try {
      p.on('accountsChanged', (accts) => {
        if (!wallet.manual) {
          wallet.address = accts && accts.length ? String(accts[0]) : '';
          wallet.data = null;
          if (wallet.address) { lsSet('hlbot.wallet', wallet.address); loadWallet(true); }
          else lsDel('hlbot.wallet');
          renderWalletChip();
          renderWalletDrawer();
        }
      });
      p.on('chainChanged', (cid) => {
        wallet.chainId = cid || '';
        renderWalletDrawer();
        renderWalletChip();
      });
    } catch (e) { /* 某些钱包不支持 on() */ }
  }
  if (p) {
    try {
      const accts = await p.request({ method: 'eth_accounts' });  // 只读，不弹窗
      if (accts && accts.length) {
        wallet.address = String(accts[0]);
        wallet.manual = false;
        wallet.providerName = wList().length ? wList()[0].name : '';
        try { wallet.chainId = (await p.request({ method: 'eth_chainId' })) || p.chainId || ''; } catch (e) { /**/ }
        await loadWallet(true);
        if (wallet.open && !wallet.timer) wallet.timer = setInterval(() => loadWallet(false), wallet.pollMs);
      }
    } catch (e) { /* 忽略 */ }
  } else if (saved) {
    // 没装钱包但有历史地址，先恢复成只读查看
    wallet.address = saved;
  }
  renderWalletChip();
}

/* ---------------- 数据 ---------------- */
async function loadWallet(full) {
  if (!wallet.address) return;
  wallet.loading = true;
  renderWalletChip();
  try {
    const r = await fetch(`/api/wallet/${encodeURIComponent(wallet.address)}?network=${encodeURIComponent(wallet.network)}&full=${full ? 1 : 0}`);
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.detail || d.message || `请求失败 (${r.status})`);
    if (full || !wallet.data) wallet.data = d;
    else {
      // 轻量轮询：只覆盖会变的字段，保留明细
      const prev = wallet.data || {};
      wallet.data = Object.assign({}, prev, {
        perp: d.perp, spot: d.spot, open_orders: d.open_orders, ts: d.ts, errors: d.errors,
      });
    }
    wallet.error = '';
  } catch (e) {
    wallet.error = String((e && e.message) || e);
  }
  wallet.loading = false;
  renderWalletChip();
  renderWalletDrawer();
}

/* ---------------- 顶栏按钮 ---------------- */
function renderWalletChip() {
  const btn = $('btnWallet');
  if (!btn) return;
  const lab = $('wLabel'), val = $('wVal'), dot = $('wDot');
  if (!lab) return;
  btn.classList.remove('connected', 'noprovider');

  if (!wCan() && !wallet.address) {
    btn.classList.add('noprovider');
    lab.textContent = wBlocked() ? '需在浏览器打开' : '未检测到钱包';
    if (dot) dot.className = 'wdot warn';
    if (val) val.textContent = '';
    btn.title = wBlocked()
      ? '当前页面在' + wEmbedKind() + '里，插件钱包无法注入 —— 点击查看解决办法'
      : '没有检测到浏览器钱包插件（安装 MetaMask / Rabby 后刷新）';
    return;
  }
  if (!wallet.address) {
    if (dot) dot.className = 'wdot';
    lab.textContent = '连接钱包';
    if (val) val.textContent = '';
    btn.title = '连接浏览器钱包，查看你在 Hyperliquid 上的真实账户';
    return;
  }
  btn.classList.add('connected');
  if (dot) dot.className = 'wdot on';
  lab.textContent = shortAddr(wallet.address);
  const p = (wallet.data || {}).perp || {};
  if (val) {
    val.textContent = wallet.busy ? '连接中…'
      : (wallet.data ? usd(p.account_value) : (wallet.loading ? '读取中…' : ''));
  }
  btn.title = (wallet.manual ? '（手动填入的地址）' : '已连接' + (wallet.providerName ? ' ' + wallet.providerName : '') + '：') + wallet.address + ' —— 点击查看账户';
}

/* ---------------- 抽屉内容 ---------------- */

/** 页面被嵌在客户端预览窗口里 —— 插件钱包不可能注入，这是「连不上」的头号原因。 */
function wEmbedHtml() {
  const url = wSelfUrl();
  return `<div class="wHelp block">
    <h3>请用系统浏览器打开这个页面</h3>
    <p>当前页面运行在 <b>${esc(wEmbedKind())}</b> 里。MetaMask 这类<b>插件钱包出于防止点击劫持的考虑，
       不会注入到被嵌入的页面</b>，所以在这里永远检测不到它 —— 不是你的钱包坏了。</p>
    <p><b>怎么做：</b>把下面这个地址复制到 Chrome / Edge / Brave 的<b>新标签页</b>里打开，再点「连接钱包」。
       该地址只监听本机，外网访问不到，可以放心。</p>
    <div class="wRow">
      <input type="text" id="wUrl" readonly value="${esc(url)}" />
      <button class="btn sm" id="wCopyUrl">复制</button>
      <a class="btn sm primary" href="${esc(url)}" target="_blank" rel="noreferrer noopener" id="wOpenTab">新窗口打开</a>
    </div>
  </div>`;
}

/** 检测不到钱包时的兜底内容：先分清「环境不让用」还是「真没装」。 */
function installHelpHtml() {
  if (wBlocked()) return wEmbedHtml();
  return `<div class="wHelp">
    <h3>没有检测到浏览器钱包</h3>
    <p>这个页面通过插件钱包（EIP-1193）识别你的地址，本身不接触私钥。装一个再刷新即可：</p>
    <ul class="links">
      <li><a href="https://metamask.io/download/" target="_blank" rel="noreferrer noopener">MetaMask</a></li>
      <li><a href="https://rabby.io/" target="_blank" rel="noreferrer noopener">Rabby Wallet</a></li>
      <li><a href="https://www.okx.com/cn/web3" target="_blank" rel="noreferrer noopener">OKX Wallet</a></li>
    </ul>
    <p class="faint">装好后刷新本页。如果仍然显示「未检测到钱包」，按顺序确认：</p>
    <ol class="faint tips">
      <li>是否在某个应用的<b>内嵌预览窗口</b>里打开的 —— 换成 Chrome / Edge 访问
        <code>${esc(wSelfUrl())}</code>，或者
        <a href="${esc(wSelfUrl())}" target="_blank" rel="noreferrer noopener">在新窗口打开</a>。</li>
      <li>浏览器里是否装了<b>多个钱包插件</b>互相抢占 <code>window.ethereum</code>（下面会列出检测到几个）。</li>
      <li>MetaMask 是否被<b>停用</b>或处于<b>锁定</b>状态。</li>
      <li>MetaMask 是不是装在<b>另一个浏览器</b>里 —— 用装有它的那个浏览器打开本页。</li>
    </ol>
  </div>`;
}

/** 多钱包共存时让用户明确选一个。 */
function wPickHtml() {
  const list = wList();
  if (list.length < 2) return '';
  return `<div class="wSection">
    <div class="wSecHead"><h4>选择要连接的钱包</h4></div>
    <div class="wPicks">
      ${list.map((x, i) => `<button class="btn sm ${i === 0 ? 'primary' : ''}" data-wpick="${i}">${esc(x.name)}</button>`).join('')}
    </div>
    <p class="faint">检测到 ${list.length} 个钱包插件，默认优先 MetaMask。要换一个就点一下。</p>
  </div>`;
}

/** 连接诊断 —— 排查「为什么连不上」时，把这一栏的信息说出来就够了。 */
function wDiagHtml() {
  const list = wList();
  let ethState = '不存在';
  try { ethState = window.ethereum ? '存在' : '不存在'; } catch (e) { ethState = '不可访问'; }
  const rows = [
    ['运行环境', wInIframe() ? ('被嵌入页面 · ' + wEmbedKind()) : '顶层页面（正常）'],
    ['页面地址', wSelfUrl()],
    ['检测到的钱包', list.length ? list.map((x) => x.name).join('、') : '无'],
    ['window.ethereum', ethState],
    ['浏览器', wUA().slice(0, 110) || '未知'],
  ];
  return `<div class="wSection">
    <div class="wSecHead"><h4>连接诊断</h4><span class="spacer"></span>
      <button class="mini" id="wDiagRescan" title="重新检测">⟳</button></div>
    <div class="wDiag">${rows.map((r) => `<div><span>${esc(r[0])}</span><b>${esc(r[1])}</b></div>`).join('')}</div>
    <p class="faint">若「检测到的钱包」显示「无」但你确实装了 MetaMask，问题就在「运行环境」那一行。</p>
  </div>`;
}

/** 未连接时的抽屉：能连就显示连接按钮，同时永远留一条「直接贴地址」的路。 */
function walletEmptyHtml() {
  const list = wList();
  return `${list.length
    ? `<div class="wConnect">
         <button class="btn primary" id="wConnBtn"${wallet.busy ? ' disabled' : ''}>${wallet.busy ? '连接中…' : '连接 ' + esc(list[0].name)}</button>
         <p class="faint">用 MetaMask / Rabby 等插件钱包连接。只会读取地址，不请求签名、不接触私钥。</p>
       </div>`
    : installHelpHtml()}
  ${wPickHtml()}
  <div class="wSection">
    <div class="wSecHead"><h4>${list.length ? '或直接输入地址查看' : '不想装插件？直接输入地址'}</h4></div>
    <p class="faint">粘贴任意 Hyperliquid 账户地址即可查看权益、持仓、挂单和成交。纯只读，不需要任何授权。</p>
    <div class="wRow">
      <input type="text" id="wManual" placeholder="0x…（40 位十六进制地址）" />
      <button class="btn sm" id="wManualGo">查看</button>
    </div>
  </div>
  ${wDiagHtml()}`;
}

function sparkline(points) {
  const pts = (points || []).filter((x) => Array.isArray(x) && isFinite(Number(x[1])));
  if (pts.length < 2) return '<div class="sparkEmpty">暂无历史数据</div>';
  const W = 100, H = 34;
  const ys = pts.map((p) => Number(p[1]));
  const lo = Math.min.apply(null, ys), hi = Math.max.apply(null, ys);
  const span = (hi - lo) || Math.abs(hi) || 1;
  const coords = pts.map((p, i) => {
    const x = (i / (pts.length - 1)) * W;
    const y = H - ((Number(p[1]) - lo) / span) * (H - 4) - 2;
    return `${x.toFixed(2)},${y.toFixed(2)}`;
  }).join(' ');
  const up = ys[ys.length - 1] >= ys[0];
  return `<svg class="spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">
    <polyline points="${coords}" fill="none" stroke="${up ? 'var(--up)' : 'var(--down)'}" stroke-width="1.6" vector-effect="non-scaling-stroke" />
  </svg>`;
}

function renderWalletDrawer() {
  const box = $('wBody');
  if (!box || !wallet.open) return;

  if (!wallet.address) {
    box.innerHTML = walletEmptyHtml();
    bindWalletDrawerEvents();
    return;
  }

  const d = wallet.data || {};
  const p = d.perp || {};
  const positions = p.positions || [];
  const orders = d.open_orders || [];
  const spot = d.spot || [];
  const fills = d.fills || [];
  const errors = d.errors || [];
  const upnl = positions.reduce((s, x) => s + Number(x.unrealized_pnl || 0), 0);
  const ch = chainInfo(wallet.chainId);
  const badChain = !wallet.manual && wCan() && wallet.chainId && !ch.hl;

  const hist = (d.history || {})[wallet.range] || [];
  const rangeLabel = { day: '24 小时', week: '7 天', month: '30 天' };

  box.innerHTML = `
    <div class="wAddr">
      <div class="wAddrTop">
        <span class="mono wAddress">${esc(wallet.address)}</span>
        <button class="mini" id="wCopy" title="复制地址">⧉</button>
      </div>
      <div class="wAddrMeta">
        ${wallet.manual
          ? '<span class="badge mode">手动查看（只读）</span>'
          : `<span class="badge ${ch.hl ? 'paper' : 'live'}">${esc(ch.name)}</span>`}
        <span class="badge mode">${esc(d.network === 'testnet' ? '测试网' : '主网')}</span>
        ${d.ts ? `<span class="faint">更新于 ${hms(d.ts)}</span>` : ''}
        ${wallet.loading ? '<span class="faint">刷新中…</span>' : ''}
      </div>
    </div>

    ${badChain ? `<div class="wWarn">当前钱包网络是 <b>${esc(ch.name)}</b>，Hyperliquid 入金走 <b>Arbitrum One</b>。
      交易本身在 Hyperliquid 链上、不受影响，只是入金前需要切网。
      <button class="btn sm" id="wSwitch">切换到 Arbitrum One</button></div>` : ''}

    ${wallet.error ? `<div class="wErr">查询失败：${esc(wallet.error)}</div>` : ''}

    <div class="wGrid">
      <div class="wCard"><label>账户权益</label><b>${usd(p.account_value)}</b></div>
      <div class="wCard"><label>可用保证金</label><b>${usd(p.withdrawable)}</b></div>
      <div class="wCard"><label>占用保证金</label><b>${usd(p.margin_used)}</b></div>
      <div class="wCard"><label>名义敞口</label><b>${usd(p.total_notional)}</b></div>
      <div class="wCard"><label>未实现盈亏</label><b class="${pnlCls(upnl)}">${positions.length ? pnl(upnl) : '—'}</b></div>
      <div class="wCard"><label>挂单 / 持仓</label><b>${orders.length} / ${positions.length}</b></div>
    </div>

    <div class="wSection">
      <div class="wSecHead">
        <h4>权益曲线</h4>
        <div class="spacer"></div>
        <div class="wRanges">
          ${['day', 'week', 'month'].map((r) =>
            `<button class="mini rng ${wallet.range === r ? 'on' : ''}" data-range="${r}">${rangeLabel[r]}</button>`).join('')}
        </div>
      </div>
      ${sparkline(hist)}
      ${hist.length ? (() => {
        const first = Number(hist[0][1]), last = Number(hist[hist.length - 1][1]);
        const diff = last - first;
        const pctv = first ? (diff / Math.abs(first)) * 100 : 0;
        return `<div class="wHistInfo">${hist.length} 个采样点 · 区间变化
          <b class="${pnlCls(diff)}">${diff >= 0 ? '+' : ''}${num(diff, 2)}（${pctv >= 0 ? '+' : ''}${num(pctv, 2)}%）</b></div>`;
      })() : ''}
    </div>

    <div class="wSection">
      <div class="wSecHead"><h4>永续持仓</h4></div>
      ${positions.length ? `<table class="tbl"><thead><tr>
          <th>标的</th><th>方向</th><th>数量</th><th>开仓均价</th><th>标记价值</th><th>未实现</th><th>回报率</th><th>强平价</th><th>杠杆</th>
        </tr></thead><tbody>${positions.map((x) => `<tr>
          <td><b>${esc(x.coin)}</b></td>
          <td><span class="pill ${x.size > 0 ? 'buy' : 'sell'}">${x.size > 0 ? '多' : '空'}</span></td>
          <td class="mono">${sz(Math.abs(x.size))}</td>
          <td class="mono">${px(x.entry_px)}</td>
          <td class="mono">${usd(x.value)}</td>
          <td class="mono ${pnlCls(x.unrealized_pnl)}">${pnl(x.unrealized_pnl)}</td>
          <td class="mono ${pnlCls(x.roe)}">${num(x.roe * 100, 2)}%</td>
          <td class="mono">${px(x.liquidation_px)}</td>
          <td class="mono faint">${num(x.leverage, 0)}x ${esc(x.leverage_type)}</td>
        </tr>`).join('')}</tbody></table>`
        : '<div class="empty">该账户当前没有永续持仓</div>'}
    </div>

    <div class="wSection">
      <div class="wSecHead"><h4>交易所挂单</h4><span class="faint">${orders.length} 笔</span></div>
      ${orders.length ? `<table class="tbl"><thead><tr>
          <th>时间</th><th>标的</th><th>方向</th><th>数量</th><th>价格</th><th>类型</th><th>属性</th>
        </tr></thead><tbody>${orders.slice(0, 60).map((o) => `<tr>
          <td class="mono faint">${tstr(o.ts)}</td>
          <td><b>${esc(o.coin)}</b></td>
          <td><span class="pill ${o.side}">${o.side === 'buy' ? '买' : '卖'}</span></td>
          <td class="mono">${sz(o.size)}</td>
          <td class="mono">${o.is_trigger ? px(o.trigger_px) : px(o.price)}</td>
          <td class="mono faint">${esc(o.order_type || '—')}</td>
          <td>${o.reduce_only ? '<span class="pill idle">只减仓</span>' : ''}${o.is_trigger ? '<span class="pill pending">触发</span>' : ''}</td>
        </tr>`).join('')}</tbody></table>`
        : '<div class="empty">没有挂单</div>'}
    </div>

    ${spot.length ? `<div class="wSection">
      <div class="wSecHead"><h4>现货余额</h4></div>
      <table class="tbl"><thead><tr><th>币种</th><th>总量</th><th>冻结</th><th>成本</th></tr></thead>
      <tbody>${spot.slice(0, 30).map((b) => `<tr>
        <td><b>${esc(b.coin)}</b></td><td class="mono">${num(b.total, 4)}</td>
        <td class="mono faint">${num(b.hold, 4)}</td><td class="mono faint">${num(b.entry_ntl, 2)}</td>
      </tr>`).join('')}</tbody></table>
    </div>` : ''}

    <div class="wSection">
      <div class="wSecHead"><h4>最近成交</h4><span class="faint">最多 60 条</span></div>
      ${fills.length ? `<table class="tbl"><thead><tr>
          <th>时间</th><th>标的</th><th>方向</th><th>数量</th><th>价格</th><th>已实现盈亏</th><th>手续费</th>
        </tr></thead><tbody>${fills.slice(0, 40).map((f) => `<tr>
          <td class="mono faint">${tstr(f.ts)}</td>
          <td><b>${esc(f.coin)}</b></td>
          <td><span class="pill ${f.side}">${f.side === 'buy' ? '买' : '卖'}</span></td>
          <td class="mono">${sz(f.size)}</td>
          <td class="mono">${px(f.price)}</td>
          <td class="mono ${pnlCls(f.closed_pnl)}">${f.closed_pnl ? pnl(f.closed_pnl) : '—'}</td>
          <td class="mono faint">${num(f.fee, 4)}</td>
        </tr>`).join('')}</tbody></table>`
        : '<div class="empty">没有成交记录</div>'}
    </div>

    <div class="wSection">
      <div class="wSecHead"><h4>绑定为交易账户</h4></div>
      <p class="faint">把上面的地址写进某个实例的 <code>env.account_address</code>，让 bot 在这个账户上下单。
        私钥仍然只从 <code>.env</code> 读取，网页不会保存任何私钥。</p>
      <div class="wRow">
        <select id="wBindSlot">
          <option value="">选择实例…</option>
          ${state.slots.map((s) => `<option value="${esc(s.name)}" ${s.running ? 'disabled' : ''}>
            ${esc(s.name)} · ${esc((s.market || {}).coin || '?')}${s.running ? '（运行中，不可改）' : ''}
          </option>`).join('')}
        </select>
        <button class="btn primary sm" id="wBindGo">绑定</button>
      </div>
      ${state.slots.some((s) => (s.env || {}).account_address === wallet.address)
        ? '<div class="wOkHint">✓ 已有实例使用该地址作为交易账户</div>' : ''}
    </div>

    <div class="wSection">
      <div class="wSecHead"><h4>手续费等级</h4></div>
      ${d.fees && (d.fees.cross_rate || d.fees.add_rate) ? `<div class="wGrid small">
        <div class="wCard"><label>吃单费率</label><b>${num((d.fees.cross_rate || 0) * 100, 4)}%</b></div>
        <div class="wCard"><label>挂单费率</label><b>${num((d.fees.add_rate || 0) * 100, 4)}%</b></div>
        <div class="wCard"><label>返佣折扣</label><b>${num((d.fees.referral_discount || 0) * 100, 2)}%</b></div>
      </div>` : '<div class="empty">未取到费率信息</div>'}
    </div>

    ${errors.length ? `<div class="wErr">部分数据获取失败：<br>${errors.map(esc).join('<br>')}</div>` : ''}

    <div class="wFoot">
      ${wallet.manual
        ? '<button class="btn sm" id="wForget">清空此地址</button>'
        : '<button class="btn sm" id="wDisconnect">断开连接</button>'}
      <span class="faint">数据来自 Hyperliquid 公开接口，只读</span>
    </div>`;

  bindWalletDrawerEvents();
}

function bindWalletDrawerEvents() {
  const on = (id, fn) => { const el = $(id); if (el) el.onclick = fn; };

  on('wConnBtn', () => walletConnect());
  on('wCopy', async () => {
    try {
      if (navigator.clipboard) await navigator.clipboard.writeText(wallet.address);
      else throw new Error('no clipboard');
      toast('地址已复制', 'ok');
    } catch (e) { toast('复制失败，请手动选中', 'warn'); }
  });
  on('wCopyUrl', async () => {
    const url = wSelfUrl();
    try {
      if (navigator.clipboard) await navigator.clipboard.writeText(url);
      else throw new Error('no clipboard');
      toast('地址已复制，粘贴到浏览器新标签页打开', 'ok');
    } catch (e) {
      const el = $('wUrl');
      if (el && el.select) { el.select(); toast('已选中，按 Ctrl+C 复制', 'warn'); }
      else toast('请手动复制：' + url, 'warn');
    }
  });
  on('wDiagRescan', () => { wRescan(); renderWalletDrawer(); toast('已重新检测钱包', 'ok'); });
  on('wSwitch', () => walletSwitchChain());
  on('wDisconnect', () => walletDisconnect());
  on('wForget', () => walletDisconnect());
  on('wBindGo', () => walletBind());
  on('wManualGo', () => {
    const el = $('wManual');
    if (el && walletManual(el.value)) { const x = $('wManual'); if (x) x.value = ''; }
  });

  const rng = $('wBody') && $('wBody').querySelectorAll ? $('wBody').querySelectorAll('.rng') : [];
  Array.prototype.forEach.call(rng || [], (b) => {
    b.onclick = () => { wallet.range = b.dataset.range || 'day'; renderWalletDrawer(); };
  });

  // 多钱包：点一下切换要连接的钱包
  const picks = $('wBody') && $('wBody').querySelectorAll ? $('wBody').querySelectorAll('[data-wpick]') : [];
  Array.prototype.forEach.call(picks || [], (b) => {
    b.onclick = () => {
      const i = Number(b.dataset.wpick || 0);
      _wChosen = _wList[i] || null;
      renderWalletDrawer();
    };
  });

  const sel = $('wBindSlot');
  if (sel) {
    if (wallet.bindSlot) sel.value = wallet.bindSlot;
    sel.onchange = () => { wallet.bindSlot = sel.value; };
  }
}

async function walletSwitchChain() {
  const p = wProvider();
  if (!p) return;
  try {
    await p.request({ method: 'wallet_switchEthereumChain', params: [{ chainId: '0xa4b1' }] });
  } catch (e) {
    if (e && (e.code === 4902 || e.code === -32603)) {
      try {
        await p.request({ method: 'wallet_addEthereumChain', params: [HL_ADD_CHAIN['0xa4b1']] });
      } catch (e2) { toast('添加网络失败：' + ((e2 && e2.message) || e2), 'err'); }
    } else if (e && e.code === 4001) {
      toast('你在钱包里取消了切换', 'warn');
    } else {
      toast(wErr(e, '切换网络').text, 'err');
    }
  }
}

async function walletBind() {
  if (!wallet.address) return;
  const slot = wallet.bindSlot || ($('wBindSlot') && $('wBindSlot').value) || '';
  if (!slot) { toast('请先选择要绑定的实例', 'warn'); return; }
  const ok = await confirmDialog({
    title: '绑定交易账户',
    body: `把实例 <b>${esc(slot)}</b> 的交易账户设为<br><span class="mono">${esc(wallet.address)}</span><br><br>
           bot 之后会用 <code>.env</code> 里的 API 钱包私钥在这个账户上下单。<br>
           <span class="warnText">请确认这个地址就是你的主钱包地址，绑错会导致下到别人的账户（会被拒单）。</span>`,
    okText: '确认绑定',
  });
  if (!ok) return;
  const r = await api('POST', '/api/wallet/bind', { address: wallet.address, slot });
  toast(r.message, r.ok ? 'ok' : 'err');
}

/* ==================== 顶部钱包按钮 ==================== */
$('btnWallet').onclick = () => {
  if (wallet.address) { walletOpen(); return; }
  if (wCan()) walletConnect();     // 有插件钱包：直接连
  else walletOpen();               // 没装：打开抽屉给安装指引 + 手动输地址
};
const _wClose = $('wClose');
if (_wClose) _wClose.onclick = () => walletClose();
const _wScrim = $('wScrim');
if (_wScrim) _wScrim.onclick = () => walletClose();
const _wRefresh = $('wRefresh');
if (_wRefresh) _wRefresh.onclick = () => { if (wallet.address) loadWallet(true); };

if (typeof window !== 'undefined' && window.addEventListener) {
  window.addEventListener('keydown', (e) => { if (e && e.key === 'Escape' && wallet.open) walletClose(); });
}

/* ==================== 启动 ==================== */
connect();
walletInit();
