/**
 * 日光账本 · 前端
 *
 * 数据全部来自后端 API，不再内置模拟数据。
 * 三层结构：房屋 → 房间 → 曲线。
 */
const API = '';                 // 同源部署，留空即可
const $ = s => document.querySelector(s);

const state = {
  houses: [],
  houseId: null,
  rooms: [],
  roomId: null,
  curve: null,
  ledger: null,
  metric: 'hourly',
  days: 3,
};

/* ---------------- API ---------------- */
async function get(path) {
  const r = await fetch(API + path);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

const api = {
  houses:        ()      => get('/api/houses'),
  rooms:         (h)     => get(`/api/houses/${h}/rooms`),
  curve:         (r,hrs) => get(`/api/rooms/${r}/curve?hours=${hrs}`),
  ledger:        (r,d)   => get(`/api/rooms/${r}/ledger?days=${d}`),
  events:        (r)     => get(`/api/rooms/${r}/events?limit=30`),
  health:        ()      => get('/api/devices/health'),
  overview:      ()      => get('/api/overview'),
  review:        (r,d)   => get(`/api/rooms/${r}/review?days=${d}`),
  cmd:           (b)     => post('/api/cmd', b),
};

async function post(path, body) {
  const r = await fetch(API + path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

/* ---------------- 渲染：房屋 ---------------- */
function renderHouses() {
  const el = $('#houseTabs');
  if (!state.houses.length) {
    el.innerHTML = '<div class="empty">数据库里还没有房屋</div>';
    return;
  }
  el.innerHTML = state.houses.map(h => `
    <div class="tab ${h.id === state.houseId ? 'active' : ''}" data-id="${h.id}">
      ${h.name}<span class="tab-sub">${h.room_count} 个房间</span>
    </div>`).join('');
  el.querySelectorAll('.tab').forEach(t => {
    t.onclick = () => selectHouse(+t.dataset.id);
  });
}

async function selectHouse(id) {
  state.houseId = id;
  state.rooms = await api.rooms(id);
  // 默认选中"最近有读数"的房间：按 last_seen_at 取最新，
  // 没有设备活动的退回第一个。演示场景下旧房间时间窗一过
  // 曲线就是空的，谁都不想看空页。
  state.roomId = state.rooms.length
    ? (state.rooms.slice().sort((a, b) =>
        String(b.last_seen_at || "").localeCompare(String(a.last_seen_at || "")))[0].room_id)
    : null;
  renderHouses();
  renderRooms();
  if (state.roomId) await loadRoomData();
  renderCompare();
}

const GRADE_COLOR = {
  'A':  ['#4ade80', 'rgba(74,222,128,.15)'],
  'B+': ['#4ade80', 'rgba(74,222,128,.15)'],
  'C':  ['#fbbf24', 'rgba(251,191,36,.15)'],
  'D':  ['#f87171', 'rgba(248,113,113,.15)'],
};
const GC = g => (GRADE_COLOR[g] || ['#9aa0aa', 'rgba(154,160,170,.15)']);

/* ---------------- 渲染：房间卡片 ---------------- */
function renderRooms() {
  const el = $('#rooms');
  if (!state.rooms.length) {
    el.innerHTML = '<div class="empty">该房屋下还没有房间</div>';
    return;
  }
  el.innerHTML = state.rooms.map(r => {
    const [c, bg] = GC(r.grade);
    const lamp = r.lamp_inflation || 0;
    const stale = r.device_status === 'online' ? '' :
      '<span class="dev-flag">设备离线</span>';
    return `
      <div class="room ${r.room_id === state.roomId ? 'active' : ''}"
           data-id="${r.room_id}" style="--grade-color:${c};--grade-bg:${bg}">
        <div class="room-name">${r.room_name}
          <span class="room-dir">${r.orientation || '—'}</span></div>
        <div class="room-meta">${r.device_id || '未绑定设备'} ${stale}</div>
        <div class="room-score">
          <span class="score-num" style="color:${c}">${r.score ?? '—'}</span>
          <span class="score-grade" style="--grade-color:${c};--grade-bg:${bg}">
            ${r.grade || '无数据'}</span>
        </div>
        <div class="room-stats">
          <div><span class="stat-label">日照</span>
               <span class="stat-val">${r.sun_minutes ?? 0} 分</span></div>
          <div><span class="stat-label">峰值</span>
               <span class="stat-val">${r.peak_mv ?? 0} mV</span></div>
        </div>
        ${lamp > 0 ? `<div class="lamp-flag">灯光虚增 +${lamp} 分</div>` : ''}
      </div>`;
  }).join('');
  el.querySelectorAll('.room').forEach(r => {
    r.onclick = async () => {
      state.roomId = +r.dataset.id;
      renderRooms();
      await loadRoomData();
    };
  });
}

/* ---------------- 加载房间数据 ---------------- */
async function loadRoomData() {
  const hours = state.metric === 'hourly' ? 24 : 24 * state.days;
  const [curve, ledger, events] = await Promise.all([
    api.curve(state.roomId, hours),
    api.ledger(state.roomId, state.days),
    api.events(state.roomId),
  ]);
  state.curve = curve;
  state.ledger = ledger;
  state.events = events;
  renderChart();
  renderNote();
  renderFindings();
  renderEvents(events);
  renderDayTable(ledger.days);
  loadReview();   // 异步独立加载，不阻塞图表
}

/* ---------------- 图表 ---------------- */
let chart = null;

function ensureChart() {
  if (chart) return;
  chart = echarts.init($('#chartMain'));
  window.addEventListener('resize', () => chart.resize());
}

function renderChart() {
  ensureChart();
  const room = state.rooms.find(r => r.room_id === state.roomId) || {};
  $('#chartTitle').textContent =
    `${room.room_name || ''} · ${room.orientation || ''} 光照曲线`;

  if (state.metric === 'hourly') {
    const s = state.curve.series;
    chart.setOption({
      grid: { left: 56, right: 24, top: 40, bottom: 44 },
      tooltip: {
        trigger: 'axis',
        backgroundColor: 'rgba(23,26,33,.95)',
        borderColor: '#2a2f3a',
        textStyle: { color: '#e8eaed', fontSize: 12 },
        formatter: ps => {
          let out = `${ps[0].axisValue}<br/>`;
          ps.forEach(p => {
            if (p.seriesName === '自然采光') out += `自然 <b style="color:#fbbf24">${p.value}</b> mV<br/>`;
            if (p.seriesName === '人工照明') out += `灯光 <b style="color:#60a5fa">${p.value}</b> mV<br/>`;
            if (p.seriesName === '累计日照') out += `累计 <b style="color:#4ade80">${p.value}</b> 分钟`;
          });
          return out;
        },
      },
      legend: { data: ['自然采光', '人工照明'], top: 6,
                textStyle: { color: '#9aa0aa' } },
      xAxis: { type: 'time', axisLabel: { color: '#6b7280' },
               axisLine: { lineStyle: { color: '#2a2f3a' } } },
      yAxis: { type: 'value', name: 'mV', nameTextStyle: { color: '#6b7280' },
               axisLabel: { color: '#6b7280' },
               splitLine: { lineStyle: { color: '#1e222b' } } },
      series: [
        {
          name: '自然采光', type: 'line', smooth: true, symbol: 'none',
          data: s.map(p => [p.ts, p.natural_mv]),
          lineStyle: { width: 2, color: '#fbbf24' },
          areaStyle: {
            color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: 'rgba(251,191,36,.28)' },
              { offset: 1, color: 'rgba(251,191,36,.02)' },
            ]),
          },
        },
        {
          name: '人工照明', type: 'line', step: 'middle', symbol: 'none',
          data: s.map(p => [p.ts, p.lamp_mv]),
          lineStyle: { width: 2, color: '#60a5fa' },
          areaStyle: { color: 'rgba(96,165,250,.12)' },
        },
      ],
    }, true);
  } else {
    const d = state.ledger.days;
    chart.setOption({
      grid: { left: 56, right: 24, top: 40, bottom: 44 },
      tooltip: { trigger: 'axis',
                 backgroundColor: 'rgba(23,26,33,.95)',
                 borderColor: '#2a2f3a', textStyle: { color: '#e8eaed' } },
      legend: { data: ['有效日照', '灯光虚增'], top: 6,
                textStyle: { color: '#9aa0aa' } },
      xAxis: { type: 'category', data: d.map(x => x.date.slice(5)),
               axisLabel: { color: '#6b7280' },
               axisLine: { lineStyle: { color: '#2a2f3a' } } },
      yAxis: { type: 'value', name: '分钟',
               nameTextStyle: { color: '#6b7280' },
               axisLabel: { color: '#6b7280' },
               splitLine: { lineStyle: { color: '#1e222b' } } },
      series: [
        { name: '有效日照', type: 'bar', barWidth: 18,
          data: d.map(x => x.sun_minutes),
          itemStyle: { color: '#fbbf24', borderRadius: [4,4,0,0] } },
        { name: '灯光虚增', type: 'bar', barWidth: 18,
          data: d.map(x => x.lamp_inflation),
          itemStyle: { color: 'rgba(96,165,250,.55)',
                       borderRadius: [4,4,0,0] } },
      ],
    }, true);
  }
}

function renderNote() {
  const note = $('#chartNote');
  if (state.metric !== 'hourly') {
    note.textContent =
      '黄色为自然光照时长，蓝色为若混入灯光照明会多算的部分。';
    return;
  }
  const last = state.ledger.days[state.ledger.days.length - 1];
  if (!last) { note.textContent = '暂无数据。'; return; }
  const over = last.lamp_inflation || 0;
  note.innerHTML = over > 0
    ? `当日若不做光源区分、直接用总照度统计，会得到
       <strong style="color:var(--warn)">${last.naive_minutes} 分钟</strong>，
       而真实自然日照只有
       <strong style="color:var(--sun)">${last.sun_minutes} 分钟</strong>，
       虚高 <strong>${Math.round(over / Math.max(1, last.sun_minutes) * 100)}%</strong>。`
    : `当日自然日照
       <strong style="color:var(--sun)">${last.sun_minutes} 分钟</strong>，
       峰值 <strong>${last.peak_mv} mV</strong>，未检测到灯光干扰。`;
}

/* ---------------- 横向对比 ---------------- */
function renderCompare() {
  const el = $('#cmpBars');
  const rooms = state.rooms.filter(r => r.sun_minutes !== null);
  if (!rooms.length) { el.innerHTML = '<div class="empty">暂无对比数据</div>'; return; }
  const max = Math.max(...rooms.map(r => r.sun_minutes));
  el.innerHTML = rooms.map(r => {
    const [c] = GC(r.grade);
    const pct = max ? (r.sun_minutes / max * 100).toFixed(1) : 0;
    return `
      <div class="cmp-row">
        <span class="cmp-label">${r.room_name} ${r.orientation || ''}</span>
        <span class="cmp-bar-wrap">
          <span class="cmp-bar" style="width:${pct}%;background:${c}"></span>
        </span>
        <span class="cmp-val">${r.sun_minutes} 分 · ${r.grade || '—'}
          （${r.score ?? '—'}）</span>
      </div>`;
  }).join('');

  const sorted = [...rooms].sort((a, b) => b.sun_minutes - a.sun_minutes);
  const best = sorted[0], worst = sorted[sorted.length - 1];
  const gap = best.sun_minutes - worst.sun_minutes;
  const note = $('#cmpNote');
  if (worst.sun_minutes >= 30) {
    note.innerHTML =
      `同一套房里，<strong style="color:var(--accent)">${best.room_name}</strong> 与
       <strong>${worst.room_name}</strong> 的有效日照相差
       <strong>${gap} 分钟</strong>，相差
       ${(gap / worst.sun_minutes).toFixed(1)} 倍。`;
  } else {
    note.innerHTML =
      `<strong>${worst.room_name}</strong> 基本全天达不到有效日照阈值
       （累计 ${worst.sun_minutes} 分钟）。`;
  }
}

/* ---------------- 关键发现 ---------------- */
function renderFindings() {
  const out = [];
  state.rooms.forEach(r => {
    const lamp = r.lamp_inflation || 0;
    if (lamp > 30) {
      out.push({ cls: 'lamp', icon: '◐',
        text: `<b>${r.room_name}</b>：若不区分光源，当日会被多算
               <strong>${lamp} 分钟</strong>日照。累计起来足够把档位从
               C 抬到 B+，让人误判"这房间朝北也没关系"。` });
    }
    if (r.device_status !== 'online') {
      out.push({ icon: '⚠',
        text: `<b>${r.room_name}</b> 的设备
               ${r.device_status === 'offline' ? '当前离线' : '从未上报'}，
               页面数据可能不是最新的。` });
    }
  });

  const storm = (state.ledger.days || []).reduce(
    (a, b) => (b.sun_minutes < (a?.sun_minutes ?? 1e9) ? b : a), null);
  if (storm) {
    out.push({ icon: '☁',
      text: `<b>${storm.date}</b> 是这几天最差的一天：有效日照仅
             ${storm.sun_minutes} 分钟，峰值 ${storm.peak_mv} mV。
             单日结论会误判，建议以多日中位数为准。` });
  }

  $('#findings').innerHTML = out.length
    ? out.map(f => `<div class="finding ${f.cls || ''}">
        <span class="finding-icon">${f.icon}</span>
        <span>${f.text}</span></div>`).join('')
    : '<div class="empty">暂无需关注的问题</div>';
}

/* ---------------- 事件表 ---------------- */
function renderEvents(events) {
  $('#events').innerHTML = events.length
    ? events.map(e => `<tr>
        <td>${(e.ts || '').slice(5, 16)}</td>
        <td>${e.type}</td><td>${e.label || ''}</td>
        <td>${(e.detail || '').slice(0, 40)}</td></tr>`).join('')
    : '<tr><td colspan="4" class="empty">暂无事件</td></tr>';
}

/* ---------------- 日账表 ---------------- */
function renderDayTable(days) {
  $('#ledgerBody').innerHTML = days.length
    ? days.map(d => `<tr>
        <td>${d.date}</td>
        <td>${d.sun_minutes} 分</td>
        <td>${d.peak_mv} mV</td>
        <td style="color:${d.lamp_inflation > 0 ? 'var(--lamp)' : 'var(--text-3)'}">
          ${d.lamp_inflation > 0 ? '+' + d.lamp_inflation + ' 分' : '无'}</td>
        <td style="color:${GC(d.grade)[0]}">${d.score}</td>
        <td>${d.grade}</td></tr>`).join('')
    : '<tr><td colspan="6" class="empty">暂无账本记录</td></tr>';
}

/* ---------------- AI 房评报告（真调 /review 接口） ---------------- */
async function loadReview() {
  const card = $('#reviewCard'), badge = $('#reviewSource');
  if (!state.roomId) { card.innerHTML = '<div class="empty">先选一个房间</div>'; return; }
  badge.textContent = '模型分析中…'; badge.className = 'badge';
  card.innerHTML = '<div class="empty">读取近 ' + state.days + ' 天光况，正在生成评估…</div>';
  let r;
  try {
    r = await api.review(state.roomId, state.days);
  } catch (e) {
    badge.textContent = '生成失败'; card.innerHTML = '<div class="empty">' + e.message + '</div>';
    return;
  }
  if (r.source === 'nim') {
    badge.textContent = '● 模型生成 · ' + (r.provider || '');
    badge.className = 'badge badge-ai';
  } else {
    badge.textContent = '● 本地统计（模型不可用）';
    badge.className = 'badge badge-local';
  }
  const gc = { A: '#34d399', B: '#a3e635', C: '#fbbf24', D: '#f87171' }[r.grade] || '#94a3b8';
  const st = r.stats || {};
  const rows = (st.raw_days || []).map(d =>
    '<tr><td>' + d.date + '</td><td>' + d.sun_min + ' 分</td><td>' + d.peak_mv +
    '</td><td>' + d.lamp_min + ' 分</td></tr>').join('');
  card.innerHTML =
    '<div class="rv-head">' +
      '<div class="rv-grade" style="color:' + gc + ';border-color:' + gc + '">' + (r.grade || '?') + '</div>' +
      '<div class="rv-headline">' + (r.headline || '') + '</div>' +
    '</div>' +
    '<p><b>晾晒：</b>' + (r.laundry || '—') + '</p>' +
    '<p><b>摆位/补光：</b>' + (r.plant_light || '—') + '</p>' +
    '<p class="rv-caveat"><b>给买家的实话：</b>' + (r.caveat || '—') + '</p>' +
    (r.note ? '<p class="rv-note">※ ' + r.note + '</p>' : '') +
    '<p class="rv-note">数据可信度：' + (r.quality || []).join('；') + '</p>' +
    (rows ? '<table><thead><tr><th>日期</th><th>有效日照</th><th>峰值</th><th>灯光时长</th></tr></thead><tbody>' + rows + '</tbody></table>' : '');
}

/* ---------------- 设备健康度 ---------------- */
async function renderHealth() {
  try {
    const hs = await api.health();
    $('#health').innerHTML = hs.length
      ? hs.map(h => {
          const bad = h.freshness !== 'live';
          return `<span class="health-chip ${bad ? 'bad' : ''}">
            ${h.device_id} · ${bad ? h.freshness : h.status}
            ${h.csq != null ? ` · CSQ ${h.csq}` : ''}</span>`;
        }).join('')
      : '<span class="health-chip">暂无设备</span>';
  } catch (e) {
    $('#health').innerHTML = '<span class="health-chip bad">后端未连接</span>';
  }
}

/* ---------------- 初始化 ---------------- */
$('#metricSel').onchange = async e => {
  state.metric = e.target.value;
  if (state.roomId) await loadRoomData();
};

$('#daysSel').onchange = async e => {
  state.days = +e.target.value;
  if (state.roomId) await loadRoomData();
};

$('#refreshBtn').onclick = async () => {
  await renderHealth();
  if (state.houseId) await selectHouse(state.houseId);
};

(async function init() {
  try {
    state.houses = await api.houses();
    if (!state.houses.length) {
      renderHouses();
      renderRooms();
      return;
    }
    await selectHouse(state.houses[0].id);
    $('#btnReview').addEventListener('click', loadReview);
    await renderHealth();
    setInterval(renderHealth, 60000);
  } catch (e) {
    document.querySelector('.wrap').insertAdjacentHTML('afterbegin',
      `<div class="banner">无法连接后端：${e.message}<br/>
       请确认已执行 <code>python app.py</code></div>`);
  }
})();