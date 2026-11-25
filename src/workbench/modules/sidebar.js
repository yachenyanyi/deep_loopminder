import { $, api, showError, state, markProjectTitle } from './state.js';

let sidebarMedia;

export function paintProjects() {
  const box = $('#projects');
  if (!box) return;
  const focusKey = box.contains(document.activeElement) ? document.activeElement.dataset.sidebarKey : null;
  box.replaceChildren();
  for (const project of state.projects) {
    const id = project.project_id;
    const expanded = state.expandedProjects.has(id);
    const entry = state.projectConversations.get(id);
    const group = document.createElement('li');
    const header = document.createElement('div');
    header.className = 'project-header';
    const toggle = document.createElement('button');
    toggle.className = 'project project-toggle';
    toggle.classList.toggle('current-project', state.project?.project_id === id);
    toggle.dataset.sidebarKey = id;
    toggle.title = [project.goal, project.directory || '未绑定目录'].join(' · ');
    toggle.setAttribute('aria-expanded', String(expanded));
    toggle.setAttribute('aria-controls', 'sessions-' + id);
    for (const className of ['project-chevron', 'folder-icon']) {
      const icon = document.createElement('span');
      icon.className = className;
      icon.setAttribute('aria-hidden', 'true');
      toggle.append(icon);
    }
    const title = document.createElement('span');
    title.className = 'project-name';
    title.textContent = markProjectTitle(project);
    toggle.append(title);
    toggle.onclick = () => {
      if (state.expandedProjects.delete(id)) paintProjects();
      else {
        state.expandedProjects.add(id);
        browseProjectSessions(project);
      }
    };
    header.append(toggle);
    const add = document.createElement('button');
    add.className = 'link project-add';
    add.dataset.sidebarKey = id + '/new';
    add.textContent = '＋';
    add.title = '直接新建并打开会话';
    add.setAttribute('aria-label', '在「' + markProjectTitle(project) + '」中新建会话');
    add.disabled = !!entry?.creating;
    add.onclick = (event) => { event.stopPropagation(); newProjectConversation(project, entry || browseProjectSessions(project)); };
    header.append(add);
    group.append(header);

    const sessions = document.createElement('ul');
    sessions.className = 'project-sessions';
    sessions.id = 'sessions-' + id;
    sessions.hidden = !expanded;
    sessions.setAttribute('aria-label', markProjectTitle(project) + '的会话');
    if (expanded && entry?.items?.length) {
      for (const conversation of entry.items.slice().reverse()) {
        const row = document.createElement('li');
        const button = document.createElement('button');
        button.className = 'project conversation';
        button.dataset.sidebarKey = id + '/' + conversation.thread_id;
        button.textContent = conversation.title || '新会话';
        button.title = button.textContent;
        if (state.conversation?.thread_id === conversation.thread_id) {
          button.classList.add('active');
          button.setAttribute('aria-current', 'true');
        }
        button.onclick = () => {
          setSidebarOpen(false, true);
          state.actions.openConversation?.(conversation, project);
        };
        row.append(button);
        sessions.append(row);
      }
    } else if (expanded) {
      const item = document.createElement('li');
      item.className = 'project-list-status';
      item.setAttribute('role', 'status');
      item.textContent = entry?.loading ? '正在读取会话…' : entry?.error ? '会话读取失败。收起后展开可重试。' : '暂无会话';
      sessions.append(item);
    }
    group.append(sessions);
    box.append(group);
  }
  if (focusKey) {
    const focus = [...box.querySelectorAll('[data-sidebar-key]')].find((button) => button.dataset.sidebarKey === focusKey && !button.disabled);
    focus?.focus({ preventScroll: true });
  }
}

export function browseProjectSessions(project, reload = false) {
  const id = project.project_id;
  let entry = state.projectConversations.get(id);
  if (!entry) {
    entry = { items: null, loading: null, error: null, creating: false };
    state.projectConversations.set(id, entry);
  }
  if (!entry.loading && !entry.creating && (reload || !entry.items)) {
    entry.error = null;
    entry.loading = api('conversations?project_id=' + encodeURIComponent(id))
      .then((result) => { entry.items = result.conversations; })
      .catch((error) => { entry.error = error; })
      .finally(() => { entry.loading = null; paintProjects(); });
  }
  paintProjects();
  return entry;
}

export async function newProjectConversation(project, entry = browseProjectSessions(project)) {
  const view = state.viewVersion;
  entry.creating = true;
  paintProjects();
  try {
    await entry.loading;
    const conversation = await api('conversations', { project_id: project.project_id });
    entry.items.push(conversation);
    if (state.viewVersion !== view) return;
    state.expandedProjects.add(project.project_id);
    setSidebarOpen(false, true);
    await state.actions.openConversation?.(conversation, project);
  } catch (error) {
    if (state.viewVersion === view) showError(error.message);
  } finally {
    entry.creating = false;
    paintProjects();
  }
}

export async function loadProjects() {
  state.projects = await api('projects');
  paintProjects();
}

export function setSidebarOpen(open, restoreFocus = false) {
  const app = $('.app');
  if (!app) return;
  const wasOpen = app.classList.contains('sidebar-open');
  app.classList.toggle('sidebar-open', open);
  $('#sidebarToggle')?.setAttribute('aria-expanded', String(open));
  $('.main').inert = open;
  if (open) $('#newProject')?.focus();
  if (!open && wasOpen && restoreFocus && sidebarMedia?.matches) $('#sidebarToggle')?.focus();
}

export function initSidebar() {
  sidebarMedia = window.matchMedia('(max-width: 860px)');
  $('#sidebarToggle').onclick = () => setSidebarOpen(true);
  $('#sidebarClose').onclick = () => setSidebarOpen(false, true);
  $('#sidebarBackdrop').onclick = () => setSidebarOpen(false, true);
  sidebarMedia.addEventListener('change', () => setSidebarOpen(false));
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape' || document.querySelector('dialog[open]') || !$('.app').classList.contains('sidebar-open')) return;
    event.preventDefault();
    setSidebarOpen(false, true);
  });
  $('#refresh').onclick = async () => {
    try {
      await loadProjects();
      for (const project of state.projects) if (state.expandedProjects.has(project.project_id)) browseProjectSessions(project, true);
    } catch (error) { showError(error.message); }
  };
}
