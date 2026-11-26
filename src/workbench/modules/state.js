export const $ = (selector) => document.querySelector(selector);

export const state = {
  projects: [],
  providers: [],
  project: null,
  expandedProjects: new Set(),
  projectConversations: new Map(),
  conversation: null,
  busyThreads: new Set(),
  drafts: new Map(),
  busy: false,
  discussionMode: false,
  discussionModeUpdates: new Map(),
  loadingView: false,
  modelFormProvider: null,
  ephemeral: false,
  localDev: false,
  traceEnabled: true,
  modeDiscovery: null,
  restoredObservers: new Set(),
  restoredBaselines: new Map(),
  viewVersion: 0,
  actions: {},
};

export async function api(path, body) {
  const response = await fetch('/workbench/api/' + path, body === undefined ? {} : {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const value = await response.json();
  if (!response.ok) {
    const error = new Error(value.error || '请求失败');
    error.project_id = value.project_id;
    error.status = response.status;
    throw error;
  }
  return value;
}

export function showError(message) {
  const banner = $('#banner');
  if (!banner) return;
  const notices = [state.ephemeral ? '当前项目状态存储在临时内存中，服务重启后会丢失。' : '', message].filter(Boolean);
  banner.textContent = notices.join(' ');
  banner.hidden = !banner.textContent;
}

export function setRunning(message = '') {
  const status = $('#runStatus');
  if (!status) return;
  status.textContent = message;
  status.hidden = !message;
}

export function markProjectTitle(project) {
  const target = project?.directory ? project.directory.replace(/[\\/]+$/, '').split(/[\\/]/).pop() : project?.goal;
  return target || '未命名项目';
}
