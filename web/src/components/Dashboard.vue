<template>
  <div class="space-y-6">
    <div class="rounded-2xl border border-blue-500/20 bg-blue-500/10 px-4 py-3 text-sm leading-6 text-blue-200">
      <div class="font-medium text-blue-100">swap_seat 总览</div>
      <div class="mt-1 text-blue-200/90">
        这里只读展示多 Team、冷却、quota cache 和 CPA OAuth active/standby。不会登录成员账号、不会同步本地 auth、不会 kick/remove/cancel invite。
      </div>
    </div>

    <div class="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
      <div>
        <h2 class="text-2xl font-bold text-white">运行总览</h2>
        <p class="mt-1 text-sm text-gray-400">状态来自只读接口；执行 swap 或 pending invite 替换请到「Seat 调度」。</p>
      </div>
      <div class="flex flex-wrap gap-2">
        <button @click="emit('navigate', 'pool')" class="btn-primary px-4 py-2 text-sm">
          去 Seat 调度
        </button>
        <button @click="emit('navigate', 'team')" class="btn-secondary px-4 py-2 text-sm">
          Team 成员
        </button>
        <button @click="emit('navigate', 'config')" class="btn-secondary px-4 py-2 text-sm">
          配置面板
        </button>
        <button @click="loadOverview" :disabled="overviewLoading" class="btn-secondary px-4 py-2 text-sm disabled:opacity-50">
          {{ overviewLoading ? '刷新中...' : '刷新总览' }}
        </button>
      </div>
    </div>

    <div class="grid grid-cols-2 gap-4 sm:grid-cols-3 xl:grid-cols-6">
      <div v-for="card in cards" :key="card.label" class="rounded-xl border border-gray-800 bg-gray-900 p-4">
        <div class="text-sm text-gray-400">{{ card.label }}</div>
        <div class="mt-1 text-3xl font-bold" :class="card.color">{{ card.value }}</div>
        <div v-if="card.hint" class="mt-1 text-[11px] text-gray-500">{{ card.hint }}</div>
      </div>
    </div>

    <div v-if="overviewError" class="rounded-xl border border-red-500/20 bg-red-500/10 px-4 py-3 text-sm text-red-300">
      {{ overviewError }}
    </div>

    <div class="grid gap-4 xl:grid-cols-[1.05fr_0.95fr]">
      <div class="rounded-2xl border border-white/10 bg-gray-900/70 p-4">
        <div class="mb-3 flex items-center justify-between gap-3">
          <div>
            <h3 class="text-sm font-semibold text-white">Team 健康状态</h3>
            <p class="mt-1 text-xs text-gray-500">按 account_id 汇总冷却和 quota cache；disabled Team 不参与自动调度。</p>
          </div>
          <span class="rounded bg-gray-800 px-2 py-1 text-xs text-gray-400">{{ enabledTeams.length }}/{{ teams.length }} enabled</span>
        </div>

        <div v-if="overviewLoading" class="h-40 animate-pulse rounded-xl bg-gray-800/70"></div>
        <div v-else-if="teams.length" class="overflow-x-auto">
          <table class="w-full text-sm">
            <thead>
              <tr class="border-b border-gray-800 text-left text-gray-500">
                <th class="px-3 py-2 font-medium">Team</th>
                <th class="px-3 py-2 font-medium">active</th>
                <th class="px-3 py-2 font-medium">冷却</th>
                <th class="px-3 py-2 font-medium">quota cache</th>
                <th class="px-3 py-2 font-medium">pending invite</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="team in teams" :key="team.account_id || team.id" class="border-b border-gray-800/50">
                <td class="px-3 py-3">
                  <div class="max-w-[220px] truncate font-medium text-white">
                    {{ team.label || team.workspace_name || team.account_id }}
                  </div>
                  <div class="mt-1 font-mono text-[11px] text-gray-500">{{ team.account_id }}</div>
                  <div class="mt-1 flex flex-wrap gap-1 text-[11px]">
                    <span class="rounded px-1.5 py-0.5" :class="team.enabled === false ? 'bg-gray-500/10 text-gray-400' : 'bg-emerald-500/10 text-emerald-300'">
                      {{ team.enabled === false ? 'disabled' : 'enabled' }}
                    </span>
                    <span class="rounded bg-gray-800 px-1.5 py-0.5 text-gray-400">
                      {{ team.session_present ? '独立 session' : '共享默认 session' }}
                    </span>
                  </div>
                </td>
                <td class="px-3 py-3 text-gray-300">{{ team.max_chatgpt_active || 2 }}</td>
                <td class="px-3 py-3">
                  <span class="rounded-full px-2 py-0.5 text-xs" :class="cooldownFor(team)?.allowed ? 'bg-emerald-500/10 text-emerald-300' : cooldownFor(team) ? 'bg-amber-500/10 text-amber-300' : 'bg-gray-500/10 text-gray-400'">
                    {{ cooldownLabel(team) }}
                  </span>
                </td>
                <td class="px-3 py-3 text-xs text-gray-300">
                  <div>可用 {{ quotaSummaryForTeam(team).available }} · 耗尽 {{ quotaSummaryForTeam(team).exhausted }}</div>
                  <div class="mt-1 text-gray-500">待复查 {{ quotaSummaryForTeam(team).stale }}</div>
                </td>
                <td class="px-3 py-3 text-xs">
                  <span v-if="team.pending_invite_email" class="rounded bg-amber-500/10 px-2 py-1 font-mono text-amber-300">
                    {{ team.pending_invite_email }}
                  </span>
                  <span v-else class="text-gray-500">未指定</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-else class="rounded-xl border border-dashed border-gray-700 px-4 py-8 text-center text-sm text-gray-500">
          暂无 Team 配置。完成管理员登录后会回退当前 Team；多 Team 可在「配置面板 → 多 Team」设置。
        </div>
      </div>

      <div class="space-y-4">
        <div class="rounded-2xl border border-white/10 bg-gray-900/70 p-4">
          <div class="mb-3 flex items-center justify-between gap-3">
            <div>
              <h3 class="text-sm font-semibold text-white">quota cache 最近记录</h3>
              <p class="mt-1 text-xs text-gray-500">不会触发 CPA quota 检查；只展示本地缓存。</p>
            </div>
            <span class="rounded bg-gray-800 px-2 py-1 text-xs text-gray-400">{{ quotaEntries.length }} 条</span>
          </div>
          <div v-if="overviewLoading" class="h-24 animate-pulse rounded-xl bg-gray-800/70"></div>
          <div v-else-if="quotaEntries.length" class="space-y-2">
            <div v-for="entry in quotaEntries.slice(0, 6)" :key="entry.key" class="rounded-xl border border-white/10 bg-white/[0.03] p-3">
              <div class="flex items-start justify-between gap-3">
                <div class="min-w-0">
                  <div class="truncate font-mono text-xs text-slate-200">{{ entry.email || '-' }}</div>
                  <div class="mt-1 truncate font-mono text-[11px] text-gray-500">{{ entry.account_id || 'default' }}</div>
                </div>
                <span class="shrink-0 rounded-full px-2 py-0.5 text-[11px]" :class="quotaStateClass(entry)">
                  {{ quotaStateLabel(entry) }}
                </span>
              </div>
              <div class="mt-2 flex flex-wrap gap-2 text-[11px] text-gray-400">
                <span class="rounded bg-gray-800 px-2 py-1">5h {{ entry.primary_remaining }}%</span>
                <span class="rounded bg-gray-800 px-2 py-1">weekly {{ entry.weekly_remaining }}%</span>
                <span class="rounded bg-gray-800 px-2 py-1">下次 {{ formatTs(entry.next_check_at || entry.exhausted_until) }}</span>
              </div>
            </div>
          </div>
          <div v-else class="rounded-xl border border-dashed border-gray-700 px-4 py-6 text-sm text-gray-500">
            暂无 quota cache。第一次执行 swap_seat 后会记录刷新时间和 5h/weekly 剩余额度。
          </div>
        </div>

        <div class="rounded-2xl border border-white/10 bg-gray-900/70 p-4">
          <div class="mb-3">
            <h3 class="text-sm font-semibold text-white">CPA OAuth active / standby</h3>
            <p class="mt-1 text-xs text-gray-500">CPA 是 OAuth/auth 真相源；Dashboard 只读展示。</p>
          </div>
          <div v-if="cpaError" class="rounded-xl border border-amber-500/20 bg-amber-500/10 px-4 py-3 text-sm text-amber-200">
            {{ cpaError }}
          </div>
          <div v-else class="grid grid-cols-2 gap-3">
            <div class="rounded-xl border border-emerald-500/20 bg-emerald-500/10 p-3">
              <div class="text-xs text-emerald-200/80">active</div>
              <div class="mt-1 text-2xl font-bold text-emerald-300">{{ cpaSummary.active }}</div>
            </div>
            <div class="rounded-xl border border-white/10 bg-white/[0.03] p-3">
              <div class="text-xs text-gray-400">standby</div>
              <div class="mt-1 text-2xl font-bold text-gray-200">{{ cpaSummary.standby }}</div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../api.js'

const props = defineProps({
  loading: Boolean,
  runningTask: Object,
  adminStatus: {
    type: Object,
    default: null,
  },
})
const emit = defineEmits(['refresh', 'task-started', 'navigate'])

const teams = ref([])
const runtimeStatus = ref(null)
const cpaAuths = ref([])
const overviewLoading = ref(false)
const overviewError = ref('')
const cpaError = ref('')

const enabledTeams = computed(() => teams.value.filter(team => team.enabled !== false))
const runtimeTeams = computed(() => Array.isArray(runtimeStatus.value?.teams) ? runtimeStatus.value.teams : [])
const quotaEntries = computed(() => runtimeStatus.value?.quota_cache?.entries || [])
const quotaSummary = computed(() => runtimeStatus.value?.quota_cache?.summary || {})
const cooldownByAccount = computed(() => {
  const map = new Map()
  for (const item of runtimeTeams.value) {
    const accountId = String(item?.team?.account_id || item?.cooldown?.scope || '').trim()
    if (accountId) map.set(accountId, item.cooldown || null)
  }
  return map
})

const codexAuths = computed(() => cpaAuths.value.filter(auth => {
  const provider = String(auth.provider || auth.type || '').toLowerCase()
  const email = String(auth.email || auth.account || '').trim()
  return (!provider || provider === 'codex') && email
}))

const cpaSummary = computed(() => ({
  active: codexAuths.value.filter(authActive).length,
  standby: codexAuths.value.filter(auth => !authActive(auth)).length,
}))

const cards = computed(() => {
  const cooldownBlocked = runtimeTeams.value.filter(item => item?.cooldown && !item.cooldown.allowed).length
  return [
    { label: 'Teams', value: enabledTeams.value.length, color: 'text-white', hint: `total ${teams.value.length}` },
    { label: 'Quota 可用', value: quotaSummary.value.available || 0, color: 'text-emerald-400', hint: 'cache' },
    { label: 'Quota 耗尽', value: quotaSummary.value.exhausted_cached || 0, color: 'text-amber-400', hint: 'blocked' },
    { label: '待复查', value: quotaSummary.value.stale || 0, color: 'text-gray-300', hint: 'stale' },
    { label: 'OAuth active', value: cpaSummary.value.active, color: 'text-cyan-400', hint: 'CPA' },
    { label: '冷却中', value: cooldownBlocked, color: cooldownBlocked ? 'text-amber-400' : 'text-emerald-400', hint: 'Team' },
  ]
})

function authActive(auth) {
  return !auth?.disabled && String(auth?.status || '').toLowerCase() === 'active'
}

async function loadOverview() {
  overviewLoading.value = true
  overviewError.value = ''
  cpaError.value = ''
  try {
    const [teamsResult, runtimeResult, cpaResult] = await Promise.allSettled([
      api.getTeams(),
      api.getSwapRuntimeStatus(),
      api.getCpaFiles(),
    ])

    if (teamsResult.status === 'fulfilled') {
      teams.value = Array.isArray(teamsResult.value.teams) ? teamsResult.value.teams : []
    } else {
      teams.value = []
      overviewError.value = teamsResult.reason?.message || '读取 Team 配置失败'
    }

    if (runtimeResult.status === 'fulfilled') {
      runtimeStatus.value = runtimeResult.value
    } else {
      runtimeStatus.value = null
      overviewError.value = overviewError.value || runtimeResult.reason?.message || '读取 swap 运行状态失败'
    }

    if (cpaResult.status === 'fulfilled') {
      const value = cpaResult.value
      cpaAuths.value = Array.isArray(value) ? value : Array.isArray(value.files) ? value.files : []
    } else {
      cpaAuths.value = []
      cpaError.value = cpaResult.reason?.message || 'CPA 未配置或读取失败'
    }
  } finally {
    overviewLoading.value = false
  }
}

function cooldownFor(team) {
  const accountId = String(team?.account_id || '').trim()
  return accountId ? cooldownByAccount.value.get(accountId) : null
}

function cooldownLabel(team) {
  const cooldown = cooldownFor(team)
  if (!cooldown) return '未记录'
  if (cooldown.allowed) return `可 swap ${cooldown.used_today || 0}/${cooldown.daily_limit || 3}`
  return `冷却中 ${formatTs(cooldown.next_allowed_at)}`
}

function quotaSummaryForTeam(team) {
  const accountId = String(team?.account_id || '').trim()
  const entries = quotaEntries.value.filter(entry => String(entry.account_id || '').trim() === accountId)
  return {
    available: entries.filter(entry => entry.quota_available).length,
    exhausted: entries.filter(entry => entry.cache_state === 'blocked_until_reset').length,
    stale: entries.filter(entry => entry.cache_state === 'stale').length,
  }
}

function formatTs(ts) {
  const value = Number(ts || 0)
  if (!value) return '-'
  const d = new Date(value * 1000)
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

function quotaStateLabel(entry) {
  if (entry.cache_state === 'blocked_until_reset') return `耗尽:${entry.window || 'reset'}`
  if (entry.cache_state === 'recent_ok') return '近期可用'
  if (entry.cache_state === 'stale') return '待复查'
  return entry.status || '-'
}

function quotaStateClass(entry) {
  if (entry.cache_state === 'blocked_until_reset') return 'bg-amber-500/10 text-amber-300'
  if (entry.cache_state === 'recent_ok') return 'bg-emerald-500/10 text-emerald-300'
  if (entry.cache_state === 'stale') return 'bg-gray-500/10 text-gray-300'
  return entry.quota_available ? 'bg-emerald-500/10 text-emerald-300' : 'bg-gray-500/10 text-gray-300'
}

onMounted(loadOverview)
</script>
