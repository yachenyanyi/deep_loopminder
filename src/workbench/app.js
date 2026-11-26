import { $, api, showError, state, setRunning } from './modules/state.js';
import { initSidebar, loadProjects, paintProjects, browseProjectSessions, setSidebarOpen } from './modules/sidebar.js';
import { checkPending, clearPending, initChat, msg, paintDiscussionMode, refreshWorkerReviews, sendMessage } from './modules/chat.js';
import { initTasks, loadTasks } from './modules/tasks.js';
import { initSettings, loadProviders } from './modules/settings.js';

// Kept in this module for the existing isolated Node observer contract.
async function watchRestoredRun(project, thread, view, previous = null) {
  const observerKey = project + ':' + thread + ':' + view;
  if (state.restoredObservers?.has(observerKey) && !previous) return;
  state.restoredObservers?.add(observerKey);
  if (state.viewVersion !== view) {
    state.restoredObservers?.delete(observerKey);
    return;
  }
  try {
    const result = await api('trace/status?project_id=' + encodeURIComponent(project) + '&thread_id=' + encodeURIComponent(thread));
    if (state.viewVersion !== view) {
      state.restoredObservers?.delete(observerKey);
      return;
    }
    const active = ['pending', 'running'].includes(result.run?.status);
    state.restoredBaselines?.set(thread, { id: result.run?.run_id, active });
    const changed = previous && previous.id !== result.run?.run_id;
    if (active) {
      state.busy = true;
      $('#send').disabled = true;
      state.actions?.paintDiscussionMode?.();
    } else if (previous && (previous.active || changed) && state.busyThreads.has(thread)) {
      setTimeout(() => watchRestoredRun(project, thread, view, previous), 3000);
      return;
    } else if (previous && (previous.active || changed)) {
      state.busy = false;
      state.restoredObservers?.delete(observerKey);
      state.restoredBaselines?.delete(thread);
      await openConversation(state.conversation);
      return;
    }
    setTimeout(() => watchRestoredRun(project, thread, view, { id: result.run?.run_id, active }), 3000);
  } catch (e) {
    if (state.viewVersion === view) {
      showError('无法查询会话运行状态：' + e.message);
      setTimeout(() => watchRestoredRun(project, thread, view, previous), 5000);
    }
  }
}

function resetProjectControls() {
  $('#taskDetailPanel').replaceChildren();
  $('#taskDetailPanel').hidden = true;
  $('#reconcileResult').textContent = '';
  $('#usageResult').textContent = '';
  $('#memoryStatus').textContent = '';
  $('#attachButton').hidden = true;
  $('#candidateAgent').value = '';
  $('#reconcileTask').value = '';
}

async function loadProject(p, conversation) {
  const view = ++state.viewVersion;
  const projectChanged = state.project?.project_id !== p.project_id;
  if (projectChanged) resetProjectControls();
  if (state.conversation) state.drafts.set(state.conversation.thread_id, $('#message').value);
  state.project = p;
  state.conversation = conversation;
  state.discussionMode = !!conversation.discussion_mode;
  state.busy = state.busyThreads.has(conversation.thread_id);
  state.loadingView = true;
  paintDiscussionMode();
  paintProjects();
  $('#title').textContent = p.goal;
  $('#title').title = [p.goal, p.directory || '未绑定目录'].join(' · ');
  $('#projectLabel').textContent = p.directory || '项目未绑定文件夹（点击绑定）';
  $('#projectLabel').title = p.directory || '点击绑定本机项目文件夹';
  $('#projectLabel').disabled = !!p.directory;
  $('#message').value = state.drafts.get(conversation.thread_id) || '';
  $('#message').disabled = false;
  $('#send').disabled = true;
  $('#model').disabled = true;
  $('#effort').disabled = true;
  $('#welcome').classList.add('hidden');
  $('#messages').classList.remove('hidden');
  $('#messages').replaceChildren();
  $('#traceFrame').src = '/workbench/trajectory?project_id=' + encodeURIComponent(p.project_id) + '&thread_id=' + encodeURIComponent(conversation.thread_id) + '&layout=workbench';
  $('#traceFrame').classList.toggle('hidden', !state.traceEnabled);
  $('#traceEmpty').classList.toggle('hidden', state.traceEnabled);
  showError('');
  setRunning('');
  let loaded = false;
  let baseline = null;
  try {
    const pendingModeUpdate = state.discussionModeUpdates.get(conversation.thread_id);
    if (pendingModeUpdate) await pendingModeUpdate.catch(() => {});
    if (state.viewVersion !== view) return;
    try {
      const status = await api('trace/status?project_id=' + encodeURIComponent(p.project_id) + '&thread_id=' + encodeURIComponent(conversation.thread_id));
      baseline = { id: status.run?.run_id, active: ['pending', 'running'].includes(status.run?.status) };
    } catch (_) { /* History may still be readable. */ }
    if (state.viewVersion !== view) return;
    if (baseline?.active) state.busy = true;
    const history = await api('history?project_id=' + encodeURIComponent(p.project_id) + '&thread_id=' + encodeURIComponent(conversation.thread_id));
    if (state.viewVersion !== view) return;
    if (typeof history.discussion_mode === 'boolean') {
      state.discussionMode = history.discussion_mode;
      conversation.discussion_mode = history.discussion_mode;
    }
    if (history.memory_model_error) showError('后台记忆暂不可用：' + history.memory_model_error);
    if (history.model_selection) {
      const value = JSON.stringify({ provider: history.model_selection.provider_id, model: history.model_selection.model_id });
      if (![...$('#model').options].some((option) => option.value === value)) $('#model').add(new Option('会话已选模型（不可用或未加载）', value));
      $('#model').value = value;
      state.actions.paintEffort?.();
      if (history.model_selection.reasoning_effort) $('#effort').value = history.model_selection.reasoning_effort;
    } else { $('#model').value = ''; state.actions.paintEffort?.(); }
    for (const item of history.messages || []) if ((item.type === 'human' || item.type === 'ai') && typeof item.content === 'string' && item.content) msg(item.type === 'human' ? 'user' : 'assistant', item.content);
    loaded = true;
    state.loadingView = false;
  } catch (error) {
    if (state.viewVersion === view) showError(error.message + '；请重新打开项目。');
  } finally {
    if (state.viewVersion === view) {
      state.loadingView = !loaded;
      $('#model').disabled = !loaded;
      $('#effort').disabled = !loaded;
      $('#send').disabled = !loaded || state.busy || !state.project;
      paintDiscussionMode();
      loadTasks();
      checkPending();
      refreshWorkerReviews();
      if (loaded) watchRestoredRun(p.project_id, conversation.thread_id, view, baseline);
    }
  }
}

async function openConversation(conversation, project = state.project) {
  if (!project || !conversation) return;
  clearPending();
  await loadProject(project, conversation);
}

async function openProject(project) {
  state.expandedProjects.add(project.project_id);
  const entry = browseProjectSessions(project);
  try {
    await entry.loading;
    if (!entry.items) throw entry.error || Error('会话读取失败');
    if (!entry.items.length) {
      entry.creating = true;
      paintProjects();
      entry.items = [await api('conversations', { project_id: project.project_id })];
      entry.creating = false;
    }
    await openConversation(entry.items[0], project);
  } catch (error) { showError('无法加载项目会话：' + error.message); }
}

async function send() {
  const view = state.viewVersion;
  await sendMessage();
  if (state.viewVersion === view) await checkPending();
}

function initProjectControls() {
  $('#newProject').onclick = () => { $('#projectError').textContent = ''; $('#projectDialog').showModal(); };
  document.querySelectorAll('[data-close]').forEach((button) => button.onclick = () => $('#' + button.dataset.close).close());
  $('#chooseFolder').onclick = async () => { try { const value = await api('select-folder', {}); if (value.directory) $('#directory').value = value.directory; } catch (error) { $('#projectError').textContent = error.message; } };
  $('#createProject').onclick = async () => {
    try {
      const project = await api('projects', { directory: $('#directory').value, goal: $('#goal').value });
      $('#projectDialog').close();
      setSidebarOpen(false, true);
      await loadProjects();
      await openProject(project);
    } catch (error) {
      if (error.status === 409 && error.project_id && confirm('该文件夹已关联项目，打开已有项目？')) {
        await loadProjects();
        const existing = state.projects.find((project) => project.project_id === error.project_id);
        if (existing) { $('#projectDialog').close(); setSidebarOpen(false, true); await openProject(existing); return; }
      }
      if (error.status === 503) await loadProjects();
      $('#projectError').textContent = error.message;
    }
  };
  $('#projectLabel').onclick = async () => {
    if (!state.project || state.project.directory) return;
    try {
      const selected = await api('select-folder', {});
      if (!selected.directory) return;
      await api('projects/bind', { project_id: state.project.project_id, directory: selected.directory });
      await loadProjects();
      const updated = state.projects.find((project) => project.project_id === state.project.project_id);
      if (updated) await openConversation(state.conversation, updated);
    } catch (error) { showError(error.message); }
  };
}

state.actions = {
  openConversation,
  send,
  watchRestoredRun,
  loadTasks,
  paintDiscussionMode,
  paintEffort: (keepUnknown = false) => {
    const select = $('#model');
    const effort = $('#effort');
    const previous = effort.value;
    effort.replaceChildren(new Option('默认思考等级', ''));
    if (select.value) {
      const selected = JSON.parse(select.value);
      const model = state.providers.find((item) => item.provider_id === selected.provider)?.models?.find((item) => item.id === selected.model);
      (model?.reasoning_levels || []).forEach((level) => effort.add(new Option(level, level)));
      if (keepUnknown && previous && ![...effort.options].some((option) => option.value === previous)) effort.add(new Option(previous + '（不可用或未加载）', previous));
    }
    if ([...effort.options].some((option) => option.value === previous)) effort.value = previous;
  },
};

initSidebar();
initChat();
initTasks();
initSettings();
initProjectControls();

Promise.all([
  loadProjects(),
  loadProviders(),
  api('workspace-settings').then((value) => { state.ephemeral = value.store_durability === 'ephemeral'; state.traceEnabled = value.trace_enabled !== false; if (state.ephemeral) showError(''); }),
]).catch((error) => showError(error.message));
