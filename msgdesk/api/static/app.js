const VIEWS = ['messages', 'sources', 'basic', 'model', 'profile'];
const SETTINGS_VIEWS = ['basic', 'sources', 'model', 'profile'];
const VIEW_TITLES = {
  messages: '消息',
  sources: '设置',
  basic: '设置',
  model: '设置',
  profile: '设置'
};
const PRIORITY_LABEL = { urgent: '紧急', high: '高', normal: '普通', low: '低' };
const STATUS_LABEL = {
  pending: '待处理',
  processing: '处理中',
  processed: '已处理',
  failed: '失败',
  ignored: '已忽略',
  success: '已完成'
};
const TYPE_LABEL = {
  web: '网站',
  ai_web: '网站',
  qq_onebot: 'QQ'
};

const state = {
  view: 'messages',
  status: null,
  sources: [],
  messages: [],
  filter: 'all',
  search: '',
  sourceId: '',
  date: '',
  lastDate: '',
  selected: new Set(),
  messageSignature: '',
  digestDate: '',
  digestContent: null,
  digestRecord: null,
  digestSource: false,
  model: null,
  modelProviders: [],
  modelEditingId: null,
  modelNames: [],
  promptFiles: [],
  qqGroups: [],
  qqSelected: new Set(),
  qqLoggedIn: false,
  sourceCategory: '',
  visionSupported: false,
  sourceEditingId: null,
  sourceFormConfig: {}
};

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[ch]));
}

function safeUrl(value) {
  try {
    const url = new URL(String(value));
    return ['http:', 'https:'].includes(url.protocol) ? url.href : '';
  } catch (error) {
    return '';
  }
}

function fmtTime(value) {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false
  });
}

function fmtFullTime(value) {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString('zh-CN', { hour12: false });
}

function dateKey(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value || '').slice(0, 10);
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${date.getFullYear()}-${month}-${day}`;
}

function localToday() {
  return dateKey(new Date());
}

let datePopEl = null;

function closeDatePicker() {
  if (datePopEl) {
    datePopEl.remove();
    datePopEl = null;
  }
}

document.addEventListener('click', () => closeDatePicker());
document.addEventListener('keydown', event => {
  if (event.key === 'Escape') closeDatePicker();
});

function openDatePicker(anchor, current, onPick) {
  // Defer so the click that opened the picker does not immediately close it.
  setTimeout(() => buildDatePicker(anchor, current, onPick), 0);
}

function buildDatePicker(anchor, current, onPick) {
  closeDatePicker();
  const today = localToday();
  const initial = /^\d{4}-\d{2}-\d{2}$/.test(current || '') ? current : today;
  let [year, month] = initial.split('-').map(Number);
  const panel = document.createElement('div');
  panel.className = 'date-pop';
  document.body.appendChild(panel);
  datePopEl = panel;
  panel.addEventListener('click', event => event.stopPropagation());

  const draw = () => {
    const startWeekday = (new Date(year, month - 1, 1).getDay() + 6) % 7;
    const daysInMonth = new Date(year, month, 0).getDate();
    const prevMonthDays = new Date(year, month - 1, 0).getDate();
    const cells = [];
    for (let index = startWeekday - 1; index >= 0; index -= 1) {
      cells.push({
        day: prevMonthDays - index, out: true,
        y: month === 1 ? year - 1 : year, m: month === 1 ? 12 : month - 1
      });
    }
    for (let day = 1; day <= daysInMonth; day += 1) {
      cells.push({ day, out: false, y: year, m: month });
    }
    let nextDay = 1;
    while (cells.length % 7 !== 0) {
      cells.push({
        day: nextDay++, out: true,
        y: month === 12 ? year + 1 : year, m: month === 12 ? 1 : month + 1
      });
    }
    panel.innerHTML = `
      <div class="date-pop-head">
        <span class="date-pop-title">${year} 年 ${month} 月</span>
        <span class="date-pop-nav">
          <button type="button" data-nav="-1" aria-label="上个月">‹</button>
          <button type="button" data-nav="1" aria-label="下个月">›</button>
        </span>
      </div>
      <div class="date-pop-grid">
        ${['一', '二', '三', '四', '五', '六', '日']
          .map(day => `<span class="date-pop-week">${day}</span>`).join('')}
        ${cells.map(cell => {
          const key = `${cell.y}-${String(cell.m).padStart(2, '0')}-${String(cell.day).padStart(2, '0')}`;
          const classes = ['date-pop-day'];
          if (cell.out) classes.push('out');
          if (key === today) classes.push('today');
          if (key === initial) classes.push('sel');
          return `<button type="button" class="${classes.join(' ')}" data-date="${key}">${cell.day}</button>`;
        }).join('')}
      </div>
      <div class="date-pop-foot">
        <button type="button" class="btn tiny" data-date="">清除</button>
        <button type="button" class="btn tiny" data-date="${today}">今天</button>
      </div>`;
  };
  draw();

  panel.addEventListener('click', event => {
    const nav = event.target.closest('[data-nav]');
    if (nav) {
      month += Number(nav.dataset.nav);
      if (month < 1) { month = 12; year -= 1; }
      if (month > 12) { month = 1; year += 1; }
      draw();
      return;
    }
    const pick = event.target.closest('[data-date]');
    if (pick) {
      const value = pick.dataset.date || '';
      closeDatePicker();
      onPick(value);
    }
  });

  placePop(panel, anchor);
}

function placePop(panel, anchor) {
  const rect = anchor.getBoundingClientRect();
  const height = panel.offsetHeight;
  const openUp = window.innerHeight - rect.bottom < height + 12 && rect.top > height + 12;
  panel.style.top = openUp
    ? `${window.scrollY + rect.top - height - 6}px`
    : `${window.scrollY + rect.bottom + 6}px`;
  const maxLeft = document.documentElement.clientWidth - panel.offsetWidth - 16;
  panel.style.left = `${window.scrollX + Math.max(8, Math.min(rect.left, maxLeft))}px`;
}

function pickDigestTime() {
  const input = document.getElementById('digestTime');
  if (!input) return;
  openTimePicker(input, input.value || '22:00', value => {
    input.value = value;
  });
}

function openTimePicker(anchor, current, onPick) {
  setTimeout(() => buildTimePicker(anchor, current, onPick), 0);
}

function buildTimePicker(anchor, current, onPick) {
  closeDatePicker();
  const initial = /^\d{1,2}:\d{2}$/.test(current || '') ? current : '22:00';
  let [hour, minute] = initial.split(':').map(Number);
  const pad = value => String(value).padStart(2, '0');
  const panel = document.createElement('div');
  panel.className = 'date-pop time-pop';
  panel.innerHTML = `
    <div class="date-pop-head"><span class="date-pop-title">选择日报时间</span></div>
    <div class="time-pop-label">时</div>
    <div class="time-pop-grid" data-kind="hour"></div>
    <div class="time-pop-label">分</div>
    <div class="time-pop-grid" data-kind="minute"></div>`;
  document.body.appendChild(panel);
  datePopEl = panel;
  panel.addEventListener('click', event => event.stopPropagation());
  const hours = panel.querySelector('[data-kind="hour"]');
  const minutes = panel.querySelector('[data-kind="minute"]');
  hours.innerHTML = Array.from({ length: 24 }, (_, index) => index)
    .map(value => `<button type="button" class="date-pop-day${value === hour ? ' sel' : ''}" data-hour="${value}">${pad(value)}</button>`)
    .join('');
  const minuteValues = Array.from({ length: 12 }, (_, index) => index * 5);
  if (minute % 5 !== 0) minuteValues.push(minute);
  minuteValues.sort((a, b) => a - b);
  minutes.innerHTML = minuteValues
    .map(value => `<button type="button" class="date-pop-day${value === minute ? ' sel' : ''}" data-minute="${value}">${pad(value)}</button>`)
    .join('');
  const select = (button) => {
    button.parentElement.querySelectorAll('.sel').forEach(item => item.classList.remove('sel'));
    button.classList.add('sel');
  };
  panel.addEventListener('click', event => {
    const hourPick = event.target.closest('[data-hour]');
    if (hourPick) {
      hour = Number(hourPick.dataset.hour);
      select(hourPick);
      return;
    }
    const minutePick = event.target.closest('[data-minute]');
    if (minutePick) {
      minute = Number(minutePick.dataset.minute);
      closeDatePicker();
      onPick(`${pad(hour)}:${pad(minute)}`);
    }
  });
  placePop(panel, anchor);
}

function dateLabel(key) {
  if (!key) return '';
  const date = new Date(key + 'T00:00:00');
  if (Number.isNaN(date.getTime())) return key;
  const week = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'][date.getDay()];
  return `${date.getFullYear()} 年 ${date.getMonth() + 1} 月 ${date.getDate()} 日 · ${week}`;
}

function effectivePriority(message) {
  return message.urgency === 'urgent' ? 'urgent' : (message.priority || 'normal');
}

function priorityBadge(value) {
  const priority = value || 'normal';
  return `<span class="badge ${esc(priority)}">${esc(PRIORITY_LABEL[priority] || priority)}</span>`;
}

function statusLabel(value) {
  return STATUS_LABEL[value] || value || '—';
}

function typeLabel(value) {
  return TYPE_LABEL[value] || value || '未知';
}

function intervalText(seconds) {
  const value = Number(seconds || 600);
  if (value % 3600 === 0) return `${value / 3600} 小时`;
  if (value >= 60) return `${Math.round(value / 60)} 分钟`;
  return `${value} 秒`;
}

function toast(message, error = false) {
  const element = document.getElementById('toast');
  element.textContent = message;
  element.className = 'toast show' + (error ? ' error' : '');
  clearTimeout(element._timer);
  element._timer = setTimeout(() => { element.className = 'toast'; }, 3400);
}

async function apiResult(path, options = {}) {
  const headers = Object.assign({}, options.headers || {});
  if (options.body !== undefined) headers['Content-Type'] = 'application/json';
  let response;
  try {
    response = await fetch(path, Object.assign({}, options, { headers }));
  } catch (error) {
    return { ok: false, status: 0, data: { message: '无法连接本地服务' } };
  }
  const text = await response.text();
  let data = {};
  if (text) {
    try { data = JSON.parse(text); } catch (error) { data = { message: text }; }
  }
  return { ok: response.ok, status: response.status, data };
}

async function api(path, options = {}) {
  const result = await apiResult(path, options);
  if (!result.ok) {
    const code = result.data.code ? result.data.code + ': ' : '';
    throw new Error(code + (result.data.message || '请求失败'));
  }
  return result.data;
}

function openModal(html) {
  document.getElementById('modal').innerHTML = html;
  document.getElementById('modalMask').classList.add('open');
}

function closeModal() {
  document.getElementById('modalMask').classList.remove('open');
}

function renderTopActions() {
  const target = document.getElementById('topActions');
  if (state.view === 'messages') {
    const options = ['<option value="">全部来源</option>'].concat(
      state.sources.map(source =>
        `<option value="${esc(source.id)}" ${state.sourceId === source.id ? 'selected' : ''}>${esc(source.name)}</option>`
      )
    ).join('');
    target.innerHTML = `
      <div class="search-box"><span class="search-icon">⌕</span>
        <input id="topSearch" placeholder="搜索消息" value="${esc(state.search)}" oninput="onSearch(this.value)">
      </div>
      <select id="topSource" onchange="onSourceFilter(this.value)">${options}</select>
      <button class="btn tiny ${state.date ? '' : 'inactive'}" id="topDateBtn" onclick="pickTopDate()">${esc((state.lastDate || localToday()).replace(/-/g, '/'))}</button>
      <button class="btn tiny" onclick="showAllDates()">全部</button>
      <button class="btn tiny" onclick="openDigest()">日报</button>`;
  } else if (state.view === 'sources') {
    const sourceEditorOpen = document.getElementById('sourceEditor')?.classList.contains('active');
    target.innerHTML = sourceEditorOpen
      ? `<button class="btn" onclick="closeSourceEditor()">返回列表</button>
         ${state.sourceEditingId ? `<button class="btn" onclick="testSource('${state.sourceEditingId}')">测试连接</button>` : ''}
         <button class="btn primary" onclick="document.getElementById('sourceEditor').requestSubmit()">保存来源</button>`
      : `<button class="btn primary" onclick="openSourceEditor()">添加来源</button>`;
  } else if (state.view === 'model') {
    const editorOpen = document.getElementById('modelEditor')?.classList.contains('active');
    target.innerHTML = editorOpen
      ? `<button class="btn" onclick="closeModelEditor()">返回列表</button>
         <button class="btn" onclick="testModel()">测试连接</button>
         <button class="btn primary" onclick="document.getElementById('modelEditor').requestSubmit()">保存配置</button>`
      : `<button class="btn primary" onclick="openModelEditor()">添加供应商</button>`;
  } else {
    target.innerHTML = '';
  }
}

function showView(view) {
  const requested = view === 'settings' ? 'basic' : view;
  const next = VIEWS.includes(requested) ? requested : 'messages';
  state.view = next;
  VIEWS.forEach(name => {
    document.getElementById('view-' + name).classList.toggle('active', name === next);
  });
  const navView = SETTINGS_VIEWS.includes(next) ? 'settings' : next;
  document.querySelectorAll('.nav-item').forEach(nav => {
    nav.classList.toggle('active', nav.dataset.view === navView);
  });
  const settingsTabs = document.getElementById('settingsTabs');
  if (settingsTabs) {
    settingsTabs.style.display = SETTINGS_VIEWS.includes(next) ? 'flex' : 'none';
    settingsTabs.querySelectorAll('[data-settings-tab]').forEach(button => {
      button.classList.toggle('active', button.dataset.settingsTab === next);
    });
  }
  document.getElementById('pageTitle').textContent = VIEW_TITLES[next];
  document.getElementById('pageMeta').textContent = '';
  history.replaceState(null, '', '#' + next);
  renderTopActions();
  if (next === 'messages') loadMessages();
  if (next === 'sources') {
    // Entering the section always starts from the list, never a stale editor.
    if (document.getElementById('sourceEditor')?.classList.contains('active')) {
      closeSourceEditor();
    }
    loadSources();
  }
  if (next === 'basic') loadRange();
  if (next === 'model') {
    if (document.getElementById('modelEditor')?.classList.contains('active')) {
      closeModelEditor();
    }
    loadModel();
  }
  if (next === 'profile') loadProfile();
  saveUiState();
}

function saveUiState() {
  try {
    sessionStorage.setItem('msgdesk_ui', JSON.stringify({ view: state.view }));
  } catch (error) {}
}

async function refreshStatus() {
  const result = await apiResult('/api/status');
  const dot = document.getElementById('serviceDot');
  const text = document.getElementById('serviceText');
  if (!result.ok) {
    dot.className = 'dot bad';
    text.textContent = '服务未连接';
    return;
  }
  state.status = result.data;
  dot.className = 'dot ok';
  text.textContent = '服务运行中';
  const sources = result.data.sources || [];
  const latest = sources
    .map(source => source.last_success_at)
    .filter(Boolean)
    .sort()
    .pop();
  document.getElementById('lastSync').textContent = latest ? fmtTime(latest) : '—';
}

function updateUnreadBadge(messages) {
  const unread = (messages || []).filter(message => !Number(message.is_read || 0)).length;
  const badge = document.getElementById('navUnread');
  if (!badge) return;
  badge.textContent = unread > 99 ? '99+' : String(unread);
  badge.style.display = unread ? '' : 'none';
}

function setFilter(filter) {
  state.filter = filter;
  state.selected.clear();
  document.querySelectorAll('#messageFilters .chip').forEach(chip => {
    chip.classList.toggle('active', chip.dataset.filter === filter);
  });
  loadMessages();
}

let searchTimer = null;
function onSearch(value) {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.search = String(value || '').trim();
    loadMessages();
  }, 260);
}

function onSourceFilter(value) {
  state.sourceId = value;
  loadMessages();
}

function onDateFilter(value) {
  state.date = value || localToday();
  state.lastDate = state.date;
  renderTopActions();
  loadMessages();
}

function showAllDates() {
  state.date = '';
  renderTopActions();
  loadMessages();
}

function pickTopDate() {
  openDatePicker(
    document.getElementById('topDateBtn'),
    state.date || localToday(),
    value => onDateFilter(value)
  );
}

function clearFilters() {
  state.filter = 'all';
  state.search = '';
  state.sourceId = '';
  state.date = '';
  state.selected.clear();
  document.querySelectorAll('#messageFilters .chip').forEach(chip => {
    chip.classList.toggle('active', chip.dataset.filter === 'all');
  });
  renderTopActions();
  loadMessages();
}

async function loadMessages(options = {}) {
  const silent = Boolean(options.silent);
  if (!silent) state.selected.clear();
  const query = new URLSearchParams({ limit: '1000' });
  if (state.filter === 'urgent') query.set('priority', 'urgent');
  if (state.filter === 'high') query.set('priority', 'high');
  if (state.filter === 'unread') query.set('unread', '1');
  if (state.filter === 'todo') query.set('todo', '1');
  if (state.filter === 'ignored') query.set('ignored', '1');
  if (state.search) query.set('q', state.search);
  if (state.sourceId) query.set('source_id', state.sourceId);
  if (state.date) query.set('date', state.date);
  const result = await apiResult('/api/messages?' + query.toString());
  if (!result.ok) {
    document.getElementById('messageList').innerHTML =
      `<div class="empty-state"><div class="empty-glyph">!</div><h3>消息读取失败</h3><p>${esc(result.data.message || '请检查服务状态')}</p></div>`;
    return;
  }
  const next = result.data.messages || [];
  const signature = next.map(item => [
    item.id, item.is_read, item.processing_status, item.category || '',
    item.updated_at || ''
  ].join(':')).join('|');
  if (silent && signature === state.messageSignature) return;
  state.messageSignature = signature;
  state.messages = next;
  updateUnreadBadge(next);
  const container = document.getElementById('messageList');
  const keepScroll = silent ? container.scrollTop : 0;
  renderMessages();
  if (silent) container.scrollTop = keepScroll;
}

function renderMessages() {
  const container = document.getElementById('messageList');
  const meta = document.getElementById('pageMeta');
  if (!state.messages.length) {
    meta.textContent = '0 条';
    const noSources = state.sources.length === 0;
    container.innerHTML = `
      <div class="empty-state">
        <div class="empty-glyph">▣</div>
        <h3>${noSources ? '还没有收集到消息' : '没有匹配的消息'}</h3>
        <p>${noSources ? '请先在“来源”中配置信息来源' : '换个筛选条件或清除搜索'}</p>
        <div class="row">
          ${noSources
            ? '<button class="btn primary" onclick="startAddSource()">添加来源</button>'
            : '<button class="btn" onclick="clearFilters()">清除筛选</button>'}
        </div>
      </div>`;
    updateBatch();
    return;
  }

  meta.textContent = `${state.messages.length} 条`;
  const groups = new Map();
  state.messages.forEach(message => {
    const key = dateKey(message.published_at);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(message);
  });

  let html = `<div class="msg-table">
    <div class="msg-head"><span></span><span>时间 / 来源</span><span>摘要</span><span>分类</span><span style="text-align:right">操作</span></div>`;
  groups.forEach((messages, key) => {
    html += `<div class="date-band">${esc(dateLabel(key))}</div>`;
    messages.forEach(message => {
      const unread = Number(message.is_read || 0) === 0;
      const selected = state.selected.has(message.id);
      const summary = message.summary_short ||
        String(message.content_text || '').replace(/\s+/g, ' ').slice(0, 130) ||
        '（无正文）';
      const status = message.processing_status || 'pending';
      const conversation = message.conversation_name &&
        message.conversation_name !== message.source_name
        ? message.conversation_name
        : '';
      const metaParts = [
        conversation,
        statusLabel(status),
        message.ignored_at ? '已忽略' : ''
      ].filter(Boolean);
      html += `<div class="msg-row ${unread ? 'unread' : ''} ${message.ignored_at ? 'ignored' : ''}" onclick="openMessage('${message.id}')">
        <label class="check" onclick="event.stopPropagation()">
          <input type="checkbox" ${selected ? 'checked' : ''} onchange="toggleSelect('${message.id}', this.checked)">
        </label>
        <div class="msg-time">${esc(fmtTime(message.published_at))}
          <div class="src">${esc(message.source_name || '未知来源')}</div>
        </div>
        <div class="msg-summary">
          <button class="text" onclick="event.stopPropagation();openMessage('${message.id}')">${esc(summary)}</button>
          <div class="meta">${esc(metaParts.join(' · '))}</div>
        </div>
        <div><span class="badge">${esc(message.category || '未分类')}</span></div>
        <div class="msg-actions">
          <button class="link" onclick="event.stopPropagation();openMessage('${message.id}')">查看</button>
          <button class="link danger-text" onclick="event.stopPropagation();askDeleteMessage('${message.id}')">删除</button>
          <details class="menu" onclick="event.stopPropagation()">
            <summary>⋯</summary>
            <div class="menu-body">
              <button onclick="toggleRead('${message.id}', ${message.is_read ? 'false' : 'true'})">${message.is_read ? '标记未读' : '标记已读'}</button>
              <button onclick="toggleIgnore('${message.id}', ${message.ignored_at ? 'false' : 'true'})">${message.ignored_at ? '取消忽略' : '忽略'}</button>
            </div>
          </details>
        </div>
      </div>`;
    });
  });
  html += '</div>';
  container.innerHTML = html;
  updateBatch();
}

function toggleSelect(id, checked) {
  if (checked) state.selected.add(id);
  else state.selected.delete(id);
  updateBatch();
}

function toggleSelectAll(checked) {
  state.selected.clear();
  if (checked) state.messages.forEach(message => state.selected.add(message.id));
  renderMessages();
}

function askIgnoreAll() {
  const ids = state.messages.filter(item => !item.ignored_at).map(item => item.id);
  if (!ids.length) {
    notify('没有可忽略的消息', '当前列表里的消息都已忽略', 'error');
    return;
  }
  openModal(`
    <div class="modal-head">
      <div><div class="eyebrow">IGNORE ALL</div><h2>忽略当前列表</h2></div>
      <button class="icon-btn" onclick="closeModal()" aria-label="关闭">×</button>
    </div>
    <p>将把当前筛选结果里的 ${ids.length} 条消息标记为忽略，它们不会再出现在日报里。消息本身仍保留，可随时取消忽略。</p>
    <div class="row" style="margin-top:16px">
      <button class="btn danger" onclick='ignoreAll(${JSON.stringify(ids)})'>确定忽略</button>
      <button class="btn" onclick="closeModal()">取消</button>
    </div>`);
}

async function ignoreAll(ids) {
  try {
    const result = await api('/api/messages/batch-ignore', {
      method: 'POST',
      body: JSON.stringify({ ids, ignored: true })
    });
    closeModal();
    notify('已忽略', `${result.updated || ids.length} 条消息不会再进日报`, 'ok');
    await loadMessages();
    refreshStatus();
  } catch (error) {
    notify('操作失败', error.message, 'error');
  }
}

async function toggleIgnore(id, ignored) {
  try {
    await api('/api/messages/' + id, {
      method: 'PATCH',
      body: JSON.stringify({ ignored })
    });
    notify(
      ignored ? '已忽略这条消息' : '已取消忽略',
      ignored ? '它不会再出现在日报里' : '它会重新计入日报',
      'ok'
    );
    closeModal();
    await loadMessages();
  } catch (error) {
    notify('操作失败', error.message, 'error');
  }
}

async function markAllRead() {
  try {
    const result = await api('/api/messages/batch-read', { method: 'POST', body: '{}' });
    notify(
      '已全部标记为已读',
      result.updated ? `${result.updated} 条消息` : '没有未读消息',
      'ok'
    );
    await loadMessages();
    refreshStatus();
  } catch (error) {
    notify('操作失败', error.message, 'error');
  }
}

function updateBatch() {
  const button = document.getElementById('batchDeleteBtn');
  const selectAll = document.getElementById('selectAll');
  button.disabled = state.selected.size === 0;
  button.textContent = state.selected.size ? `批量删除 (${state.selected.size})` : '批量删除';
  selectAll.checked = state.messages.length > 0 && state.selected.size === state.messages.length;
  selectAll.indeterminate = state.selected.size > 0 && state.selected.size < state.messages.length;
}

async function openMessage(id) {
  try {
    const message = await api('/api/messages/' + id);
    if (!Number(message.is_read || 0)) {
      try {
        await api('/api/messages/' + id, {
          method: 'PATCH',
          body: JSON.stringify({ is_read: true })
        });
        message.is_read = 1;
        const local = state.messages.find(item => item.id === id);
        if (local) local.is_read = 1;
        refreshStatus();
      } catch (error) {}
    }
    const processing = message.processing || {};
    const sourceLink = safeUrl(message.source_url);
    openModal(`
      <div class="modal-head">
        <div>
          <div class="eyebrow">MESSAGE</div>
          <h2>${esc(processing.summary_short || '消息详情')}</h2>
        </div>
        <button class="icon-btn" onclick="closeModal()" aria-label="关闭">×</button>
      </div>
      <div class="meta-grid">
        <div><div class="label">来源</div><div class="value">${esc(message.source_name || '—')}</div></div>
        <div><div class="label">会话</div><div class="value">${esc(message.conversation_name || '—')}</div></div>
        <div><div class="label">原文时间</div><div class="value">${esc(fmtFullTime(message.published_at))}</div></div>
      </div>
      <div class="row" style="margin:12px 0">
        <button class="btn tiny" onclick="toggleRead('${message.id}', ${message.is_read ? 'false' : 'true'})">${message.is_read ? '标记未读' : '标记已读'}</button>
        <button class="btn tiny danger" onclick="askDeleteMessage('${message.id}')">删除</button>
        ${sourceLink ? `<a class="btn tiny" href="${esc(sourceLink)}" target="_blank" rel="noreferrer">打开原文</a>` : ''}
      </div>
      ${processing.tags && processing.tags.length
        ? `<div class="row" style="margin-bottom:10px">${processing.tags.map(tag => `<span class="badge">${esc(tag)}</span>`).join('')}</div>`
        : ''}
      ${processing.action_items && processing.action_items.length
        ? `<h3 class="section-title">行动项</h3><ul class="md-list">${processing.action_items.map(item => `<li>${esc(item)}</li>`).join('')}</ul>`
        : ''}
      ${processing.summary_long
        ? `<h3 class="section-title">详细摘要</h3><p>${esc(processing.summary_long)}</p>`
        : ''}
      <h3 class="section-title">原始内容</h3>
      ${['markdown', 'mixed'].includes(message.content_type)
        ? `<div class="md">${renderMarkdown(message.content_text || '')}</div>`
        : `<pre class="raw-text">${esc(message.content_text || '')}</pre>`}
    `);
    renderMessages();
  } catch (error) {
    toast(error.message, true);
  }
}

async function toggleRead(id, read) {
  try {
    await api('/api/messages/' + id, {
      method: 'PATCH',
      body: JSON.stringify({ is_read: Boolean(read) })
    });
    closeModal();
    const local = state.messages.find(item => item.id === id);
    if (local) local.is_read = read ? 1 : 0;
    renderMessages();
    refreshStatus();
    toast(read ? '已标记为已读' : '已标记为未读');
  } catch (error) {
    toast(error.message, true);
  }
}

async function reprocessMessage(id) {
  try {
    await api(`/api/messages/${id}/process`, {
      method: 'POST',
      body: JSON.stringify({ force: true })
    });
    closeModal();
    toast('已重新处理');
    loadMessages();
    refreshStatus();
  } catch (error) {
    toast(error.message, true);
  }
}

async function ignoreMessage(id) {
  try {
    await api('/api/messages/' + id, {
      method: 'PATCH',
      body: JSON.stringify({ processing_status: 'ignored' })
    });
    closeModal();
    toast('已忽略');
    loadMessages();
  } catch (error) {
    toast(error.message, true);
  }
}

function askDeleteMessage(id) {
  openModal(`
    <div class="modal-head">
      <div><div class="eyebrow">DELETE</div><h2>删除这条消息？</h2></div>
      <button class="icon-btn" onclick="closeModal()" aria-label="关闭">×</button>
    </div>
    <p>“删除消息”会同时移除摘要、处理结果和关联提醒记录；“仅隐藏”保留数据，只从默认列表移除。</p>
    <div class="row" style="margin-top:16px">
      <button class="btn danger" onclick="confirmDeleteMessage('${id}', true)">删除消息</button>
      <button class="btn" onclick="confirmDeleteMessage('${id}', false)">仅隐藏</button>
      <button class="btn" onclick="closeModal()">取消</button>
    </div>
  `);
}

async function confirmDeleteMessage(id, purge) {
  try {
    await api(`/api/messages/${id}?purge=${purge ? 'true' : 'false'}`, { method: 'DELETE' });
    state.selected.delete(id);
    closeModal();
    toast(purge ? '消息已删除' : '消息已隐藏');
    loadMessages();
    refreshStatus();
  } catch (error) {
    toast(error.message, true);
  }
}

function askBatchDelete() {
  const count = state.selected.size;
  if (!count) return;
  openModal(`
    <div class="modal-head">
      <div><div class="eyebrow">BATCH DELETE</div><h2>处理选中的 ${count} 条消息</h2></div>
      <button class="icon-btn" onclick="closeModal()" aria-label="关闭">×</button>
    </div>
    <p>删除会清理消息、摘要和提醒记录；隐藏会保留原始数据。</p>
    <div class="row" style="margin-top:16px">
      <button class="btn danger" onclick="confirmBatchDelete(true)">删除消息</button>
      <button class="btn" onclick="confirmBatchDelete(false)">仅隐藏</button>
      <button class="btn" onclick="closeModal()">取消</button>
    </div>
  `);
}

async function confirmBatchDelete(purge) {
  try {
    const result = await api('/api/messages/batch-delete', {
      method: 'POST',
      body: JSON.stringify({ ids: Array.from(state.selected), purge: Boolean(purge) })
    });
    state.selected.clear();
    closeModal();
    toast(`已处理 ${result.deleted || 0} 条消息`);
    loadMessages();
    refreshStatus();
  } catch (error) {
    toast(error.message, true);
  }
}

/* Digest -------------------------------------------------------------- */
function openDigest(date) {
  // Follow the date selected in the toolbar so the drawer matches the list.
  state.digestDate = date || state.date || localToday();
  updateDigestDateLabel();
  document.getElementById('digestDrawer').classList.add('open');
  document.getElementById('drawerMask').classList.add('open');
  document.getElementById('digestDrawer').setAttribute('aria-hidden', 'false');
  loadDigest();
}

function closeDigest() {
  document.getElementById('digestDrawer').classList.remove('open');
  document.getElementById('drawerMask').classList.remove('open');
  document.getElementById('digestDrawer').setAttribute('aria-hidden', 'true');
}

function updateDigestDateLabel() {
  const label = document.getElementById('digestDate');
  if (label) {
    label.textContent = (state.digestDate || localToday()).replace(/-/g, '/');
  }
  updateDigestNav();
}

function digestBounds() {
  const range = state.status?.config?.range || {};
  return {
    start: range.start || '',
    end: range.effective_end || localToday()
  };
}

function updateDigestNav() {
  const current = state.digestDate || localToday();
  const { start, end } = digestBounds();
  const previous = document.getElementById('digestPrevDay');
  const next = document.getElementById('digestNextDay');
  if (previous) previous.disabled = Boolean(start && current <= start);
  if (next) next.disabled = Boolean(end && current >= end);
}

function shiftDigestDay(delta) {
  const current = state.digestDate || localToday();
  const date = new Date(current + 'T00:00:00');
  date.setDate(date.getDate() + delta);
  const target = dateKey(date);
  const { start, end } = digestBounds();
  if (start && target < start) {
    toast('已经到起始日期');
    return;
  }
  if (end && target > end) {
    toast('已经到结束日期');
    return;
  }
  state.digestDate = target;
  loadDigest();
}

function pickDigestDate() {
  openDatePicker(
    document.getElementById('digestDate'),
    state.digestDate || localToday(),
    value => {
      state.digestDate = value || localToday();
      loadDigest();
    }
  );
}

async function loadDigest() {
  const date = state.digestDate || localToday();
  state.digestDate = date;
  state.digestSource = false;
  updateDigestDateLabel();
  document.getElementById('digestTitle').textContent = `${date} 日报`;
  const body = document.getElementById('digestBody');
  body.innerHTML = '<p class="muted">读取中…</p>';
  const result = await apiResult('/api/digests/' + date);
  if (!result.ok) {
    state.digestContent = null;
    state.digestRecord = null;
    body.innerHTML = `
      <div class="empty-state">
        <div class="empty-glyph">▤</div>
        <h3>该日期没有已处理消息</h3>
        <p>可以切换到其他日期，或重新生成这一天的日报。</p>
        <div class="row"><button class="btn primary" onclick="regenerateDigest()">重新生成</button></div>
      </div>`;
    return;
  }
  state.digestRecord = result.data.digest || null;
  state.digestContent = result.data.content || null;
  renderDigest();
}

function renderDigest() {
  const body = document.getElementById('digestBody');
  const record = state.digestRecord || {};
  if (!state.digestContent) {
    body.innerHTML = `
      <div class="empty-state">
        <div class="empty-glyph">▤</div>
        <h3>该日期没有已处理消息</h3>
        <p>生成后会在这里以 Markdown 渲染结果展示。</p>
        <div class="row"><button class="btn primary" onclick="regenerateDigest()">重新生成</button></div>
      </div>`;
    return;
  }
  body.innerHTML = (state.digestSource
      ? `<pre class="raw-text">${esc(state.digestContent)}</pre>`
      : `<div class="md">${renderMarkdown(state.digestContent)}</div>`);
}

async function regenerateDigest() {
  const date = state.digestDate || localToday();
  try {
    const result = await api(`/api/digests/${date}/regenerate`, {
      method: 'POST',
      body: '{}'
    });
    toast(`日报已生成：${result.message_count || 0} 条消息`);
    await loadDigest();
    refreshStatus();
  } catch (error) {
    toast(error.message, true);
  }
}

function openDigestFile() {
  if (!state.digestContent) {
    toast('日报文件尚未生成', true);
    return;
  }
  const blob = new Blob([state.digestContent], { type: 'text/markdown;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  window.open(url, '_blank', 'noopener');
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}

function toggleDigestSource() {
  state.digestSource = !state.digestSource;
  renderDigest();
}

function splitTableRow(line) {
  return line.trim().replace(/^\|/, '').replace(/\|$/, '')
    .split('|').map(cell => cell.trim());
}

function renderMarkdown(markdown) {
  const lines = String(markdown || '').replace(/\r\n?/g, '\n').split('\n');
  const html = [];
  let index = 0;

  const inline = (text) => {
    let value = esc(text);
    const links = [];
    value = value.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g,
      (match, alt, src) => `<img alt="${alt}" src="${src}" style="max-width:100%;height:auto;border:2px solid var(--line);margin:6px 0">`);
    value = value.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+|\/[^\s)]+)\)/g, (match, label, url) => {
      links.push(`<a href="${url}" target="_blank" rel="noreferrer">${label}</a>`);
      return `\u0000${links.length - 1}\u0000`;
    });
    value = value.replace(/`([^`]+)`/g, '<code>$1</code>');
    value = value.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    value = value.replace(/(^|[^*])\*([^*]+)\*/g, '$1<em>$2</em>');
    value = value.replace(/~~([^~]+)~~/g, '<del>$1</del>');
    value = value.replace(/(https?:\/\/[^\s<)]+)/g,
      '<a href="$1" target="_blank" rel="noreferrer">$1</a>');
    value = value.replace(/\u0000(\d+)\u0000/g, (match, number) => links[Number(number)]);
    return value;
  };

  const parseList = (start) => {
    const items = [];
    let cursor = start;
    const first = lines[cursor].match(/^(\s*)([-*+]|\d+\.)\s+(.*)$/);
    const baseIndent = first ? first[1].length : 0;
    while (cursor < lines.length) {
      const match = lines[cursor].match(/^(\s*)([-*+]|\d+\.)\s+(.*)$/);
      if (!match) break;
      const indent = match[1].length;
      if (indent < baseIndent) break;
      const level = Math.floor((indent - baseIndent) / 2);
      let text = match[3];
      let task = '';
      const check = text.match(/^\[([ xX])\]\s+(.*)$/);
      if (check) {
        task = check[1].toLowerCase() === 'x'
          ? '<span class="task done">[x]</span> '
          : '<span class="task">[ ]</span> ';
        text = check[2];
      }
      const item = { text, task, children: [] };
      if (level > 0 && items.length) items[items.length - 1].children.push(item);
      else items.push(item);
      cursor += 1;
    }
    const renderList = (list) => `<ul class="md-list">${list.map(item =>
      `<li>${item.task}${inline(item.text)}${item.children.length ? renderList(item.children) : ''}</li>`
    ).join('')}</ul>`;
    return { html: renderList(items), next: cursor };
  };

  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) { index += 1; continue; }

    if (/^```/.test(line)) {
      const code = [];
      index += 1;
      while (index < lines.length && !/^```/.test(lines[index])) {
        code.push(lines[index]);
        index += 1;
      }
      index += 1;
      html.push(`<pre class="md-code"><code>${esc(code.join('\n'))}</code></pre>`);
      continue;
    }

    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      const level = heading[1].length;
      html.push(`<h${level}>${inline(heading[2])}</h${level}>`);
      index += 1;
      continue;
    }

    if (/^\s*([-*_])\1\1+\s*$/.test(line)) {
      html.push('<hr>');
      index += 1;
      continue;
    }

    if (/^\s*>\s?/.test(line)) {
      const quote = [];
      while (index < lines.length && /^\s*>\s?/.test(lines[index])) {
        quote.push(lines[index].replace(/^\s*>\s?/, ''));
        index += 1;
      }
      html.push(`<blockquote>${inline(quote.join(' '))}</blockquote>`);
      continue;
    }

    if (/^\s*\|.*\|\s*$/.test(line) && index + 1 < lines.length &&
        /^\s*\|?[\s:|-]+\|?\s*$/.test(lines[index + 1])) {
      const header = splitTableRow(line);
      index += 2;
      const rows = [];
      while (index < lines.length && lines[index].trim() && lines[index].includes('|')) {
        rows.push(splitTableRow(lines[index]));
        index += 1;
      }
      html.push(`<table class="md-table"><thead><tr>${
        header.map(cell => `<th>${inline(cell)}</th>`).join('')
      }</tr></thead><tbody>${
        rows.map(row => `<tr>${row.map(cell => `<td>${inline(cell)}</td>`).join('')}</tr>`).join('')
      }</tbody></table>`);
      continue;
    }

    if (/^\s*([-*+]|\d+\.)\s+/.test(line)) {
      const parsed = parseList(index);
      html.push(parsed.html);
      index = parsed.next;
      continue;
    }

    const paragraph = [];
    while (index < lines.length && lines[index].trim() &&
           !/^(#{1,6})\s+/.test(lines[index]) &&
           !/^\s*([-*+]|\d+\.)\s+/.test(lines[index]) &&
           !/^\s*>\s?/.test(lines[index]) &&
           !/^```/.test(lines[index]) &&
           !/^\s*\|.*\|\s*$/.test(lines[index])) {
      paragraph.push(lines[index]);
      index += 1;
    }
    if (!paragraph.length) {
      paragraph.push(lines[index]);
      index += 1;
    }
    html.push(`<p>${inline(paragraph.join(' '))}</p>`);
  }
  return html.join('\n');
}

/* Sources ------------------------------------------------------------- */
async function loadSources() {
  const sourcesResult = await apiResult('/api/sources');
  if (!sourcesResult.ok) {
    document.getElementById('sourceList').innerHTML =
      `<div class="empty-state"><h3>来源读取失败</h3><p>${esc(sourcesResult.data.message || '')}</p></div>`;
    return;
  }
  state.sources = sourcesResult.data.sources || [];
  if (state.sources.some(item => item.adapter_type === 'qq_onebot')) {
    const qq = await apiResult('/api/qq/state');
    state.qqLoggedIn = Boolean(qq.ok && qq.data.logged_in);
  }
  renderTopActions();
  renderSources();
}

function setSourceCategory(name) {
  state.sourceCategory = name || '';
  renderSources();
}

function sourceStatus(source) {
  if (source.adapter_type === 'qq_onebot') {
    if (!state.qqLoggedIn) return '<span class="badge bad">未登录</span>';
  }
  if (source.active_job) return '<span class="badge normal">同步中</span>';
  if (source.config && source.config.cookie_invalid) {
    return '<span class="badge bad">登录失效</span>';
  }
  if (source.last_error_code) return `<span class="badge bad">${esc(source.last_error_code)}</span>`;
  if (!source.enabled) return '<span class="badge low">已暂停</span>';
  return '<span class="badge ok">正常</span>';
}

function sourceDetail(source) {
  const config = source.config || {};
  if (source.adapter_type === 'qq_onebot') return '本机 QQ 服务（NapCat）· 群聊只读';
  if (config.url) return config.url;
  return '只读适配器';
}

function renderSources() {
  const container = document.getElementById('sourceList');
  document.getElementById('pageMeta').textContent = `${state.sources.length} 个来源`;
  const categories = Array.from(new Set(
    state.sources.map(source => typeLabel(source.type || source.adapter_type))
  ));
  const filters = document.getElementById('sourceFilters');
  if (filters) {
    filters.innerHTML = [`<button class="chip ${state.sourceCategory ? '' : 'active'}" onclick="setSourceCategory('')">全部</button>`]
      .concat(categories.map(name =>
        `<button class="chip ${state.sourceCategory === name ? 'active' : ''}" onclick="setSourceCategory('${esc(name)}')">${esc(name)}</button>`
      ))
      .join('');
  }
  const visible = state.sourceCategory
    ? state.sources.filter(source =>
        typeLabel(source.type || source.adapter_type) === state.sourceCategory)
    : state.sources;
  if (!visible.length) {
    container.innerHTML = `
      <div class="empty-state model-empty">
        <div class="empty-glyph">□</div>
        <h3>${state.sources.length ? '这个分类下没有来源' : '还没有信息来源'}</h3>
        <p>${state.sources.length ? '换个分类，或点右上角添加来源。' : '点击右上角「添加来源」，配置网站、QQ 或资讯。'}</p>
      </div>`;
    return;
  }
  container.innerHTML = visible.map(source => `
    <article class="model-provider-card">
      <div>
        <h3>${esc(source.name)}</h3>
        <div class="model-card-badges">
          ${sourceStatus(source)}
          <span class="badge normal">${esc(typeLabel(source.type || source.adapter_type))}</span>
          <span class="badge low">每 ${esc(intervalText(source.interval_seconds))}</span>
          ${source.adapter_type === 'qq_onebot' && state.qqLoggedIn
            ? `<span class="badge normal">${(source.config && source.config.selected_conversations || []).length} 个群</span>`
            : ''}
          ${source.config && !source.config.cookie_invalid && source.config.cookie_expires
            ? `<span class="badge ${new Date(source.config.cookie_expires).getTime() < Date.now() ? 'bad' : 'low'}">
                 登录至 ${esc(fmtTime(source.config.cookie_expires))}</span>`
            : source.config && !source.config.cookie_invalid && source.config.cookie
              ? '<span class="badge low">有效期未知</span>'
            : ''}
        </div>
        <div class="model-meta">${esc(sourceDetail(source))}</div>
        <div class="model-meta">最近成功：${esc(fmtTime(source.last_success_at))}</div>
      </div>
      <div class="model-card-actions">
        <button class="btn tiny" type="button" onclick="openSourceEditor('${source.id}')">编辑</button>
        <button class="btn tiny danger" type="button" onclick="askDeleteSource('${source.id}')">删除</button>
      </div>
    </article>`).join('');
}

async function testSource(id) {
  const source = state.sources.find(item => item.id === id);
  const label = source ? source.name : '来源';
  const notice = notify('正在测试来源连接…', label, 'pending', 0);
  try {
    const result = await api(`/api/sources/${id}/test`, { method: 'POST', body: '{}' });
    const validation = result.validation || {};
    const status = result.status || {};
    if (validation.ok) {
      const detail = [label, status.status || '已连接', status.message || validation.message || '']
        .filter(Boolean).join(' · ');
      notice?.set('来源连接正常', detail, 'ok');
    } else {
      notice?.set('来源配置无效', validation.message || status.message || '配置无效', 'error');
    }
  } catch (error) {
    notice?.set('来源连接失败', error.message, 'error');
  }
}

function askDeleteSource(id) {
  const source = state.sources.find(item => item.id === id);
  openModal(`
    <div class="modal-head">
      <div><div class="eyebrow">SOURCE DELETE</div><h2>处理来源“${esc(source ? source.name : '')}”</h2></div>
      <button class="icon-btn" onclick="closeModal()" aria-label="关闭">×</button>
    </div>
    <p>“停用来源”会停止采集但保留历史消息；“删除并清理”会同时移除该来源的消息、处理结果和提醒记录。</p>
    <div class="row" style="margin-top:16px">
      <button class="btn" onclick="confirmDeleteSource('${id}', false)">停用来源</button>
      <button class="btn danger" onclick="confirmDeleteSource('${id}', true)">删除并清理</button>
      <button class="btn" onclick="closeModal()">取消</button>
    </div>
  `);
}

async function confirmDeleteSource(id, purge) {
  try {
    await api(`/api/sources/${id}?purge=${purge ? 'true' : 'false'}`, { method: 'DELETE' });
    closeModal();
    toast(purge ? '来源及消息已清理' : '来源已停用');
    loadSources();
    refreshStatus();
  } catch (error) {
    toast(error.message, true);
  }
}

function inputField(label, id, value, placeholder, type) {
  return `<label class="field"><span class="field-title">${label}</span>
    <input id="${id}" type="${type || 'text'}" value="${esc(value ?? '')}" placeholder="${esc(placeholder || '')}">
  </label>`;
}

function selectField(label, id, value, options) {
  return `<label class="field"><span class="field-title">${label}</span><select id="${id}">${options.map(option =>
    `<option value="${esc(option.value)}" ${option.value === value ? 'selected' : ''}>${esc(option.label)}</option>`
  ).join('')}</select></label>`;
}

function openSourceEditor(id = null) {
  state.sourceEditingId = id === null ? null : String(id);
  saveUiState();
  const source = state.sources.find(item => item.id === state.sourceEditingId);
  const config = source && source.config ? source.config : {};
  state.sourceFormConfig = config;
  const type = source ? source.adapter_type : 'ai_web';
  const displayType = type === 'web' ? 'ai_web' : type;
  document.getElementById('sourceEditorTitle').textContent = source ? '配置来源' : '添加来源';
  const typeSelect = document.getElementById('srcType');
  typeSelect.value = displayType;
  typeSelect.disabled = false;
  document.getElementById('srcName').value = source ? source.name : '';
  document.getElementById('srcInterval').value =
    Math.max(1, Math.round(Number(source ? source.interval_seconds : 600) / 60));
  document.getElementById('srcCategory').value = source ? source.default_category || '' : '';
  document.getElementById('srcEnabled').checked = !source || source.enabled;
  renderSourceConfigFields(displayType, config);
  document.getElementById('sourceListPage').classList.add('hidden');
  document.getElementById('sourceEditor').classList.add('active');
  renderTopActions();
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function startAddSource() {
  showView('sources');
  openSourceEditor();
}

function closeSourceEditor() {
  state.sourceEditingId = null;
  saveUiState();
  document.getElementById('sourceEditor').classList.remove('active');
  document.getElementById('sourceListPage').classList.remove('hidden');
  renderSources();
  renderTopActions();
}

function renderSourceConfigFields(type, config) {
  const element = document.getElementById('sourceConfigFields');
  if (!element) return;
  const values = config || {};
  let html = '<div class="model-provider-grid source-config-grid" style="margin-top:10px">';
  if (['web', 'ai_web'].includes(type)) {
    html += inputField('地址', 'cfgUrl', values.url || '', 'https://example.com/feed.xml');
    if (type === 'ai_web') {
      html += `<label class="field"><span class="field-title">采集提示词</span>
        <textarea id="cfgPrompt" spellcheck="false" placeholder="例如：只看教务处和学院通知；摘要两句以内；图片保留原图。">${esc(values.prompt || '')}</textarea>
        <span class="muted tiny">写给智能体看：抓什么、怎么筛、摘要怎么写、图片和 md 怎么处理。</span>
      </label>`;
      html += `<div class="field"><span class="field-title">提示词附件（md / 图片）</span>
        <span class="row" style="align-items:center;gap:8px">
          <label class="btn tiny" style="display:inline-flex;align-items:center;gap:6px">选择文件
            <input type="file" id="promptFileInput" accept=".md,.markdown,.txt,image/*" multiple
                   style="display:none" onchange="uploadPromptFiles(this)">
          </label>
          <span class="muted tiny" id="promptFileHint">正在检测模型是否支持图片…</span>
        </span>
        <div id="promptFileList" class="model-tags"></div>
      </div>`;
      html += `<div class="field"><span class="field-title">登录 Cookie</span>
        <textarea id="cfgCookie" spellcheck="false" placeholder="需要登录的页面才填：浏览器登录后，从开发者工具复制 Cookie 整行">${esc(values.cookie && values.cookie !== '***' ? values.cookie : '')}</textarea>
        <span class="row" style="align-items:center;gap:8px;flex-wrap:wrap">
          <button type="button" class="btn tiny" onclick="startBrowserLogin()">浏览器登录</button>
          <span class="muted tiny" id="loginStatus">${cookieHint(values)}</span>
        </span>
      </div>`;
    }
  } else if (type === 'qq_onebot') {
    if (state.sourceEditingId === null) {
      const intervalEl = document.getElementById('srcInterval');
      if (intervalEl && Number(intervalEl.value || 0) <= 60) intervalEl.value = 1440;
    }
    html += `<div class="field qq-picker-field">
      <div class="qq-picker-layout">
        <div class="qq-picker-left">
          <div class="qq-login">
            <span class="field-title">QQ 登录</span>
            <div class="row" style="align-items:center;gap:8px;flex-wrap:wrap">
              <span id="qqLoginStatus" class="muted tiny">正在检查 QQ 服务…</span>
              <button type="button" class="btn tiny" onclick="refreshQqQr()">刷新二维码</button>
              <button type="button" class="btn tiny" onclick="reloginQq()">重新登录</button>
            </div>
            <img id="qqQr" alt="QQ 登录二维码"
                 style="display:none;width:180px;height:180px;border:2px solid var(--line);margin-top:8px">
          </div>
          <div class="qq-group-pick">
            <span class="field-title">要采集的群 <span class="muted tiny" id="qqGroupHint">正在读取…</span></span>
            <div class="qq-tools">
              <button type="button" class="btn tiny" onclick="loadQqGroups()">刷新群列表</button>
              <div class="qq-combo">
                <input id="qqGroupSearch" placeholder="搜索群名或群号，点开选择" autocomplete="off"
                       onfocus="openQqDropdown()" oninput="renderQqGroups()">
                <div id="qqGroupList" class="qq-dropdown"></div>
              </div>
            </div>
            <input type="hidden" id="cfgConversations" value="${esc((values.selected_conversations || []).join(','))}">
            <span class="muted tiny">勾选你关心的群。群聊按天汇总：每个群每天只调用一次模型。</span>
          </div>
        </div>
        <aside class="qq-picker-right">
          <div class="qq-selected-head">
            <span class="field-title">已选群</span>
            <span class="muted tiny" id="qqSelectedCount"></span>
          </div>
          <div id="qqSelectedList" class="qq-selected-list"></div>
        </aside>
      </div>
    </div>`;
    html += `<div class="field">
      <span class="field-title">群聊摘要提示词</span>
      <textarea id="cfgPrompt" spellcheck="false" placeholder="例如：只保留通知、考试、报名、奖学金相关信息；摘要写成要点，标出截止时间。">${esc(values.prompt || '')}</textarea>
      <span class="muted tiny">写给模型看：每个群每天汇总时按这个要求整理。</span>
    </div>
    <p class="muted tiny" style="grid-column:1/-1;margin:2px 0 0">
      采集和日报的日期范围在「设置 → 基础配置」中统一配置；本机 QQ 服务会自动连接。
    </p>`;
  }
  html += '</div>';
  element.innerHTML = html;
  if (type === 'qq_onebot') {
    state.qqGroups = [];
    state.qqSelected = new Set(
      (Array.isArray(values.selected_conversations) ? values.selected_conversations : [])
        .map(String)
    );
    renderQqGroups();
    ensureQqReady();
  }
  if (type === 'ai_web') {
    state.promptFiles = Array.isArray(values.prompt_files) ? values.prompt_files.slice() : [];
    renderPromptFiles();
    refreshVisionSupport();
  }
}

async function refreshVisionSupport() {
  const result = await apiResult('/api/models/vision');
  const supported = Boolean(result.ok && result.data.supported);
  state.visionSupported = supported;
  const input = document.getElementById('promptFileInput');
  if (input) input.accept = supported ? '.md,.markdown,.txt,image/*' : '.md,.markdown,.txt';
  const hint = document.getElementById('promptFileHint');
  if (hint) {
    hint.textContent = supported
      ? 'md 会拼进提示词；图片作为参考图一起发给模型。'
      : `当前模型${result.data.model ? '（' + result.data.model + '）' : ''}不支持图片输入，只能上传 md / txt。`;
  }
}

let qqLoginTimer = null;
let qqQrStamp = null;
let qqWiring = false;

async function reloginQq() {
  const status = document.getElementById('qqLoginStatus');
  if (status) status.textContent = '正在重启 QQ 服务，稍等…';
  qqQrStamp = null;
  state.qqGroups = [];
  state.qqSelected.clear();
  const box = document.getElementById('qqGroupList');
  if (box) {
    box.classList.remove('open');
    box.innerHTML = '<div class="muted tiny" style="padding:10px">登录后自动加载群列表</div>';
  }
  renderQqGroups();
  await apiResult('/api/qq/relogin', { method: 'POST', body: '{}' });
  setTimeout(checkQqLogin, 4000);
}

async function ensureQqReady() {
  const status = document.getElementById('qqLoginStatus');
  if (status) status.textContent = '正在连接本机 QQ 服务…';
  const result = await apiResult('/api/qq/status');
  if (!result.ok) {
    if (status) status.textContent = `QQ 服务启动失败：${result.data.message || ''}`;
    return;
  }
  checkQqLogin();
}

async function installQqBridge() {
  const status = document.getElementById('qqLoginStatus');
  if (status) {
    status.textContent = '正在下载 QQ 桥（约 340MB），下载完会自动启动，请稍候…';
  }
  const result = await apiResult('/api/qq/install', { method: 'POST', body: '{}' });
  if (!result.ok) {
    if (status) status.textContent = `安装失败：${result.data.message || ''}`;
    return;
  }
  if (status) status.textContent = '安装完成，正在启动 QQ 服务…';
  await apiResult('/api/qq/status');
  checkQqLogin();
}

async function refreshQqQr() {
  const status = document.getElementById('qqLoginStatus');
  if (status) status.textContent = '正在刷新二维码…';
  const result = await apiResult('/api/qq/qr/refresh', { method: 'POST', body: '{}' });
  if (!result.ok) {
    if (status) status.textContent = `刷新二维码失败：${result.data.message || ''}`;
    qqLoginTimer = setTimeout(checkQqLogin, 4000);
    return;
  }
  qqQrStamp = null;
  checkQqLogin();
}

async function checkQqLogin() {
  const status = document.getElementById('qqLoginStatus');
  const image = document.getElementById('qqQr');
  if (!status || !image) return;
  clearTimeout(qqLoginTimer);
  const result = await apiResult('/api/qq/state');
  if (!result.ok) {
    status.textContent = `QQ 服务检查失败：${result.data.message || ''}`;
    qqLoginTimer = setTimeout(checkQqLogin, 5000);
    return;
  }
  const data = result.data;
  if (!data.installed) {
    status.innerHTML = '还没安装 QQ 桥（NapCat），首次使用需要下载一次。'
      + '<button type="button" class="btn tiny" style="margin-left:8px" onclick="installQqBridge()">下载并安装</button>';
    image.style.display = 'none';
    return;
  }
  if (!data.running) {
    status.textContent = 'QQ 服务未运行，正在启动…';
    image.style.display = 'none';
    qqQrStamp = null;
    await apiResult('/api/qq/status');
    qqLoginTimer = setTimeout(checkQqLogin, 2500);
    return;
  }
  if (data.logged_in) {
    state.qqLoggedIn = true;
    image.style.display = 'none';
    if (!data.uin) {
      status.textContent = '已登录，正在接通群列表…';
      if (!qqWiring) {
        qqWiring = true;
        apiResult('/api/qq/status').finally(() => { qqWiring = false; });
      }
      qqLoginTimer = setTimeout(checkQqLogin, 2000);
      return;
    }
    const who = data.name || data.uin || 'QQ';
    status.textContent = data.uin ? `已登录：${who}（${data.uin}）` : `已登录：${who}`;
    loadQqGroups();
    return;
  }
  // Not logged in: the group list is meaningless until a scan finishes.
  state.qqLoggedIn = false;
  state.qqGroups = [];
  const box = document.getElementById('qqGroupList');
  if (box) {
    box.innerHTML = '<div class="muted tiny" style="padding:10px">登录后自动加载群列表</div>';
  }
  const hint = document.getElementById('qqGroupHint');
  if (hint) hint.textContent = '未登录，登录后加载群列表';
  renderQqGroups();
  const left = Math.max(0, Number(data.qr_left || 0));
  if (data.scanned && data.phase === 'initializing') {
    status.textContent = '已确认，正在完成登录…';
  } else if (data.scanned) {
    status.textContent = '已扫码，请在手机上点确认登录';
  } else if (data.error) {
    status.textContent = `扫码登录失败：${data.error}`;
  } else if (!data.qr_ready) {
    status.textContent = '正在生成二维码…';
  } else {
    status.textContent = `用手机 QQ 扫码登录 · 二维码 ${left} 秒后自动换新`;
  }
  if (data.qr_ready) {
    if (qqQrStamp !== data.qr_mtime) {
      qqQrStamp = data.qr_mtime;
      image.onload = () => { image.style.display = 'block'; };
      image.onerror = () => {
        image.style.display = 'none';
        qqQrStamp = null;
      };
      image.src = `/api/qq/qr?v=${data.qr_mtime}&r=${Date.now()}`;
    }
    image.style.display = 'block';
  } else {
    image.style.display = 'none';
    qqQrStamp = null;
  }
  qqLoginTimer = setTimeout(checkQqLogin, 2000);
}

function renderSelectedQqGroups() {
  const box = document.getElementById('qqSelectedList');
  if (!box) return;
  const count = document.getElementById('qqSelectedCount');
  const selected = Array.from(state.qqSelected || []);
  const byId = new Map((state.qqGroups || []).map(group => [String(group.id), group]));
  if (count) count.textContent = state.qqLoggedIn ? `${selected.length} 个` : '未登录';
  if (!state.qqLoggedIn) {
    box.innerHTML = '<div class="qq-selected-empty">登录后显示已选群</div>';
    return;
  }
  if (!selected.length) {
    box.innerHTML = '<div class="qq-selected-empty">还没有选择群</div>';
    return;
  }
  box.innerHTML = selected.map(id => {
    const group = byId.get(String(id));
    const label = group?.name || `群 ${id}`;
    return `<span class="qq-selected-chip" title="群号 ${esc(id)}">
      <span class="qq-selected-chip-name">${esc(label)}</span>
      <button type="button" aria-label="移除 ${esc(label)}"
              onclick="toggleQqGroup('${esc(id)}')">×</button>
    </span>`;
  }).join('');
}

function renderQqGroups() {
  const box = document.getElementById('qqGroupList');
  const hint = document.getElementById('qqGroupHint');
  if (!box) return;
  const groups = state.qqGroups || [];
  const query = (document.getElementById('qqGroupSearch')?.value || '').trim().toLowerCase();
  const visible = query
    ? groups.filter(group =>
        String(group.name).toLowerCase().includes(query) || String(group.id).includes(query)
      )
    : groups;
  if (!groups.length) {
    box.innerHTML = state.qqLoggedIn
      ? '<div class="muted tiny" style="padding:10px">点「刷新群列表」加载</div>'
      : '<div class="muted tiny" style="padding:10px">未登录，登录后加载群列表</div>';
  } else {
    box.innerHTML = visible.length ? visible.map(group => {
      const on = state.qqSelected.has(String(group.id));
      return `<label class="qq-group-item ${on ? 'on' : ''}">
        <input type="checkbox" ${on ? 'checked' : ''} onchange="toggleQqGroup('${esc(group.id)}')">
        <span class="qq-group-name">${esc(group.name)}</span>
        <span class="qq-group-id">${esc(group.id)}</span>
      </label>`;
    }).join('') : '<div class="muted tiny" style="padding:10px">没有匹配的群</div>';
  }
  const input = document.getElementById('cfgConversations');
  if (input) input.value = Array.from(state.qqSelected || []).join(',');
  const loaded = (state.qqGroups || []).length > 0;
  if (hint) {
    hint.textContent = !state.qqLoggedIn
      ? '未登录，登录后加载群列表'
      : state.qqSelected.size
      ? (loaded
          ? `已选 ${state.qqSelected.size} 个群`
          : '正在加载群列表')
      : (loaded ? '还没选群' : '登录后加载群列表');
  }
  renderSelectedQqGroups();
}

function openQqDropdown() {
  const box = document.getElementById('qqGroupList');
  if (box) box.classList.add('open');
  renderQqGroups();
}

document.addEventListener('click', event => {
  if (event.target.closest('.qq-combo')) return;
  const box = document.getElementById('qqGroupList');
  if (box) box.classList.remove('open');
});

function toggleQqGroup(id) {
  const key = String(id);
  if (state.qqSelected.has(key)) state.qqSelected.delete(key);
  else state.qqSelected.add(key);
  renderQqGroups();
}

async function loadQqGroups() {
  const hint = document.getElementById('qqGroupHint');
  if (!state.sourceEditingId) {
    if (hint) hint.textContent = '先保存来源，再读取群列表';
    return;
  }
  if (hint) hint.textContent = '读取中…';
  const body = {
    base_url: (document.getElementById('cfgBaseUrl')?.value || '').trim(),
    access_token: (document.getElementById('cfgAccessToken')?.value || '').trim()
  };
  const result = await apiResult(
    '/api/sources/' + encodeURIComponent(state.sourceEditingId) + '/groups',
    { method: 'POST', body: JSON.stringify(body) }
  );
  if (!result.ok) {
    if (hint) hint.textContent = '读取失败：' + (result.data.message || '未知错误');
    return;
  }
  const groups = result.data.groups || [];
  const available = new Set(groups.map(group => String(group.id)));
  state.qqSelected = new Set(
    Array.from(state.qqSelected || []).filter(id => available.has(String(id)))
  );
  state.qqGroups = groups;
  renderQqGroups();
}

function renderPromptFiles() {
  const box = document.getElementById('promptFileList');
  if (!box) return;
  box.innerHTML = (state.promptFiles || []).map((file, index) =>
    `<span class="model-tag"><a href="${esc(file.path)}" target="_blank" rel="noreferrer">${esc(file.name)}</a>
      <button type="button" aria-label="移除" onclick="removePromptFile(${index})">×</button></span>`
  ).join('') || '<span class="muted tiny">还没有附件。</span>';
}

function removePromptFile(index) {
  state.promptFiles.splice(index, 1);
  renderPromptFiles();
}

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || '').split(',')[1] || '');
    reader.onerror = () => reject(new Error('读取文件失败'));
    reader.readAsDataURL(file);
  });
}

let loginPoll = null;

function cookieHint(values) {
  if (values && values.cookie_invalid) {
    const checked = values.cookie_checked_at
      ? `（${fmtFullTime(values.cookie_checked_at)} 测试未通过）`
      : '';
    return `Cookie 已失效${checked}，请点「浏览器登录」重新登录。`;
  }
  if (values && values.cookie_expires) {
    const when = new Date(values.cookie_expires).getTime();
    if (!Number.isNaN(when)) {
      if (when < Date.now()) {
        return `Cookie 已于 ${fmtFullTime(values.cookie_expires)} 过期，请重新登录。`;
      }
      return `Cookie 有效期至 ${fmtFullTime(values.cookie_expires)}。`;
    }
  }
  if (values && values.cookie) {
    return '已保存 Cookie，但有效期未知（手动粘贴的没有过期信息）。点「浏览器登录」可让程序接管并显示有效期。';
  }
  return '需要登录的页面点这里，登录后关掉浏览器窗口即可。';
}

async function startBrowserLogin() {
  if (!state.sourceEditingId) {
    notify('先保存来源', '浏览器登录需要已经保存的来源', 'error');
    return;
  }
  const label = document.getElementById('loginStatus');
  const result = await apiResult(
    '/api/sources/' + encodeURIComponent(state.sourceEditingId) + '/login',
    { method: 'POST', body: '{}' }
  );
  if (!result.ok) {
    notify('无法打开浏览器', result.data.message || '', 'error');
    return;
  }
  notify('已打开浏览器', '请完成登录，然后关闭浏览器窗口', 'ok');
  if (label) label.textContent = '等待登录…（检测到登录成功会自动关闭浏览器）';
  clearInterval(loginPoll);
  loginPoll = setInterval(async () => {
    const status = await apiResult(
      '/api/sources/' + encodeURIComponent(state.sourceEditingId) + '/login-status'
    );
    if (!status.ok || status.data.running) return;
    clearInterval(loginPoll);
    loginPoll = null;
    if (status.data.error) {
      notify('登录未完成', status.data.error, 'error');
      if (label) label.textContent = status.data.error;
      return;
    }
    notify('Cookie 已更新', `从浏览器取到 ${status.data.saved} 条 Cookie`, 'ok');
    await loadSources();
    openSourceEditor(state.sourceEditingId);
  }, 2000);
}

async function uploadPromptFiles(input) {
  const files = Array.from(input.files || []);
  input.value = '';
  if (!files.length) return;
  if (!state.promptFiles) state.promptFiles = [];
  for (const file of files) {
    if (String(file.type || '').startsWith('image/') && !state.visionSupported) {
      notify('当前模型不支持图片', `${file.name} 未上传，只能使用 md / txt。`, 'error');
      continue;
    }
    if (file.size > 10 * 1024 * 1024) {
      notify('文件过大', `${file.name} 超过 10MB`, 'error');
      continue;
    }
    try {
      const data = await fileToBase64(file);
      const result = await api('/api/uploads', {
        method: 'POST',
        body: JSON.stringify({ filename: file.name, data_base64: data })
      });
      state.promptFiles.push({
        name: result.name || file.name,
        path: result.path,
        type: file.type || ''
      });
      renderPromptFiles();
    } catch (error) {
      notify('上传失败', `${file.name}：${error.message}`, 'error');
    }
  }
}

async function saveSourceForm(event) {
  if (event) event.preventDefault();
  const id = state.sourceEditingId;
  const type = document.getElementById('srcType').value;
  const config = Object.assign({}, state.sourceFormConfig || {});
  const value = elementId => {
    const element = document.getElementById(elementId);
    return element ? element.value.trim() : '';
  };
  const checked = elementId => Boolean(document.getElementById(elementId)?.checked);
  if (['web', 'ai_web'].includes(type)) {
    const url = value('cfgUrl');
    if (!url) { notify('来源配置无效', '请填写来源地址。', 'error'); return; }
    config.url = url;
    if (type === 'ai_web') {
      config.prompt = value('cfgPrompt');
      config.prompt_files = (state.promptFiles || []).map(file => ({
        name: file.name, path: file.path, type: file.type || ''
      }));
      const cookie = value('cfgCookie');
      if (cookie && cookie !== '***') config.cookie = cookie;
    }
  } else if (type === 'qq_onebot') {
    config.base_url = value('cfgBaseUrl') || config.base_url || 'http://127.0.0.1:3000';
    const token = value('cfgAccessToken');
    if (token && token !== '***') config.access_token = token;
    config.selected_conversations = value('cfgConversations')
      .split(/[,，\s]+/).map(item => item.trim()).filter(Boolean);
    config.prompt = value('cfgPrompt');
    delete config.history_from;
    delete config.history_to;
    delete config.history_imported_at;
    delete config.history_imported_from;
    delete config.history_imported_to;
    delete config.include_private;
    delete config.count_per_poll;
  }
  const payload = {
    type,
    name: value('srcName'),
    interval_seconds: Math.max(60, Number(value('srcInterval') || 10) * 60),
    default_category: value('srcCategory') || null,
    enabled: checked('srcEnabled'),
    config
  };
  if (!payload.name) { notify('来源配置无效', '请填写来源名称。', 'error'); return; }
  try {
    if (id) {
      await api('/api/sources/' + id, { method: 'PATCH', body: JSON.stringify(payload) });
    } else {
      await api('/api/sources', { method: 'POST', body: JSON.stringify(payload) });
    }
    notify(id ? '来源配置已保存' : '来源已添加', payload.name, 'ok');
    closeSourceEditor();
    loadSources();
    refreshStatus();
  } catch (error) {
    notify('保存失败', error.message, 'error');
  }
}

/* Model --------------------------------------------------------------- */
function modelArrayFromResponse(data) {
  const raw = Array.isArray(data?.providers) ? data.providers
    : Array.isArray(data?.items) ? data.items
    : Array.isArray(data?.models) ? data.models : null;
  if (raw) return raw.map((item, index) => normalizeModelProvider(item, index));
  // Compatibility with the old single-config response.
  const config = data?.config || {};
  const provider = data?.configured_provider || config.provider || '';
  if (!provider || provider === 'unconfigured') return [];
  return [normalizeModelProvider(Object.assign({}, config, { provider }), 0)];
}

function normalizeModelProvider(item, index) {
  const models = Array.isArray(item?.model_list) ? item.model_list
    : Array.isArray(item?.models) ? item.models
    : String(item?.model_list || '').split(/\r?\n/).map(value => value.trim()).filter(Boolean);
  const id = item?.id ?? item?.provider_id ?? item?.key ?? item?.name ?? `provider-${index}`;
  return Object.assign({}, item, {
    id: String(id),
    display_name: item?.display_name || item?.name || '',
    note: item?.note || '',
    official_url: item?.official_url || '',
    protocol: item?.protocol || 'chat_completions',
    base_url: item?.base_url || item?.api_url || '',
    model: item?.model || item?.current_model || item?.model_name || models[0] || '',
    model_list: models,
    api_key_set: Boolean(item?.api_key_set || item?.has_api_key || item?.key_configured),
    api_key_hint: item?.api_key_hint || '',
    is_default: Boolean(item?.is_default || item?.default || item?.current)
  });
}

function renderModelProviders() {
  const target = document.getElementById('modelProviderList');
  if (!target) return;
  if (!state.modelProviders.length) {
    target.innerHTML = `
      <div class="empty-state model-empty">
        <div class="empty-glyph">□</div>
        <h3>还没有模型供应商</h3>
        <p>点击右上角「添加供应商」，添加一个用于处理消息的远程模型供应商。</p>
      </div>`;
    return;
  }
  target.innerHTML = state.modelProviders.map(provider => {
    const protocol = modelProtocolLabel(provider.protocol);
    const count = provider.model_list.length;
    return `
      <article class="model-provider-card ${provider.is_default ? 'default' : ''}">
        <div>
          <h3>${esc(provider.display_name || '未命名供应商')}</h3>
          <div class="model-card-badges">
            ${provider.is_default ? '<span class="badge ok">当前默认</span>' : ''}
            <span class="badge normal">${esc(protocol)}</span>
            <span class="badge low">${count} 个模型</span>
          </div>
          <div class="model-meta">${esc(provider.base_url || '未填写 API 请求地址')}</div>
          ${provider.note ? `<div class="model-meta">${esc(provider.note)}</div>` : ''}
        </div>
        <div class="model-card-actions">
          ${provider.is_default ? '' : `<button class="btn tiny" type="button" onclick="setDefaultModel(decodeURIComponent('${encodeURIComponent(provider.id)}'))">设为默认</button>`}
          <button class="btn tiny" type="button" onclick="openModelEditor(decodeURIComponent('${encodeURIComponent(provider.id)}'))">编辑</button>
          <button class="btn tiny danger" type="button" onclick="askDeleteModel(decodeURIComponent('${encodeURIComponent(provider.id)}'))">删除</button>
        </div>
      </article>`;
  }).join('');
}

function modelProtocolLabel(value) {
  return ({
    chat_completions: 'Chat Completions',
    responses: 'Responses',
    anthropic_messages: 'Anthropic Messages'
  })[value] || value || '未指定格式';
}

function renderModelProtocolOptions(protocols, selected = '') {
  const select = document.getElementById('modelProtocol');
  if (!select) return;
  const supported = Array.isArray(protocols) && protocols.length ? protocols : ['chat_completions'];
  const values = supported.map(item => typeof item === 'string' ? item : item.value).filter(Boolean);
  if (selected && !values.includes(selected)) values.push(selected);
  select.innerHTML = values.map(value =>
    `<option value="${esc(value)}" ${value === (selected || values[0]) ? 'selected' : ''}>${esc(modelProtocolLabel(value))}</option>`
  ).join('');
}

async function loadModel() {
  const result = await apiResult('/api/models');
  if (!result.ok) {
    toast(result.data.message || '模型配置读取失败', true);
    return;
  }
  state.model = result.data;
  renderModelProtocolOptions(result.data.protocols || result.data.supported_protocols);
  state.modelProviders = modelArrayFromResponse(result.data);
  const defaultId = result.data.default_provider_id == null
    ? ''
    : String(result.data.default_provider_id);
  state.modelProviders.forEach(provider => {
    provider.is_default = Boolean(defaultId && provider.id === defaultId);
  });
  if (state.modelEditingId === null) renderModelProviders();
  else {
    const provider = state.modelProviders.find(item => item.id === String(state.modelEditingId));
    if (provider) await fillModelEditor(provider);
  }
}

function resetModelEditor() {
  document.getElementById('modelEditor').reset();
  document.getElementById('modelEditorTitle').textContent = '添加供应商';
  const keyInput = document.getElementById('modelKey');
  keyInput.value = '';
  keyInput.type = 'password';
  keyInput.nextElementSibling.textContent = '显示';
  keyInput.placeholder = '输入 API Key';
  document.getElementById('modelKeyHint').textContent = '';
  document.getElementById('modelClearKey').checked = false;
  state.modelNames = [];
  renderModelList('');
}

async function openModelEditor(id = null) {
  state.modelEditingId = id === null ? null : String(id);
  saveUiState();
  resetModelEditor();
  const provider = state.modelProviders.find(item => item.id === state.modelEditingId);
  if (provider) await fillModelEditor(provider);
  document.getElementById('modelListPage').classList.add('hidden');
  document.getElementById('modelEditor').classList.add('active');
  renderTopActions();
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function closeModelEditor() {
  state.modelEditingId = null;
  saveUiState();
  document.getElementById('modelEditor').classList.remove('active');
  document.getElementById('modelListPage').classList.remove('hidden');
  renderModelProviders();
  renderTopActions();
}

async function fillModelEditor(provider) {
  document.getElementById('modelEditorTitle').textContent = '编辑供应商';
  document.getElementById('modelDisplayName').value = provider.display_name || '';
  document.getElementById('modelNote').value = provider.note || '';
  document.getElementById('modelOfficialUrl').value = provider.official_url || '';
  renderModelProtocolOptions(state.model?.protocols || state.model?.supported_protocols, provider.protocol || 'chat_completions');
  document.getElementById('modelBaseUrl').value = provider.base_url || '';
  const keyInput = document.getElementById('modelKey');
  keyInput.value = '';
  keyInput.type = 'password';
  keyInput.nextElementSibling.textContent = '显示';
  keyInput.placeholder = provider.api_key_set ? '正在读取已保存密钥…' : '输入 API Key';
  document.getElementById('modelKeyHint').textContent = provider.api_key_set
    ? `已保存 ${provider.api_key_hint || ''}，留空则保持不变；填写新值会覆盖原密钥。`.replace('已保存 ，', '已保存密钥，')
    : '';
  if (provider.api_key_set) {
    try {
      const stored = await api('/api/models/' + encodeURIComponent(provider.id) + '/key');
      keyInput.value = stored.api_key || '';
      keyInput.placeholder = '输入 API Key';
      document.getElementById('modelKeyHint').textContent = '已保存密钥，点击右侧「显示」可查看。';
    } catch (error) {
      keyInput.placeholder = '输入 API Key';
    }
  }
  document.getElementById('modelClearKey').checked = false;
  state.modelNames = provider.model_list.slice();
  if (provider.model && !state.modelNames.includes(provider.model)) state.modelNames.unshift(provider.model);
  renderModelList(provider.model || state.modelNames[0] || '');
}

function modelFormPayload() {
  const selectedModel = document.getElementById('modelName').value.trim();
  const protocol = document.getElementById('modelProtocol').value;
  const payload = {
    provider: protocol === 'anthropic_messages' ? 'anthropic' : 'openai_compatible',
    display_name: document.getElementById('modelDisplayName').value.trim(),
    note: document.getElementById('modelNote').value.trim(),
    official_url: document.getElementById('modelOfficialUrl').value.trim(),
    protocol,
    base_url: document.getElementById('modelBaseUrl').value.trim(),
    model: selectedModel,
    model_list: state.modelNames.slice()
  };
  const key = document.getElementById('modelKey').value.trim();
  if (key) payload.api_key = key;
  if (document.getElementById('modelClearKey').checked) payload.clear_api_key = true;
  return payload;
}

async function saveModel(event) {
  if (event) event.preventDefault();
  const payload = modelFormPayload();
  if (!payload.display_name) { toast('请填写供应商名称', true); return; }
  if (!payload.base_url) { toast('请填写 API 请求地址', true); return; }
  if (!payload.api_key && !state.modelEditingId) { toast('请填写 API Key', true); return; }
  if (!payload.model) { toast('请先添加并选择模型', true); return; }
  try {
    const path = state.modelEditingId === null
      ? '/api/models'
      : '/api/models/' + encodeURIComponent(state.modelEditingId);
    const method = state.modelEditingId === null ? 'POST' : 'PATCH';
    await api(path, { method, body: JSON.stringify(payload) });
    toast('模型配置已保存');
    closeModelEditor();
    await loadModel();
    refreshStatus();
  } catch (error) {
    toast(error.message, true);
  }
}

function renderModelList(selected) {
  const select = document.getElementById('modelName');
  const tags = document.getElementById('modelListTags');
  if (!select || !tags) return;
  if (!state.modelNames.length) {
    select.innerHTML = '<option value="">请先添加模型</option>';
    tags.innerHTML = '<span class="muted tiny">还没有模型，请手动添加。</span>';
    return;
  }
  const active = selected && state.modelNames.includes(selected) ? selected : state.modelNames[0];
  select.innerHTML = state.modelNames.map(name =>
    `<option value="${esc(name)}" ${name === active ? 'selected' : ''}>${esc(name)}</option>`
  ).join('');
  tags.innerHTML = state.modelNames.map((name, index) => `
    <span class="model-tag ${name === active ? 'active' : ''}">
      <button type="button" onclick="selectModelOption(${index})">${esc(name)}</button>
      <button type="button" aria-label="移除模型" onclick="removeModelOption(${index})">×</button>
    </span>`).join('');
}

function selectModelOption(index) {
  const name = state.modelNames[index];
  if (!name) return;
  document.getElementById('modelName').value = name;
  renderModelList(name);
}

function removeModelOption(index) {
  const removed = state.modelNames[index];
  if (!removed) return;
  const selected = document.getElementById('modelName').value;
  state.modelNames.splice(index, 1);
  renderModelList(selected === removed ? state.modelNames[0] || '' : selected);
}

function addModelFromInput() {
  const input = document.getElementById('modelListInput');
  const name = input.value.trim();
  if (!name) return;
  if (!state.modelNames.includes(name)) state.modelNames.push(name);
  input.value = '';
  renderModelList(name);
}

function toggleModelKey() {
  const input = document.getElementById('modelKey');
  const button = input?.nextElementSibling;
  if (!input || !button) return;
  input.type = input.type === 'password' ? 'text' : 'password';
  button.textContent = input.type === 'password' ? '显示' : '隐藏';
}

function notify(title, detail = '', tone = 'ok', timeout = 5200) {
  const host = document.getElementById('topNotice');
  if (!host) return null;
  const item = document.createElement('div');
  item.className = 'top-notice ' + tone;
  const titleEl = document.createElement('div');
  titleEl.className = 'top-notice-title';
  const detailEl = document.createElement('div');
  detailEl.className = 'top-notice-detail';
  titleEl.textContent = title;
  detailEl.textContent = detail;
  item.append(titleEl, detailEl);
  host.appendChild(item);
  let timer = 0;
  const api = {
    set(nextTitle, nextDetail = '', nextTone = 'ok', nextTimeout = 5200) {
      clearTimeout(timer);
      item.className = 'top-notice ' + nextTone;
      titleEl.textContent = nextTitle;
      detailEl.textContent = nextDetail;
      if (nextTimeout) timer = setTimeout(() => api.close(), nextTimeout);
    },
    close() {
      clearTimeout(timer);
      item.classList.add('leaving');
      setTimeout(() => item.remove(), 200);
    }
  };
  if (timeout) timer = setTimeout(() => api.close(), timeout);
  return api;
}

async function fetchModelList() {
  const button = document.querySelector('[data-action="fetch-models"]');
  const fail = message => notify('获取模型列表失败', message, 'error');

  const payload = modelFormPayload();
  if (!payload.base_url) { fail('请先填写 API 请求地址。'); return; }
  if (!payload.api_key && state.modelEditingId === null) { fail('请先填写 API Key。'); return; }

  if (button) { button.disabled = true; button.textContent = '获取中…'; }
  const notice = notify('正在获取模型列表…', payload.base_url, 'pending', 0);
  let result;
  try {
    result = await api('/api/models/fetch', {
      method: 'POST',
      body: JSON.stringify(Object.assign({}, payload, { id: state.modelEditingId }))
    });
  } catch (error) {
    notice?.set('获取模型列表失败', error.message, 'error');
    return;
  } finally {
    if (button) { button.disabled = false; button.textContent = '获取模型列表'; }
  }

  const models = Array.isArray(result?.models) ? result.models : [];
  if (!models.length) {
    notice?.set('没有可用模型', '服务商未返回任何模型。', 'error');
    return;
  }

  // The provider's response is the source of truth for this list; keeping
  // previously typed names around made the list grow on every fetch.
  const fetched = [];
  models.forEach(name => {
    const value = String(name || '').trim();
    if (value && !fetched.includes(value)) fetched.push(value);
  });
  if (!fetched.length) {
    notice?.set('没有可用模型', '服务商未返回任何模型。', 'error');
    return;
  }
  const previous = document.getElementById('modelName').value;
  state.modelNames = fetched;
  renderModelList(fetched.includes(previous) ? previous : fetched[0]);
  notice?.set('模型列表已更新', `共 ${fetched.length} 个：${fetched.join('、')}`, 'ok');
}

async function testModel() {
  const payload = modelFormPayload();
  const fail = message => notify('连接测试无法开始', message, 'error');
  if (!payload.base_url) { fail('请先填写 API 请求地址。'); return; }
  if (!payload.model) { fail('请先添加并选择模型。'); return; }
  if (!payload.api_key && state.modelEditingId === null) { fail('请先填写 API Key。'); return; }
  const label = payload.display_name || '未命名供应商';
  const notice = notify('正在测试连接…', `${label} · ${payload.base_url} · ${payload.model}`, 'pending', 0);
  try {
    const result = await api('/api/models/test', { method: 'POST', body: JSON.stringify(Object.assign({}, payload, {
      id: state.modelEditingId
    })) });
    notice?.set('连接测试成功', `${label} · ${result.model || payload.model}`, 'ok');
  } catch (error) {
    notice?.set('连接测试失败', error.message, 'error');
  }
}

async function setDefaultModel(id) {
  try {
    await api('/api/models/' + encodeURIComponent(id) + '/default', { method: 'POST' });
    toast('默认模型已更新');
    await loadModel();
    refreshStatus();
  } catch (error) { toast(error.message, true); }
}

function askDeleteModel(id) {
  const provider = state.modelProviders.find(item => item.id === String(id));
  if (!provider) return;
  openModal(`
    <div class="modal-head">
      <div><div class="eyebrow">REMOVE PROVIDER</div><h2>删除模型供应商</h2></div>
      <button class="icon-btn" onclick="closeModal()" aria-label="关闭">×</button>
    </div>
    <p>确定删除“${esc(provider.display_name || '未命名供应商')}”吗？已保存的 API 配置和模型列表都会移除。</p>
    <div class="row" style="margin-top:16px">
      <button class="btn danger" onclick="deleteModel('${esc(id)}')">确定删除</button>
      <button class="btn" onclick="closeModal()">取消</button>
    </div>`);
}

async function deleteModel(id) {
  try {
    await api('/api/models/' + encodeURIComponent(id), { method: 'DELETE' });
    closeModal();
    toast('模型供应商已删除');
    await loadModel();
    refreshStatus();
  } catch (error) { toast(error.message, true); }
}

/* Reminders ----------------------------------------------------------- */
function pickRangeStart() {
  const input = document.getElementById('rangeStart');
  if (!input) return;
  openDatePicker(input, input.value || localToday(), value => {
    input.value = value || '';
  });
}

function pickRangeEnd() {
  const input = document.getElementById('rangeEnd');
  if (!input) return;
  openDatePicker(input, input.value || localToday(), value => {
    input.value = value || '';
  });
}

function clearRangeEnd() {
  const input = document.getElementById('rangeEnd');
  if (input) input.value = '';
}

async function loadRange() {
  const status = document.getElementById('rangeStatus');
  const result = await apiResult('/api/range');
  if (!result.ok) {
    if (status) status.textContent = `读取失败：${result.data.message || ''}`;
    return;
  }
  const range = result.data.range || {};
  const start = document.getElementById('rangeStart');
  const end = document.getElementById('rangeEnd');
  if (start) start.value = range.start || '';
  if (end) end.value = range.end || '';
  const digestTime = document.getElementById('digestTime');
  if (digestTime) digestTime.value = result.data.digest_time || '22:00';
  const autostart = document.getElementById('autostartEnabled');
  if (autostart) autostart.checked = Boolean(result.data.autostart);
  if (status) {
    const parts = [];
    if (range.effective_end) parts.push(`当前日报截止：${range.effective_end}`);
    if (result.data.digest_time) parts.push(`每日 ${result.data.digest_time} 生成日报`);
    parts.push(result.data.autostart_active ? '开机自启已生效' : '未开启开机自启');
    status.textContent = parts.join(' · ');
  }
}

async function saveRange() {
  const start = document.getElementById('rangeStart')?.value.trim() || '';
  const end = document.getElementById('rangeEnd')?.value.trim() || '';
  const digestTime = document.getElementById('digestTime')?.value || '22:00';
  const autostart = Boolean(document.getElementById('autostartEnabled')?.checked);
  const status = document.getElementById('rangeStatus');
  if (!start) {
    toast('请选择起始日期', true);
    return;
  }
  try {
    const result = await api('/api/range', {
      method: 'POST',
      body: JSON.stringify({ start, end, digest_time: digestTime, autostart })
    });
    if (status) {
      const parts = [`已保存，当前日报截止：${result.range.effective_end}`];
      parts.push(`每日 ${result.digest_time} 生成日报`);
      parts.push(result.autostart_active ? '开机自启已生效' : '未开启开机自启');
      status.textContent = parts.join(' · ');
    }
    toast('基础配置已保存');
    refreshStatus();
  } catch (error) {
    if (status) status.textContent = `保存失败：${error.message}`;
  }
}

async function loadProfile() {
  const result = await apiResult('/api/profile');
  if (!result.ok) return;
  const profile = result.data.profile || {};
  const values = {
    profileIdentity: profile.identity || '',
    profileInterests: profile.interests || '',
    profileIgnore: profile.ignore || '',
    profileTodoRule: profile.todo_rule || '',
    profileNotes: profile.notes || ''
  };
  Object.keys(values).forEach(id => {
    const el = document.getElementById(id);
    if (el) el.value = values[id];
  });
  const status = document.getElementById('profileStatus');
  if (status) status.textContent = '';
}

async function saveProfile() {
  const status = document.getElementById('profileStatus');
  const payload = {
    identity: document.getElementById('profileIdentity')?.value || '',
    interests: document.getElementById('profileInterests')?.value || '',
    ignore: document.getElementById('profileIgnore')?.value || '',
    todo_rule: document.getElementById('profileTodoRule')?.value || '',
    notes: document.getElementById('profileNotes')?.value || ''
  };
  const result = await apiResult('/api/profile', {
    method: 'POST',
    body: JSON.stringify({ profile: payload })
  });
  if (!result.ok) {
    if (status) status.textContent = `保存失败：${result.data.message || ''}`;
    return;
  }
  if (status) status.textContent = '已保存，从下一次处理开始生效。';
}

document.addEventListener('keydown', event => {
  if (event.key === 'Escape') {
    closeModal();
    closeDigest();
  }
});

window.addEventListener('DOMContentLoaded', async () => {
  await refreshStatus();
  await loadSources();
  let saved = null;
  try {
    saved = JSON.parse(sessionStorage.getItem('msgdesk_ui') || 'null');
  } catch (error) {
    saved = null;
  }
  const hashView = location.hash.replace('#', '');
  const initial = VIEWS.includes(hashView)
    ? hashView
    : (saved && VIEWS.includes(saved.view) ? saved.view : 'messages');
  showView(initial);
  setInterval(async () => {
    if (document.hidden) return;
    await refreshStatus();
    if (state.view === 'messages') await loadMessages({ silent: true });
  }, 5000);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
      refreshStatus();
      if (state.view === 'messages') loadMessages({ silent: true });
    }
  });
});
