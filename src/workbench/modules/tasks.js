import { $, api, state, showError } from './state.js';

let currentTasks = [];

function statusLabel(status) {
  return { discussion: '商讨中', developing: '开发中', review: '审查中', validation: '待人工验收', completed: '已完成' }[status] || status || '状态未知';
}

function statusForTask(task) {
  if (task.validation?.approval_state === 'approved') return 'completed';
  if (task.validation?.validation_state === 'pending' || task.validation?.validation_state === 'passed') return 'validation';
  if (task.independent_review?.status === 'pending') return 'review';
  if (task.dispatch?.status === 'running' || task.status === 'in_progress') return 'developing';
  return task.discussion?.decision === 'pass' ? 'developing' : 'discussion';
}

function formatWorkerUsage(item) {
  const roleLabel = { developer: '开发负责人', reviewer: '独立审查者' }[item.role] || item.role || item.agent_id;
  const usage = item.context_usage;
  const compactionMap = { unknown: '未知', compacted: '已压缩', none: '未压缩' };
  const compaction = compactionMap[item.compaction_visibility] || item.compaction_visibility || '未知';
  if (!usage || typeof usage.used !== 'number' || typeof usage.size !== 'number') {
    return `${roleLabel}上下文：用量未知 · 压缩状态：${compaction}`;
  }
  const remaining = typeof usage.remaining === 'number' ? usage.remaining : Math.max(0, usage.size - usage.used);
  return `${roleLabel}上下文：已用 ${usage.used.toLocaleString()} / ${usage.size.toLocaleString()} · 剩余 ${remaining.toLocaleString()} · 压缩状态：${compaction}`;
}

function taskCard(task, view, workers = []) {
  const card = document.createElement('article');
  card.className = 'task-card';
  card.id = 'task-' + task.task_id;
  const header = document.createElement('header');
  const title = document.createElement('h3');
  title.textContent = task.title || task.task_id;
  const badge = document.createElement('span');
  badge.className = 'task-status status-' + statusForTask(task);
  badge.textContent = statusLabel(statusForTask(task));
  header.append(title, badge);
  card.append(header);
  const facts = document.createElement('p');
  facts.className = 'task-facts';
  const discussion = task.discussion?.decision === 'pass' ? '已通过' : task.discussion?.decision === 'reject' ? '未通过' : '未记录';
  const review = task.independent_review?.status || '未记录';
  const validation = task.validation?.approval_state || task.validation?.validation_state || '未记录';
  facts.textContent = `商讨 ${discussion} · 独立审查 ${review} · 验收/批准 ${validation}`;
  card.append(facts);
  if (workers.length) {
    const usageLine = document.createElement('p');
    usageLine.className = 'task-usage';
    usageLine.textContent = workers.map(formatWorkerUsage).join('\n');
    card.append(usageLine);
  }
  const actions = document.createElement('div');
  actions.className = 'task-actions';
  const discuss = document.createElement('button');
  discuss.className = 'secondary';
  discuss.textContent = '商讨记录';
  discuss.onclick = () => showDiscussion(task, view);
  const approve = document.createElement('button');
  approve.className = 'secondary';
  approve.textContent = '人工验收与批准';
  approve.onclick = () => showHumanValidation(task, view);
  actions.append(discuss, approve);
  card.append(actions);
  return card;
}

async function showDiscussion(task, view) {
  const panel = $('#taskDetailPanel');
  panel.hidden = false;
  panel.replaceChildren();
  const title = document.createElement('h3');
  title.textContent = '商讨记录 · ' + (task.title || task.task_id);
  panel.append(title);
  const discussion = task.discussion;
  if (!discussion) { panel.append(Object.assign(document.createElement('p'), { textContent: '当前尚无已关联的商讨记录。' })); return; }
  const body = document.createElement('pre');
  body.textContent = `PM 确认：${discussion.pm_confirmation || '未知'}\n\n${(discussion.turns || []).map((turn, index) => {
    if (turn.unavailable) return `第 ${index + 1} 轮（不可用）\n${turn.message || '该轮来源暂不可用'}`;
    const mode = turn.mode_record || discussion.mode_record || {};
    return `第 ${index + 1} 轮（${turn.status === 'not_received' ? '结果尚未领取' : turn.status || '未知'}${turn.included_in_decision ? '，已纳入本次判断' : '，未纳入当前判断'}）\nPM：${turn.question || '未知'}\n负责人：${turn.result || '未知'}\n模式：目标 ${mode.requested_mode || '未记录'}；适配器报告 ${mode.reported_mode || '未确认'}${mode.reported_mode_source ? '（' + mode.reported_mode_source + '）' : ''}\n指纹范围：${turn.content_digest_scope || discussion.content_digest_scope || '未记录'}\nClef：${turn.decision || discussion.decision || '尚未判断'} ${JSON.stringify(turn.answer || discussion.answer || {})}`;
  }).join('\n\n')}`;
  panel.append(body);
}

async function showHumanValidation(task, view) {
  const panel = $('#taskDetailPanel');
  panel.hidden = false;
  panel.replaceChildren();
  const title = document.createElement('h3');
  title.textContent = '人工验收与批准 · ' + (task.title || task.task_id);
  panel.append(title);
  const review = task.independent_review;
  const reviewLine = document.createElement('p');
  const reviewLabels = { pass: '通过', fail: '未通过', unknown: '结论未知', pending: '审查中' };
  reviewLine.textContent = '独立审查：' + (review?.review_id ? (reviewLabels[review.status] || review.status || '未知') : '暂无可核验结论') + (review?.summary ? '。' + review.summary : '');
  panel.append(reviewLine);
  if (review?.blocking_issues?.length) {
    const blockers = document.createElement('p');
    blockers.textContent = '阻塞问题：' + review.blocking_issues.join('；');
    panel.append(blockers);
  }
  const recorded = task.validation?.validated_criteria;
  if (recorded && (Object.keys(recorded).length !== (task.acceptance_criteria || []).length || (task.acceptance_criteria || []).some((criterion) => !Object.hasOwn(recorded, criterion)))) {
    const previous = document.createElement('p');
    previous.textContent = '已保存的验收对应当时的标准：' + Object.entries(recorded).map(([criterion, passed]) => (passed ? '通过 ' : '未通过 ') + criterion).join('、');
    panel.append(previous);
  }
  const checks = (task.acceptance_criteria || []).map((criterion) => {
    const label = document.createElement('label');
    const input = document.createElement('input');
    input.type = 'checkbox';
    label.append(input, document.createTextNode(' ' + criterion));
    panel.append(label);
    return { criterion, input };
  });
  const validate = document.createElement('button');
  validate.className = 'secondary';
  validate.textContent = '记录逐项验收';
  validate.onclick = async () => {
    try { const result = await api('human-validation', { project_id: state.project.project_id, task_id: task.task_id, criteria: Object.fromEntries(checks.map((item) => [item.criterion, item.input.checked])) }); if (!result || result.status === 'cancelled') return; validate.textContent = '验收结果已记录'; await loadTasks(); }
    catch (error) { showError(error.message); }
  };
  panel.append(validate);
  for (const approved of [true, false]) {
    const button = document.createElement('button');
    button.className = 'secondary';
    button.textContent = approved ? '批准当前交付' : '拒绝当前交付';
    button.onclick = async () => {
      try { const result = await api('human-approval', { project_id: state.project.project_id, task_id: task.task_id, approved }); if (!result || result.status === 'cancelled') return; button.textContent = approved ? '人工批准已记录' : '人工拒绝已记录'; await loadTasks(); }
      catch (error) { showError(error.message); }
    };
    panel.append(button);
  }
}

export async function loadTasks() {
  const list = $('#taskList');
  const empty = $('#traceEmpty');
  if (!list || !state.project) return;
  try {
    const projectId = state.project.project_id;
    const threadId = state.conversation?.thread_id;
    const [data, usageData] = await Promise.all([
      api('project-status?project_id=' + encodeURIComponent(projectId)),
      threadId
        ? api('worker-usage?project_id=' + encodeURIComponent(projectId) + '&thread_id=' + encodeURIComponent(threadId)).catch(() => ({ workers: [] }))
        : Promise.resolve({ workers: [] }),
    ]);
    const usageByTask = new Map();
    for (const worker of usageData.workers || []) {
      if (!worker.task_id) continue;
      if (!usageByTask.has(worker.task_id)) usageByTask.set(worker.task_id, []);
      usageByTask.get(worker.task_id).push(worker);
    }
    currentTasks = data.tasks || [];
    list.replaceChildren();
    currentTasks.forEach((task) => list.append(taskCard(task, state.viewVersion, usageByTask.get(task.task_id) || [])));
    if (!currentTasks.length) list.append(Object.assign(document.createElement('p'), { className: 'notice', textContent: '当前项目暂无任务。' }));
    const recovery = $('#workerRecovery');
    recovery.replaceChildren();
    (data.unconfirmed_workers || []).forEach((worker) => {
      const button = document.createElement('button');
      button.className = 'secondary';
      button.textContent = '停止并核对负责人 · ' + (worker.task_id || worker.agent_id);
      button.onclick = async () => {
        try { await api('stop-unconfirmed-worker', { project_id: state.project.project_id, agent_id: worker.agent_id }); button.textContent = '已确认停止'; button.disabled = true; }
        catch (error) { showError(error.message); }
      };
      recovery.append(button);
    });
    if (empty && !state.traceEnabled) empty.hidden = false;
    const recoveryDetails = data.worker_executions || [];
    recoveryDetails.forEach((entry) => {
      const detail = document.createElement('div');
      const recoveryState = entry.recovery || {};
      detail.textContent = `${entry.task_id || '未知任务'} · 任务 ${entry.task_status || '未知'} · 恢复 ${recoveryState.classification || '未知'} · Worker ${recoveryState.worker_state || '未知'} · Thread ${recoveryState.thread_status || '未知'} · Run ${recoveryState.run_status || '未知'}${entry.last_progress_at ? ' · 最近进度 ' + entry.last_progress_at : ''}${entry.recovery_hint ? ' · ' + entry.recovery_hint : ''}${recoveryState.reason ? ' · ' + recoveryState.reason : ''}`;
      recovery.append(detail);
    });
    paintTaskBar(currentTasks);
  } catch (error) { showError(error.message); }
}

export function paintTaskBar(tasks = currentTasks) {
  const bar = $('#taskStatusBar');
  if (!bar) return;
  const task = tasks.find((item) => statusForTask(item) === 'validation') || tasks[0];
  if (!task) { bar.hidden = true; return; }
  bar.hidden = false;
  bar.replaceChildren();
  const text = document.createElement('span');
  text.textContent = `当前任务：${task.title || task.task_id} · ${statusLabel(statusForTask(task))}`;
  const action = document.createElement('button');
  action.className = 'link';
  action.textContent = statusForTask(task) === 'validation' ? '去验收' : '查看任务';
  action.onclick = () => {
    document.querySelector('[data-tab="activity"]').click();
    requestAnimationFrame(() => document.getElementById('task-' + task.task_id)?.scrollIntoView({ block: 'center' }));
  };
  bar.append(text, action);
}

export function initTasks() {
  $('#taskRefresh')?.addEventListener('click', loadTasks);
  let reconciled = null;
  $('#reconcileButton')?.addEventListener('click', async () => {
    const project = state.project?.project_id;
    const task = $('#reconcileTask').value.trim();
    const output = $('#reconcileResult');
    if (!project || !task) { output.textContent = '请选择项目并输入任务编号。'; return; }
    try {
      const status = await api('dispatch-status?project_id=' + encodeURIComponent(project) + '&task_id=' + encodeURIComponent(task));
      if (!status.intent) { output.textContent = '没有派工记录。'; return; }
      if (status.intent.status !== 'pending') { output.textContent = '已确认 Run：' + (status.intent.run_id || '未知'); return; }
      const body = { project_id: project, task_id: task };
      const candidate = $('#candidateAgent').value.trim();
      if (candidate) body.agent_id = candidate;
      const result = await api('reconcile-dispatch', body);
      output.textContent = result.matches?.length ? '找到关联 Run：' + result.matches.map((item) => item.run_id + '（' + item.status + '）').join('，') : '暂未找到可证明关联的 Run。';
      if (result.matches?.length === 1) { reconciled = { project_id: project, task_id: task, run_id: result.matches[0].run_id, agent_id: result.matches[0].agent_id }; $('#attachButton').hidden = false; }
    } catch (error) { output.textContent = error.message; }
  });
  $('#attachButton')?.addEventListener('click', async () => {
    if (!reconciled) return;
    try { const result = await api('attach-reconciled-dispatch', reconciled); $('#reconcileResult').textContent = '已关联官方 Run：' + result.run_id; $('#attachButton').hidden = true; reconciled = null; }
    catch (error) { $('#reconcileResult').textContent = error.message; }
  });
  $('#usageButton')?.addEventListener('click', async () => {
    if (!state.project) return;
    try {
      const data = await api('worker-usage?project_id=' + encodeURIComponent(state.project.project_id) + '&thread_id=' + encodeURIComponent(state.conversation?.thread_id || ''));
      $('#usageResult').textContent = data.workers?.length ? data.workers.map(formatWorkerUsage).join('\n') : '当前没有已关联的负责人会话。';
    } catch (error) { $('#usageResult').textContent = error.message; }
  });
  let memoryJournal = null;
  const inspectMemory = async (reconcile = false) => {
    if (!state.project) return;
    try {
      const data = await api('memory-recovery?project_id=' + encodeURIComponent(state.project.project_id), ...(reconcile ? [{}] : []));
      memoryJournal = !reconcile && data.journal_digest ? { project: state.project.project_id, digest: data.journal_digest } : null;
      $('#memoryStatus').textContent = (data.reconciled ? '已核对并恢复：' : '核对状态：') + data.status;
    } catch (error) { $('#memoryStatus').textContent = error.message; }
  };
  $('#memoryCheck')?.addEventListener('click', () => inspectMemory(false));
  $('#memoryFix')?.addEventListener('click', () => inspectMemory(true));
  $('#memoryRollback')?.addEventListener('click', async () => {
    if (!memoryJournal) { $('#memoryStatus').textContent = '请先核对当前项目的记忆写回记录。'; return; }
    try { const data = await api('memory-recovery?project_id=' + encodeURIComponent(memoryJournal.project) + '&action=rollback', { journal_digest: memoryJournal.digest }); $('#memoryStatus').textContent = data.status === 'rolled_back' ? '已恢复到写回前状态。' : data.status; memoryJournal = null; }
    catch (error) { $('#memoryStatus').textContent = error.message; }
  });
}

