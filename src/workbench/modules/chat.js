import { $, api, setRunning, showError, state } from './state.js';
import { loadProjects, paintProjects } from './sidebar.js';

export function appendInlineText(parent, text) {
  for (const part of text.split(/(\*\*[^*\n]+\*\*|`[^`\n]+`)/)) {
    if (!part) continue;
    if (part.startsWith('**') && part.endsWith('**')) {
      const strong = document.createElement('strong');
      strong.textContent = part.slice(2, -2);
      parent.append(strong);
    } else if (part.startsWith('`') && part.endsWith('`')) {
      const code = document.createElement('code');
      code.textContent = part.slice(1, -1);
      parent.append(code);
    } else parent.append(document.createTextNode(part));
  }
}

function renderTable(container, lines) {
  const table = document.createElement('table');
  const body = document.createElement('tbody');
  lines.forEach((line, index) => {
    if (index === 1) return;
    const row = document.createElement(index === 0 ? 'thead' : 'tbody');
    const tr = document.createElement('tr');
    line.split('|').slice(1, -1).map((cell) => cell.trim()).forEach((cell) => {
      const cellElement = document.createElement(index === 0 ? 'th' : 'td');
      appendInlineText(cellElement, cell);
      tr.append(cellElement);
    });
    row.append(tr);
    if (index === 0) table.append(row);
    else body.append(tr);
  });
  if (body.childElementCount) table.append(body);
  container.append(table);
}

export function renderReply(container, text) {
  container.classList.add('formatted');
  let block = null;
  let code = null;
  const lines = text.split(/\r?\n/);
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    if (line.trimStart().startsWith('```')) {
      if (code) code = null;
      else {
        const pre = document.createElement('pre');
        const codeEl = document.createElement('code');
        const copy = document.createElement('button');
        copy.type = 'button';
        copy.className = 'copy-code';
        copy.textContent = '复制';
        copy.onclick = async () => {
          try { await navigator.clipboard?.writeText(codeEl.textContent); } catch (_) { /* Clipboard can be unavailable in an unfocused tab. */ }
          copy.textContent = '已复制';
          setTimeout(() => { copy.textContent = '复制'; }, 1200);
        };
        pre.append(copy, codeEl);
        code = codeEl;
        container.append(pre);
      }
      block = null;
      continue;
    }
    if (code) {
      code.append(document.createTextNode(line + '\n'));
      continue;
    }
    const tableLines = [];
    if (/^\s*\|.*\|\s*$/.test(line) && /^\s*\|(?:\s*:?-+:?\s*\|)+\s*$/.test(lines[index + 1] || '')) {
      tableLines.push(line, lines[index + 1]);
      index += 1;
      while (/^\s*\|.*\|\s*$/.test(lines[index + 1] || '')) tableLines.push(lines[++index]);
      renderTable(container, tableLines);
      block = null;
      continue;
    }
    if (!line.trim()) { block = null; continue; }
    const heading = line.match(/^\s*(#{1,6})\s+(.+)$/);
    const item = line.match(/^\s*(\d+\.|[-*+])\s+(.+)$/);
    if (heading) {
      const element = document.createElement(heading[1].length < 3 ? 'h3' : 'h4');
      appendInlineText(element, heading[2]);
      container.append(element);
      block = null;
    } else if (item) {
      const type = item[1].endsWith('.') ? 'ol' : 'ul';
      if (block?.localName !== type) {
        block = document.createElement(type);
        if (type === 'ol') block.start = parseInt(item[1], 10);
        container.append(block);
      }
      const entry = document.createElement('li');
      appendInlineText(entry, item[2]);
      block.append(entry);
    } else {
      if (block?.localName !== 'p') {
        block = document.createElement('p');
        container.append(block);
      } else block.append(document.createTextNode('\n'));
      appendInlineText(block, line);
    }
  }
}

export function paintDiscussionMode() {
  const button = $('#discussionModeToggle');
  button.classList.toggle('active', state.discussionMode);
  button.setAttribute('aria-pressed', String(state.discussionMode));
  button.textContent = state.discussionMode ? '商讨模式 · 开' : '商讨模式';
  button.disabled = !state.project || !state.conversation || state.loadingView || state.busy;
}

export function msg(role, text, { discussionMode = false } = {}) {
  const isHostNotice = typeof text === 'string' && text.startsWith('[宿主通知]');
  if (isHostNotice) {
    const details = document.createElement('details');
    details.className = 'system-event';
    const summary = document.createElement('summary');
    summary.textContent = '后台任务运行已结束 · 点击查看通知';
    const content = document.createElement('div');
    content.textContent = text.slice('[宿主通知]'.length).trim();
    details.append(summary, content);
    $('#messages').append(details);
    $('#messages').scrollTop = $('#messages').scrollHeight;
    return details;
  }
  const element = document.createElement('div');
  element.className = 'message ' + role;
  if (role === 'assistant') renderReply(element, text);
  else if (role === 'user') {
    const badges = [];
    let bodyText = typeof text === 'string' ? text : '';
    const prefixes = [
      ['[系统提示：模型已切换]', '模型已切换'],
      ['[当前模式：商讨模式]', '商讨模式'],
    ];
    let matched = true;
    while (matched) {
      matched = false;
      for (const [prefix, label] of prefixes) {
        if (bodyText.startsWith(prefix)) {
          if (!badges.includes(label)) badges.push(label);
          const separator = bodyText.indexOf('\n\n');
          bodyText = separator !== -1 ? bodyText.slice(separator + 2) : '';
          matched = true;
        }
      }
    }
    if (discussionMode && !badges.includes('商讨模式')) badges.push('商讨模式');
    if (badges.length) {
      const badgeRow = document.createElement('div');
      badgeRow.className = 'message-badges';
      badges.forEach((label) => {
        const badge = document.createElement('span');
        badge.className = 'mode-badge';
        badge.textContent = label;
        badgeRow.append(badge);
      });
      const body = document.createElement('div');
      body.className = 'message-text';
      body.textContent = bodyText;
      element.append(badgeRow, body);
    } else element.textContent = bodyText;
  } else element.textContent = text;
  $('#messages').append(element);
  $('#messages').scrollTop = $('#messages').scrollHeight;
  return element;
}

let workerReviewLoading = false;
let workerReviewAvailable = true;

function permissionSummary(item) {
  const input = item.tool_input || {};
  const target = input.path || input.file_path || input.command || item.message || '待确认操作';
  return `${item.tool_name || '工具调用'} · ${target} · 风险 ${item.risk_level || '未知'} · 等待 ${item.timeout_seconds || 0} 秒`;
}

export async function refreshWorkerReviews() {
  const project = state.project?.project_id;
  const thread = state.conversation?.thread_id;
  const view = state.viewVersion;
  const panel = $('#workerReviewPanel');
  if (!panel) return;
  if (!project || !thread) { panel.replaceChildren(); return; }
  if (workerReviewLoading || !workerReviewAvailable) return;
  workerReviewLoading = true;
  try {
    const response = await fetch('/workbench/api/worker-reviews?project_id=' + encodeURIComponent(project) + '&thread_id=' + encodeURIComponent(thread));
    if (response.status === 404 && state.viewVersion === view) { workerReviewAvailable = false; panel.replaceChildren(); return; }
    const data = await response.json();
    if (!response.ok) throw Error(data.error || '请求失败');
    if (state.viewVersion !== view) return;
    panel.replaceChildren();
    for (const item of data.requests) {
      const card = document.createElement('article');
      card.className = 'permission-card';
      const title = document.createElement('strong');
      title.textContent = permissionSummary(item);
      const details = document.createElement('details');
      const summary = document.createElement('summary');
      summary.textContent = '查看原始请求参数';
      const raw = document.createElement('pre');
      raw.textContent = JSON.stringify({ 任务: item.task_id, 负责人: item.provider_id, 说明: item.message, 工具调用: item.tool_call, 输入: item.tool_input }, null, 2);
      details.append(summary, raw);
      card.append(title, details);
      for (const approved of [true, false]) {
        const button = document.createElement('button');
        button.className = 'secondary';
        button.textContent = approved ? '仅批准本次' : '拒绝';
        button.onclick = async () => {
          if (state.viewVersion !== view || state.project?.project_id !== project) return;
          card.querySelectorAll('button').forEach((entry) => { entry.disabled = true; });
          try { await api('worker-reviews', { project_id: project, thread_id: thread, request_id: item.request_id, approved }); await refreshWorkerReviews(); }
          catch (error) { if (state.viewVersion === view) { showError(error.message); await refreshWorkerReviews(); } }
        };
        card.append(button);
      }
      panel.append(card);
    }
  } catch (error) { if (state.viewVersion === view && error.status !== 404) showError('无法读取负责人权限请求：' + error.message); }
  finally { workerReviewLoading = false; }
}

function selectedModelParams() {
  const value = $('#model').value;
  if (!value) return { model_use_default: true };
  const selected = JSON.parse(value);
  const params = { model_provider: selected.provider, model_name: selected.model };
  if ($('#effort').value) params.reasoning_effort = $('#effort').value;
  return params;
}

export async function sendMessage() {
  const input = $('#message');
  const text = input.value.trim();
  if (!text || !state.project || !state.conversation || state.busy || state.loadingView) return;
  const project = state.project.project_id;
  const thread = state.conversation.thread_id;
  const view = state.viewVersion;
  const discussionMode = !!state.discussionMode;
  state.busy = true;
  state.busyThreads.add(thread);
  $('#send').disabled = true;
  paintDiscussionMode();
  setRunning('PM 正在思考与处理…');
  input.value = '';
  msg('user', text, { discussionMode });
  const current = state.projectConversations.get(project)?.items?.find((item) => item.thread_id === thread);
  if (current && (!current.title || current.title === '新会话')) {
    current.title = text.slice(0, 18);
    paintProjects();
    try { await api('conversations', { project_id: project, thread_id: thread, title: current.title }); } catch (_) { /* Older services can ignore title updates. */ }
  }
  let response = false;
  try {
    const result = await fetch('/workbench/api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ project_id: project, thread_id: thread, message: text, discussion_mode: discussionMode, ...selectedModelParams() }) });
    if (!result.ok) { const value = await result.json(); throw Error(value.error || '请求失败'); }
    const reader = result.body.getReader();
    const decoder = new TextDecoder();
    let pending = '';
    while (true) {
      const { value, done } = await reader.read();
      pending += decoder.decode(value || new Uint8Array(), { stream: !done });
      const frames = pending.split('\n\n');
      pending = frames.pop();
      for (const frame of frames) {
        if (!frame.startsWith('data: ')) continue;
        const event = JSON.parse(frame.slice(6));
        if (event.event === 'error') throw Error(event.data?.message || '模型请求失败');
        for (const item of event.data?.model?.messages || []) {
          if (typeof item.content === 'string' && item.content) {
            response = true;
            if (state.viewVersion === view) msg('assistant', item.content);
          }
        }
      }
      if (done) break;
    }
    if (!response && state.viewVersion === view) showError('本轮没有可显示的回复，请查看运行状态。');
  } catch (error) { if (state.viewVersion === view) showError(error.message); }
  finally {
    state.busyThreads.delete(thread);
    if (state.conversation?.thread_id === thread) {
      state.busy = false;
      $('#send').disabled = !state.project || state.loadingView;
      setRunning('');
      paintDiscussionMode();
      await state.actions.loadTasks?.();
    }
  }
}

let pendingCard = null;
function clearPending() { pendingCard?.remove(); pendingCard = null; }

export async function checkPending() {
  const project = state.project;
  const thread = state.conversation?.thread_id;
  const view = state.viewVersion;
  if (!project || !thread) return;
  let data;
  try { data = await api('pending-decision?project_id=' + encodeURIComponent(project.project_id) + '&thread_id=' + encodeURIComponent(thread)); }
  catch (error) { if (state.viewVersion === view) { clearPending(); showError(error.message); } return; }
  if (state.viewVersion !== view) return;
  clearPending();
  if (!data.pending) return;
  if (!data.answerable || !data.decisions?.length) {
    const fallback = document.createElement('div');
    fallback.className = 'message assistant';
    fallback.textContent = '⏸ 会话上有未完成的中断，但不是可在本界面答复的决策形式；请查看运行轨迹。';
    $('#messages').append(fallback);
    pendingCard = fallback;
    $('#messages').scrollTop = $('#messages').scrollHeight;
    return;
  }
  const card = document.createElement('article');
  card.className = 'message assistant decision-card';
  const head = document.createElement('strong');
  head.textContent = '⏸ 待人工决策' + (data.decisions.length > 1 ? `（共 ${data.decisions.length} 项，按顺序答复）` : '');
  card.append(head);
  const boxes = data.decisions.map((decision, index) => {
    const question = document.createElement('p');
    question.textContent = (data.decisions.length > 1 ? `第 ${index + 1} 项 · ` : '') + ((decision.args?.question || decision.description || '（未提供问题文本）'));
    const context = decision.args?.context;
    if (typeof context === 'string' && context) {
      const contextNode = document.createElement('div');
      contextNode.className = 'notice';
      contextNode.textContent = '背景：' + context;
      card.append(contextNode);
    }
    const box = document.createElement('textarea');
    box.placeholder = '输入你的决定…';
    box.setAttribute('aria-label', '决策答复 ' + (index + 1));
    card.append(question, box);
    return box;
  });
  const submit = document.createElement('button');
  submit.className = 'primary';
  submit.textContent = '提交决策并恢复运行';
  if (data.binding_available === false) submit.disabled = true;
  const status = document.createElement('div');
  status.className = 'notice';
  status.textContent = data.binding_available === false
    ? '缺少可核对的检查点，当前无法提交。请查看运行轨迹。'
    : '提交普通聊天消息不会恢复该决策；恢复只能在这里进行。';
  submit.onclick = async () => {
    if (state.viewVersion !== view || state.busyThreads.has(thread)) return;
    const answers = boxes.map((box) => box.value.trim());
    if (answers.some((answer) => !answer)) { status.textContent = '请先填写全部答复。'; return; }
    submit.disabled = true;
    boxes.forEach((box) => { box.disabled = true; });
    state.busy = true;
    state.busyThreads.add(thread);
    paintDiscussionMode();
    setRunning('后台任务运行中…');
    try {
      await api('submit-decision', { project_id: project.project_id, thread_id: thread, answers, fingerprint: data.fingerprint, interrupt_id: data.interrupt_id || null, checkpoint_id: data.checkpoint_id || null, discussion_mode: !!state.discussionMode, ...selectedModelParams() });
      status.textContent = '决策已提交，正在等待 PM 恢复运行…';
      // The decision request has returned. The restored-run observer now owns
      // the lifecycle and will refresh this conversation when the Run ends.
      state.busyThreads.delete(thread);
      state.actions.watchRestoredRun?.(project.project_id, thread, view, state.restoredBaselines?.get(thread) || null);
    } catch (error) {
      status.textContent = error.message;
      submit.disabled = false;
      boxes.forEach((box) => { box.disabled = false; });
      state.busyThreads.delete(thread);
      if (state.viewVersion === view) {
        state.busy = false;
        setRunning('');
        paintDiscussionMode();
      }
    }
  };
  card.append(submit, status);
  $('#messages').append(card);
  pendingCard = card;
  $('#messages').scrollTop = $('#messages').scrollHeight;
}

export function initChat() {
  $('#discussionModeToggle').onclick = async () => {
    if (!state.project || !state.conversation || state.busy || state.loadingView) return;
    const project = state.project.project_id;
    const conversation = state.conversation;
    const view = state.viewVersion;
    const previous = state.discussionMode;
    const next = !previous;
    state.discussionMode = next;
    paintDiscussionMode();
    $('#discussionModeToggle').disabled = true;
    $('#send').disabled = true;
    const update = api('conversations', { project_id: project, thread_id: conversation.thread_id, discussion_mode: next });
    state.discussionModeUpdates.set(conversation.thread_id, update);
    try {
      await update;
      conversation.discussion_mode = next;
      const item = state.projectConversations.get(project)?.items?.find((entry) => entry.thread_id === conversation.thread_id);
      if (item) item.discussion_mode = next;
    } catch (error) {
      if (state.viewVersion === view) {
        state.discussionMode = previous;
        showError(error.message);
      }
    } finally {
      if (state.discussionModeUpdates.get(conversation.thread_id) === update) {
        state.discussionModeUpdates.delete(conversation.thread_id);
      }
      if (state.viewVersion === view) {
        paintDiscussionMode();
        $('#send').disabled = !state.project || state.loadingView || state.busy;
      }
    }
  };
  $('#model').onchange = () => state.actions.paintEffort?.();
  $('#send').onclick = () => state.actions.send?.();
  $('#message').onkeydown = (event) => {
    if (event.isComposing || event.keyCode === 229) return;
    if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); state.actions.send?.(); }
  };
  document.querySelectorAll('[data-tab]').forEach((tab) => tab.onclick = () => {
    document.querySelectorAll('[data-tab]').forEach((item) => { item.classList.toggle('active', item === tab); item.toggleAttribute('aria-current', item === tab); });
    $('#chatView').hidden = tab.dataset.tab !== 'chat';
    $('#activityView').hidden = tab.dataset.tab !== 'activity';
    if (tab.dataset.tab === 'activity') state.actions.loadTasks?.();
  });
  setInterval(refreshWorkerReviews, 4000);
}

export { clearPending, selectedModelParams };
