import { $, api, state, showError } from './state.js';
import { loadProjects } from './sidebar.js';

function feedback(id, text, error = false) {
  const node = $(id);
  if (!node) return;
  node.classList.toggle('error', error);
  node.textContent = text;
}

function defaultEfforts() {
  const selected = $('#defaultModel').value;
  const old = $('#defaultEffort').value;
  $('#defaultEffort').replaceChildren(new Option('默认', ''));
  if (selected) {
    const value = JSON.parse(selected);
    const model = state.providers.find((item) => item.provider_id === value.provider)?.models?.find((item) => item.id === value.model);
    (model?.reasoning_levels || []).forEach((level) => $('#defaultEffort').add(new Option(level, level)));
  }
  if ([...$('#defaultEffort').options].some((option) => option.value === old)) $('#defaultEffort').value = old;
}

export function paintModels() {
  const previous = $('#model').value;
  $('#model').replaceChildren(new Option('工作台默认模型', ''));
  state.providers.forEach((provider) => (provider.models || []).filter((model) => model.enabled).forEach((model) => $('#model').add(new Option(provider.name + ' · ' + model.id, JSON.stringify({ provider: provider.provider_id, model: model.id })))));
  if (previous && ![...$('#model').options].some((option) => option.value === previous)) $('#model').add(new Option('会话已选模型（不可用或未加载）', previous));
  if ([...$('#model').options].some((option) => option.value === previous)) $('#model').value = previous;
  state.actions.paintEffort?.(true);
}

export async function loadProviders() {
  state.providers = await api('providers');
  paintModels();
}

function readProviderModels() {
  const levels = new Map([...document.querySelectorAll('#modelList input[data-levels]')].map((input) => [input.dataset.levels, input.value]));
  return [...document.querySelectorAll('#modelList input[type=checkbox]')].map((checkbox) => {
    const values = (levels.get(checkbox.dataset.model) || '').split(',').map((value) => value.trim()).filter(Boolean);
    return { id: checkbox.dataset.model, enabled: checkbox.checked, reasoning_levels: values.length ? values : null };
  });
}

function paintProviderModels(providerId, models) {
  state.modelFormProvider = providerId;
  const box = $('#modelList');
  box.replaceChildren();
  models.slice().sort((a, b) => Number(b.enabled) - Number(a.enabled) || a.id.localeCompare(b.id)).forEach((model) => {
    const row = document.createElement('div');
    row.className = 'model-row';
    const label = document.createElement('label');
    const check = document.createElement('input');
    check.type = 'checkbox';
    check.checked = !!model.enabled;
    check.dataset.model = model.id;
    label.append(check, document.createTextNode(' ' + model.id));
    const levels = document.createElement('input');
    levels.placeholder = '思考等级，例如 low, medium, high';
    levels.value = (model.reasoning_levels || []).join(', ');
    levels.dataset.levels = model.id;
    row.append(label, levels);
    box.append(row);
  });
  $('#modelActions').hidden = !models.length;
}

function loadProviderForm(providerId) {
  const provider = state.providers.find((entry) => entry.provider_id === providerId);
  const select = $('#providerSelect');
  select.replaceChildren(new Option('新增模型服务', ''));
  state.providers.forEach((entry) => select.add(new Option(entry.name === entry.provider_id ? entry.provider_id : entry.name + ' · ' + entry.provider_id, entry.provider_id)));
  select.value = provider?.provider_id || '';
  $('#providerId').value = provider?.provider_id || '';
  $('#providerName').value = provider?.name || '';
  $('#baseUrl').value = provider?.base_url || '';
  $('#token').value = '';
  paintProviderModels(provider?.provider_id || null, provider?.models || []);
}

async function refreshDefaults() {
  const current = await api('default-model');
  const select = $('#defaultModel');
  select.replaceChildren(new Option('请选择', ''));
  state.providers.forEach((provider) => (provider.models || []).filter((model) => model.enabled).forEach((model) => select.add(new Option(provider.name + ' · ' + model.id, JSON.stringify({ provider: provider.provider_id, model: model.id })))));
  if (current) {
    const saved = JSON.stringify({ provider: current.provider_id, model: current.model_id });
    if (![...select.options].some((option) => option.value === saved)) select.add(new Option(current.provider_id + ' · ' + current.model_id + '（当前不可用）', saved));
    select.value = saved;
  }
  defaultEfforts();
  if (current?.reasoning_effort) {
    const levels = [...$('#defaultEffort').options].map((option) => option.value);
    if (!levels.includes(current.reasoning_effort)) $('#defaultEffort').add(new Option(current.reasoning_effort + '（当前不可用）', current.reasoning_effort));
    $('#defaultEffort').value = current.reasoning_effort;
  }
}

export async function loadBaseSettings() {
  try { await loadProviders(); loadProviderForm($('#providerSelect').value || state.providers[0]?.provider_id); await refreshDefaults(); }
  catch (error) { feedback('#settingsError', error.message, true); }
  try {
    const clef = await api('clef');
    $('#clefAccount').value = clef.account_id || '';
    $('#clefToken').value = '';
    $('#clefAccount').placeholder = clef.account_configured ? '已配置，留空保持不变' : 'Cloudflare Account ID';
    $('#clefToken').placeholder = clef.token_configured ? '已配置，留空保持不变' : '输入 API 令牌';
  } catch (error) { feedback('#clefStatus', error.message, true); }
}

async function loadWorkerSettings() {
  for (const [prefix, query] of [['worker', ''], ['reviewer', '?provider_id=codex']]) {
    try {
      const data = await api('worker-settings' + query);
      const settings = data.settings;
      const section = $('#' + prefix + 'Section');
      if (section) section.hidden = data.source === 'custom';
      $('#' + prefix + 'Enabled').checked = !!settings.enabled;
      $('#' + prefix + 'Node').value = settings.node || '';
      $('#' + prefix + 'Endpoint').value = settings.endpoint || '';
      $('#' + prefix + 'Model').value = settings.model || '';
      $('#' + prefix + 'Runtime').value = settings.runtime_dir || '';
      $('#' + prefix + 'Token').value = '';
      $('#' + prefix + 'Status').textContent = data.restart_required ? '设置已更改，重启服务后生效。' : data.available ? '已连接。' : data.error || '当前未启用或不可用。';
    } catch (error) { feedback('#' + prefix + 'Status', error.message, true); }
  }
}

async function loadRuntimeSettings() {
  try {
    const data = await api('runtime-settings');
    $('#pmDatabase').value = '';
    $('#pmDatabase').placeholder = data.database.configured ? '已配置，留空保持不变' : '输入 PostgreSQL 连接地址';
    $('#pmDatabaseState').textContent = data.database.store_ready ? '数据库已连接。' : '数据库未连接；请填写连接地址、保存并重启 PM 服务。';
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
  } catch (error) { feedback('#pmRuntimeStatus', error.message, true); }
}

export async function loadModes() {
  const project = state.project?.project_id;
  const provider = $('#modeProvider').value;
  const choice = $('#modeChoice');
  const status = $('#modeStatus');
  choice.replaceChildren();
  status.textContent = '';
  $('#modeWriteWarning').textContent = '';
  if (!project) { status.textContent = '请先选择项目。'; return; }
  try {
    const data = await api('worker-modes?project_id=' + encodeURIComponent(project) + '&provider_id=' + encodeURIComponent(provider));
    const discovery = data.discovery || {};
    state.modeDiscovery = discovery;
    paintModeWorker(discovery, data.saved_mode);
    updateModeWarning();
    status.textContent = discovery.available === false
      ? '适配器未连接或未提供模式列表，当前无法选择；已保存的默认模式仍会保留。'
      : '适配器当前报告模式：' + (discovery.current || '未知');
  } catch (error) { status.textContent = error.message; }
}

export function paintModeWorker(discovery, savedMode) {
  const choice = $('#modeChoice');
  choice.replaceChildren();
  (discovery.modes || []).forEach((mode) => {
      const option = document.createElement('option');
      option.value = mode.id;
      option.textContent = mode.name + (mode.description ? '（' + mode.description + '）' : '');
      option.dataset.writeCapable = String(!!(mode.write_capable ?? mode.write_capable === true));
      choice.append(option);
  });
  if (savedMode && !(discovery.modes || []).some((mode) => mode.id === savedMode)) {
      const option = document.createElement('option');
      option.value = savedMode;
      option.textContent = savedMode + '（已保存，适配器未列出）';
      choice.append(option);
  }
  if (savedMode) choice.value = savedMode;
}

export function updateModeWarning() {
  const choice = $('#modeChoice');
  const discovery = state.modeDiscovery || {};
  const selected = choice.options[choice.selectedIndex];
  const writable = selected?.dataset.writeCapable === 'true' || (discovery.writable || []).includes(choice.value);
  $('#modeWriteWarning').textContent = writable
    ? '注意：模式 ' + choice.value + ' 允许修改项目。商讨不会因此获得写入授权，开发仍需双方确认与 Clef 判断通过。'
    : '';
}

export async function loadWorkspaceSettings() {
  try {
    const value = await api('workspace-settings');
    state.traceEnabled = value.trace_enabled !== false;
    $('#traceEnabled').checked = state.traceEnabled;
    $('#compactionThreshold').value = value.compaction_token_threshold ?? '';
    $('#contextLimit').value = value.context_token_limit ?? '';
    if (state.project) {
      $('#traceFrame').classList.toggle('hidden', !state.traceEnabled);
      $('#traceEmpty').classList.toggle('hidden', state.traceEnabled);
    }
  } catch (error) { feedback('#workspaceStatus', error.message, true); }
}

export function initSettings() {
  $('#defaultModel').onchange = defaultEfforts;
  $('#providerSelect').onchange = () => loadProviderForm($('#providerSelect').value);
  $('#modelSearch').oninput = () => {
    const query = $('#modelSearch').value.trim().toLowerCase();
    document.querySelectorAll('#modelList .model-row').forEach((row) => { row.hidden = query && !row.textContent.toLowerCase().includes(query); });
  };
  $('#saveProvider').onclick = async () => {
    try { const saved = await api('providers', { provider_id: $('#providerId').value.trim(), name: $('#providerName').value.trim() || undefined, base_url: $('#baseUrl').value, token: $('#token').value || undefined }); await loadProviders(); loadProviderForm(saved.provider_id); feedback('#settingsError', '服务已保存'); }
    catch (error) { feedback('#settingsError', error.message, true); }
  };
  $('#discover').onclick = async () => {
    try { const provider = $('#providerId').value.trim(); const saved = state.providers.find((entry) => entry.provider_id === provider); if (!saved || $('#token').value) throw Error('请先保存模型服务，再获取模型'); const result = await api('providers/discover', { provider_id: provider }); paintProviderModels(provider, (saved.models || []).concat(result.models.filter((id) => !(saved.models || []).some((model) => model.id === id)).map((id) => ({ id, enabled: false })))); feedback('#settingsError', '已获取模型，可编辑后保存'); }
    catch (error) { feedback('#settingsError', error.message, true); }
  };
  $('#saveModels').onclick = async () => {
    try { const id = $('#providerId').value.trim(); const provider = state.providers.find((entry) => entry.provider_id === id); if (!provider) throw Error('请先保存模型服务'); await api('providers', { provider_id: id, base_url: provider.base_url, name: provider.name, models: readProviderModels() }); await loadProviders(); await refreshDefaults(); feedback('#settingsError', '模型选择已保存'); }
    catch (error) { feedback('#settingsError', error.message, true); }
  };
  $('#saveDefault').onclick = async () => {
    if (!$('#defaultModel').value) { feedback('#defaultStatus', '请先启用并选择默认模型', true); return; }
    try { const value = JSON.parse($('#defaultModel').value); await api('default-model', { provider_id: value.provider, model_id: value.model, reasoning_effort: $('#defaultEffort').value || null }); feedback('#defaultStatus', '默认模型已保存'); } catch (error) { feedback('#defaultStatus', error.message, true); }
  };
  $('#saveClef').onclick = async () => { try { await api('clef', { account_id: $('#clefAccount').value, token: $('#clefToken').value || undefined }); $('#clefToken').value = ''; feedback('#clefStatus', 'Clef 设置已保存'); } catch (error) { feedback('#clefStatus', error.message, true); } };
  for (const prefix of ['worker', 'reviewer']) $('#' + prefix + 'Save').onclick = async () => { try { const body = { provider_id: prefix === 'reviewer' ? 'codex' : undefined, enabled: $('#' + prefix + 'Enabled').checked, node: $('#' + prefix + 'Node').value, endpoint: $('#' + prefix + 'Endpoint').value, model: $('#' + prefix + 'Model').value, runtime_dir: $('#' + prefix + 'Runtime').value }; const token = $('#' + prefix + 'Token').value; if (token) body.token = token; await api('worker-settings', body); $('#' + prefix + 'Token').value = ''; feedback('#' + prefix + 'Status', '已保存；重启服务后生效。'); } catch (error) { feedback('#' + prefix + 'Status', error.message, true); } };
  $('#savePmRuntime').onclick = async () => { try { const body = { middleware: { memory: $('#pmMemory').checked, trajectory: $('#pmTrajectory').checked, acp: $('#pmAcp').checked }, port: Number($('#pmPort').value), n_jobs_per_worker: Number($('#pmJobs').value), claim_poll_seconds: Number($('#pmPoll').value), data_dir: $('#pmDataDir').value.trim(), trajectory_directory: $('#pmTraceDir').value.trim(), trajectory_stop_on_error: $('#pmTraceStrict').checked }; if ($('#pmDatabase').value) body.database_uri = $('#pmDatabase').value; await api('runtime-settings', body); feedback('#pmRuntimeStatus', '已保存；重启 PM 服务后生效。'); } catch (error) { feedback('#pmRuntimeStatus', error.message, true); } };
  $('#saveWorkerConfig').onclick = async () => { try { await api('runtime-settings', { acp_workers: JSON.parse($('#pmExtraWorkers').value || '[]'), acp_subagents: JSON.parse($('#pmExtraAgents').value || '[]') }); feedback('#workerConfigStatus', '已保存，重启服务后生效。'); } catch (error) { feedback('#workerConfigStatus', error.message, true); } };
  $('#modeChoice').onchange = updateModeWarning;
  $('#modeProvider').onchange = loadModes;
  $('#saveMode').onclick = async () => {
    const project = state.project?.project_id;
    const provider = $('#modeProvider').value;
    const mode = $('#modeChoice').value;
    if (!project) { $('#modeStatus').textContent = '请先选择项目。'; return; }
    if (!mode) { $('#modeStatus').textContent = '没有可保存的模式。'; return; }
    try { await api('worker-modes', { project_id: project, provider_id: provider, mode_id: mode }); $('#modeStatus').textContent = '已保存目标模式：' + mode + '。修改默认值不会改写已有商讨记录。'; }
    catch (error) { $('#modeStatus').textContent = error.message; }
  };
  $('#saveWorkspace').onclick = async () => {
    try {
      const payload = { trace_enabled: $('#traceEnabled').checked };
      const threshold = $('#compactionThreshold').value.trim();
      const limit = $('#contextLimit').value.trim();
      if (threshold) payload.compaction_token_threshold = parseInt(threshold, 10);
      if (limit) payload.context_token_limit = parseInt(limit, 10);
      const value = await api('workspace-settings', payload);
      state.traceEnabled = value.trace_enabled ?? payload.trace_enabled;
      $('#traceFrame').classList.toggle('hidden', !state.traceEnabled);
      $('#traceEmpty').classList.toggle('hidden', state.traceEnabled);
      feedback('#workspaceStatus', '已保存；重启服务后生效。');
    } catch (error) { feedback('#workspaceStatus', error.message, true); }
  };
  document.querySelectorAll('[data-settings]').forEach((button) => button.onclick = () => { document.querySelectorAll('[data-settings]').forEach((item) => { item.classList.toggle('active', item === button); item.setAttribute('aria-pressed', String(item === button)); }); document.querySelectorAll('[data-settings-panel]').forEach((panel) => { panel.hidden = panel.dataset.settingsPanel !== button.dataset.settings; }); });
  $('#settingsButton').onclick = async () => {
    document.querySelector('[data-settings="models"]').click();
    document.querySelectorAll('#settingsDialog .notice, #settingsDialog .error').forEach((node) => { if (node.id.endsWith('Status') || node.id === 'settingsError' || node.id === 'defaultStatus' || node.id === 'clefStatus') node.textContent = ''; });
    $('#settingsDialog').showModal();
    await Promise.all([loadRuntimeSettings(), loadBaseSettings(), loadWorkerSettings(), loadModes(), loadWorkspaceSettings()]);
  };
}

