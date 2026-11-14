const $ = (s) => document.querySelector(s),
  state = {
    projects: [],
    providers: [],
    project: null,
    conversations: [],
    conversation: null,
    busyThreads: new Set(),
    drafts: new Map(),
    busy: false,
    loadingView: false,
    discovered: [],
    ephemeral: false,
    localDev: false,
    viewVersion: 0,
  };
// ACP permissions are live, single-use requests, separate from PM interrupts.
const workerReviewPanel = document.createElement('div');
workerReviewPanel.style.cssText = 'padding:0 28px;max-height:35vh;overflow:auto';
$('#chatView').insertBefore(workerReviewPanel, $('.composer'));
let workerReviewLoading = false,
  workerReviewAvailable = true;
async function refreshWorkerReviews() {
  const project = state.project?.project_id,
    thread = state.conversation?.thread_id,
    view = state.viewVersion;
  if (!project || !thread) {
    workerReviewPanel.replaceChildren();
    return;
  }
  if (workerReviewLoading || !workerReviewAvailable) return;
  workerReviewLoading = true;
  try {
    const response = await fetch(
      '/workbench/api/worker-reviews?project_id=' +
        encodeURIComponent(project) +
        '&thread_id=' +
        encodeURIComponent(thread),
    );
    if (response.status === 404 && state.viewVersion === view) {
      workerReviewAvailable = false;
      workerReviewPanel.replaceChildren();
      return;
    }
    const data = await response.json();
    if (!response.ok) throw Error(data.error || '请求失败');
    if (state.viewVersion !== view) return;
    workerReviewPanel.replaceChildren();
    for (const item of data.requests) {
      const card = document.createElement('div');
      card.className = 'message assistant';
      const title = document.createElement('strong');
      title.textContent =
        '负责人请求权限 · ' +
        item.tool_name +
        ' · ' +
        item.risk_level +
        '（最多等待 ' +
        item.timeout_seconds +
        ' 秒）';
      const detail = document.createElement('pre');
      detail.style.cssText =
        'white-space:pre-wrap;overflow-wrap:anywhere;max-height:20vh;overflow:auto';
      detail.textContent = JSON.stringify(
        {
          任务: item.task_id,
          负责人: item.provider_id,
          说明: item.message,
          工具调用: item.tool_call,
          输入: item.tool_input,
        },
        null,
        2,
      );
      card.append(title, detail);
      for (const approved of [true, false]) {
        const button = document.createElement('button');
        button.className = 'secondary';
        button.textContent = approved ? '仅批准本次' : '拒绝';
        button.onclick = async () => {
          if (state.viewVersion !== view || state.project?.project_id !== project) return;
          for (const b of card.querySelectorAll('button')) b.disabled = true;
          try {
            await api('worker-reviews', {
              project_id: project,
              thread_id: thread,
              request_id: item.request_id,
              approved,
            });
            if (state.viewVersion === view) await refreshWorkerReviews();
          } catch (e) {
            if (state.viewVersion === view) {
              showError(e.message);
              await refreshWorkerReviews();
            }
          }
        };
        card.append(button);
      }
      workerReviewPanel.append(card);
    }
  } catch (e) {
    if (state.viewVersion === view && e.status !== 404)
      showError('无法读取负责人权限请求：' + e.message);
  } finally {
    workerReviewLoading = false;
  }
}
setInterval(refreshWorkerReviews, 4000);
function showError(text) {
  const e = $('#banner'),
    notice = state.ephemeral ? '当前项目状态存储在临时内存中，服务重启后会丢失。' : '',
    dev = state.localDev ? '当前为开发服务，会话和 Run 在异常退出后的恢复不受保证。' : '';
  e.textContent = [notice, dev, text].filter(Boolean).join(' ');
  e.hidden = !e.textContent;
}
function msg(role, text) {
  const e = document.createElement('div');
  e.className = 'message ' + role;
  e.textContent = text;
  $('#messages').append(e);
  $('#messages').scrollTop = $('#messages').scrollHeight;
  return e;
}
async function api(path, body) {
  const r = await fetch(
    '/workbench/api/' + path,
    body === undefined
      ? {}
      : {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        },
  );
  const v = await r.json();
  if (!r.ok) {
    const e = new Error(v.error || '请求失败');
    e.project_id = v.project_id;
    e.status = r.status;
    throw e;
  }
  return v;
}
function paintProjects() {
  const box = $('#projects');
  box.replaceChildren();
  state.projects.forEach((p) => {
    const b = document.createElement('button');
    b.className = 'project' + (state.project?.project_id === p.project_id ? ' active' : '');
    b.title = p.directory || '需要绑定文件夹';
    b.textContent = '▱  ' + p.goal;
    b.onclick = () => openProject(p);
    box.append(b);
  });
}
function paintConversations() {
  const box = $('#conversations');
  box.replaceChildren();
  for (const conversation of state.conversations) {
    const button = document.createElement('button');
    button.className =
      'project' + (state.conversation?.thread_id === conversation.thread_id ? ' active' : '');
    button.textContent = conversation.title || '新会话';
    button.title = conversation.title || '新会话';
    button.onclick = () => openConversation(conversation);
    box.append(button);
  }
}
$('#newConversation').onclick = async () => {
  const project = state.project?.project_id,
    view = state.viewVersion;
  if (!project) return;
  const title = prompt('会话名称（可留空）', '');
  if (title === null) return;
  $('#newConversation').disabled = true;
  try {
    const body = { project_id: project };
    if (title.trim()) body.title = title.trim();
    const conversation = await api('conversations', body);
    if (state.project?.project_id !== project) return;
    state.conversations.push(conversation);
    paintConversations();
    if (state.viewVersion !== view) return;
    await openConversation(conversation);
  } catch (e) {
    if (state.viewVersion === view) showError(e.message);
  } finally {
    if (state.project?.project_id === project) $('#newConversation').disabled = false;
  }
};
async function loadProjects() {
  state.projects = await api('projects');
  paintProjects();
}
function paintModels() {
  const previous = $('#model').value;
  $('#model').replaceChildren(new Option('工作台默认模型', ''));
  state.providers.forEach((p) =>
    (p.models || [])
      .filter((m) => m.enabled)
      .forEach((m) =>
        $('#model').add(
          new Option(
            p.name + ' · ' + m.id,
            JSON.stringify({ provider: p.provider_id, model: m.id }),
          ),
        ),
      ),
  );
  if (previous && ![...$('#model').options].some((o) => o.value === previous))
    $('#model').add(new Option('会话已选模型（不可用或未加载）', previous));
  if ([...$('#model').options].some((o) => o.value === previous)) $('#model').value = previous;
  paintEffort(true);
}
function paintEffort(keepUnknown = false) {
  const s = $('#model').value,
    previous = $('#effort').value;
  $('#effort').replaceChildren(new Option('默认思考等级', ''));
  if (s) {
    const v = JSON.parse(s),
      p = state.providers.find((x) => x.provider_id === v.provider),
      m = p?.models?.find((x) => x.id === v.model);
    (m?.reasoning_levels || []).forEach((e) => $('#effort').add(new Option(e, e)));
    if (keepUnknown && previous && ![...$('#effort').options].some((o) => o.value === previous))
      $('#effort').add(new Option(previous + '（不可用或未加载）', previous));
  }
  if ([...$('#effort').options].some((o) => o.value === previous)) $('#effort').value = previous;
}
async function loadProviders() {
  state.providers = await api('providers');
  paintModels();
}

// A page reload loses the local streaming request, but the official Run keeps
// going. Observe only this conversation and refresh its history when it ends.
async function watchRestoredRun(project, thread, view, previous = null) {
  if (state.viewVersion !== view) return;
  try {
    const result = await api('trace/status?project_id=' + encodeURIComponent(project) +
      '&thread_id=' + encodeURIComponent(thread));
    if (state.viewVersion !== view) return;
    const active = ['pending', 'running'].includes(result.run?.status);
    const changed = previous && previous.id !== result.run?.run_id;
    if (active) {
      state.busy = true;
      $('#send').disabled = true;
    } else if (previous && (previous.active || changed) && state.busyThreads.has(thread)) {
      // Preserve the observation until the original stream has finished.
      setTimeout(() => watchRestoredRun(project, thread, view, previous), 3000);
      return;
    } else if (previous && (previous.active || changed)) {
      state.busy = false;
      await openConversation(state.conversation);
      return;
    }
    setTimeout(() => watchRestoredRun(project, thread, view,
      { id: result.run?.run_id, active }), 3000);
  } catch (e) {
    if (state.viewVersion === view) {
      showError('无法查询会话运行状态：' + e.message);
      setTimeout(() => watchRestoredRun(project, thread, view, previous), 5000);
    }
  }
}
async function loadProject(p, conversation) {
  const view = ++state.viewVersion;
  workerReviewPanel.replaceChildren();
  let loaded = false;
  let baseline = null;
  if (state.conversation) state.drafts.set(state.conversation.thread_id, $('#message').value);
  state.project = p;
  state.conversation = conversation;
  $('#message').value = state.drafts.get(conversation.thread_id) || '';
  state.busy = state.busyThreads.has(conversation.thread_id);
  state.loadingView = true;
  paintProjects();
  paintConversations();
  $('#traceFrame').src = '/workbench/trajectory?project_id=' + encodeURIComponent(p.project_id) +
    '&thread_id=' + encodeURIComponent(conversation.thread_id);
  $('#traceFrame').classList.remove('hidden');
  $('#traceEmpty').classList.add('hidden');
  $('#title').textContent = p.goal;
  $('#projectLabel').textContent = p.directory || '项目未绑定文件夹（点击绑定）';
  $('#message').disabled = false;
  $('#send').disabled = true;
  $('#model').disabled = true;
  $('#effort').disabled = true;
  $('#model').value = '';
  paintEffort();
  $('#welcome').classList.add('hidden');
  $('#messages').classList.remove('hidden');
  $('#messages').replaceChildren();
  showError('项目管理已启用；商讨和开发需要可验证的负责人环境与门禁。');
  try {
    try {
      const status = await api('trace/status?project_id=' + encodeURIComponent(p.project_id) +
        '&thread_id=' + encodeURIComponent(conversation.thread_id));
      baseline = { id: status.run?.run_id, active: ['pending', 'running'].includes(status.run?.status) };
    } catch (_) {
      // History may still be readable; the observer reports and retries failures.
    }
    if (state.viewVersion !== view) return;
    if (baseline?.active && !state.busyThreads.has(conversation.thread_id)) state.busy = true;
    const h = await api(
      'history?project_id=' +
        encodeURIComponent(p.project_id) +
        '&thread_id=' +
        encodeURIComponent(conversation.thread_id),
    );
    if (state.viewVersion !== view) return;
    if (h.memory_model_error) showError('后台记忆暂不可用：' + h.memory_model_error);
    const selection = h.model_selection;
    if (selection) {
      const value = JSON.stringify({ provider: selection.provider_id, model: selection.model_id });
      if (![...$('#model').options].some((o) => o.value === value))
        $('#model').add(new Option('会话已选模型（不可用或未加载）', value));
      $('#model').value = value;
    } else $('#model').value = '';
    paintEffort();
    if (selection?.reasoning_effort) {
      if (![...$('#effort').options].some((o) => o.value === selection.reasoning_effort))
        $('#effort').add(
          new Option(selection.reasoning_effort + '（不可用或未加载）', selection.reasoning_effort),
        );
      $('#effort').value = selection.reasoning_effort;
    }
    for (const m of h.messages || []) {
      if ((m.type === 'human' || m.type === 'ai') && typeof m.content === 'string' && m.content)
        msg(m.type === 'human' ? 'user' : 'assistant', m.content);
    }
    loaded = true;
  } catch (e) {
    if (state.viewVersion === view) showError(e.message + '；请重新打开项目。');
  } finally {
    if (state.viewVersion === view) {
      state.loadingView = !loaded;
      $('#model').disabled = !loaded;
      $('#effort').disabled = !loaded;
      $('#send').disabled = state.busy || state.loadingView || !state.project;
      if (loaded)
        watchRestoredRun(p.project_id, conversation.thread_id, view, baseline);
    }
  }
}
$('#newProject').onclick = () => {
  $('#projectError').textContent = '';
  $('#projectDialog').hidden = false;
};
document
  .querySelectorAll('[data-close]')
  .forEach((b) => (b.onclick = () => ($('#' + b.dataset.close).hidden = true)));
$('#refresh').onclick = () => loadProjects().catch((e) => showError(e.message));
$('#chooseFolder').onclick = async () => {
  try {
    const x = await api('select-folder', {});
    if (x.directory) $('#directory').value = x.directory;
  } catch (e) {
    $('#projectError').textContent = e.message;
  }
};
$('#createProject').onclick = async () => {
  try {
    const p = await api('projects', { directory: $('#directory').value, goal: $('#goal').value });
    $('#projectDialog').hidden = true;
    await loadProjects();
    await openProject(p);
  } catch (e) {
    if (e.project_id && e.status === 409 && confirm('该文件夹已关联项目。打开已有项目？')) {
      await loadProjects();
      const p = state.projects.find((x) => x.project_id === e.project_id);
      if (p) {
        $('#projectDialog').hidden = true;
        await openProject(p);
        return;
      }
    }
    if (e.project_id && e.status === 503) await loadProjects();
    $('#projectError').textContent = e.message;
  }
};
$('#saveProvider').onclick = async () => {
  try {
    await api('providers', {
      provider_id: $('#providerId').value,
      base_url: $('#baseUrl').value,
      token: $('#token').value || undefined,
    });
    $('#token').value = '';
    await loadProviders();
    $('#settingsError').textContent = '已保存';
  } catch (e) {
    $('#settingsError').textContent = e.message;
  }
};

$('#model').onchange = () => paintEffort();
document.querySelectorAll('.tab').forEach(
  (t) =>
    (t.onclick = () => {
      document.querySelectorAll('.tab').forEach((x) => x.classList.remove('active'));
      t.classList.add('active');
      $('#chatView').hidden = t.dataset.tab !== 'chat';
      $('#activityView').hidden = t.dataset.tab !== 'activity';
    }),
);
function selectedModelParams() {
  const value = $('#model').value;
  if (!value) return { model_use_default: true };
  const selected = JSON.parse(value);
  const params = { model_provider: selected.provider, model_name: selected.model };
  if ($('#effort').value) params.reasoning_effort = $('#effort').value;
  return params;
}
async function sendMessage() {
  const input = $('#message'),
    text = input.value.trim();
  if (!text || !state.project || !state.conversation || state.busy || state.loadingView) return;
  const project = state.project.project_id,
    thread = state.conversation.thread_id,
    view = state.viewVersion;
  state.busy = true;
  state.busyThreads.add(thread);
  $('#send').disabled = true;
  input.value = '';
  msg('user', text);
  let response = false;
  try {
    const body = {
      project_id: project,
      thread_id: thread,
      message: text,
      ...selectedModelParams(),
    };
    const r = await fetch('/workbench/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const x = await r.json();
      throw Error(x.error || '请求失败');
    }
    const reader = r.body.getReader(),
      decoder = new TextDecoder();
    let pending = '';
    while (true) {
      const { value, done } = await reader.read();
      pending += decoder.decode(value || new Uint8Array(), { stream: !done });
      const frames = pending.split('\n\n');
      pending = frames.pop();
      for (const frame of frames) {
        if (!frame.startsWith('data: ')) continue;
        const x = JSON.parse(frame.slice(6));
        if (x.event === 'error') throw Error(x.data?.message || '模型请求失败');
        for (const node of Object.values(x.data || {})) {
          if (
            node &&
            typeof node === 'object' &&
            typeof node.pm_memory_model_error === 'string' &&
            state.viewVersion === view
          )
            showError('后台记忆暂不可用：' + node.pm_memory_model_error);
        }
        for (const m of x.data?.model?.messages || []) {
          if (typeof m.content === 'string' && m.content) {
            response = true;
            if (state.viewVersion === view) msg('assistant', m.content);
          }
        }
      }
      if (done) break;
    }
    if (!response && state.viewVersion === view)
      showError('本轮没有可显示的回复，请查看运行状态。');
  } catch (e) {
    if (state.viewVersion === view) showError(e.message);
  } finally {
    state.busyThreads.delete(thread);
    if (state.conversation?.thread_id === thread) {
      state.busy = false;
      $('#send').disabled = !state.project || state.loadingView;
      if (state.viewVersion !== view) showError('上一轮已结束；重新打开会话可查看最新结果。');
    }
  }
}
$('#message').onkeydown = (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    send();
  }
};
Promise.all([
  loadProjects(),
  loadProviders(),
  api('workspace-settings').then((x) => {
    state.ephemeral = x.store_durability === 'ephemeral';
    state.localDev = x.checkpoint_runtime === 'local_dev';
    if (state.ephemeral || state.localDev) showError('');
  }),
]).catch((e) => showError(e.message));
// Settings only display provider metadata; credentials are never fetched back.
function defaultEfforts() {
  const selected = $('#defaultModel').value;
  const old = $('#defaultEffort').value;
  $('#defaultEffort').replaceChildren(new Option('默认', ''));
  if (selected) {
    const s = JSON.parse(selected),
      p = state.providers.find((x) => x.provider_id === s.provider),
      m = p?.models?.find((x) => x.id === s.model);
    (m?.reasoning_levels || []).forEach((x) => $('#defaultEffort').add(new Option(x, x)));
  }
  if ([...$('#defaultEffort').options].some((x) => x.value === old))
    $('#defaultEffort').value = old;
}
async function refreshDefaults() {
  const current = await api('default-model');
  const select = $('#defaultModel');
  select.replaceChildren(new Option('请选择', ''));
  state.providers.forEach((p) =>
    (p.models || [])
      .filter((m) => m.enabled)
      .forEach((m) =>
        select.add(
          new Option(
            p.name + ' · ' + m.id,
            JSON.stringify({ provider: p.provider_id, model: m.id }),
          ),
        ),
      ),
  );
  if (current)
    select.value = JSON.stringify({ provider: current.provider_id, model: current.model_id });
  defaultEfforts();
  if (current?.reasoning_effort) $('#defaultEffort').value = current.reasoning_effort;
}
$('#defaultModel').onchange = defaultEfforts;
async function loadBaseSettings() {
  try {
    await refreshDefaults();
    const c = await api('clef');
    $('#clefAccount').value = c.account_id || '';
    $('#clefAccount').placeholder = c.account_configured
      ? '已配置，留空保持不变'
      : 'Cloudflare Account ID';
  } catch (e) {
    $('#settingsError').textContent = e.message;
  }
}
$('#saveDefault').onclick = async () => {
  try {
    if (!$('#defaultModel').value) throw Error('请选择默认模型');
    const s = JSON.parse($('#defaultModel').value);
    await api('default-model', {
      provider_id: s.provider,
      model_id: s.model,
      reasoning_effort: $('#defaultEffort').value || null,
    });
    $('#settingsError').textContent = '默认模型已保存';
  } catch (e) {
    $('#settingsError').textContent = e.message;
  }
};
$('#saveClef').onclick = async () => {
  try {
    await api('clef', {
      account_id: $('#clefAccount').value,
      token: $('#clefToken').value || undefined,
    });
    $('#clefToken').value = '';
    $('#settingsError').textContent = 'Clef 设置已保存';
  } catch (e) {
    $('#settingsError').textContent = e.message;
  }
};
$('#discover').onclick = async () => {
  try {
    const p = $('#providerId').value;
    state.discovered = (await api('providers/discover', { provider_id: p })).models;
    const previous = state.providers.find((x) => x.provider_id === p)?.models || [];
    const box = $('#modelList');
    box.replaceChildren();
    state.discovered.forEach((id) => {
      const row = document.createElement('div'),
        label = document.createElement('label'),
        check = document.createElement('input'),
        levels = document.createElement('input'),
        old = previous.find((x) => x.id === id);
      row.className = 'field';
      check.type = 'checkbox';
      check.dataset.model = id;
      check.checked = !!old?.enabled;
      levels.placeholder = '思考等级，例如 low, medium, high';
      levels.value = (old?.reasoning_levels || []).join(', ');
      levels.dataset.levels = id;
      label.append(check, document.createTextNode(' ' + id));
      row.append(label, levels);
      box.append(row);
    });
    $('#settingsError').textContent = '请选择启用的模型';
  } catch (e) {
    $('#settingsError').textContent = e.message;
  }
};
$('#saveModels').onclick = async () => {
  try {
    const id = $('#providerId').value,
      p = state.providers.find((x) => x.provider_id === id);
    if (!p) throw Error('请先保存模型服务');
    const checked = [...$('#modelList input[type=checkbox]')];
    if (!checked.length) throw Error('请先获取模型');
    const models = checked.map((c) => {
      const levels = [...$('#modelList input[data-levels]')]
        .find((x) => x.dataset.levels === c.dataset.model)
        .value.split(',')
        .map((x) => x.trim())
        .filter(Boolean);
      return {
        id: c.dataset.model,
        enabled: c.checked,
        reasoning_levels: levels.length ? levels : null,
      };
    });
    await api('providers', { provider_id: id, base_url: p.base_url, name: p.name, models });
    await loadProviders();
    await refreshDefaults();
    $('#settingsError').textContent = '模型选择已保存';
  } catch (e) {
    $('#settingsError').textContent = e.message;
  }
};
$('#projectLabel').onclick = async () => {
  if (!state.project || state.project.directory) return;
  const project = state.project.project_id,
    view = state.viewVersion;
  try {
    const selected = await api('select-folder', {});
    if (!selected.directory || state.viewVersion !== view) return;
    await api('projects/bind', { project_id: project, directory: selected.directory });
    await loadProjects();
    if (state.viewVersion !== view) return;
    const updated = state.projects.find((p) => p.project_id === project);
    if (updated) await openProject(updated);
  } catch (e) {
    if (state.viewVersion === view) showError(e.message);
  }
};
$('#projectLabel').title = '未绑定文件夹的项目可点击此处绑定';
let reconciled = null;
const candidateAgent = document.createElement('input');
candidateAgent.placeholder = '候选负责人会话 ID（首次创建中断时填写）';
candidateAgent.setAttribute('aria-label', '候选负责人会话 ID');
$('#reconcileButton').before(candidateAgent);
const attachButton = document.createElement('button');
attachButton.className = 'secondary hidden';
attachButton.textContent = '关联已找到的 Run';
$('#reconcilePanel').append(attachButton);
$('#reconcileButton').onclick = async () => {
  const out = $('#reconcileResult'),
    task = $('#reconcileTask').value.trim();
  reconciled = null;
  attachButton.classList.add('hidden');
  if (!state.project || !task) {
    out.textContent = '请选择项目并输入任务编号。';
    return;
  }
  const project = state.project.project_id,
    view = state.viewVersion,
    candidate = candidateAgent.value.trim();
  out.textContent = '正在核对官方 Run…';
  try {
    const status = await api(
      'dispatch-status?project_id=' +
        encodeURIComponent(project) +
        '&task_id=' +
        encodeURIComponent(task),
    );
    if (state.viewVersion !== view) return;
    if (!status.intent) {
      out.textContent = '没有派工记录。';
      return;
    }
    if (status.intent.status !== 'pending') {
      out.textContent = '已确认 Run：' + (status.intent.run_id || '未知');
      return;
    }
    const body = { project_id: project, task_id: task };
    if (candidate) body.agent_id = candidate;
    const result = await api('reconcile-dispatch', body);
    if (state.viewVersion !== view) return;
    out.textContent = result.matches.length
      ? '找到关联 Run：' +
        result.matches.map((x) => x.run_id + '（' + x.status + '）').join('，') +
        '。不会自动重派。'
      : '暂未找到可证明关联的 Run；结果仍未知，请勿重复派工。';
    if (result.matches.length === 1) {
      reconciled = {
        project_id: project,
        task_id: task,
        run_id: result.matches[0].run_id,
        agent_id: result.matches[0].agent_id,
      };
      attachButton.classList.remove('hidden');
    }
  } catch (e) {
    if (state.viewVersion === view) out.textContent = e.message;
  }
};
attachButton.onclick = async () => {
  if (!reconciled || state.project?.project_id !== reconciled.project_id) return;
  const view = state.viewVersion,
    entry = reconciled;
  try {
    const result = await api('attach-reconciled-dispatch', entry);
    if (state.viewVersion !== view) return;
    $('#reconcileResult').textContent =
      '已关联官方 Run：' +
      result.run_id +
      '。请在对话中让 PM 恢复任务 ' +
      entry.task_id +
      ' 的负责人会话并领取结果。';
    reconciled = null;
    attachButton.classList.add('hidden');
  } catch (e) {
    if (state.viewVersion === view) $('#reconcileResult').textContent = e.message;
  }
};
$('#usageButton').onclick = async () => {
  const out = $('#usageResult');
  if (!state.project) return;
  const project = state.project.project_id,
    view = state.viewVersion;
  try {
    const data = await api(
      'worker-usage?project_id=' +
        encodeURIComponent(project) +
        '&thread_id=' +
        encodeURIComponent(state.conversation?.thread_id || ''),
    );
    if (state.viewVersion !== view) return;
    out.textContent = data.workers.length
      ? data.workers
          .map((w) => {
            const u = w.context_usage;
            return (
              (w.role || w.agent_id) +
              '：' +
              (u
                ? '已用 ' +
                  u.used +
                  ' / 容量 ' +
                  u.size +
                  '，剩余 ' +
                  (u.remaining ?? '未知') +
                  (u.cost ? '；费用 ' + u.cost.amount + ' ' + u.cost.currency : '') +
                  '；来源 ' +
                  u.source +
                  '；时间 ' +
                  u.observed_at
                : '用量未知') +
              '；压缩情况 ' +
              (w.compaction_visibility === 'unknown' ? '未知' : w.compaction_visibility)
            );
          })
          .join('\n')
      : '当前没有已关联的负责人会话。';
  } catch (e) {
    if (state.viewVersion === view) out.textContent = e.message;
  }
};
function resetProjectControls() {
  $('#reconcilePanel').classList.remove('hidden');
  $('#reconcileResult').textContent = '';
  $('#usageResult').textContent = '';
  candidateAgent.value = '';
  taskStatusResult.textContent = '';
  workerRecovery.replaceChildren();
  humanPanel.replaceChildren();
  discussionPanel.textContent = '';
  attachButton.classList.add('hidden');
  reconciled = null;
}
const taskStatusButton = document.createElement('button');
taskStatusButton.className = 'secondary';
taskStatusButton.textContent = '查看任务与门禁';
const taskStatusResult = document.createElement('div');
taskStatusResult.className = 'notice';
taskStatusResult.style.whiteSpace = 'pre-wrap';
const workerRecovery = document.createElement('div');
$('#reconcilePanel').append(taskStatusButton, taskStatusResult, workerRecovery);
taskStatusButton.onclick = async () => {
  if (!state.project) return;
  const project = state.project.project_id,
    view = state.viewVersion;
  taskStatusResult.textContent = '正在读取项目状态…';
  workerRecovery.replaceChildren();
  try {
    const data = await api('project-status?project_id=' + encodeURIComponent(project));
    if (state.viewVersion !== view) return;
    taskStatusResult.textContent = data.tasks.length
      ? data.tasks
          .map((t) => {
            const d = t.discussion,
              r = t.independent_codex_review;
            return (
              t.task_id +
              ' · ' +
              t.title +
              ' · ' +
              t.status +
              '\n负责人：' +
              (t.owner_role || '未知') +
              '；商讨判断：' +
              (d
                ? d.decision === 'not_recorded'
                  ? '尚未判断'
                  : d.decision === 'pass'
                    ? '通过（' + (d.current_pass ? '当前有效' : '已过期') + '）'
                    : d.decision === 'reject'
                      ? '最近判断未通过'
                      : '最近判断信息不足'
                : '尚未记录') +
              '；派工：' +
              (t.dispatch?.status || '尚未记录') +
              '；验收：' +
              (t.validation?.validation_state || '尚无当前验收状态') +
              '；人工决定：' +
              (t.validation?.approval_state || 'not_recorded') +
              '；独立 Codex 审查：' +
              (r?.status || '未知') +
              (r?.summary ? '（' + r.summary + '）' : '')
            );
          })
          .join('\n\n')
      : '当前项目没有任务。';
    (data.unconfirmed_workers || []).forEach((w) => {
      const button = document.createElement('button');
      button.className = 'secondary';
      const observations = { starting: '正在启动', assigned: '已分配', running: '运行中', stop_unknown: '停止未知', unknown: '状态未知' };
      button.textContent = '停止并核对负责人 · ' + (w.task_id || w.agent_id) + '（最近记录：' + (observations[w.last_job_state] || '未知') + '）';
      button.onclick = async () => {
        try {
          await api('stop-unconfirmed-worker', { project_id: project, agent_id: w.agent_id });
          if (state.viewVersion !== view) return;
          button.textContent = '已确认停止';
          button.disabled = true;
        } catch (e) {
          if (state.viewVersion === view)
            taskStatusResult.textContent = e.message + ' 服务重启后可能无法确认原进程树；重复核对不一定能恢复，请先检查原 Worker 进程。';
        }
      };
      workerRecovery.append(button);
    });
  } catch (e) {
    if (state.viewVersion === view) taskStatusResult.textContent = e.message;
  }
};
const workerSection = document.createElement('section');
workerSection.innerHTML =
  '<h2>开发负责人</h2><label class="field"><input id="workerEnabled" type="checkbox" style="display:inline;width:auto"> 启用 Claude 负责人</label><label class="field">Node 可执行文件（可选）<input id="workerNode" placeholder="node"></label><label class="field">负责人 API 地址<input id="workerEndpoint" placeholder="https://openrouter.ai/api"></label><label class="field">负责人模型<input id="workerModel" placeholder="模型名称"></label><label class="field">ACP 适配器目录<input id="workerRuntime" placeholder="本机绝对路径"></label><label class="field">负责人 API 凭据<input id="workerToken" type="password" autocomplete="off" placeholder="留空保留已有凭据"></label><div class="notice">PM 专属服务的凭据保存在 .env；保存后重启服务加载。</div><button class="secondary" id="saveWorker">保存负责人设置</button><div id="workerStatus" class="notice"></div>';
$('#settingsError').before(workerSection);
async function loadWorkerSettings() {
  try {
    const data = await api('worker-settings'),
      v = data.settings,
      custom = data.source === 'custom';
    $('#workerEnabled').checked = custom || !!v.enabled;
    $('#workerEnabled').disabled = custom;
    $('#saveWorker').disabled = custom;
    $('#workerNode').value = v.node || '';
    $('#workerEndpoint').value = v.endpoint || '';
    $('#workerModel').value = v.model || '';
    $('#workerRuntime').value = v.runtime_dir || '';
    $('#workerToken').value = '';
    $('#workerStatus').textContent = custom
      ? '此负责人由下方 PM 运行设置中的高级 Worker 配置管理；实际连接状态以运行情况为准。'
      : data.restart_required
        ? '设置已更改，重启服务后生效。'
        : data.available
          ? '负责人已连接。'
          : data.error || '负责人当前未启用或不可用。';
  } catch (e) {
    $('#workerStatus').textContent = e.message;
  }
}
$('#saveWorker').onclick = async () => {
  try {
    const enabled = $('#workerEnabled').checked;
    const body = {
      enabled,
      node: $('#workerNode').value,
      endpoint: $('#workerEndpoint').value,
      model: $('#workerModel').value,
      runtime_dir: $('#workerRuntime').value,
    };
    if (enabled && $('#workerToken').value) body.token = $('#workerToken').value;
    await api('worker-settings', body);
    $('#workerToken').value = '';
    $('#workerStatus').textContent = '已保存；重启服务后加载设置并检查只读环境。';
  } catch (e) {
    $('#workerStatus').textContent = e.message;
  }
};
const reviewerSection = document.createElement('section');
reviewerSection.innerHTML =
  '<h2>独立 Codex 审查</h2><label class="field"><input id="reviewerEnabled" type="checkbox" style="display:inline;width:auto"> 启用 Codex 审查者</label><label class="field">Node 可执行文件（可选）<input id="reviewerNode" placeholder="node"></label><label class="field">Codex Responses API 地址<input id="reviewerEndpoint" placeholder="https://api.openai.com/v1"></label><label class="field">审查模型<input id="reviewerModel" placeholder="gpt-6-luna"></label><label class="field">ACP 适配器目录<input id="reviewerRuntime" placeholder="本机绝对路径"></label><label class="field">API 凭据<input id="reviewerToken" type="password" autocomplete="off" placeholder="留空保留已有凭据"></label><div class="notice">审查使用单独的只读 Codex 会话；所选服务需要支持 Responses API。保存后重启服务。</div><button class="secondary" id="saveReviewer">保存审查设置</button><div id="reviewerStatus" class="notice"></div>';
$('#settingsError').before(reviewerSection);
async function loadReviewerSettings() {
  try {
    const data = await api('worker-settings?provider_id=codex'),
      v = data.settings,
      custom = data.source === 'custom';
    $('#reviewerEnabled').checked = custom || !!v.enabled;
    $('#reviewerEnabled').disabled = custom;
    $('#saveReviewer').disabled = custom;
    $('#reviewerNode').value = v.node || '';
    $('#reviewerEndpoint').value = v.endpoint || '';
    $('#reviewerModel').value = v.model || '';
    $('#reviewerRuntime').value = v.runtime_dir || '';
    $('#reviewerToken').value = '';
    $('#reviewerStatus').textContent = custom
      ? '此审查者由下方 PM 运行设置中的高级 Worker 配置管理；实际连接状态以运行情况为准。'
      : data.restart_required
        ? '设置已更改，重启服务后生效。'
        : data.available
          ? '审查者已连接。'
          : data.error || '审查者当前未启用或不可用。';
  } catch (e) {
    $('#reviewerStatus').textContent = e.message;
  }
}
$('#saveReviewer').onclick = async () => {
  try {
    const enabled = $('#reviewerEnabled').checked;
    const body = {
      provider_id: 'codex',
      enabled,
      node: $('#reviewerNode').value,
      endpoint: $('#reviewerEndpoint').value,
      model: $('#reviewerModel').value,
      runtime_dir: $('#reviewerRuntime').value,
    };
    if (enabled && $('#reviewerToken').value) body.token = $('#reviewerToken').value;
    await api('worker-settings', body);
    $('#reviewerToken').value = '';
    $('#reviewerStatus').textContent = '已保存；重启服务后加载设置并检查只读环境。';
  } catch (e) {
    $('#reviewerStatus').textContent = e.message;
  }
};
const humanButton = document.createElement('button');
humanButton.className = 'secondary';
humanButton.textContent = '人工验收与批准';
const humanPanel = document.createElement('div');
humanPanel.className = 'notice';
$('#reconcilePanel').append(humanButton, humanPanel);
const modeSection = document.createElement('section');
modeSection.innerHTML =
  '<h2>商讨模式</h2><label class="field">Worker<select id="modeProvider"><option value="claude">Claude 负责人</option><option value="codex">Codex 审查者</option></select></label><label class="field">项目默认模式<select id="modeChoice"></select></label><div class="notice" id="modeStatus"></div><div class="notice">保存的是目标模式；每次商讨会记录适配器实际回报的模式。所选模式可能允许写入，商讨仍不能替代双方确认与 Clef 开发门禁。</div><div class="notice" id="modeWriteWarning"></div><button class="secondary" id="saveMode">保存项目默认模式</button>';
$('#settingsError').before(modeSection);
const loadModes = async () => {
  const project = state.project?.project_id,
    provider = $('#modeProvider').value,
    choice = $('#modeChoice'),
    status = $('#modeStatus');
  choice.replaceChildren();
  status.textContent = '';
  $('#modeWriteWarning').textContent = '';
  if (!project) {
    status.textContent = '请先选择项目。';
    return;
  }
  try {
    const data = await api(
      'worker-modes?project_id=' +
        encodeURIComponent(project) +
        '&provider_id=' +
        encodeURIComponent(provider),
    );
    const d = data.discovery || {};
    state.modeDiscovery = d;
    (d.modes || []).forEach((m) => {
      const o = document.createElement('option');
      o.value = m.id;
      o.textContent = m.name + (m.description ? '（' + m.description + '）' : '');
      choice.append(o);
    });
    if (data.saved_mode && !(d.modes || []).some((m) => m.id === data.saved_mode)) {
      const o = document.createElement('option');
      o.value = data.saved_mode;
      o.textContent = data.saved_mode + '（已保存，适配器未列出）';
      choice.append(o);
    }
    if (data.saved_mode) choice.value = data.saved_mode;
    if (data.saved_mode && data.saved_mode_can_write) {
      $('#modeWriteWarning').textContent =
        '注意：已保存的模式 ' +
        data.saved_mode +
        ' 允许修改项目。商讨不会因此获得写入授权，开发仍需双方确认与 Clef 判断通过。';
    } else {
      $('#modeWriteWarning').textContent = '';
    }
    if (!d.available) {
      status.textContent = '适配器未连接或未提供模式列表，当前无法选择；已保存的默认模式仍会保留。';
    } else {
      status.textContent = '适配器当前报告模式：' + (d.current || '未知');
    }
  } catch (e) {
    status.textContent = e.message;
  }
};
$('#modeChoice').onchange = () => {
  const choice = $('#modeChoice'),
    d = state.modeDiscovery || {};
  const writable = (d.writable || []).includes(choice.value);
  $('#modeWriteWarning').textContent = writable
    ? '注意：模式 ' +
      choice.value +
      ' 允许修改项目；商讨不会因此获得写入授权，开发仍需双方确认与 Clef 判断通过。'
    : '';
};
$('#modeProvider').onchange = loadModes;
$('#saveMode').onclick = async () => {
  const project = state.project?.project_id,
    provider = $('#modeProvider').value,
    mode = $('#modeChoice').value;
  if (!project) return ($('#modeStatus').textContent = '请先选择项目。');
  if (!mode) return ($('#modeStatus').textContent = '没有可保存的模式。');
  try {
    await api('worker-modes', { project_id: project, provider_id: provider, mode_id: mode });
    $('#modeStatus').textContent = '已保存目标模式：' + mode + '。修改默认值不会改写已有商讨记录。';
  } catch (e) {
    $('#modeStatus').textContent = e.message;
  }
};
const discussionButton = document.createElement('button');
discussionButton.className = 'secondary';
discussionButton.textContent = '查看商讨记录';
const discussionPanel = document.createElement('div');
discussionPanel.className = 'notice';
discussionPanel.style.whiteSpace = 'pre-wrap';
$('#reconcilePanel').append(discussionButton, discussionPanel);
discussionButton.onclick = async () => {
  const taskId = $('#reconcileTask').value.trim(),
    project = state.project?.project_id,
    view = state.viewVersion;
  if (!project || !taskId) {
    discussionPanel.textContent = '请先选择项目并输入任务编号。';
    return;
  }
  try {
    const data = await api('project-status?project_id=' + encodeURIComponent(project));
    if (state.viewVersion !== view) return;
    const task = data.tasks.find((x) => x.task_id === taskId),
      d = task?.discussion;
    if (!task) throw Error('任务不存在');
    if (!d) {
      discussionPanel.textContent = '当前尚无已关联的商讨记录。';
      return;
    }
    discussionPanel.textContent =
      'PM 确认：' +
      (d.pm_confirmation || '未知') +
      '\n' +
      (d.turns || [])
        .map((turn, i) =>
          turn.unavailable
            ? '第 ' + (i + 1) + ' 轮来源不可用'
            : '第 ' +
              (i + 1) +
              ' 轮（' +
              (turn.status === 'not_received' ? '结果尚未领取' : turn.status || '未知') +
              (turn.included_in_decision ? '，已纳入本次判断' : '，未纳入当前判断') +
              '）\nPM：' +
              (turn.question || '未知') +
              '\n负责人：' +
              (turn.result || '未知'),
        )
        .join('\n\n') +
      '\n\n模式：目标 ' +
      (d.mode_record?.requested_mode || '未记录') +
      '；适配器报告 ' +
      (d.mode_record?.reported_mode || '未确认') +
      (d.mode_record?.reported_mode_source
        ? '（' + d.mode_record.reported_mode_source + '）'
        : '') +
      '\n\n指纹范围：' +
      (d.content_digest_scope || '未记录（旧结论未声明范围，请重新商讨后核对）') +
      '\n\nClef：' +
      (d.decision === 'not_recorded' ? '尚未判断' : d.decision) +
      ' ' +
      JSON.stringify(d.answer || {});
  } catch (e) {
    if (state.viewVersion === view) discussionPanel.textContent = e.message;
  }
};
humanButton.onclick = async () => {
  const taskId = $('#reconcileTask').value.trim(),
    project = state.project?.project_id,
    view = state.viewVersion;
  humanPanel.replaceChildren();
  if (!project || !taskId) {
    humanPanel.textContent = '请先选择项目并输入任务编号。';
    return;
  }
  try {
    const data = await api('project-status?project_id=' + encodeURIComponent(project));
    if (state.viewVersion !== view) return;
    const task = data.tasks.find((x) => x.task_id === taskId);
    if (!task) throw Error('任务不存在');
    const title = document.createElement('p');
    title.textContent = task.title + '：勾选您确认通过的验收标准，未勾选项将记录为未通过。';
    humanPanel.append(title);
    const checks = task.acceptance_criteria.map((criterion) => {
      const label = document.createElement('label'),
        check = document.createElement('input');
      check.type = 'checkbox';
      label.style.display = 'block';
      label.append(check, document.createTextNode(' ' + criterion));
      humanPanel.append(label);
      return { criterion, check };
    });
    const validate = document.createElement('button');
    validate.className = 'secondary';
    validate.textContent = '记录逐项验收';
    validate.onclick = async () => {
      if (state.viewVersion !== view) return;
      try {
        const criteria = Object.fromEntries(checks.map((x) => [x.criterion, x.check.checked]));
        const result = await api('human-validation', {
          project_id: project,
          task_id: taskId,
          criteria,
        });
        if (state.viewVersion === view)
          taskStatusResult.textContent =
            result.status === 'cancelled' ? '已取消验收。' : '验收结果已记录。';
      } catch (e) {
        if (state.viewVersion === view) taskStatusResult.textContent = e.message;
      }
    };
    humanPanel.append(validate);
    for (const approved of [true, false]) {
      const button = document.createElement('button');
      button.className = 'secondary';
      button.textContent = approved ? '批准当前交付' : '拒绝当前交付';
      button.onclick = async () => {
        if (state.viewVersion !== view) return;
        try {
          const result = await api('human-approval', {
            project_id: project,
            task_id: taskId,
            approved,
          });
          if (state.viewVersion === view)
            taskStatusResult.textContent =
              result.status === 'cancelled'
                ? '已取消人工决定。'
                : approved
                  ? '人工批准已记录。'
                  : '人工拒绝已记录。';
        } catch (e) {
          if (state.viewVersion === view) taskStatusResult.textContent = e.message;
        }
      };
      humanPanel.append(button);
    }
  } catch (e) {
    if (state.viewVersion === view) humanPanel.textContent = e.message;
  }
};
const workspaceSection = document.createElement('section');
workspaceSection.innerHTML =
  '<h2>验收与轨迹</h2><div class="notice">验收和批准由本机用户分别确认，不能关闭。</div><label class="field"><input id="traceEnabled" type="checkbox" style="display:inline;width:auto"> 记录后续运行轨迹</label><label class="field">PM 压缩启动阈值（token，默认 300000）<input id="compactionThreshold" type="number" min="1000"></label><label class="field">PM 上下文上限（token，默认 500000）<input id="contextLimit" type="number" min="1000"></label><div class="notice">阈值留空时沿用已保存值或默认值；保存后重启服务生效；已有轨迹保留。</div><button class="secondary" id="saveWorkspace">保存轨迹与压缩设置</button><div id="workspaceStatus" class="notice"></div>';
$('#settingsError').before(workspaceSection);
async function loadWorkspaceSettings() {
  try {
    const value = await api('workspace-settings');
    $('#traceEnabled').checked = value.trace_enabled;
    $('#compactionThreshold').value = value.compaction_token_threshold ?? '';
    $('#contextLimit').value = value.context_token_limit ?? '';
  } catch (e) {
    $('#workspaceStatus').textContent = e.message;
  }
}
$('#saveWorkspace').onclick = async () => {
  try {
    const payload = { trace_enabled: $('#traceEnabled').checked };
    const ct = $('#compactionThreshold').value.trim();
    const cl = $('#contextLimit').value.trim();
    if (ct) payload.compaction_token_threshold = parseInt(ct, 10);
    if (cl) payload.context_token_limit = parseInt(cl, 10);
    await api('workspace-settings', payload);
    $('#workspaceStatus').textContent = '已保存；重启服务后生效。';
  } catch (e) {
    $('#workspaceStatus').textContent = e.message;
  }
};
let memoryJournal = null;
const memoryCheck = document.createElement('button'),
  memoryFix = document.createElement('button'),
  memoryStatus = document.createElement('div');
memoryCheck.className = memoryFix.className = 'secondary';
memoryCheck.textContent = '核对记忆写回';
memoryFix.textContent = '解除未发生写入的阻断';
memoryStatus.className = 'notice';
memoryStatus.style.whiteSpace = 'pre-wrap';
$('#reconcilePanel').append(memoryCheck, memoryFix, memoryStatus);
async function memoryRecovery(reconcile) {
  const project = state.project?.project_id,
    view = state.viewVersion;
  if (!project) return;
  try {
    const path = 'memory-recovery?project_id=' + encodeURIComponent(project),
      data = await api(path, ...(reconcile ? [{}] : []));
    if (state.viewVersion !== view) return;
    memoryJournal =
      !reconcile && data.journal_digest ? { project, digest: data.journal_digest } : null;
    memoryStatus.textContent =
      (data.reconciled ? '已核对并恢复：' : '核对状态：') +
      data.status +
      '\n来源：' +
      (data.source_id || '无') +
      '\n' +
      data.files
        .map(
          (x) =>
            x.path +
            '：' +
            x.state +
            '\n原值 ' +
            (x.before_hash || '无') +
            '\n现值 ' +
            (x.current_hash || '无') +
            '\n预期 ' +
            (x.expected_hash || '无') +
            '\n索引 ' +
            (x.index_hash || '无'),
        )
        .join('\n\n') +
      (data.status === 'uncertain' || data.status === 'files_match_expected_but_unresolved'
        ? '\n已发生写入或证据不完整，仍处于阻断状态，需要人工检查。'
        : '');
  } catch (e) {
    if (state.viewVersion === view) memoryStatus.textContent = e.message;
  }
}
memoryCheck.onclick = () => memoryRecovery(false);
memoryFix.onclick = () => memoryRecovery(true);
let pendingCard = null;
function clearPending() {
  if (pendingCard) {
    pendingCard.remove();
    pendingCard = null;
  }
}
async function checkPending() {
  const project = state.project,
    thread = state.conversation?.thread_id,
    view = state.viewVersion;
  if (!project || !thread) return;
  let d;
  try {
    d = await api(
      'pending-decision?project_id=' +
        encodeURIComponent(project.project_id) +
        '&thread_id=' +
        encodeURIComponent(thread),
    );
  } catch (e) {
    if (state.viewVersion === view) {
      clearPending();
      showError(e.message);
    }
    return;
  }
  if (state.viewVersion !== view) return;
  clearPending();
  if (!d.pending) return;
  if (!d.answerable || !d.decisions.length) {
    const n = document.createElement('div');
    n.className = 'message assistant';
    n.textContent = '⏸ 会话上有未完成的中断，但不是可在本界面答复的决策形式；请查看运行轨迹。';
    $('#messages').append(n);
    pendingCard = n;
    return;
  }
  const fingerprint = d.fingerprint;
  const binding = { interrupt_id: d.interrupt_id || null, checkpoint_id: d.checkpoint_id || null };
  const card = document.createElement('div');
  card.className = 'message assistant';
  const head = document.createElement('div');
  head.style.fontWeight = '650';
  head.textContent =
    '⏸ 待人工决策' +
    (d.decisions.length > 1 ? '（共 ' + d.decisions.length + ' 项，按顺序答复）' : '') +
    (d.binding_available ? '' : '（缺少可核对的检查点，当前无法提交）');
  card.append(head);
  const boxes = [];
  d.decisions.forEach((a, i) => {
    if (d.decisions.length > 1) {
      const t = document.createElement('div');
      t.style.fontWeight = '600';
      t.textContent = '第 ' + (i + 1) + ' 项 · ' + (a.name || '未命名动作');
      card.append(t);
    }
    const q =
      (a.args && typeof a.args.question === 'string' && a.args.question) ||
      a.description ||
      '（未提供问题文本）';
    card.append(Object.assign(document.createElement('div'), { textContent: q }));
    const ctx = (a.args && typeof a.args.context === 'string' && a.args.context) || '';
    if (ctx)
      card.append(
        Object.assign(document.createElement('div'), {
          className: 'notice',
          textContent: '背景：' + ctx,
        }),
      );
    const box = document.createElement('textarea');
    box.setAttribute('aria-label', '决策答复 ' + (i + 1));
    box.placeholder = '输入你的决定…';
    box.style.cssText =
      'width:100%;min-height:60px;border:1px solid #d6dce4;border-radius:7px;padding:8px;margin-top:8px;font:inherit';
    boxes.push(box);
    card.append(box);
  });
  const submit = document.createElement('button');
  submit.className = 'primary';
  submit.textContent = '提交决策并恢复运行';
  if (!d.binding_available) submit.disabled = true;
  submit.style.marginTop = '8px';
  const status = document.createElement('div');
  status.className = 'notice';
  status.textContent = '提交普通聊天消息不会恢复该决策；恢复只能在这里进行。';
  submit.onclick = async () => {
    if (state.viewVersion !== view || state.busyThreads.has(thread)) return;
    const answers = boxes.map((b) => b.value.trim());
    if (answers.some((x) => !x)) {
      status.textContent = '请先填写全部答复。';
      return;
    }
    submit.disabled = true;
    boxes.forEach((b) => (b.disabled = true));
    state.busy = true;
    state.busyThreads.add(thread);
    $('#send').disabled = true;
    try {
      const body = {
        project_id: project.project_id,
        thread_id: thread,
        answers,
        fingerprint,
        ...binding,
        ...selectedModelParams(),
      };
      await api('submit-decision', body);
      if (state.viewVersion !== view) return;
      status.textContent = '决策已提交，PM 恢复运行已启动；稍后重新打开项目可看到 PM 的后续回复。';
    } catch (e) {
      if (state.viewVersion === view) {
        status.textContent = e.message;
        submit.disabled = false;
        boxes.forEach((b) => (b.disabled = false));
      }
    } finally {
      state.busyThreads.delete(thread);
      if (state.conversation?.thread_id === thread) {
        state.busy = false;
        $('#send').disabled = !state.project || state.loadingView;
      }
    }
  };
  card.append(submit, status);
  $('#messages').append(card);
  pendingCard = card;
  $('#messages').scrollTop = $('#messages').scrollHeight;
}
async function openProject(p) {
  const view = ++state.viewVersion;
  resetProjectControls();
  workerReviewPanel.replaceChildren();
  clearPending();
  if (state.conversation) state.drafts.set(state.conversation.thread_id, $('#message').value);
  $('#message').value = '';
  state.project = p;
  state.conversation = null;
  state.conversations = [];
  state.loadingView = true;
  $('#message').disabled = true;
  $('#send').disabled = true;
  $('#model').disabled = true;
  $('#effort').disabled = true;
  $('#newConversation').disabled = true;
  $('#messages').replaceChildren();
  paintProjects();
  paintConversations();
  try {
    const result = await api('conversations?project_id=' + encodeURIComponent(p.project_id));
    if (state.viewVersion !== view) return;
    state.conversations = result.conversations;
    if (!state.conversations.length) {
      const conversation = await api('conversations', { project_id: p.project_id });
      if (state.viewVersion !== view) return;
      state.conversations.push(conversation);
    }
    $('#newConversation').disabled = false;
    await openConversation(state.conversations[0]);
  } catch (e) {
    if (state.viewVersion === view) showError('无法加载项目会话：' + e.message);
  }
}
async function openConversation(conversation) {
  const project = state.project;
  if (!project) return;
  clearPending();
  await loadProject(project, conversation);
  if (state.conversation?.thread_id === conversation.thread_id && !state.loadingView) {
    await checkPending();
    await refreshWorkerReviews();
  }
}
async function send() {
  const view = state.viewVersion;
  await sendMessage();
  if (state.viewVersion === view) await checkPending();
}
$('#send').onclick = send;
const memoryRollback = document.createElement('button');
memoryRollback.className = 'secondary';
memoryRollback.textContent = '撤销未完成的记忆写回';
$('#reconcilePanel').append(memoryRollback);
memoryRollback.onclick = async () => {
  const project = state.project?.project_id,
    view = state.viewVersion;
  if (!project) return;
  if (!memoryJournal || memoryJournal.project !== project) {
    memoryStatus.textContent = '请先核对当前项目的记忆写回记录。';
    return;
  }
  try {
    const data = await api(
      'memory-recovery?project_id=' + encodeURIComponent(project) + '&action=rollback',
      { journal_digest: memoryJournal.digest },
    );
    if (state.viewVersion === view) {
      memoryStatus.textContent =
        data.status === 'cancelled'
          ? '已取消。'
          : data.status === 'rolled_back'
            ? '已恢复到写回前状态；可重新提交记忆任务。'
            : '当前没有待处理的写回。';
      if (data.status !== 'cancelled') memoryJournal = null;
    }
  } catch (e) {
    if (state.viewVersion === view) memoryStatus.textContent = e.message;
  }
};

// PM runtime settings are displayed separately from project and conversation data.
const runtimeSection = document.createElement('section');
runtimeSection.hidden = true;
runtimeSection.innerHTML = `
  <h2>PM 运行设置</h2>
  <div class="notice">普通设置保存在 settings.json；数据库连接和凭据保存在 .env。此处修改后重启 PM 服务生效。</div>
  <label class="field">PostgreSQL 连接地址<input id="pmDatabase" type="password" autocomplete="off" placeholder="留空保持已有连接"></label>
  <div id="pmDatabaseState" class="notice"></div>
  <label class="field"><input id="pmMemory" type="checkbox" style="display:inline;width:auto"> 启用项目记忆</label>
  <label class="field"><input id="pmTrajectory" type="checkbox" style="display:inline;width:auto"> 启用轨迹中间件</label>
  <label class="field"><input id="pmAcp" type="checkbox" style="display:inline;width:auto"> 启用 Worker 协作</label>
  <label class="field">PM 服务端口<input id="pmPort" type="number" min="1024" max="65535"></label>
  <label class="field">并行运行数<input id="pmJobs" type="number" min="2" max="128"></label>
  <label class="field">完成领取轮询间隔（秒）<input id="pmPoll" type="number" min="0.5" max="300" step="0.5"></label>
  <label class="field">PM 数据目录（可选）<input id="pmDataDir"></label>
  <label class="field">独立轨迹目录（可选）<input id="pmTraceDir"></label>
  <label class="field"><input id="pmTraceStrict" type="checkbox" style="display:inline;width:auto"> 轨迹记录失败时停止本次运行</label>
  <label class="field">额外 Worker 配置（JSON 数组）<textarea id="pmExtraWorkers" rows="4"></textarea></label>
  <label class="field">PM 子代理角色配置（JSON 数组）<textarea id="pmExtraAgents" rows="4"></textarea></label>
  <div class="notice">Worker 的 env 值保存后转入 .env；再次打开时只显示环境变量引用。</div>
  <button class="secondary" id="savePmRuntime">保存 PM 运行设置</button>
  <div id="pmRuntimeStatus" class="notice"></div>`;
$('#settingsError').before(runtimeSection);

async function loadRuntimeSettings() {
  try {
    const data = await api('runtime-settings');
    runtimeSection.hidden = false;
    $('#pmDatabase').value = '';
    $('#pmDatabaseState').textContent = data.database.store_ready
      ? '数据库已连接。'
      : '数据库未连接；请填写连接地址、保存并重启 PM 服务。';
    $('#pmMemory').checked = data.middleware.memory;
    $('#pmTrajectory').checked = data.middleware.trajectory;
    $('#pmAcp').checked = data.middleware.acp;
    $('#pmPort').value = data.port;
    $('#pmJobs').value = data.n_jobs_per_worker;
    $('#pmPoll').value = data.claim_poll_seconds;
    $('#pmDataDir').value = data.data_dir || '';
    $('#pmTraceDir').value = data.trajectory_directory || '';
    $('#pmTraceStrict').checked = data.trajectory_stop_on_error;
    $('#pmExtraWorkers').value = JSON.stringify(data.acp_workers || [], null, 2);
    $('#pmExtraAgents').value = JSON.stringify(data.acp_subagents || [], null, 2);
    $('#pmRuntimeStatus').textContent = '';
  } catch (e) {
    runtimeSection.hidden = e.status === 404;
    if (!runtimeSection.hidden) $('#pmRuntimeStatus').textContent = e.message;
  }
}

$('#savePmRuntime').onclick = async () => {
  try {
    const body = {
      middleware: {
        memory: $('#pmMemory').checked,
        trajectory: $('#pmTrajectory').checked,
        acp: $('#pmAcp').checked,
      },
      port: Number($('#pmPort').value),
      n_jobs_per_worker: Number($('#pmJobs').value),
      claim_poll_seconds: Number($('#pmPoll').value),
      data_dir: $('#pmDataDir').value.trim(),
      trajectory_directory: $('#pmTraceDir').value.trim(),
      trajectory_stop_on_error: $('#pmTraceStrict').checked,
      acp_workers: JSON.parse($('#pmExtraWorkers').value || '[]'),
      acp_subagents: JSON.parse($('#pmExtraAgents').value || '[]'),
    };
    if ($('#pmDatabase').value) body.database_uri = $('#pmDatabase').value;
    await api('runtime-settings', body);
    $('#pmDatabase').value = '';
    await loadRuntimeSettings();
    $('#pmRuntimeStatus').textContent = '已保存；重启 PM 服务后生效。';
  } catch (e) {
    $('#pmRuntimeStatus').textContent = e.message;
  }
};

// One entry point opens all settings; each section reports its own load error.
$('#settingsButton').onclick = async () => {
  $('#settingsError').textContent = '';
  $('#settingsDialog').hidden = false;
  await loadRuntimeSettings();
  await Promise.all([
    loadBaseSettings(),
    loadWorkerSettings(),
    loadReviewerSettings(),
    loadWorkspaceSettings(),
    loadModes(),
  ]);
};
