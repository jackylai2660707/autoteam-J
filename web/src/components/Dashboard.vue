<template>
  <div class="space-y-6">
    <div class="rounded-2xl border border-blue-500/20 bg-blue-500/10 px-4 py-3 text-sm leading-6 text-blue-200">
      <div class="font-medium text-blue-100">swap_seat 总览</div>
      <div class="mt-1 text-blue-200/90">
        这里只读展示多 Team、冷却、受管 quota 记录和 AutoTeam 受管 OAuth。外部 CPA auth / 非受管 Team member 受保护，不使用、不启停、不删除。
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

    <div v-if="quotaEntries.length" class="rounded-2xl border px-4 py-3" :class="quotaOverviewClass">
      <div class="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
        <div>
          <div class="text-sm font-semibold text-white">{{ quotaOverview.title }}</div>
          <div class="mt-1 text-xs leading-5 text-gray-300">{{ quotaOverview.detail }}</div>
        </div>
        <div class="grid grid-cols-3 gap-2 text-center text-xs sm:min-w-[18rem]">
          <div class="rounded-xl bg-black/20 px-3 py-2">
            <div class="text-gray-400">最低剩余</div>
            <div class="mt-1 text-lg font-bold" :class="quotaOverview.minRemaining <= 20 ? 'text-amber-300' : 'text-emerald-300'">{{ quotaOverview.minRemaining }}%</div>
          </div>
          <div class="rounded-xl bg-black/20 px-3 py-2">
            <div class="text-gray-400">受管记录</div>
            <div class="mt-1 text-lg font-bold text-white">{{ quotaEntries.length }}</div>
          </div>
          <div class="rounded-xl bg-black/20 px-3 py-2">
            <div class="text-gray-400">待复查</div>
            <div class="mt-1 text-lg font-bold text-sky-300">{{ quotaOverview.stale }}</div>
          </div>
        </div>
      </div>
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
                <th class="px-3 py-2 font-medium">目标 active</th>
                <th class="px-3 py-2 font-medium">冷却</th>
                <th class="px-3 py-2 font-medium">受管 quota</th>
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
            <h3 class="text-sm font-semibold text-white">受管 quota 最近记录</h3>
            <p class="mt-1 text-xs text-gray-500">不会触发 CPA quota 检查；只展示上次巡检记录。进度条越长，剩余越多。</p>
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
                <span class="rounded bg-gray-800 px-2 py-1">5h {{ quotaWindowText(entry, 'primary') }}</span>
                <span class="rounded bg-gray-800 px-2 py-1">weekly {{ quotaWindowText(entry, 'weekly') }}</span>
                <span class="rounded bg-gray-800 px-2 py-1">monthly {{ quotaWindowText(entry, 'monthly') }}</span>
                <span class="rounded bg-gray-800 px-2 py-1">{{ entry.cache_state === 'blocked_until_reset' ? 'reset后可用' : '下次' }} {{ quotaNextCheckLabel(entry) }}</span>
              </div>
              <div class="mt-3 space-y-1.5">
                <div v-for="bar in quotaBars(entry)" :key="bar.label" class="grid grid-cols-[4rem_1fr_3rem] items-center gap-2 text-[11px]">
                  <span class="text-gray-500">{{ bar.label }}</span>
                  <div class="h-1.5 overflow-hidden rounded-full bg-gray-800">
                    <div v-if="bar.applicable" class="h-full rounded-full" :class="bar.class" :style="{ width: `${bar.value}%` }"></div>
                  </div>
                  <span class="text-right font-mono" :class="bar.applicable ? 'text-gray-300' : 'text-gray-500'">{{ bar.applicable ? `${bar.value}%` : 'N/A' }}</span>
                </div>
              </div>
            </div>
          </div>
          <div v-else class="rounded-xl border border-dashed border-gray-700 px-4 py-6 text-sm text-gray-500">
            暂无受管 quota 记录。第一次执行 swap_seat 或等待巡检后会记录刷新时间、适用窗口和 reset 时间。
          </div>
        </div>

        <div class="rounded-2xl border border-white/10 bg-gray-900/70 p-4">
          <div class="mb-3">
            <h3 class="text-sm font-semibold text-white">AutoTeam 受管 CPA OAuth</h3>
            <p class="mt-1 text-xs text-gray-500">这里只显示系统创建/登记的 auth，并合并受管 quota 记录；外部 CPA auth 不显示为可用资源。</p>
          </div>
          <div v-if="cpaError" class="rounded-xl border border-amber-500/20 bg-amber-500/10 px-4 py-3 text-sm text-amber-200">
            {{ cpaError }}
          </div>
          <div v-else class="space-y-3">
            <div class="grid grid-cols-2 gap-3">
              <div class="rounded-xl border border-emerald-500/20 bg-emerald-500/10 p-3">
                <div class="text-xs text-emerald-200/80">受管 active</div>
                <div class="mt-1 text-2xl font-bold text-emerald-300">{{ cpaSummary.active }}</div>
              </div>
              <div class="rounded-xl border border-white/10 bg-white/[0.03] p-3">
                <div class="text-xs text-gray-400">standby</div>
                <div class="mt-1 text-2xl font-bold text-gray-200">{{ cpaSummary.standby }}</div>
              </div>
            </div>
            <div v-if="cpaProtectedUnmanaged" class="rounded-xl border border-amber-500/20 bg-amber-500/10 px-3 py-2 text-xs text-amber-200">
              已保护 {{ cpaProtectedUnmanaged }} 个外部 CPA Codex auth：不使用、不查 quota、不启停、不删除。
            </div>
            <div v-if="activeCodexAuths.length" class="space-y-2">
              <div v-for="auth in activeCodexAuths.slice(0, 5)" :key="authName(auth) || authEmail(auth)" class="rounded-xl border border-white/10 bg-white/[0.03] p-3">
                <div class="flex items-start justify-between gap-3">
                  <div class="min-w-0">
                    <div class="truncate font-mono text-xs text-slate-200">{{ authEmail(auth) }}</div>
                    <div class="mt-1 truncate font-mono text-[11px] text-gray-500">{{ authName(auth) || '-' }}</div>
                  </div>
                  <span v-if="quotaEntryForAuth(auth)" class="shrink-0 rounded-full px-2 py-0.5 text-[11px]" :class="quotaStateClass(quotaEntryForAuth(auth))">
                    {{ quotaStateLabel(quotaEntryForAuth(auth)) }}
                  </span>
                  <span v-else class="shrink-0 rounded-full bg-amber-500/10 px-2 py-0.5 text-[11px] text-amber-300">未记录</span>
                </div>
                <div v-if="quotaEntryForAuth(auth)" class="mt-2 flex flex-wrap gap-2 text-[11px] text-gray-400">
                  <span class="rounded bg-gray-800 px-2 py-1">5h {{ quotaWindowText(quotaEntryForAuth(auth), 'primary') }}</span>
                  <span class="rounded bg-gray-800 px-2 py-1">weekly {{ quotaWindowText(quotaEntryForAuth(auth), 'weekly') }}</span>
                  <span class="rounded bg-gray-800 px-2 py-1">monthly {{ quotaWindowText(quotaEntryForAuth(auth), 'monthly') }}</span>
                  <span class="rounded bg-gray-800 px-2 py-1">{{ quotaEntryForAuth(auth).cache_state === 'blocked_until_reset' ? 'reset后可用' : '下次' }} {{ quotaNextCheckLabel(quotaEntryForAuth(auth)) }}</span>
                </div>
              </div>
              <div v-if="activeCodexAuths.length > 5" class="text-xs text-gray-500">
                还有 {{ activeCodexAuths.length - 5 }} 个 active OAuth，可到「Seat 调度」查看完整列表。
              </div>
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
const cpaFilesSummary = ref({})
const overviewLoading = ref(false)
const overviewError = ref('')
const cpaError = ref('')

const enabledTeams = computed(() => teams.value.filter(team => team.enabled !== false))
const runtimeTeams = computed(() => Array.isArray(runtimeStatus.value?.teams) ? runtimeStatus.value.teams : [])
const allQuotaEntries = computed(() => runtimeStatus.value?.quota_cache?.entries || [])
const managedAuthNames = computed(() => new Set(codexAuths.value.map(authName).filter(Boolean)))
const managedAuthEmails = computed(() => new Set(codexAuths.value.map(authEmail).filter(Boolean)))
const quotaEntries = computed(() => allQuotaEntries.value.filter(entry =>
  managedAuthNames.value.has(String(entry.auth_id || '').trim())
  || managedAuthEmails.value.has(normalizedEmail(entry.email))
))
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
const activeCodexAuths = computed(() => codexAuths.value.filter(authActive))
const cpaProtectedUnmanaged = computed(() => Number(cpaFilesSummary.value?.protected_unmanaged || 0))

const cards = computed(() => {
  const cooldownBlocked = runtimeTeams.value.filter(item => item?.cooldown && !item.cooldown.allowed).length
  return [
    { label: 'Teams', value: enabledTeams.value.length, color: 'text-white', hint: `total ${teams.value.length}` },
    { label: 'Quota 可用', value: quotaEntries.value.filter(entry => entry.quota_available).length, color: 'text-emerald-400', hint: '受管' },
    { label: 'Quota 耗尽', value: quotaEntries.value.filter(entry => entry.cache_state === 'blocked_until_reset').length, color: 'text-amber-400', hint: 'blocked' },
    { label: '最低剩余', value: quotaEntries.value.length ? `${quotaOverview.value.minRemaining}%` : '-', color: quotaOverview.value.minRemaining <= 20 ? 'text-amber-400' : 'text-emerald-400', hint: '5h/weekly/monthly' },
    { label: 'OAuth active', value: cpaSummary.value.active, color: 'text-cyan-400', hint: 'CPA' },
    { label: '冷却中', value: cooldownBlocked, color: cooldownBlocked ? 'text-amber-400' : 'text-emerald-400', hint: 'Team' },
  ]
})

const quotaOverview = computed(() => {
  const entries = quotaEntries.value
  const stale = entries.filter(entry => entry.cache_state === 'stale').length
  if (!entries.length) {
    return {
      title: '暂无受管 quota 记录',
      detail: '执行 swap_seat 或等待自动巡检后，会记录每个受管 OAuth 的剩余额度。',
      minRemaining: 0,
      stale: 0,
      level: 'empty',
    }
  }
  const exhausted = entries.filter(entry => entry.cache_state === 'blocked_until_reset' || !entry.quota_available).length
  const minRemaining = Math.min(...entries.map(minQuotaRemaining))
  if (exhausted) {
    return {
      title: '有受管账号 quota 已耗尽',
      detail: `${exhausted} 个账号不会被选为 active；系统会等 reset 后再重新纳入调度。`,
      minRemaining,
      stale,
      level: 'warn',
    }
  }
  if (minRemaining <= 20) {
    return {
      title: 'quota 偏低，需要观察',
      detail: `最低窗口只剩 ${minRemaining}%，下一轮巡检会重新检查并自动调整 active。`,
      minRemaining,
      stale,
      level: 'warn',
    }
  }
  if (stale) {
    return {
      title: '上次记录可用，等待复查',
      detail: `${stale} 条记录已超过快速缓存时间；下一轮巡检会重新读取 CPA quota。`,
      minRemaining,
      stale,
      level: 'stale',
    }
  }
  return {
    title: '受管 quota 状态健康',
    detail: `${entries.length} 个受管 OAuth 都有可用 quota，swap 会按剩余量自动选择 active。`,
    minRemaining,
    stale,
    level: 'ok',
  }
})

const quotaOverviewClass = computed(() => {
  if (quotaOverview.value.level === 'warn') return 'border-amber-500/20 bg-amber-500/10'
  if (quotaOverview.value.level === 'stale') return 'border-sky-500/20 bg-sky-500/10'
  if (quotaOverview.value.level === 'ok') return 'border-emerald-500/20 bg-emerald-500/10'
  return 'border-white/10 bg-white/[0.03]'
})

function authActive(auth) {
  return !auth?.disabled && String(auth?.status || '').toLowerCase() === 'active'
}

function authEmail(auth) {
  return String(auth?.email || auth?.account || '').trim().toLowerCase()
}

function authName(auth) {
  return String(auth?.name || auth?.id || auth?.auth_index || '').trim()
}

function normalizedEmail(value) {
  return String(value || '').trim().toLowerCase()
}

function quotaEntryForAuth(auth) {
  const email = authEmail(auth)
  const name = authName(auth)
  const accountId = String(auth?.account_id || auth?.accountId || auth?.chatgpt_account_id || '').trim()

  if (name) {
    const byName = quotaEntries.value.find(entry => String(entry.auth_id || '').trim() === name)
    if (byName) return byName
  }

  if (!email) return null
  const scoped = quotaEntries.value.find(entry =>
    normalizedEmail(entry.email) === email
    && (!accountId || String(entry.account_id || '').trim() === accountId)
  )
  if (scoped) return scoped
  return quotaEntries.value.find(entry => normalizedEmail(entry.email) === email) || null
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
      cpaFilesSummary.value = value?.summary || {}
    } else {
      cpaAuths.value = []
      cpaFilesSummary.value = {}
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

function clampPercent(value) {
  return Math.max(0, Math.min(100, Number(value || 0)))
}

function minQuotaRemaining(entry) {
  const values = []
  if (entry?.primary_applicable !== false) values.push(clampPercent(entry?.primary_remaining))
  if (entry?.weekly_applicable !== false) values.push(clampPercent(entry?.weekly_remaining))
  if (entry?.monthly_applicable !== false) values.push(clampPercent(entry?.monthly_remaining))
  return values.length ? Math.min(...values) : 0
}

function quotaBarClass(value) {
  if (value <= 0) return 'bg-rose-400'
  if (value <= 20) return 'bg-amber-400'
  return 'bg-emerald-400'
}

function quotaBars(entry) {
  return [
    {
      label: '5h',
      value: clampPercent(entry?.primary_remaining),
      applicable: entry?.primary_applicable !== false,
      class: quotaBarClass(clampPercent(entry?.primary_remaining)),
    },
    {
      label: 'weekly',
      value: clampPercent(entry?.weekly_remaining),
      applicable: entry?.weekly_applicable !== false,
      class: quotaBarClass(clampPercent(entry?.weekly_remaining)),
    },
    {
      label: 'monthly',
      value: clampPercent(entry?.monthly_remaining),
      applicable: entry?.monthly_applicable !== false,
      class: quotaBarClass(clampPercent(entry?.monthly_remaining)),
    },
  ]
}

function quotaWindowText(entry, window) {
  if (entry?.[`${window}_applicable`] === false) return 'N/A'
  return `${clampPercent(entry?.[`${window}_remaining`])}%`
}

function quotaNextCheckLabel(entry) {
  const ts = entry?.next_check_at || entry?.exhausted_until
  if (ts) return formatTs(ts)
  if (entry?.cache_state === 'stale') return '下轮巡检'
  return '-'
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
  if (entry.cache_state === 'stale') return entry.quota_available ? '可用待复查' : '待复查'
  return entry.status || '-'
}

function quotaStateClass(entry) {
  if (entry.cache_state === 'blocked_until_reset') return 'bg-amber-500/10 text-amber-300'
  if (entry.cache_state === 'recent_ok') return 'bg-emerald-500/10 text-emerald-300'
  if (entry.cache_state === 'stale') return entry.quota_available ? 'bg-sky-500/10 text-sky-300' : 'bg-gray-500/10 text-gray-300'
  return entry.quota_available ? 'bg-emerald-500/10 text-emerald-300' : 'bg-gray-500/10 text-gray-300'
}

onMounted(loadOverview)
</script>
