<template>
  <div class="mt-6 bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
    <div class="px-4 py-3 border-b border-gray-800">
      <h2 class="text-lg font-semibold text-white">任务历史</h2>
    </div>

    <div v-if="tasks.length === 0" class="px-4 py-8 text-center text-gray-500 text-sm">
      暂无任务记录
    </div>

    <div v-else class="overflow-x-auto">
      <table class="w-full text-sm">
        <thead>
          <tr class="text-gray-400 text-left border-b border-gray-800">
            <th class="px-4 py-3 font-medium">任务 ID</th>
            <th class="px-4 py-3 font-medium">命令</th>
            <th class="px-4 py-3 font-medium">参数</th>
            <th class="px-4 py-3 font-medium">状态</th>
            <th class="px-4 py-3 font-medium">创建时间</th>
            <th class="px-4 py-3 font-medium">耗时</th>
            <th class="px-4 py-3 font-medium">结果</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="task in tasks" :key="task.task_id"
            class="border-b border-gray-800/50 hover:bg-gray-800/30 transition">
            <td class="px-4 py-3 font-mono text-xs text-gray-400">{{ task.task_id }}</td>
            <td class="px-4 py-3">
              <span class="px-2 py-0.5 bg-gray-800 rounded text-xs font-medium text-gray-300">
                {{ commandLabel(task.command) }}
              </span>
            </td>
            <td class="px-4 py-3 text-xs text-gray-400" :title="formatParams(task.params)">
              {{ formatParams(task.params) }}
            </td>
            <td class="px-4 py-3">
              <span class="inline-flex items-center gap-1.5 text-xs font-medium" :class="taskStatusClass(task.status)">
                <span v-if="task.status === 'running'" class="animate-spin inline-block w-3 h-3 border-2 border-current border-t-transparent rounded-full"></span>
                <span v-else class="w-1.5 h-1.5 rounded-full" :class="taskDotClass(task.status)"></span>
                {{ taskStatusLabel(task.status) }}
              </span>
            </td>
            <td class="px-4 py-3 text-xs text-gray-400">{{ formatTime(task.created_at) }}</td>
            <td class="px-4 py-3 text-xs text-gray-400">{{ duration(task) }}</td>
            <td class="px-4 py-3 text-xs max-w-lg truncate" :class="task.error ? 'text-red-400' : 'text-gray-400'" :title="task.error || formatResult(task.result)">
              {{ task.error || formatResult(task.result) }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup>
defineProps({
  tasks: { type: Array, default: () => [] },
})

function taskStatusClass(s) {
  return {
    pending: 'text-gray-400',
    running: 'text-yellow-400',
    completed: 'text-green-400',
    failed: 'text-red-400',
  }[s] || 'text-gray-400'
}

function taskDotClass(s) {
  return {
    pending: 'bg-gray-400',
    completed: 'bg-green-400',
    failed: 'bg-red-400',
  }[s] || 'bg-gray-400'
}

function taskStatusLabel(s) {
  return { pending: '等待中', running: '执行中', completed: '已完成', failed: '失败' }[s] || s
}

function formatTime(ts) {
  if (!ts) return '-'
  const d = new Date(ts * 1000)
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}:${String(d.getSeconds()).padStart(2, '0')}`
}

function duration(task) {
  const start = task.started_at || task.created_at
  const end = task.finished_at || (task.status === 'running' ? Date.now() / 1000 : null)
  if (!start || !end) return '-'
  const sec = Math.round(end - start)
  if (sec < 60) return `${sec}s`
  const min = Math.floor(sec / 60)
  return `${min}m ${sec % 60}s`
}

function formatParams(params) {
  if (!params || Object.keys(params).length === 0) return '-'
  const labels = {
    max_chatgpt_active: 'active保留',
    target: 'active保留',
    account_id: 'Team',
    email: 'pending邮箱',
    invite_domains: 'invite域名池',
    replace_with_pending_invite: '自动补位',
    replace_mode: '补位模式',
    trigger: '触发',
  }
  return Object.entries(params)
    .filter(([_, v]) => v !== '' && v !== null && v !== undefined)
    .map(([k, v]) => `${labels[k] || k}=${formatParamValue(k, v)}`)
    .join(' · ') || '-'
}

function formatParamValue(key, value) {
  if (key === 'replace_with_pending_invite') return value ? '开启' : '关闭'
  if (key === 'replace_mode') return value === 'create_invite' ? '创建invite' : 'pending'
  if (typeof value === 'boolean') return value ? '是' : '否'
  return String(value)
}

function commandLabel(command) {
  return {
    'swap-seats': 'swap_seat',
    'auto-swap-seats': '自动 swap_seat',
    'auto-detect-replace': '自动检测替换',
    'manage-teams': '多 Team 调度',
    'consume-pending-invite': '消费 pending invite',
    'create-invite': '新增 invite 注册',
    check: 'quota 检查',
    rotate: 'swap_seat',
  }[command] || command
}

function resultPartsFromSummary(result) {
  const summary = result?.summary || {}
  const parts = []
  if (Array.isArray(result?.selected_emails)) {
    parts.push(`选中 ${result.selected_emails.length} 个`)
  }
  if (Number.isFinite(summary.quota_available)) parts.push(`quota可用 ${summary.quota_available}`)
  if (Number.isFinite(summary.seat_updated)) parts.push(`seat更新 ${summary.seat_updated}`)
  if (Number.isFinite(summary.oauth_updated)) parts.push(`OAuth更新 ${summary.oauth_updated}`)
  if (Number.isFinite(summary.seat_failed) && summary.seat_failed > 0) parts.push(`seat失败 ${summary.seat_failed}`)
  if (Number.isFinite(summary.oauth_failed) && summary.oauth_failed > 0) parts.push(`OAuth失败 ${summary.oauth_failed}`)
  return parts
}

function formatResult(result) {
  if (result === null || result === undefined) return '-'
  if (typeof result === 'string') return result
  if (result.mode === 'multi_team_manage') {
    const mode = result.replace_mode === 'create_invite' ? 'create' : 'pending'
    return `Team成功 ${result.teams_ok || 0}/${result.teams_total || 0} · 失败 ${result.teams_failed || 0} · 自动补位=${result.replace_with_pending_invite ? mode : '关闭'}`
  }
  if (result.mode === 'auto_detect_replace') {
    const mode = result.replace_mode === 'create_invite' ? 'create' : 'pending'
    return `${result.replaced ? '已补位' : '未补位'} · ${mode} · ${result.team || result.account_id || 'Team'} · ${result.reason || '-'}`
  }
  if (result.mode === 'consume_pending_invite') {
    return `${result.invited ? '已消费pending' : '未消费pending'} · ${result.email || result.requested_email || '-'} · ${result.reason || '-'}`
  }
  if (result.mode === 'create_invite') {
    return `${result.invited ? '已新增invite' : '未新增invite'} · ${result.email || '-'} · ${result.reason || '-'}`
  }
  if (result.mode === 'swap_seat') {
    const parts = resultPartsFromSummary(result)
    if (result.skipped) parts.unshift(`跳过:${result.reason || 'yes'}`)
    if (result.team?.label || result.team?.account_id) parts.unshift(`Team=${result.team.label || result.team.account_id}`)
    return parts.join(' · ') || JSON.stringify(result)
  }
  return JSON.stringify(result)
}
</script>
